#!/usr/bin/env python3
"""sheetstyle: the 精排 (presentation) render of a 施工图 set (v3.9.96).

sheetset.py composes every sheet once (SheetBuild: items in paper mm + a side channel `sh.meta` with the hatch
regions, labels, view titles, tables, legend and title block). The plain PNG (render_png, used for VERIFY/核对) and
the DXF are drawn from that and do not change. This module draws the SAME geometry a second time as vector art:

  * 分区着色: light tints under hatch regions (by pattern / material) and under closed outlines whose label names a
    zone (广场/铺装 beige, 草坪 green, 泳池/水 blue, 篮球场 pink, 道路 grey, 升旗台 sand);
  * material hatches from Render a House PAT files (CC0, hatches/): 混凝土/垫层 -> RAH_CONCRETE, 碎石/级配碎石 ->
    RAH_GRAVEL, 素土/回填土/夯实土 -> RAH_EARTH, clipped to the region (holes cut out);
  * 双行引线: a label "名称（规格）" or a label with "spec" gets a name line and a small grey spec line under the shelf
    (only where the second line is free);
  * a blue title bar under every view (title + scale), navy header rows on tables (工程量表, 图纸目录),
    navy 说明/图例 headings with a framed legend, the standard title block (图号 in red);
  * the CJK font embedded (Noto Sans SC / Source Han / any font drawkit.cjk_font() finds).

Output: <name>-<no>.svg per sheet and one vector PDF. Dependency: matplotlib (installed through drawkit.need like the
other kit deps); when it is missing or anything fails the caller keeps the plain PDF.
"""
from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drawkit as dk   # noqa: E402

HATCH_DIR = Path(__file__).resolve().parent / "hatches"
PT = 72.0 / 25.4                     # points per paper mm

NAVY = "#1f3a6e"
BLUE = "#2a5caa"
BAR = "#e9f0fa"
GREY_TXT = "#6b7280"
RED = "#d0021b"

# layer colours (presentation)
LAYER_COL = {"构造": "#1b1b1b", "图框": "#1b1b1b", "尺寸": "#3d4451", "现状": "#8a8f98", "现状填充": "#a9adb3",
             "填充": "#737a85", "铺装": "#b8a37c", "绿化": "#3f8f3a", "拆除": "#d0312d", "排水": "#1f5fbf",
             "中心线": "#c0392b", "标高": RED, "索引": NAVY, "标注": "#333a45", "文字": "#1a1a1a", "说明": "#1a1a1a",
             "图形": "#1b1b1b"}

# tint per plain hatch pattern / RAH pattern
PAT_TINT = {"tile": "#f7eedb", "tile_section": "#f3e6cc", "stone": "#ecebe7", "grass": "#dcefcb", "grass_section": "#dcefcb",
            "water": "#c9e8fb", "grate": "#d5e6fa", "concrete": "#e8ebf0", "mortar": "#f3efe6", "gravel": "#eeeadf",
            "earth": "#f1dccf", "ansi": "#f0f0f0", "ansi31": "#f0f0f0", "solid": None,
            "RAH_CONCRETE": "#e8ebf0", "RAH_GRAVEL": "#eeeadf", "RAH_EARTH": "#f1dccf"}

# material words -> RAH pattern (order matters: 垫层 is concrete even when 碎石 is mentioned later)
MATERIAL_PAT = [(("混凝土", "砼", "垫层", "C15", "C20", "C25", "C30"), "RAH_CONCRETE"),
                (("级配碎石", "碎石", "砾石", "卵石", "石屑"), "RAH_GRAVEL"),
                (("素土", "回填土", "夯实土", "原土", "红土", "种植土", "土"), "RAH_EARTH")]
PLAIN_TO_RAH = {"concrete": "RAH_CONCRETE", "gravel": "RAH_GRAVEL", "earth": "RAH_EARTH"}
# pattern units (model mm in the PAT) -> paper mm: the repeat reads like the reference sheets at any view scale
PAT_PAPER = {"RAH_CONCRETE": 0.16, "RAH_GRAVEL": 0.11, "RAH_EARTH": 0.17}

