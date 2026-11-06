"""v3.9.24 (5) daily token usage.

One row per model call (llm_router.usage sink): day (Asia/Shanghai), model, role, task, input/output tokens.
  GET /api/usage?days=14            per day, per model, per role, totals
  GET /api/usage/task/{task_id}      one task's calls summed per model / role
  DELETE /api/usage?before_days=90   prune old rows
AIW_USAGE=0 stops recording (the page then shows what was recorded before).
"""
from __future__ import annotations

import logging
import os
import time
import uuid

log = logging.getLogger("aiw.usage")
TZ_OFFSET = 8 * 3600   # days are counted on the user's clock (Asia/Shanghai)

SCHEMA = """CREATE TABLE IF NOT EXISTS token_usage (id TEXT PRIMARY KEY, day TEXT, ts REAL, model TEXT, role TEXT,
  task_id TEXT, input INTEGER, output INTEGER);
CREATE INDEX IF NOT EXISTS ix_usage_day ON token_usage(day);
CREATE INDEX IF NOT EXISTS ix_usage_task ON token_usage(task_id);"""


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts + TZ_OFFSET))


def ensure_table(store) -> None:
    with store.lock:
        store.db.executescript(SCHEMA)
        store.db.commit()


class Usage:
    def __init__(self, store):
        self.store = store
        ensure_table(store)

    def record(self, row: dict) -> None:
        now = time.time()
        self.store._x("INSERT INTO token_usage VALUES (?,?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex[:16], day_of(now), now, str(row.get("model") or "")[:120],
                       str(row.get("role") or "other")[:40], str(row.get("task_id") or "")[:60],
                       int(row.get("input") or 0), int(row.get("output") or 0)))

    def install(self) -> None:
        from llm_router import usage as _u
        _u.set_sink(self.record)

    def summary(self, days: int = 14) -> dict:
        days = max(1, min(int(days or 14), 366))
        since = day_of(time.time() - (days - 1) * 86400)
        q = self.store._x
        per_day = q("SELECT day, SUM(input) AS input, SUM(output) AS output, COUNT(*) AS calls FROM token_usage "
                    "WHERE day>=? GROUP BY day ORDER BY day DESC", (since,))
        per_day_model = q("SELECT day, model, SUM(input) AS input, SUM(output) AS output, COUNT(*) AS calls FROM token_usage "
                          "WHERE day>=? GROUP BY day, model ORDER BY day DESC, input+output DESC", (since,))
        per_model = q("SELECT model, SUM(input) AS input, SUM(output) AS output, COUNT(*) AS calls FROM token_usage "
                      "WHERE day>=? GROUP BY model ORDER BY input+output DESC", (since,))
        per_role = q("SELECT role, SUM(input) AS input, SUM(output) AS output, COUNT(*) AS calls FROM token_usage "
                     "WHERE day>=? GROUP BY role ORDER BY input+output DESC", (since,))
        top_tasks = q("SELECT task_id, SUM(input) AS input, SUM(output) AS output, COUNT(*) AS calls FROM token_usage "
                      "WHERE day>=? AND task_id<>'' GROUP BY task_id ORDER BY input+output DESC LIMIT 10", (since,))
        tot = q("SELECT COALESCE(SUM(input),0) AS input, COALESCE(SUM(output),0) AS output, COUNT(*) AS calls "
                "FROM token_usage WHERE day>=?", (since,))[0]
        by_day: dict[str, dict] = {}
        for r in per_day:
            by_day[r["day"]] = {**r, "total": (r["input"] or 0) + (r["output"] or 0), "models": []}
        for r in per_day_model:
            if r["day"] in by_day:
                by_day[r["day"]]["models"].append({**r, "total": (r["input"] or 0) + (r["output"] or 0)})
        for t in top_tasks:
            task = self.store.task(t["task_id"]) or {}
            t["title"] = (task.get("prompt") or "")[:60]
        return {"days": days, "since": since, "recording": os.environ.get("AIW_USAGE", "1") != "0",
                "total": {**tot, "total": tot["input"] + tot["output"]},
                "per_day": list(by_day.values()), "per_model": per_model, "per_role": per_role, "top_tasks": top_tasks}

    def task(self, task_id: str) -> dict:
        rows = self.store._x("SELECT model, role, SUM(input) AS input, SUM(output) AS output, COUNT(*) AS calls "
                             "FROM token_usage WHERE task_id=? GROUP BY model, role", (task_id,))
        return {"task_id": task_id, "rows": rows, "input": sum(r["input"] or 0 for r in rows),
                "output": sum(r["output"] or 0 for r in rows)}

    def prune(self, before_days: int = 90) -> int:
        cut = day_of(time.time() - max(1, int(before_days)) * 86400)
        n = self.store._x("SELECT COUNT(*) AS n FROM token_usage WHERE day<?", (cut,))[0]["n"]
        self.store._x("DELETE FROM token_usage WHERE day<?", (cut,))
        return n


def mount(app, usage: Usage, auth) -> None:
    from fastapi import Depends

    @app.get("/api/usage", dependencies=[Depends(auth)])
    async def usage_summary(days: int = 14):
        return usage.summary(days)

    @app.get("/api/usage/task/{task_id}", dependencies=[Depends(auth)])
    async def usage_task(task_id: str):
        return usage.task(task_id)

    @app.delete("/api/usage", dependencies=[Depends(auth)])
    async def usage_prune(before_days: int = 90):
        return {"deleted": usage.prune(before_days)}
