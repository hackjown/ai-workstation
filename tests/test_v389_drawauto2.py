"""3.8.9: drawing still broken after 3.8.8 (task-3fc, A3-1 photo, the user pressed 继续 twice and it stayed stuck).

* the chains are FOUND in the image (lines outside the outer walls + their ticks + OCR / one VLM read per chain);
  the model's pixel boxes are never used (3fc: left box held the wrong line x1.36, right box missed it ~0 mm)
* rooms come from the wall grid: rectilinear, no overlap, names by label position
* drawing_build auto-repairs a wrong chain box / hand-drawn rooms instead of telling the model to re-read
* the guard tells the truth (「spec 已生成但核对未通过…」), 继续 resets it AND switches to the deterministic pipeline,
  and the task always converges (best draft delivered with what is unconfirmed)
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"
FIX = Path(__file__).resolve().parent / "fixtures"
A31 = FIX / "a3-1.jpeg"
SPEC_3FC = FIX / "task-3fc-model-spec.json"
G_PHOTO = KIT / "examples" / "12-031de1aca2dd.JPG"
G_SPEC = KIT / "examples" / "xinda-g-108.spec.json"

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")


@pytest.fixture(autouse=True)
def _no_ocr_install(monkeypatch):
    monkeypatch.setenv("DRAWKIT_NO_OCR_INSTALL", "1")      # never pip-install rapidocr from a test

# A3-1, verified on zoomed crops (the extension lines): x 270.5 px -> 0 mm, 708.5 -> 10200; y 378.5 -> 0, 854.5 -> 11100
A31_AX, A31_AY = (708.5 - 270.5) / 10200, (854.5 - 378.5) / 11100
A31_CHAINS = {"top": [2400, 2600, 1700, 1850, 1650], "bottom": [3800, 2900, 3500],
              "left": [1600, 2000, 2400, 1500, 3600], "right": [4900, 4400, 1800]}
# the printed labels (the user's reading, room centres in source px)
A31_LABELS = [
    {"name": "生活阳台", "x": 525, "y": 410}, {"name": "厨房", "x": 597, "y": 437}, {"name": "卧室", "x": 430, "y": 515},
    {"name": "卫生间", "x": 525, "y": 520}, {"name": "餐厅", "x": 637, "y": 520}, {"name": "衣帽间", "x": 322, "y": 580},
    {"name": "卫生间", "x": 327, "y": 662}, {"name": "卧室", "x": 370, "y": 775}, {"name": "卧室", "x": 500, "y": 710},
    {"name": "客厅", "x": 635, "y": 690}, {"name": "过道", "x": 470, "y": 620}]


def _mods():
    sys.path.insert(0, str(KIT))
    s = importlib.util.spec_from_file_location("drawkit", KIT / "drawkit.py")
    dk = importlib.util.module_from_spec(s)
    s.loader.exec_module(dk)
    import drawauto as da
    return dk, da


def _fit(dk, img):
    def fit(spec):
        sp = json.loads(json.dumps(spec)); sp["source"]["image"] = str(img)
        return dk.fit_transform(sp)
    return fit


def _pts(r):
    if r.get("poly"):
        return r["poly"]
    b = r["box"]
    return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]


def _geometry_ok(dk, rooms):
    for r in rooms:
        p = _pts(r)
        assert all(abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6 for a, b in zip(p, p[1:] + p[:1])), r
        assert abs(dk.poly_area(p)) >= 1e5, r
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            assert dk._poly_overlap(_pts(a), _pts(b)) <= 1e4, (a["name"], b["name"])


def _given():
    """tesseract reads the A3-1 digits; without it (a CI box) the model's values are the input."""
    return None if shutil.which("tesseract") else A31_CHAINS


# ---------------------------------------------------------------------------------------------- chains

