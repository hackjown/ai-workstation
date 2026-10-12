"""ProxmoxProvider against a fake PVE API (httpx MockTransport)."""
import httpx
import pytest
from aiw_protocols import VMState
from vm_runtime import ProxmoxProvider, VMRuntime

calls = []


def handler(req: httpx.Request):
    calls.append((req.method, req.url.path, req.headers.get("Authorization")))
    p = req.url.path
    if p.endswith("/status/current"):
        return httpx.Response(200, json={"data": {"status": "running", "name": "ai-linux-001", "cpus": 4,
                                                   "maxmem": 8 << 30, "uptime": 42}})
    if p.endswith("/agent/network-get-interfaces"):
        return httpx.Response(200, json={"data": {"result": [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
            {"name": "ens18", "ip-addresses": [{"ip-address": "192.168.1.50", "ip-address-type": "ipv4"}]}]}})
    if "/tasks/" in p:
        return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
    if req.method == "POST":
        return httpx.Response(200, json={"data": "UPID:pve:0001:task"})
    if p.endswith("/snapshot"):
        return httpx.Response(200, json={"data": [{"name": "clean"}]})
    return httpx.Response(404, text="nope")


@pytest.fixture
def pve():
    calls.clear()
    return ProxmoxProvider("https://pve:8006", "pve", "aiw@pve!rt", "secret",
                           transport=httpx.MockTransport(handler))


async def test_status(pve):
    s = await pve.status("100")
    assert s.state == VMState.RUNNING and s.ip == "192.168.1.50" and s.cpus == 4
    assert calls[0][2] == "PVEAPIToken=aiw@pve!rt=secret"


async def test_lifecycle(pve):
    rt = VMRuntime(pve, {"ai-linux-001": "100"})
    for fn in (rt.start, rt.stop, rt.restart):
        assert (await fn("ai-linux-001"))["ok"]
    assert (await rt.snapshot("ai-linux-001", "clean"))["ok"]
    assert (await rt.restore("ai-linux-001", "clean"))["ok"]
    paths = [c[1] for c in calls if c[0] == "POST"]
    assert "/api2/json/nodes/pve/qemu/100/status/shutdown" in paths
    assert "/api2/json/nodes/pve/qemu/100/snapshot/clean/rollback" in paths


async def test_rejects_bad_input(pve):
    with pytest.raises(ValueError):
        await pve.status("100; rm")
    with pytest.raises(ValueError):
        await pve.snapshot("100", "../x")
    with pytest.raises(KeyError):
        await VMRuntime(pve, {}).start("ai-linux-001")
