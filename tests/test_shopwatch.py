"""京东/淘宝 price watches: snapshot parsing, transition logic, and the confirm gate (never 提交订单/pay without
the user's approval)."""
import asyncio
import json
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import shopwatch as sw
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from llm_router import LLMRouter, ScriptedProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18791

JD = {"url": "https://item.jd.com/100012043978.html", "title": "Apple AirPods Pro 2", "ready": "complete",
      "text": "Apple AirPods Pro 2 京东价 ¥1899.00 到手价 ¥1699 加入购物车 立即购买",
      "sel": [{"role": "main", "q": ".p-price .price", "t": "1899.00", "ctx": "京东价 ￥1899.00", "fs": 22, "y": 300},
              {"role": "final", "q": "#J_FinalPrice", "t": "¥1699", "ctx": "到手价 ¥1699", "fs": 16, "y": 340}],
      "cand": [{"t": "¥1899.00", "ctx": "京东价 ¥1899.00", "cls": "p-price", "fs": 22, "y": 300},
               {"t": "满1000减200", "ctx": "", "cls": "", "fs": 12, "y": 360}],
      "btn": [{"t": "加入购物车", "disabled": False}, {"t": "立即购买", "disabled": False}], "frames": [], "password_input": False}

TB = {"url": "https://detail.tmall.com/item.htm?id=6543", "title": "某某 机械键盘", "ready": "complete",
      "text": "机械键盘 ¥299 券后 ¥259 原价 ¥399 领券购买 加入购物车",
      "sel": [],
      "cand": [{"t": "¥299", "ctx": "¥299 券后 ¥259", "cls": "Price--priceText--abc", "fs": 30, "y": 420},
               {"t": "券后 ¥259", "ctx": "券后 ¥259", "cls": "Price--extra", "fs": 14, "y": 460},
               {"t": "¥399", "ctx": "原价 ¥399", "cls": "", "fs": 12, "y": 470, "strike": True},
               {"t": "¥5", "ctx": "运费 ¥5", "cls": "", "fs": 12, "y": 900}],
      "btn": [{"t": "领券购买", "disabled": False}, {"t": "加入购物车", "disabled": False}], "frames": [], "password_input": False}


def test_urls_and_sites():
    assert sw.site_of("https://item.jd.com/1.html") == "jd"
    assert sw.site_of("https://detail.tmall.com/item.htm?id=1") == "tmall"
    assert sw.site_of("https://item.taobao.com/item.htm?id=1") == "taobao"
    assert sw.site_of("https://m.tb.cn/h.abc") == "taobao"
    assert sw.site_of("https://www.amazon.com/x") is None
    share = "【淘宝】限时特惠 https://m.tb.cn/h.5Xyz?tk=abc 「机械键盘」点击链接直接打开"
    assert sw.extract_url(share) == "https://m.tb.cn/h.5Xyz?tk=abc"
    assert sw.normalize_url("https://item.m.jd.com/product/100012.html") == "https://item.jd.com/100012.html"


def test_parse_price():
    assert sw.parse_price("¥199.00") == (199.0, False)
    assert sw.parse_price("￥199-299") == (199.0, True)
    assert sw.parse_price("1,299.50元") == (1299.5, False)
    assert sw.parse_price("无") == (None, False)


def test_parse_jd_final_price_and_stock():
    r = sw.parse_snapshot(JD)
    assert r["status"] == "ok" and r["price"] == 1899.0 and r["final_price"] == 1699.0
    assert r["effective"] == 1699.0 and r["in_stock"] is True


def test_parse_tmall_coupon_price_ignores_strike_and_shipping():
    r = sw.parse_snapshot(TB)
    assert r["price"] == 299.0 and r["final_price"] == 259.0 and r["effective"] == 259.0 and r["in_stock"] is True


def test_parse_walls_and_stock():
    assert sw.parse_snapshot({**JD, "url": "https://passport.jd.com/new/login.aspx"})["status"] == "login"
    assert sw.parse_snapshot({**TB, "frames": ["https://login.taobao.com/member/login.jhtml"]})["status"] == "login"
    cap = {"url": "https://detail.tmall.com/item.htm?id=1&x5secdata=abc", "text": "请拖动下方滑块完成验证"}
    assert sw.parse_snapshot(cap)["status"] == "captcha"
    assert sw.parse_snapshot({**JD, "text": "很抱歉，该商品已下柜"})["status"] == "off_shelf"
    out = sw.parse_snapshot({**JD, "btn": [{"t": "到货通知", "disabled": False}, {"t": "立即购买", "disabled": True}]})
    assert out["in_stock"] is False and out["effective"] == 1699.0
    assert sw.parse_snapshot({"error": "NO_CDP: refused"})["status"] == "no_browser"
    assert sw.parse_snapshot({**JD, "sel": [], "cand": []})["status"] == "no_price"


