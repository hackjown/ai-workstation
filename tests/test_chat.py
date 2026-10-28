"""The persistent 小秘 chat (chat.py): storage, routing, streaming replies, task cards, escalations in the chat,
the one-line result, the 交给电脑去做 offer and the rolling summary."""
import asyncio
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api.chat import OfferFilter, one_line, parse_route
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from llm_router import LLMRouter, ScriptedProvider
from llm_router.router import LLMProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18791


class FakeChat(LLMProvider):
    """route: what the classifier answers; replies: chat answers (str, or a list of pieces to stream);
    tool_first: one tool call before the answer."""
    name = "fake"

    def __init__(self, route="chat", replies=None, tool_first=None, summary="摘要：用户在聊天"):
        self.route, self.replies, self.tool_first, self.summary_text = route, list(replies or ["你好呀"]), tool_first, summary
        self.chats, self.completes = [], []

    async def decide(self, req):
        raise NotImplementedError

    async def complete(self, system, user, max_tokens=800):
        self.completes.append((system, user))
        if "ROUTE" in system:
            if self.route == "error":
                raise RuntimeError("gateway down")
            return json.dumps({"route": self.route, "title": "打开网页"}) if self.route in ("chat", "task") else self.route
        if "滚动摘要" in system:
            return self.summary_text
        return ""

    async def chat(self, messages, tools=None, max_tokens=1500, on_delta=None):
        self.chats.append({"messages": messages, "tools": [t["name"] for t in tools or []]})
        if self.tool_first and tools and not any(m.get("role") == "tool" for m in messages):
            name, args = self.tool_first
            return {"content": "", "tool_calls": [{"id": "c1", "name": name, "args": args}]}
        r = self.replies.pop(0) if self.replies else "好的"
        pieces = r if isinstance(r, list) else [r]
        for p in pieces:
            if on_delta:
                await on_delta(p)
        return {"content": "".join(pieces), "tool_calls": []}


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.ask_timeout_s = 20
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORT}", desktop_http="http://127.0.0.1:1")]
    scripts = []
    app = build_app(s, llm_factory=lambda: LLMRouter([ScriptedProvider(scripts.pop(0) if scripts else [])]))
    fake = FakeChat()
    chat = app.state.chat
    chat.llm_factory = lambda role: LLMRouter([fake])
    chat.tool_loader = None
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, app, scripts, fake
    for j in list(chat.jobs):
        j.cancel()
    await client.aclose()
    stop.set(); await t


def sse(text):
    out, ev = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:") and ev:
            out.append((ev, json.loads(line[5:].strip())))
    return out


async def wait_for(fn, timeout=10):
    for _ in range(int(timeout / 0.05)):
        r = await fn()
        if r:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


# ------------------------------------------------------------------------------------------ pure helpers
def test_offer_filter_hides_the_marker_while_streaming():
    f = OfferFilter()
    shown = "".join(f.feed(p) for p in ["我可以帮你", "下单[", "[交给电脑：打开京东", "买牛奶]]"])
    assert shown == "我可以帮你下单"
    text, offer = f.finish()
    assert text == "我可以帮你下单" and offer == "打开京东买牛奶"
    g = OfferFilter()
    assert g.feed("数组 a[") == "数组 a" and g.feed("0]") == "[0]" and g.finish() == ("数组 a[0]", None)


def test_route_parsing_defaults_to_chat():
    assert parse_route('{"route": "task", "title": "下单"}')["route"] == "task"
    assert parse_route("<think>{\"route\":\"task\"}</think> 嗯 不确定")["route"] == "chat"
    assert parse_route("")["route"] == "chat"


def test_one_line_result():
    assert one_line("## 结果\n**已经**下载好 3 个文件。另外还有说明") == "已经下载好 3 个文件。"
    assert len(one_line("长" * 300)) <= 90


