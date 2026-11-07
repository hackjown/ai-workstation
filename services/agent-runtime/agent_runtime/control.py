"""Pause / takeover / resume / stop (spec §18, §22, tests 6-8).

The loop calls `await control.checkpoint()` before every observe and every
action, so a pause lands between actions, never in the middle of one.
"""
from __future__ import annotations

import asyncio
from enum import Enum


class ControlState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    TAKEOVER = "takeover"
    STOPPED = "stopped"


class StopRequested(Exception):
    pass


# v3.9.26: who stopped a task. sessions.control() used to record every stop as by:"user" (a parent stopping its
# children, 立即发送 stopping the running task, deleting a thread, a dispatch time limit) and the loop wrote
# "stopped by user" for all of them.
STOP_SOURCES = ("user", "parent", "dispatch", "system", "thread_delete", "send_now")
STOP_TEXT = {"user": "stopped by user", "parent": "stopped by parent task", "dispatch": "stopped: dispatch time limit",
             "system": "stopped by system", "thread_delete": "stopped: thread deleted",
             "send_now": "stopped: a queued message was sent now"}
STOP_CN = {"user": "你手动停止", "parent": "主任务停止了它", "dispatch": "分派任务到时间被停止", "system": "系统停止",
           "thread_delete": "对话被删除时停止", "send_now": "为立即发送的消息让路而停止"}


def stop_text(source: str, reason: str = "") -> str:
    t = STOP_TEXT.get(source or "user", f"stopped ({source})")
    return t + (f": {reason}" if reason and source != "user" else "")


class AgentControl:
    def __init__(self):
        self.state = ControlState.RUNNING
        self._resume = asyncio.Event()
        self._resume.set()
        self.resumed_after_takeover = False
        # set the moment the user presses stop: the loop cancels whatever it is awaiting (a 60 s thinking call,
        # a long shell command) instead of noticing at the next checkpoint (task-4e6: 24 s+ after the click)
        self.stopped = asyncio.Event()
        self.stop_source = "user"
        self.stop_reason = ""

    def pause(self):
        if self.state == ControlState.RUNNING:
            self.state = ControlState.PAUSED
            self._resume.clear()

    def takeover(self):
        if self.state != ControlState.STOPPED:
            self.state = ControlState.TAKEOVER
            self._resume.clear()

    def resume(self):
        if self.state in (ControlState.PAUSED, ControlState.TAKEOVER):
            self.resumed_after_takeover = self.state == ControlState.TAKEOVER
            self.state = ControlState.RUNNING
            self._resume.set()

    def stop(self, source: str = "user", reason: str = ""):
        if self.state != ControlState.STOPPED:   # the first stop says who stopped it
            self.stop_source = source if source in STOP_SOURCES else "system"
            self.stop_reason = str(reason or "")[:200]
        self.state = ControlState.STOPPED
        self._resume.set()
        self.stopped.set()

    @property
    def is_active(self) -> bool:
        return self.state == ControlState.RUNNING

    async def checkpoint(self) -> bool:
        """Blocks while paused/taken over. Returns True if we were blocked
        (desktop may have changed: caller must re-observe)."""
        blocked = not self._resume.is_set()
        await self._resume.wait()
        if self.state == ControlState.STOPPED:
            raise StopRequested()
        return blocked
