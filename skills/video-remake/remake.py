#!/usr/bin/env python3
"""复刻视频 helper for the AIW "video-remake" skill. Python 3.8+ stdlib only (plus ffmpeg/ffprobe for `frames`).

  remake.py frames VIDEO [--out DIR] [--scene 0.3] [--step 1]
      ffprobe the video, find hard cuts (ffmpeg scene detection), grab one frame per second (at k+0.5 s, or every
      --step seconds), one frame just after each cut and a contact sheet. Writes DIR/analysis.json and prints the
      frame paths relative to ~/workspace, ready for read_image paths=[...].
  remake.py check FILE.md [--duration 15]
      validate a final video prompt (or the whole deliverable): every @图片N bound and used, timings contiguous from
      0:00 and adding up to the duration, sections present, no vague words, names used consistently.
      Exit code 1 when there are errors.
  remake.py json FILE.md [--duration 15] [--analysis analysis.json]
      turn the deliverable markdown into the JSON deliverable (slots, image prompts, shots, constraints).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ----------------------------------------------------------------------------------------------- frames

def _run(cmd: list, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(video: str) -> dict:
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", video], 120)
    if r.returncode != 0:
        raise SystemExit(f"ffprobe failed: {r.stderr.strip()[:300]}")
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise SystemExit("no video stream")
    num, _, den = str(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    dur = float(v.get("duration") or d.get("format", {}).get("duration") or 0)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = abs(int(float(sd["rotation"])))
    if str((v.get("tags") or {}).get("rotate", "")).strip():
        rot = abs(int(float(v["tags"]["rotate"])))
    if rot in (90, 270):
        w, h = h, w
    return {"file": video, "duration": round(dur, 3), "fps": round(fps, 3), "width": w, "height": h,
            "aspect": aspect(w, h), "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}


def aspect(w: int, h: int) -> str:
    if not w or not h:
        return ""
    r = w / h
    for name, val in (("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3), ("3:4", 3 / 4), ("1:1", 1.0),
                      ("21:9", 21 / 9), ("2.35:1", 2.35), ("3:2", 1.5), ("2:3", 2 / 3)):
        if abs(r - val) / val < 0.03:
            return name
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}"


def scene_scores(video: str) -> list:
    """(time, score) for every frame: ffmpeg's scene-change score, 0..1."""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        _run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", video, "-an", "-vf",
              f"select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file={tmp}", "-f", "null", "-"], 900)
        txt = Path(tmp).read_text(errors="ignore")
    finally:
        os.unlink(tmp)
    ts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", txt)]
    sc = [float(x) for x in re.findall(r"scene_score=([0-9.]+)", txt)]
    return list(zip(ts, sc))


def pick_cuts(scores: list, threshold: float = 0.3, min_gap: float = 0.4) -> tuple:
    """Hard cuts and cut candidates. A cut is a one-frame spike: above `threshold`, or above 0.1 and at least 5x the
    3rd-largest score of the 6 frames on each side (AI clips with fast motion score 0.2 without any cut, while a
    cut between two dark shots can score only 0.12). Candidates (2.5x, >= 0.05) are for a look at the frames."""
    cuts, maybe = [], []
    for i, (t, s) in enumerate(scores):
        if t <= 0.2:
            continue
        nb = sorted(x for j, (_, x) in enumerate(scores[max(0, i - 6):i + 7], start=max(0, i - 6)) if j != i)
        ref = max(nb[-3] if len(nb) >= 3 else (nb[-1] if nb else 0.0), 0.004)
        ratio = s / ref
        if s >= threshold or (s >= 0.1 and ratio >= 5):
            if not cuts or t - cuts[-1] >= min_gap:
                cuts.append(round(t, 2))
        elif s >= 0.05 and ratio >= 2.5:
            maybe.append((ratio, round(t, 2)))
    maybe = sorted(t for _, t in sorted(maybe, reverse=True)[:8] if all(abs(t - c) >= min_gap for c in cuts))
    return cuts, maybe


def detect_cuts(video: str, threshold: float = 0.3, min_gap: float = 0.4) -> list:
    return pick_cuts(scene_scores(video), threshold, min_gap)[0]


def shots_from_cuts(cuts: list, duration: float) -> list:
    edges = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
    return [{"index": i + 1, "start": round(a, 2), "end": round(b, 2)} for i, (a, b) in enumerate(zip(edges, edges[1:]))
            if b - a > 0.05]


