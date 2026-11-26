#!/usr/bin/env python3
"""acadmcp_runner: the optional second drawing engine (autocad-mcp-pro, ezdxf headless backend) behind drawkit.

Run ONLY by the isolated venv's python (its top-level modules - server, config, engineering, backends - have
generic names and must never be imported into drawkit's own interpreter). drawkit hands it one JSON document on
stdin and reads one JSON line prefixed ACADMCP_RESULT from stdout. Nothing here is exposed to the model as a tool.

input  {"spec": {sheet, lang:"en", poche, walls, openings, rooms, chains}, "out_dxf": path,
        "topology_layers": [..] (optional)}  or, QA only on an existing DXF:
       {"qa_dxf": path, "wall_layers": [..]}
output {"ok", "dxf", "critique_total", "by_focus", "by_severity", "issues": [...], "not_checked": [...],
        "domain_absent": [...], "rooms_detected": [...], "topology_findings": {...}, "omitted": [...],
        "rooms_dropped": reason|null, "version", "seconds"}
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

PIN = "1.8.0"


def _rows(issues):
    from dataclasses import asdict, is_dataclass
    return [asdict(i) if is_dataclass(i) else dict(i) for i in issues]


async def _run(doc):
    from backends.ezdxf_backend import EzdxfBackend
    from engineering.arch.spec import draw_plan_from_spec

    spec = doc.get("spec") or {}
    out = {"ok": False, "rooms_dropped": None}

    async def fresh():
        b = EzdxfBackend()
        await b.connect()
        await b.drawing_new()
        return b

    if doc.get("qa_dxf"):
        # QA only: read an existing DXF (e.g. the native engine's) and run the same checks on it
        b = EzdxfBackend()
        await b.connect()
        await b.drawing_open(doc["qa_dxf"])
        out["dxf"] = doc["qa_dxf"]
        return await _qa(b, doc, out)
    import re
    dropped = []
    for _ in range(len(spec.get("rooms") or []) + 1):
        b = await fresh()
        try:
            res = await draw_plan_from_spec(b, spec)
            break
        except ValueError as ex:
            # a room point in no closed face / in a face already labelled refuses the whole transaction (rolled
            # back): drop that room record and redraw; without any room when the refusal names none
            m = re.match(r"rooms\[(\d+)\]", str(ex))
            rooms = list(spec.get("rooms") or [])
            if not rooms:
                raise
            if m and int(m.group(1)) < len(rooms):
                dropped.append({"room": rooms[int(m.group(1))].get("id"), "reason": str(ex)[:300]})
                rooms.pop(int(m.group(1)))
            else:
                dropped.append({"room": "*", "reason": str(ex)[:300]})
                rooms = []
            spec = dict(spec, rooms=rooms)
    else:
        raise ValueError("rooms could not be placed")
    out["rooms_dropped"] = dropped or None
    out["omitted"] = [{k: str(v) for k, v in (o or {}).items()} for o in res.get("omitted") or []]
    out["openings_drawn"] = res.get("openings") or []
    saved = await b.drawing_save_as(doc["out_dxf"])
    out["dxf"] = saved.get("path") or doc["out_dxf"]
    return await _qa(b, doc, out)


async def _qa(b, doc, out):
    from engineering.qa_coverage import LEDGER_KEY, CoverageLedger, summarize
    from engineering.qa_scan import QA_SNAPSHOT_KEY

    shared: dict = {}
    rows = _rows(await b.drawing_critique(None, shared=shared))
    summ = summarize(rows, summary=True, limit=40)
    out["critique_total"] = summ["total"]
    out["by_focus"] = summ["by_focus"]
    out["by_severity"] = summ["by_severity"]
    out["issues"] = [{"focus": r.get("focus"), "severity": r.get("severity"), "message": str(r.get("message"))[:300],
                      "detail": {k: v for k, v in (r.get("detail") or {}).items()
                                 if k in ("centroid", "area_mm2", "point", "at", "code", "gap", "length")}}
                     for r in rows[:60]]
    ledger = shared.get(LEDGER_KEY) or CoverageLedger()
    snap = (shared.get(QA_SNAPSHOT_KEY) or (None, None))[0]
    cov = ledger.report(snap.layouts if snap is not None else ())
    nc, absent = [], []
    for lay in cov.get("layouts") or []:
        for x in lay.get("not_checked") or []:
            nc.append({"layout": lay.get("layout"), "lane": x if isinstance(x, str) else x.get("check") or x.get("lane") or str(x),
                       "reason": None if isinstance(x, str) else x.get("reason")})
        absent += [a for a in lay.get("domain_absent") or [] if a not in absent]
    out["not_checked"], out["domain_absent"], out["coverage_complete"] = nc, absent, cov.get("complete")

    try:
        from engineering.arch.rooms import rooms_detect
        rd = await rooms_detect(b, layers=doc.get("wall_layers"))
        out["rooms_detected"] = [{"area_m2": round(r.get("area_m2") or 0, 2), "centroid": r.get("centroid"),
                                  "label": r.get("label"), "confidence": r.get("confidence"),
                                  "loop": r.get("loop")} for r in rd.get("rooms") or []]
        out["rooms_skipped"] = len(rd.get("skipped") or [])
    except Exception as ex:  # noqa: BLE001
        out["rooms_detected"] = []
        out["rooms_error"] = f"{type(ex).__name__}: {ex}"

    try:
        from engineering.understand.snapshot import take_snapshot
        from engineering.understand.topology import finding_rows, topology_findings
        snap2 = await take_snapshot(b, None)
        layers = doc.get("topology_layers") or doc.get("wall_layers") or [l for l in snap2.layers if "WALL" in l.upper() and "-H-" not in l.upper()]
        tf = topology_findings(snap2, layers=layers, gap=None, tol=0.01)
        ts = summarize(finding_rows(tf), summary=True, limit=20)
        out["topology_findings"] = {"layers": layers, "total": ts["total"], "by_kind": ts["by_focus"],
                                    "by_severity": ts["by_severity"],
                                    "issues": [{"focus": r.get("focus"), "severity": r.get("severity"),
                                                "message": str(r.get("message"))[:200]} for r in ts["issues"]]}
    except Exception as ex:  # noqa: BLE001
        out["topology_findings"] = {"error": f"{type(ex).__name__}: {ex}"}
    out["ok"] = True
    return out


def main():
    t0 = time.time()
    # 1.8.0 keeps a durable sqlite journal in ACADMCP_STATE_DIR: never inside a repo / workspace
    os.environ["JOURNAL_DURABLE"] = "off"
    made = None
    if not os.environ.get("ACADMCP_STATE_DIR"):
        made = tempfile.mkdtemp(prefix="acadmcp-state-")
        os.environ["ACADMCP_STATE_DIR"] = made
    try:
        doc = json.loads(sys.stdin.read())
        import anyio
        out = anyio.run(_run, doc)
        try:
            from importlib.metadata import version
            out["version"] = version("autocad-mcp-pro")
        except Exception:  # noqa: BLE001
            out["version"] = None
    except Exception as ex:  # noqa: BLE001
        out = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:1500]}
    if made:
        import shutil
        shutil.rmtree(made, ignore_errors=True)
    out["seconds"] = round(time.time() - t0, 2)
    print("ACADMCP_RESULT " + json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
