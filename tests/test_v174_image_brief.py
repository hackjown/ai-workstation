"""V1.7.4: a casual photo-edit request becomes an explicit brief and a first AI edit before the agent starts."""
import base64
import io
import json

from PIL import Image

from agent_runtime import image_brief as ib
from agent_runtime import image_edit as ie
from test_phase5_agent_loop import env  # noqa: F401
from test_v17_image_edit import _ai_version, _bytes, _photo

ASK = "把上传图片中白色柜子加高到和左侧台面齐平，做好发给我\n\n附件（已放到电脑上）：\n- ~/workspace/uploads/枫木服务区.png"


def test_detects_edit_requests_and_paths():
    assert ib.image_paths(ASK) == ["uploads/枫木服务区.png"]
    assert ib.instruction(ASK) == "把上传图片中白色柜子加高到和左侧台面齐平，做好发给我"
    assert ib.wants_edit(ASK)
    assert not ib.wants_edit("这张图片里写的是什么\n\n附件（已放到电脑上）：\n- ~/workspace/uploads/a.png")


def test_parse_brief_drops_bad_boxes():
    reply = "```json\n" + json.dumps({
        "target": "窗下白色橱柜", "reference": "左侧台面", "change": "加高约 25 px",
        "moves_with": ["白色台面", "咖啡机"], "keep": ["窗户", "地面"],
        "keep_text_boxes": [[650, 680, 860, 1000], [0, 0, 1084, 1451], [5, 5, 6, 6], "x"],
        "ai_prompt": "把白色橱柜整体加高，台面和咖啡机一起上移", "check": "台面是否上移", "question": ""}, ensure_ascii=False) + "\n```"
    d = ib.parse(reply, 1084, 1451)
    assert d["keep_text_boxes"] == [[650, 680, 860, 1000]]
    p = ib.full_prompt(d)
    assert "保持完全不变：窗户、地面" in p and "咖啡机" in p
    assert ib.parse("no json here", 10, 10) is None


async def test_kickoff_makes_first_edit(env, monkeypatch):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    (tmp / "uploads").mkdir()
    (tmp / "uploads/枫木服务区.png").write_bytes(_bytes(_photo()))
    loop = AgentLoop(Task(prompt=ASK), "s-kb", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")),
                     image_cfg={"model": "gpt-image-1", "key": "k", "base_url": "https://gw/v1"})

    class Eyes:
        async def describe(self, system, q, b64, max_tokens=1500):
            assert "加高到和左侧台面齐平" in q
            return json.dumps({"target": "白柜", "moves_with": ["台面", "咖啡机"], "keep": ["地面"],
                               "keep_text_boxes": [[380, 300, 560, 520]], "ai_prompt": "白柜整体加高，台面和咖啡机一起上移",
                               "check": "台面是否明显上移", "question": ""}, ensure_ascii=False)
    loop.vision_llm = Eyes()
    seen = {}

    async def fake_edit(base, key, model, img, prompt, timeout=300):
        seen["prompt"] = prompt
        return _bytes(_ai_version(Image.open(io.BytesIO(img)).convert("RGB")))
    monkeypatch.setattr(ie, "edit", fake_edit)
    note = await loop._image_kickoff()
    assert "ALREADY DONE" in note and "compare=true" in note and "台面是否明显上移" in note
    assert "台面和咖啡机一起上移" in seen["prompt"] and "保持完全不变：地面" in seen["prompt"]
    assert (tmp / "uploads/枫木服务区-修改.png").is_file() and loop._last_image == "uploads/枫木服务区-修改.png"


async def test_kickoff_skips_without_image_model(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    loop = AgentLoop(Task(prompt=ASK), "s-kb2", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")))
    assert await loop._image_kickoff() is None
