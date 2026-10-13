"""Model capability probe: does this model, through this gateway, return tool calls
WITH arguments, and can it read an image? Answers the 'which model for which role' question."""
from __future__ import annotations

import base64
import io
import json
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

TOOL = {"type": "function", "function": {
    "name": "run_command", "description": "Run a shell command.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "the command"}},
                   "required": ["command"]}}}


def _probe_image() -> str:
    img = Image.new("RGB", (320, 160), (220, 30, 30))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 96)
    except OSError:
        try:
            font = ImageFont.load_default(size=96)
        except TypeError:  # Pillow < 10.1
            font = ImageFont.load_default()
    d.text((80, 20), "47", fill=(255, 255, 255), font=font)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _post(c: httpx.AsyncClient, url: str, key: str, body: dict) -> dict:
    r = await c.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


async def probe(base: str, key: str, model: str) -> dict:
    url = base.rstrip("/") + "/chat/completions"
    out: dict = {"model": model, "tools": False, "tool_choice": None, "vision": False, "ts": time.time()}
    notes = []
    long_cmd = "python3 -c \"print('" + "x" * 400 + "')\""
    async with httpx.AsyncClient(timeout=60) as c:
        t0 = time.time()
        for choice in ("required", "auto"):
            try:
                data = await _post(c, url, key, {
                    "model": model, "max_tokens": 1500, "tool_choice": choice, "tools": [TOOL],
                    "messages": [{"role": "user", "content": "Call run_command with exactly this command, unchanged:\n" + long_cmd}]})
            except Exception as e:  # noqa: BLE001
                notes.append(f"tool_choice={choice}: {e}")
                continue
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            raw = calls[0]["function"].get("arguments") if calls else None
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            except ValueError:
                args = {}
            cmd = str(args.get("command") or "")
            if cmd and "x" * 300 in cmd:
                out["tools"], out["tool_choice"] = True, choice
                break
            notes.append(f"tool_choice={choice}: " + ("没有工具调用" if not calls else
                                                      f"参数{'为空' if not cmd else '不完整'}（{str(raw)[:80]}）"))
        out["tools_ms"] = int((time.time() - t0) * 1000)
        t0 = time.time()
        try:
            data = await _post(c, url, key, {
                "model": model, "max_tokens": 60, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "What number is written in the image, and what is the background color? "
                                             "Answer like: 12, blue"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _probe_image()}}]}]})
            ans = (data["choices"][0]["message"].get("content") or "").lower()
            out["vision"] = "47" in ans and ("red" in ans or "红" in ans)
            if not out["vision"]:
                notes.append(f"看图回答：{ans[:80]}")
        except Exception as e:  # noqa: BLE001
            notes.append(f"看图：{e}"[:200])
        out["vision_ms"] = int((time.time() - t0) * 1000)
    out["notes"] = notes
    return out
