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
                 tool_choice: str | None = None, thinking: str | None = None, effort: str | None = None):
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")
        # v3.9.24 auto routing: a reasoning effort (low / medium / high) sent as reasoning_effort; a gateway that
        # rejects it gets the plain request (the same 400 fallback as the thinking switches)
        self.effort = (effort or "").lower() if (effort or "").lower() in ("low", "medium", "high") else ""
        self.key = api_key or os.environ["OPENAI_API_KEY"]
        self.tool_choice = tool_choice or os.environ.get("AIW_TOOL_CHOICE", "required")
        # "off": ask reasoning models (GLM, Qwen3, DeepSeek...) to skip long hidden thinking. Each agent step is
        # one small decision, so thinking mostly costs time (tens of seconds per step on GLM-V).
        self.thinking = (thinking or os.environ.get("AIW_LLM_THINKING", "off")).lower()
        if self.effort and self.thinking == "off":
            self.thinking = "on"
        self._extras_ok = True
        self._stream_ok = os.environ.get("AIW_LLM_STREAM", "1") != "0"
        self.url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")) + "/chat/completions"

    async def decide(self, req: LLMRequest) -> LLMDecision:
        content = [{"type": "text", "text": user_text(req)}]
        if req.screenshot_b64 and not req.screen_desc:
            content.append({"type": "image_url", "image_url": {"url": f"data:{_mime(req.screenshot_b64)};base64,{req.screenshot_b64}"}})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": content}],
            "tools": [{"type": "function", "function": t} for t in req.tools],
            "tool_choice": self.tool_choice,
            # a long script inside `command` gets cut (and arrives as {}) on gateways with a small default
            "max_tokens": self._max_tokens(),
        }
        saved = self.thinking
        if getattr(req, "thinking", None):
            self.thinking = req.thinking.lower()
            body["max_tokens"] = self._max_tokens()
        try:
            return await self._decide(req, body)
        finally:
            self.thinking = saved

    async def _decide(self, req: LLMRequest, body: dict) -> LLMDecision:
        required = {t["name"]: (t.get("parameters") or {}).get("required") or [] for t in req.tools}
        data, msg, calls, name, args = {}, {}, [], "", {}
        attempts, out_tokens = 0, 0
        # a thinking model writing a long reply needs minutes, not the 180 s that suits a quick decision
        wait = float(os.environ.get("AIW_LLM_TIMEOUT_S", "600" if self.thinking != "off" else "180"))
        async with httpx.AsyncClient(timeout=wait) as c:
            for attempt in range(3):
                if attempt == 2:  # some gateways (e.g. GLM) only behave with tool_choice=auto
                    body["tool_choice"] = "auto"
                data = await self._post_live(c, body, req)
                attempts += 1
                out_tokens += int((data.get("usage") or {}).get("completion_tokens") or 0)
                choice = data["choices"][0]
                msg = choice["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    # Some compatible gateways ignore tool_choice=required and answer in prose. With thinking on,
                    # DeepSeek also spent the whole max_tokens reasoning about a 12-sheet plan and was cut off before
                    # any tool call, three times in a row (run ac3, 402 s, then the task failed).
                    cut = choice.get("finish_reason") == "length"
                    said = (msg.get("content") or "")[-1500:]
                    if cut and self.thinking != "off":
                        # the whole budget went into hidden reasoning (16k tokens, ~4.5 min on DeepSeek, three times
                        # in a row = 13 min with nothing drawn, task-960): answer the retry without thinking
                        self.thinking = "off"
                        body["max_tokens"] = int(os.environ.get("AIW_LLM_MAX_TOKENS_THINKING", "16384"))
                    ask = ("Your previous reply was CUT OFF at the length limit before any tool call. Do not repeat that "
                           "reasoning: think in a few lines, then call exactly one tool now. Split long content: write "
                           "one part per call (files_write with append=true adds to the file)."
                           if cut else "Do not reply in text. Call exactly one of the provided tools now (use done/fail to finish).")
                    body["messages"] = body["messages"][:2] + [
                        {"role": "assistant", "content": said}, {"role": "user", "content": ask}]
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
            cut = (data.get("choices") or [{}])[0].get("finish_reason") == "length"
            return LLMDecision(strip_think(msg.get("content") or "")[-3000:], "fail",
                               {"reason": "model returned no tool call" + (" (output cut off by max_tokens)" if cut else "")},
                               raw=data)
        usage = {**(data.get("usage") or {}), "attempts": attempts, "out_tokens": out_tokens}
        return LLMDecision(strip_think(msg.get("content") or ""), name, args, raw=data, usage=usage,
                           reasoning=reasoning_of(msg))

    def _max_tokens(self) -> int:
        """Output budget per decision. A long script inside `command` gets cut (and arrives as {}) on gateways with a
        small default; a thinking model also spends its reasoning out of the same budget, so it gets more."""
        if self.thinking != "off":
            return int(os.environ.get("AIW_LLM_MAX_TOKENS_THINKING", "16384"))
        return int(os.environ.get("AIW_LLM_MAX_TOKENS", "4096"))

    def _extras(self) -> dict:
        if not self._extras_ok:
            return {}
        if self.thinking != "off":
            return {"reasoning_effort": self.effort} if self.effort else {}
        return {"thinking": {"type": "disabled"}, "enable_thinking": False}

    def _usage(self, data: dict) -> None:
        """v3.9.24: tokens of one HTTP call -> the usage sink (no-op when none is installed)."""
        try:
            from .. import usage as _u
            _u.record(self.model, (data or {}).get("usage"))
        except Exception:  # noqa: BLE001
            pass

    async def _post_live(self, c: httpx.AsyncClient, body: dict, req) -> dict:
        """With thinking on and someone watching, stream the reply so the reasoning shows up while the model is
        still thinking (a DeepSeek step thinks 60-70 s; the UI showed only "正在思考…"). Any trouble with the stream
        falls back to the plain request for good."""
        cb = getattr(req, "on_reasoning", None)
        if not (cb and self._stream_ok and self.thinking != "off"):
            return await self._post(c, body)
        try:
            return await self._stream(c, body, cb)
        except (httpx.HTTPStatusError, ValueError, KeyError, TypeError):
            self._stream_ok = False
            return await self._post(c, body)

    async def _stream(self, c: httpx.AsyncClient, body: dict, cb) -> dict:
        content, reasoning, calls, finish, usage = [], [], {}, None, {}
        payload = {**body, **self._extras(), "stream": True, "stream_options": {"include_usage": True}}
        async with c.stream("POST", self.url, json=payload, headers={"Authorization": f"Bearer {self.key}"}) as r:
            r.raise_for_status()
            if "json" in (r.headers.get("content-type") or "") and "event-stream" not in r.headers.get("content-type", ""):
                data = json.loads(await r.aread())  # gateway ignored stream=true
                self._usage(data)
                return data
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                ev = json.loads(chunk)
                usage = ev.get("usage") or usage
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    finish = ch.get("finish_reason") or finish
                    r_part = d.get("reasoning_content") or d.get("reasoning")
                    if r_part:
                        reasoning.append(r_part)
                        try:
                            await cb("".join(reasoning))
                        except Exception:  # noqa: BLE001  the UI hook must never break the call
                            pass
                    if d.get("content"):
                        content.append(d["content"])
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "type": "function",
                                                                       "function": {"name": "", "arguments": ""}})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        a = fn.get("arguments")
                        slot["function"]["arguments"] += a if isinstance(a, str) else (json.dumps(a) if a else "")
        msg = {"role": "assistant", "content": "".join(content) or None,
               "reasoning_content": "".join(reasoning) or None,
               "tool_calls": [calls[i] for i in sorted(calls)] or None}
        self._usage({"usage": usage})
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}

    async def _post(self, c: httpx.AsyncClient, body: dict, tries: int | None = None) -> dict:
        """POST with retries on network hiccups and 429/5xx (a gateway blip used to fail the whole task)."""
        import asyncio
        tries = tries or int(os.environ.get("AIW_LLM_RETRIES", "3"))
        for i in range(tries):
            try:
                r = await c.post(self.url, json={**body, **self._extras()}, headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code == 400 and self._extras():  # gateway rejects the thinking switches: drop them for good
                    self._extras_ok = False
                    r = await c.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code in (408, 409, 425, 429, 500, 502, 503, 504) and i < tries - 1:
                    await asyncio.sleep(2 * (i + 1) ** 2)
                    continue
                r.raise_for_status()
                data = r.json()
                self._usage(data)
                return data
            except (httpx.TransportError, ValueError) as e:  # connect/read errors, resets, a cut-off JSON body
                if i >= tries - 1:
                    raise
                await asyncio.sleep(2 * (i + 1) ** 2)
        raise RuntimeError("unreachable")

    async def describe(self, system: str, user: str, image_b64: str, max_tokens: int = 900) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": user},
                                         {"type": "image_url", "image_url": {"url": f"data:{_mime(image_b64)};base64,{image_b64}"}}]}]}
        # 3.8.8: at most 2 HTTP attempts for a picture (task-e63: 3 here x2 in the loop = ~24 s per failed read)
        from ..vision import attempts
        async with httpx.AsyncClient(timeout=120) as c:
            return strip_think((await self._post(c, body, attempts()))["choices"][0]["message"].get("content") or "")

    async def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        async with httpx.AsyncClient(timeout=90) as c:
            return strip_think((await self._post(c, body))["choices"][0]["message"].get("content") or "")


    async def chat(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 1500,
                   on_delta=None) -> dict:
        """Multi-turn chat with optional tools; streams the visible answer to on_delta (reasoning is never passed
        on: the chat shows no thinking). A gateway that rejects or ignores streaming gets the plain request."""
        body = {"model": self.model, "max_tokens": max_tokens, "messages": messages}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = "auto"
        wait = float(os.environ.get("AIW_CHAT_TIMEOUT_S", "120"))
        async with httpx.AsyncClient(timeout=wait) as c:
            data = None
            if on_delta and self._stream_ok:
                try:
                    data = await self._chat_stream(c, body, on_delta)
                except (httpx.HTTPStatusError, ValueError, KeyError, TypeError):
                    self._stream_ok = False
                    data = None
            streamed = data is not None and data.get("_streamed")
            if data is None:
                data = await self._post(c, body)
            msg = ((data.get("choices") or [{}])[0].get("message")) or {}
        content = strip_think(msg.get("content") or "")
        if on_delta and content and not streamed:
            await on_delta(content)
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name"):
                calls.append({"id": tc.get("id") or f"call_{i}", "name": fn["name"], "args": parse_args(fn.get("arguments"))})
        return {"content": content, "tool_calls": calls}

    async def _chat_stream(self, c: httpx.AsyncClient, body: dict, on_delta) -> dict:
        content, calls, think, usage = [], {}, {"in": False, "buf": ""}, {}
        payload = {**body, **self._extras(), "stream": True}
        if os.environ.get("AIW_USAGE_STREAM", "1") != "0":
            payload["stream_options"] = {"include_usage": True}
        async with c.stream("POST", self.url, json=payload, headers={"Authorization": f"Bearer {self.key}"}) as r:
            if r.status_code == 400 and self._extras():   # the thinking switches are rejected: plain request instead
                self._extras_ok = False
                raise ValueError("stream rejected")
            r.raise_for_status()
            if "event-stream" not in (r.headers.get("content-type") or ""):
                data = json.loads(await r.aread())  # gateway ignored stream=true
                self._usage(data)
                return data
            async for line in r.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                ev = json.loads(chunk)
                usage = ev.get("usage") or usage
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    piece = d.get("content") or ""
                    if piece:
                        content.append(piece)
                        vis = _visible(think, piece)
                        if vis:
                            try:
                                await on_delta(vis)
                            except Exception:  # noqa: BLE001  a closed client never breaks the reply
                                pass
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "type": "function",
                                                                       "function": {"name": "", "arguments": ""}})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        a = fn.get("arguments")
                        slot["function"]["arguments"] += a if isinstance(a, str) else (json.dumps(a) if a else "")
        msg = {"role": "assistant", "content": "".join(content) or None, "tool_calls": [calls[i] for i in sorted(calls)] or None}
        self._usage({"usage": usage})
        return {"choices": [{"message": msg}], "_streamed": True}


