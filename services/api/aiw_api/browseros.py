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

Detection: a vm-agent from before the BrowserOS release sends no "browseros" in system.health; then refresh() asks
the computer's shell (`browseros-setup.sh status`), so 重新检测 still tells installed / running, and the row says the
agent is old (`agent_old`) because the bos_* tools need the newer agent.
Install errors: the script ends a failure with `ERROR: <step>: <reason>`; summarize() turns stdout/stderr into that
line plus the few lines before it (curl's progress meter and apt chatter dropped). A sudo that needs a password comes
back as need_sudo with the one-line command to paste into the computer's terminal (no password goes through Aiw).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("aiw.browseros")

_STORE = None          # set by mount(); the probes look up the switch by machine id without a store handle
KV, SEEN = "browseros", "browseros_seen"
SETUP = Path(__file__).resolve().parents[3] / "deploy" / "vm" / "browseros-setup.sh"
SETUP_URL = "https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/browseros-setup.sh"
AGENT_URL = "https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/update-agent.sh"
# what the user pastes into the computer's own terminal (接管桌面 › 终端, or ssh) when sudo there needs a password;
# the second half updates the vm-agent so it reports BrowserOS and can drive it
MANUAL_CMD = f"curl -fsSL {SETUP_URL} | bash && curl -fsSL {AGENT_URL} | bash"


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
    sn = seen(store, mid)
    # an old agent cannot proxy bos_* calls: installed but not usable until the agent is updated
    return bool(sn.get("installed")) and not sn.get("agent_old") and (s is None or s)


def set_enabled(store, mid: str, on: bool) -> dict:
    d = _kv(store, KV)
    d[mid] = {**(d.get(mid) or {}), "enabled": bool(on), "at": time.time()}
    store.kv_set(KV, d)
    return d[mid]


def _record(store, mid: str, b: dict, **extra) -> dict:
    d = _kv(store, SEEN)
    d[mid] = {"installed": bool(b.get("installed")), "running": bool(b.get("running")), "version": b.get("version") or None,
              "mcp_port": b.get("mcp_port") or 9210, "cdp_port": b.get("cdp_port") or 9110, "at": time.time(),
              **{k: v for k, v in extra.items() if v is not None}}
    try:
        store.kv_set(SEEN, d)
    except Exception as e:  # noqa: BLE001
        log.debug("browseros seen: %s", e)
    return d[mid]


def note_health(store, mid: str, health: Optional[dict]) -> None:
    """Record what the vm-agent's system.health said about BrowserOS (older agents send nothing: leave it)."""
    if not store or not mid or not isinstance(health, dict) or "browseros" not in health:
        return
    _record(store, mid, health.get("browseros") or {}, arch=health.get("arch"), agent_old=False)


def _script_cmd(sub: str) -> str:
    """Run this image's browseros-setup.sh on the computer (shipped inline; the raw GitHub copy as a fallback)."""
    if SETUP.exists():
        b64 = base64.b64encode(SETUP.read_bytes()).decode()
        return (f"f=$(mktemp) && echo '{b64}' | base64 -d > \"$f\" && bash \"$f\" {sub}; rc=$?; rm -f \"$f\"; exit $rc")
    return f"curl -fsSL {SETUP_URL} | bash -s {sub}"


def parse_status(stdout: str) -> Optional[dict]:
    """The JSON line of `browseros-setup.sh status` (or None)."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and '"installed"' in line:
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


async def shell_status(comp, timeout: float = 20) -> Optional[dict]:
    """Ask the computer's shell what is installed: works with any vm-agent version."""
    try:
        r = await asyncio.wait_for(comp.execute("shell.exec", {"command": _script_cmd("status"), "timeout": 15},
                                                timeout=timeout), timeout + 2)
    except Exception as e:  # noqa: BLE001
        log.debug("browseros shell status: %s", e)
        return None
    return parse_status((getattr(r, "result", None) or {}).get("stdout") or "")


# ----------------------------------------------------------------------------- install output -> what to show
_PROGRESS = re.compile(r"(%\s*Total|%\s*Received|Dload\s+Upload|Total\s+Spent)|^[\s\d.:kKMGB%#-]+$")
_APT_NOISE = re.compile(r"^(Reading|Building|Get:|Hit:|Ign:|Fetched|Selecting|Preparing|Unpacking|Setting up|Processing|"
                        r"\(Reading database|WARNING: apt does not have a stable CLI)")
_SUDO_PW = re.compile(r"NEED_SUDO_PASSWORD|sudo: (a password is required|需要密码|a terminal is required)|"
                      r"sudo:.*(password|密码)", re.I)
_ERRLIKE = re.compile(r"^(E: |dpkg: |dpkg-deb: |curl: \(|sudo: |ERROR|Error|error:|FAILED|Traceback)")


def meaningful_lines(text: str) -> list[str]:
    """Output lines without curl's progress meter (\r-redrawn), apt chatter or blank lines."""
    out = []
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or _PROGRESS.search(line) or _APT_NOISE.match(line):
            continue
        out.append(line)
    return out


def summarize(stdout: str, stderr: str, exit_code) -> dict:
    """{"error": one line for the UI or None, "detail": [up to 4 lines], "need_sudo": bool, "step": str|None}."""
    so, se = meaningful_lines(stdout), meaningful_lines(stderr)
    allv = so + se
    need_sudo = exit_code == 10 or any(_SUDO_PW.search(l) for l in allv)
    if exit_code in (0, None) and not need_sudo:
        return {"error": None, "detail": [], "need_sudo": False, "step": None}
    if need_sudo:
        return {"error": "需要这台电脑的 sudo 密码", "detail": [], "need_sudo": True, "step": "sudo"}
    err = next((l for l in reversed(so) if l.startswith("ERROR:")), None)
    if err:
        m = re.match(r"ERROR:\s*([\w-]+):\s*(.*)", err)
        i = so.index(err)
        detail = [l.lstrip("| ").strip() for l in so[max(0, i - 4):i] if l.startswith("|")]
        if not detail:
            detail = [l for l in se if _ERRLIKE.match(l)][-3:]
        return {"error": err[len("ERROR:"):].strip(), "detail": detail[-4:], "need_sudo": False,
                "step": m.group(1) if m else None}
    if exit_code == 124:
        return {"error": "安装超时（15 分钟）", "detail": allv[-3:], "need_sudo": False, "step": "timeout"}
    errs = [l for l in allv if _ERRLIKE.match(l)]
    detail = (errs or allv)[-3:]
    return {"error": detail[-1] if detail else f"安装脚本退出码 {exit_code}", "detail": detail, "need_sudo": False,
            "step": None}


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
    """Ask the computer now (system.health) and record it. An agent too old to report BrowserOS gets asked through
    its shell instead, so the row shows installed / running and says the agent needs an update."""
    try:
        h = await asyncio.wait_for(computer.health(), timeout)
        if getattr(h, "success", False) and isinstance(h.result, dict):
            if "browseros" in h.result:
                note_health(store, mid, h.result)
            else:
                b = await shell_status(computer, max(timeout, 10))
                if b is not None:
                    _record(store, mid, b, arch=b.get("arch") or h.result.get("arch"), agent_old=True)
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
                "setting": setting(store, mid), "enabled": enabled(store, mid), "seen": seen(store, mid),
                "command": MANUAL_CMD if seen(store, mid).get("agent_old") else None}

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
        r = await comp.execute("shell.exec", {"command": _script_cmd("install"), "timeout": 900}, timeout=930)
        res = getattr(r, "result", None) or {}
        code = res.get("exit_code")
        st = await refresh(store, comp, mid, 10)
        ran = bool(getattr(r, "success", False)) or code == 0
        ok = ran and bool(st.get("installed"))
        s = summarize(res.get("stdout") or "", res.get("stderr") or "", code if res else None)
        err = None
        if not ok:
            err = s["error"] or getattr(r, "error", None) or "安装没有完成"
            if not s["error"] and ran and not st.get("installed"):
                err = "脚本跑完了，但没检测到 BrowserOS"
        out = {"ok": ok, "seen": st, "exit_code": code, "error": err, "detail": s["detail"], "step": s["step"],
               "need_sudo": s["need_sudo"], "agent_old": bool(st.get("agent_old")),
               "log": "\n".join(meaningful_lines((res.get("stdout") or "") + "\n" + (res.get("stderr") or "")))[-3000:]}
        if s["need_sudo"] or st.get("agent_old"):
            out["command"] = MANUAL_CMD
        return out
