"""AI Workstation API (spec §24).

    uvicorn aiw_api.main:app --host 0.0.0.0 --port 8000

REST (see README "API" for the full list)
  POST /api/tasks                      {prompt, machine_id?, thread_id?, max_steps?, isolated?} -> task + session (or queued)
  GET  /api/tasks                      activity list;  POST /api/tasks/{id}/retry;  DELETE /api/tasks/{id}
  GET  /api/threads | /api/threads/{id}  conversations with their tasks, plans, files
  /api/routines  /api/watches  /api/memories  /api/profile  /api/settings/*  /api/secrets  /api/integrations
  /api/notifications  /api/uploads  /api/agent/answer
  GET  /api/tasks/{id}
  POST /api/sessions                   {task_id}
  GET  /api/sessions/{id}              session + events + artifacts
  GET  /api/machines | /api/machines/{id}
  POST /api/machines/{id}/start|stop|restart|snapshot|restore
  GET  /api/machines/{id}/desktop      -> noVNC URL (proxied through this API)
  GET  /api/machines/{id}/screenshot   -> PNG
  POST /api/agent/pause|resume|stop|takeover   {session_id}
  POST /api/agent/confirm              {session_id, approve}
WebSocket
  /ws/session/{session_id}             live agent events
  /ws/desktop/{machine_id}             VNC websocket proxy (noVNC connects here)
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from websockets.asyncio.client import connect as ws_connect

from . import cron
from .config import Settings
from .crypto import SecretBox
from .integrations import load_tools, openapi_to_tools
from .notify import Notifier
from .scheduler import Scheduler
from .sessions import SessionManager
from .store import Store

log = logging.getLogger("aiw.api")


class TaskIn(BaseModel):
    prompt: str
    machine_id: str = "ai-linux-001"
    thread_id: Optional[str] = None
    max_steps: int = 40
    timeout_s: float = float(os.environ.get("AIW_TASK_TIMEOUT", "900"))
    isolated: bool = False

class Answer(BaseModel):
    session_id: str
    text: str

class ThreadIn(BaseModel):
    title: str = ""

class ThreadPatch(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None

class RoutineIn(BaseModel):
    name: str
    prompt: str
    schedule: str
    machine_id: Optional[str] = None
    enabled: bool = True

class WatchIn(BaseModel):
    name: str
    url: str
    mode: str = "change"          # change | contains | price_below
    value: str = ""
    interval_min: int = 60
    enabled: bool = True

class MemoryIn(BaseModel):
    text: str

class SecretIn(BaseModel):
    value: str

class UploadIn(BaseModel):
    name: str
    content_b64: str
    machine_id: str = "ai-linux-001"

class OpenAPIIn(BaseModel):
    url: Optional[str] = None
    spec: Optional[dict] = None
    base_url: Optional[str] = None
    prefix: str = ""
    headers: dict = {}

class SessionIn(BaseModel):
    task_id: str

class Ctl(BaseModel):
    session_id: str

class Confirm(BaseModel):
    session_id: str
    approve: bool

class Snap(BaseModel):
    name: str


def build_app(settings: Optional[Settings] = None, llm_factory=None, computer_factory=None, vm_runtime=None) -> FastAPI:
    settings = settings or Settings()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    store = Store(settings.db_path)
    for m in settings.machines:
        store.upsert_machine({"id": m.id, "name": m.name or m.id, "provider": m.provider,
                              "provider_vm_id": m.provider_vm_id, "agent_endpoint": m.agent_endpoint,
                              "desktop_endpoint": m.desktop_http})
    secrets = SecretBox(store, os.path.dirname(settings.db_path) or ".")
    notifier = Notifier(store, secrets)

    if vm_runtime is None and settings.pve_enabled:
        from vm_runtime import ProxmoxProvider, VMRuntime
        vm_runtime = VMRuntime(ProxmoxProvider(), {m.id: m.provider_vm_id for m in settings.machines})
    sessions = SessionManager(store, settings, llm_factory, computer_factory, notifier=notifier, secrets=secrets,
                              vm_runtime=vm_runtime)
    scheduler = Scheduler(store, sessions, notifier)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await sessions.recover_interrupted(requeue=settings.resume_interrupted)
        except Exception as e:  # noqa: BLE001
            log.warning("recovery failed: %s", e)
        if settings.scheduler_enabled:
            scheduler.start()
        yield
        await scheduler.stop()
        for s in sessions.live.values():
            s.control.stop()

    app = FastAPI(title="AI Workstation API", version="1.1.0", lifespan=lifespan)
    app.state.store, app.state.sessions, app.state.settings = store, sessions, settings
    app.state.scheduler, app.state.notifier, app.state.secrets = scheduler, notifier, secrets

    # ------------------------------------------------------------- auth
    def _ok(token: Optional[str]) -> bool:
        return not settings.api_token or (bool(token) and hmac.compare_digest(token, settings.api_token))

    def auth(request: Request):
        h = request.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else request.cookies.get("aiw_token") or request.query_params.get("token")
        if not _ok(tok):
            raise HTTPException(401, "unauthorized")

    def ws_token(ws: WebSocket) -> Optional[str]:
        return ws.query_params.get("token") or ws.cookies.get("aiw_token")

    # ------------------------------------------------------------- tasks

    @app.post("/api/tasks", dependencies=[Depends(auth)])
    async def create_task(body: TaskIn):
        if not body.prompt.strip():
            raise HTTPException(400, "empty prompt")
        if not store.machine(body.machine_id):
            raise HTTPException(404, "machine not found")
        try:
            return await sessions.submit(body.prompt.strip(), body.machine_id, body.thread_id, max_steps=body.max_steps,
                                         timeout_s=body.timeout_s, isolated=body.isolated)
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/tasks", dependencies=[Depends(auth)])
    async def list_tasks(limit: int = 50, status: Optional[str] = None):
        rows = store.recent_tasks(min(limit, 200), status)
        threads = {}
        for r in rows:
            if r.get("thread_id") and r["thread_id"] not in threads:
                threads[r["thread_id"]] = (store.thread(r["thread_id"]) or {}).get("title")
            r["thread_title"] = threads.get(r.get("thread_id"))
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else None
        return rows

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    async def get_task(task_id: str):
        t = store.task(task_id)
        if not t:
            raise HTTPException(404)
        t["plan"] = json.loads(t["plan"]) if t.get("plan") else None
        return t

    @app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(auth)])
    async def retry_task(task_id: str):
        try:
            return await sessions.retry(task_id)
        except KeyError:
            raise HTTPException(404)

    @app.delete("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    async def cancel_task(task_id: str):
        if sessions.cancel_queued(task_id):
            return {"cancelled": True}
        live = sessions.live_for_task(task_id)
        if live and live.runner and not live.runner.done():
            await sessions.control(live.id, "stop")
            return {"stopped": True}
        raise HTTPException(409, "task is not queued or running")

    @app.get("/api/tasks/{task_id}/receipts", dependencies=[Depends(auth)])
    async def receipts(task_id: str):
        t = store.task(task_id)
        if not t:
            raise HTTPException(404)
        sid = t.get("session_id")
        out, cur = [], None
        for a in (store.actions(sid) if sid else []):
            st = a.get("status") or ""
            res = a.get("result") or {}
            if st.startswith("policy"):
                cur = {"action": a["action_type"], "payload": a.get("payload"), "level": res.get("level"),
                       "reason": res.get("reason"), "ok": None, "error": None, "duration_ms": None, "at": a["created_at"]}
                out.append(cur)
            elif st.startswith("result") and cur is not None:
                cur.update(ok=st.endswith("True"), error=res.get("error"), duration_ms=res.get("duration_ms"))
        files = [a for a in store.artifacts(sid) if a["kind"] == "file"] if sid else []
        return {"task": t, "actions": out, "files": files}

    # ------------------------------------------------------------- threads
    def _thread_full(tid: str) -> dict:
        th = store.thread(tid)
        if not th:
            raise HTTPException(404)
        tasks = store.thread_tasks(tid)
        for t in tasks:
            sid = t.get("session_id")
            t["files"] = [a for a in store.artifacts(sid) if a["kind"] == "file"] if sid else []
            live = sessions.live.get(sid) if sid else None
            t["live"] = bool(live and live.runner and not live.runner.done())
            t["pending_confirm"] = live.confirm_info if live else None
            t["pending_question"] = live.question_info if live else None
        return {**th, "tasks": tasks, "queue_held": tid in sessions.held_threads}

    @app.get("/api/threads", dependencies=[Depends(auth)])
    async def threads(archived: bool = False):
        return store.threads(archived)

    @app.post("/api/threads", dependencies=[Depends(auth)])
    async def new_thread(body: ThreadIn):
        return store.create_thread(body.title)

    @app.get("/api/threads/{tid}", dependencies=[Depends(auth)])
    async def get_thread(tid: str):
        return _thread_full(tid)

    @app.patch("/api/threads/{tid}", dependencies=[Depends(auth)])
    async def patch_thread(tid: str, body: ThreadPatch):
        if not store.thread(tid):
            raise HTTPException(404)
        store.update_thread(tid, body.title, body.archived)
        return store.thread(tid)

    @app.delete("/api/threads/{tid}", dependencies=[Depends(auth)])
    async def del_thread(tid: str):
        store.delete_thread(tid)
        return {"ok": True}

    @app.post("/api/threads/{tid}/queue/resume", dependencies=[Depends(auth)])
    async def resume_queue(tid: str):
        await sessions.resume_queue(tid)
        return _thread_full(tid)


    @app.post("/api/sessions", dependencies=[Depends(auth)])
    async def create_session(body: SessionIn):
        """Re-run an existing task in a new session."""
        t = store.task(body.task_id)
        if not t:
            raise HTTPException(404)
        r = await sessions.retry(t["id"])
        return {"session_id": r["session_id"], **r}

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def get_session(sid: str):
        s = store.session(sid)
        if not s:
            raise HTTPException(404)
        live = sessions.live.get(sid)
        return {**s, "control": live.control.state.value if live else None,
                "pending_confirm": live.confirm_info if live else None,
                "pending_question": live.question_info if live else None,
                "events": store.events(sid), "artifacts": store.artifacts(sid)}

    @app.get("/api/sessions/{sid}/replay", dependencies=[Depends(auth)])
    async def replay(sid: str):
        """Step-by-step replay: each screenshot with the decision that followed it."""
        if not store.session(sid):
            raise HTTPException(404)
        frames, cur = [], None
        for ev in store.events(sid, limit=5000):
            d = ev["data"]
            if ev["type"] == "agent.observation" and d.get("artifact"):
                cur = {"step": ev["step"], "ts": ev["ts"], "shot": d["artifact"], "tool": None, "args": None, "thought": None, "ok": None}
                frames.append(cur)
            elif ev["type"] == "agent.decision" and cur is not None:
                cur.update(tool=d.get("tool"), args=d.get("args"), thought=(d.get("thought") or "")[:400])
            elif ev["type"] == "agent.action_result" and cur is not None:
                cur.update(ok=d.get("success"), error=d.get("error"))
        return {"session_id": sid, "frames": frames}

    # ------------------------------------------------------------- agent control

    for op in ("pause", "resume", "stop", "takeover"):
        async def _ctl(body: Ctl, _op=op):
            try:
                return await sessions.control(body.session_id, _op)
            except KeyError:
                raise HTTPException(404, "session not live")
        app.post(f"/api/agent/{op}", dependencies=[Depends(auth)])(_ctl)


    @app.post("/api/agent/confirm", dependencies=[Depends(auth)])
    async def confirm(body: Confirm):
        try:
            return {"answered": sessions.answer_confirm(body.session_id, body.approve)}
        except KeyError:
            raise HTTPException(404, "session not live")

    @app.post("/api/agent/answer", dependencies=[Depends(auth)])
    async def answer(body: Answer):
        try:
            return {"answered": sessions.answer_question(body.session_id, body.text)}
        except KeyError:
            raise HTTPException(404, "session not live")

    # ------------------------------------------------------------- routines (scheduled tasks)
    @app.get("/api/routines", dependencies=[Depends(auth)])
    async def routines():
        return store.all("routines", order="created_at ASC")

    def _routine_fields(body: RoutineIn) -> dict:
        try:
            cron.validate(body.schedule)
            nxt = cron.next_run(body.schedule, time.time())
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": body.name, "prompt": body.prompt, "schedule": body.schedule.strip(),
                "machine_id": body.machine_id, "enabled": 1 if body.enabled else 0, "next_run": nxt if body.enabled else None}

    @app.post("/api/routines", dependencies=[Depends(auth)])
    async def add_routine(body: RoutineIn):
        return store.insert("routines", _routine_fields(body))

    @app.put("/api/routines/{rid}", dependencies=[Depends(auth)])
    async def put_routine(rid: str, body: RoutineIn):
        if not store.get("routines", rid):
            raise HTTPException(404)
        store.update("routines", rid, **_routine_fields(body))
        return store.get("routines", rid)

    @app.delete("/api/routines/{rid}", dependencies=[Depends(auth)])
    async def del_routine(rid: str):
        store.delete("routines", rid)
        return {"ok": True}

    @app.post("/api/routines/{rid}/run", dependencies=[Depends(auth)])
    async def run_routine(rid: str):
        r = store.get("routines", rid)
        if not r:
            raise HTTPException(404)
        res = await sessions.submit(r["prompt"], r.get("machine_id"), r.get("thread_id"), source="routine",
                                    title=f"⏰ {r['name']}")
        store.update("routines", rid, last_run=time.time(), last_task_id=res["task"]["id"], thread_id=res["thread_id"])
        return res

    # ------------------------------------------------------------- watches (page tracking)
    @app.get("/api/watches", dependencies=[Depends(auth)])
    async def watches():
        return store.all("watches", order="created_at ASC")

    def _watch_fields(body: WatchIn) -> dict:
        if body.mode not in ("change", "contains", "price_below"):
            raise HTTPException(400, "mode must be change, contains or price_below")
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "url must start with http(s)://")
        if body.mode == "price_below":
            try:
                float(body.value)
            except ValueError:
                raise HTTPException(400, "price threshold must be a number")
        return {"name": body.name, "url": body.url, "mode": body.mode, "value": body.value,
                "interval_min": max(5, body.interval_min), "enabled": 1 if body.enabled else 0}

    @app.post("/api/watches", dependencies=[Depends(auth)])
    async def add_watch(body: WatchIn):
        w = store.insert("watches", _watch_fields(body))
        return w

    @app.put("/api/watches/{wid}", dependencies=[Depends(auth)])
    async def put_watch(wid: str, body: WatchIn):
        if not store.get("watches", wid):
            raise HTTPException(404)
        store.update("watches", wid, **_watch_fields(body), next_check=None)
        return store.get("watches", wid)

    @app.delete("/api/watches/{wid}", dependencies=[Depends(auth)])
    async def del_watch(wid: str):
        store.delete("watches", wid)
        return {"ok": True}

    @app.post("/api/watches/{wid}/check", dependencies=[Depends(auth)])
    async def check_watch(wid: str):
        w = store.get("watches", wid)
        if not w:
            raise HTTPException(404)
        res = await scheduler.check_watch(w)
        return {**res, "watch": store.get("watches", wid)}

    # ------------------------------------------------------------- memory + profile
    @app.get("/api/memories", dependencies=[Depends(auth)])
    async def memories():
        return store.all("memories")

    @app.post("/api/memories", dependencies=[Depends(auth)])
    async def add_memory(body: MemoryIn):
        return store.insert("memories", {"text": body.text.strip()[:500], "source": "user"})

    @app.put("/api/memories/{mid_}", dependencies=[Depends(auth)])
    async def put_memory(mid_: str, body: MemoryIn):
        store.update("memories", mid_, text=body.text.strip()[:500])
        return store.get("memories", mid_)

    @app.delete("/api/memories/{mid_}", dependencies=[Depends(auth)])
    async def del_memory(mid_: str):
        store.delete("memories", mid_)
        return {"ok": True}

    @app.get("/api/profile", dependencies=[Depends(auth)])
    async def get_profile():
        return store.kv_get("profile", {}) or {}

    @app.put("/api/profile", dependencies=[Depends(auth)])
    async def put_profile(body: dict):
        allowed = {k: str(v)[:500] for k, v in body.items() if k in ("name", "agent_name", "tone", "about", "avatar")}
        store.kv_set("profile", allowed)
        return allowed

    # ------------------------------------------------------------- settings: models, notifications
    @app.get("/api/settings/models", dependencies=[Depends(auth)])
    async def get_models():
        m = store.kv_get("models", {}) or {}
        return {**m, "env_model": os.environ.get("OPENAI_MODEL"), "env_base_url": os.environ.get("OPENAI_BASE_URL"),
                "has_key": bool(secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY"))}

    @app.put("/api/settings/models", dependencies=[Depends(auth)])
    async def put_models(body: dict):
        m = {k: (str(body.get(k) or "").strip() or None)
             for k in ("model", "vision_model", "fallback", "subagent_model", "base_url", "thinking")}
        store.kv_set("models", m)
        return m

    @app.post("/api/models/probe", dependencies=[Depends(auth)])
    async def probe_model(body: dict):
        """Check what a model can really do through this gateway: tool calls with arguments, and vision."""
        from .probe import probe
        m = store.kv_get("models", {}) or {}
        model = (body.get("model") or "").strip()
        if not model:
            raise HTTPException(400, "model required")
        base = (body.get("base_url") or m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        key = secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
        if not key:
            return {"model": model, "error": "no API key"}
        res = await probe(base, key, model)
        probes = store.kv_get("model_probes", {}) or {}
        probes[model] = res
        store.kv_set("model_probes", probes)
        return res

    @app.get("/api/models/probes", dependencies=[Depends(auth)])
    async def model_probes():
        return store.kv_get("model_probes", {}) or {}

    @app.get("/api/models", dependencies=[Depends(auth)])
    async def list_models():
        """Model ids offered by the OpenAI-compatible gateway, for the picker."""
        m = store.kv_get("models", {}) or {}
        base = (m.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        key = secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
        if not key:
            return {"models": [], "error": "no API key"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
            return {"models": sorted(x.get("id") for x in r.json().get("data", []) if x.get("id"))}
        except Exception as e:  # noqa: BLE001
            return {"models": [], "error": str(e)[:300]}

    @app.get("/api/settings/notify", dependencies=[Depends(auth)])
    async def get_notify():
        return store.kv_get("notify", {}) or {}

    @app.put("/api/settings/notify", dependencies=[Depends(auth)])
    async def put_notify(body: dict):
        cfg = {k: (str(body.get(k) or "").strip() or None) for k in ("bark_url", "ntfy_url", "webhook_url")}
        store.kv_set("notify", cfg)
        return cfg

    @app.post("/api/settings/notify/test", dependencies=[Depends(auth)])
    async def test_notify():
        return await notifier.notify("test", "AI Workstation", "通知测试成功")

    # ------------------------------------------------------------- secrets (encrypted, masked)
    @app.get("/api/secrets", dependencies=[Depends(auth)])
    async def list_secrets():
        return secrets.list()

    @app.put("/api/secrets/{name}", dependencies=[Depends(auth)])
    async def put_secret(name: str, body: SecretIn):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name):
            raise HTTPException(400, "bad secret name")
        secrets.put(name, body.value)
        return {"name": name, "saved": True}

    @app.delete("/api/secrets/{name}", dependencies=[Depends(auth)])
    async def del_secret(name: str):
        secrets.delete(name)
        return {"ok": True}

    # ------------------------------------------------------------- notifications
    @app.get("/api/notifications", dependencies=[Depends(auth)])
    async def notifications(unread: bool = False):
        return store.all("notifications", "read=0" if unread else "1=1", limit=100)

    @app.post("/api/notifications/read", dependencies=[Depends(auth)])
    async def read_notifications(body: dict):
        ids = body.get("ids")
        if ids:
            for i in ids:
                store.update("notifications", i, read=1)
        else:
            store._x("UPDATE notifications SET read=1")
        return {"ok": True}

    # ------------------------------------------------------------- integrations (HTTP / OpenAPI / MCP)
    @app.get("/api/integrations", dependencies=[Depends(auth)])
    async def get_integrations():
        return {"http_tools": store.kv_get("http_tools", []) or [], "mcp_servers": store.kv_get("mcp_servers", []) or []}

    @app.put("/api/integrations", dependencies=[Depends(auth)])
    async def put_integrations(body: dict):
        if "http_tools" in body:
            for t in body["http_tools"]:
                if not t.get("name") or not t.get("url"):
                    raise HTTPException(400, "each http tool needs name and url")
            store.kv_set("http_tools", body["http_tools"])
        if "mcp_servers" in body:
            for m_ in body["mcp_servers"]:
                if not m_.get("name") or not m_.get("url"):
                    raise HTTPException(400, "each MCP server needs name and url")
            store.kv_set("mcp_servers", body["mcp_servers"])
        return await get_integrations()

    @app.post("/api/integrations/openapi", dependencies=[Depends(auth)])
    async def import_openapi(body: OpenAPIIn):
        spec = body.spec
        if not spec and body.url:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(body.url)
                r.raise_for_status()
            try:
                spec = r.json()
            except ValueError:
                raise HTTPException(400, "OpenAPI spec must be JSON")
        if not spec:
            raise HTTPException(400, "give url or spec")
        new = openapi_to_tools(spec, body.base_url, body.headers, body.prefix)
        cur = [t for t in (store.kv_get("http_tools", []) or []) if t["name"] not in {n["name"] for n in new}]
        store.kv_set("http_tools", cur + new)
        return {"imported": len(new), "tools": [t["name"] for t in new]}

    @app.get("/api/integrations/tools", dependencies=[Depends(auth)])
    async def integration_tools():
        tools = await load_tools(store, secrets)
        return [{"name": k, "description": v[0]["description"][:200]} for k, v in tools.items()]

    # ------------------------------------------------------------- uploads (document workflow)
    @app.post("/api/uploads", dependencies=[Depends(auth)])
    async def upload(body: UploadIn):
        name = os.path.basename(body.name).replace("/", "_")[:120] or "upload.bin"
        try:
            raw = base64.b64decode(body.content_b64)
        except ValueError:
            raise HTTPException(400, "bad base64")
        if len(raw) > 40 * 1024 * 1024:
            raise HTTPException(413, "max 40 MB")
        d = os.path.join(settings.artifacts_dir, "uploads")
        os.makedirs(d, exist_ok=True)
        local = os.path.join(d, f"{int(time.time())}-{name}")
        with open(local, "wb") as f:
            f.write(raw)
        r = await sessions.computer(body.machine_id).execute("files.upload", {"path": f"uploads/{name}", "content_b64": body.content_b64},
                                                            observe=False, timeout=120)
        if not r.success:
            raise HTTPException(502, f"VM upload failed: {r.error}")
        return {"name": name, "size": len(raw), "vm_path": f"~/workspace/uploads/{name}", "artifact": local}

    # ------------------------------------------------------------- machines
    @app.get("/api/machines", dependencies=[Depends(auth)])
    async def machines():
        return store.machines()

    @app.get("/api/machines/{mid}", dependencies=[Depends(auth)])
    async def machine(mid: str):
        m = store.machine(mid)
        if not m:
            raise HTTPException(404)
        out = {**m}
        if vm_runtime:
            try:
                st = await vm_runtime.status(mid)
                store.set_machine_status(mid, st.state.value, st.ip)
                out["vm"] = st.model_dump()
            except Exception as e:  # noqa: BLE001
                out["vm_error"] = str(e)
        out["agent"] = {"ok": False, "error": "no response"}
        for attempt in range(2):  # one retry: a single slow/reconnecting probe is not "offline"
            try:
                h = await asyncio.wait_for(sessions.computer(mid).health(), 8)
                out["agent"] = h.result if h.success else {"ok": False, "error": h.error}
            except Exception as e:  # noqa: BLE001
                out["agent"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if out["agent"].get("ok"):
                break
        return out

    def _need_vm():
        if not vm_runtime:
            raise HTTPException(503, "VM runtime not configured (set PVE_HOST/PVE_TOKEN_ID/PVE_TOKEN_SECRET)")

    for op in ("start", "stop", "restart"):
        async def _vm(mid: str, _op=op):
            _need_vm()
            if sessions.busy_session(mid) and _op != "start":
                raise HTTPException(409, "stop the running agent session first")
            return await getattr(vm_runtime, _op)(mid)
        app.post(f"/api/machines/{{mid}}/{op}", dependencies=[Depends(auth)])(_vm)


    @app.post("/api/machines/{mid}/snapshot", dependencies=[Depends(auth)])
    async def snapshot(mid: str, body: Snap):
        _need_vm()
        return await vm_runtime.snapshot(mid, body.name)

    @app.post("/api/machines/{mid}/restore", dependencies=[Depends(auth)])
    async def restore(mid: str, body: Snap):
        _need_vm()
        if sessions.busy_session(mid):
            raise HTTPException(409, "stop the running agent session first")
        return await vm_runtime.restore(mid, body.name)

    @app.get("/api/machines/{mid}/screenshot", dependencies=[Depends(auth)])
    async def screenshot(mid: str):
        r = await sessions.computer(mid).screenshot()
        if not r.success:
            raise HTTPException(502, r.error)
        return Response(base64.b64decode(r.screenshot), media_type="image/png")

    @app.get("/api/machines/{mid}/desktop", dependencies=[Depends(auth)])
    async def desktop(mid: str, view_only: bool = True):
        if not store.machine(mid):
            raise HTTPException(404)
        path = f"ws/desktop/{mid}"
        vnc_pw = os.environ.get("AIW_VNC_PASSWORD", "")
        pw_q = f"&password={quote(vnc_pw, safe='')}" if vnc_pw else ""
        return {"novnc_url": f"/desktop/{mid}/vnc.html?autoconnect=1&resize=scale&reconnect=1"
                              f"&view_only={'1' if view_only else '0'}&path={path}{pw_q}",
                "ws_path": f"/{path}"}

    # noVNC static files are served by the VM's websockify (--web); proxy them
    # so the browser only ever talks to this API.
    @app.get("/desktop/{mid}/{path:path}", dependencies=[Depends(auth)])
    async def desktop_static(mid: str, path: str):
        m = store.machine(mid)
        if not m:
            raise HTTPException(404)
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{m['desktop_endpoint']}/{path}")
        return Response(r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/octet-stream"))

    @app.websocket("/ws/desktop/{mid}")
    async def ws_desktop(ws: WebSocket, mid: str):
        m = store.machine(mid)
        if not _ok(ws_token(ws)) or not m:
            await ws.close(code=4401); return
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)
        upstream_url = m["desktop_endpoint"].replace("http", "ws", 1) + "/websockify"
        try:
            async with ws_connect(upstream_url, subprotocols=["binary"], max_size=None) as up:
                async def c2u():
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        await up.send(msg.get("bytes") or msg.get("text"))

                async def u2c():
                    async for data in up:
                        if isinstance(data, bytes):
                            await ws.send_bytes(data)
                        else:
                            await ws.send_text(data)
                done, pending = await asyncio.wait([asyncio.create_task(c2u()), asyncio.create_task(u2c())],
                                                   return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
        except Exception as e:  # noqa: BLE001
            log.warning("desktop proxy %s: %s", mid, e)
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- session stream
    @app.websocket("/ws/session/{sid}")
    async def ws_session(ws: WebSocket, sid: str):
        if not _ok(ws_token(ws)):
            await ws.close(code=4401); return
        await ws.accept()
        srow = store.session(sid) or {}
        for ev in store.events(sid, limit=5000):  # replay history first
            await ws.send_json({"kind": "event", "task_id": srow.get("task_id"), **ev})
        live = sessions.live.get(sid)
        if not live:
            await ws.close(); return
        live.subscribers.add(ws)
        try:
            while True:
                msg = await ws.receive_json()  # accepts control ops from the console too
                op = msg.get("op")
                if op in ("pause", "resume", "stop", "takeover"):
                    await sessions.control(sid, op)
                elif op == "confirm":
                    sessions.answer_confirm(sid, bool(msg.get("approve")))
                elif op == "answer":
                    sessions.answer_question(sid, str(msg.get("text") or ""))
        except WebSocketDisconnect:
            pass
        finally:
            live.subscribers.discard(ws)

    # ------------------------------------------------------------- artifacts + web console
    @app.get("/api/artifacts", dependencies=[Depends(auth)])
    async def artifact(path: str, download: bool = False):
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(settings.artifacts_dir) + os.sep) or not os.path.exists(real):
            raise HTTPException(404)
        return FileResponse(real, filename=os.path.basename(real) if download else None)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        """Console-wide stream: notifications, task status, plans (drives lists and badges)."""
        if not _ok(ws_token(ws)):
            await ws.close(code=4401); return
        await ws.accept()
        notifier.subscribers.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            notifier.subscribers.discard(ws)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    if os.path.isdir(settings.web_dir):
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


app = build_app() if os.environ.get("AIW_NO_AUTOAPP") != "1" else None