# ------------------------------------------------------------------------------------------ chat replies
async def test_chat_reply_streams_and_is_stored(stack):
    client, app, _, fake = stack
    fake.replies = [["你好", "，我是小秘"]]
    r = await client.post("/api/chat/send", json={"text": "在吗"})
    evs = sse(r.text)
    kinds = [e for e, _ in evs]
    assert kinds[:2] == ["user", "route"] and "delta" in kinds and kinds[-1] == "done"
    assert "".join(d["text"] for e, d in evs if e == "delta") == "你好，我是小秘"
    final = [d for e, d in evs if e == "message"][-1]
    assert final["text"] == "你好，我是小秘" and final["role"] == "assistant"
    h = (await client.get("/api/chat")).json()
    assert [m["role"] for m in h["messages"]] == ["user", "assistant"]
    assert h["messages"][0]["text"] == "在吗"


async def test_classifier_error_means_chat_and_failure_is_said(stack):
    client, app, _, fake = stack
    fake.route = "error"
    r = (await client.post("/api/chat/send", json={"text": "帮我订个票", "stream": False})).json()
    assert [e["event"] for e in r["events"]][:2] == ["user", "route"]
    assert r["events"][1]["data"]["route"] == "chat"

    async def boom(*a, **k):
        raise RuntimeError("down")
    fake.chat = boom
    r = (await client.post("/api/chat/send", json={"text": "再试", "stream": False})).json()
    m = r["messages"][-1]
    assert m["data"]["error"] and m["data"]["offer_task"] == "再试" and "没有回应" in m["text"]


async def test_reply_can_offer_a_task_and_the_button_starts_it(stack):
    client, app, scripts, fake = stack
    fake.replies = ["这得在电脑上做。\n[[交给电脑:打开 Chrome 搜索 OpenAI]]"]
    r = (await client.post("/api/chat/send", json={"text": "帮我搜一下 OpenAI", "stream": False})).json()
    m = r["messages"][-1]
    assert m["text"] == "这得在电脑上做。" and m["data"]["offer_task"] == "打开 Chrome 搜索 OpenAI"
    scripts.append([("done", {"summary": "搜好了。结果在页面上"})])
    card = (await client.post(f"/api/chat/messages/{m['id']}/to_task", json={})).json()
    assert card["kind"] == "task" and card["data"]["task_id"]
    again = (await client.post(f"/api/chat/messages/{m['id']}/to_task", json={})).json()
    assert again["id"] == card["id"]     # pressing twice never starts two tasks


async def test_chat_tools_task_status_and_read_file(stack, tmp_path):
    client, app, _, fake = stack
    up = tmp_path / "artifacts" / "uploads"
    up.mkdir(parents=True)
    (up / "1700000000-notes.txt").write_text("会议定在周五下午三点", encoding="utf-8")
    fake.tool_first = ("read_file", {"path": "notes.txt"})
    fake.replies = ["周五下午三点"]
    await client.post("/api/chat/send", json={"text": "notes.txt 里说会议什么时候", "stream": False})
    tool_msgs = [m for m in fake.chats[-1]["messages"] if m["role"] == "tool"]
    assert "周五下午三点" in tool_msgs[0]["content"]
    assert {"read_url", "read_file", "task_status"} <= set(fake.chats[0]["tools"])
    res = await app.state.chat._task_status({})
    assert "tasks" in json.loads(res)


# ------------------------------------------------------------------------------------------ tasks from the chat
async def test_task_route_makes_a_card_that_follows_the_task_and_posts_the_result(stack):
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("files_write", {"path": "out/report.md", "content": "# hi"}),
                    ("send_file", {"path": "out/report.md"}),
                    ("done", {"summary": "报告写好了，已经发给你。"})])
    r = (await client.post("/api/chat/send", json={"text": "在电脑上写一份报告发我", "stream": False})).json()
    card = r["messages"][-1]
    assert card["kind"] == "task" and card["data"]["title"] == "打开网页"
    tid = card["data"]["task_id"]

    async def finished():
        h = (await client.get("/api/chat")).json()["messages"]
        return [m for m in h if m["kind"] == "task_result"] and h
    h = await wait_for(finished, 15)
    c = next(m for m in h if m["kind"] == "task")
    assert c["data"]["status"] == "succeeded" and c["data"]["step"] >= 1
    res = next(m for m in h if m["kind"] == "task_result")
    assert res["text"].startswith("报告写好了") and res["task_id"] == tid
    assert [f["name"] for f in res["data"]["files"]] == ["report.md"]
    # the task got the chat turns as context and still opens in the normal thread view
    th = (await client.get(f"/api/threads/{c['data']['thread_id']}")).json()
    assert th["tasks"][0]["id"] == tid


