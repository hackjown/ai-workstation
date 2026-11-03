"""Notifications: stored in the DB (bell in the web console), fanned out to open
consoles over /ws/events, and optionally pushed to the phone via Bark (iOS),
ntfy, or a generic webhook configured in Settings.

3.8 deep links: every push carries the item it is about, so tapping it opens Aiw right there.
  path (deep_path):  task/<task_id>            a task finished / failed / needs you (confirm, question, form)
                     takeover/<machine>?task=<id>  a takeover request (opens the task in takeover)
                     monitor/<watch_id>        a price / page watch alert or failure
                     chat[?m=<message id>]     briefings, 小红书运营 notices, tests, anything else
  Bark `url`:  aiw://<path> (the iOS app, which registers the aiw scheme) by default; with link mode "web"
               (设置 › 通知 or AIW_PUSH_LINK=web) the web console instead: <public url>/open/<path>, which the
               server redirects to /?open=<path> for the web app to route. Public url: 设置 › 通知 or AIW_PUBLIC_URL.
  Bark `group` by subject (任务 / 需要你 / 价格监控 / 小红书 / Aiw), `icon` = <public url>/icon-180.png when a public
  url is set, `level=timeSensitive` for things waiting on you. ntfy gets the same link as Click, webhooks as fields.
  3.9.0 Bark `sound`: a spoken (female voice) ringtone per kind of push, e.g. 请授权 for a confirmation. The .caf files
  are in apps/web/sounds/ (served at <server>/sounds/<name>.caf); upload them once in Bark › 设置 › 铃声 › 上传铃声.
  设置 › 通知 "bark_sound": "off" (or AIW_BARK_SOUND=0) sends no sound parameter (Bark's default chime).
  3.9.17: the uploaded voice ringtones sit in Bark's Library/Sounds with iOS data protection "complete", so iOS cannot
  read them while the phone is LOCKED: a locked phone got the push with no sound at all (no fallback chime). Default
  is now "builtin": a sound bundled inside Bark (always readable) per kind; "voice" keeps the spoken ringtones (rings
  only while the phone is unlocked); "off" sends none. "bark_critical": "on" sends confirm / input as critical alerts
  (ring even in silent mode, volume "bark_volume" 1-10).
  3.9.17 delivery: Bark is called with POST JSON (title / body in the body, not the URL path: a "/" in a news
  summary became a path separator and the push was dropped), the reply is checked (HTTP 200 and code 200), one retry,
  and every attempt is kept in kv "push_log" (GET /api/settings/notify/log). A scheduled routine's result is
  timeSensitive, so iOS 定时推送摘要 / 专注模式 does not hold it back."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("aiw.notify")


TASK_KINDS = ("done", "failed", "confirm", "input")
WAITING_KINDS = ("confirm", "input")
WATCH_KINDS = ("watch", "watch_error")
GROUPS = {"done": "任务", "failed": "任务", "confirm": "需要你", "input": "需要你", "watch": "价格监控",
          "watch_error": "价格监控", "xhs_warning": "小红书", "xhs_login": "小红书"}


# push -> the Bark ringtone (file name without .caf in apps/web/sounds/), and what it says
SOUNDS = {
    "shouquan": ("aiw-shouquan", "请授权"),             # confirm: approve an action
    "tianxie": ("aiw-tianxie", "请填写资料"),           # input: a form to fill
    "jieguan": ("aiw-jieguan", "请接管桌面"),           # input: a takeover (captcha, login)
    "kazhu": ("aiw-kazhu", "任务卡住了，请处理"),        # input: a stuck task waiting for 继续 / 停止
    "huida": ("aiw-huida", "有问题需要你回答"),          # input: the agent asks a question (and its reminder)
    "wancheng": ("aiw-wancheng", "任务完成"),
    "shibai": ("aiw-shibai", "任务失败了"),
    "jiangjia": ("aiw-jiangjia", "价格提醒"),           # watch: a price / page watch fired
    "jiankong": ("aiw-jiankong", "监控出错了"),         # watch_error
    "xiaohongshu": ("aiw-xiaohongshu", "小红书需要你处理"),
    "tixing": ("aiw-tixing", "小秘提醒"),               # anything else (briefings, tests)
}


# 3.9.17: sounds bundled in the Bark app (they play on a locked phone), per voice key
BUILTIN = {"shouquan": "alarm", "tianxie": "multiwayinvitation", "jieguan": "alarm", "kazhu": "alarm",
           "huida": "multiwayinvitation", "wancheng": "glass", "shibai": "descent", "jiangjia": "bell",
           "jiankong": "descent", "xiaohongshu": "bell", "tixing": "newsflash"}


def sound_mode(cfg: dict) -> str:
    """"builtin" (default) | "voice" | "off". Old values: "on" -> builtin, "0"/"false" -> off."""
    if os.environ.get("AIW_BARK_SOUND", "1") == "0":
        return "off"
    v = str(cfg.get("bark_sound") or os.environ.get("AIW_BARK_SOUND_MODE") or "builtin").strip().lower()
    if v in ("off", "0", "false", "none"):
        return "off"
    return "voice" if v == "voice" else "builtin"


def builtin_sound_for(kind: str, title: str = "", path: str = "") -> str:
    voice = sound_for(kind, title, path)
    key = next((k for k, (n, _t) in SOUNDS.items() if n == voice), "tixing")
    return BUILTIN[key]


def sound_for(kind: str, title: str = "", path: str = "") -> str:
    """The Bark sound for a push, from its kind (and, for "input", what it is waiting for)."""
    if kind == "confirm":
        key = "shouquan"
    elif kind == "input":
        key = ("jieguan" if path.startswith("takeover/") or "接管" in title else
               "tianxie" if "填写" in title or "资料" in title else
               "kazhu" if "卡住" in title else "huida")
    else:
        key = {"done": "wancheng", "failed": "shibai", "watch": "jiangjia", "watch_error": "jiankong",
               "xhs_warning": "xiaohongshu", "xhs_login": "xiaohongshu"}.get(kind, "tixing")
    return SOUNDS[key][0]


def deep_path(kind: str, task_id: Optional[str] = None, watch_id: Optional[str] = None,
              message_id: Optional[str] = None) -> str:
    """The in-app place a notification is about (no scheme): see the module docstring."""
    if kind in TASK_KINDS and task_id:
        return f"task/{quote(task_id, safe='')}"
    if kind in WATCH_KINDS and watch_id:
        return f"monitor/{quote(watch_id, safe='')}"
    return "chat" + (f"?m={quote(message_id, safe='')}" if message_id else "")


def link_settings(cfg: dict) -> tuple[str, str]:
    """(mode "app" | "web", public url without a trailing slash; "" when not set)."""
    pub = (cfg.get("public_url") or os.environ.get("AIW_PUBLIC_URL") or "").strip().rstrip("/")
    mode = (cfg.get("link_mode") or os.environ.get("AIW_PUSH_LINK") or "app").strip().lower()
    if mode not in ("app", "web"):
        mode = "app"
    if mode == "web" and not pub:
        mode = "app"   # no address to open in a browser: the app link is the only one that can work
    return mode, pub


def app_url(path: str) -> str:
    return "aiw://" + path


def web_url(pub: str, path: str) -> Optional[str]:
    return f"{pub}/open/{path}" if pub else None


def bark_params(title: str, kind: str, path: str, cfg: dict, level: Optional[str] = None) -> dict:
    """url / group / icon / level / sound for one push (shared by the POST body and the GET fallback)."""
    mode, pub = link_settings(cfg)
    params = {"url": web_url(pub, path) if mode == "web" else app_url(path), "group": GROUPS.get(kind, "Aiw")}
    if pub:
        params["icon"] = f"{pub}/icon-180.png"
    if level:
        params["level"] = level
    elif kind in WAITING_KINDS:
        params["level"] = "timeSensitive"
        if str(cfg.get("bark_critical") or "").lower() in ("on", "1", "true"):
            params["level"] = "critical"
            params["volume"] = str(max(1, min(10, int(cfg.get("bark_volume") or 5))))
    sm = sound_mode(cfg)
    if sm == "voice":
        params["sound"] = sound_for(kind, title, path)
    elif sm == "builtin":
        params["sound"] = builtin_sound_for(kind, title, path)
    return params


def bark_request(bark: str, title: str, body: str, kind: str, path: str, cfg: dict,
                 level: Optional[str] = None) -> tuple[str, dict]:
    """The Bark GET (fallback): <key url>?title=..&body=.. with url / group / icon / level / sound. Title and body
    are query parameters, never path segments (a "/" in them broke the route)."""
    params = {"title": title, "body": body[:500] or " ", **bark_params(title, kind, path, cfg, level)}
    return bark.rstrip("/"), params


def bark_post(bark: str, title: str, body: str, kind: str, path: str, cfg: dict,
              level: Optional[str] = None) -> tuple[str, dict]:
    """3.9.17 the Bark POST: <key url> with a JSON body (any text, any length up to 1000 chars)."""
    return bark.rstrip("/"), {"title": title, "body": (body or " ")[:1000], **bark_params(title, kind, path, cfg, level)}


def _bark_ok(r) -> bool:
    if r.status_code != 200:
        return False
    try:
        return int((r.json() or {}).get("code", 200)) == 200
    except Exception:  # noqa: BLE001  a self-hosted proxy may answer plain text
        return True


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
                     push: bool = True, deep: Optional[str] = None, watch_id: Optional[str] = None,
                     message_id: Optional[str] = None, level: Optional[str] = None) -> dict:
        """deep: the in-app path when the caller knows better (a takeover); else built from kind + ids. A watch alert
        (kind watch / watch_error) finds its watch by the page link when watch_id is not given."""
        if not deep:
            if kind in WATCH_KINDS and not watch_id and link:
                try:
                    r = self.store._x("SELECT id FROM watches WHERE url=? ORDER BY created_at DESC LIMIT 1", (link,))
                    watch_id = r[0]["id"] if r else None
                except Exception:  # noqa: BLE001
                    watch_id = None
            deep = deep_path(kind, task_id, watch_id, message_id)
        n = self.store.insert("notifications", {"kind": kind, "title": title, "body": (body or "")[:2000],
                                                "task_id": task_id, "thread_id": thread_id, "link": link, "read": 0})
        n = {**n, "deep": deep}
        await self.broadcast({"kind": "notification", "notification": n})
        if push:
            asyncio.create_task(self._push(title, body or "", kind, deep, level=level))
        return n

    def _log_push(self, entry: dict):
        try:
            import time as _t
            logs = (self.store.kv_get("push_log", []) or [])[-49:]
            logs.append({"at": _t.time(), **entry})
            self.store.kv_set("push_log", logs)
        except Exception as e:  # noqa: BLE001
            log.warning("push log: %s", e)

    async def _bark(self, c, bark: str, title: str, body: str, kind: str, deep: str, cfg: dict, level=None) -> bool:
        """POST JSON, check the reply, retry once (POST then the GET form); every attempt goes to push_log."""
        last = ""
        for attempt in range(2):
            try:
                u, payload = bark_post(bark, title, body, kind, deep, cfg, level)
                r = await c.post(u, json=payload)
                if _bark_ok(r):
                    self._log_push({"kind": kind, "title": title[:60], "ok": True, "via": "post", "try": attempt + 1,
                                    "sound": payload.get("sound"), "level": payload.get("level")})
                    return True
                last = f"POST {r.status_code} {r.text[:200]}"
                u, params = bark_request(bark, title, body, kind, deep, cfg, level)
                r = await c.get(u, params=params)
                if _bark_ok(r):
                    self._log_push({"kind": kind, "title": title[:60], "ok": True, "via": "get", "try": attempt + 1,
                                    "sound": params.get("sound"), "level": params.get("level")})
                    return True
                last += f" | GET {r.status_code} {r.text[:200]}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            if attempt == 0:
                await asyncio.sleep(3)
        log.warning("bark push failed: %s (%s)", last, title)
        self._log_push({"kind": kind, "title": title[:60], "ok": False, "error": last[:400]})
        return False

    async def _push(self, title: str, body: str, kind: str = "", deep: str = "chat", level: Optional[str] = None):
        cfg = self.store.kv_get("notify", {}) or {}
        bark = cfg.get("bark_url") or (self.secrets.get("bark_url") if self.secrets else None)
        ntfy = cfg.get("ntfy_url")
        hook = cfg.get("webhook_url")
        mode, pub = link_settings(cfg)
        click = web_url(pub, deep) if mode == "web" else app_url(deep)
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                if bark:
                    await self._bark(c, bark, title, body, kind, deep, cfg, level)
                if ntfy:
                    await c.post(ntfy, params={"title": title}, headers={"Click": click}, content=body[:2000].encode())
                if hook:
                    await c.post(hook, json={"title": title, "body": body, "kind": kind, "deep": deep,
                                             "app_url": app_url(deep), "web_url": web_url(pub, deep), "url": click})
        except Exception as e:  # noqa: BLE001
            log.warning("push failed: %s", e)
