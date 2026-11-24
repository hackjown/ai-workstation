"""v3.9.81: from the main chat — 「@agy 查…」 goes to agy's own chat, 「@agy 新建任务…」 makes a task,
「@agy 把 ldok 增加导出功能」 carries on the ldok task, and 「把 agy 默认模型设成 3.8 flash」 sets its default."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bots as B  # noqa: E402
from aiw_api import bot_groups as G  # noqa: E402
from aiw_api.store import Store  # noqa: E402


class FakeChat:
    def __init__(self):
        self.said = []

    def add(self, role, kind, text, data=None):
        self.said.append(text)


def mk(tmp_path):
    async def shell(mid, cmd, timeout):
        return ""
    b = B.Bots(Store(str(tmp_path / "t.db")), default_machine=lambda: "vm1", shell=shell)
    b.send = lambda cid, text, role="user", **kw: b.add(cid, role, text, {"via": kw["via"]} if kw.get("via") else {})
    chat = FakeChat()
    d = G.Dispatch(b, G.Groups(b), chat)
    return b, d, chat


def test_parse_modes():
    p = G.parse_dispatch("@agy 查一下北京天气的代码")
    assert p["mode"] == "chat" and p["bots"] == ["agy"]
    p = G.parse_dispatch("@agy新建任务，完成 ldok 的登录页")
    assert p["mode"] == "new" and p["task"].startswith("完成")
    assert G.parse_dispatch("@agy 继续做")["mode"] == "continue"
    assert G.parse_dispatch("@agy 完成 ldok 后端对接的任务")["mode"] == "continue"
    assert G.parse_dispatch("@agy 在 ~/workspace/x 里查一下")["mode"] == "chat"
    assert G.parse_dispatch("@codex 和 claude 一起做 ldok")["mode"] == "new"


def test_chat_goes_to_dm_and_named_task_wins(tmp_path):
    b, d, _ = mk(tmp_path)
    t = b.create("agy", "ldok 重建", "~/workspace/ldok")
    r = asyncio.run(d.run(G.parse_dispatch("@agy 查一下天气的代码")))
    assert b.conv(r["data"]["bot_conv"])["kind"] == "dm" and "单聊" in r["text"]
    r = asyncio.run(d.run(G.parse_dispatch("@agy 把 ldok 增加导出功能")))   # no 新建任务 / 继续: its own chat
    assert b.conv(r["data"]["bot_conv"])["kind"] == "dm"
    r = asyncio.run(d.run(G.parse_dispatch("@agy 继续 ldok，增加导出功能")))
    assert r["data"]["bot_conv"] == t["id"]
    r = asyncio.run(d.run(G.parse_dispatch("@agy 新建任务，做一个天气小程序")))
    c = b.conv(r["data"]["bot_conv"])
    assert c["id"] != t["id"] and c.get("kind") != "dm"


def test_dm_reports_only_what_chat_asked(tmp_path):
    b, d, chat = mk(tmp_path)
    r = asyncio.run(d.run(G.parse_dispatch("@agy 查一下天气的代码")))
    cid = r["data"]["bot_conv"]
    m = b.add(cid, "bot", "天气代码在这里", {"status": "done"})
    d._on_bot("message", m)
    assert chat.said and "天气代码" in chat.said[-1]
    n = len(chat.said)
    b.add(cid, "user", "从引擎页自己问的", {})
    m = b.add(cid, "bot", "引擎页的回答", {"status": "done"})
    d._on_bot("message", m)
    assert len(chat.said) == n


async def test_chat_tools_config_and_continue(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    chat = app.state.chat
    bots = chat.engines.bots

    async def models(bot, mid, refresh=False):
        return {"groups": [{"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash"},
                           {"id": "gemini-3.8-pro", "label": "Gemini 3.8 Pro"}], "efforts": []}
    bots.models = models
    tools = await chat.tools()
    r = await chat.run_tool(tools, "engine_config", {"engine": "agy", "model": "3.8 flash"})
    assert "Gemini 3.8 Flash" in r and bots.engine_cfg("agy")["model"] == "gemini-3.8-flash"
    assert "几个模型" in await chat.run_tool(tools, "engine_config", {"engine": "agy", "model": "3.8"})
    assert "gemini-3.8-flash" in await chat.run_tool(tools, "engine_config", {"engine": "agy"})
    t = bots.create("agy", "ldok 重建", "~/workspace/ldok")
    got = []

    async def run(p, attachments=None, origin=""):
        got.append(p)
        return {"text": "ok", "data": {}}
    chat.engines.run = run
    await chat._engine_task({"mode": "continue", "find": "ldok", "task": "增加导出功能"})
    assert got[-1]["conv"] == t["id"] and got[-1]["bots"] == ["agy"]
    await chat._engine_task({"mode": "continue", "task": "把 ldok 增加导出功能"})
    assert got[-1]["conv"] == t["id"]
    await chat._engine_task({"engines": ["agy"], "task": "查一下天气的代码"})
    assert got[-1]["mode"] == "chat" and not got[-1]["conv"]
    assert "没找到" in await chat._engine_task({"mode": "continue", "find": "不存在", "task": "x"})