async def test_escalation_shows_in_the_chat_with_buttons(stack):
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("fail", {"reason": "按钮点不动"})])
    r = (await client.post("/api/chat/send", json={"text": "去网站上注册", "stream": False})).json()
    card = r["messages"][-1]

    async def asked():
        h = (await client.get("/api/chat")).json()["messages"]
        return next((m for m in h if m["kind"] == "ask" and not m["data"].get("stale")), None)
    ask = await wait_for(asked, 10)
    assert ask["data"]["ask_kind"] == "escalate"
    assert ask["data"]["options"] == ["继续，换个方法再试", "我来接管桌面", "停止任务"]
    c = next(m for m in (await client.get("/api/chat")).json()["messages"] if m["id"] == card["id"])
    assert c["data"]["status"] == "waiting_input"
    ok = (await client.post(f"/api/chat/messages/{ask['id']}/answer", json={"choice": "停止任务"})).json()
    assert ok["ok"] and ok["message"]["data"]["resolved"]["answer"] == "停止任务"
    again = await client.post(f"/api/chat/messages/{ask['id']}/answer", json={"choice": "停止任务"})
    assert again.status_code == 409

    async def ended():
        h = (await client.get("/api/chat")).json()["messages"]
        return next((m for m in h if m["kind"] == "task_result"), None)
    res = await wait_for(ended, 10)
    assert res["data"]["status"] == "stopped" and res["text"].startswith("任务已停止")


async def test_manual_task_mode_skips_the_classifier(stack):
    client, app, scripts, fake = stack
    scripts.append([("done", {"summary": "ok"})])
    r = (await client.post("/api/chat/send", json={"text": "随便", "mode": "task", "stream": False})).json()
    assert r["messages"][-1]["kind"] == "task"
    assert not any("ROUTE" in s for s, _ in fake.completes)


# ------------------------------------------------------------------------------------------ context + compaction
async def test_window_is_last_20_turns_and_summary_covers_the_rest(stack):
    client, app, _, fake = stack
    chat = app.state.chat
    for i in range(30):
        chat.add("user", "text", f"问题{i}")
        chat.add("assistant", "text", f"回答{i}")
    chat.add("user", "text", "现在")
    msgs = chat.build_messages("现在")
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 20 and users[0]["content"] == "问题11" and users[-1]["content"] == "现在"
    assert "问题9" in msgs[0]["content"]          # outside the window, not yet summarised: still in the prompt
    assert await chat.compact()
    summ = chat.summary()
    assert summ["text"] == "摘要：用户在聊天" and summ["upto"] > 0
    msgs = chat.build_messages("现在")
    assert "摘要：用户在聊天" in msgs[0]["content"] and "问题9" not in msgs[0]["content"]
    h = (await client.get("/api/chat?limit=300")).json()
    assert len(h["messages"]) == 61               # the screen keeps the full history
    page = (await client.get(f"/api/chat?limit=10&before={h['messages'][20]['seq']}")).json()
    assert len(page["messages"]) == 10 and page["has_more"]


async def test_roles_alternate_for_strict_gateways(stack):
    client, app, _, fake = stack
    chat = app.state.chat
    chat.add("user", "text", "a")
    chat.add("assistant", "task", "任务", {"title": "任务", "status": "running"})
    chat.add("assistant", "task_result", "做完了", {"files": []})
    chat.add("user", "text", "b")
    msgs = chat.build_messages("b")
    roles = [m["role"] for m in msgs[1:]]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


