"""球球 expression assistant: the task state → emotion mapping (apps/web/mate-map.js) and the vendored Emotion Ball files."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
VENDOR = WEB / "vendor" / "emotionball"
FILES = ("rings.js", "emotions.js", "ball.js", "engine.js")   # load order


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_mapping_cases_in_node():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "mate_map_test.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "mate-map ok" in r.stdout


def test_every_mapped_id_exists_in_vendored_emotions():
    ids = set(re.findall(r"id: *'(\d\d)'", (VENDOR / "emotions.js").read_text(encoding="utf-8")))
    used = set(re.findall(r'"(\d\d)"', (WEB / "mate-map.js").read_text(encoding="utf-8")))
    assert used, "no ids found in mate-map.js"
    assert used <= ids, f"unknown emotion ids: {sorted(used - ids)}"
    for need in ("00", "01", "02", "30", "31", "32", "33", "34", "35", "36", "38", "39", "40", "41"):
        assert need in used


def test_vendor_keeps_licence_notice_and_non_commercial_note():
    assert "Emotion Ball Community License" in (VENDOR / "LICENSE").read_text(encoding="utf-8")
    notice = (VENDOR / "NOTICE.md").read_text(encoding="utf-8")
    assert "禁止任何商业用途" in notice and "commercial use is prohibited" in notice
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "sam70361/aora-bot" in readme and "NON-COMMERCIAL" in readme
    assert "非商用" in (ROOT / "README.md").read_text(encoding="utf-8")
    names = {p.name for p in VENDOR.iterdir()}
    assert set(FILES) <= names
    # only the engine files: not the upstream gallery site
    assert not any(n in names for n in ("app.js", "i18n.js", "hero-particles.js"))


def test_pages_load_emotion_ball_in_order_and_no_mood_mates():
    assert not (WEB / "vendor" / "moodmates").exists()
    for page in ("index.html", "mate.html"):
        html = (WEB / page).read_text(encoding="utf-8")
        assert "moodmates" not in html and "MoodMates" not in html
        order = [html.find(f"vendor/emotionball/{n}") for n in FILES]
        assert all(i > 0 for i in order) and order == sorted(order), f"{page}: wrong Emotion Ball load order"
        assert html.find("vendor/emotionball/engine.js") < html.find('src="mate-map.js"')
    js = (WEB / "mate.js").read_text(encoding="utf-8") + (WEB / "mate.html").read_text(encoding="utf-8")
    assert "EmotionBall.create(" in js and 'shape: "blob"' in js and "handleAIMessage" in js and "burst(" in js and "setActive(" in js
    css = (WEB / "mate.css").read_text(encoding="utf-8")
    # 3.8 look: amber ring + white face + black eyes everywhere (the dark 暗黑 backdrop and the red body are gone)
    assert "#09090b" not in css and "--mate-ring" in css and "--mate-fill: #ffffff" in css
    page = (WEB / "mate.html").read_text(encoding="utf-8")
    assert "--mate-ring" in page and "M.LOOK.color" in page and "MAP.LOOK.color" in (WEB / "mate.js").read_text(encoding="utf-8")
    mm = (WEB / "mate-map.js").read_text(encoding="utf-8")
    assert 'BALL_LOOK = { color: "#FFFFFF", eyeColor: "#1A1A1A" }' in mm


def test_ios_bundle_copies_emotion_ball_and_licence():
    sh = (ROOT / "apps" / "ios" / "build-ipa.sh").read_text(encoding="utf-8")
    assert "vendor/emotionball/*.js" in sh and "emotionball/LICENSE" in sh and "emotionball/NOTICE.md" in sh
    assert "moodmates" not in sh


def test_ios_mate_head_is_white_with_amber_ring():
    swift = (ROOT / "apps" / "ios" / "Sources" / "MateView.swift").read_text(encoding="utf-8")
    head = swift[swift.index("struct MateHead"):]
    assert "strokeBorder" in head and "Color.white" in head and "MateHead.eye" in head
    assert "0xF6 / 255" not in head   # the old cream ball on the amber disc
