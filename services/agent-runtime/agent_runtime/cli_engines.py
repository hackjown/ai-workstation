"""v3.9.60: AIW's side of cli_bridge.py (Codex, Claude Code, CodeBuddy, Grok Build on the computer).

Same pattern as antigravity.py: the bridge is written to ~/.config/aiw/cli/cli_bridge.py (only when its hash
changed), then every op is one shell call whose last line is `CLIRESULT {json}`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
from pathlib import Path

BRIDGE_SRC = Path(__file__).with_name("cli_bridge.py")
REMOTE = "$HOME/.config/aiw/cli/cli_bridge.py"
ENGINES = ("codex", "claude", "codebuddy", "grok")


def install_command() -> str:
    src = BRIDGE_SRC.read_bytes()
    b64 = base64.b64encode(src).decode()
    digest = hashlib.sha1(src).hexdigest()
    return (f'mkdir -p "$HOME/.config/aiw/cli" && f="{REMOTE}" && '
            f'if [ "$(sha1sum "$f" 2>/dev/null | cut -d" " -f1)" != "{digest}" ]; then '
            f"echo '{b64}' | base64 -d > \"$f.$$.tmp\" && mv -f \"$f.$$.tmp\" \"$f\"; fi && "
            f"[ \"$(sha1sum \"$f\" | cut -d' ' -f1)\" = \"{digest}\" ] && echo CLI_BRIDGE_OK")


def command(op: str, a: dict, job: dict | None = None) -> tuple[str, int]:
    """(shell command, timeout seconds) for one op."""
    py = f'python3 "{REMOTE}"'
    jid = shlex.quote(str(a.get("job") or ""))
    eng = shlex.quote(str(a.get("engine") or ""))
    if op == "check":
        return f"{py} check {eng}" + (" --probe" if a.get("probe") else ""), 200
    if op == "models":
        return f"{py} models {eng}", 120
    if op == "start":
        b = base64.b64encode(json.dumps(job or {}, ensure_ascii=False).encode()).decode()
        return f"{py} start --job-b64 {b}", 120
    if op == "wait":
        secs = max(5, min(int(a.get("seconds") or 600), 1500))
        return f"{py} wait {jid} --seconds {secs} --cursor {int(a.get('cursor') or 0)}", secs + 60
    if op == "send":
        b = base64.b64encode(str(a.get("message") or "").encode()).decode()
        return f"{py} send {jid} --text-b64 {b}", 30
    if op == "diff":
        return f"{py} diff {jid}" + (" --full" if a.get("full") else ""), 150
    if op == "stop":
        return f"{py} stop {jid}", 60
    return f"{py} list", 30


def parse(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("CLIRESULT "):
            try:
                return json.loads(line[10:])
            except ValueError:
                return None
    return None
