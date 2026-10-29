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


def test_381_ring_is_the_thick_pre_38_disc():
    """3.8.1: the 3.8 ring (~9 % of the radius) was too thin; the band is the old amber disc again: 27.6 % of the radius
    (the ball at 72.4 %: old 9 % inset x the engine's ball at 88.26 % of its SVG), at least 2.5px, with the old glow."""
    css = (WEB / "mate.css").read_text(encoding="utf-8")
    page = (WEB / "mate.html").read_text(encoding="utf-8")
    for src in (css, page):
        assert "max(13.8%, 2.5px)" in src and "max(27.6%, 2.5px)" in src and "/ 0.8826" in src
        assert "rgba(255, 214, 150, 0.38)" in src or "rgba(255,214,150,.38)" in src   # the old disc's warm glow
        assert "89.5%" not in src and "90.5%" not in src   # the thin 3.8 ring stops
    assert "--mate-pad: 6%" not in css and "--mate-pad: 11%" not in css   # every size follows the ring
    home = (WEB / "home.css").read_text(encoding="utf-8")
    assert "--mate-pad:10%" not in home
    swift = (ROOT / "apps" / "ios" / "Sources" / "MateView.swift").read_text(encoding="utf-8")
    head = swift[swift.index("struct MateHead"):]
    assert "max(2.5, s * 0.138)" in head and "s - 2 * MateHead.ringWidth(s)" in head
    assert "s * 0.05" not in head and "ringDark" not in head   # the thin 3.8 stroke and its dark variant are gone
    # the engine's blob ball really is 88.26 % of its viewBox (114.27 of 129.5), so the ball edge meets the ring
    rings = (WEB / "vendor" / "emotionball" / "rings.js").read_text(encoding="utf-8")
    assert "viewBox" in (WEB / "vendor" / "emotionball" / "ball.js").read_text(encoding="utf-8") and "[228.54,114.27]" in rings


def test_ios_version_at_least_381():   # a real "at least" check, so later builds pass too
    import plistlib
    for p in ("Info.plist", "Share/Info.plist"):
        pl = plistlib.loads((ROOT / "apps" / "ios" / p).read_bytes())
        assert tuple(int(x) for x in pl["CFBundleShortVersionString"].split(".")) >= (3, 8, 1)
        assert int(pl["CFBundleVersion"]) >= 22
