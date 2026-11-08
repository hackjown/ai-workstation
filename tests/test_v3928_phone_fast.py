"""v3.9.28 手机快通道 (backend): classification, the fast loop without cloud.wake, escalation to the computer with the
phone's results in context, persisted device queue reload, the 8 s fast timeout and POST /api/chat/device_result."""
import asyncio
import contextlib
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import phone_fast as pf
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from llm_router import LLMRouter, ScriptedProvider
from llm_router.router import LLMProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18797


class Hub0:
    def __init__(self, online=(), known=()):
        self.online, self._known = dict.fromkeys(online, {}), list(known)

    def known(self):
        return self._known


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize("text,kind", [
    ("提醒我明天早上八点买牛奶", "reminders"), ("看看我今天的日程", "calendar"), ("给妈妈打电话", "call"),
    ("拨打 13800000000", "call"), ("给老王发短信说我晚点到", "sms"), ("明早7点的闹钟", "alarm"),
    ("运行快捷指令 回家", "shortcut"), ("我在哪", "location"), ("我昨晚睡眠怎么样", "health"),
    ("通讯录里找一下张三的电话", "contacts"), ("remind me to call the bank", "reminders"), ("提醒我给妈妈打电话", "reminders"), ("what's on my calendar today", "calendar"),
])
def test_classify_device_kinds(text, kind, monkeypatch):
    monkeypatch.delenv("AIW_PHONE_FAST", raising=False)
    r = pf.classify(text, [], Hub0(known=[{"id": "p"}]))
    assert r == {"path": "device", "kind": kind}


def test_classify_vm_words_attachments_device_and_switch(monkeypatch):
    hub = Hub0(online=["p"])
    assert pf.classify("把日程导出成 excel 文件", [], hub) is None
    assert pf.classify("打开浏览器搜索明天的日历", [], hub) is None
    assert pf.classify("提醒我看 https://x.com", [], hub) is None
    assert pf.classify("提醒我处理这个", [{"name": "a.pdf"}], hub) is None
    assert pf.classify("写一段 python 代码", [], hub) is None
    assert pf.classify("今天天气怎么样", [], hub) is None
    assert pf.classify("提醒我买牛奶", [], Hub0()) is None          # no phone online or registered
    assert pf.classify("提醒我买牛奶", [], None) is None
    assert pf.classify("提醒我买牛奶", [], hub)["path"] == "device"
    monkeypatch.setenv("AIW_PHONE_FAST", "0")
    assert pf.classify("提醒我买牛奶", [], hub) is None


def test_new_device_tools_and_confirm_flags(tmp_path):
    from aiw_api.device_bridge import TOOLS, DeviceHub
    from aiw_api.store import Store
    for t in ("phone_call", "sms_send", "reminders_complete", "reminders_delete", "calendar_today"):
        assert t in TOOLS
    hub = DeviceHub(Store(str(tmp_path / "d.sqlite")))
    hub.store.kv_set("devices", [{"id": "p", "caps": ["phone.call", "sms.send", "reminders.write", "calendar.read"]}])
    tools = hub.tools(fast=True)
    for t in ("phone_call", "sms_send", "reminders_complete", "reminders_delete"):
        assert tools["device_" + t][1].needs_confirm({})
    assert not tools["device_calendar_today"][1].needs_confirm({})


# ------------------------------------------------------------------ hub: persistence + fast timeout
async def test_pending_persisted_and_reloaded(tmp_path):
    from aiw_api.device_bridge import DeviceHub
    from aiw_api.store import Store
    store = Store(str(tmp_path / "d.sqlite"))
    store.kv_set("devices", [{"id": "iph", "caps": ["reminders.write"]}])
    h1 = DeviceHub(store, ttl=60)
    job = asyncio.create_task(h1.call("reminders_create", {"title": "买牛奶"}))
    await asyncio.sleep(0.05)
    rows = store.kv_get("device_pending")
    assert len(rows) == 1 and rows[0]["tool"] == "reminders_create"
    job.cancel()                                # the API "restarts": the waiter is gone, the row stays
    with contextlib.suppress(asyncio.CancelledError):
        await job
    store.kv_set("device_pending", rows)
    h2 = DeviceHub(store, ttl=60)
    assert list(h2.pending) == [rows[0]["id"]]
    sent = []

    async def send(m):
        sent.append(m)
    assert await h2.register("iph", "ios", ["reminders.write"], send) == 1
    assert sent[0]["tool"] == "reminders_create" and sent[0]["args"] == {"title": "买牛奶"}
    assert await h2.on_result("iph", {"id": sent[0]["id"], "ok": True, "result": {"id": "r1"}})
    assert not h2.pending and store.kv_get("device_pending") == []
    orphan = store.kv_get("device_orphan_results")
    assert orphan[0]["tool"] == "reminders_create" and orphan[0]["result"] == {"id": "r1"}


