"""v3.9.41: Adobe 创意工具 (Adobe for Creativity, official hosted MCP) as a connector preset.

Mocked Adobe IMS + MCP server through httpx.MockTransport (oauth.core.TRANSPORT): metadata, DCR (confidential client,
scopes, one registration per redirect URI), authorize URL (PKCE S256, resource, llm.* scopes), token exchange
(client_secret_post, resource fallback), invalid_scope fallback (IMS redirects without state), refresh (access_denied =
re-auth), 403-without-challenge, tools/list into the agent's toolset (naming, gate, allowlist, annotations) and tools/call
with image / resource / link results that become task files on the card and on the computer."""
import asyncio
import base64
import io
import json
import pathlib
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from aiw_api.oauth import core

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP = "https://adobe-creativity.adobe.io/mcp"
IMS = "https://ims-na1.adobelogin.com"
ALL_SCOPES = ["openid", "AdobeID", "offline_access", "llm.assets.manage", "llm.assets.upload"]


def png_bytes(color=(255, 0, 0)) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(b, "PNG")
    return b.getvalue()


class FakeAdobe:
    def __init__(self):
        self.mcp_reachable = True
        self.registered, self.token_calls, self.mcp_calls, self.authorize_seen = [], [], [], []
        self.codes = {}
        self.valid = {"AT1", "AT2", "AT3"}
        self.forbid_once, self.forbid_always = set(), False
        self.refuse_resource = False
        self.refresh_error = None
        self.rt = "RT1"
        self.tools = [
            {"name": "photoshop_remove_background", "description": "Remove the background of an image",
             "inputSchema": {"type": "object", "properties": {"image_url": {"type": "string"}}}},
            {"name": "firefly_generate_image", "description": "Generate an image with Firefly",
             "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
             "annotations": {"readOnlyHint": False}},
            {"name": "stock_search", "description": "Search Adobe Stock", "inputSchema": {"type": "object"},
             "annotations": {"readOnlyHint": True}},
            {"name": "assets_delete", "description": "Delete a cloud asset", "inputSchema": {"type": "object"},
             "annotations": {"destructiveHint": True}},
        ]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.startswith(MCP) and req.method == "POST":
            if not self.mcp_reachable:
                raise httpx.ConnectError("blocked")
            auth = req.headers.get("authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else ""
            msg = json.loads(req.content)
            self.mcp_calls.append((tok, msg.get("method")))
            if tok not in self.valid or tok in self.forbid_once or self.forbid_always:   # "" valid = guest tier
                self.forbid_once.discard(tok)
                return httpx.Response(403, text="")          # Adobe: 403, no WWW-Authenticate
            if "id" not in msg:
                return httpx.Response(202)
            m = msg["method"]
            if m == "initialize":
                res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
            elif m == "tools/list":
                res = {"tools": self.tools}
            else:
                name = msg["params"]["name"]
                if name == "firefly_generate_image":
                    res = {"content": [
                        {"type": "text", "text": "Generated 1 image: https://assets.adobe.fake/out/cat.png?sig=abc"},
                        {"type": "image", "data": base64.b64encode(png_bytes()).decode(), "mimeType": "image/png"},
                        {"type": "resource_link", "uri": "https://assets.adobe.fake/out/cat.psd", "name": "cat.psd",
                         "mimeType": "image/vnd.adobe.photoshop"},
                        {"type": "resource", "resource": {"uri": "adobe://asset/cutout.png", "mimeType": "image/png",
                                                          "blob": base64.b64encode(png_bytes((0, 255, 0))).decode()}}]}
                else:
                    res = {"content": [{"type": "text", "text": f"called {name} as {tok}"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": res},
                                  headers={"mcp-session-id": "s1"})
        if u.startswith("https://assets.adobe.fake/out/cat.png"):
            return httpx.Response(200, content=png_bytes((0, 0, 255)), headers={"content-type": "image/png"})
        if u == "https://assets.adobe.fake/out/cat.psd":
            return httpx.Response(200, content=b"8BPS" + b"\0" * 100, headers={"content-type": "image/vnd.adobe.photoshop"})
        if u.startswith("https://adobe-creativity.adobe.io/.well-known/oauth-protected-resource"):
            if not self.mcp_reachable:
                raise httpx.ConnectError("blocked")
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [IMS],
                                             "mcp_protocol_version": "2025-06-18", "scopes_supported": ALL_SCOPES})
        if u == IMS + "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "issuer": IMS, "authorization_endpoint": IMS + "/ims/authorize/v2", "token_endpoint": IMS + "/ims/token/v3",
                "registration_endpoint": IMS + "/ims/register", "revocation_endpoint": IMS + "/ims/revoke",
                "scopes_supported": ["openid", "email", "profile"], "code_challenge_methods_supported": ["S256", "plain"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]})
        if u == IMS + "/ims/register":
            body = json.loads(req.content)
            self.registered.append(body)
            n = len(self.registered)
            return httpx.Response(201, json={"client_id": f"adobe-{n}", "client_secret": f"sec-{n}",
                                             "client_secret_expires_at": 0, "client_name": f"adobe-{n}",
                                             "redirect_uris": body["redirect_uris"],
                                             "token_endpoint_auth_method": body["token_endpoint_auth_method"],
                                             "scope": body.get("scope") or "AdobeID offline_access openid"})
        if u == IMS + "/ims/token/v3":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.token_calls.append(form)
            reg = next((r for i, r in enumerate(self.registered) if f"adobe-{i + 1}" == form.get("client_id")), None)
            if not reg or form.get("client_secret") != "sec-" + form["client_id"].split("-")[1]:
                return httpx.Response(400, json={"error": "invalid_client", "error_description": "invalid client_secret parameter"})
            if self.refuse_resource and form.get("resource"):
                return httpx.Response(400, json={"error": "invalid_target", "error_description": "resource not supported"})
            if form["grant_type"] == "authorization_code":
                ch = self.codes.get(form["code"])
                if not ch or core.pkce_challenge(form["code_verifier"]) != ch:
                    return httpx.Response(400, json={"error": "access_denied"})
                return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 86399,
                                                 "token_type": "bearer", "scope": " ".join(ALL_SCOPES)})
            if form["grant_type"] == "refresh_token":
                if self.refresh_error:
                    return httpx.Response(400, json={"error": self.refresh_error})
                if form["refresh_token"] != self.rt:
                    return httpx.Response(400, json={"error": "access_denied"})
                n = sum(1 for c in self.token_calls if c["grant_type"] == "refresh_token")
                self.rt = f"RT{n + 1}"
                return httpx.Response(200, json={"access_token": "AT2" if n == 1 else "AT3", "refresh_token": self.rt,
                                                 "expires_in": 86399})
        if u.startswith(IMS + "/ims/revoke"):
            return httpx.Response(200)
        return httpx.Response(404, text="no route " + u)

    def consent(self, authorize_url: str) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        self.authorize_seen.append(q)
        code = "code-" + q["state"][:6]
        self.codes[code] = q["code_challenge"]
        return q["state"], code


