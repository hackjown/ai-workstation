"""Cloud computers added from 设置 (V1.4): an InstaBox machine runs the same vm-agent as the Proxmox VM.

A cloud VM is a row in `machines` (provider "instabox", provider_vm_id = the InstaBox machine id, agent_endpoint =
the wss:// URL InstaBox publishes for port 8765) plus its entry in kv "cloud_vms" and its API key in the encrypted
secret box ("cloud_key:<id>"). Tasks, uploads, the desktop view and routines pick it like any other machine.

InstaBox pauses a machine after 30 idle minutes (fixed on the free plan) and a free machine after 8 hours of
continuous running (then refuses to wake for ~10 minutes). Paused, its public URL answers 503 but memory, running
programs and files are kept. So before a task the API wakes it and checks the agent (`wake`), and while the task
runs it holds an activity lease that it renews (`hold`), so the machine never pauses under a running task.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import contextlib
import logging
import re
import secrets as _secrets
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("aiw.cloud")

API = "https://api.instabox.sh"
SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "cloud" / "aiw-cloud.sh"
REMOTE_SCRIPT = "$HOME/.config/aiw/aiw-cloud.sh"
AGENT_PORT = 8765
LEASE_TTL = 900        # seconds; renewed every LEASE_TTL/3 while a task runs
_ID = re.compile(r"[^a-z0-9-]+")



def _cooldown_left(until) -> float:
    """Seconds until an InstaBox cooldownUntil (ISO, UTC) ends; 0 when absent, unreadable or already past."""
    if not until:
        return 0.0
    try:
        t = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (t - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        return 0.0

class CloudError(RuntimeError):
    pass


class InstaBox:
    """The few InstaBox REST calls AIW needs (https://instabox.sh/docs/api)."""

    def __init__(self, api_key: str, base: str = API, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.key, self.base, self.transport = api_key, base.rstrip("/"), transport

    async def _req(self, method: str, path: str, json: Optional[dict] = None, timeout: float = 30,
                   headers: Optional[dict] = None) -> dict:
        h = {"X-API-Key": self.key, **(headers or {})}
        async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as c:
            r = await c.request(method, self.base + path, json=json, headers=h)
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
                msg = f"{err.get('code')}: {err.get('message')}"
            except ValueError:
                msg = r.text[:200]
            raise CloudError(f"InstaBox {r.status_code} {msg}")
        return r.json() if r.content else {}

    async def me(self) -> dict:
        return await self._req("GET", "/v1/me")

    async def machines(self) -> list[dict]:
        return (await self._req("GET", "/v1/machines")).get("machines", [])

    async def machine(self, mid: str) -> dict:
        return await self._req("GET", f"/v1/machines/{mid}")

    async def create(self, name: str) -> str:
        op = await self._req("POST", "/v1/machines", {"templateId": "agent-browser", "name": name},
                             headers={"Idempotency-Key": str(uuid.uuid4())})
        mid = op.get("machineId")
        await self.wait_op(op)
        if not mid:
            raise CloudError("InstaBox did not return the new machine id")
        return mid

    async def wait_op(self, op: dict, timeout: float = 180):
        oid = op.get("id") if "state" in op else op.get("operationId")
        if not oid:
            return
        end = time.time() + timeout
        while time.time() < end:
            o = await self._req("GET", f"/v1/operations/{oid}")
            if o.get("state") in ("succeeded", "failed"):
                if o["state"] == "failed":
                    raise CloudError(f"InstaBox operation failed: {(o.get('error') or {}).get('message') or o.get('phase')}")
                return
            await asyncio.sleep(2)
        raise CloudError("InstaBox operation timed out")

    async def lifecycle(self, mid: str, op: str):
        """op: start | stop | suspend | resume"""
        res = await self._req("POST", f"/v1/machines/{mid}/{op}", headers={"Idempotency-Key": str(uuid.uuid4())})
        await self.wait_op(res)

    async def exec(self, mid: str, cmd: str, timeout: int = 120, stdin: Optional[str] = None,
                   envs: Optional[dict] = None) -> dict:
        body = {"cmd": cmd, "timeoutSeconds": timeout}
        if stdin is not None:
            body["stdin"] = stdin
        if envs:
            body["envs"] = envs
        return await self._req("POST", f"/v1/machines/{mid}/exec", body, timeout=timeout + 30)

    async def publish(self, mid: str, port: int, name: str) -> str:
        return (await self._req("POST", f"/v1/machines/{mid}/ports", {"port": port, "name": name}))["url"]

    async def lease(self, mid: str, lease_id: str, ttl: int = LEASE_TTL):
        await self._req("PUT", f"/v1/machines/{mid}/activity-leases/{lease_id}", {"ttlSeconds": ttl}, timeout=60)

    async def release(self, mid: str, lease_id: str):
        await self._req("DELETE", f"/v1/machines/{mid}/activity-leases/{lease_id}")

    async def desktop(self, mid: str, action: str = "status") -> dict:
        return await self._req("POST", f"/v1/machines/{mid}/desktop", {"action": action}, timeout=120)

    async def stream_url(self, mid: str, view_only: bool = True, ttl: int = 3600) -> str:
        return (await self._req("POST", f"/v1/machines/{mid}/desktop/stream",
                                {"viewOnly": view_only, "ttlSeconds": ttl}))["url"]

    async def snapshot(self, mid: str, name: str) -> dict:
        res = await self._req("POST", f"/v1/machines/{mid}/snapshots", {"name": name},
                              headers={"Idempotency-Key": str(uuid.uuid4())})
        await self.wait_op(res, 600)
        return res


class CloudManager:
    def __init__(self, store, secrets, agent_token: str, transport: Optional[httpx.AsyncBaseTransport] = None,
                 on_change=None):
        self.store, self.secrets, self.agent_token = store, secrets, agent_token or ""
        self.transport = transport
        self.on_change = on_change          # callback(mid): forget cached connections
        self._jobs: dict[str, asyncio.Task] = {}
        self._wake_locks: dict[str, asyncio.Lock] = {}
        self._state_cache: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------ registry
    def entries(self) -> dict:
        return self.store.kv_get("cloud_vms", {}) or {}

    def entry(self, mid: str) -> Optional[dict]:
        return self.entries().get(mid)

    def is_cloud(self, mid: str) -> bool:
        return mid in self.entries()

    def _save(self, mid: str, **kw):
        all_ = self.entries()
        all_[mid] = {**all_.get(mid, {}), **kw, "updated_at": time.time()}
        self.store.kv_set("cloud_vms", all_)

    def _log(self, mid: str, line: str):
        e = self.entry(mid) or {}
        lines = (e.get("log") or [])[-30:] + [time.strftime("%H:%M:%S ") + line]
        self._save(mid, log=lines)

    def client(self, mid: Optional[str] = None, key: Optional[str] = None) -> InstaBox:
        key = key or (self.secrets.get(f"cloud_key:{mid}") if mid else None)
        if not key:
            raise CloudError("no InstaBox API key saved for this machine")
        return InstaBox(key, transport=self.transport)

    def list(self) -> list[dict]:
        out = []
        for mid, e in self.entries().items():
            out.append({"id": mid, **{k: v for k, v in e.items() if k != "log"}, "log": (e.get("log") or [])[-8:]})
        return out

    async def add(self, name: str, api_key: str, remote_id: Optional[str] = None) -> dict:
        api_key = (api_key or "").strip()
        if not api_key.startswith("ibk_"):
            raise CloudError("InstaBox API key starts with ibk_ (Profile → API keys)")
        ib = InstaBox(api_key, transport=self.transport)
        await ib.me()  # bad key -> 401 now, not in the background
        base = _ID.sub("-", (name or "cloud").lower()).strip("-")[:30] or "cloud"
        mid = f"cloud-{base}"
        n = 2
        while self.store.machine(mid) or self.is_cloud(mid):
            mid, n = f"cloud-{base}-{n}", n + 1
        self.secrets.put(f"cloud_key:{mid}", api_key)
        self._save(mid, name=name or mid, provider="instabox", remote_id=remote_id or None, status="adding", log=[])
        self.store.upsert_machine({"id": mid, "name": name or mid, "provider": "instabox",
                                   "provider_vm_id": remote_id or "", "agent_endpoint": "wss://pending.invalid",
                                   "desktop_endpoint": "instabox:"})
        self.start_setup(mid)
        return {"id": mid, **(self.entry(mid) or {})}

    def remove(self, mid: str):
        job = self._jobs.pop(mid, None)
        if job:
            job.cancel()
        all_ = self.entries()
        all_.pop(mid, None)
        self.store.kv_set("cloud_vms", all_)
        self.secrets.delete(f"cloud_key:{mid}")
        self.store.delete_machine(mid)
        if self.on_change:
            self.on_change(mid)

    # ------------------------------------------------------------------ setup
    def start_setup(self, mid: str):
        if mid in self._jobs and not self._jobs[mid].done():
            return
        self._jobs[mid] = asyncio.create_task(self._setup(mid))

    async def _setup(self, mid: str):
        try:
            ib = self.client(mid)
            e = self.entry(mid) or {}
            rid = e.get("remote_id")
            if not rid:
                self._log(mid, "creating an InstaBox machine (agent-browser, 4 vCPU / 8 GiB)…")
                self._save(mid, status="creating")
                rid = await ib.create(_ID.sub("-", mid.lower())[:63])
                self._save(mid, remote_id=rid)
            await self._wake_remote(ib, rid, mid)
            self._save(mid, status="installing")
            self._log(mid, "uploading the setup script")
            await ib.exec(rid, f"mkdir -p $HOME/.config/aiw && cat > {REMOTE_SCRIPT} && chmod +x {REMOTE_SCRIPT}",
                          60, stdin=SCRIPT.read_text())
            self._log(mid, "installing LibreCAD, Chinese fonts, vm-agent (3-6 minutes)…")
            await ib.desktop(rid, "start")
            await ib.exec(rid, f"bash {REMOTE_SCRIPT} install", 60)
            last, end = "", time.time() + 1500
            while time.time() < end:
                await ib.lease(rid, f"aiw-setup-{mid}", 600)
                st = (await ib.exec(rid, f"bash {REMOTE_SCRIPT} status", 30)).get("stdout", "").strip()
                if st != last:
                    self._log(mid, st.splitlines()[0] if st else "?")
                    last = st
                if st.startswith("INSTALLED"):
                    break
                if st.startswith("FAILED"):
                    raise CloudError(st[:300])
                await asyncio.sleep(10)
            else:
                raise CloudError("install did not finish in 25 minutes")
            url = await ib.publish(rid, AGENT_PORT, "aiw-agent")
            ws = re.sub(r"^http", "ws", url.rstrip("/"))
            self.store.upsert_machine({"id": mid, "name": (self.entry(mid) or {}).get("name") or mid, "provider": "instabox",
                                       "provider_vm_id": rid, "agent_endpoint": ws, "desktop_endpoint": "instabox:"})
            self._save(mid, agent_url=ws)
            await self._ensure_agent(ib, rid, mid)
            with contextlib.suppress(CloudError):
                await ib.release(rid, f"aiw-setup-{mid}")
            self._save(mid, status="ready")
            self._log(mid, f"ready: {ws}")
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            log.warning("cloud setup %s: %s", mid, ex)
            self._save(mid, status="error", error=str(ex)[:300])
            self._log(mid, f"error: {ex}")
        finally:
            if self.on_change:
                self.on_change(mid)

    # ------------------------------------------------------------------ runtime
    async def state(self, mid: str, max_age: float = 20) -> dict:
        """The InstaBox view of the machine (cached briefly: the console polls every 10 s)."""
        c = self._state_cache.get(mid)
        if c and time.time() - c[0] < max_age:
            return c[1]
        e = self.entry(mid) or {}
        if not e.get("remote_id"):
            return {"state": e.get("status") or "unknown"}
        m = await self.client(mid).machine(e["remote_id"])
        st = {"state": m.get("state"), "cooldown_until": m.get("cooldownUntil"), "auto_suspend_s": m.get("autoSuspendAfterSeconds"),
              "max_run_min": m.get("maxRunMinutes"), "run_started_at": m.get("runStartedAt"),
              "spec": m.get("spec"), "free": m.get("free")}
        self._state_cache[mid] = (time.time(), st)
        return st

    async def _wake_remote(self, ib: InstaBox, rid: str, mid: str):
        m = await ib.machine(rid)
        st = m.get("state")
        if st == "running":
            return
        cd = _cooldown_left(m.get("cooldownUntil"))
        if cd > 0:  # a past cooldownUntil can linger on the record: only a future one blocks the start
            local = (datetime.now(timezone.utc) + timedelta(seconds=cd)).astimezone(timezone(timedelta(hours=8)))
            raise CloudError(f"InstaBox 连续运行满 8 小时后的强制冷却期，约 {int(cd // 60) + 1} 分钟后"
                             f"（北京时间 {local:%H:%M}）可再次启动 (cooldownUntil {m['cooldownUntil']})")
        self._log(mid, f"waking the machine ({st})")
        await ib.lifecycle(rid, "start" if st == "stopped" else "resume")
        for _ in range(60):
            if (await ib.machine(rid)).get("state") == "running":
                return
            await asyncio.sleep(2)
        raise CloudError("InstaBox machine did not come back to running")

    async def _ensure_agent(self, ib: InstaBox, rid: str, mid: str):
        r = await ib.exec(rid, f"bash {REMOTE_SCRIPT} ensure", 60, envs={"VM_AGENT_TOKEN": self.agent_token})
        out = (r.get("stdout") or "").strip()
        if not out.startswith("AGENT_"):
            raise CloudError(f"vm-agent did not start: {out or r.get('stderr', '')[:200]}")
        if out.startswith("AGENT_FAILED"):
            raise CloudError(out[:300])
        if out.startswith("AGENT_STARTED") and self.on_change:
            self.on_change(mid)   # a fresh agent process: drop the old WebSocket

    async def wake(self, mid: str):
        """Make sure the machine runs and its agent answers (before a task, or when the user opens it)."""
        e = self.entry(mid) or {}
        if e.get("status") != "ready":
            raise CloudError(f"cloud machine {mid} is not set up yet ({e.get('status')})")
        lock = self._wake_locks.setdefault(mid, asyncio.Lock())
        async with lock:
            ib = self.client(mid)
            await self._wake_remote(ib, e["remote_id"], mid)
            await ib.lease(e["remote_id"], f"aiw-wake-{mid}", 300)   # don't fall asleep between wake and task start
            await self._ensure_agent(ib, e["remote_id"], mid)
            self._state_cache.pop(mid, None)

    @contextlib.asynccontextmanager
    async def hold(self, mid: str, why: str = "task"):
        """Keep the machine awake while a task runs: a renewed activity lease, released at the end."""
        e = self.entry(mid) or {}
        ib, rid = self.client(mid), e.get("remote_id")
        lid = f"aiw-{why}-{_secrets.token_hex(4)}"

        async def renew():
            while True:
                try:
                    await ib.lease(rid, lid, LEASE_TTL)
                except Exception as ex:  # noqa: BLE001
                    log.warning("lease %s: %s", mid, ex)
                await asyncio.sleep(LEASE_TTL / 3)
        t = asyncio.create_task(renew())
        try:
            yield
        finally:
            t.cancel()
            with contextlib.suppress(Exception):
                await ib.release(rid, lid)

    async def desktop_url(self, mid: str, view_only: bool) -> str:
        e = self.entry(mid) or {}
        ib = self.client(mid)
        return await ib.stream_url(e["remote_id"], view_only=view_only, ttl=3600)

    async def power(self, mid: str, op: str):
        e = self.entry(mid) or {}
        ib = self.client(mid)
        if op == "start":
            await self.wake(mid)
        elif op == "stop":
            await ib.lifecycle(e["remote_id"], "suspend")
        elif op == "restart":
            await ib.lifecycle(e["remote_id"], "stop")
            await self.wake(mid)
        self._state_cache.pop(mid, None)
