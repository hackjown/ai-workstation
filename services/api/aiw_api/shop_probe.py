#!/usr/bin/env python3
"""京东/淘宝/天猫 price probe. Runs ON the AIW computer (stdlib only), driving the logged-in Chrome
(--remote-debugging-port=9222, profile ~/.config/aiw-chrome) over the DevTools protocol.

The API ships this file to ~/.config/aiw/shop/probe.py and runs one command per check:

  probe.py probe --url URL [--sku TEXT] [--shot] [--progress FILE]   open URL in a background tab, pick the spec, read price/stock
  probe.py order                                    read the open 确认订单 page (payable amount, address, items)
  probe.py submit                                   click 提交订单 on that page (only after the user confirmed)
  probe.py close-order                              close the 确认订单 tabs (cancel before an order exists)
  probe.py elem-at --x X --y Y                      what element sits under screen point X,Y (the confirm gate)
  probe.py url                                      the visible tab's URL
  probe.py profile                                  which Chrome profile answers and whether it is logged in to JD

v6 (2026-10-29): the item tab is brought to the front when it stays hidden without a price (a background tab never
runs requestAnimationFrame / IntersectionObserver, so JD's client-rendered price module may never appear), waits with a
MutationObserver (up to ~20 s, scrolling), reads open shadow roots and same-origin iframes, the price-keyword text
blocks near the top (到手价 / 券后 / 京东价), JSON embedded in the page for this sku, and the page's OWN price API
responses (Network domain; the page signs them itself, so h5st is no problem), then item.m.jd.com with the same
cookies (ignored when it is the limit.html page). Every stage is timed (snap.stages) and written to --progress for the
server to stream; a failed read carries a screenshot and a diagnosis (snap.diag).

v5 (2026-10-29): JD links are normalized to https://item.jd.com/<sku>.html; the probe waits for the JD price block
(scrolling a little), falls back to JD's own price APIs fetched inside the page (the logged-in cookies apply),
flags JD's 活动火爆 risk page (trade.m.jd.com/common/limit.html), and reports profile.dir / profile.logged_in
from the DevTools cookie store (pt_key / thor) instead of guessing from the Cookies file.

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
import re
import time
import urllib.request
from urllib.parse import urlparse

PROBE_VERSION = "6"
CDP = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")
# DevTools is on 127.0.0.1: never through an http_proxy the desktop user may have set (it answers 404 / refuses)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ORDER_HOSTS = ("trade.jd.com", "buy.taobao.com", "buy.tmall.com", "h5.m.taobao.com/cart/order", "trade.m.jd.com")

# ----------------------------------------------------------------------------- pure helpers (unit-tested)
_JD_SKU = (r"item\.jd\.com/(\d+)\.html", r"item\.m\.jd\.com/product/(\d+)\.html", r"item\.m\.jd\.com/(\d+)\.html",
           r"(?:^|\.)m\.jd\.com/product/(\d+)\.html", r"jd\.com/product/(\d+)\.html",
           r"item\.m\.jd\.com/ware/view\.action\?(?:.*&)?wareId=(\d+)", r"jd\.(?:com|hk)/.*?[?&](?:sku|skuId|wareId)=(\d+)")
RISK_TEXT = ("活动火爆", "请选购其他商品")
JD_LOGIN_COOKIES = ("pt_key", "thor")          # pt_pin alone (the account name) stays after a logout: not a session
JD_ID_COOKIES = ("pt_key", "pt_pin", "thor")


def jd_sku_of(url: str) -> str:
    """The JD sku in a desktop, mobile or share link ('' when it is not a JD item link)."""
    u = (url or "").strip()
    host = (urlparse(u).hostname or "").lower()
    if not (host == "jd.com" or host.endswith((".jd.com", ".jd.hk"))):
        return ""
    for pat in _JD_SKU:
        m = re.search(pat, u)
        if m:
            return m.group(1)
    return ""


def normalize_jd(url: str) -> str:
    """item.m.jd.com/product/<sku>.html?utm_user=... (and other JD item links) -> https://item.jd.com/<sku>.html."""
    sku = jd_sku_of(url)
    return f"https://item.jd.com/{sku}.html" if sku else url


def is_risk_limit(url: str, text: str = "") -> bool:
    """JD's risk-control page (活动火爆 请选购其他商品 我知道了): not a captcha, nothing the user can solve there."""
    return "/common/limit.html" in (url or "") or any(t in (text or "")[:3000] for t in RISK_TEXT)


def jd_login_state(cookies: list, now: float | None = None) -> dict:
    """Cookies from Storage.getCookies / Network.getAllCookies -> whether this profile is logged in to JD.
    Only names and counts are reported, never a cookie value."""
    now = time.time() if now is None else now
    jd, names = [], set()
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if not (dom == "jd.com" or dom.endswith(".jd.com")):
            continue
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and 0 < exp < now:
            continue
        jd.append(c)
        if c.get("name") in JD_ID_COOKIES and c.get("value"):
            names.add(c["name"])
    return {"logged_in": any(n in names for n in JD_LOGIN_COOKIES), "jd_cookies": len(jd),
            "login_cookies": sorted(names), "cookies_total": len([c for c in cookies or [] if isinstance(c, dict)])}


