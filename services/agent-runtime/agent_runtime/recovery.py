"""Recovery strategies after a failed validation. Escalates with repeated failures."""
from __future__ import annotations


class Recovery:
    def __init__(self, computer):
        self.computer = computer

    async def recover(self, consecutive_failures: int, last_action: str | None) -> str:
        if consecutive_failures == 1:
            return "Previous action had no effect or failed. Re-read the screenshot and try a different target."
        if consecutive_failures == 2:
            await self.computer.execute("computer.keypress", {"key": "Escape"})
            return "Pressed Escape to dismiss any popup/menu. Re-locate the target precisely."
        if consecutive_failures == 3:
            await self.computer.execute("computer.click", {"x": 5, "y": 5})  # neutral focus reset
            return "Reset focus. Consider a different approach (keyboard shortcut, shell, or URL)."
        return "Repeated failures. If the task cannot be completed, call fail with the reason."
