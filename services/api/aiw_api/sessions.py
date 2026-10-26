"""Session manager.

* one running AgentLoop per machine; follow-ups for a busy machine wait in a
  durable queue (tasks with status=queued) and start in order
* threads: every task belongs to a conversation; earlier turns + memory are
  given to the model as context so the user can say "now make the title bigger"
* event fan-out to WebSocket subscribers, L3 confirmations, ask_user answers
* notifications on finish / failure / confirmation / question
* recovery: tasks left running when the API restarted are marked interrupted and,
  if enabled, re-queued automatically
* optional isolated runs on a throw-away PVE clone
* dispatch: one request split into child tasks that run on several computers at once (each child is a task in
  its own child thread); the parent waits for them, gets their summaries and files, and combines the result
"""
from __future__ import annotations

import os
import re

import asyncio
import base64
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from agent_runtime import AgentControl, AgentLoop, LoopConfig
from aiw_protocols import AgentEvent, EventType, Task, TaskStatus
from computer_runtime import Computer, LinuxComputerProvider
from llm_router import LLMRouter
from tool_router import ToolExecutor

from .store import Store

log = logging.getLogger("aiw.sessions")
LIVE_STATES = ("running", "paused", "takeover", "waiting_confirm", "waiting_input")
FINAL_STATES = ("succeeded", "failed", "stopped", "interrupted", "cancelled")


@dataclass
class LiveSession:
    id: str
    task: Task
    control: AgentControl
    runner: Optional[asyncio.Task] = None
    subscribers: set = field(default_factory=set)
    confirm: Optional[asyncio.Future] = None
    confirm_info: Optional[dict] = None
    question: Optional[asyncio.Future] = None
    question_info: Optional[dict] = None
    cleanup: Optional[object] = None
    loop: Optional[object] = None  # the running AgentLoop: confirmations extend its deadline


