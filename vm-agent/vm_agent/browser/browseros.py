"""BrowserOS neo on this computer: a local proxy to its MCP server, so the AIW agent acts on page elements
(accessibility snapshot -> [ref=eN] -> act) instead of screenshot pixels.

BrowserOS neo (Debian package `browserclaw`, Chromium 155) runs its own MCP server (streamable HTTP) on
127.0.0.1:9210/mcp and CDP on 127.0.0.1:9110 (ports from ~/.config/browser-claw/.browseros/config.json). The server
only listens on localhost, so the API reaches it through this agent (browseros.call).

One MCP connection per AIW task (payload "key" = task id): BrowserOS gives every connection its own tab group, so a
task's tabs stay together and BrowserOS keeps the session's replay. The tools that run arbitrary page JavaScript or
touch saved passwords/profiles (run, evaluate, credentials, ...) are not reachable through here at all: the API's
safety gates judge each click/type by the element it targets, and a JS escape hatch would walk around them.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import time
from typing import Optional

import httpx

CONFIG = os.path.expanduser(os.environ.get("BROWSEROS_CONFIG", "~/.config/browser-claw/.browseros/config.json"))
BINARIES = ("browserclaw", "browseros", "browseros-neo")
# what the API may call. Everything else (run/evaluate: arbitrary JS; credentials/browser_profiles/browser_target/
# windows/history; save_skill/mark_skill_run; request_human_help: AIW has its own takeover) is refused here.
ALLOWED = {"tabs", "navigate", "snapshot", "diff", "act", "read", "grep", "wait", "upload", "screenshot", "name_session"}
MAX_TEXT = int(os.environ.get("VM_AGENT_BOS_MAX_TEXT", "60000"))
# calls after which the page they worked on is brought to the front of the screen (the user watching the desktop, or
# taking it over, sees the page the agent is on; BrowserOS itself opens every agent page in the background)
FRONT_TOOLS = {"tabs", "navigate", "act", "read", "snapshot", "grep", "diff", "wait", "upload", "screenshot"}
FRONT_SKIP_S = 30   # same page, same active window as last time, within this long: already in front
IDLE_S = 4 * 3600   # forget a task's MCP connection after this long unused
_TIP = re.compile(r"^Tip: this session is .*$", re.M)


def ports() -> tuple[int, int]:
    """(mcp, cdp) ports from BrowserOS's own config, falling back to its defaults."""
    try:
        with open(CONFIG) as f:
            p = json.load(f).get("ports") or {}
        return int(p.get("server") or 9210), int(p.get("cdp") or 9110)
    except Exception:  # noqa: BLE001
        return int(os.environ.get("BROWSEROS_MCP_PORT", "9210")), int(os.environ.get("BROWSEROS_CDP_PORT", "9110"))


def binary() -> Optional[str]:
    for b in BINARIES:
        p = shutil.which(b)
        if p:
            return p
    return "/usr/bin/browserclaw" if os.path.exists("/usr/bin/browserclaw") else None


def _port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def version() -> Optional[str]:
    try:
        with open(CONFIG) as f:
            i = json.load(f).get("instance") or {}
        return i.get("browseros_version") or i.get("chromium_version")
    except Exception:  # noqa: BLE001
        return None


def status() -> dict:
    """Cheap (no MCP round trip): installed? MCP port answering? Used by system.health."""
    mcp, cdp = ports()
    b = binary()
    # both ports: the MCP server (browseros-claw-server) can outlive a browser window that crashed or was closed,
    # and then 9210 still answers while nothing can be driven
    return {"installed": bool(b), "running": _port_open(mcp) and _port_open(cdp), "mcp_port": mcp, "cdp_port": cdp,
            "version": version() if b else None}


def _up() -> bool:
    mcp, cdp = ports()
    return _port_open(mcp) and _port_open(cdp)


