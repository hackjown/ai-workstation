"""v3.9.40 出行: restaurant hand-off cards (大众点评 / 美团), flight_search merging / caching / per-platform failures,
settings, the agent + chat wiring (the card lands on the task's result), the call tool text and the clients."""
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from aiw_api import flight_probe as fp
from aiw_api import travel as tr

ROOT = Path(__file__).resolve().parents[1]
F = Path(__file__).parent / "fixtures" / "v3940"


class Store:
    def __init__(self):
        self.kv = {}

    def kv_get(self, k, d=None):
        return json.loads(json.dumps(self.kv[k])) if k in self.kv else d

    def kv_set(self, k, v):
        self.kv[k] = json.loads(json.dumps(v))


class T:
    id = "task-1"


# ------------------------------------------------------------------------------------------------ helpers
def test_resolve_city():
    assert tr.resolve_city("上海") == ("SHA", "上海")
    assert tr.resolve_city("上海虹桥") == ("SHA", "上海")
    assert tr.resolve_city("虹桥机场") == ("SHA", "上海")
    assert tr.resolve_city("PVG") == ("SHA", "上海") and tr.resolve_city("pek") == ("BJS", "北京")
    assert tr.resolve_city("北京市") == ("BJS", "北京") and tr.resolve_city("大兴") == ("BJS", "北京")
    assert tr.resolve_city("西安") == ("SIA", "西安")
    assert tr.resolve_city("火星") is None and tr.resolve_city("") is None


def test_parse_date():
    today = dt.date(2026, 11, 11)
    assert tr.parse_date("2026-11-20", today) == dt.date(2026, 11, 20)
    assert tr.parse_date("明天", today) == dt.date(2026, 11, 12)
    assert tr.parse_date("11月20日", today) == dt.date(2026, 11, 20)
    assert tr.parse_date("1-5", today) == dt.date(2027, 1, 5)   # already past this year -> next year
    assert tr.parse_date("2026-02-30", today) is None and tr.parse_date("下周", today) is None


# ------------------------------------------------------------------------------------------------ restaurants
def test_dianping_and_meituan_ids():
    assert tr.dianping_ids("https://m.dianping.com/shop/1859284") == {"id": "1859284"}
    assert tr.dianping_ids("https://www.dianping.com/shop/G3IAnb1UxyTKS8TB") == {"uuid": "G3IAnb1UxyTKS8TB"}
    assert tr.dianping_ids("http://m.dianping.com/tuan/deal/856775395?utm_source=appshare&shopId=1695270385&shopUuid=G3IAnb1UxyTKS8TB") == {"id": "1695270385"}
    assert tr.dianping_ids("1859284") == {"id": "1859284"} and tr.dianping_ids("") == {}
    assert tr.meituan_poi("https://www.meituan.com/meishi/629280655/") == "629280655"
    assert tr.meituan_poi("imeituan://www.meituan.com/food/poi/detail?id=629280655") == "629280655"
    assert tr.meituan_poi("随便") == ""


def test_restaurant_card_with_shop_links():
    c = tr.restaurant_card({"name": "老吉士酒家", "city": "上海市", "address": "天平路41号", "rating": 4.6, "avg_price": 180,
                            "date": "2026-11-20", "time": "18:30", "party_size": 4,
                            "dianping_url": "https://m.dianping.com/shop/1859284", "meituan_url": "629280655"},
                           today=dt.date(2026, 11, 11))
    assert c["kind"] == "restaurant" and c["title"] == "老吉士酒家" and c["subtitle"] == "天平路41号"
    assert c["facts"][:2] == ["★ 4.6", "人均 ¥180"] and c["suggest"] == "11月20日 周五 · 18:30 · 4 人"
    dp, mt = c["buttons"]
    assert dp["label"] == "打开大众点评预订" and dp["app"] == "dianping://shopinfo?id=1859284" and dp["prefer"] == "app"
    assert dp["web"] == "https://m.dianping.com/shop/1859284" and dp["target"] == "shop"
    assert mt["app"] == "imeituan://www.meituan.com/food/poi/detail?id=629280655" and mt["web"].startswith("https://")
    assert "不会替你下单" in c["note"]