@pytest.fixture
def fake(monkeypatch):
    f = FakeAdobe()
    monkeypatch.setattr(core, "TRANSPORT", httpx.MockTransport(f))
    from aiw_api import integrations
    integrations._MCP_CACHE.clear()
    return f


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("AIW_PUBLIC_URL", raising=False)
    monkeypatch.setenv("AIW_PORT", "8000")
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    return build_app(s)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": "Bearer tok"})


def q_of(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def connect(app, fake, **body):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "adobe", **body})).json()
        assert r["status"] == "authorize", r
        state, code = fake.consent(r["authorize_url"])
        cb = await c.get("/api/oauth/callback", params={"state": state, "code": code})
        assert cb.status_code == 200 and "已连接" in cb.text, cb.text
    return r


# ------------------------------------------------------------------ catalog / UI
def test_preset_in_catalog_and_ui():
    from aiw_api.oauth.providers import CATALOG, MCP_PRESETS
    cat = CATALOG["adobe"]
    assert cat["method"] == "mcp_oauth" and cat["mcp_url"] == MCP and cat["name"] == "Adobe 创意工具"
    pre = MCP_PRESETS["adobe"]
    assert pre["skip_probe"] and pre["dcr_auth"] == "client_secret_post" and pre["scopes"] == ALL_SCOPES
    assert not any(k in pre for k in ("client_id", "client_secret"))   # nothing registered is hard-coded
    assert '"client_secret":' not in json.dumps(pre)
    js = (ROOT / "apps/web/ui.js").read_text()
    assert 'id: "adobe", name: "Adobe 创意工具"' in js and "connPresetForm" in js and "conn_proxy" in js


