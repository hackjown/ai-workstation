"""v3.9.50: a scheduled brief (the 7:30 新闻早报 routine) lands in the chat, with today's detailed weather on top.

Before, a routine ran as a task of its own thread and only its push notification said it was ready: reading it meant
opening 任务. Now chat.on_task_event posts the result of every routine run into the persistent chat as a result
message (the full summary + its files), and a brief-like routine (name or prompt: 早报 / 晨报 / 日报 / 简报 / 新闻 /
资讯 / brief / news / digest) gets a 今日天气 section first.

Weather: Open-Meteo (no key) forecast + air quality for one city: now (temperature, feels-like, humidity, wind and
gusts in 级, cloud, visibility, pressure), today's high / low, rain chance / amount / hours and when the rain comes,
UV, sunrise / sunset, AQI + PM2.5 / PM10, a 3-hourly outlook, and short tips (带伞 / 防晒 / 温差 / 大风 / 空气).

kv "weather": {"city": "海口", "lat": .., "lon": .., "name": "..", "routines": "auto" | "all" | "off"}
Env: AIW_WEATHER_CITY (default 海口) when kv has no city; AIW_ROUTINE_TO_CHAT=0 keeps routine results out of the chat.
GET / PUT /api/settings/weather; GET /api/weather/today (the text the brief gets).
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("aiw.weather")
KV = "weather"
TZ = "Asia/Shanghai"
BRIEF_RE = re.compile(r"早报|晨报|日报|简报|晚报|新闻|资讯|要闻|热点|brief|news|digest|morning", re.I)
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FC_URL = "https://api.open-meteo.com/v1/forecast"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WMO = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "冻雾", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
       56: "冻毛毛雨", 57: "冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪",
       75: "大雪", 77: "米雪", 80: "阵雨", 81: "中阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪", 95: "雷阵雨",
       96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹"}
WEEK = "一二三四五六日"


def cfg(store) -> dict:
    c = dict(store.kv_get(KV, {}) or {})
    c.setdefault("city", os.environ.get("AIW_WEATHER_CITY") or "海口")
    if c.get("routines") not in ("auto", "all", "off"):
        c["routines"] = "auto"
    return c


def save(store, body: dict) -> dict:
    c = cfg(store)
    if "city" in body:
        city = str(body.get("city") or "").strip()[:40]
        if city and city != c.get("city"):
            c.update(city=city, lat=None, lon=None, name=None)   # look the new city up again
    for k in ("lat", "lon"):
        if body.get(k) not in (None, ""):
            c[k] = float(body[k])
    if body.get("routines") in ("auto", "all", "off"):
        c["routines"] = body["routines"]
    store.kv_set(KV, c)
    return c


def wants_weather(store, name: str, prompt: str) -> bool:
    mode = cfg(store).get("routines")
    if mode == "off":
        return False
    return mode == "all" or bool(BRIEF_RE.search(f"{name or ''} {prompt or ''}"))


def wind_level(kmh: float) -> int:
    ms = (kmh or 0) / 3.6
    for lv, top in enumerate((0.2, 1.5, 3.3, 5.4, 7.9, 10.7, 13.8, 17.1, 20.7, 24.4, 28.4, 32.6)):
        if ms <= top:
            return lv
    return 12


def wind_dir(deg) -> str:
    if deg is None:
        return ""
    return ["北", "东北", "东", "东南", "南", "西南", "西", "西北"][int(((float(deg) % 360) + 22.5) // 45) % 8] + "风"


def uv_cn(uv) -> str:
    uv = float(uv or 0)
    return "弱" if uv < 3 else "中等" if uv < 6 else "强" if uv < 8 else "很强" if uv < 11 else "极强"


def aqi_cn(a) -> str:
    a = float(a or 0)
    return "优" if a <= 50 else "良" if a <= 100 else "轻度污染" if a <= 150 else "中度污染" if a <= 200 else \
        "重度污染" if a <= 300 else "严重污染"


async def _get(client, url: str, params: dict) -> dict:
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5}


async def locate(store, client) -> tuple[float, float, str]:
    """The city's coordinates. 「海口」 alone finds a village in 云南 first, so the name is also tried with 市 and the
    biggest place wins (capital > province seat > prefecture …, then population). 「定安,海南」 limits to a province."""
    c = cfg(store)
    if c.get("lat") is not None and c.get("lon") is not None:
        return float(c["lat"]), float(c["lon"]), c.get("name") or c["city"]
    bits = re.split(r"\s*[,，/ ]\s*", c["city"].strip(), maxsplit=1)
    city, region = bits[0], (bits[1] if len(bits) > 1 else "")
    names = [city] + ([city + "市", city + "县"] if not re.search(r"[市县区州盟旗]$", city) else [])
    hits = []
    for n in names:
        try:
            d = await _get(client, GEO_URL, {"name": n, "count": 10, "language": "zh", "format": "json"})
        except Exception as e:  # noqa: BLE001
            log.info("geocode %s: %s", n, e)
            continue
        hits += d.get("results") or []
    if region:
        hits = [h for h in hits if region.rstrip("省市") in f"{h.get('admin1') or ''}{h.get('admin2') or ''}"] or hits
    if not hits:
        raise ValueError(f"找不到城市「{c['city']}」")
    hit = sorted(hits, key=lambda h: (_RANK.get(h.get("feature_code") or "", 9), -(h.get("population") or 0)))[0]
    name = hit.get("name") or city
    c.update(lat=hit["latitude"], lon=hit["longitude"], name=name)
    store.kv_set(KV, c)
    return float(hit["latitude"]), float(hit["longitude"]), name


async def fetch(store, timeout: float = 12.0) -> dict:
    """The raw numbers for today (forecast + air quality)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        lat, lon, name = await locate(store, client)
        fc = await _get(client, FC_URL, {
            "latitude": lat, "longitude": lon, "timezone": TZ, "forecast_days": 2, "wind_speed_unit": "kmh",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,cloud_cover,precipitation,"
                       "pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,"
                     "precipitation_sum,precipitation_hours,precipitation_probability_max,wind_speed_10m_max,"
                     "wind_gusts_10m_max,wind_direction_10m_dominant,uv_index_max,sunrise,sunset",
            "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,wind_speed_10m,relative_humidity_2m"})
        aq = {}
        try:
            aq = await _get(client, AQ_URL, {"latitude": lat, "longitude": lon, "timezone": TZ,
                                             "current": "us_aqi,pm2_5,pm10,ozone"})
        except Exception as e:  # noqa: BLE001  air quality is a bonus
            log.info("air quality: %s", e)
    return {"name": name, "fc": fc, "aq": aq}


