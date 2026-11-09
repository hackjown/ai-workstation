"""v3.9.31: omni realtime voice / video call (/ws/omni-call): the realtime backend against a fake DashScope-style
upstream (a real local websocket server), the turn backend against a fake streaming gateway (httpx.MockTransport),
barge-in, realtime -> turn fallback, the frame-rate cap, auth, the tool bridge, the transcript saved to the 小秘 chat
at hang-up, the energy VAD, the settings, and calls disabled while omni_model is empty."""
import asyncio
import base64
import json
import math
import struct
import threading
import time

import httpx
import pytest

from aiw_api import omni_call as oc


# ------------------------------------------------------------------------------------------------ helpers
def tone(ms: int, amp: int = 8000) -> bytes:
    n = 16 * ms
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(n))


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (16 * ms)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


JPEG = b64(b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9")


def sse(*chunks) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def cfg(**kw):
    c = {"model": "qwen3.5-omni-flash", "voice": "Tina", "base_url": "http://gw/v1", "key": "gk", "rt_url": "ws://127.0.0.1:1/x",
         "rt_model": "qwen3.5-omni-flash-realtime", "rt_key": "rk", "tts_model": "", "mode": "turn", "max_seconds": 1800,
         "rt_timeout": 1.0, "max_fps": 1.0, "idle_s": 30, "audio_format": "wav"}
    c.update(kw)
    return c


class Sink:
    def __init__(self):
        self.events = []

    async def __call__(self, ev):
        self.events.append(ev)

    def of(self, t):
        return [e for e in self.events if e.get("type") == t]


class FakeChat:
    def __init__(self):
        self.added, self.started = [], []

    def agent_name(self):
        return "小秘"

    class store:   # noqa: N801
        @staticmethod
        def kv_get(k, d=None):
            return {"name": "林"} if k == "profile" else d

    def recent_text(self, n=6):
        return ""

    def add(self, role, kind, text="", data=None, task_id=None):
        m = {"id": f"m{len(self.added)}", "role": role, "kind": kind, "text": text, "data": data or {}}
        self.added.append(m)
        return m

    async def start_task(self, prompt, title="", machine_id=None, attachments=None, origin=None, source="chat", fast=None):
        self.started.append((prompt, origin, source))
        return {"data": {"task_id": f"task-{len(self.started)}", "title": prompt[:10]}}

    async def _task_status(self, a):
        return json.dumps({"tasks": [{"id": "task-1", "title": "整理报告", "status": "进行中", "result": ""}], "routines": []})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    oc._RT_FAIL.clear()
    oc._TURN_VARIANT.clear()
    oc._ACTIVE.clear()
    monkeypatch.setenv("AIW_OMNI_TURN_ASR", "1")
    yield
    oc.TRANSPORT = None


# ------------------------------------------------------------------------------------------------ fake realtime upstream
class FakeRealtime:
    """A DashScope-Omni-Realtime-shaped server in its own thread: session.created, session.updated, then scripted
    replies once audio arrives (speech_started, user transcript, an answer with audio + transcript), and a tool call
    when the transcript asks for one."""

    def __init__(self, tool_call=False, reject_tools=False):
        self.got, self.headers, self.path = [], {}, ""
        self.tool_call, self.reject_tools = tool_call, reject_tools
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = 0
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        from websockets.asyncio.server import serve

        async def handler(ws):
            self.path = ws.request.path
            self.headers = dict(ws.request.headers)
            await ws.send(json.dumps({"type": "session.created", "session": {"id": "s1"}}))
            answered = False
            async for raw in ws:
                ev = json.loads(raw)
                self.got.append(ev)
                t = ev["type"]
                if t == "session.update" and self.reject_tools and "tools" in ev["session"]:
                    await ws.send(json.dumps({"type": "error", "error": {"message": "tools not supported"}}))
                elif t == "session.update":
                    await ws.send(json.dumps({"type": "session.updated", "session": ev["session"]}))
                elif t == "input_audio_buffer.append" and not answered and sum(
                        1 for e in self.got if e["type"] == "input_audio_buffer.append") >= 3:
                    answered = True
                    for out in (
                        {"type": "input_audio_buffer.speech_started"},
                        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "这是什么"},
                        {"type": "response.created", "response": {"id": "r1"}},
                        {"type": "response.audio_transcript.delta", "delta": "这是"},
                        {"type": "response.audio.delta", "delta": b64(b"\x01\x00" * 240)},
                        {"type": "response.audio_transcript.delta", "delta": "一个杯子"},
                        {"type": "response.audio_transcript.done", "transcript": "这是一个杯子"},
                        {"type": "response.done", "response": {"status": "completed"}},
                    ):
                        await ws.send(json.dumps(out))
                    if self.tool_call:
                        await ws.send(json.dumps({"type": "response.function_call_arguments.done", "name": "create_aiw_task",
                                                  "call_id": "call_9", "arguments": json.dumps({"goal": "整理一份杯子市场报告"})}))
                elif t == "response.create":
                    await ws.send(json.dumps({"type": "response.created", "response": {"id": "r2"}}))
                    await ws.send(json.dumps({"type": "response.audio_transcript.done", "transcript": "已交给 aiw"}))
                    await ws.send(json.dumps({"type": "response.done", "response": {"status": "completed"}}))

        async def main():
            async with serve(handler, "127.0.0.1", 0) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self.ready.set()
                await asyncio.Future()
        try:
            self.loop.run_until_complete(main())
        except Exception:  # noqa: BLE001
            pass

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/api-ws/v1/realtime"

    def types(self):
        return [e["type"] for e in self.got]


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------------------------------------ app fixture
@pytest.fixture
def app(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    s.scheduler_enabled = False
    a = build_app(s)
    a.state.secrets.put("openai_api_key", "gw-key")
    return a


def set_models(app, **kw):
    m = dict(app.state.store.kv_get("models", {}) or {})
    m.update(kw)
    app.state.store.kv_set("models", m)


def recv_until(ws, t, limit=40):
    seen = []
    for _ in range(limit):
        e = json.loads(ws.receive_text())
        seen.append(e)
        if e["type"] == t:
            return e, seen
    raise AssertionError(f"no {t} in {seen}")


# ------------------------------------------------------------------------------------------------ tests: settings / auth
def test_config_derivations_and_status(app):
    assert oc.realtime_url_for("https://dashscope.aliyuncs.com/compatible-mode/v1") == "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    assert oc.realtime_url_for("https://ws123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == \
        "wss://ws123.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
    assert oc.realtime_url_for("http://192.168.50.188:28000/v1") == oc.DASHSCOPE_RT
    assert oc.realtime_model_for("qwen3.8-omni-flash") == "qwen3.8-omni-flash-realtime"
    assert oc.realtime_model_for("qwen3.5-omni-plus-realtime") == "qwen3.5-omni-plus-realtime"
    assert oc.with_model("wss://h/api-ws/v1/realtime", "m1") == "wss://h/api-ws/v1/realtime?model=m1"
    assert oc.with_model("wss://h/x?model=a", "m1") == "wss://h/x?model=a"
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        st = c.get("/api/omni/status", headers=H).json()
        assert st["enabled"] is False and "通话模型" in st["message"] and st["voice"] == "Tina"
        assert c.put("/api/settings/models", headers=H, json={"omni_model": "qwen3.8-omni-flash", "omni_mode": "bogus"}).status_code == 400
        r = c.put("/api/settings/models", headers=H, json={"omni_model": "qwen3.8-omni-flash", "omni_voice": "Serena",
                                                          "omni_realtime_url": "wss://w1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"})
        assert r.status_code == 200
        st = c.get("/api/omni/status", headers=H).json()
        assert st["enabled"] and st["model"] == "qwen3.8-omni-flash" and st["voice"] == "Serena"
        assert st["realtime_model"] == "qwen3.8-omni-flash-realtime" and st["realtime_url"].startswith("wss://w1.")
        assert c.patch("/api/settings/models", headers=H, json={"omni_max_minutes": "999"}).status_code == 400
        assert c.get("/api/omni/status").status_code == 401


def test_disabled_when_omni_model_empty(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            e = json.loads(ws.receive_text())
            assert e["type"] == "error" and e["code"] == "disabled" and "通话模型" in e["message"]
            assert json.loads(ws.receive_text())["type"] == "ended"


def test_auth_required(app):
    from fastapi.testclient import TestClient
    set_models(app, omni_model="qwen3.5-omni-flash")
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/omni-call?token=bad") as ws:
                ws.receive_text()
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/omni-call") as ws:
                ws.receive_text()


# ------------------------------------------------------------------------------------------------ tests: realtime
def test_realtime_call_end_to_end_with_tool_and_transcript(app):
    up = FakeRealtime(tool_call=True)
    set_models(app, omni_model="qwen3.8-omni-flash", omni_realtime_url=up.url, omni_voice="Serena")
    chat = app.state.chat
    started = []

    async def fake_start(prompt, title="", machine_id=None, attachments=None, origin=None, source="chat", fast=None):
        started.append((prompt, origin, source))
        return chat.add("assistant", "task", "整理报告", {"task_id": "task-77", "title": "整理报告"}, task_id="task-77")
    chat.start_task = fake_start
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True}))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready" and ready["mode"] == "realtime" and ready["model"] == "qwen3.8-omni-flash-realtime"
            assert ready["voice"] == "Serena" and ready["audio_out"] is True
            # upstream saw the bearer key (gateway key: no omni_realtime_key), the model query and the session config
            assert up.headers.get("authorization") == "Bearer gw-key"
            assert "model=qwen3.8-omni-flash-realtime" in up.path
            sess = next(e for e in up.got if e["type"] == "session.update")["session"]
            assert sess["voice"] == "Serena" and sess["audio"]["output"]["voice"] == "Serena"
            assert sess["turn_detection"]["type"] == "server_vad" and sess["input_audio_format"] == "pcm16"
            assert sess["audio"]["input"]["format"]["sample_rate"] == 16000 and sess["audio"]["output"]["format"]["sample_rate"] == 24000
            assert {t["function"]["name"] for t in sess["tools"]} == {"create_aiw_task", "task_status"}
            # a frame before any audio waits (DashScope wants audio first); a second frame within 1 s is dropped
            ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
            ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
            for _ in range(3):
                ws.send_text(json.dumps({"type": "audio", "data": b64(tone(40))}))
            assert wait_for(lambda: "input_image_buffer.append" in up.types())
            ty = up.types()
            assert ty.index("input_audio_buffer.append") < ty.index("input_image_buffer.append")
            assert ty.count("input_image_buffer.append") == 1
            got = []
            e, got = recv_until(ws, "tool")
            types = [x["type"] for x in got]
            assert "speech_started" in types and "audio" in types
            assert any(x["type"] == "transcript" and x["role"] == "user" and x["final"] and x["text"] == "这是什么" for x in got)
            assert any(x["type"] == "transcript" and x["role"] == "assistant" and x["final"] and x["text"] == "这是一个杯子" for x in got)
            assert base64.b64decode(next(x for x in got if x["type"] == "audio")["data"]) == b"\x01\x00" * 240
            assert e["name"] == "create_aiw_task" and e["status"] == "running"
            e, _ = recv_until(ws, "tool")
            assert e["status"] == "done" and "已交给 aiw" in e["text"]
            assert wait_for(lambda: "response.create" in up.types())
            out = next(x for x in up.got if x["type"] == "conversation.item.create")["item"]
            assert out["type"] == "function_call_output" and out["call_id"] == "call_9" and "已交给 aiw，完成后推送" in out["output"]
            assert started == [("整理一份杯子市场报告", "call", "chat")]
            recv_until(ws, "transcript")
            ws.send_text(json.dumps({"type": "hangup"}))
            recv_until(ws, "ended")
    rows = [r for r in chat._recent_rows() if (r.get("data") or {}).get("call")]
    assert len(rows) == 1
    t = rows[0]["text"]
    assert t.startswith("📞 视频通话记录") and "我：这是什么" in t and "小秘：这是一个杯子" in t
    assert rows[0]["data"]["call"]["mode"] == "realtime" and rows[0]["data"]["call"]["tasks"] == ["task-77"]