def test_chains_found_on_the_a3_1_photo():
    dk, da = _mods()
    rep = da.detect_chains(str(A31), given=_given())
    by = {c["side"]: c for c in rep["chains"]}
    assert set(by) == {"top", "bottom", "left", "right"}, rep
    for side, vals in A31_CHAINS.items():
        assert by[side]["values"] == vals, (side, by[side])
        assert len(by[side]["ticks_px"]) == len(vals) + 1
    # the lines the model never found: left at x~205 (it guessed a box at 120..220), right at x~805 (box 860..960)
    assert abs(by["left"]["line_px"] - 205) <= 3 and abs(by["right"]["line_px"] - 806) <= 4
    assert abs(by["top"]["line_px"] - 300) <= 3 and abs(by["bottom"]["line_px"] - 946) <= 4
    ppm = rep["px_per_mm"]
    assert abs(ppm["x"] / A31_AX - 1) < 0.02 and abs(ppm["y"] / A31_AY - 1) < 0.02, ppm
    assert any("top/bottom totals agree: 10200" in c for c in rep["checks"])
    assert any("left/right totals agree: 11100" in c for c in rep["checks"])


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_chain_digits_by_ocr_on_a3_1():
    dk, da = _mods()
    rep = da.detect_chains(str(A31))
    assert {c["side"]: c.get("by") for c in rep["chains"]} == {s: "ocr" for s in A31_CHAINS}


def test_chains_found_on_the_g_photo_from_values_only():
    dk, da = _mods()
    ref = json.loads(G_SPEC.read_text(encoding="utf-8"))
    given = {c["side"]: c["values"] for c in ref["chains"]}
    rep = da.detect_chains(str(G_PHOTO), given=given, ocr=False)
    assert {c["side"] for c in rep["chains"] if c.get("values")} == set(given), rep["warnings"]
    sp = json.loads(json.dumps(ref)); sp["source"]["image"] = str(G_PHOTO)
    T = dk.fit_transform(sp)["transform"]          # the hand-made reference boxes
    assert abs(rep["px_per_mm"]["x"] / T["ax"] - 1) < 0.02 and abs(rep["px_per_mm"]["y"] / T["ay"] - 1) < 0.02


def test_need_values_when_the_digits_cannot_be_read():
    dk, da = _mods()
    os.environ["DRAWKIT_NO_DIGIT_OCR"] = "1"
    try:
        with pytest.raises(da.NeedValues) as e:
            da.auto_spec(str(A31), chains=[], use_ocr=False, fit=_fit(dk, A31))
    finally:
        os.environ.pop("DRAWKIT_NO_DIGIT_OCR")
    assert set(e.value.boxes) == {"top", "bottom", "left", "right"}
    x0, y0, x1, y1 = e.value.boxes["left"]
    assert x0 < 205 < x1 and y0 < 380 and y1 > 850       # a crop around the real left line, for one VLM read


# ---------------------------------------------------------------------------------------------- synthetic A3-1

SYN_ROOMS = [("生活阳台", 5000, 0, 6700, 1600), ("厨房", 6700, 0, 8550, 1600), ("卧室", 2400, 1600, 5000, 4900),
             ("卫生间", 5000, 1600, 6700, 4900), ("餐厅", 6700, 1600, 10200, 4900), ("衣帽间", 0, 3600, 2400, 6000),
             ("过道", 2400, 4900, 6700, 6000), ("卫生间", 0, 6000, 2400, 7500), ("储藏", 2400, 6000, 3800, 7500),
             ("卧室", 0, 7500, 3800, 11100), ("卧室", 3800, 6000, 6700, 9300), ("客厅", 6700, 4900, 10200, 9300)]


