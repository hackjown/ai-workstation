"""TTF/OTF/TTC -> LibreCAD LFF font (glyph outlines as polylines), so LibreCAD can draw Chinese text.
LibreCAD only renders its own .lff stroke fonts, and the ones it ships have no hanzi (text shows as boxes).
Usage: ttf2lff.py <font.ttf|font.ttc|auto> out.lff [name]    ('auto' picks Noto Sans CJK SC / Noto Sans SC)
Covers ASCII, GB2312 (6763 hanzi + symbols) and common CJK punctuation; about 12 MB."""
import glob
import sys
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

def charset():
    cs = set(range(0x20, 0x7f))
    cs |= set(range(0x3000, 0x3020)) | set(range(0xff01, 0xff5f)) | {0x00b0, 0x00b1, 0x00d7, 0x00f7, 0x2014, 0x2018, 0x2019, 0x201c, 0x201d, 0x2026, 0x2103, 0x2116, 0x2160, 0x2161, 0x2162, 0x2163, 0x2460, 0x2461, 0x2462, 0x2463, 0x2464, 0x25a0, 0x25a1, 0x25b2, 0x25cb, 0x25cf, 0x2605, 0x00b7, 0x2236, 0x03a6, 0x2300}
    for hi in range(0xb0, 0xf8):          # GB2312 hanzi (6763)
        for lo in range(0xa1, 0xff):
            try: cs.add(ord(bytes([hi, lo]).decode("gb2312")))
            except Exception: pass
    for hi in range(0xa1, 0xaa):          # GB2312 symbols
        for lo in range(0xa1, 0xff):
            try: cs.add(ord(bytes([hi, lo]).decode("gb2312")))
            except Exception: pass
    return sorted(cs)

def open_font(src):
    if src == "auto":
        cands = (glob.glob("/usr/share/fonts/**/NotoSansCJK*-Regular.tt[cf]", recursive=True)
                 + glob.glob("/usr/share/fonts/**/NotoSansSC*.[ot]tf", recursive=True)
                 + glob.glob("/usr/share/fonts/**/NotoSansCJK*-Medium.tt[cf]", recursive=True)
                 + glob.glob("/usr/share/fonts/**/wqy-*.tt[cf]", recursive=True))
        if not cands:
            sys.exit("no CJK font found (apt install fonts-noto-cjk)")
        src = cands[0]
    if src.lower().endswith((".ttc", ".otc")):
        from fontTools.ttLib import TTCollection
        fonts = TTCollection(src).fonts
        for ft in fonts:  # prefer the Simplified Chinese face
            if any(" SC" in str(r) or "SC " in str(r) for r in ft["name"].names if r.nameID in (1, 4)):
                return ft
        return fonts[0]
    return TTFont(src)


def main(src, dst, name="NotoSansSC"):
    f = open_font(src); gs = f.getGlyphSet(); cmap = f.getBestCmap()
    s = 11.25 / f["head"].unitsPerEm  # cap height ~9 units, like the LibreCAD fonts
    def P(p): return f"{round(p[0]*s, 2):g},{round(p[1]*s, 2):g}"
    out = ["# Format:            LibreCAD Font 1", "# Creator:           ttf2lff.py", f"# Name:              {name}",
           "# Encoding:          UTF-8", "# LetterSpacing:     1", "# WordSpacing:       4", "# LineSpacingFactor: 1.2",
           "# Author:            Noto Sans SC (SIL OFL)", "# License:           OFL 1.1", ""]
    n = 0
    for cp in charset():
        g = cmap.get(cp)
        if not g: continue
        pen = RecordingPen(); gs[g].draw(pen)
        polys, cur, start = [], [], None
        for op, args in pen.value:
            if op == "moveTo":
                if len(cur) > 1: polys.append(cur)
                cur = [args[0]]; start = args[0]
            elif op == "lineTo": cur.append(args[0])
            elif op in ("curveTo", "qCurveTo"):
                p0 = cur[-1]; pts = list(args)
                if op == "curveTo" and len(pts) == 3:
                    a, b, c = pts
                    for i in range(1, 5):
                        t = i / 4; u = 1 - t
                        cur.append((u**3*p0[0]+3*u*u*t*a[0]+3*u*t*t*b[0]+t**3*c[0], u**3*p0[1]+3*u*u*t*a[1]+3*u*t*t*b[1]+t**3*c[1]))
                else:  # quadratic spline (implied on-curve points)
                    ctrl = pts[:-1]; end = pts[-1]
                    segs = []
                    for i, c in enumerate(ctrl):
                        e = end if i == len(ctrl)-1 else ((c[0]+ctrl[i+1][0])/2, (c[1]+ctrl[i+1][1])/2)
                        segs.append((c, e))
                    for c, e in segs:
                        q0 = cur[-1]
                        for i in range(1, 4):
                            t = i/3; u = 1-t
                            cur.append((u*u*q0[0]+2*u*t*c[0]+t*t*e[0], u*u*q0[1]+2*u*t*c[1]+t*t*e[1]))
            elif op in ("closePath", "endPath"):
                if op == "closePath" and start and cur and cur[-1] != start: cur.append(start)
                if len(cur) > 1: polys.append(cur)
                cur = []
        if len(cur) > 1: polys.append(cur)
        ch = chr(cp)
        out.append(f"[{cp:04x}] {ch if cp > 0x20 else ''}".rstrip())
        lines = []
        for poly in polys:
            pts = []
            for p in poly:
                q = P(p)
                if not pts or pts[-1] != q: pts.append(q)
            if len(pts) > 1: lines.append(";".join(pts))
        if not lines:  # space etc: LibreCAD needs a width; use an invisible point pair
            adv = f["hmtx"][g][0]*s
            lines.append(f"{round(adv,2):g},0;{round(adv,2):g},0")
        out += lines; out.append(""); n += 1
    open(dst, "w", encoding="utf-8").write("\n".join(out))
    print("glyphs", n)

main(*sys.argv[1:])
