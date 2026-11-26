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
    normalize_muse_fs_path,
    parse_muse_media,
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


@pytest.mark.asyncio
async def test_muse_media_parse_and_save_media(tmp_path, monkeypatch):
    monkeypatch.setenv("AIW_ARTIFACTS", str(tmp_path / "art"))
    assert normalize_muse_fs_path("https://muse.ai/fs/raw/workspace/out/demo.mp4?x=1") == "workspace/out/demo.mp4"
    assert normalize_muse_fs_path("/home/muse/workspace/img/pic.png") == "workspace/img/pic.png"
    assert normalize_muse_fs_path("../etc/passwd") == ""

    payload = {
        "transcript": {
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "image", "path": "/home/muse/workspace/out/poster.png", "mime_type": "image/png"},
                    {"type": "video", "file_ref": {"path": "workspace/out/clip.mp4"}, "filename": "clip.mp4"},
                    {"type": "image", "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n1234").decode(), "mime_type": "image/png", "filename": "inline.png"},
                ],
            }]
        }
    }
    media = parse_muse_media(payload)
    assert len(media) == 3
    assert media[0]["fs_path"] == "workspace/out/poster.png" and media[0]["type"] == "image"
    assert media[1]["fs_path"] == "workspace/out/clip.mp4" and media[1]["type"] == "video"
    assert media[2]["data_uri"].startswith("data:image/png;base64,")

    # Simulate Noise /fs/raw downloaded bytes and persist via Bots.save_media
    media[0]["data_bytes"] = b"\x89PNG\r\n\x1a\nposter"
    media[1]["data_bytes"] = b"\x00\x00\x00\x18ftypmp42"
    store = Store(str(tmp_path / "aiw.db"))
    bots = B.Bots(store, secrets=SecretBox(store, str(tmp_path)))
    saved = await bots.save_media("bc-muse-1", media)
    assert len(saved) == 3
    assert saved[0]["kind"] == "image" and saved[0]["size"] == len(b"\x89PNG\r\n\x1a\nposter")
    assert saved[1]["kind"] == "video" and saved[1]["size"] == len(b"\x00\x00\x00\x18ftypmp42")
    assert saved[2]["kind"] == "image" and saved[2]["size"] == len(b"\x89PNG\r\n\x1a\n1234")

    # Verify delta.presentation / image_options widgets are also parsed
    pres = {"widgets": [{"kind": "image_options", "data": {"images": [{"path": "workspace/gen/cat.png", "image_id": "img1"}]}}]}
    m_pres = parse_muse_media(pres)
    assert len(m_pres) == 1 and m_pres[0]["fs_path"] == "workspace/gen/cat.png" and m_pres[0]["type"] == "image"


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

    # Test asynchronous follow-up sync after a turn ends (e.g. Cue background subtask report)
    bots = B.Bots(store, secrets=SecretBox(store, str(tmp_path)))
    sync_calls = {"n": 0}

    def sync_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/task.listMessages"):
            sync_calls["n"] += 1
            if request.url.params.get("start_event_id") == "m3":
                return httpx.Response(200, json={"ok": True, "messages": [
                    {"id": "m3", "type": "status_update", "status_update": {"agent_status": "stopped"}},
                    {"id": "m4", "type": "assistant_message", "assistant_message": {"content": "5个新候选已测完，找到2个可用节点"}},
                ]})
            return httpx.Response(200, json={"ok": True, "messages": []})
        return httpx.Response(404, json={"ok": False})

    cue_eng = CueEngine(bots.auth_store, transport=httpx.MockTransport(sync_handler))
    bots.engines["cue"] = cue_eng
    conv = bots.create("cue", "cue测试")
    bots.set_conv(conv["id"], session="agent-default-main_task")
    cue_eng._save_sync(conv["id"], "m3", ["m1", "m2", "m3"])

    added1 = await bots.sync_cloud(conv["id"], min_interval_s=0)
    assert added1 == 1
    msgs = bots.messages(conv["id"])
    assert any("5个新候选已测完" in m["text"] for m in msgs)
    # Second sync with same watermark does not duplicate
    added2 = await bots.sync_cloud(conv["id"], min_interval_s=0)
    assert added2 == 0


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