def test_transition_only():
    w = {"value": "1700", "below": 0}
    r = sw.parse_snapshot(JD)
    hit, below, msg = sw.evaluate(w, r)
    assert hit and below and "1699" in msg
    assert sw.evaluate({**w, "below": 1}, r)[:2] == (False, True)           # still low: no repeat
    up = {**r, "effective": 1800.0}
    assert sw.evaluate({**w, "below": 1}, up)[:2] == (False, False)         # back up: re-arm
    no_stock = {**r, "in_stock": False}
    assert sw.evaluate(w, no_stock)[:2] == (False, False)                   # low but sold out: not a hit
    assert sw.evaluate(w, {**r, "status": "login"})[:2] == (False, False)


def test_payable():
    assert sw.parse_payable({"pay": ["应付总额：¥1,698.00"]}) == 1698.0
    assert sw.parse_payable({"pay": ["商品总额 ¥1899", "实付款 ￥1699.00"]}) == 1699.0
    assert sw.parse_payable({"pay": [], "text": "合计: 259.00"}) == 259.0
    assert sw.parse_payable({"pay": []}) is None


def test_watch_fields_validation():
    f = sw.watch_fields({"url": "【京东】https://item.jd.com/1.html 好物", "target_price": "199", "interval_min": 3,
                         "auto_order": True}, default_machine="m1")
    assert f["mode"] == "shop" and f["site"] == "jd" and f["interval_min"] == 10 and f["auto_order"] == 1
    assert f["machine_id"] == "m1" and f["value"] == "199" and f["quantity"] == 1
    with pytest.raises(ValueError):
        sw.watch_fields({"url": "https://example.com/x", "target_price": 1})
    with pytest.raises(ValueError):
        sw.watch_fields({"url": "https://item.jd.com/1.html", "target_price": "abc"})


# ----------------------------------------------------------------------------- the gate
def _probe(answers):
    calls = []

    async def probe(sub, **kw):
        calls.append((sub, kw))
        return answers.get(sub, {})
    return probe, calls


async def test_gate_blocks_submit_and_pay_before_confirm():
    st = {"approved": False, "submitted": False}
    probe, calls = _probe({"url": {"url": "https://trade.jd.com/shopping/order/getOrderInfo.action"},
                           "elem-at": {"url": "https://trade.jd.com/shopping/order/getOrderInfo.action", "text": "提交订单"}})
    assert await sw.gate(st, "browser_click", {"text": "提交订单"}, probe)
    assert await sw.gate(st, "browser_click", {"text": "立即支付"}, probe)
    assert await sw.gate(st, "click_element", {"target": "提交订单按钮"}, probe)
    assert await sw.gate(st, "click", {"x": 900, "y": 700}, probe, scale=2.0)
    assert calls[-1] == ("elem-at", {"x": 1800, "y": 1400})                  # screen pixels, not screenshot pixels
    assert await sw.gate(st, "keypress", {"key": "Return"}, probe)
    assert await sw.gate(st, "shell", {"command": "DISPLAY=:0 xdotool mousemove 900 700 click 1"}, probe)
    assert await sw.gate(st, "browser_click", {"text": "立即下单"}, probe)    # on the order page
    assert await sw.gate(st, "browser_click", {"text": "黑色 256GB"}, probe) is None
    assert await sw.gate(st, "browser_click", {"text": "立即购买"}, probe) is None
    assert await sw.gate(st, "type", {"text": "2"}, probe) is None
    st["approved"] = True
    assert await sw.gate(st, "browser_click", {"text": "立即支付"}, probe) is None   # paying is fine once approved
    st["submitted"] = True
    assert await sw.gate(st, "browser_click", {"text": "提交订单"}, probe)           # never a second submit


