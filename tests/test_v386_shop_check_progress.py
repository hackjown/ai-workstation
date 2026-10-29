"""3.8.6 / probe v6 (2026-10-29). The user's JD watch on ai-linux-001 (CDP 9222, Chrome 155) read the title
("…–京东", i.e. document.title) but no price 12 times in a row, and 立即检查 gave no feedback while it ran.
v6: the hidden background tab is brought to the front, the page's own price API answers are read back, labelled
prices in the visible text / embedded JSON / item.m.jd.com are used, every stage is timed and streamed to the card,
and a failed read leaves a diagnosis (screenshot, final URL, title, text near the price, versions, timings)."""
import argparse
import asyncio
import itertools
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import shop_probe as sp
from aiw_api import shopwatch as sw

from test_shop_nocdp import Comp, Sched, _w, chat, store  # noqa: F401
from test_shopwatch import stack  # noqa: F401
from test_v384_shop_jd import BANNER, LIMIT, SKU, WARE_BUSINESS

KW = [{"t": "京东价 ¥187.00", "y": 300}, {"t": "到手价 ¥137", "y": 330}, {"t": "PLUS价 ¥129", "y": 345},
      {"t": "券后¥99", "y": 2600}]          # far down: a recommended item, not this one


# ----------------------------------------------------------------------------- server-side parsing
def test_text_prices_prefer_after_coupon_and_skip_plus():
    tp = sw.text_prices(KW)
    assert tp == {"price": 187.0, "final_price": 137.0, "plus_price": 129.0}
    assert sw.text_prices([{"t": "¥137到手", "y": 200}])["final_price"] == 137.0
    assert sw.text_prices([{"t": "满199减20 ¥20", "y": 200}])["price"] is None


def test_snapshot_with_only_text_blocks_is_read():
    r = sw.parse_snapshot({**BANNER, "kw": KW})
    assert r["status"] == "ok" and r["price"] == 187.0 and r["final_price"] == 137.0 and r["effective"] == 137.0
    assert r["plus_price"] == 129.0 and r["via"] == "text"


def test_embedded_json_for_the_sku():
    emb = [{"src": "pageConfig.product", "body": json.dumps({"skuid": SKU, "name": "x"})},
           {"src": "__INITIAL_STATE__", "body": json.dumps({"skuId": SKU, "jdPrice": "187.00", "finalPrice": {"price": "137.00"}})}]
    r = sw.parse_snapshot({**BANNER, "embedded": emb})
    assert r["status"] == "ok" and r["effective"] == 137.0 and r["via"] == "embedded"


def test_mobile_page_is_used_unless_it_is_the_limit_page():
    mob = {"url": f"https://item.m.jd.com/product/{SKU}.html", "ready": "complete", "text": "¥187 到手价 ¥137",
           "sel": [], "btn": [], "cand": [{"t": "¥187", "ctx": "¥187", "cls": "price", "fs": 26, "y": 420},
                                          {"t": "到手价 ¥137", "ctx": "到手价 ¥137", "cls": "", "fs": 14, "y": 450}]}
    r = sw.parse_snapshot({**BANNER, "mobile": mob})
    assert r["status"] == "ok" and r["via"] == "mobile" and r["effective"] == 137.0
    r = sw.parse_snapshot({**BANNER, "mobile": {"risk_limit": True, "skipped": "limit.html", "url": LIMIT["url"]}})
    assert r["status"] == "no_price"          # the mobile limit page is never risk_limit for the desktop page
    raw = {**BANNER, "title_from": "document.title", "mobile": {"risk_limit": True, "skipped": "limit.html"}}
    hints = sw.diag_hints(raw, r)
    assert any("没有渲染" in h for h in hints) and any("移动版" in h for h in hints)


def test_login_to_see_price_is_need_login():
    r = sw.parse_snapshot({**BANNER, "kw": [{"t": "登录查看价格", "y": 300}]})
    assert r["status"] == "need_login" and "登录查看价格" in r["note"]


