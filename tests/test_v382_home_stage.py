"""3.8.2 iOS home: (A) while 球球 speaks, 球球 + its bubble are one group centred between the 聊天记录 button and the
input bar, the bubble under 球球 and never over it (IMG_2936); (B) "↑ 聊天记录" opens the chat at the newest message
on iOS and on the web (an older page loaded during the jump used to leave the view at old messages)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
V2 = (ROOT / "apps/ios/Sources/HomeV2.swift").read_text(encoding="utf-8")
HC = (ROOT / "apps/ios/Sources/HomeChat.swift").read_text(encoding="utf-8")
JS = (ROOT / "apps/web/home.js").read_text(encoding="utf-8")

EDGE, GAP, LINE_H, PAD_V = 14.0, 30.0, 20.0, 20.0


def group(bw, bh, d, area):
    """Python twin of StageLayout.group (HomeV2.swift); area = (x, y, w, h)."""
    ax, ay, aw, ah = area
    min_y, max_y = ay + EDGE, ay + ah - EDGE
    room = max(0.0, (max_y - min_y) - d - GAP)
    h, lines = bh, None
    if h > room:
        n = max(1, int((room - PAD_V) // LINE_H))
        lines, h = n, min(h, PAD_V + n * LINE_H)
    gh = d + GAP + h
    top = max(min_y, ay + (ah - gh) / 2)
    c = (ax + aw / 2, top + d / 2)
    x = max(ax + EDGE, min(ax + aw / 2 - bw / 2, ax + aw - EDGE - bw))
    return c, (x, top + d + GAP, bw, h), lines


def test_swift_has_the_same_group_math():
    body = V2[V2.index("static func group(bubble b: CGSize, d: CGFloat, area: CGRect)"):]
    body = body[:body.index("static func tail(")]
    for frag in ("let room = max(0, (maxY - minY) - d - gap)", "let groupH = d + gap + h",
                 "let top = max(minY, area.minY + (area.height - groupH) / 2)", "let c = CGPoint(x: area.midX, y: top + d / 2)",
                 "y: top + d + gap", "side: .below"):
        assert frag in body, frag
    stage = V2[V2.index("struct HomeStage: View"):]
    assert "StageLayout.group(bubble:" in stage and "area.height * 0.52" not in stage
    # the bubble is anchored by its top-left corner, not centred with .position (that spilled over the orb)
    assert ".offset(x: p.rect.minX, y: p.rect.minY)" in stage and ".position(x: p.rect.midX, y: p.rect.midY)" not in stage


def test_group_is_centred_and_the_bubble_never_covers_the_orb():
    # iPhone 13-ish stage: 390 wide, from under the 聊天记录 button (46) to the top of the input bar
    for area in [(0, 46, 390, 560), (0, 46, 390, 300), (0, 4, 390, 700)]:     # 300: keyboard up
        d = max(72, min(150, 390 * 0.36, area[3] * 0.3))
        (cx, cy), (bx, by, bw, bh), _ = group(270, 100, d, area)
        top, bottom = cy - d / 2, by + bh
        assert abs((top - area[1]) - (area[1] + area[3] - bottom)) < 1.0      # centred between button and input
        assert by >= cy + d / 2 + GAP - 0.01                                   # under 球球, clear of it
        assert area[1] + EDGE - 0.01 <= top and bottom <= area[1] + area[3] - EDGE + 0.01


def test_long_text_is_cut_to_the_room_left():
    area = (0, 46, 390, 260)
    d = max(72, min(150, 390 * 0.36, 260 * 0.3))
    c, (bx, by, bw, bh), lines = group(270, 400, d, area)
    assert lines is not None and lines >= 1 and by + bh <= area[1] + area[3] - EDGE + 0.01


def test_ios_history_button_opens_at_the_newest_message():
    assert "openAtEnd = true; staged = false" in HC
    assert "|| m.openAtEnd" in HC and "pin.toEnd(follow: 2.5)" in HC
    older = HC[HC.index('.id("older")'):][:400]
    assert "guard olderOK else { return }" in older        # no older page while the opening jump runs


def test_web_history_button_opens_at_the_newest_message():
    fn = JS[JS.index("function hShowChat()"):][:600]
    assert "HS.pinUntil = Date.now() + 1500" in fn and "hRender(\"bottom\")" in fn and "hScrollLatest()" in fn
    older = JS[JS.index("async function hOlder()"):][:300]
    assert "Date.now() < HS.pinUntil) return" in older
    scroll = JS[JS.index('hBox().addEventListener("scroll"'):][:300]
    assert re.search(r"HS\.pinUntil && Date\.now\(\) < HS\.pinUntil && !hNear\(\)\) return", scroll)