def test_restaurant_card_without_ids_searches_in_the_app():
    c = tr.restaurant_card({"name": "南翔馒头店", "city": "上海", "time": "晚上6点", "party_size": "2"})
    dp, mt = c["buttons"]
    assert dp["app"].startswith("dianping://searchshoplist?keyword=") and "%E5%8D%97" in dp["app"] and dp["target"] == "search"
    assert dp["web"].startswith("https://www.dianping.com/search/keyword/1/0_")
    assert mt["app"].startswith("imeituan://www.meituan.com/search?q=")
    assert c["suggest"] == "18:00 · 2 人" and "App 会打开搜索" in c["note"]
    with pytest.raises(ValueError):
        tr.restaurant_card({"name": " "})


def test_restaurant_card_respects_app_setting():
    c = tr.restaurant_card({"name": "A"}, apps=["meituan"])
    assert [b["app_name"] for b in c["buttons"]] == ["美团"]


async def test_restaurant_tool_stores_the_card():
    st = Store()
    schema, fn = tr.restaurant_tool(st, T())
    assert schema["name"] == "restaurant_booking" and "不付款" in schema["description"]
    out = await fn({"name": "新荣记", "city": "北京", "dianping_url": "https://m.dianping.com/shop/1"})   # bad id: too short
    assert "已生成预订卡片：新荣记" in out and "不要说已经订好了" in out
    cards = tr.cards_for(st, "task-1")
    assert len(cards) == 1 and cards[0]["buttons"][0]["target"] == "search"
    await fn({"name": "新荣记", "city": "北京"})   # the same card again replaces it
    assert len(tr.cards_for(st, "task-1")) == 1
    assert "缺少餐厅名称" in await fn({})
    assert not fn.needs_confirm({})


# ------------------------------------------------------------------------------------------------ flights
def probe_results(statuses=None):
    """What flight_probe.compact would print for SHA->BJS, built from the fixtures."""
    statuses = statuses or {}
    snaps = {"ctrip": {"bodies": [{"body": (F / "ctrip_list.json").read_text()}]},
             "qunar": {"bodies": [{"body": (F / "qunar_list.jsonp").read_text()}]},
             "fliggy": {"bodies": [{"body": (F / "fliggy_login_body.txt").read_text()}], "final_url": "https://sjipiao.fliggy.com/"},
             "tongcheng": {"nuxt": (F / "tongcheng_nuxt.json").read_text()}}
    out = []
    for p, s in snaps.items():
        r = fp.parse_job(p, s)
        if p in statuses:
            r = {**r, "status": statuses[p], "rows": []}
        out.append({"key": f"{p}-out", "platform": p, "leg": "out", "url": "u", **r})
    return out


def future(days=9):
    return (dt.datetime.now(tr.TZ).date() + dt.timedelta(days=days)).isoformat()


async def test_flight_search_merges_and_sorts():
    st, calls = Store(), []

    async def runner(jobs, budget):
        calls.append(jobs)
        return {"ok": True, "results": probe_results()}
    schema, fn = tr.flight_tool(st, T(), None, runner=runner)
    assert schema["name"] == "flight_search" and "不下单不付款" in schema["description"]
    tr._cache.clear()
    out = await fn({"origin": "上海", "destination": "北京", "date": future()})
    jobs = calls[0]
    assert {j["platform"] for j in jobs} == set(fp.PLATFORMS) and all(j["leg"] == "out" for j in jobs)
    assert jobs[0]["url"].startswith("https://m.ctrip.com/html5/flight/swift/domestic/SHA/BJS/")
    card = tr.cards_for(st, "task-1")[0]
    rows = card["legs"][0]["rows"]
    assert [r["price"] for r in rows] == sorted(r["price"] for r in rows)
    assert rows[0]["price"] == 488 and rows[0]["platform"] == "ctrip" and rows[0]["transfer"]
    assert not any(r["cabin"] == "公务舱" for r in rows)   # economy by default: the 公务舱 fare is filtered out
    plats = {p["platform"]: p for p in card["legs"][0]["platforms"]}
    assert plats["fliggy"]["status"] == "login" and plats["fliggy"]["status_cn"] == "需登录"
    assert plats["qunar"]["min"] == 548 and plats["tongcheng"]["status"] == "ok"
    assert plats["ctrip"]["link"]["web"].startswith("https://m.ctrip.com/") and plats["ctrip"]["link"]["prefer"] == "web"
    assert rows[0]["link"]["web"] == plats["ctrip"]["link"]["web"]
    assert "最低 ¥488 · 携程" in card["subtitle"] and "| 平台 | 航班 |" in out and "飞猪：需登录" in out
    # nothing in the text that the agent loop would take for a login/slider wall (escalate.STRONG)
    from agent_runtime import escalate
    assert escalate.human_gate(out) == ("", "")
    # cached: the second identical search does not run the probe again
    out2 = await fn({"origin": "SHA", "destination": "PEK", "date": future()})
    assert len(calls) == 1 and "缓存" in out2 and tr.cards_for(st, "task-1")[0]["cached"]
    await fn({"origin": "SHA", "destination": "PEK", "date": future(), "refresh": True})
    assert len(calls) == 2


