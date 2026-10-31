"""v3.9.3: a cloud computer in its 8-hour cooldown hands new work to the local backup computer (queued in order,
said in the chat); DWG export tries every way to start ODA under an ASCII name and says what went wrong."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from aiw_api import roles

ROOT = Path(__file__).resolve().parents[1]


class Store:
    def __init__(self, machines, kv=None, queued=()):
        self._m = {m["id"]: m for m in machines}
        self.kv = kv or {}
        self.q = list(queued)

    def machines(self):
        return list(self._m.values())

    def machine(self, mid):
        return self._m.get(mid)

    def kv_get(self, k, d=None):
        return self.kv.get(k, d)

    def tasks_by_status(self, st):
        return self.q


class Cloud:
    def __init__(self, cooling):
        self.cooling = cooling

    def entries(self):
        return {m: {"status": "ready"} for m in ("cloud-a", "cloud-b", "cloud-c")}

    def is_cloud(self, mid):
        return mid in self.entries()

    async def state(self, mid, max_age=20):
        if mid in self.cooling:
            return {"state": "stopped", "cooldown_until": "2999-01-01T00:00:00Z"}
        return {"state": "paused", "cooldown_until": None}


def _sessions(cooling=(), busy_local=False, queued=()):
    ms = [{"id": "cloud-a", "name": "云1"}, {"id": "cloud-b", "name": "云2"}, {"id": "cloud-c", "name": "云3"},
          {"id": "ai-linux-001", "name": "本地VM"}]
    st = Store(ms, {"machine_roles": {"machines": {"cloud-a": "design", "cloud-b": "media", "cloud-c": "office"}}},
               queued)
    return SimpleNamespace(store=st, cloud=Cloud(set(cooling)),
                           busy_session=lambda mid: (busy_local and mid == "ai-linux-001") or None)


def test_cooling_cloud_goes_to_local_backup():
    s = _sessions(cooling={"cloud-a"})
    mid, n = asyncio.run(roles.pick_live(s, "把户型图画成 CAD", None))
    assert mid == "ai-linux-001" and n["cooling"] == "cloud-a" and n["ahead"] == 0
    assert "冷却期" in roles.notice_text(n) and "本地VM" in roles.notice_text(n)


def test_paused_cloud_is_not_cooling_and_keeps_the_task():
    s = _sessions(cooling=set())
    mid, n = asyncio.run(roles.pick_live(s, "把户型图画成 CAD", None))
    assert mid == "cloud-a" and n is None


def test_backup_queue_is_counted():
    s = _sessions(cooling={"cloud-b"}, busy_local=True, queued=[{"machine_id": "ai-linux-001"}])
    mid, n = asyncio.run(roles.pick_live(s, "剪一个视频", None))
    assert mid == "ai-linux-001" and n["ahead"] == 2 and "排队" in roles.notice_text(n)


def _drawkit():
    spec = importlib.util.spec_from_file_location("drawkit393", ROOT / "skills/cad-drawing/drawkit.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["drawkit393"] = m
    spec.loader.exec_module(m)
    return m


def test_setback_measured_in_pixels_is_accepted():
    dk = _drawkit()
    top = {"side": "top", "values": [8400], "ticks_px": [100, 940]}
    bottom = {"side": "bottom", "values": [7100], "ticks_px": [100, 810]}
    assert dk._chains_same_scale(top, bottom) == 10.0
    bad = {"side": "bottom", "values": [7100], "ticks_px": [100, 940]}     # same pixels, other number: a misread
    assert dk._chains_same_scale(top, bad) is None


def test_dwg_error_names_the_installed_converter(tmp_path, monkeypatch):
    dk = _drawkit()
    fake = tmp_path / "ODAFileConverter"
    fake.write_text("#!/bin/sh\necho 'qt.qpa.plugin: could not load the Qt platform plugin' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("ODA_CONVERTER", str(fake))
    monkeypatch.setattr(dk.shutil, "which", lambda n: None)
    dxf = tmp_path / "户型平面图.dxf"
    dxf.write_text("0\nEOF\n")
    r = dk.export_dwg(dxf)
    assert "installed" in r["error"] and "Qt platform plugin" in r["error"] and "dwgtest" in r["error"]


def test_dwg_goes_through_an_ascii_name(tmp_path, monkeypatch):
    dk = _drawkit()
    fake = tmp_path / "ODAFileConverter"     # writes <name>.dwg for each ASCII .dxf, like ODA; ignores others
    fake.write_text('#!/bin/sh\nfor f in "$1"/*.dxf; do b=$(basename "$f" .dxf); case "$b" in *[!a-z_]*) ;; '
                    '*) echo DWG > "$2/$b.dwg";; esac; done\n')
    fake.chmod(0o755)
    monkeypatch.setenv("ODA_CONVERTER", str(fake))
    monkeypatch.setattr(dk.shutil, "which", lambda n: None)
    monkeypatch.setattr(dk, "dwg_roundtrip", lambda a, b, e=None: {"summary": "ok"})
    dxf = tmp_path / "户型平面图.dxf"
    dxf.write_text("0\nEOF\n")
    r = dk.export_dwg(dxf)
    assert r.get("dwg", "").endswith("户型平面图.dwg") and (tmp_path / "户型平面图.dwg").exists()
