"""云电脑地址不写死：InstaBox 兼容地址可配置；MCP 云电脑（如 https://mcp.box.simular.ai/mcp）加为 cloud MCP 服务器。"""
import json
import os

import httpx
import pytest

os.environ.setdefault("AIW_SECRET_KEY", "test-key")


def test_norm_base():
    from aiw_api.cloud import API, CloudError, norm_base
    assert norm_base("") == API
    assert norm_base("https://ib.example.com/") == "https://ib.example.com"
    with pytest.raises(CloudError):
        norm_base("ftp://x")


@pytest.mark.asyncio
async def test_add_with_custom_base(tmp_path):
    from aiw_api.cloud import CloudManager
    from aiw_api.crypto import SecretBox
    from aiw_api.store import Store
    seen = []

    def h(req):
        seen.append(str(req.url))
        if req.url.path == "/v1/me":
            return httpx.Response(200, json={"email": "a@b.c"})
        return httpx.Response(200, json={"stdout": "", "exitCode": 0})
    store = Store(str(tmp_path / "db.sqlite"))
    m = CloudManager(store, SecretBox(store, str(tmp_path)), "tok", transport=httpx.MockTransport(h))
    m.start_setup = lambda mid: None
    r = await m.add("my", "k_any", None, "https://ib.example.com")
    assert seen[0].startswith("https://ib.example.com/v1/me")
    assert m.entry(r["id"])["api_base"] == "https://ib.example.com"
    assert m.client(r["id"]).base == "https://ib.example.com"


@pytest.mark.asyncio
async def test_mcp_cloud_endpoints(tmp_path, monkeypatch):
    from aiw_api import integrations
    from aiw_api.config import Settings
    from aiw_api.main import build_app

    async def fake_list(self):
        assert self.url == "https://mcp.box.simular.ai/mcp"
        assert self.headers.get("Authorization") == "Bearer cb_x"
        return [{"name": "shell"}, {"name": "browser_navigate"}]
    monkeypatch.setattr(integrations.MCPClient, "list_tools", fake_list)
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = []
    app = build_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer api-secret"}) as c:
        r = await c.post("/api/cloud", json={"provider": "mcp", "name": "box", "api_base": "https://mcp.box.simular.ai/mcp", "api_key": "cb_x"})
        assert r.status_code == 200, r.text
        assert r.json()["tools"] == 2 and "headers" not in r.json()
        lst = (await c.get("/api/cloud/mcp")).json()
        assert lst[0]["url"] == "https://mcp.box.simular.ai/mcp"
        integ = (await c.get("/api/integrations")).json()["mcp_servers"]
        assert integ[0]["headers"]["Authorization"] == "Bearer {{secret:mcpcloud_box}}"
        bad = await c.post("/api/cloud", json={"provider": "mcp", "api_base": "nope"})
        assert bad.status_code == 400
        assert (await c.delete("/api/cloud/mcp/box")).status_code == 200
        assert (await c.get("/api/cloud/mcp")).json() == []


@pytest.mark.asyncio
async def test_mcp_desktop_view_and_act(tmp_path, monkeypatch):
    import base64, io
    from PIL import Image
    from aiw_api import integrations
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    buf = io.BytesIO(); Image.new("RGB", (1280, 800)).save(buf, "WEBP")
    webp = base64.b64encode(buf.getvalue()).decode()
    calls = []

    async def fake_list(self):
        return [{"name": "screenshot"}, {"name": "computer_act"}]

    async def fake_rpc(self, c, method, params=None, notify=False):
        if method == "tools/call":
            calls.append(params)
            if params["name"] == "box_view":
                return {"content": [{"type": "text", "text": '{"view_url":"https://box.example/live"}'}]}
            return {"content": [{"type": "text", "text": "ok"}, {"type": "image", "data": webp, "mimeType": "image/webp"}]}
        return {}
    monkeypatch.setattr(integrations.MCPClient, "list_tools", fake_list)
    monkeypatch.setattr(integrations.MCPClient, "_rpc", fake_rpc)
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = []
    app = build_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer api-secret"}) as c:
        assert (await c.post("/api/cloud", json={"provider": "mcp", "name": "box", "api_base": "https://m.example/mcp", "api_key": "k"})).status_code == 200
        r = (await c.get("/api/cloud/mcp/box/screen")).json()
        assert r["mime"] == "image/png" and Image.open(io.BytesIO(base64.b64decode(r["image"]))).size == (1280, 800)
        assert calls[-1] == {"name": "screenshot", "arguments": {"max_edge": 0}}
        assert (await c.post("/api/cloud/mcp/box/act", json={"action": "click", "x": 10, "y": 20, "evil": 1})).status_code == 200
        assert calls[-1]["arguments"] == {"action": "click", "x": 10, "y": 20}
        assert (await c.post("/api/cloud/mcp/box/act", json={"action": "type_from_file"})).status_code == 400
        assert (await c.get("/api/cloud/mcp/box/view")).json()["view_url"] == "https://box.example/live"
        assert (await c.get("/api/cloud/mcp/nope/screen")).status_code == 404