class SessionManager:
    def __init__(self, store: Store, settings, llm_factory=None, computer_factory=None, notifier=None,
                 secrets=None, vm_runtime=None):
        self.store, self.settings = store, settings
        self.notifier, self.secrets, self.vm_runtime = notifier, secrets, vm_runtime
        self.live: dict[str, LiveSession] = {}
        self._custom_llm = llm_factory
        self._computer_factory = computer_factory or self._default_computer
        self._computers: dict[str, Computer] = {}
        self.held_threads: set[str] = set()   # queue paused because the user pressed stop
        self._drain_lock = asyncio.Lock()
        self.cloud = None  # CloudManager, set by the API
        self._health: dict[str, tuple[float, bool]] = {}   # machine -> (checked at, vm-agent answered)

    # ------------------------------------------------------------------ factories
    def _default_computer(self, machine: dict) -> Computer:
        return Computer(LinuxComputerProvider(machine["agent_endpoint"], self.settings.agent_token), machine["id"])

    def computer(self, machine_id: str) -> Computer:
        if machine_id not in self._computers:
            m = self.store.machine(machine_id)
            if not m:
                raise KeyError(machine_id)
            self._computers[machine_id] = self._computer_factory(m)
        return self._computers[machine_id]

    def drop_computer(self, machine_id: str) -> None:
        """Forget the cached connection, e.g. after the VM's address changed in 设置."""
        c = self._computers.pop(machine_id, None)
        close = getattr(getattr(c, "provider", None), "close", None)
        if close:
            try:
                res = close()
                if asyncio.iscoroutine(res):
                    asyncio.get_event_loop().create_task(res)
            except Exception:  # noqa: BLE001
                pass

    def model_settings(self) -> dict:
        return self.store.kv_get("models", {}) or {}

    def _llm(self) -> LLMRouter:
        if self._custom_llm:
            return self._custom_llm()
        m = self.model_settings()
        key = self.secrets.get("openai_api_key") if self.secrets else None
        return LLMRouter.from_settings(m.get("model"), m.get("fallback"), key, m.get("base_url") or None, self._tool_choices(), m.get("thinking"))

    def _task_llm(self, prompt: str) -> tuple[LLMRouter, Optional[str]]:
        """The main model, or the video model for making/editing a video (设置 video_model / AIW_VIDEO_MODEL).
        The reference explainer the user liked was made by Claude Opus; task-8bd on deepseek-v4-pro cut a 16-page deck
        to 8 pages. The main model stays as the fallback, so a broken video model never stops the task."""
        if self._custom_llm:
            return self._custom_llm(), None
        m = self.model_settings()
        vm = m.get("video_model") or os.environ.get("AIW_VIDEO_MODEL")
        from agent_runtime.video import is_video_production
        if not vm or vm == m.get("model") or not is_video_production(prompt):
            return self._llm(), None
        key = self.secrets.get("openai_api_key") if self.secrets else None
        return LLMRouter.from_settings(vm, m.get("model") or m.get("fallback"), key, m.get("base_url") or None,
                                       self._tool_choices(), m.get("thinking")), vm

    def _subagent_llm(self) -> Optional[LLMRouter]:
        if self._custom_llm:
            return None
        m = self.model_settings()
        if not m.get("subagent_model"):
            return None
        key = self.secrets.get("openai_api_key") if self.secrets else None
        return LLMRouter.from_settings(m["subagent_model"], None, key, m.get("base_url") or None, self._tool_choices(), m.get("thinking"))

    def _tool_choices(self) -> dict:
        """tool_choice that the probe found working for each model ('auto' for gateways that break on 'required')."""
        probes = self.store.kv_get("model_probes", {}) or {}
        return {k: v.get("tool_choice") for k, v in probes.items() if v.get("tool_choice")}

    def _vision_llm(self) -> Optional[LLMRouter]:
        if self._custom_llm:
            return None
        m = self.model_settings()
        if not m.get("vision_model") or m.get("vision_model") == m.get("model"):
            return None
        key = self.secrets.get("openai_api_key") if self.secrets else None
        # the eyes describe pictures; thinking there only adds latency (AIW_VISION_THINKING=on to restore)
        return LLMRouter.from_settings(m["vision_model"], None, key, m.get("base_url") or None, None,
                                       os.environ.get("AIW_VISION_THINKING", "off"))

    def _geometry_llm(self) -> Optional[LLMRouter]:
        """Model that boxes things for exact image moves (AIW_GEOMETRY_MODEL / 设置 geometry_model), else the eyes."""
        if self._custom_llm:
            return None
        m = self.model_settings()
        name = m.get("geometry_model") or os.environ.get("AIW_GEOMETRY_MODEL")
        if not name:
            return None
        key = self.secrets.get("openai_api_key") if self.secrets else None
        return LLMRouter.from_settings(name, None, key, m.get("base_url") or None, None, "off")

    def _image_cfg(self) -> Optional[dict]:
        """The AI image-editing model (image_edit tool), through the same gateway and key as the other models."""
        if self._custom_llm:
            return None
        m = self.model_settings()
        model = m.get("image_model") or os.environ.get("AIW_IMAGE_MODEL")
        key = (self.secrets.get("openai_api_key") if self.secrets else None) or os.environ.get("OPENAI_API_KEY")
        if not model or not key:
            return None
        base = m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return {"model": model, "key": key, "base_url": base}

    # ------------------------------------------------------------------ context
    def build_context(self, thread_id: Optional[str], exclude_task: Optional[str] = None) -> Optional[str]:
        parts = []
        prof = self.store.kv_get("profile", {}) or {}
        if prof.get("name") or prof.get("tone") or prof.get("about"):
            parts.append("USER PROFILE: " + "; ".join(f"{k}: {prof[k]}" for k in ("name", "tone", "about") if prof.get(k)))
        mems = self.store.all("memories", limit=40)
        if mems:
            parts.append("MEMORY (facts the user told you earlier):\n" + "\n".join(f"- {m['text']}" for m in reversed(mems)))
        if thread_id:
            turns = [t for t in self.store.thread_tasks(thread_id) if t["id"] != exclude_task
                     and t["status"] not in ("queued", "cancelled")][-6:]
            if turns:
                parts.append("EARLIER IN THIS CONVERSATION (oldest first):\n" + "\n".join(
                    f"- user: {t['prompt'][:400]}\n  result ({t['status']}): {(t['result'] or '')[:400]}" for t in turns))
        return "\n\n".join(parts) or None

    # ------------------------------------------------------------------ submit / queue
    def busy_session(self, machine_id: str) -> Optional[LiveSession]:
        for s in self.live.values():
            if s.task.machine_id == machine_id and s.runner and not s.runner.done():
                return s
        return None

    async def submit(self, prompt: str, machine_id: Optional[str] = None, thread_id: Optional[str] = None,
                     source: str = "chat", max_steps: int = 40, timeout_s: float = float(os.environ.get("AIW_TASK_TIMEOUT", "900")), title: Optional[str] = None,
                     isolated: bool = False) -> dict:
        machine_id = machine_id or (self.settings.machines[0].id if self.settings.machines else "ai-linux-001")
        if not self.store.machine(machine_id):
            raise KeyError(machine_id)
        if not thread_id or not self.store.thread(thread_id):
            thread_id = self.store.create_thread(title or _title(prompt))["id"]
        max_steps, timeout_s = _budget(prompt, max_steps, timeout_s)
        task = Task(prompt=prompt, machine_id=machine_id, max_steps=max_steps, timeout_s=timeout_s,
                    thread_id=thread_id, source=source)
        row = task.model_dump()
        row["status"] = "queued"
        self.store.create_task(row)
        if isolated:
            self.store.kv_set(f"isolated:{task.id}", True)
        queued = bool(self.busy_session(machine_id)) or thread_id in self.held_threads
        if not queued:
            await self._start_task_row(self.store.task(task.id))
        else:
            await self._event_all({"kind": "task", "task": self.store.task(task.id)})
        t = self.store.task(task.id)
        return {"task": t, "session_id": t.get("session_id"), "queued": queued, "thread_id": thread_id}

    async def _start_task_row(self, row: dict, allow_busy: bool = False) -> LiveSession:
        ctx = self.build_context(row.get("thread_id"), exclude_task=row["id"])
        if row.get("parent_id"):
            ctx = _child_note(self.store.task(row["parent_id"]) or {}) + ("\n\n" + ctx if ctx else "")
        steps, secs = _budget(row["prompt"], row.get("max_steps") or 40,
                              row.get("timeout_s") or float(os.environ.get("AIW_TASK_TIMEOUT", "900")), ctx)
        task = Task(id=row["id"], prompt=row["prompt"], machine_id=row["machine_id"], thread_id=row.get("thread_id"),
                    source=row.get("source") or "chat", max_steps=steps, timeout_s=secs)
        task.context = ctx
        return await self.start(task, allow_busy=allow_busy)

    async def drain(self, machine_id: str):
        async with self._drain_lock:
            if self.busy_session(machine_id):
                return
            for row in self.store.tasks_by_status(("queued",)):
                if row["machine_id"] != machine_id or row.get("thread_id") in self.held_threads:
                    continue
                try:
                    await self._start_task_row(row)
                except Exception as e:  # noqa: BLE001
                    self.store.update_task(row["id"], "failed", f"could not start: {e}")
                    continue
                return

    async def resume_queue(self, thread_id: str):
        self.held_threads.discard(thread_id)
        for m in {r["machine_id"] for r in self.store.thread_tasks(thread_id) if r["status"] == "queued"}:
            await self.drain(m)

    def cancel_queued(self, task_id: str) -> bool:
        t = self.store.task(task_id)
        if not t or t["status"] != "queued":
            return False
        self.store.update_task(task_id, "cancelled", "removed from queue")
        return True

    async def retry(self, task_id: str) -> dict:
        t = self.store.task(task_id)
        if not t:
            raise KeyError(task_id)
        return await self.submit(t["prompt"], t["machine_id"], t.get("thread_id"), source="retry")

    async def recover_interrupted(self, requeue: bool = True) -> list[str]:
        """Called at startup: no session survives a restart, so finish the bookkeeping and re-queue."""
        out = []
        for t in self.store.tasks_by_status(LIVE_STATES):
            self.store.update_task(t["id"], "interrupted", (t.get("result") or "") + " [API restarted]")
            if t.get("session_id"):
                self.store.end_session(t["session_id"], "interrupted")
            if t.get("parent_id"):
                continue   # a child is re-dispatched by its parent's resume, never on its own
            if requeue and time.time() - (t.get("updated_at") or 0) < 1800:   # only recent work; stale runs just get marked
                res = await self.submit(f"（继续之前被中断的任务）{t['prompt']}", t["machine_id"], t.get("thread_id"),
                                        source="resume")
                out.append(res["task"]["id"])
        for t in self.store.tasks_by_status(("queued",)):
            if t.get("parent_id"):   # its parent died with the restart: nobody waits for this piece any more
                self.store.update_task(t["id"], "cancelled", "parent task interrupted")
        for m in {t["machine_id"] for t in self.store.tasks_by_status(("queued",))}:
            await self.drain(m)
        return out

    # ------------------------------------------------------------------ run
    async def start(self, task: Task, allow_busy: bool = False) -> LiveSession:
        # allow_busy: a dispatched child on its parent's own computer (the parent only waits while it runs)
        if self.busy_session(task.machine_id) and not allow_busy:
            raise RuntimeError(f"machine {task.machine_id} already has a running session")
        parent_id = (self.store.task(task.id) or {}).get("parent_id")
        sid = self.store.create_session(task.machine_id, task.id)
        self.store.set_task_session(task.id, sid)
        self.store.update_task(task.id, TaskStatus.RUNNING.value)
        live = LiveSession(sid, task, AgentControl())
        self.live[sid] = live
        plan_state: dict = {"steps": [], "done": []}

        async def emit(ev: AgentEvent):
            d = ev.model_dump(mode="json")
            if ev.type == EventType.THINKING and ev.data.get("live"):
                # the reasoning text while the model is still thinking: shown live, never stored (the final
                # reasoning is kept on agent.decision)
                await self._broadcast(live, {"kind": "event", "task_id": task.id, "thread_id": task.thread_id, **d})
                return
            self.store.add_event(d)
            if ev.type == EventType.OBSERVATION and ev.data.get("artifact"):
                self.store.add_artifact(sid, "screenshot", ev.data["artifact"])
            if ev.type == EventType.FILE and ev.data.get("artifact"):
                self.store.add_artifact(sid, "file", ev.data["artifact"])
            if ev.type == EventType.PLAN:
                plan_state.update(steps=ev.data.get("plan") or [], done=[])
                self.store.set_task_plan(task.id, plan_state)
            if ev.type == EventType.PLAN_UPDATE:
                plan_state["done"] = ev.data.get("done") or []
                self.store.set_task_plan(task.id, plan_state)
            if ev.type == EventType.MEMORY and ev.data.get("text"):
                pass  # saved by remember()
            msg = {"kind": "event", "task_id": task.id, "thread_id": task.thread_id, **d}
            await self._broadcast(live, msg)
            if ev.type in (EventType.TASK_STARTED, EventType.TASK_FINISHED, EventType.TASK_FAILED, EventType.PLAN,
                           EventType.PLAN_UPDATE, EventType.CONFIRM_REQUIRED, EventType.INPUT_REQUIRED):
                await self._event_all(msg)

        async def audit(rec: dict):
            self.store.add_action(sid, rec)

        async def confirm(tool, payload, decision) -> bool:
            live.confirm = asyncio.get_running_loop().create_future()
            live.confirm_info = {"tool": tool, "payload": payload, "level": int(decision.level), "reason": decision.reason}
            self.store.update_task(task.id, TaskStatus.WAITING_CONFIRM.value)
            await emit(AgentEvent(session_id=sid, type=EventType.CONFIRM_REQUIRED, data=live.confirm_info))
            await self._notify("confirm", "需要你确认", f"{decision.reason}：{tool}", task)
            t0 = time.monotonic()
            try:
                # a price-watch order waits up to 30 min for the user (payload confirm_timeout_s); everything else 5 min
                wait = float(payload.get("confirm_timeout_s") or 300) if isinstance(payload, dict) else 300.0
                return await asyncio.wait_for(live.confirm, wait)
            except asyncio.TimeoutError:
                return False
            finally:
                if getattr(live, "loop", None) is not None:
                    live.loop.add_wait(time.monotonic() - t0)
                live.confirm = live.confirm_info = None
                self.store.update_task(task.id, TaskStatus.RUNNING.value)

        async def ask(question: str, options: list, extra: Optional[dict] = None) -> Optional[str]:
            live.question = asyncio.get_running_loop().create_future()
            live.question_info = {"question": question, "options": options}
            if extra:  # a form card to fill, or a takeover request
                live.question_info["form"] = extra
            self.store.update_task(task.id, TaskStatus.WAITING_INPUT.value)
            kind = (extra or {}).get("kind")
            await self._notify("input", "需要你填写资料" if kind == "form" else "需要你接管桌面" if kind == "takeover"
                               else "Agent 在问你", question, task)
            try:
                # filling a form or passing a captcha takes longer than answering a question
                wait = float(self.settings.ask_timeout_s) * (3 if extra else 1)
                return await asyncio.wait_for(live.question, wait)
            except asyncio.TimeoutError:
                return None
            finally:
                live.question = live.question_info = None
                self.store.update_task(task.id, TaskStatus.RUNNING.value)

        async def remember(text: str):
            self.store.insert("memories", {"text": text[:500], "source": task.id})

        prof = self.store.kv_get("profile", {}) or {}
        system_extra = f"Agent name: {prof['agent_name']}." if prof.get("agent_name") else ""
        if prof.get("tone"):
            system_extra += f" Reply tone for summaries: {prof['tone']}."
        try:
            from agent_runtime import skills as _skills
            sk = _skills.brief(task.prompt, self.store.kv_get("skills_custom", []) or [])
            if sk:
                system_extra += "\n" + sk
        except Exception:  # noqa: BLE001 - a bad custom skill entry never blocks a task
            pass

        cleanup = None
        computer = None
        if self.store.kv_get(f"isolated:{task.id}"):
            try:
                computer, cleanup = await self._isolated_computer(task, emit, sid)
            except Exception as e:  # noqa: BLE001
                self.store.update_task(task.id, "failed", f"isolated VM failed: {e}")
                self.store.end_session(sid, "failed")
                del self.live[sid]
                raise RuntimeError(f"isolated VM failed: {e}")
        computer = computer or self.computer(task.machine_id)
        live.cleanup = cleanup

        async def runner():
            nonlocal computer
            self.store.update_task(task.id, TaskStatus.RUNNING.value)
            try:
                # integrations (MCP servers) are loaded here, off the request path: POST /api/tasks returns at once
                from .integrations import load_tools
                try:
                    extra = await load_tools(self.store, self.secrets) if self.secrets else {}
                except Exception as e:  # noqa: BLE001
                    log.warning("integrations unavailable: %s", e)
                    extra = {}
                try:  # the 开发 skill's way onto the user's deploy server (only when one is set up)
                    from . import deployer
                    if self.secrets and deployer.cfg(self.store).get("host"):
                        sch, fn = deployer.tool(self.store, self.secrets, computer)
                        extra["deploy_server"] = (sch, fn)
                except Exception as e:  # noqa: BLE001
                    log.warning("deploy tool unavailable: %s", e)
                try:  # hosts the user authorised for ops work (设置 › 电脑 › 运维主机)
                    from . import remote_hosts
                    if self.secrets and remote_hosts.hosts(self.store):
                        extra["remote_exec"] = remote_hosts.tool(self.store, self.secrets)
                except Exception as e:  # noqa: BLE001
                    log.warning("remote_exec unavailable: %s", e)
                hold = contextlib.nullcontext()
                cloud = getattr(self, "cloud", None)
                if cloud and cloud.is_cloud(task.machine_id) and not cleanup:
                    # an InstaBox machine may be paused (30 idle minutes): wake it, and keep it awake till the end
                    await cloud.wake(task.machine_id)
                    hold = cloud.hold(task.machine_id)
                    computer = self.computer(task.machine_id)   # a restarted agent means a fresh connection
                sx = system_extra
                can_dispatch = not parent_id and not cleanup and len(self.store.machines()) > 1
                if can_dispatch:
                    try:
                        sx += "\n" + roster_text(await self.roster(probe_timeout=3), task.machine_id)
                    except Exception as e:  # noqa: BLE001
                        log.warning("roster unavailable: %s", e)
                try:  # 京东/淘宝 price watches: price_watch (create from chat); order_confirm for a watch's order task
                    from . import shopwatch
                    extra.update(shopwatch.task_tools(self.store, self.notifier, task, computer, confirm))
                except Exception as e:  # noqa: BLE001
                    log.warning("price watch tools unavailable: %s", e)
                xhs_task = False
                try:  # 小红书运营: caps, the approval queue and the warning pause live in xhs.py
                    from . import xhs
                    xhs_task = xhs.is_xhs_task(task)
                    if xhs_task:
                        extra.update(xhs.task_tools(self.store, self.notifier, task, computer, confirm, self))
                except Exception as e:  # noqa: BLE001
                    log.warning("xhs tools unavailable: %s", e)
                async with hold:
                    llm, routed = self._task_llm(task.prompt)
                    if routed:
                        await emit(AgentEvent(session_id=sid, type=EventType.MESSAGE, data={"text": f"视频任务，改用视频模型 {routed}"}))
                    loop = AgentLoop(task, sid, computer, llm, ToolExecutor(computer, confirm=confirm, audit=audit),
                                     control=live.control, emit=emit, ask=ask, remember=remember, extra_tools=extra,
                                     subagent_llm=self._subagent_llm(), vision_llm=self._vision_llm(), image_cfg=self._image_cfg(),
                                     system_extra=sx.strip(),
                                     config=LoopConfig(max_steps=task.max_steps, timeout_s=task.timeout_s,
                                                       artifacts_dir=self.settings.artifacts_dir))
                    loop.geometry_llm = self._geometry_llm()
                    from . import shopwatch
                    if shopwatch.is_order_task(task):   # no 提交订单/支付 click before the user confirms
                        shopwatch.guard(loop.tools, shopwatch.ensure_state(task.id), computer)
                    if xhs_task:   # nothing goes out on 小红书 unless the user approved it
                        from . import xhs
                        xhs.guard(loop.tools, self.store, xhs.task_state(task.id), self.notifier, computer, self, task.id)
                    if can_dispatch:
                        async def _dispatcher(items, timeout_s, _live=live, _emit=emit):
                            return await self.dispatch(_live, items, timeout_s, _emit)
                        loop.dispatcher = _dispatcher
                    live.loop = loop
                    t = await loop.run()
                status, result = t.status.value, t.result
            except Exception as e:  # noqa: BLE001
                status, result = "failed", f"{type(e).__name__}: {e}"
            self.store.update_task(task.id, status, result)
            self.store.end_session(sid, status)
            if not parent_id:   # a parent that ended (failed, timed out, stopped) leaves no child running
                try:
                    await self.stop_children(task.id)
                except Exception as e:  # noqa: BLE001
                    log.warning("stop children: %s", e)
            if cleanup:
                try:
                    await cleanup()
                except Exception as e:  # noqa: BLE001
                    log.warning("isolated cleanup: %s", e)
            if status == "succeeded":
                await self._notify("done", "任务完成", result or "", task)
            elif status == "stopped":
                if task.thread_id:
                    self.held_threads.add(task.thread_id)
            else:
                await self._notify("failed", "任务失败", result or "", task)
            await self._event_all({"kind": "task", "task": self.store.task(task.id)})
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.drain(task.machine_id)))

        live.runner = asyncio.create_task(runner())
        return live

    async def _isolated_computer(self, task: Task, emit, sid):
        """Full clone of the machine's VM from snapshot AIW_CLONE_SNAPSHOT, started, used once, destroyed."""
        if not self.vm_runtime:
            raise RuntimeError("PVE is not configured")
        src = self.vm_runtime.machines[task.machine_id]
        await emit(AgentEvent(session_id=sid, type=EventType.MESSAGE, data={"text": "正在克隆一次性隔离电脑…"}))
        info = await self.vm_runtime.provider.clone(src, name=f"aiw-iso-{task.id[-6:]}", snapshot=self.settings.clone_snapshot)
        vmid = info["vmid"]

        async def cleanup():
            await self.vm_runtime.provider.destroy(vmid)

        try:
            await self.vm_runtime.provider.start(vmid)
            ip = None
            for _ in range(90):
                st = await self.vm_runtime.provider.status(vmid)
                if st.ip:
                    ip = st.ip
                    break
                await asyncio.sleep(2)
            if not ip:
                raise RuntimeError("clone got no IP (is qemu-guest-agent installed?)")
            comp = Computer(LinuxComputerProvider(f"ws://{ip}:8765", self.settings.agent_token), f"iso-{vmid}")
            for _ in range(60):
                try:
                    h = await asyncio.wait_for(comp.health(), 5)
                    if h.success:
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(2)
            await emit(AgentEvent(session_id=sid, type=EventType.MESSAGE, data={"text": f"隔离电脑已就绪 (VM {vmid}, {ip})"}))
            return comp, cleanup
        except Exception:
            await cleanup()
            raise

    async def _notify(self, kind: str, title: str, body: str, task: Task):
        if self.notifier:
            try:
                await self.notifier.notify(kind, title, body, task_id=task.id, thread_id=task.thread_id)
            except Exception as e:  # noqa: BLE001
                log.warning("notify failed: %s", e)

    async def _event_all(self, msg: dict):
        if self.notifier:
            await self.notifier.broadcast(msg)

    async def _broadcast(self, live: LiveSession, msg: dict):
        dead = []
        for ws in list(live.subscribers):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            live.subscribers.discard(ws)

    # ------------------------------------------------------------------ dispatch (several computers at once)
    async def _probe(self, mid: str, timeout: float = 5.0) -> bool:
        c = self._health.get(mid)
        if c and time.time() - c[0] < 30:
            return c[1]
        try:
            h = await asyncio.wait_for(self.computer(mid).health(), timeout)
            ok = bool(h.success and ((h.result or {}).get("ok", True) if isinstance(h.result, dict) else True))
        except Exception:  # noqa: BLE001
            ok = False
        self._health[mid] = (time.time(), ok)
        return ok

    async def roster(self, probe_timeout: float = 5.0) -> list[dict]:
        """Every computer with what dispatch needs: online/asleep/offline and busy. A paused cloud computer counts as
        available: the child task's start wakes it the way every task does (cloud.wake)."""
        cloud = getattr(self, "cloud", None)

        async def one(m: dict) -> dict:
            mid = m["id"]
            busy = self.busy_session(mid)
            info = {"id": mid, "name": m.get("name") or mid, "kind": "local", "available": False, "asleep": False,
                    "state": "unknown", "busy_task": busy.task.id if busy else None}
            if cloud and cloud.is_cloud(mid):
                info["kind"] = "cloud"
                e = cloud.entry(mid) or {}
                if e.get("status") != "ready":
                    info["state"] = e.get("status") or "not set up"
                    return info
                try:
                    st = await asyncio.wait_for(cloud.state(mid), probe_timeout + 3)
                except Exception:  # noqa: BLE001
                    st = {"state": "unknown"}
                info["state"] = st.get("state") or "unknown"
                from .cloud import _cooldown_left
                if _cooldown_left(st.get("cooldown_until")) > 0:
                    info["state"] = "cooldown"
                    return info
                info["available"] = info["state"] not in ("error", "deleted", "failed")
                info["asleep"] = info["state"] != "running"
                return info
            if busy:
                info.update(state="online", available=True)
            else:
                ok = await self._probe(mid, probe_timeout)
                info.update(state="online" if ok else "offline", available=ok)
            return info
        return list(await asyncio.gather(*(one(m) for m in self.store.machines())))

    def children_view(self, task_id: str) -> list[dict]:
        out = []
        for r in self.store.children(task_id):
            m = self.store.machine(r["machine_id"]) or {}
            out.append({"task_id": r["id"], "thread_id": r.get("thread_id"), "machine": r["machine_id"],
                        "machine_name": m.get("name") or r["machine_id"], "goal": (r.get("prompt") or "")[:300],
                        "status": r["status"], "result": (r.get("result") or "")[:500], "session_id": r.get("session_id")})
        return out

    async def stop_children(self, task_id: str) -> int:
        """Stop on a parent stops every child it dispatched: running ones are stopped, queued ones dropped."""
        n = 0
        for r in self.store.children(task_id):
            if r["status"] == "queued":
                n += self.cancel_queued(r["id"])
                continue
            live = self.live_for_task(r["id"])
            if live and live.runner and not live.runner.done() and live.control.state.value != "stopped":
                await self.control(live.id, "stop")
                n += 1
        return n

    async def dispatch(self, parent: LiveSession, items: list[dict], timeout_s: float, emit) -> list[dict]:
        """Run `items` ({machine, goal}) as child tasks, one child thread each, on several computers at once; wait for
        all of them (overall `timeout_s`) and return each one's status, summary and files. The files are copied into
        ~/workspace/dispatch/ on the parent's computer so the parent can merge them.
        Placement: a named idle computer takes its item; "auto" (or an offline/unknown name) takes an idle online one,
        the parent's own computer last; a busy computer's item moves to an idle one if there is one, else waits in that
        computer's queue. Items on the parent's own computer run one after another while the parent waits, which is
        also the fallback when it is the only computer."""
        ptask = parent.task
        own = ptask.machine_id
        roster = await self.roster()
        plan = _place(items, roster, own)
        names = {m["id"]: m["name"] for m in roster}
        kids: list[dict] = []
        for it, mid, queued in plan:
            th = self.store.create_thread("↳ " + _title(it["goal"]), parent_task_id=ptask.id, parent_thread_id=ptask.thread_id)
            steps, secs = _budget(it["goal"], 40, max(float(os.environ.get("AIW_TASK_TIMEOUT", "900")), timeout_s))
            t = Task(prompt=it["goal"], machine_id=mid, thread_id=th["id"], source="dispatch", max_steps=steps, timeout_s=secs)
            row = t.model_dump()
            row.update(status="queued", parent_id=ptask.id)
            self.store.create_task(row)
            kids.append({"task_id": t.id, "thread_id": th["id"], "machine": mid, "machine_name": names.get(mid, mid),
                         "goal": it["goal"], "status": "queued", "queued": queued})

        async def say(phase: str):
            step = getattr(getattr(parent, "loop", None), "step", None)
            await emit(AgentEvent(session_id=parent.id, type=EventType.DISPATCH, step=step, data={
                "phase": phase, "children": [{k: c.get(k) for k in ("task_id", "thread_id", "machine", "machine_name",
                                                                    "goal", "status", "queued")} for c in kids]}))

        await say("start")
        for c in kids:   # the other computers start now (or queue behind what they are doing)
            if c["machine"] == own:
                continue
            if self.busy_session(c["machine"]):
                c["queued"] = True
                continue
            try:
                await self._start_task_row(self.store.task(c["task_id"]))
            except Exception as e:  # noqa: BLE001  (busy by now: it stays queued and drain starts it)
                if not self.busy_session(c["machine"]):
                    self.store.update_task(c["task_id"], "failed", f"could not start: {e}")
                else:
                    c["queued"] = True

        async def run_own():
            for c in kids:
                if c["machine"] != own:
                    continue
                row = self.store.task(c["task_id"]) or {}
                if row.get("status") != "queued":   # stopped / cancelled meanwhile
                    continue
                try:
                    lv = await self._start_task_row(row, allow_busy=True)
                except Exception as e:  # noqa: BLE001
                    self.store.update_task(c["task_id"], "failed", f"could not start: {e}")
                    continue
                await asyncio.wait({lv.runner})   # wait, never cancel the child from here

        own_job = asyncio.ensure_future(run_own())
        poll = float(os.environ.get("AIW_DISPATCH_POLL", "1.0"))
        deadline = time.monotonic() + timeout_s
        timed_out = False
        last = None
        try:
            while True:
                for c in kids:
                    c["status"] = (self.store.task(c["task_id"]) or {}).get("status", "failed")
                sig = tuple(c["status"] for c in kids)
                if sig != last:
                    last = sig
                    if any(st != "queued" for st in sig):
                        await say("update")
                if all(st in FINAL_STATES for st in sig) and own_job.done():
                    break
                if parent.control.state.value == "stopped":
                    await self.stop_children(ptask.id)
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    await self.stop_children(ptask.id)
                    end = time.monotonic() + 15
                    while time.monotonic() < end and not all(
                            (self.store.task(c["task_id"]) or {}).get("status") in FINAL_STATES for c in kids):
                        await asyncio.sleep(0.2)
                    break
                await asyncio.sleep(poll)
        except asyncio.CancelledError:   # the parent was stopped mid-wait
            await self.stop_children(ptask.id)
            raise
        finally:
            if not own_job.done():
                own_job.cancel()

        comp = getattr(getattr(parent, "loop", None), "computer", None) or self.computer(own)
        out = []
        for i, c in enumerate(kids, 1):
            row = self.store.task(c["task_id"]) or {}
            c["status"] = row.get("status", "failed")
            sid = row.get("session_id")
            arts = [a for a in self.store.artifacts(sid) if a["kind"] == "file"] if sid else []
            local = []
            for a in arts:
                path = a["path"]
                try:
                    if os.path.getsize(path) > 40 * 1024 * 1024:
                        continue
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    dest = f"dispatch/{i}-{_slug(c['machine'])}/{os.path.basename(path)}"
                    r = await comp.execute("files.upload", {"path": dest, "content_b64": b64}, observe=False, timeout=120)
                    if r.success:
                        local.append("~/workspace/" + dest)
                except Exception as e:  # noqa: BLE001
                    log.warning("dispatch: copy %s: %s", path, e)
            summary = row.get("result") or ""
            if timed_out and c["status"] in ("stopped", "cancelled"):
                summary = f"[stopped: dispatch time limit {int(timeout_s // 60)} min reached] " + summary
            out.append({**c, "summary": summary, "files": [os.path.basename(a["path"]) for a in arts],
                        "local_files": local})
        await say("done")
        return out

    def get(self, sid: str) -> LiveSession:
        if sid not in self.live:
            raise KeyError(sid)
        return self.live[sid]

    async def control(self, sid: str, op: str) -> dict:
        live = self.get(sid)
        c = live.control
        {"pause": c.pause, "takeover": c.takeover, "resume": c.resume, "stop": c.stop}[op]()
        if op == "stop":
            for fut in (live.confirm, live.question):
                if fut and not fut.done():
                    fut.set_result(False if fut is live.confirm else None)
            if live.task.thread_id:
                self.held_threads.add(live.task.thread_id)
        if op == "stop":
            await self.stop_children(live.task.id)
        status = {"pause": "paused", "takeover": "takeover", "resume": "running", "stop": "stopped"}[op]
        self.store.update_task(live.task.id, status)
        etype = {"pause": EventType.PAUSED, "takeover": EventType.TAKEOVER, "resume": EventType.RESUMED,
                 "stop": EventType.TASK_FAILED}[op]
        ev = AgentEvent(session_id=sid, type=etype, data={"by": "user", "op": op, **({"status": "stopped"} if op == "stop" else {})})
        self.store.add_event(ev.model_dump(mode="json"))
        await self._broadcast(live, {"kind": "event", "task_id": live.task.id, **ev.model_dump(mode="json")})
        return {"session_id": sid, "state": c.state.value}

    def answer_confirm(self, sid: str, approve: bool) -> bool:
        live = self.get(sid)
        if live.confirm and not live.confirm.done():
            live.confirm.set_result(approve)
            return True
        return False

    def answer_question(self, sid: str, text: str) -> bool:
        live = self.get(sid)
        if live.question and not live.question.done():
            live.question.set_result(text)
            return True
        return False

    def live_for_task(self, task_id: str) -> Optional[LiveSession]:
        for s in self.live.values():
            if s.task.id == task_id:
                return s
        return None


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s or "pc").strip("-")[:40] or "pc"


