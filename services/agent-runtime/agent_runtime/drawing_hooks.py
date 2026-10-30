"""Loop hooks for the fresh-job rule (taskdir) and the drawing phases (drawing_phase), kept out of loop.py so the
loop only calls them at a few points. Every function takes the AgentLoop as `L`."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Optional

from . import drawing_phase as _dp
from . import taskdir as _td

_UPLOAD_RE = re.compile(r"(?:~/workspace/|/workspace/)?(uploads/[^\s'\"，。；;]+?\.(?:jpe?g|png|webp|bmp|gif))", re.I)


async def _emit(L, tool: str, ok: bool, result: dict, error: Optional[str] = None) -> None:
    from aiw_protocols import EventType
    await L.emit(EventType.ACTION_RESULT, tool=tool, success=ok, error=error, result=result)


def init(L) -> None:
    L._root_id = _td.root_id(L.task)
    L._dphase = _dp.DrawPhase() if getattr(L, "_cad_task", False) else None
    L._delivered = _td.index_for(getattr(L.cfg, "artifacts_dir", None))
    L._draw_escalations = 0


def current_upload(L) -> Optional[str]:
    """the newest image upload named in this run's request (the last one in the prompt)."""
    hits = _UPLOAD_RE.findall(L.task.prompt or "")
    return hits[-1] if hits else None


async def start(L) -> None:
    """per-task work folder; for drawings cad/ -> tasks/<root>/cad. Runs before the first step."""
    cad = bool(getattr(L, "_cad_task", False))
    if not (cad or getattr(L, "_image_task", False) or getattr(L, "_video_task", False)):
        return
    try:
        r = await L.tools.run("shell", {"command": _td.setup_command(L.task, cad), "timeout": 30},
                              intent="start this task in its own work folder")
        out = ((r.result or {}).get("stdout") or "").strip() if isinstance(r.result, dict) else ""
    except Exception:  # noqa: BLE001
        out = ""
    d = _td.rel_dir(L.task)
    L._workdir_set = True
    cont = L._root_id != L.task.id
    L.history.append(f"0. work folder {d}/ ({'continuing task ' + L._root_id + ': its files are kept' if cont else 'NEW task: it starts empty'})"
                     + (f"; cad/ is {d}/cad" if cad else "") + (f" [{out.splitlines()[0]}]" if "moved" in out or "adopted" in out else ""))


def task_block(L) -> str:
    parts = [_td.rule(L.task)] if getattr(L, "_workdir_set", False) else []
    if getattr(L, "_dphase", None):
        parts.append(L._dphase.block())
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def hidden(L) -> tuple:
    return L._dphase.hidden() if getattr(L, "_dphase", None) else ()


async def pre_tool(L, decision):
    """-> (None, None) to run the tool | ("skip", note) | ("escalate", detail)."""
    dp = getattr(L, "_dphase", None)
    if not dp:
        return None, None
    st = dp.intent(decision.thought or "", decision.tool, decision.args or {})
    if st == "escalate":
        L._draw_escalations += 1
        return "escalate", ("连续多轮都在重复分析同一件事（看图、裁图、问标签位置），没有写 spec 也没有生成图。"
                            "已提醒两次仍在循环。")
    if st == "nudge":
        L.history.append(f"{L.step}. {decision.tool} SKIPPED: the last {_dp.INTENT_WINDOW} steps repeat the same analysis "
                         "with nothing new written")
        return "skip", (_dp.STOP_NOTE + " Your last steps repeat the same analysis and nothing new was written. The "
                        "findings are in FACTS. Call drawing_auto (draft from the image) or files_write cad/spec.json now, "
                        "then drawing_build.")
    refusal = dp.gate(decision.tool, decision.args or {})
    if refusal:
        L.history.append(f"{L.step}. {decision.tool} REFUSED: read budget used up, write the spec")
        return "skip", refusal
    return None, None