def _jsonp(text: str):
    t = (text or "").strip()
    if not t:
        return None
    if t[0] not in "{[":
        m = re.match(r"^[\w$.]+\((.*)\)\s*;?\s*$", t, re.S)
        if not m:
            return None
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        return None


def _num(v):
    if isinstance(v, dict):
        v = v.get("price") if v.get("price") is not None else v.get("value")
    try:
        f = float(str(v).replace(",", "").replace("¥", "").replace("￥", "").strip())
    except (TypeError, ValueError):
        return None
    return f if 0 < f < 10_000_000 else None


def _walk(o, depth=0):
    if depth > 12:
        return
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v, depth + 1)
    elif isinstance(o, list):
        for v in o[:200]:
            yield from _walk(v, depth + 1)


_FINAL_KEYS = ("finalPrice", "estimatedPrice", "estimatePrice", "afterCouponPrice", "couponAfterPrice", "toHandPrice",
               "daoShouPrice", "preferencePrice", "promotionPrice", "finalPriceNum")
_FINAL_WORD = re.compile(r"到手|券后|用券|领券后|预估|折后|优惠后")
_PLUS_WORD = re.compile(r"plus|PLUS|会员")


def parse_api_price(body) -> dict | None:
    """A JD price API response (pc_detailpage_wareBusiness, item-soa getWareBusiness, p.3.cn prices/mgets; JSON or
    JSONP) -> {price, final_price, plus_price, in_stock}; None when it holds no price. final_price is the price after
    coupons / 到手价 (finalPrice, estimatedPrice, a {price, 到手价} pair inside couponInfo / preferenceVO ...);
    PLUS-only prices go to plus_price and are never the price we judge by."""
    data = _jsonp(body) if isinstance(body, str) else body
    if data is None:
        return None
    price, finals, plus, stock = None, [], [], None
    for d in _walk(data):
        if price is None and "p" in d and any(k in d for k in ("op", "m", "id", "l", "tpp")):
            price = _num(d.get("p"))
            if _num(d.get("tpp")):
                plus.append(_num(d.get("tpp")))
        for k in ("plusPrice", "tpp"):
            if _num(d.get(k)):
                plus.append(_num(d.get(k)))
        words = " ".join(str(v) for v in d.values() if isinstance(v, str))[:300]
        for k in _FINAL_KEYS:
            if d.get(k) not in (None, "", [], {}):
                v = _num(d.get(k))
                if v:
                    sub = d.get(k)
                    sw = " ".join(str(x) for x in sub.values() if isinstance(x, str)) if isinstance(sub, dict) else ""
                    (plus if _PLUS_WORD.search(sw) else finals).append(v)
        if "price" in d and not isinstance(d.get("price"), (dict, list)) and _FINAL_WORD.search(words):
            v = _num(d.get("price"))
            if v:
                (plus if _PLUS_WORD.search(words) else finals).append(v)
        if stock is None:
            st = d.get("StockState", d.get("stockState"))
            if isinstance(st, (int, str)) and str(st).isdigit():
                stock = int(st) in (33, 39, 40)
            elif isinstance(d.get("stockDesc"), str):
                stock = None if not d["stockDesc"] else not re.search(r"无货|售罄|缺货", d["stockDesc"])
    if price is None:
        for d in _walk(data):     # {"price": {"p": ...}} handled above; a bare {"price": "199.00"} without labels
            if not _FINAL_WORD.search(" ".join(str(v) for v in d.values() if isinstance(v, str))) and _num(d.get("jdPrice")):
                price = _num(d.get("jdPrice"))
                break
    final = None
    ok = [f for f in finals if price is None or price * 0.3 <= f <= price + 1e-9]
    if ok:
        final = min(ok)
    if price is None and final is None:
        return None
    return {"price": price, "final_price": final,
            "plus_price": min(plus) if plus else None, "in_stock": stock}


def api_urls(sku: str) -> list[tuple[str, str]]:
    body = json.dumps({"skuId": sku, "num": 1, "area": "1_72_2799_0"}, separators=(",", ":"))
    from urllib.parse import quote
    return [("pc_detailpage_wareBusiness", "https://api.m.jd.com/?appid=pc-item-soa&functionId=pc_detailpage_wareBusiness"
             f"&client=pc&clientVersion=1.0.0&t={int(time.time() * 1000)}&body={quote(body)}"),
            ("getWareBusiness", f"https://item-soa.jd.com/getWareBusiness?skuId={sku}&num=1&area=1_72_2799_0"),
            ("p.3.cn", f"https://p.3.cn/prices/mgets?skuIds=J_{sku}")]


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
        self.on_event = None      # v6: DevTools events (Network.responseReceived) seen while waiting for answers

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
            if "method" in msg and "id" not in msg:
                if self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception:  # noqa: BLE001
                        pass
                continue
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


