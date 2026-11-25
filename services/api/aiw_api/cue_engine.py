"""Cue by Manus cloud engine (official OpenAPI v2 default + opt-in experimental ConnectRPC/Socket.IO v4).

Security & stability design:
  - Browser cookies are NEVER read automatically; users must explicitly trigger「从浏览器读取」.
  - Defaults strictly to official Manus OpenAPI v2 (`https://api.manus.ai/v2/` + `x-manus-api-key`).
  - Non-public ConnectRPC/Socket.IO (`api.manus.im`) and web-token fallback require `experimental_native=True`.
  - Reuses a single `httpx.AsyncClient` per turn with progressive polling backoff (1.5s -> 10s, 429 retry-after).
  - Long-running tasks never fail on a 10-minute timeout; `task_id` / `after_id` are persisted so polling resumes
    even across API restarts.
  - Completed jobs are evicted from `_jobs` on terminal `wait()` and by TTL pruning to prevent memory leaks.
  - `agent_status == "waiting"` surfaces as `status="blocked"` with confirmation metadata for AIW chat cards.
  - `error_message` / `status_update(error)` surface the exact error reason returned by Cue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlparse

import httpx

from .cloud_auth import CloudAuthStore

log = logging.getLogger("aiw.cue")

CUE_ORIGIN = "https://cue.im"
CUE_CONNECT_BASE = "https://api.manus.im"
CUE_MANUS_API_BASE = "https://api.manus.ai/v2/"
IM_AGENT = "agent-default"
IM_TASK = "agent-default-main_task"
_EVENT_ALPHABET = string.ascii_letters + string.digits
_POLL_BACKOFF = (1.5, 2.5, 4.0, 6.0, 8.0, 10.0)
_JOB_TTL_S = 3600.0


def create_cue_event_id() -> str:
    return "".join(secrets.choice(_EVENT_ALPHABET) for _ in range(22))


def build_cue_socket_url(base_url: str, device_id: str, locale: str = "zh-CN", tz: str = "Asia/Shanghai") -> str:
    raw = (base_url or CUE_CONNECT_BASE).rstrip("/")
    parsed = urlparse(raw)
    ws_proto = "ws" if parsed.scheme == "http" else "wss"
    qs = urlencode({
        "EIO": "4",
        "transport": "websocket",
        "locale": locale,
        "tz": tz,
        "clientType": "desktop",
        "clientVersion": "1.0.6",
        "x-product-name": "manus-agents",
        "deviceId": device_id,
    })
    return f"{ws_proto}://{parsed.netloc}/socket.io/?{qs}"


def parse_socketio_packet(raw: str) -> dict[str, Any]:
    if not raw:
        return {"kind": "other", "raw": raw}
    if raw == "2" or raw.startswith("2"):
        return {"kind": "ping"}
    if raw == "3":
        return {"kind": "pong"}
    if raw.startswith("0"):
        try:
            return {"kind": "open", "payload": json.loads(raw[1:])}
        except ValueError:
            return {"kind": "open", "payload": None}
    if raw.startswith("40"):
        body = raw[2:]
        try:
            return {"kind": "connect", "payload": json.loads(body) if body else {}}
        except ValueError:
            return {"kind": "connect", "payload": None}
    if raw.startswith("41"):
        return {"kind": "disconnect"}
    if raw.startswith("44"):
        body = raw[2:]
        try:
            return {"kind": "connect_error", "payload": json.loads(body) if body else {}}
        except ValueError:
            return {"kind": "connect_error", "payload": body}
    if raw.startswith("42"):
        try:
            arr = json.loads(raw[2:])
            if isinstance(arr, list) and arr:
                return {"kind": "event", "event": str(arr[0]), "payload": arr[1] if len(arr) > 1 else None}
        except ValueError:
            pass
    return {"kind": "other", "raw": raw}


def encode_socketio_event(event_name: str, payload: Any) -> str:
    return "42" + json.dumps([event_name, payload], ensure_ascii=False)


def _read_assistant_text(ev: dict[str, Any]) -> str:
    direct = ev.get("content")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    contents = ev.get("contents")
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                val = item.get("value") or item.get("text") or item.get("content")
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
        if parts:
            return "\n".join(parts)
    return ""


def _collect_media(am: dict[str, Any], sink: list) -> None:
    """v3.9.85: assistant_message.attachments (type image|video|file|voice|slides, filename, url, path, content_type).
    The url is a temporary signed link, so AIW copies the file right after the turn."""
    raw_list = am.get("attachments") or am.get("files") or []
    if not isinstance(raw_list, list):
        return
    for a in raw_list:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url") or a.get("file_url") or a.get("download_url") or a.get("presigned_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        key = a.get("version_uid") or a.get("file_uid") or url
        if any((x.get("version_uid") or x.get("file_uid") or x.get("url")) == key for x in sink):
            continue
        row = {k: str(a.get(k) or "") for k in ("type", "filename", "path", "content_type", "file_uid", "version_uid")}
        row["url"] = url
        if not row["filename"]:
            row["filename"] = str(a.get("name") or "")
        sink.append(row)


def _extract_error_reason(ev: dict[str, Any]) -> str:
    """Extract human-readable error details from Cue error_message or status_update events."""
    for container_key in ("error_message", "status_update", "error"):
        sub = ev.get(container_key)
        if isinstance(sub, dict):
            for k in ("content", "message", "error", "reason", "detail", "code"):
                v = sub.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:300]
        elif isinstance(sub, str) and sub.strip():
            return sub.strip()[:300]
    for k in ("content", "message", "reason", "detail"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:300]
    return ""


def _http_client(proxy_url: str = "", timeout: float = 20.0, transport: Any = None) -> httpx.AsyncClient:
    kw: dict[str, Any] = {"timeout": timeout, "follow_redirects": False}
    if transport is not None:
        kw["transport"] = transport
    elif proxy_url:
        kw["proxy"] = proxy_url
    return httpx.AsyncClient(**kw)


def _openapi_headers(api_key: str, as_web_token: bool = False) -> dict[str, str]:
    if as_web_token:
        return {
            "Authorization": f"Bearer {api_key}",
            "x-product-name": "manus-agents",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    return {
        "x-manus-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _connect_headers(auth: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
        "x-client-type": "desktop",
        "x-client-version": "1.0.6",
        "x-client-id": str(auth.get("device_id") or uuid.uuid4()),
        "x-product-name": "manus-agents",
        "Origin": CUE_ORIGIN,
    }
    token = str(auth.get("access_token") or "").strip()
    api_key = str(auth.get("api_key") or "").strip()
    cookie = str(auth.get("cookie_header") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        headers["x-manus-api-key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    if cookie:
        headers["Cookie"] = cookie
    elif token:
        headers["Cookie"] = f"cue_session_id={token}; session_id={token}"
    return headers


@dataclass
class _CueJob:
    job_id: str
    conv_id: str
    agent_id: str
    session_id: str
    after_id: str = ""
    last_event_id: str = ""
    expected_user_text: str = ""
    status: str = "running"          # running | turn_done | turn_failed | blocked
    response: str = ""
    error: str = ""
    auth_error: bool = False
    waiting_confirm: bool = False
    denied: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    media: list[dict[str, Any]] = field(default_factory=list)   # v3.9.85: images / videos / files Cue made
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class CueEngine:
    """Cloud engine for Cue by Manus (official OpenAPI v2 default; experimental ConnectRPC/Socket.IO opt-in)."""
    id = "cue"
    cloud = True

    def __init__(
        self,
        auth_store: CloudAuthStore,
        shell: Optional[Callable[[str, str, int], Any]] = None,
        default_machine: Optional[Callable[[], str]] = None,
        transport: Any = None,
        poll_backoff: tuple[float, ...] = _POLL_BACKOFF,
    ):
        self.auth_store = auth_store
        self.shell = shell
        self.default_machine = default_machine
        self.transport = transport        # optional httpx.MockTransport for unit tests
        self.poll_backoff = poll_backoff  # customizable in unit tests
        self._jobs: dict[str, _CueJob] = {}
        self._last_sync: dict[str, float] = {}
        self._sync_locks: dict[str, asyncio.Lock] = {}

    def _sync_key(self, conv_id: str) -> str:
        return f"cue_sync:{conv_id}"

    def _load_sync(self, conv_id: str) -> dict[str, Any]:
        st = getattr(self.auth_store, "store", None)
        if st is None or not conv_id:
            return {}
        d = st.kv_get(self._sync_key(conv_id), None)
        return dict(d) if isinstance(d, dict) else {}

    def _save_sync(self, conv_id: str, last_event_id: str, seen_ids: Optional[list[str]] = None) -> None:
        st = getattr(self.auth_store, "store", None)
        if st is None or not conv_id or not last_event_id:
            return
        cur = self._load_sync(conv_id)
        seen = list(cur.get("seen_ids") or [])
        for x in (seen_ids or []):
            if x and x not in seen:
                seen.append(x)
        if last_event_id not in seen:
            seen.append(last_event_id)
        try:
            st.kv_set(self._sync_key(conv_id), {
                "last_event_id": last_event_id,
                "seen_ids": seen[-120:],
                "updated_at": time.time(),
            })
        except Exception:  # noqa: BLE001
            pass

    def _kv_key(self, job_id: str) -> str:
        return f"cue_job:{job_id}"

    def _persist_job(self, job: _CueJob) -> None:
        st = getattr(self.auth_store, "store", None)
        if st is None:
            return
        try:
            st.kv_set(self._kv_key(job.job_id), {
                "job_id": job.job_id,
                "agent_id": job.agent_id,
                "session_id": job.session_id,
                "after_id": job.after_id,
                "expected_user_text": job.expected_user_text,
                "ts": time.time(),
            })
        except Exception:  # noqa: BLE001
            pass

    def _clear_persisted_job(self, job_id: str) -> None:
        st = getattr(self.auth_store, "store", None)
        if st is None:
            return
        try:
            st.kv_set(self._kv_key(job_id), None)
        except Exception:  # noqa: BLE001
            pass

    def _prune_jobs(self) -> None:
        now = time.time()
        stale = [
            jid for jid, j in self._jobs.items()
            if j.status != "running" and j.finished_at and (now - j.finished_at) > _JOB_TTL_S
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    def _effective_mode(self, auth: dict[str, Any]) -> str:
        """Default strictly to official OpenAPI v2. Native mode requires explicit experimental_native=True."""
        exp_native = bool(auth.get("experimental_native"))
        mode = str(auth.get("mode") or "openapi").strip().lower()
        has_key = bool(auth.get("api_key"))
        has_native = bool(auth.get("access_token") or auth.get("cookie_header"))
        if mode == "native" and exp_native and has_native:
            return "native"
        if has_key:
            return "openapi"
        if exp_native and has_native:
            return "native"
        return ""

    async def _ensure_auth(self, mid: str = "") -> dict[str, Any]:
        """Load saved credentials only. Never auto-read browser cookies without explicit user action."""
        return self.auth_store.load("cue")

    # ------------------------------------------------------------------ OpenAPI v2 helpers
    async def _openapi_req(
        self,
        auth: dict[str, Any],
        path: str,
        body: Any = None,
        as_web_token: bool = False,
        client: Optional[httpx.AsyncClient] = None,
    ) -> tuple[bool, int, dict[str, Any], Optional[float]]:
        """-> (ok, status_code, json_dict, retry_after_s)."""
        key = str(auth.get("access_token") if as_web_token else auth.get("api_key") or "").strip()
        if not key:
            return False, 401, {"error": "未配置 Manus API Key"}, None
        base = (str(auth.get("openapi_base") or "") or CUE_MANUS_API_BASE).rstrip("/") + "/"
        url = base + path.lstrip("/")
        headers = _openapi_headers(key, as_web_token=as_web_token)
        own = client is None
        cli = client or _http_client(str(auth.get("proxy_url") or ""), 20.0, self.transport)
        try:
            if body is None:
                r = await cli.get(url, headers=headers)
            else:
                r = await cli.post(url, headers=headers, json=body)
        finally:
            if own:
                await cli.aclose()
        retry_after: Optional[float] = None
        ra = r.headers.get("retry-after") or r.headers.get("Retry-After")
        if ra:
            try:
                retry_after = max(1.0, min(float(ra), 120.0))
            except ValueError:
                pass
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {}
        if not isinstance(data, dict):
            data = {}
        ok = r.is_success and data.get("ok") is True
        return ok, r.status_code, data, retry_after

    async def _connect_req(
        self,
        auth: dict[str, Any],
        path: str,
        body: Any = None,
        method: Optional[str] = None,
    ) -> tuple[bool, int, Any]:
        base = (str(auth.get("base_url") or "") or CUE_CONNECT_BASE).rstrip("/")
        url = f"{base}/{path.lstrip('/')}"
        headers = _connect_headers(auth)
        meth = method or ("POST" if body is not None else "GET")
        async with _http_client(str(auth.get("proxy_url") or ""), 20.0, self.transport) as client:
            if meth == "GET":
                r = await client.get(url, headers=headers)
            else:
                r = await client.post(url, headers=headers, json=body if body is not None else {})
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = r.text
        return r.is_success, r.status_code, data

    # ------------------------------------------------------------------ verify & agents
    async def list_agents(self, auth: dict[str, Any]) -> list[dict[str, str]]:
        mode = self._effective_mode(auth)
        out: list[dict[str, str]] = []
        if mode == "openapi":
            ok, _, data, _ = await self._openapi_req(auth, "agent.list")
            if ok:
                out.append({"id": IM_AGENT, "name": "Cue（默认）", "session_id": IM_TASK})
                for item in data.get("data") or []:
                    if not isinstance(item, dict):
                        continue
                    aid = str(item.get("id") or "").strip()
                    tid = str(item.get("task_id") or "").strip()
                    if not aid or aid == IM_AGENT:
                        continue
                    name = str(item.get("nickname") or item.get("name") or aid).strip()
                    out.append({"id": aid, "name": name, "session_id": tid})
                return out
        if mode == "native":
            ok, _, data = await self._connect_req(auth, "/agents.v1.AgentsService/ListAgents", {})
            if ok and isinstance(data, dict):
                raw_list = data.get("agents") or data.get("items") or data.get("data") or []
                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    aid = str(item.get("id") or item.get("agentId") or item.get("agent_id") or "").strip()
                    if not aid:
                        continue
                    name = str(item.get("name") or item.get("nickname") or item.get("title") or "Cue").strip()
                    sid = str(item.get("sessionUid") or item.get("session_uid") or item.get("sessionId") or item.get("taskId") or "").strip()
                    out.append({"id": aid, "name": name, "session_id": sid})
                if out:
                    return out
        return [{"id": IM_AGENT, "name": "Cue（默认）", "session_id": IM_TASK}]

    async def verify(self, auth_override: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        auth = auth_override if auth_override is not None else self.auth_store.load("cue")
        mode = self._effective_mode(auth)
        if not mode:
            return {"ok": False, "error": "尚未配置 Manus API Key（或在实验设置中开启浏览器登录态模式）"}
        if mode == "openapi":
            ok, status, data, _ = await self._openapi_req(auth, "user.me")
            if not ok:
                detail = _extract_error_reason(data)
                return {"ok": False, "status": status, "auth_error": status in (401, 403),
                        "error": f"Manus OpenAPI 验证失败（HTTP {status}）" + (f"：{detail}" if detail else "")}
            agents = await self.list_agents(auth)
            handle = str(data.get("email") or "").strip()
            display = str(data.get("name") or "").strip()
            if auth_override is None and (handle or display):
                self.auth_store.save("cue", {"handle": handle, "display_name": display})
            return {"ok": True, "mode": "openapi", "handle": handle, "display_name": display, "agents": agents}

        ok, status, data = await self._connect_req(auth, "/agents.v1.AgentsService/ListAgents", {})
        if not ok:
            return {"ok": False, "status": status, "auth_error": status in (401, 403),
                    "error": f"Cue 实验实时通道验证失败（HTTP {status}）"}
        agents = await self.list_agents(auth)
        return {"ok": True, "mode": "native", "handle": auth.get("handle") or "",
                "display_name": auth.get("display_name") or "", "agents": agents}

    # ------------------------------------------------------------------ Engine interface
    async def models(self, mid: str = "") -> dict[str, Any]:
        auth = self.auth_store.load("cue")
        agents = await self.list_agents(auth) if self._effective_mode(auth) else [{"id": IM_AGENT, "name": "Cue（默认）"}]
        groups = [{"id": a["id"], "label": a["name"], "efforts": [], "plain": True} for a in agents]
        return {"ok": True, "groups": groups, "efforts": []}

    async def start(self, mid: str, conv: dict[str, Any], prompt: str, skip: bool = False) -> dict[str, Any]:
        self._prune_jobs()
        auth = await self._ensure_auth(mid)
        mode = self._effective_mode(auth)
        if not mode:
            return {"ok": False, "error": "Cue 尚未配置 Manus API Key：请点右上角「凭据」填写 API Key（默认只走官方 API v2）。"}
        agent_id = str(conv.get("model") or auth.get("agent_id") or IM_AGENT).strip() or IM_AGENT
        session_id = str(conv.get("session") or "").strip()
        conv_id = str(conv.get("id") or "").strip()
        job_id = f"cue-{uuid.uuid4().hex[:10]}"
        job = _CueJob(job_id=job_id, conv_id=conv_id, agent_id=agent_id, session_id=session_id, expected_user_text=prompt)
        self._jobs[job_id] = job
        job.task = asyncio.get_running_loop().create_task(self._run_turn(job, auth, mode, prompt))
        return {"ok": True, "job": job_id}

    async def send(self, mid: str, job_id: str, text: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if not job or (job.task and not job.task.done()):
            return {"ok": False, "error": "Cue job not ready"}
        auth = await self._ensure_auth(mid)
        mode = self._effective_mode(auth)
        if not mode:
            return {"ok": False, "error": "Cue 尚未配置 Manus API Key"}
        job.status = "running"
        job.response = ""
        job.error = ""
        job.auth_error = False
        job.waiting_confirm = False
        job.denied = []
        job.tools = []
        job.finished_at = 0.0
        job.expected_user_text = text
        job.event.clear()
        job.task = asyncio.get_running_loop().create_task(self._run_turn(job, auth, mode, text))
        return {"ok": True, "job": job_id}

    async def wait(self, mid: str, job_id: str, cursor: int, seconds: int = 20) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if not job:
            # Restart recovery: resume polling persisted task_id via OpenAPI v2
            st = getattr(self.auth_store, "store", None)
            saved = st.kv_get(self._kv_key(job_id), None) if st is not None else None
            if isinstance(saved, dict) and saved.get("session_id"):
                auth = await self._ensure_auth(mid)
                if self._effective_mode(auth) == "openapi":
                    job = _CueJob(
                        job_id=job_id,
                        conv_id=str(saved.get("conv_id") or ""),
                        agent_id=str(saved.get("agent_id") or IM_AGENT),
                        session_id=str(saved["session_id"]),
                        after_id=str(saved.get("after_id") or ""),
                        expected_user_text=str(saved.get("expected_user_text") or ""),
                    )
                    self._jobs[job_id] = job
                    job.task = asyncio.get_running_loop().create_task(self._resume_openapi_poll(job, auth))
        if not job:
            return {"status": "turn_failed", "cursor": cursor, "error": "Cue 会话已结束"}
        if job.status == "running":
            try:
                await asyncio.wait_for(job.event.wait(), timeout=max(1, min(int(seconds or 20), 60)))
            except asyncio.TimeoutError:
                pass
            if job.status == "running":
                job.event.clear()
        new_tools = job.tools[cursor:]
        new_cursor = len(job.tools)
        out: dict[str, Any] = {
            "status": job.status,
            "cursor": new_cursor,
            "session": job.session_id,
            "tools": new_tools,
        }
        if job.status != "running":
            out["response"] = job.response
            if job.error:
                out["error"] = job.error
            if job.auth_error:
                out["auth_error"] = True
            if job.waiting_confirm:
                out["waiting_confirm"] = True
            if job.denied:
                out["denied"] = list(job.denied)
            if job.media:
                out["media"] = list(job.media)
            # Evict completed job from memory immediately to prevent leaks
            self._jobs.pop(job_id, None)
            self._clear_persisted_job(job_id)
        return out

    async def stop(self, mid: str, job_id: str) -> dict[str, Any]:
        job = self._jobs.pop(job_id, None)
        self._clear_persisted_job(job_id)
        if job and job.task and not job.task.done():
            job.task.cancel()
        return {"ok": True}

    # ------------------------------------------------------------------ Turn execution
    def _finish(self, job: _CueJob, status: str, response: str = "", error: str = "",
                auth_error: bool = False, waiting_confirm: bool = False) -> None:
        job.status = status
        job.response = response
        job.error = error
        job.auth_error = auth_error
        job.waiting_confirm = waiting_confirm
        job.finished_at = time.time()
        job.event.set()

    async def _run_turn(self, job: _CueJob, auth: dict[str, Any], mode: str, prompt: str) -> None:
        try:
            if mode == "openapi":
                await self._run_openapi_turn(job, auth, prompt)
            else:
                await self._run_native_turn(job, auth, prompt)
        except asyncio.CancelledError:
            self._finish(job, "turn_failed", error="已停止")
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cue turn error: %r", e)
            self._finish(job, "turn_failed", error=str(e))

    async def _run_openapi_turn(self, job: _CueJob, auth: dict[str, Any], prompt: str) -> None:
        client = _http_client(str(auth.get("proxy_url") or ""), 25.0, self.transport)
        try:
            task_id = job.session_id or IM_TASK
            if not job.session_id and job.agent_id and job.agent_id != IM_AGENT:
                ok, status, detail, _ = await self._openapi_req(
                    auth, f"agent.detail?{urlencode({'agent_id': job.agent_id})}", client=client
                )
                if ok:
                    tid = str(((detail.get("data") or {}) if isinstance(detail.get("data"), dict) else {}).get("task_id") or "")
                    if tid:
                        task_id = tid
                elif status in (401, 403):
                    reason = _extract_error_reason(detail)
                    self._finish(job, "turn_failed", auth_error=True,
                                 error=f"Manus API 认证失败（HTTP {status}）" + (f"：{reason}" if reason else ""))
                    return

            ok_b, st_b, before, _ = await self._openapi_req(
                auth, f"task.listMessages?{urlencode({'task_id': task_id, 'order': 'desc', 'limit': '1'})}",
                client=client,
            )
            if not ok_b:
                reason = _extract_error_reason(before)
                self._finish(job, "turn_failed", auth_error=st_b in (401, 403),
                             error=f"读取 Cue 会话失败（HTTP {st_b}）" + (f"：{reason}" if reason else ""))
                return
            before_msgs = before.get("messages") or []
            job.after_id = str(before_msgs[0].get("id") or "") if before_msgs and isinstance(before_msgs[0], dict) else ""

            ok_s, st_s, sent, _ = await self._openapi_req(
                auth, "task.sendMessage",
                {"task_id": task_id, "message": {"content": prompt}},
                client=client,
            )
            if not ok_s:
                reason = _extract_error_reason(sent)
                self._finish(job, "turn_failed", auth_error=st_s in (401, 403),
                             error=f"发送 Cue 消息失败（HTTP {st_s}）" + (f"：{reason}" if reason else ""))
                return
            job.session_id = str(sent.get("task_id") or task_id)
            self._persist_job(job)
            await self._poll_loop(job, auth, client)
        finally:
            await client.aclose()

    async def _resume_openapi_poll(self, job: _CueJob, auth: dict[str, Any]) -> None:
        client = _http_client(str(auth.get("proxy_url") or ""), 25.0, self.transport)
        try:
            await self._poll_loop(job, auth, client)
        except asyncio.CancelledError:
            self._finish(job, "turn_failed", error="已停止")
            raise
        except Exception as e:  # noqa: BLE001
            self._finish(job, "turn_failed", error=str(e))
        finally:
            await client.aclose()

    async def _poll_loop(self, job: _CueJob, auth: dict[str, Any], client: httpx.AsyncClient) -> None:
        """Long-running poll loop with progressive backoff (1.5s -> 10s), 429 retry-after, and no 10m failure cap."""
        step = 0
        last_reply = ""
        rate_hits = 0
        transient_hits = 0
        while True:
            delay = self.poll_backoff[min(step, len(self.poll_backoff) - 1)]
            await asyncio.sleep(delay)
            step += 1

            state = await self._poll_openapi_once(
                auth, job.session_id, job.after_id, job.expected_user_text, client, sink=job.media
            )
            if state["rate_limited"]:
                rate_hits += 1
                wait_s = state.get("retry_after") or min(15.0 * rate_hits, 60.0)
                job.tools.append({"tool": "cue", "arg": f"触发限流（HTTP 429），等待 {int(wait_s)} 秒后继续轮询"})
                job.event.set()
                await asyncio.sleep(wait_s)
                continue
            rate_hits = 0

            if state.get("transient") and transient_hits < 4:
                transient_hits += 1
                wait_s = min(3.0 * transient_hits, 15.0)
                job.tools.append({"tool": "cue", "arg": f"网络波动（重试 {transient_hits}/4），{int(wait_s)} 秒后重连"})
                job.event.set()
                await asyncio.sleep(wait_s)
                continue
            if state["error"]:
                self._finish(job, "turn_failed", error=state["error"], auth_error=state["auth_error"])
                return
            transient_hits = 0
            if state.get("last_event_id"):
                job.last_event_id = state["last_event_id"]
            if state["reply"] and state["reply"] != last_reply:
                last_reply = state["reply"]
                step = 0   # reset backoff when new output arrives
            if state["waiting"]:
                if job.conv_id and job.last_event_id:
                    self._save_sync(job.conv_id, job.last_event_id, state.get("seen_ids"))
                # When Cue already sent a reply/media and transitioned to 'waiting', it is simply waiting for
                # the user's next conversational message — NOT blocked by a permission gate.
                if last_reply or job.media:
                    self._finish(job, "turn_done", response=last_reply or "（生成好了）")
                    return
                msg = "Cue 正在等待你的输入或确认，请打开 Manus 查看或继续回复。"
                job.denied = [{
                    "action": "confirm",
                    "tool": "cue",
                    "target": f"https://manus.im/app/{job.session_id}",
                    "why": "Cue 正在等待确认或补充输入",
                }]
                self._finish(job, "blocked", response=msg, waiting_confirm=True)
                return
            if state["done"]:
                if job.conv_id and job.last_event_id:
                    self._save_sync(job.conv_id, job.last_event_id, state.get("seen_ids"))
                self._finish(job, "turn_done", response=last_reply or "（Cue 已完成，但没有返回文字）")
                return

    async def _poll_openapi_once(
        self,
        auth: dict[str, Any],
        task_id: str,
        after_id: str,
        expected_user_text: str,
        client: httpx.AsyncClient,
        sink: Optional[list] = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"task_id": task_id, "order": "asc", "limit": "100"}
        if after_id:
            params["start_event_id"] = after_id
        assistant_reply = ""
        received_user = False
        last_event_id = ""
        seen_ids: list[str] = []
        seen_cursors: set[str] = set()
        while True:
            ok, st, data, retry_after = await self._openapi_req(
                auth, f"task.listMessages?{urlencode(params)}", client=client
            )
            if st == 429:
                return {"done": False, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                        "seen_ids": seen_ids, "error": "", "auth_error": False, "rate_limited": True, "retry_after": retry_after}
            if not ok:
                reason = _extract_error_reason(data)
                transient = (st == 0 or st >= 500 or st == 408)
                return {"done": False, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                        "seen_ids": seen_ids, "error": f"轮询 Cue 回复失败（HTTP {st}）" + (f"：{reason}" if reason else ""),
                        "auth_error": st in (401, 403), "rate_limited": False, "retry_after": None, "transient": transient}
            for ev in data.get("messages") or []:
                if not isinstance(ev, dict):
                    continue
                ev_id = str(ev.get("id") or "").strip()
                if after_id and ev_id == after_id:
                    continue
                if ev_id:
                    last_event_id = ev_id
                    seen_ids.append(ev_id)
                etype = ev.get("type")
                if etype == "user_message":
                    if received_user:
                        return {"done": True, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                                "seen_ids": seen_ids, "error": "", "auth_error": False, "rate_limited": False, "retry_after": None}
                    ucontent = ((ev.get("user_message") or {}) if isinstance(ev.get("user_message"), dict) else {}).get("content")
                    received_user = (ucontent == expected_user_text) or not expected_user_text
                if not received_user:
                    continue
                if etype == "assistant_message":
                    am = ev.get("assistant_message") if isinstance(ev.get("assistant_message"), dict) else {}
                    ac = am.get("content")
                    if isinstance(ac, str) and ac.strip():
                        assistant_reply = ac.strip()
                    if sink is not None:
                        _collect_media(am, sink)
                elif etype == "error_message":
                    reason = _extract_error_reason(ev)
                    return {"done": False, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                            "seen_ids": seen_ids, "error": f"Cue 执行出错：{reason}" if reason else "Cue 执行出错，请在 Manus 中查看会话",
                            "auth_error": False, "rate_limited": False, "retry_after": None}
                status_obj = ev.get("status_update") if isinstance(ev.get("status_update"), dict) else {}
                st_val = str(status_obj.get("agent_status") or "").lower()
                if st_val == "waiting":
                    return {"done": True, "waiting": True, "reply": assistant_reply, "last_event_id": last_event_id,
                            "seen_ids": seen_ids, "error": "", "auth_error": False, "rate_limited": False, "retry_after": None}
                if st_val == "stopped":
                    return {"done": True, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                            "seen_ids": seen_ids, "error": "", "auth_error": False, "rate_limited": False, "retry_after": None}
                if st_val == "error":
                    reason = _extract_error_reason(ev)
                    return {"done": False, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                            "seen_ids": seen_ids, "error": f"Cue 执行出错：{reason}" if reason else "Cue 执行出错，请在 Manus 中查看会话",
                            "auth_error": False, "rate_limited": False, "retry_after": None}
            nxt = str(data.get("next_cursor") or "").strip()
            if not nxt or nxt in seen_cursors:
                return {"done": False, "waiting": False, "reply": assistant_reply, "last_event_id": last_event_id,
                        "seen_ids": seen_ids, "error": "", "auth_error": False, "rate_limited": False, "retry_after": None}
            seen_cursors.add(nxt)
            params.pop("start_event_id", None)
            params["cursor"] = nxt

    async def sync_messages(self, conv: dict[str, Any], min_interval_s: float = 6.0) -> list[dict[str, Any]]:
        """Pull asynchronous follow-up messages (e.g. Cue background subtask reports) after a turn has ended."""
        cid = str(conv.get("id") or "").strip()
        task_id = str(conv.get("session") or "").strip()
        if not cid or not task_id:
            return []
        now = time.monotonic()
        if min_interval_s > 0 and (now - self._last_sync.get(cid, 0.0)) < min_interval_s:
            return []
        auth = self.auth_store.load("cue")
        if self._effective_mode(auth) != "openapi":
            return []

        lock = self._sync_locks.setdefault(cid, asyncio.Lock())
        if lock.locked():
            return []
        async with lock:
            self._last_sync[cid] = time.monotonic()
            sync_state = self._load_sync(cid)
            last_id = str(sync_state.get("last_event_id") or "").strip()
            seen = set(sync_state.get("seen_ids") or [])

            client = _http_client(str(auth.get("proxy_url") or ""), 15.0, self.transport)
            try:
                if not last_id:
                    # First-time watermark bootstrap for existing conversations: grab latest event id so we don't replay old history
                    ok, _, data, _ = await self._openapi_req(
                        auth, f"task.listMessages?{urlencode({'task_id': task_id, 'order': 'desc', 'limit': '5'})}",
                        client=client,
                    )
                    if not ok:
                        return []
                    msgs = [m for m in (data.get("messages") or []) if isinstance(m, dict)]
                    if not msgs:
                        return []
                    newest_id = str(msgs[0].get("id") or "").strip()
                    if newest_id:
                        self._save_sync(cid, newest_id, [str(m.get("id") or "") for m in msgs])
                    return []

                params: dict[str, str] = {"task_id": task_id, "order": "asc", "limit": "50", "start_event_id": last_id}
                new_replies: list[dict[str, Any]] = []
                new_seen: list[str] = []
                newest_id = last_id
                seen_cursors: set[str] = set()
                while True:
                    ok, _, data, _ = await self._openapi_req(
                        auth, f"task.listMessages?{urlencode(params)}", client=client
                    )
                    if not ok:
                        break
                    for ev in data.get("messages") or []:
                        if not isinstance(ev, dict):
                            continue
                        ev_id = str(ev.get("id") or "").strip()
                        if not ev_id or ev_id == last_id or ev_id in seen:
                            continue
                        newest_id = ev_id
                        new_seen.append(ev_id)
                        seen.add(ev_id)
                        if ev.get("type") == "assistant_message":
                            am = ev.get("assistant_message") if isinstance(ev.get("assistant_message"), dict) else {}
                            ac = am.get("content")
                            txt = ac.strip() if isinstance(ac, str) and ac.strip() else ""
                            media_items: list[dict[str, Any]] = []
                            _collect_media(am, media_items)
                            if txt or media_items:
                                if media_items and not txt and new_replies and not new_replies[-1].get("error"):
                                    new_replies[-1].setdefault("media", []).extend(media_items)
                                else:
                                    entry: dict[str, Any] = {"id": ev_id, "text": txt}
                                    if media_items:
                                        entry["media"] = media_items
                                    new_replies.append(entry)
                        elif ev.get("type") == "error_message":
                            reason = _extract_error_reason(ev)
                            if reason:
                                new_replies.append({"id": ev_id, "text": f"（Cue 后台任务出错：{reason}）", "error": True})
                    nxt = str(data.get("next_cursor") or "").strip()
                    if not nxt or nxt in seen_cursors:
                        break
                    seen_cursors.add(nxt)
                    params.pop("start_event_id", None)
                    params["cursor"] = nxt
                if newest_id != last_id or new_seen:
                    self._save_sync(cid, newest_id, new_seen)
                return new_replies
            finally:
                await client.aclose()

    async def _run_native_turn(self, job: _CueJob, auth: dict[str, Any], prompt: str) -> None:
        """Experimental native Cue ConnectRPC + Socket.IO v4 turn (`wss://api.manus.im/socket.io/`)."""
        import websockets

        token = str(auth.get("access_token") or "").strip()
        if not token:
            self._finish(job, "turn_failed", auth_error=True,
                         error="Cue 浏览器登录态中未找到 session token，请重新点「从浏览器读取」或改用 Manus API Key")
            return

        if not job.session_id:
            agents = await self.list_agents(auth)
            hit = next((a for a in agents if a["id"] == job.agent_id and a.get("session_id")), None)
            if not hit and agents:
                hit = next((a for a in agents if a.get("session_id")), None)
            if hit and hit.get("session_id"):
                job.session_id = hit["session_id"]
            else:
                self._finish(job, "turn_failed", error="实验实时通道未找到可用会话，请改用官方 Manus API Key 模式")
                return

        device_id = str(auth.get("device_id") or uuid.uuid4())
        ws_url = build_cue_socket_url(str(auth.get("base_url") or CUE_CONNECT_BASE), device_id)
        cookie = str(auth.get("cookie_header") or f"cue_session_id={token}; session_id={token}")
        headers = {
            "Origin": CUE_ORIGIN,
            "Authorization": f"Bearer {token}",
            "Cookie": cookie,
            "x-product-name": "manus-agents",
            "x-client-type": "desktop",
            "x-client-version": "1.0.6",
            "x-client-id": device_id,
        }
        msg_id = create_cue_event_id()
        sent_at_ms = int(time.time() * 1000)
        delta_bufs: dict[str, str] = {}
        latest_text = ""
        turn_accepted = False
        seen_running = False
        last_update_at = 0.0

        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            open_timeout=15,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
        ) as ws:
            connected = False
            deadline = time.monotonic() + 15.0
            while not connected and time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                pkt = parse_socketio_packet(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
                if pkt["kind"] == "ping":
                    await ws.send("3")
                elif pkt["kind"] == "open":
                    await ws.send("40" + json.dumps({"token": token}))
                elif pkt["kind"] == "connect":
                    connected = True
                elif pkt["kind"] in ("connect_error", "disconnect"):
                    self._finish(job, "turn_failed", auth_error=True,
                                 error="Cue 实验实时连接鉴权失败，请重新点「从浏览器读取」或改用 Manus API Key")
                    return
            if not connected:
                raise RuntimeError("Cue 实时 WebSocket 握手超时")

            await ws.send(encode_socketio_event("message", {
                "id": create_cue_event_id(),
                "timestamp": int(time.time() * 1000),
                "type": "join_session",
                "sessionId": job.session_id,
                "lastMessageId": "new_session",
                "version": 2,
            }))
            await ws.send(encode_socketio_event("message", {
                "id": msg_id,
                "timestamp": int(time.time() * 1000),
                "type": "user_message",
                "sessionId": job.session_id,
                "messageType": "text",
                "taskMode": "standard",
                "content": prompt,
                "attachments": [],
                "deliveryMode": "interrupt",
            }))

            while True:
                wait_s = 2.0 if (latest_text and not seen_running) else 15.0
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=wait_s)
                except asyncio.TimeoutError:
                    if latest_text and not seen_running and last_update_at and (time.monotonic() - last_update_at) >= 4.0:
                        self._finish(job, "turn_done", response=latest_text)
                        return
                    continue

                pkt = parse_socketio_packet(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
                if pkt["kind"] == "ping":
                    await ws.send("3")
                    continue
                if pkt["kind"] == "disconnect":
                    break
                if pkt["kind"] != "event" or pkt.get("event") != "message":
                    continue
                payload = pkt.get("payload")
                if not isinstance(payload, dict):
                    continue
                psid = str(payload.get("sessionId") or "")
                if psid and psid != job.session_id:
                    continue
                ptype = str(payload.get("type") or "")
                if ptype in ("agent_error", "error"):
                    reason = _extract_error_reason(payload)
                    self._finish(job, "turn_failed", error=f"Cue 执行出错：{reason}" if reason else "Cue Agent 执行失败")
                    return
                if ptype != "event":
                    continue
                ev = payload.get("event")
                if not isinstance(ev, dict):
                    continue
                ev_type = str(ev.get("type") or "")
                sender = str(ev.get("sender") or "").lower()

                if ev_type == "chat" and sender == "user" and str(ev.get("id") or "") == msg_id:
                    turn_accepted = True
                elif ev_type == "chatDelta" and sender == "assistant":
                    turn_accepted = True
                    tid = str(ev.get("targetEventId") or "default")
                    d_obj = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
                    chunk = d_obj.get("content") if isinstance(d_obj.get("content"), str) else (
                        ev.get("content") if isinstance(ev.get("content"), str) else ""
                    )
                    if chunk:
                        delta_bufs[tid] = delta_bufs.get(tid, "") + chunk
                        latest_text = delta_bufs[tid]
                        last_update_at = time.monotonic()
                elif ev_type == "chat" and sender == "assistant":
                    ev_ts = ev.get("timestamp") or ev.get("createdAt") or ev.get("created_at")
                    if not turn_accepted and not seen_running and isinstance(ev_ts, (int, float)):
                        ts_ms = int(ev_ts * 1000) if ev_ts < 1e12 else int(ev_ts)
                        if ts_ms < sent_at_ms - 5000:
                            continue
                    full_txt = _read_assistant_text(ev)
                    if full_txt and len(full_txt) >= len(latest_text):
                        latest_text = full_txt
                        last_update_at = time.monotonic()
                elif ev_type in ("liveStatus", "toolUsed"):
                    turn_accepted = True
                    label = str(ev.get("title") or ev.get("toolName") or ev.get("status") or "正在执行…")
                    job.tools.append({"tool": ev_type, "arg": label[:200]})
                    job.event.set()
                elif ev_type == "statusUpdate":
                    st_val = str(ev.get("agentStatus") or ev.get("status") or "").lower()
                    if st_val == "running":
                        seen_running = True
                        turn_accepted = True
                    elif st_val == "waiting":
                        msg = latest_text or "Cue 正在等待你的输入或确认，请打开 Manus 查看或继续回复。"
                        job.denied = [{
                            "action": "confirm",
                            "tool": "cue",
                            "target": f"https://manus.im/app/{job.session_id}",
                            "why": "Cue 正在等待确认或补充输入",
                        }]
                        self._finish(job, "blocked", response=msg, waiting_confirm=True)
                        return
                    elif st_val in ("stopped", "idle", "completed"):
                        if seen_running or latest_text:
                            self._finish(job, "turn_done", response=latest_text or "（Cue 已完成）")
                            return
                    elif st_val in ("error", "failed"):
                        reason = _extract_error_reason(ev)
                        self._finish(job, "turn_failed", error=f"Cue 执行出错：{reason}" if reason else "Cue Agent 执行出错")
                        return

        if latest_text:
            self._finish(job, "turn_done", response=latest_text)
        else:
            self._finish(job, "turn_failed", error="Cue 实时连接关闭，未收到回复")
