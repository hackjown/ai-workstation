"""Notifications: stored in the DB (bell in the web console), fanned out to open
consoles over /ws/events, and optionally pushed to the phone via Bark (iOS),
ntfy, or a generic webhook configured in Settings."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

log = logging.getLogger("aiw.notify")


class Notifier:
    def __init__(self, store, secrets=None):
        self.store, self.secrets = store, secrets
        self.subscribers: set = set()   # WebSockets of open consoles

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.subscribers):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    async def notify(self, kind: str, title: str, body: str = "", task_id=None, thread_id=None, link=None,
                     push: bool = True) -> dict:
        n = self.store.insert("notifications", {"kind": kind, "title": title, "body": (body or "")[:2000],
                                                "task_id": task_id, "thread_id": thread_id, "link": link, "read": 0})
        await self.broadcast({"kind": "notification", "notification": n})
        if push:
            asyncio.create_task(self._push(title, body or ""))
        return n

    async def _push(self, title: str, body: str):
        cfg = self.store.kv_get("notify", {}) or {}
        bark = cfg.get("bark_url") or (self.secrets.get("bark_url") if self.secrets else None)
        ntfy = cfg.get("ntfy_url")
        hook = cfg.get("webhook_url")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                if bark:
                    await c.get(f"{bark.rstrip('/')}/{quote(title, safe='')}/{quote(body[:500] or ' ', safe='')}?group=AIW")
                if ntfy:
                    await c.post(ntfy, params={"title": title}, content=body[:2000].encode())
                if hook:
                    await c.post(hook, json={"title": title, "body": body})
        except Exception as e:  # noqa: BLE001
            log.warning("push failed: %s", e)
