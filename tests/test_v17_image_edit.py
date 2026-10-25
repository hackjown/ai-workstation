"""V1.7: image_edit = AI image model edit + scale back + aligned restore of regions the AI garbled."""
import base64
import io
import json

import httpx
import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from agent_runtime import image_edit as ie
from test_phase5_agent_loop import env  # noqa: F401


def _photo(w=600, h=800):
    im = Image.new("RGB", (w, h), (200, 200, 190))
    d = ImageDraw.Draw(im)
    for i in range(0, w, 37):
        d.line([(i, 0), (i + 80, h)], fill=(90, 120, 160), width=5)
    d.rectangle([100, 420, 450, 650], fill=(245, 245, 245))
    d.rectangle([380, 300, 560, 520], fill=(200, 30, 30))
    d.text((400, 350), "SALE 80%", fill=(255, 255, 0))
    d.ellipse([60, 80, 220, 240], fill=(30, 140, 60))
    return im


def _bytes(im, fmt="PNG"):
    b = io.BytesIO()
    im.save(b, fmt)
    return b.getvalue()


def _ai_version(im, s=0.96, dx=9, dy=-6, out=(480, 640)):
    """What an image model does: re-render a bit zoomed/shifted at another resolution."""
    W, H = im.size
    big = im.resize((round(W * s), round(H * s)))
    canvas = Image.new("RGB", (W, H), (200, 200, 190))
    canvas.paste(big, (round((W - big.width) / 2 + dx), round((H - big.height) / 2 + dy)))
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, 0, W, 30], fill=(10, 10, 10))   # some real change
    return canvas.resize(out)


def test_align_recovers_scale_and_shift():
    o = _photo()
    r = _ai_version(o).resize(o.size)
    s, dx, dy, v = ie.align(o, r)
    assert abs(s - 0.96) < 0.012 and abs(dx - 9) < 4 and abs(dy + 6) < 4 and v < 10


def test_postprocess_scales_back_and_restores_aligned():
    o = _photo()
    garbled = _ai_version(o)
    ImageDraw.Draw(garbled).rectangle([300, 240, 440, 400], fill=(0, 0, 255))  # the poster came out wrong
    png, info = ie.postprocess(_bytes(o), _bytes(garbled), True, [[380, 300, 560, 520]], feather=0)
    out = Image.open(io.BytesIO(png))
    assert out.size == o.size and info["ai_size"] == [480, 640] and info["align"]["scale"] < 0.98
    s, dx, dy = info["align"]["scale"], info["align"]["dx"], info["align"]["dy"]
    W, H = o.size
    ox, oy = (W - W * s) / 2 + dx, (H - H * s) / 2 + dy
    box = [round(400 * s + ox), round(330 * s + oy), round(540 * s + ox), round(500 * s + oy)]
    want = o.resize((round(W * s), round(H * s))).crop([round(400 * s), round(330 * s), round(540 * s), round(500 * s)])
    diff = ImageStat.Stat(ImageChops.difference(out.crop(box).convert("RGB"), want.resize((box[2] - box[0], box[3] - box[1])))).mean
    assert max(diff) < 25  # the red poster is back where the AI picture has it, not blue
    png2, info2 = ie.postprocess(_bytes(o), _bytes(garbled), False, None)
    assert Image.open(io.BytesIO(png2)).size == (480, 640) and "align" not in info2


def test_images_in_finds_every_gateway_shape():
    b64 = base64.b64encode(_bytes(_photo(20, 20))).decode()
    uri = f"data:image/png;base64,{b64}"
    assert ie._images_in({"content": "", "images": [{"type": "image_url", "image_url": {"url": uri}}]}) == [uri]
    assert ie._images_in({"content": [{"type": "text", "text": "ok"}, {"type": "image_url", "image_url": {"url": uri}}]}) == [uri]
    assert ie._images_in({"content": f"here ![img]({uri})"}) == [uri]
    assert ie._images_in({"content": "done: https://cdn.x.com/a/out.png"}) == ["https://cdn.x.com/a/out.png"]
    assert ie._images_in({"content": "sorry, I cannot"}) == []


def test_prompt_keeps_everything_else():
    p = ie.prompt_for("把白色柜子加高到和左侧柜子一样高")
    assert "白色柜子" in p and "Keep everything else" in p


async def test_edit_uses_chat_then_images_api(monkeypatch):
    img = _bytes(_photo(64, 64))
    b64 = base64.b64encode(img).decode()
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path.endswith("/chat/completions"):
            body = json.loads(req.content)
            assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            return httpx.Response(200, json={"choices": [{"message": {"content": "no image here"}}]})
        return httpx.Response(200, json={"data": [{"b64_json": b64}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(ie.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    out = await ie.edit("https://gw/v1", "k", "gemini-2.5-flash-image", img, "raise it")
    assert out == img and calls == ["/v1/chat/completions", "/v1/images/edits"]
    calls.clear()
    await ie.edit("https://gw/v1", "k", "gpt-image-1", img, "raise it")
    assert calls == ["/v1/images/edits"]


async def test_loop_image_edit_tool(env, monkeypatch):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    assert "image_edit" in LOOP_TOOLS and BY_NAME["image_edit"].params["restore"]["type"] == "array"
    backend, comp, tmp = env
    (tmp / "uploads").mkdir()
    o = _photo()
    (tmp / "uploads/room.png").write_bytes(_bytes(o))
    loop = AgentLoop(Task(prompt="把柜子加高"), "s-ie", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    r = await loop._image_edit({"path": "uploads/room.png", "prompt": "taller"})
    assert r.startswith("REFUSED") and "改图模型" in r
    loop.image_cfg = {"model": "gpt-image-1", "key": "k", "base_url": "https://gw/v1"}
    seen = {}

    async def fake_edit(base, key, model, img, prompt, timeout=300):
        seen["prompt"] = prompt
        return _bytes(_ai_version(Image.open(io.BytesIO(img)).convert("RGB")))
    monkeypatch.setattr(ie, "edit", fake_edit)
    r = await loop._image_edit({"path": "~/workspace/uploads/room.png", "prompt": "make the cabinet taller"})
    assert r.startswith("image_edit OK") and "uploads/room-ai.png" in r and "scaled back" in r
    assert "make the cabinet taller" in seen["prompt"]
    assert Image.open(tmp / "uploads/room-ai.png").size == o.size
    r = await loop._image_edit({"path": "uploads/room-ai.png", "original": "uploads/room.png",
                                "restore": [[380, 300, 560, 520]], "save": "out/final.png"})
    assert r.startswith("image_edit OK") and "Restored 1 region" in r and (tmp / "out/final.png").is_file()
    r = await loop._image_edit({"path": "uploads/room.png", "restore": [[1, 2, 3]]})
    assert r.startswith("REFUSED")
