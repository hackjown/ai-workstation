"""v3.9.32: Gemini Live API backend for the omni call (/ws/omni-call, mode "gemini"): against a fake local
BidiGenerateContent server (a real websocket server in its own thread) - the setup message, audio / frame forwarding,
audio out, transcripts, interrupted -> speech_started, toolCall -> toolResponse with create_aiw_task, goAway and a
dropped connection resumed with the sessionResumption handle, omni_proxy through an HTTP CONNECT proxy (websockets'
own proxy= and the CONNECT tunnel used for websockets < 15), enabled / disabled logic, auto ordering, and the probe."""
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
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16 * ms))


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


JPEG = b64(b"\xff\xd8\xff\xe0" + b"x" * 200 + b"\xff\xd9")
AUDIO_OUT = b"\x02\x00" * 240


def cfg(**kw):
    c = {"model": "", "voice": "Tina", "base_url": "http://gw/v1", "key": "gk", "rt_url": "ws://127.0.0.1:9/x",
         "rt_model": "qwen3.5-omni-flash-realtime", "rt_key": "rk", "tts_model": "", "mode": "gemini", "max_seconds": 1800,
         "rt_timeout": 0.5, "max_fps": 1.0, "idle_s": 30, "audio_format": "wav",
         "gemini_model": "gemini-3.8-live", "gemini_voice": "Kore", "gemini_url": "ws://127.0.0.1:9/x",
         "gemini_key": "AIzaTEST", "proxy": "", "gemini_timeout": 2.0}
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
            return d

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
        return json.dumps({"tasks": [], "routines": []})


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # the local fake servers must never be reached through a proxy from the environment
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    oc._RT_FAIL.clear()
    oc._TURN_VARIANT.clear()
    oc._ACTIVE.clear()
    monkeypatch.setenv("AIW_OMNI_TURN_ASR", "0")
    yield
    oc.TRANSPORT = None


# ------------------------------------------------------------------------------------------------ fake Gemini Live
class FakeGemini:
    """BidiGenerateContent-shaped server: setup -> sessionResumptionUpdate + setupComplete; after 3 audio chunks an
    answer (input + output transcription, inlineData audio, turnComplete) and optionally a toolCall; a toolResponse gets
    a spoken confirmation. push() sends any message on the newest connection; drop() closes it."""

    def __init__(self, tool_call=False, reject=""):
        self.conns = []          # one dict per connection: path, headers, setup, got
        self.tool_call, self.reject = tool_call, reject
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = 0
        self.live = []
        threading.Thread(target=self._run, daemon=True).start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        from websockets.asyncio.server import serve

        async def handler(ws):
            c = {"path": ws.request.path, "headers": dict(ws.request.headers), "setup": None, "got": []}
            self.conns.append(c)
            self.live.append(ws)
            n = len(self.conns)
            answered = False
            async for raw in ws:
                ev = json.loads(raw)
                c["got"].append(ev)
                if "setup" in ev:
                    c["setup"] = ev["setup"]
                    if self.reject:
                        await ws.close(1008, self.reject)
                        return
                    await ws.send(json.dumps({"sessionResumptionUpdate": {"newHandle": f"h{n}", "resumable": True}}))
                    await ws.send(json.dumps({"setupComplete": {}}))
                elif "realtimeInput" in ev and "audio" in ev["realtimeInput"] and not answered and n == 1 and sum(
                        1 for e in c["got"] if "audio" in e.get("realtimeInput", {})) >= 3:
                    answered = True
                    for out in (
                        {"serverContent": {"inputTranscription": {"text": "这是"}}},
                        {"serverContent": {"inputTranscription": {"text": "什么"}}},
                        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                                                    "data": b64(AUDIO_OUT)}}]},
                                           "outputTranscription": {"text": "这是"}}},
                        {"serverContent": {"outputTranscription": {"text": "一个杯子"}}},
                        {"serverContent": {"generationComplete": True}},
                        {"serverContent": {"turnComplete": True}},
                    ):
                        await ws.send(json.dumps(out, ensure_ascii=False))
                    if self.tool_call:
                        await ws.send(json.dumps({"toolCall": {"functionCalls": [
                            {"id": "fc-1", "name": "create_aiw_task", "args": {"goal": "整理一份杯子市场报告"}}]}},
                            ensure_ascii=False))
                elif "toolResponse" in ev:
                    await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "已交给 aiw"}}},
                                             ensure_ascii=False))
                    await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))

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
        return f"ws://127.0.0.1:{self.port}"

    def push(self, msg):
        ws = self.live[-1]
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(msg, ensure_ascii=False)), self.loop).result(3)

    def drop(self):
        ws = self.live[-1]
        asyncio.run_coroutine_threadsafe(ws.close(1011, "internal"), self.loop).result(3)

    def got(self, i=-1):
        return self.conns[i]["got"]


