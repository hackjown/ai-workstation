"""v3.9.16: house-plan-designer upstream b94183f merged (AIW layer kept), black CAD screenshots routed to cadshot,
and screenshots pasted / dropped straight into the web composer (paste.js)."""
import importlib.util
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
HPD = ROOT / "skills" / "house-plan-designer"


def _hpd():
    spec = importlib.util.spec_from_file_location("aiw_hpd_t", HPD / "scripts" / "aiw_hpd.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_upstream_files_and_aiw_layer():
    for f in ("scripts/browser.mjs", "scripts/cadshot.py", "scripts/dimocr.py", "scripts/dimticks.py", "scripts/imgio.py",
              "reference/route-image.md", "assets/template.html",                       # upstream b94183f
              "scripts/aiw_hpd.py", "scripts/pano.mjs", "assets/pano-viewer.html", "NOTICE.md", "LICENSE"):   # AIW
        assert (HPD / f).is_file(), f
    man = (HPD / "MANIFEST").read_text().split()
    assert all((HPD / f).is_file() for f in man) and "scripts/dimocr.py" in man and "scripts/aiw_hpd.py" in man
    skill = (HPD / "SKILL.md").read_text(encoding="utf-8")
    assert skill.index("# AIW 内置版说明") < skill.index("# 上游 SKILL.md") and "dimocr.py" in skill and "黑底 CAD 截图" in skill
    assert "b94183f" in (HPD / "NOTICE.md").read_text(encoding="utf-8")
    assert "timeout: 90000" in (HPD / "scripts" / "check.mjs").read_text(encoding="utf-8")


def test_catalog_check_needs_the_new_copy():
    from agent_runtime.skills import CATALOG
    hpd = next(s for s in CATALOG if s["id"] == "house-plan-designer")
    assert "b94183f" in hpd["check"]
    assert "b94183f" in (ROOT / "deploy" / "vm" / "house-plan-designer-setup.sh").read_text()


def test_dark_screenshot_goes_to_cadshot(tmp_path):
    m = _hpd()
    dark = Image.new("RGB", (400, 300), (0, 0, 0))
    d = ImageDraw.Draw(dark)
    d.rectangle([40, 40, 360, 260], outline=(0, 255, 255), width=3)
    dark.save(tmp_path / "shot.png")
    light = Image.new("RGB", (400, 300), (255, 255, 255))
    ImageDraw.Draw(light).rectangle([40, 40, 360, 260], outline=(0, 0, 0), width=8)
    light.save(tmp_path / "plan.png")
    r = m.dark_screenshot(str(tmp_path / "shot.png"))
    assert r and r["route"] == "cadshot" and "cadshot.py" in r["next"] and "dimocr.py" in r["next"]
    assert m.dark_screenshot(str(tmp_path / "plan.png")) is None
    assert m.from_image(str(tmp_path / "shot.png"), str(tmp_path / "o" / "spec.json"))["route"] == "cadshot"


def test_paste_js_loaded_and_valid():
    html = (ROOT / "apps" / "web" / "index.html").read_text(encoding="utf-8")
    assert '<script src="paste.js"></script>' in html
    js = (ROOT / "apps" / "web" / "paste.js").read_text(encoding="utf-8")
    assert "hUpload" in js and "uploadFiles" in js and '"paste"' in js and '"drop"' in js and "截图-" in js
    try:
        subprocess.run(["node", "--check", str(ROOT / "apps" / "web" / "paste.js")], check=True, capture_output=True)
    except FileNotFoundError:
        pass
