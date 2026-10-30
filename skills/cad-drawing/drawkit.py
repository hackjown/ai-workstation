#!/usr/bin/env python3
"""drawkit: read a drawing from a photo rigorously, keep it as a structured spec, generate it deterministically.

AIW "cad-drawing" skill helper. One file, Python 3.8+, needs numpy + Pillow (+ ezdxf for the DXF).
`drawkit.py ensure` installs what is missing (pip --user, then apt) and finds or downloads a CJK font.

  READ (from the source image, pixel coordinates x right / y down)
    zoom IMG --box x0,y0,x1,y1 [--k 4] [--rotate 0|90|270] [--grid] --out crop.png
        crop + enlarge one region (a dimension chain, one room) so its numbers can be read one by one.
    ticks IMG --side top|bottom|left|right --box x0,y0,x1,y1
        find the dimension line and the tick marks / extension lines on it; prints JSON with pixel positions.
    fit SPEC [--write]
        match every chain's values to its tick candidates, fit pixel<->mm per axis, cross-check opposite chains,
        report per-tick residuals (a misread number shows up as a big residual). --write stores the transform and
        the matched ticks in the spec.
    axes SPEC --out axes.png
        draw the fitted axis grid over the source image (check that the axes sit on the walls).
  BUILD
    build SPEC [--out DIR] [--name plan] [--source IMG] [--no-dxf]
        regenerate EVERYTHING from the spec: DXF (+ a LibreCAD viewing copy), PNG, overlay on the source,
        report.json with the automatic checks. Exit code 0 = all checks pass, 2 = errors listed in the report.
    check SPEC        the checks only (no files).
    schema            print the spec schema summary.
  auto IMG --chains SPEC_OR_FACTS [--labels L.json] [--out cad/spec.json] [--debug cad/auto_debug.png] [--task ID]
        DRAFT spec from the image: wall lines from pixels, pixel->mm from the chains, rooms = wall-bounded cells
        holding a label (OCR if rapidocr is installed, else the labels file). Then only CORRECT the draft.
  ensure              install numpy / Pillow / ezdxf and a Chinese font when missing.

Spec kinds: floor_plan (rooms -> walls, doors, windows, furniture, dimension chains), diagram (boxes + connectors,
for wiring / flow / block diagrams), generic (explicit lines, arcs, circles, rects, texts, dims; mechanical parts).
All kinds share the text placement (collision-free, font shrink), the frame / title / notes, the checks and render.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.0"

# ============================================================================================ dependencies

def _pip(pkgs):
    base = [sys.executable, "-m", "pip", "install", "-q", "--user"]
    for extra in ([], ["--break-system-packages"]):
        try:
            r = subprocess.run(base + extra + pkgs, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def _apt(pkgs):
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    try:
        r = subprocess.run(sudo + ["apt-get", "install", "-y", "-q"] + pkgs, capture_output=True, text=True, timeout=900,
                           env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
        return r.returncode == 0
    except Exception:
        return False


def need(mod, pip_name=None, apt_name=None):
    """import a module, installing it first when it is missing (pip --user, then apt)."""
    import importlib
    try:
        return importlib.import_module(mod)
    except ImportError:
        pass
    if not _pip([pip_name or mod]) and apt_name:
        _apt([apt_name])
    import site
    try:
        sp = site.getusersitepackages()
        if sp not in sys.path:
            sys.path.append(sp)
    except Exception:
        pass
    importlib.invalidate_caches()
    return importlib.import_module(mod)


FONT_URLS = [
    "https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf",
    "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf",
]


def cjk_font():
    """path of a TTF/OTF with hanzi: env AIW_CJK_FONT, fc-list :lang=zh, common paths, ~/.cache/aiw/fonts, download."""
    env = os.environ.get("AIW_CJK_FONT")
    if env and os.path.exists(env):
        return env
    try:
        out = subprocess.run(["fc-list", ":lang=zh", "file"], capture_output=True, text=True, timeout=20).stdout
        files = [ln.split(":")[0].strip() for ln in out.splitlines() if ln.strip()]
        files.sort(key=lambda f: (0 if "wqy-microhei" in f.lower() else 1 if "sc" in f.lower() else 2, f))
        for f in files:
            if f.lower().endswith((".ttf", ".otf", ".ttc")) and os.path.exists(f):
                return f
    except Exception:
        pass
    for f in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", "C:/Windows/Fonts/simhei.ttf",
              "/System/Library/Fonts/PingFang.ttc"):
        if os.path.exists(f):
            return f
    d = Path.home() / ".cache" / "aiw" / "fonts"
    f = d / "NotoSansSC-Regular.otf"
    if f.exists() and f.stat().st_size > 1_000_000:
        return str(f)
    if os.environ.get("DRAWKIT_NO_DOWNLOAD") == "1":
        return ""
    d.mkdir(parents=True, exist_ok=True)
    for url in FONT_URLS:
        try:
            subprocess.run(["curl", "-fsSL", "--retry", "2", "--connect-timeout", "15", "-o", str(f) + ".tmp", url],
                           capture_output=True, timeout=300)
            if os.path.exists(str(f) + ".tmp") and os.path.getsize(str(f) + ".tmp") > 1_000_000:
                os.replace(str(f) + ".tmp", f)
                return str(f)
        except Exception:
            pass
    if _apt(["fonts-wqy-microhei"]) and os.path.exists("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"):
        return "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
    return ""


def cmd_ensure(_a):
    ok = {}
    for mod, pipn, aptn in (("numpy", "numpy", "python3-numpy"), ("PIL", "pillow", "python3-pil"),
                            ("ezdxf", "ezdxf", "python3-ezdxf")):
        try:
            m = need(mod, pipn, aptn)
            ok[mod] = getattr(m, "__version__", "ok")
        except Exception as e:  # noqa: BLE001
            ok[mod] = f"MISSING ({e})"
    ok["cjk_font"] = cjk_font() or "MISSING"
    if not shutil.which("tesseract"):     # 3.8.9: reads the dimension digits (optional; the vision read is the fallback)
        _apt(["tesseract-ocr"])
    ok["tesseract"] = shutil.which("tesseract") or ""
    ok["librecad"] = shutil.which("librecad") or ""
    print(json.dumps(ok, ensure_ascii=False))
    # the font only affects the PNG preview (the DXF names its own fonts): a missing one is a warning, not a failure
    return 0 if all(not str(ok[k]).startswith("MISSING") for k in ("numpy", "PIL", "ezdxf")) else 1


# ============================================================================================ reading the image

def _load_gray(path):
    np = need("numpy")
    need("PIL", "pillow")
    from PIL import Image
    im = Image.open(path).convert("L")
    return np.asarray(im).astype(float), im.size


def _box(s):
    v = [float(t) for t in str(s).replace(" ", "").split(",")]
    if len(v) != 4:
        raise SystemExit("--box needs x0,y0,x1,y1")
    return [int(round(t)) for t in v]


def cmd_zoom(a):
    from PIL import Image, ImageDraw
    im = Image.open(a.image).convert("RGB")
    x0, y0, x1, y1 = _box(a.box)
    c = im.crop((x0, y0, x1, y1))
    k = max(1, int(a.k))
    c = c.resize((c.width * k, c.height * k), Image.LANCZOS)
    if a.grid:
        d = ImageDraw.Draw(c)
        step = 10 if (x1 - x0) <= 150 else 25 if (x1 - x0) <= 400 else 50
        for gx in range((x0 // step + 1) * step, x1, step):
            d.line([((gx - x0) * k, 0), ((gx - x0) * k, c.height)], fill=(255, 0, 0), width=1)
            d.text(((gx - x0) * k + 2, 2), str(gx), fill=(255, 0, 0))
        for gy in range((y0 // step + 1) * step, y1, step):
            d.line([(0, (gy - y0) * k), (c.width, (gy - y0) * k)], fill=(0, 0, 255), width=1)
            d.text((2, (gy - y0) * k + 2), str(gy), fill=(0, 0, 255))
    if a.rotate:
        c = c.rotate(int(a.rotate), expand=True)
    c.save(a.out)
    print(json.dumps({"out": a.out, "size": list(c.size), "box": [x0, y0, x1, y1], "k": k, "rotate": int(a.rotate or 0)}))
    return 0


def _peaks(prof, min_gap=3.0, k=3.0):
    """local maxima clearly above the median (robust), with a sub-pixel centroid."""
    np = need("numpy")
    prof = np.asarray(prof, float)
    if len(prof) < 3:
        return []
    b = float(np.median(prof))
    mad = float(np.median(np.abs(prof - b))) + 1.0
    thr = b + k * 3 * mad
    out = []
    for i in range(1, len(prof) - 1):
        if prof[i] > thr and prof[i] >= prof[i - 1] and prof[i] >= prof[i + 1]:
            w = prof[i - 1:i + 2] - b
            c = i + (w[2] - w[0]) / max(float(w.sum()), 1e-6)
            if out and c - out[-1][0] < min_gap:
                if prof[i] > out[-1][1]:
                    out[-1] = (c, float(prof[i]))
                continue
            out.append((c, float(prof[i])))
    return out


def find_ticks(path, side, box):
    """Dimension line + tick candidates of one chain. side: where the chain sits relative to the plan."""
    np = need("numpy")
    g, _ = _load_gray(path)
    d = 255.0 - g
    x0, y0, x1, y1 = box
    sub = d[y0:y1, x0:x1]
    horiz = side in ("top", "bottom")
    # the dimension line: the row (column) with the most moderately dark pixels along the chain
    along = (sub > 22).sum(1) if horiz else (sub > 22).sum(0)
    r = int(np.argmax(along))
    line = (y0 + r) if horiz else (x0 + r)
    H, W = d.shape
    cands = {}

    def add(pos, kind, score):
        for p in list(cands):
            if abs(p - pos) <= 2.5:
                cands[p][kind] = max(cands[p].get(kind, 0), score)
                return
        cands[pos] = {kind: score}
    # A) extension lines on the plan side of the line (they run from the plan to the dimension line)
    sgn = {"top": 1, "bottom": -1, "left": 1, "right": -1}[side]
    near = sorted(line + sgn * o for o in range(3, 11))
    near = [v for v in near if 0 <= v < (H if horiz else W)]
    if near:
        prof = d[near[0]:near[-1] + 1, x0:x1].mean(0) if horiz else d[y0:y1, near[0]:near[-1] + 1].mean(1)
        for c, s in _peaks(prof, k=2.0):
            add(round((x0 if horiz else y0) + c, 1), "ext", round(s, 1))
    # B) 45 degree tick marks crossing the line (either slant)
    sc = []
    rng = range(x0 + 2, x1 - 2) if horiz else range(y0 + 2, y1 - 2)
    for t in rng:
        best = 0.0
        for o in (-1, 0, 1):
            if horiz:
                yy, xx = line + o, t
            else:
                yy, xx = t, line + o
            if not (2 <= yy < H - 2 and 2 <= xx < W - 2):
                continue
            p = sum(d[yy - k, xx + k] + d[yy + k, xx - k] for k in (1, 2))
            q = sum(d[yy - k, xx - k] + d[yy + k, xx + k] for k in (1, 2))
            best = max(best, abs(p - q))
        sc.append(best)
    for c, s in _peaks(sc, k=2.5):
        add(round(rng.start + c, 1), "tick", round(s, 1))
    out = [{"pos": p, **v} for p, v in sorted(cands.items())]
    return {"side": side, "line_at": line, "box": box, "candidates": out}


def cmd_ticks(a):
    print(json.dumps(find_ticks(a.image, a.side, _box(a.box)), ensure_ascii=False))
    return 0


def match_chain(values, cands, start=0.0):
    """Best placement of a chain (values in mm) on tick candidates (pixel positions). Returns the matched positions,
    the scale (px per mm) and per-tick residuals. A wrong number shows up as a tick far from any candidate."""
    vals = [float(v) for v in values]
    cum = [0.0]
    for v in vals:
        cum.append(cum[-1] + v)
    T = cum[-1]
    pos = sorted(float(c["pos"] if isinstance(c, dict) else c) for c in cands)
    if len(pos) < 2 or T <= 0:
        return None
    best = None
    for i in range(len(pos)):
        for j in range(i + 1, len(pos)):
            span = pos[j] - pos[i]
            if span < 10:
                continue
            s = span / T
            cap = max(3.0, 0.02 * span)
            cost, got, used = 0.0, [], set()
            for c in cum:
                p = pos[i] + s * c
                n = min(pos, key=lambda q: abs(q - p))
                e = abs(n - p)
                if n in used:               # two ticks on one mark: the chain does not fit here
                    e = cap * 10
                used.add(n)
                cost += min(e, cap) + (0 if e <= cap else cap)
                got.append(n if e <= cap else None)
            # prefer explaining more candidates with a longer span (all ticks found, no stray ones)
            cost -= 0.001 * span
            if best is None or cost < best[0]:
                best = (cost, i, j, s, got)
    if not best:
        return None
    _, i, j, s, got = best
    return {"px": [round(g, 1) if g is not None else None for g in got], "px_per_mm": s,
            "first": pos[i], "last": pos[j]}


def _lsq(pairs):
    """px = a*mm + b by least squares over (mm, px) pairs."""
    n = len(pairs)
    if n < 2:
        return None
    sx = sum(m for m, _ in pairs); sy = sum(p for _, p in pairs)
    sxx = sum(m * m for m, _ in pairs); sxy = sum(m * p for m, p in pairs)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    a = (n * sxy - sx * sy) / den
    return a, (sy - a * sx) / n


# ============================================================================================ spec

SIDES = ("top", "bottom", "left", "right")


def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    spec.setdefault("kind", "floor_plan")
    spec.setdefault("units", "mm")
    return spec


def chain_list(spec):
    ch = spec.get("chains") or []
    if isinstance(ch, dict):
        ch = [dict(v, side=k) for k, v in ch.items()]
    return ch


def chain_marks(c):
    """axis positions (mm) of a chain's ticks, from its start along x (top/bottom) or down y (left/right)."""
    out = [float(c.get("start", 0))]
    for v in c.get("values") or []:
        out.append(out[-1] + float(v))
    return out