class BrowserOS:
    def __init__(self, env: Optional[dict] = None):
        self.env = env or dict(os.environ)
        self.conns: dict[str, dict] = {}       # task key -> {"sid", "used", "named"}
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()
        self._fronted: dict[str, tuple] = {}     # task key -> (page, active window title, time) of the last front

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{ports()[0]}/mcp"

    # ------------------------------------------------------------------ process
    async def ensure_running(self, wait_s: float = 30) -> bool:
        if _up():
            return True
        b = binary()
        if not b:
            raise RuntimeError("BrowserOS is not installed on this computer (deploy/vm/browseros-setup.sh)")
        async with self._start_lock:
            if _up():
                return True
            env = dict(self.env)
            env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
            if not _running():
                # an MCP server left over from a browser that went away holds 9210: clear it so the new one binds
                kill = await asyncio.create_subprocess_shell("pkill -f browseros-claw-server >/dev/null 2>&1; sleep 1")
                await kill.wait()
                proc = await asyncio.create_subprocess_shell(
                    f"nohup {b} --no-first-run --no-default-browser-check >/dev/null 2>&1 &", env=env)
                await proc.wait()
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait_s:
                await asyncio.sleep(0.5)
                if _up():
                    await asyncio.sleep(1.0)   # the MCP server answers a moment before its first tool call works
                    return True
        raise RuntimeError(f"BrowserOS started but its MCP server did not come up on port {ports()[0]} within {wait_s:.0f}s")

    # ------------------------------------------------------------------ MCP plumbing
    async def _post(self, body: dict, sid: Optional[str]) -> tuple[Optional[str], Optional[dict], int]:
        h = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if sid:
            h["mcp-session-id"] = sid
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as c:
            r = await c.post(self.url, content=json.dumps(body), headers=h)
        new_sid = r.headers.get("mcp-session-id") or sid
        text = r.text
        if r.status_code >= 400:
            return new_sid, {"_http": r.status_code, "_body": text[:300]}, r.status_code
        if not text.strip():
            return new_sid, None, r.status_code
        if "data:" in text[:300] or (r.headers.get("content-type") or "").startswith("text/event-stream"):
            msgs = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        msgs.append(json.loads(line[5:].strip()))
                    except ValueError:
                        pass
            want = body.get("id")
            msg = next((m for m in msgs if m.get("id") == want), msgs[-1] if msgs else None)
            return new_sid, msg, r.status_code
        return new_sid, json.loads(text), r.status_code

    async def _connect(self) -> str:
        sid, r, code = await self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "aiw", "version": "1"}}}, None)
        if code >= 400 or not sid:
            raise RuntimeError(f"BrowserOS MCP initialize failed: {r}")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        return sid

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, c in self.conns.items() if now - c["used"] > IDLE_S]:
            self.conns.pop(k, None)
            self._locks.pop(k, None)

    async def call(self, key: str, tool: str, args: Optional[dict] = None, title: Optional[str] = None,
                   category: Optional[str] = None, front: Optional[bool] = None) -> dict:
        if tool not in ALLOWED and tool not in ("front", "page_shot"):
            raise PermissionError(f"BrowserOS tool '{tool}' is not available through AIW")
        await self.ensure_running()
        self._gc()
        key = key or "default"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:   # one task's calls in order: refs from one snapshot belong to the next act
            if tool == "front":
                return await self._front(key, (args or {}).get("page"))
            if tool == "page_shot":
                return await self._page_shot(key, (args or {}).get("page"))
            clean = _clean_args(tool, args or {})
            out = await self._call(key, tool, clean, title, category)
            page = front_page(tool, clean, out) if self._autofront(front) else None
            if page is not None:
                try:
                    f = await asyncio.wait_for(self._front(key, page, quick=True), 25)
                    out["front"] = {k: f.get(k) for k in ("verified", "how", "window", "page") if k in f}
                except Exception as e:  # noqa: BLE001 - showing the page is a courtesy; the call itself worked
                    out["front"] = {"verified": False, "error": f"{type(e).__name__}: {e}"[:200]}
            return out

    def _autofront(self, front: Optional[bool]) -> bool:
        if front is not None:
            return bool(front)
        return (self.env.get("VM_AGENT_BOS_AUTOFRONT") or os.environ.get("VM_AGENT_BOS_AUTOFRONT") or "1") != "0"

    async def _call(self, key: str, tool: str, args: dict, title: Optional[str], category: Optional[str]) -> dict:
        for attempt in range(2):
            c = self.conns.get(key)
            if not c:
                c = self.conns[key] = {"sid": await self._connect(), "used": time.time(), "named": False, "n": 10}
                if title:   # the session's name in BrowserOS (its cockpit and replay list it by this)
                    try:
                        await self._rpc(c, "name_session", {"name": _label(title), "category": category or "other",
                                                            "summary": title[:200]})
                    except Exception:  # noqa: BLE001 - only a label
                        pass
            c["used"] = time.time()
            r = await self._rpc(c, tool, args)
            if r.get("_http") in (400, 404) or "session" in str(r.get("error", {}).get("message", "")).lower() and r.get("error"):
                self.conns.pop(key, None)   # BrowserOS restarted or dropped the session: reconnect once
                if attempt == 0:
                    continue
            return _result(r)
        return {"ok": False, "text": "BrowserOS session could not be re-established", "is_error": True}

    async def _rpc(self, c: dict, tool: str, args: dict) -> dict:
        c["n"] += 1
        _, r, code = await self._post({"jsonrpc": "2.0", "id": c["n"], "method": "tools/call",
                                       "params": {"name": tool, "arguments": args}}, c["sid"])
        return r or {}

    async def _target(self, key: str, page) -> tuple[Optional[dict], str]:
        """(DevTools target of a task's page, error text). The page number is BrowserOS's, from its tab list."""
        listing = (await self._call(key, "tabs", {"action": "list"}, None, None)).get("text", "")
        url, group = None, None
        for m in re.finditer(r"^\[(\d+)\]\s+(\S+)(.*)$", listing, re.M):
            if page is None or int(m.group(1)) == int(page):
                url = m.group(2)
                g = re.search(r"\[group ([0-9A-Za-z_-]+)\]", m.group(3) or "")
                group = g.group(1) if g else None
                if page is not None:
                    break
        if not url:
            return None, f"page {page} not found in:\n{listing[:1500]}"
        _, cdp = ports()
        async with httpx.AsyncClient(timeout=5, trust_env=False) as cl:
            tabs = (await cl.get(f"http://127.0.0.1:{cdp}/json/list")).json()
        t = next((t for t in tabs if t.get("type") == "page" and t.get("url") == url), None)
        if not t:
            t = next((t for t in tabs if t.get("type") == "page" and url.rstrip("/") in (t.get("url") or "")), None)
        if not t:
            return None, f"no DevTools target for {url}"
        t["_cdp"] = cdp
        t["_group"] = group
        return t, ""

    async def _sh(self, cmd: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**self.env, "DISPLAY": self.env.get("DISPLAY") or ":0"})
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        return (out or b"").decode(errors="ignore")

    async def _active_page(self, key: str) -> Optional[int]:
        """BrowserOS's own answer to which page is the selected tab ('Active page: [3] ...')."""
        txt = (await self._call(key, "tabs", {"action": "active"}, None, None)).get("text", "")
        m = re.search(r"Active page:\s*\[(\d+)\]", txt)
        return int(m.group(1)) if m else None

    async def _front(self, key: str, page, quick: bool = False) -> dict:
        """Bring a task's page to the front: its tab selected in its window, and THAT window on top of the desktop.

        BrowserOS opens every agent page in the background, in a tab group of the task that it may show collapsed,
        and it ignores DevTools activation of those tabs: /json/activate, Target.activateTarget and Page.bringToFront
        all answer OK and the window keeps showing the BrowserOS neo home tab (checked live on 云电脑 2, 2026-10-28,
        after bos_tabs opened github.com/liangdabiao/whiteboard-teacher-video). So: expand the task's group, then
        chrome_cdp.front (DevTools activation + raise the window, and when the window's title still is not the page's,
        Ctrl+PageDown in that window until it is). Checked by the window title and BrowserOS's own active page.
        quick: skip everything when this page was put in front last time and the same window is still active."""
        from . import chrome_cdp
        if quick:
            last = self._fronted.get(key)
            if last and last[0] == page and time.time() - last[2] < FRONT_SKIP_S:
                now = (await self._sh("xdotool getactivewindow getwindowname 2>/dev/null")).strip()
                if now and now == last[1]:
                    return {"ok": True, "is_error": False, "verified": True, "how": "already", "window": now,
                            "page": page, "text": f"page {page} is already in front"}
        t, err = await self._target(key, page)
        if not t:
            return {"ok": False, "is_error": True, "text": err}
        if t.get("_group"):   # a tab in a collapsed group cannot be selected from the keyboard (and is invisible)
            try:
                await self._call(key, "tab_groups", {"action": "update", "groupId": t["_group"], "collapsed": False},
                                 None, None)
            except Exception:  # noqa: BLE001
                pass
        env = {**self.env, "DISPLAY": self.env.get("DISPLAY") or os.environ.get("DISPLAY") or ":0"}
        f = await asyncio.to_thread(chrome_cdp.front, t["_cdp"], t["id"], t.get("url") or "", env)
        verified, active = bool(f.get("verified")), f.get("window") or ""
        if page is not None:
            try:
                ap = await self._active_page(key)
                if ap is not None and ap != int(page):
                    verified = False
            except Exception:  # noqa: BLE001
                pass
        if verified:
            self._fronted[key] = (page, active, time.time())
        else:
            self._fronted.pop(key, None)
        url, title = t.get("url") or "", (t.get("title") or "").strip()
        if verified:
            text = (f"page {page} ({url}) is now the visible tab and its window is on top of the desktop; "
                    "screenshot / click by coordinates now act on it.")
        else:
            text = (f"page {page} ({url}) could not be brought to the front; the desktop shows another "
                    f"window ({active or 'unknown'}). Desktop screenshots will NOT show this page; the agent uses a "
                    "picture of the page itself instead.")
        return {"ok": True, "is_error": False, "text": text, "verified": verified, "window": active, "url": url,
                "title": title, "how": f.get("how"), "page": page}

    async def _page_shot(self, key: str, page) -> dict:
        """A picture of the page itself over DevTools (Page.captureScreenshot), whatever window is on the desktop.
        Its pixels are viewport CSS pixels, the x,y that bos_act click_at / type_at take."""
        t, err = await self._target(key, page)
        if not t or not t.get("webSocketDebuggerUrl"):
            return {"ok": False, "is_error": True, "text": err or "page has no DevTools socket"}
        import websockets
        ws_url = t["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, max_size=None, open_timeout=5) as ws:
            n = 0

            async def cmd(method, params=None):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if msg.get("id") == n:
                        if "error" in msg:
                            raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                        return msg.get("result") or {}
            await cmd("Page.bringToFront")
            m = await cmd("Runtime.evaluate", {"expression": "[innerWidth, innerHeight, devicePixelRatio]",
                                               "returnByValue": True})
            w, h, dpr = ((m.get("result") or {}).get("value") or [0, 0, 1])
            params = {"format": "jpeg", "quality": 75}
            if w and h and dpr and abs(float(dpr) - 1) > 0.01:
                params["clip"] = {"x": 0, "y": 0, "width": w, "height": h, "scale": 1 / float(dpr)}
            shot = await cmd("Page.captureScreenshot", params)
        return {"ok": True, "is_error": False, "text": f"picture of page {page} ({t.get('url')}), {w}x{h} viewport px",
                "images": [{"mime": "image/jpeg", "data": shot.get("data") or ""}], "width": w, "height": h,
                "url": t.get("url"), "title": t.get("title")}

    async def close(self, key: str) -> dict:
        c = self.conns.pop(key, None)
        self._locks.pop(key, None)
        if c:
            try:
                async with httpx.AsyncClient(timeout=5) as cl:
                    await cl.delete(self.url, headers={"mcp-session-id": c["sid"]})
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "closed": bool(c)}