def post_tool(L, decision, ok: bool, text: str = "") -> None:
    dp = getattr(L, "_dphase", None)
    if dp:
        dp.after(decision.tool, decision.args or {}, ok, text or "")


async def save_facts(L) -> None:
    dp = getattr(L, "_dphase", None)
    if dp and dp.dirty:
        try:
            await L.tools.run("files_write", {"path": _dp.FACTS, "content": dp.facts_json()}, intent="save the drawing facts")
            dp.dirty = False
        except Exception:  # noqa: BLE001
            pass


# ---- delivery ---------------------------------------------------------------------------------------------------

def check_delivery(L, result: dict) -> Optional[str]:
    """a file byte-identical to one ANOTHER task delivered is refused (the user's rule: a new task is a new job)."""
    b64 = (result or {}).get("content_b64")
    if not b64:
        return None
    try:
        sha = _td.sha256_b64(b64)
    except Exception:  # noqa: BLE001
        return None
    result["_sha256"] = sha
    o = L._delivered.conflict(sha, L._root_id)
    if not o:
        return None
    return (f"REFUSED: {Path(result.get('path') or 'file').name} is byte-identical to {o.get('name')} that task "
            f"{o.get('task')} delivered earlier. A new task is a fresh job: never resend or copy an earlier task's output, "
            "even when the upload has the same md5. Make the result anew in this task's folder, then send that.")


def record_delivery(L, result: dict) -> None:
    sha = (result or {}).get("_sha256")
    if not sha and (result or {}).get("content_b64"):
        try:
            sha = _td.sha256_b64(result["content_b64"])
        except Exception:  # noqa: BLE001
            sha = None
    if sha:
        L._delivered.record(sha, L._root_id, Path(result.get("path") or "file").name)
    result.pop("_sha256", None)


# ---- revision: stale spec -----------------------------------------------------------------------------------------

async def stale_spec(L, spec: str) -> Optional[str]:
    """None when the spec belongs to this task/image; else it is archived and the reason returned."""
    try:
        r = await L.tools.run("shell", {"command": _dp.stale_probe(spec, current_upload(L)), "timeout": 30},
                              intent="check the spec belongs to this image and task")
        info = json.loads(((r.result or {}).get("stdout") or "").strip().splitlines()[-1]) if isinstance(r.result, dict) else {}
    except Exception:  # noqa: BLE001
        return None
    why = _dp.stale_verdict(info, L._root_id)
    if not why:
        return None
    try:
        await L.tools.run("shell", {"command": _dp.archive_command(spec), "timeout": 30}, intent="archive the stale spec")
    except Exception:  # noqa: BLE001
        pass
    return why


# ---- tools: drawing_note, drawing_auto ------------------------------------------------------------------------------

async def tool(L, decision) -> Optional[str]:
    t, a = decision.tool, decision.args or {}
    if t == "drawing_note":
        dp = L._dphase or _dp.DrawPhase()
        L._dphase = dp
        items = a.get("items") if isinstance(a.get("items"), list) else [a]
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append(dp.note(str(it.get("kind") or "notes"), str(it.get("key") or ""), it.get("value"),
                               bool(it.get("confirmed"))))
        await save_facts(L)
        await _emit(L, "drawing_note", True, {"n": len(out)})
        return "; ".join(out) or "nothing noted (give kind, key, value)"
    if t == "drawing_auto":
        return await _auto(L, a)
    return None


