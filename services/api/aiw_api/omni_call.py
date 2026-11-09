"""v3.9.31: omni realtime voice / video call (视频通话, like 豆包): the phone or browser streams microphone audio and a
camera frame a second, the full-modality model (Qwen-Omni) answers with speech, and the call can hand work to aiw.

  WS /ws/omni-call?token=<API token>   (same token as /ws/events, /ws/device and every API call)
    app -> {"type":"start","video":true,"voice":"Tina"?}
    app -> {"type":"audio","data":"<base64 pcm16 16 kHz mono>"}   20-100 ms a chunk
    app -> {"type":"frame","data":"<base64 jpeg>"}                ≤1 fps, ≤720p (the server drops faster frames)
    app -> {"type":"interrupt"} | {"type":"hangup"} | {"type":"ping"}
    srv -> {"type":"ready","mode":"realtime"|"turn","model","voice","audio_out":bool,"max_seconds","note"?}
    srv -> {"type":"audio","data":"<base64 pcm16 24 kHz mono>"}
    srv -> {"type":"transcript","role":"user"|"assistant","text","final":bool,"speak"?:true}
           text is the whole utterance so far; speak=true: no audio came for it, the client reads it out itself
    srv -> {"type":"speech_started"}   the user started talking: stop playback now (barge-in)
    srv -> {"type":"tool","name","status":"running"|"done"|"error","text"?}
    srv -> {"type":"error","message","code"?} | {"type":"ended","reason"?}

Two backends behind that one protocol:
  realtime  a server-side WebSocket client to an OpenAI-Realtime-style endpoint (DashScope Qwen-Omni-Realtime:
            wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=… or the workspace host
            wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime, which qwen3.8-omni-flash-realtime
            requires). session.update (voice, pcm16 16k in / pcm 24k out, server_vad, input transcription, tools),
            input_audio_buffer.append, input_image_buffer.append (jpeg, only after some audio), response.audio.delta,
            response.audio_transcript.delta/done, input_audio_buffer.speech_started, response.function_call_arguments.done
            -> conversation.item.create(function_call_output) + response.create.
  turn      works on any OpenAI-compatible gateway: an energy VAD with hangover cuts the user's speech; each utterance
            goes to chat/completions (stream=true, modalities ["text","audio"], audio {voice, format}) as input_audio
            (wav) + the latest camera frame + the recent call; audio deltas (delta.audio.data, pcm16 24 kHz) stream back.
            New speech cancels the answer in flight. A model that only outputs text (qwen3.8-omni-flash) is detected
            and its sentences are read by models.omni_tts_model (an audio-output omni model) or by the client itself.
"auto" (models.omni_mode, default) tries realtime with a short timeout and falls back to turn, telling the client.

Settings (kv "models"): omni_model (empty = calls off), omni_voice (default Tina), omni_realtime_url, omni_realtime_model,
omni_tts_model, omni_mode (auto | realtime | turn), omni_max_minutes (30). Secret omni_realtime_key (else the gateway key).
Tools from a call are deliberately few: create_aiw_task (a normal aiw task via Chat.start_task, whose own confirm cards
still apply) and task_status. The call's transcript lands in the 小秘 chat as one message at hang-up.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import time
import wave
from array import array
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, WebSocket, WebSocketDisconnect   # module level: annotations are resolved here

log = logging.getLogger("aiw.omni")

DEFAULT_VOICE = "Tina"          # default of Qwen3.8-Omni / Qwen3.5-Omni (Cherry is Qwen3-Omni only)
DASHSCOPE_RT = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
VOICES = ["Tina", "Cindy", "Liora Mira", "Raymond", "Zane", "Katerina", "Ryan", "Mia", "Cici", "Theo Calm", "Serena",
          "Maia", "Evan", "Qiao", "Momo", "Wil", "Angel", "Jennifer", "Aiden", "Sunny", "Dylan", "Eric", "Peter", "Rocky",
          "Kiki", "Ethan", "Cherry"]
IN_RATE, OUT_RATE = 16000, 24000
MAX_FRAME_B64 = 256 * 1024      # DashScope: one image ≤256 KB after base64
MAX_MSG = 512 * 1024
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None   # tests: httpx.MockTransport
_RT_FAIL: dict = {}             # (url, model) -> time realtime last failed (auto mode skips it for RT_SKIP_S)
_TURN_VARIANT: dict = {}        # model -> index into TURN_VARIANTS that worked
_ACTIVE: set = set()
TURN_VARIANTS = ({"audio": True, "tools": True, "extras": True}, {"audio": True, "tools": False, "extras": False},
                 {"audio": False, "tools": False, "extras": False})


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------------------------------------------------- config
def realtime_url_for(base: str) -> str:
    """Default realtime endpoint from the gateway address: a DashScope host (dashscope*.aliyuncs.com or a workspace
    *.maas.aliyuncs.com host) keeps its host; anything else (a LAN gateway) gets the DashScope Beijing default."""
    host = (urlsplit(base or "").hostname or "").lower()
    if host.endswith(".maas.aliyuncs.com") or host in ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"):
        return f"wss://{host}/api-ws/v1/realtime"
    return DASHSCOPE_RT


def realtime_model_for(model: str) -> str:
    m = (model or "").strip()
    return m if "realtime" in m else (m + "-realtime" if m else "")


def with_model(url: str, model: str) -> str:
    if not model or re.search(r"[?&]model=", url):
        return url
    return url + ("&" if "?" in url else "?") + "model=" + model


def config(store, secrets) -> dict:
    m = store.kv_get("models", {}) or {}
    base = (m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    key = (secrets.get("openai_api_key") if secrets else None) or os.environ.get("OPENAI_API_KEY") or ""
    model = str(m.get("omni_model") or "").strip()
    rt_url = str(m.get("omni_realtime_url") or "").strip() or realtime_url_for(base)
    if rt_url.startswith("http"):
        rt_url = "ws" + rt_url[4:]
    mode = str(m.get("omni_mode") or "auto").strip().lower()
    try:
        minutes = float(m.get("omni_max_minutes") or 30)
    except (TypeError, ValueError):
        minutes = 30.0
    return {"model": model, "voice": str(m.get("omni_voice") or "").strip() or DEFAULT_VOICE,
            "base_url": base, "key": key,
            "rt_url": rt_url, "rt_model": str(m.get("omni_realtime_model") or "").strip() or realtime_model_for(model),
            "rt_key": (secrets.get("omni_realtime_key") if secrets else None) or key,
            "tts_model": str(m.get("omni_tts_model") or "").strip(),
            "mode": mode if mode in ("auto", "realtime", "turn") else "auto",
            "max_seconds": max(60.0, min(minutes, 240.0) * 60),
            "rt_timeout": _env_f("AIW_OMNI_RT_TIMEOUT", 5.0), "max_fps": _env_f("AIW_OMNI_MAX_FPS", 1.0),
            "idle_s": _env_f("AIW_OMNI_IDLE", 30.0), "audio_format": os.environ.get("AIW_OMNI_AUDIO_FORMAT", "wav")}


def public(cfg: dict, store=None) -> dict:
    """What GET /api/omni/status shows (no keys)."""
    out = {"enabled": bool(cfg["model"]), "model": cfg["model"], "voice": cfg["voice"], "voices": VOICES,
           "realtime_url": cfg["rt_url"], "realtime_model": cfg["rt_model"], "tts_model": cfg["tts_model"],
           "mode": cfg["mode"], "max_minutes": int(cfg["max_seconds"] // 60), "has_realtime_key": bool(cfg["rt_key"])}
    if not cfg["model"]:
        out["message"] = "还没有设置通话模型：设置 › 模型 › 通话模型（全模态），如 qwen3.8-omni-flash"
    if store is not None:
        out["probe"] = (store.kv_get("omni_probe", {}) or {}).get(cfg["model"])
    return out


# ---------------------------------------------------------------------------------------------------------- audio
def b64d(s: Any) -> Optional[bytes]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def to_wav(pcm: bytes, rate: int = IN_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def strip_wav(b: bytes) -> bytes:
    """A first audio chunk that is a whole WAV file: keep only the samples after its "data" header."""
    if b[:4] == b"RIFF":
        i = b.find(b"data", 12)
        if i >= 0:
            return b[i + 8:]
    return b


def rms(pcm: bytes) -> float:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    return (sum(x * x for x in a) / len(a)) ** 0.5


class EnergyVAD:
    """Energy VAD with an adaptive noise floor, a short pre-roll and a hangover: feed pcm16 16 kHz, get events
    ("start", None) when speech begins and ("end", pcm of the utterance incl. pre-roll) when it stops.
    boost > 1 raises the bar while the assistant's own voice may still be coming out of the speaker."""
    FRAME = 320 * 2   # 20 ms

    def __init__(self, start_ms: int = 80, hang_ms: int = 700, pre_ms: int = 300, min_ms: int = 250,
                 max_ms: int = 30000, floor: float = 350.0, ratio: float = 3.0):
        self.start_n, self.hang_n = start_ms // 20, hang_ms // 20
        self.pre_n, self.min_n, self.max_n = pre_ms // 20, min_ms // 20, max_ms // 20
        self.floor, self.ratio = floor, ratio
        self.noise = floor / 2
        self.boost = 1.0
        self.buf = b""
        self.pre: list[bytes] = []
        self.speech = False
        self.run = 0          # consecutive loud frames before start / quiet frames after
        self.voiced = 0
        self.seg: list[bytes] = []

    def threshold(self) -> float:
        return max(self.floor, self.noise * self.ratio) * self.boost

    def feed(self, pcm: bytes) -> list[tuple]:
        out = []
        self.buf += pcm
        while len(self.buf) >= self.FRAME:
            fr, self.buf = self.buf[:self.FRAME], self.buf[self.FRAME:]
            e = rms(fr)
            loud = e > self.threshold()
            if not self.speech:
                if not loud:   # learn the room only from quiet frames
                    self.noise = 0.95 * self.noise + 0.05 * e
                self.pre = (self.pre + [fr])[-(self.pre_n + self.start_n):]
                self.run = self.run + 1 if loud else 0
                if self.run >= self.start_n:
                    self.speech, self.seg, self.voiced, self.run = True, list(self.pre), self.run, 0
                    self.pre = []
                    out.append(("start", None))
                continue
            self.seg.append(fr)
            if loud:
                self.voiced += 1
                self.run = 0
            else:
                self.run += 1
            if self.run >= self.hang_n or len(self.seg) >= self.max_n:
                seg, voiced = b"".join(self.seg), self.voiced
                self.speech, self.seg, self.run, self.voiced = False, [], 0, 0
                out.append(("end", seg if voiced >= self.min_n else b""))
        return out


