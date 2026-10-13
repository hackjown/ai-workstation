"""Persistence (spec §23). SQLite for single-node V1; schema mirrors
infrastructure/postgres/schema.sql so moving to Postgres is a driver swap."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS machines (id TEXT PRIMARY KEY, provider TEXT, provider_vm_id TEXT, name TEXT,
  status TEXT, ip TEXT, agent_endpoint TEXT, desktop_endpoint TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, user_id TEXT, prompt TEXT, machine_id TEXT, status TEXT,
  result TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, machine_id TEXT, task_id TEXT, status TEXT,
  started_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS machine_actions (id TEXT PRIMARY KEY, session_id TEXT, action_type TEXT, payload TEXT,
  result TEXT, screenshot TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS agent_events (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, step INTEGER, data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, path TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS workspace_files (id TEXT PRIMARY KEY, session_id TEXT, path TEXT, size INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, title TEXT, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS routines (id TEXT PRIMARY KEY, name TEXT, prompt TEXT, schedule TEXT, machine_id TEXT,
  thread_id TEXT, enabled INTEGER DEFAULT 1, last_run REAL, next_run REAL, last_task_id TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, name TEXT, url TEXT, mode TEXT, value TEXT, interval_min INTEGER,
  enabled INTEGER DEFAULT 1, last_value TEXT, last_hash TEXT, last_checked REAL, next_check REAL, fail_count INTEGER DEFAULT 0,
  last_error TEXT, triggered_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, kind TEXT, title TEXT, body TEXT, task_id TEXT,
  thread_id TEXT, link TEXT, read INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, text TEXT, source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS ix_events_session ON agent_events(session_id, ts);
CREATE INDEX IF NOT EXISTS ix_actions_session ON machine_actions(session_id, created_at);
"""


