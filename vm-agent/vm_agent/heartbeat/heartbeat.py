from __future__ import annotations

import os
import platform
import shutil
import time

STARTED = time.time()


def health(backend_name: str) -> dict:
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    disk = shutil.disk_usage(os.path.expanduser("~"))
    return {
        "ok": True,
        "backend": backend_name,
        "hostname": platform.node(),
        "uptime_s": int(time.time() - STARTED),
        "load": load,
        "disk_free_gb": round(disk.free / 1e9, 1),
        "display": os.environ.get("DISPLAY"),
    }
