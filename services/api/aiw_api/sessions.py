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
from agent_runtime import escalate as _es
from agent_runtime import redact as _rd
from aiw_protocols import AgentEvent, EventType, Task, TaskStatus
from computer_runtime import Computer, LinuxComputerProvider
from llm_router import LLMRouter
from tool_router import ToolExecutor

from .store import Store

log = logging.getLogger("aiw.sessions")
TASK_ALLOW_NEVER = ("order_confirm", "xhs_review")   # 本任务内都允许 never approves an order or posts for the user
LIVE_STATES = ("running", "paused", "takeover", "waiting_confirm", "waiting_input")
FINAL_STATES = ("succeeded", "failed", "stopped", "interrupted", "cancelled")
CHAT_EVENTS = (EventType.TASK_STARTED, EventType.DECISION, EventType.PLAN, EventType.PLAN_UPDATE,
               EventType.INPUT_REQUIRED, EventType.INPUT_ANSWERED, EventType.CONFIRM_REQUIRED, EventType.CONFIRM_ANSWERED)


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
    executor: Optional[object] = None   # its ToolExecutor: the policy knows which folders this task made
    allow_task: bool = False            # the user said 本任务内都允许 (never covers payments or posts)
    last_thought: str = ""              # the agent's stated reason for the step being confirmed (为什么)
    pending_notes: list = field(default_factory=list)   # 补充要求 sent before the loop existed (3.8.5)
    allowed_scopes: set = field(default_factory=set)    # v3.9.17: "remote_diag:<host>" the user allowed once
    secret_values: list = field(default_factory=list)   # v3.9.17: this task's vault values, masked in every event


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
        self.queue_front: list[str] = []      # queued task ids the user asked to send first (立即发送), oldest ask first
        self._drain_lock = asyncio.Lock()
        self.cloud = None  # CloudManager, set by the API
        self._health: dict[str, tuple[float, bool]] = {}   # machine -> (checked at, vm-agent answered)
        # async fn(task, session_id, msg) told about a task's milestones (start, step, plan, questions, confirmations,
        # the end): the persistent chat (chat.py) keeps its task cards and escalation messages up to date from these
        self.listeners: list = []
        self.memory = None   # memory.Memory (set by main.build_app): profile + top memories for every task
        self.lessons = None  # lessons.Lessons (set by main.build_app): what earlier tasks learned (3.8.5 self-evolution)
        self.jobs: set = set()   # background jobs (lesson extraction) kept referenced until they finish

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

    def vision_chain(self) -> list[str]:
        """3.8.8: vision_model -> vision_fallback -> geometry_model -> models probed vision-capable (max 3, none
        probed text-only). See llm_router/vision.py."""
        from llm_router.vision import vision_chain
        return vision_chain(self.model_settings(), self.store.kv_get("model_probes", {}) or {})

    def vision_missing(self) -> Optional[str]:
        """A clear sentence when nothing configured can read images (else None)."""
        if self._custom_llm:
            return None
        from llm_router.vision import no_vision_text
        return no_vision_text(self.model_settings(), self.store.kv_get("model_probes", {}) or {})

    def _vision_llm(self) -> Optional[LLMRouter]:
        if self._custom_llm:
            return None
        m = self.model_settings()
        chain = self.vision_chain()
        if not chain:
            return None
        key = self.secrets.get("openai_api_key") if self.secrets else None
        if not (key or os.environ.get("OPENAI_API_KEY")):
            return None
        # the eyes describe pictures; thinking there only adds latency (AIW_VISION_THINKING=on to restore).
        # The router tries the chain in order: a 500 from the first vision model falls through to the next.
        from llm_router.providers.openai import OpenAIProvider
        key = key or os.environ.get("OPENAI_API_KEY")
        th = os.environ.get("AIW_VISION_THINKING", "off")
        return LLMRouter([OpenAIProvider(model=n, api_key=key, base_url=m.get("base_url") or None, thinking=th)
                          for n in chain])

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
        """The AI image models (image_edit / generate_image), v3.9.15 two endpoints:
          网关  = the main gateway (base_url + openai_api_key) with image_model (改图/生图模型),
          自定义 = its own base URL, key (secret image2_api_key) and model (设置 → 模型 → 生图接口).
        image_default (gateway | custom) picks the one tried first; the other is the fallback. Unset = the custom
        endpoint first when it is filled in. Top-level model/key/base_url = the default, for older callers."""
        if self._custom_llm:
            return None
        return image_cfg_from(self.model_settings(), self.secrets)

    # ------------------------------------------------------------------ context
    def build_context(self, thread_id: Optional[str], exclude_task: Optional[str] = None,
                      query: Optional[str] = None, exclude: Optional[set] = None) -> Optional[str]:
        parts = []
        prof = self.store.kv_get("profile", {}) or {}
        if prof.get("tone"):
            parts.append(f"USER PROFILE: tone: {prof['tone']}")
        mem = getattr(self, "memory", None)
        block = ""
        if mem is not None:   # memory.py: profile + the top memories for this request, in [MEMORY] … [/MEMORY]
            try:
                block = mem.prompt_block(query or "")
            except Exception as e:  # noqa: BLE001
                log.warning("memory block: %s", e)
        if block:
            parts.append("USER MEMORY (facts, preferences and decisions the user told you earlier; the user's newest "
                         "words win if they disagree):\n" + block)
        elif mem is None:
            if prof.get("name") or prof.get("about"):
                parts.append("USER PROFILE: " + "; ".join(f"{k}: {prof[k]}" for k in ("name", "about") if prof.get(k)))
            mems = self.store.all("memories", limit=40)
            if mems:
                parts.append("MEMORY (facts the user told you earlier):\n" + "\n".join(f"- {m['text']}" for m in reversed(mems)
                                                                                      if not m.get("superseded")))
        if thread_id:
            turns = [t for t in self.store.thread_tasks(thread_id) if t["id"] != exclude_task
                     and t["id"] not in (exclude or set()) and t["status"] not in ("queued", "cancelled")][-6:]
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
                     source: str = "chat", max_steps: Optional[int] = None, timeout_s: Optional[float] = None, title: Optional[str] = None,
                     isolated: bool = False, context_extra: Optional[str] = None, continues: Optional[str] = None,
                     link_thread: bool = True) -> dict:
        """context_extra: background the task gets on top of its thread and memory (the chat turns before a task
        the persistent chat started); kept in kv so a queued task still has it when it starts.
        continues: the root task this run continues (3.8.5). A message in a task's thread (source chat, the task
        view's composer, iOS ChatView) continues the thread's latest task unless link_thread=False."""
        machine_id = machine_id or (self.settings.machines[0].id if self.settings.machines else "ai-linux-001")
        if not self.store.machine(machine_id):
            raise KeyError(machine_id)
        if not thread_id or not self.store.thread(thread_id):
            thread_id = self.store.create_thread(title or _title(prompt))["id"]
        elif continues is None and link_thread and source == "chat":
            prev = [t for t in self.store.thread_tasks(thread_id)
                    if not t.get("parent_id") and t.get("status") != "cancelled"]
            if prev:
                continues = prev[-1].get("continues") or prev[-1]["id"]
        max_steps = _es.step_cap() if max_steps is None else max_steps
        timeout_s = _es.time_cap() if timeout_s is None else timeout_s
        max_steps, timeout_s = _budget(prompt, max_steps, timeout_s)
        task = Task(prompt=prompt, machine_id=machine_id, max_steps=max_steps, timeout_s=timeout_s,
                    thread_id=thread_id, source=source)
        task.prompt = self.protect(task.id, task.prompt)   # v3.9.17: passwords to the vault, *** in the prompt
        row = task.model_dump()
        row["status"] = "queued"
        if continues and self.store.task(continues):
            row["continues"] = (self.store.task(continues) or {}).get("continues") or continues
        self.store.create_task(row)
        if row.get("continues"):
            mode = source if source in ("retry", "edit", "resume") else "continue"
            self.store.kv_set(f"task_run:{task.id}", {"mode": mode})
        try:   # uploads named in the prompt (the 附件 block) belong to this task and its thread from now on
            from . import uploads as _up
            _up.link_prompt(self.store, prompt, task.id, thread_id)
        except Exception as e:  # noqa: BLE001
            log.warning("link uploads: %s", e)
        if isolated:
            self.store.kv_set(f"isolated:{task.id}", True)
        if context_extra:
            self.store.kv_set(f"task_ctx:{task.id}", str(context_extra)[:6000])
        # 3.8: a NEW message in a thread whose queue was paused by 停止 resumes that queue. Before, the hold also caught
        # every later message: it was queued with the computer idle and nothing (on iOS) could ever release it.
        if thread_id in self.held_threads and source not in ("resume", "dispatch"):
            self.held_threads.discard(thread_id)
        older = any(r["id"] != task.id and r["machine_id"] == machine_id and r.get("thread_id") not in self.held_threads
                    for r in self.store.tasks_by_status(("queued",)))
        queued = bool(self.busy_session(machine_id)) or thread_id in self.held_threads or older
        if older and not self.busy_session(machine_id):   # earlier messages go first, then this one (drain keeps the order)
            await self.drain(machine_id)
            t = self.store.task(task.id)
            return {"task": t, "session_id": t.get("session_id"), "queued": t.get("status") == "queued", "thread_id": thread_id}
        if not queued:
            await self._start_task_row(self.store.task(task.id))
        else:
            await self._event_all({"kind": "task", "task": self.store.task(task.id)})
        t = self.store.task(task.id)
        return {"task": t, "session_id": t.get("session_id"), "queued": queued, "thread_id": thread_id}

    async def _start_task_row(self, row: dict, allow_busy: bool = False) -> LiveSession:
        if getattr(self, "memory", None) is not None:   # embeds the request when an embedding model is set
            try:
                await self.memory.prepare(row.get("prompt") or "")
            except Exception as e:  # noqa: BLE001
                log.warning("memory prepare: %s", e)
        from . import continuation as _cont
        run_prompt, resume, chain_ids = row["prompt"], "", set()
        if row.get("continues"):   # 3.8.5: a 继续 / 修改 run of an earlier task picks up where it stopped
            try:
                chain_ids = {r["id"] for r in self.store.continuations(row["continues"])}
                run_prompt = _cont.run_prompt(self.store, row) or row["prompt"]
                mname = (self.store.machine(row["machine_id"]) or {}).get("name")
                resume = _cont.resume_block(self.store, row, mname)
                plan = _cont.last_plan([r for r in self.store.continuations(row["continues"]) if r["id"] != row["id"]])
                if (plan and (self.store.kv_get(f"task_run:{row['id']}", {}) or {}).get("mode") != "edit"
                        and "用户补充：" not in (run_prompt or "")):   # aiw-fix-1: new words get a new plan
                    self.store.kv_set(f"task_seed_plan:{row['id']}", plan)
            except Exception as e:  # noqa: BLE001  a broken history never blocks the run
                log.warning("resume context: %s", e)
        ctx = self.build_context(row.get("thread_id"), exclude_task=row["id"], query=run_prompt, exclude=chain_ids)
        extra_ctx = self.store.kv_get(f"task_ctx:{row['id']}")
        if extra_ctx:
            ctx = (ctx + "\n\n" if ctx else "") + extra_ctx
        if resume:
            ctx = resume + ("\n\n" + ctx if ctx else "")
        if self.lessons is not None:   # 3.8.5: what earlier tasks learned on this computer / site / skill
            try:
                from agent_runtime import skills as _skills
                sk = _skills.picked(run_prompt) + _skills.mentioned(run_prompt) + _skills.auto(run_prompt)
                block, ids = self.lessons.for_task(row["machine_id"], run_prompt, sk)
                if block:
                    ctx = (ctx + "\n\n" if ctx else "") + block
                    self.store.kv_set(f"task_lessons:{row['id']}", ids)
            except Exception as e:  # noqa: BLE001
                log.warning("lessons block: %s", e)
        if row.get("parent_id"):
            ctx = _child_note(self.store.task(row["parent_id"]) or {}) + ("\n\n" + ctx if ctx else "")
        steps, secs = row.get("max_steps"), row.get("timeout_s")
        if row.get("source") not in ("xhs", "watch_order"):
            # 3.8.2: tasks have no step / time budget (rows saved before carry the old 40 steps / 900 s)
            steps, secs = _es.step_cap(), _es.time_cap()
        steps, secs = _budget(run_prompt, int(steps or 0), float(secs or 0), ctx)
        task = Task(id=row["id"], prompt=run_prompt, machine_id=row["machine_id"], thread_id=row.get("thread_id"),
                    source=row.get("source") or "chat", max_steps=steps, timeout_s=secs,
                    continues=(row.get("continues") if row.get("continues") and row.get("continues") != row["id"] else None))
        task.context = ctx
        return await self.start(task, allow_busy=allow_busy)

    async def drain(self, machine_id: str):
        async with self._drain_lock:
            if self.busy_session(machine_id):
                return
            rows = [r for r in self.store.tasks_by_status(("queued",)) if r["machine_id"] == machine_id]
            front = {tid: i for i, tid in enumerate(self.queue_front)}
            rows.sort(key=lambda r: (0, front[r["id"]]) if r["id"] in front else (1, 0))   # stable: FIFO after 立即发送 ones
            for row in rows:
                if row["id"] in front:   # the user asked for this one now: its thread's pause no longer applies
                    self.held_threads.discard(row.get("thread_id"))
                elif row.get("thread_id") in self.held_threads:
                    continue
                if row["id"] in self.queue_front:
                    self.queue_front.remove(row["id"])
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
        if task_id in self.queue_front:
            self.queue_front.remove(task_id)
        # 3.8.2: tell the chat card and every open client (before, nothing was told: the home card kept saying
        # 排队中 and the iOS task view kept the message, so 删除 looked like it did nothing)
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            tk = Task(id=t["id"], prompt=t.get("prompt") or "", machine_id=t.get("machine_id") or "ai-linux-001",
                      thread_id=t.get("thread_id"))
            loop.create_task(self._tell(tk, t.get("session_id") or "", {"type": "task.cancelled", "status": "cancelled"}))
            loop.create_task(self._event_all({"kind": "task", "task": self.store.task(task_id)}))
        return True

    def queue_info(self, row: dict) -> dict:
        """Why a queued task has not started: the paused queue of its thread, or the task its computer is busy with."""
        busy = self.busy_session(row["machine_id"])
        out: dict = {"held": row.get("thread_id") in self.held_threads, "first": row["id"] in self.queue_front}
        if busy:
            bt = self.store.task(busy.task.id) or {}
            out["busy"] = {"task_id": busy.task.id, "thread_id": busy.task.thread_id, "status": bt.get("status"),
                           "prompt": (bt.get("prompt") or busy.task.prompt or "")[:120]}
        ahead = 0
        for r in self.store.tasks_by_status(("queued",)):
            if r["id"] == row["id"]:
                break
            if r["machine_id"] == row["machine_id"] and r.get("thread_id") not in self.held_threads:
                ahead += 1
        out["ahead"] = 0 if out["first"] else ahead
        return out

    async def send_now(self, task_id: str, stop_current: bool = False) -> dict:
        """立即发送 for a queued message. The computer runs one task at a time, so: idle -> it starts now; busy -> it
        goes first in the queue, and with stop_current the running task is stopped so it starts right after."""
        t = self.store.task(task_id)
        if not t:
            raise KeyError(task_id)
        if t["status"] != "queued":
            raise ValueError(f"task is {t['status']}, not queued")
        if task_id in self.queue_front:
            self.queue_front.remove(task_id)
        self.queue_front.insert(0, task_id)
        if t.get("thread_id"):
            self.held_threads.discard(t["thread_id"])
        busy = self.busy_session(t["machine_id"])
        stopped = None
        if busy and stop_current:
            stopped = busy.task.id
            await self.control(busy.id, "stop")   # its runner ends and drains: this one is first
        elif not busy:
            await self.drain(t["machine_id"])
        now = self.store.task(task_id) or t
        return {"task": now, "started": now.get("status") != "queued", "stopped": stopped,
                "queue": self.queue_info(now) if now.get("status") == "queued" else None}

    async def retry(self, task_id: str) -> dict:
        """继续 / 重试 on a task card (3.8.5): a continuation of the SAME task, not the bare prompt again."""
        return await self.continue_task(task_id, "", mode="retry")

    async def continue_task(self, task_id: str, text: str = "", attachments: Optional[list] = None,
                            machine_id: Optional[str] = None, mode: str = "continue") -> dict:
        """Resume a task: its prompt, plan, steps done, files and attachments go with it (continuation.py).
        A run that is still going takes the words as an added instruction instead (instruct)."""
        from . import continuation as _cont
        t = self.store.task(task_id)
        if not t:
            raise KeyError(task_id)
        root = self.store.task(t.get("continues") or task_id) or t
        last = _cont.latest(self.store, root["id"]) or t
        live = self.live_for_task(last["id"])
        text = (text or "").strip()
        if attachments:
            text = (text + "\n\n" if text else "") + "附件：\n" + "\n".join(
                "- ~/workspace/uploads/" + str(a.get("name") or os.path.basename(str(a.get("artifact") or "")).split("-", 1)[-1])
                for a in attachments if isinstance(a, dict))
        if live and live.runner and not live.runner.done():
            if mode == "edit":
                await self.control(live.id, "stop")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(live.runner), 15)
            else:
                res = await self.instruct(last["id"], text or "继续")
                self._link_attachments(attachments, last["id"], last.get("thread_id"))
                return {**res, "task": self.store.task(last["id"]), "thread_id": last.get("thread_id"),
                        "session_id": last.get("session_id"), "queued": False, "continued": root["id"]}
        if last.get("status") == "queued" and mode != "edit":
            # not started yet: the words join it (one queued run per task, never two)
            if text:
                self.store._x("UPDATE tasks SET prompt=? WHERE id=?", ((last.get("prompt") or "") + "\n" + text, last["id"]))
            self._link_attachments(attachments, last["id"], last.get("thread_id"))
            return {"task": self.store.task(last["id"]), "thread_id": last.get("thread_id"), "queued": True,
                    "session_id": None, "continued": root["id"]}
        mid = machine_id or last.get("machine_id") or root.get("machine_id")
        if not self.store.machine(mid):
            mid = None
        if mode == "edit" and text:
            self.store.kv_set(f"task_instructions:{root['id']}", text)
        shown = text or {"retry": "继续", "resume": "（继续之前被中断的任务）"}.get(mode, "继续")
        res = await self.submit(shown, mid, root.get("thread_id"), source=mode if mode in ("retry", "edit", "resume") else "continue",
                                continues=root["id"])
        self._link_attachments(attachments, res["task"]["id"], res.get("thread_id"))
        return {**res, "continued": root["id"]}

    def _link_attachments(self, attachments: Optional[list], task_id: str, thread_id: Optional[str]) -> None:
        if not attachments:
            return
        from . import uploads as _up
        for a in attachments:
            if isinstance(a, dict) and a.get("artifact"):
                with contextlib.suppress(Exception):
                    _up.link(self.store, a["artifact"], task_id, thread_id, a.get("name"))

    async def instruct(self, task_id: str, text: str) -> dict:
        """Words for a task that is running (3.8.5 补充要求): the answer to its open question, else an instruction
        the agent reads at its next step. A task that is not running is continued instead."""
        live = self.live_for_task(task_id)
        text = (text or "").strip()
        if not text:
            raise ValueError("empty instruction")
        if not (live and live.runner and not live.runner.done()):
            return await self.continue_task(task_id, text)
        text = self.protect(task_id, text)
        if live:
            live.secret_values = self.task_secret_values(task_id)
            lp = getattr(live, "loop", None)
            if lp is not None and hasattr(lp, "_secrets"):
                for c in self.task_creds(task_id):
                    v = self.secrets.get(c["key"]) if self.secrets else None
                    if v:
                        lp._secrets.setdefault(c["key"], v)
        if live.question and not live.question.done():
            live.question.set_result(text)
            return {"answered": True, "task_id": task_id}
        loop = getattr(live, "loop", None)
        if loop is None or not hasattr(loop, "inject"):
            live.pending_notes = getattr(live, "pending_notes", []) + [text]
        else:
            loop.inject(text)
        ev = AgentEvent(session_id=live.id, type=EventType.MESSAGE, data={"text": f"收到补充要求：{text[:300]}", "instruction": text[:2000]})
        self.store.add_event(ev.model_dump(mode="json"))
        await self._broadcast(live, {"kind": "event", "task_id": task_id, "thread_id": live.task.thread_id, **ev.model_dump(mode="json")})
        return {"instructed": True, "task_id": task_id}

    # ------------------------------------------------------------------ v3.9.17 密码保护
    def protect(self, task_id: str, text: str) -> str:
        """Passwords the user wrote go to the vault (secret task_cred_<task>_<n>), the text keeps ***."""
        clean, creds = _rd.extract(text or "")
        if not creds or not self.secrets:
            return clean if creds else (text or "")
        meta = list(self.store.kv_get(f"task_creds:{task_id}") or [])
        for c in creds:
            key = f"task_cred_{task_id}_{len(meta) + 1}"
            try:
                self.secrets.put(key, c["value"])
            except Exception as e:  # noqa: BLE001
                log.warning("vault: %s", e)
                continue
            meta.append({"key": key, "host": c.get("host") or "", "user": c.get("user") or "", "kind": c.get("kind")})
        self.store.kv_set(f"task_creds:{task_id}", meta)
        return clean

    def redact_stored(self, force: bool = False) -> int:
        """v3.9.17: passwords already sitting in the database (task prompts, their events and actions, chat messages)
        move to the vault and become ***. Runs once (kv redact_v3917); returns how many tasks it cleaned."""
        if not force and self.store.kv_get("redact_v3917"):
            return 0
        n = 0
        for t in self.store._x("SELECT id, prompt FROM tasks"):
            clean, creds = _rd.extract(t.get("prompt") or "")
            if not creds:
                continue
            self.protect(t["id"], t.get("prompt") or "")
            self.store._x("UPDATE tasks SET prompt=? WHERE id=?", (clean, t["id"]))
            sids = [r["id"] for r in self.store._x("SELECT id FROM sessions WHERE task_id=?", (t["id"],))]
            for c in creds:
                v = c["value"]
                for sid in sids:
                    self.store._x("UPDATE agent_events SET data=REPLACE(data, ?, '***') WHERE session_id=? AND instr(data, ?) > 0",
                                  (v, sid, v))
                    self.store._x("UPDATE machine_actions SET payload=REPLACE(payload, ?, '***') WHERE session_id=? AND instr(payload, ?) > 0",
                                  (v, sid, v))
                self.store._x("UPDATE tasks SET result=REPLACE(result, ?, '***') WHERE instr(result, ?) > 0", (v, v))
                with contextlib.suppress(Exception):
                    self.store._x("UPDATE chat_messages SET text=REPLACE(text, ?, '***') WHERE instr(text, ?) > 0", (v, v))
            n += 1
        self.store.kv_set("redact_v3917", {"at": time.time(), "tasks": n})
        return n

    def task_creds(self, task_id: str) -> list[dict]:
        """This task's credentials (and its root's, for a continuation): [{key, host, user}] without values."""
        row = self.store.task(task_id) or {}
        ids = [task_id] + ([row["continues"]] if row.get("continues") else [])
        out, seen = [], set()
        for tid in ids:
            for c in self.store.kv_get(f"task_creds:{tid}") or []:
                if c.get("key") not in seen:
                    seen.add(c.get("key"))
                    out.append(dict(c))
        return out

    def task_secret_values(self, task_id: str) -> list[str]:
        vals = []
        for c in self.task_creds(task_id):
            with contextlib.suppress(Exception):
                v = self.secrets.get(c["key"]) if self.secrets else None
                if v:
                    vals.append(v)
        with contextlib.suppress(Exception):   # the hosts the user authorised (运维主机) too
            from . import remote_hosts as _rh
            for h in _rh.hosts(self.store):
                v = self.secrets.get(f"remote_host_{h['id']}") if self.secrets else None
                if v:
                    vals.append(v)
        return vals

    async def edit_task(self, task_id: str, prompt: str) -> dict:
        """修改任务 (3.8.5): new instructions for the same task; a running run stops and the task re-runs with them,
        keeping its history, files and attachments."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("empty instructions")
        return await self.continue_task(task_id, prompt, mode="edit")

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
                res = await self.submit("（继续之前被中断的任务）", t["machine_id"], t.get("thread_id"),
                                        source="resume", continues=t.get("continues") or t["id"])
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
        with contextlib.suppress(Exception):   # v3.9.17: this task's vault values, masked in every event
            live.secret_values = self.task_secret_values(task.id)
        ssh_pool = None
        plan_state: dict = {"steps": [], "done": []}

        async def emit(ev: AgentEvent):
            d = ev.model_dump(mode="json")
            if isinstance(d.get("data"), dict):   # v3.9.17: no password / *_KEY value in any stored or sent event
                d["data"] = _rd.mask_obj(d["data"], live.secret_values)
            if ev.type == EventType.DECISION:
                live.last_thought = str(ev.data.get("thought") or "")[:600]
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
            if ev.type == EventType.PLAN_UPDATE and plan_state.get("steps"):   # ticks without a plan keep nothing
                plan_state["done"] = ev.data.get("done") or []
                self.store.set_task_plan(task.id, plan_state)
            if ev.type == EventType.FINDING and ev.data.get("board") is not None:   # v3.9.17: kept for continuations
                with contextlib.suppress(Exception):
                    root = (self.store.task(task.id) or {}).get("continues") or task.id
                    self.store.kv_set(f"task_findings:{root}", d["data"]["board"])
            if ev.type == EventType.MEMORY and ev.data.get("text"):
                pass  # saved by remember()
            if ev.type == EventType.ERROR and ev.data.get("vision_down"):   # 3.8.8: once per task, names the service
                with contextlib.suppress(Exception):
                    await self._notify("input", "读图服务不可用", str(ev.data.get("error") or "")[:300], task)
            msg = {"kind": "event", "task_id": task.id, "thread_id": task.thread_id, **d}
            await self._broadcast(live, msg)
            if ev.type in CHAT_EVENTS:
                await self._tell(task, sid, msg)
            if ev.type in (EventType.TASK_STARTED, EventType.TASK_FINISHED, EventType.TASK_FAILED, EventType.PLAN,
                           EventType.PLAN_UPDATE, EventType.CONFIRM_REQUIRED, EventType.INPUT_REQUIRED):
                await self._event_all(msg)

        async def audit(rec: dict):
            self.store.add_action(sid, _rd.mask_obj(rec, live.secret_values))

        async def confirm(tool, payload, decision) -> bool:
            payload = _rd.mask_obj(payload, live.secret_values) if isinstance(payload, dict) else payload
            scope = getattr(decision, "scope", "") or ""
            summary = await self._confirm_summary(tool, payload, decision, live)
            if scope.startswith("remote_diag:"):
                summary["scope_note"] = f"本任务内允许对该主机（{scope.split(':', 1)[1]}）执行只读诊断：同意一次，后面同类操作不再询问"
            brief = {"title": summary["title"], "risk": summary["risk"], "what": summary["what"]}
            if scope and scope in live.allowed_scopes:
                # v3.9.17: same host, same kind of read-only diagnosis, already allowed in this task
                await emit(AgentEvent(session_id=sid, type=EventType.CONFIRM_ANSWERED,
                                      data={"tool": tool, "approve": True, "by": "scope", "scope": scope, **brief}))
                return True
            if live.allow_task and tool not in TASK_ALLOW_NEVER and summary.get("category") not in ("pay", "message"):
                # 本任务内都允许: approved without a card, still on the timeline
                await emit(AgentEvent(session_id=sid, type=EventType.CONFIRM_ANSWERED,
                                      data={"tool": tool, "approve": True, "by": "task", **brief, "summary": summary}))
                return True
            live.confirm = asyncio.get_running_loop().create_future()
            live.confirm_info = {"tool": tool, "payload": payload, "level": int(decision.level), "reason": decision.reason,
                                 "summary": summary, "id": f"c{int(time.time() * 1000)}",
                                 "task_allow": tool not in TASK_ALLOW_NEVER and summary.get("category") not in ("pay", "message")}
            self.store.update_task(task.id, TaskStatus.WAITING_CONFIRM.value)
            await emit(AgentEvent(session_id=sid, type=EventType.CONFIRM_REQUIRED, data=live.confirm_info))
            await self._notify("confirm", "需要你确认", f"{summary['title'].replace('确认：', '', 1)} · {summary['risk_cn']}", task)
            t0 = time.monotonic()
            by, ok = "user", False
            try:
                # a price-watch order waits up to 30 min for the user (payload confirm_timeout_s); everything else 5 min
                wait = float(payload.get("confirm_timeout_s") or 300) if isinstance(payload, dict) else 300.0
                ok = bool(await asyncio.wait_for(live.confirm, wait))
                if ok and live.allow_task:
                    by = "user_task"     # this card's 本任务内都允许
                if ok and scope:
                    live.allowed_scopes.add(scope)
                return ok
            except asyncio.TimeoutError:
                by = "timeout"
                return False
            finally:
                if getattr(live, "loop", None) is not None:
                    live.loop.add_wait(time.monotonic() - t0)
                if live.control.state.value == "stopped":
                    by = "stop"
                live.confirm = live.confirm_info = None
                self.store.update_task(task.id, TaskStatus.RUNNING.value)
                with contextlib.suppress(Exception):
                    await emit(AgentEvent(session_id=sid, type=EventType.CONFIRM_ANSWERED,
                                          data={"tool": tool, "approve": ok, "by": by, **brief, "summary": summary}))

        async def ask(question: str, options: list, extra: Optional[dict] = None) -> Optional[str]:
            live.question = asyncio.get_running_loop().create_future()
            live.question_info = {"question": question, "options": options}
            if extra:  # a form card to fill, or a takeover request
                live.question_info["form"] = extra
            kind = (extra or {}).get("kind")
            if kind == "takeover":   # which computer: the card says where to go
                with contextlib.suppress(Exception):
                    m = self.store.machine(task.machine_id) or {}
                    if not extra.get("machine"):
                        extra["machine"] = m.get("name") or task.machine_id
                    extra["machine_id"] = task.machine_id
            self.store.update_task(task.id, TaskStatus.WAITING_INPUT.value)
            lp = getattr(live, "loop", None)
            forever = bool(getattr(lp, "_ask_forever", False))   # a stuck task: paused until the user decides
            stuck = getattr(lp, "_stuck", None) if forever else None
            if stuck and kind != "takeover":
                await self._notify("input", "任务卡住了，等你处理", f"{stuck.get('reason')}。可以：继续 / 接管 / 停止", task)
            else:
                await self._notify("input", "需要你填写资料" if kind == "form" else
                               (extra.get("title") or "需要你接管桌面") if kind == "takeover" else "Agent 在问你",
                               ((extra.get("steps") or question) + (f"（{extra['machine']}）" if extra.get("machine") else ""))
                               if kind == "takeover" else question, task,
                                   deep=f"takeover/{task.machine_id}?task={task.id}" if kind == "takeover" else None)
            try:
                if forever:   # never times out: only an answer or 停止 (which cancels the loop) ends this wait
                    return await live.question
                # filling a form or passing a captcha takes longer than answering a question
                wait = float(self.settings.ask_timeout_s) * (3 if extra else 1)
                if os.environ.get("AIW_ASK_WAIT_FOREVER", "1") != "0":
                    # 3.8.5: an unanswered question keeps the task paused (task-7bd: after 600 s the agent went on
                    # with "我按最实用的默认方案"). One reminder push after the old timeout, then it keeps waiting.
                    try:
                        return await asyncio.wait_for(asyncio.shield(live.question), wait)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(Exception):
                            await self._notify("input", "还在等你回答", (question or "")[:200] + "（任务已暂停，回答后接着做）", task)
                        return await live.question
                return await asyncio.wait_for(live.question, wait)
            except asyncio.TimeoutError:
                return None
            finally:
                live.question = live.question_info = None
                self.store.update_task(task.id, TaskStatus.RUNNING.value)

        async def remember(text: str):
            if getattr(self, "memory", None) is not None:   # dedupes, never keeps a password / code / token
                self.memory.add(text, None, task.id)
            else:
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
            nonlocal computer, ssh_pool
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
                try:  # v3.9.14: earlier chats, tasks and files (search.py), full text, the real words
                    srch = getattr(self, "search", None)
                    if srch is not None:
                        extra["session_search"] = srch.tool(exclude_task=task.id)
                except Exception as e:  # noqa: BLE001
                    log.warning("session_search unavailable: %s", e)
                try:  # hosts the user authorised for ops work (设置 › 电脑 › 运维主机)
                    from . import remote_hosts
                    if self.secrets and remote_hosts.hosts(self.store):
                        extra["remote_exec"] = remote_hosts.tool(self.store, self.secrets)
                except Exception as e:  # noqa: BLE001
                    log.warning("remote_exec unavailable: %s", e)
                creds = []
                try:  # v3.9.17 ssh_exec: one reused paramiko connection, password from the vault
                    from . import remote_hosts, ssh_exec as _sx
                    creds = self.task_creds(task.id)
                    hcreds = [{"key": f"remote_host_{h['id']}", "host": h.get("host") or "", "user": h.get("user") or "root"}
                              for h in remote_hosts.hosts(self.store)]
                    if self.secrets and (creds or hcreds) and os.environ.get("AIW_SSH_EXEC", "1") != "0":
                        ssh_pool = _sx.SSHPool()
                        extra["ssh_exec"] = _sx.tool(self.store, self.secrets, ssh_pool, creds + hcreds)
                except Exception as e:  # noqa: BLE001
                    log.warning("ssh_exec unavailable: %s", e)
                hold = contextlib.nullcontext()
                cloud = getattr(self, "cloud", None)
                if cloud and cloud.is_cloud(task.machine_id) and not cleanup:
                    # an InstaBox machine may be paused (30 idle minutes): wake it, and keep it awake till the end
                    await cloud.wake(task.machine_id)
                    hold = cloud.hold(task.machine_id)
                    computer = self.computer(task.machine_id)   # a restarted agent means a fresh connection
                sx = system_extra
                if not cleanup:   # skills an earlier task installed on this computer (cached 5 min per computer)
                    try:
                        sx += await self._installed_skills(task, computer)
                    except Exception as e:  # noqa: BLE001
                        log.warning("installed skills: %s", e)
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
                    home = None
                    try:   # 3.8.5: the task's attachments + earlier runs' files onto THIS computer, real paths in the prompt
                        block, home = await self._sync_task_files(task, computer)
                        if block:
                            task.context = (task.context + "\n\n" if task.context else "") + block
                    except Exception as e:  # noqa: BLE001
                        log.warning("sync task files: %s", e)
                    if home:
                        sx += (f"\nTHIS COMPUTER: home folder {home}, workspace {home}/workspace (uploads in "
                               f"{home}/workspace/uploads). Write paths as ~/workspace/... or $HOME/workspace/... "
                               "(Python: os.path.expanduser). Never write /home/user/... or any other guessed home.")
                    try:   # 3.9.0: the skills this task uses are checked, and installed when missing, before it starts
                        sx += await self._skill_preflight(task, computer, emit, sid)
                    except Exception as e:  # noqa: BLE001
                        log.warning("skill preflight: %s", e)
                    llm, routed = self._task_llm(task.prompt)
                    if routed:
                        await emit(AgentEvent(session_id=sid, type=EventType.MESSAGE, data={"text": f"视频任务，改用视频模型 {routed}"}))
                    live.executor = ToolExecutor(computer, confirm=confirm, audit=audit)
                    loop = AgentLoop(task, sid, computer, llm, live.executor,
                                     control=live.control, emit=emit, ask=ask, remember=remember, extra_tools=extra,
                                     subagent_llm=self._subagent_llm(), vision_llm=self._vision_llm(), image_cfg=self._image_cfg(),
                                     system_extra=sx.strip(),
                                     config=LoopConfig(max_steps=task.max_steps, timeout_s=task.timeout_s,
                                                       artifacts_dir=self.settings.artifacts_dir))
                    loop.geometry_llm = self._geometry_llm()
                    try:   # v3.9.17 密码保护: {{task_cred_…}} fills in type / bos_act / shell; outputs mask the values
                        for c in creds:
                            v = self.secrets.get(c["key"]) if self.secrets else None
                            if v:
                                loop._secrets[c["key"]] = v
                        if creds:
                            note = _rd.note_for(creds, [c["key"] for c in creds])
                            task.context = (task.context + "\n\n" if task.context else "") + note
                        root = (self.store.task(task.id) or {}).get("continues")
                        board = self.store.kv_get(f"task_findings:{root}") if root else None
                        if board:   # a continuation keeps the facts board
                            from agent_runtime.findings import FindingsBoard
                            loop.findings.items = FindingsBoard(board).items
                            with contextlib.suppress(Exception):
                                loop.guard.inherit()   # v3.9.20
                    except Exception as e:  # noqa: BLE001
                        log.warning("task secrets/findings: %s", e)
                    loop.vision_missing = self.vision_missing()
                    loop.home = home
                    seed = self.store.kv_get(f"task_seed_plan:{task.id}")
                    if seed and seed.get("steps"):   # a continuation keeps the plan and its ticks
                        loop.plan_steps = list(seed["steps"])
                        loop.plan_done = set(int(i) for i in seed.get("done") or [])
                        loop.cfg.make_plan = False
                    for n in getattr(live, "pending_notes", []) or []:
                        loop.inject(n)
                    if self.lessons is not None:
                        loop.lesson_hint = self._lesson_hook(task)
                    bos_on = False
                    try:   # BrowserOS neo on this computer (设置 › 电脑): element-level bos_* tools, one tab group per task
                        from . import browseros as _bos
                        bos_on = not cleanup and await _bos.for_task(self.store, computer, task.machine_id)
                        if bos_on:
                            loop.bos_enabled = True
                            loop.system += "\n" + _bos.PROMPT
                            loop.tools.bos_key = task.id
                            loop.tools.bos_title = (task.prompt or "")[:200]
                            src = getattr(task, "source", None) or ""
                            loop.tools.bos_category = {"watch_order": "shopping", "xhs": "social-media"}.get(src, "other")
                    except Exception as e:  # noqa: BLE001
                        log.warning("browseros unavailable: %s", e)
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
                    try:
                        t = await loop.run()
                    finally:
                        if bos_on:   # drop the task's MCP connection (its tabs stay for the replay)
                            try:
                                await computer.execute("browseros.call", {"tool": "close", "key": task.id}, timeout=10)
                            except Exception:  # noqa: BLE001
                                pass
                status, result = t.status.value, t.result
            except Exception as e:  # noqa: BLE001
                status, result = "failed", f"{type(e).__name__}: {e}"
            finally:
                if ssh_pool is not None:
                    with contextlib.suppress(Exception):
                        ssh_pool.close()
            result = _rd.mask(result, live.secret_values) if isinstance(result, str) else result
            self.store.update_task(task.id, status, result)
            self.store.end_session(sid, status)
            await self._tell(task, sid, {"type": "task.end", "status": status, "result": result,
                                         "files": [a["path"] for a in self.store.artifacts(sid) if a["kind"] == "file"]})
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
            if status == "succeeded" and self.lessons is not None and not parent_id:
                self._background(self._learn(task.id, getattr(getattr(live, "loop", None), "home", None)))
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

    # ------------------------------------------------------------------ 3.8.5: files, lessons
    async def _sync_task_files(self, task: Task, computer) -> tuple[str, Optional[str]]:
        """Put the task's attachments (every upload of its runs and its thread) and the files its earlier runs sent
        into ~/workspace on the computer this run uses; returns the prompt block and the computer's $HOME."""
        from . import continuation as _cont
        from . import uploads as _up
        row = self.store.task(task.id) or {}
        runs = self.store.continuations(row["continues"]) if row.get("continues") else [row]
        ups = _up.for_task(self.store, [r["id"] for r in runs], task.thread_id, [r.get("prompt") or "" for r in runs])
        made = _cont.files_made(self.store, [r for r in runs if r["id"] != task.id]) if row.get("continues") else []
        made = [f for f in made if f.get("local") and os.path.isfile(f["local"])]
        items = [{"local": u["path"], "rel": f"uploads/{u['name']}", "name": u["name"]} for u in ups]
        items += [{"local": f["local"], "rel": f["rel"], "name": f["name"]} for f in made
                  if f"uploads/{f['name']}" not in {i["rel"] for i in items}]
        if not items:
            info = await _up.sync_files(computer, [])
            return "", info.get("home")
        info = await _up.sync_files(computer, items)
        n_up = len(ups)
        a = _up.prompt_block({"files": info["files"][:n_up]}, "ATTACHMENTS")
        b = _up.prompt_block({"files": info["files"][n_up:]}, "FILES")
        return "\n\n".join(x for x in (a, b) if x), info.get("home")

    def _lesson_hook(self, task: Task):
        shown: set = set(self.store.kv_get(f"task_lessons:{task.id}") or [])

        async def hint(error: str, tool: str = "", domain: str = "") -> Optional[str]:
            from agent_runtime import skills as _skills
            sk = _skills.picked(task.prompt) + _skills.mentioned(task.prompt)
            text, ids = self.lessons.hint(error, tool, task.machine_id, [domain] if domain else [], sk, exclude=shown)
            shown.update(ids)
            return text or None
        return hint

    def _background(self, coro):
        job = asyncio.ensure_future(coro)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return job

    async def _learn(self, task_id: str, home: Optional[str] = None) -> list:
        """After a task ended well: extract lessons from it (and from the runs it continued)."""
        try:
            from agent_runtime import skills as _skills
            row = self.store.task(task_id) or {}
            runs = self.store.continuations(row["continues"]) if row.get("continues") else [row]
            events, user_texts = [], []
            for r in runs:
                if r.get("session_id"):
                    events += self.store.events(r["session_id"], limit=2000)
                if r is not runs[0] and (r.get("prompt") or "").strip():
                    user_texts.append(r["prompt"])
            for e in events:
                d = e.get("data") or {}
                if e.get("type") == "agent.input_answered" and isinstance(d.get("answer"), str):
                    user_texts.append(d["answer"])
                if e.get("type") == "agent.message" and d.get("instruction"):
                    user_texts.append(d["instruction"])
            prompt = runs[0].get("prompt") or ""
            sk = _skills.picked(prompt) + _skills.mentioned(prompt)
            task = {**row, "prompt": prompt, "machine_id": row.get("machine_id")}
            return await self.lessons.learn_from_task(task, events, user_texts, home, sk)
        except Exception as e:  # noqa: BLE001
            log.warning("learn lessons: %s", e)
            return []

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

    async def _notify(self, kind: str, title: str, body: str, task: Task, deep: Optional[str] = None):
        if self.notifier:
            level = None
            if getattr(task, "source", "") == "routine" and kind in ("done", "failed"):
                # 3.9.17: a scheduled routine (e.g. the 9:00 news digest) says what it is and is timeSensitive, so iOS
                # 定时推送摘要 / 专注模式 delivers it at once instead of holding it
                name = ((self.store.thread(task.thread_id) or {}).get("title") if task.thread_id else "") or "定时任务"
                title = f"{name} 已生成" if kind == "done" else f"{name} 失败"
                level = "timeSensitive"
            try:
                await self.notifier.notify(kind, title, body, task_id=task.id, thread_id=task.thread_id, deep=deep,
                                           level=level)
            except Exception as e:  # noqa: BLE001
                log.warning("notify failed: %s", e)

    async def _tell(self, task: Task, sid: str, msg: dict):
        for fn in list(self.listeners):
            try:
                await fn(task, sid, msg)
            except Exception as e:  # noqa: BLE001  a listener never breaks a task
                log.warning("task listener: %s", e)

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

    async def _installed_skills(self, task: Task, computer) -> str:
        from agent_runtime import skills as _skills
        cache = self.__dict__.setdefault("_skill_cache", {})
        hit = cache.get(task.machine_id)
        fresh = bool(re.search(r"skill|技能", task.prompt or "", re.I))   # an install task re-reads the list
        if hit and not fresh and time.time() - hit[0] < 300:
            items = hit[1]
        else:
            r = await computer.execute("shell.exec", {"command": _skills.INSTALLED_PROBE, "timeout": 15}, observe=False, timeout=25)
            res = getattr(r, "result", None)
            items = _skills.parse_installed(str((res if isinstance(res, dict) else {}).get("stdout") or ""))
            cache[task.machine_id] = (time.time(), items)
        b = _skills.installed_brief(items, task.prompt)
        return ("\n" + b) if b else ""

    async def _skill_preflight(self, task: Task, computer, emit, sid: str) -> str:
        """Check each skill the task uses on its computer; run the setup of a missing one (AIW_SKILL_SETUP_TIMEOUT,
        default 900 s). Returns a note for the agent. AIW_SKILL_PREFLIGHT=0 leaves it all to the agent as before."""
        if os.environ.get("AIW_SKILL_PREFLIGHT", "1") == "0":
            return ""
        from agent_runtime import skills as _skills
        items = _skills.preflight(task.prompt, self.store.kv_get("skills_custom", []) or [])
        if not items:
            return ""
        limit = float(os.environ.get("AIW_SKILL_SETUP_TIMEOUT", "900") or 900)

        async def sh(cmd: str, t: float):
            r = await computer.execute("shell.exec", {"command": cmd, "timeout": int(t)}, observe=False, timeout=t + 30)
            res = getattr(r, "result", None)
            res = res if isinstance(res, dict) else {}
            code = res.get("exit_code", res.get("returncode", 0 if getattr(r, "success", False) else 1))
            return (code == 0 and bool(getattr(r, "success", True))), str(res.get("stdout") or "") + str(res.get("stderr") or "")

        notes, installed = [], False
        for it in items:
            ok, _ = await sh(it["check"], 20)
            if ok:
                notes.append(f"{it['id']}: installed")
                continue
            if not it["setup"]:
                notes.append(f"{it['id']}: NOT installed and has no setup command; tell the user it is missing")
                continue
            await emit(AgentEvent(session_id=sid, type=EventType.MESSAGE, data={"text": f"这台电脑还没装 {it['title']}，先安装（只装这一次）…"}))
            ok, out = await sh(f"({it['setup']}) 2>&1 | tail -40", limit)
            ok = (await sh(it["check"], 20))[0]   # installed is what the check says, whatever the setup printed
            installed = installed or ok
            await emit(AgentEvent(session_id=sid, type=EventType.MESSAGE,
                                  data={"text": f"{it['title']} 已装好" if ok else f"{it['title']} 自动安装没成功，交给 Agent 处理"}))
            notes.append(f"{it['id']}: was missing, the system just installed it" if ok else
                         f"{it['id']}: missing; the system ran `{it['setup']}` and it FAILED (last output: "
                         f"{' '.join(out.split())[-400:]}). Fix the install once, or tell the user what blocks it")
        if installed:
            self.forget_installed_skills(task.machine_id)
        return ("\nSKILL CHECK (done by the system on this computer before you started; do not check or install "
                "these again): " + "; ".join(notes) + ".")

    def forget_installed_skills(self, machine_id: Optional[str] = None):
        """A task that installed a skill makes the next task re-read the list."""
        cache = self.__dict__.setdefault("_skill_cache", {})
        if machine_id:
            cache.pop(machine_id, None)
        else:
            cache.clear()

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
        out = list(await asyncio.gather(*(one(m) for m in self.store.machines())))
        try:   # aiw-roles-1: each cloud computer's role; the local VM stays out of shared work
            from . import roles as _roles
            out = _roles.annotate(self, out)
        except Exception as e:  # noqa: BLE001
            log.warning("roles: %s", e)
        return out

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
        try:   # aiw-roles-1: an 'auto' piece goes to the computer whose role fits it
            from . import roles as _roles
            items = _roles.place_auto(self, [dict(it) for it in items])
        except Exception as e:  # noqa: BLE001
            log.warning("roles: %s", e)
        plan = _place(items, roster, own)
        names = {m["id"]: m["name"] for m in roster}
        kids: list[dict] = []
        for it, mid, queued in plan:
            th = self.store.create_thread("↳ " + _title(it["goal"]), parent_task_id=ptask.id, parent_thread_id=ptask.thread_id)
            steps, secs = _budget(it["goal"], _es.step_cap(), _es.time_cap())
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
        # 3.8.2: children are never stopped by a clock (a stuck child pauses and asks the user itself);
        # AIW_DISPATCH_STOP_ON_TIMEOUT=1 brings the old overall limit back
        deadline = (time.monotonic() + timeout_s if os.environ.get("AIW_DISPATCH_STOP_ON_TIMEOUT", "0") == "1"
                    else float("inf"))
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

    async def _confirm_summary(self, tool, payload, decision, live: LiveSession) -> dict:
        """Plain-language card for a confirmation (title / 要做什么 / 影响 / 风险 / 为什么), built deterministically;
        AIW_CONFIRM_LLM=1 lets the model polish title + 要做什么 within AIW_CONFIRM_LLM_TIMEOUT s (risks never change)."""
        from tool_router.explain import summarize
        created = getattr(getattr(live.executor, "policy", None), "created", None)
        mname = ""
        with contextlib.suppress(Exception):
            m = self.store.machine(live.task.machine_id) or {}
            mname = m.get("name") or ""
        s = summarize(tool, payload if isinstance(payload, dict) else {}, decision.reason, live.last_thought,
                      int(decision.level), created, mname)
        if os.environ.get("AIW_CONFIRM_LLM", "").lower() in ("1", "true", "on") and not self._custom_llm:
            with contextlib.suppress(Exception):
                import json as _json
                llm = self._subagent_llm() or self._llm()
                raw = await asyncio.wait_for(llm.complete(
                    "把一个电脑操作的确认卡片改写得让普通人一眼看懂。只输出 JSON：{\"title\": \"确认：…（20 字内）\", "
                    "\"what\": \"一句话说明要做什么\"}。不要淡化风险，不要出现命令原文。",
                    _json.dumps({"title": s["title"], "what": s["what"], "risks": s["risks"], "why": s["why"]}, ensure_ascii=False),
                    200), float(os.environ.get("AIW_CONFIRM_LLM_TIMEOUT", "5")))
                j = _json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
                t, w = str(j.get("title") or "").strip(), str(j.get("what") or "").strip()
                if t.startswith("确认") and 4 < len(t) <= 40 and 4 < len(w) <= 160 and "&&" not in w and "rm -" not in w:
                    s["title"], s["what"], s["polished"] = t if t.startswith("确认：") else "确认：" + t.lstrip("确认:： "), w, True
        return s

    def answer_confirm(self, sid: str, approve: bool, scope: Optional[str] = None) -> bool:
        live = self.get(sid)
        if approve and scope == "task" and live.confirm_info and live.confirm_info.get("task_allow"):
            live.allow_task = True
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
        role = f" · 角色：{m['role_title']}" if m.get("role_title") else ""   # aiw-roles-1
        lines.append(f"- {m['id']} 「{m['name']}」 {m['kind']}{role} · {st}")
    usable = sum(1 for m in roster if m.get("available") or m["id"] == own)
    return ("COMPUTERS (this task runs on " + own + "; hand work to them with `dispatch`):\n" + "\n".join(lines) + "\n"
            "When the request has independent pieces that can run at the same time (several topics, sites, files, "
            "clients, tests or machines) or the user asks for parallel work (同时 / 并行 / 分头 / 多台电脑 / 每台电脑), "
            "call `dispatch` once with one item per piece instead of doing them one by one, then combine what comes back "
            "into one answer (merge files if one file was asked for). Give each piece to the computer whose 角色 fits it "
            "(name its id in `machine`; aiw-roles-1). Keep dependent steps, quick answers and small "
            "single jobs to yourself."
            + ("" if usable > 1 else " Only this computer is usable right now: dispatch would run the pieces one "
               "after another here, so do the work yourself unless the pieces are big and separate."))


