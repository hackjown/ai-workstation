"""v3.9.17 ssh_exec: run commands over SSH from the API with paramiko, one connection reused per task.

task-833 rewrote a full paramiko script and reconnected on every step (14 connections, 80-200 s each, one timed out
with empty stdout). ssh_exec instead:
  * connects once per (host, user, port) and reuses it for the whole task — later calls skip the ~1-3 s handshake;
  * takes the password from the vault (passwords the user wrote were stored there, see sessions.protect), never from
    the model, so no password is typed into a script and the model never sees it;
  * runs one command string (may chain with ; or &&), or several `commands`, or a local `script` uploaded to the
    host and run (no multi-level quoting); `container` wraps each command in `docker exec` for you;
  * per-command timeout default 60 s (`timeout` to change): on timeout it returns the output gathered so far with
    timed_out=true, never an empty result.

The command string the model writes uses no password; the policy/breaker judge it by what it does on the host. The
tool is added to a task only when it has a credential or the user authorised the host (sessions.py).

Connection management is live; the parsing/formatting helpers are pure and tested in tests/test_v3917_ssh_exec.py.
"""
from __future__ import annotations

import asyncio
import io
import shlex
import time
from typing import Any, Optional

MAX_OUT = 12000


def wrap(cmd: str, container: str = "", user: str = "", workdir: str = "") -> str:
    """One command, optionally run inside a docker container (no hand-rolled quoting)."""
    cmd = cmd or "true"
    if not container:
        return cmd
    flags = "-i"
    if user:
        flags += f" -u {shlex.quote(user)}"
    if workdir:
        flags += f" -w {shlex.quote(workdir)}"
    return f"docker exec {flags} {shlex.quote(container)} sh -lc {shlex.quote(cmd)}"


def clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit // 2] + f"\n…（省略 {len(text) - limit} 字）…\n" + text[-limit // 2:]


def build_commands(a: dict) -> list[str]:
    """The ordered command strings a call runs on the host (before docker wrapping)."""
    out = []
    if a.get("command"):
        out.append(str(a["command"]))
    for c in a.get("commands") or []:
        if str(c).strip():
            out.append(str(c))
    return out


def judge_call(a: dict):
    from tool_router import remote as _rm
    cmds = build_commands(a)
    if a.get("script"):
        cmds.append(str(a["script"]))
    if not cmds:
        return False
    worst = False
    for raw in cmds:
        if _rm.WRITE.search(raw):
            return "always"
        if not _rm.read_only(raw):
            worst = "once"
    return worst


class SSHPool:
    """One paramiko client per (host, port, user), reused for a task's lifetime."""

    def __init__(self):
        self.conns: dict[tuple, Any] = {}

    def _key(self, host: str, port: int, user: str) -> tuple:
        return (host, int(port or 22), user or "root")

    def _connect(self, host: str, port: int, user: str, password: Optional[str], timeout: float):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(hostname=host, port=int(port or 22), username=user or "root", password=password or None,
                  look_for_keys=not password, allow_agent=not password, timeout=timeout, banner_timeout=timeout,
                  auth_timeout=timeout)
        return c

    async def client(self, host: str, port: int, user: str, password: Optional[str], timeout: float = 15):
        key = self._key(host, port, user)
        c = self.conns.get(key)
        if c is not None:
            tr = c.get_transport()
            if tr is not None and tr.is_active():
                return c, False
            self.conns.pop(key, None)
        c = await asyncio.to_thread(self._connect, host, port, user, password, timeout)
        self.conns[key] = c
        return c, True

    def _run_sync(self, c, cmd: str, timeout: float) -> dict:
        import socket
        tr = c.get_transport()
        chan = tr.open_session(timeout=max(5.0, timeout))
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        out, err = bytearray(), bytearray()
        t0 = time.monotonic()
        timed = False
        while True:
            while chan.recv_ready():
                out += chan.recv(65536)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            if chan.exit_status_ready():
                break
            if time.monotonic() - t0 > timeout:
                timed = True
                break
            try:
                chan.settimeout(0.3)
                try:
                    b = chan.recv(65536)
                    if b:
                        out += b
                    elif not b:
                        if chan.exit_status_ready():
                            break
                except socket.timeout:
                    pass
            except Exception:  # noqa: BLE001
                break
        # one last drain
        try:
            while chan.recv_ready():
                out += chan.recv(65536)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
        except Exception:  # noqa: BLE001
            pass
        code = chan.exit_status if chan.exit_status_ready() else None
        if timed:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
        return {"exit_code": code, "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace"),
                "timed_out": timed}

    async def run(self, c, cmd: str, timeout: float) -> dict:
        return await asyncio.to_thread(self._run_sync, c, cmd, timeout)

    async def put(self, c, remote_path: str, content: bytes) -> None:
        def _put():
            sftp = c.open_sftp()
            try:
                sftp.putfo(io.BytesIO(content), remote_path)
            finally:
                sftp.close()
        await asyncio.to_thread(_put)

    def close(self):
        for c in self.conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.conns.clear()


