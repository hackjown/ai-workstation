"""Skills the user can pick with the composer's "+" menu.

The web app puts a marker at the top of the prompt, e.g. "【技能：officecli、huashu-motion】". The marker survives
retries and resumes because it is part of the prompt itself. `brief()` turns it into short instructions for the
agent: where the skill's SKILL.md lives on the computer, and how to set the skill up when it is missing.
Custom skills (name, path, description) are saved in settings under kv "skills_custom".
"""
from __future__ import annotations

import re
from typing import Optional

# 3.9.8: a floor plan turned into the interactive renovation designer (house-plan-designer), not a CAD redraw.
DESIGNER_AUTO = (r"house-?plan-?designer|装修设计器|户型设计器|设计器(网页|html)|"
                 r"(户型|平面|结构|cad|dxf|图纸)[^。\n]{0,40}(3d|三维|鸟瞰|漫游|设计器|做成网页|转成网页|生成网页|网页版|html|家具布置|装修方案)|"
                 r"(3d|三维|鸟瞰|漫游)[^。\n]{0,12}(户型|装修|看房)|floor ?plan (to|into) 3d")


def is_designer_task(prompt: str) -> bool:
    return bool(re.search(DESIGNER_AUTO, prompt or "", re.I))

# v3.9.14 auto triggers for the vendored anthropics skills
SKILL_CREATOR_AUTO = (r"skill-?creator|(做|写|创建|新建|生成|制作|保存|存|沉淀|整理)(成|一个|个|为)?[^。\n]{0,8}(技能|skill)(?!\s*(列表|市场))")
WEBAPP_TEST_AUTO = (r"webapp-?testing|playwright|(测试|自测|测一下|测一遍|检查|验收)[^。\n]{0,10}(网页|页面|前端|web ?app|网站)|"
                    r"(网页|页面|前端|网站)[^。\n]{0,10}(测试|自测|有没有\s*bug|报错)")
