"""iOS 3.6 / build 19 (v2 home, 3.6 fixes): wiring checks for the SwiftUI sources (they only compile in the ipa CI on macOS)."""
import pathlib
import plistlib
import re

IOS = pathlib.Path(__file__).resolve().parents[1] / "apps" / "ios"
SRC = IOS / "Sources"


def test_version_and_camera_permissions():
    p = plistlib.loads((IOS / "Info.plist").read_bytes())
    assert p["CFBundleShortVersionString"] == "3.6" and p["CFBundleVersion"] == "19"
    for k in ("NSCameraUsageDescription", "NSMicrophoneUsageDescription", "NSPhotoLibraryUsageDescription"):
        assert p.get(k), k


def test_home_v2_is_wired():
    home = (SRC / "HomeChat.swift").read_text(encoding="utf-8")
    v2 = (SRC / "HomeV2.swift").read_text(encoding="utf-8")
    api = (SRC / "API.swift").read_text(encoding="utf-8")
    for needle in ("/api/greeting", "/api/chat/cancel", "/api/uploads/raw", "HomeStage(", "ThinkingRow(", "SkillPicker(", "SkillToken(",
                   "AttachSheet", "HomeCamera(", ".any(of: [.images, .videos])", "PickedMovie.self", "【技能：", 'case "status"',
                   "stop.fill", "UserBubbleV2(", "AiwBubble(", "MateAvatar()"):
        assert needle in home + v2, needle
    assert "upload(for: r, fromFile: file" in api
    # the mockup's colours and corners
    for needle in ("0xFF / 255, green: 0xBE / 255, blue: 0x4D", "0xF0 / 255, green: 0xA0 / 255, blue: 0x30", "0x2A / 255, green: 0x1A / 255, blue: 0x05",
                   "0x15 / 255, green: 0x12 / 255, blue: 0x0D", "0xFF / 255, green: 0xC3 / 255, blue: 0x5C",
                   "BubbleShape(tl: 20, tr: 20, br: 6, bl: 20)", "BubbleShape(tl: 20, tr: 20, br: 20, bl: 6)", "every: 4.5",
                   'emotion: "30"', "UTType.movie.identifier", "count: 4)"):
        assert needle in v2, needle
    for k in ("拍照", "录视频", "照片/视频", "文件", "新建任务", "链接", "技能 /"):
        assert f'"{k}"' in v2, k


def test_orb_look_is_untouched():
    """Only placement / motion / face of 球球 change: mate.html's disc and colours are not overridden from Swift."""
    v2 = (SRC / "HomeV2.swift").read_text(encoding="utf-8")
    mate = (SRC / "MateView.swift").read_text(encoding="utf-8")
    assert "ring=" not in v2 and "--mate-disc" not in v2 + mate
    assert 'var ring = "amber"' in mate and "aiwMate.set(" in mate


def test_swift_braces_balance():
    for f in ("HomeChat.swift", "HomeV2.swift", "MateView.swift", "API.swift", "RichText.swift", "ChatView.swift"):
        t = (SRC / f).read_text(encoding="utf-8")
        t = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', t)          # strings
        t = re.sub(r"//[^\n]*", "", t)                         # comments
        assert t.count("{") == t.count("}"), f
        assert t.count("(") == t.count(")"), f


def test_36_fixes_are_wired():
    home = (SRC / "HomeChat.swift").read_text(encoding="utf-8")
    v2 = (SRC / "HomeV2.swift").read_text(encoding="utf-8")
    chat = (SRC / "ChatView.swift").read_text(encoding="utf-8")
    mate = (SRC / "MateView.swift").read_text(encoding="utf-8")
    # 1. bubbles placed from the orb circle, not hung on it with alignment guides
    assert "StageLayout.place(" in v2 and "StageLayout.tail(" in v2 and "alignmentGuide(below" not in v2
    # 2. no quick chips above the input
    assert "m.chips.indices" not in home
    # 3. light-mode 球球 bubbles
    assert "hBubbleFill(cs)" in v2 and "hBubbleFill(cs)" in home
    assert 'environment(\\.colorScheme, .dark)' not in v2.split("struct SkillPicker")[0]
    # 4. the small head: a square, in-window snapshot; thinking uses the same size
    assert "insertSubview(w, at: 0)" in mate and "CGPoint(x: -400" not in mate and "MateAvatar.side" in v2
    # 5. WeChat-like + panel with SF Symbols
    assert 'return "camera"' in v2 and "0xF7 / 255" in v2 and "perPage = 8" in v2
    # 6./7. bottom pinning + ↓ 最新消息 in the chat and the task view; the task view is a plain VStack
    assert "↓ 最新消息" in v2
    for src in (home, chat):
        assert "BottomPin()" in src and "reportsScrollMetrics()" in src and "reportsComposerTop()" in src and "JumpToLatest" in src
    body = chat.split("var body: some View {", 1)[1].split(".navigationTitle", 1)[0]
    assert "LazyVStack" not in re.sub(r"//[^\n]*", "", body)


def test_36_web_counterparts():
    web = IOS.parent / "web"
    js, css, html = ((web / n).read_text(encoding="utf-8") for n in ("home.js", "home.css", "index.html"))
    assert "function hPlace(" in js and "hLayoutBubble(" in js
    assert 'id="hchips"' not in html and ".hchips" not in css
    assert "html[data-theme=light] #hmsgs .a .bubble" in css
    assert 'class="mate amber hav' in js
    assert "#f7f7f7" in css and 'data-hi="camera"' in html and "hSheetPages" in js
    assert "↓ 最新消息" in html
