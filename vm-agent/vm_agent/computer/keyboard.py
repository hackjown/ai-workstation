from __future__ import annotations

from .desktop import DesktopBackend, normalize_key


async def type_text(b: DesktopBackend, text: str) -> dict:
    await b.type(text)
    return {"typed": len(text)}


async def keypress(b: DesktopBackend, key: str) -> dict:
    await b.key(key)
    return {"key": normalize_key(key)}


async def hotkey(b: DesktopBackend, keys: list[str] | str) -> dict:
    combo = keys if isinstance(keys, str) else "+".join(keys)
    await b.key(combo)
    return {"key": normalize_key(combo)}
