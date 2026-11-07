"""v3.9.27: OAuth 2.x core for direct connectors (replaces the third-party MCP relays).

* PKCE (RFC 7636, S256 only) and server-side `state` records with an expiry (kv "oauth_states"), single use.
* Redirect URI: <AIW_PUBLIC_URL or kv oauth_settings.public_url>/api/oauth/callback, else the loopback
  http://127.0.0.1:<AIW_PORT>/api/oauth/callback (Google/Microsoft desktop clients and most DCR servers accept it).
* Remote MCP authorization (MCP spec 2025-06-18): 401 -> WWW-Authenticate resource_metadata -> RFC 9728 protected
  resource metadata -> RFC 8414 authorization server metadata (OIDC discovery fallback) -> RFC 7591 dynamic client
  registration when the server offers it -> authorization code + PKCE with the RFC 8707 `resource` parameter.
* Token exchange / refresh / revoke helpers. All HTTP goes through `http()` so tests can plug in httpx.MockTransport.

Ideas ported from Rakazo's packages/adapters/src/mcp-oauth.ts (Apache-2.0): offline_access only when the server
lists it, canonical resource must be https/localhost, a client registration is bound to the issuer it came from.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets as _secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

STATE_TTL = 600.0            # an authorization must finish within 10 minutes
TOKEN_SKEW = 60.0            # refresh this many seconds before the access token lapses
CALLBACK_PATH = "/api/oauth/callback"
TRANSPORT: Optional[httpx.AsyncBaseTransport] = None   # tests: httpx.MockTransport(handler)


class OAuthError(RuntimeError):
    pass


class ReauthRequired(OAuthError):
    """The grant is gone (invalid_grant / revoked / no refresh token): the user must connect again."""


def http(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=TRANSPORT,
                             headers={"User-Agent": "ai-workstation/3.9.27"})


# ------------------------------------------------------------------ PKCE + state
def pkce_pair() -> tuple[str, str]:
    verifier = _secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class StateStore:
    """Pending authorizations, keyed by the random `state`. The verifier never leaves the server."""
    KEY = "oauth_states"

    def __init__(self, store, ttl: float = STATE_TTL):
        self.store, self.ttl = store, ttl

    def _all(self) -> dict:
        now = time.time()
        return {k: v for k, v in (self.store.kv_get(self.KEY, {}) or {}).items() if v.get("exp", 0) > now}

    def create(self, **data) -> str:
        st = _secrets.token_urlsafe(32)
        allv = self._all()
        allv[st] = {**data, "exp": time.time() + self.ttl}
        self.store.kv_set(self.KEY, allv)
        return st

    def pop(self, state: str) -> Optional[dict]:
        allv = self.store.kv_get(self.KEY, {}) or {}
        rec = allv.pop(state or "", None)
        self.store.kv_set(self.KEY, {k: v for k, v in allv.items() if v.get("exp", 0) > time.time()})
        if not rec or rec.get("exp", 0) <= time.time():
            return None
        return rec


# ------------------------------------------------------------------ redirect URI
def public_url(store) -> str:
    cfg = store.kv_get("oauth_settings", {}) or {}
    return (cfg.get("public_url") or os.environ.get("AIW_PUBLIC_URL") or "").strip().rstrip("/")


def redirect_uri(store) -> str:
    base = public_url(store)
    if base:
        return base + CALLBACK_PATH
    return f"http://127.0.0.1:{os.environ.get('AIW_PORT', '8000')}{CALLBACK_PATH}"


# ------------------------------------------------------------------ discovery (MCP authorization spec)
def parse_www_authenticate(h: str) -> dict:
    """Bearer realm="x", resource_metadata="https://…", scope="a b", error="invalid_token" -> dict."""
    out = {}
    for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_\-]*)\s*=\s*(?:"([^"]*)"|([^\s,]+))', h or ""):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def canonical_resource(url: str) -> str:
    """RFC 8707 / MCP: scheme+host lowercase, no fragment, no trailing slash unless the path is only '/'."""
    u = urlparse(url)
    path = u.path or ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    return urlunparse((u.scheme.lower(), u.netloc.lower(), path, "", u.query, ""))


def _safe_url(u: str) -> bool:
    p = urlparse(u or "")
    return p.scheme == "https" or (p.scheme == "http" and (p.hostname or "") in ("127.0.0.1", "localhost", "::1"))


def _wk(base: str, name: str) -> list[str]:
    """RFC 8414 / 9728 well-known locations: path-inserted first, then the origin root."""
    u = urlparse(base)
    path = (u.path or "").rstrip("/")
    root = f"{u.scheme}://{u.netloc}"
    out = [f"{root}/.well-known/{name}{path}"] if path else []
    out.append(f"{root}/.well-known/{name}")
    return out


async def probe(url: str, headers: Optional[dict] = None) -> dict:
    """POST an MCP initialize without a token. Returns {needs_auth, www_authenticate} or {needs_auth: False}."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "ai-workstation", "version": "3.9.27"}}}
    async with http() as c:
        r = await c.post(url, json=body, headers={"Accept": "application/json, text/event-stream", **(headers or {})})
    if r.status_code in (401, 403):
        return {"needs_auth": True, "www_authenticate": r.headers.get("www-authenticate", ""), "status": r.status_code}
    return {"needs_auth": False, "status": r.status_code}


