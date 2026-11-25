"""v3.9.86 项目模式: a project plan posted in an engine group runs the whole loop.

  讨论   every member gives its view on the plan (no files changed); AIW 管家 writes the final plan (定稿方案)
  设计   when the project has a front end: the designers each make a design (local engines: a self-contained page
         design/<engine>.html, which AIW opens in a browser on the computer and screenshots; Cue: its own pictures),
         the options go to the group as pictures, and the user picks one
  开发   the plan's steps, one after another (the existing queue: each step starts from the earlier steps' work)
  评审   a reviewer checks; the problems it lists go back to the member who owns them, then it reviews again
         (at most MAX_REWORK rounds)
  验收   AIW 管家 merges, runs the checks; a failing check goes back to its owner with the output, then again
         (at most MAX_ACCEPT_FIX rounds)
  实测   AIW 管家 starts the project from the merged branch, opens its pages in a browser (a command-line tool: its
         output) and screenshots them; the screenshots ride in the acceptance report
  交付   the user picks: push the branch, take a patch file, or merge into the base branch (only when the user's
         checkout is on the base branch and clean). Nothing is pushed or merged without that tap.

State lives in bot_groups.project (JSON). The work runs through the group's queue: the manager's own steps carry
an "op" (synth / designs / review_check / accept / shots).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from typing import Optional

from . import bots as B

log = logging.getLogger("aiw.group_project")

MANAGER_ID = "管家"
MAX_REWORK = 2
MAX_ACCEPT_FIX = 2
LETTERS = "ABCDEFGH"

TRIGGER = re.compile(r"^\s*(?:[@＠]\s*管家\s*)?(?:/项目|项目规划|项目计划|开发项目|新项目|项目)\s*[:：\n ]")

DISCUSS = ("【项目讨论，这一步不要改任何文件、不要提交】用户在群里发了下面的项目规划。请从你的角度给出意见："
           "需求有没有漏洞或含糊的地方、建议的技术方案和目录结构、风险、你适合负责哪部分、怎么验收。"
           "简明扼要，分点写。\n\n项目规划：\n{brief}")

SYNTH = """你是 AIW 管家，带一组 Agent 做一个真实项目。根据用户的项目规划和成员的讨论，写定稿方案和分工。
成员：{members}
只输出一个 JSON 对象，不要别的文字：
{{"summary": "定稿方案（markdown，含：目标、功能清单、技术方案、目录结构、验收标准）",
  "has_ui": true/false（项目有网页、App 或其他图形界面时为 true）,
  "design_by": ["做设计方案的成员id", ...]（has_ui 时选 2～3 个；本地成员做可在浏览器打开的静态设计稿，云端成员出效果图）,
  "steps": [{{"bot": "本地成员id", "task": "这一步的具体开发任务，写清改哪些文件、怎么验证"}}, ...],
  "review_by": "评审成员id（优先云端成员；没有就选一个没参与开发的本地成员；都没有写空字符串）",
  "checks": ["验收要跑的命令，如 python3 -m pytest -q 或 npm test", ...],
  "run": {{"setup": "启动前要执行的命令（如 npm install；没有写空）", "start": "启动项目的命令（如 python3 -m http.server 8765 或 npm run dev -- --port 8765；命令行工具写空）",
          "url": "启动后打开的地址，如 http://127.0.0.1:8765/", "pages": ["要截图的页面路径，如 / 或 /login", ...],
          "cli": "命令行工具时用来实际运行演示的命令（有界面时写空）"}}}}
规则：
- 开发步骤只能分给本地成员（{local}），每一步能独立完成，按依赖顺序排；写测试的步骤写清测试文件；不要让人 push、切分支、合并别人的分支。
- 端口固定用 8765 一类的高位端口，start 命令必须在前台持续运行（不要加 &）。
- checks 只写不需要联网、几分钟内能跑完的命令。
- 用中文。"""

REVIEW_CHECK = """你是 AIW 管家。下面是评审成员对项目的评审意见。判断有没有必须修的问题，并把每个问题交给负责的开发成员。
开发成员和各自负责的步骤：
{owners}
只输出 JSON：{{"pass": true/false, "issues": [{{"bot": "开发成员id", "fix": "要怎么改，写具体"}}]}}
只有阻碍验收的问题（功能缺失、bug、和定稿方案或选定设计稿不符、测试缺失）才算；风格建议不算。没有问题时 pass=true、issues 为空。"""

ACCEPT_FIX = """你是 AIW 管家。验收命令失败了。根据失败输出和分工，判断该交给哪个开发成员修，并写清要修什么。
开发成员和各自负责的步骤：
{owners}
只输出 JSON：{{"bot": "开发成员id", "fix": "要修什么（引用关键报错）"}}"""


def _json(out: str) -> Optional[dict]:
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    return v if isinstance(v, dict) else None


def is_trigger(text: str) -> bool:
    return bool(TRIGGER.match(text or "")) and len((text or "").strip()) > 12


def pick_of(text: str, designs: list) -> Optional[int]:
    """「选 B」「方案2」「用 agy 的」 -> the design's index."""
    t = (text or "").strip()
    m = re.match(r"^\s*(?:选|用|就|要)?\s*(?:方案|设计)?\s*([A-Ha-h]|[1-8])\s*(?:方案|吧|。|$)", t)
    if m:
        k = m.group(1).upper()
        i = LETTERS.index(k) if k in LETTERS else int(k) - 1
        return i if 0 <= i < len(designs) else None
    if len(t) <= 30 and re.search(r"(选|用|就|要)", t):
        for i, d in enumerate(designs):
            if d["bot"].lower() in t.lower() or B.ENGINES.get(d["bot"], {}).get("title", "\x00").lower() in t.lower():
                return i
    return None


