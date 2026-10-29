"""The user's takeover input (3.8.4): one small op language shared by the touch layer, the API and the vm-agent.

The phone (apps/web/takeover.js, the iOS keyboard) sends ops over /ws/input/<machine>; the API cleans them here,
coalesces mouse moves and hands batches to the vm-agent as one `computer.input` command, which runs them in order
with absolute xdotool calls on the VM display (vm_agent/computer/remote_input.py). Coordinates are framebuffer pixels.

  {"t": "move",   "x": 812, "y": 403}
  {"t": "click",  "x": 812, "y": 403, "button": "left|right|middle", "count": 1|2}
  {"t": "down",   "x": 812, "y": 403, "button": "left"}        # drag start (button held)
  {"t": "up",     "x": 900, "y": 410, "button": "left"}        # drag end
  {"t": "scroll", "x": 812, "y": 403, "dx": 0, "dy": 3}        # wheel notches, dy > 0 = down
  {"t": "text",   "text": "你好 abc"}                           # committed text, typed as unicode
  {"t": "key",    "key": "ctrl+c"}                             # one key or combo, xdotool names (Return, BackSpace, …)
"""
from __future__ import annotations

import re
from typing import Optional

BUTTONS = {"left": 1, "middle": 2, "right": 3}
MAX_TEXT = 2000
MAX_SCROLL = 30
_KEY = re.compile(r"^[A-Za-z0-9_]{1,24}(\+[A-Za-z0-9_]{1,24}){0,4}$")


def _xy(op: dict, size: Optional[tuple[int, int]]) -> Optional[tuple[int, int]]:
    try:
        x, y = int(round(float(op["x"]))), int(round(float(op["y"])))
    except (KeyError, TypeError, ValueError):
        return None
    if size:
        w, h = size
        x, y = max(0, min(w - 1, x)), max(0, min(h - 1, y))
    return max(0, x), max(0, y)


def clean(op, size: Optional[tuple[int, int]] = None) -> Optional[dict]:
    """A safe copy of one op (coordinates clamped to the screen), or None when it is not one we run."""
    if not isinstance(op, dict):
        return None
    t = op.get("t")
    if t in ("move", "click", "down", "up", "scroll"):
        xy = _xy(op, size)
        if xy is None:
            return None
        out = {"t": t, "x": xy[0], "y": xy[1]}
        if t in ("click", "down", "up"):
            b = op.get("button") or "left"
            if b not in BUTTONS:
                return None
            out["button"] = b
        if t == "click":
            out["count"] = 2 if op.get("count") == 2 else 1
        if t == "scroll":
            try:
                dx, dy = int(op.get("dx") or 0), int(op.get("dy") or 0)
            except (TypeError, ValueError):
                return None
            dx, dy = max(-MAX_SCROLL, min(MAX_SCROLL, dx)), max(-MAX_SCROLL, min(MAX_SCROLL, dy))
            if not dx and not dy:
                return None
            out.update(dx=dx, dy=dy)
        return out
    if t == "text":
        s = op.get("text")
        if not isinstance(s, str) or not s:
            return None
        s = "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 32)[:MAX_TEXT]
        return {"t": "text", "text": s} if s else None
    if t == "key":
        k = op.get("key")
        if not isinstance(k, str) or not _KEY.match(k.replace(" ", "")):
            return None
        return {"t": "key", "key": k.replace(" ", "")}
    return None


def coalesce(ops: list[dict]) -> list[dict]:
    """Back-to-back moves collapse into the last one (the pointer only needs to end up there)."""
    out: list[dict] = []
    for op in ops:
        if op["t"] == "move" and out and out[-1]["t"] == "move":
            out[-1] = op
        else:
            out.append(op)
    return out


# ---------------------------------------------------------------- 3.8.7: one shell command per batch
# xdotool 3.20160805 (Ubuntu 24.04, Debian 12/13, the InstaBox image) has a bug in `mousemove --sync`: when the
# pointer is already on the target pixel it waits for a move that never comes, 500 x 30 ms = 15 s. Takeover always
# sends the final move and then the click at that same pixel, so every click hung (measured on 云电脑 1: the same
# `mousemove --sync 700 400` twice took 3 ms, then 15078 ms) and the vm-agent answered "timeout after 10.0s".
# Nothing here uses --sync: a chain inside ONE xdotool process (`mousemove x y click 1`) is ordered by the X server.

