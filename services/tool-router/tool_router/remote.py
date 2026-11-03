"""v3.9.17 确认去重: remote diagnosis from the computer's shell.

task-833 asked 9 times in 14 steps ("shell: remote login", "shell touches sensitive file") for read-only checks
(ps / top / docker stats / inspect / logs / cat /proc) because a heredoc'd paramiko script had `password=` in it.
Now a shell command that reaches another host (ssh, sshpass, a paramiko/asyncssh script) is judged by what it runs
there:
  * clearly read-only           -> L2 (runs, audited, no card)
  * changes the host            -> L3, asks every time
  * cannot tell (interactive …) -> L3 once per host per task: scope "remote_diag:<host>", the card says
                                   「本任务内允许对该主机执行只读诊断」 and later calls with the same scope pass.
Pure helpers, tested in tests/test_v3917_confirm_dedupe.py.
"""
from __future__ import annotations

import re
from typing import Optional

REMOTE = re.compile(r"(?<!\.)\bssh\b(?![-_.]\w)|\bsshpass\b|\b(paramiko|asyncssh)\b[\s\S]*?\b(connect|SSHClient)\b|\bfabric\b[\s\S]*?\bConnection\b", re.I)
_HOST = re.compile(r"""(?:@|HOST\s*=\s*["']|hostname\s*=\s*["']|connect\(\s*["'])((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9-]+(?:\.[a-z0-9-]+)+)""", re.I)
_IP = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")

# changes on the remote side (also inside docker exec / a paramiko command list)
WRITE = re.compile(
    r"\b(rm|rmdir|mv|cp|kill|pkill|killall|reboot|shutdown|poweroff|halt|chmod|chown|chattr|mkfs|dd|truncate|tee|"
    r"useradd|userdel|passwd|chpasswd|crontab|mount|umount|swapoff|swapon|sysctl\s+-w|iptables|nft|ufw|"
    r"apt|apt-get|yum|dnf|apk|pip3?|npm|wget|git\s+(pull|push|reset|checkout|clean))\b|"
    r"\bdocker\s+(rm|rmi|stop|kill|restart|start|run|create|update|pause|unpause|rename|cp|commit|load|import|pull|push|"
    r"prune|system\s+prune|volume\s+(rm|prune|create)|network\s+(rm|prune|create|connect|disconnect)|image\s+(rm|prune))\b|"
    r"\bdocker[\s-]compose\s+(up|down|restart|rm|stop|start|pull|build|kill|create)\b|"
    r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|kill|daemon-reload)\b|\bservice\s+\S+\s+(start|stop|restart)\b|"
    r"\bsed\s+-i\b|\bperl\s+-pi\b|\becho\b[^|;&\n]*>\s*/(?!dev/null)|(?<![<>&\d])>{1,2}\s*/(?!dev/null|tmp/)", re.I)
# the local part of the command that writes the script itself: cat > x.py <<'EOF'
_LOCAL_WRITE = re.compile(r"\bcat\s*>\s*[\w./~$-]+\s*<<-?\s*['\"]?\w+['\"]?|\b(?:2>&1|&>\s*/dev/null|>\s*/dev/null)")
# a paramiko-style list of remote commands or an ssh 'cmd' argument: something we can read
_HAS_CMDS = re.compile(r"exec_command|\bcmds?\s*=|commands\s*=|\bssh\b[^'\"\n]*\s['\"][^'\"]{2,}['\"]|\bsshpass\b[^\n]*\bssh\b[^\n]*['\"]", re.I)
_READS = re.compile(r"\b(ps|top|uptime|free|nproc|df|du|cat|head|tail|grep|awk|ls|stat|docker\s+(ps|stats|inspect|logs|top|info|images|version|system\s+df)|"
                    r"vmstat|iostat|mpstat|pidstat|sar|lscpu|lsblk|ss|netstat|journalctl|dmesg|uname|hostname|wc|sort|uniq|cut|"
                    r"systemctl\s+(status|is-active|list-units)|pgrep|find|which|date|id|whoami)\b", re.I)


def is_remote(cmd: str) -> bool:
    return bool(REMOTE.search(cmd or ""))


