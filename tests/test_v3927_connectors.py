"""v3.9.27: direct connectors — OAuth core (PKCE, state, discovery, DCR, refresh lock, 401 retry, callback),
SecretBox AES-256-GCM + backward compat, OpenAPI import, relay migration / removal. Outbound HTTP goes to a fake
authorization server + MCP server through httpx.MockTransport (oauth.core.TRANSPORT)."""
import asyncio
import base64
import json
import pathlib
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from aiw_api.oauth import core

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP = "https://mcp.fake/mcp"


class FakeAuth:
    """Authorization server + protected MCP server in one handler."""

    def __init__(self, dcr=True, issuer="https://auth.fake"):
        self.dcr, self.issuer = dcr, issuer
        self.codes = {}          # code -> challenge
        self.token_calls = []    # grant types seen
        self.valid = {"AT1", "AT2", "AT3"}
        self.reject_once = set()
        self.registered = []
        self.rt = "RT1"
        self.mcp_auth_seen = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.startswith(MCP) and req.method == "POST":
            auth = req.headers.get("authorization", "")
            self.mcp_auth_seen.append(auth)
            tok = auth[7:] if auth.startswith("Bearer ") else ""
            if tok in self.reject_once:
                self.reject_once.discard(tok)
                return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
            if tok not in self.valid:
                return httpx.Response(401, headers={"WWW-Authenticate":
                    'Bearer resource_metadata="https://mcp.fake/.well-known/oauth-protected-resource/mcp", scope="read write"'})
            msg = json.loads(req.content)
            if "id" not in msg:
                return httpx.Response(202)
            if msg["method"] == "initialize":
                res = {"protocolVersion": "2025-06-18", "capabilities": {}}
            elif msg["method"] == "tools/list":
                res = {"tools": [{"name": "search", "description": "find", "inputSchema": {"type": "object"}},
                                 {"name": "create_issue", "description": "new", "inputSchema": {"type": "object"}}]}
            else:
                res = {"content": [{"type": "text", "text": f"called {msg['params']['name']} as {tok}"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": res},
                                  headers={"mcp-session-id": "s1"})
        if u == "https://mcp.fake/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [self.issuer]})
        if u == self.issuer + "/.well-known/oauth-authorization-server":
            meta = {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                    "token_endpoint": self.issuer + "/token", "revocation_endpoint": self.issuer + "/revoke",
                    "scopes_supported": ["read", "write", "offline_access"], "code_challenge_methods_supported": ["S256"]}
            if self.dcr:
                meta["registration_endpoint"] = self.issuer + "/register"
            return httpx.Response(200, json=meta)
        if u == self.issuer + "/register":
            body = json.loads(req.content)
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": f"dyn-{len(self.registered)}", **body})
        if u == self.issuer + "/token":
            form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            self.token_calls.append(form)
            if form["grant_type"] == "authorization_code":
                ch = self.codes.get(form["code"])
                if not ch or core.pkce_challenge(form["code_verifier"]) != ch or form.get("resource") != MCP:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
                                                 "token_type": "Bearer", "scope": "read write"})
            if form["grant_type"] == "refresh_token":
                if form["refresh_token"] != self.rt:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                n = sum(1 for c in self.token_calls if c["grant_type"] == "refresh_token")
                self.rt = f"RT{n + 1}"
                return httpx.Response(200, json={"access_token": "AT2" if n == 1 else "AT3", "refresh_token": self.rt,
                                                 "expires_in": 3600})
        if u.startswith(self.issuer + "/revoke"):
            return httpx.Response(200)
        if "/.well-known/" in u:
            return httpx.Response(404)
        return httpx.Response(404, text="no route " + u)

    def consent(self, authorize_url: str) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        code = "code-" + q["state"][:6]
        self.codes[code] = q["code_challenge"]
        return q["state"], code


@pytest.fixture
def fake(monkeypatch):
    f = FakeAuth()
    monkeypatch.setattr(core, "TRANSPORT", httpx.MockTransport(f))
    return f


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("AIW_PUBLIC_URL", raising=False)
    monkeypatch.setenv("AIW_PORT", "8123")
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    return build_app(s)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"Authorization": "Bearer tok"})


