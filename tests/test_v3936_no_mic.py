"""v3.9.36: a field call (iPhone, Gemini route) where the server got camera frames but ZERO audio messages for 21 s
(`in=0 frames=15 ... user_tr=never`) and nobody was told. Now:
  * frames but no audio for AIW_OMNI_NO_MIC_S (default 5 s) after ready -> a WARNING "no audio from client" in the
    call's timeline and one {"type":"error","code":"no_mic"} to the client (once, not every second)
  * a client that sends audio, or says it is muted ({"type":"mute","on":true}), gets no such warning
  * the start message's "client" ("ios" | "web") and "ua" (else the User-Agent header) are in the "call from" line
    and in GET /api/omni/calls
  * the web client unlocks its AudioContext synchronously in the tap and resamples 48 / 44.1 kHz exactly to 16 kHz;
    the iOS client converts the mic itself, watches for silence and restarts on interruptions / route changes."""
import json
import pathlib
import shutil
import subprocess
import time

import pytest

from aiw_api import omni_call as oc
from test_v3932_gemini_live import JPEG, b64, set_models  # noqa: F401
from test_v3935_gemini_robust import LOUD, Up, _recv_until

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("AIW_OMNI_NO_MIC_S", "0.6")
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


def _gemini(app):
    up = Up(answer=())
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url)
    return up


def _drain_until_ended(ws, limit=80):
    seen = []
    for _ in range(limit):
        e = json.loads(ws.receive_text())
        seen.append(e)
        if e["type"] == "ended":
            return seen
    raise AssertionError(seen)


def _calls(c):
    return c.get("/api/omni/calls", headers={"Authorization": "Bearer tok"}).json()["calls"]


def test_frames_without_audio_warn_once_and_log_client(app):
    _gemini(app)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True, "client": "web",
                                     "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) Safari"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            t0 = time.time()
            ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
            err = _recv_until(ws, "error")
            took = time.time() - t0
            # more frames, still no audio: no second warning
            for _ in range(3):
                ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
                time.sleep(0.3)
            ws.send_text(json.dumps({"type": "hangup"}))
            rest = _drain_until_ended(ws)
        assert err["code"] == "no_mic" and err["message"] == oc.NO_MIC_MSG
        assert "麦克风" in err["message"] and "HTTPS" in err["message"]
        assert 0.4 < took < 4, took
        assert not [e for e in rest if e.get("code") == "no_mic"], rest
        call = _calls(c)[0]
        assert call["client"] == "web" and "iPhone" in call["ua"]
        evs = call["events"]
        start = [e for e in evs if e[1].startswith("call from")][0][1]
        assert "client=web" in start and "ua=Mozilla/5.0 (iPhone" in start
        warn = [e for e in evs if "no audio from client" in e[1]]
        assert len(warn) == 1 and warn[0][2] == "warn" and "frames=1" in warn[0][1] and "client=web" in warn[0][1]
        assert call["counters"]["audio_in"] == 0


def test_no_frames_no_audio_also_warns(app):
    """A voice-only call (camera off) whose mic sends nothing: the call is >= N s old, so it warns too."""
    _gemini(app)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "client": "ios", "ua": "aiw-ios/3.9 iOS 18.1 iPhone"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            assert _recv_until(ws, "error")["code"] == "no_mic"
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        call = _calls(c)[0]
        assert call["client"] == "ios" and call["ua"].startswith("aiw-ios/")
        assert any("no audio from client" in e[1] and "client=ios" in e[1] for e in call["events"])


@pytest.mark.parametrize("how", ["audio", "muted"])
def test_audio_or_muted_client_is_not_warned(app, how):
    _gemini(app)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True, "client": "web"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            if how == "muted":
                ws.send_text(json.dumps({"type": "mute", "on": True}))
            for _ in range(6):
                ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
                if how == "audio":
                    ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
                time.sleep(0.25)
            ws.send_text(json.dumps({"type": "hangup"}))
            seen = _drain_until_ended(ws)
        assert not [e for e in seen if e.get("code") == "no_mic"], seen
        call = _calls(c)[0]
        assert not any("no audio from client" in e[1] for e in call["events"])


