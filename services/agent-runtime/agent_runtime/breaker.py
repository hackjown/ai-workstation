"""v3.9.17 工具熔断: the same tool failing the same way twice is switched off for the rest of the task.

The error is reduced to a signature (numbers, paths, ids, quotes dropped) so "timeout after 60s" and "timeout after
61s" are one kind. For shell the key also carries the program the command runs (sshpass, curl, docker …): one
missing program must not take the whole shell away. A tripped tool is hidden from the model and a call to it is
answered with what to use instead.

Pure, tested in tests/test_v3917_breaker.py.
"""
from __future__ import annotations

import os
import re
from typing import Optional

ALTERNATIVE = {
    "shell": "换一种方法（不同的命令/工具），远程主机用 ssh_exec",
    "ssh_exec": "检查主机地址/端口/账号；授权过的运维主机可用 remote_exec",
    "remote_exec": "改用 ssh_exec（同一连接批量执行多条命令）",
    "browser_open": "改用 bos_navigate（BrowserOS）或 shell 里 curl 取页面",
    "browser_navigate": "改用 bos_navigate 或 shell 里 curl",
    "browser_click": "改用 bos_act（按元素 ref 点击）或 click_element",
    "browser_fill": "改用 bos_act kind=fill",
    "browser_network": "改用 bos_read / shell 里 curl -I 看响应头",
    "read_image": "改用 shell 里 python/PIL 或 OCR 读取文字，或换一张更小的图",
    "click_element": "改用 browser_click / bos_act，或按截图坐标 click",
    "files_read": "改用 shell 里 cat/head 读取",
    "files_write": "改用 shell 里 cat > 文件 <<'EOF' 写入",
    "files_download": "改用 shell 里 curl -L -o 下载",
    "generate_image": "换另一个生图接口（设置 › 模型 › 生图接口）或告诉用户",
    "image_edit": "改用 photopea 或 shell 里 PIL 处理",
}
BOS_ALT = "BrowserOS 不可用：改用 browser_open / browser_click / screenshot（普通 Chrome）"
_PROG = re.compile(r"(?:^|[;&|(]\s*|\bsudo\s+|\btimeout\s+\d+\s+)([a-zA-Z][\w.+-]*)")
_SKIP_PROG = {"cd", "export", "set", "echo", "true", "env", "then", "do", "if", "for", "while", "source", "."}


def signature(error: str) -> str:
    e = str(error or "").lower()
    e = re.sub(r"https?://\S+", "<url>", e)
    e = re.sub(r"(/[\w.@%+-]+)+", "<path>", e)
    e = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b", "<id>", e)
    e = re.sub(r"\d+(\.\d+)?", "<n>", e)
    e = re.sub(r"(['\"]).*?\1", "<q>", e)
    e = re.sub(r"\s+", " ", e).strip()
    return e[:160]


_PREFIX = {"sudo", "nohup", "nice", "time", "env", "exec", "command", "stdbuf"}


def program(command: str) -> str:
    """The first real program a shell command runs ('sshpass', 'python3', 'docker')."""
    for seg in re.split(r"&&|\|\||;|\||\n|\(|\)", command or ""):
        toks = seg.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in _PREFIX or "=" in t or t.startswith("-"):
                i += 1
                continue
            if t == "timeout":
                i += 2 if i + 1 < len(toks) and re.fullmatch(r"[\d.]+[smh]?", toks[i + 1]) else 1
                continue
            break
        if i < len(toks):
            p = toks[i].rsplit("/", 1)[-1]
            if p not in _SKIP_PROG:
                return p
    return ""


def key_for(tool: str, args: Optional[dict]) -> str:
    if tool == "shell":
        return "shell:" + program(str((args or {}).get("command") or ""))
    return tool


class Breaker:
    def __init__(self, limit: Optional[int] = None):
        self.limit = limit or int(os.environ.get("AIW_BREAKER_LIMIT", "2") or 2)
        self.counts: dict[tuple, int] = {}
        self.tripped: dict[str, dict] = {}   # key -> {tool, error, alt}
        self.off = os.environ.get("AIW_TOOL_BREAKER", "1") == "0"

    def feed(self, tool: str, args: Optional[dict], ok: bool, error: str = "") -> Optional[str]:
        """After a call: a note for the model when this failure tripped the breaker, else None."""
        if self.off or ok or not error or tool in ("done", "fail", "ask_user", "request_takeover"):
            return None
        if str(error).startswith(("user declined", "forbidden by policy", "missing args", "REFUSED")):
            return None   # a policy/user decision is not a broken tool
        k = key_for(tool, args)
        if k in ("shell:", "shell"):
            k = "shell:?" + signature(error)[:40]
        sig = signature(error)
        n = self.counts[(k, sig)] = self.counts.get((k, sig), 0) + 1
        if n < self.limit or k in self.tripped:
            return None
        alt = BOS_ALT if tool.startswith("bos_") else ALTERNATIVE.get(tool, "换一个工具或方法")
        if k.startswith("shell:") and not k.startswith("shell:?"):
            alt = f"不要再用 {k[6:]}；" + alt
        self.tripped[k] = {"tool": tool, "error": str(error)[:200], "alt": alt}
        what = f"shell 里的 {k[6:]}" if k.startswith("shell:") and not k.startswith("shell:?") else tool
        return (f"TOOL DISABLED for this task: {what} failed twice with the same error ({str(error)[:160]}). "
                f"Do not call it again. Use instead: {alt}")

    def blocked(self, tool: str, args: Optional[dict]) -> Optional[str]:
        """Refusal text when this call goes to a tripped tool."""
        if self.off:
            return None
        k = key_for(tool, args)
        t = self.tripped.get(k) or (self.tripped.get("bos") if tool.startswith("bos_") else None)
        if t is None and tool.startswith("bos_"):
            t = next((v for kk, v in self.tripped.items() if kk.startswith("bos_")), None)
        if t is None:
            return None
        return (f"REFUSED: {tool} is switched off for this task (failed twice: {t['error'][:120]}). Use instead: {t['alt']}")

    def hidden(self) -> set:
        """Whole tools to hide from the model (shell is never hidden, only its tripped programs refused)."""
        out = {v["tool"] for k, v in self.tripped.items() if not k.startswith("shell")}
        if any(t.startswith("bos_") for t in out):
            from tool_router.registry import BOS_TOOLS
            out |= set(BOS_TOOLS)
        return out
