"""Drawing pipeline (3.8.7): read the source rigorously -> structured spec -> deterministic generator -> verify.

Why: task-a12 (a 108 m² floor plan redrawn from a sales photo) ran 187 steps and was stopped by the user. The agent
asked free-form questions about the whole photo 32 times and got contradictory numbers (9300 vs 9500, an invented
3x3 grid); it hand-typed coordinates in 122 LibreCAD batches, 35 of them sent back by the layout check for text
overlap; it patched with ERASE windows 25 times (orphans, lost walls); and nothing ever compared the result with
the photo. Here the reading is done one dimension chain at a time, twice, with a pixel cross-check, the drawing
lives in a spec file, and skills/cad-drawing/drawkit.py regenerates the whole drawing from it on every round.

This module is pure helpers (no I/O): the kit files to ship to the computer, the shell commands, the crop/zoom
for drawing_read, number parsing and the comparison of two reads, and the text summary of a build report.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path

KIT_DIR = Path(os.environ.get("AIW_DRAWKIT_DIR") or Path(__file__).resolve().parents[3] / "skills" / "cad-drawing")
KIT_FILES = ("drawkit.py", "drawauto.py", "SKILL.md", "examples/xinda-g-108.spec.json")
REMOTE = ".aiw/cad-drawing"          # under the computer's ~/workspace (files_write root)
ROOT = '"${VM_AGENT_FILES_ROOT:-$HOME/workspace}"'
MAX_ROUNDS = int(os.environ.get("AIW_DRAW_MAX_ROUNDS", "3"))


def kit_files() -> dict:
    out = {}
    for rel in KIT_FILES:
        p = KIT_DIR / rel
        if p.exists():
            out[rel] = p.read_text(encoding="utf-8")
    return out


def kit_version() -> str:
    h = hashlib.sha1()
    for rel, txt in sorted(kit_files().items()):
        h.update(rel.encode()); h.update(txt.encode("utf-8"))
    return h.hexdigest()[:12]


def version_probe() -> str:
    return f"cat {ROOT}/{REMOTE}/VERSION 2>/dev/null; true"


def install_command(version: str) -> str:
    """after the files are written: mirror them as a skill folder and make sure the dependencies are there."""
    return (f"cd {ROOT}/{REMOTE} && echo {version} > VERSION && mkdir -p \"$HOME/.claude/skills/cad-drawing/examples\" && "
            f"cp -f drawkit.py drawauto.py SKILL.md \"$HOME/.claude/skills/cad-drawing/\" && "
            f"cp -f examples/*.json \"$HOME/.claude/skills/cad-drawing/examples/\" 2>/dev/null; "
            f"M=\"$HOME/.config/aiw/drawkit-ready-v2\"; if [ ! -f \"$M\" ]; then "
            f"python3 drawkit.py ensure && mkdir -p \"$(dirname $M)\" && touch \"$M\"; else echo deps-ok; fi; "
            f"python3 -c 'import ezdxf, PIL, numpy' ")


def build_command(spec: str, out: str = "cad", name: str = "plan", source: str | None = None, task: str | None = None) -> str:
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"
    cmd = (f"cd {ROOT} && python3 {REMOTE}/drawkit.py build {q(spec)} --out {q(out)} --name {q(name)}")
    if source:
        cmd += f" --source {q(source)}"
    if task:
        cmd += f" --task {q(task)}"
    return cmd


def parse_result(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("DRAWKIT_RESULT "):
            try:
                return json.loads(line[len("DRAWKIT_RESULT "):])
            except ValueError:
                return None
    return None


def summary(rep: dict, rounds: int, out_dir: str = "cad") -> str:
    files = rep.get("files") or {}
    rel = lambda f: f if not f or f.startswith(out_dir + "/") or f.startswith("/") else f"{out_dir}/{f}"
    lines = [f"drawing_build round {rounds}/{MAX_ROUNDS}: {'ALL CHECKS PASS' if rep.get('ok') else 'CHECKS FAILED'} "
             f"(scale {rep.get('scale')}, {files.get('entities', '?')} entities)."]
    if rep.get("errors"):
        lines.append("ERRORS: " + " | ".join(rep["errors"][:12]))
    if rep.get("warnings"):
        lines.append("warnings: " + " | ".join(rep["warnings"][:8]))
    if rep.get("info"):
        lines.append("info: " + " | ".join(rep["info"][:5]))
    if rep.get("overlay_score"):
        lines.append(f"overlay: {rep['overlay_score']}")
    names = [rel(files[k]) for k in ("png", "dxf", "overlay", "librecad_pdf") if files.get(k)]
    lines.append("files: " + ", ".join(names) + f", {out_dir}/report.json")
    return "\n".join(lines)


# ---- drawing_read: one region, read twice, compared --------------------------------------------------------------

CHAIN_Q = [
    ("这是工程图上一条尺寸链的放大局部（已转成横排）。请从左到右逐个读出每一段的尺寸数字（毫米），"
     "两条短斜线/界线之间是一段，相邻的小段也要分开读。只输出 JSON：{\"values\": [数字, ...]}，看不清的写 null。"),
    ("请仔细数这条尺寸线上有几个数字，然后从右往左依次读出每个数字（毫米）。"
     "只输出 JSON：{\"count\": 个数, \"right_to_left\": [数字, ...]}，看不清的写 null。"),
    ("放大后的尺寸链：逐个数字读，从左到右，每个数字单独确认它的每一位（3 和 8、0 和 6、0 和 9 容易混）。"
     "只输出 JSON：{\"values\": [数字, ...]}。"),
]
TEXT_Q = [
    "这是图纸的放大局部。逐个列出图上能看到的所有文字（房间名、标注、编号），每个一行，按从上到下、从左到右。只输出 JSON：{\"texts\": [...]}",
    "把这块图里的文字按出现位置从左到右、从上到下列出来，不要补图上没有的字。只输出 JSON：{\"texts\": [...]}",
]


def _json_in(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    return {}


def numbers_from(text: str, key_order=("values", "right_to_left")) -> list | None:
    d = _json_in(text)
    for k in key_order:
        if isinstance(d.get(k), list):
            vals = []
            for v in d[k]:
                try:
                    vals.append(None if v is None else int(round(float(str(v).replace(",", "")))))
                except ValueError:
                    vals.append(None)
            return vals[::-1] if k == "right_to_left" else vals
    nums = re.findall(r"(?<![\d.])\d{2,6}(?![\d.])", text or "")
    return [int(n) for n in nums] or None


def texts_from(text: str) -> list:
    d = _json_in(text)
    v = d.get("texts")
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [ln.strip(" -•\t") for ln in (text or "").splitlines() if ln.strip()]


def crop_zoom(img, box, kind: str = "chain", target: int = 1600):
    """crop the region, enlarge it to about `target` px on its long side; a vertical chain is turned to read
    left->right (= bottom->top in the source, as such numbers are written)."""
    from PIL import Image
    W, H = img.size
    a, b, c_, d = [int(round(float(v))) for v in box]
    x0, x1 = max(0, min(a, c_)), min(W, max(a, c_))
    y0, y1 = max(0, min(b, d)), min(H, max(b, d))
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise ValueError(f"box {box} is empty inside the {W}x{H} image")
    c = img.crop((x0, y0, x1, y1))
    vertical = kind == "chain" and c.height > 1.5 * c.width
    if vertical:
        c = c.transpose(Image.ROTATE_270)
    k = max(2, min(12, int(target / max(1, max(c.size)))))
    c = c.resize((c.width * k, c.height * k), Image.LANCZOS)
    return c, vertical, k


def jpeg_b64(img) -> str:
    import base64
    buf = io.BytesIO(); img.convert("RGB").save(buf, "JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode()


def agree(a: list | None, b: list | None) -> bool:
    return bool(a) and a == b and None not in a


def chain_verdict(reads: list, vertical: bool) -> dict:
    """reads: number lists as read left->right on the (turned) crop. Returns values in the source's reading order
    (left->right for a horizontal chain, top->bottom for a vertical one)."""
    fix = (lambda v: list(reversed(v)) if v else v) if vertical else (lambda v: v)
    rs = [fix(r) for r in reads if r]
    out = {"reads": rs, "vertical": vertical}
    if len(rs) >= 2 and agree(rs[0], rs[1]):
        out.update(ok=True, values=rs[0], total=sum(rs[0]))
        return out
    if len(rs) >= 3:
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if agree(rs[i], rs[j]):
                    out.update(ok=True, values=rs[i], total=sum(rs[i]), note=f"reads {i + 1} and {j + 1} agree")
                    return out
    out.update(ok=False)
    if len(rs) >= 2 and rs[0] and rs[1] and len(rs[0]) == len(rs[1]):
        out["differ_at"] = [i for i, (p, q) in enumerate(zip(rs[0], rs[1])) if p != q]
    return out
