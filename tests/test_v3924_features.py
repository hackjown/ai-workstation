"""v3.9.24: auto routing, auto review, webhooks, token usage, apps, prompts, per-model context, teach, remote access,
自动签到. Unit tests first, then the full API stack (mock VM, scripted model) like test_phase7_api."""
import asyncio
import hashlib
import hmac
import json
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import apps as A
from aiw_api import autoreview as AR
from aiw_api import autoroute as RT
from aiw_api import checkin as CK
from aiw_api import webhooks as WH
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.store import Store
from agent_runtime import context as CTX
from agent_runtime import prompts as P
from llm_router import LLMRouter, ScriptedProvider
from llm_router import usage as U
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18791


class _LLM:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def complete(self, system, user, max_tokens=800):
        self.calls.append((system, user))
        return self.text


# ------------------------------------------------------------------ (2) routing
def test_route_rules(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    assert asyncio.run(RT.decide(st, "帮我开发一个爬虫并部署")) is None          # off by default: unchanged
    RT.save(st, {"enabled": True, "tiers": {"hard": {"model": "big-model", "effort": "high"},
                                            "easy": {"model": "tiny", "effort": "off"}},
                 "rules": [{"pattern": "周报", "tier": "hard"}]})
    r = asyncio.run(RT.decide(st, "帮我开发一个网站，重构后端并部署上线，排查报错"))
    assert r["tier"] == "hard" and r["model"] == "big-model" and r["effort"] == "high" and r["by"] == "rule"
    assert asyncio.run(RT.decide(st, "打开百度"))["tier"] == "easy"
    assert asyncio.run(RT.decide(st, "写周报"))["reason"].startswith("命中你的规则")
    man = asyncio.run(RT.decide(st, "打开百度", manual_model="m-x", manual_effort="low"))
    assert man["by"] == "manual" and man["model"] == "m-x"
    with pytest.raises(ValueError):
        RT.save(st, {"rules": [{"pattern": "([", "tier": "hard"}]})


def test_route_small_model_when_unsure(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    RT.save(st, {"enabled": True, "use_model": True})
    llm = _LLM('{"tier":"hard","why":"多步流程"}')
    r = asyncio.run(RT.decide(st, "把这几份文件整理一下然后发给同事看看效果怎么样再说", llm))
    assert r["by"] == "model" and r["tier"] == "hard" and llm.calls
    assert asyncio.run(RT.decide(st, "你好", _LLM("garbage")))["by"] == "rule"
    os.environ["AIW_AUTOROUTE"] = "0"
    try:
        assert asyncio.run(RT.decide(st, "开发")) is None
    finally:
        os.environ.pop("AIW_AUTOROUTE")


def test_effort_reaches_the_request_and_falls_back(monkeypatch):
    from llm_router.providers.openai import OpenAIProvider
    p = OpenAIProvider(model="m", api_key="k", effort="high")
    assert p.thinking == "on" and p._extras() == {"reasoning_effort": "high"}
    q = OpenAIProvider(model="m", api_key="k")
    assert q._extras() == {"thinking": {"type": "disabled"}, "enable_thinking": False}
    r = LLMRouter.from_settings("a", "b", "k", None, None, "off", effort="medium", role="main")
    assert r.role == "main" and r.providers[0].effort == "medium" and r.providers[1].effort == ""


# ------------------------------------------------------------------ (3) review
def test_review_rules_order(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    rv = AR.Reviewer(st, lambda m: _LLM('{"verdict":"allow","reason":"在任务范围内"}'))
    assert asyncio.run(rv.decide("shell", {"command": "rm -rf ~/x"}))["verdict"] == "ask"   # off -> the card
    AR.save(st, {"enabled": True, "allow": "shell:rm -rf ~/workspace/tmp*\nshell:git *", "deny": "*rm -rf /*\nre:mkfs"})
    d = lambda tool, cmd, cat=None: asyncio.run(rv.decide(tool, {"command": cmd}, {"category": cat}, "清理"))
    assert d("shell", "git status")["verdict"] == "allow"
    assert d("shell", "rm -rf ~/workspace/tmp/a")["verdict"] == "allow"
    assert d("shell", "rm -rf /etc")["verdict"] == "deny"
    assert d("shell", "sudo mkfs.ext4 /dev/sda")["verdict"] == "deny"
    assert d("shell", "git push", "pay")["verdict"] == "ask"                 # payments never auto-allowed
    assert d("shell", "curl x.com")["verdict"] == "ask"                      # no rule, no model
    AR.save(st, {"use_model": True})
    v = d("shell", "curl x.com")
    assert v["verdict"] == "allow" and v["by"] == "model"
    assert d("shell", "rm -rf /")["verdict"] == "deny"                       # deny beats the model
    assert len(rv.recent()) >= 8
    assert AR.match("browser_open:*taobao.com*", "browser_open", "browser_open https://www.taobao.com/x")
    assert not AR.match("shell:ls*", "browser_open", "browser_open ls")


# ------------------------------------------------------------------ (4) webhooks
def test_webhook_signature_window_replay(tmp_path):
    st = Store(str(tmp_path / "a.db"))

    class Sec(dict):
        def put(self, k, v): self[k] = v
        def delete(self, k): self.pop(k, None)
    hooks = WH.Webhooks(st, Sec(), None)
    w = hooks.create("ci", "构建结果：{{payload.status}}")
    secret, now = w["secret"], time.time()
    body = b'{"status":"ok"}'
    ts = str(int(now))
    sig = WH.sign(secret, ts, body)
    assert hooks.verify(w["id"], ts, sig, body, now) == (200, "")
    assert hooks.verify(w["id"], ts, sig, body, now)[0] == 409                   # replay
    old = str(int(now - 400))
    assert hooks.verify(w["id"], old, WH.sign(secret, old, body), body, now)[0] == 401
    assert hooks.verify(w["id"], ts, "sha256=00", body, now)[0] == 401
    assert WH.render("x {{payload.status}} {{payload.a.0}}", {"status": "ok", "a": ["z"]}) == "x ok z"
    assert "收到的数据" in WH.render("处理一下", {"k": 1})
    new = hooks.rotate(w["id"])
    assert new != secret


# ------------------------------------------------------------------ (5) usage
def test_usage_sink_and_summary(tmp_path):
    from aiw_api.usage import Usage
    st = Store(str(tmp_path / "a.db"))
    u = Usage(st)
    u.install()
    try:
        tok = U.TASK.set("task-1")
        rtok = U.ROLE.set("main")
        U.record("m1", {"prompt_tokens": 100, "completion_tokens": 20})
        U.record("m2", {"input_tokens": 5, "output_tokens": 1}, role="chat")
        U.record("m2", {})   # nothing
        U.TASK.reset(tok)
        U.ROLE.reset(rtok)
        s = u.summary(7)
        assert s["total"]["total"] == 126 and s["total"]["calls"] == 2
        assert {r["model"] for r in s["per_model"]} == {"m1", "m2"}
        assert {r["role"] for r in s["per_role"]} == {"main", "chat"}
        assert u.task("task-1")["input"] == 105
    finally:
        U.set_sink(None)


async def test_router_tags_role_on_usage(tmp_path):
    rows = []
    U.set_sink(rows.append)

    class P1(ScriptedProvider):
        model = "x"

        async def complete(self, system, user, max_tokens=800):
            U.record("x", {"prompt_tokens": 3, "completion_tokens": 4})
            return "ok"
    try:
        r = LLMRouter([P1([])])
        r.role = "small"
        assert await r.complete("s", "u") == "ok"
        assert rows and rows[0]["role"] == "small"
        assert U.ROLE.get() == ""
    finally:
        U.set_sink(None)


# ------------------------------------------------------------------ (6) apps
def _secrets():
    class Sec(dict):
        def put(self, k, v): self[k] = v
        def get(self, k, d=None): return dict.get(self, k, d)
    return Sec()


def test_app_manifest_validation_and_catalog(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    apps = A.Apps(st, _secrets())
    code = {"aiw_app": 1, "id": "hello", "name": "你好", "kind": "code",
            "code": {"entry": "main.py", "files": {"main.py": "import sys,json;print(json.load(sys.stdin)['n']*2)"},
                     "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}}
    assert apps.save(code)["enabled"] is True
    with pytest.raises(A.AppError):
        A.validate({"aiw_app": 1, "id": "Bad Id", "kind": "code"})
    with pytest.raises(A.AppError):
        A.validate({"aiw_app": 1, "id": "m1", "kind": "mcp", "mcp": {"url": "https://x/mcp", "auth": {"type": "bearer", "token": "sk-1"}}})
    with pytest.raises(A.AppError):
        A.validate({"aiw_app": 1, "id": "c2", "kind": "code", "code": {"files": {"../x": "1"}}})
    m = A.validate({"aiw_app": 1, "id": "gh", "kind": "mcp", "mcp": {"url": "https://x/mcp", "auth": {"type": "api_key", "header": "X-Key"}}})
    assert m["mcp"]["auth"]["secret"] == "app_gh_key" and apps.needed_secrets(m) == ["app_gh_key"]
    apps.save({"aiw_app": 1, "id": "tone", "name": "语气", "kind": "skill", "skill": {"text": "用敬语", "keywords": ["客服"]}})
    assert "用敬语" in apps.prompt_block("帮我回复客服消息") and "用敬语" not in apps.prompt_block("画图")
    assert "app_hello" in apps.prompt_block("x")
    # the agent's own app is a draft
    schema, call = apps.write_tool("t1")
    out = asyncio.run(call({"manifest": {"aiw_app": 1, "id": "agent-made", "kind": "skill", "skill": {"text": "x"}}}))
    assert "草稿" in out and not apps.get("agent-made")["enabled"]
    assert "没有保存" in asyncio.run(call({"manifest": {"id": "x"}}))
    assert apps.export("hello")["code"]["entry"] == "main.py"
    os.environ["AIW_APPS"] = "0"
    try:
        assert apps.active() == []
    finally:
        os.environ.pop("AIW_APPS")


# ------------------------------------------------------------------ (7) prompts
def test_prompt_override_versions_restore(tmp_path):
    from aiw_api import prompts_api as PA
    from agent_runtime.planner import PLAN_PROMPT
    st = Store(str(tmp_path / "a.db"))
    try:
        assert P.get("planner", PLAN_PROMPT) == PLAN_PROMPT
        v1 = PA.add_version(st, "planner", "只输出 JSON 数组 v1")
        assert P.get("planner", PLAN_PROMPT) == "只输出 JSON 数组 v1"
        PA.add_version(st, "planner", "v2")
        assert P.get("planner", PLAN_PROMPT) == "v2"
        assert PA.activate(st, "planner", v1["id"]) and P.get("planner", PLAN_PROMPT) == "只输出 JSON 数组 v1"
        PA.activate(st, "planner", None)
        assert P.get("planner", PLAN_PROMPT) == PLAN_PROMPT
        assert len(PA.detail(st, "planner")["versions"]) == 2
        roles = {r["role"] for r in PA.listing(st)}
        assert {"task_agent", "planner", "chat", "chat_route", "review", "route_classifier", "teach_writer"} <= roles
        PA.add_version(st, "chat", "坏的 {nope}")
        assert P.fmt("chat", "原版 {name}", name="球") == "原版 球"      # bad placeholder -> original
    finally:
        P.set_overrides({})


# ------------------------------------------------------------------ (8) context
def test_context_compression_only_when_configured():
    hist = [f"{i}. shell ls -> " + "x" * 400 for i in range(1, 41)]
    CTX.set_config({})
    assert CTX.fit(hist, "m")[1] is None
    CTX.set_config({"m": {"window": 2000, "trigger_ratio": 0.5, "keep_last": 6}})
    try:
        out, info = CTX.fit(hist, "m-pro", "system")
        assert info and info["kept"] == 6 and out[0].startswith("EARLIER STEPS") and "steps 1-15" in out[0]
        assert out[-1] == hist[-1] and len(out) == 7 and info["after"] < info["before"]
        assert CTX.fit(hist[:3], "m")[1] is None          # small history: unchanged
        assert CTX.fit(hist, "other")[1] is None           # another model: unchanged
        os.environ["AIW_CONTEXT_MANAGE"] = "0"
        assert CTX.fit(hist, "m")[1] is None
    finally:
        os.environ.pop("AIW_CONTEXT_MANAGE", None)
        CTX.set_config({})


# ------------------------------------------------------------------ 签到
def test_checkin_recipe_validation():
    assert CK.validate_recipe({"kind": "browser", "steps": [{"op": "open", "url": "https://a.cn"}, {"op": "click", "text": "签到"}]})
    for bad in ({"kind": "x", "steps": []}, {"kind": "browser", "steps": [{"op": "solve_captcha"}]},
                {"kind": "http", "steps": [{"url": "ftp://x"}]},
                {"kind": "http", "steps": [{"url": "https://x", "json": {"password": "123"}}]}):
        with pytest.raises(ValueError):
            CK.validate_recipe(bad)
    assert CK.HUMAN_RE.search("请拖动滑块完成验证") and CK.HUMAN_RE.search("geetest captcha")


# ------------------------------------------------------------------ full stack
@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    monkeypatch.setenv("AIW_PLAN_GATE", "0")   # scripted runs do not tick plan steps
    backend = MockBackend()
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(backend), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORT}", desktop_http="http://127.0.0.1:1")]
    scripts = []
    app = build_app(s, llm_factory=lambda: LLMRouter([ScriptedProvider(scripts.pop(0) if scripts else [("done", {"summary": "ok"})])]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, backend, scripts, app
    await client.aclose()
    U.set_sink(None)
    stop.set(); await t


async def wait_done(client, sid, timeout=10):
    for _ in range(int(timeout / 0.1)):
        s = (await client.get(f"/api/sessions/{sid}")).json()
        if s["status"] not in ("running",):
            return s
        await asyncio.sleep(0.1)
    raise AssertionError("session did not finish")


async def test_webhook_starts_a_task(stack):
    client, backend, scripts, app = stack
    w = (await client.post("/api/webhooks", json={"name": "部署", "prompt": "部署结果：{{payload.result}}"})).json()
    assert w["secret"].startswith("whsec_")
    lst = (await client.get("/api/webhooks")).json()
    assert lst[0]["has_secret"] and "secret" not in lst[0]
    body = json.dumps({"result": "绿色"}).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(w["secret"].encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await anon.post(f"/api/webhooks/{w['id']}", content=body, headers={"X-AIW-Timestamp": ts, "X-AIW-Signature": sig})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    task = (await client.get(f"/api/tasks/{tid}")).json()
    assert "部署结果：绿色" in task["prompt"] and task["source"] == "webhook"
    r2 = await anon.post(f"/api/webhooks/{w['id']}", content=body, headers={"X-AIW-Timestamp": ts, "X-AIW-Signature": sig})
    assert r2.status_code == 409
    r3 = await anon.post(f"/api/webhooks/{w['id']}", content=body, headers={"X-AIW-Timestamp": ts, "X-AIW-Signature": "sha256=bad"})
    assert r3.status_code == 401
    await client.put(f"/api/webhooks/{w['id']}", json={"enabled": False})
    ts2 = str(int(time.time()) + 1)
    sig2 = "sha256=" + hmac.new(w["secret"].encode(), ts2.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert (await anon.post(f"/api/webhooks/{w['id']}", content=body, headers={"X-AIW-Timestamp": ts2, "X-AIW-Signature": sig2})).status_code == 410
    calls = (await client.get(f"/api/webhooks/{w['id']}/calls")).json()
    assert [c["status"] for c in calls][::-1] == [202, 409, 401, 410]
    await anon.aclose()


async def test_review_rule_skips_the_card_and_deny_blocks(stack):
    client, backend, scripts, app = stack
    await client.put("/api/autoreview", json={"enabled": True, "allow": ["shell:rm -rf ./old*"], "deny": ["*rm -rf ./keep*"]})
    scripts.append([("shell", {"command": "rm -rf ./old"}), ("shell", {"command": "rm -rf ./keep"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "删除旧文件"})).json()
    s = await wait_done(client, r["session_id"])
    assert s["status"] == "succeeded" and not s.get("pending_confirm")
    answers = [e["data"] for e in s["events"] if e["type"] == "agent.confirm_answered"]
    assert [(a["approve"], a["by"]) for a in answers] == [(True, "review"), (False, "review")]
    log = (await client.get("/api/autoreview/log")).json()
    assert {x["verdict"] for x in log} == {"allow", "deny"}
    dry = (await client.post("/api/autoreview/test", json={"tool": "shell", "command": "rm -rf ./keep/a"})).json()
    assert dry["verdict"] == "deny"


async def test_manual_model_and_route_shown(stack):
    client, backend, scripts, app = stack
    scripts.append([("keypress", {"key": "F5"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "打开百度", "model": "my-model", "effort": "low"})).json()
    await wait_done(client, r["session_id"])
    t = (await client.get(f"/api/tasks/{r['task']['id']}")).json()
    assert t["model_pick"] == {"model": "my-model", "effort": "low"}
    # a scripted factory keeps its own model (tests), the API still answers routing settings
    a = (await client.put("/api/autoroute", json={"enabled": True, "tiers": {"hard": {"model": "big", "effort": "high"}}})).json()
    assert a["enabled"] and a["tiers"]["hard"]["model"] == "big"
    tst = (await client.post("/api/autoroute/test", json={"text": "开发一个网站并部署上线，顺便重构"})).json()
    assert tst["route"]["tier"] == "hard" and tst["route"]["model"] == "big"


async def test_prompt_trace_records_what_the_agent_saw(stack):
    client, backend, scripts, app = stack
    scripts.append([("keypress", {"key": "F5"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "打开 Chrome 搜索 OpenAI 然后截图"})).json()
    await wait_done(client, r["session_id"])
    tr = (await client.get(f"/api/prompts/trace/{r['task']['id']}")).json()
    roles = tr["runs"][0]["roles"]
    assert "task_agent" in roles and "done" in roles["task_agent"]["tools"] and roles["task_agent"]["system"]
    lst = (await client.get("/api/prompts")).json()
    assert any(x["role"] == "task_agent" for x in lst)
    d = (await client.post("/api/prompts/task_agent", json={"text": "自定义系统提示"})).json()
    assert d["id"]
    try:
        scripts.append([("keypress", {"key": "F5"}), ("done", {"summary": "ok"})])
        r2 = (await client.post("/api/tasks", json={"prompt": "再搜一次", "new_task": True})).json()
        await wait_done(client, r2["session_id"])
        tr2 = (await client.get(f"/api/prompts/trace/{r2['task']['id']}")).json()
        sysp = tr2["runs"][-1]["roles"]["task_agent"]
        assert sysp["system"].startswith("自定义系统提示") and sysp["overridden"]
    finally:
        await client.post("/api/prompts/task_agent/restore")
    assert not P.active("task_agent")


async def test_apps_api_and_code_app_runs_on_vm(stack):
    client, backend, scripts, app = stack
    m = {"aiw_app": 1, "id": "echo", "name": "回声", "kind": "code",
         "code": {"runtime": "bash", "entry": "run.sh", "files": {"run.sh": "cat input.json"}}}
    assert (await client.post("/api/apps/import", json=m)).json()["id"] == "echo"
    lst = (await client.get("/api/apps")).json()["apps"]
    assert lst[0]["id"] == "echo" and lst[0]["enabled"]
    ex = await client.get("/api/apps/echo/export")
    assert json.loads(ex.text)["id"] == "echo"
    bad = await client.post("/api/apps/import", json={"aiw_app": 1, "id": "x"})
    assert bad.status_code == 400
    ext = app.state.v3924
    comp = app.state.sessions.computer("ai-linux-001")
    schema, call = (await ext.apps.task_tools(comp))["app_echo"]
    out = await call({"q": 1})
    assert '"q": 1' in out
    await client.put("/api/apps/echo/enabled", json={"enabled": False})
    assert "app_echo" not in await ext.apps.task_tools(comp)


async def test_teach_records_takeover_ops(stack):
    client, backend, scripts, app = stack
    rec = (await client.post("/api/teach/start", json={"machine_id": "ai-linux-001", "title": "签到演示"})).json()
    assert rec["status"] == "recording"
    assert (await client.post("/api/teach/start", json={"machine_id": "ai-linux-001"})).status_code == 409
    r = await client.post("/api/machines/ai-linux-001/input", json={"ops": [
        {"t": "move", "x": 10, "y": 10}, {"t": "click", "x": 100, "y": 200}, {"t": "text", "text": "hello"},
        {"t": "key", "key": "Return"}]})
    assert r.status_code == 200
    await client.post(f"/api/teach/{rec['id']}/note", json={"text": "这一步是搜索"})
    await asyncio.sleep(1.2)
    st = (await client.post(f"/api/teach/{rec['id']}/stop")).json()
    assert st["events"] == 3 and st["shots"] >= 1
    d = (await client.get(f"/api/teach/{rec['id']}")).json()
    assert "点击 (100,200)" in d["timeline"] and "•••••" in d["timeline"] and "这一步是搜索" in d["timeline"]
    shot = await client.get(f"/api/teach/{rec['id']}/shot/0")
    assert shot.status_code == 200
    saved = (await client.post(f"/api/teach/{rec['id']}/save", json={"name": "demo-checkin", "text": "---\nname: demo\n---\n步骤"})).json()
    assert saved["app"]["id"] == "demo-checkin"
    assert (await client.get("/api/apps/demo-checkin")).json()["manifest"]["kind"] == "skill"


async def test_remote_access_page(stack, monkeypatch):
    client, backend, scripts, app = stack
    r = (await client.get("/api/remote")).json()
    assert r["auth_ok"] and r["port"] and "tunnel" in r and r["tunnel"]["running"] is False
    r = (await client.put("/api/remote", json={"custom_url": "https://aiw.example.com/"})).json()
    assert r["custom_url"] == "https://aiw.example.com"
    assert (await client.put("/api/remote", json={"custom_url": "aiw.example.com"})).status_code == 400
    monkeypatch.setenv("AIW_CLOUDFLARED", "/nonexistent")
    monkeypatch.setenv("PATH", "/nonexistent")
    st = await client.post("/api/remote/tunnel/start")
    assert st.status_code == 502 and "cloudflared" in st.text
    monkeypatch.setenv("AIW_REMOTE_TUNNEL", "0")
    assert (await client.post("/api/remote/tunnel/start")).status_code == 403


async def test_checkin_first_run_records_then_replay_and_summary_push(stack):
    client, backend, scripts, app = stack
    sites = (await client.get("/api/checkin/sites")).json()
    assert sites["sites"] == []                                   # nothing preconfigured
    s = (await client.post("/api/checkin/sites", json={"name": "示例站", "url": "https://example.com/sign"})).json()
    assert s["schedule"] == "daily 08:30" and s["jitter_min"] == 15 and s["next_run"] > time.time()
    ck = app.state.checkin
    sent = []
    orig = app.state.notifier.notify

    async def spy(kind, title, body="", **kw):
        sent.append((kind, title, body))
        return await orig(kind, title, body, **kw)
    app.state.notifier.notify = spy
    recipe = {"kind": "http", "steps": [{"method": "GET", "url": "https://example.invalid/checkin",
                                         "expect": {"contains": "成功"}}]}
    scripts.append([("checkin_save_recipe", {"recipe": recipe, "result": "ok", "message": "签到成功 +5 分"}),
                    ("done", {"summary": "签到成功"})])
    res = (await client.post(f"/api/checkin/sites/{s['id']}/run")).json()
    assert res["pending"]
    runs = (await client.get(f"/api/checkin/runs?site_id={s['id']}")).json()
    tid = runs[0]["task_id"]
    for _ in range(100):
        t = (await client.get(f"/api/tasks/{tid}")).json()
        if t["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    site = (await client.get("/api/checkin/sites")).json()["sites"][0]
    assert site["has_recipe"] and site["recipe"]["kind"] == "http" and site["last_status"] == "ok"
    assert any(k == "checkin" and "1/1" in t for k, t, _ in sent)
    # replay: the HTTP step fails (no network) -> escalates to an agent task
    sent.clear()
    scripts.append([("done", {"summary": "今天已签"})])
    res = (await client.post(f"/api/checkin/sites/{s['id']}/run")).json()
    runs = (await client.get(f"/api/checkin/runs?site_id={s['id']}")).json()
    assert runs[1]["mode"] == "replay" and runs[1]["status"] == "replay_failed" and runs[0]["mode"] == "agent_escalate"
    rr = (await client.post(f"/api/checkin/sites/{s['id']}/rerecord")).json()
    assert not rr["has_recipe"]


async def test_checkin_replay_ok_without_model(stack, monkeypatch):
    client, backend, scripts, app = stack
    s = (await client.post("/api/checkin/sites", json={"name": "B", "url": "https://b.example",
                                                         "recipe": {"kind": "http", "steps": [{"url": "https://b.example/api"}]}})).json()
    ck = app.state.checkin

    async def fake_http(site, recipe):
        return True, "already", "今天已经签过"
    monkeypatch.setattr(ck, "_replay_http", fake_http)
    sent = []

    async def spy(kind, title, body="", **kw):
        sent.append((kind, title, body))
        return {}
    app.state.notifier.notify = spy
    # the scheduler tick: due now -> one batch, one push
    ck.store.update("checkin_sites", s["id"], next_run=time.time() - 1)
    batch = await ck.tick()
    assert batch
    await asyncio.sleep(0.2)
    assert sent == [("checkin", "自动签到 1/1 成功", "☑️ B：今天已经签过")]
    nxt = ck.site(s["id"])["next_run"]
    assert nxt > time.time()
    assert await ck.tick() is None                                # not due again


async def test_usage_endpoint_and_model_context_api(stack):
    client, backend, scripts, app = stack
    U.TASK.set("t-x")
    U.record("gpt-x", {"prompt_tokens": 10, "completion_tokens": 2}, role="main")
    s = (await client.get("/api/usage?days=3")).json()
    assert s["total"]["total"] == 12 and s["per_day"][0]["models"][0]["model"] == "gpt-x"
    r = (await client.put("/api/model_context", json={"models": {"deepseek": {"window": 64000, "trigger_ratio": 0.6}}})).json()
    assert r["models"]["deepseek"]["window"] == 64000
    assert CTX.config_for("deepseek-v4-pro")["trigger_ratio"] == 0.6
    assert (await client.put("/api/model_context", json={"models": {"x": {"keep_last": 5}}})).status_code == 400
    await client.put("/api/model_context", json={"models": {}})
    assert CTX.config_for("deepseek-v4-pro") is None


# ------------------------------------------------------------------ web + iOS sources
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_web_pages_render():
    js = ROOT / "apps/web/v3924.js"
    assert subprocess.run(["node", "--check", str(js)], capture_output=True).returncode == 0
    r = subprocess.run(["node", str(ROOT / "tests/js/v3924_pages_test.js"), str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok 10" in r.stdout, r.stderr
    assert '<script src="v3924.js"></script>' in (ROOT / "apps/web/index.html").read_text(encoding="utf-8")


def test_ios_usage_settings_wired():
    src = (ROOT / "apps/ios/Sources/V3924Views.swift").read_text(encoding="utf-8")
    assert "/api/usage?days=" in src and "/api/checkin/sites" in src
    st = (ROOT / "apps/ios/Sources/SettingsView.swift").read_text(encoding="utf-8")
    assert "UsageView()" in st and "CheckinView()" in st
