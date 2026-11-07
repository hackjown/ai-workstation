"""v3.9.27: HTTP routes for direct connectors.

  GET    /api/connections                       records (no secrets) + redirect_uri + catalog methods
  POST   /api/connections                       {connector} | {type:"mcp", url, name, client_id?, client_secret?,
                                                bearer?} | {connector, token, base_url?, key?} (API-key connectors)
                                                -> {id, status: authorize|ok|needs_client|error, authorize_url?}
  GET    /api/connections/{id}/start            restart authorization (re-auth) -> same shape
  POST   /api/connections/{id}/test             list the connection's tools
  DELETE /api/connections/{id}?service=         revoke at the provider (when it has a revocation endpoint) + delete
  GET    /api/oauth/callback                    the redirect URI (no API token: the single-use state is the proof)
  GET    /api/oauth/settings                    {public_url, redirect_uri, providers:{id:{configured, client_id, help}}}
  PUT    /api/oauth/settings                    {public_url?, providers?:{id:{client_id, client_secret}}}
"""
from __future__ import annotations

import html
import re

from fastapi import Body, Depends, HTTPException
from fastapi.responses import HTMLResponse

from . import core
from .connections import manager
from .providers import CATALOG, METHOD_LABEL, PROVIDERS


def _page(ok: bool, title: str, msg: str) -> HTMLResponse:
    color = "#1a7f37" if ok else "#c62828"
    body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title><body style='font:16px -apple-system,system-ui;padding:40px;text-align:center'>"
            f"<h2 style='color:{color}'>{html.escape(title)}</h2><p>{html.escape(msg)}</p>"
            "<p style='color:#888'>可以关闭这个页面，回到 aiw。</p>"
            "<script>try{window.opener&&window.opener.postMessage({aiwOAuth:%s},'*')}catch(e){}"
            "setTimeout(function(){try{window.close()}catch(e){}},1500)</script></body>" % ("true" if ok else "false"))
    return HTMLResponse(body, status_code=200 if ok else 400)


