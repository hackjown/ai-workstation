"""The page the agent works on is brought to the front of the screen (BrowserOS + plain Chrome).

Bug (2026-10-28, 云电脑 2): bos_tabs opened github.com/liangdabiao/whiteboard-teacher-video in a background tab of
the task's (collapsed) tab group, and the BrowserOS window kept showing its "BrowserOS neo" home tab, so the user
watching the desktop never saw the page. Checked live: BrowserOS answers OK to /json/activate, Target.activateTarget
and Page.bringToFront for agent tabs and changes nothing; Ctrl+PageDown in the window does select the tab.
"""
import asyncio
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from vm_agent.browser import browseros as vb
from vm_agent.browser import chrome_cdp as cc

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- which calls show a page
def test_front_page_picks_the_page_a_call_worked_on():
    ok = {"ok": True, "is_error": False, "text": "opened page 4\n\n[Page 4 snapshot]\n- link"}
    assert vb.front_page("tabs", {"action": "new", "url": "https://x"}, ok) == 4
    assert vb.front_page("tabs", {"action": "list"}, {"ok": True, "text": "[1] chrome://newtab/"}) is None
    assert vb.front_page("tabs", {"action": "close", "page": 4}, {"ok": True, "text": "closed page 4"}) is None
    for tool in ("navigate", "act", "read", "snapshot", "grep", "diff", "wait", "upload", "screenshot"):
        assert vb.front_page(tool, {"page": 2}, {"ok": True, "text": ""}) == 2, tool
    assert vb.front_page("read", {"page": 2}, {"ok": False, "is_error": True, "text": "no page"}) is None
    assert vb.front_page("read", {}, {"ok": True, "text": ""}) is None
    assert vb.front_page("name_session", {"page": 2}, {"ok": True, "text": ""}) is None


def test_title_matches_window_names():
    assert cc.title_matches("GitHub - liangdabiao/whiteboard - BrowserOS neo", "GitHub - liangdabiao/whiteboard")
    assert cc.title_matches("Example Domain - Google Chrome", "Example Domain")
    assert not cc.title_matches("BrowserOS neo - BrowserOS neo", "Example Domain")
    assert not cc.title_matches("", "Example Domain") and not cc.title_matches("x", "")


# ----------------------------------------------------------------------------- chrome_cdp.front against fakes
class DevTools(BaseHTTPRequestHandler):
    """/json/list of three tabs; /json/activate selects the tab only when honour_activate (plain Chrome)."""
    tabs = [("A1", "BrowserOS neo", "chrome://newtab/"), ("B2", "Other page", "https://o.example/"),
            ("C3", "Example Domain", "https://example.com/")]
    honour_activate = False
    state: Path = Path("/nonexistent")
    activated: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/json/list":
            body = [{"id": i, "type": "page", "title": t, "url": u, "webSocketDebuggerUrl": f"ws://x/{i}"}
                    for i, t, u in self.tabs]
            body.insert(1, {"id": "W", "type": "service_worker", "title": "sw", "url": "chrome-extension://x"})
            data = json.dumps(body).encode()
        elif self.path.startswith("/json/activate/"):
            tid = self.path.rsplit("/", 1)[-1]
            DevTools.activated.append(tid)
            if self.honour_activate:
                idx = [i for i, _, _ in self.tabs].index(tid)
                self.state.write_text(str(idx))
            data = b"Target activated"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)


FAKE_XDOTOOL = r'''#!/usr/bin/env python3
import os, sys
state = os.environ["FAKE_X_STATE"]
titles = os.environ["FAKE_X_TITLES"].split("|")
log = open(os.environ["FAKE_X_LOG"], "a")
log.write(" ".join(sys.argv[1:]) + "\n")
idx = int(open(state).read() or 0)
a = sys.argv[1:]
if a[:1] == ["getactivewindow"]:
    print(titles[idx] + " - BrowserOS neo")
elif a[:1] == ["search"]:
    if "--class" in a or titles[idx][:20] in " ".join(a):
        print("111")
elif a[:1] == ["key"] and a[-1] == "ctrl+Next":
    open(state, "w").write(str((idx + 1) % len(titles)))
'''


