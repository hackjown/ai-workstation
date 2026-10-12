"""VM Agent WebSocket server.

    python -m vm_agent.server.websocket --host 0.0.0.0 --port 8765

Auth: shared token via `Authorization: Bearer <token>` header or `?token=`.
Each incoming message is a Command JSON; replies are CommandResult JSON,
matched by id, so the client may pipeline requests.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import serve as ws_serve
from aiw_protocols import Command, CommandResult
from pydantic import ValidationError

from ..computer.desktop import make_backend
from .router import Router

log = logging.getLogger("vm-agent")


def _token_ok(ws, expected: str | None) -> bool:
    if not expected:
        return True
    req = ws.request
    auth = req.headers.get("Authorization", "")
    supplied = auth[7:] if auth.startswith("Bearer ") else None
    if supplied is None:
        supplied = parse_qs(urlparse(req.path).query).get("token", [None])[0]
    return bool(supplied) and hmac.compare_digest(supplied, expected)


async def serve(host: str, port: int, router: Router, token: str | None, ready: asyncio.Event | None = None,
                stop: asyncio.Event | None = None):
    async def handler(ws):
        if not _token_ok(ws, token):
            await ws.close(code=4401, reason="unauthorized")
            return
        log.info("client connected %s", ws.remote_address)
        pending: set[asyncio.Task] = set()

        async def run_one(raw: str):
            try:
                cmd = Command.model_validate_json(raw)
            except ValidationError as e:
                try:
                    cid = json.loads(raw).get("id", "unknown")
                except Exception:  # noqa: BLE001
                    cid = "unknown"
                res = CommandResult(id=cid, success=False, error=f"bad command: {e.errors()[:2]}")
            else:
                log.info("-> %s %s", cmd.id, cmd.type)
                res = await router.handle(cmd)
            await ws.send(res.model_dump_json(exclude_none=True))

        async for raw in ws:
            t = asyncio.create_task(run_one(raw))
            pending.add(t)
            t.add_done_callback(pending.discard)
        for t in pending:
            t.cancel()

    async with ws_serve(handler, host, port, max_size=64 * 1024 * 1024, ping_interval=20):
        log.info("vm-agent listening on ws://%s:%s backend=%s", host, port, router.b.name)
        if ready:
            ready.set()
        await (stop.wait() if stop else asyncio.Future())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("VM_AGENT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("VM_AGENT_PORT", "8765")))
    ap.add_argument("--backend", default=None, help="x11 | mock | auto")
    ap.add_argument("--max-width", type=int, default=int(os.environ.get("VM_AGENT_SHOT_WIDTH", "0")) or None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("VM_AGENT_TOKEN")
    if not token:
        log.warning("VM_AGENT_TOKEN not set: agent is UNAUTHENTICATED")
    router = Router(make_backend(args.backend), args.max_width)
    asyncio.run(serve(args.host, args.port, router, token))


if __name__ == "__main__":
    main()
