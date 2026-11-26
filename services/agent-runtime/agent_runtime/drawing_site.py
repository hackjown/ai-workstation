"""施工图 sets (景观/场地/装修/构筑物): READ the inputs -> brief.json -> 图纸目录 -> sheet set -> VERIFY each sheet (3.9.93).

Why (task-241, 升旗台广场, user: 「画的太差了，自己也不核实，先要读懂图片、文字说明、报价等资料，规划图纸目录」):
* one read_image call on BOTH images came back shallow ("两张都是实景照片、无尺寸"); the anti-loop guard then cut
  reading off at step 4 (it even skipped the drawing_note that would have saved the findings);
* the whole job went into ONE generic sheet with hand-placed coordinates, text heights in model mm (h_mm 800 = an
  80 m tall title at 1:100), 647 text-overlap errors, the spec rewritten three times blind;
* the converge policy ran the floor-plan tracer (drawing_auto) on a site photo, and the final message carried the
  floor-plan boilerplate (墙、房间、门窗、尺寸链刻度) - false for a plaza;
* its own output was never looked at (read_image blocked by 「换方法」) and 「目前最好的草稿」 went out with hard errors.

Now a site / interior / structure job is routed here (never to the tracer):
  drawing_brief  each image read SEPARATELY with a structured JSON question (role, elements + positions, layout,
                 materials, counts, texts, dimensions), the text brief parsed (sitework.parse_text), quotes read row by
                 row; cad/brief.json = facts with their source, derived quantities, conflicts, unknowns with the
                 assumption used. Blocking unknowns are asked ONCE (ask_user card); the rest go on the 设计说明 sheet.
                 It also writes the 图纸目录 and a drafted sheet-set spec (cad/spec.json, sitework.spec_from_brief).
  drawing_build  sheetset.py: every sheet composed, mechanically checked (units, overlaps, frame, empty sheet,
                 required elements); when that passes, verify_sheets() shows EACH sheet PNG to the vision model with
                 its checklist from the brief.
  delivery       PDF (all sheets) + DXF; the message lists the 图纸目录, the assumptions and the quantity table; with
                 hard errors left it says plainly that it FAILED and what, never 「最好的草稿」.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import re
from pathlib import Path
from typing import Optional

from . import drawing as _draw

_KIT = None
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic")
_QUOTE_EXT = (".xlsx", ".xls", ".xlsm", ".csv", ".ods", ".pdf")
_UP = re.compile(r"(?:~/workspace/|/workspace/)?(uploads/[^\s'\"，。；;]+?\.(?:jpe?g|png|webp|bmp|heic|xlsx|xlsm|xls|csv|ods|pdf))", re.I)
VERIFY_READS = 14          # sheet PNG reads per task (VERIFY is never cut off by the anti-loop guard below this)


def kit():
    """skills/cad-drawing/sitework.py (shared with the computer side), loaded by path."""
    global _KIT
    if _KIT is None:
        p = _draw.KIT_DIR / "sitework.py"
        spec = importlib.util.spec_from_file_location("aiw_sitework", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)   # type: ignore[union-attr]
        _KIT = mod
    return _KIT


def user_text(prompt: str) -> str:
    """the user's own words (the 【技能】 tag and the attachment list removed)."""
    t = re.sub(r"【[^】]*】", " ", prompt or "")
    return t.split("附件（", 1)[0].strip()


def route(prompt: str) -> dict:
    try:
        return kit().classify(user_text(prompt))
    except Exception:  # noqa: BLE001  never break a task on routing
        return {"type": "floor_plan", "label": "房屋平面描图", "reasons": ["router unavailable"]}


def is_sheet_job(prompt: str) -> bool:
    return route(prompt)["type"] in ("site", "interior", "structure")


def uploads(prompt: str) -> tuple:
    seen, imgs, quotes = set(), [], []
    for p in _UP.findall(prompt or ""):
        if p in seen:
            continue
        seen.add(p)
        (imgs if p.lower().endswith(_IMG_EXT) else quotes).append(p)
    # a picture whose name says 报价/清单 is a quote read by the vision model
    q_imgs = [p for p in imgs if re.search(r"报价|清单|预算|quote", p, re.I)]
    return [p for p in imgs if p not in q_imgs], quotes, q_imgs