def _grab(video: str, t: float, path: Path) -> bool:
    r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1",
              "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(path)], 120)
    return r.returncode == 0 and path.exists() and path.stat().st_size > 0


def _font() -> Optional[str]:
    if shutil.which("fc-match"):
        r = _run(["fc-match", "-f", "%{file}", "sans"], 10)
        if r.returncode == 0 and r.stdout.strip() and os.path.exists(r.stdout.strip()):
            return r.stdout.strip()
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return p
    return None


def contact_sheet(video: str, duration: float, step: float, path: Path) -> bool:
    n = max(1, int(math.ceil(duration / step)))
    cols = 5 if n > 4 else n
    rows = int(math.ceil(n / cols))
    vf = f"fps=1/{step},scale=320:-2"
    font = _font()
    if font:  # a timestamp in each tile; skipped silently when drawtext/fonts are missing
        vf += (f",drawtext=fontfile={font}:text='%{{pts\\:hms}}':x=6:y=6:fontsize=20:fontcolor=white:"
               "box=1:boxcolor=black@0.6")
    for f in (vf, f"fps=1/{step},scale=320:-2"):
        r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video, "-an", "-vf",
                  f + f",tile={cols}x{rows}", "-frames:v", "1", "-q:v", "3", str(path)], 600)
        if r.returncode == 0 and path.exists():
            return True
    return False


def rel(p: Path) -> str:
    ws = Path.home() / "workspace"
    try:
        return str(p.resolve().relative_to(ws.resolve()))
    except ValueError:
        return str(p)


def cmd_frames(a) -> int:
    meta = probe(a.video)
    dur = meta["duration"]
    out = Path(a.out or (Path.home() / "workspace" / "remake" / Path(a.video).stem)).expanduser()
    fdir = out / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    step = a.step or (1.0 if dur <= 60 else round(dur / 60, 2))
    cuts, maybe = pick_cuts(scene_scores(a.video), a.scene)
    shots = shots_from_cuts(cuts, dur)
    seconds = []
    k = 0
    while k * step < dur - 0.3:  # a tail under 0.3 s joins the last second
        start = k * step
        end = dur if (k + 1) * step > dur - 0.3 else (k + 1) * step
        t = min(dur - 0.05, (start + end) / 2)
        p = fdir / f"s{k + 1:03d}_{t:06.2f}.jpg"
        if _grab(a.video, t, p):
            seconds.append({"sec": k + 1, "start": round(start, 2), "end": round(end, 2), "t": round(t, 2), "frame": rel(p)})
        k += 1
        if end >= dur:
            break
    for s in shots:
        s["frames"] = []
        if s["start"] > 0:  # the first frames of a new shot: what the cut lands on
            p = fdir / f"cut{s['index']:02d}_{s['start'] + 0.15:06.2f}.jpg"
            if _grab(a.video, s["start"] + 0.15, p):
                s["frames"].append(rel(p))
        s["frames"] += [x["frame"] for x in seconds if s["start"] <= x["t"] < s["end"]]
    maybe_frames = []
    for t in maybe:
        p = fdir / f"maybe_{t:06.2f}.jpg"
        if _grab(a.video, t + 0.05, p):
            maybe_frames.append({"t": t, "frame": rel(p)})
    sheet = out / "sheet.jpg"
    has_sheet = contact_sheet(a.video, dur, step, sheet)
    res = {"video": meta, "scene_threshold": a.scene, "cuts": cuts, "maybe_cuts": maybe_frames, "shots": shots, "seconds": seconds,
           "sheet": rel(sheet) if has_sheet else None, "out": rel(out)}
    (out / "analysis.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"video: {meta['width']}x{meta['height']} ({meta['aspect']}), {dur:.2f}s, {meta['fps']} fps, "
          f"audio={'yes' if meta['has_audio'] else 'no'}")
    print(f"hard cuts: {', '.join(fmt(c) for c in cuts) or 'none (one continuous shot)'}")
    if maybe_frames:
        print("possible cuts (check against the neighbouring seconds): "
              + ", ".join(f"{fmt(m['t'])} {m['frame']}" for m in maybe_frames))
    for s in shots:
        print(f"shot {s['index']}: {fmt(s['start'])}–{fmt(s['end'])}  frames: {', '.join(s['frames'])}")
    if has_sheet:
        print(f"contact sheet: {rel(sheet)}")
    print(f"analysis: {rel(out / 'analysis.json')}  (paths are relative to ~/workspace for read_image)")
    if dur > 15.05:
        print("note: longer than 15 s; most video models generate <= 15 s per run, so plan 片段 of <= 15 s each.")
    return 0


