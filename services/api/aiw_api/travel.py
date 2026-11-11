"""v3.9.40 出行: restaurant hand-off cards (大众点评 / 美团) and the multi-platform 机票比价 (携程 / 飞猪 / 去哪儿 / 同程).

Restaurant ("path 1")
  The agent finds the restaurant with its normal tools (web search, the browser), then calls restaurant_booking with
  what it found. The tool builds a card: name, address, rating / 人均 when known, the suggested date / time / party
  size, and buttons 「打开大众点评预订」/「打开美团」. The phone opens the app on the shop page when a shop id is known
  (from a dianping / meituan link), else the app's search for the name; with the app missing, the mobile web page.
  Nothing is booked or paid by aiw: the user confirms in the app.

Flights
  flight_search runs flight_probe.py on the task's computer: every enabled platform (and the return leg, if any) in
  its own Chrome window at the same time, each with a timeout; the pages' own JSON / page state / text are parsed
  there (flight_probe.parse_job) and come back as compact rows. They are merged here into one table sorted by price,
  with each platform's status (ok / 需登录 / 需验证 / 无结果 / 超时 / 失败), so one platform's wall never fails the
  search. Results are cached for travel.cache_minutes (default 10). Booking = the row's link opens that platform's
  app (universal link / App Link) or its mobile web page for the route and date; no order, no payment.

Cards
  A tool call stores its card under kv task_cards:<task id>; chat.py puts them on the task card's result
  (result.cards), which the web app, the Android WebView and the iOS app render with their buttons.

URL schemes / links used here are written from public collections and docs, not tested on a phone; see LINKS_NOTE.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

from . import flight_probe as fp

log = logging.getLogger("aiw.travel")

PROBE_SRC = Path(__file__).with_name("flight_probe.py")
REMOTE = "$HOME/.config/aiw/travel/flight_probe.py"
TZ = _dt.timezone(_dt.timedelta(hours=8))   # China, no DST

LINKS_NOTE = ("链接说明：大众点评 dianping://shopinfo?id= / ?shopuuid= / searchshoplist?keyword=、美团 "
              "imeituan://www.meituan.com/food/poi/detail?id= / search?q= 来自公开的 URL Scheme 合集，未在真机上验证；"
              "机票各平台优先打开其网页链接（装了 App 时系统可能直接用 App 打开），App 图标按钮只打开 App 首页。")

# ------------------------------------------------------------------------------------------------- settings
DEFAULTS = {"flight_platforms": {"ctrip": True, "fliggy": True, "qunar": True, "tongcheng": True},
            "cache_minutes": 10, "timeout_s": 70, "max_rows": 15, "restaurant_apps": ["dianping", "meituan"]}
KV = "travel_settings"


def settings(store) -> dict:
    s = dict(DEFAULTS)
    try:
        got = (store.kv_get(KV, {}) if store is not None else {}) or {}
    except Exception:  # noqa: BLE001
        got = {}
    plats = dict(DEFAULTS["flight_platforms"])
    plats.update({k: bool(v) for k, v in (got.get("flight_platforms") or {}).items() if k in plats})
    s.update({k: v for k, v in got.items() if k in DEFAULTS and k != "flight_platforms"})
    s["flight_platforms"] = plats
    return s


def save_settings(store, body: dict) -> dict:
    cur = settings(store)
    b = body or {}
    if isinstance(b.get("flight_platforms"), dict):
        for k, v in b["flight_platforms"].items():
            if k in cur["flight_platforms"]:
                cur["flight_platforms"][k] = bool(v)
    for k, lo, hi in (("cache_minutes", 0, 240), ("timeout_s", 20, 160), ("max_rows", 5, 40)):
        if k in b:
            try:
                cur[k] = max(lo, min(hi, int(b[k])))
            except (TypeError, ValueError):
                pass
    if isinstance(b.get("restaurant_apps"), list):
        apps = [a for a in b["restaurant_apps"] if a in ("dianping", "meituan")]
        cur["restaurant_apps"] = apps or ["dianping"]
    store.kv_set(KV, cur)
    return cur


# ------------------------------------------------------------------------------------------------- cities
# name -> (IATA city code, airports). City codes are what the OTAs search by (BJS = 首都 + 大兴, SHA = 虹桥 + 浦东).
CITIES = {
    "北京": ("BJS", ["PEK", "PKX"]), "上海": ("SHA", ["SHA", "PVG"]), "广州": ("CAN", ["CAN"]), "深圳": ("SZX", ["SZX"]),
    "成都": ("CTU", ["CTU", "TFU"]), "重庆": ("CKG", ["CKG"]), "杭州": ("HGH", ["HGH"]), "西安": ("SIA", ["XIY"]),
    "昆明": ("KMG", ["KMG"]), "南京": ("NKG", ["NKG"]), "武汉": ("WUH", ["WUH"]), "长沙": ("CSX", ["CSX"]),
    "厦门": ("XMN", ["XMN"]), "青岛": ("TAO", ["TAO"]), "三亚": ("SYX", ["SYX"]), "海口": ("HAK", ["HAK"]),
    "天津": ("TSN", ["TSN"]), "郑州": ("CGO", ["CGO"]), "沈阳": ("SHE", ["SHE"]), "大连": ("DLC", ["DLC"]),
    "哈尔滨": ("HRB", ["HRB"]), "长春": ("CGQ", ["CGQ"]), "济南": ("TNA", ["TNA"]), "福州": ("FOC", ["FOC"]),
    "南宁": ("NNG", ["NNG"]), "贵阳": ("KWE", ["KWE"]), "乌鲁木齐": ("URC", ["URC"]), "兰州": ("LHW", ["LHW"]),
    "银川": ("INC", ["INC"]), "西宁": ("XNN", ["XNN"]), "拉萨": ("LXA", ["LXA"]), "呼和浩特": ("HET", ["HET"]),
    "太原": ("TYN", ["TYN"]), "石家庄": ("SJW", ["SJW"]), "合肥": ("HFE", ["HFE"]), "南昌": ("KHN", ["KHN"]),
    "宁波": ("NGB", ["NGB"]), "温州": ("WNZ", ["WNZ"]), "无锡": ("WUX", ["WUX"]), "珠海": ("ZUH", ["ZUH"]),
    "桂林": ("KWL", ["KWL"]), "丽江": ("LJG", ["LJG"]), "西双版纳": ("JHG", ["JHG"]), "大理": ("DLU", ["DLU"]),
    "烟台": ("YNT", ["YNT"]), "泉州": ("JJN", ["JJN"]), "揭阳": ("SWA", ["SWA"]), "汕头": ("SWA", ["SWA"]),
    "张家界": ("DYG", ["DYG"]), "北海": ("BHY", ["BHY"]), "常州": ("CZX", ["CZX"]), "徐州": ("XUZ", ["XUZ"]),
    "南通": ("NTG", ["NTG"]), "扬州": ("YTY", ["YTY"]), "绵阳": ("MIG", ["MIG"]), "宜昌": ("YIH", ["YIH"]),
    "襄阳": ("XFN", ["XFN"]), "洛阳": ("LYA", ["LYA"]), "包头": ("BAV", ["BAV"]), "鄂尔多斯": ("DSN", ["DSN"]),
    "喀什": ("KHG", ["KHG"]), "伊宁": ("YIN", ["YIN"]), "九寨沟": ("JZH", ["JZH"]), "黄山": ("TXN", ["TXN"]),
    "香港": ("HKG", ["HKG"]), "澳门": ("MFM", ["MFM"]), "台北": ("TPE", ["TPE", "TSA"]),
}
AIRPORT_CITY = {ap: name for name, (_c, aps) in CITIES.items() for ap in aps}
AIRPORT_ALIASES = {"虹桥": "上海", "浦东": "上海", "首都": "北京", "大兴": "北京", "白云": "广州", "宝安": "深圳",
                   "双流": "成都", "天府": "成都", "江北": "重庆", "萧山": "杭州", "咸阳": "西安", "长水": "昆明", "禄口": "南京",
                   "天河": "武汉", "黄花": "长沙", "高崎": "厦门", "胶东": "青岛", "凤凰": "三亚", "美兰": "海口"}


def resolve_city(s: str) -> Optional[tuple[str, str]]:
    """'上海' / '上海虹桥' / '虹桥机场' / 'SHA' / 'PVG' / 'sha' -> ('SHA', '上海'). None when unknown."""
    t = re.sub(r"\s+|市$|(国际)?机场$", "", str(s or "")).strip()
    if not t:
        return None
    up = t.upper()
    if re.fullmatch(r"[A-Z]{3}", up):
        for name, (code, aps) in CITIES.items():
            if code == up:
                return code, name
        if up in AIRPORT_CITY:
            name = AIRPORT_CITY[up]
            return CITIES[name][0], name
        return up, up   # an IATA code we do not list: passed through as is
    for name in sorted(CITIES, key=len, reverse=True):
        if t.startswith(name) or name.startswith(t) and len(t) >= 2:
            return CITIES[name][0], name
    for alias, name in AIRPORT_ALIASES.items():
        if alias in t:
            return CITIES[name][0], name
    return None


def parse_date(s: Any, today: Optional[_dt.date] = None) -> Optional[_dt.date]:
    """YYYY-MM-DD / YYYY/MM/DD / M月D日 / MM-DD (this year, or next year when already past) / 今天 明天 后天."""
    today = today or _dt.datetime.now(TZ).date()
    t = str(s or "").strip()
    if not t:
        return None
    rel = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
    if t in rel:
        return today + _dt.timedelta(days=rel[t])
    m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", t)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[-/.月](\d{1,2})[日号]?", t)
    if m:
        try:
            d = _dt.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return d if d >= today else d.replace(year=today.year + 1)
    return None


WEEK = "一二三四五六日"


def cn_date(d: _dt.date) -> str:
    return f"{d.month}月{d.day}日 周{WEEK[d.weekday()]}"


# ------------------------------------------------------------------------------------------------- cards per task
def add_card(store, task_id: Optional[str], card: dict) -> None:
    if store is None or not task_id:
        return
    key = f"task_cards:{task_id}"
    cards = [c for c in (store.kv_get(key, []) or []) if c.get("id") != card.get("id")]
    cards.append(card)
    store.kv_set(key, cards[-6:])


def cards_for(store, task_id: Optional[str]) -> list:
    if store is None or not task_id:
        return []
    try:
        return list(store.kv_get(f"task_cards:{task_id}", []) or [])
    except Exception:  # noqa: BLE001
        return []


def _card_id(kind: str, *parts) -> str:
    import hashlib
    return kind + "-" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


# ------------------------------------------------------------------------------------------------- restaurants
# 大众点评 city ids for its web search (1 上海 2 北京 3 杭州 4 广州 5 南京 6 苏州 7 深圳 8 成都 9 重庆 10 天津 16 武汉
# 17 西安; from its long-standing /search/keyword/<city>/ URLs; unverified today). Unknown city -> 0.
DP_CITY = {"上海": 1, "北京": 2, "杭州": 3, "广州": 4, "南京": 5, "苏州": 6, "深圳": 7, "成都": 8, "重庆": 9, "天津": 10,
           "武汉": 16, "西安": 17}


def dianping_ids(url_or_id: str) -> dict:
    """{'id': '1859284'} / {'uuid': 'G3IAnb1UxyTKS8TB'} from a 大众点评 link (or a bare id)."""
    s = str(url_or_id or "").strip()
    if not s:
        return {}
    if re.fullmatch(r"\d{4,14}", s):
        return {"id": s}
    if re.fullmatch(r"[A-Za-z0-9]{10,24}", s) and re.search(r"[A-Za-z]", s):
        return {"uuid": s}
    m = re.search(r"[?&](?:shopId|shopid|id)=(\d{4,14})", s)
    if m and ("dianping" in s or "shop" in s.lower()):
        return {"id": m.group(1)}
    m = re.search(r"[?&](?:shopUuid|shopuuid)=([A-Za-z0-9]{6,32})", s)
    if m:
        return {"uuid": m.group(1)}
    m = re.search(r"dianping\.com/(?:shop|shopshare)/([A-Za-z0-9]{4,32})", s)
    if m:
        v = m.group(1)
        return {"id": v} if v.isdigit() else {"uuid": v}
    return {}


def meituan_poi(url_or_id: str) -> str:
    s = str(url_or_id or "").strip()
    if re.fullmatch(r"\d{4,16}", s):
        return s
    for rx in (r"meituan\.com/meishi/(\d{4,16})", r"/poi/(?:detail\?id=)?(\d{4,16})", r"[?&]poi[Ii]d=(\d{4,16})",
               r"imeituan://\S*[?&]id=(\d{4,16})"):
        m = re.search(rx, s)
        if m:
            return m.group(1)
    return ""


def dianping_link(name: str, city: str = "", shop: Optional[dict] = None) -> dict:
    shop = shop or {}
    kw = (name if not city or city in name else f"{city} {name}").strip()
    if shop.get("id"):
        return {"app": f"dianping://shopinfo?id={shop['id']}", "web": f"https://m.dianping.com/shop/{shop['id']}",
                "prefer": "app", "target": "shop"}
    if shop.get("uuid"):
        return {"app": f"dianping://shopinfo?shopuuid={shop['uuid']}", "web": f"https://www.dianping.com/shop/{shop['uuid']}",
                "prefer": "app", "target": "shop"}
    return {"app": "dianping://searchshoplist?keyword=" + quote(kw),
            "web": f"https://www.dianping.com/search/keyword/{DP_CITY.get(city, 0)}/0_" + quote(name),
            "prefer": "app", "target": "search"}


def meituan_link(name: str, city: str = "", poi: str = "") -> dict:
    kw = (name if not city or city in name else f"{city} {name}").strip()
    if poi:
        return {"app": f"imeituan://www.meituan.com/food/poi/detail?id={poi}", "web": f"https://www.meituan.com/meishi/{poi}/",
                "prefer": "app", "target": "shop"}
    return {"app": "imeituan://www.meituan.com/search?q=" + quote(kw), "web": "https://www.meituan.com/s/" + quote(kw) + "/",
            "prefer": "app", "target": "search"}


def _clock(s: Any) -> str:
    m = re.search(r"(\d{1,2})[:：点](\d{2})?", str(s or ""))
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if "下午" in str(s) or "晚" in str(s):
        h = h + 12 if h < 12 else h
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else ""


def restaurant_card(a: dict, apps: Optional[list] = None, today: Optional[_dt.date] = None) -> dict:
    name = str(a.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("缺少餐厅名称 name")
    city = str(a.get("city") or "").strip()[:12]
    city = re.sub(r"市$", "", city)
    addr = str(a.get("address") or "").strip()[:120]
    facts = []
    try:
        r = float(a.get("rating")) if a.get("rating") not in (None, "") else None
    except (TypeError, ValueError):
        r = None
    if r:
        facts.append(f"★ {r:g}")
    if a.get("avg_price") not in (None, ""):
        try:
            facts.append(f"人均 ¥{float(a['avg_price']):g}")
        except (TypeError, ValueError):
            facts.append(f"人均 {str(a['avg_price'])[:12]}")
    if a.get("cuisine"):
        facts.append(str(a["cuisine"])[:12])
    if a.get("phone"):
        facts.append(f"电话 {str(a['phone'])[:20]}")
    d = parse_date(a.get("date"), today) if a.get("date") else None
    clock = _clock(a.get("time"))
    try:
        party = int(a.get("party_size") or 0)
    except (TypeError, ValueError):
        party = 0
    party = party if 0 < party <= 60 else 0
    sug = " · ".join(x for x in ((cn_date(d) if d else ""), clock, (f"{party} 人" if party else "")) if x)
    shop = dianping_ids(a.get("dianping_url") or a.get("dianping_shop_id") or "")
    poi = meituan_poi(a.get("meituan_url") or a.get("meituan_poi_id") or "")
    apps = apps or ["dianping", "meituan"]
    buttons = []
    if "dianping" in apps or shop:
        buttons.append({"label": "打开大众点评预订", "app_name": "大众点评", **dianping_link(name, city, shop)})
    if "meituan" in apps or poi:
        buttons.append({"label": "打开美团", "app_name": "美团", **meituan_link(name, city, poi)})
    card = {"kind": "restaurant", "id": _card_id("rest", name, city, sug), "title": name, "subtitle": addr,
            "facts": facts, "suggest": sug, "city": city, "buttons": buttons,
            "note": "在 App 里确认日期、时间和人数后自己提交预订；aiw 不会替你下单或付款。"
                    + ("" if any(b.get("target") == "shop" for b in buttons) else "（没有店铺链接，App 会打开搜索）"),
            "reason": str(a.get("reason") or "")[:160], "at": time.time()}
    return card


def restaurant_text(card: dict) -> str:
    lines = [f"已生成预订卡片：{card['title']}" + (f"（{card['subtitle']}）" if card.get("subtitle") else "")]
    if card.get("facts"):
        lines.append("、".join(card["facts"]))
    if card.get("suggest"):
        lines.append("建议：" + card["suggest"])
    for b in card["buttons"]:
        lines.append(f"- {b['label']}：{b['web']}" + ("（店铺页）" if b.get("target") == "shop" else "（搜索）"))
    lines.append("卡片已显示在用户的任务结果上，用户点按钮会在手机 App 里确认预订。最终回答里说明推荐理由和建议时间/人数，不要说已经订好了。")
    return "\n".join(lines)


def restaurant_tool(store, task) -> tuple:
    schema = {"name": "restaurant_booking", "description": (
        "订餐厅/订位的最后一步：你已经用搜索或浏览器找好了一家餐厅后调用，生成一张给用户的预订卡片（店名、地址、评分、"
        "建议日期时间和人数，以及「打开大众点评预订」「打开美团」按钮，手机上一点就进 App 的店铺页确认预订）。"
        "aiw 不在网页上提交预订、不付款。能拿到大众点评/美团店铺链接就填进来（直达店铺页），拿不到就只填名称和城市（App 里搜索）。"
        "可以为两三家候选各调用一次。"),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "餐厅名称（含分店名）"},
            "city": {"type": "string", "description": "城市，如 上海"},
            "address": {"type": "string"},
            "rating": {"type": "number", "description": "评分（如大众点评 4.6），没有就不填"},
            "avg_price": {"type": "number", "description": "人均（元）"},
            "cuisine": {"type": "string", "description": "菜系/类型"},
            "phone": {"type": "string"},
            "date": {"type": "string", "description": "建议日期 YYYY-MM-DD（用户说的，或你建议的）"},
            "time": {"type": "string", "description": "建议时间 HH:MM"},
            "party_size": {"type": "integer", "description": "人数"},
            "dianping_url": {"type": "string", "description": "大众点评店铺链接或店铺 id（可选）"},
            "meituan_url": {"type": "string", "description": "美团店铺链接或 poi id（可选）"},
            "reason": {"type": "string", "description": "一句话推荐理由"}},
            "required": ["name"]}}

    async def call(a: dict) -> str:
        try:
            card = restaurant_card(a or {}, settings(store).get("restaurant_apps"))
        except ValueError as e:
            return f"没生成卡片：{e}"
        add_card(store, getattr(task, "id", None), card)
        return restaurant_text(card)

    call.needs_confirm = lambda a: False   # type: ignore[attr-defined]
    call.max_result = 1500   # type: ignore[attr-defined]
    return schema, call


# ------------------------------------------------------------------------------------------------- flights
STATUS_CN = {"ok": "成功", "login": "需登录", "captcha": "需验证", "empty": "无结果", "timeout": "超时", "error": "失败",
             "disabled": "已关闭", "skipped": "未查"}
CABIN_WORDS = {"Y": "经济舱", "C": "公务/头等舱", "F": "头等舱"}
_cache: dict = {}


def cabin_code(s: Any) -> str:
    t = str(s or "").strip().upper()
    if not t or t in ("Y", "经济", "经济舱", "ECONOMY"):
        return "Y"
    if t in ("F", "头等", "头等舱", "FIRST"):
        return "F"
    if re.search(r"C|J|公务|商务|BUSINESS", t):
        return "C"
    return "Y"


def cabin_ok(row_cabin: str, want: str) -> bool:
    c = str(row_cabin or "")
    premium = bool(re.search(r"公务|商务|头等|豪华|^[CFJ]$", c))
    if want == "Y":
        return not premium
    return premium if c else True


def plan_jobs(dep: tuple, arr: tuple, d_out: _dt.date, d_ret: Optional[_dt.date], platforms: list, timeout: int) -> list:
    jobs = []
    legs = [("out", dep, arr, d_out)] + ([("ret", arr, dep, d_ret)] if d_ret else [])
    for leg, a, b, d in legs:
        for p in platforms:
            ln = fp.search_links(p, a[0], b[0], d.isoformat(), a[1], b[1])
            jobs.append({"platform": p, "leg": leg, "url": ln["probe"], "ua": ln["ua"], "timeout": timeout,
                         "key": f"{p}-{leg}"})
    return jobs


def merge(results: list, dep: tuple, arr: tuple, d_out: _dt.date, d_ret: Optional[_dt.date], platforms_all: dict,
          adults: int = 1, cabin: str = "Y", direct_only: bool = False, max_rows: int = 15) -> dict:
    """Probe results -> the card (rows sorted by price per leg, every platform's status)."""
    by_key = {r.get("key") or f"{r.get('platform')}-{r.get('leg', 'out')}": r for r in results or []}
    legs = []
    for leg, a, b, d in [("out", dep, arr, d_out)] + ([("ret", arr, dep, d_ret)] if d_ret else []):
        rows, plats = [], []
        for p in fp.PLATFORMS:
            ln = fp.search_links(p, a[0], b[0], d.isoformat(), a[1], b[1])
            link = {"web": ln["web"], "app": ln["app"], "prefer": "web"}
            if not platforms_all.get(p, False):
                plats.append({"platform": p, "name": fp.NAMES[p], "status": "disabled", "status_cn": STATUS_CN["disabled"],
                              "count": 0, "min": None, "link": link, "note": "设置里已关闭"})
                continue
            r = by_key.get(f"{p}-{leg}")
            if r is None:
                plats.append({"platform": p, "name": fp.NAMES[p], "status": "error", "status_cn": STATUS_CN["error"],
                              "count": 0, "min": None, "link": link, "note": "没有返回"})
                continue
            prow = []
            for x in r.get("rows") or []:
                if not cabin_ok(x.get("cabin"), cabin) or (direct_only and x.get("transfer")):
                    continue
                prow.append({**x, "platform": p, "platform_name": fp.NAMES[p], "tax_note": r.get("tax_note") or "未注明",
                             "link": link})
            st = r.get("status") or "error"
            if st == "ok" and not prow:
                st = "empty"
            note = r.get("note") or ""
            if st == "ok" and r.get("via") == "text":
                note = "从页面文字读出"
            plats.append({"platform": p, "name": fp.NAMES[p], "status": st, "status_cn": STATUS_CN.get(st, st),
                          "count": len(prow), "min": min((x["price"] for x in prow), default=None), "link": link,
                          "note": note, "tax_note": r.get("tax_note") or "", "elapsed_ms": r.get("elapsed_ms")})
            rows += prow
        rows.sort(key=lambda x: (x["price"], x.get("dep_time") or "", x["platform"]))
        legs.append({"leg": leg, "label": ("去程" if leg == "out" else "返程") + f" {cn_date(d)}",
                     "from": a[1], "to": b[1], "date": d.isoformat(), "rows": rows[:max_rows], "total": len(rows),
                     "platforms": plats})
    best = legs[0]["rows"][0] if legs and legs[0]["rows"] else None
    title = f"{dep[1]} → {arr[1]} · {cn_date(d_out)}" + (f"（往返 {cn_date(d_ret)}）" if d_ret else "")
    sub = (f"最低 ¥{best['price']:g} · {best['platform_name']} {best['flight_no']} {best.get('dep_time') or ''}"
           if best else "没有查到可比较的价格")
    return {"kind": "flights", "id": _card_id("fl", dep[0], arr[0], d_out, d_ret, cabin), "title": title, "subtitle": sub,
            "adults": adults, "cabin": CABIN_WORDS.get(cabin, cabin), "legs": legs, "at": time.time(),
            "note": "价格为各平台页面显示的单人价，是否含机建燃油见每行说明；点一行在对应平台的 App/网页里选同一航班下单，"
                    "aiw 不会替你下单或付款。"}


def flight_text(card: dict) -> str:
    out = [card["title"], card["subtitle"]]
    for lg in card["legs"]:
        out.append(f"\n{lg['label']} {lg['from']}→{lg['to']}（共 {lg['total']} 条，按价格）")
        if lg["rows"]:
            out.append("| 平台 | 航班 | 航司 | 起飞-到达 | 价格 | 税费 |")
            out.append("|---|---|---|---|---|---|")
            for x in lg["rows"][:10]:
                out.append(f"| {x['platform_name']} | {x['flight_no']}{'（中转）' if x.get('transfer') else ''} | {x.get('airline') or ''} | "
                           f"{x.get('dep_time') or '?'}-{x.get('arr_time') or '?'} | ¥{x['price']:g} | {x.get('tax_note') or ''} |")
        st = "；".join(f"{p['name']}：{p['status_cn']}" + (f"（最低 ¥{p['min']:g}）" if p.get("min") else "")
                      + (f"（{p['note']}）" if p.get("note") and p["status"] != "ok" else "") for p in lg["platforms"])
        out.append("各平台：" + st)
    if card.get("adults", 1) > 1:
        out.append(f"{card['adults']} 人同行：总价约为单价 × {card['adults']}。")
    out.append("结果卡片（带每个平台的预订链接）已经放在任务结果上。最终回答：给出最低价和对应平台/航班，附上面的表格，"
               "说明需登录/需验证/失败的平台没有比到；不要替用户下单。")
    return "\n".join(out)


Runner = Callable[[list, int], Awaitable[dict]]


async def probe_runner(computer, jobs: list, budget: int, parallel: int = 4) -> dict:
    """Ship + run flight_probe.py search on the computer; one retry after reopening Chrome's DevTools port."""
    from . import shopwatch as sw
    b64 = base64.b64encode(json.dumps(jobs, ensure_ascii=False).encode()).decode()

    async def once():
        cmd = sw.cdp_env(computer) + sw.ship_command(PROBE_SRC, REMOTE, "search", jobs=b64, budget=str(budget),
                                                     parallel=str(parallel))
        try:
            r = await computer.execute("shell.exec", {"command": cmd, "timeout": budget + 30}, timeout=budget + 45)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        got = sw._last_json(r)
        return got if got is not None else {"ok": False, "error": "probe failed: " + sw._err_of(r)}
    got = await once()
    if got.get("reason") == "no_cdp":
        res = await sw.ensure_cdp(computer, getattr(computer, "machine_id", None))
        if res.get("ok"):
            got = await once()
    return got


def flight_tool(store, task, computer, runner: Optional[Runner] = None) -> tuple:
    schema = {"name": "flight_search", "description": (
        "机票比价：同时在携程、飞猪、去哪儿、同程（设置里开着的平台）查同一航线同一天的航班，合并成一张按价格排序的表"
        "（平台、航班号、航司、起降时间、价格、是否含税、预订链接），并显示每个平台成功/需登录/需验证/无结果。"
        "用户问机票、最便宜的航班、哪个平台便宜时用它，一次调用就够，不要自己逐个打开这些网站。只比价和给链接，不下单不付款。"),
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string", "description": "出发城市或机场，如 上海 / 虹桥 / SHA"},
            "destination": {"type": "string", "description": "到达城市或机场，如 北京 / PEK"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天/明天也可以）"},
            "return_date": {"type": "string", "description": "返程日期 YYYY-MM-DD，单程不填"},
            "adults": {"type": "integer", "description": "成人数，默认 1"},
            "cabin": {"type": "string", "description": "经济舱（默认）/ 公务舱 / 头等舱"},
            "direct_only": {"type": "boolean", "description": "只要直飞"},
            "platforms": {"type": "array", "items": {"type": "string", "enum": list(fp.PLATFORMS)},
                          "description": "只查这些平台（默认设置里开着的全部）"},
            "refresh": {"type": "boolean", "description": "忽略缓存重新查"}},
            "required": ["origin", "destination", "date"]}}

    async def call(a: dict) -> str:
        a = a or {}
        dep, arr = resolve_city(a.get("origin")), resolve_city(a.get("destination"))
        if not dep or not arr:
            bad = a.get("origin") if not dep else a.get("destination")
            return f"不认识「{bad}」这个城市/机场：请改用城市名（如 上海）或三字码（如 SHA）再调用一次。"
        if dep[0] == arr[0]:
            return "出发地和目的地是同一个城市，请确认。"
        today = _dt.datetime.now(TZ).date()
        d_out = parse_date(a.get("date"), today)
        if not d_out:
            return "出发日期看不懂，请用 YYYY-MM-DD 再调用一次。"
        if d_out < today:
            return f"出发日期 {d_out.isoformat()} 已经过去了（今天 {today.isoformat()}），请和用户确认日期。"
        d_ret = parse_date(a.get("return_date"), today) if a.get("return_date") else None
        if a.get("return_date") and not d_ret:
            return "返程日期看不懂，请用 YYYY-MM-DD。"
        if d_ret and d_ret < d_out:
            return "返程日期早于出发日期，请确认。"
        try:
            adults = max(1, min(9, int(a.get("adults") or 1)))
        except (TypeError, ValueError):
            adults = 1
        cab = cabin_code(a.get("cabin"))
        st = settings(store)
        enabled = {p: bool(st["flight_platforms"].get(p)) for p in fp.PLATFORMS}
        want = [p for p in (a.get("platforms") or []) if p in fp.PLATFORMS]
        if want:
            enabled = {p: (p in want and enabled[p]) for p in fp.PLATFORMS}
        plats = [p for p in fp.PLATFORMS if enabled[p]]
        if not plats:
            return "设置 › 出行 里把机票平台都关掉了（或指定的平台已关闭），没有可以查的平台。"
        key = (dep[0], arr[0], d_out.isoformat(), d_ret.isoformat() if d_ret else "", ",".join(plats))
        ttl = int(st.get("cache_minutes") or 0) * 60
        hit = _cache.get(key)
        cached = bool(hit and ttl and time.time() - hit[0] < ttl and not a.get("refresh"))
        if cached:
            results = hit[1]
        else:
            timeout = int(st.get("timeout_s") or 70)
            jobs = plan_jobs(dep, arr, d_out, d_ret, plats, timeout)
            run = runner or (lambda j, b: probe_runner(computer, j, b))
            got = await run(jobs, timeout + 10)
            if not got.get("ok") and not got.get("results"):
                err = str(got.get("error") or got.get("reason") or "未知错误")[:200]
                return (f"比价没跑起来：{err}。可能是电脑上的浏览器调试端口没开、电脑离线或超时。"
                        "可以稍后再试，或者告诉用户直接打开这些平台查：" +
                        "、".join(f"{fp.NAMES[p]} {fp.search_links(p, dep[0], arr[0], d_out.isoformat(), dep[1], arr[1])['web']}"
                                 for p in plats))
            results = got.get("results") or []
            if any(r.get("status") == "ok" for r in results) and ttl:
                _cache[key] = (time.time(), results)
                for k in [k for k, v in _cache.items() if time.time() - v[0] > max(ttl, 600) * 2]:
                    _cache.pop(k, None)
        card = merge(results, dep, arr, d_out, d_ret, enabled, adults, cab, bool(a.get("direct_only")),
                     int(st.get("max_rows") or 15))
        card["cached"] = cached
        add_card(store, getattr(task, "id", None), card)
        return flight_text(card) + ("\n（结果来自几分钟内的缓存）" if cached else "")

    call.needs_confirm = lambda a: False   # type: ignore[attr-defined]
    call.max_result = 5000   # type: ignore[attr-defined]
    return schema, call


