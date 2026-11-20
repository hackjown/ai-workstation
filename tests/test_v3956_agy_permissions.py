"""v3.9.56: Antigravity jobs blocked by permissions (task-d31e6e1305, jobs agy-11200120-3f7d / -9524 / -72a8).

* agy only reads freely inside its worktree: absolute paths in the brief are pointed into the worktree, and files that
  live only outside it (skills, untracked files) are copied to <worktree>/.aiw-context/ (ignored by git)
* a denied step ends the turn as status=blocked with which tool, on what, and why (not a generic line + 1 h idle)
* only read_file / write_file / command rules are valid; others are reported as ignored
* the agent may not run agy itself, pass --dangerously-skip-permissions or edit agy's settings
* `git commit -qm` is not Proxmox
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "tool-router"))
from agent_runtime import antigravity as agy  # noqa: E402
from agent_runtime import agy_bridge as ab  # noqa: E402

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
    rp = home / "workspace" / "site"
    (rp / "mirror").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=rp, check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (rp / "mirror" / "index.html").write_text("<title>x</title>\n")
    g("add", ".")
    g("commit", "-qm", "init")
    (rp / "notes.txt").write_text("untracked\n")            # not in the worktree
    sk = home / ".claude" / "skills" / "website-rebuild"
    (sk / "references").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# skill\n")
    (sk / "references" / "gates.md").write_text("gates\n")
    yield home, rp
    for d in (home / ".config/aiw/agy/jobs").glob("*"):
        (d / "STOP").touch()


def start(home, rp, prompt):
    job = agy.job_from_args({"repo": str(rp)}, prompt)
    return run(home, "start", "--job-b64", base64.b64encode(json.dumps(job).encode()).decode())


def test_paths_are_pointed_into_the_worktree(repo):
    home, rp = repo
    s = start(home, rp, f"Read ~/.claude/skills/website-rebuild/SKILL.md and {rp}/mirror/index.html and "
                        f"{rp}/notes.txt, write {rp}/out/a.txt")
    assert s["ok"], s
    w = run(home, "wait", s["job"], "--seconds", "30")
    wt = Path(w["worktree"])
    seen = (wt / "seen_prompts.log").read_text()
    assert f"{wt}/mirror/index.html" in seen and f"{wt}/out/a.txt" in seen
    assert f"{rp}/mirror" not in seen.split("[AIW]")[0]
    assert (wt / ".aiw-context/ext" / str(home).lstrip("/") / ".claude/skills/website-rebuild/references/gates.md").exists()
    assert (wt / ".aiw-context/repo/notes.txt").exists()
    assert ".aiw-context" not in " ".join(w["changes"]["files"])     # never committed
    assert any("website-rebuild" in x for x in s["staged"])


def test_denied_step_is_blocked_with_reason(repo):
    home, rp = repo
    s = start(home, rp, "DENYREAD /etc/hostname then continue")
    w = run(home, "wait", s["job"], "--seconds", "30")
    assert w["status"] == "blocked", w
    d = w["denied"][-1]
    assert d["action"] == "read_file" and d["tool"] == "read_file" and d["target"] == "/etc/hostname"
    assert d["outside_worktree"] and "outside" in d["why"]
    assert "denied" in w["response"]
    note = agy.render("wait", w)
    assert "NEVER work around it" in note and "/etc/hostname" in note
    q = run(home, "send", s["job"], "--text-b64", base64.b64encode(b"use the copy").decode())
    assert q["ok"]                                         # a blocked session still takes a follow-up


def test_settings_written_linted_and_reset(tmp_path):
    home = tmp_path
    r = run(home, "settings")
    assert r["action"] == "written" and r["managed"] and not r["ignored_rules"]
    p = home / ".gemini/antigravity-cli/settings.json"
    cur = json.loads(p.read_text())
    cur["permissions"]["allow"] += ["bash(**)", "run_command(regex:.*)"]
    cur["aiwManaged"] = False
    cur["dangerouslySkipPermissions"] = True
    cur["model"] = "keep-me"
    p.write_text(json.dumps(cur))
    r = run(home, "settings")
    assert r["action"] == "kept" and len(r["ignored_rules"]) == 2 and r["skip_all"]
    assert "IGNORES" in agy.render("check", {"ok": True, "settings": r})
    r = run(home, "settings", "--reset")
    new = json.loads(p.read_text())
    assert r["action"] == "reset" and new["model"] == "keep-me" and "dangerouslySkipPermissions" not in new
    assert new["permissions"]["allow"] == ab.DEFAULT_ALLOW and list(p.parent.glob("settings.json.bak-aiw-*"))


def test_default_rules_use_valid_actions_only():
    assert not ab.lint_rules({"allow": ab.DEFAULT_ALLOW, "deny": ab.DEFAULT_DENY})
    assert ab.lint_rules({"allow": ["glob(**)", "command(ls)"]}) == ["allow: glob(**)"]


@pytest.mark.parametrize("cmd,blocked", [
    ('git commit -qm "fix"', False),
    ("cd ~/workspace/x && git add -A && git commit -qm wip", False),
    ("qm list", True),
    ('cd /home/aiw/workspace/ldok-rebuild && agy --print --dangerously-skip-permissions "do it"', True),
    ("timeout 1800 ~/.local/bin/agy -p hi", True),
    ("sed -i 's/x/y/' ~/.gemini/antigravity-cli/settings.json", True),
    ('python3 "$HOME/.config/aiw/agy/agy_bridge.py" settings --reset', True),
    ('python3 "$HOME/.config/aiw/agy/agy_bridge.py" wait agy-1 --seconds 600 --cursor 0', False),
    ("which agy", False),
])
def test_policy(cmd, blocked):
    from tool_router.policy import PolicyEngine, RiskLevel
    d = PolicyEngine().check("shell.exec", {"command": cmd})
    assert (d.level == RiskLevel.L4) is blocked, d


def test_install_command_passes_policy():
    from tool_router.policy import PolicyEngine, RiskLevel
    assert PolicyEngine().check("shell.exec", {"command": agy.install_command()}).level < RiskLevel.L3
