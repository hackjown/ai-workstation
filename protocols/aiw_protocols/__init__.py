"""Shared wire protocols for AI Workstation (spec §16, §17, §23).

Every component (vm-agent, computer-runtime, tool-router, agent-runtime, api)
speaks these models. Nothing here imports a third-party project's internals.
"""
from .command import (
    ACTIONS,
    ActionType,
    Command,
    CommandResult,
    Observation,
    new_command_id,
)
from .events import AgentEvent, EventType
from .task import Task, TaskStatus
from .vm import VMState, VMStatus

__all__ = [
    "ACTIONS",
    "ActionType",
    "Command",
    "CommandResult",
    "Observation",
    "new_command_id",
    "AgentEvent",
    "EventType",
    "Task",
    "TaskStatus",
    "VMState",
    "VMStatus",
]
