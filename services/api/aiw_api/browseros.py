"""BrowserOS neo per computer: the 设置 › 电脑 switch, what the heartbeat saw, and where web probes look.

kv "browseros"       {mid: {"enabled": true|false}}   the user's switch; absent = on when the computer has BrowserOS
kv "browseros_seen"  {mid: {installed, running, version, mcp_port, cdp_port, at}}   last system.health report

When BrowserOS is on for a computer:
  * its tasks get the bos_* tools (element refs from the accessibility snapshot) next to the screenshot tools;
  * the price-watch and 小红书 probes read pages over BrowserOS's DevTools port (9110), because that is the browser
    the user logs in to and the agent works in;
  * login notices tell the user to log in inside BrowserOS, not Chrome.

Routes: GET /api/browseros, PUT /api/browseros/{mid} {"enabled": bool}, POST /api/browseros/{mid}/install
(runs deploy/vm/browseros-setup.sh on the computer through the vm-agent's shell, amd64 only).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("aiw.browseros")

_STORE = None          # set by mount(); the probes look up the switch by machine id without a store handle
KV, SEEN = "browseros", "browseros_seen"
SETUP = Path(__file__).resolve().parents[3] / "deploy" / "vm" / "browseros-setup.sh"
SETUP_URL = "https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/browseros-setup.sh"


def bind(store) -> None:
    global _STORE
    _STORE = store


def _kv(store, key: str) -> dict:
    try:
        return dict(store.kv_get(key, {}) or {})
    except Exception:  # noqa: BLE001
        return {}


def seen(store, mid: str) -> dict:
    return _kv(store, SEEN).get(mid) or {}


def setting(store, mid: str) -> Optional[bool]:
    v = (_kv(store, KV).get(mid) or {}).get("enabled")
    return None if v is None else bool(v)


def enabled(store, mid: Optional[str]) -> bool:
    """On = the user switched it on, or left the switch alone and the computer reported BrowserOS installed."""
    if not store or not mid:
        return False
    s = setting(store, mid)
    inst = bool(seen(store, mid).get("installed"))
    return inst and (s is None or s)


def set_enabled(store, mid: str, on: bool) -> dict:
    d = _kv(store, KV)
    d[mid] = {**(d.get(mid) or {}), "enabled": bool(on), "at": time.time()}
    store.kv_set(KV, d)
    return d[mid]


def note_health(store, mid: str, health: Optional[dict]) -> None:
    """Record what the vm-agent's system.health said about BrowserOS (older agents send nothing: leave it)."""
    if not store or not mid or not isinstance(health, dict) or "browseros" not in health:
        return
    b = health.get("browseros") or {}
    d = _kv(store, SEEN)
    d[mid] = {"installed": bool(b.get("installed")), "running": bool(b.get("running")), "version": b.get("version"),
              "mcp_port": b.get("mcp_port") or 9210, "cdp_port": b.get("cdp_port") or 9110, "at": time.time()}
    try:
        store.kv_set(SEEN, d)
    except Exception as e:  # noqa: BLE001
        log.debug("browseros seen: %s", e)


def cdp_port(store, mid: Optional[str]) -> Optional[int]:
    if not enabled(store, mid):
        return None
    return int(seen(store, mid).get("cdp_port") or 9110)


def probe_env(computer) -> str:
    """Shell prefix that points a VM-side probe (shop_probe / xhs_probe read VM_AGENT_CDP) at BrowserOS."""
    port = cdp_port(_STORE, getattr(computer, "machine_id", None))
    return f"VM_AGENT_CDP=http://127.0.0.1:{port} " if port else ""


def browser_name(store, mid: Optional[str]) -> str:
    return "BrowserOS" if enabled(store or _STORE, mid) else "Chrome"


async def refresh(store, computer, mid: str, timeout: float = 6) -> dict:
    """Ask the computer now (system.health) and record it."""
    try:
        h = await asyncio.wait_for(computer.health(), timeout)
        if getattr(h, "success", False) and isinstance(h.result, dict):
            note_health(store, mid, h.result)
    except Exception as e:  # noqa: BLE001
        log.debug("browseros refresh %s: %s", mid, e)
    return seen(store, mid)


