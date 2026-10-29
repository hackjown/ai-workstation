"""3.8.2: delete one chat message (DELETE /api/chat/messages/{id}), and 删除 of a queued message really removes it:
the home card follows (it said 排队中 forever) and the task view no longer lists it."""
import asyncio
import pathlib

from test_chat import stack, wait_for  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUSY = [("keypress", {"key": f"F{i % 12 + 1}"}) for i in range(300)]


async def _msgs(client):
    return (await client.get("/api/chat")).json()["messages"]


async def test_delete_one_text_message(stack):  # noqa: F811
    client, app, scripts, fake = stack
    fake.replies = ["第一条回复", "第二条回复"]
    await client.post("/api/chat/send", json={"text": "你好", "stream": False})
    await client.post("/api/chat/send", json={"text": "再说一句", "stream": False})
    h = await _msgs(client)
    first = next(m for m in h if m["text"] == "第一条回复")
    r = await client.delete(f"/api/chat/messages/{first['id']}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    h2 = await _msgs(client)
    assert all(m["id"] != first["id"] for m in h2)
    assert {m["text"] for m in h2} >= {"你好", "再说一句", "第二条回复"}     # only that one is gone
    assert (await client.delete(f"/api/chat/messages/{first['id']}")).status_code == 404


async def test_delete_queued_task_card_cancels_it_and_running_is_refused(stack):  # noqa: F811
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append(BUSY)
    r1 = (await client.post("/api/chat/send", json={"text": "一直忙", "stream": False})).json()
    busy_card = r1["messages"][-1]

    async def running():
        return next((m for m in await _msgs(client) if m["id"] == busy_card["id"]
                     and m["data"].get("status") == "running"), None)
    await wait_for(running, 10)
    assert (await client.delete(f"/api/chat/messages/{busy_card['id']}")).status_code == 409   # stop it first

    r2 = (await client.post("/api/chat/send", json={"text": "排队的那个", "stream": False})).json()
    q_card = r2["messages"][-1]
    q_tid = q_card["data"]["task_id"]
    assert (await client.get(f"/api/tasks/{q_tid}")).json()["status"] == "queued"
    d = (await client.delete(f"/api/chat/messages/{q_card['id']}")).json()
    assert d["deleted"] and d["cancelled_task"] is True
    assert (await client.get(f"/api/tasks/{q_tid}")).json()["status"] == "cancelled"
    assert all(m["id"] != q_card["id"] for m in await _msgs(client))
    await client.delete(f"/api/tasks/{busy_card['data']['task_id']}")


async def test_delete_from_task_view_updates_the_home_card(stack):  # noqa: F811
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append(BUSY)
    r1 = (await client.post("/api/chat/send", json={"text": "一直忙", "stream": False})).json()
    r2 = (await client.post("/api/chat/send", json={"text": "排队的", "stream": False})).json()
    q_card = r2["messages"][-1]
    q_tid = q_card["data"]["task_id"]
    for _ in range(50):
        if (await client.get(f"/api/tasks/{q_tid}")).json()["status"] == "queued":
            break
        await asyncio.sleep(0.1)
    assert (await client.delete(f"/api/tasks/{q_tid}")).json() == {"cancelled": True}   # the task view's 删除

    async def card_cancelled():
        return next((m for m in await _msgs(client) if m["id"] == q_card["id"]
                     and m["data"].get("status") == "cancelled"), None)
    c = await wait_for(card_cancelled, 5)
    assert c["data"]["status_line"] == "已从排队中删除"
    await client.delete(f"/api/tasks/{r1['messages'][-1]['data']['task_id']}")


def test_clients_have_delete_and_drop_cancelled():
    js = (ROOT / "apps/web/home.js").read_text()
    assert 'api("DELETE", `/api/chat/messages/' in js and 'e.op === "delete"' in js and "hDelBtns(box)" in js
    hc = (ROOT / "apps/ios/Sources/HomeChat.swift").read_text()
    assert '"DELETE", "/api/chat/messages/' in hc and 'e.s("op") == "delete"' in hc
    assert "aiwDeleteMessage" in hc and "HRowKey" in hc
    # one dialog for queued messages at the root, not one per lazy row
    assert hc.count('confirmationDialog("这条还在排队"') == 1 and "presenting: queuedPick" in hc
    st = (ROOT / "apps/ios/Sources/SelectableText.swift").read_text()
    assert 'UIAction(title: "删除"' in st and 'Label("删除", systemImage: "trash")' in st
    cv = (ROOT / "apps/ios/Sources/ChatView.swift").read_text()
    assert 'filter { $0.s("status") != "cancelled" }' in cv
