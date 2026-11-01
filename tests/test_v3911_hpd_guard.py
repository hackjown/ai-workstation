"""3.9.11 (task-a7b46fccd8): the designer job reached a draft in 15 s and then never delivered (11 make rounds,
12 overlay questions, 7 wall-rebuild scripts). The guard nudges it to furnish and deliver; open-by-nature rooms are
not reported as walls to fix."""
import importlib.util
from pathlib import Path

from agent_runtime.hpd_guard import HpdGuard

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("aiw_hpd", ROOT / "skills/house-plan-designer/scripts/aiw_hpd.py")
hpd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hpd)

DRAFT = 'HPD_DRAFT {"ok": true, "route": "trace"}'
MAKE = "python3 $HPD/scripts/aiw_hpd.py make hpd/x/spec.json --out hpd/x/work --name x"


def test_third_make_says_furnish_and_deliver():
    g = HpdGuard()
    assert g.feed("shell", {"command": "python3 aiw_hpd.py from-image a.jpg -o s.json"}, True, DRAFT) is None
    assert g.feed("shell", {"command": MAKE}) is None and g.feed("shell", {"command": MAKE}) is None
    n = g.feed("shell", {"command": MAKE})
    assert n and "--final" in n and "Stop fixing" in n
    assert g.feed("shell", {"command": MAKE + " --final hpd/x"}) is None and g.final


def test_too_many_looks_and_rebuild_scripts_are_called_out():
    g = HpdGuard()
    g.feed("shell", {"command": "python3 aiw_hpd.py from-image a.jpg -o s.json"}, True, DRAFT)
    notes = [g.feed("read_image", {"path": "work/check/overlay.png"}) for _ in range(5)]
    assert notes[:4] == [None] * 4 and "vision model" in notes[4]
    n = g.feed("files_write", {"path": "hpd/x/grid2.py", "content": "cad=json.load(open('cad-spec.json'))\nwalls=[]"})
    assert n and "Do not rebuild" in n
    assert g.feed("files_write", {"path": "hpd/x/gen3.py", "content": "walls_traced walls"}) is None   # said once


def test_no_draft_no_nudges_and_a_failed_draft_does_not_count():
    g = HpdGuard()
    g.feed("shell", {"command": "python3 aiw_hpd.py from-image a.jpg -o s.json"}, True, 'HPD_DRAFT {"ok": false}')
    assert not g.drafted
    assert all(g.feed("read_image", {"path": "x.png"}) is None for _ in range(8))


def test_deliver_nudge_after_many_steps():
    g = HpdGuard()
    g.feed("shell", {"command": "python3 aiw_hpd.py from-image a.jpg -o s.json"}, True, DRAFT)
    notes = [g.feed("shell", {"command": "ls"}) for _ in range(60)]
    assert sum(1 for n in notes if n and "nothing delivered" in n) == 2


def test_open_by_nature_rooms_are_not_walls_to_fix():
    clo = hpd.split_closure({"open": ["阳台(半赠送)", "空调位", "次卧三"],
                             "shared": [["可拓展空间", "次卧一"], ["客厅", "餐厅"], ["主卫", "客厅", "餐厅"]]})
    assert clo["open"] == ["次卧三"] and set(clo["open_ok"]) == {"阳台(半赠送)", "空调位"}
    assert clo["shared"] == [["主卫", "客厅", "餐厅"]]
    assert ["可拓展空间", "次卧一"] in clo["shared_ok"] and ["客厅", "餐厅"] in clo["shared_ok"]
