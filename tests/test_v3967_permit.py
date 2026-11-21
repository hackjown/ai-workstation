"""v3.9.67 引擎被权限拦下: name the denied command, 「允许这类命令再试」 / 「这一轮放开权限重跑」 (the user's call),
and the 管家 reroutes once before asking. Real agy_bridge.py against tests/fixtures/fake_agy.py."""
import asyncio
import base64
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "services" / "tool-router"))
from aiw_api import bots as B  # noqa: E402
from agent_runtime import agy_bridge as AB  # noqa: E402
from test_v3957_bots import LLM, make, settle, stop_all  # noqa: E402

CMD = "soffice --headless --convert-to pdf deck.pptx"


def run_op(fn, *a):
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        fn(*a)
    return json.loads(buf.getvalue().strip().split("AGYRESULT ", 1)[1])


def test_suggested_rules_stay_narrow():
    assert B.suggest_rule({"action": "command", "command": CMD}) == r"command(regex:soffice \-\-headless( .*)?)"
    assert B.suggest_rule({"action": "command", "command": "pdftoppm -png a.pdf out"}) == r"command(regex:pdftoppm \-png( .*)?)"
    assert B.suggest_rule({"action": "command", "command": "libreoffice deck.pptx"}) == "command(regex:libreoffice( .*)?)"
    assert B.suggest_rule({"action": "command", "command": "rm -rf build"}) == "command(rm -rf build)"     # risky: exact
    assert B.suggest_rule({"action": "command", "command": "soffice a | sh"}) == "command(soffice a | sh)"
    assert B.suggest_rule({"action": "read_file", "target": "/home/aiw/x.pdf"}) == "read_file(/home/aiw/x.pdf)"
    assert B.suggest_rule({"action": "?", "tool": "?"}) == ""


def test_bridge_allow_keeps_the_users_rule_and_refuses_wide_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(AB, "SETTINGS", str(tmp_path / "settings.json"))
    run_op(AB.op_settings, False)
    r = run_op(AB.op_allow, base64.b64encode(b"command(regex:soffice( .*)?)").decode())
    assert r["ok"]
    cur = json.loads((tmp_path / "settings.json").read_text())
    assert "command(regex:soffice( .*)?)" in cur["permissions"]["allow"] and cur["aiwUserAllow"] == ["command(regex:soffice( .*)?)"]
    run_op(AB.op_settings, False)                       # the install step runs `settings` every time: the rule stays
    assert "command(regex:soffice( .*)?)" in json.loads((tmp_path / "settings.json").read_text())["permissions"]["allow"]
    for bad in ("command(regex:.*)", "command(**)", "write_file(~/.gemini/x)", "command(agy --dangerously-skip-permissions)", "bash -c x"):
        assert not run_op(AB.op_allow, base64.b64encode(bad.encode()).decode())["ok"], bad
    run_op(AB.op_settings, True)                        # --reset drops the user's additions too
    cur = json.loads((tmp_path / "settings.json").read_text())
    assert "aiwUserAllow" not in cur and "command(regex:soffice( .*)?)" not in cur["permissions"]["allow"]


def test_agent_cannot_add_rules_itself():
    from tool_router.policy import PolicyEngine, RiskLevel
    from agent_runtime import antigravity as agy
    d = PolicyEngine().check("shell.exec", {"command": 'python3 "$HOME/.config/aiw/agy/agy_bridge.py" allow --rule-b64 Y29t'})
    assert d.level == RiskLevel.L4, d
    assert PolicyEngine().check("shell.exec", {"command": agy.install_command()}).level < RiskLevel.L3


async def test_denied_command_is_named_and_once_reruns_with_permissions_off(tmp_path):
    b, home = make(tmp_path)
    args = tmp_path / "args.log"
    os.environ["FAKE_AGY_ARGS"] = str(args)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        b.send(c["id"], "DENYCMD " + CMD)
        await settle(b, c["id"])
        bot = [m for m in b.messages(c["id"]) if m["role"] == "bot"][-1]
        d = bot["data"]
        assert d["status"] == "blocked"
        assert d["denied"][-1]["command"] == CMD and d["denied"][-1]["rule"].startswith("command(regex:soffice")
        assert "被权限拦下了：" + CMD in bot["text"] and "这一轮放开权限重跑" in bot["text"]
        with pytest.raises(ValueError):
            await b.permit(c["id"], "nope")
        await b.permit(c["id"], "once")
        await settle(b, c["id"])
        ms = b.messages(c["id"])
        assert ms[-1]["role"] == "bot" and ms[-1]["data"]["status"] == "done" and ms[-1]["data"].get("unrestricted")
        assert [m for m in ms if m["role"] == "bot"][-2]["data"]["permitted"] == "once"
        assert b.conv(c["id"])["job"] is None             # the unrestricted process is gone
        b.send(c["id"], "next")
        await settle(b, c["id"])
        runs = [l for l in args.read_text().splitlines() if "--input-format" in l]
        assert len(runs) == 3 and "--dangerously-skip-permissions" in runs[1] and "--dangerously-skip-permissions" not in runs[2]
        with pytest.raises(ValueError):
            await b.permit(c["id"], "once")                # nothing blocked any more
    finally:
        os.environ.pop("FAKE_AGY_ARGS", None)
        stop_all(home)


