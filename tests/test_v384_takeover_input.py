"""3.8.4: takeover input goes straight to the VM (no DOM events faked inside the viewer).

Phone -> /ws/input/<mid> (or POST /api/machines/<mid>/input) -> API pump (clean, coalesce moves) -> vm-agent
`computer.input` -> absolute xdotool on the display. Old vm-agents fall back to the classic actions + shell xdotool.
"""
import asyncio
import os
import re
import shlex
import threading
from pathlib import Path

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.remote_input import InputPump
from aiw_protocols.input_ops import clean, coalesce
from computer_runtime.providers.base import ActionResult
from vm_agent.computer import desktop
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

ROOT = Path(__file__).resolve().parents[1]
PORT = 18791


def _settings(tmp_path, port):
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{port}", desktop_http="http://127.0.0.1:1")]
    return s


@pytest.fixture
async def stack(tmp_path):
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), "agent-secret", ready, stop))
    await ready.wait()
    app = build_app(_settings(tmp_path, PORT))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, backend
    await client.aclose()
    stop.set(); await t


async def test_http_input_reaches_the_desktop(stack):
    client, b = stack
    ops = [{"t": "move", "x": 10, "y": 10}, {"t": "move", "x": 20, "y": 20}, {"t": "move", "x": 30.6, "y": 40.2},
           {"t": "click", "x": 30, "y": 40, "button": "left"},
           {"t": "click", "x": 31, "y": 41, "count": 2},
           {"t": "click", "x": 50, "y": 60, "button": "right"},
           {"t": "down", "x": 100, "y": 100}, {"t": "move", "x": 150, "y": 120}, {"t": "up", "x": 200, "y": 130},
           {"t": "scroll", "x": 300, "y": 300, "dx": 0, "dy": 3},
           {"t": "text", "text": "你好 abc"}, {"t": "key", "key": "BackSpace"}, {"t": "key", "key": "ctrl+c"},
           {"t": "key", "key": "Return"}, {"t": "move", "x": 99999, "y": -5}]
    r = await client.post("/api/machines/ai-linux-001/input", json={"ops": ops})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and not j["legacy"] and j["width"] == 1280 and j["height"] == 800
    log = b.log
    # the three moves collapse into the last one before the click
    assert ("move", 30, 40) in log and ("move", 10, 10) not in log and ("move", 20, 20) not in log
    assert ("click", 30, 40, 1, 1) in log and ("click", 31, 41, 1, 2) in log and ("click", 50, 60, 3, 1) in log
    assert ("down", 100, 100, 1) in log and ("up", 200, 130, 1) in log
    assert log.index(("down", 100, 100, 1)) < log.index(("move", 150, 120)) < log.index(("up", 200, 130, 1))
    assert ("scroll", 0, 3) in log
    assert b.typed == "你好 abc"
    assert ("key", "BackSpace") in log and ("key", "ctrl+c") in log and ("key", "Return") in log
    assert log[-1] == ("move", 1279, 0)   # clamped to the screen


