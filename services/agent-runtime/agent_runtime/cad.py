"""cad_draw / read_image support: drawings are produced by code (ezdxf on the VM), never by mouse strokes,
so every dimension is exact. The agent writes only the drawing body; this module wraps it with a helper
prelude (layers, Chinese text style, dimension style, walls, frame, title block, notes) and an epilogue that
saves the DXF and renders a PDF + PNG preview."""
from __future__ import annotations

import json
import os
import re

CAD_DIR = "cad"  # under ~/workspace on the VM

PRELUDE = r'''
# ---- AIW CAD prelude (auto-generated) ----
import json, math, os, subprocess, sys
import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment

SCALE = __SCALE__                      # drawing scale 1:SCALE; model units are millimetres
doc = ezdxf.new("R2010", setup=True)
doc.units = units.MM
doc.header["$INSUNITS"] = 4
doc.header["$LTSCALE"] = SCALE * 0.5
msp = doc.modelspace()
DXF_FONT = "simhei.ttf"                # what AutoCAD on Windows has; the preview swaps in a local CJK font
doc.styles.new("CN", dxfattribs={"font": DXF_FONT, "width": 0.8})

LAYERS = {  # name: (ACI colour, lineweight in 1/100 mm, linetype)
    "墙体": (7, 50, "CONTINUOUS"), "门窗": (4, 25, "CONTINUOUS"), "柜体": (3, 25, "CONTINUOUS"),
    "家具": (8, 18, "CONTINUOUS"), "完成面": (6, 25, "CONTINUOUS"), "轴线": (1, 13, "CENTER"),
    "隐藏线": (8, 13, "DASHED"), "填充": (8, 9, "CONTINUOUS"), "尺寸": (2, 13, "CONTINUOUS"),
    "文字": (7, 18, "CONTINUOUS"), "图框": (7, 35, "CONTINUOUS"), "索引": (5, 18, "CONTINUOUS"),
}
for _n, (_c, _w, _lt) in LAYERS.items():
    doc.layers.add(_n, color=_c, lineweight=_w, linetype=_lt if _lt in doc.linetypes else "CONTINUOUS")

_ds = doc.dimstyles.new("AIW")
for _k, _v in dict(dimscale=SCALE, dimtxt=2.5, dimasz=1.5, dimtsz=1.2, dimexe=1.5, dimexo=1.0, dimgap=0.8,
                   dimdec=0, dimtad=1, dimtih=0, dimtoh=0, dimlunit=2, dimclrd=0, dimclre=0, dimclrt=0).items():
    _ds.dxf.set(_k, _v)
_ds.dxf.dimtxsty = "CN"

def TH(mm_on_paper=3.5):
    """text height in model units for a given printed height in mm"""
    return mm_on_paper * SCALE

def line(p1, p2, layer="墙体"):
    return msp.add_line(p1, p2, dxfattribs={"layer": layer})

def pline(points, close=False, layer="墙体"):
    return msp.add_lwpolyline(points, close=close, dxfattribs={"layer": layer})

def rect(x, y, w, h, layer="柜体"):
    return pline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True, layer)

def circle(c, r, layer="家具"):
    return msp.add_circle(c, r, dxfattribs={"layer": layer})

def arc(c, r, a1, a2, layer="门窗"):
    return msp.add_arc(c, r, a1, a2, dxfattribs={"layer": layer})

def wall(x1, y1, x2, y2, t=240, hatch_it=True):
    """a straight wall of thickness t centred on the segment (x1,y1)-(x2,y2)"""
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L * t / 2, dx / L * t / 2
    pts = [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)]
    pline(pts, True, "墙体")
    if hatch_it:
        hatch(pts, "ANSI31", scale=SCALE * 0.4)
    return pts

def door(x, y, w=800, angle=0, swing=1, layer="门窗"):
    """plan door: leaf + 90° swing arc, hinge at (x,y), opening along `angle` degrees"""
    a = math.radians(angle)
    ex, ey = x + w * math.cos(a), y + w * math.sin(a)
    lx, ly = x + w * math.cos(a + swing * math.pi / 2), y + w * math.sin(a + swing * math.pi / 2)
    line((x, y), (lx, ly), layer)
    a1, a2 = (angle, angle + 90) if swing > 0 else (angle - 90, angle)
    arc((x, y), w, a1, a2, layer)
    return (ex, ey)

def window(x1, y1, x2, y2, t=240, layer="门窗"):
    """plan window: four parallel lines across the wall thickness"""
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L, dx / L
    for k in (-0.5, -1 / 6, 1 / 6, 0.5):
        line((x1 + nx * t * k, y1 + ny * t * k), (x2 + nx * t * k, y2 + ny * t * k), layer)

def hatch(points, pattern="ANSI31", scale=None, layer="填充", angle=0):
    h = msp.add_hatch(dxfattribs={"layer": layer})
    if pattern == "SOLID":
        h.set_solid_fill(color=8)
    else:
        h.set_pattern_fill(pattern, scale=scale or SCALE * 0.5, angle=angle)
    h.paths.add_polyline_path(points, is_closed=True)
    return h

def text(x, y, s, h=3.5, layer="文字", align="LEFT", rot=0, h_mm=None, **_ignored):
    """single-line text; h (alias h_mm) is the printed height in mm. align: LEFT/CENTER/RIGHT/MIDDLE_CENTER/..."""
    if h_mm is not None:
        h = h_mm
    t = msp.add_text(str(s), height=TH(h), rotation=rot, dxfattribs={"style": "CN", "layer": layer})
    t.set_placement((x, y), align=getattr(TextEntityAlignment, align.upper(), TextEntityAlignment.LEFT))
    return t

def note(x, y, lines, h=3.0, title=None, layer="文字"):
    """a block of note lines (做法说明 etc.), top-left at (x,y)"""
    gap = TH(h) * 1.7
    if title:
        text(x, y, title, h * 1.3, layer); y -= gap * 1.4
    for i, s in enumerate(lines):
        text(x, y - i * gap, s, h, layer)
    return y - len(lines) * gap

def leader(points, s, h=3.0, layer="索引"):
    """leader line through points, label at the last point"""
    pline(points, False, layer)
    x, y = points[-1]
    text(x + TH(1), y + TH(0.8), s, h, layer)

def dim(p1, p2, offset=600, angle=None):
    """linear dimension between p1 and p2. Horizontal when angle is None and the points are more apart in x,
    vertical otherwise; offset moves the dimension line away from the measured points (sign = side)."""
    (x1, y1), (x2, y2) = p1, p2
    if angle is None:
        angle = 0 if abs(x2 - x1) >= abs(y2 - y1) else 90
    if angle == 0:
        base = (x1, max(y1, y2) + offset) if offset >= 0 else (x1, min(y1, y2) + offset)
    elif angle == 90:
        base = (max(x1, x2) + offset, y1) if offset >= 0 else (min(x1, x2) + offset, y1)
    else:
        base = (x1, y1 + offset)
    d = msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=angle, dimstyle="AIW", dxfattribs={"layer": "尺寸"})
    d.render()
    return d

def dim_chain(points, offset=600, angle=None):
    """a row of consecutive dimensions through points (e.g. 柜体分格), plus the overall dimension further out"""
    for a, b in zip(points, points[1:]):
        dim(a, b, offset, angle)
    if len(points) > 2:
        dim(points[0], points[-1], offset + (TH(8) if offset >= 0 else -TH(8)), angle)

def elev_level(x, y, value_m, layer="尺寸"):
    """elevation mark (标高) ▽ with a value in metres, e.g. 2.700"""
    s = TH(3)
    pline([(x, y), (x - s / 2, y + s * 0.8), (x + s / 2, y + s * 0.8)], True, layer)
    line((x - s / 2, y + s * 0.8), (x + s * 4, y + s * 0.8), layer)
    text(x + s * 0.2, y + s, f"{value_m:.3f}", 3.0, layer)

def frame(x0=0, y0=0, size="A3", title="", project="", sheet="", drawn="AI Workstation", date=""):
    """drawing frame + title block at (x0,y0), sized for the paper at 1:SCALE. Returns the inner box."""
    W, H = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594)}[size.upper()]
    W, H = W * SCALE, H * SCALE
    pline([(x0, y0), (x0 + W, y0), (x0 + W, y0 + H), (x0, y0 + H)], True, "图框")
    m, ml = 10 * SCALE, 25 * SCALE
    ix0, iy0, ix1, iy1 = x0 + ml, y0 + m, x0 + W - m, y0 + H - m
    pline([(ix0, iy0), (ix1, iy0), (ix1, iy1), (ix0, iy1)], True, "图框")
    bw, bh = 180 * SCALE, 32 * SCALE
    bx, by = ix1 - bw, iy0
    rect(bx, by, bw, bh, "图框")
    for frac in (0.5,):
        line((bx, by + bh * frac), (ix1, by + bh * frac), "图框")
    line((bx + bw * 0.55, by), (bx + bw * 0.55, by + bh), "图框")
    text(bx + TH(3), by + bh * 0.62, project or "工程名称", 4.0, "图框")
    text(bx + TH(3), by + bh * 0.14, title or "图名", 5.0, "图框")
    text(bx + bw * 0.55 + TH(3), by + bh * 0.62, f"比例 1:{SCALE}   图号 {sheet}", 3.0, "图框")
    text(bx + bw * 0.55 + TH(3), by + bh * 0.14, f"{drawn}  {date}", 3.0, "图框")
    return (ix0, iy0, ix1, iy1)

def view_title(x, y, s, scale=None):
    """view label under a drawing, e.g. 客厅背景墙立面图 1:30"""
    text(x, y, s, 5.0, "文字", "CENTER")
    line((x - TH(len(s) * 2.6), y - TH(1.5)), (x + TH(len(s) * 2.6), y - TH(1.5)), "图框")
    text(x, y - TH(5.5), f"1:{scale or SCALE}", 3.0, "文字", "CENTER")
# ---- end prelude; drawing body follows ----
'''

