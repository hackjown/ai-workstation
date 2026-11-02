"""Long-term memory (memory.py): kinds, 记住/忘掉, background learning with duplicates and contradictions, the
secret filter, ranking (keywords or embeddings), the prompt block in the chat and in tasks, the planner keeping it,
and the /api/memories endpoints."""
import asyncio
import json
import os
import sqlite3

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from agent_runtime.planner import make_plan, plan_context
from aiw_api import memory as M
from aiw_api.store import Store
from llm_router import LLMRouter
from llm_router.router import LLMProvider

from test_chat import FakeChat, stack, wait_for  # noqa: F401  (fixture)


class Small(LLMProvider):
    name = "small"

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def decide(self, req):
        raise NotImplementedError

    async def complete(self, system, user, max_tokens=800):
        self.calls.append((system, user))
        return self.answers.pop(0) if self.answers else '{"ops": []}'


def mem_with(tmp_path, answers=()):
    store = Store(str(tmp_path / "m.db"))
    small = Small(answers)
    mem = M.Memory(store, None, llm=lambda role: LLMRouter([small]))
    return mem, small


# ------------------------------------------------------------------------------------------ pure parts
def test_secrets_are_recognised():
    for t in ("我的密码是 abc123", "验证码 482913", "github token ghp_abcdefghijklmnop", "sk-abcdefghijklmnopqrstu",
              "OpenAI API key 是 xyz", "支付密码 666666", "code: 123456", "Bearer eyJhbGciOiJIUzI1NiJ9abc"):
        assert M.looks_secret(t), t
    for t in ("用户住在杭州", "周报放在 ~/workspace/reports，用 Word 格式", "以后订酒店只要可退款的", "用户喜欢喝美式咖啡"):
        assert not M.looks_secret(t), t


def test_kind_guess_and_ops_parsing():
    assert M.guess_kind("我住在杭州") == "profile"
    assert M.guess_kind("以后订酒店只要可退款的") == "decision"
    assert M.guess_kind("周报放在 ~/workspace/reports") == "fact"
    assert M.parse_ops('<think>x</think>{"ops": [{"op": "add", "text": "a"}]}') == [{"op": "add", "text": "a"}]
    assert M.parse_ops("no json") == []


def test_old_database_gets_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, text TEXT, source TEXT, created_at REAL)")
    c.execute("INSERT INTO memories VALUES ('m1', '周报用 Word 格式', 'user', 1.0)")
    c.commit(); c.close()
    mem = M.Memory(Store(str(db)))
    row = mem.get("m1")
    assert row["kind"] == "fact" and row["superseded"] == "" and "embedding" not in row
    assert [m["id"] for m in mem.items()] == ["m1"]


# ------------------------------------------------------------------------------------------ 记住 / 忘掉
async def test_remember_and_forget_commands(tmp_path):
    mem, _ = mem_with(tmp_path)
    r = await mem.command("记住：周报放在 ~/workspace/reports，用 Word 格式")
    assert r.startswith("记住了") and mem.items()[0]["text"].startswith("周报放在")
    assert (await mem.command("帮我记住我住在上海")).startswith("记住了")
    assert mem.items(kind="profile")[0]["text"] == "我住在上海"
    # the newer word on the same subject wins; the old one is kept but superseded
    await mem.command("记住我住在杭州")
    active = [m["text"] for m in mem.items()]
    assert "我住在杭州" in active and "我住在上海" not in active
    old = next(m for m in mem.items(include_superseded=True) if m["text"] == "我住在上海")
    assert old["superseded"]
    # a repeat is not a second row
    await mem.command("记住：周报放在 ~/workspace/reports，用 Word 格式")
    assert sum(1 for m in mem.items() if m["text"].startswith("周报")) == 1
    # secrets are refused
    r = await mem.command("记住我的邮箱密码是 hunter2")
    assert "不会记" in r and not any("hunter2" in m["text"] for m in mem.items(include_superseded=True))
    # 忘掉 deletes what it names
    r = await mem.command("忘掉周报")
    assert r.startswith("已忘掉 1 条") and not any(m["text"].startswith("周报") for m in mem.items())
    assert "没有关于" in await mem.command("忘掉火星基地")
    assert await mem.command("你还记得我住哪吗") is None


