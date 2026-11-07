"""v3.9.27: native Google (Gmail / Calendar / Drive) and Microsoft Graph (Outlook mail + calendar) tools, called
directly with the user's own OAuth client (oauth/providers.py) — no relay in between.

Every tool is (schema, async fn(args)). Writes (send mail, create/update event, upload) carry
fn.needs_confirm -> True, so the agent loop puts the normal confirm card up first (answerable in the web app and
on the phone); reads carry False. Token: TokenProvider (refresh under a per-connection lock, 401 -> one retry).
"""
from __future__ import annotations

import base64
import json
import os
import re
from email.message import EmailMessage
from typing import Any, Optional

import httpx

from .oauth import core as _oc

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GCAL = "https://www.googleapis.com/calendar/v3"
GDRIVE = "https://www.googleapis.com/drive/v3"
GUPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
GRAPH = "https://graph.microsoft.com/v1.0"
REASON = "会以你的账号对外发送或修改数据（邮件、日程、文件）"


class ToolError(RuntimeError):
    pass


async def api(token, method: str, url: str, *, params: dict | None = None, json_body: Any = None,
              content: bytes | None = None, headers: dict | None = None, raw: bool = False) -> Any:
    """One authorised call. 401 -> refresh + retry once; still 401 -> needs_reauth."""
    async with httpx.AsyncClient(timeout=60, transport=_oc.TRANSPORT, follow_redirects=True) as c:
        tk = await token.get()
        for attempt in range(2):
            h = {"Authorization": "Bearer " + tk, **(headers or {})}
            r = await c.request(method, url, params=params, json=json_body, content=content, headers=h)
            if r.status_code != 401:
                break
            if attempt == 0:
                tk = await token.get(tk)
        if r.status_code == 401:
            token.reauth()
            raise ToolError("授权已失效，需要在 设置 › 连接器 重新授权")
        if r.status_code == 403 and "insufficient" in r.text.lower():
            raise ToolError("当前授权缺少这个权限（scope），请在 设置 › 连接器 重新连接并勾选对应权限")
        if r.status_code >= 400:
            raise ToolError(f"HTTP {r.status_code}: {r.text[:300]}")
        if raw:
            return r
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return r.text


def _tool(name, desc, props, required, fn, write=False, max_result=4000):
    schema = {"name": name, "description": desc,
              "parameters": {"type": "object", "properties": props, "required": required}}

    async def call(a: dict, _fn=fn):
        try:
            return await _fn(a or {})
        except (_oc.ReauthRequired, ToolError) as e:
            return {"error": str(e), "needs_reauth": "重新授权" in str(e)}
    call.needs_confirm = (lambda a: True) if write else (lambda a: False)   # type: ignore[attr-defined]
    call.confirm_reason = REASON   # type: ignore[attr-defined]
    call.max_result = max_result   # type: ignore[attr-defined]
    call.write = write   # type: ignore[attr-defined]
    return name, (schema, call)


S = {"type": "string"}


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


# ------------------------------------------------------------------ Google
def _mime(to: str, subject: str, body: str, cc: str = "", sender: str = "") -> str:
    m = EmailMessage()
    m["To"], m["Subject"] = to, subject
    if cc:
        m["Cc"] = cc
    if sender:
        m["From"] = sender
    m.set_content(body or "")
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


def _gmail_text(payload: dict) -> str:
    """Plain text of a Gmail message payload (text/plain preferred, html stripped as fallback)."""
    parts, html = [], []

    def walk(p):
        mt, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and mt == "text/plain":
            parts.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
        elif data and mt == "text/html":
            html.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
        for sp in p.get("parts") or []:
            walk(sp)
    walk(payload or {})
    if parts:
        return "\n".join(parts)
    return re.sub(r"<[^>]+>", " ", "\n".join(html))


