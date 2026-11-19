"""v3.9.54 Antigravity CLI bridge: start -> wait -> send -> wait -> diff -> stop against a fake agy."""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
from agent_runtime import antigravity as agy  # noqa: E402

BRIDGE = ROOT / "services" / "agent-runtime" / "agent_runtime" / "agy_bridge.py"
FAKE = ROOT / "tests" / "fixtures" / "fake_agy.py"


def run(home, *args, timeout=60):
    env = {**os.environ, "HOME": str(home), "AIW_AGY_BIN": str(FAKE)}
    r = subprocess.run([sys.executable, str(BRIDGE), *args], capture_output=True, text=True, env=env, timeout=timeout)
    res = agy.parse(r.stdout)
    assert res is not None, r.stdout + r.stderr
    return res


@pytest.fixture()
def repo(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rp = home / "proj"
    rp.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=rp, check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (rp / "README.md").write_text("x\n")
    g("add", ".")
    g("commit", "-qm", "init")
    yield home, rp
    subprocess.run([sys.executable, str(BRIDGE), "list"], env={**os.environ, "HOME": str(home)}, capture_output=True)
    for d in (home / ".config/aiw/agy/jobs").glob("*"):
        (d / "STOP").touch()


def test_check(repo):
    home, _ = repo
    r = run(home, "check", "--probe")
    assert r["installed"] and r["signed_in"] and "fake" in r["version"]


def test_full_cycle(repo):
    home, rp = repo
    job = agy.job_from_args({"repo": str(rp)}, agy.brief("add hello.py", {"acceptance": ["prints turn 1"]}))
    assert "prints turn 1" in job["prompt"] and job["mode"] == "accept-edits"
    s = run(home, "start", "--job-b64", base64.b64encode(json.dumps(job).encode()).decode())
    assert s["ok"] and s["branch"].startswith("aiw/"), s
    w = run(home, "wait", s["job"], "--seconds", "30")
    assert w["status"] == "turn_done", w
    assert "Done turn 1" in w["response"]
    assert w["tools"][0]["tool"] == "write_to_file"
    assert any("git push" in d for d in w["denied"])
    assert "hello.py" in " ".join(w["changes"]["files"])
    assert not (rp / "hello.py").exists()          # the main checkout is untouched
    note = agy.render("wait", w)
    assert "do NOT trust" in note and "DENIED" in note
    q = run(home, "send", s["job"], "--text-b64", base64.b64encode("test failed: fix it".encode()).decode())
    assert q["queued"]
    w2 = run(home, "wait", s["job"], "--seconds", "30", "--cursor", str(w["cursor"]))
    assert w2["turn"] == 2 and "Done turn 2" in w2["response"] and w2["tools_new"] == 1
    d = run(home, "diff", s["job"], "--full")
    assert "turn 2" in d["diff"]
    st = run(home, "stop", s["job"])
    assert st["status"] == "stopped"
    bad = run(home, "send", s["job"], "--text-b64", base64.b64encode(b"more").decode())
    assert not bad["ok"] and "ended" in bad["error"]


def test_idle_watchdog(repo, monkeypatch):
    home, rp = repo
    job = agy.job_from_args({"repo": str(rp)}, "HANG please")
    job["idle_min"] = 0.05   # 3 s
    s = run(home, "start", "--job-b64", base64.b64encode(json.dumps(job).encode()).decode())
    w = run(home, "wait", s["job"], "--seconds", "40", timeout=90)
    assert w["status"] in ("stuck", "error"), w


def test_not_a_repo(tmp_path):
    home = tmp_path
    job = agy.job_from_args({"repo": str(tmp_path)}, "x")
    r = run(home, "start", "--job-b64", base64.b64encode(json.dumps(job).encode()).decode())
    assert not r["ok"] and "git" in r["error"]


def test_offered(monkeypatch):
    monkeypatch.setenv("AIW_AGY", "auto")
    assert agy.offered("用 Antigravity 修一下登录页", False)
    assert not agy.offered("修一下登录页", True)
    monkeypatch.setenv("AIW_AGY", "1")
    assert agy.offered("修一下登录页", True) and not agy.offered("订机票", False)
    monkeypatch.setenv("AIW_AGY", "0")
    assert not agy.offered("用 antigravity", True)


def test_install_command_shape():
    c = agy.install_command()
    assert "AGY_BRIDGE_OK" in c and "settings.json" in c and "git push" not in c   # rules are base64
    assert json.loads(base64.b64decode(c.split("echo '")[2].split("'")[0]))["permissions"]["deny"]


def test_registry_has_tool():
    sys.path.insert(0, str(ROOT / "services" / "tool-router"))
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    assert "antigravity" in BY_NAME and "antigravity" in LOOP_TOOLS


async def test_loop_tool_end_to_end(repo, monkeypatch):
    """AgentLoop._antigravity: installs the bridge, starts, waits, defaults the job id, stops on task stop."""
    import types
    from agent_runtime.loop import AgentLoop
    home, rp = repo
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIW_AGY_BIN", str(FAKE))

    class Tools:
        async def run(self, name, args, intent=""):
            p = subprocess.run(["bash", "-c", args["command"]], capture_output=True, text=True, timeout=args["timeout"])
            return types.SimpleNamespace(result={"stdout": p.stdout, "stderr": p.stderr}, error=None,
                                         success=p.returncode == 0)

    class Comp:
        async def execute(self, action, payload, observe=False, timeout=30):
            if action == "shell.exec":
                subprocess.run(["bash", "-c", payload["command"]], timeout=payload["timeout"])
            return types.SimpleNamespace(success=True, result={}, error=None)

    events = []
    lp = AgentLoop.__new__(AgentLoop)
    lp.tools, lp.computer, lp.history, lp.step = Tools(), Comp(), [], 1
    lp.task = types.SimpleNamespace(prompt="用 antigravity 加 hello.py")
    lp._intent = None

    async def emit(t, **kw):
        events.append((t, kw))
    lp.emit = emit
    out = await lp._antigravity({"op": "start", "repo": str(rp), "task": "add hello.py"}, "")
    assert "started agy job" in out, out
    out = await lp._antigravity({"op": "wait", "seconds": 30}, "")
    assert "turn_done" in out and "Done turn 1" in out, out
    assert events[-1][1]["result"]["status"] == "turn_done"
    await lp._kill_commands()
    out = await lp._antigravity({"op": "wait", "seconds": 15}, "")
    assert "stopped" in out, out
