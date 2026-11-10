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
v3.9.34 routing of the call slot (omni_model + omni_model_provider):
  * Gemini Live route: the slot's provider is a Gemini provider (base_url host generativelanguage.googleapis.com, id or
    name "gemini", or the same host as omni_gemini_url) or omni_model looks like a Live model (gemini-…live…, …native-
    audio…). Then gemini_model = omni_model, the Gemini key = omni_gemini_key, else the slot provider's key (Gemini
    provider only), else the omni_gemini provider's, else env; the proxy = omni_proxy, else the slot provider's. auto
    mode = Gemini only (no realtime, no turn: Live models are not chat-completion models; a dropped Live call ends
    instead of falling back). Explicit turn mode still uses turn on the slot provider.
  * realtime (DashScope protocol) is only tried when omni_realtime_url is set, or the slot provider is DashScope /
    Bailian (dashscope*.aliyuncs.com, *.maas.aliyuncs.com), or the slot is the default gateway (as before). Otherwise
    auto = [Gemini if its key is set] + turn, and a third-party provider's key never goes to dashscope.aliyuncs.com.
Tools from a call are deliberately few: create_aiw_task (a normal aiw task via Chat.start_task, whose own confirm cards
still apply) and task_status. The call's transcript lands in the 小秘 chat as one message at hang-up.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import itertools
import json
import logging
import os
import re
import time
import wave
from array import array
from collections import deque
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


# ---------------------------------------------------------------------------------------------------------- diagnostics
def setup_logging() -> None:
    """v3.9.35: aiw.omni lines reach `docker logs` (there is no logging config, so before this only WARNING and up came
    out through logging.lastResort and none of the call's lifecycle). Only this logger gets a handler (INFO, stderr,
    no propagation), so nothing else gets louder. AIW_OMNI_LOG=WARNING / DEBUG / off changes it."""
    lg = logging.getLogger("aiw.omni")
    if getattr(lg, "_aiw_handler", False):
        return
    lvl = os.environ.get("AIW_OMNI_LOG", "INFO").strip().upper()
    if lvl in ("0", "OFF", "NONE", "FALSE"):
        return
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s aiw.omni %(message)s"))
    lg.addHandler(h)
    lg.setLevel(getattr(logging, lvl, logging.INFO))
    lg.propagate = False
    lg._aiw_handler = True   # type: ignore[attr-defined]


CALLS: deque = deque(maxlen=20)   # the last 20 calls' timelines (GET /api/omni/calls)
_CALL_SEQ = itertools.count(1)
COUNTERS = ("audio_in", "frames_in", "frames_dropped", "audio_up", "frames_up", "audio_pending", "pending_dropped",
            "up_msgs", "audio_out", "send_errors", "reconnects", "upstream_closes", "resumption_updates", "tool_calls",
            "watchdog")


class CallDiag:
    """One call's timeline and counters: every event is also a log line `omni[<id>] …` (WARNING for trouble)."""
    MAX_EVENTS = 400

    def __init__(self, peer: str = "", keep: bool = True):
        self.id = f"c{next(_CALL_SEQ)}"
        self.t0 = time.time()
        self.peer = peer
        self.backend = self.model = ""
        self.video: Optional[bool] = None
        self.ended_at: Optional[float] = None
        self.reason = ""
        self.last_error = ""
        self.events: list = []
        self.n: dict = {k: 0 for k in COUNTERS}
        self.last_user_tr = self.last_bot_tr = self.last_audio_out = 0.0
        if keep:
            CALLS.append(self)

    def bump(self, k: str, by: int = 1):
        self.n[k] = self.n.get(k, 0) + by

    def ev(self, text: str, warn: bool = False):
        if len(self.events) >= self.MAX_EVENTS:
            del self.events[40]   # keep how it started and the most recent
        self.events.append([round(time.time() - self.t0, 1), text, "warn" if warn else "info"])
        (log.warning if warn else log.info)("omni[%s] %s", self.id, text)

    def _ago(self, t: float) -> str:
        return f"{time.time() - t:.0f}s ago" if t else "never"

    def stats_line(self, ws_state: str = "") -> str:
        n = self.n
        return (f"stats t={time.time() - self.t0:.0f}s in={n['audio_in']} frames={n['frames_in']}"
                f"(-{n['frames_dropped']}) up={n['audio_up']} pending={n['audio_pending']}(-{n['pending_dropped']})"
                f" upstream_msgs={n['up_msgs']} out={n['audio_out']} reconnects={n['reconnects']}"
                f" send_err={n['send_errors']} user_tr={self._ago(self.last_user_tr)}"
                f" bot_tr={self._ago(self.last_bot_tr)}" + (f" ws={ws_state}" if ws_state else ""))

    def public(self) -> dict:
        end = self.ended_at or time.time()
        return {"id": self.id, "started": self.t0, "peer": self.peer, "backend": self.backend, "model": self.model,
                "video": self.video, "seconds": round(end - self.t0, 1), "active": self.ended_at is None,
                "reason": self.reason, "last_error": self.last_error, "counters": dict(self.n),
                "last_user_transcript_s": round(self.last_user_tr - self.t0, 1) if self.last_user_tr else None,
                "last_bot_transcript_s": round(self.last_bot_tr - self.t0, 1) if self.last_bot_tr else None,
                "events": [list(e) for e in self.events]}


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
RT_OFF_NOTE = ("实时接口只用于百炼 / DashScope：通话模型的供应商不是百炼，也没有填「实时接口地址」，"
               "不会把这个供应商的 Key 发给 DashScope")


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
    if cfg.get("mode") == "gemini" or (cfg.get("gemini_route") and cfg.get("mode") == "auto"):
        return bool(cfg.get("gemini_key"))
    return bool(cfg.get("model")) or gemini_on(cfg)


