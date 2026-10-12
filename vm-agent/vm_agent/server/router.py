"""Dispatch one protocol Command to the matching VM capability."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from aiw_protocols import ActionType as A, Command, CommandResult, Observation

from ..browser.playwright import Browser
from ..computer import keyboard, mouse
from ..computer.desktop import DesktopBackend
from ..computer.screenshot import screenshot
from ..files import read as fread, transfer, write as fwrite
from ..heartbeat.heartbeat import health
from ..shell import executor

Handler = Callable[[dict], Awaitable[dict]]


class Router:
    def __init__(self, backend: DesktopBackend, screenshot_max_width: int | None = None):
        self.b = backend
        self.browser = Browser(backend)
        self.max_w = screenshot_max_width
        # GUI input must be serialized: two concurrent clicks would interleave.
        self._input_lock = asyncio.Lock()
        b = backend
        self.handlers: dict[str, Handler] = {
            A.SCREENSHOT: lambda p: screenshot(b, p.get("max_width", self.max_w)),
            A.CLICK: lambda p: mouse.click(b, p["x"], p["y"], p.get("button", "left")),
            A.DOUBLE_CLICK: lambda p: mouse.double_click(b, p["x"], p["y"]),
            A.RIGHT_CLICK: lambda p: mouse.right_click(b, p["x"], p["y"]),
            A.MOVE: lambda p: mouse.move(b, p["x"], p["y"]),
            A.DRAG: lambda p: mouse.drag(b, p["x1"], p["y1"], p["x2"], p["y2"]),
            A.SCROLL: lambda p: mouse.scroll(b, p["dx"], p["dy"], p.get("x"), p.get("y")),
            A.TYPE: lambda p: keyboard.type_text(b, p["text"]),
            A.KEYPRESS: lambda p: keyboard.keypress(b, p["key"]),
            A.HOTKEY: lambda p: keyboard.hotkey(b, p["keys"]),
            A.OPEN_APP: lambda p: b.open_app(p["name"]),
            A.CLOSE_APP: lambda p: b.close_app(p["name"]),
            A.CURSOR: self._cursor,
            A.SCREEN_SIZE: self._size,
            A.SHELL_EXEC: lambda p: executor.run(p["command"], p.get("cwd"), p.get("timeout", 60)),
            A.BROWSER_OPEN: lambda p: self.browser.open(p.get("url")),
            A.BROWSER_NAVIGATE: lambda p: self.browser.navigate(p["url"]),
            A.BROWSER_BACK: lambda p: self.browser.back(),
            A.BROWSER_FORWARD: lambda p: self.browser.forward(),
            A.FILES_LIST: lambda p: _sync(fread.list_dir, p["path"]),
            A.FILES_READ: lambda p: _sync(fread.read, p["path"], p.get("binary", False)),
            A.FILES_WRITE: lambda p: _sync(fwrite.write, p["path"], p["content"], p.get("append", False)),
            A.FILES_DOWNLOAD: lambda p: transfer.download(p["url"], p.get("path")),
            A.FILES_UPLOAD: lambda p: _sync(transfer.upload, p["path"], p["content_b64"]),
            A.HEALTH: lambda p: _sync(health, b.name),
        }
        self.handlers = {k.value if hasattr(k, "value") else k: v for k, v in self.handlers.items()}

    async def _cursor(self, _p):
        x, y = await self.b.cursor()
        return {"x": x, "y": y}

    async def _size(self, _p):
        w, h = await self.b.screen_size()
        return {"width": w, "height": h}

    async def handle(self, cmd: Command) -> CommandResult:
        t0 = time.monotonic()
        err = cmd.validate_payload()
        if err:
            return CommandResult(id=cmd.id, success=False, error=err)
        handler = self.handlers[cmd.type]
        gui = cmd.type.startswith("computer.") or cmd.type.startswith("browser.")
        try:
            if gui and cmd.type not in (A.SCREENSHOT.value, A.CURSOR.value, A.SCREEN_SIZE.value):
                async with self._input_lock:
                    result = await asyncio.wait_for(handler(cmd.payload), cmd.timeout)
            else:
                result = await asyncio.wait_for(handler(cmd.payload), cmd.timeout)
            obs = None
            if cmd.observe:
                await asyncio.sleep(float(cmd.payload.get("settle", 0.4)) if gui else 0)
                shot = await screenshot(self.b, self.max_w)
                x, y = await self.b.cursor()
                obs = Observation(screenshot=shot["screenshot"], width=shot["width"],
                                  height=shot["height"], cursor=(x, y))
            elif cmd.type == A.SCREENSHOT.value:
                obs = Observation(screenshot=result.pop("screenshot"), width=result["width"],
                                  height=result["height"])
            ok = not (cmd.type == A.SHELL_EXEC.value and result.get("exit_code") not in (0, None))
            return CommandResult(id=cmd.id, success=ok, result=result, observation=obs,
                                 duration_ms=int((time.monotonic() - t0) * 1000))
        except asyncio.TimeoutError:
            return CommandResult(id=cmd.id, success=False, error=f"timeout after {cmd.timeout}s")
        except Exception as e:  # noqa: BLE001 - every failure must return to caller
            return CommandResult(id=cmd.id, success=False, error=f"{type(e).__name__}: {e}",
                                 duration_ms=int((time.monotonic() - t0) * 1000))


async def _sync(fn, *args: Any) -> dict:
    return await asyncio.to_thread(fn, *args)
