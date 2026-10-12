from __future__ import annotations

from .desktop import DesktopBackend


async def click(b: DesktopBackend, x: int, y: int, button: str = "left") -> dict:
    btn = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
    await b.click(int(x), int(y), button=btn)
    return {"x": x, "y": y, "button": button}


async def double_click(b: DesktopBackend, x: int, y: int) -> dict:
    await b.click(int(x), int(y), button=1, repeat=2)
    return {"x": x, "y": y}


async def right_click(b: DesktopBackend, x: int, y: int) -> dict:
    await b.click(int(x), int(y), button=3)
    return {"x": x, "y": y}


async def move(b: DesktopBackend, x: int, y: int) -> dict:
    await b.move(int(x), int(y))
    return {"x": x, "y": y}


async def drag(b: DesktopBackend, x1: int, y1: int, x2: int, y2: int) -> dict:
    await b.drag(int(x1), int(y1), int(x2), int(y2))
    return {"from": [x1, y1], "to": [x2, y2]}


async def scroll(b: DesktopBackend, dx: int, dy: int, x: int | None = None, y: int | None = None) -> dict:
    if x is not None and y is not None:
        await b.move(int(x), int(y))
    await b.scroll(int(dx), int(dy))
    return {"dx": dx, "dy": dy}