# ------------------------------------------------------------------ authorize: no probe, DCR, scopes, PKCE, resource
async def test_start_goes_straight_to_oauth_with_dcr(app, fake):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "adobe"})).json()
        assert r["status"] == "authorize", r
        assert not fake.mcp_calls                                   # no unauthenticated initialize (Adobe: 403)
        reg = fake.registered[0]
        assert reg["token_endpoint_auth_method"] == "client_secret_post"
        assert reg["redirect_uris"] == ["http://127.0.0.1:8000/api/oauth/callback"]
        assert reg["scope"].split() == ALL_SCOPES                   # IMS only grants llm.* when registered with them
        assert set(reg["grant_types"]) == {"authorization_code", "refresh_token"}
        q = q_of(r["authorize_url"])
        assert r["authorize_url"].startswith(IMS + "/ims/authorize/v2?")
        assert q["client_id"] == "adobe-1" and q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
        assert q["scope"].split() == ALL_SCOPES and q["resource"] == MCP
        assert q["redirect_uri"] == "http://127.0.0.1:8000/api/oauth/callback"
        lst = (await c.get("/api/connections")).json()
        assert lst["catalog"]["adobe"]["preset"] == "adobe" and "llm.assets.manage" in lst["catalog"]["adobe"]["scopes"]
        assert "sec-1" not in json.dumps(lst) and "sec-1" not in json.dumps(app.state.store.kv_get("oauth_states"))


async def test_discovery_unreachable_uses_builtin_ims(app, fake):
    fake.mcp_reachable = False                                       # adobe.io blocked from this network
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "adobe"})).json()
    assert r["status"] == "authorize" and r["authorize_url"].startswith(IMS + "/ims/authorize/v2?")
    assert q_of(r["authorize_url"])["resource"] == MCP
    assert "内置" in app.state.connections.get("adobe")["status_msg"]


async def test_dcr_cached_per_redirect_uri(app, fake, monkeypatch):
    async with client(app) as c:
        r1 = (await c.post("/api/connections", json={"connector": "adobe"})).json()
        r1b = (await c.post("/api/connections", json={"connector": "adobe"})).json()
        assert len(fake.registered) == 1 and q_of(r1b["authorize_url"])["client_id"] == "adobe-1"
        await c.put("/api/oauth/settings", json={"public_url": "http://192.168.50.20:8000"})
        r2 = (await c.post("/api/connections", json={"connector": "adobe"})).json()
        assert len(fake.registered) == 2 and q_of(r2["authorize_url"])["client_id"] == "adobe-2"
        assert fake.registered[1]["redirect_uris"] == ["http://192.168.50.20:8000/api/oauth/callback"]
        await c.put("/api/oauth/settings", json={"public_url": ""})
        r3 = (await c.post("/api/connections", json={"connector": "adobe"})).json()
        assert len(fake.registered) == 2 and q_of(r3["authorize_url"])["client_id"] == "adobe-1"
        # finishing the LAN login makes the LAN registration the connection's client
        state, code = fake.consent(r2["authorize_url"])
        assert (await c.get("/api/oauth/callback", params={"state": state, "code": code})).status_code == 200
    assert app.state.secrets.get_json("oauth_client_adobe")["client_id"] == "adobe-2"
    assert fake.token_calls[-1]["redirect_uri"] == "http://192.168.50.20:8000/api/oauth/callback"


