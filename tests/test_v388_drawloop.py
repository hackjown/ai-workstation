"""3.8.8: the model analysed forever and never executed (task-41c), plus the user's fresh-job rule (2026-10-30).

* phase state machine: READ budget -> reading tools refused / hidden, the spec must be written
* repeated-intent detector: near-identical thoughts with nothing new -> nudge, then escalate
* findings scratchpad (FACTS) survives history trimming: it is in the TASK text every step
* stale-spec guard: md5 + task stamp in spec.source
* drawkit auto on the G sample: room count, labels, overlap with the hand-made reference spec
* fresh job per task: own folder, cad/ symlink, continuation inherits, identical deliveries refused, lessons filtered
"""
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "skills" / "cad-drawing"
EX = KIT / "examples" / "xinda-g-108.spec.json"
PHOTO = KIT / "examples" / "12-031de1aca2dd.JPG"

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")


def _dk():
    s = importlib.util.spec_from_file_location("drawkit", KIT / "drawkit.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


# the 41c loop, as the model wrote it (thinking off): every step restarts from the user's correction
LOOP_41C = [
    ("伟少这次指出：尺寸是对的，但户型画错了——房间的位置和排列与照片不符。我先把当前生成的图和原图并排看一次，找出房间布局到底哪里不一样。", "read_image"),
    ("我先看清楚原图到底怎么布局——上一轮只裁了局部，放大图丢了。这一步直接用原图，带像素网格读一遍平面区的房间位置。", "read_image"),
    ("伟少说尺寸对、户型画错了，我需要认真对照照片重看房间布局与门的开口位置。先把平面区放大裁出来，再用网格图精读各房间标签和墙线的实际像素位置。", "shell"),
    ("伟少指出户型画错了、尺寸对。我先放大看照片的平面区，弄清真实房间布局（各房间标签的位置），再只改 spec 里的房间划分。", "read_image"),
    ("先查清现有 spec 里的房间和照片标签到底差在哪，把照片平面区放大看清房间顺序（这次不用再整体猜，逐块对照）。", "shell"),
    ("伟少指出户型画错了。我需要先认真看清原图的房间布局，而不是沿用旧 spec。先放大看平面区域，确认每个房间标签的实际位置。", "read_image"),
    ("伟少说尺寸对了但户型画错了，说明问题出在房间布局。我先看现有 spec 里的房间布置，再对照照片逐一核对每个房间的实际位置。", "shell"),
    ("伟少说“尺寸对但户型画错了”，说明问题在房间布局，不在尺寸。我先把照片的平面区裁出来放大，认真看一遍真实房间排布，再改 spec 里的 rooms。", "shell"),
    ("伟少说尺寸对、户型画错了。我需要把放大后的平面图看清楚，逐个确认每个房间标签的真实位置，再改 spec。先看刚裁出的 plan_now.png。", "read_image"),
    ("伟少说尺寸对但户型画错了，我得先把自己画的图和原图并排对比，看清房间布局差在哪。先裁剪原图的平面区并生成对比图。", "shell"),
]
CROP = "cd ~/workspace && python3 -c \"from PIL import Image; im=Image.open('uploads/p.png'); im.crop((0,0,10,10)).save('cad/c.png')\""


def _args(tool, k):
    if tool == "read_image":
        return {"path": "uploads/p.png", "question": f"房间标签在哪 {k}"}
    return {"command": CROP + f" # {k}"}


# ---------------------------------------------------------------------------------------------- phase machine

def test_read_budget_forces_the_spec():
    from agent_runtime.drawing_phase import DrawPhase, READ, SPEC
    dp = DrawPhase(read_budget=8)
    refused = None
    for k, (thought, tool) in enumerate(LOOP_41C * 2):
        a = _args(tool, k)
        refused = dp.gate(tool, a)
        if refused:
            break
        dp.after(tool, a, True, "")
    assert refused and "READ budget" in refused and "写 spec" in refused, refused
    assert dp.reads == 8 and dp.forced and "read_image" in dp.hidden() and "drawing_read" in dp.hidden()
    assert "停止分析" in dp.block()
    # writing the spec opens the next phase and the reading tools come back
    assert dp.gate("files_write", {"path": "cad/spec.json", "content": "{}"}) is None
    dp.after("files_write", {"path": "cad/spec.json", "content": "{}"}, True)
    assert dp.phase == SPEC and not dp.forced and dp.hidden() == ()
    dp.after("drawing_build", {"spec": "cad/spec.json"}, True, "drawing_build round 1/3: ALL CHECKS PASS")
    assert dp.phase == "VERIFY" and dp.verify == 1
    assert DrawPhase().phase == READ


def test_repeated_intent_nudges_then_escalates():
    from agent_runtime.drawing_phase import DrawPhase
    dp = DrawPhase(read_budget=999)
    seen = []
    for k, (thought, tool) in enumerate(LOOP_41C * 3):
        st = dp.intent(thought, tool, _args(tool, k))
        if st:
            seen.append((k, st))
        if st == "escalate":
            break
    assert [s for _, s in seen][:2] == ["nudge", "nudge"] and seen[-1][1] == "escalate", seen
    assert seen[0][0] <= 5, seen          # caught within the first few repeats, not after 150 steps
    # a step that writes something new is progress: the window starts over
    dp2 = DrawPhase(read_budget=999)
    for k, (thought, tool) in enumerate(LOOP_41C[:3]):
        assert dp2.intent(thought, tool, _args(tool, k)) is None
        dp2.note("notes", f"n{k}", "x")    # a new finding each step
    assert dp2.intent(LOOP_41C[3][0], "shell", _args("shell", 3)) is None


def test_facts_are_locked_and_shown_every_step():
    from agent_runtime.drawing_phase import DrawPhase
    dp = DrawPhase()
    dp.after("drawing_read", {"path": "uploads/p.png", "box": [195, 180, 465, 200], "side": "top"}, True,
             "chain [195, 180, 465, 200] (left->right, zoom x6): values [1300, 1800, 2800, 3300, 900], total 10100 mm.")
    assert dp.facts["chains"]["top"]["values"] == [1300, 1800, 2800, 3300, 900] and "chains:top" in dp.locked
    assert dp.note("chains", "top", [1, 2]).startswith("chains:top is LOCKED")
    dp.note("labels", "客厅", {"name": "客厅", "at_px": [310, 380]})
    b = dp.block()
    assert "FACTS so far" in b and "1300" in b and "客厅" in b and "LOCKED" in b


# ---------------------------------------------------------------------------------------------- stale spec

def test_stale_spec_by_md5_and_task(tmp_path):
    dk = _dk()
    img = tmp_path / "a.jpg"; img.write_bytes(PHOTO.read_bytes())
    sp = json.loads(EX.read_text(encoding="utf-8"))
    sp["source"]["image"] = str(img)
    sp["source"]["md5"] = "0" * 32
    assert any("md5" in r for r in dk.stale_reasons(sp))
    sp["source"]["md5"] = dk.file_md5(img)
    assert dk.stale_reasons(sp) == []
    sp["source"]["task"] = "task-old"
    assert any("task-old" in r for r in dk.stale_reasons(sp, "task-new")) and dk.stale_reasons(sp, "task-old") == []
    p = tmp_path / "spec.json"; sp["source"]["md5"] = "1" * 32; p.write_text(json.dumps(sp, ensure_ascii=False))
    rep = dk.run_build(str(p), str(tmp_path / "out"), "plan", None, False, task="task-old")
    assert not rep["ok"] and rep.get("stale") and rep["errors"][0].startswith("STALE SPEC")
    from agent_runtime.drawing_phase import stale_verdict
    assert stale_verdict({"title": "G", "cur_md5": "b"}, "t1")          # no stamp + a known upload -> stale
    assert stale_verdict({"md5": "a", "task": "t1", "cur_md5": "a"}, "t1") is None
    assert "another image" in stale_verdict({"md5": "a", "task": "t1", "cur_md5": "b"}, "t1")


# ---------------------------------------------------------------------------------------------- drawkit auto

G_LABELS = [  # what the one vision read returns on the gridded crop (the G photo prints only 管井 / 阳台 itself)
    {"name": "厨房", "x": 302, "y": 235}, {"name": "卫生间", "x": 257, "y": 265}, {"name": "管井", "x": 252, "y": 302},
    {"name": "餐厅", "x": 310, "y": 290}, {"name": "次卧", "x": 360, "y": 272}, {"name": "主卫", "x": 382, "y": 320},
    {"name": "过道", "x": 347, "y": 327}, {"name": "玄关", "x": 255, "y": 335}, {"name": "客厅", "x": 310, "y": 380},
    {"name": "主卧", "x": 372, "y": 380}, {"name": "书房", "x": 240, "y": 405}, {"name": "阳台", "x": 305, "y": 445}]


def test_auto_draft_on_the_g_sample(tmp_path):
    dk = _dk()
    sys.path.insert(0, str(KIT))
    import drawauto as da
    ref = json.loads(EX.read_text(encoding="utf-8"))
    chains = [{k: c[k] for k in ("side", "values", "box_px") if k in c} for c in ref["chains"]]
    img = str(PHOTO)

    def fit(spec):
        sp = json.loads(json.dumps(spec)); sp["source"]["image"] = img
        return dk.fit_transform(sp)
    spec, info = da.auto_spec(img, chains=chains, labels=G_LABELS, use_ocr=False, task="task-g", fit=fit,
                              debug=str(tmp_path / "dbg.png"))
    names = sorted(r["name"] for r in spec["rooms"])
    assert len(spec["rooms"]) == 12 and names == sorted(r["name"] for r in ref["rooms"]), names
    rows = da.compare(spec["rooms"], ref["rooms"])
    good = [r for r in rows if r["iou"] >= 0.5 and r["name_ok"]]
    assert len(good) >= 10, rows
    assert spec["source"]["md5"] == dk.file_md5(img) and spec["source"]["task"] == "task-g"
    assert (tmp_path / "dbg.png").exists()
    # the draft is a valid spec the generator builds (walls on the photo's dark pixels)
    p = tmp_path / "spec.json"; spec["source"]["image"] = img
    p.write_text(json.dumps(spec, ensure_ascii=False))
    rep = dk.run_build(str(p), str(tmp_path / "out"), "plan", None, False, task="task-g")
    assert not rep.get("stale") and rep.get("overlay_score", {}).get("wall_match", 0) >= 0.8, rep.get("overlay_score")
    # without chains there is no scale: a clear error, never a guess
    with pytest.raises(ValueError):
        da.auto_spec(img, chains=[], labels=G_LABELS, use_ocr=False)


def test_auto_cli_and_label_cleaning(tmp_path):
    sys.path.insert(0, str(KIT))
    import drawauto as da
    assert da.clean_label("次卧 12.6m²") == "次卧" and da.clean_label("胎") is None and da.clean_label("生活阳台") == "生活阳台"
    ref = json.loads(EX.read_text(encoding="utf-8"))
    (tmp_path / "ch.json").write_text(json.dumps({"chains": [{k: c[k] for k in ("side", "values", "box_px")} for c in ref["chains"]]}))
    (tmp_path / "lab.json").write_text(json.dumps({"labels": G_LABELS}, ensure_ascii=False))
    r = subprocess.run([sys.executable, str(KIT / "drawkit.py"), "auto", str(PHOTO), "--chains", str(tmp_path / "ch.json"),
                        "--labels", str(tmp_path / "lab.json"), "--no-ocr", "--out", str(tmp_path / "s.json"), "--task", "t1"],
                       capture_output=True, text=True, timeout=120)
    line = [x for x in r.stdout.splitlines() if x.startswith("DRAWKIT_AUTO ")][0]
    d = json.loads(line.split(" ", 1)[1])
    assert d["ok"] and len(d["rooms"]) == 12 and d["plan_px"] and json.loads((tmp_path / "s.json").read_text())["source"]["task"] == "t1"


# ---------------------------------------------------------------------------------------------- fresh job per task

def _sh(cmd, root):
    env = dict(os.environ, VM_AGENT_FILES_ROOT=str(root))
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=30).stdout


