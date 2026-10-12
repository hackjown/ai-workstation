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
        self._x("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (t["id"], t.get("user_id"), t["prompt"], t["machine_id"], t["status"], None, now, now))

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