@pytest.fixture
def screen(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), DevTools)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    x = bindir / "xdotool"
    x.write_text(FAKE_XDOTOOL.replace("/usr/bin/env python3", sys.executable))
    x.chmod(x.stat().st_mode | stat.S_IEXEC)
    st, log = tmp_path / "active", tmp_path / "xlog"
    st.write_text("0")   # the BrowserOS neo home tab is the one on screen
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_X_STATE", str(st))
    monkeypatch.setenv("FAKE_X_LOG", str(log))
    monkeypatch.setenv("FAKE_X_TITLES", "|".join(t for _, t, _ in DevTools.tabs))
    monkeypatch.setenv("DISPLAY", ":9")
    DevTools.state, DevTools.activated, DevTools.honour_activate = st, [], False
    yield srv.server_address[1], st, log
    srv.shutdown()


def test_front_selects_a_browseros_agent_tab_by_keyboard(screen):
    port, st, log = screen
    r = cc.front(port, "C3", pause=0)
    assert r["ok"] and r["verified"] and r["how"] == "keys" and r["title"] == "Example Domain"
    assert st.read_text() == "2"                       # the Example tab is the one on screen now
    assert DevTools.activated == ["C3"]                # DevTools activation is still tried first
    assert "key --clearmodifiers ctrl+Next" in log.read_text()


def test_front_plain_chrome_needs_no_keys_and_already_in_front_is_a_noop(screen):
    port, st, log = screen
    DevTools.honour_activate = True
    r = cc.front(port, "", url="https://example.com", pause=0)
    assert r["verified"] and r["how"] == "window" and st.read_text() == "2"
    assert "ctrl+Next" not in log.read_text()
    log.write_text("")
    r = cc.front(port, "C3", pause=0)
    assert r["verified"] and r["how"] == "already" and DevTools.activated == ["C3"]   # no second activation
    assert "ctrl+Next" not in log.read_text() and "windowactivate" not in log.read_text()


def test_front_without_keys_never_moves_the_users_tabs(screen):
    port, st, log = screen
    r = cc.front(port, "C3", keys=False, pause=0)
    assert r["ok"] and not r["verified"] and st.read_text() == "0"
    assert "ctrl+Next" not in log.read_text()


def test_front_unknown_page_and_cli(screen):
    port, st, _ = screen
    assert not cc.front(port, "nope")["ok"]
    import subprocess
    out = subprocess.run([sys.executable, str(ROOT / "vm-agent/vm_agent/browser/chrome_cdp.py"), "front", "--port",
                          str(port), "--id", "B2"], capture_output=True, text=True, env=os.environ, timeout=30)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert out.returncode == 0 and r["verified"] and st.read_text() == "1"


# ----------------------------------------------------------------------------- vm-agent: every bos call shows its page
class FakeBOS(vb.BrowserOS):
    def __init__(self):
        super().__init__({"DISPLAY": ":9"})
        self.calls, self.active = [], 1

    async def _call(self, key, tool, args, title, category):
        self.calls.append((tool, dict(args)))
        if tool == "tabs" and args.get("action") == "new":
            return {"ok": True, "is_error": False, "text": "opened page 3\n\n[Page 3 snapshot]\n- link \"x\" [ref=e1]"}
        if tool == "tabs" and args.get("action") == "list":
            return {"ok": True, "is_error": False, "text": "Your tabs:\n[3] https://github.com/liangdabiao/x (GitHub) "
                    "[group 254419AD7842E9761F407C50948A6B00]\n\nUser's tabs:\n[1] chrome://newtab/ (BrowserOS neo)"}
        if tool == "tabs" and args.get("action") == "active":
            return {"ok": True, "is_error": False, "text": f"Active page: [{self.active}] x"}
        return {"ok": True, "is_error": False, "text": f"{tool} done"}


@pytest.fixture
def fake_bos(monkeypatch):
    b = FakeBOS()
    monkeypatch.setattr(vb, "ensure_running", None, raising=False)

    async def up(*_a, **_k):
        return True
    monkeypatch.setattr(b, "ensure_running", up)
    fronts = []

    def fake_front(port, tid, url, env=None, **_k):
        fronts.append((port, tid, url))
        b.active = 3
        return {"ok": True, "verified": True, "how": "keys", "window": "GitHub - BrowserOS neo"}
    monkeypatch.setattr(cc, "front", fake_front)

    async def target(key, page):
        lst = (await b._call(key, "tabs", {"action": "list"}, None, None))["text"]
        assert "[group 254419AD7842E9761F407C50948A6B00]" in lst
        return {"id": "T3", "url": "https://github.com/liangdabiao/x", "title": "GitHub", "_cdp": 9110,
                "_group": "254419AD7842E9761F407C50948A6B00"}, ""
    monkeypatch.setattr(b, "_target", target)
    window = {"name": "GitHub - BrowserOS neo"}

    async def sh(cmd):
        return window["name"] if "getwindowname" in cmd else ""
    monkeypatch.setattr(b, "_sh", sh)
    return b, fronts, window


