"""v3.9.33: model provider list (设置 › 模型 › 供应商).

kv "providers" = [{id, name, kind, base_url, proxy, enabled}] - extra OpenAI-compatible endpoints. Each one's API key is
the encrypted secret "provider_key_<id>" (never returned by the API; GET only says has_key). The old global settings
are the implicit provider "default" (名称「默认」): models.base_url (else OPENAI_BASE_URL) + secret openai_api_key (else
OPENAI_API_KEY). Nothing is migrated or rewritten: with no providers and no *_provider fields every call resolves to
exactly the old base_url + key.

Scheme (one only): every model slot in kv "models" has an optional sibling field "<slot>_provider" holding a provider
id; empty / "default" / unknown / disabled = the default provider. Slots: model, fallback, vision_model,
vision_fallback, video_model, subagent_model, chat_model, small_model, embed_model, summary_model, geometry_model,
image_model, image2_model, omni_model (turn mode; also the realtime key/url when omni_realtime_key is empty),
omni_tts_model, omni_gemini (the Gemini Live key when omni_gemini_key is empty). Model values stay plain model names.
A model name that is not in a slot (an auto-routing tier or autoroute.dev.model) gets the provider of the slot that
holds the same name, else of a provider whose cached /models list has it (only when the default's list does not), else
the default; autoroute.dev.provider can name one explicitly.

kind: only "openai" (OpenAI-compatible chat/completions + /models). llm_router's GoogleProvider is a 37-line single
tool-call client without streaming, thinking or vision fallbacks, so Gemini is added as an OpenAI-compatible provider
(https://generativelanguage.googleapis.com/v1beta/openai/).
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

KV = "providers"
MODELS_KV = "provider_models"     # {provider id: [model ids]} cached by GET /api/models
DEFAULT_ID = "default"
DEFAULT_NAME = "默认"
OPENAI_DEFAULT = "https://api.openai.com/v1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PROXY_RE = re.compile(r"^(https?|socks5h?|socks4a?)://[^\s/]+")
KINDS = ("openai",)

SLOTS = ("model", "fallback", "vision_model", "vision_fallback", "video_model", "subagent_model", "chat_model",
         "small_model", "embed_model", "summary_model", "geometry_model", "image_model", "image2_model", "omni_model",
         "omni_tts_model", "omni_gemini")
PROVIDER_FIELDS = tuple(s + "_provider" for s in SLOTS)

# 快速添加 (verified 2026-11-09: Bailian Base URL 总览, aistudio.google.com/docs/openai, api-docs.deepseek.com)
PRESETS = [
    {"id": "bailian", "name": "百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "hint": "北京地域共享域名；也可用业务空间专属域名 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"},
    {"id": "gemini", "name": "Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "hint": "OpenAI 兼容接口，国内需代理；Key 在 aistudio.google.com/api-keys 创建"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "hint": "国内需代理"},
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com", "hint": ""},
]


def secret_name(pid: str) -> str:
    return f"provider_key_{pid}"


def stored(store) -> list[dict]:
    v = store.kv_get(KV, []) or []
    return [p for p in v if isinstance(p, dict) and p.get("id")]


def _models(store) -> dict:
    return store.kv_get("models", {}) or {}


def default_conn(store, secrets, m: Optional[dict] = None) -> dict:
    m = m if m is not None else _models(store)
    raw = m.get("base_url") or None
    return {"id": DEFAULT_ID, "name": DEFAULT_NAME, "kind": "openai",
            # raw_base None = let OpenAIProvider fall back to OPENAI_BASE_URL exactly as before
            "raw_base": raw, "base_url": (raw or os.environ.get("OPENAI_BASE_URL") or OPENAI_DEFAULT).rstrip("/"),
            "key": (secrets.get("openai_api_key") if secrets else None) or os.environ.get("OPENAI_API_KEY") or "",
            "proxy": "", "enabled": True, "default": True}


def find(store, pid: str) -> Optional[dict]:
    return next((p for p in stored(store) if p.get("id") == pid), None)


def conn(store, secrets, pid: Optional[str], m: Optional[dict] = None) -> dict:
    """base_url / key / proxy of a provider; empty, "default", unknown or disabled ids give the default provider
    (with "fell_back" naming the id that was asked for)."""
    pid = (pid or "").strip()
    if not pid or pid == DEFAULT_ID:
        return default_conn(store, secrets, m)
    p = find(store, pid)
    if not p or p.get("enabled") is False:
        return {**default_conn(store, secrets, m), "fell_back": pid}
    base = str(p.get("base_url") or "").strip().rstrip("/")
    return {"id": p["id"], "name": p.get("name") or p["id"], "kind": p.get("kind") or "openai", "raw_base": base or None,
            "base_url": base, "key": (secrets.get(secret_name(p["id"])) if secrets else None) or "",
            "proxy": str(p.get("proxy") or "").strip(), "enabled": True, "default": False}


def slot_conn(store, secrets, slot: str, m: Optional[dict] = None) -> dict:
    m = m if m is not None else _models(store)
    return conn(store, secrets, m.get(slot + "_provider"), m)


def provider_for_model(store, name: str, m: Optional[dict] = None, prefer: str = "") -> str:
    """Which provider id a bare model name goes to (see the module docstring)."""
    m = m if m is not None else _models(store)
    if not name:
        return ""
    if prefer and m.get(prefer) == name:
        return m.get(prefer + "_provider") or ""
    for s in SLOTS:
        if m.get(s) == name and m.get(s + "_provider"):
            return m[s + "_provider"]
    cache = store.kv_get(MODELS_KV, {}) or {}
    if name in (cache.get(DEFAULT_ID) or []):
        return ""
    live = {p["id"] for p in stored(store) if p.get("enabled") is not False}
    for pid, ids in cache.items():
        if pid in live and name in (ids or []):
            return pid
    return ""


def model_conn(store, secrets, name: str, m: Optional[dict] = None, prefer: str = "", pid: str = "") -> dict:
    m = m if m is not None else _models(store)
    return conn(store, secrets, pid or provider_for_model(store, name, m, prefer), m)


def router(store, secrets, models: list, tool_choice: Optional[dict] = None, thinking: Optional[str] = None,
           effort: Optional[str] = None, role: str = "", m: Optional[dict] = None):
    """LLMRouter over [(model name, slot or "" , provider id or "")...] - each model on its own provider. With only
    the default provider this builds exactly what LLMRouter.from_settings built (same keys, base_url, env fallbacks)."""
    from llm_router import LLMRouter
    from llm_router.providers.openai import OpenAIProvider
    m = m if m is not None else _models(store)
    tc = tool_choice or {}
    ps = []
    seen = set()
    for i, item in enumerate(models):
        name, slot, pid = (tuple(item) + ("", ""))[:3] if isinstance(item, (tuple, list)) else (item, "", "")
        if not name or name in seen:
            continue
        seen.add(name)
        c = model_conn(store, secrets, name, m, slot, pid)
        key = c["key"] or (os.environ.get("OPENAI_API_KEY") if c["default"] else "")
        if not key:
            continue
        kw: dict[str, Any] = {"model": name, "api_key": key, "base_url": c["raw_base"], "tool_choice": tc.get(name),
                              "thinking": thinking}
        if c.get("proxy"):
            kw["proxy"] = c["proxy"]
        if effort and not ps and i == 0:
            kw["effort"] = effort
        ps.append(OpenAIProvider(**kw))
    if not ps:
        return LLMRouter.from_env()
    r = LLMRouter(ps)
    r.role = role
    return r


def public(store, secrets) -> dict:
    """GET /api/providers: the list with has_key instead of keys, the implicit default first."""
    d = default_conn(store, secrets)
    out = [{"id": DEFAULT_ID, "name": DEFAULT_NAME, "kind": "openai", "base_url": d["base_url"], "proxy": "",
            "enabled": True, "default": True, "has_key": bool(d["key"])}]
    for p in stored(store):
        out.append({"id": p["id"], "name": p.get("name") or p["id"], "kind": p.get("kind") or "openai",
                    "base_url": p.get("base_url") or "", "proxy": p.get("proxy") or "",
                    "enabled": p.get("enabled") is not False, "default": False,
                    "has_key": bool(secrets.get(secret_name(p["id"])) if secrets else False)})
    return {"providers": out, "presets": PRESETS, "slots": list(SLOTS)}


def used_by(store, pid: str) -> list[str]:
    """Settings that point at a provider (model slots and autoroute.dev.provider)."""
    m = _models(store)
    out = [f for f in PROVIDER_FIELDS if m.get(f) == pid]
    ar = store.kv_get("autoroute", {}) or {}
    if ((ar.get("dev") or {}).get("provider") or "") == pid:
        out.append("autoroute.dev.provider")
    return out


def validate(body: dict, existing: Optional[dict] = None) -> dict:
    """A provider record from a request body (raises ValueError with a Chinese message)."""
    p = dict(existing or {})
    if existing is None:
        pid = str(body.get("id") or "").strip().lower()
        if not pid:
            base = re.sub(r"[^a-z0-9]+", "-", str(body.get("name") or "").lower()).strip("-")
            pid = base[:24] or "p"
        if pid == DEFAULT_ID or not ID_RE.match(pid):
            raise ValueError("供应商 id 只能用小写字母、数字、- 和 _（不能叫 default）")
        p["id"] = pid
    for k in ("name", "base_url", "proxy", "kind"):
        if k in body:
            p[k] = str(body.get(k) or "").strip()
    if "enabled" in body:
        p["enabled"] = bool(body["enabled"])
    p.setdefault("enabled", True)
    p["kind"] = p.get("kind") or "openai"
    if p["kind"] not in KINDS:
        raise ValueError("类型只支持 openai（OpenAI 兼容）；Gemini 用它的 OpenAI 兼容地址")
    if not p.get("name"):
        p["name"] = p["id"]
    b = (p.get("base_url") or "").rstrip("/")
    if not re.match(r"^https?://[^\s/]+", b):
        raise ValueError("地址要以 http:// 或 https:// 开头，如 https://dashscope.aliyuncs.com/compatible-mode/v1")
    p["base_url"] = b
    if p.get("proxy") and not PROXY_RE.match(p["proxy"]):
        raise ValueError("代理格式：http://host:port 或 socks5://host:port")
    p["name"] = p["name"][:40]
    return p


def httpx_kw(c: dict) -> dict:
    return {"proxy": c["proxy"]} if c.get("proxy") else {}


async def list_models(c: dict, transport=None) -> list[str]:
    import httpx
    async with httpx.AsyncClient(timeout=15, transport=transport, **({} if transport else httpx_kw(c))) as cl:
        r = await cl.get(f"{c['base_url']}/models", headers={"Authorization": f"Bearer {c['key']}"})
        r.raise_for_status()
    ids = []
    for x in r.json().get("data", []) or []:
        i = str(x.get("id") or "")
        if i.startswith("models/"):   # Gemini's OpenAI-compatible /models lists "models/<name>"
            i = i[7:]
        if i:
            ids.append(i)
    return sorted(set(ids))


def mount(app, store, secrets, auth) -> None:
    from fastapi import Depends, HTTPException

    @app.get("/api/providers", dependencies=[Depends(auth)])
    async def get_providers():
        return public(store, secrets)

    @app.post("/api/providers", dependencies=[Depends(auth)])
    async def add_provider(body: dict):
        try:
            p = validate(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        lst = stored(store)
        if any(x["id"] == p["id"] for x in lst):
            raise HTTPException(409, f"已经有 id 为 {p['id']} 的供应商")
        lst.append(p)
        store.kv_set(KV, lst)
        if body.get("key"):
            secrets.put(secret_name(p["id"]), str(body["key"]).strip())
        return {**{k: v for k, v in p.items()}, "has_key": bool(secrets.get(secret_name(p["id"])))}

    @app.put("/api/providers/{pid}", dependencies=[Depends(auth)])
    async def edit_provider(pid: str, body: dict):
        if pid == DEFAULT_ID:
            raise HTTPException(400, "「默认」供应商在 设置 › 模型 › 地址 / API Key 里改")
        lst = stored(store)
        i = next((n for n, x in enumerate(lst) if x["id"] == pid), None)
        if i is None:
            raise HTTPException(404, "没有这个供应商")
        try:
            lst[i] = validate({k: v for k, v in body.items() if k != "id"}, lst[i])
        except ValueError as e:
            raise HTTPException(400, str(e))
        if lst[i].get("enabled") is False and used_by(store, pid):
            raise HTTPException(409, "这个供应商还在用：" + "、".join(used_by(store, pid)) + "。先把这些改到别的供应商再停用")
        store.kv_set(KV, lst)
        if body.get("key"):
            secrets.put(secret_name(pid), str(body["key"]).strip())
        return {**lst[i], "has_key": bool(secrets.get(secret_name(pid)))}

    @app.delete("/api/providers/{pid}", dependencies=[Depends(auth)])
    async def del_provider(pid: str, force: bool = False):
        """In use: 409 with the list of settings, unless force=1, which points them back at the default provider."""
        if pid == DEFAULT_ID:
            raise HTTPException(400, "「默认」供应商不能删除")
        lst = stored(store)
        if not any(x["id"] == pid for x in lst):
            raise HTTPException(404, "没有这个供应商")
        users = used_by(store, pid)
        if users and not force:
            raise HTTPException(409, "这个供应商还在用：" + "、".join(users) + "。先改到别的供应商，或强制删除（这些设置回到「默认」）")
        if users:
            m = dict(_models(store))
            for f in users:
                if f in m:
                    m[f] = None
            store.kv_set("models", m)
            ar = store.kv_get("autoroute", {}) or {}
            if ((ar.get("dev") or {}).get("provider") or "") == pid:
                ar["dev"]["provider"] = ""
                store.kv_set("autoroute", ar)
        store.kv_set(KV, [x for x in lst if x["id"] != pid])
        secrets.delete(secret_name(pid))
        cache = store.kv_get(MODELS_KV, {}) or {}
        cache.pop(pid, None)
        store.kv_set(MODELS_KV, cache)
        return {"ok": True, "reset": users}

    @app.post("/api/providers/{pid}/test", dependencies=[Depends(auth)])
    async def test_provider(pid: str, body: Optional[dict] = None):
        """测试连接: GET <base>/models with its key (and proxy); body may carry unsaved base_url / key / proxy."""
        import time
        body = body or {}
        if pid != DEFAULT_ID and not find(store, pid) and not body.get("base_url"):
            raise HTTPException(404, "没有这个供应商")
        known = pid == DEFAULT_ID or find(store, pid) is not None
        # an unsaved provider never borrows the default's key
        c = dict(conn(store, secrets, pid)) if known else {"id": pid, "base_url": "", "key": "", "proxy": ""}
        for k in ("base_url", "key", "proxy"):
            if body.get(k):
                c[k] = str(body[k]).strip().rstrip("/") if k == "base_url" else str(body[k]).strip()
        if not c.get("key"):
            return {"ok": False, "error": "没有 API Key"}
        t0 = time.time()
        try:
            ids = await list_models(c, TRANSPORT)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "ms": int((time.time() - t0) * 1000)}
        return {"ok": True, "count": len(ids), "sample": ids[:8], "ms": int((time.time() - t0) * 1000)}


TRANSPORT = None   # tests: httpx.MockTransport for /models calls