async def discover(mcp_url: str, www_authenticate: str = "") -> dict:
    """-> {resource, prm, issuer, as_meta, scopes}. Raises OAuthError when no authorization server is found."""
    wa = parse_www_authenticate(www_authenticate)
    prm: dict = {}
    async with http() as c:
        cands = ([wa["resource_metadata"]] if wa.get("resource_metadata") else []) + _wk(mcp_url, "oauth-protected-resource")
        for u in cands:
            try:
                r = await c.get(u)
                if r.status_code == 200 and isinstance(r.json(), dict):
                    prm = r.json()
                    break
            except (httpx.HTTPError, ValueError):
                continue
        servers = prm.get("authorization_servers") or []
        if not servers:   # 2025-03-26 servers: the MCP origin is its own authorization server
            p = urlparse(mcp_url)
            servers = [f"{p.scheme}://{p.netloc}"]
        meta, issuer = {}, ""
        for asu in servers:
            for u in _wk(asu, "oauth-authorization-server") + _wk(asu, "openid-configuration"):
                try:
                    r = await c.get(u)
                    if r.status_code == 200 and r.json().get("authorization_endpoint"):
                        meta, issuer = r.json(), r.json().get("issuer") or asu
                        break
                except (httpx.HTTPError, ValueError):
                    continue
            if meta:
                break
    if not meta:
        raise OAuthError("找不到授权服务器（没有 oauth-authorization-server / openid-configuration）")
    for k in ("authorization_endpoint", "token_endpoint"):
        if not _safe_url(meta.get(k, "")):
            raise OAuthError(f"授权服务器的 {k} 不是 https 地址")
    res = prm.get("resource") if prm.get("resource") and _safe_url(prm["resource"]) else canonical_resource(mcp_url)
    scopes = (wa.get("scope") or "").split() or list(prm.get("scopes_supported") or [])
    return {"resource": res, "prm": prm, "issuer": issuer, "as_meta": meta, "scopes": scopes}


async def register_client(as_meta: dict, redirect: str, name: str = "AI Workstation") -> dict:
    """RFC 7591 dynamic registration as a public client (token_endpoint_auth_method none + PKCE)."""
    ep = as_meta.get("registration_endpoint")
    if not ep:
        raise OAuthError("该服务不支持动态注册，请填写你自己注册的 client_id / client_secret")
    loop = (urlparse(redirect).hostname or "") in ("127.0.0.1", "localhost")
    body = {"client_name": name, "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
            "application_type": "native" if loop else "web"}
    async with http() as c:
        r = await c.post(ep, json=body)
    if r.status_code >= 300:
        raise OAuthError(f"动态注册失败：HTTP {r.status_code} {r.text[:200]}")
    d = r.json()
    if not d.get("client_id"):
        raise OAuthError("动态注册没有返回 client_id")
    return {"client_id": d["client_id"], "client_secret": d.get("client_secret") or "",
            "registered_redirect": redirect, "dcr": True}


