"""Probe v7 (2026-10-29). Probe v6 on ai-linux-001 (CDP 9222, aiw-chrome profile, logged in with thor, 48 JD cookies)
ended on https://trade.m.jd.com/common/limit.html?module=detail_m1&sceneType=1 (活动火爆 请选购其他商品, 26 elements):
the limiter of JD's MOBILE item page, so no price was ever read. v7 never loads the mobile item page (short links
resolved over HTTP, mobile UA replaced, Document requests to item.m.jd.com / limit.html refused), opens the item page
as a link from www.jd.com with navigator.webdriver patched, reads the price by another route once (JD's signed
pc_detailpage_wareBusiness, the cart, 我的关注), and the server keeps a per-computer cool-down (≈45 min, doubling)
with jittered intervals instead of hitting JD again and again."""
import argparse
import itertools
import json
import os
from pathlib import Path

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import shop_probe as sp
from aiw_api import shopwatch as sw

from test_shop_nocdp import Notes, Sessions, chat, store  # noqa: F401
from test_v384_shop_jd import BANNER, LIMIT, SKU, WARE_BUSINESS

ROOT = Path(__file__).resolve().parents[1]
M_URL = f"https://item.m.jd.com/product/{SKU}.html"
DESKTOP = f"https://item.jd.com/{SKU}.html"


# ----------------------------------------------------------------------------- links and user agents
def test_short_links_resolve_to_the_sku_without_fetching_the_mobile_page():
    seen = []

    def hop(url):
        seen.append(url)
        if url.startswith("https://3.cn/"):
            return 302, f"{M_URL}?cu=true&utm_source=kong", ""
        if url.startswith("https://u.jd.com/"):
            return 200, "", "<script>var hrl='https://union-click.jd.com/jdc?e=abc&amp;p=1';</script>"
        if url.startswith("https://union-click.jd.com/"):
            return 302, f"//item.m.jd.com/product/{SKU}.html?ad_od=share", ""
        raise AssertionError("fetched " + url)
    r = sp.resolve_jd_link("https://3.cn/2-AbCdEf", hop=hop)
    assert r["sku"] == SKU and r["url"] == DESKTOP and seen == ["https://3.cn/2-AbCdEf"]
    seen.clear()
    r = sp.resolve_jd_link("https://u.jd.com/XyZ12", hop=hop)
    assert r["sku"] == SKU and seen == ["https://u.jd.com/XyZ12", "https://union-click.jd.com/jdc?e=abc&p=1"]
    assert not any("item.m.jd.com" in u for u in seen)
    bad = sp.resolve_jd_link("https://3.cn/x", hop=lambda u: (_ for _ in ()).throw(OSError("timed out")))
    assert bad["sku"] == "" and "timed out" in bad["error"]
    assert sw.normalize_url(f"【京东】{M_URL}?utm_user=plusmember 「电容笔」") == DESKTOP
    assert sp.jd_sku_of(f"https://wqitem.jd.com/item/view?sku={SKU}") == SKU


def test_mobile_pages_and_user_agents():
    assert sp.is_mobile_item_url(M_URL) and sp.is_mobile_item_url(LIMIT["url"])
    assert not sp.is_mobile_item_url(DESKTOP) and not sp.is_mobile_item_url(sp.CART_URL)
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
    linux = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
    assert sp.mobile_ua(iphone) and sp.mobile_ua("Mozilla/5.0 (Linux; Android 14) Chrome/155.0 Mobile Safari/537.36")
    assert not sp.mobile_ua(linux)
    assert not sp.mobile_ua(sp.desktop_ua("Mozilla/5.0 (Linux; Android 14) Chrome/155.0.1 Mobile"))
    assert "Chrome/155.0.0.0" in sp.desktop_ua("Mozilla/5.0 (Linux; Android 14) Chrome/155.0.1 Mobile")


