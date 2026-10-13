from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"          # user paused; agent idle
    TAKEOVER = "takeover"      # user is driving the desktop
    WAITING_CONFIRM = "waiting_confirm"  # L3 action awaiting user
    WAITING_INPUT = "waiting_input"      # agent asked the user a question
    QUEUED = "queued"                    # follow-up waiting for the machine
    INTERRUPTED = "interrupted"          # API restarted mid-run
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: f"task-{uuid.uuid4().hex[:10]}")
    prompt: str
    machine_id: str = "ai-linux-001"
    status: TaskStatus = TaskStatus.PENDING
    max_steps: int = 40
    timeout_s: float = 600.0
    created_at: float = Field(default_factory=time.time)
    result: Optional[str] = None
    thread_id: Optional[str] = None
    context: Optional[str] = None      # earlier turns of the thread + memory, prepended for the model
    source: str = "chat"               # chat | routine | retry | resume
