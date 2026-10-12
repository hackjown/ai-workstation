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
    parts = [f"TASK: {req.task}", f"SCREEN: {req.screen[0]}x{req.screen[1]} (screenshot attached, coordinates in its pixels)",
             f"PREVIOUS STEPS:\n{hist}"]
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
