"""Agent Loop (spec §18):

Task -> Planner -> [checkpoint] Observe -> LLM Decision -> Policy -> Execute
     -> Observe -> Validate -> Success | Recovery -> ... until done/fail/max_steps/timeout
"""
from __future__ import annotations

import asyncio
import json
import os
import base64
import io
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiw_protocols import AgentEvent, EventType, Task, TaskStatus
from llm_router import LLMRequest, LLMRouter
from PIL import Image
from tool_router import ToolExecutor, tool_schemas
from tool_router.registry import SUBAGENT_TOOLS

from . import cad as _cad

# Only an explicit "go on with the earlier drawing" resumes. The bare words matched far too much: run 3ac's prompt said
# "把那张补画完整" (redraw that thin sheet) and was taken for a resume, so the previous run's DXF stayed in cad/, LibreCAD
# kept the old drawing open and all 12 sheets were drawn on top of the old 12 (doubled titles and notes).
_CAD_RESUME = re.compile(r"(?i)(继续|接着|接上)(之前|上次|上一次|刚才|原来|前面|未完成|没画完|画完|画剩)|"
                         r"在(原|旧|之前|上次|已有)的?(图|图纸|dxf|文件)(上|基础|里)|(上次|之前|刚才)(没|未)(画完|完成)|"
                         r"\bresume\b|continue (the )?(previous|earlier|last|unfinished)")
