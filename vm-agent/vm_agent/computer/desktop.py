"""Desktop backends.

X11Backend   - real Ubuntu/XFCE VM: xdotool for input, mss for screenshots.
MockBackend  - in-memory canvas used by tests and by dev machines without X.

Both expose the same async surface, so screenshot.py / mouse.py / keyboard.py
never care which one is running.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import shlex
import shutil
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw

# Friendly key names -> xdotool keysyms
KEY_ALIASES = {
    "ENTER": "Return", "RETURN": "Return", "TAB": "Tab", "ESC": "Escape", "ESCAPE": "Escape",
    "BACKSPACE": "BackSpace", "DELETE": "Delete", "SPACE": "space", "UP": "Up", "DOWN": "Down",
    "LEFT": "Left", "RIGHT": "Right", "HOME": "Home", "END": "End", "PAGEUP": "Prior",
    "PAGEDOWN": "Next", "CTRL": "ctrl", "CONTROL": "ctrl", "ALT": "alt", "SHIFT": "shift",
    "SUPER": "super", "META": "super", "WIN": "super", "CMD": "super",
}

# App names the agent may use -> launch commands
APP_COMMANDS = {
    "chrome": "google-chrome --no-first-run --no-default-browser-check || chromium --no-first-run || chromium-browser --no-first-run",
    "chromium": "chromium --no-first-run || chromium-browser --no-first-run",
    "browser": "google-chrome --no-first-run || chromium --no-first-run || chromium-browser --no-first-run",
    "terminal": "xfce4-terminal || x-terminal-emulator",
    "files": "thunar",
    "editor": "mousepad",
}

APP_PROCESS = {"chrome": "chrome", "chromium": "chromium", "browser": "chrom",
               "terminal": "xfce4-terminal", "files": "thunar", "editor": "mousepad"}


def normalize_key(key: str) -> str:
    parts = [p.strip() for p in key.replace(" ", "").split("+") if p.strip()]
    return "+".join(KEY_ALIASES.get(p.upper(), p) for p in parts)


def png_b64(img: Image.Image, max_width: Optional[int] = None) -> str:
    if max_width and img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


class DesktopBackend:
    name = "base"

    async def screenshot(self) -> Image.Image: ...
    async def screen_size(self) -> tuple[int, int]: ...
    async def cursor(self) -> tuple[int, int]: ...
    async def move(self, x: int, y: int) -> None: ...
    async def click(self, x: int, y: int, button: int = 1, repeat: int = 1) -> None: ...
    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...
    async def type(self, text: str) -> None: ...
    async def key(self, key: str) -> None: ...
    async def scroll(self, dx: int, dy: int) -> None: ...
    async def open_app(self, name: str) -> dict: ...
    async def close_app(self, name: str) -> dict: ...


async def _run(cmd: str, env: Optional[dict] = None, timeout: float = 15) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class X11Backend(DesktopBackend):
    """Real desktop. Requires DISPLAY, xdotool, and mss (or scrot fallback)."""

    name = "x11"

    def __init__(self, display: Optional[str] = None):
        self.display = display or os.environ.get("DISPLAY", ":0")
        self.env = {**os.environ, "DISPLAY": self.display}
        if not shutil.which("xdotool"):
            raise RuntimeError("xdotool not installed (apt install xdotool)")

    async def _xdo(self, args: str) -> str:
        code, out, err = await _run(f"xdotool {args}", env=self.env)
        if code != 0:
            raise RuntimeError(f"xdotool {args} failed: {err.strip()}")
        return out

    async def screenshot(self) -> Image.Image:
        def grab() -> Image.Image:
            import mss  # local import: only needed on a real desktop
            with mss.mss(display=self.display) as sct:
                shot = sct.grab(sct.monitors[0])
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return await asyncio.to_thread(grab)

    async def screen_size(self) -> tuple[int, int]:
        out = await self._xdo("getdisplaygeometry")
        w, h = out.split()
        return int(w), int(h)

    async def cursor(self) -> tuple[int, int]:
        out = await self._xdo("getmouselocation --shell")
        vals = dict(line.split("=") for line in out.strip().splitlines() if "=" in line)
        return int(vals["X"]), int(vals["Y"])

    async def move(self, x, y):
        await self._xdo(f"mousemove --sync {int(x)} {int(y)}")

    async def click(self, x, y, button=1, repeat=1):
        await self.move(x, y)
        await self._xdo(f"click --repeat {int(repeat)} --delay 80 {int(button)}")

    async def drag(self, x1, y1, x2, y2):
        await self.move(x1, y1)
        await self._xdo("mousedown 1")
        await asyncio.sleep(0.05)
        await self._xdo(f"mousemove --sync {int(x2)} {int(y2)}")
        await self._xdo("mouseup 1")

    async def type(self, text):
        await self._xdo(f"type --delay 12 -- {shlex.quote(text)}")

    async def key(self, key):
        await self._xdo(f"key -- {shlex.quote(normalize_key(key))}")

    async def scroll(self, dx, dy):
        # X11 buttons: 4 up, 5 down, 6 left, 7 right. dy>0 scrolls down.
        if dy:
            await self._xdo(f"click --repeat {abs(int(dy))} {5 if dy > 0 else 4}")
        if dx:
            await self._xdo(f"click --repeat {abs(int(dx))} {7 if dx > 0 else 6}")

    async def open_app(self, name):
        cmd = APP_COMMANDS.get(name.lower(), name)
        proc = await asyncio.create_subprocess_shell(
            f"nohup sh -c {shlex.quote(cmd)} >/dev/null 2>&1 &", env=self.env)
        await proc.wait()
        await asyncio.sleep(1.5)
        return {"launched": name, "command": cmd}

    async def close_app(self, name):
        pat = APP_PROCESS.get(name.lower(), name)
        code, _, _ = await _run(f"pkill -f {shlex.quote(pat)}", env=self.env)
        return {"closed": name, "matched": code == 0}


@dataclass
class MockBackend(DesktopBackend):
    """Deterministic fake desktop. Draws every action so tests can see effects."""

    width: int = 1280
    height: int = 800
    name: str = "mock"
    log: list = field(default_factory=list)
    open_apps: set = field(default_factory=set)
    typed: str = ""

    def __post_init__(self):
        self.canvas = Image.new("RGB", (self.width, self.height), (40, 44, 52))
        self.draw = ImageDraw.Draw(self.canvas)
        self.pos = (0, 0)

    async def screenshot(self):
        return self.canvas.copy()

    async def screen_size(self):
        return self.width, self.height

    async def cursor(self):
        return self.pos

    def _check(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"coordinate ({x},{y}) outside screen {self.width}x{self.height}")

    async def move(self, x, y):
        self._check(x, y)
        self.pos = (x, y)
        self.log.append(("move", x, y))

    async def click(self, x, y, button=1, repeat=1):
        await self.move(x, y)
        r = 6
        self.draw.ellipse((x - r, y - r, x + r, y + r), fill=(255, 80, 80))
        self.log.append(("click", x, y, button, repeat))

    async def drag(self, x1, y1, x2, y2):
        self._check(x1, y1); self._check(x2, y2)
        self.draw.line((x1, y1, x2, y2), fill=(80, 200, 255), width=3)
        self.pos = (x2, y2)
        self.log.append(("drag", x1, y1, x2, y2))

    async def type(self, text):
        self.typed += text
        self.draw.text((20, 20 + 14 * (len(self.log) % 50)), text[:80], fill=(230, 230, 230))
        self.log.append(("type", text))

    async def key(self, key):
        self.log.append(("key", normalize_key(key)))

    async def scroll(self, dx, dy):
        self.log.append(("scroll", dx, dy))

    async def open_app(self, name):
        self.open_apps.add(name.lower())
        self.draw.rectangle((100, 80, 1180, 760), outline=(200, 200, 200), width=2)
        self.draw.text((110, 86), f"[{name}]", fill=(255, 255, 255))
        self.log.append(("open_app", name))
        return {"launched": name, "command": "mock"}

    async def close_app(self, name):
        matched = name.lower() in self.open_apps
        self.open_apps.discard(name.lower())
        self.log.append(("close_app", name))
        return {"closed": name, "matched": matched}


def make_backend(kind: Optional[str] = None) -> DesktopBackend:
    kind = (kind or os.environ.get("VM_AGENT_BACKEND", "auto")).lower()
    if kind == "mock":
        return MockBackend()
    if kind == "x11":
        return X11Backend()
    # auto
    if os.environ.get("DISPLAY") and shutil.which("xdotool"):
        return X11Backend()
    return MockBackend()