FRONTEND_AUTO = (r"frontend-?design|(做|写|设计|生成|搭|改)[^。\n]{0,10}(落地页|着陆页|官网|主页|landing ?page|仪表盘|dashboard|"
                 r"报告页|展示页|宣传页|个人网站|网页设计|前端页面|ui ?界面)")

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
              "设置 › 电脑 › 部署服务器. "
              # v3.9.30: the same rules the dev loop enforces (devloop.py), so prompt and framework agree
              "Tools of the coding loop (the system enforces these): keep the TODO list with todo_write — it starts from "
              "the task's acceptance items; one item in_progress at a time; an item may only be marked done after a "
              "verification (tests / the generator / verify_html / a diff check) ran after your last edit of its files. "
              "Before changing an EXISTING file read it (read_file with offset/limit, or grep/glob), then edit it with "
              "files_patch (exact `old` that matches once); never write fix_*.py scripts that rewrite files. Creating new "
              "files (PLAN.md, the prototype, new code and tests) is fine without reading. done needs the verification "
              "evidence; claim only TODO items that are done, the system appends the TODO receipt. If you are stuck "
              "(the same error repeats) re-read the code; if you need a decision or the step budget runs out, finish "
              "with done(outcome=\"ask\", summary=<question>, options=[…]) instead of guessing.")},
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
              "captchas, SMS/email codes on the user's phone, or a payment: request_takeover with action/site/what_to_do filled "
              "(an emailed or SMS code may instead be asked with ask_user fields, a single text field). Finish with "
              "done stating the account name and what is still pending (e.g. email verification); never repeat a "
              "password in the summary.")},
    {"id": "huashu-design", "title": "huashu-design 设计", "icon": "image",
     "desc": "出 UI 设计稿、海报、页面 mockup，并做设计评审",
     "path": "~/.claude/skills/huashu-design", "setup": ""},
    {"id": "librecad", "title": "LibreCAD 制图", "icon": "ruler",
     "desc": "按设计图和报价单画施工图，导出 DXF / PDF",
     "path": "", "setup": "bash ~/ai-workstation/deploy/vm/librecad-setup.sh",
     "note": ("New drawings follow the cad-drawing method: read each dimension chain with drawing_read, write "
              "cad/spec.json, generate with drawing_build. LibreCAD (cad_command) is the viewer and the editor for "
              "small manual tweaks only.")},
    {"id": "house-plan-designer", "title": "户型图 → 装修设计器", "icon": "ruler",
     "desc": "户型图（酷家乐/贝壳/售楼处图、手绘尺寸图、CAD 矢量 PDF/DXF、原始结构图）还原成单文件 HTML 装修设计器：2D + 3D 漫游 + 家具 + 水电/吊顶/铺装/拆改 + 管线自动排布 + 报价 + PDF/DXF 施工图",
     "path": "~/.claude/skills/house-plan-designer",
     "auto": DESIGNER_AUTO,
     # v3.9.16: the upstream commit in NOTICE.md is part of the check, so a computer with the older copy reinstalls
     "check": ("test -f ~/.claude/skills/house-plan-designer/assets/template.html && "
               "grep -q b94183f ~/.claude/skills/house-plan-designer/NOTICE.md && "
               "python3 ~/.claude/skills/house-plan-designer/scripts/aiw_hpd.py doctor | grep -q '\"ok\": true'"),
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/house-plan-designer-setup.sh "
               "-o /tmp/hpds.sh && bash /tmp/hpds.sh"),
     "note": ("Read the AIW section at the top of SKILL.md first, then the upstream flow under it, and "
              "reference/spec-format.md before writing the HouseSpec. Steps: `python3 ~/.claude/skills/house-plan-designer/"
              "scripts/aiw_hpd.py doctor` (ensure when not ok); work in ~/workspace/hpd/<name>/; if this task already has "
              "cad/spec.json from cad-drawing, start with `aiw_hpd.py from-cad cad/spec.json -o hpd/<name>/spec.json`; for a picture "
              "(jpg/png) start with `aiw_hpd.py from-image <img> -o hpd/<name>/spec.json --name <name>` (cad-drawing's "
              "deterministic tracer inside, ~10 s) and only patch the walls its open_rooms / shared_rooms name; only when it "
              "says ok=false use `aiw_hpd.py imgcheck` (analyze.py, or the dimension-chain route it advises); PDF/DXF use "
              "cadvec.py / cadsplit.py. Never hand-measure pixel peaks or ask the vision model for coordinates; loop "
              "`aiw_hpd.py make <spec> --out hpd/<name>/work --name <name>` at most 3 rounds (clean=true is best, can_finish=true is "
              "enough; open_ok / shared_ok rooms such as 阳台 / 空调位 / 可拓展空间 stay open; patch spec.json in place, never "
              "rebuild walls from cad-spec.json in a new script; read_image at most 4 times in all); then furnish + autoElec as SKILL.md step 5; deliver with `make ... --final "
              "hpd/<name>` then `aiw_hpd.py qa <html> --out hpd/<name>/qa` (lite+quick on the VM); send_file the HTML, plan2d "
              "and view3d PNGs; never edit assets/template.html. Do not use drawing_auto / drawing_build for this job.")},
    {"id": "cad-drawing", "title": "照图出图 CAD", "icon": "ruler",
     "desc": "照着照片或描述画技术图纸（户型/施工图、零件、接线/流程图）：精读尺寸 → 结构化 spec → 自动生成 DXF/PNG → 叠图核对",
     "path": "~/.claude/skills/cad-drawing",
     "auto": (r"施工图|装修图|平面图|立面图|户型图|剖面图|大样图|节点图|零件图|机械图|接线图|电气图|电路图|天花图|"
              r"(?<![a-z])cad(?![a-z])|librecad|\.dxf|(画|绘|出|重画|照着)[^，。,\n]{0,8}(图纸|流程图|框图|示意图)"),
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/cad-drawing-setup.sh "
               "-o /tmp/cds.sh && bash /tmp/cds.sh"),
     "note": ("drawing_build installs the kit by itself (~/workspace/.aiw/cad-drawing/drawkit.py, ezdxf and a Chinese "
              "font) on first use. Follow its checklist A-E: drawing_read one chain at a time (two reads must agree, "
              "opposite chains must add up, `drawkit.py fit` checks each number against its tick spacing), the spec in "
              "cad/spec.json (rooms from the image labels, never a template), drawing_build to regenerate the whole "
              "drawing (never ERASE patches), the overlay + report checks, at most 3 fix rounds, then report what is "
              "uncertain.")},
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
    {"id": "video-remake", "title": "复刻视频", "icon": "film",
     "desc": "复刻/仿拍一条参考视频：逐镜抽帧分析，出人物和场景参考图提示词（绑定 @图片N），再写可直接生成的逐秒视频提示词",
     "path": "~/.claude/skills/video-remake",
     "auto": (r"(复刻|仿拍|翻拍|照着拍|拍同款|做同款)[\s\S]{0,80}(视频|视屏|短片|片子|\.mp4|\.mov|\.webm|\.m4v)"
              r"|(视频|视屏|短片)\S{0,8}(复刻|仿拍|翻拍)"
              r"|(分析|反推|拆解|拉片)[\s\S]{0,30}(视频|视屏|短片)[\s\S]{0,30}提示词"
              r"|\.(mp4|mov|webm|m4v)\b[\s\S]*提示词|提示词[\s\S]*\.(mp4|mov|webm|m4v)\b"
              r"|remake\s+(this\s+|the\s+|a\s+)?video"),
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/video-remake-setup.sh "
               "-o /tmp/vrs.sh && bash /tmp/vrs.sh"),
     "note": ("SKILL.md asks you to read its template.md and example-麻将局.md as well; do. Deliver a remake kit, not a "
              "description. A) `python3 ~/.claude/skills/video-remake/remake.py frames "
              "<video> --out ~/workspace/remake/<name>` (probe, hard cuts, one frame per second, a frame after each cut, "
              "sheet.jpg; printed paths are already relative to ~/workspace for read_image); read sheet.jpg, then each "
              "shot's frames with read_image paths=[...] (<= 8 per call) and log timing, shot size, camera move, action "
              "per second. B) one character slot per recurring person (群像 slot for a background group): a standalone "
              "reference-image prompt (age range, face, hair, outfit colour+material, accessories, neutral standing pose, "
              "front + three-quarter view, plain light-grey background; props seen only in some seconds stay out). "
              "C) one scene slot per location, no people, same camera angle and aspect as the video. Slots are "
              "@图片1.. in order, characters first, scenes last. D) the final prompt exactly in the user's cleaned format "
              "(example-麻将局.md): 参考图绑定 (`@图片N是<ID>（…）`) / 参考图使用规则 / 全片统一设定 (with 时长与画幅) / "
              "逐秒提示词 (`第 N 秒（0:00–0:01）· 标题`, every person and place as @图片N, never 男子/女主/aliases) / 输出约束 "
              "(outfit locks, prop time windows, 无文字无字幕无水印); no vague words (等等/之类/大概/一些…), timings "
              "contiguous and adding up to the duration, > 15 s split into 片段. Write remake/<name>/复刻_<name>.md by "
              "template.md, run `remake.py check 复刻_<name>.md --duration <s>` until OK, `remake.py json … --analysis "
              "analysis.json > 复刻_<name>.json`, send_file both, done with the slot table and the check result. "
              "Reference images only when the user asks for them.")},
    # v3.9.14: three Apache-2.0 skills from anthropics/skills, vendored in skills/<id> (NOTICE.md), set up by aiw-skill-setup.sh
    {"id": "skill-creator", "title": "技能制作", "icon": "spark",
     "desc": "把一次做成的流程、或用户描述的做法写成新技能（SKILL.md + 脚本），以后的任务自动用上",
     "path": "~/.claude/skills/skill-creator", "auto": SKILL_CREATOR_AUTO,
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/aiw-skill-setup.sh "
               "-o /tmp/aiwss.sh && bash /tmp/aiwss.sh skill-creator"),
     "note": ("Read the AIW section at the top of SKILL.md. Write the new skill to ~/.claude/skills/<name>/ (AIW scans "
              "that folder at the start of every task, nothing else to register). Base it on what actually worked: the "
              "earlier task's successful steps, its errors and the user's corrections (session_search finds an earlier "
              "task). Scripts over prose; Chinese and English trigger words in the description; never secrets. Skip the "
              "eval/benchmark loop unless the user asks. Run scripts/quick_validate.py on the folder, send_file SKILL.md.")},
    {"id": "webapp-testing", "title": "网页自测", "icon": "monitor",
     "desc": "用 Playwright 打开网页或本地 HTML，点一遍、看控制台、截图，交付前自测",
     "path": "~/.claude/skills/webapp-testing", "auto": WEBAPP_TEST_AUTO,
     "check": "test -f ~/.claude/skills/webapp-testing/SKILL.md && python3 -c 'import playwright' 2>/dev/null",
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/aiw-skill-setup.sh "
               "-o /tmp/aiwss.sh && bash /tmp/aiwss.sh webapp-testing"),
     "note": ("Headless Playwright scripts (scripts/with_server.py for a dev server, file:// for a single HTML; WebGL pages "
              "need --use-gl=swiftshader --enable-unsafe-swiftshader). Collect console + pageerror, click the key controls, "
              "screenshot to ~/workspace/qa/<name>/ and read_image it; fix before delivering.")},
    {"id": "frontend-design", "title": "前端设计", "icon": "image",
     "desc": "做落地页、官网、仪表盘、报告页等网页时给出有设计感、不像模板的界面",
     "path": "~/.claude/skills/frontend-design", "auto": FRONTEND_AUTO,
     "setup": ("curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/aiw-skill-setup.sh "
               "-o /tmp/aiwss.sh && bash /tmp/aiwss.sh frontend-design"),
     "note": ("Commit to one clear aesthetic direction before coding; full Chinese font stack; single offline HTML that "
              "also works on a phone; self-test it the webapp-testing way before send_file.")},
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
    out = [s["id"] for s in CATALOG if s.get("auto") and re.search(s["auto"], text, re.I)]
    if "house-plan-designer" in out:      # the designer reads the plan itself; the CAD redraw phases would fight it
        out = [n for n in out if n not in ("cad-drawing", "librecad", "frontend-design")]
    return out


