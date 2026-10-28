"""v2 home (2026-10-28): 球球播报 (/api/greeting), stop a chat reply (/api/chat/cancel), status lines while 球球 thinks,
a "/" skill goes to the computer, raw uploads (videos) and pushing big files to a computer in pieces."""
import asyncio
import base64
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import greeting, uploads
from test_chat import FakeChat, sse, stack, wait_for  # noqa: F401  (fixture)
from vm_agent.files import transfer


# ------------------------------------------------------------------------------------------ greeting
async def test_greeting_orders_what_needs_the_user_first(stack):
    client, app, _, _ = stack
    st = app.state.store
    st.kv_set("profile", {"name": "伟少"})
    now = time.time()
    a = st.create_thread("Qwen 注册")
    st.create_task({"id": "t-fail", "prompt": "继续完成注册", "machine_id": "ai-linux-001", "status": "failed", "thread_id": a["id"]})
    st._x("UPDATE tasks SET result='卡在验证码' WHERE id='t-fail'")
    b = st.create_thread("确认下单")
    st.create_task({"id": "t-wait", "prompt": "下单", "machine_id": "ai-linux-001", "status": "waiting_confirm", "thread_id": b["id"]})
    c = st.create_thread("周报")
    st.create_task({"id": "t-ok", "prompt": "写周报", "machine_id": "ai-linux-001", "status": "succeeded", "thread_id": c["id"]})
    st.create_task({"id": "t-xhs", "prompt": "养号", "machine_id": "ai-linux-001", "status": "failed", "source": "xhs"})
    st.insert("routines", {"name": "晚间新闻", "prompt": "x", "schedule": "daily 23:59", "enabled": 1, "next_run": now + 60})
    st.kv_set("chat_summary", {"text": "用户在规划杭州两日游。还提到了预算。", "upto": 0})
    g = (await client.get("/api/greeting")).json()
    assert g["address"] == "伟少"
    texts = [i["text"] for i in g["items"]]
    prios = [i["prio"] for i in g["items"]]
    assert prios == sorted(prios)
    assert "确认下单" in texts[0] and g["items"][0]["mood"] == "35" and g["items"][0]["action"] == {"type": "thread", "thread_id": b["id"]}
    fail = next(i for i in g["items"] if i["prio"] == 1)
    assert "卡在验证码" in fail["text"] and fail["mood"] == "34" and fail["action"]["type"] == "retry" and fail["action"]["task_id"] == "t-fail"
    assert not any("养号" in t for t in texts)                    # background 小红书 sessions are not briefed
    assert any("周报" in t and "做完了" in t for t in texts)
    if time.strftime("%Y-%m-%d", time.gmtime(now + 8 * 3600)) == time.strftime("%Y-%m-%d", time.gmtime(now + 60 + 8 * 3600)):
        assert any("晚间新闻" in t for t in texts)
    assert any(t.startswith("上次聊到：你在规划杭州两日游") for t in texts)
    assert texts[-1].startswith("伟少，") and g["items"][-1]["action"] == {"type": "focus"}
    assert g["chips"][-1]["label"] == "💬 随便聊聊" and 2 <= len(g["chips"]) <= 4


def test_greeting_address_from_memory_and_empty_state(tmp_path):
    from aiw_api.store import Store
    st = Store(str(tmp_path / "g.db"))
    g = greeting.build(st)
    assert g["address"] == "" and len(g["items"]) == 1 and g["items"][0]["mood"] == "02"
    class Mem:
        def items(self, kind=None):
            return [{"text": "用户希望被称为「伟少」"}]
    assert greeting.address(st, Mem()) == "伟少"


# ------------------------------------------------------------------------------------------ stop, status, skills
class SlowFake(FakeChat):
    async def chat(self, messages, tools=None, max_tokens=1500, on_delta=None):
        if self.tool_first and tools and not any(m.get("role") == "tool" for m in messages):
            name, args = self.tool_first
            return {"content": "", "tool_calls": [{"id": "c1", "name": name, "args": args}]}
        if on_delta:
            await on_delta("我先查")
        await asyncio.sleep(5)
        return {"content": "我先查一下，结果是…", "tool_calls": []}


async def test_stop_keeps_what_streamed_and_says_stopped(stack):
    client, app, _, _ = stack
    chat = app.state.chat
    slow = SlowFake()
    chat.llm_factory = lambda role: __import__("llm_router").LLMRouter([slow])
    assert (await client.post("/api/chat/cancel")).json() == {"cancelled": False}
    send = asyncio.create_task(client.post("/api/chat/send", json={"text": "查一下天气"}))
    await wait_for(lambda: asyncio.sleep(0, chat._reply_job is not None))
    await asyncio.sleep(0.2)
    assert (await client.post("/api/chat/cancel")).json() == {"cancelled": True}
    r = await asyncio.wait_for(send, 5)
    evs = sse(r.text)
    final = [d for e, d in evs if e == "message"][-1]
    assert final["text"].startswith("我先查") and "（已停止）" in final["text"] and final["data"]["stopped"]
    assert evs[-1][0] == "done" and chat._reply_job is None