def grid_axes(spec):
    xs, ys = set(), set()
    for c in chain_list(spec):
        m = chain_marks(c)
        (xs if c.get("side") in ("top", "bottom") else ys).update(round(v, 3) for v in m)
    for v in (spec.get("grid") or {}).get("x", []):
        xs.add(float(v))
    for v in (spec.get("grid") or {}).get("y", []):
        ys.add(float(v))
    return sorted(xs), sorted(ys)


class Model:
    """the spec in y-up millimetres (CAD convention). y_down specs are flipped about the grid height."""

    def __init__(self, spec):
        self.spec = spec
        self.y_down = bool(spec.get("y_down", True if spec.get("kind") == "floor_plan" else False))
        xs, ys = grid_axes(spec)
        self.gx, self.gy_src = xs, ys
        self.H = max(ys) if ys else float(spec.get("height") or 0)
        self.gy = sorted(self.Y(v) for v in ys)

    def Y(self, y):
        return (self.H - float(y)) if self.y_down else float(y)

    def P(self, p):
        return (float(p[0]), self.Y(p[1]))

    def box(self, b):
        x0, y0, x1, y1 = [float(v) for v in b]
        ya, yb = self.Y(y0), self.Y(y1)
        return (min(x0, x1), min(ya, yb), max(x0, x1), max(ya, yb))


# ============================================================================================ geometry helpers

def poly_area(pts):
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def point_in_poly(x, y, pts):
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xi > x:
                inside = not inside
    return inside


def seg_dist(px, py, a, b):
    ax, ay = a; bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def box_overlap(a, b, pad=0.0):
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def box_inter_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0]); h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def seg_hits_box(p, q, b):
    """does segment pq pass through the inside of box b (Liang-Barsky)?"""
    x0, y0, x1, y1 = b
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pp == 0:
            if qq < 0:
                return False
        else:
            t = qq / pp
            if pp < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return t1 - t0 > 1e-6


def is_cjk(ch):
    o = ord(ch)
    return 0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF or 0x3000 <= o <= 0x303F


def text_width(s, h):
    """conservative width: LibreCAD's CJK font draws a hanzi about 1.2 h wide, digits ~0.7 h."""
    w = 0.0
    for ch in str(s):
        w += 1.2 * h if is_cjk(ch) else 0.4 * h if ch in " .,:;|!il1()[]" else 0.72 * h
    return w


# ============================================================================================ drawing primitives

class Sheet:
    """primitives in model mm; written to DXF and drawn to PNG by the same list, so the checks see what is drawn."""

    LAYERS = {"墙体": 7, "墙体填充": 8, "门窗": 4, "家具": 9, "尺寸": 3, "轴线": 1, "文字": 7, "房间名": 7,
              "图框": 7, "说明": 7, "连线": 5, "图形": 7, "中心线": 1}

    def __init__(self):
        self.items = []
        self.texts = []   # dicts with box
        self.warnings = []

    def line(self, a, b, layer="图形", lw=0.25):
        self.items.append({"t": "line", "pts": [tuple(a), tuple(b)], "layer": layer, "lw": lw})

    def poly(self, pts, closed=False, layer="图形", lw=0.25):
        self.items.append({"t": "poly", "pts": [tuple(p) for p in pts], "closed": closed, "layer": layer, "lw": lw})

    def fill(self, pts, layer="墙体填充", shade=0.15):
        self.items.append({"t": "fill", "pts": [tuple(p) for p in pts], "layer": layer, "shade": shade})

    def arc(self, c, r, a1, a2, layer="门窗", lw=0.18):
        self.items.append({"t": "arc", "c": tuple(c), "r": float(r), "a1": float(a1), "a2": float(a2), "layer": layer, "lw": lw})

    def circle(self, c, r, layer="图形", lw=0.25):
        self.items.append({"t": "circle", "c": tuple(c), "r": float(r), "layer": layer, "lw": lw})

    def rect(self, b, layer="图形", lw=0.25):
        x0, y0, x1, y1 = b
        self.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True, layer, lw)

    @staticmethod
    def tbox(x, y, h, s, anchor="mc", rot=0):
        w = text_width(s, h)
        hh = h * 1.15
        if rot in (90, -90, 270):
            w, hh = hh, w
        if anchor == "mc":
            return (x - w / 2, y - hh / 2, x + w / 2, y + hh / 2)
        if anchor == "ml":
            return (x, y - hh / 2, x + w, y + hh / 2)
        if anchor == "bl":
            return (x, y, x + w, y + hh)
        if anchor == "tl":
            return (x, y - hh, x + w, y)
        return (x - w / 2, y - hh / 2, x + w / 2, y + hh / 2)

    def text(self, x, y, s, h, layer="文字", anchor="mc", rot=0, role="text", owner=None):
        t = {"t": "text", "x": float(x), "y": float(y), "s": str(s), "h": float(h), "layer": layer, "anchor": anchor,
             "rot": rot, "role": role, "owner": owner, "box": self.tbox(x, y, h, s, anchor, rot)}
        self.items.append(t)
        self.texts.append(t)
        return t

    def free(self, box, pad=0.0, ignore=None):
        return not any(box_overlap(box, t["box"], pad) for t in self.texts if t is not ignore)

    def clear(self, box, layers=None):
        """no line, polyline, circle or arc passes through the box."""
        for it in self.items:
            if layers and it["layer"] not in layers:
                continue
            if it["t"] in ("line", "poly"):
                pts = it["pts"] + ([it["pts"][0]] if it.get("closed") else [])
                if any(seg_hits_box(p, q, box) for p, q in zip(pts, pts[1:])):
                    return False
            elif it["t"] in ("circle", "arc"):
                cx, cy = it["c"]; r = it["r"]
                nx = min(max(cx, box[0]), box[2]); ny = min(max(cy, box[1]), box[3])
                near = math.hypot(nx - cx, ny - cy)
                far = max(math.hypot(px - cx, py - cy) for px in (box[0], box[2]) for py in (box[1], box[3]))
                if near <= r <= far:
                    return False
        return True

    def extents(self, geom_only=False):
        xs, ys = [], []
        for it in self.items:
            if geom_only and (it["t"] == "text" or it["layer"] in ("尺寸", "文字", "中心线")):
                continue
            if it["t"] in ("line", "poly", "fill"):
                xs += [p[0] for p in it["pts"]]; ys += [p[1] for p in it["pts"]]
            elif it["t"] in ("arc", "circle"):
                xs += [it["c"][0] - it["r"], it["c"][0] + it["r"]]; ys += [it["c"][1] - it["r"], it["c"][1] + it["r"]]
            elif it["t"] == "text":
                b = it["box"]; xs += [b[0], b[2]]; ys += [b[1], b[3]]
        if not xs:
            return (0, 0, 1, 1)
        return (min(xs), min(ys), max(xs), max(ys))


# ============================================================================================ floor plan

def _norm_poly(m, pts):
    out = [m.P(p) for p in pts]
    if len(out) > 1 and out[0] == out[-1]:
        out = out[:-1]
    if poly_area(out) < 0:
        out.reverse()
    return out


def _edges(pts):
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


