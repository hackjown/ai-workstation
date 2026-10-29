#!/usr/bin/env python3
"""Photopea bridge for the AI Workstation VM (stdlib only, python 3.9+).

Photoshop does not run on the Linux VMs. Photopea (photopea.com, a browser clone of Photoshop that reads and
writes PSD and runs Photoshop-style JavaScript) does. This file runs ON THE VM; the agent ships it there by itself
(agent_runtime/photopea.py) and calls it through the shell tool.

    pp.py run --job-b64 <base64 json>   open files, run a script, save the results (starts everything as needed)
    pp.py ensure                        start the bridge server and the Photopea window, wait until ready
    pp.py status | stop

How it works: `serve` is a small HTTP server on 127.0.0.1:$AIW_PP_PORT (8791). A Chrome/Chromium window on the
desktop (the user watches it over noVNC) shows its page, which embeds Photopea in an iframe and talks to it with
Photopea's Live Messaging API (postMessage: an ArrayBuffer opens a file, a string runs a script, Photopea answers
"done"; app.echoToOE(s) sends strings back, doc.saveToOE("png") sends files back). The page long-polls the server
for jobs. Files go over HTTP as raw bytes; nothing is base64'd in the browser.

Job JSON: {"open": [paths], "fonts": [paths], "place": [paths], "script": "js", "save": [paths],
           "keep": false, "info": false, "timeout": 90}
Relative paths are relative to $VM_AGENT_FILES_ROOT or ~/workspace. Output format comes from each save path's
extension (psd png jpg jpeg webp gif svg pdf; "out.jpg:0.8" sets the quality).

Findings from testing against the real Photopea (2026-10-24, Chromium 14x):
- Chromium blocks third-party storage by default; Photopea in an iframe then throws on localStorage and never
  says "done". The private profile used here allows third-party cookies.
- Fonts load asynchronously: a save in the same script as `t.font = ...` gave a blank PNG. Saves run as their own
  message after a pause.
- A helper with parameters named a..h (or using `arguments`) made the next artLayers.add() kill Photopea for good.
  The built-in helpers use __ names; a job that gets no "done" reloads Photopea. A for-in loop over a UnitValue
  (layer.bounds[i], textItem.size; typeof says "number" but it is an object, read .value) killed it too.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PPV = "9"  # bump when this file changes; a running bridge of another version is restarted
PORT = int(os.environ.get("AIW_PP_PORT", "8791"))
HOME = Path.home()
CFG = HOME / ".config" / "aiw" / "photopea"
ROOT = Path(os.environ.get("VM_AGENT_FILES_ROOT") or (HOME / "workspace"))
BASE = f"http://127.0.0.1:{PORT}"
PP_URL = os.environ.get("AIW_PP_URL", "https://www.photopea.com")

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Photopea (AI Workstation)</title>
<style>html,body{margin:0;height:100%;overflow:hidden;background:#1e1e1e}
#bar{position:fixed;left:0;right:0;bottom:0;height:20px;font:12px sans-serif;color:#9cf;background:#111;padding:2px 8px;z-index:9}
iframe{border:0;width:100%;height:calc(100% - 24px)}</style></head><body>
<iframe id="pp" allow="clipboard-read; clipboard-write" src="__PPSRC__"></iframe><div id="bar">AI Workstation · Photopea: loading…</div>
<script>
const fr = document.getElementById('pp'), bar = document.getElementById('bar');
let ready = false, waiters = [], collect = null;
const say = t => { bar.textContent = 'AI Workstation · Photopea: ' + t; };
window.addEventListener('message', e => {
  if (e.source !== fr.contentWindow) return;
  const d = e.data;
  if (d === 'done') {
    if (!ready) { ready = true; say('ready'); fetch('/event', {method: 'POST', body: '{"type":"ready"}'}); loop(); return; }
    const w = waiters.shift(); if (w) w(); return;
  }
  if (!collect) return;
  if (typeof d === 'string') collect.echo.push(d);
  else if (d instanceof ArrayBuffer) collect.files.push(d);
});
function send(msg, ms) {
  return new Promise((ok, bad) => {
    const t = setTimeout(() => { const i = waiters.indexOf(f); if (i >= 0) waiters.splice(i, 1); bad(new Error('Photopea did not answer in ' + ms / 1000 + ' s')); }, ms);
    const f = () => { clearTimeout(t); ok(); };
    waiters.push(f); fr.contentWindow.postMessage(msg, '*');
  });
}
async function runJob(job) {
  const res = {id: job.id, echo: [], ok: true, error: null, nfiles: 0};
  collect = {echo: res.echo, files: []};
  try {
    for (let i = 0; i < job.sends.length; i++) {
      say('loading ' + job.sends[i]);
      const r = await fetch('/in/' + job.id + '/' + i);
      if (!r.ok) throw new Error('cannot read ' + job.sends[i]);
      await send(await r.arrayBuffer(), job.timeout * 1000);
    }
    if (job.sends.length) await new Promise(r => setTimeout(r, 800));  // let a just-opened file settle
    for (let i = 0; i < job.scripts.length; i++) {
      if (i) await new Promise(r => setTimeout(r, job.gap || 300));
      say(i ? 'saving' : 'running script'); await send(job.scripts[i], job.timeout * 1000);
    }
    await new Promise(r => setTimeout(r, 300));  // late saveToOE buffers
  } catch (e) {
    res.ok = false; res.error = String(e && e.message || e);
    if (/did not answer/.test(res.error)) {  // Photopea dies on some script errors and never answers again: reload it
      res.error += '. Photopea crashed on this step and was reloaded, so every open document is gone: open the files again and avoid what failed';
      ready = false; waiters = []; fr.src = fr.src.split('#')[0] + '?r=' + Date.now() + '#' + fr.src.split('#')[1];
    }
  }
  const files = collect.files; collect = null;
  for (let i = 0; i < files.length; i++) await fetch('/out/' + job.id + '/' + i, {method: 'POST', body: files[i]});
  res.nfiles = files.length;
  await fetch('/result', {method: 'POST', body: JSON.stringify(res)});
  say(res.ok ? 'ready' : 'error: ' + res.error);
}
let looping = false;
async function loop() {
  if (looping) return; looping = true;
  for (;;) {
    if (!ready) { await new Promise(r => setTimeout(r, 500)); continue; }
    try {
      const r = await fetch('/poll?ready=' + (ready ? 1 : 0));
      if (r.status === 200) await runJob(await r.json());
    } catch (e) { await new Promise(r => setTimeout(r, 1500)); }
  }
}
setInterval(() => { if (!ready) fetch('/event', {method: 'POST', body: '{"type":"loading"}'}).catch(() => {}); }, 5000);
</script></body></html>"""


