"""v3.9.24 (7) prompt management API.

kv "prompt_versions": {role: {"active": version id | None, "versions": [{id, text, ts, note}]}}  (newest last, ≤30)
kv "prompt_trace:<task_id>": {role: {system, tools, model, overridden, ts, step?}}  what each role received

  GET  /api/prompts                         roles: title, desc, original, active version, versions (no texts)
  GET  /api/prompts/{role}                  original + every version's text
  POST /api/prompts/{role}                  {text, note?} -> new version, made active
  POST /api/prompts/{role}/activate         {version_id}
  POST /api/prompts/{role}/restore          back to the original (versions are kept)
  GET  /api/prompts/trace/{task_id}         what each role actually received for that task (continuations included)
"""
from __future__ import annotations

import time
import uuid

from agent_runtime import prompts as P

KV = "prompt_versions"
MAX_VERSIONS = 30
TRACE_MAX = 60000


def _load(store) -> dict:
    return store.kv_get(KV, {}) or {}


def push(store) -> None:
    """Active versions -> agent_runtime.prompts (call after every change and at start-up)."""
    data = _load(store)
    over = {}
    for role, d in data.items():
        aid = d.get("active")
        v = next((x for x in d.get("versions") or [] if x.get("id") == aid), None)
        if v and v.get("text"):
            over[role] = v["text"]
    P.set_overrides(over)


def _ensure_registered() -> None:
    """Import the modules that register their originals, so the page lists every role even before first use."""
    for mod in ("agent_runtime.planner", "agent_runtime.loop", "aiw_api.chat", "aiw_api.autoroute", "aiw_api.autoreview", "aiw_api.teach"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            pass


def listing(store) -> list[dict]:
    _ensure_registered()
    data = _load(store)
    out = []
    for role, meta in P.ROLES.items():
        d = data.get(role) or {}
        out.append({"role": role, **meta, "original_chars": len(P.original(role) or ""),
                    "active": d.get("active"), "versions": [{k: v for k, v in x.items() if k != "text"} | {"chars": len(x.get("text") or "")}
                                                           for x in d.get("versions") or []]})
    return out


def detail(store, role: str) -> dict:
    _ensure_registered()
    d = _load(store).get(role) or {}
    return {"role": role, **(P.ROLES.get(role) or {}), "original": P.original(role) or "", "active": d.get("active"),
            "versions": d.get("versions") or []}


def add_version(store, role: str, text: str, note: str = "") -> dict:
    data = _load(store)
    d = data.setdefault(role, {"active": None, "versions": []})
    v = {"id": "pv" + uuid.uuid4().hex[:8], "text": text[:100000], "ts": time.time(), "note": (note or "")[:120]}
    d["versions"] = (d.get("versions") or []) + [v]
    if len(d["versions"]) > MAX_VERSIONS:   # never drop the active one
        keep = [x for x in d["versions"] if x["id"] == d.get("active")]
        d["versions"] = (keep + [x for x in d["versions"] if x not in keep])[-MAX_VERSIONS:]
    d["active"] = v["id"]
    store.kv_set(KV, data)
    push(store)
    return v


def activate(store, role: str, vid) -> bool:
    data = _load(store)
    d = data.get(role)
    if vid is None:
        if d:
            d["active"] = None
            store.kv_set(KV, data)
        push(store)
        return True
    if not d or not any(x["id"] == vid for x in d.get("versions") or []):
        return False
    d["active"] = vid
    store.kv_set(KV, data)
    push(store)
    return True


def trace_sink(store):
    def sink(task_id: str, role: str, rec: dict) -> None:
        key = f"prompt_trace:{task_id}"
        cur = store.kv_get(key, {}) or {}
        rec = {**rec, "system": (rec.get("system") or "")[:TRACE_MAX], "ts": time.time()}
        cur[role] = rec
        store.kv_set(key, cur)
    return sink


def trace(store, task_id: str) -> dict:
    t = store.task(task_id) or {}
    root = t.get("continues") or task_id
    runs = [r["id"] for r in store.continuations(root)] if t else [task_id]
    out = []
    for rid in runs or [task_id]:
        rec = store.kv_get(f"prompt_trace:{rid}", {}) or {}
        out.append({"task_id": rid, "roles": rec})
    return {"task_id": task_id, "runs": out}


def mount(app, store, auth) -> None:
    from fastapi import Body, Depends, HTTPException

    push(store)

    @app.get("/api/prompts", dependencies=[Depends(auth)])
    async def pr_list():
        return listing(store)

    @app.get("/api/prompts/trace/{task_id}", dependencies=[Depends(auth)])
    async def pr_trace(task_id: str):
        return trace(store, task_id)

    @app.get("/api/prompts/{role}", dependencies=[Depends(auth)])
    async def pr_get(role: str):
        if role not in P.ROLES and not _load(store).get(role):
            _ensure_registered()
            if role not in P.ROLES:
                raise HTTPException(404, "no such role")
        return detail(store, role)

    @app.post("/api/prompts/{role}", dependencies=[Depends(auth)])
    async def pr_save(role: str, body: dict = Body(...)):
        _ensure_registered()
        if role not in P.ROLES:
            raise HTTPException(404, "no such role")
        text = str((body or {}).get("text") or "")
        if not text.strip():
            raise HTTPException(400, "提示词不能为空（要用原版请点「恢复原版」）")
        return add_version(store, role, text, str((body or {}).get("note") or ""))

    @app.post("/api/prompts/{role}/activate", dependencies=[Depends(auth)])
    async def pr_activate(role: str, body: dict = Body(...)):
        if not activate(store, role, (body or {}).get("version_id")):
            raise HTTPException(404, "no such version")
        return detail(store, role)

    @app.post("/api/prompts/{role}/restore", dependencies=[Depends(auth)])
    async def pr_restore(role: str):
        activate(store, role, None)
        return detail(store, role)
