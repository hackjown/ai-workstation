from __future__ import annotations

from . import safe_path


def write(path: str, content: str, append: bool = False) -> dict:
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": str(p), "size": p.stat().st_size}
