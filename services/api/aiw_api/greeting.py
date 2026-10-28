"""球球播报 (2026-10-28, v2 home): GET /api/greeting, what 球球 says on the home screen when there is nothing new.

Web (home.js) and iOS (HomeChat.swift) show the items one at a time as speech bubbles around 球球 (a new one every
~4.5 s); 球球's face follows each item's ``mood`` (an Emotion Ball id, mate-map.js) and tapping a bubble runs its
``action``. Built from what the server already has, no model call, so it is instant and costs nothing:

  P0 a task waiting on the user (a question / confirmation / takeover)   mood 35  → open the task
  P1 a task that did not finish (last 48 h, not retried since)          mood 34  → 继续 (retry)
  P2 a task still running                                               mood 32  → open the task
  P3 a task finished today                                              mood 33  → open the task
  P4 routines that run later today                                      mood 39  → 例程
  P5 where the last chat left off (the rolling chat summary)            mood 30  → prompt 接着…
  P9 a greeting for the time of day with the user's form of address     mood 02  → focus the input

Action shapes: {"type": "thread", "thread_id"} · {"type": "retry", "task_id", "thread_id"} · {"type": "view", "view"}
· {"type": "prompt", "text"} (put text in the input) · {"type": "send", "text"} · {"type": "focus"}.
``chips`` are the quick actions above the input (the top items' short labels plus 随便聊聊).
"""
from __future__ import annotations

import re
import time
from typing import Optional

TZ = 8 * 3600   # Asia/Shanghai, like the rest of the server's user-facing times
FAILED = ("failed", "stopped", "interrupted")
WAITING = ("waiting_confirm", "waiting_input", "takeover")
RUNNING = ("running", "queued", "paused")
HIDDEN_SOURCES = ("xhs", "watch_order", "dispatch")   # background sessions the user did not start from the chat

ADDR_RE = re.compile(r"(?:称呼|叫我|喊我|称为|称他为|称她为|叫他|叫她)\s*[：:为是]?\s*[「“\"']?([^\s，。,.；;」”\"'！!]{1,8})")


def _local(ts: float) -> time.struct_time:
    return time.gmtime(ts + TZ)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", _local(ts))


def address(store, memory=None) -> str:
    """The user's form of address: profile name (设置 › 你和记忆), else a 称呼 the memory learned."""
    prof = store.kv_get("profile", {}) or {}
    name = str(prof.get("name") or "").strip()
    if name:
        return name[:12]
    rows = []
    try:
        if memory is not None:
            rows = [m.get("text") or "" for m in memory.items(kind="profile")[:20]]
        else:
            rows = [r["text"] or "" for r in store._x(
                "SELECT text FROM memories WHERE kind='profile' AND (superseded IS NULL OR superseded=0) ORDER BY created_at DESC LIMIT 20")]
    except Exception:  # noqa: BLE001  older table without kind
        rows = []
    for t in rows:
        m = ADDR_RE.search(t)
        if m:
            return m.group(1)
    return ""


def _title(store, t: dict) -> str:
    th = store.thread(t["thread_id"]) if t.get("thread_id") else None
    title = (th or {}).get("title") or ""
    if not title or title == "新对话":
        title = re.sub(r"【技能[:：][^】]*】", "", t.get("prompt") or "").strip().splitlines()[0:1]
        title = title[0] if title else "任务"
    return title[:18] + ("…" if len(title) > 18 else "")


