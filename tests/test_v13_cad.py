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
    assert "cad_draw" in BY_NAME and "read_image" in BY_NAME
    assert {"cad_draw", "read_image"} <= LOOP_TOOLS


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
    loop = AgentLoop(Task(prompt="画客厅背景墙施工图"), "s-cad", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=5, timeout_s=120, make_plan=False, artifacts_dir=str(tmp / "art")))
    t = await loop.run()
    assert t.status == TaskStatus.SUCCEEDED
    assert (tmp / "cad" / "立面.dxf").exists() and (tmp / "cad" / "立面_build.py").exists()
    files = [e for e in events if getattr(e, "type", None) and e.type.value.endswith("file")]
    assert any(e.data["name"] == "立面.dxf" for e in files)
    assert any("drawing saved" in h for h in loop.history)


from test_phase5_agent_loop import env  # noqa: E402,F401