def test_task_folder_and_cad_symlink(tmp_path):
    from aiw_protocols import Task
    from agent_runtime import taskdir as td
    (tmp_path / "cad").mkdir()
    (tmp_path / "cad" / "spec.json").write_text("{\"title\": \"G\"}")          # a legacy shared cad/ from before
    a = Task(id="task-a", prompt="画图")
    out = _sh(td.setup_command(a, cad=True), tmp_path)
    assert "legacy cad/ moved" in out
    assert (tmp_path / "cad").is_symlink() and (tmp_path / "cad").resolve() == (tmp_path / "tasks/task-a/cad").resolve()
    assert not (tmp_path / "cad" / "spec.json").exists()                       # the new task starts EMPTY
    (tmp_path / "cad" / "spec.json").write_text("{\"title\": \"A\"}")
    b = Task(id="task-b", prompt="画图（同一张照片，同 md5）")                    # a new task: a new, empty folder
    _sh(td.setup_command(b, cad=True), tmp_path)
    assert not (tmp_path / "cad" / "spec.json").exists() and (tmp_path / "tasks/task-a/cad/spec.json").exists()
    c = Task(id="task-c", prompt="改一下", continues="task-a")                   # same task card: inherits task-a
    assert td.root_id(c) == "task-a" and td.rel_dir(c) == "tasks/task-a"
    _sh(td.setup_command(c, cad=True), tmp_path)
    assert json.loads((tmp_path / "cad" / "spec.json").read_text())["title"] == "A"
    assert (tmp_path / "tasks/task-c").is_symlink()
    assert "FRESH-JOB RULE" in td.rule(b) and "md5" in td.rule(b)


