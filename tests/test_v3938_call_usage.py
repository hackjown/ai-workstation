"""v3.9.38: video-call tokens go into token_usage (role "call", model = the model used). Calls bypass llm_router, so
omni_call counts them itself: Gemini Live usageMetadata (cumulative or per turn - LiveUsageMeter - and never twice
across reconnects), DashScope realtime response.done usage, the turn backend's stream usage; per modality in 通话诊断;
/api/usage per_role shows "call" (labelled 通话 in the web and iOS usage pages)."""
import json
import pathlib
import time

import pytest

from aiw_api import omni_call as oc
from test_v3932_gemini_live import b64, set_models  # noqa: F401
from test_v3935_gemini_robust import LOUD, Up, _recv_until

ROOT = pathlib.Path(__file__).resolve().parents[1]
H = {"Authorization": "Bearer tok"}


def um(p, r, pa=None, pv=None, ra=None):
    m = {"promptTokenCount": p, "responseTokenCount": r, "totalTokenCount": p + r}
    det = [x for x in (("AUDIO", pa), ("VIDEO", pv)) if x[1] is not None]
    if det:
        m["promptTokensDetails"] = [{"modality": k, "tokenCount": v} for k, v in det]
    if ra is not None:
        m["responseTokensDetails"] = [{"modality": "AUDIO", "tokenCount": ra}]
    return m


def total(ds):
    t = oc.usage_zero()
    for d in ds:
        if d:
            t = oc.usage_add(t, d)
    return t


# ------------------------------------------------------------------------------------------------ parsing
def test_gemini_usage_fields():
    u = oc.gemini_usage({"promptTokenCount": 100, "responseTokenCount": 40, "toolUsePromptTokenCount": 5,
                         "thoughtsTokenCount": 2, "totalTokenCount": 147,
                         "promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 70}, {"modality": "VIDEO", "tokenCount": 20},
                                                 {"modality": "TEXT", "tokenCount": 10}],
                         "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 40}]})
    assert u["in"] == 105 and u["out"] == 42
    assert u["in_by"] == {"audio": 70, "video": 20, "text": 10} and u["out_by"] == {"audio": 40, "text": 2}
    assert oc.gemini_usage({"promptTokenCount": 10, "totalTokenCount": 25})["out"] == 15
    assert oc.gemini_usage({"promptTokenCount": 10, "candidatesTokenCount": 3})["out"] == 3
    assert oc.gemini_usage({}) is None and oc.gemini_usage("x") is None


def test_openai_usage_realtime_and_chat():
    rt = oc.openai_usage({"total_tokens": 300, "input_tokens": 200, "output_tokens": 100,
                          "input_token_details": {"text_tokens": 50, "audio_tokens": 120, "image_tokens": 30},
                          "output_token_details": {"text_tokens": 20, "audio_tokens": 80}})
    assert rt == {"in": 200, "out": 100, "in_by": {"text": 50, "audio": 120, "image": 30}, "out_by": {"text": 20, "audio": 80}}
    ch = oc.openai_usage({"prompt_tokens": 9, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 3, "audio_tokens": 6}})
    assert ch["in"] == 9 and ch["out"] == 4 and ch["in_by"] == {"audio": 6}
    assert oc.openai_usage(None) is None and oc.openai_usage({}) is None


# ------------------------------------------------------------------------------------------------ the meter
def test_meter_cumulative_session_counts_growth_only():
    m = oc.LiveUsageMeter()
    m.open("a", False)
    ds = []
    for p, r in ((100, 20), (250, 50), (400, 90)):   # cumulative per session
        ds += m.observe("a", oc.gemini_usage(um(p, r, pa=p)))
        ds.append(m.commit("a"))
    t = total(ds)
    assert (t["in"], t["out"]) == (400, 90) and t["in_by"] == {"audio": 400} and m.mode == "unknown"


