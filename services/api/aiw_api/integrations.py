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
def http_tool(spec: dict, secrets) -> tuple[dict, Any]:
    name = safe_name("ext_", spec["name"])
    params = spec.get("params") or {}
    schema = {"name": name, "description": (spec.get("description") or spec["name"])[:900],
              "parameters": {"type": "object", "properties": params,
                             "required": [k for k, v in params.items() if isinstance(v, dict) and v.get("required")]}}
    for v in params.values():
        if isinstance(v, dict):
            v.pop("required", None)

    async def call(args: dict) -> dict:
        url = secrets.expand(spec["url"])
        rest = dict(args)
        for k in list(rest):
            if "{" + k + "}" in url:
                url = url.replace("{" + k + "}", str(rest.pop(k)))
        method = (spec.get("method") or "GET").upper()
        headers = {k: secrets.expand(v) for k, v in (spec.get("headers") or {}).items()}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            if method in ("GET", "DELETE"):
                r = await c.request(method, url, params=rest, headers=headers)
            else:
                r = await c.request(method, url, json=rest, headers=headers)
        try:
            data = r.json()
        except ValueError:
            data = r.text[:4000]
        return {"status": r.status_code, "data": data if len(json.dumps(data, ensure_ascii=False)) < 6000
                else json.dumps(data, ensure_ascii=False)[:6000] + "..."}
    return schema, call


def openapi_to_tools(spec: dict, base_url: str | None = None, headers: dict | None = None, prefix: str = "",
                     limit: int = 40) -> list[dict]:
    base = base_url or ((spec.get("servers") or [{}])[0].get("url") or "")
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            params = {}
            for p in op.get("parameters") or []:
                if "$ref" in p or p.get("in") not in ("path", "query"):
                    continue
                sch = p.get("schema") or {"type": "string"}
                params[p["name"]] = {"type": sch.get("type", "string"), "description": (p.get("description") or "")[:200],
                                     "required": bool(p.get("required"))}
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            for k, v in (body.get("properties") or {}).items():
                params[k] = {"type": v.get("type", "string"), "description": (v.get("description") or "")[:200],
                             "required": k in (body.get("required") or [])}
            name = op.get("operationId") or f"{method}_{path}"
            out.append({"name": (prefix + "_" if prefix else "") + name, "method": method.upper(), "url": base.rstrip("/") + path,
                        "description": (op.get("summary") or op.get("description") or name)[:500],
                        "params": params, "headers": headers or {}})
            if len(out) >= limit:
                return out
    return out


# ------------------------------------------------------------------ MCP
class MCPClient:
    def __init__(self, url: str, headers: dict | None = None):
        self.url, self.headers = url, {"Accept": "application/json, text/event-stream", **(headers or {})}
        self.session_id: str | None = None
        self._id = 0

    async def _rpc(self, c: httpx.AsyncClient, method: str, params: dict | None = None, notify: bool = False) -> Any:
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            msg["id"] = self._id
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        r = await c.post(self.url, json=msg, headers=h)
        if r.headers.get("mcp-session-id"):
            self.session_id = r.headers["mcp-session-id"]
        if notify:
            return None
        r.raise_for_status()
        if "text/event-stream" in r.headers.get("content-type", ""):
            data = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if d.get("id") == msg["id"]:
                        data = d
            if data is None:
                raise RuntimeError("no MCP response in event stream")
        else:
            data = r.json()
        if "error" in data:
            raise RuntimeError(f"MCP error: {data['error']}")
        return data.get("result")

    async def _init(self, c):
        if self.session_id is None:
            await self._rpc(c, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                              "clientInfo": {"name": "ai-workstation", "version": "1.1"}})
            await self._rpc(c, "notifications/initialized", notify=True)

    async def list_tools(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as c:
            await self._init(c)
            res = await self._rpc(c, "tools/list", {})
        return (res or {}).get("tools", [])

    async def call(self, name: str, args: dict) -> Any:
        async with httpx.AsyncClient(timeout=120) as c:
            await self._init(c)
            res = await self._rpc(c, "tools/call", {"name": name, "arguments": args})
        texts = [b.get("text", "") for b in (res or {}).get("content", []) if b.get("type") == "text"]
        return {"text": "\n".join(texts)[:6000], "is_error": bool((res or {}).get("isError"))}


async def load_tools(store, secrets) -> dict[str, tuple[dict, Any]]:
    """name -> (schema, async call(args)). Broken integrations are skipped, not fatal."""
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
            for t in await client.list_tools():
                name = safe_name("mcp_", srv["name"], t["name"])
                schema = {"name": name, "description": (t.get("description") or t["name"])[:900],
                          "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}

                async def fn(args, _c=client, _n=t["name"]):
                    return await _c.call(_n, args)
                tools[name] = (schema, fn)
        except Exception as e:  # noqa: BLE001
            log.warning("mcp server %s: %s", srv.get("name"), e)
    return tools
