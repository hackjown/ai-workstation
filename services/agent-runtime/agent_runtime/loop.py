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
from tool_router.registry import BOS_TOOLS, SUBAGENT_TOOLS

from . import cad as _cad
from . import drawing as _draw
from . import drawing_hooks as _dh
from . import video as _video
from . import image_edit as _ie
from . import image_brief as _ib
from . import photopea as _pp
from . import escalate as _es
from . import vision_guard as _vg
from llm_router import vision as _vis

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
    max_steps: int = 0      # 0 = no step budget (3.8.2); >0 pauses and asks when reached, never ends the task
    timeout_s: float = 0.0  # 0 = no time budget; >0 pauses and asks when it runs low
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
        self._draw_rounds = 0          # drawing_build runs (3.8.7 pipeline; at most _draw.MAX_ROUNDS fix rounds)
        self._drawkit_ready = None     # kit version installed on the computer this task
        self._drawkit_built = False    # the drawing in cad/ was generated from a spec (layout check bypassed)
        self._cad_task = self._cad_mode is not None
        self._cad_file: Optional[str] = None  # the DXF this task saves to (a revision edits it in place)
        if self._cad_task and config is None:  # drawing a set in the GUI takes hundreds of steps
            if self.cfg.max_steps > 0:
                self.cfg.max_steps = max(self.cfg.max_steps, int(os.environ.get("AIW_CAD_MAX_STEPS", "400")))
            if self.cfg.timeout_s > 0:
                self.cfg.timeout_s = max(self.cfg.timeout_s, float(os.environ.get("AIW_CAD_TIMEOUT", "10800")))
        self._image_task = bool(_IMAGE_TASK.search(task.prompt or "")) and not self._cad_task
        if self._image_task and config is None and self.cfg.timeout_s > 0:  # task-4e2 ran out at 900 s
            self.cfg.timeout_s = max(self.cfg.timeout_s, float(os.environ.get("AIW_IMAGE_TIMEOUT", "2400")))
        self._video_task = bool(_VIDEO_TASK.search(task.prompt or "")) and not self._cad_task
        if self._video_task and config is None:  # task-247: reading a 16-page deck alone took 15 min
            if self.cfg.max_steps > 0:
                self.cfg.max_steps = max(self.cfg.max_steps, int(os.environ.get("AIW_VIDEO_MAX_STEPS", "200")))
            if self.cfg.timeout_s > 0:
                self.cfg.timeout_s = max(self.cfg.timeout_s, float(os.environ.get("AIW_VIDEO_TIMEOUT", "3600")))
        self._checkin = _es.checkin_steps()   # soft valve: pause and ask after this many steps without progress
        self._progress_at = 0                 # the step of the last visible progress
        self._pages_seen: set = set()
        _dh.init(self)   # 3.8.8: per-task work folder, delivered-file index, drawing phases
        self._waited = 0.0  # seconds spent waiting on the user (ask_user, confirmations): not counted toward timeout
        self._last_image: Optional[str] = None  # newest image_edit output, delivered if the task runs out of time
        self.validator = Validator()
        self._cad_keys_saved: set = set()
        self._cad_touched: set = set()
        self._cad_keys_unsaved: set = set()
        self.recovery = Recovery(computer)
        self._emit_fn = emit
        self.history: list[str] = []
        if self._video_task and re.search(r"huashu-motion|\bom\s|讲解|explainer", task.prompt or ""):
            self.history.append(_video.brief(task.prompt))
        self._video_rejects = 0
        self._desktop_free = (not self._cad_task and not _GUI_TASK.search(task.prompt or "")
                              and os.environ.get("AIW_ALWAYS_LOOK_FIRST", "0") != "1")
        self._images_read: dict[str, tuple] = {}  # path -> (step, description, question): history keeps only ~25 steps
        self.step = 0
        self.ask, self.remember = ask, remember
        self.extra_tools = extra_tools or {}
        self._secrets: dict[str, str] = {}  # form values the model only sees as {{key}}
        self.bos_enabled = False  # set by the session manager when this computer has BrowserOS switched on
        self._bos_hist: list[int] = []  # history indexes of BrowserOS results (older snapshots get trimmed)
        self.subagent_llm = subagent_llm
        # set by the API's session manager when other computers can take work: async (items, timeout_s) -> results.
        # None (a child task, a single computer, tests) hides the dispatch tool.
        self.dispatcher: Optional[Callable[[list, float], Awaitable[list]]] = None
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
        # 3.8.5
        self.home: Optional[str] = None       # $HOME on this computer (set by the session manager): fixes guessed paths
        self.lesson_hint: Optional[Callable[..., Awaitable[Optional[str]]]] = None   # (error, tool, domain) -> note
        self._user_notes: list[str] = []      # 补充要求 the user sent while the task runs

    def inject(self, text: str) -> None:
        """An instruction the user added while the task runs (修改任务 / 补充要求): read at the next step."""
        if (text or "").strip():
            self._user_notes.append(text.strip()[:2000])

    def _fix_paths(self, tool: str, args: dict) -> tuple[dict, bool]:
        """A guessed home (/home/user/workspace on a computer whose home is /data/home) becomes the real one.
        task-7bd: the model hard-coded /home/user/workspace in a python-docx script and the save failed."""
        home = (self.home or "").rstrip("/")
        if not home or tool not in ("shell", "files_write", "files_read", "files_list", "send_file") or not isinstance(args, dict):
            return args, False
        changed = False
        out = dict(args)
        for k in ("command", "content", "path"):
            v = out.get(k)
            if not isinstance(v, str):
                continue
            nv = _GUESSED_HOME.sub(lambda m: home + "/workspace" if not home.startswith(m.group(1).rstrip("/")) else m.group(0), v)
            if nv != v:
                out[k], changed = nv, True
        return out, changed

    @property
    def task_text(self) -> str:
        if self.task.context:
            return f"{self.task.context}\n\nCURRENT REQUEST: {self.task.prompt}"
        return self.task.prompt

    async def emit(self, type_: EventType, **data):
        if self._emit_fn:
            await self._emit_fn(AgentEvent(session_id=self.session_id, type=type_, step=self.step, data=data))

    # ---------------------------------------------------------------- observe
    async def _see(self) -> tuple[Optional[str], tuple[int, int]]:
        """The screen for this step. Working in BrowserOS, the page the task is on is brought to the front first and
        checked; when the desktop still shows another window, a picture of the page itself is used instead
        (task-d76ffb0bd1 took 13 screenshots of BrowserOS's session-replay window while the captcha sat elsewhere)."""
        p = getattr(self, "_bos_page", None)
        last = str(self._last_tool or "")
        if (p is not None and self.step - getattr(self, "_bos_step", -99) <= 4
                and (last == "screenshot" or last.startswith("bos_"))):
            try:
                v = await self._bos_view(int(p))
            except (StopRequested, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001  fall back to the desktop screenshot
                await self.emit(EventType.ERROR, error=f"找页面窗口失败，改用桌面截图：{type(e).__name__}: {str(e)[:160]}")
                v = None
            if v:
                return v
        self._page_view = None
        return await self.observe()

    async def _bos_view(self, page: int):
        ex = getattr(self.computer, "execute", None)
        if ex is None:
            return None
        key = getattr(self.tools, "bos_key", None) or "default"
        r = await asyncio.wait_for(ex("browseros.call", {"tool": "front", "args": {"page": page}, "key": key},
                                      observe=False, timeout=20), 25)
        res = r.result if (r and isinstance(r.result, dict)) else {}
        if not r or not r.success or "verified" not in res:   # an older vm-agent: no way to tell, use the desktop
            return None
        self._fresh = None   # a screenshot taken before the window was raised shows the wrong window
        if res.get("verified"):
            self._page_view = None
            return None   # the desktop now shows the page: a new screenshot is right
        r2 = await asyncio.wait_for(ex("browseros.call", {"tool": "page_shot", "args": {"page": page}, "key": key},
                                       observe=False, timeout=30), 35)
        res2 = r2.result if (r2 and isinstance(r2.result, dict)) else {}
        imgs = res2.get("images") or []
        if not r2.success or not imgs or not imgs[0].get("data"):
            return None
        raw = imgs[0]["data"]
        img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
        full = img.size
        scale = 1.0
        if img.width > self.cfg.llm_image_width:
            ratio = self.cfg.llm_image_width / img.width
            img = img.resize((self.cfg.llm_image_width, int(img.height * ratio)))
            scale = 1 / ratio
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=75)
        artifact = self._save_artifact(raw)
        await self.emit(EventType.OBSERVATION, width=full[0], height=full[1], artifact=artifact, page_shot=True,
                        page=page, url=res2.get("url"))
        self._page_view = {"page": page, "scale": scale}
        self._page_note = (f"The desktop shows another window ({res.get('window') or 'unknown'}), so this image is BrowserOS "
                           f"page {page} ({res2.get('url') or res.get('url') or ''}) itself, taken from the browser. Its "
                           "coordinates are page coordinates: act by ref with bos_act, or bos_act kind=click_at with x,y "
                           "read from THIS image. If it shows a captcha/slider/login only a person can pass, call "
                           "request_takeover now.")
        return base64.b64encode(buf.getvalue()).decode(), img.size

    def _page_click(self, decision) -> None:
        """While the model looks at a page picture (not the desktop), its coordinates belong to that page: a desktop
        click becomes bos_act click_at on the page, and page coordinates are scaled back to full size."""
        pv = getattr(self, "_page_view", None)
        if not pv:
            return
        a = dict(decision.args or {})
        sc = float(pv.get("scale") or 1.0)
        if decision.tool in ("click", "double_click", "left_click") and a.get("x") is not None and a.get("y") is not None:
            decision.tool = "bos_act"
            decision.args = {"page": pv["page"], "kind": "click_at", "x": round(float(a["x"]) * sc),
                             "y": round(float(a["y"]) * sc)}
            return
        if (decision.tool == "bos_act" and str(a.get("kind") or "") in ("click_at", "type_at", "hover_at", "drag")
                and sc != 1.0 and a.get("x") is not None and a.get("y") is not None):
            a["x"], a["y"] = round(float(a["x"]) * sc), round(float(a["y"]) * sc)
            a.setdefault("page", pv["page"])
            decision.args = a

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

    async def _video_qa(self, path: str) -> Optional[str]:
        """Loudness, channels, and for a document explainer page coverage and length (task-8bd: 8 of 16 pages, -25.7 LUFS mono)."""
        try:
            r = await asyncio.wait_for(self.tools.run("shell", {"command": _video.qa_command(path, _video.doc_path(self.task.prompt)),
                                                                  "timeout": 300}, intent="check the video before sending it"), 320)
        except Exception:  # noqa: BLE001
            return None
        if not r.success or not isinstance(r.result, dict):
            return None
        return _video.verdict(str(r.result.get("stdout") or ""), self.task.prompt)

    def add_wait(self, seconds: float) -> None:
        """Time spent waiting on the user extends the deadline (task-247 lost 10 of its 15 min to two unanswered
        confirmations, then timed out 6 s after asking a question)."""
        self._waited += max(0.0, float(seconds or 0))

    async def _run_with_deadline(self) -> None:
        job = asyncio.ensure_future(self._run())
        stop = getattr(getattr(self, "control", None), "stopped", None)
        stop_w = asyncio.ensure_future(stop.wait()) if stop is not None else None
        start = time.monotonic()
        self._t0 = start
        stopped = False
        try:
            while True:
                asking = getattr(self, "_asking_since", None)   # a question open right now does not use up the budget
                if self.cfg.timeout_s <= 0:
                    left = 3600.0          # no time budget: only the user's stop ends the task
                else:
                    left = (self.cfg.timeout_s + self._waited + (time.monotonic() - asking if asking else 0)
                            - (time.monotonic() - start))
                    if (asking or getattr(self, "ask", None)) and left <= 0:
                        # with someone to ask the time budget only pauses the task (the loop asks at the next step)
                        self._time_up = True
                        left = 30
                    if left <= 0:
                        raise asyncio.TimeoutError
                done, _ = await asyncio.wait({job, stop_w} - {None}, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                if job in done:
                    return job.result()
                if stop_w is not None and stop_w in done:
                    stopped = True  # cancel the model call / command in flight right now
                    raise StopRequested()
        finally:
            if stop_w is not None and not stop_w.done():
                stop_w.cancel()
            if not job.done():
                job.cancel()
                try:
                    await asyncio.wait_for(job, 5)
                except BaseException:  # noqa: BLE001
                    pass
            if stopped:
                await self._kill_commands()

    async def _kill_commands(self) -> None:
        """Stop also ends the shell commands this task left running on the computer (soffice, ffmpeg...)."""
        try:
            ex = getattr(self.computer, "execute", None)
            if ex:
                await asyncio.wait_for(ex("shell.kill", {}, observe=False, timeout=8), 10)
        except BaseException:  # noqa: BLE001  an older vm-agent has no shell.kill
            pass

    # ------------------------------------------------------------------- run
    async def run(self) -> Task:
        self.task.status = TaskStatus.RUNNING
        await self.emit(EventType.TASK_STARTED, prompt=self.task.prompt)
        plan_job = None
        if self.plan_steps:   # a continuation: the plan and its ticks come from the earlier run
            await self.emit(EventType.PLAN, plan=self.plan_steps, done=sorted(self.plan_done))
        elif self.cfg.make_plan and _needs_action(self.task.prompt):
            plan_job = asyncio.create_task(self._plan())  # plan in parallel with the first step, not before it
        await _dh.start(self)
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
            self._finish(TaskStatus.FAILED, _es.report("time", msg, self.history, getattr(self, "_last_page", None),
                                                       getattr(getattr(self, "_stall", None), "blocked", "")))
        except StopRequested:
            self._finish(TaskStatus.STOPPED, "stopped by user")
        except Exception as e:  # noqa: BLE001
            self._finish(TaskStatus.FAILED, _es.report("error", f"{type(e).__name__}: {e}", self.history,
                                                       getattr(self, "_last_page", None)))
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
        return src in ("retry", "resume", "continue", "edit") or bool(_CAD_RESUME.search(self.task.prompt or ""))

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
        remember its texts for the layout check, and tell the model to change only what the user named.
        3.8.7: when the drawing was generated from a spec (cad/*spec*.json), a revision edits the spec and regenerates."""
        try:
            r = await self.tools.run("shell", {"command": 'cd "${VM_AGENT_FILES_ROOT:-$HOME/workspace}" 2>/dev/null && ls -t '
                                               + _cad.CAD_DIR + '/*spec*.json 2>/dev/null | head -1', "timeout": 20},
                                     intent="find the drawing spec to revise")
            spec = ((r.result or {}).get("stdout") or "").strip() if isinstance(r.result, dict) else ""
        except Exception:  # noqa: BLE001
            spec = ""
        why = await _dh.stale_spec(self, spec) if spec else None
        if why:   # task-41c: the G-plan spec of another image was revised for 100 steps against the A3-1 photo
            self.history.append(f"0. STALE SPEC {spec} archived to cad/_stale/: {why}. This is a NEW drawing for the "
                                "current image: drawing_read the chains (side=...), drawing_auto for a draft, correct it, "
                                "drawing_build.")
            return
        if spec:
            self.history.append(
                f"0. REVISION of a GENERATED drawing: its spec is {spec}. files_read it, change only what the user named "
                "(re-read the source with drawing_read where they say it does not match the photo), files_write the "
                f"corrected spec, then drawing_build spec={spec}: the whole drawing is regenerated and checked. Never "
                "ERASE or retype on the canvas. Then send_file the PNG and DXF and done listing each change.")
            return
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
            "If the user says it does not match the source / the layout or dimensions are wrong / many overlaps, do NOT "
            "patch it: redo it with the drawing pipeline (drawing_read each dimension chain, write cad/spec.json, "
            "drawing_build) and deliver the regenerated drawing. Only for a small named fix (move one label, change "
            "one text) edit in place: change ONLY what the user asked; never redraw a sheet or a FRAME. Per fix: cad_command ['SHEET|n', "
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
        self._stall = _es.Stall()
        self._time_asked = False
        while True:
            if self.cfg.max_steps > 0 and self.step >= self.cfg.max_steps:
                n = await self._escalate("max_steps", f"已经执行了 {self.step} 步")
                if n is None:
                    return
                note, failures, shots = n, 0, 0
            if self._checkin and self.step - self._progress_at >= self._checkin:
                n = await self._escalate("checkin", f"已经执行了 {self.step} 步，最近 {self.step - self._progress_at} 步"
                                                    "没有勾掉计划、发出文件或打开新页面")
                if n is None:
                    return
                note, failures, shots = n, 0, 0
            t0 = getattr(self, "_t0", None)
            if t0 and self.cfg.timeout_s > 0 and not self._time_asked and self.ask and (getattr(self, "_time_up", False) or (
                    self.cfg.timeout_s + self._waited - (time.monotonic() - t0)) < min(90.0, 0.15 * self.cfg.timeout_s)):
                self._time_asked = True
                n = await self._escalate("time", f"任务时间预算 {self.cfg.timeout_s:.0f} 秒快用完了")
                if n is None:
                    return
                note, failures, shots = n, 0, 0
            if self.cfg.max_steps > 3 and self.step == self.cfg.max_steps - 3:
                note = ((note + "\n") if note else "") + (
                    "Only 3 steps are left in this task's budget. If you can finish, finish (send what you have, then done). "
                    "If something blocks you, call request_takeover or ask_user NOW and explain the problem to the user.")
            if await self.control.checkpoint():
                self._fresh = None
                note = ("The user took over the desktop and has now returned control. "
                        "Re-assess from the current screenshot; do not assume earlier state."
                        if self.control.resumed_after_takeover else "Agent was paused and resumed.")
                await self.emit(EventType.RESUMED, takeover=self.control.resumed_after_takeover)
            if self._user_notes:   # 3.8.5: words the user added while the task runs
                adds = "\n".join(f"- {t}" for t in self._user_notes)
                self._user_notes = []
                self.history.append(f"{self.step}. USER ADDED (mid-task): {adds[:600]}")
                self._mark_progress()
                note = ((note + "\n") if note else "") + (
                    "THE USER JUST ADDED TO THIS TASK (follow it from now on; it changes or extends the request, it is "
                    "not a new task):\n" + adds)
            self.step += 1
            blind = self._last_tool in _NO_SCREEN_TOOLS and self.step > 1
            # shell/file work (analyse a video, convert a file, write a script) needs no first look at the desktop:
            # every task used to open with a screenshot + vision description of an empty Debian desktop
            first_blind = (self.step == 1 and (self._desktop_free or getattr(self, "_dphase", None) is not None)
                           and not self.control.resumed_after_takeover)   # a drawing reads files, not the desktop
            blind = blind or first_blind
            # a CAD batch that went through needs no fresh look (screenshot + vision cost ~15 s a step);
            # look again every 4th batch so a drawing error is still caught
            if (self._last_tool == "cad_command" and self._last_ok_cad and self.step > 1
                    and self._cad_ok % 4 != 0):
                blind = True
                cad_blind = True
            else:
                cad_blind = False
            bos_blind = (self.step > 1 and str(self._last_tool or "").startswith("bos_")
                         and not (self._last_tool == "bos_tabs" and getattr(self, "_bos_front", False)))
            if bos_blind and not blind:
                blind = True
            if blind:  # a shell/file step does not change the screen: skip the screenshot and the vision call
                before, shown = None, self._last_shown
                note = ((note + "\n") if note else "") + (
                    "Screen not re-read: the last LibreCAD batch went through. Send the next batch from your plan; "
                    "the screen is shown again every few batches." if cad_blind else
                    "No screenshot: your last step was a BrowserOS tool and its result above shows the page (diff or "
                    "snapshot). Continue with bos_*; call screenshot only if you fall back to clicking by coordinates."
                    if bos_blind else
                    "No screenshot: this task looks like shell/file work (files are under ~/workspace, uploads in "
                    "~/workspace/uploads). Start with shell; call screenshot only if you need the desktop." if first_blind else
                    "Screen not re-read: your last step was a shell/file action. Keep working in the shell; "
                    "call screenshot only if you need to see the desktop.")
            else:
                before, shown = await self._see()
                self._last_shown = shown
                if getattr(self, "_page_note", None):
                    note = ((note + "\n") if note else "") + self._page_note
                    self._page_note = None

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
            live_think = {"at": 0.0, "n": 0}

            async def on_reasoning(text: str, _lt=live_think):
                # shown live under "正在思考…"; broadcast only (not stored), at most once a second
                now = time.monotonic()
                if now - _lt["at"] < 1.0 or len(text) == _lt["n"]:
                    return
                _lt["at"], _lt["n"] = now, len(text)
                await self.emit(EventType.THINKING, live=True, text=text[-1500:], chars=len(text))
            req = LLMRequest(
                system=self.system, task=self._task_with_plan(), history=self.history,
                screenshot_b64=send_shot, tools=tool_schemas([t[0] for t in self.extra_tools.values()],
                                   hide=self._hidden_tools()),
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
                          else os.environ.get("AIW_IMAGE_THINKING", "off") if self._image_task else None),
                on_reasoning=on_reasoning)
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
            if shots >= 4:   # it used to end the task as "done" with no answer; now the user hears it is stuck
                n = await self._escalate("stall", decision.thought or "连续截图，没有下一步操作")
                if n is None:
                    return
                note, failures, shots = n, 0, 0
                continue
            if (decision.tool == "done" and self._cad_task and not self._sent_file and self._done_rejects < 6
                    and self.step < self._cap() - 3):
                self._done_rejects += 1
                await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args)
                await self.emit(EventType.VALIDATION, success=False, change=0.0,
                                message="拒绝 done：图纸还没画完/没保存发送")
                self.history.append(f"{self.step}. done REJECTED: the drawing set is not finished, saved and sent")
                note = ("REJECTED: the drawings are not finished. Every sheet the user asked for must be generated "
                        "(cad/spec.json -> drawing_build) and its PNG + DXF sent with send_file before done. Continue with "
                        "the next unfinished sheet now. If you have no spec yet, read the source and write it first.")
                continue
            if (decision.tool == "done" and self._cad_task and self._sent_file and self._done_rejects < 6
                    and self.step < self._cap() - 3 and (not self._cad_pdf_ok or self._sheet_audit.get("thin"))):
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
                        "REJECTED: the drawing was not generated and checked. Generate it with drawing_build from "
                        "cad/spec.json (or, for a drawing typed in LibreCAD, export with cad_command ['SAVE|cad/<name>.dxf', "
                        "'PDF|cad/<name>.dxf|cad/<name>.pdf']; soffice mangles DXF). Then send_file the result.")
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
            if (decision.tool == "send_file" and self._video_task and self._video_rejects < 3
                    and re.search(r"\.(mp4|mov|webm)$", str(decision.args.get("path") or ""), re.I)):
                note = await self._video_qa(str(decision.args.get("path")))
                if note:
                    self._video_rejects += 1
                    await self.emit(EventType.DECISION, thought=decision.thought, tool=decision.tool, args=decision.args)
                    await self.emit(EventType.VALIDATION, success=False, change=0.0, message="视频交付前检查未通过：" + note[:300])
                    self.history.append(f"{self.step}. send_file REFUSED by video QA")
                    continue
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
                            out_tokens=u.get("out_tokens") or u.get("completion_tokens"), attempts=u.get("attempts"),
                            reasoning=(getattr(decision, "reasoning", "") or "")[-4000:] or None)
            sig = (decision.tool, repr(sorted(decision.args.items())))
            if decision.tool not in ("screenshot", "wait", "done", "fail"):
                self._rep = self._rep + 1 if sig == self._prev_sig else 0
                self._prev_sig = sig
                if self._rep >= 4:
                    await self.emit(EventType.VALIDATION, success=False, change=0.0, message="同一操作连续重复，先问你怎么办")
                    n = await self._escalate("repeat", f"连续 {self._rep + 1} 次执行同一个操作（{decision.tool}），结果没有变化")
                    if n is None:
                        return
                    note, failures = n, 0
                    continue
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

            gate, gnote = await _dh.pre_tool(self, decision)
            if gate == "converge":   # 3.8.9: looping after a spec exists -> deterministic pipeline, then deliver
                await self.emit(EventType.VALIDATION, success=False, change=0.0, message="换方法：确定性流水线出图")
                if await _dh.converge(self, gnote):
                    return
                note, failures = getattr(self, "_converge_note", None) or gnote, 0
                continue
            if gate == "escalate":
                kind, detail = gnote if isinstance(gnote, tuple) else ("drawing_loop", gnote)
                n = await self._escalate(kind, detail)
                if n is None:
                    return
                n = await _dh.on_resume(self, n)   # 继续 = a fresh budget AND the deterministic pipeline
                if self.task.status in (TaskStatus.SUCCEEDED,):
                    return
                note, failures = n, 0
                continue
            if gate == "skip":
                await self.emit(EventType.VALIDATION, success=False, change=0.0, message="停止重复分析：先写 spec 再生成")
                note = gnote
                continue
            self._page_click(decision)
            self._last_tool = decision.tool
            await self.emit(EventType.ACTION, tool=decision.tool, args=decision.args)
            if decision.tool in _vg.TOOLS and _vg.is_open(self):   # 3.8.8: vision service down, don't call it
                n = await _vg.gate(self, decision)
                if n is None:
                    return
                note = n
                continue
            handled = await self._loop_tool(decision)
            if handled is not None:
                self.history.append(f"{self.step}. {decision.tool}({_args_short(decision.args)}) -> {handled}")
                _dh.post_tool(self, decision, not str(handled).startswith(("FAILED", "REFUSED", "CONFLICT")), str(handled))
                await _dh.save_facts(self)
                note = None
                continue

            outcome = None
            run_args, fixed = self._fix_paths(decision.tool, decision.args)
            if (decision.tool == "shell" and isinstance(run_args, dict) and _OCR_RE.search(str(run_args.get("command") or ""))
                    and float(run_args.get("timeout") or 60) > float(os.environ.get("AIW_OCR_TIMEOUT", "20"))):
                # task-3fc: tesseract on the whole photo hung for 60 s; an OCR call gets 20 s, then falls back
                run_args = dict(run_args, timeout=float(os.environ.get("AIW_OCR_TIMEOUT", "20")))
            if fixed:
                self.history.append(f"{self.step}. (path fixed: /home/user/workspace → {self.home}/workspace; "
                                    "write ~/workspace next time)")
            for attempt in range(self.cfg.action_retries + 1):
                outcome = await self.tools.run(decision.tool, self._fill_secrets(decision.tool, run_args), intent=decision.thought)
                await self.emit(EventType.POLICY, level=int(outcome.decision.level), reason=outcome.decision.reason)
                retryable = (not outcome.success and outcome.decision.allowed and outcome.control is None
                             and _transient(outcome.error))
                if not retryable:
                    break
                await self.emit(EventType.RECOVERY, attempt=attempt + 1, note=f"transient error, retrying: {outcome.error[:120]}")
                await asyncio.sleep(1.5 * (attempt + 1))
            try:   # the page on screen, for a takeover card's fallback site name
                if decision.tool in ("browser_open", "browser_navigate", "bos_navigate") and decision.args.get("url"):
                    self._last_page = {"url": str(decision.args["url"]), "title": ""}
                if outcome.success and isinstance(outcome.result, dict) and outcome.result.get("url"):
                    self._last_page = {"url": str(outcome.result["url"]), "title": str(outcome.result.get("title") or "")}
                u = str((getattr(self, "_last_page", None) or {}).get("url") or "").split("#", 1)[0]
                if outcome.success and u and u not in self._pages_seen:
                    self._pages_seen.add(u)
                    self._mark_progress()
            except Exception:  # noqa: BLE001
                pass
            if outcome.action == "browseros.call" and self._secrets and isinstance(outcome.result, dict):
                t = str(outcome.result.get("text") or "")
                for sv in self._secrets.values():   # a diff can echo a filled value back
                    if sv and len(sv) >= 3:
                        t = t.replace(sv, "••••")
                outcome.result["text"] = t
            refused = (_dh.check_delivery(self, outcome.result) if decision.tool == "send_file" and outcome.success
                       and isinstance(outcome.result, dict) else None)
            if refused:   # the user's rule: an earlier task's output is never delivered again
                await self.emit(EventType.ACTION_RESULT, tool="send_file", success=False, error="same file as an earlier task", result={})
                await self.emit(EventType.VALIDATION, success=False, change=0.0, message="拒绝交付：和以前任务的成品完全相同")
                self.history.append(f"{self.step}. send_file({decision.args.get('path')}) -> {refused}")
                note = refused
                continue
            if decision.tool == "send_file" and outcome.success and isinstance(outcome.result, dict):
                await self._deliver_file(outcome.result)
                _dh.record_delivery(self, outcome.result)
                outcome.result = {k: v for k, v in outcome.result.items() if k != "content_b64"}
                outcome.result["sent_to_user"] = True
                self._sent_file = Path(outcome.result.get("path") or "file").name
                self._mark_progress()
                note = ("The file was delivered to the user's chat. If nothing else was asked, call done NOW "
                        "with a short Chinese summary.")
            if outcome.success and decision.tool not in ("screenshot", "done", "fail"):
                self._acted = True
            await self.emit(EventType.ACTION_RESULT, tool=outcome.tool, success=outcome.success,
                            error=outcome.error, result=_short(outcome.result))
            seen = ""
            if isinstance(outcome.result, dict):
                seen = str(outcome.result.get("text") or outcome.result.get("stdout") or "")[:20000]
            st = self._stall.feed(decision.tool, decision.args, seen + " " + str(outcome.error or "") + " " + (desc or ""))
            if st in ("blocked", "stall") and outcome.control is None:
                self.history.append(_hist_line(self.step, decision, outcome, "looked again; the page has not moved"))
                n = await self._escalate(st, decision.thought or "")
                if n is None:
                    return
                note, failures = n, 0
                continue
            stall_note = _es.NUDGE if st == "nudge" else None

            if outcome.control == "done":
                self._finish(TaskStatus.SUCCEEDED, decision.args.get("summary", "done"))
                return
            if outcome.control == "fail":   # the model may not give up on its own: the user decides
                n = await self._escalate("fail", str(decision.args.get("reason") or decision.thought or ""))
                if n is None:
                    return
                note, failures = n, 0
                continue

            after = outcome.screenshot
            v = self.validator.check(outcome.action, outcome.success, outcome.error, before, after)
            await self.emit(EventType.VALIDATION, success=v.success, change=v.changed, message=v.message)
            self.history.append(_hist_line(self.step, decision, outcome, v.message))
            _dh.post_tool(self, decision, outcome.success, "")
            if outcome.action == "browseros.call":
                self._bos_front = decision.tool == "bos_tabs" and (decision.args or {}).get("action") == "front" and outcome.success
                if outcome.success:   # which BrowserOS page the task is on, so a screenshot can find it
                    pg = (decision.args or {}).get("page")
                    m = re.search(r"opened page (\d+)", str((outcome.result or {}).get("text") or "")
                                  if isinstance(outcome.result, dict) else "")
                    if m:
                        pg = int(m.group(1))
                    if pg is not None:
                        self._bos_page = int(pg)
                    self._bos_step = self.step
                self._trim_bos(len(self.history) - 1)
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
                    n = await self._escalate("noargs", f"模型连续 {failures} 次调用 {decision.tool} 时没有给出参数（{outcome.error}），"
                                                       "多半是模型输出被截断，可换主模型或调大 AIW_LLM_MAX_TOKENS")
                    if n is None:
                        return
                    note, failures = n, 0
                    continue
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
                    n = await self._escalate("errors", f"连续 {failures} 步出错，最后一次：{v.message} {outcome.error or ''}")
                    if n is None:
                        return
                    note, failures = n, 0
                    continue
                note = await self.recovery.recover(failures, outcome.action)
                await self.emit(EventType.RECOVERY, attempt=failures, note=note)
                hint = await self._lesson_for(decision.tool, outcome)
                if hint:
                    note = ((note + "\n") if note else "") + hint
            if stall_note:
                note = ((note + "\n") if note else "") + stall_note


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
        if _vg.is_open(self):   # 3.8.8: the vision service is down; the screenshot goes to the main model as is
            return None
        try:
            await self.emit(EventType.THINKING, phase="vision")
            desc = (await _vg.read(self, self.vision_llm, "You are the eyes of a computer-use agent.", prompt, shot, 900,
                                   self.cfg.vision_timeout_s, count_timeout=False)).strip()
        except _vis.VisionFailed as e:
            if e.err.status is None and e.err.detail.startswith("timeout"):
                await self.emit(EventType.ERROR, error=f"视觉模型超过 {self.cfg.vision_timeout_s:.0f} 秒没回应，本步跳过（建议换更快的视觉模型）")
            else:
                await self.emit(EventType.ERROR, error=f"视觉模型失败（{e.err.where()}），改为直接发截图"[:300])
            return None
        if not desc:
            return None
        desc = convert_desc(desc, shown, self._eyes_mode)  # 0-1000 answers -> pixels of the screenshot
        self._seen_hash, self._seen_desc = h, desc
        await self.emit(EventType.OBSERVATION, vision=desc[:1500])
        return desc

    def _task_with_plan(self) -> str:
        base = self.task_text + _dh.task_block(self)
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
                    if i not in self.plan_done:
                        self._mark_progress()
                    self.plan_done.add(i)
            await self.emit(EventType.PLAN_UPDATE, done=sorted(self.plan_done), note=a.get("note"))
            await self.emit(EventType.ACTION_RESULT, tool=t, success=True, error=None, result={})
            return f"plan updated, done={sorted(self.plan_done)}"
        if t == "ask_user" and a.get("fields"):
            return await self._ask_form(a, decision.thought)
        if t == "request_takeover":
            return await self._request_takeover(a, decision.thought)
        if t == "ask_user":
            q = a.get("question") or decision.thought or "?"
            opts = [str(o) for o in (a.get("options") or [])][:6]
            await self.emit(EventType.INPUT_REQUIRED, question=q, options=opts)
            t0 = time.monotonic()
            reply = await self._ask(q, opts)
            self.add_wait(time.monotonic() - t0)
            await self.emit(EventType.INPUT_ANSWERED, answer=reply)
            await self.emit(EventType.ACTION_RESULT, tool=t, success=reply is not None, error=None if reply else "no answer",
                            result={"answer": reply})
            if reply:
                return f"user answered: {reply!r}"
            # 3.8.5: never "我按最实用的默认方案" — an unanswered question is asked again or the task waits
            return ("NO ANSWER from the user yet. Do NOT decide yourself and do NOT pick a default option: call ask_user "
                    "again with the same question (it waits until the user answers), or stop if nothing can be done "
                    "without the answer.")
        if t == "click_element":
            return await self._click_element(a, decision.thought)
        if t == "cad_draw":
            if not _cad.script_drawing_allowed(self.task.prompt):
                await self.emit(EventType.ACTION_RESULT, tool=t, success=False, error="script drawing not allowed", result={})
                return ("REFUSED: cad_draw is only for a script the user asked for. Write the drawing as a spec "
                        "(cad/spec.json, schema in the cad-drawing skill) and generate it with drawing_build.")
            return await self._cad_draw(a, decision.thought)
        if t == "drawing_read":
            return await self._drawing_read(a)
        if t == "drawing_build":
            return await self._drawing_build(a, decision.thought)
        if t in ("drawing_note", "drawing_auto"):
            return await _dh.tool(self, decision)
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
        if t == "dispatch":
            return await self._dispatch(a)
        if t in self.extra_tools:
            _schema, fn = self.extra_tools[t]
            await self.emit(EventType.TOOL_CALL, tool=t, args=a)
            _need = getattr(fn, "needs_confirm", None)   # a tool may judge its own calls (remote_exec: by host + command)
            if (_need(a) if callable(_need) else _RISKY_EXT.search(t)) and self.tools.confirm:
                from tool_router import Decision, RiskLevel
                ok = await self.tools.confirm(t, a, Decision(RiskLevel.L3, getattr(fn, "confirm_reason", None) or "external tool can act outside the VM"))
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
            text = str(res)[:int(getattr(fn, "max_result", 600))] if ok else f"FAILED ({err})"
            return text
        return None

    async def _lesson_for(self, tool: str, outcome) -> Optional[str]:
        """3.8.5: a lesson from earlier tasks that matches this error (by error text, tool, site, computer)."""
        if not self.lesson_hint:
            return None
        res = outcome.result if isinstance(outcome.result, dict) else {}
        err = " ".join(str(x) for x in (outcome.error, res.get("stderr"), res.get("stdout") if not outcome.success else "") if x)
        if not err.strip():
            return None
        dom = ""
        try:
            from urllib.parse import urlparse
            dom = urlparse(str((getattr(self, "_last_page", None) or {}).get("url") or "")).hostname or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            hint = await self.lesson_hint(err[-1500:], tool, dom)
        except Exception:  # noqa: BLE001
            return None
        if hint:
            await self.emit(EventType.MESSAGE, text="用上了以前学到的经验", lesson=hint[:600])
        return hint

    # ------------------------------------------------- forms the user fills (sign-ups, profiles)
    def _cap(self) -> int:
        return self.cfg.max_steps if self.cfg.max_steps > 0 else 10 ** 9

    def _mark_progress(self) -> None:
        self._progress_at = self.step

    async def _ask(self, q: str, opts: list, extra: Optional[dict] = None):
        if not self.ask:
            return None
        self._asking_since = time.monotonic()
        self._mark_progress()          # the user was in the loop: the no-progress count starts again
        try:
            if getattr(self, "_stall", None) is not None:
                self._stall.reset()
            if extra is None:
                return await self.ask(q, opts)
            try:
                return await self.ask(q, opts, extra)
            except TypeError:  # an older ask() without the extra argument
                return await self.ask(q, opts)
        finally:
            self._asking_since = None

    async def _ask_form(self, a: dict, intent: str) -> str:
        """ask_user with `fields`: the user gets a card, opens it, fills the fields. Passwords and other secret
        fields never enter the history or the events: the model sees {{key}} and `type` swaps the value in."""
        fields = []
        for f in a.get("fields") or []:
            if not isinstance(f, dict):
                continue
            key = _re.sub(r"[^a-zA-Z0-9_]", "_", str(f.get("key") or f.get("label") or ""))[:40].strip("_")
            if not key:
                continue
            typ = str(f.get("type") or "text").lower()
            typ = typ if typ in ("text", "password", "email", "tel", "date", "number", "select", "textarea") else "text"
            fields.append({"key": key, "label": str(f.get("label") or key)[:80], "type": typ,
                           "required": f.get("required", True) is not False,
                           "secret": bool(f.get("secret")) or typ == "password",
                           "options": [str(o) for o in (f.get("options") or [])][:40],
                           "placeholder": str(f.get("placeholder") or "")[:80], "hint": str(f.get("hint") or "")[:160]})
        if not fields:
            return "FAILED: fields needs at least one {key,label,type}"
        title = str(a.get("title") or a.get("question") or "需要填写资料")[:80]
        form = {"kind": "form", "title": title, "description": str(a.get("description") or "")[:300], "fields": fields}
        await self.emit(EventType.INPUT_REQUIRED, question=title, options=[], form=form)
        t0 = time.monotonic()
        reply = await self._ask(title, [], form)
        self.add_wait(time.monotonic() - t0)
        vals = {}
        if reply:
            try:
                vals = json.loads(reply) if isinstance(reply, str) else dict(reply)
            except (ValueError, TypeError):
                vals = {}
        if not isinstance(vals, dict) or not vals:
            await self.emit(EventType.INPUT_ANSWERED, answer=None)
            await self.emit(EventType.ACTION_RESULT, tool="ask_user", success=False, error="no answer", result={})
            return "user did not fill the form" + (f" and wrote: {reply!r}" if reply else "") + "; ask again or stop"
        shown, safe = [], {}
        for f in fields:
            v = str(vals.get(f["key"]) or "").strip()
            if f["secret"] and v:
                self._secrets[f["key"]] = v
                shown.append(f"{f['label']} ({f['key']}): use {{{{{f['key']}}}}} in `type`, it is filled in for you")
                safe[f["key"]] = "••••"
            else:
                shown.append(f"{f['label']} ({f['key']}): {v!r}")
                safe[f["key"]] = v
        await self.emit(EventType.INPUT_ANSWERED, answer=json.dumps(safe, ensure_ascii=False))
        await self.emit(EventType.ACTION_RESULT, tool="ask_user", success=True, error=None, result={"fields": list(safe)})
        self._acted = True
        return "user filled the form: " + "; ".join(shown) + ". Type each value into its own field of the page now."

    def _trim_bos(self, idx: int) -> None:
        """Keep the newest BrowserOS readbacks whole; older snapshots/diffs shrink to their first lines (they are
        stale anyway, and three store-page snapshots fill the model's context)."""
        self._bos_hist = [i for i in self._bos_hist if i < len(self.history)] + [idx]
        keep = int(os.environ.get("AIW_BOS_KEEP", "2"))
        for i in self._bos_hist[:-keep]:
            line = self.history[i]
            if len(line) > 700 and "[older BrowserOS output trimmed" not in line:
                self.history[i] = line[:500] + " …[older BrowserOS output trimmed; refs from it are stale: take a new bos_snapshot]"
        self._bos_hist = self._bos_hist[-(keep + 30):]

    def _fill_secrets(self, tool: str, args: dict) -> dict:
        if tool == "bos_act" and self._secrets and "{{" in repr(args):
            out, hit = dict(args), False

            def sub(v):
                nonlocal hit
                if isinstance(v, str) and "{{" in v:
                    for k, val in self._secrets.items():
                        if "{{" + k + "}}" in v:
                            v, hit = v.replace("{{" + k + "}}", val), True
                return v
            for k in ("text", "value"):
                if k in out:
                    out[k] = sub(out[k])
            if isinstance(out.get("fields"), list):
                out["fields"] = [{**f, "value": sub(f.get("value"))} if isinstance(f, dict) else f for f in out["fields"]]
            if hit:
                out["secret"] = True
            return out
        if tool != "type" or not self._secrets or "{{" not in str(args.get("text") or ""):
            return args
        text = str(args["text"])
        for k, v in self._secrets.items():
            text = text.replace("{{" + k + "}}", v)
        return {**args, "text": text, "secret": True}

    async def _page_now(self) -> dict:
        """The browser page on screen now ({url, title}) for a takeover card: Chrome's DevTools list (3 s at most),
        else the last url the agent opened."""
        page = dict(getattr(self, "_last_page", None) or {})
        ex = getattr(self.computer, "execute", None)
        if ex is None:
            return page
        try:
            r = await asyncio.wait_for(ex("shell.exec", {"command": "curl -s -m 2 http://127.0.0.1:9222/json/list || curl -s -m 2 http://127.0.0.1:9110/json/list", "timeout": 5},
                                          observe=False, timeout=6), 6)
            import json as _json
            tabs = _json.loads(((r.result or {}).get("stdout") or "[]")) if r and r.success else []
            tabs = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"
                    and not str(t.get("url", "")).startswith(("devtools://", "chrome-extension://", "chrome://"))]
            if tabs:
                page = {"url": tabs[0].get("url") or "", "title": tabs[0].get("title") or ""}
        except Exception:  # noqa: BLE001  the card falls back to what the agent said
            pass
        return page

    async def _escalate(self, kind: str, detail: str) -> Optional[str]:
        """Instead of failing: ask for a takeover (captcha/login wall on the page) or ask the user what to do.
        Returns the note to continue with, or None when the task has ended (stopped by the user, nobody answered,
        or nobody can be asked) - always with a full report as the result. See escalate.py."""
        # a drawing task never opened a page: do not quote whatever tab the browser had open (task-41c showed JD)
        page = await self._page_now() if (getattr(self, "_last_page", None) or not self._cad_task) else {}
        stall = getattr(self, "_stall", None) or _es.Stall()
        self._stall = stall
        blocked = stall.blocked or ("验证" if kind == "blocked" else "")
        await self.emit(EventType.VALIDATION, success=False, change=0.0, message="需要你：" + _es.KIND_TEXT.get(kind, kind))
        if not self.ask:
            self._finish(TaskStatus.FAILED, _es.report(kind, detail, self.history, page, blocked))
            return None
        self._ask_forever = True   # the session's ask() waits without a deadline: the task pauses, never ends itself
        try:
            return await self._escalate_ask(kind, detail, page, stall, blocked)
        finally:
            self._ask_forever = False

    async def _escalate_ask(self, kind: str, detail: str, page: dict, stall, blocked: str) -> Optional[str]:
        if blocked:
            act = "slider" if re.search(r"滑块|拖动|slide", blocked, re.I) else "captcha"
            r = await self._request_takeover({"action": act, "url": page.get("url") or "",
                                              "what_to_do": f"在页面上完成「{blocked}」（滑块/图形验证码等），完成后点「完成，交还给 Aiw」",
                                              "reason": f"页面出现「{blocked}」，需要真人完成验证"},
                                             f"页面出现「{blocked}」验证，Aiw 过不去，需要你来完成")
            self.history.append(f"{self.step}. request_takeover(auto: {blocked}) -> {r}")
            if r.startswith("the user handled it"):
                stall.reset()
                return ("The user completed the verification on the desktop. Re-read the page now (bos_snapshot of the page "
                        "you were on) and continue the task from there.")
        q = _es.question(kind, detail, page, blocked)
        stuck = _es.stuck_card(kind, detail, blocked)
        while True:
            self._stuck = stuck
            await self.emit(EventType.INPUT_REQUIRED, question=q, options=_es.options(kind), stuck=stuck)
            t0 = time.monotonic()
            reply = await self._ask(q, _es.options(kind))
            tries = 0
            while reply is None and tries < int(os.environ.get("AIW_STUCK_REASK", "1000000")):
                # an ask channel without "wait forever" timed out: keep the task paused and ask again
                tries += 1
                await asyncio.sleep(min(60.0, 2.0 * tries))
                reply = await self._ask(q, _es.options(kind))
            self.add_wait(time.monotonic() - t0)
            self._stuck = None
            await self.emit(EventType.INPUT_ANSWERED, answer=reply)
            c = _es.choice(reply)
            if c == "takeover":
                r = await self._request_takeover({"action": "other", "url": page.get("url") or "",
                                                  "what_to_do": "在桌面上把卡住的这一步处理掉，完成后点「完成，交还给 Aiw」",
                                                  "reason": _es.KIND_TEXT.get(kind, "卡住了")}, detail or "")
                self.history.append(f"{self.step}. request_takeover(user chose to take over) -> {r}")
                if r.startswith("the user handled it"):
                    c, reply = "retry", "我已经在桌面上处理好了，接着做"
                else:   # the takeover was cancelled or not done: still paused, ask again (only 停止 ends the task)
                    q = "接管没有完成，任务还停在这里。\n" + _es.question(kind, detail, page, blocked)
                    continue
            break
        if c == "retry":
            if self.cfg.max_steps > 0:
                self.cfg.max_steps = max(self.cfg.max_steps, self.step + max(25, _es.step_cap()))
            if kind == "time":
                self.cfg.timeout_s += max(900.0, _es.time_cap())
                self._time_asked = False
                self._time_up = False
            self._mark_progress()
            self._rep, self._prev_sig = 0, None
            stall.reset()
            hint = "" if reply in (_es.OPT_RETRY, _es.OPT_FIXED, None) else f" The user said: {reply!r}. Follow it."
            self.history.append(f"{self.step}. (asked the user: {_es.KIND_TEXT.get(kind, kind)}; they chose to go on)")
            return (f"You were stuck ({kind}: {str(detail)[:200]}). The user wants you to continue.{hint} Do NOT repeat what "
                    "already failed: try a different approach. If a person is needed (captcha, login, SMS code), call "
                    "request_takeover; if you cannot go on, call ask_user and explain. Never give up silently.")
        self._finish(TaskStatus.STOPPED if c == "stop" else TaskStatus.FAILED,
                     _es.report(kind, detail, self.history, page, blocked, answer=reply if c == "stop" else None))
        return None

    async def _request_takeover(self, a: dict, intent: str) -> str:
        from tool_router.explain import takeover_card
        page = await self._page_now() if not (a.get("site") and a.get("url")) else {}
        info = takeover_card(a, intent, page)
        if not str(a.get("reason") or "").strip():
            info["reason"] = info["steps"]
        reason = info["title"].replace("需要你接管：", "", 1)
        await self.emit(EventType.INPUT_REQUIRED, question=reason, options=[], form=info)
        t0 = time.monotonic()
        reply = await self._ask(reason, [], info)
        self.add_wait(time.monotonic() - t0)
        cancelled = isinstance(reply, str) and reply.strip().lower() in ("__cancel__", "cancel", "取消")
        await self.emit(EventType.INPUT_ANSWERED, answer=reply, takeover=True, cancelled=cancelled, title=info["title"])
        await self.emit(EventType.ACTION_RESULT, tool="request_takeover", success=reply is not None and not cancelled,
                        error="用户取消了接管" if cancelled else None, result={})
        if reply is None:
            return "the user did not take over in time; try once more yourself, or ask_user explaining what blocks you"
        if cancelled:
            return ("the user cancelled the takeover and will not do this step. Do not request_takeover for it again; "
                    "find another way that does not need it, or ask_user explaining what is blocked.")
        return f"the user handled it on the desktop (said: {reply!r}). Take a screenshot and continue from what is shown now."

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
        if self._drawkit_built:
            # a generated drawing already passed drawkit's geometric checks; a manual tweak on it is the user's call
            issues = [i for i in issues if any(h in i for h in _cad._HARD)]
        # 3.8.7: only commands LibreCAD cannot take and geometry off the sheet / in the title block send a batch back.
        # Text overlap and the like are warnings: in task-a12, 35 of 122 batches were rejected for label overlap and
        # the agent spent dozens of steps nudging labels instead of drawing.
        hard = [i for i in issues if any(h in i for h in _cad._HARD)]
        key = self._cad_sheet
        if (bad or hard) and self._cad_rejects.get(key, 0) < int(os.environ.get("AIW_CAD_MAX_REJECTS", "2")):
            issues = hard + [i for i in issues if i not in hard]
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
            p = _ws_rel(p)
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
            rp = _ws_rel(rp)
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
            text = await _vg.read(self, eyes, _cad.READ_IMAGE_PROMPT, q, b64, 1800, self.cfg.vision_timeout_s * 3)
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=str(e)[:200], result={})
            return _vg.failed_text(e, "images")
        self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="read_image", success=True, error=None,
                        result={"compare": [p for p, _ in imgs], "chars": len(text or "")})
        return f"compared side by side ({labels}{'; zoomed on ' + str(list(zoom)) if zoom else ''}):\n{(text or '').strip()[:3500]}"

    async def _read_image_one(self, a: dict) -> str:
        """Read an image (or one PDF page) from ~/workspace and have the vision model describe it for drafting."""
        path = (a.get("path") or "").strip().lstrip("/")
        path = _ws_rel(path)
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
            text = await _vg.read(self, eyes, _cad.READ_IMAGE_PROMPT, q or "请按结构描述这张图。", b64, 1800,
                                  self.cfg.vision_timeout_s * 3)
        except Exception as e:  # noqa: BLE001
            await self.emit(EventType.ACTION_RESULT, tool="read_image", success=False, error=str(e)[:200], result={})
            return _vg.failed_text(e, "image")
        self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="read_image", success=True, error=None,
                        result={"path": path, "chars": len(text or "")})
        self._images_read[path] = (self.step, (text or "").strip(), q0)
        return f"image {path} reads:\n{(text or '').strip()[:3500]}"

    # ---- 3.8.7 drawing pipeline: drawing_read (one region, read twice) and drawing_build (spec -> whole drawing) ----
    async def _load_ws_image(self, path: str):
        f = await self.tools.run("send_file", {"path": path})
        if not f.success or not isinstance(f.result, dict) or not f.result.get("content_b64"):
            return None, f"cannot read {path} ({f.error})"
        try:
            return Image.open(io.BytesIO(base64.b64decode(f.result["content_b64"]))).convert("RGB"), None
        except Exception as e:  # noqa: BLE001
            return None, f"{path} is not an image ({e})"

    async def _drawing_read(self, a: dict) -> str:
        try:
            return await self._drawing_read_body(a)
        except _vis.VisionFailed as e:   # 3.8.8: service/config trouble is not a misread: say so, never "CONFLICT"
            await self.emit(EventType.ACTION_RESULT, tool="drawing_read", success=False, error=str(e)[:200], result={})
            return _vg.failed_text(e)

    async def _drawing_read_body(self, a: dict) -> str:
        """Zoom one region of the source (a dimension chain, a room, a label) and read it with the vision model TWICE,
        asked two different ways; a third, larger read only when they disagree. Never a guess: a disagreement comes
        back as a conflict to re-read. In task-a12 whole-photo questions gave 9300 one time and 9500 the next."""
        path = _ws_rel((a.get("path") or "").strip().lstrip("/"))
        box = a.get("box") or a.get("region")
        kind = str(a.get("kind") or "chain")
        if not path or not (isinstance(box, (list, tuple)) and len(box) == 4):
            await self.emit(EventType.ACTION_RESULT, tool="drawing_read", success=False, error="missing path/box", result={})
            return ("FAILED: drawing_read needs `path` (the source image) and `box` [x0,y0,x1,y1] in the image's pixels "
                    "(one dimension chain, one room or one label; read_image grid=true shows pixel coordinates).")
        img, err = await self._load_ws_image(path)
        if img is None:
            await self.emit(EventType.ACTION_RESULT, tool="drawing_read", success=False, error=err, result={})
            return "FAILED: " + err
        try:
            crop, vertical, k = _draw.crop_zoom(img, [float(v) for v in box], kind)
        except ValueError as e:
            await self.emit(EventType.ACTION_RESULT, tool="drawing_read", success=False, error=str(e), result={})
            return f"FAILED: {e}"
        eyes = self.vision_llm or self.llm
        sysmsg = "你是严谨的工程制图读图员。只读图上真实存在的内容，不推测，只输出要求的 JSON。"
        extra = (" " + str(a.get("question")).strip()) if a.get("question") else ""

        async def ask(q, im):
            try:
                return await _vg.read(self, eyes, sysmsg, q + extra, _draw.jpeg_b64(im), 600, self.cfg.vision_timeout_s * 2)
            except _vis.VisionFailed as e:
                if e.err.kind in ("service", "auth", "none"):
                    raise
                return f"(read failed: {e.err.where()})"
            except Exception as e:  # noqa: BLE001
                return f"(read failed: {type(e).__name__})"
        if kind == "chain":
            t1, t2 = await asyncio.gather(ask(_draw.CHAIN_Q[0], crop), ask(_draw.CHAIN_Q[1], crop))
            reads = [_draw.numbers_from(t1), _draw.numbers_from(t2)]
            v = _draw.chain_verdict(reads, vertical)
            if not v.get("ok"):
                big, _, _ = _draw.crop_zoom(img, [float(x) for x in box], kind, target=2600)
                reads.append(_draw.numbers_from(await ask(_draw.CHAIN_Q[2], big)))
                v = _draw.chain_verdict(reads, vertical)
            self._acted = True
            await self.emit(EventType.ACTION_RESULT, tool="drawing_read", success=bool(v.get("ok")), error=None,
                            result={"path": path, "box": list(box), "zoom": k, "vertical": vertical, **v})
            order = "top->bottom" if vertical else "left->right"
            self.history.append(f"{self.step}. drawing_read {path} {list(box)}: {v.get('reads')} -> "
                                f"{'AGREED ' + str(v.get('values')) if v.get('ok') else 'CONFLICT'}")
            if v.get("ok"):
                return (f"chain {list(box)} ({order}, zoom x{k}): values {v['values']}, total {v['total']} mm. "
                        f"Independent reads agree: {v['reads']}. Put them in the spec chain with reads={v['reads'][:2]} "
                        "and box_px set to this box; then check opposite chains add up to the same total.")
            return (f"CONFLICT on chain {list(box)} ({order}, zoom x{k}): the reads differ {v['reads']}"
                    + (f" at positions {v['differ_at']}" if v.get("differ_at") else "") +
                    ". Do NOT pick one. Re-read with a tighter box around the differing segment(s) (a few tick marks "
                    "only), and let `drawkit.py fit` measure the segment lengths from the tick pixels: a number that "
                    "does not fit its measured length is the misread one.")
        qs = _draw.TEXT_Q
        t1, t2 = await asyncio.gather(ask(qs[0], crop), ask(qs[1], crop))
        r1, r2 = _draw.texts_from(t1), _draw.texts_from(t2)
        both = [t for t in r1 if t in r2]
        only = sorted(set(r1) ^ set(r2))
        self._acted = True
        await self.emit(EventType.ACTION_RESULT, tool="drawing_read", success=True, error=None,
                        result={"path": path, "box": list(box), "texts": both, "unsure": only})
        return (f"texts in {list(box)} (zoom x{k}), read twice: agreed {both}"
                + (f"; seen in only one read (check again before using): {only}" if only else ""))

    async def _ensure_drawkit(self) -> str | None:
        ver = _draw.kit_version()
        if self._drawkit_ready == ver:
            return None
        files = _draw.kit_files()
        if "drawkit.py" not in files:
            return f"drawkit.py is missing on the server ({_draw.KIT_DIR})"
        r = await self.tools.run("shell", {"command": _draw.version_probe(), "timeout": 20}, intent="check the drawing kit")
        have = ((r.result or {}).get("stdout") or "").strip() if isinstance(r.result, dict) else ""
        if have != ver:
            for rel, txt in files.items():
                w = await self.tools.run("files_write", {"path": f"{_draw.REMOTE}/{rel}", "content": txt},
                                         intent="install the drawing kit")
                if not w.success:
                    return f"could not write {rel} on the computer ({w.error})"
        r = await self.tools.run("shell", {"command": _draw.install_command(ver), "timeout": 900},
                                 intent="install the drawing kit dependencies (ezdxf, Chinese font) if missing")
        if not r.success:
            res = r.result if isinstance(r.result, dict) else {}
            return f"drawing kit setup failed: {(res.get('stderr') or res.get('stdout') or r.error or '')[-400:]}"
        self._drawkit_ready = ver
        return None

    async def _drawing_build(self, a: dict, intent: str) -> str:
        """Regenerate the WHOLE drawing from its spec with drawkit.py (DXF + LibreCAD view copy + PNG + overlay on the
        source + report.json with the geometric checks), then show it in LibreCAD. Never a patch on the canvas."""
        spec = str(a.get("spec") or "cad/spec.json").strip().lstrip("/")
        spec = _ws_rel(spec)
        out = str(a.get("out") or (str(Path(spec).parent) if "/" in spec else "cad"))
        name = _cad.safe_name(a.get("name") or "plan")
        content = a.get("content")
        if content is not None:
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False, indent=1)
            try:
                json.loads(content)
            except ValueError as e:
                await self.emit(EventType.ACTION_RESULT, tool="drawing_build", success=False, error="spec is not JSON", result={})
                return f"FAILED: `content` is not valid JSON ({e}). Fix it and call drawing_build again."
            w = await self.tools.run("files_write", {"path": spec, "content": content}, intent="save the drawing spec")
            if not w.success:
                return f"FAILED: could not save {spec} ({w.error})"
        err = await self._ensure_drawkit()
        if err:
            await self.emit(EventType.ACTION_RESULT, tool="drawing_build", success=False, error=err[:200], result={})
            return f"FAILED: {err}"
        r = await self.tools.run("shell", {"command": _draw.build_command(spec, out, name, a.get("source"), task=self._root_id), "timeout": 600},
                                 intent=intent or "generate the drawing from the spec")
        res = r.result if isinstance(r.result, dict) else {}
        rep = _draw.parse_result(res.get("stdout") or "")
        if not rep:
            tail = ((res.get("stderr") or "") + (res.get("stdout") or ""))[-1500:]
            await self.emit(EventType.ACTION_RESULT, tool="drawing_build", success=False, error=tail[-200:], result={})
            return f"FAILED: drawkit could not build {spec}:\n{tail}\nFix the spec (see `drawkit.py schema`) and call again."
        if rep.get("stale"):
            await self.emit(EventType.ACTION_RESULT, tool="drawing_build", success=False, error="stale spec", result={})
            return ("FAILED: " + " | ".join(rep.get("errors") or []) + f"\nNEXT: move {spec} away (it is another image's or "
                    "another task's), then drawing_auto for a draft of THIS image, correct it, drawing_build.")
        self._draw_rounds += 1
        self._drawkit_built = True
        self._acted = True
        files = rep.get("files") or {}
        rel = lambda f: f if (f.startswith(out + "/") or f.startswith("/")) else f"{out}/{f}"   # noqa: E731
        self._cad_file = rel(files.get("dxf") or name + ".dxf")
        if rep.get("ok") or self._draw_rounds >= _draw.MAX_ROUNDS:
            self._cad_pdf_ok = True      # the deliverable is the generated set; no LibreCAD-typed PDF required
            self._sheet_audit = {}
        if self._cad_task and files.get("view_dxf"):
            try:   # show it to the user in LibreCAD (the viewer / small-tweak editor, not the drawing engine)
                await self._type_cad([], rel(files["view_dxf"]), "open the generated drawing in LibreCAD")
            except Exception:  # noqa: BLE001
                pass
        await self.emit(EventType.ACTION_RESULT, tool="drawing_build", success=bool(rep.get("ok")), error=None,
                        result={k: rep.get(k) for k in ("ok", "scale", "errors", "warnings", "overlay_score", "files")})
        text = _draw.summary(rep, self._draw_rounds, out)
        png, ov = rel(files.get("png") or name + ".png"), (rel(files["overlay"]) if files.get("overlay") else None)
        if rep.get("ok"):
            nxt = (f"\nNEXT: " + (f"read_image path={ov} once with a pointed question (红色墙线与照片的墙哪里没对上？哪个房间名放错了？"
                                    f"有没有漏掉的门窗/房间？). " if ov and self._draw_rounds <= _draw.MAX_ROUNDS else "")
                   + f"If it matches, send_file {png} and {self._cad_file} and finish with done (chains read, any numbers "
                   "that differ from what the user said, the checks, what is still uncertain). If something is off, fix "
                   "cad/spec.json and call drawing_build again (the whole drawing is regenerated; never ERASE).")
        elif self._draw_rounds >= _draw.MAX_ROUNDS:
            nxt = (f"\nSTOP ITERATING ({self._draw_rounds} rounds): send_file {png} and {self._cad_file} and finish with done, "
                   "listing the remaining check errors and what is uncertain. Do not loop further.")
        else:
            nxt = ("\nNEXT: fix these in cad/spec.json (a misread chain number: re-read that chain with drawing_read; a text "
                   "overlap: move or shorten that item, or give the room label_at), then drawing_build again.")
        self.history.append(f"{self.step}. drawing_build {spec}: {'ok' if rep.get('ok') else str(len(rep.get('errors') or [])) + ' errors'}")
        return text + nxt

    def _hidden_tools(self) -> tuple:
        hide = [] if _cad.script_drawing_allowed(self.task.prompt) else ["cad_draw"]
        hide.extend(_dh.hidden(self))
        if not self.dispatcher:
            hide.append("dispatch")
        if not self.bos_enabled:
            hide.extend(BOS_TOOLS)
        return tuple(hide)

    async def _dispatch(self, a: dict) -> str:
        """Run independent pieces of the request as child tasks on several computers at once and wait for them all.
        The API's session manager does the placement, queueing, waking and stopping; this side checks the arguments,
        keeps the parent's own deadline from running out while it waits, and turns the results into a history note."""
        items = a.get("tasks") if isinstance(a.get("tasks"), list) else a.get("items")
        clean = []
        for it in items or []:
            if isinstance(it, str):
                it = {"goal": it}
            if not isinstance(it, dict):
                continue
            goal = str(it.get("goal") or it.get("task") or "").strip()
            if goal:
                clean.append({"machine": str(it.get("machine") or it.get("machine_id") or "auto").strip() or "auto",
                              "goal": goal[:4000]})
        if not self.dispatcher:
            await self.emit(EventType.ACTION_RESULT, tool="dispatch", success=False, error="no other computers", result={})
            return ("FAILED: dispatch is not available here (only one computer, or this is already a child task). "
                    "Do the pieces yourself one after another.")
        if not clean:
            await self.emit(EventType.ACTION_RESULT, tool="dispatch", success=False, error="missing args ['tasks']", result={})
            return ("FAILED: dispatch needs `tasks`: a list of {machine: id or \"auto\", goal: complete instruction}. "
                    "Call it again with the pieces filled in.")
        clean = clean[:int(os.environ.get("AIW_DISPATCH_MAX", "8"))]
        try:
            mins = float(a.get("timeout_min") or 0)
        except (TypeError, ValueError):
            mins = 0
        timeout_s = min(max(mins * 60 if mins > 0 else float(os.environ.get("AIW_DISPATCH_TIMEOUT", "3600")), 60.0),
                        float(os.environ.get("AIW_DISPATCH_MAX_TIMEOUT", "10800")))
        t0 = time.monotonic()
        try:
            results = await self.dispatcher(clean, timeout_s)
        finally:
            # waiting on the children is not this task's own work: it must not eat the parent's deadline
            self.add_wait(time.monotonic() - t0)
        self._acted = True
        ok = sum(1 for r in results if r.get("status") == "succeeded")
        await self.emit(EventType.ACTION_RESULT, tool="dispatch", success=ok > 0, error=None if ok else "no child task succeeded",
                        result={"children": [{k: r.get(k) for k in ("machine", "machine_name", "status", "task_id")} for r in results]})
        lines = [f"dispatch finished: {ok}/{len(results)} child tasks succeeded."]
        for i, r in enumerate(results, 1):
            files = r.get("local_files") or []
            lines.append(f"[{i}] {r.get('machine_name') or r.get('machine')} ({r.get('machine')}) {r.get('status')}"
                         f"{' (ran after the others: computer was busy)' if r.get('queued') else ''}: "
                         f"goal={_clip(r.get('goal') or '', 160)!r}\n    summary: {_clip(r.get('summary') or '', 900)}"
                         + (f"\n    files (on your computer): {', '.join(files)}" if files else
                            f"\n    files (in the child's chat only): {', '.join(r.get('files') or [])}" if r.get("files") else ""))
        lines.append("Now combine these results into ONE answer for the user: merge the files if a single file was asked "
                     "for and send_file it, then done with a summary that names what each computer did. Re-dispatch or do "
                     "yourself only the pieces that failed.")
        return "\n".join(lines)

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
_RISKY_EXT = _re.compile(r"(send|delete|remove|pay|purchase|order|post|publish|transfer|create|update|write|cancel|deploy)", _re.I)


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
    elif outcome.action == "browseros.call" and isinstance(outcome.result, dict):
        el = outcome.result.get("element")
        res = (f" on [{el}]" if el else "") + (
            " result:\n" + _clip(outcome.result.get("text") or "", int(os.environ.get("AIW_BOS_HIST_CHARS", "8000")))
            if outcome.success else "")
        if decision.tool == "bos_act" and isinstance(decision.args, dict) and decision.args.get("secret"):
            args = {k: ("••••" if k in ("text", "value", "fields") else v) for k, v in args.items() if k != "secret"}
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


# needs the screen: a browser, a website, a desktop app, or the user pointing at something on screen
_GUI_TASK = re.compile(
    r"打开|点击|点一下|双击|浏览器|网页|网站|网址|chrome|firefox|桌面|窗口|屏幕|截图|截屏|界面|登录|登陆|注册|下单|购买|预订|"
    r"填写|表单|上网|搜索|百度|谷歌|google|bing|淘宝|京东|微信|小红书|抖音|b站|bilibili|youtube|"
    r"https?://|www\.|\.com\b|\.cn\b|librecad|gimp|inkscape|libreoffice|wps|photopea|vs ?code|应用|软件里|app\b|"
    r"\bopen\b|\bclick\b|\bbrowse|\bwebsite|\bdesktop|\bscreen|\blog ?in\b|\bsign ?in\b|\bsearch\b", re.I)
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


_WS_PREFIX = re.compile(r"^(?:~/|\$HOME/|\$\{HOME\}/|/?(?:data/home|home/[^/]+|root)/)?workspace/")


def _ws_rel(p: str) -> str:
    """A path relative to ~/workspace, whatever home the model wrote (/data/home, /home/user, /home/aiw, ~, $HOME)."""
    p = str(p or "").strip()
    q = _WS_PREFIX.sub("", p, count=1)
    return q.lstrip("/") if q != p else p.lstrip("/")


_GUESSED_HOME = re.compile(r"(/home/(?:user|aiw|ubuntu|debian)/)workspace(?=/|\b)")
_NO_SCREEN_TOOLS = {"read_image", "image_edit", "shell", "files_read", "files_write", "files_list", "files_download", "send_file", "delegate", "dispatch",
                    # 3.8.9 (task-3fc): file-only drawing steps; a desktop look + vision description cost 12-15 s each
                    "drawing_auto", "drawing_build", "drawing_note", "drawing_read"}
_OCR_RE = re.compile(r"\b(tesseract|rapidocr|paddleocr|easyocr)\b", re.I)
_NO_REPEAT = {"files_write", "shell", "browser_open", "send_file", "files_download", "delegate", "dispatch", "open_app"}


def _thumb(b64: str):
    try:
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("L").resize((64, 40)).tobytes()
    except Exception:  # noqa: BLE001
        return None


def json_short(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)[:600]
