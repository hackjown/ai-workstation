"""云宝 expression assistant: the task state → emotion mapping (apps/web/mate-map.js) and the vendored Mood Mates files."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
VENDOR = WEB / "vendor" / "moodmates"


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


def test_vendor_has_licence_and_attribution_and_no_ball_visuals():
    assert "Mood Mates Community License" in (VENDOR / "LICENSE").read_text(encoding="utf-8")
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "sam70361/aora-bot" in readme
    names = {p.name for p in VENDOR.iterdir()}
    assert {"geometry.js", "render.js", "features.js", "fx.js", "emotions.js", "engine.js", "nimbo.js"} <= names
    assert not any(n in names for n in ("ball.js", "rings.js", "twinkle.js"))
    for page in ("index.html", "mate.html"):
        html = (WEB / page).read_text(encoding="utf-8")
        assert "emotion-ball" not in html
        order = [html.find(f"vendor/moodmates/{n}") for n in ("geometry.js", "render.js", "features.js", "fx.js", "emotions.js", "engine.js", "nimbo.js")]
        assert all(i > 0 for i in order) and order == sorted(order), f"{page}: wrong Mood Mates load order"
