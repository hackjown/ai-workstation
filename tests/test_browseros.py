"""BrowserOS neo (bos_* tools): mapping onto its MCP tools, the safety gates judging a ref by its element text,
the executor call, the loop's secrets/trim/hiding, the per-computer switch, and the vm-agent proxy against a mock MCP."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from tool_router import browseros as bos
from tool_router import Decision, RiskLevel, ToolExecutor
from tool_router.executor import ToolOutcome
from tool_router.policy import PolicyEngine


# ----------------------------------------------------------------------------- fakes
class Res:
    def __init__(self, success=True, result=None, error=None):
        self.success, self.result, self.error, self.screenshot, self.duration_ms = success, result or {}, error, None, 5


SNAP = """- heading "确认订单"
- button "提交订单" [ref=e5]
- button "立即购买" [ref=e6]
- textbox "说点什么..." [ref=e7]
- button "发送" [ref=e8]
- button "赞 128" [ref=e9]
- link "关注" [ref=e10]"""


class Comp:
    """A computer whose BrowserOS answers grep from SNAP and records every call."""
    machine_id = "m1"

    def __init__(self):
        self.calls = []

    async def execute(self, action, payload=None, observe=False, timeout=30):
        self.calls.append((action, payload, observe, timeout))
        if action == "browseros.call" and payload["tool"] == "grep":
            import re
            pat = re.compile(payload["args"]["pattern"])
            return Res(result={"ok": True, "text": "\n".join(l for l in SNAP.splitlines() if pat.search(l))})
        if action == "browseros.call":
            return Res(result={"ok": True, "is_error": False, "text": "clicked; diff: +1 -0"})
        if action == "shell.exec":   # page probes (shop / xhs): an ordinary item page with no warning
            return Res(result={"stdout": json.dumps({"url": "https://item.jd.com/1.html", "title": "x", "alerts": [],
                                                     "text": "x"})})
        return Res()


class Ex:
    """Stands in for ToolExecutor under a guard: records what reaches it."""
    def __init__(self, comp):
        self.computer, self.scale, self.confirm, self.bos_key, self.ran = comp, 1.0, None, "task-1", []

    async def run(self, name, args, intent=""):
        self.ran.append((name, args))
        return ToolOutcome(name, "browseros.call", True, Decision(RiskLevel.L1, "ok"))


# ----------------------------------------------------------------------------- mapping
def test_mcp_mapping_strips_internal_args_and_sets_defaults():
    assert bos.mcp_call("bos_snapshot", {"page": 3}) == ("snapshot", {"page": 3, "mode": "interactive"})
    t, a = bos.mcp_call("bos_act", {"page": 1, "kind": "click", "ref": "e5", "_labels": {"e5": "x"}, "_label": "x"})
    assert t == "act" and a == {"page": 1, "kind": "click", "ref": "e5", "diff": 3000.0}
    assert bos.mcp_call("bos_act", {"page": 1, "kind": "click", "ref": "e5", "diff": "500"})[1]["diff"] == 500.0
    assert bos.mcp_call("bos_tabs", {"action": "front", "page": 2}) == ("front", {"page": 2})
    assert bos.mcp_call("bos_navigate", {"page": 1, "url": "https://a.cn"})[1]["action"] == "url"
    assert bos.mcp_call("bos_upload", {"page": 1, "ref": "e3", "files": "~/a.jpg"})[1]["files"] == ["~/a.jpg"]
    assert bos.mcp_call("bos_grep", {"page": 1, "pattern": "购买"})[1]["over"] == "ax"


def test_labels_and_classic_translation():
    names = bos.parse_labels(SNAP)
    assert names["e5"] == "提交订单" and names["e9"] == "赞 128"
    assert bos.parse_labels(SNAP, with_role=True)["e5"] == "button 提交订单"
    a = {"page": 1, "kind": "click", "ref": "e5", "_labels": {"e5": "提交订单"}}
    assert bos.classic("bos_act", a) == [("browser_click", {"text": "提交订单"})]
    assert bos.classic("bos_act", {"kind": "fill", "ref": "e7", "value": "hi", "_labels": {"e7": "说点什么..."}}) == \
        [("browser_fill", {"field": "说点什么...", "text": "hi"})]
    assert bos.classic("bos_act", {"kind": "press", "key": "Control+Enter"}) == [("keypress", {"key": "ctrl+enter"})]
    assert bos.classic("bos_act", {"kind": "type", "text": "x"}) == [("type", {"text": "x"})]
    assert bos.classic("bos_navigate", {"page": 1, "url": "https://x.cn"}) == [("browser_navigate", {"url": "https://x.cn"})]
    assert bos.classic("bos_snapshot", {"page": 1}) == []
    assert bos.unresolved({"kind": "click", "ref": "e99", "_labels": {}}) == ["e99"]


# ----------------------------------------------------------------------------- shop order gate
async def test_shop_gate_refuses_submit_coordinates_stale_refs_and_dialogs():
    from aiw_api import shopwatch as sw
    comp = Comp()
    ex = Ex(comp)
    st = {"approved": False, "submitted": False}
    sw.guard(ex, st, comp)
    out = await ex.run("bos_act", {"page": 1, "kind": "click", "ref": "e5"})
    assert out.decision.forbidden and "order_confirm" in out.error and not ex.ran
    out = await ex.run("bos_act", {"page": 1, "kind": "click_at", "x": 900, "y": 700})
    assert out.decision.forbidden and "坐标" in out.error
    out = await ex.run("bos_act", {"page": 1, "kind": "click", "ref": "e404"})
    assert out.decision.forbidden and "e404" in out.error
    out = await ex.run("bos_act", {"page": 1, "kind": "dialog_accept"})
    assert out.decision.forbidden
    assert (await ex.run("shell", {"command": "curl -s http://127.0.0.1:9210/mcp"})).decision.forbidden
    assert not ex.ran
    out = await ex.run("bos_act", {"page": 1, "kind": "click", "ref": "e6"})      # 立即购买 on the item page
    assert out.success and ex.ran[-1][1]["_labels"] == {"e6": "立即购买"}
    assert (await ex.run("bos_snapshot", {"page": 1})).success
    st["approved"] = True
    assert (await ex.run("bos_act", {"page": 1, "kind": "dialog_accept"})).success
    assert (await ex.run("bos_act", {"page": 1, "kind": "click", "ref": "e5"})).decision.forbidden   # never 提交订单


# ----------------------------------------------------------------------------- 小红书 gate
@pytest.fixture
def store(tmp_path):
    from aiw_api import xhs
    from aiw_api.store import Store
    s = Store(str(tmp_path / "t.db"))
    xhs.ensure_tables(s)
    return s


async def test_xhs_gate_typing_upload_like_and_warning_check(store):
    from aiw_api import xhs
    comp = Comp()
    ex = Ex(comp)
    st = {"armed": None, "like_tokens": [], "typed": False}
    xhs.guard(ex, store, st, None, comp, None, "task-1")
    for args in ({"page": 1, "kind": "type", "text": "好可爱"},
                 {"page": 1, "kind": "fill", "ref": "e7", "value": "好可爱"},
                 {"page": 1, "kind": "press", "key": "Enter"},
                 {"page": 1, "kind": "click", "ref": "e8"},          # 发送
                 {"page": 1, "kind": "click", "ref": "e9"}):         # 赞 128 without xhs_like
        out = await ex.run("bos_act", args)
        assert out.decision.forbidden, args
    assert (await ex.run("bos_upload", {"page": 1, "ref": "e3", "files": ["~/workspace/uploads/a.jpg"]})).decision.forbidden
    assert not ex.ran
    st["like_tokens"] = [__import__("time").time()]
    n_probe = sum(1 for c in comp.calls if c[0] == "shell.exec")
    assert (await ex.run("bos_act", {"page": 1, "kind": "click", "ref": "e9"})).success
    assert st["like_tokens"] == []                                       # the token was spent
    assert sum(1 for c in comp.calls if c[0] == "shell.exec") == n_probe + 1   # page checked for warnings after the click
    st["armed"] = {"id": "d1", "text": "拉花好稳！", "title": "", "media": ["/data/home/workspace/uploads/a.jpg"]}
    assert (await ex.run("bos_act", {"page": 1, "kind": "fill", "ref": "e7", "value": "拉花好稳！"})).success
    assert (await ex.run("bos_act", {"page": 1, "kind": "fill", "ref": "e7", "value": "另写一句"})).decision.forbidden
    assert (await ex.run("bos_upload", {"page": 1, "ref": "e3", "files": ["~/workspace/uploads/a.jpg"]})).success
    assert (await ex.run("bos_upload", {"page": 1, "ref": "e3", "files": ["~/workspace/uploads/b.jpg"]})).decision.forbidden
    assert (await ex.run("bos_act", {"page": 1, "kind": "click", "ref": "e8"})).success   # 发送 after the approved text


# ----------------------------------------------------------------------------- executor + policy
async def test_executor_bos_call_payload_and_payment_confirm():
    comp = Comp()
    asked = []

    async def confirm(name, payload, decision):
        asked.append((name, payload, decision.reason))
        return False
    ex = ToolExecutor(comp, confirm=confirm)
    ex.bos_key, ex.bos_title = "task-9", "买一个杯子"
    out = await ex.run("bos_act", {"page": 2, "kind": "click", "ref": "e6"})
    assert out.success and out.result["element"] == "button 立即购买" and out.screenshot is None
    action, payload, observe, timeout = comp.calls[-1]
    assert action == "browseros.call" and not observe and timeout == 120
    assert payload["key"] == "task-9" and payload["tool"] == "act" and "_labels" not in payload["args"]
    out = await ex.run("bos_act", {"page": 2, "kind": "click", "ref": "e5", "_labels": {"e5": "立即支付"}, "_label": "button 立即支付"})
    assert not out.success and asked and asked[-1][1]["element"] == "button 立即支付"
    out = await ex.run("bos_act", {"page": 2, "kind": "fill", "ref": "e7", "value": "pw", "secret": True})
    assert comp.calls[-1][1]["args"]["sensitive"] is True


def test_policy_blocks_browseros_ports_in_shell():
    p = PolicyEngine()
    for cmd in ("curl http://127.0.0.1:9210/mcp", "curl localhost:9110/json/list", "python3 x.py --url http://h/mcp"):
        assert p.check("shell.exec", {"command": cmd}).forbidden, cmd
    assert not p.check("shell.exec", {"command": "cat ~/.workbuddy/mcp.json"}).forbidden


# ----------------------------------------------------------------------------- loop bits
def _loop():
    from agent_runtime.loop import AgentLoop
    from aiw_protocols import Task
    lp = AgentLoop.__new__(AgentLoop)
    lp.task = Task(prompt="x", machine_id="m1")
    lp.dispatcher = None
    lp.bos_enabled = False
    lp._secrets = {"pw": "s3cret!"}
    lp.history, lp._bos_hist = [], []
    return lp


def test_loop_hides_bos_tools_until_enabled_and_fills_secrets():
    lp = _loop()
    assert "bos_act" in lp._hidden_tools()
    lp.bos_enabled = True
    assert "bos_act" not in lp._hidden_tools()
    a = lp._fill_secrets("bos_act", {"page": 1, "kind": "fill", "fields": [{"ref": "e1", "value": "{{pw}}"}, {"ref": "e2", "value": "me"}]})
    assert a["fields"][0]["value"] == "s3cret!" and a["fields"][1]["value"] == "me" and a["secret"]
    a = lp._fill_secrets("bos_act", {"page": 1, "kind": "type", "text": "{{pw}}"})
    assert a["text"] == "s3cret!"
    assert "secret" not in lp._fill_secrets("bos_act", {"page": 1, "kind": "click", "ref": "e1"})


def test_loop_trims_older_snapshots():
    lp = _loop()
    for i in range(4):
        lp.history.append(f"{i}. bos_snapshot -> ok result:\n" + "x" * 3000)
        lp._trim_bos(len(lp.history) - 1)
    assert all("trimmed" in h for h in lp.history[:2]) and all("trimmed" not in h for h in lp.history[2:])


# ----------------------------------------------------------------------------- per-computer switch
def test_switch_defaults_on_when_installed(store, monkeypatch):
    from aiw_api import browseros as ab
    assert not ab.enabled(store, "m1")
    ab.note_health(store, "m1", {"ok": True, "browseros": {"installed": True, "running": True, "cdp_port": 9110}})
    assert ab.enabled(store, "m1") and ab.browser_name(store, "m1") == "BrowserOS"
    ab.bind(store)
    assert ab.probe_env(Comp()) == "VM_AGENT_CDP=http://127.0.0.1:9110 "
    ab.set_enabled(store, "m1", False)
    assert not ab.enabled(store, "m1") and ab.probe_env(Comp()) == "" and ab.browser_name(store, "m1") == "Chrome"
    ab.note_health(store, "m2", {"ok": True})     # an older vm-agent says nothing: stays off
    assert not ab.enabled(store, "m2")


# ----------------------------------------------------------------------------- vm-agent proxy vs a mock MCP
class MockMCP(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        sid = self.headers.get("mcp-session-id")
        MockMCP.seen.append((body.get("method"), sid, body.get("params")))
        if body.get("method") == "initialize":
            self.send_response(200)
            self.send_header("mcp-session-id", "sess-1")
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}}).encode())
            return
        if body.get("method") == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if sid != "sess-1":
            self.send_response(400)
            self.end_headers()
            return
        p = body["params"]
        text = f"tool={p['name']} args={json.dumps(p['arguments'], sort_keys=True)}\nTip: this session is x"
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode())

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()


async def test_vm_agent_proxy_against_mock_mcp(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockMCP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ports": {"server": port, "cdp": 1}, "instance": {"browseros_version": "0.51"}}))
    from vm_agent.browser import browseros as vb
    monkeypatch.setattr(vb, "CONFIG", str(cfg))
    monkeypatch.setattr(vb, "binary", lambda: "/usr/bin/browserclaw")
    MockMCP.seen = []
    st = vb.status()
    assert st["installed"] and st["running"] and st["mcp_port"] == port and st["version"] == "0.51"
    b = vb.BrowserOS({})
    r = await b.call("task-1", "snapshot", {"page": 1, "mode": "interactive", "_labels": {}, "session": "x"}, title="Buy a cup")
    assert r["ok"] and r["text"] == 'tool=snapshot args={"mode": "interactive", "page": 1}'      # tip line removed
    methods = [m for m, _, _ in MockMCP.seen]
    assert methods[:2] == ["initialize", "notifications/initialized"] and "tools/call" in methods
    assert MockMCP.seen[1][1] == "sess-1"                         # session id sent with notifications/initialized
    assert any(p and p.get("name") == "name_session" for _, _, p in MockMCP.seen)
    with pytest.raises(PermissionError):
        await b.call("task-1", "evaluate", {"page": 1, "expression": "1"})
    assert (await b.close("task-1"))["closed"]
    srv.shutdown()


async def test_vm_agent_router_maps_mcp_errors(monkeypatch):
    from vm_agent.server.router import Router
    from aiw_protocols import Command

    class B:
        env = {}
    r = Router(B())

    async def fake(key, tool, args, title=None, category=None):
        return {"ok": False, "is_error": True, "text": "no such ref e9"}
    monkeypatch.setattr(r.bos, "call", fake)
    res = await r.handle(Command(type="browseros.call", payload={"tool": "act", "args": {"page": 1}}))
    assert not res.success and "e9" in res.error
