#!/usr/bin/env python3
"""v3.9.40 机票比价 probe. Runs ON the AIW computer (stdlib only), driving the computer's Chrome over the DevTools
protocol, and is ALSO imported by the API (travel.py) for the URL builders and the parsers, which are unit-tested
there with fixture HTML/JSON.

The API ships this file to ~/.config/aiw/travel/flight_probe.py (shopwatch.ship_command) and runs:

  flight_probe.py search --jobs <base64 JSON> [--budget 70]   every job (platform x leg) in its own window, concurrently
  flight_probe.py version

A job is {"platform": "ctrip|fliggy|qunar|tongcheng", "leg": "out|ret", "url": ..., "ua": "mobile|desktop"}.
Each job: a new window on about:blank, Network + Page enabled, user agent set, navigator.webdriver hidden, then the
platform's public search page. While it renders, the JSON the page fetches for itself (its own, already signed
requests: nothing is re-signed or replayed here) is captured by URL pattern; the page's text and, for 同程, its
window.__NUXT__ state are read at the end. parse_job() turns that into flight rows on the computer, because
vm-agent's shell returns at most 64 KB of stdout: only compact rows go back, the raw capture of the last run of each
platform is kept in ~/.cache/aiw/flight/<platform>-<leg>.json for diagnosis.

A page that is a login page or a slider/captcha page, or that shows no flight, is reported per platform
(status login / captcha / empty / timeout / error) and never fails the other platforms.

UNVERIFIED against the live sites (written from public descriptions, no access from where this was built): every
search URL below except Qunar's oneway_list.htm and 同程's /flights/itinerary/oneway/ format (both seen live in
search-engine indexes), every captured-JSON pattern and every field name the walker looks for. The walker is generic
(any dict with a flight number + times + a price), so a renamed field degrades to the page-text parser, not to a crash.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from urllib.parse import urlparse

PROBE_VERSION = "1"
PLATFORMS = ("ctrip", "fliggy", "qunar", "tongcheng")
NAMES = {"ctrip": "携程", "fliggy": "飞猪", "qunar": "去哪儿", "tongcheng": "同程", "airline": "航司官网"}

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.5 Mobile/15E148 Safari/604.1")
DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

# ----------------------------------------------------------------------------------------------- URLs (pure)
def _q(s: str) -> str:
    return urllib.parse.quote(str(s or ""), safe="")


def search_links(platform: str, dep: str, arr: str, date: str, dep_name: str = "", arr_name: str = "") -> dict:
    """{"probe": page the probe opens, "ua": mobile|desktop, "web": page the user's phone opens, "app": app scheme
    (opens the app's home only; '' when unknown), "verified": what of this is known to exist}.
    dep/arr are IATA city codes (SHA, BJS); dates YYYY-MM-DD."""
    d, a = dep.upper(), arr.upper()
    if platform == "ctrip":
        # m.ctrip.com/html5/flight/swift/domestic/BJS/SHA/2021-03-24 is the H5 list page format quoted by its own
        # developers (proginn.com) and matched by a public userscript; unverified live from here.
        web = f"https://m.ctrip.com/html5/flight/swift/domestic/{d}/{a}/{date}"
        return {"probe": web, "ua": "mobile", "web": web, "app": "ctrip://", "verified": False}
    if platform == "qunar":
        # format seen live in a search-engine index (flight.qunar.com/site/oneway_list.htm?...fromCode=SHA&toCode=SYX)
        web = ("https://flight.qunar.com/site/oneway_list.htm?searchDepartureAirport=" + _q(dep_name or d) +
               "&searchArrivalAirport=" + _q(arr_name or a) + f"&searchDepartureTime={date}&nextNDays=0&startSearch=true"
               f"&fromCode={d}&toCode={a}&from=flight_dom_search&lowestPrice=null")
        return {"probe": web, "ua": "desktop", "web": web, "app": "qunariphone://", "verified": True}
    if platform == "fliggy":
        # PC search result page described by a public Fliggy price-watch skill; Fliggy usually wants a Taobao login
        web = (f"https://sjipiao.fliggy.com/flight_search_result.htm?tripType=0&depCity={d}&arrCity={a}&depDate={date}"
               "&depCityName=" + _q(dep_name) + "&arrCityName=" + _q(arr_name))
        return {"probe": web, "ua": "desktop", "web": web, "app": "fliggy://", "verified": False}
    if platform == "tongcheng":
        # www.ly.com/flights/itinerary/oneway/SHA-PEK?date=2024-12-01 (several public scripts; a live index hit too)
        web = f"https://www.ly.com/flights/itinerary/oneway/{d}-{a}?date={date}"
        return {"probe": web, "ua": "desktop", "web": web, "app": "tctravel://", "verified": True}
    raise ValueError(f"unknown platform {platform}")


CAPTURE = {   # JSON responses worth reading, per platform (any 'flight'-looking JSON is taken as well)
    "ctrip": re.compile(r"/restapi/soa2/\d+/.*(flight|search|list)|flightlist|FlightListSearch", re.I),
    "qunar": re.compile(r"wbdflightlist|flightlist|/touch/api/|/api/(domestic|flight)|oneway_list\.json", re.I),
    "fliggy": re.compile(r"mtop\.trip\.flight|mtop\.alitrip|flight_search|searchflight|flightlist|/h5/mtop\.trip", re.I),
    "tongcheng": re.compile(r"/flights/api/|getflightlist|flightlist|flight.*query", re.I),
}
ANY_FLIGHT_JSON = re.compile(r"flight|jipiao|airline", re.I)

# ----------------------------------------------------------------------------------------------- parsing (pure)
AIRLINES = {
    "CA": "国航", "MU": "东航", "CZ": "南航", "HU": "海航", "ZH": "深航", "3U": "川航", "MF": "厦航", "FM": "上航",
    "SC": "山东航", "9C": "春秋", "HO": "吉祥", "KN": "联航", "JD": "首都航", "GS": "天津航", "8L": "祥鹏", "PN": "西部",
    "EU": "成都航", "TV": "西藏航", "G5": "华夏", "BK": "奥凯", "NS": "河北航", "DZ": "东海航", "GJ": "长龙", "QW": "青岛航",
    "Y8": "金鹏", "DR": "瑞丽航", "A6": "红土航", "GX": "北部湾", "UQ": "乌航", "GT": "桂林航", "RY": "江西航",
    "AQ": "九元", "9H": "长安航", "LT": "龙江航", "JR": "幸福航", "FU": "福州航", "OQ": "重庆航", "KY": "昆明航",
    "ZJ": "南非航", "CN": "大新华", "HX": "香港航", "CX": "国泰", "UO": "香港快运", "NX": "澳门航", "BR": "长荣",
    "CI": "华航", "GY": "多彩", "QD": "江西快运", "TB": "途易", "YI": "英安", "WN": "西南",
}
FLIGHT_RE = re.compile(r"^(?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\d{2,4}[A-Z]?$")
TEXT_FLIGHT_RE = re.compile(r"(?<![A-Za-z0-9])((?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\d{3,4})(?![0-9])")
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)")
PRICE_TEXT_RE = re.compile(r"[¥￥]\s*(\d{2,6}(?:\.\d{1,2})?)|(\d{2,6})\s*元")

FNO_KEYS = ("flightNo", "flightNumber", "flightno", "flight_no", "fn", "flightNum", "marketFlightNo", "fltNo",
            "airlineFlightNo", "flightCode", "operateFlightNo", "mktFlightNo", "carrierFlightNo", "flightNumberStr")
DEP_KEYS = ("depTime", "departureTime", "dptTime", "dTime", "takeOffTime", "depDateTime", "departTime", "departureDateTime",
            "depDate", "startTime", "dep_time", "departure", "flightDepTime", "dptDateTime", "departureDate")
ARR_KEYS = ("arrTime", "arrivalTime", "aTime", "landTime", "arrDateTime", "arriveTime", "arrivalDateTime", "endTime",
            "arr_time", "arrival", "flightArrTime", "arrDate", "arrivalDate")
AIRLINE_KEYS = ("airlineName", "airlineShortName", "airline", "carrierName", "airlineCompany", "alName", "airCompanyName",
                "airlineChnName", "airlineCnName", "marketAirlineName", "companyName", "carrier")
DEP_AP_KEYS = ("depAirportName", "departureAirportName", "dptAirport", "dAirportName", "depAirport", "departureAirport",
               "dptAirportName", "fromAirportName", "orgAirportName", "depPortName")
ARR_AP_KEYS = ("arrAirportName", "arrivalAirportName", "arrAirport", "aAirportName", "arrivalAirport", "toAirportName",
               "dstAirportName", "arrPortName")
DEP_T_KEYS = ("depTerminal", "departureTerminal", "dptTerminal", "dTerminal", "depTerm")
ARR_T_KEYS = ("arrTerminal", "arrivalTerminal", "aTerminal", "arrTerm")
PRICE_KEYS = ("price", "lowestPrice", "minPrice", "lowPrice", "salePrice", "adultPrice", "ticketPrice", "barePrice",
              "adultSalePrice", "priceValue", "showPrice", "displayPrice", "adultTicketPrice", "cheapestPrice",
              "minSalePrice", "realPrice", "bestPrice", "lowestFare", "fare", "totalPrice", "adultTotalPrice")
TAX_KEYS = ("tax", "taxes", "airportTax", "fuelTax", "taxAmount", "adultTax", "constructionFee", "oilFee", "airportFee",
            "fuelFee", "airportConstructionFee")
CABIN_KEYS = ("cabinName", "cabinClassName", "cabinClass", "className", "seatClass", "cabin", "cabinDesc", "classGrade")
TAX_INCL_KEYS = ("taxIncluded", "isTaxIncluded", "includeTax", "priceIncludeTax")
PRICE_MIN, PRICE_MAX = 30, 60000


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"\d+(?:\.\d+)?", v.replace(",", ""))
        if m and re.fullmatch(r"\s*[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(元|起)?\s*", v):
            return float(m.group(0))
    return None


def hhmm(v) -> str:
    """'2026-11-20 07:05:00' / '07:05' / '0705' / 1763593500000 (ms) / '/Date(1763593500000+0800)/' -> '07:05'."""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"\d{10,13}", v.strip())):
        t = float(v)
        if t > 1e12:
            t /= 1000.0
        if t < 1e9:
            return ""
        return time.strftime("%H:%M", time.gmtime(t + 8 * 3600))   # China time (UTC+8, no DST)
    s = str(v)
    m = re.search(r"/Date\((\d{10,13})", s)
    if m:
        return hhmm(int(m.group(1)))
    m = TIME_RE.search(s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.fullmatch(r"\s*([01]\d|2[0-3])([0-5]\d)\s*", s)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return ""


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _flight_no(d: dict) -> str:
    v = _first(d, FNO_KEYS)
    if isinstance(v, str):
        s = v.strip().upper().replace(" ", "")
        if FLIGHT_RE.match(s):
            return s
    return ""


def _airline(d: dict, fno: str) -> str:
    v = _first(d, AIRLINE_KEYS)
    if isinstance(v, dict):
        v = _first(v, ("name", "shortName", "cnName", "chnName"))
    s = str(v or "").strip()
    if s and not FLIGHT_RE.match(s.upper()) and len(s) <= 20:
        return s if not re.fullmatch(r"[A-Z0-9]{2}", s) else AIRLINES.get(s, s)
    return AIRLINES.get(fno[:2], "")


def _text_of(v) -> str:
    if isinstance(v, dict):
        v = _first(v, ("name", "shortName", "cnName", "airportName", "value"))
    return str(v or "").strip()[:20]


def _prices(o, depth: int = 0, seen=None) -> list:
    """Every plausible price in o (a dict, at most 3 levels down, not into sub-trees holding another flight)."""
    out = []
    if depth > 3:
        return out
    if isinstance(o, dict):
        for k, v in o.items():
            kl = k.lower()
            if k in PRICE_KEYS or (kl.endswith("price") and not re.search(r"origin|org|original|market|ref|tax|fee|before|full|list|y_?price|standard|std", kl)):
                n = _num(v)
                if n is not None and PRICE_MIN <= n <= PRICE_MAX:
                    out.append(n)
                elif isinstance(v, dict):
                    out += _prices(v, depth + 1)
            elif isinstance(v, (dict, list)) and not (isinstance(v, dict) and _flight_no(v)):
                out += _prices(v, depth + 1)
    elif isinstance(o, list):
        for x in o[:60]:
            if isinstance(x, dict) and _flight_no(x):
                continue
            out += _prices(x, depth + 1)
    return out


def _tax(o, depth: int = 0):
    if depth > 3:
        return None
    if isinstance(o, dict):
        for k in TAX_KEYS:
            n = _num(o.get(k)) if k in o else None
            if n is not None and 0 <= n <= 3000:
                return n
        for v in o.values():
            if isinstance(v, (dict, list)):
                t = _tax(v, depth + 1)
                if t is not None:
                    return t
    elif isinstance(o, list):
        for x in o[:20]:
            t = _tax(x, depth + 1)
            if t is not None:
                return t
    return None


def _tax_flag(o, depth: int = 0):
    if depth > 3:
        return None
    if isinstance(o, dict):
        for k in TAX_INCL_KEYS:
            if isinstance(o.get(k), bool):
                return o[k]
        for v in o.values():
            if isinstance(v, (dict, list)):
                t = _tax_flag(v, depth + 1)
                if t is not None:
                    return t
    elif isinstance(o, list):
        for x in o[:10]:
            t = _tax_flag(x, depth + 1)
            if t is not None:
                return t
    return None


def _cabin(o, depth: int = 0) -> str:
    if depth > 2:
        return ""
    if isinstance(o, dict):
        v = _first(o, CABIN_KEYS)
        if isinstance(v, str) and len(v) <= 12:
            return v
        for x in o.values():
            if isinstance(x, (dict, list)) and not (isinstance(x, dict) and _flight_no(x)):
                c = _cabin(x, depth + 1)
                if c:
                    return c
    elif isinstance(o, list):
        for x in o[:10]:
            c = _cabin(x, depth + 1)
            if c:
                return c
    return ""


def walk_flights(obj, max_nodes: int = 200000) -> list:
    """Generic: every dict with a flight number (+ departure time) is a segment; its price is its own, else the
    nearest ancestor that holds at most 2 flight numbers (an itinerary: direct or one connection). Segments under one
    such itinerary become one row (a connection: 'MU5101/MU2345', 中转)."""
    segs, fnos_memo, count = [], {}, [0]

    def fnos(o) -> frozenset:
        k = id(o)
        if k in fnos_memo:
            return fnos_memo[k]
        s = set()
        if isinstance(o, dict):
            f = _flight_no(o)
            if f:
                s.add(f)
            for v in o.values():
                if isinstance(v, (dict, list)):
                    s |= fnos(v)
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, (dict, list)):
                    s |= fnos(v)
        fs = frozenset(s)
        fnos_memo[k] = fs
        return fs

    def rec(o, anc):
        count[0] += 1
        if count[0] > max_nodes:
            return
        if isinstance(o, dict):
            if _flight_no(o) and (hhmm(_first(o, DEP_KEYS)) or _first(o, DEP_KEYS) is not None):
                segs.append((o, [(a, k) for a, k in anc]))
            entry = [o, None]
            anc.append(entry)
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    entry[1] = k
                    rec(v, anc)
            anc.pop()
        elif isinstance(o, list):
            for v in o:
                if isinstance(v, (dict, list)):
                    rec(v, anc)

    rec(obj, [])
    groups: dict = {}
    for seg, anc in segs:
        own = _prices(seg)
        key, src = id(seg), seg
        if not own:
            for a, via in reversed(anc):
                n = len(fnos(a))
                if n > 2:
                    break
                # the ancestor's OTHER branches only (its priceList next to flightSegments), never the branch that
                # leads to this segment: a sibling in the same list is another thing, not this flight's price
                rest = {k: v for k, v in a.items() if k != via}
                if _prices(rest):
                    key, src = id(a), rest
                    break
        g = groups.setdefault(key, {"segs": [], "src": src})
        if all(s is not seg for s in g["segs"]):
            g["segs"].append(seg)
    rows = []
    for g in groups.values():
        ps = _prices(g["src"])
        if not ps:
            continue
        segs_ = sorted(g["segs"], key=lambda s: hhmm(_first(s, DEP_KEYS)) or "99")
        nos = []
        for s in segs_:
            f = _flight_no(s)
            if f not in nos:
                nos.append(f)
        if len(nos) > 2:
            continue
        first, last = segs_[0], segs_[-1]
        tax = _tax(g["src"])
        flag = _tax_flag(g["src"])
        share = first.get("shareFlightNo") or first.get("operateFlightNo") or first.get("isShare") or first.get("share")
        rows.append({
            "flight_no": "/".join(nos), "airline": _airline(first, nos[0]),
            "dep_time": hhmm(_first(first, DEP_KEYS)), "arr_time": hhmm(_first(last, ARR_KEYS)),
            "dep_airport": (_text_of(_first(first, DEP_AP_KEYS)) + _text_of(_first(first, DEP_T_KEYS)))[:24],
            "arr_airport": (_text_of(_first(last, ARR_AP_KEYS)) + _text_of(_first(last, ARR_T_KEYS)))[:24],
            "price": min(ps), "tax": tax, "tax_included": flag, "cabin": _cabin(g["src"]) or _cabin(first),
            "transfer": len(nos) > 1, "shared": bool(share and share is not False and str(share) not in ("0", "false")),
            "src": "json"})
    return dedup(rows)


def dedup(rows: list) -> list:
    best = {}
    for r in rows:
        if not r.get("flight_no") or not r.get("price"):
            continue
        k = (r["flight_no"], r.get("dep_time") or "")
        if k not in best or r["price"] < best[k]["price"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["price"], r.get("dep_time") or ""))


def parse_text(text: str) -> list:
    """The page's visible text, e.g. '东方航空 MU5101 空客330(大) 07:00 虹桥T2 09:15 首都T2 ¥560起 经济舱2.8折'.
    Each flight number (of a known airline code) opens a block that runs to the next one; the block's first two
    times are departure / arrival and its first ¥ amount the price."""
    t = re.sub(r"[ \t\u00a0]+", " ", str(text or ""))
    hits = [m for m in TEXT_FLIGHT_RE.finditer(t) if m.group(1)[:2] in AIRLINES]
    rows = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(t), m.end() + 260)
        start = max(hits[i - 1].end() if i else 0, m.start() - 60)
        before, after = t[start:m.start()], t[m.end():end]
        times = [f"{int(a):02d}:{b}" for a, b in TIME_RE.findall(after)]
        if len(times) < 2:   # a layout with the times in front of the flight number
            times = [f"{int(a):02d}:{b}" for a, b in TIME_RE.findall(before)][-2:] + times
        pm = PRICE_TEXT_RE.search(after) or PRICE_TEXT_RE.search(before[-30:])
        if len(times) < 2 or not pm:
            continue
        price = float(pm.group(1) or pm.group(2))
        if not PRICE_MIN <= price <= PRICE_MAX:
            continue
        fno = m.group(1)
        cab = re.search(r"(经济舱|超级经济舱|公务舱|商务舱|头等舱)", after)
        aps = re.findall(r"([\u4e00-\u9fa5]{2,6}(?:国际)?机场|[\u4e00-\u9fa5]{2,4}T\d)", after)
        rows.append({"flight_no": fno, "airline": AIRLINES.get(fno[:2], ""), "dep_time": times[0], "arr_time": times[1],
                     "dep_airport": aps[0] if aps else "", "arr_airport": aps[1] if len(aps) > 1 else "",
                     "price": price, "tax": None, "tax_included": None, "cabin": cab.group(1) if cab else "",
                     "transfer": "中转" in after[:80], "shared": "共享" in after[:8] or "共享" in before[-12:],
                     "src": "text", "next_day": bool(re.search(r"\+1\s*天|次日", after[:80]))})
    return dedup(rows)


LOGIN_URL = re.compile(r"passport|/login|login\.|signin|sign_in|/user/login|accounts\.", re.I)
WALL_RE = re.compile(r"滑块|拖动|安全验证|人机验证|图形验证|访问异常|访问过于频繁|请求过于频繁|captcha|nocaptcha|punish|x5sec|RGV587|"
                     r"FAIL_SYS_USER_VALIDATE|_____tmd_____|baxia|安全检测", re.I)
WALL_URL = re.compile(r"verify|captcha|punish|_____tmd_____|limit\.html|antibot|slider", re.I)
LOGIN_BODY = re.compile(r"FAIL_SYS_SESSION_EXPIRED|FAIL_SYS_TOKEN_EX|FAIL_SYS_ILLEGAL_ACCESS|NOT_?LOGIN|\"needLogin\"\s*:\s*true|"
                        r"未登录|请登录|登录后", re.I)
LOGIN_TEXT = re.compile(r"(请|需要|立即|账号|密码|扫码|短信)\s*登录|登录后(查看|搜索|预订)|手机号登录|login", re.I)
EMPTY_TEXT = re.compile(r"暂无(航班|符合|结果)|没有(找到|查询到)(符合|相关)?.{0,4}航班|无直飞航班|未查询到|该航线暂无|查询无结果", re.I)


def detect_wall(url: str, title: str, text: str, bodies: str = "") -> str:
    """'' | login | captcha | empty — only used when no flight was found. bodies: the start of the captured JSON
    answers (an mtop FAIL_SYS_SESSION_EXPIRED / RGV587 says more than the page)."""
    u, ti, tx, bo = str(url or ""), str(title or ""), str(text or "")[:6000], str(bodies or "")[:20000]
    if WALL_URL.search(u) or WALL_RE.search(ti) or (WALL_RE.search(tx) and len(tx) < 2500) or \
            re.search(r"RGV587|FAIL_SYS_USER_VALIDATE|x5sec|punish", bo):
        return "captcha"
    if LOGIN_URL.search(urlparse(u).netloc + urlparse(u).path) or (LOGIN_TEXT.search(tx) and len(tx) < 2500) or \
            "登录" in ti or LOGIN_BODY.search(bo):
        return "login"
    if EMPTY_TEXT.search(tx):
        return "empty"
    return ""


def tax_note(text: str, rows: list) -> str:
    """How the platform shows the price: 含税 / 不含机建燃油 / 未注明 (from its JSON flags, then its page text)."""
    flags = [r.get("tax_included") for r in rows if r.get("tax_included") is not None]
    if flags:
        return "含税" if all(flags) else "不含机建燃油"
    if any(r.get("tax") for r in rows):
        return "不含机建燃油（另计）"
    t = str(text or "")
    if re.search(r"含税价|含税总价|已含(机建|税)|含机建燃油", t):
        return "含税"
    if re.search(r"不含税|机建\s*[+＋和]\s*燃油|另付机建|未含机建|机建燃油另|税费另", t):
        return "不含机建燃油"
    return "未注明"


def _bodies_json(bodies: list) -> list:
    out = []
    for b in bodies or []:
        s = b.get("body") if isinstance(b, dict) else b
        if not isinstance(s, str):
            if isinstance(s, (dict, list)):
                out.append(s)
            continue
        s = s.strip()
        m = re.match(r"^[\w$.]+\s*\((.*)\)\s*;?\s*$", s, re.S)   # JSONP
        if m:
            s = m.group(1)
        if s[:1] not in "{[":
            m = re.search(r"[{\[]", s)
            s = s[m.start():] if m else ""
        try:
            out.append(json.loads(s))
        except ValueError:
            # streamed answers (SSE 'data: {...}' lines, NDJSON)
            for line in s.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line[:1] in "{[":
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def parse_job(platform: str, snap: dict) -> dict:
    """A raw capture of one platform's page -> {"status", "rows", "tax_note", "note", "via"}.
    snap: {"url", "final_url", "title", "text", "nuxt", "bodies": [{"url","body"}], "error", "timed_out"}."""
    rows, via = [], ""
    for j in _bodies_json(snap.get("bodies") or []):
        rows += walk_flights(j)
    if rows:
        via = "json"
    nx = snap.get("nuxt")
    if not rows and nx:
        try:
            rows = walk_flights(json.loads(nx) if isinstance(nx, str) else nx)
            via = "page_state" if rows else via
        except ValueError:
            pass
    text = snap.get("text") or ""
    if not rows and platform != "qunar":   # qunar's PC page scrambles its digits with CSS offsets: text is not trusted
        rows = parse_text(text)
        via = "text" if rows else via
    rows = dedup(rows)
    note = tax_note(text, rows)
    if rows:
        return {"status": "ok", "rows": rows[:40], "count": len(rows), "tax_note": note, "via": via, "note": ""}
    if snap.get("error") and not text:
        return {"status": "error", "rows": [], "count": 0, "tax_note": note, "via": "", "note": str(snap["error"])[:160]}
    heads = " ".join(str(b.get("body") if isinstance(b, dict) else b)[:2000] for b in (snap.get("bodies") or [])[:14])
    wall = detect_wall(snap.get("final_url") or snap.get("url") or "", snap.get("title") or "", text, heads)
    if wall:
        return {"status": wall, "rows": [], "count": 0, "tax_note": note, "via": "", "note": ""}
    if snap.get("timed_out"):
        return {"status": "timeout", "rows": [], "count": 0, "tax_note": note, "via": "", "note": "页面在时限内没有出结果"}
    return {"status": "empty", "rows": [], "count": 0, "tax_note": note, "via": "", "note": "页面上没读到航班"}


# ----------------------------------------------------------------------------------------------- DevTools (VM side)
CDP = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class WS:
    """Minimal DevTools websocket client (one per thread)."""

    def __init__(self, url: str, timeout: float = 10):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.sock.recv(4096)
            if not c:
                raise ConnectionError("websocket handshake failed")
            head += c
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket refused")
        self.buf, self.ids, self.on_event, self.pending = head.split(b"\r\n\r\n", 1)[1], 0, None, {}

    def _frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        n, at = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, at = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, at = struct.unpack(">Q", b[2:10])[0], 10
        if len(b) < at + n:
            return None
        self.buf = b[at + n:]
        return b[0], b[at:at + n]

    def send(self, text, op: int = 1):
        data = text.encode() if isinstance(text, str) else text
        n, hdr = len(data), bytes([0x80 | op])
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                                 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(data)))

    def recv(self, wait: float) -> str:
        end, parts = time.time() + wait, b""
        while True:
            fr = self._frame()
            if fr is None:
                self.sock.settimeout(max(0.05, end - time.time()))
                c = self.sock.recv(262144)
                if not c:
                    raise ConnectionError("websocket closed")
                self.buf += c
                continue
            b1, data = fr
            op = b1 & 0x0F
            if op == 9:
                self.send(data, 10)
                continue
            if op == 8:
                raise ConnectionError("websocket closed by peer")
            parts += data
            if b1 & 0x80:
                return parts.decode(errors="replace")

    def _dispatch(self, msg: dict, mid=None) -> bool:
        if "method" in msg and "id" not in msg:
            if self.on_event:
                try:
                    self.on_event(msg)
                except Exception:  # noqa: BLE001
                    pass
            return False
        if mid is not None and msg.get("id") == mid:
            return True
        cb = self.pending.pop(msg.get("id"), None)
        if cb:
            try:
                cb(msg)
            except Exception:  # noqa: BLE001
                pass
        return False

    def call(self, method: str, params: dict | None = None, timeout: float = 10):
        self.ids += 1
        mid = self.ids
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(self.recv(end - time.time()))
            except socket.timeout:
                break
            if self._dispatch(msg, mid):
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def request(self, method: str, params: dict, cb):
        self.ids += 1
        self.pending[self.ids] = cb
        self.send(json.dumps({"id": self.ids, "method": method, "params": params}))

    def pump(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self._dispatch(json.loads(self.recv(end - time.time())))
            except (socket.timeout, ValueError):
                break

    def eval(self, expr: str, timeout: float = 8):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        return (r.get("result") or {}).get("value")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _http(path: str):
    with LOCAL.open(CDP.rstrip("/") + path, timeout=4) as r:   # noqa: S310 - local DevTools only
        return json.loads(r.read().decode())


def find_cdp() -> str:
    """'' when a DevTools endpoint answers (CDP set to it), else what was tried."""
    global CDP
    cands = [CDP.rstrip("/"), "http://127.0.0.1:9222"]
    for d in ("~/.config/aiw-chrome", "~/.config/google-chrome", "~/.config/chromium", "~/.config/browser-os"):
        try:
            with open(os.path.join(os.path.expanduser(d), "DevToolsActivePort")) as f:
                p = f.readline().strip()
            if p.isdigit():
                cands.append(f"http://127.0.0.1:{p}")
        except OSError:
            pass
    tried = []
    for base in dict.fromkeys(cands):
        try:
            with LOCAL.open(base + "/json/version", timeout=3) as r:   # noqa: S310
                if json.loads(r.read().decode()).get("webSocketDebuggerUrl"):
                    CDP = base
                    return ""
        except Exception as e:  # noqa: BLE001
            tried.append(f"{base.rsplit(':', 1)[-1]}: {e}")
    return "; ".join(tried)


STATE_JS = r"""(() => { const t = (document.body && document.body.innerText) || '';
  const n = (t.match(/(?:[A-Z]{2}|[A-Z]\d|\d[A-Z])\d{3,4}/g) || []).length;
  return JSON.stringify({href: location.href, title: document.title, len: t.length, fn: n, ready: document.readyState}); })()"""
FINAL_JS = r"""(() => { const t = ((document.body && document.body.innerText) || '').slice(0, 60000);
  let nuxt = '';
  try { const N = window.__NUXT__ || window.__INITIAL_STATE__ || window.__PRELOADED_STATE__;
        if (N) { const s = (N.state && (N.state.book1 || N.state.flight)) || N.state || N; nuxt = JSON.stringify(s).slice(0, 3000000); } } catch (e) {}
  return JSON.stringify({href: location.href, title: document.title, text: t, nuxt}); })()"""
HIDE_WEBDRIVER = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def run_job(job: dict, deadline: float, out: dict) -> None:
    """One platform x leg in its own window. Fills out[...] with the raw snapshot (never raises)."""
    snap = {"platform": job["platform"], "leg": job.get("leg", "out"), "url": job["url"], "bodies": [], "text": "",
            "title": "", "final_url": "", "nuxt": "", "timed_out": False, "error": ""}
    out[job["key"]] = snap
    ws, tid, t0 = None, None, time.time()
    pat, total = CAPTURE.get(job["platform"]), [0]
    want: dict = {}
    try:
        bws = WS(_http("/json/version")["webSocketDebuggerUrl"])
        try:
            tid = bws.call("Target.createTarget", {"url": "about:blank", "newWindow": True}, 8)["targetId"]
        finally:
            bws.close()
        ws = WS(CDP.replace("http://", "ws://").rstrip("/") + f"/devtools/page/{tid}")
        ws.call("Network.enable", {"maxTotalBufferSize": 50_000_000, "maxResourceBufferSize": 8_000_000})
        ws.call("Page.enable")
        ua = MOBILE_UA if job.get("ua") == "mobile" else DESKTOP_UA
        ws.call("Network.setUserAgentOverride", {"userAgent": ua, "acceptLanguage": "zh-CN,zh;q=0.9"})
        if job.get("ua") == "mobile":
            try:
                ws.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True})
                ws.call("Emulation.setTouchEmulationEnabled", {"enabled": True})
            except Exception:  # noqa: BLE001
                pass
        ws.call("Page.addScriptToEvaluateOnNewDocument", {"source": HIDE_WEBDRIVER})
        try:
            ws.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception:  # noqa: BLE001
            pass

        def on_event(m):
            meth, p = m.get("method"), m.get("params") or {}
            if meth == "Network.responseReceived":
                r = p.get("response") or {}
                u, mime = r.get("url") or "", (r.get("mimeType") or "").lower()
                if ("json" in mime or "javascript" in mime or "text/plain" in mime or "event-stream" in mime) and \
                        ((pat and pat.search(u)) or ANY_FLIGHT_JSON.search(u)) and not re.search(r"\.js(\?|$)", u):
                    want[p.get("requestId")] = u
            elif meth == "Network.loadingFinished" and p.get("requestId") in want and len(snap["bodies"]) < 14:
                rid = p["requestId"]
                u = want.pop(rid)

                def got(msg, _u=u):
                    body = (msg.get("result") or {}).get("body") or ""
                    if (msg.get("result") or {}).get("base64Encoded"):
                        try:
                            body = base64.b64decode(body).decode("utf-8", "replace")
                        except Exception:  # noqa: BLE001
                            body = ""
                    if body and total[0] + len(body) < 12_000_000:
                        total[0] += len(body)
                        snap["bodies"].append({"url": _u[:300], "body": body[:4_000_000]})
                ws.request("Network.getResponseBody", {"requestId": rid}, got)
        ws.on_event = on_event
        ws.call("Page.navigate", {"url": job["url"]}, 15)
        stable, last = 0, None
        while time.time() < deadline:
            ws.pump(1.2)
            try:
                st = json.loads(ws.eval(STATE_JS, 4) or "{}")
            except Exception:  # noqa: BLE001
                continue
            snap["final_url"], snap["title"] = st.get("href") or "", st.get("title") or ""
            if snap["bodies"] and any(walk_flights(j) for j in _bodies_json(snap["bodies"][-3:])):
                ws.pump(1.5)   # the rest of a paged / streamed answer
                break
            cur = (st.get("len"), st.get("fn"))
            stable = stable + 1 if cur == last and (st.get("fn") or 0) >= 3 else 0
            last = cur
            if stable >= 2:
                break
            if time.time() - t0 > 8 and detect_wall(st.get("href"), st.get("title"), "") in ("login", "captcha"):
                break
        else:
            snap["timed_out"] = True
        try:
            fin = json.loads(ws.eval(FINAL_JS, 10) or "{}")
            snap.update({"final_url": fin.get("href") or snap["final_url"], "title": fin.get("title") or snap["title"],
                         "text": fin.get("text") or "", "nuxt": fin.get("nuxt") or ""})
        except Exception as e:  # noqa: BLE001
            snap["error"] = snap["error"] or f"read: {type(e).__name__}: {e}"[:200]
        ws.pump(0.5)
    except Exception as e:  # noqa: BLE001
        snap["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        snap["elapsed_ms"] = int((time.time() - t0) * 1000)
        if ws is not None:
            ws.close()
        if tid:
            try:
                bws = WS(_http("/json/version")["webSocketDebuggerUrl"], 5)
                bws.call("Target.closeTarget", {"targetId": tid}, 5)
                bws.close()
            except Exception:  # noqa: BLE001
                pass


def save_raw(snaps: dict) -> str:
    d = os.path.expanduser("~/.cache/aiw/flight")
    try:
        os.makedirs(d, exist_ok=True)
        for k, s in snaps.items():
            with open(os.path.join(d, f"{k}.json"), "w") as f:
                json.dump(s, f, ensure_ascii=False)
        return d
    except OSError:
        return ""


def compact(snaps: dict, jobs: list) -> dict:
    res = []
    for j in jobs:
        s = snaps.get(j["key"]) or {"error": "not run", "url": j["url"]}
        p = parse_job(j["platform"], s)
        res.append({"key": j["key"], "platform": j["platform"], "leg": j.get("leg", "out"), "url": j["url"],
                    "final_url": (s.get("final_url") or "")[:300], "title": (s.get("title") or "")[:80],
                    "elapsed_ms": s.get("elapsed_ms"), "bodies": len(s.get("bodies") or []), **p})
    return {"ok": True, "probe_version": PROBE_VERSION, "results": res}


def cmd_search(a) -> None:
    jobs = json.loads(base64.b64decode(a.jobs).decode())
    budget = max(15.0, min(float(a.budget or 70), 170.0))
    t_end = time.time() + budget
    for i, j in enumerate(jobs):
        j.setdefault("key", f"{j['platform']}-{j.get('leg', 'out')}")
    err = find_cdp()
    if err:
        print(json.dumps({"ok": False, "reason": "no_cdp", "error": "NO_CDP: " + err[:300]}, ensure_ascii=False))
        sys.exit(3)
    snaps: dict = {}

    def dump(*_):
        out = compact(snaps, jobs)
        out["partial"] = True
        out["raw_dir"] = save_raw(snaps)
        print(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
        os._exit(0)
    try:
        signal.signal(signal.SIGALRM, dump)
        signal.alarm(int(budget + 15))
    except (ValueError, AttributeError):
        pass
    sem = threading.Semaphore(int(a.parallel or 4))

    def one(j):
        with sem:
            run_job(j, min(t_end, time.time() + float(j.get("timeout") or budget)), snaps)
    ths = [threading.Thread(target=one, args=(j,), daemon=True) for j in jobs]
    for th in ths:
        th.start()
    for th in ths:
        th.join(max(0.1, t_end + 8 - time.time()))
    try:
        signal.alarm(0)
    except (ValueError, AttributeError):
        pass
    out = compact(snaps, jobs)
    out["raw_dir"] = save_raw(snaps)
    print(json.dumps(out, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("search")
    s.add_argument("--jobs", required=True)
    s.add_argument("--budget", default="70")
    s.add_argument("--parallel", default="4")
    sub.add_parser("version")
    a = ap.parse_args()
    if a.cmd == "version":
        print(json.dumps({"version": PROBE_VERSION}))
        return
    if a.cmd == "search":
        cmd_search(a)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
