"""3.9.93: 施工图 sets - read the inputs -> brief.json -> 图纸目录 -> sheets -> verify (task-241, 升旗台广场).

The run being fixed: two images in one shallow read_image, reading cut off at step 4 by the anti-loop guard, ONE generic
sheet with text heights in model mm (h_mm 800 = an 80 m title at 1:100, 647 overlap errors), the floor-plan tracer run
on a site photo, floor-plan boilerplate in the message, and a set with hard errors delivered as 「最好的草稿」.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"

PLAZA = ("【技能：librecad】\n图1是设计，图2是原图，工程概况：移除左右两边6棵椰子树，广场长27.9m×宽15.8m＝440.82㎡（含与篮球场斜接面积），"
         "原地面为红土。做法：地面基础处理→铺贴户外瓷砖→新建升旗台（围栏为大理石柱子）、（广场整体抬高，临水泥路一侧设2步通宽台阶）；"
         "广场后斜坡（长13.5m×宽5m×高1.6m）回填后铺草坪，游泳池和广场、广场和篮球场的连接处做排水，根据以上情况绘制施工图\n\n"
         "附件（已放到电脑上）：\n- ~/workspace/uploads/shengqitai-gaoqing.png\n- ~/workspace/uploads/IMG_3198.JPG")

IMG1 = json.dumps({"role": "设计效果图", "view": "从水泥路一侧朝广场看", "layout": "前方水泥路，左侧游泳池，右侧篮球场，后部居中升旗台",
                   "elements": [{"name": "升旗台", "position": "广场后部居中", "material": "大理石栏杆"},
                                {"name": "台阶", "position": "前侧通宽", "count": 2}],
                   "paving_pattern": "浅灰方砖通缝", "texts": [], "dimensions": []}, ensure_ascii=False)
IMG2 = "这是现场照片：红土地面，左右两侧有椰子树，左边游泳池，右边篮球场。"


def _load(name):
    sys.path.insert(0, str(KIT))
    s = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def sw():
    return _load("sitework")


@pytest.fixture(scope="module")
def plaza_set(sw, tmp_path_factory):
    pytest.importorskip("ezdxf")
    pytest.importorskip("PIL")
    ss = _load("sheetset")
    b = sw.build_brief(PLAZA, [{"index": 1, "path": "uploads/a.png", "read": IMG1}, {"index": 2, "path": "uploads/b.jpg", "read": IMG2}])
    spec = sw.spec_from_brief(b)
    out = tmp_path_factory.mktemp("plaza")
    import os
    os.environ["DRAWKIT_DWG"] = "0"
    rep = ss.build_set(spec, out, "升旗台广场施工图")
    return b, spec, rep, out, ss


# ------------------------------------------------------------------------------------------------ routing

def test_routing_site_vs_floor_plan(sw):
    assert sw.classify(PLAZA)["type"] == "site"
    assert sw.classify("照着这张户型图画一张平面图，三室两厅")["type"] == "floor_plan"
    assert sw.classify("把这张售楼处的户型照片描成 CAD 平面图")["type"] == "floor_plan"
    assert sw.classify("展厅装修施工图：吊顶、立面、地面铺装和水电点位，按报价单出图")["type"] == "interior"
    assert sw.classify("自行车棚钢结构雨棚施工图，基础和立面")["type"] == "structure"
    from agent_runtime import drawing_site as ds
    assert ds.is_sheet_job(PLAZA) and not ds.is_sheet_job("照着户型图描一张平面图")
    imgs, quotes, qimgs = ds.uploads(PLAZA + "\n- ~/workspace/uploads/报价.xlsx")
    assert imgs == ["uploads/shengqitai-gaoqing.png", "uploads/IMG_3198.JPG"] and quotes == ["uploads/报价.xlsx"]


# ------------------------------------------------------------------------------------------------ brief

def test_brief_from_text_images_and_quote(sw):
    quote = [["序号", "项目名称", "规格", "单位", "数量", "单价"],
             ["1", "户外瓷砖铺贴", "600×600", "㎡", "450", "85"],
             ["2", "移除椰子树", "", "棵", "6", "300"],
             ["3", "线性排水沟", "300宽", "m", "31.6", "220"]]
    b = sw.build_brief(PLAZA, [{"index": 1, "path": "uploads/a.png", "read": IMG1},
                              {"index": 2, "path": "uploads/b.jpg", "read": IMG2}], [{"path": "uploads/q.xlsx", "rows": quote}])
    s = b["site"]
    assert b["type"] == "site" and b["project"] == "升旗台广场改造工程"
    assert s["plaza"]["L"] == 27900 and s["plaza"]["W"] == 15800 and s["plaza"]["area_m2"] == 440.82
    assert s["slope"]["L"] == 13500 and s["slope"]["W"] == 5000 and s["slope"]["H"] == 1600
    assert s["trees_remove"]["n"] == 6 and s["trees_remove"]["what"] == "椰子树"
    assert s["steps"]["n"] == 2 and s["steps"].get("full_width")
    assert [d["between"] for d in s["drainage"]] == [["游泳池", "广场"], ["广场", "篮球场"]]
    assert s["flag_platform"]["railing"] == "大理石柱子"
    # each image read on its own, the user's words decide the role
    roles = {i["index"]: i["role"] for i in b["images"]}
    assert roles == {1: "设计效果图", 2: "现场照片"}
    assert any(f["source"].startswith("图1") and "升旗台" in f["key"] for f in b["facts"])
    assert all(f.get("source") for f in b["facts"])
    # quote vs drawing: tiles 450 in the quote, 416.82 by the drawings (platform footprint taken out) -> conflict
    assert any("户外瓷砖" in c["what"] and "450" in c["what"] for c in b["conflicts"]), b["conflicts"]
    trees = next(r for r in b["quantities"] if r["key"] == "trees_remove")
    assert trees["quote_qty"] == 6 and not any("移除椰子树" in c["what"] for c in b["conflicts"])
    # unknowns carry the assumption that was used; nothing blocks this job
    keys = {u["key"] for u in b["unknowns"]}
    assert {"raise.h", "paving.tile", "flag_platform.size", "slope.shape", "drain.type"} <= keys
    assert not [u for u in b["unknowns"] if u["blocking"]]
    assert any(d["key"] == "slope.backfill" and d["value"] == 54.0 for d in b["derived"])


def test_missing_main_size_is_blocking(sw):
    b = sw.build_brief("广场铺户外瓷砖，新建两步台阶，周边做排水沟，画施工图", [])
    assert b["type"] == "site"
    assert [u["key"] for u in b["unknowns"] if u["blocking"]] == ["plaza.size"]


def test_quote_rows_parsed(sw):
    items = sw.parse_quote_rows([["项目名称", "单位", "数量"], ["铺草坪", "㎡", "70"], ["合计", "", ""]])
    assert items == [dict(items[0])] and items[0]["key"] == "lawn" and items[0]["qty"] == 70 and items[0]["unit"] == "㎡"


# ------------------------------------------------------------------------------------------------ 图纸目录

def test_sheet_list_for_the_plaza(sw):
    b = sw.build_brief(PLAZA, [])
    sheets = sw.plan_sheets(b)
    assert [s["title"] for s in sheets] == ["图纸目录及设计说明", "总平面及拆除平面图", "铺装平面图", "竖向及排水平面图",
                                            "升旗台平面、立面、剖面图", "台阶及栏杆大样图", "剖面图", "排水沟节点大样图", "工程量表"]
    assert [s["no"] for s in sheets][:3] == ["JS-00", "JS-01", "JS-02"]
    # a job without a flag platform / drainage gets no such sheets
    b2 = sw.build_brief("场地长20m×宽10m，铺户外瓷砖，画施工图", [])
    t2 = [s["title"] for s in sw.plan_sheets(b2)]
    assert "升旗台平面、立面、剖面图" not in t2 and "排水沟节点大样图" not in t2 and "工程量表" in t2


# ------------------------------------------------------------------------------------------------ units

def test_text_height_is_paper_mm(tmp_path):
    pytest.importorskip("ezdxf")
    ss = _load("sheetset")
    v = ss.View({"title": "t", "scale": 100, "entities": [{"type": "rect", "box": [0, 0, 27900, 15800]},
                                                          {"type": "text", "at": [1000, 1000], "text": "广场", "h": 5}]}, "X").build()
    t = v.sh.texts[0]
    assert t["h"] == 5 and not v.errors          # 5 mm on paper
    spec = {"kind": "sheet_set", "project": "p", "sheets": [{"no": "S-01", "title": "t", "views": [v.v]}]}
    rep = ss.build_set(spec, tmp_path, "u", dxf=True)
    import ezdxf
    doc = ezdxf.readfile(rep["files"]["dxf"])
    hs = sorted(e.dxf.height for e in doc.modelspace().query("TEXT") if e.dxf.text == "广场")
    assert hs == [500.0]                         # paper 5 mm at 1:100 -> model 500 mm
    # the task-241 mistake: h given in model mm is refused with a clear message, not drawn 80 m tall
    bad = ss.View({"title": "t", "scale": 100, "entities": [{"type": "rect", "box": [0, 0, 27900, 15800]},
                                                            {"type": "text", "at": [0, 0], "text": "标题", "h": 800}]}, "X").build()
    assert any("PAPER mm" in e and "80000" in e for e in bad.errors)
    assert max(t["h"] for t in bad.sh.texts) <= 7
    # coordinates in metres: the view is a few mm on paper -> refused
    tiny = ss.View({"title": "t", "scale": 100, "entities": [{"type": "rect", "box": [0, 0, 27.9, 15.8]}]}, "X").build()
    assert any("MODEL millimetres" in e for e in tiny.errors)


def test_generic_h_mm_sanity_no_gigantic_title(tmp_path):
    dk = _load("drawkit")
    spec = {"schema": "aiw-drawing/1", "kind": "generic", "title": "升旗台广场改造工程", "paper": "A1", "scale": 100,
            "entities": [{"type": "rect", "box": [0, 0, 27900, 15800]},
                         {"type": "text", "at": [1500, 39500], "text": "升旗台广场改造工程  施工图", "h_mm": 800},
                         {"type": "text", "at": [1500, 37000], "text": "工程概况", "h_mm": 5}]}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    import os
    os.environ["DRAWKIT_DWG"] = "0"; os.environ["DRAWKIT_LIBRECAD"] = "0"
    rep = dk.run_build(str(p), str(tmp_path), "g", dxf=False)
    assert any("h_mm=800" in e and "PAPER mm" in e for e in rep["errors"])
    assert sum("text overlap" in e for e in rep["errors"]) < 5       # was 647 in task-241


# ------------------------------------------------------------------------------------------------ the set

def test_plaza_set_builds_clean_multi_page(plaza_set):
    b, spec, rep, out, ss = plaza_set
    assert rep["ok"], rep["errors"][:10]
    assert len(rep["sheets"]) == 9 and rep["files"]["pdf_pages"] == 9
    import re
    pages = re.findall(rb"/Type\s*/Page(?!s)", Path(rep["files"]["pdf"]).read_bytes())
    assert len(pages) == 9
    assert all(Path(p).exists() for p in rep["files"]["pngs"])
    req = {r["what"]: r for r in rep["requirements"]}
    assert req["6 棵椰子树（拆除）"]["ok"] and req["6 棵椰子树（拆除）"]["found"] == 6
    assert req["2 步台阶（剖面踏步）"]["found"] == 2
    assert req["广场长 27900 尺寸标注"]["ok"] and req["2 处排水沟"]["ok"]
    # every sheet has a title block with 图号 and its scale
    for s in rep["sheets"][1:-1]:
        assert s["scales"], s


def test_requirement_missing_is_an_error(plaza_set, tmp_path):
    b, spec, rep, out, ss = plaza_set
    sp = json.loads(json.dumps(spec))
    site = next(s for s in sp["sheets"] if s["key"] == "site_plan")
    site["views"][0]["entities"] = [e for e in site["views"][0]["entities"] if e.get("tag") != "tree_remove"]
    r2 = ss.build_set(sp, tmp_path, "x", dxf=False)
    assert not r2["ok"] and any("6 棵椰子树" in e for e in r2["errors"])


def test_empty_sheet_and_overflow_are_errors(tmp_path):
    ss = _load("sheetset")
    spec = {"kind": "sheet_set", "project": "p", "sheets": [
        {"no": "S-01", "title": "空图", "views": []},
        {"no": "S-02", "title": "太大", "paper": "A3", "paper_fixed": True,
         "views": [{"title": "v", "scale": 50, "entities": [{"type": "rect", "box": [0, 0, 60000, 30000]}]}]}]}
    rep = ss.build_set(spec, tmp_path, "e", dxf=False)
    errs = " | ".join(rep["errors"])
    assert "EMPTY" in errs and "do not fit" in errs


# ------------------------------------------------------------------------------------------------ policies

def _L(site=True):
    from agent_runtime.drawing_phase import DrawPhase
    return SimpleNamespace(_dphase=DrawPhase(site=site), history=[], step=4, _sheet_job=site, _draw_escalations=0)


def _d(tool, args=None, thought="我先看两张图，确认设计意图和原地形"):
    return SimpleNamespace(tool=tool, args=args or {}, thought=thought)


def test_anti_loop_lets_read_through_before_the_brief():
    from agent_runtime import drawing_hooks as dh
    L = _L()

    async def go():
        out = []
        for k in range(12):   # task-241 was cut off at step 4 of this
            tool = ("read_image", "shell", "drawing_note")[k % 3]
            args = {"path": "uploads/a.png", "question": f"布局 {k}"} if tool == "read_image" else (
                {"command": "python3 -c 'from PIL import Image'"} if tool == "shell" else {"items": [{"kind": "notes", "value": k}]})
            g, n = await dh.pre_tool(L, _d(tool, args))
            out.append(g)
            dh.post_tool(L, _d(tool, args), True, "")
        return out
    assert asyncio.run(go()) == [None] * 12
    # the floor-plan tracer is refused on a site job
    g, n = asyncio.run(dh.pre_tool(L, _d("drawing_auto", {"image": "uploads/b.jpg"})))
    assert g == "skip" and "drawing_brief" in n


def test_anti_loop_lets_verify_reads_through():
    from agent_runtime import drawing_hooks as dh
    L = _L()
    dp = L._dphase
    dp.brief_done = True
    for _ in range(3):
        dp.after("drawing_build", {"spec": "cad/spec.json"}, True, "drawing_build round 1/3: CHECKS FAILED\nERRORS: x")
    assert dp.must_deliver

    async def go():
        res = []
        for k in range(6):
            a = {"path": f"tasks/task-1/cad/升旗台广场施工图-JS-0{k}.png", "question": "看得清吗"}
            res.append((await dh.pre_tool(L, _d("read_image", a, "我看一下自己出的图")))[0])
            dh.post_tool(L, _d("read_image", a), True, "")
        return res
    assert asyncio.run(go()) == [None] * 6     # task-241: blocked by 「换方法：确定性流水线出图」


def test_floor_plan_policy_unchanged():
    """the floor-plan route keeps its READ budget (no regression of 3.8.8)."""
    from agent_runtime.drawing_phase import DrawPhase
    dp = DrawPhase(read_budget=3)
    r = None
    for k in range(5):
        r = dp.gate("read_image", {"path": "uploads/p.png", "question": str(k)})
        if r:
            break
        dp.after("read_image", {"path": "uploads/p.png", "question": str(k)}, True)
    assert r and "READ budget" in r


# ------------------------------------------------------------------------------------------------ delivery

class _Tools:
    def __init__(self):
        self.calls = []

    async def run(self, tool, args, intent=None):
        self.calls.append((tool, args))
        return SimpleNamespace(success=True, result={"path": args.get("path"), "content_b64": ""}, error=None)


def _deliver_L(rep, brief):
    from aiw_protocols import TaskStatus  # noqa: F401
    L = SimpleNamespace(tools=_Tools(), history=[], step=9, _sheet_job=True, _sent_file=None, _dphase=None,
                        task=SimpleNamespace(prompt=PLAZA, status=None, result=None), _root_id="task-x")
    L._site = {"route": {"type": "site"}, "report": rep, "brief": brief, "vision": {"errors": [], "warnings": []}, "out": "cad"}
    L._delivered = SimpleNamespace(conflict=lambda *a: None, record=lambda *a: None)

    async def deliver_file(r):
        return None
    L._deliver_file = deliver_file

    def finish(status, text):
        L.task.status, L.task.result = status, text
    L._finish = finish
    return L


def test_verify_blocks_delivery_with_hard_errors(plaza_set):
    from aiw_protocols import TaskStatus
    from agent_runtime import drawing_site as ds
    b, spec, rep, out, ss = plaza_set
    bad = dict(rep, ok=False, errors=["[JS-02 铺装平面图] text overlap at (120,80) mm: 'a' / 'b'"])
    L = _deliver_L(bad, b)
    assert asyncio.run(ds.deliver(L, "3 轮核对已用完"))
    assert L.task.status == TaskStatus.FAILED
    res = L.task.result
    assert "失败" in res and "最好的草稿" not in res and "text overlap" in res
    assert "图纸目录" in res and "JS-08" in res and "假定" in res and "工程量" in res
    for w in ("墙和房间", "门窗", "尺寸链的刻度"):     # no floor-plan boilerplate on a site job
        assert w not in res
    # and the clean set is a success with the same lists
    L2 = _deliver_L(rep, b)
    asyncio.run(ds.deliver(L2))
    assert L2.task.status == TaskStatus.SUCCEEDED and "已出图" in L2.task.result and "移除椰子树  6 棵" in L2.task.result
    sent = [a["path"] for t, a in L2.tools.calls if t == "send_file"]
    assert any(p.endswith(".pdf") for p in sent) and any(p.endswith(".dxf") for p in sent)


def test_vision_checklist_per_sheet(plaza_set, monkeypatch):
    from agent_runtime import drawing_site as ds
    b, spec, rep, out, ss = plaza_set
    from PIL import Image
    asked = []

    async def load(path):
        return Image.new("RGB", (40, 30), "white"), None

    async def vision(L, b64, q, max_tokens=1500):
        asked.append(q)
        if "JS-03" in q:
            return json.dumps({"legible": False, "empty": False, "items": [], "problems": ["文字压线看不清"]}, ensure_ascii=False)
        if "JS-01" in q:
            return json.dumps({"legible": True, "empty": False, "items": [{"what": "6 棵椰子树（拆除）", "seen": False}]}, ensure_ascii=False)
        return json.dumps({"legible": True, "empty": False, "items": []})
    monkeypatch.setattr(ds, "_vision", vision)
    L = SimpleNamespace(_load_ws_image=load, task=SimpleNamespace(prompt=PLAZA), _site=None)
    v = asyncio.run(ds.verify_sheets(L, rep, str(out)))
    assert len(asked) == 9 and any("6 棵椰子树" in q for q in asked if "JS-01" in q)
    assert any("JS-03" in e and "看不清" in e for e in v["errors"])
    # present in the drawing (mechanical tag count 6) but not recognised in the picture: a warning, not a failure
    assert any("JS-01" in w and "椰子树" in w for w in v["warnings"]) and not any("JS-01" in e for e in v["errors"])


# ------------------------------------------------------------------------------------------------ in the loop

from test_phase5_agent_loop import env  # noqa: E402,F401


async def test_loop_routes_and_reads_before_the_first_step(env, monkeypatch):  # noqa: F811
    """the plaza prompt: the loop is a 施工图 set job, drawing_brief runs before step 1 (each image read on its own),
    drawing_auto is refused; a floor-plan prompt keeps the tracer route."""
    pytest.importorskip("ezdxf")
    from agent_runtime import AgentLoop, LoopConfig
    from agent_runtime import drawing_site as ds
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    asked = []

    async def fake_inputs(L, images, quotes, qimgs):
        asked.append(list(images))
        return ([{"index": 1, "path": images[0], "read": IMG1}, {"index": 2, "path": images[1], "read": IMG2}], [], [])
    monkeypatch.setattr(ds, "read_inputs", fake_inputs)
    llm = ScriptedProvider([("drawing_auto", {"image": "uploads/IMG_3198.JPG"}), ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt=PLAZA), "s-site", comp, LLMRouter([llm]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=4, timeout_s=120, make_plan=False, artifacts_dir=str(tmp / "art")))
    assert loop._cad_task and loop._sheet_job
    await loop.run()
    assert asked == [["uploads/shengqitai-gaoqing.png", "uploads/IMG_3198.JPG"]]
    assert any("drawing_brief (automatic" in h and "JS-08 工程量表" in h for h in loop.history)
    assert any("drawing_auto REFUSED" in h for h in loop.history)
    fp = AgentLoop(Task(prompt="照着这张户型图画 CAD 平面图 ~/workspace/uploads/p.jpg"), "s-fp", comp, LLMRouter([llm]), ToolExecutor(comp),
                   config=LoopConfig(max_steps=2, timeout_s=60, make_plan=False, artifacts_dir=str(tmp / "art2")))
    assert fp._cad_task and not fp._sheet_job and not fp._dphase.site