def fmt(t: float) -> str:
    m, s = divmod(float(t), 60)
    return f"{int(m)}:{s:05.2f}" if abs(s - round(s)) > 0.01 else f"{int(m)}:{int(round(s)):02d}"

# ----------------------------------------------------------------------------------------------- parsing

SECTIONS = {
    "binding": r"参考图绑定|图片绑定|参考图槽位",
    "rules": r"参考图使用规则|参考图规则",
    "global": r"全片统一设定|统一设定|全局设定",
    "timeline": r"逐秒提示词|分镜提示词|逐镜提示词|逐镜头提示词|时间轴提示词",
    "constraints": r"输出约束|负面约束|约束条件",
}
REQUIRED = ("binding", "timeline", "constraints")
SLOT = re.compile(r"@图片\s*(\d+)")
BIND = re.compile(r"^\s*[-*•]?\s*@图片\s*(\d+)\s*(?:是|为|=|：|:)\s*([^（(，,：:\s]+)\s*(?:[（(](.*)[）)])?\s*(.*)$")
_T = r"(\d+):(\d{1,2}(?:\.\d+)?)"
SHOT = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?\s*(?:第\s*(\d+)(?:\s*[-–—~至]\s*(\d+))?\s*秒|(?:镜头|分镜|片段)\s*(\d+))\s*[（(]\s*"
                  + _T + r"\s*[-–—~至]\s*" + _T + r"\s*[）)]\s*(?:\*\*)?\s*(?:[·•:：\-–—|]\s*(.*?))?\s*(?:\*\*)?\s*$")
FENCE = re.compile(r"^\s*```")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

VAGUE = ["等等", "之类", "诸如此类", "若干", "大概", "也许", "或许", "随便", "随意", "自行发挥", "自由发挥", "尽量",
         "某种", "某个", "某些", "一些", "各种各样", "差不多", "酌情", "看情况", "视情况", "不限", "任意"]
VAGUE_DENG = re.compile(r"(?<![平同均对相上中高次头优])等(?![待候级同于价效分号到着])(?=[，。；、,.;）)\s]|$)")
FILLER = ["高清", "超清", "4K", "8K", "大师级", "杰作", "最佳质量", "电影级", "震撼", "AI生成", "AI质感", "唯美", "炫酷",
          "masterpiece", "best quality"]
GENERIC_PEOPLE = re.compile(r"男主|女主|男子|女子|男人|女人|那个人|此人|他们|她们")
SHOT_SIZE = re.compile(r"远景|全景|中景|中近景|近景|特写|构图同上|同机位|同构图|景别|半身|全身|胸上|大特写")
CAMERA = re.compile(r"推|拉|摇|移|跟|升|降|固定|手持|晃|机位|俯|仰|平视|切|镜头|环绕|甩|变焦|焦点")


def strip_md(s: str) -> str:
    return re.sub(r"^[\s#>*\-•]+|[*\s]+$", "", re.sub(r"^\s*[一二三四五六七八九十0-9]+[、.．)]\s*", "", s.strip("# *\t")))


def final_region(text: str) -> tuple[str, bool]:
    """The final video prompt part of a deliverable (under a heading with 最终视频提示词), else the whole text."""
    lines = text.splitlines()
    start = level = None
    infence = False
    for i, ln in enumerate(lines):
        if FENCE.match(ln):
            infence = not infence
            continue
        if infence:
            continue
        m = HEADING.match(ln)
        if m and "最终视频提示词" in m.group(2) and start is None:
            start, level = i + 1, len(m.group(1))
            continue
        if m and start is not None and len(m.group(1)) <= level:
            return "\n".join(lines[start:i]), True
    if start is not None:
        return "\n".join(lines[start:]), True
    return text, False


