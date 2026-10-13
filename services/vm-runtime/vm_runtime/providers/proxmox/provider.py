"""ProxmoxProvider - Proxmox VE REST API via API token.

Env:
  PVE_HOST=https://pve.lan:8006   PVE_NODE=pve
  PVE_TOKEN_ID=aiw@pve!runtime    PVE_TOKEN_SECRET=xxxxxxxx-....
  PVE_VERIFY_TLS=false            (self-signed PVE certs)

Create the token with the least privilege role (see deploy/pve/README.md):
VM.PowerMgmt + VM.Snapshot + VM.Snapshot.Rollback + VM.Audit on /vms/100 only.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

import httpx
from aiw_protocols import VMState, VMStatus

from ...vm import VMProvider

SNAP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{1,39}$")


class ProxmoxError(RuntimeError):
    pass


class ProxmoxProvider(VMProvider):
    def __init__(self, host: Optional[str] = None, node: Optional[str] = None,
                 token_id: Optional[str] = None, token_secret: Optional[str] = None,
                 verify_tls: Optional[bool] = None, transport: Optional[httpx.AsyncBaseTransport] = None,
                 task_timeout: float = 120.0):
        self.host = (host or os.environ["PVE_HOST"]).rstrip("/")
        self.node = node or os.environ.get("PVE_NODE", "pve")
        token_id = token_id or os.environ["PVE_TOKEN_ID"]
        token_secret = token_secret or os.environ["PVE_TOKEN_SECRET"]
        if verify_tls is None:
            verify_tls = os.environ.get("PVE_VERIFY_TLS", "true").lower() != "false"
        self.task_timeout = task_timeout
        self.client = httpx.AsyncClient(
            base_url=f"{self.host}/api2/json",
            headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
            verify=verify_tls, timeout=30, transport=transport)

    async def _req(self, method: str, path: str, data: dict | None = None) -> Any:
        r = await self.client.request(method, path, data=data)
        if r.status_code >= 400:
            raise ProxmoxError(f"PVE {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json().get("data")

    def _vm(self, vm_id: str) -> str:
        if not str(vm_id).isdigit():
            raise ValueError(f"invalid PVE vmid {vm_id!r}")
        return f"/nodes/{self.node}/qemu/{vm_id}"

    async def _wait(self, upid: Optional[str]) -> dict:
        """PVE async ops return a UPID; poll until stopped and check exitstatus."""
        if not upid:
            return {"ok": True}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.task_timeout
        while loop.time() < deadline:
            st = await self._req("GET", f"/nodes/{self.node}/tasks/{upid}/status")
            if st.get("status") == "stopped":
                ok = st.get("exitstatus") == "OK"
                if not ok:
                    raise ProxmoxError(f"PVE task {upid} failed: {st.get('exitstatus')}")
                return {"ok": True, "upid": upid}
            await asyncio.sleep(1)
        raise ProxmoxError(f"PVE task {upid} timed out")

    async def status(self, vm_id: str) -> VMStatus:
        d = await self._req("GET", f"{self._vm(vm_id)}/status/current")
        state = {"running": VMState.RUNNING, "stopped": VMState.STOPPED}.get(d.get("status"), VMState.UNKNOWN)
        if d.get("qmpstatus") == "paused":
            state = VMState.PAUSED
        return VMStatus(vm_id=str(vm_id), name=d.get("name"), state=state, cpus=d.get("cpus"),
                        mem_bytes=d.get("maxmem"), uptime_s=d.get("uptime"),
                        ip=await self._guest_ip(vm_id) if state == VMState.RUNNING else None)

    async def _guest_ip(self, vm_id: str) -> Optional[str]:
        """Needs qemu-guest-agent in the VM. Returns first non-loopback IPv4."""
        try:
            d = await self._req("GET", f"{self._vm(vm_id)}/agent/network-get-interfaces")
        except ProxmoxError:
            return None
        for iface in (d or {}).get("result", []):
            for addr in iface.get("ip-addresses", []):
                ip = addr.get("ip-address", "")
                if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                    return ip
        return None

    async def start(self, vm_id: str) -> dict:
        return await self._wait(await self._req("POST", f"{self._vm(vm_id)}/status/start"))

    async def stop(self, vm_id: str, force: bool = False) -> dict:
        # graceful ACPI shutdown by default; force = hard stop
        action = "stop" if force else "shutdown"
        return await self._wait(await self._req("POST", f"{self._vm(vm_id)}/status/{action}"))

    async def restart(self, vm_id: str) -> dict:
        return await self._wait(await self._req("POST", f"{self._vm(vm_id)}/status/reboot"))

    async def snapshot(self, vm_id: str, name: str, description: str = "") -> dict:
        if not SNAP_RE.match(name):
            raise ValueError("snapshot name must match " + SNAP_RE.pattern)
        return await self._wait(await self._req(
            "POST", f"{self._vm(vm_id)}/snapshot", {"snapname": name, "description": description}))

    async def restore(self, vm_id: str, snapshot: str) -> dict:
        if not SNAP_RE.match(snapshot):
            raise ValueError("invalid snapshot name")
        return await self._wait(await self._req("POST", f"{self._vm(vm_id)}/snapshot/{snapshot}/rollback"))

    async def list_snapshots(self, vm_id: str) -> list[dict]:
        return await self._req("GET", f"{self._vm(vm_id)}/snapshot")

    # ---------------------------------------------------------- throw-away clones
    # Needs extra token privileges on the clone pool/storage: VM.Clone, VM.Allocate,
    # Datastore.AllocateSpace, VM.Config.* (see README "隔离电脑").
    async def clone(self, vm_id: str, name: str, snapshot: Optional[str] = None, full: bool = True) -> dict:
        newid = str(await self._req("GET", "/cluster/nextid"))
        data = {"newid": newid, "name": re.sub(r"[^A-Za-z0-9\-]", "-", name)[:60], "full": 1 if full else 0}
        if snapshot:
            if not SNAP_RE.match(snapshot):
                raise ValueError("invalid snapshot name")
            data["snapname"] = snapshot
        await self._wait(await self._req("POST", f"{self._vm(vm_id)}/clone", data))
        return {"vmid": newid}

    async def destroy(self, vm_id: str) -> dict:
        try:
            await self.stop(vm_id, force=True)
        except ProxmoxError:
            pass
        r = await self.client.request("DELETE", f"{self._vm(vm_id)}", params={"purge": 1, "destroy-unreferenced-disks": 1})
        if r.status_code >= 400:
            raise ProxmoxError(f"PVE destroy {vm_id} -> {r.status_code}: {r.text[:300]}")
        return await self._wait(r.json().get("data"))