class ConnectProxy:
    """A minimal HTTP CONNECT proxy: records the CONNECT targets and pipes bytes both ways."""

    def __init__(self):
        self.targets = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = 0
        threading.Thread(target=self._run, daemon=True).start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def pipe(r, w):
            try:
                while True:
                    d = await r.read(65536)
                    if not d:
                        break
                    w.write(d)
                    await w.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

        async def handle(r, w):
            head = await r.readuntil(b"\r\n\r\n")
            line = head.split(b"\r\n", 1)[0].decode()
            meth, target, _ = line.split(" ", 2)
            if meth != "CONNECT":
                w.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                w.close()
                return
            self.targets.append(target)
            host, port = target.rsplit(":", 1)
            ur, uw = await asyncio.open_connection(host, int(port))
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await w.drain()
            await asyncio.gather(pipe(r, uw), pipe(ur, w))

        async def main():
            srv = await asyncio.start_server(handle, "127.0.0.1", 0)
            self.port = srv.sockets[0].getsockname()[1]
            self.ready.set()
            async with srv:
                await srv.serve_forever()
        try:
            self.loop.run_until_complete(main())
        except Exception:  # noqa: BLE001
            pass


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


def recv_until(ws, t, limit=60):
    seen = []
    for _ in range(limit):
        e = json.loads(ws.receive_text())
        seen.append(e)
        if e["type"] == t:
            return e, seen
    raise AssertionError(f"no {t} in {seen}")


