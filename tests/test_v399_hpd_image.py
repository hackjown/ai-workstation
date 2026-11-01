"""v3.9.9 house-plan-designer, picture route (task-3ddae3f2f2): a small 售楼处效果图 is sent to the dimension-chain route
at once, a normal plan keeps the analyze route; a chat attachment whose direct push failed is still named by its real
path (the run-start sync puts it there), never as 没能放到电脑上."""
import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HPD = ROOT / "skills" / "house-plan-designer"


def _imgcheck(tmp_path, img, w, h):
    r = subprocess.run([sys.executable, str(HPD / "scripts" / "aiw_hpd.py"), "imgcheck", str(img), "--out",
                        str(tmp_path / "a0"), "--total-width", str(w), "--total-height", str(h)],
                       capture_output=True, text=True, timeout=300)
    line = [x for x in r.stdout.splitlines() if x.startswith("HPD_IMGCHECK ")][-1]
    return json.loads(line.split(" ", 1)[1])


def test_imgcheck_normal_plan_keeps_analyze(tmp_path):
    pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    res = _imgcheck(tmp_path, HPD / "reference" / "example-input.jpg", 12340, 11940)
    assert res["route"] == "analyze", res
    assert (tmp_path / "a0" / "draft.json").exists()


def test_imgcheck_tiny_render_goes_dims(tmp_path):
    pytest.importorskip("cv2")
    from PIL import Image
    small = tmp_path / "small.jpg"
    im = Image.open(HPD / "reference" / "example-input.jpg")
    im.resize((im.width // 4, im.height // 4)).save(small)      # ~170 px plan body, a 240 wall ~3 px
    res = _imgcheck(tmp_path, small, 12340, 11940)
    assert res["route"] == "dims", res
    assert "尺寸链" in res["advice"]


def test_skill_md_names_imgcheck():
    text = (HPD / "SKILL.md").read_text(encoding="utf-8")
    assert "aiw_hpd.py imgcheck" in text and 'route: "dims"' in text


def test_failed_push_still_names_real_path(tmp_path, monkeypatch):
    from aiw_api import chat as chat_mod
    from aiw_api import uploads as up_mod
    art = tmp_path / "artifacts"
    (art / "uploads").mkdir(parents=True)
    f = art / "uploads" / "1793525643-photo-1793525643.jpg"
    f.write_bytes(b"jpg")

    async def boom(*a, **k):
        raise asyncio.TimeoutError()          # str() is empty: the old text said 没能放到电脑上 with no reason
    monkeypatch.setattr(up_mod, "push_to_vm", boom)
    c = chat_mod.Chat.__new__(chat_mod.Chat)
    c.settings = types.SimpleNamespace(artifacts_dir=str(art), machines=[])
    c.sessions = types.SimpleNamespace(computer=lambda mid: None, cloud=None)
    out = asyncio.run(c._push_files([{"name": "photo-1793525643.jpg", "artifact": str(f)}], "ai-linux-001"))
    assert out[0]["vm_path"] == "~/workspace/uploads/photo-1793525643.jpg"
    assert out[0]["vm_pending"] and out[0]["vm_error"] == "TimeoutError"


def _hpd():
    sys.path.insert(0, str(HPD / "scripts"))
    import aiw_hpd
    return aiw_hpd


def test_join_gaps_puts_openings_and_doors_on_walls():
    h = _hpd()
    walls = [{"a": [0, 0], "b": [2000, 0], "t": 240}, {"a": [3500, 0], "b": [6000, 0], "t": 240},   # window gap
             {"a": [0, 3000], "b": [1500, 3000], "t": 120}, {"a": [2400, 3000], "b": [4000, 3000], "t": 120}]  # door gap
    out, doors = h._join_gaps(walls, [[2750, 0]])
    hs = sorted((w["a"][1], w["a"][0], w["b"][0]) for w in out)
    assert hs == [(0, 0, 6000), (3000, 0, 4000)]
    assert [(round(f), round(t)) for _, f, t in doors] == [(1500, 2400)]


def test_close_ends_runs_a_free_end_to_the_next_wall_with_a_door():
    h = _hpd()
    walls = [{"a": [0, 0], "b": [0, 4000], "t": 240}, {"a": [5000, 0], "b": [5000, 4000], "t": 240},
             {"a": [0, 0], "b": [5000, 0], "t": 240}, {"a": [0, 4000], "b": [5000, 4000], "t": 240},
             {"a": [2500, 0], "b": [2500, 3100], "t": 120}]          # partition stops 900 short of the south wall
    adds = h._close_ends(walls)
    part = walls[4]
    assert part["b"] == [2500, 4000]
    assert [(t, round(f), round(e)) for _, f, e, t in adds] == [("door", 3100, 4000)]
    clo = h.room_closure({"walls": walls, "rooms": [{"at": [1200, 2000], "name": "A"}, {"at": [3800, 2000], "name": "B"}]})
    assert clo == {"open": [], "shared": []}


def test_web_task_row_title_never_squeezed():
    css = (ROOT / "apps" / "web" / "home.css").read_text(encoding="utf-8")
    assert ".item.tk > .grow { flex:1 1 12em" in css and ".item.tk { flex-wrap:wrap; }" in css


def test_designer_note_starts_pictures_with_from_image():
    from agent_runtime import skills
    note = next(s for s in skills.CATALOG if s["id"] == "house-plan-designer")["note"]
    assert "from-image" in note
    text = (HPD / "SKILL.md").read_text(encoding="utf-8")
    assert "aiw_hpd.py from-image" in text
