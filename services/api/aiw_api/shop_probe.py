#!/usr/bin/env python3
"""京东/淘宝/天猫 price probe. Runs ON the AIW computer (stdlib only), driving the logged-in Chrome
(--remote-debugging-port=9222, profile ~/.config/aiw-chrome) over the DevTools protocol.

The API ships this file to ~/.config/aiw/shop/probe.py and runs one command per check:

  probe.py probe --url URL [--sku TEXT] [--shot]   open URL in a background tab, pick the spec, read price/stock
  probe.py order                                    read the open 确认订单 page (payable amount, address, items)
  probe.py submit                                   click 提交订单 on that page (only after the user confirmed)
  probe.py close-order                              close the 确认订单 tabs (cancel before an order exists)
  probe.py elem-at --x X --y Y                      what element sits under screen point X,Y (the confirm gate)
  probe.py url                                      the visible tab's URL

Every command prints one JSON object. Exit code 3 means Chrome's debugging port is not reachable.
No price logic lives here: the API parses the raw snapshot (shopwatch.parse_snapshot), so it is unit-tested.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlparse

PROBE_VERSION = "4"
CDP = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")
# DevTools is on 127.0.0.1: never through an http_proxy the desktop user may have set (it answers 404 / refuses)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ORDER_HOSTS = ("trade.jd.com", "buy.taobao.com", "buy.tmall.com", "h5.m.taobao.com/cart/order", "trade.m.jd.com")


# ----------------------------------------------------------------------------- tiny websocket client (RFC 6455)
class WS:
    def __init__(self, url: str, timeout: float = 15):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        # no Origin header: Chrome 111+ only checks --remote-allow-origins when one is sent
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket handshake refused: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self.ids = 0

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str, op: int = 1):
        data = text.encode() if isinstance(text, str) else text
        hdr = bytes([0x80 | op])
        n = len(data)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:          # ping
                self.send(data, op=10)
                continue
            if op == 8:
                raise ConnectionError("websocket closed by peer")
            parts += data
            if b1 & 0x80:
                return parts.decode(errors="replace")

    def call(self, method: str, params: dict | None = None, timeout: float = 20):
        self.ids += 1
        mid = self.ids
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def eval(self, expr: str, timeout: float = 20):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError("page script error: " + str(r["exceptionDetails"].get("text"))[:200])
        return (r.get("result") or {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def cdp_candidates(proc_root: str = "/proc") -> list[str]:
    """Where a DevTools port may be: the configured one, 9222, then any running chrome/chromium started with
    --remote-debugging-port (from /proc) or that wrote a DevToolsActivePort file. BrowserOS (9110) only when it is the
    configured one: a user's Chrome without a port is chrome_cdp.py's job (it reopens Chrome with the port)."""
    out = [CDP.rstrip("/"), "http://127.0.0.1:9222"]
    dirs = [os.path.expanduser(d) for d in ("~/.config/aiw-chrome", "~/.config/google-chrome", "~/.config/chromium")]
    try:
        pids = [x for x in os.listdir(proc_root) if x.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv or "chrom" not in os.path.basename(argv[0]).lower() or any(a.startswith("--type=") for a in argv):
            continue
        for a in argv:
            if a.startswith("--remote-debugging-port="):
                port = a.split("=", 1)[1]
                if port.isdigit() and port != "0":
                    out.append(f"http://127.0.0.1:{port}")
            elif a.startswith("--user-data-dir="):
                dirs.append(os.path.expanduser(a.split("=", 1)[1]))
    for d in dirs:
        try:
            with open(os.path.join(d, "DevToolsActivePort")) as f:
                port = f.readline().strip()
            if port.isdigit():
                out.append(f"http://127.0.0.1:{port}")
        except OSError:
            pass
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def profile_of(base: str, proc_root: str = "/proc") -> dict:
    """Which browser profile answers on this DevTools endpoint (3.8.2: the card says where the price was read, and a
    profile without cookies explains a login / captcha page)."""
    port = (urlparse(base).port or 0) if base else 0
    out = {"port": port, "dir": "", "cookies": None}
    try:
        pids = [x for x in os.listdir(proc_root) if x.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
                argv = [x.decode(errors="replace") for x in f.read().split(b"\0") if x]
        except OSError:
            continue
        if not argv or any(x.startswith("--type=") for x in argv):
            continue
        if f"--remote-debugging-port={port}" in argv:
            d = next((x.split("=", 1)[1] for x in argv if x.startswith("--user-data-dir=")), "")
            out["dir"] = d or "(default)"
            break
    if out["dir"] and out["dir"] != "(default)":
        ck = [os.path.join(os.path.expanduser(out["dir"]), "Default", n) for n in ("Cookies", os.path.join("Network", "Cookies"))]
        sizes = [os.path.getsize(c) for c in ck if os.path.exists(c)]
        out["cookies"] = max(sizes) if sizes else 0
    return out


def find_cdp() -> str:
    """Point CDP at the first answering endpoint. '' when found, else what each candidate said."""
    global CDP
    tried = []
    for base in cdp_candidates():
        try:
            with LOCAL.open(base + "/json/version", timeout=3) as r:   # noqa: S310 - local only
                if json.loads(r.read().decode()).get("webSocketDebuggerUrl"):
                    CDP = base
                    return ""
        except Exception as e:  # noqa: BLE001
            tried.append(f"{base.rsplit(':', 1)[-1]}: {e}")
    return "; ".join(tried)


def _http(path: str):
    with LOCAL.open(CDP + path, timeout=4) as r:   # noqa: S310 - local DevTools only
        return json.loads(r.read().decode())


def pages() -> list[dict]:
    return [p for p in _http("/json/list") if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]


def browser_ws() -> WS:
    return WS(_http("/json/version")["webSocketDebuggerUrl"])


CDP_TOOL = os.path.expanduser(os.environ.get("AIW_CHROME_CDP_TOOL", "~/.config/aiw/chrome_cdp.py"))


def show_on_screen(target_id: str) -> dict:
    """Put a tab the user should see (the 确认订单 page being submitted) in front of the screen: its tab selected and
    its window on top. Target.activateTarget alone is ignored by BrowserOS for its agent tabs, so this goes through
    chrome_cdp.py front (shipped next to this file by the API), which falls back to selecting the tab by keyboard.
    Best effort; never used by the background price probe."""
    import subprocess
    if not os.path.isfile(CDP_TOOL):
        return {"ok": False, "error": "chrome_cdp.py not on this computer"}
    try:
        port = urlparse(CDP).port or 9222
        r = subprocess.run([sys.executable, CDP_TOOL, "front", "--port", str(port), "--id", target_id],
                           capture_output=True, text=True, timeout=30)
        return json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}


def out(obj: dict, code: int = 0):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


# ----------------------------------------------------------------------------- page scripts
EXTRACT_JS = r"""
(() => {
  const vis = el => { if (!el || !el.getBoundingClientRect) return false; const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false; const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05; };
  const T = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const fs = el => { let m = parseFloat(getComputedStyle(el).fontSize) || 0;
    for (const c of el.querySelectorAll('*')) { const f = parseFloat(getComputedStyle(c).fontSize) || 0; if (f > m) m = f; }
    return m; };
  const cls = el => { const p = []; let e = el; for (let i = 0; i < 3 && e && e !== document.body; i++, e = e.parentElement)
    p.push(((e.id || '') + ' ' + (typeof e.className === 'string' ? e.className : '')).trim()); return p.join(' | ').slice(0, 200); };
  const ctx = el => { let e = el.parentElement; for (let i = 0; i < 3 && e; i++, e = e.parentElement) { const t = T(e);
    if (t.length > 4 && t.length < 90) return t; } return ''; };
  const SEL = [
    ['final', '#J_FinalPrice'], ['final', '.finalPrice'], ['final', '[class*="finalPrice"]'], ['final', '[class*="final-price"]'],
    ['final', '[class*="couponPrice"]'], ['final', '[class*="afterCoupon"]'], ['final', '[class*="estimatePrice"]'],
    ['main', '.p-price .price'], ['main', '.summary-price .p-price'], ['main', '.J-p-price'], ['main', '[class*="price-now"]'],
    ['main', '.tm-promo-price .tm-price'], ['main', '#J_PromoPrice .tm-price'], ['main', '#J_StrPriceModBox .tm-price'],
    ['main', '#J_PromoPriceNum'], ['main', '#J_StrPrice .tb-rmb-num'],
    ['main', '[class*="Price--priceText"]'], ['main', '[class*="highlightPrice"]'], ['final', '[class*="Price--extraPrice"]'],
    ['main', '[class*="priceText"]'], ['main', '[class*="PriceText"]'],
    // 京东: the classic summary price (<span class="price J-p-100012043978">), the 2024+ page, 秒杀, and PLUS
    ['main', 'span.price[class*="J-p-"]'], ['main', '.summary-price .price'], ['main', '#jd-price'],
    ['main', '#banner-miaosha .price'], ['main', '[class*="price_now"]'], ['main', '[class*="priceNow"]'],
    ['final', '.J-finalPrice'], ['final', '[class*="final-price-num"]'],
    ['plus', '.p-price-plus .price'], ['plus', '.J-p-plus'], ['plus', '[class*="plus-price"]'], ['plus', '[class*="plusPrice"]']
  ];
  const sel = [];
  for (const [role, q] of SEL) { let els = []; try { els = document.querySelectorAll(q); } catch (e) {}
    for (const el of els) { if (!vis(el)) continue; const t = T(el); if (!/\d/.test(t) || t.length > 60) continue;
      sel.push({role, q, t, ctx: ctx(el), fs: fs(el), y: Math.round(el.getBoundingClientRect().top + scrollY)}); if (sel.length > 30) break; } }
  const cand = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('span,em,strong,b,i,div,p,dd,dt,label,ins,del,s')) {
    if (cand.length > 80) break;
    const t = T(el); if (!t || t.length > 40 || !/[¥￥]\s*\d|\d+(\.\d{1,2})?\s*元/.test(t)) continue;
    if (el.children.length > 6 || !vis(el)) continue;
    const r = el.getBoundingClientRect(); const y = Math.round(r.top + scrollY); if (y > 2200) continue;
    const key = t + '@' + y; if (seen.has(key)) continue; seen.add(key);
    const st = getComputedStyle(el);
    cand.push({t, ctx: ctx(el), cls: cls(el), fs: fs(el), y, x: Math.round(r.left),
               strike: /line-through/.test(st.textDecorationLine || st.textDecoration || '') || /^(DEL|S)$/.test(el.tagName)});
  }
  const btn = [];
  for (const el of document.querySelectorAll('a,button,[role=button],div[class*="btn"],span[class*="btn"],div[class*="Btn"],div[class*="button"]')) {
    if (btn.length > 60) break; if (!vis(el)) continue; const t = T(el); if (!t || t.length > 12) continue;
    const c = (typeof el.className === 'string' ? el.className : '') + ' ' + ((el.parentElement && typeof el.parentElement.className === 'string') ? el.parentElement.className : '');
    btn.push({t, disabled: !!el.disabled || /disabled|disable|gray|grey/i.test(c) || el.getAttribute('aria-disabled') === 'true'});
  }
  const frames = [...document.querySelectorAll('iframe')].filter(vis).map(f => f.src || '').filter(Boolean).slice(0, 10);
  const pw = [...document.querySelectorAll('input[type=password]')].some(vis);
  let jd_sku = ''; try { jd_sku = String((window.pageConfig && pageConfig.product && pageConfig.product.skuid) || ''); } catch (e) {}
  const title = (document.querySelector('.sku-name, .itemInfo-wrap .sku-name, [class*="ItemTitle--mainTitle"], [class*="mainTitle"], .tb-main-title, .tb-detail-hd h1') || {}).innerText || document.title;
  return JSON.stringify({url: location.href, title: (title || '').trim().slice(0, 120), doc_title: document.title.slice(0, 120),
    text: (document.body ? document.body.innerText : '').slice(0, 6000), sel, cand, btn, frames, password_input: pw, jd_sku,
    ready: document.readyState});
})()
"""

PICK_SKU_JS = r"""
(tokens => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden'; };
  const AREA = '#choose-attrs, #choose-attr-1, #choose-attr-2, #choose-attr-3, .choose-attrs, [class*="skuWrap"], [class*="SkuContent"], [class*="skuCate"], .tb-sku, .tm-sku, .J_TSaleProp, [class*="sku"]';
  const items = 'div.item, li, a, span, div[class*="valueItem"], div[class*="skuItem"], div[class*="SkuItem"], [data-value], [data-sku]';
  const done = [];
  for (const tok of tokens) {
    let best = null, bl = 1e9;
    for (const area of document.querySelectorAll(AREA)) {
      for (const el of area.querySelectorAll(items)) {
        if (!vis(el)) continue; const t = (el.innerText || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
        if (!t || t.length > 60 || !t.includes(tok)) continue;
        if (t.length <= bl) { best = el; bl = t.length; }   // ties: the deeper element; its click bubbles up
      }
    }
    if (best) { const c = best;
      const sel = /selected|active|current/.test(c.className || '');
      if (!sel) c.click(); done.push({token: tok, text: (best.innerText || '').trim().slice(0, 40), was_selected: sel}); }
    else done.push({token: tok, missing: true});
  }
  return JSON.stringify(done);
})
"""

ORDER_JS = r"""
(() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1 && getComputedStyle(el).visibility !== 'hidden'; };
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const pay = [];
  for (const el of document.querySelectorAll('span,em,strong,b,div,p,dd,label')) {
    if (el.children.length > 6 || !vis(el)) continue; const t = T(el); if (!t || t.length > 50) continue;
    if (/应付总额|实付款|实付|应付金额|合计|需付款|总计|订单总额/.test(t) && /\d/.test(t)) pay.push(t);
    if (pay.length > 20) break;
  }
  const addr = [];
  for (const el of document.querySelectorAll('[class*="addr"], [class*="Addr"], [class*="address"], [class*="Address"], #consignee-addr, .consignee-item, .ui-switchable-panel-selected')) {
    if (!vis(el)) continue; const t = T(el); if (t.length > 6 && t.length < 200) addr.push(t); if (addr.length > 6) break;
  }
  const submit = [...document.querySelectorAll('a,button,[role=button],div,span')].filter(el => vis(el) && el.children.length < 4 &&
    /^(提交订单|提交订单并支付|确认订单|立即支付)$/.test(T(el))).map(el => T(el)).slice(0, 4);
  return JSON.stringify({url: location.href, title: document.title, pay, addr, submit, text: document.body.innerText.slice(0, 5000)});
})()
"""

SUBMIT_FIND_JS = r"""
(() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4 && getComputedStyle(el).visibility !== 'hidden'; };
  const T = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  let best = null;
  for (const el of document.querySelectorAll('a,button,[role=button],div,span')) {
    if (!vis(el) || el.children.length > 3) continue;
    if (/^(提交订单|提交订单并支付|确认订单)$/.test(T(el))) { if (!best || el.getBoundingClientRect().width * el.getBoundingClientRect().height <
        best.getBoundingClientRect().width * best.getBoundingClientRect().height) best = el; }
  }
  if (!best) return JSON.stringify({found: false});
  best.scrollIntoView({block: 'center'}); const r = best.getBoundingClientRect();
  return JSON.stringify({found: true, text: T(best), x: r.left + r.width / 2, y: r.top + r.height / 2});
})()
"""

ELEM_AT_JS = r"""
((sx, sy) => {
  const dpr = devicePixelRatio || 1;
  const x = sx / dpr - screenX - Math.max(0, outerWidth - innerWidth) / 2;
  const y = sy / dpr - screenY - Math.max(0, outerHeight - innerHeight);
  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return JSON.stringify({url: location.href, inside: false});
  let el = document.elementFromPoint(x, y); const parts = [];
  for (let i = 0; i < 4 && el && el !== document.body; i++, el = el.parentElement) {
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (t && t.length < 40) parts.push(t);
  }
  return JSON.stringify({url: location.href, inside: true, text: parts.join(' / ').slice(0, 160)});
})
"""


# ----------------------------------------------------------------------------- commands
def visible_page() -> dict:
    ps = pages()
    if not ps:
        raise RuntimeError("Chrome has no open tab")
    best = ps[0]
    for p in ps[:8]:
        try:
            ws = WS(p["webSocketDebuggerUrl"], 4)
            v = ws.eval("document.visibilityState + ':' + document.hasFocus()", 4)
            ws.close()
        except Exception:  # noqa: BLE001
            continue
        if v and v.startswith("visible"):
            best = p
            if v.endswith("true"):
                break
    return best


def is_order_url(url: str) -> bool:
    return any(h in (url or "") for h in ORDER_HOSTS)


def cmd_probe(a):
    bws = browser_ws()
    tid = bws.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
    try:
        ws = None
        for _ in range(20):
            ps = [p for p in pages() if p.get("id") == tid]
            if ps:
                ws = WS(ps[0]["webSocketDebuggerUrl"])
                break
            time.sleep(0.2)
        if ws is None:
            raise RuntimeError("background tab did not appear")
        ws.call("Page.enable")
        try:   # a background tab otherwise looks unfocused/hidden: some prices render only for a focused page
            ws.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, 5)
        except Exception:  # noqa: BLE001
            pass
        ws.call("Page.navigate", {"url": a.url}, 30)
        end, snap = time.time() + float(a.wait), {}
        picked, last_sig = None, None
        while time.time() < end:
            time.sleep(1.2)
            try:
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
            except Exception:  # noqa: BLE001 - navigating; retry
                continue
            # prices render late (and timers run slow in a background tab): act only once two reads agree
            sig = json.dumps([x.get("t") for x in (snap.get("sel") or []) + (snap.get("cand") or [])], ensure_ascii=False)
            stable, last_sig = sig == last_sig, sig
            if not stable and time.time() < end - 3:
                host = urlparse(snap.get("url") or "").hostname or ""
                if not any(k in host for k in ("passport.jd.com", "login.taobao.com", "login.tmall.com", "login.m.jd.com")):
                    continue
            if snap.get("ready") == "complete" and (snap.get("sel") or snap.get("cand")) and picked is None and a.sku:
                toks = [t for t in a.sku.replace("，", " ").replace(",", " ").replace("/", " ").split() if t]
                try:
                    picked = json.loads(ws.eval(f"({PICK_SKU_JS})({json.dumps(toks, ensure_ascii=False)})") or "[]")
                except Exception as e:  # noqa: BLE001
                    picked = [{"error": str(e)[:120]}]
                time.sleep(2.5)
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
                break
            if snap.get("ready") == "complete" and (snap.get("sel") or len(snap.get("cand") or []) >= 2) and not a.sku:
                time.sleep(1.5)   # coupons / 到手价 load after the main price
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
                break
            host = urlparse(snap.get("url") or "").hostname or ""
            if any(k in host for k in ("passport.jd.com", "login.taobao.com", "login.tmall.com", "login.m.jd.com")):
                break
        snap["sku_picked"] = picked
        if a.shot:
            try:
                bws.call("Target.activateTarget", {"targetId": tid})
                time.sleep(0.8)
                snap["screenshot"] = ws.call("Page.captureScreenshot", {"format": "jpeg", "quality": 70}, 20).get("data")
            except Exception as e:  # noqa: BLE001
                snap["shot_error"] = str(e)[:160]
        ws.close()
    finally:
        try:
            bws.call("Target.closeTarget", {"targetId": tid})
        except Exception:  # noqa: BLE001
            pass
        bws.close()
    snap["probe_version"] = PROBE_VERSION
    snap["cdp"] = CDP
    try:
        snap["profile"] = profile_of(CDP)
    except Exception:  # noqa: BLE001
        pass
    out(snap)


def order_pages() -> list[dict]:
    return [p for p in pages() if is_order_url(p.get("url", ""))]


def cmd_order(_a):
    ps = order_pages()
    if not ps:
        out({"found": False, "tabs": [p.get("url", "")[:120] for p in pages()[:8]]})
    ws = WS(ps[0]["webSocketDebuggerUrl"])
    snap = json.loads(ws.eval(ORDER_JS) or "{}")
    ws.close()
    snap["found"] = True
    out(snap)


def cmd_submit(_a):
    ps = order_pages()
    if not ps:
        out({"ok": False, "error": "没有打开的确认订单页"})
    p = ps[0]
    bws = browser_ws()
    bws.call("Target.activateTarget", {"targetId": p["id"]})
    bws.close()
    shown = show_on_screen(p["id"])   # the user is watching this one: the order being placed is on screen
    time.sleep(0.8)
    ws = WS(p["webSocketDebuggerUrl"])
    info = json.loads(ws.eval(SUBMIT_FIND_JS) or "{}")
    if not info.get("found"):
        ws.close()
        out({"ok": False, "error": "页面上没有找到「提交订单」按钮"})
    time.sleep(0.4)
    info = json.loads(ws.eval(SUBMIT_FIND_JS) or "{}")
    for typ in ("mouseMoved", "mousePressed", "mouseReleased"):   # a trusted click, like a real mouse
        ws.call("Input.dispatchMouseEvent", {"type": typ, "x": info["x"], "y": info["y"], "button": "left", "clickCount": 1})
        time.sleep(0.06)
    time.sleep(4)
    url = ""
    try:
        url = ws.eval("location.href", 5)
    except Exception:  # noqa: BLE001 - page navigated away; read it from the tab list
        pass
    ws.close()
    if not url:
        url = next((x.get("url") for x in pages() if x.get("id") == p["id"]), "")
    out({"ok": True, "clicked": info.get("text"), "url": url, "still_order_page": is_order_url(url),
         "shown": bool(shown.get("verified"))})


def cmd_close_order(_a):
    closed = []
    bws = browser_ws()
    for p in order_pages():
        try:
            bws.call("Target.closeTarget", {"targetId": p["id"]})
            closed.append(p.get("url", "")[:120])
        except Exception:  # noqa: BLE001
            pass
    bws.close()
    out({"ok": True, "closed": closed})


def cmd_elem_at(a):
    p = visible_page()
    ws = WS(p["webSocketDebuggerUrl"])
    info = json.loads(ws.eval(f"({ELEM_AT_JS})({float(a.x)}, {float(a.y)})") or "{}")
    ws.close()
    out(info)


def cmd_url(_a):
    p = visible_page()
    out({"url": p.get("url", ""), "title": p.get("title", "")})


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--url", required=True)
    p.add_argument("--sku", default="")
    p.add_argument("--shot", action="store_true")
    p.add_argument("--wait", default="18")
    sub.add_parser("order")
    sub.add_parser("submit")
    sub.add_parser("close-order")
    e = sub.add_parser("elem-at")
    e.add_argument("--x", required=True)
    e.add_argument("--y", required=True)
    sub.add_parser("url")
    sub.add_parser("version")
    a = ap.parse_args()
    if a.cmd == "version":
        out({"version": PROBE_VERSION})
    why = find_cdp()
    if why:
        out({"error": f"NO_CDP: {why}"}, 3)
    try:
        {"probe": cmd_probe, "order": cmd_order, "submit": cmd_submit, "close-order": cmd_close_order,
         "elem-at": cmd_elem_at, "url": cmd_url}[a.cmd](a)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        out({"error": f"{type(e).__name__}: {e}"}, 1)


if __name__ == "__main__":
    main()
