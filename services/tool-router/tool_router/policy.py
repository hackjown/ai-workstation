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

from . import explain as _ex
from . import remote as _rm


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
    scope: str = ""   # v3.9.17: same scope approved once in a task = approved for the rest of it (remote_diag:<host>)

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
    # BrowserOS's DevTools (9110) and MCP (9210, /mcp) ports: the bos_* tools reach them through the element gates
    (r"\b(9110|9210)\b|/mcp(?![\w.-])", "BrowserOS internals (use the bos_* tools)"),
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
    (r"\bsystemctl\b", "service control"),
]
# sudo and package installs used to be L2 (run + audit). They now ask (L3) through the plain-language classifier in
# explain.py, like deletes outside the workspace, uploads, git push. AIW_AUTO_INSTALL=1 keeps pip/npm installs (not
# apt/sudo) automatic for setups that relied on that.
SENSITIVE_PATH = re.compile(r"(\.ssh/|id_rsa|\.env\b|\.aws/|credentials|\.pem$|\.key$|/etc/shadow|password)", re.I)
# v3.9.17: in a shell command only a real file path counts. `password=PWD` in a paramiko script, `{{task_cred_1}}`
# or the word "password" in a heredoc is not a sensitive file (task-833: 8 cards for read-only checks).
SENSITIVE_FILE = re.compile(
    r"(?:^|[\s'\"=:(<>])(?:~|\$HOME|/|\.{1,2}/)?[\w./-]*(?:\.ssh/[\w.-]*|id_(?:rsa|ed25519|ecdsa)\b|\.aws/[\w.-]*|/etc/shadow\b|"
    r"(?<![\w])\.env\b(?![\w(])|[\w-]*credentials(?:\.json|\.ya?ml|\.txt)?(?=$|[\s'\";|])|[\w-]+\.pem\b|[\w-]+\.key\b|"
    r"[\w-]*passwords?\.(?:txt|json|csv|ya?ml|kdbx|db)\b)", re.I)
# v3.9.19: a path named only to be LEFT OUT (rsync/tar --exclude, zip -x, find ! -name, grep -v, != ".env") is not
# a read of that file (task-d31: packaging without .env asked for confirmation 3 times).
_EXCLUDED = re.compile(r"""--exclude(?:=|\s+)(?:'[^']*'|"[^"]*"|\S+)|(?:!|-not)\s+-(?:i?name|path)\s+(?:'[^']*'|"[^"]*"|\S+)|"""
                       r"""\bgrep\s+-v\w*\s+(?:'[^']*'|"[^"]*"|\S+)|!=\s*(?:'[^']*'|"[^"]*")|"""
                       r"""\bzip\b[^;&|\n]*?\s-x((?:\s+(?:'[^']*'|"[^"]*"|[^\s;&|-]\S*))+)""")


def _drop_excluded(cmd: str) -> str:
    return _EXCLUDED.sub(" ", cmd)


PAY_WORDS = re.compile(r"(checkout|payment|pay now|place order|立即支付|确认付款|下单|支付)", re.I)


_RMRF = re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r|-f|-r|-rf)\s+([^;&|\n]+)")
_PROTECTED = {"workspace", "uploads", "outputs", "home", "data", ".", "..", "~", "/"}


def scratch_delete(cmd: str) -> bool:
    """task-247: `rm -rf /tmp/pptx_extract` and `rm -rf huashu-film` (a folder the task made in ~/workspace) each
    waited 5 min on a confirmation nobody saw. A recursive delete of a scratch path is routine; everything else
    still asks. Scratch: /tmp/<name>..., ~/workspace/<name>..., or a bare relative <name>/... when the command cd's into ~/workspace
    (the shell starts in ~, so without that cd a bare name is not known to be scratch)."""
    hits = list(_RMRF.finditer(cmd))
    # v3.9.22 task-ca7: `SDK=$(ls -d /tmp/x)` later in the command is not part of the delete; only the rm's own
    # targets must be plain (no $(...), backticks or ..)
    if not hits or any(re.search(r"\$\(|`|\.\.", m.group(2)) for m in hits):
        return False
    cds = re.findall(r"\bcd\s+([^;&|\s]+)", cmd)
    # v3.9.19: a project folder under ~/workspace counts too (cd ~/workspace/tasks/task-x && rm -rf pkg_out)
    # v3.9.22: a relative cd after that (cd src) stays inside it
    ws = r"(~|\$HOME|\$\{HOME\}|/data/home|/home/[\w.-]+|/root)/workspace(/[\w.@%+-]+)*"
    rel_ok = bool(cds) and bool(re.fullmatch(ws, cds[0].rstrip("/"))) and ".." not in cds[0] and all(
        (re.fullmatch(ws, c.rstrip("/")) or re.fullmatch(r"[\w@%+-][\w.@%+-]*(/[\w.@%+-]+)*", c.rstrip("/")))
        and ".." not in c for c in cds)
    for m in hits:
        for t in m.group(2).split():
            t = t.strip("'\"")
            if not t or t.startswith("-"):
                continue
            first = t.split("/")[0]
            if "*" in t:  # a glob only in the last part, inside a project folder: out/v*.mp4 (task-8bd asked 5 min for it)
                head, _, last = t.rstrip("/").rpartition("/")
                if "*" in head or last.strip("*.") == "" or not (
                        re.match(r"^/tmp/[\w.@%+-]+$", head)
                        or (re.match(r"^(~|\$HOME|/data/home|/home/[\w.-]+)/workspace/[\w.@%+-]+(/[\w.@%+-]+)*$", head)
                            and head.count("/") >= 2)):
                    return False
                continue
            if re.match(r"^/tmp/[\w.@%+-]", t):
                continue
            if re.match(r"^(~|\$HOME|/data/home|/home/[\w.-]+)/workspace/[\w.@%+-]", t) and \
                    t.rstrip("/").split("/")[-1] not in _PROTECTED and t.rstrip("/").count("/") >= 2:
                continue
            if rel_ok and not t.startswith(("/", "~", "$")) and first not in _PROTECTED and re.match(r"^[\w@%+-][\w.@%+-]*$", first):
                continue
            return False
    return True


