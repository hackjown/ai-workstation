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


class FakeEyes(ScriptedProvider):
    def __init__(self):
        super().__init__([])
        self.looks = 0

    async def describe(self, system, user, image_b64, max_tokens=900):
        self.looks += 1
        return "Terminal window in front. Search box -> (640, 40)"


async def test_separate_vision_model_reads_screen_for_tool_model(env):
    backend, comp, tmp = env
    eyes = FakeEyes()
    llm = ScriptedProvider([("open_app", {"name": "chrome"}), ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt="打开 Chrome"), "s2", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=5, timeout_s=20, make_plan=False), vision_llm=LLMRouter([eyes]))
    task = await loop.run()
    assert task.status == TaskStatus.SUCCEEDED
    assert eyes.looks >= 1
    req = llm.requests[0]
    assert req.screen_desc and "(640, 40)" in req.screen_desc
    from llm_router.router import user_text
    assert "SCREEN DESCRIPTION" in user_text(req) and "No image is attached" in user_text(req)


async def test_delegate_without_goal_is_bounced(env):
    backend, comp, tmp = env
    llm = ScriptedProvider([("delegate", {}), ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt="看一下"), "s3", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=5, timeout_s=20, make_plan=False))
    await loop.run()
    assert any("delegate needs a non-empty" in h for h in loop.history)


def test_plan_parser_drops_reasoning():
    from agent_runtime.planner import parse_plan
    junk = "The user wants a plan for an agent.\nI need to write 2-5 short steps.\nSteps:\nOpen terminal"
    assert parse_plan(junk, "用 Python 新建 周报.docx") == []
    assert parse_plan('思考…\n["生成 周报.docx", "发给用户"]', "新建周报") == ["生成 周报.docx", "发给用户"]


def test_question_is_not_a_task():
    from agent_runtime.loop import _needs_action
    assert not _needs_action("为什么没执行")
    assert _needs_action("帮我执行 ls")


async def test_stops_after_file_is_sent(env):
    backend, comp, tmp = env
    (tmp / "a.txt").write_text("hi")
    llm = ScriptedProvider([("send_file", {"path": "a.txt"}), ("files_list", {"path": "."}),
                            ("files_list", {"path": "."}), ("files_list", {"path": "."})])
    loop = AgentLoop(Task(prompt="把 a.txt 发给我"), "s4", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=8, timeout_s=20, make_plan=False, artifacts_dir=str(tmp / "art")))
    task = await loop.run()
    assert task.status == TaskStatus.SUCCEEDED and "a.txt" in task.result


async def test_identical_write_is_skipped_and_unchanged_screen_not_resent(env):
    backend, comp, tmp = env
    w = ("files_write", {"path": "m.py", "content": "print(1)"})
    llm = ScriptedProvider([w, w, ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt="写 m.py"), "s5", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=6, timeout_s=20, make_plan=False))
    await loop.run()
    assert any("SKIPPED" in h for h in loop.history)
    assert llm.requests[0].screenshot_b64 and llm.requests[0].screenshot_b64.startswith("/9j/")  # JPEG
    assert llm.requests[1].screenshot_b64 is None and "not re-read" in (llm.requests[1].note or "")  # file step: no screenshot


class FlakyProvider(ScriptedProvider):
    """Fails the first two calls like a gateway timeout (empty message), then follows its script."""
    def __init__(self, script):
        super().__init__(script)
        self.fails = 2

    async def decide(self, req):
        if self.fails:
            self.fails -= 1
            raise TimeoutError()
        return await super().decide(req)


async def test_llm_failure_is_retried_not_fatal(env, monkeypatch):
    backend, comp, tmp = env
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    events = []

    async def sink(e):
        events.append(e)
    llm = FlakyProvider([("open_app", {"name": "chrome"}), ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt="打开 Chrome"), "s1", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=15, timeout_s=20, artifacts_dir=str(tmp / "artifacts")))
    task = await loop.run()
    assert task.status == TaskStatus.SUCCEEDED
    assert sum(1 for e in events if e.type == EventType.ERROR and "模型调用失败" in str(e.data)) == 2


_real_sleep = asyncio.sleep


async def _fast_sleep(t, *a, **k):
    return await _real_sleep(min(t, 0.01), *a, **k)