_LIBRECAD_PROC = re.compile(r"(?i)(nohup\s+librecad|(^|[;&|(]\s*)librecad\s*($|[&>;)])|p?kill(all)?\b[^;&|]*librecad)")
from .control import AgentControl, StopRequested
from .grounding import convert_desc, coord_instruction, coord_mode, locate, model_name
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
    skip_unchanged_screens: bool = True  # don't resend an identical screenshot (terminal/file work)
    vision_timeout_s: float = 25.0  # a slow "eyes" model must not stall every step
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
        self._cad_mode = _cad.cad_mode(task.prompt, task.context)  # fresh | revise | None
        self._cad_task = self._cad_mode is not None
        self._cad_file: Optional[str] = None  # the DXF this task saves to (a revision edits it in place)
        if self._cad_task and config is None:  # drawing a set in the GUI takes hundreds of steps
            self.cfg.max_steps = max(self.cfg.max_steps, int(os.environ.get("AIW_CAD_MAX_STEPS", "400")))
            self.cfg.timeout_s = max(self.cfg.timeout_s, float(os.environ.get("AIW_CAD_TIMEOUT", "10800")))
        self.validator = Validator()
        self._cad_keys_saved: set = set()
        self._cad_touched: set = set()
        self._cad_keys_unsaved: set = set()
        self.recovery = Recovery(computer)
        self._emit_fn = emit
        self.history: list[str] = []
        self._images_read: dict[str, tuple[int, str]] = {}  # path -> (step, description): history keeps only ~25 steps
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
        self._fresh: Optional[str] = None  # post-action screenshot, reused as the next observation
        self._full: Optional[Image.Image] = None  # latest full-resolution screen, for click_element zooming
        self._eyes_mode = coord_mode(model_name(vision_llm)) if vision_llm else "pixel"

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
        fresh, self._fresh = self._fresh, None
        if fresh:  # the screenshot taken right after the last action is still current: no second grab
            img = Image.open(io.BytesIO(base64.b64decode(fresh))).convert("RGB")
            self._full = img
            return await self._shrink(img, fresh, {"reused": True})
        t_shot = time.time()
        r = await self.computer.execute("computer.screenshot")
        shot_ms = int((time.time() - t_shot) * 1000)
        if not r.success or not r.screenshot:
            await self.emit(EventType.ERROR, error=f"screenshot failed: {r.error}")
            return None, (0, 0)
        img = Image.open(io.BytesIO(base64.b64decode(r.screenshot))).convert("RGB")
        self._full = img
        meta = r.result if isinstance(getattr(r, "result", None), dict) else {}
        return await self._shrink(img, r.screenshot, dict(
            shot_ms=shot_ms, vm_ms=getattr(r, "duration_ms", None), grabber=meta.get("grabber"),
            grab_ms=meta.get("grab_ms"), encode_ms=meta.get("encode_ms")))

    async def _shrink(self, img, raw_b64, info) -> tuple[Optional[str], tuple[int, int]]:
        full = img.size
        if full[0] > self.cfg.llm_image_width:
            ratio = self.cfg.llm_image_width / img.width
            img = img.resize((self.cfg.llm_image_width, int(img.height * ratio)))
            self.tools.scale = 1 / ratio
        else:
            self.tools.scale = 1.0
        shown = img.size
        # JPEG is ~5-10x smaller than PNG: faster upload and fewer image tokens on most gateways
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=75)
        shot = base64.b64encode(buf.getvalue()).decode()
        artifact = self._save_artifact(raw_b64)
        await self.emit(EventType.OBSERVATION, width=full[0], height=full[1], artifact=artifact, **info)
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
        plan_job = None
        if self.cfg.make_plan and _needs_action(self.task.prompt):
            plan_job = asyncio.create_task(self._plan())  # plan in parallel with the first step, not before it
        if self._cad_mode == "revise":
            await self._cad_revise_start()
        elif self._cad_task and not self._cad_resuming():
            await self._cad_fresh_start()
        try:
            await asyncio.wait_for(self._run(), self.cfg.timeout_s)
        except asyncio.TimeoutError:
            self._finish(TaskStatus.FAILED, f"timeout after {self.cfg.timeout_s}s")
        except StopRequested:
            self._finish(TaskStatus.STOPPED, "stopped by user")
        except Exception as e:  # noqa: BLE001
            self._finish(TaskStatus.FAILED, f"{type(e).__name__}: {e}")
        if plan_job and not plan_job.done():
            plan_job.cancel()
        if self.task.status == TaskStatus.SUCCEEDED and self.plan_steps and len(self.plan_done) < len(self.plan_steps):
            self.plan_done = set(range(len(self.plan_steps)))
            await self.emit(EventType.PLAN_UPDATE, done=sorted(self.plan_done), note=None)
        ev = {TaskStatus.SUCCEEDED: EventType.TASK_FINISHED}.get(self.task.status, EventType.TASK_FAILED)
        await self.emit(ev, status=self.task.status.value, result=self.task.result, steps=self.step)
        return self.task

    def _cad_resuming(self) -> bool:
        src = str(getattr(self.task, "source", "") or "").lower()
        return src in ("retry", "resume") or bool(_CAD_RESUME.search(self.task.prompt or ""))

    async def _cad_fresh_start(self):
        """A new drawing task starts from an empty cad/ folder and a fresh LibreCAD. The 2026-10-19 run found the
        previous run's DXF, plan.md and PDF there, took itself for a resumed task and never drew a line; and an
        old drawing left open in LibreCAD would have the new sheets drawn on top of it. Old files are moved, not
        deleted."""
        cmd = ('R="${VM_AGENT_FILES_ROOT:-$HOME/workspace}/' + _cad.CAD_DIR + '"; mkdir -p "$R"; cd "$R" && '
               'if ls -A | grep -qv "^_old$"; then B="_old/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$B"; '
               'for f in * .[!.]*; do [ -e "$f" ] && [ "$f" != _old ] && mv -- "$f" "$B/"; done; echo "moved to cad/$B"; fi; '
               'pkill -x librecad; rm -f "$HOME/.cache/aiw-cad-progress"; true')
        try:
            r = await self.tools.run("shell", {"command": cmd, "timeout": 30}, intent="start the drawing from an empty cad/ folder")
            res = r.result if isinstance(r.result, dict) else {}
            moved = (res.get("stdout") or "").strip()
            if moved:
                self.history.append(f"0. previous drawing files {moved}; this task draws everything anew (cad/ is empty)")
        except Exception:  # noqa: BLE001
            pass

    async def _cad_revise_start(self):
        """A follow-up in a drawing thread fixes the drawing that exists: open the newest DXF in cad/ in LibreCAD,
        remember its texts for the layout check, and tell the model to change only what the user named."""
        find = ('cd "${VM_AGENT_FILES_ROOT:-$HOME/workspace}" 2>/dev/null && ls -t ' + _cad.CAD_DIR +
                '/*.dxf 2>/dev/null | grep -v -e "_view" -e "/_" | head -1')
        try:
            r = await self.tools.run("shell", {"command": find, "timeout": 20}, intent="find the drawing to revise")
            path = ((r.result or {}).get("stdout") or "").strip().splitlines() if isinstance(r.result, dict) else []
        except Exception:  # noqa: BLE001
            path = []
        if not path:
            self.history.append("0. REVISION requested, but cad/ holds no drawing yet: draw the set as a new task would.")
            return
        self._cad_file = path[0]
        texts = []
        try:
            r = await self.tools.run("shell", {"command": _cad.edit_command(self._cad_file, [], texts=True), "timeout": 60},
                                     intent="read the texts of the existing drawing")
            info = _cad.parse_edit(((r.result or {}).get("stdout") or "") if isinstance(r.result, dict) else "")
            texts = (info or {}).get("texts") or []
        except Exception:  # noqa: BLE001
            pass
        self._seed_boxes = _cad.seed_text_boxes(texts)
        try:
            await self._type_cad([], self._cad_file, "open the drawing to revise in LibreCAD")
        except Exception:  # noqa: BLE001
            pass
        self.history.append(
            f"0. REVISION of the existing drawing {self._cad_file} (now open in LibreCAD, {len(texts)} texts on it). "
            "Change ONLY what the user asked; never redraw a sheet or a FRAME. Per fix: cad_command ['SHEET|n', "
            "'LIST|x1,y1|x2,y2'] to see what is there, then ['SHEET|n', 'ERASE|x1,y1|x2,y2' (deletes everything lying "
            "fully inside the window) or 'ERASETEXT|exact text', then the corrected items]. Then SAVE to the same file, "
            "PDF, send_file both, and done listing each change.")

    async def _plan(self):
        try:
            steps = await make_plan(self.llm, self.task.prompt, self.task.context)
        except Exception:  # noqa: BLE001
            return
        if self.task.status == TaskStatus.RUNNING and steps:
            self.plan_steps = steps
            await self.emit(EventType.PLAN, plan=self.plan_steps, done=sorted(self.plan_done))

    def _finish(self, status: TaskStatus, result: str):
        self.task.status, self.task.result = status, result

    async def _run(self):
        failures = 0
        shots = 0
        self._acted = False
        self._done_rejects = 0
        self._nocall = 0  # consecutive replies without a tool call
        self._sent_file, self._after_send = None, 0
        self._last_sent_thumb = None
        self._last_ok: Optional[tuple] = None
        self._cad_ok = 0           # successful cad_command batches
        self._cad_calls = 0        # cad_command calls, accepted or not (drafting has begun)
        self._last_ok_cad = False  # did the last cad_command go through
        self._sheet_audit: dict = {}  # entities per sheet from the last PDF export
        self._cad_pdf_ok = False      # PDF made by LibreCAD (PDF|...), not by soffice
        self._cad_sheet = 0           # sheet the next cad_command points belong to (set by FRAME|n / SHEET|n)
        self._cad_frames: set = set()  # sheets this task drew a FRAME for
        self._cad_text_boxes: dict = getattr(self, "_seed_boxes", None) or {}  # sheet -> [(box, text)] typed so far (a revision starts with the drawing's)
        self._cad_typed_texts: set = set()
        self._cad_touched: set = set()  # sheets this task drew or erased on
        self._cad_rejects: dict = {}  # sheet -> batches sent back by the layout check (typed anyway after 2)
        # every one-line item (line|, rect|, TEXT|, DIMH|, FRAME|n ...) that landed, in its absolute form: a batch sent
        # again after a timeout, a dialog error or a reopen skips what is already there instead of drawing it twice
        self._cad_keys_saved: set = set()    # landed and saved in the DXF
        self._cad_keys_unsaved: set = set()  # landed since the last save (lost if LibreCAD restarts)
        self._prev_sig: Optional[tuple] = None
        self._rep = 0
        self._last_tool: Optional[str] = None
        self._last_shown: tuple[int, int] = (0, 0)
        note: Optional[str] = None
        while self.step < self.cfg.max_steps:
            if await self.control.checkpoint():
                self._fresh = None
                note = ("The user took over the desktop and has now returned control. "
                        "Re-assess from the current screenshot; do not assume earlier state."
                        if self.control.resumed_after_takeover else "Agent was paused and resumed.")
                await self.emit(EventType.RESUMED, takeover=self.control.resumed_after_takeover)
            self.step += 1
            blind = self._last_tool in _NO_SCREEN_TOOLS and self.step > 1
            # a CAD batch that went through needs no fresh look (screenshot + vision cost ~15 s a step);
            # look again every 4th batch so a drawing error is still caught
            if (self._last_tool == "cad_command" and self._last_ok_cad and self.step > 1
                    and self._cad_ok % 4 != 0):
                blind = True
                cad_blind = True
            else:
                cad_blind = False
            if blind:  # a shell/file step does not change the screen: skip the screenshot and the vision call
                before, shown = None, self._last_shown
                note = ((note + "\n") if note else "") + (
                    "Screen not re-read: the last LibreCAD batch went through. Send the next batch from your plan; "
                    "the screen is shown again every few batches." if cad_blind else
                    "Screen not re-read: your last step was a shell/file action. Keep working in the shell; "
                    "call screenshot only if you need to see the desktop.")
            else:
                before, shown = await self.observe()
                self._last_shown = shown

            desc = await self._look(before, shown) if self.vision_llm and before else None
            send_shot = before
            if before and self.cfg.skip_unchanged_screens and not desc:
                tiny = _thumb(before)
                if tiny is not None and tiny == self._last_sent_thumb:
                    send_shot = None
                    note = ((note + "\n") if note else "") + (
                        "Screen unchanged since your last screenshot (image omitted to save time); rely on that one.")
                else:
                    self._last_sent_thumb = tiny
            await self.emit(EventType.THINKING)
            t_llm = time.time()
            req = LLMRequest(
                system=self.system, task=self._task_with_plan(), history=self.history,
                screenshot_b64=send_shot, tools=tool_schemas([t[0] for t in self.extra_tools.values()],
                                   hide=() if _cad.script_drawing_allowed(self.task.prompt) else ("cad_draw",)),
                screen=shown, note=note, screen_desc=desc,
                # drafting sheets from cad/plan.md is transcription: with thinking on, DeepSeek spent 11-13 min per
                # sheet reasoning about label positions in its head and often ran out of budget before any tool call
                # (task-960: 2 sheets in 79 min). The layout auto-fix moves colliding labels instead.
                thinking=(os.environ.get("AIW_CAD_DRAW_THINKING", "off") if self._cad_task and getattr(self, "_cad_calls", 0) else None))
            decision = None
            tries = int(os.environ.get("AIW_DECIDE_RETRIES", "4"))
            for attempt in range(tries):
                try:
                    decision = await self.llm.decide(req)
                    break
                except (StopRequested, asyncio.CancelledError):
                    raise
                except Exception as e:  # noqa: BLE001  one bad/slow gateway reply must not kill a long task
                    await self.emit(EventType.ERROR, error=f"模型调用失败（第 {attempt + 1}/{tries} 次）：{type(e).__name__}: {str(e)[:200]}")
                    if attempt == tries - 1:
                        raise
                    req.screenshot_b64 = None  # a smaller request is more likely to get through
                    await asyncio.sleep(min(60, 5 * 3 ** attempt))
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
            if shots >= 2 and self._cad_task and decision.tool == "screenshot":
                self.history.append(f"{self.step}. screenshot SKIPPED: the screenshot is attached every turn")
                note = ("Do NOT call screenshot. The view zooms to the whole drawing after every cad_command, so lines "
                        "far apart look small; trust the command history. Continue with the next cad_command batch "
                        "from your plan (cad/plan.md).")
                continue
            if shots >= 4:
                decision.tool, decision.args = "done", {"summary": decision.thought or "已截图，但模型未给出描述。"}
            if (decision.tool == "done" and self._cad_task and not self._sent_file and self._done_rejects < 6
                    and self.step < self.cfg.max_steps - 3):
                self._done_rejects += 1
                await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args)
                await self.emit(EventType.VALIDATION, success=False, change=0.0,
                                message="拒绝 done：图纸还没画完/没保存发送")
                self.history.append(f"{self.step}. done REJECTED: the drawing set is not finished, saved and sent")
                note = ("REJECTED: the drawings are not finished. Every sheet the user listed must be drawn in LibreCAD, "
                        "saved as DXF via File > Save As, and sent with send_file before done. Continue with the next "
                        "unfinished sheet in cad/plan.md now (cad_command). If you have no plan yet, write it first.")
                continue
            if (decision.tool == "done" and self._cad_task and self._sent_file and self._done_rejects < 6
                    and self.step < self.cfg.max_steps - 3 and (not self._cad_pdf_ok or self._sheet_audit.get("thin"))):
                self._done_rejects += 1
                thin = self._sheet_audit.get("thin")
                await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args)
                await self.emit(EventType.VALIDATION, success=False, change=0.0,
                                message=f"拒绝 done：图纸不完整（内容太少的图：{thin}）" if thin else "拒绝 done：PDF 不是用 LibreCAD 导出的")
                self.history.append(f"{self.step}. done REJECTED: " + (f"sheets {thin} are nearly empty" if thin else "no LibreCAD PDF"))
                note = (f"REJECTED: sheets {thin} have too few entities ({self._sheet_audit.get('counts')}); each sheet needs its "
                        "full content from cad/plan.md: geometry, every panel/component, dimension chains (DIMH/DIMV), "
                        "material leaders (LEADER), notes (NOTE), view title. Draw what is missing, then SAVE, PDF and "
                        "send_file both again." if thin else
                        "REJECTED: export the PDF with cad_command ['SAVE|cad/<name>.dxf', 'PDF|cad/<name>.dxf|cad/<name>.pdf'] "
                        "(LibreCAD's own dxf2pdf, one A3 page per sheet); soffice mangles DXF. Then send_file the PDF.")
                self._after_send = 0
                continue
            if (decision.tool == "fail" and str(decision.args.get("reason") or "").startswith("model returned no tool call")
                    and self._nocall < 3):
                # a reply without a tool call is a model hiccup, not the agent giving up: one bad turn (a thinking
                # model cut off mid-reasoning) ended run ac3 at step 10 before a single line was drawn
                self._nocall += 1
                await self.emit(EventType.DECISION, thought=(decision.thought or "")[-1500:], tool="(none)", args={})
                await self.emit(EventType.VALIDATION, success=False, change=0.0,
                                message=f"模型这一步没有调用工具（第 {self._nocall} 次），已提示重试")
                self.history.append(f"{self.step}. (no tool call: {decision.args.get('reason')})")
                note = ("Your last reply contained NO tool call" +
                        (" because it was cut off by the output limit" if "cut off" in str(decision.args.get("reason")) else "") +
                        ". Keep the reasoning to a few lines and call exactly one tool. Long content goes in parts: "
                        "files_write one sheet per call (append=true adds to the file), cad_command one sheet per call.")
                self.step -= 1
                continue
            if decision.tool != "fail":
                self._nocall = 0
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
            llm_ms = int((time.time() - t_llm) * 1000)
            if (self._sent_file and decision.tool not in ("done", "fail", "send_file")
                    and not (self._cad_task and (not self._cad_pdf_ok or self._sheet_audit.get("thin")))):
                self._after_send += 1
                if self._after_send >= 2:  # the file is already with the user; stop wandering
                    decision.tool, decision.args = "done", {"summary": f"已生成并发送 {self._sent_file}。"}
            u = decision.usage or {}
            await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args, llm_ms=llm_ms,
                            out_tokens=u.get("out_tokens") or u.get("completion_tokens"), attempts=u.get("attempts"))
            sig = (decision.tool, repr(sorted(decision.args.items())))
            if decision.tool not in ("screenshot", "wait", "done", "fail"):
                self._rep = self._rep + 1 if sig == self._prev_sig else 0
                self._prev_sig = sig
                if self._rep >= 4:
                    await self.emit(EventType.VALIDATION, success=False, change=0.0, message="同一操作连续重复，已停止")
                    self._finish(TaskStatus.FAILED, f"模型连续 {self._rep + 1} 次重复同一操作（{decision.tool}），已停止以免空转。请换更稳定的主模型或把任务拆小。")
                    return
                if self._rep >= 2:
                    self.history.append(f"{self.step}. {decision.tool} SKIPPED: same call as the previous {self._rep} steps; its result is already above")
                    note = (f"You have issued the identical {decision.tool} call {self._rep + 1} times in a row. Its result is already in "
                            "PREVIOUS STEPS. Do something different now: fix the problem shown there, run the script, or finish with done/fail.")
                    await self.emit(EventType.VALIDATION, success=False, change=0.0, message="重复操作已跳过")
                    continue
            if self._last_ok and sig == self._last_ok[0] and decision.tool in _NO_REPEAT:
                self.history.append(f"{self.step}. {decision.tool} SKIPPED: identical to step {self._last_ok[1]}, which already succeeded")
                note = (f"You just repeated step {self._last_ok[1]} exactly ({decision.tool}); it already succeeded, so it was skipped. "
                        "Move on to the NEXT step (e.g. run the script you wrote with a short shell command).")
                await self.emit(EventType.VALIDATION, success=False, change=0.0, message="重复操作已跳过")
                continue

            if await self.control.checkpoint():  # paused while the model was thinking: discard stale decision
                self.step -= 1
                note = "Paused before acting; previous decision discarded. Re-assess."
                continue

            self._last_tool = decision.tool
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
                self._sent_file = Path(outcome.result.get("path") or "file").name
                note = ("The file was delivered to the user's chat. If nothing else was asked, call done NOW "
                        "with a short Chinese summary.")
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
            self._last_ok = ((decision.tool, repr(sorted(decision.args.items()))), self.step) if outcome.success else None
            self._fresh = after if (after and outcome.success) else None

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
                  f"{coord_instruction(self._eyes_mode, shown)} Describe the screen for an agent that cannot see:\n"
                  "1. which app/window is in front, page title or URL, any dialog, error or popup;\n"
                  "2. the visible text that matters for the task, quoted exactly;\n"
                  "3. every button, link, field or menu the agent may need next, each as: name -> (x, y) center pixel.\n"
                  "Be factual and concise (max ~250 words). Do not suggest actions.")
        try:
            await self.emit(EventType.THINKING, phase="vision")
            desc = (await asyncio.wait_for(
                self.vision_llm.describe("You are the eyes of a computer-use agent.", prompt, shot),
                self.cfg.vision_timeout_s)).strip()
        except asyncio.TimeoutError:
            await self.emit(EventType.ERROR, error=f"视觉模型超过 {self.cfg.vision_timeout_s:.0f} 秒没回应，本步跳过（建议换更快的视觉模型）")
            return None
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ERROR, error=f"视觉模型失败，改为直接发截图：{e}"[:300])
            return None
        if not desc:
            return None
        desc = convert_desc(desc, shown, self._eyes_mode)  # 0-1000 answers -> pixels of the screenshot
        self._seen_hash, self._seen_desc = h, desc
        await self.emit(EventType.OBSERVATION, vision=desc[:1500])
        return desc

    def _task_with_plan(self) -> str:
        base = self.task_text
        if self._images_read:
            base += ("\n\nIMAGES ALREADY READ (do not read again; their content is in cad/plan.md or above): "
                     + ", ".join(self._images_read))
        if not self.plan_steps:
            return base
        lines = [f"  [{'x' if i in self.plan_done else ' '}] {i}. {st}" for i, st in enumerate(self.plan_steps)]
        return base + "\n\nPLAN:\n" + "\n".join(lines)

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
        if t == "click_element":
            return await self._click_element(a, decision.thought)
        if t == "cad_draw":
            if not _cad.script_drawing_allowed(self.task.prompt):
                await self.emit(EventType.ACTION_RESULT, tool=t, success=False, error="script drawing not allowed", result={})
                return ("REFUSED: the user did not ask for a script. Draw in the LibreCAD window instead: cad_command "
                        "with LibreCAD commands (line, rectangle, circle, dimhorizontal...), text/layers/save via its dialogs.")
            return await self._cad_draw(a, decision.thought)
        if t == "cad_command":
            return await self._cad_command(a, decision.thought)
        if t == "shell" and self._cad_task and _LIBRECAD_PROC.search(str(a.get("command") or "")):
            # starting/killing LibreCAD by hand left two instances (keystrokes went to the wrong one) and
            # `pkill -f librecad` killed the tool's own shell (exit -15) in the 2026-10-19 run
            await self.emit(EventType.ACTION_RESULT, tool=t, success=False, error="LibreCAD is managed by cad_command",
                            result={})
            return ("REFUSED: do not start or kill LibreCAD from shell. cad_command starts it, keeps a single instance, "
                    "closes leftover dialogs and resumes a cut-off batch by itself: just send the next cad_command. "
                    "To reopen a file use cad_command with open='cad/<name>.dxf'.")
        if t == "read_image":
            return await self._read_image(a)
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

    async def _click_element(self, a: dict, intent: str) -> str:
        """Locate a described element with the vision model (whole screen, then a zoomed crop) and click it."""
        target = (a.get("target") or "").strip()
        eyes = self.vision_llm or self.llm
        if not target:
            await self.emit(EventType.ACTION_RESULT, tool="click_element", success=False, error="missing args ['target']", result={})
            return "FAILED: click_element needs `target`, a description of the element"
        if self._full is None:
            await self.observe()
        full = self._full
        mode = self._eyes_mode if self.vision_llm else coord_mode(model_name(self.llm))
        try:
            pt = await asyncio.wait_for(locate(eyes, full, target, mode, max_width=self.cfg.llm_image_width),
                                        self.cfg.vision_timeout_s * 2)
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="click_element", success=False, error=str(e)[:200], result={})
            return f"FAILED: could not locate '{target}' ({type(e).__name__}). Try browser_click with its text, or click with x/y."
        if pt is None:
            await self.emit(EventType.ACTION_RESULT, tool="click_element", success=False, error="not found", result={})
            return f"FAILED: '{target}' is not visible on the screen. Scroll, open the right window, or describe it differently."
        # locate() answers in full-screen pixels; the click tool expects screenshot pixels (it rescales)
        sx, sy = pt
        x, y = int(round(sx / self.tools.scale)), int(round(sy / self.tools.scale))
        tool = "double_click" if a.get("double") else ("right_click" if a.get("button") == "right" else "click")
        before = None
        if full is not None:
            buf = io.BytesIO(); full.save(buf, "PNG"); before = base64.b64encode(buf.getvalue()).decode()
        outcome = await self.tools.run(tool, {"x": x, "y": y}, intent=intent)
        await self.emit(EventType.POLICY, level=int(outcome.decision.level), reason=outcome.decision.reason)
        v = self.validator.check(outcome.action, outcome.success, outcome.error, before, outcome.screenshot)
        await self.emit(EventType.ACTION_RESULT, tool="click_element", success=outcome.success, error=outcome.error,
                        result={"target": target, "screen_x": sx, "screen_y": sy, "x": x, "y": y})
        await self.emit(EventType.VALIDATION, success=v.success, change=v.changed, message=v.message)
        if outcome.success:
            self._acted = True
            self._fresh = outcome.screenshot
        return f"{tool} at ({x}, {y}) for '{target}' -> {'ok' if outcome.success else 'FAILED ' + str(outcome.error)}; {v.message}"

    async def _cad_command(self, a: dict, intent: str) -> str:
        """Type LibreCAD command-line commands into the visible LibreCAD window (xdotool), like a person drafting."""
        raw = [str(c) for c in (a.get("commands") or []) if str(c).strip()]
        self._cad_calls = getattr(self, "_cad_calls", 0) + 1
        if not raw:
            await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=False, error="missing args ['commands']", result={})
            return "FAILED: cad_command needs `commands`, e.g. ['line','0,0','6000,0','ESC']"
        for c in raw:
            ms = re.match(r"(?i)^\s*save\s*\|\s*(\S.*\.dxf)\s*$", c) or re.match(r"(?i)^\s*pdf\s*\|\s*([^|]+\.dxf)\s*\|", c)
            if ms:
                self._cad_file = ms.group(1).strip()
        edits, raw, _ = _cad.split_edits(raw, self._cad_sheet)
        edit_note, open_path = "", a.get("open") or None
        if edits:
            res_e = await self._cad_edits(edits, intent)
            if isinstance(res_e, str) and res_e.startswith("FAILED"):
                return res_e
            edit_note, reopen = res_e
            if reopen:
                open_path = self._cad_file
            if not [c for c in raw if not _cad._SHEET.match(c)]:
                if reopen:
                    await self._type_cad([], open_path, intent)
                return edit_note + " Now send the corrected items for this spot (no FRAME), then SAVE."
        # points go onto the current sheet (FRAME|n / SHEET|n), whether given sheet-locally or absolutely
        sheet_before = self._cad_sheet
        if a.get("open"):  # LibreCAD is restarted on the file: whatever was typed since the last save is gone
            self._cad_keys_unsaved.clear()
        have = self._cad_keys_saved | self._cad_keys_unsaved
        raw, keys, already = _cad.dedupe(raw[:CAD_MAX_BATCH], self._cad_sheet, have)
        fixed: list = []
        if os.environ.get("AIW_CAD_AUTOFIX", "1") != "0" and raw:
            # after dedupe: an item resent at its old spot must be skipped as already drawn, not moved off itself
            raw, fixed = _cad.autofix(raw, self._cad_sheet, self._cad_text_boxes)
            raw, keys, more_dup = _cad.dedupe(raw, self._cad_sheet, have)
            already += more_dup
        self._cad_fixed = fixed
        if already and not any(k is not None or not _cad._SHEET.match(c) for c, k in zip(raw, keys)):
            self._cad_sheet = _cad.place(raw, self._cad_sheet)[1]
            await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=True, error=None,
                            result={"commands": raw[:80], "count": already, "typed": 0, "skipped": already})
            return (f"nothing typed: all {already} items are already on the drawing from earlier batches of this task. "
                    f"Go on with the next sheet in cad/plan.md; never resend a finished sheet.")
        drawn: list = []
        cmds, self._cad_sheet, moved = _cad.place(raw, self._cad_sheet, drawn)
        self._cad_touched |= {sh for sh, _ in drawn}
        # a text already typed at the same spot (the model redoing a sheet) would only be drawn on top of itself
        dup = {c for c in cmds if _cad._parse_text(c) and c in self._cad_typed_texts}
        if dup:
            cmds = [c for c in cmds if c not in dup]
            drawn = [d for d in drawn if d[1] not in dup]
        bad = [c[5:] for c in cmds if c.startswith("#BAD ")]
        issues, boxes = _cad.layout_issues(drawn, self._cad_text_boxes)
        key = self._cad_sheet
        if (bad or issues) and self._cad_rejects.get(key, 0) < int(os.environ.get("AIW_CAD_MAX_REJECTS", "1")):
            # checked before typing: a misplaced or unreadable batch is cheaper to fix in the plan than on the canvas
            self._cad_rejects[key] = self._cad_rejects.get(key, 0) + 1
            self._cad_sheet = sheet_before
            await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=False, error="layout check",
                            result={"commands": raw[:80], "count": len(raw), "bad": bad[:10], "issues": issues[:20]})
            msg = "NOT TYPED, nothing was drawn: fix these and send the whole batch again."
            if fixed:
                msg += f" (already auto-fixed and kept in the batch: {len(fixed)} items; fix only what is listed here)"
            if bad:
                msg += (f" UNKNOWN COMMANDS {bad[:6]}: use 'line|x1,y1|x2,y2|...', 'rect|x1,y1|x2,y2', 'circle|x,y|r', "
                        f"'arc|cx,cy|r|a1|a2', TEXT/TEXTC/NOTE/LEADER/DIMH/DIMV, or plain tool names with one point per item.")
            if issues:
                msg += (f" LAYOUT ({len(issues)}): " + "; ".join(issues[:12]) +
                        f". Sheet-local limits: drawing area x {_cad.FRAME_X1}..{_cad.FRAME_X2}, y {_cad.FRAME_Y1}..{_cad.FRAME_Y2}; "
                        f"the title block fills x>{_cad.TB_X1}, y<{_cad.TB_Y2}. TEXT|x,y is the LOWER LEFT corner and a hanzi "
                        f"is about one text height wide; use TEXTC|x,y|h|... to centre a label on x,y. Move views/labels, "
                        f"shorten or split long labels, and keep text clear of other text.")
            return msg
        for c in raw:
            mf = re.match(r"(?i)^\s*frame\s*\|\s*(\d+)", str(c))
            if mf:
                self._cad_frames.add(int(mf.group(1)))
        cmds = cmds[:CAD_MAX_EXPANDED]
        r, res, ok, out = await self._type_cad(cmds, open_path, intent)
        resumed = ""
        landed = len(cmds) if ok else 0
        if not ok:
            landed = await self._cad_progress() or 0
        if not ok and res.get("timed_out"):
            # the batch was cut off: find how far it got and type the rest once, instead of the model resending
            # everything (which drew every finished line twice)
            done = landed
            if done < len(cmds):
                start = _cad.resume_index(cmds, done)
                landed = start
                rest = (["ESC"] if start else []) + cmds[start:]
                r, res, ok, out = await self._type_cad(rest, None, intent)
                if ok:
                    landed = len(cmds)
                else:
                    p2 = await self._cad_progress() or 0
                    landed = max(start, start + p2 - (1 if start else 0))
                resumed = f" (cut off after {done} items; resumed automatically from item {start})"
        if "SAVE REFUSED" in out:  # LibreCAD came back on an empty drawing: nothing unsaved survived
            self._cad_keys_unsaved.clear()
            landed = 0
        for k in _cad.landed_keys(raw, keys, sheet_before, landed):
            self._cad_keys_unsaved.add(k)
        if ok and re.search(r"saved \S", out):
            self._cad_keys_saved |= self._cad_keys_unsaved
            self._cad_keys_unsaved.clear()
        self._last_ok_cad = ok
        m = re.search(r"AIW_SHEETS (\{.*\})", out)
        if m:
            try:
                self._sheet_audit = json.loads(m.group(1))
                self._cad_pdf_ok = True
                # a sheet framed in this task but absent from the PDF is unfinished too (an empty save passed before)
                have = {int(k) for k in (self._sheet_audit.get("counts") or {})}
                thin = {int(n) for n in self._sheet_audit.get("thin") or []}
                thin |= {n for n in self._cad_frames if n not in have}
                if self._cad_mode == "revise":  # a revision answers for what it touched, not the whole old set
                    thin &= set(self._cad_frames) | set(self._cad_touched)
                self._sheet_audit["thin"] = sorted(thin)
            except ValueError:
                pass
        await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=ok, error=None if ok else out[-200:],
                        result={"commands": raw[:80], "count": len(raw), "typed": len(cmds)})
        if ok:
            self._acted = True
            self._fresh = None
            self._cad_ok += 1
            for sh, b, body in boxes:
                self._cad_text_boxes.setdefault(sh, []).append((b, body))
            self._cad_typed_texts.update(c for c in cmds if _cad._parse_text(c))
            self._cad_rejects[key] = 0
        more = (f" (only the first {CAD_MAX_BATCH} were typed; send the rest next)" if len(raw) > CAD_MAX_BATCH else "")
        if dup or already:
            more += (f" ({already + len(dup)} items already drawn earlier in this task were skipped, so nothing is drawn twice;"
                     f" never resend finished sheets)")
        if issues:
            more += f" (typed although the layout check still finds: {'; '.join(issues[:6])})"
        if getattr(self, "_cad_fixed", None):
            more += f" (layout auto-fixed {len(self._cad_fixed)} items: {'; '.join(self._cad_fixed[:5])})"
        audit = ""
        if m and self._sheet_audit.get("thin"):
            audit = (f" THIN SHEETS {self._sheet_audit['thin']} (entities per sheet {self._sheet_audit.get('counts')}): "
                     f"those sheets are not finished; draw their missing content, save and export again.")
            if self._sheet_audit.get("no_geometry"):
                audit += (f" Sheets {self._sheet_audit['no_geometry']} have texts but almost no lines/rectangles "
                          f"(geometry per sheet {self._sheet_audit.get('geometry')}): draw the walls, outlines and panels.")
        if m and self._sheet_audit.get("stray"):
            audit += (f" STRAY ENTITIES per sheet {self._sheet_audit['stray']}: they reach outside their sheet and were "
                      f"left out of the PDF; undo/redraw them inside the sheet.")
        if moved:
            more += (f" (sheet {self._cad_sheet}: {moved} sheet-local points were placed at origin x="
                     f"{self._cad_sheet * _cad.SHEET_PITCH}; keep using local or absolute coordinates, both land right)")
        more = (" " + edit_note if edit_note else "") + more
        if ok:
            return (f"typed {len(cmds)} commands into LibreCAD{resumed}{more}: {out.strip()[-600:]}.{audit} Continue straight on "
                    f"with the next batch from cad/plan.md; no screenshot or Escape needed.")
        return (f"FAILED{resumed}: {out[-400:]}. The next cad_command closes any leftover dialog and leaves the running tool "
                f"by itself, so do not press Escape or take screenshots: resend only the commands that did not land.")

    async def _cad_edits(self, edits: list[dict], intent: str):
        """ERASE / ERASETEXT / LIST on the saved DXF. Unsaved strokes are saved first, so nothing typed is lost;
        after an erase LibreCAD is reopened on the file. Returns (note for the model, reopen?) or 'FAILED ...'."""
        if not self._cad_file:
            return ("FAILED: ERASE/LIST work on the saved drawing, and this task has not saved one yet. "
                    "SAVE|cad/<name>.dxf first.")
        if any(e["op"] != "list" for e in edits) or self._cad_keys_unsaved:
            r0, _, ok0, out0 = await self._type_cad([f"SAVE|{self._cad_file}"], None, "save before editing")
            if not ok0:
                return f"FAILED: could not save before editing: {out0[-300:]}"
            self._cad_keys_saved |= self._cad_keys_unsaved
            self._cad_keys_unsaved.clear()
        r = await self.tools.run("shell", {"command": _cad.edit_command(self._cad_file, edits), "timeout": 120}, intent=intent)
        res = r.result if isinstance(r.result, dict) else {}
        info = _cad.parse_edit(res.get("stdout") or "")
        if not info:
            err = ((res.get("stderr") or "") + (r.error or ""))[-400:]
            if "ezdxf" in err:
                err += " (install it: shell `sudo apt-get install -y python3-ezdxf`)"
            await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=False, error=err[-200:], result={"edits": edits})
            return f"FAILED: editing {self._cad_file} did not work: {err}"
        erased = int(info.get("erased") or 0)
        self._cad_touched |= {e.get("sheet") for e in edits if e["op"] != "list"}
        if erased:
            # what was erased may be drawn again: forget the skip keys and the text memory of the touched sheets
            self._cad_keys_saved.clear()
            self._cad_keys_unsaved.clear()
            self._cad_typed_texts.clear()
            for sh in {e.get("sheet") for e in edits if e["op"] != "list"}:
                self._cad_text_boxes.pop(sh, None)
            try:
                r2 = await self.tools.run("shell", {"command": _cad.edit_command(self._cad_file, [], texts=True), "timeout": 60},
                                          intent="re-read the texts after erasing")
                i2 = _cad.parse_edit(((r2.result or {}).get("stdout") or "") if isinstance(r2.result, dict) else "")
                if i2:
                    self._cad_text_boxes = _cad.seed_text_boxes(i2.get("texts") or [])
            except Exception:  # noqa: BLE001
                pass
        listed = info.get("list") or []
        await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=True, error=None,
                        result={"edits": edits, "erased": erased, "listed": len(listed)})
        note = ""
        if any(e["op"] != "list" for e in edits):
            note += f"erased {erased} entities from {self._cad_file} (LibreCAD reopened on it)."
            if not erased:
                note += " Nothing lay fully inside the window(s): LIST the spot and use a window that encloses the items."
        if listed:
            note += " LIST (sheet-local x,y of each entity's box):\n" + "\n".join(listed[:150])
        elif any(e["op"] == "list" for e in edits):
            note += " LIST: nothing inside that window."
        return note.strip(), erased > 0

    async def _type_cad(self, cmds: list[str], open_path, intent: str):
        cmd = _cad.librecad_type_command(cmds, open_path, getattr(self, "_cad_sheet", None))
        texts = sum(1 for c in cmds if _cad._parse_text(c))
        pdf = any(c.lower().startswith("pdf|") for c in cmds)
        # measured on the VM (2026-10-18): ~0.5-1 s per typed item and ~3 s per text once the drawing is big
        # 2026-10-20 VM run: ~6.5 s per text and ~1.2 s per other item; 4 s per text cut 4 batches off mid-way
        budget = int(min(1800, 60 + 1.5 * len(cmds) + 9 * texts + (300 if pdf else 0)))
        r = await self.tools.run("shell", {"command": cmd, "timeout": budget}, intent=intent)
        res = r.result if isinstance(r.result, dict) else {}
        ok = r.success and (res.get("exit_code", 0) == 0)
        out = ((res.get("stdout") or "") + (res.get("stderr") or "") or (r.error or ""))[-1500:]
        return r, res, ok, out

    async def _cad_progress(self):
        r = await self.tools.run("shell", {"command": 'cat "$HOME/.cache/aiw-cad-progress" 2>/dev/null', "timeout": 15},
                                 intent="read LibreCAD batch progress")
        res = r.result if isinstance(r.result, dict) else {}
        try:
            return int((res.get("stdout") or "").strip().split()[-1])
        except (ValueError, IndexError):
            return None

    async def _cad_draw(self, a: dict, intent: str) -> str:
        """Write the drawing body into a script with the CAD prelude, run it on the VM (ezdxf), open the DXF in
        LibreCAD on the desktop and deliver PDF + DXF to the chat."""
        code = a.get("code") or ""
        if not code.strip():
            await self.emit(EventType.ACTION_RESULT, tool="cad_draw", success=False, error="missing args ['code']", result={})
            return "FAILED: cad_draw needs `code`, the Python drawing body that uses the helpers (rect, wall, dim, text...)"
        name = _cad.safe_name(a.get("name") or "drawing")
        script = _cad.build_script(name, code, a.get("scale") or 50)
        rel = f"{_cad.CAD_DIR}/{name}_build.py"
        w = await self.tools.run("files_write", {"path": rel, "content": script}, intent=intent)
        if not w.success:
            await self.emit(EventType.ACTION_RESULT, tool="cad_draw", success=False, error=w.error, result={})
            return f"FAILED: could not write the script ({w.error})"
        r = await self.tools.run("shell", {"command": f"cd \"${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}/{_cad.CAD_DIR}\" && python3 {name}_build.py",
                                           "timeout": 180}, intent=intent)
        res = r.result if isinstance(r.result, dict) else {}
        info = _cad.parse_result(res.get("stdout") or "")
        if not info:
            err = (res.get("stderr") or r.error or "no output")[-1500:]
            if "No module named 'ezdxf'" in err or "No module named ezdxf" in err:
                err += "\n(ezdxf missing: run shell `sudo apt install -y python3-ezdxf python3-matplotlib fonts-wqy-microhei`)"
            await self.emit(EventType.ACTION_RESULT, tool="cad_draw", success=False, error=err[-300:], result={})
            return f"FAILED: the drawing script raised an error. Fix the body and call cad_draw again.\n{err}"
        self._acted = True
        if a.get("open", True):
            view = info.get("view_dxf") or info["dxf"]
            await self.tools.run("shell", {"command": f"cd \"${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}/{_cad.CAD_DIR}\" && pkill -x librecad; "
                                           f"(nohup librecad '{view}' >/dev/null 2>&1 &) ; sleep 2; echo ok",
                                           "timeout": 20}, intent=intent)
        sent = []
        if a.get("deliver", True):
            for key in ("pdf", "dxf"):
                if info.get(key):
                    f = await self.tools.run("send_file", {"path": f"{_cad.CAD_DIR}/{info[key]}"}, intent=intent)
                    if f.success and isinstance(f.result, dict):
                        await self._deliver_file(f.result)
                        sent.append(info[key])
        short = {k: info[k] for k in ("dxf", "pdf", "png", "extents_mm", "entities_by_layer", "render_error", "warning") if k in info}
        await self.emit(EventType.ACTION_RESULT, tool="cad_draw", success=True, error=None, result={**short, "sent": sent})
        tip = (f" Check it: read_image path={_cad.CAD_DIR}/{info['png']} with a question about missing or overlapping items."
               if info.get("png") else "")
        return (f"drawing saved: {json_short(short)}; sent to user: {sent or 'nothing'}; "
                f"opened in LibreCAD: {bool(a.get('open', True))}.{tip}")

    async def _read_image(self, a: dict) -> str:
        """read_image with paths=[...]: all images at once. Read one per step, the 8 design images of task-960 took
        7.5 min (about 50 s each on the vision model); in parallel they take about as long as the slowest one."""
        paths = [str(p) for p in (a.get("paths") or []) if str(p).strip()]
        if a.get("path") and str(a["path"]) not in paths:
            paths.insert(0, str(a["path"]))
        if len(paths) <= 1:
            return await self._read_image_one({**a, "path": paths[0] if paths else ""})
        sem = asyncio.Semaphore(int(os.environ.get("AIW_READ_IMAGE_PARALLEL", "4")))

        async def one(p):
            async with sem:
                return await self._read_image_one({**a, "path": p, "paths": None})
        outs = await asyncio.gather(*[one(p) for p in paths[:12]])
        return "\n\n".join(outs)

    async def _read_image_one(self, a: dict) -> str:
        """Read an image (or one PDF page) from ~/workspace and have the vision model describe it for drafting."""
        path = (a.get("path") or "").strip().lstrip("/")
        for pre in ("home/aiw/workspace/", "workspace/", "~/workspace/"):
            if path.startswith(pre):
                path = path[len(pre):]
        if not path:
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error="missing args ['path']", result={})
            return "FAILED: read_image needs `path` or `paths` (relative to ~/workspace)"
        if path.lower().endswith(".pdf"):
            page = int(a.get("page") or 1)
            stem = f".preview/{_cad.safe_name(Path(path).stem)}-p{page}"
            r = await self.tools.run("shell", {"command": f"cd \"${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}\" && mkdir -p .preview && pdftoppm -r 130 -png "
                                               f"-f {page} -l {page} -singlefile '{path}' '{stem}'", "timeout": 60})
            if not r.success:
                err = (r.result or {}).get("stderr") if isinstance(r.result, dict) else r.error
                return f"FAILED: could not render PDF page ({err}); is poppler-utils installed?"
            path = stem + ".png"
        q0 = (a.get("question") or "").strip()
        if path in self._images_read and not a.get("again"):
            st, txt = self._images_read[path]
            return (f"ALREADY READ at step {st} (not read again, it takes ~50 s). What it showed:\n{txt[:3500]}\n"
                    "Use this; pass again=true only if you need a different question answered.")
        f = await self.tools.run("send_file", {"path": path})   # binary read; not delivered to the chat
        if not f.success or not isinstance(f.result, dict) or not f.result.get("content_b64"):
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=f.error, result={})
            return f"FAILED: cannot read {path} ({f.error}). Use files_list to find the uploaded file."
        try:
            img = Image.open(io.BytesIO(base64.b64decode(f.result["content_b64"]))).convert("RGB")
            if max(img.size) > 2000:
                k = 2000 / max(img.size)
                img = img.resize((int(img.width * k), int(img.height * k)))
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception as e:  # noqa: BLE001
            return f"FAILED: {path} is not an image ({e})"
        eyes = self.vision_llm or self.llm
        q = (a.get("question") or "").strip()
        try:
            text = await asyncio.wait_for(eyes.describe(_cad.READ_IMAGE_PROMPT, q or "请按结构描述这张图。", b64, 1800),
                                          self.cfg.vision_timeout_s * 3)
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=str(e)[:200], result={})
            return f"FAILED: the vision model could not read the image ({type(e).__name__})"
        self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="read_image", success=True, error=None,
                        result={"path": path, "chars": len(text or "")})
        self._images_read[path] = (self.step, (text or "").strip())
        return f"image {path} reads:\n{(text or '').strip()[:3500]}"

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
                out = f" exit={o.result.get('exit_code')} out={_clip(str(o.result.get('stdout') or ''), 2000)!r} err={str(o.result.get('stderr',''))[:200]!r}"
            elif o.action == "files.read" and d.tool == "files_read" and isinstance(o.result, dict):
                out = " content:\n" + _read_excerpt(o.result.get("content") or "", 4000)
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


