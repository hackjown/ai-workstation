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


def test_raise_plan_moves_the_counter_and_what_stands_on_it():
    g = {"x_left": 185, "x_left_top": 243, "x_right": 845, "top_y": 705, "front_top_y": 887, "base_y": 1188,
         "target_y": 858, "probe_x": 200}
    mv = ib.plan_moves(g, 29)
    assert mv[0] == {"box": [243, 693, 845, 879], "to": [243, 664, 845, 850]}      # things on the counter, not the neighbour's
    assert mv[1]["to"][1] == 850 and mv[2]["to"] == [185, 888, 845, 1188]          # counter up 29, doors stretch, base kept
    assert ib._geometry({**g, "x_right": 200}, 1084, 1451) is None                   # too narrow to be a cabinet
    loose = ib._geometry({k: v for k, v in g.items() if k not in ("probe_x", "x_left_top")} | {"top_y": 2000}, 1084, 1451)
    assert loose["probe_x"] == 200 and loose["x_left_top"] == 185 and loose["top_y"] < loose["front_top_y"]
    assert ib.wants_raise("把正中白色柜子加高到和左侧柜子台面齐平")
    assert ib.parse_geometry('好的 {"x_left": 185, "x_right": 845, "top_y": 705, "front_top_y": 887, "base_y": 1188, '
                             '"target_y": 858}', 1084, 1451)["probe_x"] == 200


def test_moves_join_without_ghosts_and_blend_keeps_the_move():
    from PIL import ImageDraw
    im = Image.new("RGB", (400, 400), (200, 200, 200))
    d = ImageDraw.Draw(im)
    d.rectangle([100, 200, 300, 210], fill=(0, 0, 0))       # a counter edge at y 200..210
    mv = [{"box": [100, 150, 300, 220], "to": [100, 130, 300, 200]}, {"box": [100, 220, 300, 380], "to": [100, 200, 300, 380]}]
    geo, _ = ie.move_regions(_bytes(im), mv, 6)
    g = Image.open(io.BytesIO(geo)).convert("L")
    assert g.getpixel((200, 185)) < 30 and g.getpixel((200, 205)) > 150   # edge moved up 20, no ghost at the joint
    flat = _bytes(Image.new("RGB", (400, 400), (200, 200, 200)))          # an AI that "undid" the move
    out = Image.open(io.BytesIO(ie.blend_seams(geo, flat, mv))).convert("L")
    assert out.getpixel((200, 185)) < 30                                  # the edge survives: AI only on seams


async def test_kickoff_measures_and_moves_for_a_raise(env, monkeypatch):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    (tmp / "uploads").mkdir()
    (tmp / "uploads/枫木服务区.png").write_bytes(_bytes(_photo()))   # white cabinet front edge at y=420
    loop = AgentLoop(Task(prompt=ASK), "s-kb3", comp, LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=2, timeout_s=30, make_plan=False, artifacts_dir=str(tmp / "art")),
                     image_cfg={"model": "gpt-image-1", "key": "k", "base_url": "https://gw/v1"})
    replies = [json.dumps({"target": "白柜", "reference": "左侧台面", "ai_prompt": "白柜加高", "question": "",
                           "geometry": None, "keep_text_boxes": [[300, 400, 440, 500]]}, ensure_ascii=False),
               '{"x_left": 100, "x_right": 450, "top_y": 380, "front_top_y": 425, "base_y": 650, "target_y": 395, '
               '"probe_x": 110}',       # task-d2d: the brief left geometry out, so it is asked for on its own
               '{"front_top_y": 421, "target_y": 400}']

    class Eyes:
        async def describe(self, system, q, b64, max_tokens=1500):
            return replies.pop(0)
    loop.vision_llm = Eyes()
    seen = {}

    async def fake_edit(base, key, model, img, prompt, timeout=300):
        seen["prompt"] = prompt
        return _bytes(Image.new("RGB", (600, 800), (200, 200, 190)))   # an AI that redraws everything flat
    monkeypatch.setattr(ie, "edit", fake_edit)
    note = await loop._image_kickoff()
    assert "raised by 20 px exactly" in note and "只修补接缝" in seen["prompt"]
    assert '"restore"' not in note                       # a text box over the cabinet would paste the old one back
    r = await loop._image_edit({"path": "uploads/枫木服务区.png", "prompt": "白柜整体加高"})
    assert r.startswith("REFUSED") and "task-d2d" in r
    out = Image.open(tmp / "uploads/枫木服务区-修改.png").convert("RGB")
    assert min(out.getpixel((300, 405))) > 238   # the white cabinet now starts 20 px higher, AI or not


def test_grounding_boxes_convert_to_pixels():
    from agent_runtime import image_brief as ib
    t = '{"target_cabinet": [171, 611, 780, 830], "items_on_top": [175, 486, 770, 611], "reference_counter": [0, 591, 172, 600], "junction_x": 171}'
    g = ib.parse_grounding(t, 1084, 1451)
    assert g and abs(g["front_top_y"] - 887) <= 1 and abs(g["target_y"] - 858) <= 1
    assert abs(g["top_y"] - 705) <= 1 and g["front_top_y"] - g["target_y"] == 29 and abs(g["probe_x"] - 185) <= 1
    assert ib.parse_grounding("no json", 100, 100) is None
