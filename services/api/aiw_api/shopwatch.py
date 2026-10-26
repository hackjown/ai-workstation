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
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger("aiw.shopwatch")

MODE = "shop"
MIN_INTERVAL, DEFAULT_INTERVAL = 10, 30
CONFIRM_TIMEOUT_S = 1800
PROBE_SRC = Path(__file__).with_name("shop_probe.py")
REMOTE = "$HOME/.config/aiw/shop/probe.py"

COLUMNS = (("site", "TEXT"), ("quantity", "INTEGER"), ("sku", "TEXT"), ("address_note", "TEXT"),
           ("auto_order", "INTEGER"), ("machine_id", "TEXT"), ("price", "REAL"), ("final_price", "REAL"),
           ("in_stock", "INTEGER"), ("shop_status", "TEXT"), ("status_note", "TEXT"), ("item_title", "TEXT"),
           ("below", "INTEGER"), ("order_task_id", "TEXT"), ("order_status", "TEXT"))

SITE_NAMES = {"jd": "京东", "taobao": "淘宝", "tmall": "天猫"}
URL_RE = re.compile(r"https?://[^\s，。；;、\"'<>（）()【】\[\]]+")
NUM = r"([0-9]{1,7}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
PRICE_IN = re.compile(r"[¥￥]\s*" + NUM + r"(?:\s*[-~～至]\s*[¥￥]?\s*" + NUM + r")?|" + NUM + r"\s*元")
FINAL_RE = re.compile(r"到手价|券后价|券后|预估到手|到手|折后价|补贴后|活动价后|优惠后|final|coupon|aftercoupon|estimate", re.I)
NOT_PRICE_RE = re.compile(r"运费|邮费|满\s*\d|减\s*\d|省\s*[¥￥]|已省|节省|立省|返\s*[¥￥]|定金|月供|分期|起/期|x\s*\d+期|×\s*\d+期|积分|赠|红包|"
                          r"plus会员|PLUS|会员价|划线|原价|参考价|市场价|门店价|京东价.*?对比|销量|评价|关注", re.I)
OUT_OF_STOCK = re.compile(r"无货|暂时无货|已售罄|售罄|已抢光|缺货|库存不足|该地区暂不支持配送|到货通知|暂不销售|已下柜|商品已下架|宝贝已下架|此商品已下架|已下架")
OFF_SHELF = re.compile(r"商品已下柜|该商品已下柜|商品已下架|宝贝已下架|此商品已下架|宝贝不存在|商品不存在|很抱歉，您查看的商品找不到了|该商品已下架")
BUY_BTN = re.compile(r"^(立即购买|立即抢购|马上抢|领券购买|加入购物车|加入购物袋|立即预约|去购买|抢购)$")
CAPTCHA_RE = re.compile(r"滑块|拖动.*?验证|安全验证|请完成验证|验证码|访问受限|人机验证|异常流量|punish|x5sec|nocaptcha")
LOGIN_HOSTS = ("passport.jd.com", "login.taobao.com", "login.tmall.com", "login.m.jd.com", "plogin.m.jd.com", "havanalogin.taobao.com")
CAPTCHA_HOSTS = ("sec.taobao.com", "cfe.m.jd.com", "risk", "punish", "x5sec")
ORDER_HOSTS = ("trade.jd.com", "buy.taobao.com", "buy.tmall.com", "h5.m.taobao.com/cart/order", "trade.m.jd.com")
PAY_HOSTS = ("pay.jd.com", "cashier", "alipay.com", "pcashier", "payc.m.jd.com")

SUBMIT_RE = re.compile(r"提交订单|确认订单|去结算|结算")
PAY_RE = re.compile(r"立即支付|确认付款|确认支付|去支付|立即付款|付款|支付|pay\b", re.I)
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
    url = extract_url(url)
    m = re.match(r"https?://item\.m\.jd\.com/product/(\d+)\.html", url)
    if m:
        return f"https://item.jd.com/{m.group(1)}.html"
    m = re.match(r"https?://item\.m\.jd\.com/ware/view\.action\?wareId=(\d+)", url)
    if m:
        return f"https://item.jd.com/{m.group(1)}.html"
    return url