# ------------------------------------------------------------------ token exchange / resource fallback / scopes
async def test_callback_exchanges_with_client_secret_post(app, fake):
    await connect(app, fake)
    form = fake.token_calls[0]
    assert form["grant_type"] == "authorization_code" and form["client_id"] == "adobe-1" and form["client_secret"] == "sec-1"
    assert form["resource"] == MCP and form["code_verifier"]
    rec = app.state.connections.get("adobe")
    assert rec["status"] == "ok" and rec["resource_token"] is True and "llm.assets.manage" in rec["scope"]
    assert "AT1" not in json.dumps(app.state.store.kv_get("connections"))


async def test_token_endpoint_refusing_resource_falls_back(app, fake):
    fake.refuse_resource = True
    await connect(app, fake)
    assert [bool(t.get("resource")) for t in fake.token_calls] == [True, False]
    rec = app.state.connections.get("adobe")
    assert rec["status"] == "ok" and rec["resource_token"] is False
    tok = app.state.secrets.get_json("oauth_tok_adobe")
    tok["expires_at"] = time.time() + 5
    app.state.secrets.put_json("oauth_tok_adobe", tok)
    assert await app.state.connections.access_token("adobe") == "AT2"
    assert "resource" not in fake.token_calls[-1]                 # learned: refresh without it straight away


async def test_resource_mode_off(app, fake):
    r = await connect(app, fake, resource_mode="off")
    assert "resource" not in q_of(r["authorize_url"]) and "resource" not in fake.token_calls[0]


async def test_invalid_scope_without_state_retries_with_base_scopes(app, fake):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "adobe"})).json()
        assert "llm.assets.upload" in q_of(r["authorize_url"])["scope"]
        # IMS: 302 http://127.0.0.1:8000/api/oauth/callback?error=invalid_scope  (no state; seen live 2026-11-11)
        cb = await c.get("/api/oauth/callback", params={"error": "invalid_scope"}, follow_redirects=False)
        assert cb.status_code == 302 and cb.headers["location"].startswith(IMS + "/ims/authorize/v2?")
        q = q_of(cb.headers["location"])
        assert q["scope"].split() == ["openid", "AdobeID", "offline_access"]
        assert len(fake.registered) == 1                           # same registration (it has a superset)
        state, code = fake.consent(cb.headers["location"])
        assert (await c.get("/api/oauth/callback", params={"state": state, "code": code})).status_code == 200
        # a second invalid_scope after the fallback is an error page, not a loop
        r2 = (await c.get("/api/connections/adobe/start")).json()
        st2 = q_of(r2["authorize_url"])["state"]
        bad = await c.get("/api/oauth/callback", params={"state": st2, "error": "invalid_scope"}, follow_redirects=False)
        assert bad.status_code == 400
    rec = app.state.connections.get("adobe")
    assert rec["scopes"] == ["openid", "AdobeID", "offline_access"] and "基础权限" in rec["scope_note"]


async def test_custom_scopes_reregister(app, fake):
    async with client(app) as c:
        await c.post("/api/connections", json={"connector": "adobe", "scopes": "openid AdobeID"})
        assert fake.registered[0]["scope"] == "openid AdobeID"
        r = (await c.post("/api/connections", json={"connector": "adobe", "scopes": ALL_SCOPES})).json()
        assert len(fake.registered) == 2 and q_of(r["authorize_url"])["client_id"] == "adobe-2"


