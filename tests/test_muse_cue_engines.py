"""Tests for Meta Muse & Cue by Manus cloud engines, browser cookie extraction, and group integration."""
from __future__ import annotations

import asyncio
import base64
import json
import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiw_api.store import Store
from aiw_api.crypto import SecretBox
from aiw_api.cloud_auth import CloudAuthStore
from aiw_api.browser_cookies import (
    decode_jwt_claims,
    extract_cue_credentials_from_cookies,
    extract_muse_credentials_from_cookies,
)
from aiw_api.cue_engine import (
    CueEngine,
    build_cue_socket_url,
    encode_socketio_event,
    parse_socketio_packet,
)
from aiw_api.muse_engine import (
    MuseControlPlane,
    NoiseTransport,
    NoiseXXInitiator,
    PbWriter,
    SymmetricState,
    _dh,
    _gen_x25519,
    pb_decode,
    pb_get,
)
from aiw_api import bots as B
from aiw_api import bot_groups as G


def _make_jwt(payload: dict) -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


def test_browser_cookie_extractors():
    jwt_tok = _make_jwt({"email": "alice@cue.im", "name": "Alice", "exp": 1900000000})
    cookies = [
        {"name": "cue_session_id", "value": jwt_tok, "domain": ".cue.im", "path": "/"},
        {"name": "other", "value": "123", "domain": ".manus.im", "path": "/"},
        {"name": "c_user", "value": "999", "domain": ".muse.ai", "path": "/"},
        {"name": "xs", "value": "abc", "domain": "muse.ai", "path": "/"},
        {"name": "unrelated", "value": "x", "domain": ".example.com", "path": "/"},
    ]
    cue = extract_cue_credentials_from_cookies(cookies)
    assert cue["access_token"] == jwt_tok
    assert cue["handle"] == "alice@cue.im"
    assert cue["display_name"] == "Alice"
    assert "cue_session_id=" in cue["cookie_header"]
    assert "unrelated" not in cue["cookie_header"]

    muse = extract_muse_credentials_from_cookies(cookies)
    assert muse["cookie_count"] == 2
    assert "c_user=999" in muse["cookie_header"]
    assert "xs=abc" in muse["cookie_header"]
    assert "cue_session_id" not in muse["cookie_header"]

    claims = decode_jwt_claims(jwt_tok)
    assert claims["handle"] == "alice@cue.im"
    assert claims["expires_at_ms"] == 1900000000 * 1000


