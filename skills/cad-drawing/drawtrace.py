#!/usr/bin/env python3
"""drawtrace: deterministic floor-plan tracing from an image (v3.9.2). No vision model anywhere.

Geometry comes from pixels (OpenCV), text from OCR (RapidOCR, onnxruntime), meaning from rules:

  scale     OCR reads the dimension numbers ("约9800mm", or a chain 2700/3800/3600); the dimension line under / beside
            them is found in the pixels, its length in px gives mm/px per axis (both axes when both are dimensioned).
  walls     dark pixels -> morphological opening at the wall thickness (thin furniture / text / hatch lines drop
            out) -> horizontal and vertical wall rectangles = centre line + thickness, as drawn.
  openings  a gap between two collinear wall pieces: >=2 thin parallel lines in it = window (exterior) or sliding
            door (interior); a quarter-circle arc next to it = swing door (hinge + side from the arc); else opening.
  rooms     barrier = walls + closed openings; every OCR room name is a seed; a multi-source shortest path where
            crossing a drawn line is expensive splits open plans the way the drawing does (thin partitions become
            the boundaries); each region -> rectilinear polygon. A boundary between two named regions that lies on
            a long drawn line is a light partition wall.
  furniture the remaining thin line work inside the plan, traced as polylines on its own layer (optional).

The result is a drawkit spec (`walls_traced`), plus `verify()` which rasterises the generated walls back onto the
source and measures how well they coincide (precision / recall / worst misses), checks every OCR label sits in the
room of that name and that the drawn overall dimensions equal the printed ones. Thresholds are hard: a failing
verify is reported as errors, never as "looks about right".
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
from pathlib import Path

VERSION = "trace-1.0"

ROOM_WORDS = ("厅", "房", "室", "卧", "卫", "厨", "阳台", "玄关", "过道", "走廊", "衣帽", "书", "储", "露台", "花园",
              "空间", "设备", "空调", "烟道", "管井", "电梯", "楼梯", "门厅", "入户", "洗衣", "家政", "影音", "茶",
              "餐", "客", "飘窗", "平台", "车库", "工作", "保姆", "老人", "儿童", "主", "次", "多功能")
OCR_FIX = {"卫生同": "卫生间", "卧窒": "卧室", "厨方": "厨房", "客斤": "客厅", "餐斤": "餐厅", "阳合": "阳台", "书方": "书房"}
NOT_ROOM = re.compile(r"(\d{3,}|mm|㎡|m²|m2|户型|平面图|面积|建筑|套内|示意|比例|北|N$|[一二三四五六两双][室房厅卫].*[室房厅卫])", re.I)


# ----------------------------------------------------------------------------------------------- small helpers

def _np():
    import numpy as np
    return np


def _cv():
    import cv2
    return cv2


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_image(path):
    """BGR image (CJK-safe path). Small images are enlarged so thin lines and text survive."""
    np, cv2 = _np(), _cv()
    data = np.fromfile(str(path), dtype=np.uint8)
    im = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError(f"cannot read image {path}")
    h, w = im.shape[:2]
    f = 1.0
    if max(h, w) < 1600:
        f = 1600.0 / max(h, w)
    elif max(h, w) > 3200:
        f = 3200.0 / max(h, w)
    if f != 1.0:
        im = cv2.resize(im, (int(round(w * f)), int(round(h * f))), interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
    return im, f


_OCR = None


def ocr(im):
    """[(text, score, (x0,y0,x1,y1))] in image px; vertical text read on a rotated copy."""
    global _OCR
    cv2 = _cv()
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    out = []
    H, W = im.shape[:2]
    k = 2.0 if max(H, W) < 2400 else 1.0       # dimension numbers are small: read them on a 2x copy
    big = cv2.resize(im, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC) if k != 1.0 else im
    res, _ = _OCR(big)
    res = [([(p[0] / k, p[1] / k) for p in box], t, sc) for box, t, sc in (res or [])]
    for box, text, score in res or []:
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        out.append((text.strip(), float(score), (min(xs), min(ys), max(xs), max(ys)), "h"))
    rot = cv2.rotate(big, cv2.ROTATE_90_CLOCKWISE)       # rotated (x', y') = (H-1-y, x)
    res, _ = _OCR(rot)
    res = [([(p[0] / k, p[1] / k) for p in box], t, sc) for box, t, sc in (res or [])]
    for box, text, score in res or []:
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        x0, x1 = min(ys), max(ys)
        y0, y1 = H - 1 - max(xs), H - 1 - min(xs)
        if (x1 - x0) * 1.5 < (y1 - y0):                   # really vertical text: keep
            out.append((text.strip(), float(score), (x0, y0, x1, y1), "v"))
    return out


def parse_number(t):
    t = t.replace(",", "").replace("，", "").replace(" ", "")
    m = re.findall(r"\d{3,6}", t)
    return [int(v) for v in m]


def is_room_name(t):
    t = t.strip()
    if not t or len(t) > 10 or NOT_ROOM.search(t):
        return False
    if not any("\u4e00" <= ch <= "\u9fff" for ch in t):
        return False
    return any(w in t for w in ROOM_WORDS)


def room_type(name):
    if "阳台" in name or "露台" in name or "花园" in name or "平台" in name:
        return "balcony"
    if "卫" in name or "洗手" in name:
        return "bath"
    if "厨" in name:
        return "kitchen"
    if any(w in name for w in ("烟道", "管井", "空调", "设备")):
        return "shaft"
    return "room"


def room_zone(name):
    if any(w in name for w in ("客", "餐", "玄关", "过道", "走廊", "门厅", "入户", "厅")) and "卧" not in name:
        return "public"
    return None


# ----------------------------------------------------------------------------------------------- 1. masks

def dark_threshold(g):
    """the ink threshold: Otsu among non-background pixels, clamped."""
    np, cv2 = _np(), _cv()
    sel = g[g < 245]
    if sel.size < 100:
        return 100
    t, _ = cv2.threshold(sel.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return int(min(130, max(60, t * 0.75)))


def wall_kernel(dark):
    """the opening size at which thin lines are gone and walls are not yet: first plateau of the kept area."""
    np, cv2 = _np(), _cv()
    prev = int(dark.sum())
    areas = []
    for k in range(3, 31, 2):
        m = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        a = int(m.sum())
        areas.append((k, a))
        if a == 0:
            break
    # first k (>=5) after which shrinking slows to < 12 % per step, while still keeping something wall-like
    best = 5
    for (k0, a0), (k1, a1) in zip(areas, areas[1:]):
        if k0 >= 5 and a0 > 0 and (a0 - a1) / a0 < 0.12:
            best = k0
            break
    else:
        best = 7
    return best, areas


def run_lengths(mask, axis):
    """length of the run (along axis 1 = horizontal, 0 = vertical) each pixel belongs to; 0 off the mask."""
    np = _np()
    m = mask.astype(bool)
    if axis == 0:
        return run_lengths(m.T, 1).T
    out = np.zeros(m.shape, np.int32)
    for y in range(m.shape[0]):
        row = m[y]
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        st = np.where(d == 1)[0]; en = np.where(d == -1)[0]
        for a, b in zip(st, en):
            out[y, a:b] = b - a
    return out


def axis_rects(mask, min_len):
    """decompose a wall mask into horizontal and vertical rectangles (x0,y0,x1,y1,'h'|'v') in px: a pixel belongs to
    a horizontal wall when its horizontal run is long and longer than its vertical run (and vice versa), so junctions
    never glue two walls into one blob."""
    np, cv2 = _np(), _cv()
    hr = run_lengths(mask, 1); vr = run_lengths(mask, 0)
    out = []
    for o, cls in (("h", (hr >= min_len) & (hr > vr)), ("v", (vr >= min_len) & (vr >= hr))):
        n, lab, st, _ = cv2.connectedComponentsWithStats(cls.astype(np.uint8), 4)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            L = w if o == "h" else h
            if L < min_len or a < min_len * 2:
                continue
            sub = lab[y:y + h, x:x + w] == i
            prof = sub.sum(axis=0) if o == "h" else sub.sum(axis=1)       # thickness along the wall
            # split where the thickness changes (a thicker / thinner stretch is its own wall)
            cuts = [0]
            ref = None
            for j, tv in enumerate(prof):
                if tv == 0:
                    continue
                if ref is None:
                    ref = tv
                elif abs(tv - ref) > max(2, 0.3 * ref):
                    cuts.append(j); ref = tv
                else:
                    ref = 0.9 * ref + 0.1 * tv
            cuts.append(len(prof))
            for j0, j1 in zip(cuts, cuts[1:]):
                if j1 - j0 < max(4, min_len // 3):
                    continue
                part = sub[:, j0:j1] if o == "h" else sub[j0:j1, :]
                if not part.any():
                    continue
                if o == "h":
                    rows = np.where(part.sum(axis=1) >= 0.5 * (j1 - j0))[0]
                    if rows.size == 0:
                        continue
                    out.append((x + j0, y + rows.min(), x + j1, y + rows.max() + 1, o))
                else:
                    cols = np.where(part.sum(axis=0) >= 0.5 * (j1 - j0))[0]
                    if cols.size == 0:
                        continue
                    out.append((x + cols.min(), y + j0, x + cols.max() + 1, y + j1, o))
    return out


# ----------------------------------------------------------------------------------------------- 2. scale

def find_dim_line(ink, box, orient, value, W, H):
    """the dimension line belonging to a number: a long straight ink run next to the text box."""
    np = _np()
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    th = (y1 - y0) if orient == "h" else (x1 - x0)
    best = None
    if orient == "h":
        cx = (x0 + x1) // 2
        for y in list(range(y1, min(H, y1 + 3 * th))) + list(range(max(0, y0 - 3 * th), y0)):
            row = ink[y]
            if not row[cx]:
                continue
            a = cx
            while a > 0 and (row[a - 1] or row[max(0, a - 2)] or row[max(0, a - 3)]):
                a -= 1
            b = cx
            while b < W - 1 and (row[b + 1] or row[min(W - 1, b + 2)] or row[min(W - 1, b + 3)]):
                b += 1
            L = b - a
            if L > 3 * (x1 - x0) and (best is None or L > best[1] - best[0]):
                best = (a, b, y)
    else:
        cy = (y0 + y1) // 2
        for x in list(range(x1, min(W, x1 + 3 * th))) + list(range(max(0, x0 - 3 * th), x0)):
            col = ink[:, x]
            if not col[cy]:
                continue
            a = cy
            while a > 0 and (col[a - 1] or col[max(0, a - 2)] or col[max(0, a - 3)]):
                a -= 1
            b = cy
            while b < H - 1 and (col[b + 1] or col[min(H - 1, b + 2)] or col[min(H - 1, b + 3)]):
                b += 1
            L = b - a
            if L > 3 * (y1 - y0) and (best is None or L > best[1] - best[0]):
                best = (a, b, x)
    return best


def line_ticks(ink, a, b, pos, orient, reach):
    """positions along a dimension line where a tick / extension line crosses it (ink reaching >= reach px off the
    line on either side). The chain's length is measured tick to tick, not end to end (lines overshoot)."""
    H, W = ink.shape
    out = []
    for t in range(max(0, a), min(b + 1, W if orient == "h" else H)):
        hit = 0
        for side in (-1, 1):
            n = 0
            for d in range(2, reach + 2):
                y, x = (pos + side * d, t) if orient == "h" else (t, pos + side * d)
                if 0 <= y < H and 0 <= x < W and (ink[y, x] or (orient == "h" and (ink[y, max(0, x - 1)] or ink[y, min(W - 1, x + 1)]))
                                                  or (orient == "v" and (ink[max(0, y - 1), x] or ink[min(H - 1, y + 1), x]))):
                    n += 1
            hit = max(hit, n)
        if hit >= reach * 0.8:
            out.append(t)
    # runs -> centres
    ticks = []
    for t in out:
        if ticks and t - ticks[-1][-1] <= 2:
            ticks[-1].append(t)
        else:
            ticks.append([t])
    return [sum(r) / len(r) for r in ticks]


