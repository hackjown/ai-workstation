"""v3.9.27: native Google / Microsoft tool requests (mocked HTTP), approval gating of write tools, REST presets,
and the phone device bridge (queue, online delivery, TTL, approval flag) over a real TestClient websocket."""
import asyncio
import base64
import json
import time
from email import message_from_bytes
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from aiw_api.oauth import core


class Tok:
    def __init__(self, seq=("T1",)):
        self.seq, self.calls, self.reauthed = list(seq), [], False

    async def get(self, stale=None):
        self.calls.append(stale)
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]

    def reauth(self, msg=""):
        self.reauthed = True


@pytest.fixture
def http(monkeypatch):
    seen = []
    replies = {}

    def handler(req: httpx.Request):
        seen.append(req)
        for key, fn in replies.items():
            if key in str(req.url):
                return fn(req)
        return httpx.Response(200, json={})
    monkeypatch.setattr(core, "TRANSPORT", httpx.MockTransport(handler))
    return seen, replies


async def test_gmail_search_read_draft_send(http):
    seen, replies = http
    from aiw_api.native_tools import google_tools
    raw_body = base64.urlsafe_b64encode("你好，世界".encode()).decode()
    replies["/messages/m1"] = lambda r: httpx.Response(200, json={
        "id": "m1", "threadId": "t1", "snippet": "hi", "labelIds": ["UNREAD"],
        "payload": {"headers": [{"name": "From", "value": "a@x.com"}, {"name": "Subject", "value": "S"}],
                    "parts": [{"mimeType": "text/plain", "body": {"data": raw_body}}]}})
    replies["/messages?"] = lambda r: httpx.Response(200, json={"messages": [{"id": "m1"}]})
    tools = google_tools(Tok(), ["gmail"])
    assert set(tools) == {"gmail_search", "gmail_read", "gmail_create_draft", "gmail_send"}
    res = await tools["gmail_search"][1]({"query": "from:a@x.com is:unread", "max_results": 5})
    assert res["messages"][0]["subject"] == "S" and res["messages"][0]["unread"]
    q = parse_qs(urlparse(str(seen[0].url)).query)
    assert q["q"] == ["from:a@x.com is:unread"] and q["maxResults"] == ["5"]
    assert seen[0].headers["authorization"] == "Bearer T1"
    body = await tools["gmail_read"][1]({"id": "m1"})
    assert body["body"] == "你好，世界"
    await tools["gmail_send"][1]({"to": "b@y.com", "subject": "主题", "body": "正文"})
    send = seen[-1]
    assert str(send.url).endswith("/users/me/messages/send") and send.method == "POST"
    mime = message_from_bytes(base64.urlsafe_b64decode(json.loads(send.content)["raw"]))
    assert mime["To"] == "b@y.com" and "正文" in mime.get_payload(decode=True).decode()
    await tools["gmail_create_draft"][1]({"to": "b@y.com", "subject": "s", "body": "b"})
    assert str(seen[-1].url).endswith("/users/me/drafts")
    # approval gating: writes ask first, reads do not
    assert tools["gmail_send"][1].needs_confirm({}) and tools["gmail_create_draft"][1].needs_confirm({})
    assert not tools["gmail_search"][1].needs_confirm({}) and not tools["gmail_read"][1].needs_confirm({})


async def test_gcal_and_gdrive_requests(http):
    seen, replies = http
    from aiw_api.native_tools import google_tools
    tools = google_tools(Tok(), ["gcal", "gdrive"])
    assert "gmail_send" not in tools                    # only granted services become tools
    await tools["gcal_list_events"][1]({"time_min": "2026-11-08T00:00:00+08:00", "time_max": "2026-11-09T00:00:00+08:00"})
    q = parse_qs(urlparse(str(seen[-1].url)).query)
    assert q["timeMin"] == ["2026-11-08T00:00:00+08:00"] and q["singleEvents"] == ["true"]
    await tools["gcal_create_event"][1]({"summary": "会", "start": "2026-11-08T15:00:00", "end": "2026-11-08T16:00:00",
                                         "timezone": "Asia/Shanghai", "attendees": ["c@z.com"]})
    ev = json.loads(seen[-1].content)
    assert ev["start"] == {"dateTime": "2026-11-08T15:00:00", "timeZone": "Asia/Shanghai"}
    assert ev["attendees"] == [{"email": "c@z.com"}] and "sendUpdates=all" in str(seen[-1].url)
    await tools["gcal_update_event"][1]({"event_id": "e1", "summary": "改"})
    assert seen[-1].method == "PATCH" and json.loads(seen[-1].content) == {"summary": "改"}
    await tools["gdrive_search"][1]({"query": "季度报告"})
    assert "name contains '季度报告'" in parse_qs(urlparse(str(seen[-1].url)).query)["q"][0]
    await tools["gdrive_upload"][1]({"name": "a.txt", "content": "hello"})
    up = seen[-1]
    assert "uploadType=multipart" in str(up.url) and b'"name": "a.txt"' in up.content and b"hello" in up.content
    for n in ("gcal_create_event", "gcal_update_event", "gdrive_upload"):
        assert tools[n][1].needs_confirm({}), n
    for n in ("gcal_list_events", "gdrive_search", "gdrive_download"):
        assert not tools[n][1].needs_confirm({}), n


