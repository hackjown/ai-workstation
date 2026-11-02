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
v3.9.15: generate() makes a picture from words (/images/generations), and edit_chain/generate_chain try the
default image endpoint, then the other one (设置 → 模型 → 生图接口), so one broken endpoint never stops the job.
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
    field = "image[]"
    data = {"model": model, "prompt": prompt, "n": "1"}
    if re.search(r"gpt-image", model, re.I):
        data["input_fidelity"] = "high"  # keeps faces, logos and details of the input closer

    async def post():
        return await c.post(f"{base}/images/edits", headers={"Authorization": f"Bearer {key}"}, data=data,
                            files=[(field, ("image.png", png, "image/png"))])
    r = await post()
    if r.status_code in (400, 422) and "input_fidelity" in r.text:
        data.pop("input_fidelity", None)
        r = await post()
    if r.status_code in (400, 422) and re.search(r"image", r.text, re.I):
        # v3.9.15: some OpenAI-compatible servers (chatgpt2api, one-api forks) only read the field `image`
        field = "image"
        r = await post()
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


# ---------------------------------------------------------------- v3.9.15: text -> image

SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")


def gen_size(size: str) -> Optional[str]:
    """Normalise a size ('横版', '1536x1024', '16:9') to one gpt-image accepts; None = let the server choose."""
    s = str(size or "").strip().lower().replace("*", "x").replace("×", "x")
    if not s or s == "auto":
        return None
    if re.fullmatch(r"\d{3,4}x\d{3,4}", s):
        return s
    if re.search(r"横|landscape|wide|16:9|3:2|4:3", s):
        return "1536x1024"
    if re.search(r"竖|portrait|tall|9:16|2:3|3:4", s):
        return "1024x1536"
    if re.search(r"方|square|1:1", s):
        return "1024x1024"
    return None


