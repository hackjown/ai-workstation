"""v3.9.33: model provider list (kv providers + the implicit 默认 provider, "<slot>_provider" fields): backward
compatibility with only the global base_url + key, CRUD with keys that never come back from the API, per-slot routing
to two fake OpenAI-compatible servers with different keys (main / vision / chat / embed / image / omni / autoroute),
the grouped model list, the probe through a slot's provider, the connection test, and deleting / disabling a provider
that is in use."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from aiw_api import providers as pv

KEY_A, KEY_B, GW = "sk-AAAA-provider-a-secret", "sk-BBBB-provider-b-secret", "gw-key"


class FakeGateway:
    """A tiny OpenAI-compatible server: records the Authorization header and the model of each request."""

    def __init__(self, tag):
        self.tag, self.calls = tag, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):   # noqa: N802
                outer.calls.append(("GET", self.path, self.headers.get("Authorization"), None))
                self._send({"data": [{"id": f"{outer.tag}-model-1"}, {"id": f"models/{outer.tag}-model-2"}]})

            def do_POST(self):   # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.calls.append(("POST", self.path, self.headers.get("Authorization"), body.get("model")))
                if self.path.endswith("/embeddings"):
                    return self._send({"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
                self._send({"choices": [{"message": {"content": f"from {outer.tag}"}, "finish_reason": "stop"}],
                            "usage": {"completion_tokens": 2}})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def auths(self):
        return [c[2] for c in self.calls]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("AIW_LLM_STREAM", "0")
    yield
    pv.TRANSPORT = None


@pytest.fixture
def app(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    s.scheduler_enabled = False
    a = build_app(s)
    a.state.secrets.put("openai_api_key", GW)
    return a


H = {"Authorization": "Bearer tok"}


def set_models(app, **kw):
    m = dict(app.state.store.kv_get("models", {}) or {})
    m.update(kw)
    app.state.store.kv_set("models", m)


def add(c, **body):
    r = c.post("/api/providers", headers=H, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ tests
def test_backward_compat_only_global_settings(app):
    set_models(app, model="glm-4.6", fallback="deepseek-v4", base_url="http://gw.lan:8000/v1", vision_model="qwen-vl")
    ses = app.state.sessions
    llm = ses._llm()
    assert [(p.model, p.key, p.url) for p in llm.providers] == [
        ("glm-4.6", GW, "http://gw.lan:8000/v1/chat/completions"), ("deepseek-v4", GW, "http://gw.lan:8000/v1/chat/completions")]
    assert all(p._px == {} for p in llm.providers) and llm.role == "main"
    # exactly what the old LLMRouter.from_settings built
    from llm_router import LLMRouter
    old = LLMRouter.from_settings("glm-4.6", "deepseek-v4", GW, "http://gw.lan:8000/v1", ses._tool_choices(), None, role="main")
    assert [(p.model, p.key, p.url, p.thinking) for p in old.providers] == [(p.model, p.key, p.url, p.thinking) for p in llm.providers]
    v = ses._vision_llm()
    assert [(p.model, p.key) for p in v.providers][0] == ("qwen-vl", GW)
    c = pv.slot_conn(app.state.store, app.state.secrets, "chat_model")
    assert c["id"] == "default" and c["base_url"] == "http://gw.lan:8000/v1" and c["key"] == GW
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        r = cl.get("/api/providers", headers=H).json()
        assert [p["id"] for p in r["providers"]] == ["default"] and r["providers"][0]["has_key"] is True
        assert {p["name"] for p in r["presets"]} >= {"百炼", "Gemini", "OpenAI", "DeepSeek"}
        assert "key" not in r["providers"][0]
        # old settings PUT (no provider fields) still works and keeps everything
        assert cl.put("/api/settings/models", headers=H, json={"model": "glm-4.6", "base_url": "http://gw.lan:8000/v1"}).status_code == 200
    # no key anywhere: LLMRouter.from_env(), as before (which raises without any env key either)
    app.state.secrets.delete("openai_api_key")
    with pytest.raises(ValueError):
        ses._llm()


def test_crud_and_keys_never_returned(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        p = add(c, name="百炼", id="bailian", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/", key=KEY_A)
        assert p["id"] == "bailian" and p["has_key"] is True and p["base_url"].endswith("/compatible-mode/v1") and "key" not in p
        p2 = add(c, name="Gemini Relay", base_url="https://generativelanguage.googleapis.com/v1beta/openai/", proxy="socks5h://10.0.0.2:1080")
        assert p2["id"] == "gemini-relay" and p2["has_key"] is False
        assert c.post("/api/providers", headers=H, json={"name": "x", "id": "bailian", "base_url": "https://a"}).status_code == 409
        assert c.post("/api/providers", headers=H, json={"name": "x", "id": "default", "base_url": "https://a"}).status_code == 400
        assert c.post("/api/providers", headers=H, json={"name": "x", "base_url": "ftp://a"}).status_code == 400
        assert c.post("/api/providers", headers=H, json={"name": "x", "base_url": "https://a", "kind": "google"}).status_code == 400
        assert c.post("/api/providers", headers=H, json={"name": "y", "base_url": "https://a", "proxy": "ftp://p"}).status_code == 400
        r = c.put("/api/providers/gemini-relay", headers=H, json={"key": KEY_B, "name": "Gemini"})
        assert r.status_code == 200 and r.json()["has_key"] and r.json()["name"] == "Gemini"
        assert c.put("/api/providers/default", headers=H, json={"name": "x"}).status_code == 400
        # the keys appear in no response
        for url in ("/api/providers", "/api/settings/models", "/api/secrets", "/api/omni/status"):
            t = c.get(url, headers=H).text
            assert KEY_A not in t and KEY_B not in t, url
        lst = c.get("/api/providers", headers=H).json()["providers"]
        assert [x["id"] for x in lst] == ["default", "bailian", "gemini-relay"] and all(x["has_key"] for x in lst)
        assert app.state.secrets.get("provider_key_bailian") == KEY_A   # stored encrypted, per provider id
        raw = app.state.store.kv_get("providers")
        assert KEY_A not in json.dumps(raw)
        # the provider's proxy reaches the HTTP client of every model on it
        c.patch("/api/settings/models", headers=H, json={"model": "gemini-3.5-pro", "model_provider": "gemini-relay"})
        p0 = app.state.sessions._llm().providers[0]
        assert p0.key == KEY_B and p0._px == {"proxy": "socks5h://10.0.0.2:1080"}
        assert p0.url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def test_per_slot_routing_to_different_servers(app):
    a, b = FakeGateway("A"), FakeGateway("B")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        add(c, name="A", id="pa", base_url=a.base, key=KEY_A)
        add(c, name="B", id="pb", base_url=b.base, key=KEY_B, proxy="")
        r = c.patch("/api/settings/models", headers=H, json={
            "model": "main-m", "model_provider": "pa", "fallback": "fb-m", "vision_model": "eye-m", "vision_model_provider": "pb",
            "chat_model": "chat-m", "chat_model_provider": "pb", "embed_model": "emb-m", "embed_model_provider": "pa",
            "image_model": "img-m", "image_model_provider": "pb", "omni_model": "omni-m", "omni_model_provider": "pb",
            "omni_gemini_provider": "pa", "base_url": "http://gw.invalid/v1"})
        assert r.status_code == 200, r.text
        assert c.patch("/api/settings/models", headers=H, json={"small_model_provider": "nope"}).status_code == 400
        assert c.patch("/api/settings/models", headers=H, json={"small_model_provider": "default"}).json()["small_model_provider"] is None
    ses, store, sec = app.state.sessions, app.state.store, app.state.secrets
    llm = ses._llm()
    assert [(p.model, p.key) for p in llm.providers] == [("main-m", KEY_A), ("fb-m", GW)]
    assert llm.providers[0].url == a.base + "/chat/completions" and llm.providers[1].url == "http://gw.invalid/v1/chat/completions"

    async def run():
        out = [await llm.providers[0].complete("s", "hi")]
        out.append(await ses._vision_llm().providers[0].complete("s", "look"))
        out.append(await app.state.chat.llm("chat").providers[0].complete("s", "chat"))
        out.append(await app.state.memory.embed(["x"]))
        return out
    got = asyncio.run(run())
    assert got[:3] == ["from A", "from B", "from B"] and got[3] == [[0.1, 0.2]]
    assert a.calls[0][2] == f"Bearer {KEY_A}" and a.calls[0][3] == "main-m"
    assert (b.calls[0][2], b.calls[0][3]) == (f"Bearer {KEY_B}", "eye-m") and b.calls[1][3] == "chat-m"
    assert a.calls[1][1].endswith("/embeddings") and a.calls[1][2] == f"Bearer {KEY_A}"
    # image model on provider B; omni turn mode on B; Gemini key borrowed from provider A
    img = ses._image_cfg()
    assert img["chain"][0]["base_url"] == b.base and img["chain"][0]["key"] == KEY_B
    from aiw_api import omni_call as oc
    oc_cfg = oc.config(store, sec)
    assert oc_cfg["base_url"] == b.base and oc_cfg["key"] == KEY_B and oc_cfg["rt_key"] == KEY_B
    assert oc_cfg["tts_base_url"] == b.base and oc_cfg["gemini_key"] == KEY_A
    sec.put("omni_gemini_key", "AIzaOwn")
    assert oc.config(store, sec)["gemini_key"] == "AIzaOwn"   # its own key wins
    # a routed model name: same as a slot -> that slot's provider; autoroute.dev.provider names one explicitly
    m = store.kv_get("models")
    assert pv.provider_for_model(store, "eye-m", m) == "pb" and pv.provider_for_model(store, "unknown-x", m) == ""
    r2 = pv.router(store, sec, [("strong-dev", "", "pb"), ("main-m", "model")], effort="high", role="main")
    assert [(p.model, p.key, p.effort) for p in r2.providers] == [("strong-dev", KEY_B, "high"), ("main-m", KEY_A, "")]
    from aiw_api import autoroute
    autoroute.save(store, {"dev": {"model": "strong-dev", "provider": "pb"}})
    rt = autoroute.dev_route(store, "fix")
    assert rt["model"] == "strong-dev" and rt["provider"] == "pb"


def test_model_list_grouped_and_cached(app):
    def gw(req):
        auth = req.headers.get("authorization")
        if "bad.example" in str(req.url):
            return httpx.Response(401, json={"error": "bad key"})
        ids = {f"Bearer {GW}": ["glm-4.6", "qwen-vl"], f"Bearer {KEY_A}": ["models/gemini-3.5-flash", "gemini-3.5-pro"]}[auth]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    pv.TRANSPORT = httpx.MockTransport(gw)
    set_models(app, base_url="http://gw.lan/v1")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        add(c, name="Gemini", id="gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai", key=KEY_A)
        add(c, name="Broken", id="broken", base_url="https://bad.example/v1", key="x")
        add(c, name="Off", id="off", base_url="https://off.example/v1", key="y", enabled=False)
        r = c.get("/api/models", headers=H).json()
    assert r["models"] == ["glm-4.6", "qwen-vl"]   # unchanged for old clients: the default provider's list
    g = {x["provider"]: x for x in r["groups"]}
    assert [x["provider"] for x in r["groups"]] == ["default", "gemini", "broken"]   # disabled ones are skipped
    assert g["default"]["name"] == "默认" and g["gemini"]["name"] == "Gemini"
    assert g["gemini"]["models"] == ["gemini-3.5-flash", "gemini-3.5-pro"]   # "models/" prefix dropped
    assert g["broken"]["models"] == [] and "401" in g["broken"]["error"]
    store = app.state.store
    assert store.kv_get(pv.MODELS_KV)["gemini"] == ["gemini-3.5-flash", "gemini-3.5-pro"]
    # a bare model name only the Gemini provider offers resolves there; one the default also has stays default
    assert pv.provider_for_model(store, "gemini-3.5-pro") == "gemini" and pv.provider_for_model(store, "glm-4.6") == ""


def test_probe_uses_the_slots_provider_and_connection_test(app, monkeypatch):
    import aiw_api.probe as pr
    seen = []

    async def fake_probe(base, key, model, proxy=""):
        seen.append((base, key, model, proxy))
        return {"model": model, "tools": True, "vision": False, "notes": []}
    monkeypatch.setattr(pr, "probe", fake_probe)
    set_models(app, base_url="http://gw.lan/v1", vision_model="eye-m")
    pv.TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})
                                       if req.headers.get("authorization") == f"Bearer {KEY_B}" else httpx.Response(401, text="no"))
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        add(c, name="B", id="pb", base_url="https://b.example/v1", key=KEY_B, proxy="http://10.0.0.9:3128")
        c.patch("/api/settings/models", headers=H, json={"vision_model_provider": "pb"})
        r = c.post("/api/models/probe", headers=H, json={"model": "eye-m", "slot": "vision_model"}).json()
        assert r["provider"] == "pb" and seen[-1] == ("https://b.example/v1", KEY_B, "eye-m", "http://10.0.0.9:3128")
        c.post("/api/models/probe", headers=H, json={"model": "eye-m"})          # by name -> the slot holding it
        assert seen[-1][:2] == ("https://b.example/v1", KEY_B)
        c.post("/api/models/probe", headers=H, json={"model": "other", "provider": ""})   # explicit default
        assert seen[-1][:2] == ("http://gw.lan/v1", GW)
        c.post("/api/models/probe", headers=H, json={"model": "x", "provider": "pb"})
        assert seen[-1][1] == KEY_B
        t = c.post("/api/providers/pb/test", headers=H).json()
        assert t["ok"] and t["count"] == 2 and t["sample"] == ["m1", "m2"]
        bad = c.post("/api/providers/default/test", headers=H).json()
        assert bad["ok"] is False and "401" in bad["error"]
        # an unsaved provider is tested with its own form values and never borrows another key
        new = c.post("/api/providers/new/test", headers=H, json={"base_url": "https://n.example/v1"}).json()
        assert new == {"ok": False, "error": "没有 API Key"}
        ok = c.post("/api/providers/new/test", headers=H, json={"base_url": "https://n.example/v1", "key": KEY_B}).json()
        assert ok["ok"] is True
        assert c.post("/api/providers/nope/test", headers=H).status_code == 404


def test_delete_or_disable_provider_in_use(app):
    from fastapi.testclient import TestClient
    store = app.state.store
    with TestClient(app) as c:
        add(c, name="A", id="pa", base_url="https://a.example/v1", key=KEY_A)
        add(c, name="Spare", id="spare", base_url="https://s.example/v1")
        c.patch("/api/settings/models", headers=H, json={"model": "m", "model_provider": "pa", "omni_model_provider": "pa"})
        from aiw_api import autoroute
        autoroute.save(store, {"dev": {"model": "d", "provider": "pa"}})
        r = c.delete("/api/providers/pa", headers=H)
        assert r.status_code == 409
        msg = r.json()["detail"]
        assert "还在用" in msg and "model_provider" in msg and "omni_model_provider" in msg and "autoroute.dev.provider" in msg
        r = c.put("/api/providers/pa", headers=H, json={"enabled": False})
        assert r.status_code == 409 and "还在用" in r.json()["detail"]
        assert c.delete("/api/providers/default", headers=H).status_code == 400
        assert c.delete("/api/providers/spare", headers=H).json() == {"ok": True, "reset": []}
        r = c.delete("/api/providers/pa?force=1", headers=H).json()
        assert r["ok"] and set(r["reset"]) == {"model_provider", "omni_model_provider", "autoroute.dev.provider"}
        m = c.get("/api/settings/models", headers=H).json()
        assert m["model_provider"] is None and m["model"] == "m"
        assert [p["id"] for p in c.get("/api/providers", headers=H).json()["providers"]] == ["default"]
        assert app.state.secrets.get("provider_key_pa") is None
        assert autoroute.cfg(store)["dev"]["provider"] == ""
    # a stale provider id left in kv models (edited by hand) falls back to the default provider, never crashes
    set_models(app, model_provider="ghost")
    c2 = pv.slot_conn(store, app.state.secrets, "model")
    assert c2["id"] == "default" and c2["fell_back"] == "ghost" and c2["key"] == GW
    assert app.state.sessions._llm().providers[0].key == GW


def test_web_ui_wired():
    import pathlib
    import shutil
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    web = root / "apps" / "web"
    app_js = (web / "app.js").read_text(encoding="utf-8")
    for needle in ("/api/providers", "function providersHTML", "function editProvider", "测试连接", "provSelect(", "data-p=",
                   "select.psel", "provider: ps ? ps.value : undefined", "?force=1"):
        assert needle in app_js, needle
    ui = (web / "ui.js").read_text(encoding="utf-8")
    assert 'id="g-prov"' in ui and 'provSelect(r.key + "_provider"' in ui and "PROVIDER_ONLY_SLOTS" in ui
    if shutil.which("node"):
        for f in ("app.js", "ui.js"):
            r = subprocess.run(["node", "--check", str(web / f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