def _child_note(parent: dict) -> str:
    return ("CHILD TASK: you are doing ONE piece of a larger request that a parent agent split across several computers "
            "to run at the same time. Do only your piece (the CURRENT REQUEST). Save what you produce under ~/workspace "
            "and send_file each deliverable (the parent collects your files), then done with a factual summary: what "
            "you produced, file names, key facts or numbers. Do not ask the user unless truly blocked.\n"
            f"The user's whole request (for context only): {(parent.get('prompt') or '')[:800]}")


def _place(items: list[dict], roster: list[dict], own: str) -> list[tuple[dict, str, bool]]:
    """Pick a computer for each item. Returns (item, machine id, will queue)."""
    by_id = {m["id"]: m for m in roster}
    by_name = {str(m.get("name") or "").lower(): m["id"] for m in roster}
    avail = {m["id"] for m in roster if m.get("available")} | {own}
    asleep = {m["id"] for m in roster if m.get("asleep")}
    busy_other = {m["id"] for m in roster if m.get("busy_task") and m["id"] != own}
    used: set = set()
    load: dict = {}
    out: list = [None] * len(items)

    def idle() -> Optional[str]:
        c = sorted((mid for mid in avail if mid not in used and mid not in busy_other and mid != own),
                   key=lambda mid: (mid in asleep, mid))
        if c:
            return c[0]
        return own if own not in used else None

    def least() -> str:   # every computer has an item: share out, own computer first on a tie (no foreign queue)
        return min(avail, key=lambda mid: (mid in busy_other, load.get(mid, 0), mid != own, mid))

    def put(i: int, mid: str):
        used.add(mid)
        load[mid] = load.get(mid, 0) + 1
        out[i] = (items[i], mid, mid in busy_other or load[mid] > 1)

    for i, it in enumerate(items):   # named computers first, so "auto" does not take them
        want = str(it.get("machine") or "auto").strip()
        mid = want if want in by_id else by_name.get(want.lower())
        if not mid or want.lower() == "auto" or mid not in avail:
            continue
        if mid in busy_other:
            mid = idle() or mid
        put(i, mid)
    for i, it in enumerate(items):
        if out[i] is None:
            put(i, idle() or least())
    return out


