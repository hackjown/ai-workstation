from __future__ import annotations

from .desktop import DesktopBackend, png_b64


async def screenshot(backend: DesktopBackend, max_width: int | None = None) -> dict:
    img = await backend.screenshot()
    return {"screenshot": png_b64(img, max_width), "width": img.width, "height": img.height}
