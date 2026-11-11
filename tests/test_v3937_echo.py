"""v3.9.37: a field call (iOS app on speaker, Gemini Live) where every spoken answer was cut off mid-sentence: the
model's own playback came back through the mic, Gemini's VAD took it for the user (`user: 请问还有` right after
`model: 请问还有什么…`) and sent serverContent.interrupted. Now:
  * omni_gemini_barge_in = off -> setup.realtimeInputConfig.activityHandling = "NO_INTERRUPTION" (default: left out =
    START_OF_ACTIVITY_INTERRUPTS); validated in settings; shown in /api/omni/status and the ready message
  * every interrupted is counted (counters.interrupts, stats line interrupts=N) and logged in the timeline
  * the clients' {"type":"client_audio"} reports (vp on/off, echo gate numbers) are kept and logged (vp off = WARNING)
  * omni_echo_gate (default on) -> ready.echo_gate; both clients gate the mic while the answer plays
  * the iOS client falls back to no voice processing only on zero mic callbacks, and keeps unplayed buffers on restart
  * call.js callEchoGate: echo zeroed, a sustained loud voice passes (node)"""
import json
import pathlib
import shutil
import subprocess
import time

import pytest

from aiw_api import omni_call as oc
from test_v3932_gemini_live import b64, set_models  # noqa: F401
from test_v3935_gemini_robust import LOUD, Up, _recv_until

ROOT = pathlib.Path(__file__).resolve().parents[1]
H = {"Authorization": "Bearer tok"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("AIW_OMNI_NO_MIC_S", "0")
    oc._RT_FAIL.clear()
    oc._ACTIVE.clear()
    yield


@pytest.fixture
def app(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    s.scheduler_enabled = False
    a = build_app(s)
    a.state.secrets.put("omni_gemini_key", "AIzaT")
    return a


def _drain_until_ended(ws, limit=80):
    seen = []
    for _ in range(limit):
        e = json.loads(ws.receive_text())
        seen.append(e)
        if e["type"] == "ended":
            return seen
    raise AssertionError(seen)


def _calls(c):
    return c.get("/api/omni/calls", headers=H).json()["calls"]


def _wait(cond, s=3.0):
    t = time.time() + s
    while time.time() < t:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------------------------------------ setup / settings
def test_realtime_input_config_helper():
    assert oc.realtime_input_config({}) == {"automaticActivityDetection": oc.vad_config({})}
    assert "activityHandling" not in oc.realtime_input_config({"gemini_barge_in": "auto"})
    assert "activityHandling" not in oc.realtime_input_config({"gemini_barge_in": "on"})
    assert oc.realtime_input_config({"gemini_barge_in": "off"})["activityHandling"] == "NO_INTERRUPTION"
    assert oc.echo_gate_on({}) and oc.echo_gate_on({"echo_gate": "on"}) and not oc.echo_gate_on({"echo_gate": "off"})


@pytest.mark.parametrize("barge", ["", "off"])
def test_setup_activity_handling_and_ready_flags(app, barge):
    up = Up(answer=())
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        assert c.patch("/api/settings/models", headers=H, json={"omni_gemini_barge_in": "never"}).status_code == 400
        assert c.patch("/api/settings/models", headers=H, json={"omni_echo_gate": "maybe"}).status_code == 400
        r = c.patch("/api/settings/models", headers=H, json={"omni_mode": "gemini", "omni_gemini_url": up.url,
                                                              "omni_gemini_barge_in": barge.upper(),
                                                              "omni_echo_gate": "off" if barge else "on"})
        assert r.status_code == 200, r.text
        st = c.get("/api/omni/status", headers=H).json()
        assert st["gemini"]["barge_in"] is (barge != "off")
        assert st["echo_gate"] is (barge != "off")
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "client": "ios"}))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready"
            assert ready["echo_gate"] is (barge != "off") and ready["barge_in"] is (barge != "off")
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        ric = up.conns[0]["setup"]["realtimeInputConfig"]
        assert ric["automaticActivityDetection"]["startOfSpeechSensitivity"] == "START_SENSITIVITY_LOW"
        if barge == "off":
            assert ric["activityHandling"] == "NO_INTERRUPTION"
        else:
            assert "activityHandling" not in ric
        line = [e[1] for e in _calls(c)[0]["events"] if e[1].startswith("backend gemini")][0]
        assert f"barge_in={'off' if barge else 'on'}" in line and f"echo_gate={'off' if barge else 'on'}" in line


