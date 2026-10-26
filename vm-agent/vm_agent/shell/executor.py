"""shell.exec - runs inside the VM as the unprivileged agent user.

Policy (L-levels) is enforced upstream by tool-router; this layer only adds a
hard timeout and output cap so a runaway command can't wedge the agent.
"""
from __future__ import annotations

import asyncio
import os

MAX_OUTPUT = 64 * 1024
_RUNNING: set = set()  # process-group ids of commands still running (killed by shell.kill when a task is stopped)


def _env() -> dict:
    """User-installed CLIs (~/.local/bin: officecli, edge-tts, pdf2docx...) were not on PATH (task-4e6 lost two steps)."""
    env = dict(os.environ)
    home = os.path.expanduser("~")
    extra = [p for p in (f"{home}/.local/bin", f"{home}/bin", "/usr/local/bin") if p not in env.get("PATH", "").split(":")]
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    return env


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass


async def kill_all() -> dict:
    pids = list(_RUNNING)
    for pid in pids:
        _kill(pid)
    _RUNNING.clear()
    return {"killed": len(pids)}


async def run(command: str, cwd: str | None = None, timeout: float = 60.0) -> dict:
    cwd = os.path.expanduser(cwd or os.environ.get("VM_AGENT_WORKDIR", "~"))
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, env=_env())
    _RUNNING.add(proc.pid)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        code = proc.returncode
    except asyncio.TimeoutError:
        _kill(proc.pid)
        return {"exit_code": 124, "stdout": "", "stderr": f"timeout after {timeout}s", "timed_out": True}
    except asyncio.CancelledError:
        _kill(proc.pid)
        raise
    finally:
        _RUNNING.discard(proc.pid)
    return {
        "exit_code": code,
        "stdout": out.decode(errors="replace")[-MAX_OUTPUT:],
        "stderr": err.decode(errors="replace")[-MAX_OUTPUT:],
        "timed_out": False,
    }