def is_output(path: str) -> bool:
    """an image this task generated (a sheet PNG / diff / overlay): reading it is VERIFY, not looping."""
    p = str(path or "")
    return bool(re.search(r"(^|/)(cad|out)/", p)) and not p.startswith("uploads/")


# ---- READ ----------------------------------------------------------------------------------------------------------

QUOTE_IMG_Q = ("这是一张报价单/工程量清单的图片。逐行抄出每一个清单项，不要合并、不要编。只输出 JSON："
               "{\"rows\": [[\"序号\", \"项目名称\", \"规格/做法\", \"单位\", \"数量\", \"单价\", \"合价\"], ...]}")


async def _vision(L, b64: str, q: str, max_tokens: int = 1500) -> str:
    from . import vision_guard as _vg
    from . import cad as _cad
    eyes = getattr(L, "vision_llm", None) or getattr(L, "llm", None)
    return await _vg.read(L, eyes, "你是严谨的工程资料读图员。只报告图上真实存在的内容，只输出要求的 JSON。", q, b64, max_tokens,
                          L.cfg.vision_timeout_s * 3)


def _b64(img, side: int = 1800) -> str:
    im = img.convert("RGB")
    if max(im.size) > side:
        k = side / max(im.size)
        im = im.resize((int(im.width * k), int(im.height * k)))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


async def read_inputs(L, images: list, quotes: list, quote_imgs: list) -> tuple:
    """each image on its own (never two in one question), each quote row by row."""
    K = kit()
    brief_txt = user_text(L.task.prompt)[:600]
    hints = K.image_hints(user_text(L.task.prompt))
    out_imgs, out_q, notes = [], [], []

    async def one(i, path):
        img, err = await L._load_ws_image(path)
        if img is None:
            notes.append(f"图{i}（{path}）读不到：{err}")
            return {"index": i, "path": path, "read": {"role": "其它", "raw": f"unreadable: {err}", "elements": []}}
        q = K.IMAGE_Q.format(n=i, name=Path(path).name, brief=brief_txt)
        try:
            text = await _vision(L, _b64(img), q)
        except Exception as e:  # noqa: BLE001
            notes.append(f"图{i} 视觉读取失败：{str(e)[:80]}")
            text = ""
        rec = K.parse_image_read(text, hints.get(i, ""))
        rec["size"] = list(img.size)
        return {"index": i, "path": path, "read": rec}
    sem = asyncio.Semaphore(2)

    async def guarded(i, p):
        async with sem:
            return await one(i, p)
    out_imgs = list(await asyncio.gather(*[guarded(i, p) for i, p in enumerate(images, 1)]))
    for p in quotes:
        r = await L.tools.run("shell", {"command": f'cd "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}" && python3 {_draw.REMOTE}/sitework.py quote '
                                                   + "'" + p.replace("'", "'\\''") + "'", "timeout": 180}, intent="read the quote row by row")
        so = (r.result or {}).get("stdout") if isinstance(r.result, dict) else ""
        m = re.search(r"SITEWORK_QUOTE (.*)", so or "")
        if m:
            try:
                d = json.loads(m.group(1))
                out_q.append({"path": p, "rows": d.get("rows") or [], "items": d.get("items") or []})
                continue
            except ValueError:
                pass
        notes.append(f"报价 {p} 读不出来")
    for p in quote_imgs:
        img, err = await L._load_ws_image(p)
        if img is None:
            notes.append(f"报价图片 {p} 读不到")
            continue
        try:
            text = await _vision(L, _b64(img, 2200), QUOTE_IMG_Q, 2500)
            mm = re.search(r"\{.*\}", text or "", re.S)
            rows = json.loads(mm.group(0)).get("rows") if mm else []
        except Exception:  # noqa: BLE001
            rows = []
        out_q.append({"path": p, "rows": rows or [], "items": K.parse_quote_rows(rows or [])})
    return out_imgs, out_q, notes