KEY_ALIASES = {
    "ENTER": "Return", "RETURN": "Return", "TAB": "Tab", "ESC": "Escape", "ESCAPE": "Escape",
    "BACKSPACE": "BackSpace", "DELETE": "Delete", "SPACE": "space", "UP": "Up", "DOWN": "Down",
    "LEFT": "Left", "RIGHT": "Right", "HOME": "Home", "END": "End", "PAGEUP": "Prior",
    "PAGEDOWN": "Next", "CTRL": "ctrl", "CONTROL": "ctrl", "ALT": "alt", "SHIFT": "shift",
    "SUPER": "super", "META": "super", "WIN": "super", "CMD": "super",
}
# the vm-agent's shell inherits its DISPLAY; a service started without one still finds the desktop (:1 = Xvnc, else :0)
DISPLAY_GUARD = '[ -n "$DISPLAY" ] || { [ -e /tmp/.X11-unix/X1 ] && export DISPLAY=:1 || export DISPLAY=:0; }; '


def xkey(key: str) -> str:
    return "+".join(KEY_ALIASES.get(p.upper(), p) for p in key.replace(" ", "").split("+") if p)


def text_runs(text: str) -> list[tuple[str, bool]]:
    """(run, has non-ASCII) pieces in order: Chinese / emoji runs are typed slower so the app does not drop them."""
    out: list[tuple[str, bool]] = []
    for ch in text:
        wide = ord(ch) > 126
        if out and out[-1][1] == wide:
            out[-1] = (out[-1][0] + ch, wide)
        else:
            out.append((ch, wide))
    return out


def xdotool_script(ops: list[dict]) -> str:
    """Cleaned ops -> one shell command (xdotool chains joined by &&), for vm-agents whose computer.input is missing
    or still uses `mousemove --sync`. Mouse ops chain in one process; text and keys get their own (they take the rest
    of the arguments)."""
    import shlex
    cmds: list[str] = []
    chain: list[str] = []

    def flush():
        if chain:
            cmds.append("xdotool " + " ".join(chain))
            chain.clear()
    for op in ops:
        t = op["t"]
        if t == "move":
            chain += ["mousemove", str(op["x"]), str(op["y"])]
        elif t == "click":
            chain += ["mousemove", str(op["x"]), str(op["y"]), "click", "--repeat", str(op.get("count") or 1),
                      "--delay", "80", str(BUTTONS[op["button"]])]
        elif t in ("down", "up"):
            chain += ["mousemove", str(op["x"]), str(op["y"]), "mouse" + t, str(BUTTONS[op["button"]])]
        elif t == "scroll":
            chain += ["mousemove", str(op["x"]), str(op["y"])]
            if op.get("dy"):
                chain += ["click", "--repeat", str(abs(op["dy"])), "5" if op["dy"] > 0 else "4"]
            if op.get("dx"):
                chain += ["click", "--repeat", str(abs(op["dx"])), "7" if op["dx"] > 0 else "6"]
        elif t == "text":
            flush()
            for run, wide in text_runs(op["text"]):
                cmds.append(f"xdotool type --delay {70 if wide else 12} -- {shlex.quote(run)}")
        elif t == "key":
            flush()
            cmds.append("xdotool key -- " + shlex.quote(xkey(op["key"])))
    flush()
    return DISPLAY_GUARD + " && ".join(cmds) if cmds else ""


def budget(ops: list[dict]) -> float:
    """Seconds one batch may take on the VM: a little per op, more for typed text (70 ms a Chinese character)."""
    s = 6.0 + 0.25 * len(ops)
    for op in ops:
        if op["t"] == "text":
            s += sum(len(r) * (0.09 if w else 0.02) for r, w in text_runs(op["text"]))
        elif op["t"] == "click":
            s += 0.1 * (op.get("count") or 1)
    return min(120.0, s)
