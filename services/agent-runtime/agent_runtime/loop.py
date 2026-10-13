"""Agent Loop (spec §18):

Task -> Planner -> [checkpoint] Observe -> LLM Decision -> Policy -> Execute
     -> Observe -> Validate -> Success | Recovery -> ... until done/fail/max_steps/timeout
"""
from __future__ import annotations

import asyncio
import base64
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiw_protocols import AgentEvent, EventType, Task, TaskStatus
from llm_router import LLMRequest, LLMRouter
from PIL import Image
from tool_router import ToolExecutor, tool_schemas

from .control import AgentControl, StopRequested
from .planner import SYSTEM_PROMPT, plan
from .recovery import Recovery
from .validator import Validator

EventSink = Callable[[AgentEvent], Awaitable[None]]


@dataclass
class LoopConfig:
    max_steps: int = 40
    timeout_s: float = 600.0
    action_retries: int = 1
    llm_image_width: int = 1280  # downscale screenshots sent to the LLM
    max_consecutive_failures: int = 5
    artifacts_dir: Optional[str] = None


class AgentLoop:
    def __init__(self, task: Task, session_id: str, computer, llm: LLMRouter, tools: ToolExecutor,
                 control: Optional[AgentControl] = None, emit: Optional[EventSink] = None,
                 config: Optional[LoopConfig] = None):
        self.task, self.session_id = task, session_id
        self.computer, self.llm, self.tools = computer, llm, tools
        self.control = control or AgentControl()
        self.cfg = config or LoopConfig(max_steps=task.max_steps, timeout_s=task.timeout_s)
        self.validator = Validator()
        self.recovery = Recovery(computer)
        self._emit_fn = emit
        self.history: list[str] = []
        self.step = 0

    async def emit(self, type_: EventType, **data):
        if self._emit_fn:
            await self._emit_fn(AgentEvent(session_id=self.session_id, type=type_, step=self.step, data=data))

    # ---------------------------------------------------------------- observe
    async def observe(self) -> tuple[Optional[str], tuple[int, int]]:
        r = await self.computer.execute("computer.screenshot")
        if not r.success or not r.screenshot:
            await self.emit(EventType.ERROR, error=f"screenshot failed: {r.error}")
            return None, (0, 0)
        full = (r.width or 0, r.height or 0)
        shot = r.screenshot
        if full[0] > self.cfg.llm_image_width:
            img = Image.open(io.BytesIO(base64.b64decode(shot)))
            ratio = self.cfg.llm_image_width / img.width
            img = img.resize((self.cfg.llm_image_width, int(img.height * ratio)))
            buf = io.BytesIO(); img.save(buf, "PNG")
            shot = base64.b64encode(buf.getvalue()).decode()
            self.tools.scale = 1 / ratio
            shown = img.size
        else:
            self.tools.scale = 1.0
            shown = full
        artifact = self._save_artifact(r.screenshot)
        await self.emit(EventType.OBSERVATION, width=full[0], height=full[1], artifact=artifact)
        return shot, shown

    def _save_artifact(self, b64: str) -> Optional[str]:
        if not self.cfg.artifacts_dir:
            return None
        d = Path(self.cfg.artifacts_dir) / self.session_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"step-{self.step:03d}-{int(time.time()*1000)}.png"
        p.write_bytes(base64.b64decode(b64))
        return str(p)

    # ------------------------------------------------------------------- run
    async def run(self) -> Task:
        self.task.status = TaskStatus.RUNNING
        await self.emit(EventType.TASK_STARTED, prompt=self.task.prompt)
        await self.emit(EventType.PLAN, plan=plan(self.task.prompt))
        try:
            await asyncio.wait_for(self._run(), self.cfg.timeout_s)
        except asyncio.TimeoutError:
            self._finish(TaskStatus.FAILED, f"timeout after {self.cfg.timeout_s}s")
        except StopRequested:
            self._finish(TaskStatus.STOPPED, "stopped by user")
        except Exception as e:  # noqa: BLE001
            self._finish(TaskStatus.FAILED, f"{type(e).__name__}: {e}")
        ev = {TaskStatus.SUCCEEDED: EventType.TASK_FINISHED}.get(self.task.status, EventType.TASK_FAILED)
        await self.emit(ev, status=self.task.status.value, result=self.task.result, steps=self.step)
        return self.task

    def _finish(self, status: TaskStatus, result: str):
        self.task.status, self.task.result = status, result

    async def _run(self):
        failures = 0
        shots = 0
        self._acted = False
        self._done_rejects = 0
        note: Optional[str] = None
        while self.step < self.cfg.max_steps:
            if await self.control.checkpoint():
                note = ("The user took over the desktop and has now returned control. "
                        "Re-assess from the current screenshot; do not assume earlier state."
                        if self.control.resumed_after_takeover else "Agent was paused and resumed.")
                await self.emit(EventType.RESUMED, takeover=self.control.resumed_after_takeover)
            self.step += 1
            before, shown = await self.observe()

            await self.emit(EventType.THINKING)
            decision = await self.llm.decide(LLMRequest(
                system=SYSTEM_PROMPT, task=self.task.prompt, history=self.history,
                screenshot_b64=before, tools=tool_schemas(), screen=shown, note=note))
            note = None
            shots = shots + 1 if decision.tool == "screenshot" else 0
            if shots >= 2:
                note = ("You keep calling screenshot, but the current screenshot is already attached every turn. "
                        "Do NOT call screenshot again. Take the next real action, or if the task is only to look/report, "
                        "call done with the full answer in summary.")
            if shots >= 4:
                decision.tool, decision.args = "done", {"summary": decision.thought or "已截图，但模型未给出描述。"}
            if decision.tool == "done" and not self._acted and _needs_action(self.task.prompt) and self._done_rejects < 2:
                self._done_rejects += 1
                await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args)
                await self.emit(EventType.VALIDATION, success=False, change=0.0,
                                message="拒绝 done：任务要求操作，但还没有执行任何操作")
                self.history.append(f"{self.step}. done REJECTED: no action has been performed yet")
                note = ("REJECTED: you called done without performing any action. The task requires real actions "
                        "(open/search/type/click...). Do the first action now, e.g. browser_open with the URL. "
                        "Do not invent results.")
                continue
            await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args)

            if await self.control.checkpoint():  # paused while the model was thinking: discard stale decision
                self.step -= 1
                note = "Paused before acting; previous decision discarded. Re-assess."
                continue

            outcome = None
            await self.emit(EventType.ACTION, tool=decision.tool, args=decision.args)
            for attempt in range(self.cfg.action_retries + 1):
                outcome = await self.tools.run(decision.tool, decision.args, intent=decision.thought)
                await self.emit(EventType.POLICY, level=int(outcome.decision.level), reason=outcome.decision.reason)
                retryable = (not outcome.success and outcome.decision.allowed and outcome.control is None
                             and outcome.error and ("timeout" in outcome.error or "Connection" in outcome.error))
                if not retryable:
                    break
                await asyncio.sleep(0.5)
            if outcome.success and decision.tool not in ("screenshot", "done", "fail"):
                self._acted = True
            await self.emit(EventType.ACTION_RESULT, tool=outcome.tool, success=outcome.success,
                            error=outcome.error, result=_short(outcome.result))

            if outcome.control == "done":
                self._finish(TaskStatus.SUCCEEDED, decision.args.get("summary", "done"))
                return
            if outcome.control == "fail":
                self._finish(TaskStatus.FAILED, decision.args.get("reason", "agent gave up"))
                return

            after = outcome.screenshot
            v = self.validator.check(outcome.action, outcome.success, outcome.error, before, after)
            await self.emit(EventType.VALIDATION, success=v.success, change=v.changed, message=v.message)
            self.history.append(_hist_line(self.step, decision, outcome, v.message))

            if outcome.decision.forbidden or (outcome.decision.needs_confirm and not outcome.success):
                failures += 1
                note = f"Action blocked: {outcome.error}. Choose a different approach or call fail."
                continue
            if v.success:
                failures = 0
            else:
                failures += 1
                if failures >= self.cfg.max_consecutive_failures:
                    self._finish(TaskStatus.FAILED, f"too many consecutive failures; last: {v.message}")
                    return
                note = await self.recovery.recover(failures, outcome.action)
                await self.emit(EventType.RECOVERY, attempt=failures, note=note)
        self._finish(TaskStatus.FAILED, f"max steps ({self.cfg.max_steps}) reached")


def _short(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        out[k] = v[:500] + "..." if isinstance(v, str) and len(v) > 500 else v
    return out


def _hist_line(step, decision, outcome, vmsg) -> str:
    args = {k: (v[:80] if isinstance(v, str) else v) for k, v in decision.args.items()}
    res = ""
    if outcome.action == "shell.exec" and outcome.result:
        res = f" exit={outcome.result.get('exit_code')} out={outcome.result.get('stdout','')[:200]!r}"
    status = "ok" if outcome.success else f"FAILED ({outcome.error})"
    return f"{step}. {decision.tool}({args}) -> {status}{res}; {vmsg}"


_ACTION_WORDS = ("打开", "搜索", "输入", "点击", "新建", "创建", "运行", "执行", "下载", "访问", "登录", "安装", "写入", "删除",
                 "open", "search", "type", "click", "create", "run", "download", "visit", "install", "write", "go to")


def _needs_action(prompt: str) -> bool:
    p = (prompt or "").lower()
    return any(w in p for w in _ACTION_WORDS)