def schema(creds: list[dict]) -> dict:
    who = "; ".join(f"{c.get('user') or 'root'}@{c.get('host') or '?'}" for c in creds if c.get("host")) or "任务里给出的主机"
    return {"name": "ssh_exec", "description": (
        "Run shell commands on a remote host over SSH (paramiko). The password is taken from the vault automatically "
        "— never put a password, sshpass or a paramiko script in the command. The connection is reused for the whole "
        "task, so do NOT rewrite a connect script each step. Known hosts: " + who + ". "
        "Give `host` (and `user`/`port` if not the default), then one of: `command` (a string, may chain with ; or &&), "
        "`commands` (a list, run in order, far cheaper than reconnecting), or `script` (local text run on the host). "
        "`container` runs each command inside that docker container (docker exec). Read-only diagnosis (ps/top/docker "
        "stats/inspect/logs/cat /proc) runs without asking. Per-command timeout default 60 s (`timeout` to change); on "
        "timeout you get the output so far with timed_out=true. Returns exit_code, stdout, stderr per command."),
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string"}, "user": {"type": "string"}, "port": {"type": "integer"},
            "command": {"type": "string"}, "commands": {"type": "array", "items": {"type": "string"}},
            "script": {"type": "string", "description": "a script's text; uploaded to the host and run"},
            "interpreter": {"type": "string", "description": "how to run `script`, default sh (e.g. bash, python3)"},
            "container": {"type": "string", "description": "a docker container name: each command runs via docker exec"},
            "docker_user": {"type": "string"}, "workdir": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds per command, default 60"}},
            "required": ["host"]}}


def tool(store, secrets, pool: SSHPool, creds: list[dict], notifier=None, task=None):
    """(schema, async call). creds: [{key, host, user}] from sessions.task_creds (+ 运维主机)."""
    def password_for(host: str, user: str) -> Optional[str]:
        host = (host or "").lower()
        for c in creds:
            if (c.get("host") or "").lower() == host and (not user or (c.get("user") or "root") == user):
                return secrets.get(c["key"]) if secrets else None
        for c in creds:   # host match alone
            if (c.get("host") or "").lower() == host:
                return secrets.get(c["key"]) if secrets else None
        return None

    async def call(a: dict) -> dict:
        host = (a.get("host") or "").strip()
        if not host:
            # one known host: default to it
            hs = [c.get("host") for c in creds if c.get("host")]
            host = hs[0] if len(set(hs)) == 1 else ""
        if not host:
            raise ValueError("要给出 host（已知主机：" + ", ".join(sorted({c.get("host") for c in creds if c.get("host")})) + "）")
        user = (a.get("user") or next((c.get("user") for c in creds if (c.get("host") or "").lower() == host.lower() and c.get("user")), "") or "root")
        port = int(a.get("port") or 22)
        pw = password_for(host, user)
        per = max(5, min(int(a.get("timeout") or 60), 1800))
        c, fresh = await pool.client(host, port, user, pw, timeout=15)
        cmds = []
        if a.get("script"):
            path = f"/tmp/aiw_{int(time.time() * 1000)}.scr"
            await pool.put(c, path, str(a["script"]).encode("utf-8"))
            interp = a.get("interpreter") or "sh"
            cmds.append(f"{interp} {shlex.quote(path)}; rm -f {shlex.quote(path)}")
        cmds += build_commands(a)
        if not cmds:
            cmds = ["true"]
        results = []
        for raw in cmds:
            cmd = wrap(raw, a.get("container") or "", a.get("docker_user") or "", a.get("workdir") or "")
            r = await pool.run(c, cmd, per)
            r["command"] = raw[:200]
            r["stdout"] = clip(r.get("stdout") or "")
            r["stderr"] = clip(r.get("stderr") or "", 3000)
            results.append(r)
            if r.get("timed_out"):
                break
        head = {"host": host, "user": user, "reused": not fresh}
        if len(results) == 1:
            return {**head, **results[0]}
        return {**head, "results": results}

    def confirm_needed(a: dict):
        """False (read-only: no card) | 'once' (unclear: asked once per host per task) | 'always' (changes the host)."""
        return judge_call(a)

    call.needs_confirm = confirm_needed
    call.confirm_reason = "在远程主机上通过 SSH 执行命令"
    call.confirm_scope = lambda a: f"remote_diag:{(a.get('host') or '').lower()}"
    call.max_result = MAX_OUT + 4000
    return schema(creds), call
