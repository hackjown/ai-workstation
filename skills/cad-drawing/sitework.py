#!/usr/bin/env python3
"""sitework: 施工图（景观/场地/市政/装修/小型构筑物）的“先读懂资料 → 图纸目录 → 分图出图 → 逐张核对”。

v3.9.93 (task-241, 升旗台广场): the agent read two images in one shallow call, decided "both are site photos, no
dimensions", was cut off by the anti-loop policy at step 4, wrote ONE generic sheet with hand-placed coordinates,
gave text heights in model mm (h_mm 800 = an 80 m tall title at 1:100), ran the floor-plan tracer on a site photo
and delivered "目前最好的草稿" with 647 errors and floor-plan boilerplate. This module is the deterministic part of
the fix, pure stdlib so the server (routing, brief, final message) and the computer (spec generation) share it:

  classify(prompt)                 -> 房屋平面描图 (floor_plan) | 景观/场地 (site) | 装修 (interior) | 构筑物 (structure) | generic
  parse_text(prompt)               -> facts from the user's words, each with its source
  IMAGE_Q / parse_image_read(text) -> one structured read PER image (role, elements, positions, materials, counts)
  parse_quote_rows(rows)           -> 报价/清单 items (name, unit, qty, spec)
  build_brief(...)                 -> brief.json: facts + derived quantities + conflicts + unknowns (assumption used,
                                      blocking or not)
  plan_sheets(brief)               -> 图纸目录 (numbered sheet list) from the job-type template
  spec_from_brief(brief)           -> a drawkit `sheet_set` spec: every sheet drawn parametrically from the brief
  quantities(brief)                -> 工程量表 (with the quote's number beside it when there is a quote)
  requirements(brief)              -> what each sheet must show (checked mechanically and by the vision checklist)

CLI (on the computer):  python3 sitework.py classify "<prompt>"
                        python3 sitework.py brief  --prompt-file p.txt [--images reads.json] [--quotes q.json] --out cad/brief.json
                        python3 sitework.py spec   cad/brief.json --out cad/spec.json
                        python3 sitework.py quote  FILE            (xlsx/xls/csv/pdf -> rows JSON)
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from pathlib import Path

VERSION = "1.0"

# ============================================================================================ routing

_FLOOR = re.compile(r"户型|描图|照着.{0,6}(画|描)|照图|平面图.{0,6}(照片|图片)|原始结构图|房屋平面|室内平面|售楼")
_SITE = re.compile(r"广场|场地|景观|园林|铺装|铺贴户外|户外瓷砖|透水砖|草坪|草皮|绿化|种植|树池|台阶|升旗台|旗杆|篮球场|球场|"
                   r"游泳池|泳池|水泥路|道路|路面|路缘石|侧石|排水沟|截水沟|雨水|挡土墙|围墙|斜坡|护坡|回填|土方|硬化|停车场|"
                   r"室外|市政|管网|井盖|椰子树|乔木|灌木|红土|总平面|竖向")
_INTERIOR = re.compile(r"装修|吊顶|天花|立面|墙面|地面|木作|柜体|衣柜|橱柜|背景墙|水电|强电|弱电|开关|插座|灯具|卫生间|厨房|"
                       r"卧室|客厅|展厅|门店|店面|办公室|乳胶漆|墙纸|石膏板|踢脚")
_STRUCT = re.compile(r"钢结构|雨棚|车棚|凉亭|廊架|基础|独立基础|条形基础|梁|柱|混凝土结构|砌体|小品|岗亭|门楼|构筑物")
_FLOOR_HINT = re.compile(r"户型|平面图|房间|卧室|客厅")


def classify(prompt: str, attachments=None) -> dict:
    """the job type decides the whole route: floor_plan -> the pixel tracer (drawing_auto); site / interior /
    structure -> READ brief -> 图纸目录 -> sheet set. Never the tracer on a site job."""
    p = prompt or ""
    s = {"site": len(_SITE.findall(p)), "interior": len(_INTERIOR.findall(p)), "structure": len(_STRUCT.findall(p))}
    floor = bool(_FLOOR.search(p))
    reasons = [f"{k}={v}" for k, v in s.items() if v]
    if floor and s["site"] < 2:
        return {"type": "floor_plan", "label": "房屋平面描图", "reasons": reasons + ["floor-plan words"]}
    best = max(s, key=lambda k: (s[k], k == "site"))
    if s[best] == 0:
        if _FLOOR_HINT.search(p):
            return {"type": "floor_plan", "label": "房屋平面描图", "reasons": ["plan words only"]}
        return {"type": "generic", "label": "通用图纸", "reasons": []}
    if best == "site" or (s["site"] >= 3 and s["site"] >= s[best] - 1):
        return {"type": "site", "label": "景观/场地施工图", "reasons": reasons}
    if best == "interior":
        return {"type": "interior", "label": "装修施工图", "reasons": reasons}
    return {"type": "structure", "label": "小型构筑物施工图", "reasons": reasons}


def is_sheet_job(prompt: str) -> bool:
    return classify(prompt)["type"] in ("site", "interior", "structure")


# ============================================================================================ templates

# 通用做法 (generic build-ups): used only when the user / quote gives none, and always listed as 待确认
GENERIC_LAYERS = {
    "paving_tile": ["18厚户外防滑瓷砖 600×600，留缝3，专用勾缝剂", "30厚1:3干硬性水泥砂浆结合层", "100厚C20混凝土垫层（分仓≤6m，设缩缝）",
                    "150厚级配碎石垫层", "素土夯实（压实系数≥0.93）"],
    "step": ["户外防滑踏步砖（同广场面层），踏步前沿设防滑槽", "30厚1:3干硬性水泥砂浆结合层", "C20混凝土台阶（踏步现浇）",
             "150厚级配碎石垫层", "素土夯实（压实系数≥0.93）"],
    "lawn": ["草坪（满铺草皮）", "300厚种植土", "分层回填土，每层≤300 夯实（压实系数≥0.90）", "原坡面清表"],
    "platform": ["花岗岩/大理石台面（同栏杆石材）", "30厚1:3干硬性水泥砂浆结合层", "C25钢筋混凝土台身", "100厚C15混凝土垫层",
                 "150厚级配碎石垫层", "素土夯实"],
    "drain": ["成品线性排水沟 300宽，球墨铸铁/不锈钢缝隙箅子", "C25混凝土沟身，沟底找坡0.5%", "100厚C15混凝土垫层", "素土夯实"],
    "interior_floor": ["地面面层（按材料表）", "30厚1:3水泥砂浆找平", "防水层（卫生间/厨房上翻300）", "原结构楼板"],
    "interior_wall": ["面层（乳胶漆/墙纸/饰面板）", "腻子两遍", "原墙基层处理"],
    "interior_ceiling": ["9.5厚纸面石膏板吊顶", "轻钢龙骨 C50 主龙骨@900", "Φ8吊杆@1200"],
}

TEMPLATES = {
    "site": [
        ("cover", "图纸目录及设计说明"),
        ("site_plan", "总平面及拆除平面图"),
        ("paving", "铺装平面图"),
        ("grading", "竖向及排水平面图"),
        ("platform", "升旗台平面、立面、剖面图"),
        ("steps", "台阶及栏杆大样图"),
        ("section", "剖面图"),
        ("drain", "排水沟节点大样图"),
        ("qty", "工程量表"),
    ],
    "interior": [
        ("cover", "图纸目录及设计说明"), ("plan", "原始平面及拆改图"), ("layout", "平面布置图"), ("floor", "地面铺装图"),
        ("ceiling", "天花布置图"), ("elec", "水电点位图"), ("elev", "立面图"), ("detail", "节点大样图"), ("qty", "工程量表"),
    ],
    "structure": [
        ("cover", "图纸目录及设计说明"), ("plan", "平面布置图"), ("elev", "立面图"), ("section", "剖面图"),
        ("found", "基础平面及详图"), ("detail", "节点大样图"), ("qty", "工程量表"),
    ],
}

# which brief element makes a site sheet necessary (a template sheet with nothing to show is left out)
_SITE_NEEDS = {"platform": ("flag_platform",), "steps": ("steps", "railing"), "drain": ("drainage",),
               "section": ("slope", "raise", "steps")}


# ============================================================================================ text facts

_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s and all(ch in _CN for ch in s):
        if s == "十":
            return 10.0
        if s.startswith("十"):
            return 10.0 + _CN.get(s[1:], 0)
        if "十" in s:
            a, b = s.split("十", 1)
            return _CN[a] * 10 + (_CN.get(b, 0) if b else 0)
        return float(_CN[s[0]])
    return None


def _mm(v: float, unit: str | None, default="m") -> float:
    u = (unit or default).lower()
    if u in ("mm", "毫米"):
        return v
    if u in ("cm", "厘米"):
        return v * 10
    return v * 1000.0


_UNIT = r"(mm|cm|m|米|毫米|厘米)?"
_DIMS = re.compile(r"(?:长|L)?\s*(\d+(?:\.\d+)?)\s*" + _UNIT + r"\s*[×xX*＊]\s*(?:宽|W)?\s*(\d+(?:\.\d+)?)\s*" + _UNIT +
                   r"(?:\s*[×xX*＊]\s*(?:高|深|厚|H)?\s*(\d+(?:\.\d+)?)\s*" + _UNIT + r")?")
_AREA = re.compile(r"[=＝]?\s*(\d+(?:\.\d+)?)\s*(㎡|m²|m2|平方米|平米)")
_COUNT = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*(棵|株|步|级|个|根|座|处|道|盏|樘|扇)")
_STOP = "，。；;、（）()\n：:→,"


def _subject(text: str, start: int, span: int = 14) -> str:
    """the noun phrase in front of a match: back to the previous delimiter, minus verbs and filler."""
    i = start
    while i > 0 and text[i - 1] not in _STOP and start - i < span:
        i -= 1
    s = text[i:start].strip()
    s = re.sub(r"^(工程概况|做法|包括|其中|另|以及|和|及|与|新建|设|做|含)+", "", s)
    s = re.sub(r"(长|宽|高|约|为|是|共)+$", "", s)
    return s.strip()


def _src(text: str, a: int, b: int) -> str:
    return text[max(0, a):min(len(text), b)].strip()


def parse_text(prompt: str) -> dict:
    """facts the user's words give, each tagged with its source text. Structured enough to draw from, honest about
    what it did not find (those become unknowns in build_brief)."""
    t = re.sub(r"【[^】]*】", " ", prompt or "")
    t = t.split("附件（", 1)[0]
    facts, site = [], {}

    def fact(key, value, src, kind="given"):
        facts.append({"key": key, "value": value, "source": "用户文字", "quote": src, "kind": kind})

    # dimensions: "广场长27.9m×宽15.8m", "斜坡（长13.5m×宽5m×高1.6m）"
    for m in _DIMS.finditer(t):
        a, ua, b, ub, c, uc = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
        unit = ua or ub or uc
        if not unit and float(a) < 100:
            unit = "m"
        subj = _subject(t, m.start())
        if not subj:   # "斜坡（长13.5m×..." : the subject is before the bracket
            j = m.start() - 1
            while j > 0 and t[j] in " （(":
                j -= 1
            subj = _subject(t, j + 1)
        L, W = _mm(float(a), ua or unit), _mm(float(b), ub or unit)
        H = _mm(float(c), uc or unit) if c else None
        rec = {"name": subj or "尺寸", "L": round(L), "W": round(W)}
        if H is not None:
            rec["H"] = round(H)
        tail = t[m.end():m.end() + 40]
        am = _AREA.match(tail)
        if am:
            rec["area_m2"] = float(am.group(1))
            note = re.match(r"\s*[（(]([^）)]{0,30})[）)]", tail[am.end():])
            if note:
                rec["area_note"] = note.group(1)
        fact(f"dims:{rec['name']}", rec, _src(t, m.start() - len(subj or ""), m.end() + (am.end() if am else 0) + 12))
        key = _element_of(rec["name"])
        if key:
            site.setdefault(key, {}).update({k: v for k, v in rec.items() if k != "name"}, name=rec["name"])
    # counts: "6棵椰子树", "2步通宽台阶"
    for m in _COUNT.finditer(t):
        n = _num(m.group(1))
        unit = m.group(2)
        after = re.match(r"[^\s，。；、（）()→]{0,8}", t[m.end():]).group(0)
        before = _subject(t, m.start(), 10)
        what = after if after and unit in ("棵", "株", "根", "个", "盏") else (before + after)
        fact(f"count:{what or unit}", {"n": int(n) if n is not None else None, "unit": unit, "what": what, "before": before},
             _src(t, m.start() - len(before), m.end() + len(after)))
        if unit in ("棵", "株") and re.search(r"树|椰|乔木|棕榈", what + before):
            site["trees_remove" if re.search(r"移除|拆除|迁移|砍|清除|移走", t[max(0, m.start() - 12):m.end() + 8]) else "trees"] = {
                "n": int(n), "what": re.sub(r"^.*?(椰子树|棕榈|乔木|树)$", r"\1", what) or "树",
                "where": (re.search(r"(左右两边|左右两侧|两侧|两边|四周|周边|沿[^，。]{1,6})", t[max(0, m.start() - 12):m.end()]) or [None])[0]}
        if unit in ("步", "级") and re.search(r"台阶|踏步|步", unit + after + before):
            st = site.setdefault("steps", {})
            st["n"] = int(n)
            ctx = t[max(0, m.start() - 20):m.end() + 12]
            if "通宽" in ctx:
                st["full_width"] = True
            w = re.search(r"临([^，。；、（）()]{1,8}?)一侧", ctx)
            if w:
                st["side_of"] = w.group(1)
    # areas without dims
    for m in _AREA.finditer(t):
        if not any(isinstance(f["value"], dict) and f["value"].get("area_m2") == float(m.group(1)) for f in facts):
            fact(f"area:{_subject(t, m.start())}", {"area_m2": float(m.group(1))}, _src(t, m.start() - 10, m.end()))
    # elements by keyword
    if re.search(r"红土|原地面", t):
        g = re.search(r"原地面为?([^，。；、]{1,6})", t)
        site["ground"] = {"existing": g.group(1) if g else "红土"}
        fact("ground", site["ground"], g.group(0) if g else "红土")
    tile = re.search(r"(户外|防滑|仿石)?(瓷砖|透水砖|花岗岩|石材|地砖|广场砖)", t)
    if tile:
        site["paving"] = {"material": tile.group(0)}
        fact("paving", site["paving"], tile.group(0))
    if re.search(r"升旗台|旗台|旗杆", t):
        fp = site.setdefault("flag_platform", {})
        r = re.search(r"围栏为?([^，。；、）)]{1,10})", t)
        if r:
            fp["railing"] = r.group(1)
            site["railing"] = {"material": r.group(1)}
        fact("flag_platform", dict(fp), (re.search(r"[^，。；]*升旗台[^，。；]*", t) or [""])[0])
    if re.search(r"整体抬高|抬高|加高|垫高", t):
        site["raise"] = {"given": True}
        fact("raise", {"given": True}, (re.search(r"[^，。；（(]*抬高[^，。；）)]*", t) or [""])[0])
    if re.search(r"台阶|踏步", t) and "steps" not in site:
        site["steps"] = {}
    if "steps" in site and re.search(r"水泥路", t):
        site.setdefault("road", {"name": "水泥路", "existing": True})
    if re.search(r"草坪|草皮|铺草", t):
        lawn_on = "slope" if re.search(r"斜坡[^。]{0,40}草坪|回填后铺草坪", t) else "area"
        site["lawn"] = {"on": lawn_on}
        fact("lawn", site["lawn"], (re.search(r"[^，。；]*草坪[^，。；]*", t) or [""])[0])
    if "slope" in site and re.search(r"回填", t):
        site["slope"]["backfill"] = True
    for k, w in (("pool", r"游泳池|泳池"), ("court", r"篮球场|球场"), ("road", r"水泥路|道路|路面")):
        if re.search(w, t):
            site.setdefault(k, {"name": re.search(w, t).group(0), "existing": True})
    dr = []
    for clause in re.findall(r"[^，。；;\n]*排水[^，。；;\n]*", t):
        for part in re.split(r"[、]", clause):
            pm = re.search(r"([\u4e00-\u9fa5]{2,4}?)(?:和|与|及)([\u4e00-\u9fa5]{2,4}?)(?:的|连接|交接|之间|处|做|设|$)", part)
            if pm:
                pair = [pm.group(1), pm.group(2)]
                if pair not in [d["between"] for d in dr]:
                    dr.append({"between": pair})
    if dr:
        site["drainage"] = dr
        fact("drainage", dr, (re.search(r"[^；。]*排水", t) or [""])[0])
    elif re.search(r"排水", t):
        site["drainage"] = [{"between": ["场地", "周边"]}]
    seq = re.search(r"做法[：:]([^。]*)", t)
    if seq:
        steps = []
        for i, piece in enumerate(re.split(r"→|->|＞", seq.group(1))):
            for part in _split_top(piece):
                part = part.strip(" ，,、\n")
                if part and not re.search(r"根据以上|绘制|出图|画图", part):
                    steps.append(part)
        site["sequence"] = steps
        fact("sequence", steps, seq.group(0)[:80])
    name = None
    mm = re.search(r"(?:工程名称|项目名称|项目|工程)[：:]\s*([^，。；\n]{2,20})", t)
    if mm:
        name = mm.group(1).strip()
    elif "flag_platform" in site and "plaza" in site:
        name = "升旗台广场"
    else:
        for key in ("plaza", "slope", "court"):
            if site.get(key, {}).get("name"):
                name = re.sub(r"^.*?(和|与|及)", "", site[key]["name"])
                break
    return {"facts": facts, "site": site, "project": (name or "") + ("改造工程" if name and not name.endswith(("工程", "项目")) else "")}


def _split_top(s: str) -> list:
    """split on ；，; , outside brackets (a clause in （…） stays whole)."""
    out, cur, depth = [], "", 0
    for ch in s:
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch in "；;，," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _element_of(name: str) -> str | None:
    n = name or ""
    for key, pat in (("slope", r"斜坡|护坡|坡"), ("plaza", r"广场|场地|铺装|地坪"), ("flag_platform", r"升旗台|旗台"),
                     ("steps", r"台阶|踏步"), ("pool", r"泳池"), ("court", r"球场"), ("lawn", r"草坪|绿地")):
        if re.search(pat, n):
            return key
    return None


# ============================================================================================ images

IMAGE_Q = (
    "这是一个施工图任务的输入图片之一（第 {n} 张，文件 {name}）。只描述这一张图，图上没有的不要编。用户的说明是：{brief}\n"
    "只输出 JSON：{{\n"
    " \"role\": \"设计效果图|方案平面图|现场照片|手绘草图|报价单|其它\",\n"
    " \"view\": \"拍摄/绘制视角，例如 从水泥路一侧朝广场看、鸟瞰、平面\",\n"
    " \"elements\": [{{\"name\": \"构件名\", \"position\": \"在画面/场地中的位置（左/右/中/前/后，与其它构件的关系）\", "
    "\"count\": 数量或null, \"material\": \"材质/颜色\", \"note\": \"\"}}],\n"
    " \"layout\": \"用一两句话说清平面关系：哪边是什么（例如 前方水泥路、左侧游泳池、右侧篮球场、后方是什么）\",\n"
    " \"paving_pattern\": \"铺装样式（砖的形状、排法、分格）或null\",\n"
    " \"steps\": \"台阶在哪一侧、几步、多宽，或null\",\n"
    " \"texts\": [\"图上能读到的文字\"], \"dimensions\": [\"图上能读到的尺寸/标高（带位置）\"],\n"
    " \"unknowns\": [\"画施工图还缺、这张图也看不出来的东西\"]\n}}")

_ROLE_WORDS = (("设计效果图", r"效果|设计|渲染"), ("现场照片", r"现场|实景|照片|原图|现状"), ("方案平面图", r"平面"),
               ("手绘草图", r"手绘|草图"), ("报价单", r"报价|清单"))


def parse_image_read(text: str, hint: str = "") -> dict:
    """the vision model's JSON for ONE image -> a normalised record; a non-JSON answer is kept as raw text and its
    role guessed from the words (and the user's hint, e.g. 图1是设计)."""
    d = {}
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
        except ValueError:
            d = {}
    role = str(d.get("role") or "")
    if not role or role == "其它":
        for r, pat in _ROLE_WORDS:
            if re.search(pat, hint or "") or (not d and re.search(pat, text or "")):
                role = r
                break
    if hint:   # the user's own words about this image win over the model's guess
        for r, pat in _ROLE_WORDS:
            if re.search(pat, hint):
                role = r
                break
    els = []
    for e in d.get("elements") or []:
        if isinstance(e, dict) and e.get("name"):
            els.append({k: e.get(k) for k in ("name", "position", "count", "material", "note") if e.get(k) not in (None, "")})
    return {"role": role or "其它", "view": d.get("view"), "layout": d.get("layout"), "elements": els,
            "paving_pattern": d.get("paving_pattern"), "steps": d.get("steps"), "texts": d.get("texts") or [],
            "dimensions": d.get("dimensions") or [], "unknowns": d.get("unknowns") or [],
            "raw": None if d else (text or "")[:1500]}


def image_hints(prompt: str) -> dict:
    """{1: '设计', 2: '原图'} from 「图1是设计，图2是原图」."""
    out = {}
    for m in re.finditer(r"图\s*([0-9一二三四五六七八九])\s*(?:是|为)?\s*([^，。；,、]{1,10})", prompt or ""):
        k = int(_num(m.group(1)) or 0)
        if k:
            out[k] = m.group(2)
    return out


# ============================================================================================ quotes

_QTY_KEYS = [
    ("trees_remove", r"移除|移栽|迁移|砍伐|清除.*树|椰子树"),
    ("base", r"基础处理|清表|平整|夯实|素土"),
    ("gravel", r"碎石|级配"),
    ("concrete", r"混凝土垫层|砼垫层|C15|C20"),
    ("tile", r"瓷砖|地砖|铺贴|铺装|广场砖|透水砖|石材地面"),
    ("steps", r"台阶|踏步"),
    ("platform", r"升旗台|旗台"),
    ("railing", r"栏杆|围栏|石柱|大理石柱"),
    ("flagpole", r"旗杆"),
    ("backfill", r"回填|土方"),
    ("lawn", r"草坪|草皮|植草"),
    ("drain", r"排水沟|排水|截水沟|雨水"),
]


def parse_quote_rows(rows) -> list:
    """rows: list of lists (a sheet / CSV / table read) -> items {name, unit, qty, price, spec, key, row}."""
    items = []
    for i, r in enumerate(rows or []):
        cells = [str(c).strip() for c in (r or []) if c is not None and str(c).strip() not in ("", "None", "nan")]
        if len(cells) < 2:
            continue
        name = next((c for c in cells if re.search(r"[\u4e00-\u9fa5]{2,}", c)), None)
        if not name or re.search(r"^(序号|项目名称|名称|合计|小计|总计)$", name):
            continue
        unit = next((c for c in cells if re.fullmatch(r"(㎡|m²|m2|平方米|m³|m3|立方米|m|米|延米|棵|株|个|座|项|根|处|套|樘|樘|步)", c)), None)
        nums = [float(c.replace(",", "")) for c in cells if re.fullmatch(r"-?\d+(?:\.\d+)?", c.replace(",", ""))]
        spec = " ".join(c for c in cells if c not in (name, unit) and not re.fullmatch(r"-?[\d.,]+", c))[:80]
        key = next((k for k, pat in _QTY_KEYS if re.search(pat, name)), None)
        items.append({"row": i + 1, "name": name, "unit": unit, "qty": nums[1] if len(nums) >= 3 and nums[0] == int(nums[0]) and nums[0] < 200 and nums[0] == i
                      else (nums[0] if nums else None), "numbers": nums, "spec": spec, "key": key})
    return items


def read_quote_file(path: str) -> list:
    """xlsx / xls / csv / pdf -> rows (lists of cells). Best effort, no new dependencies required."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".csv", ".txt"):
        import csv
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            return [r for r in csv.reader(f)]
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # noqa: F401
            wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
            rows = []
            for ws in wb.worksheets:
                rows += [[c for c in r] for r in ws.iter_rows(values_only=True)]
            return rows
        except ImportError:
            pass
    if ext in (".xls", ".xlsx", ".xlsm", ".ods"):
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        subprocess.run(["soffice", "--headless", "--convert-to", "csv", "--outdir", d, str(p)], capture_output=True, timeout=120)
        out = list(Path(d).glob("*.csv"))
        return read_quote_file(str(out[0])) if out else []
    if ext == ".pdf":
        import subprocess
        txt = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, timeout=60).stdout
        return [re.split(r"\s{2,}", ln.strip()) for ln in txt.splitlines() if ln.strip()]
    return []


# ============================================================================================ brief

def _r2(x):
    return round(float(x) + 1e-9, 2)


def build_brief(prompt: str, images=None, quotes=None, task: str = "") -> dict:
    """images: [{"index": 1, "path": ..., "read": <vision text or dict>}], quotes: [{"path": ..., "rows": [...]}]
    or [{"path", "items"}]. Returns brief.json (facts with sources, derived quantities, conflicts, unknowns)."""
    route = classify(prompt)
    tx = parse_text(prompt)
    site = tx["site"]
    facts = list(tx["facts"])
    hints = image_hints(prompt)
    imgs = []
    for k, im in enumerate(images or [], 1):
        idx = int(im.get("index") or k)
        rd = im.get("read")
        rec = rd if isinstance(rd, dict) and "role" in rd else parse_image_read(rd if isinstance(rd, str) else json.dumps(rd or {}, ensure_ascii=False),
                                                                                  hints.get(idx, ""))
        rec.update(index=idx, path=im.get("path"), user_says=hints.get(idx))
        imgs.append(rec)
        for e in rec.get("elements") or []:
            facts.append({"key": f"image{idx}:{e['name']}", "value": e, "source": f"图{idx}（{rec['role']}）", "kind": "seen"})
        if rec.get("layout"):
            facts.append({"key": f"image{idx}:layout", "value": rec["layout"], "source": f"图{idx}（{rec['role']}）", "kind": "seen"})
        if rec.get("paving_pattern"):
            site.setdefault("paving", {}).setdefault("pattern_seen", rec["paving_pattern"])
    q_items = []
    for q in quotes or []:
        its = q.get("items") or parse_quote_rows(q.get("rows") or [])
        for it in its:
            it["file"] = q.get("path")
        q_items += its
    for it in q_items:
        facts.append({"key": f"quote:{it['name']}", "value": {k: it.get(k) for k in ("unit", "qty", "spec")},
                      "source": f"报价 {Path(str(it.get('file') or '')).name} 第{it.get('row')}行", "kind": "quote"})
    unknowns, conflicts, derived = [], [], []

    def unknown(key, q, assume, blocking=False, value=None):
        unknowns.append({"key": key, "topic": TOPICS.get(key, key.split(".")[0]), "question": q, "assumption": assume,
                         "blocking": blocking, "value": value})

    if route["type"] == "site":
        _site_defaults(site, unknown, derived, conflicts, imgs)
    elif route["type"] in ("interior", "structure"):
        if not any(f["key"].startswith("dims:") for f in facts) and not any(i.get("dimensions") for i in imgs):
            unknown("main_dims", "主要尺寸（房间/构件的长、宽、高）是多少？图片和文字里都没有。", "无", blocking=True)
    # quote vs computed
    qty = quantities({"site": site, "type": route["type"]})
    for row in qty:
        qs = [it for it in q_items if it.get("key") == row["key"] and it.get("qty") is not None]
        if qs:
            row["quote_qty"] = qs[0]["qty"]
            row["quote_row"] = f"{Path(str(qs[0].get('file') or '')).name} 第{qs[0].get('row')}行"
            if row.get("qty") and abs(qs[0]["qty"] - row["qty"]) > max(0.05 * row["qty"], 0.5):
                conflicts.append({"key": f"qty:{row['key']}", "what": f"{row['name']}：报价 {qs[0]['qty']}{row['unit']}，图纸计算 "
                                                                          f"{row['qty']}{row['unit']}（{row.get('calc', '')}）",
                                  "sources": [row["quote_row"], "图纸计算"]})
    return {"schema": "aiw-brief/1", "version": VERSION, "task": task, "made": time.strftime("%Y-%m-%d %H:%M"),
            "type": route["type"], "type_label": route["label"], "project": tx["project"] or "施工图",
            "prompt": prompt, "facts": facts, "images": imgs, "quote": q_items, "site": site,
            "derived": derived, "conflicts": conflicts, "unknowns": unknowns, "quantities": qty,
            "sources": _sources(imgs, quotes)}


def _sources(imgs, quotes) -> str:
    parts = ["用户文字说明"]
    for i in imgs or []:
        parts.append(f"图{i['index']}{i.get('role') or ''}")
    if quotes:
        parts.append("报价单")
    return "＋".join(parts)


def _site_defaults(site: dict, unknown, derived, conflicts, imgs) -> None:
    """fill every number the drawings need; whatever the user did not give is an assumption listed as 待确认."""
    pl = site.get("plaza")
    if not pl or not pl.get("L"):
        unknown("plaza.size", "场地（广场）的长和宽是多少米？", "无法出图", blocking=True)
        return
    L, W = pl["L"], pl["W"]
    calc = _r2(L * W / 1e6)
    derived.append({"key": "plaza.area_calc", "value": calc, "calc": f"{L / 1000:g}×{W / 1000:g}"})
    if pl.get("area_m2") is not None and abs(pl["area_m2"] - calc) > 0.05:
        conflicts.append({"key": "plaza.area", "what": f"用户写面积 {pl['area_m2']}㎡，长×宽算得 {calc}㎡", "sources": ["用户文字"]})
    pl.setdefault("area_m2", calc)
    ground = site.get("ground", {}).get("existing", "原地面")
    st = site.get("steps")
    rs = site.get("raise")
    if st is not None:
        st.setdefault("n", 2)
        st.setdefault("riser", 150); st.setdefault("tread", 350)
        unknown("steps.riser", "台阶每步高、踏面宽是多少？", f"踏步高 {st['riser']}、踏面宽 {st['tread']}（常用户外台阶尺寸）")
    if rs is not None or st is not None:
        h = (st or {}).get("n", 2) * (st or {}).get("riser", 150)
        site.setdefault("raise", {})["h"] = h
        unknown("raise.h", "广场整体抬高多少（完成面比水泥路高多少）？",
                f"按{(st or {}).get('n', 2)}步台阶×{(st or {}).get('riser', 150)}，广场完成面比水泥路高 {h}（广场 +{h / 1000:.3f}，水泥路 ±0.000）")
    pv = site.setdefault("paving", {"material": "户外瓷砖"})
    pv.setdefault("tile", [600, 600]); pv.setdefault("joint", 3); pv.setdefault("grid", 6000)
    unknown("paving.tile", "户外瓷砖规格（尺寸、厚度、颜色/防滑等级）和排砖方式？",
            f"{pv['tile'][0]}×{pv['tile'][1]}×18 户外防滑瓷砖，浅灰色，通缝铺贴，留缝{pv['joint']}，"
            f"每 {pv['grid'] / 1000:g}m 设伸缩缝（参考效果图：浅色方砖通缝）")
    unknown("paving.layers", "广场基层做法有没有指定？", "通用做法（待确认）：" + "／".join(GENERIC_LAYERS["paving_tile"][::-1]))
    fp = site.get("flag_platform")
    if fp is not None:
        fp.setdefault("L", 6000); fp.setdefault("W", 4000); fp.setdefault("h", 450); fp.setdefault("pole_h", 12000)
        fp.setdefault("railing", fp.get("railing") or "大理石柱子")
        fp.setdefault("post", 200); fp.setdefault("post_gap", 1200); fp.setdefault("rail_h", 1000)
        fp.setdefault("stair_w", 2400); fp.setdefault("stair_n", 3)
        unknown("flag_platform.size", "升旗台尺寸、台面高度、旗杆高度？",
                f"升旗台 {fp['L']}×{fp['W']}，台面比广场高 {fp['h']}（前侧 {fp['stair_n']} 步上台，宽 {fp['stair_w']}），"
                f"居中布置在广场后部（参考效果图），不锈钢旗杆 H={fp['pole_h']}")
        unknown("railing", "大理石柱围栏的柱子尺寸、间距、高度？",
                f"{fp['post']}×{fp['post']} 大理石方柱，柱高 {fp['rail_h']}，柱距≤{fp['post_gap']}，柱间大理石栏板/宝瓶（参考效果图），"
                "上台口不设栏杆")
    sl = site.get("slope")
    if sl is not None:
        sl.setdefault("L", 13500); sl.setdefault("W", 5000); sl.setdefault("H", 1600)
        v_tri = _r2(sl["L"] * sl["W"] * sl["H"] / 2 / 1e9)
        v_box = _r2(sl["L"] * sl["W"] * sl["H"] / 1e9)
        sl["fill_m3"] = v_tri
        derived.append({"key": "slope.backfill", "value": v_tri, "calc": f"{sl['L'] / 1000:g}×{sl['W'] / 1000:g}×{sl['H'] / 1000:g}÷2（三角形断面）"})
        unknown("slope.shape", "后斜坡的 1.6m 是回填后坡顶高出广场的高度，还是要整体填高 1.6m？",
                f"按坡脚接广场完成面、坡顶高出广场 {sl['H']}、三角形断面回填，回填量约 {v_tri}m³（若整体填高则为 {v_box}m³）")
        unknown("slope.position", "后斜坡在广场后方的哪一段？", "沿广场后边居中布置（与升旗台同轴）")
        lawn_area = _r2(sl["L"] * math.hypot(sl["W"], sl["H"]) / 1e6)
        derived.append({"key": "lawn.area", "value": lawn_area, "calc": f"{sl['L'] / 1000:g}×√({sl['W'] / 1000:g}²+{sl['H'] / 1000:g}²)（坡面）"})
    tr = site.get("trees_remove")
    if tr:
        unknown("trees.position", f"{tr['n']}棵{tr.get('what', '树')}的准确位置？",
                f"按现场照片：左右两侧各 {tr['n'] // 2} 棵，位置示意，以现场为准；挖除树根、回填夯实")
    if site.get("drainage"):
        unknown("drain.type", "排水沟形式与尺寸？排到哪里？",
                "300宽成品线性排水沟（缝隙式/铸铁箅子），沟底纵坡0.5%，坡向前侧（水泥路侧）接现状雨水井；广场面横坡 0.5% 坡向两侧排水沟")
    if site.get("court"):
        unknown("court.skew", "与篮球场斜接的具体形状？", "按矩形 27.9×15.8 出图（面积已含斜接部分），斜接边按现场放线")
    unknown("ground.level", f"原地面（{ground}）标高与水泥路的关系？", f"原地面按与水泥路齐平（±0.000）考虑，清表后夯实再做基层")


TOPICS = {"steps.riser": "台阶尺寸", "raise.h": "抬高高度", "paving.tile": "瓷砖规格", "paving.layers": "广场基层", "flag_platform.size": "升旗台",
          "railing": "大理石柱围栏", "slope.shape": "后斜坡", "slope.position": "斜坡位置", "trees.position": "椰子树位置",
          "drain.type": "排水沟", "court.skew": "篮球场斜接", "ground.level": "原地面标高", "plaza.size": "场地尺寸", "main_dims": "主要尺寸"}


# ============================================================================================ quantities

def quantities(brief: dict) -> list:
    site = brief.get("site") or {}
    rows = []

    def row(key, name, spec, unit, qty, calc):
        rows.append({"key": key, "name": name, "spec": spec, "unit": unit, "qty": _r2(qty), "calc": calc})
    pl = site.get("plaza") or {}
    if not pl.get("L"):
        return rows
    L, W = pl["L"] / 1000, pl["W"] / 1000
    A = pl.get("area_m2") or _r2(L * W)
    tr = site.get("trees_remove")
    if tr:
        row("trees_remove", f"移除{tr.get('what') or '树'}", "挖除树根、坑位回填夯实", "棵", tr["n"], "现场计数")
    row("base", "地面基础处理", f"清除{(site.get('ground') or {}).get('existing', '原地面')}表层、素土夯实（压实系数≥0.93）", "㎡", A, f"{L:g}×{W:g}")
    row("gravel", "级配碎石垫层", "150厚", "m³", A * 0.15, f"{A}×0.15")
    row("concrete", "C20混凝土垫层", "100厚，分仓≤6m", "m³", A * 0.10, f"{A}×0.10")
    fp = site.get("flag_platform")
    fpA = (fp["L"] * fp["W"] / 1e6) if fp else 0
    pv = site.get("paving") or {}
    t = pv.get("tile", [600, 600])
    row("tile", "户外瓷砖铺贴", f"{t[0]}×{t[1]}×18 防滑砖，30厚1:3干硬性砂浆结合层", "㎡", A - fpA,
        f"{A}" + (f"−{fpA:g}（升旗台占地）" if fpA else ""))
    st = site.get("steps")
    if st:
        n = st.get("n", 2)
        row("steps", f"{n}步通宽台阶", f"踏步 {st.get('tread', 350)}×{st.get('riser', 150)}，面层同广场砖", "m", L, f"通宽 {L:g}m")
        row("steps_face", "台阶面层（踏面+踢面）", "防滑踏步砖", "㎡", L * n * (st.get("tread", 350) + st.get("riser", 150)) / 1000,
            f"{L:g}×{n}×({st.get('tread', 350) / 1000:g}+{st.get('riser', 150) / 1000:g})")
    if fp:
        row("platform", "新建升旗台", f"{fp['L']}×{fp['W']}，台高{fp['h']}，C25钢筋混凝土台身，石材台面", "座", 1, "1")
        per = 2 * (fp["L"] + fp["W"]) / 1000 - fp["stair_w"] / 1000
        row("railing", "大理石柱围栏", f"{fp['post']}×{fp['post']}柱，H={fp['rail_h']}，柱距≤{fp['post_gap']}", "m", per,
            f"2×({fp['L'] / 1000:g}+{fp['W'] / 1000:g})−{fp['stair_w'] / 1000:g}（上台口）")
        row("flagpole", "不锈钢旗杆", f"H={fp['pole_h']}，含基础预埋", "根", 1, "1")
    sl = site.get("slope")
    if sl:
        row("backfill", "后斜坡回填", "分层回填夯实，每层≤300", "m³", sl.get("fill_m3") or sl["L"] * sl["W"] * sl["H"] / 2e9,
            f"{sl['L'] / 1000:g}×{sl['W'] / 1000:g}×{sl['H'] / 1000:g}÷2")
        row("lawn", "铺草坪", "满铺草皮，300厚种植土", "㎡", sl["L"] * math.hypot(sl["W"], sl["H"]) / 1e6,
            f"{sl['L'] / 1000:g}×√({sl['W'] / 1000:g}²+{sl['H'] / 1000:g}²)")
    dr = site.get("drainage") or []
    if dr:
        row("drain", "线性排水沟", "300宽，C25沟身，箅子盖板，纵坡0.5%", "m", W * len(dr), "×".join([f"{W:g}", str(len(dr))]) + "（每处沿广场边通长）")
        row("drain_pit", "排水沟出水口接现状雨水井", "含接驳管 DN200", "处", len(dr), str(len(dr)))
    return rows


# ============================================================================================ sheet list

def plan_sheets(brief: dict) -> list:
    t = brief.get("type") or "site"
    tpl = TEMPLATES.get(t) or TEMPLATES["site"]
    site = brief.get("site") or {}
    out = []
    prefix = {"site": "JS", "interior": "ZS", "structure": "JG"}.get(t, "S")
    for key, title in tpl:
        need = _SITE_NEEDS.get(key) if t == "site" else None
        if need and not any(site.get(n) for n in need):
            continue
        if key == "platform" and t == "site":
            title = "升旗台平面、立面、剖面图"
        if key == "steps" and t == "site" and not site.get("flag_platform"):
            title = "台阶大样图"
        out.append({"key": key, "title": title})
    for i, s in enumerate(out):
        s["no"] = f"{prefix}-{i:02d}"
    return out


# ============================================================================================ requirements

def requirements(brief: dict) -> list:
    """what each sheet must visibly carry; checked on the built set (tags / dims / texts) and asked of the vision
    model per sheet."""
    site = brief.get("site") or {}
    req = []
    pl = site.get("plaza") or {}
    if pl.get("L"):
        req += [{"sheet": "site_plan", "dim": pl["L"], "what": f"广场长 {pl['L']} 尺寸标注"},
                {"sheet": "site_plan", "dim": pl["W"], "what": f"广场宽 {pl['W']} 尺寸标注"},
                {"sheet": "paving", "tag": "tile", "min": 1, "what": "瓷砖排版填充"},
                {"sheet": "paving", "text": f"{(site.get('paving') or {}).get('tile', [600, 600])[0]}×", "what": "瓷砖规格标注"}]
    tr = site.get("trees_remove")
    if tr:
        req.append({"sheet": "site_plan", "tag": "tree_remove", "count": tr["n"], "what": f"{tr['n']} 棵{tr.get('what', '树')}（拆除）"})
    st = site.get("steps")
    if st:
        req.append({"sheet": "steps", "tag": "step_riser", "count": st.get("n", 2), "what": f"{st.get('n', 2)} 步台阶（剖面踏步）"})
        req.append({"sheet": "site_plan", "tag": "steps_plan", "min": 1, "what": "临水泥路一侧通宽台阶"})
    if site.get("flag_platform"):
        req += [{"sheet": "platform", "tag": "post", "min": 4, "what": "大理石柱围栏的柱子"},
                {"sheet": "platform", "tag": "flagpole", "min": 1, "what": "旗杆"}]
    sl = site.get("slope")
    if sl:
        req += [{"sheet": "section", "tag": "lawn", "min": 1, "what": "后斜坡草坪剖面"},
                {"sheet": "section", "text": f"{sl['H']}", "what": f"坡高 {sl['H']} 标注"},
                {"sheet": "site_plan", "dim": sl["L"], "what": f"斜坡长 {sl['L']}"}]
    if site.get("drainage"):
        req.append({"sheet": "grading", "tag": "drain", "count": len(site["drainage"]), "what": f"{len(site['drainage'])} 处排水沟"})
        req.append({"sheet": "drain", "tag": "drain_detail", "count": len(site["drainage"]), "what": "排水节点大样（每处一个）"})
    req.append({"sheet": "qty", "tag": "table", "min": 1, "what": "工程量表"})
    req.append({"sheet": "cover", "tag": "table", "min": 1, "what": "图纸目录表"})
    return req


# ============================================================================================ spec generation

def spec_from_brief(brief: dict) -> dict:
    """a drawkit `sheet_set` spec drawn parametrically from the brief (site jobs). Interior / structure jobs get the
    cover + quantity sheets and empty sheet slots with their template titles: the model fills their views."""
    sheets = plan_sheets(brief)
    site = brief.get("site") or {}
    G = _SiteGeom(site) if (brief.get("type") == "site" and (site.get("plaza") or {}).get("L")) else None
    out = []
    for s in sheets:
        fn = getattr(G, "sheet_" + s["key"], None) if G else None
        body = fn() if fn else ({"views": [], "todo": f"{s['title']}：按资料补画视图"} if s["key"] not in ("cover", "qty") else {})
        if s["key"] == "cover":
            body = _cover(brief, sheets)
        elif s["key"] == "qty":
            body = _qty_sheet(brief)
        out.append({"no": s["no"], "key": s["key"], "title": s["title"], "paper": body.pop("paper", "A3"), **body})
    reqs = requirements(brief)
    return {"schema": "aiw-drawing/1", "kind": "sheet_set", "project": brief.get("project") or "施工图",
            "source_note": brief.get("sources") or "", "job_type": brief.get("type"), "brief": "cad/brief.json",
            "sheets": out, "requirements": reqs,
            "quantities": brief.get("quantities") or [], "assumptions": [f"{u.get('topic') or u['key']}：{u['assumption']}" for u in brief.get("unknowns") or [] if not u.get("blocking")],
            "conflicts": [c["what"] for c in brief.get("conflicts") or []]}


def _cover(brief, sheets) -> dict:
    site = brief.get("site") or {}
    rows = [[str(i + 1), s["no"], s["title"], "A3", ""] for i, s in enumerate(sheets)]
    notes = []
    pl = site.get("plaza") or {}
    if pl.get("L"):
        notes.append(f"工程概况：{pl.get('name') or '场地'} {pl['L'] / 1000:g}m×{pl['W'] / 1000:g}m＝{pl.get('area_m2')}㎡"
                     + (f"（{pl['area_note']}）" if pl.get("area_note") else "") +
                     f"，原地面为{(site.get('ground') or {}).get('existing', '原状地面')}。")
    if site.get("sequence"):
        notes.append("施工顺序：" + " → ".join(site["sequence"]) + "。")
    notes += ["图中尺寸以毫米（mm）计，标高以米（m）计；±0.000 为现状水泥路面。",
              "本套图依据：" + (brief.get("sources") or "用户说明") + "。现状照片无尺寸，所有尺寸以用户给定数据为准，其余为下列假定。",
              "通用做法：图中标“通用做法”的构造层为常规做法，用户/报价未指定，施工前须确认。",
              "施工前须现场复核场地尺寸、现状标高、泳池/篮球场边线及排水出口位置，与图纸不符时及时反馈。"]
    if brief.get("conflicts"):
        notes.append("资料矛盾：" + "；".join(c["what"] for c in brief["conflicts"]) + "。")
    assume = [f"{u.get('topic') or u['key']}：{u['assumption']}" for u in brief.get("unknowns") or [] if not u.get("blocking")]
    return {"table": {"title": "图纸目录", "cols": ["序号", "图号", "图名", "图幅", "备注"], "widths": [14, 26, 70, 16, 24], "rows": rows,
                      "at": "left"},
            "notes_title": "设计说明", "notes": notes, "notes2_title": "假定及待确认（施工前确认）", "notes2": assume,
            "layout": "cover"}


def _qty_sheet(brief) -> dict:
    rows = []
    for i, r in enumerate(brief.get("quantities") or [], 1):
        q = r.get("quote_qty")
        diff = "" if q is None else ("一致" if abs(q - r["qty"]) <= max(0.05 * r["qty"], 0.5) else f"差 {_r2(q - r['qty'])}")
        rows.append([str(i), r["name"], r.get("spec", ""), r["unit"], r.get("calc", ""), f"{r['qty']:g}",
                     "—" if q is None else f"{q:g}", diff or ("未提供报价" if q is None else "")])
    return {"table": {"title": "工程量表（图纸计算，与报价对照）", "cols": ["序号", "项目", "做法/规格", "单位", "计算式", "工程量", "报价量", "对照"],
                      "widths": [12, 34, 92, 12, 68, 20, 18, 24], "rows": rows, "at": "left", "tag": "table"},
            "notes": ["工程量按本套图纸尺寸计算，含假定尺寸的项目随假定确认后调整。",
                      "瓷砖面积已扣除升旗台占地；损耗、勾缝剂、切割另计。"] + (["未提供报价/清单：报价量一栏留空，供报价核对。"]
                                                                if not brief.get("quote") else [])}


ROAD = 3000   # depth of the existing road drawn in front of the plaza (model mm)


class _SiteGeom:
    """parametric site drawings in model mm, y up: the plaza is x 0..L (along the road), y 0..W (from the road side
    back). Road in front (y<0), the slope behind (y>W), pool on the left (x<0), court on the right (x>L)."""

    def __init__(self, site):
        self.s = site
        pl = site["plaza"]
        self.L, self.W = pl["L"], pl["W"]
        st = site.get("steps") or {}
        self.n_st = st.get("n", 2) if site.get("steps") is not None else 0
        self.tread, self.riser = st.get("tread", 350), st.get("riser", 150)
        self.raise_h = (site.get("raise") or {}).get("h", self.n_st * self.riser)
        self.fp = site.get("flag_platform")
        self.sl = site.get("slope")
        self.dr = site.get("drainage") or []
        self.pv = site.get("paving") or {}
        self.ax = self.L / 2

    # ---------------------------------------------------------------- shared plan pieces
    def _context(self, dashed=True):
        L, W = self.L, self.W
        e = []
        y_road = -(self.n_st * self.tread) if self.n_st else 0
        e.append({"type": "line", "a": [-6000, y_road], "b": [L + 6000, y_road], "layer": "现状", "lw": 0.35})
        e.append({"type": "line", "a": [-6000, y_road - ROAD], "b": [L + 6000, y_road - ROAD], "layer": "现状", "lw": 0.35})
        e.append({"type": "hatch", "pattern": "concrete", "box": [-6000, y_road - ROAD, L + 6000, y_road], "layer": "现状填充"})
        e.append({"type": "text", "at": [L / 4, y_road - ROAD / 2], "text": "现状水泥路  ±0.000", "h": 3.5, "mask": True})
        if self.s.get("pool"):
            e.append({"type": "rect", "box": [-6000, 1500, -900, W - 1500], "layer": "现状", "ls": "dashed"})
            e.append({"type": "hatch", "pattern": "water", "box": [-6000, 1500, -900, W - 1500], "layer": "现状填充"})
            e.append({"type": "text", "at": [-3450, W / 2], "text": "现状游泳池", "h": 3.5, "mask": True})
            e.append({"type": "line", "a": [-900, 0], "b": [-900, W], "layer": "现状", "ls": "dashed"})
        if self.s.get("court"):
            e.append({"type": "rect", "box": [L + 900, 0, L + 6000, W], "layer": "现状", "ls": "dashed"})
            e.append({"type": "text", "at": [L + 3450, W / 2], "text": "现状篮球场", "h": 3.5, "rot": 90, "mask": True})
        return e

    def _plaza_outline(self, lw=0.5):
        return [{"type": "rect", "box": [0, 0, self.L, self.W], "layer": "铺装", "lw": lw, "tag": "plaza"}]

    def _steps_plan(self):
        e = []
        for i in range(1, self.n_st + 1):
            y = -i * self.tread
            e.append({"type": "line", "a": [0, y + self.tread], "b": [self.L, y + self.tread], "layer": "构造", "lw": 0.35, "tag": "steps_plan" if i == 1 else None})
        if self.n_st:
            y0 = -self.n_st * self.tread
            e.append({"type": "line", "a": [0, y0], "b": [self.L, y0], "layer": "构造", "lw": 0.5})
            e.append({"type": "line", "a": [0, y0], "b": [0, 0], "layer": "构造", "lw": 0.35})
            e.append({"type": "line", "a": [self.L, y0], "b": [self.L, 0], "layer": "构造", "lw": 0.35})
            e.append({"type": "arrow", "a": [self.L * 0.86, y0 + 60], "b": [self.L * 0.86, -30], "text": "上", "layer": "标注"})
        return e

    def _platform_plan(self, detail=False):
        fp = self.fp
        if not fp:
            return []
        x0, x1 = self.ax - fp["L"] / 2, self.ax + fp["L"] / 2
        y1 = self.W - 900
        y0 = y1 - fp["W"]
        e = [{"type": "rect", "box": [x0, y0, x1, y1], "layer": "构造", "lw": 0.5, "tag": "platform"}]
        if detail:
            e.append({"type": "hatch", "pattern": "stone", "box": [x0, y0, x1, y1], "layer": "填充"})
        # stair in front, centred
        sw, n = fp["stair_w"], fp["stair_n"]
        for i in range(n + 1):
            yy = y0 - i * 300
            e.append({"type": "line", "a": [self.ax - sw / 2, yy], "b": [self.ax + sw / 2, yy], "layer": "构造", "lw": 0.35})
        e.append({"type": "line", "a": [self.ax - sw / 2, y0], "b": [self.ax - sw / 2, y0 - n * 300], "layer": "构造", "lw": 0.35})
        e.append({"type": "line", "a": [self.ax + sw / 2, y0], "b": [self.ax + sw / 2, y0 - n * 300], "layer": "构造", "lw": 0.35})
        # posts along the edge, the stair opening left open
        p = fp["post"]
        gap = fp["post_gap"]
        posts = []

        def run(a, b, skip=None):
            Lr = math.hypot(b[0] - a[0], b[1] - a[1])
            k = max(1, math.ceil(Lr / gap))
            for i in range(k + 1):
                t = i / k
                x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                if skip and skip[0] < x < skip[1] and abs(y - a[1]) < 1:
                    continue
                posts.append((round(x), round(y)))
        ins = p / 2 + 50
        run((x0 + ins, y1 - ins), (x1 - ins, y1 - ins))
        run((x0 + ins, y0 + ins), (x0 + ins, y1 - ins))
        run((x1 - ins, y0 + ins), (x1 - ins, y1 - ins))
        # front: two runs either side of the stair opening
        run((x0 + ins, y0 + ins), (self.ax - sw / 2 - p / 2, y0 + ins))
        run((self.ax + sw / 2 + p / 2, y0 + ins), (x1 - ins, y0 + ins))
        seen = []
        for q in posts:
            if all(abs(q[0] - s[0]) > 5 or abs(q[1] - s[1]) > 5 for s in seen):
                seen.append(q)
        for (x, y) in seen:
            e.append({"type": "rect", "box": [x - p / 2, y - p / 2, x + p / 2, y + p / 2], "layer": "构造", "lw": 0.35, "tag": "post",
                      "fill": 0.55 if not detail else 0.35})
        # rail between posts (thin double line) on the three closed sides + front runs
        for (a, b) in (((x0 + ins, y1 - ins), (x1 - ins, y1 - ins)), ((x0 + ins, y0 + ins), (x0 + ins, y1 - ins)),
                       ((x1 - ins, y0 + ins), (x1 - ins, y1 - ins)), ((x0 + ins, y0 + ins), (self.ax - sw / 2 - p / 2, y0 + ins)),
                       ((self.ax + sw / 2 + p / 2, y0 + ins), (x1 - ins, y0 + ins))):
            e.append({"type": "line", "a": list(a), "b": list(b), "layer": "构造", "lw": 0.18})
        pole = (self.ax, (y0 + y1) / 2 + 400)
        e.append({"type": "circle", "c": list(pole), "r": 300 if detail else 300, "layer": "构造", "lw": 0.35})
        e.append({"type": "circle", "c": list(pole), "r": 60, "layer": "构造", "lw": 0.5, "tag": "flagpole", "fill": 0.8})
        self.fp_box = (x0, y0, x1, y1)
        self.pole = pole
        self.posts = seen
        return e

    def _slope_plan(self):
        sl = self.sl
        if not sl:
            return []
        x0, x1 = self.ax - sl["L"] / 2, self.ax + sl["L"] / 2
        y0, y1 = self.W, self.W + sl["W"]
        e = [{"type": "rect", "box": [x0, y0, x1, y1], "layer": "绿化", "lw": 0.35, "tag": "slope"},
             {"type": "hatch", "pattern": "grass", "box": [x0, y0, x1, y1], "layer": "绿化", "tag": "lawn_plan"}]
        for k in range(1, 9):   # slope hachures (long / short), drawn from the top edge down
            x = x0 + k * sl["L"] / 9
            e.append({"type": "line", "a": [x, y1], "b": [x, y1 - (sl["W"] * (0.55 if k % 2 else 0.3))], "layer": "绿化", "lw": 0.18})
        return e

    def _trees(self):
        tr = self.s.get("trees_remove")
        if not tr:
            return []
        n = tr["n"]
        left, right = (n + 1) // 2, n // 2
        e = []
        for side, k in ((0, left), (1, right)):
            x = 1800 if side == 0 else self.L - 1800
            for i in range(k):
                y = self.W * (i + 1) / (k + 1)
                e.append({"type": "tree", "c": [x, y], "r": 1500, "remove": True, "tag": "tree_remove"})
        return e

    def _drain_lines(self):
        e = []
        for d in self.dr:
            names = "".join(d.get("between") or [])
            if re.search(r"泳池", names):
                xs = (0, 300)
            elif re.search(r"球场", names):
                xs = (self.L - 300, self.L)
            else:
                xs = None
            if xs is None:
                e.append({"type": "rect", "box": [0, self.W - 300, self.L, self.W], "layer": "排水", "lw": 0.35, "tag": "drain"})
                continue
            e.append({"type": "rect", "box": [xs[0], 0, xs[1], self.W], "layer": "排水", "lw": 0.35, "tag": "drain"})
            e.append({"type": "hatch", "pattern": "grate", "box": [xs[0], 0, xs[1], self.W], "layer": "排水"})
        return e

    def _north(self, at):
        return [{"type": "north", "at": at, "r": 8}]

    # ---------------------------------------------------------------- sheets
    def sheet_site_plan(self):
        L, W = self.L, self.W
        e = self._context() + self._plaza_outline(0.7) + self._steps_plan() + self._platform_plan() + self._slope_plan() + self._trees()
        e.append({"type": "label", "at": [L * 0.25, W * 0.5], "text": f"新建广场 户外瓷砖铺装 {L / 1000:g}×{W / 1000:g}＝{self.s['plaza'].get('area_m2')}㎡", "h": 3.5,
                  "dir": "free"})
        if self.n_st:
            e.append({"type": "label", "at": [L * 0.62, -self.tread * self.n_st / 2], "text": f"{self.n_st}步通宽台阶（临水泥路一侧）", "h": 3.0, "dir": "down"})
        if self.fp:
            e.append({"type": "label", "at": [self.fp_box[2], (self.fp_box[1] + self.fp_box[3]) / 2], "text": "新建升旗台（大理石柱围栏）", "h": 3.0, "dir": "right"})
        if self.sl:
            e.append({"type": "label", "at": [self.ax - self.sl["L"] / 2 + 1500, W + self.sl["W"] * 0.6],
                      "text": f"后斜坡回填后铺草坪 {self.sl['L'] / 1000:g}×{self.sl['W'] / 1000:g}×高{self.sl['H'] / 1000:g}m", "h": 3.0, "dir": "left"})
        tr = self.s.get("trees_remove")
        if tr:
            e.append({"type": "label", "at": [1800 + 1100, W * (1 / (((tr['n'] + 1) // 2) + 1)) - 1100],
                      "text": f"现状{tr.get('what', '树')}移除（共{tr['n']}棵，左右各{tr['n'] // 2}棵）", "h": 3.0, "dir": "up"})
        dims = [{"a": [0, 0], "b": [L, 0], "offset": -(self.n_st * self.tread + ROAD) / 100 - 9, "value": L},
                {"a": [L, 0], "b": [L, W], "offset": 10 if not self.s.get("court") else 68, "value": W}]
        if self.sl:
            dims.append({"a": [self.ax - self.sl["L"] / 2, W + self.sl["W"]], "b": [self.ax + self.sl["L"] / 2, W + self.sl["W"]], "offset": 8, "value": self.sl["L"]})
            dims.append({"a": [self.ax - self.sl["L"] / 2, W], "b": [self.ax - self.sl["L"] / 2, W + self.sl["W"]], "offset": -8, "value": self.sl["W"]})
        lv = [{"type": "level", "at": [L * 0.4, W * 0.18], "value": f"+{self.raise_h / 1000:.3f}", "note": "广场完成面"}]
        e += lv + self._north([L + 3500, W + 3800])
        e.append({"type": "section_mark", "a": [self.ax + 600, -self.n_st * self.tread - 1500], "b": [self.ax + 600, W + (self.sl["W"] if self.sl else 0) + 1200],
                  "label": "1"})
        legend = [{"sym": "tree_remove", "text": f"现状{(tr or {}).get('what', '树')}（移除）"}] if tr else []
        legend += [{"sym": "dashed", "text": "现状保留（不施工）"}, {"sym": "grass", "text": "草坪"}]
        return {"views": [{"title": "总平面及拆除平面图", "scale": 100, "entities": e, "dims": dims}],
                "legend": legend,
                "notes": [f"拆除：移除广场左右两侧现状{(tr or {}).get('what', '椰子树')}共{(tr or {}).get('n', 0)}棵（位置示意，以现场为准），挖除树根，坑位分层回填夯实。" if tr else "",
                          f"广场范围 {L / 1000:g}m×{W / 1000:g}m＝{self.s['plaza'].get('area_m2')}㎡（含与篮球场斜接部分，斜接边按现场放线）。",
                          f"广场整体抬高，完成面 +{self.raise_h / 1000:.3f}（现状水泥路面 ±0.000）。" if self.raise_h else "",
                          "泳池、篮球场、水泥路为现状保留，施工时做好成品保护。"]}

    def sheet_paving(self):
        L, W = self.L, self.W
        t = self.pv.get("tile", [600, 600])
        g = self.pv.get("grid", 6000)
        e = self._plaza_outline(0.7) + self._steps_plan() + self._platform_plan() + self._drain_lines()
        holes = [list(self.fp_box)] if self.fp else []
        dr_boxes = [d["box"] for d in e if d.get("tag") == "drain"]
        e.append({"type": "hatch", "pattern": "tile", "box": [0, 0, L, W], "size": t, "origin": [self.ax, 0], "holes": holes + dr_boxes,
                  "layer": "铺装", "tag": "tile"})
        # expansion joints every g from the axis
        xs = sorted({self.ax + k * g for k in range(-10, 11) if 0 < self.ax + k * g < L})
        for x in xs:
            e.append({"type": "line", "a": [x, 0], "b": [x, W], "layer": "铺装", "lw": 0.25, "ls": "dashdot", "tag": "joint"})
        for y in range(g, int(W), g):
            e.append({"type": "line", "a": [0, y], "b": [L, y], "layer": "铺装", "lw": 0.25, "ls": "dashdot", "tag": "joint"})
        e.append({"type": "centerline", "a": [self.ax, -self.n_st * self.tread - 800], "b": [self.ax, W + 800]})
        e.append({"type": "label", "at": [self.ax - g * 1.5 + 300, W * 0.3], "text": f"{t[0]}×{t[1]}×18 户外防滑瓷砖（浅灰），通缝铺贴，留缝{self.pv.get('joint', 3)}",
                  "h": 3.0, "dir": "down"})
        e.append({"type": "label", "at": [xs[0] if xs else L / 3, W * 0.62], "text": f"伸缩缝 @{g}（10宽，嵌缝膏）", "h": 3.0, "dir": "up"})
        e.append({"type": "label", "at": [self.ax, W * 0.12], "text": "排砖自中轴线向两侧", "h": 3.0, "dir": "right"})
        e += [{"type": "slope", "at": [self.ax - L * 0.22, W * 0.45], "dir": 180, "text": "i=0.5%"},
              {"type": "slope", "at": [self.ax + L * 0.22, W * 0.45], "dir": 0, "text": "i=0.5%"}]
        dims = [{"chain": sorted({0, L} | set(xs)), "axis": "x", "base": 0, "offset": -(self.n_st * self.tread / 100 + 14)},
                {"a": [0, 0], "b": [L, 0], "offset": -(self.n_st * self.tread / 100 + 24), "value": L},
                {"a": [L, 0], "b": [L, W], "offset": 10, "value": W}]
        return {"views": [{"title": "铺装平面图", "scale": 100, "entities": e, "dims": dims}],
                "legend": [{"sym": "tile", "text": f"户外瓷砖 {t[0]}×{t[1]}"}, {"sym": "joint", "text": "伸缩缝"},
                           {"sym": "grate", "text": "线性排水沟（箅子）"}],
                "notes": [f"面层：{t[0]}×{t[1]}×18 户外防滑瓷砖（防滑等级 R11 以上），颜色参照效果图浅灰色，通缝铺贴。",
                          "排砖：以广场中轴线为起点向两侧排砖，边部非整砖≥1/2 砖，切割边置于排水沟侧。",
                          f"伸缩缝：纵横 @{g}，缝宽10，嵌缝膏填缝，与混凝土垫层分仓缝对齐。",
                          "基层（通用做法，待确认）：素土夯实→150厚级配碎石→100厚C20混凝土→30厚1:3干硬性水泥砂浆→面层。"]}

    def sheet_grading(self):
        L, W = self.L, self.W
        h = self.raise_h / 1000
        e = self._context() + self._plaza_outline(0.7) + self._steps_plan() + self._platform_plan() + self._drain_lines() + self._slope_plan()
        ridge = h + 0.5 / 100 * (L / 2 - 300) / 1000
        e.append({"type": "centerline", "a": [self.ax, 0], "b": [self.ax, W]})
        e += [{"type": "level", "at": [self.ax - 1200, W * 0.35], "value": f"+{ridge:.3f}", "note": "分水线"},
              {"type": "level", "at": [900, W * 0.12], "value": f"+{h:.3f}"},
              {"type": "level", "at": [L - 3200, W * 0.12], "value": f"+{h:.3f}"},
              {"type": "slope", "at": [self.ax - L * 0.24, W * 0.62], "dir": 180, "text": "i=0.5%"},
              {"type": "slope", "at": [self.ax + L * 0.24, W * 0.62], "dir": 0, "text": "i=0.5%"}]
        if self.fp:
            e.append({"type": "level", "at": [self.fp_box[0] + 500, self.fp_box[3] - 800], "value": f"+{h + self.fp['h'] / 1000:.3f}", "note": "台面"})
        if self.sl:
            e.append({"type": "level", "at": [self.ax + self.sl["L"] / 2 - 2500, W + self.sl["W"] - 900],
                      "value": f"+{h + self.sl['H'] / 1000:.3f}", "note": "坡顶"})
            e.append({"type": "slope", "at": [self.ax + self.sl["L"] / 2 - 4500, W + self.sl["W"] * 0.45], "dir": 270,
                      "text": f"坡度 1:{self.sl['W'] / self.sl['H']:.1f}"})
        for d in [x for x in e if x.get("tag") == "drain"]:
            b = d["box"]
            cx = (b[0] + b[2]) / 2
            e.append({"type": "arrow", "a": [cx, W * 0.75], "b": [cx, W * 0.35], "text": "沟底 i=0.5%", "layer": "排水", "side": "out"})
            e.append({"type": "rect", "box": [cx - 400, -self.n_st * self.tread - 1600, cx + 400, -self.n_st * self.tread - 800], "layer": "排水", "lw": 0.35, "tag": "pit"})
            e.append({"type": "line", "a": [cx, 0], "b": [cx, -self.n_st * self.tread - 800], "layer": "排水", "lw": 0.5, "ls": "dashed"})
        if self.dr:
            e.append({"type": "label", "at": [300 if any("泳池" in "".join(d.get("between") or []) for d in self.dr) else L - 300, W * 0.85],
                      "text": "泳池—广场连接处 300宽线性排水沟", "h": 3.0, "dir": "right"})
            if len(self.dr) > 1:
                e.append({"type": "label", "at": [L - 150, W * 0.9], "text": "广场—篮球场连接处 300宽线性排水沟", "h": 3.0, "dir": "left"})
            e.append({"type": "label", "at": [300 + 0, -self.n_st * self.tread - 1200], "text": "接现状雨水井（位置现场确定）", "h": 3.0, "dir": "right"})
        return {"views": [{"title": "竖向及排水平面图", "scale": 100, "entities": e,
                           "dims": [{"a": [0, 0], "b": [L, 0], "offset": -(self.n_st * self.tread + ROAD) / 100 - 9, "value": L}]}],
                "legend": [{"sym": "level", "text": "完成面标高（m）"}, {"sym": "slope", "text": "排水坡向"}, {"sym": "grate", "text": "线性排水沟"}],
                "notes": [f"广场完成面 +{h:.3f}，以中轴线为分水线，横坡 0.5% 坡向两侧排水沟。",
                          "排水沟沟底纵坡 0.5%，坡向水泥路一侧，出水口接现状雨水井（DN200），位置现场确定。",
                          "后斜坡草坪雨水沿坡面排向广场后边，建议坡脚设 200 宽卵石明沟（如需请确认）。"]}

    def sheet_platform(self):
        fp = self.fp
        x0, y0, x1, y1 = self.fp_box if hasattr(self, "fp_box") else (0, 0, 0, 0)
        if not hasattr(self, "fp_box"):
            self._platform_plan()
            x0, y0, x1, y1 = self.fp_box
        plan = self._platform_plan(detail=True)
        sw, n = fp["stair_w"], fp["stair_n"]
        plan.append({"type": "label", "at": [x0 + 300, y1 - 300], "text": f"{fp['post']}×{fp['post']} 大理石柱 @≤{fp['post_gap']}", "h": 3.0, "dir": "left"})
        plan.append({"type": "label", "at": list(self.pole), "text": f"不锈钢旗杆 H={fp['pole_h']}", "h": 3.0, "dir": "right"})
        plan.append({"type": "label", "at": [self.ax + sw / 2 - 200, y0 - n * 150], "text": f"上台踏步 {n}步 300×150", "h": 3.0, "dir": "right"})
        plan.append({"type": "section_mark", "a": [self.ax - 500, y0 - n * 300 - 900], "b": [self.ax - 500, y1 + 900], "label": "2"})
        pdims = [{"a": [x0, y1], "b": [x1, y1], "offset": 9, "value": fp["L"]},
                 {"a": [x1, y0], "b": [x1, y1], "offset": 9, "value": fp["W"]},
                 {"chain": [x0, self.ax - sw / 2, self.ax + sw / 2, x1], "axis": "x", "base": y0 - n * 300, "offset": -8}]
        # front elevation (x along, z up from plaza finish 0)
        h, rh, p = fp["h"], fp["rail_h"], fp["post"]
        el = [{"type": "line", "a": [x0 - 1500, 0], "b": [x1 + 1500, 0], "layer": "构造", "lw": 0.7},
              {"type": "rect", "box": [x0, 0, x1, h], "layer": "构造", "lw": 0.5},
              {"type": "hatch", "pattern": "stone", "box": [x0, 0, x1, h], "layer": "填充"}]
        for i in range(n):
            el.append({"type": "rect", "box": [self.ax - sw / 2, i * 150, self.ax + sw / 2, (i + 1) * 150], "layer": "构造", "lw": 0.35})
        front_posts = sorted({round(px) for (px, py) in self.posts if abs(py - (y0 + p / 2 + 50)) < 5})
        for px in front_posts:
            el.append({"type": "rect", "box": [px - p / 2, h, px + p / 2, h + rh], "layer": "构造", "lw": 0.35, "tag": "post"})
            el.append({"type": "rect", "box": [px - p / 2 - 25, h + rh, px + p / 2 + 25, h + rh + 80], "layer": "构造", "lw": 0.35})
        segs = []
        left = [px for px in front_posts if px < self.ax]
        right = [px for px in front_posts if px > self.ax]
        for grp in (left, right):
            for a, b in zip(grp, grp[1:]):
                segs.append((a + p / 2, b - p / 2))
        for a, b in segs:
            el.append({"type": "rect", "box": [a, h + rh - 120, b, h + rh], "layer": "构造", "lw": 0.25})
            el.append({"type": "rect", "box": [a, h + 80, b, h + 160], "layer": "构造", "lw": 0.25})
            k = max(2, int((b - a) / 200))
            for j in range(1, k):
                bx = a + (b - a) * j / k
                el.append({"type": "baluster", "x": bx, "y0": h + 160, "y1": h + rh - 120, "w": 90})
        pole_top = h + 3200
        el.append({"type": "line", "a": [self.ax - 57, h], "b": [self.ax - 57, pole_top], "layer": "构造", "lw": 0.35, "tag": "flagpole"})
        el.append({"type": "line", "a": [self.ax + 57, h], "b": [self.ax + 57, pole_top], "layer": "构造", "lw": 0.35})
        el.append({"type": "break", "a": [self.ax - 500, pole_top], "b": [self.ax + 500, pole_top]})
        el.append({"type": "label", "at": [self.ax + 60, h + 2400], "text": f"不锈钢旗杆 Φ114，H={fp['pole_h']}（自台面，折断表示）", "h": 3.0, "dir": "right"})
        el.append({"type": "label", "at": [front_posts[0] if front_posts else x0, h + rh * 0.6], "text": "大理石柱、栏板、扶手（同色）", "h": 3.0, "dir": "left"})
        el += [{"type": "level", "at": [x1 + 400, 0], "value": f"+{self.raise_h / 1000:.3f}", "kind": "elev"},
               {"type": "level", "at": [x1 + 400, h], "value": f"+{(self.raise_h + h) / 1000:.3f}", "kind": "elev"},
               {"type": "level", "at": [x1 + 400, h + rh], "value": f"+{(self.raise_h + h + rh) / 1000:.3f}", "kind": "elev"}]
        edims = [{"chain": [0, h, h + rh], "axis": "y", "base": x0, "offset": -10},
                 {"a": [x0, 0], "b": [x1, 0], "offset": -9, "value": fp["L"]}]
        # section through the platform (2-2): y along depth, z up
        d0 = 0
        sec = self._platform_section()
        return {"views": [{"title": "升旗台平面图", "scale": 50, "entities": plan, "dims": pdims},
                          {"title": "升旗台正立面图", "scale": 50, "entities": el, "dims": edims},
                          {"title": "2-2 升旗台剖面图", "scale": 50, "entities": sec["e"], "dims": sec["dims"]}],
                "notes": [f"升旗台 {fp['L']}×{fp['W']}，台面高出广场 {fp['h']}，C25 钢筋混凝土台身，石材台面（尺寸为假定，待确认）。",
                          f"围栏：{fp.get('railing', '大理石柱子')}，{fp['post']}×{fp['post']} 方柱，柱高 {fp['rail_h']}，柱距≤{fp['post_gap']}，"
                          "柱内预埋 Φ12 钢筋与台身锚固，栏板/扶手同石材。",
                          f"旗杆：不锈钢 Φ114，H={fp['pole_h']}，独立基础预埋件由厂家深化。",
                          "上台踏步：正面居中，踏面 300、踢面 150。"]}

    def _platform_section(self):
        fp = self.fp
        h, rh, p = fp["h"], fp["rail_h"], fp["post"]
        D = fp["W"]
        n = fp["stair_n"]
        y0 = 0
        # 3.9.96 (2-2 fix): the plaza finish line stops at the stair and the platform (it ran through both), the plaza
        # build-up under the finish is drawn (it was blank, so the stair foundation hung in white space below the
        # ground and the steps read as lower than the plaza), and the stair is a closed concrete body ON the plaza slab.
        xl, xr = -n * 300 - 1500, D + 1500
        e = [{"type": "line", "a": [xl, 0], "b": [-n * 300, 0], "layer": "构造", "lw": 0.7},
             {"type": "line", "a": [D, 0], "b": [xr, 0], "layer": "构造", "lw": 0.7}]
        hole = [[0, -550, D, h]]
        e.append({"type": "hatch", "pattern": "mortar", "box": [xl, -50, xr, 0], "holes": hole, "layer": "填充", "material": "面层及结合层"})
        e.append({"type": "hatch", "pattern": "concrete", "box": [xl, -150, xr, -50], "holes": hole, "layer": "填充", "material": "C20混凝土垫层"})
        e.append({"type": "hatch", "pattern": "gravel", "box": [xl, -300, xr, -150], "holes": hole, "layer": "填充", "material": "级配碎石"})
        e.append({"type": "hatch", "pattern": "earth", "box": [xl, -900, xr, -300], "holes": hole, "layer": "填充", "material": "素土夯实"})
        for yy in (-50, -150, -300):
            e.append({"type": "line", "a": [xl, yy], "b": [0, yy], "layer": "构造", "lw": 0.18})
            e.append({"type": "line", "a": [D, yy], "b": [xr, yy], "layer": "构造", "lw": 0.18})
        # platform: stone top, C25 body, 100 C15 垫层, 150 gravel
        e.append({"type": "rect", "box": [0, -550, D, h], "layer": "构造", "lw": 0.5})
        e.append({"type": "hatch", "pattern": "concrete", "box": [0, -300, D, h - 50], "layer": "填充", "material": "C25钢筋混凝土"})
        e.append({"type": "rect", "box": [0, h - 50, D, h], "layer": "构造", "lw": 0.25})
        e.append({"type": "hatch", "pattern": "stone", "box": [0, h - 50, D, h], "layer": "填充"})
        e.append({"type": "line", "a": [0, -300], "b": [D, -300], "layer": "构造", "lw": 0.25})
        e.append({"type": "hatch", "pattern": "concrete", "box": [0, -400, D, -300], "layer": "填充", "material": "C15混凝土垫层"})
        e.append({"type": "line", "a": [0, -400], "b": [D, -400], "layer": "构造", "lw": 0.25})
        e.append({"type": "hatch", "pattern": "gravel", "box": [0, -550, D, -400], "layer": "填充", "material": "级配碎石"})
        # stair in front (negative y): closed concrete body standing on the plaza finish
        st = [[-n * 300, 0]]
        for i in range(n):
            st += [[-(n - i) * 300, (i + 1) * 150], [-(n - i - 1) * 300, (i + 1) * 150]]
        st += [[0, 0]]
        e.append({"type": "hatch", "pattern": "concrete", "points": st, "layer": "填充", "material": "C20混凝土台阶"})
        e.append({"type": "poly", "points": st, "closed": True, "layer": "构造", "lw": 0.5})
        for yy in (D - p / 2 - 50,):
            e.append({"type": "rect", "box": [yy - p / 2, h, yy + p / 2, h + rh], "layer": "构造", "lw": 0.35, "tag": "post"})
        e.append({"type": "label", "at": [D * 0.5, h * 0.4], "text": "C25 钢筋混凝土台身", "h": 3.0, "dir": "down"})
        e.append({"type": "layers", "at": [D * 0.75, h - 25], "items": GENERIC_LAYERS["platform"][:4], "h": 2.5, "title": "台面做法（通用做法，待确认）"})
        e += [{"type": "level", "at": [D + 300, 0], "value": f"+{self.raise_h / 1000:.3f}", "kind": "elev"},
              {"type": "level", "at": [D + 300, h], "value": f"+{(self.raise_h + h) / 1000:.3f}", "kind": "elev"}]
        dims = [{"chain": [-n * 300, 0, D], "axis": "x", "base": -900, "offset": -8},
                {"chain": [0, h, h + rh], "axis": "y", "base": D, "offset": 22}]
        return {"e": e, "dims": dims}

    def sheet_steps(self):
        n, tr, ri = self.n_st, self.tread, self.riser
        views = []
        if n:
            e = []
            x_road = -n * tr - 1200
            e.append({"type": "line", "a": [x_road, 0], "b": [-n * tr, 0], "layer": "现状", "lw": 0.5})
            e.append({"type": "hatch", "pattern": "concrete", "box": [x_road, -200, -n * tr, 0], "layer": "现状填充"})
            e.append({"type": "text", "at": [x_road + 600, 250], "text": "现状水泥路", "h": 2.5})
            prof = [[-n * tr, 0]]
            for i in range(n):
                prof += [[-(n - i) * tr, (i + 1) * ri], [-(n - i - 1) * tr, (i + 1) * ri]]
            prof += [[1500, n * ri]]
            e.append({"type": "poly", "points": prof, "layer": "构造", "lw": 0.7})
            for i in range(n):
                e.append({"type": "line", "a": [-(n - i) * tr, i * ri], "b": [-(n - i) * tr, (i + 1) * ri], "layer": "构造", "lw": 0.7, "tag": "step_riser"})
            # concrete body + gravel + earth below
            body = [[-n * tr, 0]] + prof[1:-1] + [[1500, n * ri], [1500, n * ri - 160], [0, n * ri - 160], [0, -100], [-n * tr, -100]]
            e.append({"type": "hatch", "pattern": "concrete", "points": [[-n * tr, -100], [-n * tr, ri - 40]] +
                      [[p[0], p[1] - 40] for p in prof[1:-1]] + [[1500, n * ri - 40], [1500, n * ri - 160], [0, n * ri - 160], [0, -100]], "layer": "填充"})
            e.append({"type": "poly", "points": [[-n * tr, -100], [0, -100], [0, n * ri - 160], [1500, n * ri - 160]], "layer": "构造", "lw": 0.25})
            # 3.9.96: under the plaza the build-up continues (150 gravel under the C20 slab, earth below); it was blank
            pg = n * ri - 160 - 150
            e.append({"type": "hatch", "pattern": "gravel", "box": [-n * tr, -250, 0, -100], "layer": "填充"})
            e.append({"type": "hatch", "pattern": "gravel", "box": [0, pg, 1500, n * ri - 160], "layer": "填充"})
            e.append({"type": "line", "a": [0, pg], "b": [1500, pg], "layer": "构造", "lw": 0.18})
            e.append({"type": "line", "a": [-n * tr, -250], "b": [0, -250], "layer": "构造", "lw": 0.18})
            e.append({"type": "line", "a": [0, -250], "b": [0, pg], "layer": "构造", "lw": 0.18})
            e.append({"type": "hatch", "pattern": "earth", "points": [[x_road, -450], [1500, -450], [1500, pg], [0, pg], [0, -250], [x_road, -250]],
                      "layer": "填充"})
            e.append({"type": "layers", "at": [-tr * 0.5, n * ri - 20], "items": GENERIC_LAYERS["step"], "h": 2.5, "title": "台阶做法（通用做法，待确认）"})
            e += [{"type": "level", "at": [x_road + 200, 0], "value": "±0.000", "kind": "elev"},
                  {"type": "level", "at": [900, n * ri], "value": f"+{n * ri / 1000:.3f}", "kind": "elev"}]
            dims = [{"chain": [-(n - i) * tr for i in range(n + 1)], "axis": "x", "base": -450, "offset": -8},
                    {"chain": [i * ri for i in range(n + 1)], "axis": "y", "base": x_road, "offset": -8}]
            views.append({"title": f"台阶剖面大样（{n}步通宽）", "scale": 20, "entities": e, "dims": dims})
        if self.fp:
            fp = self.fp
            p, rh, gap = fp["post"], fp["rail_h"], fp["post_gap"]
            e = [{"type": "line", "a": [-300, 0], "b": [2 * gap + p + 300, 0], "layer": "构造", "lw": 0.7}]
            for i in range(3):
                x = i * gap
                e.append({"type": "rect", "box": [x, 0, x + p, rh], "layer": "构造", "lw": 0.5, "tag": "post"})
                e.append({"type": "rect", "box": [x - 25, rh, x + p + 25, rh + 80], "layer": "构造", "lw": 0.5})
                e.append({"type": "hatch", "pattern": "stone", "box": [x, 0, x + p, rh], "layer": "填充"})
            for i in range(2):
                a, b = i * gap + p, (i + 1) * gap
                e.append({"type": "rect", "box": [a, rh - 120, b, rh], "layer": "构造", "lw": 0.35})
                e.append({"type": "rect", "box": [a, 80, b, 160], "layer": "构造", "lw": 0.35})
                k = 4
                for j in range(1, k + 1):
                    bx = a + (b - a) * j / (k + 1)
                    e.append({"type": "baluster", "x": bx, "y0": 160, "y1": rh - 120, "w": 110})
            e.append({"type": "label", "at": [p / 2, rh * 0.7], "text": f"{p}×{p} 大理石方柱", "h": 2.5, "dir": "left"})
            e.append({"type": "label", "at": [gap + p + 200, rh - 60], "text": "大理石扶手 120厚", "h": 2.5, "dir": "up"})
            e.append({"type": "label", "at": [gap * 1.5 + p / 2, rh * 0.5], "text": "大理石宝瓶/栏板（参考效果图）", "h": 2.5, "dir": "right"})
            e.append({"type": "label", "at": [p / 2, -10], "text": "柱内预埋Φ12钢筋锚入台身≥300", "h": 2.5, "dir": "down"})
            dims = [{"chain": [0, p, gap, gap + p, 2 * gap, 2 * gap + p], "axis": "x", "base": 0, "offset": -10},
                    {"chain": [0, 80, 160, rh - 120, rh, rh + 80], "axis": "y", "base": 2 * gap + p, "offset": 10}]
            views.append({"title": "大理石柱围栏立面大样", "scale": 20, "entities": e, "dims": dims})
        return {"views": views,
                "notes": [f"台阶：{n} 步通宽（与广场同宽），踏面 {tr}、踢面 {ri}，踏步前沿设防滑槽，面层同广场砖（尺寸为假定，待确认）。",
                          "台阶两端与现状泳池、篮球场边线收口按现场处理。",
                          "围栏石材颜色、纹理参照效果图，石材六面防护，干挂/湿贴由石材厂家深化。"]}

    def _profile(self):
        """1-1 profile along the axis: road -> steps -> plaza -> slope top. (x = distance from the road edge)."""
        n, tr, ri = self.n_st, self.tread, self.riser
        H = self.raise_h
        pts = [[-n * tr - 4000, 0], [-n * tr, 0]]
        for i in range(n):
            pts += [[-(n - i) * tr, (i + 1) * ri], [-(n - i - 1) * tr, (i + 1) * ri]]
        pts += [[self.W, H]]
        if self.sl:
            pts += [[self.W + self.sl["W"], H + self.sl["H"]], [self.W + self.sl["W"] + 2500, H + self.sl["H"]]]
        return pts

    def sheet_section(self):
        L, W = self.L, self.W
        H = self.raise_h
        views = []
        prof = self._profile()
        e = [{"type": "poly", "points": prof, "layer": "构造", "lw": 0.7}]
        e.append({"type": "line", "a": [prof[0][0], -600], "b": [prof[-1][0], -600], "layer": "现状", "ls": "dashed"})
        e.append({"type": "hatch", "pattern": "earth", "points": [[prof[0][0], -600]] + [p for p in prof] + [[prof[-1][0], -600]], "layer": "填充"})
        if self.fp:
            fp = self.fp
            yb = W - 900 - fp["W"]
            e.append({"type": "rect", "box": [yb, H, W - 900, H + fp["h"]], "layer": "构造", "lw": 0.5})
            e.append({"type": "line", "a": [yb + fp["W"] / 2 + 400, H + fp["h"]], "b": [yb + fp["W"] / 2 + 400, H + fp["h"] + 6000], "layer": "构造", "lw": 0.5, "tag": "flagpole"})
            e.append({"type": "break", "a": [yb + fp["W"] / 2 - 100, H + fp["h"] + 6000], "b": [yb + fp["W"] / 2 + 900, H + fp["h"] + 6000]})
            e.append({"type": "label", "at": [yb + fp["W"] / 2, H + fp["h"]], "text": "升旗台", "h": 3.0, "dir": "up"})
        if self.sl:
            e.append({"type": "hatch", "pattern": "grass_section", "points": [[W, H], [W + self.sl["W"], H + self.sl["H"]], [W + self.sl["W"], H]], "layer": "绿化", "tag": "lawn"})
            e.append({"type": "label", "at": [W + self.sl["W"] * 0.55, H + self.sl["H"] * 0.55], "text": f"回填土后铺草坪（坡高 {self.sl['H']}）", "h": 3.0, "dir": "up"})
        e.append({"type": "text", "at": [prof[0][0] + 1500, -350], "text": "现状水泥路", "h": 3.0})
        e += [{"type": "level", "at": [prof[0][0] + 600, 0], "value": "±0.000", "kind": "elev"},
              {"type": "level", "at": [W * 0.35, H], "value": f"+{H / 1000:.3f}", "kind": "elev"}]
        if self.sl:
            e.append({"type": "level", "at": [W + self.sl["W"] + 600, H + self.sl["H"]], "value": f"+{(H + self.sl['H']) / 1000:.3f}", "kind": "elev"})
        dims = [{"chain": [-self.n_st * self.tread, 0, W] + ([W + self.sl["W"]] if self.sl else []), "axis": "x", "base": -600, "offset": -8}]
        views.append({"title": "1-1 剖面图（沿广场中轴）", "scale": 100, "entities": e, "dims": dims})
        if self.sl:
            sl = self.sl
            e2 = [{"type": "poly", "points": [[-2000, H], [0, H], [sl["W"], H + sl["H"]], [sl["W"] + 1200, H + sl["H"]]], "layer": "构造", "lw": 0.7},
                  {"type": "line", "a": [-2000, H - 300], "b": [0, H - 300], "layer": "构造", "lw": 0.25},
                  {"type": "hatch", "pattern": "concrete", "box": [-2000, H - 300, 0, H], "layer": "填充"},
                  {"type": "poly", "points": [[0, H - 900], [sl["W"] + 1200, H - 900 + sl["H"] * 0.15]], "layer": "现状", "ls": "dashed"},
                  {"type": "hatch", "pattern": "earth", "points": [[0, H - 900], [0, H], [sl["W"], H + sl["H"] - 300], [sl["W"] + 1200, H + sl["H"] - 300],
                                                                    [sl["W"] + 1200, H - 900 + sl["H"] * 0.15]], "layer": "填充"},
                  {"type": "hatch", "pattern": "grass_section", "points": [[0, H], [sl["W"], H + sl["H"]], [sl["W"] + 1200, H + sl["H"]],
                                                                          [sl["W"] + 1200, H + sl["H"] - 300], [sl["W"], H + sl["H"] - 300], [300, H]],
                   "layer": "绿化", "tag": "lawn"},
                  {"type": "layers", "at": [sl["W"] * 0.45, H + sl["H"] * 0.45], "items": GENERIC_LAYERS["lawn"], "h": 2.5,
                   "title": "草坡做法（通用做法，待确认）", "side": "left"},
                  {"type": "label", "at": [sl["W"] * 0.6, H + 200], "text": "分层回填夯实，每层≤300", "h": 2.5, "dir": "down"},
                  {"type": "level", "at": [-1700, H], "value": f"+{H / 1000:.3f}", "kind": "elev"},
                  {"type": "level", "at": [sl["W"] + 300, H + sl["H"]], "value": f"+{(H + sl['H']) / 1000:.3f}", "kind": "elev"}]
            d2 = [{"a": [0, H - 900], "b": [sl["W"], H - 900], "offset": -8, "value": sl["W"]},
                  {"chain": [H, H + sl["H"]], "axis": "y", "base": sl["W"] + 1200, "offset": 10, "values": [sl["H"]]}]
            views.append({"title": "后斜坡回填草坪剖面大样", "scale": 50, "entities": e2, "dims": d2})
        return {"views": views,
                "notes": [f"后斜坡 {self.sl['L'] / 1000:g}m×{self.sl['W'] / 1000:g}m，坡顶高出广场 {self.sl['H']}（按此理解，待确认），"
                          f"回填量约 {self.sl.get('fill_m3')}m³（三角形断面）。" if self.sl else "",
                          "回填土不得含建筑垃圾、树根，分层回填夯实，每层≤300，草坪下 300 厚种植土。",
                          "广场基层总厚约 300，与抬高高度一致，原红土清表后夯实。"]}

    def sheet_drain(self):
        views = []
        H = self.raise_h
        for i, d in enumerate(self.dr):
            names = "—".join(d.get("between") or ["场地", "周边"])
            other = (d.get("between") or ["", ""])[0] if "广场" not in (d.get("between") or [""])[0] else (d.get("between") or ["", ""])[1]
            e = []
            # channel 300 wide inner, walls 120, depth 300 + 100 base
            wi, tw, dep = 300, 120, 350
            x0 = 0
            e.append({"type": "poly", "points": [[x0 - tw, H - 50], [x0 - tw, H - dep - 150], [x0 + wi + tw, H - dep - 150], [x0 + wi + tw, H - 50]],
                      "closed": False, "layer": "构造", "lw": 0.5, "tag": "drain_detail"})
            e.append({"type": "poly", "points": [[x0, H - 30], [x0, H - dep], [x0 + wi, H - dep], [x0 + wi, H - 30]], "layer": "构造", "lw": 0.5})
            e.append({"type": "hatch", "pattern": "concrete", "points": [[x0 - tw, H - 50], [x0 - tw, H - dep - 150], [x0 + wi + tw, H - dep - 150], [x0 + wi + tw, H - 50],
                                                                         [x0 + wi, H - 50], [x0 + wi, H - dep], [x0, H - dep], [x0, H - 50]], "layer": "填充"})
            e.append({"type": "rect", "box": [x0 - 10, H - 30, x0 + wi + 10, H], "layer": "构造", "lw": 0.35})
            for k in range(1, 6):
                e.append({"type": "line", "a": [x0 + k * wi / 6, H - 30], "b": [x0 + k * wi / 6, H], "layer": "构造", "lw": 0.18})
            # plaza side (right of the channel): build-up
            e.append({"type": "poly", "points": [[x0 + wi + tw, H], [x0 + wi + tw + 900, H]], "layer": "构造", "lw": 0.7})
            ys = [H, H - 18, H - 48, H - 148, H - 298]
            pats = ["tile_section", "mortar", "concrete", "gravel"]
            for a, b, pt in zip(ys, ys[1:], pats):
                e.append({"type": "hatch", "pattern": pt, "box": [x0 + wi + tw, b, x0 + wi + tw + 900, a], "layer": "填充"})
                e.append({"type": "line", "a": [x0 + wi + tw, b], "b": [x0 + wi + tw + 900, b], "layer": "构造", "lw": 0.18})
            e.append({"type": "hatch", "pattern": "earth", "box": [x0 - 900, H - 700, x0 + wi + tw + 900, H - dep - 150], "layer": "填充"})
            e.append({"type": "hatch", "pattern": "earth", "box": [x0 + wi + tw, H - dep - 150, x0 + wi + tw + 900, H - 298], "layer": "填充"})
            e.append({"type": "layers", "at": [x0 + wi + tw + 900, H - 9], "items": GENERIC_LAYERS["paving_tile"], "h": 2.5,
                      "title": "广场做法（通用做法，待确认）", "side": "right"})
            # other side: existing pool coping / court surface
            e.append({"type": "rect", "box": [x0 - tw - 900, H - 400, x0 - tw, H - 50], "layer": "现状", "ls": "dashed"})
            e.append({"type": "text", "at": [x0 - tw - 450, H - 225], "text": f"现状{other or '场地'}", "h": 2.5})
            e.append({"type": "label", "at": [x0 + wi / 2, H - 15], "text": "不锈钢缝隙/铸铁箅子 300宽", "h": 2.5, "dir": "up"})
            e.append({"type": "label", "at": [x0 + wi / 2, H - dep - 75], "text": "C25混凝土沟身，沟底纵坡0.5%", "h": 2.5, "dir": "down"})
            e.append({"type": "level", "at": [x0 + wi + tw + 1100, H], "value": f"+{H / 1000:.3f}", "kind": "elev"})
            dims = [{"chain": [x0 - tw, x0, x0 + wi, x0 + wi + tw], "axis": "x", "base": H - 700, "offset": -6},
                    {"chain": [H - dep - 150, H - dep, H], "axis": "y", "base": x0 - tw - 900, "offset": -6}]
            views.append({"title": f"排水节点{i + 1}（{names}连接处）", "scale": 10, "entities": e, "dims": dims})
        return {"views": views,
                "notes": ["排水沟为 300 宽成品线性排水沟（或现浇 C25 混凝土沟），箅子顶面低于广场完成面 3~5。",
                          "沟底纵坡 0.5%，出水口接现状雨水井；与泳池相接一侧须防止广场雨水流入泳池，泳池边沿现状保留并做好保护。",
                          "节点构造为通用做法，排水沟规格、出水位置待确认。"]}


# ============================================================================================ CLI

def _cmd(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="sitework.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("classify"); p.add_argument("prompt")
    p = sub.add_parser("brief"); p.add_argument("--prompt-file", required=True); p.add_argument("--images"); p.add_argument("--quotes")
    p.add_argument("--task", default=""); p.add_argument("--out", required=True)
    p = sub.add_parser("spec"); p.add_argument("brief"); p.add_argument("--out", required=True)
    p = sub.add_parser("quote"); p.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "classify":
        print(json.dumps(classify(a.prompt), ensure_ascii=False)); return 0
    if a.cmd == "quote":
        rows = read_quote_file(a.file)
        print("SITEWORK_QUOTE " + json.dumps({"rows": [[None if c is None else str(c) for c in r] for r in rows][:400],
                                               "items": parse_quote_rows(rows)}, ensure_ascii=False)); return 0
    if a.cmd == "brief":
        prompt = Path(a.prompt_file).read_text(encoding="utf-8")
        imgs = json.loads(Path(a.images).read_text(encoding="utf-8")) if a.images else []
        qs = json.loads(Path(a.quotes).read_text(encoding="utf-8")) if a.quotes else []
        b = build_brief(prompt, imgs, qs, a.task)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
        print("SITEWORK_BRIEF " + json.dumps({"type": b["type"], "unknowns": b["unknowns"], "conflicts": b["conflicts"],
                                               "sheets": plan_sheets(b)}, ensure_ascii=False)); return 0
    if a.cmd == "spec":
        b = json.loads(Path(a.brief).read_text(encoding="utf-8"))
        s = spec_from_brief(b)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        print("SITEWORK_SPEC " + json.dumps({"sheets": [{"no": x["no"], "title": x["title"]} for x in s["sheets"]]}, ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_cmd(sys.argv[1:]))