async def test_gate_click_elsewhere_and_unknown_point():
    st = {"approved": False}
    probe, _ = _probe({"elem-at": {"url": "https://item.jd.com/1.html", "text": "黑色"}})
    assert await sw.gate(st, "click", {"x": 10, "y": 10}, probe) is None
    probe, _ = _probe({"elem-at": {"error": "NO_CDP"}})
    assert await sw.gate(st, "click", {"x": 10, "y": 10}, probe)              # can't see it: refuse before approval


async def test_guard_wraps_executor():
    from tool_router import Decision, RiskLevel
    from tool_router.executor import ToolOutcome
    ran = []

    class Ex:
        scale = 1.0
        confirm = None

        async def run(self, name, args, intent=""):
            ran.append((name, args, intent))
            return ToolOutcome(name, "x", True, Decision(RiskLevel.L0, "ok"))

    ex = Ex()

    class Comp:
        async def execute(self, action, payload=None, observe=False, timeout=30):
            class R:
                result = {"stdout": json.dumps({"url": "https://item.jd.com/1.html", "text": "x"})}
            return R()
    st = {"approved": False, "submitted": False}
    sw.guard(ex, st, Comp())
    out = await ex.run("browser_click", {"text": "提交订单"}, intent="提交订单")
    assert not out.success and out.decision.forbidden and not ran
    out = await ex.run("browser_click", {"text": "立即购买"}, intent="点击立即购买下单")
    assert out.success and ran[-1][2] == ""                                  # 下单 wording raises no stray card


# ----------------------------------------------------------------------------- order tool
class FakeComputer:
    def __init__(self, order_page):
        self.order_page, self.subs = order_page, []

    async def execute(self, action, payload=None, observe=False, timeout=30):
        cmd = (payload or {}).get("command", "")
        sub = cmd.split('python3 "$f" ', 1)[1].split()[0] if 'python3 "$f" ' in cmd else action
        self.subs.append(sub)
        out = {"order": self.order_page, "submit": {"ok": True, "url": "https://pay.jd.com/x", "clicked": "提交订单"},
               "close-order": {"ok": True, "closed": ["x"]}}.get(sub, {})

        class R:
            result = {"stdout": json.dumps(out, ensure_ascii=False)}
        return R()


class FakeTask:
    id, thread_id, machine_id, source, prompt = "task-1", "th-1", "m1", "watch_order", ""


@pytest.fixture
def store(tmp_path):
    from aiw_api.store import Store
    s = Store(str(tmp_path / "s.db"))
    sw.ensure_columns(s)
    return s


def _watch(store, target="1700", qty=1):
    f = sw.watch_fields({"url": "https://item.jd.com/1.html", "target_price": target, "quantity": qty, "auto_order": True},
                        default_machine="m1")
    return store.insert("watches", f)


@pytest.mark.parametrize("answer", [False, True])
async def test_order_confirm_gate(store, answer):
    w = _watch(store)
    comp = FakeComputer({"found": True, "pay": ["应付总额：¥1699.00"], "addr": ["张三 上海市浦东新区"], "text": ""})
    asked = []

    async def confirm(tool, payload, decision):
        asked.append((tool, payload, decision))
        assert "submit" not in comp.subs                                     # nothing submitted while asking
        return answer
    st = {"watch": w["id"], "approved": False, "submitted": False}
    _, call = sw.order_tool(store, None, FakeTask(), comp, confirm, st)
    res = await call({"item": "AirPods", "payable": 1699})
    assert asked and asked[0][1]["实付金额"] == "¥1699" and asked[0][1]["confirm_timeout_s"] == 1800
    assert asked[0][2].level == 3
    if answer:
        assert res.startswith("SUBMITTED") and "submit" in comp.subs and st["submitted"]
        assert store.get("watches", w["id"])["order_status"] == "submitted"
        assert store.get("watches", w["id"])["enabled"] == 0
    else:
        assert res.startswith("REJECTED") and "submit" not in comp.subs and "close-order" in comp.subs
        assert store.get("watches", w["id"])["order_status"] == "rejected"


async def test_order_above_target_never_asks(store):
    w = _watch(store, target="1600")
    comp = FakeComputer({"found": True, "pay": ["实付款 ¥1699.00"], "text": ""})

    async def confirm(*a):
        raise AssertionError("must not ask")
    _, call = sw.order_tool(store, None, FakeTask(), comp, confirm, {"watch": w["id"]})
    res = await call({"item": "AirPods", "payable": 1500})                  # the page's higher number wins
    assert res.startswith("ABOVE_TARGET") and "submit" not in comp.subs and "close-order" in comp.subs