@pytest.mark.asyncio
async def test_cue_sync_messages_async_media(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    secrets = SecretBox(store, str(tmp_path))
    bots = B.Bots(store, secrets=secrets)
    bots.artifacts_dir = str(tmp_path / "artifacts")
    bots.auth_store.save("cue", {"api_key": "sk-manus-test", "mode": "openapi", "source": "manual"})

    def handler(req: httpx.Request) -> httpx.Response:
        assert "task.listMessages" in str(req.url)
        return httpx.Response(200, json={"ok": True, "messages": [
            {"id": "ev-10", "type": "assistant_message", "assistant_message": {
                "content": "两张海滩漫步图都好了。",
                "attachments": [
                    {"type": "image", "filename": "beach1.png", "url": "https://cdn.example/beach1.png",
                     "content_type": "image/png", "file_uid": "f1"},
                ],
            }},
            {"id": "ev-11", "type": "assistant_message", "assistant_message": {
                "content": "",
                "attachments": [
                    {"type": "video", "filename": "clip.mp4", "url": "https://cdn.example/clip.mp4",
                     "content_type": "video/mp4", "file_uid": "f2"},
                ],
            }},
        ]})

    cue = bots.engines["cue"]
    cue.transport = httpx.MockTransport(handler)
    conv = bots.create("cue", "Cue 图片测试")
    bots.set_conv(conv["id"], session="task-media-1")
    cue._save_sync(conv["id"], "ev-09", ["ev-09"])

    saved_calls = []

    async def fake_save_media(cid, items):
        saved_calls.append((cid, items))
        return [{"name": it["filename"], "kind": B.media_kind(it["filename"], it["content_type"], it["type"]),
                 "path": f"/tmp/{it['filename']}", "size": 123, "content_type": it["content_type"]}
                for it in items]

    bots.save_media = fake_save_media
    added = await bots.sync_cloud(conv["id"], min_interval_s=0)
    assert added == 1
    assert len(saved_calls) == 1 and len(saved_calls[0][1]) == 2
    msgs = bots.messages(conv["id"])
    assert len(msgs) == 1
    media = msgs[0]["data"].get("media") or []
    assert [m["kind"] for m in media] == ["image", "video"]
    assert [m["name"] for m in media] == ["beach1.png", "clip.mp4"]


def test_dm_unread_badge_lifecycle(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    secrets = SecretBox(store, str(tmp_path))
    bots = B.Bots(store, secrets=secrets)

    app = FastAPI()
    B.mount(app, bots, lambda: None)
    client = TestClient(app)

    dm = client.post("/api/bots/dm/cue").json()
    cid = dm["id"]

    # Initially 0 unread
    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert eng_map["cue"]["unread"] == 0

    # User sends a message -> still 0 unread
    bots.add(cid, "user", "帮我生成图片")
    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert eng_map["cue"]["unread"] == 0

    # Running placeholder does not count as unread yet
    running_msg = bots.add(cid, "bot", "", {"status": "running"})
    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert eng_map["cue"]["unread"] == 0

    # When running message completes + another async reply arrives -> 2 unread
    bots.update(running_msg["id"], "好，我来生成", {"status": "done"})
    bots.add(cid, "bot", "两张图都好了", {"status": "done", "proactive": True})
    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert eng_map["cue"]["unread"] == 2

    # Opening the DM marks it read -> 0 unread
    res = client.get(f"/api/bots/convs/{cid}")
    assert res.status_code == 200 and res.json()["conv"]["unread"] == 0
    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert eng_map["cue"]["unread"] == 0


@pytest.mark.asyncio
async def test_local_media_extraction_and_manager_self_find_media(tmp_path):
    # 1. vm_file_path normalizes /home/<user>/workspace/... and extract_local_media_paths finds workspace images
    assert B.vm_file_path("/home/aiw/workspace/groups/bg-1/design.png", "~/workspace/groups/bg-1") == "groups/bg-1/design.png"
    assert B.vm_file_path("file:///home/aiw/workspace/groups/bg-1/shot.webp", "~/workspace/groups/bg-1") == "groups/bg-1/shot.webp"
    paths = B.extract_local_media_paths(
        "设计图已保存到 `/home/aiw/workspace/groups/bg-1/agy.png` 和 ![预览](shots/cbc.webp)",
        {"files": ["design/home.png", "src/app.ts"]},
    )
    assert paths == ["design/home.png", "/home/aiw/workspace/groups/bg-1/agy.png", "shots/cbc.webp"]

    # 2. Group member live status & @管家 self-execution without delegating to engines
    store = Store(str(tmp_path / "aiw.db"))
    store.upsert_machine({"id": "ai-linux-001", "provider": "local", "provider_vm_id": "v1",
                          "name": "Local PC", "agent_endpoint": "ws://localhost:8001"})
    secrets = SecretBox(store, str(tmp_path))
    shell_cmds = []

    async def fake_shell(mid: str, cmd: str, timeout: int) -> str:
        shell_cmds.append(cmd)
        if "python3 -c" in cmd:
            return (
                "0|1700000200|/home/aiw/workspace/.aiw-worktrees/bg/agy/design/agy-ui.png\n"
                "1|1700000100|/home/aiw/workspace/.aiw-worktrees/bg/codebuddy/design/cbc-ui.png\n"
            )
        return "DIRTY 0\nGROUP_OK main\n"

    bots = B.Bots(store, shell=fake_shell, secrets=secrets)
    groups = G.Groups(bots)
    g = await groups.create("home-inventory", "~/workspace/demo", ["agy", "codebuddy", "cue"], "main")
    gid = g["id"]

    # Simulate cue failed/disconnected -> member status shows "error"
    cue_m = groups.member(g, "cue")
    bots.add(cue_m["conv"], "bot", "连接超时", {"status": "failed"})
    g_st = groups.group(gid)
    by_bot = {m["bot"]: m for m in g_st["members"]}
    assert by_bot["cue"]["state"] == "error" and "出错" in by_bot["cue"]["state_label"]
    assert by_bot["agy"]["state"] == "ok"

    # Mock fetch_file and track bots.send calls to verify 管家 NEVER delegates to agy/codebuddy
    delegated = []
    orig_send = bots.send
    bots.send = lambda *a, **kw: (delegated.append((a, kw)), orig_send(*a, **kw))[1]

    async def fake_fetch(cid, raw, out_dir):
        name = raw.rsplit("/", 1)[-1]
        return {"path": str(tmp_path / name), "name": name, "size": 456}

    bots.fetch_file = fake_fetch

    assert G.is_manager_self_task("@管家 把agy和cbc设计的截图找到，发到群里，不要让别人做，你自己来做", True) is True
    await groups.send(gid, "@管家 把agy和cbc设计的截图找到，发到群里，不要让别人做，你自己来做")
    # Wait for background _manager_do task to finish
    import asyncio
    await asyncio.gather(*list(groups._tasks))

    assert delegated == []   # 管家 did it itself, zero delegation to agy/codebuddy!
    gmsgs = groups.messages(gid)
    aiw_msgs = [m for m in gmsgs if m["role"] == "aiw"]
    assert len(aiw_msgs) == 1
    media = aiw_msgs[0]["data"].get("media") or []
    assert [m["name"] for m in media] == ["agy-ui.png", "cbc-ui.png"]
    assert [m["kind"] for m in media] == ["image", "image"]


@pytest.mark.asyncio
async def test_cue_waiting_with_reply_is_turn_done_and_group_assign_passes_history(tmp_path):
    # 1. Cue returning assistant_message + agent_status=waiting is a normal turn_done (NOT blocked/denied)
    store = Store(str(tmp_path / "aiw.db"))
    auth_store = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    auth_store.save("cue", {"api_key": "sk-manus-test", "mode": "openapi", "source": "manual"})

    def waiting_reply_handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/task.sendMessage"):
            return httpx.Response(200, json={"ok": True, "task_id": "t-wreply"})
        if p.endswith("/task.listMessages"):
            if req.url.params.get("order") == "desc":
                return httpx.Response(200, json={"ok": True, "messages": []})
            return httpx.Response(200, json={"ok": True, "messages": [
                {"id": "u1", "type": "user_message", "user_message": {"content": "看下设计"}},
                {"id": "a1", "type": "assistant_message", "assistant_message": {"content": "设计我看完了，建议选方案A。你觉得呢？"}},
                {"id": "w1", "type": "status_update", "status_update": {"agent_status": "waiting"}},
            ]})
        return httpx.Response(404, json={"ok": False})

    eng = CueEngine(auth_store, transport=httpx.MockTransport(waiting_reply_handler), poll_backoff=(0.01,))
    st = await eng.start("cloud", {"model": "", "session": ""}, "看下设计")
    res = await eng.wait("cloud", st["job"], 0, seconds=5)
    assert res["status"] == "turn_done"
    assert "方案A" in res["response"]
    assert not res.get("denied") and not res.get("waiting_confirm")

    # 2. Group @cue direct assign receives recent chat history + posted image labels & attachment
    img_file = tmp_path / "agy-design.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\n")
    bots = B.Bots(store, secrets=SecretBox(store, str(tmp_path)))
    groups = G.Groups(bots)
    g = await groups.create("home-inventory", "", ["cue", "muse"])
    gid = g["id"]
    groups.add(gid, "bot", "agy", "这是首页桌面端设计稿", {"media": [{"name": "agy-design.png", "path": str(img_file), "kind": "image", "label": "Antigravity · 首页"}]})

    sent_calls = []
    bots.send = lambda cid, text, role="user", **kw: (sent_calls.append((cid, text, kw)), {"id": "m1"})[1]
    await groups.send(gid, "@cue 看看群里聊天记录和截图，点评一下")
    assert len(sent_calls) == 1
    _cid, prompt_sent, kw = sent_calls[0]
    assert "这是首页桌面端设计稿" in prompt_sent
    assert "Antigravity · 首页" in prompt_sent
    atts = kw.get("attachments") or []
    assert any(a.get("artifact") == str(img_file) for a in atts)


@pytest.mark.asyncio
async def test_group_multi_owner_steps_cloud_reroute_no_merge_and_unread(tmp_path):
    # 1. parse_steps expands multiple @mentions on a single step line
    steps = G.parse_steps(
        "第 1 步 @agy @cbc @cue @muse 各自设计前端\n第 2 步 @管家 汇总对比并向我提问",
        ["agy", "codebuddy", "cue", "muse"],
    )
    assert [s["bot"] for s in steps] == ["agy", "codebuddy", "cue", "muse", G.MANAGER_ID]

    # 2. _reroute_cloud_local_plan prevents assigning local-path verification to cloud engines (muse/cue)
    bad_plan = {
        "steps": [
            {"bot": "agy", "task": "在 ~/workspace/demo 开发前端", "step": "开发"},
            {"bot": "muse", "task": "验收 ~/workspace/demo 里的本地代码和文件", "step": "验收"},
            {"bot": "cue", "task": "讨论配色方案和体验建议", "step": "讨论"},
        ],
        "assign": {},
        "report": "计划",
    }
    fixed = G._reroute_cloud_local_plan(bad_plan, ["agy", "codebuddy", "muse", "cue"])
    assert fixed["steps"][0]["bot"] == "agy"
    assert fixed["steps"][1]["bot"] == "codebuddy"   # rerouted from muse to local engine (avoiding agy)
    assert fixed["steps"][2]["bot"] == "cue"         # pure discussion stays on cloud engine

    # 3. Cloud-only reply in a mixed group does NOT trigger auto_merge, and group unread count works
    store = Store(str(tmp_path / "aiw.db"))
    store.upsert_machine({"id": "ai-linux-001", "provider": "local", "provider_vm_id": "v1",
                          "name": "Local PC", "agent_endpoint": "ws://localhost:8001"})
    secrets = SecretBox(store, str(tmp_path))

    async def fake_shell(mid: str, cmd: str, timeout: int) -> str:
        return "DIRTY 0\nGROUP_OK main\n"

    bots = B.Bots(store, shell=fake_shell, secrets=secrets)
    groups = G.Groups(bots)
    app = FastAPI()
    G.mount(app, groups, lambda: None)
    client = TestClient(app)

    g = await groups.create("mixed-grp", "~/workspace/demo", ["agy", "cue"], "main", auto_merge=True)
    gid = g["id"]
    merged_called = []
    groups.merge = lambda _gid: (merged_called.append(_gid), {"ok": True})[1]

    # Simulate user @cue and cue replying
    groups.add(gid, "user", "我", "@cue 看看记录")
    groups.set_group(gid, status="working", pending=["cue"])
    cue_conv = groups.member(g, "cue")["conv"]
    cue_msg = bots.add(cue_conv, "bot", "我看完了", {"status": "done"})
    groups._on_bot("message", cue_msg)
    assert merged_called == []
    assert groups.group(gid)["status"] == "idle"
    assert groups.group(gid)["unread"] == 1

    # Opening group clears unread to 0
    r = client.get(f"/api/bots/groups/{gid}")
    assert r.status_code == 200 and r.json()["group"]["unread"] == 0
    assert groups.group(gid)["unread"] == 0


def test_cue_prompt_trim_under_5000_tokens():
    from aiw_api.cue_engine import _estimate_tokens, _trim_for_cue

    huge = "头部角色说明\n" + ("这是一段很长的群聊历史和仓库文件内容。" * 800) + "\n最终任务：请设计首页界面。"
    assert _estimate_tokens(huge) > 5000
    trimmed = _trim_for_cue(huge, max_tokens=4200)
    assert _estimate_tokens(trimmed) <= 4500
    assert "头部角色说明" in trimmed and "请设计首页界面" in trimmed


@pytest.mark.asyncio
async def test_group_option_cards_with_images_and_choose(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    secrets = SecretBox(store, str(tmp_path))
    bots = B.Bots(store, secrets=secrets)
    groups = G.Groups(bots)
    app = FastAPI()
    G.mount(app, groups, lambda: None)
    client = TestClient(app)

    g = await groups.create("ui-pick", "", ["muse", "cue"])
    gid = g["id"]
    groups.add(gid, "bot", "muse", "极简浅色方案，卡片清晰", {"status": "done", "media": [{"name": "muse.png", "path": "/tmp/muse.png", "kind": "image"}]})
    groups.add(gid, "bot", "cue", "暗黑仪表盘方案，信息密度高", {"status": "done", "media": [{"name": "cue.png", "path": "/tmp/cue.png", "kind": "image"}]})

    await groups._final(gid, "汇总对比以上方案并做成卡片供我选择")
    msgs = groups.messages(gid)
    card_msg = next((m for m in msgs if (m.get("data") or {}).get("cards")), None)
    assert card_msg is not None
    cards = card_msg["data"]["cards"]
    assert len(cards) == 2
    assert [c["id"] for c in cards] == ["A", "B"]
    assert cards[0]["media"][0]["name"] == "muse.png"
    assert cards[1]["media"][0]["name"] == "cue.png"

    # Choose card B via HTTP endpoint
    r = client.post(f"/api/bots/groups/{gid}/choose", json={"msg_id": card_msg["id"], "card_id": "B"})
    assert r.status_code == 200
    updated_msgs = groups.messages(gid)
    picked_msg = next(m for m in updated_msgs if m["id"] == card_msg["id"])
    assert picked_msg["data"].get("picked") == "B"
    assert any("方案 B" in m["text"] for m in updated_msgs if m["role"] == "aiw")


@pytest.mark.asyncio
async def test_group_remove_member_and_routine_checkin_engine_dispatch(tmp_path):
    from aiw_api.checkin import Checkin
    from aiw_api.scheduler import Scheduler

    store = Store(str(tmp_path / "aiw.db"))
    secrets = SecretBox(store, str(tmp_path))
    bots = B.Bots(store, secrets=secrets)
    groups = G.Groups(bots)

    app = FastAPI()
    G.mount(app, groups, lambda: None)
    client = TestClient(app)

    # 1. Remove member from group (and reject removing the last member)
    g = await groups.create("rm-test", "", ["muse", "cue"])
    gid = g["id"]
    r = client.delete(f"/api/bots/groups/{gid}/members/cue")
    assert r.status_code == 200
    assert [m["bot"] for m in r.json()["members"]] == ["muse"]
    r2 = client.delete(f"/api/bots/groups/{gid}/members/muse")
    assert r2.status_code == 400

    # 2. Routine with bot="cue" dispatches to Bots instead of sessions
    sent = []
    bots.send = lambda cid, text, role="user", **kw: (sent.append((cid, text)), {"id": "bm-1"})[1]

    class DummySessions:
        listeners = []
        async def submit(self, *a, **kw):
            raise AssertionError("should dispatch to Bots, not sessions")

    sched = Scheduler(store, DummySessions(), None)
    sched.bots = bots
    routine = store.insert("routines", {
        "name": "每日调研", "prompt": "总结今日 AI 新闻", "schedule": "daily 09:00",
        "bot": "cue", "enabled": 1, "next_run": 0,
    })
    res = await sched.fire_routine(routine, advance=False)
    assert res["bot"] == "cue" and res["conv_id"].startswith("bc-")
    assert len(sent) == 1 and "AI 新闻" in sent[0][1]

    # 3. Checkin with bot="agy" dispatches to Bots and on_bot_event records completion
    ck = Checkin(store, DummySessions(), None, secrets)
    ck.bots = bots
    site = ck.add({"name": "测试论坛", "url": "https://example.com/checkin", "bot": "agy"})
    res_ck = await ck._agent(site, "cb-1", "record")
    assert res_ck["status"] == "pending" and res_ck["bot"] == "agy"
    cid = res_ck["conv_id"]
    ck.on_bot_event("message", {"conv": cid, "role": "bot", "text": "今天已经签过啦", "data": {"status": "done"}})
    updated_site = ck.site(site["id"])
    assert updated_site["last_status"] == "already"


@pytest.mark.asyncio
async def test_no_space_mentions_manager_tools_and_chat_ssh(tmp_path):
    # 1. No-space mentions (@agy完成任务了吗？ / @管家看看仓库)
    to, mgr, rest = G.mentions("@agy完成任务了吗？", ["agy", "codebuddy", "cue"])
    assert to == ["agy"] and mgr is False and rest == "完成任务了吗？"
    to2, mgr2, rest2 = G.mentions("@管家看看仓库", ["agy", "codebuddy"])
    assert to2 == [] and mgr2 is True and rest2 == "看看仓库"

    # 2. Manager agent loop executes local_shell + read_local_file and blocks dangerous commands
    store = Store(str(tmp_path / "aiw.db"))
    store.upsert_machine({"id": "ai-linux-001", "provider": "local", "provider_vm_id": "v1",
                          "name": "Local PC", "agent_endpoint": "ws://localhost:8001"})
    secrets = SecretBox(store, str(tmp_path))
    sh_calls = []

    async def fake_shell(mid: str, cmd: str, timeout: int) -> str:
        sh_calls.append(cmd)
        if "git remote -v" in cmd:
            return "origin\thttps://github.com/hackjown/home-inventory.git (push)\n"
        return "DIRTY 0\nGROUP_OK main\n"

    class FakeToolLLM:
        def __init__(self):
            self.turn = 0
        async def chat(self, messages, schemas, max_tokens=2500):
            self.turn += 1
            if self.turn == 1:
                names = {s["name"] for s in (schemas or [])}
                assert {"local_shell", "read_local_file", "ssh_exec"}.issubset(names)
                return {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "local_shell", "args": {"command": "git remote -v"}},
                        {"id": "c2", "name": "local_shell", "args": {"command": "git push --force origin main"}},
                    ],
                }
            # Second turn sees tool outputs
            tool_msgs = [m["content"] for m in messages if m.get("role") == "tool"]
            assert any("home-inventory.git" in t for t in tool_msgs)
            assert any("已拦截高危命令" in t for t in tool_msgs)
            return {"content": "已核实远端仓库为 home-inventory.git。", "tool_calls": []}

    fake_llm = FakeToolLLM()
    bots = B.Bots(store, llm=lambda: fake_llm, shell=fake_shell, secrets=secrets)
    groups = G.Groups(bots)
    g = await groups.create("tool-grp", "~/workspace/demo", ["agy"], "main")
    out = await groups._manager_run(g, "sys", "检查远端仓库")
    assert "home-inventory.git" in out

    # 3. Main chat exposes ssh_exec tool directly
    from aiw_api.chat import Chat
    from aiw_api.config import Settings

    class _DummySess:
        listeners = []

    chat = Chat(store, _DummySess(), None, Settings(), secrets)
    ctools = await chat.tools()
    assert "ssh_exec" in ctools


