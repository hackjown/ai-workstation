"""ComputerProvider - the one interface Agent/Tool layers depend on (spec §14).

Swapping PVE for a cloud VM, E2B or Windows means a new provider, never a
change to agent-runtime (spec §30.14).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ActionResult:
    success: bool
    result: dict[str, Any] = field(default_factory=dict)
    screenshot: Optional[str] = None  # base64 PNG
    width: Optional[int] = None
    height: Optional[int] = None
    error: Optional[str] = None
    action: Optional[str] = None
    duration_ms: Optional[int] = None


class ComputerProvider(abc.ABC):
    @abc.abstractmethod
    async def execute(self, action: str, payload: dict | None = None, observe: bool = False,
                      timeout: float = 30.0) -> ActionResult: ...

    # Convenience surface from the spec. All route through execute().
    async def screenshot(self) -> ActionResult:
        return await self.execute("computer.screenshot")

    async def click(self, x: int, y: int, button: str = "left") -> ActionResult:
        return await self.execute("computer.click", {"x": x, "y": y, "button": button})

    async def double_click(self, x: int, y: int) -> ActionResult:
        return await self.execute("computer.double_click", {"x": x, "y": y})

    async def right_click(self, x: int, y: int) -> ActionResult:
        return await self.execute("computer.right_click", {"x": x, "y": y})

    async def move(self, x: int, y: int) -> ActionResult:
        return await self.execute("computer.move", {"x": x, "y": y})

    async def type(self, text: str) -> ActionResult:
        return await self.execute("computer.type", {"text": text})

    async def keypress(self, key: str) -> ActionResult:
        return await self.execute("computer.keypress", {"key": key})

    async def hotkey(self, *keys: str) -> ActionResult:
        return await self.execute("computer.hotkey", {"keys": list(keys)})

    async def scroll(self, dx: int, dy: int, x: int | None = None, y: int | None = None) -> ActionResult:
        p = {"dx": dx, "dy": dy}
        if x is not None and y is not None:
            p.update(x=x, y=y)
        return await self.execute("computer.scroll", p)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> ActionResult:
        return await self.execute("computer.drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def shell(self, command: str, cwd: str | None = None, timeout: float = 60) -> ActionResult:
        p = {"command": command, "timeout": timeout}
        if cwd:
            p["cwd"] = cwd
        return await self.execute("shell.exec", p, timeout=timeout + 5)

    async def open_app(self, name: str) -> ActionResult:
        return await self.execute("computer.open_app", {"name": name})

    async def close_app(self, name: str) -> ActionResult:
        return await self.execute("computer.close_app", {"name": name})

    async def screen_size(self) -> tuple[int, int]:
        r = await self.execute("computer.screen_size")
        return r.result["width"], r.result["height"]

    async def health(self) -> ActionResult:
        return await self.execute("system.health")

    async def close(self) -> None:  # pragma: no cover - optional
        return None
