"""3.8 message queue: a message sent after 停止 is no longer stuck in a paused queue, and 立即发送 / 删除 work
for a queued message (POST /api/tasks/{id}/send_now, DELETE /api/tasks/{id})."""
import asyncio
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from llm_router import LLMRouter, ScriptedProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18795


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORT}", desktop_http="http://127.0.0.1:1")]
    scripts = []
    app = build_app(s, llm_factory=lambda: LLMRouter([ScriptedProvider(scripts.pop(0) if scripts else
                                                                        [("done", {"summary": "ok"})])]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, backend, scripts, app
    await client.aclose()
    stop.set(); await t


BUSY = [("keypress", {"key": f"F{i % 12 + 1}"}) for i in range(400)]   # busy, never finishing by itself


async def status_of(client, tid, task_id):
    th = (await client.get(f"/api/threads/{tid}")).json()
    return next(t for t in th["tasks"] if t["id"] == task_id), th


async def wait_status(client, tid, task_id, want, timeout=10):
    for _ in range(int(timeout / 0.1)):
        t, th = await status_of(client, tid, task_id)
        if t["status"] in want:
            return t, th
        await asyncio.sleep(0.1)
    raise AssertionError(f"{task_id} stayed {t['status']}: " + str([(x["id"], x["status"], (x.get("result") or "")[:80]) for x in th["tasks"]]))


async def stop_all(client, tid):
    th = (await client.get(f"/api/threads/{tid}")).json()
    for t in th["tasks"]:
        if t["status"] in ("running", "paused", "queued"):
            await client.delete(f"/api/tasks/{t['id']}")


async def test_message_after_stop_is_not_stuck(stack):
    """Root cause of 'queued messages are never sent': 停止 paused the thread's queue, and every later message in that
    thread was queued behind the pause with the computer idle. A new message now resumes the queue."""
    client, backend, scripts, _ = stack
    scripts.append(BUSY)
    r = (await client.post("/api/tasks", json={"prompt": "loop", "max_steps": 400})).json()
    tid = r["thread_id"]
    await client.delete(f"/api/tasks/{r['task']['id']}")          # 停止 (the iOS stop button)
    await wait_status(client, tid, r["task"]["id"], ("stopped",))
    await asyncio.sleep(0.2)
    assert (await client.get(f"/api/threads/{tid}")).json()["queue_held"]
    r2 = (await client.post("/api/tasks", json={"prompt": "next", "thread_id": tid})).json()
    assert not r2["queued"] and r2["session_id"]                  # computer idle: it starts at once
    await wait_status(client, tid, r2["task"]["id"], ("succeeded",))
    assert not (await client.get(f"/api/threads/{tid}")).json()["queue_held"]


async def test_queued_follow_up_runs_when_the_task_ends(stack):
    client, backend, scripts, _ = stack
    scripts.append([("keypress", {"key": f"F{i + 1}"}) for i in range(4)] + [("done", {"summary": "first"})])
    r = (await client.post("/api/tasks", json={"prompt": "first"})).json()
    tid = r["thread_id"]
    r2 = (await client.post("/api/tasks", json={"prompt": "second", "thread_id": tid})).json()
    assert r2["queued"]
    t, th = await status_of(client, tid, r2["task"]["id"])
    if t["status"] == "queued":   # why it waits: the busy task is named for the UI
        assert t["queue"]["busy"]["task_id"] == r["task"]["id"] and not t["queue"]["held"]
    await wait_status(client, tid, r2["task"]["id"], ("succeeded",))


async def test_send_now_and_delete(stack):
    client, backend, scripts, _ = stack
    scripts.append(BUSY)
    r = (await client.post("/api/tasks", json={"prompt": "loop", "max_steps": 400})).json()
    tid = r["thread_id"]
    a = (await client.post("/api/tasks", json={"prompt": "a", "thread_id": tid})).json()["task"]["id"]
    b = (await client.post("/api/tasks", json={"prompt": "b", "thread_id": tid})).json()["task"]["id"]
    # 删除 a queued message
    assert (await client.delete(f"/api/tasks/{a}")).json()["cancelled"]
    t, _ = await status_of(client, tid, a)
    assert t["status"] == "cancelled"
    # 立即发送 while the computer is busy: it goes first, nothing starts, the busy task is named
    res = (await client.post(f"/api/tasks/{b}/send_now", json={})).json()
    assert not res["started"] and res["queue"]["first"] and res["queue"]["busy"]["task_id"] == r["task"]["id"]
    # ... and with stop_current the running task is stopped and b starts right after
    res = (await client.post(f"/api/tasks/{b}/send_now", json={"stop_current": True})).json()
    assert res["stopped"] == r["task"]["id"]
    await wait_status(client, tid, r["task"]["id"], ("stopped",))
    await wait_status(client, tid, b, ("running", "succeeded"))
    # not queued any more -> 409
    assert (await client.post(f"/api/tasks/{b}/send_now", json={})).status_code == 409
    assert (await client.post("/api/tasks/nope/send_now", json={})).status_code == 404
    await stop_all(client, tid)


async def test_send_now_releases_a_paused_queue(stack):
    """The web's 发送排队消息 case: messages queued before 停止 stay paused, 立即发送 starts the one tapped."""
    client, backend, scripts, app = stack
    scripts.append(BUSY)
    r = (await client.post("/api/tasks", json={"prompt": "loop", "max_steps": 400})).json()
    tid = r["thread_id"]
    q = (await client.post("/api/tasks", json={"prompt": "waiting", "thread_id": tid})).json()["task"]["id"]
    await client.delete(f"/api/tasks/{r['task']['id']}")
    await wait_status(client, tid, r["task"]["id"], ("stopped",))
    await asyncio.sleep(0.3)
    t, th = await status_of(client, tid, q)
    assert t["status"] == "queued" and th["queue_held"] and t["queue"]["held"]   # stop pauses what was queued
    res = (await client.post(f"/api/tasks/{q}/send_now", json={})).json()
    assert res["started"]
    await wait_status(client, tid, q, ("succeeded",))
