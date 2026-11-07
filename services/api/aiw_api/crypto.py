"""Credential storage: secrets saved from the web console are encrypted at rest
(key in AIW_SECRET_KEY or an auto-created 0600 key file next to the DB) and the API only ever returns masked previews.

v3.9.27: new values are written as AES-256-GCM records "v2:<b64(nonce|ciphertext|tag)>" (12-byte random nonce,
the secret's name as associated data, key = SHA-256 over a domain tag + the same key material). Values written by
older versions (Fernet = AES-128-CBC + HMAC) still decrypt; they are re-encrypted as v2 the next time they are saved.
OAuth tokens and client registrations (oauth/connections.py) live in this box too, never in kv or in model context."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - cryptography is in requirements.txt
    Fernet = None  # type: ignore
    AESGCM = None  # type: ignore
    InvalidToken = Exception  # type: ignore

V2 = "v2:"


class SecretBox:
    def __init__(self, store, key_dir: str):
        self.store = store
        raw = os.environ.get("AIW_SECRET_KEY")
        if raw:
            key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        else:
            kf = Path(key_dir) / ".secret_key"
            if not kf.exists():
                kf.parent.mkdir(parents=True, exist_ok=True)
                kf.write_bytes(Fernet.generate_key() if Fernet else base64.urlsafe_b64encode(os.urandom(32)))
                os.chmod(kf, 0o600)
            key = kf.read_bytes().strip()
        self.f = Fernet(key) if Fernet else None   # legacy records (read only)
        # v3.9.27: AES-256-GCM key from the same material, so no new key file and no migration step
        self.aes = AESGCM(hashlib.sha256(b"aiw-secretbox-v2|" + key).digest()) if AESGCM else None

    def encrypt(self, name: str, value: str) -> str:
        if not self.aes:
            raise RuntimeError("cryptography not installed; cannot store secrets")
        nonce = os.urandom(12)
        return V2 + base64.urlsafe_b64encode(nonce + self.aes.encrypt(nonce, value.encode(), name.encode())).decode()

    def decrypt(self, name: str, enc: str) -> Optional[str]:
        if not enc:
            return None
        if enc.startswith(V2):
            if not self.aes:
                return None
            try:
                raw = base64.urlsafe_b64decode(enc[len(V2):].encode())
                return self.aes.decrypt(raw[:12], raw[12:], name.encode()).decode()
            except Exception:  # noqa: BLE001 - wrong key / tampered / renamed record
                return None
        if not self.f:
            return None
        try:   # pre-3.9.27 Fernet record
            return self.f.decrypt(enc.encode()).decode()
        except InvalidToken:
            return None

    def put(self, name: str, value: str):
        self.store.secret_put(name, self.encrypt(name, value))

    def get(self, name: str) -> Optional[str]:
        return self.decrypt(name, self.store.secret_get(name) or "")

    def get_json(self, name: str) -> Optional[dict]:
        """v3.9.27: structured secrets (OAuth token sets, client registrations)."""
        import json
        v = self.get(name)
        try:
            return json.loads(v) if v else None
        except ValueError:
            return None

    def put_json(self, name: str, value: dict):
        import json
        self.put(name, json.dumps(value, ensure_ascii=False))

    def list(self) -> list[dict]:
        out = []
        for r in self.store.secret_names():
            if r["name"].startswith("oauth_"):   # v3.9.27: token sets / client registrations are not user secrets
                continue
            v = self.get(r["name"]) or ""
            out.append({"name": r["name"], "preview": mask(v), "updated_at": r["updated_at"]})
        return out

    def delete(self, name: str):
        self.store.secret_delete(name)

    def expand(self, text: str) -> str:
        """Replace {{secret:NAME}} placeholders (used in integration headers/urls)."""
        import re
        return re.sub(r"\{\{secret:([A-Za-z0-9_\-]+)\}\}", lambda m: self.get(m.group(1)) or "", text or "")


def mask(v: str) -> str:
    if not v:
        return ""
    return ("•" * 6) + v[-4:] if len(v) > 8 else "•" * len(v)
