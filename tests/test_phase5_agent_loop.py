"""Agent loop on the mock desktop with a scripted LLM: spec §25 example,
policy blocking, pause/takeover/resume (acceptance tests 6-8)."""
import asyncio

import pytest
from agent_runtime import AgentControl, AgentLoop, LoopConfig
from aiw_protocols import EventType, Task, TaskStatus
from computer_runtime import Computer, LinuxComputerProvider
from llm_router import LLMRouter, ScriptedProvider
from tool_router import PolicyEngine, RiskLevel, ToolExecutor
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18766


@pytest.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path))
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), None, ready, stop))
    await ready.wait()
    comp = Computer(LinuxComputerProvider(f"ws://127.0.0.1:{PORT}"))
    yield backend, comp, tmp_path
    await comp.provider.close()
    stop.set(); await t


def make_loop(comp, script, tmp_path, control=None, confirm=None, events=None):
    async def sink(e):
        if events is not None:
            events.append(e)
    llm = ScriptedProvider(script)
    loop = AgentLoop(Task(prompt="打开 Chrome，搜索 OpenAI"), "s1", comp, LLMRouter([llm]),
                     ToolExecutor(comp, confirm=confirm), control=control, emit=sink,
                     config=LoopConfig(max_steps=15, timeout_s=20, artifacts_dir=str(tmp_path / "artifacts")))
    return loop, llm


async def test_search_openai_example(env):
    backend, comp, tmp = env
    events = []
    loop, llm = make_loop(comp, [
        ("open_app", {"name": "chrome"}),
        ("keypress", {"key": "ctrl+l"}),
        ("type", {"text": "OpenAI"}),
        ("keypress", {"key": "ENTER"}),
        ("screenshot", {}),
        ("done", {"summary": "searched"}),
    ], tmp, events=events)
    task = await loop.run()
    assert task.status == TaskStatus.SUCCEEDED, task.result
    assert "chrome" in backend.open_apps and backend.typed == "OpenAI"
    assert ("key", "Return") in backend.log
    assert llm.requests[0].screenshot_b64  # model always sees the screen
    assert any(e.type == EventType.VALIDATION for e in events)
    assert list((tmp / "artifacts" / "s1").glob("*.png"))  # screenshots are artifacts


async def test_terminal_create_file(env):
    backend, comp, tmp = env
    loop, _ = make_loop(comp, [("shell", {"command": "echo hi > test.txt", "cwd": str(tmp)}),
                               ("done", {"summary": "ok"})], tmp)
    assert (await loop.run()).status == TaskStatus.SUCCEEDED
    assert (tmp / "test.txt").read_text().strip() == "hi"


async def test_policy_blocks(env):
    backend, comp, tmp = env
    asked = []

    async def confirm(name, payload, decision):
        asked.append(payload["command"]); return False
    loop, llm = make_loop(comp, [("shell", {"command": "sudo su -"}),
                                 ("shell", {"command": "rm -rf ~/workspace"}),
                                 ("fail", {"reason": "blocked"})], tmp, confirm=confirm)
    task = await loop.run()
    assert task.status == TaskStatus.FAILED
    assert asked == ["rm -rf ~/workspace"]  # L3 asked, L4 never asked
    assert "forbidden" in loop.history[0] and "declined" in loop.history[1]


async def test_pause_takeover_resume(env):
    backend, comp, tmp = env
    control = AgentControl()
    events = []
    script = [("click", {"x": 100, "y": 100}), ("click", {"x": 200, "y": 200}),
              ("click", {"x": 300, "y": 300}), ("done", {"summary": "ok"})]
    loop, llm = make_loop(comp, script, tmp, control=control, events=events)

    orig = llm.decide

    async def slow_decide(req):
        await asyncio.sleep(0.15)
        return await orig(req)
    llm.decide = slow_decide

    run = asyncio.create_task(loop.run())
    await asyncio.sleep(0.25)
    control.takeover()                      # Test 6/7: agent stops, user drives
    await asyncio.sleep(0.4)
    n_clicks = sum(1 for e in backend.log if e[0] == "click")
    await backend.click(640, 400)           # user's own click during takeover
    await asyncio.sleep(0.3)
    assert sum(1 for e in backend.log if e[0] == "click") == n_clicks + 1  # agent did nothing
    control.resume()                        # Test 8: continues from current desktop
    task = await asyncio.wait_for(run, 10)
    assert task.status == TaskStatus.SUCCEEDED
    assert any(e.type == EventType.RESUMED and e.data["takeover"] for e in events)
    assert any(r.note and "took over" in r.note for r in llm.requests)


async def test_stop(env):
    backend, comp, tmp = env
    control = AgentControl()
    loop, llm = make_loop(comp, [("move", {"x": 5, "y": 5})] * 50, tmp, control=control)
    run = asyncio.create_task(loop.run())
    await asyncio.sleep(0.2)
    control.stop()
    assert (await asyncio.wait_for(run, 5)).status == TaskStatus.STOPPED


async def test_max_steps(env):
    backend, comp, tmp = env
    loop, _ = make_loop(comp, [("move", {"x": 5, "y": 5})] * 50, tmp)
    loop.cfg.max_steps = 3
    t = await loop.run()
    assert t.status == TaskStatus.FAILED and "max steps" in t.result


def test_policy_levels():
    p = PolicyEngine(allow_domains=[])
    L = lambda a, pl: p.check(a, pl).level
    assert L("computer.open_app", {"name": "chrome"}) == RiskLevel.L0
    assert L("files.write", {"path": "notes.txt", "content": "x"}) == RiskLevel.L1
    assert L("shell.exec", {"command": "sudo apt install ffmpeg"}) == RiskLevel.L2
    assert L("shell.exec", {"command": "rm -rf build/"}) == RiskLevel.L3
    assert L("files.upload", {"path": ".ssh/id_rsa", "content_b64": ""}) == RiskLevel.L3
    assert L("shell.exec", {"command": "pvesh get /nodes"}) == RiskLevel.L4
    assert L("shell.exec", {"command": "sudo -i"}) == RiskLevel.L4
    assert L("browser.navigate", {"url": "http://192.168.1.1"}) == RiskLevel.L3
    assert PolicyEngine(allow_domains=["openai.com"]).check("browser.navigate", {"url": "https://evil.com"}).level == RiskLevel.L3


async def test_done_without_action_is_rejected(env):
    backend, comp, tmp = env
    loop, _ = make_loop(comp, [("done", {"summary": "fake weather"}),
                               ("open_app", {"name": "chrome"}),
                               ("done", {"summary": "real"})], tmp)
    loop.task.prompt = "打开浏览器搜索深圳天气"
    t = await loop.run()
    assert t.status == TaskStatus.SUCCEEDED and t.result == "real"


async def test_send_file_delivers_artifact(env):
    backend, comp, tmp = env
    (tmp / "report.docx").write_bytes(b"PK\x03\x04fake-docx")
    events = []
    loop, _ = make_loop(comp, [("send_file", {"path": "report.docx"}), ("done", {"summary": "sent"})], tmp, events=events)
    loop.cfg.artifacts_dir = str(tmp / "artifacts")
    t = await loop.run()
    assert t.status == TaskStatus.SUCCEEDED
    f = [e for e in events if e.type.value == "agent.file"]
    assert f and f[0].data["name"] == "report.docx"
    from pathlib import Path
    assert Path(f[0].data["artifact"]).read_bytes() == b"PK\x03\x04fake-docx"
