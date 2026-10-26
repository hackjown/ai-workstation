"""Deploy server: where the 开发 skill installs what it built, once the user has confirmed.

It is separate from the online-update host so it can be changed at any time (设置 › 电脑 › 部署服务器).
The SSH password stays here on the API side (encrypted secret "deploy_ssh_password") and never reaches the VM:
the agent calls the `deploy_server` tool, the API packs the project on the VM into a tar, carries it over and
runs the command on the server itself. Every call goes through the user's confirm card (see loop._RISKY_EXT).
"""
from __future__ import annotations

import base64
import shlex
from typing import Any, Optional

SECRET = "deploy_ssh_password"
DEFAULT = {"host": "", "port": 22, "user": "root", "dir": "~/apps", "sudo": False, "name": ""}
MAX_OUT = 6000


def cfg(store) -> dict:
    c = dict(DEFAULT)
    c.update({k: v for k, v in (store.kv_get("deploy_cfg", {}) or {}).items() if k in DEFAULT})
    return c


def save(store, secrets, body: dict) -> dict:
    c = cfg(store)
    for k in DEFAULT:
        if k in body:
            c[k] = int(body[k] or 22) if k == "port" else bool(body[k]) if k == "sudo" else str(body[k]).strip()
    store.kv_set("deploy_cfg", c)
    if body.get("password"):
        secrets.put(SECRET, str(body["password"]))
    return {"config": c, "has_password": bool(secrets.get(SECRET))}


async def _connect(c: dict, password: Optional[str]):
    import asyncio
    import asyncssh
    if not c.get("host"):
        raise ValueError("还没有填部署服务器（设置 › 电脑 › 部署服务器）")
    return await asyncio.wait_for(asyncssh.connect(
        c["host"], port=int(c.get("port") or 22), username=c.get("user") or "root", password=password or None,
        known_hosts=None, client_keys=None if password else (), connect_timeout=15), 20)


async def _home_dir(conn, d: str) -> str:
    d = (d or "~/apps").strip()
    if d.startswith("~"):
        r = await conn.run("echo $HOME", timeout=10)
        d = ((r.stdout or "").strip() or "/root") + d[1:]
    return d.rstrip("/") or "/"


def _wrap(c: dict, cmd: str) -> str:
    return f"sudo -S -p '' sh -c {shlex.quote(cmd)}" if c.get("sudo") else cmd


async def test(c: dict, password: Optional[str]) -> dict:
    async with await _connect(c, password) as conn:
        d = await _home_dir(conn, c.get("dir"))
        r = await conn.run(f"mkdir -p {shlex.quote(d)} && cd {shlex.quote(d)} && pwd && uname -sm && "
                           "(docker --version 2>/dev/null || echo 'no docker')", timeout=20)
        if r.exit_status != 0:
            raise ValueError((r.stderr or r.stdout or "命令失败")[:300])
        out = (r.stdout or "").strip().splitlines()
        return {"dir": out[0] if out else d, "system": out[1] if len(out) > 1 else "", "docker": out[-1] if len(out) > 2 else ""}


