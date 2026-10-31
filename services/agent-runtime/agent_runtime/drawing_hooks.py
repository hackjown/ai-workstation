"""Loop hooks for the fresh-job rule (taskdir) and the drawing phases (drawing_phase), kept out of loop.py so the
loop only calls them at a few points. Every function takes the AgentLoop as `L`."""
from __future__ import annotations

import asyncio
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
    """-> (None, None) to run the tool | ("skip", note) | ("escalate", (kind, detail)) | ("converge", why).

    3.8.9 (task-3fc): the old guard said 「没有动手写 spec」 after the spec had been written AND built, and paused
    again right after each 继续. Now: in BUILD / VERIFY reads aimed at the build errors have a budget; when the
    model loops after a spec exists, the deterministic pipeline runs (and then the best draft is delivered)
    instead of a pause; only a task with no spec at all and no way to make one asks the user."""
    dp = getattr(L, "_dphase", None)
    if not dp:
        return None, None
    if dp.must_deliver and decision.tool not in ("send_file", "done", "drawing_build"):
        return "converge", f"{_dp.VERIFY_ROUNDS} 轮核对已用完"
    st = dp.intent(decision.thought or "", decision.tool, decision.args or {})
    if st == "escalate" or (st == "nudge" and dp.nudges >= 2 and (dp.spec_written or dp.resumes)):
        if dp.spec_written or dp.builds or dp.auto_runs == 0:
            return "converge", dp.stuck()[1]
        L._draw_escalations += 1             # no spec and the pipeline could not make one: ask the user
        return "escalate", dp.stuck()
    if st == "nudge":
        L.history.append(f"{L.step}. {decision.tool} SKIPPED: the last {_dp.INTENT_WINDOW} steps repeat the same analysis "
                         "with nothing new written")
        if dp.spec_written and dp.last_ok is False:
            return "skip", ("Your last steps repeat the same analysis. The spec exists and the build reported: "
                            + " | ".join(dp.last_errors[:6]) + ". Fix exactly those in cad/spec.json and drawing_build "
                            "again (it re-locates chain boxes and regenerates bad rooms by itself), or call drawing_auto.")
        return "skip", (_dp.STOP_NOTE + " Your last steps repeat the same analysis and nothing new was written. The "
                        "findings are in FACTS. Call drawing_auto (draft from the image) or files_write cad/spec.json now, "
                        "then drawing_build.")
    refusal = dp.gate(decision.tool, decision.args or {})
    if refusal:
        L.history.append(f"{L.step}. {decision.tool} REFUSED: " + ("read budget used up, write the spec" if dp.phase == _dp.READ
                                                                   else "targeted-read budget used, fix and rebuild"))
        return "skip", refusal
    return None, None


async def on_resume(L, note: str) -> str:
    """the user answered 继续 to a drawing pause: fresh counters + budget, AND a different strategy: the
    deterministic pipeline runs now (auto chains -> grid rooms -> names from the facts -> build -> auto-repair)."""
    dp = getattr(L, "_dphase", None) or _dp.DrawPhase()
    L._dphase = dp
    dp.resume()
    res = await auto_pipeline(L, "the user said 继续")
    L.history.append(f"{L.step}. (继续: deterministic pipeline) -> {res.splitlines()[0][:200]}")
    if dp.last_ok and getattr(L, "_cad_file", None):     # it passes: deliver now instead of another round
        await deliver_best(L, "确定性流水线一次通过")
        return note
    return (note + "\n\nSWITCHED STRATEGY (the user's 继续): the deterministic pipeline ran for you:\n" + res +
            "\nDo not re-read the image. If the build passed, send_file the PNG, DXF and PDF and finish with done; if it "
            "still reports errors, fix only those in cad/spec.json and drawing_build once more.")


async def converge(L, why: str) -> bool:
    """never pause in a loop: run the deterministic pipeline once (if it has not run since the last pause), and if
    the drawing still does not pass, deliver the best draft with what is unconfirmed. True when the task ended."""
    dp = getattr(L, "_dphase", None) or _dp.DrawPhase()
    L._dphase = dp
    if dp.auto_runs <= dp.resumes and not dp.must_deliver:
        res = await auto_pipeline(L, why)
        dp.quiet = _dp.QUIET_STEPS
        L.history.append(f"{L.step}. (stuck: {why[:80]} -> deterministic pipeline) -> {res.splitlines()[0][:200]}")
        L._converge_note = ("You were looping, so the deterministic pipeline ran instead:\n" + res +
                            "\nDo not re-read the image: send_file the PNG, DXF and PDF and finish with done (list what "
                            "is unconfirmed), or fix only the listed errors once.")
        if dp.last_ok:
            return await deliver_best(L, why)
        return False
    if not getattr(L, "_cad_file", None):      # nothing was ever built: nothing to deliver yet
        return False
    return await deliver_best(L, why)


