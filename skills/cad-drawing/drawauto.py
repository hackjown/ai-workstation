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
    return np.abs(a - ref).max(2) < 0.085


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


def build_rooms(wv, wh, paper, xs, ys, band, thr=0.42):
    """xs/ys: axis lists from snap_axis. Returns cells, region id per inside cell, wall flags."""
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
    # outside: flood from the ring through edges without a wall, into paper-like cells
    outside = np.zeros((ny, nx), bool)
    stack = []
    for j in range(ny):
        for i in range(nx):
            border = (i == 0 and not vwall[j, 0]) or (i == nx - 1 and not vwall[j, nx]) or \
                     (j == 0 and not hwall[0, i]) or (j == ny - 1 and not hwall[ny, i])
            if border and pap[j, i] > 0.35:
                outside[j, i] = True; stack.append((j, i))
    while stack:
        j, i = stack.pop()
        for dj, di, wall in ((0, -1, vwall[j, i]), (0, 1, vwall[j, i + 1] if i + 1 <= nx else True),
                             (-1, 0, hwall[j, i]), (1, 0, hwall[j + 1, i] if j + 1 <= ny else True)):
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
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception:  # noqa: BLE001
        if not allow_install:
            return [], "rapidocr-onnxruntime not installed"
        import subprocess
        mark = Path.home() / ".config/aiw/rapidocr-tried"
        if mark.exists():
            return [], "rapidocr-onnxruntime not installed (install tried before)"
        mark.parent.mkdir(parents=True, exist_ok=True); mark.touch()
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-q", "rapidocr-onnxruntime"],
                       timeout=600, capture_output=True)
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except Exception:  # noqa: BLE001
            return [], "rapidocr-onnxruntime could not be installed"
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(im.width, x1), min(im.height, y1)
    crop = im.crop((x0, y0, x1, y1))
    k = max(1, min(k, int(2400 / max(1, max(crop.size)))))
    big = np.asarray(crop.resize((crop.width * k, crop.height * k), Image.LANCZOS))
    res, _ = RapidOCR()(big)
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
    if not T and chains and fit is not None:
        try:
            T = (fit(spec) or {}).get("transform") or {}
        except Exception as e:  # noqa: BLE001
            info["warnings"].append(f"fit failed: {e}")
    if not all(k in T for k in ("ax", "bx", "ay", "by")):
        raise ValueError("no pixel->mm transform: read the dimension chains first (drawing_read) and pass them with "
                         "--chains, or give --transform ax,bx,ay,by")
    spec["source"]["transform"] = {k: round(float(v), 6) for k, v in T.items() if k in ("ax", "bx", "ay", "by")}

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
    pad = 300 * s
    X0, X1 = int(max(0, px0 - pad)), int(min(W, px1 + pad)); Y0, Y1 = int(max(0, py0 - pad)), int(min(H, py1 + pad))
    min_len = max(4, 700 * s)
    cx = line_candidates(wv.T, X0, X1, Y0, Y1, min_len)   # columns: transpose so rows = x
    cy = line_candidates(wh, Y0, Y1, X0, X1, min_len)
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
    g = build_rooms(wv, wh, paper, xs, ys, band)
    # labels
    labs = list(labels or [])
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
            continue
        by_reg.setdefault(reg_of[c], []).append((lab, c))
    rooms = []
    used = {}

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
            rooms.append({"id": rid("room"), "name": "房间?", "poly": poly, "confidence": "low", "auto": True})
            spec["uncertain"].append(f"房间 {poly_box(poly)} 没有读到文字标签：按原图补上名字")
            continue
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
        zone = "public" if len(lab) > 1 else None
        for k, (lb, _c0) in enumerate(lab):
            if not groups[k]:
                continue
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
    spec["notes"].append("自动草稿（drawkit auto）：墙线来自像素、比例来自尺寸链刻度。门窗未自动识别，需按原图补。")
    info.update({"x_lines": [round(e["mm"]) for e in xs], "y_lines": [round(e["mm"]) for e in ys],
                 "x_unsnapped": [round(e["mm"]) for e in xs if not e["mark"]],
                 "y_unsnapped": [round(e["mm"]) for e in ys if not e["mark"]],
                 "regions": len(g["regions"]), "rooms": len(rooms),
                 "plan_px": [round(px0 - pad), round(py0 - pad), round(px1 + pad), round(py1 + pad)]})
    if debug:
        draw_debug(im, xs, ys, g, rooms, labs, T, debug)
        info["debug"] = str(debug)
    return spec, info


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


if __name__ == "__main__":     # convenience: python3 drawauto.py IMG CHAINS.json [LABELS.json]
    from importlib import import_module
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    dk = import_module("drawkit")
    sys.exit(dk.main(["auto"] + sys.argv[1:]))