async def test_allow_writes_the_rule_and_the_command_then_runs(tmp_path):
    b, home = make(tmp_path)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        b.send(c["id"], "DENYCMD " + CMD)
        await settle(b, c["id"])
        await b.permit(c["id"], "allow")
        await settle(b, c["id"])
        st = json.loads((home / ".gemini/antigravity-cli/settings.json").read_text())
        assert r"command(regex:soffice \-\-headless( .*)?)" in st["permissions"]["allow"]
        sysm = [m for m in b.messages(c["id"]) if m["role"] == "system"][-1]
        assert sysm["data"]["permit"] == "allow"
        b.send(c["id"], "DENYCMD " + CMD)
        await settle(b, c["id"])
        last = b.messages(c["id"])[-1]
        assert last["data"]["status"] == "done" and last["text"].strip() == "ran " + CMD
    finally:
        stop_all(home)


async def test_manager_reroutes_once_then_waits_for_the_user(tmp_path):
    llm = LLM([json.dumps({"done": True, "report": "完成了"})])
    b, home = make(tmp_path, llm)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        b.takeover(c["id"], "DENYCMD " + CMD, 5)
        await settle(b, c["id"], 60)
        ms = b.messages(c["id"])
        aiw = [m for m in ms if m["role"] == "aiw"]
        assert "写进工作目录里的一个脚本" in aiw[1]["text"]                    # rerouted once, without the LLM
        assert aiw[-1]["data"].get("permit") and "又被权限拦下" in aiw[-1]["text"] or \
            [m for m in ms if m["role"] == "bot"][-1]["data"]["status"] != "blocked"
        tk = b.conv(c["id"])["takeover"]
        if [m for m in ms if m["role"] == "bot"][-1]["data"]["status"] == "blocked":
            assert tk and tk.get("waiting") == "permit"
            await b.permit(c["id"], "once")
            await settle(b, c["id"], 60)
            assert not b.conv(c["id"])["takeover"]          # the 管家 judged the rerun (LLM said done) and handed back
            assert llm.outs == []
    finally:
        stop_all(home)


async def test_manager_waits_for_the_users_permit_after_the_reroute(tmp_path):
    llm = LLM([json.dumps({"done": True, "report": "完成了"})])
    b, home = make(tmp_path, llm)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        tk = b.takeover(c["id"], "DENYCMD " + CMD, 5)["takeover"]
        b.set_conv(c["id"], takeover={**tk, "rerouted": True})     # as if the reroute already happened
        await settle(b, c["id"], 60)
        ms = b.messages(c["id"])
        assert ms[-1]["role"] == "aiw" and ms[-1]["data"]["permit"] and "又被权限拦下了：" + CMD in ms[-1]["text"]
        assert b.conv(c["id"])["takeover"]["waiting"] == "permit" and llm.outs      # the LLM was not asked
        await b.permit(c["id"], "once")
        await settle(b, c["id"], 60)
        assert not b.conv(c["id"])["takeover"] and llm.outs == []
        assert [m for m in b.messages(c["id"]) if m["role"] == "aiw"][-1]["data"].get("done")
    finally:
        stop_all(home)


def test_engine_file_paths():
    wd = "~/workspace/agy-chat/bc-76abe6fed7"
    assert B.vm_file_path("file:///home/aiw/workspace/agy-chat/bc-76abe6fed7/安盛.pptx", wd) == \
        "/home/aiw/workspace/agy-chat/bc-76abe6fed7/安盛.pptx"
    assert B.vm_file_path("file:///home/aiw/workspace/a%20b.pdf", wd) == "/home/aiw/workspace/a b.pdf"
    assert B.vm_file_path("out/deck.pptx", wd) == "agy-chat/bc-76abe6fed7/out/deck.pptx"
    assert B.vm_file_path("~/workspace/x.md", wd) == "x.md"
    for bad in ("~/.ssh/id_rsa", "../../etc/passwd", ""):
        with pytest.raises(ValueError):
            B.vm_file_path(bad, wd)


async def test_fetch_file_copies_it_for_the_app(tmp_path):
    class R:
        success, error = True, ""
        result = {"content_b64": base64.b64encode(b"PPTX").decode(), "size": 4}

    class Comp:
        async def execute(self, action, payload, **kw):
            assert action == "files.read" and payload["path"].endswith("/安盛.pptx")
            return R()

    class Sessions:
        def computer(self, mid):
            return Comp()
    b, home = make(tmp_path)
    b.sessions = Sessions()
    c = b.create("agy", "ppt", "~/workspace/agy-chat/bc-1")
    r = await b.fetch_file(c["id"], "file:///home/aiw/workspace/agy-chat/bc-1/安盛.pptx", str(tmp_path / "art"))
    assert r["name"] == "安盛.pptx" and r["size"] == 4 and Path(r["path"]).read_bytes() == b"PPTX"
    assert Path(r["path"]).parent == tmp_path / "art" / "engine-files" / c["id"]


def test_clients_offer_the_permit_and_open_engine_files():
    js = (ROOT / "apps/web/bots.js").read_text(encoding="utf-8")
    css = (ROOT / "apps/web/bots.css").read_text(encoding="utf-8")
    sw = (ROOT / "apps/ios/Sources/EngineView.swift").read_text(encoding="utf-8")
    st = (ROOT / "apps/ios/Sources/SelectableText.swift").read_text(encoding="utf-8")
    assert "btPermit('allow')" in js and "btPermit('once')" in js and "/permit`" in js
    assert "/file?path=" in js and "#aiwfile=" in js and "btFileLinks(m.text)" in js
    assert ".bt-top .bt-wd { display:none; }" in css and ".bt-top .grow { min-width:0;" in css
    assert '"/permit", ["mode": "allow", "rule": allowRule]' in sw and '["mode": "once"]' in sw
    assert "engineFilePaths(m.s(\"text\"))" in sw and ".environment(\\.aiwOpenFile" in sw and "/file?path=" in sw
    assert "URL.isFileURL, let f = onFile" in st and "v.onFile = context.environment.aiwOpenFile" in st
