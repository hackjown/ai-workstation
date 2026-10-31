"""3.9.2: task-778 - the floor plan came out wrong after 166 steps. The vision model guessed the wall pixels
(off by 500-700 mm), the rooms were forced into a three-row grid, the source has only overall dimensions
(约9800 / 约10900) and the old "overlay" check said 91/91 wall pieces match while the drawing was wrong.

Now: drawtrace.py traces walls / openings / line work from the pixels, reads the dimension numbers and room
names with OCR (no vision model), and drawing_build VERIFIES the result against the source pixels with hard
thresholds. DXF -> DWG with ODA File Converter, checked by a round trip.
"""
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"
FIX = Path(__file__).resolve().parent / "fixtures"
PLAN = FIX / "plan-9800x10900.png"       # task-778's source (black walls, grey floors, overall dims only)
A31 = FIX / "a3-1.jpeg"                  # a photo with four segmented chains

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
pytest.importorskip("ezdxf")
sys.path.insert(0, str(KIT))


def _need_trace():
    pytest.importorskip("cv2")
    pytest.importorskip("rapidocr_onnxruntime")
    import drawtrace
    return drawtrace


def _build(spec, tmp_path, name="plan"):
    import drawkit
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return drawkit.run_build(str(p), str(tmp_path / "out"), name)


@pytest.fixture(scope="module")
def traced():
    dt = _need_trace()
    spec, info, _ = dt.trace(str(PLAN), task="t-778")
    return spec, info


def test_scale_from_overall_dimensions(traced):
    spec, info = traced
    ch = {c["side"]: c["values"] for c in spec["chains"]}
    assert ch["top"] == [9800] and ch["left"] == [10900]
    sx, sy = spec["source"]["mm_per_px"]
    # the 9800 line spans ~1004 source px: ~9.76 mm/px; both axes agree within 1 %
    assert 9.6 < sx < 9.9 and abs(sx / sy - 1) < 0.01


def test_every_room_name_by_ocr_and_in_its_room(traced):
    spec, info = traced
    names = {r["name"] for r in spec["rooms"]}
    for n in ("玄关", "厨房", "餐厅", "客厅", "公卫", "主卫", "儿童房", "老人房", "主卧室", "可拓展空间"):
        assert n in names
    assert not any(n.startswith("房间?") for n in names)
    assert all(L["by"] == "ocr" for L in info["labels"])
    # no invented room (task-778 drew a 主卧过道 that is not on the source)
    assert "主卧过道" not in names


def test_build_verifies_against_the_source(traced, tmp_path):
    spec, _ = traced
    spec = json.loads(json.dumps(spec)); spec["source"]["image"] = str(PLAN)
    rep = _build(spec, tmp_path)
    assert rep["ok"], rep["errors"]
    v = rep["verify"]
    assert v["wall_precision"] >= 0.95 and v["wall_recall"] >= 0.93
    assert v["labels_checked"] >= 12
    assert v["ink_coverage"] >= 0.95
    assert (tmp_path / "out" / "diff.png").exists() and (tmp_path / "out" / "plan.pdf").exists()


def test_a_wrong_drawing_fails_verify(traced, tmp_path):
    """the old overlay said 91/91 for a wrong drawing; verify must fail a plan whose walls are 600 mm off."""
    spec, _ = traced
    bad = json.loads(json.dumps(spec)); bad["source"]["image"] = str(PLAN)
    for w in bad["walls_traced"]:
        w["a"][0] += 600; w["b"][0] += 600
    rep = _build(bad, tmp_path)
    assert not rep["ok"]
    assert any(e.startswith("verify:") for e in rep["errors"])


def test_hollow_wall_piece_is_found_and_drawn_as_wall(traced, tmp_path):
    """the 老人房 top wall continues as two thin lines (hollow) for ~1.3 m: the solid-wall check cannot see it, so it
    is traced as a wall piece of its own (wall layer, drawn as two lines like the source)."""
    spec, _ = traced
    hw = spec.get("hollow_walls") or []
    assert any(abs(h["a"][1] - h["b"][1]) < 1e-6 and 5000 <= h["a"][0] <= 5200 and 6300 <= h["b"][0] <= 6500
               for h in hw), hw
    assert sum(abs(h["b"][0] - h["a"][0]) + abs(h["b"][1] - h["a"][1]) for h in hw) < 3000   # no cabinets / beds
    sp = json.loads(json.dumps(spec)); sp["source"]["image"] = str(PLAN)
    rep = _build(sp, tmp_path)
    assert rep["ok"], rep["errors"]
    assert rep["verify"]["hollow_wall_mm"] >= 1000
    import ezdxf
    d = ezdxf.readfile(tmp_path / "out" / "plan.dxf")
    assert len(d.modelspace().query('LWPOLYLINE[layer=="墙体"]')) + len(d.modelspace().query('LINE[layer=="墙体"]')) > 0


def test_verify_fails_a_dropped_hollow_wall_or_dropped_line_work(traced, tmp_path):
    spec, _ = traced
    a = json.loads(json.dumps(spec)); a["source"]["image"] = str(PLAN); a.pop("hollow_walls", None)
    rep = _build(a, tmp_path / "a")
    assert not rep["ok"] and any("HOLLOW" in e for e in rep["errors"])
    b = json.loads(json.dumps(spec)); b["source"]["image"] = str(PLAN); b["traced_lines"] = b["traced_lines"][::3]
    rep = _build(b, tmp_path / "b")
    assert not rep["ok"] and any("line work" in e for e in rep["errors"])
    assert rep["verify"]["ink_coverage"] < 0.9


