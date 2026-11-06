"""v3.9.24 (8) per-model context settings for the step history.

Each step the model gets the system prompt, the task text and the last 25 history lines (llm_router.user_text).
With a context setting for the step's model (设置 › 上下文, kv "model_context": {model | "*": {window, trigger_ratio,
trigger_tokens, keep_last}}) the history is compressed before it is sent once its estimated size passes the trigger:
the newest `keep_last` lines stay word for word, older lines become one short line each inside a single
"EARLIER STEPS (compressed)" block, and steps that fell out of the 25-line window are named in one line instead of
vanishing. The facts board (findings) is part of the task text and is never compressed.

No setting for the model (and no "*" default) = the history goes out exactly as before. AIW_CONTEXT_MANAGE=0 = off.
Token counts are estimates (CJK char ≈ 1 token, other chars ≈ 1/4 token); no tokenizer dependency.
"""
from __future__ import annotations

import os
import re
from typing import Optional

WINDOW = 25            # lines user_text() sends
_CFG: dict[str, dict] = {}
_CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]")


def set_config(cfg: dict) -> None:
    _CFG.clear()
    _CFG.update({str(k): dict(v) for k, v in (cfg or {}).items() if isinstance(v, dict)})


def config_for(model: str) -> Optional[dict]:
    if os.environ.get("AIW_CONTEXT_MANAGE", "1") == "0":
        return None
    m = (model or "").strip()
    c = _CFG.get(m)
    if c is None:   # a prefix entry ("deepseek" covers "deepseek-v4-pro"), then the default
        c = next((v for k, v in _CFG.items() if k not in ("*",) and k and m.startswith(k)), None)
    return c if c is not None else _CFG.get("*")


def est_tokens(text: str) -> int:
    t = text or ""
    cjk = len(_CJK.findall(t))
    return cjk + (len(t) - cjk + 3) // 4


def trigger_of(c: dict) -> int:
    window = int(c.get("window") or 0)
    if c.get("trigger_tokens"):
        return int(c["trigger_tokens"])
    ratio = float(c.get("trigger_ratio") or 0.7)
    return int(window * ratio) if window else 0


def fit(history: list[str], model: str, fixed: str = "") -> tuple[list[str], Optional[dict]]:
    """-> (history to send, info or None when nothing changed). `fixed` = system + task text (counted, never cut)."""
    c = config_for(model)
    if not c or not history:
        return history, None
    trig = trigger_of(c)
    if trig <= 0:
        return history, None
    base = est_tokens(fixed)
    window = history[-WINDOW:]
    before = base + sum(est_tokens(h) for h in window)
    dropped = len(history) - len(window)
    if before <= trig:
        return history, None
    keep = max(2, min(int(c.get("keep_last") or 8), WINDOW - 2))
    old, recent = window[:-keep], window[-keep:]
    per = int(c.get("line_chars") or 160)
    lines = []
    if dropped:
        names = [m.group(1) for h in history[:dropped] for m in [re.match(r"\s*\d+\.\s*([A-Za-z_][\w.]*)", h)] if m]
        tally: dict[str, int] = {}
        for n in names:
            tally[n] = tally.get(n, 0) + 1
        lines.append(f"steps 1-{dropped}: " + (", ".join(f"{k}×{v}" for k, v in tally.items()) or "earlier actions"))
    for h in old:
        one = " ".join((h or "").split())
        lines.append(one[:per] + ("…" if len(one) > per else ""))
    block = "EARLIER STEPS (compressed to fit the context; details were cut):\n" + "\n".join(lines)
    out = [block] + list(recent)
    after = base + sum(est_tokens(h) for h in out)
    cap = int(c.get("recent_chars") or 1500)
    if after > trig:   # still too big: cut the long outputs of the kept steps too
        out = [block] + [h if len(h) <= cap else h[:cap] + " …[cut]" for h in recent]
        after = base + sum(est_tokens(h) for h in out)
    return out, {"before": before, "after": after, "trigger": trig, "kept": len(recent), "compressed": len(old),
                 "dropped": dropped}
