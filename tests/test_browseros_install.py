"""BrowserOS install/detection (2026-10-27): the user saw 未检测 + 安装没有完成 with curl's progress meter as the "error"
on two cloud boxes where the deb had in fact installed (their vm-agent was too old to report BrowserOS), and
"sudo: 需要密码" on the local VM. Covers: progress-meter stripping and the script's ERROR line, sudo → need_sudo +
the paste-in command, the old-agent shell fallback for 重新检测, the install route, and browseros-setup.sh itself
(stubbed dpkg/sudo/curl/apt-get) for arch / sudo / download / apt failures, idempotent re-runs and `status`."""
import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "vm" / "browseros-setup.sh"

# what the old script printed on the user's cloud boxes (curl -fL without -s: the meter on stderr)
CURL_METER = ("  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current\n"
              "                                 Dload  Upload   Total   Spent    Left  Speed\n"
              "\r  0     0    0     0    0     0      0      0 --:--:-- --:--:-- --:--:--     0"
              "\r 13  175M   13 23.6M    0     0  51.4M      0  0:00:03 --:--:--  0:00:03 51.3M"
              "\r100  175M  100  175M    0     0  80.5M      0  0:00:02  0:00:02 --:--:-- 80.6M\n")


class Res:
    def __init__(self, success=True, result=None, error=None):
        self.success, self.result, self.error = success, result or {}, error


@pytest.fixture
def store(tmp_path):
    from aiw_api.store import Store
    return Store(str(tmp_path / "t.db"))


# ----------------------------------------------------------------------------- output -> message
def test_progress_meter_is_never_the_error():
    from aiw_api import browseros as ab
    assert ab.meaningful_lines(CURL_METER) == []
    out = "== downloading x\n== installing\n   | E: Unable to correct problems, you have held broken packages.\nERROR: apt: apt-get install of the BrowserOS deb failed\n"
    s = ab.summarize(out, CURL_METER, 1)
    assert s["error"] == "apt: apt-get install of the BrowserOS deb failed" and s["step"] == "apt"
    assert s["detail"] == ["E: Unable to correct problems, you have held broken packages."]
    assert not s["need_sudo"]
    # an old script with no ERROR line: the error-looking line, still not the meter
    s = ab.summarize("== downloading\n", CURL_METER + "E: Could not get lock /var/lib/dpkg/lock-frontend\n", 100)
    assert s["error"].startswith("E: Could not get lock") and "Dload" not in json.dumps(s)
    assert ab.summarize("OK", CURL_METER, 0)["error"] is None


@pytest.mark.parametrize("out,err,code", [
    ("ERROR: sudo: NEED_SUDO_PASSWORD\n", "", 10),
    ("== downloading\n", CURL_METER + "sudo: 需要密码\nsudo: 需要密码\n", 1),
    ("", "sudo: a password is required\n", 1),
])
def test_sudo_password_is_said_plainly(out, err, code):
    from aiw_api import browseros as ab
    s = ab.summarize(out, err, code)
    assert s["need_sudo"] and s["error"] == "需要这台电脑的 sudo 密码"
    assert "browseros-setup.sh | bash" in ab.MANUAL_CMD and "update-agent.sh | bash" in ab.MANUAL_CMD
    assert ab.MANUAL_CMD.startswith("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/")


# ----------------------------------------------------------------------------- detection with an old vm-agent
class OldAgent:
    """A vm-agent from before the BrowserOS release: health has no browseros key; its shell answers `status`."""
    def __init__(self, status=None, health=None):
        self.status = status if status is not None else {"installed": True, "running": True, "version": "0.51.2.3",
                                                         "arch": "amd64", "mcp_port": 9210, "cdp_port": 9110}
        self.h = health or {"ok": True, "backend": "x11"}
        self.calls = []

    async def health(self):
        return Res(result=dict(self.h))

    async def execute(self, action, payload=None, observe=False, timeout=30):
        self.calls.append((action, payload))
        cmd = payload.get("command", "")
        if action == "shell.exec" and cmd.rstrip().endswith("exit $rc") and " status;" in cmd:
            return Res(result={"exit_code": 0, "stdout": json.dumps(self.status) + "\n", "stderr": ""})
        if action == "shell.exec" and " install;" in cmd:
            return Res(success=True, result={"exit_code": 0, "stdout": "== BrowserOS is already installed\nOK: BrowserOS installed\n",
                                             "stderr": CURL_METER})
        return Res()