# ---------------------------------------------------------------- server

class State:
    def __init__(self):
        self.jobs: "queue.Queue[dict]" = queue.Queue()
        self.by_id: dict[str, dict] = {}
        self.ready = False
        self.seen = 0.0           # last request from the page
        self.lock = threading.Lock()


S = State()


def _cors(h):
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Private-Network", "true")
    h.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "*")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        elif isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        _cors(self)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            src = PP_URL + "#" + urllib.request.quote(json.dumps({"environment": {"localsave": True}}), safe="")
            return self._send(200, PAGE.replace("__PPSRC__", src), "text/html; charset=utf-8")
        if p == "/status":
            return self._send(200, {"ok": True, "ppv": PPV, "ready": S.ready,
                                    "page_seen_s": round(time.time() - S.seen, 1) if S.seen else None,
                                    "queued": S.jobs.qsize()})
        if p == "/poll":
            S.seen = time.time()
            if "ready=1" in self.path:  # a restarted server learns the page's state from its next poll
                S.ready = True
            try:
                job = S.jobs.get(timeout=20)
            except queue.Empty:
                S.seen = time.time()
                return self._send(204)
            S.seen = time.time()
            return self._send(200, {k: job.get(k, 0) for k in ("id", "sends", "scripts", "gap", "timeout")})
        if p.startswith("/in/") or p.startswith("/f/"):
            # /in/<job>/<n>: files the page sends to Photopea; /f/<job>/<n>: files a script places with app.open(url)
            parts = p.split("/")
            job = S.by_id.get(parts[2]) if len(parts) == 4 else None
            key = "send_paths" if parts[1] == "in" else "place_paths"
            try:
                path = job[key][int(parts[3])]
                return self._send(200, Path(path).read_bytes(), "application/octet-stream")
            except Exception:  # noqa: BLE001
                return self._send(404, {"error": "no such file"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/event":
            S.seen = time.time()
            try:
                ev = json.loads(self._body() or b"{}")
            except ValueError:
                ev = {}
            if ev.get("type") == "ready":
                S.ready = True
            elif ev.get("type") == "loading":
                S.ready = False
            return self._send(200, {"ok": True})
        if p.startswith("/out/"):
            parts = p.split("/")
            job = S.by_id.get(parts[2]) if len(parts) == 4 else None
            data = self._body()
            if job is not None:
                job["outs"][int(parts[3])] = data
            return self._send(200, {"ok": True})
        if p == "/result":
            S.seen = time.time()
            res = json.loads(self._body() or b"{}")
            job = S.by_id.get(res.get("id"))
            if job is not None:
                job["result"] = res
                job["done"].set()
            return self._send(200, {"ok": True})
        if p == "/job":
            req = json.loads(self._body() or b"{}")
            return self._send(200, _run_job(req))
        if p == "/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)), daemon=True).start()
            return None
        return self._send(404, {"error": "not found"})


def _queue(job: dict, wait_s: float) -> bool:
    S.by_id[job["id"]] = job
    S.jobs.put(job)
    return job["done"].wait(wait_s)


def _new_job(jid: str, timeout: int, **kw) -> dict:
    job = {"id": jid, "sends": [], "send_paths": [], "place_paths": [], "scripts": [], "gap": 300,
           "timeout": timeout, "outs": {}, "done": threading.Event(), "result": None}
    job.update(kw)
    return job


def _run_job(req: dict) -> dict:
    with S.lock:  # one job at a time: Photopea has one active document
        if not S.ready:
            return {"ok": False, "error": "Photopea is not loaded in the bridge window yet"}
        jid = uuid.uuid4().hex[:12]
        timeout = int(req.get("timeout") or 90)
        place = [str(x) for x in req.get("place") or []]
        if not req.get("keep") and (req.get("open") or req.get("fresh")):
            pre = _new_job(jid + "c", timeout, scripts=[_CLOSE_ALL])
            _queue(pre, min(timeout, 60))
            S.by_id.pop(pre["id"], None)
        sends = [str(x) for x in (req.get("fonts") or [])] + [str(x) for x in (req.get("open") or [])]
        body = req.get("script") or ""
        saves = [str(x) for x in req.get("save") or []]
        scripts = []
        if body or place:
            scripts.append(_prelude(jid, place) + "try{\n" + body + "\n}catch(e){__pplog.push('PPERR '+e+"
                           "(e&&e.line?' (line '+e.line+')':''));}\n"
                           "if(__pplog.length)app.echoToOE('PPLOG '+JSON.stringify(__pplog));")
        tail = "".join(f"app.activeDocument.saveToOE({json.dumps(_fmt(s))});\n" for s in saves)
        if tail:  # own message after a pause: text/fonts render asynchronously
            scripts.append("try{\n" + tail + "}catch(e){app.echoToOE('PPERR save: '+e);}")
        if req.get("info"):
            scripts.append(_INFO)
        gap = 4000 if re.search(r"\.font\b|textItem|LayerKind\.TEXT|ppPlace", body) else (1200 if body else 300)
        job = _new_job(jid, timeout, sends=[Path(x).name for x in sends], send_paths=sends, place_paths=place,
                       scripts=scripts, gap=gap)
        wait = timeout * (len(sends) + len(scripts) + 1) + gap * len(scripts) / 1000 + 30
        if not _queue(job, wait):
            S.by_id.pop(jid, None)
            return {"ok": False, "error": f"no answer from the Photopea window within {int(wait)} s"}
        res = job["result"] or {}
        S.by_id.pop(jid, None)
        outs = [job["outs"].get(i, b"") for i in range(int(res.get("nfiles") or 0))]
        written = []
        for i, data in enumerate(outs):
            if i < len(saves):
                dest = Path(saves[i].rsplit(":", 1)[0] if _has_q(saves[i]) else saves[i])
            else:
                base = Path(saves[0].rsplit(":", 1)[0] if saves and _has_q(saves[0]) else (saves[0] if saves else ROOT / "photopea" / "out.png"))
                dest = base.with_name(f"{base.stem}-{i + 1}{base.suffix or '.png'}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            written.append({"path": str(dest), "bytes": len(data)})
        echo = []
        for e in res.get("echo") or []:
            e = str(e)
            if e.startswith("PPLOG "):
                try:
                    echo += [str(x) for x in json.loads(e[6:])]
                    continue
                except ValueError:
                    pass
            echo.append(e)
        errs = [e[6:] for e in echo if e.startswith("PPERR ")]
        info_json = None
        for e in echo:
            if e.startswith("PPINFO "):
                try:
                    info_json = json.loads(e[7:])
                except ValueError:
                    info_json = e[7:]
        out = {"ok": bool(res.get("ok")) and not errs, "error": res.get("error") or ("; ".join(errs) or None),
               "saved": written, "echo": [e for e in echo if not e.startswith(("PPERR ", "PPINFO "))][:50]}
        if saves and len(written) < len(saves) and out["ok"]:
            out["ok"] = False
            out["error"] = f"asked to save {len(saves)} file(s) but Photopea returned {len(written)} (no open document?)"
        if info_json is not None:
            out["info"] = info_json
        return out


def _has_q(s: str) -> bool:
    name = Path(s).name
    return ":" in name and name.rsplit(":", 1)[1].replace(".", "").isdigit()


def _fmt(save: str) -> str:
    q = None
    if _has_q(save):
        save, q = save.rsplit(":", 1)
    ext = Path(save).suffix.lower().lstrip(".") or "png"
    ext = {"jpeg": "jpg", "tif": "tiff"}.get(ext, ext)
    if ext == "psd":
        return "psd"
    if ext in ("jpg", "webp"):
        return f"{ext}:{q or '0.92'}"
    return ext


_CLOSE_ALL = ("while(app.documents.length>0){try{app.activeDocument.close(SaveOptions.DONOTSAVECHANGES);}"
              "catch(e){app.activeDocument.close();}}")

_INFO = r"""(function(){function __n(x){if(x!==null&&x!==undefined&&x.value!==undefined)return Math.round(x.value);if(typeof x=='number')return Math.round(x);return Math.round(parseFloat(String(x)));}
function __w(ls,dp){var o=[];for(var i=0;i<ls.length;i++){var l=ls[i];var it={name:l.name,type:l.typename,visible:l.visible};
try{it.opacity=Math.round(l.opacity);}catch(e){}try{var __bb=l.bounds;it.bounds=[__n(__bb[0]),__n(__bb[1]),__n(__bb[2]),__n(__bb[3])];}catch(e){}
try{if(l.kind==LayerKind.TEXT){var t=l.textItem;it.text=t.contents;try{it.font=t.font;}catch(e){}try{it.size=__n(t.size);}catch(e){}
try{it.color=t.color.rgb.hexValue;}catch(e){}}}catch(e){}
if(l.typename=="LayerSet"&&dp<6){it.layers=__w(l.layers,dp+1);}o.push(it);}return o;}
var docs=[];for(var i=0;i<app.documents.length;i++){var dd=app.documents[i];
docs.push({name:dd.name,width:__n(dd.width),height:__n(dd.height),resolution:Math.round(dd.resolution||72),
active:dd==app.activeDocument,layers:__w(dd.layers,0)});}
app.echoToOE('PPINFO '+JSON.stringify(docs));})();"""


def _prelude(jid: str, place: list[str]) -> str:
    files = {Path(p).name: f"{BASE}/f/{jid}/{i}" for i, p in enumerate(place)}
    # Photopea's script engine is fragile: a helper with parameters named a..h, or one using `arguments`, made the
    # next artLayers.add() kill Photopea for good (no "done" ever). Hence the __ names. ppLog buffers; the script
    # echoes once at the end.
    return ("var __pplog=[];\nvar PP_FILES=" + json.dumps(files, ensure_ascii=False) + ";\n"
            "function ppPlace(__n){var __u=PP_FILES[__n];if(!__u)throw new Error('ppPlace: '+__n+' was not listed in place');"
            "app.open(__u,null,true);}\n"
            "function ppColor(__h){var __c=new SolidColor();__c.rgb.hexValue=String(__h).replace('#','');return __c;}\n"
            "function ppLog(__p1,__p2,__p3,__p4,__p5,__p6){var __r=[__p1,__p2,__p3,__p4,__p5,__p6],__o=[];"
            "for(var __i=0;__i<__r.length;__i++){var __v=__r[__i];if(__v===undefined||__v===null)continue;"
            "if(typeof __v!='string'&&__v.value!==undefined)__v=__v.value;__o.push(typeof __v=='string'?__v:JSON.stringify(__v));}__pplog.push(__o.join(' '));}\n")


def serve():
    CFG.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()


# ---------------------------------------------------------------- client

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # an http_proxy env must not catch 127.0.0.1


def _get(path: str, timeout=5):
    with _NOPROXY.open(BASE + path, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _post(path: str, data: dict, timeout=600):
    req = urllib.request.Request(BASE + path, json.dumps(data).encode(), {"Content-Type": "application/json"})
    with _NOPROXY.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def status():
    try:
        return _get("/status")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"bridge not running ({type(e).__name__})"}


def _browser() -> list[str] | None:
    for b in (os.environ.get("AIW_PP_BROWSER"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if b and shutil.which(b):
            return [shutil.which(b)]
    hits = sorted((HOME / ".cache").glob("ms-playwright/chromium-*/chrome-linux*/chrome"))
    return [str(hits[-1])] if hits else None


def _display() -> str:
    d = os.environ.get("DISPLAY")
    if d:
        return d
    for n in (0, 1):  # 104 runs :0, the cloud box Xvnc :1
        if Path(f"/tmp/.X11-unix/X{n}").exists():
            return f":{n}"
    return ":0"


def _allow_photopea_storage(profile: Path):
    """Photopea runs in an iframe of the bridge page, i.e. as a third-party site. Chromium blocks third-party
    storage by default, and Photopea then dies on `localStorage` before it ever says "done".
    Allow third-party cookies/storage in this private profile only."""
    pf = profile / "Default" / "Preferences"
    try:
        d = json.loads(pf.read_text()) if pf.is_file() else {}
    except ValueError:
        d = {}
    pr = d.setdefault("profile", {})
    pr["cookie_controls_mode"] = 0
    pr["block_third_party_cookies"] = False
    pr.setdefault("default_content_setting_values", {})["cookies"] = 1
    pr["exit_type"] = "Normal"
    d.setdefault("translate", {})["enabled"] = False
    d.setdefault("browser", {})["check_default_browser"] = False
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(d))


def ensure(wait: int = 120) -> dict:
    CFG.mkdir(parents=True, exist_ok=True)
    st = status()
    if st.get("ok") and st.get("ppv") != PPV:
        try:
            _post("/quit", {}, 5)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
        st = status()
    if not st.get("ok"):
        log = open(CFG / "bridge.log", "ab")
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)
        for _ in range(50):
            time.sleep(0.2)
            st = status()
            if st.get("ok"):
                break
        if not st.get("ok"):
            return {"ok": False, "error": "bridge server did not start; see ~/.config/aiw/photopea/bridge.log"}
        time.sleep(2.5)  # an open bridge window re-polls within a second or two
        st = status()
    seen = st.get("page_seen_s")
    if seen is None or seen > 30:
        br = _browser()
        if not br:
            return {"ok": False, "error": "no Chrome/Chromium on this computer: sudo apt-get install -y chromium"}
        env = dict(os.environ, DISPLAY=_display())
        args = br + [f"--user-data-dir={CFG / 'profile'}", "--no-first-run", "--no-default-browser-check",
                     "--password-store=basic", "--disable-features=Translate", "--disable-session-crashed-bubble",
                     "--start-maximized", f"--app={BASE}/"]
        if os.environ.get("AIW_PP_HEADLESS"):
            args.insert(1, "--headless=new")
        args += [a for a in os.environ.get("AIW_PP_BROWSER_ARGS", "").split() if a]
        _allow_photopea_storage(CFG / "profile")
        for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):  # a crashed earlier run leaves these
            try:
                (CFG / "profile" / f).unlink()
            except OSError:
                pass
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=open(CFG / "browser.log", "ab"),
                         stderr=subprocess.STDOUT, env=env, start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        st = status()
        if st.get("ready"):
            _raise_window()
            return {"ok": True, "ready": True, "waited_s": round(time.time() - t0, 1)}
        time.sleep(1)
    return {"ok": False, "error": f"Photopea did not finish loading within {wait} s "
                                  "(is www.photopea.com reachable from this computer?)", "status": status()}


def _raise_window():
    if shutil.which("wmctrl"):
        subprocess.run(["wmctrl", "-a", "Photopea (AI Workstation)"], env=dict(os.environ, DISPLAY=_display()),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _abs(p: str) -> str:
    p = str(p).strip()
    for pre in ("/data/home/workspace/", "/home/user/workspace/", "/home/aiw/workspace/", "home/aiw/workspace/", "~/workspace/", "$HOME/workspace/", "workspace/"):
        if p.startswith(pre):
            p = p[len(pre):]
    p = os.path.expanduser(p)
    return str(Path(p) if os.path.isabs(p) else ROOT / p)


def run(job: dict) -> dict:
    for k in ("open", "fonts", "place"):
        job[k] = [_abs(p) for p in job.get(k) or []]
        missing = [p for p in job[k] if not Path(p).is_file()]
        if missing:
            return {"ok": False, "error": f"file not found: {', '.join(missing)}"}
    job["save"] = [(_abs(s.rsplit(":", 1)[0]) + ":" + s.rsplit(":", 1)[1]) if _has_q(s) else _abs(s)
                   for s in job.get("save") or []]
    e = ensure()
    if not e.get("ok"):
        return e
    try:
        return _post("/job", job, int(job.get("timeout") or 90) * 8 + 120)
    except (urllib.error.URLError, OSError) as ex:
        return {"ok": False, "error": f"bridge error: {ex}"}


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "serve":
        serve()
        return 0
    if cmd == "ensure":
        out = ensure()
    elif cmd == "status":
        out = status()
    elif cmd == "stop":
        try:
            out = _post("/quit", {}, 5)
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": str(e)}
    elif cmd == "run":
        import base64
        if "--job-b64" in argv:
            job = json.loads(base64.b64decode(argv[argv.index("--job-b64") + 1]).decode())
        elif "--job" in argv:
            job = json.loads(Path(argv[argv.index("--job") + 1]).read_text())
        else:
            job = json.loads(sys.stdin.read())
        out = run(job)
    else:
        out = {"ok": False, "error": f"unknown command {cmd}"}
    print("PPRESULT " + json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
