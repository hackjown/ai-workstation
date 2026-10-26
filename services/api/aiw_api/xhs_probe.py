#!/usr/bin/env python3
"""小红书 page probe. Runs ON the AIW computer (stdlib only) against the logged-in Chrome's DevTools port
(--remote-debugging-port=9222). The API ships it to ~/.config/aiw/xhs/probe.py.

  probe.py state     the most recent xiaohongshu.com tab: url, title, dialog/toast texts, a slice of the body text,
                     whether a QR login box or a captcha frame is showing. Read only: it never clicks or types.

Prints one JSON object. The API classifies it (xhs.classify), so the rules are unit-tested there.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from urllib.parse import urlparse

CDP = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")
HOSTS = ("xiaohongshu.com", "xhslink.com")


class WS:
    """Tiny RFC 6455 client (same approach as shop_probe.py)."""

    def __init__(self, url: str, timeout: float = 10):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.sock.recv(4096)
            if not c:
                raise ConnectionError("handshake failed")
            head += c
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("handshake refused")
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self.ids = 0

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise ConnectionError("closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, data, op: int = 1):
        data = data.encode() if isinstance(data, str) else data
        n = len(data)
        hdr = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                    bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                    bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 9:
                self.send(data, op=10)
                continue
            if op == 8:
                raise ConnectionError("closed by peer")
            parts += data
            if b1 & 0x80:
                return parts.decode(errors="replace")

    def eval(self, expr: str, timeout: float = 10):
        self.ids += 1
        mid = self.ids
        self.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                              "params": {"expression": expr, "returnByValue": True}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.recv())
            if msg.get("id") == mid:
                return ((msg.get("result") or {}).get("result") or {}).get("value")
        raise TimeoutError("eval")


STATE_JS = r"""
(() => {
  const vis = el => { if (!el || !el.getBoundingClientRect) return false; const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false; const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05; };
  const T = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const alerts = [];
  const q = '[role=dialog],[role=alert],[aria-modal=true],[class*="toast"],[class*="Toast"],[class*="modal"],[class*="Modal"],' +
            '[class*="dialog"],[class*="Dialog"],[class*="captcha"],[class*="verify"],[class*="notice"],[class*="tip-"],[class*="login-container"]';
  const isNote = el => { const c = (typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '');
    return /note|comment|feed|mask/i.test(c) || !!el.querySelector('#noteContainer,[class*="note-"],[class*="comment"],[class*="feeds"]'); };
  for (const el of document.querySelectorAll(q)) { if (alerts.length > 12) break; if (!vis(el) || isNote(el)) continue; const t = T(el);
    if (t && t.length < 400 && !alerts.includes(t)) alerts.push(t); }
  const frames = [...document.querySelectorAll('iframe')].filter(vis).map(f => f.src || '');
  const captcha = frames.some(s => /captcha|verify|sec\./i.test(s)) || !!document.querySelector('[class*="captcha"] canvas, [class*="slider-verify"], #red-captcha');
  const qr = [...document.querySelectorAll('[class*="qrcode"], [class*="qr-code"], img[class*="qr"]')].some(vis);
  return JSON.stringify({url: location.href, title: document.title.slice(0, 120), alerts,
    text: (document.body ? document.body.innerText : '').slice(0, 3000), captcha_frame: captcha, qr_login: qr});
})()
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "state":
        print(json.dumps({"error": "usage: probe.py state"}))
        return
    try:
        with urllib.request.urlopen(CDP + "/json/list", timeout=4) as r:   # noqa: S310 - local DevTools only
            tabs = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"devtools not reachable: {e}"}))
        sys.exit(3)
    tabs = [t for t in tabs if t.get("type") == "page" and any(h in (t.get("url") or "") for h in HOSTS)]
    if not tabs:
        print(json.dumps({"url": "", "tabs": 0}))
        return
    t = tabs[0]   # /json/list lists the most recently used tab first
    try:
        ws = WS(t["webSocketDebuggerUrl"])
        snap = json.loads(ws.eval(STATE_JS) or "{}")
    except Exception as e:  # noqa: BLE001
        snap = {"url": t.get("url"), "title": t.get("title"), "alerts": [], "text": "", "eval_error": str(e)[:200]}
    snap["tabs"] = len(tabs)
    print(json.dumps(snap, ensure_ascii=False))


if __name__ == "__main__":
    main()
