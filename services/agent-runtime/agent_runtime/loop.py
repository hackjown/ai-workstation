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
from . import image_edit as _ie
from . import image_brief as _ib
from . import photopea as _pp

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
                 vision_llm: Optional[LLMRouter] = None, image_cfg: Optional[dict] = None):
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
        self._image_task = bool(_IMAGE_TASK.search(task.prompt or "")) and not self._cad_task
        if self._image_task and config is None:  # each AI edit takes ~70 s and a check ~30 s (task-4e2 ran out at 900 s)
            self.cfg.timeout_s = max(self.cfg.timeout_s, float(os.environ.get("AIW_IMAGE_TIMEOUT", "2400")))
        self._video_task = bool(_VIDEO_TASK.search(task.prompt or "")) and not self._cad_task
        if self._video_task and config is None:  # task-247: reading a 16-page deck alone took 15 min
            self.cfg.max_steps = max(self.cfg.max_steps, int(os.environ.get("AIW_VIDEO_MAX_STEPS", "200")))
            self.cfg.timeout_s = max(self.cfg.timeout_s, float(os.environ.get("AIW_VIDEO_TIMEOUT", "3600")))
        self._waited = 0.0  # seconds spent waiting on the user (ask_user, confirmations): not counted toward timeout
        self._last_image: Optional[str] = None  # newest image_edit output, delivered if the task runs out of time
        self.validator = Validator()
        self._cad_keys_saved: set = set()
        self._cad_touched: set = set()
        self._cad_keys_unsaved: set = set()
        self.recovery = Recovery(computer)
        self._emit_fn = emit
        self.history: list[str] = []
        if self._video_task and re.search(r"huashu-motion|\bom\s", task.prompt or ""):
            self.history.append(
                "0. (setup) huashu-motion needs Node 22+, Chrome/Chromium, FFmpeg (+ uv/Kokoro for a Chinese voice). "
                "First run `~/.claude/skills/huashu-motion/bin/om doctor`; if anything is missing run "
                "`bash ~/ai-workstation/deploy/vm/motion-setup.sh` (or the copy in the repo) and check again. Ask the "
                "director's questions in ONE ask_user as soon as you know the material, not after reading everything.")
        self._images_read: dict[str, tuple] = {}  # path -> (step, description, question): history keeps only ~25 steps
        self.step = 0
        self.ask, self.remember = ask, remember
        self.extra_tools = extra_tools or {}
        self.subagent_llm = subagent_llm
        self.vision_llm = vision_llm  # optional "eyes": reads the screen for a tool model without vision
        self.image_cfg = image_cfg or {}  # {"base_url", "key", "model"}: the AI image-editing model, if set
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

    def add_wait(self, seconds: float) -> None:
        """Time spent waiting on the user extends the deadline (task-247 lost 10 of its 15 min to two unanswered
        confirmations, then timed out 6 s after asking a question)."""
        self._waited += max(0.0, float(seconds or 0))

    async def _run_with_deadline(self) -> None:
        job = asyncio.ensure_future(self._run())
        start = time.monotonic()
        try:
            while True:
                left = self.cfg.timeout_s + self._waited - (time.monotonic() - start)
                if left <= 0:
                    raise asyncio.TimeoutError
                done, _ = await asyncio.wait({job}, timeout=left)
                if done:
                    return job.result()
        finally:
            if not job.done():
                job.cancel()
                try:
                    await job
                except BaseException:  # noqa: BLE001
                    pass

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
            await self._run_with_deadline()
        except asyncio.TimeoutError:
            msg = f"timeout after {self.cfg.timeout_s:.0f}s"
            if self._last_image and not getattr(self, "_sent_file", None):
                try:  # hand over what exists instead of nothing (task-484: the one try hit a dead tunnel)
                    for _i in range(3):
                        f = await asyncio.wait_for(self.tools.run("send_file", {"path": self._last_image}), 120)
                        if f.success or not _transient(f.error):
                            break
                        await asyncio.sleep(5 * (_i + 1))
                    if f.success and isinstance(f.result, dict):
                        await self._deliver_file(f.result)
                        msg += f"；时间用完前的最新一版结果已发给你（{self._last_image}），可能还没完全达到要求"
                except Exception:  # noqa: BLE001
                    pass
            self._finish(TaskStatus.FAILED, msg)
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
            "fully inside the window: check the ERASED list it returns) or 'ERASETEXT|exact whole text|x1,y1|x2,y2' (one "
            "label; the window is needed when the text occurs more than once), then the corrected items]. Labels, walls "
            "and index letters the user did not name stay as they are: if a new line clashes with one, move your item or "
            "shift that label slightly, never delete it. Then SAVE to the same file, PDF, send_file both, and done "
            "listing each change.")

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
        self._cad_rejects: dict = {}  # sheet -> batches sent back by the layout check (typed anyway after AIW_CAD_MAX_REJECTS=2; a resend costs ~8 s with thinking off)
        # every one-line item (line|, rect|, TEXT|, DIMH|, FRAME|n ...) that landed, in its absolute form: a batch sent
        # again after a timeout, a dialog error or a reopen skips what is already there instead of drawing it twice
        self._cad_keys_saved: set = set()    # landed and saved in the DXF
        self._cad_keys_unsaved: set = set()  # landed since the last save (lost if LibreCAD restarts)
        self._prev_sig: Optional[tuple] = None
        self._rep = 0
        self._last_tool: Optional[str] = None
        self._last_shown: tuple[int, int] = (0, 0)
        note: Optional[str] = None
        if self._image_task:
            try:
                note = await self._image_kickoff()
            except (StopRequested, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001  the agent can still do it the long way
                await self.emit(EventType.ERROR, error=f"自动修图首轮失败，改由模型逐步处理：{type(e).__name__}: {str(e)[:160]}")
                note = None
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
                # Before the first sheet too (AIW_CAD_PLAN_THINKING): run e7d's planning step thought for 809 s and
                # returned no tool call; the brief carries the coordinates, so planning is transcription as well.
                thinking=((os.environ.get("AIW_CAD_DRAW_THINKING", "off") if getattr(self, "_cad_calls", 0)
                           else os.environ.get("AIW_CAD_PLAN_THINKING", "off")) if self._cad_task
                          # an image edit is measure -> move/edit -> compare -> send; task-484 thought 50-70 s per
                          # step and then 540 s on one step (900 s budget gone with the result already made at 175 s)
                          else os.environ.get("AIW_IMAGE_THINKING", "off") if self._image_task else None))
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
            if (self._image_task and self._last_image and not self._sent_file
                    and decision.tool not in ("image_edit", "send_file", "done", "fail", "photopea")):
                self._since_edit = getattr(self, "_since_edit", 0) + 1
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
                             and _transient(outcome.error))
                if not retryable:
                    break
                await self.emit(EventType.RECOVERY, attempt=attempt + 1, note=f"transient error, retrying: {outcome.error[:120]}")
                await asyncio.sleep(1.5 * (attempt + 1))
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
            if decision.tool == "image_edit" and outcome.success:
                self._since_edit = 0
            elif self._image_task and getattr(self, "_since_edit", 0) >= int(os.environ.get("AIW_IMAGE_CHECK_STEPS", "3")):
                # task-484: a deterministic move was made at 175 s, then three more measuring scripts (60-70 s of
                # thinking each) ran until the 900 s budget was gone and the user got nothing
                note = (f"You have spent {self._since_edit} steps checking {self._last_image} without editing it. Decide "
                        f"NOW: if it does what the user asked, send_file {self._last_image}; if a number is off, run "
                        "image_edit once more with corrected pixels. Do not measure again.")

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
            t0 = time.monotonic()
            reply = await self.ask(q, opts) if self.ask else None
            self.add_wait(time.monotonic() - t0)
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
        if t == "image_edit":
            return await self._image_edit(a)
        if t == "photopea":
            return await self._photopea(a, decision.thought)
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
        if (bad or issues) and self._cad_rejects.get(key, 0) < int(os.environ.get("AIW_CAD_MAX_REJECTS", "2")):
            # checked before typing: a misplaced or unreadable batch is cheaper to fix in the plan than on the canvas
            self._cad_rejects[key] = self._cad_rejects.get(key, 0) + 1
            self._cad_sheet = sheet_before
            await self.emit(EventType.ACTION_RESULT, tool="cad_command", success=False, error="layout check",
                            result={"commands": raw[:80], "count": len(raw), "bad": bad[:10], "issues": issues[:20]})
            msg = "NOT TYPED, nothing was drawn: fix these and send the whole batch again."
            if edits:  # task-2c3: the erase had run, the model believed it had not and erased again further out
                msg = ("The ERASE/ERASETEXT part of this batch WAS applied (" + edit_note.strip() + "); do not send it again. "
                       "The drawing part was NOT TYPED: fix these and send only the drawing commands again.")
            if self._cad_mode == "revise" and issues:
                msg += (" This is a revision: a label the user did not name that now clashes with your new lines stays on the "
                        "drawing. Move your new item, or move that label a little (ERASETEXT|exact text|x1,y1|x2,y2, then "
                        "the same text nearby); never delete it.")
            if fixed:
                msg += f" (already auto-fixed and kept in the batch: {len(fixed)} items; fix only what is listed here)"
            if bad:
                msg += (f" UNKNOWN COMMANDS {bad[:6]}: use 'line|x1,y1|x2,y2|...', 'rect|x1,y1|x2,y2', 'circle|x,y|r', "
                        f"'arc|cx,cy|r|a1|a2', TEXT/TEXTC/NOTE/LEADER/DIMH/DIMV, or plain tool names with one point per item.")
            if issues:
                msg += (f" LAYOUT ({len(issues)}): " + "; ".join(issues[:12]) +
                        f". Sheet-local limits: drawing area x {_cad.FRAME_X1}..{_cad.FRAME_X2}, y {_cad.FRAME_Y1}..{_cad.FRAME_Y2}; "
                        f"the title block fills x>{_cad.TB_X1}, y<{_cad.TB_Y2}. TEXT|x,y is the LOWER LEFT corner and a hanzi "
                        f"is about 1.25 text heights wide (digits 0.75); use TEXTC|x,y|h|... to centre a label on x,y. Move views/labels, "
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
            at = self._sheet_audit.get("stray_at") or {}
            audit += (f" STRAY ENTITIES per sheet {self._sheet_audit['stray']}: they reach outside their sheet and were "
                      f"left out of the PDF. Where (sheet-local): {json.dumps(at, ensure_ascii=False)[:600]}. Remove each "
                      f"with ONE 'SHEET|n' + 'ERASE|x1,y1|x2,y2' around those coordinates (or ERASETEXT|text), redraw it "
                      f"inside x {_cad.FRAME_X1}..{_cad.FRAME_X2}, then export again; no LIST needed.")
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
            gone = info.get("gone") or []
            if gone:  # task-2c3: a wide window took the 形象墙 polyline and labels along with the door, unnoticed
                note += (" ERASED: " + "; ".join(gone[:25]) + (f"; ... {len(gone) - 25} more" if len(gone) > 25 else "") +
                         ". If any of these is not part of what the user asked to change, draw it again exactly as it was.")
            for amb in info.get("ambiguous") or []:
                note += (f" ERASETEXT|{amb['text']} matched {len(amb['at'])} texts on the sheet (at {', '.join(amb['at'])}); "
                         f"nothing was erased. Name the one to remove with a window: ERASETEXT|{amb['text']}|x1,y1|x2,y2.")
            if not erased and not info.get("ambiguous"):
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

    async def _image_kickoff(self) -> Optional[str]:
        """A casual edit request ("把白色柜子加高到和左侧台面齐平") -> an explicit brief written by the vision model
        while it looks at the photo -> the first AI edit, made here. The agent then only checks and fixes.
        task-55a without it: 8 read_image + 8 measuring scripts, then a stretch of the doors delivered as done."""
        if os.environ.get("AIW_IMAGE_AUTOSTART", "on").lower() in ("0", "off", "false"):
            return None
        paths = _ib.image_paths(self.task.prompt)
        if len(paths) != 1 or not _ib.wants_edit(self.task.prompt):
            return None
        if not (self.image_cfg.get("model") and self.image_cfg.get("key")):
            return None
        src = paths[0]
        instr = _ib.instruction(self.task.prompt)
        self.step += 1
        await self.emit(EventType.DECISION, thought="先看图，把要求翻译成明确的修图指令（改什么、哪些跟着动、哪些不动）",
                        tool="read_image", args={"path": src, "question": "理解修图要求"})
        f = await self.tools.run("send_file", {"path": src})
        if not f.success or not isinstance(f.result, dict) or not f.result.get("content_b64"):
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=f.error, result={})
            return None
        img = Image.open(io.BytesIO(base64.b64decode(f.result["content_b64"]))).convert("RGB")
        w, h = img.size
        g = _with_grid(img)
        if max(g.size) > 2000:
            k = 2000 / max(g.size)
            g = g.resize((int(g.width * k), int(g.height * k)))
        buf = io.BytesIO(); g.save(buf, "JPEG", quality=88)
        eyes = self.vision_llm or self.llm
        text = await _retry_once(lambda: asyncio.wait_for(
            eyes.describe(_ib.BRIEF_SYSTEM, _ib.brief_user(instr, w, h), base64.b64encode(buf.getvalue()).decode(), 1500),
            self.cfg.vision_timeout_s * 3))
        brief = _ib.parse(text, w, h)
        if not brief:
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error="no usable edit brief", result={})
            return None
        if _ib.wants_raise(instr):
            # task-d2d: the brief left geometry out. task-3f3: pixel numbers read off a grid were wrong (900/850).
            # Ask for 0-1000 grounding boxes on the plain picture, the format these models are trained on.
            plain = img if max(img.size) <= 2000 else img.resize((int(w * 2000 / max(w, h)), int(h * 2000 / max(w, h))))
            pb = io.BytesIO(); plain.save(pb, "JPEG", quality=90)
            geyes = getattr(self, "geometry_llm", None) or eyes
            try:
                gtext = await _retry_once(lambda: asyncio.wait_for(
                    geyes.describe("你是目标定位模型，只输出 JSON。", _ib.grounding_user(instr, brief),
                                   base64.b64encode(pb.getvalue()).decode(), 400), self.cfg.vision_timeout_s * 2))
                brief["geometry"] = (_ib.parse_grounding(gtext, w, h) or _ib.parse_geometry(gtext, w, h)
                                     or brief.get("geometry"))
                brief["geometry_raw"] = (gtext or "")[:300]
            except Exception as e:  # noqa: BLE001
                brief["geometry_raw"] = f"error: {e}"[:300]
        await self.emit(EventType.ACTION_RESULT, tool="read_image", success=True, error=None,
                        result={"path": src, "brief": _ib.summary(brief)[:300], "raw": (text or "")[:1500],
                                "geometry": brief.get("geometry"), "geometry_raw": brief.get("geometry_raw")})
        self.history.append(f"{self.step}. read_image {src} -> edit brief: {_ib.summary(brief)[:600]}")
        if brief["question"]:
            return (f"The request can be read two ways ({brief['question']}). Call ask_user with that question "
                    f"(in Chinese) before editing. Edit brief so far: {_ib.summary(brief)}")
        save = str(Path(src).with_name(Path(src).stem + "-修改.png"))
        args = {"path": src, "prompt": _ib.full_prompt(brief), "save": save}
        keep = brief["keep_text_boxes"] + brief["front_boxes"]
        if keep:
            args["restore"] = keep
        how = "按修图指令让 AI 改图：" + _ib.summary(brief)[:200]
        measured = ""
        g = brief.get("geometry")
        if g:  # raise/lower to a reference: image models do this only roughly, so measure and move exactly
            dy = await self._measure_raise(img, f.result["content_b64"], brief, g)
            up, down = re.search(r"加高|抬高|升高|变高|调高", instr), re.search(r"降低|变矮|压低|调低", instr)
            if dy is not None and ((up and dy < 0) or (down and dy > 0) or g["top_y"] > g["front_top_y"] - 40):
                # task-3f3: the vision model read both edges as y=900 and top_y=850, so "加高" became a 13 px lowering
                # that left the coffee machine behind. A measurement that contradicts the request is not used.
                return (f"Measuring failed: the vision model's numbers contradict the request (target edge y="
                        f"{g['front_top_y']}, reference y={g['target_y']}, things on top from y={g['top_y']}). Do NOT "
                        "edit by guesswork and do not deliver an unchanged picture. ask_user in Chinese: say the "
                        "automatic measurement was unreliable and ask how much higher it should go (e.g. 约几厘米, or "
                        "'和左边台面上沿对齐') so you can move it exactly with image_edit move.")
            if dy is not None and abs(dy) < 3:
                return (f"Measured: the target edge is already level with the reference (y={g['front_top_y']} vs "
                        f"{g['target_y']}, within 3 px). Tell the user it is already level, with those numbers, and "
                        "ask whether they meant something else; do not edit.")
            if dy is not None:
                mv = _ib.plan_moves(g, dy)
                args = {"path": src, "save": save, "move": mv, "prompt": _ib.blend_prompt(brief, dy)}
                # a restore box over the moved cabinet would paste the old cabinet back (task-d2d's [650,720,1000,999]);
                # keep only boxes clear of it, or things standing in front of it on the floor
                keep = [b for b in keep if b[3] >= g["base_y"] - 20 or not any(
                    b[0] < m["to"][2] and b[2] > m["to"][0] and b[1] < m["to"][3] and b[3] > m["to"][1] for m in mv)]
                if keep:
                    args["restore"] = keep
                self._raise_lock = src
                measured = (f"Measured: target counter front edge y={g['front_top_y']}, reference y={g['target_y']} -> "
                            f"{'raised' if dy > 0 else 'lowered'} by {abs(dy)} px exactly with move (counter and "
                            "everything on it shifted, doors stretched, base kept), the AI only blended the seams.")
                how = (f"量好了：{brief['target'] or '目标'}台面前沿 y={g['front_top_y']}，{brief['reference'] or '参照'} "
                       f"y={g['target_y']}，{'上移' if dy > 0 else '下移'} {abs(dy)} 像素（台面和上面的东西一起动，柜门拉伸，"
                       "踢脚线不动），AI 只修接缝")
        if not measured and _ib.wants_raise(instr):
            how = ("没能量出两处台面的位置，只能让 AI 直接改（AI 对精确对齐不可靠，结果要严格核对）：" + _ib.summary(brief)[:160])
        self.step += 1
        await self.emit(EventType.DECISION, thought=how, tool="image_edit", args=args)
        await self.emit(EventType.ACTION, tool="image_edit", args=args)
        res = await self._image_edit(args)
        self.history.append(f"{self.step}. image_edit {src} -> {res[:400]}")
        self._last_tool = "image_edit"
        if not res.startswith("image_edit OK"):
            return (f"An automatic first AI edit was tried and failed: {res[:400]}\nEdit brief: {_ib.summary(brief)}\n"
                    f"Image-model prompt used: {args['prompt']}\nFix the cause or try again; do not switch to move for this.")
        check = brief["check"] or "目标物体是否按要求改好，跟着动的东西是否一起动了，其他地方是否保持原样"
        if measured:
            check = "接缝处有没有错位、重复残影或断开的线条？海报和文字是否完好？"
        head = ("The user's request was turned into this edit brief and the first edit is ALREADY DONE (do not "
                f"start over, do not measure with scripts).\n{measured}\nEdit brief: {_ib.summary(brief)}\n"
                f"Edit args used: {json.dumps(args, ensure_ascii=False)}\nResult: {res}\n"
                f"Now: read_image paths=[{src}, {save}] compare=true question=\"{check}；有没有文字乱码、物体变形？\". ")
        if measured:
            return head + (f"The raise itself is exact, never redo it with a prompt alone. If a seam looks bad, run "
                           f"image_edit with the SAME path, move and restore as above and a prompt naming that seam "
                           f"(the AI is only used along the seams). If the poster or text is damaged, image_edit "
                           f"path={save} original={src} restore=[[l,t,r,b]]. Then send_file {save} and done; in done say "
                           "in Chinese what moved and by how many pixels.")
        return head + (f"If the check confirms the change, send_file {save} and done (say in Chinese what changed). "
                       f"If it is wrong or too weak, run image_edit again with path={src} (the ORIGINAL), a sharper "
                       "prompt that says exactly what was wrong, the same save and restore boxes. Garbled text only: "
                       f"image_edit path={save} original={src} restore=[[l,t,r,b]].")

    async def _measure_raise(self, img, b64: str, brief: dict, g: dict) -> Optional[int]:
        """dy (> 0 raise) from a zoomed crop of where the two counters meet, sharpened to the pixel row; None if the
        measurement is unusable. Updates g in place."""
        crop, _box = _ib.measure_crop(img, g)
        buf = io.BytesIO(); crop.convert("RGB").save(buf, "JPEG", quality=90)
        eyes = self.vision_llm or self.llm
        try:
            text = await _retry_once(lambda: asyncio.wait_for(
                eyes.describe("你是精确读图的测量员，只输出 JSON。", _ib.measure_question(brief),
                              base64.b64encode(buf.getvalue()).decode(), 200), self.cfg.vision_timeout_s * 2))
            m = _ib.parse_measure(text)
        except Exception:  # noqa: BLE001
            m = None
        if m:
            ft, ty = m
            if abs(ft - g["front_top_y"]) < 90 and abs(ty - g["target_y"]) < 90:
                g["front_top_y"], g["target_y"] = ft, ty
        raw = base64.b64decode(b64)
        x0 = g["probe_x"] + 15 if g["probe_x"] < (g["x_left"] + g["x_right"]) / 2 else g["probe_x"] - 200
        g["front_top_y"] = await asyncio.to_thread(_ie.edge_row, raw, x0, x0 + 185, g["front_top_y"])
        dy = g["front_top_y"] - g["target_y"]
        if abs(dy) > 0.2 * img.height or g["top_y"] - dy < 0:
            return None
        return dy

    async def _image_edit(self, a: dict) -> str:
        """AI image edit through the gateway's image model, then size restore + pasting original regions back."""
        def rel(p):
            p = str(p or "").strip().lstrip("/")
            for pre in ("home/aiw/workspace/", "workspace/", "~/workspace/"):
                if p.startswith(pre):
                    p = p[len(pre):]
            return p
        src, prompt = rel(a.get("path")), str(a.get("prompt") or "").strip()
        boxes = a.get("restore") or []
        moves = a.get("move") or []
        if isinstance(moves, dict):
            moves = [moves]
        cfg = self.image_cfg
        err = None
        if moves and (not isinstance(moves, list) or any(not isinstance(m, dict) or len(m.get("box") or []) != 4
                                                          or len(m.get("to") or []) != 4 for m in moves)):
            err = 'move must be {"box": [l,t,r,b], "to": [l,t,r,b]} (or a list of them) in the image\'s pixels'
        elif prompt and (not cfg.get("model") or not cfg.get("key")):
            err = ("no AI image model is set up: the user picks one under 设置 → 模型 → 改图模型 "
                   "(e.g. gpt-image-1 or gemini-2.5-flash-image). Tell the user; Photopea can still do exact edits")
        elif not src:
            err = "image_edit needs `path` (the image, relative to ~/workspace)"
        elif not prompt and not boxes and not moves:
            err = "image_edit needs `prompt` (the change, in plain words), `move` or `restore` boxes"
        elif not isinstance(boxes, list) or any(not isinstance(b, (list, tuple)) or len(b) != 4 for b in boxes):
            err = "restore must be a list of [left, top, right, bottom] boxes in the ORIGINAL image's pixels"
        if not err and prompt and not moves and getattr(self, "_raise_lock", None) == src:
            err = ("the raise was measured and made exactly at the start; a prompt-only edit of the original lets the "
                   "image model redraw the cabinet where it was (task-d2d). Fix seams by repeating the first image_edit "
                   "(same move and restore, new prompt), or deliver the result")
        if err:
            await self.emit(EventType.ACTION_RESULT, tool="image_edit", success=False, error=err[:200], result={})
            return f"REFUSED: {err}"
        orig_path = rel(a.get("original")) or src
        save = rel(a.get("save")) or str(Path(src).with_name(Path(src).stem + "-ai.png"))
        if not save.lower().endswith(".png"):
            save = str(Path(save).with_suffix(".png"))
        f = await self.tools.run("send_file", {"path": src})
        if not f.success or not isinstance(f.result, dict) or not f.result.get("content_b64"):
            await self.emit(EventType.ACTION_RESULT, tool="image_edit", success=False, error=f.error, result={})
            return f"FAILED: cannot read {src} ({f.error})"
        src_bytes = base64.b64decode(f.result["content_b64"])
        orig_bytes = src_bytes
        if orig_path != src:  # restore from the untouched original while editing an earlier AI result
            g = await self.tools.run("send_file", {"path": orig_path})
            if g.success and isinstance(g.result, dict) and g.result.get("content_b64"):
                orig_bytes = base64.b64decode(g.result["content_b64"])
        try:
            out = src_bytes
            if moves:  # exact geometry first; an optional prompt then only blends the seams
                src_bytes, minfo = await asyncio.to_thread(_ie.move_regions, src_bytes, moves, int(a.get("feather") or 6))
                out = src_bytes
            if prompt:
                out = await _ie.edit(cfg.get("base_url") or "", cfg["key"], cfg["model"], src_bytes, _ie.prompt_for(prompt),
                                     float(os.environ.get("AIW_IMAGE_EDIT_TIMEOUT", "300")))
                if moves:  # the AI only blends: keep the exact move, take its redraw along the seams only
                    out = await asyncio.to_thread(_ie.blend_seams, src_bytes, out, minfo["moves"])
            png, info = await asyncio.to_thread(_ie.postprocess, orig_bytes, out, a.get("keep_size", True) is not False,
                                                boxes, int(a.get("feather") or 6))
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="image_edit", success=False, error=str(e)[:200], result={})
            return f"FAILED: AI image edit with {cfg['model']}: {str(e)[:600]}"
        up = None
        for _i in range(3):  # a 2 MB upload through the tunnel can hit a blip; the AI result must not be lost to it
            up = await self.computer.execute("files.upload", {"path": save, "content_b64": base64.b64encode(png).decode()},
                                             observe=False, timeout=120)
            if up.success or not _transient(up.error):
                break
            await asyncio.sleep(2 * (_i + 1))
        if not up.success:
            await self.emit(EventType.ACTION_RESULT, tool="image_edit", success=False, error=up.error, result={})
            return f"FAILED: could not save {save} on the computer ({up.error})"
        self._acted = True
        self._images_read.pop(save, None)
        self._last_image = save
        await self.emit(EventType.ACTION_RESULT, tool="image_edit", success=True, error=None,
                        result={"saved": save, **{k: v for k, v in info.items() if k != "orig_size"}})
        w, h = info["size"]
        if prompt:
            lines = [f"image_edit OK: saved {save} ({w}x{h} px; the model returned {info['ai_size'][0]}x{info['ai_size'][1]}"
                     f"{', scaled back to the original size' if info['ai_size'] != info['size'] else ''})."]
        else:
            lines = [f"image_edit OK (no AI): saved {save} ({w}x{h} px)."]
        if moves:
            lines.append("Moved exactly: " + "; ".join(f"{m['box']} -> {m['to']}" for m in minfo["moves"]) + ".")
        if info.get("align"):
            al = info["align"]
            lines.append(f"Restored {len(boxes)} region(s) from {orig_path}; the AI picture was offset by scale "
                         f"{al['scale']}, dx {al['dx']}, dy {al['dy']} px (residual {al['residual']}).")
        if info.get("changed") is not None:
            ch = info["changed"]
            lines.append(f"{ch}% of the picture differs from the original"
                         + (" -- ALMOST NOTHING CHANGED: the model ignored the request. Do not deliver this; run "
                            "image_edit again with a more concrete prompt (what moves where, by how much)."
                            if ch < 1.5 and prompt and not moves else "."))
        lines.append(f"Next: read_image paths=[{orig_path}, {save}] compare=true with a question about the change, to "
                     "check 1) the change is really there and goes the way the user asked and 2) garbled text or "
                     "logos, warped objects. Fix garbled regions with image_edit restore=[[l,t,r,b]] (pixels of the "
                     f"original, original={orig_path}, path={save}, no prompt), other details with photopea, then send_file.")
        return "\n".join(lines)

    async def _photopea(self, a: dict, intent: str) -> str:
        """Edit images in Photopea (a Photoshop clone in the browser) on the VM's desktop. The bridge script ships
        with this image and is written to the VM on first use, so neither VM needs a setup step."""
        job = _pp.build_job(a)
        err = _pp.check_job(job)
        if err:
            await self.emit(EventType.ACTION_RESULT, tool="photopea", success=False, error=err[:200], result={})
            return f"REFUSED: {err}"
        if not getattr(self, "_pp_installed", False):
            r = await self.tools.run("shell", {"command": _pp.install_command(), "timeout": 60},
                                     intent="install the Photopea bridge on this computer")
            out = ((r.result or {}).get("stdout") or "") if isinstance(r.result, dict) else ""
            if "PP_OK" not in out:
                e = ((r.result or {}).get("stderr") if isinstance(r.result, dict) else None) or r.error or out
                await self.emit(EventType.ACTION_RESULT, tool="photopea", success=False, error=str(e)[-200:], result={})
                return f"FAILED: could not install the Photopea bridge on the computer: {str(e)[-400:]}"
            self._pp_installed = True
        r = await self.tools.run("shell", {"command": _pp.run_command(job), "timeout": _pp.shell_timeout(job)},
                                 intent=intent or "edit the image in Photopea")
        res_raw = r.result if isinstance(r.result, dict) else {}
        res = _pp.parse_result(res_raw.get("stdout") or "")
        if res is None:
            e = ((res_raw.get("stderr") or "") + (r.error or ""))[-500:] or "no result from the bridge"
            await self.emit(EventType.ACTION_RESULT, tool="photopea", success=False, error=e[-200:], result={})
            return f"FAILED: photopea bridge: {e}"
        ok = bool(res.get("ok"))
        if ok:
            self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="photopea", success=ok, error=None if ok else str(res.get("error"))[:200],
                        result={"saved": _pp.saved_paths(res), "log": (res.get("echo") or [])[:5]})
        return _pp.summarize(res)

    async def _read_image(self, a: dict) -> str:
        """read_image with paths=[...]: all images at once. Read one per step, the 8 design images of task-960 took
        7.5 min (about 50 s each on the vision model); in parallel they take about as long as the slowest one."""
        paths = [str(p) for p in (a.get("paths") or []) if str(p).strip()]
        if a.get("path") and str(a["path"]) not in paths:
            paths.insert(0, str(a["path"]))
        if a.get("compare") and len(paths) >= 2:
            return await self._read_compare(paths[:4], (a.get("question") or "").strip())
        if len(paths) <= 1:
            return await self._read_image_one({**a, "path": paths[0] if paths else ""})
        sem = asyncio.Semaphore(int(os.environ.get("AIW_READ_IMAGE_PARALLEL", "4")))

        async def one(p):
            async with sem:
                return await self._read_image_one({**a, "path": p, "paths": None})
        outs = await asyncio.gather(*[one(p) for p in paths[:12]])
        return "\n\n".join(outs)

    async def _read_compare(self, paths: list[str], question: str) -> str:
        """Several images side by side in ONE picture, labelled A, B, ..., read in one vision call. Reading them one
        by one cannot compare them: in task-f08 each half of a before/after check was asked "is it level now?" on
        its own, the model said yes, and an unchanged picture was delivered as done."""
        imgs = []
        for p in paths:
            rp = p.strip().lstrip("/")
            for pre in ("home/aiw/workspace/", "workspace/", "~/workspace/"):
                if rp.startswith(pre):
                    rp = rp[len(pre):]
            f = await self.tools.run("send_file", {"path": rp})
            if not f.success or not isinstance(f.result, dict) or not f.result.get("content_b64"):
                return f"FAILED: cannot read {rp} ({f.error})"
            try:
                imgs.append((rp, Image.open(io.BytesIO(base64.b64decode(f.result["content_b64"]))).convert("RGB")))
            except Exception as e:  # noqa: BLE001
                return f"FAILED: {rp} is not an image ({e})"
        zoom = None
        if len(imgs) == 2:
            try:
                zoom = _ie.changed_box(imgs[0][1], imgs[1][1].resize(imgs[0][1].size))
            except Exception:  # noqa: BLE001
                zoom = None
        b64 = base64.b64encode(_side_by_side([im for _, im in imgs], zoom=zoom)).decode()
        labels = "; ".join(f"{chr(65 + i)} = {p}" for i, (p, _) in enumerate(imgs))
        q = (f"这张图把 {len(imgs)} 张图片左右并排放在一起，从左到右依次标为 "
             + "、".join(chr(65 + i) for i in range(len(imgs))) + "。"
             + ("黄线下面一排是两张图中发生变化的区域（原图像素 " + str(list(zoom)) + "）放大后的对比，细节以下面一排为准。"
                if zoom else "")
             + "请逐张对比回答，并明确指出它们之间的差别（没有差别就直接说没有差别）：" + (question or "两张图有什么不同？"))
        eyes = self.vision_llm or self.llm
        try:
            text = await _retry_once(lambda: asyncio.wait_for(eyes.describe(_cad.READ_IMAGE_PROMPT, q, b64, 1800),
                                                              self.cfg.vision_timeout_s * 3))
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=str(e)[:200], result={})
            return f"FAILED: the vision model could not read the images ({type(e).__name__})"
        self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="read_image", success=True, error=None,
                        result={"compare": [p for p, _ in imgs], "chars": len(text or "")})
        return f"compared side by side ({labels}{'; zoomed on ' + str(list(zoom)) if zoom else ''}):\n{(text or '').strip()[:3500]}"

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
        q0 = (a.get("question") or "").strip() + (" [grid]" if a.get("grid") else "")
        prev = self._images_read.get(path)
        # only the same question is answered from the cache: task-f08 asked a second, sharper question about the
        # cabinet heights, got the first (generic) reading back, and then guessed the direction of the edit wrongly
        if prev and not a.get("again") and (not q0 or q0 == (prev[2] if len(prev) > 2 else "")):
            st, txt = prev[0], prev[1]
            return (f"ALREADY READ at step {st} (not read again, it takes ~50 s). What it showed:\n{txt[:3500]}\n"
                    "Use this; ask a different `question` if you need something else from it.")
        f = await self.tools.run("send_file", {"path": path})   # binary read; not delivered to the chat
        if not f.success or not isinstance(f.result, dict) or not f.result.get("content_b64"):
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=f.error, result={})
            return f"FAILED: cannot read {path} ({f.error}). Use files_list to find the uploaded file."
        try:
            img = Image.open(io.BytesIO(base64.b64decode(f.result["content_b64"]))).convert("RGB")
            if a.get("grid"):
                img = _with_grid(img)
            if max(img.size) > 2000:
                k = 2000 / max(img.size)
                img = img.resize((int(img.width * k), int(img.height * k)))
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception as e:  # noqa: BLE001
            return f"FAILED: {path} is not an image ({e})"
        eyes = self.vision_llm or self.llm
        q = (a.get("question") or "").strip()
        if a.get("grid"):
            q = ("图上画了红色像素网格线，线旁的数字是原图像素坐标（x 向右、y 向下）。回答位置时请用这些像素坐标。" + q)
        try:
            text = await _retry_once(lambda: asyncio.wait_for(
                eyes.describe(_cad.READ_IMAGE_PROMPT, q or "请按结构描述这张图。", b64, 1800), self.cfg.vision_timeout_s * 3))
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=str(e)[:200], result={})
            return f"FAILED: the vision model could not read the image ({type(e).__name__})"
        self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="read_image", success=True, error=None,
                        result={"path": path, "chars": len(text or "")})
        self._images_read[path] = (self.step, (text or "").strip(), q0)
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
async def _retry_once(make):
    """Run make() (a fresh coroutine each call); on an exception try once more after a short pause."""
    try:
        return await make()
    except Exception:  # noqa: BLE001
        await asyncio.sleep(2)
        return await make()


