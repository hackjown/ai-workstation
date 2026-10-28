"""3.8: Bark / ntfy / webhook pushes deep-link into Aiw (aiw://… for the iOS app, <public url>/open/… for the web)."""
import asyncio
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import notify as N
from aiw_api.config import Settings
from aiw_api.main import build_app
from aiw_api.store import Store


def test_deep_paths_per_kind():
    assert N.deep_path("done", "task-1") == "task/task-1"
    assert N.deep_path("failed", "task-1") == "task/task-1"
    assert N.deep_path("confirm", "task-1") == "task/task-1"
    assert N.deep_path("input", "task-1") == "task/task-1"
    assert N.deep_path("watch", watch_id="w1") == "monitor/w1"
    assert N.deep_path("watch_error", watch_id="w1") == "monitor/w1"
    assert N.deep_path("xhs_login") == "chat"
    assert N.deep_path("test", message_id="m 1") == "chat?m=m%201"
    assert N.deep_path("done") == "chat"            # no task id: the chat


def test_bark_request_app_and_web(monkeypatch):
    monkeypatch.delenv("AIW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("AIW_PUSH_LINK", raising=False)
    u, p = N.bark_request("https://api.day.app/KEY/", "任务完成", "好了", "done", "task/t1", {})
    assert u == "https://api.day.app/KEY/%E4%BB%BB%E5%8A%A1%E5%AE%8C%E6%88%90/%E5%A5%BD%E4%BA%86"
    assert p == {"url": "aiw://task/t1", "group": "任务"}          # no public url: no icon, app link
    _, p = N.bark_request("https://api.day.app/KEY", "t", "b", "confirm", "task/t1", {"public_url": "https://aiw.example.com/"})
    assert p["url"] == "aiw://task/t1" and p["icon"] == "https://aiw.example.com/icon-180.png"
    assert p["group"] == "需要你" and p["level"] == "timeSensitive"
    _, p = N.bark_request("https://api.day.app/KEY", "t", "b", "watch", "monitor/w1",
                          {"public_url": "https://aiw.example.com", "link_mode": "web"})
    assert p["url"] == "https://aiw.example.com/open/monitor/w1" and p["group"] == "价格监控"
    # web mode without an address falls back to the app link; the env var works too
    assert N.link_settings({"link_mode": "web"}) == ("app", "")
    monkeypatch.setenv("AIW_PUBLIC_URL", "http://192.168.50.102:18000")
    monkeypatch.setenv("AIW_PUSH_LINK", "web")
    _, p = N.bark_request("https://api.day.app/KEY", "t", "b", "failed", "task/t9", {})
    assert p["url"] == "http://192.168.50.102:18000/open/task/t9"


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request):
        self.calls.append(request)
        return httpx.Response(200, json={"code": 200})


@pytest.fixture
def mock_http(monkeypatch):
    rec = Rec()
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(rec)
        return real(*a, **kw)
    monkeypatch.setattr(N.httpx, "AsyncClient", factory)
    return rec