# ---------------------------------------------------------------------------------------------------------- tools
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "create_aiw_task",
        "description": "把需要在电脑上完成的事（查资料整理成文档、操作软件、画图、写代码等较长的工作）交给 aiw 作为一个新任务执行，"
                       "完成后会推送给用户。只在用户明确要你去做某件事时调用。",
        "parameters": {"type": "object", "properties": {
            "goal": {"type": "string", "description": "任务目标，用一两句完整中文写清楚要做什么、交付什么"}},
            "required": ["goal"]}}},
    {"type": "function", "function": {
        "name": "task_status",
        "description": "查询 aiw 任务的最新进度（不传 task_id 时返回最近的几个任务）",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "任务 id，可选"}}, "required": []}}},
]


class ToolBridge:
    """The two functions a call may use. Nothing here runs a command: a task goes through the normal chat path
    (Chat.start_task), so its computer, routing and confirm cards are the same as a typed request."""
    MAX_TASKS = 3

    def __init__(self, chat):
        self.chat = chat
        self.tasks: list[str] = []

    async def run(self, name: str, args: Any) -> str:
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {}
        args = args if isinstance(args, dict) else {}
        try:
            if name == "create_aiw_task":
                return await self._create(str(args.get("goal") or "").strip())
            if name == "task_status":
                return await self._status(str(args.get("task_id") or "").strip())
        except Exception as e:  # noqa: BLE001  a tool failure is spoken, never kills the call
            log.warning("omni tool %s: %s", name, e)
            return f"没办成：{type(e).__name__}"
        return f"没有这个工具：{name}"

    async def _create(self, goal: str) -> str:
        if not goal:
            return "没听清要做什么，请用户再说一遍任务目标"
        if self.chat is None:
            return "现在交不了任务（聊天未就绪）"
        if len(self.tasks) >= self.MAX_TASKS:
            return "这次通话已经交了 3 个任务，先等它们做完"
        card = await self.chat.start_task(goal[:2000], origin="call", source="chat")
        d = (card or {}).get("data") or {}
        if not d.get("task_id"):
            return "没能交给 aiw：" + str((card or {}).get("text") or "")[:120]
        self.tasks.append(d["task_id"])
        return f"已交给 aiw，完成后推送（任务「{d.get('title') or goal[:20]}」，id {d['task_id']}）"

    async def _status(self, task_id: str) -> str:
        if self.chat is None:
            return "查不到任务"
        raw = await self.chat._task_status({"task_id": task_id} if task_id else {})
        try:
            tasks = (json.loads(raw) or {}).get("tasks") or []
        except ValueError:
            return str(raw)[:400]
        if not tasks:
            return "没有找到任务"
        return "；".join(f"「{t.get('title')}」{t.get('status')}" + (f"：{str(t.get('result') or '')[:80]}" if t.get("result") else "")
                        for t in tasks[:3])


