"""v3.9.92 工作日志（work_items）：每个任务结束、每轮聊天之后记一笔「今天做了什么、说过什么、还欠什么」，给每日回顾用。

一行 = 一件事（表 work_items）：
* day        AIW_TZ 里的日期（YYYY-MM-DD），ts 是记下来的时间
* source     task（任务结束：成功、失败、停止、中断都记）或 chat（一轮聊天，交给电脑的不算，任务自己会记）
* title / ask / outcome   一句话标题、用户原话（截断）、结果一句话
* deliverables  交付物（文件、链接、提交）  open_items  还没做完的事
* claims    我们说过、之后可能会变的话，每条带「怎么核对」：
            {"text": "...", "how_to_check": {"kind": "git|pypi|github_release|url|watch|none", "target": "...",
             "expect": "..."}, "check_after_ts": 时间戳}
* status    open（有没做完的事或待核对的话）/ closed / expired（推了两次没人理，不再推）
* last_pushed_ts / push_count / push_hash（推过的发现的 hash，JSON 列表，去重用）/ superseded_by（后来的那一条）

写入
* sessions.py 在任务结束（和 lessons.learn_from_task 同一处，但不只成功）、chat.after_exchange 在 memory.learn 之后，
  都放到后台跑，绝不挡回复。
* 小模型读这一轮，输出 JSON；没有模型或模型失败时按任务状态 / 原话兜底（失败 / 停止 → 没做完；结果里的
  github release / pypi 链接 → 可核对的话）。
* 密码、验证码、token 一律不存（memory.looks_secret）：像密钥的字段直接丢掉，用户原话像密钥则整条不记。
* 同一个任务继续（继续 / 重做）时，旧的那条 superseded_by 新的一条并关掉。

读取：items(day)、open_before(day)、recent(days) 给 review.py；GET /api/worklog?day= 在 review.py 里挂。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from . import cron
from .memory import looks_secret, norm

log = logging.getLogger("aiw.worklog")

FINAL = ("succeeded", "failed", "stopped", "interrupted", "cancelled", "timeout")
UNFINISHED = ("failed", "stopped", "interrupted", "timeout")
CHECK_KINDS = ("git", "pypi", "github_release", "url", "watch", "none")
MAX_LIST = 6

EXTRACT_PROMPT = """你是工作日志整理员（WORKLOG）。读下面这一件事（一个任务的结果，或用户和助理的一轮聊天），整理成一行 JSON：
{"title": "十个字左右的标题", "outcome": "结果一句话", "deliverables": ["交付物：文件名、链接、提交，没有就空"],
 "open_items": ["还没做完、等用户、下一步要做的事，没有就空"],
 "claims": [{"text": "助理说过的、以后可能变的事实（版本号、最新发布、某页面写着什么、价格）",
             "how_to_check": {"kind": "git|pypi|github_release|url|watch|none", "target": "仓库地址 / 包名 / owner/repo / 网址 / 追踪 id",
                              "expect": "当时的值，如版本号、commit 前缀、页面应包含的文字"},
             "check_after_hours": 12}]}
