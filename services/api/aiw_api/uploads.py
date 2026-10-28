"""Uploads (2026-10-28, v2 home): photos, videos and files from the chat's + sheet.

* POST /api/uploads            JSON {name, content_b64} (old clients; up to JSON_MAX_MB)
* POST /api/uploads/raw?name=  the file itself as the request body, streamed to disk (videos; up to RAW_MAX_MB)
* push_to_vm()                 copies a stored upload onto a computer in CHUNK pieces: one files.upload message stays far
                               under the vm-agent's 64 MB websocket frame limit, so a 300 MB video gets there too.
A vm-agent older than 2026-10-28 ignores ``append`` and would keep only the last piece; the size it reports gives that
away and the push fails with a plain message instead of leaving a broken file.
"""
from __future__ import annotations

import base64
import os
import re
import time

MB = 1024 * 1024
JSON_MAX_MB = int(os.environ.get("AIW_UPLOAD_JSON_MAX_MB", "100"))
RAW_MAX_MB = int(os.environ.get("AIW_UPLOAD_MAX_MB", "500"))
CHUNK = int(os.environ.get("AIW_UPLOAD_CHUNK_MB", "16")) * MB

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".hevc")


class PushError(RuntimeError):
    pass


def clean_name(name: str) -> str:
    n = os.path.basename(str(name or "")).replace("/", "_").replace("\\", "_").strip()
    n = re.sub(r"[\x00-\x1f]", "", n)[:120]
    return n or "upload.bin"


def local_path(artifacts_dir: str, name: str) -> str:
    d = os.path.join(artifacts_dir, "uploads")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{int(time.time())}-{name}")


async def push_to_vm(computer, local: str, vm_rel: str, timeout: float = 180) -> int:
    """Copy ``local`` to ``~/workspace/<vm_rel>`` on the computer. Returns the size; raises PushError."""
    total = os.path.getsize(local)
    sent = 0
    with open(local, "rb") as f:
        first = True
        while first or sent < total:
            piece = f.read(CHUNK)
            payload = {"path": vm_rel, "content_b64": base64.b64encode(piece).decode()}
            if not first:
                payload["append"] = True
            r = await computer.execute("files.upload", payload, observe=False, timeout=timeout)
            if not r.success:
                raise PushError(f"放到电脑上失败：{r.error}")
            sent += len(piece)
            res = getattr(r, "result", None)
            got = res.get("size") if isinstance(res, dict) else None
            if not first and got is not None and int(got) != sent:
                raise PushError(f"电脑上的 vm-agent 太旧，不能分段接收大于 {CHUNK // MB} MB 的文件：请更新 vm-agent（设置 › 电脑 › 更新）后再试")
            first = False
    return total
