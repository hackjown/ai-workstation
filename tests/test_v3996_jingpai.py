"""3.9.96: 精排 (presentation) render of 施工图 sets - sheetstyle.py.

A sheet_set delivers a vector PDF in the reference style (zone tints, Render a House material hatches, two-line
leaders, blue view title bars, 工程量表 / 图例 / 标题栏, embedded CJK font). The per-sheet PNGs used by VERIFY and the
DXF are the plain ones, unchanged; interior floor plans (drawkit build) are not touched at all.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"

PLAZA = ("图1是设计，图2是原图，工程概况：移除左右两边6棵椰子树，广场长27.9m×宽15.8m＝440.82㎡（含与篮球场斜接面积），"
         "原地面为红土。做法：地面基础处理→铺贴户外瓷砖→新建升旗台（围栏为大理石柱子）、（广场整体抬高，临水泥路一侧设2步通宽台阶）；"
         "广场后斜坡（长13.5m×宽5m×高1.6m）回填后铺草坪，游泳池和广场、广场和篮球场的连接处做排水，根据以上情况绘制施工图")


def _load(name):
    sys.path.insert(0, str(KIT))
    s = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    pytest.importorskip("ezdxf")
    pytest.importorskip("PIL")
    pytest.importorskip("matplotlib")
    os.environ["DRAWKIT_DWG"] = "0"
    sw, ss = _load("sitework"), _load("sheetset")
    spec = sw.spec_from_brief(sw.build_brief(PLAZA, []))
    pres = tmp_path_factory.mktemp("pres")
    plain = tmp_path_factory.mktemp("plain")
    rp = ss.build_set(spec, pres, "广场施工图")
    rq = ss.build_set(json.loads(json.dumps(spec)), plain, "广场施工图", style="plain")
    return spec, rp, rq, pres, plain


# ------------------------------------------------------------------------------------------------ PAT parser

def test_pat_parser_reads_render_a_house_patterns():
    st = _load("sheetstyle")
    for name in ("RAH_CONCRETE", "RAH_GRAVEL", "RAH_EARTH"):
        p = st.load_pattern(name)
        assert p and len(p["lines"]) >= 3, name
    earth = st.load_pattern("RAH_EARTH")
    assert earth["lines"][0][:5] == (0.0, 0.0, 0.0, 0.0, 24.0) and earth["lines"][0][5] == [12.0, -12.0]
    combined = st.parse_pat((KIT / "hatches" / "architectural-metric.pat").read_text(encoding="utf-8"))
    assert {"RAH_CONCRETE", "RAH_GRAVEL", "RAH_EARTH"} <= set(combined)
    assert (KIT / "hatches" / "LICENSE.txt").read_text(encoding="utf-8").startswith("CC0")
    p = st.parse_pat("; c\n*X,demo\n45, 0,0, 0,5\n0, 0,0, 0,10, 5,-5\n")
    assert p["X"]["lines"] == [(45.0, 0.0, 0.0, 0.0, 5.0, []), (0.0, 0.0, 0.0, 0.0, 10.0, [5.0, -5.0])]


def test_pat_segments_are_clipped_to_the_polygon_and_holes():
    st = _load("sheetstyle")
    pat = st.parse_pat("*H,horiz\n0, 0,0, 0,1\n")["H"]
    poly = [(0, 0), (10, 0), (10, 10), (0, 10)]
    segs, dots = st.pat_segments(pat, poly, holes=[(4, -1, 6, 11)], k=1.0)
    assert segs and not dots
    for a, b in segs:
        for x, y in (a, b):
            assert -1e-6 <= x <= 10 + 1e-6 and -1e-6 <= y <= 10 + 1e-6
        assert not (a[0] < 5 < b[0] and b[0] - a[0] > 6)     # nothing runs through the hole
    tri = [(0, 0), (10, 0), (0, 10)]
    segs, _ = st.pat_segments(st.load_pattern("RAH_CONCRETE"), tri, k=0.2)
    assert segs and all(x + y <= 10 + 1e-6 for s in segs for x, y in s)


def test_material_to_pattern_mapping():
    st = _load("sheetstyle")
    assert st.material_pattern("100厚C20混凝土垫层") == "RAH_CONCRETE"
    assert st.material_pattern("垫层") == "RAH_CONCRETE"
    assert st.material_pattern("150厚级配碎石") == "RAH_GRAVEL" and st.material_pattern("碎石") == "RAH_GRAVEL"
    for w in ("素土夯实", "回填土", "夯实土"):
        assert st.material_pattern(w) == "RAH_EARTH"
    assert st.material_pattern(None, "concrete") == "RAH_CONCRETE" and st.material_pattern("tile") is None
    assert st.split_label("回填土后铺草坪（坡高 1600）") == ("回填土后铺草坪", "坡高 1600")
    assert st.split_label("升旗台") == ("升旗台", None)
    assert st.zone_tint("广场—篮球场连接处 300宽线性排水沟") is None and st.zone_tint("现状游泳池")


# ------------------------------------------------------------------------------------------------ sheet set

def test_presentation_pdf_for_sheet_set(built):
    spec, rp, rq, pres, plain = built
    f = rp["files"]
    assert rp["ok"], rp["errors"]
    assert f.get("style") == "presentation"
    assert Path(f["pdf"]).name == "广场施工图.pdf" and Path(f["pdf_plain"]).name == "广场施工图-plain.pdf"
    n = len(spec["sheets"])
    assert f["pdf_pages"] == n and len(f["svgs"]) == n
    data = Path(f["pdf"]).read_bytes()
    assert data[:5] == b"%PDF-" and data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") + data.count(b"/Type/Page") >= 1
    assert b"/FontFile" in data or b"/Type3" in data   # the CJK font is embedded (outlines, Type 3 for OTF/CFF)
    assert b"/Subtype /Image" not in data   # vector, not the PNG pages
    svg = Path(f["svgs"][0]).read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg
    if shutil.which("pdftotext"):
        txt = subprocess.run(["pdftotext", f["pdf"], "-"], capture_output=True, text=True).stdout
        assert "工程量表" in txt and "图例" in txt and spec["sheets"][1]["no"] in txt


def test_plain_png_and_dxf_unchanged_by_the_style(built):
    _spec, rp, rq, pres, plain = built
    assert rq["files"].get("style") is None and "pdf_plain" not in rq["files"] and "svgs" not in rq["files"]
    for a, b in zip(rp["files"]["pngs"], rq["files"]["pngs"]):
        assert Path(a).read_bytes() == Path(b).read_bytes(), Path(a).name
    strip = lambda p: [ln for ln in Path(p).read_text(encoding="utf-8").splitlines()   # noqa: E731
                       if not (ln.strip().replace(".", "").isdigit() and len(ln.strip()) > 12) and not ln.startswith("{") and " @ " not in ln]
    assert strip(rp["files"]["dxf"]) == strip(rq["files"]["dxf"])
    # the plain page PDF kept next to the 精排 one is the one the plain style writes
    assert Path(rp["files"]["pdf_plain"]).stat().st_size > 0


def test_style_option_and_fallback(built, monkeypatch, tmp_path):
    ss = _load("sheetset")
    assert ss.sheet_style({}, None) == "presentation"
    assert ss.sheet_style({"style": "plain"}) == "plain" and ss.sheet_style({}, "精排") == "presentation"
    monkeypatch.setenv("DRAWKIT_SHEET_STYLE", "plain")
    assert ss.sheet_style({}) == "plain"
    monkeypatch.delenv("DRAWKIT_SHEET_STYLE")
    # renderer failure: the plain PDF stays the deliverable, with a warning
    spec = built[0]
    import sheetstyle
    monkeypatch.setattr(sheetstyle, "write_presentation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    small = dict(spec, sheets=[spec["sheets"][-1]], requirements=[])
    rep = ss.build_set(small, tmp_path, "x", dxf=False)
    assert rep["files"]["pdf"].endswith("x.pdf") and "style" not in rep["files"]
    assert any("精排 PDF not written" in w for w in rep["warnings"])


def test_section_2_2_stair_sits_on_the_plaza(built):
    """the 2-2 section: the stair is a closed body from the plaza finish (0) up to the platform, the plaza build-up is
    drawn under it, and the finish line does not run through the platform (the steps read as below ground before)."""
    spec = built[0]
    plat = next(s for s in spec["sheets"] if s.get("key") == "platform")
    sec = next(v for v in plat["views"] if v["title"].startswith("2-2"))
    polys = [e for e in sec["entities"] if e["type"] == "poly" and e.get("closed")]
    stair = next(p for p in polys if min(q[1] for q in p["points"]) == 0 and max(q[1] for q in p["points"]) > 0)
    assert min(q[0] for q in stair["points"]) < 0 and max(q[0] for q in stair["points"]) == 0
    assert all(q[1] >= 0 for q in stair["points"])
    lines0 = [e for e in sec["entities"] if e["type"] == "line" and e["a"][1] == 0 and e["b"][1] == 0]
    assert lines0 and not any(min(e["a"][0], e["b"][0]) < 1 and max(e["a"][0], e["b"][0]) > 1 for e in lines0)
    mats = {e.get("material") for e in sec["entities"] if e["type"] == "hatch"}
    assert {"级配碎石", "素土夯实"} <= mats


# ------------------------------------------------------------------------------------------------ interior + kit

def test_interior_floor_plan_path_untouched(tmp_path):
    pytest.importorskip("ezdxf")
    os.environ["DRAWKIT_DWG"] = "0"
    dk = _load("drawkit")
    spec = tmp_path / "spec.json"
    shutil.copy(KIT / "examples" / "xinda-g-108.spec.json", spec)
    rep = dk.run_build(str(spec), str(tmp_path), "plan")
    files = rep.get("files") or {}
    assert rep.get("kind") != "sheet_set"
    assert files.get("png") and files.get("pdf") and "svgs" not in files and "style" not in files
    assert not list(tmp_path.glob("*.svg")) and not list(tmp_path.glob("*-plain.pdf"))
    assert "sheetstyle" not in (KIT / "drawkit.py").read_text(encoding="utf-8")
    for f in ("drawkit.py", "drawauto.py", "drawtrace.py"):
        assert "sheetstyle" not in (KIT / f).read_text(encoding="utf-8")


def test_kit_files_ship_sheetstyle_and_hatches():
    sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
    from agent_runtime import drawing
    for rel in ("sheetstyle.py", "hatches/RAH_CONCRETE.pat", "hatches/RAH_GRAVEL.pat", "hatches/RAH_EARTH.pat",
                "hatches/architectural-metric.pat", "hatches/LICENSE.txt"):
        assert rel in drawing.KIT_FILES and (drawing.KIT_DIR / rel).exists(), rel
    files = drawing.kit_files()
    assert "sheetstyle.py" in files and files["hatches/RAH_EARTH.pat"].count("*RAH_EARTH") == 1
    cmd = drawing.install_command("v")
    assert "sheetstyle.py" in cmd and "hatches/*" in cmd and "cad-drawing/hatches" in cmd
