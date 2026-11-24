"""Cue by Manus cloud engine (OpenAPI v2 + ConnectRPC / Socket.IO v4).

Reference protocol: qybaihe/botark (`src/main/cue/cueOpenApi.ts`, `cueClient.ts`, `cueChatSocket.ts`).
Implements the same Engine interface as `AgyEngine` / `CliEngine` (`models`, `start`, `send`, `wait`, `stop`)
so `Bots` and `Groups` can drive Cue directly from `aiw-api` without a local VM CLI.
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

from .browser_cookies import read_browser_cookies
from .cloud_auth import CloudAuthStore

log = logging.getLogger("aiw.cue")

CUE_ORIGIN = "https://cue.im"
CUE_CONNECT_BASE = "https://api.manus.im"
CUE_MANUS_API_BASE = "https://api.manus.ai/v2/"
IM_AGENT = "agent-default"
IM_TASK = "agent-default-main_task"
_EVENT_ALPHABET = string.ascii_letters + string.digits


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
    agent_id: str
    session_id: str
    status: str = "running"          # running | turn_done | turn_failed
    response: str = ""
    error: str = ""
    auth_error: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 0
    event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class CueEngine:
    """Cloud engine for Cue by Manus (`api.manus.ai/v2` OpenAPI + `api.manus.im` ConnectRPC/Socket.IO)."""
    id = "cue"
    cloud = True

    def __init__(
        self,
        auth_store: CloudAuthStore,
        shell: Optional[Callable[[str, str, int], Any]] = None,
        default_machine: Optional[Callable[[], str]] = None,
        transport: Any = None,
    ):
        self.auth_store = auth_store
        self.shell = shell
        self.default_machine = default_machine
        self.transport = transport   # optional httpx.MockTransport for unit tests
        self._jobs: dict[str, _CueJob] = {}

    def _effective_mode(self, auth: dict[str, Any]) -> str:
        mode = str(auth.get("mode") or "auto").strip().lower()
        has_key = bool(auth.get("api_key"))
        has_native = bool(auth.get("access_token") or auth.get("cookie_header"))
        if mode == "openapi" and has_key:
            return "openapi"
        if mode == "native" and has_native:
            return "native"
        if has_key:
            return "openapi"
        if has_native:
            return "native"
        return ""

    async def _ensure_auth(self, mid: str = "") -> dict[str, Any]:
        auth = self.auth_store.load("cue")
        if self._effective_mode(auth):
            return auth
        # Best-effort auto-read from local / connected-computer browser
        res = await read_browser_cookies(
            "cue",
            shell=self.shell,
            machine_id=mid or (self.default_machine() if self.default_machine else ""),
        )
        if res.get("ok") and res.get("creds"):
            patch = {**res["creds"], "source": res.get("source") or "browser", "mode": "native"}
            return self.auth_store.save("cue", patch)
        return auth

    # ------------------------------------------------------------------ OpenAPI v2 helpers
    async def _openapi_req(
        self,
        auth: dict[str, Any],
        path: str,
        body: Any = None,
        as_web_token: bool = False,
    ) -> tuple[bool, int, dict[str, Any]]:
        key = str(auth.get("access_token") if as_web_token else auth.get("api_key") or "").strip()
        if not key:
            return False, 401, {"error": "未配置 Cue API Key / Token"}
        base = (str(auth.get("openapi_base") or "") or CUE_MANUS_API_BASE).rstrip("/") + "/"
        url = base + path.lstrip("/")
        headers = _openapi_headers(key, as_web_token=as_web_token)
        async with _http_client(str(auth.get("proxy_url") or ""), 20.0, self.transport) as client:
            if body is None:
                r = await client.get(url, headers=headers)
            else:
                r = await client.post(url, headers=headers, json=body)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {}
        if not isinstance(data, dict):
            data = {}
        ok = r.is_success and data.get("ok") is True
        return ok, r.status_code, data

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
            ok, _, data = await self._openapi_req(auth, "agent.list")
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
            return {"ok": False, "error": "尚未配置 Cue API Key 或浏览器登录态"}
        if mode == "openapi":
            ok, status, data = await self._openapi_req(auth, "user.me")
            if not ok:
                return {"ok": False, "status": status, "auth_error": status in (401, 403),
                        "error": f"Manus OpenAPI 验证失败（HTTP {status}）"}
            agents = await self.list_agents(auth)
            handle = str(data.get("email") or "").strip()
            display = str(data.get("name") or "").strip()
            if auth_override is None and (handle or display):
                self.auth_store.save("cue", {"handle": handle, "display_name": display})
            return {"ok": True, "mode": "openapi", "handle": handle, "display_name": display, "agents": agents}

        ok, status, data = await self._connect_req(auth, "/agents.v1.AgentsService/ListAgents", {})
        if not ok:
            # Fallback: try OpenAPI with web token
            if auth.get("access_token"):
                ok2, status2, data2 = await self._openapi_req(auth, "user.me", as_web_token=True)
                if ok2:
                    return {"ok": True, "mode": "native", "handle": str(data2.get("email") or ""),
                            "display_name": str(data2.get("name") or ""), "agents": await self.list_agents(auth)}
                return {"ok": False, "status": status2, "auth_error": status2 in (401, 403),
                        "error": f"Cue 登录态验证失败（HTTP {status} / {status2}）"}
            return {"ok": False, "status": status, "auth_error": status in (401, 403),
                    "error": f"Cue 登录态验证失败（HTTP {status}）"}
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
        auth = await self._ensure_auth(mid)
        mode = self._effective_mode(auth)
        if not mode:
            return {"ok": False, "error": "Cue 尚未配置凭据：请点右上角配置 Manus API Key，或先在浏览器登录 cue.im 后点「从浏览器读取」。"}
        agent_id = str(conv.get("model") or auth.get("agent_id") or IM_AGENT).strip() or IM_AGENT
        session_id = str(conv.get("session") or "").strip()
        job_id = f"cue-{uuid.uuid4().hex[:10]}"
        job = _CueJob(job_id=job_id, agent_id=agent_id, session_id=session_id)
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
            return {"ok": False, "error": "Cue 尚未配置凭据"}
        job.status = "running"
        job.response = ""
        job.error = ""
        job.auth_error = False
        job.tools = []
        job.event.clear()
        job.task = asyncio.get_running_loop().create_task(self._run_turn(job, auth, mode, text))
        return {"ok": True, "job": job_id}

    async def wait(self, mid: str, job_id: str, cursor: int, seconds: int = 20) -> dict[str, Any]:
        job = self._jobs.get(job_id)
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
        return out

    async def stop(self, mid: str, job_id: str) -> dict[str, Any]:
        job = self._jobs.pop(job_id, None)
        if job and job.task and not job.task.done():
            job.task.cancel()
        return {"ok": True}

    # ------------------------------------------------------------------ Turn execution
    async def _run_turn(self, job: _CueJob, auth: dict[str, Any], mode: str, prompt: str) -> None:
        try:
            if mode == "openapi":
                await self._run_openapi_turn(job, auth, prompt, as_web_token=False)
            else:
                await self._run_native_turn(job, auth, prompt)
        except asyncio.CancelledError:
            job.status = "turn_failed"
            job.error = "已停止"
            job.event.set()
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cue turn error: %r", e)
            job.status = "turn_failed"
            job.error = str(e)
            job.event.set()

    async def _run_openapi_turn(self, job: _CueJob, auth: dict[str, Any], prompt: str, as_web_token: bool) -> None:
        task_id = job.session_id or IM_TASK
        if not job.session_id and job.agent_id and job.agent_id != IM_AGENT:
            ok, status, detail = await self._openapi_req(
                auth, f"agent.detail?{urlencode({'agent_id': job.agent_id})}", as_web_token=as_web_token
            )
            if ok:
                tid = str(((detail.get("data") or {}) if isinstance(detail.get("data"), dict) else {}).get("task_id") or "")
                if tid:
                    task_id = tid
            elif status in (401, 403):
                job.status = "turn_failed"
                job.auth_error = True
                job.error = f"Manus API 认证失败（HTTP {status}）"
                job.event.set()
                return

        ok_b, st_b, before = await self._openapi_req(
            auth, f"task.listMessages?{urlencode({'task_id': task_id, 'order': 'desc', 'limit': '1'})}",
            as_web_token=as_web_token,
        )
        if not ok_b:
            job.status = "turn_failed"
            job.auth_error = st_b in (401, 403)
            job.error = f"读取 Cue 会话失败（HTTP {st_b}）"
            job.event.set()
            return
        before_msgs = before.get("messages") or []
        after_id = str(before_msgs[0].get("id") or "") if before_msgs and isinstance(before_msgs[0], dict) else ""

        ok_s, st_s, sent = await self._openapi_req(
            auth, "task.sendMessage",
            {"task_id": task_id, "message": {"content": prompt}},
            as_web_token=as_web_token,
        )
        if not ok_s:
            job.status = "turn_failed"
            job.auth_error = st_s in (401, 403)
            job.error = f"发送 Cue 消息失败（HTTP {st_s}）"
            job.event.set()
            return
        job.session_id = str(sent.get("task_id") or task_id)

        deadline = time.monotonic() + 600.0
        last_reply = ""
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            done, reply_text, err_text, is_auth = await self._poll_openapi_once(
                auth, job.session_id, after_id, prompt, as_web_token
            )
            if err_text:
                job.status = "turn_failed"
                job.auth_error = is_auth
                job.error = err_text
                job.event.set()
                return
            if reply_text:
                last_reply = reply_text
            if done:
                job.response = last_reply or "（Cue 已完成，但没有返回文字）"
                job.status = "turn_done"
                job.event.set()
                return

        job.status = "turn_failed"
        job.error = "等待 Cue 回复超时"
        job.event.set()

    async def _poll_openapi_once(
        self,
        auth: dict[str, Any],
        task_id: str,
        after_id: str,
        expected_user_text: str,
        as_web_token: bool,
    ) -> tuple[bool, str, str, bool]:
        params: dict[str, str] = {"task_id": task_id, "order": "asc", "limit": "100"}
        if after_id:
            params["start_event_id"] = after_id
        assistant_reply = ""
        received_user = False
        seen_cursors: set[str] = set()
        while True:
            ok, st, data = await self._openapi_req(
                auth, f"task.listMessages?{urlencode(params)}", as_web_token=as_web_token
            )
            if not ok:
                return False, assistant_reply, f"轮询 Cue 回复失败（HTTP {st}）", st in (401, 403)
            for ev in data.get("messages") or []:
                if not isinstance(ev, dict):
                    continue
                if after_id and ev.get("id") == after_id:
                    continue
                etype = ev.get("type")
                if etype == "user_message":
                    if received_user:
                        return True, assistant_reply, "", False
                    ucontent = ((ev.get("user_message") or {}) if isinstance(ev.get("user_message"), dict) else {}).get("content")
                    received_user = (ucontent == expected_user_text) or not expected_user_text
                if not received_user:
                    continue
                if etype == "assistant_message":
                    ac = ((ev.get("assistant_message") or {}) if isinstance(ev.get("assistant_message"), dict) else {}).get("content")
                    if isinstance(ac, str) and ac.strip():
                        assistant_reply = ac.strip()
                elif etype == "error_message":
                    return False, assistant_reply, "Cue 执行出错，请在 Manus 中查看会话", False
                status_obj = ev.get("status_update") if isinstance(ev.get("status_update"), dict) else {}
                st_val = str(status_obj.get("agent_status") or "").lower()
                if st_val in ("waiting", "stopped"):
                    if not assistant_reply and st_val == "waiting":
                        assistant_reply = "Cue 正在等待输入或确认，请打开 Manus 查看。"
                    return True, assistant_reply, "", False
                if st_val == "error":
                    return False, assistant_reply, "Cue 执行出错，请在 Manus 中查看会话", False
            nxt = str(data.get("next_cursor") or "").strip()
            if not nxt or nxt in seen_cursors:
                return False, assistant_reply, "", False
            seen_cursors.add(nxt)
            params.pop("start_event_id", None)
            params["cursor"] = nxt

    async def _run_native_turn(self, job: _CueJob, auth: dict[str, Any], prompt: str) -> None:
        """Native Cue ConnectRPC + Socket.IO v4 WebSocket turn (`wss://api.manus.im/socket.io/`)."""
        import websockets

        token = str(auth.get("access_token") or "").strip()
        if not token:
            job.status = "turn_failed"
            job.auth_error = True
            job.error = "Cue 浏览器登录态中未找到 session token，请重新从浏览器读取或改用 Manus API Key"
            job.event.set()
            return

        # Resolve session_id from agent list if not set yet
        if not job.session_id:
            agents = await self.list_agents(auth)
            hit = next((a for a in agents if a["id"] == job.agent_id and a.get("session_id")), None)
            if not hit and agents:
                hit = next((a for a in agents if a.get("session_id")), None)
            if hit and hit.get("session_id"):
                job.session_id = hit["session_id"]
            else:
                # Fallback to OpenAPI using web token
                await self._run_openapi_turn(job, auth, prompt, as_web_token=True)
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
            # 1) Engine.IO open -> send Socket.IO connect with auth token
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
                    job.status = "turn_failed"
                    job.auth_error = True
                    job.error = "Cue 实时连接鉴权失败，请重新从浏览器读取登录态"
                    job.event.set()
                    return
            if not connected:
                raise RuntimeError("Cue 实时 WebSocket 握手超时")

            # 2) Join session & send user message
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

            # 3) Receive streaming reply
            turn_deadline = time.monotonic() + 600.0
            while time.monotonic() < turn_deadline:
                wait_s = 2.0 if (latest_text and not seen_running) else 15.0
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=wait_s)
                except asyncio.TimeoutError:
                    if latest_text and not seen_running and last_update_at and (time.monotonic() - last_update_at) >= 4.0:
                        job.response = latest_text
                        job.status = "turn_done"
                        job.event.set()
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
                    job.status = "turn_failed"
                    job.error = str(payload.get("message") or payload.get("error") or "Cue Agent 执行失败")
                    job.event.set()
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
                    elif st_val in ("stopped", "waiting", "idle", "completed"):
                        if seen_running or latest_text:
                            job.response = latest_text or "（Cue 已完成）"
                            job.status = "turn_done"
                            job.event.set()
                            return
                    elif st_val in ("error", "failed"):
                        job.status = "turn_failed"
                        job.error = str(ev.get("message") or "Cue Agent 执行出错")
                        job.event.set()
                        return

        if latest_text:
            job.response = latest_text
            job.status = "turn_done"
        else:
            job.status = "turn_failed"
            job.error = "Cue 实时连接关闭，未收到回复"
        job.event.set()