def _argv(proc_root: str, pid: str) -> list[str]:
    """A process's argv. Chrome rewrites its own cmdline (setproctitle) into one space-joined string padded with NULs,
    so a single element holding ' --' is split again; v4 missed --remote-debugging-port there (profile dir '')."""
    with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
        raw = [x.decode(errors="replace") for x in f.read().split(b"\0") if x.strip()]
    out = []
    for x in raw:
        out += x.split() if " --" in x else [x]
    return out


_DEFAULT_DIRS = (("google-chrome-beta", "~/.config/google-chrome-beta"), ("google-chrome", "~/.config/google-chrome"),
                 ("chromium", "~/.config/chromium"), ("browseros", "~/.config/browser-os"), ("chrome", "~/.config/google-chrome"))


def profile_of(base: str, proc_root: str = "/proc", home_dirs: tuple | None = None) -> dict:
    """Which browser profile answers on this DevTools endpoint (3.8.2: the card says where the price was read, and a
    profile without cookies explains a login / captcha page). dir: the --user-data-dir of the process listening with
    that port (or the binary's default dir), else the dir whose DevToolsActivePort names the port. cookies: size of
    the profile's Cookies file (0 = a fresh profile). logged_in is added by the probe from the DevTools cookie store."""
    port = (urlparse(base).port or 0) if base else 0
    out = {"port": port, "dir": "", "cookies": None}
    try:
        pids = [x for x in os.listdir(proc_root) if x.isdigit()]
    except OSError:
        pids = []
    seen_dirs = []
    for pid in pids:
        try:
            argv = _argv(proc_root, pid)
        except OSError:
            continue
        if not argv or any(x.startswith("--type=") for x in argv):
            continue
        d = next((x.split("=", 1)[1] for x in argv if x.startswith("--user-data-dir=")), "")
        if d:
            seen_dirs.append(os.path.expanduser(d))
        has_port = f"--remote-debugging-port={port}" in argv or any(
            argv[i] == "--remote-debugging-port" and i + 1 < len(argv) and argv[i + 1] == str(port) for i in range(len(argv)))
        if port and has_port:
            if not d:
                exe = os.path.basename(argv[0]).lower()
                d = next((os.path.expanduser(v) for k, v in _DEFAULT_DIRS if k in exe), "(default)")
            out["dir"] = d
            break
    if not out["dir"] and port:
        dirs = list(home_dirs) if home_dirs is not None else [os.path.expanduser(x) for x in (
            "~/.config/aiw-chrome", "~/.config/google-chrome", "~/.config/chromium", "~/.config/browser-os")]
        for d in seen_dirs + dirs:
            try:
                with open(os.path.join(d, "DevToolsActivePort")) as f:
                    if f.readline().strip() == str(port):
                        out["dir"] = d
                        break
            except OSError:
                pass
    if out["dir"] and out["dir"] != "(default)":
        ck = [os.path.join(os.path.expanduser(out["dir"]), "Default", n) for n in ("Cookies", os.path.join("Network", "Cookies"))]
        sizes = [os.path.getsize(c) for c in ck if os.path.exists(c)]
        out["cookies"] = max(sizes) if sizes else 0
    return out


