"""The 京东 price watch's "NO_CDP: Connection refused" (2026-10-28): finding a DevTools port, reopening the user's own
Chrome with one on the same profile (login kept), launchers that always carry the port, and the probe's own discovery."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from aiw_api import shop_probe as sp
from vm_agent.browser import chrome_cdp as cc


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    old = cc.HOME
    cc.set_home(str(h))
    monkeypatch.setattr(cc, "_SCOPE", [])
    yield h
    cc.set_home(old)


def mkproc(root, pid, argv, env=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()) + b"\0")


def mkprofile(base, cookies=True):
    (base / "Default" / "Network").mkdir(parents=True)
    (base / "Default" / "Cache").mkdir()
    (base / "Default" / "Cache" / "data_0").write_text("x" * 100)
    (base / "Default" / "Sessions").mkdir()
    (base / "Default" / "Sessions" / "Session_1").write_text("tabs")
    if cookies:
        (base / "Default" / "Network" / "Cookies").write_text("jd-login-cookie")
    (base / "SingletonLock").symlink_to("host-999999")


class Fake:
    """Stands in for the real browser: which ports answer, what got closed and launched."""

    def __init__(self, monkeypatch, up=()):
        self.up, self.closed, self.launched = set(up), [], []
        monkeypatch.setattr(cc, "version", lambda port, timeout=1.5: {"Browser": "Chrome/141", "webSocketDebuggerUrl": "ws://x"}
                            if int(port) in self.up else None)
        monkeypatch.setattr(cc, "graceful_close", lambda pid, timeout=20: self.closed.append(pid) or "closed")
        monkeypatch.setattr(cc, "wait_port", lambda port, timeout=40: ({"Browser": "Chrome/141"} if int(port) in self.up else None))

        def launch(args, env):
            self.launched.append((args, env.get("DISPLAY")))
            self.up.add(9222)
            return 1
        monkeypatch.setattr(cc, "launch", launch)


def test_procs_reads_main_browsers_only(tmp_path, home):
    root = tmp_path / "proc"
    mkproc(root, 100, ["/usr/lib/chromium/chromium", "--password-store=basic", "https://jd.com"])
    mkproc(root, 101, ["/usr/lib/chromium/chromium", "--type=renderer"])
    mkproc(root, 102, ["/opt/google/chrome/chrome", "--remote-debugging-port=9333", f"--user-data-dir={home}/.config/aiw-chrome"])
    mkproc(root, 103, ["/usr/lib/browserclaw/browserclaw", "--remote-debugging-port=9110"])
    mkproc(root, 104, ["/usr/bin/python3", "x.py"])
    ps = {p["pid"]: p for p in cc.procs(str(root))}
    assert set(ps) == {100, 102, 103}
    assert ps[100]["kind"] == "chromium" and ps[100]["default_dir"] and ps[100]["port"] is None
    assert ps[100]["dir"] == str(home / ".config" / "chromium")
    assert ps[102]["port"] == 9333 and not ps[102]["default_dir"] and ps[102]["kind"] == "chrome"
    assert ps[103]["kind"] == "browseros"


def test_ensure_found_does_not_touch_chrome(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    mkproc(root, 100, ["/usr/lib/chromium/chromium", "--remote-debugging-port=9222"])
    f = Fake(monkeypatch, up={9222})
    r = cc.ensure(9222, root=str(root))
    assert r["ok"] and r["port"] == 9222 and r["how"] == "found" and not f.closed and not f.launched


def test_ensure_relaunches_default_profile_keeping_login(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    mkprofile(home / ".config" / "chromium")
    mkproc(root, 100, ["/usr/lib/chromium/chromium", "--password-store=basic", "--enable-gpu-rasterization",
                       "--profile-directory=Default", "https://item.jd.com/1.html"], {"DISPLAY": ":1"})
    mkproc(root, 101, ["/usr/lib/chromium/chromium", "--type=renderer"])
    mkproc(root, 102, ["/usr/lib/chromium/chromium", f"--user-data-dir={home}/.config/aiw/photopea/profile",
                       "--app=http://127.0.0.1:8791"])                 # AIW's Photopea helper: never touched
    mkproc(root, 103, ["/usr/lib/browserclaw/browserclaw", "--remote-debugging-port=9110"])
    f = Fake(monkeypatch, up={9110})                                     # only BrowserOS answers: still reopen Chrome
    r = cc.ensure(9222, root=str(root))
    assert r["ok"] and r["how"] == "relaunched" and r["port"] == 9222, r
    assert f.closed == [100]
    assert f.launched == [(["--restore-last-session"], ":1")]            # same display, tabs restored
    new = home / ".config" / "aiw-chrome"
    assert r["profile"] == str(new) and r["migrated"]["from"].endswith("chromium")
    assert (new / "Default" / "Network" / "Cookies").read_text() == "jd-login-cookie"    # the JD login came along
    assert (new / "Default" / "Sessions" / "Session_1").exists()
    assert not (new / "Default" / "Cache").exists() and not os.path.lexists(new / "SingletonLock")
    conf = (home / ".config" / "aiw" / "chrome.conf").read_text()
    assert f"AIW_CHROME_DIR='{new}'" in conf and "AIW_CDP_PORT=9222" in conf
    flags = (home / ".config" / "aiw" / "chrome-flags").read_text().split()
    assert "--password-store=basic" in flags and "--profile-directory=Default" in flags   # same cookie encryption
    assert not any("jd.com" in x for x in flags)
    assert os.access(cc.WRAPPER, os.X_OK)


def test_ensure_relaunch_keeps_a_non_default_profile_in_place(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    mine = home / "chrome-profile"
    mkprofile(mine)
    mkproc(root, 100, ["/opt/google/chrome/chrome", f"--user-data-dir={mine}"], {"DISPLAY": ":0"})
    f = Fake(monkeypatch)
    r = cc.ensure(9222, root=str(root))
    assert r["ok"] and r["how"] == "relaunched" and r["migrated"] is None and r["profile"] == str(mine)
    assert f.launched[0][1] == ":0" and not (home / ".config" / "aiw-chrome").exists()


def test_ensure_existing_aiw_profile_is_backed_up(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    mkprofile(home / ".config" / "google-chrome")
    old = home / ".config" / "aiw-chrome"
    old.mkdir(parents=True)
    (old / "marker").write_text("older aiw profile")
    mkproc(root, 100, ["/opt/google/chrome/chrome"], {"DISPLAY": ":1"})
    Fake(monkeypatch)
    r = cc.ensure(9222, root=str(root))
    assert r["ok"] and r["migrated"]["backup"] and open(os.path.join(r["migrated"]["backup"], "marker")).read() == "older aiw profile"


def test_ensure_browseros_only_and_failures(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    f = Fake(monkeypatch, up={9110})
    r = cc.ensure(9222, root=str(root))
    assert r["ok"] and r["port"] == 9110 and r["how"] == "found"                # no Chrome of the user's: BrowserOS
    f.up.clear()
    r = cc.ensure(9222, start=False, root=str(root))
    assert not r["ok"] and r["reason"] == "not_running"
    monkeypatch.setattr(cc, "browser_bin", lambda *a, **k: None)
    r = cc.ensure(9222, root=str(root))
    assert not r["ok"] and r["reason"] == "no_browser_installed"


def test_ensure_relaunch_port_never_opens(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    mkproc(root, 100, ["/usr/lib/chromium/chromium", f"--user-data-dir={home}/p"], {"DISPLAY": ":1"})
    f = Fake(monkeypatch)
    monkeypatch.setattr(cc, "launch", lambda args, env: f.launched.append(args) or 1)   # nothing comes up
    r = cc.ensure(9222, root=str(root), wait=1)
    assert not r["ok"] and r["reason"] == "port_not_opened" and "9222" in r["error"]


def test_install_launchers_rewrites_every_entry(tmp_path, home):
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "chromium.desktop").write_text("[Desktop Entry]\nName=Chromium\nExec=/usr/bin/chromium %U\nTryExec=chromium\n\n"
                                           "[Desktop Action new-private-window]\nExec=/usr/bin/chromium --incognito\n")
    panel = home / ".config" / "xfce4" / "panel" / "launcher-5"
    panel.mkdir(parents=True)
    (panel / "1700.desktop").write_text("[Desktop Entry]\nExec=env LANG=zh_CN.UTF-8 chromium --user-data-dir=/x %U\n")
    auto = home / ".config" / "autostart"
    auto.mkdir(parents=True)
    (auto / "browseros.desktop").write_text("[Desktop Entry]\nExec=browserclaw --no-first-run\n")
    r = cc.install_launchers(9222, apps_dirs=(str(apps),))
    assert r["desktop_entries"] == ["chromium.desktop"]
    menu = (home / ".local" / "share" / "applications" / "chromium.desktop").read_text()
    assert f"Exec={cc.WRAPPER} %U" in menu and f"Exec={cc.WRAPPER} --incognito" in menu and f"TryExec={cc.WRAPPER}" in menu
    assert (panel / "1700.desktop").read_text().strip() == f"[Desktop Entry]\nExec=env LANG=zh_CN.UTF-8 {cc.WRAPPER} %U"
    assert (auto / "browseros.desktop").read_text() == "[Desktop Entry]\nExec=browserclaw --no-first-run\n"
    assert f"Exec={cc.WRAPPER} --restore-last-session" in (auto / "aiw-chrome.desktop").read_text()
    assert "WebBrowser=aiw-chrome" in (home / ".config" / "xfce4" / "helpers.rc").read_text()
    again = cc.install_launchers(9222, apps_dirs=(str(apps),))
    assert again["changed"] == []                                              # idempotent


def test_wrapper_always_passes_the_port(tmp_path, home):
    cc.install_launchers(9333, apps_dirs=())
    fake = tmp_path / "fakechrome"
    out = tmp_path / "args"
    fake.write_text(f"#!/bin/sh\nfor a in \"$@\"; do echo \"$a\"; done > {out}\n")
    fake.chmod(0o755)
    cc.write_conf(str(fake), str(home / "prof"), 9333, ["--password-store=gnome-libsecret", "--profile-directory=Profile 1"])
    subprocess.run([cc.WRAPPER, "https://item.jd.com/1.html"], env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, check=True)
    args = out.read_text().splitlines()
    assert "--remote-debugging-port=9333" in args and "--remote-allow-origins=*" in args
    assert "--remote-debugging-address=127.0.0.1" in args and f"--user-data-dir={home}/prof" in args
    assert "--profile-directory=Profile 1" in args and "--password-store=gnome-libsecret" in args
    assert "https://item.jd.com/1.html" in args and "--password-store=basic" not in args


# ----------------------------------------------------------------------------- the probe finds a port by itself
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"Browser": "Chrome/141", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def devtools():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_probe_discovers_a_running_chrome_port(tmp_path, monkeypatch, devtools):
    root = tmp_path / "proc"
    mkproc(root, 100, ["/opt/google/chrome/chrome", f"--remote-debugging-port={devtools}"])
    mkproc(root, 101, ["/opt/google/chrome/chrome", "--type=renderer", "--remote-debugging-port=1"])
    monkeypatch.setattr(sp, "CDP", "http://127.0.0.1:1")
    cands = sp.cdp_candidates(str(root))
    assert cands[:2] == ["http://127.0.0.1:1", "http://127.0.0.1:9222"] and f"http://127.0.0.1:{devtools}" in cands
    assert cands.count("http://127.0.0.1:1") == 1
    monkeypatch.setattr(sp, "cdp_candidates", lambda root="/proc": ["http://127.0.0.1:1", f"http://127.0.0.1:{devtools}"])
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")              # a desktop proxy must not swallow localhost
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert sp.find_cdp() == "" and sp.CDP == f"http://127.0.0.1:{devtools}"
    assert cc.version(devtools)["Browser"] == "Chrome/141"
    monkeypatch.setattr(sp, "cdp_candidates", lambda root="/proc": ["http://127.0.0.1:1"])
    assert "1:" in sp.find_cdp()


def test_discover_real_http(tmp_path, devtools):
    root = tmp_path / "proc"
    mkproc(root, 100, ["/usr/lib/chromium/chromium", f"--remote-debugging-port={devtools}"])
    eps = cc.discover((), str(root))
    assert any(e["port"] == devtools and e["kind"] == "chromium" and e["pid"] == 100 for e in eps)


def test_nothing_running_starts_the_newest_logged_in_profile(tmp_path, home, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    mkprofile(home / ".config" / "chromium")                    # the user logged in here, then closed Chromium
    monkeypatch.setattr(cc, "browser_bin", lambda *a, **k: "/usr/bin/chromium")
    f = Fake(monkeypatch)
    r = cc.ensure(9222, root=str(root))
    assert r["ok"] and r["how"] == "launched" and r["migrated"]["from"].endswith("chromium")
    assert (home / ".config" / "aiw-chrome" / "Default" / "Network" / "Cookies").exists()
    assert (home / ".config" / "aiw" / "chrome-flags").read_text() == ""   # Chrome picks its own password store
    assert f.launched == [(["--restore-last-session"], f.launched[0][1])]
    r = cc.ensure(9222, root=str(root))
    assert r["how"] == "found"
