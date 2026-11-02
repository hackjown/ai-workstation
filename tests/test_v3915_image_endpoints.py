"""v3.9.15: a second image endpoint (生图接口) with a default + fallback, generate_image, chat-side pictures."""
import asyncio
import base64
import io
import json
import types

import httpx
from PIL import Image

from agent_runtime import image_edit as IE
from aiw_api import chat as C
from aiw_api.sessions import image_cfg_from


class _Sec(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def _png(color=(255, 0, 0), size=(64, 48)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def _fake_httpx(monkeypatch, handler):
    real = httpx.AsyncClient

    def client(*a, **kw):
        kw.pop("follow_redirects", None)
        return real(transport=httpx.MockTransport(handler), follow_redirects=True, timeout=kw.get("timeout", 30))
    monkeypatch.setattr(IE, "httpx", types.SimpleNamespace(AsyncClient=client, HTTPError=httpx.HTTPError))


def test_cfg_order_default_and_fallback(monkeypatch):
    for k in ("AIW_IMAGE_MODEL", "OPENAI_API_KEY", "AIW_IMAGE2_MODEL", "AIW_IMAGE2_API_KEY", "AIW_IMAGE2_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    sec = _Sec(openai_api_key="gk", image2_api_key="ck")
    m = {"base_url": "http://gw/v1", "image_model": "gpt-image-1", "image2_base_url": "http://192.168.50.188:3500/v1/",
         "image2_model": "gpt-image-2.5"}
    cfg = image_cfg_from(m, sec)                       # unset default + custom filled in -> custom first
    assert [p["id"] for p in cfg["chain"]] == ["custom", "gateway"]
    assert cfg["model"] == "gpt-image-2.5" and cfg["chain"][0]["base_url"] == "http://192.168.50.188:3500/v1"
    cfg = image_cfg_from({**m, "image_default": "gateway"}, sec)
    assert [p["id"] for p in cfg["chain"]] == ["gateway", "custom"] and cfg["key"] == "gk"
    cfg = image_cfg_from({**m, "image_model": None, "image_default": "gateway"}, sec)   # default not set up -> the other
    assert [p["id"] for p in cfg["chain"]] == ["custom"]
    assert image_cfg_from({"base_url": "http://gw/v1"}, _Sec()) is None


def test_generate_falls_back_to_second_endpoint(monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.url.host, req.url.path))
        if req.url.host == "bad":
            return httpx.Response(503, text="upstream down")
        body = json.loads(req.content)
        assert body["model"] == "gpt-image-1" and body["size"] == "1536x1024"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})
    _fake_httpx(monkeypatch, handler)
    cfg = {"chain": [{"id": "custom", "name": "自定义生图接口", "base_url": "http://bad/v1", "key": "k", "model": "gpt-image-2.5"},
                     {"id": "gateway", "name": "主网关", "base_url": "http://good/v1", "key": "k", "model": "gpt-image-1"}]}
    outs, used = asyncio.run(IE.generate_chain(cfg, "一只猫", IE.gen_size("横版"), 1, 10))
    assert used["id"] == "gateway" and Image.open(io.BytesIO(outs[0])).size == (64, 48)
    assert ("bad", "/v1/images/generations") in calls and ("good", "/v1/images/generations") in calls


def test_edit_retries_with_plain_image_field(monkeypatch):
    seen = []

    def handler(req: httpx.Request):
        raw = req.content.decode("latin-1")
        seen.append('name="image[]"' in raw)
        if 'name="image[]"' in raw:
            return httpx.Response(400, text="missing required field image")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png((0, 0, 255))).decode()}]})
    _fake_httpx(monkeypatch, handler)
    cfg = {"chain": [{"id": "custom", "name": "x", "base_url": "http://gen/v1", "key": "k", "model": "gpt-image-2.5"}]}
    out, used = asyncio.run(IE.edit_chain(cfg, _png(), "换成蓝色", 10))
    assert used["id"] == "custom" and seen[-1] is False and True in seen
    assert Image.open(io.BytesIO(out)).getpixel((1, 1))[:3] == (0, 0, 255)


def test_gen_size():
    assert IE.gen_size("1536*1024") == "1536x1024" and IE.gen_size("竖版") == "1024x1536"
    assert IE.gen_size("") is None and IE.gen_size("auto") is None and IE.gen_size("1:1") == "1024x1024"