def test_a_stray_number_no_longer_counts_as_the_jd_price():
    assert not sp.has_jd_price({"sel": [{"role": "main", "t": "满199减20"}, {"role": "any", "t": "关注 1.2万+"}]})
    assert sp.has_jd_price({"sel": [{"role": "main", "t": "¥187.00"}]}) and sp.has_jd_price({"sel": [{"role": "main", "t": "187.00"}]})
    assert sp.kw_price({"kw": [{"t": "到手价 ¥137", "y": 300}]}) and not sp.kw_price({"kw": [{"t": "到手价 ¥137", "y": 3000}]})


def test_stages_are_timed_and_written(tmp_path):
    p = tmp_path / "run.json"
    st = sp.Stages(str(p))
    st("connect")
    assert json.loads(p.read_text())["label"] == "连接浏览器"
    st("open")
    st.finish()
    assert not p.exists() and [x["stage"] for x in st.list] == ["connect", "open"] and all("ms" in x for x in st.list)


# ----------------------------------------------------------------------------- cmd_probe against a fake DevTools
class Dev:
    """The item page never renders a price while hidden; the page itself fetched pc_detailpage_wareBusiness."""

    def __init__(self, snaps, bodies=None, mobile=None, net=True):
        self.snaps, self.bodies, self.mobile, self.net = list(snaps), bodies or {}, mobile, net
        self.calls, self.navs = [], []

    def ws(self, url, timeout=15):
        dev = self

        class W:
            on_event = None

            def call(self, method, params=None, timeout=20):
                dev.calls.append(method)
                if method == "Target.createTarget":
                    return {"targetId": "T1"}
                if method == "Storage.getCookies":
                    return {"cookies": [{"name": "pt_key", "value": "S", "domain": ".jd.com", "expires": -1}]}
                if method == "Page.navigate":
                    dev.navs.append(params["url"])
                    if dev.net and self.on_event and "item.jd.com" in params["url"]:
                        self.on_event({"method": "Network.responseReceived", "params": {"requestId": "R1", "response": {
                            "url": "https://api.m.jd.com/?appid=pc-item-soa&functionId=pc_detailpage_wareBusiness&h5st=x",
                            "status": 200}}})
                        self.on_event({"method": "Network.responseReceived", "params": {"requestId": "R2", "response": {
                            "url": "https://api.m.jd.com/?functionId=pc_recommend_list", "status": 200}}})
                if method == "Network.getResponseBody":
                    return {"body": dev.bodies.get(params["requestId"], ""), "base64Encoded": False}
                if method == "Page.captureScreenshot":
                    return {"data": "QUJD"}
                return {}

            def eval(self, expr, timeout=20):
                if expr.startswith("(document.querySelector('#profile_path')"):
                    return "/home/aiw/.config/aiw-chrome/Default"
                if "__aiw_p" in expr:
                    src = json.loads(expr[expr.rindex(")(") + 2:].split(",", 1)[0])
                    return json.dumps({"src": src, "status": 200, "body": '{"code":"605","echo":"h5st is required"}'})
                if "location.href" in expr and "JSON.stringify" in expr:
                    if dev.navs and "item.m.jd.com" in dev.navs[-1] and dev.mobile is not None:
                        return json.dumps(dev.mobile)
                    return json.dumps(dev.snaps.pop(0) if len(dev.snaps) > 1 else dev.snaps[0])
                return None

            def close(self):
                pass
        return W()


def _probe(monkeypatch, dev, url, wait="20", progress=""):
    clock = itertools.count(0, 0.5)
    monkeypatch.setattr(sp.time, "time", lambda: 1000 + next(clock))
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
        sp.cmd_probe(argparse.Namespace(url=url, sku="", shot=False, wait=wait, progress=progress))
    return got


HIDDEN = {**BANNER, "ready": "complete", "visibility": "hidden", "title_from": "document.title", "kw": [], "n_elems": 900}


def test_hidden_tab_is_brought_forward_and_the_pages_own_price_api_is_read(monkeypatch, tmp_path):
    dev = Dev([HIDDEN], bodies={"R1": WARE_BUSINESS, "R2": '{"list":[{"p":"9.9","op":"19","m":"29"}]}'})
    prog = tmp_path / "run.json"
    snap = _probe(monkeypatch, dev, f"https://item.jd.com/{SKU}.html", progress=str(prog))
    assert "Page.bringToFront" in dev.calls and snap["fronted"]["how"] == "Page.bringToFront"
    assert "Network.enable" in dev.calls
    assert snap["api_price"]["src"] == "page:pc_detailpage_wareBusiness" and snap["api_price"]["final_price"] == 137.0
    assert "api" not in snap                                        # the unsigned API calls were not needed
    assert snap["probe_version"] == "6" and not prog.exists()
    stages = [s["stage"] for s in snap["stages"]]
    assert stages[:4] == ["connect", "open", "wait", "front"] and "net" in stages and stages[-1] == "done"
    r = sw.parse_snapshot(snap)
    assert r["status"] == "ok" and r["effective"] == 137.0 and r["via"] == "api"
    assert "mobile" not in snap and "screenshot" not in snap         # read fine: no diagnosis picture


