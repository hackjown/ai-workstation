#!/usr/bin/env python3
"""drawauto: a DRAFT floor-plan spec straight from the source image (drawkit.py auto).

Why (task-41c, 200 steps, stopped by the user): the model re-cropped the photo and asked the vision model where each
room label sits ~80 times, got a different answer every time, and never committed anything. Walls, the pixel->mm
transform and the label positions are measurable, so they are measured here once and written down:

  1. pixel->mm transform from the dimension chains (drawkit fit: tick pixels of each chain), or one given
  2. wall lines: thick dark "ridges" (darker than both sides, low colour saturation), projected per row / column ->
     candidate lines, snapped to the chains' axis marks (or to 50 mm when they sit between marks)
  3. every edge of the resulting grid is tested for wall coverage; cells that are paper-coloured and reachable from
     outside without crossing a wall are outside the flat
  4. cells joined across edges without a wall form rooms (union-find); one region with several labels (an open
     living/dining/entry) is split between its labels by nearest cell and given one zone
  5. room labels: OCR (rapidocr-onnxruntime, optional) on the enlarged plan, kept only when they are room words,
     or a labels JSON [{"name","x","y"}] in source pixels (the agent asks the vision model ONCE on a gridded crop)
  6. writes the draft spec (source md5 + task stamp, chains, rooms, notes/uncertain) and a debug image.

The model then only CORRECTS the draft (doors, windows, a misplaced wall), it does not re-derive the layout.
Needs numpy + Pillow. Pure functions are importable for tests.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

ROOM_WORDS = ("主卧", "次卧", "卧室", "儿童房", "老人房", "客房", "书房", "客厅", "起居室", "餐厅", "厨房", "卫生间", "主卫",
              "客卫", "公卫", "阳台", "生活阳台", "景观阳台", "露台", "衣帽间", "玄关", "门厅", "过道", "走廊", "储藏", "储物",
              "管井", "设备平台", "空调位", "楼梯", "电梯", "飘窗", "洗衣房", "家政间", "多功能房", "工人房", "茶室")
ROOM_RE = re.compile("|".join(sorted(ROOM_WORDS, key=len, reverse=True)))
TYPE_OF = {"阳台": "balcony", "露台": "balcony", "设备平台": "balcony", "空调位": "balcony", "卫": "bath", "卫生间": "bath",
           "厨房": "kitchen"}
PUBLIC = ("客厅", "起居室", "餐厅", "玄关", "门厅", "过道", "走廊")


def md5_of(path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def room_type(name: str) -> str:
    if any(w in name for w in ("阳台", "露台", "设备平台", "空调位")):
        return "balcony"
    if "卫" in name:
        return "bath"
    if "厨" in name:
        return "kitchen"
    return "room"


def clean_label(txt: str):
    """an OCR string -> a room word, or None ('次卧 12.6m²' -> '次卧'; '胎' -> None)."""
    m = ROOM_RE.search(txt or "")
    return m.group(0) if m else None


# ------------------------------------------------------------------------------------------------ image measures

def load(path):
    import numpy as np
    from PIL import Image
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(float) / 255.0
    return im, a


def ridge_masks(a, d):
    """vertical-wall mask (a pixel darker than the pixels d to its left AND right) and horizontal-wall mask, both
    restricted to fairly neutral, fairly dark pixels (walls are grey/black; floors, carpets, beds are coloured)."""
    import numpy as np
    lum = a @ np.array([0.299, 0.587, 0.114])
    mx, mn = a.max(2), a.min(2)
    sat = (mx - mn) / np.maximum(mx, 1e-3)
    ok = (sat < 0.5) & (lum < 0.72)
    H, W = lum.shape
    pad = np.pad(lum, d, mode="edge")
    left, right = pad[d:d + H, 0:W], pad[d:d + H, 2 * d:2 * d + W]
    up, down = pad[0:H, d:d + W], pad[2 * d:2 * d + H, d:d + W]
    depth = 0.075
    vert = ok & (np.minimum(left, right) - lum > depth)
    hor = ok & (np.minimum(up, down) - lum > depth)
    strong = (lum < 0.3) & (sat < 0.5)        # black walls are walls whatever their surroundings
    return vert | strong, hor | strong, lum, sat


def paper_mask(a):
    """pixels of the sheet's background colour (estimated from the image border)."""
    import numpy as np
    H, W, _ = a.shape
    b = max(2, min(H, W) // 40)
    border = np.concatenate([a[:b].reshape(-1, 3), a[-b:].reshape(-1, 3), a[:, :b].reshape(-1, 3), a[:, -b:].reshape(-1, 3)])
    ref = np.median(border, 0)
    if ref.max() < 0.6:          # a dark border (photo of a screen): use the brightest common colour instead
        flat = a.reshape(-1, 3)
        ref = np.median(flat[flat.mean(1) > np.percentile(flat.mean(1), 90)], 0)
    # a pure white page next to a white tiled floor (A3-1: page 255, tiles 235-249) needs a tight tolerance
    tol = 0.02 if float(ref.min()) > 0.97 else 0.085
    return np.abs(a - ref).max(2) < tol


def line_candidates(mask, lo, hi, a0, a1, min_len):
    """positions (along the other axis) of long wall runs: mask rows/cols lo..hi, counted between a0..a1."""
    import numpy as np
    sub = mask[lo:hi, a0:a1]
    prof = sub.sum(1).astype(float)
    # a smoothed profile, peaks well above the floor, at least min_len pixels of wall
    k = np.array([0.25, 0.5, 0.25])
    sm = np.convolve(prof, k, mode="same")
    out = []
    for i in range(1, len(sm) - 1):
        if sm[i] >= min_len and sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1]:
            w = sm[i - 1:i + 2]
            c = lo + i + (w[2] - w[0]) / max(float(w.sum()), 1e-6)
            if out and c - out[-1][0] < 2.5:
                if sm[i] > out[-1][1]:
                    out[-1] = (c, float(sm[i]))
                continue
            out.append((c, float(sm[i])))
    return out


def coverage(mask, horiz, pos, s0, s1, band):
    """fraction of samples along a grid edge (row/col `pos`, from s0 to s1) that have wall pixels within +-band."""
    import numpy as np
    H, W = mask.shape
    p0, p1 = int(round(pos - band)), int(round(pos + band)) + 1
    a, b = int(round(min(s0, s1))), int(round(max(s0, s1)))
    if horiz:
        p0, p1 = max(0, p0), min(H, p1)
        a, b = max(0, a), min(W, b)
        if p1 <= p0 or b - a < 2:
            return 0.0
        col = mask[p0:p1, a:b].any(0)
    else:
        p0, p1 = max(0, p0), min(W, p1)
        a, b = max(0, a), min(H, b)
        if p1 <= p0 or b - a < 2:
            return 0.0
        col = mask[a:b, p0:p1].any(1)
    # trim the ends (the crossing walls themselves)
    t = max(1, int(0.12 * len(col)))
    core = col[t:-t] if len(col) > 2 * t + 2 else col
    return float(np.mean(core)) if len(core) else 0.0


# ------------------------------------------------------------------------------------------------ grid -> rooms

def snap_axis(cands_px, a, b, marks, total, tol_mm):
    """candidate pixel lines -> sorted [(mm, px, snapped)] inside 0..total; chain marks always included."""
    out = {}
    for m in marks:
        out[round(m)] = {"mm": float(m), "px": a * m + b, "mark": True, "seen": False}
    for p, _s in cands_px:
        mm = (p - b) / a
        if mm < -tol_mm or mm > total + tol_mm:
            continue
        near = min(marks, key=lambda m: abs(m - mm)) if marks else None
        if near is not None and abs(near - mm) <= tol_mm:
            o = out[round(near)]
            if not o["seen"]:
                o["px"], o["seen"] = p, True
            continue
        v = round(mm / 50.0) * 50.0
        if any(abs(v - k) <= tol_mm for k in out):
            continue
        out[round(v)] = {"mm": v, "px": p, "mark": False, "seen": True}
    return [out[k] for k in sorted(out)]


def run_lengths(mask, axis):
    """for every True pixel, the length of the run of True pixels it belongs to, along `axis` (1 = rows)."""
    import numpy as np
    m = mask if axis == 1 else mask.T
    H, W = m.shape
    pad = np.zeros((H, W + 2), bool); pad[:, 1:-1] = m
    d = np.diff(pad.astype(np.int8), axis=1)
    out = np.zeros((H, W), np.int32)
    for r in range(H):
        st = np.nonzero(d[r] == 1)[0]; en = np.nonzero(d[r] == -1)[0]
        for a_, b_ in zip(st, en):
            out[r, a_:b_] = b_ - a_
    return out if axis == 1 else out.T