def tool(store, secrets, computer) -> tuple[dict, Any]:
    """The agent's deploy_server tool for this task, or raises when no server is set up."""
    schema = {"name": "deploy_server", "description": (
        "Install or update what you built on the user's deploy server (an SSH host the user configured; you never "
        "see its password). Only after the user confirmed the design and the tests pass. `upload`: a file or folder "
        "under ~/workspace on this computer (e.g. 'myapp'); it is copied to <deploy dir>/<name> on the server "
        "(node_modules, .git, __pycache__, .venv are skipped). `command`: a shell command run on the server in "
        "that folder (e.g. 'docker compose up -d --build' or 'bash install.sh'). Returns exit code and output. "
        "Each call asks the user to allow it."),
        "parameters": {"type": "object", "properties": {
            "upload": {"type": "string", "description": "path under ~/workspace to copy first (optional)"},
            "name": {"type": "string", "description": "folder name on the server, default = the upload's name"},
            "command": {"type": "string", "description": "command to run on the server in that folder"},
            "timeout": {"type": "integer", "description": "seconds, default 600"}},
            "required": ["command"]}}

    async def call(a: dict) -> dict:
        c = cfg(store)
        pw = secrets.get(SECRET)
        up = (a.get("upload") or "").strip().strip("/")
        if up.startswith("~/workspace/"):
            up = up[len("~/workspace/"):]
        name = (a.get("name") or (up.split("/")[-1] if up else "") or c.get("name") or "app").strip()
        name = "".join(ch for ch in name if ch.isalnum() or ch in "-_.") or "app"
        data = None
        if up:
            tar = ".aiw-deploy.tgz"
            r = await computer.execute("shell.exec", {"command": (
                f"cd ~/workspace && rm -f {tar} && tar czf {tar} --exclude=node_modules --exclude=.git "
                f"--exclude=__pycache__ --exclude=.venv -C {shlex.quote(up.rsplit('/', 1)[0] if '/' in up else '.')} "
                f"{shlex.quote(up.rsplit('/', 1)[-1])} && du -k {tar} | cut -f1"), "timeout": 300}, timeout=320)
            if not r.success or (r.result or {}).get("exit_code") not in (0, None):
                raise ValueError(f"打包 {up} 失败：{((r.result or {}).get('stderr') or r.error or '')[:300]}")
            f = await computer.execute("files.read", {"path": tar, "binary": True}, timeout=300)
            if not f.success or not (f.result or {}).get("content_b64"):
                raise ValueError(f"读取打包文件失败：{f.error}")
            data = base64.b64decode(f.result["content_b64"])
            if (f.result.get("size") or 0) > len(data):
                raise ValueError("项目超过 40MB，先删掉构建产物或大文件再部署")
            await computer.execute("shell.exec", {"command": f"rm -f ~/workspace/{tar}", "timeout": 30}, timeout=45)
        async with await _connect(c, pw) as conn:
            base = await _home_dir(conn, c.get("dir"))
            dest = f"{base}/{name}"
            q = shlex.quote
            if data is not None:
                src = up.rsplit("/", 1)[-1]
                async with conn.start_sftp_client() as sftp:
                    await conn.run(f"mkdir -p {q(base)}", check=True, timeout=20)
                    async with sftp.open(f"{base}/.aiw-upload.tgz", "wb") as fh:
                        await fh.write(data)
                # unpack into a temp folder, then rename into place so a rerun replaces the code but keeps .env/data
                unpack = (f"cd {q(base)} && rm -rf .aiw-new && mkdir .aiw-new && tar xzf .aiw-upload.tgz -C .aiw-new && "
                          f"mkdir -p {q(name)} && cp -a .aiw-new/{q(src)}/. {q(name)}/ 2>/dev/null || "
                          f"cp -a .aiw-new/{q(src)} {q(name)}; rm -rf .aiw-new .aiw-upload.tgz")
                r = await conn.run(_wrap(c, unpack), input=(pw + "\n") if c.get("sudo") and pw else None, timeout=120)
                if r.exit_status != 0:
                    raise ValueError(f"解包失败：{(r.stderr or '')[:300]}")
            cmd = f"mkdir -p {q(dest)} && cd {q(dest)} && {a.get('command') or 'ls -la'}"
            r = await conn.run(_wrap(c, cmd), input=(pw + "\n") if c.get("sudo") and pw else None,
                               timeout=int(a.get("timeout") or 600))
        out, err = r.stdout or "", r.stderr or ""
        return {"server": f"{c.get('user')}@{c.get('host')}", "dir": dest, "uploaded_bytes": len(data) if data else 0,
                "exit_code": r.exit_status, "stdout": out[-MAX_OUT:], "stderr": err[-2000:]}

    return schema, call