async def test_new_tab_is_brought_to_front_and_its_group_expanded(fake_bos):
    b, fronts, _ = fake_bos
    r = await b.call("task-1", "tabs", {"action": "new", "url": "https://github.com/liangdabiao/x"})
    assert r["ok"] and r["text"].startswith("opened page 3")
    assert r["front"] == {"verified": True, "how": "keys", "window": "GitHub - BrowserOS neo", "page": 3}
    assert fronts == [(9110, "T3", "https://github.com/liangdabiao/x")]
    assert ("tab_groups", {"action": "update", "groupId": "254419AD7842E9761F407C50948A6B00",
                           "collapsed": False}) in b.calls


async def test_reads_and_acts_show_their_page_but_not_twice_and_lookups_never(fake_bos):
    b, fronts, window = fake_bos
    await b.call("task-1", "read", {"page": 3, "format": "markdown"})
    await b.call("task-1", "grep", {"page": 3, "pattern": "Install"})          # same page, same window: skipped
    assert len(fronts) == 1
    window["name"] = "BrowserOS neo - BrowserOS neo"                            # the user (or BrowserOS) switched away
    r = await b.call("task-1", "act", {"page": 3, "kind": "click", "ref": "e1"})
    assert len(fronts) == 2 and r["front"]["verified"]
    await b.call("task-1", "grep", {"page": 3, "pattern": "x"}, front=False)    # a gate's lookup
    await b.call("task-1", "tabs", {"action": "list"})
    assert len(fronts) == 2


async def test_unverified_when_browseros_still_shows_another_page(fake_bos, monkeypatch):
    b, fronts, _ = fake_bos
    monkeypatch.setattr(cc, "front", lambda *a, **k: {"ok": True, "verified": True, "how": "keys", "window": "x"})
    b.active = 1   # BrowserOS says its home tab is still the selected one
    r = await b.call("task-1", "front", {"page": 3})
    assert r["ok"] and not r["verified"] and "could not be brought to the front" in r["text"]


async def test_autofront_can_be_switched_off(fake_bos):
    b, fronts, _ = fake_bos
    b.env["VM_AGENT_BOS_AUTOFRONT"] = "0"
    r = await b.call("task-1", "read", {"page": 3})
    assert "front" not in r and fronts == []


async def test_router_passes_front_only_when_given(monkeypatch):
    from vm_agent.server.router import Router
    from aiw_protocols import Command

    class B:
        env = {}
    r = Router(B())
    seen = []

    async def fake(key, tool, args, title=None, category=None, **kw):
        seen.append(kw)
        return {"ok": True, "is_error": False, "text": ""}
    monkeypatch.setattr(r.bos, "call", fake)
    await r.handle(Command(type="browseros.call", payload={"tool": "read", "args": {"page": 1}}))
    await r.handle(Command(type="browseros.call", payload={"tool": "grep", "args": {"page": 1}, "front": False}))
    assert seen == [{}, {"front": False}]


async def test_gate_lookup_asks_vm_agent_not_to_front():
    from tool_router import browseros as bos

    class C:
        def __init__(self):
            self.payloads = []

        async def execute(self, action, payload=None, observe=False, timeout=30):
            self.payloads.append(payload)

            class R:
                success, result = True, {"text": 'button "提交订单" [ref=e5]'}
            return R()
    c = C()
    got = await bos.resolve(c, "task-1", 3, ["e5"])
    assert got["e5"] == "提交订单" and c.payloads[0]["front"] is False


# ----------------------------------------------------------------------------- price watch
def test_background_probe_never_fronts_and_submit_does(monkeypatch, tmp_path):
    src = (ROOT / "services/api/aiw_api/shop_probe.py").read_text()
    probe = src[src.index("def cmd_probe"):src.index("def order_pages")]
    assert "show_on_screen" not in probe and '"background": True' in probe
    submit = src[src.index("def cmd_submit"):src.index("def cmd_close_order")]
    assert "show_on_screen(p[\"id\"])" in submit
    from aiw_api import shop_probe as sp
    monkeypatch.setattr(sp, "CDP_TOOL", str(tmp_path / "missing.py"))
    assert sp.show_on_screen("X")["ok"] is False
