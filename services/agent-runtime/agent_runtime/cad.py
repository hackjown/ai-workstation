"""cad_draw / read_image support: drawings are produced by code (ezdxf on the VM), never by mouse strokes,
so every dimension is exact. The agent writes only the drawing body; this module wraps it with a helper
prelude (layers, Chinese text style, dimension style, walls, frame, title block, notes) and an epilogue that
saves the DXF and renders a PDF + PNG preview."""
from __future__ import annotations

import json
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


def script_drawing_allowed(prompt: str) -> bool:
    """cad_draw (ezdxf script) only when the user asked to DRAW with a script; a negated mention
    ("禁止用脚本出图") or reading files with a script does not count. Otherwise draw in LibreCAD."""
    p = _NEGATED.sub("", prompt or "")
    return bool(_SCRIPT_DRAW.search(p))


def librecad_type_command(commands: list[str], open_path: str | None = None) -> str:
    """Shell line that brings LibreCAD to the front (launching it, optionally on a file) and types each command
    into its command line followed by Enter, the way a person would. Runs on the VM desktop (DISPLAY=:0)."""
    import shlex
    parts = ["export DISPLAY=${DISPLAY:-:0}"]
    if open_path:
        safe = str(open_path).replace('"', "").replace("`", "").replace("$(", "")
        parts.append(f'(pkill -x librecad; sleep 1; nohup librecad "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}/{safe}" >/dev/null 2>&1 &)')
        parts.append("sleep 4")
    else:
        parts.append("(pgrep -x librecad >/dev/null || (nohup librecad >/dev/null 2>&1 &); true)")
        parts.append("for i in 1 2 3 4 5 6 7 8; do xdotool search --onlyvisible --class librecad >/dev/null 2>&1 && break; sleep 1; done")
    parts.append("W=$(xdotool search --onlyvisible --class librecad | tail -1)")
    parts.append('[ -n "$W" ] || { echo "LibreCAD window not found"; exit 3; }')
    parts.append('xdotool windowactivate --sync "$W"; sleep 0.3')
    for c in commands:
        c = str(c)
        if c.upper() in ("ESC", "ESCAPE"):
            parts.append("xdotool key Escape; sleep 0.2")
            continue
        parts.append(f"xdotool type --delay 25 -- {shlex.quote(c)}; xdotool key Return; sleep 0.35")
    parts.append(f"echo typed {len(commands)} commands")
    return " && ".join(parts)