async def for_task(store, computer, mid: Optional[str]) -> bool:
    """Should this task get the bos_* tools? A switch turned off answers at once; otherwise a fresh health look
    (cached for 2 minutes) decides, so a BrowserOS installed a minute ago is picked up."""
    if not mid or setting(store, mid) is False:
        return False
    s = seen(store, mid)
    if not s or time.time() - float(s.get("at") or 0) > 120:
        await refresh(store, computer, mid)
    return enabled(store, mid)


PROMPT = """BROWSEROS (this computer has BrowserOS; prefer it for web pages):
- Work web pages with the bos_* tools: bos_tabs action=new url=… opens a page in your own tab group and returns its
  page id and a snapshot. Every actionable element carries [ref=eN]; bos_act kind=click/fill/select/press acts on it by
  ref and returns a short diff, so you do NOT need a screenshot after it. Refs go stale after navigation or big
  changes: take a new bos_snapshot (or bos_grep for one element) before acting again.
- bos_read for reading prices/articles, bos_grep to find one button cheaply; keep snapshots in mode=interactive.
- For passwords and other secrets use {{key}} in bos_act fill/type values exactly as given; it is filled in for you.
- Fall back to screenshot + click by coordinates only when an element has no ref (canvas, captcha-free custom widgets):
  first bos_tabs action=front page=N so that page is the visible tab, then screenshot.
- Logins and cookies live in BrowserOS's own profile: when a site needs a login, ask the user to log in inside
  BrowserOS through request_takeover (once; it is remembered).
- Do not talk to BrowserOS's ports (9110/9210) from the shell."""


def mount(app, store, sessions, auth) -> None:
    from fastapi import Depends, HTTPException
    bind(store)
    dep = [Depends(auth)]

    def row(m: dict) -> dict:
        mid = m["id"]
        return {"id": mid, "name": m.get("name") or mid, "cloud": (m.get("provider") or "") == "instabox",
                "setting": setting(store, mid), "enabled": enabled(store, mid), "seen": seen(store, mid)}

    @app.get("/api/browseros", dependencies=dep)
    async def bos_list(refresh_now: bool = False):
        ms = store.machines()
        if refresh_now:
            async def one(m):
                try:
                    await refresh(store, sessions.computer(m["id"]), m["id"], 5)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.gather(*(one(m) for m in ms))
        return {"machines": [row(m) for m in ms]}

    @app.put("/api/browseros/{mid}", dependencies=dep)
    async def bos_put(mid: str, body: dict):
        if not store.machine(mid):
            raise HTTPException(404, "no such computer")
        set_enabled(store, mid, bool((body or {}).get("enabled")))
        return row(store.machine(mid))

    @app.post("/api/browseros/{mid}/install", dependencies=dep)
    async def bos_install(mid: str):
        m = store.machine(mid)
        if not m:
            raise HTTPException(404, "no such computer")
        if sessions.busy_session(mid):
            raise HTTPException(409, "这台电脑正在跑任务，等它结束再安装")
        cloud = getattr(sessions, "cloud", None)
        if cloud and cloud.is_cloud(mid):
            await cloud.wake(mid)
        comp = sessions.computer(mid)
        script = SETUP.read_text() if SETUP.exists() else None
        if script:
            import base64
            cmd = ("f=$(mktemp) && echo '" + base64.b64encode(script.encode()).decode() + "' | base64 -d > \"$f\" && "
                   "bash \"$f\" install; rc=$?; rm -f \"$f\"; exit $rc")
        else:
            cmd = f"curl -fsSL {SETUP_URL} | bash -s install"
        r = await comp.execute("shell.exec", {"command": cmd, "timeout": 900}, timeout=930)
        res = getattr(r, "result", None) or {}
        st = await refresh(store, comp, mid, 10)
        ok = bool(getattr(r, "success", False)) and bool(st.get("installed"))
        return {"ok": ok, "seen": st, "exit_code": res.get("exit_code"),
                "log": ((res.get("stdout") or "") + "\n" + (res.get("stderr") or ""))[-3000:],
                "error": None if ok else (getattr(r, "error", None) or "安装没有完成（看日志）")}
