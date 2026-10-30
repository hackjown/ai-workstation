"""3.8.9: chat written before the 3.8.8 task card (ccd7a59) shows one bubble per task event (the v1 card, 需要你接管 /
任务卡住了 questions, confirmations, 任务已停止 / results). The chat list now folds each task's old messages into ONE
v2 task card at the place of its first message, built from the task record. Render time only: the database keeps
every old message as it was, and a question somebody still waits on stays answerable."""
import json

from test_chat import FakeChat, stack, wait_for  # noqa: F401  (fixture)


def _task(app, tid, status="stopped", result="任务已停止。用户停止了任务", continues=None):
    st = app.state.store
    st.create_task({"id": tid, "prompt": "【技能:剪视频】把素材剪成 30 秒的短片", "machine_id": "ai-linux-001", "status": status,
                    "continues": continues})
    st.update_task(tid, status, result)


def _rows(app):
    return app.state.store._x("SELECT id, kind, text, data FROM chat_messages ORDER BY seq")


async def test_old_task_messages_fold_into_one_card(stack):
    client, app, scripts, fake = stack
    chat = app.state.chat
    _task(app, "task-old1")
    user = chat.add("user", "text", "帮我剪个视频")
    v1 = chat.add("assistant", "task", "剪视频", {"task_id": "task-old1", "title": "剪视频", "status": "running",
                                               "thread_id": "th-1"}, task_id="task-old1")
    tk = chat.add("assistant", "ask", "需要你接管", {"task_id": "task-old1", "ask_kind": "takeover", "question": "需要你接管",
                                                 "session_id": "gone", "takeover": {"site": "剪映"},
                                                 "resolved": {"answer": "完成", "text": "完成", "at": 1}}, task_id="task-old1")
    stuck = chat.add("assistant", "ask", "任务卡住了", {"task_id": "task-old1", "ask_kind": "escalate", "question": "任务卡住了",
                                                    "options": ["继续，换个方法再试", "我来接管桌面", "停止任务"],
                                                    "session_id": "gone", "stale": True}, task_id="task-old1")
    res = chat.add("assistant", "task_result", "任务已停止。", {"task_id": "task-old1", "status": "stopped", "files": []},
                   task_id="task-old1")
    after = chat.add("assistant", "text", "还有别的吗？")
    before = _rows(app)

    h = (await client.get("/api/chat")).json()["messages"]
    assert [m["id"] for m in h] == [user["id"], v1["id"], after["id"]]      # one card, where the task started
    card = h[1]
    d = card["data"]
    assert card["kind"] == "task" and d["card"] == 2 and d["folded"] is True and d["root_id"] == "task-old1"
    assert d["folded_ids"] == [v1["id"], tk["id"], stuck["id"], res["id"]]
    assert d["status"] == "stopped" and d["status_line"] == "已停止" and d["title"] == "剪视频"
    assert d["prompt"] == "把素材剪成 30 秒的短片"                              # from the task record, skill tag cut
    acts = d["actions"]
    assert [a["ask_kind"] for a in acts] == ["takeover", "escalate"]
    assert acts[0]["resolved"]["text"] == "完成" and acts[1]["stale"] is True
    assert d["result"]["text"] == "任务已停止。" and d["result"]["status"] == "stopped"
    # nothing was folded in the database: every old message is still there, same kind and text (fresh() only keeps
    # the old v1 card's own status in step with the task, as before 3.8.9)
    assert [(r["id"], r["kind"], r["text"]) for r in _rows(app)] == [(r["id"], r["kind"], r["text"]) for r in before]
    assert all("folded" not in json.loads(r["data"]) for r in _rows(app))


async def test_continued_runs_fold_together_and_a_waiting_question_is_kept(stack):
    client, app, scripts, fake = stack
    chat = app.state.chat
    _task(app, "task-r1", "failed", "按钮点不动")
    _task(app, "task-r2", "succeeded", "做好了，文件在 out/a.mp4", continues="task-r1")
    a = chat.add("assistant", "task", "注册", {"task_id": "task-r1", "title": "注册", "status": "failed"}, task_id="task-r1")
    b = chat.add("assistant", "task_result", "任务没有完成：按钮点不动", {"status": "failed", "files": []}, task_id="task-r1")
    c = chat.add("assistant", "task", "注册", {"task_id": "task-r2", "title": "注册", "status": "succeeded"}, task_id="task-r2")
    d = chat.add("assistant", "task_result", "做好了", {"status": "succeeded",
                                                      "files": [{"path": "out/a.png", "kind": "image", "name": "a.png"}]},
                 task_id="task-r2")
    # another task whose question is still open (a live session would answer it): left alone
    _task(app, "task-w", "running", "")
    q = chat.add("assistant", "ask", "做成 Word 还是 PPT？", {"task_id": "task-w", "ask_kind": "question", "question": "做成 Word 还是 PPT？",
                                                        "options": ["Word", "PPT"], "session_id": "s-live"}, task_id="task-w")
    r = chat.add("assistant", "task_result", "…", {"status": "running", "files": []}, task_id="task-w")
    real = chat.fresh

    def keep_open(m):   # pretend the session still waits (fresh() would mark it stale without a live session)
        return m if m["id"] == q["id"] else real(m)
    chat.fresh = keep_open
    try:
        h = (await client.get("/api/chat")).json()["messages"]
    finally:
        chat.fresh = real
    ids = [m["id"] for m in h]
    assert ids == [a["id"], q["id"], r["id"]]
    card = h[0]["data"]
    assert card["folded_ids"] == [a["id"], b["id"], c["id"], d["id"]] and card["runs"] == 2
    assert card["status"] == "succeeded" and card["task_id"] == "task-r2"
    assert card["result"]["text"] == "做好了" and card["result"]["thumb"] == "out/a.png"


async def test_v2_cards_and_messages_without_a_task_are_untouched(stack):
    client, app, scripts, fake = stack
    chat = app.state.chat
    v2 = chat.add("assistant", "task", "新任务", {"title": "新任务", "status": "succeeded", "card": 2, "root_id": "task-x",
                                               "actions": []}, task_id="task-x")
    lone = chat.add("assistant", "ask", "要继续吗？", {"ask_kind": "question", "options": ["是"], "session_id": "gone"})
    h = (await client.get("/api/chat")).json()["messages"]
    assert [m["id"] for m in h] == [v2["id"], lone["id"]]
    assert "folded" not in h[0]["data"] and h[1]["kind"] == "ask"
    assert json.loads(_rows(app)[0]["data"])["card"] == 2