EPILOGUE = r'''
# ---- AIW CAD epilogue (auto-generated) ----
_name = __NAME__
_out = {"dxf": _name + ".dxf"}
doc.saveas(_out["dxf"])
_counts = {}
for _e in msp:
    _counts[_e.dxf.layer] = _counts.get(_e.dxf.layer, 0) + 1
_out["entities_by_layer"] = _counts
_cjk = ""
try:
    _cjk = subprocess.run(["fc-list", ":lang=zh", "file"], capture_output=True, text=True).stdout.split(":")[0].strip()
except Exception:
    pass
if _cjk:
    from ezdxf.tools import fonts as _fonts
    _fonts.build_system_font_cache()
    doc.styles.get("CN").dxf.font = os.path.basename(_cjk)
try:
    from ezdxf import bbox
    _ext = bbox.extents(msp)
    _out["extents_mm"] = [round(_ext.extmin.x), round(_ext.extmin.y), round(_ext.extmax.x), round(_ext.extmax.y)]
except Exception as _e:
    _out["extents_error"] = str(_e)
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.addons.drawing.properties import LayoutProperties
    _w, _h = 16.54, 11.69
    if "extents_mm" in _out:
        _x0, _y0, _x1, _y1 = _out["extents_mm"]
        _r = (_y1 - _y0) / max(_x1 - _x0, 1)
        _h = max(4.0, min(_w * _r, 23.4))
    _fig = plt.figure(figsize=(_w, _h))
    _ax = _fig.add_axes([0.02, 0.02, 0.96, 0.96])
    _saved = {}
    for _l in doc.layers:                 # print like a construction drawing: black lines, grey hatches
        _saved[_l.dxf.name] = _l.dxf.color
        _l.dxf.color = 8 if _l.dxf.name == "填充" else 7
    _ctx = RenderContext(doc)
    _lp = LayoutProperties.from_layout(msp)
    _lp.set_colors("#FFFFFF")
    Frontend(_ctx, MatplotlibBackend(_ax)).draw_layout(msp, finalize=True, layout_properties=_lp)
    for _l in doc.layers:
        _l.dxf.color = _saved.get(_l.dxf.name, 7)
    _fig.set_size_inches(_w, _h)
    _fig.savefig(_name + ".pdf", facecolor="white")
    _fig.savefig(_name + ".png", dpi=110, facecolor="white")
    _out["pdf"], _out["png"] = _name + ".pdf", _name + ".png"
    if not _cjk:
        _out["warning"] = "no Chinese font found (apt install fonts-wqy-microhei); preview text may be boxes"
except Exception as _e:
    _out["render_error"] = f"{type(_e).__name__}: {_e}"
doc.styles.get("CN").dxf.font = DXF_FONT
doc.saveas(_out["dxf"])
# a LibreCAD viewing copy that uses LibreCAD's own CJK font (the delivered DXF keeps simhei.ttf for AutoCAD)
for _lff in ("wqy-unicode", "unicode"):
    if os.path.exists(f"/usr/share/librecad/fonts/{_lff}.lff"):
        doc.styles.get("CN").dxf.font = _lff + ".lff"
        doc.saveas("." + _name + ".view.dxf")
        _out["view_dxf"] = "." + _name + ".view.dxf"
        break
print("AIW_CAD_RESULT " + json.dumps(_out, ensure_ascii=False))
'''

_NAME_OK = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")


def safe_name(name: str) -> str:
    n = _NAME_OK.sub("_", (name or "").strip())[:60].strip("_")
    return n or "drawing"


def build_script(name: str, code: str, scale: int = 50) -> str:
    scale = int(scale) if str(scale).isdigit() and 1 <= int(scale) <= 1000 else 50
    body = (code or "").replace("\r\n", "\n")
    return (PRELUDE.replace("__SCALE__", str(scale)) + "\n" + body + "\n"
            + EPILOGUE.replace("__NAME__", json.dumps(safe_name(name), ensure_ascii=False)))


def parse_result(stdout: str) -> dict | None:
    for line in (stdout or "").splitlines()[::-1]:
        if line.startswith("AIW_CAD_RESULT "):
            try:
                return json.loads(line[len("AIW_CAD_RESULT "):])
            except ValueError:
                return None
    return None


READ_IMAGE_PROMPT = (
    "你是室内装修/工程制图助手。仔细读这张图（设计效果图、平面图、立面图、手稿或报价单截图），用中文按以下结构如实写出，"
    "不要编造看不清的数字：\n"
    "1. 图的类型和内容（哪个空间、哪个面、什么视角）\n"
    "2. 所有能读到的尺寸、标高、数量（逐条列出，标明位置）\n"
    "3. 构件/造型清单：从左到右、从下到上列出每个造型、柜体、门洞、灯带、线条、材质分界，带相对位置和大致比例\n"
    "4. 材料、颜色、做法文字\n"
    "5. 看不清或缺失、画施工图前需要向用户确认的地方\n"
)


# --- script drawing is opt-in -------------------------------------------------------------------------------
_NEGATED = re.compile(r"(禁止|不要|不能|不准|不许|不用|别|勿|严禁|\bnever\b|\bdon't\b|\bdo not\b|\bno\b)[^，。；,;.\n]{0,40}", re.I)
_SCRIPT_DRAW = re.compile(r"cad_draw|用[^，。；,;\n]{0,15}(脚本|代码|script|ezdxf)[^，。；,;\n]{0,6}(画|出图|生成|绘|做)"
                          r"|(script|python|ezdxf)[^，。；,;.\n]{0,10}(draw|generate)", re.I)


_CAD_TASK = re.compile(r"施工图|装修图|平面图|立面图|librecad|(?<![a-z])cad(?![a-z])|dxf", re.I)


def is_cad_task(prompt: str) -> bool:
    return bool(_CAD_TASK.search(prompt or ""))


def script_drawing_allowed(prompt: str) -> bool:
    """cad_draw (ezdxf script) only when the user asked to DRAW with a script; a negated mention
    ("禁止用脚本出图") or reading files with a script does not count. Otherwise draw in LibreCAD."""
    p = _NEGATED.sub("", prompt or "")
    return bool(_SCRIPT_DRAW.search(p))


# typing speed: LibreCAD keeps up with 8 ms/char and an 80 ms pause per command (tested with 150-command batches);
# the old 25 ms / 350 ms made a 60-command batch take ~45 s
TYPE_DELAY_MS = int(os.environ.get("AIW_CAD_TYPE_DELAY_MS", "8"))
STEP_PAUSE_S = float(os.environ.get("AIW_CAD_STEP_PAUSE_S", "0.08"))

LIBRECAD_FONT = "notosc"   # outline CJK font made by deploy/vm/ttf2lff.py; LibreCAD's own fonts have no hanzi


def _librecad_font_setup() -> str:
    """Shell: once per VM, give LibreCAD a Chinese font (its own fonts have no hanzi) and a 1:50 template.
    See deploy/vm/librecad-setup.sh; a quick no-op once the marker exists."""
    return ('([ -f "$HOME/.config/aiw/librecad-ready-v1" ] || { S=$(ls "$HOME"/ai-workstation/deploy/vm/librecad-setup.sh '
            '/opt/ai-workstation/deploy/vm/librecad-setup.sh 2>/dev/null | head -1); [ -n "$S" ] && bash "$S" >/dev/null 2>&1; }; true)')