def test_nothing_anywhere_gives_screenshot_and_skips_a_mobile_limit_page(monkeypatch):
    dev = Dev([HIDDEN], net=False, mobile={**LIMIT})
    snap = _probe(monkeypatch, dev, f"https://item.jd.com/{SKU}.html", wait="8")
    assert [a["src"] for a in snap["api"]] == ["pc_detailpage_wareBusiness", "getWareBusiness", "p.3.cn"]
    assert snap["screenshot"] == "QUJD" and snap["mobile"]["skipped"] == "limit.html"
    assert dev.navs[-1] == f"https://item.m.jd.com/product/{SKU}.html"
    assert "Emulation.clearDeviceMetricsOverride" in dev.calls        # the tab's mobile emulation is undone
    r = sw.parse_snapshot(snap)
    assert r["status"] == "no_price"
    hints = sw.diag_hints(snap, r)
    assert any("没有请求任何价格接口" in h for h in hints) and any("h5st" in h for h in hints)


# ----------------------------------------------------------------------------- the check: stages, diag, backoff reset
async def test_manual_check_resets_backoff_and_saves_a_diagnosis(store, chat, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("AIW_ARTIFACTS", str(tmp_path / "art"))
    page = {**HIDDEN, "probe_version": "6", "screenshot": "QUJD", "net_seen": 0, "api": [{"src": "p.3.cn", "error": "x"}],
            "stages": [{"stage": "connect", "label": "连接浏览器", "at_ms": 0, "ms": 100}], "snippets": ["进店逛逛"]}
    comp = Comp(ensure={"ok": True, "port": 9222}, page=page)
    w = _w(store)
    store.update("watches", w["id"], fail_count=12, next_check=99999999999)
    w = store.get("watches", w["id"])
    seen = []
    res = await sw.check(Sched(store, comp), w, now=1000.0, manual=True, progress=lambda ev: seen.append(ev["stage"]))
    w2 = store.get("watches", w["id"])
    assert w2["fail_count"] == 1 and w2["next_check"] < 1000 + 4 * 3600   # starts over, not 12 failures of backoff
    assert seen[:2] == ["machine", "push"] and "save" in seen
    d = res["diag"]
    assert d["status"] == "no_price" and d["probe_version"] == "6" and d["final_url"] == BANNER["url"]
    assert d["page_title"] == BANNER["title"] and d["title_from"] == "document.title" and d["snippets"] == ["进店逛逛"]
    assert d["screenshot"] is True and d["screenshot_url"] == f"/api/watches/{w['id']}/diag.jpg"
    assert (tmp_path / "art" / "shop_diag" / f"{w['id']}.jpg").read_bytes() == b"ABC"
    assert d["hints"] and d["probe_stages"][0]["label"] == "连接浏览器" and res["stages"][0]["stage"] == "machine"
    assert sw.get_diag(store, w["id"])["status"] == "no_price" and w2["diag_at"] == 1000.0
    assert "没有渲染" in w2["last_error"] or "价格接口" in w2["last_error"]   # the card says why


async def test_probe_progress_is_polled_while_it_runs(monkeypatch):
    monkeypatch.setattr(sw, "PROGRESS_FIRST_S", 0.01)
    monkeypatch.setattr(sw, "PROGRESS_POLL_S", 0.02)

    class Slow:
        machine_id = "m"

        def __init__(self):
            self.cmds = []

        async def execute(self, action, payload=None, observe=False, timeout=30):
            cmd = (payload or {}).get("command", "")
            self.cmds.append(cmd)
            out = {}
            if "probe.py" in cmd:
                await asyncio.sleep(0.3)
                out = {"url": "u", "probe_version": "6"}
            elif cmd.startswith("cat /tmp/aiw-shop-run-"):
                out = {"stage": "wait", "label": "等待价格渲染"}

            class R:
                result = {"stdout": json.dumps(out, ensure_ascii=False)}
            return R()

    got = []
    prog = sw.Progress(lambda ev: got.append(ev["label"]))
    comp = Slow()
    raw = await sw.run_probe(comp, "probe", timeout=5, progress=prog, url="https://item.jd.com/1.html")
    assert raw["probe_version"] == "6" and "等待价格渲染" in got
    assert any("--progress '/tmp/aiw-shop-run-" in c for c in comp.cmds)


# ----------------------------------------------------------------------------- the API: job + polling, diag
async def test_check_job_streams_stages_and_diag_endpoints(stack, monkeypatch, tmp_path):  # noqa: F811
    client, _scripts, app = stack
    w = (await client.post("/api/watches", json={"url": f"https://item.jd.com/{SKU}.html", "mode": "shop", "value": "120"})).json()
    assert (await client.get(f"/api/watches/{w['id']}/diag")).status_code == 404
    gate = asyncio.Event()

    async def fake(sched, ww, now=None, manual=False, progress=None):
        assert manual is True
        progress({"stage": "push", "label": "推送探针"})
        await gate.wait()
        progress({"stage": "wait", "label": "等待价格渲染", "src": "probe"})
        d = {"at": 5.0, "status": "no_price", "probe_version": "6", "hints": ["x"]}
        sw.save_diag(sched.store, sched.sessions, ww["id"], {"screenshot": "QUJD"}, d)
        return {"ok": False, "error": "页面上没有读到价格", "result": {"status": "no_price", "note": "页面上没有读到价格"},
                "diag": d, "stages": []}
    monkeypatch.setattr(sw, "check", fake)
    r = (await client.post(f"/api/watches/{w['id']}/check?async=1")).json()
    job = r["job"]
    assert job["state"] == "running" and job["id"]
    await asyncio.sleep(0.05)
    j = (await client.get(f"/api/watches/{w['id']}/check/{job['id']}")).json()
    assert j["stage"] == "push" and j["label"] == "推送探针"
    again = (await client.post(f"/api/watches/{w['id']}/check?async=1")).json()["job"]
    assert again["id"] == job["id"]                               # one run per watch at a time
    gate.set()
    for _ in range(50):
        j = (await client.get(f"/api/watches/{w['id']}/check/{job['id']}")).json()
        if j["state"] == "done":
            break
        await asyncio.sleep(0.02)
    assert j["state"] == "done" and j["result"]["status"] == "no_price" and j["label"].startswith("完成：")
    assert [s["stage"] for s in j["stages"]] == ["start", "push", "wait", "done"]
    assert j["result"]["diag"]["probe_version"] == "6" and j["result"]["server_version"]
    d = (await client.get(f"/api/watches/{w['id']}/diag")).json()
    assert d["status"] == "no_price" and d["screenshot"] is True
    img = await client.get(f"/api/watches/{w['id']}/diag.jpg")
    assert img.status_code == 200 and img.content == b"ABC" and img.headers["content-type"] == "image/jpeg"
    assert (await client.get(f"/api/watches/{w['id']}/check/nope")).status_code == 404


def test_web_and_ios_show_the_stage_and_diag():
    root = os.path.join(os.path.dirname(__file__), "..", "apps")
    js = open(os.path.join(root, "web", "shop.js"), encoding="utf-8").read()
    assert "check?async=1" in js and "shopDiag" in js and "查看诊断" in js and "检查中…" in js and 'class="spin"' in js
    sw_ = open(os.path.join(root, "ios", "Sources", "ShopCheck.swift"), encoding="utf-8").read()
    assert "check?async=1" in sw_ and "diag.jpg" in sw_ and "ShopDiagView" in sw_
    v = open(os.path.join(root, "ios", "Sources", "ShopWatchView.swift"), encoding="utf-8").read()
    assert "runWatchCheck" in v and "查看诊断" in v and ".disabled(busy != nil)" in v
    h = open(os.path.join(root, "ios", "Sources", "HomeSheets.swift"), encoding="utf-8").read()
    assert "runWatchCheck" in h