async def deliver_best(L, why: str) -> bool:
    """send the best draft (PNG, DXF, PDF) with a list of what is unconfirmed, and finish the task."""
    from aiw_protocols import TaskStatus
    dp = getattr(L, "_dphase", None) or _dp.DrawPhase()
    dxf = getattr(L, "_cad_file", None)
    sent = []
    if dxf:
        stem = dxf[:-4] if dxf.lower().endswith(".dxf") else dxf
        for p in (stem + ".png", dxf, stem + ".pdf"):
            r = await L.tools.run("send_file", {"path": p}, intent="deliver the best draft")
            if not (r.success and isinstance(r.result, dict)):
                continue
            if check_delivery(L, r.result):
                continue
            await L._deliver_file(r.result)
            record_delivery(L, r.result)
            sent.append(Path(p).name)
    unc = await _uncertain(L)
    ok = dp.last_ok is True
    lines = [("图纸已生成并通过自动核对。" if ok else "已交付目前最好的草稿（自动核对还有未通过的项）。") +
             (f" 文件：{', '.join(sent)}。" if sent else " （没能取回文件，图纸在电脑的 cad/ 目录里。）")]
    if not ok and dp.last_errors:
        lines.append("未通过的核对：" + "；".join(_dp._short_err(e) for e in dp.last_errors[:6]))
    if unc:
        lines.append("待确认：" + "；".join(unc[:8]))
    lines.append("精度说明：比例来自照片上尺寸链的刻度（逐轴拟合），墙和房间来自像素网格，门窗按墙上的缺口推断；"
                 "照片本身不完全按比例时，个别墙位可能有 ±100–300 mm 偏差。")
    if not ok:
        lines.append(f"（停在这里而不是继续循环：{why}）")
    if sent:
        L._sent_file = sent[0]
    dp.phase = _dp.DELIVER
    L._finish(TaskStatus.SUCCEEDED, "\n".join(lines))
    return True


async def _uncertain(L) -> list:
    try:
        r = await L.tools.run("shell", {"command": 'cd "${VM_AGENT_FILES_ROOT:-$HOME/workspace}" && python3 -c "import json;'
                                                   's=json.load(open(\'cad/spec.json\'));print(json.dumps(s.get(\'uncertain\') or [],'
                                                   'ensure_ascii=False))"', "timeout": 20}, intent="read what is uncertain")
        return json.loads(((r.result or {}).get("stdout") or "[]").strip().splitlines()[-1]) if isinstance(r.result, dict) else []
    except Exception:  # noqa: BLE001
        return []


