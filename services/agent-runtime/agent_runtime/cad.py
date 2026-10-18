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


def _macro(c: str) -> list[str] | None:
    """FRAME / NOTE / DIMH / DIMV / LEADER -> plain LibreCAD commands, or None when c is not a macro."""
    m = re.match(r"(?i)^\s*(frame|note|dimh|dimv|leader)\s*\|(.*)$", c)
    if not m:
        return None
    kind, bits = m.group(1).upper(), [b.strip() for b in m.group(2).split("|")]
    out: list[str] = []
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
        if proj:
            out.append(f"TEXT|{_pt(bx + 150, y1 + bh / 2 + 330)}|220|{proj}")
        out.append(f"TEXT|{_pt(bx + 150, y1 + 300)}|350|{title}")
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


def place(commands: list[str], sheet: int = 0) -> tuple[list[str], int, int]:
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
        for e in expand([c]):
            t = _parse_text(e)
            if t:
                pt, h, body = t
                e = f"TEXT|{shift(pt)}|{h}|{body}"
            elif _ABS_POINT.match(e):
                e = shift(e)
            out.append(e)
    return out, sheet, moved


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
    subprocess.run(["librecad", "dxf2pdf", "-a", "-m", "-p", "420x297", "-f", "0,0,0,0", dxf],
                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    made = dxf[:-4] + ".pdf"
    if made != pdf and os.path.exists(made):
        shutil.move(made, pdf)
    return os.path.exists(pdf) and os.path.getsize(pdf) > 0
counts, pages = {}, []
try:
    import ezdxf
    from ezdxf import bbox
    doc = ezdxf.readfile(src)
    groups = {}
    for e in doc.modelspace():
        try:
            b = bbox.extents([e])
            if not b.has_data:
                continue
            n = int(b.center.x // PITCH)
        except Exception:
            continue
        groups.setdefault(n, set()).add(e.dxf.handle)
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
print("pdf %s: %d pages" % (out, len(pages)))
print("AIW_SHEETS " + json.dumps({"counts": counts, "thin": thin}))
'''


def librecad_type_command(commands: list[str], open_path: str | None = None) -> str:
    """Shell line that brings LibreCAD to the front (launching it, optionally on a file), maximises it, and types
    each command into its command line followed by Enter, the way a person would. Runs on the VM desktop.
    'TEXT|x,y|height|内容' draws a whole text: opens the Text dialog, pastes the string (Chinese works through the
    clipboard, not xdotool type), sets the height, OK, then places it at x,y."""
    import shlex
    parts = ["export DISPLAY=${DISPLAY:-:0}", _librecad_font_setup()]
    if open_path:
        safe = str(open_path).replace('"', "").replace("`", "").replace("$(", "")
        parts.append(f'(pkill -x librecad; sleep 1; nohup librecad "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}/{safe}" >/dev/null 2>&1 &)')
        parts.append("sleep 4")
    else:
        parts.append("(pgrep -x librecad >/dev/null || (nohup librecad >/dev/null 2>&1 &); true)")
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
            parts.append(f'echo {b64} | base64 -d > /tmp/aiw_splitpdf.py && python3 /tmp/aiw_splitpdf.py "{fs[0]}" "{fs[1]}" {MIN_SHEET_ENTITIES}')
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
                f"sleep 0.08; xdotool key Tab ctrl+a; xdotool type --delay {TYPE_DELAY_MS} -- {shlex.quote(h)}; xdotool key Return; "
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
