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
"""
from __future__ import annotations

import os

import asyncio
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

    def model_settings(self) -> dict:
        return self.store.kv_get("models", {}) or {}

    def _llm(self) -> LLMRouter:
        if self._custom_llm:
            return self._custom_llm()
        m = self.model_settings()
        key = self.secrets.get("openai_api_key") if self.secrets else None
        return LLMRouter.from_settings(m.get("model"), m.get("fallback"), key, m.get("base_url") or None, self._tool_choices(), m.get("thinking"))

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
        return LLMRouter.from_settings(m["vision_model"], None, key, m.get("base_url") or None, None, m.get("thinking"))

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

    async def _start_task_row(self, row: dict) -> LiveSession:
        task = Task(id=row["id"], prompt=row["prompt"], machine_id=row["machine_id"], thread_id=row.get("thread_id"),
                    source=row.get("source") or "chat", max_steps=row.get("max_steps") or 40,
                    timeout_s=row.get("timeout_s") or float(os.environ.get("AIW_TASK_TIMEOUT", "900")))
        task.context = self.build_context(task.thread_id, exclude_task=task.id)
        return await self.start(task)

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
            if requeue and time.time() - (t.get("updated_at") or 0) < 1800:   # only recent work; stale runs just get marked
                res = await self.submit(f"（继续之前被中断的任务）{t['prompt']}", t["machine_id"], t.get("thread_id"),
                                        source="resume")
                out.append(res["task"]["id"])
        for m in {t["machine_id"] for t in self.store.tasks_by_status(("queued",))}:
            await self.drain(m)
        return out

    # ------------------------------------------------------------------ run
    async def start(self, task: Task) -> LiveSession:
        if self.busy_session(task.machine_id):
            raise RuntimeError(f"machine {task.machine_id} already has a running session")
        sid = self.store.create_session(task.machine_id, task.id)
        self.store.set_task_session(task.id, sid)
        self.store.update_task(task.id, TaskStatus.RUNNING.value)
        live = LiveSession(sid, task, AgentControl())
        self.live[sid] = live
        plan_state: dict = {"steps": [], "done": []}

        async def emit(ev: AgentEvent):
            d = ev.model_dump(mode="json")
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
            try:
                return await asyncio.wait_for(live.confirm, 300)
            except asyncio.TimeoutError:
                return False
            finally:
                live.confirm = live.confirm_info = None
                self.store.update_task(task.id, TaskStatus.RUNNING.value)

        async def ask(question: str, options: list) -> Optional[str]:
            live.question = asyncio.get_running_loop().create_future()
            live.question_info = {"question": question, "options": options}
            self.store.update_task(task.id, TaskStatus.WAITING_INPUT.value)
            await self._notify("input", "Agent 在问你", question, task)
            try:
                return await asyncio.wait_for(live.question, float(self.settings.ask_timeout_s))
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
            self.store.update_task(task.id, TaskStatus.RUNNING.value)
            try:
                # integrations (MCP servers) are loaded here, off the request path: POST /api/tasks returns at once
                from .integrations import load_tools
                try:
                    extra = await load_tools(self.store, self.secrets) if self.secrets else {}
                except Exception as e:  # noqa: BLE001
                    log.warning("integrations unavailable: %s", e)
                    extra = {}
                loop = AgentLoop(task, sid, computer, self._llm(), ToolExecutor(computer, confirm=confirm, audit=audit),
                                 control=live.control, emit=emit, ask=ask, remember=remember, extra_tools=extra,
                                 subagent_llm=self._subagent_llm(), vision_llm=self._vision_llm(), system_extra=system_extra.strip(),
                                 config=LoopConfig(max_steps=task.max_steps, timeout_s=task.timeout_s,
                                                   artifacts_dir=self.settings.artifacts_dir))
                t = await loop.run()
                status, result = t.status.value, t.result
            except Exception as e:  # noqa: BLE001
                status, result = "failed", f"{type(e).__name__}: {e}"
            self.store.update_task(task.id, status, result)
            self.store.end_session(sid, status)
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


def _title(prompt: str) -> str:
    p = " ".join((prompt or "").split())
    return p[:24] + ("…" if len(p) > 24 else "")