GIT_SKILL = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[/\s#?]|[^\x00-\x7f]|$)")


def git_skills(prompt: str) -> list[tuple]:
    """aiw-fix-1: skill repos named by URL ("安装 https://github.com/linkease/kai-skills 这个skill")."""
    if not re.search(r"skill|技能", prompt or "", re.I):
        return []
    out: list[tuple] = []
    for m in GIT_SKILL.finditer(prompt or ""):
        if (m.group(1), m.group(2)) not in out:
            out.append((m.group(1), m.group(2)))
    return out


# Skills already installed on a computer (npx skills add / git clone into ~/.claude/skills ...). Read from the computer
# itself at the start of a task, so a skill installed by an earlier task is known to every later one (the analysis of
# task-c987469146: kai-skills was installed, yet the next run hand-wrote paramiko scripts and grepped for credentials).
INSTALLED_PROBE = (
    'for d in "$HOME/.claude/skills" "$HOME/.workbuddy/skills" "$HOME/.agents/skills"; do '
    '[ -d "$d" ] && find -L "$d" -maxdepth 3 -name SKILL.md 2>/dev/null; done | sort -u | head -60 | '
    'while IFS= read -r f; do '
    'n=$(sed -n "s/^name:[[:space:]]*//p" "$f" | head -1); '
    'ds=$(sed -n "s/^description:[[:space:]]*//p" "$f" | head -1 | cut -c1-180); '
    'printf "SKILL|%s|%s|%s\n" "$f" "$n" "$ds"; done'
)


def parse_installed(stdout: str) -> list[dict]:
    out, seen = [], set()
    for line in (stdout or "").splitlines():
        if not line.startswith("SKILL|"):
            continue
        parts = line.split("|", 3) + ["", "", ""]
        path, name, desc = parts[1].strip(), parts[2].strip().strip("'\""), parts[3].strip().strip("'\"")
        if not path:
            continue
        name = name or path.rstrip("/").split("/")[-2]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "path": path, "desc": desc})
    return out