def _render_a3_like(path, s=0.043, ox=270, oy=380):
    """walls (dark grey bands with black edges on a wood-coloured fill) + 4 dimension chains with ticks."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1004, 1361), "white")
    d = ImageDraw.Draw(im)
    P = lambda x, y: (ox + s * x, oy + s * y)  # noqa: E731
    for n, x0, y0, x1, y1 in SYN_ROOMS:
        fill = (196, 158, 121) if "卧" in n or n in ("衣帽间", "过道", "储藏") else (238, 236, 230)
        d.rectangle([P(x0, y0), P(x1, y1)], fill=fill)
    for n, x0, y0, x1, y1 in SYN_ROOMS:
        for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x0, y1), (x1, y1)), ((x0, y0), (x0, y1))):
            d.line([P(*a), P(*b)], fill=(0, 0, 0), width=9)
            d.line([P(*a), P(*b)], fill=(51, 51, 51), width=5)
    # door gaps in a few inner walls
    for (x, y, horiz) in ((3300, 4900, True), (4400, 6000, True), (6700, 3000, False), (1200, 6000, True)):
        a, b = (P(x - 450, y), P(x + 450, y)) if horiz else (P(x, y - 450), P(x, y + 450))
        d.line([a, b], fill=(238, 236, 230), width=11)
    W, H = 10200 * s, 11100 * s

    def chain(side, vals, line, total_line):
        acc, marks = 0, [0]
        for v in vals:
            acc += v; marks.append(acc)
        for p in (line, total_line):
            if side in ("top", "bottom"):
                d.line([(ox - 6, p), (ox + W + 6, p)], fill=(80, 80, 80), width=1)
            else:
                d.line([(p, oy - 6), (p, oy + H + 6)], fill=(80, 80, 80), width=1)
        for m in marks:
            if side in ("top", "bottom"):
                x = ox + s * m
                d.line([(x, min(line, total_line) - 8), (x, max(line, total_line) + 8)], fill=(80, 80, 80), width=1)
                d.ellipse([x - 2, line - 2, x + 2, line + 2], fill=(0, 0, 0))
            else:
                y = oy + s * m
                d.line([(min(line, total_line) - 8, y), (max(line, total_line) + 8, y)], fill=(80, 80, 80), width=1)
                d.ellipse([line - 2, y - 2, line + 2, y + 2], fill=(0, 0, 0))
    chain("top", A31_CHAINS["top"], oy - 80, oy - 114)
    chain("bottom", A31_CHAINS["bottom"], oy + H + 88, oy + H + 122)
    chain("left", A31_CHAINS["left"], ox - 65, ox - 100)
    chain("right", A31_CHAINS["right"], ox + W + 95, ox + W + 128)
    im.save(path, quality=92)
    return s


def test_synthetic_a3_like_plan_end_to_end(tmp_path):
    dk, da = _mods()
    img = tmp_path / "syn.jpg"
    s = _render_a3_like(img)
    rep = da.detect_chains(str(img), given=A31_CHAINS, ocr=False)
    assert abs(rep["px_per_mm"]["x"] / s - 1) < 0.02 and abs(rep["px_per_mm"]["y"] / s - 1) < 0.02, rep
    labels = [{"name": n, "x": 270 + s * (x0 + x1) / 2, "y": 380 + s * (y0 + y1) / 2} for n, x0, y0, x1, y1 in SYN_ROOMS]
    spec, info = da.auto_spec(str(img), chains=[{"side": k, "values": v} for k, v in A31_CHAINS.items()],
                              labels=labels, use_ocr=False, fit=_fit(dk, img), task="t-syn")
    assert info["wall_mode"] == "thick"
    assert len(spec["rooms"]) >= 10
    _geometry_ok(dk, spec["rooms"])
    rows = da.compare(spec["rooms"], [{"name": n, "box": [x0, y0, x1, y1]} for n, x0, y0, x1, y1 in SYN_ROOMS])
    assert sum(r["iou"] >= 0.8 and r["name_ok"] for r in rows) >= 10, rows
    p = tmp_path / "spec.json"; spec["source"]["image"] = str(img); p.write_text(json.dumps(spec, ensure_ascii=False))
    out = dk.run_build(str(p), str(tmp_path / "out"), "plan", None, False, task="t-syn")
    assert out["ok"], out["errors"]


# ---------------------------------------------------------------------------------------------- the real A3-1

def test_a3_1_auto_pipeline(tmp_path):
    dk, da = _mods()
    img = tmp_path / "a3-1.jpeg"; img.write_bytes(A31.read_bytes())
    given = _given()
    spec, info = da.auto_spec(str(img), chains=[{"side": k, "values": v} for k, v in (given or {}).items()],
                              labels=A31_LABELS, use_ocr=False, fit=_fit(dk, img), task="task-3fc", debug=str(tmp_path / "d.png"))
    T = spec["source"]["transform"]
    assert abs(T["ax"] / A31_AX - 1) < 0.02 and abs(T["ay"] / A31_AY - 1) < 0.02, T
    assert len(spec["rooms"]) >= 10 and "房间?" not in [r["name"] for r in spec["rooms"]]
    _geometry_ok(dk, spec["rooms"])
    # every printed label names the room at its own position
    for lb in A31_LABELS:
        mx, my = (lb["x"] - T["bx"]) / T["ax"], (lb["y"] - T["by"]) / T["ay"]
        hit = [r["name"] for r in spec["rooms"] if dk.point_in_poly(mx, my, _pts(r))]
        assert hit == [lb["name"]], (lb, hit)
    p = tmp_path / "spec.json"; spec["source"]["image"] = str(img); p.write_text(json.dumps(spec, ensure_ascii=False))
    out = dk.run_build(str(p), str(tmp_path / "out"), "plan", None, True, task="task-3fc")
    assert out["ok"], out["errors"]
    assert out["overlay_score"]["wall_match"] >= 0.9 and (tmp_path / "out" / "plan.pdf").exists()


def test_build_repairs_the_3fc_spec(tmp_path):
    """the model's own step-10 spec: wrong left/right boxes, a non-rectilinear 过道, overlaps, a door off its wall."""
    dk, _da = _mods()
    up = tmp_path / "uploads"; up.mkdir()
    (up / "photo-1793335952.jpg").write_bytes(A31.read_bytes())
    p = tmp_path / "spec.json"; p.write_text(SPEC_3FC.read_text(encoding="utf-8"), encoding="utf-8")
    os.environ.pop("DRAWKIT_NO_REPAIR", None)
    cwd = os.getcwd(); os.chdir(tmp_path)
    try:
        out = dk.run_build(str(p), str(tmp_path / "out"), "plan", None, False, task="task-3fc1eb61c5")
    finally:
        os.chdir(cwd)
    rp = " | ".join(out["repairs"])
    assert "left chain re-located" in rp and "x1.36" in rp, rp
    assert "right chain re-located" in rp and "~0 mm" in rp, rp
    assert "rooms regenerated from the wall grid" in rp, rp
    assert out["ok"], out["errors"]
    fixed = json.loads(p.read_text(encoding="utf-8"))
    _geometry_ok(dk, fixed["rooms"])
    assert not [c for c in fixed["chains"] if c.get("box_px") and c["side"] in ("left", "right")]
    assert (tmp_path / "spec.before-repair.json").exists()
    os.environ["DRAWKIT_NO_REPAIR"] = "1"     # without the repair it is the build task-3fc saw
    try:
        p.write_text(SPEC_3FC.read_text(encoding="utf-8"), encoding="utf-8")
        os.chdir(tmp_path)
        out = dk.run_build(str(p), str(tmp_path / "out2"), "plan", None, False, task="task-3fc1eb61c5")
    finally:
        os.chdir(cwd); os.environ.pop("DRAWKIT_NO_REPAIR")
    errs = " | ".join(out["errors"])
    assert "left chain segment 1: written 1600 but the drawing measures ~2181" in errs and "not horizontal/vertical" in errs


