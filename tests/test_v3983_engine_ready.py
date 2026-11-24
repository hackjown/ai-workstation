"""v3.9.83: the 引擎 avatar dot is green only when we know the engine can answer (logged in / configured)."""
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
from aiw_api import bots as B  # noqa: E402
from aiw_api.store import Store  # noqa: E402
from agent_runtime import cli_bridge as CB  # noqa: E402


def mk(tmp_path, checks):
    async def shell(mid, cmd, timeout):
        if "CLI_BRIDGE_OK" in cmd or "base64 -d" in cmd:
            return "CLI_BRIDGE_OK\nAGY_BRIDGE_OK"
        for eng, res in checks.items():
            if f" check {eng}" in cmd or (eng == "agy" and " check" in cmd and "cli_bridge" not in cmd):
                return "CLIRESULT " + json.dumps(res) + "\nAGYRESULT " + json.dumps(res)
        return 'CLIRESULT {"ok": false}'
    return B.Bots(Store(str(tmp_path / "t.db")), default_machine=lambda: "vm1", shell=shell)


def test_states(tmp_path):
    b = mk(tmp_path, {"codex": {"ok": True, "installed": True, "creds": False},
                      "claude": {"ok": True, "installed": True, "creds": True},
                      "grok": {"ok": False, "installed": False}})
    assert b.ready_state("codex")[0] == "unknown"           # nothing checked yet: not green
    asyncio.run(b.refresh_ready())
    assert b.ready_state("codex")[0] == "off"
    assert b.ready_state("claude")[0] == "ok"
    st, why = b.ready_state("grok")
    assert st == "off" and "没装" in why
    assert b.ready_state("cue", {"configured": False})[0] == "off"
    assert b.ready_state("muse", {"configured": True})[0] == "ok"


def test_creds_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert CB.has_creds("codex") is False and CB.has_creds("grok") is False
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text('{"tokens": 1}')
    assert CB.has_creds("codex") is True
    (tmp_path / ".claude.json").write_text('{"oauthAccount": {}}')
    assert CB.has_creds("claude") is True
    (tmp_path / ".grok").mkdir()
    assert CB.has_creds("grok") is None


def test_web_bar_two_rows_and_ready_dot():
    css = (ROOT / "apps/web/bots.css").read_text(encoding="utf-8")
    js = (ROOT / "apps/web/bots.js").read_text(encoding="utf-8")
    assert "grid-template-columns:repeat(4" in css and "e.ready" in js