def installed_brief(items: list[dict], prompt: str = "", limit: int = 3000) -> str:
    """System-prompt block naming the skills installed on this computer; the ones the request's words touch come first."""
    if not items:
        return ""
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", (prompt or "").lower())}

    def hit(it: dict) -> int:
        text = (it["name"] + " " + it["desc"] + " " + it["path"]).lower()
        return sum(1 for w in words if w in text)

    items = sorted(items, key=lambda it: -hit(it))
    lines = ["SKILLS INSTALLED ON THIS COMPUTER (an earlier task installed them; never clone or install them again). "
             "When one fits the job, read its SKILL.md first and use its own scripts/commands and the connection details "
             "it documents instead of writing your own code or searching old scripts for credentials:"]
    size = len(lines[0])
    for it in items:
        ln = f"- {it['name']}: {it['desc'][:180] or '(no description)'} ({it['path']})"
        if size + len(ln) > limit:
            lines.append(f"- … {len(items) - len(lines) + 1} more under ~/.claude/skills")
            break
        lines.append(ln)
        size += len(ln)
    return "\n".join(lines)


def brief(prompt: str, custom: Optional[list[dict]] = None) -> str:
    names = picked(prompt)
    names += [n for n in mentioned(prompt, custom) if n.lower() not in [x.lower() for x in names]]
    names += [n for n in auto(prompt) if n.lower() not in [x.lower() for x in names]]
    gh = git_skills(prompt)
    if not names and not gh:
        return ""
    lines = ["The user picked these skills for this task; use them instead of improvising:"]
    for owner, repo in gh:
        lines.append(
            f"- {repo} (skill repo github.com/{owner}/{repo}): FIRST check whether it is already installed: "
            f"`find ~/.claude/skills ~/.workbuddy/skills ~/.agents/skills ~/workspace/{repo} -maxdepth 3 -name SKILL.md 2>/dev/null`. "
            "If its skills are there, do NOT clone or install it again (an earlier run may have done it): read the SKILL.md "
            "files that fit the job and use that skill's own scripts/commands for the work instead of writing your own "
            "(say in the summary which skill you used). Only if nothing is there: "
            f"`npx -y skills add {owner}/{repo} -g -a claude-code -s '*' -y --copy`, once, then list what it installed.")
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


