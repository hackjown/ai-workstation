"""v3.9.31: omni realtime voice / video call (视频通话, like 豆包): the phone or browser streams microphone audio and a
camera frame a second, the full-modality model (Qwen-Omni) answers with speech, and the call can hand work to aiw.

  WS /ws/omni-call?token=<API token>   (same token as /ws/events, /ws/device and every API call)
    app -> {"type":"start","video":true,"voice":"Tina"?}
    app -> {"type":"audio","data":"<base64 pcm16 16 kHz mono>"}   20-100 ms a chunk
    app -> {"type":"frame","data":"<base64 jpeg>"}                ≤1 fps, ≤720p (the server drops faster frames)
    app -> {"type":"interrupt"} | {"type":"hangup"} | {"type":"ping"}
    srv -> {"type":"ready","mode":"realtime"|"turn"|"gemini","model","voice","audio_out":bool,"max_seconds","note"?}
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
  gemini    v3.9.32: Gemini Live API (BidiGenerateContent) as a server-side WebSocket client: setup (models/<name>,
            responseModalities AUDIO, prebuilt voice, systemInstruction, functionDeclarations, input/output audio
            transcription, automatic activity detection, contextWindowCompression slidingWindow so audio+video sessions
            are not cut at 2 minutes, sessionResumption) -> setupComplete; realtimeInput audio (audio/pcm;rate=16000) and
            video (image/jpeg); serverContent modelTurn inlineData (pcm16 24 kHz), input/outputTranscription,
            interrupted (-> speech_started), turnComplete; toolCall -> toolResponse; goAway / a dropped connection ->
            a new connection resumed with the last sessionResumptionUpdate handle, invisible to the client.
            Barge-in is the server's VAD. From China it needs models.omni_proxy (http/https/socks) or a relay URL.
"auto" (models.omni_mode, default) tries Gemini first when omni_gemini_key is set, then realtime (when omni_model is
set) with a short timeout, then turn, telling the client. "gemini" uses only Gemini. A backend that failed is skipped
for AIW_OMNI_RT_SKIP seconds in auto mode.

Settings (kv "models"): omni_model (empty = no realtime/turn), omni_voice (default Tina), omni_realtime_url,
omni_realtime_model, omni_tts_model, omni_mode (auto | realtime | turn | gemini), omni_max_minutes (30),
omni_gemini_model (gemini-3.8-live), omni_gemini_voice (Puck), omni_gemini_url (relay / reverse proxy base, optional),
omni_proxy (http://, https://, socks5:// for the Gemini connection; else HTTPS_PROXY / ALL_PROXY from the environment).
Secrets omni_realtime_key (else the gateway key), omni_gemini_key (else env GEMINI_API_KEY / GOOGLE_API_KEY).
Calls are on when omni_model is set, or when a Gemini key is set and the mode is auto or gemini.
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
# v3.9.32 Gemini Live (ai.google.dev/api/live, aistudio.google.com/docs/live-api)
GEMINI_HOST = "wss://generativelanguage.googleapis.com"
GEMINI_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
DEFAULT_GEMINI_MODEL = "gemini-3.8-live"
DEFAULT_GEMINI_VOICE = "Puck"
GEMINI_VOICES = ["Puck", "Kore", "Zephyr", "Charon", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
                 "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
                 "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia",
                 "Sadaltager", "Sulafat"]
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


MODES = ("auto", "realtime", "turn", "gemini")


def gemini_url_for(base: str) -> str:
    """The BidiGenerateContent endpoint. omni_gemini_url may be a bare host (a relay / reverse proxy in front of
    generativelanguage.googleapis.com), which gets the standard path, or a full URL, which is used as given."""
    b = (base or "").strip() or GEMINI_HOST
    if b.startswith("http"):
        b = "ws" + b[4:]
    if "BidiGenerateContent" in b:
        return b
    return b.rstrip("/") + GEMINI_PATH


def with_key(url: str, key: str) -> str:
    if not key or re.search(r"[?&]key=", url):
        return url
    return url + ("&" if "?" in url else "?") + "key=" + key


def gemini_on(cfg: dict) -> bool:
    return bool(cfg.get("gemini_key")) and cfg.get("mode") in ("auto", "gemini")


def enabled(cfg: dict) -> bool:
    """Calls are possible: an omni model (realtime / turn), or a Gemini key in auto / gemini mode."""
    if cfg.get("mode") == "gemini":
        return bool(cfg.get("gemini_key"))
    return bool(cfg.get("model")) or gemini_on(cfg)


def disabled_message(cfg: dict) -> str:
    if cfg.get("mode") == "gemini":
        return "选了 Gemini Live 模式但还没有 Gemini API Key：设置 › 模型 › 视频通话 › Gemini Key"
    return "还没有设置通话模型：设置 › 模型 › 通话模型（全模态），如 qwen3.8-omni-flash；或填 Gemini Live 的 Key"


GEMINI_DEFAULTS = {"gemini_model": DEFAULT_GEMINI_MODEL, "gemini_voice": DEFAULT_GEMINI_VOICE, "gemini_url": "",
                   "gemini_key": "", "proxy": "", "gemini_timeout": 8.0}


def with_defaults(cfg: dict) -> dict:
    """A config dict built before v3.9.32 (or by hand) gets the Gemini keys with their defaults."""
    c = dict(cfg)
    for k, v in GEMINI_DEFAULTS.items():
        c.setdefault(k, v)
    if not c["gemini_url"]:
        c["gemini_url"] = gemini_url_for("")
    return c


def config(store, secrets) -> dict:
    m = store.kv_get("models", {}) or {}
    base = (m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    key = (secrets.get("openai_api_key") if secrets else None) or os.environ.get("OPENAI_API_KEY") or ""
    tts_base, tts_key, gem_pkey, gem_pproxy = base, key, "", ""
    if any(m.get(f) for f in ("omni_model_provider", "omni_tts_model_provider", "omni_gemini_provider")):
        # v3.9.33: providers for the omni model (turn mode, and the realtime key / URL when they are empty), the TTS
        # model, and Gemini Live (its key when omni_gemini_key is empty, its proxy when omni_proxy is empty)
        from . import providers as _pv
        if m.get("omni_model_provider"):
            c = _pv.slot_conn(store, secrets, "omni_model", m)
            base, key = c["base_url"], c["key"]
        tts_base, tts_key = base, key
        if m.get("omni_tts_model_provider"):
            c = _pv.slot_conn(store, secrets, "omni_tts_model", m)
            tts_base, tts_key = c["base_url"], c["key"]
        if m.get("omni_gemini_provider"):
            c = _pv.slot_conn(store, secrets, "omni_gemini", m)
            if not c["default"]:
                gem_pkey, gem_pproxy = c["key"], c.get("proxy") or ""
    model = str(m.get("omni_model") or "").strip()
    rt_url = str(m.get("omni_realtime_url") or "").strip() or realtime_url_for(base)
    if rt_url.startswith("http"):
        rt_url = "ws" + rt_url[4:]
    mode = str(m.get("omni_mode") or "auto").strip().lower()
    gkey = ((secrets.get("omni_gemini_key") if secrets else None) or gem_pkey or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY") or "")
    try:
        minutes = float(m.get("omni_max_minutes") or 30)
    except (TypeError, ValueError):
        minutes = 30.0
    return {"model": model, "voice": str(m.get("omni_voice") or "").strip() or DEFAULT_VOICE,
            "base_url": base, "key": key, "tts_base_url": tts_base, "tts_key": tts_key,
            "rt_url": rt_url, "rt_model": str(m.get("omni_realtime_model") or "").strip() or realtime_model_for(model),
            "rt_key": (secrets.get("omni_realtime_key") if secrets else None) or key,
            "tts_model": str(m.get("omni_tts_model") or "").strip(),
            "mode": mode if mode in MODES else "auto",
            "gemini_model": str(m.get("omni_gemini_model") or "").strip() or DEFAULT_GEMINI_MODEL,
            "gemini_voice": str(m.get("omni_gemini_voice") or "").strip() or DEFAULT_GEMINI_VOICE,
            "gemini_url": gemini_url_for(str(m.get("omni_gemini_url") or "")), "gemini_key": gkey,
            "proxy": str(m.get("omni_proxy") or "").strip() or gem_pproxy,
            "gemini_timeout": _env_f("AIW_OMNI_GEMINI_TIMEOUT", 8.0),
            "max_seconds": max(60.0, min(minutes, 240.0) * 60),
            "rt_timeout": _env_f("AIW_OMNI_RT_TIMEOUT", 5.0), "max_fps": _env_f("AIW_OMNI_MAX_FPS", 1.0),
            "idle_s": _env_f("AIW_OMNI_IDLE", 30.0), "audio_format": os.environ.get("AIW_OMNI_AUDIO_FORMAT", "wav")}


def public(cfg: dict, store=None) -> dict:
    """What GET /api/omni/status shows (no keys)."""
    out = {"enabled": enabled(cfg), "model": cfg["model"], "voice": cfg["voice"], "voices": VOICES,
           "realtime_url": cfg["rt_url"], "realtime_model": cfg["rt_model"], "tts_model": cfg["tts_model"],
           "mode": cfg["mode"], "max_minutes": int(cfg["max_seconds"] // 60), "has_realtime_key": bool(cfg["rt_key"]),
           "gemini": {"on": gemini_on(cfg), "model": cfg["gemini_model"], "voice": cfg["gemini_voice"],
                      "voices": GEMINI_VOICES, "url": cfg["gemini_url"], "has_key": bool(cfg["gemini_key"]),
                      "proxy": bool(cfg["proxy"])}}
    if not out["enabled"]:
        out["message"] = disabled_message(cfg)
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


# ---------------------------------------------------------------------------------------------------------- gemini
def _connect_params() -> set:
    try:
        import inspect
        from websockets.asyncio.client import connect
        return set(inspect.signature(connect.__init__).parameters)
    except Exception:  # noqa: BLE001  pragma: no cover
        return set()


WS_PROXY_ARG = "proxy" in _connect_params()   # websockets >= 15: proxy= (http, https, socks via python-socks)


def env_proxy(url: str) -> str:
    """HTTPS_PROXY / ALL_PROXY (or their lowercase forms) for a wss:// URL, honouring NO_PROXY; '' when none."""
    import urllib.request
    p = urlsplit(url)
    host = p.hostname or ""
    if urllib.request.proxy_bypass(f"{host}:{p.port or (443 if p.scheme == 'wss' else 80)}"):
        return ""
    px = urllib.request.getproxies()
    for k in (("wss", "https", "socks", "all") if p.scheme == "wss" else ("ws", "http", "https", "socks", "all")):
        if px.get(k):
            return px[k]
    return ""