class Store:
    def __init__(self, path: str = "aiw.db"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
            for col, typ in (("thread_id", "TEXT"), ("source", "TEXT"), ("plan", "TEXT"), ("session_id", "TEXT"),
                             ("max_steps", "INTEGER"), ("timeout_s", "REAL")):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_tasks_thread ON tasks(thread_id, created_at)")
            self.db.commit()

    def _x(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return [dict(r) for r in cur.fetchall()]

    def upsert_machine(self, m: dict):
        now = time.time()
        self._x("""INSERT INTO machines VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET provider=excluded.provider, provider_vm_id=excluded.provider_vm_id,
                   name=excluded.name, agent_endpoint=excluded.agent_endpoint,
                   desktop_endpoint=excluded.desktop_endpoint, updated_at=excluded.updated_at""",
                (m["id"], m["provider"], m["provider_vm_id"], m.get("name", m["id"]), m.get("status", "unknown"),
                 m.get("ip"), m["agent_endpoint"], m.get("desktop_endpoint"), now, now))

    def set_machine_status(self, mid: str, status: str, ip: Optional[str] = None):
        self._x("UPDATE machines SET status=?, ip=COALESCE(?, ip), updated_at=? WHERE id=?", (status, ip, time.time(), mid))

    def machines(self) -> list[dict]:
        return self._x("SELECT * FROM machines ORDER BY id")

    def machine(self, mid: str) -> Optional[dict]:
        r = self._x("SELECT * FROM machines WHERE id=?", (mid,))
        return r[0] if r else None

    def create_task(self, t: dict):
        now = time.time()
        self._x("""INSERT INTO tasks (id, user_id, prompt, machine_id, status, result, created_at, updated_at,
                   thread_id, source, max_steps, timeout_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t["id"], t.get("user_id"), t["prompt"], t["machine_id"], t["status"], None, now, now,
                 t.get("thread_id"), t.get("source", "chat"), t.get("max_steps", 40), t.get("timeout_s", 600)))
        if t.get("thread_id"):
            self.touch_thread(t["thread_id"])

    def set_task_session(self, tid: str, sid: str):
        self._x("UPDATE tasks SET session_id=? WHERE id=?", (sid, tid))

    def set_task_plan(self, tid: str, plan: dict):
        self._x("UPDATE tasks SET plan=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), tid))

    def tasks_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        q = ",".join("?" * len(statuses))
        return self._x(f"SELECT * FROM tasks WHERE status IN ({q}) ORDER BY created_at", statuses)

    def thread_tasks(self, thread_id: str, limit: int = 200) -> list[dict]:
        rows = self._x("SELECT * FROM tasks WHERE thread_id=? ORDER BY created_at LIMIT ?", (thread_id, limit))
        for r in rows:
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else None
        return rows

    def recent_tasks(self, limit: int = 50, status: Optional[str] = None) -> list[dict]:
        if status:
            return self._x("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        return self._x("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---------------------------------------------------------------- threads
    def create_thread(self, title: str = "") -> dict:
        tid, now = f"thr-{uuid.uuid4().hex[:10]}", time.time()
        self._x("INSERT INTO threads VALUES (?,?,?,?,?)", (tid, title or "新对话", 0, now, now))
        return self.thread(tid)

    def thread(self, tid: str) -> Optional[dict]:
        r = self._x("SELECT * FROM threads WHERE id=?", (tid,))
        return r[0] if r else None

    def threads(self, archived: bool = False, limit: int = 100) -> list[dict]:
        return self._x("""SELECT t.*, (SELECT prompt FROM tasks WHERE thread_id=t.id ORDER BY created_at DESC LIMIT 1) AS last_prompt,
                          (SELECT status FROM tasks WHERE thread_id=t.id ORDER BY created_at DESC LIMIT 1) AS last_status,
                          (SELECT COUNT(*) FROM tasks WHERE thread_id=t.id) AS task_count
                          FROM threads t WHERE archived=? ORDER BY updated_at DESC LIMIT ?""", (1 if archived else 0, limit))

    def update_thread(self, tid: str, title: Optional[str] = None, archived: Optional[bool] = None):
        if title is not None:
            self._x("UPDATE threads SET title=? WHERE id=?", (title, tid))
        if archived is not None:
            self._x("UPDATE threads SET archived=? WHERE id=?", (1 if archived else 0, tid))

    def touch_thread(self, tid: str):
        self._x("UPDATE threads SET updated_at=? WHERE id=?", (time.time(), tid))

    def delete_thread(self, tid: str):
        self._x("DELETE FROM threads WHERE id=?", (tid,))
        self._x("UPDATE tasks SET thread_id=NULL WHERE thread_id=?", (tid,))

    # ---------------------------------------------------------------- generic rows
    def insert(self, table: str, row: dict) -> dict:
        row = {"id": row.get("id") or f"{table[:3]}-{uuid.uuid4().hex[:10]}", "created_at": time.time(), **row}
        cols = ",".join(row)
        self._x(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        return self.get(table, row["id"])

    def get(self, table: str, rid: str) -> Optional[dict]:
        r = self._x(f"SELECT * FROM {table} WHERE id=?", (rid,))
        return r[0] if r else None

    def update(self, table: str, rid: str, **fields):
        if fields:
            sets = ",".join(f"{k}=?" for k in fields)
            self._x(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), rid))

    def delete(self, table: str, rid: str):
        self._x(f"DELETE FROM {table} WHERE id=?", (rid,))

    def all(self, table: str, where: str = "1=1", args: tuple = (), order: str = "created_at DESC", limit: int = 200) -> list[dict]:
        return self._x(f"SELECT * FROM {table} WHERE {where} ORDER BY {order} LIMIT ?", (*args, limit))

    # ---------------------------------------------------------------- kv settings
    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self._x("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def kv_set(self, key: str, value: Any):
        self._x("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)))

    def secret_put(self, name: str, enc: str):
        self._x("INSERT INTO secrets VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (name, enc, time.time()))

    def secret_get(self, name: str) -> Optional[str]:
        r = self._x("SELECT value FROM secrets WHERE name=?", (name,))
        return r[0]["value"] if r else None

    def secret_names(self) -> list[dict]:
        return self._x("SELECT name, updated_at FROM secrets ORDER BY name")

    def secret_delete(self, name: str):
        self._x("DELETE FROM secrets WHERE name=?", (name,))

    def actions(self, sid: str) -> list[dict]:
        rows = self._x("SELECT * FROM machine_actions WHERE session_id=? ORDER BY created_at", (sid,))
        for r in rows:
            for k in ("payload", "result"):
                try:
                    r[k] = json.loads(r[k]) if r.get(k) else None
                except ValueError:
                    pass
        return rows

    def delete_machine(self, mid: str):
        self._x("DELETE FROM machines WHERE id=?", (mid,))

    def update_task(self, tid: str, status: str, result: Optional[str] = None):
        self._x("UPDATE tasks SET status=?, result=COALESCE(?, result), updated_at=? WHERE id=?",
                (status, result, time.time(), tid))

    def task(self, tid: str) -> Optional[dict]:
        r = self._x("SELECT * FROM tasks WHERE id=?", (tid,))
        return r[0] if r else None

    def create_session(self, machine_id: str, task_id: str) -> str:
        sid = f"sess-{uuid.uuid4().hex[:10]}"
        self._x("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (sid, machine_id, task_id, "running", time.time(), None))
        return sid

    def end_session(self, sid: str, status: str):
        self._x("UPDATE sessions SET status=?, ended_at=? WHERE id=?", (status, time.time(), sid))

    def session(self, sid: str) -> Optional[dict]:
        r = self._x("SELECT * FROM sessions WHERE id=?", (sid,))
        return r[0] if r else None

    def add_event(self, ev: dict):
        self._x("INSERT INTO agent_events VALUES (?,?,?,?,?,?)",
                (ev["id"], ev["session_id"], ev["type"], ev.get("step"), json.dumps(ev.get("data", {}), ensure_ascii=False), ev["ts"]))

    def events(self, sid: str, limit: int = 500) -> list[dict]:
        rows = self._x("SELECT * FROM agent_events WHERE session_id=? ORDER BY ts LIMIT ?", (sid, limit))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    def add_action(self, sid: str, rec: dict):
        self._x("INSERT INTO machine_actions VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, sid, rec.get("action"), json.dumps(rec.get("payload"), ensure_ascii=False),
                 json.dumps({k: rec.get(k) for k in ("level", "reason", "error", "duration_ms")}, ensure_ascii=False),
                 rec.get("screenshot"), rec.get("phase") + ":" + str(rec.get("success", "")), time.time()))

    def add_artifact(self, sid: str, kind: str, path: str):
        self._x("INSERT INTO artifacts VALUES (?,?,?,?,?)", (uuid.uuid4().hex, sid, kind, path, time.time()))

    def artifacts(self, sid: str) -> list[dict]:
        return self._x("SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at", (sid,))