def dimension_chains(texts, ink, W, H, plan_box):
    """group the printed numbers into chains (one per side), each with its line extent in px."""
    nums = []
    for t, s, b, o in texts:
        vals = parse_number(t)
        if len(vals) != 1 or not (300 <= vals[0] <= 100000) or s < 0.6:
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        px0, py0, px1, py1 = plan_box
        if px0 <= cx <= px1 and py0 <= cy <= py1:
            continue                                   # a number inside the plan is not a chain number
        if o == "h" and (cy < py0 or cy > py1):
            side = "top" if cy < py0 else "bottom"
        elif o == "v" and (cx < px0 or cx > px1):
            side = "left" if cx < px0 else "right"
        else:
            continue
        nums.append({"side": side, "value": vals[0], "box": b, "o": o, "c": (cx, cy)})
    chains = {}
    for side in ("top", "bottom", "left", "right"):
        group = [n for n in nums if n["side"] == side]
        if not group:
            continue
        # rows: numbers at a similar distance from the plan are one chain; the innermost complete one is used
        key = (lambda n: n["c"][1]) if side in ("top", "bottom") else (lambda n: n["c"][0])
        group.sort(key=key)
        rows = []
        for n in group:
            if rows and abs(key(rows[-1][-1]) - key(n)) < 0.6 * max(n["box"][3] - n["box"][1], n["box"][2] - n["box"][0], 8) \
                    and True:
                rows[-1].append(n)
            else:
                rows.append([n])
        cand = []
        for row in rows:
            along = (lambda n: n["c"][0]) if side in ("top", "bottom") else (lambda n: n["c"][1])
            row.sort(key=along)
            o = "h" if side in ("top", "bottom") else "v"
            lines = [find_dim_line(ink, n["box"], o, n["value"], W, H) for n in row]
            lines = [l for l in lines if l]
            if not lines:
                continue
            a = min(l[0] for l in lines); b = max(l[1] for l in lines)
            pos = sorted(l[2] for l in lines)[len(lines) // 2]
            vals = [n["value"] for n in row]
            th = int(max(4, min(n["box"][3] - n["box"][1], n["box"][2] - n["box"][0]) * 0.35))
            ticks = line_ticks(ink, a, b, pos, o, th)
            by = "line ends"
            if len(ticks) >= 2 and ticks[-1] - ticks[0] > 0.6 * (b - a):
                # with every segment's tick found, they must sit where the cumulative values put them
                a, b = ticks[0], ticks[-1]
                by = f"ticks ({len(ticks)} found)"
            cand.append({"side": side, "values": vals, "line_px": [a, b], "pos_px": pos, "n": len(vals), "span_by": by,
                         "ticks_px": ticks, "mm_per_px": sum(vals) / float(max(1, b - a))})
        if cand:
            # prefer the chain with the most segments that is consistent; the overall line, if separate, checks it
            cand.sort(key=lambda c: (-c["n"], -(c["line_px"][1] - c["line_px"][0])))
            chains[side] = cand[0]
            if len(cand) > 1:
                chains[side]["others"] = [{"values": c["values"], "line_px": c["line_px"]} for c in cand[1:]]
    return chains


def double_line_walls(ink, thick, dmin, dmax, L, gray=None, plan_box=None):
    """light partitions drawn as two thin parallel lines dmin..dmax apart (the fill between them may be the floor
    colour): the gap is closed across, the band kept when it is long and touches a thick wall or another band."""
    np, cv2 = _np(), _cv()
    lines = ink.astype(np.uint8)
    out = np.zeros_like(lines)
    floor_filled = False
    if gray is not None and plan_box is not None:
        px0, py0, px1, py1 = plan_box
        win = gray[py0:py1, px0:px1]
        floor_filled = win.size > 0 and float(((win > 150) & (win < 245)).mean()) > 0.25
    for o in ("h", "v"):
        along = np.ones((1, L), np.uint8) if o == "h" else np.ones((L, 1), np.uint8)
        across = np.ones((dmax + 1, 1), np.uint8) if o == "h" else np.ones((1, dmax + 1), np.uint8)
        thick_k = np.ones((dmin, 1), np.uint8) if o == "h" else np.ones((1, dmin), np.uint8)
        ln = cv2.morphologyEx(lines, cv2.MORPH_OPEN, along)             # long straight thin lines only
        cl = cv2.morphologyEx(ln, cv2.MORPH_CLOSE, across)              # two of them close together -> a band
        cl = cv2.morphologyEx(cl, cv2.MORPH_OPEN, thick_k)              # at least dmin across
        cl = cv2.morphologyEx(cl, cv2.MORPH_OPEN, along)
        n, lab, st, _ = cv2.connectedComponentsWithStats(cl, 4)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            tk = a / float(w if o == "h" else h)
            if not (dmin <= tk <= dmax + 2):
                continue
            if plan_box is not None:
                px0, py0, px1, py1 = plan_box
                if x + w < px0 or x > px1 or y + h < py0 or y > py1:
                    continue
            comp = lab == i
            if gray is not None and floor_filled:
                between = comp & (ink == 0)
                nb = int(between.sum())
                if nb and float((gray[between] >= 245).sum()) / nb > 0.5:
                    continue          # white between the two lines: furniture outline (floors are filled)
            out[comp] = 1
    out[thick > 0] = 0
    # keep bands connected to the thick walls (directly or through other bands)
    allw = ((out > 0) | (thick > 0)).astype(np.uint8)
    allw = cv2.dilate(allw, np.ones((3, 3), np.uint8))
    n, lab = cv2.connectedComponents(allw, connectivity=8)
    good = set(np.unique(lab[thick > 0])) - {0}
    keep = np.isin(lab, list(good)) & (out > 0)
    return keep.astype(np.uint8)


# ----------------------------------------------------------------------------------------------- 3. openings

def arc_score(thin, hinge, r, quadrant_dirs):
    """fraction of a quarter circle (radius r around the hinge, between two unit directions) that lies on ink."""
    np = _np()
    H, W = thin.shape
    (ux, uy), (vx, vy) = quadrant_dirs
    hit = tot = 0
    for i in range(1, 30):
        t = (math.pi / 2) * i / 30.0
        dx = math.cos(t) * ux + math.sin(t) * vx
        dy = math.cos(t) * uy + math.sin(t) * vy
        x = int(round(hinge[0] + r * dx)); y = int(round(hinge[1] + r * dy))
        if not (2 <= x < W - 2 and 2 <= y < H - 2):
            continue
        tot += 1
        if thin[y - 2:y + 3, x - 2:x + 3].any():
            hit += 1
    return hit / tot if tot else 0.0


def parallel_lines(thin, rect, o):
    """how many distinct ink lines run along the gap (window glazing / sliding leaves)."""
    np = _np()
    x0, y0, x1, y1 = [int(v) for v in rect]
    win = thin[max(0, y0):y1, max(0, x0):x1]
    if win.size == 0:
        return 0
    prof = win.mean(axis=1 if o == "h" else 0)
    on = prof > 0.6
    n = 0; prev = False
    for v in on:
        if v and not prev:
            n += 1
        prev = v
    return n


# ----------------------------------------------------------------------------------------------- 4. rooms

def multi_source(cost, seeds, blocked):
    """multi-source Dijkstra on a small grid: label of the cheapest seed for every free cell."""
    np = _np()
    h, w = cost.shape
    INF = float("inf")
    dist = np.full((h, w), INF)
    lab = np.full((h, w), -1, dtype=np.int32)
    pq = []
    for i, (x, y) in enumerate(seeds):
        if 0 <= x < w and 0 <= y < h and not blocked[y, x]:
            dist[y, x] = 0; lab[y, x] = i
            heapq.heappush(pq, (0.0, x, y, i))
    while pq:
        d, x, y, i = heapq.heappop(pq)
        if d > dist[y, x]:
            continue
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not blocked[ny, nx]:
                nd = d + cost[ny, nx]
                if nd < dist[ny, nx]:
                    dist[ny, nx] = nd; lab[ny, nx] = i
                    heapq.heappush(pq, (nd, nx, ny, i))
    return lab


def ortho_poly(mask, cell, smooth):
    """largest region of a cell mask -> rectilinear polygon (px of the full image)."""
    np, cv2 = _np(), _cv()
    m = mask.astype(np.uint8)
    if smooth > 1:
        k = np.ones((smooth, smooth), np.uint8)
        m2 = cv2.morphologyEx(cv2.morphologyEx(m, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k)
        if m2.sum() > 0.5 * m.sum():
            m = m2
    # pull the outline in by more than the simplification tolerance: neighbouring rooms (open plans have no wall
    # between them) can then never overlap after the corners are re-squared
    er = cv2.erode(m, np.ones((7, 7), np.uint8))
    if er.sum() > 0.3 * m.sum():
        m = er
    cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = max(cs, key=cv2.contourArea)
    eps = 1.5
    p = cv2.approxPolyDP(c, eps, True).reshape(-1, 2).astype(float)
    if len(p) < 4:
        x, y, w, h = cv2.boundingRect(c)
        p = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], float)
    pts = orthogonalize([tuple(q) for q in p])
    if not pts or len(pts) < 4:
        x, y, w, h = cv2.boundingRect(c)
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return [((x + 0.5) * cell, (y + 0.5) * cell) for x, y in pts]