# ------------------------------------------------------------------------------------------------- agent wiring
FLIGHT_ASK = re.compile(r"(机票|飞机票|航班|特价票).{0,16}(价|便宜|最低|多少钱|比价|比一比|对比|查|订|搜|找|看)|"
                        r"(查|订|搜|比|找|看|买).{0,20}(机票|飞机票|航班)|(最便宜|最低价|特价|便宜).{0,6}(机票|飞机票|航班)|(飞|坐飞机)(去|到|回).{0,12}(便宜|最低|多少钱|机票)|"
                        r"(携程|飞猪|去哪儿|同程).{0,10}(机票|航班|比价|哪个便宜)")
RESTAURANT_ASK = re.compile(r"(订|预订|预定|约|找|推荐|定).{0,8}(餐厅|饭店|餐馆|位子|座位|包间|包厢|饭馆|馆子|一桌|桌子)|"
                            r"(大众点评|美团).{0,8}(订|预订|预定|订座|订位)|订座|订位|(吃饭|聚餐|请客|约饭).{0,12}(地方|餐厅|订)")
NOT_RESTAURANT = re.compile(r"外卖|点餐|下单买|快递")


def wants_flights(text: str) -> bool:
    return bool(FLIGHT_ASK.search(str(text or "")))


def wants_restaurant(text: str) -> bool:
    t = str(text or "")
    return bool(RESTAURANT_ASK.search(t)) and not NOT_RESTAURANT.search(t)