# 3.9.0 pre-flight: before the agent starts, the system checks each skill the task uses and runs its setup when it
# is missing, so the agent never starts by improvising an install (or skipping it). `check` exits 0 when installed.
CHECKS = {
    "officecli": "test -x ~/.local/bin/officecli && test -f ~/.workbuddy/skills/officecli/SKILL.md",
    "librecad": "command -v librecad >/dev/null",
    "photocraft": "command -v photocraft-cli >/dev/null || test -x ~/.local/bin/photocraft-cli",
}


def used(prompt: str, custom: Optional[list[dict]] = None) -> list[str]:
    """Every skill a task uses: picked with "/" or "+", named in words, or needed by the request itself."""
    names = picked(prompt)
    names += [n for n in mentioned(prompt, custom) if n.lower() not in [x.lower() for x in names]]
    names += [n for n in auto(prompt) if n.lower() not in [x.lower() for x in names]]
    return names


def preflight(prompt: str, custom: Optional[list[dict]] = None) -> list[dict]:
    """[{id, check, setup}] for the skills this task uses that can be checked on the computer."""
    out = []
    for n in used(prompt, custom):
        s = _find(n, custom or [])
        if not s:
            continue
        sid = s.get("id") or n
        check = s.get("check") or CHECKS.get(sid) or (f"test -f {s['path']}/SKILL.md" if s.get("path") else "")
        if not check:
            continue
        setup = s.get("setup") or (f"npx -y skills add {s['source']} -g -a claude-code -s '*' -y --copy" if s.get("source") else "")
        out.append({"id": sid, "title": s.get("title") or sid, "check": check, "setup": setup})
    return out


def listing(custom: Optional[list[dict]] = None) -> list[dict]:
    out = [dict(s, builtin=True) for s in CATALOG]
    for s in custom or []:
        if s.get("id") and not any(x["id"] == s["id"] for x in out):
            out.append({"id": s["id"], "title": s.get("title") or s["id"], "icon": "spark",
                        "desc": s.get("desc", ""), "path": s.get("path", ""), "setup": s.get("setup", ""), "builtin": False,
                        "source": s.get("source", ""), "machines": s.get("machines", [])})
    return out