# ---------------------------------------------------------------------------------------------- the guard

def test_guard_tells_the_truth_and_resume_resets():
    from agent_runtime.drawing_phase import DrawPhase, QUIET_STEPS
    dp = DrawPhase()
    dp.after("files_write", {"path": "cad/spec.json", "content": "{}"}, True)
    dp.after("drawing_build", {"spec": "cad/spec.json"}, True,
             "drawing_build round 1/3: CHECKS FAILED (scale 1:50, 535 entities).\nERRORS: left chain segment 1: written "
             "1600 but the drawing measures ~2181 mm: re-read this number on a bigger zoom | room 过道: edge (5450.0, "
             "5550.0)->(4200.0, 9500.0) is not horizontal/vertical (rooms are rectilinear)")
    kind, detail = dp.stuck()
    assert kind == "drawing_verify" and "spec 已生成" in detail and "核对未通过" in detail and "左链第1段" in detail
    assert "×1.36" in detail and "没有写 spec" not in detail
    # reads aimed at the build errors are allowed (a budget per round), not called a loop
    thought = "左、右两条尺寸链的刻度框位置与原图不符，先看看当前裁出的尺寸链区域，再重新定框。"
    for k in range(4):
        assert dp.intent(thought, "read_image", {"path": "uploads/p.jpg", "question": f"链 {k}"}) is None
        assert dp.gate("read_image", {"path": "uploads/p.jpg"}) is None
        dp.after("read_image", {"path": "uploads/p.jpg"}, True)
    assert dp.gate("read_image", {"path": "uploads/p.jpg"}).startswith("REFUSED")
    dp.nudges = 3
    dp.resume()
    assert dp.nudges == 0 and dp.round_reads == 0 and dp.reads == 0 and dp.quiet == QUIET_STEPS and dp.resumes == 1
    for k in range(QUIET_STEPS):          # the same repeated thought right after 继续 does not pause again
        assert dp.intent(thought, "shell", {"command": f"ls {k}"}) is None