class Plan:
    def __init__(self, spec):
        self.spec = spec
        self.m = Model(spec)
        w = spec.get("walls") or {}
        self.t_ext = float(w.get("exterior", 240))
        self.t_int = float(w.get("interior", 120))
        self.t_rail = float(w.get("railing", 100))
        self.errors, self.warns, self.info = [], [], []
        self.rooms = []
        for r in spec.get("rooms") or []:
            pts = _norm_poly(self.m, r.get("poly") or self._rect_poly(r.get("box")))
            self.rooms.append(dict(r, pts=pts, id=r.get("id") or r.get("name")))
        self.by_id = {r["id"]: r for r in self.rooms}

    def _rect_poly(self, b):
        if not b:
            return []
        x0, y0, x1, y1 = b
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    # ---- walls from the room polygons
    def wall_segments(self):
        xs = sorted({p[0] for r in self.rooms for p in r["pts"]})
        ys = sorted({p[1] for r in self.rooms for p in r["pts"]})
        segs = {}
        for r in self.rooms:
            for a, b in _edges(r["pts"]):
                if abs(a[1] - b[1]) < 1e-6:          # horizontal
                    y = a[1]; lo, hi = sorted((a[0], b[0]))
                    cuts = [x for x in xs if lo < x < hi]
                    pts = [lo] + cuts + [hi]
                    for u, v in zip(pts, pts[1:]):
                        segs.setdefault(("h", y, u, v), []).append(r)
                elif abs(a[0] - b[0]) < 1e-6:
                    x = a[0]; lo, hi = sorted((a[1], b[1]))
                    cuts = [y for y in ys if lo < y < hi]
                    pts = [lo] + cuts + [hi]
                    for u, v in zip(pts, pts[1:]):
                        segs.setdefault(("v", x, u, v), []).append(r)
        nowall = [(self.m.P(n["a"]), self.m.P(n["b"])) for n in self.spec.get("no_wall") or []]
        out = []
        for (o, c, u, v), rs in segs.items():
            mid = ((u + v) / 2, c) if o == "h" else (c, (u + v) / 2)
            if any(seg_dist(mid[0], mid[1], a, b) < 1 for a, b in nowall):
                continue
            if len(rs) >= 2:
                z = {r.get("zone") for r in rs}
                if len(z) == 1 and None not in z:
                    continue                           # open plan: same zone, no wall
                t = self.t_int
                if any(r.get("type") == "balcony" for r in rs) and any(r.get("type") != "balcony" for r in rs):
                    t = self.t_ext if self.spec.get("balcony_wall") == "exterior" else self.t_int
            else:
                t = self.t_rail if rs[0].get("type") == "balcony" else self.t_ext
            out.append({"o": o, "c": c, "u": u, "v": v, "t": t, "rooms": [r["id"] for r in rs]})
        for e in self.spec.get("extra_walls") or []:
            a, b = self.m.P(e["a"]), self.m.P(e["b"])
            t = float(e.get("t", self.t_int))
            if abs(a[1] - b[1]) < 1e-6:
                out.append({"o": "h", "c": a[1], "u": min(a[0], b[0]), "v": max(a[0], b[0]), "t": t, "rooms": []})
            else:
                out.append({"o": "v", "c": a[0], "u": min(a[1], b[1]), "v": max(a[1], b[1]), "t": t, "rooms": []})
        # merge collinear neighbours of equal thickness
        out.sort(key=lambda s: (s["o"], s["c"], s["t"], s["u"]))
        merged = []
        for s in out:
            p = merged[-1] if merged else None
            if p and p["o"] == s["o"] and abs(p["c"] - s["c"]) < 1e-6 and p["t"] == s["t"] and abs(p["v"] - s["u"]) < 1e-6:
                p["v"] = s["v"]; p["rooms"] = sorted(set(p["rooms"]) | set(s["rooms"]))
            else:
                merged.append(dict(s))
        return merged

    def wall_rects(self, segs):
        """each wall as a rectangle around its axis; an end is extended by t/2 only where another wall meets it."""
        def touches(pt, me):
            for s in segs:
                if s is me:
                    continue
                if s["o"] == "h":
                    if abs(pt[1] - s["c"]) < 1 and s["u"] - 1 <= pt[0] <= s["v"] + 1:
                        return True
                else:
                    if abs(pt[0] - s["c"]) < 1 and s["u"] - 1 <= pt[1] <= s["v"] + 1:
                        return True
            return False
        rects = []
        for s in segs:
            h = s["t"] / 2
            if s["o"] == "h":
                a, b = (s["u"], s["c"]), (s["v"], s["c"])
                eu = h if touches(a, s) else 0
                ev = h if touches(b, s) else 0
                rects.append((s["u"] - eu, s["c"] - h, s["v"] + ev, s["c"] + h))
            else:
                a, b = (s["c"], s["u"]), (s["c"], s["v"])
                eu = h if touches(a, s) else 0
                ev = h if touches(b, s) else 0
                rects.append((s["c"] - h, s["u"] - eu, s["c"] + h, s["v"] + ev))
        return rects

    def find_wall(self, segs, p, tol=None):
        best = None
        for s in segs:
            if s["o"] == "h":
                d = abs(p[1] - s["c"]) if s["u"] - 1 <= p[0] <= s["v"] + 1 else 1e9
            else:
                d = abs(p[0] - s["c"]) if s["u"] - 1 <= p[1] <= s["v"] + 1 else 1e9
            if best is None or d < best[0]:
                best = (d, s)
        if best and best[0] <= (tol if tol is not None else max(best[1]["t"], 150)):
            return best[1]
        return None

    def nearest_wall(self, segs, p, tol):
        best = None
        for s in segs:
            if s["o"] == "h":
                q = (min(max(p[0], s["u"]), s["v"]), s["c"])
            else:
                q = (s["c"], min(max(p[1], s["u"]), s["v"]))
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            if s["v"] - s["u"] >= 500 and (best is None or d < best[0]):
                best = (d, s)
        return best[1] if best and best[0] <= tol else None

    def openings(self, segs):
        """doors and windows resolved onto their walls: (kind, spec, wall, lo, hi)."""
        out = []
        for kind in ("doors", "windows"):
            for o in self.spec.get(kind) or []:
                if "a" in o and "b" in o:
                    a, b = self.m.P(o["a"]), self.m.P(o["b"])
                    p = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                    w = math.hypot(b[0] - a[0], b[1] - a[1])
                else:
                    p = self.m.P(o["at"]); w = float(o.get("width", 900 if kind == "doors" else 1500))
                s = self.find_wall(segs, p)
                if not s:
                    # 3.8.9: snap to the nearest wall edge (within 1 m) instead of failing the build
                    s = self.nearest_wall(segs, p, float(os.environ.get("DRAWKIT_SNAP_MM", "1000")))
                    if s:
                        q = (min(max(p[0], s["u"] + w / 2), s["v"] - w / 2) if s["v"] - s["u"] > w else (s["u"] + s["v"]) / 2,
                             s["c"]) if s["o"] == "h" else \
                            (s["c"], min(max(p[1], s["u"] + w / 2), s["v"] - w / 2) if s["v"] - s["u"] > w else (s["u"] + s["v"]) / 2)
                        self.warns.append(f"{kind[:-1]} {o.get('id') or o.get('at') or o.get('a')}: snapped onto the nearest wall "
                                          f"({tuple(round(v) for v in p)} -> {tuple(round(v) for v in q)} mm)")
                        self.snapped = getattr(self, "snapped", 0) + 1
                        p = q
                if not s:
                    msg = (f"{kind[:-1]} {o.get('id') or o.get('name') or o.get('at') or o.get('a')}: not on any wall "
                           f"(point {tuple(round(v) for v in p)} mm)")
                    if os.environ.get("DRAWKIT_STRICT_OPENINGS") == "1":
                        self.errors.append(msg + "; put it on a room edge")
                    else:   # 3.8.9: no wall within 1 m - left out and listed, the build still converges
                        self.warns.append(msg + ": left out of the drawing (no wall within 1 m); listed as uncertain")
                        self.spec.setdefault("uncertain", []).append(f"{kind[:-1]} at {o.get('at') or o.get('a')} 没画：附近 1 m 内没有墙")
                    continue
                along = p[0] if s["o"] == "h" else p[1]
                lo, hi = along - w / 2, along + w / 2
                if lo < s["u"] - 1 or hi > s["v"] + 1:
                    self.warns.append(f"{kind[:-1]} {o.get('id') or o.get('name') or ''} at {tuple(round(v) for v in p)}: "
                                      f"{round(w)} wide runs past its wall ({round(s['u'])}..{round(s['v'])}); clipped")
                    lo, hi = max(lo, s["u"]), min(hi, s["v"])
                out.append((kind, o, s, lo, hi))
        return out

    # ---- union of rectangles minus openings -> filled cells + outline edges
    @staticmethod
    def rect_union(rects, holes):
        xs = sorted({v for r in rects + holes for v in (r[0], r[2])})
        ys = sorted({v for r in rects + holes for v in (r[1], r[3])})
        nx, ny = len(xs) - 1, len(ys) - 1
        if nx <= 0 or ny <= 0:
            return [], []
        ix = {v: i for i, v in enumerate(xs)}; iy = {v: i for i, v in enumerate(ys)}
        grid = [[False] * nx for _ in range(ny)]
        for r in rects:
            for j in range(iy[r[1]], iy[r[3]]):
                for i in range(ix[r[0]], ix[r[2]]):
                    grid[j][i] = True
        for r in holes:
            for j in range(iy[r[1]], iy[r[3]]):
                for i in range(ix[r[0]], ix[r[2]]):
                    grid[j][i] = False
        cells = []
        for j in range(ny):                      # row runs -> rectangles
            i = 0
            while i < nx:
                if grid[j][i]:
                    k = i
                    while k < nx and grid[j][k]:
                        k += 1
                    cells.append((xs[i], ys[j], xs[k], ys[j + 1]))
                    i = k
                else:
                    i += 1
        edges = []
        for j in range(ny):
            for i in range(nx):
                if not grid[j][i]:
                    continue
                if j == 0 or not grid[j - 1][i]:
                    edges.append(((xs[i], ys[j]), (xs[i + 1], ys[j])))
                if j == ny - 1 or not grid[j + 1][i]:
                    edges.append(((xs[i], ys[j + 1]), (xs[i + 1], ys[j + 1])))
                if i == 0 or not grid[j][i - 1]:
                    edges.append(((xs[i], ys[j]), (xs[i], ys[j + 1])))
                if i == nx - 1 or not grid[j][i + 1]:
                    edges.append(((xs[i + 1], ys[j]), (xs[i + 1], ys[j + 1])))
        return cells, _merge_edges(edges)

    # ---- build
    def build(self, sh: Sheet, scale):
        segs = self.wall_segments()
        self.segs = segs
        rects = self.wall_rects(segs)
        ops = self.openings(segs)
        holes = []
        for kind, o, s, lo, hi in ops:
            h = s["t"] / 2 + 1
            holes.append((lo, s["c"] - h, hi, s["c"] + h) if s["o"] == "h" else (s["c"] - h, lo, s["c"] + h, hi))
        cells, edges = self.rect_union(rects, holes)
        self.wall_cells, self.wall_edges = cells, edges
        for c in cells:
            sh.fill([(c[0], c[1]), (c[2], c[1]), (c[2], c[3]), (c[0], c[3])], "墙体填充", 0.35)
        for a, b in edges:
            sh.line(a, b, "墙体", 0.5)
        self.obstacles = {r["id"]: [] for r in self.rooms}
        for kind, o, s, lo, hi in ops:
            if kind == "doors":
                self._door(sh, o, s, lo, hi)
            else:
                self._window(sh, o, s, lo, hi)
        for f in self.spec.get("furniture") or []:
            self._furniture(sh, f)
        self._labels(sh, scale)
        self._dims(sh, scale)
        return sh

    def _room_at(self, p):
        for r in self.rooms:
            if point_in_poly(p[0], p[1], r["pts"]):
                return r
        return None

    def _door(self, sh, o, s, lo, hi):
        typ = o.get("type", "swing")
        t = s["t"]; c = s["c"]; w = hi - lo
        horiz = s["o"] == "h"
        # which side the door opens to: the 'into' room, else the side with a room
        sgn = 1
        into = self.by_id.get(o.get("into")) if o.get("into") else None
        mid = (lo + hi) / 2
        probe = lambda k: ((mid, c + k * (t / 2 + 150)) if horiz else (c + k * (t / 2 + 150), mid))
        if into is not None:
            sgn = 1 if point_in_poly(*probe(1), into["pts"]) else -1
        elif self._room_at(probe(-1)) and not self._room_at(probe(1)):
            sgn = -1
        face = c + sgn * t / 2
        if typ == "opening":
            return
        if typ == "sliding":
            for k, (a0, a1) in enumerate(((lo, mid + 50), (mid - 50, hi))):
                off = (k - 0.5) * t / 3
                if horiz:
                    sh.rect((a0, c + off - 20, a1, c + off + 20), "门窗", 0.18)
                else:
                    sh.rect((c + off - 20, a0, c + off + 20, a1), "门窗", 0.18)
            return
        leaves = 2 if typ == "double" else 1
        hinge = o.get("hinge", "start")
        if self.m.y_down and not horiz:      # 'start' is the smaller y as written in a y-down spec (the upper end)
            hinge = "end" if hinge == "start" else "start"
        parts = [(lo, hi, hinge)] if leaves == 1 else [(lo, mid, "start"), (mid, hi, "end")]
        room = into or self._room_at(probe(sgn))
        for a0, a1, hg in parts:
            L = a1 - a0
            hp = a0 if hg == "start" else a1
            far = a1 if hg == "start" else a0
            if horiz:
                H = (hp, face); tip = (hp, face + sgn * L); jam = (far, face)
                sh.line(H, tip, "门窗", 0.25)
                a_tip = 90 if sgn > 0 else 270
                a_jam = 0 if far > hp else 180
            else:
                H = (face, hp); tip = (face + sgn * L, hp); jam = (face, far)
                sh.line(H, tip, "门窗", 0.25)
                a_tip = 0 if sgn > 0 else 180
                a_jam = 90 if far > hp else 270
            a1_, a2_ = _ccw_pair(a_tip, a_jam)
            sh.arc(H, L, a1_, a2_, "门窗", 0.18)
            box = (min(H[0], tip[0], jam[0]), min(H[1], tip[1], jam[1]), max(H[0], tip[0], jam[0]), max(H[1], tip[1], jam[1]))
            if room is not None:
                self.obstacles.setdefault(room["id"], []).append(box)

    def _window(self, sh, o, s, lo, hi):
        t = s["t"]; c = s["c"]
        offs = (-t / 2, -t / 6, t / 6, t / 2)
        for off in offs:
            if s["o"] == "h":
                sh.line((lo, c + off), (hi, c + off), "门窗", 0.18)
            else:
                sh.line((c + off, lo), (c + off, hi), "门窗", 0.18)
        if s["o"] == "h":
            sh.line((lo, c - t / 2), (lo, c + t / 2), "门窗", 0.18); sh.line((hi, c - t / 2), (hi, c + t / 2), "门窗", 0.18)
        else:
            sh.line((c - t / 2, lo), (c + t / 2, lo), "门窗", 0.18); sh.line((c - t / 2, hi), (c + t / 2, hi), "门窗", 0.18)

    def _furniture(self, sh, f):
        x0, y0, x1, y1 = self.m.box(f["box"])
        typ = f.get("type", "box")
        w, h = x1 - x0, y1 - y0
        L = "家具"
        sh.rect((x0, y0, x1, y1), L, 0.18)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        head = f.get("head", "")   # bed head side: top/bottom/left/right in the spec's frame
        if self.m.y_down and head in ("top", "bottom"):
            head = "bottom" if head == "top" else "top"
        if typ == "bed":
            hd = head or ("left" if w > h else "top")
            if hd in ("left", "right"):
                px = x0 + 0.06 * w if hd == "left" else x1 - 0.22 * w
                for k in (0, 1):
                    yy = y0 + h * (0.08 + 0.48 * k)
                    sh.rect((px, yy, px + 0.16 * w, yy + 0.38 * h), L, 0.13)
                fx = x0 + 0.35 * w if hd == "left" else x1 - 0.35 * w
                sh.line((fx, y0), (fx, y1), L, 0.13)
            else:
                py = y1 - 0.22 * h if hd == "top" else y0 + 0.06 * h
                for k in (0, 1):
                    xx = x0 + w * (0.08 + 0.48 * k)
                    sh.rect((xx, py, xx + 0.38 * w, py + 0.16 * h), L, 0.13)
                fy = y1 - 0.35 * h if hd == "top" else y0 + 0.35 * h
                sh.line((x0, fy), (x1, fy), L, 0.13)
        elif typ == "sofa":
            k = 0.22
            if w >= h:
                sh.line((x0, y0 + k * h), (x1, y0 + k * h), L, 0.13) if head != "top" else sh.line((x0, y1 - k * h), (x1, y1 - k * h), L, 0.13)
            else:
                sh.line((x0 + k * w, y0), (x0 + k * w, y1), L, 0.13) if head != "right" else sh.line((x1 - k * w, y0), (x1 - k * w, y1), L, 0.13)
        elif typ in ("table", "dining_table", "desk"):
            if typ == "dining_table":
                n = 2 if max(w, h) < 1400 else 3
                for i in range(n):
                    if w >= h:
                        xx = x0 + w * (i + 0.5) / n
                        for yy in (y0 - 260, y1 + 60):
                            sh.rect((xx - 200, yy, xx + 200, yy + 200), L, 0.13)
                    else:
                        yy = y0 + h * (i + 0.5) / n
                        for xx in (x0 - 260, x1 + 60):
                            sh.rect((xx, yy - 200, xx + 200, yy + 200), L, 0.13)
        elif typ == "toilet":
            r = min(w, h) * 0.35
            sh.circle((cx, cy), r, L, 0.13)
        elif typ == "basin":
            sh.circle((cx, cy), min(w, h) * 0.32, L, 0.13)
        elif typ in ("counter", "wardrobe", "cabinet", "tv"):
            if typ == "wardrobe":
                sh.line((x0, y0), (x1, y1), L, 0.1); sh.line((x0, y1), (x1, y0), L, 0.1)
        elif typ == "stove":
            for dx in (-0.22, 0.22):
                sh.circle((cx + dx * w, cy), min(w, h) * 0.18, L, 0.13)
        elif typ == "sink":
            sh.rect((x0 + 0.12 * w, y0 + 0.15 * h, x1 - 0.12 * w, y1 - 0.15 * h), L, 0.13)
        elif typ in ("shower", "bath"):
            sh.line((x0, y0), (x1, y1), L, 0.1)
        room = self._room_at((cx, cy))
        pad = 60
        box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        if typ == "dining_table":
            box = (x0 - 300, y0 - 300, x1 + 300, y1 + 300)
        if room is not None:
            self.obstacles.setdefault(room["id"], []).append(box)
        if f.get("label"):
            sh.text(cx, cy, f["label"], 0.6 * float(self.spec.get("_label_h", 300)), "家具", role="furniture")

    # ---- room labels at the pole of inaccessibility, free of furniture, doors and other texts
    def _labels(self, sh, scale):
        H0 = float(self.spec.get("label_height_mm", 4.0)) * scale
        min_k = float(self.spec.get("label_min_ratio", 0.55))
        for r in sorted(self.rooms, key=lambda r: poly_area(r["pts"])):
            name = r.get("name") or r["id"]
            lines = [name]
            if r.get("show_area", self.spec.get("show_area", False)):
                lines.append(f"{poly_area(r['pts']) / 1e6:.1f}m²")
            if r.get("label") is False:
                continue
            placed = None
            k = 1.0
            H0r = float(r.get("label_height_mm", 0)) * scale or H0
            while k >= min_k - 1e-9 and placed is None:
                h = H0r * k
                placed = self._place(sh, r, lines, h, strict=True)
                k -= 0.1
            if placed is None:
                placed = self._place(sh, r, lines, H0r * min_k, strict=False)
                self.warns.append(f"room {name}: label touches furniture or a door swing (no clear spot even at "
                                  f"{round(min_k * 100)}% size)")
            if placed is None:   # 3.8.9: a narrow room still gets its name, smaller (a warning, never a dead end)
                for kk in (0.45, 0.35, 0.28):
                    placed = self._place(sh, r, lines[:1], H0r * kk, strict=False, inset_k=0.0)
                    if placed is not None:
                        lines = lines[:1]
                        self.warns.append(f"room {name}: label squeezed to {round(kk * 100)}% size (narrow room)")
                        break
            if placed is None:
                self.errors.append(f"room {name}: no room for its label")
                continue
            (cx, cy), h = placed
            if len(lines) == 1:
                sh.text(cx, cy, lines[0], h, "房间名", role="room", owner=r["id"])
            else:
                sh.text(cx, cy + 0.55 * h, lines[0], h, "房间名", role="room", owner=r["id"])
                sh.text(cx, cy - 0.75 * h, lines[1], 0.7 * h, "房间名", role="room", owner=r["id"])
            r["_label"] = (cx, cy, h)

    def _block(self, cx, cy, lines, h):
        if len(lines) == 1:
            return Sheet.tbox(cx, cy, h, lines[0])
        b1 = Sheet.tbox(cx, cy + 0.55 * h, h, lines[0]); b2 = Sheet.tbox(cx, cy - 0.75 * h, 0.7 * h, lines[1])
        return (min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[2], b2[2]), max(b1[3], b2[3]))

    def _place(self, sh, r, lines, h, strict, inset_k=1.0):
        pts = r["pts"]
        x0 = min(p[0] for p in pts); x1 = max(p[0] for p in pts)
        y0 = min(p[1] for p in pts); y1 = max(p[1] for p in pts)
        step = max(40.0, min(x1 - x0, y1 - y0) / 24)
        inset = inset_k * min(self.t_ext / 2 + 40, 0.18 * min(x1 - x0, y1 - y0)) + (1 - inset_k) * 30
        obst = self.obstacles.get(r["id"], [])
        if r.get("label_at"):
            ax, ay = self.m.P(r["label_at"])
            cands = [(ax, ay)]
        else:
            cands = []
            yy = y0 + step / 2
            while yy < y1:
                xx = x0 + step / 2
                while xx < x1:
                    cands.append((xx, yy)); xx += step
                yy += step
        best = None
        for cx, cy in cands:
            b = self._block(cx, cy, lines, h)
            corners = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
            if not all(point_in_poly(px, py, pts) for px, py in corners):
                continue
            edge_d = min(min(seg_dist(px, py, a, c) for a, c in _edges(pts)) for px, py in corners + [(cx, cy)])
            if edge_d < inset:
                continue
            if not sh.free(b, pad=0.25 * h):
                continue
            hit = sum(box_inter_area(b, o) for o in obst)
            if strict and hit > 0:
                continue
            clear = min([edge_d] + [_box_gap(b, o) for o in obst])
            cen = math.hypot(cx - (x0 + x1) / 2, cy - (y0 + y1) / 2)
            score = clear - 0.15 * cen - hit / max(h, 1)
            if best is None or score > best[0]:
                best = (score, (cx, cy))
        if best is None:
            return None
        return best[1], h

    # ---- dimension chains, as in the source
    def _dims(self, sh, scale):
        ext = self.envelope()
        th = float(self.spec.get("dim_text_mm", 2.5)) * scale
        off1 = float(self.spec.get("dim_offset_mm", 9)) * scale
        gap = float(self.spec.get("dim_row_mm", 7)) * scale
        tick = 1.2 * scale
        for c in chain_list(self.spec):
            side = c.get("side")
            marks = chain_marks(c)
            if side in ("top", "bottom"):
                pos = marks
                base = ext[3] + off1 if side == "top" else ext[1] - off1
                rows = [base, base + gap] if side == "top" else [base, base - gap]
            else:
                pos = [self.m.Y(v) for v in marks]
                base = ext[0] - off1 if side == "left" else ext[2] + off1
                rows = [base, base - gap] if side == "left" else [base, base + gap]
            _chain(sh, side, pos, [float(v) for v in c.get("values")], rows[0], ext, th, tick)
            if c.get("overall", True) and len(c.get("values") or []) > 1:
                _chain(sh, side, [pos[0], pos[-1]], [abs(marks[-1] - marks[0])], rows[1], ext, th, tick)
            c["_drawn"] = [round(v) for v in c.get("values")]

    def envelope(self):
        xs = [p[0] for r in self.rooms for p in r["pts"]]; ys = [p[1] for r in self.rooms for p in r["pts"]]
        h = self.t_ext / 2
        return (min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h)

    # ---- checks specific to floor plans
    def checks(self, sh):
        E, W, I = self.errors, self.warns, self.info
        for r in self.rooms:
            pts = r["pts"]
            if len(pts) < 3:
                E.append(f"room {r.get('name')}: polygon needs at least 3 points"); continue
            for a, b in _edges(pts):
                if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6:
                    E.append(f"room {r.get('name')}: edge {a}->{b} is not horizontal/vertical (rooms are rectilinear)")
            if abs(poly_area(pts)) < 1e5:
                E.append(f"room {r.get('name')}: area under 0.1 m²")
            if not r.get("_label") and r.get("label") is not False:
                E.append(f"room {r.get('name')}: has no label")
        # rooms must not overlap
        for i, a in enumerate(self.rooms):
            for b in self.rooms[i + 1:]:
                ov = _poly_overlap(a["pts"], b["pts"])
                if ov > 1e4:
                    E.append(f"rooms {a.get('name')} and {b.get('name')} overlap by {ov / 1e6:.2f} m²")
        # walls closed: every vertex of the wall outline joins an even number of edges
        deg = {}
        for a, b in self.wall_edges:
            for p in (a, b):
                k = (round(p[0], 1), round(p[1], 1))
                deg[k] = deg.get(k, 0) + 1
        odd = [k for k, v in deg.items() if v % 2]
        if odd:
            E.append(f"wall outline not closed at {odd[:4]}")
        # the outer envelope: every boundary piece of the union of rooms is a wall, a window or a door
        outline = _outline_of_rooms(self.rooms)
        for a, b in outline:
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if not self.find_wall(self.segs, mid, tol=1):
                E.append(f"outer edge {tuple(round(v) for v in a)}-{tuple(round(v) for v in b)} has no wall")
        # axes: every chain tick should be used by some room edge (else the chain or a room is off)
        xs = {round(p[0]) for r in self.rooms for p in r["pts"]}
        ys = {round(p[1]) for r in self.rooms for p in r["pts"]}
        for c in chain_list(self.spec):
            side = c.get("side")
            for v in chain_marks(c):
                vv = v if side in ("top", "bottom") else self.m.Y(v)
                pool = xs if side in ("top", "bottom") else ys
                if not any(abs(vv - q) <= 5 for q in pool):
                    W.append(f"{side} chain: axis {round(v)} is used by no room edge (check the chain or the rooms)")
        return E, W, I


