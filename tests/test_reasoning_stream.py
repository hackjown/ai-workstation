"""Streamed reasoning (thinking models) reaches the UI hook, and the decision keeps it."""
import json

import httpx
import pytest
from llm_router import LLMRequest
from llm_router.providers.openai import OpenAIProvider, reasoning_of


def _sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


async def test_stream_reasoning_and_tool_call(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "先看文档"}}]},
        {"choices": [{"delta": {"reasoning_content": "结构。"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "shell", "arguments": "{\"comm"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "and\": \"ls\"}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"completion_tokens": 42}},
    ]

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=_sse(chunks), headers={"content-type": "text/event-stream"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    p = OpenAIProvider(model="m", api_key="k", base_url="http://x", thinking="on")
    seen = []

    async def cb(text):
        seen.append(text)
    tools = [{"name": "shell", "parameters": {"required": ["command"]}}]
    d = await p.decide(LLMRequest(system="s", task="t", history=[], screenshot_b64=None, tools=tools, on_reasoning=cb))
    assert d.tool == "shell" and d.args == {"command": "ls"}
    assert d.reasoning == "先看文档结构。" and seen[-1] == "先看文档结构。"
    assert d.usage["out_tokens"] == 42


def test_reasoning_of_variants():
    assert reasoning_of({"reasoning_content": "a"}) == "a"
    assert reasoning_of({"reasoning": "b"}) == "b"
    assert reasoning_of({"content": "<think>c</think>call"}) == "c"
    assert reasoning_of({"content": "x"}) == ""
