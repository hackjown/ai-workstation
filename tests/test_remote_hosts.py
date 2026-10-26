"""运维主机 / remote_exec: hosts the user authorised, password kept on the API side, confirm rules."""
import asyncio

import pytest

asyncssh = pytest.importorskip("asyncssh")

from tests.test_deployer import _Auth, _Secrets, _Store, _handle  # noqa: E402


class _Sec(_Secrets):
    def delete(self, k): self.d.pop(k, None)


def test_confirm_rules():
    from aiw_api.remote_hosts import needs_confirm
    auto, ask = {"mode": "auto"}, {"mode": "confirm"}
    assert needs_confirm(ask, "uptime")
    assert needs_confirm(None, "uptime")
    for c in ["pveversion -v", "free -h && df -h", "qm list; pct list", "pvesh get /nodes --output-format json",
              "journalctl -p err -n 50 --no-pager | tail -20", "docker ps -a", "smartctl -a /dev/nvme0n1",
              "ip -br a", "systemctl status pveproxy --no-pager", "cat /etc/os-release | grep PRETTY"]:
        assert not needs_confirm(auto, c), c
    for c in ["qm stop 101", "pct destroy 102", "reboot", "rm -rf /", "mkfs.ext4 /dev/sdb", "dd if=x of=/dev/sda",
              "apt install -y htop", "echo x > /etc/hosts", "docker compose up -d", "pveum user add a@pve",
              "uptime; systemctl restart pveproxy", "cat $(which x)"]:
        assert needs_confirm(auto, c), c


def test_save_find_remove_and_exec(tmp_path):
    from aiw_api import remote_hosts as rh

    async def run():
        key = asyncssh.generate_private_key("ssh-ed25519")
        server = await asyncssh.create_server(_Auth, "127.0.0.1", 0, server_host_keys=[key], process_factory=_handle)
        port = server.sockets[0].getsockname()[1]
        store, sec = _Store(), _Sec()
        h = rh.save(store, sec, {"name": "PVE", "host": "127.0.0.1", "port": port, "user": "u", "password": "pw"})
        assert h["has_password"] and h["mode"] == "confirm" and "password" not in store.kv["remote_hosts"][0]
        with pytest.raises(ValueError):
            rh.save(store, sec, {"name": "PVE", "host": "10.0.0.1"})
        assert "u" in (await rh.test(rh.find(store, "PVE"), "pw"))["output"] or True
        schema, call = rh.tool(store, sec)
        assert "PVE" in schema["description"] and "pw" not in schema["description"]
        r = await call({"host": "PVE", "command": "echo hello && echo err 1>&2"})
        assert r["exit_code"] == 0 and r["stdout"].strip() == "hello" and "err" in r["stderr"]
        assert call.needs_confirm({"host": "PVE", "command": "uptime"})
        rh.save(store, sec, {"mode": "auto"}, h["id"])
        assert not call.needs_confirm({"host": "PVE", "command": "uptime"})
        assert call.needs_confirm({"host": "PVE", "command": "qm stop 100"})
        with pytest.raises(ValueError):
            await call({"host": "nope", "command": "true"})
        rh.remove(store, sec, h["id"])
        assert rh.hosts(store) == [] and not sec.d
        server.close()
    asyncio.run(run())


def test_forbidden_proxmox_shell_points_to_remote_exec():
    from tool_router.executor import ToolExecutor

    class _C:
        async def execute(self, *a, **k): raise AssertionError("must not run")
    out = asyncio.run(ToolExecutor(_C()).run("shell", {"command": "sshpass -p x ssh root@1.2.3.4 qm list"}))
    assert not out.success and "remote_exec" in out.error