只写真的说过的话，最多 4 条 claims；不确定怎么核对就 kind=none。不要写密码、验证码、token、密钥。"""


def day_of(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), cron.tz()).strftime("%Y-%m-%d")


def clip(s: Any, n: int = 200) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def clean(s: Any, n: int = 200) -> str:
    """One field, clipped; '' when it looks like a secret."""
    t = clip(s, n)
    return "" if looks_secret(t) else t


def ensure_table(store) -> None:
    store._x("""CREATE TABLE IF NOT EXISTS work_items (id TEXT PRIMARY KEY, day TEXT, ts REAL, task_id TEXT, thread_id TEXT,
        source TEXT, title TEXT, ask TEXT, outcome TEXT, task_status TEXT, deliverables TEXT, open_items TEXT, claims TEXT,
        status TEXT DEFAULT 'open', last_pushed_ts REAL, push_count INTEGER DEFAULT 0, push_hash TEXT DEFAULT '[]',
        superseded_by TEXT DEFAULT '', responded_ts REAL)""")
    store._x("CREATE INDEX IF NOT EXISTS work_items_day ON work_items(day)")


def parse_json(text: str) -> dict:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


GH_REL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/releases/tag/([\w.\-]+)", re.I)
PYPI_RE = re.compile(r"pypi\.org/project/([\w.\-]+)/([0-9][\w.\-]*)", re.I)
PIN_RE = re.compile(r"\b([A-Za-z][\w\-]{1,40})==([0-9][\w.\-]*)")
TITLE_STRIP = re.compile(r"^\s*(【技能[:：][^】]*】|⏰\s*)")


def fallback_claims(text: str, ts: float) -> list[dict]:
    """Claims the text carries in a checkable form: a GitHub release tag link, a PyPI version link, a pin name==x.y."""
    out, seen = [], set()
    for repo, tag in GH_REL_RE.findall(text or ""):
        k = ("gh", repo.lower())
        if k not in seen:
            seen.add(k)
            out.append({"text": f"{repo} 的发布是 {tag}", "how_to_check": {"kind": "github_release", "target": repo, "expect": tag},
                        "check_after_ts": ts + 12 * 3600})
    for name, ver in PYPI_RE.findall(text or "") + PIN_RE.findall(text or ""):
        k = ("pypi", name.lower())
        if k not in seen:
            seen.add(k)
            out.append({"text": f"{name} 的版本是 {ver}", "how_to_check": {"kind": "pypi", "target": name, "expect": ver},
                        "check_after_ts": ts + 12 * 3600})
    return out[:4]


def norm_claims(raw: Any, ts: float) -> list[dict]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        text = clean(c.get("text"), 240)
        if not text:
            continue
        h = c.get("how_to_check") if isinstance(c.get("how_to_check"), dict) else {}
        kind = str(h.get("kind") or "none").strip().lower()
        kind = kind if kind in CHECK_KINDS else "none"
        target, expect = clean(h.get("target"), 300), clean(h.get("expect"), 200)
        if kind != "none" and not target:
            kind = "none"
        try:
            hrs = float(c.get("check_after_hours", 12))
        except (TypeError, ValueError):
            hrs = 12.0
        after = float(c["check_after_ts"]) if isinstance(c.get("check_after_ts"), (int, float)) else ts + max(0.0, hrs) * 3600
        out.append({"text": text, "how_to_check": {"kind": kind, "target": target, "expect": expect}, "check_after_ts": after})
    return out[:4]


def _list(raw: Any, n: int = 200) -> list[str]:
    vals = raw if isinstance(raw, list) else ([raw] if raw else [])
    return [x for x in (clean(v, n) for v in vals) if x][:MAX_LIST]


class Worklog:
    def __init__(self, store, llm: Optional[Callable[[str], Any]] = None):
        self.store, self.llm = store, llm
        self._lock = asyncio.Lock()
        ensure_table(store)

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _out(r: dict) -> dict:
        d = dict(r)
        for k in ("deliverables", "open_items", "claims", "push_hash"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except ValueError:
                d[k] = []
        return d

    def get(self, iid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM work_items WHERE id=?", (iid,))
        return self._out(r[0]) if r else None

    def items(self, day: Optional[str] = None) -> list[dict]:
        rows = self.store._x("SELECT * FROM work_items WHERE day=? ORDER BY ts ASC", (day or day_of(),))
        return [self._out(r) for r in rows]

    def open_before(self, day: str, days: int = 14) -> list[dict]:
        since = time.time() - days * 86400
        rows = self.store._x("SELECT * FROM work_items WHERE day<? AND status='open' AND (superseded_by IS NULL OR superseded_by='') "
                             "AND ts>=? ORDER BY ts ASC", (day, since))
        return [self._out(r) for r in rows]

    def recent(self, days: int = 14, now: Optional[float] = None) -> list[dict]:
        since = (now or time.time()) - days * 86400
        return [self._out(r) for r in self.store._x("SELECT * FROM work_items WHERE ts>=? ORDER BY ts ASC", (since,))]

    def update(self, iid: str, **fields) -> None:
        for k in ("deliverables", "open_items", "claims", "push_hash"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        self.store.update("work_items", iid, **fields)

    def add(self, *, source: str, title: str, ask: str = "", outcome: str = "", task_id: Optional[str] = None,
            thread_id: Optional[str] = None, task_status: str = "", deliverables=(), open_items=(), claims=(),
            ts: Optional[float] = None) -> Optional[dict]:
        ts = ts or time.time()
        title = clean(TITLE_STRIP.sub("", title or ""), 60) or clean(TITLE_STRIP.sub("", ask or ""), 40)
        if not title:
            return None
        deliverables, open_items = _list(list(deliverables)), _list(list(open_items))
        claims = norm_claims(list(claims), ts)
        unfinished = task_status in UNFINISHED
        status = "open" if (open_items or unfinished or any(c["how_to_check"]["kind"] != "none" for c in claims)) else "closed"
        iid = f"wi-{uuid.uuid4().hex[:10]}"
        self.store._x("INSERT INTO work_items (id, day, ts, task_id, thread_id, source, title, ask, outcome, task_status, "
                      "deliverables, open_items, claims, status, push_count, push_hash, superseded_by) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'[]','')",
                      (iid, day_of(ts), ts, task_id, thread_id, source, title, clean(ask, 300), clean(outcome, 300),
                       task_status, json.dumps(deliverables, ensure_ascii=False), json.dumps(open_items, ensure_ascii=False),
                       json.dumps(claims, ensure_ascii=False), status))
        if task_id:   # a continuation of the same task: the older line is replaced by this one
            from . import continuation as _cont
            try:
                root = _cont.root_id(self.store, task_id)
            except Exception:  # noqa: BLE001
                root = task_id
            ids = {task_id, root} | {r["id"] for r in (self.store.continuations(root) if root else [])}
            ph = ",".join("?" * len(ids))
            self.store._x(f"UPDATE work_items SET superseded_by=?, status='closed' WHERE id<>? AND task_id IN ({ph}) "
                          "AND (superseded_by IS NULL OR superseded_by='')", (iid, iid, *ids))
        return self.get(iid)

    # ------------------------------------------------------------------ extraction
    async def _extract(self, body: str) -> dict:
        if not self.llm:
            return {}
        try:
            out = await asyncio.wait_for(self.llm("small").complete(EXTRACT_PROMPT, body[:6000], 700), 90)
        except Exception as e:  # noqa: BLE001
            log.info("worklog extract: %s", e)
            return {}
        return parse_json(out)

    async def record_task(self, task: dict, status: str, result: Any = "", files: list = ()) -> Optional[dict]:
        """Background, after a task ended (any final status)."""
        try:
            prompt = TITLE_STRIP.sub("", str(task.get("prompt") or ""))
            res = str(result or "")
            if looks_secret(prompt) and len(prompt) < 40:
                return None
            ts = time.time()
            body = f"任务：{clip(prompt, 1500)}\n状态：{status}\n结果：{clip(res, 3000)}\n文件：{', '.join(map(str, files or []))[:600]}"
            async with self._lock:
                d = await self._extract(body)
            fb_open = [f"{'没做完' if status != 'stopped' else '被停止'}：{clip(res, 120) or clip(prompt, 60)}"] if status in UNFINISHED else []
            deliv = d.get("deliverables") if d.get("deliverables") else [str(f).rsplit("/", 1)[-1] for f in files or []]
            claims = d.get("claims") if isinstance(d.get("claims"), list) else fallback_claims(res, ts)
            opens = _list(d.get("open_items")) + [x for x in fb_open if not d.get("open_items")]
            return self.add(source="task", title=d.get("title") or prompt[:40], ask=prompt,
                            outcome=d.get("outcome") or (clip(res, 160) if res else STATUS_CN.get(status, status)),
                            task_id=task.get("id"), thread_id=task.get("thread_id"), task_status=status,
                            deliverables=deliv, open_items=opens, claims=claims, ts=ts)
        except Exception as e:  # noqa: BLE001  never breaks the task end
            log.warning("worklog task: %s", e)
            return None

    async def record_chat(self, user: dict, replies: list[dict]) -> Optional[dict]:
        """Background, after a chat exchange. Exchanges that started / continued a task are left to the task end."""
        try:
            text = str((user or {}).get("text") or "").strip()
            if len(text) < 4 or looks_secret(text):
                return None
            if any((r or {}).get("kind") in ("task", "ask", "confirm") for r in replies or []):
                return None
            if any(((r or {}).get("data") or {}).get("memory") or ((r or {}).get("data") or {}).get("review_prefs")
                   for r in replies or []):
                return None
            reply = "\n".join(str((r or {}).get("text") or "")[:1500] for r in replies or [] if (r or {}).get("kind") in ("text", "task_result"))
            ts = time.time()
            async with self._lock:
                d = await self._extract(f"用户：{clip(text, 1500)}\n助理：{clip(reply, 3000)}")
            if not d and not fallback_claims(reply, ts):
                # no model: keep a light line (repeated asks show up in 猜你想要), nothing to check
                return self.add(source="chat", title=text[:40], ask=text, outcome=clip(reply, 160), ts=ts)
            claims = d.get("claims") if isinstance(d.get("claims"), list) else fallback_claims(reply, ts)
            return self.add(source="chat", title=d.get("title") or text[:40], ask=text, outcome=d.get("outcome") or clip(reply, 160),
                            deliverables=d.get("deliverables") or [], open_items=d.get("open_items") or [], claims=claims, ts=ts)
        except Exception as e:  # noqa: BLE001
            log.warning("worklog chat: %s", e)
            return None


STATUS_CN = {"succeeded": "已完成", "failed": "没完成", "stopped": "已停止", "interrupted": "被中断", "cancelled": "已取消",
             "timeout": "超时"}


def same_ask(a: str, b: str) -> bool:
    from .memory import similarity
    na, nb = norm(a), norm(b)
    return bool(na and nb) and (na == nb or similarity(a, b) >= 0.6)