# ------------------------------------------------------------------ PKCE / state / parsing
def test_pkce_s256_and_state_single_use(tmp_path):
    v, c = core.pkce_pair()
    assert 43 <= len(v) <= 128 and c == base64.urlsafe_b64encode(__import__("hashlib").sha256(v.encode()).digest()).decode().rstrip("=")
    from aiw_api.store import Store
    st = core.StateStore(Store(str(tmp_path / "s.db")))
    s1 = st.create(cid="x", verifier=v)
    assert st.pop(s1)["verifier"] == v
    assert st.pop(s1) is None                       # single use
    assert st.pop("forged") is None
    short = core.StateStore(st.store, ttl=-1)
    assert short.pop(short.create(cid="y")) is None  # expired


def test_www_authenticate_and_resource():
    wa = core.parse_www_authenticate('Bearer realm="mcp", resource_metadata="https://x/.well-known/oauth-protected-resource", scope="a b"')
    assert wa["resource_metadata"].endswith("oauth-protected-resource") and wa["scope"] == "a b"
    assert core.canonical_resource("HTTPS://Mcp.Example.com/mcp/#f") == "https://mcp.example.com/mcp"
    url = core.authorize_url("https://a/authorize", "cid", "http://127.0.0.1:8000/api/oauth/callback", "st", "ch",
                             ["read"], "https://mcp/x", offline_ok=True)
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["resource"] == ["https://mcp/x"] and q["scope"] == ["read offline_access"]


def test_redirect_uri_loopback_and_public(tmp_path, monkeypatch):
    from aiw_api.store import Store
    s = Store(str(tmp_path / "r.db"))
    monkeypatch.delenv("AIW_PUBLIC_URL", raising=False)
    monkeypatch.setenv("AIW_PORT", "9001")
    assert core.redirect_uri(s) == "http://127.0.0.1:9001/api/oauth/callback"
    monkeypatch.setenv("AIW_PUBLIC_URL", "https://aiw.example.com/")
    assert core.redirect_uri(s) == "https://aiw.example.com/api/oauth/callback"
    s.kv_set("oauth_settings", {"public_url": "https://kv.example.com"})
    assert core.redirect_uri(s) == "https://kv.example.com/api/oauth/callback"


# ------------------------------------------------------------------ SecretBox
def test_secretbox_aes_gcm_and_legacy_fernet(tmp_path, monkeypatch):
    monkeypatch.delenv("AIW_SECRET_KEY", raising=False)
    from cryptography.fernet import Fernet
    from aiw_api.crypto import SecretBox
    from aiw_api.store import Store
    st = Store(str(tmp_path / "c.db"))
    box = SecretBox(st, str(tmp_path))
    box.put("k1", "hello-secret")
    raw = st.secret_get("k1")
    assert raw.startswith("v2:") and "hello" not in raw
    assert box.get("k1") == "hello-secret"
    # a record written by <= 3.9.26 (plain Fernet with the same key file) still reads
    legacy = Fernet((tmp_path / ".secret_key").read_bytes().strip()).encrypt(b"old-value").decode()
    st.secret_put("old", legacy)
    assert box.get("old") == "old-value"
    # name is bound as associated data: a record copied under another name does not decrypt; tampering fails
    st.secret_put("moved", raw)
    assert box.get("moved") is None
    st.secret_put("k2", raw[:-4] + ("AAAA" if not raw.endswith("AAAA") else "BBBB"))
    assert box.get("k2") is None
    box.put_json("oauth_tok_x", {"access_token": "a"})
    assert box.get_json("oauth_tok_x") == {"access_token": "a"}
    assert all(not s["name"].startswith("oauth_") for s in box.list())
    # a new process with the same key file reads both kinds
    box2 = SecretBox(st, str(tmp_path))
    assert box2.get("k1") == "hello-secret" and box2.get("old") == "old-value"