def read_cookies(bws, ws=None) -> list:
    """Every cookie of the browser's default context (Storage.getCookies on the browser target), else
    Network.getAllCookies on a page. None when neither answers."""
    try:
        return bws.call("Storage.getCookies", {}, 8).get("cookies") or []
    except Exception:  # noqa: BLE001
        pass
    if ws is not None:
        try:
            return ws.call("Network.getAllCookies", {}, 8).get("cookies") or []
        except Exception:  # noqa: BLE001
            pass
    return None


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
  // v6: open shadow roots and same-origin iframes too (a price module may live in either)
  const ROOTS = [document];
  try { for (const el of document.querySelectorAll('*')) { if (el.shadowRoot && ROOTS.length < 40) ROOTS.push(el.shadowRoot); } } catch (e) {}
  for (const f of document.querySelectorAll('iframe')) { try { if (f.contentDocument && ROOTS.length < 50) ROOTS.push(f.contentDocument); } catch (e) {} }
  const ALL = q => { const o = []; for (const r of ROOTS) { try { o.push(...r.querySelectorAll(q)); } catch (e) {} } return o; };
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
    ['plus', '.p-price-plus .price'], ['plus', '.J-p-plus'], ['plus', '[class*="plus-price"]'], ['plus', '[class*="plusPrice"]'],
    // 京东 2025+ item page (v5): the price module, 券后/到手 blocks, data-price attributes
    ['final', '[class*="coupon-price"]'], ['final', '[class*="couponAfter"]'], ['final', '[class*="after-coupon"]'],
    ['final', '[class*="daoshou"]'], ['final', '[class*="toHand"]'],
    ['main', '#J_StrPriceModule .price'], ['main', '.itemInfo-wrap .p-price .price'], ['main', '.itemInfo-wrap .summary-price .price'],
    ['main', '#J_summary .p-price .price'], ['main', '[data-price]']
  ];
  // broad fallbacks, only when none of the above matched (they also catch labels and PLUS blocks)
  const BROAD = ['#J_StrPriceModule [class*="price"]', '#J_summary [class*="price"]', '.product-intro [class*="price"]',
                 '.itemInfo-wrap [class*="price"]', '[class*="summary-price"] [class*="price"]'];
  const isPlus = el => /plus/i.test(cls(el));
  const sel = [];
  const add = (role, q, el, t) => { if (role !== 'plus' && role !== 'final' && isPlus(el)) role = 'plus';
    sel.push({role, q, t, ctx: ctx(el), fs: fs(el), y: Math.round(el.getBoundingClientRect().top + scrollY)}); };
  for (const [role, q] of SEL) { const els = ALL(q);
    for (const el of els) { if (!vis(el)) continue; let t = T(el);
      if (q === '[data-price]' && !/\d/.test(t)) t = el.getAttribute('data-price') || '';
      if (!/\d/.test(t) || t.length > 60) continue;
      add(role, q, el, t); if (sel.length > 30) break; } }
  if (!sel.some(x => x.role === 'main' || x.role === 'final')) {
    for (const q of BROAD) { const els = ALL(q);
      for (const el of els) { if (!vis(el) || el.children.length > 3) continue; const t = T(el);
        if (!/\d/.test(t) || t.length > 30) continue; add('any', q, el, t); if (sel.length > 40) break; } }
  }
  const cand = [];
  const seen = new Set();
  for (const el of ALL('span,em,strong,b,i,div,p,dd,dt,label,ins,del,s')) {
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
  if (!jd_sku) { const m = location.href.match(/item\.(?:m\.)?jd\.com\/(?:product\/)?(\d+)\.html/); if (m) jd_sku = m[1]; }
  const tEl = document.querySelector('.sku-name, .itemInfo-wrap .sku-name, [class*="ItemTitle--mainTitle"], [class*="mainTitle"], .tb-main-title, .tb-detail-hd h1, [class*="sku-title"], [class*="skuName"], [class*="goods-name"], [class*="itemName"]');
  const title = (tEl && tEl.innerText) || document.title;
  // v6: text blocks with a price keyword, with where they sit (the regex runs on the server, unit-tested)
  const kw = []; const KW = /到手价|到手|券后|京东价|秒杀价|促销价|售价|价格|登录查看|登录后查看|PLUS|[¥￥]\s*\d/;
  for (const r of ROOTS) { if (kw.length > 40) break; let tw; try { tw = document.createTreeWalker(r.body || r, NodeFilter.SHOW_TEXT); } catch (e) { continue; }
    let n, k = 0; while ((n = tw.nextNode()) && kw.length <= 40 && k++ < 20000) { const v = (n.nodeValue || '').trim(); if (!v || v.length > 80 || !KW.test(v)) continue;
      let e = n.parentElement, blk = ''; for (let i = 0; i < 4 && e; i++, e = e.parentElement) { const t = T(e); if (t.length > 120) break; blk = t; if (/\d/.test(t) && /[¥￥]|价/.test(t)) break; }
      if (!blk || !n.parentElement || !vis(n.parentElement)) continue; const rr = n.parentElement.getBoundingClientRect(); const y = Math.round(rr.top + scrollY);
      if (kw.some(x => x.t === blk && x.y === y)) continue; kw.push({t: blk, y, x: Math.round(rr.left), cls: cls(n.parentElement).slice(0, 80)}); } }
  // v6: JSON the page carries for this sku (pageConfig, __INITIAL_STATE__, __NEXT_DATA__ ...)
  const embedded = [];
  try { const pc = window.pageConfig && pageConfig.product; if (pc) { const o = {}; for (const k of Object.keys(pc)) { const v = pc[k];
      if (/price|sku|name|stock/i.test(k) && (v == null || typeof v !== 'object' || /price/i.test(k))) o[k] = v; }
    embedded.push({src: 'pageConfig.product', body: JSON.stringify(o).slice(0, 4000)}); } } catch (e) {}
  for (const g of ['__INITIAL_STATE__', '__NEXT_DATA__', '__NUXT__', '__INITIAL_DATA__', '__PRELOADED_STATE__', '__APOLLO_STATE__', 'pageData', '_itemInfo', '__DATA__']) {
    try { const root = window[g]; if (!root || typeof root !== 'object' || !jd_sku) continue; const hits = [], seen = new Set(); let steps = 0;
      const walk = (o, d) => { if (!o || typeof o !== 'object' || d > 10 || seen.has(o) || hits.length > 4 || steps++ > 30000) return; seen.add(o);
        const id = String(o.skuId ?? o.skuid ?? o.sku ?? o.wareId ?? o.id ?? '');
        if (id === jd_sku && Object.keys(o).some(k => /price/i.test(k))) { hits.push(o); return; }
        for (const k in o) { try { walk(o[k], d + 1); } catch (e) {} } };
      walk(root, 0); for (const h of hits) embedded.push({src: g, body: JSON.stringify(h).slice(0, 8000)}); } catch (e) {} }
  return JSON.stringify({url: location.href, title: (title || '').trim().slice(0, 120), doc_title: document.title.slice(0, 120),
    title_from: tEl && tEl.innerText ? 'element' : 'document.title',
    text: (document.body ? document.body.innerText : '').slice(0, 6000), sel, cand, btn, frames, password_input: pw, jd_sku,
    kw, embedded, ready: document.readyState, visibility: document.visibilityState, focus: document.hasFocus(),
    n_elems: document.getElementsByTagName('*').length, shadow_roots: ROOTS.length - 1, scroll_y: Math.round(scrollY)});
})()
"""

# one JD price API call from inside the item page: same site cookies (the logged-in session) apply. p.3.cn has no
# CORS for item.jd.com, so it falls back to JSONP.
API_JS = r"""
(async (src, url) => {
  const one = {src};
  const jsonp = () => new Promise((ok, no) => { const cb = '__aiw_p' + Date.now(); const s = document.createElement('script');
    const t = setTimeout(() => { delete window[cb]; s.remove(); no(new Error('jsonp timeout')); }, 6000);
    window[cb] = d => { clearTimeout(t); delete window[cb]; s.remove(); ok(JSON.stringify(d)); };
    s.onerror = () => { clearTimeout(t); s.remove(); no(new Error('jsonp load error')); };
    s.src = url + (url.includes('?') ? '&' : '?') + 'callback=' + cb; (document.head || document.documentElement).appendChild(s); });
  try {
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 6000);
    const r = await fetch(url, {credentials: 'include', signal: ctl.signal}); clearTimeout(tm);
    one.status = r.status; one.body = (await r.text()).slice(0, 40000);
  } catch (e) {
    one.error = String(e).slice(0, 160);
    if (src === 'p.3.cn') { try { one.body = await jsonp(); one.jsonp = true; } catch (e2) { one.error += ' / ' + String(e2).slice(0, 100); } }
  }
  return JSON.stringify(one);
})
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
    return any(h in (url or "") for h in ORDER_HOSTS) and "/common/limit.html" not in (url or "")