async def test_http_input_validation(stack):
    client, _ = stack
    assert (await client.post("/api/machines/nope/input", json={"ops": [{"t": "key", "key": "a"}]})).status_code == 404
    assert (await client.post("/api/machines/ai-linux-001/input", json={"ops": []})).status_code == 400
    r = await client.post("/api/machines/ai-linux-001/input", json={"ops": [{"t": "key", "key": "a; rm -rf /"}]})
    assert r.json()["accepted"] == 0
    r = await client.post("/api/machines/ai-linux-001/input", json={"ops": [{"t": "key", "key": "a"}]},
                          headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_clean_and_coalesce():
    assert clean({"t": "click", "x": 5, "y": 6}) == {"t": "click", "x": 5, "y": 6, "button": "left", "count": 1}
    assert clean({"t": "click", "x": 5, "y": 6, "button": "evil"}) is None
    assert clean({"t": "scroll", "x": 1, "y": 1, "dx": 0, "dy": 999}, (100, 100))["dy"] == 30
    assert clean({"t": "scroll", "x": 1, "y": 1, "dx": 0, "dy": 0}) is None
    assert clean({"t": "text", "text": "a\x00b\nc"}) == {"t": "text", "text": "ab\nc"}
    assert clean({"t": "key", "key": "ctrl+alt+Left"})["key"] == "ctrl+alt+Left"
    assert clean({"t": "key", "key": "$(reboot)"}) is None and clean("x") is None and clean({"t": "eval"}) is None
    m = [{"t": "move", "x": i, "y": i} for i in range(5)] + [{"t": "click", "x": 4, "y": 4, "button": "left", "count": 1}]
    assert coalesce(m) == [{"t": "move", "x": 4, "y": 4}, m[-1]]


def test_x11_types_chinese_slower(monkeypatch):
    calls = []
    b = desktop.X11Backend.__new__(desktop.X11Backend)

    async def xdo(args):
        calls.append(args)
        return ""
    b._xdo = xdo
    asyncio.run(b.type("ab你好c"))
    assert calls == ["type --delay 12 -- ab", "type --delay 70 -- " + shlex.quote("你好"), "type --delay 12 -- c"]
    calls.clear()
    asyncio.run(b.mouse_down(3, 4)); asyncio.run(b.mouse_up(5, 6, 3))
    assert calls == ["mousemove --sync 3 4 mousedown 1", "mousemove --sync 5 6 mouseup 3"]


class OldAgent:
    """A vm-agent from before 3.8.4: no computer.input."""
    def __init__(self):
        self.calls = []

    async def execute(self, action, payload=None, observe=False, timeout=30.0):
        self.calls.append((action, payload))
        if action == "computer.input":
            return ActionResult(False, error="unknown action type: computer.input")
        if action == "computer.screen_size":
            return ActionResult(True, {"width": 1600, "height": 900})
        return ActionResult(True, {})


async def test_old_agent_fallback():
    c = OldAgent()
    p = InputPump(c)
    await p.screen()
    p.push([{"t": "move", "x": 1, "y": 1}, {"t": "click", "x": 2, "y": 2, "count": 2}, {"t": "down", "x": 3, "y": 3},
            {"t": "up", "x": 4, "y": 4}, {"t": "text", "text": "hi"}, {"t": "text", "text": "中文"},
            {"t": "key", "key": "ctrl+v"}, {"t": "click", "x": 5, "y": 5, "button": "right"}, {"t": "scroll", "x": 1, "y": 1, "dy": -2}])
    await p.drain()
    acts = [a for a, _ in c.calls]
    assert acts[:2] == ["computer.screen_size", "computer.input"] and p.legacy is True
    assert "computer.double_click" in acts and "computer.hotkey" in acts and "computer.scroll" in acts
    sh = [pl["command"] for a, pl in c.calls if a == "shell.exec"]
    assert sh[0] == "xdotool mousemove --sync 3 3 mousedown 1" and sh[1] == "xdotool mousemove --sync 4 4 mouseup 1"
    assert sh[2].startswith("xdotool type --delay 70 -- ") and "中文" in sh[2]
    assert ("computer.type", {"text": "hi"}) in c.calls
    assert ("computer.click", {"x": 5, "y": 5, "button": "right"}) in c.calls
    # once known old, it never asks for computer.input again
    c.calls.clear(); p.push({"t": "key", "key": "Return"}); await p.drain()
    assert [a for a, _ in c.calls] == ["computer.hotkey"]


def test_ws_input(tmp_path):
    """The socket: hello with the screen size, ops in, a held button released when the phone goes away."""
    from starlette.testclient import TestClient
    port = PORT + 1
    backend = MockBackend()
    loop = asyncio.new_event_loop()
    ready, stop = asyncio.Event(), asyncio.Event()

    def agent():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve("127.0.0.1", port, Router(backend), "agent-secret", ready, stop))
    th = threading.Thread(target=agent, daemon=True); th.start()
    for _ in range(100):
        if ready.is_set():
            break
        threading.Event().wait(0.05)
    try:
        app = build_app(_settings(tmp_path, port))
        with TestClient(app) as tc:
            with pytest.raises(Exception):
                with tc.websocket_connect("/ws/input/ai-linux-001?token=bad") as ws:
                    ws.receive_json()
            with tc.websocket_connect("/ws/input/ai-linux-001?token=api-secret") as ws:
                hello = ws.receive_json()
                assert hello == {"op": "hello", "width": 1280, "height": 800}
                ws.send_json({"ops": [{"t": "move", "x": 5, "y": 5}, {"t": "click", "x": 5, "y": 5}]})
                ws.send_json({"t": "text", "text": "键盘"})
                ws.send_json({"t": "down", "x": 40, "y": 40})
                ws.send_json({"op": "ping", "t": 1})
                assert ws.receive_json() == {"op": "pong", "t": 1}
                for _ in range(100):
                    if ("down", 40, 40, 1) in backend.log:
                        break
                    threading.Event().wait(0.05)
            for _ in range(100):   # closed mid-drag: the button comes up
                if ("up", 40, 40, 1) in backend.log:
                    break
                threading.Event().wait(0.05)
        assert ("click", 5, 5, 1, 1) in backend.log and backend.typed == "键盘"
        assert ("down", 40, 40, 1) in backend.log and ("up", 40, 40, 1) in backend.log
    finally:
        loop.call_soon_threadsafe(stop.set); th.join(5)