async def auto_pipeline(L, why: str = "") -> str:
    """auto chains -> grid rooms -> names from the facts (and the old spec) -> build (auto-repairs)."""
    dp = getattr(L, "_dphase", None) or _dp.DrawPhase()
    L._dphase = dp
    dp.auto_runs += 1
    # names the model / the user / the vision read gave (OCR-only names are re-read inside _auto with one VLM call)
    labels = [{"name": x.get("name"), "x": (x.get("at_px") or [None, None])[0], "y": (x.get("at_px") or [None, None])[1]}
              for x in dp.facts.get("labels") or [] if isinstance(x, dict) and x.get("by") not in (None, "ocr")]
    labels = [x for x in labels if x["name"] and x["x"] is not None]
    a = {"from_spec": True}
    if labels:
        a["labels"] = labels
    text = await _auto(L, a)
    if text.startswith("FAILED"):
        return text
    from types import SimpleNamespace
    b = await L._drawing_build({"spec": "cad/spec.json"}, "build the deterministic draft")
    post_tool(L, SimpleNamespace(tool="drawing_build", args={"spec": "cad/spec.json"}), not b.startswith("FAILED"), b)
    return text.split("\nNEXT:")[0] + "\n" + b


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
    """drawkit auto: the chains are FOUND in the image (3.8.9 - the model's pixel boxes are never used), their
    numbers come from the model's values / the facts / the old spec, tesseract (<=20 s), or one VLM read per chain
    crop; rooms come from the wall grid, names from labels (facts, the old spec's rooms by position, OCR, or one VLM
    read)."""
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
    # values only: a model-made box is dropped (task-3fc: left box held the wrong line, right box missed it)
    chains = [{"side": c["side"], "values": c["values"]} for c in chains
              if c.get("side") in ("top", "bottom", "left", "right") and c.get("values")]
    err = await L._ensure_drawkit()
    if err:
        return f"FAILED: {err}"
    await L.tools.run("files_write", {"path": "cad/auto_chains.json", "content": json.dumps({"chains": chains}, ensure_ascii=False)},
                      intent="save the chain values for the draft")
    r = await L.tools.run("shell", {"command": 'cd "${VM_AGENT_FILES_ROOT:-$HOME/workspace}" && [ -e cad/spec.json ] && echo exists; true',
                                    "timeout": 20}, intent="is there a spec already")
    exists = "exists" in (((r.result or {}).get("stdout") or "") if isinstance(r.result, dict) else "")
    from_spec = "cad/spec.json" if (exists and a.get("from_spec")) else None
    out = "cad/spec.json" if (not exists or from_spec) else "cad/spec.draft.json"
    if from_spec:
        await L.tools.run("shell", {"command": 'cd "${VM_AGENT_FILES_ROOT:-$HOME/workspace}" && cp -f cad/spec.json cad/spec.prev.json',
                                    "timeout": 20}, intent="keep the previous spec")
        from_spec = "cad/spec.prev.json"
    labels = None
    if isinstance(a.get("labels"), list) and a["labels"]:
        await L.tools.run("files_write", {"path": "cad/auto_labels.json", "content": json.dumps({"labels": a["labels"]}, ensure_ascii=False)},
                          intent="save the room labels")
        labels = "cad/auto_labels.json"

    async def run(lab):
        cmd = _dp.auto_command(image, out, "cad/auto_chains.json", lab, L._root_id, "cad/auto_debug.png", from_spec)
        rr = await L.tools.run("shell", {"command": cmd, "timeout": 900}, intent="draft the spec from the image")
        res_ = rr.result if isinstance(rr.result, dict) else {}
        return _dp.parse_auto(res_.get("stdout") or ""), res_
    rep, res = await run(labels)
    if rep and rep.get("need_values"):
        # the chains were found but no number could be read: one strict-JSON VLM read per chain crop
        got = await _vlm_chain_values(L, image, rep)
        if got:
            have = {c["side"] for c in chains}
            chains += [{"side": k, "values": v} for k, v in got.items() if k not in have]
            await L.tools.run("files_write", {"path": "cad/auto_chains.json", "content": json.dumps({"chains": chains}, ensure_ascii=False)},
                              intent="save the chain values for the draft")
            rep, res = await run(labels)
    unnamed = any(x.get("name") == "房间?" for x in (rep or {}).get("rooms") or [])
    traced = bool(rep and rep.get("wall_mode") == "traced")
    if rep and rep.get("ok") and labels is None and not traced and (len(rep.get("labels") or []) < 8 or unnamed):
        # OCR read none or only some of the printed names (task-3fc: 7 of 11): ask the vision model ONCE, on a
        # gridded crop, for JSON positions; an OCR hit of the same name nearby keeps its sharper position
        ocr = [x for x in rep.get("labels") or [] if x.get("x") is not None]
        labs = _merge_labels(await _vlm_labels(L, image, rep), ocr)
        if len(labs) > len(ocr):
            await L.tools.run("files_write", {"path": "cad/auto_labels.json", "content": json.dumps({"labels": labs}, ensure_ascii=False)},
                              intent="save the room labels")
            rep2, res2 = await run("cad/auto_labels.json")
            rep, res = (rep2, res2) if rep2 else (rep, res)
    if not rep or not rep.get("ok"):
        tail = (rep or {}).get("error") or ((res.get("stderr") or "") + (res.get("stdout") or ""))[-800:]
        await _emit(L, "drawing_auto", False, {}, str(tail)[:200])
        return f"FAILED: drawkit auto: {tail}"
    for lab in rep.get("labels") or []:
        dp.note("labels", lab.get("name"), {"name": lab.get("name"), "at_px": [lab.get("x"), lab.get("y")], "by": lab.get("by")})
    for c in rep.get("chains") or []:
        if c.get("values"):
            dp.facts["chains"][c["side"]] = {"values": c["values"], "by": c.get("by"), "line_px": c.get("line_px")}
            dp.locked.add(f"chains:{c['side']}")
    dp.note("source", "image", image)
    dp.note("source", "md5", rep.get("md5"))
    dp.after("drawing_auto", a, True, "")
    await save_facts(L)
    L._acted = True
    await _emit(L, "drawing_auto", True, {"rooms": len(rep.get("rooms") or []), "out": out})
    if traced:
        rooms = ", ".join(x["name"] for x in rep.get("rooms") or [])
        ch = "; ".join(f"{c['side']} {c.get('values')}" for c in rep.get("chains") or [])
        return (f"TRACED from the pixels into {out} (deterministic: {rep.get('walls')} walls, openings {rep.get('openings')}, "
                f"{rep.get('traced_lines')} traced lines; room names by OCR: {rooms or 'none'}; dimensions by OCR: {ch}; "
                f"debug image cad/auto_debug.png). Warnings: {rep.get('warnings') or 'none'}. Uncertain: {rep.get('uncertain') or 'none'}.\n"
                f"NEXT: drawing_build spec={out} right away" + (" (after moving it to cad/spec.json)" if out != "cad/spec.json" else "")
                + ". Only set title/subtitle if you know them; do not move walls, rooms or labels - drawing_build compares "
                  "the result with the source pixels itself.")
    rooms = "; ".join(f"{x['name']} {x['box']}" for x in rep.get("rooms") or [])
    ch = "; ".join(f"{c['side']} {c.get('values')} ({c.get('by')}, line at {c.get('line_px')} px)" for c in rep.get("chains") or [])
    return (f"DRAFT written to {out} ({len(rep.get('rooms') or [])} rectilinear rooms from the wall grid, debug image "
            f"cad/auto_debug.png): {rooms}.\nChains found in the image: {ch}. Checks: {rep.get('checks') or []}. "
            f"Openings placed: {rep.get('openings') or {}}. Uncertain: {rep.get('uncertain') or 'none'}.\nNEXT: CORRECT the "
            f"draft, do not re-derive it: only rename a room that is 房间? or wrongly named (names come from labels), set "
            f"title/subtitle" + (" and write it to cad/spec.json" if out != "cad/spec.json" else "") + ", then drawing_build.")


