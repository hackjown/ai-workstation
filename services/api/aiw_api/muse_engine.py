"""Meta Muse native cloud engine (HTTPS control plane + Noise_XX_25519_AESGCM_SHA256 over WebSocket).

Reference protocol: qybaihe/botark (`src/main/muse-native/`: `protobuf.ts`, `noise.ts`, `transport.ts`,
`controlPlane.ts`, `gateway.ts`, `chat.ts`).
Implements the same Engine interface as `AgyEngine` / `CliEngine` (`models`, `start`, `send`, `wait`, `stop`).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .browser_cookies import read_browser_cookies
from .cloud_auth import CloudAuthStore

log = logging.getLogger("aiw.muse")

MUSE_ORIGIN = "https://muse.ai"
MUSE_NOISE_URL = "wss://hatch.metaaivm.com/v1/noise"
MUSE_APP_ID = "hatch-web"
MUSE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
MUSE_CHAT_CAPABILITIES = [
    "hotel_results",
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
]
MUSE_BUSY_ACTIVITY = {
    "responding",
    "composing",
    "making_something",
    "working",
    "waiting_for_subagents",
    "compacting",
    "needs_approval",
}
MUSE_SERVICE = {"daemon": 0, "sentinel": 1, "vault": 2, "authd": 3, "ssh_access": 4}
_MAX_CHUNK = 65489
_NOISE_PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"


# --------------------------------------------------------------------------------------------- Minimal Protobuf codec
class PbWriter:
    def __init__(self) -> None:
        self._buf = bytearray()

    def _varint(self, v: int) -> None:
        x = v & ((1 << 64) - 1)
        while x > 0x7F:
            self._buf.append((x & 0x7F) | 0x80)
            x >>= 7
        self._buf.append(x)

    def _tag(self, field_no: int, wire: int) -> None:
        self._varint((field_no << 3) | wire)

    def uint(self, field_no: int, v: int) -> "PbWriter":
        self._tag(field_no, 0)
        self._varint(int(v))
        return self

    def bool(self, field_no: int, v: bool) -> "PbWriter":
        return self.uint(field_no, 1 if v else 0)

    def bytes(self, field_no: int, v: bytes) -> "PbWriter":
        self._tag(field_no, 2)
        self._varint(len(v))
        self._buf.extend(v)
        return self

    def string(self, field_no: int, v: str) -> "PbWriter":
        return self.bytes(field_no, v.encode("utf-8"))

    def finish(self) -> bytes:
        return bytes(self._buf)


def pb_decode(buf: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pos = 0
    n = len(buf)

    def read_varint() -> int:
        nonlocal pos
        res = 0
        shift = 0
        while True:
            if pos >= n:
                raise ValueError("pb: truncated varint")
            b = buf[pos]
            pos += 1
            res |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                return res
            shift += 7
            if shift > 70:
                raise ValueError("pb: varint too long")

    while pos < n:
        key = read_varint()
        field_no = key >> 3
        wire = key & 7
        if field_no == 0:
            raise ValueError("pb: field 0")
        if wire == 0:
            out.append({"field": field_no, "wire": 0, "int": read_varint()})
        elif wire == 2:
            ln = read_varint()
            if pos + ln > n:
                raise ValueError("pb: truncated bytes")
            out.append({"field": field_no, "wire": 2, "bytes": buf[pos:pos + ln]})
            pos += ln
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"pb: unsupported wire type {wire}")
    return out


def pb_get(fields: list[dict[str, Any]], n: int) -> Optional[dict[str, Any]]:
    for f in fields:
        if f["field"] == n:
            return f
    return None


def _is_owner_rv_challenge(payload: bytes) -> bool:
    try:
        fields = pb_decode(payload)
        att = pb_get(fields, 1)
        state = pb_get(fields, 2)
        nonce = pb_get(fields, 3)
        return bool(
            att and att["wire"] == 2
            and state and state["wire"] == 0 and state.get("int") in (1, 2)
            and nonce and nonce["wire"] == 2 and len(nonce.get("bytes") or b"") == 32
        )
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------------- Noise_XX_25519_AESGCM_SHA256
def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf2(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    temp = _hmac_sha256(ck, ikm)
    o1 = _hmac_sha256(temp, b"\x01")
    o2 = _hmac_sha256(temp, o1 + b"\x02")
    return o1, o2


def _gen_x25519() -> tuple[X25519PrivateKey, bytes]:
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, pub


def _dh(priv: X25519PrivateKey, remote_pub: bytes) -> bytes:
    return priv.exchange(X25519PublicKey.from_public_bytes(remote_pub))


class CipherState:
    def __init__(self, key: Optional[bytes] = None) -> None:
        self._aes = AESGCM(key) if key is not None else None
        self._n = 0
        self._poisoned = False

    def _iv(self) -> bytes:
        n = self._n
        self._n += 1
        return b"\x00\x00\x00\x00" + struct.pack(">Q", n)

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._poisoned:
            raise RuntimeError("CipherState: poisoned")
        if self._aes is None:
            return plaintext
        try:
            return self._aes.encrypt(self._iv(), plaintext, ad)
        except Exception:
            self._poisoned = True
            raise

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._poisoned:
            raise RuntimeError("CipherState: poisoned")
        if self._aes is None:
            return ciphertext
        if len(ciphertext) < 16:
            self._poisoned = True
            raise ValueError("CipherState: ciphertext too short")
        try:
            return self._aes.decrypt(self._iv(), ciphertext, ad)
        except Exception:
            self._poisoned = True
            raise


class SymmetricState:
    def __init__(self) -> None:
        h = _NOISE_PROTOCOL.ljust(32, b"\x00")
        self.h = h
        self.ck = h
        self.cs = CipherState()
        self.mix_hash(b"")

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes) -> None:
        ck, k = _hkdf2(self.ck, ikm)
        self.ck = ck
        self.cs = CipherState(k)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        c = self.cs.encrypt_with_ad(self.h, plaintext)
        self.mix_hash(c)
        return c

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        p = self.cs.decrypt_with_ad(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return p

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _hkdf2(self.ck, b"")
        return CipherState(k1), CipherState(k2)


class NoiseXXInitiator:
    def __init__(self) -> None:
        self._ss = SymmetricState()
        self._e: Optional[tuple[X25519PrivateKey, bytes]] = None
        self._re: Optional[bytes] = None
        self._rs: Optional[bytes] = None
        self._phase = 1

    def write_message1(self, payload: bytes) -> bytes:
        if self._phase != 1:
            raise RuntimeError("NoiseXX: write_message1 wrong phase")
        self._e = _gen_x25519()
        self._ss.mix_hash(self._e[1])
        c = self._ss.encrypt_and_hash(payload)
        self._phase = 2
        return self._e[1] + c

    def read_message2(self, msg: bytes) -> bytes:
        if self._phase != 2 or self._e is None:
            raise RuntimeError("NoiseXX: read_message2 wrong phase")
        if len(msg) < 96:
            raise ValueError(f"NoiseXX: message 2 too short ({len(msg)})")
        self._re = msg[:32]
        self._ss.mix_hash(self._re)
        self._ss.mix_key(_dh(self._e[0], self._re))
        self._rs = self._ss.decrypt_and_hash(msg[32:80])
        self._ss.mix_key(_dh(self._e[0], self._rs))
        payload = self._ss.decrypt_and_hash(msg[80:])
        self._phase = 3
        return payload

    def write_message3(self, payload: bytes) -> bytes:
        if self._phase != 3 or self._re is None:
            raise RuntimeError("NoiseXX: write_message3 wrong phase")
        s_priv, s_pub = _gen_x25519()
        c1 = self._ss.encrypt_and_hash(s_pub)
        self._ss.mix_key(_dh(s_priv, self._re))
        c2 = self._ss.encrypt_and_hash(payload)
        self._phase = 4
        return c1 + c2

    def split(self) -> tuple[CipherState, CipherState]:
        if self._phase != 4:
            raise RuntimeError("NoiseXX: split wrong phase")
        self._phase = 5
        return self._ss.split()


# --------------------------------------------------------------------------------------------- HTTP-over-Noise Transport
class NoiseTransport:
    def __init__(self, send_cs: CipherState, recv_cs: CipherState) -> None:
        self._send = send_cs
        self._recv = recv_cs
        self._next_stream = 1
        self._assemblies: dict[int, dict[str, Any]] = {}

    def encrypt_request(
        self,
        service: str,
        verb: str,
        path: str,
        headers: list[tuple[str, str]],
        body: bytes = b"",
    ) -> tuple[int, list[bytes]]:
        sid = self._next_stream
        self._next_stream += 1
        app = PbWriter().string(1, verb).string(2, path)
        for k, v in headers:
            app.bytes(3, PbWriter().string(1, k).string(2, v).finish())
        if body:
            app.bytes(4, body)
        app.bool(5, True)
        sf = PbWriter().uint(1, sid).bytes(2, app.finish()).finish()
        return sid, self._wrap(service, sf)

    def encrypt_reset(self, service: str, stream_id: int, reason: str = "") -> list[bytes]:
        rst = PbWriter().uint(1, 1)
        if reason:
            rst.string(2, reason)
        sf = PbWriter().uint(1, stream_id).bytes(5, rst.finish()).finish()
        return self._wrap(service, sf)

    def _wrap(self, service: str, service_frame: bytes) -> list[bytes]:
        sr = PbWriter()
        svc_id = MUSE_SERVICE.get(service, 0)
        if svc_id != 0:
            sr.uint(1, svc_id)
        sr.bytes(2, service_frame)
        payload = sr.finish()
        chunk_id = struct.unpack("<q", os.urandom(8))[0]
        total = max(1, (len(payload) + _MAX_CHUNK - 1) // _MAX_CHUNK)
        frames: list[bytes] = []
        for i in range(total):
            part = payload[i * _MAX_CHUNK:(i + 1) * _MAX_CHUNK]
            nf = PbWriter().uint(1, chunk_id).uint(2, i).uint(3, total).bytes(4, part).finish()
            frames.append(self._send.encrypt_with_ad(b"", nf))
        return frames

    def decrypt(self, msg: bytes) -> Optional[dict[str, Any]]:
        plain = self._recv.decrypt_with_ad(b"", msg)
        nf = pb_decode(plain)
        chunk_id = int((pb_get(nf, 1) or {}).get("int") or 0)
        idx = int((pb_get(nf, 2) or {}).get("int") or 0)
        total = int((pb_get(nf, 3) or {}).get("int") or 1)
        payload = (pb_get(nf, 4) or {}).get("bytes") or b""
        if total < 1 or total > 256 or idx < 0 or idx >= total:
            raise ValueError("bad noise chunk header")
        if total == 1:
            whole = payload
        else:
            now = time.monotonic()
            for k in [k for k, a in self._assemblies.items() if now - a["at"] > 60]:
                self._assemblies.pop(k, None)
            a = self._assemblies.setdefault(chunk_id, {"total": total, "chunks": {}, "at": now})
            a["chunks"][idx] = payload
            if len(a["chunks"]) < total:
                return None
            self._assemblies.pop(chunk_id, None)
            whole = b"".join(a["chunks"][i] for i in range(total))

        resp = (pb_get(pb_decode(whole), 1) or {}).get("bytes")
        if not resp:
            raise ValueError("empty ServiceResponse payload")
        sf = pb_decode(resp)
        sid = int((pb_get(sf, 1) or {}).get("int") or 0)
        r_bytes = (pb_get(sf, 3) or {}).get("bytes")
        if r_bytes is not None:
            rf = pb_decode(r_bytes)
            hdrs = []
            for f in rf:
                if f["field"] == 2 and f.get("bytes"):
                    hf = pb_decode(f["bytes"])
                    hk = ((pb_get(hf, 1) or {}).get("bytes") or b"").decode("utf-8", "replace")
                    hv = ((pb_get(hf, 2) or {}).get("bytes") or b"").decode("utf-8", "replace")
                    hdrs.append((hk, hv))
            return {
                "kind": "response",
                "stream_id": sid,
                "status": int((pb_get(rf, 1) or {}).get("int") or 0),
                "headers": hdrs,
                "body": (pb_get(rf, 3) or {}).get("bytes") or b"",
                "end_body": bool((pb_get(rf, 4) or {}).get("int") or 0),
            }
        bc_bytes = (pb_get(sf, 4) or {}).get("bytes")
        if bc_bytes is not None:
            bf = pb_decode(bc_bytes)
            return {
                "kind": "body_chunk",
                "stream_id": sid,
                "data": (pb_get(bf, 1) or {}).get("bytes") or b"",
                "end_body": bool((pb_get(bf, 2) or {}).get("int") or 0),
            }
        rs_bytes = (pb_get(sf, 5) or {}).get("bytes")
        if rs_bytes is not None:
            rf = pb_decode(rs_bytes)
            return {
                "kind": "reset",
                "stream_id": sid,
                "code": int((pb_get(rf, 1) or {}).get("int") or 0),
                "reason": ((pb_get(rf, 2) or {}).get("bytes") or b"").decode("utf-8", "replace"),
            }
        return None


# --------------------------------------------------------------------------------------------- Control Plane
def _vm_id_from_gateway(url: str) -> str:
    m = re.match(r"^wss://([a-z0-9-]{8,64})\.metaaivm\.com/?", (url or "").strip(), re.I)
    return m.group(1).lower() if m else ""


class MuseAuthError(RuntimeError):
    pass


class MuseControlPlane:
    def __init__(self, cookie_header: str, proxy_url: str = "", transport: Any = None) -> None:
        self.cookie_header = cookie_header
        self.proxy_url = proxy_url
        self.transport = transport
        self._lease: Optional[dict[str, str]] = None
        self._wake_retry = 0

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Origin": MUSE_ORIGIN,
            "Referer": MUSE_ORIGIN + "/",
            "User-Agent": MUSE_UA,
            "Cookie": self.cookie_header,
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        kw: dict[str, Any] = {"timeout": 20.0, "follow_redirects": False}
        if self.transport is not None:
            kw["transport"] = self.transport
        elif self.proxy_url:
            kw["proxy"] = self.proxy_url
        last_err: Optional[Exception] = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(0.3 * attempt)
            try:
                async with httpx.AsyncClient(**kw) as client:
                    r = await client.post(MUSE_ORIGIN + path, headers=self._headers(), json=body)
                try:
                    data = r.json()
                except Exception:  # noqa: BLE001
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                if r.status_code in (401, 403):
                    raise MuseAuthError(f"Muse 未登录或 Cookie 已过期（{path}: HTTP {r.status_code}）")
                if r.status_code in (429, 502, 503, 504) and attempt < 2:
                    last_err = RuntimeError(f"{path}: HTTP {r.status_code}")
                    continue
                if not r.is_success:
                    err = str(data.get("error") or "")[:120]
                    raise RuntimeError(f"{path}: HTTP {r.status_code}" + (f" {err}" if err else ""))
                return data
            except MuseAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt >= 2:
                    raise
        raise last_err or RuntimeError(f"{path}: failed")

    async def auth_check(self) -> dict[str, Any]:
        d = await self._post("/api/auth/check", {})
        return {"ok": d.get("ok") is True, "viewer_id": str(d.get("viewer_id") or "")}

    async def wake(self) -> None:
        try:
            await self._post("/api/hatch/vm/wake", {
                "vm_id": (self._lease or {}).get("vm_id"),
                "retry_count": self._wake_retry,
            })
            self._wake_retry += 1
        except Exception:  # noqa: BLE001
            pass

    async def lease_vm(self) -> dict[str, str]:
        last_err: Optional[Exception] = None
        for vm_type in ("standard", "hatch_vm"):
            try:
                d = await self._post("/api/hatch/lease-vm", {"vmType": vm_type})
                gw = str(d.get("gatewayUrl") or "")
                vm_name = str(d.get("vmName") or "")
                vm_id = str(d.get("vmId") or "") or _vm_id_from_gateway(gw) or vm_name
                if d.get("status") == "assigned" and gw and vm_id:
                    self._lease = {"vm_id": vm_id, "vm_name": vm_name or vm_id, "gateway_url": gw}
                    return self._lease
                last_err = RuntimeError(f"lease-vm: status={d.get('status')}")
            except MuseAuthError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise last_err or RuntimeError("Muse lease-vm 失败")

    async def credentials(self, force_refresh: bool = False) -> dict[str, str]:
        await self.wake()
        lease = self._lease if (self._lease and not force_refresh) else await self.lease_vm()
        body: dict[str, Any] = {"vmAddress": lease["gateway_url"], "vmName": lease["vm_name"]}
        if force_refresh:
            body["force_refresh"] = True
        t = await self._post("/api/hatch/token", body)
        auth_token = str(t.get("token") or "")
        if not auth_token:
            raise RuntimeError("Muse /api/hatch/token 未返回 token")
        notary = str(t.get("notary_token") or "").strip()
        if not notary:
            try:
                n = await self._post("/api/hatch/noise-notary-token", {"vmName": lease["vm_name"]})
                v = n.get("notaryToken") or n.get("notary_token")
                if isinstance(v, str) and v.strip():
                    notary = v.strip()
            except Exception:  # noqa: BLE001
                pass
        if notary.startswith("delegation."):
            notary = ""
        return {**lease, "auth_token": auth_token, "notary_token": notary}


# --------------------------------------------------------------------------------------------- Text extraction
def _extract_muse_text(payload: dict[str, Any]) -> str:
    # Check payload.transcript.messages[].content[].text first
    transcript = payload.get("transcript")
    if isinstance(transcript, dict):
        msgs = transcript.get("messages")
        if isinstance(msgs, list):
            parts: list[str] = []
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if str(m.get("role") or "").lower() not in ("", "assistant", "model"):
                    continue
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict):
                            txt = blk.get("text") or blk.get("content")
                            if isinstance(txt, str) and txt.strip():
                                parts.append(txt.strip())
            if parts:
                return re.sub(r"\[\[hatch_widget:[^\]]+\]\]", "", "\n".join(parts)).strip()
    for k in ("display_text", "markdown", "content", "text"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return re.sub(r"\[\[hatch_widget:[^\]]+\]\]", "", v).strip()
    return ""


# --------------------------------------------------------------------------------------------- Engine
@dataclass
class _MuseJob:
    job_id: str
    session_id: str
    status: str = "running"
    response: str = ""
    error: str = ""
    auth_error: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class MuseEngine:
    """Cloud engine for Meta Muse (`muse.ai` control plane + Noise_XX over `wss://hatch.metaaivm.com/v1/noise`)."""
    id = "muse"
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
        self.transport = transport
        self._jobs: dict[str, _MuseJob] = {}

    async def _ensure_auth(self, mid: str = "") -> dict[str, Any]:
        auth = self.auth_store.load("muse")
        if auth.get("cookie_header"):
            return auth
        res = await read_browser_cookies(
            "muse",
            shell=self.shell,
            machine_id=mid or (self.default_machine() if self.default_machine else ""),
        )
        if res.get("ok") and res.get("creds"):
            patch = {**res["creds"], "source": res.get("source") or "browser"}
            return self.auth_store.save("muse", patch)
        return auth

    async def verify(self, auth_override: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        auth = auth_override if auth_override is not None else self.auth_store.load("muse")
        cookie = str(auth.get("cookie_header") or "").strip()
        if not cookie:
            return {"ok": False, "error": "尚未配置 Muse Cookie：请先在浏览器登录 muse.ai 后点「从浏览器读取」，或手动粘贴 Cookie。"}
        cp = MuseControlPlane(cookie, str(auth.get("proxy_url") or ""), self.transport)
        try:
            res = await cp.auth_check()
        except MuseAuthError as e:
            return {"ok": False, "auth_error": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        if not res.get("ok"):
            return {"ok": False, "auth_error": True, "error": "Muse auth/check 返回未登录"}
        if auth_override is None and res.get("viewer_id"):
            self.auth_store.save("muse", {"viewer_id": res["viewer_id"]})
        return {"ok": True, "viewer_id": res.get("viewer_id") or ""}

    async def models(self, mid: str = "") -> dict[str, Any]:
        return {
            "ok": True,
            "groups": [{"id": "muse", "label": "Meta Muse（默认）", "efforts": [], "plain": True}],
            "efforts": [],
        }

    async def start(self, mid: str, conv: dict[str, Any], prompt: str, skip: bool = False) -> dict[str, Any]:
        auth = await self._ensure_auth(mid)
        if not auth.get("cookie_header"):
            return {"ok": False, "error": "Muse 尚未配置凭据：请在浏览器登录 muse.ai 后点「从浏览器读取」，或手动填入 Cookie。"}
        job_id = f"muse-{uuid.uuid4().hex[:10]}"
        job = _MuseJob(job_id=job_id, session_id=str(conv.get("session") or "").strip())
        self._jobs[job_id] = job
        job.task = asyncio.get_running_loop().create_task(self._run_turn(job, auth, prompt))
        return {"ok": True, "job": job_id}

    async def send(self, mid: str, job_id: str, text: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if not job or (job.task and not job.task.done()):
            return {"ok": False, "error": "Muse job not ready"}
        auth = await self._ensure_auth(mid)
        if not auth.get("cookie_header"):
            return {"ok": False, "error": "Muse 尚未配置凭据"}
        job.status = "running"
        job.response = ""
        job.error = ""
        job.auth_error = False
        job.tools = []
        job.event.clear()
        job.task = asyncio.get_running_loop().create_task(self._run_turn(job, auth, text))
        return {"ok": True, "job": job_id}

    async def wait(self, mid: str, job_id: str, cursor: int, seconds: int = 20) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if not job:
            return {"status": "turn_failed", "cursor": cursor, "error": "Muse 会话已结束"}
        if job.status == "running":
            try:
                await asyncio.wait_for(job.event.wait(), timeout=max(1, min(int(seconds or 20), 60)))
            except asyncio.TimeoutError:
                pass
            if job.status == "running":
                job.event.clear()
        new_tools = job.tools[cursor:]
        out: dict[str, Any] = {
            "status": job.status,
            "cursor": len(job.tools),
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

    async def _run_turn(self, job: _MuseJob, auth: dict[str, Any], prompt: str) -> None:
        import websockets

        try:
            cp = MuseControlPlane(str(auth.get("cookie_header") or ""), str(auth.get("proxy_url") or ""), self.transport)
            creds = await cp.credentials()
            params = {
                "vm_id": creds["vm_id"],
                "auth_token": creds["auth_token"],
                "app_id": MUSE_APP_ID,
                "request_id": str(uuid.uuid4()),
            }
            if creds.get("notary_token"):
                params["notary_token"] = creds["notary_token"]
            ws_url = f"{MUSE_NOISE_URL}?{urlencode(params)}"
            node_id = str(auth.get("node_id") or uuid.uuid4())

            async with websockets.connect(
                ws_url,
                additional_headers={"Origin": MUSE_ORIGIN, "User-Agent": MUSE_UA},
                open_timeout=15,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
            ) as ws:
                # 1) Noise_XX handshake
                hs = NoiseXXInitiator()
                payload1 = PbWriter().bytes(1, os.urandom(32)).finish()
                await ws.send(hs.write_message1(payload1))
                msg2 = await asyncio.wait_for(ws.recv(), timeout=15.0)
                if not isinstance(msg2, (bytes, bytearray)):
                    raise RuntimeError("Muse Noise handshake received non-binary frame")
                payload2 = hs.read_message2(bytes(msg2))
                if _is_owner_rv_challenge(payload2):
                    raise RuntimeError("该 Muse 账号绑定了 Confidential VM（Owner RV），请先在浏览器打开 muse.ai 完成设备验证或切换标准 VM。")
                await ws.send(hs.write_message3(b""))
                send_cs, recv_cs = hs.split()
                nt = NoiseTransport(send_cs, recv_cs)

                def _req_headers() -> list[tuple[str, str]]:
                    return [
                        ("Content-Type", "application/json"),
                        ("x-request-id", str(uuid.uuid4())),
                        ("x-app-id", MUSE_APP_ID),
                        ("Accept-Language", "en-US"),
                    ]

                # 2) Open chat.subscribe stream
                sub_body = json.dumps({"capabilities": MUSE_CHAT_CAPABILITIES}).encode("utf-8")
                sub_sid, sub_frames = nt.encrypt_request("daemon", "POST", "/chat/subscribe", _req_headers(), sub_body)
                for f in sub_frames:
                    await ws.send(f)

                # 3) Send chat.stream turn
                stream_params: dict[str, Any] = {
                    "items": [{"type": "text", "text": prompt}],
                    "node_id": node_id,
                    "capabilities": MUSE_CHAT_CAPABILITIES,
                }
                if job.session_id:
                    stream_params["session_id"] = job.session_id
                chat_body = json.dumps(stream_params, ensure_ascii=False).encode("utf-8")
                chat_sid, chat_frames = nt.encrypt_request("daemon", "POST", "/chat/stream", _req_headers(), chat_body)
                for f in chat_frames:
                    await ws.send(f)

                # 4) Read NDJSON events from chat.subscribe & ack from chat.stream
                line_bufs: dict[int, str] = {sub_sid: "", chat_sid: ""}
                msg_order: list[str] = []
                msg_texts: dict[str, str] = {}
                done_ids: set[str] = set()
                last_activity = ""
                last_text_at = 0.0
                turn_deadline = time.monotonic() + 600.0

                def _combined_text() -> str:
                    parts = [msg_texts[mid] for mid in msg_order if msg_texts.get(mid, "").strip()]
                    return "\n\n".join(parts).strip()

                while time.monotonic() < turn_deadline:
                    is_idle = (not last_activity) or (last_activity not in MUSE_BUSY_ACTIVITY)
                    all_done = bool(msg_order) and all(mid in done_ids for mid in msg_order)
                    wait_s = 2.0 if (all_done and is_idle and _combined_text()) else 15.0
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait_s)
                    except asyncio.TimeoutError:
                        if _combined_text() and is_idle and (all_done or (last_text_at and time.monotonic() - last_text_at >= 4.0)):
                            job.response = _combined_text()
                            job.status = "turn_done"
                            job.event.set()
                            return
                        continue

                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    frame = nt.decrypt(bytes(raw))
                    if not frame:
                        continue
                    sid = frame["stream_id"]
                    if frame["kind"] == "reset" and sid == chat_sid and not _combined_text():
                        raise RuntimeError(f"Muse stream reset ({frame.get('code')} {frame.get('reason')})")
                    chunk = frame.get("body") if frame["kind"] == "response" else (frame.get("data") if frame["kind"] == "body_chunk" else b"")
                    if frame["kind"] == "response" and frame.get("status", 200) >= 400 and sid == chat_sid:
                        err_txt = chunk.decode("utf-8", "replace")[:200]
                        raise RuntimeError(f"Muse chat.stream HTTP {frame.get('status')}: {err_txt}")
                    if not chunk:
                        continue
                    buf = line_bufs.get(sid, "") + chunk.decode("utf-8", "replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(ev, dict):
                            continue
                        if sid == chat_sid:
                            res_obj = ev.get("result") if isinstance(ev.get("result"), dict) else {}
                            asid = str(ev.get("session_id") or res_obj.get("session_id") or "")
                            if asid and not job.session_id:
                                job.session_id = asid
                            continue
                        if ev.get("type") != "event":
                            continue
                        ename = str(ev.get("event") or ev.get("event_name") or "")
                        payload = dict(ev.get("payload") if isinstance(ev.get("payload"), dict) else ev)
                        ev_sid = str(payload.get("session_id") or "")
                        if job.session_id and ev_sid and ev_sid != job.session_id:
                            continue

                        if ename == "delta.message_start":
                            mid_s = str(payload.get("message_id") or payload.get("id") or f"m{len(msg_order)}")
                            if mid_s not in msg_texts:
                                msg_order.append(mid_s)
                                msg_texts[mid_s] = ""
                        elif ename == "delta.text_append":
                            mid_s = str(payload.get("message_id") or payload.get("id") or (msg_order[-1] if msg_order else "m0"))
                            if mid_s not in msg_texts:
                                msg_order.append(mid_s)
                                msg_texts[mid_s] = ""
                            delta = str(payload.get("text") or payload.get("delta") or "")
                            if delta:
                                msg_texts[mid_s] += delta
                                last_text_at = time.monotonic()
                        elif ename == "delta.message_done":
                            mid_s = str(payload.get("message_id") or payload.get("id") or (msg_order[-1] if msg_order else "m0"))
                            if mid_s not in msg_texts:
                                msg_order.append(mid_s)
                                msg_texts[mid_s] = ""
                            full_txt = _extract_muse_text(payload)
                            if full_txt and len(full_txt) >= len(msg_texts[mid_s]):
                                msg_texts[mid_s] = full_txt
                            done_ids.add(mid_s)
                            last_text_at = time.monotonic()
                        elif ename in ("agent.status", "agent_status", "presence.status"):
                            code = str(payload.get("activity_code") or payload.get("status") or "").lower()
                            if code:
                                last_activity = code
                                if code in MUSE_BUSY_ACTIVITY:
                                    job.tools.append({"tool": "muse", "arg": code})
                                    job.event.set()
                                elif _combined_text() and all(m in done_ids for m in msg_order):
                                    job.response = _combined_text()
                                    job.status = "turn_done"
                                    job.event.set()
                                    return
                        elif "tool" in ename:
                            tname = str(payload.get("name") or payload.get("tool_name") or ename)
                            job.tools.append({"tool": tname[:60], "arg": str(payload.get("summary") or "")[:200]})
                            job.event.set()
                    line_bufs[sid] = buf

                if _combined_text():
                    job.response = _combined_text()
                    job.status = "turn_done"
                else:
                    job.status = "turn_failed"
                    job.error = "等待 Muse 回复超时"
                job.event.set()
        except asyncio.CancelledError:
            job.status = "turn_failed"
            job.error = "已停止"
            job.event.set()
            raise
        except MuseAuthError as e:
            job.status = "turn_failed"
            job.auth_error = True
            job.error = str(e)
            job.event.set()
        except Exception as e:  # noqa: BLE001
            log.warning("muse turn error: %r", e)
            job.status = "turn_failed"
            job.error = str(e)
            job.event.set()