def roster_text(roster: list[dict], own: str) -> str:
    lines = []
    for m in roster:
        st = ("this task's computer" if m["id"] == own else
              "busy with another task (work sent there waits in its queue)" if m.get("busy_task") else
              "asleep, woken automatically when given work" if m.get("asleep") and m.get("available") else
              "online, idle" if m.get("available") else f"{m.get('state') or 'offline'}, not usable now")
        lines.append(f"- {m['id']} 「{m['name']}」 {m['kind']} · {st}")
    usable = sum(1 for m in roster if m.get("available") or m["id"] == own)
    return ("COMPUTERS (this task runs on " + own + "; hand work to them with `dispatch`):\n" + "\n".join(lines) + "\n"
            "When the request has independent pieces that can run at the same time (several topics, sites, files, "
            "clients, tests or machines) or the user asks for parallel work (同时 / 并行 / 分头 / 多台电脑 / 每台电脑), "
            "call `dispatch` once with one item per piece instead of doing them one by one, then combine what comes back "
            "into one answer (merge files if one file was asked for). Keep dependent steps, quick answers and small "
            "single jobs to yourself."
            + ("" if usable > 1 else " Only this computer is usable right now: dispatch would run the pieces one "
               "after another here, so do the work yourself unless the pieces are big and separate."))


