"""3.8.6: iOS 电脑 › 查看 opens one computer's desktop full screen (noVNC / InstaBox viewer) with the 3.8.4 takeover
layer, without a task. The server's desktop endpoint wakes a sleeping cloud computer when asked (wake=1)."""
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

from test_shopwatch import stack  # noqa: F401

SRC = os.path.join(os.path.dirname(__file__), "..", "apps", "ios", "Sources")


def _r(name):
    return open(os.path.join(SRC, name), encoding="utf-8").read()


def test_machines_sheet_has_a_view_button_before_use():
    h = _r("HomeSheets.swift")
    i_view, i_use = h.index('Button("查看")'), h.index('Button(cur ? "使用中" : "使用")')
    assert i_view < i_use and "cloud || hasDesktop(m)" in h and "desktop_endpoint" in h
    assert "TakeoverView(liveSession: nil, machineId: t.id, wake: true" in h and ".fullScreenCover(item: $viewing)" in h


def test_takeover_view_works_for_one_machine_without_a_task():
    d = _r("DesktopView.swift")
    assert "init(liveSession: String?, machineId: String? = nil, wake: Bool = false" in d
    assert "mid = machineId ?? api.machine" in d and '"&wake=1"' in d
    assert 'Label("关闭", systemImage: "xmark")' in d and "standalone && (landscape || url == nil)" in d
    assert "TakeoverWeb(url: u, server: api.server, mid: mid, token: api.token)" in d   # same /ws/input/<mid> layer


async def test_desktop_endpoint_wakes_a_sleeping_cloud_computer(stack, monkeypatch):  # noqa: F811
    client, _s, app = stack
    cloud = app.state.cloud
    app.state.store.upsert_machine({"id": "cloud-x", "name": "云电脑 1", "provider": "instabox", "provider_vm_id": "m_x",
                                    "agent_endpoint": "ws://127.0.0.1:1", "desktop_endpoint": ""})
    state = {"s": "paused"}
    woke = []

    async def st(mid):
        return {"state": state["s"]}

    async def wake(mid):
        woke.append(mid)
        state["s"] = "running"

    async def url(mid, view_only):
        return f"https://view.instabox.sh/x?vo={int(view_only)}"
    monkeypatch.setattr(cloud, "is_cloud", lambda mid: mid == "cloud-x")
    monkeypatch.setattr(cloud, "state", st)
    monkeypatch.setattr(cloud, "wake", wake)
    monkeypatch.setattr(cloud, "desktop_url", url)
    r = (await client.get("/api/machines/cloud-x/desktop?view_only=false")).json()
    assert r["paused"] is True and not woke                     # a plain look does not wake it
    r = (await client.get("/api/machines/cloud-x/desktop?view_only=false&wake=1")).json()
    assert woke == ["cloud-x"] and r["novnc_url"] == "https://view.instabox.sh/x?vo=0" and r["external"] is True