# ------------------------------------------------------------------ discovery + DCR + callback (end to end)
async def test_mcp_oauth_discovery_dcr_callback_and_tools(app, fake):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"type": "mcp", "url": MCP, "name": "Fake"})).json()
        assert r["status"] == "authorize", r
        assert r["redirect_uri"] == "http://127.0.0.1:8123/api/oauth/callback"
        q = {k: v[0] for k, v in parse_qs(urlparse(r["authorize_url"]).query).items()}
        assert r["authorize_url"].startswith("https://auth.fake/authorize?")
        assert q["client_id"] == "dyn-1" and q["code_challenge_method"] == "S256" and q["resource"] == MCP
        assert "offline_access" in q["scope"].split() and "read" in q["scope"].split()
        assert fake.registered[0]["token_endpoint_auth_method"] == "none"
        assert fake.registered[0]["application_type"] == "native"
        state, code = fake.consent(r["authorize_url"])
        cb = await c.get("/api/oauth/callback", params={"state": state, "code": code})   # no API token needed
        assert cb.status_code == 200 and "已连接" in cb.text
        again = await c.get("/api/oauth/callback", params={"state": state, "code": code})
        assert again.status_code == 400                                                   # state is single use
        lst = (await c.get("/api/connections")).json()
        conn = next(x for x in lst["connections"] if x["id"] == r["id"])
        assert conn["status"] == "ok" and conn["has_token"] and "access_token" not in json.dumps(lst)
        t = (await c.post(f"/api/connections/{r['id']}/test")).json()
        assert t["ok"] and any(n.endswith("_search") for n in t["tools"])
    from aiw_api.integrations import load_tools
    tools = await load_tools(app.state.store, app.state.secrets)
    name = next(n for n in tools if n.startswith("mcp_") and n.endswith("_search"))
    out = await tools[name][1]({})
    assert out["text"] == "called search as AT1"
    # the token itself is only in the vault, encrypted
    assert "AT1" not in json.dumps(app.state.store.kv_get("connections"))
    assert app.state.store.secret_get(f"oauth_tok_{r['id']}").startswith("v2:")


async def test_callback_bad_state_and_denied(app, fake):
    async with client(app) as c:
        assert (await c.get("/api/oauth/callback", params={"state": "nope", "code": "x"})).status_code == 400
        r = (await c.post("/api/connections", json={"type": "mcp", "url": MCP})).json()
        state, _ = fake.consent(r["authorize_url"])
        d = await c.get("/api/oauth/callback", params={"state": state, "error": "access_denied"})
        assert d.status_code == 400
        conn = (await c.get("/api/connections")).json()["connections"][0]
        assert conn["status"] == "error"


async def test_no_dcr_needs_client_then_user_client(app, fake):
    fake.dcr = False
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"type": "mcp", "url": MCP, "name": "nodcr"})).json()
        assert r["status"] == "needs_client"
        r2 = (await c.post("/api/connections", json={"type": "mcp", "url": MCP, "name": "nodcr",
                                                     "client_id": "mine", "client_secret": "s3c"})).json()
        assert r2["status"] == "authorize" and "client_id=mine" in r2["authorize_url"]
        assert "s3c" not in json.dumps(app.state.store.kv_get("oauth_states"))   # secret never in the state record


async def test_client_bound_to_issuer(app, fake):
    conns = app.state.connections
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"type": "mcp", "url": MCP, "name": "bind"})).json()
        assert "client_id=dyn-1" in r["authorize_url"]
        # the MCP server now points at another authorization server: the old registration is not sent there
        fake.issuer = "https://evil.fake"
        r2 = (await c.post("/api/connections", json={"type": "mcp", "url": MCP, "name": "bind"})).json()
        assert r2["authorize_url"].startswith("https://evil.fake/authorize") and "client_id=dyn-2" in r2["authorize_url"]
    assert app.state.secrets.get_json(f"oauth_client_{r['id']}")["issuer"] == "https://evil.fake"
    assert conns.get(r["id"])["issuer"] == "https://evil.fake"


# ------------------------------------------------------------------ refresh lock / 401 retry / re-auth
async def _connected(app, fake, name="Fake"):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"type": "mcp", "url": MCP, "name": name})).json()
        state, code = fake.consent(r["authorize_url"])
        assert (await c.get("/api/oauth/callback", params={"state": state, "code": code})).status_code == 200
    return r["id"]


async def test_concurrent_refresh_makes_one_request(app, fake):
    cid = await _connected(app, fake)
    conns = app.state.connections
    tok = app.state.secrets.get_json(f"oauth_tok_{cid}")
    tok["expires_at"] = time.time() + 10          # inside the 60 s skew -> refresh due
    app.state.secrets.put_json(f"oauth_tok_{cid}", tok)
    res = await asyncio.gather(*(conns.access_token(cid) for _ in range(8)))
    assert set(res) == {"AT2"}
    assert sum(1 for t in fake.token_calls if t["grant_type"] == "refresh_token") == 1
    assert app.state.secrets.get_json(f"oauth_tok_{cid}")["refresh_token"] == "RT2"   # rotated token kept


