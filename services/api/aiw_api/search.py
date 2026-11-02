"""In-app search (v3.9.14): one SQLite FTS5 index over the chat, tasks and files, for the user and for Aiw.

What is indexed (kind / ref):
* chat     chat_messages rows with text (user and assistant), ref = message id
* task     tasks: the prompt (skill marker stripped) and the result text, ref = task id
* file     uploads (name, path) and artifacts (path) a task produced, ref = upload/artifact id

The index is a contentless copy kept up to date incrementally: every search first pulls rows changed since the last
mark (kv "search_marks"), so nothing has to hook the many places that write messages and tasks. A first search on an
old database back-fills everything in one go (a few thousand rows a second).

Chinese: FTS5's default tokenizer does not split CJK, so the table uses the trigram tokenizer (SQLite >= 3.34): any
substring of three or more characters matches. Shorter terms (two-character words such as 户型 or 京东) fall back to
LIKE on the same table. Without trigram support the same table is a plain table and every term uses LIKE.

Readers
* GET /api/search?q=&kind=chat|task|file&limit=  the search box on the web and in the iOS app
* session_search tool  the chat model and every task (sessions.py) can look up earlier conversations and tasks
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

log = logging.getLogger("aiw.search")

KINDS = ("chat", "task", "file")
KIND_CN = {"chat": "对话", "task": "任务", "file": "文件"}
MARK = re.compile(r"【技能[:：]\s*[^】]+】")
SECRETISH = re.compile(r"(密码|验证码|token|api[ _-]?key|私钥)\s*[:：=]\s*\S+", re.I)
BATCH = 2000


def _clean(text: Any, n: int = 4000) -> str:
    s = MARK.sub("", str(text or "")).strip()
    s = SECRETISH.sub(lambda m: m.group(1) + "：***", s)
    return s[:n]


def terms(q: str) -> list[str]:
    """Search words: quoted phrases stay whole, the rest split on spaces/punctuation."""
    q = (q or "").strip()[:200]
    out = [p for p in re.findall(r'"([^"]+)"', q) if p.strip()]
    q = re.sub(r'"[^"]*"', " ", q)
    out += [w for w in re.split(r"[\s,，、;；。.!！?？:：()（）\[\]【】]+", q) if w]
    seen, res = set(), []
    for w in out:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            res.append(w)
    return res[:8]


def snippet(text: str, words: list[str], width: int = 42) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    low = t.lower()
    pos = -1
    for w in words:
        pos = low.find(w.lower())
        if pos >= 0:
            break
    if pos < 0:
        return t[: width * 2] + ("…" if len(t) > width * 2 else "")
    a, b = max(0, pos - width), min(len(t), pos + width)
    return ("…" if a else "") + t[a:b] + ("…" if b < len(t) else "")


class Search:
    def __init__(self, store):
        self.store = store
        self.fts = True
        self._last_sync = 0.0
        self.ensure()

    # ------------------------------------------------------------------ table
    def ensure(self) -> None:
        try:
            self.store._x("CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
                          "title, body, kind UNINDEXED, ref UNINDEXED, ts UNINDEXED, extra UNINDEXED, tokenize='trigram')")
        except Exception as e:  # noqa: BLE001  an SQLite without fts5/trigram: plain table, LIKE only
            log.warning("fts5 trigram unavailable (%s): search uses LIKE", e)
            self.fts = False
            self.store._x("CREATE TABLE IF NOT EXISTS search_rows (title TEXT, body TEXT, kind TEXT, ref TEXT, "
                          "ts REAL, extra TEXT)")
            self.store._x("CREATE INDEX IF NOT EXISTS ix_search_rows_ref ON search_rows(kind, ref)")

    @property
    def table(self) -> str:
        return "search_fts" if self.fts else "search_rows"

    def _put(self, kind: str, ref: str, title: str, body: str, ts: float, extra: dict) -> None:
        self.store._x(f"DELETE FROM {self.table} WHERE kind=? AND ref=?", (kind, ref))
        if not (title or body):
            return
        self.store._x(f"INSERT INTO {self.table} (title, body, kind, ref, ts, extra) VALUES (?,?,?,?,?,?)",
                      (title, body, kind, ref, ts, json.dumps(extra, ensure_ascii=False)))

    def drop(self, kind: str, ref: str) -> None:
        self.store._x(f"DELETE FROM {self.table} WHERE kind=? AND ref=?", (kind, ref))

    # ------------------------------------------------------------------ incremental sync
    def _marks(self) -> dict:
        return self.store.kv_get("search_marks", {}) or {}

    def _tables(self) -> set:
        return {r["name"] for r in self.store._x("SELECT name FROM sqlite_master WHERE type='table'")}

    def sync(self, force: bool = False) -> int:
        """Index rows changed since the last mark. Cheap when nothing changed (one indexed query per source)."""
        if not force and time.time() - self._last_sync < 1.0:
            return 0
        self._last_sync = time.time()
        marks, have, n = self._marks(), self._tables(), 0
        if "chat_messages" in have:
            m = float(marks.get("chat") or 0)
            rows = self.store._x("SELECT id, role, kind, text, data, task_id, created_at, "
                                 "COALESCE(updated_at, created_at) AS ts FROM chat_messages "
                                 "WHERE COALESCE(updated_at, created_at) > ? ORDER BY ts LIMIT ?", (m, BATCH))
            for r in rows:
                text = _clean(r.get("text"))
                if r.get("kind") in ("task",) and not text:
                    try:
                        d = json.loads(r.get("data") or "{}")
                        text = _clean(d.get("title") or d.get("prompt") or "")
                    except ValueError:
                        text = ""
                who = "我" if r.get("role") == "user" else "Aiw"
                self._put("chat", r["id"], who, text, r.get("created_at") or r["ts"],
                          {"role": r.get("role"), "task_id": r.get("task_id")})
                m = max(m, r["ts"] or 0)
                n += 1
            marks["chat"] = m
        if "tasks" in have:
            m = float(marks.get("tasks") or 0)
            rows = self.store._x("SELECT id, prompt, result, status, thread_id, created_at, "
                                 "COALESCE(updated_at, created_at) AS ts FROM tasks "
                                 "WHERE COALESCE(updated_at, created_at) > ? ORDER BY ts LIMIT ?", (m, BATCH))
            for r in rows:
                prompt = _clean(r.get("prompt"), 2000)
                title = re.sub(r"\s+", " ", prompt)[:80]
                body = prompt + ("\n\n" + _clean(r.get("result"), 4000) if r.get("result") else "")
                self._put("task", r["id"], title, body, r.get("created_at") or r["ts"],
                          {"status": r.get("status"), "thread_id": r.get("thread_id")})
                m = max(m, r["ts"] or 0)
                n += 1
            marks["tasks"] = m
        if "uploads" in have:
            m = float(marks.get("uploads") or 0)
            rows = self.store._x("SELECT id, name, path, task_id, thread_id, created_at FROM uploads WHERE created_at > ? "
                                 "ORDER BY created_at LIMIT ?", (m, BATCH))
            for r in rows:
                self._put("file", "u:" + r["id"], r.get("name") or os.path.basename(r.get("path") or ""),
                          r.get("path") or "", r["created_at"],
                          {"task_id": r.get("task_id"), "thread_id": r.get("thread_id"), "upload_id": r["id"],
                           "path": r.get("path"), "source": "upload"})
                m = max(m, r["created_at"] or 0)
                n += 1
            marks["uploads"] = m
        if "artifacts" in have:
            m = float(marks.get("artifacts") or 0)
            rows = self.store._x("SELECT a.id, a.path, a.kind, a.created_at, s.task_id FROM artifacts a "
                                 "LEFT JOIN sessions s ON s.id = a.session_id WHERE a.created_at > ? "
                                 "ORDER BY a.created_at LIMIT ?", (m, BATCH))
            for r in rows:
                p = r.get("path") or ""
                if (r.get("kind") or "") == "screenshot" or re.search(r"\.(jpe?g|png)$", p, re.I) and "/shots/" in p:
                    m = max(m, r["created_at"] or 0)
                    continue
                self._put("file", "a:" + r["id"], os.path.basename(p), p, r["created_at"],
                          {"task_id": r.get("task_id"), "path": p, "source": "artifact"})
                m = max(m, r["created_at"] or 0)
                n += 1
            marks["artifacts"] = m
        if n:
            self.store.kv_set("search_marks", marks)
            if n >= BATCH:   # a big back-fill: take the next batch now
                self._last_sync = 0
                n += self.sync(force=True)
        return n

    # ------------------------------------------------------------------ query
    def search(self, q: str, kinds: Optional[list] = None, limit: int = 20, offset: int = 0,
               task_id: Optional[str] = None) -> dict:
        try:
            self.sync()
        except Exception as e:  # noqa: BLE001  a broken sync never blocks a search over what is indexed
            log.warning("search sync: %s", e)
        words = terms(q)
        if not words:
            return {"q": q, "results": [], "total": 0}
        kinds = [k for k in (kinds or KINDS) if k in KINDS] or list(KINDS)
        limit = max(1, min(int(limit or 20), 100))
        where, args = [f"kind IN ({','.join('?' * len(kinds))})"], list(kinds)
        long = [w for w in words if len(w) >= 3] if self.fts else []
        for w in words:
            if w not in long:
                where.append("(title LIKE ? OR body LIKE ?)")
                like = "%" + w.replace("%", "").replace("_", "") + "%"
                args += [like, like]
        if long:
            match = " ".join('"' + w.replace('"', '""') + '"' for w in long)
            where.insert(0, "search_fts MATCH ?")
            args.insert(0, match)
            order = "bm25(search_fts, 2.0, 1.0) + (strftime('%s','now') - ts) / 86400.0 / 365.0"
        else:
            order = "-ts"
        sql_where = " AND ".join(where)
        try:
            total = self.store._x(f"SELECT COUNT(*) AS n FROM {self.table} WHERE {sql_where}", tuple(args))[0]["n"]
            rows = self.store._x(f"SELECT title, body, kind, ref, ts, extra FROM {self.table} WHERE {sql_where} "
                                 f"ORDER BY {order} LIMIT ? OFFSET ?", tuple(args) + (limit, int(offset or 0)))
        except Exception as e:  # noqa: BLE001  a query FTS cannot parse: LIKE over every term instead
            log.info("fts query failed (%s), LIKE fallback", e)
            where, args = [f"kind IN ({','.join('?' * len(kinds))})"], list(kinds)
            for w in words:
                where.append("(title LIKE ? OR body LIKE ?)")
                args += ["%" + w + "%"] * 2
            sql_where = " AND ".join(where)
            total = self.store._x(f"SELECT COUNT(*) AS n FROM {self.table} WHERE {sql_where}", tuple(args))[0]["n"]
            rows = self.store._x(f"SELECT title, body, kind, ref, ts, extra FROM {self.table} WHERE {sql_where} "
                                 "ORDER BY ts DESC LIMIT ? OFFSET ?", tuple(args) + (limit, int(offset or 0)))
        out = []
        for r in rows:
            try:
                extra = json.loads(r.get("extra") or "{}")
            except ValueError:
                extra = {}
            if task_id and extra.get("task_id") == task_id and r["kind"] == "chat":
                continue
            item = {"kind": r["kind"], "kind_cn": KIND_CN[r["kind"]], "id": r["ref"], "title": r.get("title") or "",
                    "snippet": snippet(r.get("body") or "", words), "ts": r.get("ts"),
                    "task_id": extra.get("task_id") or (r["ref"] if r["kind"] == "task" else None),
                    "thread_id": extra.get("thread_id"), "status": extra.get("status"), "path": extra.get("path"),
                    "role": extra.get("role")}
            if r["kind"] == "chat":
                item["open"] = f"chat?m={r['ref']}"
            elif r["kind"] == "task":
                item["open"] = f"task/{r['ref']}"
            else:
                item["open"] = f"task/{item['task_id']}" if item["task_id"] else "chat"
            out.append(item)
        return {"q": q, "results": out, "total": total}

    def context(self, kind: str, ref: str, around: int = 6) -> list[dict]:
        """The messages around a chat hit (scroll), or a task's own text."""
        if kind == "chat":
            me = self.store._x("SELECT seq FROM chat_messages WHERE id=?", (ref,))
            if not me:
                return []
            seq = me[0]["seq"]
            rows = self.store._x("SELECT id, role, text, created_at FROM chat_messages WHERE seq BETWEEN ? AND ? "
                                 "AND COALESCE(text,'') <> '' ORDER BY seq", (seq - around, seq + around))
            return [{"id": r["id"], "role": r["role"], "text": _clean(r["text"], 600), "ts": r["created_at"],
                     "hit": r["id"] == ref} for r in rows]
        if kind == "task":
            t = self.store.task(ref)
            if not t:
                return []
            return [{"id": ref, "role": "task", "status": t.get("status"), "text": _clean(t.get("prompt"), 1500),
                     "result": _clean(t.get("result"), 2500), "ts": t.get("created_at")}]
        return []

    # ------------------------------------------------------------------ the tool (chat + tasks)
    TOOL = {"name": "session_search",
            "description": ("搜索用户以前的聊天、任务和文件（全文搜索，返回原话，不是摘要）。用户提到「上次/之前/那次/以前那个…」、"
                            "要接着以前的事做、或你需要以前的做法和结果时先用它。query 写 1-3 个关键词（中文至少两个字）；"
                            "找到后用 open 参数（结果里的 kind:id）看那条消息前后的原话或那个任务的完整结果。"),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "关键词，空格分开；整句加引号"},
                "kind": {"type": "string", "enum": ["", "chat", "task", "file"], "description": "只搜某一类，可留空"},
                "open": {"type": "string", "description": "展开一条结果，如 chat:<id> 或 task:<id>"}}}}

    def tool(self, exclude_task: Optional[str] = None):
        async def call(a: dict) -> str:
            a = a or {}
            if a.get("open"):
                kind, _, ref = str(a["open"]).partition(":")
                ctx = self.context(kind.strip(), ref.strip())
                if not ctx:
                    return "没有找到这条。"
                lines = []
                for c in ctx:
                    if kind == "task":
                        lines.append(f"任务 {c['id']}（{c.get('status')}）\n要求：{c['text']}\n结果：{c.get('result') or '（无）'}")
                    else:
                        who = "用户" if c["role"] == "user" else "助理"
                        lines.append(("→ " if c.get("hit") else "  ") + f"{_day(c['ts'])} {who}：{c['text']}")
                return "\n".join(lines)
            kinds = [a["kind"]] if a.get("kind") in KINDS else None
            res = self.search(a.get("query") or "", kinds, 8, task_id=exclude_task)
            if not res["results"]:
                return f"没有搜到「{a.get('query', '')}」。换个关键词（更短、或用别的说法）再试一次。"
            lines = [f"共 {res['total']} 条，最相关的 {len(res['results'])} 条："]
            for r in res["results"]:
                extra = f"（{r['status']}）" if r.get("status") else ""
                if r["kind"] == "file":
                    lines.append(f"- [文件] {_day(r['ts'])} {r['title']} 路径 {r.get('path') or ''}"
                                 + (f" 来自任务 {r['task_id']}" if r.get("task_id") else ""))
                else:
                    head = r["title"] if r["kind"] == "task" else ("用户" if r.get("role") == "user" else "助理")
                    lines.append(f"- [{r['kind_cn']} {r['kind']}:{r['id']}] {_day(r['ts'])} {head}{extra}：{r['snippet']}")
            return "\n".join(lines)
        call.max_result = 3500   # the loop cuts external tool results at 600 chars by default
        return self.TOOL, call


def _day(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(ts) + 8 * 3600))
    except (TypeError, ValueError):
        return ""


def mount(app, search: Search, auth) -> None:
    from fastapi import Depends, HTTPException

    @app.get("/api/search", dependencies=[Depends(auth)])
    async def api_search(q: str = "", kind: str = "", limit: int = 30, offset: int = 0):
        kinds = [k for k in kind.split(",") if k] or None
        return search.search(q, kinds, limit, offset)

    @app.get("/api/search/context", dependencies=[Depends(auth)])
    async def api_search_context(kind: str, id: str):
        ctx = search.context(kind, id)
        if not ctx:
            raise HTTPException(404, "not found")
        return {"items": ctx}

    @app.post("/api/search/reindex", dependencies=[Depends(auth)])
    async def api_reindex():
        search.store._x(f"DELETE FROM {search.table}")
        search.store.kv_set("search_marks", {})
        return {"indexed": search.sync(force=True)}
