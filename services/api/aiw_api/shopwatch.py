"""京东 / 淘宝 / 天猫 price watches with optional automatic ordering (stopped at the payment step).

How a check works
  The scheduler calls check() for watches with mode "shop". It runs shop_probe.py on the chosen AIW computer
  through vm-agent's shell: the probe opens the link in a background tab of the computer's logged-in Chrome
  (DevTools port 9222), picks the spec, and returns a raw snapshot of price-looking elements, buttons and text.
  parse_snapshot() here turns that into price / 到手价 / stock / login-wall, deterministically and cheaply. Only
  when no price can be read does a vision model look at a screenshot (if one is configured).

What a hit does
  A hit is a transition (price <= target and in stock, after not being so). With auto_order off it notifies.
  With auto_order on it submits an agent task on that computer (source "watch_order") that opens the item,
  picks spec and quantity, clicks 立即购买 and stops on the 确认订单 page, then calls order_confirm. That tool
  re-reads the payable amount from the page itself, refuses (and notifies) if it is above the target, and
  otherwise raises the L3 confirmation card + push for up to 30 minutes. Only an explicit approve makes it click
  提交订单 (done by this code, not the model). Payment passwords are never handled: the agent hands the
  cashier to the user through the takeover desktop. A guard around the task's tool executor refuses any
  attempt by the model to click 提交订单 / 支付 itself, by text, by coordinates, by Enter, or through the shell.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from .shop_probe import PROBE_VERSION, clean_name, is_generic_title, is_risk_limit, normalize_jd, parse_api_price

log = logging.getLogger("aiw.shopwatch")

MODE = "shop"
MIN_INTERVAL, DEFAULT_INTERVAL = 10, 30
CONFIRM_TIMEOUT_S = 1800
PROBE_SRC = Path(__file__).with_name("shop_probe.py")
REMOTE = "$HOME/.config/aiw/shop/probe.py"

COLUMNS = (("site", "TEXT"), ("quantity", "INTEGER"), ("sku", "TEXT"), ("address_note", "TEXT"),
           ("auto_order", "INTEGER"), ("machine_id", "TEXT"), ("price", "REAL"), ("final_price", "REAL"),
           ("in_stock", "INTEGER"), ("shop_status", "TEXT"), ("status_note", "TEXT"), ("item_title", "TEXT"),
           ("below", "INTEGER"), ("order_task_id", "TEXT"), ("order_status", "TEXT"), ("diag_at", "REAL"))

SITE_NAMES = {"jd": "京东", "taobao": "淘宝", "tmall": "天猫"}
URL_RE = re.compile(r"https?://[^\s，。；;、\"'<>（）()【】\[\]]+")
NUM = r"([0-9]{1,7}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
PRICE_IN = re.compile(r"[¥￥]\s*" + NUM + r"(?:\s*[-~～至]\s*[¥￥]?\s*" + NUM + r")?|" + NUM + r"\s*元")
FINAL_RE = re.compile(r"到手价|券后价|券后|用券后|领券后|预估到手|预估价|到手|折后价|补贴后|活动价后|优惠后|final|coupon|aftercoupon|estimate", re.I)
# a 券后/到手 label right in front of a price ('满150减50 券后¥137'): final, even when 满减 text sits before it
FINAL_NEAR = re.compile(r"(到手价?|券后价?|用券后|领券后|预估到手价?|预估价|折后价?|优惠后)[：:\s]*$")
# '¥137券后 ¥187': a label glued to the price before it belongs to that price, not to the next one
GLUED_BEFORE = re.compile(r"[¥￥]?\d[\d.,]*(到手价?|券后价?)[：:\s]*$")
FINAL_AFTER = re.compile(r"^(券后价?|到手价?)")
NOT_PRICE_RE = re.compile(r"运费|邮费|满\s*\d|减\s*\d|省\s*[¥￥]|已省|节省|立省|返\s*[¥￥]|定金|月供|分期|起/期|x\s*\d+期|×\s*\d+期|积分|赠|红包|"
                          r"plus会员|PLUS|会员价|划线|原价|参考价|市场价|门店价|京东价.*?对比|销量|评价|关注", re.I)
OUT_OF_STOCK = re.compile(r"无货|暂时无货|已售罄|售罄|已抢光|缺货|库存不足|该地区暂不支持配送|到货通知|暂不销售|已下柜|商品已下架|宝贝已下架|此商品已下架|已下架")
OFF_SHELF = re.compile(r"商品已下柜|该商品已下柜|商品已下架|宝贝已下架|此商品已下架|宝贝不存在|商品不存在|很抱歉，您查看的商品找不到了|该商品已下架")
BUY_BTN = re.compile(r"^(立即购买|立即抢购|马上抢|领券购买|加入购物车|加入购物袋|立即预约|去购买|抢购)$")
CAPTCHA_RE = re.compile(r"滑块|拖动.*?验证|安全验证|请完成验证|验证码|访问受限|人机验证|异常流量|punish|x5sec|nocaptcha")
LOGIN_HOSTS = ("passport.jd.com", "login.taobao.com", "login.tmall.com", "login.m.jd.com", "plogin.m.jd.com", "havanalogin.taobao.com")
CAPTCHA_HOSTS = ("sec.taobao.com", "cfe.m.jd.com", "iv.jd.com", "safe.jd.com", "risk", "punish", "x5sec")
# 京东 shows these instead of a number before a product is priced (预售/新品) or while its price is withheld
PENDING_RE = re.compile(r"价格待发布|暂无报价|价格待定")
ORDER_HOSTS = ("trade.jd.com", "buy.taobao.com", "buy.tmall.com", "h5.m.taobao.com/cart/order", "trade.m.jd.com")
PAY_HOSTS = ("pay.jd.com", "cashier", "alipay.com", "pcashier", "payc.m.jd.com")

SUBMIT_RE = re.compile(r"提交订单|确认订单|去结算|结算")
PAY_RE = re.compile(r"立即支付|确认付款|确认支付|去支付|立即付款|付款|支付|pay\b", re.I)
# BrowserOS's own MCP / DevTools ports: a shell command talking to them walks around the element gates
BOS_SHELL_RE = re.compile(r"\b(9110|9210)\b|/mcp(?![\w.-])", re.I)
SHELL_CLICK_RE = re.compile(r"xdotool|9222|devtools|dispatchMouseEvent|Input\.dispatch|chrome-remote|playwright|puppeteer|"
                            r"pyautogui|ydotool|shop/probe\.py", re.I)
ENTER_KEYS = re.compile(r"^(enter|return|kp_enter|ctrl\+enter|ctrl\+return)$", re.I)


# ----------------------------------------------------------------------------- urls and sites
def extract_url(text: str) -> str:
    """A pasted share text (【淘宝】… https://m.tb.cn/h.xxx …) or a bare link -> the link."""
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,!?") if m else (text or "").strip()


def site_of(url: str) -> Optional[str]:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    if host == "jd.com" or host.endswith((".jd.com", ".jd.hk", ".3.cn")) or host in ("3.cn", "u.jd.com"):
        return "jd"
    if host == "tmall.com" or host.endswith((".tmall.com", ".tmall.hk")):
        return "tmall"
    if host == "taobao.com" or host.endswith((".taobao.com", ".tb.cn")) or host == "tb.cn":
        return "taobao"
    return None


def normalize_url(url: str) -> str:
    """Share text or any JD item link (item.m.jd.com/product/<sku>.html?utm_user=..., m.jd.com, wareId=) ->
    https://item.jd.com/<sku>.html; other links unchanged."""
    return normalize_jd(extract_url(url))


def is_shop_url(url: str) -> bool:
    return site_of(extract_url(url)) is not None


def is_order_url(url: str) -> bool:
    return any(h in (url or "") for h in ORDER_HOSTS) and "/common/limit.html" not in (url or "")


def is_pay_url(url: str) -> bool:
    return any(h in (url or "") for h in PAY_HOSTS)


# ----------------------------------------------------------------------------- parsing (pure, unit-tested)
def parse_price(text: str) -> tuple[Optional[float], bool]:
    """'¥199.00' -> (199.0, False); '￥199-299' -> (199.0, True); no price -> (None, False)."""
    m = PRICE_IN.search(text or "")
    if not m:
        return None, False
    lo = m.group(1) or m.group(3)
    hi = m.group(2)
    try:
        v = float(lo.replace(",", ""))
    except (TypeError, ValueError):
        return None, False
    if not (0 < v < 10_000_000):
        return None, False
    return v, bool(hi)


def _bare_number(text: str) -> Optional[float]:
    """Price elements often hold only the digits (<span class=price>199.00</span>)."""
    t = (text or "").strip().lstrip("¥￥").strip()
    m = re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", t)
    return float(t) if m and float(t) > 0 else None


def _label_after(ctx: str, t: str) -> str:
    t = (t or "").strip()
    if not ctx or not t or t not in ctx:
        return ""
    return ctx.split(t, 1)[1][:8]


def _final_label(pre: str, after: str) -> Optional[bool]:
    """True: a 券后/到手 label belongs to this price; False: the label in front belongs to the price before it."""
    if FINAL_AFTER.search(after or ""):
        return True
    if GLUED_BEFORE.search(pre or ""):
        return False
    return True if FINAL_NEAR.search(pre or "") else None


def price_line(r: dict, target=None) -> str:
    """'原价 ¥187 · 券后 ¥137 · 目标 ¥120' (or '现价 ¥187 · 目标 ¥120' when the page has no coupon price)."""
    def y(v):
        return f"¥{float(v):g}"
    parts = []
    if r.get("final_price") is not None:
        if r.get("price") is not None and abs(float(r["price"]) - float(r["final_price"])) > 1e-9:
            parts.append(f"原价 {y(r['price'])}")
        parts.append(f"券后 {y(r['final_price'])}")
    elif r.get("price") is not None:
        parts.append(f"现价 {y(r['price'])}")
    if target not in (None, ""):
        try:
            parts.append(f"目标 {y(target)}")
        except (TypeError, ValueError):
            pass
    return " · ".join(parts)


def _label_before(ctx: str, t: str) -> str:
    """The few characters in front of a price inside its context ('运费 ¥5' -> '运费 '): its label."""
    t = (t or "").strip()
    if not ctx or not t or t not in ctx:
        return ""
    return ctx.split(t, 1)[0][-8:]


KW_FINAL = re.compile(r"(到手价?|券后价?|用券后|领券后|预估到手价?)\s*[:：]?\s*[¥￥]\s*" + NUM + r"|[¥￥]\s*" + NUM + r"\s*(?:到手价?|券后价?)")
KW_MAIN = re.compile(r"(京东价|秒杀价|促销价|售价|价格)\s*[:：]?\s*[¥￥]\s*" + NUM)
KW_PLUS = re.compile(r"PLUS|plus|会员|Plus")
KW_PLUS_PRICE = re.compile(r"(?:PLUS|Plus|plus|会员)\s*(?:专享)?价?\s*[:：]?\s*[¥￥]\s*" + NUM)
LOGIN_PRICE_RE = re.compile(r"登录查看价格|登录后查看|登录后可见|请登录查看|登录看价|价格登录可见")


def _f(v) -> Optional[float]:
    try:
        x = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return x if 0 < x < 10_000_000 else None


def text_prices(kw: list, max_y: int = 1600) -> dict:
    """v6: labelled prices in the visible text blocks near the top ('到手价 ¥137', '¥137到手', '京东价 ¥187').
    PLUS / 会员 prices go to plus, never to the price we judge by."""
    main, finals, plus = None, [], []
    for b in sorted([k for k in kw or [] if isinstance(k, dict)], key=lambda k: int(k.get("y") or 0)):
        if int(b.get("y") or 0) > max_y:
            continue
        t = str(b.get("t") or "")
        for m in KW_PLUS_PRICE.finditer(t):
            v = _f(m.group(1))
            if v is not None:
                plus.append(v)
        for m in KW_FINAL.finditer(t):
            v = _f(m.group(2) or m.group(3))
            pre = t[max(0, m.start() - 8):m.start()]
            if v is None or NOT_PRICE_RE.search(pre):
                continue
            (plus if KW_PLUS.search(pre + m.group(0)) else finals).append(v)
        for m in KW_MAIN.finditer(t):
            v = _f(m.group(2))
            pre = t[max(0, m.start() - 8):m.start()]
            if v is None or NOT_PRICE_RE.search(pre):
                continue
            if KW_PLUS.search(pre):
                plus.append(v)
            elif main is None:
                main = v
    return {"price": main, "final_price": min(finals) if finals else None, "plus_price": min(plus) if plus else None}


def embedded_price(items: list) -> Optional[dict]:
    """v6: JSON the page carries for this sku (pageConfig.product, __INITIAL_STATE__ ... filtered by sku in the page)."""
    for e in items or []:
        if not isinstance(e, dict) or not e.get("body"):
            continue
        try:
            d = json.loads(e["body"])
        except ValueError:
            continue
        got = parse_api_price(d)
        if not got and isinstance(d, dict):
            for k in ("jdPrice", "salePrice", "currentPrice", "price", "p"):
                v = d.get(k)
                v = _f(v.get("price") if isinstance(v, dict) else v)
                if v:
                    got = {"price": v, "final_price": None, "plus_price": None, "in_stock": None}
                    break
        if got and (got.get("price") or got.get("final_price")):
            return {**got, "src": e.get("src")}
    return None


RISK_ALT_NOTE = "京东风控限流，已自动换方式读价"
ROUTE_CN = {"signed": "京东签名价格接口", "cart": "购物车", "follow": "我的关注"}
TAB_ROUTE_CN = {"cart": "已打开的购物车页", "follow": "已打开的我的关注页"}


def _alt_result(alt: dict) -> dict:
    price, fin = alt.get("price"), alt.get("final_price")
    if fin is not None and price is not None and not (price * 0.3 <= fin <= price + 1e-9):
        fin = None
    out = {"status": "ok", "price": price, "final_price": fin, "effective": fin if fin is not None else price,
           "in_stock": alt.get("in_stock"), "via": "alt", "alt_src": alt.get("src")}
    if alt.get("plus_price"):
        out["plus_price"] = alt["plus_price"]
    return out


def parse_snapshot(snap: dict) -> dict:
    """_parse_snapshot, plus v8's partial runs: a probe that ran out of time still gives what it read, and the note
    says which stage it stopped in."""
    res = _parse_snapshot(snap)
    if isinstance(snap, dict) and not snap.get("error") and snap.get("stage_error"):
        res["timed_out"] = True
        if res["status"] != "ok":
            res["note"] = ((res.get("note") or "") + "；这次检查" + snap["stage_error"]).lstrip("；")
    return res


def _parse_snapshot(snap: dict) -> dict:
    """Raw probe snapshot -> {status, price, final_price, effective, in_stock, title, url, note}.

    status: ok | login | need_login | captcha | risk_limit | off_shelf | price_pending | no_price | error
    price is the shown selling price, final_price the 到手价/券后价 when the page shows one,
    effective = final_price or price (what the order would cost per item before shipping)."""
    if not isinstance(snap, dict) or snap.get("error"):
        err = (snap or {}).get("error") if isinstance(snap, dict) else "bad probe output"
        note = cn_error(str(err))
        if isinstance(snap, dict) and snap.get("stage_error"):
            note += "，" + snap["stage_error"]
        return {"status": "no_browser" if str(err).startswith("NO_CDP") else "error", "note": note[:300],
                "price": None, "final_price": None, "effective": None, "in_stock": None, "title": "", "url": ""}
    url = snap.get("url") or ""
    host = (urlparse(url).hostname or "").lower()
    text = snap.get("text") or ""
    # v8: the product name from JD's own data first; the site-wide '京东(JD.COM)-正品低价…' title is never a name
    title = clean_name(snap.get("item_name") or "") or clean_name(snap.get("title") or "") or clean_name(snap.get("doc_title") or "")
    res = {"status": "ok", "price": None, "final_price": None, "effective": None, "in_stock": None,
           "title": title[:120], "url": url, "note": "", "range": False}
    if snap.get("cdp"):
        res["cdp"] = str(snap["cdp"])[:60]
    prof = snap.get("profile") if isinstance(snap.get("profile"), dict) else {}
    if prof.get("dir"):
        res["profile"] = str(prof["dir"])[:120]
    fresh = prof.get("cookies") == 0 or prof.get("logged_in") is False
    if "logged_in" in prof:
        res["logged_in"] = prof.get("logged_in")
    frames = " ".join(snap.get("frames") or [])
    risk = bool(snap.get("risk_limit") or is_risk_limit(url, text) or snap.get("mobile_blocked"))
    alt = snap.get("alt_price") if isinstance(snap.get("alt_price"), dict) else None
    if snap.get("route") in ("tab", "cart_first") and alt and (alt.get("price") or alt.get("final_price")):
        # v10: read off the cart / 我的关注 tab already open (or the cart first): the item page was never opened
        res.update(_alt_result(alt), route=snap.get("route"))
        res["title"] = clean_name(alt.get("name") or "") or ""
        res["note"] = ("价格从" + (TAB_ROUTE_CN.get(alt.get("src"), "已打开的页面") if snap.get("route") == "tab"
                                  else "购物车") + "读出，没有打开商品页")
        if alt.get("stale"):
            res["note"] += "（刷新后没读到，用的是刷新前的价格）"
        return res
    if (risk or snap.get("route") == "alt") and alt and (alt.get("price") or alt.get("final_price")):
        # v7: the item page was limited / sent to the mobile page (or skipped during the cool-down): another route
        res.update(_alt_result(alt), risk=risk or bool(snap.get("cooling")), route=snap.get("route") or "auto")
        res["title"] = res["title"] if res["title"] and not is_risk_limit(url, res["title"]) and "多快好省" not in res["title"] else ""
        res["note"] = (RISK_ALT_NOTE if res["risk"] else "") + f"（{ROUTE_CN.get(alt.get('src'), alt.get('src') or '')}）"
        return res
    if risk or snap.get("route") == "alt":
        # 活动火爆 请选购其他商品 (trade.m.jd.com/common/limit.html): JD refuses this computer for now; not a captcha
        res["risk"] = True
        if snap.get("mobile_blocked") and not snap.get("risk_limit"):
            note = ("京东把商品页跳到了移动版商品页（item.m.jd.com，被限流的就是它），已拦截没有打开；"
                    "换方式（签名接口/购物车/我的关注）也没读到价格")
        elif snap.get("route") == "alt" and not risk:
            note = "京东风控限流冷却中，只用了其他方式（签名接口/购物车/我的关注），没读到价格"
        else:
            note = "京东风控限制了这台电脑的访问（活动火爆页）。这不是验证码，没有可以完成的验证" + (
                "；已自动换方式（签名接口/购物车/我的关注）也没读到价格" if snap.get("alt") else "")
        res.update(status="risk_limit", note=note + ("；读价用的浏览器也没有登录京东" if prof.get("logged_in") is False else ""))
        return res
    if any(h in host for h in LOGIN_HOSTS) or (snap.get("password_input") and re.search(r"登录|login", text[:800] + title, re.I)) \
            or any(h in frames for h in ("login.taobao.com", "passport.jd.com", "havanalogin")):
        res.update(status="login", note="需要登录" + (f"（读价用的浏览器配置 {prof['dir']} 里没有登录信息）" if fresh else ""))
        return res
    if any(h in url for h in CAPTCHA_HOSTS) or (CAPTCHA_RE.search(text[:1500]) and len(text) < 2500):
        res.update(status="captcha", note="出现了滑块/安全验证" + (
            f"（读价用的浏览器配置 {prof['dir']} 里没有登录信息，像是新开的配置）" if fresh else ""))
        return res
    if OFF_SHELF.search(text[:3000]):
        res.update(status="off_shelf", in_stock=False, note="商品已下架")
        return res

    finals, mains, plus = [], [], []
    for s in snap.get("sel") or []:
        v, rng = parse_price(s.get("t") or "")
        if v is None:
            v, rng = _bare_number(s.get("t") or ""), False
        if s.get("role") == "plus":         # PLUS 会员价: only for PLUS members, never the price we judge by
            if v is not None:
                plus.append(v)
            continue
        pre = _label_before(s.get("ctx") or "", s.get("t") or "")
        lab = _final_label(pre, _label_after(s.get("ctx") or "", s.get("t") or ""))
        near_final = lab is True
        if v is None or NOT_PRICE_RE.search(s.get("t") or "") or (NOT_PRICE_RE.search(pre) and not near_final):
            continue
        label = f"{s.get('t', '')} {s.get('ctx', '')}"
        is_final = s.get("role") == "final" or bool(FINAL_RE.search(s.get("t") or "")) or near_final
        (finals if is_final else mains).append((v, rng, float(s.get("fs") or 0), int(s.get("y") or 0), label))
    pm = PENDING_RE.search(" ".join([text[:2000]] + [str(s.get("t") or "") for s in snap.get("sel") or []]))
    if pm and not mains and not finals:
        # no price element of its own: any ¥ on the page belongs to recommended items, not to this one
        res.update(status="price_pending", note=f"商品页显示「{pm.group(0)}」，还没有开价", plus_price=min(plus) if plus else None)
        return res
    cands = []
    for c in snap.get("cand") or []:
        t = c.get("t") or ""
        v, rng = parse_price(t)
        if v is None or c.get("strike") or NOT_PRICE_RE.search(t):
            continue
        pre = _label_before(c.get("ctx") or "", t)
        lab = _final_label(pre, _label_after(c.get("ctx") or "", t))
        near_final = lab is True
        if NOT_PRICE_RE.search(pre) and not near_final:
            continue
        own_final = bool(FINAL_RE.search(t) or FINAL_RE.search(c.get("cls") or ""))
        ctx_final = near_final or (lab is None and bool(FINAL_RE.search(pre)))
        cands.append((v, rng, float(c.get("fs") or 0), int(c.get("y") or 0), own_final or ctx_final))
    if not finals:
        finals = [(v, r, f, y, "") for (v, r, f, y, fin) in cands if fin]
    if not mains:
        # the biggest price-looking text in the top part of the page is the selling price
        top = [x for x in cands if not x[4] and x[3] < 1400] or [x for x in cands if not x[4]]
        if top:
            best = max(top, key=lambda x: (x[2], -x[3]))
            mains = [best[:4] + ("",)]
    else:
        mains.sort(key=lambda x: (-x[2], x[3]))
    if mains:
        res["price"], res["range"] = mains[0][0], bool(mains[0][1])
    if finals:
        f = min(x[0] for x in finals)
        # a 到手价 above the shown price is a different thing (PLUS, bundles); a tiny one is a coupon amount
        if res["price"] is None or (res["price"] * 0.3 <= f <= res["price"]):
            res["final_price"] = f
    api = snap.get("api_price") if isinstance(snap.get("api_price"), dict) else None
    alt_used = False
    if api is None and alt and (alt.get("price") or alt.get("final_price")):
        api, alt_used = alt, True     # v7: no price on the item page: signed API / cart / 我的关注
    if api is None:
        for att in snap.get("api") or []:
            got = parse_api_price(att.get("body") or "") if isinstance(att, dict) and att.get("body") else None
            if got and (got.get("price") or got.get("final_price")):
                api = {**got, "src": att.get("src")}
                break
    if res["price"] is None and res["final_price"] is None and api:
        # v5: no price element rendered: JD's own price API, read inside the logged-in page
        res["price"] = api.get("price")
        f = api.get("final_price")
        if f is not None and (res["price"] is None or res["price"] * 0.3 <= f <= res["price"] + 1e-9):
            res["final_price"] = f
        if api.get("plus_price"):
            plus.append(float(api["plus_price"]))
        res["via"] = "alt" if alt_used else "api"
        res["note"] = (f"页面没显示价格，价格由{ROUTE_CN.get(api.get('src'), '')}读出" if alt_used else
                       f"页面没显示价格，价格由京东价格接口读出（{api.get('src') or 'api'}）")
    if res["price"] is None and res["final_price"] is None:
        tp = text_prices(snap.get("kw") or [])     # v6: '到手价 ¥137' in the visible text near the top
        if tp["price"] is not None or tp["final_price"] is not None:
            res["price"] = tp["price"]
            f = tp["final_price"]
            if f is not None and (res["price"] is None or res["price"] * 0.3 <= f <= res["price"] + 1e-9):
                res["final_price"] = f
            res["via"], res["note"] = "text", "价格从页面文字里读出（价格元素没匹配上）"
        if tp["plus_price"]:
            plus.append(tp["plus_price"])
    if res["price"] is None and res["final_price"] is None:
        emb = embedded_price(snap.get("embedded") or [])   # v6: JSON in the page for this sku
        if emb:
            res["price"] = emb.get("price")
            f = emb.get("final_price")
            if f is not None and (res["price"] is None or res["price"] * 0.3 <= f <= res["price"] + 1e-9):
                res["final_price"] = f
            if emb.get("plus_price"):
                plus.append(float(emb["plus_price"]))
            res["via"], res["note"] = "embedded", f"价格从页面内嵌数据读出（{emb.get('src')}）"
    mob = snap.get("mobile") if isinstance(snap.get("mobile"), dict) else None
    if res["price"] is None and res["final_price"] is None and mob and not mob.get("skipped") and not mob.get("error"):
        m = parse_snapshot({**mob, "profile": {k: v for k, v in prof.items() if k != "logged_in"}})
        if m.get("effective") is not None and m.get("status") == "ok":   # v6: item.m.jd.com, same cookies
            res.update(price=m.get("price"), final_price=m.get("final_price"), via="mobile",
                       note="电脑版页面没显示价格，价格由移动版商品页读出")
            if m.get("plus_price"):
                plus.append(float(m["plus_price"]))
            if res["in_stock"] is None:
                res["in_stock"] = m.get("in_stock")
    # compare by the after-coupon price (券后/到手价, never PLUS-only); the list price only when there is none
    res["effective"] = res["final_price"] if res["final_price"] is not None else res["price"]
    if plus:
        res["plus_price"] = min(plus)
    if api and api.get("in_stock") is not None and not (snap.get("btn") or []):
        res["in_stock"] = bool(api["in_stock"])

    btns = snap.get("btn") or []
    buy = [b for b in btns if BUY_BTN.match((b.get("t") or "").strip())]
    if any(not b.get("disabled") for b in buy if (b.get("t") or "") != "立即预约"):
        res["in_stock"] = True
    if any((b.get("t") or "").strip() in ("到货通知", "已售罄", "无货", "暂时无货", "已抢光") for b in btns) or \
            (buy and all(b.get("disabled") for b in buy)):
        res["in_stock"] = False
    if res["in_stock"] is None and OUT_OF_STOCK.search(text[:3000]) and not buy:
        res["in_stock"] = False
    if res["effective"] is None:
        res.update(status="no_price", note="页面上没有读到价格" + (
            "（京东价格接口也没有返回价格）" if snap.get("api") else ""))
        lp = LOGIN_PRICE_RE.search(" ".join([text[:3000]] + [str(k.get("t") or "") for k in snap.get("kw") or []
                                                            if isinstance(k, dict)]))
        if lp:   # v6: the page hides the price until login
            res.update(status="need_login", note=f"商品页显示「{lp.group(0)}」，读价用的浏览器没有登录京东")
            return res
    if prof.get("logged_in") is False and (site_of(url) == "jd" or snap.get("jd_sku")):
        # v5: the DevTools cookie store has no pt_key / thor: this Chrome is not the logged-in one (到手价 needs login)
        res.update(status="need_login", note="读价用的浏览器没有登录京东（没有 pt_key / thor 登录 Cookie）"
                   + (f"，配置目录 {prof['dir']}" if prof.get("dir") else ""))
    return res


def evaluate(w: dict, r: dict) -> tuple[bool, bool, str]:
    """-> (hit, below_now, message). A hit is only the transition into "at or below target and buyable"."""
    try:
        target = float(w.get("value") or 0)
    except (TypeError, ValueError):
        target = 0.0
    eff = r.get("effective")
    below = r.get("status") == "ok" and eff is not None and eff <= target + 1e-9 and r.get("in_stock") is not False
    hit = below and not int(w.get("below") or 0)
    msg = (price_line(r, target) + " · 已到目标") if eff is not None else ""
    return hit, below, msg


def parse_payable(order: dict) -> Optional[float]:
    """确认订单 page snapshot -> the amount the user would pay (实付款 / 应付总额 …)."""
    lines = list(order.get("pay") or [])
    text = order.get("text") or ""
    for lab in ("实付款", "应付总额", "应付金额", "需付款", "实付", "订单总额", "合计", "总计"):
        for ln in lines + re.findall(lab + r"[^\n]{0,30}", text):
            if lab in ln:
                tail = ln.split(lab, 1)[1]
                v, _ = parse_price(tail)
                if v is None:
                    m = re.search(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", tail)
                    v = float(m.group(0)) if m else None
                if v is not None:
                    return v
    return None


# ----------------------------------------------------------------------------- store
def ensure_columns(store) -> None:
    with store.lock:
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(watches)")}
        for col, typ in COLUMNS:
            if col not in cols:
                store.db.execute(f"ALTER TABLE watches ADD COLUMN {col} {typ}")
        store.db.commit()


def watch_fields(d: dict, default_machine: Optional[str] = None) -> dict:
    """Validated columns for a shop watch (API body or agent tool args). Raises ValueError with a Chinese message."""
    url = normalize_url(str(d.get("url") or ""))
    site = site_of(url)
    if not site:
        raise ValueError("只支持京东（jd.com）、淘宝（taobao.com）、天猫（tmall.com）的商品链接")
    try:
        target = float(str(d.get("target_price") if d.get("target_price") not in (None, "") else d.get("value") or "").strip().lstrip("¥￥"))
    except ValueError:
        raise ValueError("目标价必须是数字")
    if target <= 0:
        raise ValueError("目标价必须大于 0")
    try:
        qty = max(1, min(99, int(d.get("quantity") or 1)))
    except (TypeError, ValueError):
        qty = 1
    try:
        interval = int(d.get("interval_min") or DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL
    name = str(d.get("name") or "").strip() or f"{SITE_NAMES[site]} ≤ ¥{target:g}"
    return {"name": name[:80], "url": url, "mode": MODE, "value": f"{target:g}", "site": site,
            "interval_min": max(MIN_INTERVAL, interval), "quantity": qty,
            "sku": str(d.get("sku") or "").strip()[:120], "address_note": str(d.get("address_note") or "").strip()[:200],
            "auto_order": 1 if d.get("auto_order") else 0,
            "machine_id": str(d.get("machine_id") or d.get("machine") or default_machine or "").strip() or None,
            "enabled": 0 if d.get("enabled") is False else 1}


def _is_cloud(m: dict) -> bool:
    return (m.get("provider") or "") == "instabox" or str(m.get("id") or "").startswith("cloud-")


def default_machine(store) -> Optional[str]:
    """Where a watch runs when nobody chose: the first local computer (home IP, the user's logged-in Chrome), never
    whichever computer happens to be busy or selected right now."""
    ms = store.machines() if store else []
    local = [m for m in ms if not _is_cloud(m)]
    return (local or ms or [{}])[0].get("id")


def resolve_machine(store, value) -> Optional[str]:
    """A computer the user named: its id, or its name ('本地vm', '云电脑 2'), or 本地/local for the only local one.
    None when nothing matches (the caller says so; it never swaps in another computer)."""
    v = str(value or "").strip()
    if not v or not store:
        return None
    ms = store.machines()
    for m in ms:
        if m["id"] == v:
            return v
    key = re.sub(r"\s+", "", v).lower()
    for m in ms:
        for n in (m.get("name"), m.get("custom_name"), m.get("default_name"), m.get("raw_name")):
            if n and re.sub(r"\s+", "", str(n)).lower() == key:
                return m["id"]
    if re.fullmatch(r"(本地|本机|家里|local)(的)?(vm|虚拟机|电脑|机器|computer)?", key):
        local = [m for m in ms if not _is_cloud(m)]
        if len(local) == 1:
            return local[0]["id"]
    return None


def machine_label(store, mid: Optional[str]) -> str:
    return ((store.machine(mid) or {}).get("name") if (store and mid) else None) or (mid or "未指定的电脑")


# ----------------------------------------------------------------------------- running the probe on the computer
# chrome_cdp.py (stdlib, in the vm-agent package) keeps a DevTools port open on the computer's own logged-in Chrome.
# The API ships it over the shell like the probe, so it works before the computer's vm-agent is updated.
CDP_TOOL_SRC = Path(__file__).resolve().parents[3] / "vm-agent" / "vm_agent" / "browser" / "chrome_cdp.py"
CDP_TOOL_REMOTE = "$HOME/.config/aiw/chrome_cdp.py"
UPDATE_CMD = "curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/update-agent.sh | bash"
OPT_RETRY, OPT_PAUSE = "立即重试", "暂停这个监控"


def ship_command(src: Path, remote: str, sub: str, **kw) -> str:
    """Write src to remote on the computer (only when its sha1 differs) and run `python3 remote sub --k v ...`."""
    try:
        data = src.read_bytes()
    except OSError:   # an image without the file: say so as JSON instead of failing the whole check
        return f"echo {_q(json.dumps({'ok': False, 'reason': 'tool_missing', 'error': f'{src.name} is not in this AIW server image'}))}"
    # 3.9.10: gzip first. One argument to /bin/sh -c may be at most 128 KiB (MAX_ARG_STRLEN); probe v10 is 110 KB,
    # 147 KB in base64, and every check failed with 'OSError: [Errno 7] Argument list too long'
    b64 = base64.b64encode(gzip.compress(data, 9, mtime=0)).decode()
    digest = hashlib.sha1(data).hexdigest()
    args = " ".join(f"--{k.replace('_', '-')} {_q(str(v))}" for k, v in kw.items()
                    if v not in (None, "", False) and v is not True and not isinstance(v, (list, tuple)))
    for k, v in kw.items():
        if isinstance(v, (list, tuple)):
            args += "".join(f" --{k.replace('_', '-')} {_q(str(x))}" for x in v)
    args += "".join(f" --{k.replace('_', '-')}" for k, v in kw.items() if v is True)
    d = remote.rsplit("/", 1)[0]
    return (f'mkdir -p "{d}" && f="{remote}" && '
            f'if [ "$(sha1sum "$f" 2>/dev/null | cut -d" " -f1)" != "{digest}" ]; then '
            f"echo '{b64}' | base64 -d | gzip -dc > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"; fi && python3 \"$f\" {sub} {args}")


def probe_command(sub: str, **kw) -> str:
    return ship_command(PROBE_SRC, REMOTE, sub, **kw)


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _last_json(r) -> Optional[dict]:
    res = getattr(r, "result", None) or {}
    for line in reversed((res.get("stdout") or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _err_of(r) -> str:
    res = getattr(r, "result", None) or {}
    return str(res.get("stderr") or getattr(r, "error", None) or "no output")[-300:]


def _store():
    from . import browseros as _bos
    return _bos._STORE


def known_port(mid: Optional[str]) -> Optional[int]:
    """The DevTools port chrome_cdp last found or opened on this computer (kv shop_cdp_port {mid: port})."""
    st = _store()
    if not st or not mid:
        return None
    try:
        return int((st.kv_get("shop_cdp_port", {}) or {}).get(mid) or 0) or None
    except (TypeError, ValueError):
        return None


def remember_port(mid: Optional[str], port: Optional[int]) -> None:
    st = _store()
    if not st or not mid:
        return
    d = dict(st.kv_get("shop_cdp_port", {}) or {})
    if port:
        d[mid] = int(port)
    else:
        d.pop(mid, None)
    st.kv_set("shop_cdp_port", d)


def cdp_env(computer, port: Optional[int] = None) -> str:
    from . import browseros as _bos   # BrowserOS on: read pages over its DevTools port, where the user is logged in
    if port:
        return f"VM_AGENT_CDP=http://127.0.0.1:{int(port)} "
    env = _bos.probe_env(computer)
    if env:
        return env
    p = known_port(getattr(computer, "machine_id", None))
    return f"VM_AGENT_CDP=http://127.0.0.1:{p} " if p and p != 9222 else ""


STAGE_LABELS = {"start": "开始检查", "machine": "确认电脑在线", "push": "推送探针", "ensure": "打开浏览器调试端口",
                "vision": "视觉模型读截图", "save": "保存结果", "done": "完成"}


class Progress:
    """Stages of one check, timed; each new stage goes to cb (the 立即检查 job the card polls)."""

    def __init__(self, cb: Optional[Callable[[dict], Any]] = None):
        self.cb, self.t0, self.stages = cb, time.time(), []

    def __call__(self, stage: str, label: Optional[str] = None, **kw) -> None:
        label = label or STAGE_LABELS.get(stage, stage)
        if self.stages and self.stages[-1]["stage"] == stage and self.stages[-1]["label"] == label:
            return
        now_ms = int((time.time() - self.t0) * 1000)
        if self.stages and "ms" not in self.stages[-1]:
            self.stages[-1]["ms"] = max(0, now_ms - self.stages[-1]["at_ms"])
        self.stages.append({"stage": stage, "label": label, "at_ms": now_ms, **kw})
        if self.cb:
            try:
                self.cb({"stage": stage, "label": label, **kw})
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> list:
        if self.stages and "ms" not in self.stages[-1]:
            self.stages[-1]["ms"] = max(0, int((time.time() - self.t0) * 1000) - self.stages[-1]["at_ms"])
        return self.stages


PROGRESS_POLL_S = 1.5
PROGRESS_FIRST_S = 1.0


async def _poll_progress(computer, path: str, prog: "Progress") -> None:
    """While the probe runs, read the stage it writes to path and pass it on (best effort; never fails the check)."""
    seen = None
    await asyncio.sleep(PROGRESS_FIRST_S)
    while True:
        try:
            r = await computer.execute("shell.exec", {"command": f"cat {path} 2>/dev/null || true", "timeout": 5}, timeout=8)
            got = _last_json(r)
            if got and got.get("stage") and (got.get("stage"), got.get("label")) != seen:
                seen = (got.get("stage"), got.get("label"))
                prog(str(got["stage"]), str(got.get("label") or got["stage"]), src="probe")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(PROGRESS_POLL_S)


async def run_probe(computer, sub: str, timeout: float = 60, cdp_port: Optional[int] = None,
                    progress: Optional["Progress"] = None, **kw) -> dict:
    poller, path = None, ""
    if sub == "probe":
        kw.setdefault("budget", str(int(min(PROBE_BUDGET, max(20, timeout - 40)))))
        path = f"/tmp/aiw-shop-run-{os.urandom(4).hex()}.json"
        kw["progress"] = path
        if progress is not None:
            poller = asyncio.ensure_future(_poll_progress(computer, path, progress))
    r = None
    try:
        r = await computer.execute("shell.exec", {"command": cdp_env(computer, cdp_port) + probe_command(sub, **kw),
                                                  "timeout": timeout}, timeout=timeout + 15)
    except Exception as e:  # noqa: BLE001  (the agent's own timeout / a dropped connection): keep what the probe wrote
        r = type("R", (), {"result": {"stderr": f"{type(e).__name__}: {e}"}, "error": str(e)})()
    finally:
        if poller is not None:
            poller.cancel()
    got = _last_json(r)
    if got is not None:
        if sub == "probe":
            got.setdefault("pushed_version", PROBE_VERSION)
        return got
    out = {"error": f"probe failed: {_err_of(r)}"}
    if sub == "probe":
        # v8: the probe was pushed in this very command, so its version is known; the stages it got through are in
        # its progress file (it is killed before it can print): the card says WHERE it stopped, not just "timeout"
        out["pushed_version"] = PROBE_VERSION
        part = await _read_partial(computer, path)
        if part:
            out["stages"] = part.get("stages") or []
            out["probe_version"] = str(part.get("probe_version") or "") or None
            cur = (out["stages"][-1] if out["stages"] else {}) or {}
            if cur:
                out["stage_error"] = f"卡在「{cur.get('label') or cur.get('stage')}」"
    return out


async def _read_partial(computer, path: str) -> Optional[dict]:
    if not path:
        return None
    try:
        r = await computer.execute("shell.exec", {"command": f"cat {path} 2>/dev/null; rm -f {path} {path}.tmp", "timeout": 5},
                                   timeout=8)
        return _last_json(r)
    except Exception:  # noqa: BLE001
        return None


ERR_CN = ((re.compile(r"probe failed: timeout after (\d+)s?", re.I), lambda m: f"检查超时（{m.group(1)} 秒没有结束）"),
          (re.compile(r"timeout after (\d+(?:\.\d+)?)s?", re.I), lambda m: f"超时（{m.group(1)} 秒）"),
          (re.compile(r"^probe failed: no output$", re.I), lambda m: "探针没有输出（电脑上的 python3 没运行起来？）"),
          (re.compile(r"^probe failed:\s*", re.I), lambda m: "检查失败："),
          (re.compile(r"NO_CDP:?\s*", re.I), lambda m: "连不上浏览器调试端口："),
          (re.compile(r"Failed to fetch", re.I), lambda m: "请求被浏览器拦下"),
          (re.compile(r"ConnectionError|ConnectionResetError|websocket closed", re.I), lambda m: "连接断开"))


def cn_error(text: str) -> str:
    """v8: the status / error line in plain Chinese ('probe failed: timeout after 160s' -> '检查超时（160 秒没有结束）')."""
    t = str(text or "")
    for rx, f in ERR_CN:
        t = rx.sub(f, t)
    return t


async def run_cdp_tool(computer, sub: str, timeout: float = 150, **kw) -> dict:
    """chrome_cdp.py on the computer: ensure / open-url / close-tab / status. Never raises."""
    try:
        r = await computer.execute("shell.exec", {"command": ship_command(CDP_TOOL_SRC, CDP_TOOL_REMOTE, sub, **kw),
                                                  "timeout": timeout}, timeout=timeout + 15)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "tool_failed", "error": f"{type(e).__name__}: {e}"[:300]}
    got = _last_json(r)
    if got is not None:
        return got
    return {"ok": False, "reason": "tool_failed", "error": _err_of(r)}


async def install_cdp_tool(computer) -> bool:
    """Put chrome_cdp.py at ~/.config/aiw/chrome_cdp.py (probe.py submit uses it to bring the order tab to the
    front). Best effort: a computer without it still works, the tab is just not raised."""
    res = await run_cdp_tool(computer, "version", timeout=20)
    return res.get("reason") not in ("tool_failed", "tool_missing")


async def ensure_cdp(computer, mid: Optional[str], port: int = 9222) -> dict:
    """Find a DevTools endpoint on the computer, or reopen the user's Chrome with one (keeping its login)."""
    res = await run_cdp_tool(computer, "ensure", timeout=150, port=port, also=[9110])
    if res.get("ok") and res.get("port"):
        remember_port(mid, res["port"])
    return res


async def _computer(sessions, mid: str):
    cloud = getattr(sessions, "cloud", None)
    if cloud and cloud.is_cloud(mid):
        await cloud.wake(mid)
    return sessions.computer(mid)


async def vision_read(vision, shot_b64: str, site: str) -> Optional[dict]:
    """OCR/vision fallback when the DOM gave no price (or there is no DOM: the screen-only path)."""
    if not vision or not shot_b64:
        return None
    raw = await vision.describe(
        "你在读电商商品页截图。只输出 JSON。",
        f"这是{SITE_NAMES.get(site, '电商')}商品详情页的截图。读出售价和到手价/券后价（没有就 null）以及是否有货；"
        "PLUS 会员价、划线价、运费不算。page 说明截图里是什么页面。"
        '输出：{"price": 数字或null, "final_price": 数字或null, "in_stock": true/false/null, '
        '"page": "product" | "login" | "captcha" | "other", "title": "商品名或空"}', shot_b64, max_tokens=250)
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    out = {}
    for k in ("price", "final_price"):
        try:
            out[k] = float(d[k]) if d.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            out[k] = None
    out["in_stock"] = d.get("in_stock") if isinstance(d.get("in_stock"), bool) else None
    out["page"] = str(d.get("page") or "product").lower()
    out["title"] = str(d.get("title") or "")[:120]
    return out


SCREEN_WAIT = 9     # seconds for the page to render in the visible browser before the screenshot


async def desktop_read(computer, sessions, w: dict) -> Optional[dict]:
    """Last resort without any DevTools port: open the link as a tab of the browser on screen, screenshot the desktop,
    let the vision model read it, and close that tab again. None when there is no vision model or it read nothing."""
    vision = sessions._vision_llm() if hasattr(sessions, "_vision_llm") else None
    if not vision:
        return None
    site = w.get("site") or site_of(w["url"]) or ""
    opened = await run_cdp_tool(computer, "open-url", timeout=40, url=w["url"])
    if not opened.get("ok"):
        return None
    await asyncio.sleep(SCREEN_WAIT)
    shot = None
    try:
        s = await computer.screenshot()
        shot = getattr(s, "screenshot", None)
    except Exception as e:  # noqa: BLE001
        log.info("desktop_read screenshot: %s", e)
    v = await vision_read(vision, shot or "", site) if shot else None
    marks = {"jd": ["京东", "JD"], "taobao": ["淘宝", "taobao"], "tmall": ["天猫", "tmall"]}.get(site, [])
    if v and v.get("title"):
        marks.append(v["title"][:12])
    await run_cdp_tool(computer, "close-tab", timeout=20, title_has=marks)
    if not v:
        return None
    base = {"price": None, "final_price": None, "effective": None, "in_stock": v.get("in_stock"),
            "title": v.get("title") or "", "url": w["url"], "via": "screen"}
    if v["page"] == "login":
        return {**base, "status": "login", "note": "需要登录（从屏幕截图看到的是登录页）"}
    if v["page"] == "captcha":
        return {**base, "status": "captcha", "note": "出现了滑块/安全验证（从屏幕截图看到）"}
    eff = v.get("final_price") or v.get("price")
    if not eff:
        return None
    return {**base, "status": "ok", "price": v.get("price"), "final_price": v.get("final_price"), "effective": eff,
            "note": "浏览器调试端口不可用，价格由屏幕截图读出"}


# ----------------------------------------------------------------------------- the scheduled check
LIVE_TASK = ("queued", "running", "paused", "takeover", "waiting_confirm", "waiting_input")
FAIL_STATES = ("error", "no_price", "no_browser")
PROBE_TIMEOUT = 200         # hard cap for one probe run (the shell command is killed after this)
PROBE_BUDGET = 150          # v8: the probe's own budget; it stops each stage in time and answers well before the cap
RISK_BACKOFF_MIN = 2 * 3600  # JD's 活动火爆 page: look again much later, more checks only keep it on


# v7: JD's limiter is per computer (its IP / browser), so the cool-down is too: every watch on that computer waits.
RISK_COOL_BASE = int(os.environ.get("AIW_SHOP_RISK_COOL_MIN", "45")) * 60    # 45 min, then 90, 180 ... (max 12 h)
RISK_COOL_MAX = 12 * 3600


def jitter(seconds: float, frac: float = 0.15, rnd: Optional[Callable[[], float]] = None) -> float:
    """seconds +-frac (so checks never line up on a fixed beat JD can see)."""
    r = (rnd or random.random)()
    return round(max(60.0, seconds * (1 - frac + 2 * frac * r)), 3)


def risk_cooldown(level: int, rnd: Optional[Callable[[], float]] = None) -> float:
    return jitter(min(RISK_COOL_BASE * (2 ** max(0, level - 1)), RISK_COOL_MAX), 0.2, rnd)


def risk_state(store, mid: str) -> dict:
    try:
        return store.kv_get(f"shop_risk:{mid}", None) or {}
    except Exception:  # noqa: BLE001
        return {}


def risk_cooling(store, mid: str, now: float) -> Optional[dict]:
    st = risk_state(store, mid)
    return st if float(st.get("until") or 0) > now else None


def risk_hit(store, mid: str, now: float, alt_ok: bool, url: str = "") -> dict:
    """One more limit page (or redirect to the mobile page) on this computer: the cool-down doubles."""
    st = risk_state(store, mid)
    # a limit page long after the last cool-down ended starts over at level 1
    level = 1 if now - float(st.get("until") or 0) > 24 * 3600 else int(st.get("level") or 0) + 1
    st = {"level": level, "at": now, "until": now + risk_cooldown(level), "alt_ok": bool(alt_ok), "url": (url or "")[:200]}
    store.kv_set(f"shop_risk:{mid}", st)
    return st


def risk_alt(store, mid: str, alt_ok: bool) -> None:
    st = risk_state(store, mid)
    if st:
        st["alt_ok"] = bool(alt_ok)
        store.kv_set(f"shop_risk:{mid}", st)


def risk_clear(store, mid: str) -> None:
    if risk_state(store, mid):
        store.kv_set(f"shop_risk:{mid}", {})


def mins_left(until: float, now: float) -> int:
    return max(1, int(round((until - now) / 60)))


def risk_backoff(interval_s: int, fails: int) -> int:
    return min(max(interval_s * 4, RISK_BACKOFF_MIN) * (2 ** min(max(fails - 1, 0), 2)), 12 * 3600)


def _backoff(interval_s: int, fails: int) -> int:
    return min(interval_s * (2 ** min(fails, 4)), 6 * 3600)


REASON_CN = {
    "port_not_opened": "Chrome 已经按带调试端口的方式重新打开，但端口一直没有响应",
    "port_not_answering": "Chrome 带着调试端口在运行，但端口不响应（可能卡住了）",
    "profile_locked": "Chrome 关掉后它的用户配置一直被占用，没法重新打开",
    "no_browser_installed": "这台电脑上没有安装 Chrome / Chromium",
    "no_display": "这台电脑的桌面（DISPLAY）没有运行，浏览器开不起来",
    "not_running": "Chrome 没有在运行",
    "tool_failed": "在这台电脑上检查浏览器的脚本没能运行",
}


def no_browser_text(machine: str, bname: str, ens: Optional[dict], raw_note: str = "") -> str:
    """What the user reads when no page could be opened at all: what is wrong and what fixes it (never a raw urlopen error)."""
    ens = ens or {}
    why = REASON_CN.get(str(ens.get("reason") or ""), "")
    detail = str(ens.get("error") or raw_note or "").strip()
    lines = [f"「{machine}」上的 {bname} 读不了网页，这次没能检查价格。"]
    lines.append("原因：" + (why or "浏览器的调试端口（9222）连不上") + (f"（{detail[:160]}）" if detail and detail not in why else ""))
    if ens.get("reason") == "no_browser_installed":
        lines.append(f"解决：在这台电脑上安装 Chromium（sudo apt install chromium），然后运行：\n{UPDATE_CMD}")
    elif ens.get("reason") == "no_display":
        lines.append("解决：在 AIW 里打开这台电脑的接管桌面确认桌面在运行（或重启这台电脑），然后点「立即重试」。")
    else:
        lines.append(f"解决：在这台电脑的终端里（用户 aiw）运行下面这行，它会让 Chrome 以后总是带着调试端口启动，"
                     f"登录状态会保留：\n{UPDATE_CMD}\n运行完点「立即重试」。也可以先在接管桌面里把 Chrome 关掉再点重试。")
    return "\n".join(lines)


USER_STATES = ("login", "need_login", "captcha", "risk_limit")   # the user (or time) has to clear these


def risk_text(machine: str, bname: str = "Chrome", minutes: Optional[int] = None) -> str:
    wait = f"约 {minutes} 分钟后再试，之后每次被限流冷却时间翻倍" if minutes else "冷却 30–60 分钟后再试，之后每次被限流冷却时间翻倍"
    return (f"京东风控限流：「{machine}」被京东限制访问（活动火爆页：“活动火爆 请选购其他商品”）。这不是验证码，没有要完成的验证。"
            f"已自动换方式（京东签名接口 / 购物车 / 我的关注）读价，读不到就稍后重试：{wait}，冷却期间不打开商品页。"
            f"可以在「{machine}」的接管桌面里用 {bname} 手动打开一次这个商品看看；频繁刷新只会让限制更久。"
            "把商品加入购物车或关注它，换方式读价会更稳。")


DIAG_STATES = ("no_price", "risk_limit", "need_login", "login", "captcha", "error", "no_browser")


def _diag_dir(sessions) -> Path:
    base = getattr(getattr(sessions, "settings", None), "artifacts_dir", None) or os.environ.get("AIW_ARTIFACTS") \
        or "workspace/artifacts"
    return Path(base) / "shop_diag"


def diag_shot_path(sessions, wid: str) -> Path:
    return _diag_dir(sessions) / f"{re.sub(r'[^A-Za-z0-9_-]', '', str(wid))}.jpg"


def diag_hints(raw: dict, r: dict) -> list[str]:
    """Plain reasons (Chinese) read off the probe's diagnosis: what most likely kept the price from being read."""
    h = []
    pv = str(raw.get("probe_version") or "")
    if pv and pv != PROBE_VERSION:
        h.append(f"电脑上运行的探针是 v{pv}，服务器带的是 v{PROBE_VERSION}（推送没成功？）")
    if raw.get("stage_error"):
        h.append("这次检查" + str(raw["stage_error"]) + "，已返回到这一步为止读到的内容")
    if raw.get("skeleton"):
        h.append("商品页只有京东通用标题、没有商品模块（骨架页/软拦截），已直接改用签名接口")
    if raw.get("risk_limit"):
        h.append("京东返回了 活动火爆 风控页（limit.html）")
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    if raw.get("mobile_blocked"):
        h.append("京东把商品页跳到了移动版商品页（item.m.jd.com，限流的就是它），探针已拦截没有打开")
    if env.get("ua_override"):
        h.append("浏览器的 User-Agent 像手机（京东会把手机 UA 跳到移动版），这次已改成电脑版 UA")
    if env.get("webdriver"):
        h.append("浏览器报告 navigator.webdriver=true（像是带自动化参数启动），已在页面里改成 false")
    res = raw.get("resolved") if isinstance(raw.get("resolved"), dict) else None
    if res and not res.get("sku"):
        h.append("短链接没能解析出商品编号（" + str(res.get("error") or "") + "）")
    for a in raw.get("alt") or []:
        if not isinstance(a, dict) or a.get("price"):
            continue
        name = {"signed": "京东签名接口", "cart": "购物车", "follow": "我的关注", "tab_cart": "已打开的购物车页",
                "tab_follow": "已打开的我的关注页"}.get(a.get("route"), a.get("route"))
        why = ("没登录" if a.get("login") else "风控页" if a.get("risk_limit") else
               "页面没有响应（一直没加载完）" if a.get("missing") == "no_answer" or a.get("no_answer") else
               "列表里没有这个商品" if a.get("missing") == "not_listed" else
               (a.get("refused") or a.get("error") or (f"HTTP {a.get('status')}" if a.get("status") else "没读到")))
        h.append(f"{name}：{str(why)[:60]}")
    if r.get("status") in ("need_login", "login") or (raw.get("profile") or {}).get("logged_in") is False:
        h.append("读价用的浏览器没有京东登录 Cookie（pt_key / thor）")
    if raw.get("hung"):
        h.append("商品页标签卡住不响应（一直没加载完），已关掉它，换新标签页读购物车 / 我的关注")
    if r.get("status") == "no_price":
        if raw.get("title_from") == "document.title" and not raw.get("sel") and not raw.get("cand"):
            h.append("商品信息区没有渲染出来（标题只来自 document.title，没有任何价格元素）")
        if raw.get("visibility") and raw.get("visibility") != "visible" and not raw.get("fronted"):
            h.append(f"商品页标签一直在后台（visibility={raw.get('visibility')}），没能切到前台")
        if raw.get("n_elems") is not None and int(raw.get("n_elems") or 0) < 300:
            h.append(f"页面元素很少（{raw.get('n_elems')} 个），像是白屏或没加载完")
        if raw.get("net_seen") == 0:
            h.append("页面自己没有请求任何价格接口（价格模块没加载）")
        elif raw.get("net") and not any(n.get("price") for n in raw.get("net") or []):
            h.append("页面自己的价格接口有返回，但里面没读出价格")
        if raw.get("api") and not raw.get("api_price"):
            h.append("直接调用的京东价格接口都没返回价格（多半已需要 h5st 签名或已停用）")
        mob = raw.get("mobile") if isinstance(raw.get("mobile"), dict) else {}
        if mob.get("risk_limit"):
            h.append("移动版商品页是 活动火爆 风控页，没用它")
        elif mob and not mob.get("error") and not mob.get("skipped"):
            h.append("移动版商品页也没读到价格")
    return h


def build_diag(raw: dict, r: dict, prog: Optional[Progress], now: float, shot: bool) -> dict:
    """What the card's 查看诊断 shows and the check JSON returns: enough to tell why the next failure happened."""
    raw = raw if isinstance(raw, dict) else {}
    prof = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
    mob = raw.get("mobile") if isinstance(raw.get("mobile"), dict) else None
    d = {"at": now, "status": r.get("status"), "note": r.get("note"), "via": r.get("via"),
         "probe_version": raw.get("probe_version") or raw.get("pushed_version") or PROBE_VERSION,
         "probe_answered": bool(raw.get("probe_version")), "server_probe_version": PROBE_VERSION,
         "stage_error": raw.get("stage_error"), "timed_out": bool(raw.get("timed_out") or raw.get("stage_error")),
         "budget_s": raw.get("budget_s"), "item_name": raw.get("item_name"),
         "server_version": os.environ.get("AIW_VERSION", "dev")[:7],
         "final_url": raw.get("url") or "", "asked_url": raw.get("asked_url") or "", "page_title": raw.get("doc_title") or raw.get("title") or "",
         "title": raw.get("title") or "", "title_from": raw.get("title_from"), "ready": raw.get("ready"),
         "visibility": raw.get("visibility"), "focus": raw.get("focus"), "fronted": raw.get("fronted"),
         "n_elems": raw.get("n_elems"), "shadow_roots": raw.get("shadow_roots"),
         "price_elements": [{k: x.get(k) for k in ("role", "q", "t")} for x in (raw.get("sel") or [])[:8]],
         "price_texts": [x.get("t") for x in (raw.get("cand") or [])[:8]],
         "snippets": (raw.get("snippets") or [])[:10],
         "api": [{k: a.get(k) for k in ("src", "status", "error", "jsonp") if k in a} for a in raw.get("api") or [] if isinstance(a, dict)],
         "net_seen": raw.get("net_seen"), "net": (raw.get("net") or [])[:12],
         "mobile": ({k: mob.get(k) for k in ("asked_url", "url", "title", "risk_limit", "login", "skipped", "error", "ready")}
                    | {"price_texts": [x.get("t") for x in (mob.get("cand") or [])[:5]]}) if mob else None,
         "profile": {k: prof.get(k) for k in ("port", "dir", "logged_in", "jd_cookies", "login_cookies", "cookies_total") if k in prof},
         "cdp": raw.get("cdp"), "error": cn_error(raw.get("error") or raw.get("shot_error") or "") or None,
         "route": raw.get("route"), "env": raw.get("env"), "nav_chain": (raw.get("nav_chain") or [])[:12],
         "mobile_blocked": raw.get("mobile_blocked"), "resolved": raw.get("resolved"),
         "alt": [{k: a.get(k) for k in ("route", "price", "status", "error", "refused", "login", "missing", "rows", "url",
                                        "signed", "risk_limit", "from") if k in a}
                 for a in raw.get("alt") or [] if isinstance(a, dict)],
         "cooldown": r.get("cooldown"),
         "probe_stages": raw.get("stages") or [], "stages": prog.close() if prog else [],
         "screenshot": bool(shot)}
    d["hints"] = diag_hints(raw, r)
    return d


def save_diag(store, sessions, wid: str, raw: dict, d: dict) -> None:
    """Keep the last diagnosis per watch (kv shop_diag:<wid>) and, on a failure, the probe's screenshot."""
    shot = raw.get("screenshot") if isinstance(raw, dict) else None
    if shot and d.get("status") in DIAG_STATES:
        try:
            pth = diag_shot_path(sessions, wid)
            pth.parent.mkdir(parents=True, exist_ok=True)
            pth.write_bytes(base64.b64decode(shot))
            d["screenshot"] = True
            d["screenshot_url"] = f"/api/watches/{wid}/diag.jpg"
        except Exception as e:  # noqa: BLE001
            d["screenshot"] = False
            d["screenshot_error"] = str(e)[:120]
    else:
        d["screenshot"] = False
    try:
        store.kv_set(f"shop_diag:{wid}", d)
        store.update("watches", wid, diag_at=d.get("at"))
    except Exception:  # noqa: BLE001
        log.info("save_diag %s failed", wid, exc_info=True)


def repair_titles(store, rows: Optional[list] = None) -> list:
    """v8: watches whose shown name became JD's site-wide title ('京东(JD.COM)-正品低价…', from a page that never
    rendered): the stored item_title is cleared (the user's own name shows again) and a generic name falls back to
    '京东商品 <sku>'. Returns the rows as they are now. Idempotent; touches only those rows."""
    rows = list(rows if rows is not None else store.all("watches", order="created_at ASC"))
    out = []
    for w in rows:
        fix = {}
        if w.get("mode") == MODE and w.get("item_title") and is_generic_title(w["item_title"]):
            fix["item_title"] = None
        if w.get("mode") == MODE and is_generic_title(w.get("name") or ""):
            sku = re.search(r"/(\d{5,})\.html", w.get("url") or "")
            fix["name"] = f"{SITE_NAMES.get(w.get('site') or 'jd', '京东')}商品 {sku.group(1)}" if sku else \
                f"{SITE_NAMES.get(w.get('site') or 'jd', '京东')} ≤ ¥{w.get('value')}"
        if fix:
            try:
                store.update("watches", w["id"], **fix)
            except Exception:  # noqa: BLE001
                log.info("repair_titles %s failed", w.get("id"), exc_info=True)
            w = {**w, **fix}
        out.append(w)
    return out


def get_diag(store, wid: str) -> Optional[dict]:
    return store.kv_get(f"shop_diag:{wid}", None)


async def check(sched, w: dict, now: Optional[float] = None, manual: bool = False,
                progress: Optional[Callable[[dict], Any]] = None) -> dict:
    store, sessions, notifier = sched.store, sched.sessions, sched.notifier
    ensure_columns(store)
    now = now or time.time()
    prog = Progress(progress)
    interval = max(MIN_INTERVAL, int(w.get("interval_min") or DEFAULT_INTERVAL)) * 60
    _sync_order_status(store, w)
    if manual and int(w.get("fail_count") or 0):   # v6: the user asked now: the 失败 N 次 自动退避 starts over
        store.update("watches", w["id"], fail_count=0, next_check=None)
        w = {**w, "fail_count": 0}
    mid = w.get("machine_id")
    if not mid:   # an old watch without a computer: pin one for good (it used to follow settings.machines[0])
        mid = default_machine(store) or (sessions.settings.machines[0].id if getattr(sessions.settings, "machines", None)
                                         else "ai-linux-001")
        store.update("watches", w["id"], machine_id=mid)
        w = {**w, "machine_id": mid}
    if not store.machine(mid):   # 3.8.2: never borrow another computer
        r = {"status": "no_machine", "note": f"设定的电脑「{mid}」已经不在了：请在价格监控里换一台电脑",
             "price": None, "final_price": None, "effective": None, "in_stock": None, "title": ""}
        return await apply_result(store, sessions, notifier, w, r, now, interval, mid, manual=manual)
    prog("machine")
    wait = await _machine_wait(sessions, mid)
    if wait:
        res = await _defer(store, notifier, w, mid, wait, now, manual)
        res["stages"] = prog.close()
        return res
    url = normalize_url(w.get("url") or "")
    if url and url != w.get("url"):   # an old watch saved with a mobile / share link: probe the desktop item page
        store.update("watches", w["id"], url=url)
        w = {**w, "url": url}
    is_jd = (w.get("site") or site_of(w["url"])) == "jd"
    route = ""
    cool = risk_cooling(store, mid, now) if is_jd else None
    if cool:
        # v7: JD limited this computer a moment ago. Scheduled checks touch JD again only by the other route and only
        # while that route works; the item page waits for the cool-down to end. 立即检查 tries the other route once.
        if not manual and not cool.get("alt_ok"):
            res = await _risk_wait(store, w, mid, cool, now)
            res["stages"] = prog.close()
            return res
        route = "alt"
    ens, raw = None, {}
    try:
        computer = await _computer(sessions, mid)
        prog("push")
        raw = await run_probe(computer, "probe", timeout=PROBE_TIMEOUT, progress=prog, url=w["url"], sku=w.get("sku") or "",
                              route=route)
        if str(raw.get("error", "")).startswith("NO_CDP"):
            # (1) any other answering endpoint, (2) or the user's own Chrome reopened with the port, login kept
            prog("ensure")
            ens = await ensure_cdp(computer, mid)
            if not ens.get("ok") and _bname(store, mid) == "BrowserOS":
                await computer.execute("computer.open_app", {"name": "browseros"}, timeout=30)
                await asyncio.sleep(6)
                ens = await ensure_cdp(computer, mid)
            if ens.get("ok"):
                prog("push")
                raw = await run_probe(computer, "probe", timeout=PROBE_TIMEOUT, cdp_port=ens.get("port"), progress=prog,
                                      url=w["url"], sku=w.get("sku") or "", route=route)
                if ens.get("how") in ("relaunched", "launched"):
                    await notifier.notify("watch", f"已重新打开 Chrome（带调试端口）：{w['name']}",
                                          f"「{(store.machine(mid) or {}).get('name') or mid}」上的 Chrome 之前没开调试端口，"
                                          "AIW 已把它关掉再按原来的配置重新打开（标签页和登录状态保留），以后会一直带着端口启动。",
                                          link=w["url"], push=False)
        r = parse_snapshot(raw)
        mname = (store.machine(mid) or {}).get("name") or mid
        if is_jd and isinstance(raw, dict) and not raw.get("error"):
            r = _risk_update(store, w, mid, raw, r, route, now)
        if r["status"] == "need_login":
            r["note"] = f"「{mname}」上的 {_bname(store, mid)} " + r["note"].replace("读价用的浏览器", "")
        elif r["status"] == "risk_limit":
            r["note"] = r["note"].replace("这台电脑", f"「{mname}」")
        if r["status"] == "no_browser":
            # (3) no DevTools at all: read the visible browser through the screen
            seen = await desktop_read(computer, sessions, w)
            if seen:
                r = {**r, **seen, "range": False}
            else:
                r["note"] = no_browser_text((store.machine(mid) or {}).get("name") or mid, _bname(store, mid), ens, r.get("note") or "")
        elif r["status"] == "no_price":
            vision = sessions._vision_llm() if hasattr(sessions, "_vision_llm") else None
            if vision:
                prog("vision")
                shot = raw.get("screenshot") or ""
                old = str(raw.get("probe_version") or "0")
                if not shot and old.isdigit() and int(old) < 6:   # an old probe without the v6 failure screenshot
                    raw2 = await run_probe(computer, "probe", timeout=PROBE_TIMEOUT + 10, cdp_port=(ens or {}).get("port"),
                                           url=w["url"], sku=w.get("sku") or "", shot=True)
                    shot = raw2.get("screenshot") or ""
                    if shot and not raw.get("screenshot"):
                        raw["screenshot"] = shot
                v = await vision_read(vision, shot, w.get("site") or site_of(w["url"]) or "")
                if v and (v.get("final_price") or v.get("price")):
                    r.update(price=v.get("price"), final_price=v.get("final_price"), status="ok", note="价格由视觉模型读出",
                             effective=v.get("final_price") or v.get("price"), via="vision")
                    if r.get("in_stock") is None:
                        r["in_stock"] = v.get("in_stock")
            if r["status"] == "no_price":
                hints = diag_hints(raw, r)
                if hints:
                    r["note"] = (r.get("note") or "页面上没有读到价格") + "：" + "；".join(hints[:2])
    except Exception as e:  # noqa: BLE001
        r = {"status": "error", "note": f"{type(e).__name__}: {e}"[:300], "price": None, "final_price": None,
             "effective": None, "in_stock": None, "title": ""}
    prog("save")
    diag = build_diag(raw, r, prog, now, bool(raw.get("screenshot")) if isinstance(raw, dict) else False)
    save_diag(store, sessions, w["id"], raw if isinstance(raw, dict) else {}, diag)
    res = await apply_result(store, sessions, notifier, w, r, now, interval, mid, manual=manual)
    res["diag"] = diag
    res["stages"] = diag["stages"]
    return res


def _risk_update(store, w: dict, mid: str, raw: dict, r: dict, route: str, now: float) -> dict:
    """v7: keep the computer's cool-down in step with what this run met, and say it on the card."""
    sku = str(raw.get("jd_sku") or (raw.get("resolved") or {}).get("sku") or "")
    if sku.isdigit() and not normalize_jd(w.get("url") or "").startswith("https://item.jd.com/"):
        store.update("watches", w["id"], url=f"https://item.jd.com/{sku}.html")   # a short link, resolved for good
    if route == "alt":
        st = risk_state(store, mid)
        risk_alt(store, mid, r.get("status") == "ok")
        until = float(st.get("until") or now)
        if r.get("status") == "ok":
            r["note"] = (RISK_ALT_NOTE + r["note"][len(RISK_ALT_NOTE):] if r.get("note", "").startswith(RISK_ALT_NOTE)
                         else RISK_ALT_NOTE + (r.get("note") or "")) + f"；商品页冷却中，约 {mins_left(until, now)} 分钟后再打开"
            r["risk"] = True
        else:
            r["retry_at"] = until
            r["note"] = "京东风控限流冷却中，换方式也没读到价格，约 " + f"{mins_left(until, now)} 分钟后再试（冷却期间不再打开商品页）"
        return r
    if r.get("risk"):
        st = risk_hit(store, mid, now, r.get("status") == "ok", raw.get("url") or "")
        if r.get("status") == "ok":
            r["note"] = (r.get("note") or RISK_ALT_NOTE) + f"；商品页冷却约 {mins_left(st['until'], now)} 分钟"
        else:
            r["retry_at"] = st["until"]
            r["note"] = (r.get("note") or "京东风控限流") + f"。已自动换方式/稍后重试：约 {mins_left(st['until'], now)} 分钟后再试"
        r["cooldown"] = {"level": st["level"], "until": st["until"]}
    elif r.get("status") == "ok":
        risk_clear(store, mid)
    return r


async def _risk_wait(store, w: dict, mid: str, cool: dict, now: float) -> dict:
    """No JD request at all while the computer cools down and the other route did not work either."""
    left = mins_left(float(cool["until"]), now)
    note = f"京东风控限流冷却中（{machine_label(store, mid)}），约 {left} 分钟后再试；冷却期间不访问京东"
    store.update("watches", w["id"], shop_status="risk_limit", status_note=note, last_error=note[:300],
                 next_check=float(cool["until"]) + jitter(180, 0.5))
    return {"ok": False, "waiting": "risk_cooldown", "machine": mid, "until": cool["until"], "error": note,
            "result": {"status": "risk_limit", "note": note, "price": None, "final_price": None, "effective": None,
                       "in_stock": None, "title": ""}}


BUSY_RECHECK_S = int(os.environ.get("AIW_SHOP_BUSY_RECHECK_S", "300"))


async def _machine_wait(sessions, mid: str) -> Optional[str]:
    """'' when the watch's own computer can be used now, else why it has to wait: 'busy' (a task runs there; the
    check would share its browser) or 'offline'. A paused cloud computer is woken by the check, so it is not offline."""
    if os.environ.get("AIW_SHOP_WAIT_BUSY", "1") != "0" and getattr(sessions, "busy_session", None) and sessions.busy_session(mid):
        return "busy"
    cloud = getattr(sessions, "cloud", None)
    if cloud and cloud.is_cloud(mid):
        return ""
    probe = getattr(sessions, "_probe", None)
    if probe is not None:
        try:
            if not await probe(mid, 6.0):
                return "offline"
        except Exception:  # noqa: BLE001
            return "offline"
    return ""


async def _defer(store, notifier, w: dict, mid: str, why: str, now: float, manual: bool) -> dict:
    """Wait for the watch's own computer (never another one) and say so once per stretch of waiting."""
    machine = machine_label(store, mid)
    status = "machine_busy" if why == "busy" else "machine_offline"
    note = (f"「{machine}」正在做别的任务，价格检查等它做完再做（不会换到别的电脑）" if why == "busy" else
            f"「{machine}」现在连不上（关机或 vm-agent 没在运行），等它上线再检查（不会换到别的电脑）")
    store.update("watches", w["id"], shop_status=status, status_note=note, last_error=note[:300],
                 next_check=now + (BUSY_RECHECK_S if why == "busy" else max(BUSY_RECHECK_S, 600)))
    if manual or w.get("shop_status") != status:
        say_chat(f"价格监控「{w['name']}」：{note}。")
        if why == "offline":
            await notifier.notify("watch_error", f"价格监控在等电脑上线（{machine}）：{w['name']}", note, link=w.get("url"),
                                  push=manual is False)
    return {"ok": False, "waiting": why, "machine": mid, "machine_name": machine, "error": note}


async def apply_result(store, sessions, notifier, w: dict, r: dict, now: float, interval: int, mid: str,
                       manual: bool = False) -> dict:
    prev_status = w.get("shop_status")
    site_name = SITE_NAMES.get(w.get("site") or site_of(w["url"]) or "", "电商")
    machine = (store.machine(mid) or {}).get("name") or mid
    fields: dict[str, Any] = {"last_checked": now, "shop_status": r["status"], "status_note": r.get("note") or None}
    if r.get("title") and not is_generic_title(r["title"]):
        fields["item_title"] = r["title"]
    elif is_generic_title(w.get("item_title") or "x") and w.get("item_title"):
        fields["item_title"] = None    # v8: a generic JD title stored by an earlier version
    if r["status"] == "ok":
        fields.update(price=r.get("price"), final_price=r.get("final_price"),
                      in_stock=None if r.get("in_stock") is None else int(bool(r["in_stock"])),
                      fail_count=0, last_error=None, next_check=now + jitter(interval),
                      last_value=f"{r['effective']:g}")
        hit, below, msg = evaluate(w, r)
        fields["below"] = int(below)
        if hit:
            fields["triggered_at"] = now
        store.update("watches", w["id"], **fields)
        if prev_status and prev_status != "ok" or manual:
            resolve_asks(w["id"], "已恢复正常")
        if manual or (prev_status in FAIL_STATES + USER_STATES):
            say_chat(f"价格监控「{w['name']}」恢复了：{price_line(r, w.get('value') or 0)}。"
                     + ("（页面没显示价格，这次是从京东价格接口读的）" if r.get("via") == "api" else "")
                     + ("（这次是从屏幕截图读的）" if r.get("via") == "screen" else "")
                     + (f"（{r.get('note')}）" if r.get("via") == "alt" and r.get("note") else ""))
        if hit:
            w2 = store.get("watches", w["id"])
            title = w2.get("item_title") or w2["name"]
            if int(w.get("auto_order") or 0):
                started = await start_order(store, sessions, notifier, w2, r)
                if started:
                    await notifier.notify("watch", f"降价了，正在下单：{title[:40]}",
                                          f"{msg}。AIW 正在{machine}上把它放进订单，付款前会请你确认。\n{w['url']}", link=w["url"])
            else:
                await notifier.notify("watch", f"{site_name}降价：{title[:40]}", f"{msg}\n{w['url']}", link=w["url"])
        return {"ok": True, "triggered": hit, "value": fields.get("last_value"), "result": r}

    fails = int(w.get("fail_count") or 0) + 1
    if r["status"] == "off_shelf":
        fields.update(in_stock=0, below=0, fail_count=0, next_check=now + interval * 4, last_error=None)
    elif r["status"] == "price_pending":     # not a failure: keep looking at the normal pace until it is priced
        fields.update(below=0, fail_count=0, next_check=now + interval, last_error=None)
    elif r["status"] == "risk_limit":       # JD's 活动火爆 page: back off for hours, more checks only keep it on
        fields.update(fail_count=fails, next_check=r.get("retry_at") or now + jitter(risk_backoff(interval, fails)),
                      last_error=(r.get("note") or "")[:300])
    else:
        fields.update(fail_count=fails, next_check=now + jitter(_backoff(interval, fails)), last_error=(r.get("note") or r["status"])[:300])
    store.update("watches", w["id"], **fields)
    bname = _bname(store, mid)
    changed = r["status"] != prev_status
    if changed:
        if r["status"] == "risk_limit":
            mins = mins_left(r["retry_at"], now) if r.get("retry_at") else None
            await notifier.notify("watch_error", f"京东风控限流，已自动换方式/稍后重试（{machine}）：{w['name']}",
                                  risk_text(machine, bname, mins) + f"\n{w['url']}", link=w["url"], deep=f"takeover/{mid}")
        elif r["status"] == "need_login":
            await notifier.notify("watch_error", f"需要登录{site_name}（{machine}）：{w['name']}",
                                  f"{r.get('note')}。请在 AIW 里打开「{machine}」的接管桌面，在 {bname} 里登录京东一次（扫码即可），"
                                  f"之后价格（含券后价）会自动读取。\n{w['url']}", link=w["url"], deep=f"takeover/{mid}")
        elif r["status"] == "login":
            await notifier.notify("watch_error", f"需要登录{site_name}：{w['name']}",
                                  f"「{machine}」上的 {bname} 还没登录{site_name}。请在 AIW 里打开这台电脑的接管桌面，"
                                  f"在 {bname} 里登录一次（扫码即可），之后价格会自动读取。\n{w['url']}", link=w["url"])
        elif r["status"] == "captcha":
            via = f"（读的是 {r.get('cdp') or bname} 上的浏览器）" if r.get("cdp") else ""
            await notifier.notify("watch_error", f"{site_name}要求验证（{machine}）：{w['name']}",
                                  f"在「{machine}」上读取价格时遇到滑块/安全验证{via}。请在「{machine}」的接管桌面里完成一次验证；"
                                  f"检查频率已自动放慢。\n{w['url']}", link=w["url"], deep=f"takeover/{mid}")
        elif r["status"] == "no_machine":
            await notifier.notify("watch_error", f"价格监控的电脑不在了：{w['name']}", r.get("note") or "", link=w["url"])
        elif r["status"] == "off_shelf":
            await notifier.notify("watch_error", f"商品已下架：{w['name']}", w["url"], link=w["url"])
        elif r["status"] == "price_pending":
            await notifier.notify("watch", f"还没开价：{w['name']}", f"{r.get('note')}。开价后会接着按目标价盯着。\n{w['url']}",
                                  link=w["url"], push=False)
        elif r["status"] == "no_browser":
            await notifier.notify("watch_error", f"价格监控打不开浏览器：{w['name']}", f"{r.get('note')}\n{w['url']}", link=w["url"])
    # no silent failure (escalate.py): anything the user must fix becomes a chat question with one-tap 立即重试
    if r["status"] in FAIL_STATES + USER_STATES + ("no_machine",) and (changed or manual or fails == 3):
        what = {"login": f"「{machine}」上的 {bname} 还没登录{site_name}：请在接管桌面里登录一次（扫码即可），再点「{OPT_RETRY}」。",
                "need_login": f"{r.get('note')}：请在「{machine}」的接管桌面里登录京东一次（扫码即可），再点「{OPT_RETRY}」。",
                "risk_limit": risk_text(machine, bname, mins_left(r["retry_at"], now) if r.get("retry_at") else None),
                "captcha": f"{site_name}弹出了滑块/安全验证：请在「{machine}」的接管桌面里完成一次，再点「{OPT_RETRY}」。"
                }.get(r["status"]) or str(r.get("note") or r["status"])
        if r["status"] in ("error", "no_price"):
            what = f"价格监控「{w['name']}」这次没读到价格：{what}" + ("（已连续 3 次）" if fails >= 3 else "")
        post_ask(w, what, mid=mid, machine=machine)
    if r["status"] in FAIL_STATES and fails == 3:
        await notifier.notify("watch_error", f"价格监控失败：{w['name']}", f"连续 3 次读取失败：{r.get('note')}", link=w["url"])
    return {"ok": False, "error": r.get("note") or r["status"], "result": r}


# ----------------------------------------------------------------------------- chat questions (one-tap retry)
SCHED = None    # the Scheduler, for 立即重试 (bind() in main.py)
CHAT = None     # chat.Chat: where the question shows up (web home + iOS)


def bind(scheduler=None, chat=None) -> None:
    global SCHED, CHAT
    if scheduler is not None:
        SCHED = scheduler
    if chat is not None:
        CHAT = chat


def _asks(wid: str) -> list[dict]:
    if not CHAT:
        return []
    from .chat import _row
    rows = CHAT.store._x("SELECT * FROM chat_messages WHERE kind='ask' AND data LIKE ? ORDER BY seq DESC LIMIT 20",
                         (f'%"watch_id": "{wid}"%',))
    return [m for m in (_row(x) for x in rows) if not (m.get("data") or {}).get("resolved")]


def resolve_asks(wid: str, answer: str) -> None:
    for m in _asks(wid):
        CHAT.update(m["id"], data={"resolved": {"answer": answer, "by": "watch", "at": time.time()}})


def say_chat(text: str) -> None:
    if CHAT:
        try:
            CHAT.add("assistant", "text", text)
        except Exception as e:  # noqa: BLE001
            log.warning("shopwatch chat: %s", e)


def post_ask(w: dict, text: str, mid: Optional[str] = None, machine: Optional[str] = None) -> Optional[dict]:
    """One open question per watch: the older one is closed when a newer one is posted. 3.8.2: the card names the
    computer it came from (the user saw captcha cards and no captcha on the computer they had chosen)."""
    if not CHAT:
        return None
    try:
        mid = mid or w.get("machine_id")
        machine = machine or machine_label(getattr(CHAT, "store", None), mid)
        resolve_asks(w["id"], "有了新的情况，见下一条")
        return CHAT.add("assistant", "ask", text, {"watch_id": w["id"], "ask_kind": "question",
                                                   "title": f"价格监控 · {w['name']} · {machine}", "question": text,
                                                   "machine_id": mid, "machine": machine,
                                                   "options": [OPT_RETRY, OPT_PAUSE]})
    except Exception as e:  # noqa: BLE001
        log.warning("shopwatch ask: %s", e)
        return None


async def answer_ask(chat, m: dict, ans: str) -> dict:
    """The user's tap on a watch question: 立即重试 (any free text counts as that too) or 暂停这个监控."""
    d = m.get("data") or {}
    store = chat.store
    w = store.get("watches", d.get("watch_id") or "")
    if not w:
        chat.update(m["id"], data={"stale": True})
        raise ValueError("这个价格监控已经删除了")
    a = str(ans or "").strip()
    m = chat.update(m["id"], data={"resolved": {"answer": a, "by": "chat", "at": time.time()}})
    if a == OPT_PAUSE or "暂停" in a or a in ("停止", "停", "取消"):
        store.update("watches", w["id"], enabled=0)
        chat.add("assistant", "text", f"已暂停价格监控「{w['name']}」。在「追踪」页可以重新打开。")
        return {"ok": True, "message": m, "open_task": False}
    store.update("watches", w["id"], enabled=1, next_check=None)
    chat.add("assistant", "text", f"好的，正在重新检查「{w['name']}」…（如果要重开 Chrome，大约需要 1 分钟）")
    sched = SCHED

    async def _go():
        try:
            if sched is None:
                raise RuntimeError("调度器没有启动")
            await check(sched, store.get("watches", w["id"]) or w, manual=True)
        except Exception as e:  # noqa: BLE001
            post_ask(w, f"重试价格监控「{w['name']}」时出错了：{type(e).__name__}: {e}"[:300])
    task = asyncio.get_running_loop().create_task(_go())
    getattr(chat, "jobs", set()).add(task)
    task.add_done_callback(lambda t: getattr(chat, "jobs", set()).discard(t))
    return {"ok": True, "message": m, "open_task": False}


def _bname(store, mid) -> str:
    from . import browseros as _bos
    return _bos.browser_name(store, mid)


def _sync_order_status(store, w: dict) -> None:
    tid = w.get("order_task_id")
    if not tid or w.get("order_status") not in ("running",):
        return
    t = store.task(tid)
    if t and t["status"] not in LIVE_TASK:
        store.update("watches", w["id"], order_status="ended" if t["status"] == "succeeded" else t["status"])


# ----------------------------------------------------------------------------- ordering
def order_prompt(w: dict, r: Optional[dict] = None) -> str:
    target = float(w.get("value") or 0)
    seen = ""
    if r and r.get("effective") is not None:
        seen = f"（刚读到的价格 ¥{r['effective']:g}）"
    return (
        f"【价格监控自动下单】{SITE_NAMES.get(w.get('site') or '', '')}商品：{w['url']}\n"
        f"目标价：实付不超过 ¥{target * int(w.get('quantity') or 1):g}（单价 ≤ ¥{target:g}）{seen}；数量：{int(w.get('quantity') or 1)}；"
        f"规格：{w.get('sku') or '默认/当前选中的规格'}；收货地址：{w.get('address_note') or '默认地址'}。\n"
        "步骤：\n"
        "1. browser_open 打开商品链接（这台电脑的 Chrome 已登录）。遇到登录页、滑块或短信验证，用 request_takeover 请用户在接管桌面处理。\n"
        "2. 用 browser_click 点规格文字选好规格，把数量改成要求的数量，再点「立即购买」（淘宝/天猫也可能叫「领券购买」）。不要加入购物车。\n"
        "3. 进入「确认订单/提交订单」页后，按要求核对或选择收货地址，然后调用 order_confirm（填 item、spec、quantity、payable=页面上的实付/应付金额、address）。"
        "绝对不要自己点「提交订单」「立即支付」，也不要用 shell/xdotool 点击。\n"
        "4. order_confirm 会等用户确认（最长 30 分钟）。它返回 SUBMITTED 才表示订单已提交：接着看收银台，需要支付密码/指纹/短信时，"
        "用 request_takeover 请用户在接管桌面里完成付款（不要索要、不要输入支付密码），用户完成后 done。\n"
        "5. 它返回 REJECTED、TIMEOUT 或 ABOVE_TARGET 时不要提交，直接 done 并说明原因。\n"
        "6. 商品无货、规格不存在或价格明显不对时，不要下单，done 说明情况。"
    )


async def start_order(store, sessions, notifier, w: dict, r: Optional[dict] = None) -> bool:
    tid = w.get("order_task_id")
    if tid:
        t = store.task(tid)
        if t and t["status"] in LIVE_TASK:
            return False
    res = await sessions.submit(order_prompt(w, r), w.get("machine_id") or None, source="watch_order",
                                title=f"🛒 {(w.get('item_title') or w['name'])[:30]}", max_steps=60, timeout_s=1800)
    task_id = res["task"]["id"]
    store.kv_set(f"shop_order:{task_id}", w["id"])
    store.update("watches", w["id"], order_task_id=task_id, order_status="running")
    return True


# per order task: {"watch": id, "approved": bool, "submitted": bool}
_STATE: dict[str, dict] = {}


def order_state(task_id: str) -> Optional[dict]:
    return _STATE.get(task_id)


def ensure_state(task_id: str) -> dict:
    return _STATE.setdefault(task_id, {"watch": None, "approved": False, "submitted": False})


def summary_text(d: dict) -> str:
    return (f"商品：{d['item'][:60]}｜规格：{d['spec'] or '默认'}｜数量：{d['quantity']}｜"
            f"实付：¥{d['payable']:g}｜地址：{(d['address'] or '默认地址')[:60]}")


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    p, _ = parse_price(str(v))
    return p if p is not None else _bare_number(str(v))


def order_tool(store, notifier, task, computer, confirm: Callable[..., Awaitable[bool]], state: dict):
    w = store.get("watches", state["watch"]) or {}

    class _Quiet:
        async def notify(self, *a, **k):
            return None
    notifier = notifier or _Quiet()
    target_total = float(w.get("value") or 0) * int(w.get("quantity") or 1)
    schema = {"name": "order_confirm", "description": (
        "Price-watch order: call this ONCE you are on the 确认订单/提交订单 page (after 立即购买), never earlier. It reads the "
        "payable amount from the page, checks it against the user's limit, asks the user to confirm (up to 30 min), and only "
        "on approval clicks 提交订单 itself. Returns SUBMITTED, REJECTED, TIMEOUT, ABOVE_TARGET or FAILED with what to do next."),
        "parameters": {"type": "object", "properties": {
            "item": {"type": "string", "description": "商品名"},
            "spec": {"type": "string", "description": "选中的规格"},
            "quantity": {"type": "integer"},
            "payable": {"type": "number", "description": "页面上的实付/应付总额（元）"},
            "address": {"type": "string", "description": "页面上的收货地址（姓名可省略）"}},
            "required": ["item", "payable"]}}

    async def call(a: dict) -> str:
        if state.get("submitted"):
            return "ALREADY SUBMITTED: 订单已经提交过了。处理收银台付款（需要密码就 request_takeover），然后 done。"
        page = await run_probe(computer, "order", timeout=30)
        if page.get("error"):
            return f"FAILED: 读不到确认订单页（{page['error'][:160]}）。确认已经点了「立即购买」并停在确认订单页后再调用。"
        if not page.get("found"):
            return "FAILED: 没有找到确认订单页（trade.jd.com / buy.taobao.com / buy.tmall.com）。先点「立即购买」进入确认订单页，再调用 order_confirm。"
        on_page = parse_payable(page)
        said = _num(a.get("payable"))
        amounts = [x for x in (on_page, said) if x is not None]
        if not amounts:
            return "FAILED: 读不到实付金额。看清页面上的实付/应付总额，填进 payable 再调用一次。"
        payable = max(amounts)   # when page and model disagree, the higher number decides (never under-report)
        d = {"item": str(a.get("item") or w.get("item_title") or w.get("name") or "")[:120],
             "spec": str(a.get("spec") or w.get("sku") or "")[:80], "quantity": int(a.get("quantity") or w.get("quantity") or 1),
             "payable": payable, "address": str(a.get("address") or ((page.get("addr") or [""])[0]))[:160]}
        if payable > target_total + 1e-6:
            await run_probe(computer, "close-order", timeout=30)
            store.update("watches", w["id"], order_status="above_target")
            await notifier.notify("watch", f"没有下单：实付 ¥{payable:g} 超过目标 ¥{target_total:g}",
                                  summary_text(d) + f"\n{w.get('url', '')}", task_id=task.id, thread_id=task.thread_id,
                                  link=w.get("url"))
            return (f"ABOVE_TARGET: 实付 ¥{payable:g} 超过目标 ¥{target_total:g}，已关闭确认订单页，没有提交。"
                    "不要再下单，直接 done 说明原因。")
        from tool_router import Decision, RiskLevel
        payload = {"商品": d["item"], "规格": d["spec"] or "默认", "数量": d["quantity"], "实付金额": f"¥{payable:g}",
                   "收货地址": d["address"] or "默认地址", "链接": w.get("url", ""), "confirm_timeout_s": CONFIRM_TIMEOUT_S}
        reason = f"确认下单并付款（30 分钟内不确认会自动取消）：{summary_text(d)}"
        store.update("watches", w["id"], order_status="waiting_confirm")
        t0 = time.monotonic()
        ok = await confirm("order_confirm", payload, Decision(RiskLevel.L3, reason))
        if not ok:
            timed_out = time.monotonic() - t0 >= CONFIRM_TIMEOUT_S - 5
            await run_probe(computer, "close-order", timeout=30)
            store.update("watches", w["id"], order_status="timeout" if timed_out else "rejected")
            word = "TIMEOUT: 30 分钟内没有确认" if timed_out else "REJECTED: 用户拒绝了这次下单"
            return f"{word}。确认订单页已关闭，订单没有提交。不要再操作，直接 done。"
        state["approved"] = True
        await install_cdp_tool(computer)
        res = await run_probe(computer, "submit", timeout=40)
        if not res.get("ok"):
            store.update("watches", w["id"], order_status="submit_failed")
            return (f"FAILED: 用户已确认，但没能点到「提交订单」（{res.get('error')}）。现在可以用 browser_click 点「提交订单」一次；"
                    "点不到就 request_takeover 请用户在接管桌面里提交。")
        state["submitted"] = True
        store.update("watches", w["id"], order_status="submitted", enabled=0)
        await notifier.notify("watch", f"订单已提交：{d['item'][:40]}", summary_text(d) + "\n请在接管桌面里完成付款（如需支付密码）。",
                              task_id=task.id, thread_id=task.thread_id, link=w.get("url"))
        return (f"SUBMITTED: 已点「提交订单」，当前页面 {res.get('url', '')[:120]}。"
                + ("页面似乎还停在确认订单页，截图看看是否有弹窗（如地址/库存提示）。" if res.get("still_order_page") else "")
                + "现在到收银台：需要支付密码/指纹/短信时用 request_takeover 请用户在接管桌面完成付款，绝不输入或索要密码；完成后 done。")

    call.needs_confirm = lambda a: False   # it raises its own card with the verified summary
    call.max_result = 800
    return schema, call


def create_tool(store, task):
    schema = {"name": "price_watch", "description": (
        "Create a 京东/淘宝/天猫 price watch for the user (e.g. '帮我盯着这个链接，低于 199 就下单'). AIW then checks the item "
        "on the chosen computer's logged-in Chrome every interval_min minutes; when the price (incl. 到手价/券后价) drops to "
        "target_price and it is in stock, it notifies, and with auto_order=true it fills the order up to the payment step "
        "and asks the user to confirm before paying. Use auto_order=true only when the user said to buy/下单."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "商品链接或分享文本"},
            "target_price": {"type": "number", "description": "目标单价（元），价格 ≤ 它时触发"},
            "quantity": {"type": "integer", "description": "下单数量，默认 1"},
            "sku": {"type": "string", "description": "规格，如 '黑色 256GB'"},
            "address_note": {"type": "string", "description": "收货地址说明，空=默认地址"},
            "interval_min": {"type": "integer", "description": "检查间隔分钟，最少 10，默认 30"},
            "auto_order": {"type": "boolean", "description": "触发后自动下单到付款前"},
            "machine": {"type": "string", "description": "在哪台电脑上检查：电脑的名字或 id（如 '本地vm'）。用户说了就必须填；"
                                                          "不填 = 第一台本地电脑（不是当前这台）"},
            "name": {"type": "string"}},
            "required": ["url", "target_price"]}}

    async def call(a: dict) -> dict:
        ensure_columns(store)
        want = a.get("machine_id") or a.get("machine")
        f = watch_fields({k: v for k, v in a.items() if k not in ("machine", "machine_id")}, default_machine=None)
        if want:   # 3.8.2: the computer the user named, by id or name; never silently the one this task runs on
            mid = resolve_machine(store, want)
            if not mid:
                names = "、".join(f"{m.get('name') or m['id']}（{m['id']}）" for m in store.machines())
                return {"error": f"没有叫「{want}」的电脑。可选：{names}。请问用户要用哪一台，再调用一次。"}
            f["machine_id"] = mid
        else:
            f["machine_id"] = default_machine(store)
        w = store.insert("watches", f)
        return {"created": w["id"], "name": w["name"], "url": w["url"], "target": w["value"], "auto_order": bool(w["auto_order"]),
                "interval_min": w["interval_min"], "machine": w["machine_id"],
                "machine_name": machine_label(store, w["machine_id"]),
                "note": "已创建，首次检查会在 1 分钟内开始；在「追踪」页可以查看和修改。"}

    call.needs_confirm = lambda a: False
    return schema, call


ORDER_MARK = "【价格监控自动下单】"


def is_order_task(task) -> bool:
    """A watch's order task, also when it was re-queued after an API restart (source "resume", same prompt)."""
    return (getattr(task, "source", "") or "") == "watch_order" or ORDER_MARK in (getattr(task, "prompt", "") or "")


def _order_watch(store, task) -> Optional[str]:
    wid = store.kv_get(f"shop_order:{task.id}")
    if wid and store.get("watches", wid):
        return wid
    for w in store.all("watches", "mode=? AND order_status IN ('running','waiting_confirm')", (MODE,)):
        if w.get("url") and w["url"] in (task.prompt or ""):
            store.kv_set(f"shop_order:{task.id}", w["id"])
            return w["id"]
    return None


def task_tools(store, notifier, task, computer, confirm) -> dict:
    """Extra agent tools for this task: price_watch always; order_confirm (and the gate state) for an order task.
    An order task whose watch cannot be found still gets the gate, just no way to submit."""
    tools = {"price_watch": create_tool(store, task)}
    if is_order_task(task):
        ensure_columns(store)
        wid = _order_watch(store, task)
        st = _STATE.setdefault(task.id, {"watch": wid, "approved": False, "submitted": False})
        if wid:
            tools["order_confirm"] = order_tool(store, notifier, task, computer, confirm, st)
    return tools


# ----------------------------------------------------------------------------- the confirm gate
ProbeFn = Callable[..., Awaitable[dict]]


async def gate(state: dict, name: str, args: dict, probe: ProbeFn, scale: float = 1.0) -> Optional[str]:
    """None = allowed. A string = refused, with the reason the model sees. No 提交订单 or payment click gets past
    this before the user approved through order_confirm; 提交订单 never (order_confirm clicks it itself)."""
    approved = bool(state.get("approved"))
    a = args or {}
    if name == "shell" and (SHELL_CLICK_RE.search(str(a.get("command") or "")) or BOS_SHELL_RE.search(str(a.get("command") or ""))):
        return "下单任务里不能用 shell 去点击网页或驱动浏览器；用 browser_click 选规格，确认订单页上调用 order_confirm。"
    if name in ("browser_click", "click_element"):
        text = str(a.get("text") or a.get("target") or "")
        if SUBMIT_RE.search(text):
            return ("「提交订单」只能由 order_confirm 在用户确认后点击。" if not state.get("submitted")
                    else "订单已经提交过了，不要再点提交。")
        if PAY_RE.search(text) and not approved:
            return "付款按钮在用户确认前是锁定的：先在确认订单页调用 order_confirm。"
        if re.search(r"下单", text) and not approved:
            try:
                info = await probe("url")
            except Exception:  # noqa: BLE001
                info = {}
            if is_order_url(info.get("url") or "") or info.get("error"):
                return "确认订单页上的下单按钮由 order_confirm 在用户确认后点击。"
        return None
    if name in ("click", "double_click", "right_click"):
        if a.get("x") is None or a.get("y") is None:
            return None
        try:
            info = await probe("elem-at", x=int(round(float(a["x"]) * scale)), y=int(round(float(a["y"]) * scale)))
        except Exception as e:  # noqa: BLE001
            info = {"error": str(e)}
        if info.get("error"):
            return None if approved else "下单任务里看不清这个点上是什么按钮：请改用 browser_click 写按钮文字。"
        text = info.get("text") or ""
        if SUBMIT_RE.search(text) and (is_order_url(info.get("url") or "") or not state.get("submitted")):
            return "这个位置是「提交订单」，只能由 order_confirm 在用户确认后点击。"
        if PAY_RE.search(text) and not approved and (is_order_url(info.get("url") or "") or is_pay_url(info.get("url") or "")):
            return "付款按钮在用户确认前是锁定的：先在确认订单页调用 order_confirm。"
        return None
    if name == "keypress" and ENTER_KEYS.match(str(a.get("key") or "").strip()) and not approved:
        try:
            info = await probe("url")
        except Exception:  # noqa: BLE001
            info = {}
        if is_order_url(info.get("url") or "") or is_pay_url(info.get("url") or ""):
            return "确认订单页上不能按回车（可能直接提交订单）；请调用 order_confirm。"
        return None
    if name == "browser_fill" and a.get("submit") and not approved:
        try:
            info = await probe("url")
        except Exception:  # noqa: BLE001
            info = {}
        if is_order_url(info.get("url") or "") or is_pay_url(info.get("url") or ""):
            return "确认订单页上不能带回车提交；填完后调用 order_confirm。"
    return None


def guard(executor, state: dict, computer) -> None:
    """Wrap a task's ToolExecutor.run with gate(). Also keeps the model's 下单/支付 wording from raising stray L3 cards
    for harmless steps (the real confirmation is order_confirm's card)."""
    from tool_router import Decision, RiskLevel
    from tool_router.executor import ToolOutcome
    orig = executor.run

    async def probe(sub: str, **kw) -> dict:
        return await run_probe(computer, sub, timeout=20, **kw)

    async def run(name: str, args: dict, intent: str = ""):
        scale = float(getattr(executor, "scale", 1.0) or 1.0)
        if name.startswith("bos_"):   # BrowserOS: judged as the classic click/typing it amounts to
            from tool_router import browseros as bos
            why, args = await bos.gate_bos(executor, name, args, lambda n, a: gate(state, n, a, probe, scale),
                                           bool(state.get("approved")))
            text = bos.gate_text(name, args)
            clicky = bool(text)
        else:
            why = await gate(state, name, args, probe, scale)
            text = str((args or {}).get("text") or "")
            clicky = name == "browser_click"
        if why:
            return ToolOutcome(name, None, False, Decision(RiskLevel.L4, why), error=f"forbidden by policy: {why}")
        auto = clicky and (state.get("approved") or not PAY_RE.search(text))
        if not auto:
            return await orig(name, args, intent="")
        saved = executor.confirm

        async def yes(*_a, **_k):
            return True
        executor.confirm = yes      # covered by order_confirm's card (paid) or not a payment at all (立即下单 on the item page)
        try:
            return await orig(name, args, intent="")
        finally:
            executor.confirm = saved

    executor.run = run