LOGIN_HOST_KEYS = ("passport.jd.com", "login.taobao.com", "login.tmall.com", "login.m.jd.com", "plogin.m.jd.com")


_PRICE_T = re.compile(r"[¥￥]\s*\d|^\s*\d{1,7}(?:\.\d{1,2})?\s*(?:起|元)?\s*$")
_NOT_T = re.compile(r"满\s*\d|减\s*\d|运费|\d+\s*期|积分|评价|条|关注|万\+|人")
_KW_LABEL = re.compile(r"(到手价?|券后价?|京东价|秒杀价|促销价|售价)\s*[:：]?\s*[¥￥]\s*\d|[¥￥]\s*\d[\d.,]*\s*(到手价?|券后价?)")


def has_jd_price(snap: dict) -> bool:
    """v6: a price-looking text in a JD price element (v5 took any digit, so a stray number skipped every fallback)."""
    return any(x.get("role") in ("main", "final", "any") and _PRICE_T.search(str(x.get("t") or ""))
               and not _NOT_T.search(str(x.get("t") or "")) for x in snap.get("sel") or [])


def kw_price(snap: dict) -> bool:
    """A labelled price (到手价 ¥137 / 京东价 ¥187) in a text block near the top of the page."""
    return any(_KW_LABEL.search(str(k.get("t") or "")) and int(k.get("y") or 0) < 1600 for k in snap.get("kw") or [])


STAGE_CN = {"connect": "连接浏览器", "open": "打开商品页", "wait": "等待价格渲染", "front": "切到前台渲染", "read": "读取价格",
            "net": "读取页面自己的价格接口", "api": "调用价格接口", "shot": "保存诊断截图", "mobile": "试移动版商品页",
            "done": "完成"}


class Stages:
    """Timed stages of one probe run. With a path, the current stage is written there (atomically) so the API can
    stream it to the card while the probe runs (shopwatch.run_probe polls it)."""

    def __init__(self, path: str = ""):
        self.t0, self.list, self.path = time.time(), [], path or ""

    def __call__(self, key, **extra):
        now_ms = int((time.time() - self.t0) * 1000)
        if self.list and "ms" not in self.list[-1]:
            self.list[-1]["ms"] = max(0, now_ms - self.list[-1]["at_ms"])
        if key is None:
            return
        self.list.append({"stage": key, "label": STAGE_CN.get(key, key), "at_ms": now_ms, **extra})
        self._write()

    def _write(self):
        if not self.path:
            return
        try:
            cur = self.list[-1] if self.list else {}
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(json.dumps({"stage": cur.get("stage"), "label": cur.get("label"), "stages": self.list}, ensure_ascii=False))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def finish(self):
        self(None)
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


WAIT_JS = r"""
(ms => new Promise(ok => {
  const Q = '#J_FinalPrice, .p-price .price, span.price[class*="J-p-"], .summary-price .price, [class*="finalPrice"], [class*="price_now"], [class*="priceNow"], [class*="coupon-price"]';
  const has = () => { try { const e = document.querySelector(Q); return !!(e && /\d/.test(e.textContent || '')); } catch (x) { return false; } };
  if (has()) return ok('has');
  let done = false, mo = null;
  const fin = v => { if (done) return; done = true; try { mo && mo.disconnect(); } catch (x) {} ok(v); };
  try { mo = new MutationObserver(() => { if (has()) fin('has'); });
        mo.observe(document.documentElement || document, {childList: true, subtree: true, characterData: true}); } catch (x) {}
  setTimeout(() => fin('timeout'), ms);
}))
"""

