"""External tools the agent can call directly instead of clicking through a UI.

* HTTP tools: {name, description, method, url, params, headers, body}. `{param}` in the
  url is filled from the call, other params go to the query string (GET) or JSON body.
  Headers/urls may reference stored secrets as {{secret:NAME}}.
* OpenAPI import: converts a spec's operations into HTTP tools.
* MCP servers (Streamable HTTP): tools/list + tools/call over JSON-RPC.

All of them run on the API host, never inside the VM. Tool names are prefixed
ext_ (HTTP/OpenAPI) and mcp_ (MCP) so the policy and the UI can tell them apart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("aiw.integrations")
NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def safe_name(prefix: str, *parts: str) -> str:
    return (prefix + "_".join(NAME_RE.sub("_", p) for p in parts if p))[:64]


# ------------------------------------------------------------------ HTTP tools
def http_tool(spec: dict, secrets, token=None) -> tuple[dict, Any]:
    """v3.9.27: `token` (TokenProvider) adds Authorization: Bearer from an OAuth connection, refreshed on 401;
    spec["write"] True/False overrides the name-based confirm rule (tools/ connectors know what writes)."""
    name = safe_name("ext_", spec["name"])
    params = spec.get("params") or {}
    schema = {"name": name, "description": (spec.get("description") or spec["name"])[:900],
              "parameters": {"type": "object", "properties": params,
                             "required": [k for k, v in params.items() if isinstance(v, dict) and v.get("required")]}}
    for v in params.values():
        if isinstance(v, dict):
            v.pop("required", None)

    async def call(args: dict) -> dict:
        from .oauth import core as _oc
        url = secrets.expand(spec["url"])
        rest = dict(args)
        for k in list(rest):
            if "{" + k + "}" in url:
                url = url.replace("{" + k + "}", str(rest.pop(k)))
        method = (spec.get("method") or "GET").upper()
        headers = {k: secrets.expand(v) for k, v in (spec.get("headers") or {}).items()}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, transport=_oc.TRANSPORT) as c:
            async def send(h):
                if method in ("GET", "DELETE"):
                    # v3.9.27: keep query parameters already in the url (key=…&token=…): httpx `params` replaces them
                    from urllib.parse import parse_qsl, urlsplit, urlunsplit
                    sp = urlsplit(url)
                    q = dict(parse_qsl(sp.query, keep_blank_values=True))
                    q.update({k: v for k, v in rest.items()})
                    return await c.request(method, urlunsplit(sp._replace(query="")), params=q or None, headers=h)
                return await c.request(method, url, json=rest, headers=h)
            if token is not None:
                try:
                    tk = await token.get()
                except _oc.ReauthRequired as e:
                    return {"status": 401, "error": str(e), "needs_reauth": True}
                r = await send({**headers, "Authorization": "Bearer " + tk})
                if r.status_code == 401:   # refresh once, retry once
                    try:
                        r = await send({**headers, "Authorization": "Bearer " + await token.get(tk)})
                    except _oc.ReauthRequired as e:
                        return {"status": 401, "error": str(e), "needs_reauth": True}
                    if r.status_code == 401:
                        token.reauth()
                        return {"status": 401, "error": "授权已失效，需要在 设置 › 连接器 重新授权", "needs_reauth": True}
            else:
                r = await send(headers)
        try:
            data = r.json()
        except ValueError:
            data = r.text[:4000]
        return {"status": r.status_code, "data": data if len(json.dumps(data, ensure_ascii=False)) < 6000
                else json.dumps(data, ensure_ascii=False)[:6000] + "..."}
    if "write" in spec:
        w = bool(spec["write"])
        call.needs_confirm = lambda a, _w=w: _w   # type: ignore[attr-defined]
        call.confirm_reason = "连接器写操作：会改动外部账号里的数据"   # type: ignore[attr-defined]
    return schema, call


def _deref(spec: dict, node: Any, depth: int = 0) -> Any:
    """v3.9.27: resolve local $ref (#/components/...) so request bodies / parameters defined once are imported."""
    if depth > 12:
        return node
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            cur: Any = spec
            for part in node["$ref"][2:].split("/"):
                cur = (cur or {}).get(part.replace("~1", "/").replace("~0", "~")) if isinstance(cur, dict) else None
            return _deref(spec, cur or {}, depth + 1)
        return {k: _deref(spec, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(spec, v, depth + 1) for v in node]
    return node


def parse_spec(text: str) -> dict:
    """JSON or YAML OpenAPI text -> dict."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml
        d = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"不是 JSON 也不是 YAML：{e}")
    if not isinstance(d, dict):
        raise ValueError("OpenAPI 文档必须是对象")
    return d


def openapi_security(spec: dict) -> list[dict]:
    """securitySchemes -> [{name, type: bearer|apiKey|oauth2|basic, in, param}] for the UI to ask the right secret."""
    out = []
    for n, sc in ((spec.get("components") or {}).get("securitySchemes") or {}).items():
        sc = _deref(spec, sc)
        t = sc.get("type")
        if t == "http":
            out.append({"name": n, "type": "basic" if sc.get("scheme") == "basic" else "bearer"})
        elif t == "apiKey":
            out.append({"name": n, "type": "apiKey", "in": sc.get("in"), "param": sc.get("name")})
        elif t in ("oauth2", "openIdConnect"):
            fl = (sc.get("flows") or {}).get("authorizationCode") or {}
            out.append({"name": n, "type": "oauth2", "authorize": fl.get("authorizationUrl"), "token": fl.get("tokenUrl"),
                        "scopes": list((fl.get("scopes") or {}).keys())})
    return out


_UNSAFE_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "cookie"}


def openapi_to_tools(spec: dict, base_url: str | None = None, headers: dict | None = None, prefix: str = "",
                     limit: int = 40, secret: str = "") -> list[dict]:
    """v3.9.27: $ref resolved, header/path/query params, JSON body, securitySchemes -> auth header/query with
    {{secret:<secret>}}; GET = read, everything else = write (asks first)."""
    base = base_url or ((spec.get("servers") or [{}])[0].get("url") or "")
    hdr = {k: v for k, v in (headers or {}).items() if k.lower() not in _UNSAFE_HEADERS}
    query_auth: dict = {}
    if secret:
        sec = openapi_security(spec)
        s0 = sec[0] if sec else {"type": "bearer"}
        if s0["type"] in ("bearer", "oauth2"):
            hdr.setdefault("Authorization", f"Bearer {{{{secret:{secret}}}}}")
        elif s0["type"] == "basic":
            hdr.setdefault("Authorization", f"Basic {{{{secret:{secret}}}}}")
        elif s0["type"] == "apiKey" and s0.get("in") == "header" and (s0.get("param") or "").lower() not in _UNSAFE_HEADERS:
            hdr.setdefault(s0["param"], f"{{{{secret:{secret}}}}}")
        elif s0["type"] == "apiKey" and s0.get("in") == "query":
            query_auth[s0["param"]] = f"{{{{secret:{secret}}}}}"
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        ops = _deref(spec, ops) if isinstance(ops, dict) else {}
        shared = ops.get("parameters") or []
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            params = {}
            for p in list(shared) + list(op.get("parameters") or []):
                if not isinstance(p, dict) or p.get("in") not in ("path", "query") or not p.get("name"):
                    continue
                sch = p.get("schema") or {"type": "string"}
                params[p["name"]] = {"type": sch.get("type", "string"), "description": (p.get("description") or "")[:200],
                                     "required": bool(p.get("required")) or p.get("in") == "path"}
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            for k, v in (body.get("properties") or {}).items():
                v = v if isinstance(v, dict) else {}
                params[k] = {"type": v.get("type", "string"), "description": (v.get("description") or "")[:200],
                             "required": k in (body.get("required") or [])}
                if v.get("type") == "array" and isinstance(v.get("items"), dict):
                    params[k]["items"] = {"type": v["items"].get("type", "string")}
            name = op.get("operationId") or f"{method}_{path}"
            url = base.rstrip("/") + path
            if query_auth:
                from urllib.parse import urlencode
                url += ("&" if "?" in url else "?") + "&".join(f"{k}={v}" for k, v in query_auth.items())
            out.append({"name": (prefix + "_" if prefix else "") + name, "method": method.upper(), "url": url,
                        "description": (op.get("summary") or op.get("description") or name)[:500],
                        "params": params, "headers": dict(hdr), "write": method.lower() != "get"})
            if len(out) >= limit:
                return out
    return out


# ------------------------------------------------------------------ MCP
class MCPAuthError(RuntimeError):
    """v3.9.27: the server keeps answering 401 after a refresh -> the connection needs re-authorization."""


class MCPForbidden(RuntimeError):
    """v3.9.41: 403 after a refreshed token (Adobe answers 403, not 401): the account / plan / region is not allowed.
    Not a re-authorization: logging in again would give the same answer."""


class MCPClient:
    """Streamable HTTP MCP client (protocol 2025-06-18, falls back to the 2024-11-05 HTTP+SSE transport when the
    URL does not take a POST). v3.9.27: `token` is a TokenProvider (oauth/connections.py): every request carries
    Authorization: Bearer from the vault, a 401 refreshes once and retries once, a second 401 marks the connection
    needs_reauth."""
    PROTOCOL = "2025-06-18"

    def __init__(self, url: str, headers: dict | None = None, token=None, proxy: str = "", retry_403: bool = False,
                 fetch_links: bool = False):
        self.url, self.headers = url, {"Accept": "application/json, text/event-stream", **(headers or {})}
        self.token = token
        self.proxy, self.retry_403, self.fetch_links = proxy or "", retry_403, fetch_links
        self.session_id: str | None = None
        self.protocol: str | None = None
        self._id = 0
        self._legacy: dict | None = None   # {"post": url, "pending": {id: future}, "task": task}

    def _client(self, timeout: float) -> httpx.AsyncClient:
        from .oauth import core as _oc
        kw = {"proxy": self.proxy} if self.proxy and _oc.TRANSPORT is None else {}
        return httpx.AsyncClient(timeout=timeout, transport=_oc.TRANSPORT, follow_redirects=True, **kw)

    async def _headers(self, stale: str | None = None) -> dict:
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        if self.token is not None:
            h["Authorization"] = "Bearer " + await self.token.get(stale)
        return h

    async def _post(self, c: httpx.AsyncClient, msg: dict) -> httpx.Response:
        h = await self._headers()
        r = await c.post(self.url, json=msg, headers=h)
        if r.status_code == 401 and self.token is not None:   # refresh once, retry once
            await r.aclose()
            stale = h.get("Authorization", "")[7:]
            r = await c.post(self.url, json=msg, headers=await self._headers(stale))
            if r.status_code == 401:
                self.token.reauth()
                raise MCPAuthError("授权已失效，需要在 设置 › 连接器 重新授权")
        if r.status_code == 403 and self.token is not None and self.retry_403:
            # v3.9.41 Adobe: an expired/insufficient token is a bare 403 - refresh once, retry once
            body = r.text[:300]
            await r.aclose()
            stale = h.get("Authorization", "")[7:]
            try:
                r = await c.post(self.url, json=msg, headers=await self._headers(stale))
            except Exception as e:  # noqa: BLE001 - ReauthRequired from the refresh
                raise MCPAuthError(f"授权已失效，需要在 设置 › 连接器 重新授权（{e}）") from e
            if r.status_code == 403:
                raise MCPForbidden("服务拒绝访问（HTTP 403）：账号没有这个功能的权限、套餐或地区不支持，或者需要代理。"
                                   + (f" 服务返回：{body}" if body.strip() else ""))
        return r

    async def _rpc(self, c: httpx.AsyncClient, method: str, params: dict | None = None, notify: bool = False) -> Any:
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            msg["id"] = self._id
        if self._legacy is not None:
            return await self._legacy_rpc(c, msg, notify)
        r = await self._post(c, msg)
        if r.headers.get("mcp-session-id"):
            self.session_id = r.headers["mcp-session-id"]
        if notify:
            return None
        r.raise_for_status()
        if "text/event-stream" in r.headers.get("content-type", ""):
            data = _sse_pick(r.text, msg["id"])
            if data is None:
                raise RuntimeError("no MCP response in event stream")
        else:
            data = r.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    # ---- 2024-11-05 HTTP+SSE fallback: GET opens the stream, the first `endpoint` event names the POST url,
    # responses arrive on the stream
    async def _legacy_open(self, c: httpx.AsyncClient):
        from urllib.parse import urljoin
        loop = asyncio.get_running_loop()
        ready: asyncio.Future = loop.create_future()
        pending: dict = {}

        async def reader():
            try:
                h = {"Accept": "text/event-stream", **{k: v for k, v in (await self._headers()).items() if k != "Accept"}}
                async with c.stream("GET", self.url, headers=h) as r:
                    if r.status_code >= 400:
                        raise RuntimeError(f"SSE GET {r.status_code}")
                    ev = ""
                    async for line in r.aiter_lines():
                        if line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("data:"):
                            d = line[5:].strip()
                            if ev == "endpoint" and not ready.done():
                                ready.set_result(urljoin(self.url, d))
                            else:
                                try:
                                    j = json.loads(d)
                                except ValueError:
                                    continue
                                f = pending.pop(j.get("id"), None)
                                if f and not f.done():
                                    f.set_result(j)
                        elif not line:
                            ev = ""
            except Exception as e:  # noqa: BLE001
                if not ready.done():
                    ready.set_exception(e)
                for f in pending.values():
                    if not f.done():
                        f.set_exception(e)
        task = asyncio.create_task(reader())
        post = await asyncio.wait_for(ready, 15)
        self._legacy = {"post": post, "pending": pending, "task": task}

    async def _legacy_rpc(self, c, msg: dict, notify: bool):
        lg = self._legacy
        fut = None
        if not notify:
            fut = asyncio.get_running_loop().create_future()
            lg["pending"][msg["id"]] = fut
        r = await c.post(lg["post"], json=msg, headers=await self._headers())
        if r.status_code >= 400:
            raise RuntimeError(f"MCP SSE POST {r.status_code}")
        if notify:
            return None
        data = await asyncio.wait_for(fut, 60)
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    async def _close(self):
        if self._legacy:
            self._legacy["task"].cancel()
            self._legacy = None
            self.session_id = None

    async def _init(self, c):
        if self.session_id is None and self._legacy is None:
            params = {"protocolVersion": self.PROTOCOL, "capabilities": {},
                      "clientInfo": {"name": "ai-workstation", "version": "3.9.27"}}
            try:
                res = await self._rpc(c, "initialize", params)
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (400, 404, 405):
                    raise
                await self._legacy_open(c)   # v3.9.27: old HTTP+SSE server
                res = await self._rpc(c, "initialize", params)
            self.protocol = (res or {}).get("protocolVersion") or self.PROTOCOL
            await self._rpc(c, "notifications/initialized", notify=True)

    async def list_tools(self) -> list[dict]:
        async with self._client(20) as c:
            try:
                await self._init(c)
                tools, cursor = [], None
                for _ in range(20):   # paginated tools/list, capped (Rakazo: MCP_MAX_PAGES)
                    res = await self._rpc(c, "tools/list", {"cursor": cursor} if cursor else {})
                    tools += (res or {}).get("tools", [])
                    cursor = (res or {}).get("nextCursor")
                    if not cursor or len(tools) >= 250:
                        break
            finally:
                await self._close()
        return tools[:250]

    async def call(self, name: str, args: dict) -> Any:
        async with self._client(120) as c:
            try:
                await self._init(c)
                res = await self._rpc(c, "tools/call", {"name": name, "arguments": args})
            finally:
                await self._close()
        if not self.fetch_links:
            content = (res or {}).get("content", []) or []
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            out = {"text": "\n".join(texts)[:6000], "is_error": bool((res or {}).get("isError"))}
            img = next((b for b in content if b.get("type") == "image" and b.get("data")), None)
            if img:   # screenshots (Simular box sends webp): re-encode to PNG so every vision model can read them
                out["image_b64"], out["image_mime"] = to_png_b64(img["data"], img.get("mimeType") or "")
            return out
        return await rich_result(res or {}, self.proxy, name)


# ------------------------------------------------------------------ v3.9.41 rich MCP results (Adobe 创意工具)
MAX_FILE = int(__import__("os").environ.get("AIW_MCP_MAX_FILE", str(40 * 1024 * 1024)))
MAX_FILES = 8
MEDIA_EXT = re.compile(r"\.(png|jpe?g|webp|gif|tiff?|psd|svg|ai|pdf|indd|mp4|mov|webm|mp3|wav|m4a|zip)(?:[?#]|$)", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def _fname(name: str, mime: str, i: int, tool: str) -> str:
    import mimetypes
    import os as _os
    base = re.sub(r"[^\w.\-]+", "_", _os.path.basename((name or "").split("?")[0]))[:80].strip("._")
    if not base:
        base = f"{NAME_RE.sub('_', tool)[:30]}-{int(__import__('time').time())}-{i + 1}"
    if "." not in base:
        base += mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".bin"
    return base


def _seeable(mime: str) -> bool:
    return (mime or "").split(";")[0].strip().lower() in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif")


async def _fetch(url: str, proxy: str) -> tuple[bytes, str] | None:
    """A result link (Adobe returns presigned storage URLs that expire): download it now, capped."""
    from .oauth import core as _oc
    try:
        kw = {"proxy": proxy} if proxy and _oc.TRANSPORT is None else {}
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, transport=_oc.TRANSPORT, **kw) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    return None
                if int(r.headers.get("content-length") or 0) > MAX_FILE:
                    return None
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_FILE:
                        return None
                return bytes(buf), r.headers.get("content-type", "").split(";")[0]
    except Exception as e:  # noqa: BLE001
        log.warning("mcp result link %s: %s", url[:80], e)
        return None


async def rich_result(res: dict, proxy: str = "", tool: str = "") -> dict:
    """tools/call result -> {text, is_error, files[{name, mime, size, content_b64 | url}], links, image_b64?}.
    Every binary output (image blocks, embedded resource blobs, resource_link / media URLs in the text) becomes a file:
    the agent loop puts it on the task card (artifact) and on the computer (~/workspace/<save_dir>/); the model only
    reads the short text plus the first image. Big base64 never reaches the model's context."""
    import base64 as _b64
    content = res.get("content") or []
    texts, files, links = [], [], []
    first_img = None
    for i, b in enumerate(content):
        t = b.get("type")
        if t == "text":
            texts.append(b.get("text") or "")
        elif t in ("image", "audio") and b.get("data"):
            mime = b.get("mimeType") or ("image/png" if t == "image" else "audio/mpeg")
            raw = _b64.b64decode(b["data"])
            files.append({"name": _fname("", mime, i, tool), "mime": mime, "size": len(raw), "content_b64": b["data"]})
            if t == "image" and first_img is None and _seeable(mime):
                first_img = (b["data"], mime)
        elif t == "resource":
            r = b.get("resource") or {}
            mime = r.get("mimeType") or ""
            if r.get("blob"):
                raw = _b64.b64decode(r["blob"])
                files.append({"name": _fname(r.get("uri") or "", mime, i, tool), "mime": mime, "size": len(raw),
                              "content_b64": r["blob"], "uri": r.get("uri") or ""})
                if _seeable(mime) and first_img is None:
                    first_img = (r["blob"], mime)
            elif r.get("text") is not None:
                texts.append(f"[{r.get('uri') or 'resource'}]\n{r['text']}")
        elif t == "resource_link":
            links.append({"url": b.get("uri") or "", "name": b.get("name") or b.get("title") or "",
                          "mime": b.get("mimeType") or "", "size": b.get("size")})
    if not texts and res.get("structuredContent") is not None:
        texts.append(json.dumps(res["structuredContent"], ensure_ascii=False)[:6000])
    text = "\n".join(texts)
    for u in URL_RE.findall(text):   # media URLs inside the text (presigned asset links)
        u = u.rstrip(".,;")
        if MEDIA_EXT.search(u.split("?")[0].split("#")[0]) and not any(x["url"] == u for x in links):
            links.append({"url": u, "name": "", "mime": "", "size": None})
    for j, ln in enumerate(links[:MAX_FILES]):
        if not ln["url"].startswith(("https://", "http://")):
            continue
        got = await _fetch(ln["url"], proxy)
        if not got:
            ln["fetched"] = False
            continue
        data, ctype = got
        mime = ln["mime"] or ctype or ""
        files.append({"name": _fname(ln["name"] or ln["url"], mime, len(files) + j, tool), "mime": mime, "size": len(data),
                      "content_b64": _b64.b64encode(data).decode(), "url": ln["url"]})
        ln["fetched"] = True
        if _seeable(mime) and first_img is None and len(data) <= 8 * 1024 * 1024:
            first_img = (_b64.b64encode(data).decode(), mime)
    seen, uniq = set(), []
    for f in files[:MAX_FILES]:
        n, k = f["name"], 2
        while n in seen:
            stem, dot, ext = f["name"].rpartition(".")
            n = f"{stem}-{k}.{ext}" if dot else f"{f['name']}-{k}"
            k += 1
        seen.add(n)
        uniq.append({**f, "name": n})
    out = {"text": text[:6000], "is_error": bool(res.get("isError"))}
    if uniq:
        out["files"] = uniq
        lines = [f"- {f['name']}（{f['mime'] or '文件'}，{round(f['size'] / 1024)} KB）" for f in uniq]
        out["text"] = (out["text"] + "\n\n" if out["text"] else "") + "生成/返回的文件：\n" + "\n".join(lines)
    miss = [ln["url"] for ln in links if ln.get("fetched") is False]
    if miss:
        out["links"] = miss
        out["text"] += "\n没能下载的链接（可能已过期或需要代理）：\n" + "\n".join(miss[:5])
    if first_img:
        out["image_b64"], out["image_mime"] = to_png_b64(*first_img) if len(first_img[0]) < 12_000_000 else first_img
        out["image_note"] = "result"
    return out


def _sse_pick(text: str, mid) -> dict | None:
    data = None
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("id") == mid:
                data = d
    return data


def to_png_b64(data: str, mime: str = "") -> tuple[str, str]:
    if mime == "image/png":
        return data, mime
    try:
        import base64, io
        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB").save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode(), "image/png"
    except Exception:  # noqa: BLE001 - keep the original when Pillow cannot read it
        return data, mime or "image/png"


_MCP_CACHE: dict[str, tuple[float, list]] = {}
_MCP_TTL = 300.0


_MCP_STALE = 86400.0
CONN_ERRORS: dict[str, str] = {}   # v3.9.43: cid -> why its tool list failed last time (shown on the task + 测试)


def _err_text(e: BaseException, timeout: float | None = None) -> str:
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return f"超时（{int(timeout or 0)} 秒内没拿到工具列表，多半是代理慢或没走代理）"
    t = f"{type(e).__name__}: {str(e)}".strip()
    return t[:300]


async def _mcp_tools(client: "MCPClient", key: str, timeout: float = 8) -> list[dict]:
    """Tool lists are cached 5 min; v3.9.43: when a refresh fails, the last good list (up to 24 h) is used, so
    one slow proxy round trip never strips a connector from a task. Raises only with no list at all."""
    import time
    hit = _MCP_CACHE.get(key)
    if hit and time.time() - hit[0] < _MCP_TTL:
        return hit[1]
    try:
        tools = await asyncio.wait_for(client.list_tools(), timeout)
    except Exception as e:  # noqa: BLE001
        if hit and hit[1] and time.time() - hit[0] < _MCP_STALE:
            log.warning("mcp tools %s: %s - using the list from %d s ago", key[:60], _err_text(e, timeout),
                        int(time.time() - hit[0]))
            return hit[1]
        raise
    _MCP_CACHE[key] = (time.time(), tools)
    return tools


def forget_tools(prefix: str) -> None:
    """v3.9.41: drop cached tool lists (after a connection's settings change)."""
    for k in [k for k in _MCP_CACHE if k.startswith(prefix)]:
        _MCP_CACHE.pop(k, None)


def gate_tools(tools: dict, prompt: str) -> dict:
    """v3.9.41: tools whose fn carries `gate` (a regex) are only offered to tasks whose request matches it - 50+ Adobe
    tools would otherwise ride along in every step of every task. expose=always on the connection sets no gate."""
    out = {}
    for k, v in (tools or {}).items():
        g = getattr(v[1], "gate", None)
        if g and not re.search(g, prompt or ""):
            continue
        out[k] = v
    return out


_ADOBE_ASK = r"(?i)adobe|photoshop|lightroom|illustrator|firefly|indesign|adobe\s*express|(?<![a-z])ps(?![a-z])"


def adobe_route_note(tools: dict, prompt: str, store=None, secrets=None) -> str:
    """v3.9.42: the user named Photoshop / Adobe / Firefly. Returns one line for the task: how many Adobe tools
    this task got, or why it got none (not connected / needs re-auth / tool list failed), so a Photopea run is
    never silent about it. Empty when the request does not name Adobe."""
    if not re.search(_ADOBE_ASK, prompt or ""):
        return ""
    n = sum(1 for k in (tools or {}) if k.startswith("mcp_adobe_"))
    if n:
        return f"已带上 Adobe 创意工具（{n} 个），优先用 Adobe 处理。"
    status = ""
    try:
        from .oauth import manager
        for rec in manager(store, secrets).all():
            if rec.get("connector") == "adobe" or rec.get("preset") == "adobe" or rec.get("provider") == "adobe" or str(rec.get("id", "")).startswith("adobe"):
                status = rec.get("status") or "?"
                break
    except Exception:  # noqa: BLE001
        status = ""
    if not status:
        why = "Adobe 创意工具还没连接（设置 › 连接器 › 设计）"
    elif status != "ok":
        why = "Adobe 创意工具需要重新授权"
    else:
        err = CONN_ERRORS.get("adobe") or next((v for k, v in CONN_ERRORS.items() if k.startswith("adobe")), "")
        why = "Adobe 工具列表没取到：" + (err or "原因未知，看 docker logs aiw-api | grep 'connection adobe'")
    return f"{why}，这次改用 Photopea。"


async def load_tools(store, secrets) -> dict[str, tuple[dict, Any]]:
    """name -> (schema, async call(args)). Broken integrations are skipped, not fatal.
    MCP tool lists are cached for 5 minutes and each server gets 8 s, so a slow server never stalls a task start."""
    tools: dict[str, tuple[dict, Any]] = {}
    for spec in store.kv_get("http_tools", []) or []:
        if spec.get("enabled", True):
            try:
                schema, fn = http_tool(json.loads(json.dumps(spec)), secrets)
                tools[schema["name"]] = (schema, fn)
            except Exception as e:  # noqa: BLE001
                log.warning("http tool %s: %s", spec.get("name"), e)
    for srv in store.kv_get("mcp_servers", []) or []:
        if not srv.get("enabled", True):
            continue
        try:
            client = MCPClient(secrets.expand(srv["url"]), {k: secrets.expand(v) for k, v in (srv.get("headers") or {}).items()})
            for t in await _mcp_tools(client, srv["url"] + json.dumps(srv.get("headers") or {}, sort_keys=True)):
                name = safe_name("mcp_", srv["name"], t["name"])
                schema = {"name": name, "description": (t.get("description") or t["name"])[:900],
                          "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}

                async def fn(args, _c=client, _n=t["name"]):
                    return await _c.call(_n, args)
                tools[name] = (schema, fn)
        except Exception as e:  # noqa: BLE001
            log.warning("mcp server %s: %s", srv.get("name"), e)
    try:   # v3.9.27: direct connections (OAuth MCP, native Google/Microsoft, REST presets) + the phone bridge
        tools.update(await connection_tools(store, secrets))
    except Exception as e:  # noqa: BLE001
        log.warning("connections: %s", e)
    try:
        from . import device_bridge
        hub = device_bridge.existing_hub(store)
        if hub is not None:
            tools.update(hub.tools())
    except Exception as e:  # noqa: BLE001
        log.warning("device bridge: %s", e)
    return tools


def _glob(name: str, pat: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(name, pat) or name == pat


async def connection_tools(store, secrets) -> dict[str, tuple[dict, Any]]:
    """v3.9.27: tools from 设置 › 连接器 connections. A connection that needs re-authorization contributes one
    tool, connector_status, so the agent tells the user instead of failing silently."""
    import os
    from .native_tools import google_tools, microsoft_tools, rest_preset_tools
    from .oauth import manager
    from .oauth.providers import CATALOG
    conns = manager(store, secrets)
    out: dict[str, tuple[dict, Any]] = {}
    recs = conns.all()
    if not recs:
        return out
    dl = os.path.join(os.path.dirname(getattr(store, "path", "") or getattr(store, "db_path", "") or "/tmp/aiw.db"), "downloads")
    for rec in recs:
        if rec.get("status") != "ok":
            continue
        cid, method = rec["id"], rec.get("method")
        try:
            if method == "native" and rec.get("provider") == "google":
                out.update(google_tools(conns.token_provider(cid), rec.get("services") or [], dl))
            elif method == "native" and rec.get("provider") == "microsoft":
                out.update(microsoft_tools(conns.token_provider(cid)))
            elif method == "rest_oauth":
                out.update(rest_preset_tools(cid, CATALOG.get(rec.get("connector") or cid) or {}, secrets, token=conns.token_provider(cid)))
            elif method == "rest_token":
                out.update(rest_preset_tools(cid, CATALOG.get(cid) or {}, secrets, base_url=rec.get("base_url") or ""))
            elif method == "mcp_oauth" and rec.get("mcp_url"):
                from .oauth import core as _oc
                from .oauth.providers import MCP_PRESETS
                pre = MCP_PRESETS.get(rec.get("preset") or "") or {}
                tp = None if rec.get("auth") == "none" else conns.token_provider(cid)
                client = MCPClient(rec["mcp_url"], token=tp, proxy=_oc.proxy_for(store, rec),
                                   retry_403=bool(pre.get("retry_403")), fetch_links=bool(pre.get("fetch_links")))
                allow = [a.lower() for a in (rec.get("allow_tools") or [])]
                gate = pre.get("gate") if pre and rec.get("expose", "auto") != "always" else None
                _to = 45 if pre else 8   # v3.9.43: Adobe's 50+ tools over a proxy from China can take >20 s
                try:
                    _tl = await _mcp_tools(client, "conn:" + cid + rec["mcp_url"], _to)
                except Exception as e:  # noqa: BLE001
                    CONN_ERRORS[cid] = _err_text(e, _to)
                    raise
                CONN_ERRORS.pop(cid, None)
                if not _tl:
                    CONN_ERRORS[cid] = "服务器返回了空的工具列表（账号或地区可能没有开通）"
                for t in _tl:
                    if allow and not any(_glob(t["name"].lower(), a) for a in allow):
                        continue
                    name = safe_name("mcp_", cid, t["name"])
                    desc = (t.get("description") or t["name"])
                    if pre:
                        desc = f"[{rec.get('name') or cid}] " + desc
                    schema = {"name": name, "description": desc[:900],
                              "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}

                    async def fn(args, _c=client, _n=t["name"]):
                        try:
                            return await _c.call(_n, args)
                        except MCPAuthError as e:
                            return {"text": str(e), "is_error": True, "needs_reauth": True}
                        except MCPForbidden as e:
                            return {"text": str(e), "is_error": True, "forbidden": True}
                    if pre:
                        ann = t.get("annotations") or {}
                        if ann.get("destructiveHint") is True:
                            fn.needs_confirm = lambda a: True   # type: ignore[attr-defined]
                            fn.confirm_reason = f"{rec.get('name')}：会删除或覆盖你账号里的内容"   # type: ignore[attr-defined]
                        elif ann.get("readOnlyHint") is True:
                            fn.needs_confirm = lambda a: False   # type: ignore[attr-defined]
                        fn.max_result = 3000   # type: ignore[attr-defined]
                        fn.save_dir = pre.get("save_dir") or f"connectors/{cid}"   # type: ignore[attr-defined]
                        if gate:
                            fn.gate = gate   # type: ignore[attr-defined]
                    out[name] = (schema, fn)
        except Exception as e:  # noqa: BLE001 - one broken connection never hides the others
            log.warning("connection %s: %s", cid, e)

    bad = [r for r in conns.all() if r.get("status") in ("needs_reauth", "error", "needs_client")]
    if bad:
        async def status(_a, _bad=bad):
            return {"connections": [{"name": r.get("name"), "status": r.get("status"), "message": r.get("status_msg")}
                                    for r in _bad],
                    "hint": "这些连接现在不能用。请告诉用户去 设置 › 连接器 重新授权，不要换别的办法绕过。"}
        status.needs_confirm = lambda a: False   # type: ignore[attr-defined]
        out["connector_status"] = ({"name": "connector_status",
                                    "description": "查看哪些应用连接器需要用户重新授权（" + "、".join(str(r.get("name")) for r in bad)[:200] + "）",
                                    "parameters": {"type": "object", "properties": {}}}, status)
    return out
