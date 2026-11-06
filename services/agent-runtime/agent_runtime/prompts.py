"""v3.9.24 (7) prompt management: per-role system prompts the user can view, edit, version and restore.

Code keeps the original text; call sites ask `get(role, ORIGINAL)` and get the user's active version when there is
one (and AIW_PROMPT_OVERRIDES is not 0), else the original unchanged. The API (aiw_api.prompts_api) owns storage
and pushes the active overrides here with `set_overrides`; nothing in agent_runtime touches the database.

`trace(task_id, role, system, tools, model)` reports what one role actually received for a task; the API stores
it so 设置 › 提示词 can show it next to the task.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

ROLES: dict[str, dict] = {}            # role -> {"title", "desc"}; the original text is registered with it
_ORIGINAL: dict[str, str] = {}
_OVERRIDES: dict[str, str] = {}
_trace_sink: Optional[Callable[[str, str, dict], None]] = None


def register(role: str, title: str, original: str, desc: str = "") -> None:
    ROLES[role] = {"title": title, "desc": desc}
    _ORIGINAL[role] = original


def original(role: str) -> Optional[str]:
    return _ORIGINAL.get(role)


def set_overrides(over: dict[str, str]) -> None:
    _OVERRIDES.clear()
    _OVERRIDES.update({k: v for k, v in (over or {}).items() if isinstance(v, str) and v.strip()})


def active(role: str) -> bool:
    return role in _OVERRIDES and os.environ.get("AIW_PROMPT_OVERRIDES", "1") != "0"


def get(role: str, default: str) -> str:
    """The user's active version of `role`, else `default` (the code's own text)."""
    if role not in _ORIGINAL:
        _ORIGINAL[role] = default
    if active(role):
        return _OVERRIDES[role]
    return default


def fmt(role: str, default: str, **kw) -> str:
    """get() + str.format; an edited prompt whose {placeholders} no longer fit falls back to the original."""
    text = get(role, default)
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return default.format(**kw)


def set_trace_sink(fn: Optional[Callable[[str, str, dict], None]]) -> None:
    global _trace_sink
    _trace_sink = fn


def trace(task_id: str, role: str, system: str, tools: Optional[list] = None, model: str = "", **extra) -> None:
    if _trace_sink is None or not task_id:
        return
    try:
        names = []
        for t in tools or []:
            n = t.get("name") if isinstance(t, dict) else str(t)
            if n:
                names.append(n)
        _trace_sink(task_id, role, {"system": system or "", "tools": names, "model": model or "",
                                    "overridden": active(role), **extra})
    except Exception:  # noqa: BLE001  tracing never breaks a run
        pass
