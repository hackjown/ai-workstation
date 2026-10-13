from __future__ import annotations

import json
import os
import re

import httpx

from ..router import LLMDecision, LLMProvider, LLMRequest, user_text


def parse_args(raw) -> dict:
    """Tool arguments as gateways actually send them: a JSON string, an already-decoded
    object, a double-encoded string, or JSON wrapped in a ``` fence."""
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    for _ in range(2):
        try:
            val = json.loads(text)
        except ValueError:
            break
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            text = val
            continue
        break
    try:  # trailing garbage / cut-off object: take the outermost {...}
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return {}


def code_block(text: str) -> str:
    """A shell command the model wrote in prose instead of in the arguments."""
    m = re.search(r"```(bash|sh|shell|python|py)?\s*\n(.*?)```", text or "", re.S)
    if not m:
        return ""
    lang, code = (m.group(1) or "bash").lower(), m.group(2).strip()
    if lang in ("python", "py"):
        return "python3 - <<'AIW_PY'\n" + code + "\nAIW_PY"
    return code


class OpenAIProvider(LLMProvider):
    """Chat Completions with tools. Works with OpenAI and compatible gateways
    (set OPENAI_BASE_URL, e.g. a local vLLM / Qwen-VL / DeepSeek endpoint)."""

    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None, base_url: str | None = None,
                 tool_choice: str | None = None):
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")
        self.key = api_key or os.environ["OPENAI_API_KEY"]
        self.tool_choice = tool_choice or os.environ.get("AIW_TOOL_CHOICE", "required")
        self.url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")) + "/chat/completions"

    async def decide(self, req: LLMRequest) -> LLMDecision:
        content = [{"type": "text", "text": user_text(req)}]
        if req.screenshot_b64 and not req.screen_desc:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{req.screenshot_b64}"}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": content}],
            "tools": [{"type": "function", "function": t} for t in req.tools],
            "tool_choice": self.tool_choice,
            # a long script inside `command` gets cut (and arrives as {}) on gateways with a small default
            "max_tokens": int(os.environ.get("AIW_LLM_MAX_TOKENS", "4096")),
        }
        required = {t["name"]: (t.get("parameters") or {}).get("required") or [] for t in req.tools}
        data, msg, calls, name, args = {}, {}, [], "", {}
        async with httpx.AsyncClient(timeout=180) as c:
            for attempt in range(3):
                if attempt == 2:  # some gateways (e.g. GLM) only behave with tool_choice=auto
                    body["tool_choice"] = "auto"
                r = await c.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
                r.raise_for_status()
                data = r.json()
                choice = data["choices"][0]
                msg = choice["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    # Some compatible gateways ignore tool_choice=required and answer in prose.
                    body["messages"] = body["messages"][:2] + [
                        {"role": "assistant", "content": msg.get("content") or ""},
                        {"role": "user", "content": "Do not reply in text. Call exactly one of the provided tools now "
                                                    "(use done/fail to finish)."}]
                    continue
                fn = calls[0].get("function") or {}
                name, args = fn.get("name") or "", parse_args(fn.get("arguments"))
                if name == "shell" and not args.get("command"):
                    code = code_block(msg.get("content") or "")
                    if code:
                        args["command"] = code
                missing = [k for k in required.get(name, []) if args.get(k) in (None, "")]
                if not missing:
                    break
                # Tell the model exactly what was wrong instead of letting the loop fail on it 5 times.
                truncated = choice.get("finish_reason") == "length"
                body["messages"] = body["messages"][:2] + [
                    {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [calls[0]]},
                    {"role": "tool", "tool_call_id": calls[0].get("id") or "call_0",
                     "content": f"ERROR: the {name} call arrived without required argument(s) {missing}"
                                + (" because the output was cut off (too long)" if truncated else "")
                                + ". Call the tool again with every required argument filled in. "
                                  "For long scripts first write the file with files_write, then run it with a short shell command."}]
        if not calls:
            return LLMDecision(msg.get("content") or "", "fail", {"reason": "model returned no tool call"}, raw=data)
        return LLMDecision(msg.get("content") or "", name, args, raw=data, usage=data.get("usage", {}))

    async def describe(self, system: str, user: str, image_b64: str, max_tokens: int = 900) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": user},
                                         {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}]}]}
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"].get("content") or ""

    async def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"].get("content") or ""
