from __future__ import annotations

import base64

from . import safe_path

MAX_READ = 2 * 1024 * 1024
MAX_BINARY = 40 * 1024 * 1024  # send_file


def list_dir(path: str = ".") -> dict:
    p = safe_path(path)
    entries = []
    for c in sorted(p.iterdir()):
        st = c.stat()
        entries.append({"name": c.name, "dir": c.is_dir(), "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"path": str(p), "entries": entries}


def read(path: str, binary: bool = False) -> dict:
    p = safe_path(path)
    data = p.read_bytes()[:MAX_BINARY if binary else MAX_READ]
    if binary:
        return {"path": str(p), "content_b64": base64.b64encode(data).decode(), "size": p.stat().st_size}
    return {"path": str(p), "content": data.decode(errors="replace"), "size": p.stat().st_size}