def test_realtime_key_and_speech_started_cancels_response():
    up = FakeRealtime()

    async def run():
        sink = Sink()
        rt = oc.RealtimeBackend(cfg(rt_url=up.url, rt_key="omni-secret"), sink, None, "hi", False)
        await rt.start()
        assert "tools" not in next(e for e in up.got if e["type"] == "session.update")["session"]   # no bridge, no tools
        rt.responding = True
        await rt._on({"type": "input_audio_buffer.speech_started"})
        await asyncio.sleep(0.2)
        assert sink.of("speech_started") and "response.cancel" in up.types()
        await rt.close()
    asyncio.run(run())
    assert up.headers.get("authorization") == "Bearer omni-secret"


def test_realtime_session_retries_without_tools():
    up = FakeRealtime(reject_tools=True)

    async def run():
        rt = oc.RealtimeBackend(cfg(rt_url=up.url), Sink(), oc.ToolBridge(FakeChat()), "hi", True)
        await rt.start()
        await rt.close()
    asyncio.run(run())
    ups = [e["session"] for e in up.got if e["type"] == "session.update"]
    assert len(ups) == 2 and "tools" in ups[0] and "tools" not in ups[1] and "input_audio_transcription" in ups[1]


# ------------------------------------------------------------------------------------------------ tests: turn mode
class Gateway:
    """Fake OpenAI-compatible gateway: records each chat/completions body and answers per `mode`."""

    def __init__(self, reply=None, slow=False, reject_audio=False):
        self.bodies, self.slow, self.reject_audio = [], slow, reject_audio
        self.reply = reply
        self.cancelled = False

    async def __call__(self, req: httpx.Request):
        body = json.loads(req.content)
        self.bodies.append(body)
        if body["modalities"] == ["text"] and isinstance(body["messages"][-1]["content"], list) and any(
                p.get("type") == "text" and "转写" in p.get("text", "") for p in body["messages"][-1]["content"]):
            return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "桌上这个是什么"}}]}))
        if self.reject_audio and "audio" in body:
            return httpx.Response(400, json={"error": {"message": "This model does not support audio output"}})
        if self.reply:
            return self.reply(body)
        if self.slow:
            async def gen():
                try:
                    yield b"data: " + json.dumps({"choices": [{"delta": {"content": "我看"}}]}).encode() + b"\n\n"
                    yield b"data: " + json.dumps({"choices": [{"delta": {"audio": {"data": b64(b"\x02\x00" * 100)}}}]}).encode() + b"\n\n"
                    await asyncio.sleep(10)
                    yield b"data: [DONE]\n\n"
                except (asyncio.CancelledError, GeneratorExit):
                    self.cancelled = True
                    raise
            return httpx.Response(200, content=gen())
        if "audio" not in body:
            return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "是一个杯子。"}}]}))
        wav_head = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 20 + b"data\x00\x00\x00\x00"
        return httpx.Response(200, content=sse(
            {"choices": [{"delta": {"content": "是一个"}}]},
            {"choices": [{"delta": {"audio": {"data": b64(wav_head + b"\x03\x00" * 50 + b"\x04")}}}]},
            {"choices": [{"delta": {"audio": {"data": b64(b"\x00" + b"\x05\x00" * 10)}}}]},
            {"choices": [{"delta": {"content": "杯子。"}, "finish_reason": "stop"}]}))