def test_delivered_index_refuses_another_tasks_output(tmp_path):
    from agent_runtime import taskdir as td
    idx = td.index_for(str(tmp_path))
    b64 = base64.b64encode(b"PNG-bytes").decode()
    sha = td.sha256_b64(b64)
    idx.record(sha, "task-a", "plan.png")
    assert idx.conflict(sha, "task-a") is None                 # the same task (or its continuation) may resend
    assert idx.conflict(sha, "task-b")["task"] == "task-a"


def test_lessons_never_teach_reusing_old_output(tmp_path):
    from aiw_api.lessons import Lessons, reuses_old_output, SEED_DRAW
    from aiw_api.store import Store
    bad = "First run md5sum on the new upload; if it is identical, reuse the earlier spec and resend the old png"
    assert reuses_old_output(bad) and not any(reuses_old_output(f) for _, _, _, f in SEED_DRAW)
    st = Store(str(tmp_path / "a.db"))
    les = Lessons(st)
    assert les.add("skill", "cad-drawing", "re-uploaded image may be the same file", bad, "auto") is None
    # a learned 'reuse' lesson from before 3.8.8 is removed by the drawloop seed
    st.kv_set("lessons_seeded_drawloop_v1", None)
    st._x("INSERT INTO lessons (id, scope, key, problem, fix, source, weight, hits, created_at, updated_at) "
          "VALUES ('les-old', 'skill', 'cad-drawing', 'same file', ?, 'auto', 1, 0, 0, 0)", (bad,))
    les.seed_draw()
    assert not [r for r in les.items() if r["id"] == "les-old"]
    assert any("全新" in r["fix"] for r in les.items(scope="general"))


