"""v3.9.17 item 1: 已确认事实板."""
from agent_runtime.findings import FindingsBoard, conclusions, says_sufficient, names_missing


def test_add_dedupe_and_block_every_turn():
    b = FindingsBoard()
    assert b.add("clipboard-online 容器内 uvicorn 产生约 5700 个僵尸进程", 8)[0] == "added"
    assert b.add("clipboard-online 容器里 uvicorn 产生约 5700 个僵尸进程", 9)[0] == "dup"
    assert b.max_repeats() == 1
    blk = b.block()
    assert "CONFIRMED FINDINGS" in blk and "F1" in blk and "5700" in blk


def test_overturn_needs_evidence():
    b = FindingsBoard()
    b.add("clouddrive2 占 50% CPU", 6)
    st, old = b.add("clouddrive2 只占 5% CPU", 9, replaces="F1")
    assert st == "refused" and not old.get("replaced_by")
    st, new = b.add("clouddrive2 只占 5% CPU", 9, replaces="F1", evidence="连续 10 次 docker stats 平均 4.8%")
    assert st == "replaced" and new["replaces"] == "F1"
    live = b.live()
    assert [i["id"] for i in live] == ["F2"]
    assert "证据" in b.block()


def test_auto_capture_from_thoughts():
    t = "已经查到关键线索：负载 19~21、5731 个僵尸进程、clipboard-online 一个容器就有 5619 个 PID。再补一次数据：僵尸进程的来源进程"
    cs = conclusions(t)
    assert cs and "5731" in cs[0] and "再补" not in cs[0]
    assert conclusions("已经拿到整体数据了，但有几个关键点还没查清") == []   # negated
    assert conclusions("再补一次数据，用来确定根因") == []                      # a plan
    assert says_sufficient("现状已基本查清：机器是 4 核") and not says_sufficient("还没查清楚")
    assert names_missing("还需要补两块证据：僵尸进程的完整分布") and not names_missing("直接出报告")


def test_board_survives_history_trimming_in_task_text():
    """The board rides in the task text (never trimmed), not in the step history."""
    from agent_runtime import AgentLoop
    from aiw_protocols import Task

    class C:  # noqa: D401
        pass
    loop = AgentLoop.__new__(AgentLoop)
    loop.findings = FindingsBoard()
    loop.findings.add("aiw-api 下约 60 个僵尸进程", 3)
    assert "aiw-api" in loop.findings.block()
