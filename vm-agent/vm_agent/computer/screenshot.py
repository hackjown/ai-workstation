from __future__ import annotations

from .desktop import DesktopBackend, png_b64


async def screenshot(backend: DesktopBackend, max_width: int | None = None) -> dict:
    import time
    t0 = time.monotonic()
    img = await backend.screenshot()
    t1 = time.monotonic()
    b64 = png_b64(img, max_width)
    return {"screenshot": b64, "width": img.width, "height": img.height,
            "grabber": getattr(backend, "_grabber", None), "grab_ms": int((t1 - t0) * 1000),
            "encode_ms": int((time.monotonic() - t1) * 1000)}