# ------------------------------------------------------------------------------------------------ interrupts / reports
def test_interrupts_counted_and_logged_and_client_audio_kept(app):
    up = Up(answer=())
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "client": "ios",
                                     "ua": "aiw-ios/3.9.28 iOS 17.0 iPhone"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "client_audio", "event": "start", "vp": False, "gate": True,
                                     "why": "no mic callbacks", "rate": 48000, "chans": 1, "route": "Speaker"}))
            ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
            assert _wait(lambda: up.live)
            for i in range(2):
                up.push({"serverContent": {"modelTurn": {"parts": [{"inlineData": {
                    "mimeType": "audio/pcm;rate=24000", "data": b64(LOUD)}}]},
                    "outputTranscription": {"text": "请问还有什么"}}})
                assert _recv_until(ws, "audio")
                up.push({"serverContent": {"inputTranscription": {"text": "请问还有"}}})
                up.push({"serverContent": {"interrupted": True}})
                assert _recv_until(ws, "speech_started")
                up.push({"serverContent": {"turnComplete": True}})
            ws.send_text(json.dumps({"type": "client_audio", "event": "turn", "vp": True, "gate": True,
                                     "echo": 0.031, "thr": 0.093, "gated_s": 3.2, "barges": 0, "junk": "x"}))
            ws.send_text(json.dumps({"type": "client_audio", "echo": "not a number"}))   # ignored, no crash
            time.sleep(0.3)
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        call = _calls(c)[0]
        assert call["counters"]["interrupts"] == 2
        evs = call["events"]
        ints = [e for e in evs if "model interrupted by user speech" in e[1]]
        assert len(ints) == 2 and "#1" in ints[0][1] and "#2" in ints[1][1]
        assert "请问还有" in ints[0][1]   # what Gemini heard (the echo) is in the line
        assert any("interrupts=2" in e[1] for e in evs if e[1].startswith("ended"))
        vp = [e for e in evs if e[1].startswith("client audio start")]
        assert len(vp) == 1 and vp[0][2] == "warn" and "voice_processing(AEC)=OFF" in vp[0][1]
        assert "why=no mic callbacks" in vp[0][1] and "route=Speaker" in vp[0][1]
        turn = [e for e in evs if e[1].startswith("client audio turn")][0]
        assert turn[2] == "info" and "echo=0.031" in turn[1] and "gated_s=3.2" in turn[1] and "junk" not in turn[1]
        ca = call["client_audio"]
        assert ca["vp"] is True and ca["gate"] is True and ca["echo"] == 0.031 and ca["rate"] == 48000.0
        assert call["counters"]["client_reports"] == 2


def test_stats_line_has_interrupts():
    d = oc.CallDiag(keep=False)
    d.bump("interrupts", 3)
    assert "interrupts=3" in d.stats_line()


def test_client_audio_report_unit():
    d = oc.CallDiag(keep=False)
    oc.client_audio_report(d, {"type": "client_audio"})
    assert d.n["client_reports"] == 0 and not d.events
    oc.client_audio_report(d, {"type": "client_audio", "event": "restart", "vp": True, "why": "route\n  x" * 30})
    assert d.n["client_reports"] == 1 and d.events[-1][2] == "info"
    assert len(d.client_audio["why"]) <= 120 and "\n" not in d.client_audio["why"]


# ------------------------------------------------------------------------------------------------ clients
def test_web_client_echo_gate_wiring():
    js = (ROOT / "apps" / "web" / "call.js").read_text(encoding="utf-8")
    for needle in ("function callEchoGate(", "callDownsampler(ctx.sampleRate, callGateSend)", "function callPlaying(",
                   "CALL_ECHO_HOLD_MS = 300", "m.echo_gate === false", 'aiw_call_echo_gate', 'type: "client_audio"',
                   "echoCancellation: true", 'callGateReport("turn")', "sustainMs: 200", "floor: 0.06", "factor: 3"):
        assert needle in js, needle
    ui = (ROOT / "apps" / "web" / "ui.js").read_text(encoding="utf-8")
    assert 'name="omni_gemini_barge_in"' in ui and 'name="omni_echo_gate"' in ui
    # next to the VAD settings
    assert abs(ui.index('name="omni_gemini_barge_in"') - ui.index('name="omni_gemini_silence_ms"')) < 1500
    app_js = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert "body.omni_gemini_barge_in" in app_js and "body.omni_echo_gate" in app_js and "n.interrupts" in app_js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_web_echo_gate_behaviour_node():
    f = ROOT / "apps" / "web" / "call.js"
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    script = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
eval(src.match(/function callEchoGate[\s\S]*?\n}\n/)[0]);
const tone = (amp) => { const b = new Float32Array(640); for (let i = 0; i < 640; i++) b[i] = amp * Math.SQRT2 * Math.sin(2 * Math.PI * 300 * i / 16000); return b; };
const rms = (b) => Math.sqrt(b.reduce((s, v) => s + v * v, 0) / b.length);
const run = (gate, seq) => { let sent = 0, loud = 0, chunks = 0;
  for (const [amp, playing] of seq) for (const c of gate(tone(amp), playing)) { chunks++; sent += c.length; if (rms(c) > 0.001) loud++; }
  return { sent, loud, chunks }; };