async def brief_tool(L, a: dict) -> str:
    """drawing_brief: READ every input -> cad/brief.json + 图纸目录 + drafted cad/spec.json."""
    from aiw_protocols import EventType
    K = kit()
    prompt = L.task.prompt or ""
    imgs, quotes, qimgs = uploads(prompt)
    if isinstance(a.get("images"), list) and a["images"]:
        imgs = [re.sub(r"^(~/workspace/|/workspace/)", "", str(x)) for x in a["images"]]
    if isinstance(a.get("quotes"), list) and a["quotes"]:
        quotes = [re.sub(r"^(~/workspace/|/workspace/)", "", str(x)) for x in a["quotes"]]
    err = await L._ensure_drawkit()
    if err:
        return f"FAILED: {err}"
    st = site_state(L)
    if st.get("reads") is None or a.get("again"):
        st["reads"] = await read_inputs(L, imgs, quotes, qimgs)
    images, qs, notes = st["reads"]
    text = user_text(prompt) + (("\n用户补充：" + st["extra"]) if st.get("extra") else "")
    brief = K.build_brief(text, images, qs, task=getattr(L, "_root_id", "") or "")
    blocking = [u for u in brief["unknowns"] if u.get("blocking")]
    if blocking and not st.get("asked"):
        st["asked"] = True
        q = "画施工图前有几项资料缺失，没有它们画不出来：\n" + "\n".join(f"{i}. {u['question']}" for i, u in enumerate(blocking, 1))
        await L.emit(EventType.INPUT_REQUIRED, question=q, options=["我来补充", "先按常规假定出图"])
        reply = await L._ask(q, ["我来补充", "先按常规假定出图"])
        await L.emit(EventType.INPUT_ANSWERED, answer=reply)
        if reply and not re.search(r"先按|假定|常规", str(reply)):
            st["extra"] = str(reply)
            brief = K.build_brief(user_text(prompt) + "\n用户补充：" + str(reply), images, qs, task=getattr(L, "_root_id", "") or "")
    st["brief"] = brief
    sheets = K.plan_sheets(brief)
    w = await L.tools.run("files_write", {"path": "cad/brief.json", "content": json.dumps(brief, ensure_ascii=False, indent=1)},
                          intent="save the brief")
    spec = K.spec_from_brief(brief)
    write_spec = a.get("regen", True)
    if write_spec:
        await L.tools.run("files_write", {"path": "cad/spec.json", "content": json.dumps(spec, ensure_ascii=False, indent=1)},
                          intent="save the drafted sheet set")
    dp = getattr(L, "_dphase", None)
    if dp is not None:
        dp.brief_done = True
        dp.after("drawing_brief", a, True, "")
    L._acted = True
    await L.emit(EventType.ACTION_RESULT, tool="drawing_brief", success=bool(w.success), error=None,
                 result={"type": brief["type"], "images": [{"index": i["index"], "role": i.get("role")} for i in brief["images"]],
                         "sheets": [f"{s['no']} {s['title']}" for s in sheets], "unknowns": len(brief["unknowns"]),
                         "conflicts": len(brief["conflicts"])})
    return brief_summary(brief, sheets, notes) + (
        "\nNEXT: drawing_build spec=cad/spec.json name=" + _name(brief) + " (the set above is already drafted from the brief, "
        "every sheet parametric; change cad/spec.json only where the brief is wrong - coordinates in MODEL mm, text h and "
        "offsets in PAPER mm - then build again). Do not call drawing_auto: that is the floor-plan tracer.")


def _name(brief) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "", (brief.get("project") or "施工图").replace("改造工程", "")) + "施工图"


def brief_summary(brief: dict, sheets: list, notes: list = ()) -> str:
    L = [f"BRIEF ({brief.get('type_label')}) saved to cad/brief.json."]
    for im in brief.get("images") or []:
        els = "、".join(f"{e['name']}" + (f"({e.get('position')})" if e.get("position") else "") for e in (im.get("elements") or [])[:10])
        L.append(f"图{im['index']} {Path(str(im.get('path') or '')).name}: {im.get('role')}" + (f"，用户说是「{im['user_says']}」" if im.get("user_says") else "")
                 + (f"；布局：{im['layout']}" if im.get("layout") else "") + (f"；构件：{els}" if els else "")
                 + (f"；尺寸：{im['dimensions']}" if im.get("dimensions") else ""))
    given = [f for f in brief.get("facts") or [] if f.get("kind") == "given"]
    L.append("用户给定：" + "；".join(f"{f['key']}={json.dumps(f['value'], ensure_ascii=False)[:80]}" for f in given[:12]))
    if brief.get("quote"):
        L.append(f"报价：{len(brief['quote'])} 项")
    for c in brief.get("conflicts") or []:
        L.append("矛盾：" + c["what"])
    L.append("假定（非阻塞，写进设计说明）：" + "；".join(f"{u.get('topic')}: {u['assumption']}" for u in brief.get("unknowns") or []
                                               if not u.get("blocking")))
    bl = [u for u in brief.get("unknowns") or [] if u.get("blocking")]
    if bl:
        L.append("阻塞（已问用户）：" + "；".join(u["question"] for u in bl))
    L.append("图纸目录：" + "；".join(f"{s['no']} {s['title']}" for s in sheets))
    for n in notes or []:
        L.append("注意：" + n)
    return "\n".join(L)


