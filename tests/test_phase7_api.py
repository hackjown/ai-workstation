"""Full V1 chain through the HTTP API: Web -> API -> Agent Runtime -> LLM Router
-> Tool Router -> Computer Runtime -> WebSocket -> VM Agent (mock desktop)."""
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

PORT = 18767


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
    app = build_app(s, llm_factory=lambda: LLMRouter([ScriptedProvider(scripts.pop(0))]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, backend, scripts, app
    await client.aclose()
    stop.set(); await t


async def wait_done(client, sid, timeout=10):
    for _ in range(int(timeout / 0.1)):
        s = (await client.get(f"/api/sessions/{sid}")).json()
        if s["status"] not in ("running",):
            return s
        await asyncio.sleep(0.1)
    raise AssertionError("session did not finish")


async def test_auth(stack):
    client, *_ = stack
    r = await client.get("/api/machines", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_machine_and_screenshot(stack):
    client, *_ = stack
    m = (await client.get("/api/machines/ai-linux-001")).json()
    assert m["agent"]["ok"] and m["agent"]["backend"] == "mock"
    r = await client.get("/api/machines/ai-linux-001/screenshot")   # acceptance test 1
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert (await client.post("/api/machines/ai-linux-001/start")).status_code == 503  # no PVE configured


async def test_task_end_to_end(stack):
    client, backend, scripts, _ = stack
    scripts.append([("open_app", {"name": "chrome"}), ("keypress", {"key": "ctrl+l"}),
                    ("type", {"text": "OpenAI"}), ("keypress", {"key": "ENTER"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "打开 Chrome 搜索 OpenAI"})).json()
    s = await wait_done(client, r["session_id"])
    assert s["status"] == "succeeded"
    t = (await client.get(f"/api/tasks/{r['task']['id']}")).json()
    assert t["status"] == "succeeded"
    types = [e["type"] for e in s["events"]]
    assert types[0] == "task.started" and types[-1] == "task.finished"
    assert s["artifacts"]
    assert backend.typed == "OpenAI"


async def test_confirm_flow(stack):
    client, backend, scripts, _ = stack
    scripts.append([("shell", {"command": "rm -rf ./old"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "删除旧文件"})).json()
    sid = r["session_id"]
    for _ in range(50):
        s = (await client.get(f"/api/sessions/{sid}")).json()
        if s["pending_confirm"]:
            break
        await asyncio.sleep(0.1)
    assert s["pending_confirm"]["level"] == 3
    assert (await client.post("/api/agent/confirm", json={"session_id": sid, "approve": True})).json()["answered"]
    s = await wait_done(client, sid)
    assert s["status"] == "succeeded"


async def test_one_session_per_machine_and_stop(stack):
    client, backend, scripts, _ = stack
    scripts.append([("screenshot", {})] * 200)
    r = (await client.post("/api/tasks", json={"prompt": "loop", "max_steps": 200})).json()
    r2 = await client.post("/api/tasks", json={"prompt": "second"})
    assert r2.status_code == 409
    await client.post("/api/agent/pause", json={"session_id": r["session_id"]})
    await client.post("/api/agent/stop", json={"session_id": r["session_id"]})
    s = await wait_done(client, r["session_id"])
    assert s["status"] == "stopped"