def _box_gap(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0); dy = max(b[1] - a[3], a[1] - b[3], 0)
    return math.hypot(dx, dy)


def _ccw_pair(a, b):
    """arc from angle a to angle b the short way (90 degrees), as a CCW (start, end) pair."""
    d = (b - a) % 360
    return (a, b) if d <= 180 else (b, a)


def _merge_edges(edges):
    """join collinear touching unit edges into long segments."""
    hs, vs = {}, {}
    for a, b in edges:
        if abs(a[1] - b[1]) < 1e-9:
            hs.setdefault(a[1], []).append(tuple(sorted((a[0], b[0]))))
        else:
            vs.setdefault(a[0], []).append(tuple(sorted((a[1], b[1]))))
    out = []
    for y, iv in hs.items():
        for u, v in _join(iv):
            out.append(((u, y), (v, y)))
    for x, iv in vs.items():
        for u, v in _join(iv):
            out.append(((x, u), (x, v)))
    return out


def _join(iv):
    iv = sorted(iv)
    out = []
    for u, v in iv:
        if out and abs(out[-1][1] - u) < 1e-9:
            out[-1] = (out[-1][0], v)
        else:
            out.append((u, v))
    return out


def _poly_overlap(p, q):
    """overlap area of two rectilinear polygons on their compressed grid."""
    xs = sorted({v[0] for v in p + q}); ys = sorted({v[1] for v in p + q})
    s = 0.0
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
            if point_in_poly(cx, cy, p) and point_in_poly(cx, cy, q):
                s += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
    return s


def _outline_of_rooms(rooms):
    xs = sorted({p[0] for r in rooms for p in r["pts"]}); ys = sorted({p[1] for r in rooms for p in r["pts"]})
    def inside(cx, cy):
        return any(point_in_poly(cx, cy, r["pts"]) for r in rooms)
    edges = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
            if not inside(cx, cy):
                continue
            if j == 0 or not inside(cx, (ys[j - 1] + ys[j]) / 2):
                edges.append(((xs[i], ys[j]), (xs[i + 1], ys[j])))
            if j == len(ys) - 2 or not inside(cx, (ys[j + 1] + ys[j + 2]) / 2):
                edges.append(((xs[i], ys[j + 1]), (xs[i + 1], ys[j + 1])))
            if i == 0 or not inside((xs[i - 1] + xs[i]) / 2, cy):
                edges.append(((xs[i], ys[j]), (xs[i], ys[j + 1])))
            if i == len(xs) - 2 or not inside((xs[i + 1] + xs[i + 2]) / 2, cy):
                edges.append(((xs[i + 1], ys[j]), (xs[i + 1], ys[j + 1])))
    return edges


def _chain(sh, side, pos, vals, row, ext, th, tick):
    """one dimension row: dimension line, extension lines, 45 degree ticks, numbers (staggered when they collide)."""
    horiz = side in ("top", "bottom")
    out = 1 if side in ("top", "right") else -1
    if horiz:
        sh.line((pos[0], row), (pos[-1], row), "尺寸", 0.13)
        start = ext[3] + 2.5 * tick if side == "top" else ext[1] - 2.5 * tick
        for p in pos:
            sh.line((p, start), (p, row + out * 0.6 * tick), "尺寸", 0.13)
            sh.line((p - tick / 2, row - tick / 2), (p + tick / 2, row + tick / 2), "尺寸", 0.35)
    else:
        sh.line((row, pos[0]), (row, pos[-1]), "尺寸", 0.13)
        start = ext[0] - 2.5 * tick if side == "left" else ext[2] + 2.5 * tick
        for p in pos:
            sh.line((start, p), (row + out * 0.6 * tick, p), "尺寸", 0.13)
            sh.line((row - tick / 2, p - tick / 2), (row + tick / 2, p + tick / 2), "尺寸", 0.35)
    for i, v in enumerate(vals):
        a, b = pos[i], pos[i + 1]
        mid = (a + b) / 2
        s = str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:g}"
        length = abs(b - a)
        # 3.8.9 (task-3fc: "text '3500' is crossed by a 尺寸 line"): a spot must be free of other texts AND of
        # dimension lines; the number slides along its segment before it is lifted further
        first = None
        for k in range(4):
            lift = 0.35 * th + k * 1.25 * th
            for along in (0.0, -0.25, 0.25):
                m_ = mid + along * length
                if horiz:
                    y = row + (lift + 0.55 * th if side == "top" else -(lift + 0.55 * th))
                    box = Sheet.tbox(m_, y, th, s)
                else:
                    x = row - (lift + 0.55 * th) if side == "left" else row + (lift + 0.55 * th)
                    box = Sheet.tbox(x, m_, th, s, rot=90)
                if first is None:
                    first = (m_, y if horiz else x)
                if sh.free(box, pad=0.15 * th) and sh.clear(_shrink(box, 0.08), ("尺寸",)):
                    break
            else:
                continue
            break
        else:
            m_, v_ = first
            if horiz:
                y = v_
            else:
                x = v_
        if horiz:
            sh.text(m_, y, s, th, "尺寸", role="dim")
        else:
            sh.text(x, m_, s, th, "尺寸", rot=90, role="dim")
        if text_width(s, th) > length:
            sh.warnings.append(f"{side} chain: {s} is wider than its {round(length)} segment (number moved out)")


# ============================================================================================ diagram + generic

class Diagram:
    """boxes with centred labels and connectors (straight or orthogonal) with arrowheads and edge labels."""

    def __init__(self, spec):
        self.spec = spec
        self.m = Model(spec)
        self.errors, self.warns, self.info = [], [], []

    def build(self, sh, scale):
        H = float(self.spec.get("label_height_mm", 3.5)) * scale
        nodes = {}
        for n in self.spec.get("nodes") or []:
            x0, y0, x1, y1 = self.m.box(n["box"]) if "box" in n else self.m.box(
                [n["at"][0] - n.get("w", 3000) / 2, n["at"][1] - n.get("h", 1500) / 2,
                 n["at"][0] + n.get("w", 3000) / 2, n["at"][1] + n.get("h", 1500) / 2])
            nodes[n["id"]] = (x0, y0, x1, y1)
            shape = n.get("shape", "box")
            if shape == "circle":
                sh.circle(((x0 + x1) / 2, (y0 + y1) / 2), min(x1 - x0, y1 - y0) / 2, "图形", 0.35)
            elif shape == "diamond":
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                sh.poly([(cx, y1), (x1, cy), (cx, y0), (x0, cy)], True, "图形", 0.35)
            else:
                sh.rect((x0, y0, x1, y1), "图形", 0.35)
            text = str(n.get("text", n["id"]))
            h = H
            while text_width(text, h) > 0.86 * (x1 - x0) and h > 0.5 * H:
                h *= 0.9
            if text_width(text, h) > 0.86 * (x1 - x0):
                self.warns.append(f"node {n['id']}: text wider than its box")
            sh.text((x0 + x1) / 2, (y0 + y1) / 2, text, h, "文字", role="node", owner=n["id"])
        for e in self.spec.get("edges") or []:
            a, b = nodes.get(e.get("from")), nodes.get(e.get("to"))
            if not a or not b:
                self.errors.append(f"edge {e.get('from')}->{e.get('to')}: unknown node")
                continue
            pts = _route(a, b, e.get("route", "ortho"))
            sh.poly(pts, False, "连线", 0.25)
            _arrow(sh, pts[-2], pts[-1], 0.9 * scale)
            if e.get("label"):
                (px, py), (qx, qy) = max(zip(pts, pts[1:]), key=lambda s: math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]))
                mx, my = (px + qx) / 2, (py + qy) / 2
                h = 0.8 * H
                for k in range(6):
                    off = (0.9 + 1.3 * k) * h
                    cx, cy = (mx, my + off) if abs(qy - py) < 1e-6 else (mx + off + text_width(e["label"], h) / 2, my)
                    if sh.free(Sheet.tbox(cx, cy, h, e["label"]), 0.15 * h):
                        break
                sh.text(cx, cy, e["label"], h, "文字", role="edge")
        self.nodes = nodes
        return sh

    def checks(self, sh):
        for t in sh.texts:
            if t["role"] == "edge":
                for nid, b in self.nodes.items():
                    if box_overlap(t["box"], b):
                        self.warns.append(f"edge label '{t['s']}' overlaps node {nid}")
        return self.errors, self.warns, self.info