PRICE_NET_RE = re.compile(r"api\.m\.jd\.com|item-soa\.jd\.com|p\.3\.cn|pe\.3\.cn|cd\.jd\.com|[/?&]price", re.I)
NET_SKIP = re.compile(r"recommend|similar|guess|hotsale|comment|shopInfo|pc_shop|getLegoWareDetail|club", re.I)
NET_PRIO = re.compile(r"warebusiness|price|getWareBusiness|pc_item", re.I)
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.5 Mobile/15E148 Safari/604.1")


def net_name(u: str) -> str:
    m = re.search(r"functionId=([\w.\-]+)", u or "")
    if m:
        return m.group(1)
    p = urlparse(u or "")
    return ((p.hostname or "") + p.path)[:80]


def net_prices(ws, captured: dict, sku: str) -> tuple[list, dict | None]:
    """The page's OWN price API responses (seen on the Network domain while it loaded; the page signed them itself)."""
    tries, items = [], list(captured.items())
    items.sort(key=lambda kv: 0 if NET_PRIO.search(net_name(kv[1].get("url", ""))) else 1)
    for rid, info in items[:12]:
        name = net_name(info.get("url", ""))
        ent = {"src": "page:" + name, "status": info.get("status")}
        tries.append(ent)
        if NET_SKIP.search(name):
            ent["skipped"] = True
            continue
        try:
            b = ws.call("Network.getResponseBody", {"requestId": rid}, 6)
            text = b.get("body") or ""
            if b.get("base64Encoded"):
                text = base64.b64decode(text).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            ent["error"] = f"{type(e).__name__}: {e}"[:120]
            continue
        ent["size"] = len(text)
        got = parse_api_price(text) if text else None
        ent["price"] = bool(got)
        if got and (got.get("price") or got.get("final_price")) and (sku in text or NET_PRIO.search(name)):
            return tries, {**got, "src": ent["src"]}
        if not got:
            ent["head"] = re.sub(r"\s+", " ", text[:160])
    return tries, None


def prev_visible(exclude: str) -> str:
    """The tab the user had in front (to give it back after the probe tab was brought forward)."""
    try:
        for p in pages()[:8]:
            if p.get("id") == exclude:
                continue
            ws = WS(p["webSocketDebuggerUrl"], 4)
            try:
                v = ws.eval("document.visibilityState", 4)
            finally:
                ws.close()
            if v == "visible":
                return p.get("id") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def bring_front(ws, bws, tid: str) -> dict:
    out = {"prev": prev_visible(tid)}
    for how, fn in (("Page.bringToFront", lambda: ws.call("Page.bringToFront", {}, 5)),
                    ("Target.activateTarget", lambda: bws.call("Target.activateTarget", {"targetId": tid}, 5))):
        try:
            fn()
            out.setdefault("how", how)
        except Exception as e:  # noqa: BLE001
            out[how] = str(e)[:80]
    return out


def mobile_read(ws, sku: str, wait: float = 12) -> dict:
    """item.m.jd.com/product/<sku>.html in the same tab (same cookies, iPhone UA). Its limit.html is not used."""
    url = f"https://item.m.jd.com/product/{sku}.html"
    res = {"asked_url": url}
    try:
        try:
            ws.call("Emulation.setUserAgentOverride", {"userAgent": IPHONE_UA}, 5)
            ws.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True}, 5)
        except Exception:  # noqa: BLE001
            pass
        ws.call("Page.navigate", {"url": url}, 20)
        end, snap = time.time() + wait, {}
        while time.time() < end:
            time.sleep(1.0)
            try:
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
            except Exception:  # noqa: BLE001
                continue
            if is_risk_limit(snap.get("url") or "", snap.get("text") or ""):
                return {**res, "url": snap.get("url"), "risk_limit": True, "skipped": "limit.html"}
            if any(k in (urlparse(snap.get("url") or "").hostname or "") for k in LOGIN_HOST_KEYS):
                return {**res, "url": snap.get("url"), "login": True, "skipped": "login page"}
            if snap.get("ready") == "complete" and (has_jd_price(snap) or kw_price(snap) or len(snap.get("cand") or []) >= 1):
                time.sleep(1.0)
                try:
                    snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
                except Exception:  # noqa: BLE001
                    pass
                break
        keep = {k: snap.get(k) for k in ("url", "title", "doc_title", "sel", "cand", "btn", "kw", "embedded", "ready",
                                         "visibility", "jd_sku", "frames", "password_input")}
        keep["text"] = (snap.get("text") or "")[:3000]
        return {**res, **keep}
    except Exception as e:  # noqa: BLE001
        return {**res, "error": f"{type(e).__name__}: {e}"[:160]}
    finally:
        for m, p in (("Emulation.setUserAgentOverride", {"userAgent": ""}), ("Emulation.clearDeviceMetricsOverride", {})):
            try:
                ws.call(m, p, 5)
            except Exception:  # noqa: BLE001
                pass


