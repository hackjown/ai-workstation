"""Computer facade: what tool-router holds. Adds observe() helpers."""
from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from typing import Optional

from .providers.base import ActionResult, ComputerProvider


@dataclass
class Snapshot:
    screenshot_b64: str
    width: int
    height: int
    digest: str

    def thumbnail_diff(self, other: "Snapshot") -> float:
        """0.0 identical .. 1.0 completely different (page-change detection)."""
        from PIL import Image, ImageChops
        a = Image.open(io.BytesIO(base64.b64decode(self.screenshot_b64))).convert("L").resize((64, 40))
        b = Image.open(io.BytesIO(base64.b64decode(other.screenshot_b64))).convert("L").resize((64, 40))
        diff = ImageChops.difference(a, b)
        return diff.point(lambda p: 255 if p > 24 else 0).histogram()[255] / (64 * 40)


class Computer:
    def __init__(self, provider: ComputerProvider, machine_id: str = "ai-linux-001"):
        self.provider = provider
        self.machine_id = machine_id

    async def observe(self) -> Optional[Snapshot]:
        r = await self.provider.screenshot()
        if not r.success or not r.screenshot:
            return None
        return Snapshot(r.screenshot, r.width or 0, r.height or 0,
                        hashlib.sha1(r.screenshot.encode()).hexdigest())

    async def execute(self, action: str, payload: dict | None = None, observe: bool = False,
                      timeout: float = 30.0) -> ActionResult:
        return await self.provider.execute(action, payload, observe=observe, timeout=timeout)

    def __getattr__(self, item):
        # computer.click(...), computer.type(...) etc. delegate to the provider.
        return getattr(self.provider, item)