def _route(a, b, how):
    ac = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2); bc = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    def port(bx, toward):
        cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
        dx, dy = toward[0] - cx, toward[1] - cy
        if abs(dx) * (bx[3] - bx[1]) >= abs(dy) * (bx[2] - bx[0]):
            return (bx[2] if dx > 0 else bx[0], cy), "h"
        return (cx, bx[3] if dy > 0 else bx[1]), "v"
    pa, da = port(a, bc); pb, db = port(b, ac)
    if how == "straight" or abs(pa[0] - pb[0]) < 1 or abs(pa[1] - pb[1]) < 1:
        return [pa, pb]
    if da == "h" and db == "h":
        mx = (pa[0] + pb[0]) / 2
        return [pa, (mx, pa[1]), (mx, pb[1]), pb]
    if da == "v" and db == "v":
        my = (pa[1] + pb[1]) / 2
        return [pa, (pa[0], my), (pb[0], my), pb]
    return [pa, (pb[0], pa[1]), pb] if da == "h" else [pa, (pa[0], pb[1]), pb]


def _arrow(sh, p, q, L):
    ang = math.atan2(q[1] - p[1], q[0] - p[0])
    for s in (-1, 1):
        a = ang + math.pi - s * math.radians(20)
        sh.line(q, (q[0] + L * math.cos(a), q[1] + L * math.sin(a)), "连线", 0.25)


class Generic:
    """explicit entities (mechanical parts, details): line, poly, rect, circle, arc, centerline, text, label, dim."""

    def __init__(self, spec):
        self.spec = spec
        self.m = Model(spec)
        self.errors, self.warns, self.info = [], [], []

    def build(self, sh, scale):
        P = self.m.P
        th = float(self.spec.get("dim_text_mm", 2.5)) * scale
        for e in self.spec.get("entities") or []:
            t = e.get("type"); L = e.get("layer", "图形"); lw = float(e.get("lw", 0.35))
            if t == "line":
                sh.line(P(e["a"]), P(e["b"]), L, lw)
            elif t == "centerline":
                sh.line(P(e["a"]), P(e["b"]), "中心线", 0.13)
            elif t in ("poly", "polyline"):
                sh.poly([P(p) for p in e["points"]], bool(e.get("closed")), L, lw)
            elif t == "rect":
                sh.rect(self.m.box(e["box"]), L, lw)
            elif t == "circle":
                sh.circle(P(e["c"]), e["r"], L, lw)
            elif t == "arc":
                sh.arc(P(e["c"]), e["r"], e["a1"], e["a2"], L, lw)
            elif t == "text":
                x, y = P(e["at"])
                sh.text(x, y, e["text"], float(e.get("h_mm", 3.5)) * scale, e.get("layer", "文字"), e.get("anchor", "mc"))
        g = sh.extents(geom_only=True)
        for d in self.spec.get("dims") or []:
            a, b = P(d["a"]), P(d["b"])
            horiz = abs(a[1] - b[1]) < abs(a[0] - b[0]) if "axis" not in d else d["axis"] == "x"
            off = float(d.get("offset_mm", 8)) * scale
            val = d.get("value", round(abs(b[0] - a[0]) if horiz else abs(b[1] - a[1])))
            ext = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            if d.get("outside", True):     # dimension rows sit outside the part outline, as on a real drawing
                ext = (min(ext[0], g[0]), min(ext[1], g[1]), max(ext[2], g[2]), max(ext[3], g[3])) if not horiz else \
                    (ext[0], min(ext[1], g[1]), ext[2], max(ext[3], g[3]))
                if not horiz:
                    ext = (min(a[0], b[0], g[0]), ext[1], max(a[0], b[0], g[2]), ext[3])
                    ext = (ext[0], min(a[1], b[1]), ext[2], max(a[1], b[1]))
            if horiz:
                side = "top" if off > 0 else "bottom"
                row = ext[3] + off if off > 0 else ext[1] + off
                _chain(sh, side, sorted([a[0], b[0]]), [float(val)], row, ext, th, 1.2 * scale)
            else:
                side = "right" if off > 0 else "left"
                row = ext[2] + off if off > 0 else ext[0] + off
                _chain(sh, side, sorted([a[1], b[1]]), [float(val)], row, ext, th, 1.2 * scale)
        for lab in self.spec.get("labels") or []:     # auto-placed text near a point, clear of texts and lines
            x, y = P(lab["at"]); h = float(lab.get("h_mm", 3.0)) * scale
            best = None
            for ring in range(1, 9):
                for k in range(16):
                    ang = k * math.pi / 8
                    rr = ring * 2.0 * h
                    cx, cy = x + math.cos(ang) * rr, y + math.sin(ang) * rr
                    w = text_width(lab["text"], h)
                    cx += (w / 2) * (1 if math.cos(ang) >= 0 else -1)
                    b = Sheet.tbox(cx, cy, h, lab["text"])
                    if sh.free(b, 0.2 * h) and sh.clear(b):
                        best = (cx, cy, b); break
                if best:
                    break
            if not best:
                self.warns.append(f"label {lab['text']}: no clear spot near its point")
                best = (x + 2 * h, y + 2 * h, None)
            cx, cy, b = best
            sh.text(cx, cy, lab["text"], h, "文字", role="label")
            ex = b[0] if b and cx > x else (b[2] if b else cx)
            sh.line((x, y), (ex, cy), "文字", 0.13)
        return sh

    def checks(self, sh):
        return self.errors, self.warns, self.info


# ============================================================================================ sheet: frame, title, notes

PAPER = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594)}
SCALES = (1, 2, 5, 10, 20, 25, 50, 75, 100, 150, 200, 250, 500)


def choose_scale(w, h, paper, notes_w_mm, want=None):
    if want:
        return int(want)
    pw, ph = PAPER.get(paper, PAPER["A3"])
    for s in SCALES:
        if w / s <= pw - 30 - notes_w_mm and h / s <= ph - 70:
            return s
    return SCALES[-1]


def make(spec):
    kind = spec.get("kind", "floor_plan")
    Eng = {"floor_plan": Plan, "diagram": Diagram, "generic": Generic}.get(kind)
    if Eng is None:
        raise SystemExit(f"unknown kind {kind}; use floor_plan, diagram or generic")
    eng = Eng(spec)
    # first pass at 1:1 to learn the extents, then pick the scale and build for real
    probe = Sheet()
    eng0 = Eng(json.loads(json.dumps(spec)))
    try:
        eng0.build(probe, 100)
    except Exception:
        pass
    ex = probe.extents(geom_only=True)
    ex = (ex[0], ex[1], ex[2] + 0.25 * (ex[2] - ex[0]), ex[3] + 0.25 * (ex[3] - ex[1]))   # room for dimension rows
    notes_w = float(spec.get("notes_width_mm", 95))
    paper = spec.get("paper", "A3")
    scale = choose_scale(ex[2] - ex[0], ex[3] - ex[1], paper, notes_w, spec.get("scale"))
    sh = Sheet()
    eng.build(sh, scale)
    _sheet_furniture(sh, spec, scale, paper, notes_w, eng)
    return eng, sh, scale


def _sheet_furniture(sh, spec, scale, paper, notes_w, eng):
    """A3 border + title block, view title + scale under the plan, notes/legend column right of it: all outside the
    plan extents, laid out by measured text boxes (never trial and error)."""
    ex = sh.extents()
    pw, ph = PAPER.get(paper, PAPER["A3"])
    W, H = pw * scale, ph * scale
    m = 10 * scale
    # plan placed with its extents centred in the area left of the notes column, above the title band
    area = (m + 15 * scale, m + 45 * scale, W - m - notes_w * scale, H - m)
    dx = (area[0] + area[2]) / 2 - (ex[0] + ex[2]) / 2
    dy = (area[1] + area[3]) / 2 - (ex[1] + ex[3]) / 2
    _shift(sh, dx, dy)
    eng.offset = (dx, dy)
    ex = sh.extents()
    x0, y0 = 0.0, 0.0
    sh.rect((x0, y0, W, H), "图框", 0.25)
    sh.rect((x0 + m, y0 + m, W - m, H - m), "图框", 0.7)
    # title under the plan
    title = spec.get("title") or "图纸"
    t1 = 7 * scale
    ty = ex[1] - 9 * scale
    sh.text((ex[0] + ex[2]) / 2, ty, title, t1, "文字", role="title")
    sub = f"比例 1:{scale}" + (f"    {spec['subtitle']}" if spec.get("subtitle") else "")
    sh.text((ex[0] + ex[2]) / 2, ty - 1.6 * t1, sub, 3.5 * scale, "文字", role="title")
    # notes / legend column
    nx = W - m - (notes_w - 5) * scale
    ny = H - m - 10 * scale
    h = 3.0 * scale
    lines = []
    if spec.get("kind", "floor_plan") == "floor_plan":
        lines.append(("图例", True))
    for n in (spec.get("notes") or []):
        lines.append((n, False))
    unc = [u for u in (spec.get("uncertain") or [])]
    y = ny
    sh.text(nx, y, "说明", 4.5 * scale, "说明", "ml", role="notes"); y -= 8 * scale
    num = 0
    for n, legend in lines:
        if legend:
            y = _legend(sh, nx, y, scale, eng)
            continue
        num += 1
        for k, part in enumerate(_wrap(f"{num}. {n}", h, (notes_w - 12) * scale)):
            sh.text(nx + (0 if k == 0 else 2 * h), y, part, h, "说明", "ml", role="notes"); y -= 1.7 * h
        y -= 0.6 * h
    if unc:
        y -= 0.8 * h
        sh.text(nx, y, "待确认", 4.0 * scale, "说明", "ml", role="notes"); y -= 7 * scale
        for i, n in enumerate(unc, 1):
            for k, part in enumerate(_wrap(f"{i}. {n}", h, (notes_w - 12) * scale)):
                sh.text(nx + (0 if k == 0 else 2 * h), y, part, h, "说明", "ml", role="notes"); y -= 1.7 * h
            y -= 0.6 * h
    if y < m + 45 * scale:
        sh.warnings.append("notes column is too long for the sheet; shorten the notes")
    # title block bottom-right
    bw, bh = 170 * scale, 32 * scale
    bx0, by0 = W - m - bw, m
    sh.rect((bx0, by0, W - m, by0 + bh), "图框", 0.5)
    rows = [("项目", spec.get("project") or ""), ("图名", title), ("比例", f"1:{scale}"),
            ("来源", spec.get("source_note") or (Path((spec.get("source") or {}).get("image", "")).name or "—")),
            ("日期", time.strftime("%Y-%m-%d"))]
    rh = bh / len(rows)
    for i, (k, v) in enumerate(rows):
        yy = by0 + bh - (i + 1) * rh
        if i:
            sh.line((bx0, yy + rh), (W - m, yy + rh), "图框", 0.18)
        hh = min(3.0 * scale, rh * 0.55)
        sh.text(bx0 + 2 * scale, yy + rh / 2, k, hh, "文字", "ml", role="tb")
        vv = str(v)
        while text_width(vv, hh) > bw - 26 * scale and len(vv) > 4:
            vv = vv[:-2] + "…"
        sh.text(bx0 + 20 * scale, yy + rh / 2, vv, hh, "文字", "ml", role="tb")
    sh.line((bx0 + 18 * scale, by0), (bx0 + 18 * scale, by0 + bh), "图框", 0.18)
    sh.frame = (0, 0, W, H)


def _legend(sh, x, y, scale, eng):
    h = 3.0 * scale
    sw = 12 * scale
    t_ext = getattr(eng, "t_ext", 240); t_int = getattr(eng, "t_int", 120)
    items = [("ext", f"外墙 {int(t_ext)}"), ("int", f"内墙 {int(t_int)}"), ("door", "门（弧线为开启方向）"), ("win", "窗")]
    for kind, label in items:
        cy = y
        if kind in ("ext", "int"):
            t = (t_ext if kind == "ext" else t_int) * 1.0
            b = (x, cy - t / 2, x + sw, cy + t / 2)
            sh.fill([(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])], "墙体填充", 0.35)
            sh.rect(b, "墙体", 0.5)
        elif kind == "door":
            r = 0.6 * sw
            sh.line((x, cy - r / 2), (x, cy + r / 2), "门窗", 0.25)
            sh.arc((x, cy - r / 2), r, 0, 90, "门窗", 0.18)
        else:
            for off in (-0.4, -0.13, 0.13, 0.4):
                sh.line((x, cy + off * 2 * scale), (x + sw, cy + off * 2 * scale), "门窗", 0.18)
        sh.text(x + sw + 4 * scale, cy, label, h, "说明", "ml", role="notes")
        y -= 2.6 * h
    return y - 0.6 * h


