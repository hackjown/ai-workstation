"""v3.9.26 (task-830): the session manager's side of the dev/fix workflow.

* thread_signals()  what the thread holds: files sent (html/js/py …), scripts run, repo paths, real CAD history, and the
                    provenance of every delivered file (kv thread_dev:<thread>, else rebuilt from the stored events, so
                    old threads like thr-8d8cfb0ce9 work too)
* task_intent()     intent.classify() on the run's words + those signals, persisted as kv task_intent:<task id>
* persister()       the loop's persist(kind, value): provenance / fingerprints per thread, contract / git per task
* rollback()        「回滚」: git back to the HEAD recorded at the start of the dev_fix task (on its computer)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from agent_runtime import intent as _in
from agent_runtime import provenance as _pv

log = logging.getLogger("aiw.devflow")
_CODE_EXT = re.compile(r"\.(html?|js|mjs|ts|tsx|jsx|py|css|vue|sh)$", re.I)


def _events(store, sid: str) -> list:
    try:
        return store.events(sid, limit=3000) or []
    except Exception:  # noqa: BLE001
        return []


def _scan(store, thread_id: Optional[str], exclude: set) -> dict:
    """One pass over the thread's stored runs: sent files, script runs, cad history, rebuilt provenance."""
    out = {"sent_files": [], "script_runs": [], "repo_paths": [], "cad_history": False, "provenance": {}}
    if not thread_id:
        return out
    book = _pv.ProvenanceBook()
    shell_log: list = []
    try:
        rows = store.thread_tasks(thread_id) or []
    except Exception:  # noqa: BLE001
        rows = []
    for row in rows:
        if row.get("id") in exclude or not row.get("session_id"):
            continue
        acts: dict = {}
        for e in _events(store, row["session_id"]):
            d = e.get("data") if isinstance(e.get("data"), dict) else e   # v3.9.29: exported/flattened events too
            typ, st = e.get("type"), e.get("step")
            if typ == "agent.decision" and d.get("tool"):
                acts[st] = {"tool": d.get("tool"), "args": d.get("args") or {}}
                if d.get("tool") in ("cad_command", "drawing_build", "cad_draw"):
                    out["cad_history"] = True
            elif typ == "agent.action_result" and st in acts and acts[st]["tool"] == d.get("tool"):
                a = acts.pop(st)
                res = d.get("result") if isinstance(d.get("result"), dict) else {}
                if a["tool"] == "shell":
                    cmd = str((a["args"] or {}).get("command") or "")
                    cwd = str((a["args"] or {}).get("cwd") or "").strip()
                    if cwd and not re.match(r"\s*cd\s", cmd):   # v3.9.29: the shell tool's cwd = a leading cd
                        cmd = f"cd {cwd} && {cmd}"
                    shell_log.append({"step": st, "command": cmd, "stdout": str(res.get("stdout") or "")})
                    from agent_runtime import devflow as _rdf   # every script of `cd X && python3 Y.py && …`
                    for sc in _rdf.scripts_run(cmd) or [x for x in [_pv.script_of(cmd)] if x]:
                        if sc in out["script_runs"]:
                            out["script_runs"].remove(sc)   # newest last
                        out["script_runs"].append(sc)
                        if not sc.startswith("~/workspace") and "/workspace/" not in sc and sc not in out["repo_paths"]:
                            out["repo_paths"].append(sc)
                elif a["tool"] == "send_file" and d.get("success"):
                    p = str(res.get("path") or (a["args"] or {}).get("path") or "")
                    if p:
                        out["sent_files"].append(p)
                        if p.lower().endswith(".dxf"):
                            out["cad_history"] = True
                        book.record(p, _pv.producer_for(p, shell_log), row.get("id") or "")
        shell_log = shell_log[-200:]
    out["provenance"] = book.to_dict()
    out["sent_files"] = out["sent_files"][-30:]
    out["script_runs"] = out["script_runs"][-20:]
    out["repo_paths"] = out["repo_paths"][-20:]
    return out


def thread_dev(store, thread_id: Optional[str]) -> dict:
    return (store.kv_get(f"thread_dev:{thread_id}") or {}) if thread_id else {}


def thread_signals(store, thread_id: Optional[str], exclude: Optional[set] = None) -> dict:
    sig = _scan(store, thread_id, set(exclude or ()))
    kept = thread_dev(store, thread_id)
    prov = dict(sig.get("provenance") or {})
    for k, v in (kept.get("provenance") or {}).items():   # what the loop recorded beats what the events show
        prov[k] = v
    sig["provenance"] = prov
    sig["fingerprints"] = dict(kept.get("fingerprints") or {})
    if prov:
        for p, e in prov.items():
            if e.get("script") and e["script"] not in sig["script_runs"]:
                sig["script_runs"].append(e["script"])
    return sig


