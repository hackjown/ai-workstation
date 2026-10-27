"""Screenshot of the wrong window (task-d76ffb0bd1): find the task's page, else use a picture of the page itself."""
import base64
import io
from types import SimpleNamespace

from PIL import Image

from agent_runtime import AgentLoop, LoopConfig
from aiw_protocols import Task
from llm_router import LLMRouter, ScriptedProvider


def _jpeg(w, h):
    b = io.BytesIO(); Image.new("RGB", (w, h), (200, 30, 30)).save(b, "JPEG"); return base64.b64encode(b.getvalue()).decode()


class FakeComputer:
    def __init__(self, verified):
        self.verified, self.calls = verified, []

    async def execute(self, action, payload=None, observe=True, timeout=None):
        self.calls.append((action, (payload or {}).get("tool")))
        if action == "browseros.call" and payload["tool"] == "front":
            return SimpleNamespace(success=True, result={"ok": True, "verified": self.verified, "window": "BrowserOS - 会话回放",
                                                         "url": "https://chat.qwen.ai/auth?mode=register"}, screenshot=None)
        if action == "browseros.call" and payload["tool"] == "page_shot":
            return SimpleNamespace(success=True, result={"ok": True, "images": [{"data": _jpeg(2560, 1440)}],
                                                         "url": "https://chat.qwen.ai/auth?mode=register"}, screenshot=None)
        if action == "computer.screenshot":
            return SimpleNamespace(success=True, result={}, screenshot=_jpeg(1600, 900), duration_ms=1)
        raise AssertionError(action)


def _loop(comp, tmp_path):
    lp = AgentLoop(Task(prompt="继续完成注册"), "s-pv", comp, LLMRouter([ScriptedProvider([])]),
                   SimpleNamespace(bos_key="task-x", scale=1.0), emit=_sink,
                   config=LoopConfig(max_steps=5, timeout_s=30, make_plan=False, artifacts_dir=str(tmp_path)))
    lp._bos_page, lp._bos_step, lp.step, lp._last_tool = 3, 5, 6, "screenshot"
    lp._fresh = _jpeg(1600, 900)   # the screenshot tool's own grab, of the wrong window
    return lp


async def _sink(e):
    pass


async def test_wrong_window_uses_the_page_picture_and_maps_clicks(tmp_path):
    comp = FakeComputer(verified=False)
    lp = _loop(comp, tmp_path)
    shot, shown = await lp._see()
    assert ("browseros.call", "page_shot") in comp.calls and shown[0] == lp.cfg.llm_image_width
    assert "page 3" in lp._page_note and lp._fresh is None
    d = SimpleNamespace(tool="click", args={"x": 100, "y": 50})
    lp._page_click(d)
    sc = 2560 / lp.cfg.llm_image_width
    assert d.tool == "bos_act" and d.args == {"page": 3, "kind": "click_at", "x": round(100 * sc), "y": round(50 * sc)}


async def test_right_window_takes_a_new_desktop_screenshot(tmp_path):
    comp = FakeComputer(verified=True)
    lp = _loop(comp, tmp_path)
    await lp._see()
    assert comp.calls[-1] == ("computer.screenshot", None) and not lp._page_view
    d = SimpleNamespace(tool="click", args={"x": 1, "y": 2})
    lp._page_click(d)
    assert d.tool == "click"
