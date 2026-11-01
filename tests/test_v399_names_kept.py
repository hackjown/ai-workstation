"""3.9.9 (task-0db): "继续把客厅和厨房的文字加上去" - the run's prompt dropped the user's words (any text starting with
继续 counted as a bare 'go on'), the tracer ignored --labels / --from-spec names it could not OCR itself, and the
converge path delivered "通过自动核对" with neither name on the drawing."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"
PLAN = Path(__file__).resolve().parent / "fixtures" / "plan-9800x10900.png"


def test_follow_up_words_starting_with_continue_are_kept():
    from aiw_api.continuation import is_bare_continue
    for t in ("继续", "继续。", "（继续）", "继续做吧", "接着做", "continue"):
        assert is_bare_continue(t), t
    for t in ("继续把客厅和厨房的文字加上去", "继续，再加个门", "继续画完卫生间"):
        assert not is_bare_continue(t), t


def test_requested_names_from_the_follow_up():
    from agent_runtime.drawing_hooks import requested_names
    p = "精准画出户型图\n\n【接着上次继续做】用户补充：继续把客厅和厨房的文字加上去"
    assert requested_names(p) == ["客厅", "厨房"]
    assert requested_names("x\n\n【接着上次继续做】用户补充：主卧室名字标上") == ["主卧室"]
    assert requested_names("x\n\n【接着上次继续做】用户补充：墙再加厚一点") == []
    assert requested_names("x\n\n【接着上次继续做】") == []


def test_tracer_keeps_names_it_could_not_read(monkeypatch):
    pytest.importorskip("numpy"); pytest.importorskip("cv2"); pytest.importorskip("rapidocr_onnxruntime")
    sys.path.insert(0, str(KIT))
    import drawtrace as dt
    full, _i, _t = dt.trace(str(PLAN), task="t")
    pos = {r["name"]: r["label_px"] for r in full["rooms"]}
    assert "客厅" in pos and "厨房" in pos
    real = dt.is_room_name
    monkeypatch.setattr(dt, "is_room_name", lambda t: real(t) and t.strip() not in ("客厅", "厨房"))
    blind, _i, _t = dt.trace(str(PLAN), task="t")
    assert "客厅" not in {r["name"] for r in blind["rooms"]}          # the VM's OCR missed them (task-0db)
    hints = {"labels": [{"name": "客厅", "x": pos["客厅"][0], "y": pos["客厅"][1], "by": "user"},
                        {"name": "厨房", "x": pos["厨房"][0], "y": pos["厨房"][1], "by": "spec"}]}
    tr = dt.Trace(str(PLAN), hints).run()
    sp = tr.spec(task="t")
    names = {r["name"] for r in sp["rooms"]} | {f["text"] for f in sp.get("free_labels") or []}
    assert {"客厅", "厨房"} <= names