def test_route_helpers():
    assert sp.jd_area("a=1; ipLoc-djd=19-1607-4773-0; b=2") == "19_1607_4773_0" and sp.jd_area("") == "1_72_2799_0"
    assert sp.api_risk('{"code":"605","echo":"h5st is required"}') == "h5st is required"
    assert sp.api_risk(WARE_BUSINESS) == ""
    cart = json.dumps({"resultData": {"cartInfo": {"vendors": [{"sorted": [
        {"item": {"Id": "1", "skuId": "999", "price": "9.90"}},
        {"item": {"skuId": SKU, "price": "187.00", "finalPrice": {"price": "137.00", "priceContent": "到手价"}}}]}]}}})
    got = sp.price_of_items(cart, SKU)
    assert got["price"] == 187.0 and got["final_price"] == 137.0
    assert sp.price_of_items(cart, "123") is None
    rows = [{"prices": [{"t": "¥199.00", "strike": True}, {"t": "¥187.00", "fs": 16}, {"t": "PLUS价 ¥129", "fs": 12},
                        {"t": "到手价¥137.00", "fs": 12}]}]
    assert sp.row_price(rows) == {"price": 187.0, "final_price": 137.0, "plus_price": None, "in_stock": None}


def test_launcher_never_adds_automation_flags():
    src = (ROOT / "vm-agent/vm_agent/browser/chrome_cdp.py").read_text()
    assert "--enable-automation" not in src and "--headless" not in src.split("WRAPPER_SH", 1)[-1][:4000]
    assert "--enable-automation" not in sp.STEALTH_JS and "webdriver" in sp.STEALTH_JS


# ----------------------------------------------------------------------------- cmd_probe against a fake DevTools
class Dev:
    """One background tab. redirect_mobile: item.jd.com is sent on to item.m.jd.com (the Fetch domain pauses it).
    signed / cart_rows / follow_rows: what the other routes answer."""

    def __init__(self, item=None, redirect_mobile=False, ua="", signed=None, cart_rows=None, follow_rows=None):
        self.item, self.redirect_mobile, self.ua = item, redirect_mobile, ua
        self.signed, self.cart_rows, self.follow_rows = signed, cart_rows, follow_rows
        self.calls, self.navs, self.notified, self.params = [], [], [], {}

    def ws(self, url, timeout=15):
        dev = self

        class W:
            on_event = None

            def call(self, method, params=None, timeout=20):
                dev.calls.append(method)
                dev.params.setdefault(method, []).append(params or {})
                if method == "Target.createTarget":
                    return {"targetId": "T1"}
                if method == "Storage.getCookies":
                    return {"cookies": [{"name": "thor", "value": "S", "domain": ".jd.com", "expires": -1},
                                        {"name": "ipLoc-djd", "value": "19-1607-4773-0", "domain": ".jd.com", "expires": -1}]}
                if method == "Page.navigate":
                    dev.navs.append(params["url"])
                    if dev.redirect_mobile and "item.jd.com" in params["url"] and self.on_event:
                        self.on_event({"method": "Network.requestWillBeSent", "params": {"type": "Document",
                                       "request": {"url": params["url"]}}})
                        self.on_event({"method": "Network.requestWillBeSent", "params": {"type": "Document",
                                       "request": {"url": M_URL}, "redirectResponse": {"status": 302}}})
                        self.on_event({"method": "Fetch.requestPaused", "params": {"requestId": "F1",
                                       "request": {"url": M_URL}}})
                        return {"errorText": "net::ERR_BLOCKED_BY_CLIENT"}
                if method == "Page.captureScreenshot":
                    return {"data": "QUJD"}
                return {}

            def notify(self, method, params=None):
                dev.notified.append((method, params))

            def eval(self, expr, timeout=20):
                if "navigator.userAgent" in expr and "webdriver" in expr and "platform" in expr:
                    return json.dumps({"ua": dev.ua, "webdriver": False})
                if "#profile_path" in expr:
                    return "/home/aiw/.config/aiw-chrome/Default"
                if "signed:pc_detailpage_wareBusiness" in expr:
                    return json.dumps(dev.signed or {"src": "signed:pc_detailpage_wareBusiness", "status": 200, "signed": True,
                                                     "body": '{"code":"605","echo":"h5st is required"}'})
                if "n_links" in expr:
                    last = dev.navs[-1] if dev.navs else ""
                    rows = dev.cart_rows if last == sp.CART_URL else dev.follow_rows if last == sp.FOLLOW_URL else None
                    return json.dumps({"url": last, "rows": rows or [], "n_links": len(rows or []), "login": False, "text": ""})
                if "__aiw_p" in expr:
                    return json.dumps({"src": "x", "status": 200, "body": '{"code":"605","echo":"h5st is required"}'})
                if "title_from" in expr:
                    if dev.redirect_mobile:
                        return json.dumps({"url": "chrome-error://chromewebdata/", "ready": "complete", "sel": [], "cand": []})
                    return json.dumps(dev.item)
                return None

            def close(self):
                pass
        return W()


