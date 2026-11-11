"""v3.9.27: connection records + TokenManager (refresh with a per-connection lock, re-auth status) + the
authorize/callback flows behind 设置 › 连接器.

Records live in kv "connections" (no secrets in them). Secrets, all in the SecretBox (AES-256-GCM):
  oauth_tok_<cid>        token set {access_token, refresh_token, expires_at, scope, ...}
  oauth_client_<cid>     a client registration for this connection (DCR result or a client the user typed for a
                         custom MCP server), bound to the issuer it was registered at
  oauth_dcr_<cid>        v3.9.41 presets: {redirect URI: client} - one dynamic registration per redirect URI (SSH tunnel
                         127.0.0.1 vs LAN IP vs public URL); the one that issued the token is copied to oauth_client_<cid>
  oauth_cfg_<provider>   the user's own OAuth client for google / microsoft / github / slack / ... (设置 › 高级)
  conn_<connector>       API keys / tokens (same name the pre-3.9.27 UI used, so existing connections keep working)

status: ok | pending | needs_client | needs_reauth | error. needs_reauth is shown in the UI ("需要重新授权") and
returned to the agent as the tool result, so it tells the user instead of retrying.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Optional

from . import core
from .providers import CATALOG, MCP_PRESETS, PROVIDERS, RELAY_HOST_RE, RELAY_IDS, scopes_for

log = logging.getLogger("aiw.oauth")
KEY = "connections"
REAUTH_MSG = "需要重新授权"
MIGRATED_MSG = "需要重新授权（已改为直连）"


class Connections:
    def __init__(self, store, secrets, notifier=None):
        self.store, self.secrets, self.notifier = store, secrets, notifier
        self.states = core.StateStore(store)
        self._locks: dict[str, asyncio.Lock] = {}
        self.refresh_count = 0   # tests: concurrent refresh -> one token request

    # ------------------------------------------------------------- records
    def all(self) -> list[dict]:
        return list(self.store.kv_get(KEY, []) or [])

    def get(self, cid: str) -> Optional[dict]:
        return next((c for c in self.all() if c["id"] == cid), None)

    def save(self, rec: dict) -> dict:
        rec["updated_at"] = time.time()
        cur = [c for c in self.all() if c["id"] != rec["id"]]
        cur.append(rec)
        self.store.kv_set(KEY, cur)
        return rec

    def set_status(self, cid: str, status: str, msg: str = "") -> Optional[dict]:
        rec = self.get(cid)
        if not rec:
            return None
        changed = rec.get("status") != status
        rec["status"], rec["status_msg"] = status, msg
        self.save(rec)
        if changed and status == "needs_reauth" and self.notifier is not None:
            try:   # best effort push: "Gmail 需要重新授权"
                loop = asyncio.get_running_loop()
                loop.create_task(self.notifier.notify("connector", f"{rec.get('name') or cid} {REAUTH_MSG}",
                                                      "打开 设置 › 连接器 重新连接", link="/#settings"))
            except Exception:  # noqa: BLE001
                pass
        return rec

    def public(self, rec: dict) -> dict:
        tok = self.secrets.get_json(f"oauth_tok_{rec['id']}") or {}
        out = {k: v for k, v in rec.items() if k not in ("as_meta",)}
        out["has_token"] = bool(tok.get("access_token"))
        out["expires_at"] = tok.get("expires_at")
        out["scope"] = tok.get("scope") or rec.get("scope") or ""
        if rec.get("proxy"):
            out["proxy"] = core.mask_url(rec["proxy"])
            out["proxy"] = re.sub(r"//[^/@]*@", "//***@", out["proxy"])   # user:pass in a proxy URL
        return out

    def provider_client(self, provider: str) -> Optional[dict]:
        cfg = self.secrets.get_json(f"oauth_cfg_{provider}") or {}
        if not cfg.get("client_id"):
            return None
        p = PROVIDERS.get(provider) or {}
        return {"client_id": cfg["client_id"], "client_secret": cfg.get("client_secret") or "",
                "auth_method": p.get("auth_method", "client_secret_post")}

    def set_provider_client(self, provider: str, client_id: str, client_secret: str = ""):
        if not client_id:
            self.secrets.delete(f"oauth_cfg_{provider}")
        else:
            self.secrets.put_json(f"oauth_cfg_{provider}", {"client_id": client_id.strip(),
                                                            "client_secret": (client_secret or "").strip()})

    def client_for(self, rec: dict) -> Optional[dict]:
        own = self.secrets.get_json(f"oauth_client_{rec['id']}")
        if own and own.get("client_id"):
            return own
        if rec.get("provider"):
            return self.provider_client(rec["provider"])
        return None

    def endpoints(self, rec: dict) -> dict:
        if rec.get("method") in ("native", "rest_oauth") or (rec.get("method") == "mcp_oauth" and rec.get("via_provider")):
            p = PROVIDERS[rec["provider"]]
            return {"authorize": p["authorize"], "token": p["token"], "revoke": p.get("revoke") or ""}
        m = rec.get("as_meta") or {}
        return {"authorize": m.get("authorization_endpoint", ""), "token": m.get("token_endpoint", ""),
                "revoke": m.get("revocation_endpoint", "")}

    # ------------------------------------------------------------- tokens
    def _lock(self, cid: str) -> asyncio.Lock:
        lk = self._locks.get(cid)
        if lk is None:
            lk = self._locks[cid] = asyncio.Lock()
        return lk

    async def access_token(self, cid: str, stale: Optional[str] = None) -> str:
        """A valid access token. `stale`: the token a server just rejected (401) -> refresh unless someone already did.
        Refreshes are serialised per connection, so a rotating refresh token is never spent twice."""
        rec = self.get(cid)
        if not rec:
            raise core.ReauthRequired(f"连接 {cid} 不存在")
        tok = self.secrets.get_json(f"oauth_tok_{cid}") or {}
        if not tok.get("access_token"):
            self.set_status(cid, "needs_reauth", REAUTH_MSG)
            raise core.ReauthRequired(f"{rec.get('name') or cid} 还没有授权")
        if stale is None and not core.expired(tok):
            return tok["access_token"]
        async with self._lock(cid):
            tok = self.secrets.get_json(f"oauth_tok_{cid}") or {}
            if stale is None and not core.expired(tok):
                return tok["access_token"]
            if stale is not None and tok.get("access_token") and tok["access_token"] != stale and not core.expired(tok):
                return tok["access_token"]
            client = self.client_for(rec)
            ep = self.endpoints(rec)
            if not client or not ep["token"]:
                self.set_status(cid, "needs_reauth", REAUTH_MSG)
                raise core.ReauthRequired("缺少 OAuth 客户端配置")
            try:
                self.refresh_count += 1
                pre = MCP_PRESETS.get(rec.get("preset") or "") or {}
                res = "" if rec.get("resource_mode") == "off" else (rec.get("resource") or "")
                new = await core.refresh(ep["token"], client, tok, res, proxy=core.proxy_for(self.store, rec),
                                         resource_fallback=(rec.get("resource_mode") or pre.get("resource")) == "auto")
            except core.ReauthRequired as e:
                self.set_status(cid, "needs_reauth", REAUTH_MSG)
                raise core.ReauthRequired(f"{rec.get('name') or cid} {REAUTH_MSG}：{e}") from e
            self.secrets.put_json(f"oauth_tok_{cid}", new)
            if rec.get("status") != "ok":
                self.set_status(cid, "ok", "")
            return new["access_token"]

    def token_provider(self, cid: str) -> "TokenProvider":
        return TokenProvider(self, cid)

    # ------------------------------------------------------------- authorize
    async def start(self, connector: str = "", *, mcp_url: str = "", name: str = "", client_id: str = "",
                    client_secret: str = "", send_mail: bool = False, options: Optional[dict] = None) -> dict:
        """-> {id, status, authorize_url?, provider?, message?}. status "authorize": open authorize_url.
        v3.9.41 options (presets): proxy, scopes (list or space separated), resource_mode auto|on|off, guest, expose."""
        redirect = core.redirect_uri(self.store)
        cat = CATALOG.get(connector) or {}
        if connector and not cat:
            raise core.OAuthError(f"未知连接器 {connector}")
        if cat.get("preset") in MCP_PRESETS:
            return await self._start_preset(connector, cat, redirect, options or {}, client_id, client_secret)
        method = cat.get("method") or ("mcp_oauth" if mcp_url else "")
        if method in ("native", "rest_oauth"):
            provider = cat["provider"]
            cid = provider if method == "native" else connector
            rec = self.get(cid) or {"id": cid, "connector": connector, "name": PROVIDERS[provider]["name"] if method == "native"
                                    else cat["name"], "method": method, "provider": provider, "services": [],
                                    "created_at": time.time()}
            services = list(dict.fromkeys((rec.get("services") or []) + list(cat.get("services") or [connector])))
            rec.update({"services": services, "method": method, "provider": provider, "connector": rec.get("connector") or connector})
            client = self.provider_client(provider)
            if not client:
                rec.update(status=rec.get("status") if rec.get("status") == "ok" else "needs_client",
                           status_msg=f"先在「高级」里填 {PROVIDERS[provider]['name']} 的 OAuth 客户端")
                self.save(rec)
                return {"id": cid, "status": "needs_client", "provider": provider, "redirect_uri": redirect,
                        "message": rec["status_msg"]}
            send = send_mail or bool(rec.get("send_mail"))
            rec["send_mail"] = send
            scopes = scopes_for(provider, services, send=send) if method == "native" else \
                list(PROVIDERS[provider]["scopes"].get(connector, []))
            if rec.get("status") != "ok":
                rec["status"], rec["status_msg"] = "pending", "等待授权"
            self.save(rec)
            return self._authorize(rec, client, PROVIDERS[provider]["authorize"], PROVIDERS[provider]["token"], scopes,
                                   "", PROVIDERS[provider].get("extra") or {}, redirect)
        if method == "mcp_oauth":
            url = mcp_url or cat.get("mcp_url") or ""
            if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
                raise core.OAuthError("MCP 地址必须是 https://")
            cid = connector or ("mcp_" + re.sub(r"[^a-z0-9]+", "_", (name or url).lower()).strip("_")[:24] or uuid.uuid4().hex[:8])
            rec = self.get(cid) or {"id": cid, "connector": connector or "mcp", "name": cat.get("name") or name or url,
                                    "method": "mcp_oauth", "created_at": time.time()}
            rec.update({"mcp_url": url, "provider": cat.get("provider") or rec.get("provider") or ""})
            pr = await core.probe(url)
            if not pr["needs_auth"]:
                rec.update(status="ok", status_msg="无需授权", auth="none")
                self.save(rec)
                return {"id": cid, "status": "ok"}
            try:
                disc = await core.discover(url, pr.get("www_authenticate", ""))
            except core.OAuthError as e:
                if rec.get("provider") and self.provider_client(rec["provider"]):
                    disc = None   # GitHub-style: the MCP server takes the vendor's own OAuth token
                else:
                    rec.update(status="needs_client" if rec.get("provider") else "error", status_msg=str(e))
                    self.save(rec)
                    return {"id": cid, "status": rec["status"], "provider": rec.get("provider"), "message": str(e),
                            "redirect_uri": redirect}
            if disc is None:
                p = PROVIDERS[rec["provider"]]
                rec.update(via_provider=True, resource="", issuer=p["authorize"])
                self.save(rec)
                return self._authorize(rec, self.provider_client(rec["provider"]), p["authorize"], p["token"],
                                       list(p["scopes"].get(connector, [])), "", p.get("extra") or {}, redirect)
            meta = disc["as_meta"]
            rec.update(via_provider=False, resource=disc["resource"], issuer=disc["issuer"],
                       as_meta={k: meta.get(k) for k in ("issuer", "authorization_endpoint", "token_endpoint",
                                                         "registration_endpoint", "revocation_endpoint", "scopes_supported")})
            if client_id:
                client = {"client_id": client_id, "client_secret": client_secret, "issuer": disc["issuer"], "dcr": False}
                self.secrets.put_json(f"oauth_client_{cid}", client)
            else:
                client = self.secrets.get_json(f"oauth_client_{cid}")
                if client and client.get("issuer") != disc["issuer"]:
                    client = None   # bound to another authorization server: never send it elsewhere, register again
                if client and client.get("dcr") and client.get("registered_redirect") != redirect:
                    client = None
                if not client and meta.get("registration_endpoint"):
                    client = await core.register_client(meta, redirect)
                    client["issuer"] = disc["issuer"]
                    self.secrets.put_json(f"oauth_client_{cid}", client)
                if not client and rec.get("provider"):
                    pc = self.provider_client(rec["provider"])
                    client = {**pc, "issuer": disc["issuer"]} if pc else None
                if not client:
                    rec.update(status="needs_client", status_msg="该服务不支持动态注册，请填你自己注册的 client_id / client_secret")
                    self.save(rec)
                    return {"id": cid, "status": "needs_client", "provider": rec.get("provider"),
                            "redirect_uri": redirect, "message": rec["status_msg"]}
            if rec.get("status") != "ok":
                rec["status"], rec["status_msg"] = "pending", "等待授权"
            self.save(rec)
            offline = "offline_access" in (meta.get("scopes_supported") or [])
            return self._authorize(rec, client, meta["authorization_endpoint"], meta["token_endpoint"], disc["scopes"],
                                   disc["resource"], {}, redirect, offline_ok=offline)
        raise core.OAuthError(f"{cat.get('name') or connector} 不走 OAuth（{method}）")

    async def _start_preset(self, connector: str, cat: dict, redirect: str, opts: dict, client_id: str = "",
                            client_secret: str = "") -> dict:
        """v3.9.41: a remote MCP preset (Adobe 创意工具). Goes straight to OAuth: no probe (403 without a challenge),
        discovery with the preset's metadata as the fallback when adobe.io cannot be read, a confidential DCR client per
        redirect URI (registered with the scopes), PKCE S256, `resource` with an automatic fallback."""
        pre = MCP_PRESETS[cat["preset"]]
        cid = connector
        url = cat["mcp_url"]
        rec = self.get(cid) or {"id": cid, "connector": connector, "name": cat["name"], "method": "mcp_oauth",
                                "preset": cat["preset"], "created_at": time.time()}
        rec.update(mcp_url=url, preset=cat["preset"], method="mcp_oauth", provider="")
        if "proxy" in opts:
            p = str(opts.get("proxy") or "").strip()
            if not core.valid_proxy(p):
                raise core.OAuthError("代理地址要以 http:// https:// 或 socks5:// 开头")
            rec["proxy"] = p
        if opts.get("scopes"):
            sc = opts["scopes"] if isinstance(opts["scopes"], list) else str(opts["scopes"]).replace(",", " ").split()
            rec["scopes"] = [x for x in sc if x][:20]
            rec.pop("scope_note", None)
        if opts.get("resource_mode") in ("auto", "on", "off"):
            rec["resource_mode"] = opts["resource_mode"]
        if opts.get("expose") in ("auto", "always"):
            rec["expose"] = opts["expose"]
        if opts.get("allow_tools") is not None:
            al = opts["allow_tools"]
            rec["allow_tools"] = [x.strip() for x in (al if isinstance(al, list) else str(al).split(",")) if x.strip()][:200]
        proxy = core.proxy_for(self.store, rec)
        if opts.get("guest"):
            # reported "guest" tier without login: only if the server really answers an unauthenticated initialize
            pr = await core.probe(url, proxy=proxy)
            if pr["needs_auth"]:
                rec.update(status=rec.get("status") if rec.get("status") == "ok" else "error",
                           status_msg=f"免登录模式不可用（HTTP {pr.get('status')}），请用 Adobe 账号登录")
                self.save(rec)
                return {"id": cid, "status": "error", "message": rec["status_msg"]}
            rec.update(status="ok", status_msg="免登录模式（功能受限）", auth="none")
            self.save(rec)
            return {"id": cid, "status": "ok"}
        disc_note = ""
        try:
            disc = await core.discover(url, "", proxy=proxy)
            meta, issuer, resource = disc["as_meta"], disc["issuer"], disc["resource"]
        except Exception as e:  # noqa: BLE001 - unreachable from here (China) or metadata changed: use the preset's
            log.warning("preset %s discovery: %s", cid, e)
            meta, issuer, resource = dict(pre["as_meta"]), pre["issuer"], core.canonical_resource(url)
            disc_note = f"（自动发现失败，用内置的 Adobe IMS 地址：{type(e).__name__}）"
        if not meta.get("registration_endpoint") and pre["as_meta"].get("registration_endpoint") and \
                issuer.rstrip("/") == pre["issuer"].rstrip("/"):
            meta["registration_endpoint"] = pre["as_meta"]["registration_endpoint"]
        if not meta.get("revocation_endpoint") and issuer.rstrip("/") == pre["issuer"].rstrip("/"):
            meta["revocation_endpoint"] = pre["as_meta"].get("revocation_endpoint")
        scopes = list(rec.get("scopes") or pre["scopes"])
        rec.update(via_provider=False, resource=resource, issuer=issuer,
                   as_meta={k: meta.get(k) for k in ("issuer", "authorization_endpoint", "token_endpoint",
                                                     "registration_endpoint", "revocation_endpoint", "scopes_supported")})
        regs = self.secrets.get_json(f"oauth_dcr_{cid}") or {}
        if client_id:   # a client the user registered in the Adobe Developer Console (optional)
            client = {"client_id": client_id, "client_secret": client_secret, "issuer": issuer, "dcr": False,
                      "auth_method": pre.get("dcr_auth") or "client_secret_post"}
            regs[redirect] = client
        else:
            client = regs.get(redirect)
            if client and (client.get("issuer") != issuer or
                           (client.get("dcr") and pre.get("dcr_scope") and set(scopes) - set((client.get("req_scopes") or [])))):
                client = None   # another server, or registered with fewer scopes than we now ask for
            if client and client.get("secret_expires_at") and client["secret_expires_at"] < time.time() + 60:
                client = None
            if not client:
                if not meta.get("registration_endpoint"):
                    rec.update(status="needs_client", status_msg="Adobe 没有提供动态注册，请填你自己的 client_id / client_secret")
                    self.save(rec)
                    return {"id": cid, "status": "needs_client", "redirect_uri": redirect, "message": rec["status_msg"]}
                client = await core.register_client(meta, redirect, pre.get("client_name") or "AI Workstation",
                                                    auth_method=pre.get("dcr_auth") or "none",
                                                    scope=scopes if pre.get("dcr_scope") else None, proxy=proxy)
                client.update(issuer=issuer, req_scopes=scopes)
            regs[redirect] = client
        self.secrets.put_json(f"oauth_dcr_{cid}", regs)
        if not self.secrets.get_json(f"oauth_client_{cid}"):
            self.secrets.put_json(f"oauth_client_{cid}", client)   # nothing connected yet: this is the client
        if rec.get("status") != "ok":
            rec["status"], rec["status_msg"] = "pending", "等待授权" + disc_note
        self.save(rec)
        mode = rec.get("resource_mode") or pre.get("resource") or "auto"
        res = "" if mode == "off" else resource
        base = list(pre.get("base_scopes") or [])
        return self._authorize(rec, client, meta["authorization_endpoint"], meta["token_endpoint"], scopes, res, {},
                               redirect, offline_ok="offline_access" in scopes, preset=cat["preset"], req_scopes=scopes,
                               scope_retry=bool(base) and set(scopes) != set(base) and not opts.get("_scope_fallback"),
                               resource_fallback=(mode == "auto"))

    def _authorize(self, rec, client, auth_ep, token_ep, scopes, resource, extra, redirect, offline_ok=False,
                   **st_extra) -> dict:
        verifier, challenge = core.pkce_pair()
        # the client (and its secret) is NOT put in the state record: the callback looks it up again in the vault
        state = self.states.create(cid=rec["id"], verifier=verifier, redirect=redirect, token=token_ep, resource=resource,
                                   **st_extra)
        url = core.authorize_url(auth_ep, client["client_id"], redirect, state, challenge, scopes, resource, extra,
                                 offline_ok=offline_ok)
        return {"id": rec["id"], "status": "authorize", "authorize_url": url, "redirect_uri": redirect}

    async def callback(self, state: str, code: str = "", error: str = "", error_description: str = "") -> dict:
        st = self.states.pop(state)
        if not st and not state and error:   # v3.9.41: IMS sends ?error=invalid_scope with no state
            st = self.states.pop_latest(scope_retry=True)
        if not st:
            raise core.OAuthError("授权已过期或链接无效（state 不匹配），请回到连接器页面重新连接")
        rec = self.get(st["cid"])
        if not rec:
            raise core.OAuthError("连接已被删除")
        pre = MCP_PRESETS.get(st.get("preset") or "") or {}
        if error == "invalid_scope" and pre and st.get("scope_retry"):
            # v3.9.41: the server refused the extra scopes (Adobe llm.*): authorize again with the base set
            rec["scopes"] = list(pre.get("base_scopes") or [])
            rec["scope_note"] = "已退回基础权限（服务拒绝了 " + " ".join(st.get("req_scopes") or []) + "）"
            self.save(rec)
            out = await self.start(rec.get("connector") or rec["id"], options={"_scope_fallback": True})
            if out.get("status") == "authorize":
                return {**rec, "retry_url": out["authorize_url"]}
        if error or not code:
            self.set_status(rec["id"], "error", f"授权被拒绝：{error or '没有返回 code'} {error_description}"[:200])
            raise core.OAuthError(f"授权被拒绝：{error or 'no code'}", code=error or "")
        if pre:   # the registration made for THIS redirect URI issued the code; it becomes the connection's client
            reg = (self.secrets.get_json(f"oauth_dcr_{rec['id']}") or {}).get(st["redirect"])
            if reg and reg.get("client_id"):
                self.secrets.put_json(f"oauth_client_{rec['id']}", reg)
        client = self.client_for(rec)
        if not client:
            raise core.OAuthError("OAuth 客户端配置已被删除，请重新连接")
        proxy = core.proxy_for(self.store, rec)
        tok = await core.exchange_code(st["token"], client, code, st["verifier"], st["redirect"], st.get("resource") or "",
                                       proxy=proxy, resource_fallback=bool(st.get("resource_fallback")))
        self.secrets.put_json(f"oauth_tok_{rec['id']}", tok)
        rec = self.get(rec["id"]) or rec
        rec.update(status="ok", status_msg="", scope=tok.get("scope", ""), connected_at=time.time())
        if pre:
            rec["resource_token"] = tok.get("resource_sent", bool(st.get("resource")))
            rec["auth"] = "oauth"
        self.save(rec)
        return rec

    # ------------------------------------------------------------- disconnect
    async def disconnect(self, cid: str, service: str = "") -> bool:
        rec = self.get(cid)
        if not rec:
            return False
        if service and len(rec.get("services") or []) > 1:
            rec["services"] = [s for s in rec["services"] if s != service]
            self.save(rec)
            return True
        tok = self.secrets.get_json(f"oauth_tok_{cid}") or {}
        client = self.client_for(rec) or {}
        ep = self.endpoints(rec) if rec.get("method") in ("native", "rest_oauth", "mcp_oauth") else {}
        if tok and ep.get("revoke") and client:
            try:
                await core.revoke(ep["revoke"], client, tok, proxy=core.proxy_for(self.store, rec))
            except Exception as e:  # noqa: BLE001
                log.warning("revoke %s: %s", cid, e)
        for n in (f"oauth_tok_{cid}", f"oauth_client_{cid}", f"oauth_dcr_{cid}"):
            self.secrets.delete(n)
        self.store.kv_set(KEY, [c for c in self.all() if c["id"] != cid])
        return True

    # ------------------------------------------------------------- API-key connectors (rest_token)
    def save_token(self, connector: str, token: str, base_url: str = "", key: str = "") -> dict:
        cat = CATALOG.get(connector) or {}
        if cat.get("method") != "rest_token":
            raise core.OAuthError(f"{connector} 不是 API Key 连接器")
        self.secrets.put(f"conn_{connector}", token.strip())
        if key:
            self.secrets.put(f"conn_{connector}_key", key.strip())
        rec = self.get(connector) or {"id": connector, "connector": connector, "name": cat["name"], "method": "rest_token",
                                      "created_at": time.time()}
        rec.update(status="ok", status_msg="", base_url=(base_url or "").rstrip("/"))
        return self.save(rec)


class TokenProvider:
    """What MCPClient / REST tools hold: get() a bearer, get(stale=<rejected>) after a 401."""

    def __init__(self, conns: Connections, cid: str):
        self.conns, self.cid = conns, cid

    async def get(self, stale: Optional[str] = None) -> str:
        return await self.conns.access_token(self.cid, stale)

    def reauth(self, msg: str = REAUTH_MSG):
        self.conns.set_status(self.cid, "needs_reauth", msg)


# ------------------------------------------------------------------ migration from relays
def is_relay(srv: dict, secrets) -> bool:
    if srv.get("name") in RELAY_IDS:
        return True
    try:
        url = secrets.expand(srv.get("url") or "")
    except Exception:  # noqa: BLE001
        url = srv.get("url") or ""
    if re.search(RELAY_HOST_RE, url, re.I):
        return True
    cat = CATALOG.get(srv.get("name") or "")
    # pre-3.9.27 "url" connectors were relay addresses pasted into {{secret:conn_<id>}}; feishu/dingtalk were official
    return bool(cat) and cat["method"] not in ("token", "mcp_url") and srv.get("url") == f"{{{{secret:conn_{srv['name']}}}}}"


def migrate_relays(store, secrets) -> list[str]:
    """One time: relay MCP servers leave mcp_servers (the agent stops using them) and show up as connections with
    status needs_reauth + "需要重新授权（已改为直连）". The old relay address stays in the vault until the user removes
    it (it is a credential at the relay, revoke it there too)."""
    if store.kv_get("relay_migrated_v3927"):
        return []
    servers = store.kv_get("mcp_servers", []) or []
    keep, moved = [], []
    conns = list(store.kv_get(KEY, []) or [])
    have = {c["id"] for c in conns}
    for s in servers:
        if not is_relay(s, secrets):
            keep.append(s)
            continue
        name = s.get("name") or "relay"
        moved.append(name)
        cat = CATALOG.get(name) or {}
        cid = cat["provider"] if cat.get("method") == "native" else name
        if cid in have:
            for c in conns:
                if c["id"] == cid:
                    c.setdefault("legacy_services", []).append(name)
            continue
        have.add(cid)
        conns.append({"id": cid, "connector": name, "name": (PROVIDERS[cat["provider"]]["name"] if cat.get("method") == "native"
                                                            else cat.get("name") or name),
                      "method": cat.get("method") or "relay", "provider": cat.get("provider") or "",
                      "mcp_url": cat.get("mcp_url") or "", "services": [], "legacy_services": [name],
                      "status": "needs_reauth", "status_msg": MIGRATED_MSG, "migrated_from_relay": True,
                      "created_at": time.time(), "updated_at": time.time()})
    if moved:
        store.kv_set("mcp_servers", keep)
        store.kv_set(KEY, conns)
    store.kv_set("relay_migrated_v3927", time.time())
    return moved


_MGRS: dict[int, Connections] = {}


def manager(store, secrets, notifier=None) -> Connections:
    """One Connections per store, so every caller shares the same refresh locks."""
    m = _MGRS.get(id(store))
    if m is None or m.store is not store:
        m = _MGRS[id(store)] = Connections(store, secrets, notifier)
    elif notifier is not None and m.notifier is None:
        m.notifier = notifier
    return m


__all__ = ["Connections", "TokenProvider", "migrate_relays", "manager", "is_relay", "MIGRATED_MSG", "Any"]