def instructions_for(chat, video: bool) -> str:
    name = "小秘"
    who, recent = "", ""
    if chat is not None:
        try:
            name = chat.agent_name()
            prof = chat.store.kv_get("profile", {}) or {}
            if prof.get("name"):
                who = f"用户叫{prof['name']}。"
            recent = chat.recent_text(6)[-1500:]
        except Exception:  # noqa: BLE001
            pass
    s = (f"你是{name}，正在和用户{'视频' if video else '语音'}通话。{who}用自然口语回答，简短（通常一到三句），"
         "不要用 Markdown、列表、表情符号或网址。")
    if video:
        s += "你能看到用户摄像头的画面，问到眼前的东西时结合画面回答，看不清就直说。"
    s += ("需要在电脑上做的较长工作（查资料写文档、操作软件、画图、写代码）调用 create_aiw_task 交给 aiw，然后告诉用户已交给 aiw、"
          "完成后会推送；问任务进度时调用 task_status。不要假装已经做完了没做的事。")
    if recent:
        s += "\n\n" + recent
    return s


# ---------------------------------------------------------------------------------------------------------- realtime
Emit = Callable[[dict], Awaitable[None]]


async def ws_connect(url: str, key: str, timeout: float):
    """websockets >= 13: the asyncio client (additional_headers); older: the legacy one (extra_headers)."""
    hdr = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        from websockets.asyncio.client import connect
        return await asyncio.wait_for(connect(url, additional_headers=hdr, open_timeout=timeout, max_size=8 * 1024 * 1024,
                                              ping_interval=20), timeout + 1)
    except ImportError:   # pragma: no cover
        import websockets
        return await asyncio.wait_for(websockets.connect(url, extra_headers=hdr, open_timeout=timeout,
                                                         max_size=8 * 1024 * 1024), timeout + 1)