def _with_grid(img, step: int | None = None):
    """Red pixel grid with coordinate labels, so a vision model can answer positions in real pixels."""
    from PIL import ImageDraw, ImageFont
    img = img.copy()
    step = step or (50 if max(img.size) <= 1200 else 100)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=max(12, max(img.size) // 70))
    except TypeError:
        font = ImageFont.load_default()
    for x in range(step, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(255, 0, 0), width=1)
        if x % (step * 2) == 0:
            d.text((x + 2, 2), str(x), fill=(255, 0, 0), font=font)
    for y in range(step, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(255, 0, 0), width=1)
        if y % (step * 2) == 0:
            d.text((2, y + 2), str(y), fill=(255, 0, 0), font=font)
    return img


_VIDEO_TASK = re.compile(r"视频|讲解片|宣传片|发布片|动画片|动效|huashu-motion|\bvideo\b|\.mp4\b|explainer", re.I)
_IMAGE_TASK = re.compile(r"\.(png|jpe?g|webp|heic|bmp)\b.*|图片|照片|效果图|P图|修图|改图", re.I | re.S)


_TRANSIENT = re.compile(r"timeout|timed out|connection|reset by peer|broken pipe|eof|temporar|unavailable|"
                        r"\b50[234]\b|bad gateway|too many requests|\b429\b|websocket|not connected", re.I)


def _transient(err) -> bool:
    """Network / model hiccups worth one more try. task-4e2: a shell failed with "TimeoutError: " -- the old check
    looked for lower-case "timeout" only, so nothing retried."""
    return bool(err) and bool(_TRANSIENT.search(str(err)))


def _side_by_side(imgs: list, height: int = 900, zoom: tuple | None = None) -> bytes:
    """Images scaled to one height, next to each other with a big A/B/C label on each, as a JPEG. With `zoom`
    (a box in the first image's pixels) a second row shows that box cut from every image, enlarged: a 2 %
    height change is ~12 px at full-picture scale and the vision model called it "identical" (task-4e2)."""
    from PIL import ImageDraw, ImageFont
    if zoom:
        top = Image.open(io.BytesIO(_side_by_side(imgs, height))).convert("RGB")
        crops = []
        for im in imgs:
            k = im.width / imgs[0].width
            crops.append(im.crop(tuple(int(v * k) for v in zoom)))
        bottom = Image.open(io.BytesIO(_side_by_side(crops, height))).convert("RGB")
        if bottom.width != top.width:
            bottom = bottom.resize((top.width, max(1, round(bottom.height * top.width / bottom.width))))
        canvas = Image.new("RGB", (top.width, top.height + bottom.height + 20), (255, 230, 0))
        canvas.paste(top, (0, 0))
        canvas.paste(bottom, (0, top.height + 20))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    scaled = [im.resize((max(1, round(im.width * height / im.height)), height)) for im in imgs]
    gap = 16
    W = sum(im.width for im in scaled) + gap * (len(scaled) - 1)
    canvas = Image.new("RGB", (W, height + 60), "white")
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=44)
    except TypeError:
        font = ImageFont.load_default()
    x = 0
    for i, im in enumerate(scaled):
        canvas.paste(im, (x, 60))
        d.text((x + 10, 6), chr(65 + i), fill="red", font=font)
        x += im.width + gap
    if canvas.width > 2400:
        k = 2400 / canvas.width
        canvas = canvas.resize((2400, round(canvas.height * k)))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=88)
    return buf.getvalue()


_NO_SCREEN_TOOLS = {"read_image", "image_edit", "shell", "files_read", "files_write", "files_list", "files_download", "send_file", "delegate"}
_NO_REPEAT = {"files_write", "shell", "browser_open", "send_file", "files_download", "delegate", "open_app"}


def _thumb(b64: str):
    try:
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("L").resize((64, 40)).tobytes()
    except Exception:  # noqa: BLE001
        return None


def json_short(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)[:600]
