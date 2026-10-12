from __future__ import annotations

import os

import httpx

from ..router import LLMDecision, LLMProvider, LLMRequest, user_text


class GoogleProvider(LLMProvider):
    name = "google"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
        self.key = api_key or os.environ["GEMINI_API_KEY"]

    async def decide(self, req: LLMRequest) -> LLMDecision:
        parts = [{"text": user_text(req)}]
        if req.screenshot_b64:
            parts.append({"inline_data": {"mime_type": "image/png", "data": req.screenshot_b64}})
        body = {
            "system_instruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": parts}],
            "tools": [{"function_declarations": req.tools}],
            "tool_config": {"function_calling_config": {"mode": "ANY"}},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json=body, headers={"x-goog-api-key": self.key})
            r.raise_for_status()
            data = r.json()
        cparts = data["candidates"][0]["content"]["parts"]
        thought = " ".join(p.get("text", "") for p in cparts if "text" in p).strip()
        call = next((p["functionCall"] for p in cparts if "functionCall" in p), None)
        if not call:
            return LLMDecision(thought, "fail", {"reason": "model returned no tool call"}, raw=data)
        return LLMDecision(thought, call["name"], call.get("args") or {}, raw=data)
