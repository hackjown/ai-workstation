"""image_edit tool: AI image editing (a prompt changes a photo), then exact touch-ups.

Generative models (gpt-image-1, Gemini 2.5 Flash Image "nano banana", Qwen-Image-Edit, Seedream...) can make
changes Photopea's scripts cannot, such as raising a cabinet and redrawing the wall behind it. They also tend to
re-render the whole picture: the resolution changes, the framing drifts a little, and small text (posters,
labels, signs) comes back garbled. So this module also:
  - scales the result back to the original's size (keep_size),
  - finds how the AI shifted/scaled the picture (global alignment, PIL only), and
  - pastes chosen regions of the original back over the result, aligned and feathered (restore).
Further touch-ups (text, colour, a layered PSD) are the photopea tool's job.

The model is called through the same OpenAI-compatible gateway as the agent's other models:
  - /images/edits (multipart) for gpt-image-*/dall-e style models,
  - /chat/completions with the image attached for chat models that answer with an image (Gemini image models on
    OpenRouter/one-api/new-api style gateways put it in message.images, in content parts, or as a data URI).
Each is tried, the likelier one first.
"""
from __future__ import annotations

import base64
import io
import re
from typing import Optional

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

IMAGES_API_HINT = re.compile(r"gpt-image|dall-e|dalle|flux|seedream|seededit|qwen-image|kolors|stable-diffusion|sdxl|hidream",
                             re.I)
_DATA_URI = re.compile(r"data:image/[a-zA-Z+.-]+;base64,([A-Za-z0-9+/=\s]+)")
_HTTP_IMG = re.compile(r"https?://[^\s)\"'<>]+")


class ImageEditError(RuntimeError):
    pass


def prompt_for(instruction: str) -> str:
    """Wrap the user's change so the model keeps everything else."""
    return (f"Edit this photo. Change only this: {instruction.strip()}\n"
            "Keep everything else exactly as it is: the same camera angle, framing, lighting, colours, every other "
            "object and its position, and all text and logos unchanged. Photorealistic, seamless. "
            "Do not add new objects, text or watermarks.")


# ---------------------------------------------------------------- model calls

def _png(img_bytes: bytes, max_side: int = 2048) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    if max(im.size) > max_side:
        k = max_side / max(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


async def _fetch(c: httpx.AsyncClient, url: str) -> bytes:
    r = await c.get(url)
    r.raise_for_status()
    return r.content


async def _from_images_api(c, base, key, model, png: bytes, prompt: str) -> bytes:
    files = [("image[]", ("image.png", png, "image/png"))]
    data = {"model": model, "prompt": prompt, "n": "1"}
    if re.search(r"gpt-image", model, re.I):
        data["input_fidelity"] = "high"  # keeps faces, logos and details of the input closer
    r = await c.post(f"{base}/images/edits", headers={"Authorization": f"Bearer {key}"}, data=data, files=files)
    if r.status_code == 400 and "input_fidelity" in r.text:
        data.pop("input_fidelity", None)
        r = await c.post(f"{base}/images/edits", headers={"Authorization": f"Bearer {key}"}, data=data, files=files)
    if r.status_code >= 400:
        raise ImageEditError(f"/images/edits {r.status_code}: {r.text[:300]}")
    d = (r.json().get("data") or [{}])[0]
    if d.get("b64_json"):
        return base64.b64decode(d["b64_json"])
    if d.get("url"):
        return await _fetch(c, d["url"])
    raise ImageEditError("/images/edits returned no image")


def _images_in(msg) -> list[str]:
    """Image payloads (data URIs, bare base64 or URLs) anywhere in a chat message, in order."""
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for k in ("url", "b64_json", "data", "image_url", "inline_data", "inlineData", "images", "content", "parts",
                      "image", "result"):
                if k in x:
                    v = x[k]
                    if isinstance(v, str) and k in ("url", "b64_json", "data", "image_url", "image", "result"):
                        if v.startswith(("data:image", "http")) or (k in ("b64_json", "data", "result") and len(v) > 200):
                            out.append(v)
                    else:
                        walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, str):
            out.extend(m.group(0) for m in _DATA_URI.finditer(x))
            if not out:
                out.extend(u for u in _HTTP_IMG.findall(x) if re.search(r"\.(png|jpe?g|webp)(\?|$)|image", u, re.I))
    walk(msg)
    return out


async def _decode(c, payload: str) -> bytes:
    if payload.startswith("http"):
        return await _fetch(c, payload)
    m = _DATA_URI.match(payload)
    return base64.b64decode(re.sub(r"\s", "", m.group(1) if m else payload))


