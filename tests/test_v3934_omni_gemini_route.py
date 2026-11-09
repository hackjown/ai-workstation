"""v3.9.34: the call slot (omni_model + omni_model_provider) routed by what it holds.

* a Gemini provider (base_url host generativelanguage.googleapis.com, id / name "gemini", or the omni_gemini_url relay
  host) or a Gemini Live model name (gemini-…live…, …native-audio…) in the slot = Gemini Live with that model, the
  provider's key and proxy; auto = Gemini only; the DashScope realtime endpoint is never contacted
* a non-DashScope provider without omni_realtime_url never gets the realtime (DashScope) attempt
* the default gateway keeps the v3.9.31 behaviour (realtime, then turn)
* the probe's shape for each case (Gemini Live badge only / realtime skipped / family + hint)

Fake servers: FakeGemini (BidiGenerateContent), ConnectProxy (HTTP CONNECT) from test_v3932, FakeRealtime (DashScope
realtime) from test_v3931 with oc.DASHSCOPE_RT pointed at it, and an httpx.MockTransport chat gateway for turn mode."""
import asyncio
import json

import httpx
import pytest

from aiw_api import omni_call as oc
from aiw_api import providers as pv
from test_v3931_omni_call import FakeRealtime, Gateway
from test_v3932_gemini_live import ConnectProxy, FakeGemini, Sink, b64, cfg, recv_until, tone

H = {"Authorization": "Bearer tok"}
GEM_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIW_OMNI_TURN_ASR", "0")
    oc._RT_FAIL.clear()
    oc._TURN_VARIANT.clear()
    oc._ACTIVE.clear()
    yield
    oc.TRANSPORT = None


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


def add_provider(app, pid, name, base_url, key, proxy=""):
    lst = [p for p in pv.stored(app.state.store) if p["id"] != pid]
    lst.append({"id": pid, "name": name, "kind": "openai", "base_url": base_url, "proxy": proxy, "enabled": True})
    app.state.store.kv_set("providers", lst)
    app.state.secrets.put(pv.secret_name(pid), key)


class RecordingGateway(Gateway):
    """Gateway that also records which URL / key each request went to."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    async def __call__(self, req: httpx.Request):
        self.calls.append((str(req.url), req.headers.get("authorization")))
        return await super().__call__(req)


def dashscope_trap(monkeypatch) -> FakeRealtime:
    """A fake DashScope realtime server standing in for wss://dashscope.aliyuncs.com: any connection is recorded."""
    trap = FakeRealtime()
    monkeypatch.setattr(oc, "DASHSCOPE_RT", trap.url)
    return trap