@pytest.mark.asyncio
async def test_group_messages_pagination(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    bots = B.Bots(store, secrets=SecretBox(store, str(tmp_path)))
    groups = G.Groups(bots)
    g = await groups.create("page-grp", "", ["muse"])
    gid = g["id"]
    for i in range(1, 6):
        groups.add(gid, "user", "user", f"msg-{i}")

    # Latest 2 messages (page 1)
    p1, has1 = groups.messages_page(gid, limit=2)
    assert [m["text"] for m in p1] == ["msg-4", "msg-5"]
    assert has1 is True

    # Older 2 messages before p1[0] (page 2)
    p2, has2 = groups.messages_page(gid, before=p1[0]["seq"], limit=2)
    assert [m["text"] for m in p2] == ["msg-2", "msg-3"]
    assert has2 is True

    # Remaining older messages before p2[0] (page 3, includes system creation msg + msg-1)
    p3, has3 = groups.messages_page(gid, before=p2[0]["seq"], limit=5)
    assert p3[-1]["text"] == "msg-1"
    assert has3 is False

    # Incremental after query
    inc, has_inc = groups.messages_page(gid, after=p2[-1]["seq"], limit=10)
    assert [m["text"] for m in inc] == ["msg-4", "msg-5"]
    assert has_inc is False
