"""Probe v10 (2026-11-01 21:21, ai-linux-001, item 10177280748947). The item tab stayed 'loading' with 233
elements and its renderer stopped answering: the signed API (20 s), the unsigned APIs (24 s), the screenshot and then
the cart route, navigated in that same frozen tab, all waited out Runtime.evaluate timeouts and the cart said '列表里没有
这个商品', while the user's own cart tab on the same screen showed every price.

v10: an open cart / 我的关注 tab is read first (attached, reloaded once, never navigated, closed or fronted) and no new
tab is opened when it prices the sku; a frozen tab is dropped and the alt routes run in a fresh tab; a page that never
answered is 'no_answer', not 'not_listed'; a sku priced from the cart goes to the cart first next time."""
import argparse
import itertools
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import shop_probe as sp
from aiw_api import shopwatch as sw

from test_v384_shop_jd import SKU
from test_v388_jd_risk_limit import DESKTOP, Dev
from test_v389_jd_probe_v8 import SKELETON

ROW = [{"text": "Apple Pencil ¥277.11", "name": "Apple Pencil 二代 电容笔",
        "prices": [{"t": "¥277.11", "strike": False, "fs": 16, "cls": "price"}]}]
CART_TAB = {"id": "U1", "type": "page", "url": "https://cart.jd.com/cart_index", "webSocketDebuggerUrl": "ws://cart"}
PROBE_TAB = {"id": "T1", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://t"}


class CartTab:
    """The user's own cart tab: rows drawn, answers every read."""

    def __init__(self, rows=None, frozen=False):
        self.rows, self.frozen, self.calls = rows, frozen, []

    def __call__(self, url, timeout=15):
        tab = self

        class W:
            on_event = None
            bodies = {}

            def call(self, method, params=None, timeout=20):
                tab.calls.append(method)
                return {}

            def request(self, *a, **k):
                pass

            def pump(self, *a, **k):
                pass

            def eval(self, expr, timeout=20):
                if tab.frozen:
                    raise TimeoutError("Runtime.evaluate")
                if "n_links" in expr:
                    return json.dumps({"url": CART_TAB["url"], "rows": tab.rows or [], "n_links": len(tab.rows or []),
                                       "login": False, "text": ""})
                return 1

            def close(self):
                pass
        return W()


def _run(monkeypatch, dev, tabs, cart=None, budget="", step=0.5):
    clock = itertools.count(0, step)
    monkeypatch.setattr(sp.time, "time", lambda: 1000 + next(clock))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "WS", lambda u, t=15: cart(u, t) if u == "ws://cart" else dev.ws(u, t))
    monkeypatch.setattr(sp, "browser_ws", lambda: dev.ws("x"))
    monkeypatch.setattr(sp, "pages", lambda: list(tabs))
    monkeypatch.setattr(sp, "profile_of", lambda base, *a, **k: {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome"})
    monkeypatch.setattr(sp, "_watchdog", lambda total: (lambda: None))
    got = {}

    def fake_out(obj, code=0):
        got.update(obj)
        raise SystemExit(code)
    monkeypatch.setattr(sp, "out", fake_out)
    with pytest.raises(SystemExit):
        sp.cmd_probe(argparse.Namespace(url=DESKTOP, sku="", shot=False, wait="30", progress="", route="auto", budget=budget))
    return got


def test_the_open_cart_tab_prices_it_without_opening_the_item_page(monkeypatch):
    dev, cart = Dev(item=SKELETON), CartTab(rows=ROW)
    snap = _run(monkeypatch, dev, [CART_TAB, PROBE_TAB], cart)
    assert snap["route"] == "tab" and snap["alt_price"]["price"] == 277.11 and snap["alt_price"]["src"] == "cart"
    assert "Target.createTarget" not in dev.calls and not dev.navs          # no new tab, no item page
    assert "Page.reload" in cart.calls and "Page.navigate" not in cart.calls  # refreshed in place, never navigated
    assert "Target.closeTarget" not in dev.calls                            # the user's tab stays
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["price"] == 277.11 and "购物车" in r["note"] and r["title"] == "Apple Pencil 二代 电容笔"
    assert sp.remembered_route(SKU) == "cart"


def test_a_cart_tab_without_this_sku_falls_back_to_the_item_page(monkeypatch):
    dev, cart = Dev(item=SKELETON), CartTab(rows=[])
    snap = _run(monkeypatch, dev, [CART_TAB, PROBE_TAB], cart)
    assert "Target.createTarget" in dev.calls and DESKTOP in dev.navs
    assert snap["alt"][0]["route"] == "tab_cart" and snap["alt"][0]["missing"] == "not_listed"


def test_a_frozen_cart_tab_is_no_answer_not_not_listed(monkeypatch):
    dev, cart = Dev(item=SKELETON), CartTab(rows=ROW, frozen=True)
    snap = _run(monkeypatch, dev, [CART_TAB, PROBE_TAB], cart)
    t = snap["alt"][0]
    assert t["route"] == "tab_cart" and t["missing"] == "no_answer"
    assert any("没有响应" in h for h in sw.diag_hints(snap, sw.parse_snapshot(snap)))


class HungDev(Dev):
    """The item tab freezes after navigation; a NEW tab (second createTarget) works and its cart shows the row."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.targets = 0

    def ws(self, url, timeout=15):
        w = super().ws(url, timeout)
        dev, call0, eval0 = self, w.call, w.eval

        def call(method, params=None, timeout=20):
            if method == "Target.createTarget":
                dev.targets += 1
                dev.calls.append(method)
                return {"targetId": f"T{dev.targets}"}
            return call0(method, params, timeout)

        def ev(expr, timeout=20):
            if dev.targets == 1 and dev.navs:
                raise TimeoutError("Runtime.evaluate")
            return eval0(expr, timeout)
        w.call, w.eval = call, ev
        return w


def test_a_frozen_item_tab_is_dropped_and_the_cart_is_read_in_a_fresh_tab(monkeypatch):
    dev = HungDev(item=SKELETON, cart_rows=ROW)
    tabs = [{"id": "T1", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://t"},
            {"id": "T2", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://t2"}]
    snap = _run(monkeypatch, dev, tabs, CartTab())
    assert snap.get("hung") is True and dev.targets == 2
    assert "Page.captureScreenshot" not in dev.calls                         # no 5 s picture of a frozen tab
    assert snap["alt_price"]["src"] == "cart" and snap["alt_price"]["price"] == 277.11
    stages = [s["stage"] for s in snap["stages"]]
    assert "fresh" in stages and "api" not in stages
    assert sw.parse_snapshot(snap)["status"] == "ok"


def test_cart_first_after_the_cart_priced_it(monkeypatch):
    sp.remember_route(SKU, "cart")
    dev = Dev(item=SKELETON, cart_rows=ROW)
    snap = _run(monkeypatch, dev, [PROBE_TAB], CartTab())
    assert snap["route"] == "cart_first" and dev.navs == [sp.CART_URL]      # the item page was never opened
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["price"] == 277.11


def test_longer_waits_still_fit_under_the_cap():
    assert sp.WAIT_MAX_S >= 25 and sp.SKELETON_AFTER_S >= 10
    assert sw.PROBE_BUDGET + 12 < sw.PROBE_TIMEOUT and sp.DEFAULT_BUDGET + 12 < sw.PROBE_TIMEOUT


def test_the_shipped_probe_fits_in_one_shell_argument():
    """3.9.10 hotfix: '/bin/sh -c CMD' takes at most 128 KiB per argument; the base64 of probe v10 alone was 147 KB."""
    cmd = sw.probe_command("probe", url=DESKTOP, budget="150", progress="/tmp/x.json")
    assert len(cmd.encode()) < 100_000 and "gzip -dc" in cmd