async def test_flight_search_round_trip_cabin_and_direct():
    st, calls = Store(), []

    async def runner(jobs, budget):
        calls.append(jobs)
        res = probe_results()
        return {"ok": True, "results": res + [{**r, "key": r["key"].replace("-out", "-ret"), "leg": "ret"} for r in res]}
    _, fn = tr.flight_tool(st, T(), None, runner=runner)
    tr._cache.clear()
    await fn({"origin": "上海", "destination": "北京", "date": future(), "return_date": future(12), "cabin": "公务舱",
              "direct_only": True, "adults": 2})
    assert {j["leg"] for j in calls[0]} == {"out", "ret"}
    ret = [j for j in calls[0] if j["leg"] == "ret"][0]
    assert "/BJS/SHA/" in ret["url"] or "BJS" in ret["url"]
    card = tr.cards_for(st, "task-1")[0]
    assert len(card["legs"]) == 2 and card["legs"][1]["label"].startswith("返程") and card["adults"] == 2
    rows = card["legs"][0]["rows"]
    assert rows and all(not r["transfer"] for r in rows)
    assert [r["flight_no"] for r in rows if r["platform"] == "ctrip"] == ["MU5105"]   # the 公务舱 fare only


async def test_flight_search_respects_settings_and_reports_failures():
    st = Store()
    tr.save_settings(st, {"flight_platforms": {"fliggy": False}, "cache_minutes": 0})
    seen = []

    async def runner(jobs, budget):
        seen.append(jobs)
        return {"ok": True, "results": probe_results({"qunar": "captcha", "tongcheng": "timeout"})}
    _, fn = tr.flight_tool(st, T(), None, runner=runner)
    out = await fn({"origin": "上海", "destination": "北京", "date": future()})
    assert "fliggy" not in {j["platform"] for j in seen[0]}
    plats = {p["platform"]: p for p in tr.cards_for(st, "task-1")[0]["legs"][0]["platforms"]}
    assert plats["fliggy"]["status"] == "disabled" and plats["qunar"]["status_cn"] == "需验证"
    assert plats["tongcheng"]["status_cn"] == "超时" and plats["ctrip"]["status"] == "ok"
    assert "去哪儿：需验证" in out
    await fn({"origin": "上海", "destination": "北京", "date": future()})
    assert len(seen) == 2   # cache_minutes 0 = no cache


async def test_flight_search_errors():
    st = Store()

    async def broken(jobs, budget):
        return {"ok": False, "reason": "no_cdp", "error": "NO_CDP: 9222: refused"}
    _, fn = tr.flight_tool(st, T(), None, runner=broken)
    tr._cache.clear()
    assert "不认识「火星」" in await fn({"origin": "火星", "destination": "北京", "date": future()})
    assert "同一个城市" in await fn({"origin": "虹桥", "destination": "浦东", "date": future()})
    assert "已经过去了" in await fn({"origin": "上海", "destination": "北京", "date": "2020-01-01"})
    assert "早于出发日期" in await fn({"origin": "上海", "destination": "北京", "date": future(5), "return_date": future(2)})
    out = await fn({"origin": "上海", "destination": "北京", "date": future()})
    assert "比价没跑起来" in out and "https://flight.qunar.com/" in out and not tr.cards_for(st, "task-1")
    tr.save_settings(st, {"flight_platforms": {p: False for p in fp.PLATFORMS}})
    assert "都关掉了" in await fn({"origin": "上海", "destination": "北京", "date": future()})


