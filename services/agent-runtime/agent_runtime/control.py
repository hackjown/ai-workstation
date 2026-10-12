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


class AgentControl:
    def __init__(self):
        self.state = ControlState.RUNNING
        self._resume = asyncio.Event()
        self._resume.set()
        self.resumed_after_takeover = False

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

    def stop(self):
        self.state = ControlState.STOPPED
        self._resume.set()

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