def _clip(text: str, limit: int) -> str:
    """Head and tail of long command output, so the model sees both what started and how it ended."""
    return text if len(text) <= limit else text[: limit * 3 // 4] + f" ...[{len(text) - limit} chars cut]... " + text[-limit // 4:]


def _read_excerpt(text: str, limit: int = 6000) -> str:
    """File content the model sees: whole file when short, head and tail when long."""
    if len(text) <= limit:
        return text
    return text[: limit * 2 // 3] + f"\n... [{len(text) - limit} chars omitted] ...\n" + text[-limit // 3:]


def _hist_line(step, decision, outcome, vmsg) -> str:
    args = {k: (v[:80] if isinstance(v, str) else v) for k, v in decision.args.items()}
    res = ""
    if outcome.action == "shell.exec" and outcome.result:
        res = (f" exit={outcome.result.get('exit_code')} out={_clip(outcome.result.get('stdout') or '', 6000)!r}"
               + (f" err={outcome.result.get('stderr','')[-300:]!r}" if outcome.result.get("stderr") else ""))
    elif outcome.action == "files.write" and outcome.success and isinstance(outcome.result, dict):
        res = f" saved {outcome.result.get('path')} ({outcome.result.get('size')} bytes); next: run or use it"
    elif outcome.action == "files.read" and outcome.success and isinstance(outcome.result, dict) and decision.tool == "files_read":
        res = " content:\n" + _read_excerpt(outcome.result.get("content") or "") + "\n(end of file content; do not read it again)"
    elif outcome.action == "files.list" and outcome.success and isinstance(outcome.result, dict):
        ents = outcome.result.get("entries") or []
        res = " entries=" + ", ".join((e.get("name", "") + ("/" if e.get("dir") else "")) for e in ents[:60])
    status = "ok" if outcome.success else f"FAILED ({outcome.error})"
    return f"{step}. {decision.tool}({args}) -> {status}{res}; {vmsg}"


_ACTION_WORDS = ("打开", "搜索", "输入", "点击", "新建", "创建", "运行", "执行", "下载", "访问", "登录", "安装", "写入", "删除",
                 "open", "search", "type", "click", "create", "run", "download", "visit", "install", "write", "go to")


_QUESTION = re.compile(r"^(为什么|为啥|怎么回事|怎么没|是什么|什么原因|why|what|how come)|[?？吗呢]\s*$", re.I)


def _is_question(prompt: str) -> bool:
    p = (prompt or "").strip()
    return bool(_QUESTION.search(p)) and len(p) < 60 and not re.search(r"帮我|请你|给我|发给|please", p, re.I)


def _needs_action(prompt: str) -> bool:
    p = (prompt or "").lower()
    if _is_question(p):
        return False
    return any(w in p for w in _ACTION_WORDS)


CAD_MAX_BATCH = 200
CAD_MAX_EXPANDED = 600
_NO_SCREEN_TOOLS = {"read_image", "shell", "files_read", "files_write", "files_list", "files_download", "send_file", "delegate"}
_NO_REPEAT = {"files_write", "shell", "browser_open", "send_file", "files_download", "delegate", "open_app"}


def _thumb(b64: str):
    try:
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("L").resize((64, 40)).tobytes()
    except Exception:  # noqa: BLE001
        return None


def json_short(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)[:600]