async def test_401_refreshes_once_and_retries(app, fake):
    cid = await _connected(app, fake)
    from aiw_api.integrations import MCPClient
    fake.reject_once.add("AT1")                      # server revoked the access token early
    cl = MCPClient(MCP, token=app.state.connections.token_provider(cid))
    tools = await cl.list_tools()
    assert [t["name"] for t in tools] == ["search", "create_issue"]
    assert fake.mcp_auth_seen[-1] == "Bearer AT2"
    assert sum(1 for t in fake.token_calls if t["grant_type"] == "refresh_token") == 1


async def test_invalid_grant_marks_needs_reauth(app, fake):
    cid = await _connected(app, fake)
    fake.valid = set()                               # every token rejected
    fake.rt = "something-else"                       # refresh -> invalid_grant
    from aiw_api.integrations import load_tools
    from aiw_api.integrations import _MCP_CACHE
    _MCP_CACHE.clear()
    tools = await load_tools(app.state.store, app.state.secrets)
    rec = app.state.connections.get(cid)
    assert rec["status"] == "needs_reauth"
    assert "connector_status" in tools
    st = await tools["connector_status"][1]({})
    assert st["connections"][0]["status"] == "needs_reauth" and "重新授权" in st["hint"]
    async with client(app) as c:
        r = (await c.get(f"/api/connections/{cid}/start")).json()
        assert r["status"] == "authorize"            # re-auth flow restarts


async def test_disconnect_revokes_and_deletes(app, fake):
    cid = await _connected(app, fake)
    async with client(app) as c:
        assert (await c.delete(f"/api/connections/{cid}")).status_code == 200
        assert (await c.get("/api/connections")).json()["connections"] == []
    assert app.state.secrets.get(f"oauth_tok_{cid}") is None


# ------------------------------------------------------------------ native provider config
async def test_native_google_needs_client_then_authorize(app, fake):
    async with client(app) as c:
        r = (await c.post("/api/connections", json={"connector": "gmail"})).json()
        assert r["status"] == "needs_client" and r["provider"] == "google"
        s = (await c.put("/api/oauth/settings", json={"public_url": "https://aiw.example.com",
                                                      "providers": {"google": {"client_id": "gid", "client_secret": "gsec"}}})).json()
        assert s["redirect_uri"] == "https://aiw.example.com/api/oauth/callback"
        assert s["providers"]["google"]["configured"] and "gsec" not in json.dumps(s)
        r = (await c.post("/api/connections", json={"connector": "gmail"})).json()
        q = {k: v[0] for k, v in parse_qs(urlparse(r["authorize_url"]).query).items()}
        assert q["client_id"] == "gid" and q["access_type"] == "offline" and q["redirect_uri"] == s["redirect_uri"]
        assert "gmail.readonly" in q["scope"] and "gmail.send" not in q["scope"]       # minimal scopes by default
        r2 = (await c.post("/api/connections", json={"connector": "gcal"})).json()
        q2 = parse_qs(urlparse(r2["authorize_url"]).query)["scope"][0]
        assert "calendar.events" in q2 and "gmail.readonly" in q2                        # one Google grant, union
        assert app.state.connections.get("google")["services"] == ["gmail", "gcal"]