async def test_fast_timeout_wakes_then_reports_offline(tmp_path, monkeypatch):
    from aiw_api.device_bridge import DeviceHub
    from aiw_api.store import Store
    monkeypatch.setenv("AIW_DEVICE_FAST_TTL", "0.3")
    pushes = []

    class N:
        async def notify(self, *a, **k):
            pushes.append((a, k))
    store = Store(str(tmp_path / "d.sqlite"))
    store.kv_set("devices", [{"id": "iph", "caps": ["calendar.read"]}])
    hub = DeviceHub(store, N())
    assert hub.fast_ttl == 0.3
    r = await asyncio.wait_for(hub.call("calendar_today", {}, fast=True), 5)
    assert r["ok"] is False and r["error"] == "手机 App 未连接/不在前台"
    assert pushes and pushes[0][1].get("link") == "aiw://open"
    assert not hub.pending and not store.kv_get("device_pending")   # a fast call is never queued for later


# ------------------------------------------------------------------ the chat + session path
class FakeChat(LLMProvider):
    name = "fake"

    def __init__(self):
        self.completes = []

    async def decide(self, req):
        raise NotImplementedError

    async def complete(self, system, user, max_tokens=800):
        self.completes.append(system)
        return json.dumps({"route": "chat", "title": ""}) if "ROUTE" in system else ""

    async def chat(self, messages, tools=None, max_tokens=1500, on_delta=None):
        return {"content": "好的", "tool_calls": []}


class FastLLM:
    """Scripted fast-loop model: a list of tool-call lists, then the final text."""

    def __init__(self, steps, final="已完成"):
        self.steps, self.final, self.seen = list(steps), final, []

    async def chat(self, messages, tools=None, max_tokens=1500, on_delta=None):
        self.seen.append({"tools": [t["name"] for t in tools or []], "messages": messages})
        if self.steps:
            calls = self.steps.pop(0)
            return {"content": "", "tool_calls": [{"id": f"c{i}", "name": n, "args": a} for i, (n, a) in enumerate(calls)]}
        return {"content": self.final, "tool_calls": []}


class SpyCloud:
    def __init__(self):
        self.woke, self.held = [], []

    def is_cloud(self, mid):
        return True

    async def wake(self, mid):
        self.woke.append(mid)

    @contextlib.asynccontextmanager
    async def hold(self, mid):
        self.held.append(mid)
        yield


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    monkeypatch.delenv("AIW_PHONE_FAST", raising=False)
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(MockBackend()), "agent-secret", ready, stop))
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
    sessions = chat.sessions
    cloud = SpyCloud()
    sessions.cloud = cloud
    hub = app.state.devices
    calls = []

    async def send(m):   # the phone: answers every call at once
        calls.append(m)
        res = {"reminders_list": {"reminders": [{"id": "r1", "title": "买牛奶"}]},
               "reminders_create": {"id": "r9"}, "calendar_today": {"events": [{"title": "周会"}]}}.get(m["tool"], {"ok": 1})
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(hub.on_result("iph", {"id": m["id"], "ok": True, "result": res})))
    await hub.register("iph", "ios", ["reminders.read", "reminders.write", "calendar.read"], send, "我的 iPhone")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield {"client": client, "app": app, "scripts": scripts, "fake": fake, "sessions": sessions, "cloud": cloud,
           "hub": hub, "calls": calls, "chat": chat}
    for j in list(chat.jobs):
        j.cancel()
    await client.aclose()
    stop.set(); await t