async def _http_tunnel(proxy: str, host: str, port: int, timeout: float):
    """websockets < 15 has no proxy support: open a CONNECT tunnel through an http:// proxy ourselves and hand the
    socket to connect(sock=...). https:// and socks proxies need websockets >= 15."""
    import socket
    p = urlsplit(proxy)
    if p.scheme != "http":
        raise RuntimeError(f"{p.scheme}:// 代理需要 websockets>=15（pip install -U 'websockets>=15'），或改用 http:// 代理")
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (p.hostname, p.port or 80)), timeout)
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if p.username:
            from urllib.parse import unquote
            cred = b64e(f"{unquote(p.username)}:{unquote(p.password or '')}".encode())
            req += f"Proxy-Authorization: Basic {cred}\r\n"
        await loop.sock_sendall(sock, (req + "\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout)
            if not chunk:
                raise RuntimeError("代理断开了连接")
            buf += chunk
            if len(buf) > 16384:
                raise RuntimeError("代理响应过长")
        status = buf.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        if " 200" not in status:
            raise RuntimeError(f"代理拒绝 CONNECT：{status[:80]}")
        return sock
    except BaseException:
        sock.close()
        raise


async def ws_open(url: str, headers: dict, timeout: float, proxy: str = ""):
    """A WebSocket for the Gemini connection, through omni_proxy (or the environment's proxy) when there is one."""
    from websockets.asyncio.client import connect
    kw: dict = {"additional_headers": headers, "open_timeout": timeout, "max_size": 16 * 1024 * 1024, "ping_interval": 20}
    if WS_PROXY_ARG:
        kw["proxy"] = proxy or True   # True: websockets reads HTTPS_PROXY / ALL_PROXY / NO_PROXY itself
        try:
            return await asyncio.wait_for(connect(url, **kw), timeout + 2)
        except ImportError as e:   # socks:// without python-socks
            raise RuntimeError("SOCKS 代理需要安装 python-socks：pip install 'python-socks[asyncio]'；或改用 http:// 代理") from e
    px = proxy or env_proxy(url)
    if px:
        u = urlsplit(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = await _http_tunnel(px, u.hostname or "", port, timeout)
        kw["sock"] = sock
        if u.scheme == "wss":
            kw["server_hostname"] = u.hostname
    return await asyncio.wait_for(connect(url, **kw), timeout + 2)


def gemini_tools() -> list:
    """TOOL_SPECS as Gemini functionDeclarations (OpenAPI schema with upper-case types)."""
    def conv(sch):
        if not isinstance(sch, dict):
            return sch
        out = {}
        for k, v in sch.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.upper()
            elif k == "properties" and isinstance(v, dict):
                out[k] = {n: conv(x) for n, x in v.items()}
            elif k == "required" and not v:
                continue
            else:
                out[k] = conv(v) if isinstance(v, dict) else v
        return out
    return [{"functionDeclarations": [{"name": t["function"]["name"], "description": t["function"]["description"],
                                       "parameters": conv(t["function"]["parameters"])} for t in TOOL_SPECS]}]


def _close_reason(e: BaseException) -> str:
    """The reason Gemini gives in its close frame (a bad model, key or setup field), else the exception text."""
    for attr in ("rcvd", "sent"):
        fr = getattr(e, attr, None)
        if fr is not None and getattr(fr, "reason", ""):
            return f"{getattr(fr, 'code', '')} {fr.reason}".strip()
    return f"{type(e).__name__}: {str(e)[:160]}"


class GeminiBackend:
    """Gemini Live API (BidiGenerateContent) behind the same client protocol. One logical session may span several
    WebSocket connections: a goAway or a dropped connection reconnects with the latest resumption handle."""
    mode = "gemini"
    MAX_RECONNECTS = 30
    PENDING_MAX = 100          # audio chunks kept while reconnecting (~2-4 s), oldest dropped first

    def __init__(self, cfg: dict, emit: Emit, bridge: Optional[ToolBridge], instructions: str, video: bool):
        self.cfg, self.emit, self.bridge = with_defaults(cfg), emit, bridge
        self.instructions, self.video = instructions, video
        self.ws = None
        self.reader: Optional[asyncio.Task] = None
        self.handle = ""           # latest resumable sessionResumptionUpdate.newHandle
        self.closed = False
        self.audio_out = True
        self.said = ""
        self.heard = ""
        self.responding = False
        self.jobs: dict = {}       # function call id -> task
        self.side: set = set()
        self.reconnecting: Optional[asyncio.Task] = None
        self.reconnects = 0
        self.pending: list[str] = []
        self.log: list[str] = []   # what happened to the connection (tests / debugging)

    # -- connection
    def url(self) -> str:
        return with_key(gemini_url_for(self.cfg["gemini_url"]), self.cfg["gemini_key"])

    def setup(self, handle: str = "") -> dict:
        model = self.cfg["gemini_model"]
        s: dict = {
            "model": model if model.startswith("models/") else "models/" + model,
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.cfg["gemini_voice"]}}}},
            "systemInstruction": {"parts": [{"text": self.instructions}]},
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
            "realtimeInputConfig": {"automaticActivityDetection": {
                "disabled": False, "silenceDurationMs": int(_env_f("AIW_OMNI_GEMINI_SILENCE_MS", 800))}},
            # without compression an audio+video session ends after ~2 minutes (audio only ~15)
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {"handle": handle} if handle else {},
        }
        if self.bridge is not None:
            s["tools"] = gemini_tools()
        return {"setup": s}

    async def _open(self, handle: str = ""):
        t = self.cfg["gemini_timeout"]
        hdr = {"x-goog-api-key": self.cfg["gemini_key"]} if self.cfg["gemini_key"] else {}
        ws = await ws_open(self.url(), hdr, t, self.cfg.get("proxy") or "")
        try:
            await ws.send(json.dumps(self.setup(handle), ensure_ascii=False))
            end = time.time() + t
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    raise RuntimeError("等 setupComplete 超时")
                except Exception as e:  # noqa: BLE001  closed during setup: Gemini says why in the close frame
                    raise RuntimeError("Gemini 拒绝了连接：" + _close_reason(e)) from e
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if "setupComplete" in ev:
                    return ws
                if "error" in ev:
                    raise RuntimeError(str(ev["error"])[:200])
                await self._on(ev)   # e.g. a sessionResumptionUpdate before setupComplete
        except BaseException:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    async def start(self):
        self.ws = await self._open()
        self.log.append("open")
        self.reader = asyncio.create_task(self._read(self.ws))

    async def _read(self, ws):
        try:
            async for raw in ws:
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                await self._on(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("omni gemini read: %s", _close_reason(e))
        if ws is self.ws and not self.closed:   # the connection dropped under us (not a planned switch)
            self._reconnect("closed")

    def _reconnect(self, why: str):
        if self.reconnecting is None or self.reconnecting.done():
            self.reconnecting = asyncio.create_task(self._do_reconnect(why))

    async def _do_reconnect(self, why: str):
        old = self.ws
        self.ws = None   # audio queues in self.pending meanwhile
        self.log.append("reconnect:" + why)
        while not self.closed:
            if not self.handle or self.reconnects >= self.MAX_RECONNECTS:
                break
            self.reconnects += 1
            try:
                ws = await self._open(self.handle)
            except Exception as e:  # noqa: BLE001
                log.info("omni gemini resume: %s", e)
                self.handle = ""     # a stale handle will not work a second time
                try:
                    ws = await self._open()   # a fresh session (the context restarts) beats a dead call
                    await self.emit({"type": "error", "message": "Gemini 会话已重新开始（上下文未能接续）", "code": "resumed_fresh"})
                except Exception as e2:  # noqa: BLE001
                    log.info("omni gemini reopen: %s", e2)
                    break
            self.ws = ws
            self.log.append("resumed")
            self.reader = asyncio.create_task(self._read(ws))
            if old is not None:
                j = asyncio.create_task(self._close_ws(old))
                self.side.add(j)
                j.add_done_callback(self.side.discard)
            pend, self.pending = self.pending, []
            for a in pend:
                await self._send({"realtimeInput": {"audio": {"data": a, "mimeType": f"audio/pcm;rate={IN_RATE}"}}})
            return
        if not self.closed:
            self.closed = True
            await self.emit({"type": "_closed"})

    @staticmethod
    async def _close_ws(ws):
        try:
            await asyncio.wait_for(ws.close(), 2)
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, msg: dict) -> bool:
        ws = self.ws
        if ws is None or self.closed:
            return False
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception as e:  # noqa: BLE001  the reader notices the close and reconnects
            log.info("omni gemini send: %s", e)
            return False

    # -- server messages
    async def _on(self, ev: dict):
        if "sessionResumptionUpdate" in ev:
            u = ev["sessionResumptionUpdate"] or {}
            if u.get("resumable") and u.get("newHandle"):
                self.handle = str(u["newHandle"])
        if "goAway" in ev:   # the server ends this connection soon: switch now, while the handle is fresh
            self.log.append("goAway")
            if self.ws is not None:
                self._reconnect("goAway")
        if "toolCall" in ev:
            for fc in (ev["toolCall"] or {}).get("functionCalls") or []:
                fid = str(fc.get("id") or "")
                j = asyncio.create_task(self._tool(str(fc.get("name") or ""), fc.get("args") or {}, fid))
                self.jobs[fid or id(j)] = j
                j.add_done_callback(lambda _j, k=fid or id(j): self.jobs.pop(k, None))
        if "toolCallCancellation" in ev:
            for fid in (ev["toolCallCancellation"] or {}).get("ids") or []:
                j = self.jobs.pop(str(fid), None)
                if j is not None and not j.done():
                    j.cancel()
        sc = ev.get("serverContent")
        if not isinstance(sc, dict):
            return
        it = sc.get("inputTranscription") or sc.get("interimInputTranscription")
        if isinstance(it, dict) and it.get("text"):
            self.heard += str(it["text"])
            await self.emit({"type": "transcript", "role": "user", "text": self.heard.strip(), "final": False})
        if sc.get("interrupted"):
            self.responding = False
            await self.emit({"type": "speech_started"})   # the user talks over the answer: stop playback now
            await self._end_said(cut=True)
        mt = sc.get("modelTurn")
        if isinstance(mt, dict):
            for part in mt.get("parts") or []:
                d = part.get("inlineData") if isinstance(part, dict) else None
                if isinstance(d, dict) and d.get("data") and str(d.get("mimeType") or "audio/pcm").startswith("audio"):
                    if not self.responding:
                        self.responding = True
                        await self._end_heard()
                    await self.emit({"type": "audio", "data": d["data"]})
        ot = sc.get("outputTranscription")
        if isinstance(ot, dict) and ot.get("text"):
            await self._end_heard()
            self.said += str(ot["text"])
            await self.emit({"type": "transcript", "role": "assistant", "text": self.said.strip(), "final": False})
        if sc.get("turnComplete"):
            self.responding = False
            await self._end_heard()
            await self._end_said()

    async def _end_heard(self):
        txt, self.heard = self.heard.strip(), ""
        if txt:
            await self.emit({"type": "transcript", "role": "user", "text": txt, "final": True})

    async def _end_said(self, cut: bool = False):
        txt, self.said = self.said.strip(), ""
        if txt:
            await self.emit({"type": "transcript", "role": "assistant", "text": txt + ("…" if cut else ""), "final": True})

    async def _tool(self, name: str, args: Any, fid: str):
        await self.emit({"type": "tool", "name": name, "status": "running"})
        try:
            out = await self.bridge.run(name, args) if self.bridge else "工具不可用"
        except asyncio.CancelledError:
            await self.emit({"type": "tool", "name": name, "status": "error", "text": "已取消"})
            raise
        await self.emit({"type": "tool", "name": name, "status": "done", "text": out[:200]})
        resp: dict = {"result": out}
        if "3.1-flash-live" not in self.cfg["gemini_model"]:
            resp["scheduling"] = "WHEN_IDLE"   # non-blocking calls (gemini-3.8-live default): speak it when free
        fr = {"name": name, "response": resp}
        if fid:
            fr["id"] = fid
        await self._send({"toolResponse": {"functionResponses": [fr]}})

    # -- client input
    async def audio(self, pcm_b64: str, pcm: bytes):
        msg = {"realtimeInput": {"audio": {"data": pcm_b64, "mimeType": f"audio/pcm;rate={IN_RATE}"}}}
        if self.ws is None and not self.closed:
            self.pending = (self.pending + [pcm_b64])[-self.PENDING_MAX:]
            return
        await self._send(msg)

    async def frame(self, jpeg_b64: str):
        if self.ws is not None:   # frames during a reconnect are simply skipped (the next one comes in a second)
            await self._send({"realtimeInput": {"video": {"data": jpeg_b64, "mimeType": "image/jpeg"}}})

    async def interrupt(self):
        return None   # Gemini's own VAD handles barge-in; the client already stopped its playback

    async def close(self):
        self.closed = True
        for j in list(self.jobs.values()) + list(self.side):
            j.cancel()
        if self.reconnecting is not None and self.reconnecting is not asyncio.current_task():
            self.reconnecting.cancel()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
        if self.ws is not None:
            await self._close_ws(self.ws)


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

    async def _stream(self, body: dict, on_text, on_audio, tts: bool = False) -> dict:
        """One streamed chat/completions call: {"text", "audio": bool, "tool_calls": [...]}. tts: the 朗读模型's
        own provider (v3.9.33 tts_base_url / tts_key, the omni provider when not set)."""
        base = (self.cfg.get("tts_base_url") or self.cfg["base_url"]) if tts else self.cfg["base_url"]
        key = (self.cfg.get("tts_key") or self.cfg["key"]) if tts else self.cfg["key"]
        url = base + "/chat/completions"
        res = {"text": "", "audio": False, "tool_calls": []}
        content, tscript, calls = "", "", {}
        first = True
        async with self.client.stream("POST", url, json=body, headers={"Authorization": f"Bearer {key}"}) as r:
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
                await self._stream(body, noop, self._audio_out, tts=True)
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
        self.cfg = with_defaults(cfg)
        if voice and isinstance(voice, str) and len(voice) <= 40:
            if voice.strip() in GEMINI_VOICES:
                self.cfg["gemini_voice"] = voice.strip()
            else:
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
        self.dead = ""            # set when the upstream is gone for good: the call loop hangs up
        self.instructions = instructions_for(chat, video)

    async def _emit(self, ev: dict):
        t = ev.get("type")
        if t == "_closed":   # the realtime / Gemini upstream dropped mid-call: carry on in turn mode when possible
            if not self.ended and self.backend is not None and self.backend.mode in ("realtime", "gemini"):
                if self.cfg["model"] and self.cfg["mode"] == "auto":
                    await self._fallback("实时连接断开" if self.backend.mode == "realtime" else "Gemini 连接断开")
                else:
                    self.dead = "upstream_closed"
                    try:
                        await self.send({"type": "error", "message": "模型连接断开了，通话结束", "code": "upstream_closed"})
                    except Exception:  # noqa: BLE001
                        pass
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

    async def _try(self, kind: str) -> str:
        """Open the realtime or Gemini backend; '' on success, else a note for the client."""
        cfg = self.cfg
        key = ("gemini", cfg["gemini_url"], cfg["gemini_model"]) if kind == "gemini" else (cfg["rt_url"], cfg["rt_model"])
        label = "Gemini Live" if kind == "gemini" else "实时接口"
        if cfg["mode"] == "auto" and time.time() - _RT_FAIL.get(key, 0) < _env_f("AIW_OMNI_RT_SKIP", 300):
            return f"{label}刚才连不上"
        if kind == "gemini":
            b: Any = GeminiBackend(cfg, self._emit, self.bridge, self.instructions, self.video)
            limit = cfg["gemini_timeout"] + 4
        else:
            b = RealtimeBackend(cfg, self._emit, self.bridge, self.instructions, self.video)
            limit = cfg["rt_timeout"] + 2
        try:
            await asyncio.wait_for(b.start(), limit)
            self.backend = b
            _RT_FAIL.pop(key, None)
            return ""
        except Exception as e:  # noqa: BLE001
            _RT_FAIL[key] = time.time()
            log.info("omni %s %s: %s", kind, key, e)
            try:
                await b.close()
            except Exception:  # noqa: BLE001
                pass
            return f"{label}连不上（{type(e).__name__}: {str(e)[:120]}）"

    def plan(self) -> list:
        """Backends to try, in order. auto: Gemini first when its key is set, then realtime and turn when omni_model
        is set. gemini / realtime / turn: only that one."""
        cfg = self.cfg
        if cfg["mode"] == "gemini":
            return ["gemini"]
        if cfg["mode"] in ("realtime", "turn"):
            return [cfg["mode"]]
        out = ["gemini"] if cfg["gemini_key"] else []
        return out + (["realtime", "turn"] if cfg["model"] else [])

    async def start(self) -> dict:
        cfg = self.cfg
        notes: list[str] = []
        for kind in self.plan():
            if kind == "turn":
                self.backend = TurnBackend(cfg, self._emit, self.bridge, self.instructions, self.video)
                await self.backend.start()
                if notes:
                    notes[-1] += "，已用轮流对话"
                break
            n = await self._try(kind)
            if not n:
                break
            notes.append(n)
        if self.backend is None:
            raise RuntimeError("；".join(notes) or disabled_message(cfg))
        b = self.backend
        model = {"realtime": cfg["rt_model"], "gemini": cfg["gemini_model"]}.get(b.mode, cfg["model"])
        voice = cfg["gemini_voice"] if b.mode == "gemini" else cfg["voice"]
        ready = {"type": "ready", "mode": b.mode, "voice": voice, "audio_out": b.audio_out,
                 "model": model, "max_seconds": int(cfg["max_seconds"])}
        if notes:
            ready["note"] = "；".join(notes)
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
        mode = {"realtime": "实时", "turn": "轮流", "gemini": "Gemini"}.get(getattr(self.backend, "mode", ""), "")
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
                "call": {"mode": getattr(self.backend, "mode", ""), "video": self.video,
                         "model": self.cfg["gemini_model"] if getattr(self.backend, "mode", "") == "gemini" else self.cfg["model"],
                         "seconds": int(time.time() - self.started), "turns": len(self.lines), "reason": reason,
                         "tasks": list(self.bridge.tasks)}})
        except Exception as e:  # noqa: BLE001
            log.warning("omni transcript save: %s", e)
            return None


