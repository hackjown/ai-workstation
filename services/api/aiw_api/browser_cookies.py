"""Read Cue (cue.im / manus.im) cookies from local or connected-computer browsers (explicit user action only).

Supports two extraction methods in priority order:
  1. Chromium DevTools Protocol (CDP) on local debugging ports (BrowserOS / Chrome / Edge) via `Storage.getCookies`.
  2. Direct read of Chromium-family SQLite `Cookies` databases (Chrome, Chrome Canary, Chromium, Arc, Brave, Edge,
     BrowserOS) on macOS (Keychain Safe Storage + AES-128-CBC) and Linux (peanuts / plaintext).

When `aiw-api` runs in Docker and the user's browser lives on a connected computer (`machine_id`), `read_remote_cookies`
runs the same lightweight extractor via `shell.exec` on that computer.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("aiw.browser_cookies")

CUE_DOMAINS = ("cue.im", "manus.im", "manus.ai")
CUE_SESSION_COOKIE_NAMES = ("cue_session_id", "session_id", "access_token", "manus_session", "auth_token")

# (label, macOS app-support relative path, macOS Keychain service name, Linux ~/.config relative path)
CHROMIUM_BROWSERS = (
    ("Chrome", "Google/Chrome", "Chrome Safe Storage", "google-chrome"),
    ("Chrome Canary", "Google/Chrome Canary", "Chrome Safe Storage", "google-chrome-unstable"),
    ("Arc", "Arc/User Data", "Arc Safe Storage", ""),
    ("Brave", "BraveSoftware/Brave-Browser", "Brave Safe Storage", "BraveSoftware/Brave-Browser"),
    ("Edge", "Microsoft Edge", "Microsoft Edge Safe Storage", "microsoft-edge"),
    ("Chromium", "Chromium", "Chromium Safe Storage", "chromium"),
    ("BrowserOS", "BrowserOS", "BrowserOS Safe Storage", "browseros"),
)

DEFAULT_CDP_PORTS = (9222, 9223, 9229, 9333)


def _domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    h = (host or "").strip().lower().lstrip(".")
    if not h:
        return False
    for d in domains:
        dl = d.lower().lstrip(".")
        if h == dl or h.endswith("." + dl):
            return True
    return False


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of JWT payload without signature verification (display/expiry metadata only)."""
    try:
        parts = (token or "").strip().split(".")
        if len(parts) < 2 or not parts[1]:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            return {}
        out: dict[str, Any] = {}
        email = payload.get("email") or payload.get("preferred_username") or payload.get("sub")
        if isinstance(email, str) and email.strip():
            out["handle"] = email.strip()
        name = payload.get("name") or payload.get("nickname")
        if isinstance(name, str) and name.strip():
            out["display_name"] = name.strip()
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            out["expires_at_ms"] = int(exp * 1000) if exp < 1e12 else int(exp)
        return out
    except Exception:  # noqa: BLE001
        return {}


