"""computer.input (3.8.4): the user's own takeover input, a batch of ops run in order on the VM display.

See aiw_protocols/input_ops.py for the op language. Everything is absolute xdotool (X11Backend): mousemove (no --sync since 3.8.7),
click / mousedown / mouseup, wheel buttons 4-7, `type` (non-ASCII runs typed slower so Chinese is not dropped) and
`key`. The router holds the input lock for the whole batch, so the agent's own clicks never interleave with it.
"""
from __future__ import annotations

from aiw_protocols.input_ops import BUTTONS, clean, coalesce

from .desktop import DesktopBackend


async def run(b: DesktopBackend, ops: list) -> dict:
    size = await b.screen_size()
    todo = coalesce([o for o in (clean(op, size) for op in (ops or [])[:200]) if o])
    done = 0
    for op in todo:
        t = op["t"]
        if t == "move":
            await b.move(op["x"], op["y"])
        elif t == "click":
            await b.click(op["x"], op["y"], button=BUTTONS[op["button"]], repeat=op["count"])
        elif t == "down":
            await b.mouse_down(op["x"], op["y"], BUTTONS[op["button"]])
        elif t == "up":
            await b.mouse_up(op["x"], op["y"], BUTTONS[op["button"]])
        elif t == "scroll":
            await b.move(op["x"], op["y"])
            await b.scroll(op["dx"], op["dy"])
        elif t == "text":
            await b.type(op["text"])
        elif t == "key":
            await b.key(op["key"])
        done += 1
    # v 2 (3.8.7): no `mousemove --sync` underneath; the API sends batches here only when it sees v >= 2
    return {"applied": done, "width": size[0], "height": size[1], "v": 2}
