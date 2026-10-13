"""LinuxComputerProvider - talks ONLY to vm-agent over WebSocket (spec Phase 3).

Features: auto-connect/reconnect, request pipelining by id, per-call timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from aiw_protocols import Command, CommandResult
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ..base import ActionResult, ComputerProvider

log = logging.getLogger("computer-runtime")


class LinuxComputerProvider(ComputerProvider):
    def __init__(self, endpoint: str, token: Optional[str] = None, connect_timeout: float = 10.0,
                 retries: int = 2):
        self.endpoint = endpoint
        self.token = token
        self.connect_timeout = connect_timeout
        self.retries = retries
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._pending: dict[str, tuple] = {}  # id -> (ws, future)
        self._lock = asyncio.Lock()

    async def _ensure(self):
        async with self._lock:
            if self._ws is not None:
                return
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
            self._ws = await asyncio.wait_for(
                connect(self.endpoint, additional_headers=headers, max_size=64 * 1024 * 1024),
                self.connect_timeout)
            self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        ws = self._ws
        try:
            async for raw in ws:
                data = json.loads(raw)
                entry = self._pending.pop(data.get("id"), None)
                if entry and not entry[1].done():
                    entry[1].set_result(CommandResult.model_validate(data))
        except ConnectionClosed:
            pass
        finally:
            # Only tear down state that belongs to THIS connection: a newer connection may already be live.
            if self._ws is ws:
                self._ws = None
            for cid, (conn, fut) in list(self._pending.items()):
                if conn is ws:
                    self._pending.pop(cid, None)
                    if not fut.done():
                        fut.set_exception(ConnectionError("vm-agent connection lost"))

    async def execute(self, action: str, payload: dict | None = None, observe: bool = False,
                      timeout: float = 30.0) -> ActionResult:
        cmd = Command(type=action, payload=payload or {}, observe=observe, timeout=timeout)
        last_err = None
        for attempt in range(self.retries + 1):
            try:
                await self._ensure()
                ws = self._ws
                fut = asyncio.get_running_loop().create_future()
                self._pending[cmd.id] = (ws, fut)
                await ws.send(cmd.model_dump_json())
                res: CommandResult = await asyncio.wait_for(fut, timeout + 5)
                obs = res.observation
                return ActionResult(
                    success=res.success, result=res.result, error=res.error, action=action,
                    screenshot=obs.screenshot if obs else None,
                    width=obs.width if obs else None, height=obs.height if obs else None,
                    duration_ms=res.duration_ms)
            except (ConnectionError, OSError, ConnectionClosed, asyncio.TimeoutError) as e:
                self._pending.pop(cmd.id, None)
                last_err = f"{type(e).__name__}: {e}"
                log.warning("vm-agent call %s failed (attempt %s): %s", action, attempt + 1, last_err)
                # A slow command is not a dead link: keep the shared connection (other calls ride on it).
                # Input actions are NOT idempotent: only retry on connection failure, not timeout.
                if isinstance(e, asyncio.TimeoutError):
                    break
                await self.close()
                await asyncio.sleep(0.5 * (attempt + 1))
        return ActionResult(success=False, error=last_err, action=action)

    async def close(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        if self._reader:
            self._reader.cancel()
            self._reader = None