def format_cookie_header(cookies: list[dict[str, Any]], domains: Optional[tuple[str, ...]] = None) -> str:
    """Build a deduplicated `name=value; ...` Cookie header from cookie dicts."""
    seen: dict[str, str] = {}
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "").strip()
        dom = str(c.get("domain") or "").strip()
        if not name or not val:
            continue
        if domains and dom and not _domain_matches(dom, domains):
            continue
        # Prefer more specific / newer non-empty cookie
        seen[name] = val
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def extract_cue_credentials_from_cookies(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract access_token, cookie_header, handle and display_name for Cue from raw browser cookies."""
    matched = [c for c in cookies if isinstance(c, dict) and _domain_matches(str(c.get("domain") or ""), CUE_DOMAINS)]
    if not matched:
        return {}
    by_name: dict[str, dict[str, Any]] = {}
    for c in matched:
        n = str(c.get("name") or "").strip()
        v = str(c.get("value") or "").strip()
        if not n or not v:
            continue
        # Prefer cue.im over manus.im for cue_session_id, otherwise keep first/latest non-empty
        if n not in by_name or "cue.im" in str(c.get("domain") or "").lower():
            by_name[n] = c

    access_token = ""
    for pref in CUE_SESSION_COOKIE_NAMES:
        if pref in by_name:
            access_token = str(by_name[pref].get("value") or "").strip()
            break
    if not access_token:
        for n, c in by_name.items():
            low = n.lower()
            if "session" in low or "token" in low or "jwt" in low or "auth" in low:
                access_token = str(c.get("value") or "").strip()
                break

    claims = decode_jwt_claims(access_token) if access_token else {}
    cookie_header = format_cookie_header(matched, CUE_DOMAINS)
    out: dict[str, Any] = {
        "cookie_header": cookie_header,
        "cookie_count": len(by_name),
        "cookie_names": sorted(by_name.keys()),
    }
    if access_token:
        out["access_token"] = access_token
    if claims.get("handle"):
        out["handle"] = claims["handle"]
    if claims.get("display_name"):
        out["display_name"] = claims["display_name"]
    if claims.get("expires_at_ms"):
        out["expires_at_ms"] = claims["expires_at_ms"]
    return out


# --------------------------------------------------------------------------------------------- CDP reader
async def _read_cdp_port(port: int, domains: tuple[str, ...]) -> list[dict[str, Any]]:
    import httpx
    import websockets

    ws_url = ""
    async with httpx.AsyncClient(timeout=2.5) as client:
        r = await client.get(f"http://127.0.0.1:{port}/json/version")
        if r.status_code != 200:
            return []
        ws_url = str((r.json() or {}).get("webSocketDebuggerUrl") or "")
    if not ws_url:
        return []
    async with websockets.connect(ws_url, open_timeout=3, close_timeout=2, max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        for _ in range(20):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("id") == 1:
                all_cookies = (msg.get("result") or {}).get("cookies") or []
                now = time.time()
                out = []
                for c in all_cookies:
                    if not isinstance(c, dict):
                        continue
                    exp = c.get("expires")
                    if isinstance(exp, (int, float)) and exp > 0 and exp < now:
                        continue
                    if _domain_matches(str(c.get("domain") or ""), domains):
                        out.append({
                            "name": str(c.get("name") or ""),
                            "value": str(c.get("value") or ""),
                            "domain": str(c.get("domain") or ""),
                            "path": str(c.get("path") or "/"),
                        })
                return out
    return []


async def read_local_cdp_cookies(domains: tuple[str, ...]) -> tuple[list[dict[str, Any]], str]:
    env_ports = os.environ.get("AIW_CDP_PORTS", "")
    ports: list[int] = []
    for p in env_ports.split(","):
        p = p.strip()
        if p.isdigit():
            ports.append(int(p))
    for p in DEFAULT_CDP_PORTS:
        if p not in ports:
            ports.append(p)
    for port in ports:
        try:
            cookies = await _read_cdp_port(port, domains)
            if cookies:
                return cookies, f"cdp:{port}"
        except Exception:  # noqa: BLE001
            continue
    return [], ""


# --------------------------------------------------------------------------------------------- SQLite reader
def _derive_chromium_key(password: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, dklen=16)


def _macos_keychain_password(service: str) -> Optional[bytes]:
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", service],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        pw = out.strip()
        return pw if pw else None
    except Exception:  # noqa: BLE001
        return None


def _decrypt_chromium_value(enc: bytes, key: Optional[bytes], db_version: int) -> str:
    if not enc:
        return ""
    if not key:
        return ""
    if enc.startswith((b"v10", b"v11")):
        body = enc[3:]
    else:
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
        dec = cipher.decryptor()
        padded = dec.update(body) + dec.finalize()
        unpadder = PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        # Chromium DB version >= 24 prepends SHA256(host_key) (32 bytes) before the cookie value
        if db_version >= 24 and len(plain) >= 32:
            tail = plain[32:]
            try:
                return tail.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return plain.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def _chromium_cookie_files() -> list[tuple[str, Path, str]]:
    """Return list of (browser_label, cookies_db_path, keychain_service)."""
    sys_name = platform.system().lower()
    home = Path.home()
    found: list[tuple[str, Path, str]] = []
    for label, mac_rel, kc_svc, linux_rel in CHROMIUM_BROWSERS:
        if sys_name == "darwin":
            base = home / "Library" / "Application Support" / mac_rel
        elif sys_name == "linux" and linux_rel:
            base = home / ".config" / linux_rel
        else:
            continue
        if not base.exists():
            continue
        candidates = [
            base / "Default" / "Cookies",
            base / "Default" / "Network" / "Cookies",
        ]
        for prof in sorted(base.glob("Profile *")):
            candidates.append(prof / "Cookies")
            candidates.append(prof / "Network" / "Cookies")
        for p in candidates:
            if p.is_file():
                found.append((label, p, kc_svc))
    return found


def read_local_sqlite_cookies(domains: tuple[str, ...]) -> tuple[list[dict[str, Any]], str]:
    """Read matching cookies from local Chromium-family browsers (macOS & Linux)."""
    sys_name = platform.system().lower()
    key_cache: dict[str, Optional[bytes]] = {}
    now_chrome_us = int((time.time() + 11644473600) * 1_000_000)

    for label, db_path, kc_svc in _chromium_cookie_files():
        tmp_dir = tempfile.mkdtemp(prefix="aiw-ck-")
        try:
            dst = Path(tmp_dir) / "Cookies"
            shutil.copy2(db_path, dst)
            for ext in ("-wal", "-shm"):
                side = Path(str(db_path) + ext)
                if side.is_file():
                    try:
                        shutil.copy2(side, Path(str(dst) + ext))
                    except Exception:  # noqa: BLE001
                        pass
            conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
            try:
                cur = conn.cursor()
                db_ver = 0
                try:
                    row = cur.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                    if row:
                        db_ver = int(row[0])
                except Exception:  # noqa: BLE001
                    pass
                where = " OR ".join("host_key LIKE ?" for _ in domains)
                params = [f"%{d}" for d in domains]
                rows = cur.execute(
                    f"SELECT host_key, name, value, encrypted_value, path, expires_utc "
                    f"FROM cookies WHERE ({where})",
                    params,
                ).fetchall()
            finally:
                conn.close()

            if not rows:
                continue

            if kc_svc not in key_cache:
                if sys_name == "darwin":
                    pw = _macos_keychain_password(kc_svc)
                    key_cache[kc_svc] = _derive_chromium_key(pw, 1003) if pw else None
                else:
                    key_cache[kc_svc] = _derive_chromium_key(b"peanuts", 1)
            key = key_cache[kc_svc]

            out: list[dict[str, Any]] = []
            for host_key, name, val, enc_val, path, exp_utc in rows:
                if not _domain_matches(str(host_key or ""), domains):
                    continue
                if isinstance(exp_utc, int) and exp_utc > 0 and exp_utc < now_chrome_us:
                    continue
                text_val = str(val or "").strip()
                if not text_val and enc_val:
                    raw_enc = bytes(enc_val) if isinstance(enc_val, (bytes, bytearray, memoryview)) else b""
                    text_val = _decrypt_chromium_value(raw_enc, key, db_ver).strip()
                if text_val:
                    out.append({
                        "name": str(name or ""),
                        "value": text_val,
                        "domain": str(host_key or ""),
                        "path": str(path or "/"),
                    })
            if out:
                return out, f"browser:{label}"
        except Exception as e:  # noqa: BLE001
            log.debug("sqlite cookie read %s failed: %s", db_path, e)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return [], ""


# --------------------------------------------------------------------------------------------- Remote computer reader
_REMOTE_SCRIPT = r'''
import base64, hashlib, json, os, platform, shutil, sqlite3, subprocess, tempfile, time, urllib.request
from pathlib import Path

DOMAINS = json.loads(os.environ["AIW_CK_DOMAINS"])

def match(host):
    h = (host or "").strip().lower().lstrip(".")
    return bool(h) and any(h == d or h.endswith("." + d) for d in DOMAINS)

def try_cdp():
    for port in (9222, 9223, 9229, 9333):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                ws_url = (json.loads(r.read().decode()) or {}).get("webSocketDebuggerUrl") or ""
            if not ws_url:
                continue
            import asyncio, websockets
            async def grab():
                async with websockets.connect(ws_url, open_timeout=3, close_timeout=2, max_size=32*1024*1024) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
                    for _ in range(20):
                        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if m.get("id") == 1:
                            return (m.get("result") or {}).get("cookies") or []
                return []
            cks = asyncio.run(grab())
            now = time.time()
            out = [
                {"name": str(c.get("name") or ""), "value": str(c.get("value") or ""),
                 "domain": str(c.get("domain") or ""), "path": str(c.get("path") or "/")}
                for c in cks
                if isinstance(c, dict) and match(c.get("domain"))
                and not (isinstance(c.get("expires"), (int, float)) and c["expires"] > 0 and c["expires"] < now)
                and c.get("value")
            ]
            if out:
                return out, f"remote-cdp:{port}"
        except Exception:
            continue
    return [], ""

def try_sqlite():
    sys_name = platform.system().lower()
    home = Path.home()
    browsers = [
        ("Chrome", "Google/Chrome", "Chrome Safe Storage", "google-chrome"),
        ("Arc", "Arc/User Data", "Arc Safe Storage", ""),
        ("Brave", "BraveSoftware/Brave-Browser", "Brave Safe Storage", "BraveSoftware/Brave-Browser"),
        ("Edge", "Microsoft Edge", "Microsoft Edge Safe Storage", "microsoft-edge"),
        ("Chromium", "Chromium", "Chromium Safe Storage", "chromium"),
        ("BrowserOS", "BrowserOS", "BrowserOS Safe Storage", "browseros"),
    ]
    now_us = int((time.time() + 11644473600) * 1_000_000)
    for label, mac_rel, kc_svc, linux_rel in browsers:
        base = (home / "Library/Application Support" / mac_rel) if sys_name == "darwin" else (home / ".config" / linux_rel if linux_rel else Path("/nonexistent"))
        if not base.exists():
            continue
        cands = [base / "Default/Cookies", base / "Default/Network/Cookies"]
        for prof in sorted(base.glob("Profile *")):
            cands += [prof / "Cookies", prof / "Network/Cookies"]
        for p in cands:
            if not p.is_file():
                continue
            td = tempfile.mkdtemp(prefix="aiw-rck-")
            try:
                dst = Path(td) / "Cookies"
                shutil.copy2(p, dst)
                conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
                cur = conn.cursor()
                db_ver = 0
                try:
                    row = cur.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                    if row:
                        db_ver = int(row[0])
                except Exception:
                    pass
                where = " OR ".join("host_key LIKE ?" for _ in DOMAINS)
                rows = cur.execute(
                    f"SELECT host_key, name, value, encrypted_value, path, expires_utc FROM cookies WHERE ({where})",
                    [f"%{d}" for d in DOMAINS],
                ).fetchall()
                conn.close()
                if not rows:
                    continue
                key = None
                if sys_name == "darwin":
                    try:
                        pw = subprocess.check_output(["security", "find-generic-password", "-w", "-s", kc_svc], stderr=subprocess.DEVNULL, timeout=5).strip()
                        if pw:
                            key = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1003, dklen=16)
                    except Exception:
                        pass
                else:
                    key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, dklen=16)
                out = []
                for host_key, name, val, enc_val, path, exp_utc in rows:
                    if not match(host_key) or (isinstance(exp_utc, int) and exp_utc > 0 and exp_utc < now_us):
                        continue
                    tv = str(val or "").strip()
                    if not tv and enc_val and key:
                        raw = bytes(enc_val)
                        if raw.startswith((b"v10", b"v11")):
                            try:
                                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                                from cryptography.hazmat.primitives.padding import PKCS7
                                dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
                                padded = dec.update(raw[3:]) + dec.finalize()
                                up = PKCS7(128).unpadder()
                                plain = up.update(padded) + up.finalize()
                                if db_ver >= 24 and len(plain) >= 32:
                                    try:
                                        tv = plain[32:].decode("utf-8").strip()
                                    except UnicodeDecodeError:
                                        tv = plain.decode("utf-8", "ignore").strip()
                                else:
                                    tv = plain.decode("utf-8", "ignore").strip()
                            except Exception:
                                pass
                    if tv:
                        out.append({"name": str(name or ""), "value": tv, "domain": str(host_key or ""), "path": str(path or "/")})
                if out:
                    return out, f"remote-browser:{label}"
            except Exception:
                pass
            finally:
                shutil.rmtree(td, ignore_errors=True)
    return [], ""

cks, src = try_cdp()
if not cks:
    cks, src = try_sqlite()
print("COOKIERESULT " + json.dumps({"cookies": cks, "source": src}, ensure_ascii=False))
'''


async def read_remote_browser_cookies(
    shell: Callable[[str, str, int], Any],
    machine_id: str,
    domains: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str]:
    """Run cookie extractor on a connected computer via vm-agent shell.exec."""
    script_b64 = base64.b64encode(_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    dom_json = json.dumps(list(domains))
    cmd = (
        f"AIW_CK_DOMAINS='{dom_json}' python3 -c "
        f"\"import base64; exec(base64.b64decode('{script_b64}').decode())\""
    )
    out = await shell(machine_id, cmd, 30)
    for line in reversed((out or "").splitlines()):
        if line.startswith("COOKIERESULT "):
            try:
                data = json.loads(line[len("COOKIERESULT "):])
                return list(data.get("cookies") or []), str(data.get("source") or "")
            except ValueError:
                return [], ""
    return [], ""


async def read_browser_cookies(
    bot: str = "cue",
    *,
    shell: Optional[Callable[[str, str, int], Any]] = None,
    machine_id: str = "",
) -> dict[str, Any]:
    """Read cookies for `cue` from local browsers first, then connected computer if available."""
    if bot != "cue":
        return {"ok": False, "source": "", "cookies": [], "creds": {}}
    domains = CUE_DOMAINS

    cookies, source = await read_local_cdp_cookies(domains)
    if not cookies:
        cookies, source = await asyncio.to_thread(read_local_sqlite_cookies, domains)
    if not cookies and shell and machine_id:
        try:
            cookies, source = await read_remote_browser_cookies(shell, machine_id, domains)
        except Exception as e:  # noqa: BLE001
            log.debug("remote cookie read (%s @ %s) failed: %s", bot, machine_id, e)

    if not cookies:
        return {"ok": False, "source": "", "cookies": [], "creds": {}}

    creds = extract_cue_credentials_from_cookies(cookies)
    return {
        "ok": bool(creds),
        "source": source,
        "cookies": cookies,
        "creds": creds,
    }
