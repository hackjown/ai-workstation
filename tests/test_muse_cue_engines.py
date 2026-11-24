"""Tests for Meta Muse CLI engine (`muse exec --json`) and hardened Cue by Manus cloud engine."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "agent-runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))

from agent_runtime import cli_bridge as CB  # noqa: E402
from agent_runtime import cli_engines as CE  # noqa: E402
from aiw_api import bot_groups as G  # noqa: E402
from aiw_api import bots as B  # noqa: E402
from aiw_api.browser_cookies import decode_jwt_claims, extract_cue_credentials_from_cookies  # noqa: E402
from aiw_api.cloud_auth import CLOUD_BOTS, CloudAuthStore  # noqa: E402
from aiw_api.crypto import SecretBox  # noqa: E402
from aiw_api.cue_engine import (  # noqa: E402
    CueEngine,
    build_cue_socket_url,
    encode_socketio_event,
    parse_socketio_packet,
)
from aiw_api.store import Store  # noqa: E402


def _make_jwt(payload: dict) -> str:
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{hdr}.{body}.sig"


# --------------------------------------------------------------------------------------------- Muse CLI tests
def test_muse_cli_cmd_steps_session_no_yolo_and_jsonl():
    assert "muse" in CE.ENGINES
    assert "muse" in CB.ENGINES
    assert "~/.muse/**" in CB.DENY_PATHS

    argv, stdin = CB.build_cmd(
        {"engine": "muse", "bin": "/usr/local/bin/muse", "workdir": "/tmp/demo", "model": "muse-pro", "effort": "high"},
        "修一下登录 bug",
        "sess-42",
        "/tmp/turn1",
    )
    assert stdin is None
    assert argv[:3] == ["/usr/local/bin/muse", "exec", "--json"]
    assert "--yolo" not in argv
    assert "--session-id" in argv and argv[argv.index("--session-id") + 1] == "sess-42"
    assert "--max-model-steps" in argv and argv[argv.index("--max-model-steps") + 1] == "100"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "muse-pro"
    assert argv[-1].endswith("修一下登录 bug")   # v3.9.78: AIW rules lead the prompt

    # Simulate `muse exec --json` JSONL output
    events = []
    t = CB.Turn("muse", events.append)
    sample_lines = [
        json.dumps({"type": "session.started", "session_id": "muse-s-99"}),
        json.dumps({"type": "tool_call", "id": "tc1", "name": "Bash", "arguments": {"command": "pytest -q"}}),
        json.dumps({"type": "tool_result", "tool_call_id": "tc1", "output": "5 passed"}),
        json.dumps({"type": "message", "text": "已修复登录逻辑并通过测试"}),
        json.dumps({"type": "final", "result": "登录 bug 已修复，pytest 全部通过。"}),
    ]
    for ln in sample_lines:
        t.line(ln)
    assert t.session == "muse-s-99"
    assert t.got_result is True
    assert t.failed is False
    assert t.tools == 1
    assert t.response() == "登录 bug 已修复，pytest 全部通过。"


def test_muse_meta_api_key_injected_from_secretbox(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    secrets = SecretBox(store, str(tmp_path))
    secrets.put("META_API_KEY", "meta-secret-xyz")
    bots = B.Bots(store, secrets=secrets)
    job = bots.engines["muse"].job({"session": "s1", "workdir": "~/workspace/proj", "effort": "medium"}, "hello")
    assert job["engine"] == "muse"
    assert "env" not in job   # v3.9.78: the key goes to the computer through setenv, never in the job
    assert bots.engines["muse"]._env_fn() == {"META_API_KEY": "meta-secret-xyz"}
    assert job["effort"] == "medium"


# --------------------------------------------------------------------------------------------- Cue security & UX tests
def test_cue_cookie_extractor_and_cloud_auth_store(tmp_path):
    assert CLOUD_BOTS == ("cue",)
    jwt_tok = _make_jwt({"email": "alice@cue.im", "name": "Alice", "exp": 1900000000})
    cookies = [
        {"name": "cue_session_id", "value": jwt_tok, "domain": ".cue.im", "path": "/"},
        {"name": "unrelated", "value": "x", "domain": ".example.com", "path": "/"},
    ]
    cue = extract_cue_credentials_from_cookies(cookies)
    assert cue["access_token"] == jwt_tok
    assert cue["handle"] == "alice@cue.im"
    assert "unrelated" not in cue["cookie_header"]
    assert decode_jwt_claims(jwt_tok)["expires_at_ms"] == 1900000000 * 1000

    store = Store(str(tmp_path / "aiw.db"))
    auth = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    # Saving cookie without experimental_native keeps native mode disabled
    auth.save("cue", {"access_token": jwt_tok, "cookie_header": "cue_session_id=abc"})
    assert auth.public_view("cue")["configured"] is False
    assert auth.public_view("cue")["experimental_native"] is False

    # Saving API Key enables official OpenAPI mode
    auth.save("cue", {"api_key": "sk-manus-secret-12345678"})
    pub = auth.public_view("cue")
    assert pub["configured"] is True
    assert pub["mode"] == "openapi"
    assert "12345678" not in pub["api_key_preview"]


@pytest.mark.asyncio
async def test_cue_no_auto_browser_cookie_and_experimental_gate(tmp_path):
    store = Store(str(tmp_path / "aiw.db"))
    auth_store = CloudAuthStore(store, SecretBox(store, str(tmp_path)))
    eng = CueEngine(auth_store, poll_backoff=(0.01,))

    # Unconfigured Cue must fail cleanly without auto-reading browser cookies
    res = await eng.start("cloud", {"model": "", "session": ""}, "hello")
    assert res["ok"] is False
    assert "API Key" in res["error"]

    # Cookie present without experimental_native=True is still refused
    auth_store.save("cue", {"access_token": "tok", "cookie_header": "cue_session_id=tok"})
    assert eng._effective_mode(auth_store.load("cue")) == ""
    auth_store.save("cue", {"experimental_native": True, "mode": "native"})
    assert eng._effective_mode(auth_store.load("cue")) == "native"


@pytest.mark.asyncio
async def test_cue_openapi_backoff_429_eviction_and_error_reason(tmp_path):
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
    assert st["ok"] is True
    jid = st["job"]
    # Wait until done (first wait may wake on 429 tool event)
    res = {}
    for _ in range(5):
        res = await eng.wait("cloud", jid, 0, seconds=5)
        if res["status"] != "running":
            break
    assert res["status"] == "turn_done"
    assert res["response"] == "done after 429"
    # Completed job is evicted from memory (no leak)
    assert jid not in eng._jobs

    # Test detailed error_message reason + waiting confirmation card status
    def err_handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/task.sendMessage"):
            return httpx.Response(200, json={"ok": True, "task_id": "t-err"})
        if p.endswith("/task.listMessages"):
            if request.url.params.get("order") == "desc":
                return httpx.Response(200, json={"ok": True, "messages": []})
            return httpx.Response(200, json={"ok": True, "messages": [
                {"id": "u1", "type": "user_message", "user_message": {"content": "fail me"}},
                {"id": "e1", "type": "error_message", "error_message": {"content": "Insufficient credits on workspace"}},
            ]})
        return httpx.Response(404, json={"ok": False})

    eng_err = CueEngine(auth_store, transport=httpx.MockTransport(err_handler), poll_backoff=(0.01,))
    st2 = await eng_err.start("cloud", {"model": "", "session": ""}, "fail me")
    res2 = await eng_err.wait("cloud", st2["job"], 0, seconds=5)
    assert res2["status"] == "turn_failed"
    assert "Insufficient credits on workspace" in res2["error"]

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
    assert res3["status"] == "blocked"
    assert res3["waiting_confirm"] is True
    assert res3["denied"][0]["tool"] == "cue"


def test_cue_socketio_helpers():
    url = build_cue_socket_url("https://api.manus.im", "dev-123")
    assert url.startswith("wss://api.manus.im/socket.io/?")
    assert parse_socketio_packet("2")["kind"] == "ping"
    ev = parse_socketio_packet(encode_socketio_event("message", {"type": "event", "x": 1}))
    assert ev["kind"] == "event" and ev["payload"]["x"] == 1


# --------------------------------------------------------------------------------------------- Bots & Groups integration
def test_bots_and_groups_muse_local_and_cue_cloud(tmp_path):
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

    # 1. muse is local CLI; cue is cloud
    eng_map = {e["id"]: e for e in client.get("/api/bots").json()["engines"]}
    assert not eng_map["muse"].get("cloud")
    assert eng_map["cue"]["cloud"] is True

    # 2. Pure cloud group (cue only) skips git shell
    r = client.post("/api/bots/groups", json={"title": "云端调研", "members": ["cue"]})
    assert r.status_code == 200
    assert len(shell_calls) == 0
    assert r.json()["repo"] == ""

    # 3. Mixed group: codex + muse both get local git worktrees & branches; cue is cloud
    r = client.post("/api/bots/groups", json={"title": "开发群", "repo": "~/workspace/demo", "members": ["codex", "muse"]})
    assert r.status_code == 200
    g_dev = r.json()
    assert len(shell_calls) == 1
    branches = {m["bot"]: m["branch"] for m in g_dev["members"]}
    assert branches["codex"].endswith("/codex")
    assert branches["muse"].endswith("/muse")

    # Dynamically add cue into existing dev group (no extra git worktree)
    r = client.post(f"/api/bots/groups/{g_dev['id']}/members", json={"members": ["cue"]})
    assert r.status_code == 200
    g_after = r.json()
    assert [m["bot"] for m in g_after["members"]] == ["codex", "muse", "cue"]
    assert len(shell_calls) == 1

    # merge_script merges codex & muse, skips cue
    ms = groups.merge_script(g_after)
    assert f"aiw/{g_dev['id']}/codex" in ms
    assert f"aiw/{g_dev['id']}/muse" in ms
    assert "/cue" not in ms


# ---------------------------------------------------------------- v3.9.78: Muse secrets and permissions
def test_v3978_muse_key_never_in_job_or_start_command():
    import asyncio as _a
    from aiw_api.bots import CliEngine
    from agent_runtime import cli_engines as ce

    cmds = []

    async def sh(mid, cmd, to):
        cmds.append(cmd)
        if "CLI_BRIDGE_OK" in cmd:
            return "CLI_BRIDGE_OK"
        return 'CLIRESULT {"ok": true, "job": "muse-1"}'

    eng = CliEngine("muse", sh, set(), env_fn=lambda: {"META_API_KEY": "sk-secret-123"})
    job = eng.job({"bot": "muse"}, "hi")
    assert "env" not in job
    _a.run(eng.start("m1", {"bot": "muse"}, "hi"))
    _a.run(eng.start("m1", {"bot": "muse"}, "again"))
    import base64 as _b
    setenv = [c for c in cmds if " setenv " in c]
    starts = [c for c in cmds if " start " in c]
    assert len(setenv) == 1, "secrets are written once, not every start"
    assert len(starts) == 2
    for c in starts:
        payload = c.split("--job-b64 ", 1)[1].strip()
        assert "sk-secret-123" not in _b.b64decode(payload).decode()


def test_v3978_bridge_env_file_is_private_and_job_json_clean(tmp_path, monkeypatch):
    from agent_runtime import cli_bridge as cb
    monkeypatch.setattr(cb, "ROOT", str(tmp_path))
    monkeypatch.setattr(cb, "ENV_DIR", str(tmp_path / "env"))
    cb.save_engine_env("muse", {"META_API_KEY": "k1", "EMPTY": ""})
    import os as _o, stat as _s
    path = cb.env_path("muse")
    assert _s.S_IMODE(_o.stat(path).st_mode) == 0o600
    assert cb.load_engine_env("muse") == {"META_API_KEY": "k1"}
    assert cb.run_env(cb.load_engine_env("muse"))["META_API_KEY"] == "k1"


def test_v3978_muse_cmd_uses_deny_and_approval_flags_when_present(monkeypatch):
    from agent_runtime import cli_bridge as cb
    monkeypatch.setattr(cb, "muse_flags", lambda b: {"deny": "deny", "approval": "--disable-approval"})
    argv, stdin = cb.build_cmd({"engine": "muse", "bin": "muse", "workdir": "/w", "effort": "high"}, "做事", "", "/tmp")
    assert "--yolo" not in argv
    assert "--disable-approval" in argv
    assert "--deny" in argv and any("git push" in x for x in argv)
    assert argv[-1].startswith("[AIW 规则]") and argv[-1].endswith("做事")
    assert argv[argv.index("--max-model-steps") + 1] == "100"


def test_v3978_muse_cmd_without_flags_still_has_rules(monkeypatch):
    from agent_runtime import cli_bridge as cb
    monkeypatch.setattr(cb, "muse_flags", lambda b: {"deny": "", "approval": ""})
    argv, _ = cb.build_cmd({"engine": "muse", "bin": "muse", "workdir": "/w"}, "x", "s1", "/tmp")
    assert argv[:3] == ["muse", "exec", "--json"]
    assert "--deny" not in argv and "--disable-approval" not in argv
    assert argv[argv.index("--session-id") + 1] == "s1"
    assert "git push" in argv[-1]