# ------------------------------------------------------------------ authorize / token / revoke
def authorize_url(endpoint: str, client_id: str, redirect: str, state: str, challenge: str,
                  scopes: list[str] | None = None, resource: str = "", extra: dict | None = None,
                  offline_ok: bool = False) -> str:
    sc = list(scopes or [])
    if offline_ok and sc and "offline_access" not in sc:
        sc.append("offline_access")
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    if sc:
        q["scope"] = " ".join(sc)
    if resource:
        q["resource"] = resource
    q.update({k: v for k, v in (extra or {}).items() if v is not None})
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(q)


def _norm_tokens(d: dict, old: Optional[dict] = None) -> dict:
    now = time.time()
    tok = {"access_token": d.get("access_token") or (d.get("authed_user") or {}).get("access_token") or "",
           "token_type": d.get("token_type") or "Bearer",
           "refresh_token": d.get("refresh_token") or (old or {}).get("refresh_token") or "",
           "scope": d.get("scope") or (old or {}).get("scope") or "",
           "obtained_at": now}
    if d.get("expires_in"):
        try:
            tok["expires_at"] = now + float(d["expires_in"])
        except (TypeError, ValueError):
            pass
    if d.get("id_token"):
        tok["id_token"] = d["id_token"]
    if not tok["access_token"]:
        raise OAuthError(f"token 响应里没有 access_token：{str(d)[:200]}")
    return tok


async def _token_post(endpoint: str, form: dict, client: dict) -> dict:
    form = {k: v for k, v in form.items() if v not in (None, "")}
    auth = None
    if client.get("client_secret") and client.get("auth_method", "client_secret_post") == "client_secret_basic":
        auth = (client["client_id"], client["client_secret"])
    else:
        form["client_id"] = client["client_id"]
        if client.get("client_secret"):
            form["client_secret"] = client["client_secret"]
    async with http() as c:
        r = await c.post(endpoint, data=form, auth=auth, headers={"Accept": "application/json"})
    try:
        d = r.json()
    except ValueError:
        from urllib.parse import parse_qsl   # GitHub without Accept: json answers form-encoded
        d = dict(parse_qsl(r.text))
    if r.status_code >= 400 or d.get("error") or d.get("ok") is False:
        err = d.get("error") or f"HTTP {r.status_code}"
        if err in ("invalid_grant", "invalid_token", "unauthorized_client", "invalid_client"):
            raise ReauthRequired(f"{err}: {d.get('error_description') or ''}"[:300])
        raise OAuthError(f"{err}: {d.get('error_description') or r.text[:200]}")
    return d


async def exchange_code(endpoint: str, client: dict, code: str, verifier: str, redirect: str,
                        resource: str = "") -> dict:
    d = await _token_post(endpoint, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                     "code_verifier": verifier, "resource": resource}, client)
    return _norm_tokens(d)


async def refresh(endpoint: str, client: dict, tokens: dict, resource: str = "") -> dict:
    if not tokens.get("refresh_token"):
        raise ReauthRequired("没有 refresh token")
    d = await _token_post(endpoint, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                     "resource": resource}, client)
    return _norm_tokens(d, tokens)


async def revoke(endpoint: str, client: dict, tokens: dict) -> bool:
    """RFC 7009 best effort: refresh token first (revokes the whole grant at most servers)."""
    if not endpoint:
        return False
    ok = False
    for hint in ("refresh_token", "access_token"):
        t = tokens.get(hint)
        if not t:
            continue
        try:
            form = {"token": t, "token_type_hint": hint, "client_id": client.get("client_id")}
            if client.get("client_secret"):
                form["client_secret"] = client["client_secret"]
            async with http(10) as c:
                r = await c.post(endpoint, data=form)
            ok = ok or r.status_code < 400
        except httpx.HTTPError:
            pass
    return ok


def expired(tokens: dict, skew: float = TOKEN_SKEW) -> bool:
    exp = tokens.get("expires_at")
    return bool(exp) and time.time() >= float(exp) - skew


def mask_url(u: str) -> str:
    return re.sub(r"([?&](?:key|token|api_?key|access_token)[^=]*=)[^&]+", r"\1***", u or "", flags=re.I)


__all__ = ["OAuthError", "ReauthRequired", "StateStore", "pkce_pair", "pkce_challenge", "redirect_uri", "public_url",
           "parse_www_authenticate", "canonical_resource", "probe", "discover", "register_client", "authorize_url",
           "exchange_code", "refresh", "revoke", "expired", "http", "TOKEN_SKEW", "Any"]
