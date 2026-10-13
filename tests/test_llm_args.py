"""Gateways that drop or mangle tool arguments (seen with GLM on an OpenAI-compatible gateway)."""
import json

import httpx
from llm_router import LLMRequest
from llm_router.providers.openai import OpenAIProvider, code_block, parse_args

TOOLS = [{"name": "shell", "description": "run", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}]


def test_parse_args_variants():
    assert parse_args('{"command": "ls"}') == {"command": "ls"}
    assert parse_args({"command": "ls"}) == {"command": "ls"}
    assert parse_args(json.dumps('{"command": "ls"}')) == {"command": "ls"}          # double-encoded
    assert parse_args('```json\n{"command": "ls"}\n```') == {"command": "ls"}
    assert parse_args('{"command": "ls"} trailing') == {"command": "ls"}
    assert parse_args("") == {} and parse_args(None) == {}
    assert code_block("do this\n```python\nprint(1)\n```").startswith("python3 - <<'AIW_PY'\nprint(1)")


async def test_empty_args_are_retried_with_feedback(monkeypatch):
    replies = [
        {"choices": [{"finish_reason": "length", "message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]}}]},
        {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "shell", "arguments": '{"command": "echo hi"}'}}]}}]},
    ]
    sent = []

    async def fake_post(self, url, json=None, headers=None):
        sent.append(json)
        return httpx.Response(200, json=replies.pop(0), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    d = await OpenAIProvider(model="m", api_key="k", base_url="http://gw").decide(
        LLMRequest(system="s", task="t", history=[], screenshot_b64=None, tools=TOOLS))
    assert d.tool == "shell" and d.args == {"command": "echo hi"}
    assert sent[0]["max_tokens"] >= 4096
    fb = sent[1]["messages"][-1]
    assert fb["role"] == "tool" and "command" in fb["content"] and "cut off" in fb["content"]
