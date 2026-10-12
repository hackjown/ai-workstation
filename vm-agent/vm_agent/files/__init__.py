"""File access is jailed to VM_AGENT_FILES_ROOT (default ~/workspace)."""
from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    r = Path(os.path.expanduser(os.environ.get("VM_AGENT_FILES_ROOT", "~/workspace"))).resolve()
    r.mkdir(parents=True, exist_ok=True)
    return r


def safe_path(p: str) -> Path:
    base = root()
    target = (base / p.lstrip("/")).resolve() if not Path(p).is_absolute() else Path(p).resolve()
    if base != target and base not in target.parents:
        raise PermissionError(f"path outside files root: {p}")
    return target