def _title(prompt: str) -> str:
    p = " ".join((prompt or "").split())
    return p[:24] + ("…" if len(p) > 24 else "")


_OFFICE_TASK = re.compile(r"officecli|\.?docx|\.?xlsx|\.?pptx|word|excel|空白页|分页|文档|表格|幻灯片|PPT", re.I)


def _budget(prompt: str, max_steps: int, timeout_s: float, context: Optional[str] = None) -> tuple[int, float]:
    """0 means no budget (the 3.8.2 default) and stays 0: the per-skill bumps below only raise a budget that is set.
    """
    st, secs = _budget_raw(prompt, max_steps, timeout_s, context)
    return (st if max_steps and max_steps > 0 else 0), (secs if timeout_s and timeout_s > 0 else 0.0)


def _budget_raw(prompt: str, max_steps: int, timeout_s: float, context: Optional[str] = None) -> tuple[int, float]:
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


def image_cfg_from(m: dict, secrets=None) -> Optional[dict]:
    """See SessionManager._image_cfg. Also used by the chat and by /api/image/test."""
    m = m or {}
    gw_model = m.get("image_model") or os.environ.get("AIW_IMAGE_MODEL")
    gw_key = (secrets.get("openai_api_key") if secrets else None) or os.environ.get("OPENAI_API_KEY")
    gw_base = m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    c_model = m.get("image2_model") or os.environ.get("AIW_IMAGE2_MODEL")
    c_key = (secrets.get("image2_api_key") if secrets else None) or os.environ.get("AIW_IMAGE2_API_KEY")
    c_base = m.get("image2_base_url") or os.environ.get("AIW_IMAGE2_BASE_URL")
    gw = {"id": "gateway", "name": "主网关", "base_url": gw_base, "key": gw_key, "model": gw_model} \
        if gw_model and gw_key else None
    cu = {"id": "custom", "name": "自定义生图接口", "base_url": c_base.rstrip("/"), "key": c_key, "model": c_model} \
        if c_model and c_key and c_base else None
    default = (m.get("image_default") or "").strip() or ("custom" if cu else "gateway")
    chain = [p for p in ((cu, gw) if default == "custom" else (gw, cu)) if p]
    if not chain:
        return None
    first = chain[0]
    return {"model": first["model"], "key": first["key"], "base_url": first["base_url"], "chain": chain,
            "default": default}