def target(cmd: str) -> str:
    m = _HOST.search(cmd or "")
    if m:
        return m.group(1).lower()
    m = _IP.search(cmd or "")
    return m.group(1) if m else ""


def judge(cmd: str) -> str:
    """'read' | 'write' | 'unsure' for a command that reaches another host."""
    c = _LOCAL_WRITE.sub(" ", cmd or "")
    c = re.sub(r"(?m)^\s*#.*$", " ", c)                    # comments
    c = re.sub(r"\bimport\s+[\w., ]+|\bfrom\s+\w+\s+import\s+[\w., *]+", " ", c)
    if WRITE.search(c):
        return "write"
    if _HAS_CMDS.search(c) and _READS.search(c):
        return "read"
    return "unsure"


def scope_for(cmd: str) -> Optional[str]:
    h = target(cmd)
    return f"remote_diag:{h or '?'}"


_FIRST = re.compile(r"^\s*(?:sudo\s+(?:-\S+\s+)*)?(?:timeout\s+\d+\s+)?(?:env\s+\S+=\S+\s+)*([\w./-]+)(?:\s+(\S+))?")
READ_FIRST = {"ps", "top", "htop", "uptime", "free", "nproc", "df", "du", "cat", "head", "tail", "grep", "egrep", "zgrep",
              "awk", "ls", "stat", "vmstat", "iostat", "mpstat", "pidstat", "sar", "lscpu", "lsblk", "ss", "netstat",
              "journalctl", "dmesg", "uname", "hostname", "wc", "sort", "uniq", "cut", "tr", "pgrep", "find", "which",
              "date", "id", "whoami", "echo", "printf", "true", "sleep", "cd", "test", "[", "xargs", "column", "sed",
              "readlink", "realpath", "getconf", "ip", "ping", "nslookup", "dig", "curl", "lsof", "pstree", "iotop",
              "env", "printenv", "file", "md5sum", "sha256sum", "basename", "dirname", "seq", "for", "do", "done",
              "while", "if", "then", "fi", "else", "time", "nice", "jq", "less", "more", "zcat", "strings", "base64"}
DOCKER_READ = {"ps", "stats", "inspect", "logs", "top", "info", "images", "version", "events", "diff", "port",
               "system", "network", "volume", "compose", "container", "image", "exec"}


def read_only(cmd: str) -> bool:
    """A plain command (as run on the host) that only reads: every piece starts with a read program, nothing writes.
    `docker exec <c> <read cmd>` counts as a read when the inner command is one."""
    c = cmd or ""
    if WRITE.search(c) or re.search(r"`|\$\(\s*(rm|kill|curl\s+-X\s*(POST|PUT|DELETE))", c):
        return False
    if re.search(r"\bcurl\b[^|;&]*\s-(X\s*(POST|PUT|DELETE|PATCH)|d\b|-data|F\b|o\b|O\b)", c):
        return False
    for part in re.split(r"\|\||&&|;|\||\n", c):
        if not part.strip():
            continue
        m = _FIRST.match(part)
        if not m:
            return False
        prog, sub = m.group(1).rsplit("/", 1)[-1], (m.group(2) or "")
        if prog == "docker" or prog == "docker-compose":
            if sub == "exec":   # judge what runs inside
                inner = re.sub(r"^\s*docker\s+exec\s+(?:-\S+\s+(?:\S+\s+)?)*?\S+\s+", "", part)
                inner = re.sub(r"^(sh|bash)\s+-l?c\s+", "", inner).strip("'\" ")
                if not inner or not read_only(inner):
                    return False
                continue
            if sub not in DOCKER_READ:
                return False
            if sub in ("system", "network", "volume", "container", "image", "compose") and not re.search(
                    r"\b(df|ls|inspect|ps|logs|top|config|version)\b", part):
                return False
            continue
        if prog in ("sed",) and re.search(r"\s-i", part):
            return False
        if prog not in READ_FIRST and not re.fullmatch(r"python3?", prog):
            return False
        if re.fullmatch(r"python3?", prog) and not re.search(r"-c\s+['\"]?(import (os|json|sys|psutil)|print)", part):
            return False
    return True
