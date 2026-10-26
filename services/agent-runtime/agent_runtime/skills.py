"""Skills the user can pick with the composer's "+" menu.

The web app puts a marker at the top of the prompt, e.g. "【技能：officecli、huashu-motion】". The marker survives
retries and resumes because it is part of the prompt itself. `brief()` turns it into short instructions for the
agent: where the skill's SKILL.md lives on the computer, and how to set the skill up when it is missing.
Custom skills (name, path, description) are saved in settings under kv "skills_custom".
"""
from __future__ import annotations

import re
from typing import Optional

MARK = re.compile(r"【技能[:：]\s*([^】]+)】")

SEARCH_DIRS = "~/.claude/skills ~/.workbuddy/skills ~/.codebuddy/skills ~/.agents/skills"

CATALOG: list[dict] = [
    {"id": "huashu-motion", "title": "huashu-motion 视频", "icon": "film",
     "desc": "把文档、PPT 做成带配音字幕的讲解视频或动画",
     "path": "~/.claude/skills/huashu-motion", "setup": "bash ~/ai-workstation/deploy/vm/motion-setup.sh"},
    {"id": "officecli", "title": "OfficeCLI 文档", "icon": "doc",
     "desc": "精确读写 Word、Excel、PPT：改格式、填表、批量替换、出 PDF",
     "path": "~/.workbuddy/skills/officecli", "setup": "bash ~/ai-workstation/deploy/vm/devagents-setup.sh",
     "note": "The CLI is ~/.local/bin/officecli; prefer it over GUI clicking for any .docx/.xlsx/.pptx edit."},
    {"id": "reverse-engineer-anything", "title": "REA 逆向分析", "icon": "search",
     "desc": "拆解 Electron、JS、网页应用：看结构、找接口、抓协议",
     "path": "~/.workbuddy/skills/reverse-engineer-anything", "setup": "bash ~/ai-workstation/deploy/vm/devagents-setup.sh"},
    {"id": "huashu-design", "title": "huashu-design 设计", "icon": "image",
     "desc": "出 UI 设计稿、海报、页面 mockup，并做设计评审",
     "path": "~/.claude/skills/huashu-design", "setup": ""},
    {"id": "librecad", "title": "LibreCAD 制图", "icon": "ruler",
     "desc": "按设计图和报价单画施工图，导出 DXF / PDF",
     "path": "", "setup": "bash ~/ai-workstation/deploy/vm/librecad-setup.sh",
     "note": "Use the built-in cad_draw / cad tools for the drawing; LibreCAD is only for viewing."},
    {"id": "photopea", "title": "Photopea 修图", "icon": "image",
     "desc": "AI 改图后用 Photopea 精修文字、图层，导出 PSD / PNG",
     "path": "", "setup": "", "note": "Use the image_edit tool for the AI edit and the photopea tool for exact finishing."},
]


def picked(prompt: str) -> list[str]:
    out: list[str] = []
    for m in MARK.finditer(prompt or ""):
        for n in re.split(r"[、,，\s]+", m.group(1)):
            if n.strip() and n.strip() not in out:
                out.append(n.strip())
    return out


def strip(prompt: str) -> str:
    return MARK.sub("", prompt or "").strip()


def _find(name: str, custom: list[dict]) -> Optional[dict]:
    key = name.lower()
    for s in list(custom or []) + CATALOG:
        if key in (str(s.get("id", "")).lower(), str(s.get("title", "")).lower()):
            return s
    return None


def brief(prompt: str, custom: Optional[list[dict]] = None) -> str:
    names = picked(prompt)
    if not names:
        return ""
    lines = ["The user picked these skills for this task; use them instead of improvising:"]
    for n in names:
        s = _find(n, custom or [])
        if not s:
            lines.append(f"- {n}: find it with `ls -d $(for d in {SEARCH_DIRS}; do echo $d/*; done) 2>/dev/null | grep -i {n}`, "
                         "then read its SKILL.md first and follow it.")
            continue
        sid = s.get("id") or n
        line = f"- {sid} ({s.get('desc', '')})"
        if s.get("path"):
            line += f": read {s['path']}/SKILL.md first (only that file, not the skill's source) and follow it"
            line += f"; if the folder is missing, look in {SEARCH_DIRS}"
        if s.get("setup"):
            line += f"; if it is not installed, run `{s['setup']}` once"
        if s.get("note"):
            line += ". " + s["note"]
        lines.append(line + ".")
    return "\n".join(lines)


def listing(custom: Optional[list[dict]] = None) -> list[dict]:
    out = [dict(s, builtin=True) for s in CATALOG]
    for s in custom or []:
        if s.get("id") and not any(x["id"] == s["id"] for x in out):
            out.append({"id": s["id"], "title": s.get("title") or s["id"], "icon": "spark",
                        "desc": s.get("desc", ""), "path": s.get("path", ""), "setup": s.get("setup", ""), "builtin": False})
    return out
