"""Schedules for routines: 5-field cron ("0 9 * * 1-5"), "every 30m" / "every 2h",
or "daily 09:00". Evaluated in AIW_TZ (default Asia/Shanghai)."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("AIW_TZ", "Asia/Shanghai"))


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron field out of range: {spec}")
        out.update(range(a, b + 1, step))
    return out


def validate(schedule: str) -> str:
    next_run(schedule, datetime.now(tz()).timestamp())
    return schedule.strip()


def next_run(schedule: str, after_ts: float) -> float:
    s = (schedule or "").strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|minutes?|h|hours?)", s)
    if m:
        n = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        if n < 5:
            raise ValueError("minimum interval is 5 minutes")
        return after_ts + n * 60
    m = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", s)
    if m:
        s = f"{int(m.group(2))} {int(m.group(1))} * * *"
    parts = s.split()
    if len(parts) != 5:
        raise ValueError("schedule must be cron (5 fields), 'every 30m' or 'daily 09:00'")
    mins, hours, dom, mon, dow = (_field(parts[0], 0, 59), _field(parts[1], 0, 23), _field(parts[2], 1, 31),
                                  _field(parts[3], 1, 12), _field(parts[4].replace("7", "0"), 0, 6))
    t = datetime.fromtimestamp(after_ts, tz()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if (t.minute in mins and t.hour in hours and t.day in dom and t.month in mon
                and (t.weekday() + 1) % 7 in dow):
            return t.timestamp()
        t += timedelta(minutes=1)
    raise ValueError("schedule never fires")