def _clip(s: str, n: int = 40) -> str:
    s = " ".join(str(s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def build(store, chat=None, memory=None, now: Optional[float] = None) -> dict:
    now = now or time.time()
    addr = address(store, memory)
    call = f"{addr}，" if addr else ""
    items: list[dict] = []

    def add(prio, text, mood, action, chip=None):
        items.append({"id": f"g{len(items)}", "prio": prio, "text": text, "mood": mood, "action": action,
                      **({"chip": chip} if chip else {})})

    rows = [t for t in store.recent_tasks(120) if not t.get("parent_id") and (t.get("source") or "") not in HIDDEN_SOURCES]
    newest: dict = {}   # thread -> its newest task (a retry continues the same thread)
    for t in rows:
        k = t.get("thread_id") or t["id"]
        if k not in newest:
            newest[k] = t
    for t in newest.values():
        st, age = t.get("status"), now - (t.get("updated_at") or t.get("created_at") or 0)
        title = _title(store, t)
        tid = t.get("thread_id")
        if st in WAITING:
            what = {"waiting_confirm": "等你确认一下", "waiting_input": "在等你回答", "takeover": "在等你接管桌面"}[st]
            add(0, f"{call}「{title}」{what}，点我去看看。", "35", {"type": "thread", "thread_id": tid}, f"👀 看看 {_clip(title, 8)}")
        elif st in FAILED and age < 48 * 3600:
            why = _clip(t.get("result") or "", 28)
            when = "昨晚" if _day(now) != _day(t.get("created_at") or now) and _local(t.get("created_at") or now).tm_hour >= 18 else ""
            add(1, f"{when}「{title}」没做完" + (f"：{why}" if why else "") + "。要我接着做吗？", "34",
                {"type": "retry", "task_id": t["id"], "thread_id": tid}, f"🔁 继续 {_clip(title, 8)}")
        elif st in RUNNING:
            add(2, f"「{title}」正在电脑上做，点我看进度。", "32", {"type": "thread", "thread_id": tid}, f"🖥 看 {_clip(title, 8)}")
        elif st == "succeeded" and _day(t.get("updated_at") or 0) == _day(now):
            res = _clip(t.get("result") or "", 40)
            add(3, f"{call}「{title}」做完了" + (f"：{res}" if res else "。"), "33", {"type": "thread", "thread_id": tid},
                f"📄 看 {_clip(title, 8)}")
    # routines that still run today
    today = _day(now)
    try:
        routines = store.all("routines", "enabled=1 AND next_run IS NOT NULL", order="next_run ASC", limit=20)
    except Exception:  # noqa: BLE001
        routines = []
    later = [r for r in routines if r.get("next_run") and r["next_run"] > now and _day(r["next_run"]) == today]
    if later:
        r = later[0]
        more = f"，还有 {len(later) - 1} 个例程" if len(later) > 1 else ""
        add(4, f"今天 {time.strftime('%H:%M', _local(r['next_run']))} 我会去跑「{_clip(r.get('name'), 12)}」{more}。", "39",
            {"type": "view", "view": "routines"}, "⏰ 今天的例程")
    # where the chat left off
    summ = {}
    try:
        summ = (chat.summary() if chat is not None else store.kv_get("chat_summary", {})) or {}
    except Exception:  # noqa: BLE001
        summ = {}
    text = re.sub(r"^\s*(摘要[:：])?", "", str(summ.get("text") or "")).strip()
    if text:
        first = re.split(r"(?<=[。！？!?])", text)[0]
        first = re.sub(r"^用户", "你", first)
        add(5, f"上次聊到：{_clip(first, 44)}", "30", {"type": "prompt", "text": "接着上次的聊："}, "💬 接着上次聊")
    # always: a greeting
    h = _local(now).tm_hour
    part = "夜深了" if h < 5 else "早上好" if h < 11 else "中午好" if h < 13 else "下午好" if h < 18 else "晚上好"
    add(9, f"{addr + '，' if addr else ''}{part}！有什么要我做的，直接说就行。", "02", {"type": "focus"})
    items.sort(key=lambda x: x["prio"])
    items = items[:8]
    chips, seen = [], set()
    for it in items:
        c = it.get("chip")
        if c and c not in seen and len(chips) < 3:
            seen.add(c)
            chips.append({"label": c, "action": it["action"]})
    chips.append({"label": "💬 随便聊聊", "action": {"type": "focus"}})
    agent = chat.agent_name() if chat is not None else ((store.kv_get("profile", {}) or {}).get("agent_name") or "小秘")
    return {"address": addr, "agent": agent, "items": items, "chips": chips, "at": now}


def mount(app, store, chat, memory, auth) -> None:
    from fastapi import Depends

    @app.get("/api/greeting", dependencies=[Depends(auth)])
    async def greeting():
        return build(store, chat, memory)