async def _from_chat(c, base, key, model, png: bytes, prompt: str) -> bytes:
    b64 = base64.b64encode(png).decode()
    body = {"model": model, "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
    r = await c.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code == 400 and "modalities" in r.text:
        body.pop("modalities")
        r = await c.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code >= 400:
        raise ImageEditError(f"/chat/completions {r.status_code}: {r.text[:300]}")
    msg = ((r.json().get("choices") or [{}])[0]).get("message") or {}
    found = _images_in(msg)
    if not found:
        txt = msg.get("content") if isinstance(msg.get("content"), str) else ""
        raise ImageEditError(f"{model} answered without an image{': ' + txt[:200] if txt else ''}")
    return await _decode(c, found[-1])


async def edit(base_url: str, key: str, model: str, img_bytes: bytes, prompt: str, timeout: float = 300) -> bytes:
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    png = _png(img_bytes)
    firm = prompt + "\nReturn the edited picture itself as an image. Do not answer with text only."
    if IMAGES_API_HINT.search(model):
        order = [(_from_images_api, prompt), (_from_chat, firm)]
    else:  # task-f08: qwen image models answered "done, see above" in text twice before sending a picture
        order = [(_from_chat, prompt), (_from_chat, firm), (_from_images_api, prompt)]
    errs = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        for fn, p in order:
            try:
                out = await fn(c, base, key, model, png, p)
                Image.open(io.BytesIO(out)).verify()
                return out
            except (ImageEditError, httpx.HTTPError, ValueError, OSError) as e:
                errs.append(str(e)[:300])
    raise ImageEditError(" | ".join(errs))


# ---------------------------------------------------------------- alignment and restore

def _gray(im: Image.Image, w: int) -> Image.Image:
    return im.convert("L").resize((w, max(1, round(im.height * w / im.width))), Image.BILINEAR)


def _score(a: Image.Image, b: Image.Image, s: float, dx: int, dy: int) -> float:
    """Mean abs difference between b and a scaled by s (about the centre) then shifted by (dx, dy), on the
    central 70% of b (the borders are where reframing adds or drops content)."""
    W, H = b.size
    sw, sh = max(1, round(a.width * s)), max(1, round(a.height * s))
    a2 = a.resize((sw, sh), Image.BILINEAR)
    ox, oy = (W - sw) / 2 + dx, (H - sh) / 2 + dy
    l, t, r, btm = round(W * .15), round(H * .15), round(W * .85), round(H * .85)
    ca = a2.crop((round(l - ox), round(t - oy), round(r - ox), round(btm - oy)))
    cb = b.crop((l, t, r, btm))
    return ImageStat.Stat(ImageChops.difference(ca, cb)).mean[0]


def align(orig: Image.Image, result: Image.Image) -> tuple[float, float, float, float]:
    """Similarity (scale s, shift dx, dy in result pixels, about the centre) that maps the original onto the
    result after the result was resized to the original's size. Returns (s, dx, dy, residual)."""
    best = (1.0, 0, 0, _score(_gray(orig, 160), _gray(result, 160), 1.0, 0, 0))
    a, b = _gray(orig, 160), _gray(result, 160)
    for si in range(-12, 13):            # scale 0.88 .. 1.12, shift up to +-12 px of 160
        s = 1 + si * 0.01
        for dy in range(-12, 13, 2):
            for dx in range(-12, 13, 2):
                v = _score(a, b, s, dx, dy)
                if v < best[3]:
                    best = (s, dx, dy, v)
    s0, dx0, dy0, _ = best
    k = 640 / 160
    a, b = _gray(orig, 640), _gray(result, 640)
    best = (s0, dx0 * k, dy0 * k, _score(a, b, s0, round(dx0 * k), round(dy0 * k)))
    for si in range(-3, 4):
        s = s0 + si * 0.0035
        for dy in range(round(dy0 * k) - 6, round(dy0 * k) + 7, 2):
            for dx in range(round(dx0 * k) - 6, round(dx0 * k) + 7, 2):
                v = _score(a, b, s, dx, dy)
                if v < best[3]:
                    best = (s, dx, dy, v)
    s, dx, dy, v = best
    f = orig.width / 640
    return s, dx * f, dy * f, v


def restore(orig: Image.Image, result: Image.Image, boxes: list, s: float, dx: float, dy: float,
            feather: int = 6) -> Image.Image:
    """Paste the original's boxes (l, t, r, b in original pixels) onto the result where the AI moved them."""
    W, H = result.size
    sw, sh = round(orig.width * s), round(orig.height * s)
    big = orig.convert("RGB").resize((sw, sh), Image.LANCZOS)
    ox, oy = round((W - sw) / 2 + dx), round((H - sh) / 2 + dy)
    layer = Image.new("RGB", (W, H))
    layer.paste(big, (ox, oy))
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    for bx in boxes:
        l, t, r, b = [float(x) for x in bx]
        d.rectangle([round(l * s + ox), round(t * s + oy), round(r * s + ox), round(b * s + oy)], fill=255)
    if feather:
        mask = mask.filter(ImageFilter.GaussianBlur(feather))
    out = result.convert("RGB").copy()
    out.paste(layer, (0, 0), mask)
    return out


def changed_percent(orig: Image.Image, res: Image.Image) -> float:
    """Share of the picture (in %) that clearly differs: catches a model that returns the input unchanged."""
    a, b = _gray(orig, 320), _gray(res, 320)
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > 40 else 0)
    return round(100 * ImageStat.Stat(diff).mean[0] / 255, 1)


def postprocess(orig_bytes: bytes, out_bytes: bytes, keep_size: bool = True, boxes: Optional[list] = None,
                feather: int = 6) -> tuple[bytes, dict]:
    orig = Image.open(io.BytesIO(orig_bytes)).convert("RGB")
    res = Image.open(io.BytesIO(out_bytes)).convert("RGB")
    info = {"ai_size": list(res.size), "orig_size": list(orig.size)}
    if keep_size or boxes:
        if res.size != orig.size:
            res = res.resize(orig.size, Image.LANCZOS)
    if boxes:
        s, dx, dy, v = align(orig, res)
        info["align"] = {"scale": round(s, 4), "dx": round(dx, 1), "dy": round(dy, 1), "residual": round(v, 1)}
        res = restore(orig, res, boxes, s, dx, dy, feather)
    if res.size == orig.size:
        info["changed"] = changed_percent(orig, res)
    info["size"] = list(res.size)
    buf = io.BytesIO()
    res.save(buf, "PNG")
    return buf.getvalue(), info
