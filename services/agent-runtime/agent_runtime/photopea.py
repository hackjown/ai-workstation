"""photopea tool: edit images like in Photoshop, in Photopea on the VM's desktop.

Photoshop has no Linux build, so the agent drives Photopea (photopea.com, a browser clone of Photoshop: PSD in and
out, layers, text, masks, filters, Photoshop-style JavaScript). The bridge (photopea_bridge.py, stdlib only) is
shipped to the VM by the agent itself on first use, so 104 and the cloud box need nothing but Chrome/Chromium.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

BRIDGE_SRC = Path(__file__).with_name("photopea_bridge.py")
REMOTE = "$HOME/.config/aiw/photopea/pp.py"
FORMATS = (".png", ".jpg", ".jpeg", ".webp", ".psd", ".gif", ".svg", ".pdf", ".tiff", ".tif", ".bmp")


def bridge_version() -> str:
    m = re.search(r'^PPV = "([^"]+)"', BRIDGE_SRC.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0"


def install_command() -> str:
    """Write the bridge to the VM unless the very same file is already there (one shell call)."""
    src = BRIDGE_SRC.read_bytes()
    b64 = base64.b64encode(src).decode()
    digest = hashlib.sha1(src).hexdigest()
    return (f'mkdir -p "$HOME/.config/aiw/photopea" && f="{REMOTE}" && '
            f'if [ "$(sha1sum "$f" 2>/dev/null | cut -d" " -f1)" != "{digest}" ]; then '
            f"echo '{b64}' | base64 -d > \"$f.tmp\" && mv \"$f.tmp\" \"$f\" && echo installed; "
            f"else echo present; fi && [ \"$(sha1sum \"$f\" | cut -d' ' -f1)\" = \"{digest}\" ] && echo PP_OK")


def build_job(a: dict) -> dict:
    """Tool args -> bridge job. Paths stay relative to ~/workspace; the bridge resolves them."""
    def lst(v):
        if v is None or v == "":
            return []
        return [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
    try:
        timeout = int(a.get("timeout") or 90)
    except (TypeError, ValueError):
        timeout = 90
    return {"open": lst(a.get("open")), "place": lst(a.get("place")), "fonts": lst(a.get("fonts")),
            "save": lst(a.get("save")), "script": str(a.get("script") or ""), "keep": bool(a.get("keep")),
            "info": bool(a.get("info")), "timeout": max(20, min(timeout, 600))}


def check_job(job: dict) -> str | None:
    """A message for the model when the call cannot work, before anything runs on the VM."""
    s = job["script"]
    if not (job["open"] or s or job["save"] or job["info"] or job["place"]):
        return "photopea needs at least one of open / script / save / info"
    if re.search(r"\barguments\b", s):
        return ("do not use `arguments` in a Photopea script: its engine dies on it and never answers. "
                "Give helper functions named parameters instead.")
    if re.search(r"function\s+(log|hex|place)\s*\(", s):
        return "do not define log/hex/place yourself: use the built-in ppLog / ppColor / ppPlace"
    if re.search(r"\bapp\.echoToOE\s*\(", s):
        return "use ppLog(...) instead of app.echoToOE: an echo in the middle of a script can kill Photopea"
    if "saveToOE" in s:
        return "do not call saveToOE yourself: list the output files in `save` (the format comes from the extension)"
    if job["place"] and not job["open"] and not job["keep"]:
        return "place adds images to an open document: also give `open`, or keep=true to use the one already open"
    for p in job["save"]:
        if Path(p.rsplit(":", 1)[0] if re.search(r":[\d.]+$", p) else p).suffix.lower() not in FORMATS:
            return f"cannot tell the format of {p}: use .psd .png .jpg .webp .gif .svg or .pdf"
    return None


def run_command(job: dict) -> str:
    b64 = base64.b64encode(json.dumps(job, ensure_ascii=False).encode()).decode()
    return f'python3 "{REMOTE}" run --job-b64 {b64}'


def shell_timeout(job: dict) -> int:
    steps = len(job["open"]) + len(job["fonts"]) + 4
    return min(3600, 180 + job["timeout"] * steps)


def parse_result(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("PPRESULT "):
            try:
                return json.loads(line[9:])
            except ValueError:
                return None
    return None


def _layers(layers, depth=0, out=None, limit=80):
    out = [] if out is None else out
    for l in layers or []:
        if len(out) >= limit:
            break
        bits = [f"{'  ' * depth}- {l.get('name')!s}", str(l.get("type", ""))]
        if l.get("bounds"):
            bits.append("bounds " + ",".join(str(x) for x in l["bounds"]))
        if not l.get("visible", True):
            bits.append("hidden")
        if l.get("opacity") not in (None, 100):
            bits.append(f"opacity {l['opacity']}")
        if "text" in l:
            bits.append(f"TEXT {str(l['text'])[:80]!r} font {l.get('font')} size {l.get('size')} color #{l.get('color')}")
        out.append(" ".join(bits))
        if l.get("layers"):
            _layers(l["layers"], depth + 1, out, limit)
    return out


def saved_paths(res: dict) -> list[str]:
    out = []
    for s in res.get("saved") or []:
        p = str(s.get("path", ""))
        out.append(p.split("/workspace/", 1)[1] if "/workspace/" in p else p)
    return out


def summarize(res: dict) -> str:
    """Result for the model's history."""
    lines = ["photopea OK." if res.get("ok") else f"photopea FAILED: {res.get('error')}"]
    for rel, s in zip(saved_paths(res), res.get("saved") or []):
        lines.append(f"saved {rel} ({s.get('bytes')} bytes)")
    if res.get("echo"):
        lines.append("ppLog: " + " | ".join(str(e) for e in res["echo"])[:1500])
    info = res.get("info")
    if isinstance(info, list):
        for d in info:
            lines.append(f"document {d.get('name')} {d.get('width')}x{d.get('height')} px"
                         f"{' (active)' if d.get('active') else ''}; layers top to bottom:")
            lines += _layers(d.get("layers"))
    if res.get("ok") and res.get("saved"):
        lines.append("Check the result with read_image on the saved PNG/JPG before calling it done; "
                     "send_file the files the user asked for.")
    return "\n".join(lines)