def snippets(snap: dict) -> list[str]:
    """Short visible-text snippets around price keywords (for the diagnosis)."""
    out = [str(k.get("t"))[:120] + f" @y{k.get('y')}" for k in (snap.get("kw") or [])[:10]]
    if not out:
        t = snap.get("text") or ""
        for m in re.finditer(r"到手价|券后|京东价|[¥￥]\s*\d|登录查看", t):
            out.append(t[max(0, m.start() - 30):m.end() + 30].replace("\n", " "))
            if len(out) >= 6:
                break
    return out


def jd_api_price(ws, sku: str) -> dict:
    """JD's own price APIs, tried in order from inside the item page; the first that yields a price wins."""
    tries = []
    for src, url in api_urls(sku):
        try:
            one = json.loads(ws.eval(f"({API_JS})({json.dumps(src)}, {json.dumps(url)})", 12) or "{}")
        except Exception as e:  # noqa: BLE001
            one = {"src": src, "error": f"{type(e).__name__}: {e}"[:160]}
        got = parse_api_price(one.get("body") or "") if one.get("body") else None
        tries.append({k: (v[:300] if k == "body" and isinstance(v, str) else v) for k, v in one.items()})
        if got and (got.get("price") or got.get("final_price")):
            tries[-1]["body"] = (one.get("body") or "")[:20000]
            return {"api": tries, "api_price": {**got, "src": src}}
    return {"api": tries}


def profile_dir_from_version_page(ws) -> str:
    """chrome://version names the profile path: the last resort for profile.dir when /proc did not say."""
    try:
        ws.call("Page.navigate", {"url": "chrome://version"}, 10)
        time.sleep(0.8)
        pth = (ws.eval("(document.querySelector('#profile_path') || {}).innerText || ''", 5) or "").strip()
        return os.path.dirname(pth) if pth else ""
    except Exception:  # noqa: BLE001
        return ""


