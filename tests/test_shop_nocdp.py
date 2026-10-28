"""Price watch without a DevTools port (2026-10-28, "京东 ≤ ¥120" failed with NO_CDP: Connection refused):
another endpoint or a reopened Chrome first, the screen as a last resort, and otherwise a clear chat question with a
one-tap 立即重试 instead of a raw urlopen error. Plus JD's 价格待发布 and PLUS prices."""
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import chat as chat_mod
from aiw_api import shopwatch as sw
from vm_agent.browser import chrome_cdp as cc

from test_shopwatch import JD

NO_CDP = {"error": "NO_CDP: 9222: <urlopen error [Errno 111] Connection refused>"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from aiw_api import browseros
    from aiw_api.store import Store
    s = Store(str(tmp_path / "s.db"))
    sw.ensure_columns(s)
    chat_mod.ensure_tables(s)
    monkeypatch.setattr(browseros, "_STORE", s)     # BrowserOS off, no remembered port: a clean computer
    return s


class Notes:
    def __init__(self):
        self.sent = []

    async def notify(self, kind, title, body="", **kw):
        self.sent.append((kind, title, body))


class Comp:
    """A computer whose probe answers only on the port chrome_cdp opened; records every tool call."""
    machine_id = "m-nocdp"

    def __init__(self, ensure=None, page=JD, shot="QUJD"):
        self.ensure, self.page, self.shot, self.calls = ensure or {}, page, shot, []

    async def execute(self, action, payload=None, observe=False, timeout=30):
        cmd = (payload or {}).get("command", "")
        out = {}
        if "chrome_cdp.py" in cmd:
            sub = cmd.split('python3 "$f" ', 1)[1].split()[0]
            self.calls.append(("cdp", sub, cmd))
            out = {"ensure": self.ensure, "open-url": {"ok": True, "how": "new_tab"}, "close-tab": {"ok": True}}.get(sub, {})
        elif "probe.py" in cmd:
            port = self.ensure.get("port")
            ok = port and f"VM_AGENT_CDP=http://127.0.0.1:{port} " in cmd or (port == 9222 and "VM_AGENT_CDP" not in cmd
                                                                                and any(c[1] == "ensure" for c in self.calls))
            self.calls.append(("probe", ok))
            out = self.page if ok else NO_CDP
        else:
            self.calls.append((action, payload))

        class R:
            result = {"stdout": json.dumps(out, ensure_ascii=False)}
        return R()

    async def screenshot(self):
        class S:
            screenshot = self.shot
        return S()


class Vision:
    def __init__(self, answer):
        self.answer, self.asked = answer, 0

    async def describe(self, system, user, image_b64, max_tokens=900):
        self.asked += 1
        return json.dumps(self.answer)


class Sessions:
    class settings:
        machines = []

    def __init__(self, comp, vision=None):
        self.comp, self.vision = comp, vision

    def computer(self, mid):
        return self.comp

    def _vision_llm(self):
        return self.vision


class Sched:
    def __init__(self, store, comp, vision=None):
        self.store, self.sessions, self.notifier = store, Sessions(comp, vision), Notes()


@pytest.fixture
def chat(store):
    c = object.__new__(chat_mod.Chat)
    c.store, c.notifier, c.jobs = store, None, set()
    sw.bind(chat=c)
    yield c
    sw.bind(chat=None)
    sw.CHAT = None


def _w(store, target="120"):
    f = sw.watch_fields({"url": "https://item.jd.com/100012043978.html", "target_price": target}, default_machine="m-nocdp")
    return store.insert("watches", f)


async def test_reopened_chrome_is_used(store, chat):
    comp = Comp(ensure={"ok": True, "port": 9333, "how": "relaunched", "profile": "/home/aiw/.config/aiw-chrome"})
    sch = Sched(store, comp)
    w = _w(store, target="1700")
    res = await sw.check(sch, w)
    assert res["ok"] and res["result"]["effective"] == 1699.0, res
    ens = next(c for c in comp.calls if c[0] == "cdp")
    assert ens[1] == "ensure" and "--port '9222'" in ens[2] and "--also '9110'" in ens[2]
    assert ("probe", True) in comp.calls
    assert any("已重新打开 Chrome" in t for _, t, _ in sch.notifier.sent)
    assert sw.known_port("m-nocdp") == 9333                # the next check goes straight there
    assert sw.cdp_env(comp) == "VM_AGENT_CDP=http://127.0.0.1:9333 "


async def test_screen_fallback_reads_the_visible_browser(store, chat, monkeypatch):
    monkeypatch.setattr(sw, "SCREEN_WAIT", 0)
    comp = Comp(ensure={"ok": False, "reason": "port_not_opened", "error": "Chrome 已重启，但 9222 端口在 45 秒内没有打开"})
    vis = Vision({"price": 129, "final_price": 115, "in_stock": True, "page": "product", "title": "某某 耳机"})
    sch = Sched(store, comp, vis)
    w = _w(store)
    res = await sw.check(sch, w)
    assert res["ok"] and res["triggered"] and res["result"]["effective"] == 115 and res["result"]["via"] == "screen"
    subs = [c[1] for c in comp.calls if c[0] == "cdp"]
    assert subs == ["ensure", "open-url", "close-tab"]
    close = [c[2] for c in comp.calls if c[0] == "cdp" and c[1] == "close-tab"][0]
    assert "--title-has '京东'" in close                                   # closes only the tab it opened
    assert vis.asked == 1


async def test_no_page_at_all_asks_with_one_tap_retry(store, chat, monkeypatch):
    comp = Comp(ensure={"ok": False, "reason": "port_not_opened", "error": "Chrome 已重启，但 9222 端口在 45 秒内没有打开"})
    sch = Sched(store, comp)                                               # no vision model: nothing else to try
    w = _w(store)
    res = await sw.check(sch, w)
    assert not res["ok"] and res["result"]["status"] == "no_browser"
    note = res["result"]["note"]
    assert "Errno" not in note and "urlopen" not in note                   # never the raw error
    assert "update-agent.sh" in note and "立即重试" in note and "端口" in note
    asks = sw._asks(w["id"])
    assert len(asks) == 1 and asks[0]["data"]["options"] == [sw.OPT_RETRY, sw.OPT_PAUSE]
    assert asks[0]["data"]["ask_kind"] == "question" and "update-agent.sh" in asks[0]["text"]
    assert any(k == "watch_error" and "update-agent.sh" in b for k, _, b in sch.notifier.sent)
    # the same failure on the next scheduled check does not post again
    await sw.check(sch, store.get("watches", w["id"]))
    assert len(sw._asks(w["id"])) == 1
    # chat.fresh keeps a watch question answerable (no task session behind it)
    assert not chat_mod.Chat.fresh(chat, asks[0])["data"].get("stale")

    ran = []

    async def fake_check(sched, w2, now=None, manual=False):
        ran.append((w2["id"], manual))
        return {"ok": True}
    monkeypatch.setattr(sw, "check", fake_check)
    sw.bind(scheduler=sch)
    out = await chat_mod.Chat.answer(chat, asks[0]["id"], choice=sw.OPT_RETRY)
    assert out["ok"] and out["message"]["data"]["resolved"]["answer"] == sw.OPT_RETRY
    for t in list(chat.jobs):
        await t
    assert ran == [(w["id"], True)]
    with pytest.raises(ValueError):
        await chat_mod.Chat.answer(chat, asks[0]["id"], choice=sw.OPT_RETRY)   # answered already


async def test_pause_from_the_question(store, chat):
    w = _w(store)
    m = sw.post_ask(w, "读不到价格")
    out = await sw.answer_ask(chat, m, sw.OPT_PAUSE)
    assert out["ok"] and store.get("watches", w["id"])["enabled"] == 0


async def test_recovery_closes_the_question(store, chat):
    w = _w(store, target="1700")
    sw.post_ask(w, "读不到价格")
    store.update("watches", w["id"], shop_status="no_browser", fail_count=2)
    comp = Comp(ensure={"ok": True, "port": 9222, "how": "found"})
    res = await sw.check(Sched(store, comp), store.get("watches", w["id"]))
    assert res["ok"] and not sw._asks(w["id"])
    texts = [r["text"] for r in (chat_mod._row(x) for x in store._x("SELECT * FROM chat_messages WHERE kind='text'"))]
    assert any("恢复了" in t and "1699" in t for t in texts)


def test_no_browser_text_variants():
    t = sw.no_browser_text("ai-linux-001", "Chrome", {"reason": "no_browser_installed", "error": "x"})
    assert "apt install chromium" in t
    t = sw.no_browser_text("ai-linux-001", "Chrome", None, "NO_CDP: <urlopen error [Errno 111] Connection refused>")
    assert "「ai-linux-001」上的 Chrome" in t and "update-agent.sh" in t


# ----------------------------------------------------------------------------- 京东 page details
def test_jd_price_pending_is_not_a_failure():
    snap = {**JD, "text": "某新品 价格待发布 预约 ¥1999 推荐商品 ¥59", "sel": [{"role": "main", "q": ".p-price .price",
            "t": "价格待发布", "ctx": "京东价 价格待发布", "fs": 22, "y": 300}],
            "cand": [{"t": "¥59", "ctx": "推荐 ¥59", "cls": "", "fs": 14, "y": 900}], "btn": [{"t": "立即预约", "disabled": False}]}
    r = sw.parse_snapshot(snap)
    assert r["status"] == "price_pending" and "价格待发布" in r["note"] and r["effective"] is None


def test_jd_plus_price_is_reported_not_used():
    snap = {**JD, "sel": JD["sel"] + [{"role": "plus", "q": ".p-price-plus .price", "t": "¥1599", "ctx": "PLUS价 ¥1599",
                                       "fs": 14, "y": 320}]}
    r = sw.parse_snapshot(snap)
    assert r["effective"] == 1699.0 and r["plus_price"] == 1599.0


def test_jd_risk_pages_are_captcha():
    assert sw.parse_snapshot({**JD, "url": "https://iv.jd.com/slide?x=1"})["status"] == "captcha"


async def test_pending_watch_keeps_its_pace(store, chat):
    w = _w(store)
    snap = {**JD, "text": "价格待发布", "sel": [], "cand": []}
    comp = Comp(ensure={"ok": True, "port": 9222, "how": "found"}, page=snap)
    sch = Sched(store, comp)
    res = await sw.check(sch, w, now=1000.0)
    w2 = store.get("watches", w["id"])
    assert res["result"]["status"] == "price_pending" and w2["fail_count"] == 0 and w2["next_check"] == 1000.0 + 30 * 60
    assert not sw._asks(w["id"])


def test_cdp_tool_ships_from_the_vm_agent_package():
    assert sw.CDP_TOOL_SRC.is_file() and sw.CDP_TOOL_SRC.read_text() == open(cc.__file__).read()
    cmd = sw.ship_command(sw.CDP_TOOL_SRC, sw.CDP_TOOL_REMOTE, "close-tab", title_has=["京东", "JD"])
    assert cmd.endswith("close-tab  --title-has '京东' --title-has 'JD'") or "--title-has '京东' --title-has 'JD'" in cmd
