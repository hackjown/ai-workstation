"""v3.9.35: the Gemini Live call backend, hardened after a field report (a call that answered for ~40 s and then went
silent; later calls 「已接通」 but never answering, no error, nothing in docker logs). Against a fake BidiGenerateContent
server (a real websocket server in its own thread):
  * a fatal close (quota / 1007 / 1008) is surfaced to the client with its code + reason and ends the call
    (v3.9.34: resumed in a loop, reason only at log.info, client saw nothing)
  * a resumed connection that dies while the reconnect is still flushing queued audio is reconnected again
    (v3.9.34: its reconnect request was dropped -> the call stayed 'connected' to a closed socket, forever silent)
  * connections that keep dying at once end the call after 3 quick drops (v3.9.34: up to 30 sessions in seconds)
  * an exception inside the reconnect fails the call instead of leaving ws None forever
  * a stalled upstream send aborts the connection and reconnects
  * the watchdog: speech for > N s and nothing from Gemini -> 「模型没有响应，正在重连…」, resume, then a fresh session,
    then the call ends with a clear error; quiet audio never trips it
  * the upstream connection is closed on hang-up and when the client just drops
  * GET /api/omni/calls (auth) with the call's timeline and counters; aiw.omni logging set up
  * VAD config in setup (default START_SENSITIVITY_LOW) and the settings that change it
  * the client's interrupt drops the rest of the answer's audio and sends nothing upstream
  * audioStreamEnd after the microphone pauses"""
import asyncio
import json
import logging
import threading
import time

import pytest

from aiw_api import omni_call as oc
from test_v3932_gemini_live import AUDIO_OUT, JPEG, FakeChat, Sink, b64, cfg, set_models, tone, wait_for  # noqa: F401

QUIET = b"\x00\x00" * 640           # 40 ms of silence
LOUD = tone(40, 9000)                # 40 ms of a loud tone (rms ~6400)
QUOTA = "You exceeded your current quota, please check your plan and billing details."