async def test_every_push_carries_the_link(tmp_path, mock_http, monkeypatch):
    monkeypatch.delenv("AIW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("AIW_PUSH_LINK", raising=False)
    st = Store(str(tmp_path / "db.sqlite"))
    st.kv_set("notify", {"bark_url": "https://api.day.app/KEY", "ntfy_url": "https://ntfy.sh/aiw",
                         "webhook_url": "https://hook.example.com/x"})
    st.insert("watches", {"id": "w-jd", "name": "京东", "url": "https://item.jd.com/1.html", "mode": "shop", "created_at": 1})
    n = N.Notifier(st)
    r = await n.notify("watch", "京东降价", "¥99", link="https://item.jd.com/1.html")   # price watch: found by its link
    assert r["deep"] == "monitor/w-jd"
    await n.notify("done", "任务完成", "ok", task_id="task-7", thread_id="thr-7")
    await n.notify("input", "需要你接管桌面", "验证码", task_id="task-8", deep="takeover/ai-linux-001?task=task-8")
    await asyncio.sleep(0.2)
    bark = [c for c in mock_http.calls if c.url.host == "api.day.app"]
    assert [c.url.params["url"] for c in bark] == ["aiw://monitor/w-jd", "aiw://task/task-7", "aiw://takeover/ai-linux-001?task=task-8"]
    assert all(c.url.params["group"] for c in bark)
    ntfy = [c for c in mock_http.calls if c.url.host == "ntfy.sh"]
    assert ntfy[0].headers["Click"] == "aiw://monitor/w-jd"
    hooks = [c for c in mock_http.calls if c.url.host == "hook.example.com"]
    import json
    h = json.loads(hooks[1].content)
    assert h["deep"] == "task/task-7" and h["app_url"] == "aiw://task/task-7" and h["web_url"] is None


async def test_settings_open_route_and_takeover_link(tmp_path):
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    app = build_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer t"}) as c:
        r = await c.put("/api/settings/notify", json={"bark_url": "https://api.day.app/K", "public_url": "https://aiw.example.com",
                                                      "link_mode": "web"})
        assert r.json()["public_url"] == "https://aiw.example.com" and r.json()["link_mode"] == "web"
        assert (await c.put("/api/settings/notify", json={"link_mode": "sms"})).status_code == 400
        assert (await c.put("/api/settings/notify", json={"public_url": "aiw.example.com"})).status_code == 400
        r = await c.get("/open/task/task-1", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/?open=task%2Ftask-1"
        r = await c.get("/open/takeover/ai-linux-001?task=task-2", follow_redirects=False)
        assert r.headers["location"] == "/?open=takeover%2Fai-linux-001%3Ftask%3Dtask-2"
    src = (N.__file__.replace("notify.py", "sessions.py"))
    assert 'deep=f"takeover/{task.machine_id}?task={task.id}"' in open(src, encoding="utf-8").read()


def test_ios_and_web_route_the_links():
    import pathlib
    import plistlib
    root = pathlib.Path(__file__).resolve().parents[1]
    p = plistlib.loads((root / "apps/ios/Info.plist").read_bytes())
    assert any("aiw" in t.get("CFBundleURLSchemes", []) for t in p["CFBundleURLTypes"])
    src = root / "apps/ios/Sources"
    dl = (src / "DeepLink.swift").read_text(encoding="utf-8")
    for host in ('case "task"', 'case "takeover"', 'case "monitor"', 'case "chat"'):
        assert host in dl
    app = (src / "AiwApp.swift").read_text(encoding="utf-8")
    assert "DeepLinks.shared.handle(u)" in app and "ImportCenter.shared.handle(u)" in app and "didReceive" in app
    assert 'userInfo = ["deep"' in app
    root_view = (src / "ThreadsView.swift").read_text(encoding="utf-8")
    assert "onChange(of: links.pending)" in root_view and "openDeep()" in root_view and "/api/tasks/" in root_view
    assert "didFinishLaunchingWithOptions" in (src / "DesktopView.swift").read_text(encoding="utf-8")   # cold launch taps
    assert "aiwShowChat" in (src / "HomeChat.swift").read_text(encoding="utf-8")
    assert "focusWatch" in (src / "HomeSheets.swift").read_text(encoding="utf-8")
    web = root / "apps/web"
    assert '<script src="deeplink.js"></script>' in (web / "index.html").read_text(encoding="utf-8")
    js = (web / "deeplink.js").read_text(encoding="utf-8")
    for k in ('"task"', '"takeover"', '"monitor"', 'go("home")', "searchParams" if False else "URLSearchParams(location.search)"):
        assert k in js
    ui = (web / "ui.js").read_text(encoding="utf-8")
    assert 'name="link_mode"' in ui and 'name="public_url"' in ui
