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
from tool_router.registry import SUBAGENT_TOOLS

from .control import AgentControl, StopRequested
from .planner import SYSTEM_PROMPT, make_plan
from .recovery import Recovery
from .validator import Validator

EventSink = Callable[[AgentEvent], Awaitable[None]]
AskFn = Callable[[str, list], Awaitable[Optional[str]]]
RememberFn = Callable[[str], Awaitable[None]]
ExtTool = tuple[dict, Callable[[dict], Awaitable[dict]]]  # (schema, call)


@dataclass
class LoopConfig:
    max_steps: int = 40
    timeout_s: float = 600.0
    action_retries: int = 1
    llm_image_width: int = 1280  # downscale screenshots sent to the LLM
    max_consecutive_failures: int = 5
    artifacts_dir: Optional[str] = None
    subagent_steps: int = 8
    make_plan: bool = True


class AgentLoop:
    def __init__(self, task: Task, session_id: str, computer, llm: LLMRouter, tools: ToolExecutor,
                 control: Optional[AgentControl] = None, emit: Optional[EventSink] = None,
                 config: Optional[LoopConfig] = None, ask: Optional[AskFn] = None,
                 remember: Optional[RememberFn] = None, extra_tools: Optional[dict[str, ExtTool]] = None,
                 subagent_llm: Optional[LLMRouter] = None, system_extra: str = "",
                 vision_llm: Optional[LLMRouter] = None):
        self.task, self.session_id = task, session_id
        self.computer, self.llm, self.tools = computer, llm, tools
        self.control = control or AgentControl()
        self.cfg = config or LoopConfig(max_steps=task.max_steps, timeout_s=task.timeout_s)
        self.validator = Validator()
        self.recovery = Recovery(computer)
        self._emit_fn = emit
        self.history: list[str] = []
        self.step = 0
        self.ask, self.remember = ask, remember
        self.extra_tools = extra_tools or {}
        self.subagent_llm = subagent_llm
        self.vision_llm = vision_llm  # optional "eyes": reads the screen for a tool model without vision
        self._seen_hash: Optional[str] = None
        self._seen_desc: Optional[str] = None
        self.system = SYSTEM_PROMPT + (("\n" + system_extra) if system_extra else "")
        self.plan_steps: list[str] = []
        self.plan_done: set[int] = set()

    @property
    def task_text(self) -> str:
        if self.task.context:
            return f"{self.task.context}\n\nCURRENT REQUEST: {self.task.prompt}"
        return self.task.prompt

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

    async def _deliver_file(self, res: dict):
        b64 = res.get("content_b64")
        if not b64 or not self.cfg.artifacts_dir:
            return
        name = Path(res.get("path") or "file").name
        d = Path(self.cfg.artifacts_dir) / self.session_id / "files"
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_bytes(base64.b64decode(b64))
        await self.emit(EventType.FILE, name=name, size=res.get("size"), artifact=str(p),
                        truncated=bool(res.get("size") and res["size"] > p.stat().st_size))

    # ------------------------------------------------------------------- run
    async def run(self) -> Task:
        self.task.status = TaskStatus.RUNNING
        await self.emit(EventType.TASK_STARTED, prompt=self.task.prompt)
        try:
            if self.cfg.make_plan:
                self.plan_steps = await make_plan(self.llm, self.task.prompt, self.task.context)
                await self.emit(EventType.PLAN, plan=self.plan_steps, done=[])
        except Exception:  # noqa: BLE001
            self.plan_steps = []
        try:
            await asyncio.wait_for(self._run(), self.cfg.timeout_s)
        except asyncio.TimeoutError:
            self._finish(TaskStatus.FAILED, f"timeout after {self.cfg.timeout_s}s")
        except StopRequested:
            self._finish(TaskStatus.STOPPED, "stopped by user")
        except Exception as e:  # noqa: BLE001
            self._finish(TaskStatus.FAILED, f"{type(e).__name__}: {e}")
        if self.task.status == TaskStatus.SUCCEEDED and self.plan_steps and len(self.plan_done) < len(self.plan_steps):
            self.plan_done = set(range(len(self.plan_steps)))
            await self.emit(EventType.PLAN_UPDATE, done=sorted(self.plan_done), note=None)
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

            desc = await self._look(before, shown) if self.vision_llm and before else None
            await self.emit(EventType.THINKING)
            decision = await self.llm.decide(LLMRequest(
                system=self.system, task=self._task_with_plan(), history=self.history,
                screenshot_b64=before, tools=tool_schemas([t[0] for t in self.extra_tools.values()]),
                screen=shown, note=note, screen_desc=desc))
            if decision.tool and not decision.args and decision.tool not in ("screenshot", "done", "fail"):
                raw = decision.raw or {}
                try:
                    m = raw["choices"][0]["message"]
                    detail = str({"content": (m.get("content") or "")[:300], "tool_calls": m.get("tool_calls")})[:600]
                except Exception:  # noqa: BLE001
                    detail = str(raw)[:600]
                await self.emit(EventType.ERROR, error=f"模型调用 {decision.tool} 时参数为空", raw=detail)
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

            await self.emit(EventType.ACTION, tool=decision.tool, args=decision.args)
            handled = await self._loop_tool(decision)
            if handled is not None:
                self.history.append(f"{self.step}. {decision.tool}({_args_short(decision.args)}) -> {handled}")
                note = None
                continue

            outcome = None
            for attempt in range(self.cfg.action_retries + 1):
                outcome = await self.tools.run(decision.tool, decision.args, intent=decision.thought)
                await self.emit(EventType.POLICY, level=int(outcome.decision.level), reason=outcome.decision.reason)
                retryable = (not outcome.success and outcome.decision.allowed and outcome.control is None
                             and outcome.error and ("timeout" in outcome.error or "Connection" in outcome.error))
                if not retryable:
                    break
                await asyncio.sleep(0.5)
            if decision.tool == "send_file" and outcome.success and isinstance(outcome.result, dict):
                await self._deliver_file(outcome.result)
                outcome.result = {k: v for k, v in outcome.result.items() if k != "content_b64"}
                outcome.result["sent_to_user"] = True
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

            if not outcome.success and (outcome.error or "").startswith("missing args"):
                failures += 1
                if failures >= self.cfg.max_consecutive_failures:
                    self._finish(TaskStatus.FAILED, f"模型连续 {failures} 次调用 {decision.tool} 时没有给出参数（{outcome.error}）。"
                                                    "多半是模型输出被截断或网关不支持工具参数，可换模型或调大 AIW_LLM_MAX_TOKENS。")
                    return
                note = (f"Your last {decision.tool} call had no arguments ({outcome.error}). Call it again WITH the "
                        "required arguments. If the content is long, use files_write to save a script first, then run it.")
                continue
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


    async def _look(self, shot: str, shown: tuple[int, int]) -> Optional[str]:
        """Ask the vision model what is on screen. Re-read only when the picture changed."""
        import hashlib
        h = hashlib.sha1(shot.encode()).hexdigest()
        if h == self._seen_hash and self._seen_desc:
            return self._seen_desc + "\n(screen unchanged since the last description)"
        last = self.history[-1] if self.history else "(first step)"
        prompt = (f"TASK OF THE AGENT: {self.task.prompt}\nLAST STEP: {last}\n"
                  f"The screenshot is {shown[0]}x{shown[1]} pixels. Describe it for an agent that cannot see:\n"
                  "1. which app/window is in front, page title or URL, any dialog, error or popup;\n"
                  "2. the visible text that matters for the task, quoted exactly;\n"
                  "3. every button, link, field or menu the agent may need next, each as: name -> (x, y) center pixel.\n"
                  "Be factual and concise (max ~250 words). Do not suggest actions.")
        try:
            await self.emit(EventType.THINKING, phase="vision")
            desc = (await self.vision_llm.describe("You are the eyes of a computer-use agent.", prompt, shot)).strip()
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ERROR, error=f"视觉模型失败，改为直接发截图：{e}"[:300])
            return None
        if not desc:
            return None
        self._seen_hash, self._seen_desc = h, desc
        await self.emit(EventType.OBSERVATION, vision=desc[:1500])
        return desc

    def _task_with_plan(self) -> str:
        if not self.plan_steps:
            return self.task_text
        lines = [f"  [{'x' if i in self.plan_done else ' '}] {i}. {st}" for i, st in enumerate(self.plan_steps)]
        return self.task_text + "\n\nPLAN:\n" + "\n".join(lines)

    # ------------------------------------------------- tools handled by the loop
    async def _loop_tool(self, decision) -> Optional[str]:
        """update_plan / ask_user / remember / delegate / external tools. Returns a history
        note when handled here, None when the Tool Router should run it."""
        t, a = decision.tool, decision.args or {}
        if t == "update_plan":
            for i in a.get("done") or []:
                if isinstance(i, int) and 0 <= i < len(self.plan_steps):
                    self.plan_done.add(i)
            await self.emit(EventType.PLAN_UPDATE, done=sorted(self.plan_done), note=a.get("note"))
            await self.emit(EventType.ACTION_RESULT, tool=t, success=True, error=None, result={})
            return f"plan updated, done={sorted(self.plan_done)}"
        if t == "ask_user":
            q = a.get("question") or decision.thought or "?"
            opts = [str(o) for o in (a.get("options") or [])][:6]
            await self.emit(EventType.INPUT_REQUIRED, question=q, options=opts)
            reply = await self.ask(q, opts) if self.ask else None
            await self.emit(EventType.INPUT_ANSWERED, answer=reply)
            await self.emit(EventType.ACTION_RESULT, tool=t, success=reply is not None, error=None if reply else "no answer",
                            result={"answer": reply})
            return f"user answered: {reply!r}" if reply else "user did not answer within the time limit; decide yourself or fail"
        if t == "remember":
            text = (a.get("text") or "").strip()
            if text and self.remember:
                await self.remember(text)
            await self.emit(EventType.MEMORY, text=text)
            await self.emit(EventType.ACTION_RESULT, tool=t, success=bool(text), error=None, result={})
            return "saved to memory"
        if t == "delegate":
            if not (a.get("goal") or "").strip():
                await self.emit(EventType.ACTION_RESULT, tool=t, success=False, error="missing args ['goal']", result={})
                return ("FAILED: delegate needs a non-empty `goal` describing the whole sub-task. "
                        "Call it again with goal filled in, or do the work yourself with shell/files_write.")
            summary = await self._delegate(a.get("goal") or "")
            self._acted = True
            return f"helper finished: {summary[:400]}"
        if t in self.extra_tools:
            _schema, fn = self.extra_tools[t]
            await self.emit(EventType.TOOL_CALL, tool=t, args=a)
            if _RISKY_EXT.search(t) and self.tools.confirm:
                from tool_router import Decision, RiskLevel
                ok = await self.tools.confirm(t, a, Decision(RiskLevel.L3, "external tool can act outside the VM"))
                if not ok:
                    await self.emit(EventType.ACTION_RESULT, tool=t, success=False, error="user declined", result={})
                    return "user declined this external action; choose another approach or ask_user"
            try:
                res = await fn(a)
                ok, err = True, None
            except Exception as e:  # noqa: BLE001
                res, ok, err = {}, False, f"{type(e).__name__}: {e}"
            self._acted = self._acted or ok
            await self.emit(EventType.ACTION_RESULT, tool=t, success=ok, error=err, result=_short(res if isinstance(res, dict) else {"result": res}))
            text = str(res)[:600] if ok else f"FAILED ({err})"
            return text
        return None

    async def _delegate(self, goal: str) -> str:
        """Text-only helper: shell/file tools, no screenshots, its own short history."""
        llm = self.subagent_llm or self.llm
        await self.emit(EventType.SUBAGENT, phase="start", goal=goal)
        hist: list[str] = []
        summary = "helper hit its step limit"
        schemas = tool_schemas(only=SUBAGENT_TOOLS)
        for i in range(self.cfg.subagent_steps):
            await self.control.checkpoint()
            d = await llm.decide(LLMRequest(
                system="You are a helper agent with a Linux shell and the ~/workspace folder. No screen. "
                       "One tool call per turn. Finish with done and a factual summary of what you did and produced.",
                task=goal, history=hist, screenshot_b64=None, tools=schemas))
            if d.tool not in SUBAGENT_TOOLS:
                hist.append(f"{i+1}. {d.tool} is not available to the helper")
                continue
            await self.emit(EventType.SUBAGENT, phase="step", tool=d.tool, args=_args_short(d.args))
            if d.tool in ("done", "fail"):
                summary = d.args.get("summary") or d.args.get("reason") or d.thought or d.tool
                break
            o = await self.tools.run(d.tool, d.args, intent=d.thought)
            out = ""
            if o.action == "shell.exec" and o.result:
                out = f" exit={o.result.get('exit_code')} out={str(o.result.get('stdout',''))[:300]!r} err={str(o.result.get('stderr',''))[:200]!r}"
            hist.append(f"{i+1}. {d.tool}({_args_short(d.args)}) -> {'ok' if o.success else 'FAILED ' + str(o.error)}{out}")
        await self.emit(EventType.SUBAGENT, phase="done", summary=summary)
        await self.emit(EventType.ACTION_RESULT, tool="delegate", success=True, error=None, result={"summary": summary[:500]})
        return summary


import re as _re
_RISKY_EXT = _re.compile(r"(send|delete|remove|pay|purchase|order|post|publish|transfer|create|update|write|cancel)", _re.I)


def _args_short(a: dict) -> dict:
    return {k: (v[:80] if isinstance(v, str) else v) for k, v in (a or {}).items()}


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