# ------------------------------------------------------------------------------------------------ tests: config
def test_url_and_settings_and_enabled_logic(app, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert oc.gemini_url_for("") == ("wss://generativelanguage.googleapis.com/ws/"
                                     "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
    assert oc.gemini_url_for("https://relay.example.com/") == "wss://relay.example.com" + oc.GEMINI_PATH
    full = "wss://r.example.com/gl/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    assert oc.gemini_url_for(full) == full
    assert oc.with_key("wss://h/p", "K") == "wss://h/p?key=K" and oc.with_key("wss://h/p?key=A", "K") == "wss://h/p?key=A"
    assert oc.DEFAULT_GEMINI_VOICE in oc.GEMINI_VOICES and "Kore" in oc.GEMINI_VOICES
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        st = c.get("/api/omni/status", headers=H).json()
        assert st["enabled"] is False and st["gemini"]["has_key"] is False and st["gemini"]["model"] == "gemini-3.8-live"
        assert st["gemini"]["voice"] == "Puck"
        # a Gemini key alone (omni_model empty) turns calls on in auto mode
        assert c.put("/api/secrets/omni_gemini_key", headers=H, json={"value": "AIzaXYZ"}).status_code == 200
        st = c.get("/api/omni/status", headers=H).json()
        assert st["enabled"] is True and st["gemini"]["on"] is True and not st.get("message")
        assert c.get("/api/settings/models", headers=H).json()["has_gemini_key"] is True
        # gemini mode + new keys save; a bad proxy URL is refused
        r = c.patch("/api/settings/models", headers=H, json={"omni_mode": "gemini", "omni_gemini_model": "gemini-3.1-flash-live-preview",
                                                            "omni_gemini_voice": "Kore", "omni_proxy": "socks5h://10.0.0.2:1080",
                                                            "omni_gemini_url": "wss://relay.example.com"})
        assert r.status_code == 200, r.text
        st = c.get("/api/omni/status", headers=H).json()
        assert st["enabled"] and st["mode"] == "gemini" and st["gemini"]["model"] == "gemini-3.1-flash-live-preview"
        assert st["gemini"]["url"] == "wss://relay.example.com" + oc.GEMINI_PATH and st["gemini"]["proxy"] is True
        assert c.patch("/api/settings/models", headers=H, json={"omni_proxy": "ftp://x:1"}).status_code == 400
        assert c.patch("/api/settings/models", headers=H, json={"omni_mode": "nope"}).status_code == 400
        cfgd = oc.config(app.state.store, app.state.secrets)
        assert cfgd["proxy"] == "socks5h://10.0.0.2:1080" and cfgd["gemini_voice"] == "Kore" and cfgd["gemini_key"] == "AIzaXYZ"
        # turn / realtime mode need omni_model: a Gemini key alone does not enable them
        c.patch("/api/settings/models", headers=H, json={"omni_mode": "turn"})
        assert c.get("/api/omni/status", headers=H).json()["enabled"] is False
        # gemini mode without a key: off, with a Gemini-specific message
        c.delete("/api/secrets/omni_gemini_key", headers=H)
        c.patch("/api/settings/models", headers=H, json={"omni_mode": "gemini", "omni_model": "qwen3.8-omni-flash"})
        st = c.get("/api/omni/status", headers=H).json()
        assert st["enabled"] is False and "Gemini" in st["message"]
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            e = json.loads(ws.receive_text())
            assert e["type"] == "error" and e["code"] == "disabled" and "Gemini" in e["message"]


def test_auto_plan_order():
    assert oc.OmniCall(cfg(mode="auto", model="m"), Sink()).plan() == ["gemini", "realtime", "turn"]
    assert oc.OmniCall(cfg(mode="auto", model="m", gemini_key=""), Sink()).plan() == ["realtime", "turn"]
    assert oc.OmniCall(cfg(mode="auto", model=""), Sink()).plan() == ["gemini"]
    assert oc.OmniCall(cfg(mode="gemini", model="m"), Sink()).plan() == ["gemini"]
    assert oc.OmniCall(cfg(mode="turn", model="m"), Sink()).plan() == ["turn"]
    # a pre-v3.9.32 config dict (no gemini keys) still works
    old = {k: v for k, v in cfg(mode="auto", model="m").items() if not k.startswith("gemini") and k != "proxy"}
    assert oc.OmniCall(old, Sink()).plan() == ["realtime", "turn"]


# ------------------------------------------------------------------------------------------------ tests: backend
def test_setup_message_audio_frame_and_answer():
    up = FakeGemini()

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, oc.ToolBridge(FakeChat()), "你是小秘", True)
        await g.start()
        await g.frame(JPEG)
        for _ in range(3):
            pcm = tone(40)
            await g.audio(b64(pcm), pcm)
        for _ in range(100):
            if any(e["type"] == "transcript" and e["role"] == "assistant" and e["final"] for e in sink.events):
                break
            await asyncio.sleep(0.03)
        await g.close()
        return sink, g
    sink, g = asyncio.run(run())
    c = up.conns[0]
    # key in the query (as documented for WebSockets) and as x-goog-api-key
    assert c["path"].startswith(oc.GEMINI_PATH + "?key=AIzaTEST") and c["headers"].get("x-goog-api-key") == "AIzaTEST"
    s = c["setup"]
    assert s["model"] == "models/gemini-3.8-live"
    assert s["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert s["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert s["systemInstruction"] == {"parts": [{"text": "你是小秘"}]}
    assert s["inputAudioTranscription"] == {} and s["outputAudioTranscription"] == {}
    assert s["realtimeInputConfig"]["automaticActivityDetection"]["disabled"] is False
    assert s["contextWindowCompression"] == {"slidingWindow": {}} and s["sessionResumption"] == {}
    decls = s["tools"][0]["functionDeclarations"]
    assert {d["name"] for d in decls} == {"create_aiw_task", "task_status"}
    cat = next(d for d in decls if d["name"] == "create_aiw_task")
    assert cat["parameters"]["type"] == "OBJECT" and cat["parameters"]["properties"]["goal"]["type"] == "STRING"
    assert cat["parameters"]["required"] == ["goal"] and "required" not in next(d for d in decls if d["name"] == "task_status")["parameters"]
    # the first client message is setup; then realtimeInput video / audio with the current field names
    got = c["got"]
    assert "setup" in got[0] and len(got[0]) == 1
    vid = next(e for e in got if "video" in e.get("realtimeInput", {}))["realtimeInput"]["video"]
    assert vid == {"data": JPEG, "mimeType": "image/jpeg"}
    aud = [e["realtimeInput"]["audio"] for e in got if "audio" in e.get("realtimeInput", {})]
    assert len(aud) == 3 and aud[0]["mimeType"] == "audio/pcm;rate=16000" and base64.b64decode(aud[0]["data"]) == tone(40)
    assert not any("mediaChunks" in e.get("realtimeInput", {}) for e in got)
    # audio out unchanged (24 kHz pcm16 base64), transcripts partial then final, handle kept
    assert base64.b64decode(sink.of("audio")[0]["data"]) == AUDIO_OUT
    users = [e for e in sink.of("transcript") if e["role"] == "user"]
    assert users[-1] == {"type": "transcript", "role": "user", "text": "这是什么", "final": True}
    assert any(not e["final"] for e in users)
    asst = [e for e in sink.of("transcript") if e["role"] == "assistant"]
    assert asst[-1]["text"] == "这是一个杯子" and asst[-1]["final"] and any(not e["final"] for e in asst)
    assert g.handle == "h1"


def test_interrupted_sends_speech_started_and_cuts_transcript():
    up = FakeGemini()

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        await g.start()
        await asyncio.to_thread(up.push, {"serverContent": {"outputTranscription": {"text": "我给你讲一个很长的故事"}}})
        await asyncio.to_thread(up.push, {"serverContent": {"interrupted": True}})
        await asyncio.to_thread(up.push, {"serverContent": {"turnComplete": True}})
        for _ in range(100):
            if sink.of("speech_started") and any(e.get("final") for e in sink.of("transcript")):
                break
            await asyncio.sleep(0.03)
        await g.interrupt()   # client interrupt: nothing is sent upstream
        await g.close()
        return sink
    sink = asyncio.run(run())
    assert len(sink.of("speech_started")) == 1
    fin = [e for e in sink.of("transcript") if e["final"]]
    assert fin == [{"type": "transcript", "role": "assistant", "text": "我给你讲一个很长的故事…", "final": True}]
    assert [list(e) for e in up.got()][1:] == []   # only the setup went upstream
    assert "tools" not in up.conns[0]["setup"]       # no bridge: no tools


def test_tool_call_round_trip_with_create_aiw_task():
    up = FakeGemini(tool_call=True)
    chat = FakeChat()

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, oc.ToolBridge(chat), "hi", False)
        await g.start()
        for _ in range(3):
            pcm = tone(40)
            await g.audio(b64(pcm), pcm)
        for _ in range(150):
            if any(e["role"] == "assistant" and e["final"] and "已交给" in e["text"] for e in sink.of("transcript")):
                break
            await asyncio.sleep(0.03)
        await g.close()
        return sink
    sink = asyncio.run(run())
    assert chat.started == [("整理一份杯子市场报告", "call", "chat")]
    tr = next(e for e in up.got() if "toolResponse" in e)["toolResponse"]["functionResponses"]
    assert len(tr) == 1 and tr[0]["id"] == "fc-1" and tr[0]["name"] == "create_aiw_task"
    assert "已交给 aiw，完成后推送" in tr[0]["response"]["result"] and tr[0]["response"]["scheduling"] == "WHEN_IDLE"
    tools = sink.of("tool")
    assert [t["status"] for t in tools] == ["running", "done"] and tools[0]["name"] == "create_aiw_task"
    # gemini-3.1-flash-live is synchronous only: no scheduling field
    up2 = FakeGemini(tool_call=True)

    async def run2():
        g = oc.GeminiBackend(cfg(gemini_url=up2.url, gemini_model="gemini-3.1-flash-live-preview"), Sink(), oc.ToolBridge(FakeChat()), "hi", False)
        await g.start()
        for _ in range(3):
            pcm = tone(40)
            await g.audio(b64(pcm), pcm)
        for _ in range(150):
            if any("toolResponse" in e for e in up2.got()):
                break
            await asyncio.sleep(0.03)
        await g.close()
    asyncio.run(run2())
    tr2 = next(e for e in up2.got() if "toolResponse" in e)["toolResponse"]["functionResponses"][0]
    assert "scheduling" not in tr2["response"] and up2.conns[0]["setup"]["model"] == "models/gemini-3.1-flash-live-preview"


def test_goaway_and_drop_resume_with_handle():
    up = FakeGemini()

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", True)
        await g.start()
        assert g.handle == "h1"
        await asyncio.to_thread(up.push, {"goAway": {"timeLeft": "10s"}})
        for _ in range(100):
            if len(up.conns) >= 2 and up.conns[1]["setup"] is not None and g.ws is not None and "resumed" in g.log:
                break
            await asyncio.sleep(0.03)
        pcm = tone(40)
        await g.audio(b64(pcm), pcm)   # goes to the new connection
        for _ in range(50):
            if any("realtimeInput" in e for e in up.got(1)):
                break
            await asyncio.sleep(0.03)
        assert g.handle == "h2"
        # the server drops the connection without warning: resume again with the newest handle
        await asyncio.to_thread(up.drop)
        for _ in range(100):
            if len(up.conns) >= 3 and up.conns[2]["setup"] is not None and g.ws is not None and g.log.count("resumed") == 2:
                break
            await asyncio.sleep(0.03)
        await g.close()
        return sink, g
    sink, g = asyncio.run(run())
    assert up.conns[1]["setup"]["sessionResumption"] == {"handle": "h1"}
    assert up.conns[2]["setup"]["sessionResumption"] == {"handle": "h2"}
    assert any("realtimeInput" in e for e in up.got(1))
    assert g.log[:3] == ["open", "goAway", "reconnect:goAway"] and "reconnect:closed" in g.log
    assert not sink.of("_closed") and not sink.of("error")   # invisible to the client


def test_upstream_gone_ends_gemini_only_call_and_falls_back_in_auto():
    up = FakeGemini()

    async def run():
        sink = Sink()
        call = oc.OmniCall(cfg(gemini_url=up.url, mode="gemini"), sink, FakeChat(), video=False)
        ready = await call.start()
        assert ready["mode"] == "gemini" and ready["model"] == "gemini-3.8-live" and ready["voice"] == "Kore"
        call.backend.handle = ""             # nothing to resume with
        up.reject = "gone"                   # and a fresh session is refused too
        await asyncio.to_thread(up.drop)
        for _ in range(100):
            if call.dead:
                break
            await asyncio.sleep(0.03)
        assert call.dead == "upstream_closed" and any(e.get("code") == "upstream_closed" for e in sink.of("error"))
        await call.finish()
        # auto + omni_model: Gemini unreachable -> turn, with a note; Gemini is then skipped for a while
        gw_calls = []
        oc.TRANSPORT = httpx.MockTransport(lambda r: gw_calls.append(r) or httpx.Response(200, text="data: [DONE]\n\n"))
        sink2 = Sink()
        c2 = oc.OmniCall(cfg(mode="auto", model="qwen3.5-omni-flash", gemini_url="ws://127.0.0.1:9/x", gemini_timeout=0.5),
                         sink2, None)
        r2 = await c2.start()
        assert r2["mode"] == "turn" and "Gemini Live连不上" in r2["note"] and "实时接口连不上" in r2["note"]
        assert ("gemini", "ws://127.0.0.1:9/x", "gemini-3.8-live") in oc._RT_FAIL
        assert "Gemini Live刚才连不上" in (await oc.OmniCall(c2.cfg, Sink(), None).start())["note"]
        await c2.finish()
        # gemini mode with an unreachable endpoint: the call fails to connect (no silent fallback)
        with pytest.raises(RuntimeError):
            await oc.OmniCall(cfg(mode="gemini", gemini_url="ws://127.0.0.1:9/x", gemini_timeout=0.5), Sink(), None).start()
    asyncio.run(run())


def test_proxy_passed_through_connect_tunnel(monkeypatch):
    up = FakeGemini()
    px = ConnectProxy()

    async def one():
        g = oc.GeminiBackend(cfg(gemini_url=up.url, proxy=f"http://127.0.0.1:{px.port}"), Sink(), None, "hi", False)
        await g.start()
        await g.close()

    # websockets >= 15: its own proxy= support
    assert oc.WS_PROXY_ARG is True
    asyncio.run(one())
    assert px.targets == [f"127.0.0.1:{up.port}"] and up.conns[-1]["setup"]["model"] == "models/gemini-3.8-live"
    # websockets < 15 (no proxy=): our own CONNECT tunnel
    monkeypatch.setattr(oc, "WS_PROXY_ARG", False)
    asyncio.run(one())
    assert px.targets == [f"127.0.0.1:{up.port}"] * 2 and len(up.conns) == 2
    with pytest.raises(RuntimeError, match="websockets>=15"):
        asyncio.run(oc._http_tunnel("socks5://127.0.0.1:1", "h", 443, 1))
    # the proxy setting reaches the connection from the stored settings
    seen = {}

    async def fake_open(url, headers, timeout, proxy=""):
        seen.update(url=url, headers=headers, proxy=proxy)
        raise RuntimeError("stop")
    monkeypatch.setattr(oc, "ws_open", fake_open)
    with pytest.raises(RuntimeError):
        asyncio.run(oc.GeminiBackend(cfg(proxy="socks5://10.1.1.1:1080", gemini_url="wss://relay.example.com/x/BidiGenerateContent"),
                                     Sink(), None, "", False).start())
    assert seen == {"url": "wss://relay.example.com/x/BidiGenerateContent?key=AIzaTEST",
                    "headers": {"x-goog-api-key": "AIzaTEST"}, "proxy": "socks5://10.1.1.1:1080"}


def test_ws_gemini_call_end_to_end(app):
    up = FakeGemini(tool_call=True)
    app.state.secrets.put("omni_gemini_key", "AIzaE2E")
    set_models(app, omni_mode="auto", omni_gemini_url=up.url, omni_gemini_voice="Aoede")   # omni_model empty
    chat = app.state.chat
    started = []

    async def fake_start(prompt, title="", machine_id=None, attachments=None, origin=None, source="chat", fast=None):
        started.append(prompt)
        return chat.add("assistant", "task", "报告", {"task_id": "task-88", "title": "报告"}, task_id="task-88")
    chat.start_task = fake_start
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True}))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready" and ready["mode"] == "gemini" and ready["model"] == "gemini-3.8-live"
            assert ready["voice"] == "Aoede" and ready["audio_out"] is True
            assert "key=AIzaE2E" in up.conns[0]["path"]
            ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
            for _ in range(3):
                ws.send_text(json.dumps({"type": "audio", "data": b64(tone(40))}))
            e, seen = recv_until(ws, "tool")
            assert any(x["type"] == "audio" for x in seen)
            assert any(x["type"] == "transcript" and x["role"] == "user" and x["final"] and x["text"] == "这是什么" for x in seen)
            e, _ = recv_until(ws, "tool")
            assert e["status"] == "done" and "已交给 aiw" in e["text"]
            assert wait_for(lambda: any("toolResponse" in x for x in up.got()))
            ws.send_text(json.dumps({"type": "interrupt"}))
            e, _ = recv_until(ws, "transcript")
            ws.send_text(json.dumps({"type": "hangup"}))
            recv_until(ws, "ended")
    assert started == ["整理一份杯子市场报告"]
    assert any("video" in x.get("realtimeInput", {}) for x in up.got())
    rows = [r for r in chat._recent_rows() if (r.get("data") or {}).get("call")]
    assert len(rows) == 1 and "（Gemini）" in rows[0]["text"] and "我：这是什么" in rows[0]["text"]
    assert rows[0]["data"]["call"]["mode"] == "gemini" and rows[0]["data"]["call"]["model"] == "gemini-3.8-live"
    assert rows[0]["data"]["call"]["tasks"] == ["task-88"]


