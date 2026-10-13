"""Agent events (spec §30.12: every action produces an Event/Audit record)."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class EventType(str, Enum):
    TASK_STARTED = "task.started"
    TASK_FINISHED = "task.finished"
    TASK_FAILED = "task.failed"
    PLAN = "agent.plan"
    OBSERVATION = "agent.observation"
    THINKING = "agent.thinking"
    DECISION = "agent.decision"
    POLICY = "agent.policy"
    ACTION = "agent.action"
    ACTION_RESULT = "agent.action_result"
    VALIDATION = "agent.validation"
    RECOVERY = "agent.recovery"
    PAUSED = "agent.paused"
    RESUMED = "agent.resumed"
    TAKEOVER = "agent.takeover"
    CONFIRM_REQUIRED = "agent.confirm_required"
    MESSAGE = "agent.message"
    ERROR = "agent.error"


class AgentEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str
    type: EventType
    ts: float = Field(default_factory=time.time)
    step: Optional[int] = None
    data: dict[str, Any] = Field(default_factory=dict)
