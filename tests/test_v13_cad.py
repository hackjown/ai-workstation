"""V1.3: cad_draw builds an ezdxf script with the helper prelude; read_image describes files for drafting."""
import importlib.util
import json
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
    assert "-- za;" not in cad.librecad_type_command(["text"])


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