def directional_walls(mask, tmax, lmin):
    """split a wall mask into vertical and horizontal wall pixels: thin across (<= tmax), long along (>= lmin).
    A blob (a patterned rug, a dark sofa) is thick both ways and drops out."""
    hr, vr = run_lengths(mask, 1), run_lengths(mask, 0)
    return mask & (hr <= tmax) & (vr >= lmin), mask & (vr <= tmax) & (hr >= lmin)


def build_rooms(wv, wh, paper, xs, ys, band, thr=0.42, barrier=None):
    """xs/ys: axis lists from snap_axis. Returns cells, region id per inside cell, wall flags. barrier: a mask of
    any drawn line (windows are two thin lines, not a wall); the outside is flooded only through edges that have
    neither a wall nor a fully drawn line, so a white floor next to a window is not taken for the page."""
    nx, ny = len(xs) - 1, len(ys) - 1
    import numpy as np
    vwall = np.zeros((ny, nx + 1), bool)    # vertical edge left of cell (j, i) at x index i
    hwall = np.zeros((ny + 1, nx), bool)    # horizontal edge above cell (j, i) at y index j
    vcov = np.zeros((ny, nx + 1)); hcov = np.zeros((ny + 1, nx))
    for j in range(ny):
        for i in range(nx + 1):
            c = coverage(wv, False, xs[i]["px"], ys[j]["px"], ys[j + 1]["px"], band)
            vcov[j, i] = c; vwall[j, i] = c >= thr
    for j in range(ny + 1):
        for i in range(nx):
            c = coverage(wh, True, ys[j]["px"], xs[i]["px"], xs[i + 1]["px"], band)
            hcov[j, i] = c; hwall[j, i] = c >= thr
    # paper fraction of each cell's core
    pap = np.zeros((ny, nx))
    for j in range(ny):
        for i in range(nx):
            x0, x1 = sorted((xs[i]["px"], xs[i + 1]["px"])); y0, y1 = sorted((ys[j]["px"], ys[j + 1]["px"]))
            dx, dy = (x1 - x0) * 0.2, (y1 - y0) * 0.2
            sub = paper[int(y0 + dy):max(int(y0 + dy) + 1, int(y1 - dy)), int(x0 + dx):max(int(x0 + dx) + 1, int(x1 - dx))]
            pap[j, i] = float(sub.mean()) if sub.size else 1.0
    vbar, hbar = vwall.copy(), hwall.copy()
    if barrier is not None:
        for j in range(ny):
            for i in range(nx + 1):
                vbar[j, i] |= coverage(barrier, False, xs[i]["px"], ys[j]["px"], ys[j + 1]["px"], max(2.0, band * 0.6)) >= 0.75
        for j in range(ny + 1):
            for i in range(nx):
                hbar[j, i] |= coverage(barrier, True, ys[j]["px"], xs[i]["px"], xs[i + 1]["px"], max(2.0, band * 0.6)) >= 0.75
    # outside: flood from the ring through edges without a wall, into paper-like cells
    outside = np.zeros((ny, nx), bool)
    stack = []
    for j in range(ny):
        for i in range(nx):
            border = (i == 0 and not vbar[j, 0]) or (i == nx - 1 and not vbar[j, nx]) or \
                     (j == 0 and not hbar[0, i]) or (j == ny - 1 and not hbar[ny, i])
            if border and pap[j, i] > 0.35:
                outside[j, i] = True; stack.append((j, i))
    while stack:
        j, i = stack.pop()
        for dj, di, wall in ((0, -1, vbar[j, i]), (0, 1, vbar[j, i + 1] if i + 1 <= nx else True),
                             (-1, 0, hbar[j, i]), (1, 0, hbar[j + 1, i] if j + 1 <= ny else True)):
            q, r = j + dj, i + di
            if 0 <= q < ny and 0 <= r < nx and not wall and not outside[q, r] and pap[q, r] > 0.35:
                outside[q, r] = True; stack.append((q, r))
    outside |= pap > 0.8
    # union-find of inside cells
    parent = {}

    def find(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]; c = parent[c]
        return c
    for j in range(ny):
        for i in range(nx):
            if not outside[j, i]:
                parent[(j, i)] = (j, i)
    for j in range(ny):
        for i in range(nx):
            if outside[j, i]:
                continue
            if i + 1 < nx and not outside[j, i + 1] and not vwall[j, i + 1]:
                parent[find((j, i))] = find((j, i + 1))
            if j + 1 < ny and not outside[j + 1, i] and not hwall[j + 1, i]:
                parent[find((j, i))] = find((j + 1, i))
    regions = {}
    for c in parent:
        regions.setdefault(find(c), []).append(c)
    return {"regions": list(regions.values()), "outside": outside, "vwall": vwall, "hwall": hwall,
            "vcov": vcov, "hcov": hcov, "paper": pap}


def split_by_walls(cells, seeds, g, xs, ys, penalty=30000.0):
    """one region with several labels (rooms joined through door gaps / an open plan): every cell goes to the label
    it reaches most cheaply, where crossing a grid edge costs its length plus `penalty` x its wall coverage. A door
    gap in a wall still costs a lot, an open edge almost nothing, so rooms end at their walls. -> {k: [cells]}"""
    import heapq
    cs = set(cells)
    owner = {}
    cen = lambda c: ((xs[c[1]]["mm"] + xs[c[1] + 1]["mm"]) / 2, (ys[c[0]]["mm"] + ys[c[0] + 1]["mm"]) / 2)  # noqa: E731
    pq = [(0.0, k, c) for k, c in enumerate(seeds) if c in cs]
    heapq.heapify(pq)
    while pq:
        d, k, c = heapq.heappop(pq)
        if c in owner:
            continue
        owner[c] = k
        j, i = c
        for q, cov in (((j, i + 1), g["vcov"][j, i + 1]), ((j, i - 1), g["vcov"][j, i]),
                       ((j + 1, i), g["hcov"][j + 1, i]), ((j - 1, i), g["hcov"][j, i])):
            if q in cs and q not in owner:
                a, b = cen(c), cen(q)
                heapq.heappush(pq, (d + abs(a[0] - b[0]) + abs(a[1] - b[1]) + penalty * float(cov), k, q))
    groups = {k: [] for k in range(len(seeds))}
    for c, k in owner.items():
        groups[k].append(c)
    return groups


def cells_poly(cells, xs, ys):
    """union of grid cells -> one rectilinear polygon (mm) by walking its boundary edges (outer ring only)."""
    cs = set(cells)
    edges = {}
    for (j, i) in cs:
        x0, x1, y0, y1 = xs[i]["mm"], xs[i + 1]["mm"], ys[j]["mm"], ys[j + 1]["mm"]
        if (j - 1, i) not in cs:
            edges[(x0, y0)] = edges.get((x0, y0), []) + [(x1, y0)]
        if (j, i + 1) not in cs:
            edges[(x1, y0)] = edges.get((x1, y0), []) + [(x1, y1)]
        if (j + 1, i) not in cs:
            edges[(x1, y1)] = edges.get((x1, y1), []) + [(x0, y1)]
        if (j, i - 1) not in cs:
            edges[(x0, y1)] = edges.get((x0, y1), []) + [(x0, y0)]
    if not edges:
        return []
    start = min(edges)
    ring, cur, prev_dir = [start], start, None
    for _ in range(4 * len(cs) + 8):
        nxts = edges.get(cur) or []
        if not nxts:
            break
        if len(nxts) > 1 and prev_dir is not None:   # pinch point: turn left first (keeps the ring simple)
            nxts.sort(key=lambda p: ((math.atan2(p[1] - cur[1], p[0] - cur[0]) - prev_dir) % (2 * math.pi)))
        nxt = nxts.pop(0)
        prev_dir = math.atan2(nxt[1] - cur[1], nxt[0] - cur[0])
        cur = nxt
        if cur == start:
            break
        ring.append(cur)
    # drop collinear points
    out = []
    n = len(ring)
    for k in range(n):
        p, q, r = ring[k - 1], ring[k], ring[(k + 1) % n]
        if (q[0] - p[0]) * (r[1] - q[1]) - (q[1] - p[1]) * (r[0] - q[0]) != 0:
            out.append([round(q[0]), round(q[1])])
    return out


def poly_box(poly):
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def poly_area(pts):
    s = 0.0
    for k in range(len(pts)):
        x0, y0 = pts[k - 1]; x1, y1 = pts[k]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2