def test_unmute_gets_its_own_window(app):
    """Muted for a while, then unmuted with nothing coming: the warning comes N s after the unmute, not at once."""
    _gemini(app)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "client": "web"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "mute", "on": True}))
            time.sleep(1.0)
            t0 = time.time()
            ws.send_text(json.dumps({"type": "mute", "on": False}))
            assert _recv_until(ws, "error")["code"] == "no_mic"
            assert time.time() - t0 > 0.4
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)


def test_client_defaults_and_user_agent_header(app):
    _gemini(app)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok", headers={"User-Agent": "OldApp/1.0"}) as ws:
            ws.send_text(json.dumps({"type": "start", "video": False}))   # an older client: no client / ua fields
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        call = _calls(c)[0]
        assert call["client"] == "?" and call["ua"] == "OldApp/1.0"
        start = [e for e in call["events"] if e[1].startswith("call from")][0][1]
        assert "client=?" in start and "ua=OldApp/1.0" in start


def test_no_mic_check_can_be_turned_off(app, monkeypatch):
    monkeypatch.setenv("AIW_OMNI_NO_MIC_S", "0")
    _gemini(app)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "client": "web"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            time.sleep(1.0)
            ws.send_text(json.dumps({"type": "hangup"}))
            seen = _drain_until_ended(ws)
        assert not [e for e in seen if e.get("code") == "no_mic"]


# ------------------------------------------------------------------------------------------------ clients
def test_web_client_unlocks_audio_in_the_tap():
    js = (ROOT / "apps" / "web" / "call.js").read_text(encoding="utf-8")
    body = js[js.index("async function openCall("):]
    body = body[:body.index("\n}\n")]
    # the AudioContext is unlocked before the first await of the click handler
    assert body.index("callUnlockAudio();") < body.index("await ")
    for needle in ('client: "web"', 'type: "mute"', 'm.code === "no_mic"', "callAudioGate(true", "onstatechange",
                   "callMicCheck", "callDownsampler(ctx.sampleRate"):   # v3.9.38: warning text + meter removed
        assert needle in js, needle
    css = (ROOT / "apps" / "web" / "call.css").read_text(encoding="utf-8")
    assert ".call-gate" in css


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_web_call_js_syntax_and_downsampler():
    f = ROOT / "apps" / "web" / "call.js"
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    script = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
eval(src.match(/function callDownsampler[\s\S]*?\n}\n/)[0]);
const out = {};
for (const rate of [48000, 44100]) {
  let n = 0, chunks = 0, peak = 0;
  const ds = callDownsampler(rate, (c) => { chunks++; n += c.length; for (const v of c) peak = Math.max(peak, Math.abs(v)); });
  for (let t = 0; t < rate * 2; t += 128) { const b = new Float32Array(128); for (let i = 0; i < 128; i++) b[i] = 0.5 * Math.sin(2 * Math.PI * 300 * (t + i) / rate); ds(b); }
  out[rate] = { n, chunks, peak };
}
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", script, str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    for rate in ("48000", "44100"):   # 2 s of 128-sample worklet blocks -> exactly 50 chunks of 40 ms at 16 kHz
        assert res[rate]["chunks"] == 50 and res[rate]["n"] == 32000, res
        assert 0.45 < res[rate]["peak"] <= 0.5, res


def test_ios_client_robust_mic():
    swift = (ROOT / "apps" / "ios" / "Sources" / "CallView.swift").read_text(encoding="utf-8")
    assert "AVAudioConverter(" not in swift   # the converter that silently produced nothing is gone
    for needle in ('"client": "ios"', '"type": "mute"', 'o.s("code") == "no_mic"',   # v3.9.38: no-mic note removed
                   "AVAudioSession.interruptionNotification", "AVAudioSession.routeChangeNotification",
                   ".AVAudioEngineConfigurationChange", "setVoiceProcessingEnabled(voiceProcessing)",
                   "voiceProcessing: false",
                   "int16ChannelData", "floatChannelData", "removeObserver"):
        assert needle in swift, needle
    # v3.9.35 behaviour kept
    assert "lastErr = o.s(\"message\")" in swift and 'r != "hangup"' in swift
    assert swift.count("{") == swift.count("}")