def _visible(state: dict, piece: str) -> str:
    """Streamed content without <think>…</think> blocks some gateways put inside the answer."""
    out = ""
    buf = state["buf"] + piece
    state["buf"] = ""
    while buf:
        if state["in"]:
            j = buf.find("</think>")
            if j < 0:
                state["buf"] = buf[-8:]
                return out
            buf, state["in"] = buf[j + 8:], False
        else:
            j = buf.find("<think>")
            if j < 0:
                k = buf.rfind("<")
                if k >= 0 and "<think>".startswith(buf[k:]) and len(buf) - k < 7:
                    out += buf[:k]
                    state["buf"] = buf[k:]
                    return out
                return out + buf
            out += buf[:j]
            buf, state["in"] = buf[j + 7:], True
    return out


def reasoning_of(msg: dict) -> str:
    """The model's thinking: reasoning_content (DeepSeek, Qwen, GLM gateways), reasoning (OpenRouter), or a
    <think> block inside content."""
    r = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if isinstance(r, list):
        r = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in r)
    if not r:
        m = re.search(r"<think>(.*?)(</think>|$)", msg.get("content") or "", re.S)
        r = m.group(1) if m else ""
    return str(r).strip()


def strip_think(text: str) -> str:
    """Drop <think>...</think> blocks that some gateways leave inside content."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()


def _mime(b64: str) -> str:
    return "image/jpeg" if (b64 or "").startswith("/9j/") else "image/png"