async def test_refresh_falls_back_to_the_shell_for_an_old_agent(store):
    from aiw_api import browseros as ab
    st = await ab.refresh(store, OldAgent(), "m1")
    assert st["installed"] and st["running"] and st["agent_old"] and st["arch"] == "amd64" and st["at"]
    assert not ab.enabled(store, "m1")             # bos_* tools need the newer agent
    # the agent gets updated: health reports it, agent_old clears, the switch defaults on
    new = OldAgent(health={"ok": True, "arch": "x86_64", "browseros": {"installed": True, "running": True, "version": "0.51.2.3"}})
    st = await ab.refresh(store, new, "m1")
    assert st["agent_old"] is False and st["arch"] == "x86_64" and ab.enabled(store, "m1")
    assert not new.calls                            # no shell round trip when health answers


async def test_refresh_old_agent_without_browseros(store):
    from aiw_api import browseros as ab
    st = await ab.refresh(store, OldAgent(status={"installed": False, "running": False, "version": "", "arch": "amd64"}), "m2")
    assert st["installed"] is False and st["at"] and st["version"] is None


def test_parse_status_ignores_noise():
    from aiw_api import browseros as ab
    assert ab.parse_status("junk\n{\"installed\": true, \"running\": false}\n")["installed"] is True
    assert ab.parse_status("no json") is None


# ----------------------------------------------------------------------------- install route
def _app(store, comp, busy=False):
    from fastapi import FastAPI
    from aiw_api import browseros as ab
    store.upsert_machine({"id": "m1", "name": "云电脑 1", "provider": "instabox", "provider_vm_id": "r1", "agent_endpoint": "ws://x"})

    class Sessions:
        cloud = None
        def busy_session(self, mid):
            return busy
        def computer(self, mid):
            return comp
    app = FastAPI()
    ab.mount(app, store, Sessions(), lambda: None)
    return app


def test_install_route_reports_installed_but_old_agent(store):
    from fastapi.testclient import TestClient
    with TestClient(_app(store, OldAgent())) as cl:
        r = cl.post("/api/browseros/m1/install").json()
        assert r["ok"] and r["agent_old"] and "update-agent.sh" in r["command"] and r["error"] is None
        assert "Dload" not in r["log"]
        rows = cl.get("/api/browseros").json()["machines"]
        assert rows[0]["seen"]["agent_old"] and rows[0]["command"] and not rows[0]["enabled"]


def test_install_route_need_sudo(store):
    from fastapi.testclient import TestClient

    class SudoAgent(OldAgent):
        async def execute(self, action, payload=None, observe=False, timeout=30):
            if " install;" in payload.get("command", ""):
                return Res(success=False, error="exit code 10", result={
                    "exit_code": 10, "stdout": "ERROR: sudo: NEED_SUDO_PASSWORD\n", "stderr": ""})
            return await super().execute(action, payload, observe, timeout)
    comp = SudoAgent(status={"installed": False, "running": False, "version": "", "arch": "amd64"},
                     health={"ok": True, "browseros": {"installed": False, "running": False}})
    with TestClient(_app(store, comp)) as cl:
        r = cl.post("/api/browseros/m1/install").json()
        assert not r["ok"] and r["need_sudo"] and r["error"] == "需要这台电脑的 sudo 密码" and r["command"]


def test_install_route_shows_the_real_error(store):
    from fastapi.testclient import TestClient

    class AptAgent(OldAgent):
        async def execute(self, action, payload=None, observe=False, timeout=30):
            if " install;" in payload.get("command", ""):
                return Res(success=False, error="exit code 1", result={"exit_code": 1, "stderr": CURL_METER, "stdout":
                           "== installing\n   | dpkg: dependency problems prevent configuration of browserclaw:\n"
                           "ERROR: apt: apt-get install of the BrowserOS deb failed\n"})
            return await super().execute(action, payload, observe, timeout)
    comp = AptAgent(health={"ok": True, "browseros": {"installed": False, "running": False}})
    with TestClient(_app(store, comp)) as cl:
        r = cl.post("/api/browseros/m1/install").json()
        assert not r["ok"] and r["error"].startswith("apt:") and r["detail"][0].startswith("dpkg: dependency")
        assert "Dload" not in json.dumps(r) and not r.get("command")


def test_web_ui_never_shows_the_raw_log_tail():
    js = (ROOT / "apps" / "web" / "ui.js").read_text()
    body = js[js.index("async function bosInstall"):js.index("/* ---------- 运维主机 */")]
    assert "r.log" not in body and "bosInstallMsg(r)" in body and "需要这台电脑的 sudo 密码" in js
    assert "重新检测" in js and "bosCmd(" in js


