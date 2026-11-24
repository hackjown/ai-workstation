"""v3.9.69: engine avatars are round with AIW's amber ring and the engine's real logo, on the web and on iOS."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDS = ["agy", "codex", "claude", "codebuddy", "grok"]


def test_logo_files_exist():
    for i in IDS:
        svg = (ROOT / "apps/web/engines" / f"{i}.svg").read_text()
        assert svg.lstrip().startswith("<!--") and "<svg" in svg and "currentColor" not in svg, i
        png = (ROOT / "apps/ios/engines" / f"eng-{i}.png").read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n", i
    assert "MIT" in (ROOT / "apps/web/engines/LICENSE").read_text()


def test_web_uses_round_logo_avatar():
    js = (ROOT / "apps/web/bots.js").read_text()
    css = (ROOT / "apps/web/bots.css").read_text()
    assert "function btAv(" in js and 'src="engines/${id}.svg"' in js
    assert js.count("${btAv(") >= 4   # v3.9.80: + avatar bar, task picker, settings
    assert js.count('<span class="bt-av') == 2   # helper + the group count
    assert "border-radius:50%" in css and "border:1.5px solid var(--bt-c" in css   # v3.9.80


def test_ios_engine_avatar():
    sw = (ROOT / "apps/ios/Sources/EngineView.swift").read_text()
    assert "struct EngineAvatar: View" in sw and 'inDirectory: "engines"' in sw
    assert sw.count("EngineAvatar(bot:") >= 4   # list row, reply, group reply, new-conversation picker
    assert "Circle().strokeBorder(engineColor(id, cs)" in sw   # v3.9.80: its own colour, amber is AIW's
    assert "cp engines/eng-*.png engines/LICENSE $APP/engines/" in (ROOT / "apps/ios/build-ipa.sh").read_text()