async def _gen_images_api(c, base, key, model, prompt: str, size: Optional[str], n: int) -> list[bytes]:
    body = {"model": model, "prompt": prompt, "n": n}
    if size:
        body["size"] = size
    r = await c.post(f"{base}/images/generations", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code in (400, 422) and size and "size" in r.text:
        body.pop("size")
        r = await c.post(f"{base}/images/generations", headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code >= 400:
        raise ImageEditError(f"/images/generations {r.status_code}: {r.text[:300]}")
    out = []
    for d in (r.json().get("data") or []):
        if d.get("b64_json"):
            out.append(base64.b64decode(d["b64_json"]))
        elif d.get("url"):
            out.append(await _decode(c, d["url"]))
    if not out:
        raise ImageEditError("/images/generations returned no image")
    return out


async def _gen_chat(c, base, key, model, prompt: str, size: Optional[str], n: int) -> list[bytes]:
    text = prompt + (f"\n画幅：{size}" if size else "") + "\nReturn the picture itself as an image."
    body = {"model": model, "modalities": ["image", "text"], "messages": [{"role": "user", "content": text}]}
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
    return [await _decode(c, f) for f in found[:max(1, n)]]


async def generate(base_url: str, key: str, model: str, prompt: str, size: Optional[str] = None, n: int = 1,
                   timeout: float = 300) -> list[bytes]:
    """Text to image: /images/generations for gpt-image/dall-e style models, else a chat model that answers with one."""
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    n = max(1, min(4, int(n or 1)))
    order = [_gen_images_api, _gen_chat] if IMAGES_API_HINT.search(model) else [_gen_chat, _gen_images_api]
    errs = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        for fn in order:
            try:
                outs = await fn(c, base, key, model, prompt, size, n)
                for o in outs:
                    Image.open(io.BytesIO(o)).verify()
                return outs
            except (ImageEditError, httpx.HTTPError, ValueError, OSError) as e:
                errs.append(str(e)[:300])
    raise ImageEditError(" | ".join(errs))


# ---------------------------------------------------------------- v3.9.15: two endpoints, default + fallback

def chain_of(cfg: Optional[dict]) -> list[dict]:
    """The image endpoints to try, in order: cfg["chain"] (sessions._image_cfg), else the single legacy entry."""
    cfg = cfg or {}
    ch = [p for p in (cfg.get("chain") or []) if p.get("model") and p.get("key")]
    if ch:
        return ch
    if cfg.get("model") and cfg.get("key"):
        return [{"name": "网关", "base_url": cfg.get("base_url") or "", "key": cfg["key"], "model": cfg["model"]}]
    return []


async def edit_chain(cfg, img_bytes: bytes, prompt: str, timeout: float = 300) -> tuple[bytes, dict]:
    """edit() on the default endpoint; on any failure the next one. Returns (image, the endpoint that made it)."""
    errs = []
    for p in chain_of(cfg):
        try:
            return await edit(p.get("base_url") or "", p["key"], p["model"], img_bytes, prompt, timeout), p
        except Exception as e:  # noqa: BLE001
            errs.append(f"{p.get('name') or p['model']}（{p['model']}）: {str(e)[:300]}")
    raise ImageEditError(" || ".join(errs) or "no image model is set up")


async def generate_chain(cfg, prompt: str, size: Optional[str] = None, n: int = 1,
                         timeout: float = 300) -> tuple[list[bytes], dict]:
    errs = []
    for p in chain_of(cfg):
        try:
            return await generate(p.get("base_url") or "", p["key"], p["model"], prompt, size, n, timeout), p
        except Exception as e:  # noqa: BLE001
            errs.append(f"{p.get('name') or p['model']}（{p['model']}）: {str(e)[:300]}")
    raise ImageEditError(" || ".join(errs) or "no image model is set up")


def to_png(raw: bytes) -> bytes:
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


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


def move_regions(img_bytes: bytes, moves: list, feather: int = 6) -> tuple[bytes, dict]:
    """Deterministic geometry, no AI: each move copies the pixels of `box` [l,t,r,b] and scales them into `to`
    [l,t,r,b] (raise a cabinet = same box with a smaller top). For exact "raise / lower / widen / shift by N px"
    jobs that image models do loosely (task-4e2: four AI tries, none level)."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W, H = img.size
    out = img.copy()
    done = []
    clip = lambda b: [max(0, min(W, b[0])), max(0, min(H, b[1])), max(0, min(W, b[2])), max(0, min(H, b[3]))]
    tos = [clip([int(round(float(v))) for v in mv["to"]]) for mv in moves]
    for i, mv in enumerate(moves):
        box = clip([int(round(float(v))) for v in mv["box"]])
        to = tos[i]
        if box[2] - box[0] < 2 or box[3] - box[1] < 2 or to[2] - to[0] < 2 or to[3] - to[1] < 2:
            raise ValueError(f"empty box in move {mv}")
        piece = img.crop(tuple(box)).resize((to[2] - to[0], to[3] - to[1]), Image.LANCZOS)
        # an edge shared with another move stays hard: feathering it lets the old picture show through the joint
        others = [o for j, o in enumerate(tos) if j != i]
        hard = {"top": any(abs(o[3] - to[1]) <= 1 and o[0] < to[2] and o[2] > to[0] for o in others),
                "bottom": any(abs(o[1] - to[3]) <= 1 and o[0] < to[2] and o[2] > to[0] for o in others),
                "left": any(abs(o[2] - to[0]) <= 1 and o[1] < to[3] and o[3] > to[1] for o in others),
                "right": any(abs(o[0] - to[2]) <= 1 and o[1] < to[3] and o[3] > to[1] for o in others)}
        out.paste(piece, (to[0], to[1]), _feather_mask(piece.size, feather, hard))
        done.append({"box": box, "to": to})
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), {"moves": done}


def seam_mask(size, moves: list, band: int = 22) -> Image.Image:
    """Where geometric moves leave seams: each moved area's outer edges (not the joints between moves, which are
    exact) and whatever the moves vacated without covering it again."""
    W, H = size
    tos = [[float(v) for v in mv["to"]] for mv in moves]
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)

    def shared(t, side):
        for o in tos:
            if o is t:
                continue
            if side == "top" and abs(o[3] - t[1]) <= 1 and o[0] < t[2] and o[2] > t[0]:
                return True
            if side == "bottom" and abs(o[1] - t[3]) <= 1 and o[0] < t[2] and o[2] > t[0]:
                return True
        return False
    for t in tos:
        l, top, r, bot = t
        if not shared(t, "top"):
            d.rectangle([l - band, top - band, r + band, top + band], fill=255)
        if not shared(t, "bottom"):
            d.rectangle([l - band, bot - band, r + band, bot + band], fill=255)
        d.rectangle([l - band, top - band, l + band, bot + band], fill=255)
        d.rectangle([r - band, top - band, r + band, bot + band], fill=255)
    vac = Image.new("L", (W, H), 0)
    dv = ImageDraw.Draw(vac)
    for mv in moves:
        dv.rectangle([float(v) for v in mv["box"]], fill=255)
    for t in tos:
        dv.rectangle(t, fill=0)
    if vac.getbbox():
        m = ImageChops.lighter(m, vac.filter(ImageFilter.MaxFilter(2 * (band // 2) + 1)))
    return m.filter(ImageFilter.GaussianBlur(band / 3))


def blend_seams(geo_bytes: bytes, ai_bytes: bytes, moves: list, band: int = 22) -> bytes:
    """Keep the exact geometric result and take the AI's redraw only along the seams, so the AI cannot undo the move
    (task-0d5/238: asked to raise the counter, the image model redrew it where it was)."""
    geo = Image.open(io.BytesIO(geo_bytes)).convert("RGB")
    ai = Image.open(io.BytesIO(ai_bytes)).convert("RGB")
    if ai.size != geo.size:
        ai = ai.resize(geo.size, Image.LANCZOS)
    s, dx, dy, _v = align(geo, ai)
    W, H = geo.size
    ox, oy = (W - W * s) / 2 + dx, (H - H * s) / 2 + dy
    ai_in_geo = ai.transform((W, H), Image.AFFINE, (s, 0, ox, 0, s, oy), Image.BICUBIC)
    out = geo.copy()
    out.paste(ai_in_geo, (0, 0), seam_mask((W, H), moves, band))
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def edge_row(img_bytes: bytes, x0: int, x1: int, y_guess: int, reach: int = 14) -> int:
    """The row near y_guess where columns x0..x1 change brightness most (a counter's front edge): sharpens a vision
    model's estimate to the pixel."""
    im = Image.open(io.BytesIO(img_bytes)).convert("L")
    W, H = im.size
    x0, x1 = max(0, min(x0, x1)), min(W, max(x0, x1))
    if x1 - x0 < 4:
        return y_guess
    best, by = -1.0, y_guess
    for y in range(max(1, y_guess - reach), min(H - 1, y_guess + reach + 1)):
        a = im.crop((x0, y - 1, x1, y)); b = im.crop((x0, y, x1, y + 1))
        v = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
        if v > best:
            best, by = v, y
    return by


def _feather_mask(size, feather: int, hard: Optional[dict] = None):
    w, h = size
    f = max(0, min(feather, w // 4, h // 4))
    if not f:
        return Image.new("L", size, 255)
    hard = hard or {}
    m = Image.new("L", size, 0)
    m.paste(255, (f, f, w - f, h - f))
    m = m.filter(ImageFilter.GaussianBlur(f / 2))
    if hard.get("top"):
        m.paste(255, (0, 0, w, f * 2))
    if hard.get("bottom"):
        m.paste(255, (0, h - f * 2, w, h))
    if hard.get("left"):
        m.paste(255, (0, 0, f * 2, h))
    if hard.get("right"):
        m.paste(255, (w - f * 2, 0, w, h))
    return m


def changed_box(orig: Image.Image, res: Image.Image, pad: float = 0.08):
    """Bounding box (original pixels) of where the two pictures clearly differ, padded; None if nothing or all."""
    a, b = _gray(orig, 320), _gray(res, 320)
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > 40 else 0)
    bb = diff.getbbox()
    if not bb:
        return None
    k = orig.width / a.width
    l, t, r, btm = [v * k for v in bb]
    pw, ph = (r - l) * pad + 10, (btm - t) * pad + 10
    box = (max(0, int(l - pw)), max(0, int(t - ph)), min(orig.width, int(r + pw)), min(orig.height, int(btm + ph)))
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.7 * orig.width * orig.height:
        return None
    return box


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