# ------------------------------------------------------------------ refresh / re-auth
async def test_refresh_and_access_denied_means_reauth(app, fake):
    await connect(app, fake)
    conns = app.state.connections
    tok = app.state.secrets.get_json("oauth_tok_adobe")
    tok["expires_at"] = time.time() + 10
    app.state.secrets.put_json("oauth_tok_adobe", tok)
    res = await asyncio.gather(*(conns.access_token("adobe") for _ in range(5)))
    assert set(res) == {"AT2"}
    rf = [t for t in fake.token_calls if t["grant_type"] == "refresh_token"]
    assert len(rf) == 1 and rf[0]["client_secret"] == "sec-1" and rf[0]["resource"] == MCP
    tok = app.state.secrets.get_json("oauth_tok_adobe")
    tok["expires_at"] = time.time() + 10
    app.state.secrets.put_json("oauth_tok_adobe", tok)
    fake.refresh_error = "access_denied"                            # IMS's answer to a dead refresh token
    with pytest.raises(core.ReauthRequired):
        await conns.access_token("adobe")
    assert conns.get("adobe")["status"] == "needs_reauth"


# ------------------------------------------------------------------ tools into the agent's toolset
async def test_tools_listed_named_gated_and_confirmed(app, fake):
    await connect(app, fake)
    from aiw_api.integrations import gate_tools, load_tools
    tools = await load_tools(app.state.store, app.state.secrets)
    names = sorted(n for n in tools if n.startswith("mcp_adobe_"))
    assert names == ["mcp_adobe_assets_delete", "mcp_adobe_firefly_generate_image", "mcp_adobe_photoshop_remove_background",
                     "mcp_adobe_stock_search"]
    sch, fn = tools["mcp_adobe_stock_search"]
    assert sch["description"].startswith("[Adobe 创意工具]")
    assert fn.needs_confirm({}) is False                           # readOnlyHint
    assert tools["mcp_adobe_assets_delete"][1].needs_confirm({}) is True   # destructiveHint
    assert fn.save_dir == "connectors/adobe"
    # gate: only creative requests carry the Adobe tools
    assert not any(n.startswith("mcp_adobe_") for n in gate_tools(tools, "查一下明天上海的天气"))
    for p in ("帮我把这张照片抠图换成白底", "用PS修一下", "Generate a poster with Firefly", "做个小红书封面"):
        assert "mcp_adobe_firefly_generate_image" in gate_tools(tools, p), p
    # expose=always, allowlist (PATCH, no re-auth)
    async with client(app) as c:
        pr = (await c.patch("/api/connections/adobe", json={"expose": "always", "allow_tools": "photoshop_*, stock_search",
                                                             "proxy": "socks5://127.0.0.1:1080"})).json()
        assert pr["expose"] == "always" and pr["proxy"].startswith("socks5://")
        assert (await c.patch("/api/connections/adobe", json={"proxy": "ftp://x"})).status_code == 400
        await c.patch("/api/connections/adobe", json={"proxy": ""})
        t = (await c.post("/api/connections/adobe/test")).json()
        assert t["ok"] and sorted(x for x in t["tools"] if x.startswith("mcp_adobe_")) == \
            ["mcp_adobe_photoshop_remove_background", "mcp_adobe_stock_search"]
    tools = await load_tools(app.state.store, app.state.secrets)
    assert "mcp_adobe_stock_search" in gate_tools(tools, "查天气")


async def test_403_refreshes_once_then_forbidden(app, fake):
    await connect(app, fake)
    from aiw_api.integrations import load_tools
    tools = await load_tools(app.state.store, app.state.secrets)
    fake.forbid_once.add("AT1")                                      # token lapsed early: bare 403
    out = await tools["mcp_adobe_stock_search"][1]({})
    assert out["text"] == "called stock_search as AT2" and not out["is_error"]
    assert sum(1 for t in fake.token_calls if t["grant_type"] == "refresh_token") == 1
    fake.forbid_always = True                                        # still 403 with a fresh token: not a re-auth
    out = await tools["mcp_adobe_stock_search"][1]({})
    assert out["is_error"] and out.get("forbidden") and "403" in out["text"]
    assert app.state.connections.get("adobe")["status"] == "ok"