# ------------------------------------------------------------------------------------------ learning
async def test_learning_adds_dedupes_replaces_and_drops_secrets(tmp_path):
    mem, small = mem_with(tmp_path)
    small.answers.append(json.dumps({"ops": [
        {"op": "add", "kind": "fact", "text": "用户喜欢喝美式咖啡"},
        {"op": "add", "kind": "profile", "text": "用户住在上海"},
        {"op": "add", "kind": "fact", "text": "用户的验证码是 123456"}]}, ensure_ascii=False))
    done = await mem.learn({"id": "msg-1", "text": "我在上海，平时只喝美式"}, [{"kind": "text", "text": "好的"}])
    assert len(done) == 2
    texts = {m["text"]: m for m in mem.items()}
    assert set(texts) == {"用户喜欢喝美式咖啡", "用户住在上海"} and texts["用户住在上海"]["kind"] == "profile"
    assert texts["用户喜欢喝美式咖啡"]["source"] == "msg-1"
    # the model saw the related memories with their ids and says one of them changed
    old = texts["用户住在上海"]["id"]
    small.answers.append(json.dumps({"ops": [
        {"op": "replace", "id": old, "kind": "profile", "text": "用户搬到了杭州住"},
        {"op": "add", "kind": "fact", "text": "用户喜欢喝美式咖啡"},
        {"op": "same", "id": texts["用户喜欢喝美式咖啡"]["id"]}]}, ensure_ascii=False))
    await mem.learn({"id": "msg-2", "text": "我上个月搬到杭州了，住在西湖边"}, [{"kind": "text", "text": "恭喜"}])
    assert old in small.calls[-1][0]   # the existing memories were in the prompt
    active = [m["text"] for m in mem.items()]
    assert "用户搬到了杭州住" in active and "用户住在上海" not in active
    assert active.count("用户喜欢喝美式咖啡") == 1
    assert mem.get(old)["superseded"] == next(m["id"] for m in mem.items() if m["text"] == "用户搬到了杭州住")
    # nothing to learn from a one-word message or a memory command
    n = len(small.calls)
    await mem.learn({"text": "嗯"}, [])
    await mem.learn({"text": "记住我喜欢猫"}, [])
    assert len(small.calls) == n


async def test_learning_survives_a_model_error(tmp_path):
    store = Store(str(tmp_path / "m.db"))

    class Down(Small):
        async def complete(self, system, user, max_tokens=800):
            raise RuntimeError("gateway down")
    mem = M.Memory(store, None, llm=lambda role: LLMRouter([Down([])]))
    assert await mem.learn({"text": "我住在杭州西湖边"}, []) == []


# ------------------------------------------------------------------------------------------ ranking + prompt
async def test_prompt_block_has_profile_and_top_8_relevant(tmp_path):
    mem, _ = mem_with(tmp_path)
    mem.store.kv_set("profile", {"name": "伟少", "about": "做装修设计"})
    mem.add("我住在杭州", "profile")
    for i in range(12):
        mem.add(f"无关的记忆第{i}条：喜欢第{i}种颜色", "fact")
    mem.add("订酒店只要可以免费取消的", "decision")
    mem.add("出差订机票偏好靠窗座位", "fact")
    b = mem.prompt_block("帮我订下周去北京的酒店和机票")
    assert b.startswith(M.OPEN) and b.endswith(M.CLOSE)
    assert "称呼：伟少" in b and "做装修设计" in b and "我住在杭州" in b
    lines = [ln for ln in b.splitlines() if ln.startswith("- [")]
    assert len(lines) == 8
    assert "免费取消" in lines[0] or "免费取消" in lines[1]
    assert any("靠窗" in ln for ln in lines[:2])
    assert mem.prompt_block("") .count("- [") == 8


async def test_embeddings_rank_when_a_model_is_set(tmp_path):
    mem, _ = mem_with(tmp_path)
    vec = {"苹果": [1, 0, 0], "香蕉": [0, 1, 0], "汽车": [0, 0, 1]}

    async def embedder(texts):
        out = []
        for t in texts:
            out.append(next((v for k, v in vec.items() if k in t), [0.3, 0.3, 0.3]))
        return out
    mem.embedder = embedder
    mem.add("用户每天早上吃一个苹果", "fact")
    mem.add("用户开一辆汽车上班", "fact")
    mem.add("用户不吃香蕉", "fact")
    await asyncio.sleep(0)
    await mem.prepare("早餐给我推荐点水果，苹果怎么样")
    assert all(json.loads(r["embedding"]) for r in mem.active_rows())
    top = mem.search("早餐给我推荐点水果，苹果怎么样", 1)
    assert top[0]["text"] == "用户每天早上吃一个苹果"


# ------------------------------------------------------------------------------------------ planner keeps memory
class PlanLLM(LLMProvider):
    name = "plan"

    def __init__(self):
        self.users = []

    async def decide(self, req):
        raise NotImplementedError

    async def complete(self, system, user, max_tokens=800):
        self.users.append(user)
        return '["打开网站", "订酒店", "确认结果并汇报"]'


