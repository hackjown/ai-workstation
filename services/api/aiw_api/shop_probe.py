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

v8 (2026-10-30): one time budget per run (--budget, default 115 s; the server's hard cap is 160 s) with short
per-stage timeouts (no socket read blocks past its call's timeout) and a SIGALRM watchdog that stops the running stage
and still prints the partial snapshot with stage_error. The render wait is at most 15 s and stops after ~6 s on a
skeleton (only JD's site-wide title, few elements, no price module); the signed API then runs first, the page's own
answers next (bodies read at Network.loadingFinished, a dropped body is 'gone', not an error), the unsigned APIs only
with time left. The screenshot takes ≤5 s and is skipped / dropped when a price was found; cart and 我的关注 run only
while time remains. The product name comes from JD's data (wname/skuName, cart / 关注 rows), never the generic title.

v7 (2026-10-29): on ai-linux-001 v6 ended on trade.m.jd.com/common/limit.html?module=detail_m1 (the MOBILE item page's
limiter) with a logged-in desktop profile (thor). v7 never loads the mobile item page: short links (3.cn, u.jd.com,
union-click) are resolved to the sku over plain HTTP first (stopping before item.m.jd.com), the tab gets a desktop
user agent when the browser's looks mobile (item.jd.com 302s mobile UAs to item.m.jd.com), navigator.webdriver is
patched before any page script, the item page is opened as a link from www.jd.com (referrer), and every Document
request to item.m.jd.com or limit.html is refused (Fetch domain) and recorded with the redirect chain. When the item
page is limited, redirected or shows no price, the price is read once by another route, in order: JD's own signed
pc_detailpage_wareBusiness (PSign / h5st made by JD's SDK in the page), the cart page (cart.jd.com/cart_index) and
the 我的关注 list (t.jd.com/follow/product), both read-only. --route alt skips the item page (the server's cool-down
after a limit page). The v6 item.m.jd.com fallback is gone: it was the page being limited.

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

PROBE_VERSION = "8"

# v8: one time budget for the whole probe run (the server's hard cap is 160 s; the probe aims to answer by ~120 s).
# Each stage takes what it needs out of what is left; the cart / 我的关注 routes run only while time remains, and a
# watchdog (SIGALRM at budget + 12 s) stops whatever is running so the partial result is still printed.
DEFAULT_BUDGET = 115.0
BUDGET = {"end": 0.0, "total": 0.0}


class OutOfTime(BaseException):
    """Raised by the watchdog (a BaseException so no `except Exception` on the way swallows it)."""


def budget_left() -> float:
    return max(0.0, BUDGET["end"] - time.time()) if BUDGET.get("end") else 1e9


def cap(seconds: float, reserve: float = 0.0) -> float:
    """seconds, but never more than what is left of the budget (minus reserve)."""
    return max(0.2, min(float(seconds), budget_left() - reserve))
CDP = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")
# DevTools is on 127.0.0.1: never through an http_proxy the desktop user may have set (it answers 404 / refuses)
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ORDER_HOSTS = ("trade.jd.com", "buy.taobao.com", "buy.tmall.com", "h5.m.taobao.com/cart/order", "trade.m.jd.com")

# ----------------------------------------------------------------------------- pure helpers (unit-tested)
_JD_SKU = (r"item\.jd\.com/(\d+)\.html", r"item\.m\.jd\.com/product/(\d+)\.html", r"item\.m\.jd\.com/(\d+)\.html",
           r"(?:^|\.)m\.jd\.com/product/(\d+)\.html", r"jd\.com/product/(\d+)\.html",
           r"item\.m\.jd\.com/ware/view\.action\?(?:.*&)?wareId=(\d+)", r"jd\.(?:com|hk)/.*?[?&](?:sku|skuId|wareId)=(\d+)",
           r"(?:wqitem|wq)\.jd\.com/item/view\?(?:.*&)?sku=(\d+)")
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


# v8: JD's site-wide title. A product page that never rendered (a skeleton / soft block) only has this as its
# document.title; it must never become a watch's product name.
GENERIC_TITLE = re.compile(r"^\s*(京东\s*[(（]?\s*JD\.COM\s*[)）]?|JD\.COM|京东|京东商城|京东全球购|京东超市)\s*"
                           r"([-–—_|·]\s*(正品低价|多快好省|品质保障|配送及时|轻松购物|专业综合网上购物商城).*)?\s*[!！]?\s*$|"
                           r"正品低价、品质保障、配送及时、轻松购物|多快好省，购物上京东", re.I)
_NAME_KEYS = ("skuName", "wname", "wareName", "skuTitle", "goodsName", "itemName", "productName", "name", "title")
_NAME_SKIP = re.compile(r"^(满\s*\d|领券|优惠|券|PLUS|plus|京东价|到手价|券后|有货|无货|自营|官方|店铺|促销|赠品)|^[\d\s.¥￥,]+$")


def is_generic_title(t: str) -> bool:
    t = (t or "").strip()
    return not t or bool(GENERIC_TITLE.search(t))


def clean_name(t: str) -> str:
    """A product name as shown on a card: the page-title suffix (【行情 报价 价格 评测】-京东) cut off."""
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    t = re.sub(r"\s*【[^】]*(行情|报价|价格|评测)[^】]*】.*$", "", t)
    t = re.sub(r"\s*[-–—_|]\s*(京东|JD\.COM|京东商城)\s*$", "", t, flags=re.I).strip()
    return "" if is_generic_title(t) else t[:120]


def name_of(body, sku: str = "") -> str:
    """v8: the product name in a JD API answer (wareBusiness / cart / follow JSON): skuName, wname, wareName ...
    Prefers a dict that also names this sku."""
    data = _jsonp(body) if isinstance(body, str) else body
    if data is None:
        return ""
    best = ""
    for d in _walk(data):
        mine = bool(sku) and any(str(d.get(k) or "") == str(sku) for k in ("skuId", "skuid", "id", "sku", "wareId"))
        for k in _NAME_KEYS:
            v = d.get(k)
            if not isinstance(v, str) or not (6 <= len(v.strip()) <= 200) or _NAME_SKIP.search(v.strip()):
                continue
            if k in ("name", "title") and not mine:
                continue      # 'name' is everywhere (coupons, shops): only next to this sku
            v = clean_name(v)
            if v:
                if mine:
                    return v
                best = best or v
    return best


def api_urls(sku: str) -> list[tuple[str, str]]:
    body = json.dumps({"skuId": sku, "num": 1, "area": "1_72_2799_0"}, separators=(",", ":"))
    from urllib.parse import quote
    return [("pc_detailpage_wareBusiness", "https://api.m.jd.com/?appid=pc-item-soa&functionId=pc_detailpage_wareBusiness"
             f"&client=pc&clientVersion=1.0.0&t={int(time.time() * 1000)}&body={quote(body)}"),
            ("getWareBusiness", f"https://item-soa.jd.com/getWareBusiness?skuId={sku}&num=1&area=1_72_2799_0"),
            ("p.3.cn", f"https://p.3.cn/prices/mgets?skuIds=J_{sku}")]


# ----------------------------------------------------------------------------- v7: links, user agent, routes
JD_SHORT_HOSTS = ("3.cn", "u.jd.com", "union-click.jd.com", "jd.cn", "m.jd.com", "jingfen.jd.com", "prodev.m.jd.com")
# the mobile item page and its limiter: never loaded by the probe (module=detail_m1 is what JD rate-limits)
MOBILE_DOC_PATTERNS = ("*://item.m.jd.com/*", "*://trade.m.jd.com/common/limit.html*", "*://wqitem.jd.com/*",
                       "*://wq.jd.com/item/*")
_MOBILE_UA = re.compile(r"iphone|ipad|ipod|android|mobile|\bios\b|windows phone|harmonyos", re.I)
JD_HOME = "https://www.jd.com/"
CART_URL = "https://cart.jd.com/cart_index"
FOLLOW_URL = "https://t.jd.com/follow/product"


def is_jd_link(url: str) -> bool:
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in JD_SHORT_HOSTS or host == "jd.com" or host.endswith((".jd.com", ".3.cn"))


def is_mobile_item_url(url: str) -> bool:
    """item.m.jd.com / wqitem / the limit page: the mobile item flow JD limits (module=detail_m1)."""
    u = (url or "").lower()
    host = urlparse(u).hostname or ""
    return host in ("item.m.jd.com", "wqitem.jd.com") or "/common/limit.html" in u or (host == "wq.jd.com" and "/item/" in u)


def mobile_ua(ua: str) -> bool:
    return bool(_MOBILE_UA.search(ua or ""))


def desktop_ua(ua: str) -> str:
    """A desktop Chrome UA with the same Chrome version (item.jd.com 302s any mobile UA to item.m.jd.com)."""
    m = re.search(r"(?:Chrome|CriOS)/(\d+)", ua or "")
    major = m.group(1) if m else "130"
    return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


_HOP_RE = (re.compile(r"""\bhrl\s*=\s*['"]([^'"]+)['"]"""), re.compile(r"""location(?:\.href)?\s*=\s*['"](https?:[^'"]+)['"]"""),
           re.compile(r"""location\.replace\(\s*['"](https?:[^'"]+)['"]"""),
           re.compile(r"""<meta[^>]+http-equiv=["']?refresh[^>]+url=([^"'>\s]+)""", re.I))


def next_hop(base: str, body: str) -> str:
    """The URL a JD jump page (u.jd.com / union-click: var hrl='...'; a meta refresh) sends the browser to."""
    from urllib.parse import urljoin
    for rx in _HOP_RE:
        m = rx.search(body or "")
        if m:
            return urljoin(base, m.group(1).replace("&amp;", "&"))
    m = re.search(r"item\.jd\.com/(\d+)\.html|item\.m\.jd\.com/product/(\d+)\.html", body or "")
    if m:
        return f"https://item.jd.com/{m.group(1) or m.group(2)}.html"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # noqa: D401 - we follow by hand (and stop before item.m.jd.com)
        return None


def _http_hop(url: str, timeout: float = 8) -> tuple[int, str, str]:
    """One GET without following redirects: (status, Location, body head). Desktop UA, no cookies."""
    op = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": desktop_ua(""), "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with op.open(req, timeout=timeout) as r:   # noqa: S310 - JD short links only
            return r.status, r.headers.get("Location") or "", r.read(60000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location") or "", ""


def resolve_jd_link(url: str, hop=None, hops: int = 6) -> dict:
    """3.cn / u.jd.com / union-click / other JD links without a sku -> the sku, over plain HTTP (no browser, no
    cookies). Stops at the first URL that names the sku, so item.m.jd.com itself is never fetched."""
    from urllib.parse import urljoin
    hop = hop or _http_hop
    chain, cur = [], (url or "").strip()
    for _ in range(hops):
        sku = jd_sku_of(cur)
        if sku:
            return {"sku": sku, "url": f"https://item.jd.com/{sku}.html", "chain": chain}
        if not cur.startswith("http"):
            break
        try:
            status, loc, body = hop(cur)
        except Exception as e:  # noqa: BLE001
            chain.append({"url": cur[:200], "error": f"{type(e).__name__}: {e}"[:120]})
            return {"sku": "", "url": url, "chain": chain, "error": chain[-1]["error"]}
        chain.append({"url": cur[:200], "status": status})
        nxt = urljoin(cur, loc) if loc else next_hop(cur, body)
        if not nxt or nxt == cur:
            break
        cur = nxt
    sku = jd_sku_of(cur)
    if sku:
        return {"sku": sku, "url": f"https://item.jd.com/{sku}.html", "chain": chain}
    return {"sku": "", "url": url, "chain": chain, "error": "no sku in the link"}


def jd_area(cookie_str: str) -> str:
    """ipLoc-djd=1-72-2799-0 (the user's delivery area) -> 1_72_2799_0; Beijing when absent."""
    m = re.search(r"(?:^|;\s*)ipLoc-djd=([\d\-_.]+)", cookie_str or "")
    if not m:
        return "1_72_2799_0"
    parts = re.split(r"[-_.]", m.group(1))[:4]
    return "_".join(parts + ["0"] * (4 - len(parts)))


RISK_CODES = re.compile(r'"code"\s*:\s*"?(601|605|403|-1|1)"?|"echo"\s*:|risk|verify|h5st', re.I)


def api_risk(body: str) -> str:
    """Why a JD API answer carries no data: a risk / signature refusal ('' when it looks like data)."""
    b = (body or "")[:600]
    if not b or parse_api_price(body):
        return ""
    m = re.search(r'"(?:echo|message|msg)"\s*:\s*"([^"]{1,80})"', b)
    if RISK_CODES.search(b):
        return (m.group(1) if m else b[:80]).strip()
    return ""


def items_for_sku(body, sku: str) -> list:
    """Objects in a cart / follow-list API answer that are this sku (skuId / id / wareId), at most 5."""
    data = _jsonp(body) if isinstance(body, str) else body
    out = []
    for d in _walk(data):
        ids = {str(d.get(k)) for k in ("skuId", "skuid", "sku", "id", "wareId", "itemId", "productId") if d.get(k) is not None}
        if sku in ids and any(re.search(r"price|Price", k) for k in d):
            out.append(d)
            if len(out) >= 5:
                break
    return out


def price_of_items(body, sku: str) -> dict | None:
    """The price of this sku inside a cart / follow-list API answer (other items in the list are ignored)."""
    for d in items_for_sku(body, sku):
        got = parse_api_price(d)
        p = _num(d.get("price")) or _num(d.get("jdPrice")) or _num(d.get("salePrice"))
        if not got:
            got = {"price": p, "final_price": None, "plus_price": None, "in_stock": None} if p else None
        elif got.get("price") is None and p:
            # a cart row: {"skuId", "price": "187.00", "finalPrice": {...到手价}} -> the row's own price is the price
            got = dict(got, price=p)
            if got.get("final_price") and not (p * 0.3 <= got["final_price"] <= p + 1e-9):
                got["final_price"] = None
        if got and (got.get("price") or got.get("final_price")):
            return got
    return None


# ----------------------------------------------------------------------------- tiny websocket client (RFC 6455)
class WS:
    def __init__(self, url: str, timeout: float = 15):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        self.base_timeout = timeout
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
        self.pending = {}         # v8: id -> callback for request() (response bodies read at loadingFinished)
        self.bodies = {}          # v8: requestId -> response body text, read the moment loading finished

    def _frame(self):
        """v8: one whole frame from the buffer, or None (nothing is consumed until the frame is complete, so a socket
        timeout in the middle of a frame never corrupts the stream)."""
        b = self.buf
        if len(b) < 2:
            return None
        b1, b2 = b[0], b[1]
        n, at = b2 & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, at = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, at = struct.unpack(">Q", b[2:10])[0], 10
        mask = None
        if b2 & 0x80:
            if len(b) < at + 4:
                return None
            mask, at = b[at:at + 4], at + 4
        if len(b) < at + n:
            return None
        data, self.buf = b[at:at + n], b[at + n:]
        if mask:
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
        return b1, data

    def _more(self, wait: float | None = None):
        self.sock.settimeout(self.base_timeout if wait is None else max(0.05, wait))
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("websocket closed")
        self.buf += chunk

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

    def recv(self, wait: float | None = None) -> str:
        """One message. wait: seconds before socket.timeout (None = the socket's own timeout)."""
        end = None if wait is None else time.time() + wait
        parts = b""
        while True:
            fr = self._frame()
            if fr is None:
                self._more(None if end is None else end - time.time())
                continue
            b1, data = fr
            op = b1 & 0x0F
            if op == 9:          # ping
                self.send(data, op=10)
                continue
            if op == 8:
                raise ConnectionError("websocket closed by peer")
            parts += data
            if b1 & 0x80:
                return parts.decode(errors="replace")

    def _dispatch(self, msg: dict, mid: int | None = None) -> bool:
        """True when msg answers mid. Events go to on_event, answers to fire-and-forget requests to their callback."""
        if "method" in msg and "id" not in msg:
            if self.on_event:
                try:
                    self.on_event(msg)
                except Exception:  # noqa: BLE001
                    pass
            return False
        if mid is not None and msg.get("id") == mid:
            return True
        cb = self.pending.pop(msg.get("id"), None)
        if cb:
            try:
                cb(msg)
            except Exception:  # noqa: BLE001
                pass
        return False

    def call(self, method: str, params: dict | None = None, timeout: float = 20):
        self.ids += 1
        mid = self.ids
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        timeout = max(0.2, min(timeout, budget_left() + 2)) if BUDGET.get("end") else timeout
        end = time.time() + timeout
        while time.time() < end:
            try:   # v8: never block past the call's own timeout (a slow page used to hold the socket 15 s per read)
                msg = json.loads(self.recv(end - time.time()))
            except socket.timeout:
                break
            if self._dispatch(msg, mid):
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def request(self, method: str, params: dict | None, cb) -> None:
        """v8: send now, answer to cb whenever it arrives (inside a later call()/pump())."""
        self.ids += 1
        self.pending[self.ids] = cb
        self.send(json.dumps({"id": self.ids, "method": method, "params": params or {}}))

    def pump(self, seconds: float = 0.3) -> None:
        """Read whatever arrives for a moment (events, answers to request())."""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            try:
                self._dispatch(json.loads(self.recv(end - time.time())))
            except (socket.timeout, ValueError):
                break

    def notify(self, method: str, params: dict | None = None):
        """v7: a command whose answer we do not wait for (Fetch.failRequest from inside an event handler: a nested
        call() would swallow the answer the outer call is waiting for)."""
        self.ids += 1
        self.send(json.dumps({"id": self.ids, "method": method, "params": params or {}}))

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

# v7: before any page script runs: a browser started for automation reports navigator.webdriver = true
STEALTH_JS = r"""
try { if (navigator.webdriver) Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => false, configurable: true}); } catch (e) {}
"""

ENV_JS = "JSON.stringify({ua: navigator.userAgent, webdriver: navigator.webdriver === true, platform: navigator.platform || ''})"

# v7: JD's own signed price API, from any desktop jd.com page (item page, cart): JD's SDK (ParamsSign -> h5st,
# SHA256, getJsToken) is the one the item page uses (appId fb5df, pc-item-soa / pc_detailpage_wareBusiness); it is
# loaded from JD's CDN when the page has not got it. The logged-in cookies apply (credentials: include).
SIGNED_JS = r"""
(async (sku, area) => {
  const one = {src: 'signed:pc_detailpage_wareBusiness', origin: location.origin};
  const load = src => new Promise(ok => { const s = document.createElement('script'); s.src = src; s.onload = () => ok(true);
    s.onerror = () => ok(false); (document.head || document.documentElement).appendChild(s); setTimeout(() => ok(false), 8000); });
  try {
    if (!window.ParamsSign && !window.PSign) one.sdk_loaded = await load('https://storage.360buyimg.com/webcontainer/js_security_v3_0.1.8.js');
    if (!window.SHA256) await load('https://storage.360buyimg.com/retail-mall/lib/sha256-new.js');
    if (!window.getJsToken) await load('https://gias.jd.com/js/pc-tk.js');
    const signer = window.PSign || (window.ParamsSign ? new window.ParamsSign({appId: 'fb5df', preRequest: false, debug: false,
      onSign() {}, onRequestTokenRemotely() {}, onRequestToken() {}}) : null);
    if (!signer || !window.SHA256) { one.error = 'no JD signing SDK on ' + location.host; return JSON.stringify(one); }
    const body = {skuId: Number(sku) || sku, area, num: 1, bbTraffic: '', giftServiceIsSelected: '', customInfoId: ''};
    const t = Date.now(); const bs = JSON.stringify(body);
    const sig = await signer.sign({appid: 'pc-item-soa', functionId: 'pc_detailpage_wareBusiness', client: 'pc',
                                   clientVersion: '1.0.0', t, body: window.SHA256(bs)});
    const tok = await new Promise(ok => { try { window.getJsToken(r => ok((r && r.jsToken) || ''), 200); } catch (e) { ok(''); }
                                          setTimeout(() => ok(''), 3000); });
    const ck = (document.cookie.match(/(?:^|;\s*)__jda=([^;]+)/) || [])[1] || '';
    const q = new URLSearchParams({appid: 'pc-item-soa', functionId: 'pc_detailpage_wareBusiness', client: 'pc',
      clientVersion: '1.0.0', t: String(t), body: bs, h5st: (sig && sig.h5st) || '', 'x-api-eid-token': tok,
      loginType: '3', uuid: ck.split('.')[1] || '', scval: String(sku)});
    one.signed = !!(sig && sig.h5st);
    const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 8000);
    const r = await fetch('https://api.m.jd.com/?' + q.toString(), {credentials: 'include', signal: ctl.signal}); clearTimeout(tm);
    one.status = r.status; one.body = (await r.text()).slice(0, 60000);
  } catch (e) { one.error = String(e).slice(0, 160); }
  return JSON.stringify(one);
})
"""

# v7: this sku's row on the cart / 我的关注 page (read only: nothing is clicked or changed there)
ROW_JS = r"""
(sku => {
  const T = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const links = [...document.querySelectorAll('a[href*="' + sku + '"], [data-sku="' + sku + '"], [data-skuid="' + sku + '"], [skuid="' + sku + '"], [data-id="' + sku + '"]')];
  const rows = [];
  for (const a of links) { let e = a, best = null;
    for (let i = 0; i < 9 && e && e !== document.body; i++, e = e.parentElement) { const t = T(e);
      if (/[¥￥]\s*\d/.test(t) && t.length < 700) { best = e; break; } }
    if (best && !rows.includes(best)) rows.push(best); if (rows.length >= 3) break; }
  const out = rows.map(r => { const prices = [];
    for (const el of r.querySelectorAll('span,em,strong,b,i,div,p,del,s,label')) { if (el.children.length > 4) continue; const t = T(el);
      if (!t || t.length > 40 || !/[¥￥]\s*\d/.test(t)) continue; const st = getComputedStyle(el);
      prices.push({t, strike: /line-through/.test(st.textDecorationLine || '') || /^(DEL|S)$/.test(el.tagName), fs: parseFloat(st.fontSize) || 0,
                   cls: ((typeof el.className === 'string' ? el.className : '') + ' ' + (el.parentElement && typeof el.parentElement.className === 'string' ? el.parentElement.className : '')).slice(0, 120)}); if (prices.length > 12) break; }
    let name = '';   // v8: the product name: the longest link text / title pointing at this sku inside the row
    for (const a of r.querySelectorAll('a[href*="' + sku + '"], [title]')) { const t = ((a.getAttribute('title') || '') || T(a)).trim();
      if (t.length > name.length && t.length < 200 && !/[¥￥]\s*\d/.test(t)) name = t; }
    return {text: T(r).slice(0, 400), prices, name}; });
  return JSON.stringify({url: location.href, title: document.title, rows: out, n_links: links.length,
                         login: /passport\.jd\.com|plogin|login\.aspx/.test(location.href), n_elems: document.getElementsByTagName('*').length,
                         text: (document.body ? document.body.innerText : '').slice(0, 600)});
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
            "resolve": "解析短链接", "signed": "用京东签名接口读价", "cart": "从购物车读价", "follow": "从我的关注读价",
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
                f.write(json.dumps({"stage": cur.get("stage"), "label": cur.get("label"), "stages": self.list,
                                    "probe_version": PROBE_VERSION, "budget_s": BUDGET.get("total")}, ensure_ascii=False))
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

def net_name(u: str) -> str:
    m = re.search(r"functionId=([\w.\-]+)", u or "")
    if m:
        return m.group(1)
    p = urlparse(u or "")
    return ((p.hostname or "") + p.path)[:80]


def body_of(ws, rid: str, timeout: float = 3) -> tuple[str, str]:
    """v8: a response body -> (text, why_missing). The body read at loadingFinished (ws.bodies) comes first; asking
    later often answers 'No resource with given identifier found' (Chrome dropped it), which is reported as 'gone',
    never as an error that hides the rest."""
    bodies = getattr(ws, "bodies", None) or {}
    if rid in bodies:
        return bodies[rid], ""
    try:
        b = ws.call("Network.getResponseBody", {"requestId": rid}, cap(timeout))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        return "", "gone" if "No resource" in msg or "No data found" in msg else f"{type(e).__name__}: {msg}"[:120]
    text = b.get("body") or ""
    if b.get("base64Encoded"):
        text = base64.b64decode(text).decode("utf-8", "replace")
    return text, ""


NET_TOTAL_S = 5.0


def net_prices(ws, captured: dict, sku: str) -> tuple[list, dict | None]:
    """The page's OWN price API responses (seen on the Network domain while it loaded; the page signed them itself).
    v8: at most ~5 s in all, bodies taken from what was read at loadingFinished."""
    tries, items = [], list(captured.items())
    items.sort(key=lambda kv: 0 if NET_PRIO.search(net_name(kv[1].get("url", ""))) else 1)
    try:
        if hasattr(ws, "pump"):
            ws.pump(0.3)   # answers to the body requests sent at loadingFinished
    except Exception:  # noqa: BLE001
        pass
    end = time.time() + cap(NET_TOTAL_S, 2)
    for rid, info in items[:12]:
        name = net_name(info.get("url", ""))
        ent = {"src": "page:" + name, "status": info.get("status")}
        tries.append(ent)
        if NET_SKIP.search(name):
            ent["skipped"] = True
            continue
        if time.time() > end:
            ent["skipped"] = "no_time"
            continue
        text, why = body_of(ws, rid, 3)
        if why:
            ent["gone" if why == "gone" else "error"] = True if why == "gone" else why
            continue
        ent["size"] = len(text)
        got = parse_api_price(text) if text else None
        ent["price"] = bool(got)
        if got and (got.get("price") or got.get("final_price")) and (sku in text or NET_PRIO.search(name)):
            nm = name_of(text, sku)
            return tries, {**got, "src": ent["src"], **({"name": nm} if nm else {})}
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
        if budget_left() < 12:   # v8: the unsigned APIs are long shots; never at the cost of the routes after them
            tries.append({"src": src, "skipped": "no_time"})
            continue
        try:
            one = json.loads(ws.eval(f"({API_JS})({json.dumps(src)}, {json.dumps(url)})", cap(8, 4)) or "{}")
        except Exception as e:  # noqa: BLE001
            one = {"src": src, "error": f"{type(e).__name__}: {e}"[:160]}
        got = parse_api_price(one.get("body") or "") if one.get("body") else None
        tries.append({k: (v[:300] if k == "body" and isinstance(v, str) else v) for k, v in one.items()})
        if got and (got.get("price") or got.get("final_price")):
            tries[-1]["body"] = (one.get("body") or "")[:20000]
            nm = name_of(one.get("body") or "", sku)
            return {"api": tries, "api_price": {**got, "src": src, **({"name": nm} if nm else {})}}
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


_FINAL_ROW = re.compile(r"到手|券后|用券|优惠后|预估|折后")


def row_price(rows: list) -> dict | None:
    """Prices read off this sku's row on the cart / 我的关注 page -> {price, final_price}; struck-through and PLUS
    prices are skipped, a 到手/券后/优惠后 price is the final one."""
    for r in rows or []:
        mains, finals = [], []
        for x in r.get("prices") or []:
            t = str(x.get("t") or "")
            m = re.search(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", t)
            if not m or x.get("strike") or _PLUS_WORD.search(t + " " + str(x.get("cls") or "")):
                continue
            v = _num(m.group(1))
            if not v:
                continue
            (finals if _FINAL_ROW.search(t + " " + str(x.get("cls") or "")) else mains).append(v)
        price = mains[0] if mains else None
        fin = min(finals) if finals else None
        if fin is not None and price is not None and not (price * 0.3 <= fin <= price + 1e-9):
            fin = None
        if price is not None or fin is not None:
            out = {"price": price, "final_price": fin, "plus_price": None, "in_stock": None}
            nm = clean_name(r.get("name") or "")
            if nm:
                out["name"] = nm
            return out
    return None


def _cookie_value(cookies, name: str) -> str:
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") == name and str(c.get("domain") or "").lstrip(".").endswith("jd.com"):
            return str(c.get("value") or "")
    return ""


def signed_price(ws, sku: str, area: str) -> dict:
    """JD's signed pc_detailpage_wareBusiness from the current desktop jd.com page (see SIGNED_JS)."""
    try:
        one = json.loads(ws.eval(f"({SIGNED_JS})({json.dumps(sku)}, {json.dumps(area)})", cap(20, 2)) or "{}")
    except Exception as e:  # noqa: BLE001
        one = {"src": "signed:pc_detailpage_wareBusiness", "error": f"{type(e).__name__}: {e}"[:160]}
    body = one.pop("body", "") or ""
    got = parse_api_price(body) if body else None
    nm = name_of(body, sku) if body else ""
    if got and nm:
        got = {**got, "name": nm}
    one["route"] = "signed"
    one["price"] = bool(got and (got.get("price") or got.get("final_price")))
    if not one["price"]:
        one["refused"] = api_risk(body) or None
        one["head"] = re.sub(r"\s+", " ", body[:160]) if body else None
    return {"try": one, "price": {**got, "src": "signed"} if one["price"] else None}


def page_route(ws, name: str, url: str, sku: str, captured: dict, wait: float = 10) -> dict:
    """Open the cart / 我的关注 page in the probe tab (a link from www.jd.com), read this sku's row and the page's
    own API answers that carry it. Read only: nothing is clicked there."""
    one = {"route": name, "asked_url": url}
    captured.clear()
    try:
        ws.call("Page.navigate", {"url": url, "referrer": JD_HOME, "transitionType": "link"}, cap(15, 3))
    except Exception as e:  # noqa: BLE001
        one["error"] = f"{type(e).__name__}: {e}"[:160]
        return {"try": one, "price": None}
    end, snap = time.time() + cap(wait, 6), {}
    while time.time() < end:
        time.sleep(1.2)
        try:
            snap = json.loads(ws.eval(f"({ROW_JS})({json.dumps(sku)})", cap(6, 1)) or "{}")
        except Exception:  # noqa: BLE001
            continue
        if snap.get("login") or snap.get("rows") or is_risk_limit(snap.get("url") or "", snap.get("text") or ""):
            break
    one.update(url=(snap.get("url") or "")[:200], login=bool(snap.get("login")), rows=len(snap.get("rows") or []),
               n_links=snap.get("n_links"), n_elems=snap.get("n_elems"))
    if is_risk_limit(snap.get("url") or "", snap.get("text") or ""):
        one["risk_limit"] = True
        return {"try": one, "price": None}
    got = row_price(snap.get("rows") or [])
    if got:
        one["price"] = True
        one["from"] = "row"
        return {"try": one, "price": {**got, "src": name}}
    end = time.time() + cap(NET_TOTAL_S, 2)
    for rid, info in list(captured.items())[:20]:     # the page's own answers (cart / follow APIs) naming this sku
        if time.time() > end:
            break
        text, why = body_of(ws, rid, 3)
        if why or sku not in text:
            continue
        got = price_of_items(text, sku)
        if got:
            nm = name_of(text, sku)
            if nm:
                got = {**got, "name": nm}
            one.update(price=True, src_api=net_name(info.get("url", "")))
            one["from"] = "net"
            return {"try": one, "price": {**got, "src": name}}
    one["price"] = False
    one["missing"] = "not_listed" if not one.get("login") else "login"
    return {"try": one, "price": None}


ROUTE_MIN_S = {"cart": 20.0, "follow": 14.0}   # v8: a page route starts only with this much of the budget left


def alt_routes(ws, sku: str, area: str, captured: dict, st, on_jd_page: bool, signed_done: bool = False) -> dict:
    """v7: the price without the item page, each route once, in order: signed API, cart, 我的关注.
    v8: the signed API is skipped when it already ran on the item page; cart / 我的关注 only while time remains."""
    tries = []
    if on_jd_page and not signed_done:
        st("signed")
        r = signed_price(ws, sku, area)
        tries.append(r["try"])
        if r["price"]:
            return {"alt": tries, "alt_price": r["price"]}
    for name, url in (("cart", CART_URL), ("follow", FOLLOW_URL)):
        if budget_left() < ROUTE_MIN_S[name]:
            tries.append({"route": name, "skipped": "no_time", "price": False})
            continue
        st(name)
        r = page_route(ws, name, url, sku, captured)
        tries.append(r["try"])
        if r["price"]:
            return {"alt": tries, "alt_price": r["price"]}
        if r["try"].get("risk_limit"):
            break
        if name == "cart" and not on_jd_page and not signed_done and not r["try"].get("login"):
            st("signed")   # the cart page carries JD's signing SDK: the signed API from there
            r2 = signed_price(ws, sku, area)
            tries.append(r2["try"])
            if r2["price"]:
                return {"alt": tries, "alt_price": r2["price"]}
    return {"alt": tries}


SKELETON_AFTER_S = 6.0      # v8: a generic-title page with no product module this long is not going to render
SKELETON_MAX_ELEMS = 600
WAIT_MAX_S = 15.0           # v8: the render wait never takes more than this (v7: up to ~36 s with slow evals)
SHOT_MAX_S = 5.0


def is_skeleton(snap: dict) -> bool:
    """v8: JD answered the item URL with a page that is not a product page: only the site-wide title (no product
    title element), few elements, no price element or price text. Seen on 102 (295 elements, 2026-10-30)."""
    return (is_generic_title(snap.get("doc_title") or snap.get("title") or "") and snap.get("title_from") != "element"
            and int(snap.get("n_elems") or 0) < SKELETON_MAX_ELEMS and not snap.get("sel") and not snap.get("cand"))


def _watchdog(total: float):
    """SIGALRM at budget + 12 s: raise OutOfTime in whatever is running, so cmd_probe still prints what it has."""
    try:
        import signal

        def fire(_sig, _frm):
            raise OutOfTime()
        signal.signal(signal.SIGALRM, fire)
        signal.setitimer(signal.ITIMER_REAL, total + 12)
        return lambda: signal.setitimer(signal.ITIMER_REAL, 0)
    except (ImportError, ValueError, AttributeError, OSError):   # not the main thread / no signals: soft budget only
        return lambda: None


def cmd_probe(a):
    st = Stages(getattr(a, "progress", "") or "")
    try:
        total = float(getattr(a, "budget", "") or DEFAULT_BUDGET)
    except (TypeError, ValueError):
        total = DEFAULT_BUDGET
    BUDGET.update(end=time.time() + total, total=total)
    stop_dog = _watchdog(total) if getattr(a, "progress", "") or getattr(a, "budget", "") else (lambda: None)
    st("connect")
    route = (getattr(a, "route", "") or "auto").lower()
    url = normalize_jd(a.url)
    sku = jd_sku_of(url)
    resolved = None
    if not sku and is_jd_link(url):
        # v7: a 3.cn / u.jd.com share link opened in the browser lands on item.m.jd.com (the limited page)
        st("resolve")
        resolved = resolve_jd_link(url)
        if resolved.get("sku"):
            url, sku = resolved["url"], resolved["sku"]
    jd = bool(sku)
    jd_link = jd or is_jd_link(url)
    prof = {}
    try:
        prof = profile_of(CDP)
    except Exception:  # noqa: BLE001
        prof = {"port": urlparse(CDP).port or 0, "dir": "", "cookies": None}
    front_mode = os.environ.get("AIW_SHOP_FRONT", "1") != "0"
    fronted, snap, captured, prev = None, {}, {}, ""
    blocked, chain, env = [], [], {}
    stage_error, signed_done = "", False
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
            meth, prm = m.get("method"), m.get("params") or {}
            if meth == "Network.responseReceived" and len(captured) < 40:
                resp = prm.get("response") or {}
                u = resp.get("url") or ""
                if PRICE_NET_RE.search(u) or "cart" in u.lower() or "follow" in u.lower():
                    captured[prm.get("requestId")] = {"url": u, "status": resp.get("status")}
            elif meth == "Network.loadingFinished" and prm.get("requestId") in captured and hasattr(ws, "request"):
                # v8: read the body NOW; asked for later Chrome may have dropped it ('No resource with given identifier')
                rid = prm.get("requestId")
                if not NET_SKIP.search(net_name(captured[rid].get("url", ""))) and len(ws.bodies) < 30:
                    def keep(msg, _rid=rid):
                        res = msg.get("result") or {}
                        text = res.get("body") or ""
                        if res.get("base64Encoded"):
                            text = base64.b64decode(text).decode("utf-8", "replace")
                        if text:
                            ws.bodies[_rid] = text[:200000]
                    ws.request("Network.getResponseBody", {"requestId": rid}, keep)
            elif meth == "Network.requestWillBeSent" and prm.get("type") == "Document" and len(chain) < 12:
                rr = prm.get("redirectResponse") or {}
                chain.append({"url": str((prm.get("request") or {}).get("url") or "")[:200],
                              **({"from_status": rr.get("status")} if rr else {})})
            elif meth == "Fetch.requestPaused":
                u = str((prm.get("request") or {}).get("url") or "")
                if is_mobile_item_url(u):   # v7: the mobile item page / its limiter: never loaded
                    blocked.append(u[:240])
                    ws.notify("Fetch.failRequest", {"requestId": prm.get("requestId"), "errorReason": "BlockedByClient"})
                else:
                    ws.notify("Fetch.continueRequest", {"requestId": prm.get("requestId")})
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
        try:   # v7: what JD sees: the user agent (a mobile one is 302'd to item.m.jd.com) and navigator.webdriver
            env = json.loads(ws.eval(ENV_JS, 5) or "{}")
        except Exception:  # noqa: BLE001
            env = {}
        if jd_link and mobile_ua(env.get("ua") or ""):
            try:
                ws.call("Emulation.setUserAgentOverride", {"userAgent": desktop_ua(env.get("ua") or ""),
                                                           "acceptLanguage": "zh-CN,zh", "platform": "Linux x86_64"}, 5)
                env["ua_override"] = desktop_ua(env.get("ua") or "")
            except Exception as e:  # noqa: BLE001
                env["ua_override_error"] = str(e)[:100]
        try:
            ws.call("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS}, 5)
            env["webdriver_patch"] = True
        except Exception:  # noqa: BLE001
            env["webdriver_patch"] = False
        if jd_link:
            try:
                ws.call("Fetch.enable", {"patterns": [{"urlPattern": p, "resourceType": "Document", "requestStage": "Request"}
                                                      for p in MOBILE_DOC_PATTERNS]}, 5)
                env["mobile_guard"] = True
            except Exception:  # noqa: BLE001
                env["mobile_guard"] = False
        if not prof.get("dir"):
            prof["dir"] = profile_dir_from_version_page(ws)
            if prof["dir"]:
                prof["dir_from"] = "chrome://version"
        picked = None
        if route != "alt":
            st("open")
            ws.call("Page.navigate", {"url": url, "referrer": JD_HOME, "transitionType": "link"}, cap(20, 30))
            st("wait")
            t_wait = time.time()
            end = t_wait + min(float(a.wait), WAIT_MAX_S, max(1.0, budget_left() - 60))
            last_sig, i = None, 0
            while time.time() < end:
                if blocked:
                    break
                try:   # v6: returns as soon as a price element appears (MutationObserver), else after 1.5 s
                    ws.eval(f"({WAIT_JS})(1500)", cap(4))
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.3)
                i += 1
                try:
                    snap = json.loads(ws.eval(EXTRACT_JS, cap(6)) or "{}")
                except Exception:  # noqa: BLE001 - navigating; retry
                    time.sleep(0.9)
                    continue
                if blocked:
                    break
                if is_risk_limit(snap.get("url") or "", snap.get("text") or ""):
                    snap["risk_limit"] = True     # 活动火爆: nothing renders later, stop here
                    break
                if jd and snap.get("ready") == "complete" and time.time() - t_wait >= SKELETON_AFTER_S and is_skeleton(snap):
                    snap["skeleton"] = True       # v8: a generic-title shell: go straight to the signed API
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
        if blocked:
            # v7: JD sent the desktop item page (or a short link) to the mobile item page: refused, not loaded
            snap = {**{k: snap.get(k) for k in ("title", "doc_title", "title_from") if snap.get(k)},
                    "url": blocked[0], "mobile_blocked": blocked[0], "text": "", "sel": [], "cand": [], "btn": []}
            if not sku and jd_sku_of(blocked[0]):
                sku = jd_sku_of(blocked[0])
                jd = True
                snap["resolved_from_redirect"] = True
        snap["sku_picked"] = picked
        snap["fronted"] = fronted
        snap["route"] = route
        host = urlparse(snap.get("url") or "").hostname or ""
        login_page = any(k in host for k in LOGIN_HOST_KEYS)
        on_item = route != "alt" and not blocked and not snap.get("risk_limit") and not login_page
        page_ok = on_item
        cookies = None
        if jd:
            snap["jd_sku"] = snap.get("jd_sku") or sku
            if page_ok and not has_jd_price(snap) and not kw_price(snap):
                # v8: the signed API first (proven on 102: it priced the item when the page showed nothing), then the
                # page's own answers (bodies read at loadingFinished), then the unsigned APIs if time is left
                if "jd.com" in (snap.get("url") or url):
                    cookies = read_cookies(bws, ws)
                    area = jd_area("ipLoc-djd=" + _cookie_value(cookies, "ipLoc-djd")) if cookies else "1_72_2799_0"
                    st("signed")
                    r = signed_price(ws, snap["jd_sku"], area)
                    signed_done = True
                    snap.setdefault("alt", []).append(r["try"])
                    if r["price"]:
                        snap["api_price"] = r["price"]
                if not snap.get("api_price"):
                    st("net")
                    snap["net"], got = net_prices(ws, captured, snap["jd_sku"])
                    if got:
                        snap["api_price"] = got
                    elif "jd.com" in (snap.get("url") or url) and budget_left() > 30:
                        st("api")
                        snap.update(jd_api_price(ws, snap["jd_sku"]))
        priced = (has_jd_price(snap) or kw_price(snap) or bool(snap.get("api_price"))) if jd else \
            bool(snap.get("sel") or len(snap.get("cand") or []) >= 1)
        need_diag = not page_ok or not priced
        if (a.shot or need_diag) and route != "alt" and (a.shot or budget_left() > 25):
            # v8: at most ~5 s, never when the price is already known (v7: 27 s on 102)
            st("shot")
            t_shot = time.time()
            try:   # a background tab produces no frames: bring it forward for the picture
                if not fronted:
                    bws.call("Target.activateTarget", {"targetId": tid}, 1.5)
                time.sleep(0.3)
                left = SHOT_MAX_S - (time.time() - t_shot)
                if left > 0.5:
                    snap["screenshot"] = ws.call("Page.captureScreenshot", {"format": "jpeg", "quality": 50,
                                                                            "optimizeForSpeed": True}, left).get("data")
                else:
                    snap["shot_error"] = "skipped: no time"
            except Exception as e:  # noqa: BLE001
                snap["shot_error"] = str(e)[:160]
        if cookies is None:
            cookies = read_cookies(bws, ws)
        if jd and not login_page and (route == "alt" or not page_ok or not priced):
            # v7: limited, redirected to the mobile page, or no price: another route, once each
            area = jd_area("ipLoc-djd=" + _cookie_value(cookies, "ipLoc-djd")) if cookies else "1_72_2799_0"
            got_alt = alt_routes(ws, sku, area, captured, st, on_jd_page=on_item, signed_done=signed_done)
            got_alt["alt"] = (snap.get("alt") or []) + got_alt.get("alt", [])
            snap.update(got_alt)
            if snap.get("alt_price") and not a.shot:
                snap.pop("screenshot", None)   # v8: priced after all: no picture to keep
        if cookies is not None:
            stl = jd_login_state(cookies)
            prof.update(logged_in=stl["logged_in"] if (jd or stl["jd_cookies"]) else None, jd_cookies=stl["jd_cookies"],
                        login_cookies=stl["login_cookies"], cookies_total=stl["cookies_total"])
            if prof.get("cookies") is None:
                prof["cookies"] = stl["cookies_total"]
        snap["snippets"] = snippets(snap)
        snap["net_seen"] = len(captured)
        ws.close()
    except OutOfTime:
        # v8: the budget ran out in the middle of a stage: say which, keep everything read so far
        cur = st.list[-1] if st.list else {}
        stage_error = f"「{cur.get('label') or cur.get('stage') or '?'}」超时（总预算 {int(BUDGET['total'])} 秒用完）"
    finally:
        stop_dog()
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
    snap["budget_s"] = BUDGET["total"]
    if stage_error:
        snap["stage_error"] = stage_error
        snap["timed_out"] = True
    nm = next((clean_name(x.get("name") or "") for x in (snap.get("api_price"), snap.get("alt_price"))
               if isinstance(x, dict) and x.get("name")), "")
    if nm:
        snap["item_name"] = nm      # v8: the product name from JD's own data (the page may show only the generic title)
    snap["cdp"] = CDP
    snap["asked_url"] = url
    snap["profile"] = prof
    snap["env"] = env
    snap["nav_chain"] = chain
    if resolved is not None:
        snap["resolved"] = resolved
    if blocked:
        snap["blocked"] = blocked[:5]
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
    p.add_argument("--route", default="auto", choices=("auto", "alt"))
    p.add_argument("--budget", default="")   # v8: seconds for the whole run (the server passes its own)
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