class PolicyEngine:
    def __init__(self, allow_domains: Optional[list[str]] = None, extra_rules: Optional[list[Rule]] = None):
        env = os.environ.get("AIW_ALLOW_DOMAINS", "")
        self.allow_domains = allow_domains if allow_domains is not None else [d.strip() for d in env.split(",") if d.strip()]
        self.rules: list[Rule] = [self._shell, self._files, self._net, self._typing, self._dom] + (extra_rules or [])
        self.created: set = set()   # folders this task made (mkdir / --outdir): deleting them again is routine

    def note_success(self, action: str, payload: dict):
        """Called after a tool call ran: remember the folders a shell command created."""
        if action == "shell.exec":
            try:
                self.created |= _ex.created_dirs(str((payload or {}).get("command") or ""))
            except Exception:  # noqa: BLE001
                pass

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
        full = cmd = p.get("command", "")
        for pat, why in SHELL_L4:
            if re.search(pat, cmd):
                return Decision(RiskLevel.L4, f"shell: {why}")
        if scratch_delete(cmd) or self._own_delete(cmd):  # strip the scratch deletes, judge the rest as usual
            cmd = _RMRF.sub("true", cmd)
        remote = _rm.is_remote(cmd) and os.environ.get("AIW_REMOTE_DEDUPE", "1") != "0"
        for pat, why in SHELL_L3:
            if remote and why == "remote login":
                continue
            if re.search(pat, cmd):
                return Decision(RiskLevel.L3, f"shell: {why}")
        if SENSITIVE_FILE.search(_drop_excluded(cmd)):
            return Decision(RiskLevel.L3, "shell touches sensitive file")
        if remote:   # v3.9.17: judged by what it runs on the other host
            j = _rm.judge(cmd)
            if j == "read":
                return Decision(RiskLevel.L2, "shell: remote read-only diagnosis")
            if j == "write":
                return Decision(RiskLevel.L3, "shell: remote change")
            return Decision(RiskLevel.L3, "shell: remote login", scope=_rm.scope_for(cmd))
        try:
            e = _ex.explain_shell(full, self.created)
        except Exception:  # noqa: BLE001
            e = {"ask": None, "category": "other"}
        if e.get("ask"):
            if e.get("category") == "install" and os.environ.get("AIW_AUTO_INSTALL", "").lower() in ("1", "true", "pip", "yes") \
                    and not re.search(r"\bsudo\b|\b(apt|apt-get|snap|dnf|yum|dpkg)\b|\s-g\b|--global|--break-system", full) \
                    and not re.search(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", full):
                return Decision(RiskLevel.L2, "shell: package install")
            return Decision(RiskLevel.L3, f"shell: {e['ask']}")
        for pat, why in SHELL_L2:
            if re.search(pat, cmd):
                return Decision(RiskLevel.L2, f"shell: {why}")
        return Decision(RiskLevel.L1, "shell command")

    def _own_delete(self, cmd: str) -> bool:
        """rm -rf of folders this task created itself (under the workspace or /tmp), nothing else in the rm."""
        if not self.created:
            return False
        hits = list(_RMRF.finditer(cmd))
        if not hits or re.search(r"\$\(|`|\*", cmd):
            return False
        cwd = "~"
        m = re.search(r"\bcd\s+([^;&|\s]+)", cmd)
        if m and cmd.find(m.group(0)) < hits[0].start():
            cwd = _ex.norm_path(m.group(1))
        for h in hits:
            for t in h.group(2).split():
                if t.startswith("-"):
                    continue
                q = _ex.norm_path(t, cwd)
                if q not in self.created or _ex.zone(q) not in ("workspace", "tmp") or q.rstrip("/") in (
                        "~/workspace", "~/workspace/uploads", "~/workspace/outputs"):
                    return False
        return True

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
        if action not in ("browser.open", "browser.navigate", "files.download", "browser.network"):
            return None
        url = p.get("url")
        if action == "browser.network" and not url:
            return Decision(RiskLevel.L0, "read page requests")
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
