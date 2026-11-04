"""No silent failures (task-d76ffb0bd1: a captcha, 32 steps of looking, then "max steps (40) reached")."""
from agent_runtime import escalate as es
from aiw_protocols import EventType, TaskStatus
from test_phase5_agent_loop import env, make_loop  # noqa: F401


def test_d76_sequence_asks_for_a_takeover_at_the_captcha():
    s = es.Stall()
    seq = [("bos_tabs", {"action": "list"}, ""), ("bos_tabs", {"action": "new", "url": "https://chat.qwen.ai/"}, "Sign up"),
           ("bos_snapshot", {"page": 3}, "Sign up"), ("bos_act", {"kind": "click", "ref": "e7"}, "Email"),
           ("bos_act", {"kind": "fill"}, "Email"), ("bos_act", {"kind": "click", "ref": "e5"}, "Continue"),
           ("bos_snapshot", {"page": 3}, "Continue"), ("bos_snapshot", {"page": 3}, 'button "刷新验证码" image'),
           ("bos_tabs", {"action": "front", "page": 3}, "")]
    got = [s.feed(t, a, r) for t, a, r in seq]
    assert got[:8] == [None] * 8 and got[8] == "blocked" and s.blocked == "刷新验证码"


def test_reloading_the_wall_page_is_not_progress():
    s = es.Stall()
    assert s.feed("bos_snapshot", {}, "请完成安全验证 滑块") is None
    assert s.feed("bos_navigate", {"url": "x"}, "请完成安全验证 滑块") == "blocked"


def test_looking_without_acting_nudges_then_asks():
    s = es.Stall()
    out = [s.feed("screenshot", {}, "") for _ in range(es.STALL_ESCALATE)]
    assert out[es.STALL_NUDGE - 1] == "nudge" and out[-1] == "stall"
    s.reset(); s.feed("screenshot", {}, ""); s.feed("shell", {"command": "ls"}, "")
    assert s.looks == 0


def test_choice_and_report():
    assert es.choice(es.OPT_STOP) == "stop" and es.choice("我来接管") == "takeover" and es.choice(None) == "none"
    assert es.choice("换成手机号注册试试") == "retry"
    r = es.report("max_steps", "已经执行了 40 步", ["1. bos_act(click) -> ok", "2. screenshot() -> ok"],
                  {"url": "https://chat.qwen.ai/auth?mode=register", "title": "Qwen"}, "刷新验证码")
    assert "刷新验证码" in r and "chat.qwen.ai" in r and "bos_act" in r and "screenshot" not in r


async def test_fail_becomes_a_question_and_stop_ends_with_a_report(env):  # noqa: F811
    backend, comp, tmp = env
    events, asked = [], []
    loop, _ = make_loop(comp, [("fail", {"reason": "按钮点不动"})], tmp, events=events)

    async def ask(q, opts, extra=None):
        asked.append((q, opts)); return es.OPT_STOP
    loop.ask = ask
    t = await loop.run()
    assert asked and "按钮点不动" in asked[0][0] and asked[0][1] == es.options("fail")
    assert t.status == TaskStatus.STOPPED and "按你的选择已停止" in t.result
    assert any(e.type == EventType.INPUT_REQUIRED for e in events)


async def test_out_of_steps_asks_and_continues_when_told(env):  # noqa: F811
    backend, comp, tmp = env
    loop, _ = make_loop(comp, [("move", {"x": 5, "y": 5}), ("move", {"x": 6, "y": 6}), ("move", {"x": 7, "y": 7}),
                               ("done", {"summary": "ok"})], tmp)
    loop.cfg.max_steps = 2
    asked = []

    async def ask(q, opts, extra=None):
        asked.append(q); return es.OPT_RETRY
    loop.ask = ask
    t = await loop.run()
    assert asked and "步数用完" in asked[0]
    assert t.status == TaskStatus.SUCCEEDED, t.result
