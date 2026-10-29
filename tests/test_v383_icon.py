"""3.8.3: one brand icon everywhere, the 球球 face (thick amber ring, white face, two short bar eyes looking up-right).
iOS: Assets.xcassets/AppIcon 1024 master (full-bleed square, cream, no alpha so iOS applies its own mask); build-ipa.sh
writes the loose AppIcon PNGs from it and compiles Assets.car when actool works. Web: square icon-180/512 (apple-touch,
manifest, Bark push icon) and a transparent orb for favicon + header logo."""
import json
import plistlib
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
IOS = ROOT / "apps" / "ios"
CREAM = (250, 243, 232)


def test_ios_master_icon_is_opaque_full_bleed_1024():
    im = Image.open(IOS / "Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png")
    assert im.size == (1024, 1024) and im.mode == "RGB"
    assert im.getpixel((0, 0)) == CREAM and im.getpixel((1023, 1023)) == CREAM
    cs = json.loads((IOS / "Assets.xcassets/AppIcon.appiconset/Contents.json").read_text())
    assert cs["images"][0]["filename"] == "AppIcon-1024.png" and cs["images"][0]["size"] == "1024x1024"


def test_build_script_uses_the_master_and_actool():
    sh = (IOS / "build-ipa.sh").read_text()
    assert "Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png" in sh and "icon-512.png" not in sh
    assert "actool --compile" in sh and "AppIcon60x60@3x.png" in sh and "AppIcon83.5x83.5@2x~ipad.png" in sh
    pl = plistlib.loads((IOS / "Info.plist").read_bytes())
    assert "AppIcon60x60" in pl["CFBundleIcons"]["CFBundlePrimaryIcon"]["CFBundleIconFiles"]
    assert "AppIcon76x76" in pl["CFBundleIcons~ipad"]["CFBundlePrimaryIcon"]["CFBundleIconFiles"]


def test_web_icons():
    for name, px in (("icon-180.png", 180), ("icon-512.png", 512)):
        im = Image.open(WEB / name).convert("RGB")
        assert im.size == (px, px) and im.getpixel((0, 0)) == CREAM   # square, full bleed (Bark + home screen)
    for name in ("logo.png", "favicon-32.png"):
        im = Image.open(WEB / name)
        assert im.mode == "RGBA" and im.getpixel((0, 0))[3] == 0      # transparent orb
    html = (WEB / "index.html").read_text()
    assert 'href="favicon.ico"' in html and 'href="favicon-32.png"' in html and 'src="logo.png"' in html
    assert 'src="logo.svg"' not in html and (WEB / "favicon.ico").exists()
    man = json.loads((WEB / "manifest.json").read_text())
    assert {i["src"] for i in man["icons"]} == {"icon-180.png", "icon-512.png"}
