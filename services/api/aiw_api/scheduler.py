"""Background scheduler: routines (scheduled agent tasks) and page watches
(change / text appears / price below a threshold), with de-duplicated alerts
and exponential back-off on fetch failures."""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time

import httpx

from . import cron

log = logging.getLogger("aiw.scheduler")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 AIW-Watch"
PRICE_RE = re.compile(r"(?:[$¥￥€£]|USD|CNY|RMB)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:元|块|USD|CNY)")


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def find_price(text: str) -> float | None:
    for m in PRICE_RE.finditer(text):
        v = (m.group(1) or m.group(2) or "").replace(",", "")
        try:
            f = float(v)
        except ValueError:
            continue
        if f > 0:
            return f
    return None


def evaluate(watch: dict, text: str) -> tuple[bool, str, str]:
    """-> (triggered, new_value, message). Only a transition triggers, so alerts never repeat."""
    mode, val = watch.get("mode") or "change", (watch.get("value") or "").strip()
    h = hashlib.sha256(text.encode()).hexdigest()
    if mode == "contains":
        present = val.lower() in text.lower()
        was = watch.get("last_value") == "present"
        return present and not was, "present" if present else "absent", f"页面出现了「{val}」"
    if mode == "price_below":
        price = find_price(text)
        if price is None:
            return False, "no-price", ""
        limit = float(val or 0)
        prev = watch.get("last_value")
        prev_below = prev not in (None, "", "no-price") and float(prev) < limit
        return price < limit and not prev_below, str(price), f"价格 {price:g} 低于 {limit:g}"
    first = not watch.get("last_hash")
    return (not first and h != watch.get("last_hash")), h, "页面内容有变化"


class Scheduler:
    def __init__(self, store, sessions, notifier, tick: float = 20.0):
        self.store, self.sessions, self.notifier, self.tick = store, sessions, notifier, tick
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            try:
                await self.run_due()
            except Exception as e:  # noqa: BLE001
                log.warning("scheduler tick failed: %s", e)
            await asyncio.sleep(self.tick)

    async def run_due(self, now: float | None = None):
        now = now or time.time()
        for r in self.store.all("routines", "enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,), "next_run ASC"):
            await self.fire_routine(r, now)
        for w in self.store.all("watches", "enabled=1 AND (next_check IS NULL OR next_check<=?)", (now,), "created_at ASC"):
            await self.check_watch(w, now)

    async def fire_routine(self, r: dict, now: float | None = None) -> dict:
        now = now or time.time()
        try:
            nxt = cron.next_run(r["schedule"], now)
        except ValueError:
            nxt = None
        self.store.update("routines", r["id"], last_run=now, next_run=nxt)
        res = await self.sessions.submit(r["prompt"], r.get("machine_id") or None, thread_id=r.get("thread_id"),
                                         source="routine", title=f"⏰ {r['name']}")
        self.store.update("routines", r["id"], last_task_id=res["task"]["id"], thread_id=res["task"]["thread_id"])
        return res

    async def check_watch(self, w: dict, now: float | None = None) -> dict:
        now = now or time.time()
        interval = max(5, int(w.get("interval_min") or 60)) * 60
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as c:
                r = await c.get(w["url"])
                r.raise_for_status()
            text = page_text(r.text)
            hit, value, msg = evaluate(w, text)
            fields = dict(last_checked=now, next_check=now + interval, fail_count=0, last_error=None, last_value=value)
            if (w.get("mode") or "change") == "change":
                fields["last_hash"] = value
                fields["last_value"] = text[:200]
            if hit:
                fields["triggered_at"] = now
                await self.notifier.notify("watch", f"追踪提醒：{w['name']}", f"{msg}\n{w['url']}", link=w["url"])
            self.store.update("watches", w["id"], **fields)
            return {"ok": True, "triggered": hit, "value": fields["last_value"]}
        except Exception as e:  # noqa: BLE001
            fails = int(w.get("fail_count") or 0) + 1
            backoff = interval * (2 ** min(fails, 5))
            self.store.update("watches", w["id"], last_checked=now, next_check=now + backoff, fail_count=fails,
                              last_error=f"{type(e).__name__}: {e}"[:300])
            if fails == 3:
                await self.notifier.notify("watch_error", f"追踪失败：{w['name']}", f"连续 3 次读取失败：{e}", link=w["url"])
            return {"ok": False, "error": str(e)}
