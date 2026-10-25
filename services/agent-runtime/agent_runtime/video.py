"""Explainer videos made from a document (PPTX/PDF) with huashu-motion.

task-8bd (deepseek-v4-pro, 2026-10-25) shipped a 1:46 cut of a 16-page deck. Its own ask_user default was a
"60 s Apple-keynote quick cut"; the user did not answer within the window, so it ran with that default and used about
8 of the 16 pages. The past-dividend (fulfilment ratio) pages, the guaranteed year-25 breakeven, the three withdrawal
plans and currency conversion were all dropped. The audio sat at -25.7 LUFS mono, and "non-guaranteed" was a separate
near-empty screen instead of a badge on the figures. The reference cut the user liked (another model, 4:58) has
numbered chapters for every page, builds each screen element by element, puts a 非保证 badge on every illustrated
number, gives offer deadlines, ends on the full disclaimer, and is stereo at -16 LUFS.
It also lost ~25 min reading om's source and started `om render` 4 times because the 60 s shell timeout killed it.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Optional

DOC = re.compile(r"[^\s'\"]+\.(pptx?|pdf|key|docx?)\b", re.I)
SHORT_ASK = re.compile(r"(\d+)\s*(秒|s\b|sec)|精简|短视频|快剪|竖屏|抖音|一分钟|1分钟|预告", re.I)


def doc_path(prompt: str) -> Optional[str]:
    m = DOC.search(prompt or "")
    return m.group(0) if m else None


def brief(prompt: str) -> str:
    doc = doc_path(prompt)
    short = bool(SHORT_ASK.search(prompt or ""))
    lines = [
        "0. (video brief) huashu-motion needs Node 22+, Chrome, FFmpeg. First run `~/.claude/skills/huashu-motion/bin/om "
        "doctor`; if anything is missing run `bash ~/ai-workstation/deploy/vm/motion-setup.sh`. Use the om commands as "
        "SKILL.md documents them (om new / voice / check / qa / render); do NOT read om's own source code.",
    ]
    if doc and not short:
        lines.append(
            "EXPLAINER OF A DOCUMENT: cover EVERY page/section, in order, as numbered chapters (01 产品概览, 02 ...). "
            "Length follows the content (about 15-20 s per page, so a 16-page deck runs 4-5 min); never a 60 s teaser "
            "unless the user asks for one. The default you offer in ask_user is this full version, and that same default is "
            "what you do if the user does not answer. Before writing scenes, write huashu-film/coverage.md: one line per "
            "page, `第N页 <title> -> 场景 <id>`; any page you merge says where it went. Delivery is refused until every page "
            "is listed.")
    lines.append(
        "SCREENS: chapter title top-left on every screen; build each screen element by element (cards in one by one, "
        "bars grow, counters roll, timelines draw) instead of a static slide; use the whole frame. The narration explains "
        "(what the number means, why), it does not read the screen word for word; subtitles are short phrases.")
    lines.append(
        "FINANCE/INSURANCE FACTS: copy every figure exactly as the document has it (no rounding, no swapping rows); "
        "always the currency unit (万美元, not 万); a 非保证/演示 badge ON every illustrated or projected figure, guaranteed "
        "vs non-guaranteed visually separate; offers carry their deadline and 以官方条款为准; past dividend / fulfilment "
        "ratio pages are kept with their years; end on the document's full disclaimer.")
    lines.append(
        "AUDIO: narration + a quiet music bed (om sound), stereo, loudness -16 LUFS: "
        "`ffmpeg -i in.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11 -ac 2 -c:v copy out.mp4`. Run `om check` and `om qa` before "
        "send_file.")
    lines.append(
        "RENDER: `om render` runs for minutes. Pass shell timeout 1800, or start it ONCE with nohup and poll the log; never "
        "start a second render while one runs.")
    return " ".join(lines)


QA_PY = r'''
import json, os, re, subprocess, sys, glob
v, doc = sys.argv[1], sys.argv[2]
v = os.path.expanduser(v)
if not os.path.isabs(v):
    for base in (os.path.expanduser("~/workspace"), os.path.expanduser("~/workspace/huashu-film")):
        if os.path.exists(os.path.join(base, v)):
            v = os.path.join(base, v); break
out = {"video": v}
def run(a):
    return subprocess.run(a, capture_output=True, text=True, timeout=300)
try:
    p = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,channels",
                        "-of", "json", v]).stdout or "{}")
    out["duration"] = float(p.get("format", {}).get("duration") or 0)
    out["channels"] = max([s.get("channels") or 0 for s in p.get("streams", []) if s.get("codec_type") == "audio"] or [0])
    e = run(["ffmpeg", "-hide_banner", "-nostats", "-i", v, "-vn", "-af", "ebur128", "-f", "null", "-"]).stderr
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", e)
    out["lufs"] = float(m[-1]) if m else None
except Exception as ex:
    out["probe_error"] = str(ex)
pages = None
if doc != "-":
    d = os.path.expanduser(doc)
    if not os.path.isabs(d):
        d = os.path.join(os.path.expanduser("~/workspace"), d)
    try:
        if d.lower().endswith((".pptx", ".ppt")):
            from pptx import Presentation
            pages = len(Presentation(d).slides)
        elif d.lower().endswith(".pdf"):
            r = run(["pdfinfo", d]).stdout
            m = re.search(r"Pages:\s+(\d+)", r); pages = int(m.group(1)) if m else None
    except Exception as ex:
        out["doc_error"] = str(ex)
out["pages"] = pages
cov = None
for c in [os.path.join(os.path.dirname(v), "coverage.md"), os.path.join(os.path.dirname(os.path.dirname(v)), "coverage.md"),
          os.path.expanduser("~/workspace/huashu-film/coverage.md")] + glob.glob(os.path.expanduser("~/workspace/*/coverage.md")):
    if os.path.exists(c):
        cov = open(c, encoding="utf-8", errors="ignore").read(); out["coverage_file"] = c; break
if cov is not None and pages:
    seen = {int(x) for g in re.findall(r"(?:第\s*(\d+)\s*页|slide[\s-]*(\d+))", cov, re.I) for x in g if x}
    for a, b in re.findall(r"第\s*(\d+)\s*[-–~至到]\s*(\d+)\s*页", cov):
        seen.update(range(int(a), int(b) + 1))
    out["missing_pages"] = [i for i in range(1, pages + 1) if i not in seen]
print(json.dumps(out, ensure_ascii=False))
'''


def qa_command(video: str, doc: Optional[str]) -> str:
    return "python3 -c " + shlex.quote(QA_PY) + " " + shlex.quote(video) + " " + shlex.quote(doc or "-")


def verdict(raw: str, prompt: str) -> Optional[str]:
    """None when the video may go out, else what to fix (English, for the model)."""
    try:
        q = json.loads((raw or "").strip().splitlines()[-1])
    except Exception:  # noqa: BLE001  - a QA hiccup never blocks delivery
        return None
    problems = []
    lufs, ch, dur, pages = q.get("lufs"), q.get("channels"), q.get("duration") or 0, q.get("pages")
    if lufs is not None and not (-19.5 <= lufs <= -12.5):
        problems.append(f"loudness is {lufs} LUFS (target -16): ffmpeg -af loudnorm=I=-16:TP=-1.5:LRA=11 -ac 2 -c:v copy")
    if ch == 1:
        problems.append("audio is mono: add -ac 2 (and a quiet music bed with om sound)")
    short = bool(SHORT_ASK.search(prompt or ""))
    if pages and not short:
        if "coverage_file" not in q:
            problems.append(f"no coverage.md: write huashu-film/coverage.md mapping all {pages} pages (第N页 <title> -> 场景)")
        elif q.get("missing_pages"):
            problems.append(f"pages {q['missing_pages']} of {pages} are not in the video/coverage.md: add a scene for each "
                            "(or say in coverage.md which scene carries it) and render again")
        if dur and dur < pages * 9:
            problems.append(f"{dur:.0f} s for a {pages}-page document is a teaser, not an explainer: about 15-20 s per page "
                            "with narration that explains each page")
    if not problems:
        return None
    return "REFUSED send_file, the video is not ready: " + "; ".join(problems) + ". Fix, re-render/re-mux, then send_file again."
