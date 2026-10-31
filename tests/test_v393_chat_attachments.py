"""v3.9.3: files shared into the chat reach the computer, whether the message goes straight to a task or the
reply offers 交给电脑去做 first."""
from aiw_api.chat import attachments_want_task
from test_chat import FakeChat, stack, wait_for  # noqa: F401  (the same app + fake model)


def test_attachment_rule():
    att = [{"name": "plan.png"}]
    assert attachments_want_task("帮我把这张图描成 CAD", att)
    assert attachments_want_task("转成 PDF", att)
    assert attachments_want_task("按这个文件去填表", att)
    assert not attachments_want_task("这是什么", att)
    assert not attachments_want_task("总结一下", att)
    assert not attachments_want_task("帮我把这张图描成 CAD", [])


def _upload(tmp_path, name="plan.png"):
    up = tmp_path / "artifacts" / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    f = up / f"1700000000-{name}"
    f.write_bytes(b"\x89PNG fake")
    return {"name": name, "artifact": str(f)}


async def test_attachment_with_action_goes_straight_to_the_computer(stack, tmp_path):
    client, app, scripts, fake = stack
    fake.route = "chat"                      # the classifier would have said chat: the rule must win
    scripts.append([("done", {"summary": "好了"})])
    r = (await client.post("/api/chat/send", json={"text": "帮我转成 DXF", "attachments": [_upload(tmp_path)],
                                                   "stream": False})).json()
    route = [e["data"] for e in r["events"] if e["event"] == "route"][0]
    card = r["messages"][-1]
    assert route["route"] == "task" and card["kind"] == "task"
    assert not [c for c in fake.completes if "ROUTE" in c[0]]     # decided without asking the classifier


async def test_offer_button_carries_the_attachments(stack, tmp_path):
    client, app, scripts, fake = stack
    fake.replies = ["这得在电脑上做。\n[[交给电脑:把附件里的户型图描成 CAD]]"]
    att = _upload(tmp_path)
    r = (await client.post("/api/chat/send", json={"text": "看看这张图", "attachments": [att],
                                                   "stream": False})).json()
    m = r["messages"][-1]
    assert m["data"]["offer_task"] and m["data"]["offer_attachments"][0]["name"] == "plan.png"
    seen = {}
    real = app.state.chat.start_task

    async def spy(prompt, title="", machine_id=None, attachments=None, **kw):
        seen["att"] = attachments
        return await real(prompt, title, machine_id, attachments, **kw)
    app.state.chat.start_task = spy
    scripts.append([("done", {"summary": "好了"})])
    card = (await client.post(f"/api/chat/messages/{m['id']}/to_task", json={})).json()
    assert card["kind"] == "task" and seen["att"] and seen["att"][0]["name"] == "plan.png"


async def test_old_offer_without_stored_files_finds_the_user_message(stack, tmp_path):
    client, app, scripts, fake = stack
    fake.replies = ["这得在电脑上做。\n[[交给电脑:处理附件]]"]
    r = (await client.post("/api/chat/send", json={"text": "看看这张图", "attachments": [_upload(tmp_path)],
                                                   "stream": False})).json()
    m = r["messages"][-1]
    chat = app.state.chat
    d = dict(m["data"]); d.pop("offer_attachments", None)
    chat.store._x("UPDATE chat_messages SET data=? WHERE id=?", (__import__("json").dumps(d), m["id"]))
    assert chat._attachments_before(chat.get(m["id"]))[0]["name"] == "plan.png"
