"""Tests for Meta Muse (`muse.ai` native Noise gateway) and hardened Cue by Manus cloud engine."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiw_api import bot_groups as G
from aiw_api import bots as B
from aiw_api.browser_cookies import (
    decode_jwt_claims,
    extract_cue_credentials_from_cookies,
    extract_muse_credentials_from_cookies,
)
from aiw_api.cloud_auth import CLOUD_BOTS, CloudAuthStore
from aiw_api.crypto import SecretBox
from aiw_api.cue_engine import (
    CueEngine,
    build_cue_socket_url,
    encode_socketio_event,
    parse_socketio_packet,
)
from aiw_api.muse_engine import (
    MuseControlPlane,
    MuseEngine,
    NoiseTransport,
    NoiseXXInitiator,
    PbWriter,
    SymmetricState,
    _dh,
    _gen_x25519,
    pb_decode,
    pb_get,
)
from aiw_api.store import Store


def _make_jwt(payload: dict) -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


# --------------------------------------------------------------------------------------------- Muse native tests
def test_muse_protobuf_noise_and_transport_roundtrip():
    raw = PbWriter().uint(1, 42).string(2, "hello").bool(3, True).finish()
    fields = pb_decode(raw)
    assert pb_get(fields, 1)["int"] == 42
    assert pb_get(fields, 2)["bytes"] == b"hello"
    assert pb_get(fields, 3)["int"] == 1

    init = NoiseXXInitiator()
    resp_ss = SymmetricState()
    resp_s_priv, resp_s_pub = _gen_x25519()
    resp_e_priv, resp_e_pub = _gen_x25519()

    m1 = init.write_message1(b"nonce-payload")
    init_e_pub = m1[:32]
    resp_ss.mix_hash(init_e_pub)
    assert resp_ss.decrypt_and_hash(m1[32:]) == b"nonce-payload"

    resp_ss.mix_hash(resp_e_pub)
    resp_ss.mix_key(_dh(resp_e_priv, init_e_pub))
    c_rs = resp_ss.encrypt_and_hash(resp_s_pub)
    resp_ss.mix_key(_dh(resp_s_priv, init_e_pub))
    c_p2 = resp_ss.encrypt_and_hash(b"attestation")
    assert init.read_message2(resp_e_pub + c_rs + c_p2) == b"attestation"

    m3 = init.write_message3(b"")
    init_s_pub = resp_ss.decrypt_and_hash(m3[:48])
    resp_ss.mix_key(_dh(resp_e_priv, init_s_pub))
    assert resp_ss.decrypt_and_hash(m3[48:]) == b""

    init_send, init_recv = init.split()
    resp_recv, resp_send = resp_ss.split()

    client_t = NoiseTransport(init_send, init_recv)
    sid, frames = client_t.encrypt_request("daemon", "POST", "/chat/stream", [("Content-Type", "application/json")], b"{}")
    assert sid == 1 and len(frames) == 1
    assert pb_get(pb_decode(resp_recv.decrypt_with_ad(b"", frames[0])), 4)["bytes"]

    resp_pb = PbWriter().uint(1, 200).bytes(3, b'{"ok":true}\n').bool(4, True).finish()
    sf = PbWriter().uint(1, sid).bytes(3, resp_pb).finish()
    sr = PbWriter().bytes(1, sf).finish()
    nf = PbWriter().uint(1, 7).uint(2, 0).uint(3, 1).bytes(4, sr).finish()
    decoded = client_t.decrypt(resp_send.encrypt_with_ad(b"", nf))
    assert decoded["kind"] == "response" and decoded["status"] == 200 and decoded["body"] == b'{"ok":true}\n'


@pytest.mark.asyncio
async def test_muse_control_plane_and_no_auto_cookie(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "c_user=1" in request.headers.get("cookie", "")
        p = request.url.path
        if p == "/api/auth/check":
            return httpx.Response(200, json={"ok": True, "viewer_id": "v123"})
        if p == "/api/hatch/vm/wake":
            return httpx.Response(200, json={"ok": True})
        if p == "/api/hatch/lease-vm":
            return httpx.Response(200, json={"status": "assigned", "vmId": "vm-abc", "vmName": "vm-abc",
                                             "gatewayUrl": "wss://vm-abc.metaaivm.com/"})
        if p == "/api/hatch/token":
            return httpx.Response(200, json={"token": "tok-xyz", "notary_token": "notary-1"})
        return httpx.Response(404, json={})

    cp = MuseControlPlane("c_user=1", transport=httpx.MockTransport(handler))
    assert await cp.auth_check() == {"ok": True, "viewer_id": "v123"}
    creds = await cp.credentials()
    assert creds["vm_id"] == "vm-abc" and creds["auth_token"] == "tok-xyz"

    # Unconfigured Muse must fail cleanly without auto-reading browser cookies
    store = Store(str(tmp_path / "aiw.db"))
    auth_store = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    eng = MuseEngine(auth_store)
    st = await eng.start("cloud", {"session": ""}, "hi")
    assert st["ok"] is False


# --------------------------------------------------------------------------------------------- Cookie & Cue tests
def test_cookie_extractors_and_cloud_auth_store(tmp_path):
    assert set(CLOUD_BOTS) == {"cue", "muse"}
    jwt_tok = _make_jwt({"email": "alice@cue.im", "name": "Alice", "exp": 1900000000})
    cookies = [
        {"name": "cue_session_id", "value": jwt_tok, "domain": ".cue.im", "path": "/"},
        {"name": "c_user", "value": "999", "domain": ".muse.ai", "path": "/"},
        {"name": "xs", "value": "abc", "domain": "muse.ai", "path": "/"},
        {"name": "unrelated", "value": "x", "domain": ".example.com", "path": "/"},
    ]
    cue = extract_cue_credentials_from_cookies(cookies)
    assert cue["access_token"] == jwt_tok and cue["handle"] == "alice@cue.im"
    assert "unrelated" not in cue["cookie_header"]
    assert decode_jwt_claims(jwt_tok)["expires_at_ms"] == 1900000000 * 1000

    muse = extract_muse_credentials_from_cookies(cookies)
    assert muse["cookie_count"] == 2 and "c_user=999" in muse["cookie_header"]

    store = Store(str(tmp_path / "aiw.db"))
    auth = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    # Cue cookie without experimental_native stays unconfigured
    auth.save("cue", {"access_token": jwt_tok, "cookie_header": "cue_session_id=abc"})
    assert auth.public_view("cue")["configured"] is False
    auth.save("cue", {"api_key": "sk-manus-secret-12345678"})
    assert auth.public_view("cue")["configured"] is True and auth.public_view("cue")["mode"] == "openapi"
    # Muse cookie configures Muse
    auth.save("muse", {"cookie_header": "c_user=999; xs=abc"})
    assert auth.public_view("muse")["configured"] is True


@pytest.mark.asyncio
async def test_cue_no_auto_browser_cookie_and_experimental_gate(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    auth_store = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    eng = CueEngine(auth_store, poll_backoff=(0.01,))

    res = await eng.start("cloud", {"model": "", "session": ""}, "hello")
    assert res["ok"] is False and "API Key" in res["error"]

    auth_store.save("cue", {"access_token": "tok", "cookie_header": "cue_session_id=tok"})
    assert eng._effective_mode(auth_store.load("cue")) == ""
    auth_store.save("cue", {"experimental_native": True, "mode": "native"})
    assert eng._effective_mode(auth_store.load("cue")) == "native"


@pytest.mark.asyncio
async def test_cue_openapi_backoff_429_eviction_error_and_waiting_card(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    auth_store = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    auth_store.save("cue", {"api_key": "test-manus-key"})

    poll_n = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/task.sendMessage"):
            return httpx.Response(200, json={"ok": True, "task_id": "task-long-1"})
        if path.endswith("/task.listMessages"):
            if request.url.params.get("order") == "desc":
                return httpx.Response(200, json={"ok": True, "messages": [{"id": "m0"}]})
            poll_n["n"] += 1
            if poll_n["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "messages": [
                {"id": "m1", "type": "user_message", "user_message": {"content": "hello"}},
                {"id": "m2", "type": "assistant_message", "assistant_message": {"content": "done after 429"}},
                {"id": "m3", "type": "status_update", "status_update": {"agent_status": "stopped"}},
            ]})
        return httpx.Response(404, json={"ok": False})

    eng = CueEngine(auth_store, transport=httpx.MockTransport(handler), poll_backoff=(0.01, 0.02))
    st = await eng.start("cloud", {"model": "", "session": ""}, "hello")
    jid = st["job"]
    res = {}
    for _ in range(5):
        res = await eng.wait("cloud", jid, 0, seconds=5)
        if res["status"] != "running":
            break
    assert res["status"] == "turn_done" and res["response"] == "done after 429"
    assert jid not in eng._jobs

    def err_handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/task.sendMessage"):
            return httpx.Response(200, json={"ok": True, "task_id": "t-err"})
        if p.endswith("/task.listMessages"):
            if request.url.params.get("order") == "desc":
                return httpx.Response(200, json={"ok": True, "messages": []})
            return httpx.Response(200, json={"ok": True, "messages": [
                {"id": "u1", "type": "user_message", "user_message": {"content": "fail me"}},
                {"id": "e1", "type": "error_message", "error_message": {"content": "Insufficient credits"}},
            ]})
        return httpx.Response(404, json={"ok": False})

    eng_err = CueEngine(auth_store, transport=httpx.MockTransport(err_handler), poll_backoff=(0.01,))
    st2 = await eng_err.start("cloud", {"model": "", "session": ""}, "fail me")
    res2 = await eng_err.wait("cloud", st2["job"], 0, seconds=5)
    assert res2["status"] == "turn_failed" and "Insufficient credits" in res2["error"]

    def wait_handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/task.sendMessage"):
            return httpx.Response(200, json={"ok": True, "task_id": "t-wait"})
        if p.endswith("/task.listMessages"):
            if request.url.params.get("order") == "desc":
                return httpx.Response(200, json={"ok": True, "messages": []})
            return httpx.Response(200, json={"ok": True, "messages": [
                {"id": "u1", "type": "user_message", "user_message": {"content": "need confirm"}},
                {"id": "w1", "type": "status_update", "status_update": {"agent_status": "waiting"}},
            ]})
        return httpx.Response(404, json={"ok": False})

    eng_wait = CueEngine(auth_store, transport=httpx.MockTransport(wait_handler), poll_backoff=(0.01,))
    st3 = await eng_wait.start("cloud", {"model": "", "session": ""}, "need confirm")
    res3 = await eng_wait.wait("cloud", st3["job"], 0, seconds=5)
    assert res3["status"] == "blocked" and res3["waiting_confirm"] is True


def test_cue_socketio_helpers():
    assert build_cue_socket_url("https://api.manus.im", "dev-123").startswith("wss://api.manus.im/socket.io/?")
    assert parse_socketio_packet("2")["kind"] == "ping"
    ev = parse_socketio_packet(encode_socketio_event("message", {"type": "event", "x": 1}))
    assert ev["kind"] == "event" and ev["payload"]["x"] == 1


# --------------------------------------------------------------------------------------------- Bots & Groups integration
def test_bots_and_groups_muse_and_cue_cloud(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    store.upsert_machine({"id": "ai-linux-001", "provider": "local", "provider_vm_id": "v1",
                          "name": "Local PC", "agent_endpoint": "ws://localhost:8001"})
    secrets = SecretBox(store, str(tmp_path))
    shell_calls = []

    async def fake_shell(mid: str, cmd: str, timeout: int) -> str:
        shell_calls.append((mid, cmd))
        return "DIRTY 0\nGROUP_OK main\n"

    bots = B.Bots(store, shell=fake_shell, secrets=secrets)
    groups = G.Groups(bots)

    app = FastAPI()
    B.mount(app, bots, lambda: None)
    G.mount(app, groups, lambda: None)
    client = TestClient(app)

    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert eng_map["muse"]["cloud"] is True and eng_map["cue"]["cloud"] is True

    # PUT / GET / DELETE /api/bots/engines/muse/auth (verify JSON body parsing, not query param)
    r = client.put("/api/bots/engines/muse/auth", json={"cookie_header": "c_user=1; xs=abc", "verify": False})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["auth"]["configured"] is True
    assert client.get("/api/bots/engines/muse/auth").json()["has_cookie"] is True
    assert client.delete("/api/bots/engines/muse/auth").status_code == 200

    # Pure cloud group (muse + cue) skips git shell completely
    r = client.post("/api/bots/groups", json={"title": "云端讨论", "members": ["muse", "cue"]})
    assert r.status_code == 200 and len(shell_calls) == 0 and r.json()["repo"] == ""

    # Mixed group: codex gets git worktree; dynamically add muse & cue without running git
    r = client.post("/api/bots/groups", json={"title": "开发群", "repo": "~/workspace/demo", "members": ["codex"]})
    assert r.status_code == 200 and len(shell_calls) == 1
    gid = r.json()["id"]

    r = client.post(f"/api/bots/groups/{gid}/members", json={"members": ["muse", "cue"]})
    assert r.status_code == 200 and len(shell_calls) == 1
    g_after = r.json()
    assert [m["bot"] for m in g_after["members"]] == ["codex", "muse", "cue"]

    ms = groups.merge_script(g_after)
    assert f"aiw/{gid}/codex" in ms and "/muse" not in ms and "/cue" not in ms
