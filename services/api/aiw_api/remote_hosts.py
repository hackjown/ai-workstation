"""运维主机: SSH hosts the user explicitly authorised the agent to run commands on (设置 › 电脑 › 运维主机).

The VM shell refuses anything that touches Proxmox (L4) and asks for every `ssh` (L3), so an ops task such as
"PVE 主机 192.168.50.200 你来执行" used to dead-end. Here the user adds the host once; its password is kept
encrypted on the API side (secret "remote_host_<id>") and never reaches the VM or the model. The agent calls the
`remote_exec` tool with the host's name and a command; the API runs it over SSH and returns the output.

Each host has a mode:
  confirm  (default) every command shows the user's 允许/拒绝 card
  auto     read-only-looking commands run without asking; anything that changes the host still asks
Commands that can destroy data (mkfs, dd to a disk, rm -rf /, qm/pct destroy, pvesm remove, …) always ask.
"""
from __future__ import annotations

import re
import secrets as _rnd
import shlex
from typing import Any, Optional

KV = "remote_hosts"
MAX_OUT = 8000
FIELDS = ("name", "host", "port", "user", "sudo", "mode", "note")

ALWAYS_ASK = re.compile(
    r"\bmkfs|\bdd\s+[^|;&]*of=/dev/|>\s*/dev/(sd|nvme|vd|hd)|\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$|\*)|"
    r"\b(qm|pct)\s+(destroy|stop|shutdown|reset|rollback|delsnapshot)\b|\bpvesm\s+(remove|free)\b|"
    r"\bpvecm\s+(delnode|expected)\b|\bzpool\s+(destroy|labelclear)|\blvremove\b|\bvgremove\b|\bwipefs\b|"
    r"\b(shutdown|poweroff|halt|reboot)\b|\bsgdisk\s+-[zZ]|\bfdisk\b|\bparted\b", re.I)
# a command made only of these words reads the host and changes nothing
_READ = re.compile(
    r"^\s*(cat|head|tail|less|grep|egrep|zgrep|awk|sed\s+-n|ls|ll|df|du|free|uptime|uname|hostname|hostnamectl|ip\s+(a|addr|r|route|link|-br)|"
    r"ss|netstat|ps|top\s+-bn1|lsblk|blkid|findmnt|mount$|lscpu|lspci|lsusb|dmidecode|sensors|smartctl\s+-[aAiHx]|nvme\s+(list|smart-log)|"
    r"pveversion|pvesh\s+get|qm\s+(list|status|config|pending)|pct\s+(list|status|config)|pvesm\s+(status|list)|pvecm\s+status|"
    r"journalctl|dmesg|systemctl\s+(status|is-active|is-enabled|list-units|list-timers)|docker\s+(ps|images|info|version|inspect|logs|stats\s+--no-stream)|"
    r"docker\s+compose\s+(ps|ls|config|logs|version)|cat|wc|sort|uniq|cut|tr|date|whoami|id|env|printenv|which|type|command\s+-v|"
    r"echo|printf|true|test|\[|nproc|getconf|timedatectl|zpool\s+(status|list)|zfs\s+list|lvs|vgs|pvs|cut|ping\s+-c\s*\d+|curl\s+-[sSIkm]+|"
    r"nc\s+-z|timeout\s+\d+\s+(ping|nc|curl)|stat|file|md5sum|sha256sum|find\s+\S+(\s+-(name|type|maxdepth|mtime|size)\s+\S+)*)\b", re.I)


def hosts(store) -> list[dict]:
    return [dict(h) for h in (store.kv_get(KV, []) or [])]


def public(store, secrets) -> list[dict]:
    return [{**h, "has_password": bool(secrets.get(_secret(h["id"])))} for h in hosts(store)]


def _secret(hid: str) -> str:
    return f"remote_host_{hid}"


def _clean(body: dict, old: Optional[dict] = None) -> dict:
    h = dict(old or {"port": 22, "user": "root", "sudo": False, "mode": "confirm", "note": ""})
    for k in FIELDS:
        if k in body and body[k] is not None:
            v = body[k]
            h[k] = int(v or 22) if k == "port" else bool(v) if k == "sudo" else str(v).strip()
    if h.get("mode") not in ("confirm", "auto"):
        h["mode"] = "confirm"
    if not h.get("host"):
        raise ValueError("要填主机地址")
    if not re.match(r"^[\w.\-:\[\]]+$", h["host"]):
        raise ValueError("主机地址格式不对")
    h["name"] = (h.get("name") or h["host"]).strip()[:40]
    return h


