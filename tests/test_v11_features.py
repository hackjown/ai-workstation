"""V1.1 features: threads + context, queue, plan, ask_user, memory, delegate,
external tools, receipts/replay, routines, watches, notifications, secrets,
interrupted-task recovery."""
import asyncio
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import cron
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.scheduler import evaluate, find_price, page_text
from llm_router import LLMRouter, ScriptedProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18768


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    monkeypatch.setenv("AIW_SECRET_KEY", "test-key")
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.ask_timeout_s = 5
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORT}", desktop_http="http://127.0.0.1:1")]
    scripts, providers = [], []

    def factory():
        p = ScriptedProvider(scripts.pop(0))
        providers.append(p)
        return LLMRouter([p])
    app = build_app(s, llm_factory=factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, backend, scripts, app, providers, tmp_path
    await client.aclose()
    stop.set(); await t


async def wait_task(client, tid, timeout=10):
    for _ in range(int(timeout / 0.1)):
        t = (await client.get(f"/api/tasks/{tid}")).json()
        if t["status"] in ("succeeded", "failed", "stopped", "cancelled", "interrupted"):
            return t
        await asyncio.sleep(0.1)
    raise AssertionError(f"task did not finish: {t}")


async def test_thread_followup_queue_and_context(stack):
    client, backend, scripts, app, providers, _ = stack
    scripts.append([("type", {"text": "a"}), ("done", {"summary": "第一步完成"})])
    scripts.append([("type", {"text": "b"}), ("done", {"summary": "第二步完成"})])
    r1 = (await client.post("/api/tasks", json={"prompt": "输入 a"})).json()
    r2 = (await client.post("/api/tasks", json={"prompt": "再输入 b", "thread_id": r1["thread_id"]})).json()
    assert r2["queued"]
    t2 = await wait_task(client, r2["task"]["id"])
    assert t2["status"] == "succeeded"
    # the follow-up saw the first turn as context
    assert "第一步完成" in providers[1].requests[0].task
    th = (await client.get(f"/api/threads/{r1['thread_id']}")).json()
    assert [t["status"] for t in th["tasks"]] == ["succeeded", "succeeded"]
    assert th["tasks"][0]["plan"]["steps"] and th["tasks"][0]["plan"]["done"]
    lst = (await client.get("/api/threads")).json()
    assert lst[0]["id"] == r1["thread_id"] and lst[0]["task_count"] == 2


async def test_plan_update_ask_user_and_memory(stack):
    client, backend, scripts, app, providers, _ = stack
    scripts.append([("ask_user", {"question": "用哪个账号？", "options": ["工作", "个人"]}),
                    ("update_plan", {"done": [0]}),
                    ("remember", {"text": "周报放在 ~/workspace/reports"}),
                    ("type", {"text": "x"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "登录邮箱，然后整理周报"})).json()
    sid = r["session_id"]
    for _ in range(50):
        s = (await client.get(f"/api/sessions/{sid}")).json()
        if s["pending_question"]:
            break
        await asyncio.sleep(0.1)
    assert s["pending_question"]["options"] == ["工作", "个人"]
    assert (await client.post("/api/agent/answer", json={"session_id": sid, "text": "工作"})).json()["answered"]
    t = await wait_task(client, r["task"]["id"])
    assert t["status"] == "succeeded"
    assert any("工作" in h for h in providers[0].requests[-1].history)
    mem = (await client.get("/api/memories")).json()
    assert mem[0]["text"].startswith("周报")
    notes = (await client.get("/api/notifications")).json()
    assert {"input", "done"} <= {n["kind"] for n in notes}
    # memory is injected into the next task's context
    scripts.append([("done", {"summary": "ok"})])
    r2 = (await client.post("/api/tasks", json={"prompt": "看看屏幕"})).json()
    await wait_task(client, r2["task"]["id"])
    assert "周报放在" in providers[1].requests[0].task


async def test_delegate_external_tool_receipts_replay(stack, monkeypatch):
    client, backend, scripts, app, providers, tmp = stack
    import aiw_api.sessions as sess_mod

    async def fake_tools(store, secrets):
        async def weather(args):
            return {"city": args.get("city"), "temp": 21}
        return {"ext_weather": ({"name": "ext_weather", "description": "weather",
                                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}, weather)}
    monkeypatch.setattr("aiw_api.integrations.load_tools", fake_tools)
    scripts.append([("delegate", {"goal": "create report.txt"}),
                    # helper (same scripted provider here) runs a shell step and finishes
                    ("shell", {"command": "echo hi > report.txt", "cwd": str(tmp)}), ("done", {"summary": "report.txt created"}),
                    ("ext_weather", {"city": "上海"}),
                    ("click", {"x": 5, "y": 5}),
                    ("done", {"summary": "all done"})])
    r = (await client.post("/api/tasks", json={"prompt": "创建报告并查天气"})).json()
    t = await wait_task(client, r["task"]["id"])
    assert t["status"] == "succeeded", t
    assert (tmp / "report.txt").exists()
    s = (await client.get(f"/api/sessions/{r['session_id']}")).json()
    types = [e["type"] for e in s["events"]]
    assert "agent.subagent" in types and "agent.tool_call" in types
    rec = (await client.get(f"/api/tasks/{r['task']['id']}/receipts")).json()
    assert any(a["action"] == "shell.exec" and a["ok"] for a in rec["actions"])
    rp = (await client.get(f"/api/sessions/{r['session_id']}/replay")).json()
    assert rp["frames"] and rp["frames"][0]["shot"].endswith(".png")
    del sess_mod


async def test_retry_and_recover_interrupted(stack):
    client, backend, scripts, app, providers, _ = stack
    scripts.append([("fail", {"reason": "no"})])
    r = (await client.post("/api/tasks", json={"prompt": "试一下"})).json()
    assert (await wait_task(client, r["task"]["id"]))["status"] == "failed"
    scripts.append([("done", {"summary": "ok"})])
    rr = (await client.post(f"/api/tasks/{r['task']['id']}/retry")).json()
    assert (await wait_task(client, rr["task"]["id"]))["status"] == "succeeded"
    assert rr["thread_id"] == r["thread_id"]
    # a task left "running" by a crash is marked interrupted and re-queued
    store, sessions = app.state.store, app.state.sessions
    store.create_task({"id": "task-crashed", "prompt": "旧任务", "machine_id": "ai-linux-001", "status": "running",
                       "thread_id": r["thread_id"]})
    scripts.append([("done", {"summary": "resumed"})])
    new = await sessions.recover_interrupted()
    assert store.task("task-crashed")["status"] == "interrupted"
    assert (await wait_task(client, new[0]))["status"] == "succeeded"


async def test_routines_watches_notify_secrets(stack, monkeypatch):
    client, backend, scripts, app, providers, _ = stack
    assert (await client.post("/api/routines", json={"name": "x", "prompt": "p", "schedule": "bad"})).status_code == 400
    ro = (await client.post("/api/routines", json={"name": "日报", "prompt": "截图汇报", "schedule": "daily 09:00"})).json()
    assert ro["next_run"] > time.time()
    scripts.append([("done", {"summary": "日报完成"})])
    ro["next_run"] = time.time() - 1
    res = await app.state.scheduler.fire_routine(ro)
    assert (await wait_task(client, res["task"]["id"]))["status"] == "succeeded"
    assert (await client.get("/api/routines")).json()[0]["last_task_id"] == res["task"]["id"]

    pages = iter(["<p>原价 ¥199</p>", "<p>现价 ¥149</p>", "<p>现价 ¥139</p>"])

    real_get = httpx.AsyncClient.get

    async def fake_get(self, url, **kw):
        if not str(url).startswith("https://shop.example"):
            return await real_get(self, url, **kw)
        return httpx.Response(200, text=next(pages), request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    w = (await client.post("/api/watches", json={"name": "耳机", "url": "https://shop.example/x",
                                                  "mode": "price_below", "value": "150"})).json()
    a = (await client.post(f"/api/watches/{w['id']}/check")).json()
    b = (await client.post(f"/api/watches/{w['id']}/check")).json()
    c = (await client.post(f"/api/watches/{w['id']}/check")).json()
    assert (a["triggered"], b["triggered"], c["triggered"]) == (False, True, False)   # alert once, de-duplicated
    notes = (await client.get("/api/notifications?unread=true")).json()
    assert sum(n["kind"] == "watch" for n in notes) == 1
    await client.post("/api/notifications/read", json={})
    assert (await client.get("/api/notifications?unread=true")).json() == []

    await client.put("/api/secrets/openai_api_key", json={"value": "sk-abcdefgh12345678"})
    lst = (await client.get("/api/secrets")).json()
    assert lst[0]["preview"].endswith("5678") and "abcdefgh" not in str(lst)
    raw = app.state.store.secret_get("openai_api_key")
    assert "abcdefgh" not in raw                                     # encrypted at rest
    assert app.state.secrets.get("openai_api_key") == "sk-abcdefgh12345678"


def test_cron_and_watch_helpers():
    base = 1767225600  # 2026-01-01 00:00 UTC = 08:00 Shanghai
    assert cron.next_run("every 30m", base) == base + 1800
    nxt = cron.next_run("daily 09:00", base)
    assert nxt - base == 3600
    assert cron.next_run("0 9 * * 1-5", base) >= nxt
    with pytest.raises(ValueError):
        cron.next_run("every 1m", base)
    assert find_price(page_text("<div>Price: $1,299.00</div>")) == 1299.0
    assert evaluate({"mode": "contains", "value": "有货"}, "现在有货")[0]
    assert not evaluate({"mode": "contains", "value": "有货", "last_value": "present"}, "现在有货")[0]
    assert not evaluate({"mode": "change"}, "a")[0]                           # first check sets the baseline
    assert evaluate({"mode": "change", "last_hash": "x"}, "a")[0]


async def test_openapi_import_and_http_tool(stack, monkeypatch):
    client, *_ = stack
    spec = {"servers": [{"url": "https://api.example.com"}],
            "paths": {"/items/{id}": {"get": {"operationId": "getItem", "summary": "Get item",
                                               "parameters": [{"name": "id", "in": "path", "required": True,
                                                               "schema": {"type": "string"}}]}}}}
    r = (await client.post("/api/integrations/openapi", json={"spec": spec, "prefix": "demo",
                                                              "headers": {"X-Key": "{{secret:demo_key}}"}})).json()
    assert r["imported"] == 1
    await client.put("/api/secrets/demo_key", json={"value": "k123"})
    seen = {}

    real_request = httpx.AsyncClient.request

    async def fake_request(self, method, url, **kw):
        if not str(url).startswith("https://api.example.com"):
            return await real_request(self, method, url, **kw)
        seen.update(method=method, url=url, headers=kw.get("headers"))
        return httpx.Response(200, json={"ok": 1}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    tools = (await client.get("/api/integrations/tools")).json()
    assert tools[0]["name"] == "ext_demo_getItem"
    from aiw_api.integrations import load_tools
    app_tools = await load_tools(stack[3].state.store, stack[3].state.secrets)
    out = await app_tools["ext_demo_getItem"][1]({"id": "42"})
    assert out["data"] == {"ok": 1} and seen["url"].endswith("/items/42") and seen["headers"]["X-Key"] == "k123"


async def test_vm_ip_setting_repoints_machine(stack, monkeypatch):
    client, backend, scripts, app, providers, tmp = stack
    monkeypatch.setenv("VM_AGENT_PORT", str(PORT))
    r = await client.put("/api/settings/vm", json={"ip": "bad ip!"})
    assert r.status_code == 400
    r = await client.put("/api/settings/vm", json={"ip": "127.0.0.1", "auto": False})
    assert r.status_code == 200 and r.json()["agent_endpoint"] == f"ws://127.0.0.1:{PORT}"
    g = (await client.get("/api/settings/vm")).json()
    assert g["ip"] == "127.0.0.1"
    m = (await client.get("/api/machines/ai-linux-001")).json()
    assert m["agent"].get("ok")  # reconnected through the new endpoint
    # saved address survives a restart and beats the env/config value
    from aiw_api.main import _apply_saved_vm_ip
    store = app.state.store
    store.kv_set("vm_net", {"ip": "10.9.8.7"})
    _apply_saved_vm_ip(store, app.state.settings)
    assert store.machine("ai-linux-001")["agent_endpoint"].startswith("ws://10.9.8.7:")
