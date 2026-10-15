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
    assert "xdotool type --delay 25 -- line" in s and "-- @6000,0" in s and "key Escape" in s
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
