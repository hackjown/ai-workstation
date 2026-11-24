"""v3.9.64: the main chat finds engine conversations (engine_convs) and carries on in one (engine_task conv=…)."""
import json

import pytest

from test_v3961_groups import G, make, stop_all, until  # noqa: F401  (same fixtures: fake CLIs, real bridge)


async def test_find_convs_by_title_content_and_engine(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    a = b.create("agy", "ldok后端对接", str(tmp_path / "ldok"))
    b.add(a["id"], "user", "把 ldok 的登录接口接到后端")
    b.add(a["id"], "bot", "出现这种情况主要有两方面的根本原因：鉴权头没带上")
    c = b.create("codex", "别的事", str(tmp_path / "x"))
    b.add(c["id"], "bot", "鉴权已经修好")
    rows = b.find_convs("ldok 后端")
    assert [r["id"] for r in rows] == [a["id"]] and rows[0]["bot"] == "agy" and rows[0]["messages"] == 2
    assert [r["id"] for r in b.find_convs("鉴权")] == [c["id"], a["id"]]      # content, newest first
    assert "鉴权头" in b.find_convs("鉴权", bot="agy")[0]["match"]
    assert b.find_convs("鉴权", bot="grok") == []
    assert len(b.find_convs("")) == 2


async def test_dispatch_carries_on_in_the_found_conversation(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    try:
        c = b.create("codex", "ldok后端对接", str(tmp_path / "ldok"), origin="")
        r = await d.run({"bots": ["agy"], "task": "把剩下的对接做完", "workdir": "", "manage": False, "cont": False,
                         "conv": c["id"]}, origin="chat")
        assert r["data"]["bot_conv"] == c["id"] and "ldok后端对接" in r["text"] and "Codex" in r["text"]
        assert len(b.convs()) == 1 and b.conv(c["id"])["origin"] == "chat"
        await until(lambda: any("Codex 回复" in m["text"] for m in chat.msgs))
        with pytest.raises(ValueError):
            await d.run({"bots": ["codex"], "task": "x", "conv": "bc-nope"})
    finally:
        stop_all(home)


async def test_naming_the_conversation_title_reuses_it(tmp_path):
    b, g, d, chat, home, llm = make(tmp_path)
    try:
        c = b.create("codex", "ldok后端对接", str(tmp_path / "ldok"))
        r = await d.run(G.parse_dispatch("@codex 完成ldok后端对接的任务"), origin="m1")
        assert r["data"]["bot_conv"] == c["id"] and len(b.convs()) == 1
        r2 = await d.run(G.parse_dispatch("@codex 写个全新的东西"), origin="m2")
        assert r2["data"]["bot_conv"] != c["id"]
        await until(lambda: sum("Codex 回复" in m["text"] for m in chat.msgs) >= 2)
    finally:
        stop_all(home)


async def test_chat_tools(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    chat = app.state.chat
    bots = chat.engines.bots
    c = bots.create("agy", "ldok后端对接", "~/workspace/ldok")
    bots.add(c["id"], "user", "接后端")
    bots.add(c["id"], "bot", "接好了一半")
    tools = await chat.tools()
    assert "engine_convs" in tools and "engine_task" not in tools
    found = json.loads(await chat.run_tool(tools, "engine_convs", {"query": "ldok"}))
    assert found[0]["id"] == c["id"] and found[0]["title"] == "ldok后端对接"
    read = await chat.run_tool(tools, "engine_convs", {"conv": c["id"]})
    assert "ldok后端对接" in read and "接好了一半" in read and "~/workspace/ldok" in read
    assert "没有找到" in await chat.run_tool(tools, "engine_convs", {"query": "不存在的东西"})
    got = []

    async def run(p, attachments=None, origin=""):
        got.append(p)
        return {"text": "ok", "data": {}}
    chat.engines.run = run
    assert await chat._engine_task({"conv": c["id"], "task": "做完它", "manage": True}) == "ok"
    assert got[0]["bots"] == ["agy"] and got[0]["conv"] == c["id"] and got[0]["manage"] is True
    assert "没有这个引擎对话" in await chat._engine_task({"conv": "bc-x", "task": "y"})