def mount(app, store, secrets, notifier, auth) -> None:
    conns = manager(store, secrets, notifier)
    app.state.connections = conns

    @app.get("/api/connections", dependencies=[Depends(auth)])
    async def list_connections():
        return {"connections": [conns.public(c) for c in conns.all()], "redirect_uri": core.redirect_uri(store),
                "catalog": {k: {"name": v["name"], "method": v["method"], "label": METHOD_LABEL.get(v["method"], ""),
                                "provider": v.get("provider") or "", "mcp_url": v.get("mcp_url") or ""}
                            for k, v in CATALOG.items()}}

    async def _start(**kw) -> dict:
        try:
            return await conns.start(**kw)
        except core.OAuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001 - network errors talking to the provider
            raise HTTPException(502, f"连接失败：{type(e).__name__}: {str(e)[:200]}")

    @app.post("/api/connections", dependencies=[Depends(auth)])
    async def create_connection(body: dict = Body(...)):
        connector = str(body.get("connector") or "")
        if body.get("type") == "mcp" or (not connector and body.get("url")):
            url = str(body.get("url") or "")
            if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
                raise HTTPException(400, "MCP 地址必须以 https:// 开头")
            if body.get("bearer"):   # a server that takes a static key: classic mcp_servers entry, key in the vault
                name = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(body.get("name") or "mcp"))[:32]
                secrets.put(f"conn_{name}", str(body["bearer"]))
                cur = [m for m in (store.kv_get("mcp_servers", []) or []) if m.get("name") != name]
                cur.append({"name": name, "url": url, "headers": {"Authorization": f"Bearer {{{{secret:conn_{name}}}}}"}})
                store.kv_set("mcp_servers", cur)
                return {"id": name, "status": "ok"}
            return await _start(mcp_url=url, name=str(body.get("name") or ""), client_id=str(body.get("client_id") or ""),
                                client_secret=str(body.get("client_secret") or ""))
        cat = CATALOG.get(connector)
        if not cat:
            raise HTTPException(404, "未知连接器")
        if cat["method"] == "rest_token":
            if not body.get("token"):
                raise HTTPException(400, "先填 API Key / Token")
            if cat.get("base_url_field") and not str(body.get("base_url") or "").startswith(("https://", "http://")):
                raise HTTPException(400, "先填服务地址（https://…）")
            rec = conns.save_token(connector, str(body["token"]), str(body.get("base_url") or ""), str(body.get("key") or ""))
            return {"id": rec["id"], "status": "ok"}
        return await _start(connector=connector, send_mail=bool(body.get("send_mail")),
                            client_id=str(body.get("client_id") or ""), client_secret=str(body.get("client_secret") or ""))

    @app.get("/api/connections/{cid}/start", dependencies=[Depends(auth)])
    async def restart(cid: str):
        rec = conns.get(cid)
        if not rec:
            raise HTTPException(404, "没有这个连接")
        if rec.get("method") == "mcp_oauth" and (rec.get("connector") in (None, "", "mcp") or rec["connector"] not in CATALOG):
            return await _start(mcp_url=rec.get("mcp_url") or "", name=rec.get("name") or cid)
        if rec.get("method") == "native":
            svc = (rec.get("services") or rec.get("legacy_services") or [])
            out = None
            for s in svc or [rec.get("connector")]:
                out = await _start(connector=s)
            return out
        return await _start(connector=rec.get("connector") or cid)

    @app.post("/api/connections/{cid}/test", dependencies=[Depends(auth)])
    async def test_connection(cid: str):
        from ..integrations import connection_tools
        rec = conns.get(cid)
        if not rec:
            raise HTTPException(404, "没有这个连接")
        if rec.get("status") != "ok":
            return {"ok": False, "status": rec.get("status"), "message": rec.get("status_msg"), "tools": []}
        tools = await connection_tools(store, secrets)
        mine = [n for n in tools if n != "connector_status"]
        rec = conns.get(cid) or rec
        return {"ok": rec.get("status") == "ok", "status": rec.get("status"), "tools": mine[:60]}

    @app.delete("/api/connections/{cid}", dependencies=[Depends(auth)])
    async def delete_connection(cid: str, service: str = ""):
        if not await conns.disconnect(cid, service):
            raise HTTPException(404, "没有这个连接")
        if not service:
            secrets.delete(f"conn_{cid}")
            secrets.delete(f"conn_{cid}_key")
        return {"ok": True}

    @app.get("/api/oauth/callback")
    async def oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
        try:
            rec = await conns.callback(state, code, error, error_description)
        except core.OAuthError as e:
            return _page(False, "连接失败", str(e))
        except Exception as e:  # noqa: BLE001
            return _page(False, "连接失败", f"{type(e).__name__}: {str(e)[:200]}")
        try:
            await notifier.broadcast({"type": "connection", "id": rec["id"], "status": "ok"})
        except Exception:  # noqa: BLE001
            pass
        return _page(True, f"{rec.get('name') or rec['id']} 已连接", "授权成功，Agent 现在可以直接调用它了。")

    @app.get("/api/oauth/settings", dependencies=[Depends(auth)])
    async def oauth_settings():
        cfg = store.kv_get("oauth_settings", {}) or {}
        prov = {}
        for pid, p in PROVIDERS.items():
            c = conns.provider_client(pid)
            prov[pid] = {"name": p["name"], "configured": bool(c), "client_id": (c or {}).get("client_id", ""),
                         "has_secret": bool((c or {}).get("client_secret")), "help": p.get("help", ""),
                         "console": p.get("console", "")}
        return {"public_url": cfg.get("public_url") or "", "env_public_url": core.public_url(store) if not cfg.get("public_url") else "",
                "redirect_uri": core.redirect_uri(store), "providers": prov}

    @app.put("/api/oauth/settings", dependencies=[Depends(auth)])
    async def put_oauth_settings(body: dict = Body(...)):
        if "public_url" in body:
            u = str(body.get("public_url") or "").strip().rstrip("/")
            if u and not re.match(r"^https?://[^\s/]+", u):
                raise HTTPException(400, "公网地址要以 https:// 开头")
            cfg = store.kv_get("oauth_settings", {}) or {}
            cfg["public_url"] = u
            store.kv_set("oauth_settings", cfg)
        for pid, v in (body.get("providers") or {}).items():
            if pid not in PROVIDERS or not isinstance(v, dict):
                continue
            cid = str(v.get("client_id") or "").strip()
            sec = str(v.get("client_secret") or "")
            old = conns.provider_client(pid) or {}
            if cid and not sec and old.get("client_id") == cid:
                sec = old.get("client_secret", "")   # keep the stored secret when only the id is re-sent
            conns.set_provider_client(pid, cid, sec)
        return await oauth_settings()
