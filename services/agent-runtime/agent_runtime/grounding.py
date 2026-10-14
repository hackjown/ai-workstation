"""Grounding: turn what a vision model says about the screen into exact screen pixels.

Why this exists: Qwen3-VL, GLM-4.xV / GLM-5V and Gemini answer with coordinates normalized to 0-1000,
whatever pixel size the prompt claims. Treating those as pixels of a 1280x720 screenshot puts every
click off by tens to hundreds of pixels (task-25a: "注册" at (1322,146) was clicked at (1029,199)).

* coord_mode(model)         -> "norm1000" | "pixel"  (env AIW_VISION_COORDS overrides)
* convert_desc(text, ...)   -> rewrite every "(x, y)" in a screen description into screenshot pixels
* parse_point(text)         -> (x, y) from {"x":..,"y":..}, [x, y], [x1,y1,x2,y2] (bbox -> center), <point>
* locate(eyes, img, ...)    -> two passes: find the element on the whole screen, then zoom into a crop
                               around it and find it again, for a click within a few pixels
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from typing import Optional

from PIL import Image

_NORM_MODELS = re.compile(r"qwen3|qwen-?3|qvq|glm|gemini|kimi|ui-?tars|seed|doubao", re.I)
_PIXEL_MODELS = re.compile(r"qwen2\.?5|qwen-?2|gpt|claude|o\d|llava|minicpm", re.I)


def coord_mode(model: str | None) -> str:
    env = (os.environ.get("AIW_VISION_COORDS") or "auto").lower()
    if env in ("norm1000", "pixel"):
        return env
    m = model or ""
    if _PIXEL_MODELS.search(m) and not re.search(r"qwen3", m, re.I):
        return "pixel"
    return "norm1000" if _NORM_MODELS.search(m) else "pixel"


def model_name(router) -> str:
    try:
        return str(getattr(router.providers[0], "model", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def coord_instruction(mode: str, shown: tuple[int, int]) -> str:
    if mode == "norm1000":
        return ("Give every position as (x, y) normalized to 0-1000: x=0 is the left edge, x=1000 the right edge, "
                "y=0 the top, y=1000 the bottom.")
    return f"The screenshot is {shown[0]}x{shown[1]} pixels. Give every position as (x, y) pixel coordinates in it."


_PAIR = re.compile(r"([(\[（])\s*(\d{1,4}(?:\.\d+)?)\s*[,，]\s*(\d{1,4}(?:\.\d+)?)\s*([)\]）])")


def to_pixels(x: float, y: float, mode: str, size: tuple[int, int]) -> tuple[int, int]:
    w, h = size
    if mode == "norm1000":
        x, y = x * w / 1000.0, y * h / 1000.0
    return max(0, min(w - 1, int(round(x)))), max(0, min(h - 1, int(round(y))))


def convert_desc(desc: str, shown: tuple[int, int], mode: str) -> str:
    """Rewrite coordinate pairs in a description into pixels of the screenshot the tool model is told about."""
    if mode != "norm1000" or not desc:
        return desc

    def rep(m):
        x, y = to_pixels(float(m.group(2)), float(m.group(3)), mode, shown)
        return f"{m.group(1)}{x}, {y}{m.group(4)}"
    return _PAIR.sub(rep, desc)


def parse_point(text: str) -> Optional[tuple[float, float]]:
    """A point from the model's answer. None when it says the element is not visible."""
    if not text:
        return None
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{[^{}]*\}", t, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                if d.get("found") is False or ("x" in d and d["x"] is None):
                    return None
                box = d.get("bbox_2d") or d.get("bbox")
                if isinstance(box, list) and len(box) == 4:
                    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                if d.get("x") is not None and d.get("y") is not None:
                    return float(d["x"]), float(d["y"])
                pt = d.get("point") or d.get("point_2d")
                if isinstance(pt, list) and len(pt) == 2:
                    return float(pt[0]), float(pt[1])
        except (ValueError, TypeError):
            pass
    if re.search(r"not (visible|found)|没有找到|看不到|不可见|null", t, re.I) and not re.search(r"\d+\s*[,，]\s*\d+", t):
        return None
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", t)]
    if len(nums) >= 4 and re.search(r"bbox|box|\[\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\]", t):
        return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    if len(nums) >= 2:
        return nums[0], nums[1]
    return None


def _jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


LOCATE_SYSTEM = "You are a precise GUI grounding model. You answer with JSON only."


def _locate_prompt(target: str, mode: str, size: tuple[int, int], zoomed: bool) -> str:
    where = ("This is a zoomed-in crop of the screen. " if zoomed else "")
    return (f"{where}Find this UI element: {target}\n"
            f"{coord_instruction(mode, size)}\n"
            'Answer ONLY with JSON: {"x": <center x>, "y": <center y>} for the clickable center of that element, '
            'or {"found": false} if it is not in the image. No other text.')


async def locate(eyes, full: Image.Image, target: str, mode: str, zoom: bool = True,
                 max_width: int = 1280) -> Optional[tuple[int, int]]:
    """Screen pixel (in `full`'s coordinates) of the element described by `target`, or None."""
    img = full
    if full.width > max_width:
        img = full.resize((max_width, int(full.height * max_width / full.width)))
    ans = await eyes.describe(LOCATE_SYSTEM, _locate_prompt(target, mode, img.size, False), _jpeg_b64(img), 200)
    p = parse_point(ans)
    if p is None:
        return None
    x, y = to_pixels(p[0], p[1], mode, img.size)
    sx, sy = int(round(x * full.width / img.width)), int(round(y * full.height / img.height))
    if not zoom:
        return sx, sy
    # second pass: a crop around the first guess, enlarged, so small buttons get many pixels
    cw, ch = min(full.width, 520), min(full.height, 320)
    left = max(0, min(full.width - cw, sx - cw // 2))
    top = max(0, min(full.height - ch, sy - ch // 2))
    crop = full.crop((left, top, left + cw, top + ch))
    k = 2 if cw * 2 <= 1400 else 1
    big = crop.resize((cw * k, ch * k), Image.LANCZOS) if k > 1 else crop
    try:
        ans2 = await eyes.describe(LOCATE_SYSTEM, _locate_prompt(target, mode, big.size, True), _jpeg_b64(big), 200)
    except Exception:  # noqa: BLE001
        return sx, sy
    p2 = parse_point(ans2)
    if p2 is None:
        return sx, sy
    zx, zy = to_pixels(p2[0], p2[1], mode, big.size)
    fx, fy = left + zx / k, top + zy / k
    # a refinement that jumps far away means the zoomed pass latched onto something else
    if abs(fx - sx) > cw * 0.45 or abs(fy - sy) > ch * 0.45:
        return sx, sy
    return int(round(fx)), int(round(fy))
