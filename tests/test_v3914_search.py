"""v3.9.14: in-app search (search.py), session_search tool, memory snapshot/consolidation, vendored skills."""
import asyncio
import json
import time
from pathlib import Path

from aiw_api import memory as M
from aiw_api import search as S
from aiw_api.store import Store
from aiw_api import chat as C
from aiw_api import uploads as U

ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    C.ensure_tables(st)
    U.ensure_table(st)
    M.ensure_tables(st)
    return st


def _msg(st, mid, role, text, ts):
    st._x("INSERT INTO chat_messages (id, conv, role, kind, text, data, task_id, created_at, updated_at) "
          "VALUES (?,?,?,?,?,?,?,?,?)", (mid, "main", role, "text", text, "{}", None, ts, ts))


def test_chinese_search_long_short_and_incremental(tmp_path):
    st = _store(tmp_path)
    s = S.Search(st)
    _msg(st, "m1", "user", "帮我把海口三房两厅的户型图做成装修设计器", 1000)
    _msg(st, "m2", "assistant", "好的，户型图已经识别出 11 个房间", 1001)
    _msg(st, "m3", "user", "京东上那个显卡降价了吗", 1002)
    st.create_task({"id": "task-1", "prompt": "【技能：house-plan-designer】海口户型图转 3D 漫游", "machine_id": "vm",
                    "status": "succeeded"})
    st.update_task("task-1", "succeeded", "交付 海口三房两厅-全景漫游.html")
    r = s.search("装修设计器")            # >= 3 chars: FTS trigram
    assert [x["id"] for x in r["results"]] == ["m1"] and r["results"][0]["open"] == "chat?m=m1"
    r = s.search("户型")                  # 2 chars: LIKE fallback, hits chat and task
    ids = {x["id"] for x in r["results"]}
    assert {"m1", "m2", "task-1"} <= ids
    r = s.search("户型 全景漫游")          # AND of a short and a long term
    assert [x["id"] for x in r["results"]] == ["task-1"] and r["results"][0]["open"] == "task/task-1"
    assert "技能" not in r["results"][0]["title"]   # the skill marker is stripped
    assert s.search("显卡", ["task"])["results"] == []
    # a later message is picked up by the next search without any hook
    _msg(st, "m4", "user", "显卡买好了", time.time())
    s._last_sync = 0
    assert {x["id"] for x in s.search("显卡")["results"]} == {"m3", "m4"}
    # an edited message is re-indexed, not duplicated
    st._x("UPDATE chat_messages SET text=?, updated_at=? WHERE id='m4'", ("显卡退货了", time.time() + 1))
    s._last_sync = 0
    hits = s.search("显卡")["results"]
    assert len(hits) == 2 and any("退货" in h["snippet"] for h in hits)