def test_hollow_wall_render_is_refused_not_faked(tmp_path):
    """a3-1 (colour render, hollow double-line walls): the tracer would find only a few solid pieces. It must refuse
    (drawkit auto then uses the wall-grid pipeline, which v3.8.9 tuned on this very photo) instead of delivering a
    drawing that only looks traced; the chains it read are still exact."""
    dt = _need_trace()
    with pytest.raises(ValueError, match="outline"):
        dt.Trace(str(A31)).run()
    tr = dt.Trace(str(A31))
    import drawtrace
    tr.T = None
    texts = drawtrace.ocr(tr.im)
    vals = sorted(v for t, s_, b, o in texts for v in drawtrace.parse_number(t) if o == "h" and 1000 <= v <= 3000)
    assert {2400, 2600, 1700, 1850, 1650, 2900} <= set(vals)


def test_auto_falls_back_when_the_tracer_cannot_read(tmp_path, monkeypatch):
    import drawkit
    monkeypatch.setattr(drawkit, "_auto_trace", lambda a: (_ for _ in ()).throw(ValueError("no dimension numbers")))
    # the legacy pipeline is still there and is told why
    src = (KIT / "drawkit.py").read_text(encoding="utf-8")
    assert '"trace_error": trace_err' in src and "drawauto" in src


# ---------------------------------------------------------------------------------------------- DWG

FAKE_ODA = r"""#!/usr/bin/env python3
# fake ODA File Converter: <in> <out> <ver> <DWG|DXF> 0 1  -> copies every file with the new extension
import os, shutil, sys
i, o, ver, typ = sys.argv[1:5]
drop = os.environ.get("FAKE_ODA_DROP") == "1"
for f in os.listdir(i):
    b, _ = os.path.splitext(f)
    dst = os.path.join(o, b + "." + typ.lower())
    if drop and typ == "DXF":
        import ezdxf
        d = ezdxf.readfile(os.path.join(i, f)); m = d.modelspace()
        for e in list(m)[: len(m) // 2]: m.delete_entity(e)
        d.saveas(dst)
    else:
        shutil.copy(os.path.join(i, f), dst)
"""


def _fake_oda(tmp_path, monkeypatch):
    exe = tmp_path / "ODAFileConverter"
    exe.write_text(FAKE_ODA)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ODA_CONVERTER", str(exe))
    monkeypatch.setenv("ODA_XVFB", "0")
    return exe


def _a_dxf(tmp_path):
    import ezdxf
    d = ezdxf.new("R2010"); m = d.modelspace()
    d.layers.add("墙体")
    for i in range(20):
        m.add_line((0, i * 100), (5000, i * 100), dxfattribs={"layer": "墙体"})
    p = tmp_path / "plan.dxf"; d.saveas(p)
    return p


def test_dwg_written_and_round_trip_checked(tmp_path, monkeypatch):
    import drawkit
    _fake_oda(tmp_path, monkeypatch)
    r = drawkit.export_dwg(_a_dxf(tmp_path))
    assert r.get("dwg") and Path(r["dwg"]).exists(), r
    assert "ODA" in r["by"] and "same extents" in r["check"]


def test_dwg_rejected_when_the_round_trip_loses_entities(tmp_path, monkeypatch):
    import drawkit
    _fake_oda(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_ODA_DROP", "1")
    r = drawkit.export_dwg(_a_dxf(tmp_path))
    assert "error" in r and "round-trip" in r["error"]
    assert not (tmp_path / "plan.dwg").exists()


def test_no_converter_is_a_warning_not_a_crash(tmp_path, monkeypatch):
    import drawkit
    monkeypatch.setenv("ODA_CONVERTER", "/nonexistent")
    monkeypatch.setattr(drawkit, "_oda_converter", lambda: None)
    monkeypatch.setattr(drawkit.shutil, "which", lambda n: None)
    r = drawkit.export_dwg(_a_dxf(tmp_path))
    assert "no DWG converter" in r["error"]


# ---------------------------------------------------------------------------------------------- task-778 crash

def test_big_sheet_png_is_capped(tmp_path, monkeypatch):
    """task-778: DecompressionBombError (245 M px) killed drawing_build twice."""
    import drawkit
    monkeypatch.setenv("DRAWKIT_PNG_MAX_PX", "2000000")
    sh = drawkit.Sheet(); sh.rect((0, 0, 200000, 150000), "图框", 0.5)
    r = drawkit.render_png(sh, str(tmp_path / "x.png"), scale=10)
    assert r["size"][0] * r["size"][1] <= 2_100_000


# ---------------------------------------------------------------------------------------------- runtime wiring

def test_runtime_ships_the_tracer_and_reports_dwg_and_verify():
    sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
    from agent_runtime import drawing as d
    assert "drawtrace.py" in d.KIT_FILES and "drawtrace.py" in d.install_command("x")
    s = d.summary({"ok": True, "scale": "1:75", "files": {"png": "plan.png", "dxf": "plan.dxf", "dwg": "plan.dwg",
                                                          "pdf": "plan.pdf", "diff": "diff.png"},
                   "verify": {"wall_precision": 0.99, "wall_recall": 0.96, "tolerance_mm": 34, "labels_checked": 13}}, 1)
    assert "cad/plan.dwg" in s and "precision 0.99" in s
    loop = (ROOT / "services" / "agent-runtime" / "agent_runtime" / "loop.py").read_text(encoding="utf-8")
    assert "verify passed" in loop and "hand-edit walls" in loop
    hooks = (ROOT / "services" / "agent-runtime" / "agent_runtime" / "drawing_hooks.py").read_text(encoding="utf-8")
    assert "not traced and" in hooks          # no vision-model label read for a traced drawing
