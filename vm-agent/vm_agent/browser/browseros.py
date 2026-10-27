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
    return {"installed": bool(b), "running": _port_open(mcp), "mcp_port": mcp, "cdp_port": cdp,
            "version": version() if b else None}


class BrowserOS:
    def __init__(self, env: Optional[dict] = None):
        self.env = env or dict(os.environ)
        self.conns: dict[str, dict] = {}       # task key -> {"sid", "used", "named"}
        self._locks: dict[str, asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{ports()[0]}/mcp"

    # ------------------------------------------------------------------ process
    async def ensure_running(self, wait_s: float = 30) -> bool:
        mcp, _ = ports()
        if _port_open(mcp):
            return True
        b = binary()
        if not b:
            raise RuntimeError("BrowserOS is not installed on this computer (deploy/vm/browseros-setup.sh)")
        async with self._start_lock:
            if _port_open(mcp):
                return True
            env = dict(self.env)
            env.setdefault("DISPLAY", os.environ.get("DISPLAY", ":0"))
            if not _running():
                proc = await asyncio.create_subprocess_shell(
                    f"nohup {b} --no-first-run --no-default-browser-check >/dev/null 2>&1 &", env=env)
                await proc.wait()
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait_s:
                await asyncio.sleep(0.5)
                if _port_open(ports()[0]):
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
                   category: Optional[str] = None) -> dict:
        if tool not in ALLOWED and tool != "front":
            raise PermissionError(f"BrowserOS tool '{tool}' is not available through AIW")
        await self.ensure_running()
        self._gc()
        key = key or "default"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:   # one task's calls in order: refs from one snapshot belong to the next act
            if tool == "front":
                return await self._front(key, (args or {}).get("page"))
            return await self._call(key, tool, _clean_args(tool, args or {}), title, category)

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

    async def _front(self, key: str, page) -> dict:
        """Bring a task's page to the front of the BrowserOS window, so the screenshot/xdotool fallback can see it."""
        listing = (await self._call(key, "tabs", {"action": "list"}, None, None)).get("text", "")
        url = None
        for m in re.finditer(r"^\[(\d+)\]\s+(\S+)", listing, re.M):
            if page is None or int(m.group(1)) == int(page):
                url = m.group(2)
                if page is not None:
                    break
        if not url:
            return {"ok": False, "is_error": True, "text": f"page {page} not found in:\n{listing[:1500]}"}
        _, cdp = ports()
        async with httpx.AsyncClient(timeout=5) as cl:
            tabs = (await cl.get(f"http://127.0.0.1:{cdp}/json/list")).json()
            t = next((t for t in tabs if t.get("type") == "page" and t.get("url") == url), None)
            if not t:
                t = next((t for t in tabs if t.get("type") == "page" and url.rstrip("/") in (t.get("url") or "")), None)
            if not t:
                return {"ok": False, "is_error": True, "text": f"no DevTools target for {url}"}
            await cl.get(f"http://127.0.0.1:{cdp}/json/activate/{t['id']}")
        # raise the window itself (another app may be on top)
        proc = await asyncio.create_subprocess_shell(
            "xdotool search --onlyvisible --class 'browserclaw|chromium-browser|BrowserOS' windowactivate 2>/dev/null; "
            "wmctrl -x -a browserclaw 2>/dev/null; true", env={**self.env, "DISPLAY": self.env.get("DISPLAY") or ":0"})
        await proc.wait()
        return {"ok": True, "is_error": False, "text": f"page {page} ({url}) is now the visible tab of the BrowserOS window; "
                "screenshot / click by coordinates now act on it."}

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
