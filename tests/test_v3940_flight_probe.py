"""v3.9.40 机票比价 probe (flight_probe.py): the parsers on fixture JSON / page text, wall detection, the search URLs,
and one whole `search` run against a fake Chrome DevTools endpoint (real websocket, real subprocess)."""
import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from aiw_api import flight_probe as fp

F = Path(__file__).parent / "fixtures" / "v3940"
PROBE = Path(fp.__file__)


def body(name):
    return {"url": "https://example/" + name, "body": (F / name).read_text()}


# ------------------------------------------------------------------------------------------------ parsers
def test_ctrip_json_itineraries_prices_and_transfer():
    r = fp.parse_job("ctrip", {"bodies": [body("ctrip_list.json")], "text": ""})
    assert r["status"] == "ok" and r["via"] == "json"
    rows = {x["flight_no"]: x for x in r["rows"]}
    assert rows["MU5101"]["price"] == 560 and rows["MU5101"]["dep_time"] == "07:00" and rows["MU5101"]["arr_time"] == "09:15"
    assert rows["MU5101"]["dep_airport"] == "虹桥国际机场T2" and rows["MU5101"]["tax"] == 50
    t = rows["HO1101/HO1251"]   # a connection is one row: first departure, last arrival, one price
    assert t["transfer"] and t["dep_time"] == "06:10" and t["arr_time"] == "10:40" and t["price"] == 488
    assert [x["price"] for x in r["rows"]] == sorted(x["price"] for x in r["rows"])
    assert rows["MU5105"]["cabin"] == "公务舱"
    assert r["tax_note"].startswith("不含机建燃油")


def test_qunar_jsonp_epoch_times_and_string_price():
    r = fp.parse_job("qunar", {"bodies": [body("qunar_list.jsonp")], "text": ""})
    rows = {x["flight_no"]: x for x in r["rows"]}
    assert rows["MU5101"]["dep_time"] == "07:00" and rows["MU5101"]["price"] == 548   # ms epoch -> China time
    assert rows["CZ3908"]["price"] == 590 and rows["CZ3908"]["arr_time"] == "14:25"


def test_qunar_page_text_is_not_trusted():
    """Qunar's PC page scrambles its digits with CSS offsets: no rows from text, only from its JSON."""
    r = fp.parse_job("qunar", {"bodies": [], "text": (F / "ctrip_page.txt").read_text()})
    assert r["status"] != "ok" and not r["rows"]


def test_fliggy_mtop_tax_included_and_login_answer():
    r = fp.parse_job("fliggy", {"bodies": [body("fliggy_mtop.txt")], "text": ""})
    assert r["status"] == "ok" and r["tax_note"] == "含税"
    assert r["rows"][0]["flight_no"] == "9C8919" and r["rows"][0]["airline"] == "春秋"
    w = fp.parse_job("fliggy", {"bodies": [body("fliggy_login_body.txt")], "text": "", "final_url": "https://sjipiao.fliggy.com/x"})
    assert w["status"] == "login"


def test_tongcheng_page_state():
    r = fp.parse_job("tongcheng", {"bodies": [], "nuxt": (F / "tongcheng_nuxt.json").read_text(), "text": ""})
    assert r["status"] == "ok" and r["via"] == "page_state"
    assert [x["flight_no"] for x in r["rows"]] == ["MU5101", "CA1858"]
    assert r["rows"][1]["dep_airport"] == "虹桥T2"


def test_page_text_fallback():
    r = fp.parse_job("ctrip", {"bodies": [], "text": (F / "ctrip_page.txt").read_text()})
    assert r["status"] == "ok" and r["via"] == "text"
    rows = {x["flight_no"]: x for x in r["rows"]}
    assert rows["MU5101"]["price"] == 560 and rows["MU5101"]["dep_time"] == "07:00" and rows["MU5101"]["arr_time"] == "09:15"
    assert rows["FM9101"]["shared"] and rows["FM9101"]["next_day"] and not rows["CA1858"]["shared"]
    assert r["tax_note"] == "不含机建燃油"


@pytest.mark.parametrize("name,want", [("login_page.txt", "login"), ("captcha_page.txt", "captcha"), ("empty_page.txt", "empty")])
def test_walls(name, want):
    r = fp.parse_job("ctrip", {"bodies": [], "text": (F / name).read_text(), "final_url": "https://m.ctrip.com/html5/flight/x"})
    assert r["status"] == want and not r["rows"]


def test_login_url_timeout_error():
    assert fp.parse_job("fliggy", {"text": "x", "final_url": "https://login.taobao.com/member/login.jhtml"})["status"] == "login"
    assert fp.parse_job("ctrip", {"text": "加载中", "timed_out": True})["status"] == "timeout"
    assert fp.parse_job("ctrip", {"text": "", "error": "ConnectionError: x"})["status"] == "error"


