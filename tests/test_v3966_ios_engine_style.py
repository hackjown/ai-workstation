"""v3.9.66 iOS 引擎: your messages select like the chat; engine names in their own colours (same as the web page)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "apps/ios/Sources/EngineView.swift").read_text(encoding="utf-8")
CSS = (ROOT / "apps/web/bots.css").read_text(encoding="utf-8")


def test_user_messages_are_selectable_text():
    assert "textSelection(.enabled)" not in SRC
    assert SRC.count("engineUserText(m.s(\"text\"))") == 2
    assert "func engineUserText" in SRC and "SelectableText(text: uiAttributed(inlineMD(text)" in SRC


def test_engine_colours_match_the_web():
    for bot, hexc in (("agy", "c026d3"), ("codex", "10a37f"), ("claude", "d97757"), ("codebuddy", "2b6cff")):
        assert f".bt-name.{bot} {{ color:#{hexc}; }}" in CSS
        assert re.search(rf'case "{bot}": return Color\(.*// #{hexc}', SRC), bot
    assert 'case "grok"' in SRC
    assert "foregroundColor(engineColor(conv.s(\"bot\")" in SRC and "foregroundColor(engineColor(m.s(\"who\"), cs))" in SRC
    g = SRC[SRC.index("struct EngineGroupView"):]
    assert "@Environment(\\.colorScheme) private var cs" in g[:600]
