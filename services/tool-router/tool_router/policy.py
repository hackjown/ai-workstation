"""Policy Engine (spec §19). Every tool call is classified before execution.

L0 auto | L1 low-risk auto | L2 execute + audit | L3 needs user confirm | L4 forbidden
Rules are ordered; highest level wins. Extend via PolicyEngine(extra_rules=...).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional
from urllib.parse import urlparse


class RiskLevel(IntEnum):
    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3
    L4 = 4


@dataclass
class Decision:
    level: RiskLevel
    reason: str

    @property
    def allowed(self) -> bool:
        return self.level < RiskLevel.L3

    @property
    def needs_confirm(self) -> bool:
        return self.level == RiskLevel.L3

    @property
    def forbidden(self) -> bool:
        return self.level == RiskLevel.L4


Rule = Callable[[str, dict], Optional[Decision]]

SHELL_L4 = [
    (r"\bsudo\s+su\b|\bsu\s+-?\s*root\b|\bsudo\s+-i\b|\bsudo\s+-s\b", "root shell"),
    (r"/etc/pve\b|\bpvesh\b|\bqm\s+|\bpct\s+|\bpvecm\b|:8006\b", "touches Proxmox"),
    (r"\bmkfs\b|\bdd\s+.*of=/dev/|>\s*/dev/sd", "raw disk write"),
    (r"\b(shutdown|poweroff|halt|reboot)\b", "power control belongs to VM Runtime"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\b(iptables|nft|ufw)\b", "firewall change"),
]
SHELL_L3 = [
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b", "recursive force delete"),
    (r"\brm\s+.*(\*|~/?\s*$|/\s*$)", "bulk delete"),
    (r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba)?sh", "pipe remote script to shell"),
    (r"\bscp\b|\brsync\b.*@|\bnc\b\s|\bsftp\b", "send files off-VM"),
    (r"\bssh\b", "remote login"),
    (r"\bchmod\s+-R\s+777\b", "world-writable tree"),
    (r"\bgit\s+push\b", "publish code"),
]
SHELL_L2 = [
    (r"\bsudo\b", "privileged command"),
    (r"\b(apt|apt-get|snap)\s+(install|remove|purge)\b", "package change"),
    (r"\bpip3?\s+install\b|\bnpm\s+(i|install)\b", "package install"),
    (r"\bsystemctl\b", "service control"),
]
SENSITIVE_PATH = re.compile(r"(\.ssh/|id_rsa|\.env\b|\.aws/|credentials|\.pem$|\.key$|/etc/shadow|password)", re.I)
PAY_WORDS = re.compile(r"(checkout|payment|pay now|place order|立即支付|确认付款|下单|支付)", re.I)


class PolicyEngine:
    def __init__(self, allow_domains: Optional[list[str]] = None, extra_rules: Optional[list[Rule]] = None):
        env = os.environ.get("AIW_ALLOW_DOMAINS", "")
        self.allow_domains = allow_domains if allow_domains is not None else [d.strip() for d in env.split(",") if d.strip()]
        self.rules: list[Rule] = [self._shell, self._files, self._net, self._typing, self._dom] + (extra_rules or [])

    def check(self, action: str, payload: dict) -> Decision:
        best = Decision(RiskLevel.L0, "default allow")
        for rule in self.rules:
            d = rule(action, payload or {})
            if d and d.level > best.level:
                best = d
        return best

    # --- rules -------------------------------------------------------------
    def _shell(self, action, p):
        if action != "shell.exec":
            return None
        cmd = p.get("command", "")
        for pats, lvl in ((SHELL_L4, RiskLevel.L4), (SHELL_L3, RiskLevel.L3), (SHELL_L2, RiskLevel.L2)):
            for pat, why in pats:
                if re.search(pat, cmd):
                    return Decision(lvl, f"shell: {why}")
        if SENSITIVE_PATH.search(cmd):
            return Decision(RiskLevel.L3, "shell touches sensitive file")
        return Decision(RiskLevel.L1, "shell command")

    def _files(self, action, p):
        if not action.startswith("files."):
            return None
        path = p.get("path", "") or ""
        if action == "files.upload" and SENSITIVE_PATH.search(path):
            return Decision(RiskLevel.L3, "upload of sensitive file")
        if SENSITIVE_PATH.search(path):
            return Decision(RiskLevel.L3, "sensitive path")
        return Decision(RiskLevel.L0 if action in ("files.list", "files.read") else RiskLevel.L1, "workspace file op")

    def _net(self, action, p):
        if action not in ("browser.open", "browser.navigate", "files.download"):
            return None
        url = p.get("url")
        if not url:
            return Decision(RiskLevel.L0, "open browser")
        host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
        if host in ("localhost", "127.0.0.1") or host.startswith(("10.", "192.168.", "172.16.")) or host.endswith(".lan"):
            return Decision(RiskLevel.L3, f"private network target {host}")
        if self.allow_domains and not any(host == d or host.endswith("." + d) for d in self.allow_domains):
            return Decision(RiskLevel.L3, f"domain {host} not in allowlist")
        return Decision(RiskLevel.L2 if action == "files.download" else RiskLevel.L0, "network access")

    def _typing(self, action, p):
        if action == "computer.type" and SENSITIVE_PATH.search(p.get("text", "")):
            return Decision(RiskLevel.L2, "typing sensitive-looking text")
        return None

    def _dom(self, action, p):
        if action == "browser.click" and PAY_WORDS.search(p.get("text", "")):
            return Decision(RiskLevel.L3, "clicking a payment button")
        if action == "browser.fill" and SENSITIVE_PATH.search(p.get("text", "")):
            return Decision(RiskLevel.L2, "typing sensitive-looking text")
        return None

    @staticmethod
    def payment_hint(text: str) -> bool:
        """Agent loop calls this on the model's stated intent to force L3 on payments."""
        return bool(PAY_WORDS.search(text or ""))
