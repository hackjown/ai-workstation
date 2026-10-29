"""3.8.7 (2026-10-29), two bugs the user hit in iOS 3.8.6 电脑 › 查看 on an iPhone:

1. Local VM (ai-linux-001): a black screen with bare white inputs, "Send Credentials" and "Cancel" = noVNC's
   vnc.html with none of its CSS/JS. The iframe had ?token= on the page only; every noVNC file under /desktop/<mid>/
   was a 401 (no cookie in the app). Now our own viewer page (desktop_view.py) on noVNC's RFB API, public noVNC
   static files, the token on the websocket and the password sent on credentialsrequired.
2. 云电脑 1 (InstaBox, vm-agent 3.8.3): pointer moves worked, taps and double taps gave 「电脑没收到操作：
   RuntimeError: timeout after 10.0s」. xdotool 3.20160805 `mousemove --sync` to the pixel the pointer is already
   on waits 15 s (measured on the box: 3 ms, then 15078 ms); a click is always sent right after the move to that
   pixel. Now no --sync anywhere, one xdotool chain per batch for older agents, computer.input only for v >= 2.
"""
import asyncio
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import desktop_view
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.remote_input import InputPump
from aiw_protocols.input_ops import budget, clean, xdotool_script
from computer_runtime.providers.base import ActionResult
from vm_agent.computer import desktop, remote_input
from vm_agent.computer.desktop import MockBackend


# ------------------------------------------------------------------ 1. the local viewer
def _app(tmp_path, monkeypatch, desk="http://desk.local:6080", pw="s3cret"):
    monkeypatch.setenv("AIW_VNC_PASSWORD", pw)
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint="ws://127.0.0.1:1", desktop_http=desk)]
    return build_app(s)


class FakeNoVNC(httpx.AsyncBaseTransport):
    def __init__(self):
        self.paths = []

    async def handle_async_request(self, request):
        self.paths.append(request.url.path)
        ct = "text/css" if request.url.path.endswith(".css") else "application/javascript"
        return httpx.Response(200, content=b"/*novnc*/", headers={"content-type": ct})


@pytest.fixture
def novnc(monkeypatch):
    fake = FakeNoVNC()
    real = httpx.AsyncClient

    import types

    def client(*a, **kw):
        kw["transport"] = fake
        return real(*a, **kw)
    # only the API's own outgoing client (the proxy to the VM's websockify), not this test's client
    monkeypatch.setattr("aiw_api.main.httpx", types.SimpleNamespace(AsyncClient=client, HTTPError=httpx.HTTPError))
    return fake