def _resolve_sides(chains: dict) -> list:
    """facts chains (keyed by side, or by box when the side was not given) -> spec chain list with sides."""
    named, loose = [], []
    for k, v in (chains or {}).items():
        if not isinstance(v, dict) or not v.get("values"):
            continue
        if k in ("top", "bottom", "left", "right"):
            named.append(dict(v, side=k))
        else:
            loose.append(dict(v))
    have = {c["side"] for c in named}
    hz = [c for c in loose if c.get("box_px") and (c["box_px"][2] - c["box_px"][0]) >= (c["box_px"][3] - c["box_px"][1])]
    vt = [c for c in loose if c not in hz and c.get("box_px")]
    hz.sort(key=lambda c: c["box_px"][1] + c["box_px"][3]); vt.sort(key=lambda c: c["box_px"][0] + c["box_px"][2])
    for lst, sides in ((hz, ("top", "bottom")), (vt, ("left", "right"))):
        if lst:
            for side, c in ((sides[0], lst[0]), (sides[1], lst[-1]) if len(lst) > 1 else (None, None)):
                if side and c is not None and side not in have:
                    named.append(dict(c, side=side)); have.add(side)
    return [{k: c[k] for k in ("side", "values", "box_px", "start") if k in c} for c in named]


def grid_crop(img, box, target=1600):
    """crop + enlarge with a pixel grid labelled in SOURCE coordinates (for the one label read)."""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    c = img.crop((x0, y0, x1, y1))
    k = max(1, min(8, int(target / max(1, max(c.size)))))
    c = c.resize((c.width * k, c.height * k), Image.LANCZOS)
    d = ImageDraw.Draw(c)
    step = 10 if (x1 - x0) <= 150 else 25 if (x1 - x0) <= 400 else 50
    for gx in range((x0 // step + 1) * step, x1, step):
        d.line([((gx - x0) * k, 0), ((gx - x0) * k, c.height)], fill=(255, 0, 0), width=1)
        d.text(((gx - x0) * k + 2, 2), str(gx), fill=(255, 0, 0))
    for gy in range((y0 // step + 1) * step, y1, step):
        d.line([(0, (gy - y0) * k), (c.width, (gy - y0) * k)], fill=(255, 0, 0), width=1)
        d.text((2, (gy - y0) * k + 2), str(gy), fill=(255, 0, 0))
    return c


async def _auto(L, a: dict) -> str:
    dp = L._dphase or _dp.DrawPhase()
    L._dphase = dp
    image = str(a.get("image") or a.get("path") or dp.facts.get("source", {}).get("image") or current_upload(L) or "").strip()
    image = re.sub(r"^(~/workspace/|/workspace/)", "", image)
    if not image:
        await _emit(L, "drawing_auto", False, {}, "no image")
        return "FAILED: drawing_auto needs `image` (the source photo, e.g. uploads/photo.jpg)."
    chains = _resolve_sides(dp.facts.get("chains") or {})
    if isinstance(a.get("chains"), list):
        chains = [c for c in a["chains"] if isinstance(c, dict)] or chains
    if not any(c.get("side") in ("top", "bottom") for c in chains) or not any(c.get("side") in ("left", "right") for c in chains):
        await _emit(L, "drawing_auto", False, {}, "no chains")
        return ("FAILED: drawing_auto needs at least one horizontal and one vertical dimension chain in FACTS (read them "
                "with drawing_read, giving side=top/bottom/left/right, or pass `chains`).")
    err = await L._ensure_drawkit()
    if err:
        return f"FAILED: {err}"
    await L.tools.run("files_write", {"path": "cad/auto_chains.json", "content": json.dumps({"chains": chains}, ensure_ascii=False)},
                      intent="save the chains for the draft")
    r = await L.tools.run("shell", {"command": 'cd "${VM_AGENT_FILES_ROOT:-$HOME/workspace}" && [ -e cad/spec.json ] && echo exists; true',
                                    "timeout": 20}, intent="is there a spec already")
    exists = "exists" in (((r.result or {}).get("stdout") or "") if isinstance(r.result, dict) else "")
    out = "cad/spec.draft.json" if exists else "cad/spec.json"
    labels = None
    if isinstance(a.get("labels"), list) and a["labels"]:
        await L.tools.run("files_write", {"path": "cad/auto_labels.json", "content": json.dumps({"labels": a["labels"]}, ensure_ascii=False)},
                          intent="save the room labels")
        labels = "cad/auto_labels.json"
    cmd = _dp.auto_command(image, out, "cad/auto_chains.json", labels, L._root_id, "cad/auto_debug.png")
    r = await L.tools.run("shell", {"command": cmd, "timeout": 900}, intent="draft the spec from the image")
    res = r.result if isinstance(r.result, dict) else {}
    rep = _dp.parse_auto(res.get("stdout") or "")
    if rep and rep.get("ok") and len(rep.get("labels") or []) < 2 and labels is None:
        # no printed labels the OCR could read: ask the vision model ONCE, on a gridded crop, for JSON positions
        labs = await _vlm_labels(L, image, rep)
        if labs:
            await L.tools.run("files_write", {"path": "cad/auto_labels.json", "content": json.dumps({"labels": labs}, ensure_ascii=False)},
                              intent="save the room labels")
            cmd = _dp.auto_command(image, out, "cad/auto_chains.json", "cad/auto_labels.json", L._root_id, "cad/auto_debug.png")
            r = await L.tools.run("shell", {"command": cmd, "timeout": 900}, intent="draft the spec with the labels")
            res = r.result if isinstance(r.result, dict) else {}
            rep = _dp.parse_auto(res.get("stdout") or "") or rep
    if not rep or not rep.get("ok"):
        tail = (rep or {}).get("error") or ((res.get("stderr") or "") + (res.get("stdout") or ""))[-800:]
        await _emit(L, "drawing_auto", False, {}, str(tail)[:200])
        return f"FAILED: drawkit auto: {tail}"
    for lab in rep.get("labels") or []:
        dp.note("labels", lab.get("name"), {"name": lab.get("name"), "at_px": [lab.get("x"), lab.get("y")], "by": lab.get("by")})
    dp.note("source", "image", image)
    dp.note("source", "md5", rep.get("md5"))
    dp.after("drawing_auto", a, True, "")
    await save_facts(L)
    L._acted = True
    await _emit(L, "drawing_auto", True, {"rooms": len(rep.get("rooms") or []), "out": out})
    rooms = "; ".join(f"{x['name']} {x['box']}" for x in rep.get("rooms") or [])
    return (f"DRAFT written to {out} ({len(rep.get('rooms') or [])} rooms, debug image cad/auto_debug.png): {rooms}. "
            f"Axis lines x {rep.get('x_lines')} y {rep.get('y_lines')} (not on a chain mark: x {rep.get('x_unsnapped')}, "
            f"y {rep.get('y_unsnapped')}). Uncertain: {rep.get('uncertain') or 'none'}.\nNEXT: CORRECT the draft, do not "
            f"re-derive it: files_read {out}, fix only what is wrong (a missing or misplaced wall, a room named 房间?), add "
            "doors and windows from the photo, set title/subtitle, files_write it" +
            (" to cad/spec.json" if out != "cad/spec.json" else "") + ", then drawing_build.")


async def _vlm_labels(L, image: str, rep: dict) -> list:
    eyes = getattr(L, "vision_llm", None) or getattr(L, "llm", None)
    if eyes is None or not hasattr(eyes, "describe"):
        return []
    img, err = await L._load_ws_image(image)
    if img is None:
        return []
    box = rep.get("plan_px") or [0, 0, img.width, img.height]
    try:
        crop = grid_crop(img, box)
        buf = io.BytesIO(); crop.convert("RGB").save(buf, "JPEG", quality=90)
        text = await eyes.describe("你是严谨的读图员，只报告图上真实存在的文字，只输出 JSON。", _dp.LABEL_Q,
                                   base64.b64encode(buf.getvalue()).decode(), 900)
    except Exception:  # noqa: BLE001
        return []
    labs = _dp.labels_from(text)
    x0, y0, x1, y1 = box
    return [lb for lb in labs if x0 - 5 <= lb["x"] <= x1 + 5 and y0 - 5 <= lb["y"] <= y1 + 5]