def _probe(monkeypatch, dev, url, route="auto", wait="6"):
    clock = itertools.count(0, 0.5)
    monkeypatch.setattr(sp.time, "time", lambda: 1000 + next(clock))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "WS", dev.ws)
    monkeypatch.setattr(sp, "browser_ws", lambda: dev.ws("x"))
    monkeypatch.setattr(sp, "pages", lambda: [{"id": "T1", "type": "page", "webSocketDebuggerUrl": "ws://t"}])
    monkeypatch.setattr(sp, "profile_of", lambda base, *a, **k: {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome"})
    got = {}

    def fake_out(obj, code=0):
        got.update(obj)
        raise SystemExit(code)
    monkeypatch.setattr(sp, "out", fake_out)
    with pytest.raises(SystemExit):
        sp.cmd_probe(argparse.Namespace(url=url, sku="", shot=False, wait=wait, progress="", route=route))
    return got


def test_redirect_to_the_mobile_page_is_refused_and_the_signed_api_reads_the_price(monkeypatch):
    signed = {"src": "signed:pc_detailpage_wareBusiness", "status": 200, "signed": True, "body": WARE_BUSINESS}
    dev = Dev(redirect_mobile=True, signed=signed, cart_rows=[{"prices": [{"t": "¥187.00"}]}])
    snap = _probe(monkeypatch, dev, DESKTOP)
    nav = dev.params["Page.navigate"][0]
    assert nav["url"] == DESKTOP and nav["referrer"] == sp.JD_HOME and nav["transitionType"] == "link"
    pats = [p["urlPattern"] for p in dev.params["Fetch.enable"][0]["patterns"]]
    assert "*://item.m.jd.com/*" in pats and any("limit.html" in p for p in pats)
    assert dev.params["Page.addScriptToEvaluateOnNewDocument"][0]["source"] == sp.STEALTH_JS
    assert ("Fetch.failRequest", {"requestId": "F1", "errorReason": "BlockedByClient"}) in dev.notified
    assert snap["mobile_blocked"] == M_URL and snap["nav_chain"][-1] == {"url": M_URL, "from_status": 302}
    assert not any("item.m.jd.com" in u for u in dev.navs)
    assert [a["route"] for a in snap["alt"]] == ["cart"] and snap["alt_price"]["src"] == "cart"   # priced: no more JD calls
    assert snap["probe_version"] == "7"
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["via"] == "alt" and r["risk"] is True and r["effective"] == 187.0
    assert r["note"].startswith("京东风控限流，已自动换方式读价") and "购物车" in r["note"]


def test_limit_page_then_signed_api_on_the_cart_page(monkeypatch):
    signed = {"src": "signed:pc_detailpage_wareBusiness", "status": 200, "signed": True, "body": WARE_BUSINESS}
    dev = Dev(item={**LIMIT, "title_from": "document.title", "n_elems": 26}, signed=signed)
    snap = _probe(monkeypatch, dev, DESKTOP)
    assert snap["risk_limit"] is True and dev.navs == [DESKTOP, sp.CART_URL]   # one other page, no mobile page
    assert [a["route"] for a in snap["alt"]] == ["cart", "signed"] and snap["alt_price"]["src"] == "signed"
    assert dev.params["Page.navigate"][1]["referrer"] == sp.JD_HOME
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["effective"] == 137.0 and r["price"] == 187.0 and "签名" in r["note"]


def test_nothing_works_is_risk_limit_with_every_route_in_the_diagnosis(monkeypatch):
    dev = Dev(item={**LIMIT, "title_from": "document.title", "n_elems": 26})
    snap = _probe(monkeypatch, dev, DESKTOP)
    assert [a["route"] for a in snap["alt"]] == ["cart", "signed", "follow"] and "alt_price" not in snap
    r = sw.parse_snapshot(snap)
    assert r["status"] == "risk_limit" and "不是验证码" in r["note"] and "换方式" in r["note"]
    d = sw.build_diag(snap, r, None, 1.0, False)
    assert d["alt"] and d["env"]["mobile_guard"] is True and d["route"] == "auto"
    assert any("列表里没有这个商品" in h for h in d["hints"]) and any("h5st" in h for h in d["hints"])


def test_mobile_user_agent_is_replaced_and_alt_route_skips_the_item_page(monkeypatch):
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 CriOS/155.0 Mobile/15E148"
    dev = Dev(ua=iphone, cart_rows=[{"prices": [{"t": "¥187.00"}, {"t": "券后 ¥137.00"}]}])
    snap = _probe(monkeypatch, dev, DESKTOP, route="alt")
    ov = dev.params["Emulation.setUserAgentOverride"][0]["userAgent"]
    assert not sp.mobile_ua(ov) and snap["env"]["ua_override"] == ov
    assert DESKTOP not in dev.navs and dev.navs[0] == sp.CART_URL and "screenshot" not in snap
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["effective"] == 137.0 and r["via"] == "alt"


def test_a_normal_item_page_needs_no_other_route(monkeypatch):
    item = {**BANNER, "ready": "complete", "visibility": "visible", "kw": [],
            "sel": [{"role": "main", "q": ".p-price .price", "t": "¥187.00", "ctx": "京东价 ¥187.00", "fs": 22, "y": 300}]}
    dev = Dev(item=item)
    snap = _probe(monkeypatch, dev, DESKTOP)
    assert "alt" not in snap and dev.navs == [DESKTOP] and "mobile_blocked" not in snap
    assert sw.parse_snapshot(snap)["status"] == "ok"


# ----------------------------------------------------------------------------- the server: per-computer cool-down
class Comp:
    machine_id = "m-nocdp"

    def __init__(self, pages):
        self.pages, self.cmds = list(pages), []

    async def execute(self, action, payload=None, observe=False, timeout=30):
        cmd = (payload or {}).get("command", "")
        out = {}
        if "probe.py" in cmd:
            self.cmds.append(cmd)
            out = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

        class R:
            result = {"stdout": json.dumps(out, ensure_ascii=False)}
        return R()


class Sched:
    def __init__(self, store, comp):
        self.store, self.sessions, self.notifier = store, Sessions(comp, None), Notes()


def _w(store, url=DESKTOP):
    f = sw.watch_fields({"url": url, "target_price": "120"}, default_machine="m-nocdp")
    return store.insert("watches", f)


LIMITED = {**LIMIT, "probe_version": "7", "jd_sku": SKU, "alt": [{"route": "cart", "price": False, "missing": "not_listed"}],
           "profile": {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome", "logged_in": True}}
ALT_OK = {**LIMITED, "alt": [{"route": "cart", "price": True}],
          "alt_price": {"price": 187.0, "final_price": 137.0, "src": "cart"}}
ITEM_OK = {**BANNER, "ready": "complete", "probe_version": "7",
           "sel": [{"role": "main", "q": ".p-price .price", "t": "¥187.00", "ctx": "", "fs": 22, "y": 300}]}


async def test_limit_page_cools_the_computer_down_and_other_watches_wait(store, chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sw.random, "random", lambda: 0.5)
    comp = Comp([LIMITED])
    sch = Sched(store, comp)
    w1, w2 = _w(store), _w(store, "https://item.jd.com/100012043978.html")
    res = await sw.check(sch, w1, now=1000.0)
    st = sw.risk_state(store, "m-nocdp")
    assert res["result"]["status"] == "risk_limit" and st["level"] == 1 and st["alt_ok"] is False
    assert st["until"] == 1000.0 + 45 * 60                              # 45 min (jitter at its middle)
    row = store.get("watches", w1["id"])
    assert row["next_check"] == st["until"] and "约 45 分钟后再试" in row["status_note"]
    assert any("京东风控限流，已自动换方式/稍后重试" in t for _k, t, _b in sch.notifier.sent)
    n = len(comp.cmds)
    res2 = await sw.check(sch, store.get("watches", w2["id"]), now=1100.0)    # same computer, cooling: no JD request
    assert res2["waiting"] == "risk_cooldown" and len(comp.cmds) == n
    assert store.get("watches", w2["id"])["next_check"] >= st["until"]
    # 立即检查 during the cool-down: the other route only, once
    res3 = await sw.check(sch, store.get("watches", w2["id"]), now=1200.0, manual=True)
    assert "--route 'alt'" in comp.cmds[-1] and res3["result"]["status"] == "risk_limit"
    # after the cool-down a second limit page doubles it
    comp.pages = [LIMITED]
    await sw.check(sch, store.get("watches", w1["id"]), now=st["until"] + 10)
    st2 = sw.risk_state(store, "m-nocdp")
    assert st2["level"] == 2 and st2["until"] - (st["until"] + 10) == 90 * 60
    assert "--route" not in comp.cmds[-1]


async def test_other_route_keeps_watching_during_the_cool_down_and_a_clean_read_clears_it(store, chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sw.random, "random", lambda: 0.5)
    comp = Comp([ALT_OK])
    sch = Sched(store, comp)
    w = _w(store)
    res = await sw.check(sch, w, now=1000.0)
    st = sw.risk_state(store, "m-nocdp")
    assert res["ok"] and res["result"]["effective"] == 137.0 and st["alt_ok"] is True
    row = store.get("watches", w["id"])
    assert row["shop_status"] == "ok" and row["status_note"].startswith("京东风控限流，已自动换方式读价")
    assert row["next_check"] - 1000.0 == pytest.approx(30 * 60)       # the normal interval (jitter at its middle)
    comp.pages = [{**ALT_OK, "route": "alt", "risk_limit": False, "url": "about:blank", "text": ""}]
    res = await sw.check(sch, store.get("watches", w["id"]), now=row["next_check"])
    assert "--route 'alt'" in comp.cmds[-1] and res["ok"] and "冷却中" in store.get("watches", w["id"])["status_note"]
    comp.pages = [ITEM_OK]
    res = await sw.check(sch, store.get("watches", w["id"]), now=st["until"] + 1)
    assert res["ok"] and "--route" not in comp.cmds[-1] and sw.risk_state(store, "m-nocdp") == {}


async def test_a_short_link_is_stored_as_the_desktop_item_once_resolved(store, chat):  # noqa: F811
    comp = Comp([{**ITEM_OK, "resolved": {"sku": SKU, "url": DESKTOP}, "jd_sku": SKU}])
    sch = Sched(store, comp)
    w = _w(store, "https://3.cn/2-AbCdEf")
    assert w["site"] == "jd"
    await sw.check(sch, w, now=1000.0)
    assert store.get("watches", w["id"])["url"] == DESKTOP


def test_intervals_are_jittered():
    vals = {round(sw.jitter(1800, rnd=lambda v=v: v)) for v in (0.0, 0.5, 1.0)}
    assert vals == {1530, 1800, 2070}
    assert sw.risk_cooldown(1, rnd=lambda: 0.5) == 45 * 60 and sw.risk_cooldown(9, rnd=lambda: 0.5) == 12 * 3600
    assert 36 * 60 <= sw.risk_cooldown(1, rnd=lambda: 0.0) and sw.risk_cooldown(1, rnd=lambda: 1.0) <= 54 * 60