async def test_planner_keeps_the_memory_block_of_a_long_context():
    block = "USER MEMORY (facts):\n[MEMORY]\n关于用户的长期记忆：\n画像：称呼：伟少\n- [决定] 订酒店只要可以免费取消的\n[/MEMORY]"
    ctx = block + "\n\nEARLIER IN THE USER'S CHAT:\n" + ("- 用户：闲聊内容很多很多\n" * 400) + "- 用户：最后一句"
    bg = plan_context(ctx)
    assert "免费取消" in bg and "最后一句" in bg and bg.count("[MEMORY]") == 1
    assert len(bg) <= 1500 + 1200 + 10
    p = PlanLLM()
    steps = await make_plan(LLMRouter([p]), "订下周去北京的酒店", ctx)
    assert steps and "免费取消" in p.users[0] and "最后一句" in p.users[0]
    # an oversized block is cut but still closed
    big = "[MEMORY]\n" + "\n".join(f"- [事实] 很长的记忆 {i} " + "字" * 40 for i in range(60)) + "\n[/MEMORY]\n尾巴"
    bg = plan_context(big)
    assert bg.startswith("[MEMORY]") and "[/MEMORY]" in bg and bg.endswith("尾巴")
    assert plan_context(None) == "" and plan_context("只有背景") == "只有背景"


# ------------------------------------------------------------------------------------------ in the app
async def test_chat_uses_memory_and_tasks_get_it(stack):  # noqa: F811
    client, app, scripts, fake = stack
    r = (await client.post("/api/chat/send", json={"text": "记住：以后订酒店只要可以免费取消的", "stream": False})).json()
    assert r["messages"][-1]["text"].startswith("记住了") and r["messages"][-1]["data"].get("memory")
    assert not fake.chats   # no model reply for a memory command
    fake.replies.append("好的，我按免费取消来找。")
    await client.post("/api/chat/send", json={"text": "帮我看看北京的酒店", "mode": "chat", "stream": False})
    msgs = fake.chats[-1]["messages"]
    system, last = msgs[0]["content"], msgs[-1]["content"]
    # v3.9.14: the per-question memories ride on the last user turn, the system prompt stays cacheable
    assert "[MEMORY]" in last and "免费取消" in last and "免费取消" not in system
    ctx = app.state.chat.sessions.build_context(None, query="订酒店")
    assert "USER MEMORY" in ctx and "免费取消" in ctx
    # 忘掉 in the chat
    r = (await client.post("/api/chat/send", json={"text": "忘掉订酒店的事", "stream": False})).json()
    assert r["messages"][-1]["text"].startswith("已忘掉")
    assert (await client.get("/api/memories")).json() == []


async def test_memory_api(stack):  # noqa: F811
    client, app, scripts, fake = stack
    a = (await client.post("/api/memories", json={"text": "周报放在 ~/workspace/reports"})).json()
    assert a["kind"] == "fact" and a["source"] == "user"
    b = (await client.post("/api/memories", json={"text": "我叫伟少", "kind": "profile"})).json()
    assert (await client.post("/api/memories", json={"text": "我的密码是 123"})).status_code == 400
    assert (await client.post("/api/memories", json={"text": "x", "kind": "weird"})).status_code == 400
    e = (await client.put(f"/api/memories/{a['id']}", json={"text": "周报放在 ~/workspace/weekly", "kind": "decision"})).json()
    assert e["text"].endswith("weekly") and e["kind"] == "decision"
    assert (await client.put(f"/api/memories/{a['id']}", json={"text": "token abc"})).status_code == 400
    assert (await client.put("/api/memories/nope", json={"text": "x"})).status_code == 404
    lst = (await client.get("/api/memories")).json()
    assert {m["id"] for m in lst} == {a["id"], b["id"]} and all("embedding" not in m for m in lst)
    assert [m["id"] for m in (await client.get("/api/memories?kind=profile")).json()] == [b["id"]]
    assert (await client.get("/api/memories?q=周报")).json()[0]["id"] == a["id"]
    # superseded memories only show with all=true and can be restored
    mem = app.state.memory
    n = mem.add("我叫小伟", "profile", supersedes=b["id"])
    assert b["id"] not in {m["id"] for m in (await client.get("/api/memories")).json()}
    assert b["id"] in {m["id"] for m in (await client.get("/api/memories?all=true")).json()}
    assert (await client.post(f"/api/memories/{b['id']}/restore")).json()["superseded"] == ""
    assert (await client.delete(f"/api/memories/{n['id']}")).json()["ok"]
    assert (await client.delete(f"/api/memories/{n['id']}")).status_code == 404