def test_hhmm_forms():
    assert fp.hhmm("2026-11-20 07:05:00") == "07:05"
    assert fp.hhmm("7:05") == "07:05" and fp.hhmm("0705") == "07:05"
    assert fp.hhmm("/Date(1763593500000+0800)/") == fp.hhmm(1763593500000)
    assert fp.hhmm(None) == "" and fp.hhmm(True) == ""


def test_walker_ignores_non_flights_and_bad_prices():
    junk = {"code": "A1001", "list": [{"flightNo": "MU5101", "depTime": "07:00", "arrTime": "09:00", "price": 5},
                                      {"flightNo": "not a flight", "depTime": "07:00", "price": 500},
                                      {"name": "上海", "price": 300}]}
    assert fp.walk_flights(junk) == []


def test_search_links():
    q = fp.search_links("qunar", "SHA", "BJS", "2026-11-20", "上海", "北京")
    assert q["web"].startswith("https://flight.qunar.com/site/oneway_list.htm?searchDepartureAirport=%E4%B8%8A%E6%B5%B7")
    assert "fromCode=SHA&toCode=BJS" in q["web"] and "searchDepartureTime=2026-11-20" in q["web"]
    assert fp.search_links("tongcheng", "sha", "bjs", "2026-11-20")["web"] == "https://www.ly.com/flights/itinerary/oneway/SHA-BJS?date=2026-11-20"
    assert fp.search_links("ctrip", "SHA", "BJS", "2026-11-20")["web"] == "https://m.ctrip.com/html5/flight/swift/domestic/SHA/BJS/2026-11-20"
    assert "depCity=SHA&arrCity=BJS&depDate=2026-11-20" in fp.search_links("fliggy", "SHA", "BJS", "2026-11-20", "上海", "北京")["web"]
    with pytest.raises(ValueError):
        fp.search_links("kayak", "SHA", "BJS", "2026-11-20")


def test_compact_output_stays_small():
    big = {"flightItineraryList": [{"flightSegments": [{"flightList": [{"flightNo": f"MU{5000 + i}", "departureDateTime": "2026-11-20 07:00",
                                                                        "arrivalDateTime": "2026-11-20 09:00"}]}],
                                    "priceList": [{"adultPrice": 400 + i}]} for i in range(300)]}
    snaps = {f"{p}-out": {"bodies": [{"url": "u", "body": json.dumps(big)}], "text": "x" * 50000} for p in fp.PLATFORMS}
    jobs = [{"platform": p, "leg": "out", "url": "u", "key": f"{p}-out"} for p in fp.PLATFORMS]
    out = fp.compact(snaps, jobs)
    assert all(len(r["rows"]) == 40 and r["count"] == 300 for r in out["results"])
    assert len(json.dumps(out, ensure_ascii=False).encode()) < 64 * 1024   # vm-agent's shell stdout cap


# ------------------------------------------------------------------------------------------------ the CLI
def test_cli_version_and_no_cdp(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "VM_AGENT_CDP": "http://127.0.0.1:1"}
    v = subprocess.run([sys.executable, str(PROBE), "version"], capture_output=True, text=True, env=env, timeout=20)
    assert json.loads(v.stdout)["version"] == fp.PROBE_VERSION
    jobs = base64.b64encode(json.dumps([{"platform": "ctrip", "url": "https://x", "ua": "mobile"}]).encode()).decode()
    r = subprocess.run([sys.executable, str(PROBE), "search", "--jobs", jobs, "--budget", "15"], capture_output=True,
                       text=True, env={**env, "VM_AGENT_CDP": "http://127.0.0.1:9"}, timeout=40)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out.get("ok"):   # something answers on 9222 in this sandbox: nothing to assert about no_cdp
        pytest.skip("a DevTools endpoint is running here")
    assert r.returncode == 3 and out["reason"] == "no_cdp"


