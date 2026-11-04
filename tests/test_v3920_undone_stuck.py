"""v3.9.20: task-62c (继续深挖 delivered at step 1 from the last run's facts, then a give-up report) and the stuck
popup's 继续执行（没卡住） choice."""
from agent_runtime import escalate as es
from agent_runtime.findings import FindingsBoard
from agent_runtime.loop import _undone, _undone_card
from agent_runtime.progress_guard import ProgressGuard, is_diagnostic

P62C = ("【技能：reverse-engineer-anything】\nkanju1.ai去看看这个网址，能不能复刻这个网站\n\n"
        "【接着上次继续做】用户补充：继续深挖")
THOUGHT = "上一轮已经确认 4K 两路是 resolve:// 票据，需要在服务端复现解析链路。我先查看工作区现状。"


def test_clone_is_not_a_diagnosis():
    assert not is_diagnostic(P62C)
    assert not is_diagnostic("kanju1.ai去看看这个网址，能不能复刻这个网站")
    assert is_diagnostic("查一下服务器为什么慢")


def test_continuation_does_not_deliver_from_inherited_facts():
    board = FindingsBoard()
    board.add("4K 两路都是 resolve:// 票据", 3, "model")
    g = ProgressGuard("查一下 4K 为什么播不了\n【接着上次继续做】用户补充：继续", board, question=True)
    g.inherit()
    v, _ = g.decide(1, THOUGHT, "shell", {"command": "ls"})
    assert v == "ok"


def test_deep_ask_never_cut_short():
    board = FindingsBoard()
    board.add("x 是主因", 1, "model")
    g = ProgressGuard("排查一下为什么慢，继续深挖", board, question=True)
    for step in range(1, 12):
        v, _ = g.decide(step, "已查清：主因是 x", "shell", {"command": "ls"}, elapsed_s=step * 60)
        assert v in ("ok",), (step, v)


def test_sufficient_waits_min_steps():
    board = FindingsBoard()
    g = ProgressGuard("排查一下服务器为什么慢", board, question=True)
    v, _ = g.decide(1, "已查清：主因是 CPU 被 java 占满", "shell", {"command": "top"})
    assert v == "ok"
    v, _ = g.decide(4, "已查清：主因是 CPU 被 java 占满", "shell", {"command": "top"})
    assert v == "deliver_now"


def test_undone_summary_and_card():
    s = ("伟少，我如实汇报：**方案A这一轮没达到验收标准**。\n\n## 三、建议怎么处理\n\n"
         "**路线 1（推荐）：在服务端复现 resolve 调用，而不是前端猜地址。**\n...\n"
         "**路线 2（立即见效）：4K 线路标记为「需原站解析」。**\n")
    assert _undone(s)
    q, opts = _undone_card(s)
    assert "还没完成" in q
    assert opts[0].startswith("路线 1") and opts[1].startswith("路线 2")
    assert opts[-1] == "停止并出报告"
    assert not _undone("已全部完成，测试 12 项通过。")


def test_stuck_popup_has_go_on():
    assert es.options("stall")[0] == es.OPT_GO
    assert es.OPT_GO in es.options("checkin") and es.OPT_GO in es.options("stuck_fact")
    assert es.OPT_GO not in es.options("blocked")
    assert es.choice(es.OPT_GO) == "go" and es.choice("继续执行") == "go" and es.choice("继续") == "go"
    assert es.choice(es.OPT_RETRY) == "retry" and es.choice("换个方法") == "retry"
    assert es.choice("停止任务") == "stop"
    assert "继续执行" in es.stuck_card("stall")["actions"]


def test_ios_send_survives_background():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "apps/ios/Sources/HomeChat.swift").read_text()
    assert "beginBackgroundTask" in src and "recoverAfterDrop" in src
    assert "case \"user\": accepted = true" in src