async def test_status_lines_while_the_chat_uses_a_tool(stack):
    client, app, _, fake = stack
    fake.tool_first = ("task_status", {})
    fake.replies = ["没有失败的任务"]
    evs = sse((await client.post("/api/chat/send", json={"text": "我有哪些任务没完成？"})).text)
    st = [d["text"] for e, d in evs if e == "status"]
    assert st[0] == "正在想…" and "正在查任务…" in st
    assert [d for e, d in evs if e == "message"][-1]["text"] == "没有失败的任务"


async def test_a_skill_from_the_picker_goes_to_the_computer(stack):
    client, app, scripts, fake = stack
    scripts.append([("done", {"summary": "做好了"})])
    fake.route = "chat"   # the classifier would say chat: the skill marker decides
    evs = sse((await client.post("/api/chat/send", json={"text": "【技能：video-remake】\n复刻这个视频"})).text)
    route = [d for e, d in evs if e == "route"][0]
    assert route["route"] == "task" and route.get("skill")
    card = [d for e, d in evs if e == "task"][0]
    assert card["kind"] == "task" and "技能" not in card["data"]["title"]
    assert not any("ROUTE" in s for s, _ in fake.completes)   # no classifier call


# ------------------------------------------------------------------------------------------ uploads
async def test_raw_upload_streams_a_video_and_caps_size(stack, monkeypatch):
    client, app, _, _ = stack
    body = os.urandom(3 * 1024 * 1024)
    r = await client.post("/api/uploads/raw?name=clip.MOV&local_only=true", content=body, headers={"Content-Type": "video/quicktime"})
    j = r.json()
    assert r.status_code == 200 and j["size"] == len(body) and j["kind"] == "video" and j["vm_path"] is None
    assert open(j["artifact"], "rb").read() == body
    monkeypatch.setattr(uploads, "RAW_MAX_MB", 1)
    r = await client.post("/api/uploads/raw?name=big.mp4&local_only=true", content=body)
    assert r.status_code == 413
    assert (await client.post("/api/uploads/raw?name=e.mp4&local_only=true", content=b"")).status_code == 400


async def test_big_file_reaches_the_computer_in_pieces(stack, monkeypatch, tmp_path):
    client, app, _, _ = stack
    monkeypatch.setattr(uploads, "CHUNK", 256 * 1024)
    body = os.urandom(256 * 1024 * 3 + 999)
    r = await client.post("/api/uploads/raw?name=v.mp4", content=body)
    assert r.status_code == 200, r.text
    assert r.json()["vm_path"] == "~/workspace/uploads/v.mp4"
    got = (tmp_path / "vmfiles" / "uploads" / "v.mp4").read_bytes()
    assert got == body
    # the JSON endpoint still works for old clients
    r = await client.post("/api/uploads", json={"name": "a.txt", "content_b64": base64.b64encode(b"hi").decode(), "local_only": True})
    assert r.status_code == 200 and r.json()["size"] == 2


async def test_old_vm_agent_without_append_is_reported(monkeypatch, tmp_path):
    class R:
        def __init__(self, size): self.success, self.error, self.result = True, None, {"size": size}
    class OldComputer:   # overwrites each piece and reports only that piece's size
        async def execute(self, action, payload, observe=False, timeout=0):
            return R(len(base64.b64decode(payload["content_b64"])))
    monkeypatch.setattr(uploads, "CHUNK", 10)
    f = tmp_path / "x.bin"; f.write_bytes(b"a" * 25)
    with pytest.raises(uploads.PushError, match="vm-agent 太旧"):
        await uploads.push_to_vm(OldComputer(), str(f), "uploads/x.bin")
    f.write_bytes(b"a" * 8)
    assert await uploads.push_to_vm(OldComputer(), str(f), "uploads/x.bin") == 8


def test_vm_agent_upload_appends(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path))
    a = transfer.upload("uploads/v.bin", base64.b64encode(b"abc").decode())
    b = transfer.upload("uploads/v.bin", base64.b64encode(b"de").decode(), append=True)
    assert a["size"] == 3 and b["size"] == 5 and (tmp_path / "uploads" / "v.bin").read_bytes() == b"abcde"
    c = transfer.upload("uploads/v.bin", base64.b64encode(b"z").decode())
    assert c["size"] == 1
