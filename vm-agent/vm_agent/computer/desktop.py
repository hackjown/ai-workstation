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

# ~/.local/bin/aiw-chrome (chrome_cdp.py install-launchers) always starts Chrome with the DevTools port
AIW_CHROME = '[ -x "$HOME/.local/bin/aiw-chrome" ] && exec "$HOME/.local/bin/aiw-chrome"; '
# App names the agent may use -> launch commands
APP_COMMANDS = {
    "chrome": AIW_CHROME + "google-chrome --no-first-run --no-default-browser-check --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --password-store=basic --start-maximized --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=$HOME/.config/aiw-chrome || chromium --no-first-run --no-default-browser-check --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --password-store=basic --start-maximized --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=$HOME/.config/aiw-chrome || chromium-browser --no-first-run --no-default-browser-check --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --password-store=basic --start-maximized --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=$HOME/.config/aiw-chrome",
    "chromium": "chromium --no-first-run || chromium-browser --no-first-run",
    "browser": AIW_CHROME + "google-chrome --no-first-run --no-default-browser-check --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --password-store=basic --start-maximized --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=$HOME/.config/aiw-chrome || chromium --no-first-run --no-default-browser-check --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --password-store=basic --start-maximized --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=$HOME/.config/aiw-chrome || chromium-browser --no-first-run --no-default-browser-check --hide-crash-restore-bubble --disable-session-crashed-bubble --disable-features=Translate,TranslateUI --password-store=basic --start-maximized --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir=$HOME/.config/aiw-chrome",
    # BrowserOS neo (deploy/vm/browseros-setup.sh): its MCP server on 127.0.0.1:9210 comes up with the window
    "browseros": "browserclaw --no-first-run --no-default-browser-check || browseros --no-first-run",
    "terminal": "xfce4-terminal || x-terminal-emulator",
    "files": "thunar",
    "editor": "mousepad",
}

APP_PROCESS = {"chrome": "chrome", "chromium": "chromium", "browser": "chrom", "browseros": "lib/browserclaw/browserclaw",
               "terminal": "xfce4-terminal", "files": "thunar", "editor": "mousepad"}


def _runs(text: str) -> list[tuple[str, bool]]:
    """Split text into (run, has non-ASCII) pieces, in order."""
    out: list[tuple[str, bool]] = []
    for ch in text:
        wide = ord(ch) > 126
        if out and out[-1][1] == wide:
            out[-1] = (out[-1][0] + ch, wide)
        else:
            out.append((ch, wide))
    return out


def normalize_key(key: str) -> str:
    parts = [p.strip() for p in key.replace(" ", "").split("+") if p.strip()]
    return "+".join(KEY_ALIASES.get(p.upper(), p) for p in parts)