# ------------------------------------------------------------------------------------------------ fake Gemini Live
class Up:
    """A BidiGenerateContent-shaped server whose behaviour is set per connection number (1-based):
      answer      set of connections that answer after 3 audio chunks (transcripts, audio, turnComplete)
      close_audio {n or "rest": (code, reason)}: close that connection when its first audio arrives
      reject      {n or "rest": (code, reason)}: close at setup
      setup_delay seconds before setupComplete on connections >= 2 (a proxy's latency)
    Each connection records what it got and when it was closed."""

    def __init__(self, answer=(1,), close_audio=None, reject=None, setup_delay=0.0):
        self.answer, self.close_audio, self.reject = set(answer), dict(close_audio or {}), dict(reject or {})
        self.setup_delay = setup_delay
        self.conns: list = []
        self.live: list = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        assert self.ready.wait(5)

    def _rule(self, d, n):
        return d.get(n, d.get("rest") if n >= 2 else None)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        from websockets.asyncio.server import serve

        async def handler(ws):
            n = len(self.conns) + 1
            c = {"n": n, "setup": None, "got": [], "closed": None}
            self.conns.append(c)
            self.live.append(ws)
            answered = False
            try:
                async for raw in ws:
                    ev = json.loads(raw)
                    c["got"].append(ev)
                    if "setup" in ev:
                        c["setup"] = ev["setup"]
                        rj = self._rule(self.reject, n)
                        if rj:
                            await ws.close(*rj)
                            return
                        if n >= 2 and self.setup_delay:
                            await asyncio.sleep(self.setup_delay)
                        await ws.send(json.dumps({"sessionResumptionUpdate": {"newHandle": f"h{n}", "resumable": True}}))
                        await ws.send(json.dumps({"setupComplete": {}}))
                    elif "audio" in ev.get("realtimeInput", {}):
                        ca = self._rule(self.close_audio, n)
                        if ca:
                            await ws.close(*ca)
                            return
                        if n in self.answer and not answered and sum(
                                1 for e in c["got"] if "audio" in e.get("realtimeInput", {})) >= 3:
                            answered = True
                            for out in ({"serverContent": {"inputTranscription": {"text": "你好"}}},
                                        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
                                            "mimeType": "audio/pcm;rate=24000", "data": b64(AUDIO_OUT)}}]},
                                            "outputTranscription": {"text": "你好呀"}}},
                                        {"serverContent": {"turnComplete": True}}):
                                await ws.send(json.dumps(out, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass
            finally:
                c["closed"] = time.time()

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

    def push(self, msg, i=-1):
        asyncio.run_coroutine_threadsafe(self.live[i].send(json.dumps(msg, ensure_ascii=False)), self.loop).result(3)

    def drop(self, code=1011, reason="internal error", i=-1):
        asyncio.run_coroutine_threadsafe(self.live[i].close(code, reason), self.loop).result(3)

    def audio_count(self, i):
        return sum(1 for e in self.conns[i]["got"] if "audio" in e.get("realtimeInput", {}))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    oc._RT_FAIL.clear()
    oc._ACTIVE.clear()
    yield


async def stream(g, pcm=LOUD, n=50, gap=0.02):
    for _ in range(n):
        await g.audio(b64(pcm), pcm)
        await asyncio.sleep(gap)


async def until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def slow_sends(monkeypatch, delay=0.01):
    """Upstream sends that wait (a slow proxy's backpressure): gives the reader time to see a close mid-flush."""
    orig = oc.ws_open

    async def slow_open(*a, **k):
        ws = await orig(*a, **k)
        real = ws.send

        async def send(m):
            await asyncio.sleep(delay)
            return await real(m)
        ws.send = send
        return ws
    monkeypatch.setattr(oc, "ws_open", slow_open)


# ------------------------------------------------------------------------------------------------ bug: fatal close
def test_quota_close_is_surfaced_and_ends_the_call_without_a_resume_storm():
    up = Up(close_audio={"rest": (1011, QUOTA)}, setup_delay=0.2)

    async def run():
        sink = Sink()
        call = oc.OmniCall(cfg(gemini_url=up.url, mode="gemini"), sink, FakeChat(), video=False)
        await call.start()
        await asyncio.to_thread(up.drop)          # a transient drop: resumed
        await stream(call.backend, n=40)          # the resumed connection is closed for quota on the first audio
        assert await until(lambda: call.dead)
        await call.finish("upstream_closed")
        return sink, call
    sink, call = asyncio.run(run())
    errs = sink.of("error")
    assert call.dead == "upstream_closed"
    assert any(e["code"] == "upstream_closed" and "1011" in e["message"] and "quota" in e["message"] for e in errs), errs
    assert len(up.conns) == 2                       # v3.9.34 opened up to 31 sessions here
    assert any("upstream closed code=1011" in e[1] and "quota" in e[1] for e in call.diag.events)


@pytest.mark.parametrize("code,reason,fatal", [
    (1007, "Request contains an invalid argument.", True), (1008, "Operation is not implemented", True),
    (1011, QUOTA, True), (1011, "Internal error encountered.", False), (1000, "", False), (1006, "", False),
    (1011, "RESOURCE_EXHAUSTED", True)])
def test_fatal_close_classification(code, reason, fatal):
    assert oc.fatal_close(code, reason) is fatal


def test_invalid_argument_mid_call_ends_with_reason():
    up = Up()

    async def run():
        sink = Sink()
        call = oc.OmniCall(cfg(gemini_url=up.url, mode="gemini"), sink, FakeChat(), video=False)
        await call.start()
        await asyncio.to_thread(up.drop, 1007, "Request contains an invalid argument.")
        assert await until(lambda: call.dead)
        await call.finish()
        return sink
    sink = asyncio.run(run())
    assert any("1007 Request contains an invalid argument." in e["message"] for e in sink.of("error"))
    assert len(up.conns) == 1


# ------------------------------------------------------------------------------------------------ bug: dropped request
def test_connection_dying_during_flush_is_reconnected_again(monkeypatch):
    """v3.9.34: the resumed connection closes while _do_reconnect is still flushing pending audio; its reader's
    _reconnect() was ignored (a reconnect was 'running'), so self.ws stayed a CLOSED socket: no error, no reply."""
    slow_sends(monkeypatch)
    up = Up(answer=(3,), close_audio={2: (1011, "Internal error encountered.")}, setup_delay=0.3)

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        await g.start()
        await asyncio.to_thread(up.drop)
        await stream(g, n=60)
        assert await until(lambda: len(up.conns) >= 3 and up.audio_count(2) >= 3)
        assert await until(lambda: bool(sink.of("audio")))
        state = (g.ws is not None, g.closed, g.reader.done())
        await g.close()
        return sink, g, state
    sink, g, state = asyncio.run(run())
    assert state == (True, False, False)              # a live connection, not a dead socket
    assert up.conns[2]["setup"]["sessionResumption"] == {"handle": "h2"}
    assert any("died during the switch" in e for e in g.diag.events) or g.log.count("resumed") >= 2
    assert not sink.of("_closed")


def test_connections_that_keep_dying_end_the_call_after_three():
    up = Up(close_audio={"rest": (1011, "Internal error encountered.")})

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        await g.start()
        await asyncio.to_thread(up.drop)
        for _ in range(200):
            if g.closed:
                break
            await g.audio(b64(LOUD), LOUD)
            await asyncio.sleep(0.02)
        await g.close()
        return sink
    sink = asyncio.run(run())
    closed = sink.of("_closed")
    assert closed and "连续 3 次" in closed[0]["message"] and "Internal error" in closed[0]["message"]
    assert len(up.conns) <= 5                       # 1 + 3 quick drops (+ at most one in flight)


def test_exception_inside_reconnect_fails_the_call(monkeypatch):
    up = Up()

    async def boom(self):
        raise KeyError("unexpected")
    monkeypatch.setattr(oc.GeminiBackend, "_flush", boom)

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        await g.start()
        await asyncio.to_thread(up.drop)
        assert await until(lambda: g.closed)
        await g.close()
        return sink, g
    sink, g = asyncio.run(run())
    assert sink.of("_closed") and "KeyError" in sink.of("_closed")[0]["message"]
    assert g.ws is None or g.closed


def test_stalled_send_aborts_and_reconnects(monkeypatch):
    monkeypatch.setenv("AIW_OMNI_SEND_TIMEOUT", "0.2")
    up = Up()

    async def run():
        g = oc.GeminiBackend(cfg(gemini_url=up.url), Sink(), None, "hi", False)
        await g.start()
        first = g.ws

        async def hang(_m):
            await asyncio.sleep(30)
        first.send = hang                            # a proxy that stopped reading
        await g.audio(b64(LOUD), LOUD)
        assert await until(lambda: len(up.conns) >= 2 and g.ws is not None and g.ws is not first)
        await stream(g, n=3)
        assert await until(lambda: up.audio_count(1) >= 1)
        await g.close()
        return g
    g = asyncio.run(run())
    assert any("send stalled" in e for e in g.diag.events)


# ------------------------------------------------------------------------------------------------ watchdog
def test_watchdog_resumes_then_fresh_then_ends(monkeypatch):
    monkeypatch.setenv("AIW_OMNI_WATCHDOG_S", "0.6")
    monkeypatch.setenv("AIW_OMNI_WATCHDOG_VOICED_S", "0.2")
    monkeypatch.setenv("AIW_OMNI_TICK_S", "0.05")
    up = Up(answer=())                               # never says anything

    async def run():
        sink = Sink()
        call = oc.OmniCall(cfg(gemini_url=up.url, mode="gemini"), sink, FakeChat(), video=False)
        await call.start()
        call.start_ticker()
        for _ in range(300):
            if call.dead:
                break
            await call.on_audio(b64(LOUD))
            await asyncio.sleep(0.02)
        await call.finish("no_response")
        return sink, call
    sink, call = asyncio.run(run())
    notes = [e for e in sink.of("error") if e.get("code") == "no_response"]
    assert len(notes) == 2 and notes[0]["message"] == "模型没有响应，正在重连…"
    final = [e for e in sink.of("error") if e.get("code") == "upstream_closed"]
    assert final and "模型一直没有响应" in final[0]["message"] and final[0].get("reason") == "no_response"
    assert call.dead == "no_response"
    assert len(up.conns) == 3
    assert up.conns[1]["setup"]["sessionResumption"] == {"handle": "h1"}   # 1st: resume
    assert up.conns[2]["setup"]["sessionResumption"] == {}                 # 2nd: a fresh session
    assert all(c["closed"] for c in up.conns)                              # nothing left open upstream


def test_watchdog_ignores_quiet_audio_and_resets_on_answers(monkeypatch):
    monkeypatch.setenv("AIW_OMNI_WATCHDOG_S", "0.4")
    monkeypatch.setenv("AIW_OMNI_WATCHDOG_VOICED_S", "0.2")
    up = Up(answer=(1,))

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        await g.start()
        for _ in range(40):                          # silence for 0.8 s: the user is just listening
            await g.audio(b64(QUIET), QUIET)
            await g.tick()
            await asyncio.sleep(0.02)
        assert not sink.of("error") and len(up.conns) == 1
        await stream(g, n=5)                         # speech that gets an answer
        assert await until(lambda: bool(sink.of("audio")))
        assert g.wd_fires == 0 and time.time() - g.last_up < 1 and g.voiced_s < 0.5   # the clock restarted
        await g.close()
    asyncio.run(run())


# ------------------------------------------------------------------------------------------------ hang-up / drop
@pytest.fixture
def app(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    s.scheduler_enabled = False
    return build_app(s)


def _recv_until(ws, t, limit=80):
    for _ in range(limit):
        e = json.loads(ws.receive_text())
        if e["type"] == t:
            return e
    raise AssertionError(t)


def test_upstream_closed_on_hangup_and_on_client_drop_and_calls_api(app):
    up = Up()
    app.state.secrets.put("omni_gemini_key", "AIzaT")
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url)
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
            for _ in range(3):
                ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
            _recv_until(ws, "audio")
            ws.send_text(json.dumps({"type": "hangup"}))
            assert _recv_until(ws, "ended")["reason"] == "hangup"
        assert wait_for(lambda: up.conns[0]["closed"] is not None)
        # the client just goes away (app killed / network gone) without a hangup
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
        assert wait_for(lambda: len(up.conns) == 2 and up.conns[1]["closed"] is not None)
        assert wait_for(lambda: not oc._ACTIVE and oc.CALLS[-1].ended_at is not None), [(c.id, c.events, c.ended_at) for c in oc.CALLS]
        # 通话诊断
        assert c.get("/api/omni/calls").status_code == 401
        r = c.get("/api/omni/calls", headers=H).json()
        calls = r["calls"]
        assert len(calls) >= 2 and "grep" in r["log_hint"]
        drop, first = calls[0], calls[1]
        assert drop["reason"] == "disconnect" and drop["active"] is False
        assert first["reason"] == "hangup" and first["backend"] == "gemini" and first["model"] == "gemini-3.8-live"
        texts = [e[1] for e in first["events"]]
        for needle in ("setup sent", "setupComplete", "ready: gemini", "user: 你好", "model: 你好呀", "upstream closed by us",
                       "ended (hangup)"):
            assert any(needle in t for t in texts), (needle, texts)
        n = first["counters"]
        assert n["audio_in"] == 3 and n["audio_up"] == 3 and n["frames_in"] == 1 and n["frames_up"] == 1
        assert n["audio_out"] >= 1 and n["up_msgs"] >= 3 and first["last_bot_transcript_s"] is not None


def test_dead_call_hangs_up_without_waiting_for_the_client(app):
    """v3.9.34 noticed a dead upstream only at the next client message; a muted phone kept it 'connected' until idle."""
    up = Up(reject={"rest": (1008, "gone")})
    app.state.secrets.put("omni_gemini_key", "AIzaT")
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            up.drop(1011, "Internal error encountered.")
            t0 = time.time()
            err = _recv_until(ws, "error")
            end = _recv_until(ws, "ended")
            assert time.time() - t0 < 5
    assert err["code"] == "upstream_closed" and "1008 gone" in err["message"]
    assert end["reason"] == "upstream_closed"


def test_logging_setup_only_touches_aiw_omni(monkeypatch):
    lg = logging.getLogger("aiw.omni")
    saved = (lg.handlers[:], lg.level, lg.propagate, getattr(lg, "_aiw_handler", None))
    try:
        lg.handlers.clear()
        if hasattr(lg, "_aiw_handler"):
            del lg._aiw_handler
        oc.setup_logging()
        oc.setup_logging()                         # idempotent
        assert len(lg.handlers) == 1 and lg.level == logging.INFO and lg.propagate is False
        assert logging.getLogger("aiw.api").level == logging.NOTSET and not logging.getLogger("aiw").handlers
        d = oc.CallDiag(keep=False)
        seen = []
        h = logging.Handler()
        h.emit = lambda r: seen.append(r.getMessage())
        lg.addHandler(h)
        d.ev("setupComplete")
        d.ev("upstream closed code=1011", warn=True)
        assert seen == [f"omni[{d.id}] setupComplete", f"omni[{d.id}] upstream closed code=1011"]
    finally:
        lg.handlers[:] = saved[0]
        lg.setLevel(saved[1])
        lg.propagate = saved[2]
        if saved[3] is None and hasattr(lg, "_aiw_handler"):
            del lg._aiw_handler


def test_calls_ring_keeps_last_20():
    oc.CALLS.clear()
    ids = [oc.CallDiag().id for _ in range(25)]
    assert [c.id for c in oc.CALLS] == ids[-20:]


# ------------------------------------------------------------------------------------------------ VAD + settings
def test_vad_config_defaults_and_settings(app):
    assert oc.vad_config({}) == {"disabled": False, "startOfSpeechSensitivity": "START_SENSITIVITY_LOW"}
    assert oc.vad_config({"gemini_vad_start": "api"}) == {"disabled": False}
    assert oc.vad_config({"gemini_vad_start": "high", "gemini_vad_end": "low", "gemini_silence_ms": "600"}) == {
        "disabled": False, "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
        "endOfSpeechSensitivity": "END_SENSITIVITY_LOW", "silenceDurationMs": 600}
    up = Up()
    app.state.secrets.put("omni_gemini_key", "AIzaT")
    from fastapi.testclient import TestClient
    H = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        assert c.patch("/api/settings/models", headers=H, json={"omni_gemini_vad_start": "loud"}).status_code == 400
        assert c.patch("/api/settings/models", headers=H, json={"omni_gemini_silence_ms": "20"}).status_code == 400
        r = c.patch("/api/settings/models", headers=H, json={"omni_mode": "gemini", "omni_gemini_url": up.url,
                                                              "omni_gemini_vad_start": "HIGH", "omni_gemini_vad_end": "low",
                                                              "omni_gemini_silence_ms": "650"})
        assert r.status_code == 200 and r.json()["omni_gemini_vad_start"] == "high"
        st = c.get("/api/omni/status", headers=H).json()
        assert st["gemini"]["vad"]["startOfSpeechSensitivity"] == "START_SENSITIVITY_HIGH"
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "hangup"}))
            _recv_until(ws, "ended")
        aad = up.conns[0]["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
        assert aad == {"disabled": False, "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                       "endOfSpeechSensitivity": "END_SENSITIVITY_LOW", "silenceDurationMs": 650}
        # cleared = defaults again (start low, the rest the API's)
        c.patch("/api/settings/models", headers=H, json={"omni_gemini_vad_start": "", "omni_gemini_vad_end": "",
                                                          "omni_gemini_silence_ms": ""})
        assert c.get("/api/omni/status", headers=H).json()["gemini"]["vad"] == {
            "disabled": False, "startOfSpeechSensitivity": "START_SENSITIVITY_LOW"}


# ------------------------------------------------------------------------------------------------ interrupt / stream end
def test_client_interrupt_drops_rest_of_answer_and_sends_nothing_upstream():
    up = Up(answer=())

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        await g.start()
        part = {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm", "data": b64(AUDIO_OUT)}}]}}}
        await asyncio.to_thread(up.push, part)
        assert await until(lambda: len(sink.of("audio")) == 1)
        n_up = len(up.conns[0]["got"])
        await g.interrupt()
        await asyncio.to_thread(up.push, part)
        await asyncio.to_thread(up.push, {"serverContent": {"turnComplete": True}})
        await asyncio.sleep(0.2)
        assert len(sink.of("audio")) == 1            # the rest of that answer is not played
        await asyncio.to_thread(up.push, part)       # the next answer is
        assert await until(lambda: len(sink.of("audio")) == 2)
        assert len(up.conns[0]["got"]) == n_up       # nothing sent upstream for the interrupt
        await g.close()
    asyncio.run(run())


def test_audio_stream_end_after_mic_pause():
    up = Up(answer=())

    async def run():
        g = oc.GeminiBackend(cfg(gemini_url=up.url), Sink(), None, "hi", False)
        await g.start()
        await stream(g, n=3)
        g.last_audio_in = time.time() - 2           # the phone muted 2 s ago
        await g.tick()
        await g.tick()                              # once, not every tick
        assert await until(lambda: any(e.get("realtimeInput", {}).get("audioStreamEnd") for e in up.conns[0]["got"]))
        await g.close()
    asyncio.run(run())
    assert sum(1 for e in up.conns[0]["got"] if e.get("realtimeInput", {}).get("audioStreamEnd")) == 1


def test_tool_timeout_still_answers_the_model(monkeypatch):
    monkeypatch.setenv("AIW_OMNI_TOOL_TIMEOUT", "0.2")
    up = Up(answer=())

    class SlowBridge:
        async def run(self, name, args):
            await asyncio.sleep(10)

    async def run():
        sink = Sink()
        g = oc.GeminiBackend(cfg(gemini_url=up.url), sink, None, "hi", False)
        g.bridge = SlowBridge()
        await g.start()
        await asyncio.to_thread(up.push, {"toolCall": {"functionCalls": [{"id": "f1", "name": "task_status", "args": {}}]}})
        assert await until(lambda: any("toolResponse" in e for e in up.conns[0]["got"]), 3)
        await g.close()
    asyncio.run(run())
    fr = [e for e in up.conns[0]["got"] if "toolResponse" in e][0]["toolResponse"]["functionResponses"][0]
    assert fr["id"] == "f1" and "超时" in fr["response"]["result"]


# ------------------------------------------------------------------------------------------------ clients
def test_web_and_ios_wired():
    import pathlib
    import shutil
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    web = root / "apps" / "web"
    ui = (web / "ui.js").read_text(encoding="utf-8")
    for needle in ('name="omni_gemini_vad_start"', 'name="omni_gemini_vad_end"', 'name="omni_gemini_silence_ms"',
                   "通话诊断", 'onclick="omniCallsDiag()"'):
        assert needle in ui, needle
    app_js = (web / "app.js").read_text(encoding="utf-8")
    assert "async function omniCallsDiag()" in app_js and '"/api/omni/calls"' in app_js
    assert "body.omni_gemini_vad_start" in app_js
    swift = (root / "apps" / "ios" / "Sources" / "CallView.swift").read_text(encoding="utf-8")
    assert "lastErr = o.s(\"message\")" in swift and 'r != "hangup"' in swift
    if shutil.which("node"):
        for f in ("app.js", "ui.js", "call.js"):
            r = subprocess.run(["node", "--check", str(web / f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
