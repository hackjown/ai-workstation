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
