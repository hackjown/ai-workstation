"""File access is jailed to VM_AGENT_FILES_ROOT (default ~/workspace).

v3.9.29 (task-68b): files.patch — an exact string replacement with an expected match count that returns a unified
diff. files.write is jailed to ~/workspace, so a dev/fix task whose source lives elsewhere (~/ai-workstation/news/
aiw_news.py) could only change it through side scripts (fix_*.py) — and the model wrote 11 of them without running them.
files.patch edits the source in place. Outside the files root it is allowed only when the caller (the agent loop, for a
path in the dev task contract's allowed_paths) sets allow_outside, and then only under the home folder (or
VM_AGENT_PATCH_ROOTS, ':'-separated) and never on credential/secret paths.
"""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

_SENSITIVE = re.compile(r"(^|/)(\.ssh|\.gnupg|\.aws|\.kube|\.docker)(/|$)|id_rsa|\.env$|\.pem$|\.key$|credentials|"
                        r"password|/etc/(shadow|sudoers)", re.I)


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


def patch_roots() -> list[Path]:
    env = os.environ.get("VM_AGENT_PATCH_ROOTS")
    raw = env.split(":") if env else ["~"]
    return [Path(os.path.expanduser(x)).resolve() for x in raw if x.strip()]


def source_path(p: str, allow_outside: bool = False) -> Path:
    """The files root as safe_path(); with allow_outside also ~ (VM_AGENT_PATCH_ROOTS), never secret paths."""
    raw = os.path.expanduser(str(p or ""))
    try:
        return safe_path(p if not str(p).startswith("~") else raw)
    except PermissionError:
        if not allow_outside:
            raise PermissionError(f"path outside files root: {p} (files_patch outside ~/workspace is only allowed on the "
                                  "dev task contract's source files)")
    target = Path(raw).resolve()
    if _SENSITIVE.search(str(target)):
        raise PermissionError(f"sensitive path: {p}")
    if not any(target == r or r in target.parents for r in patch_roots()):
        raise PermissionError(f"path outside the allowed roots: {p}")
    return target


def patch(path: str, old: str, new: str, count: int = 1, allow_outside: bool = False, dry_run: bool = False) -> dict:
    """Replace `old` with `new` exactly; the number of matches must equal `count` (0 = all, at least one).
    -> {path, replaced, diff, size}. A mismatch raises ValueError naming the real count and near lines."""
    p = source_path(path, allow_outside)
    if not p.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    if not old:
        raise ValueError("old must not be empty")
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0 or (count and n != count):
        hint = ""
        first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")[:60]
        if first:
            near = [f"{i}: {ln.strip()[:120]}" for i, ln in enumerate(text.splitlines(), 1) if first[:30] in ln][:5]
            if near:
                hint = " Lines with the start of `old`: " + " | ".join(near)
        raise ValueError(f"expected {count or '>=1'} match(es) of old in {path}, found {n}.{hint}")
    out = text.replace(old, new) if not count else text.replace(old, new, count)
    diff = "".join(difflib.unified_diff(text.splitlines(True), out.splitlines(True), fromfile=str(p), tofile=str(p), n=2))
    if not dry_run:
        tmp = p.with_name(p.name + ".aiw-tmp")
        tmp.write_text(out, encoding="utf-8")
        try:
            os.chmod(tmp, p.stat().st_mode & 0o7777)   # keep an executable script executable
        except OSError:
            pass
        os.replace(tmp, p)
    return {"path": str(p), "replaced": n if not count else count, "diff": diff[:12000], "size": len(out.encode()),
            "dry_run": bool(dry_run)}