def test_cloud_auth_store_encryption(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    store.upsert_machine({"id": "ai-linux-001", "provider": "local", "provider_vm_id": "v1",
                          "name": "Local PC", "agent_endpoint": "ws://localhost:8001"})
    secrets = SecretBox(store, str(tmp_path))
    auth = CloudAuthStore(store, secrets)

    auth.save("cue", {"api_key": "sk-manus-secret-12345678", "proxy_url": "http://127.0.0.1:7890"})
    pub = auth.public_view("cue")
    assert pub["configured"] is True
    assert pub["has_api_key"] is True
    assert "12345678" not in pub["api_key_preview"]
    assert pub["proxy_url"] == "http://127.0.0.1:7890"
    assert auth.load("cue")["api_key"] == "sk-manus-secret-12345678"

    auth.clear("cue")
    assert auth.public_view("cue")["configured"] is False


@pytest.mark.asyncio
async def test_cue_openapi_verify_and_turn(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    auth_store = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    auth_store.save("cue", {"api_key": "test-manus-key"})

    poll_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-manus-api-key") == "test-manus-key"
        path = request.url.path
        if path.endswith("/user.me"):
            return httpx.Response(200, json={"ok": True, "user_id": "u1", "email": "dev@manus.ai", "name": "Dev"})
        if path.endswith("/agent.list"):
            return httpx.Response(200, json={"ok": True, "data": [{"id": "ag-1", "task_id": "t-1", "nickname": "Researcher"}]})
        if path.endswith("/task.sendMessage"):
            return httpx.Response(200, json={"ok": True, "task_id": "agent-default-main_task"})
        if path.endswith("/task.listMessages"):
            if request.url.params.get("order") == "desc":
                return httpx.Response(200, json={"ok": True, "messages": [{"id": "m0"}]})
            poll_calls["n"] += 1
            return httpx.Response(200, json={"ok": True, "messages": [
                {"id": "m1", "type": "user_message", "user_message": {"content": "hello cue"}},
                {"id": "m2", "type": "assistant_message", "assistant_message": {"content": "hi from cue!"}},
                {"id": "m3", "type": "status_update", "status_update": {"agent_status": "stopped"}},
            ]})
        return httpx.Response(404, json={"ok": False})

    eng = CueEngine(auth_store, transport=httpx.MockTransport(handler))
    v = await eng.verify()
    assert v["ok"] is True
    assert v["handle"] == "dev@manus.ai"
    assert len(v["agents"]) == 2

    m = await eng.models("cloud")
    assert m["ok"] is True
    assert any(g["id"] == "ag-1" for g in m["groups"])

    st = await eng.start("cloud", {"model": "", "session": ""}, "hello cue")
    assert st["ok"] is True
    res = await eng.wait("cloud", st["job"], 0, seconds=10)
    assert res["status"] == "turn_done"
    assert res["response"] == "hi from cue!"


def test_cue_socketio_helpers():
    url = build_cue_socket_url("https://api.manus.im", "dev-123")
    assert url.startswith("wss://api.manus.im/socket.io/?")
    assert "EIO=4" in url and "deviceId=dev-123" in url
    assert parse_socketio_packet("2")["kind"] == "ping"
    assert parse_socketio_packet('0{"sid":"abc"}')["kind"] == "open"
    assert parse_socketio_packet('40{"sid":"xyz"}')["kind"] == "connect"
    ev = parse_socketio_packet(encode_socketio_event("message", {"type": "event", "x": 1}))
    assert ev["kind"] == "event" and ev["event"] == "message" and ev["payload"]["x"] == 1


def test_muse_protobuf_noise_and_transport_roundtrip():
    # 1. Protobuf round-trip
    raw = PbWriter().uint(1, 42).string(2, "hello").bool(3, True).finish()
    fields = pb_decode(raw)
    assert pb_get(fields, 1)["int"] == 42
    assert pb_get(fields, 2)["bytes"] == b"hello"
    assert pb_get(fields, 3)["int"] == 1

    # 2. Noise_XX_25519_AESGCM_SHA256 full initiator <-> responder handshake
    init = NoiseXXInitiator()
    resp_ss = SymmetricState()
    resp_s_priv, resp_s_pub = _gen_x25519()
    resp_e_priv, resp_e_pub = _gen_x25519()

    # -> e
    m1 = init.write_message1(b"nonce-payload")
    init_e_pub = m1[:32]
    resp_ss.mix_hash(init_e_pub)
    p1 = resp_ss.decrypt_and_hash(m1[32:])
    assert p1 == b"nonce-payload"

    # <- e, ee, s, es
    resp_ss.mix_hash(resp_e_pub)
    resp_ss.mix_key(_dh(resp_e_priv, init_e_pub))
    c_rs = resp_ss.encrypt_and_hash(resp_s_pub)
    resp_ss.mix_key(_dh(resp_s_priv, init_e_pub))
    c_p2 = resp_ss.encrypt_and_hash(b"attestation")
    m2 = resp_e_pub + c_rs + c_p2

    p2 = init.read_message2(m2)
    assert p2 == b"attestation"

    # -> s, se
    m3 = init.write_message3(b"")
    init_s_pub = resp_ss.decrypt_and_hash(m3[:48])
    resp_ss.mix_key(_dh(resp_e_priv, init_s_pub))
    p3 = resp_ss.decrypt_and_hash(m3[48:])
    assert p3 == b""

    init_send, init_recv = init.split()
    resp_recv, resp_send = resp_ss.split()

    # 3. NoiseTransport request -> decrypt + synthetic ServiceResponse -> decrypt
    client_t = NoiseTransport(init_send, init_recv)
    sid, frames = client_t.encrypt_request("daemon", "POST", "/chat/stream", [("Content-Type", "application/json")], b"{}")
    assert sid == 1 and len(frames) == 1
    # Verify responder can decrypt client's frame
    plain_req = resp_recv.decrypt_with_ad(b"", frames[0])
    assert pb_get(pb_decode(plain_req), 4)["bytes"]

    # Build a synthetic server ServiceResponse
    resp_pb = PbWriter().uint(1, 200).bytes(3, b'{"ok":true}\n').bool(4, True).finish()
    sf = PbWriter().uint(1, sid).bytes(3, resp_pb).finish()
    sr = PbWriter().bytes(1, sf).finish()
    nf = PbWriter().uint(1, 7).uint(2, 0).uint(3, 1).bytes(4, sr).finish()
    enc_resp = resp_send.encrypt_with_ad(b"", nf)

    decoded = client_t.decrypt(enc_resp)
    assert decoded["kind"] == "response"
    assert decoded["stream_id"] == sid
    assert decoded["status"] == 200
    assert decoded["body"] == b'{"ok":true}\n'
    assert decoded["end_body"] is True


@pytest.mark.asyncio
async def test_muse_control_plane():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "c_user=1" in request.headers.get("cookie", "")
        p = request.url.path
        if p == "/api/auth/check":
            return httpx.Response(200, json={"ok": True, "viewer_id": "v123"})
        if p == "/api/hatch/vm/wake":
            return httpx.Response(200, json={"ok": True})
        if p == "/api/hatch/lease-vm":
            return httpx.Response(200, json={"status": "assigned", "vmId": "vm-abc", "vmName": "vm-abc", "gatewayUrl": "wss://vm-abc.metaaivm.com/"})
        if p == "/api/hatch/token":
            return httpx.Response(200, json={"token": "tok-xyz", "notary_token": "notary-1"})
        return httpx.Response(404, json={})

    cp = MuseControlPlane("c_user=1", transport=httpx.MockTransport(handler))
    chk = await cp.auth_check()
    assert chk == {"ok": True, "viewer_id": "v123"}
    creds = await cp.credentials()
    assert creds["vm_id"] == "vm-abc" and creds["auth_token"] == "tok-xyz" and creds["notary_token"] == "notary-1"


def test_bots_and_groups_with_muse_and_cue(tmp_path):
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

    # 1. /api/bots exposes muse & cue as cloud engines
    r = client.get("/api/bots")
    assert r.status_code == 200
    eng_map = {e["id"]: e for e in r.json()["engines"]}
    assert eng_map["muse"]["cloud"] is True
    assert eng_map["cue"]["cloud"] is True

    # 2. Pure cloud group skips git shell completely
    r = client.post("/api/bots/groups", json={"title": "云端讨论", "members": ["muse", "cue"]})
    assert r.status_code == 200
    g_cloud = r.json()
    assert len(shell_calls) == 0
    assert g_cloud["repo"] == ""
    assert g_cloud["auto_merge"] is False
    assert [m["bot"] for m in g_cloud["members"]] == ["muse", "cue"]
    assert all(m.get("cloud") for m in g_cloud["members"])

    # 3. Mixed group: only codex gets a worktree; then dynamically add muse & cue
    r = client.post("/api/bots/groups", json={"title": "混合群", "repo": "~/workspace/demo", "members": ["codex"]})
    assert r.status_code == 200
    g_mix = r.json()
    assert len(shell_calls) == 1
    assert g_mix["members"][0]["branch"].endswith("/codex")

    r = client.post(f"/api/bots/groups/{g_mix['id']}/members", json={"members": ["muse", "cue"]})
    assert r.status_code == 200
    g_after = r.json()
    assert [m["bot"] for m in g_after["members"]] == ["codex", "muse", "cue"]
    assert len(shell_calls) == 1   # adding cloud members does not run git

    # 4. merge_script ignores cloud members
    ms = groups.merge_script(g_after)
    assert "aiw/" + g_mix["id"] + "/codex" in ms
    assert "/muse" not in ms and "/cue" not in ms

    # 5. Mentions and main chat dispatch support muse & cue
    to, mgr, rest = G.mentions("@muse @cue 对比两个方案", ["codex", "muse", "cue"])
    assert to == ["muse", "cue"] and not mgr and rest == "对比两个方案"
    p = G.parse_dispatch("让 muse 和 cue 一起调研一下最新进展")
    assert p and p["bots"] == ["muse", "cue"]
