"""v3.9.17 items 2 + 9(a)(b): sufficiency, diagnosis budget, stuck detection."""
from agent_runtime.findings import FindingsBoard
from agent_runtime.progress_guard import ProgressGuard, is_diagnostic, is_delivery

P = "ssh 到 docker 服务器，看看是哪些容器占用高，全面分析"


def test_diagnostic_detection():
    assert is_diagnostic(P) and is_diagnostic("排查一下为什么 9210 起不来")
    assert not is_diagnostic("画一张客厅平面图") and not is_diagnostic("帮我在京东下单")


def test_sufficient_then_deliver_next_step():
    g = ProgressGuard(P)
    assert g.decide(3, "先看 CPU", "shell", {"command": "uptime"})[0] == "ok"
    v, note = g.decide(8, "已经查到关键线索：5731 个僵尸进程。再补一次数据", "shell", {"command": "x"})
    assert v == "deliver_now" and "DELIVER NOW" in note
    # the step that said it is the one extra; the next collection is refused
    assert g.decide(9, "还需要补两块证据：分布统计", "shell", {"command": "y"})[0] == "force_done"
    assert "5731" in g.synth_report() and "深挖" in g.synth_report()


def test_one_extra_collection_only_when_it_names_what_is_missing():
    g = ProgressGuard(P)
    g.board.add("5700 个僵尸进程", 2)
    v, _ = g.decide(5, "已掌握大部分数据。", "done", {"summary": "x"})   # delivering already
    assert v == "deliver_now"
    assert g.decide(6, "还缺一组数据：僵尸增长速率", "shell", {"command": "z"})[0] == "extra"
    assert g.decide(7, "再补一组", "shell", {"command": "w"})[0] == "force_done"
    assert g.decide(8, "", "files_write", {"path": "report.md", "content": "# 报告"})[0] == "ok"


def test_budget_8_steps_or_10_minutes(monkeypatch):
    g = ProgressGuard(P)
    assert g.decide(7, "看日志", "shell", {})[0] == "ok"
    assert g.decide(8, "看日志", "shell", {})[0] == "deliver_now" and g.reason == "budget"
    g2 = ProgressGuard(P)
    assert g2.decide(3, "看日志", "shell", {}, elapsed_s=601)[0] == "deliver_now"
    monkeypatch.setenv("AIW_DIAG_STEPS", "0")
    monkeypatch.setenv("AIW_DIAG_SECONDS", "0")
    g3 = ProgressGuard(P)
    for i in range(1, 60):
        g3.board.add(__import__("hashlib").sha1(str(i).encode()).hexdigest(), i)
        g3.on_fact(i)
        assert g3.decide(i, "", "shell", {})[0] == "ok"
    assert g3.decide(60, "", "shell", {})[0] == "deliver_now" and g3.reason == "soft_cap"


def test_same_conclusion_three_times_asks(monkeypatch):
    # v3.9.20: an action task restating a fact is not stuck; a question task asks after AIW_STUCK_MIN_STEPS steps
    # and AIW_SAME_FACT_LIMIT (5) repeats
    monkeypatch.setenv("AIW_DIAG_STEPS", "0")
    g = ProgressGuard("帮我把这个网页的价格抓下来写成表格", question=False)
    t = "发现 价格接口 api/price 返回 403"
    for i in range(1, 8):
        assert g.decide(i, t, "shell", {})[0] == "ok"
    monkeypatch.setenv("AIW_STUCK_MIN_STEPS", "3")
    q = ProgressGuard("帮我看看价格接口为什么 403", question=True)
    q.min_steps = 99
    for i in range(1, 5):
        assert q.decide(i, t, "shell", {})[0] == "ok"
    v, why = q.decide(5, t, "shell", {})
    assert v == "ask" and "5" in why
    q.reset_after_ask(5)
    assert q.decide(6, "换个方法", "shell", {})[0] == "ok"


def test_twenty_steps_without_a_new_fact_asks(monkeypatch):
    monkeypatch.setenv("AIW_DIAG_STEPS", "0")
    monkeypatch.setenv("AIW_DIAG_SECONDS", "0")
    g = ProgressGuard(P)
    for i in range(1, 20):
        assert g.decide(i, "继续", "shell", {})[0] == "ok"
    assert g.decide(20, "继续", "shell", {})[0] == "ask"
    # visible progress (a new page, a plan tick) counts as something new
    g2 = ProgressGuard(P)
    g2.step_budget = g2.time_budget = 0
    assert g2.decide(20, "继续", "shell", {}, progress_at=15)[0] == "ok"
    # a long action task is not stopped by this rule (its valve is the 150-step check-in)
    g3 = ProgressGuard("帮我整理下载目录", question=False)
    assert all(g3.decide(i, "继续", "shell", {})[0] == "ok" for i in range(1, 46))


def test_action_tasks_are_not_cut_short():
    g = ProgressGuard("帮我在京东下单 iPhone")
    g.board.add("已确认登录成功 jd", 1)
    assert g.decide(2, "已确认登录成功，接着下单", "bos_act", {})[0] == "ok"


def test_delivery_tools():
    assert is_delivery("done") and is_delivery("files_write", {"path": "r.md", "content": "x"})
    assert not is_delivery("files_write", {"path": "x.py", "content": "import paramiko"})