def _hhmm(iso: str) -> str:
    return (iso or "")[11:16]


def render(data: dict, now: Optional[datetime] = None) -> str:
    """The 今日天气 section (markdown) from fetch()'s numbers."""
    now = now or datetime.now(ZoneInfo(TZ))
    fc, aq, name = data.get("fc") or {}, data.get("aq") or {}, data.get("name") or ""
    cur, day, hr = fc.get("current") or {}, fc.get("daily") or {}, fc.get("hourly") or {}
    today = now.strftime("%Y-%m-%d")
    di = (day.get("time") or [today]).index(today) if today in (day.get("time") or []) else 0

    def dv(k, default=None):
        v = day.get(k) or []
        return v[di] if di < len(v) and v[di] is not None else default

    code = dv("weather_code", cur.get("weather_code"))
    tmax, tmin = dv("temperature_2m_max"), dv("temperature_2m_min")
    amax, amin = dv("apparent_temperature_max"), dv("apparent_temperature_min")
    lines = [f"### 今日天气 · {name} · {now.month}月{now.day}日 周{WEEK[now.weekday()]}"]
    head = f"**{WMO.get(code, '—')}**"
    if tmax is not None and tmin is not None:
        head += f"，{round(tmin)}~{round(tmax)}°C"
        if amax is not None and amin is not None:
            head += f"（体感 {round(amin)}~{round(amax)}°C）"
    lines.append(head)
    if cur:
        w = cur.get("wind_speed_10m") or 0
        s = (f"- 现在 {_hhmm(cur.get('time')) or now.strftime('%H:%M')}：{round(cur.get('temperature_2m', 0))}°C，"
             f"体感 {round(cur.get('apparent_temperature', 0))}°C，{WMO.get(cur.get('weather_code'), '')}，"
             f"湿度 {round(cur.get('relative_humidity_2m', 0))}%，{wind_dir(cur.get('wind_direction_10m'))} {wind_level(w)}级"
             f"（{round(w)} km/h，阵风 {round(cur.get('wind_gusts_10m') or 0)} km/h）")
        extra = []
        if cur.get("cloud_cover") is not None:
            extra.append(f"云量 {round(cur['cloud_cover'])}%")
        if cur.get("visibility") is not None:
            extra.append(f"能见度 {cur['visibility'] / 1000:.1f} km")
        if cur.get("pressure_msl") is not None:
            extra.append(f"气压 {round(cur['pressure_msl'])} hPa")
        lines.append(s + ("，" + "，".join(extra) if extra else ""))
    # rain: chance, amount, hours, and when it comes (hours of today with chance >= 40%)
    pp, ps, ph = dv("precipitation_probability_max", 0) or 0, dv("precipitation_sum", 0) or 0, dv("precipitation_hours", 0) or 0
    times = hr.get("time") or []
    wet = [_hhmm(t) for i, t in enumerate(times) if t.startswith(today) and t[11:13] >= now.strftime("%H")
           and ((hr.get("precipitation_probability") or [0] * len(times))[i] or 0) >= 40]
    rain = f"- 降水：概率 {round(pp)}%，预计 {ps:.1f} mm"
    if ph:
        rain += f"，约 {round(ph)} 小时"
    if wet:
        rain += f"，集中在 {wet[0]}–{wet[-1][:2]}:59"
    lines.append(rain)
    wmax = dv("wind_speed_10m_max", 0) or 0
    lines.append(f"- 风：{wind_dir(dv('wind_direction_10m_dominant'))}，最大 {wind_level(wmax)}级（{round(wmax)} km/h），"
                 f"阵风最大 {round(dv('wind_gusts_10m_max', 0) or 0)} km/h")
    uv = dv("uv_index_max")
    sun = f"- 紫外线：{uv:.0f}（{uv_cn(uv)}）" if uv is not None else "- 紫外线：—"
    if dv("sunrise"):
        sun += f"；日出 {_hhmm(dv('sunrise'))}，日落 {_hhmm(dv('sunset'))}"
    lines.append(sun)
    ac = aq.get("current") or {}
    if ac.get("us_aqi") is not None:
        lines.append(f"- 空气：AQI {round(ac['us_aqi'])}（{aqi_cn(ac['us_aqi'])}），PM2.5 {round(ac.get('pm2_5') or 0)} µg/m³，"
                     f"PM10 {round(ac.get('pm10') or 0)} µg/m³")
    # 3-hourly outlook for the rest of today
    slots = []
    for i, t in enumerate(times):
        if not t.startswith(today) or int(t[11:13]) % 3 or t[11:13] < now.strftime("%H"):
            continue
        g = lambda k: (hr.get(k) or [None] * len(times))[i]   # noqa: E731
        slots.append(f"{t[11:13]}时 {round(g('temperature_2m') or 0)}° {WMO.get(g('weather_code'), '')} "
                     f"雨{round(g('precipitation_probability') or 0)}%")
    if slots:
        lines.append("- 分时段：" + " ｜ ".join(slots))
    tips = []
    if pp >= 50 or ps >= 1:
        tips.append("出门带伞")
    if uv is not None and uv >= 6:
        tips.append("注意防晒")
    if tmax is not None and tmin is not None and tmax - tmin >= 8:
        tips.append(f"早晚温差 {round(tmax - tmin)}°C，注意增减衣物")
    if wind_level(dv("wind_gusts_10m_max", 0) or 0) >= 7:
        tips.append("阵风较大，远离广告牌和高处物品")
    if tmax is not None and tmax >= 35:
        tips.append("高温，注意补水")
    if tmin is not None and tmin <= 5:
        tips.append("气温低，注意保暖")
    if ac.get("us_aqi") and ac["us_aqi"] > 100:
        tips.append("空气较差，户外可戴口罩")
    if tips:
        lines.append("- 提示：" + "；".join(tips))
    return "\n".join(lines)


