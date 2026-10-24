"""V1.3: cad_draw builds an ezdxf script with the helper prelude; read_image describes files for drafting."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("aiw_cad", ROOT / "services/agent-runtime/agent_runtime/cad.py")
cad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cad)

BODY = '''
ix0, iy0, ix1, iy1 = frame(0, 0, "A3", title="客厅电视背景墙立面图", project="测试", sheet="LM-01")
ox, oy = ix0 + 3000, iy0 + 3000
rect(ox, oy, 4200, 2700, "完成面")
rect(ox + 1200, oy + 300, 1800, 400, "柜体")
dim_chain([(ox, oy), (ox + 1200, oy), (ox + 3000, oy), (ox + 4200, oy)], offset=-800)
wall(ox, oy - 2000, ox + 4200, oy - 2000, 240)
door(ox, oy - 1880, 800, 90)
note(ox + 7000, oy + 2700, ["1. 18mm木工板基层", "2. 9mm岩板"], title="做法说明")
elev_level(ox - 600, oy + 2700, 2.7)
view_title(ox + 2100, oy - 3000, "立面图", 50)
'''


def test_safe_name():
    assert cad.safe_name("客厅/背景墙 立面.dxf") == "客厅_背景墙_立面_dxf"
    assert cad.safe_name("") == "drawing"


def test_build_script_has_prelude_body_epilogue():
    s = cad.build_script("立面", BODY, 30)
    assert "SCALE = 30" in s and "AIW_CAD_RESULT" in s and "dim_chain([(ox, oy)" in s
    assert cad.build_script("x", "pass", "bad").count("SCALE = 50") == 1


def test_parse_result():
    assert cad.parse_result('noise\nAIW_CAD_RESULT {"dxf": "a.dxf"}\n') == {"dxf": "a.dxf"}
    assert cad.parse_result("nothing") is None


def test_script_runs_and_writes_dxf(tmp_path):
    pytest.importorskip("ezdxf")
    (tmp_path / "b.py").write_text(cad.build_script("立面", BODY, 50), encoding="utf-8")
    r = subprocess.run([sys.executable, "b.py"], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    info = cad.parse_result(r.stdout)
    assert info, r.stderr[-800:]
    assert (tmp_path / info["dxf"]).exists()
    assert info["entities_by_layer"].get("尺寸", 0) >= 4
    import ezdxf
    doc = ezdxf.readfile(tmp_path / info["dxf"])
    assert doc.styles.get("CN").dxf.font == "simhei.ttf"   # delivered DXF keeps the AutoCAD font
    assert doc.units == 4


def test_registry_exposes_cad_tools():
    sys.path.insert(0, str(ROOT / "services/tool-router"))
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    assert "cad_draw" in BY_NAME and "read_image" in BY_NAME and "cad_command" in BY_NAME
    assert {"cad_draw", "cad_command", "read_image"} <= LOOP_TOOLS
    from tool_router import tool_schemas
    assert "cad_draw" not in [t["name"] for t in tool_schemas(hide=("cad_draw",))]


def test_script_drawing_is_opt_in():
    assert not cad.script_drawing_allowed("根据设计图和报价表画装修施工图，cad那种的")
    assert cad.script_drawing_allowed("用python脚本画施工图")
    assert cad.script_drawing_allowed("用脚本画客厅背景墙施工图")
    assert not cad.script_drawing_allowed("禁止用 python / ezdxf 脚本生成图纸，也不要用 cad_draw；读报价表可以用脚本，画图只能用 cad_command")


def test_librecad_command_line():
    s = cad.librecad_type_command(["line", "0,0", "@6000,0", "ESC"], "cad/a.dxf")
    assert f"xdotool type --delay {cad.TYPE_DELAY_MS} -- line" in s and "-- @6000,0" in s and "key Escape" in s
    assert 'librecad "${VM_AGENT_FILES_ROOT:-$HOME/workspace}/cad/a.dxf"' in s
    assert s.rstrip().endswith("then zoomauto'") and "-- za;" in s
    assert "trap - EXIT" in cad.librecad_type_command(["text"])


def test_cad_task_detection():
    assert cad.is_cad_task("根据设计图和报价表画装修施工图，cad那种的")
    assert cad.is_cad_task("打开 LibreCAD 画平面图")
    assert not cad.is_cad_task("打开浏览器搜索 decade")


async def test_cad_draw_refused_without_script_request(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    events = []

    async def sink(e):
        events.append(e)
    llm = ScriptedProvider([("cad_draw", {"name": "x", "code": "pass"}), ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(prompt="画施工图，cad那种的"), "s-cad2", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=4, timeout_s=60, make_plan=False, artifacts_dir=str(tmp / "art")))
    await loop.run()
    assert not (tmp / "cad" / "x_build.py").exists()
    assert any("REFUSED" in h for h in loop.history)


async def test_cad_draw_tool_in_the_loop(env):  # noqa: F811
    pytest.importorskip("ezdxf")
    import asyncio as _a
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task, TaskStatus
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    events = []

    async def sink(e):
        events.append(e)
    llm = ScriptedProvider([("cad_draw", {"name": "立面", "code": BODY, "scale": 50, "open": False}),
                            ("done", {"summary": "画好了"})])
    loop = AgentLoop(Task(prompt="用脚本画客厅背景墙施工图"), "s-cad", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=5, timeout_s=120, make_plan=False, artifacts_dir=str(tmp / "art")))
    t = await loop.run()
    assert t.status == TaskStatus.SUCCEEDED
    assert (tmp / "cad" / "立面.dxf").exists() and (tmp / "cad" / "立面_build.py").exists()
    files = [e for e in events if getattr(e, "type", None) and e.type.value.endswith("file")]
    assert any(e.data["name"] == "立面.dxf" for e in files)
    assert any("drawing saved" in h for h in loop.history)


from test_phase5_agent_loop import env  # noqa: E402,F401


async def test_cad_done_rejected_until_file_sent(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    llm = ScriptedProvider([("files_write", {"path": "cad/plan.md", "content": "LM-01"}), ("done", {"summary": "画完了"}),
                            ("files_write", {"path": "cad/plan.md", "content": "LM-01 LM-02"}), ("done", {"summary": "画完了"})])

    async def sink(e):
        pass
    loop = AgentLoop(Task(prompt="在 LibreCAD 里画施工图"), "s-cad3", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=10, timeout_s=60, make_plan=False, artifacts_dir=str(tmp / "art")))
    await loop.run()
    assert any("done REJECTED: the drawing set" in h for h in loop.history)


def test_librecad_command_line_is_valid_shell():
    import subprocess
    for cmds in (["text"], ["line", "0,0", "100,0", "ESC"]):
        s = cad.librecad_type_command(cmds)
        r = subprocess.run(["sh", "-n", "-c", s], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "a dialog" not in s.split("echo ")[-1].split("'")[0]
    assert "maximized_vert" in cad.librecad_type_command(["za"])


def test_cad_task_budget():
    from aiw_api.sessions import _budget
    assert _budget("在 LibreCAD 里画施工图", 40, 900) == (400, 10800.0)
    assert _budget("打开浏览器", 40, 900) == (40, 900)


def test_text_and_save_items():
    import subprocess
    s = cad.librecad_type_command(["rectangle", "0,0", "100,100", "ESC", "TEXT|1000,800|500|兴丹路 吊顶平面图 1:50",
                                   "SAVE|cad/荣誉室.dxf"])
    assert subprocess.run(["sh", "-n", "-c", s], capture_output=True).returncode == 0
    assert "xclip" in s and "兴丹路 吊顶平面图 1:50" in s and "1000,800" in s
    assert "alt+f" in s and "荣誉室.dxf" in s and "ctrl+shift+s" not in s
    assert s.count("ctrl+m") >= 3          # command line refocused before typed commands
    assert "librecad-setup.sh" in s        # Chinese font + 1:50 template on first use
    assert cad._parse_text("TEXT|1,2|3|a|b") == ("1,2", "3", "a|b")
    assert cad._parse_text("text|1,2|标题") == ("1,2", "350", "标题")
    assert cad._parse_text("text") is None


def test_ttf2lff_header():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "deploy/vm/ttf2lff.py"
    assert "LibreCAD Font 1" in src.read_text()
    tpl = Path(__file__).resolve().parents[1] / "deploy/vm/librecad-template-1to50.dxf"
    t = tpl.read_text()
    assert "$DIMSCALE\n 40\n50" in t and "ENTITIES\n  0\nENDSEC" in t


def test_text_clipboard_does_not_hold_the_output_pipe():
    """xclip forks and keeps serving the clipboard; with the shell's stdout attached it held the pipe open and
    every TEXT batch hung until the 120 s timeout (run of 2026-10-18)."""
    s = cad.librecad_type_command(["TEXT|0,0|300|荣誉室"])
    assert "xclip -selection clipboard -i >/dev/null 2>&1;" in s
    assert "</dev/null; " not in s.split("xclip", 1)[1][:60]  # stdin must stay the piped text


def test_cad_command_timeout_scales_with_batch():
    import inspect
    from agent_runtime import loop as L
    src = inspect.getsource(L.AgentLoop._type_cad)
    assert '"timeout": budget' in src and L.CAD_MAX_BATCH >= 150
    assert "_cad_progress" in inspect.getsource(L.AgentLoop._cad_command)  # a cut-off batch resumes itself


def test_sheet_macros_expand_to_librecad_commands():
    import subprocess
    out = cad.expand(["FRAME|2|平面布置图|JZ-03|1:50|兴丹路消防救援站荣誉室", "DIMH|0|-800|0,4000,6000,10000",
                      "NOTE|1000,2000|175|一、工程概况;二、设计依据", "LEADER|100,100|900,900|175|9厘夹板", "line", "0,0", "1,1", "ESC"])
    assert out[:3] == ["rectangle", "50000,0", "71000,14850"]
    assert "TEXT|61650,800|350|平面布置图" in out and "TEXT|1000,1685|175|二、设计依据" in out
    assert out.count("dimhorizontal") == 4 and "5000,-1400" in out       # 3 segments + overall further out
    assert any(c.endswith("|9厘夹板") for c in out) and out[-4:] == ["line", "0,0", "1,1", "ESC"]
    assert cad.expand(["DIMH|oops"])[0].startswith("#BAD")
    s = cad.librecad_type_command(out + ["SAVE|cad/兴丹路消防救援站荣誉室施工图.dxf", "PDF|cad/a.dxf|cad/a.pdf"])
    assert subprocess.run(["sh", "-n", "-c", s], capture_output=True).returncode == 0
    assert "aiw_splitpdf.py" in s and "aiw-cad-progress" in s
    assert 'xdotool type --delay 15 -- "${VM' not in s.split("Save Drawing As")[1][:400].split("else")[0]  # path pasted


def test_resume_index_backs_up_to_the_tool():
    c = ["line", "0,0", "1,1", "ESC", "rectangle", "2,2", "3,3", "TEXT|1,1|175|a"]
    assert cad.resume_index(c, 6) == 4 and cad.resume_index(c, 7) == 7 and cad.resume_index(c, 0) == 0


def test_split_pdf_script_compiles():
    compile(cad.SPLIT_PDF_SCRIPT, "aiw_splitpdf.py", "exec")


async def test_cad_done_rejected_without_librecad_pdf(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    llm = ScriptedProvider([("files_write", {"path": "cad/a.pdf", "content": "x"}), ("send_file", {"path": "cad/a.pdf"}),
                            ("done", {"summary": "画完了"}), ("done", {"summary": "画完了"})])

    async def sink(e):
        pass
    loop = AgentLoop(Task(prompt="在 LibreCAD 里画施工图"), "s-cad4", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=10, timeout_s=60, make_plan=False, artifacts_dir=str(tmp / "art")))
    await loop.run()
    assert any("done REJECTED: no LibreCAD PDF" in h for h in loop.history)


# ---- V1.3.7: sheet-local coordinates land on their sheet; LibreCAD not started/killed from shell ----
def test_place_moves_local_points_onto_current_sheet():
    from agent_runtime import cad
    cmds, sheet, moved = cad.place(["FRAME|4|左侧墙立面图|4|1:50|X", "line", "2000,3000", "12000,3000", "ESC",
                                    "TEXT|5000,8000|250|历史沿革", "line", "102000,3500", "112000,3500", "ESC"], 0)
    assert sheet == 4 and moved == 3
    assert "102000,3000" in cmds and "112000,3000" in cmds and "TEXT|105000,8000|250|历史沿革" in cmds
    assert "102000,3500" in cmds  # absolute points stay as they are
    assert cmds[1] == "100000,0"  # the FRAME itself is absolute


def test_place_keeps_sheet_across_batches_and_sheet_marker():
    from agent_runtime import cad
    cmds, sheet, _ = cad.place(["DIMH|2000|1500|1000,5000,11000", "LEADER|3000,4000|6000,6000|175|乳胶漆"], 2)
    assert sheet == 2 and all(not c[0].isdigit() or float(c.split(",")[0]) >= 50000 for c in cmds if "," in c and "|" not in c)
    cmds, sheet, _ = cad.place(["SHEET|7", "circle", "3000,750", "350"], 2)
    assert sheet == 7 and cmds == ["circle", "178000,750", "350"]
    cmds, sheet, moved = cad.place(["line", "0,0", "@1000,0", "ESC"], 0)
    assert moved == 0 and cmds == ["line", "0,0", "@1000,0", "ESC"]


def test_shell_cannot_start_or_kill_librecad():
    from agent_runtime.loop import _LIBRECAD_PROC as P
    for c in ["nohup librecad >/tmp/l 2>&1 &", "pkill -f librecad; sleep 2", "(librecad &)", "killall librecad"]:
        assert P.search(c), c
    for c in ["pgrep -a librecad", "which librecad", "xdotool search --name LibreCAD", "cat ~/.config/LibreCAD/LibreCAD.conf"]:
        assert not P.search(c), c


def test_type_command_keeps_one_librecad():
    from agent_runtime import cad
    s = cad.librecad_type_command(["line", "0,0", "100,0", "ESC"])
    assert "pgrep -xc librecad" in s and "pkill -o -x librecad" in s


# ---- V1.3.8 ----
def test_text_sets_angle_every_time():
    from agent_runtime import cad
    s = cad.librecad_type_command(["TEXT|1000,1000|400|标题"])
    i = s.index("-- 400;")
    assert "xdotool key Tab ctrl+a; xdotool type --delay" in s[i:i + 120] and "-- 0; xdotool key Return" in s[i:i + 160]
    assert "TextAngle=0" in s  # reset before LibreCAD starts


def test_save_refuses_to_wipe_file_from_empty_drawing():
    from agent_runtime import cad
    s = cad.librecad_type_command(["SAVE|cad/a.dxf"])
    assert "*unnamed*" in s and "SAVE REFUSED" in s


def test_split_pdf_is_fast_and_reports_stray(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    import time
    from agent_runtime import cad
    d = ezdxf.new()
    m = d.modelspace()
    for n in range(3):
        for i in range(300):
            m.add_text("轻钢龙骨", dxfattribs={"height": 175, "insert": (n * 25000 + 1000 + i, 2000)})
    m.add_line((26000, 100), (90000, 100))
    src = tmp_path / "a.dxf"
    d.saveas(src)
    code = cad.SPLIT_PDF_SCRIPT.split("counts, pages, stray")[0]
    assert "bbox.extents" not in cad.SPLIT_PDF_SCRIPT
    t = time.time()
    script = tmp_path / "s.py"
    script.write_text(cad.SPLIT_PDF_SCRIPT.replace('subprocess.run(["librecad"', 'False and subprocess.run(["librecad"'))
    r = subprocess.run([sys.executable, str(script), str(src), str(tmp_path / "o.pdf")], capture_output=True, text=True)
    assert time.time() - t < 30 and code
    assert '"stray": {"1": 1}' in r.stdout or "pdf failed" in r.stdout


def test_cad_resume_words():
    from agent_runtime.loop import _CAD_RESUME
    assert _CAD_RESUME.search("继续画完剩下的图") and not _CAD_RESUME.search("打开 LibreCAD 画施工图")
    assert _CAD_RESUME.search("（继续之前被中断的任务）画施工图") and _CAD_RESUME.search("在原图上补两张立面")
    # run 3ac: a fresh task whose instructions say how to fix a thin sheet is not a resume
    assert not _CAD_RESUME.search("如果提示 THIN SHEETS，就把那张补画完整，再保存并重新导出。每画完一张就保存一次")
    assert not _CAD_RESUME.search("画完一张继续下一张，不要 continue 旧文件以外的东西")


# ---- V1.3.9 ----
def test_one_line_geometry_forms_expand():
    from agent_runtime import cad
    out, _, _ = cad.place(["FRAME|2|a|2|1:50|p", "rectangle|2000,3000|12000,9000", "line|1,2|3,4|5,6|C",
                           "circle|6000,6000|300", "arc|4000,6000|800|0|90"], 0)
    i = out.index("rectangle", 20)
    assert out[i:i + 4] == ["rectangle", "52000,3000", "62000,9000", "ESC"]
    assert ["line", "50001,2", "50003,4", "50005,6", "50001,2", "ESC"] == out[i + 4:i + 10]
    assert out[i + 10:i + 14] == ["circle", "56000,6000", "300", "ESC"]
    assert out[i + 14] == "line" and out[i + 15] == "54800,6000" and out[-2] == "54000,6800"


def test_unknown_pipe_command_is_reported_not_typed():
    from agent_runtime import cad
    out, _, _ = cad.place(["hatch|1,2|3,4", "TEXT|1,1|175|a|b"], 0)
    assert out[0] == "#BAD hatch|1,2|3,4" and out[1].startswith("TEXT|")


def test_textc_centres():
    from agent_runtime import cad
    (t,), _, _ = cad.place(["TEXTC|10000,5000|200|一二三四"], 0)
    pt, h, body = cad._parse_text(t)
    x, y = cad._xy(pt)
    assert abs(x - (10000 - 2 * 1.05 * 200)) < 2 and y == 4900


def test_layout_issues():
    from agent_runtime import cad
    drawn = []
    cad.place(["FRAME|4|x|4|1:50|p", "rect|2000,2000|12000,5000", "TEXT|3000,8000|250|历史沿革",
               "TEXT|3200,8050|250|领导关怀", "line|900,3000|5000,3000", "TEXT|3000,14300|175|太高"], 0, drawn)
    issues, boxes = cad.layout_issues(drawn)
    s = " ".join(issues)
    assert "rectangle from 2000,2000 to 12000,5000 runs inside the title block" in s and "overlaps text '历史沿革'" in s
    assert "900,3000 is outside the border" in s and "'太高'" in s
    assert len([b for b in boxes if not str(b[2]).startswith(chr(0))]) == 3
    ok = []
    cad.place(["FRAME|4|x|4|1:50|p", "rect|2500,4000|12500,7000", "TEXTC|7500,5500|250|历史沿革"], 0, ok)
    assert cad.layout_issues(ok)[0] == []


async def test_cad_layout_check_sends_batch_back_untyped(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    loop = AgentLoop(Task(prompt="继续在 LibreCAD 画施工图"), "s-lay", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop._cad_frames, loop._cad_text_boxes, loop._cad_typed_texts, loop._cad_rejects = set(), {}, set(), {}
    loop._cad_sheet, loop._cad_ok = 0, 0
    typed = []

    async def fake_type(cmds, open_path, intent):
        typed.append(cmds)
        return None, {}, True, "typed"
    loop._type_cad = fake_type
    r = await loop._cad_command({"commands": ["FRAME|4|x|4|1:50|p", "rect|2000,2000|12000,5000", "bogus|1,2"]}, "i")
    assert r.startswith("NOT TYPED") and "title block" in r and "bogus" in r and not typed
    r = await loop._cad_command({"commands": ["FRAME|4|x|4|1:50|p", "rect|2500,4000|12500,7000",
                                              "TEXTC|7500,5500|250|历史沿革"]}, "i")
    assert typed and "NOT TYPED" not in r
    r = await loop._cad_command({"commands": ["SHEET|4", "TEXTC|7500,5500|250|历史沿革"]}, "i")
    assert "already on the drawing" in r


# ---- V1.3.10: nothing is drawn twice ----
def test_dedupe_skips_items_already_drawn():
    from agent_runtime import cad
    batch = ["FRAME|4|左侧墙|4|1:50|p", "rect|2000,3000|4000,5000", "TEXTC|3000,6000|250|历史沿革",
             "line|2000,3000|4000,3000", "SAVE|cad/x.dxf"]
    raw, keys, skipped = cad.dedupe(batch, 0, set())
    assert raw == batch and skipped == 0 and keys[-1] is None
    have = set(cad.landed_keys(raw, keys, 0, 10 ** 6))
    # the same sheet again, written with absolute points this time, plus one new line
    again = ["FRAME|4|左侧墙立面|建施-04|1:50|p", "rect|102000,3000|104000,5000", "TEXTC|3000,6000|250|历史沿革",
             "line|2000,4000|4000,4000"]
    raw2, keys2, skipped2 = cad.dedupe(again, 0, have)
    assert skipped2 == 3 and raw2 == ["SHEET|4", "line|2000,4000|4000,4000"]
    out, sheet, _ = cad.place(raw2, 0)
    assert sheet == 4 and out[:3] == ["line", "102000,4000", "104000,4000"]
    # duplicates inside one batch go too
    raw3, _, s3 = cad.dedupe(["rect|0,0|1,1", "rect|0,0|1,1"], 0, set())
    assert s3 == 1 and len(raw3) == 1


def test_landed_keys_only_counts_finished_items():
    from agent_runtime import cad
    raw = ["FRAME|1|a|1|1:50|p", "rect|2000,3000|4000,5000", "line|2000,3000|4000,3000"]
    raw, keys, _ = cad.dedupe(raw, 0, set())
    n_frame = len(cad.expand([raw[0]]))
    got = cad.landed_keys(raw, keys, 0, n_frame + 2)  # cut off inside the rect
    assert got == ["FRAME|1"]
    got = cad.landed_keys(raw, keys, 0, n_frame + 4)
    assert len(got) == 2


async def test_cad_resend_after_failure_draws_only_the_rest(env):  # noqa: F811
    """Run 3ac: sheet 0 timed out, then a Text dialog error, then the model reopened the file and sent the whole
    sheet again. Items that landed must not be typed a second time; items lost with an unsaved restart must."""
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    loop = AgentLoop(Task(prompt="在 LibreCAD 画施工图"), "s-dup", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop._cad_frames, loop._cad_text_boxes, loop._cad_typed_texts, loop._cad_rejects = set(), {}, set(), {}
    loop._cad_sheet, loop._cad_ok = 0, 0
    typed, plan = [], []

    async def fake_type(cmds, open_path, intent):
        typed.append(list(cmds))
        return plan.pop(0)(cmds)
    loop._type_cad = fake_type
    progress = {"n": 0}

    async def fake_progress():
        return progress["n"]
    loop._cad_progress = fake_progress
    sheet = ["FRAME|0|目录|0|1:50|p", "rect|2500,3000|18500,12000", "line|2500,11200|18500,11200",
             "line|2500,10400|18500,10400", "SAVE|cad/x.dxf"]
    n_frame = len(__import__("agent_runtime").cad.expand([sheet[0]]))

    def fail_after_rect(cmds):  # the frame and the rectangle land, then a dialog error
        progress["n"] = n_frame + 4
        return None, {}, False, "Text dialog did not open"
    plan.append(fail_after_rect)
    await loop._cad_command({"commands": sheet}, "i")
    plan.append(lambda cmds: (None, {}, True, "saved /x.dxf"))
    r = await loop._cad_command({"commands": sheet}, "i")
    assert "2 items already drawn" in r
    assert typed[-1][:3] == ["line", "2500,11200", "18500,11200"] and "rectangle" not in typed[-1]
    # everything is saved now; a third resend types nothing at all
    r = await loop._cad_command({"commands": sheet[:4]}, "i")
    assert r.startswith("nothing typed") and len(typed) == 2
    # unsaved work is forgotten when LibreCAD is reopened on the file
    plan.append(lambda cmds: (None, {}, True, "typed"))
    await loop._cad_command({"commands": ["SHEET|0", "line|3000,5000|9000,5000"]}, "i")
    plan.append(lambda cmds: (None, {}, True, "typed"))
    await loop._cad_command({"open": "cad/x.dxf", "commands": ["SHEET|0", "line|3000,5000|9000,5000"]}, "i")
    assert typed[-1][:3] == ["line", "3000,5000", "9000,5000"]


# ---- V1.3.11: a reply without a tool call is retried, not fatal ----
async def test_no_tool_call_is_retried_not_fatal(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, LLMDecision
    from llm_router.router import LLMProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    seen = []

    class Flaky(LLMProvider):
        name = "flaky"
        n = 0

        async def decide(self, req):
            Flaky.n += 1
            seen.append(req)
            if Flaky.n <= 2:
                return LLMDecision("long reasoning ...", "fail", {"reason": "model returned no tool call (output cut off by max_tokens)"})
            return LLMDecision("ok", "done", {"summary": "完成"})
    loop = AgentLoop(Task(prompt="看一下屏幕"), "s-nc", comp, LLMRouter([Flaky()]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=6, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    t = await loop.run()
    assert t.status.value == "succeeded" and Flaky.n == 3


def test_openai_thinking_budget(monkeypatch):
    from llm_router.providers.openai import OpenAIProvider
    monkeypatch.delenv("AIW_LLM_MAX_TOKENS_THINKING", raising=False)
    monkeypatch.delenv("AIW_LLM_MAX_TOKENS", raising=False)
    assert OpenAIProvider(model="m", api_key="k", thinking="on")._max_tokens() == 16384
    assert OpenAIProvider(model="m", api_key="k", thinking="off")._max_tokens() == 4096


def test_files_write_has_append():
    from tool_router.registry import TOOLS
    spec = next(t for t in TOOLS if t.name == "files_write")
    assert "append" in spec.params


def test_layout_v1313_generic_checks():
    """2026-10-23 PDF: titles past their cell, labels crossed by panel edges, crossing leaders, doubled overall
    dimensions, dimension numbers on top of each other."""
    from agent_runtime import cad
    d = []
    cad.place(["FRAME|4|x|4|1:50|p",
               "rect|2500,4000|4000,9000", "TEXT|2600,6000|250|历史沿革板块很长的标签",       # runs out of its panel
               "LEADER|3000,5000|9000,12000|175|面层",                                         # too long
               "LEADER|3000,8000|5000,6000|175|基层", "LEADER|3500,6000|5000,8200|175|踢脚",  # cross each other
               "DIMH|4000|3000|2500,2600,2950,4000",                                          # 100 and 350: numbers collide
               "DIMH|4000|2400|2500,4000"], 0, d)                                             # overall drawn twice
    s = " | ".join(cad.layout_issues(d)[0])
    assert "is crossed by line" in s and "longer than" in s and "crosses another leader" in s
    assert "is already drawn" in s and "cannot hold their number" in s
    (title,) = [c for c in cad.expand(["FRAME|7|入口形象墙正面(十六字方针)立面图|07|1:50|兴丹路消防救援站荣誉室"]) if c.startswith("TEXT") and "入口" in c]
    pt, h, body = cad._parse_text(title)
    assert cad.text_box(0, 0, float(h), body)[2] <= 5300


def test_layout_clean_elevation_passes():
    from agent_runtime import cad
    d = []
    cad.place(["FRAME|4|x|4|1:50|p", "rect|2500,4000|12500,7000", "rect|2500,4000|6500,7000",
               "TEXTC|4500,5500|250|历史沿革", "DIMH|4000|3200|2500,6500,8500,10500,12500",
               "LEADER|12500,6500|13300,7200|175|面层：PVC板", "LEADER|12500,5000|13300,5600|175|基层：轻钢龙骨"], 0, d)
    assert cad.layout_issues(d)[0] == []


# ---------------------------------------------------------------- V1.4.1: local revisions
def test_cad_mode_revision_in_drawing_thread():
    from agent_runtime import cad
    ctx = "EARLIER IN THIS CONVERSATION:\n- user: 打开桌面上的 LibreCAD 画施工图\n  result (succeeded): cad/x.dxf"
    assert cad.cad_mode("左侧墙的标题底板位置不对，改到顶上", ctx) == "revise"
    assert cad.cad_mode("节点图的引线压线了，调整一下", ctx) == "revise"
    assert cad.cad_mode("把立面图重新画一遍", ctx) == "fresh"
    assert cad.cad_mode("帮我添加一个 Excel 表格", ctx) is None      # not about the drawing
    assert cad.cad_mode("为什么上次失败", ctx) is None
    assert cad.cad_mode("打开 LibreCAD 画施工图", None) == "fresh"
    assert cad.cad_mode("要出的图：0. 目录 ... " + "x" * 700 + " 施工图 把那张补画完整", ctx) == "fresh"


def test_split_edits_sheet_local_and_absolute():
    from agent_runtime import cad
    e, rest, sh = cad.split_edits(["SHEET|4", "ERASE|2000,3000|5000,6000", "ERASE|101000,100|102000,200",
                                   "ERASETEXT|历史沿革", "LIST|4", "rect|1,1|2,2"], 0)
    assert e[0]["box"] == [102000, 3000, 105000, 6000] and e[0]["sheet"] == 4
    assert e[1]["box"] == [101000, 100, 102000, 200]
    assert e[2] == {"op": "erasetext", "text": "历史沿革", "sheet": 4}
    assert e[3]["op"] == "list" and e[3]["box"][0] == 100000
    assert rest == ["SHEET|4", "rect|1,1|2,2"] and sh == 4


def test_edit_script_erases_window_and_lists(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    import subprocess
    from agent_runtime import cad
    (tmp_path / "cad").mkdir()
    d = ezdxf.new(); m = d.modelspace()
    m.add_line((102500, 3000), (104000, 3000))
    m.add_text("历史沿革", height=250, dxfattribs={"insert": (102600, 4000)})
    m.add_line((110000, 3000), (112000, 3000))
    d.saveas(tmp_path / "cad" / "t.dxf")
    e, _, _ = cad.split_edits(["SHEET|4", "LIST|4", "ERASE|2000,2500|5000,5000"], 0)
    out = subprocess.run(["bash", "-c", cad.edit_command("cad/t.dxf", e, texts=True)], capture_output=True, text=True,
                         env={**os.environ, "VM_AGENT_FILES_ROOT": str(tmp_path)}).stdout
    info = cad.parse_edit(out)
    assert info["erased"] == 2 and len(info["list"]) == 3 and info["texts"] == []
    assert len(ezdxf.readfile(tmp_path / "cad" / "t.dxf").modelspace()) == 1


def test_seed_text_boxes_feed_layout_check():
    from agent_runtime import cad
    boxes = cad.seed_text_boxes([[102000, 4000, 250, "历史沿革"]])
    issues, _ = cad.layout_issues([(4, "TEXT|102100,4050|250|领导关怀")], boxes)
    assert any("overlap" in i or "重叠" in i or "on top" in i for i in issues), issues


async def test_cad_erase_then_redraw_same_item(env):  # noqa: F811
    """A revision erases a spot and draws the corrected item there, even if an identical item was typed before."""
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    loop = AgentLoop(Task(prompt="在 LibreCAD 画施工图"), "s-rev", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop._cad_frames, loop._cad_text_boxes, loop._cad_typed_texts, loop._cad_rejects = set(), {}, set(), {}
    loop._cad_sheet, loop._cad_ok = 0, 0
    typed, opened = [], []

    async def fake_type(cmds, open_path, intent):
        typed.append(cmds); opened.append(open_path)
        return None, {}, True, "saved cad/x.dxf" if any(str(c).startswith("SAVE") for c in cmds) else "typed"

    async def fake_edits(edits, intent):
        loop._cad_keys_saved.clear(); loop._cad_keys_unsaved.clear(); loop._cad_typed_texts.clear()
        return "erased 3 entities", True
    loop._type_cad = fake_type
    r = await loop._cad_command({"commands": ["FRAME|4|x|4|1:50|p", "rect|2500,4000|12500,7000", "SAVE|cad/x.dxf"]}, "i")
    assert loop._cad_file == "cad/x.dxf"
    loop._cad_edits = fake_edits
    r = await loop._cad_command({"commands": ["SHEET|4", "ERASE|2000,3500|13000,7500", "rect|2500,4000|12500,7000"]}, "i")
    assert "erased 3" in r and "NOT TYPED" not in r and "already" not in r
    assert opened[-1] == "cad/x.dxf" and any("102500" in str(c) for c in typed[-1]), typed[-1]
    assert 4 in loop._cad_touched


async def test_cad_edit_without_saved_file_is_refused(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    loop = AgentLoop(Task(prompt="在 LibreCAD 画施工图"), "s-rev2", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop._cad_sheet = 0
    r = await loop._cad_command({"commands": ["SHEET|2", "LIST|2"]}, "i")
    assert r.startswith("FAILED") and "SAVE" in r
