"""3.8.7 drawing pipeline: read rigorously -> spec -> deterministic generator (drawkit) -> verify.

Regression case: the 信达海天下 G户型 sales photo of task-a12 (187 steps, stopped by the user). Its spec lives in
skills/cad-drawing/examples/ and must keep building with every check passing and the walls on the photo's walls.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"
EX = KIT / "examples" / "xinda-g-108.spec.json"
PHOTO = KIT / "examples" / "12-031de1aca2dd.JPG"

pytest.importorskip("numpy")
pytest.importorskip("ezdxf")
os.environ.setdefault("DRAWKIT_NO_DOWNLOAD", "1")
os.environ.setdefault("DRAWKIT_LIBRECAD", "0")

_s = importlib.util.spec_from_file_location("drawkit", KIT / "drawkit.py")
dk = importlib.util.module_from_spec(_s)
_s.loader.exec_module(dk)


def _spec(tmp_path, mutate=None):
    sp = json.loads(EX.read_text(encoding="utf-8"))
    sp["source"]["image"] = str(PHOTO)
    if mutate:
        mutate(sp)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(sp, ensure_ascii=False), encoding="utf-8")
    return p


def test_example_builds_with_all_checks_passing(tmp_path):
    rep = dk.run_build(str(_spec(tmp_path)), str(tmp_path / "out"), "plan")
    assert rep["ok"], rep["errors"]
    assert rep["scale"] == "1:100"
    names = {r["name"] for r in rep["rooms"]}
    assert {"厨房", "卫生间", "餐厅", "客厅", "次卧", "主卫", "主卧", "书房", "阳台", "玄关"} <= names
    assert all(r["labelled"] for r in rep["rooms"])
    assert "top/bottom chains agree: 10100 mm" in rep["info"] and "left/right chains agree: 13700 mm" in rep["info"]
    # the fitted pixel scale is the same on both axes (square pixels) and the walls sit on the photo's walls
    fit = next(i for i in rep["info"] if i.startswith("pixel->mm fit"))
    ax = float(fit.split("ax=")[1].split(",")[0]); ay = float(fit.split("ay=")[1].split(",")[0])
    assert abs(ax / ay - 1) < 0.03
    assert rep["overlay_score"]["wall_match"] >= 0.9
    out = tmp_path / "out"
    for f in ("plan.dxf", ".plan.view.dxf", "plan.png", "overlay.png", "report.json"):
        assert (out / f).exists(), f
    import ezdxf
    doc = ezdxf.readfile(out / "plan.dxf")
    texts = {e.dxf.text for e in doc.modelspace().query("TEXT")}
    assert {"客厅", "主卧", "13700", "10100", "3900", "比例 1:100    三房两厅两卫 约108m²"} <= texts
    assert len(doc.modelspace().query("ARC")) >= 6        # door swings
    assert ezdxf.readfile(out / ".plan.view.dxf").styles.get("CN").dxf.font == "notosc"


def test_misread_numbers_are_caught_by_sums_and_tick_pixels(tmp_path):
    """the numbers given in the brief (left 3000/1600, right without the second 600) do not fit the photo."""
    def bad(sp):
        sp["chains"][2]["values"] = [1200, 3750, 3850, 3000, 1600]
        sp["chains"][2]["reads"] = [sp["chains"][2]["values"]] * 2
        sp["chains"][3]["values"] = [600, 3600, 1850, 4550, 2500]
        sp["chains"][3]["reads"] = [sp["chains"][3]["values"]] * 2
    rep = dk.run_build(str(_spec(tmp_path, bad)), str(tmp_path / "out"), "plan", dxf=False)
    err = " ".join(rep["errors"])
    assert not rep["ok"]
    assert "left chain spans 0..13400 but right spans 0..13100" in err
    assert "left chain segment 4: written 3000 but the drawing measures ~3" in err
    assert "left chain segment 5: written 1600" in err


def test_two_reads_must_agree(tmp_path):
    def differ(sp):
        sp["chains"][0]["reads"] = [[1300, 1800, 2800, 3300, 900], [1300, 1800, 2800, 3800, 900]]
    rep = dk.run_build(str(_spec(tmp_path, differ)), str(tmp_path / "out"), "plan", dxf=False)
    assert any("top chain: read #2" in e for e in rep["errors"])


def test_tick_finder_on_the_photo():
    t = dk.find_ticks(str(PHOTO), "left", [182, 200, 206, 480])
    ext = [round(c["pos"]) for c in t["candidates"] if "ext" in c]
    assert t["line_at"] == 192 and ext == [219, 241, 310, 380, 451, 470]


def test_text_overlap_is_an_error():
    sh = dk.Sheet()
    sh.text(0, 0, "客厅", 300)
    sh.text(100, 50, "餐厅", 300)
    e, _ = dk.text_checks(sh, None)
    assert any("text overlap" in x for x in e)


def test_rooms_are_labelled_and_walls_closed(tmp_path):
    def drop(sp):   # a room edge with nothing on the other side becomes an exterior wall: still closed
        sp["rooms"] = [r for r in sp["rooms"] if r["id"] != "balcony"]
        sp["doors"] = [d for d in sp["doors"] if d["id"] != "d-balcony"]
    rep = dk.run_build(str(_spec(tmp_path, drop)), str(tmp_path / "out"), "plan", dxf=False)
    assert rep["ok"], rep["errors"]
    assert not any("not closed" in e or "has no wall" in e for e in rep["errors"])


def test_door_off_any_wall_is_reported(tmp_path):
    def stray(sp):
        sp["doors"].append({"id": "x", "at": [4500, 3300], "width": 900})
    rep = dk.run_build(str(_spec(tmp_path, stray)), str(tmp_path / "out"), "plan", dxf=False)
    assert any("door x: not on any wall" in e for e in rep["errors"])


def test_diagram_and_generic_kinds(tmp_path):
    diag = {"kind": "diagram", "title": "接线示意", "y_down": True,
            "nodes": [{"id": "a", "text": "断路器 QF", "box": [0, 0, 3000, 1200]},
                      {"id": "b", "text": "接触器 KM1", "box": [5000, 2000, 9000, 3200]}],
            "edges": [{"from": "a", "to": "b", "label": "L1 L2 L3"}]}
    part = {"kind": "generic", "title": "法兰盘", "y_down": False,
            "entities": [{"type": "circle", "c": [0, 0], "r": 60}, {"type": "circle", "c": [45, 0], "r": 6},
                         {"type": "centerline", "a": [-70, 0], "b": [70, 0]}],
            "dims": [{"a": [-60, 0], "b": [60, 0], "offset_mm": -12, "value": 120}],
            "labels": [{"at": [45, 0], "text": "4×Ø12 均布"}]}
    for name, sp in (("diag", diag), ("part", part)):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(sp, ensure_ascii=False), encoding="utf-8")
        rep = dk.run_build(str(p), str(tmp_path / name), name)
        assert rep["ok"], (name, rep["errors"])
        assert (tmp_path / name / f"{name}.dxf").exists()
    assert dk.run_build(str(tmp_path / "part.json"), str(tmp_path / "part"), "part", dxf=False)["scale"] == "1:1"


# ---- agent side ----------------------------------------------------------------------------------------------------

def test_drawing_helpers():
    from agent_runtime import drawing as d
    assert d.numbers_from('{"values": [1300, 1800, null]}') == [1300, 1800, None]
    assert d.numbers_from('{"count": 3, "right_to_left": [900, 3300, 2800]}') == [2800, 3300, 900]
    v = d.chain_verdict([[2500, 4550, 1850, 3600, 600, 600], [2500, 4550, 1850, 3600, 600, 600]], vertical=True)
    assert v["ok"] and v["values"] == [600, 600, 3600, 1850, 4550, 2500] and v["total"] == 13700
    v = d.chain_verdict([[1000, 3000], [1000, 3900]], vertical=False)
    assert not v["ok"] and v["differ_at"] == [1]
    assert "drawkit.py" in d.kit_files() and len(d.kit_version()) == 12
    from PIL import Image
    im = Image.open(PHOTO)
    c, vertical, k = d.crop_zoom(im, [182, 200, 206, 480])
    assert vertical and c.width > c.height and k >= 4


def test_tools_registered_and_skill_auto_picked():
    from agent_runtime import skills
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    assert {"drawing_read", "drawing_build"} <= set(BY_NAME) and {"drawing_read", "drawing_build"} <= LOOP_TOOLS
    for p in ("照着这张户型图画一个CAD平面图", "把这个零件图画成dxf", "画一张电机接线图", "按描述画一张流程图纸"):
        assert "cad-drawing" in skills.auto(p), p
    assert "~/.claude/skills/cad-drawing/SKILL.md" in skills.brief("照着照片画户型图")
    from agent_runtime import planner
    assert "drawing_build" in planner.SYSTEM_PROMPT and "NEVER generate the drawing yourself" not in planner.SYSTEM_PROMPT


def test_lessons_seeded_once(tmp_path):
    from aiw_api.lessons import Lessons
    from aiw_api.store import Store
    st = Store(str(tmp_path / "a.db"))
    les = Lessons(st)
    rows = les.items(scope="skill")
    assert len([r for r in rows if r["key"] == "cad-drawing"]) == 9     # 6 from 3.8.7 + 3 from 3.8.8 (drawloop seed)
    les.delete(rows[0]["id"])
    Lessons(st)                      # a restart does not bring a deleted seed back
    assert len([r for r in Lessons(st).items(scope="skill") if r["key"] == "cad-drawing"]) == 8
    block, _ = les.for_task("m1", "照着照片画户型图", ["cad-drawing"])
    assert "drawing_read" in block or "drawing_note" in block or "drawing_auto" in block


from test_phase5_agent_loop import env  # noqa: E402,F401


def _loop(comp, tmp, prompt="照着这张照片画户型平面图 CAD"):
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    return AgentLoop(Task(prompt=prompt), "s-draw", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=4, timeout_s=60, make_plan=False, artifacts_dir=str(tmp / "art")))


async def test_drawing_build_tool_end_to_end(env, monkeypatch):  # noqa: F811
    """drawing_build installs the kit through the vm-agent, builds the example and opens it in LibreCAD."""
    backend, comp, tmp = env
    monkeypatch.setenv("HOME", str(tmp / "home"))
    (tmp / "home").mkdir()
    (tmp / "cad").mkdir()
    (tmp / "uploads").mkdir()
    (tmp / "uploads" / "photo.jpg").write_bytes(PHOTO.read_bytes())
    sp = json.loads(EX.read_text(encoding="utf-8"))
    sp["source"]["image"] = "uploads/photo.jpg"
    loop = _loop(comp, tmp)
    opened = []

    async def fake_type(cmds, open_path, intent):
        opened.append(open_path)
        return None, {}, True, "ok"
    loop._type_cad = fake_type
    r = await loop._drawing_build({"spec": "cad/spec.json", "content": json.dumps(sp, ensure_ascii=False)}, "build")
    assert "ALL CHECKS PASS" in r and "read_image path=cad/overlay.png" in r, r
    assert (tmp / ".aiw/cad-drawing/drawkit.py").exists() and (tmp / "home/.claude/skills/cad-drawing/SKILL.md").exists()
    assert (tmp / "cad/plan.dxf").exists() and (tmp / "cad/plan.png").exists() and (tmp / "cad/overlay.png").exists()
    assert opened == ["cad/.plan.view.dxf"] and loop._cad_pdf_ok and loop._drawkit_built
    # a broken spec comes back as a fix-and-retry, never as a crash
    r = await loop._drawing_build({"content": "{not json"}, "build")
    assert r.startswith("FAILED") and "not valid JSON" in r


async def test_drawing_read_reads_twice_and_reports_conflicts(env):  # noqa: F811
    backend, comp, tmp = env
    (tmp / "uploads").mkdir()
    (tmp / "uploads" / "photo.jpg").write_bytes(PHOTO.read_bytes())
    loop = _loop(comp, tmp)
    answers = []

    class Eyes:
        async def describe(self, system, user, b64, max_tokens=900):
            return answers.pop(0)
    loop.vision_llm = Eyes()
    answers[:] = ['{"values": [1000, 3900, 3850, 3750, 1200]}', '{"count": 5, "right_to_left": [1200, 3750, 3850, 3900, 1000]}']
    r = await loop._drawing_read({"path": "uploads/photo.jpg", "box": [176, 210, 194, 480]})
    assert "values [1200, 3750, 3850, 3900, 1000], total 13700" in r and "top->bottom" in r
    answers[:] = ['{"values": [1600, 3000]}', '{"right_to_left": [3900, 1000]}', '{"values": [1000, 3900]}']
    r = await loop._drawing_read({"path": "uploads/photo.jpg", "box": [176, 400, 194, 480]})
    assert "values [3900, 1000]" in r
    answers[:] = ['{"values": [1600, 3000]}', '{"right_to_left": [3900, 1000]}', '{"values": [1600, 3900]}']
    r = await loop._drawing_read({"path": "uploads/photo.jpg", "box": [176, 400, 194, 480]})
    assert r.startswith("CONFLICT") and "Do NOT pick one" in r


async def test_layout_check_only_rejects_hard_problems(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AIW_CAD_AUTOFIX", "0")
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    loop = AgentLoop(Task(prompt="继续在 LibreCAD 画施工图"), "s-lay2", comp, LLMRouter([ScriptedProvider([])]),
                     ToolExecutor(comp), config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False,
                                                           artifacts_dir=str(tmp / "art")))
    loop._cad_frames, loop._cad_text_boxes, loop._cad_typed_texts, loop._cad_rejects = set(), {}, set(), {}
    loop._cad_sheet, loop._cad_ok = 0, 0
    typed = []

    async def fake_type(cmds, open_path, intent):
        typed.append(cmds)
        return None, {}, True, "typed"
    loop._type_cad = fake_type
    # two labels on top of each other: a warning now, typed anyway (task-a12 lost dozens of steps to these rejections)
    r = await loop._cad_command({"commands": ["FRAME|4|x|4|1:50|p", "TEXT|3000,8000|250|历史沿革",
                                              "TEXT|3200,8050|250|领导关怀"]}, "i")
    assert typed and not r.startswith("NOT TYPED") and "typed although the layout check still finds" in r
