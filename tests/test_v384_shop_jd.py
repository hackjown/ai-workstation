"""2026-10-29, the JD price watch on the user's local VM (CDP 9222 fine, Chrome 155): the mobile share link landed on
JD's 活动火爆 risk page (trade.m.jd.com/common/limit.html), the desktop page was read before its price block rendered
(ready "interactive", only the store banner), profile dir "" / cookies null, and the manual check answered with an
empty body. Plus: the target is an after-coupon price (原价 ¥187, 券后 ¥137, 目标 ¥120)."""
import asyncio
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import shop_probe as sp
from aiw_api import shopwatch as sw

from test_shop_nocdp import Comp, Sched, _w, chat, store  # noqa: F401
from test_shopwatch import JD, stack  # noqa: F401

SKU = "10177280748947"
SHARE = f"https://item.m.jd.com/product/{SKU}.html?utm_user=plusmember&gx=RnAomTM&ad_od=share&utm_source=androidapp"
LIMIT = {"url": "https://trade.m.jd.com/common/limit.html?module=detail_m1&sceneType=1", "title": "京东",
         "text": "活动火爆 请选购其他商品 我知道了", "sel": [], "cand": [], "btn": [{"t": "我知道了", "disabled": False}],
         "ready": "complete", "cdp": "http://127.0.0.1:9222", "profile": {"port": 9222, "dir": "", "cookies": None}}
