"""V1.4: InstaBox cloud computers added from 设置."""
import asyncio
import json
import os

import httpx
import pytest

os.environ.setdefault("AIW_SECRET_KEY", "test-key")


def fake_instabox(state):
    calls = state.setdefault("calls", [])

    def handler(req: httpx.Request):
        p, m = req.url.path, req.method
        body = json.loads(req.content or b"{}") if req.content else {}
        calls.append((m, p, body.get("cmd", "")[:120] if isinstance(body, dict) else ""))
        if req.headers.get("X-API-Key") != "ibk_test":
            return httpx.Response(401, json={"error": {"code": "unauthenticated", "message": "bad key"}})
        if p == "/v1/me":
            return httpx.Response(200, json={"email": "a@b.c", "allowedSpecs": [{"vcpus": 4}]})
        if p == "/v1/machines/m_1" and m == "GET":
            return httpx.Response(200, json={"id": "m_1", "state": state.get("vm", "running"), "cooldownUntil": None,
                                             "autoSuspendAfterSeconds": 1800, "maxRunMinutes": 480})
        if p.endswith("/resume"):
            state["vm"] = "running"
            return httpx.Response(202, json={"id": "op1", "state": "queued", "machineId": "m_1"})
        if p == "/v1/operations/op1":
            return httpx.Response(200, json={"id": "op1", "state": "succeeded"})
        if p.endswith("/exec"):
            cmd = body["cmd"]
            if "status" in cmd:
                out = "INSTALLED"
            elif "ensure" in cmd:
                assert body["envs"]["VM_AGENT_TOKEN"] == "agent-secret"
                out = "AGENT_STARTED"
            else:
                out = "INSTALLING start"
            return httpx.Response(200, json={"stdout": out, "stderr": "", "exitCode": 0})
        if p.endswith("/desktop"):
            return httpx.Response(200, json={"running": True})
        if p.endswith("/desktop/stream"):
            return httpx.Response(200, json={"url": "https://view.instabox.sh/x", "expiresAt": "z"})
        if p.endswith("/ports"):
            return httpx.Response(200, json={"url": "https://8765-1.instabox.tech"})
        if "/activity-leases/" in p:
            return httpx.Response(200, json={"id": "l"})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": p}})
    return httpx.MockTransport(handler)


@pytest.fixture
def mgr(tmp_path):
    from aiw_api.cloud import CloudManager
    from aiw_api.crypto import SecretBox
    from aiw_api.store import Store
    store = Store(str(tmp_path / "db.sqlite"))
    state = {}
    dropped = []
    m = CloudManager(store, SecretBox(store, str(tmp_path)), "agent-secret", transport=fake_instabox(state),
                     on_change=dropped.append)
    return m, store, state, dropped


async def test_add_existing_machine_installs_and_publishes(mgr):
    m, store, state, dropped = mgr
    from aiw_api.cloud import CloudError
    with pytest.raises(CloudError):
        await m.add("box", "ibk_wrong", "m_1")
    e = await m.add("My Box", "ibk_test", "m_1")
    mid = e["id"]
    assert mid == "cloud-my-box" and store.machine(mid)["provider"] == "instabox"
    await m._jobs[mid]
    ent = m.entry(mid)
    assert ent["status"] == "ready", ent
    row = store.machine(mid)
    assert row["agent_endpoint"] == "wss://8765-1.instabox.tech" and row["provider_vm_id"] == "m_1"
    cmds = [c for _, p, c in state["calls"] if p.endswith("/exec")]
    assert any("cat > " in c for c in cmds) and any("ensure" in c for c in cmds)
    assert mid in dropped


async def test_wake_resumes_paused_machine_and_hold_leases(mgr):
    m, store, state, dropped = mgr
    e = await m.add("b", "ibk_test", "m_1")
    await m._jobs[e["id"]]
    state["vm"] = "paused"
    state["calls"].clear()
    await m.wake(e["id"])
    paths = [p for _, p, _ in state["calls"]]
    assert any(p.endswith("/resume") for p in paths) and any("/activity-leases/" in p for p in paths)
    state["calls"].clear()
    async with m.hold(e["id"]):
        await asyncio.sleep(0.05)
    ms = [(mm, p) for mm, p, _ in state["calls"] if "/activity-leases/" in p]
    assert ms[0][0] == "PUT" and ms[-1][0] == "DELETE"
    assert (await m.desktop_url(e["id"], True)).startswith("https://view.instabox.sh")
    m.remove(e["id"])
    assert store.machine(e["id"]) is None and not m.is_cloud(e["id"])
