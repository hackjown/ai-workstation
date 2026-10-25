"""V1.7.2 (task-4e2): exact `move`, zoomed compare, pixel grid, retries on transient errors, longer image budget."""
import asyncio
import base64
import io

import httpx
from PIL import Image

from agent_runtime import image_edit as ie
from agent_runtime import loop as L
from agent_runtime.recovery import Recovery
from test_phase5_agent_loop import env  # noqa: F401
from test_v17_image_edit import _bytes, _photo


def test_move_raises_a_box_exactly():
    o = _photo()  # white cabinet at [100,420,450,650]
    png, info = ie.move_regions(_bytes(o), [{"box": [100, 420, 450, 650], "to": [100, 390, 450, 650]}], feather=0)
    r = Image.open(io.BytesIO(png)).convert("RGB")
    assert info["moves"][0]["to"] == [100, 390, 450, 650]
    assert r.getpixel((270, 395)) == (245, 245, 245) and o.getpixel((270, 395)) != (245, 245, 245)
    assert r.getpixel((270, 700)) == o.getpixel((270, 700))  # below the box untouched


def test_changed_box_and_zoomed_side_by_side():
    o = _photo()
    png, _ = ie.move_regions(_bytes(o), [{"box": [100, 420, 450, 650], "to": [100, 400, 450, 650]}], feather=0)
    r = Image.open(io.BytesIO(png)).convert("RGB")
    box = ie.changed_box(o, r)
    assert box and box[1] <= 405 and box[3] >= 415 and box[1] > 300
    assert ie.changed_box(o, o.copy()) is None
    plain = Image.open(io.BytesIO(L._side_by_side([o, r])))
    zoomed = Image.open(io.BytesIO(L._side_by_side([o, r], zoom=box)))
    assert zoomed.width == plain.width and zoomed.height > plain.height + 100


def test_transient_and_grid_and_recovery():
    assert L._transient("TimeoutError: ") and L._transient("HTTP 502 Bad Gateway") and L._transient("Connection reset")
    assert not L._transient("exit code 1: SyntaxError") and not L._transient(None)
    g = L._with_grid(Image.new("RGB", (400, 300), "white"))
    assert g.getpixel((50, 10)) == (255, 0, 0) and g.getpixel((25, 25)) == (255, 255, 255)
    note = asyncio.run(Recovery(None).recover(1, "shell.exec"))
    assert "click" not in note and "timeout" in note


async def test_loop_move_without_ai_model_and_tool_retry(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    (tmp / "uploads").mkdir()
    (tmp / "uploads/room.png").write_bytes(_bytes(_photo()))
    loop = AgentLoop(Task(prompt="把 uploads/room.png 白色柜子加高"), "s-mv", comp, LLMRouter([ScriptedProvider([])]),
                     ToolExecutor(comp), config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False,
                                                           artifacts_dir=str(tmp / "art")))
    r = await loop._image_edit({"path": "uploads/room.png", "move": {"box": [100, 420, 450, 650], "to": [100, 390, 450, 650]},
                                "save": "uploads/room-up.png"})
    assert r.startswith("image_edit OK (no AI)") and "Moved exactly" in r and loop._last_image == "uploads/room-up.png"
    r = await loop._image_edit({"path": "uploads/room.png", "move": [{"box": [1, 2, 3]}]})
    assert r.startswith("REFUSED")


def test_image_tasks_get_a_longer_budget():
    from agent_runtime import AgentLoop
    from aiw_protocols import Task
    lp = AgentLoop(Task(prompt="把上传图片中白色柜子加高\n- ~/workspace/uploads/枫木服务区.png", timeout_s=900), "s", None, None, None)
    assert lp._image_task and lp.cfg.timeout_s >= 2400
    lp = AgentLoop(Task(prompt="打开 Chrome 搜索 OpenAI", timeout_s=900), "s", None, None, None)
    assert not lp._image_task and lp.cfg.timeout_s == 900


async def test_llm_post_retries_gateway_blips(monkeypatch):
    from llm_router.providers.openai import OpenAIProvider
    monkeypatch.setenv("AIW_LLM_RETRIES", "3")
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("reset")
        if n["i"] == 2:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    p = OpenAIProvider(model="m", api_key="k", base_url="https://gw/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        d = await p._post(c, {"model": "m", "messages": []})
    assert d["choices"][0]["message"]["content"] == "ok" and n["i"] == 3


_real_sleep = asyncio.sleep


async def _nosleep(*a, **k):
    await _real_sleep(0)
