"""shell.exec - runs inside the VM as the unprivileged agent user.

Policy (L-levels) is enforced upstream by tool-router; this layer only adds a
hard timeout and output cap so a runaway command can't wedge the agent.
"""
from __future__ import annotations

import asyncio
import os

MAX_OUTPUT = 64 * 1024


async def run(command: str, cwd: str | None = None, timeout: float = 60.0) -> dict:
    cwd = os.path.expanduser(cwd or os.environ.get("VM_AGENT_WORKDIR", "~"))
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        code = proc.returncode
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        return {"exit_code": 124, "stdout": "", "stderr": f"timeout after {timeout}s", "timed_out": True}
    return {
        "exit_code": code,
        "stdout": out.decode(errors="replace")[-MAX_OUTPUT:],
        "stderr": err.decode(errors="replace")[-MAX_OUTPUT:],
        "timed_out": False,
    }
