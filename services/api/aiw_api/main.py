"""AI Workstation API (spec §24).

    uvicorn aiw_api.main:app --host 0.0.0.0 --port 8000

REST
  POST /api/tasks                      {prompt, machine_id?, max_steps?}  -> task + session
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
import logging
import os
from urllib.parse import quote
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from aiw_protocols import Task
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from websockets.asyncio.client import connect as ws_connect

from .config import Settings
from .sessions import SessionManager
from .store import Store

log = logging.getLogger("aiw.api")


class TaskIn(BaseModel):
    prompt: str
    machine_id: str = "ai-linux-001"
    max_steps: int = 40
    timeout_s: float = 600

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
    sessions = SessionManager(store, settings, llm_factory, computer_factory)

    if vm_runtime is None and settings.pve_enabled:
        from vm_runtime import ProxmoxProvider, VMRuntime
        vm_runtime = VMRuntime(ProxmoxProvider(), {m.id: m.provider_vm_id for m in settings.machines})

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for s in sessions.live.values():
            s.control.stop()

    app = FastAPI(title="AI Workstation API", version="1.0.0", lifespan=lifespan)
    app.state.store, app.state.sessions, app.state.settings = store, sessions, settings

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
        if not store.machine(body.machine_id):
            raise HTTPException(404, "machine not found")
        task = Task(prompt=body.prompt, machine_id=body.machine_id, max_steps=body.max_steps, timeout_s=body.timeout_s)
        store.create_task(task.model_dump())
        try:
            live = await sessions.start(task)
        except RuntimeError as e:
            store.update_task(task.id, "failed", str(e))
            raise HTTPException(409, str(e))
        return {"task": store.task(task.id), "session_id": live.id}

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(auth)])
    async def get_task(task_id: str):
        t = store.task(task_id)
        if not t:
            raise HTTPException(404)
        return t


    @app.post("/api/sessions", dependencies=[Depends(auth)])
    async def create_session(body: SessionIn):
        """Re-run an existing task in a new session."""
        t = store.task(body.task_id)
        if not t:
            raise HTTPException(404)
        task = Task(id=t["id"], prompt=t["prompt"], machine_id=t["machine_id"])
        try:
            live = await sessions.start(task)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"session_id": live.id}

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    async def get_session(sid: str):
        s = store.session(sid)
        if not s:
            raise HTTPException(404)
        live = sessions.live.get(sid)
        return {**s, "control": live.control.state.value if live else None,
                "pending_confirm": live.confirm_info if live else None,
                "events": store.events(sid), "artifacts": store.artifacts(sid)}

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
        for ev in store.events(sid):  # replay history first
            await ws.send_json({"kind": "event", **ev})
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

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    if os.path.isdir(settings.web_dir):
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


app = build_app() if os.environ.get("AIW_NO_AUTOAPP") != "1" else None