_CACHE: dict = {}


WTTR_URL = "https://wttr.in/{q}"


_EN = {"sunny": "晴", "clear": "晴", "partly cloudy": "多云", "cloudy": "多云", "overcast": "阴", "mist": "薄雾",
       "fog": "雾", "freezing fog": "冻雾", "patchy rain nearby": "局部有雨", "patchy rain possible": "可能有零星小雨",
       "patchy light drizzle": "零星毛毛雨", "light drizzle": "毛毛雨", "patchy light rain": "零星小雨",
       "light rain": "小雨", "light rain shower": "小阵雨", "moderate rain": "中雨", "moderate rain at times": "间歇中雨",
       "heavy rain": "大雨", "heavy rain at times": "间歇大雨", "moderate or heavy rain shower": "中到大阵雨",
       "torrential rain shower": "暴雨", "thundery outbreaks possible": "可能有雷雨", "thundery outbreaks in nearby": "附近有雷雨",
       "patchy light rain with thunder": "零星雷阵雨", "moderate or heavy rain with thunder": "中到大雷雨",
       "light snow": "小雪", "moderate snow": "中雪", "heavy snow": "大雪", "light sleet": "小雨夹雪"}


def _zh(t: str) -> str:
    return _EN.get(t.lower().strip(), t) if t and t.isascii() else t


