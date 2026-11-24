"""Encrypted credential store for cloud engines (Cue by Manus).

Stores credentials inside `SecretBox` (AES-256-GCM at rest), never exposing raw tokens/cookies back to the UI.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

CLOUD_BOTS = ("cue",)


def _secret_key(bot: str) -> str:
    return f"oauth_bot_{bot}"   # prefix oauth_ keeps it out of generic SecretBox.list()


def _mask(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "••••"
    return s[:4] + "••••" + s[-4:]


class CloudAuthStore:
    def __init__(self, store, secrets=None):
        self.store = store
        self.secrets = secrets

    def load(self, bot: str) -> dict[str, Any]:
        if bot not in CLOUD_BOTS:
            return {}
        key = _secret_key(bot)
        data = None
        if self.secrets is not None:
            data = self.secrets.get_json(key)
        if data is None and self.store is not None:
            data = self.store.kv_get(key, None)
        return dict(data) if isinstance(data, dict) else {}

    def save(self, bot: str, patch: dict[str, Any], replace: bool = False) -> dict[str, Any]:
        if bot not in CLOUD_BOTS:
            raise ValueError(f"unsupported cloud bot: {bot}")
        cur = {} if replace else self.load(bot)
        for k, v in (patch or {}).items():
            if v is None:
                cur.pop(k, None)
            elif isinstance(v, bool):
                cur[k] = v
            elif isinstance(v, str):
                s = v.strip()
                if s:
                    cur[k] = s
                elif replace or k in ("proxy_url", "agent_id", "base_url", "mode"):
                    cur.pop(k, None)
            else:
                cur[k] = v
        if bot == "cue" and not cur.get("device_id"):
            cur["device_id"] = str(uuid.uuid4())
        cur["updated_at"] = time.time()
        key = _secret_key(bot)
        if self.secrets is not None:
            self.secrets.put_json(key, cur)
        elif self.store is not None:
            self.store.kv_set(key, cur)
        return cur

    def clear(self, bot: str) -> None:
        if bot not in CLOUD_BOTS:
            return
        key = _secret_key(bot)
        if self.secrets is not None:
            self.secrets.delete(key)
        if self.store is not None:
            try:
                self.store.kv_set(key, None)
            except Exception:  # noqa: BLE001
                pass

    def public_view(self, bot: str) -> dict[str, Any]:
        if bot not in CLOUD_BOTS:
            return {"bot": bot, "configured": False}
        d = self.load(bot)
        has_key = bool(d.get("api_key"))
        has_token = bool(d.get("access_token"))
        has_cookie = bool(d.get("cookie_header"))
        exp_native = bool(d.get("experimental_native"))
        mode = d.get("mode") or ("native" if (exp_native and not has_key and (has_token or has_cookie)) else "openapi")
        configured = has_key or (exp_native and (has_token or has_cookie))
        return {
            "bot": "cue",
            "configured": configured,
            "mode": mode,
            "experimental_native": exp_native,
            "has_api_key": has_key,
            "api_key_preview": _mask(d.get("api_key", "")),
            "has_access_token": has_token,
            "access_token_preview": _mask(d.get("access_token", "")),
            "has_cookie": has_cookie,
            "agent_id": d.get("agent_id") or "",
            "proxy_url": d.get("proxy_url") or "",
            "base_url": d.get("base_url") or "",
            "handle": d.get("handle") or "",
            "display_name": d.get("display_name") or "",
            "source": d.get("source") or "",
            "updated_at": d.get("updated_at") or 0,
        }
