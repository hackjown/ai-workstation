"""The user's takeover input, straight to the VM (3.8.4; 3.8.7 delivery fix).

Before 3.8.4 the touch layer (apps/web/takeover.js) faked mouse and key DOM events inside the noVNC page. That could
not reach a viewer on another origin (InstaBox 云电脑: every key and touch of the layer was dropped) and noVNC grabbed
focus on each tap, so the phone keyboard closed. Now the phone sends small ops (aiw_protocols/input_ops.py) here:

  WS   /ws/input/{mid}?token=…    {"op": "hello", "width", "height"} first, then the client streams ops (JSON, one op
                                  or {"ops": [...]}) and gets {"op": "error", "error"} back only when something failed
                                  (the same error at most once every ERR_QUIET seconds).
  POST /api/machines/{mid}/input  {"ops": [...]} -> {"ok", "applied"} (tests, and the client's fallback when a socket
                                  cannot be opened)

A pump per connection keeps order, collapses back-to-back mouse moves into the last one and hands whatever is queued
to the VM as one command per batch. The socket never waits for the VM (fire and forget, ordered by the pump).

3.8.7, how a batch reaches the VM ("mode"), decided once per connection by a `computer.input` probe with no ops:
  native   a vm-agent that answers v >= 2: `computer.input` (absolute xdotool, no --sync)
  script   anything older (no computer.input before 3.8.4, or 3.8.4-3.8.6 whose X11 move used `mousemove --sync`):
           ONE shell.exec running an xdotool chain (input_ops.xdotool_script), so update-agent.sh is not required.
Root cause of 「电脑没收到操作：RuntimeError: timeout after 10.0s」 on 云电脑 1 (a 3.8.3 agent): the old path sent
computer.move then computer.click; click = `mousemove --sync` to the pixel the pointer was already on, which
xdotool 3.20160805 waits 15 s on; the agent gave up at 10 s. Moves worked, every click and double click failed.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Optional

from aiw_protocols.input_ops import budget, clean, coalesce, xdotool_script
from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("aiw.input")
BATCH = 40
ERR_QUIET = 15.0      # seconds the same error stays quiet on the phone (no toast spam)


class InputPump:
    def __init__(self, computer, legacy: Optional[bool] = None):
        self.c = computer
        # "native" | "script" | None (not known yet: the probe or the first batch tells)
        self.mode: Optional[str] = None if legacy is None else ("script" if legacy else "native")
        self.size: Optional[tuple[int, int]] = None
        self.q: collections.deque = collections.deque()
        self.ev = asyncio.Event()
        self.errors: list[str] = []
        self.applied = 0
        self.held: Optional[str] = None    # a mouse button the phone holds down (drag), released if it goes away
        self.at = (0, 0)
        self.busy = False
        self._last_err = ("", 0.0)

    @property
    def legacy(self) -> Optional[bool]:
        return None if self.mode is None else self.mode == "script"

    async def screen(self) -> Optional[tuple[int, int]]:
        """The screen size, and (first call) which way batches go: a `computer.input` with no ops is harmless on
        every agent and tells the version (v >= 2 since 3.8.7)."""
        if self.size and self.mode:
            return self.size
        if self.mode is None:
            try:
                r = await self.c.execute("computer.input", {"ops": []}, timeout=8)
                if r.success:
                    res = r.result or {}
                    self.mode = "native" if int(res.get("v") or 0) >= 2 else "script"
                    if res.get("width"):
                        self.size = (int(res["width"]), int(res["height"]))
                elif "unknown action type" in (r.error or ""):
                    self.mode = "script"
            except Exception as e:  # noqa: BLE001
                log.info("input probe: %s", e)
        if not self.size:
            try:
                r = await self.c.execute("computer.screen_size", {}, timeout=8)
                if r.success and r.result.get("width"):
                    self.size = (int(r.result["width"]), int(r.result["height"]))
            except Exception as e:  # noqa: BLE001
                log.info("screen size: %s", e)
        return self.size

    def push(self, raw) -> int:
        """Queue ops (one or a list); returns how many were accepted."""
        n = 0
        for op in raw if isinstance(raw, list) else [raw]:
            o = clean(op, self.size)
            if not o:
                continue
            if "x" in o:
                self.at = (o["x"], o["y"])
            if o["t"] == "down":
                self.held = o["button"]
            elif o["t"] == "up":
                self.held = None
            if o["t"] == "move" and self.q and self.q[-1]["t"] == "move":
                self.q[-1] = o
            else:
                self.q.append(o)
            n += 1
        if n:
            self.ev.set()
        return n

    async def drain(self) -> None:
        self.busy = True
        first: Optional[BaseException] = None
        try:
            while self.q:   # a failed batch does not strand the ops queued behind it
                batch = []
                while self.q and len(batch) < BATCH:
                    batch.append(self.q.popleft())
                try:
                    await self.apply(coalesce(batch))
                except Exception as e:  # noqa: BLE001
                    first = first or e
            if first:
                raise first
        finally:
            self.busy = False
            self.ev.clear()

    async def idle(self) -> None:
        """Until the worker has sent everything queued."""
        while self.q or self.busy:
            await asyncio.sleep(0.02)

    async def run(self, on_error=None) -> None:
        while True:
            await self.ev.wait()
            try:
                await self.drain()
            except Exception as e:  # noqa: BLE001 - one bad batch must not end the takeover
                msg = f"{type(e).__name__}: {e}"[:300]
                self.errors.append(msg)
                last, at = self._last_err
                now = time.monotonic()
                if on_error and (msg != last or now - at > ERR_QUIET):
                    self._last_err = (msg, now)
                    await on_error(msg)

    async def _call(self, action: str, payload: dict, timeout: float):
        """One command; a lost connection (nothing ran) is tried once more, a slow command is not (clicks are not
        idempotent)."""
        r = await self.c.execute(action, payload, timeout=timeout)
        err = r.error or ""
        if not r.success and ("ConnectionError" in err or "connection lost" in err or "ConnectionClosed" in err):
            await asyncio.sleep(0.5)
            r = await self.c.execute(action, payload, timeout=timeout)
        return r

    async def apply(self, ops: list[dict]) -> None:
        if not ops:
            return
        t = budget(ops)
        if self.mode != "script":
            r = await self._call("computer.input", {"ops": ops}, timeout=t)
            if r.success:
                if self.mode is None:   # no probe answer: this batch ran on whatever agent it is; pick for the rest
                    self.mode = "native" if int((r.result or {}).get("v") or 0) >= 2 else "script"
                self.applied += int((r.result or {}).get("applied") or 0)
                return
            if "unknown action type" not in (r.error or ""):
                raise RuntimeError(r.error or "input failed")
            self.mode = "script"   # a vm-agent before 3.8.4
        cmd = xdotool_script(ops)
        if not cmd:
            return
        r = await self._call("shell.exec", {"command": cmd, "timeout": t}, timeout=t + 4)
        if not r.success:
            res = r.result or {}
            raise RuntimeError(r.error or (res.get("stderr") or "").strip()[:200] or f"xdotool exit {res.get('exit_code')}")
        self.applied += len(ops)


def mount(app, store, sessions, auth, ws_ok) -> None:
    from fastapi import Depends, HTTPException

    def computer(mid: str):
        if not store.machine(mid):
            return None
        try:
            return sessions.computer(mid)
        except KeyError:
            return None

    @app.post("/api/machines/{mid}/input", dependencies=[Depends(auth)])
    async def machine_input(mid: str, body: dict):
        c = computer(mid)
        if not c:
            raise HTTPException(404, "no such computer")
        ops = (body or {}).get("ops")
        if not isinstance(ops, list) or not ops:
            raise HTTPException(400, "ops must be a non-empty list")
        p = InputPump(c)
        await p.screen()
        n = p.push(ops[:200])
        try:
            await p.drain()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"input failed: {e}"[:300])
        return {"ok": True, "accepted": n, "applied": p.applied, "legacy": bool(p.legacy), "mode": p.mode,
                "width": (p.size or (0, 0))[0], "height": (p.size or (0, 0))[1]}

    @app.websocket("/ws/input/{mid}")
    async def ws_input(ws: WebSocket, mid: str):
        c = computer(mid)
        if not ws_ok(ws) or not c:
            await ws.close(code=4401 if c else 4404)
            return
        await ws.accept()
        p = InputPump(c)
        size = await p.screen()
        await ws.send_json({"op": "hello", "width": size[0] if size else None, "height": size[1] if size else None})

        async def err(msg: str):
            try:
                await ws.send_json({"op": "error", "error": msg})
            except Exception:  # noqa: BLE001
                pass

        worker = asyncio.create_task(p.run(err))
        try:
            while True:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("op") == "ping":
                    await ws.send_json({"op": "pong", "t": msg.get("t")})
                elif msg.get("op") == "size":
                    p.size = None
                    s = await p.screen()
                    await ws.send_json({"op": "hello", "width": s[0] if s else None, "height": s[1] if s else None})
                else:
                    p.push(msg["ops"] if isinstance(msg.get("ops"), list) else msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as e:  # noqa: BLE001
            log.info("input socket %s: %s", mid, e)
        finally:
            # never leave a mouse button held down on the VM when the phone goes away mid-drag
            try:
                if p.held:
                    p.push({"t": "up", "x": p.at[0], "y": p.at[1], "button": p.held})
                await asyncio.wait_for(p.idle(), 5)
            except Exception:  # noqa: BLE001
                pass
            worker.cancel()