def speak(ms=600):
    return tone(ms) + silence(900)


async def feed(be, pcm, chunk_ms=40):
    step = 32 * chunk_ms
    for i in range(0, len(pcm), step):
        part = pcm[i:i + step]
        await be.audio(b64(part), part)


async def settle(be, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if (be.job is None or be.job.done()) and not be.side:
            return
        await asyncio.sleep(0.02)


def test_turn_mode_request_shape_audio_and_transcripts():
    gw = Gateway()
    oc.TRANSPORT = httpx.MockTransport(gw)

    async def run():
        sink = Sink()
        be = oc.TurnBackend(cfg(), sink, oc.ToolBridge(FakeChat()), "SYS", True)
        await be.frame(JPEG)
        await feed(be, silence(300) + speak())
        await settle(be)
        await be.close()
        return sink
    sink = asyncio.run(run())
    main = next(b for b in gw.bodies if "audio" in b)
    assert main["stream"] is True and main["modalities"] == ["text", "audio"] and main["audio"] == {"voice": "Tina", "format": "wav"}
    assert main["model"] == "qwen3.5-omni-flash" and main["enable_thinking"] is False
    assert {t["function"]["name"] for t in main["tools"]} == {"create_aiw_task", "task_status"}
    assert main["messages"][0] == {"role": "system", "content": "SYS"}
    parts = main["messages"][-1]["content"]
    assert parts[0]["type"] == "input_audio" and parts[0]["input_audio"]["format"] == "wav"
    wav = base64.b64decode(parts[0]["input_audio"]["data"].split(",", 1)[1])
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64," + JPEG
    assert sink.of("speech_started")
    pcm = b"".join(base64.b64decode(e["data"]) for e in sink.of("audio"))
    assert pcm == b"\x03\x00" * 50 + b"\x04\x00" + b"\x05\x00" * 10   # wav header stripped, odd byte carried over
    finals = [e for e in sink.of("transcript") if e["final"]]
    assert {"role": "assistant", "text": "是一个杯子。"}.items() <= next(e for e in finals if e["role"] == "assistant").items()
    assert not any(e.get("speak") for e in finals)
    assert any(e["role"] == "user" and e["text"] == "桌上这个是什么" for e in finals)


def test_turn_mode_barge_in_cancels_inflight_answer():
    gw = Gateway(slow=True)
    oc.TRANSPORT = httpx.MockTransport(gw)

    async def run():
        sink = Sink()
        be = oc.TurnBackend(cfg(), sink, None, "SYS", False)
        await feed(be, silence(200) + speak())
        for _ in range(100):
            if sink.of("audio"):
                break
            await asyncio.sleep(0.02)
        assert sink.of("audio")
        first_job = be.job
        n0 = len(sink.of("speech_started"))
        await feed(be, tone(300))            # the user talks over the answer
        assert len(sink.of("speech_started")) == n0 + 1
        assert first_job.done() and first_job.cancelled()
        await be.close()
        return sink
    sink = asyncio.run(run())
    cut = [e for e in sink.of("transcript") if e["role"] == "assistant" and e["final"]]
    assert cut and cut[-1]["text"].endswith("…")


def test_turn_mode_text_only_model_falls_back_and_client_speaks():
    gw = Gateway(reject_audio=True)
    oc.TRANSPORT = httpx.MockTransport(gw)

    async def run():
        sink = Sink()
        be = oc.TurnBackend(cfg(model="qwen3.8-omni-flash"), sink, None, "SYS", False)
        await feed(be, silence(200) + speak())
        await settle(be)
        await be.close()
        return sink, be
    sink, be = asyncio.run(run())
    asst = [e for e in sink.of("transcript") if e["role"] == "assistant" and e["final"]]
    assert asst[-1]["text"] == "是一个杯子。" and asst[-1]["speak"] is True
    assert oc._TURN_VARIANT["qwen3.8-omni-flash"] == 2 and be.audio_out is False
    assert not sink.of("error")


def test_turn_mode_text_only_model_with_tts_model():
    def reply(body):
        if body["model"] == "qwen3.5-omni-flash":   # the TTS model reads the sentence
            assert body["messages"][-1]["content"] == "是一个杯子。"
            return httpx.Response(200, content=sse({"choices": [{"delta": {"audio": {"data": b64(b"\x07\x00" * 8)}}}]}))
        if "audio" in body:
            return httpx.Response(400, json={"error": {"message": "audio output not supported"}})
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "是一个杯子。"}}]}))
    gw = Gateway(reply=reply)
    oc.TRANSPORT = httpx.MockTransport(gw)

    async def run():
        sink = Sink()
        be = oc.TurnBackend(cfg(model="qwen3.8-omni-flash", tts_model="qwen3.5-omni-flash"), sink, None, "SYS", False)
        await feed(be, silence(200) + speak())
        await settle(be)
        await be.close()
        return sink
    sink = asyncio.run(run())
    assert b"".join(base64.b64decode(e["data"]) for e in sink.of("audio")) == b"\x07\x00" * 8
    asst = [e for e in sink.of("transcript") if e["role"] == "assistant" and e["final"]]
    assert asst[-1]["text"] == "是一个杯子。" and not asst[-1].get("speak")