# zone words in a label / text inside a closed outline -> tint
ZONE_TINT = [(("泳池", "水池", "水体", "水面"), "#cdeafb"), (("篮球场", "球场", "运动场"), "#f8dcd6"),
             (("草坪", "草坡", "绿化", "绿地", "斜坡"), "#dcefcb"), (("升旗台",), "#f6e3b8"),
             (("广场", "铺装", "瓷砖", "地砖"), "#f8efdc"), (("水泥路", "道路", "路面", "车道"), "#e6e7ea"),
             (("混凝土",), "#e8ebf0")]

TAG_TINT = {"plaza": "#f8efdc", "paving": "#f8efdc", "platform": "#f6e3b8", "slope": "#dcefcb", "lawn": "#dcefcb",
            "pool": "#cdeafb", "court": "#f8dcd6", "road": "#e6e7ea"}

LS_MM = {"dashed": (3.0, 1.5), "dashdot": (6.0, 1.2, 0.6, 1.2), "center": (8.0, 1.2, 1.5, 1.2)}


# ============================================================================================ PAT files

def parse_pat(text: str) -> dict:
    """AutoCAD .pat -> {NAME: {"desc", "lines": [(angle, x, y, dx, dy, [dashes])]}} (values as written, mm)."""
    pats, cur = {}, None
    for raw in text.splitlines():
        ln = raw.split(";", 1)[0].strip()
        if not ln:
            continue
        if ln.startswith("*"):
            name, _, desc = ln[1:].partition(",")
            cur = {"desc": desc.strip(), "lines": []}
            pats[name.strip().upper()] = cur
            continue
        if cur is None:
            continue
        try:
            v = [float(x) for x in ln.split(",") if x.strip()]
        except ValueError:
            continue
        if len(v) < 5:
            continue
        cur["lines"].append((v[0], v[1], v[2], v[3], v[4], v[5:]))
    return pats


_PATS: dict = {}


def load_pattern(name: str):
    name = name.upper()
    if name in _PATS:
        return _PATS[name]
    p = HATCH_DIR / f"{name}.pat"
    found = None
    for f in ([p] if p.exists() else []) + [HATCH_DIR / "architectural-metric.pat"]:
        if f.exists():
            found = parse_pat(f.read_text(encoding="utf-8", errors="replace")).get(name)
            if found:
                break
    _PATS[name] = found
    return found


def material_pattern(*words) -> str | None:
    """混凝土/垫层 -> RAH_CONCRETE, 碎石/级配碎石 -> RAH_GRAVEL, 素土/回填土/夯实土 -> RAH_EARTH; also the plain names."""
    for w in words:
        if not w:
            continue
        w = str(w)
        if w in PLAIN_TO_RAH:
            return PLAIN_TO_RAH[w]
        if w.upper().startswith("RAH_"):
            return w.upper()
        for keys, pat in MATERIAL_PAT:
            if any(k in w for k in keys):
                return pat
    return None


def _clip(p, d, poly):
    n = len(poly)
    ts = []
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


def _box_cut(p, d, b):
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


def _sub(iv, cut):
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


def pat_segments(pattern, poly, holes=(), k=1.0, origin=(0.0, 0.0), limit=60000):
    """the line families of a parsed PAT pattern, scaled by k (pattern unit -> paper mm), clipped to the polygon
    minus the hole boxes. -> (segments [((x0,y0),(x1,y1))], dots [(x,y)]). Dense results are coarsened."""
    if not pattern or not poly:
        return [], []
    xs = [q[0] for q in poly]; ys = [q[1] for q in poly]
    box = (min(xs), min(ys), max(xs), max(ys))
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    for _ in range(4):
        segs, dots, over = [], [], False
        for ang, x0, y0, dx, dy, dashes in pattern["lines"]:
            a = math.radians(ang)
            u = (math.cos(a), math.sin(a)); n = (-u[1], u[0])
            ox, oy = origin[0] + x0 * k, origin[1] + y0 * k
            sx, sy = dx * k, dy * k
            if abs(sy) < 1e-9:
                continue
            offs = [((c[0] - ox) * n[0] + (c[1] - oy) * n[1]) for c in corners]
            j0, j1 = int(math.floor(min(offs) / sy)) - 1, int(math.ceil(max(offs) / sy)) + 1
            if j0 > j1:
                j0, j1 = j1, j0
            ds = [abs(v) * k for v in dashes]
            sign = [v for v in dashes]
            L = sum(ds)
            for j in range(j0, j1 + 1):
                bx = ox + j * (sx * u[0] + sy * n[0])
                by = oy + j * (sx * u[1] + sy * n[1])
                iv = _clip((bx, by), u, poly)
                for hb in holes or ():
                    r = _box_cut((bx, by), u, hb)
                    if r:
                        iv = _sub(iv, r)
                for t0, t1 in iv:
                    if t1 - t0 < 1e-6:
                        continue
                    if not ds or L < 1e-9:
                        segs.append(((bx + u[0] * t0, by + u[1] * t0), (bx + u[0] * t1, by + u[1] * t1)))
                        continue
                    c = math.floor(t0 / L) * L
                    while c < t1:
                        t = c
                        for dl, sg in zip(ds, sign):
                            if sg > 0:
                                a_, b_ = max(t, t0), min(t + dl, t1)
                                if b_ - a_ > 1e-6:
                                    segs.append(((bx + u[0] * a_, by + u[1] * a_), (bx + u[0] * b_, by + u[1] * b_)))
                            elif sg == 0 and t0 <= t <= t1:
                                dots.append((bx + u[0] * t, by + u[1] * t))
                            t += dl
                        c += L
                        if len(segs) + len(dots) > limit:
                            over = True; break
                    if over:
                        break
                if over:
                    break
            if over:
                break
        if not over:
            return segs, dots
        k *= 1.6
    return segs, dots


