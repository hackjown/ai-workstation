#!/usr/bin/env python3
"""Keep a DevTools (CDP) port open on the AIW computer's own logged-in Chrome / Chromium. Stdlib only.

Why (2026-10-28): the 京东 price watch failed with "NO_CDP: Connection refused". The user had logged in to JD in the
Chromium they opened from the XFCE menu, which runs on its default profile WITHOUT --remote-debugging-port, so nothing
listened on 9222; and launching "chrome" again with the port only handed the request to that running instance
(Chrome's single-instance lock), so the port never opened.

What this does
  discover   every answering DevTools endpoint: the configured port, 9222, BrowserOS 9110, and any running
             chrome/chromium started with --remote-debugging-port (read from /proc) or its DevToolsActivePort file.
  ensure     discover; if none, and the user's normal Chrome is running without a port: close it gracefully
             (SIGTERM = a clean shutdown that saves the session), wait for its profile lock to clear, and relaunch it
             on the same DISPLAY with --remote-debugging-port=<port> --remote-allow-origins=* (127.0.0.1 only) and
             --restore-last-session, so its tabs and logins (cookies) are kept. Chrome 136+ ignores the port on its
             default profile directory, so a default profile is first copied (caches left out) to ~/.config/aiw-chrome
             and Chrome runs from there; the old copy of aiw-chrome, if any, is kept as aiw-chrome.bak-<time>.
             Nothing running: start it (with the newest logged-in profile).
  install-launchers   ~/.local/bin/aiw-chrome (the one way AIW starts Chrome: always with the port) and per-user
             overrides of the desktop menu entries, panel launchers, desktop icons, the XFCE web-browser helper and
             an autostart entry, so a Chrome the user opens by hand has the port too and this cannot break again.
  open-url / close-tab   the last-resort path without CDP: open a link as a new tab of the visible browser and close
             it again (only when the active window's title says it is that page).
  front      bring one DevTools page to the front of the screen: its tab selected in its window and that window on
             top, checked by the active window's title. BrowserOS neo ignores every DevTools activation for the tabs
             its agents own (/json/activate, Target.activateTarget and Page.bringToFront all answer OK and change
             nothing, checked live 2026-10-28), so when the title does not match, the tab is selected the way a person
             would: Ctrl+PageDown in the browser window until its title is the page's.

Runs from the vm-agent (computer.open_app "chrome") and is also shipped by the API to ~/.config/aiw/chrome_cdp.py
and run over the shell, so a computer whose vm-agent is not updated yet still gets it. Every command prints one JSON.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

VERSION = "2"
HOME = CFG = AIW_DIR = CONF = FLAGS = LOCK = WRAPPER = ""


def set_home(home: str) -> None:
    """All paths hang off $HOME (tests point it at a temp dir)."""
    global HOME, CFG, AIW_DIR, CONF, FLAGS, LOCK, WRAPPER
    HOME = home
    CFG = os.path.join(HOME, ".config")
    AIW_DIR = os.path.join(CFG, "aiw-chrome")
    CONF = os.path.join(CFG, "aiw", "chrome.conf")        # sh: AIW_CHROME_BIN / AIW_CHROME_DIR / AIW_CDP_PORT
    FLAGS = os.path.join(CFG, "aiw", "chrome-flags")      # one extra flag per line (kept from the user's Chrome)
    LOCK = os.path.join(CFG, "aiw", "chrome-cdp.lock")
    WRAPPER = os.path.join(HOME, ".local", "bin", "aiw-chrome")


set_home(os.path.expanduser("~"))
DEFAULT_PORT = 9222
BOS_PORT = 9110
BIN_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
DESKTOP_IDS = ("google-chrome.desktop", "google-chrome-stable.desktop", "chromium.desktop", "chromium-browser.desktop",
               "org.chromium.Chromium.desktop")
# flags that belong to one launch, or that this tool sets itself
DROP_FLAGS = ("--user-data-dir", "--remote-debugging-port", "--remote-debugging-address", "--remote-debugging-pipe",
              "--remote-allow-origins", "--restore-last-session", "--app", "--new-window", "--incognito",
              "--no-startup-window", "--profile-directory", "--headless", "--flag-switches-begin", "--flag-switches-end")
CACHE_DIRS = {"Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache", "DawnCache",
              "DawnGraphiteCache", "DawnWebGPUCache", "component_crx_cache", "extensions_crx_cache", "Crashpad",
              "Crash Reports", "optimization_guide_model_store", "Safe Browsing", "screen_ai", "OnDeviceHeadSuggestModel"}
SINGLETON = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def say(obj: dict, code: int = 0):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


# ----------------------------------------------------------------------------- processes
def _kind(argv0: str) -> str | None:
    b = os.path.basename(argv0 or "").lower()
    p = (argv0 or "").lower()
    if "browserclaw" in p or "browseros" in p:
        return "browseros"
    if b in ("chrome", "google-chrome", "google-chrome-stable", "google-chrome-beta", "google-chrome-unstable"):
        return "chrome"
    if b in ("chromium", "chromium-browser", "chromium-freeworld"):
        return "chromium"
    return None


def _flag(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return None


def default_dir(kind: str, argv0: str = "") -> str:
    p = (argv0 or "").lower()
    if kind == "chrome":
        for ch in ("beta", "unstable"):
            if ch in p:
                return os.path.join(CFG, f"google-chrome-{ch}")
        return os.path.join(CFG, "google-chrome")
    return os.path.join(CFG, "chromium")


def procs(root: str = "/proc", uid: int | None = None) -> list[dict]:
    """Main (browser) processes of chrome / chromium / BrowserOS owned by this user. Renderers etc. (--type=) are left out."""
    uid = os.getuid() if uid is None else uid
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for pid in names:
        if not pid.isdigit():
            continue
        d = os.path.join(root, pid)
        try:
            if os.stat(d).st_uid != uid:
                continue
            with open(os.path.join(d, "cmdline"), "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        if len(argv) == 1 and " " in argv[0]:      # a process that rewrote its title into one string
            argv = argv[0].split()
        kind = _kind(argv[0])
        if not kind or any(a.startswith("--type=") for a in argv):
            continue
        udd = _flag(argv, "--user-data-dir")
        udd = os.path.abspath(os.path.expanduser(udd)) if udd else None
        port = _flag(argv, "--remote-debugging-port")
        try:
            port = int(port) if port not in (None, "") else None
        except ValueError:
            port = None
        out.append({"pid": int(pid), "argv": argv, "exe": argv[0], "kind": kind, "user_data_dir": udd,
                    "dir": udd or default_dir(kind, argv[0]), "default_dir": udd is None or
                    os.path.abspath(udd) == os.path.abspath(default_dir(kind, argv[0])),
                    "port": port, "headless": any(a.startswith("--headless") for a in argv),
                    "app": any(a.startswith("--app=") for a in argv)})
    return sorted(out, key=lambda p: p["pid"])


def _helper_profile(p: dict) -> bool:
    """AIW's own helper browsers (Photopea bridge profile under ~/.config/aiw/, Playwright's chromium, headless runs)."""
    d = p.get("dir") or ""
    return bool(p.get("headless") or "ms-playwright" in p.get("exe", "") or d.startswith(os.path.join(CFG, "aiw") + os.sep)
                or p.get("app"))


def environ_of(pid: int, root: str = "/proc") -> dict:
    try:
        with open(os.path.join(root, str(pid), "environ"), "rb") as f:
            pairs = [x.split(b"=", 1) for x in f.read().split(b"\0") if b"=" in x]
        return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}
    except OSError:
        return {}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a zombie is gone for our purposes
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[-1][:1] != "Z"
    except OSError:
        return True


# ----------------------------------------------------------------------------- DevTools endpoints
def version(port: int, timeout: float = 1.5) -> dict | None:
    try:
        # never through an http_proxy the desktop user set: DevTools is local
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{int(port)}/json/version", timeout=timeout) as r:  # noqa: S310
            d = json.loads(r.read().decode())
        return d if isinstance(d, dict) and d.get("webSocketDebuggerUrl") else None
    except Exception:  # noqa: BLE001
        return None


def active_port_file(udd: str) -> int | None:
    try:
        with open(os.path.join(udd, "DevToolsActivePort")) as f:
            return int(f.readline().strip())
    except (OSError, ValueError):
        return None


def discover(ports=(), root: str = "/proc", ps: list[dict] | None = None) -> list[dict]:
    """Answering endpoints, best first: the asked ports, 9222, then ports of running browsers, BrowserOS 9110 last."""
    ps = procs(root) if ps is None else ps
    cand: list[tuple[int, str, str]] = []
    for p in ports or ():
        if p:
            cand.append((int(p), "configured", ""))
    cand.append((DEFAULT_PORT, "default", ""))
    for p in ps:
        if p["port"]:
            cand.append((p["port"], "process", p["kind"]))
        fp = active_port_file(p["dir"])
        if fp:
            cand.append((fp, "DevToolsActivePort", p["kind"]))
    cand.append((BOS_PORT, "browseros", "browseros"))
    seen, out = set(), []
    for port, src, kind in cand:
        if port in seen or port <= 0:
            continue
        seen.add(port)
        v = version(port)
        if v:
            owner = next((p for p in ps if p["port"] == port or active_port_file(p["dir"]) == port), None)
            k = kind or (owner or {}).get("kind") or ("browseros" if port == BOS_PORT else "")
            out.append({"port": port, "source": src, "kind": k, "browser": v.get("Browser", ""),
                        "dir": (owner or {}).get("dir"), "pid": (owner or {}).get("pid")})
    return out


# ----------------------------------------------------------------------------- profiles
def cookies_mtime(udd: str) -> float:
    best = 0.0
    try:
        profs = [n for n in os.listdir(udd) if n == "Default" or n.startswith("Profile ")]
    except OSError:
        return 0.0
    for prof in profs:
        for rel in ("Network/Cookies", "Cookies"):
            with contextlib.suppress(OSError):
                best = max(best, os.path.getmtime(os.path.join(udd, prof, rel)))
    return best


def wait_unlock(udd: str, timeout: float = 15) -> bool:
    """True once nobody holds the profile: SingletonLock gone, or left behind by a process that no longer runs."""
    lock = os.path.join(udd, "SingletonLock")
    end = time.time() + timeout
    while time.time() < end:
        if not os.path.lexists(lock):
            return True
        try:
            target = os.readlink(lock)              # "<hostname>-<pid>"
            pid = int(target.rsplit("-", 1)[1])
        except (OSError, ValueError, IndexError):
            pid = 0
        if pid and not alive(pid):
            for n in SINGLETON:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(udd, n))
            return True
        time.sleep(0.5)
    return not os.path.lexists(lock)


def clone_profile(src: str, dst: str | None = None) -> dict:
    """Copy a (closed) profile to dst, caches left out. An existing dst is kept as dst.bak-<time>."""
    dst = dst or AIW_DIR
    backup = None
    if os.path.isdir(dst) and os.listdir(dst):
        backup = f"{dst}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(dst, backup)

    def ignore(_d, names):
        return [n for n in names if n in CACHE_DIRS or n in SINGLETON or n == "DevToolsActivePort"]
    shutil.copytree(src, dst, symlinks=True, ignore=ignore)
    return {"from": src, "to": dst, "backup": backup}


def kept_flags(argv: list[str]) -> list[str]:
    out = []
    for a in argv[1:]:
        if not a.startswith("--"):
            continue                                  # URLs and stray words belong to that one launch
        if any(a == f or a.startswith(f + "=") for f in DROP_FLAGS):
            continue
        out.append(a)
    prof = _flag(argv, "--profile-directory")
    if prof:
        out.append(f"--profile-directory={prof}")
    return out


def browser_bin(kind: str | None = None, exe: str = "") -> str | None:
    """The launcher on PATH (Debian's /usr/bin/chromium wrapper reads /etc/chromium.d), else the binary itself."""
    names = {"chrome": ("google-chrome", "google-chrome-stable"), "chromium": ("chromium", "chromium-browser")}.get(kind or "", BIN_NAMES)
    for n in names:
        b = shutil.which(n)
        if b:
            return b
    if exe and os.path.isabs(exe) and os.access(exe, os.X_OK):
        return exe
    for n in BIN_NAMES:
        b = shutil.which(n)
        if b:
            return b
    return None


def _shq(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def write_conf(bin_: str | None, udd: str, port: int, flags: list[str] | None) -> None:
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF, "w") as f:
        f.write("# written by AIW (vm-agent chrome_cdp.py): how ~/.local/bin/aiw-chrome starts Chrome\n")
        if bin_:
            f.write(f"AIW_CHROME_BIN={_shq(bin_)}\n")
        f.write(f"AIW_CHROME_DIR={_shq(udd)}\nAIW_CDP_PORT={int(port)}\n")
    if flags is not None:
        with open(FLAGS, "w") as f:
            f.write("".join(x + "\n" for x in flags))


# ----------------------------------------------------------------------------- launchers
WRAPPER_SH = r"""#!/bin/sh
# AIW: start Chrome / Chromium ALWAYS with the DevTools port on 127.0.0.1, so the price watch and the browser tools
# can read the logged-in pages. Written by the AIW vm-agent (chrome_cdp.py install-launchers); settings in
# ~/.config/aiw/chrome.conf, extra flags (one per line) in ~/.config/aiw/chrome-flags.
AIW_CHROME_DIR="$HOME/.config/aiw-chrome"
AIW_CDP_PORT=9222
AIW_CHROME_BIN=
[ -f "$HOME/.config/aiw/chrome.conf" ] && . "$HOME/.config/aiw/chrome.conf"
if [ -z "$AIW_CHROME_BIN" ] || ! command -v "$AIW_CHROME_BIN" >/dev/null 2>&1; then
  for b in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$b" >/dev/null 2>&1; then AIW_CHROME_BIN=$b; break; fi
  done
fi
[ -n "$AIW_CHROME_BIN" ] || { echo "aiw-chrome: no google-chrome or chromium installed" >&2; exit 127; }
F="$HOME/.config/aiw/chrome-flags"
if [ -f "$F" ]; then
  while IFS= read -r l; do [ -n "$l" ] && set -- "$@" "$l"; done < "$F"
else
  set -- "$@" --password-store=basic
fi
exec "$AIW_CHROME_BIN" --remote-debugging-port="$AIW_CDP_PORT" --remote-debugging-address=127.0.0.1 \
  '--remote-allow-origins=*' --user-data-dir="$AIW_CHROME_DIR" --no-first-run --no-default-browser-check "$@"
"""


def _rewrite_exec(line: str, wrapper: str) -> str:
    """'Exec=/usr/bin/chromium --incognito %U' -> 'Exec=<wrapper> --incognito %U' (the port flags come from the wrapper)."""
    key, _, cmd = line.partition("=")
    parts = cmd.strip().split()
    if not parts:
        return line
    i = 0
    if parts[0] == "env":                           # Exec=env VAR=x /usr/bin/chromium ...
        i = 1
        while i < len(parts) and "=" in parts[i]:
            i += 1
    if i >= len(parts) or not _kind(parts[i]) or _kind(parts[i]) == "browseros":
        if i < len(parts) and os.path.basename(parts[i]) == "aiw-chrome":
            parts[i] = wrapper
            return f"{key}={' '.join(parts)}"
        return line
    rest = [a for a in parts[i + 1:] if not any(a == f or a.startswith(f + "=") for f in
                                                ("--user-data-dir", "--remote-debugging-port", "--remote-allow-origins",
                                                 "--remote-debugging-address"))]
    return f"{key}={' '.join(parts[:i] + [wrapper] + rest)}"


def rewrite_desktop(text: str, wrapper: str | None = None) -> str:
    wrapper = wrapper or WRAPPER
    out = []
    for ln in text.splitlines():
        if ln.startswith("Exec=") or ln.startswith("TryExec="):
            if ln.startswith("TryExec="):
                if _kind(ln.split("=", 1)[1].strip()) in ("chrome", "chromium"):
                    ln = f"TryExec={wrapper}"
            else:
                ln = _rewrite_exec(ln, wrapper)
        out.append(ln)
    return "\n".join(out) + "\n"


def _write(path: str, text: str, mode: int | None = None) -> bool:
    old = None
    with contextlib.suppress(OSError):
        with open(path) as f:
            old = f.read()
    if old == text:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    if mode:
        os.chmod(path, mode)
    return True


def install_launchers(port: int = DEFAULT_PORT, apps_dirs=("/usr/share/applications", "/usr/local/share/applications"),
                      autostart: bool = True) -> dict:
    """The wrapper plus per-user launchers that all go through it. Idempotent; needs no sudo."""
    changed = []
    if _write(WRAPPER, WRAPPER_SH, 0o755):
        changed.append(WRAPPER)
    if not os.path.exists(CONF):
        write_conf(None, AIW_DIR, port, None)
        changed.append(CONF)
    else:   # keep the chosen browser / profile, but make the port the asked one
        with open(CONF) as f:
            txt = f.read()
        if f"AIW_CDP_PORT={int(port)}\n" not in txt:
            lines = [ln for ln in txt.splitlines() if not ln.startswith("AIW_CDP_PORT=")] + [f"AIW_CDP_PORT={int(port)}"]
            _write(CONF, "\n".join(lines) + "\n")
            changed.append(CONF)
    user_apps = os.path.join(HOME, ".local", "share", "applications")
    found = []
    for d in apps_dirs:
        for name in DESKTOP_IDS:
            src = os.path.join(d, name)
            if os.path.isfile(src) and name not in found:
                found.append(name)
                with open(src, errors="replace") as f:
                    text = rewrite_desktop(f.read())
                if _write(os.path.join(user_apps, name), text):
                    changed.append(os.path.join(user_apps, name))
    # launchers that are copies: XFCE panel launchers, desktop icons, existing autostart entries
    globs = [os.path.join(CFG, "xfce4", "panel"), os.path.join(HOME, "Desktop"), os.path.join(HOME, "桌面"),
             os.path.join(CFG, "autostart")]
    for base in globs:
        for dirpath, _dirs, files in os.walk(base):
            for n in files:
                if not n.endswith(".desktop") or n == "aiw-chrome.desktop":
                    continue
                p = os.path.join(dirpath, n)
                with contextlib.suppress(OSError):
                    with open(p, errors="replace") as f:
                        txt = f.read()
                    new = rewrite_desktop(txt)
                    if new.rstrip("\n") != txt.rstrip("\n") and _write(p, new, os.stat(p).st_mode & 0o777):
                        changed.append(p)
    # the XFCE "web browser" (exo-open --launch WebBrowser, the panel's browser button)
    helper = ("[Desktop Entry]\nNoDisplay=true\nVersion=1.0\nEncoding=UTF-8\nType=X-XFCE-Helper\n"
              "X-XFCE-Category=WebBrowser\nX-XFCE-CommandsWithParameter=" + WRAPPER + " \"%s\"\nIcon=chromium\n"
              "Name=AIW Chrome (debug port)\nX-XFCE-Commands=" + WRAPPER + "\n")
    hp = os.path.join(HOME, ".local", "share", "xfce4", "helpers", "aiw-chrome.desktop")
    if _write(hp, helper):
        changed.append(hp)
    rc = os.path.join(CFG, "xfce4", "helpers.rc")
    with contextlib.suppress(OSError):
        txt = open(rc).read() if os.path.exists(rc) else ""
        cur = next((ln.split("=", 1)[1].strip() for ln in txt.splitlines() if ln.startswith("WebBrowser=")), "")
        lines = [ln for ln in txt.splitlines() if not ln.startswith("WebBrowser=")] + ["WebBrowser=aiw-chrome"]
        # only take over the default browser when it is unset or already a Chrome / Chromium
        if (not cur or "chrom" in cur.lower()) and _write(rc, "\n".join(lines) + "\n"):
            changed.append(rc)
    if autostart and os.environ.get("AIW_CHROME_AUTOSTART", "1") != "0":
        ap = os.path.join(CFG, "autostart", "aiw-chrome.desktop")
        entry = ("[Desktop Entry]\nType=Application\nName=Chrome (AIW, debug port)\n"
                 f"Exec={WRAPPER} --restore-last-session\nX-GNOME-Autostart-enabled=true\nNoDisplay=false\nTerminal=false\n")
        if _write(ap, entry):
            changed.append(ap)
    return {"wrapper": WRAPPER, "desktop_entries": found, "changed": changed}


# ----------------------------------------------------------------------------- start / stop
def graceful_close(pid: int, timeout: float = 20) -> str:
    """SIGTERM is Chrome's clean shutdown on Linux (session saved, exit marked clean); SIGKILL only if it hangs."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return "closed"
        time.sleep(0.3)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    for _ in range(20):
        if not alive(pid):
            break
        time.sleep(0.25)
    return "killed"


def _launch_env(base: dict | None) -> dict:
    env = dict(os.environ)
    env.update({k: v for k, v in (base or {}).items() if k in (
        "DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
        "WAYLAND_DISPLAY", "LANG", "LANGUAGE", "LC_ALL", "GNOME_KEYRING_CONTROL", "SSH_AUTH_SOCK")})
    if not env.get("DISPLAY"):
        env["DISPLAY"] = _guess_display()
    return env


def _guess_display() -> str:
    with contextlib.suppress(OSError):
        xs = sorted(n for n in os.listdir("/tmp/.X11-unix") if n.startswith("X"))
        if ":1" in [":" + n[1:] for n in xs]:
            return ":1"
        if xs:
            return ":" + xs[0][1:]
    return ":1"


_SCOPE: list[str] | None = None


def scope_prefix(env: dict) -> list[str]:
    """Started from the vm-agent (a systemd user service), Chrome would live in the agent's cgroup and be killed on
    every agent restart (update-agent.sh restarts it). A transient scope of its own keeps it alive."""
    global _SCOPE
    if _SCOPE is None:
        _SCOPE = []
        if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
            with contextlib.suppress(Exception):
                ok = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"], env=env,
                                    capture_output=True, timeout=10).returncode == 0
                if ok:
                    _SCOPE = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    return list(_SCOPE)


def spawn(argv: list[str], env: dict) -> int:
    p = subprocess.Popen(scope_prefix(env) + argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, cwd=HOME)
    return p.pid


def launch(args: list[str], env: dict) -> int:
    if not os.access(WRAPPER, os.X_OK):
        install_launchers(DEFAULT_PORT)
    return spawn([WRAPPER, *args], env)


def wait_port(port: int, timeout: float = 40) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        v = version(port, 1.0)
        if v:
            return v
        time.sleep(0.7)
    return None


@contextlib.contextmanager
def _locked():
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    f = open(LOCK, "w")
    try:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        f.close()


def _usable(p: dict) -> bool:
    return p["kind"] in ("chrome", "chromium") and not _helper_profile(p)


def ensure(port: int = DEFAULT_PORT, also=(BOS_PORT,), relaunch: bool = True, start: bool = True,
           root: str = "/proc", wait: float = 45) -> dict:
    """-> {ok, port, how: found|relaunched|launched, ...} or {ok: False, reason, error, ...}."""
    with _locked():
        ps = procs(root)
        eps = discover((port, *also), root, ps)
        chrome_eps = [e for e in eps if e["kind"] != "browseros"]
        steps: list[str] = []
        if not chrome_eps:
            # a Chrome started with a port that does not answer yet may just be starting: give it a moment
            for p in ps:
                if _usable(p) and p["port"] and wait_port(p["port"], 12):
                    eps = discover((port, *also), root, ps)
                    chrome_eps = [e for e in eps if e["kind"] != "browseros"]
                    break
        portless = [p for p in ps if _usable(p) and not (p["port"] and version(p["port"]))]
        if chrome_eps:
            e = chrome_eps[0]
            return {"ok": True, "port": e["port"], "how": "found", "endpoint": e, "endpoints": eps}
        if eps and not (portless and relaunch):
            e = eps[0]                                  # only BrowserOS answers and no Chrome of the user's runs
            return {"ok": True, "port": e["port"], "how": "found", "endpoint": e, "endpoints": eps}
        if portless and relaunch:
            target = max(portless, key=lambda p: (cookies_mtime(p["dir"]), p["pid"]))
            env = _launch_env(environ_of(target["pid"], root))
            flags = kept_flags(target["argv"])
            bin_ = browser_bin(target["kind"], target["exe"])
            for p in portless:                          # every window of that browser goes with it
                if p["dir"] == target["dir"]:
                    steps.append(f"close {p['pid']}: {graceful_close(p['pid'])}")
            if not wait_unlock(target["dir"], 15):
                return {"ok": False, "reason": "profile_locked", "error": f"{target['dir']} 的配置锁一直没有释放",
                        "steps": steps}
            udd, migrated = target["dir"], None
            if target["default_dir"]:
                # Chrome 136+ never opens the port on its default profile directory: run it from a copy instead
                migrated = clone_profile(target["dir"], AIW_DIR)
                udd = AIW_DIR
                steps.append(f"profile copied to {AIW_DIR}")
            write_conf(bin_, udd, port, flags)
            launchers = install_launchers(port)
            launch(["--restore-last-session"], env)
            v = wait_port(port, wait)
            res = {"how": "relaunched", "port": port, "profile": udd, "migrated": migrated, "display": env.get("DISPLAY"),
                   "flags": flags, "bin": bin_, "steps": steps, "launchers": launchers.get("changed")}
            if v:
                return {"ok": True, "browser": v.get("Browser", ""), **res}
            return {"ok": False, "reason": "port_not_opened", "error": f"Chrome 已重启，但 {port} 端口在 {int(wait)} 秒内没有打开",
                    **res}
        if any(_usable(p) for p in ps):
            return {"ok": False, "reason": "port_not_answering",
                    "error": "Chrome 带着调试端口在运行，但端口不响应（可能卡住了）", "procs": [p["argv"][:6] for p in ps]}
        if not start:
            return {"ok": False, "reason": "not_running", "error": "Chrome 没有在运行"}
        bin_ = browser_bin()
        if not bin_:
            return {"ok": False, "reason": "no_browser_installed", "error": "这台电脑上没有安装 Chrome 或 Chromium"}
        # nothing running: start it on the profile the user logged in last (a default one is copied over first)
        udd, migrated = AIW_DIR, None
        if os.path.isfile(CONF):
            with open(CONF) as f:
                for ln in f:
                    if ln.startswith("AIW_CHROME_DIR="):
                        udd = ln.split("=", 1)[1].strip().strip("'\"") or AIW_DIR
        newest = max((d for d in (default_dir("chrome"), default_dir("chromium")) if os.path.isdir(d)),
                     key=cookies_mtime, default=None)
        if newest and cookies_mtime(newest) > cookies_mtime(udd) + 1 and udd == AIW_DIR and wait_unlock(newest, 3):
            migrated = clone_profile(newest, AIW_DIR)
        if migrated or not os.path.isfile(CONF):
            # a copied default profile keeps the password store Chrome picks by itself (its cookies are encrypted with it)
            write_conf(bin_, udd, port, [] if migrated else (None if os.path.exists(FLAGS) else ["--password-store=basic"]))
        launchers = install_launchers(port)
        env = _launch_env({})
        launch(["--restore-last-session"], env)
        v = wait_port(port, wait)
        res = {"how": "launched", "port": port, "profile": udd, "migrated": migrated, "display": env.get("DISPLAY"),
               "bin": bin_, "launchers": launchers.get("changed")}
        if v:
            return {"ok": True, "browser": v.get("Browser", ""), **res}
        reason = "no_display" if not os.path.exists("/tmp/.X11-unix") else "port_not_opened"
        return {"ok": False, "reason": reason, "error": f"启动了 Chrome，但 {port} 端口在 {int(wait)} 秒内没有打开", **res}


# ----------------------------------------------------------------------------- without CDP: the visible browser
def open_url(url: str, root: str = "/proc") -> dict:
    """Open url as a new tab of the browser on screen (its single-instance lock forwards it), else start one."""
    ps = [p for p in procs(root) if _usable(p) or p["kind"] == "browseros"]
    if ps:
        p = max(ps, key=lambda x: (x["kind"] != "browseros", cookies_mtime(x["dir"])))
        env = _launch_env(environ_of(p["pid"], root))
        args = [browser_bin(p["kind"], p["exe"]) or p["exe"]]
        if p["user_data_dir"]:
            args.append(f"--user-data-dir={p['user_data_dir']}")
        spawn(args + [url], env)   # handed to the running instance, which opens it as a tab
        how = "new_tab"
    else:
        env = _launch_env({})
        launch([url], env)
        how = "launched"
    time.sleep(1.5)
    if shutil.which("xdotool"):
        subprocess.run("xdotool search --onlyvisible --class 'chrom|Chrom|browserclaw' | tail -1 | "
                       "xargs -r xdotool windowactivate --sync", shell=True, env=env, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "how": how, "display": env.get("DISPLAY")}


def close_tab(title_has: list[str]) -> dict:
    """Ctrl+W, only when the active window's title shows the page we opened (never someone else's tab)."""
    if not shutil.which("xdotool"):
        return {"ok": False, "error": "xdotool missing"}
    env = _launch_env({})
    t = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], env=env, capture_output=True, text=True,
                       timeout=5).stdout.strip()
    if not any(s and s.lower() in t.lower() for s in title_has):
        return {"ok": False, "title": t, "error": "active window is not the page we opened"}
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+w"], env=env, timeout=5)
    return {"ok": True, "title": t}


# ----------------------------------------------------------------------------- bring a page to the front
BROWSER_CLASSES = "browserclaw|BrowserOS|browseros|chromium|Chromium|google-chrome|Google-chrome"


def _http_get(port: int, path: str, timeout: float = 3) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # DevTools is local: never via a proxy
    with opener.open(f"http://127.0.0.1:{int(port)}{path}", timeout=timeout) as r:  # noqa: S310
        return r.read()


def page_targets(port: int) -> list[dict]:
    try:
        return [t for t in json.loads(_http_get(port, "/json/list").decode()) if t.get("type") == "page"]
    except Exception:  # noqa: BLE001
        return []


def _xdo(args: list[str], env: dict, timeout: float = 5) -> str:
    try:
        return subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def active_title(env: dict) -> str:
    return _xdo(["getactivewindow", "getwindowname"], env).strip()


def title_matches(active: str, title: str) -> bool:
    """Chrome names its window '<tab title> - Google Chrome' (BrowserOS: '<tab title> - BrowserOS neo')."""
    a, t = " ".join((active or "").split()), " ".join((title or "").split())
    return bool(a and t) and (a.startswith(t[:40]) or t[:30] in a)


def _windows(env: dict, title: str = "") -> list[str]:
    """Browser windows on screen: the ones whose name holds the page's title first, then the rest."""
    ids = _xdo(["search", "--onlyvisible", "--class", BROWSER_CLASSES], env).split()
    first = []
    if title:
        named = set(_xdo(["search", "--onlyvisible", "--name", _re_escape(title[:40])], env).split())
        first = [w for w in ids if w in named]
    return first + [w for w in ids if w not in first]


