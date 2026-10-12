"""Session manager: one running AgentLoop per session, plus its control,
event fan-out to WebSocket subscribers, and L3 confirmation futures."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from agent_runtime import AgentControl, AgentLoop, LoopConfig
from aiw_protocols import AgentEvent, EventType, Task, TaskStatus
from computer_runtime import Computer, LinuxComputerProvider
from llm_router import LLMRouter
from tool_router import ToolExecutor

from .store import Store

log = logging.getLogger("aiw.sessions")


@dataclass
class LiveSession:
    id: str
    task: Task
    control: AgentControl
    runner: Optional[asyncio.Task] = None
    subscribers: set = field(default_factory=set)
    confirm: Optional[asyncio.Future] = None
    confirm_info: Optional[dict] = None


class SessionManager:
    def __init__(self, store: Store, settings, llm_factory=None, computer_factory=None):
        self.store, self.settings = store, settings
        self.live: dict[str, LiveSession] = {}
        self._llm_factory = llm_factory or LLMRouter.from_env
        self._computer_factory = computer_factory or self._default_computer
        self._computers: dict[str, Computer] = {}

    def _default_computer(self, machine: dict) -> Computer:
        return Computer(LinuxComputerProvider(machine["agent_endpoint"], self.settings.agent_token), machine["id"])

    def computer(self, machine_id: str) -> Computer:
        if machine_id not in self._computers:
            m = self.store.machine(machine_id)
            if not m:
                raise KeyError(machine_id)
            self._computers[machine_id] = self._computer_factory(m)
        return self._computers[machine_id]

    def busy_session(self, machine_id: str) -> Optional[LiveSession]:
        for s in self.live.values():
            if s.task.machine_id == machine_id and s.runner and not s.runner.done():
                return s
        return None

    async def start(self, task: Task) -> LiveSession:
        if self.busy_session(task.machine_id):
            raise RuntimeError(f"machine {task.machine_id} already has a running session")
        sid = self.store.create_session(task.machine_id, task.id)
        live = LiveSession(sid, task, AgentControl())
        self.live[sid] = live
        computer = self.computer(task.machine_id)

        async def emit(ev: AgentEvent):
            d = ev.model_dump(mode="json")
            self.store.add_event(d)
            if ev.type == EventType.OBSERVATION and ev.data.get("artifact"):
                self.store.add_artifact(sid, "screenshot", ev.data["artifact"])
            await self._broadcast(live, {"kind": "event", **d})

        async def audit(rec: dict):
            self.store.add_action(sid, rec)

        async def confirm(tool, payload, decision) -> bool:
            live.confirm = asyncio.get_running_loop().create_future()
            live.confirm_info = {"tool": tool, "payload": payload, "level": int(decision.level), "reason": decision.reason}
            self.store.update_task(task.id, TaskStatus.WAITING_CONFIRM.value)
            await emit(AgentEvent(session_id=sid, type=EventType.CONFIRM_REQUIRED, data=live.confirm_info))
            try:
                return await asyncio.wait_for(live.confirm, 300)
            except asyncio.TimeoutError:
                return False
            finally:
                live.confirm = live.confirm_info = None
                self.store.update_task(task.id, TaskStatus.RUNNING.value)

        loop = AgentLoop(task, sid, computer, self._llm_factory(), ToolExecutor(computer, confirm=confirm, audit=audit),
                         control=live.control, emit=emit,
                         config=LoopConfig(max_steps=task.max_steps, timeout_s=task.timeout_s,
                                           artifacts_dir=self.settings.artifacts_dir))

        async def runner():
            self.store.update_task(task.id, TaskStatus.RUNNING.value)
            t = await loop.run()
            self.store.update_task(task.id, t.status.value, t.result)
            self.store.end_session(sid, t.status.value)

        live.runner = asyncio.create_task(runner())
        return live

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
        if op == "stop" and live.confirm and not live.confirm.done():
            live.confirm.set_result(False)
        status = {"pause": "paused", "takeover": "takeover", "resume": "running", "stop": "stopped"}[op]
        self.store.update_task(live.task.id, status)
        etype = {"pause": EventType.PAUSED, "takeover": EventType.TAKEOVER, "resume": EventType.RESUMED,
                 "stop": EventType.TASK_FAILED}[op]
        ev = AgentEvent(session_id=sid, type=etype, data={"by": "user", "op": op})
        self.store.add_event(ev.model_dump(mode="json"))
        await self._broadcast(live, {"kind": "event", **ev.model_dump(mode="json")})
        return {"session_id": sid, "state": c.state.value}

    def answer_confirm(self, sid: str, approve: bool) -> bool:
        live = self.get(sid)
        if live.confirm and not live.confirm.done():
            live.confirm.set_result(approve)
            return True
        return False