# ------------------------------------------------------------------------------------------------ helpers
def test_detection_helpers():
    assert oc.gemini_live_name("gemini-3.8-live") and oc.gemini_live_name("models/gemini-live-2.5-flash-preview")
    assert oc.gemini_live_name("gemini-2.5-flash-native-audio-preview-09-2025")
    assert not oc.gemini_live_name("gemini-3.5-pro") and not oc.gemini_live_name("qwen3.8-omni-flash")
    assert not oc.gemini_live_name("") and not oc.gemini_live_name("deliver-live")
    assert oc.qwen_omni_name("qwen3.8-omni-flash") and not oc.qwen_omni_name("qwen-max")
    assert pv.is_gemini({"base_url": GEM_BASE, "default": False})
    assert pv.is_gemini({"base_url": GEM_BASE, "default": True})            # the default gateway pointed at Google
    assert pv.is_gemini({"id": "gemini", "base_url": "https://my-relay.example/v1"})
    assert pv.is_gemini({"id": "g2", "name": "Gemini", "base_url": "https://my-relay.example/v1"})
    assert pv.is_gemini({"id": "r", "base_url": "https://relay.example.com/v1beta/openai"}, "wss://relay.example.com")
    assert not pv.is_gemini({"id": "r", "base_url": "https://relay.example.com/v1"}, "wss://other.example.com")
    assert not pv.is_gemini({"id": "ds", "name": "百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    assert not pv.is_gemini(None)
    assert pv.is_dashscope({"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
    assert pv.is_dashscope({"base_url": "https://ws9.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"})
    assert not pv.is_dashscope({"base_url": "http://192.168.50.188:28000/v1"}) and not pv.is_dashscope({})


# ------------------------------------------------------------------------------------------------ Gemini route
def test_gemini_provider_in_call_slot_uses_gemini_live_end_to_end(app, monkeypatch):
    trap = dashscope_trap(monkeypatch)
    gw = RecordingGateway()
    oc.TRANSPORT = httpx.MockTransport(gw)
    up = FakeGemini()
    px = ConnectProxy()
    add_provider(app, "gem", "Gemini", GEM_BASE, "AIzaFromProvider", proxy=f"http://127.0.0.1:{px.port}")
    # the user's setup: provider Gemini + gemini-3.8-live in the call slot, mode auto, omni_gemini_* untouched
    # (omni_gemini_url only points the test at the fake Live server)
    set_models(app, omni_model="gemini-3.8-live", omni_model_provider="gem", omni_mode="auto", omni_gemini_url=up.url)
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_route"] is True and c["rt_on"] is False
    assert c["gemini_model"] == "gemini-3.8-live" and c["gemini_key"] == "AIzaFromProvider"
    assert c["proxy"] == f"http://127.0.0.1:{px.port}"
    assert oc.OmniCall(c, Sink()).plan() == ["gemini"]
    assert oc.enabled(c) and oc.family(c) == "gemini" and "免费层" in oc.hint_for(c)
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        st = cl.get("/api/omni/status", headers=H).json()
        assert st["enabled"] and st["gemini"]["route"] is True and st["gemini"]["model"] == "gemini-3.8-live"
        assert st["gemini"]["has_key"] and st["gemini"]["proxy"] and st["realtime_on"] is False
        assert st["family"] == "gemini" and "国内需代理" in st["hint"] and "AIzaFromProvider" not in json.dumps(st)
        with cl.websocket_connect("/ws/omni-call?token=tok") as ws:
            ws.send_text(json.dumps({"type": "start", "video": False}))
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready" and ready["mode"] == "gemini" and ready["model"] == "gemini-3.8-live"
            assert ready["audio_out"] is True and not ready.get("note")
            for _ in range(3):
                ws.send_text(json.dumps({"type": "audio", "data": b64(tone(40))}))
            _e, seen = recv_until(ws, "transcript")
            ws.send_text(json.dumps({"type": "hangup"}))
            recv_until(ws, "ended")
    conn = up.conns[0]
    assert conn["setup"]["model"] == "models/gemini-3.8-live"
    assert "key=AIzaFromProvider" in conn["path"] and conn["headers"].get("x-goog-api-key") == "AIzaFromProvider"
    assert px.targets and px.targets[0] == f"127.0.0.1:{up.port}"          # through the provider's proxy
    assert trap.got == [] and trap.path == ""                              # DashScope realtime never contacted
    assert gw.calls == []                                                  # and no turn-mode chat request either


def test_gemini_route_overrides_and_probe_shape(app, monkeypatch):
    trap = dashscope_trap(monkeypatch)
    gw = RecordingGateway()
    oc.TRANSPORT = httpx.MockTransport(gw)
    up = FakeGemini()
    add_provider(app, "gem", "Gemini", GEM_BASE, "AIzaFromProvider", proxy="http://10.9.9.9:7890")
    set_models(app, omni_model="gemini-3.8-live", omni_model_provider="gem", omni_mode="auto", omni_gemini_url=up.url)
    # omni_gemini_key and omni_proxy still win over the provider's
    app.state.secrets.put("omni_gemini_key", "AIzaOwn")
    set_models(app, omni_proxy="")   # empty -> provider proxy
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_key"] == "AIzaOwn" and c["proxy"] == "http://10.9.9.9:7890"
    set_models(app, omni_proxy="socks5h://10.0.0.2:1080")
    assert oc.config(app.state.store, app.state.secrets)["proxy"] == "socks5h://10.0.0.2:1080"
    set_models(app, omni_proxy="")
    app.state.secrets.delete("omni_gemini_key")
    # probe through the API (proxy removed so the fake server is reached directly)
    add_provider(app, "gem", "Gemini", GEM_BASE, "AIzaFromProvider")
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        r = cl.post("/api/omni/probe", headers=H).json()
        assert r["route"] == "gemini" and r["family"] == "gemini" and "免费层" in r["hint"]
        assert r["mode"] == "gemini" and r["gemini"]["ok"] and r["gemini"]["audio"] is True
        assert r["gemini"]["model"] == "gemini-3.8-live"
        assert r["realtime"] == {"skipped": True} and r["turn"] == {"skipped": True}
        assert not any("qwen" in n.lower() for n in r["notes"])
        assert cl.get("/api/omni/status", headers=H).json()["probe"]["route"] == "gemini"
    assert trap.got == [] and gw.calls == []
    # a rejected key: Gemini badge off, a Key note naming the model, still no realtime / turn attempt
    bad = FakeGemini(reject="API key not valid. Please pass a valid API key.")

    async def run():
        return await oc.probe(cfg(model="gemini-3.8-live", gemini_route=True, rt_on=False, mode="auto",
                                  gemini_url=bad.url, rt_url=trap.url))
    no = asyncio.run(run())
    assert no["mode"] is None and no["gemini"]["ok"] is False and no["gemini"]["audio"] is False
    assert any("gemini-3.8-live" in n and "Key" in n for n in no["notes"])
    assert no["realtime"] == {"skipped": True} and trap.got == []


def test_gemini_route_without_key_is_disabled_not_sent_elsewhere(app, monkeypatch):
    trap = dashscope_trap(monkeypatch)
    add_provider(app, "gem", "Gemini", GEM_BASE, "")      # provider without a key
    set_models(app, omni_model="gemini-3.8-live", omni_model_provider="gem", omni_mode="auto")
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_route"] and c["gemini_key"] == "" and not oc.enabled(c)
    assert "Gemini" in oc.disabled_message(c)
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        with cl.websocket_connect("/ws/omni-call?token=tok") as ws:
            e = json.loads(ws.receive_text())
            assert e["type"] == "error" and e["code"] == "disabled" and "Gemini" in e["message"]
    assert trap.got == []


def test_live_model_name_alone_routes_but_never_borrows_a_foreign_key(app, monkeypatch):
    add_provider(app, "relay", "中转", "https://relay.example.net/v1", "sk-relay-key", proxy="http://10.1.1.1:8080")
    set_models(app, omni_model="gemini-2.5-flash-native-audio-preview-09-2025", omni_model_provider="relay")
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_route"] and c["gemini_model"] == "gemini-2.5-flash-native-audio-preview-09-2025"
    assert c["gemini_key"] == "" and c["proxy"] == ""          # sk-relay-key is not a Google key
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaEnv")
    assert oc.config(app.state.store, app.state.secrets)["gemini_key"] == "AIzaEnv"
    # the omni_gemini provider (hidden slot) still supplies the key
    add_provider(app, "g", "Google", GEM_BASE, "AIzaHiddenSlot", proxy="http://10.2.2.2:7890")
    set_models(app, omni_gemini_provider="g")
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_key"] == "AIzaHiddenSlot" and c["proxy"] == "http://10.2.2.2:7890"
    # preset id "gemini" with a custom base, and a relay matching omni_gemini_url, count as Gemini providers
    add_provider(app, "gemini", "随便叫", "https://gw.example.org/v1", "AIzaPreset")
    set_models(app, omni_model="gemini-3.5-flash", omni_model_provider="gemini", omni_gemini_provider=None)
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_route"] and c["gemini_key"] == "AIzaPreset" and c["gemini_model"] == "gemini-3.5-flash"
    add_provider(app, "r2", "relay2", "https://live-relay.example.com/v1beta/openai", "AIzaRelay")
    set_models(app, omni_model="my-voice-model", omni_model_provider="r2", omni_gemini_url="wss://live-relay.example.com")
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_route"] and c["gemini_key"] == "AIzaRelay" and c["gemini_url"].startswith("wss://live-relay.example.com/")
    # explicit turn mode on a routed slot still runs turn on the slot provider (the user chose it)
    set_models(app, omni_mode="turn")
    c = oc.config(app.state.store, app.state.secrets)
    assert oc.OmniCall(c, Sink()).plan() == ["turn"] and oc.enabled(c)


# ------------------------------------------------------------------------------------------------ realtime gate
def test_non_dashscope_provider_with_qwen_model_skips_realtime(app, monkeypatch):
    trap = dashscope_trap(monkeypatch)
    gw = RecordingGateway(reject_audio=True)
    oc.TRANSPORT = httpx.MockTransport(gw)
    add_provider(app, "lan", "LAN 网关", "http://192.168.50.188:28000/v1", "sk-lan")
    set_models(app, omni_model="qwen3.8-omni-flash", omni_model_provider="lan", omni_mode="auto")
    c = oc.config(app.state.store, app.state.secrets)
    assert c["rt_on"] is False and c["gemini_route"] is False and c["rt_key"] == "sk-lan"
    assert oc.OmniCall(c, Sink()).plan() == ["turn"]
    assert oc.family(c) == "qwen" and "业务空间" in oc.hint_for(c)
    app.state.secrets.put("omni_gemini_key", "AIzaX")
    assert oc.OmniCall(oc.config(app.state.store, app.state.secrets), Sink()).plan() == ["gemini", "turn"]
    app.state.secrets.delete("omni_gemini_key")
    # realtime-only mode: refused with a note, nothing sent to DashScope
    set_models(app, omni_mode="realtime")
    c = oc.config(app.state.store, app.state.secrets)
    assert oc.OmniCall(c, Sink()).plan() == []
    with pytest.raises(RuntimeError, match="百炼"):
        asyncio.run(oc.OmniCall(c, Sink()).start())
    # probe: realtime skipped (no badge), turn tried on the provider
    set_models(app, omni_mode="auto")
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        r = cl.post("/api/omni/probe", headers=H).json()
    assert r["realtime"]["skipped"] is True and "百炼" in r["realtime"]["reason"]
    assert r["turn"]["ok"] and r["mode"] == "turn" and r["family"] == "qwen" and r["route"] == ""
    assert not any("业务空间" in n for n in r["notes"])          # the realtime-URL note only after a real attempt
    assert trap.got == [] and trap.path == ""
    assert gw.calls and all(u.startswith("http://192.168.50.188:28000/v1/") and a == "Bearer sk-lan" for u, a in gw.calls)
    # an explicit omni_realtime_url opts back in
    set_models(app, omni_realtime_url=trap.url)
    c = oc.config(app.state.store, app.state.secrets)
    assert c["rt_on"] is True and oc.OmniCall(c, Sink()).plan() == ["realtime", "turn"]


def test_dashscope_providers_keep_realtime():
    class St:
        def __init__(self, models, provs):
            self.d = {"models": models, "providers": provs}

        def kv_get(self, k, d=None):
            return self.d.get(k, d)

    class Sec(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    for base, want in (("https://dashscope.aliyuncs.com/compatible-mode/v1", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"),
                       ("https://ws7.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                        "wss://ws7.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime")):
        st = St({"omni_model": "qwen3.8-omni-flash", "omni_model_provider": "bl"},
                [{"id": "bl", "name": "百炼", "kind": "openai", "base_url": base, "enabled": True}])
        c = oc.config(st, Sec({"provider_key_bl": "sk-bl"}))
        assert c["rt_on"] is True and c["rt_url"] == want and c["rt_key"] == "sk-bl" and not c["gemini_route"]
        assert oc.OmniCall(c, Sink()).plan() == ["realtime", "turn"]


# ------------------------------------------------------------------------------------------------ default gateway
def test_default_gateway_behaviour_unchanged(app, monkeypatch):
    trap = dashscope_trap(monkeypatch)
    gw = RecordingGateway(reject_audio=True)
    oc.TRANSPORT = httpx.MockTransport(gw)
    set_models(app, base_url="http://192.168.50.188:28000/v1", omni_model="qwen3.8-omni-flash")   # no providers at all
    c = oc.config(app.state.store, app.state.secrets)
    assert c["rt_on"] is True and c["gemini_route"] is False and c["rt_url"] == trap.url and c["rt_key"] == "gw-key"
    assert oc.OmniCall(c, Sink()).plan() == ["realtime", "turn"]
    assert c["gemini_model"] == oc.DEFAULT_GEMINI_MODEL
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        r = cl.post("/api/omni/probe", headers=H).json()
        st = cl.get("/api/omni/status", headers=H).json()
    assert r["realtime"]["ok"] is True and r["mode"] == "realtime" and r["turn"]["ok"] and "gemini" not in r
    assert trap.headers.get("authorization") == "Bearer gw-key" and r["family"] == "qwen"
    assert st["realtime_on"] is True and st["gemini"]["route"] is False
    # omni_model_provider explicitly "default" (stored as None) is the same thing
    set_models(app, omni_model_provider=None)
    assert oc.config(app.state.store, app.state.secrets)["rt_on"] is True
    # the default gateway pointed at Google with a Live model: routed, the gateway key is the Google key
    set_models(app, base_url=GEM_BASE, omni_model="gemini-3.8-live")
    c = oc.config(app.state.store, app.state.secrets)
    assert c["gemini_route"] and c["gemini_key"] == "gw-key" and c["rt_on"] is False
    # a hand-built config dict without the v3.9.34 keys plans exactly as before
    old = {k: v for k, v in cfg(mode="auto", model="m").items() if k not in ("rt_on", "gemini_route")}
    assert oc.OmniCall(old, Sink()).plan() == ["gemini", "realtime", "turn"]


# ------------------------------------------------------------------------------------------------ web
def test_web_probe_display_wired():
    import pathlib
    import shutil
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    js = (root / "apps/web/app.js").read_text(encoding="utf-8")
    assert "function omniFamily" in js and "OMNI_HINTS" in js and 'p.route === "gemini"' in js
    assert "免费层数据会用于改进产品" in js and '"Gemini Live"' in js
    assert "rt.skipped" in js and "body.omni_model_provider = ps.value" in js
    role = next(line for line in js.splitlines() if 'key: "omni_model"' in line)
    assert "qwen" not in role.lower()                         # the static Qwen hint is gone from the label
    if shutil.which("node"):
        # evaluate just the probe-display functions (the rest of app.js needs a browser)
        part = js[js.index("const OMNI_HINTS"):js.index("async function probeOmni")]
        esc = next(line for line in js.splitlines() if line.startswith("const esc"))
        code = esc + "\nconst S = {};\n" + part + """
;Object.assign(S, {omniProbes: {}, providers: [{id: "gem", name: "Gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai"}]});
function fakeSel(v, pid) { return {value: v, parentNode: {querySelector: () => ({value: pid})}, closest: () => null}; }
const out = {
  gem: omniFamily(fakeSel("gemini-3.8-live", "gem")), gemProv: omniFamily(fakeSel("whatever", "gem")),
  qwen: omniFamily(fakeSel("qwen3.8-omni-flash", "")), other: omniFamily(fakeSel("gpt-4o-audio", "")),
  html: omniProbeHTML({route: "gemini", family: "gemini", model: "gemini-3.8-live", gemini: {ok: true, audio: true, ms: 900, model: "gemini-3.8-live"}, realtime: {skipped: true}, turn: {skipped: true}, notes: []}),
  html2: omniProbeHTML({family: "", model: "x", realtime: {skipped: true}, turn: {ok: true, audio: true, ms: 1000}, mode: "turn", notes: []}),
  html3: omniProbeHTML(null, "qwen"),
};
console.log(JSON.stringify(out));
"""
        p = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr[-800:]
        o = json.loads(p.stdout.strip().splitlines()[-1])
        assert o["gem"] == "gemini" and o["gemProv"] == "gemini" and o["qwen"] == "qwen" and o["other"] == ""
        assert "Gemini Live" in o["html"] and "会说话" in o["html"] and "实时" not in o["html"] and "轮流" not in o["html"]
        assert "免费层" in o["html"] and "Qwen" not in o["html"]
        assert "实时 " not in o["html2"] and "轮流" in o["html2"] and "Qwen" not in o["html2"]
        assert "Qwen-Omni" in o["html3"]
