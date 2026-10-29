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
