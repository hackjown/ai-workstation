"""Validator: did the action do anything? (page-change detection, spec §18)."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageChops

VISUAL_ACTIONS = {"computer.click", "computer.double_click", "computer.right_click", "computer.type",
                  "computer.keypress", "computer.hotkey", "computer.scroll", "computer.drag",
                  "computer.open_app", "computer.close_app", "browser.open", "browser.navigate",
                  "browser.back", "browser.forward"}


def change_ratio(a_b64: str, b_b64: str) -> float:
    a = Image.open(io.BytesIO(base64.b64decode(a_b64))).convert("L").resize((96, 60))
    b = Image.open(io.BytesIO(base64.b64decode(b_b64))).convert("L").resize((96, 60))
    d = ImageChops.difference(a, b)
    return d.point(lambda p: 255 if p > 20 else 0).histogram()[255] / (96 * 60)


@dataclass
class Validation:
    success: bool
    changed: float | None
    message: str


class Validator:
    def __init__(self, min_change: float = 0.002):
        self.min_change = min_change

    def check(self, action: str | None, ok: bool, error: str | None, before: str | None, after: str | None) -> Validation:
        if not ok:
            return Validation(False, None, f"action failed: {error}")
        if action in VISUAL_ACTIONS and before and after:
            r = change_ratio(before, after)
            if r < self.min_change and action not in ("computer.scroll", "computer.keypress", "computer.hotkey"):
                return Validation(False, r, f"no visible change after {action} ({r:.3%})")
            return Validation(True, r, f"screen changed {r:.1%}")
        return Validation(True, None, "ok")