def _title(prompt: str) -> str:
    p = " ".join((prompt or "").split())
    return p[:24] + ("…" if len(p) > 24 else "")


_OFFICE_TASK = re.compile(r"officecli|\.?docx|\.?xlsx|\.?pptx|word|excel|空白页|分页|文档|表格|幻灯片|PPT", re.I)


def _budget(prompt: str, max_steps: int, timeout_s: float, context: Optional[str] = None) -> tuple[int, float]:
    """Drawing a construction set in the CAD GUI takes hundreds of steps and hours, not 40 steps / 15 min.
    A revision in a drawing thread gets the same budget (it saves, exports and sends the whole set again)."""
    from agent_runtime.cad import cad_mode
    if cad_mode(prompt, context):
        return (max(max_steps, int(os.environ.get("AIW_CAD_MAX_STEPS", "400"))),
                max(timeout_s, float(os.environ.get("AIW_CAD_TIMEOUT", "10800"))))
    from agent_runtime.loop import _IMAGE_TASK, _VIDEO_TASK
    from agent_runtime import skills as _sk
    if "dev" in _sk.picked(prompt):  # writing and testing a program, with mockup rounds the user confirms
        return (max(max_steps, int(os.environ.get("AIW_DEV_MAX_STEPS", "300"))),
                max(timeout_s, float(os.environ.get("AIW_DEV_TIMEOUT", "5400"))))
    if _VIDEO_TASK.search(prompt or ""):  # task-247: a deck-to-explainer ran out at 900 s before the first frame
        return (max(max_steps, int(os.environ.get("AIW_VIDEO_MAX_STEPS", "200"))),
                max(timeout_s, float(os.environ.get("AIW_VIDEO_TIMEOUT", "3600"))))
    if _OFFICE_TASK.search(prompt or ""):  # task-b26: render, fix, re-render does not fit in 40 steps
        return max(max_steps, int(os.environ.get("AIW_OFFICE_MAX_STEPS", "80"))), max(timeout_s, 1800.0)
    if _IMAGE_TASK.search(prompt or ""):
        # each AI edit ~70 s, each vision check ~30 s: 900 s ran out on task-4e2 and task-484 (a retry kept the
        # old 900 s because the API passes its own LoopConfig, so the loop's own image budget never applied)
        return max_steps, max(timeout_s, float(os.environ.get("AIW_IMAGE_TIMEOUT", "2400")))
    return max_steps, timeout_s