def test_files_and_secrets(tmp_path):
    st = _store(tmp_path)
    s = S.Search(st)
    st._x("INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("u1", "报价单.xlsx", "/app/workspace/uploads/报价单.xlsx", 10, "file", "t1", "task-9", None, None, 1))
    r = s.search("报价单")
    assert r["results"] and r["results"][0]["kind"] == "file" and r["results"][0]["open"] == "task/task-9"
    _msg(st, "m9", "user", "我的密码: hunter2abc 别忘了", 5)
    s._last_sync = 0
    assert s.search("hunter2abc")["results"] == []


def test_tool_and_context(tmp_path):
    st = _store(tmp_path)
    s = S.Search(st)
    for i in range(10):
        _msg(st, f"c{i}", "user" if i % 2 == 0 else "assistant", f"第{i}句 关于小红书运营", 100 + i)
    schema, call = s.tool()
    assert schema["name"] == "session_search" and call.max_result >= 3000
    out = asyncio.run(call({"query": "小红书运营"}))
    assert "chat:c" in out
    ctx = asyncio.run(call({"open": "chat:c5"}))
    assert "→" in ctx and "第5句" in ctx and "第0句" in ctx
    assert "没有搜到" in asyncio.run(call({"query": "完全不存在的词语"}))


def test_api_mounted():
    src = (ROOT / "services/api/aiw_api/main.py").read_text()
    assert "_search.mount(app, srch, auth)" in src and "chat.search = srch" in src and "sessions.search = srch" in src
    assert 'extra["session_search"]' in (ROOT / "services/api/aiw_api/sessions.py").read_text()


class _LLM:
    def __init__(self, out):
        self.out, self.calls = out, []

    async def complete(self, system, user, max_tokens=800):
        self.calls.append((system, user))
        return self.out


def test_memory_snapshot_stable_and_consolidate(tmp_path):
    st = _store(tmp_path)
    mem = M.Memory(st, None, llm=None)
    a = mem.add("用户叫小黄", "profile", "user")
    time.sleep(0.01)
    b = mem.add("用户住在海口", "profile", "user")
    snap1 = mem.profile_block()
    mem.apply([{"op": "same", "id": a["id"]}])          # bumps updated_at
    assert mem.profile_block() == snap1 and snap1.index("小黄") < snap1.index("海口")
    mem.add("订酒店只要能免费取消的", "decision", "user")
    assert "免费取消" not in mem.profile_block() and "免费取消" in mem.facts_block("订酒店")
    # consolidation: only past the caps, merges into one, old rows superseded
    for i in range(M.PROFILE_CAP + 2):
        mem.add(f"用户喜欢第{i}种颜色的杯子吧", "profile", "user")
    assert mem.needs_consolidation()
    ids = [r["id"] for r in mem.items(kind="profile")][:3]
    llm = _LLM(json.dumps({"ops": [{"op": "merge", "ids": ids, "kind": "profile", "text": "用户喜欢各种颜色的杯子"}]}))
    mem.llm = lambda _k: llm
    done = asyncio.run(mem.consolidate())
    assert done and done[0]["op"] == "merge"
    active = {r["id"] for r in mem.items(kind="profile")}
    assert not (set(ids) & active) and done[0]["id"] in active
    assert not mem.needs_consolidation()   # once a day


def test_vendored_skills_and_routing():
    from agent_runtime import skills as sk
    for sid in ("skill-creator", "webapp-testing", "frontend-design"):
        d = ROOT / "skills" / sid
        assert (d / "LICENSE").exists() and "Apache" in (d / "LICENSE").read_text()
        assert "anthropics/skills" in (d / "NOTICE.md").read_text()
        man = (d / "MANIFEST").read_text().split()
        assert "SKILL.md" in man and all((d / f).exists() for f in man)
        assert "AIW 内置版说明" in (d / "SKILL.md").read_text()
        assert any(s["id"] == sid for s in sk.CATALOG)
    assert sk.auto("把刚才的流程存成技能") == ["skill-creator"]
    assert sk.auto("帮我做一个产品落地页") == ["frontend-design"]
    assert "webapp-testing" in sk.auto("测试一下这个网页有没有报错")
    assert "frontend-design" not in sk.auto("户型图做成3D漫游网页")
    assert sk.auto("技能列表") == []
    for sid in ("docx", "xlsx", "pptx", "pdf"):   # source-available, never in the public repo
        assert not (ROOT / "skills" / sid).exists()
    sh = (ROOT / "deploy/vm/aiw-skill-setup.sh").read_text()
    assert "playwright" in sh and "MANIFEST" in sh


def test_http_search_end_to_end(tmp_path):
    import os
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    os.makedirs(s.artifacts_dir, exist_ok=True)
    app = build_app(s)
    c, H = TestClient(app), {"Authorization": "Bearer t"}
    st = app.state.search.store
    _msg(st, "h1", "user", "上次那个全景漫游怎么分享给朋友", 10)
    r = c.get("/api/search", params={"q": "全景漫游"}, headers=H).json()
    assert r["total"] == 1 and r["results"][0]["id"] == "h1"
    assert c.get("/api/search", params={"q": "全景"}, headers=H).json()["total"] == 1
    ctx = c.get("/api/search/context", params={"kind": "chat", "id": "h1"}, headers=H).json()
    assert ctx["items"][0]["hit"]
    assert c.get("/api/search", params={"q": "x"}).status_code == 401
    assert c.post("/api/search/reindex", headers=H).json()["indexed"] >= 1
    tools = __import__("asyncio").run(app.state.chat.tools())
    assert "session_search" in tools


def test_web_and_ios_wiring():
    web = ROOT / "apps" / "web"
    idx = (web / "index.html").read_text(encoding="utf-8")
    assert 'id="searchBtn"' in idx and '<script src="search.js"></script>' in idx and 'href="search.css"' in idx
    js = (web / "search.js").read_text(encoding="utf-8")
    assert "/api/search?q=" in js and "aiwOpen" in js and "openSearch" in js
    assert "hJumpTo" in (web / "home.js").read_text(encoding="utf-8")
    assert 'q.get("m")' in (web / "deeplink.js").read_text(encoding="utf-8")
    ios = ROOT / "apps" / "ios" / "Sources"
    hc = (ios / "HomeChat.swift").read_text(encoding="utf-8")
    top = hc[hc.index("ToolbarItemGroup(placement: .navigationBarTrailing)"):][:600]
    assert "openSheet(.search)" in top and "magnifyingglass" in top and 'square.and.pencil").font(.system(size: 17)' not in top
    assert 'Label("新建任务", systemImage: "square.and.pencil")' in hc      # still in the "+" menu
    assert "loadUntil" in hc and "proxy.scrollTo(id, anchor: .center)" in hc
    tv = (ios / "ThreadsView.swift").read_text(encoding="utf-8")
    assert "case .search: SearchSheet(close: close)" in tv and 'userInfo: info' in tv
    assert "/api/search?" in (ios / "SearchView.swift").read_text(encoding="utf-8")