def test_turn_mode_tool_call_round_trip():
    calls = {"n": 0}

    def reply(body):
        if body["modalities"] == ["text"]:
            return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "x"}}]}))
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, content=sse(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "create_aiw_task", "arguments": "{\"goal\": "}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"写周报\"}"}}]}, "finish_reason": "tool_calls"}]}))
        assert body["messages"][-1]["role"] == "tool" and "已交给 aiw" in body["messages"][-1]["content"]
        assert body["messages"][-2]["tool_calls"][0]["function"]["name"] == "create_aiw_task"
        return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "好的，已交给 aiw。"}}]}))
    gw = Gateway(reply=reply)
    oc.TRANSPORT = httpx.MockTransport(gw)
    fc = FakeChat()

    async def run():
        sink = Sink()
        be = oc.TurnBackend(cfg(), sink, oc.ToolBridge(fc), "SYS", False)
        await feed(be, silence(200) + speak())
        await settle(be)
        await be.close()
        return sink
    sink = asyncio.run(run())
    assert fc.started == [("写周报", "call", "chat")]
    assert [e["status"] for e in sink.of("tool")] == ["running", "done"]


# ------------------------------------------------------------------------------------------------ tests: call-level
def test_fallback_realtime_to_turn_and_frame_cap():
    gw = Gateway()
    oc.TRANSPORT = httpx.MockTransport(gw)

    async def run():
        sink = Sink()
        call = oc.OmniCall(cfg(mode="auto", rt_url="ws://127.0.0.1:9/nothing", rt_timeout=0.5), sink, FakeChat(), video=True)
        ready = await call.start()
        assert ready["mode"] == "turn" and "实时接口连不上" in ready["note"] and "轮流对话" in ready["note"]
        assert (call.cfg["rt_url"], call.cfg["rt_model"]) in oc._RT_FAIL
        # frame cap: 1 fps
        assert await call.on_frame(JPEG) is True
        assert await call.on_frame(JPEG) is False
        assert await call.on_frame("A" * (oc.MAX_FRAME_B64 + 4)) is False
        assert await call.on_frame("not base64 !!") is False
        call.last_frame -= 1.1
        assert await call.on_frame(JPEG) is True
        assert any(e.get("code") == "frame_too_big" for e in sink.of("error"))
        # next call in auto mode skips the realtime attempt for a while
        call2 = oc.OmniCall(cfg(mode="auto", rt_url="ws://127.0.0.1:9/nothing"), Sink(), None)
        t0 = time.time()
        r2 = await call2.start()
        assert r2["mode"] == "turn" and time.time() - t0 < 0.3
        # forced realtime: no fallback
        with pytest.raises(RuntimeError):
            await oc.OmniCall(cfg(mode="realtime", rt_url="ws://127.0.0.1:9/nothing", rt_timeout=0.3), Sink(), None).start()
        await call.finish()
        await call2.finish()
    asyncio.run(run())


