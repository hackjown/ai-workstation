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
    {"id": "signup", "title": "网站注册 填资料", "icon": "doc",
     "desc": "在网站注册账号、填写资料：每一步页面要填什么先发表单给你填，再由 Agent 填入",
     "path": "", "setup": "",
     "auto": r"注册(?!表)|开户|sign\s*-?\s*up|create (an |a )?account|填(写)?(资料|表|信息)|申请(账号|帐号|ID)",
     "note": ("Sign-up / form-filling flow. Use the browser on the desktop (open the official site, never a mirror). "
              "For EACH page step: 1) screenshot and read every input on the page (labels, required marks, select "
              "options such as country/region, birthday format, security questions, password rules); 2) ask_user with "
              "`fields` for exactly those inputs, title like '<site> 注册 · 第 N 步', description saying what it is "
              "for, password rules in the field hint, passwords as type password, select options copied from the page; "
              "never fill personal data the user did not give, and do not ask for data the page does not want; "
              "3) click each field and `type` the value (passwords: type the {{key}} placeholder), pick selects, then "
              "screenshot to check before pressing continue/next; 4) on validation errors, ask_user again with only "
              "the fields to fix and the page's error text as hint. Human checks: Cloudflare 'verify you are human' / "
              "Turnstile checkbox: click the box yourself, wait 5 s, screenshot; after 2 failed tries, or for image "
              "captchas, SMS/email codes on the user's phone, or a payment: request_takeover with exactly what to do "
              "(an emailed or SMS code may instead be asked with ask_user fields, a single text field). Finish with "
              "done stating the account name and what is still pending (e.g. email verification); never repeat a "
              "password in the summary.")},
    {"id": "huashu-design", "title": "huashu-design 设计", "icon": "image",
     "desc": "出 UI 设计稿、海报、页面 mockup，并做设计评审",
     "path": "~/.claude/skills/huashu-design", "setup": ""},
    {"id": "librecad", "title": "LibreCAD 制图", "icon": "ruler",
     "desc": "按设计图和报价单画施工图，导出 DXF / PDF",
     "path": "", "setup": "bash ~/ai-workstation/deploy/vm/librecad-setup.sh",
     "note": "Use the built-in cad_draw / cad tools for the drawing; LibreCAD is only for viewing."},
    {"id": "photocraft", "title": "PhotoCraft 修图", "icon": "image",
     "desc": "不用 AI 的精确修图：裁剪、改尺寸、调色、曲线、锐化、模糊、文字、PSD，可批量",
     "path": "", "auto": r"photocraft",
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/photocraft-setup.sh "
               "-o /tmp/pcs.sh && bash /tmp/pcs.sh"),
     "note": ("PhotoCraft is a Photoshop-like editor with a headless CLI; every edit is a plain algorithm (no AI model, no "
              "network). Check `photocraft-cli --version`; if missing, run the setup (needs sudo). Prefer the CLI over the GUI: "
              "`photocraft-cli info in.png` (size, layers as JSON); find a command and its JSON params with "
              "`photocraft-cli commands --json --filter <word>` (e.g. imageSize, crop, curves, levels, hueSaturation, "
              "brightnessContrast, unsharpMask, gaussianBlur, canvasSize, rotate, type); then "
              "`photocraft-cli run in.png --cmd <id> --params '<json>' [--cmd … --params …] --out out.png` "
              "(--quality for jpg/webp; never overwrite the original; .psd/.pcraft keep layers). Many images: "
              "`photocraft-cli batch --help`. Look at the result (read the image) before sending, then send_file it. "
              "Use the GUI (`photocraft`, on the desktop display) only when the user wants to watch or take over; it has no "
              "GPU here so it is slow. Do not use image_edit (AI) for this skill unless the user asks for an AI change. "
              "Recipe, raise a cabinet/counter by D px so it lines up with another (measure both top edges in pixels first "
              "with python/PIL column profiles near the junction): one run with --cmd select.lasso {points: polygon around the "
              "counter top plus everything standing on it, top edge on a plain band of wall, side edge along the junction} → "
              "layer.new.layerViaCopy → layer.translate {dy:-D} → layer.selectBottom → select.rect {the door band just under "
              "the counter} → layer.new.layerViaCopy → edit.transform {rect:[x0,y0,x1,y1], quad: same box with its top moved "
              "up by D} → layer.flattenImage --out. Keep posters/objects in front outside both regions, then crop-compare "
              "before/after at the junction and send both.")},
    {"id": "photopea", "title": "Photopea 修图", "icon": "image",
     "desc": "AI 改图后用 Photopea 精修文字、图层，导出 PSD / PNG",
     "path": "", "setup": "", "note": "Use the image_edit tool for the AI edit and the photopea tool for exact finishing."},
    {"id": "xhs", "title": "小红书运营", "icon": "spark",
     "desc": "在已登录的浏览器里点赞、回复我笔记下的评论、给兴趣相近的笔记写评论、发布笔记；发出前都要你确认",
     "path": "", "setup": "",
     "auto": r"小红书\s*(运营|养号|互动|点赞|涨粉)|xhs\s*运营",
     "note": ("Work in the visible Chrome of THIS computer, which keeps the user's xiaohongshu.com login (the local VM on "
              "the home network is preferred over the cloud box: a datacenter IP is more likely to be flagged). Always start "
              "with xhs_status. Not logged in -> request_takeover asking the user to scan the QR code once with the 小红书 app. "
              "Behave like a person, never like a script: wait a random 3-20 s between actions, open a post and scroll/read it "
              "(images, text, a few comments) for 10-60 s before deciding anything, skip most posts, mix browsing, search "
              "(browser_navigate to https://www.xiaohongshu.com/search_result?keyword=<one of the user's keywords>) and the "
              "home feed, never act on many posts in a row, end the session when its time is up. Likes: only on posts you "
              "actually read and that fit the persona; call xhs_like first, then click the heart once. Comments on other "
              "people's posts and replies on the user's own posts (通知 › 评论和@ or the user's own notes): write each one fresh "
              "from that post's real content and the persona/tone, short and specific, as the user would say it; never a "
              "template, never the same sentence twice, no links, no ads, no 互关/私信/引流, no hashtags in comments. Save each "
              "with xhs_draft (target_url, target_title, target_excerpt = what you read), then send the batch with xhs_review "
              "and wait. Send only approved drafts: xhs_begin -> type the returned text exactly -> pause -> click 发送 -> "
              "xhs_sent. Posts go through https://creator.xiaohongshu.com/publish/publish (upload the media files with the "
              "file dialog, ctrl+L for the path, title <= 20 chars), only after approval, same xhs_begin/xhs_sent steps. "
              "If ANY captcha, slider, verification, 操作频繁 or account warning appears: call xhs_warning at once, do not "
              "touch it, and finish with done. Never solve a captcha. Respect the caps the tools report.")},
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


def auto(prompt: str) -> list[str]:
    """Skills a task needs whether or not it was picked (a sign-up must ask for the data, never invent it)."""
    text = strip(prompt or "")
    return [s["id"] for s in CATALOG if s.get("auto") and re.search(s["auto"], text, re.I)]


def brief(prompt: str, custom: Optional[list[dict]] = None) -> str:
    names = picked(prompt)
    names += [n for n in mentioned(prompt, custom) if n.lower() not in [x.lower() for x in names]]
    names += [n for n in auto(prompt) if n.lower() not in [x.lower() for x in names]]
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