def orthogonalize(pts):
    """each edge becomes horizontal or vertical (by its direction); runs of one kind merge; corners re-intersected."""
    n = len(pts)
    if n < 3:
        return None
    edges = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            continue
        kind = "h" if abs(dx) >= abs(dy) else "v"
        L = math.hypot(dx, dy)
        val = (a[1] + b[1]) / 2 if kind == "h" else (a[0] + b[0]) / 2
        edges.append([kind, val, L])
    merged = []
    for e in edges:
        if merged and merged[-1][0] == e[0]:
            p = merged[-1]
            p[1] = (p[1] * p[2] + e[1] * e[2]) / (p[2] + e[2]); p[2] += e[2]
        else:
            merged.append(list(e))
    if len(merged) > 1 and merged[0][0] == merged[-1][0]:
        p = merged.pop()
        q = merged[0]
        q[1] = (q[1] * q[2] + p[1] * p[2]) / (q[2] + p[2]); q[2] += p[2]
    if len(merged) < 4 or len(merged) % 2:
        return None
    out = []
    for i in range(len(merged)):
        e, f = merged[i - 1], merged[i]
        if e[0] == f[0]:
            return None
        x = e[1] if e[0] == "v" else f[1]
        y = e[1] if e[0] == "h" else f[1]
        out.append((x, y))
    return out


def skeleton(mask):
    """Zhang-Suen thinning (numpy), 1-px centre lines of the thin line work."""
    np = _np()
    img = (mask > 0).astype(np.uint8)
    img = np.pad(img, 1)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            P2 = img[:-2, 1:-1]; P3 = img[:-2, 2:]; P4 = img[1:-1, 2:]; P5 = img[2:, 2:]
            P6 = img[2:, 1:-1]; P7 = img[2:, :-2]; P8 = img[1:-1, :-2]; P9 = img[:-2, :-2]
            C = img[1:-1, 1:-1]
            nb = [P2, P3, P4, P5, P6, P7, P8, P9]
            B = sum(n.astype(np.int16) for n in nb)
            seq = nb + [P2]
            A = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.int16) for i in range(8))
            if step == 0:
                c1 = (P2 * P4 * P6) == 0; c2 = (P4 * P6 * P8) == 0
            else:
                c1 = (P2 * P4 * P8) == 0; c2 = (P2 * P6 * P8) == 0
            rm = (C == 1) & (B >= 2) & (B <= 6) & (A == 1) & c1 & c2
            if rm.any():
                img[1:-1, 1:-1][rm] = 0
                changed = True
    return img[1:-1, 1:-1]


def skeleton_paths(sk):
    """walk a 1-px skeleton into polylines (x, y): from end points and junctions, then the remaining loops."""
    np = _np()
    H, W = sk.shape
    on = set(zip(*np.where(sk > 0)))
    nbrs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    def nb(p):
        y, x = p
        return [(y + dy, x + dx) for dy, dx in nbrs if (y + dy, x + dx) in on]
    deg = {p: len(nb(p)) for p in on}
    seen_edge = set()
    paths = []
    starts = [p for p, d in deg.items() if d != 2] + [p for p, d in deg.items() if d == 2]
    visited = set()
    for s in starts:
        for n0 in nb(s):
            e = (min(s, n0), max(s, n0))
            if e in seen_edge:
                continue
            path = [s, n0]; seen_edge.add(e)
            prev, cur = s, n0
            while deg.get(cur, 0) == 2:
                nxt = [q for q in nb(cur) if q != prev]
                if not nxt:
                    break
                q = nxt[0]
                e = (min(cur, q), max(cur, q))
                if e in seen_edge:
                    break
                seen_edge.add(e)
                path.append(q); prev, cur = cur, q
            paths.append([(x, y) for y, x in path])
            visited.update(path)
        if deg.get(s, 0) == 0 and s not in visited:
            visited.add(s)
    return paths


# ----------------------------------------------------------------------------------------------- the tracer

