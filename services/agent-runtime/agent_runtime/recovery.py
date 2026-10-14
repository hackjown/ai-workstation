"""Recovery strategies after a failed validation. Escalates with repeated failures."""
from __future__ import annotations


class Recovery:
    def __init__(self, computer):
        self.computer = computer

    async def recover(self, consecutive_failures: int, last_action: str | None) -> str:
        if consecutive_failures == 1:
            return ("Previous action had no visible effect. If it was a click by x/y, the point was probably off: "
                    "on a web page use browser_click with the element's text; otherwise click_element with a description.")
        if consecutive_failures == 2:
            await self.computer.execute("computer.keypress", {"key": "Escape"})
            return "Pressed Escape to dismiss any popup/menu. Re-locate the target precisely."
        if consecutive_failures == 3:
            # no blind click here: (5, 5) is the XFCE Applications menu and opened it
            return ("Three misses in a row. Change method: on a web page use browser_click with the element's text "
                    "or browser_fill with the field label; elsewhere use click_element with a description, a keyboard "
                    "shortcut, or a URL.")
        return "Repeated failures. If the task cannot be completed, call fail with the reason."
