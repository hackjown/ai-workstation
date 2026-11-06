"""v3.9.24 (4) webhook triggers: an outside system starts an agent task.

  POST /api/webhooks/{id}            (no API token: the signature is the auth)
       headers  X-AIW-Timestamp: <unix seconds>
                X-AIW-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
       body     JSON (any object; the payload is given to the prompt template)
       -> 202 {"ok": true, "task_id", "thread_id"}  | 401 bad/missing signature | 409 replay | 410 disabled
  The timestamp must be within ±5 minutes (AIW_WEBHOOK_WINDOW_S) and each signature is accepted once (replay cache).
  The prompt template may use {{payload}} (pretty JSON), {{payload.a.b}} (one field), {{name}}, {{now}}.

Management (API token):
  GET/POST /api/webhooks · PUT/DELETE /api/webhooks/{id} · POST /api/webhooks/{id}/rotate (returns the new secret
  ONCE) · GET /api/webhooks/{id}/calls
The secret lives in the encrypted vault as webhook_<id>; it is never logged or listed again.
AIW_WEBHOOKS=0 turns the public endpoint off (404).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import time
import uuid
from collections import OrderedDict
from typing import Optional

from fastapi import Request

log = logging.getLogger("aiw.webhooks")
SCHEMA = """CREATE TABLE IF NOT EXISTS webhooks (id TEXT PRIMARY KEY, name TEXT, prompt TEXT, machine_id TEXT,
  enabled INTEGER DEFAULT 1, thread_id TEXT, last_call REAL, calls INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS webhook_calls (id TEXT PRIMARY KEY, webhook_id TEXT, ts REAL, status INTEGER, ok INTEGER,
  task_id TEXT, error TEXT, preview TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS ix_whcalls ON webhook_calls(webhook_id, ts);"""
MAX_BODY = 256 * 1024


def window_s() -> int:
    return int(os.environ.get("AIW_WEBHOOK_WINDOW_S", "300"))


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def render(template: str, payload, name: str = "") -> str:
    def field(path: str):
        cur = payload
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return ""
        return cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False)

    def sub(m):
        key = m.group(1).strip()
        if key == "payload":
            return json.dumps(payload, ensure_ascii=False, indent=2)[:20000]
        if key.startswith("payload."):
            return str(field(key[8:]))[:5000]
        if key == "name":
            return name
        if key == "now":
            return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))
        return m.group(0)
    out = re.sub(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}", sub, template or "")
    if "{{" not in (template or "") and payload not in (None, {}, []):
        out += "\n\n收到的数据（JSON）：\n" + json.dumps(payload, ensure_ascii=False, indent=2)[:20000]
    return out


class Webhooks:
    def __init__(self, store, secrets, sessions):
        self.store, self.secrets, self.sessions = store, secrets, sessions
        self._seen: "OrderedDict[str, float]" = OrderedDict()   # signature -> ts (replay cache)
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    # ------------------------------------------------------------ management
    def list(self) -> list[dict]:
        rows = self.store.all("webhooks", order="created_at DESC")
        for r in rows:
            r["has_secret"] = bool(self.secrets.get(f"webhook_{r['id']}"))
            r["path"] = f"/api/webhooks/{r['id']}"
        return rows

    def create(self, name: str, prompt: str, machine_id: Optional[str] = None, enabled: bool = True) -> dict:
        wid = "wh" + uuid.uuid4().hex[:14]
        self.store.insert("webhooks", {"id": wid, "name": (name or "Webhook")[:60], "prompt": (prompt or "")[:8000],
                                       "machine_id": machine_id or None, "enabled": 1 if enabled else 0, "calls": 0})
        secret = self.rotate(wid)
        return {**self.store.get("webhooks", wid), "secret": secret, "path": f"/api/webhooks/{wid}"}

    def rotate(self, wid: str) -> str:
        secret = "whsec_" + _secrets.token_urlsafe(32)
        self.secrets.put(f"webhook_{wid}", secret)
        return secret

    def update(self, wid: str, body: dict) -> Optional[dict]:
        if not self.store.get("webhooks", wid):
            return None
        f = {}
        for k in ("name", "prompt"):
            if k in body:
                f[k] = str(body[k] or "")[:8000]
        if "machine_id" in body:
            f["machine_id"] = str(body["machine_id"] or "") or None
        if "enabled" in body:
            f["enabled"] = 1 if body["enabled"] else 0
        self.store.update("webhooks", wid, **f)
        return self.store.get("webhooks", wid)

    def delete(self, wid: str) -> None:
        self.store.delete("webhooks", wid)
        self.secrets.delete(f"webhook_{wid}")
        self.store._x("DELETE FROM webhook_calls WHERE webhook_id=?", (wid,))

    def calls(self, wid: str, limit: int = 30) -> list[dict]:
        return self.store.all("webhook_calls", "webhook_id=?", (wid,), "ts DESC", max(1, min(limit, 200)))

    def _log(self, wid: str, status: int, ok: bool, error: str = "", task_id: str = "", preview: str = "", ip: str = ""):
        self.store._x("INSERT INTO webhook_calls VALUES (?,?,?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex[:16], wid, time.time(), status, 1 if ok else 0, task_id, error[:300],
                       preview[:300], ip[:64]))
        # keep the newest 200 calls per webhook
        self.store._x("DELETE FROM webhook_calls WHERE webhook_id=? AND id NOT IN (SELECT id FROM webhook_calls "
                      "WHERE webhook_id=? ORDER BY ts DESC LIMIT 200)", (wid, wid))

    # ------------------------------------------------------------ verification
    def verify(self, wid: str, ts: str, sig: str, body: bytes, now: Optional[float] = None) -> tuple[int, str]:
        """-> (http status, error). 200 = good."""
        now = now or time.time()
        secret = self.secrets.get(f"webhook_{wid}")
        if not secret:
            return 401, "no secret"
        try:
            t = float(ts)
        except (TypeError, ValueError):
            return 401, "missing or bad X-AIW-Timestamp"
        if abs(now - t) > window_s():
            return 401, "timestamp outside the replay window"
        want = sign(secret, ts, body)
        if not sig or not hmac.compare_digest(want, sig.strip()):
            return 401, "bad signature"
        for k, v in list(self._seen.items()):   # forget signatures older than the window
            if now - v > window_s() * 2:
                self._seen.pop(k, None)
            else:
                break
        if sig in self._seen:
            return 409, "replayed request"
        self._seen[sig] = now
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return 200, ""

    async def fire(self, wh: dict, payload) -> dict:
        prompt = render(wh.get("prompt") or "处理这个 webhook：{{payload}}", payload, wh.get("name") or "")
        res = await self.sessions.submit(prompt, wh.get("machine_id") or None, thread_id=wh.get("thread_id"),
                                         source="webhook", title=f"🔗 {wh.get('name') or 'Webhook'}")
        self.store.update("webhooks", wh["id"], last_call=time.time(), calls=int(wh.get("calls") or 0) + 1,
                          thread_id=res["task"]["thread_id"])
        return res


def mount(app, hooks: Webhooks, auth) -> None:
    from fastapi import Body, Depends, HTTPException
    from fastapi.responses import JSONResponse

    @app.get("/api/webhooks", dependencies=[Depends(auth)])
    async def wh_list():
        return hooks.list()

    @app.post("/api/webhooks", dependencies=[Depends(auth)])
    async def wh_create(body: dict = Body(...)):
        b = body or {}
        if not str(b.get("prompt") or "").strip():
            raise HTTPException(400, "需要填写任务模板（prompt）")
        return hooks.create(str(b.get("name") or ""), str(b["prompt"]), b.get("machine_id"), bool(b.get("enabled", True)))

    @app.put("/api/webhooks/{wid}", dependencies=[Depends(auth)])
    async def wh_put(wid: str, body: dict = Body(...)):
        r = hooks.update(wid, body or {})
        if not r:
            raise HTTPException(404, "no such webhook")
        return r

    @app.delete("/api/webhooks/{wid}", dependencies=[Depends(auth)])
    async def wh_del(wid: str):
        hooks.delete(wid)
        return {"ok": True}

    @app.post("/api/webhooks/{wid}/rotate", dependencies=[Depends(auth)])
    async def wh_rotate(wid: str):
        if not hooks.store.get("webhooks", wid):
            raise HTTPException(404, "no such webhook")
        return {"secret": hooks.rotate(wid)}

    @app.get("/api/webhooks/{wid}/calls", dependencies=[Depends(auth)])
    async def wh_calls(wid: str, limit: int = 30):
        return hooks.calls(wid, limit)

    @app.post("/api/webhooks/{wid}", status_code=202)
    async def wh_receive(wid: str, request: Request):
        if os.environ.get("AIW_WEBHOOKS", "1") == "0":
            raise HTTPException(404, "webhooks are off")
        wh = hooks.store.get("webhooks", wid)
        ip = request.client.host if request.client else ""
        if not wh:
            raise HTTPException(404, "no such webhook")
        body = await request.body()
        if len(body) > MAX_BODY:
            hooks._log(wid, 413, False, "body too large", ip=ip)
            raise HTTPException(413, "body too large")
        status, err = hooks.verify(wid, request.headers.get("X-AIW-Timestamp", ""),
                                   request.headers.get("X-AIW-Signature", ""), body)
        if status != 200:
            hooks._log(wid, status, False, err, ip=ip)
            return JSONResponse({"ok": False, "error": err}, status_code=status)
        if not wh.get("enabled"):
            hooks._log(wid, 410, False, "disabled", ip=ip)
            return JSONResponse({"ok": False, "error": "webhook is disabled"}, status_code=410)
        try:
            payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        except (UnicodeDecodeError, ValueError):
            hooks._log(wid, 400, False, "body is not JSON", ip=ip)
            return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
        try:
            res = await hooks.fire(wh, payload)
        except Exception as e:  # noqa: BLE001
            hooks._log(wid, 500, False, f"{type(e).__name__}: {e}", ip=ip)
            return JSONResponse({"ok": False, "error": "could not start the task"}, status_code=500)
        tid = res["task"]["id"]
        try:
            from agent_runtime import redact as _rd
            preview = _rd.mask(body[:300].decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            preview = ""
        hooks._log(wid, 202, True, task_id=tid, preview=preview, ip=ip)
        return {"ok": True, "task_id": tid, "thread_id": res["task"]["thread_id"]}