def cmd_probe(a):
    st = Stages(getattr(a, "progress", "") or "")
    st("connect")
    url = normalize_jd(a.url)
    sku = jd_sku_of(url)
    jd = bool(sku)
    prof = {}
    try:
        prof = profile_of(CDP)
    except Exception:  # noqa: BLE001
        prof = {"port": urlparse(CDP).port or 0, "dir": "", "cookies": None}
    front_mode = os.environ.get("AIW_SHOP_FRONT", "1") != "0"
    fronted, snap, captured, prev = None, {}, {}, ""
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

        def on_event(m):
            if m.get("method") == "Network.responseReceived" and len(captured) < 40:
                resp = (m.get("params") or {}).get("response") or {}
                u = resp.get("url") or ""
                if PRICE_NET_RE.search(u):
                    captured[m["params"].get("requestId")] = {"url": u, "status": resp.get("status")}
        ws.on_event = on_event
        ws.call("Page.enable")
        try:   # v6: the page's own (signed) price API answers, read back if nothing renders
            ws.call("Network.enable", {"maxTotalBufferSize": 20_000_000, "maxResourceBufferSize": 5_000_000}, 5)
        except Exception:  # noqa: BLE001
            pass
        try:   # a background tab otherwise looks unfocused/hidden: some prices render only for a focused page
            ws.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, 5)
        except Exception:  # noqa: BLE001
            pass
        if not prof.get("dir"):
            prof["dir"] = profile_dir_from_version_page(ws)
            if prof["dir"]:
                prof["dir_from"] = "chrome://version"
        st("open")
        ws.call("Page.navigate", {"url": url}, 30)
        st("wait")
        t_wait = time.time()
        end = t_wait + float(a.wait)
        picked, last_sig, i = None, None, 0
        while time.time() < end:
            try:   # v6: returns as soon as a price element appears (MutationObserver), else after 1.5 s
                ws.eval(f"({WAIT_JS})(1500)", 5)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
            i += 1
            try:
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
            except Exception:  # noqa: BLE001 - navigating; retry
                time.sleep(0.9)
                continue
            if is_risk_limit(snap.get("url") or "", snap.get("text") or ""):
                snap["risk_limit"] = True     # 活动火爆: nothing renders later, stop here
                break
            host = urlparse(snap.get("url") or "").hostname or ""
            if any(k in host for k in LOGIN_HOST_KEYS):
                break
            priced = has_jd_price(snap) or kw_price(snap)
            if jd and not priced and front_mode and fronted is None and time.time() - t_wait > 5 \
                    and snap.get("visibility") != "visible":
                # v6: a hidden background tab runs no requestAnimationFrame / IntersectionObserver: JD's
                # client-rendered price module may never appear there
                st("front")
                fronted = bring_front(ws, bws, tid)
                prev = fronted.get("prev") or ""
                st("wait")
                continue
            if jd and not priced and i % 3 == 0:
                try:   # JD renders the price module lazily: nudge it with a scroll, then back to the top
                    ws.eval("window.scrollBy(0, 600); setTimeout(() => window.scrollTo(0, 0), 600); 1", 5)
                except Exception:  # noqa: BLE001
                    pass
            # prices render late (and timers run slow in a background tab): act only once two reads agree
            sig = json.dumps([x.get("t") for x in (snap.get("sel") or []) + (snap.get("cand") or [])], ensure_ascii=False)
            stable, last_sig = sig == last_sig, sig
            if not stable and time.time() < end - 3:
                continue
            complete = snap.get("ready") == "complete"
            if jd:   # a JD item page is done only when its own price is there
                found = complete and priced
            else:
                found = complete and (snap.get("sel") or len(snap.get("cand") or []) >= 2)
            if found and picked is None and a.sku:
                st("read")
                toks = [t for t in a.sku.replace("，", " ").replace(",", " ").replace("/", " ").split() if t]
                try:
                    picked = json.loads(ws.eval(f"({PICK_SKU_JS})({json.dumps(toks, ensure_ascii=False)})") or "[]")
                except Exception as e:  # noqa: BLE001
                    picked = [{"error": str(e)[:120]}]
                time.sleep(2.5)
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
                break
            if found and not a.sku:
                st("read")
                time.sleep(1.5)   # coupons / 到手价 load after the main price
                snap = json.loads(ws.eval(EXTRACT_JS) or "{}")
                break
        snap["sku_picked"] = picked
        snap["fronted"] = fronted
        host = urlparse(snap.get("url") or "").hostname or ""
        page_ok = not snap.get("risk_limit") and not any(k in host for k in LOGIN_HOST_KEYS)
        if jd:
            snap["jd_sku"] = snap.get("jd_sku") or sku
            if page_ok and not has_jd_price(snap) and not kw_price(snap):
                st("net")
                snap["net"], got = net_prices(ws, captured, snap["jd_sku"])
                if got:
                    snap["api_price"] = got
                elif "jd.com" in (snap.get("url") or url):
                    st("api")
                    snap.update(jd_api_price(ws, snap["jd_sku"]))
        priced = has_jd_price(snap) or kw_price(snap) or bool(snap.get("api_price")) if jd else \
            bool(snap.get("sel") or len(snap.get("cand") or []) >= 1)
        need_diag = not page_ok or not priced
        if a.shot or need_diag:
            st("shot")
            try:   # a background tab produces no frames: bring it forward for the picture
                if not fronted:
                    prev = prev or prev_visible(tid)
                    bws.call("Target.activateTarget", {"targetId": tid}, 5)
                time.sleep(0.8)
                snap["screenshot"] = ws.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}, 20).get("data")
            except Exception as e:  # noqa: BLE001
                snap["shot_error"] = str(e)[:160]
        if jd and page_ok and not priced:
            st("mobile")
            snap["mobile"] = mobile_read(ws, snap["jd_sku"])
        cookies = read_cookies(bws, ws)
        if cookies is not None:
            stl = jd_login_state(cookies)
            prof.update(logged_in=stl["logged_in"] if (jd or stl["jd_cookies"]) else None, jd_cookies=stl["jd_cookies"],
                        login_cookies=stl["login_cookies"], cookies_total=stl["cookies_total"])
            if prof.get("cookies") is None:
                prof["cookies"] = stl["cookies_total"]
        snap["snippets"] = snippets(snap)
        snap["net_seen"] = len(captured)
        ws.close()
    finally:
        try:
            bws.call("Target.closeTarget", {"targetId": tid})
        except Exception:  # noqa: BLE001
            pass
        if prev and prev != tid:   # give the user's tab back
            try:
                bws.call("Target.activateTarget", {"targetId": prev}, 5)
            except Exception:  # noqa: BLE001
                pass
        bws.close()
        st("done")
        st.finish()
    snap["stages"] = st.list
    snap["probe_version"] = PROBE_VERSION
    snap["cdp"] = CDP
    snap["asked_url"] = url
    snap["profile"] = prof
    out(snap)


def cmd_profile(_a):
    """Which profile answers on the DevTools port and whether it is logged in to JD (no page is opened)."""
    prof = profile_of(CDP)
    bws = browser_ws()
    try:
        cookies = read_cookies(bws)
    finally:
        bws.close()
    if cookies is not None:
        prof.update(jd_login_state(cookies))
    out({"probe_version": PROBE_VERSION, "cdp": CDP, "profile": prof})


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
    p.add_argument("--wait", default="20")
    p.add_argument("--progress", default="")
    sub.add_parser("order")
    sub.add_parser("submit")
    sub.add_parser("close-order")
    e = sub.add_parser("elem-at")
    e.add_argument("--x", required=True)
    e.add_argument("--y", required=True)
    sub.add_parser("url")
    sub.add_parser("profile")
    sub.add_parser("version")
    a = ap.parse_args()
    if a.cmd == "version":
        out({"version": PROBE_VERSION})
    why = find_cdp()
    if why:
        out({"error": f"NO_CDP: {why}"}, 3)
    try:
        {"probe": cmd_probe, "order": cmd_order, "submit": cmd_submit, "close-order": cmd_close_order,
         "elem-at": cmd_elem_at, "url": cmd_url, "profile": cmd_profile}[a.cmd](a)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        out({"error": f"{type(e).__name__}: {e}"}, 1)


if __name__ == "__main__":
    main()