def disabled_message(cfg: dict) -> str:
    if cfg.get("mode") == "gemini":
        return "选了 Gemini Live 模式但还没有 Gemini API Key：设置 › 模型 › 视频通话 › Gemini Key"
    if cfg.get("gemini_route") and cfg.get("mode") == "auto":
        return ("通话模型是 Gemini Live 模型，但没有 Gemini API Key：给它的供应商填 Key，"
                "或填 设置 › 模型 › 视频通话 › Gemini Key")
    return "还没有设置通话模型：设置 › 模型 › 通话模型（全模态），如 qwen3.8-omni-flash；或填 Gemini Live 的 Key"


GEMINI_DEFAULTS = {"gemini_model": DEFAULT_GEMINI_MODEL, "gemini_voice": DEFAULT_GEMINI_VOICE, "gemini_url": "",
                   "gemini_key": "", "proxy": "", "gemini_timeout": 8.0,
                   "gemini_vad_start": "", "gemini_vad_end": "", "gemini_silence_ms": ""}


def with_defaults(cfg: dict) -> dict:
    """A config dict built before v3.9.32 (or by hand) gets the Gemini keys with their defaults."""
    c = dict(cfg)
    for k, v in GEMINI_DEFAULTS.items():
        c.setdefault(k, v)
    if not c["gemini_url"]:
        c["gemini_url"] = gemini_url_for("")
    return c


def gemini_live_name(model: str) -> bool:
    """v3.9.34: a model name that is a Gemini Live (BidiGenerateContent) model: gemini-…live… or …native-audio…."""
    n = (model or "").strip().lower()
    if n.startswith("models/"):
        n = n[7:]
    return (n.startswith("gemini-") and "live" in n) or "native-audio" in n


def qwen_omni_name(model: str) -> bool:
    n = (model or "").strip().lower()
    return n.startswith("qwen") and "omni" in n


HINT_GEMINI = "Gemini Live：国内需代理（填「出站代理」或在供应商里填代理）；免费层数据会用于改进产品"
HINT_QWEN = ("Qwen-Omni：qwen3.8-omni-flash 只输出文字（轮流模式配「朗读模型」）；"
             "qwen3.8-omni-flash-realtime 要填业务空间实时接口地址")


def family(cfg: dict) -> str:
    """"gemini" when the call slot is routed to Gemini Live, "qwen" for a qwen*-omni model, else ""."""
    if cfg.get("gemini_route"):
        return "gemini"
    return "qwen" if qwen_omni_name(cfg.get("model") or "") else ""


def hint_for(cfg: dict) -> str:
    return {"gemini": HINT_GEMINI, "qwen": HINT_QWEN}.get(family(cfg), "")