def test_meter_per_turn_detected_and_corrected():
    m = oc.LiveUsageMeter()
    m.open("a", False)
    ds = []
    vals = ((100, 20), (180, 35), (260, 10), (330, 25))   # per turn: the prompt grows (context), responses vary
    for p, r in vals:
        ds += m.observe("a", oc.gemini_usage(um(p, r)))
        ds.append(m.commit("a"))
    t = total(ds)
    assert m.mode == "turn"
    assert (t["in"], t["out"]) == (sum(p for p, _ in vals), sum(r for _, r in vals))


def test_meter_several_reports_in_one_turn_latest_wins():
    m = oc.LiveUsageMeter()
    m.open("a", False)
    ds = m.observe("a", oc.gemini_usage(um(100, 5))) + m.observe("a", oc.gemini_usage(um(100, 30)))
    ds.append(m.commit("a"))
    ds.append(m.commit("a"))   # nothing pending: nothing more
    t = total(ds)
    assert (t["in"], t["out"]) == (100, 30)


def test_meter_resumed_session_continues_or_restarts_without_double_count():
    # counters continue after resumption: only the growth
    m = oc.LiveUsageMeter()
    m.open(1, False)
    ds = m.observe(1, oc.gemini_usage(um(100, 20))) + [m.commit(1), m.close(1)]
    m.open(2, True)
    ds += m.observe(2, oc.gemini_usage(um(160, 30))) + [m.commit(2)]
    t = total(ds)
    assert (t["in"], t["out"]) == (160, 30)
    # counters restart after resumption: the new connection counted from zero
    m = oc.LiveUsageMeter()
    m.open(1, False)
    ds = m.observe(1, oc.gemini_usage(um(100, 20))) + [m.commit(1), m.close(1)]
    m.open(2, True)
    ds += m.observe(2, oc.gemini_usage(um(40, 8))) + [m.commit(2)]
    t = total(ds)
    assert (t["in"], t["out"]) == (140, 28)
    # a fresh session never continues the old counters
    m = oc.LiveUsageMeter()
    m.open(1, False)
    ds = m.observe(1, oc.gemini_usage(um(100, 20))) + [m.commit(1)]
    m.open(2, False)
    ds += m.observe(2, oc.gemini_usage(um(120, 25))) + [m.commit(2)]
    t = total(ds)
    assert (t["in"], t["out"]) == (220, 45)


# ------------------------------------------------------------------------------------------------ end to end
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("AIW_OMNI_NO_MIC_S", "0")
    monkeypatch.setenv("AIW_OMNI_USAGE_FLUSH_S", "0")   # write every delta at once (tests)
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


def _wait(cond, s=4.0):
    t = time.time() + s
    while time.time() < t:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _drain_until_ended(ws, limit=120):
    for _ in range(limit):
        if json.loads(ws.receive_text())["type"] == "ended":
            return
    raise AssertionError("no ended")


