"""Probe v8 (2026-10-30). Two diagnostics from the 102 deployment:

A. 12:40 'probe failed: timeout after 160s', probe 'v?': the stages added up to more than the server's cap
   (wait 36 s, unsigned APIs 45 s, screenshot 27 s, cart 24 s, 关注 14 s, run one after another), so the probe was
   killed before it printed anything.
B. 12:44 v7 succeeded only through the signed API: item.jd.com answered a 295-element page with JD's site-wide
   title (a skeleton / soft block), Network.getResponseBody said 'No resource with given identifier', and the watch
   took '京东(JD.COM)-正品低价、品质保障、配送及时、轻松购物!' as the product name.

v8: one time budget (115 s, under the 160 s cap) with short stage timeouts and a watchdog that still prints the
partial result; a skeleton stops the render wait early and the signed API runs first; the screenshot takes ≤5 s and
only when there is no price; cart / 我的关注 only while time remains; bodies read at loadingFinished; the product
name comes from JD's data, never the generic title (stored ones are repaired); the probe version is always known."""
import argparse
import itertools
import json
import os
import socket

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import shop_probe as sp
from aiw_api import shopwatch as sw

from test_shop_nocdp import Notes, Sessions, chat, store  # noqa: F401
from test_v384_shop_jd import SKU, WARE_BUSINESS
from test_v388_jd_risk_limit import DESKTOP, Dev

GENERIC = "京东(JD.COM)-正品低价、品质保障、配送及时、轻松购物!"
SKELETON = {"url": DESKTOP, "title": GENERIC, "doc_title": GENERIC, "title_from": "document.title", "ready": "complete",
            "visibility": "visible", "n_elems": 295, "sel": [], "cand": [], "kw": [], "btn": [], "text": "京东 首页"}
SIGNED_OK = {"src": "signed:pc_detailpage_wareBusiness", "status": 200, "signed": True,
             "body": json.dumps({**json.loads(WARE_BUSINESS), "wareInfo": {"skuId": SKU, "wname": "Apple Pencil 二代 电容笔"}},
                                ensure_ascii=False)}


