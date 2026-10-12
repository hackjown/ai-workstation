from __future__ import annotations

import json
import os

import httpx

from ..router import LLMDecision, LLMProvider, LLMRequest, user_text


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None, base_url: str | None = None):
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")) + "/v1/messages"

    async def decide(self, req: LLMRequest) -> LLMDecision:
        content = []
        if req.screenshot_b64:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": req.screenshot_b64}})
        content.append({"type": "text", "text": user_text(req)})
        body = {
            "model": self.model, "max_tokens": 1024, "system": req.system,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in req.tools],
            "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": content}],
        }
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(self.url, json=body, headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            data = r.json()
        thought = " ".join(b.get("text", "") for b in data["content"] if b["type"] == "text").strip()
        call = next((b for b in data["content"] if b["type"] == "tool_use"), None)
        if not call:
            return LLMDecision(thought, "fail", {"reason": "model returned no tool call"}, raw=data)
        return LLMDecision(thought, call["name"], call.get("input") or {}, raw=data, usage=data.get("usage", {}))