def test_escalate_texts():
    from agent_runtime import escalate as es
    q = es.question("drawing_verify", "spec 已生成并出过 1 次图，但核对未通过：左链第1段写 1600 量得 ~2181（×1.36）。")
    assert "核对没通过" in q and "左链" in q and "没有动手写 spec" not in q


# ---------------------------------------------------------------------------------------------- the loop: task-3fc replay

from test_phase5_agent_loop import env  # noqa: E402,F401

LOOP_3FC = [  # the model's real thoughts after the failed build (steps 12-21)
    ("左、右两条尺寸链的刻度框位置与原图不符（拟合比例偏差 40.7%），先看看当前裁出的尺寸链区域，再重新定框。", "shell"),
    ("之前对尺寸链读取有误，导致坐标轴拟合比例失真，房间多边形也乱了。我需要先重新读取四条尺寸链的像素刻度。", "shell"),
    ("我先看看上一轮 spec 里左右两条尺寸链的 box_px 是否框错了导致测量偏差，先核对一下原图上尺寸链的实际像素位置。", "shell"),
    ("问题定位：左链数值与照片刻度完全对不上，右链刻度框根本没框到尺寸线，说明左右链的像素框选错了。我需要先看原图。", "read_image"),
    ("我先说明现状：上一版 spec 报了两个明显问题：尺寸链数字和刻度对不上，以及房间多边形里出现了斜边。先查看原图。", "shell"),
    ("我先看清上一轮建图报的错：左、右尺寸链和刻度的拟合对不上，说明链的像素框画错了位置。先看源图整体。", "read_image"),
    ("我先看现在的报告和 spec，问题主要是尺寸链 box 的像素框和过道房间的多边形不合法。先确认原图上尺寸链的真实像素位置。", "read_image"),
    ("现在主要问题是坐标匹配有误：像素范围取错了，左边尺寸链、右边尺寸链的刻度位置都错了。先查看左边尺寸链刻度。", "shell"),
    ("我先看一下草稿生成的平面图，确认当前整体状态，然后按照片校正 spec。", "read_image"),
    ("我先看一下当前 spec 和生成图的问题：left 链读到的刻度间距显示我把内容和像素映射搞错了。先读出当前 spec 的完整内容。", "shell"),
]


class _Eyes:
    """the vision model: one gridded label read and per-chain value reads, as JSON."""
    name = "eyes"

    def __init__(self):
        self.asked = []

    async def describe(self, system, user, b64, max_tokens=900):
        self.asked.append(user[:40])
        if "房间名称文字" in user:
            return json.dumps({"labels": A31_LABELS}, ensure_ascii=False)
        if "尺寸链（" in user:
            vertical = "上到下" in user
            n = int(user.split("（")[1].split(" 段")[0])
            pick = [v for k, v in A31_CHAINS.items() if len(v) == n and (k in ("left", "right")) == vertical]
            return json.dumps({"values": pick[0] if pick else []})
        return "一张户型图。"


