"""Tool Router: every tool call goes through here (spec §30.6).

LLM tool call -> schema check -> policy -> Computer Runtime -> audit event.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from . import browseros as bos
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
        # BrowserOS: one MCP connection (= one tab group) per task; the session manager sets these
        self.bos_key = "default"
        self.bos_title: Optional[str] = None
        self.bos_category: Optional[str] = None

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

        if spec.action == "browseros.call":
            return await self._bos(name, args, intent)

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
            hint = ""
            if spec.action == "shell.exec" and re.search(r"Proxmox|root shell", decision.reason):
                hint = ("。这台电脑的 shell 不能直接操作 Proxmox/远程 root；用户在 设置 › 电脑 › 运维主机 授权过的主机请用 "
                        "remote_exec 工具执行；没有授权就用 ask_user 请用户去那里添加主机（不要让用户在聊天里发密码）")
            return ToolOutcome(name, spec.action, False, decision, error=f"forbidden by policy: {decision.reason}{hint}")
        if decision.needs_confirm:
            ok = await self.confirm(name, payload, decision) if self.confirm else False
            if not ok:
                return ToolOutcome(name, spec.action, False, decision, error=f"user declined: {decision.reason}")

        if spec.action in SETTLE:
            payload.setdefault("settle", SETTLE[spec.action] * float(os.environ.get("AIW_SETTLE_SCALE", "1")))
        observe = spec.action not in ("computer.screenshot", "shell.exec", "browser.network") and not spec.action.startswith("files.")
        t_out = float(args.get("timeout") or _default_timeout(spec.action, payload))
        if spec.action == "shell.exec":
            # the VM kills the command at payload["timeout"] (60 s when absent): pip got 300 s here but died at 60 s,
            # and task-8bd's `om render` was killed at 60 s and started 4 times
            t_out = min(t_out, float(os.environ.get("AIW_SHELL_MAX_TIMEOUT", "3600")))
            payload["timeout"] = t_out
            t_out += 15
        r = await self.computer.execute(spec.action, payload, observe=observe, timeout=t_out)
        # reads are safe to repeat: retry them here so every caller (read_image, image_edit, photopea) is covered
        for i in range(2 if spec.action in _SAFE_REPEAT else 0):
            if r.success or not _TRANSIENT.search(str(r.error or "")):
                break
            import asyncio
            await asyncio.sleep(1.5 * (i + 1))
            r = await self.computer.execute(spec.action, payload, observe=observe, timeout=t_out)
        await self._audit({"phase": "result", "tool": name, "action": spec.action, "success": r.success,
                           "error": r.error, "duration_ms": r.duration_ms})
        if r.success and hasattr(self.policy, "note_success"):
            self.policy.note_success(spec.action, payload)
        return ToolOutcome(name, spec.action, r.success, decision, r.result, r.error, r.screenshot)

    async def _bos(self, name: str, args: dict, intent: str = "") -> ToolOutcome:
        """A BrowserOS tool: policy judged on the classic call it amounts to (a 下单/支付 element click asks, a
        private-network url asks), then one MCP call through the vm-agent. No screenshot: bos_act returns a diff."""
        args = await bos.annotate(self, name, args)
        tool, margs = bos.mcp_call(name, args)          # internal _labels/_label stripped here
        if args.get("secret") and name == "bos_act":
            margs["sensitive"] = True
        decision = Decision(RiskLevel.L1 if name not in bos.READ_ONLY else RiskLevel.L0, "browseros")
        for cname, cargs in bos.classic(name, args):
            cs = BY_NAME.get(cname)
            if cs and cs.action:
                d = self.policy.check(cs.action, cargs)
                if d.level > decision.level:
                    decision = d
        if PolicyEngine.payment_hint(intent) and decision.level < RiskLevel.L3 and name == "bos_act":
            decision = Decision(RiskLevel.L3, "payment intent")
        shown = {k: v for k, v in args.items() if not str(k).startswith("_") and k != "secret"}
        if args.get("secret"):
            shown = {k: ("••••" if k in ("value", "text") else v) for k, v in shown.items()}
            if shown.get("fields"):
                shown["fields"] = [{**f, "value": "••••"} for f in shown["fields"] if isinstance(f, dict)]
        await self._audit({"phase": "policy", "tool": name, "action": "browseros.call", "payload": shown,
                           "level": int(decision.level), "reason": decision.reason})
        if decision.forbidden:
            return ToolOutcome(name, "browseros.call", False, decision, error=f"forbidden by policy: {decision.reason}")
        if decision.needs_confirm:
            label = args.get("_label")
            ok = await self.confirm(name, {**shown, **({"element": label} if label else {})}, decision) if self.confirm else False
            if not ok:
                return ToolOutcome(name, "browseros.call", False, decision, error=f"user declined: {decision.reason}")
        payload = {"tool": tool, "args": margs, "key": self.bos_key or "default"}
        if self.bos_title:
            payload["title"] = self.bos_title
        if self.bos_category:
            payload["category"] = self.bos_category
        t_out = float(os.environ.get("AIW_BOS_TIMEOUT", "120"))
        try:
            r = await self.computer.execute("browseros.call", payload, observe=False, timeout=t_out)
        except Exception as e:  # noqa: BLE001
            return ToolOutcome(name, "browseros.call", False, decision, error=f"BrowserOS: {type(e).__name__}: {e}")
        result = dict(r.result or {}) if isinstance(r.result, dict) else {}
        result.pop("images", None)
        if args.get("_label"):
            result["element"] = args["_label"]
        await self._audit({"phase": "result", "tool": name, "action": "browseros.call", "success": r.success,
                           "error": r.error, "duration_ms": r.duration_ms})
        return ToolOutcome(name, "browseros.call", r.success, decision, result, r.error, None)

    async def _audit(self, rec: dict):
        if self.audit:
            await self.audit(rec)


import re as _re0
_SAFE_REPEAT = {"files.read", "files.list", "computer.screenshot"}
_TRANSIENT = _re0.compile(r"timeout|timed out|connection|reset by peer|broken pipe|eof|temporar|unavailable|"
                          r"\b50[234]\b|bad gateway|websocket|not connected", _re0.I)


def _redact(p: dict) -> dict:
    out = dict(p)
    if out.get("secret") and "text" in out:
        out["text"] = "••••"
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
    if action == "browser.network":   # v3.9.17: it listens `seconds` (max 60) before answering
        return min(60.0, float(args.get("seconds") or 6)) + 20
    if action != "shell.exec":
        return 30
    cmd = str(args.get("command", ""))
    if _RENDER.search(cmd):
        return 1800
    if _MEDIA.search(cmd):
        return 600
    return 300 if _INSTALL.search(cmd) else 60


# renders and encodes run for minutes (task-8bd: om render killed at 60 s, restarted 4 times)
_RENDER = _re.compile(r"\bom\s+(render|export|qa)\b|bin/om\s+(render|export|qa)\b|remotion\s+render|hyperframes\s+render", _re.I)
_MEDIA = _re.compile(r"\bffmpeg\b|\bedge-tts\b|\bom\s+(voice|sound|samples|check)\b|bin/om\s+(voice|sound|samples|check)\b|"
                     r"soffice\b.*--convert-to|libreoffice\b.*--convert-to|whisper", _re.I)