def test_touch_layer_uses_the_input_channel():
    tk = (ROOT / "apps/web/takeover.js").read_text(encoding="utf-8")
    app = (ROOT / "apps/web/app.js").read_text(encoding="utf-8")
    html = (ROOT / "apps/web/takeover.html").read_text(encoding="utf-8")
    assert "ws/input/" in tk and "/api/machines/" in tk and '"/input"' in tk   # socket + POST fallback
    for needle in ("鼠标指针", "直接触控", "count === 2", '"right"', '"down"', '"up"', '"scroll"', '"text"', '"key"',
                   "MOVE_MS", "flushMove", "ctrl+c", "ctrl+v", "ctrl+a", "Super", "Alt", "compositionend", "toR("):
        assert needle in tk, needle
    assert "mid: MID" in app and "token: TOKEN" in app
    assert "AIW_TAKEOVER_MID" in html and "AIW_TAKEOVER_TOKEN" in html
    # direct mode: long press = right click; trackpad: the click goes to the cursor
    assert re.search(r"long press.*right click", tk, re.I | re.S)


def test_touch_layer_gestures_js():
    """tests/js/takeover_touch_test.js: gestures -> ops, exact mapping (portrait letterbox, landscape, pinch zoom)."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node or subprocess.run([node, "-e", "require('jsdom')"], capture_output=True).returncode != 0:
        pytest.skip("node + jsdom not installed")
    r = subprocess.run([node, str(ROOT / "tests/js/takeover_touch_test.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ALL OK" in r.stdout, r.stdout + r.stderr


def test_ios_native_keyboard():
    desk = (ROOT / "apps/ios/Sources/DesktopView.swift").read_text(encoding="utf-8")
    # the page gets the machine and the token for /ws/input
    assert "TakeoverWeb(url: u, server: api.server, mid: mid, token: api.token)" in desk
    assert "window.AIW_TAKEOVER_MID=" in desk and "window.AIW_TAKEOVER_TOKEN=" in desk
    # a hidden field that keeps the keyboard up, commits only finished IME text, forwards keys into the page
    for needle in ("final class KeyField: UITextField", "override var canResignFirstResponder: Bool { !keepUp }",
                   "markedTextRange == nil", "override func deleteBackward()", "textFieldShouldReturn", "inputAccessoryView = makeBar()",
                   '"mod:ctrl"', '"mod:alt"', '"mod:super"', '"ctrl+c"', '"ctrl+v"', '"ctrl+a"', '"Escape"', '"Tab"',
                   "UIKeyCommand.inputLeftArrow", "aiwTouch.input(", 'case "keyboard": self.host?.keyboard(true)',
                   "autocorrectionType = .no", "smartQuotesType = .no"):
        assert needle in desk, needle
    assert "loadHTMLString(html, baseURL: base)" in desk