# ============================================================================================ fonts

def _font_files():
    """(regular, bold) CJK font paths: env AIW_CJK_FONT(_BOLD), Noto Sans SC / CJK SC / Source Han, drawkit's font."""
    reg = os.environ.get("AIW_CJK_FONT") if os.environ.get("AIW_CJK_FONT") and os.path.exists(os.environ["AIW_CJK_FONT"]) else None
    cands = []
    for d in ("/usr/share/fonts", str(Path.home() / ".cache" / "aiw" / "fonts"), str(Path.home() / ".fonts"),
              str(Path.home() / ".local/share/fonts"), "C:/Windows/Fonts", "/System/Library/Fonts", "/Library/Fonts"):
        if os.path.isdir(d):
            for root, _dirs, files in os.walk(d):
                for f in files:
                    fl = f.lower()
                    if fl.endswith((".otf", ".ttf")) and any(k in fl for k in ("notosanssc", "notosanscjksc", "sourcehansanssc",
                                                                                 "sourcehansanscn", "notosanscjk-", "msyh", "simhei")):
                        cands.append(os.path.join(root, f))
    def pick(weights):
        for w in weights:
            for c in sorted(cands):
                b = os.path.basename(c).lower()
                if w in b and "mono" not in b and "serif" not in b:
                    return c
        return None
    reg = reg or pick(["sc-regular", "cn-regular", "sc-normal", "msyh.", "simhei"]) or dk.cjk_font() or None
    bold = os.environ.get("AIW_CJK_FONT_BOLD") or pick(["sc-bold", "cn-bold", "sc-medium", "cn-medium", "msyhbd"])
    if bold and not os.path.exists(bold):
        bold = None
    return reg, bold


# ============================================================================================ geometry helpers

def _pip(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _area(poly):
    return abs(sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
                   for i in range(len(poly)))) / 2


def _bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _inter(a, b, pad=0.0):
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


NOT_ZONE = ("排水", "沟", "井", "连接", "接口", "缝", "箅")


def zone_tint(text):
    """the zone a label / text names: the keyword that comes FIRST in the text ('广场—篮球场连接处' names the plaza);
    a label about a drain, joint or connection names no zone."""
    t = str(text)
    if any(k in t for k in NOT_ZONE):
        return None
    best = None
    for keys, col in ZONE_TINT:
        for k in keys:
            i = t.find(k)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, col)
    return best[1] if best else None


_SPLIT = re.compile(r"^(.{2,}?)\s*[（(]([^（）()]{2,})[）)]\s*$")


def split_label(text, spec=None):
    """'回填土后铺草坪（坡高 1600）' -> ('回填土后铺草坪', '坡高 1600'); an explicit spec wins."""
    if spec:
        return str(text), str(spec)
    m = _SPLIT.match(str(text))
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return str(text), None


# ============================================================================================ renderer

