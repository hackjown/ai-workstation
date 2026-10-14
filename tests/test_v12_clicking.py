"""V1.2 clicking: vision coordinate frames, two-pass grounding, click_element, DOM clicks via CDP."""
import asyncio
import base64
import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

from agent_runtime import AgentLoop, LoopConfig
from agent_runtime.grounding import convert_desc, coord_mode, locate, parse_point, to_pixels
from aiw_protocols import Task, TaskStatus
from llm_router import LLMRouter, ScriptedProvider
from tool_router import ToolExecutor
from vm_agent.browser.cdp import screen_point

from test_phase5_agent_loop import env  # noqa: F401  (fixture)


def test_qwen3_vl_coordinates_are_0_to_1000(monkeypatch):
    monkeypatch.delenv("AIW_VISION_COORDS", raising=False)
    assert coord_mode("deveco-qwen3-vl") == "norm1000"
    assert coord_mode("glm-5v-turbo") == "norm1000"
    assert coord_mode("qwen2.5-vl-72b") == "pixel"
    assert coord_mode("gpt-4o") == "pixel"
    monkeypatch.setenv("AIW_VISION_COORDS", "pixel")
    assert coord_mode("deveco-qwen3-vl") == "pixel"


def test_task_25a_register_button_lands_on_the_button():
    # qwen3-vl said 注册 -> (823, 159); the real button is at about (1322, 146) on the 1600x900 screen.
    desc = "注册按钮 -> (823, 159)\n登录按钮 -> （777, 159）"
    out = convert_desc(desc, (1280, 720), "norm1000")
    assert "(1053, 114)" in out and "（995, 114）" in out
    x, y = to_pixels(823, 159, "norm1000", (1280, 720))
    sx, sy = x * 1.25, y * 1.25  # executor scale 1600/1280
    assert abs(sx - 1322) < 15 and abs(sy - 146) < 15
    assert convert_desc(desc, (1280, 720), "pixel") == desc


@pytest.mark.parametrize("text,expect", [
    ('{"x": 512, "y": 88}', (512, 88)),
    ('```json\n{"x":10,"y":20}\n```', (10, 20)),
    ('{"bbox_2d": [100, 50, 200, 70]}', (150, 60)),
    ('[100, 50, 200, 70] bbox', (150, 60)),
    ('{"found": false}', None),
    ('<think>hmm</think>{"x": 3, "y": 4}', (3, 4)),
    ('(640, 360)', (640, 360)),
])
def test_parse_point(text, expect):
    assert parse_point(text) == expect


class GroundingEyes(ScriptedProvider):
    """Answers like Qwen3-VL (0-1000). The first look is off by `first_error` screen pixels;
    the zoomed look reads the red button exactly."""

    def __init__(self, true_xy, screen=(1600, 900), first_error=(40, -30)):
        super().__init__([])
        self.true_xy, self.screen, self.first_error, self.calls = true_xy, screen, first_error, []

    async def describe(self, system, user, image_b64, max_tokens=900):
        img = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")
        self.calls.append(img.size)
        if "zoomed-in crop" not in user:
            tx, ty = self.true_xy[0] + self.first_error[0], self.true_xy[1] + self.first_error[1]
            return '{"x": %d, "y": %d}' % (tx / self.screen[0] * 1000, ty / self.screen[1] * 1000)
        px = img.load()
        pts = [(x, y) for x in range(0, img.width, 2) for y in range(0, img.height, 2)
               if px[x, y][0] > 200 and px[x, y][1] < 80]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        return '{"x": %d, "y": %d}' % (cx / img.width * 1000, cy / img.height * 1000)


def _paint_button(img, xy):
    ImageDraw.Draw(img).rectangle([xy[0] - 18, xy[1] - 10, xy[0] + 18, xy[1] + 10], fill=(230, 30, 30))
    return img


async def test_locate_refines_with_a_zoomed_crop():
    true = (1322, 146)
    eyes = GroundingEyes(true)
    screen = _paint_button(Image.new("RGB", (1600, 900), (236, 230, 217)), true)
    pt = await locate(LLMRouter([eyes]), screen, "右上角红色注册按钮", "norm1000")
    assert len(eyes.calls) == 2 and eyes.calls[0] == (1280, 720)
    assert abs(pt[0] - true[0]) <= 4 and abs(pt[1] - true[1]) <= 4


async def test_click_element_clicks_where_the_eyes_found_it(env, monkeypatch):  # noqa: F811
    backend, comp, tmp = env
    monkeypatch.setenv("AIW_VISION_COORDS", "norm1000")
    true = (900, 500)  # the mock desktop is 1280x800
    _paint_button(backend.canvas, true)
    eyes = GroundingEyes(true, (backend.width, backend.height), first_error=(25, 20))
    llm = ScriptedProvider([("click_element", {"target": "红色按钮"}), ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt="点击红色按钮"), "s-ce", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=5, timeout_s=20, make_plan=False), vision_llm=LLMRouter([eyes]))
    loop._look = lambda shot, shown: asyncio.sleep(0, result=None)  # only the grounding calls matter here
    t = await loop.run()
    assert t.status == TaskStatus.SUCCEEDED
    clicks = [e for e in backend.log if e[0] == "click"]
    assert clicks and abs(clicks[-1][1] - true[0]) <= 4 and abs(clicks[-1][2] - true[1]) <= 4


def test_screen_point_adds_browser_chrome():
    info = {"cx": 1321.5, "cy": 33, "sx": 0, "sy": 0, "ow": 1600, "oh": 900, "iw": 1600, "ih": 787, "dpr": 1}
    assert screen_point(info) == (1322, 146)


CHROME = shutil.which("chromium") or shutil.which("google-chrome")


@pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium for the CDP test")
async def test_dom_click_and_fill_with_real_chrome(tmp_path):
    from vm_agent.browser import cdp
    page = tmp_path / "p.html"
    page.write_text('<meta charset="utf-8"><nav><a href="#">主页</a><button>登录</button><button>注册</button></nav>'
                    '<label>用户名 <input></label><input type="password" placeholder="密码"><button>注册并登录</button>',
                    encoding="utf-8")
    port = 9333
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", f"--remote-debugging-port={port}",
                             f"--user-data-dir={tmp_path / 'prof'}", page.as_uri()],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        c = cdp.CDP(f"http://127.0.0.1:{port}")
        for _ in range(40):
            if await c.available():
                break
            await asyncio.sleep(0.25)

        class B:
            def __init__(self):
                self.log = []

            async def click(self, x, y, button=1, repeat=1):
                self.log.append(("click", x, y))

            async def key(self, k):
                self.log.append(("key", k))

            async def type(self, t):
                self.log.append(("type", t))

        b = B()
        r = await cdp.click(b, "注册", c)
        assert r["clicked"] == "注册" and r["tag"] == "button"
        r = await cdp.fill(b, "密码", "12345678", cdp=c)
        assert r["typed"] == "••••••••"
        async with await c.session() as ws:
            assert await c._eval(ws, "document.querySelector('input[type=password]').value") == "12345678"
        with pytest.raises(LookupError) as e:
            await cdp.click(b, "不存在", c)
        assert "注册并登录" in str(e.value)
    finally:
        proc.kill()