def _wrap(s, h, width):
    """wrap a note: never split a number or a latin word, never start a line with punctuation."""
    import re as _re
    toks = _re.findall(r"[0-9A-Za-z.~²+\-/×]+|.", s)
    out, cur = [], ""
    for tk in toks:
        if cur and text_width(cur + tk, h) > width and tk not in "，。；：、）)!！?？,.;:":
            out.append(cur); cur = tk.lstrip()
        else:
            cur += tk
    if cur:
        out.append(cur)
    return out


def _shift(sh, dx, dy):
    for it in sh.items:
        if it["t"] in ("line", "poly", "fill"):
            it["pts"] = [(p[0] + dx, p[1] + dy) for p in it["pts"]]
        elif it["t"] in ("arc", "circle"):
            it["c"] = (it["c"][0] + dx, it["c"][1] + dy)
        elif it["t"] == "text":
            it["x"] += dx; it["y"] += dy
            b = it["box"]; it["box"] = (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)


# ============================================================================================ checks (all kinds)

def text_checks(sh, eng):
    E, W = [], []
    T = sh.texts
    for i, a in enumerate(T):
        for b in T[i + 1:]:
            ar = box_inter_area(a["box"], b["box"])
            if ar > 0:
                E.append(f"text overlap: '{a['s']}' and '{b['s']}'")
    # room / node labels must not be crossed by lines (walls, furniture, dimension lines)
    for t in T:
        if t["role"] not in ("room", "node", "title", "notes", "dim"):
            continue
        b = _shrink(t["box"], 0.08)
        for it in sh.items:
            if it["t"] not in ("line", "poly"):
                continue
            if t["role"] == "node" and it["layer"] == "图形":
                continue
            pts = it["pts"] + ([it["pts"][0]] if it.get("closed") else [])
            if any(seg_hits_box(p, q, b) for p, q in zip(pts, pts[1:])):
                (E if t["role"] in ("room", "dim") and it["layer"] in ("墙体", "尺寸") else W).append(
                    f"text '{t['s']}' is crossed by a {it['layer']} line")
                break
    fr = getattr(sh, "frame", None)
    if fr:
        for t in T:
            b = t["box"]
            if b[0] < fr[0] or b[1] < fr[1] or b[2] > fr[2] or b[3] > fr[3]:
                E.append(f"text '{t['s']}' is outside the sheet")
    return E, W


def _shrink(b, k):
    dx = (b[2] - b[0]) * k; dy = (b[3] - b[1]) * k
    return (b[0] + dx, b[1] + dy, b[2] - dx, b[3] - dy)


def chain_checks(spec, m: Model):
    E, W, I = [], [], []
    ch = chain_list(spec)
    by = {c.get("side"): c for c in ch}
    for c in ch:
        vals = [float(v) for v in c.get("values") or []]
        if not vals:
            E.append(f"{c.get('side')} chain has no values"); continue
        reads = c.get("reads") or []
        for k, r in enumerate(reads):
            if [float(v) for v in r] != vals:
                E.append(f"{c['side']} chain: read #{k + 1} {r} differs from the values {c['values']}: re-read the zoomed crop")
        if len(reads) < 2 and not c.get("given") and not c.get("auto"):
            W.append(f"{c['side']} chain: read it twice (two zoomed reads) and store both in 'reads'")
    for a, b in (("top", "bottom"), ("left", "right")):
        if a in by and b in by:
            ma, mb = chain_marks(by[a]), chain_marks(by[b])
            if abs(ma[0] - mb[0]) > 1 or abs(ma[-1] - mb[-1]) > 1:
                msg = (f"{a} chain spans {round(ma[0])}..{round(ma[-1])} but {b} spans {round(mb[0])}..{round(mb[-1])}"
                       f" (sum {round(ma[-1] - ma[0])} vs {round(mb[-1] - mb[0])})")
                if by[a].get("explain") or by[b].get("explain"):
                    I.append(msg + " - explained: " + (by[a].get("explain") or by[b].get("explain")))
                else:
                    E.append(msg + ": opposite chains must add up to the same total, or say why in 'explain' (a setback)")
            else:
                I.append(f"{a}/{b} chains agree: {round(ma[-1] - ma[0])} mm")
    return E, W, I


def fit_transform(spec, write=False):
    """pixel<->mm per axis from the chains' matched ticks; per-tick residuals flag misread numbers."""
    src = spec.get("source") or {}
    res = {"x": [], "y": []}
    report = {"chains": {}, "errors": [], "warnings": []}
    for c in chain_list(spec):
        side = c.get("side")
        marks = chain_marks(c)
        px = c.get("ticks_px")
        cands = c.get("tick_candidates")
        full = None
        if c.get("box_px") and src.get("image"):
            px = None                     # always re-measured from the image, so stale ticks never linger
        if (not px or "auto" == px) and src.get("image") and c.get("box_px"):
            full = find_ticks(src["image"], side, c["box_px"])["candidates"]
            cands = [d["pos"] for d in full]
            c["tick_candidates"] = cands
        if (not px or px == "auto") and cands:
            mt = None
            strong = sorted(d["pos"] for d in (full or []) if "ext" in d)
            n = len(c.get("values") or []) + 1
            if len(strong) == n:
                # one extension line per tick: the chain's own marks, in order. Segment lengths are then measured,
                # so a misread number (3000 for 3900) shows up as a segment that does not fit.
                px = strong
            elif len(strong) >= 2:
                # the outer extension lines are the chain ends: scale from them, nearest mark for the inner ticks
                lo, hi = strong[0], strong[-1]
                cum = chain_marks(dict(c, start=0))
                s_ = (hi - lo) / cum[-1]
                pool = sorted(set(strong) | set(float(v) for v in cands))
                px = []
                for v in cum:
                    p = lo + s_ * v
                    q = min(pool, key=lambda t: abs(t - p))
                    px.append(round(q, 1) if abs(q - p) <= max(3.0, 0.02 * (hi - lo)) else None)
                px[0], px[-1] = lo, hi
            else:
                mt = match_chain(c.get("values"), cands)
                if mt:
                    px = mt["px"]
        if not px or len(px) != len(marks):
            report["warnings"].append(f"{side} chain: no tick pixels (give ticks_px or box_px); not used in the fit")
            continue
        c["ticks_px"] = px
        axis = "x" if side in ("top", "bottom") else "y"
        res[axis] += [(mm, p) for mm, p in zip(marks, px) if p is not None]
    T = {}
    for axis in ("x", "y"):
        f = _lsq(res[axis])
        if f:
            T["a" + axis], T["b" + axis] = f
    if "ax" in T and "ay" in T:
        r = abs(T["ax"]) / abs(T["ay"])
        if abs(r - 1) > 0.03:
            report["warnings"].append(f"x and y pixel scales differ by {abs(r - 1) * 100:.1f}% (photo taken at an angle, or "
                                      f"a chain is misread)")
    for c in chain_list(spec):
        side = c.get("side"); axis = "x" if side in ("top", "bottom") else "y"
        if "a" + axis not in T or not c.get("ticks_px"):
            continue
        a, b = T["a" + axis], T["b" + axis]
        marks = chain_marks(c)
        rows = []
        for i, (mm, p) in enumerate(zip(marks, c["ticks_px"])):
            if p is None:
                rows.append({"mm": mm, "px": None, "res_mm": None}); continue
            rows.append({"mm": mm, "px": p, "res_mm": round((p - b) / a - mm)})
        # segment check: measured vs written
        for i, v in enumerate(c.get("values")):
            p0, p1 = c["ticks_px"][i], c["ticks_px"][i + 1]
            if p0 is None or p1 is None:
                report["warnings"].append(f"{side} chain segment {i + 1}: an end tick is not where the written values put it "
                                          f"(source not to scale there, or a number is off)")
                continue
            meas = (p1 - p0) / a
            d = meas - float(v)
            tol_err = max(400.0, 0.2 * float(v)); tol_warn = max(200.0, 0.08 * float(v))
            if abs(d) > tol_err:
                report["errors"].append(f"{side} chain segment {i + 1}: written {v} but the drawing measures ~{round(meas)} mm: "
                                        f"re-read this number on a bigger zoom")
            elif abs(d) > tol_warn:
                report["warnings"].append(f"{side} chain segment {i + 1}: written {v}, measures ~{round(meas)} mm "
                                          f"(source not to scale here; keep the written value)")
        report["chains"][side] = rows
    report["transform"] = T
    if write and T:
        spec.setdefault("source", {})["transform"] = {k: round(v, 6) for k, v in T.items()}
    return report


# ============================================================================================ output: DXF, PNG, overlay

def write_dxf(sh, path, view_font="notosc"):
    ezdxf = need("ezdxf", "ezdxf", "python3-ezdxf")
    doc = ezdxf.new("R2010", setup=True)
    doc.units = 4  # mm
    try:
        doc.styles.add("CN", font="simhei.ttf")
    except Exception:
        pass
    for name, col in Sheet.LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=col)
    msp = doc.modelspace()
    for it in sh.items:
        att = {"layer": it["layer"]}
        lw = it.get("lw")
        if lw:
            att["lineweight"] = min((v for v in (13, 18, 25, 35, 50, 70) if v >= int(lw * 100)), default=70)
        t = it["t"]
        if t == "line":
            msp.add_line(it["pts"][0], it["pts"][1], dxfattribs=att)
        elif t == "poly":
            msp.add_lwpolyline(it["pts"], close=it.get("closed", False), dxfattribs=att)
        elif t == "fill":
            hb = msp.add_hatch(color=8, dxfattribs={"layer": it["layer"]})
            hb.paths.add_polyline_path(it["pts"], is_closed=True)
        elif t == "arc":
            msp.add_arc(it["c"], it["r"], it["a1"], it["a2"], dxfattribs=att)
        elif t == "circle":
            msp.add_circle(it["c"], it["r"], dxfattribs=att)
        elif t == "text":
            e = msp.add_text(it["s"], height=it["h"], rotation=it.get("rot", 0) or 0,
                             dxfattribs={"layer": it["layer"], "style": "CN"})
            from ezdxf.enums import TextEntityAlignment as A
            al = {"mc": A.MIDDLE_CENTER, "ml": A.MIDDLE_LEFT, "bl": A.BOTTOM_LEFT, "tl": A.TOP_LEFT}.get(it["anchor"], A.MIDDLE_CENTER)
            e.set_placement((it["x"], it["y"]), align=al)
    doc.saveas(path)
    out = {"dxf": path, "entities": len(msp)}
    # LibreCAD viewing copy: LibreCAD draws only its own .lff fonts; notosc is made by librecad-setup.sh
    view = str(Path(path).with_name("." + Path(path).stem + ".view.dxf"))
    doc.styles.get("CN").dxf.font = view_font
    doc.saveas(view)
    out["view_dxf"] = view
    return out


def _font(size):
    from PIL import ImageFont
    f = cjk_font()
    try:
        return ImageFont.truetype(f, max(6, int(size))) if f else ImageFont.load_default()
    except Exception:
        return ImageFont.load_default()


def render_png(sh, path, px_per_mm_paper=6.0, scale=100):
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = getattr(sh, "frame", None) or sh.extents()
    k = px_per_mm_paper / scale          # px per model mm
    Wp, Hp = int((x1 - x0) * k) + 2, int((y1 - y0) * k) + 2
    im = Image.new("RGB", (Wp, Hp), "white")
    d = ImageDraw.Draw(im)
    T = lambda p: ((p[0] - x0) * k, (y1 - p[1]) * k)
    _draw_items(d, sh.items, T, k, px_per_mm_paper)
    im.save(path)
    return {"png": path, "size": [Wp, Hp], "px_per_model_mm": k, "origin": [x0, y1]}


def _draw_items(d, items, T, k, ppm, color=None, only=None, width_k=1.0):
    cache = {}
    for it in items:
        if only and it["layer"] not in only:
            continue
        t = it["t"]
        col = color or ((40, 40, 40) if it["layer"] in ("墙体", "图框") else (90, 90, 90) if it["layer"] in ("文字", "房间名", "说明")
                        else (60, 110, 160) if it["layer"] == "门窗" else (0, 120, 0) if it["layer"] == "尺寸"
                        else (140, 140, 140) if it["layer"] == "家具" else (60, 60, 60))
        w = max(1, int(round(it.get("lw", 0.25) * ppm * width_k)))
        if t == "fill":
            sh_ = it.get("shade", 0.3)
            fc = color or tuple(int(255 - (255 - c) * sh_ * 2.4) for c in (30, 30, 30))
            d.polygon([T(p) for p in it["pts"]], fill=fc)
        elif t == "line":
            d.line([T(p) for p in it["pts"]], fill=col, width=w)
        elif t == "poly":
            pts = [T(p) for p in it["pts"]] + ([T(it["pts"][0])] if it.get("closed") else [])
            d.line(pts, fill=col, width=w)
        elif t in ("arc", "circle"):
            cx, cy = T(it["c"]); r = it["r"] * k
            bb = [cx - r, cy - r, cx + r, cy + r]
            if t == "circle":
                d.ellipse(bb, outline=col, width=w)
            else:
                # PIL angles run clockwise from +x in image space: CCW a1..a2 maps to -a2..-a1
                d.arc(bb, -it["a2"], -it["a1"], fill=col, width=w)
        elif t == "text":
            size = it["h"] * k / 0.86
            key = int(size)
            if key not in cache:
                cache[key] = _font(size)
            f = cache[key]
            x, y = T((it["x"], it["y"]))
            from PIL import Image as _I, ImageDraw as _D
            tw = int(d.textlength(it["s"], font=f)) + 4
            th = int(size * 1.3) + 4
            if it.get("rot") in (90, 270, -90):
                tile = _I.new("RGBA", (tw, th), (255, 255, 255, 0))
                _D.Draw(tile).text((2, th / 2), it["s"], font=f, fill=col + (255,), anchor="lm")
                tile = tile.rotate(90, expand=True)
                d._image.paste(tile, (int(x - tile.width / 2), int(y - tile.height / 2)), tile)
            else:
                anc = {"mc": "mm", "ml": "lm", "bl": "ls", "tl": "lt"}.get(it["anchor"], "mm")
                d.text((x, y), it["s"], font=f, fill=col, anchor=anc)