BANNER = {"url": f"https://item.jd.com/{SKU}.html", "title": "电容笔 适用Apple Pencil", "ready": "interactive",
          "text": "进店逛逛 关注店铺 搜全站", "sel": [], "cand": [], "btn": [], "jd_sku": SKU, "frames": [],
          "password_input": False, "cdp": "http://127.0.0.1:9222",
          "profile": {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome", "cookies": 40960, "logged_in": True}}

# pc_detailpage_wareBusiness (trimmed): list price, PLUS price, 到手价 and the coupon block
WARE_BUSINESS = json.dumps({
    "price": {"id": SKU, "p": "187.00", "op": "199.00", "m": "299.00", "tpp": "129.00", "plusTag": True},
    "finalPrice": {"price": "137.00", "priceContent": "到手价"},
    "couponInfo": [{"discount": "50", "quota": "150", "name": "满150减50"}],
    "preferenceVO": {"preferencePrice": "137.00", "title": "券后价"},
    "stockInfo": {"StockState": 33, "stockDesc": "有货"}}, ensure_ascii=False)
SOA = json.dumps({"price": {"p": "187.00", "op": "187.00", "id": SKU}, "estimatedPrice": "139.00",
                  "stock": {"StockState": 34, "stockDesc": "无货"}})
P3 = f'jQuery123([{{"cbf":"0","id":"J_{SKU}","m":"299.00","op":"199.00","p":"187.00"}}]);'


# ----------------------------------------------------------------------------- d. urls
def test_mobile_share_link_is_normalized():
    want = f"https://item.jd.com/{SKU}.html"
    assert sw.normalize_url(SHARE) == want
    assert sw.normalize_url(f"【京东】电容笔 {SHARE} 复制打开") == want
    assert sw.normalize_url(f"https://item.m.jd.com/{SKU}.html") == want
    assert sw.normalize_url(f"https://item.m.jd.com/ware/view.action?wareId={SKU}&sid=x") == want
    assert sw.normalize_url(f"https://item.jd.com/{SKU}.html#crumb-wrap") == want
    assert sw.normalize_url("https://detail.tmall.com/item.htm?id=6543") == "https://detail.tmall.com/item.htm?id=6543"
    assert sp.jd_sku_of(SHARE) == SKU and sp.jd_sku_of("https://example.com/product/1.html") == ""
    assert sw.watch_fields({"url": SHARE, "target_price": "120"}, default_machine="m")["url"] == want


async def test_stored_mobile_link_is_probed_as_desktop(store, chat):  # noqa: F811
    w = store.insert("watches", {**sw.watch_fields({"url": "https://item.jd.com/1.html", "target_price": "120"},
                                                   default_machine="m-nocdp"), "url": SHARE})
    comp = Comp(ensure={"ok": True, "port": 9222}, page={**JD, "cdp": "x"})
    seen = []
    orig = comp.execute

    async def execute(action, payload=None, **kw):
        seen.append((payload or {}).get("command", ""))
        return await orig(action, payload, **kw)
    comp.execute = execute
    await sw.check(Sched(store, comp), w, now=1000.0)
    assert any(f"--url 'https://item.jd.com/{SKU}.html'" in c for c in seen)
    assert store.get("watches", w["id"])["url"] == f"https://item.jd.com/{SKU}.html"


# ----------------------------------------------------------------------------- c. the 活动火爆 risk page
def test_limit_page_is_risk_limit_not_captcha():
    assert sp.is_risk_limit(LIMIT["url"]) and sp.is_risk_limit("https://item.jd.com/1.html", "活动火爆 请选购其他商品")
    r = sw.parse_snapshot(LIMIT)
    assert r["status"] == "risk_limit" and "活动火爆" in r["note"] and "不是验证码" in r["note"]
    assert not sw.is_order_url(LIMIT["url"]) and not sp.is_order_url(LIMIT["url"])


async def test_risk_limit_backs_off_for_hours_and_says_no_captcha(store, chat):  # noqa: F811
    w = _w(store)
    comp = Comp(ensure={"ok": True, "port": 9222}, page=LIMIT)
    sch = Sched(store, comp)
    res = await sw.check(sch, w, now=1000.0)
    w2 = store.get("watches", w["id"])
    assert res["result"]["status"] == "risk_limit" and w2["shop_status"] == "risk_limit"
    assert w2["next_check"] - 1000.0 >= 30 * 60     # v7: a per-computer cool-down from ~45 min, doubling
    assert "本地vm" in w2["status_note"]
    kinds = [(t, b) for _k, t, b in sch.notifier.sent]
    assert kinds and all("滑块" not in b for _t, b in kinds) and any("不是验证码" in b for _t, b in kinds)
    asks = sw._asks(w["id"])
    assert asks and "不是验证码" in asks[-1]["text"] and "手动打开" in asks[-1]["text"]


# ----------------------------------------------------------------------------- b. the price API fallback
def test_ware_business_after_coupon_price():
    got = sp.parse_api_price(WARE_BUSINESS)
    assert got["price"] == 187.0 and got["final_price"] == 137.0 and got["plus_price"] == 129.0 and got["in_stock"] is True


def test_soa_and_p3_responses():
    got = sp.parse_api_price(SOA)
    assert got["price"] == 187.0 and got["final_price"] == 139.0 and got["in_stock"] is False
    got = sp.parse_api_price(P3)
    assert got["price"] == 187.0 and got["final_price"] is None and got["plus_price"] is None
    assert sp.parse_api_price('jQuery1([{"id":"J_1","p":"-1.00"}])') is None
    assert sp.parse_api_price("<html>blocked</html>") is None


def test_snapshot_without_dom_price_uses_the_api():
    snap = {**BANNER, "api": [{"src": "pc_detailpage_wareBusiness", "error": "TypeError: Failed to fetch"},
                              {"src": "getWareBusiness", "status": 200, "body": WARE_BUSINESS}]}
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["via"] == "api" and r["price"] == 187.0 and r["final_price"] == 137.0
    assert r["effective"] == 137.0 and r["plus_price"] == 129.0 and "接口" in r["note"]
    r2 = sw.parse_snapshot({**BANNER, "api_price": {"price": 187.0, "final_price": None, "src": "p.3.cn"}})
    assert r2["effective"] == 187.0
    r3 = sw.parse_snapshot({**BANNER, "api": [{"src": "p.3.cn", "error": "x"}]})
    assert r3["status"] == "no_price" and "接口" in r3["note"]


# ----------------------------------------------------------------------------- the after-coupon price decides
COUPON_DOM = {"url": f"https://item.jd.com/{SKU}.html", "title": "电容笔", "ready": "complete",
              "text": "京东价 ¥187.00 满150减50 券后¥137 PLUS价 ¥129 加入购物车", "frames": [], "password_input": False,
              "sel": [{"role": "main", "q": ".p-price .price", "t": "187.00", "ctx": "京东价 ¥187.00", "fs": 22, "y": 300},
                      {"role": "plus", "q": ".p-price-plus .price", "t": "¥129", "ctx": "PLUS价 ¥129", "fs": 14, "y": 330}],
              "cand": [{"t": "¥187.00", "ctx": "京东价 ¥187.00", "cls": "p-price", "fs": 22, "y": 300},
                       {"t": "¥137", "ctx": "满150减50 券后¥137", "cls": "", "fs": 16, "y": 340},
                       {"t": "满150减50", "ctx": "满150减50 券后¥137", "cls": "", "fs": 12, "y": 340}],
              "btn": [{"t": "加入购物车", "disabled": False}],
              "profile": {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome", "cookies": 4096, "logged_in": True}}


def test_coupon_price_in_the_price_block_is_compared():
    r = sw.parse_snapshot(COUPON_DOM)
    assert r["price"] == 187.0 and r["final_price"] == 137.0 and r["effective"] == 137.0 and r["plus_price"] == 129.0
    hit, below, _ = sw.evaluate({"value": "120", "below": 0}, r)
    assert not hit and not below                                    # 137 after coupon is still above 120
    hit, below, msg = sw.evaluate({"value": "140", "below": 0}, r)
    assert hit and below and "原价 ¥187 · 券后 ¥137 · 目标 ¥140" in msg
    assert sw.price_line({"price": 187.0, "final_price": None}, "120") == "现价 ¥187 · 目标 ¥120"


def test_coupon_label_after_the_price():
    snap = {**COUPON_DOM, "sel": COUPON_DOM["sel"][:1],
            "cand": [{"t": "¥137", "ctx": "¥137券后 ¥187", "cls": "", "fs": 16, "y": 340},
                     {"t": "¥187", "ctx": "¥137券后 ¥187", "cls": "", "fs": 22, "y": 300}]}
    r = sw.parse_snapshot(snap)
    assert r["final_price"] == 137.0 and r["price"] == 187.0


# ----------------------------------------------------------------------------- e. which profile, logged in or not
def test_login_cookies_are_detected_by_name_only():
    now = 2_000_000_000
    on = [{"name": "pt_key", "value": "AAJ", "domain": ".jd.com", "expires": now + 86400},
          {"name": "pt_pin", "value": "user", "domain": ".jd.com", "expires": -1},
          {"name": "__jda", "value": "1", "domain": ".jd.com", "expires": -1},
          {"name": "SID", "value": "g", "domain": ".google.com"}]
    st = sp.jd_login_state(on, now)
    assert st == {"logged_in": True, "jd_cookies": 3, "login_cookies": ["pt_key", "pt_pin"], "cookies_total": 4}
    assert "AAJ" not in json.dumps(st)
    assert sp.jd_login_state([{"name": "thor", "value": "x", "domain": "passport.jd.com"}], now)["logged_in"] is True
    off = [{"name": "pt_pin", "value": "user", "domain": ".jd.com"},
           {"name": "pt_key", "value": "old", "domain": ".jd.com", "expires": now - 10}]
    assert sp.jd_login_state(off, now)["logged_in"] is False and sp.jd_login_state([], now)["logged_in"] is False


def test_not_logged_in_profile_is_need_login():
    snap = {**JD, "profile": {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome", "cookies": 2048, "logged_in": False}}
    r = sw.parse_snapshot(snap)
    assert r["status"] == "need_login" and "pt_key" in r["note"] and "aiw-chrome" in r["note"]
    assert sw.parse_snapshot({**JD, "profile": {"port": 9222, "dir": "", "cookies": None}})["status"] == "ok"


async def test_need_login_names_the_computer(store, chat):  # noqa: F811
    w = _w(store)
    page = {**JD, "profile": {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome", "cookies": 2048, "logged_in": False}}
    sch = Sched(store, Comp(ensure={"ok": True, "port": 9222}, page=page))
    res = await sw.check(sch, w, now=1000.0)
    assert res["result"]["status"] == "need_login" and "「本地vm」" in res["result"]["note"]
    assert any("本地vm" in t for _k, t, _b in sch.notifier.sent)


def test_profile_dir_from_a_setproctitle_cmdline(tmp_path):
    proc = tmp_path / "proc" / "77"
    proc.mkdir(parents=True)
    prof = tmp_path / "aiw-chrome"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_bytes(b"x" * 512)
    # Chrome rewrites its cmdline into one space-joined string padded with NULs
    (proc / "cmdline").write_bytes(f"/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir={prof} "
                                   "--restore-last-session".encode() + b"\0" * 40)
    p = sp.profile_of("http://127.0.0.1:9222", str(tmp_path / "proc"))
    assert p["dir"] == str(prof) and p["cookies"] == 512


def test_profile_dir_from_devtools_active_port(tmp_path):
    (tmp_path / "proc").mkdir()
    d = tmp_path / "chromium"
    d.mkdir()
    (d / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")
    p = sp.profile_of("http://127.0.0.1:9222", str(tmp_path / "proc"), home_dirs=(str(d),))
    assert p["dir"] == str(d) and p["cookies"] == 0


def test_probe_version_is_bumped():
    assert sp.PROBE_VERSION == "9"   # v9 (2026-11-01), v8 (2026-10-30), v7 (2026-10-29): see test_v388_jd_risk_limit.py


# ----------------------------------------------------------------------------- g. chrome_cdp.py
def test_missing_cdp_tool_does_not_break(tmp_path):
    cmd = sw.ship_command(tmp_path / "nope.py", sw.CDP_TOOL_REMOTE, "version")
    assert cmd.startswith("echo ") and "tool_missing" in cmd


async def test_submit_ships_chrome_cdp_first():
    from test_shopwatch import FakeComputer
    comp = FakeComputer({})
    assert await sw.install_cdp_tool(comp) is True
    assert comp.subs == ["version"]


# ----------------------------------------------------------------------------- f. the check endpoint is always JSON
async def test_check_endpoint_always_answers_json(stack, monkeypatch):  # noqa: F811
    from aiw_api import main as main_mod
    client, _scripts, _app = stack
    w = (await client.post("/api/watches", json={"url": SHARE, "mode": "shop", "value": "120"})).json()
    assert w["url"] == f"https://item.jd.com/{SKU}.html"

    async def boom(*a, **kw):
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(sw, "check", boom)
    r = await client.post(f"/api/watches/{w['id']}/check")
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is False and body["status"] == "error" and "probe exploded" in body["error"] and body["note"]

    async def hang(*a, **kw):
        await asyncio.sleep(30)
    monkeypatch.setattr(sw, "check", hang)
    monkeypatch.setattr(main_mod, "CHECK_TIMEOUT_S", 0.2)
    r = await client.post(f"/api/watches/{w['id']}/check")
    assert r.status_code == 200 and r.json()["status"] == "timeout" and r.json()["ok"] is False

    async def fine(sched, ww, now=None, manual=False):
        return {"ok": False, "error": "x", "result": {"status": "risk_limit", "note": "京东风控", "price": float("nan")}}
    monkeypatch.setattr(sw, "check", fine)
    r = await client.post(f"/api/watches/{w['id']}/check")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "risk_limit" and body["result"]["price"] is None and body["watch"]


# ----------------------------------------------------------------------------- a+b+e. cmd_probe against a fake DevTools
class FakeDevTools:
    """One browser + one background tab: the item page renders only the store banner, the price APIs answer
    one by one, the cookie store holds a JD login."""

    def __init__(self, snaps, api_bodies, cookies):
        self.snaps, self.api_bodies, self.cookies, self.calls, self.navs = list(snaps), dict(api_bodies), cookies, [], []

    def ws(self, url, timeout=15):
        dev = self

        class W:
            def call(self, method, params=None, timeout=20):
                dev.calls.append(method)
                if method == "Target.createTarget":
                    return {"targetId": "T1"}
                if method == "Storage.getCookies":
                    return {"cookies": dev.cookies}
                if method == "Page.navigate":
                    dev.navs.append(params["url"])
                return {}

            def eval(self, expr, timeout=20):
                if expr.startswith("(document.querySelector('#profile_path')"):
                    return "/home/aiw/.config/aiw-chrome/Default"
                if "__aiw_p" in expr:
                    src = json.loads(expr[expr.rindex(")(") + 2:].split(",", 1)[0])
                    b = dev.api_bodies.get(src)
                    return json.dumps({"src": src, "status": 200, "body": b} if b else {"src": src, "error": "Failed to fetch"})
                if "window.scrollBy" in expr:
                    dev.calls.append("scroll")
                    return 1
                if "location.href" in expr and "JSON.stringify" in expr:
                    return json.dumps(dev.snaps.pop(0) if len(dev.snaps) > 1 else dev.snaps[0])
                return None

            def close(self):
                pass
        return W()


def _run_probe(monkeypatch, dev, url, wait="4"):
    import argparse
    monkeypatch.setattr(sp, "WS", dev.ws)
    monkeypatch.setattr(sp, "browser_ws", lambda: dev.ws("x"))
    monkeypatch.setattr(sp, "pages", lambda: [{"id": "T1", "type": "page", "webSocketDebuggerUrl": "ws://t"}])
    monkeypatch.setattr(sp, "profile_of", lambda base, *a, **k: {"port": 9222, "dir": "", "cookies": None})
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    got = {}

    def fake_out(obj, code=0):
        got.update(obj)
        raise SystemExit(code)
    monkeypatch.setattr(sp, "out", fake_out)
    with pytest.raises(SystemExit):
        sp.cmd_probe(argparse.Namespace(url=url, sku="", shot=False, wait=wait))
    return got


def test_probe_waits_then_reads_the_api_and_the_login(monkeypatch):
    import itertools
    clock = itertools.count(0, 0.5)
    monkeypatch.setattr(sp.time, "time", lambda: 1000 + next(clock))
    cookies = [{"name": "pt_key", "value": "SECRETVAL", "domain": ".jd.com", "expires": -1},
               {"name": "pt_pin", "value": "u", "domain": ".jd.com", "expires": -1}]
    dev = FakeDevTools([BANNER], {"getWareBusiness": WARE_BUSINESS}, cookies)
    snap = _run_probe(monkeypatch, dev, SHARE, wait="8")   # v8: the budget's clock reads cost fake time too
    assert dev.navs[-1] == f"https://item.jd.com/{SKU}.html" and snap["asked_url"] == dev.navs[-1]
    assert ("scroll" in dev.calls or "Page.bringToFront" in dev.calls) and snap["jd_sku"] == SKU and snap["probe_version"] == sp.PROBE_VERSION
    assert snap.get("api_price"), snap.get("api")
    assert snap["api_price"]["src"] == "getWareBusiness" and snap["api_price"]["final_price"] == 137.0
    assert [a["src"] for a in snap["api"]] == ["pc_detailpage_wareBusiness", "getWareBusiness"]   # stops at the first
    assert snap["profile"]["logged_in"] is True and snap["profile"]["dir"] == "/home/aiw/.config/aiw-chrome"
    assert "SECRETVAL" not in json.dumps(snap)
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["effective"] == 137.0


def test_probe_stops_on_the_limit_page(monkeypatch):
    import itertools
    clock = itertools.count(0, 0.5)
    monkeypatch.setattr(sp.time, "time", lambda: 1000 + next(clock))
    dev = FakeDevTools([LIMIT], {"getWareBusiness": WARE_BUSINESS}, [])
    snap = _run_probe(monkeypatch, dev, f"https://item.jd.com/{SKU}.html", wait="20")
    assert snap["risk_limit"] is True and "api" not in snap and snap["profile"]["logged_in"] is False
    assert sw.parse_snapshot(snap)["status"] == "risk_limit"