class RealtimeBackend:
    mode = "realtime"

    def __init__(self, cfg: dict, emit: Emit, bridge: Optional[ToolBridge], instructions: str, video: bool):
        self.cfg, self.emit, self.bridge = cfg, emit, bridge
        self.instructions, self.video = instructions, video
        self.ws = None
        self.reader: Optional[asyncio.Task] = None
        self.responding = False
        self.audio_sent = False
        self.pending_frame: Optional[str] = None
        self.said = ""            # assistant transcript of the response being spoken
        self.heard = ""
        self.closed = False
        self.audio_out = True
        self.jobs: set = set()

    def session(self, tools: bool, asr: bool = True) -> dict:
        v = self.cfg["voice"]
        s = {"modalities": ["text", "audio"], "instructions": self.instructions, "voice": v,
             "input_audio_format": "pcm16", "output_audio_format": "pcm16",
             "audio": {"input": {"format": {"type": "pcm", "sample_rate": IN_RATE}},
                       "output": {"voice": v, "format": {"type": "pcm", "sample_rate": OUT_RATE}}},
             "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300, "silence_duration_ms": 800}}
        if asr:   # the user's words for captions / the transcript (DashScope's realtime ASR model)
            s["input_audio_transcription"] = {"model": os.environ.get("AIW_OMNI_ASR_MODEL", "qwen3-asr-flash-realtime")}
        if tools and self.bridge is not None:
            s["tools"] = TOOL_SPECS
        return s

    async def _send(self, ev: dict):
        if self.ws is not None and not self.closed:
            await self.ws.send(json.dumps(ev, ensure_ascii=False))

    async def _recv(self, timeout: float) -> dict:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        return json.loads(raw)

    async def _wait(self, want: str, timeout: float) -> dict:
        end = time.time() + timeout
        while True:
            ev = await self._recv(max(0.1, end - time.time()))
            if ev.get("type") == "error":
                raise RuntimeError(str((ev.get("error") or {}).get("message") or ev.get("error") or "error")[:200])
            if ev.get("type") == want:
                return ev

    async def start(self):
        url = with_model(self.cfg["rt_url"], self.cfg["rt_model"])
        t = self.cfg["rt_timeout"]
        self.ws = await ws_connect(url, self.cfg["rt_key"], t)
        try:
            await self._wait("session.created", t)
            # a model without function calling, then an endpoint without that ASR model: retry with less
            for i, (tools, asr) in enumerate(((True, True), (False, True), (False, False))):
                await self._send({"type": "session.update", "session": self.session(tools=tools, asr=asr)})
                try:
                    await self._wait("session.updated", t)
                    break
                except RuntimeError:
                    if i == 2:
                        raise
        except BaseException:
            await self.close()
            raise
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                await self._on(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("omni realtime read: %s", e)
        if not self.closed:
            self.closed = True
            await self.emit({"type": "_closed"})

    async def _on(self, ev: dict):
        t = ev.get("type") or ""
        if t == "response.created":
            self.responding, self.said = True, ""
        elif t == "response.audio.delta" and ev.get("delta"):
            await self.emit({"type": "audio", "data": ev["delta"]})
        elif t in ("response.audio_transcript.delta", "response.text.delta"):
            self.said += str(ev.get("delta") or "")
            await self.emit({"type": "transcript", "role": "assistant", "text": self.said, "final": False})
        elif t in ("response.audio_transcript.done", "response.text.done"):
            txt = str(ev.get("transcript") or ev.get("text") or self.said)
            if txt.strip():
                await self.emit({"type": "transcript", "role": "assistant", "text": txt, "final": True})
            self.said = ""
        elif t == "response.done":
            self.responding = False
            if self.said.strip():   # cancelled before its transcript was done
                await self.emit({"type": "transcript", "role": "assistant", "text": self.said + "…", "final": True})
                self.said = ""
        elif t == "input_audio_buffer.speech_started":
            await self.emit({"type": "speech_started"})
            if self.responding:
                await self.interrupt()
        elif t == "conversation.item.input_audio_transcription.delta":
            self.heard = str(ev.get("text") or "") + str(ev.get("stash") or "") or (self.heard + str(ev.get("delta") or ""))
            await self.emit({"type": "transcript", "role": "user", "text": self.heard, "final": False})
        elif t == "conversation.item.input_audio_transcription.completed":
            txt = str(ev.get("transcript") or self.heard)
            self.heard = ""
            if txt.strip():
                await self.emit({"type": "transcript", "role": "user", "text": txt, "final": True})
        elif t == "response.function_call_arguments.done":
            j = asyncio.create_task(self._tool(str(ev.get("name") or ""), ev.get("arguments") or "{}", str(ev.get("call_id") or "")))
            self.jobs.add(j)
            j.add_done_callback(self.jobs.discard)
        elif t == "error":
            err = ev.get("error") or {}
            msg = str(err.get("message") if isinstance(err, dict) else err)[:200]
            if "no active response" not in msg.lower() and "cancel" not in msg.lower():
                await self.emit({"type": "error", "message": "模型报错：" + msg})

    async def _tool(self, name: str, args: Any, call_id: str):
        await self.emit({"type": "tool", "name": name, "status": "running"})
        out = await self.bridge.run(name, args) if self.bridge else "工具不可用"
        await self.emit({"type": "tool", "name": name, "status": "done", "text": out[:200]})
        await self._send({"type": "conversation.item.create",
                          "item": {"type": "function_call_output", "call_id": call_id, "output": out}})
        await self._send({"type": "response.create"})

    async def audio(self, pcm_b64: str, pcm: bytes):
        await self._send({"type": "input_audio_buffer.append", "audio": pcm_b64})
        if not self.audio_sent:
            self.audio_sent = True
            if self.pending_frame:   # DashScope wants some audio before the first image
                f, self.pending_frame = self.pending_frame, None
                await self.frame(f)

    async def frame(self, jpeg_b64: str):
        if not self.audio_sent:
            self.pending_frame = jpeg_b64
            return
        await self._send({"type": "input_image_buffer.append", "image": jpeg_b64})

    async def interrupt(self):
        if self.responding:
            self.responding = False
            await self._send({"type": "response.cancel"})

    async def close(self):
        self.closed = True
        for j in list(self.jobs):
            j.cancel()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
        if self.ws is not None:
            try:
                await asyncio.wait_for(self.ws.close(), 2)
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------------------------------------- turn
class HTTPFail(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status, self.text = status, text


SENT_END = re.compile(r"[。！？!?；;\n]")


class TurnBackend:
    mode = "turn"
    TURNS = 6

    def __init__(self, cfg: dict, emit: Emit, bridge: Optional[ToolBridge], instructions: str, video: bool):
        self.cfg, self.emit, self.bridge = cfg, emit, bridge
        self.instructions, self.video = instructions, video
        self.vad = EnergyVAD()
        self.turns: list[dict] = []
        self.jpeg: Optional[str] = None   # the latest camera frame (base64), sent with the next utterance
        self.jpeg_at = 0.0
        self.job: Optional[asyncio.Task] = None
        self.side: set = set()
        self.speaking_until = 0.0
        self.carry = b""
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), transport=TRANSPORT)
        v = _TURN_VARIANT.get(cfg["model"], 0)
        self.audio_out = TURN_VARIANTS[v]["audio"] or bool(cfg["tts_model"])

    async def start(self):
        return None

    # -- input
    async def audio(self, pcm_b64: str, pcm: bytes):
        self.vad.boost = 2.0 if time.time() < self.speaking_until else 1.0
        for ev, seg in self.vad.feed(pcm):
            if ev == "start":
                await self.interrupt()
                await self.emit({"type": "speech_started"})
            elif seg:
                self.job = asyncio.create_task(self._reply(seg))

    async def frame(self, jpeg_b64: str):
        self.jpeg, self.jpeg_at = jpeg_b64, time.time()

    async def interrupt(self):
        j, self.job = self.job, None
        if j and not j.done():
            j.cancel()
            try:
                await j
            except BaseException:  # noqa: BLE001
                pass
        self.speaking_until = 0.0

    async def close(self):
        await self.interrupt()
        for j in list(self.side):
            j.cancel()
        await self.client.aclose()

    # -- output
    async def _audio_out(self, b64: str, first: bool):
        raw = b64d(b64)
        if not raw:
            return
        if first:
            raw = strip_wav(raw)
        raw = self.carry + raw
        cut = len(raw) // 2 * 2
        self.carry = raw[cut:]
        if cut:
            now = time.time()
            self.speaking_until = max(self.speaking_until, now) + cut / (OUT_RATE * 2)
            await self.emit({"type": "audio", "data": b64e(raw[:cut])})

    # -- one utterance
    async def _reply(self, seg: bytes):
        wav_b64 = b64e(to_wav(seg))
        turn = {"user": None, "assistant": ""}
        hist = self._history()
        self.turns.append(turn)
        if os.environ.get("AIW_OMNI_TURN_ASR", "1") != "0":
            j = asyncio.create_task(self._transcribe(wav_b64, turn))
            self.side.add(j)
            j.add_done_callback(self.side.discard)
        content: list = [{"type": "input_audio", "input_audio": {"data": "data:;base64," + wav_b64, "format": "wav"}}]
        if self.video and self.jpeg and time.time() - self.jpeg_at < 10:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + self.jpeg}})
        msgs = [{"role": "system", "content": self.instructions}] + hist + [{"role": "user", "content": content}]
        try:
            text = await self._answer(msgs, turn)
            turn["assistant"] = text
        except asyncio.CancelledError:
            if turn["assistant"]:
                turn["assistant"] += "…"
                await self.emit({"type": "transcript", "role": "assistant", "text": turn["assistant"], "final": True})
            raise
        except HTTPFail as e:
            await self.emit({"type": "error", "message": f"模型请求失败（HTTP {e.status}）：{e.text[:160]}"})
        except Exception as e:  # noqa: BLE001
            log.warning("omni turn: %s", e)
            await self.emit({"type": "error", "message": f"模型请求失败：{type(e).__name__}: {str(e)[:160]}"})

    def _history(self) -> list[dict]:
        out = []
        for t in self.turns[-self.TURNS:]:
            out.append({"role": "user", "content": t["user"] or "（一段语音）"})
            if t["assistant"]:
                out.append({"role": "assistant", "content": t["assistant"]})
        return out

    def _body(self, msgs: list, v: dict, model: Optional[str] = None, tools: bool = True) -> dict:
        b = {"model": model or self.cfg["model"], "messages": msgs, "stream": True,
             "stream_options": {"include_usage": True}}
        if v["audio"]:
            b["modalities"] = ["text", "audio"]
            b["audio"] = {"voice": self.cfg["voice"], "format": self.cfg["audio_format"]}
        else:
            b["modalities"] = ["text"]
        if v["tools"] and tools and self.bridge is not None:
            b["tools"] = TOOL_SPECS
        if v["extras"]:
            b["enable_thinking"] = False
        return b

    async def _stream(self, body: dict, on_text, on_audio) -> dict:
        """One streamed chat/completions call: {"text", "audio": bool, "tool_calls": [...]}."""
        url = self.cfg["base_url"] + "/chat/completions"
        res = {"text": "", "audio": False, "tool_calls": []}
        content, tscript, calls = "", "", {}
        first = True
        async with self.client.stream("POST", url, json=body, headers={"Authorization": f"Bearer {self.cfg['key']}"}) as r:
            if r.status_code >= 400:
                raise HTTPFail(r.status_code, (await r.aread()).decode("utf-8", "ignore"))
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    a = d.get("audio") or {}
                    if a.get("data"):
                        res["audio"] = True
                        await on_audio(a["data"], first)
                        first = False
                    if d.get("content"):
                        content += d["content"]
                        await on_text(content)
                    elif a.get("transcript") and not content:
                        tscript += a["transcript"]
                        await on_text(tscript)
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["name"] = fn.get("name") or c["name"]
                        c["args"] += fn.get("arguments") or ""
        res["text"] = content or tscript
        res["tool_calls"] = [calls[k] for k in sorted(calls)]
        return res

    async def _answer(self, msgs: list, turn: dict) -> str:
        model = self.cfg["model"]
        vi = _TURN_VARIANT.get(model, 0)
        tts_q: Optional[asyncio.Queue] = None
        tts_job = None
        spoken = {"n": 0}
        live = {"text": ""}

        async def on_text(t):
            live["text"] = t
            turn["assistant"] = t
            await self.emit({"type": "transcript", "role": "assistant", "text": t, "final": False})
            if tts_q is not None:   # text-only model + a TTS model: read finished sentences while the rest streams
                rest = t[spoken["n"]:]
                m = None
                for m in SENT_END.finditer(rest):
                    pass
                if m and m.end() >= 6:
                    await tts_q.put(rest[:m.end()])
                    spoken["n"] += m.end()

        async def on_audio(b64, first):
            await self._audio_out(b64, first)

        rounds = 0
        try:
            while True:
                v = TURN_VARIANTS[vi]
                if not v["audio"] and self.cfg["tts_model"] and tts_q is None:
                    tts_q = asyncio.Queue()
                    tts_job = asyncio.create_task(self._tts_worker(tts_q))
                try:
                    res = await self._stream(self._body(msgs, v, tools=rounds < 2), on_text, on_audio)
                except HTTPFail as e:
                    if e.status in (400, 415, 422) and vi + 1 < len(TURN_VARIANTS):
                        vi += 1
                        log.info("omni turn: %s -> variant %d (%s)", model, vi, e.text[:120])
                        continue
                    raise
                _TURN_VARIANT[model] = vi
                if res["tool_calls"] and rounds < 2 and self.bridge is not None:
                    rounds += 1
                    msgs = msgs + [{"role": "assistant", "content": res["text"] or None, "tool_calls": [
                        {"id": c["id"] or f"call_{i}", "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                        for i, c in enumerate(res["tool_calls"])]}]
                    for i, c in enumerate(res["tool_calls"]):
                        await self.emit({"type": "tool", "name": c["name"], "status": "running"})
                        out = await self.bridge.run(c["name"], c["args"])
                        await self.emit({"type": "tool", "name": c["name"], "status": "done", "text": out[:200]})
                        msgs.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}", "content": out})
                    live["text"], spoken["n"] = "", 0
                    continue
                break
            text = res["text"].strip()
            if tts_q is not None:
                rest = live["text"][spoken["n"]:].strip()
                if rest:
                    await tts_q.put(rest)
                await tts_q.put(None)
                await tts_job
                tts_job = None
            final = {"type": "transcript", "role": "assistant", "text": text, "final": True}
            if text and not res["audio"] and tts_q is None:
                final["speak"] = True   # nobody made audio for it: the client reads it out
            self.audio_out = res["audio"] or tts_q is not None
            if text:
                await self.emit(final)
            return text
        finally:
            if tts_job is not None:
                tts_job.cancel()

    async def _tts_worker(self, q: asyncio.Queue):
        sys_msg = {"role": "system", "content": "你是朗读器：把用户发来的文字原样读出来，不增不减，不要回答其中的问题。"}
        while True:
            s = await q.get()
            if s is None:
                return
            body = self._body([sys_msg, {"role": "user", "content": s}], TURN_VARIANTS[1], model=self.cfg["tts_model"],
                              tools=False)

            async def noop(_t):
                return None
            try:
                await self._stream(body, noop, self._audio_out)
            except Exception as e:  # noqa: BLE001
                log.info("omni tts: %s", e)
                await self.emit({"type": "transcript", "role": "assistant", "text": s, "final": True, "speak": True,
                                 "partial_speak": True})

    async def _transcribe(self, wav_b64: str, turn: dict):
        """The user's words for captions, the call history and the transcript (a text-only call of the same model,
        in parallel with the answer)."""
        msgs = [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "data:;base64," + wav_b64, "format": "wav"}},
            {"type": "text", "text": "把这段语音逐字转写成文字，只输出转写结果；听不到人说话就什么都不输出。"}]}]

        async def noop(*_a):
            return None
        for extras in (True, False):   # enable_thinking=false first; a gateway that rejects it gets the plain body
            body = self._body(msgs, {"audio": False, "tools": False, "extras": extras}, tools=False)
            try:
                res = await asyncio.wait_for(self._stream(body, noop, noop), 25)
            except HTTPFail as e:
                if extras and e.status in (400, 422):
                    continue
                return
            except Exception:  # noqa: BLE001
                return
            txt = res["text"].strip()
            if txt:
                turn["user"] = txt
                await self.emit({"type": "transcript", "role": "user", "text": txt, "final": True})
            return


