"""v3.9.92 每日回顾里的手机能力：明天的日程和提醒事项、日程开始前的提醒、每晚的健康小结。

走的是已有的设备桥（device_bridge.py 的 DeviceHub，WS /ws/device）：手机 App 在 hello 里报它的 capabilities
（只有用户在 aiw App › 设置 › 设备能力 里打开的才会报），服务器用 hub.call(tool, args) 让手机执行
calendar_list / reminders_list / health_read。没有新协议。

* 只用已授权的能力：known() 里没有 calendar.read / reminders.read / health.read 就不碰。没授权但有用时，
  回顾消息末尾最多提示一次（kv proactive_prefs.cap_hint_shown；PUT /api/review/prefs 设回 false 可再提示）。
* 手机不在线：不排队、不发「打开 App」推送，直接跳过，日志里写原因（log.info "device skip: …"）。
* 日程 / 提醒事项（20:00 回顾）：明天的日程（时间冲突、早于 prefs.early_hour 点开始的）、过期没完成的提醒、
  明天到期的提醒，作为「明天」一节进回顾，算可处理的事。
* 日程开始前提醒：每 AIW_EVENT_SYNC_MIN 分钟（默认 30）在手机在线时同步未来 36 小时的日程到 kv event_reminders，
  按 事件 id + 开始时间 去重；日程改了时间就改提醒时间（只重排一次，不会重复推）；日程被删就丢掉。到点
  （prefs.event_lead_min，默认 15 分钟，0 = 关）用已有的 notify（Bark，timeSensitive，点开回到聊天）推一条。
* 健康小结：独立定时（prefs.health_summary_time，默认 21:30 AIW_TZ；"off" 或 health_enabled=false 关掉）。读当天
  步数、昨晚睡眠、体能训练（分钟、千卡）、心率均值，以及手机支持时的活动能量 / 静息心率 / HRV；和用户自己最近
  7 天的平均比，发一条简短小结 + 最多一条建议。表 health_daily 只存每天的汇总数字，不存原始样本；小结由规则
  生成，不调用任何模型、不发给第三方。只说趋势，不给医疗建议；看着不寻常的只说「值得留意」。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Optional

from . import cron

log = logging.getLogger("aiw.device_review")

EVENTS_KV = "event_reminders"
SYNC_KV = "event_reminders_synced"
HEALTH_NEXT_KV = "health_next_ts"
CALL_TTL = 20.0
USEFUL = {"calendar.read": "读取日历", "reminders.read": "读取提醒事项", "health.read": "读取健康"}
DEFAULTS = {"event_lead_min": 15, "early_hour": 8, "health_enabled": True, "health_summary_time": "21:30",
            "cap_hint_shown": False}


def dprefs(store) -> dict:
    from .review import PREFS_KV
    p = store.kv_get(PREFS_KV, {}) or {}
    return {k: p.get(k, v) for k, v in DEFAULTS.items()}


def set_dpref(store, **kw) -> None:
    from .review import PREFS_KV
    p = store.kv_get(PREFS_KV, {}) or {}
    p.update(kw)
    store.kv_set(PREFS_KV, p)


def tz():
    return cron.tz()


def parse_iso(s) -> Optional[float]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz())
    return d.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz()).isoformat(timespec="seconds")


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz()).strftime("%H:%M")


def day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz())
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def ensure_tables(store) -> None:
    store._x("""CREATE TABLE IF NOT EXISTS health_daily (day TEXT PRIMARY KEY, steps INTEGER, sleep_min INTEGER,
        active_kcal INTEGER, workout_min INTEGER, workout_kcal INTEGER, hr_avg REAL, resting_hr REAL, hrv REAL, updated_at REAL)""")


class DeviceReview:
    def __init__(self, store, hub=None, notifier=None, chat=None):
        self.store, self.hub, self.notifier, self.chat = store, hub, notifier, chat
        ensure_tables(store)

    # ---------------------------------------------------------------- capabilities
    def granted(self, cap: str) -> bool:
        return bool(self.hub) and any(cap in (d.get("caps") or []) for d in self.hub.known())

    def online(self, cap: str) -> bool:
        return bool(self.hub) and self.hub._pick(cap) is not None

    async def call(self, tool: str, args: dict) -> Optional[dict]:
        """One device call, only when a device with the capability is online; None (and a log line) otherwise."""
        from .device_bridge import TOOLS
        cap = TOOLS[tool][0]
        if not self.granted(cap):
            log.info("device skip: %s not granted", cap)
            return None
        if not self.online(cap):
            log.info("device skip: no device online for %s", cap)
            return None
        try:
            r = await self.hub.call(tool, args, ttl=CALL_TTL)
        except Exception as e:  # noqa: BLE001
            log.info("device skip: %s failed: %s", tool, e)
            return None
        if not r.get("ok") or not isinstance(r.get("result"), dict):
            log.info("device skip: %s -> %s", tool, str(r.get("error"))[:120])
            return None
        return r["result"]

    def hint(self) -> str:
        """At most once ever (until reset): the one useful capability that is not granted."""
        if dprefs(self.store)["cap_hint_shown"] or not self.hub or not self.hub.known():
            return ""
        missing = [c for c in ("calendar.read", "reminders.read") if not self.granted(c)]
        if not missing:
            return ""
        set_dpref(self.store, cap_hint_shown=True)
        names = "、".join(f"「{USEFUL[c]}」" for c in missing)
        return f"小提示：想让每晚回顾带上明天的日程和提醒，可以在手机 aiw App › 设置 › 设备能力 里打开{names}。只提示这一次。"

    # ---------------------------------------------------------------- tomorrow (20:00 review)
    async def tomorrow(self, now: Optional[float] = None) -> list[dict]:
        """Action items for tomorrow: conflicts, early starts, overdue / due reminders. [] when nothing is granted / online."""
        now = now or time.time()
        p = dprefs(self.store)
        start = day_start(now) + 86400
        end = start + 86400
        out: list[dict] = []
        ev = await self.call("calendar_list", {"start": iso(start), "end": iso(end)}) if self.granted("calendar.read") else None
        events = []
        for e in (ev or {}).get("events") or []:
            s, t = parse_iso(e.get("start")), parse_iso(e.get("end"))
            if s is None or e.get("all_day"):
                continue
            events.append({**e, "_s": s, "_e": t or s})
        events.sort(key=lambda e: e["_s"])
        for i, a in enumerate(events):
            for b in events[i + 1:]:
                if b["_s"] < a["_e"]:
                    out.append({"kind": "conflict", "key": f"conflict:{a['id']}:{b['id']}:{a['_s']}:{b['_s']}",
                                "text": f"明天 {hm(a['_s'])}「{a.get('title')}」和 {hm(b['_s'])}「{b.get('title')}」时间重叠"})
        if events and datetime.fromtimestamp(events[0]["_s"], tz()).hour < int(p["early_hour"]):
            e = events[0]
            out.append({"kind": "early", "key": f"early:{e['id']}:{e['_s']}",
                        "text": f"明天 {hm(e['_s'])} 就有「{e.get('title')}」，比平时早" + (f"（{e['location']}）" if e.get("location") else "")})
        if events:
            out.append({"kind": "agenda", "key": f"agenda:{start}:{len(events)}:{events[0]['_s']}", "info": True,
                        "text": "明天的日程：" + "；".join(f"{hm(e['_s'])} {e.get('title')}" for e in events[:6]) +
                                ("…" if len(events) > 6 else "")})
        rem = await self.call("reminders_list", {}) if self.granted("reminders.read") else None
        for r in (rem or {}).get("reminders") or []:
            due = parse_iso(r.get("due"))
            if r.get("done") or due is None:
                continue
            if due < now:
                out.append({"kind": "overdue", "key": f"overdue:{r.get('id')}:{due}",
                            "text": f"提醒事项「{r.get('title')}」{datetime.fromtimestamp(due, tz()).strftime('%m-%d')} 就到期了，还没完成"})
            elif start <= due < end:
                out.append({"kind": "due", "key": f"due:{r.get('id')}:{due}", "text": f"提醒事项「{r.get('title')}」明天 {hm(due)} 到期"})
        return out

    # ---------------------------------------------------------------- pre-event reminders
    async def sync_events(self, now: Optional[float] = None) -> dict:
        """Upcoming 36 h of events -> kv event_reminders {id: {start, title, fire_at, sent, moved}}. Moves reschedule
        (once per move), deletions drop. None of this runs without calendar.read and an online device."""
        now = now or time.time()
        lead = int(dprefs(self.store)["event_lead_min"] or 0)
        ev = await self.call("calendar_list", {"start": iso(now), "end": iso(now + 36 * 3600)})
        if ev is None:
            return {"synced": False}
        self.store.kv_set(SYNC_KV, now)
        cur = self.store.kv_get(EVENTS_KV, {}) or {}
        seen, out = set(), {}
        stats = {"synced": True, "added": 0, "moved": 0, "dropped": 0}
        for e in ev.get("events") or []:
            s = parse_iso(e.get("start"))
            eid = str(e.get("id") or "")
            if not eid or s is None or e.get("all_day") or s <= now:
                continue
            seen.add(eid)
            old = cur.get(eid)
            rec = {"start": s, "title": str(e.get("title") or "日程")[:80], "location": str(e.get("location") or "")[:80],
                   "fire_at": s - lead * 60, "sent": False, "moved": 0}
            if old and abs(float(old["start"]) - s) < 1:
                rec.update(sent=old.get("sent", False), moved=old.get("moved", 0))
            elif old:
                rec["moved"] = int(old.get("moved") or 0) + 1   # a new start: a new reminder, the old one is gone
                stats["moved"] += 1
            else:
                stats["added"] += 1
            out[eid] = rec
        for eid, old in cur.items():   # gone from the calendar (cancelled / deleted) or already over
            if eid not in seen and float(old.get("start") or 0) > now:
                stats["dropped"] += 1
        self.store.kv_set(EVENTS_KV, out)
        return stats

    async def fire_due(self, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        lead = int(dprefs(self.store)["event_lead_min"] or 0)
        if lead <= 0:
            return []
        cur = self.store.kv_get(EVENTS_KV, {}) or {}
        fired = []
        for eid, r in cur.items():
            if r.get("sent") or now < float(r["fire_at"]) or now >= float(r["start"]):
                continue
            r["sent"] = True
            fired.append(eid)
            mins = max(1, round((float(r["start"]) - now) / 60))
            if self.notifier is not None:
                try:
                    await self.notifier.notify("event", f"{mins} 分钟后：{r['title']}",
                                               f"{hm(float(r['start']))} 开始" + (f" · {r['location']}" if r.get("location") else ""),
                                               deep="chat", level="timeSensitive")
                except Exception as e:  # noqa: BLE001
                    log.info("event reminder push: %s", e)
        if fired:
            self.store.kv_set(EVENTS_KV, cur)
        return fired

    # ---------------------------------------------------------------- health
    NEWER = ("active_energy", "resting_heart_rate", "hrv")

    async def _health(self, kind: str, start: float, end: float) -> Optional[dict]:
        unsupported = self.__dict__.setdefault("_unsupported", set())
        if kind in unsupported:
            return None
        r = await self.call("health_read", {"kind": kind, "start": iso(start), "end": iso(end)})
        if r is None and kind in self.NEWER:   # an app without this kind: do not ask again in this process
            unsupported.add(kind)
        return r

    async def read_day(self, day0: float, now: float) -> Optional[dict]:
        """Daily aggregates for the day starting at day0 (sleep = the night before it). None without health.read online."""
        end = min(day0 + 86400, now)
        steps = await self._health("steps", day0, end)
        if steps is None:
            return None
        row = {"steps": int(steps.get("steps") or 0)}
        sl = await self._health("sleep", day0 - 6 * 3600, day0 + 12 * 3600)
        row["sleep_min"] = int(sl["asleep_minutes"]) if sl and sl.get("asleep_minutes") else None
        wo = await self._health("workouts", day0, end)
        ws = (wo or {}).get("workouts") or []
        row["workout_min"] = sum(int(w.get("minutes") or 0) for w in ws) if wo is not None else None
        row["workout_kcal"] = sum(int(w.get("kcal") or 0) for w in ws) if wo is not None else None
        hr = await self._health("heart_rate", day0, end)
        row["hr_avg"] = round(float(hr["avg"]), 1) if hr and hr.get("count") else None
        # newer kinds: answered only by an app that supports them (else None)
        ae = await self._health("active_energy", day0, end)
        row["active_kcal"] = int(ae.get("kcal") or 0) if ae and ae.get("kcal") is not None else None
        rh = await self._health("resting_heart_rate", day0, end)
        row["resting_hr"] = float(rh["bpm"]) if rh and rh.get("bpm") else None
        hv = await self._health("hrv", day0, end)
        row["hrv"] = float(hv["ms"]) if hv and hv.get("ms") else None
        return row

    def save_day(self, day: str, row: dict) -> None:
        cols = ("steps", "sleep_min", "active_kcal", "workout_min", "workout_kcal", "hr_avg", "resting_hr", "hrv")
        vals = [row.get(c) for c in cols]
        self.store._x(f"INSERT INTO health_daily (day, {','.join(cols)}, updated_at) VALUES (?,{','.join('?' * len(cols))},?) "
                      f"ON CONFLICT(day) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols) + ",updated_at=excluded.updated_at",
                      (day, *vals, time.time()))

    def baseline(self, day: str) -> dict:
        rows = self.store._x("SELECT * FROM health_daily WHERE day<? ORDER BY day DESC LIMIT 7", (day,))
        out = {"days": len(rows)}
        for c in ("steps", "sleep_min", "active_kcal", "workout_min", "hr_avg", "resting_hr", "hrv"):
            v = [float(r[c]) for r in rows if r.get(c) is not None]
            out[c] = sum(v) / len(v) if len(v) >= 3 else None
        return out

    async def health_summary(self, now: Optional[float] = None) -> Optional[dict]:
        """Read today, backfill missing baseline days, compare, post + push. None when off / not granted / offline."""
        now = now or time.time()
        p = dprefs(self.store)
        if not p["health_enabled"] or str(p["health_summary_time"]).lower() == "off":
            log.info("health summary: off")
            return None
        if not self.granted("health.read") or not self.online("health.read"):
            log.info("device skip: health.read %s", "not granted" if not self.granted("health.read") else "offline")
            return None
        d0 = day_start(now)
        today = datetime.fromtimestamp(d0, tz()).strftime("%Y-%m-%d")
        row = await self.read_day(d0, now)
        if row is None:
            return None
        self.save_day(today, row)
        have = {r["day"] for r in self.store._x("SELECT day FROM health_daily WHERE day<?", (today,))}
        for i in range(1, 8):   # a baseline from the user's own last 7 days
            ds = d0 - i * 86400
            dname = datetime.fromtimestamp(ds, tz()).strftime("%Y-%m-%d")
            if dname not in have:
                past = await self.read_day(ds, now)
                if past:
                    self.save_day(dname, past)
        base = self.baseline(today)
        text, tip = compose(row, base)
        msg = None
        if self.chat is not None:
            msg = self.chat.add("assistant", "text", text + (f"\n\n{tip}" if tip else ""), {"health_summary": {"day": today}})
        if self.notifier is not None:
            await self.notifier.notify("health", "今日健康小结", text[:300], deep=f"chat?m={msg['id']}" if msg else "chat",
                                       message_id=msg["id"] if msg else None)
        return {"day": today, "row": row, "baseline": base, "text": text, "tip": tip, "message": msg}

    # ---------------------------------------------------------------- scheduler
    async def tick(self, now: Optional[float] = None) -> None:
        if os.environ.get("AIW_REVIEW", "1") == "0":
            return
        now = now or time.time()
        try:
            every = float(os.environ.get("AIW_EVENT_SYNC_MIN", "30")) * 60
            if dprefs(self.store)["event_lead_min"] and self.granted("calendar.read") and \
                    now - float(self.store.kv_get(SYNC_KV, 0) or 0) >= every:
                await self.sync_events(now)
            await self.fire_due(now)
        except Exception as e:  # noqa: BLE001
            log.info("event reminders: %s", e)
        p = dprefs(self.store)
        t = str(p["health_summary_time"] or "off").lower()
        if not p["health_enabled"] or t == "off" or not self.granted("health.read"):
            return
        nxt = self.store.kv_get(HEALTH_NEXT_KV)
        if not nxt or (nxt and float(nxt) - now > 86400 + 60):
            self.store.kv_set(HEALTH_NEXT_KV, cron.next_run(f"daily {t}", now))
            return
        if now < float(nxt):
            return
        self.store.kv_set(HEALTH_NEXT_KV, cron.next_run(f"daily {t}", now))
        try:
            await self.health_summary(now)
        except Exception as e:  # noqa: BLE001
            log.info("health summary: %s", e)


def _pct(v, b) -> Optional[int]:
    if v is None or not b:
        return None
    return round((float(v) - b) / b * 100)


def _hmin(m) -> str:
    m = int(m)
    return f"{m // 60} 小时 {m % 60:02d} 分"


def compose(row: dict, base: dict) -> tuple[str, str]:
    """(one short summary, at most one suggestion). Trends against the user's own 7-day average only; no advice beyond
    habits, anything unusual is 'worth watching'."""
    parts = []

    def cmp(v, b):
        p = _pct(v, b)
        return "" if p is None else f"（7 天平均 {int(b):,}，{'+' if p >= 0 else ''}{p}%）"
    parts.append(f"今天 {row['steps']:,} 步{cmp(row['steps'], base.get('steps'))}")
    if row.get("sleep_min"):
        b = base.get("sleep_min")
        parts.append(f"昨晚睡了 {_hmin(row['sleep_min'])}" + (f"（平均 {_hmin(b)}）" if b else ""))
    if row.get("workout_min"):
        parts.append(f"训练 {row['workout_min']} 分钟" + (f" · {row['workout_kcal']} 千卡" if row.get("workout_kcal") else ""))
    if row.get("active_kcal"):
        parts.append(f"活动能量 {row['active_kcal']} 千卡{cmp(row['active_kcal'], base.get('active_kcal'))}")
    if row.get("resting_hr"):
        parts.append(f"静息心率 {row['resting_hr']:.0f}")
    if row.get("hrv"):
        parts.append(f"HRV {row['hrv']:.0f} ms")
    text = "🫀 **今日健康小结**\n" + " · ".join(parts)
    if base.get("days", 0) < 3:
        text += "\n（还在积累你自己的 7 天基线，过几天对比会更准）"
    tip = ""
    rh, brh = row.get("resting_hr"), base.get("resting_hr")
    hv, bhv = row.get("hrv"), base.get("hrv")
    sp, bsp = row.get("sleep_min"), base.get("sleep_min")
    st, bst = row.get("steps"), base.get("steps")
    if rh and brh and rh >= brh * 1.1:
        tip = f"静息心率比你最近 7 天平均高了一些（{brh:.0f} → {rh:.0f}），值得留意几天的变化。"
    elif hv and bhv and hv <= bhv * 0.8:
        tip = f"HRV 比你最近 7 天平均低一些（{bhv:.0f} → {hv:.0f} ms），值得留意。"
    elif sp and bsp and sp <= bsp * 0.85:
        tip = "昨晚睡得比你平时少，今晚可以早点休息。"
    elif st is not None and bst and st <= bst * 0.6:
        tip = "今天走得比你平时少不少，明天可以多走动一下。"
    return text, tip