class Renderer:
    def __init__(self, mpl, fonts):
        self.mpl = mpl
        from matplotlib.font_manager import FontProperties
        reg, bold = fonts
        self.fp = FontProperties(fname=reg) if reg else FontProperties(family="sans-serif")
        self.fpb = FontProperties(fname=bold) if bold else None
        self.font_files = fonts

    # -------------------------------------------------------------- text
    def text(self, ax, x, y, s, h, color="#1a1a1a", anchor="mc", rot=0, bold=False, z=6):
        ha = {"mc": "center", "ml": "left", "bl": "left", "tl": "left", "mr": "right", "br": "right"}.get(anchor, "center")
        va = {"mc": "center", "ml": "center", "bl": "baseline", "tl": "top", "mr": "center", "br": "baseline"}.get(anchor, "center")
        fp = self.fpb if (bold and self.fpb) else self.fp
        size = h / 0.86 * PT
        kw = dict(fontproperties=fp, fontsize=size, color=color, ha=ha, va=va, rotation=rot or 0, rotation_mode="anchor")
        if bold and not self.fpb:
            # no bold CJK face: a stroked outline copy under the real text (the text itself stays searchable / copyable)
            from matplotlib import patheffects as pe
            u = ax.text(x, y, s, zorder=z - 0.01, **kw)
            u.set_path_effects([pe.Stroke(linewidth=max(0.25, h * 0.05 * PT), foreground=color)])
        return ax.text(x, y, s, zorder=z, **kw)

    # -------------------------------------------------------------- one sheet
    def sheet(self, b, fig_path_svg=None):
        import matplotlib.pyplot as plt
        from matplotlib.collections import LineCollection, PolyCollection
        from matplotlib.patches import Polygon as MPoly, Rectangle, PathPatch
        from matplotlib.path import Path as MPath
        sh = b.sh
        W, H = b.W, b.H
        fig = plt.figure(figsize=(W / 25.4, H / 25.4), dpi=72)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, W); ax.set_ylim(0, H); ax.set_aspect("equal"); ax.axis("off")
        meta = getattr(sh, "meta", None) or []
        self.warns = []

        tb = next((r for r in meta if r["k"] == "tb"), None)
        tbox = tb["box"] if tb else None
        texts_all = [t for t in sh.items if t["t"] == "text"]

        # ---- 1. zone tints: hatch regions + closed outlines named by a label / text inside them
        hatch_recs = [r for r in meta if r["k"] == "hatch"]
        for r in hatch_recs:
            pat = r["pattern"]
            rah = material_pattern(r.get("material")) or PLAIN_TO_RAH.get(pat)
            col = PAT_TINT.get(rah) if rah else PAT_TINT.get(pat)
            if col:
                ax.add_patch(MPoly(r["poly"], closed=True, facecolor=col, edgecolor="none", zorder=1))
        closed = []
        for it in sh.items:
            if it["t"] == "poly" and it.get("closed") and it["layer"] != "图框" and len(it["pts"]) >= 3 and it.get("_v"):
                a = _area(it["pts"])
                if a > 60:
                    closed.append((a, it))
        closed.sort(key=lambda q: q[0])
        probes = []   # (x, y, words)
        for r in meta:
            if r["k"] == "label":
                probes.append((r["at"][0], r["at"][1], r["text"]))
        for t in texts_all:
            if t.get("role") == "text" and t.get("_v"):
                probes.append((t["x"], t["y"], t["s"]))
        tinted = set()
        for a, it in closed:   # a tagged outline names its own zone (spec tags: plaza, platform, slope, lawn, pool...)
            col = TAG_TINT.get(str(it.get("tag") or ""))
            if col and not any(_bbox(h["poly"]) == _bbox(it["pts"]) for h in hatch_recs):
                ax.add_patch(MPoly(it["pts"], closed=True, facecolor=col, edgecolor="none", zorder=0.8 + 0.15 / (1 + a / 100.0)))
                tinted.add(id(it))
        for x, y, words in probes:
            col = zone_tint(words)
            if not col:
                continue
            for a, it in closed:
                if id(it) in tinted:
                    continue
                if _pip(x, y, it["pts"]):
                    hz = [h for h in hatch_recs if _bbox(h["poly"]) == _bbox(it["pts"])]
                    if not hz:   # a hatched region keeps its material tint
                        ax.add_patch(MPoly(it["pts"], closed=True, facecolor=col, edgecolor="none", zorder=0.8 + 0.15 / (1 + a / 100.0)))
                    tinted.add(id(it))
                    break

        # ---- 2. plain fills (grey), title-block / table / bar backgrounds
        for it in sh.items:
            if it["t"] == "fill":
                if it.get("_hz") is not None and it["_hz"]["pattern"] in ("solid", "tile_section"):
                    pat = it["_hz"]["pattern"]
                    col = "#e9d9b8" if pat == "tile_section" else "#c9ccd2"
                    ax.add_patch(MPoly(it["pts"], closed=True, facecolor=col, edgecolor="none", zorder=2))
                    continue
                g = float(it.get("shade", 0.3))
                if it["layer"] == "填充" and g >= 0.85:   # arrow heads, north arrow, label dots
                    col = LAYER_COL.get("标注")
                    lab = it.get("_lab")
                    if lab is not None:
                        col = NAVY
                else:
                    v = 1 - 0.88 * min(1.0, g)
                    col = (v, v, v)
                ax.add_patch(MPoly(it["pts"], closed=True, facecolor=col, edgecolor="none", zorder=2))

        # ---- 3. RAH material hatches (replace the plain concrete / gravel / earth items)
        replaced = set()
        for r in hatch_recs:
            rah = material_pattern(r.get("material")) or PLAIN_TO_RAH.get(r["pattern"])
            if not rah:
                continue
            pat = load_pattern(rah)
            if not pat:
                continue
            k = PAT_PAPER.get(rah, 0.15) * (0.55 if r.get("legend") else 1.0)
            segs, dots = pat_segments(pat, r["poly"], r.get("holes") or (), k)
            replaced.add(id(r))
            path = MPath(r["poly"] + [r["poly"][0]], closed=True)
            clip = PathPatch(path, transform=ax.transData, facecolor="none", edgecolor="none")
            ax.add_patch(clip)
            if segs:
                lc = LineCollection(segs, colors="#6f7684" if rah != "RAH_EARTH" else "#8c6d5c", linewidths=0.12 * PT,
                                    zorder=3, capstyle="round")
                lc.set_clip_path(clip)
                ax.add_collection(lc)
            if dots:
                lc = LineCollection([((x - 0.06, y), (x + 0.06, y)) for x, y in dots], colors="#6f7684",
                                    linewidths=0.25 * PT, zorder=3, capstyle="round")
                lc.set_clip_path(clip)
                ax.add_collection(lc)

        # ---- 4. lines
        groups = {}
        skip_t = set()
        for it in sh.items:
            t = it["t"]
            if t in ("text", "fill"):
                continue
            hz = it.get("_hz")
            if hz is not None and id(hz) in replaced:
                continue
            if it.get("_vt") is not None:          # plain view-title underline: replaced by the title bar
                continue
            if tbox and it["layer"] == "图框" and _inter(_item_box(it), tbox, -0.3) and _inside(_item_box(it), tbox):
                continue                              # plain title block grid: redrawn
            col = LAYER_COL.get(it["layer"], "#1b1b1b")
            if hz is not None:
                pat = hz["pattern"]
                col = {"tile": "#c2ad86", "grass": "#4f9a45", "grass_section": "#4f9a45", "water": "#5aa7d8",
                       "grate": "#2f6fc4", "stone": "#8f939a", "mortar": "#8f939a"}.get(pat, col)
            if it.get("_lab") is not None:
                col = "#333a45"
            elif it["layer"] == "铺装" and hz is None and float(it.get("lw", 0.25)) >= 0.35:
                col = "#3b3326"   # a paving outline, not the tile grid
            if it["layer"] == "标高" and t == "poly" and it.get("closed") and len(it["pts"]) == 3:
                ax.add_patch(MPoly(it["pts"], closed=True, facecolor=RED, edgecolor=RED, linewidth=0.15 * PT, zorder=4))
                continue
            lw = float(it.get("lw", 0.25))
            ls = it.get("ls") if it.get("ls") in LS_MM else None
            key = (col, round(lw, 3), ls)
            g = groups.setdefault(key, [])
            if t == "line":
                g.append(list(it["pts"]))
            elif t == "poly":
                g.append(list(it["pts"]) + ([it["pts"][0]] if it.get("closed") else []))
            elif t == "circle":
                c, rr = it["c"], it["r"]
                m = max(12, min(72, int(rr * 6)))
                g.append([(c[0] + rr * math.cos(2 * math.pi * i / m), c[1] + rr * math.sin(2 * math.pi * i / m)) for i in range(m + 1)])
            elif t == "arc":
                c, rr = it["c"], it["r"]
                a1, a2 = it["a1"], it["a2"]
                if a2 < a1:
                    a2 += 360
                m = max(6, int((a2 - a1) / 6))
                g.append([(c[0] + rr * math.cos(math.radians(a1 + (a2 - a1) * i / m)),
                           c[1] + rr * math.sin(math.radians(a1 + (a2 - a1) * i / m))) for i in range(m + 1)])
        for (col, lw, ls), paths in groups.items():
            kw = {}
            if ls:
                kw["linestyles"] = [(0, tuple(v * PT for v in LS_MM[ls]))]
            lc = LineCollection(paths, colors=col, linewidths=max(0.08, lw) * PT * 0.95, zorder=4, capstyle="butt",
                                joinstyle="round", **kw)
            ax.add_collection(lc)

        # ---- 5. view title bars
        for r in meta:
            if r["k"] != "view":
                continue
            x0, x1 = r["slot"]
            bx = r["bbox"]
            x0 = min(x0, bx[0]); x1 = max(x1, bx[2])
            ty = r["ty"]
            y0, y1 = ty - 2.6, ty + 5.4
            ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor=BAR, edgecolor="none", zorder=4.5))
            ax.add_patch(Rectangle((x0, y0), 1.6, y1 - y0, facecolor=NAVY, edgecolor="none", zorder=4.6))
            ax.plot([x0, x1], [y0, y0], color=NAVY, lw=0.6 * PT, zorder=4.6, solid_capstyle="butt")
            self.text(ax, x0 + 3.5, ty, r["title"], 4.6, NAVY, "bl", bold=True, z=7)
            self.text(ax, x1 - 1.5, ty, f"比例 1:{r['scale']:g}", 3.0, GREY_TXT, "br", z=7)

        # ---- 6. tables: navy header row, zebra rows
        for r in meta:
            if r["k"] != "table" or not r["rows"]:
                continue
            x, top, ws = r["x"], r["top"], r["widths"]
            tw = sum(ws)
            ys = [top] + r["rows"]
            ax.add_patch(Rectangle((x, ys[1]), tw, ys[0] - ys[1], facecolor=NAVY, edgecolor="none", zorder=4.7))
            for i in range(1, len(ys) - 1):
                if i % 2 == 0:
                    ax.add_patch(Rectangle((x, ys[i + 1]), tw, ys[i] - ys[i + 1], facecolor="#f3f6fb", edgecolor="none", zorder=1.5))
            r["_head"] = (x, ys[1], x + tw, ys[0])

        # ---- 7. legend frame
        for r in meta:
            if r["k"] == "legend" and r.get("bottom") is not None:
                lx0 = r["x"] - 1.5
                lx1 = b.inner[2] - 3
                ax.add_patch(Rectangle((lx0, r["bottom"] - 1.5), lx1 - lx0, r["top"] - 5.5 - r["bottom"] + 1.5, facecolor="#fbfcfe",
                                       edgecolor="#c5cfdf", linewidth=0.25 * PT, zorder=0.5))

        # ---- 8. text masks + texts
        heads = [r["_head"] for r in meta if r.get("_head")]
        lab_done = set()
        for t in texts_all:
            if t.get("_vt") is not None:
                continue
            if t.get("role") == "tb":
                continue
            s, h = t["s"], t["h"]
            col = "#1a1a1a"
            bold = False
            if t["layer"] == "尺寸":
                col = "#2f3640"
            elif t["layer"] == "标高":
                col = RED
            elif t["layer"] == "索引":
                col = NAVY; bold = True
            elif t["layer"] == "拆除":
                col = "#d0312d"
            in_head = any(hb[0] - 0.1 <= t["x"] <= hb[2] + 0.1 and hb[1] - 0.1 <= t["y"] <= hb[3] + 0.1 for hb in heads)
            if in_head:
                col = "#ffffff"; bold = True
            if t.get("_hdr"):
                col = NAVY; bold = True
                bx = t["box"]
                ax.plot([bx[0], bx[0] + max(40.0, bx[2] - bx[0] + 30)], [bx[1] - 0.2, bx[1] - 0.2], color=NAVY, lw=0.45 * PT, zorder=6)
            lay = t.get("_lay")
            if lay is not None:
                rec, kk = lay
                if rec["title"] and kk == 0:
                    col = NAVY; bold = True
                elif rec["title"]:
                    num = "①②③④⑤⑥⑦⑧⑨⑩"[kk - 1] if kk - 1 < 10 else f"{kk}."
                    s = f"{num} {s}"
            lab = t.get("_lab")
            if lab is not None and id(lab) not in lab_done:
                lab_done.add(id(lab))
                if self._two_line(ax, t, lab, texts_all):
                    continue
            if t.get("role") in ("label", "notes", "dim", "title", "text") and not in_head and not t.get("_hdr"):
                b_ = t["box"]; pad = 0.25
                ax.add_patch(Rectangle((b_[0] - pad, b_[1] - pad), b_[2] - b_[0] + 2 * pad, b_[3] - b_[1] + 2 * pad,
                                       facecolor="white", edgecolor="none", zorder=5))
            self.text(ax, t["x"], t["y"], s, h, col, t.get("anchor", "mc"), t.get("rot", 0) or 0, bold=bold)

        # ---- 9. title block
        if tbox:
            self._title_block(ax, b, tbox)
        if fig_path_svg:
            fig.savefig(fig_path_svg, format="svg")
        return fig

    def _two_line(self, ax, t, lab, texts_all):
        """name on the shelf, the spec line in small grey under it, when the second line has room."""
        from matplotlib.patches import Rectangle
        name, spec = split_label(lab["text"], lab.get("spec"))
        if not spec:
            return False
        h = t["h"]
        hs = max(1.9, round(h * 0.72, 2))
        a, ey, b_ = lab["shelf"]
        wn = dk.text_width(name, h)
        ws = dk.text_width(spec, hs)
        w = max(wn, ws)
        x0 = t["x"]
        if abs(lab["at"][0] - (a + (b_ - a))) < abs(lab["at"][0] - a) and w > b_ - a:   # shelf ends at the point side
            x0 = b_ - w
        sbox = (x0, ey - 0.7 - hs * 1.15, x0 + ws, ey - 0.5)
        nbox = (x0, ey + 0.6, x0 + wn, ey + 0.6 + h * 1.15)
        for o in texts_all:
            if o is t or o.get("_vt") is not None:
                continue
            if _inter(o["box"], sbox, 0.3):
                return False
        frame = self._frame
        if frame and not (frame[0] <= sbox[0] and sbox[2] <= frame[2] and sbox[1] >= frame[1]):
            return False
        for bx_ in (nbox, sbox):
            ax.add_patch(Rectangle((bx_[0] - 0.25, bx_[1] - 0.25), bx_[2] - bx_[0] + 0.5, bx_[3] - bx_[1] + 0.5,
                                   facecolor="white", edgecolor="none", zorder=5))
        if w > b_ - a + 0.01:
            ax.plot([min(a, x0), max(b_, x0 + w)], [ey, ey], color="#333a45", lw=0.18 * PT, zorder=5.5)
        self.text(ax, x0, ey + 0.6, name, h, "#1a1a1a", "bl")
        self.text(ax, x0, ey - 0.7, spec, hs, GREY_TXT, "tl")
        return True

    _frame = None

    def _title_block(self, ax, b, box):
        from matplotlib.patches import Rectangle
        x0, y0, x1, y1 = box
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor="white", edgecolor="#1b1b1b", linewidth=0.5 * PT, zorder=5))
        spec = b.spec
        views = b.s.get("views") or []
        scales = sorted({float(v.get("scale") or 100) for v in views})
        sc = " / ".join(f"1:{s:g}" for s in scales) if scales else "—"
        import time as _t
        rows = [("工程名称", spec.get("project") or "", "设计阶段", "施工图"),
                ("图纸名称", b.title, "图　号", b.no),
                ("比　　例", sc, "日　期", _t.strftime("%Y-%m-%d")),
                ("资料来源", spec.get("source_note") or "—", "张　次", f"第{b.idx + 1}张/共{b.total}张"),
                ("说　　明", "AI 生成，须专业人员复核签字后使用", None, None)]
        rh = (y1 - y0) / len(rows)
        c1, c2, c3 = x0 + 20, x1 - 44, x1 - 28
        for i, (k, v, k2, v2) in enumerate(rows):
            yt = y1 - i * rh; yb = yt - rh; ym = (yt + yb) / 2
            ax.add_patch(Rectangle((x0, yb), c1 - x0, rh, facecolor="#eef2f8", edgecolor="none", zorder=5.1))
            if k2:
                ax.add_patch(Rectangle((c2, yb), c3 - c2, rh, facecolor="#eef2f8", edgecolor="none", zorder=5.1))
            if i:
                ax.plot([x0, x1], [yt, yt], color="#1b1b1b", lw=0.18 * PT, zorder=5.2)
            ax.plot([c1, c1], [yb, yt], color="#1b1b1b", lw=0.18 * PT, zorder=5.2)
            if k2:
                ax.plot([c2, c2], [yb, yt], color="#1b1b1b", lw=0.18 * PT, zorder=5.2)
                ax.plot([c3, c3], [yb, yt], color="#1b1b1b", lw=0.18 * PT, zorder=5.2)
            self.text(ax, (x0 + c1) / 2, ym, k, 2.4, "#4b5563", "mc", z=6)
            hh = 3.4 if i in (0, 1) else 2.6
            vw = (c2 if k2 else x1) - c1 - 3
            vv = str(v)
            while dk.text_width(vv, hh) > vw and len(vv) > 4:
                vv = vv[:-2] + "…"
            self.text(ax, c1 + 1.8, ym, vv, hh, "#111111", "ml", bold=i in (0, 1), z=6)
            if k2:
                self.text(ax, (c2 + c3) / 2, ym, k2, 2.4, "#4b5563", "mc", z=6)
                if i == 1:
                    self.text(ax, (c3 + x1) / 2, ym, str(v2), 4.2, RED, "mc", bold=True, z=6)
                else:
                    self.text(ax, (c3 + x1) / 2, ym, str(v2), 2.6, "#111111", "mc", z=6)