def test_chat_routes_pictures_to_itself():
    assert C.wants_image_gen("帮我画一张赛博朋克风格的猫咪海报")
    assert C.wants_image_gen("生成一个 logo，蓝色极简")
    assert not C.wants_image_gen("帮我画一张户型平面图")
    assert not C.wants_image_gen("把这个画成 CAD 图")
    assert not C.wants_image_gen("画一张图", [{"name": "a.png"}])
    assert C.wants_image_edit("把背景换成海边", [{"name": "photo.jpg"}])
    assert not C.wants_image_edit("把背景换成海边", [{"name": "doc.pdf"}])
    assert not C.wants_image_edit("转成 PSD 图层", [{"name": "a.png"}])


def test_registry_has_generate_image():
    from tool_router.registry import LOOP_TOOLS, tool_schemas
    assert "generate_image" in LOOP_TOOLS
    assert any(t["name"] == "generate_image" for t in tool_schemas())


async def test_settings_keep_other_keys_and_image_test(tmp_path, monkeypatch):
    from aiw_api.config import MachineConfig, Settings
    from aiw_api.main import build_app
    for k in ("AIW_IMAGE_MODEL", "AIW_IMAGE2_MODEL", "AIW_IMAGE2_API_KEY", "AIW_IMAGE2_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = [MachineConfig(id="m1", provider="proxmox", provider_vm_id="100", agent_endpoint="ws://127.0.0.1:1",
                                desktop_http="http://127.0.0.1:1")]
    app = build_app(s)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"Authorization": "Bearer t"})
    await c.patch("/api/settings/models", json={"chat_model": "fast-chat"})
    r = (await c.put("/api/settings/models", json={"model": "m", "image2_base_url": "http://g/v1/", "image2_model": "gpt-image-2.5",
                                                   "image_default": "custom"})).json()
    assert r["chat_model"] == "fast-chat" and r["image2_base_url"] == "http://g/v1" and r["image_default"] == "custom"
    assert (await c.put("/api/settings/models", json={"image_default": "x"})).status_code == 400
    await c.put("/api/secrets/image2_api_key", json={"value": "ck"})
    assert (await c.get("/api/settings/models")).json()["has_image2_key"] is True

    async def fake_gen(base, key, model, prompt, size, n, timeout):
        if model == "gpt-image-2.5":
            return [_png()]
        raise IE.ImageEditError("nope")
    monkeypatch.setattr(IE, "generate", fake_gen)
    t = (await c.post("/api/image/test", json={})).json()
    assert t["ok"] and t["default"] == "custom" and t["results"][0]["model"] == "gpt-image-2.5"
    await c.aclose()


async def test_chat_makes_the_picture_itself(tmp_path, monkeypatch):
    """画一张… goes to the chat (not a task), generate_image runs, the picture rides on the reply as data.files."""
    import os
    os.environ["AIW_NO_AUTOAPP"] = "1"
    from aiw_api.config import MachineConfig, Settings
    from aiw_api.main import build_app
    from llm_router import LLMRouter
    from test_chat import FakeChat
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = [MachineConfig(id="m1", provider="proxmox", provider_vm_id="100", agent_endpoint="ws://127.0.0.1:1",
                                desktop_http="http://127.0.0.1:1")]
    app = build_app(s)
    fake = FakeChat(route="task")            # the classifier would say task: the picture shortcut must win
    chat = app.state.chat
    chat.llm_factory = lambda role: LLMRouter([fake])
    chat.tool_loader = None
    app.state.store.kv_set("models", {"image2_base_url": "http://g/v1", "image2_model": "gpt-image-2.5"})
    app.state.secrets.put("image2_api_key", "ck")

    async def fake_gen(base, key, model, prompt, size, n, timeout):
        return [_png()] * n
    monkeypatch.setattr(IE, "generate", fake_gen)
    fake.tool_first = ("generate_image", {"prompt": "一只橘猫", "n": 2})
    fake.replies = ["画好了，两张橘猫。"]
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"Authorization": "Bearer t"})
    await c.post("/api/chat/send", json={"text": "帮我画一张橘猫的插画", "stream": False})
    msgs = (await c.get("/api/chat")).json()["messages"]
    last = msgs[-1]
    assert last["kind"] == "text" and len(last["data"]["files"]) == 2
    assert all(f["kind"] == "image" and os.path.isfile(f["path"]) for f in last["data"]["files"])
    assert "generate_image" in fake.chats[0]["tools"] and "offer_task" not in last["data"]
    for j in list(chat.jobs):
        j.cancel()
    await c.aclose()