async def test_desktop_hands_out_our_viewer_with_password(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer api-secret"}) as c:
        d = (await c.get("/api/machines/ai-linux-001/desktop?view_only=false")).json()
    u = d["novnc_url"]
    assert u.startswith("/desktop/ai-linux-001/aiw-view.html?") and "view_only=0" in u
    assert "path=ws/desktop/ai-linux-001" in u and "password=s3cret" in u and d["ws_path"] == "/ws/desktop/ai-linux-001"
    assert "vnc.html" not in u


async def test_viewer_page_needs_token_sets_cookie_and_handles_credentials(tmp_path, monkeypatch, novnc):
    app = _app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/desktop/ai-linux-001/aiw-view.html?view_only=0")).status_code == 401
        assert (await c.get("/desktop/ai-linux-001/vnc.html")).status_code == 401      # pages need the token
        r = await c.get("/desktop/ai-linux-001/aiw-view.html?view_only=0&path=ws/desktop/ai-linux-001&token=api-secret")
        assert r.status_code == 200 and "aiw_token=api-secret" in r.headers.get("set-cookie", "")
        html = r.text
        # the page connects itself with noVNC's RFB API, token on the socket, password on credentialsrequired
        for bit in ('import("./core/rfb.js")', "credentialsrequired", "sendCredentials", "securityfailure",
                    'searchParams.set("token"', "scaleViewport = true", 'id="noVNC_container"', "fallback()"):
            assert bit in html, bit
        # no bare noVNC form: our notes are styled in the page itself
        assert "Send Credentials" not in html and "#aiw_msg" in html
        # noVNC's own code loads without a token (the iOS iframe has no cookie): this was the 401 behind the bare form
        for p in ("app/styles/base.css", "core/rfb.js", "vendor/pako/lib/zlib/inflate.js", "app/images/icons/novnc-192x192.png"):
            r = await c.get(f"/desktop/ai-linux-001/{p}")
            assert r.status_code == 200, p
        assert "/app/styles/base.css" in novnc.paths and "/core/rfb.js" in novnc.paths
        assert (await c.get("/desktop/nope/core/rfb.js")).status_code == 404
        # with the cookie the page just set, vnc.html works too (the fallback for noVNC < 1.0)
        assert c.cookies.get("aiw_token") == "api-secret"
        assert (await c.get("/desktop/ai-linux-001/vnc.html")).status_code == 200


def test_public_asset_rules():
    assert desktop_view.public_asset("app/styles/base.css") and desktop_view.public_asset("core/rfb.js")
    assert not desktop_view.public_asset("vnc.html") and not desktop_view.public_asset("")
    assert not desktop_view.public_asset("app/../vnc.html") and not desktop_view.public_asset("x.json")


# ------------------------------------------------------------------ 2. clicks on old / new agents
class Agent:
    """Plays a vm-agent: `version` None = before 3.8.4 (no computer.input), 1 = 3.8.4-3.8.6, 2 = 3.8.7."""
    def __init__(self, version):
        self.version, self.calls = version, []

    async def execute(self, action, payload=None, observe=False, timeout=30.0):
        self.calls.append((action, payload, timeout))
        if action == "computer.input":
            if self.version is None:
                return ActionResult(False, error="unknown action type: computer.input")
            res = {"applied": len(payload["ops"]), "width": 1600, "height": 900}
            if self.version >= 2:
                res["v"] = 2
            return ActionResult(True, res)
        if action == "computer.screen_size":
            return ActionResult(True, {"width": 1600, "height": 900})
        if action == "shell.exec":
            return ActionResult(True, {"exit_code": 0, "stdout": "", "stderr": ""})
        return ActionResult(False, error=f"unexpected {action}")


TAP = [{"t": "move", "x": 700, "y": 400}, {"t": "click", "x": 700, "y": 400, "button": "left"}]


@pytest.mark.parametrize("version", [None, 1])
async def test_older_agents_get_one_xdotool_chain_without_sync(version):
    a = Agent(version)
    p = InputPump(a)
    assert await p.screen() == (1600, 900) and p.mode == "script"
    p.push(TAP + [{"t": "click", "x": 700, "y": 400, "count": 2}])
    await p.drain()
    sh = [pl for act, pl, _ in a.calls if act == "shell.exec"]
    assert len(sh) == 1 and "--sync" not in sh[0]["command"]
    # the final move goes first and the click lands on that pixel, all in one X client
    assert "mousemove 700 400 click --repeat 1 --delay 80 1 mousemove 700 400 click --repeat 2 --delay 80 1" in sh[0]["command"]
    assert sh[0]["command"].startswith('[ -n "$DISPLAY" ] ||')   # an agent started without DISPLAY still finds :1 / :0
    # never the classic per-op actions (computer.click = mousemove --sync on those agents)
    assert not [x for x in a.calls if x[0] in ("computer.click", "computer.move", "computer.double_click")]
    assert p.applied == 3


async def test_new_agent_uses_computer_input():
    a = Agent(2)
    p = InputPump(a)
    await p.screen()
    assert p.mode == "native"
    p.push(TAP)
    await p.drain()
    batches = [pl for act, pl, _ in a.calls if act == "computer.input" and pl["ops"]]
    assert batches == [{"ops": [{"t": "move", "x": 700, "y": 400}, {"t": "click", "x": 700, "y": 400, "button": "left", "count": 1}]}]
    assert not [x for x in a.calls if x[0] == "shell.exec"]


async def test_errors_do_not_spam_and_do_not_strand_the_queue(monkeypatch):
    class Slow(Agent):
        async def execute(self, action, payload=None, observe=False, timeout=30.0):
            if action == "shell.exec" and "click" in payload["command"]:
                self.calls.append((action, payload, timeout))
                return ActionResult(False, error="timeout after 10.0s")
            return await super().execute(action, payload, observe, timeout)
    a = Slow(None)
    p = InputPump(a)
    await p.screen()
    sent = []

    async def on_err(m):
        sent.append(m)
    w = asyncio.create_task(p.run(on_err))
    for _ in range(4):
        p.push(TAP)
        await asyncio.sleep(0.02)
        await p.idle()
    p.push({"t": "key", "key": "Return"})
    await asyncio.sleep(0.02); await p.idle()
    w.cancel()
    assert len(p.errors) == 4 and len(sent) == 1          # one note, not four toasts
    assert any("key -- Return" in pl["command"] for act, pl, _ in a.calls if act == "shell.exec")


async def test_connection_loss_is_retried_once_but_a_timeout_is_not():
    class Flaky(Agent):
        n = 0

        async def execute(self, action, payload=None, observe=False, timeout=30.0):
            if action == "shell.exec":
                self.n += 1
                if self.n == 1:
                    return ActionResult(False, error="ConnectionError: vm-agent connection lost")
            return await super().execute(action, payload, observe, timeout)
    a = Flaky(None)
    p = InputPump(a)
    await p.screen()
    p.push(TAP); await p.drain()
    assert a.n == 2 and p.applied == 2


def test_budget_is_short_for_clicks_longer_for_chinese():
    assert budget([clean(o) for o in TAP]) < 8
    assert budget([{"t": "text", "text": "中" * 200}]) > 20


def test_script_quotes_text_and_keys():
    s = xdotool_script([{"t": "text", "text": "a'b; rm -rf /"}, {"t": "key", "key": "enter"}])
    assert "xdotool type --delay 12 -- 'a'\"'\"'b; rm -rf /'" in s and "xdotool key -- Return" in s
    assert xdotool_script([]) == ""


def test_x11_backend_never_syncs_and_clicks_in_one_process():
    calls = []
    b = desktop.X11Backend.__new__(desktop.X11Backend)

    async def xdo(args):
        calls.append(args)
        return ""
    b._xdo = xdo
    asyncio.run(b.click(700, 400)); asyncio.run(b.move(1, 2)); asyncio.run(b.click(5, 6, 3, 2)); asyncio.run(b.drag(1, 1, 9, 9))
    assert calls[0] == "mousemove 700 400 click --repeat 1 --delay 80 1"
    assert calls[1] == "mousemove 1 2" and calls[2] == "mousemove 5 6 click --repeat 2 --delay 80 3"
    assert all("--sync" not in c for c in calls)


def test_agent_reports_v2():
    r = asyncio.run(remote_input.run(MockBackend(), []))
    assert r["v"] == 2 and r["applied"] == 0


def test_takeover_js_quiet_errors():
    src = open(os.path.join(os.path.dirname(__file__), "..", "apps", "web", "takeover.js"), encoding="utf-8").read()
    assert "function oops(" in src and 'oops("电脑没收到操作：' in src