def _item_box(it):
    t = it["t"]
    if t in ("line", "poly", "fill"):
        return _bbox(it["pts"])
    if t in ("circle", "arc"):
        return (it["c"][0] - it["r"], it["c"][1] - it["r"], it["c"][0] + it["r"], it["c"][1] + it["r"])
    return it.get("box") or (0, 0, 0, 0)


def _inside(a, b, tol=0.3):
    return a[0] >= b[0] - tol and a[1] >= b[1] - tol and a[2] <= b[2] + tol and a[3] <= b[3] + tol


def available():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def write_presentation(built, out_dir, name="drawings", pdf_name=None, install=True):
    """the 精排 SVG per sheet + one vector PDF. -> {"pdf", "svgs", "pages", "font", "warnings"}; raises when it cannot."""
    if install:
        dk.need("matplotlib", "matplotlib", "python3-matplotlib")
    import matplotlib
    if "matplotlib.pyplot" not in sys.modules:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    fonts = _font_files()
    otf = bool(fonts[0] and fonts[0].lower().endswith(".otf"))
    rc = {"pdf.fonttype": 3 if otf or not fonts[0] else 42, "svg.fonttype": "path", "lines.scale_dashes": False,
          "pdf.compression": 9, "svg.hashsalt": "aiw-sheetstyle", "path.simplify": False}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (pdf_name or f"{name}.pdf")
    tmp = pdf_path.with_name(pdf_path.name + ".part")
    svgs, warns = [], []
    with matplotlib.rc_context(rc):
        R = Renderer(matplotlib, fonts)
        with PdfPages(str(tmp), metadata={"Title": (built[0].spec.get("project") if built else "") or name,
                                          "Creator": "AIW cad-drawing sheetstyle (精排)"}) as pdf:
            for b in built:
                if not hasattr(b, "W"):
                    continue
                R._frame = b.inner
                svg = out_dir / f"{name}-{b.no}.svg"
                fig = R.sheet(b, str(svg))
                pdf.savefig(fig)
                plt.close(fig)
                svgs.append(str(svg))
                warns += R.warns
    os.replace(tmp, pdf_path)
    return {"pdf": str(pdf_path), "svgs": svgs, "pages": len(svgs), "font": fonts[0], "warnings": warns}


if __name__ == "__main__":
    import json
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sheetset as ss
    sp = json.load(open(sys.argv[1], encoding="utf-8"))
    r = ss.build_set(sp, sys.argv[2] if len(sys.argv) > 2 else "out", sys.argv[3] if len(sys.argv) > 3 else "drawings",
                     style="presentation")
    print("SHEETSTYLE_RESULT " + json.dumps({k: r[k] for k in ("ok", "errors", "warnings", "files")}, ensure_ascii=False))
