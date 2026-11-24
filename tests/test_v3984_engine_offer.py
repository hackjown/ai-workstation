"""v3.9.84: 「让 agy 改代码」 without @ is offered to agy (one tap), AIW never runs it itself; the engine chat takes
pasted screenshots like the main chat."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bot_groups as G  # noqa: E402


def test_implied_engine():
    assert G.implied_engine("好，那你让agy按照你的要求修改代码") == "agy"
    assert G.implied_engine("让 codex 修一下登录") == "codex"
    assert G.implied_engine("叫cbc写个测试") == "codebuddy"
    for t in ("查询agy的ldok开发进度", "agy 在做什么", "让agy查一下进度", "@agy 改代码", "你来改代码"):
        assert G.implied_engine(t) is None, t


def test_chat_routes_offer_before_tasks():
    src = (ROOT / "services/api/aiw_api/chat.py").read_text(encoding="utf-8")
    assert src.index("implied_engine(text)") < src.index("cands = self.recent_tasks()")
    assert "/to_engine" in src and "offer_engine_text" in src


def test_paste_goes_to_engine_chat():
    js = (ROOT / "apps/web/paste.js").read_text(encoding="utf-8")
    assert "btUpload(files)" in js and "#btPrompt" in js


def test_offer_goes_into_the_task_we_looked_at(tmp_path):
    import asyncio
    sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
    sys.path.insert(0, str(ROOT / "tests"))
    from aiw_api.chat import Chat
    from test_v3981_chat_routes import mk
    b, d, _ = mk(tmp_path)
    task = b.create("agy", "增加对115网盘的刮削", "~/workspace/kanju")
    ch = Chat.__new__(Chat)
    ch.engines = d
    msgs = {}

    class St:
        def _x(self, *a):
            return [{"id": "p", "role": "assistant", "kind": "text", "text": "方案：多源级联", "data": "{}", "seq": 1,
                     "created_at": 0, "updated_at": 0, "conv": "main"}]
    ch.store = St()

    def add(role, kind, text, data=None):
        m = {"id": f"m{len(msgs)}", "role": role, "kind": kind, "text": text, "data": data or {}, "seq": 9}
        msgs[m["id"]] = m
        return m
    ch.add, ch.get = add, msgs.get
    ch.update = lambda mid, data=None, **k: msgs[mid]["data"].update(data or {})
    ch._attachments_before = lambda m, look=4: []
    ch.note_engine_focus(b.conv(task["id"]))
    off = ch.engine_offer("agy", "好，那你让agy按照你的要求修改代码", {"seq": 5})
    assert off["data"]["offer_engine_conv"] == task["id"] and "115" in off["text"]
    asyncio.run(ch.to_engine(off["id"]))
    texts = [m["text"] for m in b.messages(task["id"])]
    assert any("多源级联" in t for t in texts)
    assert ch.engine_offer("codex", "让 codex 改", {"seq": 5})["data"].get("offer_engine_conv") is None


def test_offer_goes_into_the_dm_we_looked_at(tmp_path):
    sys.path.insert(0, str(ROOT / "tests"))
    from aiw_api.chat import Chat
    from test_v3981_chat_routes import mk
    b, d, _ = mk(tmp_path)
    dm = b.dm("agy")
    ch = Chat.__new__(Chat)
    ch.store = type("St", (), {"_x": lambda self, *a: []})()
    ch.add = lambda role, kind, text, data=None: {"text": text, "data": data or {}}
    ch.note_engine_focus(b.conv(dm["id"]))
    off = ch.engine_offer("agy", "让agy按你说的改", {"seq": 5})
    assert off["data"]["offer_engine_conv"] == dm["id"] and "单聊" in off["text"]