def _hdrs(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers") or []}


def google_tools(token, services: list[str], downloads_dir: str = "") -> dict:
    out: dict = {}
    if "gmail" in services:
        async def search(a):
            lst = await api(token, "GET", f"{GMAIL}/messages", params={"q": a.get("query") or "",
                                                                      "maxResults": min(int(a.get("max_results") or 10), 25)})
            res = []
            for m in (lst.get("messages") or [])[:25]:
                d = await api(token, "GET", f"{GMAIL}/messages/{m['id']}",
                              params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
                h = _hdrs(d)
                res.append({"id": d["id"], "thread_id": d.get("threadId"), "from": h.get("from"), "subject": h.get("subject"),
                            "date": h.get("date"), "snippet": d.get("snippet"), "unread": "UNREAD" in (d.get("labelIds") or [])})
            return {"messages": res}

        async def read(a):
            d = await api(token, "GET", f"{GMAIL}/messages/{a['id']}", params={"format": "full"})
            h = _hdrs(d)
            return {"id": d["id"], "from": h.get("from"), "to": h.get("to"), "cc": h.get("cc"), "subject": h.get("subject"),
                    "date": h.get("date"), "body": _gmail_text(d.get("payload") or {})[:8000]}

        async def draft(a):
            msg = {"raw": _mime(a["to"], a.get("subject") or "", a.get("body") or "", a.get("cc") or "")}
            if a.get("thread_id"):
                msg["threadId"] = a["thread_id"]
            d = await api(token, "POST", f"{GMAIL}/drafts", json_body={"message": msg})
            return {"draft_id": d.get("id"), "message_id": (d.get("message") or {}).get("id"), "saved": True}

        async def send(a):
            if a.get("draft_id"):
                d = await api(token, "POST", f"{GMAIL}/drafts/send", json_body={"id": a["draft_id"]})
            else:
                msg = {"raw": _mime(a["to"], a.get("subject") or "", a.get("body") or "", a.get("cc") or "")}
                if a.get("thread_id"):
                    msg["threadId"] = a["thread_id"]
                d = await api(token, "POST", f"{GMAIL}/messages/send", json_body=msg)
            return {"sent": True, "id": d.get("id")}
        for n, t in (
            _tool("gmail_search", "搜索 Gmail 邮件（Gmail 搜索语法，如 from:alice is:unread newer_than:7d），返回发件人、主题、摘要和 id",
                  {"query": _s("Gmail 搜索语句"), "max_results": {"type": "integer", "description": "最多几封，默认 10，最多 25"}},
                  ["query"], search),
            _tool("gmail_read", "读取一封 Gmail 邮件的正文", {"id": _s("gmail_search 返回的邮件 id")}, ["id"], read, max_result=6000),
            _tool("gmail_create_draft", "在 Gmail 里起草一封邮件（存到草稿箱，不发送）",
                  {"to": _s("收件人，多个用逗号"), "subject": _s("主题"), "body": _s("正文（纯文本）"), "cc": _s("抄送，可选"),
                   "thread_id": _s("回复某个会话时填它的 thread_id，可选")}, ["to", "subject", "body"], draft, write=True),
            _tool("gmail_send", "用用户的 Gmail 发送邮件（或发送已有草稿 draft_id）。会先请用户确认",
                  {"to": _s("收件人"), "subject": _s("主题"), "body": _s("正文"), "cc": _s("抄送，可选"),
                   "thread_id": _s("回复时的 thread_id，可选"), "draft_id": _s("直接发送这个草稿，填了就不用填其它字段")},
                  [], send, write=True),
        ):
            out[n] = t
    if "gcal" in services:
        async def list_events(a):
            p = {"singleEvents": "true", "orderBy": "startTime", "maxResults": min(int(a.get("max_results") or 25), 100)}
            for k_in, k_out in (("time_min", "timeMin"), ("time_max", "timeMax"), ("query", "q")):
                if a.get(k_in):
                    p[k_out] = a[k_in]
            d = await api(token, "GET", f"{GCAL}/calendars/{a.get('calendar_id') or 'primary'}/events", params=p)
            return {"events": [{"id": e.get("id"), "summary": e.get("summary"), "start": e.get("start"), "end": e.get("end"),
                                "location": e.get("location"), "attendees": [x.get("email") for x in e.get("attendees") or []],
                                "link": e.get("htmlLink")} for e in d.get("items") or []]}

        def _when(v: str, tz: str) -> dict:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or ""):
                return {"date": v}
            return {"dateTime": v, **({"timeZone": tz} if tz else {})}

        def _event_body(a):
            b: dict = {}
            tz = a.get("timezone") or ""
            for k in ("summary", "description", "location"):
                if a.get(k) is not None:
                    b[k] = a[k]
            if a.get("start"):
                b["start"] = _when(a["start"], tz)
            if a.get("end"):
                b["end"] = _when(a["end"], tz)
            if a.get("attendees"):
                at = a["attendees"] if isinstance(a["attendees"], list) else str(a["attendees"]).split(",")
                b["attendees"] = [{"email": x.strip()} for x in at if x.strip()]
            return b

        async def create(a):
            d = await api(token, "POST", f"{GCAL}/calendars/{a.get('calendar_id') or 'primary'}/events", json_body=_event_body(a),
                          params={"sendUpdates": "all" if a.get("attendees") else "none"})
            return {"created": True, "id": d.get("id"), "link": d.get("htmlLink")}

        async def update(a):
            d = await api(token, "PATCH", f"{GCAL}/calendars/{a.get('calendar_id') or 'primary'}/events/{a['event_id']}",
                          json_body=_event_body(a))
            return {"updated": True, "id": d.get("id"), "link": d.get("htmlLink")}
        ev_props = {"summary": _s("标题"), "start": _s("开始：RFC3339（2026-11-08T15:00:00+08:00）或全天 2026-11-08"),
                    "end": _s("结束，同上"), "description": _s("说明"), "location": _s("地点"),
                    "attendees": {"type": "array", "items": S, "description": "参会人邮箱"},
                    "timezone": _s("IANA 时区，如 Asia/Shanghai"), "calendar_id": _s("日历 id，默认 primary")}
        for n, t in (
            _tool("gcal_list_events", "列出 Google 日历里的日程（按开始时间）",
                  {"time_min": _s("起始 RFC3339"), "time_max": _s("结束 RFC3339"), "query": _s("关键词，可选"),
                   "calendar_id": _s("默认 primary"), "max_results": {"type": "integer"}}, [], list_events),
            _tool("gcal_create_event", "在 Google 日历里新建日程（有参会人时会发邀请）。会先请用户确认", ev_props,
                  ["summary", "start", "end"], create, write=True),
            _tool("gcal_update_event", "修改 Google 日历里的一个日程（只填要改的字段）。会先请用户确认",
                  {"event_id": _s("gcal_list_events 返回的 id"), **ev_props}, ["event_id"], update, write=True),
        ):
            out[n] = t
    if "gdrive" in services:
        async def dsearch(a):
            q = a.get("query") or ""
            if q and not re.search(r"\b(contains|=|in parents|mimeType)\b", q):
                esc = q.replace("\\", "\\\\").replace("'", "\\'")
                q = f"(name contains '{esc}' or fullText contains '{esc}') and trashed = false"
            d = await api(token, "GET", f"{GDRIVE}/files", params={
                "q": q or "trashed = false", "pageSize": min(int(a.get("max_results") or 20), 50),
                "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)", "orderBy": "modifiedTime desc"})
            return d

        async def download(a):
            fid = a["file_id"]
            meta = await api(token, "GET", f"{GDRIVE}/files/{fid}", params={"fields": "id,name,mimeType,size"})
            mt = meta.get("mimeType", "")
            if mt.startswith("application/vnd.google-apps."):
                exp = {"document": "text/plain", "spreadsheet": "text/csv", "presentation": "text/plain"}.get(mt.rsplit(".", 1)[-1], "application/pdf")
                r = await api(token, "GET", f"{GDRIVE}/files/{fid}/export", params={"mimeType": exp}, raw=True)
                mt = exp
            else:
                if int(meta.get("size") or 0) > 25 * 1024 * 1024:
                    raise ToolError("文件超过 25MB，请让用户在网页里下载")
                r = await api(token, "GET", f"{GDRIVE}/files/{fid}", params={"alt": "media"}, raw=True)
            res = {"name": meta.get("name"), "mime": mt, "size": len(r.content)}
            if mt.startswith("text/") or mt in ("application/json",):
                res["text"] = r.content.decode("utf-8", "replace")[:8000]
            if downloads_dir:
                os.makedirs(downloads_dir, exist_ok=True)
                safe = re.sub(r"[^\w.\-]+", "_", meta.get("name") or fid)[:120]
                path = os.path.join(downloads_dir, safe)
                with open(path, "wb") as f:
                    f.write(r.content)
                res["saved_to"] = path
            return res

        async def upload(a):
            name, mime = a["name"], a.get("mime") or "text/plain"
            data = base64.b64decode(a["content_base64"]) if a.get("content_base64") else (a.get("content") or "").encode()
            meta = {"name": name, **({"parents": [a["folder_id"]]} if a.get("folder_id") else {})}
            boundary = "aiw3927boundary"
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                    f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--".encode()
            d = await api(token, "POST", GUPLOAD, params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                          content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
            return {"uploaded": True, **(d if isinstance(d, dict) else {})}
        for n, t in (
            _tool("gdrive_search", "在 Google 云端硬盘里搜索文件（按名称和全文）",
                  {"query": _s("关键词，或 Drive 查询语法"), "max_results": {"type": "integer"}}, ["query"], dsearch),
            _tool("gdrive_download", "下载/读取云端硬盘文件：文档导出为文本、表格导出为 CSV，其它文件保存到服务器",
                  {"file_id": _s("gdrive_search 返回的 id")}, ["file_id"], download, max_result=6000),
            _tool("gdrive_upload", "上传一个文件到用户的 Google 云端硬盘（只能访问 aiw 自己上传的文件）。会先请用户确认",
                  {"name": _s("文件名"), "content": _s("文本内容"), "content_base64": _s("二进制内容的 base64（二选一）"),
                   "mime": _s("MIME 类型，默认 text/plain"), "folder_id": _s("目标文件夹 id，可选")}, ["name"], upload, write=True),
        ):
            out[n] = t
    return out


# ------------------------------------------------------------------ Microsoft Graph
def _recips(v) -> list[dict]:
    lst = v if isinstance(v, list) else [x for x in str(v or "").split(",")]
    return [{"emailAddress": {"address": x.strip()}} for x in lst if str(x).strip()]


def microsoft_tools(token) -> dict:
    out: dict = {}

    async def search(a):
        p = {"$top": min(int(a.get("top") or 10), 25), "$select": "id,subject,from,receivedDateTime,bodyPreview,isRead"}
        if a.get("query"):
            p["$search"] = '"' + str(a["query"]).replace('"', "") + '"'
        else:
            p["$orderby"] = "receivedDateTime desc"
        d = await api(token, "GET", f"{GRAPH}/me/messages", params=p)
        return {"messages": [{"id": m["id"], "subject": m.get("subject"), "from": ((m.get("from") or {}).get("emailAddress") or {}).get("address"),
                              "date": m.get("receivedDateTime"), "preview": m.get("bodyPreview"), "unread": not m.get("isRead")}
                             for m in d.get("value") or []]}

    async def read(a):
        d = await api(token, "GET", f"{GRAPH}/me/messages/{a['id']}", headers={"Prefer": 'outlook.body-content-type="text"'})
        return {"id": d.get("id"), "subject": d.get("subject"), "from": ((d.get("from") or {}).get("emailAddress") or {}).get("address"),
                "to": [r["emailAddress"]["address"] for r in d.get("toRecipients") or []], "date": d.get("receivedDateTime"),
                "body": ((d.get("body") or {}).get("content") or "")[:8000]}

    def _msg(a):
        return {"subject": a.get("subject") or "", "body": {"contentType": "Text", "content": a.get("body") or ""},
                "toRecipients": _recips(a.get("to")), **({"ccRecipients": _recips(a["cc"])} if a.get("cc") else {})}

    async def draft(a):
        d = await api(token, "POST", f"{GRAPH}/me/messages", json_body=_msg(a))
        return {"draft_id": d.get("id"), "saved": True}

    async def send(a):
        if a.get("draft_id"):
            await api(token, "POST", f"{GRAPH}/me/messages/{a['draft_id']}/send")
        else:
            await api(token, "POST", f"{GRAPH}/me/sendMail", json_body={"message": _msg(a), "saveToSentItems": True})
        return {"sent": True}

    async def events(a):
        d = await api(token, "GET", f"{GRAPH}/me/calendarView", params={
            "startDateTime": a["start"], "endDateTime": a["end"], "$top": min(int(a.get("top") or 50), 100),
            "$select": "id,subject,start,end,location,attendees,webLink", "$orderby": "start/dateTime"},
            headers={"Prefer": f'outlook.timezone="{a.get("timezone") or "UTC"}"'})
        return {"events": [{"id": e["id"], "subject": e.get("subject"), "start": e.get("start"), "end": e.get("end"),
                            "location": (e.get("location") or {}).get("displayName"), "link": e.get("webLink")}
                           for e in d.get("value") or []]}

    def _ev(a):
        tz = a.get("timezone") or "UTC"
        b: dict = {}
        if a.get("subject") is not None:
            b["subject"] = a["subject"]
        if a.get("start"):
            b["start"] = {"dateTime": a["start"], "timeZone": tz}
        if a.get("end"):
            b["end"] = {"dateTime": a["end"], "timeZone": tz}
        if a.get("body") is not None:
            b["body"] = {"contentType": "Text", "content": a["body"]}
        if a.get("location"):
            b["location"] = {"displayName": a["location"]}
        if a.get("attendees"):
            b["attendees"] = [{**r, "type": "required"} for r in _recips(a["attendees"])]
        return b

    async def create(a):
        d = await api(token, "POST", f"{GRAPH}/me/events", json_body=_ev(a))
        return {"created": True, "id": d.get("id"), "link": d.get("webLink")}

    async def update(a):
        d = await api(token, "PATCH", f"{GRAPH}/me/events/{a['event_id']}", json_body=_ev(a))
        return {"updated": True, "id": d.get("id")}
    ev = {"subject": _s("标题"), "start": _s("开始，本地时间 2026-11-08T15:00:00"), "end": _s("结束"),
          "timezone": _s("Windows 或 IANA 时区，如 China Standard Time / Asia/Shanghai"), "body": _s("说明"),
          "location": _s("地点"), "attendees": {"type": "array", "items": S, "description": "参会人邮箱"}}
    for n, t in (
        _tool("outlook_search_mail", "搜索 Outlook 邮件（不填 query 就是最新邮件）",
              {"query": _s("关键词"), "top": {"type": "integer"}}, [], search),
        _tool("outlook_read_mail", "读取一封 Outlook 邮件正文", {"id": _s("邮件 id")}, ["id"], read, max_result=6000),
        _tool("outlook_create_draft", "在 Outlook 里起草邮件（存草稿，不发送）",
              {"to": _s("收件人"), "subject": _s("主题"), "body": _s("正文"), "cc": _s("抄送")}, ["to", "subject", "body"],
              draft, write=True),
        _tool("outlook_send_mail", "用用户的 Outlook 发送邮件（或发送草稿 draft_id）。会先请用户确认",
              {"to": _s("收件人"), "subject": _s("主题"), "body": _s("正文"), "cc": _s("抄送"), "draft_id": _s("草稿 id")},
              [], send, write=True),
        _tool("outlook_list_events", "列出 Outlook 日历在一段时间内的日程",
              {"start": _s("开始 ISO 时间"), "end": _s("结束 ISO 时间"), "timezone": _s("返回用的时区"), "top": {"type": "integer"}},
              ["start", "end"], events),
        _tool("outlook_create_event", "在 Outlook 日历新建日程（有参会人会发邀请）。会先请用户确认", ev,
              ["subject", "start", "end"], create, write=True),
        _tool("outlook_update_event", "修改 Outlook 日程（只填要改的字段）。会先请用户确认", {"event_id": _s("日程 id"), **ev},
              ["event_id"], update, write=True),
    ):
        out[n] = t
    return out


def rest_preset_tools(cid: str, cat: dict, secrets, token=None, base_url: str = "") -> dict:
    """Preset REST tools of a rest_oauth / rest_token connector (oauth/providers.py CATALOG[...]["tools"])."""
    from .integrations import http_tool
    out = {}
    for t in cat.get("tools") or []:
        spec = json.loads(json.dumps(t))
        spec["url"] = spec["url"].replace("{base}", base_url or "")
        hdr = {}
        if token is None:
            if cat.get("query_auth"):   # Trello: key + token in the query string
                sep = "&" if "?" in spec["url"] else "?"
                spec["url"] += f"{sep}key={{{{secret:conn_{cid}_key}}}}&token={{{{secret:conn_{cid}}}}}"
            else:
                hdr[cat.get("header") or "Authorization"] = (cat.get("prefix") if cat.get("prefix") is not None else "Bearer ") + \
                    f"{{{{secret:conn_{cid}}}}}"
        spec["headers"] = hdr
        reads = ("_search", "_list_folder", "_get_temporary_link")
        spec["write"] = spec.get("method", "GET").upper() != "GET" and not spec["name"].endswith(reads)
        schema, fn = http_tool(spec, secrets, token=token)
        schema["name"] = spec["name"]   # connector tools keep their own names (dropbox_search, not ext_dropbox_search)
        out[spec["name"]] = (schema, fn)
    return out


__all__ = ["google_tools", "microsoft_tools", "rest_preset_tools", "api", "ToolError", "Optional"]