def _parse_text(c: str):
    """'TEXT|x,y|height|content' (also 'text|x,y|content', height 350) -> (point, height, content) or None."""
    if not re.match(r"(?i)^\s*(text|txt)\s*\|", c):
        return None
    bits = [b.strip() for b in c.split("|", 3)]
    if len(bits) == 3:
        return bits[1], "350", bits[2]
    if len(bits) == 4:
        h = bits[2] if re.fullmatch(r"\d+(\.\d+)?", bits[2]) else "350"
        body = bits[3] if h == bits[2] else bits[2] + "|" + bits[3]
        return bits[1], h, body
    return None



# ---- sheet macros: expand into the same LibreCAD commands a draftsman would type -----------------------------
SHEET_PITCH, SHEET_W, SHEET_H = 25000, 21000, 14850   # A3 at 1:50, sheet n at x = n*25000
MIN_SHEET_ENTITIES = int(os.environ.get("AIW_CAD_MIN_SHEET_ENTITIES", "40"))
_POINT = re.compile(r"^\s*@?-?\d")


def _f(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _pt(x: float, y: float) -> str:
    return f"{_f(x)},{_f(y)}"


def _nums(s: str) -> list[float]:
    return [float(v) for v in re.split(r"[ ,，]+", s.strip()) if v]


def _xy(s: str) -> tuple[float, float]:
    a = _nums(s)
    return a[0], a[1]


_KNOWN_PIPE = re.compile(r"(?i)^\s*(#bad |(text|txt|save|pdf|sheet|frame)\s*\|)")


def _macro(c: str) -> list[str] | None:
    """FRAME / NOTE / DIMH / DIMV / LEADER and the one-line geometry forms LINE|p1|p2|..., RECT|p1|p2,
    CIRCLE|c|r, ARC|c|r|a1|a2 -> plain LibreCAD commands, or None when c is not a macro.
    The 2026-10-20 run wrote every line and rectangle as 'rectangle|2000,2000|12000,8000'; typed as is, LibreCAD
    ignored it, so no wall or panel outline reached the drawing while texts and dimensions did."""
    m = re.match(r"(?i)^\s*(textc|frame|note|dimh|dimv|leader|line|li|l|pline|polyline|pl|rect|rectangle|rec|circle|ci|c|arc|a)\s*\|(.*)$", c)
    if not m:
        if "|" in c and not _KNOWN_PIPE.match(c):
            raise ValueError("unknown command form")  # -> '#BAD', reported back instead of typed into nowhere
        return None
    kind, bits = m.group(1).upper(), [b.strip() for b in m.group(2).split("|")]
    out: list[str] = []
    pts = lambda bs: [_pt(*_xy(b)) for b in bs if b]
    if kind == "TEXTC":  # TEXTC|x,y|h|内容: text centred on x,y (TEXT is left-aligned: x,y is its lower left corner)
        x, y = _xy(bits[0])
        h = float(bits[1]) if len(bits) > 2 else 250.0
        body = bits[2] if len(bits) > 2 else bits[1]
        x1, _, x2, _ = text_box(x, y, h, body)
        return [f"TEXT|{_pt(round(x - (x2 - x1) / 2), round(y - h / 2))}|{_f(h)}|{body}"]
    if kind in ("LINE", "LI", "L", "PLINE", "POLYLINE", "PL"):
        close = bool(bits) and bits[-1].upper() in ("C", "CLOSE")
        P = pts(bits[:-1] if close else bits)
        if len(P) < 2:
            raise ValueError("line needs two points")
        return ["line", *P, *([P[0]] if close else []), "ESC"]
    if kind in ("RECT", "RECTANGLE", "REC"):
        P = pts(bits)
        if len(P) != 2:
            raise ValueError("rectangle needs two corners")
        return ["rectangle", *P, "ESC"]
    if kind in ("CIRCLE", "CI", "C"):
        return ["circle", pts(bits[:1])[0], _f(float(bits[1])), "ESC"]
    if kind in ("ARC", "A"):  # ARC|cx,cy|r|start°|end° counter-clockwise, typed as a fine line chain
        import math             # (LibreCAD's own arc prompts misread typed radius/angles: wrong centre and radius)
        cx, cy = _xy(bits[0])
        r, a1, a2 = float(bits[1]), float(bits[2]), float(bits[3])
        if a2 <= a1:
            a2 += 360
        n = max(4, int((a2 - a1) / 7.5))
        return ["line", *[_pt(round(cx + r * math.cos(math.radians(a1 + (a2 - a1) * i / n)), 1),
                              round(cy + r * math.sin(math.radians(a1 + (a2 - a1) * i / n)), 1)) for i in range(n + 1)], "ESC"]
    if kind == "FRAME":  # FRAME|n|图名|图号|比例|工程名称
        n = int(float(bits[0] or 0))
        title = bits[1] if len(bits) > 1 else ""
        no = bits[2] if len(bits) > 2 else ""
        scale = bits[3] if len(bits) > 3 and bits[3] else "1:50"
        proj = bits[4] if len(bits) > 4 else ""
        ox = n * SHEET_PITCH
        x1, y1, x2, y2 = ox + 1250, 500, ox + SHEET_W - 500, SHEET_H - 500   # 25 mm binding edge, 10 mm others
        bx, bw, bh = x2 - 9000, 9000, 2000                                   # 180 x 40 mm title block
        out += ["rectangle", _pt(ox, 0), _pt(ox + SHEET_W, SHEET_H), "ESC",
                "rectangle", _pt(x1, y1), _pt(x2, y2), "ESC",
                "rectangle", _pt(bx, y1), _pt(x2, y1 + bh), "ESC",
                "line", _pt(bx, y1 + bh / 2), _pt(x2, y1 + bh / 2), "ESC",
                "line", _pt(bx + 5600, y1), _pt(bx + 5600, y1 + bh), "ESC"]
        # titles shrink to their cell: '入口形象墙正面(十六字方针)' at 350 ran into the 比例 cell (2026-10-23)
        if proj:
            out.append(f"TEXT|{_pt(bx + 150, y1 + bh / 2 + 330)}|{_f(fit_height(proj, 220, 5300))}|{proj}")
        out.append(f"TEXT|{_pt(bx + 150, y1 + 300)}|{_f(fit_height(title, 350, 5300))}|{title}")
        out.append(f"TEXT|{_pt(bx + 5750, y1 + bh / 2 + 330)}|200|图号 {no}")
        out.append(f"TEXT|{_pt(bx + 5750, y1 + 330)}|200|比例 {scale}")
        return out
    if kind == "NOTE":  # NOTE|x,y|h|第一行;第二行;...   (top line at y, going down)
        x, y = _xy(bits[0])
        h = float(bits[1]) if len(bits) > 2 and re.fullmatch(r"\d+(\.\d+)?", bits[1]) else 175.0
        body = bits[2] if len(bits) > 2 else bits[1]
        for i, line in enumerate([t for t in re.split(r"[;；\n]", body) if t.strip()]):
            out.append(f"TEXT|{_pt(x, y - i * h * 1.8)}|{_f(h)}|{line.strip()}")
        return out
    if kind in ("DIMH", "DIMV"):  # DIMH|y_measured|y_dimline|x1,x2,x3...   DIMV|x_measured|x_dimline|y1,y2,...
        base, line, vals = float(bits[0]), float(bits[1]), _nums(bits[2])
        side = 1 if line >= base else -1
        tool = "dimhorizontal" if kind == "DIMH" else "dimvertical"
        P = (lambda a, b: _pt(a, b)) if kind == "DIMH" else (lambda a, b: _pt(b, a))
        for a, b in zip(vals, vals[1:]):
            out += [tool, P(a, base), P(b, base), P((a + b) / 2, line)]
        if len(vals) > 2:  # overall dimension one row further out
            out += [tool, P(vals[0], base), P(vals[-1], base), P((vals[0] + vals[-1]) / 2, line + side * 600)]
        out.append("ESC")
        return out
    if kind == "LEADER":  # LEADER|x1,y1|x2,y2|h|文字   arrow end at x1,y1, label on a shelf from x2,y2
        (ax, ay), (bx, by) = _xy(bits[0]), _xy(bits[1])
        h = float(bits[2]) if len(bits) > 3 else 175.0
        label = bits[3] if len(bits) > 3 else bits[2]
        shelf = max(600.0, len(label) * h * 1.05)
        d = 1 if bx >= ax else -1
        out += ["line", _pt(ax, ay), _pt(bx, by), _pt(bx + d * shelf, by), "ESC",
                "circle", _pt(ax, ay), _f(h * 0.25), "ESC",
                f"TEXT|{_pt(bx if d > 0 else bx - shelf, by + h * 0.3)}|{_f(h)}|{label}"]
        return out
    return None


def expand(commands: list[str]) -> list[str]:
    """Macros expanded; everything else passes through unchanged."""
    out: list[str] = []
    for c in commands:
        c = str(c)
        try:
            m = _macro(c)
        except (ValueError, IndexError):
            m = None
            out.append(f"#BAD {c}")
            continue
        out.extend(m if m is not None else [c])
    return out


_ABS_POINT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_SHEET = re.compile(r"(?i)^\s*(frame|sheet)\s*\|\s*(-?\d+(?:\.\d+)?)")


# sheet-local layout limits (FRAME): inner border and the 180 x 40 mm title block in its lower right corner
FRAME_X1, FRAME_Y1, FRAME_X2, FRAME_Y2 = 1250, 500, SHEET_W - 500, SHEET_H - 500
TB_X1, TB_Y2 = FRAME_X2 - 9000, FRAME_Y1 + 2000


def _cw(ch: str) -> float:
    """Width of one character in text heights, as LibreCAD's CJK font draws it (measured on the 2026-10-23 PDF:
    the 0.95 estimate let titles run into the next title-block cell)."""
    o = ord(ch)
    if o > 0x2e80 or ch in "（）【】，。：；、":
        return 1.05
    if ch in "()[]-.,:;|/ ":
        return 0.45
    return 0.65


def text_box(x: float, y: float, h: float, body: str) -> tuple[float, float, float, float]:
    """Rough extent of a left-aligned single-line text: a hanzi is about one text height wide, latin ~0.65."""
    w = sum(_cw(ch) for ch in body) * h
    return x, y, x + w, y + h


def fit_height(body: str, h: float, width: float, h_min: float = 120.0) -> float:
    """The largest height <= h at which body fits in width."""
    w1 = text_box(0, 0, 1.0, body)[2] or 1.0
    return max(h_min, min(h, (width / w1) // 5 * 5))


def _overlap(a, b) -> float:
    """Overlapping area as a share of the smaller box."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return w * h / small


def _pad(b, k=0.15):
    """A text box with its line gap: two texts closer than ~0.3 h read as one smudge."""
    h = b[3] - b[1]
    return b[0], b[1] - h * k, b[2], b[3] + h * k


def _shrink(b, k=0.12):
    dx, dy = (b[2] - b[0]) * k, (b[3] - b[1]) * k
    return b[0] + dx, b[1] + dy, b[2] - dx, b[3] - dy


def _seg_hits_box(seg, b) -> bool:
    """Does the segment pass through the box (Liang-Barsky clip)?"""
    (x1, y1, x2, y2), (bx1, by1, bx2, by2) = seg, b
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - bx1), (dx, bx2 - x1), (-dy, y1 - by1), (dy, by2 - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    return True


def _segs_cross(a, b) -> bool:
    """Proper crossing of two segments (touching ends do not count)."""
    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = a, b
    d1, d2 = orient(bx1, by1, bx2, by2, ax1, ay1), orient(bx1, by1, bx2, by2, ax2, ay2)
    d3, d4 = orient(ax1, ay1, ax2, ay2, bx1, by1), orient(ax1, ay1, ax2, ay2, bx2, by2)
    return d1 * d2 < 0 and d3 * d4 < 0


DIM_TEXT_H = float(os.environ.get("AIW_CAD_DIM_TEXT_H", "150"))  # dimension numbers as drawn at 1:50
LEADER_MAX = float(os.environ.get("AIW_CAD_LEADER_MAX", "6000"))  # 120 mm on paper at 1:50
SEG, LEAD, DIMK = "\x00seg", "\x00leader", "\x00dim"     # markers for non-text entries in the box memory


def _dim_geom(tool: str, p1, p2, p3):
    """(text box, dimension-line segment, measured span key) of a dimhorizontal/dimvertical, as LibreCAD draws it:
    the number centred above (left of) the dimension line."""
    if tool == "dimhorizontal":
        a, b, line = min(p1[0], p2[0]), max(p1[0], p2[0]), p3[1]
        txt = _f(round(b - a))
        w = text_box(0, 0, DIM_TEXT_H, txt)[2]
        m = (a + b) / 2
        return (m - w / 2, line + 40, m + w / 2, line + 40 + DIM_TEXT_H), (a, line, b, line), ("H", round(a), round(b)), txt
    a, b, line = min(p1[1], p2[1]), max(p1[1], p2[1]), p3[0]
    txt = _f(round(b - a))
    w = text_box(0, 0, DIM_TEXT_H, txt)[2]
    m = (a + b) / 2
    return (line - 40 - DIM_TEXT_H, m - w / 2, line - 40, m + w / 2), (line, a, line, b), ("V", round(a), round(b)), txt


def layout_issues(items: list[tuple[int, str]], prior_boxes: dict | None = None) -> tuple[list[str], list]:
    """items: (sheet, expanded command) for everything the model drew itself (not FRAME output).
    Checked before typing, on this batch and on what earlier batches put on the sheet (prior_boxes: sheet ->
    [(box, body)], where body is the text, or a marker for a line segment, a leader or a dimension):
    - points, texts, dimension lines outside the sheet's inner border or inside the title block;
    - texts or dimension numbers on top of each other (dimension chains with segments too short for their number);
    - texts crossed by a line: a label running out of its table cell, panel or title-block cell;
    - leaders that cross each other or run more than LEADER_MAX from the object to their label;
    - the same span dimensioned twice (DIMH/DIMV chains already add the overall dimension).
    Found on the 2026-10-20 and 2026-10-23 runs. Returns (issues, new memory entries as (sheet, box, body))."""
    issues, boxes = [], []
    prior_boxes = prior_boxes or {}
    tool, prev, dimpts = "", None, []

    def where(sheet, x, y):
        lx = x - sheet * SHEET_PITCH
        if not (FRAME_X1 - 1 <= lx <= FRAME_X2 + 1 and FRAME_Y1 - 1 <= y <= FRAME_Y2 + 1):
            return "outside the border"
        if lx > TB_X1 + 1 and y < TB_Y2 - 1:
            return "inside the title block"
        return ""

    def mem(sheet):
        return prior_boxes.get(sheet, []) + [(bb, bd) for s2, bb, bd in boxes if s2 == sheet]

    def texts(sheet):
        return [(bb, bd) for bb, bd in mem(sheet) if not str(bd).startswith("\x00") or bd == DIMK]

    def label(bd, bb):
        return f"dimension {_f(round(max(bb[2] - bb[0], bb[3] - bb[1])))}" if bd == DIMK else f"text '{str(bd)[:16]}'"

    def seg_issue(sheet, seg, what):
        ox = sheet * SHEET_PITCH
        for k in range(0, 21):
            hx, hy = seg[0] + (seg[2] - seg[0]) * k / 20, seg[1] + (seg[3] - seg[1]) * k / 20
            w = where(sheet, hx, hy)
            if w:
                return f"{what} runs {w} near {_pt(round(hx - ox), round(hy))}"
        return ""

    def struck(sheet, seg, bb, body, by_new_line):
        ox = sheet * SHEET_PITCH
        lo = f"line {_pt(round(seg[0] - ox), round(seg[1]))}-{_pt(round(seg[2] - ox), round(seg[3]))}"
        return (f"text '{str(body)[:16]}' at {_pt(round(bb[0] - ox), round(bb[1]))} is crossed by {lo}: it runs out of "
                f"its cell/panel or sits on a line (shorten it, lower its height, or move it clear)")

    for sheet, e in items:
        ox = sheet * SHEET_PITCH
        if e.startswith("#LEADER "):
            try:
                (ax, ay), (bx, by) = [_xy(v) for v in e[8:].split("|")[:2]]
            except (ValueError, IndexError):
                continue
            seg = (ax, ay, bx, by)
            if ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 > LEADER_MAX:
                issues.append(f"leader from {_pt(ax - ox, ay)} to {_pt(bx - ox, by)} is longer than {_f(LEADER_MAX)}: "
                              f"put its label just outside the object, level with the point it names")
            for pb, bd in mem(sheet):
                if bd == LEAD and _segs_cross(seg, pb):
                    issues.append(f"leader from {_pt(ax - ox, ay)} to {_pt(bx - ox, by)} crosses another leader: "
                                  f"order the labels in the same order as the points they name")
                    break
            boxes.append((sheet, seg, LEAD))
            continue
        t = _parse_text(e)
        if t:
            try:
                x, y = _xy(t[0])
                h = float(t[1])
            except (ValueError, IndexError):
                continue
            b = text_box(x, y, h, t[2])
            bad = where(sheet, b[0], b[1]) or where(sheet, b[2], b[3]) or where(sheet, b[2], b[1])
            if bad:
                issues.append(f"text '{t[2][:16]}' at {_pt(x - ox, y)} (ends near x={_f(round(b[2] - ox))}) is {bad}")
            for pb, pbody in texts(sheet):
                if _overlap(_pad(b), _pad(pb) if pbody != DIMK else pb) > 0.05:
                    issues.append(f"text '{t[2][:16]}' at {_pt(x - ox, y)} overlaps {label(pbody, pb)}")
                    break
            else:
                for pb, pbody in mem(sheet):
                    if pbody in (SEG, LEAD) and _seg_hits_box(pb, _shrink(b)):
                        issues.append(struck(sheet, pb, b, t[2], False))
                        break
            boxes.append((sheet, b, t[2]))
            continue
        m = _ABS_POINT.match(e)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            if tool in ("dimhorizontal", "dimvertical", "dimh", "dimv"):
                dimpts.append((x, y))
                if len(dimpts) == 3:
                    tb, dseg, span, txt = _dim_geom("dimhorizontal" if tool in ("dimhorizontal", "dimh") else "dimvertical", *dimpts)
                    dimpts = []
                    w = seg_issue(sheet, dseg, f"dimension {txt}")
                    if w:
                        issues.append(w)
                    dup = next((1 for pb, bd in mem(sheet) if bd == "\x00span" and pb == span), None)
                    if dup:
                        issues.append(f"dimension {txt} ({'x' if span[0] == 'H' else 'y'} {_f(span[1] - (ox if span[0] == 'H' else 0))}"
                                      f"..{_f(span[2] - (ox if span[0] == 'H' else 0))}) is already drawn: a DIMH/DIMV chain adds its "
                                      f"overall dimension by itself")
                    else:
                        for pb, pbody in texts(sheet):
                            if _overlap(tb, _pad(pb) if pbody != DIMK else pb) > 0.05:
                                issues.append(f"dimension {txt} near {_pt(round((tb[0] + tb[2]) / 2 - ox), round(tb[1]))} overlaps "
                                              f"{label(pbody, pb)}: segments under ~{_f(round(DIM_TEXT_H * 4))} cannot hold their "
                                              f"number; dimension them in a separate row further out, or move the text")
                                break
                    boxes.append((sheet, tb, DIMK))
                    boxes.append((sheet, span, "\x00span"))
                continue
            bad = where(sheet, x, y)
            if bad:
                issues.append(f"point {_pt(x - ox, y)} is {bad}")
            elif prev is not None and tool in ("line", "li", "l", "rectangle", "rec", "rect", "polyline", "pl"):
                # the edges between two good points can still cross the title block (a rectangle from 2000,2000
                # to 12000,5000 runs through it)
                (px, py) = prev
                segs = ([((px, py), (x, py)), ((x, py), (x, y)), ((x, y), (px, y)), ((px, y), (px, py))]
                        if tool.startswith("rec") else [((px, py), (x, y))])
                for (ax, ay), (bx, by) in segs:
                    w = seg_issue(sheet, (ax, ay, bx, by), f"{tool} from {_pt(px - ox, py)} to {_pt(x - ox, y)}")
                    if w:
                        issues.append(w)
                        break
            if prev is not None and tool in ("line", "li", "l", "rectangle", "rec", "rect", "polyline", "pl"):
                (px, py) = prev
                segs = ([(px, py, x, py), (x, py, x, y), (x, y, px, y), (px, y, px, py)]
                        if tool.startswith("rec") else [(px, py, x, y)])
                for sg in segs:
                    for pb, pbody in texts(sheet):
                        if pbody != DIMK and _seg_hits_box(sg, _shrink(pb)):
                            issues.append(struck(sheet, sg, pb, pbody, True))
                            break
                    boxes.append((sheet, sg, SEG))
            prev = (x, y)
            continue
        tool, prev, dimpts = e.strip().lower(), None, []
    # one message per problem is enough for the model; the same text crossed by four rectangle edges is one issue
    seen, uniq = set(), []
    for i in issues:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq, boxes


def place(commands: list[str], sheet: int = 0, drawn: list | None = None) -> tuple[list[str], int, int]:
    """Expand macros and put every point on its sheet. 'FRAME|n|...' (or 'SHEET|n') makes sheet n current, and from
    then on a point given sheet-locally (x left of the sheet's origin n*25000) is moved onto it, so both
    'line 2000,3000' after FRAME|4 and the absolute 'line 102000,3000' land on sheet 4. Without this the model drew
    sheets 1-11 at local coordinates and every sheet piled up on top of sheet 0 (2026-10-19 run).
    Returns (expanded commands, current sheet afterwards, points moved)."""
    out: list[str] = []
    moved = 0

    def shift(p: str) -> str:
        nonlocal moved
        m = _ABS_POINT.match(p)
        ox = sheet * SHEET_PITCH
        if not m or ox <= 0:
            return p
        x, y = float(m.group(1)), float(m.group(2))
        if x < ox - 2000:  # a sheet-local x (0..21000): absolute points on this sheet are >= its origin
            moved += 1
            return _pt(x + ox, y)
        return p

    for c in commands:
        c = str(c)
        ms = _SHEET.match(c)
        if ms:
            sheet = max(0, int(float(ms.group(2))))
            if ms.group(1).upper() == "SHEET":
                continue  # only sets the origin
            out.extend(expand([c]))  # FRAME is already absolute
            continue
        ml = re.match(r"(?i)^\s*leader\s*\|([^|]+)\|([^|]+)\|", c)
        if ml and drawn is not None:
            try:
                a1, b1 = shift(_pt(*_xy(ml.group(1)))), shift(_pt(*_xy(ml.group(2))))
                moved -= (a1 != _pt(*_xy(ml.group(1)))) + (b1 != _pt(*_xy(ml.group(2))))
                drawn.append((sheet, f"#LEADER {a1}|{b1}"))
            except (ValueError, IndexError):
                pass
        for e in expand([c]):
            t = _parse_text(e)
            if t:
                pt, h, body = t
                e = f"TEXT|{shift(pt)}|{h}|{body}"
            elif _ABS_POINT.match(e):
                e = shift(e)
            out.append(e)
            if drawn is not None:
                drawn.append((sheet, e))
    return out, sheet, moved


def _item_key(c: str, sheet: int) -> tuple[str | None, int, int]:
    """(key, expanded length, sheet afterwards) of one cad_command item. The key is the item's absolute, expanded form,
    so 'rect|2000,3000|4000,5000' after FRAME|4 and 'rect|102000,3000|104000,5000' are the same drawing. A FRAME's key
    is its sheet alone (a second frame on a sheet is always a duplicate). Bare tool names and single points (the
    multi-item style) and SAVE/PDF get no key: they are never skipped."""
    c = str(c)
    ms = _SHEET.match(c)
    if ms:
        n = max(0, int(float(ms.group(2))))
        if ms.group(1).upper() == "SHEET":
            return None, 0, n
        return f"FRAME|{n}", len(expand([c])), n
    out, sheet2, _ = place([c], sheet)
    low = c.strip().lower()
    if "|" not in c or low.startswith(("save", "pdf", "#")) or any(e.startswith("#BAD ") for e in out):
        return None, len(out), sheet2
    return "\x1f".join(out), len(out), sheet2


def dedupe(raw: list[str], sheet: int, have: set) -> tuple[list[str], list, int]:
    """Drop items already drawn in this task (or twice in this batch). Run 3ac (2026-10-20) resent sheet 0 three
    times after a timeout and a dialog error and drew it on top of itself. A skipped FRAME|n becomes SHEET|n so the
    following sheet-local points still land on sheet n. Returns (items to type, their keys, items skipped)."""
    out, keys, seen, skipped = [], [], set(), 0
    for c in raw:
        k, _n, nxt = _item_key(c, sheet)
        if k is not None and (k in have or k in seen):
            skipped += 1
            if k.startswith("FRAME|"):
                out.append(f"SHEET|{nxt}")
                keys.append(None)
            sheet = nxt
            continue
        if k is not None:
            seen.add(k)
        out.append(c)
        keys.append(k)
        sheet = nxt
    return out, keys, skipped


def landed_keys(raw: list[str], keys: list, sheet: int, landed: int) -> list[str]:
    """Keys of the items whose whole expansion was typed, given `landed` expanded commands went in."""
    got, pos = [], 0
    for c, k in zip(raw, keys):
        _k, n, sheet = _item_key(c, sheet)
        pos += n
        if pos > landed:
            break
        if k is not None:
            got.append(k)
    return got


def resume_index(commands: list[str], done: int) -> int:
    """After a batch was cut off with `done` items typed, where to restart so no tool is left half-fed:
    back up to the item that started the tool (a point never starts one)."""
    i = max(0, min(done, len(commands)))
    while i > 0 and i < len(commands) and _POINT.match(commands[i]):
        i -= 1
    return i


SPLIT_PDF_SCRIPT = r'''# AIW: one PDF page per A3 sheet, rendered by LibreCAD itself (librecad dxf2pdf). Sheet n sits at x = n*PITCH.
import json, os, re, shutil, subprocess, sys, tempfile
src, out = sys.argv[1], sys.argv[2]
PITCH, W, H, MIN = 25000.0, 21000.0, 14850.0, int(sys.argv[3]) if len(sys.argv) > 3 else 40
tmp = tempfile.mkdtemp(prefix="aiw_pdf_")
env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
def dxf2pdf(dxf, pdf):  # dxf2pdf ignores -o for a single file and writes <name>.pdf beside it
    try:
        subprocess.run(["librecad", "dxf2pdf", "-a", "-m", "-p", "420x297", "-f", "0,0,0,0", dxf],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    made = dxf[:-4] + ".pdf"
    if made != pdf and os.path.exists(made):
        shutil.move(made, pdf)
    return os.path.exists(pdf) and os.path.getsize(pdf) > 0
counts, pages, stray, geom = {}, [], {}, {}
try:
    import ezdxf
    doc = ezdxf.readfile(src)
    # sheet of an entity from its own defining points (ezdxf.bbox measured every text with fonts and took >5 min
    # on a 3000-entity drawing, so every PDF export timed out). An entity reaching outside its sheet (a stray
    # line drawn from a local to an absolute point) would shrink the page to a speck: left out and reported.
    def pts(e):
        t, g = e.dxftype(), e.dxf.get
        if t == "LINE":
            return [g("start"), g("end")]
        if t == "LWPOLYLINE":
            return [(p[0], p[1]) for p in e.get_points()]
        if t == "POLYLINE":
            return [v.dxf.location for v in e.vertices]
        if t in ("CIRCLE", "ARC"):
            c, r = g("center"), float(g("radius") or 0)
            return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        if t == "DIMENSION":
            return [p for p in (g("defpoint"), g("defpoint2"), g("defpoint3"), g("text_midpoint")) if p is not None]
        if t == "HATCH":
            out = []
            for path in e.paths:
                out += [(v[0], v[1]) for v in getattr(path, "vertices", [])]
                for ed in getattr(path, "edges", []):
                    for a in ("start", "end", "center"):
                        if hasattr(ed, a):
                            out.append(getattr(ed, a))
            return out
        p = g("insert")
        return [p] if p is not None else []
    groups, stray, geom = {}, {}, {}
    for e in doc.modelspace():
        try:
            P = [p for p in pts(e) if p is not None]
        except Exception:
            P = []
        if not P:
            continue
        n = int(P[0][0] // PITCH)
        lo, hi = n * PITCH - 1500, n * PITCH + W + 1500
        if any(not (lo <= p[0] <= hi) or not (-1500 <= p[1] <= H + 1500) for p in P):
            stray[n] = stray.get(n, 0) + 1
            continue
        groups.setdefault(n, set()).add(e.dxf.handle)
        if e.dxftype() in ("LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "HATCH", "INSERT", "SPLINE", "ELLIPSE"):
            geom[n] = geom.get(n, 0) + 1
    for n in sorted(groups):
        if n < 0 or n > 60:
            continue
        counts[n] = len(groups[n])
        d = ezdxf.readfile(src)
        m = d.modelspace()
        for e in list(m):
            if e.dxf.handle not in groups[n]:
                m.delete_entity(e)
        f = os.path.join(tmp, "sheet%02d.dxf" % n)
        d.saveas(f)
        if dxf2pdf(f, f[:-4] + ".pdf"):
            pages.append(f[:-4] + ".pdf")
except ImportError:
    pass
if not pages:  # no ezdxf: the whole drawing on one page
    one = os.path.join(tmp, "all.pdf")
    shutil.copy(src, os.path.join(tmp, "all.dxf"))
    if dxf2pdf(os.path.join(tmp, "all.dxf"), one):
        pages.append(one)
if not pages:
    print("pdf failed: librecad dxf2pdf produced nothing"); sys.exit(6)
ok = False
if len(pages) == 1:
    shutil.copy(pages[0], out); ok = True
if not ok and shutil.which("pdfunite"):
    ok = subprocess.run(["pdfunite", *pages, out]).returncode == 0
if not ok and shutil.which("gs"):
    ok = subprocess.run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite", "-sOutputFile=" + out, *pages]).returncode == 0
if not ok:
    try:
        from pypdf import PdfWriter
        w = PdfWriter()
        for p in pages:
            w.append(p)
        w.write(out); ok = True
    except Exception:
        pass
if not ok:
    print("pdf failed: could not merge pages (apt install poppler-utils)"); sys.exit(6)
thin = [n for n, c in counts.items() if c < MIN]
# the frame alone is 3 rectangles + 2 lines; a sheet with little more than that has texts but no drawing
# (2026-10-20: every line/rectangle was lost and the texts-only sheets passed as finished)
nogeom = [n for n in counts if geom.get(n, 0) < 15 and n > 1]
thin = sorted(set(thin) | set(nogeom))
print("pdf %s: %d pages" % (out, len(pages)))
print("AIW_SHEETS " + json.dumps({"counts": counts, "thin": thin, "stray": stray, "geometry": geom, "no_geometry": nogeom}))
'''


# before LibreCAD starts: text angle 0, alignment bottom-left, width 1 (the Text dialog starts from these)
_TEXT_DEFAULTS = ("C=$HOME/.config/LibreCAD/LibreCAD.conf; [ -f \"$C\" ] && sed -i "
                  "-e '/^TextAngle=/d' -e '/^TextAlignmentT=/d' -e '/^TextWidthRelation=/d' "
                  "-e 's/^\\[Draw\\]$/[Draw]\\nTextAngle=0\\nTextAlignmentT=7\\nTextWidthRelation=1/' \"$C\"; true")


def librecad_type_command(commands: list[str], open_path: str | None = None) -> str:
    """Shell line that brings LibreCAD to the front (launching it, optionally on a file), maximises it, and types
    each command into its command line followed by Enter, the way a person would. Runs on the VM desktop.
    'TEXT|x,y|height|内容' draws a whole text: opens the Text dialog, pastes the string (Chinese works through the
    clipboard, not xdotool type), sets the height, OK, then places it at x,y."""
    import shlex
    parts = ["export DISPLAY=${DISPLAY:-:0}", _librecad_font_setup()]
    if open_path:
        safe = str(open_path).replace('"', "").replace("`", "").replace("$(", "")
        parts.append(f'(pkill -x librecad; sleep 1; {_TEXT_DEFAULTS}; nohup librecad "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}/{safe}" >/dev/null 2>&1 &)')
        parts.append("sleep 4")
    else:
        parts.append("(pgrep -x librecad >/dev/null || { " + _TEXT_DEFAULTS + "; (nohup librecad >/dev/null 2>&1 &); }; true)")
    # first start shows a Welcome (language/units) dialog; accept its defaults
    parts.append("for i in $(seq 1 25); do xdotool search --onlyvisible --name '^LibreCAD' >/dev/null 2>&1 && break; "
                 "WL=$(xdotool search --onlyvisible --name '^Welcome$' 2>/dev/null | head -1); "
                 '[ -n "$WL" ] && { xdotool windowfocus "$WL" 2>/dev/null; xdotool key --window "$WL" Return; }; sleep 1; done')
    # one LibreCAD only: a second instance (started by hand from shell) took the keystrokes and every Text
    # dialog timed out (2026-10-19 run); keep the newest, whose window is the one in front
    parts.append("(for i in 1 2 3; do N=$(pgrep -xc librecad 2>/dev/null); [ \"${N:-0}\" -gt 1 ] 2>/dev/null || break; pkill -o -x librecad; sleep 1; done; true)")
    parts.append("W=; for i in $(seq 1 20); do W=$(xdotool search --onlyvisible --name '^LibreCAD' 2>/dev/null | tail -1); "
                 '[ -n "$W" ] && break; sleep 0.5; done')
    parts.append('[ -n "$W" ] || { echo "LibreCAD window not found"; exit 3; }')
    parts.append('(xdotool windowactivate --sync "$W" 2>/dev/null || xdotool windowfocus "$W"; true); sleep 0.3')
    # maximised, so the user watching sees the whole canvas
    parts.append('(wmctrl -i -r "$(printf 0x%08x "$W")" -b add,maximized_vert,maximized_horz 2>/dev/null'
                 ' || xdotool windowmove "$W" 0 0 windowsize "$W" 100% 100% 2>/dev/null; true)')
    # LibreCAD only takes a typed point for some tools after the mouse has moved over the canvas
    parts.append('hover(){ eval "$(xdotool getwindowgeometry --shell "$W")"; '
                 'xdotool mousemove $((X+WIDTH*2/5)) $((Y+HEIGHT/2)); sleep 0.05; '
                 'xdotool mousemove $((X+WIDTH*2/5+9)) $((Y+HEIGHT/2+7)); sleep 0.05; }')
    parts.append("hover")
    # a dialog left open by an earlier step (e.g. an unfinished Text dialog) swallows every keystroke: close it
    parts.append("(for N in '^Text$' '^Hatch' '^Save Drawing As'; do for D in $(xdotool search --onlyvisible --name \"$N\" 2>/dev/null); do "
                 'xdotool windowfocus "$D" 2>/dev/null; xdotool key --window "$D" Escape; sleep 0.3; done; done; true)')
    commands = expand(commands)
    # progress file: after a timeout the loop reads how far the batch got and resumes from there
    parts.append('PF="$HOME/.cache/aiw-cad-progress"; mkdir -p "$HOME/.cache"; echo 0 > "$PF"')
    texts, dialog, bad = 0, False, []
    for i, c in enumerate(commands):
        c = str(c)
        if i:
            parts.append(f'echo {i} > "$PF"')
        if c.startswith("#BAD "):
            bad.append(c[5:])
            continue
        mp = re.match(r"(?i)^\s*pdf\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*$", c)
        if mp:  # PDF|cad/x.dxf|cad/x.pdf: LibreCAD's own dxf2pdf, one A3 page per sheet
            import base64
            fs = []
            for f in mp.groups():
                f = f.replace('"', "").replace("`", "").replace("$(", "")
                fs.append(f if f.startswith("/") else "${VM_AGENT_FILES_ROOT:-$HOME/workspace}/" + f.lstrip("~/").removeprefix("workspace/"))
            b64 = base64.b64encode(SPLIT_PDF_SCRIPT.encode()).decode()
            parts.append(f'echo {b64} | base64 -d > /tmp/aiw_splitpdf.py && timeout 280 python3 /tmp/aiw_splitpdf.py "{fs[0]}" "{fs[1]}" {MIN_SHEET_ENTITIES}')
            continue
        t = _parse_text(c)
        if t:
            pt, h, body = t
            texts += 1
            # the same steps a person takes, with no idle waits: poll for the dialog instead of sleeping
            parts.append(
                "xdotool key Escape Escape; sleep 0.08; "
                f"printf %s {shlex.quote(body)} | xclip -selection clipboard -i >/dev/null 2>&1; "
                f"xdotool key ctrl+m; xdotool type --delay {TYPE_DELAY_MS} -- text; xdotool key Return; "
                "D=; for i in $(seq 1 60); do D=$(xdotool search --onlyvisible --name '^Text$' 2>/dev/null | tail -1); "
                '[ -n "$D" ] && break; sleep 0.05; done; '
                '[ -n "$D" ] || { echo "Text dialog did not open"; exit 4; }; '
                # the first Text dialog of a batch can take a moment to load its font list before it takes a paste
                f"sleep {0.6 if texts == 1 else 0.2}; "
                "xdotool key ctrl+a; "
                f"if command -v xclip >/dev/null; then xdotool key ctrl+v; else xdotool type --delay 30 -- {shlex.quote(body)}; fi; "
                # Tab order teText > leHeight > leAngle. The angle is remembered from the last text, so one stray value
                # rotated every later text (2026-10-19 PDF): set height AND angle every time
                f"sleep 0.08; xdotool key Tab ctrl+a; xdotool type --delay {TYPE_DELAY_MS} -- {shlex.quote(h)}; "
                f"xdotool key Tab ctrl+a; xdotool type --delay {TYPE_DELAY_MS} -- 0; xdotool key Return; "
                "for i in $(seq 1 60); do xdotool search --onlyvisible --name '^Text$' >/dev/null 2>&1 || break; sleep 0.05; done; "
                f"hover; xdotool key ctrl+m; xdotool type --delay {TYPE_DELAY_MS} -- {shlex.quote(pt)}; xdotool key Return; sleep 0.2; "
                "xdotool key Escape; sleep 0.05")
            continue
        m = re.match(r"(?i)^\s*save\s*\|\s*(.+?)\s*$", c)
        if m:  # File > Save as... (ctrl+shift+s is Save ALL in LibreCAD, and shortcuts are dead while the command line has focus)
            path = m.group(1).replace('"', "").replace("`", "").replace("$(", "")
            if not path.startswith("/"):
                path = "${VM_AGENT_FILES_ROOT:-$HOME/workspace}/" + path.lstrip("~/").removeprefix("workspace/")
            parts.append(
                # LibreCAD restarted on an empty new drawing would overwrite the real file with nothing (2026-10-19:
                # an 832 KB DXF became 15 KB): refuse, the agent reopens the file with open= first
                f'case "$(xdotool getwindowname "$W" 2>/dev/null)" in *unnamed*) [ "$(stat -c%s "{path}" 2>/dev/null || echo 0)" -gt 30000 ] && '
                f'{{ echo "SAVE REFUSED: LibreCAD shows an empty new drawing, saving would wipe {path}. Send cad_command with '
                f'open=<that file> to reopen it, then continue"; exit 7; }};; esac; '
                f'mkdir -p "$(dirname "{path}")"; xdotool key Escape; sleep 0.15; xdotool key Escape; sleep 0.2; '
                "xdotool key alt+f; sleep 0.6; xdotool key a; "
                "SD=; for i in $(seq 1 20); do SD=$(xdotool search --onlyvisible --name 'Save Drawing As' 2>/dev/null | tail -1); "
                '[ -n "$SD" ] && break; sleep 0.2; done; [ -n "$SD" ] || { echo "Save As dialog did not open"; exit 5; }; '
                # paste the path: xdotool type drops Chinese characters (丹防援荣图.dxf for 兴丹路消防救援站荣誉室施工图.dxf)
                f'sleep 0.3; printf %s "{path}" | xclip -selection clipboard -i >/dev/null 2>&1; '
                f'xdotool key ctrl+a; if command -v xclip >/dev/null; then xdotool key ctrl+v; else xdotool type --delay 15 -- "{path}"; fi; '
                'sleep 0.2; xdotool key Return; sleep 1.5; '
                # overwrite confirmation, if any
                "xdotool search --onlyvisible --name 'Save Drawing As' >/dev/null 2>&1 && { xdotool key alt+y; sleep 0.3; xdotool key Return; sleep 1; }; "
                f'[ -s "{path}" ] && echo "saved {path}" || {{ echo "save failed: {path} not written"; exit 5; }}')
            continue
        if c.upper() in ("ESC", "ESCAPE"):
            # one Escape only steps back to the tool's first prompt ("Specify first point"); two leave the tool
            parts.append("xdotool key Escape; sleep 0.15; xdotool key Escape; sleep 0.2")
            continue
        # ctrl+m = "Focus on Command Line": Escape inside the command line drops keyboard focus, after which
        # typed commands went nowhere
        parts.append(f"xdotool key ctrl+m; xdotool type --delay {TYPE_DELAY_MS} -- {shlex.quote(c)}; xdotool key Return; sleep {STEP_PAUSE_S}")
    last = str(commands[-1]).strip().lower() if commands else ""
    if last.startswith("save") or last.startswith("pdf"):
        last = "za"  # no zoom after a save
    dialog = last in ("text", "txt", "mtext", "mtxt", "hatch", "ha")  # a dialog is now open; leave it to the agent
    if not dialog and not (last.startswith("zoom") or last in ("za", "zw", "zp", "zv")):
        # fit the whole drawing so the screenshot shows what was drawn (a fresh view only shows ~0..1000 mm)
        parts.append("xdotool key ctrl+m; xdotool type --delay 25 -- za; xdotool key Return; sleep 0.5")  # zoom resumes the running tool
    tail = "; the dialog is open now, fill it in" if dialog else ", then zoomauto"
    if texts:
        tail += f" ({texts} texts placed)"
    if bad:
        tail += f"; SKIPPED malformed macros: {bad[:5]}"
    parts.append(f'echo {len(commands)} > "$PF"')
    parts.append("echo " + shlex.quote(f"typed {len(commands)} commands{tail}"))
    return " && ".join(parts)


# --- revising a finished drawing ---------------------------------------------------------------------------------
# The user checks the set and asks for local fixes ("左侧墙的标题底板要放到顶上", "节点图标注压线了"). Those are made
# on the drawing that exists: erase the wrong part, draw the right one, save and export again. Nothing is redrawn.
_CAD_REVISE = re.compile(r"修改|修正|改一下|改成|改为|改到|改掉|调整|局部|有误|不对|错了|有问题|挪|移到|移动|往(上|下|左|右)|"
                         r"删掉|删除|去掉|补上|补画|加上|增加|添加|替换|换成|放大|缩小|对齐|压线|重叠|"
                         r"在(原|已有|画好|现有)的?(图|基础|图纸)", re.I)
_CAD_REDRAW = re.compile(r"重新画|重画|从头|全部重|重新出图|redraw|from scratch", re.I)
_CAD_TOPIC = re.compile(r"图|墙|标注|尺寸|文字|字|引线|节点|标题|板块|版面|线|框|天花|吊顶|门|踢脚|灯带|做法|比例|图例|dxf|pdf", re.I)
_CAD_CONTEXT = re.compile(r"施工图|\.dxf|librecad|cad_command|立面图|平面图", re.I)


def cad_mode(prompt: str, context: str | None = None) -> str | None:
    """'fresh' (start from an empty cad/ folder), 'revise' (edit the drawing the thread already has), or None (not a
    drawing task). A follow-up in a drawing thread is a revision unless it asks for a redraw or is a whole new brief:
    the user re-sent the full 12-sheet prompt in the same thread before, and that must still start fresh."""
    p = prompt or ""
    in_thread = bool(_CAD_CONTEXT.search(context or ""))
    full_brief = len(p) > 600 or "FRAME|" in p or "要出的图" in p
    if in_thread and not full_brief and not _CAD_REDRAW.search(p) and (is_cad_task(p) or (_CAD_REVISE.search(p) and _CAD_TOPIC.search(p))):
        return "revise"
    if is_cad_task(p):
        return "fresh"
    return None


_EDIT = re.compile(r"(?i)^\s*(erase|erasetext|list)\s*\|(.*)$")


def split_edits(commands: list[str], sheet: int = 0) -> tuple[list[dict], list[str], int]:
    """Pull 'ERASE|x1,y1|x2,y2', 'ERASETEXT|内容' and 'LIST|x1,y1|x2,y2' (or 'LIST|n' for sheet n) out of a batch.
    Window points follow the same rule as drawing points: sheet-local after FRAME|n / SHEET|n, or absolute.
    Returns (edits, the drawing commands left, the sheet current at the end)."""
    edits, rest = [], []
    cur = sheet
    for c in commands:
        c = str(c)
        ms = _SHEET.match(c)
        if ms:
            cur = max(0, int(float(ms.group(2))))
            rest.append(c)
            continue
        m = _EDIT.match(c)
        if not m:
            rest.append(c)
            continue
        op, args = m.group(1).lower(), [a.strip() for a in m.group(2).split("|")]
        ox = cur * SHEET_PITCH
        if op == "erasetext":
            if args and args[0]:
                edits.append({"op": "erasetext", "text": args[0], "sheet": cur})
            continue
        if op == "list" and len(args) == 1 and re.fullmatch(r"\d+", args[0]):
            n = int(args[0])
            edits.append({"op": "list", "box": [n * SHEET_PITCH, 0, n * SHEET_PITCH + SHEET_W, SHEET_H], "sheet": n})
            continue
        try:
            (x1, y1), (x2, y2) = _xy(args[0]), _xy(args[1])
        except (ValueError, IndexError):
            rest.append(f"#BAD {c}")
            continue
        if ox > 0 and max(x1, x2) < ox - 2000:  # sheet-local window
            x1, x2 = x1 + ox, x2 + ox
        box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        edits.append({"op": op, "box": box, "sheet": int(box[0] // SHEET_PITCH)})
    return edits, rest, cur


_EDIT_PY = r'''
import json, sys
import ezdxf
from ezdxf import bbox
path, edits = sys.argv[1], json.loads(sys.argv[2])
P, W = 25000, 21000
doc = ezdxf.readfile(path)
msp = doc.modelspace()
cache = bbox.Cache()
def ext(e):
    try:
        b = bbox.extents([e], cache=cache, fast=True)
        return (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y) if b.has_data else None
    except Exception:
        return None
def body(e):
    t = e.dxftype()
    if t == "TEXT": return e.dxf.text
    if t == "MTEXT": return e.plain_text()
    if t == "DIMENSION":
        try: return "dim " + str(round(e.get_measurement()))
        except Exception: return "dim"
    return ""
def inside(b, w): return b and b[0] >= w[0] - 1 and b[1] >= w[1] - 1 and b[2] <= w[2] + 1 and b[3] <= w[3] + 1
def r(v): return int(round(v))
out = {"erased": 0, "list": [], "texts": []}
changed = False
for ed in edits:
    if ed["op"] in ("erase", "list"):
        w = ed["box"]
        hit = [e for e in msp if inside(ext(e), w)]
        if ed["op"] == "erase":
            for e in hit: msp.delete_entity(e)
            out["erased"] += len(hit); changed = changed or bool(hit)
        else:
            ox = (int(w[0]) // P) * P
            for e in hit[:150]:
                b = ext(e); s = body(e)
                out["list"].append(f"{e.dxftype()} {r(b[0]-ox)},{r(b[1])}..{r(b[2]-ox)},{r(b[3])}" + (f" '{s[:40]}'" if s else ""))
            if len(hit) > 150: out["list"].append(f"... {len(hit) - 150} more")
    elif ed["op"] == "erasetext":
        n = ed.get("sheet")
        for e in [e for e in msp if e.dxftype() in ("TEXT", "MTEXT") and ed["text"] in body(e)]:
            b = ext(e)
            if n is not None and b and not (n * P - 1 <= b[0] <= n * P + W + 1):
                continue
            msp.delete_entity(e); out["erased"] += 1; changed = True
if "--texts" in sys.argv:  # every text on the drawing, for the layout check of the revision
    for e in msp.query("TEXT"):
        try:
            out["texts"].append([r(e.dxf.insert.x), r(e.dxf.insert.y), round(e.dxf.height), e.dxf.text[:60]])
        except Exception:
            pass
    for e in msp.query("MTEXT"):
        try:
            h = e.dxf.char_height
            for k, line in enumerate(e.plain_text().split("\n")[:20]):  # top-left anchored lines, downwards
                out["texts"].append([r(e.dxf.insert.x), r(e.dxf.insert.y - h * (1 + 1.6 * k)), round(h), line[:60]])
        except Exception:
            pass
if changed:
    doc.saveas(path)
print("AIW_EDIT " + json.dumps(out, ensure_ascii=False))
'''


def edit_command(path: str, edits: list[dict], texts: bool = False) -> str:
    """Shell line that applies ERASE/ERASETEXT/LIST to the saved DXF with ezdxf (reading and deleting, never drawing)
    and prints 'AIW_EDIT {json}'. LibreCAD is reopened on the file afterwards by the caller."""
    import json as _j
    import shlex
    safe = str(path).replace('"', "").replace("`", "").replace("$(", "")
    return (f'cd "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}" && python3 -c {shlex.quote(_EDIT_PY)} '
            f'"{safe}" {shlex.quote(_j.dumps(edits, ensure_ascii=False))}' + (" --texts" if texts else ""))


def parse_edit(out: str) -> dict | None:
    import json as _j
    m = re.search(r"AIW_EDIT (\{.*\})", out or "")
    if not m:
        return None
    try:
        return _j.loads(m.group(1))
    except ValueError:
        return None


def seed_text_boxes(texts: list) -> dict:
    """Text boxes of a drawing that already exists, so the layout check of a revision knows what is on the sheet."""
    items = []
    for t in texts or []:
        try:
            x, y, h, body = float(t[0]), float(t[1]), float(t[2]), str(t[3])
        except (ValueError, IndexError, TypeError):
            continue
        if body.strip():
            items.append((int(x // SHEET_PITCH), f"TEXT|{_pt(x, y)}|{_f(h)}|{body}"))
    _, boxes = layout_issues(items, {})
    out: dict = {}
    for sh, b, body in boxes:
        out.setdefault(sh, []).append((b, body))
    return out