def is_shop_url(url: str) -> bool:
    return site_of(extract_url(url)) is not None


def is_order_url(url: str) -> bool:
    return any(h in (url or "") for h in ORDER_HOSTS)


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


def _label_before(ctx: str, t: str) -> str:
    """The few characters in front of a price inside its context ('运费 ¥5' -> '运费 '): its label."""
    t = (t or "").strip()
    if not ctx or not t or t not in ctx:
        return ""
    return ctx.split(t, 1)[0][-8:]


def parse_snapshot(snap: dict) -> dict:
    """Raw probe snapshot -> {status, price, final_price, effective, in_stock, title, url, note}.

    status: ok | login | captcha | off_shelf | no_price | error
    price is the shown selling price, final_price the 到手价/券后价 when the page shows one,
    effective = final_price or price (what the order would cost per item before shipping)."""
    if not isinstance(snap, dict) or snap.get("error"):
        err = (snap or {}).get("error") if isinstance(snap, dict) else "bad probe output"
        return {"status": "no_browser" if str(err).startswith("NO_CDP") else "error", "note": str(err)[:300],
                "price": None, "final_price": None, "effective": None, "in_stock": None, "title": "", "url": ""}
    url = snap.get("url") or ""
    host = (urlparse(url).hostname or "").lower()
    text = snap.get("text") or ""
    title = (snap.get("title") or snap.get("doc_title") or "").strip()
    res = {"status": "ok", "price": None, "final_price": None, "effective": None, "in_stock": None,
           "title": title[:120], "url": url, "note": "", "range": False}
    frames = " ".join(snap.get("frames") or [])
    if any(h in host for h in LOGIN_HOSTS) or (snap.get("password_input") and re.search(r"登录|login", text[:800] + title, re.I)) \
            or any(h in frames for h in ("login.taobao.com", "passport.jd.com", "havanalogin")):
        res.update(status="login", note="需要登录")
        return res
    if any(h in url for h in CAPTCHA_HOSTS) or (CAPTCHA_RE.search(text[:1500]) and len(text) < 2500):
        res.update(status="captcha", note="出现了滑块/安全验证")
        return res
    if OFF_SHELF.search(text[:3000]):
        res.update(status="off_shelf", in_stock=False, note="商品已下架")
        return res

    finals, mains = [], []
    for s in snap.get("sel") or []:
        v, rng = parse_price(s.get("t") or "")
        if v is None:
            v, rng = _bare_number(s.get("t") or ""), False
        if v is None or NOT_PRICE_RE.search(s.get("t") or "") or NOT_PRICE_RE.search(_label_before(s.get("ctx") or "", s.get("t") or "")):
            continue
        label = f"{s.get('t', '')} {s.get('ctx', '')}"
        is_final = s.get("role") == "final" or bool(FINAL_RE.search(s.get("t") or ""))
        (finals if is_final else mains).append((v, rng, float(s.get("fs") or 0), int(s.get("y") or 0), label))
    cands = []
    for c in snap.get("cand") or []:
        t = c.get("t") or ""
        v, rng = parse_price(t)
        if v is None or c.get("strike") or NOT_PRICE_RE.search(t):
            continue
        pre = _label_before(c.get("ctx") or "", t)
        if NOT_PRICE_RE.search(pre):
            continue
        own_final = bool(FINAL_RE.search(t) or FINAL_RE.search(c.get("cls") or ""))
        ctx_final = bool(FINAL_RE.search(pre))
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
    res["effective"] = res["final_price"] if res["final_price"] is not None else res["price"]

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
        res.update(status="no_price", note="页面上没有读到价格")
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
    label = "到手价" if r.get("final_price") is not None else "现价"
    msg = f"{label} ¥{eff:g}，已到目标 ¥{target:g}" if eff is not None else ""
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


