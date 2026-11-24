"""v3.9.80: each engine has its own long-lived chat (单聊), its settings (点名字 › 配置), 新话题, and an overview the
引擎 page's avatars and the main chat's engine_status tool read."""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bots as B  # noqa: E402
from aiw_api.store import Store  # noqa: E402


def mk(tmp_path):
    async def shell(mid, cmd, timeout):
        return ""
    return B.Bots(Store(str(tmp_path / "t.db")), default_machine=lambda: "vm1", shell=shell)


def test_dm_is_one_per_engine(tmp_path):
    b = mk(tmp_path)
    c1 = b.dm("codex")
    c2 = b.dm("codex")
    assert c1["id"] == c2["id"] and c1["kind"] == "dm"
    assert c1["workdir"] == "~/workspace/codex-chat/main"
    assert b.dm("cue")["workdir"] == ""
    assert {c["kind"] for c in b.convs()} == {"dm"}


def test_cfg_defaults_for_dm_and_tasks(tmp_path):
    b = mk(tmp_path)
    b.set_engine_cfg("claude", model="opus", effort="high", workdir="~/workspace/mine")
    assert b.engine_cfg("claude") == {"model": "opus", "effort": "high", "machine_id": "", "workdir": "~/workspace/mine"}
    d = b.dm("claude")
    assert d["workdir"] == "~/workspace/mine"
    assert b.effective(d)["model"] == "opus" and b.effective(d)["effort"] == "high"
    t = b.create("claude", "x")
    assert (t.get("kind") or "") != "dm" and b.effective(t)["model"] == "opus"
    b.set_engine_cfg("claude", model="sonnet")   # v3.9.81: chats and tasks follow the default as it changes
    assert b.effective(b.conv(d["id"]))["model"] == "sonnet" and b.effective(b.conv(t["id"]))["model"] == "sonnet"
    own = b.create("claude", "y", model="haiku")
    assert b.effective(own)["model"] == "haiku"


def test_cfg_rejects_bad_values(tmp_path):
    b = mk(tmp_path)
    with pytest.raises(ValueError):
        b.set_engine_cfg("codex", effort="turbo")
    with pytest.raises(ValueError):
        b.set_engine_cfg("codex", workdir="~/a/../../etc")
    with pytest.raises(ValueError):
        b.set_engine_cfg("nope", model="x")


def test_new_topic_drops_session(tmp_path):
    b = mk(tmp_path)
    d = b.dm("codex")
    b.set_conv(d["id"], session="sess-1")
    c = asyncio.run(b.new_topic(d["id"]))
    assert not c.get("session") and not c.get("job")
    assert b.messages(d["id"])[-1]["data"].get("new_topic")


def test_overview_counts_running(tmp_path):
    b = mk(tmp_path)
    d = b.dm("codex")
    b.set_conv(d["id"], status="running")
    ov = b.overview()
    assert ov["engines"]["codex"]["busy"] == 1 and ov["engines"]["claude"]["busy"] == 0
    assert ov["running"][0]["kind"] == "dm"


def test_api_routes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    b = mk(tmp_path)
    app = FastAPI()
    B.mount(app, b, lambda: None)
    cl = TestClient(app)
    d = cl.post("/api/bots/dm/codex").json()
    assert d["kind"] == "dm"
    r = cl.put("/api/bots/engines/codex/config", json={"model": "gpt-5-codex", "effort": "low"})
    assert r.status_code == 200 and r.json()["cfg"]["model"] == "gpt-5-codex"
    assert cl.put("/api/bots/engines/codex/config", json={"effort": "bad"}).status_code == 400
    eng = {e["id"]: e for e in cl.get("/api/bots").json()["engines"]}
    assert eng["codex"]["dm"] == d["id"] and eng["codex"]["cfg"]["effort"] == "low"
    assert cl.post(f"/api/bots/convs/{d['id']}/new_topic").status_code == 200
    assert cl.post("/api/bots/dm/nope").status_code == 404
