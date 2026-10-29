"""The user's takeover input, straight to the VM (3.8.4).

Before 3.8.4 the touch layer (apps/web/takeover.js) faked mouse and key DOM events inside the noVNC page. That could
not reach a viewer on another origin (InstaBox 云电脑: every key and touch of the layer was dropped) and noVNC grabbed
focus on each tap, so the phone keyboard closed. Now the phone sends small ops (aiw_protocols/input_ops.py) here:

  WS   /ws/input/{mid}?token=…    {"op": "hello", "width", "height"} first, then the client streams ops (JSON, one op
                                  or {"ops": [...]}) and gets {"op": "error", "error"} back only when something failed.
  POST /api/machines/{mid}/input  {"ops": [...]} -> {"ok", "applied"} (tests, and the client's fallback when a socket
                                  cannot be opened)

A pump per connection keeps order, collapses back-to-back mouse moves into the last one (the phone already sends at
most ~60 a second) and hands whatever is queued to the vm-agent as one `computer.input` batch: absolute xdotool on the
VM display, so it works the same for local and cloud computers, portrait and landscape.

A vm-agent from before 3.8.4 answers "unknown action type": the pump then runs the same ops through the older
actions (computer.move / click / double_click / scroll / type / hotkey) and shell.exec xdotool for button down / up and
for non-ASCII text, so takeover works before update-agent.sh too (just a little slower).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import shlex
from typing import Optional

from aiw_protocols.input_ops import BUTTONS, clean, coalesce
from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("aiw.input")
BATCH = 40


class InputPump:
    def __init__(self, computer, legacy: Optional[bool] = None):
        self.c = computer
        self.legacy = legacy           # None = not known yet (first batch tells)
        self.size: Optional[tuple[int, int]] = None
        self.q: collections.deque = collections.deque()
        self.ev = asyncio.Event()
        self.errors: list[str] = []
        self.applied = 0
        self.held: Optional[str] = None    # a mouse button the phone holds down (drag), released if it goes away
        self.at = (0, 0)
        self.busy = False

    async def screen(self) -> Optional[tuple[int, int]]:
        if self.size:
            return self.size
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
        try:
            while self.q:
                batch = []
                while self.q and len(batch) < BATCH:
                    batch.append(self.q.popleft())
                await self.apply(coalesce(batch))
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
                if on_error:
                    await on_error(msg)

    async def apply(self, ops: list[dict]) -> None:
        if not ops:
            return
        if self.legacy is not True:
            r = await self.c.execute("computer.input", {"ops": ops}, timeout=20 + 2 * len(ops))
            if r.success:
                self.legacy = False
                self.applied += int(r.result.get("applied") or 0)
                return
            if "unknown action type" not in (r.error or ""):
                raise RuntimeError(r.error or "input failed")
            self.legacy = True   # a vm-agent before 3.8.4
        for op in ops:
            await self._one(op)
            self.applied += 1

    async def _one(self, op: dict) -> None:
        t = op["t"]
        if t == "move":
            r = await self.c.execute("computer.move", {"x": op["x"], "y": op["y"]}, timeout=10)
        elif t == "click":
            if op["count"] == 2 and op["button"] == "left":
                r = await self.c.execute("computer.double_click", {"x": op["x"], "y": op["y"]}, timeout=10)
            else:
                r = await self.c.execute("computer.click", {"x": op["x"], "y": op["y"], "button": op["button"]}, timeout=10)
        elif t in ("down", "up"):
            r = await self.c.execute("shell.exec", {"command": f"xdotool mousemove --sync {op['x']} {op['y']} "
                                                               f"mouse{t} {BUTTONS[op['button']]}", "timeout": 8}, timeout=12)
        elif t == "scroll":
            r = await self.c.execute("computer.scroll", {"dx": op["dx"], "dy": op["dy"], "x": op["x"], "y": op["y"]}, timeout=10)
        elif t == "text":
            if all(ord(ch) < 127 for ch in op["text"]):
                r = await self.c.execute("computer.type", {"text": op["text"]}, timeout=30)
            else:   # the old agent types at --delay 12, which drops Chinese characters
                r = await self.c.execute("shell.exec", {"command": "xdotool type --delay 70 -- " + shlex.quote(op["text"]),
                                                        "timeout": 60}, timeout=65)
        else:
            r = await self.c.execute("computer.hotkey", {"keys": op["key"]}, timeout=10)
        if not r.success:
            raise RuntimeError(r.error or f"{t} failed")


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
        return {"ok": True, "accepted": n, "applied": p.applied, "legacy": bool(p.legacy),
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
