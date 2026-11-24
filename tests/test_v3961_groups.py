"""v3.9.61: 引擎群聊 (each engine on its own git worktree branch, AIW 管家 splits the work and merges) and 派活 from the
main chat. Real git and the real cli_bridge.py on this machine, tests/fixtures/fake_cli.py standing in for each CLI."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bots as B  # noqa: E402
from aiw_api import bot_groups as G  # noqa: E402
from aiw_api.store import Store  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fake_cli.py"
ENGS = ("codex", "claude", "codebuddy", "grok")
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


class LLM:
    def __init__(self, out):
        self.out = out
        self.calls = []

    async def complete(self, system, user, max_tokens):
        self.calls.append((system, user))
        return self.out


class FakeChat:
    def __init__(self):
        self.msgs = []

    def add(self, role, kind, text, data=None):
        self.msgs.append({"role": role, "text": text, "data": data or {}})
        return self.msgs[-1]


def make(tmp_path, plan=None):
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(FAKE, 0o755)

    async def shell(mid, cmd, timeout):
        env = {**os.environ, "HOME": str(home), "FAKE_CLI_STATE": str(tmp_path / "state"),
               "GIT_CONFIG_GLOBAL": "/dev/null", **{f"AIW_{e.upper()}_BIN": str(FAKE) for e in ENGS}}
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, env=env, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE)
        o, e = await asyncio.wait_for(p.communicate(), timeout + 5)
        return o.decode() + e.decode()
    llm = LLM(plan or "")
    b = B.Bots(Store(str(tmp_path / "t.db")), default_machine=lambda: "vm1", shell=shell, llm=lambda: llm)
    B.POLL_S = 10
    chat = FakeChat()
    g = G.Groups(b, chat)
    d = G.Dispatch(b, g, chat)
    G.report_takeover(b, chat)
    return b, g, d, chat, home, llm


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout


def init_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hi\n")
    git(path, "add", "-A")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return path


async def until(cond, secs=60):
    for _ in range(secs * 10):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


def stop_all(home):
    for d in (home / ".config/aiw/cli/jobs").glob("*"):
        (d / "STOP").touch()


async def test_group_splits_work_and_merges_branches(tmp_path):
    plan = json.dumps({"assign": {"codex": "写后端 OWNFILE", "claude": "写前端 OWNFILE"}, "report": "前后端分开做"})
    b, g, d, chat, home, llm = make(tmp_path, plan)
    repo = init_repo(tmp_path / "proj")
    try:
        grp = await g.create("登录页", str(repo), ["codex", "claude"])
        assert grp["base"] == "main" and [m["bot"] for m in grp["members"]] == ["codex", "claude"]
        wt = Path(str(grp["members"][0]["worktree"]).replace("~", str(home), 1))
        assert wt.is_dir() and git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"aiw/{grp['id']}/codex"
        assert not [c for c in b.convs() if c.get("grp")]            # members are not in the plain list
        await g.send(grp["id"], "做一个登录页")                        # no @: the 管家 splits it
        await until(lambda: (g.group(grp["id"]) or {}).get("status") == "idle"
                    and any(m["data"].get("merge") for m in g.messages(grp["id"])))
        msgs = g.messages(grp["id"])
        plan_msg = next(m for m in msgs if m["role"] == "aiw" and m["data"].get("plan"))
        assert "前后端分开做" in plan_msg["text"] and "@codex 写后端" in plan_msg["text"]
        bots = [m for m in msgs if m["role"] == "bot"]
        assert {m["who"] for m in bots} == {"codex", "claude"}
        res = next(m for m in msgs if m["data"].get("merge"))
        assert set(res["data"]["merge"]["merged"]) == {"codex", "claude"} and not res["data"]["merge"]["conflicts"]
        mb = f"aiw/{grp['id']}/merge"
        files = git(repo, "ls-tree", "--name-only", mb).split()
        assert {"codex.py", "claude.py", "README.md"} <= set(files)
        assert git(repo, "rev-parse", "main") == git(repo, "rev-parse", "main~0")   # main untouched
        assert "codex.py" not in git(repo, "ls-tree", "--name-only", "main")
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
        # each member got the brief: its branch, commit, no push
        seen = (tmp_path / "state" / "seen_prompts.log").read_text()
        assert f"aiw/{grp['id']}/codex" in seen and "不要 push" in seen and "写后端" in seen
        assert any("引擎群聊" in m["text"] for m in chat.msgs) is False   # not started from the chat: no chat message
    finally:
        stop_all(home)


async def test_mention_goes_to_that_member_only(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    repo = init_repo(tmp_path / "p2")
    try:
        grp = await g.create("x", str(repo), ["codex", "grok"], auto_merge=False)
        await g.send(grp["id"], "@grok 加一个 OWNFILE 文件")
        await until(lambda: any(m["role"] == "bot" for m in g.messages(grp["id"])))
        await until(lambda: g.group(grp["id"])["status"] == "idle")
        msgs = g.messages(grp["id"])
        assert [m["who"] for m in msgs if m["role"] == "bot"] == ["grok"]
        assert not llm.calls                                        # no plan for an @
        assert msgs[-1]["role"] == "aiw" and "合并" in msgs[-1]["text"]   # auto_merge off: waits for the user
    finally:
        stop_all(home)


async def test_conflict_goes_back_then_reported(tmp_path):
    # both edit hello.py: the second merge conflicts; the fix turn (fake still appends) conflicts again; after
    # MAX_FIX tries the 管家 hands it to the user
    plan = json.dumps({"assign": {"codex": "改 hello", "claude": "也改 hello"}, "report": "各改一版"})
    b, g, d, chat, home, llm = make(tmp_path, plan)
    G.MAX_FIX = 1
    repo = init_repo(tmp_path / "p3")
    try:
        grp = await g.create("冲突", str(repo), ["codex", "claude"])
        await g.send(grp["id"], "改 hello")
        await until(lambda: any(m["data"].get("done") is False for m in g.messages(grp["id"])), 90)
        msgs = g.messages(grp["id"])
        fix = [m for m in msgs if m["role"] == "aiw" and "交回" in m["text"]]
        assert fix and "hello.py" in fix[0]["text"]
        seen = (tmp_path / "state" / "seen_prompts.log").read_text()
        assert f"git merge aiw/{grp['id']}/merge" in seen
        assert "需要你看一下" in msgs[-1]["text"]
    finally:
        G.MAX_FIX = 2
        stop_all(home)


def test_parse_dispatch():
    p = G.parse_dispatch("@codex 修一下登录 bug，在 ~/workspace/ldok 里")
    assert p["bots"] == ["codex"] and p["task"].startswith("修一下登录") and p["workdir"] == "~/workspace/ldok"
    p = G.parse_dispatch("@codex和claude一起做一个登录页")
    assert p["bots"] == ["codex", "claude"] and p["task"] == "做一个登录页"
    p = G.parse_dispatch("@grok、cbc、agy：写测试")
    assert p["bots"] == ["grok", "codebuddy", "agy"] and p["task"] == "写测试"
    assert G.parse_dispatch("@clade 盯着把测试跑通")["manage"] is True
    assert G.parse_dispatch("@workbuddy 继续")["cont"] is True and G.parse_dispatch("@workbuddy 继续")["bots"] == ["codebuddy"]
    assert G.parse_dispatch("用claude的api写个脚本") is None
    assert G.parse_dispatch("codex 是什么") is None
    assert G.parse_dispatch("让我想想") is None
    assert G.parse_dispatch("@所有引擎一起重构")["bots"] == ["agy", "codex", "claude", "codebuddy", "grok", "muse", "cue"]


def test_mentions():
    to, mgr, rest = G.mentions("@codex @Claude Code 做 X", ["codex", "claude"])
    assert to == ["codex", "claude"] and not mgr and rest == "做 X"
    to, mgr, rest = G.mentions("@管家 分一下", ["codex"])
    assert to == [] and mgr and rest == "分一下"
    assert G.mentions("@grok 写", ["codex"])[0] == []                 # not a member


async def test_dispatch_from_chat_and_reply_comes_back(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    try:
        r = await d.run(G.parse_dispatch(f"@codex 写个 hello，在 {tmp_path}/solo"), origin="msg-1")
        cid = r["data"]["bot_conv"]
        assert "Codex" in r["text"] and b.conv(cid)["origin"] == "msg-1"
        await until(lambda: any("Codex 回复" in m["text"] for m in chat.msgs))
        m = next(m for m in chat.msgs if "Codex 回复" in m["text"])
        assert "Done turn 1" in m["text"] and m["data"]["bot_conv"] == cid
        assert (tmp_path / "solo" / "hello.py").exists()
        await asyncio.sleep(0.3)
        assert sum("Codex 回复" in m["text"] for m in chat.msgs) == 1   # once per reply
    finally:
        stop_all(home)


async def test_dispatch_two_engines_makes_a_group_and_reports_the_merge(tmp_path):
    plan = json.dumps({"assign": {"codex": "a OWNFILE", "grok": "b OWNFILE"}, "report": "分开"})
    b, g, d, chat, home, llm = make(tmp_path, plan)
    repo = init_repo(tmp_path / "p4")
    try:
        r = await d.run(G.parse_dispatch(f"@codex 和 grok 一起做 X，仓库 {repo}"), origin="msg-2")
        gid = r["data"]["bot_group"]
        assert g.group(gid)["repo"] == str(repo) and g.group(gid)["origin"] == "msg-2"
        await until(lambda: any("引擎群聊" in m["text"] for m in chat.msgs), 90)
        msg = next(m for m in chat.msgs if "引擎群聊" in m["text"])
        assert f"aiw/{gid}/merge" in msg["text"] and msg["data"]["bot_group"] == gid
    finally:
        stop_all(home)


async def test_bad_inputs(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    with pytest.raises(ValueError):
        await g.create("x", "../etc", ["codex"])
    with pytest.raises(ValueError):
        await g.create("x", str(tmp_path / "r"), ["nosuch"])
    with pytest.raises(ValueError):
        await g.create("x", str(tmp_path / "r"), ["codex"], base="--upload-pack=x")


async def test_new_repo_when_folder_is_empty(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    grp = await g.create("新", "", ["claude"])
    assert grp["repo"].startswith("~/workspace/groups/")
    repo = Path(grp["repo"].replace("~", str(home), 1))
    assert (repo / ".git").exists()


def test_http_routes_and_chat_hook():
    main = (ROOT / "services/api/aiw_api/main.py").read_text(encoding="utf-8")
    assert "_groups.Groups(app.state.bots, chat)" in main and "chat.engines = _groups.Dispatch(" in main
    chat = (ROOT / "services/api/aiw_api/chat.py").read_text(encoding="utf-8")
    assert "disp.parse(text)" in chat and 'out["engine_task"]' not in chat
    web = (ROOT / "apps/web/bots.js").read_text(encoding="utf-8")
    assert "/api/bots/groups" in web and "btGroupOpen" in web


async def test_chat_send_hands_off_before_routing(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    chat = app.state.chat
    assert isinstance(chat.engines, G.Dispatch)
    got = []

    class Fake:
        parse = staticmethod(G.parse_dispatch)

        async def run(self, p, attachments=None, origin=""):
            got.append((p, origin))
            return {"text": "交给 Codex 了", "data": {"bot_conv": "bc-1"}}
    chat.engines = Fake()
    r = await chat.send("@codex 修一下登录")
    assert r["route"] == "engine" and got[0][0]["bots"] == ["codex"] and got[0][1] == r["user"]["id"]
    assert r["messages"][0]["data"]["bot_conv"] == "bc-1"
    tools = await chat.tools()
    assert "engine_task" not in tools   # v3.9.82: only @引擎 talks to an engine
    assert G.parse_dispatch("让 codex 修一下登录") is None and G.parse_dispatch("查询agy的ldok开发进度") is None