def shot_script(out_dir: str, items: list, wd: str = "", setup: str = "", start: str = "", url: str = "",
                cli: str = "") -> str:
    """Bash for the computer: start the project (its own process group), wait for the address, screenshot each
    item with a headless browser, stop it. items: [(name, url_or_file)]. A command-line tool: run it, put its
    output in a terminal-style page, screenshot that."""
    L = ['set +e', f'O={out_dir}', 'mkdir -p "$O"', 'rm -f "$O"/*.png',
         'BR=$(command -v chromium || command -v chromium-browser || command -v google-chrome || command -v google-chrome-stable || true)',
         'shot() { if [ -n "$BR" ]; then timeout 60 "$BR" --headless=new --no-sandbox --disable-gpu --hide-scrollbars '
         '--force-device-scale-factor=1 --window-size=1280,860 --virtual-time-budget=4000 --screenshot="$O/$1.png" "$2" >/dev/null 2>&1; '
         'elif python3 -c "import playwright" 2>/dev/null; then timeout 90 python3 - "$O/$1.png" "$2" <<\'PY\' >/dev/null 2>&1\n'
         'import sys\nfrom playwright.sync_api import sync_playwright\n'
         'with sync_playwright() as p:\n    b = p.chromium.launch()\n    pg = b.new_page(viewport={"width": 1280, "height": 860})\n'
         '    pg.goto(sys.argv[2]); pg.wait_for_timeout(1500); pg.screenshot(path=sys.argv[1]); b.close()\nPY\n'
         'fi; [ -s "$O/$1.png" ] && echo "SHOT $1 $O/$1.png" || echo "SHOT_FAIL $1"; }',
         '[ -z "$BR" ] && ! python3 -c "import playwright" 2>/dev/null && echo "NO_BROWSER"']
    if wd:
        L.append(f'cd {wd} || {{ echo "RUN_ERR 进不了 {wd}"; exit 0; }}')
    if setup:
        L.append(f'timeout 600 bash -c {shlex.quote(setup)} > "$O/setup.log" 2>&1 || echo "SETUP_FAIL $(tail -n 5 "$O/setup.log" | tr "\\n" " ")"')
    if start:
        L += [f'setsid bash -c {shlex.quote(start)} > "$O/server.log" 2>&1 < /dev/null & PG=$!',
              f'U={shlex.quote(url or "http://127.0.0.1:8765/")}',
              'UP=0; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 2 "$U" && { UP=1; break; }; '
              'kill -0 $PG 2>/dev/null || break; sleep 1; done',
              '[ "$UP" = 1 ] && echo "UP $U" || echo "NOT_UP $(tail -n 8 "$O/server.log" | tr "\\n" " ")"']
    if cli:
        L += [f'timeout 120 bash -c {shlex.quote(cli)} > "$O/cli.txt" 2>&1; echo "CLI_EXIT $?"',
              f'python3 - "$O/cli.txt" "$O/cli.html" {shlex.quote(cli)} <<\'PY\'\n'
              'import html, sys\nt = open(sys.argv[1], errors="replace").read()[-6000:]\n'
              'open(sys.argv[2], "w").write("<html><body style=\\"margin:0;background:#16130f;color:#e8e2d8;font:14px/1.5 '
              'Menlo,monospace;padding:20px\\"><div style=\\"color:#f0a030\\">$ " + html.escape(sys.argv[3]) + '
              '"</div><pre style=\\"white-space:pre-wrap\\">" + html.escape(t) + "</pre></body></html>")\nPY',
              'shot cli "file://$O/cli.html"']
    for name, target in items:
        if target.startswith("file://~/") and re.fullmatch(r"[\w./\-]+", target[9:]):   # a file in the workspace
            L.append(f'shot {shlex.quote(name)} "file://$HOME/{target[9:]}"')
        else:
            L.append(f'shot {shlex.quote(name)} {shlex.quote(target)}')
    if start:
        L.append('kill -TERM -- -$PG 2>/dev/null; sleep 1; kill -KILL -- -$PG 2>/dev/null; echo STOPPED')
    L.append('echo SHOTS_DONE')
    return "\n".join(L)


