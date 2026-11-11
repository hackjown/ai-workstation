"""v3.9.38: a field call (iOS app, speaker, Gemini Live) that worked, but 2.5 s after the start the app restarted its
audio engine WITHOUT voice processing ("why=no mic callbacks") although the mic then delivered a chunk every 40 ms -
the whole call ran with the echo canceller off, and every later client_audio turn report repeated the stale why.
Root cause: enabling voice processing / the speaker route reconfigures the audio unit right after engine.start();
AVAudioEngine stops on that configuration change, and v3.9.37 ignored every notification within 1 s of its own start.
Now:
  * iOS: notifications are decided when the restart runs (skipped only while the engine still runs); tap callbacks are
    counted per engine start, first thing in the callback; the fallback waits 6 s from engine.start(), retries WITH
    voice processing first, drops it only after a second 6 s of nothing; why per report, vp_off_reason apart
  * no reminder texts on the call screen (iOS + web): only errors that end the call, plus the minimal web
    「点一下开始声音」 button; the server still logs no_mic and keeps everything in 通话诊断 (回声消除关闭 included)"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from aiw_api import omni_call as oc
from test_v3932_gemini_live import JPEG, set_models  # noqa: F401
from test_v3935_gemini_robust import Up, _recv_until

ROOT = pathlib.Path(__file__).resolve().parents[1]
SWIFT = (ROOT / "apps" / "ios" / "Sources" / "CallView.swift").read_text(encoding="utf-8")
JS = (ROOT / "apps" / "web" / "call.js").read_text(encoding="utf-8")
CSS = (ROOT / "apps" / "web" / "call.css").read_text(encoding="utf-8")
H = {"Authorization": "Bearer tok"}


def _code(src: str) -> str:
    """The source without comments (the history in the header comments may quote old texts)."""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in src.splitlines())


def _func(src: str, head: str) -> str:
    body = src[src.index(head):]
    return body[:body.index("\n    }\n")]


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


def _drain_until_ended(ws, limit=80):
    seen = []
    for _ in range(limit):
        e = json.loads(ws.receive_text())
        seen.append(e)
        if e["type"] == "ended":
            return seen
    raise AssertionError(seen)


# ------------------------------------------------------------------------------------------------ iOS: root cause
def test_ios_config_change_no_longer_swallowed():
    sr = _func(SWIFT, "private func scheduleRestart(")
    # v3.9.37's early return within 1 s of our own start is gone; the decision is made in the work item, on isRunning
    assert "< 1.0 { return }" not in sr
    w = sr[sr.index("DispatchWorkItem"):]
    assert "self.engine.isRunning" in w and "now - started < 1.5" in w
    assert "autoRestarts.count >= 4" in w   # loop guard instead of ignoring
    assert '"config (engine stopped)"' in SWIFT


def test_ios_tap_counted_per_start_before_anything_else():
    start = _func(SWIFT, "private func startAudio(")
    # a new generation and zeroed counters BEFORE the tap is installed; the clock starts after engine.start()
    assert start.index("tapGen += 1") < start.index("installTap")
    assert start.index("tapCount = 0; lastTapAt = 0; audioStartAt = 0") < start.index("try engine.start()")
    assert start.index("try engine.start()") < start.index("audioStartAt = ProcessInfo.processInfo.systemUptime")
    assert "self?.onTap(buf, gen: gen)" in start
    tap = SWIFT[SWIFT.index("private func onTap("):]
    tap = tap[:tap.index("\n    }\n")]
    # counted first, whatever the buffer holds (no frames / mute / gate check in front of it)
    assert tap.index("tapCount += 1") < tap.index("onMic(buf)")
    assert "frameLength" not in tap and "isMuted" not in tap and "gate" not in tap
    assert "gen == tapGen" in tap


def test_ios_fallback_ladder_is_conservative():
    assert "stallSeconds: TimeInterval = 6" in SWIFT
    check = _func(SWIFT, "private func checkMic()")
    assert "guard started > 0" in check and "now - max(last, started) >= stallSeconds" in check
    assert "!restartPending" in check
    # stage 0 -> restart WITH voice processing; only stage 1 (+ still on) -> without
    first = check.index("voiceProcessing: true")
    second = check.index("voiceProcessing: false")
    assert first < second and "micStage == 0" in check[:first] and "micStage == 1 && vpOn" in check[first:second]
    assert "isMuted" not in check and "lastVoice" not in check   # quiet / muted never counts
    # the server's no_mic only nudges the first (VP-on) step, never drops voice processing itself
    no_mic = SWIFT[SWIFT.index('o.s("code") == "no_mic"'):]
    no_mic = no_mic[:no_mic.index('case "ended"')]
    assert "voiceProcessing: true" in no_mic and "voiceProcessing: false" not in no_mic


def test_ios_why_per_report_and_vp_off_reason_apart():
    rep = _func(SWIFT, "private func reportAudio(")
    assert "why: String)" in rep and '"why": why' in rep and "audioWhy" not in rep
    assert 'o["vp_off_reason"] = vpOffReason' in rep and '"taps": taps' in rep and '"mic_stage": micStage' in rep
    assert 'reportAudio("turn", why: "turn")' in SWIFT
    assert 'reportAudio("restart", why: why)' in SWIFT and 'reportAudio("start", why: audioWhy)' in SWIFT
    assert SWIFT.count("{") == SWIFT.count("}") and SWIFT.count("(") == SWIFT.count(")")


# ------------------------------------------------------------------------------------------------ no notes on screen
def test_ios_call_screen_has_no_reminder_texts():
    code = _code(SWIFT)
    for gone in ("回声消除没开", "麦克风没有声音", "麦克风只有静音", "通话声音被系统打断", "麦克风重启失败",
                 "没有摄像头权限，先用语音", "正在\\(name)", "@Published var note", "c.note", "self.note", "note = "):
        assert gone not in code, gone
    # fatal errors still shown, and the server's reason at "ended"
    assert "Text(c.error)" in code and 'lastErr = o.s("message")' in code and 'r != "hangup"' in code
    assert 'fail("没有麦克风权限' in code and 'fail("麦克风打不开' in code


def test_web_call_screen_has_no_reminder_texts():
    code = _code(JS)
    for gone in ("callMicWarn", "callMeter", "callWarn", "call-meter", "call-warn", "没有收到麦克风声音",
                 "麦克风只有静音", "麦克风被系统停掉了", "点一下重新打开麦克风", "服务器没有收到麦克风声音",
                 "摄像头打不开，先用语音", "m.note ? m.note", 'toast(m.status === "running"', 'toast(m.message || "出错了")'):
        assert gone not in code, gone
    assert ".call-meter" not in CSS and ".call-warn" not in CSS
    # the audio-unlock button stays, minimal and with one text
    assert "点一下开始声音" in code and "function callAudioGate(on)" in code and "v3.9.38: minimal" in CSS
    on = code[code.index("function callOn(m)"):]
    on = on[:on.index("\n}\n")]
    assert 'm.code === "no_mic"' in on and "callLog(" in on
    assert "callEnd(m.message" in on and "CALL.lastErr" in on   # fatal: before ready / disabled / at ended
    assert 'callGateReport("turn", "turn")' in code and "why: why || event" in code


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_web_call_js_node_check():
    for f in ("call.js", "app.js", "v3924.js"):
        r = subprocess.run(["node", "--check", str(ROOT / "apps" / "web" / f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_diag_keeps_echo_cancel_off_flag():
    app_js = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert "回声消除关闭" in app_js and "vp_off_reason" in app_js


# ------------------------------------------------------------------------------------------------ server
def test_server_still_logs_no_mic(app):
    up = Up(answer=())
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url)
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True, "client": "ios"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "frame", "data": JPEG}))
            assert _recv_until(ws, "error")["code"] == "no_mic"   # still sent (iOS uses it silently)
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        call = c.get("/api/omni/calls", headers=H).json()["calls"][0]
        assert [e for e in call["events"] if "no audio from client" in e[1] and e[2] == "warn"]


def test_client_audio_why_is_per_report():
    d = oc.CallDiag(keep=False)
    oc.client_audio_report(d, {"type": "client_audio", "event": "restart", "vp": False, "why": "no mic callbacks 6s after"
                               " the voice-processing restart", "vp_off_reason": "no mic callbacks 6s after the voice-"
                               "processing restart", "taps": 0, "mic_stage": 2, "running": True})
    assert d.client_audio["why"].startswith("no mic callbacks") and d.client_audio["vp_off_reason"]
    line = d.events[-1]
    assert line[2] == "warn" and "vp_off_reason=no mic callbacks" in line[1] and "mic_stage=2" in line[1] and "taps=0" in line[1]
    oc.client_audio_report(d, {"type": "client_audio", "event": "turn", "vp": True, "why": "turn", "echo": 0.01})
    assert d.client_audio["why"] == "turn" and "vp_off_reason" not in d.client_audio
    # a report without why does not inherit the last one
    oc.client_audio_report(d, {"type": "client_audio", "event": "turn", "vp": True, "echo": 0.02})
    assert "why" not in d.client_audio and "why=" not in d.events[-1][1]
    assert d.client_audio["vp"] is True   # vp itself is still the latest state