# ------------------------------------------------------------------------------------------ OpenAI-compatible chat
async def test_openai_chat_streams_content_only_and_collects_tool_calls(monkeypatch):
    from llm_router.providers.openai import OpenAIProvider

    def body(chunks):
        return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    answers = [
        [{"choices": [{"delta": {"reasoning_content": "想一想"}}]},
         {"choices": [{"delta": {"content": "<think>内心"}}]},
         {"choices": [{"delta": {"content": "戏</think>你好"}}]},
         {"choices": [{"delta": {"content": "，在的"}}]}],
        [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "read_url", "arguments": "{\"url\":"}}]}}]},
         {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"https://a.b\"}"}}]}}]}],
    ]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=body(answers.pop(0)), headers={"content-type": "text/event-stream"})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    p = OpenAIProvider(model="m", api_key="k", base_url="http://x", thinking="off")
    seen = []

    async def cb(t):
        seen.append(t)
    r = await p.chat([{"role": "user", "content": "在吗"}], None, 100, cb)
    assert "".join(seen) == "你好，在的" and r["content"] == "你好，在的" and r["tool_calls"] == []
    r = await p.chat([{"role": "user", "content": "读"}], [{"name": "read_url", "parameters": {}}], 100, cb)
    assert r["tool_calls"] == [{"id": "t1", "name": "read_url", "args": {"url": "https://a.b"}}]
    assert sent[1]["tools"][0]["function"]["name"] == "read_url" and sent[0]["stream"] is True


async def test_takeover_choice_posts_a_takeover_message(stack):
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("fail", {"reason": "要验证码"})])
    await client.post("/api/chat/send", json={"text": "去网站上登录", "stream": False})

    async def ask_of(kind):
        async def f():
            h = (await client.get("/api/chat")).json()["messages"]
            return next((m for m in h if m["kind"] == "ask" and m["data"]["ask_kind"] == kind
                         and not m["data"].get("resolved") and not m["data"].get("stale")), None)
        return f
    esc = await wait_for(await ask_of("escalate"), 10)
    r = (await client.post(f"/api/chat/messages/{esc['id']}/answer", json={"choice": "我来接管桌面"})).json()
    assert r["open_task"] is True                  # the client jumps into the task view in takeover mode
    tk = await wait_for(await ask_of("takeover"), 10)
    assert tk["data"]["session_id"] and tk["text"].startswith("需要你接管")
    await client.post(f"/api/chat/messages/{tk['id']}/answer", json={"choice": "__cancel__"})

    async def ended():
        h = (await client.get("/api/chat")).json()["messages"]
        return next((m for m in h if m["kind"] == "task_result"), None)
    res = await wait_for(ended, 10)
    assert res["data"]["status"] in ("stopped", "failed")


def test_answer_text_never_shows_null():
    """3.7: a resolved ask/form card showed "已回答：<null>" (iOS printed NSNull). The server now sends resolved.text."""
    from aiw_api.chat import answer_text, _row
    import json as _j
    assert answer_text(None) == "" and answer_text("") == "" and answer_text("null") == "" and answer_text("{}") == ""
    assert answer_text("done") == "已完成，交还给电脑" and answer_text("__cancel__") == "已取消"
    assert answer_text("A1893，iPadOS 17") == "A1893，iPadOS 17"
    f = [{"key": "model", "label": "型号"}, {"key": "os", "label": "系统版本"}]
    assert answer_text(_j.dumps({"model": "A2602", "os": "17.5"}, ensure_ascii=False), f) == "型号：A2602；系统版本：17.5"
    row = _row({"id": "m", "data": _j.dumps({"resolved": {"answer": None}})})
    assert row["data"]["resolved"]["text"] == ""
    row = _row({"id": "m", "data": _j.dumps({"resolved": {"answer": True}})})
    assert row["data"]["resolved"]["text"] == "已允许"