# ----------------------------------------------------------------------------- the script itself
def _stubs(tmp_path, arch="amd64", sudo_ok=True, curl_ok=True, apt_ok=True, installed=False):
    d = tmp_path / "bin"
    d.mkdir()
    state = tmp_path / "installed"
    if installed:
        state.write_text("1")
    bin_ = tmp_path / "browserclaw"
    files = {
        "id": 'if [ "$1" = -u ]; then echo 1000; else /usr/bin/id "$@"; fi',
        "dpkg": f'''case "$1" in --print-architecture) echo {arch};;
  -s) [ -f "{state}" ] && echo "Status: install ok installed" && echo "Version: 0.51.2.3" && exit 0; exit 1;;
  --configure) exit 0;; esac''',
        "dpkg-query": f'[ -f "{state}" ] && printf 0.51.2.3',
        "dpkg-deb": "exit 0",
        "sudo": ('if [ "$1" = -n ]; then shift; ' + ('' if sudo_ok else 'echo "sudo: a password is required" >&2; exit 1; ')
                 + 'fi; [ "$1" = -v ] && exit 0; [ "$1" = true ] && exit 0; exec "$@"'),
        "curl": (f'''while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done; head -c 2000000 /dev/zero > "$out"'''
                 if curl_ok else 'echo "curl: (22) The requested URL returned error: 404" >&2; exit 22'),
        "apt-get": (f'''for a in "$@"; do [ "$a" = install ] && {{ echo "Setting up browserclaw" ; touch "{state}"; printf '#!/bin/sh\\nexit 0\\n' > "{bin_}"; chmod +x "{bin_}"; }}; done; exit 0'''
                    if apt_ok else 'case " $* " in *" install "*) echo "E: Unable to correct problems, you have held broken packages." >&2; exit 100;; esac; exit 0'),
        "pgrep": "exit 1",
        "setsid": "exit 0",
    }
    for name, body in files.items():
        p = d / name
        p.write_text("#!/bin/bash\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    if installed:
        bin_.write_text("#!/bin/sh\nexit 0\n")
        bin_.chmod(0o755)
    env = {"PATH": f"{d}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), "BROWSEROS_BIN": str(bin_),
           "TMPDIR": str(tmp_path), "DISPLAY": ":99", "BROWSEROS_START_WAIT": "1"}
    (tmp_path / "home").mkdir()
    return env


def _run(env, *args):
    # no controlling terminal (like the vm-agent's shell): sudo cannot prompt
    p = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL, start_new_session=True)
    return p.returncode, p.stdout, p.stderr


needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="bash")


@needs_bash
def test_script_arch(tmp_path):
    code, out, _ = _run(_stubs(tmp_path, arch="arm64"))
    assert code == 3 and "ERROR: arch:" in out


@needs_bash
def test_script_sudo_without_tty_says_need_password(tmp_path):
    code, out, err = _run(_stubs(tmp_path, sudo_ok=False))
    assert code == 10 and out.strip().splitlines()[-1] == "ERROR: sudo: NEED_SUDO_PASSWORD"


@needs_bash
def test_script_download_error_is_real_and_quiet(tmp_path):
    code, out, err = _run(_stubs(tmp_path, curl_ok=False))
    assert code == 1 and "ERROR: download:" in out and "404" in out
    assert "Dload" not in out + err


@needs_bash
def test_script_apt_error_line(tmp_path):
    code, out, err = _run(_stubs(tmp_path, apt_ok=False))
    assert code == 1 and "ERROR: apt:" in out and "held broken packages" in out
    from aiw_api import browseros as ab
    s = ab.summarize(out, err, code)
    assert s["step"] == "apt" and any("held broken packages" in l for l in s["detail"])


@needs_bash
def test_script_installs_then_is_idempotent(tmp_path, monkeypatch):
    env = _stubs(tmp_path)
    code, out, err = _run(env)
    # the stub browser never opens 9210, so `start` fails, but the install and the autostart went through
    assert "== installing" in out and (tmp_path / "home/.config/autostart/browseros.desktop").exists()
    code2, out2, _ = _run(env)
    assert "already installed" in out2 and "== downloading" not in out2
    code3, out3, _ = _run(env, "status")
    st = json.loads(out3.strip().splitlines()[-1])
    assert code3 == 0 and st["installed"] is True and st["version"] == "0.51.2.3" and st["arch"] == "amd64"


@needs_bash
def test_script_status_not_installed(tmp_path):
    code, out, _ = _run(_stubs(tmp_path), "status")
    assert code == 0 and json.loads(out)["installed"] is False


def test_script_uses_quiet_curl_everywhere():
    for f in ["deploy/vm/browseros-setup.sh", "deploy/vm/update-agent.sh"]:
        txt = (ROOT / f).read_text()
        for line in txt.splitlines():
            if line.strip().startswith("#") or "curl " not in line:
                continue
            assert "-fsSL" in line or "-sS" in line, (f, line)