def _turn(up, usage, i=-1):
    up.push({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": b64(LOUD)}}]}}}, i)
    up.push({"usageMetadata": usage, "serverContent": {"turnComplete": True}}, i)


def test_gemini_call_usage_recorded_with_reconnect(app):
    up = Up(answer=())
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url, omni_gemini_model="gemini-3.8-live")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": True, "client": "web"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
            assert _wait(lambda: up.live)
            _turn(up, um(100, 20, pa=80, pv=20, ra=20))   # cumulative counters on connection 1
            assert _recv_until(ws, "audio")
            _turn(up, um(250, 50, pa=190, pv=60, ra=50))
            assert _recv_until(ws, "audio")
            up.drop()                                       # reconnect, resumed with h1
            assert _wait(lambda: len(up.live) >= 2 and len(up.conns) >= 2 and up.conns[1]["setup"] is not None)
            time.sleep(0.3)
            _turn(up, um(300, 70, pa=230, pv=70, ra=70))   # resumed session continues the counters
            assert _recv_until(ws, "audio")
            time.sleep(0.3)
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        call = c.get("/api/omni/calls", headers=H).json()["calls"][0]
        tk = call["tokens"]
        assert (tk["in"], tk["out"]) == (300, 70), tk
        assert tk["in_by"] == {"audio": 230, "video": 70} and tk["out_by"] == {"audio": 70}
        assert any(e[1].startswith("tokens in=300") for e in call["events"])
        u = c.get("/api/usage?days=1", headers=H).json()
        role = [r for r in u["per_role"] if r["role"] == "call"]
        assert role and role[0]["input"] == 300 and role[0]["output"] == 70, u["per_role"]
        assert [m for m in u["per_model"] if m["model"] == "gemini-3.8-live"]


def test_gemini_per_turn_usage_recorded_in_full(app):
    up = Up(answer=())
    set_models(app, omni_mode="gemini", omni_gemini_url=up.url, omni_gemini_model="gemini-3.8-live")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False, "client": "ios"}))
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_text(json.dumps({"type": "audio", "data": b64(LOUD)}))
            assert _wait(lambda: up.live)
            for p, r in ((100, 30), (150, 10), (210, 25)):
                _turn(up, um(p, r))
                assert _recv_until(ws, "audio")
            time.sleep(0.3)
            ws.send_text(json.dumps({"type": "hangup"}))
            _drain_until_ended(ws)
        call = c.get("/api/omni/calls", headers=H).json()["calls"][0]
        assert (call["tokens"]["in"], call["tokens"]["out"]) == (460, 65) and call["tokens"]["mode"] == "turn"
        u = c.get("/api/usage?days=1", headers=H).json()
        role = [r for r in u["per_role"] if r["role"] == "call"][0]
        assert (role["input"], role["output"]) == (460, 65)


def test_batching_writes_at_hangup(app, monkeypatch):
    monkeypatch.setenv("AIW_OMNI_USAGE_FLUSH_S", "3600")
    rows = []
    call = oc.OmniCall({"model": "m"}, None, usage_sink=rows.append)
    import asyncio

    async def go():
        for _ in range(5):
            await call._emit({"type": "_usage", "model": "qwen-omni", "in": 10, "out": 3, "in_by": {"audio": 10}, "out_by": {}})
        assert rows == []          # batched, nothing written per delta
        await call.finish("hangup")
    asyncio.run(go())
    assert rows == [{"model": "qwen-omni", "role": "call", "task_id": "", "input": 50, "output": 15}]
    assert call.diag.tokens["in_by"] == {"audio": 50}


def test_realtime_and_turn_backends_emit_usage():
    import asyncio
    got = []

    async def emit(ev):
        got.append(ev)
    rt = oc.RealtimeBackend(oc.with_defaults({"rt_model": "qwen3.8-omni-flash-realtime"}), emit, None, "", False)
    asyncio.run(rt._on({"type": "response.done", "response": {"usage": {"input_tokens": 12, "output_tokens": 7,
                        "input_token_details": {"audio_tokens": 12}, "output_token_details": {"audio_tokens": 7}}}}))
    u = [e for e in got if e["type"] == "_usage"]
    assert u and u[0]["model"] == "qwen3.8-omni-flash-realtime" and u[0]["in"] == 12 and u[0]["out_by"] == {"audio": 7}
    src = (ROOT / "services" / "api" / "aiw_api" / "omni_call.py").read_text(encoding="utf-8")
    stream = src[src.index("    async def _stream("):src.index("    async def _answer(")]
    assert '"include_usage": True' in src and "openai_usage(chunk.get(\"usage\"))" in stream and '"_usage"' in stream


def test_usage_ui_labels():
    assert 'call: "通话"' in (ROOT / "apps" / "web" / "v3924.js").read_text(encoding="utf-8")
    assert '"call": "通话"' in (ROOT / "apps" / "ios" / "Sources" / "V3924Views.swift").read_text(encoding="utf-8")
    app_js = (ROOT / "apps" / "web" / "app.js").read_text(encoding="utf-8")
    assert "function omniTokLine(" in app_js and "omniTokLine(c.tokens)" in app_js