async def test_call_results_become_files(app, fake):
    await connect(app, fake)
    from aiw_api.integrations import load_tools
    tools = await load_tools(app.state.store, app.state.secrets)
    out = await tools["mcp_adobe_firefly_generate_image"][1]({"prompt": "a cat"})
    files = out["files"]
    names = [f["name"] for f in files]
    assert len(files) == 4, names                                   # image block, resource blob, psd link, png url
    assert any(n.endswith(".psd") for n in names) and "cutout.png" in names and "cat.png" in names
    assert len(set(names)) == 4
    assert all(f["content_b64"] and f["size"] > 0 for f in files)
    assert out["image_b64"] and out["image_mime"] == "image/png" and out["image_note"] == "result"
    assert "生成/返回的文件" in out["text"] and len(out["text"]) < 6500
    assert "content_b64" not in out["text"]


async def test_guest_mode_only_when_server_allows(app, fake):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "adobe", "guest": True})).json()
        assert r["status"] == "error" and "免登录" in r["message"]      # our fake answers 403 like the real one
    fake.valid.add("")
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "adobe", "guest": True})).json()
    assert r["status"] == "ok" and app.state.connections.get("adobe")["auth"] == "none"


async def test_disconnect_revokes_and_forgets_registrations(app, fake):
    await connect(app, fake)
    async with client(app) as c:
        assert (await c.delete("/api/connections/adobe")).status_code == 200
    for n in ("oauth_tok_adobe", "oauth_client_adobe", "oauth_dcr_adobe"):
        assert app.state.secrets.get(n) is None


# ------------------------------------------------------------------ proxy
def test_proxy_helpers(monkeypatch):
    assert core.valid_proxy("") and core.valid_proxy("http://192.168.50.1:7890") and core.valid_proxy("socks5://h:1080")
    assert not core.valid_proxy("ftp://x") and not core.valid_proxy("192.168.1.1:7890")
    monkeypatch.setattr(core, "TRANSPORT", None)
    c = core.http(proxy="http://127.0.0.1:7890")
    assert c._mounts, "proxy mounted"
    st = SimpleNamespace(kv_get=lambda k, d=None: {"proxy": "http://global:1"})
    assert core.proxy_for(st, {"proxy": "http://own:2"}) == "http://own:2"
    assert core.proxy_for(st, {}) == "http://global:1"


async def test_oauth_settings_proxy(app, fake):
    async with client(app) as c:
        s = (await c.put("/api/oauth/settings", json={"proxy": "http://192.168.50.1:7890"})).json()
        assert s["proxy"] == "http://192.168.50.1:7890"
        assert (await c.put("/api/oauth/settings", json={"proxy": "nope"})).status_code == 400


# ------------------------------------------------------------------ agent loop: files to the task card + computer
async def test_loop_ext_files_to_card_and_computer(tmp_path):
    from agent_runtime.loop import AgentLoop
    delivered, uploaded = [], []

    async def deliver(res):
        delivered.append(res["path"])

    class Comp:
        async def execute(self, action, payload, **kw):
            uploaded.append((action, payload["path"], len(payload["content_b64"])))
            return SimpleNamespace(success=True, error=None)

    fake_self = SimpleNamespace(_deliver_file=deliver, computer=Comp())
    b64 = base64.b64encode(png_bytes()).decode()
    res = {"text": "ok", "files": [{"name": "cat.png", "mime": "image/png", "size": 70, "content_b64": b64},
                                    {"name": "../../etc/x.psd", "mime": "", "size": 4, "content_b64": "OEJQUw=="}]}
    await AgentLoop._ext_files(fake_self, res, "connectors/adobe")
    assert "files" not in res and delivered == ["cat.png", "x.psd"]
    assert [u[1] for u in uploaded] == ["connectors/adobe/cat.png", "connectors/adobe/x.psd"]
    assert "~/workspace/connectors/adobe/cat.png" in res["text"]


def test_loop_wires_ext_files_and_result_image_note():
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "await self._ext_files(res" in src and "_ext_image_note" in src and "NOT the screen" in src
    sess = (ROOT / "services/api/aiw_api/sessions.py").read_text()
    assert "gate_tools(extra, task.prompt" in sess