async def test_microsoft_graph_requests(http):
    seen, replies = http
    from aiw_api.native_tools import microsoft_tools
    tools = microsoft_tools(Tok())
    await tools["outlook_send_mail"][1]({"to": "a@x.com, b@y.com", "subject": "Hi", "body": "Yo"})
    m = json.loads(seen[-1].content)
    assert str(seen[-1].url) == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert [r["emailAddress"]["address"] for r in m["message"]["toRecipients"]] == ["a@x.com", "b@y.com"]
    await tools["outlook_list_events"][1]({"start": "2026-11-08T00:00:00", "end": "2026-11-09T00:00:00", "timezone": "Asia/Shanghai"})
    assert "calendarView" in str(seen[-1].url) and seen[-1].headers["prefer"] == 'outlook.timezone="Asia/Shanghai"'
    await tools["outlook_search_mail"][1]({"query": "invoice"})
    assert parse_qs(urlparse(str(seen[-1].url)).query)["$search"] == ['"invoice"']
    await tools["outlook_create_event"][1]({"subject": "s", "start": "2026-11-08T09:00:00", "end": "2026-11-08T10:00:00"})
    assert json.loads(seen[-1].content)["start"]["timeZone"] == "UTC"
    assert tools["outlook_send_mail"][1].needs_confirm({}) and tools["outlook_create_event"][1].needs_confirm({})
    assert not tools["outlook_read_mail"][1].needs_confirm({})


async def test_native_401_refresh_retry_then_reauth(http):
    seen, replies = http
    from aiw_api.native_tools import microsoft_tools
    state = {"n": 0}

    def unauth(r):
        state["n"] += 1
        return httpx.Response(401) if r.headers["authorization"] == "Bearer OLD" else httpx.Response(200, json={"value": []})
    replies["/me/messages"] = unauth
    tok = Tok(["OLD", "NEW"])
    res = await microsoft_tools(tok)["outlook_search_mail"][1]({})
    assert res == {"messages": []} and tok.calls == [None, "OLD"] and state["n"] == 2
    replies["/me/messages"] = lambda r: httpx.Response(401)
    tok2 = Tok(["X"])
    res = await microsoft_tools(tok2)["outlook_search_mail"][1]({})
    assert res["needs_reauth"] and tok2.reauthed


async def test_rest_presets_token_and_oauth(http, tmp_path, monkeypatch):
    seen, replies = http
    monkeypatch.delenv("AIW_SECRET_KEY", raising=False)
    from aiw_api.crypto import SecretBox
    from aiw_api.native_tools import rest_preset_tools
    from aiw_api.oauth.providers import CATALOG
    from aiw_api.store import Store
    box = SecretBox(Store(str(tmp_path / "p.db")), str(tmp_path))
    box.put("conn_todoist", "td-key")
    box.put("conn_trello", "tr-tok"); box.put("conn_trello_key", "tr-key")
    td = rest_preset_tools("todoist", CATALOG["todoist"], box)
    await td["todoist_tasks"][1]({"query": "today"})
    assert seen[-1].headers["authorization"] == "Bearer td-key"
    assert td["todoist_create_task"][1].needs_confirm({}) and td["todoist_close_task"][1].needs_confirm({})
    assert not td["todoist_tasks"][1].needs_confirm({})
    tr = rest_preset_tools("trello", CATALOG["trello"], box)
    await tr["trello_boards"][1]({})
    q = parse_qs(urlparse(str(seen[-1].url)).query)
    assert q["key"] == ["tr-key"] and q["token"] == ["tr-tok"]
    db = rest_preset_tools("dropbox", CATALOG["dropbox"], box, token=Tok(["DB1"]))
    await db["dropbox_search"][1]({"query": "x"})
    assert seen[-1].headers["authorization"] == "Bearer DB1" and seen[-1].method == "POST"
    assert not db["dropbox_search"][1].needs_confirm({}) and db["dropbox_create_shared_link"][1].needs_confirm({})


