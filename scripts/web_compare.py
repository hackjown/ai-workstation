#!/usr/bin/env python3
"""v3.9.49: compare the VM path with the DeepSeek path (联网工具) on your own recent tasks.

1) Report only (read-only, nothing runs):
     docker exec aiw-api python scripts/web_compare.py --n 10
   Lists the last N finished top-level tasks that web_lite() routes to DeepSeek, with how the old run went
   (status, minutes, steps, screenshots, tokens).

2) Re-run them on the DeepSeek path and compare:
     docker exec -e AIW_API_TOKEN=... aiw-api python scripts/web_compare.py --n 10 --rerun
   Each prompt is sent again as a NEW task (POST /api/tasks, new_task) on the same computer, one at a time, while
   设置 › 联网工具 is on (backend deepseek). Then both runs are put side by side and saved as
   workspace/web-compare-<time>.md and .json. Re-run tasks are real tasks: only pick read / summarise tasks (the
   default filter), never ones that order, post or log in.

   --all       also list tasks web_lite() would NOT route (they stay on the VM path, shown for reference)
   --api URL   default http://127.0.0.1:8000 (inside the container)
   --timeout   seconds per re-run task (default 900)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("protocols", "services/agent-runtime", "services/llm-router", "services/tool-router",
          "services/computer-runtime", "services/vm-runtime", "services/api", "vm-agent"):
    sys.path.insert(0, str(ROOT / p))

TERMINAL = ("succeeded", "failed", "stopped", "interrupted")


def db_path() -> str:
    return os.environ.get("AIW_DB") or str(ROOT / "workspace" / "aiw.db")


def q(db, sql, args=()):
    try:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def metrics(db, task_id: str) -> dict:
    t = (q(db, "SELECT * FROM tasks WHERE id=?", (task_id,)) or [{}])[0]
    sess = q(db, "SELECT id, started_at, ended_at FROM sessions WHERE task_id=?", (task_id,))
    sids = [s["id"] for s in sess]
    ph = ",".join("?" * len(sids)) or "''"
    actions = q(db, f"SELECT data FROM agent_events WHERE type='agent.action' AND session_id IN ({ph})", tuple(sids))
    tools = [json.loads(a["data"] or "{}").get("tool") for a in actions]
    shots = q(db, f"SELECT COUNT(*) AS n FROM machine_actions WHERE session_id IN ({ph}) AND action_type LIKE '%screenshot%'",
              tuple(sids))
    tok = q(db, "SELECT COALESCE(SUM(input),0) AS i, COALESCE(SUM(output),0) AS o FROM token_usage WHERE task_id=?", (task_id,))
    web = q(db, "SELECT kind, COUNT(*) AS n, COALESCE(SUM(cost),0) AS c FROM web_tools_usage WHERE task_id=? GROUP BY kind",
            (task_id,))
    lite_msg = q(db, f"SELECT COUNT(*) AS n FROM agent_events WHERE type='agent.message' AND session_id IN ({ph}) "
                     "AND data LIKE '%联网：DeepSeek%'", tuple(sids))
    dur = sum(((s["ended_at"] or s["started_at"] or 0) - (s["started_at"] or 0)) for s in sess) if sess else \
        (t.get("updated_at") or 0) - (t.get("created_at") or 0)
    return {"id": task_id, "status": t.get("status"), "minutes": round(max(0.0, dur) / 60, 1), "steps": len(tools),
            "screenshots": (shots[0]["n"] if shots else 0), "browser": sum(1 for x in tools if str(x or "").startswith(("browser_", "bos_"))),
            "tokens": (tok[0]["i"] + tok[0]["o"]) if tok else 0,
            "web_calls": {w["kind"]: w["n"] for w in web}, "web_cost": round(sum(w["c"] for w in web), 4),
            "lite": bool(lite_msg and lite_msg[0]["n"])}


def candidates(db, n: int, include_all: bool) -> list[dict]:
    from agent_runtime.loop import web_lite
    rows = q(db, "SELECT id, prompt, machine_id, status, created_at FROM tasks WHERE (parent_id IS NULL OR parent_id='') "
                 "AND (continues IS NULL OR continues='') AND status IN ('succeeded','failed') ORDER BY created_at DESC LIMIT 400")
    out = []
    for r in rows:
        r["web_lite"] = web_lite(r["prompt"] or "")
        if r["web_lite"] or include_all:
            out.append(r)
        if len([x for x in out if x["web_lite"]]) >= n:
            break
    return out


def api(base: str, token: str, method: str, path: str, body=None) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def rerun(base, token, row, timeout) -> str:
    r = api(base, token, "POST", "/api/tasks", {"prompt": row["prompt"], "machine_id": row["machine_id"], "new_task": True})
    tid = (r.get("task") or r).get("id")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        t = api(base, token, "GET", f"/api/tasks/{tid}")
        st = (t.get("task") or t).get("status")
        if st in TERMINAL:
            return tid
        if st in ("waiting_input", "waiting_confirm", "paused", "takeover"):
            print(f"  {tid} 在等你（{st}），去网页处理后会继续…", flush=True)
    print(f"  {tid} 超时 {timeout}s，停止", flush=True)
    try:
        sid = (api(base, token, "GET", f"/api/tasks/{tid}") or {}).get("session_id")
        if sid:
            api(base, token, "POST", "/api/agent/stop", {"session_id": sid})
    except Exception:  # noqa: BLE001
        pass
    return tid


def fmt_row(label, m):
    w = m.get("web_calls") or {}
    webs = f"搜{w.get('search', 0)}/读{w.get('fetch', 0)}/文{w.get('doc', 0)} ${m.get('web_cost', 0):.4f}" if w else "-"
    return (f"| {label} | {m['status']} | {m['minutes']} | {m['steps']} | {m['screenshots']} | {m['browser']} | "
            f"{m['tokens']:,} | {webs} |")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--rerun", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--api", default=os.environ.get("AIW_COMPARE_API", "http://127.0.0.1:8000"))
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    path = db_path()
    if not os.path.isfile(path):
        print(f"找不到数据库 {path}（设 AIW_DB）", file=sys.stderr)
        return 2
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = candidates(db, a.n, a.all)
    if not rows:
        print("最近没有「查 / 读 / 总结」类的任务。")
        return 0
    lines = ["| 任务 | 状态 | 分钟 | 步数 | 截图 | 浏览器步 | token | 联网工具 |", "|---|---|---|---|---|---|---|---|"]
    report = []
    token = os.environ.get("AIW_API_TOKEN", "")
    if a.rerun and not token:
        print("--rerun 需要 AIW_API_TOKEN", file=sys.stderr)
        return 2
    if a.rerun:
        w = api(a.api, token, "GET", "/api/web-tools")
        if not w.get("active"):
            print("设置 › 联网工具 没启用（后端 deepseek + DeepSeek Key），先打开再比较", file=sys.stderr)
            return 2
    for r in rows:
        old = metrics(db, r["id"])
        title = (r["prompt"] or "").replace("\n", " ")[:36]
        lines.append(f"| **{title}** {'(DeepSeek 路径)' if r['web_lite'] else '(只走 VM)'} | | | | | | | |")
        lines.append(fmt_row(f"旧 {r['id']}", old))
        item = {"prompt": r["prompt"], "web_lite": r["web_lite"], "old": old}
        if a.rerun and r["web_lite"]:
            print(f"重跑：{title}", flush=True)
            tid = rerun(a.api, token, r, a.timeout)
            db2 = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            db2.row_factory = sqlite3.Row
            new = metrics(db2, tid)
            item["new"] = new
            lines.append(fmt_row(f"新 {tid}", new))
        report.append(item)
    md = "\n".join(lines)
    print(md)
    if a.rerun:
        done = [x for x in report if x.get("new")]
        if done:
            def avg(k, side):
                v = [x[side][k] for x in done]
                return round(sum(v) / len(v), 1)
            ok_old = sum(1 for x in done if x["old"]["status"] == "succeeded")
            ok_new = sum(1 for x in done if x["new"]["status"] == "succeeded")
            summ = (f"\n{len(done)} 个任务：成功 旧 {ok_old} / 新 {ok_new}；平均分钟 旧 {avg('minutes', 'old')} / 新 {avg('minutes', 'new')}；"
                    f"平均截图 旧 {avg('screenshots', 'old')} / 新 {avg('screenshots', 'new')}；平均 token 旧 {avg('tokens', 'old'):,} / "
                    f"新 {avg('tokens', 'new'):,}；联网工具共 ${sum(x['new']['web_cost'] for x in done):.4f}。"
                    f"切回桌面的：{sum(1 for x in done if x['new']['browser'])} 个。结果要人工看一遍质量。")
            md += "\n" + summ
            print(summ)
        stamp = time.strftime("%Y%m%d-%H%M")
        out = ROOT / "workspace" / f"web-compare-{stamp}"
        out.with_suffix(".md").write_text(md, encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已保存 {out}.md / .json（宿主机 ./data/ 下）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
