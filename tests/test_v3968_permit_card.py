"""v3.9.68: an agy step its permission rules stopped becomes a confirm card in the AIW chat (允许这类命令 / 只这一轮全放开
/ 拒绝) with a push, like a task's 需要你确认. Real agy_bridge.py against tests/fixtures/fake_agy.py."""
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "tests"))
from test_v3957_bots import LLM, make, settle, stop_all  # noqa: E402

CMD = "soffice --headless --convert-to pdf deck.pptx"


class Chat:
    def __init__(self):
        self.msgs = {}

    def add(self, role, kind, text="", data=None, task_id=None):
        m = {"id": "msg-" + uuid.uuid4().hex[:8], "role": role, "kind": kind, "text": text, "data": dict(data or {})}
        self.msgs[m["id"]] = m
        return m

    def get(self, mid):
        return self.msgs.get(mid)

    def update(self, mid, text=None, data=None, **kw):
        self.msgs[mid]["data"].update(data or {})
        return self.msgs[mid]

    def cards(self):
        return [m for m in self.msgs.values() if m["kind"] == "engine_permit"]


class Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, kind, title, body="", **kw):
        self.sent.append({"kind": kind, "title": title, "body": body, **kw})
        return {}


def setup(tmp_path, llm=None):
    b, home = make(tmp_path, llm)
    b.chat, b.notifier = Chat(), Notifier()
    return b, home


async def test_blocked_turn_posts_a_confirm_card_and_a_push(tmp_path):
    b, home = setup(tmp_path)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        b.send(c["id"], "DENYCMD " + CMD)
        await settle(b, c["id"])
        [card] = b.chat.cards()
        d = card["data"]
        assert d["conv"] == c["id"] and d["command"] == CMD and d["rule"].startswith("command(regex:soffice") and not d["takeover"]
        assert b.conv(c["id"])["permit_card"] == card["id"]
        [n] = b.notifier.sent
        assert n["kind"] == "confirm" and "需要你确认" in n["title"] and CMD in n["body"] and n["deep"] == f"chat?m={card['id']}"
        # 拒绝: nothing widened, nothing reruns, the card says so
        await b.permit(c["id"], "deny")
        assert card["data"]["resolved"]["text"] == "已拒绝" and not b.conv(c["id"])["permit_card"]
        assert not b.queues.get(c["id"]) and b.messages(c["id"])[-1]["data"]["permit"] == "deny"
        bot = [m for m in b.messages(c["id"]) if m["role"] == "bot"][-1]
        assert bot["data"]["permitted"] == "deny"
    finally:
        stop_all(home)


async def test_card_allow_runs_and_a_new_message_makes_an_open_card_stale(tmp_path):
    b, home = setup(tmp_path)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        b.send(c["id"], "DENYCMD " + CMD)
        await settle(b, c["id"])
        first = b.chat.cards()[0]
        b.send(c["id"], "算了，换个做法")                   # the conversation moved on
        assert first["data"]["stale"] and not b.conv(c["id"])["permit_card"]
        await settle(b, c["id"])
        b.send(c["id"], "DENYCMD " + CMD)
        await settle(b, c["id"])
        second = b.chat.cards()[-1]
        assert second is not first and not second["data"].get("stale")
        await b.permit(c["id"], "allow")
        assert second["data"]["resolved"]["text"] == "已允许这类命令"
        await settle(b, c["id"])
        assert [m for m in b.messages(c["id"]) if m["role"] == "bot"][-1]["data"]["status"] == "done"
    finally:
        stop_all(home)


async def test_takeover_waiting_posts_the_card(tmp_path):
    llm = LLM([json.dumps({"done": True, "report": "完成了"})])
    b, home = setup(tmp_path, llm)
    try:
        c = b.create("agy", "ppt", str(tmp_path / "proj"))
        tk = b.takeover(c["id"], "DENYCMD " + CMD, 5)["takeover"]
        b.set_conv(c["id"], takeover={**tk, "rerouted": True})
        await settle(b, c["id"], 60)
        [card] = b.chat.cards()
        assert card["data"]["takeover"] and b.conv(c["id"])["takeover"]["waiting"] == "permit"
        await b.permit(c["id"], "deny")
        assert card["data"]["resolved"]["text"] == "已拒绝" and not b.conv(c["id"])["takeover"]
    finally:
        stop_all(home)


def test_clients_render_the_card():
    js = (ROOT / "apps/web/home.js").read_text(encoding="utf-8")
    assert 'm.kind === "engine_permit"' in js and "hEPermit(" in js and "'deny'" in js and "只这一轮全放开" in js
    hc = (ROOT / "apps/ios/Sources/HomeChat.swift").read_text(encoding="utf-8")
    assert 'case "engine_permit"' in hc and "struct EnginePermitRow" in hc and '["mode": mode]' in hc
    ev = (ROOT / "apps/ios/Sources/EngineView.swift").read_text(encoding="utf-8")
    assert '["mode": "deny"]' in ev and "aiw_open_engine_conv" in ev
    assert "btPermit('deny')" in (ROOT / "apps/web/bots.js").read_text(encoding="utf-8")
