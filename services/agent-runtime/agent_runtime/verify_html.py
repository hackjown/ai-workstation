"""v3.9.26 (task-830): verify_html — the built-in way to check a page in a real headless Chromium.

task-830 spent ~15 minutes on verification itself: a headless chrome that hung the shell, BrowserOS MCP (9210) down,
the policy blocking 9222, CDP answering 403 until --remote-allow-origins=*. This tool does it one way that works:

* finds Chromium / Chrome (AIW_CHROME, chromium, chromium-browser, google-chrome(-stable), Playwright's bundled one);
* starts it headless with --remote-debugging-port=0 --remote-allow-origins=* and its own profile, in its own process
  group, under a hard timeout (SIGALRM inside + `timeout` outside); the whole process tree is killed at the end;
* loads a file:// path or an http(s) url over CDP (a tiny stdlib WebSocket client: nothing to install);
* returns ONE JSON line: console errors / exceptions, counts for each selector, each click action
  (click selector -> did the expected overlay/selector become visible, its text length), optional screenshot.
No browser -> {"ok": false, "error": "no_browser", ...} (a structured error, never a hang).

The part above `# ---- loop side` runs ON THE COMPUTER (command() embeds it in a python3 heredoc), stdlib only.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "microsoft-edge")


class VerifyTimeout(Exception):
    pass


def find_browser():
    env = os.environ.get("AIW_CHROME")
    if env and os.path.exists(os.path.expanduser(env)):
        return os.path.expanduser(env)
    for n in NAMES:
        p = shutil.which(n)
        if p:
            return p
    for pat in ("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux*/headless_shell",
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
                "/opt/google/chrome/chrome", "/snap/bin/chromium"):
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    return None


class WS:
    """A minimal RFC 6455 client (text frames, masking, ping/pong, fragmentation) for one CDP target."""

    def __init__(self, url, timeout=10.0):
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            buf += chunk
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("websocket handshake refused: " + head.split(b"\r\n")[0].decode(errors="replace"))

    def send(self, text):
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9:   # ping
                self.sock.sendall(bytes([0x8A, len(data)]) + data[:125])
                continue
            if op == 0x8:
                raise ConnectionError("websocket closed by browser")
            msg += data
            if fin:
                return msg.decode(errors="replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    def call(self, method, params=None, timeout=15.0):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            self.ws.sock.settimeout(max(0.1, end - time.time()))
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result") or {}
            if "method" in m:
                self.events.append(m)
        raise VerifyTimeout(f"{method} timed out")

    def pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.ws.sock.settimeout(max(0.05, end - time.time()))
            try:
                m = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                return False
            if "method" in m:
                self.events.append(m)
                if until and m["method"] == until:
                    return True
        return False

    def js(self, expr, timeout=10.0):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        if r.get("exceptionDetails"):
            return {"__error__": (r["exceptionDetails"].get("exception") or {}).get("description")
                    or r["exceptionDetails"].get("text")}
        return (r.get("result") or {}).get("value")


def _console(events):
    errs = []
    for e in events:
        m, p = e.get("method"), e.get("params") or {}
        if m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            errs.append({"kind": "exception", "text": ((d.get("exception") or {}).get("description") or d.get("text") or "")[:300]})
        elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            txt = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args") or [])
            errs.append({"kind": "console", "text": txt[:300]})
        elif m == "Log.entryAdded" and (p.get("entry") or {}).get("level") == "error":
            en = p["entry"]
            errs.append({"kind": "log", "text": (str(en.get("text") or "") + " " + str(en.get("url") or ""))[:300]})
    return errs


VIS_JS = ("(function(s){const e=document.querySelector(s);if(!e)return{exists:false,visible:false,text_len:0};"
          "const c=getComputedStyle(e);const r=e.getBoundingClientRect();"
          "return{exists:true,visible:c.display!=='none'&&c.visibility!=='hidden'&&parseFloat(c.opacity||'1')>0&&r.width>0&&r.height>0,"
          "text_len:(e.innerText||e.textContent||'').trim().length}})(%s)")
CLICK_JS = ("(function(s,i){const els=document.querySelectorAll(s);if(!els.length)return{found:0};const e=els[Math.min(i,els.length-1)];"
            "try{e.scrollIntoView({block:'center'})}catch(_){};e.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));"
            "return{found:els.length}})(%s,%d)")


BG_JS = ("(function(){const b=document.body||document.documentElement,h=document.documentElement;const cb=getComputedStyle(b),"
         "ch=getComputedStyle(h);return [cb.backgroundColor,ch.backgroundColor,cb.color].join('|')})()")
MEDIA_JS = ("(function(){let n=0;for(const s of document.styleSheets){let r;try{r=s.cssRules}catch(e){continue}"
            "for(const x of r){if(x.media&&/prefers-color-scheme/.test(x.media.mediaText))n++}}return n})()")
FONT_JS = ("(function(s){const els=[...document.querySelectorAll(s)].filter(e=>e.offsetParent!==null||e.getClientRects().length);"
           "const e=els[0]||document.querySelector(s);return e?getComputedStyle(e).fontFamily:null})(%s)")
IMG_JS = ("(function(s){const els=[...document.querySelectorAll(s)];let loaded=0;for(const e of els){if(e.tagName==='IMG'){"
          "if(e.naturalWidth>0||!e.complete)loaded++}else loaded++}return {n:els.length,loaded:loaded}})(%s)")
TEXT_JS = "(document.documentElement.textContent||'')"


def _norm_text(t):
    return "".join(str(t or "").split()).lower()


def eval_checks(cdp, checks, url, html=None):
    """v3.9.29: requirement checks derived from the user's request (dev_contract.requirement_checks)."""
    res = []
    for c in checks or []:
        if not isinstance(c, dict) or not c.get("kind"):
            continue
        r = {"id": c.get("id"), "label": c.get("label"), "ok": False}
        k = c["kind"]
        try:
            if k == "dark_mode":
                rules = cdp.js(MEDIA_JS) or 0
                looks = {}
                for mode in ("light", "dark"):
                    cdp.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": mode}]})
                    cdp.pump(0.15)
                    looks[mode] = cdp.js(BG_JS)
                cdp.call("Emulation.setEmulatedMedia", {"features": []})
                r["ok"] = bool(looks.get("light")) and looks.get("light") != looks.get("dark")
                r["detail"] = f"media rules={rules}; light={looks.get('light')}; dark={looks.get('dark')}"
            elif k == "font":
                fam = cdp.js(FONT_JS % json.dumps(c.get("selector") or "h1"))
                want = [str(x).lower() for x in c.get("any_of") or []]
                r["ok"] = bool(fam) and any(w in str(fam).lower() for w in want)
                r["detail"] = f"font-family={fam}"
            elif k in ("min_count", "selector"):
                v = cdp.js(IMG_JS % json.dumps(c.get("selector") or "img"))
                v = v if isinstance(v, dict) else {"n": 0, "loaded": 0}
                r["ok"] = int(v.get("loaded") or 0) >= int(c.get("min") or 1)
                r["detail"] = f"count={v.get('n')} loaded={v.get('loaded')} min={c.get('min') or 1}"
            elif k == "text":
                txt = cdp.js(TEXT_JS)
                r["ok"] = _norm_text(c.get("contains")) in _norm_text(txt)
                r["detail"] = ("found " if r["ok"] else "missing ") + repr(c.get("contains"))
            elif k == "max_bytes":
                n = os.path.getsize(url[7:]) if url.startswith("file://") else len((html or "").encode())
                r["ok"] = n <= int(c.get("max") or 0)
                r["detail"] = f"size={n} max={c.get('max')}"
            elif k == "no_embedded_font":
                body = html
                if body is None and url.startswith("file://"):
                    with open(url[7:], encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                hits = len(re.findall(r"@font-face[^}]*url\(\s*['\"]?data:|data:(font|application)/(woff2?|ttf|otf|x-font)", body or "", re.I))
                r["ok"] = hits == 0
                r["detail"] = f"embedded fonts={hits}"
            else:
                r["detail"] = "unknown check"
        except Exception as e:  # noqa: BLE001
            r["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
        res.append(r)
    return res


def to_url(target):
    t = str(target or "").strip()
    if t.startswith(("http://", "https://", "file://", "data:", "about:")):
        return t
    p = os.path.expanduser(t)
    if not os.path.isabs(p):
        base = os.path.expanduser(os.environ.get("VM_AGENT_FILES_ROOT", "~/workspace"))
        p = os.path.join(base, p)
    return "file://" + os.path.abspath(p)


def run(spec):
    t0 = time.time()
    url = to_url(spec.get("url") or spec.get("path"))
    out = {"ok": False, "url": url, "console_errors": [], "selectors": {}, "clicks": [], "screenshot": None}
    if url.startswith("file://") and not os.path.exists(url[7:]):
        out.update(error="not_found", detail=f"file not found: {url[7:]}")
        return out
    exe = find_browser()
    if not exe:
        out.update(error="no_browser", detail="no Chromium/Chrome found (AIW_CHROME, chromium, google-chrome, Playwright)",
                   hint="安装 chromium（sudo apt install chromium 或 pip install playwright && playwright install chromium）后重试")
        return out
    out["browser"] = exe
    prof = tempfile.mkdtemp(prefix="aiw-verify-")
    w, h = (spec.get("viewport") or [1280, 900])[:2]
    args = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run", "--no-default-browser-check",
            "--disable-dev-shm-usage", "--disable-extensions", "--mute-audio", "--hide-scrollbars",
            "--remote-debugging-port=0", "--remote-allow-origins=*", f"--user-data-dir={prof}",
            f"--window-size={int(w)},{int(h)}", "about:blank"]
    proc = None
    ws = None
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        port = None
        end = time.time() + float(spec.get("start_timeout") or 15)
        pf = os.path.join(prof, "DevToolsActivePort")
        while time.time() < end and port is None:
            if proc.poll() is not None:
                raise RuntimeError(f"browser exited at start (code {proc.returncode})")
            if os.path.exists(pf):
                try:
                    port = int(open(pf).read().split()[0])
                except (ValueError, IndexError, OSError):
                    pass
            time.sleep(0.1)
        if not port:
            raise VerifyTimeout("browser did not open its DevTools port")
        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read())
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            page = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT"), timeout=5).read())
        ws = WS(page["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            cdp.call(m)
        cdp.call("Page.navigate", {"url": url})
        loaded = cdp.pump(float(spec.get("load_timeout") or 15), until="Page.loadEventFired")
        cdp.pump(float(spec.get("settle_ms") or 600) / 1000.0)
        out["loaded"] = loaded
        out["title"] = cdp.js("document.title")
        out["text_len"] = cdp.js("(document.body&&document.body.innerText||'').trim().length")
        for sel in spec.get("selectors") or []:
            v = cdp.js("document.querySelectorAll(%s).length" % json.dumps(sel))
            out["selectors"][sel] = v if isinstance(v, int) else -1
        if spec.get("req_checks"):   # v3.9.29
            html = None if url.startswith("file://") else cdp.js("document.documentElement.outerHTML")
            out["checks"] = eval_checks(cdp, spec["req_checks"], url, html)
            out["checks_ok"] = all(c.get("ok") for c in out["checks"])
        for c in spec.get("clicks") or []:
            if not isinstance(c, dict) or not c.get("click"):
                continue
            r = {"click": c["click"], "index": int(c.get("index") or 0), "expect": c.get("expect")}
            if c.get("before_js"):
                cdp.js(str(c["before_js"]))
            f = cdp.js(CLICK_JS % (json.dumps(c["click"]), int(c.get("index") or 0)))
            r["found"] = (f or {}).get("found", 0) if isinstance(f, dict) else 0
            cdp.pump(float(c.get("wait_ms") or 500) / 1000.0)
            if c.get("expect"):
                v = cdp.js(VIS_JS % json.dumps(c["expect"]))
                v = v if isinstance(v, dict) else {}
                r.update(visible=bool(v.get("visible")), exists=bool(v.get("exists")), text_len=int(v.get("text_len") or 0))
                r["ok"] = r["found"] > 0 and r["visible"] and r["text_len"] >= int(c.get("min_text") or 0)
            else:
                r["ok"] = r["found"] > 0
            if c.get("reset_js"):
                cdp.js(str(c["reset_js"]))
            elif c.get("reset", True):
                cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
                cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
                cdp.pump(0.2)
            out["clicks"].append(r)
        if spec.get("screenshot"):
            shot = os.path.expanduser(str(spec["screenshot"]))
            if not os.path.isabs(shot):
                shot = os.path.join(os.path.expanduser(os.environ.get("VM_AGENT_FILES_ROOT", "~/workspace")), shot)
            os.makedirs(os.path.dirname(shot) or ".", exist_ok=True)
            img = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": bool(spec.get("full_page"))}, 30)
            with open(shot, "wb") as fh:
                fh.write(base64.b64decode(img.get("data") or ""))
            out["screenshot"] = shot
        cdp.pump(0.1)
        out["console_errors"] = _console(cdp.events)[:30]
        exc = [e for e in out["console_errors"] if e["kind"] == "exception"]
        errs = exc if not spec.get("strict_console") else out["console_errors"]
        min_sel = spec.get("min_counts") or {}
        sel_ok = all(out["selectors"].get(k, 0) >= int(v) for k, v in min_sel.items())
        out["ok"] = bool(loaded or out.get("text_len")) and not errs and sel_ok and all(c.get("ok") for c in out["clicks"])
        if not sel_ok:
            out["failed_counts"] = {k: [out["selectors"].get(k, 0), int(v)] for k, v in min_sel.items()
                                    if out["selectors"].get(k, 0) < int(v)}
    except VerifyTimeout as e:
        out.update(error="timeout", detail=str(e))
    except Exception as e:  # noqa: BLE001
        out.update(error=type(e).__name__, detail=str(e)[:400])
    finally:
        if ws is not None:
            ws.close()
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(3)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(prof, ignore_errors=True)
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def main(argv):
    spec = json.loads(argv[1]) if len(argv) > 1 else {}
    limit = int(float(spec.get("timeout") or 60))

    def _alarm(*_a):
        raise VerifyTimeout(f"hard timeout {limit}s")
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(limit)
    except (ValueError, AttributeError):
        pass
    try:
        res = run(spec)
    except VerifyTimeout as e:
        res = {"ok": False, "error": "timeout", "detail": str(e)}
    finally:
        try:
            signal.alarm(0)
        except (ValueError, AttributeError):
            pass
    print(json.dumps(res, ensure_ascii=False))


# ---- loop side (not shipped to the computer)
def command(spec: dict) -> str:
    """Shell that runs the checker above on the computer: one JSON line on stdout."""
    import inspect
    import shlex
    src = inspect.getsource(sys.modules[__name__]).split("\n# ---- loop side")[0]
    limit = int(float(spec.get("timeout") or 60))
    return (f"timeout -k 5 {limit + 15} python3 - {shlex.quote(json.dumps(spec, ensure_ascii=False))} <<'AIW_VH_EOF'\n"
            f"{src}\nmain(sys.argv)\nAIW_VH_EOF")


def parse(stdout: str) -> dict:
    for line in reversed([x for x in str(stdout or "").splitlines() if x.strip()]):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "error": "no_result", "detail": str(stdout or "")[-300:]}


def brief(res: dict, limit: int = 1800) -> str:
    """The result as the model sees it."""
    r = dict(res or {})
    r["console_errors"] = (r.get("console_errors") or [])[:8]
    s = json.dumps(r, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


SCHEMA_DESC = ("Check an HTML page in a real headless Chromium (built in; use this instead of starting chrome/chromium, "
               "BrowserOS or CDP by hand). Loads `path` (file under ~/workspace or absolute) or `url`, returns JSON: "
               "console errors/exceptions, the count of each CSS selector in `selectors`, and for each item of `clicks` "
               "({click: selector, index, expect: selector that must become visible, min_text}) whether it worked, plus an "
               "optional screenshot path; `req_checks` (added by the system for dev tasks) report each requirement as checks[]. Hard timeout, the browser is always killed. Use it as the acceptance / "
               "regression evidence of a dev fix.")

if __name__ == "__main__":
    main(sys.argv)