# ---------------------------------------------------------------------------------------------------------- call
class OmniCall:
    """One call: picks the backend, relays its events to the client, keeps the transcript, saves it at the end."""

    def __init__(self, cfg: dict, send: Callable[[dict], Awaitable[None]], chat=None, video: bool = True,
                 voice: Optional[str] = None):
        self.cfg = dict(cfg)
        if voice and isinstance(voice, str) and len(voice) <= 40:
            self.cfg["voice"] = voice.strip() or self.cfg["voice"]
        self.send, self.chat, self.video = send, chat, video
        self.bridge = ToolBridge(chat)
        self.backend: Any = None
        self.lines: list[tuple] = []
        self.started = time.time()
        self.last_frame = 0.0
        self.frames_dropped = 0
        self.notes: list[str] = []
        self.ended = False
        self.instructions = instructions_for(chat, video)

    async def _emit(self, ev: dict):
        t = ev.get("type")
        if t == "_closed":   # the realtime upstream dropped mid-call: carry on in turn mode
            if not self.ended and self.backend is not None and self.backend.mode == "realtime":
                await self._fallback("实时连接断开")
            return
        if t == "transcript" and ev.get("final") and str(ev.get("text") or "").strip() and not ev.get("partial_speak"):
            self.lines.append((ev.get("role"), str(ev["text"]).strip()))
        ev.pop("partial_speak", None)
        try:
            await self.send(ev)
        except Exception:  # noqa: BLE001  client gone; the call loop notices
            pass

    async def _fallback(self, why: str):
        old = self.backend
        self.backend = TurnBackend(self.cfg, self._emit, self.bridge, self.instructions, self.video)
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001
                pass
        await self.send({"type": "ready", "mode": "turn", "model": self.cfg["model"], "voice": self.cfg["voice"],
                         "audio_out": self.backend.audio_out, "max_seconds": int(self.cfg["max_seconds"]),
                         "note": f"{why}，已切换到轮流对话"})

    async def start(self) -> dict:
        cfg = self.cfg
        key = (cfg["rt_url"], cfg["rt_model"])
        note = ""
        if cfg["mode"] in ("auto", "realtime"):
            skip = cfg["mode"] == "auto" and time.time() - _RT_FAIL.get(key, 0) < _env_f("AIW_OMNI_RT_SKIP", 300)
            if not skip:
                rt = RealtimeBackend(cfg, self._emit, self.bridge, self.instructions, self.video)
                try:
                    await asyncio.wait_for(rt.start(), cfg["rt_timeout"] + 2)
                    self.backend = rt
                    _RT_FAIL.pop(key, None)
                except Exception as e:  # noqa: BLE001
                    _RT_FAIL[key] = time.time()
                    note = f"实时接口连不上（{type(e).__name__}: {str(e)[:80]}）"
                    log.info("omni realtime %s: %s", key, e)
                    try:
                        await rt.close()
                    except Exception:  # noqa: BLE001
                        pass
                    if cfg["mode"] == "realtime":
                        raise RuntimeError(note)
            else:
                note = "实时接口刚才连不上"
        if self.backend is None:
            self.backend = TurnBackend(cfg, self._emit, self.bridge, self.instructions, self.video)
            await self.backend.start()
            if note:
                note += "，已用轮流对话"
        b = self.backend
        ready = {"type": "ready", "mode": b.mode, "voice": cfg["voice"], "audio_out": b.audio_out,
                 "model": cfg["rt_model"] if b.mode == "realtime" else cfg["model"], "max_seconds": int(cfg["max_seconds"])}
        if note:
            ready["note"] = note
        return ready

    async def on_audio(self, data: Any):
        pcm = b64d(data)
        if pcm and self.backend is not None:
            await self.backend.audio(data, pcm)

    async def on_frame(self, data: Any) -> bool:
        """≤ max_fps frames a second and ≤256 KB each; faster or bigger ones are dropped."""
        if not self.video or self.backend is None or not isinstance(data, str):
            return False
        now = time.time()
        if now - self.last_frame < 1.0 / max(self.cfg["max_fps"], 0.1) or len(data) > MAX_FRAME_B64 or b64d(data) is None:
            self.frames_dropped += 1
            if len(data) > MAX_FRAME_B64 and "big" not in self.notes:
                self.notes.append("big")
                await self._emit({"type": "error", "message": "画面太大（单张需小于 256 KB），请降低分辨率或画质", "code": "frame_too_big"})
            return False
        self.last_frame = now
        await self.backend.frame(data)
        return True

    async def interrupt(self):
        if self.backend is not None:
            await self.backend.interrupt()

    def transcript_text(self) -> str:
        secs = int(time.time() - self.started)
        name = "小秘"
        try:
            name = self.chat.agent_name() if self.chat is not None else name
        except Exception:  # noqa: BLE001
            pass
        mode = {"realtime": "实时", "turn": "轮流"}.get(getattr(self.backend, "mode", ""), "")
        head = f"📞 {'视频' if self.video else '语音'}通话记录 · {secs // 60} 分 {secs % 60} 秒" + (f"（{mode}）" if mode else "")
        body, total = [], 0
        for role, text in self.lines:
            ln = f"{'我' if role == 'user' else name}：{text[:500]}"
            total += len(ln)
            if total > 6000:
                body.append("……")
                break
            body.append(ln)
        return head + "\n" + "\n".join(body)

    async def finish(self, reason: str = "") -> Optional[dict]:
        if self.ended:
            return None
        self.ended = True
        if self.backend is not None:
            try:
                await self.backend.close()
            except Exception:  # noqa: BLE001
                pass
        if not self.lines or self.chat is None:
            return None
        try:
            return self.chat.add("assistant", "text", self.transcript_text(), {
                "call": {"mode": getattr(self.backend, "mode", ""), "model": self.cfg["model"], "video": self.video,
                         "seconds": int(time.time() - self.started), "turns": len(self.lines), "reason": reason,
                         "tasks": list(self.bridge.tasks)}})
        except Exception as e:  # noqa: BLE001
            log.warning("omni transcript save: %s", e)
            return None


