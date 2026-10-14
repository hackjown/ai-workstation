"""browser.* actions.

Default mode drives the *visible* Chrome via keyboard so the user watching noVNC
sees exactly what the agent does. If VM_AGENT_BROWSER=playwright and Playwright
is installed, a persistent CDP-attached context is used instead for reliable
URL/title readback.
"""
from __future__ import annotations

import asyncio
import os

from ..computer.desktop import DesktopBackend


class Browser:
    def __init__(self, backend: DesktopBackend):
        self.b = backend
        self.mode = os.environ.get("VM_AGENT_BROWSER", "desktop")
        self._pw = None
        self._page = None

    async def _playwright_page(self):
        if self._page is None:
            from playwright.async_api import async_playwright  # optional dep
            self._pw = await async_playwright().start()
            cdp = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")
            browser = await self._pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return self._page

    async def open(self, url: str | None = None) -> dict:
        if self.mode == "playwright" or not url or not hasattr(self.b, "env"):
            await self.b.open_app("chrome")
            if url:
                return await self.navigate(url)
            return {"opened": True}
        # Pass the URL on Chrome's command line: no keystrokes race a window that is still starting
        # (typing ctrl+l into the desktop is what made browser_open hang), and a running Chrome
        # simply opens it in a new tab.
        import shlex
        from ..computer.desktop import APP_COMMANDS
        cmd = " || ".join(f"{alt.strip()} {shlex.quote(url)}" for alt in APP_COMMANDS["chrome"].split("||"))
        proc = await asyncio.create_subprocess_shell(
            f"nohup sh -c {shlex.quote(cmd)} >/dev/null 2>&1 &", env=self.b.env)
        await proc.wait()
        window = await self._wait_window()
        return {"url": url, "window": window}

    async def _wait_window(self, timeout: float = 12.0) -> bool:
        """Wait until a Chrome window is visible and raise it, so the user sees it in the desktop view."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            proc = await asyncio.create_subprocess_shell(
                "xdotool search --onlyvisible --class 'chrom' | tail -1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=self.b.env)
            out, _ = await proc.communicate()
            wid = out.decode().strip()
            if wid:
                await asyncio.sleep(1.0)  # let the page start painting
                p2 = await asyncio.create_subprocess_shell(
                    f"xdotool windowactivate {wid}", stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, env=self.b.env)
                await p2.wait()
                return True
            await asyncio.sleep(0.5)
        return False

    async def navigate(self, url: str) -> dict:
        if self.mode == "playwright":
            page = await self._playwright_page()
            await page.goto(url)
            return {"url": page.url, "title": await page.title()}
        await self.b.key("ctrl+l")
        await asyncio.sleep(0.2)
        await self.b.type(url)
        await self.b.key("Return")
        await asyncio.sleep(1.0)
        return {"url": url}

    async def back(self) -> dict:
        if self.mode == "playwright":
            page = await self._playwright_page()
            await page.go_back()
            return {"url": page.url}
        await self.b.key("alt+Left")
        return {"ok": True}

    async def forward(self) -> dict:
        if self.mode == "playwright":
            page = await self._playwright_page()
            await page.go_forward()
            return {"url": page.url}
        await self.b.key("alt+Right")
        return {"ok": True}
