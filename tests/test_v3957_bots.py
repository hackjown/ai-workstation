"""v3.9.57 引擎对话: an Antigravity session the user talks to directly, AIW 管家 takeover, and quoting with context.

Runs the real agy_bridge.py (plain mode: a folder, no worktree) against tests/fixtures/fake_agy.py on this machine.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bots as B  # noqa: E402
from aiw_api.store import Store  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fake_agy.py"


class LLM:
    def __init__(self, outs):
        self.outs, self.seen = list(outs), []

    async def complete(self, system, user, n):
        self.seen.append(user)
        return self.outs.pop(0)


def make(tmp_path, llm=None):
    home = tmp_path / "home"
    home.mkdir()

    async def shell(mid, cmd, timeout):
        env = {**os.environ, "HOME": str(home), "AIW_AGY_BIN": str(FAKE)}
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, env=env, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE)
        o, e = await asyncio.wait_for(p.communicate(), timeout + 5)
        return o.decode() + e.decode()
    b = B.Bots(Store(str(tmp_path / "t.db")), llm=(lambda: llm), default_machine=lambda: "vm1", shell=shell)
    B.POLL_S = 10
    return b, home


async def settle(b, cid, secs=40):
    for _ in range(secs * 10):
        if not b.busy(cid):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("still busy")


def stop_all(home):
    for d in (home / ".config/aiw/agy/jobs").glob("*"):
        (d / "STOP").touch()


async def test_direct_chat_keeps_one_session_in_a_folder(tmp_path):
    b, home = make(tmp_path)
    try:
        c = b.create("agy", "试试", str(tmp_path / "proj"))
        b.send(c["id"], "write hello")
        await settle(b, c["id"])
        b.send(c["id"], "again")
        await settle(b, c["id"])
        ms = b.messages(c["id"])
        bots = [m for m in ms if m["role"] == "bot"]
        assert [m["text"].strip() for m in bots] == ["Done turn 1: edited hello.py", "Done turn 2: edited hello.py"]
        assert bots[0]["data"]["status"] == "done" and bots[0]["data"]["steps"][0]["tool"] == "write_to_file"
        assert (tmp_path / "proj" / "hello.py").read_text().count("print") == 2     # edited in place, same process
        c2 = b.conv(c["id"])
        assert c2["job"] and c2["session"] and c2["status"] == "idle"
        jobs = list((home / ".config/aiw/agy/jobs").glob("*"))
        assert len(jobs) == 1 and json.loads((jobs[0] / "job.json").read_text())["plain"] is True
    finally:
        stop_all(home)


async def test_quote_brings_the_message_and_its_context(tmp_path):
    b, home = make(tmp_path)
    try:
        c = b.create("agy", "", str(tmp_path / "p"))
        b.send(c["id"], "first ask")
        await settle(b, c["id"])
        reply = [m for m in b.messages(c["id"]) if m["role"] == "bot"][0]
        q = b.quote(reply["id"])
        assert q["who"] == "Antigravity" and q["context"][0]["text"] == "first ask"
        m = b.send(c["id"], "改一下这里", quote=q)
        assert m["data"]["quote"]["id"] == reply["id"]
        await settle(b, c["id"])
        seen = (tmp_path / "p" / "seen_prompts.log").read_text()
        assert "［引用 Antigravity 的话］Done turn 1" in seen and "用户：first ask" in seen and "改一下这里" in seen
    finally:
        stop_all(home)


async def test_takeover_drives_until_done_then_hands_back(tmp_path):
    llm = LLM(['{"done": false, "next": "加上测试并运行", "report": "还缺测试"}',
               '{"done": true, "report": "两轮都完成了，hello.py 已修改"}'])
    b, home = make(tmp_path, llm)
    try:
        c = b.create("agy", "", str(tmp_path / "q"))
        b.takeover(c["id"], "写一个 hello.py")
        await settle(b, c["id"], 60)
        roles = [(m["role"], m["text"][:12]) for m in b.messages(c["id"])]
        assert roles[0][0] == "system" and roles[1] == ("aiw", "写一个 hello.py")
        assert ("aiw", "加上测试并运行") in roles
        assert sum(1 for r in roles if r[0] == "bot") == 2
        assert b.conv(c["id"])["takeover"] is None
        assert "目标：写一个 hello.py" in llm.seen[0] and "Done turn 1" in llm.seen[0]
        last_aiw = [m for m in b.messages(c["id"]) if m["role"] == "aiw"][-1]
        assert last_aiw["data"].get("done") and "完成" in last_aiw["text"]
    finally:
        stop_all(home)


async def test_takeover_asks_the_user_and_stops(tmp_path):
    b, home = make(tmp_path, LLM(['{"done": false, "ask_user": "要 push 到 GitHub 吗？", "report": "改好了"}']))
    try:
        c = b.create("agy", "", str(tmp_path / "r"))
        b.takeover(c["id"], "改好并发布")
        await settle(b, c["id"], 60)
        aiw = [m for m in b.messages(c["id"]) if m["role"] == "aiw"]
        assert "需要你决定：要 push 到 GitHub 吗？" in aiw[-1]["text"] and aiw[-1]["data"]["ask"]
        assert b.conv(c["id"])["takeover"] is None
    finally:
        stop_all(home)


def test_quote_block_and_decision_parsing():
    s = B.quote_block({"who": "小秘", "text": "价格是 30 元", "context": [{"who": "用户", "text": "多少钱"}]})
    assert s.startswith("［引用 小秘 的话］价格是 30 元") and "用户：多少钱" in s
    d = B.parse_decision('好的\n{"done": true, "report": "ok"}')
    assert d == {"done": True, "next": "", "report": "ok", "ask_user": ""}
    assert B.parse_decision("not json")["done"] is False


def test_codex_listed_but_not_creatable(tmp_path):
    b, _ = make(tmp_path)
    try:
        b.create("codex")
    except ValueError as e:
        assert "还没接入" in str(e)
    else:
        raise AssertionError
    try:
        b.create("agy", "", "../etc")
    except ValueError:
        pass
    else:
        raise AssertionError


def test_chat_and_frontend_know_quotes_and_bots():
    chat = (ROOT / "services/api/aiw_api/chat.py").read_text(encoding="utf-8")
    assert 'ud["quote"] = quote' in chat and "def quote(self, mid" in chat
    main = (ROOT / "services/api/aiw_api/main.py").read_text(encoding="utf-8")
    assert "_bots.mount(app" in main


def test_endpoints_and_main_chat_quote(tmp_path):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    with TestClient(app) as cl:
        h = {"Authorization": "Bearer t"}
        r = cl.get("/api/bots", headers=h).json()
        assert {e["id"] for e in r["engines"]} == {"agy", "codex"} and r["convs"] == []
        c = cl.post("/api/bots/convs", json={"bot": "agy", "title": "t", "workdir": "~/workspace/x"}, headers=h).json()
        assert c["workdir"] == "~/workspace/x" and c["status"] == "idle"
        assert cl.post("/api/bots/convs", json={"bot": "codex"}, headers=h).status_code == 400
        assert cl.get(f"/api/bots/convs/{c['id']}", headers=h).json()["messages"] == []
        assert cl.patch(f"/api/bots/convs/{c['id']}", json={"title": "新名字"}, headers=h).json()["title"] == "新名字"
        # a quote in the main chat: the user message keeps it and the model reads it with its context
        chat = app.state.chat
        a = chat.add("user", "text", "ldok 首页多少行代码")
        b = chat.add("assistant", "text", "大约 1200 行")
        q = app.state.bots.quote(b["id"], chat)
        assert q["text"] == "大约 1200 行" and q["context"][0]["text"] == "ldok 首页多少行代码"
        m = chat.add("user", "text", "能减半吗", {"quote": q})
        t = chat.render(chat.get(m["id"]))
        assert t.startswith("［引用") and "大约 1200 行" in t and t.endswith("能减半吗")
        assert cl.delete(f"/api/bots/convs/{c['id']}", headers=h).json()["ok"]