# ------------------------------------------------------------------------------------------------ labels

def ocr_labels(im, box, k=6, allow_install=False):
    """room labels from rapidocr inside the plan box (source pixels). [] when the OCR is not available."""
    import importlib.util
    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        if not allow_install or os.environ.get("DRAWKIT_NO_OCR_INSTALL") == "1":
            return [], "rapidocr-onnxruntime not installed"
        import subprocess
        mark = Path.home() / ".config/aiw/rapidocr-tried"
        if mark.exists():
            return [], "rapidocr-onnxruntime not installed (install tried before)"
        mark.parent.mkdir(parents=True, exist_ok=True); mark.touch()
        # 3.8.9: installed in the background (it took minutes in the foreground); this run uses the vision read
        try:
            subprocess.Popen([sys.executable, "-m", "pip", "install", "--user", "-q", "rapidocr-onnxruntime"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception:  # noqa: BLE001
            pass
        return [], "rapidocr-onnxruntime is being installed in the background (next drawing uses it)"
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(im.width, x1), min(im.height, y1)
    crop = im.crop((x0, y0, x1, y1))
    k = max(1, min(k, int(2400 / max(1, max(crop.size)))))
    big = crop.resize((crop.width * k, crop.height * k), Image.LANCZOS)
    # 3.8.9: the OCR runs in a child process with a hard timeout (task-3fc: tesseract on the whole photo hung 60 s)
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        big.save(os.path.join(td, "in.png"))
        code = ("import json,sys\nfrom rapidocr_onnxruntime import RapidOCR\nr,_=RapidOCR()(sys.argv[1])\n"
                "print(json.dumps([[q,t,float(s)] for q,t,s in (r or [])],ensure_ascii=False))")
        try:
            pr = subprocess.run([sys.executable, "-c", code, os.path.join(td, "in.png")], capture_output=True, text=True,
                                timeout=OCR_TIMEOUT)
            res = json.loads((pr.stdout or "[]").strip().splitlines()[-1]) if pr.stdout.strip() else []
        except Exception:  # noqa: BLE001
            return [], f"OCR gave up after {OCR_TIMEOUT:.0f} s"
    out = []
    for quad, txt, sc in res or []:
        name = clean_label(txt)
        if not name:
            continue
        cx = sum(p[0] for p in quad) / 4 / k + x0
        cy = sum(p[1] for p in quad) / 4 / k + y0
        out.append({"name": name, "x": round(cx, 1), "y": round(cy, 1), "text": txt, "score": round(float(sc), 2),
                    "by": "ocr"})
    return out, None


def merge_labels(*lists, near=6.0):
    out = []
    for lst in lists:
        for lab in lst or []:
            if not lab.get("name"):
                continue
            if any(o["name"] == lab["name"] and abs(o["x"] - lab["x"]) < near and abs(o["y"] - lab["y"]) < near for o in out):
                continue
            out.append(dict(lab))
    return out


# ------------------------------------------------------------------------------------------------ main entry

class NeedValues(ValueError):
    """the chains were found but their numbers could not be read: the caller reads each chain crop once (VLM, strict
    JSON) and calls again with the values."""
    def __init__(self, det):
        self.det = det
        boxes = {}
        for side, ls in (det.get("lines") or {}).items():
            c = next((c for c in det.get("chains") or [] if c["side"] == side), None)
            if c and side in det.get("need_values", []):
                p = c["line_px"]; tk = c["ticks_px"]
                boxes[side] = ([tk[0] - 6, p - 30, tk[-1] + 6, p + 30] if side in ("top", "bottom") else
                               [p - 30, tk[0] - 6, p + 30, tk[-1] + 6])
                boxes[side] = [int(round(v)) for v in boxes[side]]
        self.boxes = boxes
        segs = {c["side"]: c.get("segments") for c in det.get("chains") or [] if c.get("segments")}
        super().__init__("NEED VALUES: the dimension chains were found but their numbers could not be read: "
                         + ", ".join(f"{k} ({segs.get(k)} segments, crop {v})" for k, v in boxes.items()))


def auto_spec(image, chains=None, transform=None, labels=None, use_ocr=True, task=None, allow_install=False,
              debug=None, fit=None):
    """-> (spec, info). chains: spec-style chain list (side, values, box_px...); transform: {ax,bx,ay,by} with
    px = a*mm + b; fit: callable(spec)->{"transform":...} (drawkit.fit_transform)."""
    import numpy as np
    im, a = load(image)
    H, W = a.shape[:2]
    chains = [dict(c) for c in (chains or []) if isinstance(c, dict) and c.get("values")]
    info = {"warnings": [], "labels": []}
    spec = {"schema": "aiw-drawing/1", "kind": "floor_plan", "title": "户型平面图（自动草稿）", "y_down": True,
            "draft": True,
            "source": {"image": str(image), "md5": md5_of(image), "size": [W, H]},
            "chains": chains, "walls": {"exterior": 240, "interior": 120, "railing": 100},
            "rooms": [], "doors": [], "windows": [], "furniture": [], "notes": [], "uncertain": []}
    if task:
        spec["source"]["task"] = task
    T = dict(transform or {})
    if not T and not os.environ.get("DRAWKIT_NO_DETECT"):
        # 3.8.9: the chains are FOUND in the image; the model's values only help name them (never its boxes)
        given = {c["side"]: c["values"] for c in chains if c.get("side") in ("top", "bottom", "left", "right")}
        det = detect_chains(image, given=given, ocr=not os.environ.get("DRAWKIT_NO_DIGIT_OCR"), a=a, im=im)
        info["chains_detected"] = {k: det.get(k) for k in ("bbox", "checks", "warnings", "need_values", "px_per_mm", "ocr")}
        found = chains_for_spec(det)
        have = {c["side"] for c in found}
        # a side the detector could not find keeps the model's own chain (box_px) as the last resort
        chains = found + [c for c in chains if c.get("side") not in have and (c.get("box_px") or c.get("ticks_px"))]
        spec["chains"] = chains
        if det.get("need_values") and not ({"top", "bottom"} & have and {"left", "right"} & have):
            raise NeedValues(det)
        info["warnings"] += det.get("warnings") or []
    if not T and chains and fit is not None:
        try:
            T = (fit(spec) or {}).get("transform") or {}
        except Exception as e:  # noqa: BLE001
            info["warnings"].append(f"fit failed: {e}")
    if not all(k in T for k in ("ax", "bx", "ay", "by")):
        raise ValueError("no pixel->mm transform: read the dimension chains first (drawing_read) and pass them with "
                         "--chains, or give --transform ax,bx,ay,by")
    spec["source"]["transform"] = {k: round(float(v), 6) for k, v in T.items() if k in ("ax", "bx", "ay", "by")}
    spec["source"]["transform_note"] = "px = ax*mm + bx (mm -> image px); mm = (px - bx) / ax. free_labels accept at_px."

    def marks(sides):
        ms = set()
        tot = 0.0
        for c in chains:
            if c.get("side") in sides:
                acc = float(c.get("start") or 0); ms.add(acc)
                for v in c["values"]:
                    acc += float(v); ms.add(acc)
                tot = max(tot, acc)
        return sorted(ms), tot
    mx_, tx = marks(("top", "bottom"))
    my_, ty = marks(("left", "right"))
    if not tx or not ty:
        raise ValueError("need at least one horizontal (top/bottom) and one vertical (left/right) chain")
    ax, bx, ay, by = T["ax"], T["bx"], T["ay"], T["by"]
    s = (abs(ax) + abs(ay)) / 2                     # px per mm
    d = max(2, int(round(250 * s)))                 # ridge test distance (wider than a wall)
    band = max(1.5, 140 * s)                        # wall search band around a grid line
    wv, wh, lum, sat = ridge_masks(a, d)
    paper = paper_mask(a)
    px0, px1 = sorted((bx, ax * tx + bx)); py0, py1 = sorted((by, ay * ty + by))
    # 3.8.9: a coloured marketing plan (wood floor, furniture, watermark) draws its walls as THICK dark-grey bands.
    # There the ridge test fires on every sofa and bed outline; the thick-dark mask (an opening that removes 1-2 px
    # lines) is the wall layer instead, and any drawn line (windows) bounds the outside flood.
    dk_ = (lum < 0.45) & (sat < 0.35)
    thick1 = _dilate(_erode(dk_, 1), 1)
    # walls drawn as two thin black lines with a light core (A3-1's baths): close the core first, then open
    r2 = max(2, int(round(60 * s)))
    thick = thick1 | _dilate(_erode(_erode(_dilate(dk_, r2), r2), r2), r2)
    sub = thick[int(py0):int(py1) + 1, int(px0):int(px1) + 1]
    perim = 2 * ((px1 - px0) + (py1 - py0))
    barrier = None
    mode = "ridge"
    cand_mask = None
    if sub.size and sub.sum() >= 2.5 * perim:
        tmax, lmin = max(4, int(round(330 * s))), max(6, int(round(450 * s)))
        wv, wh = directional_walls(thick, tmax, lmin)
        cand_mask = thick1
        barrier = (lum < 0.6)
        mode = "thick"
    info["wall_mode"] = mode
    pad = 300 * s
    X0, X1 = int(max(0, px0 - pad)), int(min(W, px1 + pad)); Y0, Y1 = int(max(0, py0 - pad)), int(min(H, py1 + pad))
    min_len = max(4, 700 * s)
    cv_, ch_ = (directional_walls(cand_mask, tmax, lmin)) if cand_mask is not None else (wv, wh)
    cx = line_candidates(cv_.T, X0, X1, Y0, Y1, min_len)   # columns: transpose so rows = x
    cy = line_candidates(ch_, Y0, Y1, X0, X1, min_len)
    tol = 160.0
    xs = snap_axis(cx, ax, bx, mx_, tx, tol)
    ys = snap_axis(cy, ay, by, my_, ty, tol)
    # drop lines a hair apart (two edges of one wall) keeping the chain mark
    def thin(lst, gap=330.0):
        out = []
        for e in lst:
            if out and e["mm"] - out[-1]["mm"] < gap:
                if e["mark"] and not out[-1]["mark"]:
                    out[-1] = e
                continue
            out.append(e)
        return out
    xs, ys = thin(xs), thin(ys)
    g = build_rooms(wv, wh, paper, xs, ys, band, barrier=barrier)
    info["_grid"] = (xs, ys, g)
    # labels (a label given in mm - a room of an earlier spec - is placed with this transform)
    labs = []
    for lb in labels or []:
        lb = dict(lb)
        if lb.get("mm") and lb.get("x") is None:
            lb["x"], lb["y"] = round(ax * lb["mm"][0] + bx, 1), round(ay * lb["mm"][1] + by, 1)
        if lb.get("x") is not None:
            labs.append(lb)
    if use_ocr:
        o, why = ocr_labels(im, (px0 - pad, py0 - pad, px1 + pad, py1 + pad), allow_install=allow_install)
        if why:
            info["warnings"].append(why)
        labs = merge_labels(labs, o)
    info["labels"] = labs

    def cell_of(x, y):
        i = next((k for k in range(len(xs) - 1) if min(xs[k]["px"], xs[k + 1]["px"]) <= x < max(xs[k]["px"], xs[k + 1]["px"])), None)
        j = next((k for k in range(len(ys) - 1) if min(ys[k]["px"], ys[k + 1]["px"]) <= y < max(ys[k]["px"], ys[k + 1]["px"])), None)
        return None if i is None or j is None else (j, i)
    reg_of = {}
    for n, cells in enumerate(g["regions"]):
        for c in cells:
            reg_of[c] = n
    by_reg = {}
    for lab in labs:
        c = cell_of(lab["x"], lab["y"])
        if c is None or c not in reg_of:
            info["warnings"].append(f"label {lab['name']} at ({lab['x']},{lab['y']}) px is outside every detected room")
            spec["uncertain"].append(f"标签「{lab['name']}」(像素 {lab['x']:.0f},{lab['y']:.0f}) 不在识别出的任何房间里，请核对该处墙线")
            spec.setdefault("free_labels", []).append({"text": lab["name"], "at_px": [lab["x"], lab["y"]]})  # 3.9.6: never drop a name
            continue
        by_reg.setdefault(reg_of[c], []).append((lab, c))
    rooms = []
    used = {}
    cell_room = {}

    def rid(name):
        base = {"客厅": "living", "餐厅": "dining", "厨房": "kitchen", "主卧": "master", "次卧": "bed", "卧室": "bed",
                "书房": "study", "卫生间": "bath", "主卫": "mbath", "客卫": "bath", "阳台": "balcony", "生活阳台": "balcony",
                "玄关": "entry", "过道": "hall", "衣帽间": "closet", "管井": "shaft"}.get(name, "room")
        used[base] = used.get(base, 0) + 1
        return base if used[base] == 1 else f"{base}{used[base]}"

    def ccenter(c):
        j, i = c
        return ((xs[i]["px"] + xs[i + 1]["px"]) / 2, (ys[j]["px"] + ys[j + 1]["px"]) / 2)
    for n, cells in enumerate(g["regions"]):
        lab = by_reg.get(n) or []
        if not lab:
            area = sum((xs[i + 1]["mm"] - xs[i]["mm"]) * (ys[j + 1]["mm"] - ys[j]["mm"]) for j, i in cells)
            if area < 0.6e6:          # a sliver between double lines: not a room
                continue
            poly = cells_poly(cells, xs, ys)
            for c in cells:
                cell_room[c] = len(rooms)
            rooms.append({"id": rid("room"), "name": "房间?", "poly": poly, "confidence": "low", "auto": True})
            spec["uncertain"].append(f"房间 {poly_box(poly)} 没有读到文字标签：按原图补上名字")
            continue
        groups = None
        if mode == "thick":
            groups = split_by_walls(cells, [c0 for _lb, c0 in lab], g, xs, ys)
            if any(not groups.get(k) for k in range(len(lab))):
                groups = None   # 3.9.6 (task-ab7): open plan (客厅+厨房+玄关, no wall between): split by nearest label
        if groups is None:      # faint ridge walls (a photo): coverage is too noisy to route by; nearest label (3.8.8)
            groups = {k: [] for k in range(len(lab))}
            for c in cells:
                px, py = ccenter(c)
                k = min(range(len(lab)), key=lambda t: (lab[t][0]["x"] - px) ** 2 + (lab[t][0]["y"] - py) ** 2)
                groups[k].append(c)
            for k, (lb, c0) in enumerate(lab):
                if c0 not in groups[k]:                # the label's own cell always stays with it
                    for kk in groups:
                        if c0 in groups[kk]:
                            groups[kk].remove(c0)
                    groups[k].append(c0)
        zone = None      # 3.8.9: walls between rooms of one region are decided per shared edge (open_edges)
        for k, (lb, _c0) in enumerate(lab):
            if not groups.get(k):    # 3.9.6: two names in one cell - keep the name as text, never drop it
                spec.setdefault("free_labels", []).append({"text": lb["name"], "at_px": [lb["x"], lb["y"]]})
                continue
            for c in groups[k]:
                cell_room[c] = len(rooms)
            poly = cells_poly(groups[k], xs, ys)
            r = {"id": rid(lb["name"]), "name": lb["name"], "poly": poly, "confidence": "medium" if lb.get("by") == "ocr" else "low",
                 "auto": True, "label_px": [lb["x"], lb["y"]]}
            t = room_type(lb["name"])
            if t != "room":
                r["type"] = t
            if zone:
                r["zone"] = zone
            if len(poly) == 4:
                r["box"] = poly_box(poly); r.pop("poly")
            rooms.append(r)
    spec["rooms"] = rooms
    op = open_edges(cell_room, rooms, g, xs, ys, wv, wh, barrier, band, s)
    spec["no_wall"], spec["doors"], spec["windows"] = op["no_wall"], op["doors"], op["windows"]
    info["openings"] = {k: len(v) for k, v in op.items()}
    spec["notes"].append("自动草稿（drawkit auto）：墙线来自像素、比例来自尺寸链刻度；门窗按墙上的缺口自动放置，需核对。")
    info.update({"x_lines": [round(e["mm"]) for e in xs], "y_lines": [round(e["mm"]) for e in ys],
                 "x_unsnapped": [round(e["mm"]) for e in xs if not e["mark"]],
                 "y_unsnapped": [round(e["mm"]) for e in ys if not e["mark"]],
                 "regions": len(g["regions"]), "rooms": len(rooms),
                 "plan_px": [round(px0 - pad), round(py0 - pad), round(px1 + pad), round(py1 + pad)]})
    if debug:
        draw_debug(im, xs, ys, g, rooms, labs, T, debug)
        info["debug"] = str(debug)
    return spec, info


def _gaps(mask, horiz, pos, s0, s1, band):
    """uncovered stretches (px, along the line) of a mask band around a grid edge: [(a, b)]."""
    import numpy as np
    H, W = mask.shape
    p0, p1 = int(round(pos - band)), int(round(pos + band)) + 1
    a, b = int(round(min(s0, s1))), int(round(max(s0, s1)))
    if horiz:
        p0, p1, a, b = max(0, p0), min(H, p1), max(0, a), min(W, b)
        col = mask[p0:p1, a:b].any(0) if p1 > p0 and b > a else np.zeros(0, bool)
    else:
        p0, p1, a, b = max(0, p0), min(W, p1), max(0, a), min(H, b)
        col = mask[a:b, p0:p1].any(1) if p1 > p0 and b > a else np.zeros(0, bool)
    out, st = [], None
    for k, v in enumerate(col.tolist() + [True]):
        if not v and st is None:
            st = k
        elif v and st is not None:
            out.append((a + st, a + k)); st = None
    return out


def open_edges(cell_room, rooms, g, xs, ys, wv, wh, barrier, band, s):
    """walls, doors and windows from the wall pixels along the room boundaries (mm):
    * two rooms whose shared boundary shows (almost) no wall: open plan -> no_wall
    * a shared wall with a gap 600-1500 mm wide: a door at the gap
    * an outer edge with a gap: a window where lines are drawn across it (glazing), else a door."""
    ny, nx = len(ys) - 1, len(xs) - 1
    runs = {}
    for j in range(ny):
        for i in range(nx + 1):
            a_, b_ = cell_room.get((j, i - 1)) if i > 0 else None, cell_room.get((j, i)) if i < nx else None
            if a_ != b_ and (a_ is not None or b_ is not None):
                runs.setdefault(("v", i, a_, b_) if (a_ or -1) < (b_ or -1) else ("v", i, b_, a_), []).append(j)
    for j in range(ny + 1):
        for i in range(nx):
            a_, b_ = cell_room.get((j - 1, i)) if j > 0 else None, cell_room.get((j, i)) if j < ny else None
            if a_ != b_ and (a_ is not None or b_ is not None):
                runs.setdefault(("h", j, a_, b_) if (a_ or -1) < (b_ or -1) else ("h", j, b_, a_), []).append(i)
    no_wall, doors, windows = [], [], []
    mm_x = lambda px: round(px)  # noqa: E731
    for (o, k, ra, rb), idx in runs.items():
        idx = sorted(set(idx))
        # contiguous pieces
        pieces, cur = [], [idx[0]]
        for t in idx[1:]:
            if t == cur[-1] + 1:
                cur.append(t)
            else:
                pieces.append(cur); cur = [t]
        pieces.append(cur)
        inner = ra is not None and rb is not None
        for pc in pieces:
            if o == "v":
                c_px, c_mm = xs[k]["px"], xs[k]["mm"]
                lo, hi = ys[pc[0]], ys[pc[-1] + 1]
                cov = sum(g["vcov"][j, k] * (ys[j + 1]["mm"] - ys[j]["mm"]) for j in pc) / max(1.0, hi["mm"] - lo["mm"])
            else:
                c_px, c_mm = ys[k]["px"], ys[k]["mm"]
                lo, hi = xs[pc[0]], xs[pc[-1] + 1]
                cov = sum(g["hcov"][k, i] * (xs[i + 1]["mm"] - xs[i]["mm"]) for i in pc) / max(1.0, hi["mm"] - lo["mm"])
            length = hi["mm"] - lo["mm"]
            P = (lambda u: [c_mm, round(u)]) if o == "v" else (lambda u: [round(u), c_mm])   # noqa: E731
            if inner and cov < 0.25:
                no_wall.append({"a": P(lo["mm"]), "b": P(hi["mm"])})
                continue
            if cov > 0.97 or length < 500:
                continue
            m = wv if o == "v" else wh
            span_px = hi["px"] - lo["px"]
            to_mm = lambda px: lo["mm"] + (px - lo["px"]) * length / span_px   # noqa: E731
            for ga, gb in _gaps(m, o == "h", c_px, lo["px"], hi["px"], max(1.5, band * 0.5)):
                wmm = (gb - ga) * length / span_px
                if wmm < 550 or ga - lo["px"] < 2 or hi["px"] - gb < 2:
                    continue
                u0, u1 = to_mm(ga), to_mm(gb)
                mid = (u0 + u1) / 2
                if inner:
                    if wmm <= 1600:
                        a_n, b_n = rooms[ra]["name"], rooms[rb]["name"]
                        into = rooms[rb]["id"] if a_n in PUBLIC or b_n not in PUBLIC and ra < rb else rooms[ra]["id"]
                        doors.append({"at": P(mid), "width": int(round(min(wmm, 1000) / 50) * 50), "into": into, "auto": True})
                else:
                    glazed = barrier is not None and _gaps(barrier, o == "h", c_px, ga, gb, max(1.5, band * 0.5)) == []
                    if glazed or wmm > 1600:
                        windows.append({"a": P(u0 + 50), "b": P(u1 - 50), "auto": True})
                    else:
                        doors.append({"at": P(mid), "width": int(round(min(wmm, 1000) / 50) * 50),
                                      "into": rooms[ra if ra is not None else rb]["id"], "auto": True})
    return {"no_wall": no_wall, "doors": doors, "windows": windows}


def draw_debug(im, xs, ys, g, rooms, labs, T, out, k=3):
    from PIL import Image, ImageDraw, ImageFont
    big = im.resize((im.width * k, im.height * k), Image.LANCZOS).convert("RGBA")
    lay = Image.new("RGBA", big.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    font = None
    for f in ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"):
        if os.path.exists(f):
            font = ImageFont.truetype(f, 14 * k // 3 + 6); break
    font = font or ImageFont.load_default()
    pal = [(230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180), (70, 240, 240), (240, 50, 230),
           (210, 245, 60), (250, 190, 190), (0, 128, 128), (170, 110, 40), (128, 0, 0), (0, 0, 128), (128, 128, 0)]
    P = lambda mmx, mmy: ((T["ax"] * mmx + T["bx"]) * k, (T["ay"] * mmy + T["by"]) * k)  # noqa: E731
    for n, r in enumerate(rooms):
        pts = r.get("poly") or (lambda b: [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]])(r["box"])
        c = pal[n % len(pal)]
        d.polygon([P(*p) for p in pts], fill=c + (60,), outline=c + (255,))
    ny, nx = g["vwall"].shape[0], g["hwall"].shape[1]
    for j in range(ny):
        for i in range(nx + 1):
            if g["vwall"][j, i]:
                d.line([(xs[i]["px"] * k, ys[j]["px"] * k), (xs[i]["px"] * k, ys[j + 1]["px"] * k)], fill=(255, 0, 0, 255), width=3)
    for j in range(ny + 1):
        for i in range(nx):
            if g["hwall"][j, i]:
                d.line([(xs[i]["px"] * k, ys[j]["px"] * k), (xs[i + 1]["px"] * k, ys[j]["px"] * k)], fill=(255, 0, 0, 255), width=3)
    for e in xs:
        d.line([(e["px"] * k, ys[0]["px"] * k - 12), (e["px"] * k, ys[0]["px"] * k - 2)], fill=(0, 0, 255, 255), width=2)
        d.text((e["px"] * k + 2, ys[0]["px"] * k - 28), str(round(e["mm"])), fill=(0, 0, 255, 255), font=font)
    for e in ys:
        d.line([(xs[0]["px"] * k - 12, e["px"] * k), (xs[0]["px"] * k - 2, e["px"] * k)], fill=(0, 0, 255, 255), width=2)
    for r in rooms:
        if r.get("label_px"):
            x, y = r["label_px"]
            d.ellipse([x * k - 4, y * k - 4, x * k + 4, y * k + 4], fill=(0, 0, 0, 255))
            d.text((x * k + 6, y * k - 8), r["name"], fill=(0, 0, 0, 255), font=font)
        elif r["name"] == "房间?":
            pts = r.get("poly") or []
            if pts:
                cx = sum(p[0] for p in pts) / len(pts); cy = sum(p[1] for p in pts) / len(pts)
                d.text(P(cx, cy), "?", fill=(200, 0, 0, 255), font=font)
    Image.alpha_composite(big, lay).convert("RGB").save(out)
    return out


def compare(auto_rooms, ref_rooms):
    """accuracy vs a reference spec: per reference room the best-overlapping auto room (IoU of their polygons via a
    raster) and whether the names agree."""
    import numpy as np

    def pts(r):
        if r.get("poly"):
            return r["poly"]
        b = r["box"]
        return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]
    allp = [p for r in list(auto_rooms) + list(ref_rooms) for p in pts(r)]
    X = max(p[0] for p in allp) + 1; Y = max(p[1] for p in allp) + 1
    step = 50.0
    gx = np.arange(0, X, step) + step / 2; gy = np.arange(0, Y, step) + step / 2
    GX, GY = np.meshgrid(gx, gy)

    def mask(poly):
        m = np.zeros(GX.shape, bool)
        n = len(poly)
        for k in range(n):
            x0, y0 = poly[k - 1]; x1, y1 = poly[k]
            if y0 == y1:
                continue
            cond = ((GY >= min(y0, y1)) & (GY < max(y0, y1)))
            xc = x0 + (GY - y0) * (x1 - x0) / (y1 - y0)
            m ^= cond & (GX < xc)
        return m
    am = [mask(pts(r)) for r in auto_rooms]
    rows = []
    for r in ref_rooms:
        rm = mask(pts(r))
        best, bi = 0.0, None
        for k, m in enumerate(am):
            u = (rm | m).sum()
            iou = float((rm & m).sum() / u) if u else 0.0
            if iou > best:
                best, bi = iou, k
        rows.append({"ref": r.get("name"), "auto": auto_rooms[bi]["name"] if bi is not None else None,
                     "iou": round(best, 2), "name_ok": bi is not None and auto_rooms[bi]["name"] == r.get("name")})
    return rows


# ------------------------------------------------------------------------------------------------ chains (3.8.9)
# Why (task-3fc): the TEXT model guessed every chain's box_px from a prose description (left [120,300,220,1060]);
# the left box held the wrong line (every segment measured 1.36x), the right box missed its line (~0 mm), the fit
# was 40% off and the build could never pass. The chains are found here instead: long thin lines outside the
# outer-wall bbox, their ticks, their digits (OCR with a timeout, or one VLM read per chain crop done by the server).
# The model may pass only the VALUES; the boxes are always located here.

OCR_TIMEOUT = float(os.environ.get("DRAWKIT_OCR_TIMEOUT", "20"))


def _lum_sat(a):
    import numpy as np
    lum = a @ np.array([0.299, 0.587, 0.114])
    mx, mn = a.max(2), a.min(2)
    return lum, (mx - mn) / np.maximum(mx, 1e-3)


def _erode(mask, r):
    """binary erosion with a (2r+1)^2 square, numpy only."""
    import numpy as np
    m = mask.copy()
    for ax in (0, 1):
        acc = m.copy()
        for k in range(1, r + 1):
            acc &= np.roll(m, k, ax) & np.roll(m, -k, ax)
        m = acc
    return m


def _dilate(mask, r):
    import numpy as np
    m = mask.copy()
    for ax in (0, 1):
        acc = m.copy()
        for k in range(1, r + 1):
            acc |= np.roll(m, k, ax) | np.roll(m, -k, ax)
        m = acc
    return m


def _components(mask, step=2):
    """connected components of a (downsampled) bool mask -> list of (count, x0, y0, x1, y1) in full pixels."""
    import numpy as np
    m = mask[::step, ::step]
    H, W = m.shape
    seen = np.zeros_like(m)
    out = []
    ys, xs = np.nonzero(m)
    for y, x in zip(ys.tolist(), xs.tolist()):
        if seen[y, x]:
            continue
        stack = [(y, x)]; seen[y, x] = True
        n, x0, y0, x1, y1 = 0, x, y, x, y
        while stack:
            cy, cx = stack.pop()
            n += 1
            x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    q, r = cy + dy, cx + dx
                    if 0 <= q < H and 0 <= r < W and m[q, r] and not seen[q, r]:
                        seen[q, r] = True; stack.append((q, r))
        out.append((n, x0 * step, y0 * step, x1 * step + step - 1, y1 * step + step - 1))
    return sorted(out, reverse=True)


def wall_mask(a):
    """thick, dark, neutral strokes: outer and inner walls (black or dark grey on any coloured fill). Thin lines
    (dimension lines, furniture outlines, text) do not survive the opening."""
    lum, sat = _lum_sat(a)
    H, W = lum.shape
    dark = (lum < 0.5) & (sat < 0.3)
    r = max(2, int(round(min(H, W) / 400)))       # walls are >= 2r+1 px thick (1004 px wide -> 5 px)
    return _dilate(_erode(dark, r), r)


def wall_bbox(a):
    """bbox of the flat's walls: the largest thick-dark component, grown by every sizeable wall piece near it
    (windows and doors cut the walls into pieces; a legend or title further away is left out)."""
    m = wall_mask(a)
    comps = _components(m)
    if not comps:
        return None
    n0 = comps[0][0]
    pieces = [c for c in comps if c[0] >= max(12, 0.04 * n0)]
    x0, y0, x1, y1 = comps[0][1:]
    grown = True
    while grown:
        grown = False
        reach = max(0.35 * max(x1 - x0, y1 - y0), 0.12 * max(m.shape))
        for c in pieces:
            cx0, cy0, cx1, cy1 = c[1:]
            if cx0 >= x0 and cy0 >= y0 and cx1 <= x1 and cy1 <= y1:
                continue
            gap = max(cx0 - x1, x0 - cx1, cy0 - y1, y0 - cy1, 0)
            if gap <= reach:
                x0, y0, x1, y1 = min(x0, cx0), min(y0, cy0), max(x1, cx1), max(y1, cy1)
                grown = True
    return [int(x0), int(y0), int(x1), int(y1)]


def _runs(row, gap=2):
    """(start, end) of True runs in a 1-D bool array, bridging gaps <= gap."""
    out = []
    s = None; last = None
    for i, v in enumerate(row.tolist()):
        if v:
            if s is None:
                s = i
            elif i - last > gap + 1:
                out.append((s, last)); s = i
            last = i
    if s is not None:
        out.append((s, last))
    return out


def _smooth(lum, axis, k=7):
    """running mean along one axis (a faint, noisy 1 px line in a JPEG photo becomes a continuous run)."""
    import numpy as np
    c = np.cumsum(np.pad(lum, [(k // 2, k - k // 2)] if lum.ndim == 1 else
                         ([(0, 0), (k // 2, k - k // 2)] if axis == 1 else [(k // 2, k - k // 2), (0, 0)]), mode="edge"),
                  axis=axis)
    if axis == 1:
        return (c[:, k:] - c[:, :-k]) / k
    return (c[k:, :] - c[:-k, :]) / k


def ticked_lines(lum, horiz, thr=0.72, minfrac=0.15, k=7):
    """every long thin line (horizontal or vertical) in the image with its ticks:
    [{"pos","a0","a1","len","ticks","inner"}]. A dimension line has >= 2 inner ticks."""
    import numpy as np
    H, W = lum.shape
    sm = _smooth(lum, 1 if horiz else 0, k)
    n, L = (H, W) if horiz else (W, H)
    cands = []
    for p in range(3, n - 3):
        line = sm[p, :] if horiz else sm[:, p]
        for r0, r1 in _runs(line < thr):
            ln = r1 - r0
            if ln < minfrac * L:
                continue
            o1 = sm[p - 3, r0:r1] if horiz else sm[r0:r1, p - 3]
            o2 = sm[p + 3, r0:r1] if horiz else sm[r0:r1, p + 3]
            if float(np.mean(o1 < thr)) < 0.5 or float(np.mean(o2 < thr)) < 0.5:
                cands.append({"pos": p, "a0": r0, "a1": r1, "len": ln})
    groups = []
    for c in sorted(cands, key=lambda c: (c["pos"], c["a0"])):
        for g in groups:
            if c["pos"] - g[-1]["pos"] <= 2 and min(c["a1"], g[-1]["a1"]) - max(c["a0"], g[-1]["a0"]) > 0.5 * c["len"]:
                g.append(c); break
        else:
            groups.append([c])
    out = []
    for g in groups:
        best = max(g, key=lambda c: c["len"])
        Ln = dict(best, pos=float(np.mean([c["pos"] for c in g])))
        tk = line_ticks(lum, Ln, "top" if horiz else "left", thr=thr)
        Ln["ticks"] = tk
        m_ = max(3.0, 0.05 * Ln["len"])          # ticks well inside the line (not its end ticks)
        Ln["inner"] = len([t for t in tk if Ln["a0"] + m_ < t < Ln["a1"] - m_])
        out.append(Ln)
    return out


def plausible_bbox(b, shape):
    H, W = shape[:2]
    if not b:
        return False
    w, h = b[2] - b[0], b[3] - b[1]
    return w >= 0.2 * min(H, W) and h >= 0.2 * min(H, W) and b[0] > 3 and b[1] > 3 and b[2] < W - 4 and b[3] < H - 4


def bbox_from_lines(hl, vl):
    """no usable wall mask (a faint photo): the plan sits between the outermost pair of ticked lines per axis."""
    def pair(ls):
        best = None
        ls = [L for L in ls if L["inner"] >= 1]
        for i, a in enumerate(ls):
            for b in ls[i + 1:]:
                ov = min(a["a1"], b["a1"]) - max(a["a0"], b["a0"])
                if ov < 0.7 * max(a["len"], b["len"]):
                    continue
                d = abs(a["pos"] - b["pos"])
                if best is None or d > best[0]:
                    best = (d, a, b)
        return best
    ph, pv = pair(hl), pair(vl)
    if not ph or not pv:
        return None
    _, a, b = ph; _, c, d = pv
    x0, x1 = (a["a0"] + b["a0"]) / 2, (a["a1"] + b["a1"]) / 2
    y0, y1 = (c["a0"] + d["a0"]) / 2, (c["a1"] + d["a1"]) / 2
    # the dimension lines themselves are outside the plan: shrink to between them
    top, bot = sorted((a["pos"], b["pos"])); lef, rig = sorted((c["pos"], d["pos"]))
    return [int(max(x0, lef + 3)), int(max(y0, top + 3)), int(min(x1, rig - 3)), int(min(y1, bot - 3))]


def side_lines(lines, bbox, side):
    """the ticked lines outside one side of the plan, nearest first."""
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    horiz = side in ("top", "bottom")
    reach = 0.45 * (bh if horiz else bw) + 20
    lo, hi = (x0, x1) if horiz else (y0, y1)
    out = []
    for L in lines:
        ov = min(L["a1"], hi) - max(L["a0"], lo)
        if ov < 0.4 * (hi - lo) or L["len"] > 1.35 * (hi - lo):
            continue
        p = L["pos"]
        edge = {"top": y0, "bottom": y1, "left": x0, "right": x1}[side]
        d = (edge - p) if side in ("top", "left") else (p - edge)
        if -2 <= d <= reach:
            out.append(dict(L, dist=d))
    return sorted(out, key=lambda L: L["dist"])


def line_ticks(lum, line, side, reach=(3, 8), thr=0.72):
    """tick positions on a dimension line: extension lines crossing it (dark on both sides), or a 45 degree slash
    / dot right on it. Sub-pixel centroids, along the line."""
    import numpy as np
    H, W = lum.shape
    horiz = side in ("top", "bottom")
    p = int(round(line["pos"]))
    a0, a1 = int(line["a0"]) - 3, int(line["a1"]) + 4
    a0, a1 = max(0, a0), min(W if horiz else H, a1)
    r0, r1 = reach
    prof = []
    for t in range(a0, a1):
        best = 0.0
        for o in (-1, 0, 1):
            tt = t + o
            if not (0 <= tt < (W if horiz else H)):
                continue
            if horiz:
                up = lum[max(0, p - r1):max(0, p - r0 + 1), tt]; dn = lum[min(H, p + r0):min(H, p + r1 + 1), tt]
            else:
                up = lum[tt, max(0, p - r1):max(0, p - r0 + 1)]; dn = lum[tt, min(W, p + r0):min(W, p + r1 + 1)]
            s = min(float(np.mean(up < thr)) if up.size else 0.0, float(np.mean(dn < thr)) if dn.size else 0.0)
            best = max(best, s)
        prof.append(best)
    prof = np.asarray(prof)
    ticks = []
    i = 0
    while i < len(prof):
        if prof[i] >= 0.6:
            j = i
            while j + 1 < len(prof) and prof[j + 1] >= 0.6:
                j += 1
            if j - i <= 4:                     # a line 1-4 px wide, not a block of text or hatch
                w = prof[i:j + 1]
                ticks.append(round(a0 + i + float((np.arange(len(w)) * w).sum() / w.sum()), 1))
            i = j + 1
        else:
            i += 1
    # dots / short slashes right on the line (no extension line through it): a dark blob 5 px across
    dots = []
    for t in range(a0 + 2, a1 - 2):
        if horiz:
            w = lum[max(0, p - 2):p + 3, t - 1:t + 2]
        else:
            w = lum[t - 1:t + 2, max(0, p - 2):p + 3]
        dots.append(float(1 - w.mean()) if w.size else 0.0)
    if dots:
        dots = np.asarray(dots)
        base = float(np.median(dots))
        thr_d = base + max(0.12, 3 * float(np.median(np.abs(dots - base))))
        i = 0
        while i < len(dots):
            if dots[i] > thr_d:
                j = i
                while j + 1 < len(dots) and dots[j + 1] > thr_d:
                    j += 1
                if j - i <= 6:
                    c = a0 + 2 + (i + j) / 2.0
                    if not any(abs(c - q) <= 3 for q in ticks):
                        ticks.append(round(c, 1))
                i = j + 1
            else:
                i += 1
        ticks.sort()
    # a line end counts as a tick only when no tick is near it (dimension lines overshoot their end ticks a little)
    near = max(12.0, 0.03 * (line["a1"] - line["a0"]))
    for e in (float(line["a0"]), float(line["a1"])):
        if not any(abs(e - t) <= near for t in ticks):
            ticks.append(e)
    return sorted(ticks)


def _tesseract_digits(crop, timeout=None):
    """digits in a small crop with tesseract (eng, digits only), or None. Never more than `timeout` s."""
    import shutil
    import subprocess
    import tempfile
    from PIL import Image
    if not shutil.which("tesseract"):
        return None
    timeout = OCR_TIMEOUT if timeout is None else timeout
    k = max(1, int(round(48 / max(1, crop.height))))
    c = crop.convert("L").resize((crop.width * k, crop.height * k), Image.LANCZOS)
    c = c.point(lambda v: 0 if v < 150 else 255)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        c.save(f.name)
        try:
            r = subprocess.run(["tesseract", f.name, "-", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789"],
                               capture_output=True, text=True, timeout=timeout)
        except Exception:  # noqa: BLE001   (timeout, crash: fall back)
            return None
        finally:
            try:
                os.unlink(f.name)
            except OSError:
                pass
    m = re.findall(r"\d{3,5}", r.stdout or "")
    return int(m[0]) if m else None


def segment_crops(im, line, ticks, side, band=26):
    """for each segment between ticks: (crop of the text beside the line on each side), upright for OCR."""
    horiz = side in ("top", "bottom")
    p = line["pos"]
    out = []
    for t0, t1 in zip(ticks, ticks[1:]):
        pair = []
        for sgn in (-1, 1):
            if horiz:
                b = (t0 + 3, p - band if sgn < 0 else p + 3, t1 - 3, p - 3 if sgn < 0 else p + band)
            else:
                b = (p - band if sgn < 0 else p + 3, t0 + 3, p - 3 if sgn < 0 else p + band, t1 - 3)
            b = tuple(int(round(v)) for v in b)
            if b[2] - b[0] < 4 or b[3] - b[1] < 4:
                pair.append(None); continue
            c = im.crop(b)
            if not horiz:
                c = c.rotate(-90, expand=True)     # vertical chains print their numbers reading bottom -> top
            pair.append(c)
        out.append(pair)
    return out


def _proportional(values, ticks, tol=0.08):
    """values fit the tick spacing: every segment's px/mm within tol of the median. -> (ok, px_per_mm, worst)."""
    seg = [b - a for a, b in zip(ticks, ticks[1:])]
    if len(seg) != len(values) or not values or min(values) <= 0:
        return False, None, None
    r = sorted(s / v for s, v in zip(seg, values))
    med = r[len(r) // 2] if len(r) % 2 else (r[len(r) // 2 - 1] + r[len(r) // 2]) / 2
    worst = max(abs(s / v / med - 1) for s, v in zip(seg, values))
    return worst <= tol, med, worst


def _best_subset(values, ticks):
    """given values but more tick candidates than values+1: the tick subset that fits (drawkit.match_chain-like)."""
    if len(ticks) < len(values) + 1:
        return None
    best = None
    T = float(sum(values))
    for i in range(len(ticks)):
        for j in range(len(ticks) - 1, i, -1):
            s = (ticks[j] - ticks[i]) / T
            if s <= 0:
                continue
            acc, got, cost = 0.0, [ticks[i]], 0.0
            for v in values:
                acc += v
                q = min(ticks, key=lambda t: abs(t - (ticks[i] + s * acc)))
                cost += abs(q - (ticks[i] + s * acc)); got.append(q)
            if len(set(got)) != len(got):
                continue
            cost -= 0.01 * (ticks[j] - ticks[i])
            if best is None or cost < best[0]:
                best = (cost, got)
    return best[1] if best else None


def detect_chains(image, given=None, ocr=True, ocr_timeout=None, a=None, im=None):
    """-> {"bbox", "chains": [spec chains with ticks_px, values, by, line_px, confidence], "total_lines",
    "transform", "checks", "warnings", "need_values": [sides]}.
    given: {side: [values]} from the model / facts (only VALUES; the boxes are always found here)."""
    import numpy as np
    if a is None:
        im, a = load(image)
    lum, _sat = _lum_sat(a)
    given = {k: [float(v) for v in vs] for k, vs in (given or {}).items() if vs}
    bbox = wall_bbox(a)
    paper = float(np.median(lum[paper_mask(a)])) if paper_mask(a).any() else 1.0
    lines = {}
    for thr in (0.72, min(0.95, paper - 0.06)):
        hl, vl = ticked_lines(lum, True, thr), ticked_lines(lum, False, thr)
        bb = bbox if plausible_bbox(bbox, lum.shape) else bbox_from_lines(hl, vl)
        if not bb:
            continue
        got = {s_: side_lines(hl if s_ in ("top", "bottom") else vl, bb, s_) for s_ in ("top", "bottom", "left", "right")}
        score = lambda d: sum(1 for v in d.values() if any(L["inner"] >= 1 for L in v))   # noqa: E731
        if score(got) > score(lines):
            lines, bbox, rep_thr = got, bb, thr
        if score(lines) == 4:
            break
    rep = {"bbox": bbox, "chains": [], "totals": {}, "checks": [], "warnings": [], "need_values": [], "lines": {}}
    if not lines:
        rep["warnings"].append("no dimension chains found (no ticked lines around the plan)")
        return rep
    rep["threshold"] = round(rep_thr, 3)
    t_left = time.time() + (ocr_timeout if ocr_timeout is not None else OCR_TIMEOUT) * 3
    for side in ("top", "bottom", "left", "right"):
        info = [(L, L["ticks"]) for L in lines.get(side) or []][:4]
        rep["lines"][side] = [{"pos": round(L["pos"], 1), "span": [L["a0"], L["a1"]], "ticks": tk} for L, tk in info]
        if not info:
            rep["warnings"].append(f"{side}: no dimension line found outside the walls")
            continue
        # the segment chain = the line with the most ticks (>= 3); a 2-tick line further out is the overall total
        # the segment chain = the nearest line with ticks inside it (the overall total sits further out); when the
        # model gave values, the line whose ticks fit them wins
        ticked = [t for t in info if t[0]["inner"] >= 1] or info
        seg = ticked[0]
        g_ = given.get(side)
        if g_:
            fits = [t for t in ticked if _proportional(g_, t[1], tol=0.3)[0] or
                    (len(t[1]) > len(g_) + 1 and _best_subset(g_, t[1]) and _proportional(g_, _best_subset(g_, t[1]), tol=0.3)[0])]
            if fits:
                seg = fits[0]
        L, tk = seg
        tot = [t for t in info if t is not seg and len(t[1]) == 2]
        if tot:
            T = tot[0]
            rep["totals"][side] = {"line_px": round(T[0]["pos"], 1), "ticks_px": T[1]}
        values, by, conf = None, None, "low"
        g = given.get(side)
        if g:
            ok, _s, worst = _proportional(g, tk)
            loose = _proportional(g, tk, tol=0.3)[0]       # a photo is rarely exactly to scale
            if ok or loose:
                values, by, conf = g, "given", "high" if ok else "medium"
            else:
                sub = _best_subset(g, tk) if len(tk) > len(g) + 1 else None
                if sub and _proportional(g, sub, tol=0.3)[0]:
                    tk, values, by, conf = sub, g, "given", "medium"
                else:
                    rep["warnings"].append(f"{side}: the given values {g} do not fit the {len(tk) - 1} measured segments "
                                           f"(worst {round((worst or 9) * 100)}% off)")
        if values is None and ocr and len(tk) >= 2 and time.time() < t_left:
            got = []
            for pair in segment_crops(im, L, tk, side):
                v = None
                for c in pair:
                    if c is not None and time.time() < t_left:
                        v = _tesseract_digits(c, ocr_timeout)
                        if v and 100 <= v <= 30000:
                            break
                        v = None
                got.append(v)
            while len(got) > 1 and got[0] is None and all(got[1:]):      # a stray end tick: drop it
                got, tk = got[1:], tk[1:]
            while len(got) > 1 and got[-1] is None and all(got[:-1]):
                got, tk = got[:-1], tk[:-1]
            if got and all(got):
                ok, _s, worst = _proportional(got, tk)
                if ok:
                    values, by, conf = [float(v) for v in got], "ocr", "medium"
                else:
                    rep["warnings"].append(f"{side}: OCR read {got}, which does not fit the tick spacing")
            rep.setdefault("ocr", {})[side] = got
        c = {"side": side, "ticks_px": tk, "line_px": round(L["pos"], 1), "auto": True}
        if values:
            c.update(values=[int(v) if float(v).is_integer() else v for v in values], by=by, confidence=conf)
        else:
            rep["need_values"].append(side)
            c["segments"] = len(tk) - 1
        rep["chains"].append(c)
    _chain_consistency(rep)
    return rep


def _chain_consistency(rep):
    """opposite chains add up alike; the wall bbox aspect matches the mm totals; per-axis scale from the ticks."""
    by = {c["side"]: c for c in rep["chains"] if c.get("values")}
    S = {}
    for side, c in by.items():
        tk = c["ticks_px"]
        S[side] = (tk[-1] - tk[0]) / float(sum(c["values"]))
    for a_, b_ in (("top", "bottom"), ("left", "right")):
        if a_ in by and b_ in by:
            ta, tb = sum(by[a_]["values"]), sum(by[b_]["values"])
            if abs(ta - tb) > 1:
                rep["checks"].append(f"{a_} total {ta:.0f} ≠ {b_} total {tb:.0f}")
            else:
                rep["checks"].append(f"{a_}/{b_} totals agree: {ta:.0f}")
    sx = [S[k] for k in ("top", "bottom") if k in S]; sy = [S[k] for k in ("left", "right") if k in S]
    if sx and sy:
        ax, ay = sum(sx) / len(sx), sum(sy) / len(sy)
        rep["px_per_mm"] = {"x": round(ax, 6), "y": round(ay, 6)}
        b = rep.get("bbox")
        if b:
            wx = max(c["ticks_px"][-1] for c in by.values() if c["side"] in ("top", "bottom")) - \
                min(c["ticks_px"][0] for c in by.values() if c["side"] in ("top", "bottom"))
            rep["checks"].append(f"x/y px per mm {ax:.5f}/{ay:.5f} ({abs(ax / ay - 1) * 100:.1f}% apart); wall bbox "
                                 f"{b[2] - b[0]}x{b[3] - b[1]} px, top/bottom ticks span {wx:.0f} px")
            if abs(ax / ay - 1) > 0.06:
                rep["warnings"].append(f"x and y scales differ by {abs(ax / ay - 1) * 100:.1f}%: a chain's values may be "
                                       "misread")


def chains_for_spec(rep):
    """spec chain list (values + ticks_px, no box) from detect_chains; chains that start later than their opposite
    one get a `start`."""
    out = []
    by = {c["side"]: c for c in rep.get("chains") or [] if c.get("values")}
    for axis in (("top", "bottom"), ("left", "right")):
        cs = [by[s] for s in axis if s in by]
        if not cs:
            continue
        s = sum((c["ticks_px"][-1] - c["ticks_px"][0]) / float(sum(c["values"])) for c in cs) / len(cs)
        first = min(c["ticks_px"][0] for c in cs)
        for c in cs:
            d = {"side": c["side"], "values": c["values"], "ticks_px": c["ticks_px"], "auto": True,
                 "by": c.get("by"), "confidence": c.get("confidence", "medium"), "given": c.get("by") == "given"}
            st = (c["ticks_px"][0] - first) / s
            if st > 150:
                d["start"] = int(round(st / 50.0) * 50)
            out.append(d)
    return out


if __name__ == "__main__":     # convenience: python3 drawauto.py IMG CHAINS.json [LABELS.json]
    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    dk = import_module("drawkit")
    sys.exit(dk.main(["auto"] + sys.argv[1:]))
