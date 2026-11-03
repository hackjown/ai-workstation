"""v3.9.17 item 5 + 9(d): ssh_exec (reused paramiko connection, vault password, docker exec, partial output on
timeout) and the shell refusing hand-written SSH scripts when ssh_exec is there. No network: a fake client."""
import json
import time

from aiw_api import ssh_exec as SX

PW = "Pw" + "-" + "93kq!"


class FakeChan:
    def __init__(self, cmd, script):
        self.cmd = cmd
        self.out = list(script.get("out", []))
        self.done_after = script.get("done_after", 0.0)
        self.code = script.get("code", 0)
        self.t0 = time.monotonic()

    def settimeout(self, t):
        pass

    def exec_command(self, cmd):
        self.cmd = cmd

    def recv_ready(self):
        return bool(self.out) and (time.monotonic() - self.t0) >= 0

    def recv(self, n):
        if self.out:
            return self.out.pop(0).encode()
        import socket
        time.sleep(0.05)
        raise socket.timeout()

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return not self.out and (time.monotonic() - self.t0) >= self.done_after

    @property
    def exit_status(self):
        return self.code

    def close(self):
        pass


class FakeClient:
    def __init__(self, scripts):
        self.scripts, self.cmds, self.puts, self.closed = scripts, [], [], False

    def get_transport(self):
        me = self

        class T:
            def is_active(self):
                return not me.closed

            def open_session(self, timeout=None):
                class Ch(FakeChan):
                    def exec_command(ch, cmd):
                        me.cmds.append(cmd)
                        sc = next((v for k, v in me.scripts.items() if k in cmd), {"out": ["ok\\n"]})
                        FakeChan.__init__(ch, cmd, sc)
                return Ch("", {})
        return T()

    def open_sftp(self):
        me = self

        class S:
            def putfo(self, fo, path):
                me.puts.append((path, fo.read().decode()))

            def close(self):
                pass
        return S()

    def close(self):
        self.closed = True


class _Sec:
    def __init__(self, d):
        self.d = d

    def get(self, k):
        return self.d.get(k)


def _tool(scripts, monkeypatch):
    pool = SX.SSHPool()
    connects = []
    client = FakeClient(scripts)

    def fake_connect(host, port, user, password, timeout):
        connects.append((host, port, user, password))
        return client
    monkeypatch.setattr(pool, "_connect", fake_connect)
    creds = [{"key": "task_cred_t_1", "host": "192.168.50.188", "user": "root"}]
    schema, call = SX.tool(None, _Sec({"task_cred_t_1": PW}), pool, creds)
    return schema, call, pool, client, connects


async def test_batch_one_connection_reused_password_from_vault(monkeypatch):
    schema, call, pool, client, connects = _tool({"docker stats": {"out": ["clouddrive2 50%\\n"]}}, monkeypatch)
    assert "password" not in json.dumps(schema).lower() or "never put a password" in schema["description"]
    r = await call({"host": "192.168.50.188", "commands": ["uptime", "docker stats --no-stream", "cat /proc/loadavg"]})
    assert len(r["results"]) == 3 and "clouddrive2" in r["results"][1]["stdout"]
    r2 = await call({"host": "192.168.50.188", "command": "nproc"})
    assert r2["reused"] is True
    assert connects == [("192.168.50.188", 22, "root", PW)]          # one handshake for the whole task
    assert all(PW not in c for c in client.cmds)                     # never in a command line


async def test_docker_exec_and_script_upload_without_quoting(monkeypatch):
    schema, call, pool, client, connects = _tool({}, monkeypatch)
    await call({"host": "192.168.50.188", "container": "clipboard-online", "command": "ps -ef | grep 'uvicorn x'"})
    assert client.cmds[-1].startswith("docker exec -i clipboard-online sh -lc ")
    await call({"host": "192.168.50.188", "script": "for p in $(pgrep uvicorn); do cat /proc/$p/status; done\\n"})
    path, body = client.puts[-1]
    assert path.startswith("/tmp/aiw_") and "pgrep uvicorn" in body and path in client.cmds[-1]


async def test_timeout_returns_the_output_so_far(monkeypatch):
    schema, call, pool, client, connects = _tool({"vmstat": {"out": ["procs 1\\n", "procs 2\\n"], "done_after": 30}}, monkeypatch)
    t0 = time.monotonic()
    r = await call({"host": "192.168.50.188", "command": "vmstat 1 100", "timeout": 5})
    assert r["timed_out"] is True and "procs 1" in r["stdout"] and "procs 2" in r["stdout"]
    assert time.monotonic() - t0 < 9


def test_confirm_levels():
    assert SX.judge_call({"commands": ["docker stats --no-stream", "docker logs --tail 50 x", "cat /proc/loadavg"]}) is False
    assert SX.judge_call({"command": "docker restart clipboard-online"}) == "always"
    assert SX.judge_call({"command": "./fix.sh"}) == "once"


def test_text_for_the_model():
    from agent_runtime.loop import _ext_text
    t = _ext_text({"host": "h", "user": "root", "reused": True, "results": [
        {"command": "uptime", "exit_code": 0, "stdout": "up 3 days", "stderr": ""},
        {"command": "vmstat 1 100", "exit_code": None, "stdout": "procs", "stderr": "", "timed_out": True}]})
    assert "=== [1] uptime" in t and "复用连接" in t and "超时" in t
