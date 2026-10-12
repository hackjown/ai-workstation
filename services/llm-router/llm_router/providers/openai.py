from __future__ import annotations

import json
import os

import httpx

from ..router import LLMDecision, LLMProvider, LLMRequest, user_text


class OpenAIProvider(LLMProvider):
    """Chat Completions with tools. Works with OpenAI and compatible gateways
    (set OPENAI_BASE_URL, e.g. a local vLLM / Qwen-VL / DeepSeek endpoint)."""

    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None, base_url: str | None = None):
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")
        self.key = api_key or os.environ["OPENAI_API_KEY"]
        self.url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")) + "/chat/completions"

    async def decide(self, req: LLMRequest) -> LLMDecision:
        content = [{"type": "text", "text": user_text(req)}]
        if req.screenshot_b64:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{req.screenshot_b64}"}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": content}],
            "tools": [{"type": "function", "function": t} for t in req.tools],
            "tool_choice": "required",
        }
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
            r.raise_for_status()
            data = r.json()
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return LLMDecision(msg.get("content") or "", "fail", {"reason": "model returned no tool call"}, raw=data)
        fn = calls[0]["function"]
        return LLMDecision(msg.get("content") or "", fn["name"], json.loads(fn.get("arguments") or "{}"),
                           raw=data, usage=data.get("usage", {}))
