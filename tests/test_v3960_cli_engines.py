"""v3.9.60: Codex, Claude Code, CodeBuddy and Grok Build in 引擎, through the real cli_bridge.py on this machine and
tests/fixtures/fake_cli.py standing in for each CLI (one process per turn, the session id resumed on the next)."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bots as B  # noqa: E402
from aiw_api.store import Store  # noqa: E402
from agent_runtime import cli_bridge as CB  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fake_cli.py"
ENGS = ("codex", "claude", "codebuddy", "grok")


def make(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(FAKE, 0o755)
    args = tmp_path / "args.jsonl"

    async def shell(mid, cmd, timeout):
        env = {**os.environ, "HOME": str(home), "FAKE_CLI_ARGS": str(args),
               **{f"AIW_{e.upper()}_BIN": str(FAKE) for e in ENGS}}
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, env=env, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE)
        o, e = await asyncio.wait_for(p.communicate(), timeout + 5)
        return o.decode() + e.decode()
    b = B.Bots(Store(str(tmp_path / "t.db")), default_machine=lambda: "vm1", shell=shell)
    B.POLL_S = 10
    return b, home, args


async def settle(b, cid, secs=40):
    for _ in range(secs * 10):
        if not b.busy(cid):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("still busy")


def stop_all(home):
    for d in (home / ".config/aiw/cli/jobs").glob("*"):
        (d / "STOP").touch()


@pytest.mark.parametrize("eng", ENGS)
async def test_two_turns_one_session(tmp_path, eng):
    b, home, args = make(tmp_path)
    try:
        c = b.create(eng, "试试", str(tmp_path / "proj"), model="m-1")
        b.send(c["id"], "write hello")
        await settle(b, c["id"])
        b.send(c["id"], "again")
        await settle(b, c["id"])
        bots = [m for m in b.messages(c["id"]) if m["role"] == "bot"]
        assert [m["text"].strip() for m in bots] == ["Done turn 1: edited hello.py", "Done turn 2: edited hello.py"]
        assert bots[0]["data"]["status"] == "done" and bots[0]["data"]["steps"]
        assert (tmp_path / "proj" / "hello.py").read_text().count("print") == 2
        c2 = b.conv(c["id"])
        assert c2["session"] and c2["job"] and c2["status"] == "idle"
        calls = [json.loads(x) for x in args.read_text().splitlines()]
        assert len(calls) == 2
        first, second = calls
        # the second call resumes the first one's session, with the model the conversation picked
        assert c2["session"] in second and c2["session"] not in first
        assert "m-1" in first
        if eng == "codex":
            assert first[:2] == ["exec", "--json"] and "--skip-git-repo-check" in first
            assert 'sandbox_mode="workspace-write"' in first and "--full-auto" not in first
            assert second[second.index("resume") + 1] == c2["session"]
        elif eng == "claude":
            assert "--permission-mode" in first and "acceptEdits" in first and "--verbose" in first
            assert "Bash(git push:*)" in first[first.index("--disallowedTools") + 1]
        elif eng == "codebuddy":
            assert "-y" in first and "--disallowedTools" in first
        else:
            assert "--always-approve" in first and "Bash(git push*)" in first and "--prompt-file" in first
        jobs = list((home / ".config/aiw/cli/jobs").glob("*"))
        assert len(jobs) == 1      # one daemon carried both turns
    finally:
        stop_all(home)


async def test_resume_after_the_daemon_ended(tmp_path):
    b, home, args = make(tmp_path)
    try:
        c = b.create("claude", "", str(tmp_path / "p"))
        b.send(c["id"], "one")
        await settle(b, c["id"])
        sid = b.conv(c["id"])["session"]
        stop_all(home)
        await asyncio.sleep(1.5)
        b.send(c["id"], "two")
        await settle(b, c["id"])
        bots = [m for m in b.messages(c["id"]) if m["role"] == "bot"]
        assert bots[-1]["text"].strip() == "Done turn 2: edited hello.py"      # same session, turn 2
        calls = [json.loads(x) for x in args.read_text().splitlines()]
        assert calls[-1][calls[-1].index("--resume") + 1] == sid
        assert len(list((home / ".config/aiw/cli/jobs").glob("*"))) == 2
    finally:
        stop_all(home)


async def test_denied_step_is_blocked_and_named(tmp_path):
    b, home, _ = make(tmp_path)
    try:
        c = b.create("claude", "", str(tmp_path / "p"))
        b.send(c["id"], "DENY please push")
        await settle(b, c["id"])
        bot = [m for m in b.messages(c["id"]) if m["role"] == "bot"][-1]
        assert bot["data"]["status"] == "blocked"
        assert bot["data"]["denied"] and bot["data"]["denied"][0]["tool"] == "Bash"
        assert "git push" in bot["text"]
    finally:
        stop_all(home)


async def test_not_logged_in_says_how_to_log_in(tmp_path):
    b, home, _ = make(tmp_path)
    try:
        c = b.create("codebuddy", "", str(tmp_path / "p"))
        b.send(c["id"], "NOLOGIN")
        await settle(b, c["id"])
        bot = [m for m in b.messages(c["id"]) if m["role"] == "bot"][-1]
        assert bot["data"]["status"] == "failed"
        assert "需要重新登录" in bot["text"] and "codebuddy" in bot["text"]
    finally:
        stop_all(home)


async def test_models_and_efforts_per_engine(tmp_path):
    b, home, _ = make(tmp_path)
    g = await b.models("grok", "vm1")
    assert [x["id"] for x in g["groups"]][:2] == ["grok-4.6", "grok-code-fast-2"] and "high" in g["efforts"]
    cl = await b.models("claude", "vm1")
    assert [x["id"] for x in cl["groups"]] == ["opus", "sonnet", "haiku"] and cl["efforts"] == []
    cx = await b.models("codex", "vm1")
    assert "xhigh" in cx["efforts"] and "max" not in cx["efforts"]
    # v3.9.62: codex's own catalog (`codex debug models`), hidden ones left out, each model with its own strengths
    assert [x["id"] for x in cx["groups"]][:2] == ["gpt-6-sol", "gpt-6-mini"]
    assert "internal-x" not in [x["id"] for x in cx["groups"]]
    assert cx["groups"][0]["efforts"] == ["low", "medium", "high", "xhigh"] and cx["groups"][1]["efforts"] == ["low"]
    # CodeBuddy has no `models` subcommand: the bridge must not run one (it would start a chat), so no grok list here
    cb = await b.models("codebuddy", "vm1")
    assert "grok-4.6" not in [x["id"] for x in cb["groups"]]
    # v3.9.63: CodeBuddy's account models (static list) and its six strengths (settings.reasoningEffort)
    ids = [x["id"] for x in cb["groups"]]
    assert "hy4std" in ids and "hy4flash-free" in ids and "glm-5.3" in ids
    assert cb["efforts"] == ["minimal", "low", "medium", "high", "xhigh", "max"]


def test_codebuddy_effort_goes_in_settings(tmp_path):
    import json as _j
    import importlib.util as _u
    spec = _u.spec_from_file_location("cb_bridge", str(ROOT / "services/agent-runtime/agent_runtime/cli_bridge.py"))
    m = _u.module_from_spec(spec)
    spec.loader.exec_module(m)
    job = {"engine": "codebuddy", "bin": "codebuddy", "workdir": str(tmp_path), "model": "glm-5.3", "effort": "max"}
    argv, stdin = m.build_cmd(job, "hi", "", str(tmp_path))
    assert argv[argv.index("--model") + 1] == "glm-5.3"
    assert _j.loads(argv[argv.index("--settings") + 1]) == {"reasoningEffort": "max"} and stdin == "hi"
    job["effort"] = ""
    assert "--settings" not in m.build_cmd(job, "hi", "", str(tmp_path))[0]


def test_codebuddy_models_json_is_read(tmp_path, monkeypatch):
    import importlib.util as _u
    (tmp_path / ".codebuddy").mkdir()
    (tmp_path / ".codebuddy/models.json").write_text('{"models":[{"id":"my-m","name":"Mine"}],"availableModels":["hy4std","x-1"]}')
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = _u.spec_from_file_location("cb_bridge2", str(ROOT / "services/agent-runtime/agent_runtime/cli_bridge.py"))
    m = _u.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert [x["id"] for x in m.config_models("codebuddy")] == ["my-m", "hy4std", "x-1"]


async def test_missing_cli_says_how_to_install(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    async def shell(mid, cmd, timeout):
        env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin", "AIW_GROK_BIN": "/nope/grok"}
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, env=env, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE)
        o, e = await p.communicate()
        return o.decode() + e.decode()
    b = B.Bots(Store(str(tmp_path / "t.db")), default_machine=lambda: "vm1", shell=shell)
    c = b.create("grok", "", str(tmp_path / "p"))
    b.send(c["id"], "hi")
    await settle(b, c["id"])
    bot = [m for m in b.messages(c["id"]) if m["role"] == "bot"][-1]
    assert "上没装 Grok Build" in bot["text"]   # v3.9.73 names the computer and "x.ai/cli/install.sh" in bot["text"]


def test_codex_sandbox_failure_is_listed_as_denied():
    recs = []
    t = CB.Turn("codex", recs.append)
    t.line(json.dumps({"type": "thread.started", "thread_id": "th1"}))
    t.line(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "touch /etc/x",
                                                          "aggregated_output": "Read-only file system", "exit_code": 1}}))
    t.line(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}))
    assert t.session == "th1" and t.failed and t.error == "boom"
    assert t.denied and t.denied[0]["target"] == "touch /etc/x"
    assert recs[0]["kind"] == "tool" and recs[0]["error"].startswith("exit 1")


def test_engines_listed_available():
    ids = {e["id"]: e for e in B.ENGINES.values()}
    assert all(ids[e]["available"] for e in ("agy",) + ENGS)
    assert ids["codebuddy"]["short"] == "cbc"
