"""Phase 2 + 3 acceptance on the mock desktop:
computer.screenshot() / click(500,300) / type("hello") / keypress("ENTER") round-trip
over a real WebSocket to a real vm-agent process (in-process server)."""
import asyncio
import base64

import pytest
from computer_runtime import Computer, LinuxComputerProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

TOKEN = "test-token"


@pytest.fixture
async def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path))
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(serve("127.0.0.1", 18765, Router(backend), TOKEN, ready, stop))
    await ready.wait()
    yield backend
    stop.set()
    await task


async def test_phase3_acceptance(agent):
    computer = Computer(LinuxComputerProvider("ws://127.0.0.1:18765", TOKEN))
    shot = await computer.screenshot()
    assert shot.success and shot.width == 1280
    assert base64.b64decode(shot.screenshot)[:8] == b"\x89PNG\r\n\x1a\n"
    assert (await computer.click(500, 300)).success
    assert (await computer.type("hello")).success
    assert (await computer.keypress("ENTER")).success
    assert ("click", 500, 300, 1, 1) in agent.log
    assert agent.typed == "hello"
    assert ("key", "Return") in agent.log
    await computer.close()


async def test_all_actions(agent, tmp_path):
    c = LinuxComputerProvider("ws://127.0.0.1:18765", TOKEN)
    assert (await c.double_click(10, 10)).success
    assert (await c.right_click(10, 10)).success
    assert (await c.drag(1, 1, 50, 50)).success
    assert (await c.scroll(0, 3)).success
    assert (await c.hotkey("ctrl", "l")).success
    assert (await c.open_app("chrome")).success
    assert (await c.close_app("chrome")).result["matched"]
    r = await c.shell("echo hi > test.txt && cat test.txt", cwd=str(tmp_path))
    assert r.success and r.result["stdout"].strip() == "hi"
    assert (await c.execute("files.write", {"path": "a/b.txt", "content": "x"})).success
    r = await c.execute("files.list", {"path": "a"})
    assert r.result["entries"][0]["name"] == "b.txt"
    r = await c.execute("files.read", {"path": "../../etc/passwd"})
    assert not r.success and "outside" in r.error
    r = await c.execute("computer.click", {"x": 99999, "y": 1})
    assert not r.success
    r = await c.execute("computer.click", {"x": 1})
    assert not r.success and "missing" in r.error
    h = await c.health()
    assert h.success and h.result["backend"] == "mock"
    obs = await c.execute("computer.click", {"x": 20, "y": 20}, observe=True)
    assert obs.screenshot
    await c.close()


async def test_auth_rejected(agent):
    c = LinuxComputerProvider("ws://127.0.0.1:18765", "wrong", retries=0)
    r = await c.screenshot()
    assert not r.success
    await c.close()


async def test_pipelined(agent):
    c = LinuxComputerProvider("ws://127.0.0.1:18765", TOKEN)
    rs = await asyncio.gather(*[c.click(i, i) for i in range(20)])
    assert all(r.success for r in rs)
    await c.close()