def save(store, secrets, body: dict, hid: Optional[str] = None) -> dict:
    lst = hosts(store)
    old = next((h for h in lst if h["id"] == hid), None) if hid else None
    h = _clean(body, old)
    if any(x["name"] == h["name"] and x.get("id") != (old or {}).get("id") for x in lst):
        raise ValueError(f"已经有叫「{h['name']}」的主机了")
    if old:
        h["id"] = old["id"]
        lst = [h if x["id"] == old["id"] else x for x in lst]
    else:
        h["id"] = _rnd.token_hex(4)
        lst.append(h)
    store.kv_set(KV, lst)
    if body.get("password"):
        secrets.put(_secret(h["id"]), str(body["password"]))
    return {**h, "has_password": bool(secrets.get(_secret(h["id"])))}


def remove(store, secrets, hid: str) -> None:
    store.kv_set(KV, [h for h in hosts(store) if h["id"] != hid])
    try:
        secrets.delete(_secret(hid))
    except Exception:  # noqa: BLE001
        pass


def find(store, key: str) -> Optional[dict]:
    key = (key or "").strip()
    for h in hosts(store):
        if key in (h["id"], h["name"], h["host"]):
            return h
    return None


def needs_confirm(h: Optional[dict], cmd: str) -> bool:
    if not h or h.get("mode") != "auto" or ALWAYS_ASK.search(cmd or ""):
        return True
    # every piece of a pipeline / && chain must be a read
    parts = [p for p in re.split(r"\|\||&&|;|\|", cmd or "") if p.strip()]
    if not parts or re.search(r"(^|[^2&])>\s*[^&/]|>>|\btee\b|`|\$\(", cmd):
        return True
    return not all(_READ.match(p) for p in parts)


async def _connect(h: dict, password: Optional[str]):
    import asyncio
    import asyncssh
    return await asyncio.wait_for(asyncssh.connect(
        h["host"], port=int(h.get("port") or 22), username=h.get("user") or "root", password=password or None,
        known_hosts=None, client_keys=None if password else (), connect_timeout=15), 20)


def _wrap(h: dict, cmd: str) -> str:
    if h.get("sudo") and (h.get("user") or "root") != "root":
        return f"sudo -S -p '' sh -c {shlex.quote(cmd)}"
    return cmd


async def run(h: dict, password: Optional[str], cmd: str, timeout: int = 120) -> dict:
    async with await _connect(h, password) as conn:
        r = await conn.run(_wrap(h, cmd), input=(password + "\n") if h.get("sudo") and password else None,
                           timeout=max(5, min(int(timeout or 120), 3600)))
    out, err = r.stdout or "", r.stderr or ""
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT // 2] + f"\n…（中间省略 {len(out) - MAX_OUT} 字）…\n" + out[-MAX_OUT // 2:]
    return {"host": h["name"], "exit_code": r.exit_status, "stdout": out, "stderr": err[-3000:]}


async def test(h: dict, password: Optional[str]) -> dict:
    r = await run(h, password, "hostname; uname -srm; (pveversion 2>/dev/null || true); (docker --version 2>/dev/null || true)", 20)
    if r["exit_code"] != 0:
        raise ValueError((r["stderr"] or r["stdout"] or "命令失败")[:300])
    return {"output": r["stdout"].strip()}


def tool(store, secrets) -> tuple[dict, Any]:
    names = [f"{h['name']} ({h.get('user') or 'root'}@{h['host']}{'，免确认只读' if h.get('mode') == 'auto' else ''})"
             for h in hosts(store)]
    schema = {"name": "remote_exec", "description": (
        "Run a shell command over SSH on a host the user authorised in 设置 › 电脑 › 运维主机 (you never see its "
        "password; do NOT ssh/sshpass/paramiko from this computer's shell for these hosts, use this tool). "
        "Authorised hosts: " + "; ".join(names) + ". Prefer read-only checks first; the user is asked to allow "
        "commands that change the host, and destructive ones (mkfs, dd, qm/pct destroy/stop, reboot …) always. "
        "Returns exit_code, stdout, stderr. One command string may chain steps with && or ;."),
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "host name (or address) from the list above"},
            "command": {"type": "string", "description": "shell command to run on that host"},
            "timeout": {"type": "integer", "description": "seconds, default 120"}},
            "required": ["host", "command"]}}

    async def call(a: dict) -> dict:
        h = find(store, a.get("host"))
        if not h:
            raise ValueError(f"没有授权的主机「{a.get('host')}」；可用：{', '.join(x['name'] for x in hosts(store))}")
        return await run(h, secrets.get(_secret(h["id"])), a.get("command") or "true", a.get("timeout") or 120)

    def confirm_needed(a: dict) -> bool:
        return needs_confirm(find(store, a.get("host")), a.get("command") or "")

    call.needs_confirm = confirm_needed            # read by the agent loop instead of its name-based rule
    call.confirm_reason = "在授权的运维主机上执行命令"
    call.max_result = MAX_OUT + 3500
    return schema, call