def _re_escape(s: str) -> str:
    import re
    return re.escape(s)


def front(port: int, target_id: str = "", url: str = "", env: dict | None = None, keys: bool = True,
          max_tabs: int = 40, pause: float = 0.12) -> dict:
    """Select a DevTools page's tab in its window and raise that window. One JSON-able dict, never raises.

    verified = the active X window's title is now the page's title. how = already | cdp | window | keys.
    keys=False never touches the keyboard (for a caller that must not move the user's tabs)."""
    env = _launch_env(env or {})
    pages = page_targets(port)
    t = next((p for p in pages if target_id and p.get("id") == target_id), None)
    if not t and url:
        t = next((p for p in pages if p.get("url") == url), None) or \
            next((p for p in pages if url.rstrip("/") and url.rstrip("/") in (p.get("url") or "")), None)
    if not t:
        return {"ok": False, "verified": False, "error": f"no DevTools page {target_id or url} on port {port}"}
    title, tid = (t.get("title") or t.get("url") or "").strip(), t.get("id")
    res = {"ok": True, "verified": False, "id": tid, "url": t.get("url"), "title": title, "how": ""}
    if not shutil.which("xdotool"):
        with contextlib.suppress(Exception):
            _http_get(port, f"/json/activate/{tid}")
        return {**res, "how": "cdp", "error": "xdotool missing: the tab is activated but the window is not checked"}
    if title_matches(active_title(env), title):
        return {**res, "verified": True, "how": "already", "window": active_title(env)}
    with contextlib.suppress(Exception):
        _http_get(port, f"/json/activate/{tid}")
        time.sleep(0.25)
    wins = _windows(env, title)
    for w in wins[:6]:   # a plain Chrome selected the tab: only its window has to come up
        _xdo(["windowmap", w], env)
        _xdo(["windowactivate", "--sync", w], env)
        if title_matches(active_title(env), title):
            return {**res, "verified": True, "how": "window" if not res["how"] else res["how"], "window": active_title(env)}
    if keys:
        n = max(2, min(max_tabs, len(pages) + 1))
        for w in wins[:4]:   # BrowserOS (or a Chrome that ignored the activation): select the tab like a person would
            _xdo(["windowactivate", "--sync", w], env)
            start = active_title(env)
            seen = 0
            for _ in range(n):
                _xdo(["key", "--clearmodifiers", "ctrl+Next"], env)
                time.sleep(pause)
                now = active_title(env)
                if title_matches(now, title):
                    return {**res, "verified": True, "how": "keys", "window": now}
                seen += 1
                if now == start and seen > 1:
                    break   # back where we began: the page is not in this window
    return {**res, "how": "cdp", "window": active_title(env)}


