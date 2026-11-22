"""v3.9.73: an engine conversation can run on a cloud computer; 自动 sends CAD / video there and keeps code local."""
import asyncio
from pathlib import Path

from aiw_api import bots as B

ROOT = Path(__file__).resolve().parents[1]


class FakeStore:
    pass


def make(roles=None):
    bt = B.Bots.__new__(B.Bots)
    bt.default_machine = lambda: "ai-linux-001"
    class S: pass
    s = S(); s.cloud = None; s.store = None
    bt.sessions = s
    import aiw_api.roles as R
    R_roles, R_cloud = R.roles_of, None
    return bt, R


def test_pick_machine(monkeypatch):
    bt, R = make()
    monkeypatch.setattr(R, "roles_of", lambda s: {"cloud-1": "design", "cloud-2": "media", "cloud-3": "office"})
    monkeypatch.setattr(R, "is_code", lambda t: "修 bug" in t or "写个脚本" in t)
    assert bt.pick_machine("根据这个ppt做一个讲解视频") == "cloud-2"
    assert bt.pick_machine("用 LibreCAD 画一张 dxf 平面图") == "cloud-1"
    assert bt.pick_machine("帮我修 bug，视频播放器卡住") == "ai-linux-001"
    assert bt.pick_machine("整理一下周报") == "ai-linux-001"
    monkeypatch.setattr(R, "roles_of", lambda s: {})
    assert bt.pick_machine("剪辑视频") == "ai-linux-001"


def test_wiring():
    src = (ROOT / "services/api/aiw_api/bots.py").read_text()
    assert 'AUTO_MACHINE = "auto"' in src and "await self.wake(mid)" in src
    assert "bots.resolve_machine(cid, body.text" in src and '没有这台电脑' in src
    js = (ROOT / "apps/web/bots.js").read_text()
    assert "btMachineOpts()" in js and "machine_id: f.machine" in js and "c.machine_label" in js
    sw = (ROOT / "apps/ios/Sources/EngineView.swift").read_text()
    assert '"machine_id": machine' in sw and 'Text("自动（按任务选）").tag("auto")' in sw