async def test_probe_runner_ships_the_probe_and_retries_after_ensure(monkeypatch):
    from aiw_api import shopwatch as sw
    cmds, ensured = [], []

    class R:
        def __init__(self, out):
            self.result = {"stdout": out, "stderr": "", "exit_code": 0}
            self.error = None

    class Comp:
        machine_id = "m1"

        async def execute(self, action, params, timeout=None):
            cmds.append(params["command"])
            if len(cmds) == 1:
                return R(json.dumps({"ok": False, "reason": "no_cdp", "error": "NO_CDP"}))
            return R("noise\n" + json.dumps({"ok": True, "results": []}))

    async def ensure(computer, mid, port=9222):
        ensured.append(mid)
        return {"ok": True, "port": 9222}
    monkeypatch.setattr(sw, "ensure_cdp", ensure)
    monkeypatch.setattr(sw, "cdp_env", lambda c, p=None: "")
    got = await tr.probe_runner(Comp(), [{"platform": "ctrip", "url": "u"}], 30)
    assert got == {"ok": True, "results": []} and ensured == ["m1"] and len(cmds) == 2
    assert ".config/aiw/travel/flight_probe.py" in cmds[0] and " search --jobs " in cmds[0] and "--budget '30'" in cmds[0]


def test_settings_roundtrip():
    st = Store()
    s = tr.settings(st)
    assert all(s["flight_platforms"].values()) and s["cache_minutes"] == 10
    s = tr.save_settings(st, {"flight_platforms": {"qunar": False, "bogus": True}, "cache_minutes": "999", "timeout_s": 5,
                              "restaurant_apps": ["meituan", "x"]})
    assert s["flight_platforms"] == {"ctrip": True, "fliggy": True, "qunar": False, "tongcheng": True}
    assert s["cache_minutes"] == 240 and s["timeout_s"] == 20 and s["restaurant_apps"] == ["meituan"]
    assert tr.settings(st)["flight_platforms"]["qunar"] is False


def test_intent_and_prompt():
    for t in ("帮我查一下下周五上海到北京最便宜的机票", "比一比携程和飞猪哪个机票便宜", "订一张明天去成都的机票", "查航班价格"):
        assert tr.wants_flights(t), t
    for t in ("帮我在静安寺附近订个餐厅，周六晚上4个人", "大众点评上帮我订座", "推荐个请客吃饭的地方并订位"):
        assert tr.wants_restaurant(t), t
    for t in ("帮我点个外卖", "把这个文档翻译成英文", "今天天气怎么样"):
        assert not tr.wants_travel(t), t
    assert "flight_search" in tr.prompt_extra("查机票最低价") and "restaurant_booking" not in tr.prompt_extra("查机票最低价")
    assert "restaurant_booking" in tr.prompt_extra("订个餐厅") and tr.prompt_extra("写个脚本") == ""
    assert set(tr.task_tools(Store(), T(), None)) == {"restaurant_booking", "flight_search"}


# ------------------------------------------------------------------------------------------------ wiring
def test_call_tool_mentions_flights_and_restaurants():
    from aiw_api import omni_call as oc
    d = oc.TOOL_SPECS[0]["function"]["description"]
    assert "机票" in d and "餐厅" in d and "这通电话里" in d and "以前的请求不算" in d
    assert {t["function"]["name"] for t in oc.TOOL_SPECS} == {"create_aiw_task", "task_status"}   # no new call tools


def test_chat_routes_travel_to_a_task():
    from aiw_api import chat
    assert chat.wants_travel("帮我查明天上海到北京最便宜的机票")
    assert not chat.wants_travel("你好")