class Trace:
    def __init__(self, path, hints=None):
        np, cv2 = _np(), _cv()
        self.path = str(path)
        self.hints = hints or {}
        self.im, self.f = load_image(path)
        self.g = cv2.cvtColor(self.im, cv2.COLOR_BGR2GRAY)
        self.H, self.W = self.g.shape
        self.notes, self.warnings, self.uncertain = [], [], []

    # -- everything
    def run(self):
        np, cv2 = _np(), _cv()
        g = self.g
        self.T = dark_threshold(g)
        self.texts = ocr(self.im)
        text_mask = np.zeros_like(g, dtype=np.uint8)
        for t, s, b, o in self.texts:
            x0, y0, x1, y1 = [int(round(v)) for v in b]
            cv2.rectangle(text_mask, (x0 - 2, y0 - 2), (x1 + 2, y1 + 2), 1, -1)
        self.text_mask = text_mask
        dark = ((g < self.T) & (text_mask == 0)).astype(np.uint8)
        self.ink = (g < 200) & (text_mask == 0)              # any drawn line (thin lines are often grey)
        k, areas = wall_kernel(dark)
        self.k = k
        W0 = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        n, lab, st, _ = cv2.connectedComponentsWithStats(W0, 8)
        keep = np.zeros(n, bool)
        cand = []
        for i in range(1, n):
            x, y, w_, h_, a = st[i]
            if a < 6 * k * k or max(w_, h_) < 4 * k:
                continue
            # a wall network is a frame (thin strokes), a solid dark panel / photo / logo fills its box
            if a / float(w_ * h_) > 0.45 and min(w_, h_) > 6 * k:
                continue
            cand.append(i)
        if not cand:
            raise ValueError("no walls found (no thick dark lines in the image)")
        # the plan: the biggest wall network, plus the pieces lying within / next to its box
        main = max(cand, key=lambda i: st[i][4])
        x, y, w_, h_, _ = st[main]
        bx0, by0, bx1, by1 = x, y, x + w_, y + h_
        mg = int(0.10 * max(self.W, self.H))          # walls broken by door / window gaps are still one plan
        keep[main] = True
        grew = True
        while grew:
            grew = False
            for i in cand:
                if keep[i]:
                    continue
                x, y, w_, h_, _ = st[i]
                if x + w_ >= bx0 - mg and x <= bx1 + mg and y + h_ >= by0 - mg and y <= by1 + mg:
                    keep[i] = True; grew = True
                    bx0, by0, bx1, by1 = min(bx0, x), min(by0, y), max(bx1, x + w_), max(by1, y + h_)
        self.wall = keep[lab].astype(np.uint8)
        ys, xs = np.where(self.wall > 0)
        self.plan_box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
        self.thick = self.wall.copy()
        self._scale()
        dmin = max(3, int(round(40 / self.sx))); dmax = max(dmin + 2, int(round(180 / self.sx)))
        L = max(3 * k, int(round(500 / self.sx)))
        # light partitions drawn as thin double lines are NOT turned into walls by guesswork: every thin line is
        # traced as it is drawn (traced_lines), so they come out exactly as in the source. (double_line_walls is
        # kept for reporting only.)
        self.bands = double_line_walls(self.ink & (self.thick == 0), self.thick, dmin, dmax, L, self.g, self.plan_box)
        self.rects = axis_rects(self.wall, max(3 * k, 12))
        self._walls()
        self._openings()
        self._rooms()
        self._plausible()
        return self

    def _plausible(self):
        """the tracer only answers for drawings whose walls are drawn SOLID (black / dark filled bands). Hollow
        double-line walls, photos and colour renders leave the outline open: then it refuses (drawkit auto falls back
        to the wall-grid pipeline) instead of delivering a plan that only looks traced."""
        np, cv2 = _np(), _cv()
        inside = getattr(self, "_inside_cache", None)
        if inside is None:
            inside = self._inside_cache = self._inside_mask()
        R = ((inside > 0) | (self.barrier > 0)).astype(np.uint8)
        R = cv2.morphologyEx(R, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        cs, _ = cv2.findContours(R, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cs:
            raise ValueError("no enclosed plan area")
        c = max(cs, key=cv2.contourArea)
        near = cv2.dilate(self.barrier, np.ones((2 * self.k + 1, 2 * self.k + 1), np.uint8)) > 0
        pts = c.reshape(-1, 2)
        cov = float(np.mean([near[y, x] for x, y in pts]))
        named = [L for L in self.labels if L.get("region")]
        self.outline_coverage = round(cov, 3)
        if cov < float(os.environ.get("DRAWTRACE_MIN_OUTLINE", "0.5")):
            raise ValueError(f"the traced walls close only {cov:.0%} of the plan outline (walls drawn hollow / photo): "
                             "not traceable as solid walls")
        if self.labels and len(named) < 0.6 * len(self.labels):
            raise ValueError(f"only {len(named)} of {len(self.labels)} room names fall in closed rooms")

    # -- px <-> mm
    def _scale(self):
        np = _np()
        chains = dimension_chains(self.texts, self.ink, self.W, self.H, self.plan_box)
        self.chains = chains
        sx = [c["mm_per_px"] for s, c in chains.items() if s in ("top", "bottom")]
        sy = [c["mm_per_px"] for s, c in chains.items() if s in ("left", "right")]
        if self.hints.get("mm_per_px"):
            sx = sx or [float(self.hints["mm_per_px"])]
        if not sx and not sy:
            raise ValueError("no dimension numbers with a dimension line were found (OCR); give the overall size")
        if not sx:
            sx = sy; self.uncertain.append("横向没有尺寸标注：横向比例按纵向比例取（像素按正方形）")
        if not sy:
            sy = sx; self.uncertain.append("纵向没有尺寸标注：纵向比例按横向比例取（像素按正方形）")
        self.sx, self.sy = float(np.median(sx)), float(np.median(sy))
        for side, c in chains.items():
            ref = self.sx if side in ("top", "bottom") else self.sy
            if abs(c["mm_per_px"] / ref - 1) > 0.02:
                self.warnings.append(f"{side} chain total {sum(c['values'])} gives {c['mm_per_px']:.3f} mm/px, the other "
                                     f"chain on that axis {ref:.3f}: the drawing is not exactly to scale there")
        if abs(self.sx / self.sy - 1) > 0.03:
            self.warnings.append(f"x and y scales differ by {abs(self.sx / self.sy - 1) * 100:.1f}% (photo taken at an angle, "
                                 "or a dimension number is off)")
        # origin: the start of the top (else bottom) chain line, and of the left (else right) chain line
        cx = chains.get("top") or chains.get("bottom")
        cy = chains.get("left") or chains.get("right")
        self.x0 = cx["line_px"][0] if cx else self.plan_box[0]
        self.y0 = cy["line_px"][0] if cy else self.plan_box[1]

    def X(self, px):
        return (px - self.x0) * self.sx

    def Y(self, py):
        return (py - self.y0) * self.sy

    def PX(self, mm):
        return mm / self.sx + self.x0

    def PY(self, mm):
        return mm / self.sy + self.y0

    @staticmethod
    def snap(v, q=10):
        return int(round(v / q) * q)

    # -- walls as centre lines + thickness (mm)
    def _walls(self):
        segs = []
        for x0, y0, x1, y1, o in self.rects:
            if o == "h":
                t = (y1 - y0) * self.sy; c = self.Y((y0 + y1) / 2); u, v = self.X(x0), self.X(x1)
            else:
                t = (x1 - x0) * self.sx; c = self.X((x0 + x1) / 2); u, v = self.Y(y0), self.Y(y1)
            segs.append({"o": o, "c": c, "u": u, "v": v, "t": t, "px": (x0, y0, x1, y1)})
        # thickness classes: cluster to the drawing's own wall thicknesses (no 240/120 template)
        ts = sorted(s["t"] for s in segs)
        classes = []
        for t in ts:           # greedy, against the class's FIRST value (no chaining 60 -> 130 -> 200)
            if classes and t - classes[-1][0] <= max(20, 0.2 * classes[-1][0]):
                classes[-1].append(t)
            else:
                classes.append([t])
        reps = [sorted(c)[len(c) // 2] for c in classes]
        for s in segs:
            r = min(reps, key=lambda q: abs(q - s["t"]))
            s["t"] = max(60, self.snap(r, 10))
        # merge collinear overlapping pieces of equal thickness
        segs.sort(key=lambda s: (s["o"], s["t"], s["u"]))
        out = []
        for s in segs:
            m = None
            for p in out:
                if p["o"] == s["o"] and p["t"] == s["t"] and abs(p["c"] - s["c"]) <= 0.4 * s["t"] and s["u"] <= p["v"] + 1 and p["u"] <= s["v"] + 1:
                    m = p; break
            if m:
                L1, L2 = m["v"] - m["u"], s["v"] - s["u"]
                m["c"] = (m["c"] * L1 + s["c"] * L2) / max(1e-6, L1 + L2)
                m["u"], m["v"] = min(m["u"], s["u"]), max(m["v"], s["v"])
            else:
                out.append(dict(s))
        for s in out:
            s["c"] = self.snap(s["c"]); s["u"] = self.snap(s["u"]); s["v"] = self.snap(s["v"])
        # a wall end that stops short of a perpendicular wall by less than half a thickness meets it
        for s in out:
            for p in out:
                if p is s or p["o"] == s["o"]:
                    continue
                if p["u"] - p["t"] <= s["c"] <= p["v"] + p["t"]:
                    for end in ("u", "v"):
                        if abs(s[end] - p["c"]) <= p["t"] / 2 + 15:
                            s[end] = p["c"]
        self.segs = [s for s in out if s["v"] - s["u"] >= 2 * s["t"] or s["v"] - s["u"] >= 300]
        # legend thicknesses: the length-weighted dominant class is the main wall, the thinnest class with real
        # length (>= 10 % of all wall length) the interior one
        by_t = {}
        for s in self.segs:
            by_t[s["t"]] = by_t.get(s["t"], 0) + (s["v"] - s["u"])
        tot = sum(by_t.values()) or 1
        self.t_ext = max(by_t, key=lambda t: by_t[t])
        real = [t for t, L in by_t.items() if L >= 0.1 * tot]
        self.t_int = min(real) if real else self.t_ext

    # -- doors, windows, plain openings in the gaps of collinear walls
    def _openings(self):
        np = _np()
        thin = self.ink & (self.wall == 0)
        self.thin = thin
        lines = []          # collinear groups: same orientation, centre lines within half a thickness
        for s in sorted(self.segs, key=lambda s: (s["o"], s["c"])):
            for grp in lines:
                if grp[0]["o"] == s["o"] and abs(grp[0]["c"] - s["c"]) <= max(60, 0.5 * max(grp[0]["t"], s["t"])):
                    grp.append(s); break
            else:
                lines.append([s])
        lines = {i: g for i, g in enumerate(lines)}
        self.doors, self.windows, self.gaps = [], [], []
        seen = set()
        for key, ss in lines.items():
            ss.sort(key=lambda s: s["u"])
            for a, b in zip(ss, ss[1:]):
                lo, hi = a["v"], b["u"]
                w = hi - lo
                if not (450 <= w <= 4200):
                    continue
                o = a["o"]; c = (a["c"] + b["c"]) / 2; t = max(a["t"], b["t"])
                k = (o, round(c), round(lo), round(hi))
                if k in seen:
                    continue
                seen.add(k)
                # px rectangle of the gap (a little wider than the wall)
                if o == "h":
                    r = (self.PX(lo) + 2, self.PY(c - t), self.PX(hi) - 2, self.PY(c + t))
                else:
                    r = (self.PX(c - t), self.PY(lo) + 2, self.PX(c + t), self.PY(hi) - 2)
                nl = parallel_lines(thin, r, o)
                # arc: hinge at either end, swinging to either side
                best = (0.0, None)
                for hinge_end, hv, fv in (("start", lo, hi), ("end", hi, lo)):
                    for side in (1, -1):
                        if o == "h":
                            hp = (self.PX(hv), self.PY(c)); rr = abs(fv - hv) / self.sx
                            u = (1 if fv > hv else -1, 0); v = (0, side)
                        else:
                            hp = (self.PX(c), self.PY(hv)); rr = abs(fv - hv) / self.sy
                            u = (0, 1 if fv > hv else -1); v = (side, 0)
                        sc = arc_score(thin, hp, rr, (u, v))
                        if sc > best[0]:
                            best = (sc, (hinge_end, side))
                at = [self.snap((lo + hi) / 2), self.snap(c)] if o == "h" else [self.snap(c), self.snap((lo + hi) / 2)]
                gap = {"o": o, "c": c, "lo": lo, "hi": hi, "t": t, "at": at, "width": self.snap(w), "lines": nl,
                       "arc": round(best[0], 2)}
                if best[0] >= 0.45:
                    hinge, side = best[1]
                    gap["kind"] = "door"
                    probe = (at[0], at[1] + side * 400) if o == "h" else (at[0] + side * 400, at[1])
                    self.doors.append({"at": at, "width": self.snap(w), "hinge": hinge, "_into_probe": probe, "type": "swing"})
                elif nl >= 2:
                    gap["kind"] = "glazed"
                else:
                    gap["kind"] = "opening"
                self.gaps.append(gap)

    def _inside_mask(self):
        """pixels inside the plan: not reachable from the image border over background-white, with every
        wall gap closed."""
        np, cv2 = _np(), _cv()
        bar = self.wall.copy()
        for gp in self.gaps:
            o, c, lo, hi, t = gp["o"], gp["c"], gp["lo"], gp["hi"], gp["t"]
            if o == "h":
                p0 = (int(self.PX(lo)) - 1, int(self.PY(c - t / 2))); p1 = (int(self.PX(hi)) + 1, int(self.PY(c + t / 2)))
            else:
                p0 = (int(self.PX(c - t / 2)), int(self.PY(lo)) - 1); p1 = (int(self.PX(c + t / 2)), int(self.PY(hi)) + 1)
            cv2.rectangle(bar, p0, p1, 1, -1)
        self.barrier = bar
        white = (self.g >= 242) & (bar == 0)
        # colour-filled floors are not white even when light: keep them inside
        hsv = _cv().cvtColor(self.im, _cv().COLOR_BGR2HSV)
        white &= hsv[:, :, 1] < 25
        free = white.astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(free, 4)
        border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
        outside = np.isin(lab, list(border))
        inside = (~outside) & (bar == 0)
        # trim to the plan's bounding box (+ a little for balconies drawn outside the walls)
        x0, y0, x1, y1 = self.plan_box
        mg = int(3 * self.k)
        clip = np.zeros_like(inside)
        clip[max(0, y0 - mg):y1 + mg, max(0, x0 - mg):x1 + mg] = True
        return inside & clip

    def _rooms(self):
        np, cv2 = _np(), _cv()
        inside = self._inside_mask()
        cell = max(2, self.k // 2)
        h, w = self.H // cell, self.W // cell
        ins = cv2.resize(inside.astype(np.uint8), (w, h), interpolation=cv2.INTER_AREA) > 0
        bar = cv2.resize(self.barrier, (w, h), interpolation=cv2.INTER_AREA) > 0
        thin = cv2.resize(self.thin.astype(np.uint8), (w, h), interpolation=cv2.INTER_AREA) > 0
        blocked = (~ins) | bar
        cost = np.where(thin, 60.0, 1.0)
        labels = []
        for t, s, b, o in self.texts:
            if s < 0.6 or not is_room_name(t):
                continue
            name = re.sub(r"\s+", "", t).replace("（", "(").replace("）", ")")
            for bad, good in OCR_FIX.items():
                name = name.replace(bad, good)
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            labels.append({"name": name, "px": (cx, cy), "box": b})
        # the same name read twice (horizontal + rotated pass) at one place is one label
        uniq = []
        for L in sorted(labels, key=lambda L: -(L["box"][2] - L["box"][0]) * (L["box"][3] - L["box"][1])):
            if any(U["name"] == L["name"] and math.hypot(U["px"][0] - L["px"][0], U["px"][1] - L["px"][1])
                   < 1.5 * max(U["box"][2] - U["box"][0], U["box"][3] - U["box"][1]) for U in uniq):
                continue
            uniq.append(L)
        labels = uniq
        # 3.9.9 (task-0db): names given by the user / the model / the earlier spec ("加上客厅和厨房") are seeds too.
        # Before, the tracer used only its own OCR, so a name OCR could not read was wiped on every auto run.
        hb = sorted(max(L["box"][2] - L["box"][0], L["box"][3] - L["box"][1]) for L in labels) or [40 * self.f]
        hs = hb[len(hb) // 2]
        for H in self.hints.get("labels") or []:
            nm = re.sub(r"\s+", "", str(H.get("name") or ""))
            if not nm or H.get("x") is None or H.get("y") is None:
                continue
            x, y = float(H["x"]) * self.f, float(H["y"]) * self.f
            if any(L["name"] == nm and math.hypot(L["px"][0] - x, L["px"][1] - y) < 6 * hs for L in labels):
                continue     # OCR read the same name nearby: keep OCR's position
            if any(L["name"] == nm for L in labels) and H.get("by") == "spec":
                continue     # the earlier spec's copy of a name OCR read again elsewhere
            labels.append({"name": nm, "px": (x, y), "box": (x - hs, y - hs / 2.5, x + hs, y + hs / 2.5),
                           "by": H.get("by") or "hint"})
        if not labels:
            self.uncertain.append("图上没有读到房间名（OCR）：房间未命名，墙和线条照原图描")
        seeds = []
        for L in labels:
            x, y = int(L["px"][0] // cell), int(L["px"][1] // cell)
            if not (0 <= x < w and 0 <= y < h) or blocked[y, x]:
                # nearest free cell
                ys, xs = np.where(~blocked)
                if ys.size == 0:
                    seeds.append((-1, -1)); continue
                j = int(np.argmin((xs - x) ** 2 + (ys - y) ** 2))
                if (xs[j] - x) ** 2 + (ys[j] - y) ** 2 > (40 // cell + 3) ** 2:
                    seeds.append((-1, -1)); L["outside"] = True; continue
                x, y = int(xs[j]), int(ys[j])
            seeds.append((x, y))
        lab = multi_source(cost, seeds, blocked)
        self.cell = cell
        self.room_lab = lab
        rooms = []
        used = {}
        for i, L in enumerate(labels):
            m = lab == i
            area_mm2 = m.sum() * cell * cell * self.sx * self.sy
            if L.get("outside") or area_mm2 < 0.3e6:
                if L.get("outside"):
                    self.uncertain.append(f"「{L['name']}」在墙外（不在封闭区域里），只标注文字")
                L["region"] = None
                continue
            poly = ortho_poly(m, cell, smooth=max(1, int(150 / (cell * self.sx))))
            if not poly:
                continue
            pts = [[self.snap(self.X(x)), self.snap(self.Y(y))] for x, y in poly]
            rid = re.sub(r"\W", "", L["name"]) or f"r{i}"
            used[rid] = used.get(rid, 0) + 1
            if used[rid] > 1:
                rid = f"{rid}{used[rid]}"
            rooms.append({"id": rid, "name": L["name"], "poly": pts, "type": room_type(L["name"]),
                          "zone": room_zone(L["name"]), "label_at": [self.snap(self.X(L["px"][0])), self.snap(self.Y(L["px"][1]))],
                          "label_px": [round(L["px"][0] / self.f, 1), round(L["px"][1] / self.f, 1)], "confidence": "high",
                          "label_h": self.snap(0.8 * min(L["box"][2] - L["box"][0], L["box"][3] - L["box"][1]) * self.sy, 5),
                          "_cells": m})
            L["region"] = rid
        self.labels = labels
        # rooms of one open area (connected without crossing a barrier) share a zone
        comp_n, comp = cv2.connectedComponents((~blocked).astype(np.uint8), connectivity=4)
        for r in rooms:
            ys, xs = np.where(r["_cells"])
            cid = np.bincount(comp[ys, xs]).argmax() if ys.size else 0
            r["_comp"] = int(cid)
        # light partitions: the shared boundary of two named regions that lies on drawn line work
        self.partitions = []
        idx = {r["id"]: k for k, r in enumerate(rooms)}
        for o in ("h", "v"):
            if o == "h":
                a, b = lab[:-1, :], lab[1:, :]
            else:
                a, b = lab[:, :-1], lab[:, 1:]
            diff = (a != b) & (a >= 0) & (b >= 0)
            pairs = {}
            ys, xs = np.where(diff)
            for y, x in zip(ys, xs):
                pairs.setdefault((min(a[y, x], b[y, x]), max(a[y, x], b[y, x])), []).append((x, y))
            for (i, j), pts in pairs.items():
                if len(pts) * cell * (self.sx if o == "h" else self.sy) < 600:
                    continue
                # group by the boundary line coordinate
                byline = {}
                for x, y in pts:
                    byline.setdefault(y if o == "h" else x, []).append(x if o == "h" else y)
                for c0, along in byline.items():
                    if len(along) * cell * (self.sx if o == "h" else self.sy) < 600:
                        continue
                    along.sort()
                    on = 0
                    for v in along:
                        cy, cx = (c0, v) if o == "h" else (v, c0)
                        y0, y1 = max(0, cy - 1), min(h, cy + 3); x0, x1 = max(0, cx - 1), min(w, cx + 3)
                        if thin[y0:y1, x0:x1].any():
                            on += 1
                    if on / len(along) >= 0.75:
                        cc = (c0 + 1) * cell
                        if o == "h":
                            seg = {"o": "h", "c": self.snap(self.Y(cc)), "u": self.snap(self.X(along[0] * cell)),
                                   "v": self.snap(self.X((along[-1] + 1) * cell))}
                        else:
                            seg = {"o": "v", "c": self.snap(self.X(cc)), "u": self.snap(self.Y(along[0] * cell)),
                                   "v": self.snap(self.Y((along[-1] + 1) * cell))}
                        seg["t"] = max(50, self.snap(min(self.t_int * 0.6, 100)))
                        seg["between"] = [labels[i]["name"], labels[j]["name"]]
                        self.partitions.append(seg)
        # open areas: rooms that touch with no wall and no partition between them share a zone
        for r in rooms:
            r["zone"] = r["zone"] or None
        comps = {}
        for r in rooms:
            comps.setdefault(r["_comp"], []).append(r)
        part_pairs = {tuple(sorted(p["between"])) for p in self.partitions}
        for cid, rs in comps.items():
            if len(rs) < 2:
                continue
            for a_ in rs:
                for b_ in rs:
                    if a_ is b_ or tuple(sorted((a_["name"], b_["name"]))) in part_pairs:
                        continue
                    if (a_["type"], b_["type"]) == ("room", "room") and not (a_["zone"] and b_["zone"]):
                        z = a_["zone"] or b_["zone"] or f"open{cid}"
                        a_["zone"] = a_["zone"] or z; b_["zone"] = b_["zone"] or z
        for r in rooms:
            r.pop("_cells", None); r.pop("_comp", None)
            if not r["zone"]:
                r.pop("zone")
        self.rooms = rooms
        # door: which room it opens into
        for d in self.doors:
            p = d.pop("_into_probe")
            px, py = int(self.PX(p[0]) // cell), int(self.PY(p[1]) // cell)
            if 0 <= px < w and 0 <= py < h and lab[py, px] >= 0:
                rid = labels[lab[py, px]].get("region")
                if rid:
                    d["into"] = rid

    # -- the spec
    def spec(self, task=None, image_rel=None):
        walls = [{"a": [s["u"], s["c"]] if s["o"] == "h" else [s["c"], s["u"]],
                  "b": [s["v"], s["c"]] if s["o"] == "h" else [s["c"], s["v"]], "t": s["t"]} for s in self.segs]
        windows, doors = [], []
        for gp in self.gaps:
            if gp["kind"] == "glazed":
                ext = self._is_exterior(gp)
                if ext:
                    windows.append({"at": gp["at"], "width": gp["width"]})
                else:
                    doors.append({"at": gp["at"], "width": gp["width"], "type": "sliding"})
            elif gp["kind"] == "opening":
                doors.append({"at": gp["at"], "width": gp["width"], "type": "opening"})
        doors = self.doors + doors
        chains = []
        for side, c in self.chains.items():
            ch = {"side": side, "values": c["values"], "by": "ocr", "line_px": [round(v / self.f, 1) for v in c["line_px"]]}
            chains.append(ch)
        spec = {
            "schema": "aiw-drawing/1", "kind": "floor_plan", "title": self.hints.get("title") or "户型平面图",
            "subtitle": self.hints.get("subtitle") or "", "y_down": True,
            "source": {"image": image_rel or self.path, "md5": md5(self.path), "task": task, "trace": VERSION,
                       "transform": {"ax": round(1 / (self.sx * self.f), 6), "bx": round(self.x0 / self.f, 3),
                                     "ay": round(1 / (self.sy * self.f), 6), "by": round(self.y0 / self.f, 3)},
                       "mm_per_px": [round(self.sx * self.f, 4), round(self.sy * self.f, 4)]},
            "wall_mode": "traced",
            "walls": {"exterior": self.t_ext, "interior": self.t_int, "railing": 100},
            "walls_traced": walls,
            "chains": chains,
            "rooms": self.rooms,
            "doors": doors,
            "windows": windows,
            "furniture": [],
            "notes": ["墙、门窗、房间由像素和 OCR 确定性提取（drawtrace），未用视觉模型估坐标"],
            "uncertain": self.uncertain,
        }
        out_lab = [L for L in self.labels if L.get("region") is None]
        if out_lab:
            spec["free_labels"] = [{"text": L["name"], "at": [self.snap(self.X(L["px"][0])), self.snap(self.Y(L["px"][1]))],
                                    "h": self.snap(0.8 * min(L["box"][2] - L["box"][0], L["box"][3] - L["box"][1]) * self.sy, 5)}
                                   for L in out_lab]
        return spec

    def _is_exterior(self, gp):
        """a gap is on the exterior when one side of it is outside the plan."""
        np = _np()
        inside = getattr(self, "_inside_cache", None)
        if inside is None:
            inside = self._inside_cache = self._inside_mask()
        o, c, t = gp["o"], gp["c"], gp["t"]
        mid = (gp["lo"] + gp["hi"]) / 2
        res = []
        for sgn in (1, -1):
            off = c + sgn * (t / 2 + 250)
            px, py = (self.PX(mid), self.PY(off)) if o == "h" else (self.PX(off), self.PY(mid))
            px, py = int(px), int(py)
            if 0 <= px < self.W and 0 <= py < self.H:
                res.append(bool(inside[max(0, py - 2):py + 3, max(0, px - 2):px + 3].any()))
            else:
                res.append(False)
        return not all(res)

    def hollow_walls(self):
        """wall pieces drawn HOLLOW (two thin parallel lines) that continue a solid wall: same direction and centre
        line, about the same thickness, starting where the solid wall ends. Cabinet / bed outlines are two parallel
        lines too, but they do not continue a wall; window lines inside a wall gap are openings. In mm (y down)."""
        np, cv2 = _np(), _cv()
        if getattr(self, "_hollow", None) is not None:
            return self._hollow
        out = []
        bands = getattr(self, "bands", None)
        if bands is None or not bands.any():
            self._hollow = out
            return out
        bands = bands.astype(np.uint8).copy()
        for gp in getattr(self, "gaps", []):
            o, c, lo, hi, t = gp["o"], gp["c"], gp["lo"], gp["hi"], gp["t"]
            if o == "h":
                r = (int(self.PX(lo)) - 2, int(self.PY(c - t)), int(self.PX(hi)) + 2, int(self.PY(c + t)))
            else:
                r = (int(self.PX(c - t)), int(self.PY(lo)) - 2, int(self.PX(c + t)), int(self.PY(hi)) + 2)
            bands[max(0, r[1]):r[3] + 1, max(0, r[0]):r[2] + 1] = 0
        n, lab, st, _ = cv2.connectedComponentsWithStats(bands, 8)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            o = "h" if w * self.sx >= h * self.sy else "v"
            length = w * self.sx if o == "h" else h * self.sy
            if length < 500:
                continue
            thick = a * (self.sy if o == "h" else self.sx) / float(w if o == "h" else h)
            c = self.Y(y + h / 2.0) if o == "h" else self.X(x + w / 2.0)
            u0, u1 = (self.X(x), self.X(x + w)) if o == "h" else (self.Y(y), self.Y(y + h))
            for sg in self.segs:
                if sg["o"] != o or abs(sg["c"] - c) > 0.5 * sg["t"] or not (0.6 * sg["t"] <= thick <= 1.4 * sg["t"]):
                    continue
                if min(abs(u0 - sg["v"]), abs(u1 - sg["u"])) <= max(2 * sg["t"], 150):
                    out.append({"o": o, "c": round(sg["c"], 1), "u": round(u0, 1), "v": round(u1, 1), "t": round(sg["t"], 1)})
                    break
        self._hollow = out
        return out

    def furniture_lines(self, max_items=20000):
        """thin line work inside the plan (furniture, fittings), as polylines in mm (y down)."""
        np, cv2 = _np(), _cv()
        inside = getattr(self, "_inside_cache", None)
        if inside is None:
            inside = self._inside_cache = self._inside_mask()
        x0, y0, x1, y1 = self.plan_box
        mg = 3 * self.k
        clip = np.zeros(self.thin.shape, bool)
        clip[max(0, y0 - mg):y1 + mg, max(0, x0 - mg):x1 + mg] = True
        m = (self.thin & clip & ~cv2.dilate(self.wall, np.ones((3, 3), np.uint8)).astype(bool)).astype(np.uint8)
        sk = skeleton(m)
        self.line_mask = m
        out = []
        for poly in skeleton_paths(sk):
            if len(poly) < 2:
                continue
            L = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(poly, poly[1:]))
            if L < 3:
                continue
            p = cv2.approxPolyDP(np.array(poly, np.float32).reshape(-1, 1, 2), 0.8, False).reshape(-1, 2)
            out.append([[round(self.X(x), 1), round(self.Y(y), 1)] for x, y in p])
            if len(out) >= max_items:
                break
        return out

    # -- debug image
    def debug(self, path):
        np, cv2 = _np(), _cv()
        im = (self.im * 0.35 + 255 * 0.65).astype(np.uint8)
        im[self.wall > 0] = (60, 60, 230)
        for p in self.partitions:
            a = (int(self.PX(p["u"] if p["o"] == "h" else p["c"])), int(self.PY(p["c"] if p["o"] == "h" else p["u"])))
            b = (int(self.PX(p["v"] if p["o"] == "h" else p["c"])), int(self.PY(p["c"] if p["o"] == "h" else p["v"])))
            cv2.line(im, a, b, (200, 0, 200), 3)
        col = {"door": (0, 160, 0), "glazed": (230, 140, 0), "opening": (0, 200, 230)}
        for gp in self.gaps:
            x, y = int(self.PX(gp["at"][0])), int(self.PY(gp["at"][1]))
            cv2.circle(im, (x, y), max(4, self.k // 2), col[gp["kind"]], -1)
        for r in self.rooms:
            pts = np.array([[int(self.PX(x)), int(self.PY(y))] for x, y in r["poly"]], np.int32)
            cv2.polylines(im, [pts], True, (255, 90, 0), 2)
        for c in self.chains.values():
            if c["side"] in ("top", "bottom"):
                cv2.line(im, (int(c["line_px"][0]), int(c["pos_px"])), (int(c["line_px"][1]), int(c["pos_px"])), (0, 0, 0), 3)
            else:
                cv2.line(im, (int(c["pos_px"]), int(c["line_px"][0])), (int(c["pos_px"]), int(c["line_px"][1])), (0, 0, 0), 3)
        ok, buf = cv2.imencode(".png", im)
        buf.tofile(str(path))
        return str(path)


def _py(o):
    """numpy scalars -> python, recursively (the spec is JSON)."""
    if isinstance(o, dict):
        return {k: _py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_py(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            return o.item()
        except Exception:  # noqa: BLE001
            return o
    return o


def trace(path, task=None, image_rel=None, hints=None, debug=None, furniture=True):
    tr = Trace(path, hints).run()
    sp = tr.spec(task=task, image_rel=image_rel)
    hw = tr.hollow_walls()
    if hw:
        # drawn as the source draws them (two lines) but on the wall layer, so the DXF/DWG knows they are walls
        sp["hollow_walls"] = [{"a": [h["u"], h["c"]] if h["o"] == "h" else [h["c"], h["u"]],
                               "b": [h["v"], h["c"]] if h["o"] == "h" else [h["c"], h["v"]], "t": h["t"]} for h in hw]
    if furniture:
        lines = tr.furniture_lines()
        if hw:
            def inside(p):
                for h in hw:
                    a, b = (p[0], p[1]) if h["o"] == "h" else (p[1], p[0])
                    if h["u"] - 30 <= a <= h["v"] + 30 and abs(b - h["c"]) <= 0.5 * h["t"] + 30:
                        return True
                return False
            lines = [pl for pl in lines if not all(inside(p) for p in pl)]
        sp["traced_lines"] = lines
    sp = _py(sp)
    if debug:
        tr.debug(debug)
    info = {"k": tr.k, "ink_threshold": tr.T, "mm_per_px": sp["source"]["mm_per_px"],
            "chains": {s: {"values": c["values"], "line_px": c["line_px"]} for s, c in tr.chains.items()},
            "walls": len(tr.segs), "partitions": len(tr.partitions),
            "gaps": {k: sum(1 for g in tr.gaps if g["kind"] == k) for k in ("door", "glazed", "opening")},
            "labels": [{"name": L["name"], "x": round(L["px"][0] / tr.f, 1), "y": round(L["px"][1] / tr.f, 1),
                        "by": "ocr", "room": L.get("region")} for L in tr.labels],
            "warnings": tr.warnings}
    return sp, info, tr


# ----------------------------------------------------------------------------------------------- verification

def _seg_of(e):
    """{"a": [x, y], "b": [x, y], "t": t} (spec mm, y down) -> {"o", "c", "u", "v", "t"}"""
    try:
        (ax, ay), (bx, by), t = e["a"], e["b"], float(e.get("t") or 100)
    except Exception:  # noqa: BLE001
        return None
    if abs(ay - by) < 1e-6:
        return {"o": "h", "c": ay, "u": min(ax, bx), "v": max(ax, bx), "t": t}
    if abs(ax - bx) < 1e-6:
        return {"o": "v", "c": ax, "u": min(ay, by), "v": max(ay, by), "t": t}
    return None


def verify(spec, eng, image=None, out_png=None, tol_px=None):
    """rasterise the GENERATED walls onto the source image and compare with the source's own wall pixels.

    errors (hard): wall precision < 0.93 or recall < 0.90; >= 0.5 m of hollow (double-line) wall the solid-wall check
    cannot see; < 90 % of the plan's line work drawn; an OCR room name missing / outside its room; drawn overall
    dimension != printed. Returns (errors, warnings, info, metrics)."""
    np, cv2 = _np(), _cv()
    src = spec.get("source") or {}
    image = image or src.get("image")
    E, W, I = [], [], []
    if not image or not os.path.exists(image) or not src.get("transform"):
        return E, ["verify: no source image / transform, the drawing was not compared with the source"], I, {}
    tr = Trace(image).run() if not getattr(eng, "_trace", None) else eng._trace
    T = src["transform"]
    f = tr.f
    m = eng.m
    H, Wd = tr.H, tr.W
    drawn = np.zeros((H, Wd), np.uint8)
    dx, dy = 0.0, 0.0      # wall_cells and room points are in model mm, before the sheet layout shift
    for c in getattr(eng, "wall_cells", []):
        xs, ys = [], []
        for X, Y in ((c[0], c[1]), (c[2], c[3])):
            X -= dx; Y -= dy
            yd = (m.H - Y) if m.y_down else Y
            xs.append((T["ax"] * X + T["bx"]) * f); ys.append((T["ay"] * yd + T["by"]) * f)
        x0, x1 = sorted(xs); y0, y1 = sorted(ys)
        cv2.rectangle(drawn, (int(round(x0)), int(round(y0))), (int(round(x1)) - 1, int(round(y1)) - 1), 1, -1)
    srcw = tr.wall
    tol = tol_px or max(2, int(round(30 / (tr.sx))))           # ~30 mm, at least 2 px
    ker = np.ones((2 * tol + 1, 2 * tol + 1), np.uint8)
    d_src = cv2.dilate(srcw, ker); d_drw = cv2.dilate(drawn, ker)
    dn, sn = int(drawn.sum()), int(srcw.sum())
    prec = float((drawn & d_src).sum()) / max(1, dn)
    rec = float((srcw & d_drw).sum()) / max(1, sn)
    metrics = {"wall_precision": round(prec, 3), "wall_recall": round(rec, 3), "tolerance_mm": round(tol * tr.sx)}
    P_MIN, R_MIN = float(os.environ.get("DRAWKIT_VERIFY_P", "0.93")), float(os.environ.get("DRAWKIT_VERIFY_R", "0.90"))
    # where it misses: connected pieces of source wall not covered, biggest first, in mm
    miss = (srcw & (1 - d_drw)).astype(np.uint8)
    extra = (drawn & (1 - d_src)).astype(np.uint8)

    def pieces(mask, what):
        n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
        out = []
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if a * tr.sx * tr.sy < 0.02e6:     # < 0.02 m² of wall
                continue
            out.append((a, f"{what} wall at x {round(tr.X(x))}..{round(tr.X(x + w))}, y {round(tr.Y(y))}..{round(tr.Y(y + h))} mm"))
        return [s for a, s in sorted(out, reverse=True)[:5]]
    if prec < P_MIN:
        E.append(f"verify: only {prec:.0%} of the drawn walls lie on walls of the source (need {P_MIN:.0%}): "
                 + "; ".join(pieces(extra, "extra")))
    if rec < R_MIN:
        E.append(f"verify: only {rec:.0%} of the source's walls are drawn (need {R_MIN:.0%}): " + "; ".join(pieces(miss, "missing")))
    if prec >= P_MIN and rec >= R_MIN:
        I.append(f"verify: walls coincide with the source (precision {prec:.0%}, recall {rec:.0%}, ±{metrics['tolerance_mm']} mm)")
    # labels: each OCR room name in the room of that name
    bad = []
    from drawkit import point_in_poly   # noqa: E402  (same folder)
    n_lab = 0
    for r in getattr(eng, "rooms", []):
        lp = r.get("label_px")
        if not lp:
            continue
        n_lab += 1
        X = (lp[0] - T["bx"]) / T["ax"]; Yd = (lp[1] - T["by"]) / T["ay"]
        P = (X, (m.H - Yd) if m.y_down else Yd)
        if not point_in_poly(P[0], P[1], r["pts"]):
            bad.append(r.get("name"))
    # every room name printed on the source is in the drawing
    drawn_names = [r.get("name") for r in getattr(eng, "rooms", [])] + [x.get("text") for x in spec.get("free_labels") or []]
    for L in getattr(tr, "labels", []):
        if L["name"] not in drawn_names:
            bad.append(L["name"] + " (missing)")
    metrics["labels_checked"] = n_lab
    if bad:
        E.append("verify: room names not inside the room of that name: " + ", ".join(bad))
    # overall dimensions as printed
    for c in spec.get("chains") or []:
        tot = sum(float(v) for v in c.get("values") or [])
        drawn_tot = sum(float(v) for v in (c.get("_drawn") or c.get("values") or []))
        if abs(tot - drawn_tot) > 1:
            E.append(f"verify: {c.get('side')} chain drawn {drawn_tot} but printed {tot}")
    metrics["doors"] = len(spec.get("doors") or []); metrics["windows"] = len(spec.get("windows") or [])
    # 3.9.2b: precision / recall above only compare SOLID walls (both sides use the same solid-wall mask). Two more
    # checks close the gap: (1) walls drawn hollow (two thin parallel lines joined to the solid walls) are found
    # and must not be silently turned into furniture lines; (2) every bit of line work inside the plan must be in
    # the drawing (walls + traced lines), so nothing the source shows is left out.
    found = tr.hollow_walls()
    given = [_seg_of(e) for e in spec.get("hollow_walls") or []]
    missing, total = [], 0.0
    for hw in found:
        total += hw["v"] - hw["u"]
        ok = any(g and g["o"] == hw["o"] and abs(g["c"] - hw["c"]) <= 0.5 * hw["t"]
                 and min(g["v"], hw["v"]) - max(g["u"], hw["u"]) >= 0.8 * (hw["v"] - hw["u"]) for g in given)
        if not ok:
            missing.append(hw)
    metrics["hollow_wall_mm"] = round(total)
    if missing:
        E.append(f"verify: {len(missing)} wall piece(s) drawn HOLLOW in the source (two thin lines continuing a solid "
                 "wall) are not in the drawing as walls: " + "; ".join(
                     (f"y {round(h['c'])}, x {round(h['u'])}..{round(h['v'])} mm" if h["o"] == "h" else
                      f"x {round(h['c'])}, y {round(h['u'])}..{round(h['v'])} mm") for h in missing[:5]))
    ink_src = ((tr.wall > 0) | (getattr(tr, "thin", tr.ink) > 0)).astype(np.uint8)
    x0, y0, x1, y1 = tr.plan_box
    mg = int(3 * tr.k)
    clip = np.zeros_like(ink_src); clip[max(0, y0 - mg):y1 + mg, max(0, x0 - mg):x1 + mg] = 1
    ink_src &= clip
    drawn_all = drawn.copy()
    for pl in spec.get("traced_lines") or []:
        pts = np.array([[(T["ax"] * X + T["bx"]) * f, (T["ay"] * Y + T["by"]) * f] for X, Y in pl], np.int32)
        if len(pts) >= 2:
            cv2.polylines(drawn_all, [pts.reshape(-1, 1, 2)], False, 1, 2)
    for e in spec.get("hollow_walls") or []:
        g = _seg_of(e)
        if not g:
            continue
        for side in (-0.5, 0.5):
            cc = g["c"] + side * g["t"]
            if g["o"] == "h":
                p0, p1 = (g["u"], cc), (g["v"], cc)
            else:
                p0, p1 = (cc, g["u"]), (cc, g["v"])
            q = [(int((T["ax"] * X + T["bx"]) * f), int((T["ay"] * Y + T["by"]) * f)) for X, Y in (p0, p1)]
            cv2.line(drawn_all, q[0], q[1], 1, 2)
    ink_cov = float((ink_src & cv2.dilate(drawn_all, ker)).sum()) / max(1, int(ink_src.sum()))
    metrics["ink_coverage"] = round(ink_cov, 3)
    INK_MIN = float(os.environ.get("DRAWKIT_VERIFY_INK", "0.90"))
    if ink_cov < INK_MIN:
        left = (ink_src & (1 - cv2.dilate(drawn_all, ker))).astype(np.uint8)
        E.append(f"verify: only {ink_cov:.0%} of the source's line work inside the plan is in the drawing (need "
                 f"{INK_MIN:.0%}): " + "; ".join(pieces(left, "undrawn")))
    if out_png:
        im = (tr.im * 0.25 + 255 * 0.75).astype(np.uint8)
        both = (drawn > 0) & (srcw > 0)
        im[(srcw > 0) & ~(d_drw > 0)] = (40, 40, 230)       # red: in the source, not drawn
        im[(drawn > 0) & ~(d_src > 0)] = (230, 120, 0)      # blue: drawn, not in the source
        im[both] = (40, 40, 40)
        x0, y0, x1, y1 = tr.plan_box
        mg = 40
        im = im[max(0, y0 - mg):y1 + mg, max(0, x0 - mg):x1 + mg]
        cv2.putText(im, f"P {prec:.0%}  R {rec:.0%}  red=missing blue=extra", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
        ok, buf = cv2.imencode(".png", im)
        buf.tofile(out_png)
        metrics["diff_png"] = out_png
    return E, W, I, metrics


if __name__ == "__main__":
    import sys
    sp, info, _ = trace(sys.argv[1], debug=sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(info, ensure_ascii=False, indent=1))