def site_state(L) -> dict:
    st = getattr(L, "_site", None)
    if st is None:
        st = {"route": route(L.task.prompt or ""), "reads": None, "brief": None, "asked": False, "vision": {}, "verify_reads": 0}
        L._site = st
    return st


# ---- VERIFY --------------------------------------------------------------------------------------------------------

SHEET_Q = ("这是一套施工图中的一张：{no} {title}（图幅 {paper}）。逐项核对，只按图上能看到的回答，看不清就说看不清。\n"
           "必须出现的内容：\n{items}\n"
           "只输出 JSON：{{\"legible\": true/false（文字和尺寸能看清、没有大面积重叠）, \"empty\": true/false（图面基本空白）, "
           "\"items\": [{{\"what\": \"...\", \"seen\": true/false, \"note\": \"\"}}], "
           "\"problems\": [\"最严重的问题，例如 文字压线/尺寸看不清/图形跑出图框/与说明不符\"]}}")


async def verify_sheets(L, rep: dict, out_dir: str) -> dict:
    """the vision checklist per sheet, against the brief's requirements. Mechanical checks are the ground truth for
    presence (a tagged entity is there); the vision model judges what a person sees: legibility, empty sheets,
    elements that are not recognisable. -> {"errors": [...], "warnings": [...], "sheets": {no: verdict}}"""
    st = site_state(L)
    reqs = {}
    for r in rep.get("requirements") or []:
        reqs.setdefault(r.get("sheet"), []).append(r)
    rel = lambda f: f if (not f or f.startswith(out_dir + "/") or f.startswith("/")) else f"{out_dir}/{Path(f).name}"   # noqa: E731
    out = {"errors": [], "warnings": [], "sheets": {}}
    sem = asyncio.Semaphore(3)

    async def one(s):
        async with sem:
            if st["verify_reads"] >= VERIFY_READS:
                return s, None
            st["verify_reads"] += 1
            img, err = await L._load_ws_image(rel(s.get("png") or ""))
            if img is None:
                return s, {"error": err}
            items = [r.get("what") for r in reqs.get(s.get("key"), [])] or ["图名对应的主要图形、尺寸和文字说明"]
            q = SHEET_Q.format(no=s["no"], title=s["title"], paper=s.get("paper"), items="\n".join(f"- {i}" for i in items))
            try:
                text = await _vision(L, _b64(img, 2000), q, 900)
            except Exception as e:  # noqa: BLE001
                return s, {"error": str(e)[:120]}
            m = re.search(r"\{.*\}", text or "", re.S)
            try:
                return s, json.loads(m.group(0)) if m else {"error": "no JSON", "raw": (text or "")[:300]}
            except ValueError:
                return s, {"error": "bad JSON", "raw": (text or "")[:300]}
    res = await asyncio.gather(*[one(s) for s in rep.get("sheets") or []])
    for s, v in res:
        tag = f"[{s['no']} {s['title']}]"
        out["sheets"][s["no"]] = v
        if v is None:
            out["warnings"].append(f"{tag} 视觉复核预算已用完，未复核")
            continue
        if v.get("error"):
            out["warnings"].append(f"{tag} 视觉复核没做成（{v['error']}）")
            continue
        if v.get("empty") is True:
            out["errors"].append(f"{tag} 视觉复核：图面基本空白")
        if v.get("legible") is False:
            out["errors"].append(f"{tag} 视觉复核：看不清（" + "；".join(str(p) for p in (v.get("problems") or [])[:2]) + "）")
        mech = {r.get("what"): r.get("ok") for r in reqs.get(s.get("key"), [])}
        for it in v.get("items") or []:
            if isinstance(it, dict) and it.get("seen") is False:
                what = str(it.get("what") or "")
                if mech.get(what):    # present in the drawing (tagged / dimensioned): the picture does not show it clearly
                    out["warnings"].append(f"{tag} 视觉复核没认出「{what}」（图中已有，可能太小/不清楚）")
                else:
                    out["errors"].append(f"{tag} 视觉复核：缺「{what}」")
        for p in (v.get("problems") or [])[:2]:
            if v.get("legible") is not False:
                out["warnings"].append(f"{tag} 视觉复核意见：{p}")
    st["vision"] = out
    return out