class FakeChrome:
    """/json/version + a browser websocket (Target.*) + page websockets that 'load' a platform page: the page fetches
    its flight JSON (Network events, getResponseBody) and evaluates STATE_JS / FINAL_JS."""

    def __init__(self, pages: dict):
        self.pages, self.port, self.loop, self.closed, self.created = pages, None, None, [], []
        self.ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        assert self.ready.wait(10)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        from websockets.asyncio.server import serve
        from websockets.http11 import Response
        from websockets.datastructures import Headers

        def process_request(conn, req):
            if req.path.startswith("/json/version"):
                b = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/b1"}).encode()
                return Response(200, "OK", Headers({"Content-Type": "application/json", "Content-Length": str(len(b))}), b)
            return None
        self.stop = asyncio.get_running_loop().create_future()
        async with serve(self._handler, "127.0.0.1", 0, process_request=process_request, max_size=None) as srv:
            self.port = srv.sockets[0].getsockname()[1]
            self.ready.set()
            await self.stop

    async def _handler(self, ws):
        path = ws.request.path
        if path.startswith("/devtools/browser"):
            async for raw in ws:
                m = json.loads(raw)
                if m["method"] == "Target.createTarget":
                    tid = f"T{len(self.created) + 1}"
                    self.created.append(tid)
                    await ws.send(json.dumps({"id": m["id"], "result": {"targetId": tid}}))
                else:
                    if m["method"] == "Target.closeTarget":
                        self.closed.append(m["params"]["targetId"])
                    await ws.send(json.dumps({"id": m["id"], "result": {}}))
            return
        url = ""
        async for raw in ws:
            m = json.loads(raw)
            meth, p = m["method"], m.get("params") or {}
            res = {}
            if meth == "Page.navigate":
                url = p["url"]
                await ws.send(json.dumps({"id": m["id"], "result": {"frameId": "f"}}))
                page = self.pages.get(url) or {}
                for i, (u, _b) in enumerate(page.get("bodies", [])):
                    await ws.send(json.dumps({"method": "Network.responseReceived", "params": {
                        "requestId": f"r{i}", "response": {"url": u, "mimeType": "application/json", "status": 200}}}))
                    await ws.send(json.dumps({"method": "Network.loadingFinished", "params": {"requestId": f"r{i}"}}))
                continue
            if meth == "Network.getResponseBody":
                page = self.pages.get(url) or {}
                i = int(p["requestId"][1:])
                res = {"body": page["bodies"][i][1], "base64Encoded": False}
            elif meth == "Runtime.evaluate":
                page = self.pages.get(url) or {}
                if "nuxt" in p["expression"] and "innerText" in p["expression"] and "slice(0, 60000)" in p["expression"]:
                    v = json.dumps({"href": page.get("final_url", url), "title": page.get("title", ""), "text": page.get("text", ""),
                                    "nuxt": page.get("nuxt", "")})
                else:
                    v = json.dumps({"href": page.get("final_url", url), "title": page.get("title", ""), "len": len(page.get("text", "")),
                                    "fn": 0, "ready": "complete"})
                res = {"result": {"type": "string", "value": v}}
            await ws.send(json.dumps({"id": m["id"], "result": res}))

    def shutdown(self):
        self.loop.call_soon_threadsafe(self.stop.set_result, None)


def test_search_end_to_end_against_fake_devtools(tmp_path):
    links = {p: fp.search_links(p, "SHA", "BJS", "2026-11-20", "上海", "北京") for p in fp.PLATFORMS}
    pages = {
        links["ctrip"]["probe"]: {"bodies": [("https://m.ctrip.com/restapi/soa2/14022/flightListSearch", (F / "ctrip_list.json").read_text())],
                                  "text": "上海-北京", "title": "携程机票"},
        links["qunar"]["probe"]: {"bodies": [("https://flight.qunar.com/touch/api/domestic/wbdflightlist?x=1", (F / "qunar_list.jsonp").read_text())],
                                  "text": "加载中"},
        links["fliggy"]["probe"]: {"final_url": "https://login.taobao.com/member/login.jhtml", "title": "登录", "text": "手机号登录"},
        links["tongcheng"]["probe"]: {"nuxt": (F / "tongcheng_nuxt.json").read_text(), "text": "同程机票", "title": "同程"},
    }
    chrome = FakeChrome(pages)
    chrome.start()
    try:
        jobs = [{"platform": p, "leg": "out", "url": links[p]["probe"], "ua": links[p]["ua"], "timeout": 12} for p in fp.PLATFORMS]
        b64 = base64.b64encode(json.dumps(jobs).encode()).decode()
        env = {**os.environ, "HOME": str(tmp_path), "VM_AGENT_CDP": f"http://127.0.0.1:{chrome.port}"}
        r = subprocess.run([sys.executable, str(PROBE), "search", "--jobs", b64, "--budget", "20"], capture_output=True,
                           text=True, env=env, timeout=90)
        out = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        chrome.shutdown()
    assert out["ok"] and out["probe_version"] == fp.PROBE_VERSION, r.stderr
    st = {x["platform"]: x for x in out["results"]}
    assert st["ctrip"]["status"] == "ok" and st["ctrip"]["rows"][0]["price"] == 488 and st["ctrip"]["bodies"] == 1
    assert st["qunar"]["status"] == "ok" and st["qunar"]["rows"][0]["flight_no"] == "MU5101"
    assert st["fliggy"]["status"] == "login"
    assert st["tongcheng"]["status"] == "ok" and st["tongcheng"]["rows"][0]["price"] == 575
    assert sorted(chrome.closed) == sorted(chrome.created) and len(chrome.created) == 4   # every window closed again
    raw = Path(out["raw_dir"]) / "ctrip-out.json"
    assert raw.exists() and str(tmp_path) in out["raw_dir"]
