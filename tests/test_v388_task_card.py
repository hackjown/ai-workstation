"""3.8.8: a task is ONE card in the 小秘 chat. Questions, takeovers and confirmations are actions on the card
(answered by action_id, collapsed to ✓ when answered), the result (summary, files, thumbnail) is on the card, a
retry / continue re-arms the same card and moves it to the bottom, and old-style messages still render and answer."""
import asyncio

from test_chat import FakeChat, stack, wait_for  # noqa: F401  (fixture)


async def _card(client, mid=None):
    h = (await client.get("/api/chat")).json()["messages"]
    return next(m for m in h if m["kind"] == "task" and (mid is None or m["id"] == mid))


async def _open_action(client, mid, kind=None):
    async def f():
        c = await _card(client, mid)
        return next((a for a in c["data"].get("actions") or [] if not a.get("resolved") and not a.get("stale")
                     and (kind is None or a["kind"] == kind)), None)
    return await wait_for(f, 10)


async def test_confirmation_is_an_action_on_the_card_and_result_has_a_thumbnail(stack):
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("shell", {"command": "rm ~/workspace/uploads/a.pdf"}),
                    ("files_write", {"path": "out/pic.png", "content": "png"}),
                    ("send_file", {"path": "out/pic.png"}),
                    ("done", {"summary": "推送好了，截图发你了。"})])
    r = (await client.post("/api/chat/send", json={"text": "把代码推上去，截图给我", "stream": False})).json()
    card = r["messages"][-1]
    assert card["data"]["card"] == 2 and card["data"]["root_id"] == card["task_id"] and card["data"]["prompt"]
    act = await _open_action(client, card["id"], "confirm")
    assert act["summary"]["title"] and act["session_id"]
    c = await _card(client, card["id"])
    assert c["data"]["status"] == "waiting_confirm"
    ok = (await client.post(f"/api/chat/messages/{card['id']}/answer", json={"action_id": act["id"], "approve": True})).json()
    assert ok["ok"] and ok["action_id"] == act["id"]

    async def done():
        c = await _card(client, card["id"])
        return c if c["data"].get("result") else None
    c = await wait_for(done, 15)
    res = c["data"]["result"]
    assert res["status"] == "succeeded" and res["text"].startswith("推送好了")
    assert res["thumb"] and res["thumb"].endswith("pic.png") and res["files"][0]["kind"] == "image"
    a = next(x for x in c["data"]["actions"] if x["id"] == act["id"])
    assert a["resolved"]["text"] == "已允许"
    h = (await client.get("/api/chat")).json()["messages"]
    assert [m["kind"] for m in h if m["role"] == "assistant"] == ["task"]
    # the chat model still sees how the task ended
    assert "推送好了" in app.state.chat.render(c)


async def test_a_plain_chat_reply_answers_the_cards_question(stack):
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("ask_user", {"question": "做成 Word 还是 PPT？", "options": ["Word", "PPT"]}),
                    ("done", {"summary": "做好了 PPT"})])
    r = (await client.post("/api/chat/send", json={"text": "把资料整理成文档", "stream": False})).json()
    card = r["messages"][-1]
    act = await _open_action(client, card["id"], "ask")
    assert act["options"] == ["Word", "PPT"] and act["ask_kind"] == "question"
    out = await app.state.chat.send("PPT", [], None, "auto")
    assert out["route"] == "answer" and out["messages"][0]["id"] == card["id"]

    async def done():
        c = await _card(client, card["id"])
        return c if (c["data"].get("result") or {}).get("status") == "succeeded" else None
    c = await wait_for(done, 15)
    a = next(x for x in c["data"]["actions"] if x["id"] == act["id"])
    assert a["resolved"]["answer"] == "PPT"