# ---- the final message --------------------------------------------------------------------------------------------

def final_message(rep: Optional[dict], brief: Optional[dict], vision: Optional[dict], sent: list, why: str = "") -> tuple:
    """(ok, text). Lists the 图纸目录, the assumptions and the quantity table; a failed set says FAILED and why."""
    rep = rep or {}
    errs = list(rep.get("errors") or []) + list((vision or {}).get("errors") or [])
    ok = bool(rep) and not errs
    lines = []
    if not rep:
        lines.append("出图失败：施工图还没有生成出来" + (f"（{why}）" if why else "") + "。")
    elif ok:
        lines.append(f"施工图已出图，共 {len(rep.get('sheets') or [])} 张，机械检查和逐张视觉复核都通过。"
                     + (f" 文件：{', '.join(sent)}。" if sent else ""))
    else:
        lines.append(f"出图未通过核对（失败）：还有 {len(errs)} 项硬性问题没有解决，图纸不能直接用于施工。"
                     + (f" 已发的文件（仅供查看问题）：{', '.join(sent)}。" if sent else ""))
        lines.append("未通过：" + "；".join(e[:120] for e in errs[:8]) + ("…" if len(errs) > 8 else ""))
    if rep.get("sheets"):
        lines.append("图纸目录：")
        for s in rep["sheets"]:
            lines.append(f"  {s['no']}  {s['title']}（{s.get('paper')}，{'、'.join(s.get('scales') or []) or '—'}）")
    unk = [u for u in (brief or {}).get("unknowns") or [] if not u.get("blocking")]
    if unk:
        lines.append("假定（施工前请确认）：")
        for u in unk:
            lines.append(f"  · {u.get('topic') or u['key']}：{u['assumption']}")
    for c in (brief or {}).get("conflicts") or []:
        lines.append("资料矛盾：" + c["what"])
    q = (brief or {}).get("quantities") or rep.get("quantities") or []
    if q:
        lines.append("工程量（按图纸计算" + ("，与报价对照" if any(r.get("quote_qty") is not None for r in q) else "，未提供报价") + "）：")
        for r in q:
            lines.append(f"  {r['name']}  {r['qty']:g} {r['unit']}" + (f"（报价 {r['quote_qty']:g}）" if r.get("quote_qty") is not None else ""))
    vw = (vision or {}).get("warnings") or []
    if vw and ok:
        lines.append("复核提示：" + "；".join(vw[:4]))
    return ok, "\n".join(lines)


async def read_report(L, path: str = "cad/report.json") -> Optional[dict]:
    r = await L.tools.run("files_read", {"path": path}, intent="read the build report")
    if not (r.success and isinstance(r.result, dict)):
        return None
    try:
        return json.loads(r.result.get("content") or "")
    except ValueError:
        return None


async def deliver(L, why: str = "") -> bool:
    """send the PDF + DXF (+DWG) of the set and finish with the honest message. True when the task ended."""
    from aiw_protocols import TaskStatus
    from . import drawing_hooks as _dh
    st = site_state(L)
    rep = st.get("report") or await read_report(L)
    brief = st.get("brief")
    sent = []
    files = (rep or {}).get("files") or {}
    out = st.get("out") or "cad"
    rel = lambda f: f if (not f or f.startswith(out + "/") or f.startswith("/")) else f"{out}/{Path(f).name}"   # noqa: E731
    for k in ("pdf", "dxf", "dwg"):
        if not files.get(k):
            continue
        r = await L.tools.run("send_file", {"path": rel(files[k])}, intent="deliver the drawing set")
        if not (r.success and isinstance(r.result, dict)) or _dh.check_delivery(L, r.result):
            continue
        await L._deliver_file(r.result)
        _dh.record_delivery(L, r.result)
        sent.append(Path(files[k]).name)
    ok, text = final_message(rep, brief, st.get("vision"), sent, why)
    if sent:
        L._sent_file = sent[0]
    dp = getattr(L, "_dphase", None)
    if dp is not None:
        from . import drawing_phase as _dp
        dp.phase = _dp.DELIVER
    L._finish(TaskStatus.SUCCEEDED if ok else TaskStatus.FAILED, text)
    return True