def png_b64(img: Image.Image, max_width: Optional[int] = None) -> str:
    if max_width and img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)  # optimize=True costs seconds per 1600x900 frame
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
    async def mouse_down(self, x: int, y: int, button: int = 1) -> None: ...
    async def mouse_up(self, x: int, y: int, button: int = 1) -> None: ...
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

    _grabber: Optional[str] = None  # the first method that worked on this display, tried first next time

    async def screenshot(self) -> Image.Image:
        return await asyncio.to_thread(self._grab_any)

    def _grab_any(self) -> Image.Image:
        """mss fails on some Xvnc servers ("drawable's visual not found"), so fall back through other grabbers."""
        order = ["mss", "xwdraw", "pil", "scrot", "import", "xwd"]
        if self._grabber in order:
            order.remove(self._grabber)
            order.insert(0, self._grabber)
        errors = []
        for name in order:
            try:
                img = getattr(self, "_grab_" + name)()
                self._grabber = name
                return img.convert("RGB")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
        raise RuntimeError("all screenshot methods failed: " + " | ".join(errors))

    def _grab_mss(self) -> Image.Image:
        import mss  # local import: only needed on a real desktop
        with mss.mss(display=self.display) as sct:
            shot = sct.grab(sct.monitors[0])
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def _grab_xwdraw(self) -> Image.Image:
        """xwd + a tiny parser: ~0.3s, versus ~5s for ImageMagick `import`."""
        if not shutil.which("xwd"):
            raise RuntimeError("xwd not installed (apt install x11-apps)")
        import struct
        import subprocess
        env = dict(os.environ, DISPLAY=self.display or os.environ.get("DISPLAY", ":1"))
        raw = subprocess.run(["xwd", "-root", "-silent"], env=env, capture_output=True, timeout=15, check=True).stdout
        f = struct.unpack(">25I", raw[:100])
        header, depth, w, h, byte_order, bpp, bpl, ncolors = f[0], f[3], f[4], f[5], f[7], f[11], f[12], f[19]
        red_mask = f[14]
        data = raw[header + ncolors * 12:]
        if bpp == 32:
            mode = ("BGRX" if red_mask == 0xFF0000 else "RGBX") if byte_order == 0 else ("XRGB" if red_mask == 0xFF0000 else "XBGR")
            return Image.frombuffer("RGBX", (w, h), data, "raw", mode, bpl, 1).convert("RGB")
        if bpp == 24:
            mode = "BGR" if (byte_order == 0) == (red_mask == 0xFF0000) else "RGB"
            return Image.frombuffer("RGB", (w, h), data, "raw", mode, bpl, 1)
        if bpp == 16 and depth == 16 and byte_order == 0:
            return Image.frombuffer("RGB", (w, h), data, "raw", "BGR;16", bpl, 1)
        raise RuntimeError(f"unsupported xwd format bpp={bpp} depth={depth}")

    def _grab_pil(self) -> Image.Image:
        from PIL import ImageGrab
        return ImageGrab.grab(xdisplay=self.display)

    def _run_png(self, cmd: list[str]) -> Image.Image:
        import subprocess
        env = dict(os.environ, DISPLAY=self.display or os.environ.get("DISPLAY", ":1"))
        out = subprocess.run(cmd, env=env, capture_output=True, timeout=15, check=True).stdout
        return Image.open(io.BytesIO(out))

    def _grab_import(self) -> Image.Image:
        if not shutil.which("import"):
            raise RuntimeError("imagemagick not installed")
        return self._run_png(["import", "-silent", "-window", "root", "png:-"])

    def _grab_xwd(self) -> Image.Image:
        if not (shutil.which("xwd") and shutil.which("convert")):
            raise RuntimeError("xwd/convert not installed")
        import subprocess
        env = dict(os.environ, DISPLAY=self.display or os.environ.get("DISPLAY", ":1"))
        raw = subprocess.run(["xwd", "-root", "-silent"], env=env, capture_output=True, timeout=15, check=True).stdout
        png = subprocess.run(["convert", "xwd:-", "png:-"], input=raw, capture_output=True, timeout=15, check=True).stdout
        return Image.open(io.BytesIO(png))

    def _grab_scrot(self) -> Image.Image:
        if not shutil.which("scrot"):
            raise RuntimeError("scrot not installed")
        import tempfile
        import subprocess
        f = os.path.join(tempfile.gettempdir(), "aiw-shot.png")
        env = dict(os.environ, DISPLAY=self.display or os.environ.get("DISPLAY", ":1"))
        subprocess.run(["scrot", "-o", f], env=env, capture_output=True, timeout=15, check=True)
        img = Image.open(f)
        img.load()
        return img

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
        # 3.8.4: xdotool types a character that is not on the keyboard map (Chinese, emoji) by remapping a spare
        # keycode for it; at --delay 12 the app often reads the key after the map moved on and drops it
        # (丹防援荣图.dxf for 兴丹路消防救援站荣誉室施工图.dxf). Such runs go slower; plain ASCII stays fast.
        for run, wide in _runs(text):
            await self._xdo(f"type --delay {70 if wide else 12} -- {shlex.quote(run)}")

    async def mouse_down(self, x, y, button=1):
        await self._xdo(f"mousemove --sync {int(x)} {int(y)} mousedown {int(button)}")

    async def mouse_up(self, x, y, button=1):
        await self._xdo(f"mousemove --sync {int(x)} {int(y)} mouseup {int(button)}")

    async def key(self, key):
        await self._xdo(f"key -- {shlex.quote(normalize_key(key))}")

    async def scroll(self, dx, dy):
        # X11 buttons: 4 up, 5 down, 6 left, 7 right. dy>0 scrolls down.
        if dy:
            await self._xdo(f"click --repeat {abs(int(dy))} {5 if dy > 0 else 4}")
        if dx:
            await self._xdo(f"click --repeat {abs(int(dx))} {7 if dx > 0 else 6}")

    async def open_app(self, name):
        if name.lower() in ("chrome", "browser"):
            got = await _open_chrome(self.env)
            if got:
                return {"launched": name, **got}
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


async def _open_chrome(env: dict) -> Optional[dict]:
    """Chrome with its DevTools port, always: an instance the user opened without one is reopened with it on the same
    profile (login kept, chrome_cdp.ensure); one that already has it just gets a new window. None = use the old command."""
    from urllib.parse import urlparse
    try:
        from ..browser import chrome_cdp
        port = urlparse(os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")).port or 9222
        r = await asyncio.to_thread(chrome_cdp.ensure, port, (), True, True, "/proc", 40)
    except Exception:  # noqa: BLE001 - the plain launch command below still runs
        return None
    if not r.get("ok"):
        return None
    if r.get("how") == "found" and os.access(chrome_cdp.WRAPPER, os.X_OK):
        proc = await asyncio.create_subprocess_exec(chrome_cdp.WRAPPER, env=env, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        del proc
        await asyncio.sleep(1.5)
    return {"command": "aiw-chrome", "cdp": {k: r.get(k) for k in ("port", "how", "profile") if r.get(k)}}


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

    async def mouse_down(self, x, y, button=1):
        await self.move(x, y)
        self.log.append(("down", x, y, button))

    async def mouse_up(self, x, y, button=1):
        await self.move(x, y)
        self.log.append(("up", x, y, button))

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