def _running() -> bool:
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().split(b"\0")[0].decode(errors="ignore")
            except OSError:
                continue
            if cmd.endswith(("/browserclaw", "/browseros")) and "crashpad" not in cmd:
                return True
    except OSError:
        pass
    return False


def front_page(tool: str, args: dict, out: dict) -> Optional[int]:
    """The page a successful call opened or worked on, when it is one to show; None = leave the screen alone."""
    if tool not in FRONT_TOOLS or not out.get("ok") or out.get("is_error"):
        return None
    if tool == "tabs":
        act = str(args.get("action") or "")
        if act not in ("new", "open"):
            return None   # list / active / close show nothing new
        m = re.search(r"(?:opened|new) page\s*\[?(\d+)", out.get("text") or "", re.I) or \
            re.search(r"^\[(\d+)\]", out.get("text") or "", re.M)
        return int(m.group(1)) if m else None
    p = args.get("page")
    try:
        return int(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def _label(title: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split()
    return (" ".join(["aiw"] + t[:2]) or "aiw task")[:64]


def _clean_args(tool: str, a: dict) -> dict:
    a = {k: v for k, v in a.items() if v is not None and not str(k).startswith("_")}
    a.pop("session", None)   # the connection is the session
    if tool == "upload":
        for k in ("file",):
            if isinstance(a.get(k), str):
                a[k] = os.path.expanduser(a[k])
        if isinstance(a.get("files"), list):
            a["files"] = [os.path.expanduser(str(x)) for x in a["files"]]
    return a


def _result(r: dict) -> dict:
    if r.get("_http"):
        return {"ok": False, "is_error": True, "text": f"BrowserOS MCP HTTP {r['_http']}: {r.get('_body')}"}
    if r.get("error"):
        return {"ok": False, "is_error": True, "text": f"BrowserOS error: {r['error'].get('message') or r['error']}"}
    res = r.get("result") or {}
    texts, images = [], []
    for c in res.get("content") or []:
        if c.get("type") == "text":
            texts.append(c.get("text") or "")
        elif c.get("type") == "image" and c.get("data"):
            images.append({"mime": c.get("mimeType") or "image/jpeg", "data": c["data"]})
    text = _TIP.sub("", "\n".join(texts)).replace("\n--- Additional context (auto-included) ---", "").strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n... [{len(text) - MAX_TEXT} chars cut]"
    out = {"ok": not res.get("isError"), "is_error": bool(res.get("isError")), "text": text}
    if images:
        out["images"] = images[:2]
    return out
