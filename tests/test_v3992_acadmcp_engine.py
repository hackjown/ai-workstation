"""3.9.92: optional second drawing engine AIW_CAD_ENGINE=acadmcp (autocad-mcp-pro 1.8.0, ezdxf headless backend).

The engine runs in its own venv through skills/cad-drawing/acadmcp_runner.py; here the runner subprocess is mocked
(no network, no venv): it writes a DXF with the walls it was given, the way autocad-mcp-pro lays its layers out.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"
EX = KIT / "examples" / "xinda-g-108.spec.json"
PHOTO = KIT / "examples" / "12-031de1aca2dd.JPG"

pytest.importorskip("numpy")
ezdxf = pytest.importorskip("ezdxf")
os.environ.setdefault("DRAWKIT_NO_DOWNLOAD", "1")
os.environ.setdefault("DRAWKIT_LIBRECAD", "0")
os.environ.setdefault("DRAWKIT_DWG", "0")

_s = importlib.util.spec_from_file_location("drawkit_acad", KIT / "drawkit.py")
dk = importlib.util.module_from_spec(_s)
_s.loader.exec_module(dk)


def _spec_file(tmp_path):
    sp = json.loads(EX.read_text(encoding="utf-8"))
    sp["source"]["image"] = str(PHOTO)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(sp, ensure_ascii=False), encoding="utf-8")
    return p


def _fake_dxf(spec, path):
    doc = ezdxf.new("R2010")
    for name in ("A-WALL-E-N", "A-WALL-H-N", "A-DOOR-E-N", "A-WIND-E-N", "A-ROOM-T-N"):
        doc.layers.add(name)
    msp = doc.modelspace()
    for w in spec["walls"]:
        (x0, y0), (x1, y1) = w["axis"]
        h = w["thickness"] / 2
        if abs(y0 - y1) < 1e-6:
            pts = [(min(x0, x1), y0 - h), (max(x0, x1), y0 - h), (max(x0, x1), y0 + h), (min(x0, x1), y0 + h)]
        else:
            pts = [(x0 - h, min(y0, y1)), (x0 + h, min(y0, y1)), (x0 + h, max(y0, y1)), (x0 - h, max(y0, y1))]
        hb = msp.add_hatch(dxfattribs={"layer": "A-WALL-H-N"})
        hb.paths.add_polyline_path(pts, is_closed=True)
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "A-WALL-E-N"})
    msp.add_arc((0, 0), 800, 0, 90, dxfattribs={"layer": "A-DOOR-E-N"})
    msp.add_text("ROOM01", dxfattribs={"layer": "A-ROOM-T-N"})       # their English label: must not be imported
    doc.saveas(path)


def _mock_runner(monkeypatch, result=None, fail=None, calls=None):
    real = subprocess.run
    monkeypatch.setattr(dk, "ensure_acadmcp", lambda: ("/fake/venv/bin/python", None))

    def run(args, *a, **kw):
        if isinstance(args, list) and len(args) > 1 and str(args[1]).endswith("acadmcp_runner.py"):
            doc = json.loads(kw["input"])
            if calls is not None:
                calls.append(doc)
            if fail:
                out = {"ok": False, "error": fail}
            else:
                _fake_dxf(doc["spec"], doc["out_dxf"])
                out = dict({"ok": True, "dxf": doc["out_dxf"], "critique_total": 0, "by_focus": {}, "by_severity": {},
                            "issues": [], "not_checked": [], "domain_absent": ["gdt"], "rooms_detected": [],
                            "topology_findings": {"total": 0}, "omitted": [], "rooms_dropped": None,
                            "version": "1.8.0", "seconds": 0.1}, **(result or {}))
            return subprocess.CompletedProcess(args, 0 if out["ok"] else 2, "ACADMCP_RESULT " + json.dumps(out), "")
        return real(args, *a, **kw)
    monkeypatch.setattr(dk.subprocess, "run", run)


def test_default_engine_is_native(tmp_path, monkeypatch):
    monkeypatch.delenv("AIW_CAD_ENGINE", raising=False)
    calls = []
    _mock_runner(monkeypatch, calls=calls)
    assert dk.cad_engine() == "native"
    rep = dk.run_build(str(_spec_file(tmp_path)), str(tmp_path / "out"), "plan")
    assert rep["engine"]["used"] == "native" and not calls
    assert "fallback_reason" not in rep["engine"]


def test_unknown_engine_value_is_native(monkeypatch):
    monkeypatch.setenv("AIW_CAD_ENGINE", "autocad")
    assert dk.cad_engine() == "native"


def test_acadmcp_engine_draws_walls_and_reports_qa(tmp_path, monkeypatch):
    monkeypatch.setenv("AIW_CAD_ENGINE", "acadmcp")
    calls = []
    issues = [{"focus": "arch_opening_clash", "severity": "error", "message": "D3 clashes with W7"},
              {"focus": "duplicate_entities", "severity": "warning", "message": "Lines 9F and A1 are duplicates."},
              {"focus": "arch_room_unlabelled", "severity": "warning", "message": "face carries no label"}]
    _mock_runner(monkeypatch, calls=calls, result={
        "critique_total": 3, "by_focus": {"arch_opening_clash": 1, "duplicate_entities": 1, "arch_room_unlabelled": 1},
        "issues": issues, "not_checked": [{"layout": "Layout1", "lane": "iso128", "reason": "no_viewport_scale"}],
        "omitted": [{"element": "W9", "reason": "it runs into a junction of wall 'W7'"}]})
    rep = dk.run_build(str(_spec_file(tmp_path)), str(tmp_path / "out"), "plan")
    assert rep["engine"]["used"] == "acadmcp"
    a = rep["engine"]["acadmcp"]
    assert a["critique_total"] == 3 and a["not_checked"][0]["reason"] == "no_viewport_scale"
    # the spec the runner got: their schema only, English, walls with axis/thickness, openings by wall+offset+width
    their = calls[0]["spec"]
    assert set(their) <= {"sheet", "lang", "poche", "walls", "openings", "rooms", "chains", "schedules", "stairs"}
    assert their["lang"] == "en" and their["walls"] and all(len(w["axis"]) == 2 and w["thickness"] > 0 for w in their["walls"])
    assert all(o["wall"] in {w["id"] for w in their["walls"]} and o["width"] > 0 and o["offset"] >= 0 for o in their["openings"])
    assert all(r["name"].isascii() for r in their["rooms"])
    # geometric finding -> error (blocking); others -> warnings; our stripped labels are not an issue
    assert any("arch_opening_clash" in e for e in rep["errors"])
    assert any("duplicate_entities" in w for w in rep["warnings"])
    assert any("W9" in w for w in rep["warnings"])
    assert not any("arch_room_unlabelled" in w for w in rep["errors"] + rep["warnings"])
    # the final DXF carries the imported walls on drawkit's layers and drawkit's Chinese room names, not theirs
    doc = ezdxf.readfile(rep["files"]["dxf"])
    msp = doc.modelspace()
    assert len(msp.query('HATCH[layer=="墙体填充"]')) >= len(their["walls"])
    texts = [t.dxf.text for t in msp.query("TEXT")]
    assert "ROOM01" not in texts and any("卧室" in t or "客厅" in t for t in texts)
    assert "_acad_items" not in json.loads((tmp_path / "spec.json").read_text(encoding="utf-8"))


def test_acadmcp_failure_falls_back_to_native_with_chinese_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("AIW_CAD_ENGINE", "acadmcp")
    _mock_runner(monkeypatch, fail="ValueError: walls/openings: bad junction")
    rep = dk.run_build(str(_spec_file(tmp_path)), str(tmp_path / "out"), "plan")
    assert rep["engine"]["used"] == "native"
    assert "回退原生引擎" in rep["engine"]["fallback_reason"] and "bad junction" in rep["engine"]["fallback_reason"]
    assert rep["ok"], rep["errors"]          # the native drawing still builds and passes
    assert Path(rep["files"]["png"]).exists()


def test_acadmcp_missing_venv_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("AIW_CAD_ENGINE", "acadmcp")
    monkeypatch.setenv("AIW_ACADMCP_HOME", str(tmp_path / "no-venv"))
    monkeypatch.setenv("AIW_ACADMCP_NO_INSTALL", "1")
    rep = dk.run_build(str(_spec_file(tmp_path)), str(tmp_path / "out"), "plan")
    assert rep["engine"]["used"] == "native" and "未安装" in rep["engine"]["fallback_reason"]


def test_mapping_openings_fit_their_walls():
    sp = json.loads(EX.read_text(encoding="utf-8"))
    their, notes = dk.acadmcp_spec(sp)
    walls = {w["id"]: w for w in their["walls"]}
    spans = {}
    for o in their["openings"]:
        (x0, y0), (x1, y1) = walls[o["wall"]]["axis"]
        L = abs(x1 - x0) + abs(y1 - y0)
        assert o["offset"] + o["width"] <= L + 1e-6
        for a, b in spans.get(o["wall"], []):
            assert not (o["offset"] < b - 1 and a < o["offset"] + o["width"] - 1)
        spans.setdefault(o["wall"], []).append((o["offset"], o["offset"] + o["width"]))
        if o["kind"] == "door":
            assert o["swing"] in ("in", "out") and o["hand"] in ("left", "right")


def test_runner_is_isolated_from_drawkit():
    src = (KIT / "drawkit.py").read_text(encoding="utf-8")
    for mod in ("server", "config", "engineering", "backends"):
        assert f"import {mod}" not in src and f"from {mod} " not in src and f"from {mod}." not in src
    run = (KIT / "acadmcp_runner.py").read_text(encoding="utf-8")
    assert 'JOURNAL_DURABLE"] = "off"' in run and "ACADMCP_RESULT" in run
    compile(run, "acadmcp_runner.py", "exec")


def test_kit_ships_the_runner_and_passes_the_engine(monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
    from agent_runtime import drawing as d
    assert "acadmcp_runner.py" in d.KIT_FILES and "acadmcp_runner.py" in d.install_command("x")
    monkeypatch.delenv("AIW_CAD_ENGINE", raising=False)
    assert "AIW_CAD_ENGINE" not in d.build_command("cad/spec.json")
    monkeypatch.setenv("AIW_CAD_ENGINE", "acadmcp")
    assert "AIW_CAD_ENGINE=acadmcp python3" in d.build_command("cad/spec.json")
    monkeypatch.setenv("AIW_CAD_ENGINE", "rm -rf /")
    assert "AIW_CAD_ENGINE" not in d.build_command("cad/spec.json")