async def test_task_3fc_replay_converges_instead_of_pausing(env, monkeypatch):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task, TaskStatus
    from llm_router import LLMRouter
    from tool_router import ToolExecutor
    from test_v388_drawloop import _Thoughts
    backend, comp, tmp = env
    monkeypatch.setenv("HOME", str(tmp / "home")); (tmp / "home").mkdir()
    monkeypatch.setenv("DRAWKIT_NO_REPAIR", "1")       # the model's build fails exactly as it did in task-3fc
    monkeypatch.setenv("DRAWKIT_LIBRECAD", "0")
    if not shutil.which("tesseract"):
        monkeypatch.setenv("DRAWKIT_NO_DIGIT_OCR", "1")
    (tmp / "uploads").mkdir()
    (tmp / "uploads" / "photo-1793335952.jpg").write_bytes(A31.read_bytes())
    tid = "task-3fc1eb61c5"
    model_spec = json.loads(SPEC_3FC.read_text(encoding="utf-8"))
    model_spec["source"]["task"] = tid
    bad_chains = [{"side": "top", "values": A31_CHAINS["top"], "box_px": [150, 250, 900, 340]},
                  {"side": "bottom", "values": A31_CHAINS["bottom"], "box_px": [150, 1020, 900, 1120]},
                  {"side": "left", "values": A31_CHAINS["left"], "box_px": [120, 300, 220, 1060]},
                  {"side": "right", "values": A31_CHAINS["right"], "box_px": [860, 300, 960, 1060]}]
    crop = "cd ~/workspace && python3 -c \"from PIL import Image; im=Image.open('uploads/photo-1793335952.jpg'); im.crop((0,0,500,600)).save('cad/c{k}.png')\""
    script = [("这是新任务：先查看原图，看清整体布局。", "read_image", {"path": "uploads/photo-1793335952.jpg", "question": "整体布局"}),
              ("我已读懂原图，现在直接生成草稿 spec。", "drawing_auto", {"image": "uploads/photo-1793335952.jpg", "chains": bad_chains}),
              ("我先把草稿整理成正式 spec：按原图标签补上房间名，加上门窗。", "files_write",
               {"path": "cad/spec.json", "content": json.dumps(model_spec, ensure_ascii=False)}),
              ("已写好 spec，现在按 spec 整张生成图纸并看检查结果。", "drawing_build", {"spec": "cad/spec.json"})]
    for k, (th, tool) in enumerate(LOOP_3FC * 3):
        script.append((th, tool, {"path": "uploads/photo-1793335952.jpg", "question": f"尺寸链在哪 {k}"} if tool == "read_image"
                       else {"command": crop.replace("{k}", str(k))}))
    prov = _Thoughts(script)
    asked = []

    async def ask(q, opts, extra=None):
        asked.append(q)
        return "继续，换个方法再试"
    eyes = _Eyes()
    loop = AgentLoop(Task(id=tid, prompt="【技能：cad-drawing】\n画出上传图片里面的户型图，仔细对照了画。\n\n附件（已放到电脑上）：\n"
                                         "- ~/workspace/uploads/photo-1793335952.jpg"),
                     "s-3fc", comp, LLMRouter([prov]), ToolExecutor(comp), ask=ask,
                     config=LoopConfig(max_steps=80, timeout_s=600, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop.vision_llm = eyes

    async def fake_type(cmds, open_path, intent):
        return None, {}, True, "ok"
    loop._type_cad = fake_type
    await loop.run()
    hist = "\n".join(loop.history)
    # never the false 「没有动手写 spec」 after the spec was written and built
    assert not [q for q in asked if "没有动手写 spec" in q], asked
    assert "deterministic pipeline" in hist, hist[-3000:]
    assert loop.task.status == TaskStatus.SUCCEEDED, (loop.task.status, loop.task.result)
    res = loop.task.result or ""
    assert "图纸已生成并通过自动核对" in res or "最好的草稿" in res, res
    assert "精度说明" in res
    sent = sorted(p.name for p in (tmp / "art" / "s-3fc" / "files").glob("*")) if (tmp / "art" / "s-3fc" / "files").exists() else []
    assert any(n.endswith(".png") for n in sent) and any(n.endswith(".dxf") for n in sent), sent
    assert loop.step < 30, loop.step
    spec = json.loads((tmp / "cad" / "spec.json").read_text(encoding="utf-8"))
    dk, _ = _mods()
    _geometry_ok(dk, spec["rooms"])
    # file-only drawing steps never took a desktop screenshot + vision description
    assert not any(a.startswith("Describe") or "屏幕" in a for a in eyes.asked), eyes.asked


async def test_resume_runs_the_deterministic_pipeline(env, monkeypatch):  # noqa: F811
    """继续 after a pause: counters reset, and the pipeline runs at once (no immediate re-pause)."""
    from agent_runtime import AgentLoop, LoopConfig, drawing_hooks as dh
    from aiw_protocols import Task, TaskStatus
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    monkeypatch.setenv("HOME", str(tmp / "home")); (tmp / "home").mkdir()
    monkeypatch.setenv("DRAWKIT_LIBRECAD", "0")
    if not shutil.which("tesseract"):
        monkeypatch.setenv("DRAWKIT_NO_DIGIT_OCR", "1")
    (tmp / "uploads").mkdir()
    (tmp / "uploads" / "p.jpg").write_bytes(A31.read_bytes())
    loop = AgentLoop(Task(id="task-r1", prompt="【技能：cad-drawing】照着画户型图 ~/workspace/uploads/p.jpg"), "s-r1", comp,
                     LLMRouter([ScriptedProvider([])]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=10, timeout_s=300, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop.vision_llm = _Eyes()

    async def fake_type(cmds, open_path, intent):
        return None, {}, True, "ok"
    loop._type_cad = fake_type
    loop._dphase.nudges = 3
    note = await dh.on_resume(loop, "You were stuck.")
    dp = loop._dphase
    assert dp.resumes == 1 and dp.nudges == 0 and dp.auto_runs == 1
    assert dp.last_ok is True and loop.task.status == TaskStatus.SUCCEEDED, (dp.last_errors, loop.task.result)
    assert "已生成并通过自动核对" in (loop.task.result or "")
    assert md5(tmp / "uploads" / "p.jpg") == json.loads((tmp / "cad" / "spec.json").read_text())["source"]["md5"]


def md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def test_lessons_drop_pixel_box_guessing(tmp_path):
    from aiw_api.lessons import Lessons
    from aiw_api.store import Store
    st = Store(str(tmp_path / "a.db"))
    les = Lessons(st)
    n_skill = len([r for r in les.items(scope="skill") if r["key"] == "cad-drawing"])
    assert any("不要猜像素框" in r["fix"] for r in les.items(scope="skill"))
    assert any("继续" in r["problem"] for r in les.items(scope="general"))
    # the learned lesson task-3fc applied ("drawkit ticks ... --box x1,y1,x2,y2") goes with the v2 seed
    st.kv_set("lessons_seeded_drawloop_v2", None)
    st._x("INSERT INTO lessons (id, scope, key, problem, fix, source, weight, hits, created_at, updated_at) "
          "VALUES ('les-box', 'skill', 'cad-drawing', 'drawing_read misreads a chain', ?, 'auto', 1, 0, 0, 0)",
          ("Run python3 drawkit.py ticks <img> --side left --box x1,y1,x2,y2 to get the tick pixels",))
    les.seed_draw2()
    assert not [r for r in les.items() if r["id"] == "les-box"]
    assert len([r for r in les.items(scope="skill") if r["key"] == "cad-drawing"]) == n_skill