def wants_travel(text: str) -> bool:
    return wants_flights(text) or wants_restaurant(text)


def prompt_extra(prompt: str) -> str:
    s = ""
    if wants_flights(prompt):
        s += ("\n机票比价（v3.9.40）：调用 flight_search 工具，它一次就并行查携程/飞猪/去哪儿/同程并合并成按价格排序的表；"
              "不要自己逐个打开这些网站，不要登录、不要过验证、不要下单付款。缺出发地/目的地/日期时先 ask_user。"
              "最终回答写最低价（平台+航班+时间+是否含税），附 flight_search 给的表格，并列出没比到的平台和原因。")
    if wants_restaurant(prompt):
        s += ("\n订餐厅（v3.9.40，交给手机 App 确认）：先用搜索/浏览器按用户的城市、位置、口味、人数、预算找 1-3 家合适的餐厅"
              "（尽量拿到大众点评或美团的店铺链接、评分、人均、地址），再对每家调用 restaurant_booking 生成预订卡片，"
              "建议日期时间和人数（用户没说时按常理建议并说明）。不要在网页上提交预订或付款；最终回答里说明推荐理由，"
              "并告诉用户点卡片上的按钮在大众点评/美团 App 里确认。")
    return s


def task_tools(store, task, computer) -> dict:
    return {"restaurant_booking": restaurant_tool(store, task), "flight_search": flight_tool(store, task, computer)}


# ------------------------------------------------------------------------------------------------- HTTP
def mount(app, store, auth) -> None:
    from fastapi import Depends
    dep = [Depends(auth)]

    @app.get("/api/travel/settings", dependencies=dep)
    async def travel_get():
        return {**settings(store), "platform_names": {p: fp.NAMES[p] for p in fp.PLATFORMS}, "links_note": LINKS_NOTE}

    @app.put("/api/travel/settings", dependencies=dep)
    async def travel_put(body: dict):
        return save_settings(store, body or {})

    @app.get("/api/travel/cards/{task_id}", dependencies=dep)
    async def travel_cards(task_id: str):
        return {"cards": cards_for(store, task_id)}