async def test_retry_reuses_the_same_card_and_moves_it_down(stack):
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("fail", {"reason": "按钮点不动"})])
    r = (await client.post("/api/chat/send", json={"text": "去网站上注册", "stream": False})).json()
    card = r["messages"][-1]
    act = await _open_action(client, card["id"])
    await client.post(f"/api/chat/messages/{card['id']}/answer", json={"action_id": act["id"], "choice": "停止任务"})

    async def ended():
        c = await _card(client, card["id"])
        return c if c["data"].get("result") else None
    c1 = await wait_for(ended, 10)
    app.state.chat.add("assistant", "text", "别的话题")
    scripts.append([("done", {"summary": "注册好了"})])
    rr = (await client.post(f"/api/tasks/{card['task_id']}/retry")).json()

    async def again():
        c = await _card(client, card["id"])
        return c if c["task_id"] == rr["task"]["id"] and (c["data"].get("result") or {}).get("status") == "succeeded" else None
    c2 = await wait_for(again, 15)
    assert c2["seq"] > c1["seq"] and c2["data"]["runs"] == 2 and c2["data"]["result"]["text"].startswith("注册好了")
    h = (await client.get("/api/chat")).json()["messages"]
    assert [m["kind"] for m in h if m["role"] == "assistant"] == ["text", "task"]   # the card is the newest again
    # the answered escalation of the first run collapsed to a ✓ line and stays
    assert any((a.get("resolved") or {}).get("answer") == "停止任务" for a in c2["data"]["actions"])


async def test_old_style_messages_still_render_and_answer(stack):
    client, app, scripts, fake = stack
    chat = app.state.chat
    old = chat.add("assistant", "ask", "要继续吗？", {"ask_kind": "question", "options": ["是"], "session_id": "gone"})
    res = chat.add("assistant", "task_result", "做完了", {"status": "succeeded", "files": []})
    h = (await client.get("/api/chat")).json()["messages"]
    assert [m["kind"] for m in h] == ["ask", "task_result"]
    assert next(m for m in h if m["id"] == old["id"])["data"]["stale"]   # nobody waits: shown as 已过期, never rewritten
    assert next(m for m in h if m["id"] == res["id"])["text"] == "做完了"
    r = await client.post(f"/api/chat/messages/{old['id']}/answer", json={"choice": "是"})
    assert r.status_code == 409


async def test_unknown_action_is_404_and_bump_keeps_ids(stack):
    client, app, scripts, fake = stack
    chat = app.state.chat
    c = chat.add("assistant", "task", "任务", {"title": "任务", "status": "succeeded", "card": 2, "actions": []})
    t = chat.add("assistant", "text", "后来的话")
    r = await client.post(f"/api/chat/messages/{c['id']}/answer", json={"action_id": "act-nope", "choice": "x"})
    assert r.status_code == 404
    b = chat.bump(c["id"])
    assert b["id"] == c["id"] and b["seq"] > t["seq"]
    n = chat.add("assistant", "text", "再后来")
    assert n["seq"] > b["seq"]          # autoincrement never reuses the bumped seq
    await asyncio.sleep(0)


def test_web_and_ios_render_the_card_v2():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    js = (root / "apps/web/home.js").read_text(encoding="utf-8")
    css = (root / "apps/web/home.css").read_text(encoding="utf-8")
    assert "function htcAction(" in js and "action_id" in js and "hContinue(" in js and "htc-thumb" in js
    assert "moved" in js and ".hcard.htc" in css
    v2 = (root / "apps/ios/Sources/TaskCardV2.swift").read_text(encoding="utf-8")
    home = (root / "apps/ios/Sources/HomeChat.swift").read_text(encoding="utf-8")
    assert "struct CardActionView" in v2 and "\"action_id\"" in v2 and "AsyncImage" in v2 and "TimelineView" in v2
    assert "continueTask(" in home and "cardAsk" in home and "sortMsgs(); bottomTick" in home
    for k in ("运行中", "等你", "已完成", "失败", "已停止"):
        assert k in v2 and k in js
