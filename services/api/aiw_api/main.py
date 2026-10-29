"""AI Workstation API (spec §24).

    uvicorn aiw_api.main:app --host 0.0.0.0 --port 8000

REST (see README "API" for the full list)
  POST /api/tasks                      {prompt, machine_id?, thread_id?, max_steps?, isolated?} -> task + session (or queued)
  GET  /api/tasks                      activity list;  POST /api/tasks/{id}/retry;  DELETE /api/tasks/{id}
  GET  /api/threads | /api/threads/{id}  conversations with their tasks, plans, files
  /api/routines  /api/watches  /api/memories  /api/profile  /api/settings/*  /api/secrets  /api/integrations
  /api/notifications  /api/uploads  /api/agent/answer
  /api/chat (history) · POST /api/chat/send (SSE: user/route/delta/message/task/done) · /api/chat/messages/{id}/answer|to_task
  GET  /api/tasks/{id}
  POST /api/sessions                   {task_id}
  GET  /api/sessions/{id}              session + events + artifacts
  GET  /api/machines | /api/machines/{id}
  PUT  /api/machines/{id}/name          -> rename (display name shared by web + iOS; "" = default 云电脑 N)
  POST /api/machines/{id}/start|stop|restart|snapshot|restore
  GET  /api/machines/{id}/desktop      -> noVNC URL (proxied through this API)
  GET  /api/machines/{id}/screenshot   -> PNG
  POST /api/agent/pause|resume|stop|takeover   {session_id}
  POST /api/agent/confirm              {session_id, approve, scope?: "task"}
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
import uuid
from urllib.parse import quote
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from websockets.asyncio.client import connect as ws_connect

from . import cron
from . import uploads as _uploads
from .config import Settings
from .crypto import SecretBox
from .integrations import load_tools, openapi_to_tools
from .notify import Notifier
from .scheduler import Scheduler
from .sessions import SessionManager
from .store import Store

log = logging.getLogger("aiw.api")
CHECK_TIMEOUT_S = int(os.environ.get("AIW_WATCH_CHECK_TIMEOUT_S", "240"))


def _json_safe(o):
    """Anything -> plain JSON values (NaN/inf -> None, unknown types -> str): a response that can always be sent."""
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_json_safe(v) for v in o]
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if o is None or isinstance(o, (str, int, bool)):
        return o
    return str(o)


class TaskIn(BaseModel):
    prompt: str
    machine_id: str = "ai-linux-001"
    thread_id: Optional[str] = None
    max_steps: Optional[int] = None   # None = AIW_TASK_MAX_STEPS (default 0: no step budget)
    timeout_s: Optional[float] = None  # None = AIW_TASK_TIMEOUT (default 0: no time budget)
    isolated: bool = False
    new_task: bool = False             # 3.8.5: a message in a thread continues its task unless this is set

class ContinueIn(BaseModel):          # 3.8.5: 继续 / 我已经上传了 / 补充要求 / 修改任务
    text: str = ""
    attachments: list = []
    machine_id: Optional[str] = None

class EditIn(BaseModel):
    prompt: str

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
    name: str = ""
    url: str
    mode: str = "change"          # change | contains | price_below | shop (京东/淘宝/天猫, see shopwatch.py)
    value: str = ""               # shop: target price
    interval_min: int = 60
    enabled: bool = True
    quantity: int = 1             # shop only from here on
    sku: str = ""
    address_note: str = ""
    auto_order: bool = False
    machine_id: Optional[str] = None

class MemoryIn(BaseModel):
    text: str

class SecretIn(BaseModel):
    value: str

class UploadIn(BaseModel):
    name: str
    content_b64: str
    machine_id: str = "ai-linux-001"
    local_only: bool = False      # a chat attachment: kept on the API host only (the chat reads it; a task uploads it)
    thread_id: Optional[str] = None   # 3.8.5: which conversation / task the file is for (kept with the upload)
    task_id: Optional[str] = None

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
    scope: Optional[str] = None   # "task": also allow the rest of this task's confirmations (not payments/posts)

class Snap(BaseModel):
    name: str


_HOST_RE = __import__("re").compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _vm_endpoints(ip: str) -> dict:
    return {"agent_endpoint": f"ws://{ip}:{os.environ.get('VM_AGENT_PORT', '8765')}",
            "desktop_endpoint": f"http://{ip}:{os.environ.get('AIW_NOVNC_PORT', '6080')}"}


def _set_vm_ip(store, mid: str, ip: str) -> dict:
    """Point a machine at a new address: the agent WebSocket and the noVNC proxy both follow."""
    m = store.machine(mid)
    if not m:
        raise KeyError(mid)
    m = {**m, **_vm_endpoints(ip), "ip": ip}
    store.upsert_machine(m)
    store.set_machine_status(mid, m.get("status") or "unknown", ip)
    return m


def _apply_saved_vm_ip(store, settings) -> None:
    """The address saved in 设置 beats AIW_VM_IP from .env, so a new DHCP lease needs no .env edit or restart."""
    net = store.kv_get("vm_net", {}) or {}
    if net.get("ip") and settings.machines:
        _set_vm_ip(store, settings.machines[0].id, net["ip"])


def build_app(settings: Optional[Settings] = None, llm_factory=None, computer_factory=None, vm_runtime=None) -> FastAPI:
    settings = settings or Settings()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    store = Store(settings.db_path)
    for m in settings.machines:
        store.upsert_machine({"id": m.id, "name": m.name or m.id, "provider": m.provider,
                              "provider_vm_id": m.provider_vm_id, "agent_endpoint": m.agent_endpoint,
                              "desktop_endpoint": m.desktop_http})
    _apply_saved_vm_ip(store, settings)
    secrets = SecretBox(store, os.path.dirname(settings.db_path) or ".")
    notifier = Notifier(store, secrets)

    if vm_runtime is None and settings.pve_enabled:
        from vm_runtime import ProxmoxProvider, VMRuntime
        vm_runtime = VMRuntime(ProxmoxProvider(), {m.id: m.provider_vm_id for m in settings.machines})
    sessions = SessionManager(store, settings, llm_factory, computer_factory, notifier=notifier, secrets=secrets,
                              vm_runtime=vm_runtime)
    scheduler = Scheduler(store, sessions, notifier)
    from .cloud import CloudError, CloudManager
    cloud = CloudManager(store, secrets, settings.agent_token, on_change=sessions.drop_computer)
    sessions.cloud = cloud

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await sessions.recover_interrupted(requeue=settings.resume_interrupted)
        except Exception as e:  # noqa: BLE001
            log.warning("recovery failed: %s", e)
        if settings.scheduler_enabled:
            scheduler.start()
        for e in cloud.list():  # a setup interrupted by an API restart carries on
            if e.get("status") in ("adding", "creating", "installing"):
                cloud.start_setup(e["id"])
        yield
        await scheduler.stop()
        for s in sessions.live.values():
            s.control.stop()

    app = FastAPI(title="AI Workstation API", version="1.1.0", lifespan=lifespan)
    app.state.store, app.state.sessions, app.state.settings = store, sessions, settings
    app.state.cloud = cloud
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

    from . import xhs   # 小红书运营: settings, approval queue, pause (/api/xhs/*)
    xhs.mount(app, store, sessions, notifier, auth)
    from . import remote_input   # 3.8.4: takeover input straight to the VM (/ws/input/{mid}, /api/machines/{mid}/input)
    remote_input.mount(app, store, sessions, auth, lambda ws: _ok(ws_token(ws)))
    from . import browseros   # BrowserOS neo per computer (/api/browseros*)
    browseros.mount(app, store, sessions, auth)
    from . import chat as _chat   # the persistent 小秘 chat (/api/chat*): routing, task cards, escalations in the chat
    chat = _chat.Chat(store, sessions, notifier, settings, secrets)
    app.state.chat = chat
    _chat.mount(app, chat, auth)
    from . import memory as _memory   # long-term memory: profile / facts / decisions for the chat and every task
    mem = _memory.Memory(store, sessions, llm=chat.llm)
    chat.memory = mem
    sessions.memory = mem
    app.state.memory = mem
    _memory.mount(app, mem, auth)
    from . import lessons as _lessons   # 3.8.5 self-evolution: Aiw 学到的经验 (/api/lessons)
    # a test's scripted llm_factory has no small model: lessons then learn by the built-in rules only
    lessons = _lessons.Lessons(store, llm=None if llm_factory else chat.llm)
    sessions.lessons = lessons
    app.state.lessons = lessons
    _lessons.mount(app, lessons, auth)
    _uploads.ensure_table(store)
    from . import greeting as _greeting   # 球球播报 on the home screen (/api/greeting)
    _greeting.mount(app, store, chat, mem, auth)

    # ------------------------------------------------------------- tasks

    @app.post("/api/tasks", dependencies=[Depends(auth)])
    async def create_task(body: TaskIn):
        if not body.prompt.strip():
            raise HTTPException(400, "empty prompt")
        if not store.machine(body.machine_id):
            raise HTTPException(404, "machine not found")
        try:
            return await sessions.submit(body.prompt.strip(), body.machine_id, body.thread_id, max_steps=body.max_steps,
                                         timeout_s=body.timeout_s, isolated=body.isolated, link_thread=not body.new_task)
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

    @app.post("/api/tasks/{task_id}/continue", dependencies=[Depends(auth)])
    async def continue_task(task_id: str, body: ContinueIn = Body(default=ContinueIn())):
        """继续 the SAME task (3.8.5): its plan, steps done, files and attachments go with it. While it runs, the
        words are an added instruction (or the answer to its open question)."""
        if body.machine_id and not store.machine(body.machine_id):
            raise HTTPException(404, "machine not found")
        try:
            return await sessions.continue_task(task_id, body.text, body.attachments, body.machine_id)
        except KeyError:
            raise HTTPException(404)
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/tasks/{task_id}/instruct", dependencies=[Depends(auth)])
    async def instruct_task(task_id: str, body: ContinueIn):
        """补充要求 while the task runs (a stopped task is continued with the words)."""
        if not store.task(task_id):
            raise HTTPException(404)
        try:
            res = await sessions.instruct(task_id, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        sessions._link_attachments(body.attachments, task_id, (store.task(task_id) or {}).get("thread_id"))
        return res

    @app.post("/api/tasks/{task_id}/edit", dependencies=[Depends(auth)])
    async def edit_task(task_id: str, body: EditIn):
        """修改任务: new instructions for the same task, re-run with its history, files and attachments."""
        try:
            return await sessions.edit_task(task_id, body.prompt)
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/tasks/{task_id}/runs", dependencies=[Depends(auth)])
    async def task_runs(task_id: str):
        """Every run of one task (root first) + its attachments: the task page shows them as one task."""
        from . import continuation as _cont
        if not store.task(task_id):
            raise HTTPException(404)
        runs = _cont.chain(store, task_id)
        root = runs[0] if runs else store.task(task_id)
        ups = _uploads.for_task(store, [r["id"] for r in runs], root.get("thread_id"), [r.get("prompt") or "" for r in runs])
        return {"root": root["id"], "instructions": _cont.instructions(store, root), "runs": runs,
                "attachments": [{"name": u["name"], "size": u["size"], "vm_path": f"~/workspace/uploads/{u['name']}",
                                 "kind": u.get("kind")} for u in ups]}

    @app.delete("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    async def cancel_task(task_id: str):
        if sessions.cancel_queued(task_id):
            return {"cancelled": True}
        live = sessions.live_for_task(task_id)
        if live and live.runner and not live.runner.done():
            await sessions.control(live.id, "stop")
            return {"stopped": True}
        raise HTTPException(409, "task is not queued or running")

    @app.post("/api/tasks/{task_id}/send_now", dependencies=[Depends(auth)])
    async def send_now(task_id: str, body: dict = Body(default={})):
        """立即发送 a queued message: starts it when its computer is idle, else puts it first in the queue
        ({"stop_current": true} also stops the task the computer is busy with, so it starts right after)."""
        try:
            return await sessions.send_now(task_id, bool((body or {}).get("stop_current")))
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(409, str(e))

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

    @app.get("/api/tasks/{task_id}/export", dependencies=[Depends(auth)])
    async def export_task(task_id: str):
        """Everything needed to debug a run, no screenshots or secrets: task, model steps with timing, tool results."""
        base = await receipts(task_id)
        sid = base["task"].get("session_id")
        steps = []
        for e in (store.events(sid, limit=2000) if sid else []):
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            d = {k: v for k, v in d.items() if k not in ("screenshot", "screenshot_b64", "image", "content_b64")}
            steps.append({"type": e.get("type"), "step": e.get("step"), "at": e.get("ts"), **d})
        settings = store.kv_get("models", {}) or {}
        return {"exported_at": time.time(), "server_version": os.environ.get("AIW_VERSION", "dev")[:7], "task": base["task"], "models": {k: v for k, v in settings.items() if "key" not in k.lower()},
                "events": steps, "receipts": base["actions"], "files": [f.get("path") for f in base["files"]]}

    # ------------------------------------------------------------- threads
    def _thread_full(tid: str) -> dict:
        th = store.thread(tid)
        if not th:
            raise HTTPException(404)
        tasks = store.thread_tasks(tid)
        for t in tasks:
            if t.get("continues"):   # 3.8.5: a 继续 / 修改 run of the task above (shown as the same task)
                t["run_mode"] = (store.kv_get(f"task_run:{t['id']}", {}) or {}).get("mode") or "continue"
            sid = t.get("session_id")
            t["files"] = [a for a in store.artifacts(sid) if a["kind"] == "file"] if sid else []
            live = sessions.live.get(sid) if sid else None
            t["live"] = bool(live and live.runner and not live.runner.done())
            t["pending_confirm"] = live.confirm_info if live else None
            t["pending_question"] = live.question_info if live else None
            if t.get("status") == "queued":   # why it waits (iOS / web show it under the queued message)
                t["queue"] = sessions.queue_info(t)
            kids = sessions.children_view(t["id"])
            if kids:   # pieces this task dispatched to other computers (each with its own thread)
                t["children"] = kids
        if th.get("parent_thread_id"):
            pt = store.thread(th["parent_thread_id"]) or {}
            th["parent_title"] = pt.get("title")
        roots = [t for t in tasks if not t.get("continues")]
        instr = {r["id"]: store.kv_get(f"task_instructions:{r['id']}") for r in roots}
        ups = [{"name": u["name"], "size": u["size"], "vm_path": f"~/workspace/uploads/{u['name']}", "task_id": u.get("task_id")}
               for u in _uploads.thread_uploads(store, tid)]
        return {**th, "tasks": tasks, "queue_held": tid in sessions.held_threads, "attachments": ups,
                "instructions": {k: v for k, v in instr.items() if v}}   # 修改任务: the task's current instructions

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
        for t in store.thread_tasks(tid):  # stop what is still queued or running before its records go
            if not sessions.cancel_queued(t["id"]):
                live = sessions.live_for_task(t["id"])
                if live and live.runner and not live.runner.done():
                    await sessions.control(live.id, "stop")
        for t in store.thread_tasks(tid):   # child threads of dispatched pieces go with their parent
            await sessions.stop_children(t["id"])
            for c in store.children(t["id"]):
                if c.get("thread_id"):
                    sessions.held_threads.discard(c["thread_id"])
                    store.delete_thread(c["thread_id"])
        sessions.held_threads.discard(tid)
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
            args = (body.session_id, body.approve) + ((body.scope,) if body.scope else ())
            return {"answered": sessions.answer_confirm(*args)}
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
        from . import shopwatch
        if body.mode == shopwatch.MODE or (body.mode == "price_below" and shopwatch.is_shop_url(body.url)):
            shopwatch.ensure_columns(store)
            try:
                f = shopwatch.watch_fields(body.model_dump(), default_machine=shopwatch.default_machine(store) or
                                           (settings.machines[0].id if settings.machines else None))
            except ValueError as e:
                raise HTTPException(400, str(e))
            if f.get("machine_id") and not store.machine(f["machine_id"]):
                mid = shopwatch.resolve_machine(store, f["machine_id"])   # 3.8.2: a name ('本地vm') works too
                if not mid:
                    raise HTTPException(400, f"没有这台电脑：{f['machine_id']}")
                f["machine_id"] = mid
            return f
        if not body.name:
            raise HTTPException(400, "name is required")
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
        f = _watch_fields(body)
        if f.get("mode") == "shop":  # a changed target or link starts a fresh transition, never a stale order
            f.update(below=0, fail_count=0)
        store.update("watches", wid, **f, next_check=None)
        return store.get("watches", wid)

    @app.delete("/api/watches/{wid}", dependencies=[Depends(auth)])
    async def del_watch(wid: str):
        store.delete("watches", wid)
        return {"ok": True}

    CHECK_JOBS: dict[str, dict] = {}   # v6: 立即检查 jobs (job id -> stage, stages, result), newest 50 kept

    async def _run_check(wid: str, w: dict, job: Optional[dict] = None) -> tuple[dict, int]:
        def on_stage(ev: dict):
            if job is not None:
                job.update(stage=ev.get("stage"), label=ev.get("label"), updated_at=time.time())
                job["stages"].append({"stage": ev.get("stage"), "label": ev.get("label"),
                                      "at_ms": int((time.time() - job["started_at"]) * 1000)})
        # always a JSON body (the user saw an empty reply from curl): a crash or a hang becomes {"ok": false, ...}
        try:
            coro = scheduler.check_watch(w, manual=True, progress=on_stage) if job is not None else \
                scheduler.check_watch(w, manual=True)
            res = await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT_S)
            status = (res.get("result") or {}).get("status") or ("ok" if res.get("ok") else res.get("waiting") or "error")
            body = {**res, "status": status, "note": res.get("note") or (res.get("result") or {}).get("note") or res.get("error") or ""}
            code = 200
        except asyncio.TimeoutError:
            log.warning("watch check %s timed out after %ss", wid, CHECK_TIMEOUT_S)
            body = {"ok": False, "status": "timeout", "note": f"检查超过 {CHECK_TIMEOUT_S} 秒没有结束（电脑或浏览器没响应）",
                    "error": "timeout"}
            code = 200
        except Exception as e:  # noqa: BLE001
            log.exception("watch check %s failed", wid)
            body = {"ok": False, "status": "error", "note": "检查时服务器出错，详情见服务器日志", "error": f"{type(e).__name__}: {e}"[:300]}
            code = 500
        try:
            body["watch"] = store.get("watches", wid)
        except Exception:  # noqa: BLE001
            body["watch"] = None
        body.setdefault("probe_version", (body.get("diag") or {}).get("probe_version"))
        body["server_version"] = os.environ.get("AIW_VERSION", "dev")[:7]
        return _json_safe(body), code

    @app.post("/api/watches/{wid}/check", dependencies=[Depends(auth)])
    async def check_watch(wid: str, request: Request):
        w = store.get("watches", wid)
        if not w:
            raise HTTPException(404)
        if request.query_params.get("async") in ("1", "true", "yes"):
            # v6: answer at once with a job id; GET .../check/{job} shows the stage until it is done
            running = next((j for j in CHECK_JOBS.values() if j["watch_id"] == wid and j["state"] == "running"), None)
            if running:
                return JSONResponse(_json_safe({"ok": True, "job": {k: v for k, v in running.items() if not k.startswith("_")}}))
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "watch_id": wid, "state": "running", "stage": "start", "label": "开始检查",
                   "stages": [{"stage": "start", "label": "开始检查", "at_ms": 0}], "started_at": time.time(),
                   "updated_at": time.time(), "result": None}
            CHECK_JOBS[jid] = job
            for old in sorted(CHECK_JOBS.values(), key=lambda j: j["started_at"])[:-50]:
                CHECK_JOBS.pop(old["id"], None)

            async def runner():
                body, code = await _run_check(wid, w, job)
                job.update(state="done", result=body, code=code, finished_at=time.time(), stage="done",
                           label="完成：" + _check_summary(body))
                job["stages"].append({"stage": "done", "label": job["label"],
                                      "at_ms": int((job["finished_at"] - job["started_at"]) * 1000)})
            job["_task"] = asyncio.ensure_future(runner())
            return {"ok": True, "job": {k: v for k, v in job.items() if not k.startswith("_")}}
        body, code = await _run_check(wid, w)
        return JSONResponse(body, status_code=code)

    def _check_summary(body: dict) -> str:
        r = body.get("result") or {}
        if r.get("status") == "ok" and r.get("effective") is not None:
            from . import shopwatch as _sw
            return _sw.price_line(r) or "正常"
        if body.get("ok") and not r:
            return "触发提醒" if body.get("triggered") else "没有新变化"
        return str(body.get("note") or body.get("status") or "失败")[:120]

    @app.get("/api/watches/{wid}/check/{jid}", dependencies=[Depends(auth)])
    async def check_job(wid: str, jid: str):
        job = CHECK_JOBS.get(jid)
        if not job or job["watch_id"] != wid:
            raise HTTPException(404, "没有这个检查（服务器重启过？）")
        return JSONResponse(_json_safe({k: v for k, v in job.items() if not k.startswith("_")}))

    @app.get("/api/watches/{wid}/diag", dependencies=[Depends(auth)])
    async def watch_diag(wid: str):
        from . import shopwatch as _sw
        if not store.get("watches", wid):
            raise HTTPException(404)
        d = _sw.get_diag(store, wid)
        if not d:
            raise HTTPException(404, "还没有诊断信息：点一次「立即检查」")
        return JSONResponse(_json_safe(d))

    @app.get("/api/watches/{wid}/diag.jpg", dependencies=[Depends(auth)])
    async def watch_diag_shot(wid: str):
        from . import shopwatch as _sw
        p = _sw.diag_shot_path(sessions, wid)
        if not store.get("watches", wid) or not p.exists():
            raise HTTPException(404)
        return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------- memory + profile
    # /api/memories* lives in memory.py (kinds, superseded, search); mounted next to the chat above

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
             for k in ("model", "vision_model", "fallback", "subagent_model", "image_model", "video_model", "base_url", "thinking",
                       "chat_model", "small_model", "embed_model")}
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

    # ------------------------------------------------------------- settings: VM address
    def _main_mid() -> str:
        return settings.machines[0].id if settings.machines else "ai-linux-001"

    def _repoint(ip: str) -> dict:
        mid = _main_mid()
        if sessions.busy_session(mid):
            raise HTTPException(409, "有任务正在运行，先停止任务再改 IP")
        m = _set_vm_ip(store, mid, ip)
        sessions.drop_computer(mid)  # the next call opens a WebSocket to the new address
        return m

    @app.get("/api/settings/vm", dependencies=[Depends(auth)])
    async def get_vm_net():
        net = store.kv_get("vm_net", {}) or {}
        m = store.machine(_main_mid()) or {}
        return {"ip": net.get("ip"), "auto": bool(net.get("auto")), "env_ip": os.environ.get("AIW_VM_IP"),
                "agent_endpoint": m.get("agent_endpoint"), "pve": bool(vm_runtime)}

    @app.put("/api/settings/vm", dependencies=[Depends(auth)])
    async def put_vm_net(body: dict):
        ip = str(body.get("ip") or "").strip()
        if ip and not _HOST_RE.match(ip):
            raise HTTPException(400, "IP 或主机名格式不对")
        net = {"ip": ip or None, "auto": bool(body.get("auto"))}
        target = ip or os.environ.get("AIW_VM_IP") or "127.0.0.1"   # empty = back to .env
        m = _repoint(target)
        store.kv_set("vm_net", net)
        return {**net, "agent_endpoint": m["agent_endpoint"]}

    @app.post("/api/settings/vm/detect", dependencies=[Depends(auth)])
    async def detect_vm_ip():
        """Ask Proxmox (qemu-guest-agent) for the VM's current IPv4."""
        _need_vm()
        st = await vm_runtime.status(_main_mid())
        if not st.ip:
            raise HTTPException(404, "PVE 没有返回 IP：虚拟机要开机，并安装 qemu-guest-agent")
        return {"ip": st.ip}

    async def _follow_pve_ip(mid: str, ip: Optional[str]) -> None:
        net = store.kv_get("vm_net", {}) or {}
        m = store.machine(mid) or {}
        if (net.get("auto") and ip and mid == _main_mid() and not sessions.busy_session(mid)
                and _vm_endpoints(ip)["agent_endpoint"] != m.get("agent_endpoint")):
            _set_vm_ip(store, mid, ip)
            sessions.drop_computer(mid)
            store.kv_set("vm_net", {**net, "ip": ip})

    # ------------------------------------------------------------- cloud computers (InstaBox)
    @app.get("/api/cloud", dependencies=[Depends(auth)])
    async def cloud_list():
        names = {m["id"]: m for m in store.machines()}
        out = []
        for c in cloud.list():   # the display name (云电脑 N or the user's own) wins over the name typed when adding
            m = names.get(c["id"]) or {}
            out.append({**c, "raw_name": c.get("name"), "name": m.get("name") or c.get("name"),
                        "default_name": m.get("default_name"), "custom_name": m.get("custom_name"), "order": m.get("order")})
        return out

    @app.post("/api/cloud/instabox/machines", dependencies=[Depends(auth)])
    async def cloud_remote_machines(body: dict):
        """The machines an InstaBox key can see, so 设置 can offer 'use an existing one'."""
        from .cloud import InstaBox
        key = str(body.get("api_key") or "").strip()
        if not key:
            raise HTTPException(400, "api_key required")
        try:
            ib = InstaBox(key)
            me, ms = await ib.me(), await ib.machines()
        except CloudError as e:
            raise HTTPException(400, str(e))
        used = {e.get("remote_id") for e in cloud.entries().values()}
        return {"account": me.get("email") or me.get("login"), "allowed_specs": me.get("allowedSpecs"),
                "machines": [{"id": m["id"], "name": m.get("name"), "state": m.get("state"),
                              "template": (m.get("template") or {}).get("id"), "spec": m.get("spec"),
                              "in_use": m["id"] in used} for m in ms]}

    @app.post("/api/cloud", dependencies=[Depends(auth)])
    async def cloud_add(body: dict):
        if str(body.get("provider") or "instabox") != "instabox":
            raise HTTPException(400, "only InstaBox is supported for now")
        try:
            return await cloud.add(str(body.get("name") or "").strip() or "instabox", str(body.get("api_key") or ""),
                                   str(body.get("remote_id") or "").strip() or None)
        except CloudError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/cloud/{mid}/setup", dependencies=[Depends(auth)])
    async def cloud_setup(mid: str):
        if not cloud.is_cloud(mid):
            raise HTTPException(404)
        cloud.start_setup(mid)
        return {"ok": True}

    @app.delete("/api/cloud/{mid}", dependencies=[Depends(auth)])
    async def cloud_remove(mid: str):
        if not cloud.is_cloud(mid):
            raise HTTPException(404)
        if sessions.busy_session(mid):
            raise HTTPException(409, "stop the running agent session first")
        cloud.remove(mid)   # only forgets it here; the InstaBox machine itself is kept
        return {"ok": True}

    @app.get("/api/settings/notify", dependencies=[Depends(auth)])
    async def get_notify():
        return store.kv_get("notify", {}) or {}

    @app.put("/api/settings/notify", dependencies=[Depends(auth)])
    async def put_notify(body: dict):
        cfg = {k: (str(body.get(k) or "").strip() or None) for k in ("bark_url", "ntfy_url", "webhook_url", "public_url", "link_mode")}
        if cfg["link_mode"] not in (None, "app", "web"):
            raise HTTPException(400, "link_mode is app or web")
        if cfg["public_url"] and not cfg["public_url"].startswith(("http://", "https://")):
            raise HTTPException(400, "public_url must start with http:// or https://")
        store.kv_set("notify", cfg)
        return cfg

    @app.get("/open/{path:path}")
    async def open_link(path: str, request: Request):
        """Web fallback of a push deep link (<public url>/open/task/<id> …): the web app routes ?open=<path>."""
        from urllib.parse import quote as _q
        from fastapi.responses import RedirectResponse
        q = str(request.url.query)
        target = path.strip("/") + (("?" + q) if q else "")
        return RedirectResponse("/?open=" + _q(target, safe=""), status_code=302)

    @app.post("/api/settings/notify/test", dependencies=[Depends(auth)])
    async def test_notify():
        return await notifier.notify("test", "Aiw", "通知测试成功：点这条会打开 Aiw 的聊天")

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

    # ------------------------------------------------------------- online update (SSH to the Docker host)
    @app.get("/api/update", dependencies=[Depends(auth)])
    async def update_info():
        from . import updater
        c = updater.cfg(store)
        return {"config": c, "has_password": bool(secrets.get(updater.SECRET)), "current": os.environ.get("AIW_VERSION", "dev"),
                "github": await updater.github_latest(), "job": store.kv_get("update_job", None)}

    @app.get("/api/deploy/config", dependencies=[Depends(auth)])
    async def deploy_config():
        from . import deployer
        return {"config": deployer.cfg(store), "has_password": bool(secrets.get(deployer.SECRET))}

    @app.put("/api/deploy/config", dependencies=[Depends(auth)])
    async def deploy_save(body: dict):
        from . import deployer
        return deployer.save(store, secrets, body)

    @app.post("/api/deploy/test", dependencies=[Depends(auth)])
    async def deploy_test():
        from . import deployer
        try:
            return await deployer.test(deployer.cfg(store), secrets.get(deployer.SECRET))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"连接失败：{type(e).__name__}: {str(e)[:200]}")

    @app.get("/api/remote-hosts", dependencies=[Depends(auth)])
    async def remote_hosts_list():
        from . import remote_hosts
        return remote_hosts.public(store, secrets)

    @app.post("/api/remote-hosts", dependencies=[Depends(auth)])
    async def remote_hosts_add(body: dict):
        from . import remote_hosts
        try:
            return remote_hosts.save(store, secrets, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/remote-hosts/{hid}", dependencies=[Depends(auth)])
    async def remote_hosts_edit(hid: str, body: dict):
        from . import remote_hosts
        try:
            return remote_hosts.save(store, secrets, body, hid)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/remote-hosts/{hid}", dependencies=[Depends(auth)])
    async def remote_hosts_del(hid: str):
        from . import remote_hosts
        remote_hosts.remove(store, secrets, hid)
        return {"ok": True}

    @app.post("/api/remote-hosts/{hid}/test", dependencies=[Depends(auth)])
    async def remote_hosts_test(hid: str):
        from . import remote_hosts
        h = remote_hosts.find(store, hid)
        if not h:
            raise HTTPException(404)
        try:
            return await remote_hosts.test(h, secrets.get(f"remote_host_{h['id']}"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"连接失败：{type(e).__name__}: {str(e)[:200]}")

    @app.put("/api/update/config", dependencies=[Depends(auth)])
    async def update_config(body: dict):
        from . import updater
        c = updater.cfg(store)
        for k in updater.DEFAULT:
            if k in body:
                c[k] = int(body[k] or 22) if k == "port" else bool(body[k]) if k == "sudo" else str(body[k]).strip()
        store.kv_set("update_cfg", c)
        if body.get("password"):
            secrets.put(updater.SECRET, str(body["password"]))
        return {"config": c, "has_password": bool(secrets.get(updater.SECRET))}

    @app.post("/api/update/test", dependencies=[Depends(auth)])
    async def update_test():
        from . import updater
        try:
            return await updater.test(updater.cfg(store), secrets.get(updater.SECRET))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"连接失败：{type(e).__name__}: {str(e)[:200]}")

    @app.post("/api/update/start", dependencies=[Depends(auth)])
    async def update_start():
        from . import updater
        gh = await updater.github_latest()
        try:
            r = await updater.start(updater.cfg(store), secrets.get(updater.SECRET))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"启动更新失败：{type(e).__name__}: {str(e)[:200]}")
        job = {"started_at": r["started_at"], "from": os.environ.get("AIW_VERSION", "dev"),
               "target": (gh.get("image") or {}).get("sha", "")}
        store.kv_set("update_job", job)
        return job

    @app.get("/api/update/status", dependencies=[Depends(auth)])
    async def update_status():
        from . import updater
        try:
            st = await updater.status(updater.cfg(store), secrets.get(updater.SECRET))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"读取进度失败：{type(e).__name__}: {str(e)[:200]}")
        st["job"] = store.kv_get("update_job", None)
        st["current"] = os.environ.get("AIW_VERSION", "dev")
        return st

    # ------------------------------------------------------------- skills (composer "+" menu)
    @app.get("/api/skills", dependencies=[Depends(auth)])
    async def get_skills():
        from agent_runtime import skills as _skills
        return _skills.listing(store.kv_get("skills_custom", []) or [])

    @app.put("/api/skills/custom", dependencies=[Depends(auth)])
    async def put_custom_skills(body: list[dict]):
        out = []
        for s_ in body:
            sid = re.sub(r"[^A-Za-z0-9_.-]", "-", str(s_.get("id") or "").strip())[:60]
            if not sid:
                raise HTTPException(400, "each skill needs an id")
            out.append({"id": sid, "title": str(s_.get("title") or sid)[:60], "desc": str(s_.get("desc") or "")[:200],
                        "path": str(s_.get("path") or f"~/.claude/skills/{sid}")[:300], "setup": str(s_.get("setup") or "")[:300],
                        "source": str(s_.get("source") or "")[:200], "machines": list(s_.get("machines") or [])[:20]})
        store.kv_set("skills_custom", out)
        return await get_skills()

    async def _skill_shell(mid: str, cmd: str, timeout: float) -> dict:
        import shlex
        r = await sessions.computer(mid).shell("bash -c " + shlex.quote(cmd), timeout=timeout)
        if not r.success and not r.result:
            raise RuntimeError(r.error or "shell failed")
        return r.result or {}

    async def _skill_wake(mid: str):
        if cloud.is_cloud(mid):
            await cloud.wake(mid)

    @app.post("/api/skills/install", dependencies=[Depends(auth)])
    async def install_skill(body: dict):
        """Install a skill package on every computer (npx skills add … into ~/.claude/skills)."""
        from . import skillsync
        try:
            pkg = skillsync.parse_source(body.get("source", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        job = store.kv_get("skills_install_job", None) or {}
        if job and not job.get("done") and time.time() - job.get("started", 0) < 900:
            raise HTTPException(409, "上一个技能还在安装")
        ms = [m for m in store.machines() if not body.get("machines") or m["id"] in body["machines"]]
        if not ms:
            raise HTTPException(400, "还没有电脑")
        asyncio.create_task(skillsync.install_all(pkg, ms, _skill_shell, _skill_wake, store.kv_get, store.kv_set))
        await asyncio.sleep(0.05)
        return store.kv_get("skills_install_job", None)

    @app.get("/api/skills/install", dependencies=[Depends(auth)])
    async def install_skill_status():
        return store.kv_get("skills_install_job", None)

    @app.get("/api/skills/check", dependencies=[Depends(auth)])
    async def check_skills():
        """Which custom skills each running computer has (paused cloud computers are not woken)."""
        from . import skillsync
        ids = [s["id"] for s in store.kv_get("skills_custom", []) or []]
        out = {}

        async def one(m):
            mid = m["id"]
            if cloud.is_cloud(mid):
                try:
                    if (await cloud.state(mid)).get("state") != "running":
                        out[mid] = {"name": m.get("name") or mid, "offline": True}; return
                except Exception:  # noqa: BLE001
                    out[mid] = {"name": m.get("name") or mid, "offline": True}; return
            try:
                r = await asyncio.wait_for(_skill_shell(mid, skillsync.check_script(ids), 20), 25)
                has = [l[8:].strip() for l in (r.get("stdout") or "").splitlines() if l.startswith("AIW_HAS ")]
                out[mid] = {"name": m.get("name") or mid, "has": has}
            except Exception as e:  # noqa: BLE001
                out[mid] = {"name": m.get("name") or mid, "offline": True, "error": str(e)[:120]}
        await asyncio.gather(*(one(m) for m in store.machines()))
        return out

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

    @app.post("/api/integrations/mcp/test", dependencies=[Depends(auth)])
    async def test_mcp(body: dict):
        """Connect to an MCP server (saved or not) and list its tools, so a preset or a typo shows up at once."""
        from .integrations import MCPClient
        url = str(body.get("url") or "")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "url must be http(s)")
        hdr = {str(k): secrets.expand(str(v)) for k, v in (body.get("headers") or {}).items()}
        try:
            tools = await asyncio.wait_for(MCPClient(secrets.expand(url), hdr).list_tools(), 25)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"MCP server did not answer: {type(e).__name__}: {str(e)[:200]}")
        return {"tools": [t.get("name") for t in tools]}

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
        name = _uploads.clean_name(body.name)
        try:
            raw = base64.b64decode(body.content_b64)
        except ValueError:
            raise HTTPException(400, "bad base64")
        if len(raw) > _uploads.JSON_MAX_MB * _uploads.MB:
            raise HTTPException(413, f"max {_uploads.JSON_MAX_MB} MB here; send big files (videos) to /api/uploads/raw")
        local = _uploads.local_path(settings.artifacts_dir, name)
        with open(local, "wb") as f:
            f.write(raw)
        return await _upload_done(name, local, len(raw), body.machine_id, body.local_only, body.thread_id, body.task_id)

    # the + sheet's photos / videos / files: the body IS the file (no base64), streamed to disk
    @app.post("/api/uploads/raw", dependencies=[Depends(auth)])
    async def upload_raw(request: Request, name: str, machine_id: str = "ai-linux-001", local_only: bool = False,
                         thread_id: Optional[str] = None, task_id: Optional[str] = None):
        name = _uploads.clean_name(name)
        cap = _uploads.RAW_MAX_MB * _uploads.MB
        local = _uploads.local_path(settings.artifacts_dir, name)
        size = 0
        try:
            with open(local, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap:
                        raise HTTPException(413, f"max {_uploads.RAW_MAX_MB} MB")
                    f.write(chunk)
        except BaseException:
            try:
                os.remove(local)
            except OSError:
                pass
            raise
        if not size:
            os.remove(local)
            raise HTTPException(400, "empty file")
        return await _upload_done(name, local, size, machine_id, local_only, thread_id, task_id)

    async def _upload_done(name: str, local: str, size: int, machine_id: str, local_only: bool,
                           thread_id: Optional[str] = None, task_id: Optional[str] = None) -> dict:
        kind = "video" if name.lower().endswith(_uploads.VIDEO_EXT) else ""
        # 3.8.5: every upload stays on the API host and is recorded, so any later run on any computer gets it
        rec = _uploads.record(store, name, local, size, kind, thread_id or None, task_id or None,
                              machine_id=None if local_only else machine_id)
        if local_only:
            return {"name": name, "size": size, "vm_path": None, "artifact": local, "kind": kind, "upload_id": rec["id"]}
        if cloud.is_cloud(machine_id):
            try:
                await cloud.wake(machine_id)
            except CloudError as e:
                raise HTTPException(503, str(e))
        try:
            await _uploads.push_to_vm(sessions.computer(machine_id), local, f"uploads/{name}")
        except _uploads.PushError as e:
            raise HTTPException(502, f"VM upload failed: {e}")
        return {"name": name, "size": size, "vm_path": f"~/workspace/uploads/{name}", "artifact": local, "kind": kind,
                "upload_id": rec["id"]}

    # ------------------------------------------------------------- machines
    @app.get("/api/machines", dependencies=[Depends(auth)])
    async def machines():
        return store.machines()

    @app.put("/api/machines/{mid}/name", dependencies=[Depends(auth)])
    async def rename_machine(mid: str, body: dict):
        """Rename a computer for web and iOS alike. An empty name goes back to the default (云电脑 N / configured name)."""
        raw = (body or {}).get("name")
        if raw is not None and not isinstance(raw, str):
            raise HTTPException(400, "name must be a string")
        name = " ".join((raw or "").split())
        if len(name) > 32:
            raise HTTPException(400, "名称最多 32 个字")
        if any(ord(ch) < 32 for ch in (raw or "") if ch not in " \t\n"):
            raise HTTPException(400, "名称里有不能用的字符")
        m = store.rename_machine(mid, name)
        if not m:
            raise HTTPException(404, "no such computer")
        return m

    @app.get("/api/machines/{mid}", dependencies=[Depends(auth)])
    async def machine(mid: str):
        m = store.machine(mid)
        if not m:
            raise HTTPException(404)
        out = {**m}
        if cloud.is_cloud(mid):
            try:
                st = await cloud.state(mid)
            except Exception as e:  # noqa: BLE001
                st = {"state": "unknown", "error": str(e)[:200]}
            e = cloud.entry(mid) or {}
            out["vm"], out["cloud"] = st, {k: e.get(k) for k in ("provider", "status", "error", "remote_id")}
            if e.get("status") != "ready":
                out["agent"] = {"ok": False, "error": f"云电脑{ {'adding': '添加中', 'creating': '创建中', 'installing': '安装中', 'error': '安装失败'}.get(e.get('status'), e.get('status')) }"}
                return out
            if st.get("state") != "running":
                # paused: don't poke it (a probe would only fail); the next task or 开机 wakes it
                out["agent"] = {"ok": False, "paused": True, "error": "已自动暂停，任务开始时自动唤醒"}
                return out
        if vm_runtime and not cloud.is_cloud(mid):
            try:
                st = await vm_runtime.status(mid)
                store.set_machine_status(mid, st.state.value, st.ip)
                await _follow_pve_ip(mid, st.ip)
                out["vm"] = st.model_dump()
            except Exception as e:  # noqa: BLE001
                out["vm_error"] = str(e)
        out["agent"] = {"ok": False, "error": "no response"}
        for attempt in range(2):  # one retry: a single slow/reconnecting probe is not "offline"
            try:
                h = await asyncio.wait_for(sessions.computer(mid).health(), 8)
                out["agent"] = h.result if h.success else {"ok": False, "error": h.error}
                if h.success and isinstance(h.result, dict):
                    browseros.note_health(store, mid, h.result)
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
            if cloud.is_cloud(mid):
                if sessions.busy_session(mid) and _op != "start":
                    raise HTTPException(409, "stop the running agent session first")
                try:
                    await cloud.power(mid, _op)
                except CloudError as e:
                    raise HTTPException(502, str(e))
                return {"ok": True}
            _need_vm()
            if sessions.busy_session(mid) and _op != "start":
                raise HTTPException(409, "stop the running agent session first")
            return await getattr(vm_runtime, _op)(mid)
        app.post(f"/api/machines/{{mid}}/{op}", dependencies=[Depends(auth)])(_vm)


    @app.post("/api/machines/{mid}/snapshot", dependencies=[Depends(auth)])
    async def snapshot(mid: str, body: Snap):
        if cloud.is_cloud(mid):
            try:
                return await cloud.client(mid).snapshot(cloud.entry(mid)["remote_id"], body.name)
            except CloudError as e:
                raise HTTPException(502, str(e))
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
        if cloud.is_cloud(mid):
            # InstaBox's own viewer: a short-lived link, view-only enforced by its server
            try:
                st = await cloud.state(mid)
                if st.get("state") != "running":
                    return {"novnc_url": None, "paused": True, "external": True}
                return {"novnc_url": await cloud.desktop_url(mid, view_only), "external": True}
            except CloudError as e:
                raise HTTPException(502, str(e))
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
                    sessions.answer_confirm(sid, bool(msg.get("approve")), *((msg["scope"],) if msg.get("scope") else ()))
                elif op == "answer":
                    sessions.answer_question(sid, str(msg.get("text") or ""))
        except WebSocketDisconnect:
            pass
        finally:
            live.subscribers.discard(ws)

    # ------------------------------------------------------------- artifacts + web console
    @app.get("/api/artifacts", dependencies=[Depends(auth)])
    async def artifact(path: str, download: bool = False, member: str = "", images: bool = False):
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(settings.artifacts_dir) + os.sep) or not os.path.exists(real):
            raise HTTPException(404)
        if (images or member) and real.lower().endswith(".zip"):
            # a zip of screenshots is unreadable on a phone: list its pictures, and serve one so the chat can show it
            import mimetypes
            import zipfile
            from fastapi.responses import Response
            try:
                with zipfile.ZipFile(real) as z:
                    pics = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename
                            and re.search(r"\.(png|jpe?g|webp|gif)$", i.filename, re.I) and i.file_size <= 20 * 1024 * 1024][:24]
                    if images:
                        return [{"name": i.filename, "size": i.file_size} for i in pics]
                    hit = next((i for i in pics if i.filename == member), None)
                    if not hit:
                        raise HTTPException(404)
                    disp = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(os.path.basename(member))}"} if download else {}
                    return Response(z.read(hit), media_type=mimetypes.guess_type(member)[0] or "image/png",
                                    headers={"Cache-Control": "private, max-age=86400", **disp})
            except zipfile.BadZipFile:
                return [] if images else Response(status_code=404)
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
        @app.middleware("http")
        async def no_stale_web(request: Request, call_next):
            resp = await call_next(request)
            p = request.url.path
            if not p.startswith("/api") and (p == "/" or p.endswith((".js", ".css", ".html"))):
                resp.headers["Cache-Control"] = "no-cache"  # always revalidate so updates show up at once
            return resp

        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


app = build_app() if os.environ.get("AIW_NO_AUTOAPP") != "1" else None