async def test_order_needs_order_page(store):
    w = _watch(store)
    comp = FakeComputer({"found": False})

    async def confirm(*a):
        raise AssertionError("must not ask")
    _, call = sw.order_tool(store, None, FakeTask(), comp, confirm, {"watch": w["id"]})
    assert (await call({"item": "x", "payable": 1})).startswith("FAILED")


# ----------------------------------------------------------------------------- end to end through the API + agent loop
@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    monkeypatch.setenv("AIW_SECRET_KEY", "test-key")
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.ask_timeout_s = 5
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORT}", desktop_http="http://127.0.0.1:1")]
    scripts = []

    def factory():
        return LLMRouter([ScriptedProvider(scripts.pop(0))])
    app = build_app(s, llm_factory=factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, scripts, app
    await client.aclose()
    stop.set(); await t


async def _wait(client, tid, states, timeout=15):
    for _ in range(int(timeout / 0.1)):
        t = (await client.get(f"/api/tasks/{tid}")).json()
        if t["status"] in states:
            return t
        await asyncio.sleep(0.1)
    raise AssertionError(f"task stuck: {t}")


@pytest.mark.parametrize("approve", [True, False])
async def test_watch_to_order_end_to_end(stack, monkeypatch, approve):
    client, scripts, app = stack
    subs = []

    async def fake_probe(computer, sub, timeout=60, **kw):
        subs.append(sub)
        if sub == "probe":
            return JD
        if sub == "order":
            return {"found": True, "pay": ["应付总额：¥1699.00"], "addr": ["上海市浦东新区"], "text": ""}
        if sub == "submit":
            return {"ok": True, "url": "https://pay.jd.com/x"}
        if sub in ("url", "elem-at"):
            return {"url": "https://trade.jd.com/shopping/order/getOrderInfo.action", "text": "提交订单"}
        return {"ok": True}
    monkeypatch.setattr(sw, "run_probe", fake_probe)

    bad = (await client.post("/api/watches", json={"url": "https://example.com/1", "mode": "shop", "value": "1"})).status_code
    assert bad == 400
    w = (await client.post("/api/watches", json={"url": "https://item.jd.com/100012043978.html", "mode": "shop",
                                                  "value": "1700", "auto_order": True, "interval_min": 30})).json()
    assert w["mode"] == "shop" and w["auto_order"] == 1 and w["interval_min"] == 30
    scripts.append([("browser_click", {"text": "提交订单"}),        # the model jumps the gun: refused
                    ("click", {"x": 900, "y": 700}),               # by coordinates: refused too
                    ("order_confirm", {"item": "AirPods Pro 2", "spec": "默认", "quantity": 1, "payable": 1699}),
                    ("done", {"summary": "finished"})])
    res = (await client.post(f"/api/watches/{w['id']}/check")).json()
    assert res["triggered"] is True
    w2 = next(x for x in (await client.get("/api/watches")).json() if x["id"] == w["id"])
    assert w2["order_status"] == "running" and w2["final_price"] == 1699.0 and w2["below"] == 1
    tid = w2["order_task_id"]
    t = await _wait(client, tid, ("waiting_confirm",))
    assert "submit" not in subs
    sess = (await client.get(f"/api/sessions/{t['session_id']}")).json()
    assert sess["pending_confirm"]["tool"] == "order_confirm" and "1699" in sess["pending_confirm"]["reason"]
    await client.post("/api/agent/confirm", json={"session_id": t["session_id"], "approve": approve})
    t = await _wait(client, tid, ("succeeded", "failed"))
    assert ("submit" in subs) is approve
    evs = (await client.get(f"/api/sessions/{t['session_id']}")).json()["events"]
    refused = [e for e in evs if e["type"] == "agent.action_result" and not e["data"].get("success")
               and "order_confirm" in str(e["data"].get("error"))]
    assert len(refused) == 2                                                # the early 提交订单 click and the coordinate click
    w3 = next(x for x in (await client.get("/api/watches")).json() if x["id"] == w["id"])
    assert w3["order_status"] == ("submitted" if approve else "rejected")
    # a second check while still low starts no second order
    res2 = (await client.post(f"/api/watches/{w['id']}/check")).json()
    assert res2["triggered"] is False