def parse_shots(out: str) -> dict:
    r = {"shots": [], "fail": [], "no_browser": "NO_BROWSER" in (out or ""), "up": None, "error": ""}
    for line in (out or "").splitlines():
        w = line.split(" ", 2)
        if w[0] == "SHOT" and len(w) == 3:
            r["shots"].append((w[1], w[2]))
        elif w[0] == "SHOT_FAIL" and len(w) > 1:
            r["fail"].append(w[1])
        elif w[0] == "UP":
            r["up"] = True
        elif w[0] == "NOT_UP":
            r["up"] = False
            r["error"] = line[7:]
        elif w[0] in ("RUN_ERR", "SETUP_FAIL"):
            r["error"] = (r["error"] + " " + line).strip()
        elif w[0] == "CLI_EXIT" and len(w) > 1:
            r["cli_exit"] = w[1]
    return r


class Project:
    """The pipeline, on top of a Groups instance (bot_groups.Groups)."""

    def __init__(self, groups):
        self.g = groups

    # ------------------------------------------------------------------ helpers
    @property
    def bots(self):
        return self.g.bots

    def state(self, gid: str) -> dict:
        return dict((self.g.group(gid) or {}).get("project") or {})

    def put(self, gid: str, **kw) -> dict:
        p = self.state(gid)
        p.update(kw)
        if "phase" in kw:
            p.setdefault("log", []).append([kw["phase"], time.time()])
        self.g.set_group(gid, project=p)
        return p

    def say(self, gid: str, text: str, data: Optional[dict] = None):
        return self.g.add(gid, "aiw", "AIW 管家", text, {**(data or {}), "board": True})

    def title(self, b: str) -> str:
        return B.ENGINES.get(b, {}).get("title", b)

    def local_ids(self, g: dict) -> list:
        return [m["bot"] for m in g["members"] if m.get("branch") and not B.is_cloud_engine(m["bot"])]

    def owners(self, p: dict) -> str:
        return "\n".join(f"- {s['bot']}：{B.clip(s['task'], 200)}" for s in p.get("steps") or []) or "（无）"

    def last_reply(self, gid: str, bot: str) -> dict:
        r = self.g.store._x("SELECT text, data FROM bot_group_messages WHERE grp=? AND role='bot' AND who=? "
                            "ORDER BY seq DESC LIMIT 1", (gid, bot))
        return {"text": r[0]["text"], "data": B._j(r[0]["data"], {})} if r else {"text": "", "data": {}}

    async def llm(self, sys_p: str, user: str, n: int = 2500) -> str:
        if not self.bots.llm:
            return ""
        try:
            return await asyncio.wait_for(self.bots.llm().complete(sys_p, user, n), 150)
        except Exception as e:  # noqa: BLE001
            log.warning("project llm: %s", e)
            return ""

    def wt(self, gid: str, name: str = "_merge") -> str:
        return f"~/workspace/.aiw-worktrees/{gid}/{name}"

    async def fetch(self, g: dict, rel_paths: list) -> list:
        """Screenshots / patch files on the computer -> artifacts on the API host (through a local member's conv)."""
        conv = next((m["conv"] for m in g["members"] if m.get("branch") and m.get("conv")), None)
        if not conv:
            return []
        out_dir = self.bots._art_dir() if hasattr(self.bots, "_art_dir") else "workspace/artifacts"
        got = []
        for name, path in rel_paths:
            try:
                r = await self.bots.fetch_file(conv, path, out_dir)
                got.append({"name": r["name"], "path": r["path"], "size": r.get("size"),
                            "kind": B.media_kind(r["name"]), "label": name})
            except Exception as e:  # noqa: BLE001
                log.warning("project fetch %s: %s", path, e)
        return got

    async def screenshots(self, g: dict, sub: str, items: list, wd: str = "", run: Optional[dict] = None) -> dict:
        run = run or {}
        out_dir = self.wt(g["id"], "_shots/" + sub)
        from .bot_groups import qp
        script = shot_script(qp(out_dir), items, qp(wd) if wd else "", run.get("setup") or "", run.get("start") or "",
                             run.get("url") or "", run.get("cli") or "")
        try:
            out = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(script), 900)
        except Exception as e:  # noqa: BLE001
            return {"shots": [], "fail": [], "no_browser": False, "up": None, "error": B.clip(e, 300), "media": []}
        r = parse_shots(out)
        rel = [(n, re.sub(r"^/(?:home/[^/]+|root)/workspace/", "~/workspace/", p)) for n, p in r["shots"]]
        r["media"] = await self.fetch(g, rel)
        return r

    # ------------------------------------------------------------------ 1. 讨论
    async def start(self, gid: str, text: str, attachments: Optional[list] = None):
        g = self.g.group(gid)
        brief = TRIGGER.sub("", text, count=1).strip() or text
        if not self.local_ids(g):
            self.say(gid, "项目模式要有本地编程成员（agy、Codex、Claude Code、CodeBuddy、Grok）写代码，这个群里只有云端成员。"
                          "先用「+ 成员」加一个本地成员。")
            return
        self.g.set_group(gid, project={"phase": "discuss", "brief": brief, "rework": 0, "fixes": 0, "designs": [],
                                       "log": [["discuss", time.time()]]})
        members = [m["bot"] for m in g["members"]]
        cloud_first = sorted(members, key=lambda b: 0 if B.is_cloud_engine(b) else 1)
        steps = [{"bot": b, "task": DISCUSS.format(brief=brief), "step": "讨论：给出意见"} for b in cloud_first]
        steps.append({"bot": MANAGER_ID, "op": "synth", "task": "汇总讨论，写定稿方案", "step": "汇总讨论，写定稿方案"})
        self.say(gid, "收到项目规划，按 讨论 → " + "设计（有前端时）→ 开发 → 评审 → 验收 → 实测截图 → 交付 来走。先请每位成员说说意见。")
        await self.g._start_steps(gid, steps, "第一轮：讨论", attachments)

    # ------------------------------------------------------------------ manager ops (called from Groups._run_next)
    async def op(self, gid: str, s: dict):
        fn = {"synth": self.synth, "designs": self.designs, "review_check": self.review_check,
              "accept": self.accept, "shots": self.shots}.get(s.get("op") or "")
        if not fn:
            await self.g._run_next(gid)
            return
        try:
            await fn(gid, s)
        except Exception as e:  # noqa: BLE001
            log.exception("project op %s", s.get("op"))
            self.g.set_group(gid, status="idle", queue=[], pending=[])
            self.say(gid, f"管家在「{s.get('step') or s.get('op')}」这一步出错了：{B.clip(e, 300)}。说「@管家 继续」重试这一步。")
            self.g.set_group(gid, queue=[s])

    async def synth(self, gid: str, s: dict):
        g = self.g.group(gid)
        p = self.state(gid)
        local = self.local_ids(g)
        mem = "；".join(f"{m['bot']}（{self.title(m['bot'])}{'，云端' if B.is_cloud_engine(m['bot']) else '，本地代码'}）"
                       for m in g["members"])
        out = await self.llm(SYNTH.format(members=mem, local="、".join(local)),
                             f"仓库：{g['repo']}（基线 {g['base']}）\n\n项目规划：\n{p.get('brief')}\n\n"
                             f"讨论：\n{self.g._context(g, {'bot': ''}, 14000)}", 4000)
        plan = _json(out) or {}
        steps = [x for x in (plan.get("steps") or []) if isinstance(x, dict) and x.get("bot") in local and x.get("task")]
        if not steps:
            steps = [{"bot": local[0], "task": "按项目规划完成开发，并写测试：\n" + p.get("brief", "")}]
        designers = [b for b in (plan.get("design_by") or []) if b in [m["bot"] for m in g["members"]]][:3]
        has_ui = bool(plan.get("has_ui"))
        if has_ui and not designers:
            designers = (local + [m["bot"] for m in g["members"] if B.is_cloud_engine(m["bot"])])[:2]
        review_by = plan.get("review_by") if plan.get("review_by") in [m["bot"] for m in g["members"]] else ""
        if not review_by:
            cloud = [m["bot"] for m in g["members"] if B.is_cloud_engine(m["bot"])]
            idle = [b for b in local if b not in {x["bot"] for x in steps}]
            review_by = (cloud or idle or [""])[0]
        run = plan.get("run") if isinstance(plan.get("run"), dict) else {}
        checks = [c for c in (plan.get("checks") or []) if isinstance(c, str)][:5]
        summary = str(plan.get("summary") or "").strip() or p.get("brief", "")
        p = self.put(gid, summary=summary, has_ui=has_ui, steps=steps, review_by=review_by, run=run, checks=checks,
                     designers=designers if has_ui else [])
        lines = "\n".join(f"{i}. {self.title(x['bot'])}：{B.clip(x['task'].splitlines()[0], 90)}" for i, x in enumerate(steps, 1))
        tail = (f"\n\n评审：{self.title(review_by)}" if review_by else "") + (
            "\n验收命令：" + "；".join(f"`{c}`" for c in checks) if checks else "")
        self.say(gid, f"**定稿方案**\n\n{summary}\n\n**分工**\n{lines}{tail}", {"project_plan": True})
        if has_ui and designers:
            self.put(gid, phase="design")
            dsteps = []
            for b in designers:
                if B.is_cloud_engine(b):
                    t = ("【设计方案】按定稿方案为这个项目的界面出一套视觉设计：主要页面的布局、配色、字体、组件风格，"
                         "并生成 1～3 张主要页面的效果图。不要写代码。\n\n定稿方案：\n" + summary)
                else:
                    t = (f"【设计方案，这一步只做设计稿】按定稿方案为项目界面做一份设计稿：新建 design/{b}.html，"
                         "一个自包含的静态页面（样式写在页面里，不引用网络资源和图片 URL，可以用 CSS/SVG），"
                         "把主要页面并排或上下排在这一个文件里，内容用真实感的示例数据。只改 design/ 目录，不写功能代码。"
                         "做完 git add 并 commit。回复里写一句设计思路。\n\n定稿方案：\n" + summary)
                dsteps.append({"bot": b, "task": t, "step": "出设计方案", "design": True})
            dsteps.append({"bot": MANAGER_ID, "op": "designs", "task": "截图设计方案给用户选", "step": "截图设计方案"})
            self.g.set_group(gid, queue=dsteps + list(self.g.group(gid).get("queue") or []))
            n = len(designers)
            self.say(gid, f"有前端，先请 {'、'.join(self.title(b) for b in designers)} 各出一套设计方案（{n} 套），截图后给你选。")
            await self.g._run_next(gid)
            return
        self.put(gid, phase="confirm")
        self.g.set_group(gid, status="idle", pending=[], queue=[])
        self.say(gid, "方案定了。没问题就点「开工」（或回复“开工”）；要改就直接说，我改了再开工。",
                 {"actions": [{"id": "go", "label": "开工"}]})
        await self.g._tell_chat(self.g.group(gid), "项目方案定了，等你确认开工。")

    # ------------------------------------------------------------------ 2. 设计
    async def designs(self, gid: str, s: dict):
        g = self.g.group(gid)
        p = self.state(gid)
        opts = []
        for i, b in enumerate(p.get("designers") or []):
            label = LETTERS[i]
            r = self.last_reply(gid, b)
            note = B.clip(re.sub(r"\s+", " ", r["text"]), 240)
            if B.is_cloud_engine(b):
                media = [x for x in (r["data"].get("media") or []) if x.get("kind") == "image"][:3]
                opts.append({"label": label, "bot": b, "note": note, "media": media})
                continue
            m = self.g.member(g, b)
            path = f"{m['worktree'].rstrip('/')}/design/{b}.html"
            sh = await self.screenshots(g, f"design-{b}", [(f"design-{b}", "file://" + path)])
            opts.append({"label": label, "bot": b, "note": note, "media": sh.get("media") or [], "file": f"design/{b}.html",
                         "src": path, "error": "电脑上没有浏览器（装 chromium 后可截图）" if sh.get("no_browser") else (sh.get("error") or "")})
        self.put(gid, phase="pick", designs=opts)
        self.g.set_group(gid, status="idle", pending=[], queue=[])
        body = "\n\n".join(f"**方案 {o['label']}**（{self.title(o['bot'])}）：{o['note'] or '（没有说明）'}"
                           + ("" if o["media"] else f"\n（没拿到截图{('：' + o['error']) if o.get('error') else ''}）")
                           for o in opts)
        media = [{**x, "label": f"方案 {o['label']}"} for o in opts for x in o["media"]]
        self.say(gid, f"**设计方案，请选一个**\n\n{body}\n\n点下面的按钮，或回复“选 A”。也可以说想改哪里。",
                 {"media": media, "actions": [{"id": "pick", "value": o["label"], "label": f"用方案 {o['label']}"} for o in opts]})
        await self.g._tell_chat(self.g.group(gid), f"项目有 {len(opts)} 套设计方案，等你选。")

    async def pick(self, gid: str, idx: int):
        p = self.state(gid)
        d = (p.get("designs") or [])[idx]
        self.put(gid, pick=d["label"], phase="dev")
        self.say(gid, f"用方案 {d['label']}（{self.title(d['bot'])}）。开始开发。")
        note = (f"\n\n界面严格按用户选定的设计方案 {d['label']} 来做" +
                (f"：设计稿是仓库里的 {d['file']}（已合进你的分支，没有就执行 git merge aiw/{gid}/{d['bot']}）；"
                 "其他 design/*.html 是没选中的方案，不要参考，也不要删。" if d.get("file") else
                 f"（{self.title(d['bot'])} 的效果图和说明）：{d.get('note', '')}"))
        await self.develop(gid, note)

    async def go(self, gid: str):
        self.put(gid, phase="dev")
        self.say(gid, "开工。")
        await self.develop(gid, "")

    # ------------------------------------------------------------------ 3. 开发 + 4. 评审 + 5. 验收 + 6. 实测
    async def develop(self, gid: str, design_note: str):
        p = self.state(gid)
        head = f"定稿方案：\n{B.clip(p.get('summary', ''), 6000)}{design_note}\n\n你这一步："
        steps = [{"bot": x["bot"], "task": head + x["task"], "step": B.clip(x["task"].splitlines()[0], 80)}
                 for x in p.get("steps") or []]
        steps += self.tail(p)
        self.put(gid, phase="dev")
        await self.g._start_steps(gid, steps, "开发")

    def tail(self, p: dict) -> list:
        out = []
        if p.get("review_by"):
            out.append({"bot": p["review_by"], "task": self.review_task(p), "step": "评审", "review": True})
            out.append({"bot": MANAGER_ID, "op": "review_check", "task": "看评审意见", "step": "看评审意见"})
        out.append({"bot": MANAGER_ID, "op": "accept", "task": "合并并跑验收命令", "step": "验收"})
        return out

    def review_task(self, p: dict) -> str:
        pick = next((d for d in p.get("designs") or [] if d["label"] == p.get("pick")), None)
        return ("【评审】对照定稿方案" + (f"和用户选定的设计方案 {pick['label']}（{pick.get('file') or self.title(pick['bot'])}）"
                                      if pick else "") +
                "评审各成员的实现：功能是否齐全、有没有 bug、测试是否覆盖验收标准、界面是否符合设计。"
                "列出必须修的问题（写清文件和位置），没有就写“评审通过”。\n\n定稿方案：\n" + B.clip(p.get("summary", ""), 6000))

    async def review_check(self, gid: str, s: dict):
        p = self.state(gid)
        r = self.last_reply(gid, p.get("review_by") or "")
        v = _json(await self.llm(REVIEW_CHECK.format(owners=self.owners(p)), r["text"] or "（没有评审意见）", 1500)) or {}
        dev = {x["bot"] for x in p.get("steps") or []}
        issues = [i for i in (v.get("issues") or []) if isinstance(i, dict) and i.get("bot") in dev and i.get("fix")]
        if v.get("pass", True) or not issues:
            self.say(gid, "评审通过，进入验收。" if r["text"] else "没有评审意见，直接验收。")
            await self.g._run_next(gid)
            return
        if p.get("rework", 0) >= MAX_REWORK:
            self.say(gid, f"评审还有 {len(issues)} 个问题，已经返工 {MAX_REWORK} 轮，不再自动返工，先验收，问题写进报告。",
                     {"open_issues": issues})
            self.put(gid, open_issues=issues)
            await self.g._run_next(gid)
            return
        n = p.get("rework", 0) + 1
        self.put(gid, rework=n, phase="rework")
        by = {}
        for i in issues:
            by.setdefault(i["bot"], []).append(i["fix"])
        fix = [{"bot": b, "task": f"【返工 第 {n}/{MAX_REWORK} 轮】评审提出的问题，逐条修好、跑测试、commit：\n- " + "\n- ".join(f),
                "step": f"返工（第 {n} 轮）"} for b, f in by.items()]
        fix += [{"bot": p["review_by"], "task": "【复审】" + self.review_task(p), "step": "复审", "review": True},
                {"bot": MANAGER_ID, "op": "review_check", "task": "看复审意见", "step": "看复审意见"}]
        rest = [x for x in self.g.group(gid).get("queue") or [] if x.get("op") != "review_check"]
        self.g.set_group(gid, queue=fix + rest)
        self.say(gid, f"评审发现 {len(issues)} 个问题，交回给 {'、'.join(self.title(b) for b in by)} 返工（第 {n}/{MAX_REWORK} 轮）。")
        await self.g._run_next(gid)

    async def accept(self, gid: str, s: dict):
        g = self.g.group(gid)
        p = self.put(gid, phase="accept")
        r = await self.g.merge(gid)
        g = self.g.group(gid)
        if g["status"] == "fixing":   # conflicts went back to the members; accept again once they are fixed
            self.g.set_group(gid, queue=[s] + list(g.get("queue") or []))
            return
        if r.get("error"):
            self.g.set_group(gid, status="idle", queue=[s])
            self.say(gid, f"合并没做成，验收停在这里：{r['error']}。处理好后说「@管家 继续」。")
            return
        runs = await self.g._run_checks(g, p.get("checks") or [])
        bad = [(c, o) for c, o in runs if not re.search(r"EXIT 0\s*$", o.strip())]
        self.put(gid, runs=[[c, B.clip(o, 3000)] for c, o in runs])
        if bad and p.get("fixes", 0) < MAX_ACCEPT_FIX:
            n = p.get("fixes", 0) + 1
            fail = "\n\n".join(f"$ {c}\n{B.clip(o, 2500)}" for c, o in bad)
            v = _json(await self.llm(ACCEPT_FIX.format(owners=self.owners(p)), fail, 800)) or {}
            dev = [x["bot"] for x in p.get("steps") or []]
            bot = v.get("bot") if v.get("bot") in dev else (dev[-1] if dev else self.local_ids(g)[0])
            self.put(gid, fixes=n, phase="fix")
            self.say(gid, f"验收没过（{len(bad)} 条命令失败），交给 {self.title(bot)} 修（第 {n}/{MAX_ACCEPT_FIX} 次）。\n\n```\n{B.clip(fail, 1500)}\n```")
            self.g.set_group(gid, queue=[
                {"bot": bot, "task": f"【验收返修 第 {n}/{MAX_ACCEPT_FIX} 次】验收命令失败。{v.get('fix') or ''}\n"
                                     f"先执行 git merge aiw/{gid}/merge 拿到合并后的代码，修好，确认下面的命令都通过，commit。\n\n{fail}",
                 "step": f"验收返修（第 {n} 次）"},
                s, *list(self.g.group(gid).get("queue") or [])])
            await self.g._run_next(gid)
            return
        self.g.set_group(gid, queue=[{"bot": MANAGER_ID, "op": "shots", "task": "实际运行并截图", "step": "实测截图"}])
        await self.g._run_next(gid)

    async def shots(self, gid: str, s: dict):
        g = self.g.group(gid)
        p = self.put(gid, phase="run")
        run = dict(p.get("run") or {})
        items = []
        if run.get("start"):
            base = (run.get("url") or "http://127.0.0.1:8765/").rstrip("/")
            for i, pg in enumerate((run.get("pages") or ["/"])[:6]):
                pg = pg if str(pg).startswith("http") else base + "/" + str(pg).lstrip("/")
                items.append((f"page{i + 1}", pg))
        if not run.get("start") and not run.get("cli"):
            self.say(gid, "定稿方案里没写怎么启动，跳过实测截图。")
            sh = {"media": [], "shots": []}
        else:
            self.say(gid, "实测：在合并后的代码上启动项目，打开页面截图。")
            sh = await self.screenshots(g, "run", items, self.wt(gid), run)
        self.put(gid, shots=sh.get("media") or [], run_result={k: sh.get(k) for k in ("up", "error", "no_browser", "fail", "cli_exit")})
        await self.report(gid, sh)

    # ------------------------------------------------------------------ 7. 交付
    async def report(self, gid: str, sh: dict):
        g = self.g.group(gid)
        p = self.state(gid)
        runs = p.get("runs") or []
        checks = "\n\n".join(f"$ {c}\n{B.clip(o, 1500)}" for c, o in runs)
        ok = all(re.search(r"EXIT 0\s*$", o.strip()) for _, o in runs) if runs else None
        rr = p.get("run_result") or {}
        run_note = ("电脑上没有浏览器，没能截图（在电脑上装 chromium 后说「@管家 继续」重拍）。" if rr.get("no_browser") else
                    f"项目没启动起来：{rr.get('error')}" if rr.get("up") is False else
                    f"截了 {len(sh.get('media') or [])} 张图。" if sh.get("media") else (rr.get("error") or ""))
        text = await self.llm(
            "你是 AIW 管家，写项目验收报告。只依据给你的材料，不编造。用中文：先一张 markdown 表（环节、谁做的、结果），"
            "再写：功能完成情况（对照定稿方案）、测试结果、实测情况、遗留问题、返工了几轮。没有的数据写“未知”。",
            f"定稿方案：\n{B.clip(p.get('summary', ''), 5000)}\n\n设计：选了方案 {p.get('pick') or '（无前端）'}\n"
            f"评审返工 {p.get('rework', 0)} 轮，验收返修 {p.get('fixes', 0)} 次\n遗留问题：{p.get('open_issues') or '无'}\n\n"
            f"验收命令输出：\n{checks or '（没有）'}\n\n实测：{run_note}\n\n群聊记录：\n{self.g._context(g, {'bot': ''}, 9000)}", 3000)
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip() or (
            f"验收：{'通过' if ok else '有失败' if ok is False else '没有验收命令'}。{run_note}")
        mb = f"aiw/{gid}/merge"
        self.put(gid, phase="deliver", passed=ok)
        self.g.set_group(gid, status="idle", pending=[], queue=[])
        acts = [{"id": "deliver", "value": "patch", "label": "生成补丁"},
                {"id": "deliver", "value": "push", "label": "推送分支"},
                {"id": "deliver", "value": "merge", "label": f"合入 {g['base']}"}]
        self.say(gid, f"**验收报告**\n\n{text}\n\n{('运行记录：' + chr(10) + checks) if checks else ''}\n\n"
                      f"实测：{run_note}\n\n成果在分支 {mb}，还没推送，{g['base']} 没动。选怎么交付：",
                 {"report": True, "done": True, "media": sh.get("media") or [], "actions": acts})
        await self.g._tell_chat(self.g.group(gid), f"项目做完了，验收{'通过' if ok else '有问题' if ok is False else '完成'}，{run_note} 等你选怎么交付。")

    async def deliver(self, gid: str, how: str):
        g = self.g.group(gid)
        from .bot_groups import qp
        mb, base = f"aiw/{gid}/merge", g["base"]
        R, MW = qp(g["repo"]), qp(self.wt(gid))
        if how == "push":
            cmd = f'git -C {R} push -u origin {shlex.quote(mb)} 2>&1 | tail -n 8; echo "EXIT ${{PIPESTATUS[0]}}"'
        elif how == "merge":
            cmd = (f'B=$(git -C {R} rev-parse --abbrev-ref HEAD); '
                   f'if [ "$B" != {shlex.quote(base)} ]; then echo "你的仓库现在在 $B，不在 {base}，没合。"; echo "EXIT 3"; '
                   f'elif [ -n "$(git -C {R} status --porcelain --untracked-files=no)" ]; then echo "你的仓库有没提交的改动，没合。"; echo "EXIT 4"; '
                   f'else git -C {R} -c user.name=AIW -c user.email=aiw@localhost merge --no-ff --no-edit {shlex.quote(mb)} 2>&1 | tail -n 8; '
                   f'echo "EXIT ${{PIPESTATUS[0]}}"; fi')
        elif how == "patch":
            out = f"~/workspace/.aiw-worktrees/{gid}/aiw-{gid}.patch"
            cmd = (f'git -C {MW} format-patch --stdout {shlex.quote(base)}..{shlex.quote(mb)} > {qp(out)} 2>&1; '
                   f'echo "FILES $(grep -c "^From [0-9a-f]\\{{40\\}}" {qp(out)}) 个提交"; echo "EXIT $?"')
        else:
            raise ValueError("不认识的交付方式")
        try:
            o = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(cmd), 300)
        except Exception as e:  # noqa: BLE001
            o = f"运行失败：{e}\nEXIT 1"
        ok = bool(re.search(r"EXIT 0\s*$", o.strip()))
        body = re.sub(r"\n?EXIT \d+\s*$", "", o.strip())
        data: dict = {"delivered": how if ok else ""}
        if how == "patch" and ok:
            data["media"] = await self.fetch(g, [("补丁", f"~/workspace/.aiw-worktrees/{gid}/aiw-{gid}.patch")])
        names = {"push": "推送分支", "merge": f"合入 {base}", "patch": "生成补丁"}
        self.say(gid, f"{names[how]}：{'完成' if ok else '没成功'}。\n\n```\n{B.clip(body, 1500)}\n```", data)
        if ok:
            self.put(gid, delivered=sorted(set((self.state(gid).get("delivered") or []) + [how])))

    # ------------------------------------------------------------------ what the user types while the project waits
    async def on_text(self, gid: str, text: str) -> bool:
        p = self.state(gid)
        ph, t = p.get("phase"), (text or "").strip()
        if ph == "confirm" and re.fullmatch(r"(开工|开始|开始吧|确认|可以|没问题|好|go|ok)[。.!！]?", t, re.I):
            self.g._bg(self.go(gid))
            return True
        if ph == "pick":
            i = pick_of(t, p.get("designs") or [])
            if i is not None:
                self.g._bg(self.pick(gid, i))
                return True
        if ph == "deliver" and len(t) <= 20:
            how = "patch" if re.search(r"补丁|patch", t, re.I) else "push" if re.search(r"推送|push", t, re.I) else \
                "merge" if re.search(r"合入|合并到|合进", t) else ""
            if how:
                self.g._bg(self.deliver(gid, how))
                return True
        return False

    async def action(self, gid: str, act: str, value: str = "") -> dict:
        p = self.state(gid)
        if act == "go" and p.get("phase") == "confirm":
            self.g._bg(self.go(gid))
        elif act == "pick" and p.get("phase") == "pick":
            i = pick_of(value, p.get("designs") or [])
            if i is None:
                raise ValueError("没有这个方案")
            self.g._bg(self.pick(gid, i))
        elif act == "deliver" and p.get("phase") == "deliver":
            self.g._bg(self.deliver(gid, value))
        else:
            raise ValueError("项目现在不在这一步")
        return {"ok": True}