def test_transcript_saved_to_chat_at_hangup():
    async def run():
        fc = FakeChat()
        call = oc.OmniCall(cfg(), Sink(), fc, video=False)
        await call.start()
        await call._emit({"type": "transcript", "role": "user", "text": "明天几点开会", "final": True})
        await call._emit({"type": "transcript", "role": "assistant", "text": "上午十点", "final": False})
        await call._emit({"type": "transcript", "role": "assistant", "text": "上午十点。", "final": True})
        m = await call.finish("hangup")
        assert await call.finish("again") is None   # only once
        empty = oc.OmniCall(cfg(), Sink(), fc)
        await empty.start()
        assert await empty.finish() is None          # nothing said: nothing saved
        return fc, m
    fc, m = asyncio.run(run())
    assert len(fc.added) == 1 and m["kind"] == "text" and m["role"] == "assistant"
    assert m["text"].startswith("📞 语音通话记录") and "（轮流）" in m["text"]
    assert m["text"].endswith("我：明天几点开会\n小秘：上午十点。")
    assert m["data"]["call"]["turns"] == 2 and m["data"]["call"]["mode"] == "turn"


def test_ws_turn_mode_end_to_end_and_limits(app, monkeypatch):
    gw = Gateway()
    oc.TRANSPORT = httpx.MockTransport(gw)
    set_models(app, omni_model="qwen3.5-omni-flash", omni_mode="turn", omni_max_minutes="1")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "voice": "Ethan"}))
            ready = json.loads(ws.receive_text())
            assert ready["mode"] == "turn" and ready["model"] == "qwen3.5-omni-flash" and ready["voice"] == "Ethan"
            assert ready["max_seconds"] == 60
            pcm = silence(200) + speak()
            for i in range(0, len(pcm), 1280):
                ws.send_text(json.dumps({"type": "audio", "data": b64(pcm[i:i + 1280])}))
            recv_until(ws, "audio")
            e, _ = recv_until(ws, "transcript", 60)
            ws.send_text(json.dumps({"type": "interrupt"}))
            ws.send_text(json.dumps({"type": "hangup"}))
            recv_until(ws, "ended", 60)
        assert next(b for b in gw.bodies if "audio" in b)["audio"]["voice"] == "Ethan"
        # first message must be start
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "audio", "data": ""}))
            e, _ = recv_until(ws, "ended")
            assert e["reason"] == "no_start"
        # idle: no message for idle_s ends the call
        monkeypatch.setenv("AIW_OMNI_IDLE", "0.3")
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False}))
            recv_until(ws, "ready")
            e, seen = recv_until(ws, "ended")
            assert e["reason"] == "idle" and any(x.get("code") == "idle" for x in seen)