# ----------------------------------------------------------------------------- running the probe on the computer
def probe_command(sub: str, **kw) -> str:
    src = PROBE_SRC.read_bytes()
    b64 = base64.b64encode(src).decode()
    digest = hashlib.sha1(src).hexdigest()
    args = " ".join(f"--{k} {_q(str(v))}" for k, v in kw.items() if v not in (None, "", False) and v is not True)
    args += "".join(f" --{k}" for k, v in kw.items() if v is True)
    return (f'mkdir -p "$HOME/.config/aiw/shop" && f="{REMOTE}" && '
            f'if [ "$(sha1sum "$f" 2>/dev/null | cut -d" " -f1)" != "{digest}" ]; then '
            f"echo '{b64}' | base64 -d > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"; fi && python3 \"$f\" {sub} {args}")


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


async def run_probe(computer, sub: str, timeout: float = 60, **kw) -> dict:
    r = await computer.execute("shell.exec", {"command": probe_command(sub, **kw), "timeout": timeout}, timeout=timeout + 15)
    res = getattr(r, "result", None) or {}
    out = (res.get("stdout") or "").strip().splitlines()
    for line in reversed(out):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    err = res.get("stderr") or getattr(r, "error", None) or "no output"
    return {"error": f"probe failed: {str(err)[-300:]}"}


async def _computer(sessions, mid: str):
    cloud = getattr(sessions, "cloud", None)
    if cloud and cloud.is_cloud(mid):
        await cloud.wake(mid)
    return sessions.computer(mid)


async def vision_read(vision, shot_b64: str, site: str) -> Optional[dict]:
    """OCR/vision fallback when the DOM gave no price."""
    if not vision or not shot_b64:
        return None
    raw = await vision.describe(
        "你在读电商商品页截图。只输出 JSON。",
        f"这是{SITE_NAMES.get(site, '电商')}商品详情页。读出售价和到手价/券后价（没有就 null）以及是否有货。"
        '输出：{"price": 数字或null, "final_price": 数字或null, "in_stock": true/false/null}', shot_b64, max_tokens=200)
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
    return out


# ----------------------------------------------------------------------------- the scheduled check
LIVE_TASK = ("queued", "running", "paused", "takeover", "waiting_confirm", "waiting_input")


def _backoff(interval_s: int, fails: int) -> int:
    return min(interval_s * (2 ** min(fails, 4)), 6 * 3600)


async def check(sched, w: dict, now: Optional[float] = None) -> dict:
    store, sessions, notifier = sched.store, sched.sessions, sched.notifier
    ensure_columns(store)
    now = now or time.time()
    interval = max(MIN_INTERVAL, int(w.get("interval_min") or DEFAULT_INTERVAL)) * 60
    mid = w.get("machine_id") or (sessions.settings.machines[0].id if getattr(sessions.settings, "machines", None) else "ai-linux-001")
    _sync_order_status(store, w)
    try:
        computer = await _computer(sessions, mid)
        raw = await run_probe(computer, "probe", timeout=70, url=w["url"], sku=w.get("sku") or "")
        if str(raw.get("error", "")).startswith("NO_CDP"):
            # Chrome is closed (or was started without the debugging port): open it the way the agent does
            await computer.execute("computer.open_app", {"name": "chrome"}, timeout=30)
            await asyncio.sleep(6)
            raw = await run_probe(computer, "probe", timeout=70, url=w["url"], sku=w.get("sku") or "")
        r = parse_snapshot(raw)
        if r["status"] == "no_price":
            vision = sessions._vision_llm() if hasattr(sessions, "_vision_llm") else None
            if vision:
                raw2 = await run_probe(computer, "probe", timeout=80, url=w["url"], sku=w.get("sku") or "", shot=True)
                v = await vision_read(vision, raw2.get("screenshot") or "", w.get("site") or site_of(w["url"]) or "")
                if v and (v.get("final_price") or v.get("price")):
                    r.update(price=v.get("price"), final_price=v.get("final_price"), status="ok", note="价格由视觉模型读出",
                             effective=v.get("final_price") or v.get("price"))
                    if r.get("in_stock") is None:
                        r["in_stock"] = v.get("in_stock")
    except Exception as e:  # noqa: BLE001
        r = {"status": "error", "note": f"{type(e).__name__}: {e}"[:300], "price": None, "final_price": None,
             "effective": None, "in_stock": None, "title": ""}
    return await apply_result(store, sessions, notifier, w, r, now, interval, mid)


