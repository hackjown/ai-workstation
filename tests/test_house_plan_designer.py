"""3.9.8 house-plan-designer: routing (designer tasks never enter the CAD redraw phases) and the cad-drawing bridge."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runtime import cad, skills

ROOT = Path(__file__).resolve().parents[1]
HPD = ROOT / "skills" / "house-plan-designer"


@pytest.mark.parametrize("p", ["把这张户型图做成装修设计器网页", "户型图转 3D 漫游", "CAD 图纸转设计器",
                               "用 house-plan-designer 把结构图还原", "把平面图做成网页，带家具布置"])
def test_designer_prompts(p):
    assert skills.is_designer_task(p)
    assert skills.auto(p) == ["house-plan-designer"]
    assert cad.cad_mode(p) is None


@pytest.mark.parametrize("p", ["照这张户型图画平面图 DXF", "画一个水电施工图", "户型图重画一下，出 DWG"])
def test_cad_prompts_stay_cad(p):
    assert not skills.is_designer_task(p)
    assert "house-plan-designer" not in skills.auto(p)
    assert cad.cad_mode(p) == "fresh"


def test_skill_files():
    assert (HPD / "assets" / "template.html").stat().st_size > 1_000_000
    text = (HPD / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: house-plan-designer")
    assert "AIW 内置版说明" in text and "上游 SKILL.md" in text
    man = (HPD / "MANIFEST").read_text(encoding="utf-8").split()
    assert "assets/template.html" in man and "scripts/aiw_hpd.py" in man
    assert all((HPD / f).exists() for f in man)


def test_from_cad_bridge(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("ezdxf")
    spec = ROOT / "skills" / "cad-drawing" / "examples" / "xinda-g-108.spec.json"
    out = tmp_path / "draft.json"
    r = subprocess.run([sys.executable, str(HPD / "scripts" / "aiw_hpd.py"), "from-cad", str(spec), "-o", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    hs = json.loads(out.read_text(encoding="utf-8"))
    assert len(hs["walls"]) > 20 and hs["autoElec"] is True
    names = {x["name"] for x in hs["rooms"]}
    assert {"客厅", "主卧", "厨房"} <= names
    assert sum(len(w.get("openings", [])) for w in hs["walls"]) >= 10
    assert not any(f["key"] == "counter" and False for f in hs["furniture"])
    r2 = subprocess.run([sys.executable, str(HPD / "scripts" / "build.py"), str(out), "-o", str(tmp_path / "x.html")],
                        capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    assert "window.__PRESET__=" in (tmp_path / "x.html").read_text(encoding="utf-8")