def test_probe_gemini_ok_and_rejected(app):
    up = FakeGemini()
    bad = FakeGemini(reject="API key not valid. Please pass a valid API key.")

    async def run():
        ok = await oc.probe(cfg(gemini_url=up.url))
        no = await oc.probe(cfg(gemini_url=bad.url))
        none = await oc.probe(cfg(gemini_key=""))
        return ok, no, none
    ok, no, none = asyncio.run(run())
    assert ok["mode"] == "gemini" and ok["gemini"]["ok"] and ok["gemini"]["model"] == "gemini-3.8-live"
    assert ok["gemini"]["ms"] >= 0 and not ok["realtime"] and not ok["turn"]
    assert no["mode"] is None and not no["gemini"]["ok"] and "API key not valid" in no["gemini"]["error"]
    assert any("Key" in n for n in no["notes"])
    assert none.get("error")
    # through the API: a Gemini-only setup stores the probe under "" and status returns it
    app.state.secrets.put("omni_gemini_key", "AIzaP")
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url)
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        r = c.post("/api/omni/probe", headers=H).json()
        assert r["mode"] == "gemini" and r["gemini"]["ok"]
        assert c.get("/api/omni/status", headers=H).json()["probe"]["gemini"]["ok"] is True


def test_web_settings_and_clients_wired():
    import pathlib
    import shutil
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    web, ios = root / "apps" / "web", root / "apps" / "ios"
    ui = (web / "ui.js").read_text(encoding="utf-8")
    for needle in ('name="omni_gemini_key"', 'name="omni_gemini_model"', 'name="omni_gemini_voice"', 'name="omni_proxy"',
                   'name="omni_gemini_url"', '["gemini", "Gemini Live"]', "免费层可用，国内需代理；免费层数据会被用于改进产品",
                   "aistudio.google.com/api-keys"):
        assert needle in ui, needle
    app_js = (web / "app.js").read_text(encoding="utf-8")
    assert "/api/secrets/omni_gemini_key" in app_js and "body.omni_proxy" in app_js and 'gemini: "Gemini Live"' in app_js
    assert 'gemini: "Gemini"' in (web / "call.js").read_text(encoding="utf-8")
    assert 'c.mode == "gemini" ? "Gemini"' in (ios / "Sources" / "CallView.swift").read_text(encoding="utf-8")
    if shutil.which("node"):
        for f in ("call.js", "app.js", "ui.js"):
            r = subprocess.run(["node", "--check", str(web / f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