def status(port: int = DEFAULT_PORT) -> dict:
    ps = procs()
    return {"version": VERSION, "endpoints": discover((port,), ps=ps),
            "browsers": [{k: p[k] for k in ("pid", "kind", "dir", "port", "default_dir")} for p in ps if not _helper_profile(p)],
            "wrapper": os.access(WRAPPER, os.X_OK), "conf": open(CONF).read() if os.path.isfile(CONF) else None}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("ensure")
    e.add_argument("--port", type=int, default=DEFAULT_PORT)
    e.add_argument("--also", type=int, action="append", default=None)
    e.add_argument("--no-relaunch", action="store_true")
    e.add_argument("--no-start", action="store_true")
    e.add_argument("--wait", type=float, default=45)
    d = sub.add_parser("discover")
    d.add_argument("--port", type=int, action="append", default=[])
    i = sub.add_parser("install-launchers")
    i.add_argument("--port", type=int, default=DEFAULT_PORT)
    i.add_argument("--no-autostart", action="store_true")
    o = sub.add_parser("open-url")
    o.add_argument("--url", required=True)
    c = sub.add_parser("close-tab")
    c.add_argument("--title-has", action="append", default=[])
    s = sub.add_parser("status")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    f = sub.add_parser("front")
    f.add_argument("--port", type=int, default=DEFAULT_PORT)
    f.add_argument("--id", default="")
    f.add_argument("--url", default="")
    f.add_argument("--no-keys", action="store_true")
    sub.add_parser("version")
    a = ap.parse_args()
    try:
        if a.cmd == "ensure":
            r = ensure(a.port, tuple(a.also) if a.also is not None else (BOS_PORT,), not a.no_relaunch, not a.no_start,
                       wait=a.wait)
            say(r, 0 if r.get("ok") else 3)
        if a.cmd == "discover":
            say({"endpoints": discover(tuple(a.port))})
        if a.cmd == "install-launchers":
            say({"ok": True, **install_launchers(a.port, autostart=not a.no_autostart)})
        if a.cmd == "open-url":
            say(open_url(a.url))
        if a.cmd == "close-tab":
            say(close_tab(a.title_has))
        if a.cmd == "status":
            say(status(a.port))
        if a.cmd == "front":
            r = front(a.port, a.id, a.url, keys=not a.no_keys)
            say(r, 0 if r.get("ok") else 3)
        say({"version": VERSION})
    except SystemExit:
        raise
    except Exception as ex:  # noqa: BLE001 - always one JSON line, never a bare traceback
        say({"ok": False, "reason": "tool_failed", "error": f"{type(ex).__name__}: {ex}"[:400]}, 1)


if __name__ == "__main__":
    main()