def root_intent(store, root: dict, signals: Optional[dict] = None) -> str:
    got = store.kv_get(f"task_intent:{root['id']}") or {}
    if got.get("intent") in _in.INTENTS:
        return got["intent"]
    from . import continuation as _cont
    return _in.classify(_cont.instructions(store, root), {}).get("intent") or _in.OTHER


def task_intent(store, row: dict, signals: Optional[dict] = None) -> dict:
    """The run's intent: a continuation is judged by its NEW words (in the thread's context); persisted."""
    from . import continuation as _cont
    words = row.get("prompt") or ""
    if row.get("continues") and row.get("continues") != row.get("id"):
        mode = str((store.kv_get(f"task_run:{row['id']}", {}) or {}).get("mode") or row.get("source") or "continue")
        root = store.task(row["continues"]) or row
        if mode == "edit" or not words.strip() or _cont.is_bare_continue(words) or words.startswith("（继续之前"):
            words = _cont.instructions(store, root)
            if mode != "edit":   # 继续 / 重试 keep the task's own intent
                prev = store.kv_get(f"task_intent:{root['id']}") or {}
                if prev.get("intent") in _in.INTENTS:
                    info = dict(prev)
                    store.kv_set(f"task_intent:{row['id']}", info)
                    return info
    info = _in.classify(words, signals or {})
    try:
        store.kv_set(f"task_intent:{row['id']}", info)
    except Exception as e:  # noqa: BLE001
        log.warning("task intent: %s", e)
    return info


def persister(store, task):
    """loop.persist(kind, value) for one task."""
    root = task.continues or task.id
    tid = task.thread_id

    def put(kind: str, value) -> None:
        if kind in ("provenance", "fingerprint") and tid:
            cur = store.kv_get(f"thread_dev:{tid}") or {}
            if kind == "provenance":
                cur["provenance"] = {**(cur.get("provenance") or {}), **(value or {})}
            else:
                fps = {**(cur.get("fingerprints") or {}), **(value or {})}
                cur["fingerprints"] = dict(list(fps.items())[-40:])
            store.kv_set(f"thread_dev:{tid}", cur)
        elif kind == "contract":
            store.kv_set(f"task_contract:{task.id}", value)
        elif kind == "git":
            store.kv_set(f"task_git:{task.id}", value)
            if root != task.id:
                store.kv_set(f"task_git:{root}", value)
    return put


def git_state(store, task_id: str) -> Optional[dict]:
    """The git record of a task (its own, else its root's)."""
    t = store.task(task_id) or {}
    for k in (task_id, t.get("continues")):
        if k:
            g = store.kv_get(f"task_git:{k}")
            if g and (g.get("repo") or g.get("mode") == "copy"):
                return g
    return None


async def rollback(sessions, task_id: str) -> dict:
    """「回滚」 a dev_fix task: git back to the recorded HEAD on the task's computer (git_ops.op_rollback)."""
    from agent_runtime import git_ops as _git
    store = sessions.store
    t = store.task(task_id)
    if not t:
        raise KeyError(task_id)
    g = git_state(store, task_id)
    if not g:
        raise ValueError("这个任务没有记录 git 快照，无法回滚")
    root = t.get("continues") or task_id
    for r in store.continuations(root) or [t]:
        live = sessions.live_for_task(r["id"])
        if live and live.runner and not live.runner.done():
            raise RuntimeError("任务还在运行，先停止再回滚")
    comp = sessions.computer(g.get("machine_id") or t.get("machine_id"))
    cmd = _git.command("rollback", {**{k: g.get(k) for k in _git.STATE_KEYS}, "task": root})
    r = await comp.execute("shell.exec", {"command": cmd, "timeout": 90}, observe=False, timeout=110)
    res = _git.parse((r.result or {}).get("stdout") if isinstance(r.result, dict) else "")
    if res.get("ok"):
        g = {**g, "rolled_back": True, "rollback": res}
        store.kv_set(f"task_git:{task_id}", g)
        if root != task_id:
            store.kv_set(f"task_git:{root}", g)
    return {"ok": bool(res.get("ok")), "result": res, "git": g}


def dispatch_budget(store) -> tuple[int, float]:
    """v3.9.26: a dispatched child task with no budget (0) gets this one (kv "dispatch" {max_steps, timeout_s}, else
    AIW_DISPATCH_MAX_STEPS / AIW_DISPATCH_TIMEOUT). Reaching it pauses and asks, like every budget (3.8.2)."""
    c = {}
    try:
        c = store.kv_get("dispatch") or {}
    except Exception:  # noqa: BLE001
        pass
    try:
        steps = int(c.get("max_steps") or os.environ.get("AIW_DISPATCH_MAX_STEPS", "150") or 0)
    except ValueError:
        steps = 150
    try:
        secs = float(c.get("timeout_s") or os.environ.get("AIW_DISPATCH_TIMEOUT", "3600") or 0)
    except ValueError:
        secs = 3600.0
    return max(0, steps), max(0.0, secs)