# ---------------------------------------------------------------------------------------------------------- probe
async def probe_gemini(cfg: dict) -> dict:
    """Open a Gemini Live connection, send setup, wait for setupComplete (short timeout), close."""
    t0 = time.time()
    cfg = with_defaults(cfg)
    out: dict = {"model": cfg["gemini_model"], "url": cfg["gemini_url"], "proxy": bool(cfg.get("proxy"))}
    if not cfg["gemini_key"]:
        return {**out, "ok": False, "error": "没有 Gemini API Key", "ms": 0}
    g = GeminiBackend(cfg, _noop_emit, None, "probe", False)
    try:
        await asyncio.wait_for(g.start(), cfg["gemini_timeout"] + 4)
        out["ok"] = True
        _RT_FAIL.pop(("gemini", cfg["gemini_url"], cfg["gemini_model"]), None)
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        await g.close()
    out["ms"] = int((time.time() - t0) * 1000)
    return out


async def probe(cfg: dict) -> dict:
    """设置 › 通话模型 › 探测: can Gemini Live / the realtime endpoint be opened, and does a tiny turn-mode request
    return audio? Only the backends that are configured are tried."""
    cfg = with_defaults(cfg)
    out: dict = {"model": cfg["model"], "ts": time.time(), "realtime": {}, "turn": {}, "mode": None, "notes": []}
    if cfg.get("gemini_key"):
        out["gemini"] = await probe_gemini(cfg)
        if not out["gemini"].get("ok"):
            err = out["gemini"].get("error") or ""
            if not cfg.get("proxy") and not env_proxy(cfg["gemini_url"]) and "googleapis.com" in cfg["gemini_url"]:
                out["notes"].append("国内直连 Google 通常不通：在「出站代理」填 http://… 或 socks5://… 代理，或填中转地址")
            if "API key" in err or "PERMISSION" in err.upper() or "1008" in err:
                out["notes"].append("Gemini 拒绝了 Key 或模型名：检查 Gemini Key 和模型（如 gemini-3.8-live）")
    if not cfg["model"]:
        if "gemini" not in out:
            return {**out, "error": "没有设置通话模型，也没有 Gemini Key"}
        out["mode"] = "gemini" if out["gemini"].get("ok") else None
        return out
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
    if (out.get("gemini") or {}).get("ok") and cfg["mode"] in ("auto", "gemini"):
        out["mode"] = "gemini"
    elif cfg["mode"] == "gemini":
        out["mode"] = None
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
        if enabled(cfg) or cfg["model"]:   # keyed by omni_model ("" for a Gemini-only setup)
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
        if not enabled(cfg):
            await bye("disabled", disabled_message(cfg), "disabled")
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
                    if call.dead:
                        reason = call.dead
                        break
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
                if call.dead:
                    reason = call.dead
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


__all__ = ["OmniCall", "RealtimeBackend", "TurnBackend", "GeminiBackend", "probe_gemini", "gemini_url_for", "ws_open", "EnergyVAD", "ToolBridge", "config", "probe", "mount",
           "realtime_url_for", "realtime_model_for", "TOOL_SPECS", "VOICES"]
