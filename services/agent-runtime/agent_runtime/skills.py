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
     "note": ("OfficeCLI is a command-line program plus this skill guide. Call the program by its full path "
              "~/.local/bin/officecli (older computers do not have ~/.local/bin on PATH); if that file is missing, run "
              "the setup. Prefer it over GUI clicking for any .docx/.xlsx/.pptx edit, and after an edit render to PDF "
              "(soffice --headless --convert-to pdf) to check the pages. "
              "Blank or extra pages: do not study the XML first. Render to PDF, count pages (pdfinfo) and characters per "
              "page (pdftotext -f N -l N), and look at the page images; a page with a few characters (a footer like "
              "'第1页') is overflow from the page before, not a page to delete. In a PDF-converted .docx every page is a "
              "section with exact line heights, so fix overflow by shrinking that section's spacing: the empty paragraph "
              "at the section start, the empty section-break paragraph, huge exact line heights (lineSpacing) or "
              "spaceBefore/After on the footer — `officecli set f.docx /body/p[N] --prop lineSpacing=1pt --prop "
              "lineRule=exact --prop spaceBefore=0pt --prop spaceAfter=0pt` — then render again until the page count "
              "matches the source. Save as a new file (…_去空白页.docx), keep the original, and send_file it.")},
    {"id": "reverse-engineer-anything", "title": "REA 逆向分析", "icon": "search",
     "desc": "拆解 Electron、JS、网页应用：看结构、找接口、抓协议",
     "path": "~/.workbuddy/skills/reverse-engineer-anything", "setup": "bash ~/ai-workstation/deploy/vm/devagents-setup.sh"},
    {"id": "dev", "title": "开发 程序", "icon": "code",
     "desc": "在电脑上写程序并测试；有界面的先出页面截图给你选，确认后再开发",
     "path": "", "setup": "",
     "note": ("Work as a careful software engineer in ~/workspace/<project> (git init, commit each milestone). "
              "1) Restate the goal and write PLAN.md (features, stack, how it will be tested). "
              "2) If the program has ANY user interface (web page, app screen, desktop window), do NOT write app code yet: "
              "build a static clickable prototype (plain HTML/CSS, realistic content, no lorem ipsum) — two or three distinct "
              "directions when the user gave no style — and if huashu-design was also picked, design it by that skill. "
              "Screenshot every key screen with headless Chrome (`chromium --headless=new --hide-scrollbars --screenshot=a.png "
              "--window-size=1440,900 file://…`, and 390,844 for mobile when it matters), send_file each PNG, then ask_user "
              "which direction / what to change. Iterate until the user confirms; only then implement. "
              "3) Implement in small steps; write automated tests next to the code and run them after every change; "
              "fix until they pass. For a web UI also run it and screenshot the real pages, checking they match the approved "
              "mockup. 4) Finish with send_file of the final screenshots (and a zip of the project if asked), then done "
              "with: what was built, how to run it, and the test results (numbers, not 'tests pass'). "
              "5) Deploy only when the user asked for it or says yes to ask_user '要部署到服务器吗？': if the deploy_server "
              "tool is available, call it with upload=<project folder> and the install command (prefer Docker compose "
              "or an install.sh you wrote and tested here), then check the result (curl the page, docker ps) with "
              "another deploy_server call and report the address. If the tool is missing, tell the user to fill "
              "设置 › 电脑 › 部署服务器.")},
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


def mentioned(prompt: str, custom: Optional[list[dict]] = None) -> list[str]:
    """A skill named in plain words ("你用officecli去修改") counts as picked too: task-b26 typed the name
    instead of ticking it, got no brief, and spent 40 steps reading XML."""
    low = (prompt or "").lower()
    out = []
    for sk in list(custom or []) + CATALOG:
        sid = (sk.get("id") or "").lower()
        if len(sid) >= 6 and re.search(rf"(?<![a-z0-9-]){re.escape(sid)}(?![a-z0-9-])", low) and sid not in out:
            out.append(sid)
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
    names += [n for n in mentioned(prompt, custom) if n.lower() not in [x.lower() for x in names]]
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
        setup = s.get("setup") or (f"npx -y skills add {s['source']} -g -a claude-code -s '*' -y --copy" if s.get("source") else "")
        if setup:
            line += f"; if it is not installed, run `{setup}` once"
        if s.get("note"):
            line += ". " + s["note"]
        lines.append(line + ".")
    return "\n".join(lines)


def listing(custom: Optional[list[dict]] = None) -> list[dict]:
    out = [dict(s, builtin=True) for s in CATALOG]
    for s in custom or []:
        if s.get("id") and not any(x["id"] == s["id"] for x in out):
            out.append({"id": s["id"], "title": s.get("title") or s["id"], "icon": "spark",
                        "desc": s.get("desc", ""), "path": s.get("path", ""), "setup": s.get("setup", ""), "builtin": False,
                        "source": s.get("source", ""), "machines": s.get("machines", [])})
    return out
