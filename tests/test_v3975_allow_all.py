"""v3.9.75: the user asked (2026-11-22) that agy stop asking for every new command, and that a turn which stops
silently mid-task (the PDF -> video run: 2 steps, no reply) be carried on instead of left blank."""
import importlib.util
import os
from pathlib import Path

from aiw_api import bots as B

ROOT = Path(__file__).resolve().parents[1]
sp = importlib.util.spec_from_file_location("agyb75", ROOT / "services/agent-runtime/agent_runtime/agy_bridge.py")
b = importlib.util.module_from_spec(sp)
sp.loader.exec_module(b)


def test_default_allows_everything_but_deny_list():
    assert "command(*)" in b.DEFAULT_ALLOW and "read_file(*)" in b.DEFAULT_ALLOW
    for r in ("command(sudo)", "command(git push)", "command(rm -rf /)", "command(rm -rf ~)"):
        assert r in b.DEFAULT_DENY
    h = os.path.expanduser("~")
    assert f"read_file({h}/.ssh)" in b.DEFAULT_DENY and f"write_file({h}/.gemini)" in b.DEFAULT_DENY
    assert not b.lint_rules({"allow": b.DEFAULT_ALLOW, "deny": b.DEFAULT_DENY})


def test_managed_settings_get_upgraded(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    b.write_json(str(p), {"aiwManaged": True, "permissions": {"allow": ["command(git)"], "deny": []},
                          "aiwUserAllow": ["command(pdftotext x.pdf y.txt)"]})
    monkeypatch.setattr(b, "SETTINGS", str(p))
    try:
        b.op_settings(False)
    except SystemExit:
        pass
    new = b.read_json(str(p), {})
    assert "command(*)" in new["permissions"]["allow"]
    assert "command(pdftotext x.pdf y.txt)" in new["permissions"]["allow"]
    assert "command(sudo)" in new["permissions"]["deny"]


def test_silent_stop_is_nudged_and_logged():
    res = {"status": "turn_done", "response": "", "said": []}
    assert B.needs_nudge(res, 2)
    assert not B.needs_nudge(res, 0)
    assert not B.needs_nudge({**res, "response": "做完了"}, 2)
    assert not B.needs_nudge({**res, "status": "blocked"}, 2)
    assert "agy 最后的日志" in B.turn_text({"response": "", "stderr_tail": ["model error: quota"]})


def test_nudge_limit():
    assert B.MAX_NUDGES == 3 and "继续" in B.NUDGE