async def _until(fn, timeout=10):
    for _ in range(int(timeout / 0.05)):
        r = fn()
        if r:
            return r
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_fast_path_no_classifier_no_cloud_wake(stack):
    llm = FastLLM([[("device_reminders_list", {})]], final="你有 1 条提醒：买牛奶")
    stack["sessions"]._fast_llm = lambda task: llm
    out = await stack["chat"].send("看看我的提醒事项")
    assert out["route"] == "task"
    card = out["messages"][-1]
    assert card["data"]["fast_path"] == "device" and card["data"]["machine_name"] == "手机"
    assert not any("ROUTE" in c for c in stack["fake"].completes)        # no LLM router
    tid = card["data"]["task_id"]
    store = stack["sessions"].store
    t = await _until(lambda: (store.task(tid) or {}).get("status") in ("succeeded", "failed") and store.task(tid))
    assert t["status"] == "succeeded" and "买牛奶" in t["result"]
    assert stack["cloud"].woke == [] and stack["cloud"].held == []
    assert stack["calls"][0]["tool"] == "reminders_list"
    assert set(llm.seen[0]["tools"]) >= {"device_reminders_list", "ask_user", "escalate_to_computer"}
    assert not any(n.startswith(("shell", "files_", "click")) for n in llm.seen[0]["tools"])


async def test_fast_task_not_queued_behind_busy_machine_and_write_confirms(stack):
    from aiw_api.sessions import LiveSession
    from agent_runtime import AgentControl
    from aiw_protocols import Task
    sessions = stack["sessions"]
    blocker = asyncio.create_task(asyncio.sleep(30))
    sessions.live["busy"] = LiveSession("busy", Task(prompt="x", machine_id="ai-linux-001"), AgentControl(), runner=blocker)
    try:
        llm = FastLLM([[("device_reminders_create", {"title": "买牛奶"})]], final="已添加提醒")
        sessions._fast_llm = lambda task: llm
        res = await sessions.submit("提醒我买牛奶", None, None, source="chat", title="t", fast="device")
        assert res["queued"] is False
        tid = res["task"]["id"]
        store = sessions.store
        await _until(lambda: (store.task(tid) or {}).get("status") == "waiting_confirm")
        r = await stack["client"].post("/api/agent/confirm", json={"session_id": store.task(tid)["session_id"], "approve": True})
        assert r.status_code == 200
        t = await _until(lambda: (store.task(tid) or {}).get("status") in ("succeeded", "failed") and store.task(tid))
        assert t["status"] == "succeeded"
        assert stack["calls"][-1]["tool"] == "reminders_create"
        assert sessions.busy_session("ai-linux-001").id == "busy"    # the fast task never counted as busy
    finally:
        blocker.cancel()
        sessions.live.pop("busy", None)


async def test_escalation_continues_on_computer_with_device_results(stack):
    sessions = stack["sessions"]
    llm = FastLLM([[("device_calendar_today", {})], [("escalate_to_computer", {"reason": "要生成文档"})]])
    sessions._fast_llm = lambda task: llm
    prov = ScriptedProvider([("done", {"summary": "文档做好了"})])
    stack["scripts"].append(prov.script)
    seen = []
    real = sessions._routed_llm

    async def routed(task, small=None):
        seen.append(task.context or "")
        return await real(task, small)
    sessions._routed_llm = routed
    res = await sessions.submit("看看今天的日程", None, None, source="chat", title="t", fast="device")
    tid = res["task"]["id"]
    store = sessions.store
    t = await _until(lambda: (store.task(tid) or {}).get("status") in ("succeeded", "failed") and store.task(tid), 20)
    assert t["status"] == "succeeded", t
    assert stack["cloud"].woke == ["ai-linux-001"]                     # the computer path wakes it as always
    assert store.kv_get(f"task_fast:{tid}")["escalated"] is True
    assert seen and "手机快通道已完成的步骤" in seen[0] and "周会" in seen[0]


async def test_device_result_endpoint(stack):
    c = stack["client"]
    r = await c.post("/api/chat/device_result", json={"tool": "reminders_create", "ok": True, "args": {"title": "买牛奶"},
                                                      "result": {"id": "r1"}, "client_id": "abc"})
    assert r.status_code == 200 and r.json()["ok"]
    m = r.json()["message"]
    assert m["data"]["device_result"] and m["data"]["tool"] == "reminders_create" and "reminders_create" in m["text"]
    r2 = await c.post("/api/chat/device_result", json={"tool": "reminders_create", "client_id": "abc"})
    assert r2.json()["duplicate"] is True
    hist = (await c.get("/api/chat")).json()
    assert sum(1 for x in hist["messages"] if (x.get("data") or {}).get("client_id") == "abc") == 1