async def apply_result(store, sessions, notifier, w: dict, r: dict, now: float, interval: int, mid: str) -> dict:
    prev_status = w.get("shop_status")
    site_name = SITE_NAMES.get(w.get("site") or site_of(w["url"]) or "", "电商")
    machine = (store.machine(mid) or {}).get("name") or mid
    fields: dict[str, Any] = {"last_checked": now, "shop_status": r["status"], "status_note": r.get("note") or None}
    if r.get("title"):
        fields["item_title"] = r["title"]
    if r["status"] == "ok":
        fields.update(price=r.get("price"), final_price=r.get("final_price"),
                      in_stock=None if r.get("in_stock") is None else int(bool(r["in_stock"])),
                      fail_count=0, last_error=None, next_check=now + interval,
                      last_value=f"{r['effective']:g}")
        hit, below, msg = evaluate(w, r)
        fields["below"] = int(below)
        if hit:
            fields["triggered_at"] = now
        store.update("watches", w["id"], **fields)
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
    else:
        fields.update(fail_count=fails, next_check=now + _backoff(interval, fails), last_error=(r.get("note") or r["status"])[:300])
    store.update("watches", w["id"], **fields)
    if r["status"] != prev_status:
        if r["status"] == "login":
            await notifier.notify("watch_error", f"需要登录{site_name}：{w['name']}",
                                  f"「{machine}」上的 Chrome 还没登录{site_name}。请在 AIW 里打开这台电脑的接管桌面，"
                                  f"在 Chrome 里登录一次（扫码即可），之后价格会自动读取。\n{w['url']}", link=w["url"])
        elif r["status"] == "captcha":
            await notifier.notify("watch_error", f"{site_name}要求验证：{w['name']}",
                                  f"读取价格时遇到滑块/安全验证。请在「{machine}」的接管桌面里完成一次验证；"
                                  f"检查频率已自动放慢。\n{w['url']}", link=w["url"])
        elif r["status"] == "off_shelf":
            await notifier.notify("watch_error", f"商品已下架：{w['name']}", w["url"], link=w["url"])
        elif r["status"] == "no_browser":
            await notifier.notify("watch_error", f"价格监控打不开浏览器：{w['name']}",
                                  f"「{machine}」上的 Chrome 调试端口连不上（{r.get('note')}）。", link=w["url"])
    if r["status"] in ("error", "no_price", "no_browser") and fails == 3:
        await notifier.notify("watch_error", f"价格监控失败：{w['name']}", f"连续 3 次读取失败：{r.get('note')}", link=w["url"])
    return {"ok": False, "error": r.get("note") or r["status"], "result": r}


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
            "name": {"type": "string"}},
            "required": ["url", "target_price"]}}

    async def call(a: dict) -> dict:
        ensure_columns(store)
        f = watch_fields(a, default_machine=task.machine_id)
        if f.get("machine_id") and not store.machine(f["machine_id"]):
            f["machine_id"] = task.machine_id
        w = store.insert("watches", f)
        return {"created": w["id"], "name": w["name"], "url": w["url"], "target": w["value"], "auto_order": bool(w["auto_order"]),
                "interval_min": w["interval_min"], "machine": w["machine_id"],
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
    if name == "shell" and SHELL_CLICK_RE.search(str(a.get("command") or "")):
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
        why = await gate(state, name, args, probe, float(getattr(executor, "scale", 1.0) or 1.0))
        if why:
            return ToolOutcome(name, None, False, Decision(RiskLevel.L4, why), error=f"forbidden by policy: {why}")
        text = str((args or {}).get("text") or "")
        auto = name == "browser_click" and (state.get("approved") or not PAY_RE.search(text))
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