async def fetch_wttr(store, timeout: float = 15.0) -> str:
    """v3.9.52: the second source (wttr.in, no key) when Open-Meteo cannot be reached from the API host."""
    c = cfg(store)
    q = f"{c['lat']},{c['lon']}" if c.get("lat") is not None and c.get("lon") is not None else c["city"]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        d = await _get(client, WTTR_URL.format(q=q), {"format": "j1", "lang": "zh"})
    return render_wttr(d, c.get("name") or c["city"])


def render_wttr(d: dict, name: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(ZoneInfo(TZ))
    cur = (d.get("current_condition") or [{}])[0]
    day = (d.get("weather") or [{}])[0]
    desc = lambda x: _zh(((x.get("lang_zh") or x.get("weatherDesc") or [{}])[0].get("value") or "").strip())   # noqa: E731
    lines = [f"### 今日天气 · {name} · {now.month}月{now.day}日 周{WEEK[now.weekday()]}"]
    head = f"**{desc(cur) or '—'}**"
    if day.get("mintempC") and day.get("maxtempC"):
        head += f"，{day['mintempC']}~{day['maxtempC']}°C"
    lines.append(head)
    if cur:
        kmh = float(cur.get("windspeedKmph") or 0)
        lines.append(f"- 现在：{cur.get('temp_C')}°C，体感 {cur.get('FeelsLikeC')}°C，湿度 {cur.get('humidity')}%，"
                     f"{wind_dir(cur.get('winddirDegree'))} {wind_level(kmh)}级（{round(kmh)} km/h），"
                     f"云量 {cur.get('cloudcover')}%，能见度 {cur.get('visibility')} km，气压 {cur.get('pressure')} hPa")
    hours = day.get("hourly") or []
    if hours:
        pp = max(int(h.get("chanceofrain") or 0) for h in hours)
        mm = sum(float(h.get("precipMM") or 0) for h in hours)
        lines.append(f"- 降水：概率 {pp}%，预计 {mm:.1f} mm")
        gust = max(float(h.get("WindGustKmph") or 0) for h in hours)
        lines.append(f"- 阵风最大 {round(gust)} km/h（{wind_level(gust)}级）")
        slots = [f"{int(h.get('time') or 0) // 100:02d}时 {h.get('tempC')}° {desc(h)} 雨{h.get('chanceofrain')}%"
                 for h in hours if int(h.get("time") or 0) // 100 >= now.hour - 2]
        if slots:
            lines.append("- 分时段：" + " ｜ ".join(slots))
    uv = day.get("uvIndex")
    ast = (day.get("astronomy") or [{}])[0]
    if uv is not None:
        lines.append(f"- 紫外线：{uv}（{uv_cn(float(uv))}）；日出 {ast.get('sunrise', '')}，日落 {ast.get('sunset', '')}")
    return "\n".join(lines)


async def today_text(store) -> str:
    """The section, or one line saying the weather could not be read (never raises). Cached 20 minutes per city.
    v3.9.52: Open-Meteo twice (a 7:30 run can meet a slow first connection), then wttr.in."""
    key = cfg(store).get("city")
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 1200:
        return hit[1]
    errs = []
    for attempt in range(2):
        try:
            txt = render(await fetch(store, timeout=12.0 + 8 * attempt))
            _CACHE[key] = (time.time(), txt)
            return txt
        except Exception as e:  # noqa: BLE001
            errs.append(f"open-meteo {type(e).__name__}")
            log.warning("weather (open-meteo, try %d): %s", attempt + 1, e)
    try:
        txt = await fetch_wttr(store)
        _CACHE[key] = (time.time(), txt)
        return txt
    except Exception as e:  # noqa: BLE001
        errs.append(f"wttr {type(e).__name__}")
        log.warning("weather (wttr): %s", e)
    return f"### 今日天气 · {key}\n（天气暂时没读到：{'；'.join(errs)}）"


def to_html(md: str) -> str:
    """The weather section as an HTML block for the top of a brief page (v3.9.52: the 早报 html carries it too)."""
    import html as _h
    out, items = [], []
    for ln in (md or "").splitlines():
        t = _h.escape(ln.strip())
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        if t.startswith("### "):
            out.append(f'<h2 style="margin:0 0 8px;font-size:20px">{t[4:]}</h2>')
        elif t.startswith("- "):
            items.append(f"<li>{t[2:]}</li>")
        elif t:
            out.append(f'<p style="margin:4px 0;font-size:17px">{t}</p>')
    if items:
        out.append('<ul style="margin:6px 0 0;padding-left:20px;line-height:1.7">' + "".join(items) + "</ul>")
    return ('<section id="aiw-weather" style="margin:16px auto;max-width:960px;padding:16px 20px;border-radius:14px;'
            'background:#eef6ff;color:#123;font-family:-apple-system,system-ui,sans-serif">' + "".join(out) + "</section>")


def inject_html(path: str, md: str) -> bool:
    """Put the weather section right after <body> of a brief page (once). True when the file was changed."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            page = f.read()
        if 'id="aiw-weather"' in page:
            return False
        block = to_html(md)
        m = re.search(r"<body[^>]*>", page, re.I)
        page = page[:m.end()] + block + page[m.end():] if m else block + page
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        return True
    except OSError as e:
        log.warning("weather into %s: %s", path, e)
        return False


def routine_for(store, task) -> Optional[dict]:
    """The routine a task was fired by (its last_task_id or its thread)."""
    try:
        rows = store.all("routines", order="created_at ASC") or []
    except Exception:  # noqa: BLE001
        return None
    tid, thr = getattr(task, "id", None), getattr(task, "thread_id", None)
    return next((r for r in rows if r.get("last_task_id") == tid), None) or \
        next((r for r in rows if thr and r.get("thread_id") == thr), None)
