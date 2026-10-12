#!/usr/bin/env python3
"""Phase 2/3 manual acceptance from a dev machine:
   python tools-cli.py ws://VM_IP:8765 screenshot out.png
   python tools-cli.py ws://VM_IP:8765 click 500 300
   python tools-cli.py ws://VM_IP:8765 type hello
   python tools-cli.py ws://VM_IP:8765 key ENTER
   python tools-cli.py ws://VM_IP:8765 shell "ls ~"
Token from $VM_AGENT_TOKEN."""
import asyncio, base64, os, sys
sys.path[:0] = ["protocols", "services/computer-runtime"]
from computer_runtime import LinuxComputerProvider


async def main(ep, op, *a):
    c = LinuxComputerProvider(ep, os.environ.get("VM_AGENT_TOKEN"))
    if op == "screenshot":
        r = await c.screenshot(); open(a[0] if a else "shot.png", "wb").write(base64.b64decode(r.screenshot))
    elif op == "click": r = await c.click(int(a[0]), int(a[1]))
    elif op == "type": r = await c.type(" ".join(a))
    elif op == "key": r = await c.keypress(a[0])
    elif op == "open": r = await c.open_app(a[0])
    elif op == "shell": r = await c.shell(" ".join(a))
    elif op == "health": r = await c.health()
    else: raise SystemExit(__doc__)
    print("OK" if r.success else "FAIL", r.result if r.success else r.error)
    await c.close()

if __name__ == "__main__":
    if len(sys.argv) < 3: raise SystemExit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