def _probe(monkeypatch, dev, url=DESKTOP, budget="", wait="20", step=0.5):
    clock = itertools.count(0, step)
    monkeypatch.setattr(sp.time, "time", lambda: 1000 + next(clock))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "WS", dev.ws)
    monkeypatch.setattr(sp, "browser_ws", lambda: dev.ws("x"))
    monkeypatch.setattr(sp, "pages", lambda: [{"id": "T1", "type": "page", "webSocketDebuggerUrl": "ws://t"}])
    monkeypatch.setattr(sp, "profile_of", lambda base, *a, **k: {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome"})
    monkeypatch.setattr(sp, "_watchdog", lambda total: (lambda: None))
    got = {}

    def fake_out(obj, code=0):
        got.update(obj)
        raise SystemExit(code)
    monkeypatch.setattr(sp, "out", fake_out)
    with pytest.raises(SystemExit):
        sp.cmd_probe(argparse.Namespace(url=url, sku="", shot=False, wait=wait, progress="", route="auto", budget=budget))
    return got


# ----------------------------------------------------------------------------- run B: skeleton page
def test_generic_title_is_never_a_name():
    assert sp.is_generic_title(GENERIC) and sp.is_generic_title("京东(JD.COM)-正品低价、品质保障、配送及时、轻松购物！")
    assert sp.is_generic_title("") and not sp.is_generic_title("Apple Pencil 二代 电容笔")
    assert sp.clean_name("Apple Pencil 电容笔【行情 报价 价格 评测】-京东") == "Apple Pencil 电容笔"
    assert sp.clean_name(GENERIC) == ""
    assert sp.name_of(SIGNED_OK["body"], SKU) == "Apple Pencil 二代 电容笔"
    assert sp.name_of({"coupons": [{"name": "满150减50优惠券"}]}, SKU) == ""      # a coupon's name is not the product's


def test_skeleton_page_stops_waiting_and_the_signed_api_goes_first(monkeypatch):
    dev = Dev(item=SKELETON, signed=SIGNED_OK, cart_rows=[{"prices": [{"t": "¥187.00"}]}])
    snap = _probe(monkeypatch, dev)
    assert snap["skeleton"] is True
    wait = next(s for s in snap["stages"] if s["stage"] == "wait")
    assert wait["ms"] < 15000                                           # v7 waited 25-36 s here
    order = [s["stage"] for s in snap["stages"]]
    assert order.index("signed") < order.index("done") and "net" not in order and "api" not in order
    assert "shot" not in order and "screenshot" not in snap                # priced: no picture
    assert dev.navs == [DESKTOP]                                          # no cart / 我的关注 trip
    assert snap["api_price"]["src"] == "signed" and snap["item_name"] == "Apple Pencil 二代 电容笔"
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["effective"] == 137.0 and r["title"] == "Apple Pencil 二代 电容笔"
    d = sw.build_diag(snap, r, None, 1.0, False)
    assert d["probe_version"] == sp.PROBE_VERSION and any("骨架页" in h for h in d["hints"])


def test_skeleton_without_a_name_keeps_the_title_empty(monkeypatch):
    signed = {**SIGNED_OK, "body": WARE_BUSINESS}
    snap = _probe(monkeypatch, Dev(item=SKELETON, signed=signed))
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["title"] == ""                       # never the generic title


# ----------------------------------------------------------------------------- run A: the budget
def test_slow_routes_stay_inside_the_budget(monkeypatch):
    """Nothing prices; every clock read costs 2 s. The probe still answers inside its budget and skips the page
    routes it has no time for, instead of being killed by the server's 160 s cap."""
    dev = Dev(item=SKELETON)
    snap = _probe(monkeypatch, dev, budget="60", step=2.0)
    total = max(s["at_ms"] + s.get("ms", 0) for s in snap["stages"])
    assert total < 60_000 + 15_000
    skipped = [a["route"] for a in snap.get("alt") or [] if a.get("skipped") == "no_time"]
    assert skipped                                                       # cart / 我的关注 waited for time that was not there
    assert snap["budget_s"] == 60.0


def test_watchdog_returns_partial_results_with_the_stage(monkeypatch):
    def boom(*a, **k):
        raise sp.OutOfTime()
    monkeypatch.setattr(sp, "signed_price", boom)
    snap = _probe(monkeypatch, Dev(item=SKELETON))
    assert snap["timed_out"] is True and "用京东签名接口读价" in snap["stage_error"]
    assert snap["probe_version"] == sp.PROBE_VERSION and snap["stages"][-1]["stage"] == "done"
    r = sw.parse_snapshot(snap)
    assert r["status"] != "ok" and "超时" in r["note"] and r["timed_out"]


def test_response_bodies_are_read_at_loading_finished():
    class W:
        def __init__(self):
            self.bodies, self.calls = {"R1": WARE_BUSINESS}, []

        def call(self, method, params=None, timeout=20):
            self.calls.append((method, params))
            raise RuntimeError("Network.getResponseBody: No resource with given identifier found")

        def pump(self, s):
            pass
    ws = W()
    tries, got = sp.net_prices(ws, {"R1": {"url": "https://api.m.jd.com/?functionId=pc_detailpage_wareBusiness", "status": 200},
                                    "R2": {"url": "https://api.m.jd.com/relsearch", "status": 200}}, SKU)
    assert got and got["final_price"] == 137.0 and not ws.calls          # from the body kept at loadingFinished
    ws.bodies = {}
    tries, got = sp.net_prices(ws, {"R2": {"url": "https://api.m.jd.com/?functionId=relsearch", "status": 200}}, SKU)
    assert got is None and tries[0].get("gone") is True and "error" not in tries[0]


def test_websocket_frames_survive_a_timeout_in_the_middle():
    a, b = socket.socketpair()
    ws = object.__new__(sp.WS)
    ws.sock, ws.buf, ws.base_timeout, ws.ids, ws.on_event, ws.pending, ws.bodies = a, b"", 2, 0, None, {}, {}
    msg = json.dumps({"id": 1, "result": {"x": "y" * 300}}).encode()
    frame = bytes([0x81, 126]) + len(msg).to_bytes(2, "big") + msg
    b.sendall(frame[:50])
    with pytest.raises(socket.timeout):
        ws.recv(0.2)
    b.sendall(frame[50:])
    assert json.loads(ws.recv(1))["result"]["x"] == "y" * 300
    a.close(), b.close()


# ----------------------------------------------------------------------------- the server
class SilentComp:
    """The probe is killed by the cap (no JSON on stdout); its progress file says how far it got."""
    machine_id = "m-nocdp"

    def __init__(self):
        self.cmds = []

    async def execute(self, action, payload=None, observe=False, timeout=30):
        cmd = (payload or {}).get("command", "")
        self.cmds.append(cmd)
        if cmd.startswith("cat /tmp/aiw-shop-run-"):
            out = json.dumps({"stage": "cart", "label": "从购物车读价", "probe_version": "8",
                              "stages": [{"stage": "connect", "label": "连接浏览器", "at_ms": 0},
                                         {"stage": "cart", "label": "从购物车读价", "at_ms": 150000}]}, ensure_ascii=False)
        else:
            out = ""

        class R:
            result = {"stdout": out, "stderr": "timeout after 160s"}
        return R()


async def test_a_killed_probe_still_says_its_version_and_where_it_stopped(store, chat):  # noqa: F811
    comp = SilentComp()
    raw = await sw.run_probe(comp, "probe", timeout=sw.PROBE_TIMEOUT, url=DESKTOP)
    assert "--budget '150'" in comp.cmds[0] and "--progress" in comp.cmds[0]
    assert raw["pushed_version"] == sp.PROBE_VERSION and raw["probe_version"] == "8"
    assert raw["stage_error"] == "卡在「从购物车读价」"
    r = sw.parse_snapshot(raw)
    assert r["status"] == "error" and r["note"].startswith("检查超时（160 秒没有结束）") and "从购物车读价" in r["note"]
    d = sw.build_diag(raw, r, None, 1.0, False)
    assert d["probe_version"] == "8" and d["timed_out"] and "超时" in d["error"]


def test_budget_is_under_the_cap():
    assert sw.PROBE_BUDGET + 12 < sw.PROBE_TIMEOUT and sp.DEFAULT_BUDGET + 12 < sw.PROBE_TIMEOUT
    assert sp.WAIT_MAX_S <= 30 and sp.SHOT_MAX_S <= 5


def test_errors_read_in_chinese():
    assert sw.cn_error("probe failed: timeout after 160s") == "检查超时（160 秒没有结束）"
    assert sw.cn_error("getWareBusiness TypeError: Failed to fetch").endswith("请求被浏览器拦下")


async def test_generic_titles_are_repaired_and_never_stored(store, chat):  # noqa: F811
    f = sw.watch_fields({"url": DESKTOP, "target_price": "120", "name": "我的电容笔"}, default_machine="m-nocdp")
    w = store.insert("watches", f)
    store.update("watches", w["id"], item_title=GENERIC)
    g = store.insert("watches", {**f, "name": GENERIC})
    rows = {x["id"]: x for x in sw.repair_titles(store)}
    assert rows[w["id"]]["item_title"] is None and rows[w["id"]]["name"] == "我的电容笔"   # the user's own name shows
    assert rows[g["id"]]["name"] == f"京东商品 {SKU}"
    assert store.get("watches", w["id"])["item_title"] is None
    r = {"status": "ok", "price": 187.0, "final_price": 137.0, "effective": 137.0, "in_stock": True, "title": GENERIC, "note": ""}
    await sw.apply_result(store, Sessions(None), Notes(), store.get("watches", w["id"]), r, 1000.0, 1800, "m-nocdp")
    assert store.get("watches", w["id"])["item_title"] is None


async def test_watch_list_api_repairs_titles_and_translates_errors(store):  # noqa: F811
    f = sw.watch_fields({"url": DESKTOP, "target_price": "120"}, default_machine="m-nocdp")
    w = store.insert("watches", f)
    store.update("watches", w["id"], item_title=GENERIC, last_error="probe failed: timeout after 160s")
    rows = sw.repair_titles(store)
    assert rows[0]["item_title"] is None
    assert sw.cn_error(store.get("watches", w["id"])["last_error"]).startswith("检查超时")
