"""LLM Router (spec §30.15: swapping GPT/Claude/Gemini never touches Computer Runtime).

Each step the agent sends one LLMRequest (system, task, compact history text,
latest screenshot) and gets back one LLMDecision (thought + one tool call).
Stateless per step keeps context bounded and makes pause/takeover/resume
trivial: the next step just observes whatever the desktop looks like now.
"""
from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMRequest:
    system: str
    task: str
    history: list[str]
    screenshot_b64: Optional[str]
    tools: list[dict]
    screen: tuple[int, int] = (0, 0)
    note: Optional[str] = None  # e.g. "user took over and returned control"
    screen_desc: Optional[str] = None  # set when a separate vision model read the screenshot


@dataclass
class LLMDecision:
    thought: str
    tool: str
    args: dict = field(default_factory=dict)
    raw: Optional[dict] = None
    usage: dict = field(default_factory=dict)


class LLMProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    async def decide(self, req: LLMRequest) -> LLMDecision: ...

    async def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        """Plain text completion (planning, summaries). Optional per provider."""
        raise NotImplementedError

    async def describe(self, system: str, user: str, image_b64: str, max_tokens: int = 900) -> str:
        """Look at an image and answer in text (the 'eyes' role). Optional per provider."""
        raise NotImplementedError


class ScriptedProvider(LLMProvider):
    """Replays a fixed list of decisions. For tests and offline demos."""

    name = "scripted"

    def __init__(self, script: list[tuple[str, dict]]):
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    async def decide(self, req: LLMRequest) -> LLMDecision:
        self.requests.append(req)
        if not self.script:
            return LLMDecision("script exhausted", "fail", {"reason": "script exhausted"})
        tool, args = self.script.pop(0)
        return LLMDecision(f"scripted {tool}", tool, args)


def user_text(req: LLMRequest) -> str:
    hist = "\n".join(req.history[-25:]) or "(none yet)"
    if req.screen_desc:
        screen = (f"SCREEN: {req.screen[0]}x{req.screen[1]}. No image is attached: a vision model looked at the current "
                  f"screenshot for you. Coordinates below are pixels of that screenshot; use them for click/type.\n"
                  f"SCREEN DESCRIPTION:\n{req.screen_desc}")
    elif req.screenshot_b64:
        screen = f"SCREEN: {req.screen[0]}x{req.screen[1]} (screenshot attached, coordinates in its pixels)"
    else:
        screen = "SCREEN: not available this step"
    parts = [f"TASK: {req.task}", screen, f"PREVIOUS STEPS:\n{hist}"]
    if req.note:
        parts.append(f"NOTE: {req.note}")
    parts.append("Decide the single next tool call. Briefly state your reasoning first.")
    return "\n\n".join(parts)


class LLMRouter:
    """Picks a provider by name; falls back in order on provider errors."""

    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise ValueError("no LLM providers configured")
        self.providers = providers

    @classmethod
    def from_settings(cls, model: str | None = None, fallback: str | None = None,
                      api_key: str | None = None, base_url: str | None = None,
                      tool_choice: dict | None = None) -> "LLMRouter":
        """Models picked in the web console (OpenAI-compatible gateway). Falls back to env config."""
        from .providers.openai import OpenAIProvider
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key or not (model or fallback):
            return cls.from_env()
        tc = tool_choice or {}
        ps = [OpenAIProvider(model=m, api_key=key, base_url=base_url, tool_choice=tc.get(m)) for m in (model, fallback) if m]
        return cls(ps)

    @classmethod
    def from_env(cls) -> "LLMRouter":
        from .providers.anthropic import AnthropicProvider
        from .providers.google import GoogleProvider
        from .providers.openai import OpenAIProvider
        order = os.environ.get("AIW_LLM_ORDER", "anthropic,openai,google").split(",")
        avail = {"anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY"),
                 "openai": (OpenAIProvider, "OPENAI_API_KEY"),
                 "google": (GoogleProvider, "GEMINI_API_KEY")}
        ps = [avail[n][0]() for n in order if n in avail and os.environ.get(avail[n][1])]
        return cls(ps)

    async def decide(self, req: LLMRequest) -> LLMDecision:
        last = None
        for p in self.providers:
            try:
                return await p.decide(req)
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"all LLM providers failed: {last}")

    async def describe(self, system: str, user: str, image_b64: str, max_tokens: int = 900) -> str:
        last = None
        for p in self.providers:
            try:
                return await p.describe(system, user, image_b64, max_tokens)
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"no provider could describe the image: {last}")

    async def complete(self, system: str, user: str, max_tokens: int = 800) -> str:
        last = None
        for p in self.providers:
            try:
                return await p.complete(system, user, max_tokens)
            except NotImplementedError as e:
                last = e
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"no provider could complete: {last}")
