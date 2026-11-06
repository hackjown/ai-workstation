"""v3.9.24 (11) remote access: one page for every way to reach AIW from outside the home network.

  LAN         the addresses of this host (inside Docker bridge mode that is the container IP: set AIW_LAN_IP or the
              LAN address in the page to show the real one)
  Tailscale   `tailscale ip -4` when the CLI is in the container / on the host, else the address you type in
  Your HTTPS  a reverse proxy you run yourself (Caddy / Nginx / Lucky / frp …) — just stored and shown
  Built-in    a Cloudflare "quick tunnel" (cloudflared tunnel --url …): no account, no port forwarding; a random
              https://<words>.trycloudflare.com address that changes every time the tunnel starts
Push stays on Bark (no APNs).

The built-in tunnel only starts when an API token is set (AIW_API_TOKEN): everything behind it keeps the normal
auth. China: *.trycloudflare.com goes through Cloudflare's global network; from mainland China it is often slow and
sometimes blocked or reset, and quick tunnels have no uptime guarantee — use Tailscale or your own HTTPS for
anything you rely on. Downloading cloudflared comes from GitHub releases (set AIW_CLOUDFLARED_URL to a mirror).

  GET  /api/remote                 everything above + tunnel state
  PUT  /api/remote                 {lan_url, tailscale_url, custom_url, tunnel_autostart}
  POST /api/remote/tunnel/start | /stop | /install
AIW_REMOTE_TUNNEL=0 hides / blocks the built-in tunnel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import socket
import time
from typing import Optional

log = logging.getLogger("aiw.remote")
KV = "remote_access"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"


def lan_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("192.0.2.1", 9))   # TEST-NET: nothing is sent, the OS just picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def port() -> int:
    return int(os.environ.get("AIW_PORT", os.environ.get("PORT", "8000")))


class Tunnel:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.url: Optional[str] = None
        self.error: Optional[str] = None
        self.started: Optional[float] = None
        self._reader: Optional[asyncio.Task] = None
        self.log_tail: list[str] = []

    def binary(self) -> Optional[str]:
        for c in (os.environ.get("AIW_CLOUDFLARED"), os.path.join(self.data_dir, "bin", "cloudflared"), shutil.which("cloudflared")):
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def install(self) -> str:
        import httpx
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm"}.get(platform.machine().lower())
        if not arch:
            raise RuntimeError(f"不支持的 CPU 架构：{platform.machine()}")
        url = os.environ.get("AIW_CLOUDFLARED_URL") or RELEASE.format(arch=arch)
        dst_dir = os.path.join(self.data_dir, "bin")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "cloudflared")
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            if len(r.content) < 1_000_000:
                raise RuntimeError("下载到的文件太小，不像 cloudflared（镜像地址不对？）")
            with open(dst + ".part", "wb") as f:
                f.write(r.content)
        os.chmod(dst + ".part", 0o755)
        os.replace(dst + ".part", dst)
        return dst

    async def start(self, target: str) -> None:
        if self.running():
            return
        b = self.binary()
        if not b:
            raise RuntimeError("没有 cloudflared：先点「安装」或把它放到 PATH / AIW_CLOUDFLARED")
        self.url, self.error, self.log_tail = None, None, []
        self.proc = await asyncio.create_subprocess_exec(
            b, "tunnel", "--no-autoupdate", "--url", target,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.started = time.time()
        self._reader = asyncio.create_task(self._read())
        for _ in range(60):   # the address shows up within a few seconds
            if self.url or not self.running():
                break
            await asyncio.sleep(0.5)
        if not self.running() and not self.url:
            self.error = "cloudflared 退出了：" + " | ".join(self.log_tail[-3:])

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                self.log_tail = (self.log_tail + [line[:300]])[-30:]
                m = URL_RE.search(line)
                if m and not self.url:
                    self.url = m.group(0)
        except Exception as e:  # noqa: BLE001
            log.info("tunnel reader: %s", e)

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 8)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._reader:
            self._reader.cancel()
        self.proc, self.url = None, None

    def state(self) -> dict:
        return {"running": self.running(), "url": self.url if self.running() else None, "error": self.error,
                "started": self.started if self.running() else None, "binary": bool(self.binary()),
                "log": self.log_tail[-8:]}


def mount(app, store, settings, auth) -> Tunnel:
    from fastapi import Body, Depends, HTTPException

    data_dir = os.path.dirname(settings.db_path) or "."
    tunnel = Tunnel(data_dir)
    app.state.tunnel = tunnel

    def cfg() -> dict:
        return store.kv_get(KV, {}) or {}

    def allowed() -> bool:
        return os.environ.get("AIW_REMOTE_TUNNEL", "1") != "0"

    async def tailscale() -> dict:
        exe = shutil.which("tailscale")
        if not exe:
            return {"cli": False}
        try:
            p = await asyncio.create_subprocess_exec(exe, "ip", "-4", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(p.communicate(), 4)
            ip = (out.decode().split() or [""])[0]
            return {"cli": True, "ip": ip or None}
        except Exception as e:  # noqa: BLE001
            return {"cli": True, "error": str(e)[:120]}

    @app.get("/api/remote", dependencies=[Depends(auth)])
    async def remote_get():
        c = cfg()
        env_lan = os.environ.get("AIW_LAN_IP")
        return {"port": port(), "lan_ips": lan_ips(), "env_lan_ip": env_lan, "in_docker": in_docker(),
                "lan_url": c.get("lan_url") or (f"http://{env_lan}:{port()}" if env_lan else ""),
                "tailscale": await tailscale(), "tailscale_url": c.get("tailscale_url") or "",
                "custom_url": c.get("custom_url") or "", "tunnel_autostart": bool(c.get("tunnel_autostart")),
                "auth_ok": bool(settings.api_token), "tunnel_allowed": allowed(), "tunnel": tunnel.state()}

    @app.put("/api/remote", dependencies=[Depends(auth)])
    async def remote_put(body: dict = Body(...)):
        c = cfg()
        for k in ("lan_url", "tailscale_url", "custom_url"):
            if k in (body or {}):
                v = str(body[k] or "").strip()[:300]
                if v and not re.match(r"^https?://", v):
                    raise HTTPException(400, f"{k} 要以 http:// 或 https:// 开头")
                c[k] = v.rstrip("/")
        if "tunnel_autostart" in (body or {}):
            c["tunnel_autostart"] = bool(body["tunnel_autostart"])
        store.kv_set(KV, c)
        return await remote_get()

    @app.post("/api/remote/tunnel/install", dependencies=[Depends(auth)])
    async def tunnel_install():
        if not allowed():
            raise HTTPException(403, "内置隧道已关闭（AIW_REMOTE_TUNNEL=0）")
        try:
            path = await tunnel.install()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"下载 cloudflared 失败：{e}"[:300])
        return {"ok": True, "path": path}

    @app.post("/api/remote/tunnel/start", dependencies=[Depends(auth)])
    async def tunnel_start():
        if not allowed():
            raise HTTPException(403, "内置隧道已关闭（AIW_REMOTE_TUNNEL=0）")
        if not settings.api_token:
            raise HTTPException(409, "先设置 AIW_API_TOKEN：没有令牌时不允许把服务暴露到公网")
        try:
            await tunnel.start(f"http://127.0.0.1:{port()}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e)[:300])
        return tunnel.state()

    @app.post("/api/remote/tunnel/stop", dependencies=[Depends(auth)])
    async def tunnel_stop():
        await tunnel.stop()
        return tunnel.state()

    return tunnel


async def autostart(store, settings, tunnel: Tunnel) -> None:
    c = store.kv_get(KV, {}) or {}
    if c.get("tunnel_autostart") and settings.api_token and os.environ.get("AIW_REMOTE_TUNNEL", "1") != "0" and tunnel.binary():
        try:
            await tunnel.start(f"http://127.0.0.1:{port()}")
        except Exception as e:  # noqa: BLE001
            log.warning("tunnel autostart: %s", e)
