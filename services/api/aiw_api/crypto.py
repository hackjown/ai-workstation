"""Credential storage: secrets saved from the web console are encrypted at rest
(Fernet, key in AIW_SECRET_KEY or an auto-created 0600 key file next to the DB)
and the API only ever returns masked previews."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - cryptography is in requirements.txt
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore


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
        self.f = Fernet(key) if Fernet else None

    def put(self, name: str, value: str):
        if not self.f:
            raise RuntimeError("cryptography not installed; cannot store secrets")
        self.store.secret_put(name, self.f.encrypt(value.encode()).decode())

    def get(self, name: str) -> Optional[str]:
        enc = self.store.secret_get(name)
        if not enc or not self.f:
            return None
        try:
            return self.f.decrypt(enc.encode()).decode()
        except InvalidToken:
            return None

    def list(self) -> list[dict]:
        out = []
        for r in self.store.secret_names():
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
