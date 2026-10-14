"""Computer Runtime <-> VM Agent command protocol (spec §16 / §17).

Request:  {"id": "action-001", "type": "computer.click", "payload": {"x": 820, "y": 450}}
Response: {"id": "action-001", "success": true, "result": {}, "observation": {"screenshot": "..."}}
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    # computer.*
    SCREENSHOT = "computer.screenshot"
    CLICK = "computer.click"
    DOUBLE_CLICK = "computer.double_click"
    RIGHT_CLICK = "computer.right_click"
    MOVE = "computer.move"
    DRAG = "computer.drag"
    TYPE = "computer.type"
    KEYPRESS = "computer.keypress"
    HOTKEY = "computer.hotkey"
    SCROLL = "computer.scroll"
    OPEN_APP = "computer.open_app"
    CLOSE_APP = "computer.close_app"
    CURSOR = "computer.cursor_position"
    SCREEN_SIZE = "computer.screen_size"
    # shell.*
    SHELL_EXEC = "shell.exec"
    # browser.*
    BROWSER_OPEN = "browser.open"
    BROWSER_NAVIGATE = "browser.navigate"
    BROWSER_BACK = "browser.back"
    BROWSER_FORWARD = "browser.forward"
    BROWSER_CLICK = "browser.click"  # click a page element found by its text (DOM), with the real mouse
    BROWSER_FILL = "browser.fill"  # type into a form field found by its label/placeholder
    # files.*
    FILES_LIST = "files.list"
    FILES_READ = "files.read"
    FILES_WRITE = "files.write"
    FILES_DOWNLOAD = "files.download"
    FILES_UPLOAD = "files.upload"
    # system
    HEALTH = "system.health"


ACTIONS: list[str] = [a.value for a in ActionType]

# Required payload keys per action. Validated on both sides.
PAYLOAD_SCHEMA: dict[str, list[str]] = {
    ActionType.CLICK.value: ["x", "y"],
    ActionType.DOUBLE_CLICK.value: ["x", "y"],
    ActionType.RIGHT_CLICK.value: ["x", "y"],
    ActionType.MOVE.value: ["x", "y"],
    ActionType.DRAG.value: ["x1", "y1", "x2", "y2"],
    ActionType.TYPE.value: ["text"],
    ActionType.KEYPRESS.value: ["key"],
    ActionType.HOTKEY.value: ["keys"],
    ActionType.SCROLL.value: ["dx", "dy"],
    ActionType.OPEN_APP.value: ["name"],
    ActionType.CLOSE_APP.value: ["name"],
    ActionType.SHELL_EXEC.value: ["command"],
    ActionType.BROWSER_OPEN.value: [],
    ActionType.BROWSER_NAVIGATE.value: ["url"],
    ActionType.BROWSER_CLICK.value: ["text"],
    ActionType.BROWSER_FILL.value: ["field", "text"],
    ActionType.FILES_LIST.value: ["path"],
    ActionType.FILES_READ.value: ["path"],
    ActionType.FILES_WRITE.value: ["path", "content"],
    ActionType.FILES_DOWNLOAD.value: ["url"],
    ActionType.FILES_UPLOAD.value: ["path", "content_b64"],
}


def new_command_id(prefix: str = "action") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Command(BaseModel):
    id: str = Field(default_factory=new_command_id)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # Ask the agent to attach a screenshot after executing.
    observe: bool = False
    timeout: float = 30.0

    def validate_payload(self) -> Optional[str]:
        if self.type not in ACTIONS:
            return f"unknown action type: {self.type}"
        missing = [k for k in PAYLOAD_SCHEMA.get(self.type, []) if k not in self.payload]
        if missing:
            return f"missing payload keys for {self.type}: {missing}"
        return None


class Observation(BaseModel):
    screenshot: Optional[str] = None  # base64 PNG or artifact id
    width: Optional[int] = None
    height: Optional[int] = None
    cursor: Optional[tuple[int, int]] = None


class CommandResult(BaseModel):
    id: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    observation: Optional[Observation] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