def overlay(spec, sh, eng, out, k=3):
    """the generated plan drawn on the source photo with the fitted pixel<->mm transform."""
    from PIL import Image, ImageDraw
    src = spec.get("source") or {}
    T0 = src.get("transform")
    if not (src.get("image") and T0 and os.path.exists(src["image"])):
        return None
    base = Image.open(src["image"]).convert("RGB")
    W, H = base.size
    big = base.resize((W * k, H * k), Image.LANCZOS)
    big = Image.blend(big, Image.new("RGB", big.size, "white"), 0.45)
    d = ImageDraw.Draw(big, "RGBA")
    dx, dy = getattr(eng, "offset", (0, 0))
    m = eng.m

    def T(p):
        X = p[0] - dx; Y = p[1] - dy
        yd = (m.H - Y) if m.y_down else Y
        return ((T0["ax"] * X + T0["bx"]) * k, (T0["ay"] * yd + T0["by"]) * k)
    plan_items = [it for it in sh.items if it["layer"] in ("墙体", "墙体填充", "门窗", "房间名")]
    for it in plan_items:
        if it["t"] == "fill":
            d.polygon([T(p) for p in it["pts"]], fill=(230, 30, 30, 110))
    _draw_items(d, [it for it in plan_items if it["t"] != "fill" and it["t"] != "text"], T, abs(T0["ax"]) * k, 4,
                color=(220, 0, 0))
    for it in plan_items:
        if it["t"] == "text":
            x, y = T((it["x"], it["y"]))
            f = _font(max(10, it["h"] * abs(T0["ax"]) * k / 0.86))
            d.text((x, y), it["s"], font=f, fill=(0, 60, 220, 255), anchor="mm")
    # crop to the plan area (+ margin) so the overlay is readable
    xs, ys = [], []
    for it in plan_items:
        for p in it.get("pts", []) + ([it["c"]] if "c" in it else []):
            q = T(p); xs.append(q[0]); ys.append(q[1])
    if xs:
        mg = 60 * k
        big = big.crop((max(0, int(min(xs) - mg)), max(0, int(min(ys) - mg)), min(big.width, int(max(xs) + mg)),
                        min(big.height, int(max(ys) + mg))))
    big.save(out)
    score = _overlay_score(spec, eng)
    return {"overlay": out, **score}