def test_tool_bridge_create_and_status():
    async def run():
        fc = FakeChat()
        b = oc.ToolBridge(fc)
        r = await b.run("create_aiw_task", json.dumps({"goal": "把上周的会议纪要整理成 Word"}))
        assert r.startswith("已交给 aiw，完成后推送") and fc.started[0][1:] == ("call", "chat")
        assert "没听清" in await b.run("create_aiw_task", {"goal": "  "})
        await b.run("create_aiw_task", {"goal": "b"})
        await b.run("create_aiw_task", {"goal": "c"})
        assert "3 个任务" in await b.run("create_aiw_task", {"goal": "d"})
        assert len(fc.started) == 3
        assert "整理报告" in await b.run("task_status", {})
        assert "没有这个工具" in await b.run("run_command", {"command": "rm -rf /"})
    asyncio.run(run())


def test_energy_vad_segments_and_ignores_blips():
    v = oc.EnergyVAD()
    ev = v.feed(silence(500) + tone(600) + silence(900))
    assert [e for e, _ in ev] == ["start", "end"]
    seg = ev[1][1]
    assert 0.6 * 32000 * 0.9 < len(seg) < (0.6 + 0.3 + 0.08 + 0.75) * 32000
    v2 = oc.EnergyVAD()
    ev2 = v2.feed(silence(300) + tone(100) + silence(900))   # a 100 ms click: started, but too short to answer
    assert [e for e, _ in ev2] == ["start", "end"] and ev2[1][1] == b""
    v3 = oc.EnergyVAD()
    v3.boost = 50.0
    assert v3.feed(tone(300)) == []          # while the assistant talks, its echo does not count as speech