def parse_prompt(text: str) -> dict:
    sec: dict = {k: [] for k in SECTIONS}
    sec["head"] = []
    cur = "head"
    for raw in text.splitlines():
        if FENCE.match(raw):
            continue
        name = strip_md(raw)
        hit = next((k for k, pat in SECTIONS.items() if re.fullmatch(rf"(?:{pat})(?:[（(].*[）)])?[:：]?", name)), None)
        if hit:
            cur = hit
            continue
        sec[cur].append(raw.rstrip())
    slots = []
    for ln in sec["binding"]:
        m = BIND.match(ln.replace("**", ""))
        if m:
            desc = (m.group(3) or "").strip() or (m.group(4) or "").strip()
            slots.append({"slot": f"@图片{int(m.group(1))}", "n": int(m.group(1)), "id": m.group(2).strip(),
                          "type": slot_type(m.group(2)), "desc": desc})
    shots, cur_shot = [], None
    for ln in sec["timeline"]:
        m = SHOT.match(ln)
        if m:
            a1, a2, a3, m1, s1, m2, s2, title = m.groups()
            cur_shot = {"label": ln.strip().strip("#* "), "kind": "sec" if a1 else "shot",
                        "n1": int(a1 or a3), "n2": int(a2 or a1 or a3),
                        "start": int(m1) * 60 + float(s1), "end": int(m2) * 60 + float(s2),
                        "title": (title or "").strip(), "lines": []}
            shots.append(cur_shot)
        elif cur_shot is not None and ln.strip():
            cur_shot["lines"].append(ln.strip())
    for s in shots:
        s["text"] = "\n".join(s.pop("lines"))
        s["slots"] = sorted({f"@图片{int(n)}" for n in SLOT.findall(s["text"])}, key=lambda x: int(x[3:]))
    lines = lambda k: [x.strip() for x in sec[k] if x.strip()]  # noqa: E731
    return {"slots": slots, "rules": lines("rules"), "global": lines("global"), "shots": shots,
            "constraints": lines("constraints"), "present": [k for k in SECTIONS if sec[k] and any(x.strip() for x in sec[k])],
            "sections_seen": [k for k in SECTIONS if k in _seen(text)]}


def _seen(text: str) -> set:
    out = set()
    for raw in text.splitlines():
        name = strip_md(raw)
        for k, pat in SECTIONS.items():
            if re.fullmatch(rf"(?:{pat})(?:[（(].*[）)])?[:：]?", name):
                out.add(k)
    return out


def slot_type(ident: str) -> str:
    if re.search(r"场景|背景图|环境|地点|空镜", ident):
        return "scene"
    if re.search(r"群|背景\d*[人女男]|路人|配角组|\d+[人女男]", ident):
        return "group"
    if re.search(r"道具|物品|产品", ident):
        return "prop"
    return "character"


def duration_hint(p: dict) -> Optional[float]:
    for ln in p["global"]:
        m = re.search(r"(?:时长|总长|片长)[^0-9]{0,6}(\d+(?:\.\d+)?)\s*(?:秒|s\b)", ln)
        if m:
            return float(m.group(1))
    return None

# ----------------------------------------------------------------------------------------------- checks