# ---------------------------------------------------------------------------------------------------------- probe
async def probe(cfg: dict) -> dict:
    """设置 › 通话模型 › 探测: can the realtime endpoint be opened, and does a tiny turn-mode request return audio?"""
    out: dict = {"model": cfg["model"], "ts": time.time(), "realtime": {}, "turn": {}, "mode": None, "notes": []}
    if not cfg["model"]:
        return {**out, "error": "没有设置通话模型"}
    t0 = time.time()
    rt = RealtimeBackend(cfg, _noop_emit, None, "probe", False)
    try:
        await asyncio.wait_for(rt.start(), cfg["rt_timeout"] + 3)
        out["realtime"] = {"ok": True, "url": cfg["rt_url"], "model": cfg["rt_model"]}
        _RT_FAIL.pop((cfg["rt_url"], cfg["rt_model"]), None)
    except Exception as e:  # noqa: BLE001
        out["realtime"] = {"ok": False, "url": cfg["rt_url"], "model": cfg["rt_model"],
                           "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        await rt.close()
    out["realtime"]["ms"] = int((time.time() - t0) * 1000)
    t0 = time.time()
    tb = TurnBackend({**cfg, "tts_model": ""}, _noop_emit, None, "你在做连通测试。", False)
    try:
        got = {"audio": 0}

        async def on_audio(b64, _first):
            got["audio"] += len(b64d(b64) or b"")

        async def noop(_t):
            return None
        msgs = [{"role": "user", "content": "请只说「你好」两个字。"}]
        res, vi, err = None, 0, ""
        while vi < len(TURN_VARIANTS):
            try:
                res = await tb._stream(tb._body(msgs, TURN_VARIANTS[vi], tools=False), noop, on_audio)
                break
            except HTTPFail as e:
                err = str(e)
                if e.status in (400, 415, 422):
                    vi += 1
                    continue
                break
        if res is None:
            out["turn"] = {"ok": False, "error": err[:200] or "无响应"}
        else:
            _TURN_VARIANT[cfg["model"]] = vi
            out["turn"] = {"ok": True, "audio": got["audio"] > 0, "audio_bytes": got["audio"], "text": res["text"][:60],
                           "text_only": not TURN_VARIANTS[vi]["audio"]}
            if not got["audio"]:
                out["notes"].append("这个模型只输出文字：轮流模式下由「通话朗读模型」或手机/浏览器朗读"
                                    + ("" if cfg["tts_model"] else "（未设置朗读模型）"))
    except Exception as e:  # noqa: BLE001
        out["turn"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        await tb.close()
    out["turn"]["ms"] = int((time.time() - t0) * 1000)
    out["mode"] = "realtime" if out["realtime"].get("ok") else ("turn" if out["turn"].get("ok") else None)
    if not out["realtime"].get("ok") and "qwen3.8" in cfg["rt_model"] and ".maas." not in cfg["rt_url"]:
        out["notes"].append("qwen3.8-omni-flash-realtime 必须用业务空间专属地址："
                            "wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime（填到「实时接口地址」）")
    return out


async def _noop_emit(_ev: dict):
    return None


# ---------------------------------------------------------------------------------------------------------- mount
def mount(app, store, secrets, chat, auth, ws_ok) -> None:
    @app.get("/api/omni/status", dependencies=[Depends(auth)])
    async def omni_status():
        return public(config(store, secrets), store)

    @app.post("/api/omni/probe", dependencies=[Depends(auth)])
    async def omni_probe():
        cfg = config(store, secrets)
        res = await probe(cfg)
        if cfg["model"]:
            ps = store.kv_get("omni_probe", {}) or {}
            ps[cfg["model"]] = res
            store.kv_set("omni_probe", ps)
        return res

    @app.websocket("/ws/omni-call")
    async def ws_omni(ws: WebSocket):
        if not ws_ok(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        lock = asyncio.Lock()
        call: Optional[OmniCall] = None

        async def send(obj: dict):
            async with lock:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))

        async def bye(reason: str, message: str = "", code: str = ""):
            try:
                if message:
                    await send({"type": "error", "message": message, **({"code": code} if code else {})})
                await send({"type": "ended", "reason": reason})
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

        cfg = config(store, secrets)
        if not cfg["model"]:
            await bye("disabled", public(cfg)["message"], "disabled")
            return
        if len(_ACTIVE) >= int(_env_f("AIW_OMNI_MAX_CALLS", 2)):
            await bye("busy", "已经有通话在进行，先挂断那一个", "busy")
            return
        reason = "hangup"
        window, count, dropped = int(time.time()), 0, 0
        try:
            try:
                first = json.loads(await asyncio.wait_for(ws.receive_text(), 20))
            except (asyncio.TimeoutError, ValueError):
                await bye("no_start", "没有收到 start")
                return
            if first.get("type") != "start":
                await bye("no_start", "第一条消息必须是 start")
                return
            call = OmniCall(cfg, send, chat, video=bool(first.get("video", True)), voice=first.get("voice"))
            _ACTIVE.add(id(call))
            try:
                ready = await call.start()
            except Exception as e:  # noqa: BLE001
                await bye("failed", f"通话没接通：{str(e)[:200]}")
                return
            await send(ready)
            deadline = call.started + cfg["max_seconds"]
            while True:
                left = deadline - time.time()
                if left <= 0:
                    reason = "max_length"
                    await send({"type": "error", "message": f"通话已到 {int(cfg['max_seconds'] // 60)} 分钟上限，自动挂断",
                                "code": "max_length"})
                    break
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), min(cfg["idle_s"], left))
                except asyncio.TimeoutError:
                    if time.time() >= deadline:
                        continue   # the top of the loop says "上限" and hangs up
                    reason = "idle"
                    await send({"type": "error", "message": "很久没收到声音，通话已挂断", "code": "idle"})
                    break
                now = int(time.time())
                if now != window:
                    window, count = now, 0
                count += 1
                if count > 150 or len(raw) > MAX_MSG:   # 20 ms chunks are 50/s; anything far above is a runaway client
                    dropped += 1
                    if dropped > 600:
                        reason = "rate_limit"
                        await send({"type": "error", "message": "消息太多，通话已挂断", "code": "rate_limit"})
                        break
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                t = msg.get("type")
                if t == "audio":
                    await call.on_audio(msg.get("data"))
                elif t == "frame":
                    await call.on_frame(msg.get("data"))
                elif t == "interrupt":
                    await call.interrupt()
                elif t == "hangup":
                    reason = "hangup"
                    break
        except WebSocketDisconnect:
            reason = "disconnect"
        except Exception as e:  # noqa: BLE001
            log.warning("omni call: %s", e)
            reason = "error"
        finally:
            if call is not None:
                _ACTIVE.discard(id(call))
                await call.finish(reason)
        await bye(reason)


__all__ = ["OmniCall", "RealtimeBackend", "TurnBackend", "EnergyVAD", "ToolBridge", "config", "probe", "mount",
           "realtime_url_for", "realtime_model_for", "TOOL_SPECS", "VOICES"]