def config(store, secrets) -> dict:
    m = store.kv_get("models", {}) or {}
    base = (m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    key = (secrets.get("openai_api_key") if secrets else None) or os.environ.get("OPENAI_API_KEY") or ""
    tts_base, tts_key, gem_pkey, gem_pproxy = base, key, "", ""
    from . import providers as _pv
    # the call slot's provider (the implicit default when omni_model_provider is empty / unknown / disabled)
    slot = _pv.slot_conn(store, secrets, "omni_model", m) if m.get("omni_model_provider") else \
        {"id": _pv.DEFAULT_ID, "name": _pv.DEFAULT_NAME, "base_url": base, "key": key, "proxy": "", "default": True}
    if any(m.get(f) for f in ("omni_model_provider", "omni_tts_model_provider", "omni_gemini_provider")):
        # v3.9.33: providers for the omni model (turn mode, and the realtime key / URL when they are empty), the TTS
        # model, and Gemini Live (its key when omni_gemini_key is empty, its proxy when omni_proxy is empty)
        if m.get("omni_model_provider"):
            base, key = slot["base_url"], slot["key"]
        tts_base, tts_key = base, key
        if m.get("omni_tts_model_provider"):
            c = _pv.slot_conn(store, secrets, "omni_tts_model", m)
            tts_base, tts_key = c["base_url"], c["key"]
        if m.get("omni_gemini_provider"):
            c = _pv.slot_conn(store, secrets, "omni_gemini", m)
            if not c["default"]:
                gem_pkey, gem_pproxy = c["key"], c.get("proxy") or ""
    model = str(m.get("omni_model") or "").strip()
    rt_url_set = str(m.get("omni_realtime_url") or "").strip()
    # v3.9.34: a Gemini provider or a Gemini Live model name in the call slot = Gemini Live with that model, the
    # provider's key and proxy (omni_gemini_key / omni_proxy still win); the DashScope realtime endpoint never sees it
    slot_gem_provider = _pv.is_gemini(slot, str(m.get("omni_gemini_url") or ""))
    gemini_route = bool(model) and (slot_gem_provider or gemini_live_name(model))
    if gemini_route and slot_gem_provider and slot.get("key"):
        gem_pkey = slot["key"]
    if gemini_route and slot_gem_provider and slot.get("proxy"):
        gem_pproxy = slot["proxy"]
    # realtime (DashScope protocol) only with an explicit omni_realtime_url, a DashScope / Bailian provider, or the
    # default gateway (as before v3.9.34) - never a third-party provider's key to dashscope.aliyuncs.com
    rt_on = not gemini_route and (bool(rt_url_set) or slot.get("default", False) or _pv.is_dashscope(slot))
    rt_url = rt_url_set or realtime_url_for(base)
    if rt_url.startswith("http"):
        rt_url = "ws" + rt_url[4:]
    mode = str(m.get("omni_mode") or "auto").strip().lower()
    gkey = ((secrets.get("omni_gemini_key") if secrets else None) or gem_pkey or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY") or "")
    try:
        minutes = float(m.get("omni_max_minutes") or 30)
    except (TypeError, ValueError):
        minutes = 30.0
    gmodel = (model[7:] if model.startswith("models/") else model) if gemini_route else \
        (str(m.get("omni_gemini_model") or "").strip() or DEFAULT_GEMINI_MODEL)
    return {"model": model, "voice": str(m.get("omni_voice") or "").strip() or DEFAULT_VOICE,
            "base_url": base, "key": key, "tts_base_url": tts_base, "tts_key": tts_key,
            "rt_url": rt_url, "rt_model": str(m.get("omni_realtime_model") or "").strip() or realtime_model_for(model),
            "rt_key": (secrets.get("omni_realtime_key") if secrets else None) or key,
            "rt_on": rt_on, "gemini_route": gemini_route,
            "tts_model": str(m.get("omni_tts_model") or "").strip(),
            "mode": mode if mode in MODES else "auto",
            "gemini_model": gmodel,
            "gemini_voice": str(m.get("omni_gemini_voice") or "").strip() or DEFAULT_GEMINI_VOICE,
            "gemini_url": gemini_url_for(str(m.get("omni_gemini_url") or "")), "gemini_key": gkey,
            "proxy": str(m.get("omni_proxy") or "").strip() or gem_pproxy,
            "gemini_timeout": _env_f("AIW_OMNI_GEMINI_TIMEOUT", 8.0),
            # v3.9.35 Gemini automatic activity detection: start low|high|api ("" = low), end low|high ("" = API
            # default), silence ms ("" = API default)
            "gemini_vad_start": str(m.get("omni_gemini_vad_start") or "").strip().lower(),
            "gemini_vad_end": str(m.get("omni_gemini_vad_end") or "").strip().lower(),
            "gemini_silence_ms": str(m.get("omni_gemini_silence_ms") or "").strip(),
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
                      "proxy": bool(cfg["proxy"]), "route": bool(cfg.get("gemini_route")),
                      "vad": vad_config(with_defaults(cfg))},
           "realtime_on": bool(cfg.get("rt_on", True)), "family": family(cfg), "hint": hint_for(cfg)}
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


# v3.9.35: a close whose code or reason says retrying cannot help (bad argument, policy / quota / key / model) ends the
# call with that reason instead of resuming in a loop; anything else (1000 after goAway, 1001, 1006, a bare 1011) resumes
FATAL_CODES = (1002, 1003, 1007, 1008, 1009, 1010)
FATAL_RE = re.compile(r"quota|exceed|resource[_ ]?exhausted|billing|api[ _]?key|permission|denied|unauthori[sz]|"
                      r"not found|not_found|not supported|not implemented|unsupported|invalid|limit", re.I)


def _close_info(ws=None, e: Optional[BaseException] = None) -> tuple:
    """(code, reason) of a closed upstream connection: the close frame Gemini sent, else the one we sent, else the
    connection's own close_code (1006 = dropped without a close frame)."""
    for attr in ("rcvd", "sent"):
        fr = getattr(e, attr, None) if e is not None else None
        if fr is not None:
            return getattr(fr, "code", None), str(getattr(fr, "reason", "") or "")
    code = getattr(ws, "close_code", None) if ws is not None else None
    reason = getattr(ws, "close_reason", "") if ws is not None else ""
    if code is None and e is not None:
        return None, f"{type(e).__name__}: {str(e)[:160]}"
    return code, str(reason or "")


def fatal_close(code, reason: str) -> bool:
    return code in FATAL_CODES or bool(reason and FATAL_RE.search(reason))


def vad_config(cfg: dict) -> dict:
    """v3.9.35 realtimeInputConfig.automaticActivityDetection from omni_gemini_vad_start / _end / _silence_ms.
    start: "" = low (aiw default: a phone on speaker hears its own echo and room noise, and START_SENSITIVITY_HIGH, the
    API default, takes that for speech), "api" = leave it to the API; end: "" = API default (END_SENSITIVITY_HIGH);
    silence: "" = API default (~800 ms) unless AIW_OMNI_GEMINI_SILENCE_MS is set."""
    aad: dict = {"disabled": False}
    st = str(cfg.get("gemini_vad_start") or "low").strip().lower()
    if st in ("low", "high"):
        aad["startOfSpeechSensitivity"] = f"START_SENSITIVITY_{st.upper()}"
    en = str(cfg.get("gemini_vad_end") or "").strip().lower()
    if en in ("low", "high"):
        aad["endOfSpeechSensitivity"] = f"END_SENSITIVITY_{en.upper()}"
    ms = str(cfg.get("gemini_silence_ms") or "").strip() or os.environ.get("AIW_OMNI_GEMINI_SILENCE_MS", "")
    try:
        if ms and 0 < int(float(ms)) <= 10000:
            aad["silenceDurationMs"] = int(float(ms))
    except ValueError:
        pass
    return aad


class _NoDiag:
    """Stand-in when a backend runs outside a call (the probe, unit tests)."""
    def __init__(self):
        self.n: dict = {}
        self.events: list = []

    def ev(self, text: str, warn: bool = False):
        self.events.append(text)
        (log.warning if warn else log.debug)("omni %s", text)

    def bump(self, k: str, by: int = 1):
        self.n[k] = self.n.get(k, 0) + by


class GeminiBackend:
    """Gemini Live API (BidiGenerateContent) behind the same client protocol. One logical session may span several
    WebSocket connections: a goAway or a dropped connection reconnects with the latest resumption handle.
    v3.9.35: every close is logged with its code and reason; a fatal one (quota, bad key / model / argument) ends the
    call with that reason; at most one upstream connection is open at a time (the old one is closed before the next is
    opened); a connection that dies while the reconnect is still flushing audio is reconnected again (it used to leave
    the call 'connected' to a closed socket); connections that keep dying at once end the call instead of a resume
    storm; a send that stalls aborts the connection; a watchdog reconnects when the user talks and nothing comes back."""
    mode = "gemini"
    MAX_RECONNECTS = 30
    PENDING_MAX = 100          # audio chunks kept while reconnecting (~2-4 s), oldest dropped first
    QUICK_S = 20.0             # a connection that ends this soon without any serverContent is a "quick drop"
    MAX_QUICK_DROPS = 3        # this many in a row: the call ends with the last close reason

    def __init__(self, cfg: dict, emit: Emit, bridge: Optional[ToolBridge], instructions: str, video: bool, diag=None):
        self.cfg, self.emit, self.bridge = with_defaults(cfg), emit, bridge
        self.instructions, self.video = instructions, video
        self.diag = diag if diag is not None else _NoDiag()
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
        self.again = False         # the new connection died while a reconnect was still running: go round again
        self.reconnects = 0
        self.quick_drops = 0
        self.pending: list[str] = []
        self.flushing = False
        self.opened: set = set()   # every upstream connection not yet closed by us (closed at hang-up)
        self.log: list[str] = []   # what happened to the connection (tests / debugging)
        self.last_close = ""
        self.conn_t0 = 0.0
        self.conn_content = False  # this connection has sent serverContent / toolCall
        now = time.time()
        self.last_up = now         # last serverContent / toolCall from upstream (the watchdog's clock)
        self.voiced_s = 0.0        # seconds of loud client audio since then
        self.wd_fires = 0
        self.last_audio_in = 0.0
        self.stream_open = False   # audio sent since the last audioStreamEnd
        self.drop_audio_until = 0.0
        self.send_timeout = _env_f("AIW_OMNI_SEND_TIMEOUT", 5.0)
        self.wd_s = _env_f("AIW_OMNI_WATCHDOG_S", 20.0)
        self.wd_voiced = _env_f("AIW_OMNI_WATCHDOG_VOICED_S", 3.0)
        self.wd_rms = _env_f("AIW_OMNI_WATCHDOG_RMS", 800.0)
        self.wd_max = int(_env_f("AIW_OMNI_WATCHDOG_MAX", 2))
        self.tool_timeout = _env_f("AIW_OMNI_TOOL_TIMEOUT", 30.0)

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
            "realtimeInputConfig": {"automaticActivityDetection": vad_config(self.cfg)},
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
        self.opened.add(ws)
        try:
            await ws.send(json.dumps(self.setup(handle), ensure_ascii=False))
            self.diag.ev(f"setup sent (model {self.cfg['gemini_model']}, {'resume' if handle else 'new session'})")
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
                if not isinstance(ev, dict):
                    continue
                if "setupComplete" in ev:
                    self.diag.ev("setupComplete")
                    return ws
                if "error" in ev:
                    raise RuntimeError(str(ev["error"])[:200])
                await self._on(ev)   # e.g. a sessionResumptionUpdate before setupComplete
        except BaseException:
            await self._close_ws(ws)
            raise

    def _installed(self, ws):
        self.ws = ws
        self.conn_t0 = time.time()
        self.conn_content = False
        self.last_up = time.time()
        self.voiced_s = 0.0
        self.reader = asyncio.create_task(self._read(ws))

    async def start(self):
        self._installed(await self._open())
        self.log.append("open")

    async def _read(self, ws):
        err: Optional[BaseException] = None
        try:
            async for raw in ws:
                self.diag.bump("up_msgs")
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                try:
                    await self._on(ev)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001  one bad message never takes the connection down
                    self.diag.ev(f"handler error {type(e).__name__}: {str(e)[:160]}", warn=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            err = e
        code, reason = _close_info(ws, err)
        self.opened.discard(ws)
        if ws is not self.ws or self.closed:   # a connection we replaced or closed ourselves
            self.diag.ev(f"upstream closed {code} {reason[:120]} (replaced connection)".rstrip())
            return
        age = time.time() - self.conn_t0
        self.diag.bump("upstream_closes")
        self.last_close = f"{code} {reason}".strip()
        self.diag.ev(f"upstream closed code={code} reason={reason[:200]!r} after {age:.1f}s"
                     f"{'' if self.conn_content else ' (nothing received on it)'}", warn=True)
        if fatal_close(code, reason):
            await self._fail(f"Gemini 断开了连接：{self.last_close[:200]}")
            return
        self.quick_drops = self.quick_drops + 1 if (age < self.QUICK_S and not self.conn_content) else 0
        self._reconnect("closed")

    def _reconnect(self, why: str, fresh: bool = False):
        if self.closed:
            return
        if self.reconnecting is not None and not self.reconnecting.done():
            self.again = True   # the reconnect in flight checks this before it returns
            return
        self.again = False
        self.reconnecting = asyncio.create_task(self._do_reconnect(why, fresh))
        self.reconnecting.add_done_callback(self._reconnect_done)

    def _reconnect_done(self, t: asyncio.Task):
        if t.cancelled():
            return
        e = t.exception()
        if e is not None:   # never a call with ws None forever: _do_reconnect fails the call itself; this is the net
            self.diag.ev(f"reconnect task crashed: {type(e).__name__}: {e}", warn=True)

    async def _do_reconnect(self, why: str, fresh: bool = False):
        try:
            await self._reconnect_loop(why, fresh)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.diag.ev(f"reconnect error {type(e).__name__}: {e}", warn=True)
            await self._fail(f"Gemini 重连出错：{type(e).__name__}: {str(e)[:120]}")

    async def _reconnect_loop(self, why: str, fresh: bool):
        self.log.append("reconnect:" + why)
        while not self.closed:
            old, self.ws = self.ws, None   # audio queues in self.pending meanwhile
            if old is not None:
                await self._close_ws(old)   # one live upstream session per call, never two
            if fresh:
                self.handle = ""
            if self.quick_drops >= self.MAX_QUICK_DROPS:
                await self._fail(f"Gemini 连接一接通就断（连续 {self.quick_drops} 次，最后一次：{self.last_close[:160] or '无原因'}）")
                return
            if self.reconnects >= self.MAX_RECONNECTS:
                await self._fail(f"Gemini 重连次数过多（{self.reconnects} 次），最后一次断开：{self.last_close[:160]}")
                return
            self.reconnects += 1
            self.diag.bump("reconnects")
            self.diag.ev(f"reconnect #{self.reconnects} ({why}) {'resume with handle' if self.handle else 'fresh session'}")
            ws = None
            if self.handle:
                try:
                    ws = await self._open(self.handle)
                    self.diag.ev("resumed")
                except Exception as e:  # noqa: BLE001
                    self.diag.ev(f"resume failed: {str(e)[:200]}", warn=True)
                    self.handle = ""     # a stale handle will not work a second time; a fresh session may
            if ws is None:
                try:
                    ws = await self._open()   # a fresh session (the context restarts) beats a dead call
                    self.diag.ev("fresh session opened (context restarted)", warn=True)
                    await self.emit({"type": "error", "message": "Gemini 会话已重新开始（上下文未能接续）", "code": "resumed_fresh"})
                except Exception as e2:  # noqa: BLE001
                    self.diag.ev(f"reopen failed: {str(e2)[:200]}", warn=True)
                    await self._fail(f"Gemini 连不上了：{str(e2)[:200]}")
                    return
            if self.closed:
                await self._close_ws(ws)
                return
            self._installed(ws)
            self.log.append("resumed")
            await self._flush()
            if self.again or (self.reader is not None and self.reader.done()) or self.ws is not ws:
                # the new connection died while we were flushing; its reader's reconnect request landed here
                self.again = False
                self.diag.ev("new connection died during the switch; reconnecting again", warn=True)
                why = "closed"
                fresh = False
                continue
            return

    async def _flush(self):
        """Send the audio queued while there was no connection, in order (new chunks queue behind it meanwhile)."""
        self.flushing = True
        try:
            while self.pending and self.ws is not None and not self.closed:
                a = self.pending.pop(0)
                if await self._send({"realtimeInput": {"audio": {"data": a, "mimeType": f"audio/pcm;rate={IN_RATE}"}}}):
                    self.diag.bump("audio_up")
                    self.stream_open = True
        finally:
            self.flushing = False

    async def _fail(self, message: str, code: str = "upstream_closed"):
        """The upstream is gone for good: say why (client + log) and let the call end."""
        if self.closed:
            return
        self.closed = True
        self.diag.ev("call failed: " + message, warn=True)
        for ws in list(self.opened):
            await self._close_ws(ws)
        await self.emit({"type": "_closed", "message": message, "code": code})

    async def _close_ws(self, ws):
        self.opened.discard(ws)
        try:
            await asyncio.wait_for(ws.close(), 2)
        except asyncio.CancelledError:   # the call's task is being cancelled: still never leave the session open
            self._abort(ws)
            raise
        except Exception:  # noqa: BLE001  a slow proxy: drop the TCP connection so the session cannot linger
            self._abort(ws)

    def _abort(self, ws):
        tr = getattr(ws, "transport", None)
        try:
            if tr is not None:
                tr.abort()   # the reader sees 1006 and reconnects
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, msg: dict) -> bool:
        ws = self.ws
        if ws is None or self.closed:
            return False
        try:
            await asyncio.wait_for(ws.send(json.dumps(msg, ensure_ascii=False)), self.send_timeout)
            return True
        except asyncio.TimeoutError:
            self.diag.ev(f"send stalled > {self.send_timeout:.0f}s: dropping the upstream connection", warn=True)
            self._abort(ws)
            return False
        except Exception as e:  # noqa: BLE001  the reader notices the close and reconnects
            self.diag.bump("send_errors")
            if self.diag.n.get("send_errors", 0) in (1, 10, 100, 1000):
                self.diag.ev(f"send failed ({self.diag.n.get('send_errors')}x): {type(e).__name__}: {str(e)[:120]}", warn=True)
            return False

    # -- server messages
    def _content(self):
        self.last_up = time.time()
        self.voiced_s = 0.0
        self.wd_fires = 0
        self.quick_drops = 0
        self.conn_content = True

    async def _on(self, ev: dict):
        if "sessionResumptionUpdate" in ev:
            u = ev["sessionResumptionUpdate"] or {}
            if u.get("resumable") and u.get("newHandle"):
                self.handle = str(u["newHandle"])
                self.diag.bump("resumption_updates")
        if "goAway" in ev:   # the server ends this connection soon: switch now, while the handle is fresh
            self.log.append("goAway")
            self.diag.ev(f"goAway (timeLeft {(ev.get('goAway') or {}).get('timeLeft')})")
            if self.ws is not None:
                self._reconnect("goAway")
        if "toolCall" in ev:
            self._content()
            for fc in (ev["toolCall"] or {}).get("functionCalls") or []:
                fid = str(fc.get("id") or "")
                name = str(fc.get("name") or "")
                self.diag.bump("tool_calls")
                self.diag.ev(f"tool call {name} id={fid or '-'}")
                j = asyncio.create_task(self._tool(name, fc.get("args") or {}, fid))
                self.jobs[fid or id(j)] = j
                j.add_done_callback(lambda _j, k=fid or id(j): self.jobs.pop(k, None))
        if "toolCallCancellation" in ev:
            self._content()
            for fid in (ev["toolCallCancellation"] or {}).get("ids") or []:
                self.diag.ev(f"tool call cancelled id={fid}")
                j = self.jobs.pop(str(fid), None)
                if j is not None and not j.done():
                    j.cancel()
        sc = ev.get("serverContent")
        if not isinstance(sc, dict):
            return
        self._content()
        it = sc.get("inputTranscription") or sc.get("interimInputTranscription")
        if isinstance(it, dict) and it.get("text"):
            self.heard += str(it["text"])
            await self.emit({"type": "transcript", "role": "user", "text": self.heard.strip(), "final": False})
        if sc.get("interrupted"):
            self.responding = False
            self.drop_audio_until = 0.0
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
                    if time.time() < self.drop_audio_until:
                        continue   # the user tapped 打断: the rest of this answer is not played
                    await self.emit({"type": "audio", "data": d["data"]})
        ot = sc.get("outputTranscription")
        if isinstance(ot, dict) and ot.get("text"):
            await self._end_heard()
            self.said += str(ot["text"])
            await self.emit({"type": "transcript", "role": "assistant", "text": self.said.strip(), "final": False})
        if sc.get("turnComplete"):
            self.responding = False
            self.drop_audio_until = 0.0
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
            out = await asyncio.wait_for(self.bridge.run(name, args), self.tool_timeout) if self.bridge else "工具不可用"
        except asyncio.TimeoutError:   # a blocking call must always get an answer, or the model waits forever
            out = "超时了，没办成"
            self.diag.ev(f"tool {name} timed out after {self.tool_timeout:.0f}s", warn=True)
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
        ok = await self._send({"toolResponse": {"functionResponses": [fr]}})
        self.diag.ev(f"tool {name} done, response {'sent' if ok else 'NOT sent (no connection)'}")

    # -- client input
    async def audio(self, pcm_b64: str, pcm: bytes):
        now = time.time()
        self.last_audio_in = now
        if self.wd_s > 0 and rms(pcm) >= self.wd_rms:
            self.voiced_s += len(pcm) / (2.0 * IN_RATE)
        if self.closed:
            return
        if self.ws is None or self.flushing:
            self.pending.append(pcm_b64)
            self.diag.bump("audio_pending")
            if len(self.pending) > self.PENDING_MAX:
                del self.pending[0]
                self.diag.bump("pending_dropped")
            return
        if await self._send({"realtimeInput": {"audio": {"data": pcm_b64, "mimeType": f"audio/pcm;rate={IN_RATE}"}}}):
            self.diag.bump("audio_up")
            self.stream_open = True

    async def frame(self, jpeg_b64: str):
        if self.ws is not None and not self.flushing:   # frames during a reconnect are skipped (the next one comes in a second)
            if await self._send({"realtimeInput": {"video": {"data": jpeg_b64, "mimeType": "image/jpeg"}}}):
                self.diag.bump("frames_up")

    async def interrupt(self):
        """The user tapped 打断 (the client already stopped its playback): Gemini has no cancel message for realtime
        input, so the rest of the answer in flight is not sent to the client (until turnComplete / interrupted)."""
        if self.responding:
            self.drop_audio_until = time.time() + 30
            self.diag.ev("client interrupt: dropping the rest of this answer's audio")

    def reconnect_running(self) -> bool:
        return self.reconnecting is not None and not self.reconnecting.done()

    async def tick(self):
        """Once a second from the call: audioStreamEnd after a pause in the microphone (mute), and the watchdog."""
        if self.closed or self.ws is None or self.reconnect_running():
            return
        now = time.time()
        if self.stream_open and self.last_audio_in and now - self.last_audio_in > 1.5 \
                and os.environ.get("AIW_OMNI_GEMINI_STREAM_END", "1") != "0":
            self.stream_open = False   # docs: a paused stream (mic off) sends audioStreamEnd so cached audio is flushed
            await self._send({"realtimeInput": {"audioStreamEnd": True}})
        if self.wd_s <= 0 or self.jobs:
            return
        quiet = now - self.last_up
        if quiet < self.wd_s or self.voiced_s < self.wd_voiced:
            return
        self.wd_fires += 1
        self.diag.bump("watchdog")
        if self.wd_fires > self.wd_max:
            await self._fail(f"模型一直没有响应（已重连 {self.wd_max} 次仍没有回应），通话结束", "no_response")
            return
        self.diag.ev(f"watchdog: {self.voiced_s:.1f}s of speech and nothing from Gemini for {quiet:.0f}s; "
                     f"reconnect #{self.wd_fires} ({'fresh session' if self.wd_fires >= 2 else 'resume'})", warn=True)
        await self.emit({"type": "error", "message": "模型没有响应，正在重连…", "code": "no_response"})
        self.last_up, self.voiced_s = now, 0.0
        self._reconnect("watchdog", fresh=self.wd_fires >= 2)

    async def close(self):
        self.closed = True
        for j in list(self.jobs.values()) + list(self.side):
            j.cancel()
        if self.reconnecting is not None and self.reconnecting is not asyncio.current_task():
            self.reconnecting.cancel()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
        conns = set(self.opened)
        if self.ws is not None:
            conns.add(self.ws)
        if conns:
            self.diag.ev(f"upstream closed by us ({len(conns)} connection{'s' if len(conns) > 1 else ''})")
        try:
            await asyncio.gather(*(self._close_ws(ws) for ws in conns))
        except asyncio.CancelledError:
            for ws in conns:
                self._abort(ws)
            raise


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
                 voice: Optional[str] = None, diag: Optional[CallDiag] = None):
        self.cfg = with_defaults(cfg)
        self.diag = diag if diag is not None else CallDiag()
        self.diag.video = video
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
        self.dead_evt = asyncio.Event()
        self.ticker: Optional[asyncio.Task] = None
        self.instructions = instructions_for(chat, video)

    def _die(self, reason: str):
        self.dead = reason
        self.dead_evt.set()

    def start_ticker(self):
        """v3.9.35: once a second the backend's tick (Gemini: audioStreamEnd, watchdog); every AIW_OMNI_STATS_S (15)
        one counters line in the log / timeline."""
        if self.ticker is None:
            self.ticker = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self):
        every = max(1.0, _env_f("AIW_OMNI_STATS_S", 15.0))
        step = max(0.05, _env_f("AIW_OMNI_TICK_S", 1.0))
        last = time.time()
        while not self.ended:
            await asyncio.sleep(step)
            b = self.backend
            try:
                if b is not None and hasattr(b, "tick"):
                    await b.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.diag.ev(f"tick error {type(e).__name__}: {e}", warn=True)
            if time.time() - last >= every:
                last = time.time()
                ws = getattr(b, "ws", None)
                state = "" if getattr(b, "mode", "") not in ("gemini", "realtime") else (
                    "reconnecting" if ws is None else str(getattr(getattr(ws, "state", None), "name", "open")).lower())
                self.diag.ev(self.diag.stats_line(state))

    async def _emit(self, ev: dict):
        t = ev.get("type")
        if t == "_closed":   # the realtime / Gemini upstream dropped mid-call: carry on in turn mode when possible
            why = str(ev.get("message") or "")
            self.diag.ev("upstream gone: " + (why or "closed"), warn=True)
            if not self.ended and self.backend is not None and self.backend.mode in ("realtime", "gemini"):
                if self.cfg["model"] and self.cfg["mode"] == "auto" and not self.cfg.get("gemini_route"):
                    await self._fallback(("实时连接断开" if self.backend.mode == "realtime" else "Gemini 连接断开")
                                         + (f"（{why[:120]}）" if why else ""))
                else:
                    code = str(ev.get("code") or "upstream_closed")
                    msg = f"{why}，通话结束" if why else "模型连接断开了，通话结束"
                    self.diag.last_error = msg
                    self._die(code if code == "no_response" else "upstream_closed")
                    try:
                        await self.send({"type": "error", "message": msg, "code": "upstream_closed",
                                         **({"reason": code} if code != "upstream_closed" else {})})
                    except Exception:  # noqa: BLE001
                        pass
            return
        if t == "audio":
            self.diag.bump("audio_out")
        elif t == "transcript":
            if ev.get("role") == "user":
                self.diag.last_user_tr = time.time()
            else:
                self.diag.last_bot_tr = time.time()
            if ev.get("final") and str(ev.get("text") or "").strip():
                self.diag.ev(f"{'user' if ev.get('role') == 'user' else 'model'}: {str(ev['text']).strip()[:80]}")
        elif t == "error":
            self.diag.last_error = str(ev.get("message") or "")
            self.diag.ev(f"to client: error {ev.get('code') or ''} {ev.get('message') or ''}".strip(), warn=True)
        elif t == "tool":
            self.diag.ev(f"tool {ev.get('name')} {ev.get('status')}")
        if t == "transcript" and ev.get("final") and str(ev.get("text") or "").strip() and not ev.get("partial_speak"):
            self.lines.append((ev.get("role"), str(ev["text"]).strip()))
        ev.pop("partial_speak", None)
        try:
            await self.send(ev)
        except Exception:  # noqa: BLE001  client gone; the call loop notices
            pass

    async def _fallback(self, why: str):
        self.diag.ev(f"fallback to turn: {why}", warn=True)
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
            b: Any = GeminiBackend(cfg, self._emit, self.bridge, self.instructions, self.video, diag=self.diag)
            limit = cfg["gemini_timeout"] + 4
            self.diag.ev(f"backend gemini: model {cfg['gemini_model']} url {cfg['gemini_url'].split('?')[0]}"
                         f" proxy={'yes' if cfg.get('proxy') else ('env' if env_proxy(cfg['gemini_url']) else 'no')}"
                         f" vad={json.dumps(vad_config(cfg), ensure_ascii=False)}")
        else:
            b = RealtimeBackend(cfg, self._emit, self.bridge, self.instructions, self.video)
            limit = cfg["rt_timeout"] + 2
            self.diag.ev(f"backend realtime: model {cfg['rt_model']} url {cfg['rt_url']}")
        try:
            await asyncio.wait_for(b.start(), limit)
            self.backend = b
            _RT_FAIL.pop(key, None)
            return ""
        except Exception as e:  # noqa: BLE001
            _RT_FAIL[key] = time.time()
            self.diag.ev(f"{kind} did not open: {type(e).__name__}: {str(e)[:200]}", warn=True)
            try:
                await b.close()
            except Exception:  # noqa: BLE001
                pass
            return f"{label}连不上（{type(e).__name__}: {str(e)[:120]}）"

    def plan(self) -> list:
        """Backends to try, in order. auto: Gemini first when its key is set, then realtime (only when allowed, see
        config rt_on) and turn when omni_model is set; a call slot routed to Gemini Live (v3.9.34) = Gemini only.
        gemini / realtime / turn: only that one (realtime not at all when it is not allowed)."""
        cfg = self.cfg
        rt_on = cfg.get("rt_on", True)
        if cfg["mode"] == "gemini":
            return ["gemini"]
        if cfg["mode"] == "realtime":
            return ["realtime"] if rt_on else []
        if cfg["mode"] == "turn":
            return ["turn"]
        if cfg.get("gemini_route"):
            return ["gemini"]
        out = ["gemini"] if cfg["gemini_key"] else []
        return out + ((["realtime"] if rt_on else []) + ["turn"] if cfg["model"] else [])

    async def start(self) -> dict:
        cfg = self.cfg
        notes: list[str] = []
        for kind in self.plan():
            if kind == "turn":
                self.diag.ev(f"backend turn: model {cfg['model']} base {cfg['base_url']}")
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
            if cfg["mode"] == "realtime" and not cfg.get("rt_on", True):
                notes.append(RT_OFF_NOTE)
            raise RuntimeError("；".join(notes) or disabled_message(cfg))
        b = self.backend
        model = {"realtime": cfg["rt_model"], "gemini": cfg["gemini_model"]}.get(b.mode, cfg["model"])
        self.diag.backend, self.diag.model = b.mode, model
        self.diag.ev(f"ready: {b.mode} {model}" + (f" (note: {'；'.join(notes)[:160]})" if notes else ""))
        voice = cfg["gemini_voice"] if b.mode == "gemini" else cfg["voice"]
        ready = {"type": "ready", "mode": b.mode, "voice": voice, "audio_out": b.audio_out,
                 "model": model, "max_seconds": int(cfg["max_seconds"])}
        if notes:
            ready["note"] = "；".join(notes)
        return ready

    async def on_audio(self, data: Any):
        pcm = b64d(data)
        self.diag.bump("audio_in")
        if pcm and self.backend is not None:
            await self.backend.audio(data, pcm)

    async def on_frame(self, data: Any) -> bool:
        """≤ max_fps frames a second and ≤256 KB each; faster or bigger ones are dropped."""
        if not self.video or self.backend is None or not isinstance(data, str):
            return False
        now = time.time()
        self.diag.bump("frames_in")
        if now - self.last_frame < 1.0 / max(self.cfg["max_fps"], 0.1) or len(data) > MAX_FRAME_B64 or b64d(data) is None:
            self.frames_dropped += 1
            self.diag.bump("frames_dropped")
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
        self.diag.reason = reason
        if self.ticker is not None and self.ticker is not asyncio.current_task():
            self.ticker.cancel()
        try:
            if self.backend is not None:
                try:
                    await self.backend.close()
                except Exception as e:  # noqa: BLE001
                    self.diag.ev(f"backend close: {type(e).__name__}: {e}", warn=True)
        finally:
            self.diag.ended_at = time.time()
            self.diag.ev(f"ended ({reason or '-'}) after {self.diag.ended_at - self.started:.0f}s; " + self.diag.stats_line())
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
    out: dict = {"model": cfg["model"], "ts": time.time(), "realtime": {}, "turn": {}, "mode": None, "notes": [],
                 "family": family(cfg), "hint": hint_for(cfg), "route": "gemini" if cfg.get("gemini_route") else ""}
    if cfg.get("gemini_route") and cfg["mode"] in ("auto", "gemini"):
        # v3.9.34: the call slot holds a Gemini Live model / Gemini provider: only Gemini Live is probed (no realtime,
        # no turn); a Live session always answers with audio (responseModalities AUDIO), so it "speaks" when it opens
        out["gemini"] = await probe_gemini(cfg)
        g = out["gemini"]
        g["audio"] = bool(g.get("ok"))
        out["realtime"] = {"skipped": True}
        out["turn"] = {"skipped": True}
        out["mode"] = "gemini" if g.get("ok") else None
        if not g.get("ok"):
            err = g.get("error") or ""
            if not cfg.get("proxy") and not env_proxy(cfg["gemini_url"]) and "googleapis.com" in cfg["gemini_url"]:
                out["notes"].append("国内直连 Google 通常不通：在供应商或「出站代理」填 http://… 或 socks5://… 代理，或填中转地址")
            if "API key" in err or "PERMISSION" in err.upper() or "1008" in err:
                out["notes"].append(f"Gemini 拒绝了 Key 或模型名：检查供应商的 Key 和模型名（{cfg['gemini_model']}）")
        return out
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
    if not cfg.get("rt_on", True):   # v3.9.34: never a non-DashScope provider's key to the DashScope endpoint
        out["realtime"] = {"skipped": True, "reason": RT_OFF_NOTE}
    else:
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
    if out["realtime"].get("ok") is False and "qwen3.8" in cfg["rt_model"] and ".maas." not in cfg["rt_url"]:
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

    @app.get("/api/omni/calls", dependencies=[Depends(auth)])
    async def omni_calls():
        """v3.9.35 通话诊断: the last 20 calls (newest first) with their event timelines and counters."""
        return {"calls": [c.public() for c in reversed(CALLS)], "active": len(_ACTIVE),
                "log_hint": "docker logs aiw-api 2>&1 | grep 'aiw.omni'"}

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
        peer = f"{ws.client.host}" if getattr(ws, "client", None) else ""
        if not enabled(cfg):
            log.warning("omni call from %s refused: %s", peer, disabled_message(cfg))
            await bye("disabled", disabled_message(cfg), "disabled")
            return
        if len(_ACTIVE) >= int(_env_f("AIW_OMNI_MAX_CALLS", 2)):
            log.warning("omni call from %s refused: busy (%d active)", peer, len(_ACTIVE))
            await bye("busy", "已经有通话在进行，先挂断那一个", "busy")
            return
        reason = "hangup"
        window, count, dropped = int(time.time()), 0, 0
        recv: Optional[asyncio.Task] = None
        dead_wait: Optional[asyncio.Task] = None
        diag = CallDiag(peer)
        try:
            try:
                first = json.loads(await asyncio.wait_for(ws.receive_text(), 20))
            except (asyncio.TimeoutError, ValueError):
                await bye("no_start", "没有收到 start")
                return
            if first.get("type") != "start":
                await bye("no_start", "第一条消息必须是 start")
                return
            diag.ev(f"call from {peer or '?'}: video={bool(first.get('video', True))} mode={cfg['mode']}"
                    f" slot={cfg['model'] or '-'} gemini_route={bool(cfg.get('gemini_route'))}")
            call = OmniCall(cfg, send, chat, video=bool(first.get("video", True)), voice=first.get("voice"), diag=diag)
            _ACTIVE.add(id(call))
            try:
                ready = await call.start()
            except Exception as e:  # noqa: BLE001
                diag.ev(f"not connected: {str(e)[:300]}", warn=True)
                diag.last_error = f"通话没接通：{str(e)[:200]}"
                await bye("failed", diag.last_error)
                return
            await send(ready)
            call.start_ticker()
            deadline = call.started + cfg["max_seconds"]
            dead_wait = asyncio.ensure_future(call.dead_evt.wait())
            last_msg = time.time()
            while True:
                left = deadline - time.time()
                if left <= 0:
                    reason = "max_length"
                    await send({"type": "error", "message": f"通话已到 {int(cfg['max_seconds'] // 60)} 分钟上限，自动挂断",
                                "code": "max_length"})
                    break
                if call.dead:   # v3.9.35: the upstream is gone for good - hang up now, not at the next client message
                    reason = call.dead
                    break
                if recv is None:
                    recv = asyncio.ensure_future(ws.receive_text())
                wait = min(cfg["idle_s"] - (time.time() - last_msg), left)
                done, _ = await asyncio.wait({recv, dead_wait}, timeout=max(0.0, wait),
                                             return_when=asyncio.FIRST_COMPLETED)
                if recv not in done:
                    if call.dead:
                        reason = call.dead
                        break
                    if time.time() >= deadline:
                        continue   # the top of the loop says "上限" and hangs up
                    if time.time() - last_msg < cfg["idle_s"]:
                        continue
                    reason = "idle"
                    await send({"type": "error", "message": "很久没收到声音，通话已挂断", "code": "idle"})
                    break
                t_recv, recv = recv, None
                raw = t_recv.result()   # WebSocketDisconnect / a closed socket raises here
                last_msg = time.time()
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
            diag.ev(f"call loop error {type(e).__name__}: {e}", warn=True)
            reason = "error"
        finally:
            for t in (recv, dead_wait):
                if t is not None and not t.done():
                    t.cancel()
            if call is not None:
                _ACTIVE.discard(id(call))
                await call.finish(reason)
            else:
                diag.reason, diag.ended_at = reason, time.time()
        await bye(reason)


__all__ = ["CallDiag", "CALLS", "setup_logging", "vad_config", "OmniCall", "RealtimeBackend", "TurnBackend", "GeminiBackend", "probe_gemini", "gemini_url_for", "ws_open", "EnergyVAD", "ToolBridge", "config", "probe", "mount",
           "realtime_url_for", "realtime_model_for", "TOOL_SPECS", "VOICES"]
