"""v3.9.24 token usage hook.

Every HTTP call a provider makes reports its `usage` block here. Who made the call (role: main / chat / small /
vision / subagent / route / review …) and for which task ride on context variables, so no call site has to pass
them along. The API installs a sink (aiw_api.usage) that writes one row per call; with no sink (tests, the VM)
recording is a no-op. A sink error never breaks a model call.
"""
from __future__ import annotations

import contextvars
import logging
import os
from typing import Callable, Optional

log = logging.getLogger("aiw.usage")

ROLE: contextvars.ContextVar[str] = contextvars.ContextVar("aiw_llm_role", default="")
TASK: contextvars.ContextVar[str] = contextvars.ContextVar("aiw_llm_task", default="")

_sink: Optional[Callable[[dict], None]] = None


def set_sink(fn: Optional[Callable[[dict], None]]) -> None:
    global _sink
    _sink = fn


def enabled() -> bool:
    return _sink is not None and os.environ.get("AIW_USAGE", "1") != "0"


def tokens_of(usage: dict) -> tuple[int, int]:
    """(input, output) from an OpenAI-style, Anthropic-style or Gemini-style usage block."""
    u = usage or {}
    inp = u.get("prompt_tokens", u.get("input_tokens", u.get("promptTokenCount", 0))) or 0
    out = u.get("completion_tokens", u.get("output_tokens", u.get("candidatesTokenCount", 0))) or 0
    try:
        return int(inp), int(out)
    except (TypeError, ValueError):
        return 0, 0


def record(model: str, usage: Optional[dict], role: str = "", task_id: str = "") -> None:
    if not enabled() or not usage:
        return
    inp, out = tokens_of(usage)
    if not inp and not out:
        return
    try:
        _sink({"model": model or "", "role": role or ROLE.get() or "other", "task_id": task_id or TASK.get() or "",
               "input": inp, "output": out})
    except Exception as e:  # noqa: BLE001
        log.debug("usage sink: %s", e)