# ------------------------------------------------------------------ OpenAPI import
async def test_openapi_yaml_ref_and_security(app):
    spec = """
openapi: 3.0.0
servers: [{url: "https://api.acme.test/v1"}]
components:
  securitySchemes:
    key: {type: apiKey, in: header, name: X-Acme-Key}
  parameters:
    Id: {name: id, in: path, required: true, schema: {type: string}}
  schemas:
    Note: {type: object, required: [text], properties: {text: {type: string}, tags: {type: array, items: {type: string}}}}
paths:
  /notes/{id}:
    parameters: [{$ref: "#/components/parameters/Id"}]
    get: {operationId: getNote, summary: Get a note}
  /notes:
    post:
      operationId: addNote
      requestBody: {content: {application/json: {schema: {$ref: "#/components/schemas/Note"}}}}
"""
    async with client(app) as c:
        r = (await c.post("/api/integrations/openapi", json={"spec": spec, "prefix": "acme", "api_key": "K-9"})).json()
        assert r["imported"] == 2 and r["security"][0]["type"] == "apiKey"
        tools = (await c.get("/api/integrations")).json()["http_tools"]
    get = next(t for t in tools if t["name"] == "acme_getNote")
    add = next(t for t in tools if t["name"] == "acme_addNote")
    assert get["params"]["id"]["required"] and get["headers"]["X-Acme-Key"] == "{{secret:openapi_acme}}"
    assert add["params"]["text"]["required"] and add["params"]["tags"]["type"] == "array"
    assert get["write"] is False and add["write"] is True
    from aiw_api.integrations import http_tool
    _s, fn = http_tool(add, app.state.secrets)
    assert fn.needs_confirm({}) is True
    assert app.state.secrets.get("openapi_acme") == "K-9"


# ------------------------------------------------------------------ relay migration / removal
def test_relay_migration(tmp_path, monkeypatch):
    monkeypatch.delenv("AIW_SECRET_KEY", raising=False)
    from aiw_api.crypto import SecretBox
    from aiw_api.oauth import migrate_relays
    from aiw_api.oauth.connections import MIGRATED_MSG
    from aiw_api.store import Store
    st = Store(str(tmp_path / "m.db"))
    box = SecretBox(st, str(tmp_path))
    box.put("conn_gmail", "https://mcp.zap" + "ier.com/api/mcp/s/abc/mcp")
    box.put("conn_notion", "https://backend.compo" + "sio.dev/v3/mcp/xyz")
    box.put("conn_feishu", "https://open.feishu.cn/mcp/abc")
    st.kv_set("mcp_servers", [
        {"name": "gmail", "url": "{{secret:conn_gmail}}", "headers": {}},
        {"name": "notion", "url": "{{secret:conn_notion}}", "headers": {}},
        {"name": "zap" + "ier", "url": "{{secret:conn_zap" + "ier}}", "headers": {}},
        {"name": "feishu", "url": "{{secret:conn_feishu}}", "headers": {}},
        {"name": "github", "url": "https://api.githubcopilot.com/mcp/", "headers": {"Authorization": "Bearer {{secret:conn_github}}"}},
        {"name": "mytool", "url": "https://my.example/mcp", "headers": {}},
    ])
    moved = migrate_relays(st, box)
    assert sorted(moved) == sorted(["gmail", "notion", "zap" + "ier"])
    assert [m["name"] for m in st.kv_get("mcp_servers")] == ["feishu", "github", "mytool"]
    conns = {c["id"]: c for c in st.kv_get("connections")}
    assert conns["google"]["status"] == "needs_reauth" and conns["google"]["status_msg"] == MIGRATED_MSG
    assert conns["notion"]["method"] == "mcp_oauth" and conns["notion"]["status_msg"] == MIGRATED_MSG
    assert migrate_relays(st, box) == []            # one time


def test_no_relay_references_left():
    pat = re.compile("zap" + "ier|compo" + "sio", re.I)
    allowed = {"services/api/aiw_api/oauth/providers.py", "services/api/aiw_api/oauth/connections.py"}
    hits = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT).as_posix()
        if (not p.is_file() or rel.startswith(("workspace/", ".git/", "tests/")) or "__pycache__" in rel
                or "/vendor/" in rel or p.suffix in (".png", ".jpg", ".ipa", ".zip", ".pyc", ".car")):
            continue
        try:
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        if pat.search(txt) and rel not in allowed:
            hits.append(rel)
    assert hits == []


def test_ui_connectors_match_catalog():
    from aiw_api.oauth.providers import CATALOG
    js = (ROOT / "apps/web/ui.js").read_text()
    block = js[js.index("const CONNECTORS = ["):js.index("];", js.index("const CONNECTORS = ["))]
    ids = re.findall(r'\{ id: "([a-z0-9]+)"', block)
    assert len(ids) >= 25 and set(ids) == set(CATALOG)
    for cid in ids:
        assert CATALOG[cid]["method"] in ("mcp_oauth", "native", "rest_oauth", "token", "rest_token", "mcp_url")
    assert "/api/oauth/settings" in js and "/api/connections" in js and "redirect_uri" in js
