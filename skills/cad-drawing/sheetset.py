#!/usr/bin/env python3
"""sheetset: one spec -> a numbered set of 施工图 sheets (v3.9.93, task-241).

spec kind "sheet_set":
{
 "kind": "sheet_set", "project": "升旗台广场改造工程", "source_note": "用户文字说明＋图1设计效果图＋图2现场照片",
 "sheets": [
   {"no": "JS-01", "title": "总平面及拆除平面图", "paper": "A3",
    "views": [{"title": "总平面图", "scale": 100,
               "entities": [ ...model mm, y up... ], "dims": [ ... ]}],
    "notes": ["..."], "legend": [{"sym": "tile", "text": "..."}],
    "table": {"title", "cols", "widths" (paper mm), "rows"}}
 ],
 "requirements": [{"sheet": "site_plan", "tag": "tree_remove", "count": 6, "what": "6 棵椰子树"}],
 "quantities": [...], "assumptions": [...], "conflicts": [...]
}

UNITS (the task-241 bug): geometry is in MODEL mm (27.9 m = 27900). Every size that is about the PAPER - text height
`h`, dimension `offset`, symbol sizes - is in PAPER mm (text 2.5-7). A view at 1:100 draws a 5 mm text 500 model mm
tall; nothing else converts. Absurd values are refused with a message instead of drawing an 80 m title.

Each sheet is composed in paper mm (views placed automatically, title under each view with its scale, notes column
with wrapping, legend, title block with 图名/图号/比例/日期/来源), checked (text overlap, out of frame, empty sheet,
units, required elements), rendered to PNG; all sheets go into one multi-page PDF and one DXF (sheets side by side,
each at its own scale so the main view is 1:1 model mm).

Entity types (model mm unless noted): line {a,b}, poly {points, closed}, rect {box}, circle {c,r}, arc {c,r,a1,a2},
centerline {a,b}, text {at, text, h (paper mm), rot, anchor}, hatch {pattern, box|points, holes:[box], size:[dx,dy]
(tile, model mm), origin}, tree {c, r, remove}, level {at, value, note, kind: plan|elev}, slope {at, dir (deg), text},
arrow {a, b, text}, north {at}, section_mark {a, b, label}, break {a, b}, baluster {x, y0, y1, w},
label {at, text, h, dir: up|down|left|right|free} (auto-placed with a leader), layers {at, items, title, side}
(构造做法 callout). Any entity may carry "tag" (counted by the requirement checks), "layer", "lw" (mm), "ls"
(dashed|dashdot|center), "fill" (0..1 grey fill of a closed shape).
dims: {a, b, value?, offset (paper mm, sign = side)} or {chain: [coords], axis: x|y, base (model coord of the
measured edge), offset, values?}.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drawkit as dk   # noqa: E402

PAPER = dk.PAPER
TEXT_MIN, TEXT_MAX = 1.8, 12.0       # paper mm; outside = unit mistake
MARG_L, MARG = 25.0, 10.0            # binding margin left, others
TB_W, TB_H = 180.0, 40.0
NOTES_W = 92.0
LS = {"dashed": (3.0, 1.5), "dashdot": (6.0, 1.2, 0.6, 1.2), "center": (8.0, 1.2, 1.5, 1.2)}
LAYERS = {"图框": 7, "文字": 7, "说明": 7, "尺寸": 3, "标注": 7, "构造": 7, "铺装": 8, "填充": 8, "绿化": 3, "现状": 8,
          "现状填充": 9, "拆除": 1, "排水": 5, "中心线": 1, "标高": 7, "索引": 7, "图形": 7}
COLORS = {"尺寸": (0, 110, 0), "现状": (120, 120, 120), "现状填充": (165, 165, 165), "填充": (110, 110, 110),
          "铺装": (120, 120, 120), "绿化": (40, 130, 40), "拆除": (200, 30, 30), "排水": (30, 90, 170), "中心线": (190, 40, 40)}


_GID = 0


class SetError(Exception):
    pass


# ============================================================================================ geometry helpers

def _clip_line(p, d, poly):
    """parameters t where the infinite line p + t d is inside the polygon (pairs)."""
    ts = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ex, ey = b[0] - a[0], b[1] - a[1]
        den = d[0] * ey - d[1] * ex
        if abs(den) < 1e-12:
            continue
        t = ((a[0] - p[0]) * ey - (a[1] - p[1]) * ex) / den
        u = ((a[0] - p[0]) * d[1] - (a[1] - p[1]) * d[0]) / den
        if 0 <= u < 1:
            ts.append(t)
    ts.sort()
    return [(ts[i], ts[i + 1]) for i in range(0, len(ts) - 1, 2)]


def _box_range(p, d, b):
    t0, t1 = -1e18, 1e18
    for k in (0, 1):
        lo, hi = b[k], b[k + 2]
        if abs(d[k]) < 1e-12:
            if not (lo <= p[k] <= hi):
                return None
        else:
            a1, a2 = (lo - p[k]) / d[k], (hi - p[k]) / d[k]
            t0, t1 = max(t0, min(a1, a2)), min(t1, max(a1, a2))
    return (t0, t1) if t0 < t1 else None


def _subtract(iv, cut):
    out = []
    for a, b in iv:
        c0, c1 = cut
        if c1 <= a or c0 >= b:
            out.append((a, b)); continue
        if c0 > a:
            out.append((a, c0))
        if c1 < b:
            out.append((c1, b))
    return out


def _pip(x, y, poly):
    return dk.point_in_poly(x, y, poly)


def _rand(*k):
    h = hashlib.md5(repr(k).encode()).digest()
    return h[0] / 255.0, h[1] / 255.0, h[2] / 255.0


# ============================================================================================ one view

class View:
    def __init__(self, v: dict, sheet_no: str):
        self.v = v
        self.s = float(v.get("scale") or 100)
        self.sh = dk.Sheet()
        self.errors, self.warns = [], []
        self.no = sheet_no
        self.title = v.get("title") or ""

    def P(self, p):
        return (float(p[0]) / self.s, float(p[1]) / self.s)

    def _tag(self, e, n0):
        global _GID
        _GID += 1
        for it in self.sh.items[n0:]:
            if e.get("tag"):
                it["tag"] = e["tag"]
                it["_g"] = _GID
            if e.get("ls"):
                it["ls"] = e["ls"]

    def _h(self, e, default):
        h = float(e.get("h", e.get("h_mm", default)))
        if not (TEXT_MIN <= h <= TEXT_MAX):
            self.errors.append(f"[{self.no} {self.title}] text '{str(e.get('text'))[:20]}' h={h:g}: h is the PRINTED height in "
                               f"PAPER mm ({TEXT_MIN:g}–{TEXT_MAX:g}, normally 2.5/3.5/5/7). At 1:{self.s:g} it is drawn "
                               f"{h * self.s:g} model mm tall. Do not convert it to model units yourself.")
            h = min(max(h, TEXT_MIN), 7.0)
        return h

    # ---------------------------------------------------------------- entities
    def build(self):
        sh = self.sh
        ents = self.v.get("entities") or []
        later = []
        for e in ents:
            t = e.get("type")
            if t in ("label", "layers"):
                later.append(e); continue
            n0 = len(sh.items)
            try:
                self._entity(e)
            except (KeyError, TypeError, ValueError, IndexError) as ex:
                self.errors.append(f"[{self.no} {self.title}] bad {t} entity {json.dumps(e, ensure_ascii=False)[:80]}: {type(ex).__name__} {ex}")
            self._tag(e, n0)
        self._units_check()
        for d in self.v.get("dims") or []:
            try:
                self._dim(d)
            except (KeyError, TypeError, ValueError, IndexError) as ex:
                self.errors.append(f"[{self.no} {self.title}] bad dim {json.dumps(d, ensure_ascii=False)[:80]}: {ex}")
        for e in later:
            n0 = len(sh.items)
            if e["type"] == "label":
                self._label(e)
            else:
                self._layers(e)
            self._tag(e, n0)
        return self

    def _units_check(self):
        g = self.sh.extents(geom_only=True)
        w, h = g[2] - g[0], g[3] - g[1]
        if self.sh.items and max(w, h) < 8:
            self.errors.append(f"[{self.no} {self.title}] the view is only {w:.1f}×{h:.1f} mm on paper at 1:{self.s:g}: coordinates "
                               "must be MODEL millimetres (27.9 m = 27900), not metres")
        self.paper_wh = (w, h)

    def _entity(self, e):
        sh, P, t = self.sh, self.P, e.get("type")
        L = e.get("layer", "构造"); lw = float(e.get("lw", 0.25))
        if t == "line":
            sh.line(P(e["a"]), P(e["b"]), L, lw)
        elif t == "centerline":
            sh.line(P(e["a"]), P(e["b"]), "中心线", 0.18); sh.items[-1]["ls"] = "center"
        elif t in ("poly", "polyline"):
            pts = [P(p) for p in e["points"]]
            if e.get("fill"):
                sh.fill(pts, "填充", float(e["fill"]))
            sh.poly(pts, bool(e.get("closed")), L, lw)
        elif t == "rect":
            x0, y0, x1, y1 = e["box"]
            pts = [P((x0, y0)), P((x1, y0)), P((x1, y1)), P((x0, y1))]
            if e.get("fill"):
                sh.fill(pts, "填充", float(e["fill"]))
            sh.poly(pts, True, L, lw)
        elif t == "circle":
            c = P(e["c"]); r = float(e["r"]) / self.s
            if e.get("fill"):
                sh.fill([(c[0] + r * math.cos(a / 8 * math.pi), c[1] + r * math.sin(a / 8 * math.pi)) for a in range(16)], "填充", float(e["fill"]))
            sh.circle(c, r, L, lw)
        elif t == "arc":
            sh.arc(P(e["c"]), float(e["r"]) / self.s, e["a1"], e["a2"], L, lw)
        elif t == "text":
            x, y = P(e["at"])
            h = self._h(e, 3.0)
            sh.text(x, y, e["text"], h, e.get("layer", "文字"), e.get("anchor", "mc"), rot=e.get("rot", 0) or 0, role="text")
        elif t == "hatch":
            self._hatch(e)
        elif t == "tree":
            c = P(e["c"]); r = float(e.get("r", 1500)) / self.s
            sh.circle(c, r, "现状" if e.get("remove") else "绿化", 0.25)
            for k in range(8):
                a = k * math.pi / 4 + 0.2
                sh.line((c[0] + 0.25 * r * math.cos(a), c[1] + 0.25 * r * math.sin(a)), (c[0] + 0.9 * r * math.cos(a), c[1] + 0.9 * r * math.sin(a)),
                        "现状" if e.get("remove") else "绿化", 0.18)
            if e.get("remove"):
                d = r * 0.85
                sh.line((c[0] - d, c[1] - d), (c[0] + d, c[1] + d), "拆除", 0.5)
                sh.line((c[0] - d, c[1] + d), (c[0] + d, c[1] - d), "拆除", 0.5)
        elif t == "level":
            self._level(P(e["at"]), str(e["value"]), e.get("note"), e.get("kind", "plan"))
        elif t == "slope":
            x, y = P(e["at"]); a = math.radians(float(e.get("dir", 0))); Ln = float(e.get("len", 14))
            b = (x + Ln * math.cos(a), y + Ln * math.sin(a))
            self._arrow((x, y), b, "标注")
            mx, my = (x + b[0]) / 2, (y + b[1]) / 2
            if abs(math.sin(a)) < 0.5:
                sh.text(mx, my + 2.4, e.get("text", ""), 2.5, "标注", "mc", role="text")
            else:
                sh.text(mx + 2.5, my, e.get("text", ""), 2.5, "标注", "ml", role="text")
        elif t == "arrow":
            a, b = P(e["a"]), P(e["b"])
            self._arrow(a, b, e.get("layer", "标注"))
            if e.get("text"):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                if abs(a[0] - b[0]) < abs(a[1] - b[1]):
                    tw = dk.text_width(e["text"], 2.5)
                    sh.text(mx + (1.8 if e.get("side") != "out" else -1.8 - tw), my, e["text"], 2.5, "标注", "ml", role="text")
                else:
                    sh.text(mx, my + 2.4, e["text"], 2.5, "标注", "mc", role="text")
        elif t == "north":
            x, y = P(e["at"]); r = float(e.get("r", 8))
            sh.circle((x, y), r, "标注", 0.25)
            sh.fill([(x, y + r), (x - r * 0.35, y - r), (x, y - r * 0.55)], "填充", 0.9)
            sh.poly([(x, y + r), (x - r * 0.35, y - r), (x, y - r * 0.55), (x + r * 0.35, y - r), (x, y + r)], True, "标注", 0.25)
            sh.text(x, y + r + 3.2, "北", 3.5, "标注", "mc", role="text")
        elif t == "section_mark":
            a, b = P(e["a"]), P(e["b"])
            dx, dy = b[0] - a[0], b[1] - a[1]; Ln = math.hypot(dx, dy) or 1
            ux, uy = dx / Ln, dy / Ln
            px, py = uy, -ux       # looking to the right of a->b
            for q, s in ((a, 1), (b, -1)):
                q2 = (q[0] + s * ux * 7, q[1] + s * uy * 7)
                sh.line(q, q2, "索引", 0.7)
                sh.line(q, (q[0] + px * 4.5, q[1] + py * 4.5), "索引", 0.7)
                sh.text(q[0] + px * 4.5 + (2.2 if px >= 0 else -2.2), q[1] + py * 4.5 + 2.6 * (1 if py >= 0 else -1) + (0 if abs(py) > 0.5 else 2.6),
                        str(e.get("label", "1")), 3.5, "索引", "mc", role="text")
        elif t == "break":
            a, b = P(e["a"]), P(e["b"])
            m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            sh.poly([a, (m[0] - 1.2, m[1]), (m[0] - 0.6, m[1] + 2.0), (m[0] + 0.6, m[1] - 2.0), (m[0] + 1.2, m[1]), b], False, "构造", 0.18)
        elif t == "baluster":
            x = float(e["x"]); y0, y1 = float(e["y0"]), float(e["y1"]); w = float(e.get("w", 100))
            H = y1 - y0
            prof = [(0.35, 0), (0.35, 0.08), (0.2, 0.14), (0.5, 0.45), (0.22, 0.78), (0.32, 0.86), (0.32, 1.0)]
            right = [P((x + w * a, y0 + H * f)) for a, f in prof]
            left = [P((x - w * a, y0 + H * f)) for a, f in prof][::-1]
            sh.poly(right + left, True, e.get("layer", "构造"), 0.18)
        else:
            raise ValueError(f"unknown entity type {t}")

    def _arrow(self, a, b, layer):
        self.sh.line(a, b, layer, 0.25)
        dx, dy = b[0] - a[0], b[1] - a[1]; Ln = math.hypot(dx, dy) or 1
        ux, uy = dx / Ln, dy / Ln
        hl, hw = 2.6, 0.8
        p1 = (b[0] - ux * hl + uy * hw, b[1] - uy * hl - ux * hw)
        p2 = (b[0] - ux * hl - uy * hw, b[1] - uy * hl + ux * hw)
        self.sh.fill([b, p1, p2], "填充", 0.9)
        self.sh.poly([b, p1, p2], True, layer, 0.18)

    def _level(self, p, value, note=None, kind="plan"):
        sh = self.sh
        x, y = p
        h = 2.6
        sh.poly([(x, y), (x - h, y + h), (x + h, y + h)], True, "标高", 0.25)
        Ln = max(12.0, dk.text_width(value, 2.5) + 1.5)
        sh.line((x - h, y + h), (x - h + Ln + h, y + h), "标高", 0.25)
        sh.text(x - h + 0.6, y + h + 0.8, value, 2.5, "标高", "bl", role="text")
        if note:
            sh.text(x - h + Ln + h + 1.2, y + h + 0.8, note, 2.5, "标高", "bl", role="text")

    # ---------------------------------------------------------------- hatch
    def _hatch(self, e):
        sh = self.sh
        if e.get("box"):
            x0, y0, x1, y1 = e["box"]
            poly = [self.P((x0, y0)), self.P((x1, y0)), self.P((x1, y1)), self.P((x0, y1))]
        else:
            poly = [self.P(p) for p in e["points"]]
        holes = [[v / self.s for v in hb] for hb in e.get("holes") or []]
        pat = e.get("pattern", "ansi")
        layer = e.get("layer", "填充")
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        bx = (min(xs), min(ys), max(xs), max(ys))
        diag = math.hypot(bx[2] - bx[0], bx[3] - bx[1])

        def family(angle, spacing, offset=0.0, lw=0.13, dash=None):
            a = math.radians(angle)
            d = (math.cos(a), math.sin(a)); nrm = (-d[1], d[0])
            c = ((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2)
            k0 = int(math.floor(-diag / 2 / spacing)) - 1
            for k in range(k0, -k0 + 1):
                off = k * spacing + offset
                p = (c[0] + nrm[0] * off, c[1] + nrm[1] * off)
                iv = _clip_line(p, d, poly)
                for hb in holes:
                    r = _box_range(p, d, hb)
                    if r:
                        iv = _subtract(iv, r)
                for t0, t1 in iv:
                    if t1 - t0 < 0.05:
                        continue
                    a_ = (p[0] + d[0] * t0, p[1] + d[1] * t0); b_ = (p[0] + d[0] * t1, p[1] + d[1] * t1)
                    sh.line(a_, b_, layer, lw)
                    if dash:
                        sh.items[-1]["ls"] = dash

        def points(spacing, jitter=0.6):
            out = []
            nx, ny = int((bx[2] - bx[0]) / spacing) + 1, int((bx[3] - bx[1]) / spacing) + 1
            if nx * ny > 40000:
                spacing *= math.sqrt(nx * ny / 40000.0)
                nx, ny = int((bx[2] - bx[0]) / spacing) + 1, int((bx[3] - bx[1]) / spacing) + 1
            for i in range(nx):
                for j in range(ny):
                    r1, r2, r3 = _rand(self.no, self.title, i, j, pat)
                    x = bx[0] + (i + 0.5 + (r1 - 0.5) * jitter) * spacing
                    y = bx[1] + (j + 0.5 + (r2 - 0.5) * jitter) * spacing
                    if _pip(x, y, poly) and not any(hb[0] <= x <= hb[2] and hb[1] <= y <= hb[3] for hb in holes):
                        out.append((x, y, r3))
            return out

        if pat == "tile":
            tx, ty = (e.get("size") or [600, 600])
            ox, oy = e.get("origin") or [0, 0]
            sx, sy = tx / self.s, ty / self.s
            ox, oy = ox / self.s, oy / self.s
            for vert, step, o in ((True, sx, ox), (False, sy, oy)):
                k0 = int(math.floor(((bx[0] if vert else bx[1]) - o) / step))
                k1 = int(math.ceil(((bx[2] if vert else bx[3]) - o) / step))
                for k in range(k0, k1 + 1):
                    v = o + k * step
                    p = (v, bx[1] - 1) if vert else (bx[0] - 1, v)
                    d = (0, 1) if vert else (1, 0)
                    iv = _clip_line(p, d, poly)
                    for hb in holes:
                        r = _box_range(p, d, hb)
                        if r:
                            iv = _subtract(iv, r)
                    for t0, t1 in iv:
                        if t1 - t0 > 0.05:
                            sh.line((p[0] + d[0] * t0, p[1] + d[1] * t0), (p[0] + d[0] * t1, p[1] + d[1] * t1), layer, 0.09)
        elif pat in ("ansi", "ansi31", "stone"):
            family(45, 2.2 if pat != "stone" else 2.6)
            if pat == "stone":
                family(45, 2.6, 0.9, 0.09)
        elif pat == "earth":
            family(45, 1.6, 0, 0.13, "dashed")
            family(-45, 3.2, 0, 0.09, "dashed")
        elif pat == "concrete":
            for (x, y, r) in points(1.8):
                if r < 0.18:
                    s = 0.5
                    sh.poly([(x, y + s), (x - s * 0.9, y - s * 0.6), (x + s * 0.9, y - s * 0.6)], True, layer, 0.09)
                else:
                    sh.circle((x, y), 0.12, layer, 0.13)
        elif pat == "mortar":
            for (x, y, r) in points(0.9, 0.8):
                sh.circle((x, y), 0.08, layer, 0.09)
        elif pat == "gravel":
            for (x, y, r) in points(1.6, 0.8):
                sh.circle((x, y), 0.3 + 0.3 * r, layer, 0.13)
        elif pat in ("grass", "grass_section"):
            sp = float(e.get("spacing") or (4.5 if pat == "grass" else 2.4))
            for (x, y, r) in points(sp, 0.9):
                s = 0.9 if pat == "grass" else 0.7
                for dxp in (-0.5, 0.0, 0.5):
                    sh.line((x + dxp * s, y), (x + dxp * s * 1.8, y + s * (1.3 if dxp == 0 else 1.0)), layer, 0.18)
        elif pat == "water":
            for (x, y, r) in points(4.0, 0.5):
                sh.poly([(x - 1.2, y), (x - 0.6, y + 0.35), (x, y), (x + 0.6, y - 0.35), (x + 1.2, y)], False, layer, 0.13)
        elif pat == "grate":
            long_x = (bx[2] - bx[0]) > (bx[3] - bx[1])
            family(90 if long_x else 0, 0.9, 0, 0.09)
        elif pat in ("solid", "tile_section"):
            sh.fill(poly, layer, float(e.get("shade", 0.45 if pat == "tile_section" else 0.3)))
        else:
            self.errors.append(f"[{self.no} {self.title}] unknown hatch pattern '{pat}' (tile, stone, earth, concrete, mortar, gravel, "
                               "grass, grass_section, water, grate, solid, ansi)")

    # ---------------------------------------------------------------- dims
    def _dim(self, d):
        sh = self.sh
        th = 2.5
        if "chain" in d:
            pos = [float(v) for v in d["chain"]]
            axis = d.get("axis", "x")
            base = float(d.get("base", 0))
            vals = d.get("values") or [abs(b - a) for a, b in zip(pos, pos[1:])]
            off = float(d.get("offset", -8))
        else:
            a, b = d["a"], d["b"]
            horiz = abs(a[1] - b[1]) <= abs(a[0] - b[0]) if "axis" not in d else d["axis"] == "x"
            axis = "x" if horiz else "y"
            pos = sorted([float(a[0]), float(b[0])]) if horiz else sorted([float(a[1]), float(b[1])])
            base = float(a[1]) if horiz else float(a[0])
            vals = [d.get("value", abs(pos[1] - pos[0]))]
            off = float(d.get("offset", -8))
        if abs(off) > 80:
            self.errors.append(f"[{self.no} {self.title}] dimension offset {off:g}: offset is PAPER mm (5–30), not model mm")
            off = math.copysign(10, off)
        P = [p / self.s for p in pos]
        b0 = base / self.s
        row = b0 + off
        side = 1 if off > 0 else -1
        tick = 1.2
        if axis == "x":
            sh.line((P[0] - 1.5, row), (P[-1] + 1.5, row), "尺寸", 0.13)
            for p in P:
                sh.line((p, b0 + side * 1.5), (p, row + side * 1.5), "尺寸", 0.13)
                sh.line((p - tick, row - tick), (p + tick, row + tick), "尺寸", 0.35)
        else:
            sh.line((row, P[0] - 1.5), (row, P[-1] + 1.5), "尺寸", 0.13)
            for p in P:
                sh.line((b0 + side * 1.5, p), (row + side * 1.5, p), "尺寸", 0.13)
                sh.line((row - tick, p - tick), (row + tick, p + tick), "尺寸", 0.35)
        for i, v in enumerate(vals):
            a, b = P[i], P[i + 1]
            s = str(int(round(float(v)))) if abs(float(v) - round(float(v))) < 1e-6 else f"{float(v):g}"
            mid = (a + b) / 2
            seg = abs(b - a)
            w = dk.text_width(s, th)
            placed = None
            cands = [(mid, 0)]
            if w > seg - 0.6:
                cands += [(b + w / 2 + 1.2, 0), (a - w / 2 - 1.2, 0), (mid, 1), (mid, 2)]
            else:
                cands += [(mid, 1)]
            for m_, lift in cands:
                if axis == "x":
                    y = row + 1.0 + th * 0.6 + lift * th * 1.3
                    box = dk.Sheet.tbox(m_, y, th, s)
                else:
                    x = row - 1.0 - th * 0.6 - lift * th * 1.3
                    box = dk.Sheet.tbox(x, m_, th, s, rot=90)
                if sh.free(box, 0.3):
                    placed = (m_, lift); break
            if placed is None:
                placed = cands[0]
            m_, lift = placed
            if axis == "x":
                sh.text(m_, row + 1.0 + th * 0.6 + lift * th * 1.3, s, th, "尺寸", role="dim")
            else:
                sh.text(row - 1.0 - th * 0.6 - lift * th * 1.3, m_, s, th, "尺寸", rot=90, role="dim")
            sh.items[-1]["dimval"] = float(v)
            sh.texts[-1]["dimval"] = float(v)

    # ---------------------------------------------------------------- labels (auto placed)
    def _label(self, e):
        sh = self.sh
        h = self._h(e, 3.0)
        x, y = self.P(e["at"])
        s = str(e["text"])
        w = dk.text_width(s, h)
        pref = e.get("dir", "free")
        dirs = {"up": [(0, 1), (1, 1), (-1, 1)], "down": [(0, -1), (1, -1), (-1, -1)], "left": [(-1, 0), (-1, 1), (-1, -1)],
                "right": [(1, 0), (1, 1), (1, -1)]}.get(pref, [])
        dirs += [(1, 1), (-1, 1), (1, -1), (-1, -1), (0, 1), (0, -1), (1, 0), (-1, 0)]
        best = None
        for dist in (7, 10, 14, 19, 25, 33, 42):
            for ddx, ddy in dirs:
                ex, ey = x + ddx * dist * (0.9 if ddy else 1), y + ddy * dist * (0.75 if ddx else 1)
                if ddx >= 0:
                    bx0 = ex if ddx > 0 else ex - w / 2
                else:
                    bx0 = ex - w
                box = (bx0, ey + 0.6, bx0 + w, ey + 0.6 + h * 1.15)
                if sh.free(box, 0.8) and self._clear_strong(box):
                    best = (ex, ey, bx0, box); break
            if best:
                break
        if not best:
            self.warns.append(f"[{self.no} {self.title}] label '{s[:16]}' has no free place near its point")
            ex, ey = x + 8, y + 8
            best = (ex, ey, ex, (ex, ey + 0.6, ex + w, ey + 0.6 + h * 1.15))
        ex, ey, bx0, box = best
        sh.circle((x, y), 0.35, "标注", 0.35)
        sh.fill([(x + 0.35 * math.cos(a / 4 * math.pi), y + 0.35 * math.sin(a / 4 * math.pi)) for a in range(8)], "填充", 0.9)
        near = bx0 if abs(bx0 - x) < abs(bx0 + w - x) else bx0 + w
        sh.line((x, y), (near, ey), "标注", 0.18)
        sh.line((bx0, ey), (bx0 + w, ey), "标注", 0.18)
        sh.text(bx0, ey + 0.6, s, h, "文字", "bl", role="label")

    def _clear_strong(self, box):
        """a label must not sit on the outline of a main element (thick construction / existing lines); hatch, dims
        and thin lines may run under it (the text is masked)."""
        for it in self.sh.items:
            if it["t"] in ("line", "poly") and it.get("lw", 0) >= 0.35 and it["layer"] in ("构造", "铺装", "现状", "排水", "绿化"):
                pts = it["pts"] + ([it["pts"][0]] if it.get("closed") else [])
                if any(dk.seg_hits_box(p, q, box) for p, q in zip(pts, pts[1:])):
                    return False
        return True

    def _layers(self, e):
        """构造做法 callout: leader from the point, the build-up layers listed top-down, each on its own line."""
        sh = self.sh
        h = self._h(e, 2.5)
        x, y = self.P(e["at"])
        items = [str(i) for i in e.get("items") or []]
        title = e.get("title")
        lines = ([title] if title else []) + items
        w = max(dk.text_width(s, h) for s in lines) + 1
        step = h * 1.75
        tot = step * len(lines)
        side = e.get("side", "right")
        best = None
        for dx in ((8, 14, 22, 32) if side == "right" else (-8, -14, -22, -32)):
            for dy in (10, 16, 24, 34, 46, -10, -24):
                x0 = x + dx if dx > 0 else x + dx - w
                y_top = y + dy + tot
                box = (x0 - 0.5, y + dy - 0.5, x0 + w, y_top + 0.5)
                if sh.free(box, 0.6) and self._clear_strong(box):
                    best = (x0, y + dy, y_top); break
            if best:
                break
        if not best:
            self.warns.append(f"[{self.no} {self.title}] build-up callout at {e['at']} has no free place")
            best = (x + 10, y + 10, y + 10 + tot)
        x0, yb, yt = best
        sh.circle((x, y), 0.4, "标注", 0.35)
        ax = x0 if x0 > x else x0 + w
        sh.line((x, y), (ax, yb), "标注", 0.18)
        sh.line((ax, yb), (ax, yt), "标注", 0.18)
        yy = yt
        for k, s in enumerate(lines):
            yy -= step
            sh.line((x0, yy), (x0 + w, yy), "标注", 0.13)
            sh.text(x0 + 0.5, yy + 0.5, s, h, "文字", "bl", role="label")


# ============================================================================================ sheet composition

class SheetBuild:
    def __init__(self, spec, s, idx, total):
        self.spec, self.s, self.idx, self.total = spec, s, idx, total
        self.no = s.get("no") or f"S-{idx:02d}"
        self.title = s.get("title") or ""
        self.paper = s.get("paper") or "A3"
        self.errors, self.warns, self.info = [], [], []

    def compose(self):
        pw, ph = PAPER.get(self.paper, PAPER["A3"])
        sh = dk.Sheet()
        self.sh = sh
        self.W, self.H = pw, ph
        inner = (MARG_L, MARG, pw - MARG, ph - MARG)
        self.inner = inner
        sh.rect((0, 0, pw, ph), "图框", 0.18)
        sh.rect(inner, "图框", 0.7)
        self._title_block()
        has_side = bool(self.s.get("notes") or self.s.get("legend")) and self.s.get("layout") != "cover"
        right = inner[2] - (NOTES_W + 4 if has_side else 3)
        area = (inner[0] + 4, inner[1] + TB_H + 5, right, inner[3] - 4)
        if self.s.get("layout") == "cover":
            self._cover(area)
        else:
            y_top = area[3]
            if self.s.get("table"):
                y_top = self._table(self.s["table"], area[0], area[3], area[2] - area[0] if not has_side else None) - 6
            views = []
            for v in self.s.get("views") or []:
                vb = View(v, self.no).build()
                self.errors += vb.errors; self.warns += vb.warns
                views.append(vb)
            if views:
                self._pack(views, (area[0], area[1], area[2], y_top))
            if has_side:
                self._side(inner[2] - NOTES_W, inner[3] - 4, inner[1] + TB_H + 4)
        sh.frame = (0, 0, pw, ph)
        return self

    # ---------------------------------------------------------------- views
    def _measure(self, vb):
        ex = vb.sh.extents()
        tw = dk.text_width(vb.title, 5) + 4 + dk.text_width(f"1:{vb.s:g}", 3.5)
        w = max(ex[2] - ex[0], tw)
        return ex, w, (ex[3] - ex[1]) + 12

    def _pack(self, views, area):
        x0, y0, x1, y1 = area
        AW, AH = x1 - x0, y1 - y0
        ms = [self._measure(v) for v in views]
        # rows: greedy, in order
        rows, cur, cw = [], [], 0
        for i, (ex, w, h) in enumerate(ms):
            if cur and cw + w + 8 > AW:
                rows.append(cur); cur, cw = [], 0
            cur.append(i); cw += w + 8
        if cur:
            rows.append(cur)
        heights = [max(ms[i][2] for i in r) for r in rows]
        tot = sum(heights)
        if tot > AH + 0.5 or any(sum(ms[i][1] for i in r) + 8 * (len(r) - 1) > AW + 0.5 for r in rows):
            big = max(range(len(views)), key=lambda i: ms[i][1] * ms[i][2])
            v = views[big]
            self.errors.append(f"[{self.no} {self.title}] the views do not fit the {self.paper} drawing area "
                               f"({AW:.0f}×{AH:.0f} mm; they need ~{max(sum(ms[i][1] for i in r) for r in rows):.0f}×{tot:.0f}): "
                               f"use a smaller scale for '{v.title}' (now 1:{v.s:g}), split the sheet, or paper A2/A1")
        gap = max(0.0, (AH - tot) / (len(rows) + 1))
        y = y1 - gap
        for r, hrow in zip(rows, heights):
            wsum = sum(ms[i][1] for i in r)
            gx = max(8.0, (AW - wsum) / (len(r) + 1))
            x = x0 + gx
            for i in r:
                ex, w, h = ms[i]
                vb = views[i]
                # centre the view in its slot, top aligned in the row
                dx = x + (w - (ex[2] - ex[0])) / 2 - ex[0]
                dy = (y - (hrow - h) / 2) - ex[3]
                dk._shift(vb.sh, dx, dy)
                cx = x + w / 2
                ty = y - (hrow - h) / 2 - (ex[3] - ex[1]) - 6.5
                self._absorb(vb.sh)
                tw = dk.text_width(vb.title, 5)
                sw = dk.text_width(f"1:{vb.s:g}", 3.5)
                tx = cx - (tw + 3 + sw) / 2
                self.sh.text(tx, ty, vb.title, 5, "文字", "bl", role="title")
                self.sh.text(tx + tw + 3, ty, f"1:{vb.s:g}", 3.5, "文字", "bl", role="title")
                self.sh.line((tx, ty - 1.0), (tx + tw, ty - 1.0), "文字", 0.7)
                self.sh.line((tx, ty - 2.0), (tx + tw, ty - 2.0), "文字", 0.18)
                x += w + gx
            y -= hrow + gap
        self.scales = [v.s for v in views]

    def _absorb(self, other):
        self.sh.items += other.items
        self.sh.texts += other.texts

    # ---------------------------------------------------------------- side column: notes + legend
    def _side(self, x, y, ymin):
        sh = self.sh
        sh.line((x - 2, ymin), (x - 2, self.inner[3]), "图框", 0.25)
        notes = [n for n in self.s.get("notes") or [] if n]
        h = 3.0
        if notes:
            sh.text(x, y - 3, "说明", 5, "说明", "ml", role="notes"); y -= 9
            for i, n in enumerate(notes, 1):
                for k, part in enumerate(dk._wrap(f"{i}. {n}", h, NOTES_W - 11)):
                    sh.text(x + (0 if k == 0 else 3.5), y, part, h, "说明", "ml", role="notes"); y -= h * 1.6
                y -= 1.2
        leg = self.s.get("legend") or []
        if leg:
            y -= 3
            sh.text(x, y, "图例", 5, "说明", "ml", role="notes"); y -= 8
            for it in leg:
                self._legend_item(x, y, it)
                y -= 7.5
        if y < ymin:
            self.errors.append(f"[{self.no} {self.title}] the notes/legend column runs {ymin - y:.0f} mm into the title block: "
                               "shorten the notes or move some to the 设计说明 sheet")

    def _legend_item(self, x, y, it):
        sh = self.sh
        sym = it.get("sym")
        b = (x, y - 2.5, x + 12, y + 2.5)
        poly = [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
        tmp = View({"scale": 1, "title": "图例"}, self.no)
        if sym in ("tile", "grass", "grate", "stone", "earth", "concrete", "water"):
            e = {"type": "hatch", "pattern": sym, "points": [list(p) for p in poly], "size": [2, 2], "spacing": 2.2, "layer": "铺装" if sym == "tile" else
                 ("绿化" if sym == "grass" else "排水" if sym == "grate" else "填充")}
            tmp._hatch(e)
            tmp.sh.poly(poly, True, "构造", 0.25)
        elif sym == "tree_remove":
            tmp._entity({"type": "tree", "c": [x + 6, y], "r": 2.6, "remove": True})
        elif sym in ("dashed", "joint"):
            tmp.sh.line((x, y), (x + 12, y), "现状" if sym == "dashed" else "铺装", 0.35)
            tmp.sh.items[-1]["ls"] = "dashed" if sym == "dashed" else "dashdot"
        elif sym == "level":
            tmp._level((x + 4, y - 2), "+0.300")
        elif sym == "slope":
            tmp._arrow((x, y), (x + 12, y), "标注")
        else:
            tmp.sh.rect(b, "构造", 0.25)
        self._absorb_items(tmp.sh)
        sh.text(x + 16, y, it.get("text", ""), 3.0, "说明", "ml", role="notes")

    def _absorb_items(self, other):
        self.sh.items += [i for i in other.items if i["t"] != "text"]
        for t in other.texts:   # legend sample texts (the level value) stay in the legend
            self.sh.items.append(t); self.sh.texts.append(t)

    # ---------------------------------------------------------------- table
    def _table(self, tb, x, ytop, width=None):
        sh = self.sh
        cols, widths, rows = tb.get("cols") or [], [float(w) for w in tb.get("widths") or []], tb.get("rows") or []
        if not widths:
            widths = [(width or 200) / max(1, len(cols))] * len(cols)
        tw = sum(widths)
        if width and tw < width - 1 and tb.get("stretch", True):
            k = width / tw
            widths = [w * k for w in widths]; tw = width
        h = 3.0
        y = ytop
        if tb.get("title"):
            sh.text(x, y - 3, tb["title"], 5, "文字", "ml", role="title"); y -= 9
        top = y

        def row(cells, head=False):
            nonlocal y
            parts = [dk._wrap(str(c), h, w - 3) for c, w in zip(cells, widths)]
            n = max(1, max(len(p) for p in parts))
            rh = max(7.0 if head else 6.0, n * h * 1.45 + 2.6)
            xx = x
            for p, w in zip(parts, widths):
                for k, s in enumerate(p):
                    sh.text(xx + 1.5, y - 1.3 - h * 0.62 - k * h * 1.45, s, h, "文字", "ml", role="table")
                xx += w
            y -= rh
            sh.line((x, y), (x + tw, y), "图框", 0.5 if head else 0.18)
        sh.line((x, top), (x + tw, top), "图框", 0.5)
        row(cols, True)
        for r in rows:
            row(r)
        xx = x
        for w in [0] + widths:
            xx += w
            sh.line((xx, top), (xx, y), "图框", 0.5 if xx in (x, x + tw) else 0.18)
        sh.items[-1]["tag"] = "table"
        if y < self.inner[1] + TB_H + 2:
            self.errors.append(f"[{self.no} {self.title}] the table ({len(rows)} rows) runs into the title block: split it over two sheets")
        return y

    def _cover(self, area):
        x0, y0, x1, y1 = area
        x1 = self.inner[2] - 4
        tb = self.s["table"]
        tw = sum(float(w) for w in tb.get("widths") or [150])
        y = self._table(tb, x0, y1, None)
        colx = x0 + tw + 12
        colw = x1 - colx
        h = 3.0
        yy = y1

        def block(title, items, x, y, w):
            self.sh.text(x, y - 3, title, 5, "文字", "ml", role="title"); y -= 9
            for i, n in enumerate([n for n in items if n], 1):
                for k, part in enumerate(dk._wrap(f"{i}. {n}", h, w - 9)):
                    self.sh.text(x + (0 if k == 0 else 3.5), y, part, h, "说明", "ml", role="notes"); y -= h * 1.55
                y -= 1.0
            return y
        yy = block(self.s.get("notes_title") or "设计说明", self.s.get("notes") or [], colx, yy, colw)
        if self.s.get("notes2"):
            yy = block(self.s.get("notes2_title") or "假定及待确认", self.s.get("notes2"), colx, yy - 4, colw)
        if yy < self.inner[1] + TB_H + 2:
            # the right column is full: continue under the table
            self.errors.append(f"[{self.no} {self.title}] design notes overflow the sheet ({self.inner[1] + TB_H + 2 - yy:.0f} mm): shorten them")
        self.scales = []

    # ---------------------------------------------------------------- title block
    def _title_block(self):
        sh = self.sh
        x1, y0 = self.inner[2], self.inner[1]
        x0, y1 = x1 - TB_W, y0 + TB_H
        sh.rect((x0, y0, x1, y1), "图框", 0.5)
        views = self.s.get("views") or []
        scales = sorted({float(v.get("scale") or 100) for v in views})
        sc = "、".join(f"1:{s:g}" for s in scales) if scales else "—"
        src = self.spec.get("source_note") or "—"
        rows = [("工程名称", self.spec.get("project") or "", None, None),
                ("图名", self.title, "图号", self.no),
                ("比例", sc, "日期", time.strftime("%Y-%m-%d")),
                ("资料来源", src, None, None),
                ("说明", "AI 生成，须专业人员复核签字后使用", "张次", f"{self.idx + 1}/{self.total}")]
        rh = TB_H / len(rows)
        for i, (k, v, k2, v2) in enumerate(rows):
            yy = y1 - (i + 1) * rh
            if i:
                sh.line((x0, yy + rh), (x1, yy + rh), "图框", 0.18)
            hh = 3.0 if i != 1 else 3.5
            sh.text(x0 + 1.5, yy + rh / 2, k, 2.5, "文字", "ml", role="tb")
            vw = (TB_W - 22 - (38 if k2 else 2))
            vv = str(v)
            while dk.text_width(vv, hh) > vw - 2 and len(vv) > 4:
                vv = vv[:-2] + "…"
            sh.text(x0 + 21, yy + rh / 2, vv, hh, "文字", "ml", role="tb")
            if k2:
                sh.line((x1 - 38, yy), (x1 - 38, yy + rh), "图框", 0.18)
                sh.line((x1 - 26, yy), (x1 - 26, yy + rh), "图框", 0.18)
                sh.text(x1 - 37, yy + rh / 2, k2, 2.5, "文字", "ml", role="tb")
                sh.text(x1 - 25, yy + rh / 2, str(v2), 3.0, "文字", "ml", role="tb")
        sh.line((x0 + 19.5, y0), (x0 + 19.5, y1), "图框", 0.18)

    # ---------------------------------------------------------------- checks
    def check(self):
        sh = self.sh
        E, W = self.errors, self.warns
        T = sh.texts
        for t in T:
            if t["role"] in ("tb",):
                continue
            if not (TEXT_MIN - 1e-6 <= t["h"] <= TEXT_MAX):
                E.append(f"[{self.no} {self.title}] text '{t['s'][:20]}' is {t['h']:.1f} mm tall on paper (allowed {TEXT_MIN:g}–{TEXT_MAX:g})")
        n_ov = 0
        for i, a in enumerate(T):
            for b in T[i + 1:]:
                if dk.box_inter_area(dk._shrink(a["box"], 0.06), dk._shrink(b["box"], 0.06)) > 0:
                    n_ov += 1
                    if n_ov <= 12:
                        E.append(f"[{self.no} {self.title}] text overlap at ({a['box'][0]:.0f},{a['box'][1]:.0f}) mm: "
                                 f"'{a['s'][:18]}' / '{b['s'][:18]}'")
        if n_ov > 12:
            E.append(f"[{self.no} {self.title}] … {n_ov - 12} more text overlaps")
        fr = (0, 0, self.W, self.H)
        inner = self.inner
        out = []
        for it in sh.items:
            if it["layer"] == "图框":
                continue
            bb = _item_box(it)
            if bb and (bb[0] < inner[0] - 0.5 or bb[1] < inner[1] - 0.5 or bb[2] > inner[2] + 0.5 or bb[3] > inner[3] + 0.5):
                out.append(bb)
        if out:
            b = out[0]
            E.append(f"[{self.no} {self.title}] {len(out)} item(s) outside the drawing frame, e.g. at ({b[0]:.0f},{b[1]:.0f})–({b[2]:.0f},{b[3]:.0f}) mm")
        tbx = (inner[2] - TB_W, inner[1], inner[2], inner[1] + TB_H)
        hit = [it for it in sh.items if it.get("role") != "tb" and it["layer"] != "图框" and _item_box(it)
               and dk.box_overlap(_item_box(it), tbx, -0.5)]
        if hit:
            E.append(f"[{self.no} {self.title}] {len(hit)} item(s) drawn over the title block")
        geom = sum(1 for it in sh.items if it["t"] != "text" and it["layer"] != "图框")
        if geom < 12 and not self.s.get("table"):
            E.append(f"[{self.no} {self.title}] the sheet is (nearly) EMPTY: {geom} drawn items" +
                     (f" ({self.s.get('todo')})" if self.s.get("todo") else ""))
        self.counts = {"items": len(sh.items), "texts": len(T), "geometry": geom}
        return self


def _item_box(it):
    t = it["t"]
    if t in ("line", "poly", "fill"):
        xs = [p[0] for p in it["pts"]]; ys = [p[1] for p in it["pts"]]
        return (min(xs), min(ys), max(xs), max(ys))
    if t in ("circle", "arc"):
        return (it["c"][0] - it["r"], it["c"][1] - it["r"], it["c"][0] + it["r"], it["c"][1] + it["r"])
    if t == "text":
        return it["box"]
    return None


# ============================================================================================ requirements

def check_requirements(spec, built):
    """required elements per sheet (by the sheet's key): tagged item counts, dimension values, texts."""
    res, E = [], []
    by_key = {b.s.get("key"): b for b in built}
    for r in spec.get("requirements") or []:
        b = by_key.get(r.get("sheet"))
        rec = dict(r)
        if b is None:
            rec.update(ok=False, found="sheet missing")
            E.append(f"required: {r.get('what')} — the sheet '{r.get('sheet')}' is not in the set")
            res.append(rec); continue
        items = b.sh.items
        if r.get("tag"):
            tagged = [i for i in items if i.get("tag") == r["tag"]]
            # one entity can make several items (a tree: circle + spokes + cross): count entity groups by first item
            n = _groups(tagged)
            ok = (n == r["count"]) if "count" in r else n >= r.get("min", 1)
            rec.update(ok=ok, found=n)
        elif r.get("dim") is not None:
            vals = [t.get("dimval") for t in b.sh.texts if t.get("dimval") is not None]
            ok = any(abs(v - float(r["dim"])) < 0.5 for v in vals)
            rec.update(ok=ok, found=sorted({int(v) for v in vals})[:20])
        elif r.get("text"):
            ok = any(str(r["text"]) in t["s"] for t in b.sh.texts)
            rec.update(ok=ok, found=ok)
        else:
            rec.update(ok=True)
        if not rec["ok"]:
            E.append(f"[{b.no} {b.title}] required: {r.get('what')} — found {rec.get('found')}"
                     + (f", expected {r['count']}" if "count" in r else ""))
        res.append(rec)
    return res, E


def _groups(items):
    seen, n = set(), 0
    for it in items:
        g = it.get("_g")
        if g is None:
            n += 1
        elif g not in seen:
            seen.add(g); n += 1
    return n


# ============================================================================================ output

def _dash_items(items):
    """dashed / dash-dot lines split into segments (PNG only; the DXF uses real linetypes)."""
    out = []
    for it in items:
        ls = it.get("ls")
        if ls not in LS or it["t"] not in ("line", "poly"):
            out.append(it); continue
        pat = LS[ls]
        pts = it["pts"] + ([it["pts"][0]] if it.get("closed") else [])
        for a, b in zip(pts, pts[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1e-6:
                continue
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            t, k = 0.0, 0
            while t < L:
                seg = pat[k % len(pat)]
                if k % 2 == 0:
                    t2 = min(L, t + seg)
                    out.append({"t": "line", "pts": [(a[0] + ux * t, a[1] + uy * t), (a[0] + ux * t2, a[1] + uy * t2)],
                                "layer": it["layer"], "lw": it.get("lw", 0.18)})
                t += seg; k += 1
    return out


def render_png(sh, path, ppm=10.0):
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = sh.frame
    Wp, Hp = int((x1 - x0) * ppm) + 2, int((y1 - y0) * ppm) + 2
    im = Image.new("RGB", (Wp, Hp), "white")
    d = ImageDraw.Draw(im)
    T = lambda p: ((p[0] - x0) * ppm, (y1 - p[1]) * ppm)   # noqa: E731
    geo = [i for i in sh.items if i["t"] != "text"]
    fills = [i for i in geo if i["t"] == "fill"]
    lines = [i for i in geo if i["t"] != "fill"]
    _draw(d, fills, T, ppm)
    _draw(d, _dash_items(lines), T, ppm)
    for t in sh.texts:   # text mask: a white box under each label / note so hatch lines never cross the words
        if t.get("role") in ("label", "notes", "dim", "title", "table", "text"):
            b = t["box"]
            pad = 0.25
            if t.get("rot") in (90, 270, -90):
                pass
            d.rectangle([T((b[0] - pad, b[3] + pad)), T((b[2] + pad, b[1] - pad))], fill="white")
    _draw(d, [i for i in sh.items if i["t"] == "text"], T, ppm)
    im.save(path)
    return {"png": path, "size": [Wp, Hp]}


def _draw(d, items, T, ppm):
    cache = {}
    from PIL import Image as _I, ImageDraw as _D
    for it in items:
        t = it["t"]
        col = COLORS.get(it["layer"], (30, 30, 30))
        w = max(1, int(round(it.get("lw", 0.25) * ppm * 0.9)))
        if t == "fill":
            sh_ = float(it.get("shade", 0.3))
            g = int(255 - 225 * min(1.0, sh_))
            d.polygon([T(p) for p in it["pts"]], fill=(g, g, g))
        elif t == "line":
            d.line([T(p) for p in it["pts"]], fill=col, width=w)
        elif t == "poly":
            pts = [T(p) for p in it["pts"]] + ([T(it["pts"][0])] if it.get("closed") else [])
            d.line(pts, fill=col, width=w, joint="curve")
        elif t in ("arc", "circle"):
            cx, cy = T(it["c"]); r = it["r"] * ppm
            bb = [cx - r, cy - r, cx + r, cy + r]
            if r < 0.6:
                d.point((cx, cy), fill=col); continue
            if t == "circle":
                d.ellipse(bb, outline=col, width=w)
            else:
                d.arc(bb, -it["a2"], -it["a1"], fill=col, width=w)
        elif t == "text":
            size = it["h"] * ppm / 0.86
            key = int(size * 4)
            if key not in cache:
                cache[key] = dk._font(size)
            f = cache[key]
            x, y = T((it["x"], it["y"]))
            tc = (0, 100, 0) if it["layer"] == "尺寸" else (20, 20, 20)
            if it.get("rot") in (90, 270, -90):
                tw = int(d.textlength(it["s"], font=f)) + 4
                th = int(size * 1.3) + 4
                tile = _I.new("RGBA", (tw, th), (255, 255, 255, 0))
                _D.Draw(tile).text((2, th / 2), it["s"], font=f, fill=tc + (255,), anchor="lm")
                tile = tile.rotate(90, expand=True)
                d._image.paste(tile, (int(x - tile.width / 2), int(y - tile.height / 2)), tile)
            else:
                anc = {"mc": "mm", "ml": "lm", "bl": "ls", "tl": "lt"}.get(it["anchor"], "mm")
                d.text((x, y), it["s"], font=f, fill=tc, anchor=anc)


def write_dxf(built, path, view_font="notosc"):
    ezdxf = dk.need("ezdxf", "ezdxf", "python3-ezdxf")
    doc = ezdxf.new("R2010", setup=True)
    doc.units = 4
    try:
        doc.styles.add("CN", font="simhei.ttf")
    except Exception:  # noqa: BLE001
        pass
    for name, col in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=col)
    lt = {"dashed": "DASHED", "dashdot": "DASHDOT", "center": "CENTER"}
    msp = doc.modelspace()
    X = 0.0
    n = 0
    sheets_at = []
    from ezdxf.enums import TextEntityAlignment as A
    for b in built:
        S = float((b.s.get("views") or [{}])[0].get("scale") or 100)
        sheets_at.append({"no": b.no, "x": X, "scale": S})
        tf = lambda p: (X + p[0] * S, p[1] * S)   # noqa: E731
        for it in b.sh.items:
            att = {"layer": it["layer"] if it["layer"] in LAYERS else "图形"}
            lw = it.get("lw")
            if lw:
                att["lineweight"] = min((v for v in (9, 13, 18, 25, 35, 50, 70) if v >= int(lw * 100)), default=70)
            if it.get("ls") in lt:
                att["linetype"] = lt[it["ls"]]
                att["ltscale"] = S * 0.5 if hasattr(att, "get") else 1
            t = it["t"]
            if t == "line":
                msp.add_line(tf(it["pts"][0]), tf(it["pts"][1]), dxfattribs={k: v for k, v in att.items() if k != "ltscale"})
            elif t == "poly":
                msp.add_lwpolyline([tf(p) for p in it["pts"]], close=it.get("closed", False), dxfattribs={k: v for k, v in att.items() if k != "ltscale"})
            elif t == "fill":
                g = float(it.get("shade", 0.3))
                hb = msp.add_hatch(color=8 if g < 0.6 else 250, dxfattribs={"layer": "填充"})
                hb.paths.add_polyline_path([tf(p) for p in it["pts"]], is_closed=True)
            elif t == "circle":
                msp.add_circle(tf(it["c"]), it["r"] * S, dxfattribs={k: v for k, v in att.items() if k != "ltscale"})
            elif t == "arc":
                msp.add_arc(tf(it["c"]), it["r"] * S, it["a1"], it["a2"], dxfattribs={k: v for k, v in att.items() if k != "ltscale"})
            elif t == "text":
                e = msp.add_text(it["s"], height=it["h"] * S, rotation=it.get("rot", 0) or 0,
                                 dxfattribs={"layer": it["layer"] if it["layer"] in LAYERS else "文字", "style": "CN", "width": 0.8})
                al = {"mc": A.MIDDLE_CENTER, "ml": A.MIDDLE_LEFT, "bl": A.BOTTOM_LEFT, "tl": A.TOP_LEFT}.get(it["anchor"], A.MIDDLE_CENTER)
                e.set_placement(tf((it["x"], it["y"])), align=al)
            n += 1
        X += (b.W + 30) * S
    doc.header["$LTSCALE"] = 50
    doc.saveas(path)
    view = str(Path(path).with_name("." + Path(path).stem + ".view.dxf"))
    try:
        doc.styles.get("CN").dxf.font = view_font
        doc.saveas(view)
    except Exception:  # noqa: BLE001
        view = None
    return {"dxf": path, "view_dxf": view, "entities": n, "sheets_at": sheets_at}


_MEASURE = {}


def measured_width(s, h):
    """the real advance of the text in the Chinese font the PNG uses (+4 %), never less than the conservative estimate
    for CJK; task-241 notes ran past the frame because '→', '≤', '³' are full width in the font."""
    f = _MEASURE.get("font")
    if f is None:
        try:
            from PIL import ImageFont
            p = dk.cjk_font()
            f = ImageFont.truetype(p, 100) if p else False
        except Exception:  # noqa: BLE001
            f = False
        _MEASURE["font"] = f
    est = _EST(s, h)
    if not f:
        return est
    try:
        w = f.getlength(str(s)) / 100.0 * h / 0.86 * 1.04
    except Exception:  # noqa: BLE001
        return est
    return max(w, 0.9 * est) if any(dk.is_cjk(c) for c in str(s)) else w


_EST = dk.text_width


def build_set(spec, out_dir, name="drawings", dxf=True):
    dk.text_width = measured_width
    try:
        return _build_set(spec, out_dir, name, dxf)
    finally:
        dk.text_width = _EST


def _build_set(spec, out_dir, name="drawings", dxf=True):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = spec.get("sheets") or []
    E, W, I = [], [], []
    if not sheets:
        E.append("sheet_set has no sheets: plan the 图纸目录 first (sitework.py spec from cad/brief.json)")
    built = []
    for i, s in enumerate(sheets):
        b = None
        try:
            order = ["A3", "A2", "A1"]
            p0 = s.get("paper") or "A3"
            tries = order[order.index(p0):] if (p0 in order and not s.get("paper_fixed")) else [p0]
            for paper in tries:   # views that do not fit: the next paper size (A3 -> A2 -> A1), never a cramped sheet
                b = SheetBuild(spec, dict(s, paper=paper), i, len(sheets))
                b.compose()
                if not any("do not fit" in e or "runs into the title block" in e or "overflow" in e for e in b.errors):
                    break
            if b.paper != p0:
                I.append(f"[{b.no} {b.title}] paper {p0} -> {b.paper} so the views fit at their scales")
            b.check()
        except Exception as ex:  # noqa: BLE001
            if b is None:
                b = SheetBuild(spec, s, i, len(sheets))
            b.errors.append(f"[{b.no} {b.title}] could not be composed: {type(ex).__name__}: {ex}")
            if not hasattr(b, "sh"):
                b.sh = dk.Sheet(); b.sh.frame = (0, 0, 420, 297)
            b.counts = {}
        built.append(b)
    papers = {b.no: b.paper for b in built}
    for i, b in enumerate(built):   # the 图纸目录 shows the paper each sheet really got
        tb = b.s.get("table") or {}
        if b.s.get("layout") == "cover" and "图幅" in (tb.get("cols") or []):
            k = tb["cols"].index("图幅")
            rows = [list(r) for r in tb.get("rows") or []]
            changed = False
            for r in rows:
                no = next((c for c in r if c in papers), None)
                if no and r[k] != papers[no]:
                    r[k] = papers[no]; changed = True
            if changed:
                s2 = dict(b.s, table=dict(tb, rows=rows))
                nb = SheetBuild(spec, s2, i, len(sheets))
                nb.compose().check()
                built[i] = nb
    req, re_ = check_requirements(spec, built)
    for b in built:
        E += b.errors; W += b.warns
    E += re_
    for c in spec.get("conflicts") or []:
        W.append("资料矛盾: " + c)
    files = {"pngs": []}
    for b in built:
        p = out_dir / f"{name}-{b.no}.png"
        try:
            render_png(b.sh, str(p))
            files["pngs"].append(str(p))
        except Exception as ex:  # noqa: BLE001
            E.append(f"[{b.no}] PNG not rendered: {type(ex).__name__}: {ex}")
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        ims = [Image.open(p).convert("RGB") for p in files["pngs"]]
        if ims:
            ims[0].save(out_dir / f"{name}.pdf", "PDF", resolution=254.0, save_all=True, append_images=ims[1:])
            files["pdf"] = str(out_dir / f"{name}.pdf")
            files["pdf_pages"] = len(ims)
    except Exception as ex:  # noqa: BLE001
        E.append(f"PDF not written: {type(ex).__name__}: {ex}")
    if dxf:
        try:
            files.update(write_dxf(built, str(out_dir / f"{name}.dxf")))
        except Exception as ex:  # noqa: BLE001
            E.append(f"DXF not written: {type(ex).__name__}: {ex}")
        if files.get("dxf") and os.environ.get("DRAWKIT_DWG", "1") == "1":
            try:
                dw = dk.export_dwg(files["dxf"])
                if dw.get("dwg"):
                    files["dwg"] = dw["dwg"]
                elif dw.get("error"):
                    W.append("DWG not written: " + dw["error"])
            except Exception as ex:  # noqa: BLE001
                W.append(f"DWG not written: {ex}")
    rep = {"version": dk.VERSION, "kind": "sheet_set", "project": spec.get("project"),
           "sheets": [{"no": b.no, "key": b.s.get("key"), "title": b.title, "paper": b.paper,
                       "scales": [f"1:{s:g}" for s in getattr(b, "scales", [])], "errors": len(b.errors), "counts": getattr(b, "counts", {}),
                       "png": str(out_dir / f"{name}-{b.no}.png")} for b in built],
           "errors": dk._uniq(E), "warnings": dk._uniq(W), "info": I, "requirements": req,
           "quantities": spec.get("quantities") or [], "assumptions": spec.get("assumptions") or [],
           "conflicts": spec.get("conflicts") or [], "files": files, "scale": "见各图"}
    rep["ok"] = not rep["errors"]
    with open(out_dir / "report.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return rep


if __name__ == "__main__":
    sp = json.load(open(sys.argv[1], encoding="utf-8"))
    r = build_set(sp, sys.argv[2] if len(sys.argv) > 2 else "out", sys.argv[3] if len(sys.argv) > 3 else "drawings")
    print("DRAWKIT_RESULT " + json.dumps({k: r[k] for k in ("ok", "errors", "warnings", "files", "sheets")}, ensure_ascii=False))
