"""3.8.2: no step / time budget (task-c5a94187a0 ended at 40 steps half way through a Fliggy comparison).
A task never ends itself: whatever would have ended it pauses and asks; only 停止 ends it.
A soft valve asks after AIW_TASK_CHECKIN_STEPS steps without progress."""
from agent_runtime import AgentLoop, LoopConfig
from agent_runtime import escalate as es
from aiw_protocols import Task, TaskStatus
from llm_router import LLMRouter, ScriptedProvider
from tool_router import ToolExecutor
from test_phase5_agent_loop import env  # noqa: F401


def _loop(comp, script, tmp, **cfg):
    llm = ScriptedProvider(script)
    return AgentLoop(Task(prompt="打开 Chrome，比价"), "s-nl", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(artifacts_dir=str(tmp / "a"), make_plan=False, **cfg))


def test_defaults_have_no_budget(monkeypatch):
    monkeypatch.delenv("AIW_TASK_MAX_STEPS", raising=False)
    monkeypatch.delenv("AIW_TASK_TIMEOUT", raising=False)
    assert es.step_cap() == 0 and es.time_cap() == 0
    assert Task(prompt="x").max_steps == 0 and Task(prompt="x").timeout_s == 0
    assert LoopConfig().max_steps == 0 and LoopConfig().timeout_s == 0
    monkeypatch.setenv("AIW_TASK_MAX_STEPS", "300")
    assert es.step_cap() == 300
    monkeypatch.setenv("AIW_TASK_CHECKIN_STEPS", "0")
    assert es.checkin_steps() == 0


def test_skill_bumps_keep_no_budget():
    from aiw_api.sessions import _budget
    assert _budget("用 LibreCAD 画施工图", 0, 0) == (0, 0.0)
    assert _budget("把这个 docx 改一下", 0, 0) == (0, 0.0)
    assert _budget("把这个 docx 改一下", 40, 900)[0] >= 80     # an explicit budget is still raised per skill


async def test_runs_past_40_steps_without_asking(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AIW_TASK_CHECKIN_STEPS", "150")
    backend, comp, tmp = env
    script = [("move", {"x": 10 + i, "y": 10 + (i % 7)}) for i in range(45)] + [("done", {"summary": "比完价了"})]
    lp = _loop(comp, script, tmp)
    asked = []

    async def ask(q, opts, extra=None):
        asked.append(q); return es.OPT_STOP
    lp.ask = ask
    t = await lp.run()
    assert t.status == TaskStatus.SUCCEEDED, t.result
    assert lp.step > 40 and not asked


async def test_no_progress_valve_pauses_and_continues(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AIW_TASK_CHECKIN_STEPS", "5")
    backend, comp, tmp = env
    script = [("move", {"x": 10 + i, "y": 20 + i}) for i in range(7)] + [("done", {"summary": "ok"})]
    lp = _loop(comp, script, tmp)
    asked = []

    async def ask(q, opts, extra=None):
        asked.append(q); return es.OPT_RETRY
    lp.ask = ask
    t = await lp.run()
    assert t.status == TaskStatus.SUCCEEDED, t.result
    assert asked and "没有明显进展" in asked[0] and "不会自己结束" in asked[0]


async def test_unanswered_question_keeps_waiting_until_stop(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AIW_TASK_CHECKIN_STEPS", "0")
    backend, comp, tmp = env
    lp = _loop(comp, [("fail", {"reason": "登录不了"})], tmp)
    calls = []

    async def ask(q, opts, extra=None):   # an ask channel that times out twice, then the user presses 停止
        calls.append(q)
        return None if len(calls) < 3 else es.OPT_STOP
    lp.ask = ask
    t = await lp.run()
    assert len(calls) == 3 and t.status == TaskStatus.STOPPED       # never FAILED on its own


def test_stuck_card_says_what_and_what_to_do():
    c = es.stuck_card("max_steps", "已经执行了 40 步")
    assert c["actions"] == ["继续", "接管", "停止"] and "步数" in c["reason"] and "继续 / 接管 / 停止" in c["line"]
    c = es.stuck_card("blocked", "", "滑块")
    assert "滑块" in c["reason"]