def test_clients_carry_the_cards():
    js = (ROOT / "apps/web/travel.js").read_text()
    assert "AiwAndroid.openApp" in js and "function hTravelCards" in js and "travelSettingsHTML" in js
    assert "hTravelCards(r.cards)" in (ROOT / "apps/web/home.js").read_text()
    idx = (ROOT / "apps/web/index.html").read_text()
    assert idx.index('src="travel.js"') < idx.index('src="home.js"') and "travel.css" in idx
    assert '["g-travel", "出行"]' in (ROOT / "apps/web/ui.js").read_text()
    plist = (ROOT / "apps/ios/Info.plist").read_text()
    q = re.search(r"<key>LSApplicationQueriesSchemes</key><array>(.*?)</array>", plist).group(1)
    for s in ("dianping", "imeituan", "ctrip", "shortcuts"):
        assert f"<string>{s}</string>" in q
    sw = (ROOT / "apps/ios/Sources/TravelCards.swift").read_text()
    assert ".universalLinksOnly" in sw and "struct TravelCardsView" in sw
    assert "TravelCardsView(cards: result.a(\"cards\"))" in (ROOT / "apps/ios/Sources/TaskCardV2.swift").read_text()
    kt = (ROOT / "apps/android/app/src/main/java/com/hackjown/aiw/MainActivity.kt").read_text()
    assert "fun openApp(app: String, web: String)" in kt and "@JavascriptInterface fun openApp" in kt
    assert 'android:scheme="dianping"' in (ROOT / "apps/android/app/src/main/AndroidManifest.xml").read_text()


# ------------------------------------------------------------------------------------------------ the whole stack
from test_chat import FakeChat, stack, wait_for  # noqa: E402,F401  (fixture)


async def _card(client, mid):
    h = (await client.get("/api/chat")).json()["messages"]
    return next(m for m in h if m["kind"] == "task" and m["id"] == mid)


async def test_restaurant_card_lands_on_the_task_result(stack):
    client, app, scripts, fake = stack
    fake.route = "chat"   # the travel rule sends it to the computer anyway
    scripts.append([("restaurant_booking", {"name": "老吉士酒家", "city": "上海", "address": "天平路41号", "rating": 4.6,
                                            "time": "18:30", "party_size": 4,
                                            "dianping_url": "https://m.dianping.com/shop/1859284"}),
                    ("done", {"summary": "推荐老吉士酒家，点卡片在大众点评里确认 18:30 4 位。"})])
    r = (await client.post("/api/chat/send", json={"text": "帮我在徐汇订个本帮菜餐厅，今晚4个人", "stream": False})).json()
    assert any(e["event"] == "route" and e["data"]["route"] == "task" for e in r["events"])
    card = r["messages"][-1]
    assert card["kind"] == "task"

    async def done():
        c = await _card(client, card["id"])
        return c if (c["data"].get("result") or {}).get("status") == "succeeded" else None
    c = await wait_for(done, 20)
    cards = c["data"]["result"]["cards"]
    assert cards[0]["kind"] == "restaurant" and cards[0]["buttons"][0]["app"] == "dianping://shopinfo?id=1859284"
    got = (await client.get(f"/api/travel/cards/{c['data']['task_id']}")).json()["cards"]
    assert got[0]["title"] == "老吉士酒家"


async def test_flight_search_runs_in_a_task(stack, monkeypatch):
    client, app, scripts, fake = stack

    async def runner(computer, jobs, budget, parallel=4):
        assert computer is not None
        return {"ok": True, "results": probe_results()}
    monkeypatch.setattr(tr, "probe_runner", runner)
    tr._cache.clear()
    scripts.append([("flight_search", {"origin": "上海", "destination": "北京", "date": future()}),
                    ("done", {"summary": "最低 ¥488（携程，中转）。"})])
    r = (await client.post("/api/chat/send", json={"text": "查一下下周上海飞北京最便宜的机票", "stream": False})).json()
    card = r["messages"][-1]

    async def done():
        c = await _card(client, card["id"])
        return c if (c["data"].get("result") or {}).get("status") == "succeeded" else None
    c = await wait_for(done, 20)
    fl = c["data"]["result"]["cards"][0]
    assert fl["kind"] == "flights" and fl["legs"][0]["rows"][0]["price"] == 488
    s = (await client.get("/api/travel/settings")).json()
    assert s["platform_names"]["qunar"] == "去哪儿"
    s = (await client.put("/api/travel/settings", json={"flight_platforms": {"tongcheng": False}})).json()
    assert s["flight_platforms"]["tongcheng"] is False
