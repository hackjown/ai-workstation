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
        await self.b.open_app("chrome")
        if url:
            return await self.navigate(url)
        return {"opened": True}

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
