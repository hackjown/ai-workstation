"""deploy_server end to end against an in-process SSH server (asyncssh) that runs commands locally."""
import asyncio
import base64
import io
import os
import tarfile
from types import SimpleNamespace

import pytest

asyncssh = pytest.importorskip("asyncssh")


class _Store:
    def __init__(self): self.kv = {}
    def kv_get(self, k, d=None): return self.kv.get(k, d)
    def kv_set(self, k, v): self.kv[k] = v


class _Secrets:
    def __init__(self): self.d = {}
    def get(self, k): return self.d.get(k)
    def put(self, k, v): self.d[k] = v


class _Computer:
    """Plays the VM: shell.exec runs locally with ~/workspace mapped to a temp folder."""
    def __init__(self, ws): self.ws = ws
    async def execute(self, action, payload, observe=False, timeout=30):
        if action == "shell.exec":
            cmd = payload["command"].replace("~/workspace", self.ws)
            p = await asyncio.create_subprocess_shell(cmd, stdout=-1, stderr=-1)
            o, e = await p.communicate()
            return SimpleNamespace(success=True, error=None, result={"exit_code": p.returncode, "stdout": o.decode(), "stderr": e.decode()})
        if action == "files.read":
            data = open(os.path.join(self.ws, payload["path"]), "rb").read()
            return SimpleNamespace(success=True, error=None, result={"content_b64": base64.b64encode(data).decode(), "size": len(data)})
        raise AssertionError(action)


async def _handle(process):
    p = await asyncio.create_subprocess_shell(process.command, stdin=-1, stdout=-1, stderr=-1)
    o, e = await p.communicate()
    process.stdout.write(o.decode()); process.stderr.write(e.decode())
    process.exit(p.returncode)


class _Auth(asyncssh.SSHServer):
    def begin_auth(self, username): return True
    def password_auth_supported(self): return True
    def validate_password(self, username, password): return password == "pw"


def test_deploy_uploads_project_and_runs_command(tmp_path):
    from aiw_api import deployer
    ws, srv = tmp_path / "ws", tmp_path / "srv"
    (ws / "myapp" / "node_modules").mkdir(parents=True); srv.mkdir()
    (ws / "myapp" / "index.html").write_text("<h1>hi</h1>")
    (ws / "myapp" / "node_modules" / "big.js").write_text("x")

    async def run():
        key = asyncssh.generate_private_key("ssh-ed25519")
        server = await asyncssh.create_server(_Auth, "127.0.0.1", 0, server_host_keys=[key],
                                              process_factory=_handle, sftp_factory=True)
        port = server.sockets[0].getsockname()[1]
        store, sec = _Store(), _Secrets()
        deployer.save(store, sec, {"host": "127.0.0.1", "port": port, "user": "u", "dir": str(srv), "password": "pw"})
        assert (await deployer.test(deployer.cfg(store), "pw"))["dir"] == str(srv)
        _, call = deployer.tool(store, sec, _Computer(str(ws)))
        r = await call({"upload": "myapp", "command": "cat index.html && ls"})
        assert r["exit_code"] == 0 and "<h1>hi</h1>" in r["stdout"] and "node_modules" not in r["stdout"]
        (ws / "myapp" / "index.html").write_text("<h1>v2</h1>")
        (srv / "myapp" / ".env").write_text("KEEP=1")
        r = await call({"upload": "~/workspace/myapp", "command": "cat index.html .env"})
        assert "<h1>v2</h1>" in r["stdout"] and "KEEP=1" in r["stdout"]
        server.close()
    asyncio.run(run())