const rep = (n, amp, playing) => Array.from({ length: n }, () => [amp, playing]);
const out = {};
// 1) echo of 0.08 during 3 s of playback: all zeros, same length (75 chunks)
let g = callEchoGate(); out.echo = run(g, rep(75, 0.08, true)); out.echoLevel = g.stats.echo; out.thr = g.stats.thr;
// 2) then a short loud blip (120 ms) - still not a barge-in
out.blip = run(g, rep(3, 0.5, true).concat(rep(5, 0.08, true)));
// 3) then a sustained loud voice (400 ms at 0.5 > 3 x 0.08): barge-in, the held chunks go out too
out.barge = run(g, rep(10, 0.5, true)); out.barges = g.stats.barges;
// 4) not playing: everything passes as it is
g = callEchoGate(); out.idle = run(g, rep(10, 0.02, false));
// 5) gate off: echo passes
g = callEchoGate({ on: false }); out.off = run(g, rep(10, 0.08, true));
// 6) loud echo with no AEC (0.15) from the first chunk: the warm-up learns it, no false barge-in
g = callEchoGate(); out.noaec = run(g, rep(50, 0.15, true)); out.noaecBarges = g.stats.barges;
// 7) a held start of speech when the answer ends is sent as it is
g = callEchoGate(); run(g, rep(20, 0.01, true)); out.tail = run(g, rep(2, 0.5, true).concat([[0.5, false]]));
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", script, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["echo"] == {"sent": 75 * 640, "loud": 0, "chunks": 75}, o
    assert 0.06 < o["echoLevel"] < 0.1 and 0.18 < o["thr"] < 0.3, o
    assert o["blip"] == {"sent": 8 * 640, "loud": 0, "chunks": 8}, o
    assert o["barge"]["loud"] == 10 and o["barge"]["sent"] == 10 * 640 and o["barges"] == 1, o
    assert o["idle"]["loud"] == 10 and o["off"]["loud"] == 10, o
    assert o["noaec"]["loud"] == 0 and o["noaecBarges"] == 0, o
    assert o["tail"]["loud"] == 3 and o["tail"]["chunks"] == 3, o


def test_ios_client_vp_fallback_gate_and_playback():
    swift = (ROOT / "apps" / "ios" / "Sources" / "CallView.swift").read_text(encoding="utf-8")
    assert swift.count("{") == swift.count("}")
    # fallback to no voice processing only when the tap gives no callbacks at all
    check = swift[swift.index("private func checkMic()"):]
    check = check[:check.index("\n    }\n")]
    assert "noCallbacks && vpOn && !vpFallbackTried" in check
    assert "now - max(buf, started) >= 3" in check
    no_mic = swift[swift.index('o.s("code") == "no_mic"'):]
    no_mic = no_mic[:no_mic.index('case "ended"')]
    assert "noCallbacks && vpOn" in no_mic
    # mode reported to the server and on screen
    for needle in ('"type": "client_audio"', '"vp": vpOn', "reportAudio(\"start\")", "reportAudio(\"restart\")",
                   "reportAudio(\"turn\")", "回声消除没开", "vpErr = error.localizedDescription",
                   "isVoiceProcessingBypassed = false"):
        assert needle in swift, needle
    # playback on the same voice-processing engine, .voiceChat
    assert "engine.connect(player, to: engine.mainMixerNode, format: outFormat)" in swift
    assert "mode: .voiceChat" in swift and "setVoiceProcessingEnabled(voiceProcessing)" in swift
    # echo gate
    for needle in ("struct EchoGate", "var floor: Float = 0.06, factor: Float = 3", "sustainMs = 200",
                   "gate.feed(samples, playing: playing)", "playEndAt + echoHold", "echoHold: TimeInterval = 0.3",
                   '"echo_gate"', "aiw.call.echoGate"):
        assert needle in swift, needle
    # tail kept: buffers complete when played, re-scheduled after an engine restart
    assert "completionCallbackType: .dataPlayedBack" in swift
    assert "let carry = takeQueue()" in swift and "for b in carry { schedule(b) }" in swift
    assert "AVAudioConverter(" not in swift