def _merge_labels(vlm: list, ocr: list, near: float = 60.0) -> list:
    out = []
    used = set()
    for v in vlm or []:
        k = next((i for i, o in enumerate(ocr) if i not in used and o.get("name") == v.get("name")
                  and abs(o["x"] - v["x"]) <= near and abs(o["y"] - v["y"]) <= near), None)
        if k is not None:
            used.add(k); out.append({"name": v["name"], "x": ocr[k]["x"], "y": ocr[k]["y"], "by": "ocr+vlm"})
        else:
            out.append(v)
    out += [dict(o) for i, o in enumerate(ocr) if i not in used]
    return out


async def _vlm_chain_values(L, image: str, rep: dict) -> dict:
    """one strict-JSON VLM read per chain crop whose numbers OCR could not read: {side: [values]}."""
    eyes = getattr(L, "vision_llm", None) or getattr(L, "llm", None)
    if eyes is None or not hasattr(eyes, "describe"):
        return {}
    img, err = await L._load_ws_image(image)
    if img is None:
        return {}
    out = {}
    for side, box in (rep.get("chain_boxes") or {}).items():
        n = (rep.get("segments") or {}).get(side)
        try:
            crop = grid_crop(img, box, target=1400)
            if side in ("left", "right"):
                crop = crop.rotate(-90, expand=True)
            buf = io.BytesIO(); crop.convert("RGB").save(buf, "JPEG", quality=90)
            q = (f"这是户型图的一条尺寸链（{n} 段）。按从{'左到右' if side in ('top', 'bottom') else '上到下（图已旋转，按图中从左到右）'}"
                 f"列出每一段上印的数字（毫米），只输出 JSON：{{\"values\": [..{n} 个整数..]}}")
            text = await asyncio.wait_for(eyes.describe("你是严谨的读图员，只报告图上真实存在的数字，只输出 JSON。", q,
                                                        base64.b64encode(buf.getvalue()).decode(), 300), 60)
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            vals = [int(v) for v in json.loads(m.group(0)).get("values")] if m else []
        except (ValueError, TypeError, AttributeError):
            vals = []
        if vals and (not n or len(vals) == n):
            out[side] = vals
    return out


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
