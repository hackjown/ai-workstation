"""Tool Router: every tool call goes through here (spec §30.6).

LLM tool call -> schema check -> policy -> Computer Runtime -> audit event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .policy import Decision, PolicyEngine, RiskLevel
from .registry import BY_NAME

ConfirmFn = Callable[[str, dict, Decision], Awaitable[bool]]
AuditFn = Callable[[dict], Awaitable[None]]

COORD_KEYS = ("x", "y", "x1", "y1", "x2", "y2")
# seconds to wait after an action before the follow-up screenshot: long enough for a page to react,
# because that screenshot is also what the model sees on its next step
SETTLE = {"computer.click": 0.8, "computer.double_click": 0.8, "computer.right_click": 0.6, "computer.keypress": 0.6,
          "computer.type": 0.3, "browser.click": 1.2, "browser.fill": 0.4, "browser.navigate": 1.5,
          "browser.open": 1.0, "browser.back": 1.2, "browser.forward": 1.2}


@dataclass
class ToolOutcome:
    tool: str
    action: Optional[str]
    success: bool
    decision: Decision
    result: dict = field(default_factory=dict)
    error: Optional[str] = None
    screenshot: Optional[str] = None
    control: Optional[str] = None  # "done" / "fail" for loop control tools


class ToolExecutor:
    def __init__(self, computer, policy: Optional[PolicyEngine] = None,
                 confirm: Optional[ConfirmFn] = None, audit: Optional[AuditFn] = None):
        self.computer = computer
        self.policy = policy or PolicyEngine()
        self.confirm = confirm
        self.audit = audit
        self.scale = 1.0  # screen px / screenshot px, set by the loop when it downsizes

    async def run(self, name: str, args: dict, intent: str = "") -> ToolOutcome:
        spec = BY_NAME.get(name)
        if spec is None:
            return ToolOutcome(name, None, False, Decision(RiskLevel.L4, "unknown tool"), error=f"unknown tool {name}")
        if name in ("done", "fail"):  # never bounce the finish on a missing text arg
            args = dict(args)
            for k in spec.required:
                if not args.get(k):
                    args[k] = intent or ("done" if name == "done" else "gave up")
        missing = [k for k in spec.required if k not in args]
        if missing:
            return ToolOutcome(name, spec.action, False, Decision(RiskLevel.L0, "schema"), error=f"missing args {missing}")
        if spec.action is None:
            return ToolOutcome(name, None, True, Decision(RiskLevel.L0, "control"), result=args, control=name)

        payload = dict(args)
        if name == "send_file":
            payload["binary"] = True
        if self.scale != 1.0:
            for k in COORD_KEYS:
                if k in payload and payload[k] is not None:
                    payload[k] = int(round(payload[k] * self.scale))

        decision = self.policy.check(spec.action, payload)
        if PolicyEngine.payment_hint(intent) and decision.level < RiskLevel.L3:
            decision = Decision(RiskLevel.L3, "payment intent")
        await self._audit({"phase": "policy", "tool": name, "action": spec.action, "payload": _redact(payload),
                           "level": int(decision.level), "reason": decision.reason})

        if decision.forbidden:
            return ToolOutcome(name, spec.action, False, decision, error=f"forbidden by policy: {decision.reason}")
        if decision.needs_confirm:
            ok = await self.confirm(name, payload, decision) if self.confirm else False
            if not ok:
                return ToolOutcome(name, spec.action, False, decision, error=f"user declined: {decision.reason}")

        if spec.action in SETTLE:
            import os
            payload.setdefault("settle", SETTLE[spec.action] * float(os.environ.get("AIW_SETTLE_SCALE", "1")))
        observe = spec.action not in ("computer.screenshot", "shell.exec") and not spec.action.startswith("files.")
        r = await self.computer.execute(spec.action, payload, observe=observe,
                                        timeout=float(args.get("timeout", _default_timeout(spec.action, payload))))
        await self._audit({"phase": "result", "tool": name, "action": spec.action, "success": r.success,
                           "error": r.error, "duration_ms": r.duration_ms})
        return ToolOutcome(name, spec.action, r.success, decision, r.result, r.error, r.screenshot)

    async def _audit(self, rec: dict):
        if self.audit:
            await self.audit(rec)


def _redact(p: dict) -> dict:
    out = dict(p)
    if "content_b64" in out:
        out["content_b64"] = f"<{len(out['content_b64'])} b64 chars>"
    if "content" in out and isinstance(out["content"], str) and len(out["content"]) > 200:
        out["content"] = out["content"][:200] + "..."
    return out


import re as _re
_INSTALL = _re.compile(r"\b(pip3?|apt(-get)?|npm|pnpm|yarn|conda|uv)\b.*\b(install|add|update|upgrade)\b|playwright install|git clone|curl|wget", _re.I)


def _default_timeout(action: str, args: dict) -> float:
    """Shell commands get 60s, package installs and downloads 300s (a mirror can be slow)."""
    if action == "files.read" and args.get("binary"):
        # send_file: the whole file comes back base64 in one message. Through the InstaBox tunnel a 3.4 MB PDF did
        # not arrive within 30 s (task-a0e), three tries failed and the jammed socket timed out the next shell too.
        import os
        return float(os.environ.get("AIW_SEND_FILE_TIMEOUT", "300"))
    if action != "shell.exec":
        return 30
    return 300 if _INSTALL.search(str(args.get("command", ""))) else 60
