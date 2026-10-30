"""iOS 3.7 / build 20 (v2 home, 3.6 + 3.7 fixes): wiring checks for the SwiftUI sources (they only compile in the ipa CI on macOS)."""
import pathlib
import plistlib
import re

IOS = pathlib.Path(__file__).resolve().parents[1] / "apps" / "ios"
SRC = IOS / "Sources"


def test_version_and_camera_permissions():
    p = plistlib.loads((IOS / "Info.plist").read_bytes())
    assert p["CFBundleShortVersionString"] == "3.8.9" and p["CFBundleVersion"] == "30"
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
    for f in ("HomeChat.swift", "HomeV2.swift", "MateView.swift", "API.swift", "RichText.swift", "ChatView.swift",
              "CardStyle.swift", "DesktopView.swift", "AiwApp.swift", "TaskCardV2.swift"):
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
    assert "StageLayout.group(" in v2 and "StageLayout.tail(" in v2 and "alignmentGuide(below" not in v2
    # 2. no quick chips above the input
    assert "m.chips.indices" not in home
    # 3. light-mode 球球 bubbles
    assert "hBubbleFill(cs)" in v2 and "hBubbleFill(cs)" in home
    assert 'environment(\\.colorScheme, .dark)' not in v2.split("struct SkillPicker")[0]
    # 4. the small head: drawn natively since 3.7 (no web-view snapshot); thinking uses the same size
    assert "struct MateHead" in mate and "MateSnapshot" not in mate and "CGPoint(x: -400" not in mate and "MateAvatar.side" in v2
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


def test_37_fixes_are_wired():
    home = (SRC / "HomeChat.swift").read_text(encoding="utf-8")
    v2 = (SRC / "HomeV2.swift").read_text(encoding="utf-8")
    chat = (SRC / "ChatView.swift").read_text(encoding="utf-8")
    mate = (SRC / "MateView.swift").read_text(encoding="utf-8")
    card = (SRC / "CardStyle.swift").read_text(encoding="utf-8")
    desk = (SRC / "DesktopView.swift").read_text(encoding="utf-8")
    app = (SRC / "AiwApp.swift").read_text(encoding="utf-8")
    build = (IOS / "build-ipa.sh").read_text(encoding="utf-8")
    # 1. the avatar is a drawing: always a full circle, no WKWebView, no snapshot
    head = mate.split("struct MateAvatar", 1)[1]
    assert "MateHead(size: size)" in head and "takeSnapshot" not in mate and "WKWebView(" not in head
    assert "contentInsetAdjustmentBehavior = .never" in mate
    # 2. task cards: the shared style, no forced dark scheme; the same in the task view
    assert "0xFF / 255, green: 0xF4 / 255, blue: 0xE0" in card and "func taskCardStyle" in card
    card2 = (SRC / "TaskCardV2.swift").read_text(encoding="utf-8")   # 3.8.8: the chat's task card moved there
    assert ".taskCardStyle(waiting ? .live : tone, cs)" in card2 and "taskCardStyle(cardTone(live.status), cs" in chat
    assert "environment(\\.colorScheme, .dark)" not in home
    # 3. answered cards never print the raw answer
    assert "resolvedText(resolved)" in home and "<null>" in card and "func answerText" not in home
    assert home.count(".askCardStyle(cs, settled: done || stale)") == 2
    # 4. the pill floats in the safe area above the composer, amber border; chat and task view
    for src in (home, chat):
        assert ".overlay(alignment: .bottomTrailing)" in src and "alignmentGuide(VerticalAlignment.top)" not in src
    assert "hAmber.opacity(cs == .dark ? 0.75 : 0.9)" in v2
    # 5. the task view: amber user bubble, the small head, the + panel
    assert "UserBubbleV2(" in (SRC / "RichText.swift").read_text(encoding="utf-8")
    assert "MateAvatar()" in chat and "AttachSheet(kinds:" in chat and "Menu {" not in chat.split("var composer: some View {", 1)[1].split("func send()", 1)[0]
    # briefing bubbles: white in light
    assert "cs == .dark ? hStageBubble : Color.white" in v2
    # 6. takeover: the touch page, landscape for that view only
    assert "TakeoverWeb(" in desk and "loadHTMLString(html, baseURL: base)" in desk and "aiwTakeover" in desk
    assert "requestGeometryUpdate(.iOS(interfaceOrientations:" in desk and "supportedInterfaceOrientationsFor" in desk
    assert "@UIApplicationDelegateAdaptor(AppDelegate.self)" in app
    assert "cp ../web/takeover.html ../web/takeover.js $APP/takeover/" in build


def test_37_web_counterparts():
    web = IOS.parent / "web"
    js, css, html, app, tk = ((web / n).read_text(encoding="utf-8") for n in ("home.js", "home.css", "index.html", "app.js", "takeover.js"))
    assert "function hResolved(" in js and '"已回答：" + String(a)' not in js
    assert "function hPlaceFabs(" in js and "#hToLatest, #toLatest" in css
    assert "html[data-theme=light] #hmsgs .hcard.st-succeeded" in css and "#fff4e0" in css
    assert "html[data-theme=light] .hbb" in css and "work tcard st-" in app
    assert '<script src="takeover.js"></script>' in html and "AiwTouch.attach(" in app
    for needle in ("trackpad", "direct", "showLoupe", "pinch", "requestFullscreen", 'orientation', ".lock(\"landscape\")", "aiwt-rot",
                   "two-finger tap: right click", "Control", "Escape", "Tab"):
        assert needle in tk, needle
    assert (web / "takeover.html").exists()