def check_text(text: str, duration: Optional[float] = None) -> dict:
    region, is_deliverable = final_region(text)
    p = parse_prompt(region)
    errors: list = []
    warns: list = []
    seen = p["sections_seen"]
    for k in REQUIRED:
        if k not in seen:
            errors.append(f"缺少段落「{SECTIONS[k].split('|')[0]}」")
    for k in ("rules", "global"):
        if k not in seen:
            warns.append(f"建议加上段落「{SECTIONS[k].split('|')[0]}」")

    bound = {s["slot"]: s for s in p["slots"]}
    if not bound:
        errors.append("参考图绑定里没有任何「@图片N是<ID>（描述）」行")
    nums = sorted(s["n"] for s in p["slots"])
    if nums and nums != list(range(1, len(nums) + 1)):
        warns.append(f"槽位编号不连续：{nums}（应从 @图片1 起连续编号）")
    dup = sorted({s["slot"] for s in p["slots"] if nums.count(s["n"]) > 1})
    if dup:
        errors.append(f"槽位重复绑定：{'、'.join(dup)}")
    ids = [s["id"] for s in p["slots"]]
    for i in sorted({x for x in ids if ids.count(x) > 1}):
        errors.append(f"两个槽位用了同一个 ID「{i}」")
    for s in p["slots"]:
        if "/" in s["desc"] or "或" in s["desc"]:
            warns.append(f"{s['slot']} 的描述里有二选一（/ 或「或」），外观要定死一种：{s['desc'][:40]}")
        if not s["desc"]:
            warns.append(f"{s['slot']}（{s['id']}）没有括号里的外观/服装要点")

    body = "\n".join(p["rules"] + p["global"] + [s["label"] + "\n" + s["text"] for s in p["shots"]] + p["constraints"])
    used_all = {f"@图片{int(n)}" for n in SLOT.findall(body)}
    for sl in sorted(used_all - set(bound), key=lambda x: int(x[3:])):
        errors.append(f"{sl} 被引用但没有在参考图绑定里绑定")
    used_tl = set().union(*[set(s["slots"]) for s in p["shots"]]) if p["shots"] else set()
    for sl in sorted(set(bound) - used_tl, key=lambda x: int(x[3:])):
        errors.append(f"{sl}（{bound[sl]['id']}）已绑定，但没有任何一个镜头引用它")

    shots = p["shots"]
    if not shots:
        errors.append("逐秒提示词里没有识别到「第 N 秒（0:00–0:01）· 标题」格式的镜头标题")
    dur = duration or duration_hint(p)
    t = 0.0
    for s in shots:
        lab = s["label"][:24]
        if abs(s["start"] - t) > 0.05:
            errors.append(f"{lab}：开始 {fmt(s['start'])} 与上一镜结束 {fmt(t)} 不衔接（{'空档' if s['start'] > t else '重叠'}）")
        if s["end"] - s["start"] <= 0.05:
            errors.append(f"{lab}：结束时间不晚于开始时间")
        if s["kind"] == "sec" and (s["n1"] - 1 != round(s["start"]) or s["n2"] != round(s["end"])):
            errors.append(f"{lab}：秒号与时间码不一致（第 {s['n1']}{'–' + str(s['n2']) if s['n2'] != s['n1'] else ''} 秒 "
                          f"应为 {fmt(s['n1'] - 1)}–{fmt(s['n2'])}）")
        t = s["end"]
        txt = s["text"]
        if len(txt) < 20:
            errors.append(f"{lab}：镜头描述太短（{len(txt)} 字）")
        if not SHOT_SIZE.search(txt + s["title"]) and not CAMERA.search(txt + s["title"]):
            warns.append(f"{lab}：没写景别或运镜")
        if not s["slots"] and bound:
            warns.append(f"{lab}：没有引用任何参考图（画面里的人物/场景要用 @图片N）")
        for b in p["slots"]:
            if len(b["id"]) >= 2 and b["id"] in txt.replace(b["slot"], ""):
                errors.append(f"{lab}：用了别名「{b['id']}」，镜头里统一写 {b['slot']}")
        g = GENERIC_PEOPLE.findall(txt)
        if g and bound:
            warns.append(f"{lab}：出现泛称「{'、'.join(sorted(set(g)))}」，绑定过的人物请写 @图片N")
    if shots:
        if dur and abs(t - dur) > 0.05:
            errors.append(f"镜头时间合计 {fmt(t)}，与视频时长 {fmt(dur)} 不一致")
        if not dur:
            warns.append(f"没有给出视频时长（--duration 或 全片统一设定里写「时长：N 秒」），按最后一镜结束 {fmt(t)} 计")
        if (dur or t) > 15.05:
            warns.append("时长超过 15 秒：多数视频模型一次只生成 ≤15 秒，请拆成多个片段，每段单独一份提示词")

    whole = region
    for w in VAGUE:
        if w in whole:
            errors.append(f"含糊词「{w}」：写出具体的东西、数量或动作")
    for ln in whole.splitlines():
        if VAGUE_DENG.search(ln):
            errors.append(f"列举后用「等」收尾（要列全）：{ln.strip()[:40]}")
    for w in FILLER:
        if w.lower() in whole.lower():
            warns.append(f"空泛修饰「{w}」不提供画面信息，删掉或换成具体的光线/质感描述")

    cons = "\n".join(p["constraints"])
    if p["constraints"] and not re.search(r"文字|字幕|水印", cons):
        warns.append("输出约束里没有写文字/字幕/水印的要求")
    for s in p["slots"]:
        if s["type"] in ("character",) and p["constraints"] and s["slot"] not in cons:
            warns.append(f"输出约束里没有锁定 {s['slot']}（{s['id']}）的服装/面部，建议写「全程不得变装」")

    if is_deliverable:
        outside = text.replace(region, "")
        for s in p["slots"]:
            if not re.search(rf"^#+.*{re.escape(s['slot'])}(?!\d)", outside, re.M):
                errors.append(f"交付文档里没有 {s['slot']}（{s['id']}）的生成提示词小节（### {s['slot']} · {s['id']}）")
            if not re.search(rf"^\|\s*{re.escape(s['slot'])}\s*\|", outside, re.M):
                errors.append(f"图片槽位表里没有 {s['slot']} 这一行")
    return {"ok": not errors, "errors": errors, "warnings": warns, "duration": dur, "total": round(t, 3),
            "slots": [s["slot"] for s in p["slots"]], "shots": len(shots), "deliverable": is_deliverable}


