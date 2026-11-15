"""v3.9.49 DeepSeek 联网工具 (aiw_api.dsweb): routing (web_lite), key lookup order, login / captcha wall detection,
usage counting and billing rules, doc_ask refusing private files and cleaning up after itself, the settings API, the
chat's read_url through Fetch, and a lite task that never takes a screenshot until it opens a browser."""
import asyncio
import json

import httpx
import pytest

from aiw_api import dsweb as ds
from aiw_api import providers as pv

KEY_DS, KEY_OTHER, GW = "sk-ds-provider-key", "sk-other-provider", "gw-key"
H = {"Authorization": "Bearer tok"}

LONG = "DeepSeek 发布了新的搜索接口。" * 40


class FakeDeepSeek:
    """api.deepseek.com behind an httpx.MockTransport: records every request."""

    def __init__(self):
        self.calls = []
        self.search_status = [200]
        self.fetch_page = {"title": "文章", "content": "# 文章\n\n" + LONG}
        self.deleted = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = req.content
        self.calls.append((req.method, req.url.path, req.headers.get("authorization"), body))
        p = req.url.path
        if p == "/v1/tools/search":
            st = self.search_status.pop(0) if len(self.search_status) > 1 else self.search_status[0]
            if st != 200:
                return httpx.Response(st, json={"error": {"message": "slow down", "type": "x", "param": None, "code": "x"}})
            q = json.loads(body)["query"]
            if q == "nothing":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={"results": [
                {"rerank_score": 2.1, "title": "DeepSeek Search API", "url": "https://api-docs.deepseek.com/guides/search",
                 "snippet": "s", "summary": "Search API 返回结构化结果", "content": "", "site_name": "DeepSeek",
                 "site_icon": None, "date": "2026-10-17T00:00:00+00:00", "authority": 3}]})
        if p == "/v1/tools/fetch":
            u = json.loads(body)["url"]
            if "missing" in u:
                return httpx.Response(404, json={"error": {"message": "no content", "type": "x", "param": None, "code": "x"}})
            if "login" in u:
                return httpx.Response(200, json={"title": "京东-欢迎登录", "content": "请登录 账号登录 扫码登录 忘记密码"})
            return httpx.Response(200, json=self.fetch_page)
        if p == "/files" and req.method == "POST":
            return httpx.Response(200, json={"id": "file-api-123", "object": "file", "bytes": 10, "created_at": 1,
                                             "filename": "a.pdf", "purpose": "text_recognition", "expires_at": 3601})
        if p == "/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "合同第 3 条写的是 30 天付款。"}}],
                                             "usage": {"prompt_tokens": 1200, "completion_tokens": 40}})
        if p.startswith("/files/") and req.method == "DELETE":
            self.deleted.append(p.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": "file-api-123", "object": "file", "deleted": True})
        return httpx.Response(404, json={"error": {"message": "?"}})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("AIW_DSWEB_RETRY_S", "0")
    yield
    ds.TRANSPORT = None
    pv.TRANSPORT = None


@pytest.fixture
def fake():
    f = FakeDeepSeek()
    ds.TRANSPORT = httpx.MockTransport(f.handler)
    return f


@pytest.fixture
def app(tmp_path):
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    s.scheduler_enabled = False
    a = build_app(s)
    a.state.secrets.put("openai_api_key", GW)
    return a


def add_provider(app, pid, base, key):
    lst = pv.stored(app.state.store)
    lst.append({"id": pid, "name": pid, "kind": "openai", "base_url": base, "proxy": "", "enabled": True})
    app.state.store.kv_set(pv.KV, lst)
    app.state.secrets.put(pv.secret_name(pid), key)


# --------------------------------------------------------------------------------------------- routing
@pytest.mark.parametrize("prompt,lite", [
    ("帮我查一下今天 AI 领域的新闻并总结", True),
    ("总结一下 https://example.com/a 这篇文章", True),
    ("对比 iPhone 17 和 Pixel 10 的参数", True),
    ("research the latest RISC-V news", True),
    ("登录京东帮我下单", False),
    ("打开浏览器搜索 DeepSeek", False),
    ("帮我在小红书搜一下露营攻略", False),
    ("截图看一下桌面", False),
    ("画个 CAD 户型图", False),
    ("用PS修一下这张图", False),
    ("把这个视频剪一下", False),
    ("【技能：cad-drawing】查一下尺寸", False),
    ("你好", False),
    ("打开淘宝下单\n\n【接着上次继续做】用户补充：再查一下这款的评测并总结", True),
])
def test_web_lite_routing(prompt, lite):
    from agent_runtime.loop import web_lite
    assert web_lite(prompt) is lite


def test_web_tools_never_take_a_screenshot():
    from agent_runtime.loop import _NO_SCREEN_TOOLS, _NON_GUI_TOOLS
    for t in ("web_search", "web_fetch", "doc_ask"):
        assert t in _NO_SCREEN_TOOLS and t in _NON_GUI_TOOLS
    assert "browser_open" not in _NO_SCREEN_TOOLS and "browser_open" not in _NON_GUI_TOOLS


# --------------------------------------------------------------------------------------------- key lookup
def test_key_lookup_order(app, monkeypatch):
    st, sec = app.state.store, app.state.secrets
    assert ds.resolve_key(st, sec) is None                                    # default is an OpenAI gateway: never sent
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert ds.resolve_key(st, sec)["source"] == "env"
    m = dict(st.kv_get("models", {}) or {})
    m["base_url"] = "https://api.deepseek.com"
    st.kv_set("models", m)
    k = ds.resolve_key(st, sec)
    assert (k["source"], k["key"]) == ("default", GW)                         # default beats the env
    add_provider(app, "ds", "https://api.deepseek.com/v1", KEY_DS)
    k = ds.resolve_key(st, sec)
    assert (k["source"], k["provider"], k["key"]) == ("deepseek_provider", "ds", KEY_DS)
    add_provider(app, "relay", "https://relay.example.com/v1", KEY_OTHER)
    st.kv_set(ds.KV, {"provider": "relay"})
    k = ds.resolve_key(st, sec)
    assert (k["source"], k["provider"], k["key"]) == ("picked", "relay", KEY_OTHER)   # the user's pick wins
    lst = pv.stored(st)
    lst[-1]["enabled"] = False
    st.kv_set(pv.KV, lst)
    assert ds.resolve_key(st, sec)["provider"] == "ds"                       # a disabled pick falls through
    st.kv_set(ds.KV, {"backend": "vm"})
    assert ds.enabled(st, sec) is False and ds.tools(st, sec) == {}          # backend vm switches everything off


# --------------------------------------------------------------------------------------------- walls
@pytest.mark.parametrize("title,content,why", [
    ("京东-欢迎登录", "请登录 账号登录 扫码登录", "登录页"),
    ("Just a moment...", "Checking your browser before accessing", "验证/风控页"),
    ("安全验证", "拖动下方滑块完成拼图", "验证/风控页"),
    ("Sign in", "Please sign in to continue to your account", "登录页"),
    ("短", "只有一句话", "正文太短"),
    ("文章", LONG, ""),
    ("长文章", "页面底部：登录 / 注册\n" + LONG * 4, ""),          # a real article with a login link in the footer
])
def test_wall_reason(title, content, why):
    r = ds.wall_reason(title, content)
    assert (r.startswith(why) if why else r == "")


# --------------------------------------------------------------------------------------------- calls + usage
async def test_search_fetch_and_usage_counting(app, fake):
    st, sec = app.state.store, app.state.secrets
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    res = await ds.search(st, sec, "x" * 300, count=99)
    body = json.loads(fake.calls[-1][3])
    assert len(body["query"]) == 128 and body["count"] == 50 and fake.calls[-1][2] == f"Bearer {KEY_DS}"
    assert res[0]["url"].startswith("https://api-docs.deepseek.com")
    assert await ds.search(st, sec, "nothing") == []                           # empty: not billed
    page = await ds.fetch(st, sec, "https://example.com/a")
    assert page["blocked"] == "" and page["title"] == "文章"
    page = await ds.fetch(st, sec, "https://example.com/login")
    assert page["blocked"] == "登录页"
    with pytest.raises(ds.DSError) as e:
        await ds.fetch(st, sec, "https://example.com/missing")
    assert e.value.status == 404
    with pytest.raises(ds.DSError):
        await ds.fetch(st, sec, "https://user:pw@example.com/")             # never sent
    u = ds.usage_summary(st)["today"]
    assert (u["search"], u["fetch"], u["failed"]) == (2, 3, 1)
    assert abs(u["cost"] - 3 * 0.00075) < 1e-9                               # 1 search + 2 fetches with content


async def test_429_is_retried_once_402_is_not(app, fake):
    st, sec = app.state.store, app.state.secrets
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    fake.search_status = [429, 200]
    assert len(await ds.search(st, sec, "q")) == 1
    n = len(fake.calls)
    fake.search_status = [402]
    with pytest.raises(ds.DSError) as e:
        await ds.search(st, sec, "q")
    assert "余额不足" in e.value.message and len(fake.calls) == n + 1


async def test_fetch_tool_says_failed_and_points_to_the_browser(app, fake):
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    t = ds.tools(app.state.store, app.state.secrets, origin="task")
    assert set(t) == {"web_search", "web_fetch", "doc_ask"}
    out = await t["web_fetch"][1]({"url": "https://passport.jd.com/login"})
    assert out.startswith("FAILED") and "browser_open" in out
    out = await t["web_fetch"][1]({"url": "https://example.com/missing"})
    assert out.startswith("FAILED") and "browser_open" in out
    out = await t["web_search"][1]({"query": "DeepSeek"})
    assert "https://api-docs.deepseek.com/guides/search" in out and "2026-10-17" in out


# --------------------------------------------------------------------------------------------- doc_ask
@pytest.mark.parametrize("name", ["IMG_2031.HEIC", "photo.jpg", "scan.zip", "客厅扫描.zip", "room-model.usdz", "plan.dwg",
                                  "身份证正反面.pdf", "passport_scan.pdf", "weights.safetensors", "a.exe"])
def test_doc_ask_refuses_private_files(name):
    assert ds.doc_refusal(name) != ""


async def test_doc_ask_refused_file_is_never_uploaded(app, fake, tmp_path):
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    p = tmp_path / "身份证.pdf"
    p.write_bytes(b"%PDF-1.4")
    t = ds.tools(app.state.store, app.state.secrets, resolve_path=lambda x: str(p))
    out = await t["doc_ask"][1]({"path": "身份证.pdf", "question": "号码是多少"})
    assert out.startswith("REFUSED") and fake.calls == []


async def test_doc_ask_uploads_with_expiry_asks_and_deletes(app, fake, tmp_path):
    st, sec = app.state.store, app.state.secrets
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    p = tmp_path / "合同.docx"
    p.write_bytes(b"PK\x03\x04 fake docx")
    r = await ds.doc_ask(st, sec, str(p), "付款期限？")
    assert r["answer"].startswith("合同第 3 条") and r["file_deleted"] and fake.deleted == ["file-api-123"]
    up = next(c for c in fake.calls if c[1] == "/files")
    raw = up[3].decode("latin-1")
    assert raw.index('name="purpose"') < raw.index('name="file"')            # purpose before file
    assert "text_recognition" in raw and 'name="expires_after[seconds]"' in raw and "3600" in raw
    chat = json.loads(next(c for c in fake.calls if c[1] == "/chat/completions")[3])
    assert chat["model"] == "deepseek-flash" and chat["messages"][1]["content"][0] == {"type": "file", "file_id": "file-api-123"}
    u = ds.usage_summary(st)["today"]
    assert u["doc"] == 1 and u["cost"] > 0


# --------------------------------------------------------------------------------------------- API
def test_settings_api_and_test_button(app, fake):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/api/web-tools", headers=H).json()
    assert r["backend"] == "deepseek" and r["active"] is False and r["key"] is None
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    r = c.get("/api/web-tools", headers=H).json()
    assert r["active"] and r["key"]["source"] == "deepseek_provider" and KEY_DS not in json.dumps(r)
    assert c.put("/api/web-tools", headers=H, json={"backend": "nope"}).status_code == 400
    assert c.put("/api/web-tools", headers=H, json={"provider": "ghost"}).status_code == 404
    r = c.put("/api/web-tools", headers=H, json={"doc_ask": False, "provider": "ds"}).json()
    assert r["doc_ask"] is False and r["provider"] == "ds"
    assert "doc_ask" not in ds.tools(app.state.store, app.state.secrets)
    t = c.post("/api/web-tools/test", headers=H, json={}).json()
    assert t["ok"] and t["search"]["count"] == 1 and t["fetch"]["chars"] > 200
    assert c.get("/api/web-tools").status_code == 401


def test_cli_search_and_fetch(fake, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-cli")
    monkeypatch.setenv("AIW_DB", "/nonexistent/aiw.db")
    assert ds.main(["search", "DeepSeek", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["title"] == "DeepSeek Search API" and fake.calls[-1][2] == "Bearer sk-cli"
    assert ds.main(["fetch", "https://example.com/login"]) == 3
    assert "FAILED" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------- chat
async def test_chat_gets_web_search_doc_ask_and_read_url_uses_fetch(app, fake):
    add_provider(app, "ds", "https://api.deepseek.com", KEY_DS)
    chat = app.state.chat
    chat.tool_loader = None
    tools = await chat.tools()
    assert "web_search" in tools and "doc_ask" in tools and chat.tool_status("web_search") == "正在搜索…"
    msgs = chat.build_messages("今天新闻")
    assert "web_search" in msgs[0]["content"] and "doc_ask" in msgs[0]["content"]
    out = await chat._read_url({"url": "https://example.com/a"})
    assert out.startswith("[200 DeepSeek Fetch]") and fake.calls[-1][1] == "/v1/tools/fetch"
    out = await chat._read_url({"url": "https://example.com/login"})
    assert "读不到正文" in out and "交给电脑" in out


# --------------------------------------------------------------------------------------------- loop
async def test_lite_task_runs_without_screenshots_until_browser_open(tmp_path, monkeypatch):
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import EventType, Task, TaskStatus
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor

    shots = []

    class Comp:   # no desktop at all: any screenshot is recorded
        async def execute(self, action, **kw):
            from aiw_protocols import ActionResult
            if action == "computer.screenshot":
                shots.append(1)
            return ActionResult(success=True, screenshot=None, output="")

    calls = []

    async def web_search(a):
        calls.append(("web_search", a))
        return "1. 新闻 https://example.com/n"

    async def web_fetch(a):
        calls.append(("web_fetch", a))
        return "FAILED: 登录页。改用 browser_open"

    extra = {"web_search": ({"name": "web_search", "description": "s", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}, web_search),
             "web_fetch": ({"name": "web_fetch", "description": "f", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}, web_fetch)}
    events = []

    async def sink(e):
        events.append(e)
    llm = ScriptedProvider([("web_search", {"query": "AI 新闻"}), ("web_fetch", {"url": "https://example.com/n"}),
                            ("done", {"summary": "今天的 AI 新闻：……"})])
    comp = Comp()
    loop = AgentLoop(Task(prompt="帮我查一下今天的 AI 新闻并总结"), "s9", comp, LLMRouter([llm]), ToolExecutor(comp),
                     emit=sink, extra_tools=extra,
                     config=LoopConfig(max_steps=10, timeout_s=20, artifacts_dir=str(tmp_path / "art")))
    assert loop._web_lite and loop._desktop_free
    t = await loop.run()
    assert t.status == TaskStatus.SUCCEEDED, t.result
    assert [c[0] for c in calls] == ["web_search", "web_fetch"]
    assert shots == [] and all(r.screenshot_b64 is None for r in llm.requests)
    # the same prompt without the web tools is a normal (desktop) task
    loop2 = AgentLoop(Task(prompt="帮我查一下今天的 AI 新闻并总结"), "s10", comp, LLMRouter([ScriptedProvider([])]),
                      ToolExecutor(comp), emit=sink, config=LoopConfig(max_steps=1, timeout_s=5, artifacts_dir=str(tmp_path / "a2")))
    assert loop2._web_lite is False


# --------------------------------------------------------------------------------------------- compare script
def test_web_compare_report(app, tmp_path, monkeypatch, capsys):
    import importlib.util
    import time as _t
    st = app.state.store
    now = _t.time()
    for i, (p, status) in enumerate([("帮我查一下今天的 AI 新闻并总结", "succeeded"), ("登录京东下单", "succeeded"),
                                     ("总结 https://example.com/a", "failed")]):
        st._x("INSERT INTO tasks (id, prompt, machine_id, status, result, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
              (f"task-{i}", p, "ai-linux-001", status, "", now - 100 + i, now - 40 + i))
        st._x("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (f"s{i}", "ai-linux-001", f"task-{i}", "done", now - 100, now - 40))
        st._x("INSERT INTO agent_events VALUES (?,?,?,?,?,?)", (f"e{i}", f"s{i}", "agent.action", 1,
                                                               json.dumps({"tool": "browser_open"}), now))
        st._x("INSERT INTO machine_actions VALUES (?,?,?,?,?,?,?,?)", (f"m{i}", f"s{i}", "computer.screenshot", "{}", "", "",
                                                                       "ok", now))
    spec = importlib.util.spec_from_file_location("web_compare", "scripts/web_compare.py")
    wc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wc)
    monkeypatch.setenv("AIW_DB", st.path)
    monkeypatch.setattr("sys.argv", ["web_compare.py", "--n", "10"])
    assert wc.main() == 0
    out = capsys.readouterr().out
    assert "帮我查一下今天的 AI 新闻" in out and "总结 https://example.com/a" in out and "登录京东" not in out
    assert "| 旧 task-0 | succeeded | 1.0 | 1 | 1 | 1 |" in out


def test_settings_card_is_on_the_grouped_settings_page():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    ui = (root / "apps/web/ui.js").read_text()
    assert '["g-web", "联网"]' in ui and 'id="g-web"' in ui and "loadWebTools()" in ui
    assert 'getElementById("g-web")' in (root / "apps/web/app.js").read_text()


# --------------------------------------------------------------------------------------------- third-party gateway
def test_picked_third_party_gateway_gets_its_own_address_and_key(app, tmp_path):
    """A picked relay is called at its own base_url; its key never reaches api.deepseek.com."""
    seen = []

    def relay(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.host, req.url.path, req.headers.get("authorization"), req.content))
        p = req.url.path
        if p == "/v1/tools/search":
            return httpx.Response(200, json={"results": [{"title": "t", "url": "https://a.example/x", "summary": "s"}]})
        if p == "/v1/tools/fetch":
            return httpx.Response(200, json={"title": "文章", "content": LONG})
        if p == "/v1/files" and req.method == "POST":
            return httpx.Response(200, json={"id": "f1"})
        if p == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})
        if p == "/v1/files/f1":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"error": {"message": "?"}})

    ds.TRANSPORT = httpx.MockTransport(relay)
    st, sec = app.state.store, app.state.secrets
    add_provider(app, "relay", "http://relay.example.com:8787/v1/", KEY_OTHER)
    st.kv_set(ds.KV, {"provider": "relay", "doc_model": "workbuddy/cn:deepseek-v4.1"})
    k = ds.resolve_key(st, sec)
    assert k["third_party"] and k["base"] == "http://relay.example.com:8787/v1"
    asyncio.run(ds.search(st, sec, "q"))
    asyncio.run(ds.fetch(st, sec, "https://a.example/x"))
    doc = tmp_path / "合同.txt"
    doc.write_text("付款 30 天")
    asyncio.run(ds.doc_ask(st, sec, str(doc), "几天付款"))
    assert {h for h, *_ in seen} == {"relay.example.com"}
    assert all(a == f"Bearer {KEY_OTHER}" for _, _, a, _ in seen)
    assert [p for _, p, _, _ in seen] == ["/v1/tools/search", "/v1/tools/fetch", "/v1/files", "/v1/chat/completions",
                                          "/v1/files/f1"]
    chat = json.loads(next(b for _, p, _, b in seen if p == "/v1/chat/completions"))
    assert chat["model"] == "workbuddy/cn:deepseek-v4.1"
    pub = ds.public(st, sec)
    assert pub["key"]["third_party"] and pub["key"]["host"] == "relay.example.com"
    assert {p["id"]: p["official"] for p in pub["providers"]}["relay"] is False


def test_third_party_without_the_tools_says_so(app):
    ds.TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(404, text="404 page not found"))
    add_provider(app, "relay", "http://relay.example.com/v1", KEY_OTHER)
    app.state.store.kv_set(ds.KV, {"provider": "relay"})
    with pytest.raises(ds.DSError) as e:
        asyncio.run(ds.search(app.state.store, app.state.secrets, "q"))
    assert "不支持 /tools/search" in e.value.message and "第三方「relay」" in e.value.message
    ds.TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"}))
    with pytest.raises(ds.DSError) as e:
        asyncio.run(ds.search(app.state.store, app.state.secrets, "q"))
    assert "不是 JSON" in e.value.message


def test_doc_model_setting_is_validated(app):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    assert c.put("/api/web-tools", headers=H, json={"doc_model": "a b"}).status_code == 400
    assert c.put("/api/web-tools", headers=H, json={"doc_model": "deepseek-v4.1"}).json()["doc_model"] == "deepseek-v4.1"