def test_probe_reports_working_mode(app):
    up = FakeRealtime()
    gw = Gateway(reject_audio=True)
    oc.TRANSPORT = httpx.MockTransport(gw)

    async def run():
        ok = await oc.probe(cfg(model="qwen3.8-omni-flash", rt_url=up.url, rt_model="qwen3.8-omni-flash-realtime"))
        bad = await oc.probe(cfg(model="qwen3.8-omni-flash", rt_url="ws://127.0.0.1:9/x", rt_model="qwen3.8-omni-flash-realtime",
                                 rt_timeout=0.3))
        return ok, bad
    ok, bad = asyncio.run(run())
    assert ok["mode"] == "realtime" and ok["realtime"]["ok"] and ok["turn"]["ok"] and ok["turn"]["text_only"]
    assert bad["mode"] == "turn" and not bad["realtime"]["ok"] and bad["turn"]["audio"] is False
    assert any("只输出文字" in n for n in bad["notes"]) and any("业务空间" in n for n in bad["notes"])


# ------------------------------------------------------------------------------------------------ tests: clients wired
def test_web_and_ios_clients_are_wired():
    import pathlib
    import shutil
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    web, ios = root / "apps" / "web", root / "apps" / "ios"
    html = (web / "index.html").read_text(encoding="utf-8")
    assert 'id="callBtn"' in html and '<script src="call.js">' in html and 'href="call.css"' in html and "openCall(true)" in html
    js = (web / "call.js").read_text(encoding="utf-8")
    for needle in ("/ws/omni-call", "echoCancellation: true", "audioWorklet", "createScriptProcessor", "image/jpeg", "0.6", "720",
                   "speech_started", '"interrupt"', '"hangup"', "24000", "speechSynthesis"):
        assert needle in js, needle
    assert 'key: "omni_model", label: "通话模型（全模态）"' in (web / "app.js").read_text(encoding="utf-8")
    assert 'name="omni_voice"' in (web / "ui.js").read_text(encoding="utf-8")
    if shutil.which("node"):
        for f in ("call.js", "app.js", "ui.js"):
            r = subprocess.run(["node", "--check", str(web / f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
    sw = (ios / "Sources" / "CallView.swift").read_text(encoding="utf-8")
    for needle in (".playAndRecord", ".voiceChat", "setVoiceProcessingEnabled", "AVAudioPlayerNode", "AVCaptureSession",
                   "URLSessionWebSocketTask", "/ws/omni-call", "sampleRate: 16000", "sampleRate: 24000", "jpegData(compressionQuality: 0.6)"):
        assert needle in sw, needle
    assert "CallView(video: true)" in (ios / "Sources" / "HomeChat.swift").read_text(encoding="utf-8")
    import plistlib
    p = plistlib.loads((ios / "Info.plist").read_bytes())
    assert "视频通话" in p["NSCameraUsageDescription"] and "视频通话" in p["NSMicrophoneUsageDescription"]