# ---------------------------------------------------------------------------------------------- the loop

from test_phase5_agent_loop import env  # noqa: E402,F401


class _Thoughts:
    """replays (thought, tool, args): the 41c loop with its real thoughts."""
    name = "thoughts"

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def decide(self, req):
        from llm_router.router import LLMDecision
        self.requests.append(req)
        if not self.script:
            return LLMDecision("script exhausted", "fail", {"reason": "script exhausted"})
        th, tool, args = self.script.pop(0)
        return LLMDecision(th, tool, args)

    async def describe(self, system, user, b64, max_tokens=900):
        return "一张户型图。"


async def test_looping_transcript_is_stopped_by_the_gate(env, monkeypatch):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    monkeypatch.setenv("HOME", str(tmp / "home")); (tmp / "home").mkdir()
    (tmp / "uploads").mkdir()
    from PIL import Image
    Image.new("RGB", (40, 40), "white").save(tmp / "uploads" / "p.png")
    script = [(th, tool, _args(tool, k)) for k, (th, tool) in enumerate(LOOP_41C * 6)]
    prov = _Thoughts(script)
    loop = AgentLoop(Task(id="task-41c", prompt="【技能：cad-drawing】照着这张照片画户型平面图 CAD ~/workspace/uploads/p.png"),
                     "s-loop", comp, LLMRouter([prov]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=60, timeout_s=120, make_plan=False, artifacts_dir=str(tmp / "art")))
    loop.vision_llm = prov
    await loop.run()
    res = loop.task.result or ""
    assert "反复分析" in res, res[:400]
    assert loop.step < 25, loop.step                         # stopped early, not after 150 steps
    hist = "\n".join(loop.history)
    assert "SKIPPED: the last" in hist or "REFUSED" in hist
    t = prov.requests[-1].task
    assert "DRAWING PHASE" in t and "FRESH-JOB RULE" in t and "tasks/task-41c" in t
    assert (tmp / "cad").is_symlink() and (tmp / "tasks/task-41c/cad").is_dir()


async def test_send_file_identical_to_another_tasks_output_is_refused(env):  # noqa: F811
    from agent_runtime import AgentLoop, LoopConfig, taskdir as td
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor
    backend, comp, tmp = env
    (tmp / "old.png").write_bytes(b"\x89PNG old result of task-a")
    td.index_for(str(tmp / "art")).record(td.sha256_b64(base64.b64encode((tmp / "old.png").read_bytes()).decode()),
                                         "task-a", "plan.png")
    (tmp / "new.png").write_bytes(b"\x89PNG made in this task")
    prov = ScriptedProvider([("send_file", {"path": "old.png"}), ("send_file", {"path": "new.png"}),
                             ("done", {"summary": "ok"})])
    loop = AgentLoop(Task(id="task-b", prompt="把结果图发给我"), "s-del", comp, LLMRouter([prov]), ToolExecutor(comp),
                     config=LoopConfig(max_steps=6, timeout_s=60, make_plan=False, artifacts_dir=str(tmp / "art")))
    await loop.run()
    hist = "\n".join(loop.history)
    assert "REFUSED: old.png is byte-identical" in hist, hist
    files = list((tmp / "art" / "s-del" / "files").glob("*")) if (tmp / "art" / "s-del" / "files").exists() else []
    assert [f.name for f in files] == ["new.png"]
    idx = td.index_for(str(tmp / "art"))
    assert idx.owner(td.sha256_b64(base64.b64encode(b"\x89PNG made in this task").decode()))["task"] == "task-b"