def cmd_check(a) -> int:
    r = check_text(Path(a.file).read_text(encoding="utf-8"), a.duration)
    print(f"slots: {' '.join(r['slots']) or '-'} | shots: {r['shots']} | total {fmt(r['total'])}"
          + (f" / duration {fmt(r['duration'])}" if r["duration"] else ""))
    for e in r["errors"]:
        print("ERROR  " + e)
    for w in r["warnings"]:
        print("WARN   " + w)
    print("OK" if r["ok"] else f"FAILED: {len(r['errors'])} error(s)")
    return 0 if r["ok"] else 1

# ----------------------------------------------------------------------------------------------- json

def image_prompts(text: str) -> dict:
    """### @图片N · ID sections of the deliverable -> {slot: prompt text} (fences stripped)."""
    out: dict = {}
    cur = None
    for ln in text.splitlines():
        m = HEADING.match(ln)
        if m:
            sm = SLOT.search(m.group(2))
            cur = f"@图片{int(sm.group(1))}" if sm and "最终视频提示词" not in m.group(2) else None
            if cur:
                out.setdefault(cur, [])
            continue
        if cur and not FENCE.match(ln):
            out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def slot_table(text: str) -> list:
    rows = []
    for ln in text.splitlines():
        if ln.strip().startswith("|") and SLOT.search(ln):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if cells and SLOT.fullmatch(cells[0].replace(" ", "")):  # the slot table, not the shot analysis
                rows.append(cells)
    return rows


def to_json(text: str, duration: Optional[float] = None, analysis: Optional[dict] = None) -> dict:
    region, _ = final_region(text)
    p = parse_prompt(region)
    prompts = image_prompts(text.replace(region, ""))
    table = slot_table(text.replace(region, ""))
    rows = {r[0].replace(" ", ""): r for r in table}
    slots = []
    for s in p["slots"]:
        row = rows.get(s["slot"], [])
        slots.append(dict(s, upload=row[3] if len(row) > 3 else "", table_row=row, prompt=prompts.get(s["slot"], "")))
    title = next((HEADING.match(ln).group(2).strip() for ln in text.splitlines() if HEADING.match(ln)), "")
    res = {"title": title, "duration": duration or duration_hint(p) or (p["shots"][-1]["end"] if p["shots"] else None),
           "slots": slots, "rules": p["rules"], "global": p["global"],
           "shots": [{k: s[k] for k in ("label", "title", "start", "end", "slots", "text")} for s in p["shots"]],
           "constraints": p["constraints"], "final_prompt": region.strip("\n").replace("```text\n", "").replace("```", "").strip(),
           "check": check_text(text, duration)}
    if analysis:
        res["video"] = analysis.get("video")
        res["cuts"] = analysis.get("cuts")
    return res


def cmd_json(a) -> int:
    ana = json.loads(Path(a.analysis).read_text(encoding="utf-8")) if a.analysis else None
    print(json.dumps(to_json(Path(a.file).read_text(encoding="utf-8"), a.duration, ana), ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="复刻视频 helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("frames")
    f.add_argument("video")
    f.add_argument("--out")
    f.add_argument("--scene", type=float, default=0.3)
    f.add_argument("--step", type=float, default=0)
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--duration", type=float)
    j = sub.add_parser("json")
    j.add_argument("file")
    j.add_argument("--duration", type=float)
    j.add_argument("--analysis")
    a = ap.parse_args(argv)
    return {"frames": cmd_frames, "check": cmd_check, "json": cmd_json}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