def test_agent_loop_honours_needs_confirm():
    """The agent loop consults fn.needs_confirm before any extra tool (agent_runtime/loop.py)."""
    import inspect
    from agent_runtime import loop
    src = inspect.getsource(loop)
    assert 'getattr(fn, "needs_confirm", None)' in src and "self.tools.confirm(" in src


# ------------------------------------------------------------------ device bridge
async def test_hub_queue_online_delivery_ttl(tmp_path):
    from aiw_api.device_bridge import DeviceHub
    from aiw_api.store import Store
    hub = DeviceHub(Store(str(tmp_path / "d.db")), ttl=5)
    r = await hub.call("calendar_list", {"start": "a", "end": "b"})
    assert not r["ok"] and "设备能力" in r["error"]          # no device ever offered the capability
    sent = []

    async def send(m):
        sent.append(m)
    await hub.register("iph", "ios", ["calendar.read", "reminders.write"], send)
    hub.unregister("iph", send)                               # app went to the background
    assert "device_calendar_list" in hub.tools() and "device_reminders_create" in hub.tools()
    assert "device_health_read" not in hub.tools()
    task = asyncio.create_task(hub.call("calendar_list", {"start": "a", "end": "b"}))
    await asyncio.sleep(0.05)
    assert sent == [] and len(hub.pending) == 1               # queued while offline
    await hub.register("iph", "ios", ["calendar.read", "reminders.write"], send)  # app opened -> queue flushed
    assert sent[0]["type"] == "call" and sent[0]["tool"] == "calendar_list"
    await hub.on_result("iph", {"id": sent[0]["id"], "ok": True, "result": {"events": [1]}})
    assert (await task) == {"ok": True, "result": {"events": [1]}, "device": "iph"}
    hub.unregister("iph", send)
    hub.ttl = 0.2
    t0 = time.time()
    r = await hub.call("calendar_list", {"start": "a", "end": "b"})
    assert not r["ok"] and r["expired"] and time.time() - t0 < 3
    assert hub.pending == {}
    tools = hub.tools()
    assert tools["device_reminders_create"][1].needs_confirm({}) and not tools["device_calendar_list"][1].needs_confirm({})


def test_ws_device_end_to_end(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    monkeypatch.setenv("AIW_DEVICE_TTL", "20")
    s = Settings(api_token="tok", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = []
    app = build_app(s)
    hub = app.state.devices
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/device?token=bad") as ws:
                ws.receive_text()
        hello = {"type": "hello", "device_id": "iphone-1", "platform": "ios", "name": "我的 iPhone",
                 "capabilities": ["calendar.read", "calendar.write", "health.read"], "features": {"alarmkit": False}}
        with c.websocket_connect("/ws/device?token=tok") as ws:
            ws.send_text(json.dumps(hello))
            assert json.loads(ws.receive_text())["type"] == "welcome"
            fut = c.portal.start_task_soon(hub.call, "health_read", {"kind": "steps", "start": "a", "end": "b"})
            call = json.loads(ws.receive_text())
            assert call["tool"] == "health_read" and call["args"]["kind"] == "steps"
            ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": True, "result": {"steps": 8123}}))
            assert fut.result(timeout=5)["result"] == {"steps": 8123}
            ws.send_text(json.dumps({"type": "ping"}))
            assert json.loads(ws.receive_text())["type"] == "pong"
        # offline: the call waits; reconnecting delivers it; an error from the phone reaches the agent
        fut = c.portal.start_task_soon(hub.call, "calendar_create", {"title": "x", "start": "a", "end": "b"})
        time.sleep(0.2)
        assert not fut.done()
        with c.websocket_connect("/ws/device?token=tok") as ws:
            ws.send_text(json.dumps(hello))
            w = json.loads(ws.receive_text())
            assert w["type"] == "welcome" and w["queued"] == 1
            call = json.loads(ws.receive_text())
            assert call["tool"] == "calendar_create"
            ws.send_text(json.dumps({"type": "result", "id": call["id"], "ok": False, "error": "用户在手机上拒绝了日历权限"}))
            r = fut.result(timeout=5)
            assert not r["ok"] and "日历权限" in r["error"]
        devs = c.get("/api/devices", headers={"Authorization": "Bearer tok"}).json()
        assert devs["devices"][0]["id"] == "iphone-1" and devs["devices"][0]["online"] is False
    # device tools reach the agent through load_tools, writes flagged for the confirm card
    from aiw_api.integrations import load_tools
    tools = asyncio.run(load_tools(app.state.store, app.state.secrets))
    assert tools["device_calendar_create"][1].needs_confirm({}) and not tools["device_health_read"][1].needs_confirm({})