def _overlay_score(spec, eng):
    """how much of the generated wall area lies on dark (wall-coloured) source pixels, and the reverse."""
    try:
        import numpy as np
        g, (W, H) = _load_gray(spec["source"]["image"])
    except Exception:
        return {}
    T0 = spec["source"]["transform"]; m = eng.m
    dark = g < 150
    hit = tot = 0
    for c in getattr(eng, "wall_cells", []):
        xs = [c[0], c[2]]; ys = [c[1], c[3]]
        px = sorted(T0["ax"] * x + T0["bx"] for x in xs)
        py = sorted(T0["ay"] * ((m.H - y) if m.y_down else y) + T0["by"] for y in ys)
        x0, x1 = int(math.floor(px[0])), int(math.ceil(px[1])); y0, y1 = int(math.floor(py[0])), int(math.ceil(py[1]))
        x0, y0 = max(0, x0 - 1), max(0, y0 - 1); x1, y1 = min(W, x1 + 1), min(H, y1 + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        win = dark[y0:y1, x0:x1]
        tot += 1
        hit += 1 if win.any() else 0
    return {"wall_pieces_on_dark_pixels": f"{hit}/{tot}" if tot else "n/a",
            "wall_match": round(hit / tot, 3) if tot else None}


def cmd_axes(a):
    from PIL import Image, ImageDraw
    spec = load_spec(a.spec)
    rep = fit_transform(spec, write=True)
    T = spec["source"]["transform"]
    im = Image.open(spec["source"]["image"]).convert("RGB")
    k = int(a.k)
    im = im.resize((im.width * k, im.height * k), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    xs, ys = grid_axes(spec)
    for x in xs:
        X = (T["ax"] * x + T["bx"]) * k
        d.line([(X, 0), (X, im.height)], fill=(255, 0, 0), width=1); d.text((X + 2, 2), str(int(x)), fill=(255, 0, 0))
    for y in ys:
        Y = (T["ay"] * y + T["by"]) * k
        d.line([(0, Y), (im.width, Y)], fill=(0, 0, 255), width=1); d.text((2, Y + 2), str(int(y)), fill=(0, 0, 255))
    im.save(a.out)
    print(json.dumps({"out": a.out, "fit": rep}, ensure_ascii=False))
    return 0


def librecad_export(view_dxf, out_dir):
    """real CAD render through LibreCAD's own dxf2pdf / dxf2png when it is installed (optional)."""
    lc = shutil.which("librecad")
    if not lc:
        return {}
    out = {}
    for mode, ext in (("dxf2pdf", "pdf"), ("dxf2png", "png")):
        try:
            env = dict(os.environ); env.setdefault("QT_QPA_PLATFORM", "offscreen")
            subprocess.run([lc, mode, view_dxf], cwd=out_dir, capture_output=True, timeout=120, env=env)
            f = Path(view_dxf).with_suffix("." + ext)
            if f.exists():
                out["librecad_" + ext] = str(f)
        except Exception:
            pass
    return out


# ============================================================================================ commands

def file_md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def stale_reasons(spec, task=None):
    """why this spec does not belong to the current image / task (empty = it does). The spec records the md5 of the
    image it was read from and the task (work folder) that wrote it; `drawkit auto` and every build stamp both."""
    src = spec.get("source") or {}
    out = []
    img = src.get("image")
    if src.get("md5") and img and os.path.exists(img):
        cur = file_md5(img)
        if cur != src["md5"]:
            out.append(f"it was read from an image with md5 {src['md5'][:8]}, but {os.path.basename(img)} is {cur[:8]}")
    if task and src.get("task") and str(src["task"]) != str(task):
        out.append(f"it belongs to task {src['task']}, this is task {task} (a new task always starts a new spec)")
    return out


def _room_problems(spec):
    """room geometry a generator cannot draw: non-rectilinear, degenerate, overlapping."""
    m = Model(spec)
    rooms = []
    for r in spec.get("rooms") or []:
        pts = r.get("poly") or ([[r["box"][0], r["box"][1]], [r["box"][2], r["box"][1]], [r["box"][2], r["box"][3]],
                                  [r["box"][0], r["box"][3]]] if r.get("box") else [])
        rooms.append((r, pts))
    bad = []
    for r, pts in rooms:
        if len(pts) < 3 or abs(poly_area(pts)) < 1e5:
            bad.append(f"{r.get('name')}: degenerate")
            continue
        if any(abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6 for a, b in _edges(pts)):
            bad.append(f"{r.get('name')}: not rectilinear")
    for i, (a, pa) in enumerate(rooms):
        for b, pb in rooms[i + 1:]:
            if len(pa) >= 3 and len(pb) >= 3 and _poly_overlap(pa, pb) > 1e4:
                bad.append(f"{a.get('name')}/{b.get('name')}: overlap")
    del m
    return bad


def repair_spec(spec):
    """3.8.9 auto-repair before the checks, so a wrong pixel box or hand-drawn rooms never loop the model:
    * a chain whose segments all measure the same factor off its written values (the box held another line) or ~0
      (the box missed the line) is re-located in the image from its VALUES (drawauto.detect_chains);
    * rooms that are not rectilinear / overlap / are degenerate are regenerated from the wall grid; their names are
      kept by position (each old room's label point names the new cell group)."""
    src = spec.get("source") or {}
    img = src.get("image")
    out = []
    if not img or not os.path.exists(img) or spec.get("kind", "floor_plan") != "floor_plan":
        return out
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import drawauto as da
    probe = json.loads(json.dumps(spec))
    fr = fit_transform(probe)
    bad = {}
    for e in fr["errors"]:
        mt = re.match(r"(\w+) chain segment (\d+): written ([\d.]+) but the drawing measures ~(-?\d+) mm", e)
        if mt:
            bad.setdefault(mt.group(1), []).append((float(mt.group(3)), float(mt.group(4))))
    for w in fr["warnings"]:
        mt = re.match(r"(\w+) chain: no tick pixels", w)
        if mt:
            bad.setdefault(mt.group(1), [])
    boxed = [c.get("side") for c in chain_list(spec) if c.get("box_px") and not c.get("auto")]
    if bad or boxed:
        for s_ in boxed:
            bad.setdefault(s_, bad.get(s_) or None)
        given = {c.get("side"): c.get("values") for c in chain_list(spec) if c.get("values")}
        det = da.detect_chains(img, given=given, ocr=False)
        by = {c["side"]: c for c in det.get("chains") or [] if c.get("values")}
        for side, segs in bad.items():
            if segs is None:          # a model-given box that measured fine: still use the located line, silently
                c = next((c for c in chain_list(spec) if c.get("side") == side), None)
                d = by.get(side)
                if c is not None and d is not None and d.get("by") == "given":
                    c.pop("box_px", None); c.pop("tick_candidates", None)
                    c["ticks_px"] = d["ticks_px"]; c["auto"] = True
                continue
            ratios = [m_ / w_ for w_, m_ in segs if w_]
            why = ("every segment measured ~0 mm: the box missed the line" if ratios and max(abs(r) for r in ratios) < 0.1 else
                   f"every segment measured x{sum(ratios) / len(ratios):.2f} its value: the box held another line"
                   if ratios and max(ratios) - min(ratios) < 0.15 * max(abs(sum(ratios) / len(ratios)), 1e-6) else
                   "segments did not fit their values")
            c = next((c for c in chain_list(spec) if c.get("side") == side), None)
            d = by.get(side)
            if c is not None and d is not None and d.get("by") == "given":
                old = c.get("box_px")
                c.pop("box_px", None); c.pop("tick_candidates", None)
                c["ticks_px"] = d["ticks_px"]; c["auto"] = True
                out.append(f"{side} chain re-located in the image ({why}; box {old} -> dimension line at "
                           f"{d['line_px']} px, ticks {d['ticks_px']})")
            else:
                out.append(f"{side} chain: {why}; could not re-locate it from its values (they may be misread)")
    probs = _room_problems(spec)
    if probs and spec.get("y_down", True):
        fr = fit_transform(json.loads(json.dumps(spec)), write=False)
        T = fr.get("transform") or {}
        if all(k in T for k in ("ax", "bx", "ay", "by")):
            labs = []
            for r in spec.get("rooms") or []:
                if not r.get("name") or r.get("name") == "房间?":
                    continue
                if r.get("label_px"):
                    x, y = r["label_px"]
                else:
                    pts = r.get("poly") or ([[r["box"][0], r["box"][1]], [r["box"][2], r["box"][3]]] if r.get("box") else [])
                    if r.get("label_at"):
                        mx, my = r["label_at"]
                    elif pts:
                        mx = sum(p[0] for p in pts) / len(pts); my = sum(p[1] for p in pts) / len(pts)
                    else:
                        continue
                    x, y = T["ax"] * mx + T["bx"], T["ay"] * my + T["by"]
                labs.append({"name": r["name"], "x": round(x, 1), "y": round(y, 1), "by": "spec"})
            try:
                auto, info = da.auto_spec(img, chains=chain_list(spec), transform=T, labels=labs, use_ocr=False)
            except Exception as ex:  # noqa: BLE001
                out.append(f"rooms could not be regenerated from the wall grid ({type(ex).__name__}: {ex})")
                auto = None
            if auto and len(auto["rooms"]) >= max(2, len(labs) // 2):
                spec["rooms"] = auto["rooms"]
                spec["no_wall"] = auto.get("no_wall") or []
                if not spec.get("doors"):
                    spec["doors"] = auto.get("doors") or []
                if not spec.get("windows"):
                    spec["windows"] = auto.get("windows") or []
                spec.setdefault("uncertain", []).extend(u for u in auto.get("uncertain") or [] if u not in spec.get("uncertain", []))
                out.append("rooms regenerated from the wall grid (" + "; ".join(probs[:6]) + f"): {len(spec['rooms'])} "
                           "rectilinear rooms, names kept by position")
    return out


def run_build(spec_path, out_dir=None, name="plan", source=None, dxf=True, task=None, repair=True):
    spec = load_spec(spec_path)
    if source:
        spec.setdefault("source", {})["image"] = source
    src = spec.get("source") or {}
    base = Path(spec_path).resolve().parent
    if src.get("image") and not os.path.isabs(src["image"]):
        for cand in (base / src["image"], Path.cwd() / src["image"], Path.home() / "workspace" / src["image"]):
            if cand.exists():
                src["image"] = str(cand); break
    out_dir = Path(out_dir or base)
    out_dir.mkdir(parents=True, exist_ok=True)
    E, W, I = [], [], []
    stale = stale_reasons(spec, task)
    if stale:   # task-41c: a G-plan spec from an earlier image was "revised" for 100 steps against the A3-1 photo
        rep = {"version": VERSION, "kind": spec.get("kind"), "ok": False, "stale": True, "files": {},
               "errors": ["STALE SPEC: " + r for r in stale] + [
                   "this spec was not written for this image / this task: do not build or deliver it. Archive it and "
                   "write a NEW spec for the current image (drawkit.py auto gives a draft)"], "warnings": [], "info": []}
        return rep
    repairs = repair_spec(spec) if repair and os.environ.get("DRAWKIT_NO_REPAIR") != "1" else []
    m = Model(spec)
    e, w, i = chain_checks(spec, m); E += e; W += w; I += i
    if src.get("image") and os.path.exists(src["image"]):
        fr = fit_transform(spec, write=True)
        E += fr["errors"]; W += fr["warnings"]
        if fr.get("transform"):
            I.append("pixel->mm fit: " + ", ".join(f"{k}={v:.5f}" for k, v in fr["transform"].items()))
    eng, sh, scale = make(spec)
    e, w, i = eng.checks(sh); E += e; W += w; I += i
    e, w = text_checks(sh, eng); E += e; W += w
    W += sh.warnings
    if not cjk_font():
        W.append("no Chinese font on this computer (apt install fonts-wqy-microhei): the PNG preview shows boxes for hanzi")
    rep = {"version": VERSION, "kind": spec.get("kind"), "scale": f"1:{scale}", "errors": _uniq(E), "warnings": _uniq(W),
           "info": _uniq(I), "repairs": repairs}
    if spec.get("kind", "floor_plan") == "floor_plan":
        rep["rooms"] = [{"name": r.get("name"), "area_axis_m2": round(poly_area(r["pts"]) / 1e6, 2),
                         "labelled": bool(r.get("_label"))} for r in eng.rooms]
        rep["chains"] = {c.get("side"): c.get("values") for c in chain_list(spec)}
    files = {}
    if dxf:
        try:
            files.update(write_dxf(sh, str(out_dir / f"{name}.dxf")))
        except Exception as ex:  # noqa: BLE001
            rep["errors"].append(f"DXF not written: {type(ex).__name__}: {ex}")
    files.update(render_png(sh, str(out_dir / f"{name}.png"), scale=scale))
    try:   # a PDF of the sheet for delivery (Pillow; LibreCAD's own export stays optional)
        from PIL import Image as _Im
        _Im.open(out_dir / f"{name}.png").convert("RGB").save(out_dir / f"{name}.pdf", "PDF", resolution=150.0)
        files["pdf"] = str(out_dir / f"{name}.pdf")
    except Exception:  # noqa: BLE001
        pass
    ov = overlay(spec, sh, eng, str(out_dir / "overlay.png"))
    if ov:
        files["overlay"] = ov.pop("overlay")
        rep["overlay_score"] = ov
    if dxf and files.get("view_dxf") and os.environ.get("DRAWKIT_LIBRECAD", "1") == "1":
        files.update(librecad_export(files["view_dxf"], str(out_dir)))
    rep["files"] = files
    rep["ok"] = not rep["errors"]
    with open(out_dir / "report.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    if repairs:
        try:
            raw = load_spec(spec_path)
            bak = Path(spec_path).with_name(Path(spec_path).stem + ".before-repair.json")
            with open(bak, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=1)
            for k in ("chains", "rooms", "no_wall", "doors", "windows", "uncertain"):
                if k in spec:
                    raw[k] = [{kk: vv for kk, vv in c.items() if kk not in ("_drawn", "tick_candidates")} if isinstance(c, dict) else c
                              for c in spec[k]]
            with open(spec_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass
    # keep what the fit learned (transform, matched ticks) in the spec so the next round starts from it
    if src.get("transform") or src.get("image"):
        try:
            raw = load_spec(spec_path)
            if src.get("transform"):
                raw.setdefault("source", {})["transform"] = src["transform"]
            if src.get("image") and os.path.exists(src["image"]) and not (raw.get("source") or {}).get("md5"):
                raw.setdefault("source", {})["md5"] = file_md5(src["image"])
            if task and not (raw.get("source") or {}).get("task"):
                raw.setdefault("source", {})["task"] = task
            if isinstance(raw.get("chains"), list):
                with open(spec_path, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return rep


def _uniq(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


def cmd_build(a):
    rep = run_build(a.spec, a.out, a.name, a.source, not a.no_dxf, task=a.task or os.environ.get("DRAWKIT_TASK"))
    print("DRAWKIT_RESULT " + json.dumps(rep, ensure_ascii=False))
    return 0 if rep["ok"] else 2


def cmd_check(a):
    rep = run_build(a.spec, a.out or "/tmp/drawkit-check", "check", None, False)
    print("DRAWKIT_RESULT " + json.dumps({k: rep[k] for k in ("ok", "errors", "warnings", "info")}, ensure_ascii=False))
    return 0 if rep["ok"] else 2


def cmd_fit(a):
    spec = load_spec(a.spec)
    rep = fit_transform(spec, write=a.write)
    e, w, i = chain_checks(spec, Model(spec))
    rep["errors"] = e + rep["errors"]; rep["warnings"] = w + rep["warnings"]; rep["info"] = i
    if a.write:
        with open(a.spec, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if not rep["errors"] else 2


def _read_json_arg(v):
    if not v:
        return None
    if os.path.exists(v):
        with open(v, encoding="utf-8") as f:
            return json.load(f)
    return json.loads(v)


def cmd_auto(a):
    """draft spec from the image: walls from pixels, scale from the chains, rooms from labels (see drawauto.py)."""
    need("numpy"); need("PIL", "pillow")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import drawauto as da
    src = _read_json_arg(a.chains) or {}
    chains = src.get("chains") if isinstance(src, dict) else src
    if isinstance(chains, dict):          # facts.json keeps chains by side
        chains = [dict(v, side=k) for k, v in chains.items() if isinstance(v, dict)]
    T = None
    if a.transform:
        v = [float(t) for t in a.transform.split(",")]
        T = dict(zip(("ax", "bx", "ay", "by"), v))
    elif isinstance(src, dict) and (src.get("source") or {}).get("transform") and not a.refit:
        T = src["source"]["transform"]
    labs = _read_json_arg(a.labels) or []
    if isinstance(labs, dict):
        labs = labs.get("labels") or []
    image = os.path.abspath(a.image)
    if getattr(a, "from_spec", None) and os.path.exists(a.from_spec):
        # 3.8.9 the deterministic pipeline after a failed / looping round: the old spec gives the chain VALUES and
        # the room NAMES by position (its geometry is not reused); a spec of another image is ignored
        try:
            old = load_spec(a.from_spec)
            if (old.get("source") or {}).get("md5") in (None, file_md5(image)):
                have = {c.get("side") for c in chains or []}
                chains = list(chains or []) + [{"side": c["side"], "values": c["values"]} for c in chain_list(old)
                                               if c.get("values") and c.get("side") not in have]
                if not labs:     # names by position, in the chains' mm frame (placed with the NEW transform)
                    for r in old.get("rooms") or []:
                        if not r.get("name") or r["name"] == "房间?":
                            continue
                        if r.get("label_px"):
                            labs.append({"name": r["name"], "x": r["label_px"][0], "y": r["label_px"][1], "by": "spec"})
                            continue
                        pts = r.get("poly") or ([[r["box"][0], r["box"][1]], [r["box"][2], r["box"][3]]] if r.get("box") else [])
                        if r.get("label_at"):
                            labs.append({"name": r["name"], "mm": list(r["label_at"]), "by": "spec"})
                        elif pts:
                            labs.append({"name": r["name"], "mm": [sum(p[0] for p in pts) / len(pts),
                                                                   sum(p[1] for p in pts) / len(pts)], "by": "spec"})
        except Exception:  # noqa: BLE001
            pass

    def fit(spec):
        sp = json.loads(json.dumps(spec))
        sp["source"]["image"] = image
        return fit_transform(sp)
    try:
        spec, info = da.auto_spec(image, chains=chains, transform=T, labels=labs, use_ocr=not a.no_ocr, task=a.task,
                                  allow_install=a.install_ocr, debug=a.debug, fit=fit)
    except da.NeedValues as e:
        segs = {c["side"]: c.get("segments") for c in e.det.get("chains") or []}
        print("DRAWKIT_AUTO " + json.dumps({"ok": False, "error": str(e), "need_values": sorted(e.boxes),
                                             "chain_boxes": e.boxes, "segments": segs,
                                             "found": [c["side"] for c in e.det.get("chains") or [] if c.get("values")]},
                                            ensure_ascii=False))
        return 3
    except ValueError as e:
        print("DRAWKIT_AUTO " + json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    spec["source"]["image"] = a.image          # keep the path as given (relative to ~/workspace)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=1)
    rooms = [{"name": r["name"], "box": r.get("box") or da.poly_box(r["poly"])} for r in spec["rooms"]]
    det = info.get("chains_detected") or {}
    print("DRAWKIT_AUTO " + json.dumps({"ok": True, "out": a.out, "rooms": rooms, "labels": info.get("labels"),
                                         "chains": [{k: c.get(k) for k in ("side", "values", "by", "ticks_px")}
                                                    for c in spec["chains"]], "checks": det.get("checks"),
                                         "openings": info.get("openings"), "wall_mode": info.get("wall_mode"),
                                         "x_lines": info.get("x_lines"), "y_lines": info.get("y_lines"),
                                         "x_unsnapped": info.get("x_unsnapped"), "y_unsnapped": info.get("y_unsnapped"),
                                         "warnings": info.get("warnings"), "uncertain": spec["uncertain"],
                                         "debug": info.get("debug"), "md5": spec["source"]["md5"],
                                         "plan_px": info.get("plan_px"), "transform": spec["source"].get("transform")}, ensure_ascii=False))
    return 0


SCHEMA = """spec (JSON). Lengths in mm. Floor plans read from a photo use y_down=true: y grows DOWN from the top axis,
like the picture, so left/right chains are listed top->bottom (the generator flips to CAD y-up itself).
{
 "schema": "aiw-drawing/1", "kind": "floor_plan" | "diagram" | "generic",
 "title": "G户型 平面图", "subtitle": "三房两厅两卫 约108m²", "project": "...", "paper": "A3", "scale": null (auto),
 "y_down": true,
 "source": {"image": "uploads/x.jpg", "transform": {"ax","bx","ay","by"} (written by fit/build)},
 "chains": [ {"side": "top|bottom|left|right", "values": [1300,1800,...], "start": 0,
              "reads": [[...first zoomed read...], [...second read...]], "box_px": [x0,y0,x1,y1] (chain region in the
              photo, for automatic tick finding) or "ticks_px": [px of every tick], "confidence": "high|medium|low",
              "explain": "why this chain's total differs from the opposite one (setback)"} ],
 "walls": {"exterior": 240, "interior": 120, "railing": 100},
 "rooms": [ {"id": "living", "name": "客厅", "poly": [[x,y],...] or "box": [x0,y0,x1,y1], "type": "room|balcony|bath|kitchen",
             "zone": "public" (rooms of one zone are open to each other: no wall between them), "confidence": "high",
             "label_at": [x,y] (optional fixed label point), "show_area": false} ],
 "doors": [ {"id": "d1", "at": [x,y] (centre, ON a room edge), "width": 900, "into": "room id", "hinge": "start|end",
             "type": "swing|double|sliding|opening"} ],
 "windows": [ {"at": [x,y], "width": 1500} or {"a": [x,y], "b": [x,y]} ],
 "no_wall": [ {"a": [x,y], "b": [x,y]} ],  "extra_walls": [ {"a": [x,y], "b": [x,y], "t": 120} ],
 "furniture": [ {"type": "bed|sofa|dining_table|table|desk|counter|stove|sink|wardrobe|tv|toilet|basin|shower|bath|box",
                 "box": [x0,y0,x1,y1], "head": "top|bottom|left|right" (beds), "label": optional} ],
 "notes": ["..."], "uncertain": ["what could not be read for sure, and the value used"]
}
diagram: "nodes": [{"id","text","box":[x0,y0,x1,y1] | "at":[x,y],"w","h","shape":"box|circle|diamond"}],
         "edges": [{"from","to","label","route":"ortho|straight"}]
generic: "entities": [{"type":"line","a","b"} | {"type":"poly","points","closed"} | {"type":"rect","box"} |
         {"type":"circle","c","r"} | {"type":"arc","c","r","a1","a2"} | {"type":"centerline","a","b"} |
         {"type":"text","at","text","h_mm"}], "dims": [{"a","b","offset_mm","value"}], "labels": [{"at","text"}]
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="drawkit.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("zoom"); p.add_argument("image"); p.add_argument("--box", required=True); p.add_argument("--k", default=4)
    p.add_argument("--rotate", default=0); p.add_argument("--grid", action="store_true"); p.add_argument("--out", required=True)
    p.set_defaults(f=cmd_zoom)
    p = sub.add_parser("ticks"); p.add_argument("image"); p.add_argument("--side", required=True, choices=SIDES)
    p.add_argument("--box", required=True); p.set_defaults(f=cmd_ticks)
    p = sub.add_parser("fit"); p.add_argument("spec"); p.add_argument("--write", action="store_true"); p.set_defaults(f=cmd_fit)
    p = sub.add_parser("axes"); p.add_argument("spec"); p.add_argument("--out", required=True); p.add_argument("--k", default=3)
    p.set_defaults(f=cmd_axes)
    p = sub.add_parser("build"); p.add_argument("spec"); p.add_argument("--out"); p.add_argument("--name", default="plan")
    p.add_argument("--source"); p.add_argument("--no-dxf", action="store_true"); p.add_argument("--task")
    p.set_defaults(f=cmd_build)
    p = sub.add_parser("check"); p.add_argument("spec"); p.add_argument("--out"); p.set_defaults(f=cmd_check)
    p = sub.add_parser("auto"); p.add_argument("image"); p.add_argument("--chains"); p.add_argument("--transform")
    p.add_argument("--labels"); p.add_argument("--no-ocr", action="store_true"); p.add_argument("--install-ocr", action="store_true")
    p.add_argument("--refit", action="store_true"); p.add_argument("--task"); p.add_argument("--out"); p.add_argument("--debug")
    p.add_argument("--from-spec", dest="from_spec")
    p.set_defaults(f=cmd_auto)
    p = sub.add_parser("schema"); p.set_defaults(f=lambda a: print(SCHEMA) or 0)
    p = sub.add_parser("ensure"); p.set_defaults(f=cmd_ensure)
    a = ap.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
