from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class VMState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class VMStatus(BaseModel):
    vm_id: str
    name: Optional[str] = None
    state: VMState = VMState.UNKNOWN
    cpus: Optional[int] = None
    mem_bytes: Optional[int] = None
    uptime_s: Optional[int] = None
    ip: Optional[str] = None
