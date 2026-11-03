"""v3.9.17 browser_network: the requests a page makes, read through Chrome DevTools (Network domain).

"对照网站" jobs (what does site A send that our request does not) were done by screenshots of DevTools. This
attaches to a tab of the AIW Chrome (9222) or BrowserOS (9110), listens for a few seconds (optionally reloading the
page or opening a URL in it first) and returns each request: method, URL, resource type, status, mime type, the
request headers with secret values masked, the NAMES of the cookies sent and set, and the time it took.
Cookie values never leave this computer.

`summarize()` is pure (tested in tests/test_v3917_browser_network.py); `capture()` talks to the browser.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

SECRET_HEADER = re.compile(r"^(cookie|set-cookie|authorization|proxy-authorization|x-csrf-token|x-xsrf-token|"
                           r"x-api-key|api-key|x-auth-token|x-access-token|.*token.*|.*secret.*|bx-ua|bx-umidtoken)$", re.I)
KEEP_VALUE = re.compile(r"^(content-type|accept|accept-language|referer|origin|user-agent|sec-fetch-\w+|sec-ch-ua.*|"
                        r"x-requested-with|content-length|cache-control|pragma|host|bx-v)$", re.I)


def cookie_names(header: str) -> list[str]:
    out = []
    for part in (header or "").split(";"):
        k = part.split("=", 1)[0].strip()
        if k and k not in out:
            out.append(k)
    return out


def set_cookie_names(header: str) -> list[str]:
    out = []
    for line in re.split(r"\n", header or ""):
        k = line.split(";", 1)[0].split("=", 1)[0].strip()
        if k and k not in out:
            out.append(k)
    return out


def safe_headers(h: Optional[dict]) -> dict:
    out = {}
    for k, v in (h or {}).items():
        lk = str(k).lower()
        if lk in ("cookie", "set-cookie"):
            continue   # names only, below
        if SECRET_HEADER.match(lk) and not KEEP_VALUE.match(lk):
            out[k] = f"<{len(str(v))} chars>"
        else:
            out[k] = str(v)[:300]
    return out


def _match(url: str, pat: str) -> bool:
    if not pat:
        return True
    if len(pat) > 2 and pat.startswith("/") and pat.endswith("/"):
        try:
            return bool(re.search(pat[1:-1], url))
        except re.error:
            return pat in url
    return pat.lower() in url.lower()


def summarize(events: list[dict], match: str = "", limit: int = 60) -> dict:
    """CDP Network.* events -> {requests: [...], count, shown}. Request order is kept."""
    reqs: dict[str, dict] = {}
    order: list[str] = []
    for ev in events:
        m, p = ev.get("method"), ev.get("params") or {}
        rid = p.get("requestId")
        if not rid:
            continue
        r = reqs.get(rid)
        if r is None and m in ("Network.requestWillBeSent", "Network.requestWillBeSentExtraInfo",
                               "Network.responseReceived", "Network.responseReceivedExtraInfo"):
            r = reqs[rid] = {"id": rid, "cookies_sent": [], "cookies_set": [], "headers": {}}
            order.append(rid)
        if r is None:
            continue
        if m == "Network.requestWillBeSent":
            q = p.get("request") or {}
            r.update(method=q.get("method"), url=q.get("url"), type=p.get("type"), t0=p.get("timestamp"))
            r["headers"].update(q.get("headers") or {})
            if q.get("hasPostData") or q.get("postData"):
                r["post_bytes"] = len(q.get("postData") or "")
            if p.get("redirectResponse"):
                r["redirected_from"] = (p.get("redirectResponse") or {}).get("status")
        elif m == "Network.requestWillBeSentExtraInfo":
            r["headers"].update(p.get("headers") or {})
            for c in p.get("associatedCookies") or []:
                name = ((c.get("cookie") or {}).get("name"))
                if name and name not in r["cookies_sent"] and not c.get("blockedReasons"):
                    r["cookies_sent"].append(name)
        elif m == "Network.responseReceived":
            s = p.get("response") or {}
            r.update(status=s.get("status"), mime=s.get("mimeType"), remote=s.get("remoteIPAddress"),
                     protocol=s.get("protocol"))
            if not r.get("url"):
                r["url"] = s.get("url")
            sh = {str(k).lower(): v for k, v in (s.get("headers") or {}).items()}
            if sh.get("set-cookie"):
                r["cookies_set"] += [n for n in set_cookie_names(sh["set-cookie"]) if n not in r["cookies_set"]]
            if sh.get("location"):
                r["location"] = str(sh["location"])[:300]
            r["t1"] = p.get("timestamp")
        elif m == "Network.responseReceivedExtraInfo":
            sh = {str(k).lower(): v for k, v in (p.get("headers") or {}).items()}
            if sh.get("set-cookie"):
                r["cookies_set"] += [n for n in set_cookie_names(sh["set-cookie"]) if n not in r["cookies_set"]]
            if p.get("statusCode") and not r.get("status"):
                r["status"] = p.get("statusCode")
        elif m == "Network.loadingFailed":
            r["failed"] = p.get("errorText") or "failed"
            if p.get("blockedReason"):
                r["failed"] += f" ({p['blockedReason']})"
        elif m == "Network.loadingFinished":
            r["bytes"] = p.get("encodedDataLength")
            r["t1"] = r.get("t1") or p.get("timestamp")
    out = []
    for rid in order:
        r = reqs[rid]
        url = r.get("url") or ""
        if not url or url.startswith("data:") or not _match(url, match):
            continue
        hdr = r.pop("headers", {})
        ck = next((v for k, v in hdr.items() if str(k).lower() == "cookie"), "")
        for n in cookie_names(ck):
            if n not in r["cookies_sent"]:
                r["cookies_sent"].append(n)
        r["headers"] = safe_headers(hdr)
        if r.get("t0") and r.get("t1"):
            r["ms"] = int((float(r["t1"]) - float(r["t0"])) * 1000)
        for k in ("t0", "t1", "id"):
            r.pop(k, None)
        out.append(r)
    return {"count": len(out), "shown": min(len(out), limit), "requests": out[:limit]}


def text(summary: dict) -> str:
    """A compact readout for the model (one block per request)."""
    lines = [f"{summary.get('count', 0)} requests (showing {summary.get('shown', 0)})"]
    for i, r in enumerate(summary.get("requests") or [], 1):
        st = r.get("status") if r.get("status") is not None else (r.get("failed") or "?")
        lines.append(f"[{i}] {r.get('method') or '?'} {st} {r.get('type') or ''} {r.get('url')}"
                     + (f" ({r['ms']} ms)" if r.get("ms") is not None else ""))
        if r.get("cookies_sent"):
            lines.append("    cookies sent: " + ", ".join(r["cookies_sent"]))
        if r.get("cookies_set"):
            lines.append("    cookies set: " + ", ".join(r["cookies_set"]))
        if r.get("location"):
            lines.append("    -> " + r["location"])
        hs = r.get("headers") or {}
        if hs:
            lines.append("    headers: " + "; ".join(f"{k}: {v}" for k, v in list(hs.items())[:24]))
    return "\n".join(lines)


async def _targets(port: int) -> list[dict]:
    import httpx
    async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
        r = await c.get(f"http://127.0.0.1:{port}/json/list")
        r.raise_for_status()
        return [p for p in r.json() if p.get("type") == "page" and p.get("webSocketDebuggerUrl")
                and not str(p.get("url", "")).startswith(("devtools://", "chrome-extension://", "chrome://"))]


async def capture(url: str = "", page_match: str = "", reload: bool = False, seconds: float = 6.0,
                  match: str = "", limit: int = 60, port: Optional[int] = None) -> dict:
    import websockets
    pages, used = [], None
    for p in ([int(port)] if port else [9222, 9110]):
        try:
            pages = await _targets(p)
            if pages:
                used = p
                break
        except Exception:  # noqa: BLE001
            continue
    if not pages:
        raise RuntimeError("没有可连的浏览器 DevTools 端口（9222 AIW Chrome / 9110 BrowserOS）；先 browser_open 打开页面")
    tab = next((p for p in pages if page_match and page_match.lower() in str(p.get("url", "")).lower()), pages[0])
    events: list[dict] = []
    ids = iter(range(1, 10 ** 6))
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=5) as ws:
        async def call(method, params=None):
            i = next(ids)
            await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            return i
        await call("Network.enable", {"maxPostDataSize": 0})
        if url:
            await call("Page.navigate", {"url": url})
        elif reload:
            await call("Page.reload", {"ignoreCache": True})
        end = time.monotonic() + max(1.0, min(float(seconds or 6), 60.0))
        while time.monotonic() < end:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), max(0.05, end - time.monotonic())))
            except asyncio.TimeoutError:
                break
            if str(msg.get("method") or "").startswith("Network."):
                events.append(msg)
        try:
            await call("Network.disable")
        except Exception:  # noqa: BLE001
            pass
    s = summarize(events, match, int(limit or 60))
    s.update(page=tab.get("url"), title=tab.get("title"), port=used)
    s["text"] = f"page: {tab.get('title') or ''} {tab.get('url')}\n" + text(s)
    return s
