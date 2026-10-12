"""files.download (URL -> VM ~/workspace/downloads) and files.upload (base64 -> VM)."""
from __future__ import annotations

import base64
import os
from urllib.parse import urlparse

import httpx

from . import safe_path

MAX_DOWNLOAD = 500 * 1024 * 1024


async def download(url: str, path: str | None = None) -> dict:
    name = os.path.basename(urlparse(url).path) or "download.bin"
    target = safe_path(path or f"downloads/{name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise ValueError("download exceeds limit")
                    f.write(chunk)
    return {"path": str(target), "size": size, "url": url}


def upload(path: str, content_b64: str) -> dict:
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(content_b64)
    p.write_bytes(data)
    return {"path": str(p), "size": len(data)}
