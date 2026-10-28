"""iOS 3.5 / build 18 (v2 home): wiring checks for the SwiftUI sources (they only compile in the ipa CI on macOS)."""
import pathlib
import plistlib
import re

IOS = pathlib.Path(__file__).resolve().parents[1] / "apps" / "ios"
SRC = IOS / "Sources"


def test_version_and_camera_permissions():
    p = plistlib.loads((IOS / "Info.plist").read_bytes())
    assert p["CFBundleShortVersionString"] == "3.5" and p["CFBundleVersion"] == "18"
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
                   'emotion: "30"', "dash: [4, 4]", "UTType.movie.identifier", "count: 4)"):
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
    for f in ("HomeChat.swift", "HomeV2.swift", "MateView.swift", "API.swift", "RichText.swift"):
        t = (SRC / f).read_text(encoding="utf-8")
        t = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', t)          # strings
        t = re.sub(r"//[^\n]*", "", t)                         # comments
        assert t.count("{") == t.count("}"), f
        assert t.count("(") == t.count(")"), f
