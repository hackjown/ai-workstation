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
an "op" (synth / designs / review_check / accept / shots / repush).

v3.9.88 (ideas from t3code): the step in hand survives a restart (Groups.recover); a manager step that fails retries
by itself (30 s, 2 min, 10 min) and a member that fails pauses for 重试 / 跳过; a question can take a reason
(「都不要，重做」 with why, 「改一下方案」); 开 PR writes its title and description from the report, the review
guide and the screenshots, then AIW watches the PR: a failing check or a conflict goes back to its owner, a new
comment is handed out, and the fix is pushed again.
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
MAX_REDESIGN = 3
MAX_CI_FIX = 3
OP_RETRY = (30, 120, 600)          # a manager step that fails runs again after these many seconds
WATCH_EVERY = 120                  # PR check interval
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


GUIDE = """你是 AIW 管家，给用户写一份“审阅导览”：按阅读顺序带他走一遍这次的改动，不用逐行读 diff。
只输出 JSON：{"stops": [{"title": "这一站讲什么", "files": ["相关文件"], "why": "看什么、为什么，一两句", "careful": true/false}],
"mechanical": ["纯机械改动的文件（改名、生成文件、快照、锁文件）"]}
规则：3～8 站，按理解顺序（先核心数据/接口，再逻辑，再界面，最后测试）；careful=true 只给需要人拍板的地方
（安全、数据丢失、接口变化、和定稿方案不一致、为了过测试放宽的地方）；每个改动文件都要出现在某一站或 mechanical 里。用中文。"""

PR_TEXT = """你是 AIW 管家，为这次改动写 GitHub Pull Request 的标题和说明。只依据给你的材料。
只输出 JSON：{"title": "一行标题，不超过 70 字", "body": "markdown 说明：概述、改了什么（按模块）、怎么验证的（列出跑过的命令和结果）、遗留问题"}
用中文。"""

PR_FEEDBACK = """你是 AIW 管家。PR 上有新的评论或评审。判断哪些需要改代码，并交给负责的开发成员。
开发成员和各自负责的步骤：
{owners}
只输出 JSON：{{"issues": [{{"bot": "开发成员id", "fix": "要怎么改，引用评论"}}], "reply": "一句话概括这些评论"}}
只是提问、感谢、无需改代码的评论不要放进 issues。"""


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
              "accept": self.accept, "shots": self.shots, "repush": self.repush}.get(s.get("op") or "")
        if not fn:
            await self.g._run_next(gid)
            return
        try:
            await fn(gid, s)
        except Exception as e:  # noqa: BLE001
            log.exception("project op %s", s.get("op"))
            n = int(s.get("tries") or 0)
            rest = list((self.g.group(gid) or {}).get("queue") or [])
            if n < len(OP_RETRY):   # v3.9.88: a manager step is plain code: it retries on its own
                wait = OP_RETRY[n]
                s2 = {**s, "tries": n + 1}
                self.g.set_group(gid, status="working", pending=[], queue=[s2] + rest, cur=s2)
                self.say(gid, f"管家在「{s.get('step') or s.get('op')}」这一步出错了：{B.clip(e, 300)}。"
                              f"{wait // 60 and f'{wait // 60} 分钟' or f'{wait} 秒'}后自动重试（第 {n + 1}/{len(OP_RETRY)} 次）。")
                self.g._bg(self._retry_later(gid, s2, wait))
                return
            self.g.set_group(gid, status="idle", pending=[], queue=[s] + rest,
                             paused={"bot": MANAGER_ID, "why": "failed", "op": s.get("op")})
            self.say(gid, f"管家在「{s.get('step') or s.get('op')}」这一步出错了：{B.clip(e, 300)}，自动重试 {len(OP_RETRY)} 次都没成。"
                          "处理好后点「重试这一步」，或跳过它。", {"actions": self.g.pause_actions()})

    async def _retry_later(self, gid: str, s: dict, wait: float):
        await asyncio.sleep(wait)
        g = self.g.group(gid)
        q = (g or {}).get("queue") or []
        if g and g["status"] == "working" and q and q[0].get("op") == s.get("op") and q[0].get("tries") == s.get("tries"):
            await self.g._run_next(gid)

    async def synth(self, gid: str, s: dict):
        g = self.g.group(gid)
        p = self.state(gid)
        local = self.local_ids(g)
        mem = "；".join(f"{m['bot']}（{self.title(m['bot'])}{'，云端' if B.is_cloud_engine(m['bot']) else '，本地代码'}）"
                       for m in g["members"])
        out = await self.llm(SYNTH.format(members=mem, local="、".join(local)),
                             f"仓库：{g['repo']}（基线 {g['base']}）\n\n项目规划：\n{p.get('brief')}\n\n"
                             + (f"上一版定稿方案：\n{B.clip(p.get('summary', ''), 5000)}\n\n用户要求这样改（必须照做）：\n- "
                                + "\n- ".join(p.get("feedback") or []) + "\n\n" if p.get("feedback") else "")
                             + f"讨论：\n{self.g._context(g, {'bot': ''}, 14000)}", 4000)
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
            dsteps = self.design_steps(designers, summary)
            self.g.set_group(gid, queue=dsteps + list(self.g.group(gid).get("queue") or []))
            n = len(designers)
            self.say(gid, f"有前端，先请 {'、'.join(self.title(b) for b in designers)} 各出一套设计方案（{n} 套），截图后给你选。")
            await self.g._run_next(gid)
            return
        self.put(gid, phase="confirm")
        self.g.set_group(gid, status="idle", pending=[], queue=[])
        self.say(gid, "方案定了。没问题就点「开工」（或回复“开工”）；要改就点「改一下方案」写上怎么改，我改了再给你看。",
                 {"actions": [{"id": "go", "label": "开工", "desc": "按这个方案分工开发"},
                              {"id": "revise", "label": "改一下方案", "desc": "写上要怎么改，管家改完再给你确认", "reason": True}]})
        await self.g._tell_chat(self.g.group(gid), "项目方案定了，等你确认开工。")

    # ------------------------------------------------------------------ 2. 设计
    def design_steps(self, designers: list, summary: str, why: str = "", prev: Optional[list] = None) -> list:
        redo = ""
        if why:
            was = "；".join(f"方案 {d['label']}（{self.title(d['bot'])}）：{d.get('note', '')}" for d in prev or [])
            redo = (f"【重做】用户看了上一轮的设计都不满意，理由：{why}\n上一轮：{B.clip(was, 1500)}\n"
                    "这一轮必须针对理由改，和上一轮明显不同。\n\n")
        out = []
        for b in designers:
            if B.is_cloud_engine(b):
                t = (redo + "【设计方案】按定稿方案为这个项目的界面出一套视觉设计：主要页面的布局、配色、字体、组件风格，"
                     "并生成 1～3 张主要页面的效果图。不要写代码。\n\n定稿方案：\n" + summary)
            else:
                t = (redo + f"【设计方案，这一步只做设计稿】按定稿方案为项目界面做一份设计稿：{'覆盖' if why else '新建'} design/{b}.html，"
                     "一个自包含的静态页面（样式写在页面里，不引用网络资源和图片 URL，可以用 CSS/SVG），"
                     "把主要页面并排或上下排在这一个文件里，内容用真实感的示例数据。只改 design/ 目录，不写功能代码。"
                     "做完 git add 并 commit。回复里写一句设计思路。\n\n定稿方案：\n" + summary)
            out.append({"bot": b, "task": t, "step": "重做设计方案" if why else "出设计方案", "design": True})
        out.append({"bot": MANAGER_ID, "op": "designs", "task": "截图设计方案给用户选", "step": "截图设计方案"})
        return out

    async def redesign(self, gid: str, why: str):
        p = self.state(gid)
        n = int(p.get("redesigns") or 0) + 1
        self.put(gid, phase="design", redesigns=n, design_why=(p.get("design_why") or []) + [why])
        self.say(gid, f"好，都不要。带着你的理由请 {'、'.join(self.title(b) for b in p.get('designers') or [])} 重出一轮（第 {n}/{MAX_REDESIGN} 轮）。")
        await self.g._start_steps(gid, self.design_steps(p.get("designers") or [], p.get("summary", ""), why, p.get("designs")),
                                  "重做设计")

    async def revise(self, gid: str, why: str):
        p = self.state(gid)
        self.put(gid, phase="discuss", feedback=(p.get("feedback") or []) + [why])
        self.say(gid, "按你的意见改方案。")
        self.g.set_group(gid, queue=[{"bot": MANAGER_ID, "op": "synth", "task": "按用户意见改定稿方案", "step": "改定稿方案"}],
                         status="working", pending=[])
        await self.g._run_next(gid)

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
        acts = [{"id": "pick", "value": o["label"], "label": f"用方案 {o['label']}",
                 "desc": f"{self.title(o['bot'])} 的设计，按它开发"} for o in opts]
        if int(p.get("redesigns") or 0) < MAX_REDESIGN:
            acts.append({"id": "redesign", "label": "都不要，重做", "reason": True,
                         "desc": "写上理由，设计的人带着理由重出一轮"})
        self.say(gid, f"**设计方案，请选一个**\n\n{body}\n\n点下面的按钮，或回复“选 A”；都不满意就回复“都不要，理由是…”。",
                 {"media": media, "actions": acts})
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
        guide = await self.guide(g)
        mb = f"aiw/{gid}/merge"
        self.put(gid, phase="deliver", passed=ok, report=B.clip(text, 8000), guide=guide)
        self.g.set_group(gid, status="idle", pending=[], queue=[])
        acts = [{"id": "deliver", "value": "pr", "label": "开 PR", "desc": "推送分支并开 PR，说明里带验收报告、审阅导览和截图；之后管家盯 CI"},
                {"id": "deliver", "value": "patch", "label": "生成补丁", "desc": "生成 .patch 文件下载，什么都不推送"},
                {"id": "deliver", "value": "push", "label": "推送分支", "desc": f"只推送 {mb}，不开 PR"},
                {"id": "deliver", "value": "merge", "label": f"合入 {g['base']}", "desc": "在电脑上把成果合进基线（仓库要停在基线且干净）"}]
        gmd = self.guide_md(guide)
        self.say(gid, f"**验收报告**\n\n{text}\n\n{('**审阅导览**' + chr(10) + chr(10) + gmd + chr(10) + chr(10)) if gmd else ''}"
                      f"{('运行记录：' + chr(10) + checks) if checks else ''}\n\n"
                      f"实测：{run_note}\n\n成果在分支 {mb}，还没推送，{g['base']} 没动。选怎么交付：",
                 {"report": True, "done": True, "media": sh.get("media") or [], "actions": acts, "guide": guide})
        await self.g._tell_chat(self.g.group(gid), f"项目做完了，验收{'通过' if ok else '有问题' if ok is False else '完成'}，{run_note} 等你选怎么交付。")

    # ------------------------------------------------------------------ 审阅导览
    async def guide(self, g: dict) -> dict:
        from .bot_groups import qp
        mb, base = f"aiw/{g['id']}/merge", g["base"]
        cmd = (f"R={qp(g['repo'])}; git -C \"$R\" diff --stat=200 {shlex.quote(base)}...{shlex.quote(mb)}; echo '=====DIFF'; "
               f"git -C \"$R\" diff {shlex.quote(base)}...{shlex.quote(mb)} | head -c 60000")
        try:
            out = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(cmd), 120)
        except Exception as e:  # noqa: BLE001
            log.warning("guide: %s", e)
            return {}
        if not (out or "").strip():
            return {}
        v = _json(await self.llm(GUIDE, B.clip(out, 64000), 2500)) or {}
        stops = [x for x in (v.get("stops") or []) if isinstance(x, dict) and x.get("title")][:8]
        return {"stops": stops, "mechanical": [str(x) for x in (v.get("mechanical") or [])][:40]} if stops else {}

    @staticmethod
    def guide_md(gd: Optional[dict]) -> str:
        if not gd or not gd.get("stops"):
            return ""
        lines = []
        for i, x in enumerate(gd["stops"], 1):
            lines.append(f"{i}. {'⚠️ **要仔细看** ' if x.get('careful') else ''}**{x.get('title')}**"
                         + (f"（{'、'.join('`' + f + '`' for f in (x.get('files') or [])[:6])}）" if x.get("files") else "")
                         + f"：{x.get('why', '')}")
        if gd.get("mechanical"):
            lines.append(f"{len(gd['stops']) + 1}. 机械改动：{'、'.join('`' + f + '`' for f in gd['mechanical'][:12])}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 开 PR + 盯 PR
    async def pr_text(self, g: dict, p: dict, shots: list) -> tuple:
        runs = "\n".join(f"$ {c}\n{B.clip(o, 600)}" for c, o in p.get("runs") or [])
        v = _json(await self.llm(PR_TEXT, f"项目：{g['title']}\n定稿方案：\n{B.clip(p.get('summary', ''), 4000)}\n\n"
                                          f"验收报告：\n{B.clip(p.get('report', ''), 4000)}\n\n验收命令：\n{runs or '（没有）'}", 2000)) or {}
        title = B.clip(str(v.get("title") or g["title"]).strip().splitlines()[0], 100)
        body = str(v.get("body") or p.get("report") or "").strip()
        gmd = self.guide_md(p.get("guide"))
        if gmd:
            body += "\n\n## 审阅导览\n\n" + gmd
        if shots:
            body += "\n\n## 实际运行截图\n\n" + "\n\n".join(f"**{n}**\n\n![{n}]({u})" for n, u in shots)
        if p.get("pick"):
            body += f"\n\n设计：用户选定方案 {p['pick']}。"
        body += f"\n\n---\n由 AIW 管家开的 PR：评审返工 {p.get('rework', 0)} 轮，验收返修 {p.get('fixes', 0)} 次。"
        return title, body

    async def open_pr(self, g: dict) -> tuple:
        """Push the branch, put the screenshots on a side branch (aiw/<gid>/shots) so the description can show them,
        and open the PR with gh. Returns (ok, text, url)."""
        from .bot_groups import qp
        gid, p = g["id"], self.state(g["id"])
        mb, base, sb = f"aiw/{gid}/merge", g["base"], f"aiw/{gid}/shots"
        R = qp(g["repo"])
        names = [(x.get("label") or x.get("name"), x.get("name")) for x in p.get("shots") or [] if x.get("kind") == "image"]
        shot_dir = qp(self.wt(gid, "_shots/run"))
        pre = (f'set -o pipefail; R={R}; git -C "$R" push -u origin {shlex.quote(mb)} 2>&1 | tail -n 5 || {{ echo "PUSH_FAIL"; exit 0; }}; '
               'U=$(git -C "$R" remote get-url origin); echo "REMOTE $U"; ')
        if names:
            pre += (f'S={shot_dir}; if ls "$S"/*.png >/dev/null 2>&1; then T=$(mktemp -d); cp "$S"/*.png "$T"/; '
                    'git -C "$T" init -q && git -C "$T" checkout -q -b ' + shlex.quote(sb) + ' && git -C "$T" add -A && '
                    'git -C "$T" -c user.name=AIW -c user.email=aiw@localhost commit -q -m "AIW 实测截图" && '
                    f'git -C "$T" push -q -f "$U" {shlex.quote(sb)} 2>&1 | tail -n 2 && echo SHOTS_OK; rm -rf "$T"; fi; ')
        try:
            out = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(pre), 300)
        except Exception as e:  # noqa: BLE001
            return False, f"推送失败：{e}", ""
        if "PUSH_FAIL" in out:
            return False, "推送失败：\n" + B.clip(out, 800), ""
        remote = (re.search(r"^REMOTE (\S+)", out, re.M) or [None, ""])[1]
        gh = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote)
        shots = []
        if gh and "SHOTS_OK" in out:
            shots = [(n, f"https://github.com/{gh[1]}/{gh[2]}/blob/{sb}/{f}?raw=true") for n, f in names]
        title, body = await self.pr_text(g, p, shots)
        import base64
        b64 = base64.b64encode(body.encode()).decode()
        cmd = (f'R={R}; cd "$R" || exit 0; command -v gh >/dev/null || {{ echo NO_GH; exit 0; }}; '
               f'F=$(mktemp); echo {b64} | base64 -d > "$F"; '
               f'U=$(gh pr view {shlex.quote(mb)} --json url -q .url 2>/dev/null); '
               f'if [ -n "$U" ]; then gh pr edit {shlex.quote(mb)} --body-file "$F" >/dev/null 2>&1; echo "PR_URL $U"; '
               f'else gh pr create --base {shlex.quote(base)} --head {shlex.quote(mb)} --title {shlex.quote(title)} --body-file "$F" 2>&1 | tail -n 3; fi; rm -f "$F"')
        try:
            o2 = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(cmd), 120)
        except Exception as e:  # noqa: BLE001
            o2 = f"gh 运行失败：{e}"
        url = (re.search(r"https://\S+/pull/\d+", o2 or "") or [""])[0]
        if url:
            return True, f"PR：{url}\n标题：{title}" + ("（说明里带了截图）" if shots else ""), url
        cmp = f"https://github.com/{gh[1]}/{gh[2]}/compare/{base}...{mb}?expand=1" if gh else ""
        why = "电脑上没装 GitHub CLI（gh），" if "NO_GH" in (o2 or "") else f"gh 没开成：{B.clip(o2, 400)}\n"
        return False, f"分支已推送，但 PR 没开成：{why}装好并 `gh auth login` 后再点「开 PR」" + (f"，或在这里手动开：{cmp}" if cmp else ""), ""

    def watch(self, gid: str):
        tasks = self.__dict__.setdefault("_watch", {})
        t = tasks.get(gid)
        if t and not t.done():
            return
        tasks[gid] = self.g._bg(self._watch_loop(gid))

    def unwatch(self, gid: str):
        t = self.__dict__.get("_watch", {}).pop(gid, None)
        if t and not t.done():
            t.cancel()
        p = self.state(gid)
        if (p.get("watch") or {}).get("on"):
            self.put(gid, watch={**p["watch"], "on": False})

    async def _watch_loop(self, gid: str):
        while True:
            await asyncio.sleep(WATCH_EVERY)
            try:
                if not await self.watch_tick(gid):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("pr watch %s: %s", gid, e)

    async def pr_state(self, g: dict) -> Optional[dict]:
        from .bot_groups import qp
        mb = f"aiw/{g['id']}/merge"
        cmd = (f'cd {qp(g["repo"])} && echo "=====VIEW" && gh pr view {shlex.quote(mb)} --json state,mergeable,headRefOid,comments,reviews,url && '
               f'echo "=====CHECKS" && gh pr checks {shlex.quote(mb)} --json name,bucket,link 2>/dev/null; echo "=====ME"; gh api user -q .login 2>/dev/null')
        out = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(cmd), 90)
        if "=====VIEW" not in (out or ""):
            return None
        view = _json(out.split("=====VIEW", 1)[1].split("=====CHECKS", 1)[0])
        if not view:
            return None
        ck = out.split("=====CHECKS", 1)[1].split("=====ME", 1)[0].strip() if "=====CHECKS" in out else ""
        try:
            checks = json.loads(ck) if ck.startswith("[") else []
        except ValueError:
            checks = []
        me = out.split("=====ME", 1)[1].strip() if "=====ME" in out else ""
        return {**view, "checks": checks, "me": me}

    async def watch_tick(self, gid: str) -> bool:
        """One look at the PR. Returns False when watching is over."""
        g = self.g.group(gid)
        p = self.state(gid)
        w = dict(p.get("watch") or {})
        if not g or not w.get("on"):
            return False
        if g["status"] != "idle":          # a fix is under way; look again later
            return True
        st = await self.pr_state(g)
        if not st:
            w["fails"] = int(w.get("fails") or 0) + 1
            self.put(gid, watch=w)
            if w["fails"] >= 8:
                self.say(gid, "连续 8 次读不到 PR 状态，不再盯了。")
                self.unwatch(gid)
                return False
            return True
        w["fails"] = 0
        if st.get("state") in ("MERGED", "CLOSED"):
            self.say(gid, f"PR {'已合并' if st['state'] == 'MERGED' else '已关闭'}，不再盯了。")
            await self.g._tell_chat(g, f"项目的 PR {'已合并' if st['state'] == 'MERGED' else '已关闭'}。")
            self.put(gid, watch={**w, "on": False})
            return False
        head = st.get("headRefOid") or ""
        owners = [x["bot"] for x in p.get("steps") or []] or self.local_ids(g)
        failed = [c for c in st.get("checks") or [] if c.get("bucket") == "fail"]
        pending = [c for c in st.get("checks") or [] if c.get("bucket") == "pending"]
        if failed and w.get("ci_sha") != head:
            w["ci_sha"] = head
            if int(w.get("ci_fixes") or 0) >= MAX_CI_FIX:
                self.put(gid, watch=w)
                self.say(gid, f"CI 还有 {len(failed)} 项失败，已经自动修了 {MAX_CI_FIX} 次，交给你看：{st.get('url')}")
                await self.g._tell_chat(g, "项目 PR 的 CI 还是红的，需要你看一下。")
                return True
            log_out = await self.ci_log(g)
            v = _json(await self.llm(ACCEPT_FIX.format(owners=self.owners(p)),
                                     "CI 失败：" + "、".join(c.get("name", "") for c in failed) + "\n\n" + log_out, 800)) or {}
            bot = v.get("bot") if v.get("bot") in owners else owners[-1]
            w["ci_fixes"] = int(w.get("ci_fixes") or 0) + 1
            self.put(gid, watch=w, phase="ci")
            self.say(gid, f"PR 的 CI 红了（{'、'.join(c.get('name', '') for c in failed[:5])}），交给 {self.title(bot)} 修（第 {w['ci_fixes']}/{MAX_CI_FIX} 次）。")
            await self.g._start_steps(gid, [
                {"bot": bot, "task": f"【CI 修复】PR 的 CI 失败了。{v.get('fix') or ''}\n先执行 git merge aiw/{gid}/merge 拿到最新代码，"
                                     f"修好、本地跑通相关测试、commit。\n\nCI 失败日志：\n{log_out}", "step": "修 CI"},
                {"bot": MANAGER_ID, "op": "repush", "task": "合并并推送到 PR", "step": "推送修复"}], "")
            return True
        if st.get("mergeable") == "CONFLICTING" and w.get("conflict_sha") != head:
            w["conflict_sha"] = head
            bot = owners[-1]
            self.put(gid, watch=w, phase="ci")
            self.say(gid, f"PR 和 {g['base']} 冲突了，交给 {self.title(bot)} 合并解决。")
            await self.g._start_steps(gid, [
                {"bot": bot, "task": f"【解决 PR 冲突】先执行 git merge aiw/{gid}/merge，再 git fetch origin && git merge origin/{g['base']}，"
                                     "解决冲突，跑通测试，commit。", "step": "解决 PR 冲突"},
                {"bot": MANAGER_ID, "op": "repush", "task": "合并并推送到 PR", "step": "推送修复"}], "")
            return True
        notes = [c for c in st.get("comments") or [] if (c.get("author") or {}).get("login") not in ("", None, st.get("me"))]
        notes += [r for r in st.get("reviews") or [] if (r.get("author") or {}).get("login") not in ("", None, st.get("me")) and r.get("body")]
        seen = int(w.get("seen") or 0)
        if len(notes) > seen:
            new = notes[seen:]
            w["seen"] = len(notes)
            w["quiet"] = int(w.get("quiet") or 0) + 1
            text = "\n".join(f"- {(c.get('author') or {}).get('login')}：{B.clip(c.get('body'), 400)}" for c in new)
            v = _json(await self.llm(PR_FEEDBACK.format(owners=self.owners(p)), text, 1200)) or {}
            issues = [i for i in (v.get("issues") or []) if isinstance(i, dict) and i.get("bot") in owners and i.get("fix")]
            self.put(gid, watch=w)
            self.say(gid, f"PR 有 {len(new)} 条新评论：\n{text}" + ("" if issues else "\n\n不需要改代码。"))
            if issues:
                by = {}
                for i in issues:
                    by.setdefault(i["bot"], []).append(i["fix"])
                self.put(gid, phase="ci")
                await self.g._start_steps(gid, [{"bot": b, "task": f"【PR 评论】先执行 git merge aiw/{gid}/merge，然后按评论改：\n- "
                                                 + "\n- ".join(f) + "\n跑通测试后 commit。", "step": "按 PR 评论改"} for b, f in by.items()]
                                          + [{"bot": MANAGER_ID, "op": "repush", "task": "合并并推送到 PR", "step": "推送修复"}], "")
            if w["quiet"] >= 10:
                self.say(gid, "连续 10 次只有评论，不再盯了。")
                self.unwatch(gid)
                return False
            return True
        if not failed and not pending and st.get("checks") and w.get("green_sha") != head:
            w["green_sha"] = head
            self.put(gid, watch=w)
            self.say(gid, f"PR 的 CI 全部通过（{len(st['checks'])} 项）。{st.get('url') or ''}")
            await self.g._tell_chat(g, "项目 PR 的 CI 全绿了，等你合并。")
        else:
            self.put(gid, watch=w)
        return True

    async def ci_log(self, g: dict) -> str:
        from .bot_groups import qp
        mb = f"aiw/{g['id']}/merge"
        cmd = (f'cd {qp(g["repo"])} && I=$(gh run list --branch {shlex.quote(mb)} --limit 5 --json databaseId,conclusion '
               "-q '[.[] | select(.conclusion==\"failure\")][0].databaseId' 2>/dev/null); "
               '[ -n "$I" ] && gh run view "$I" --log-failed 2>/dev/null | tail -n 120')
        try:
            return B.clip(await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(cmd), 120), 6000) or "（没拿到日志）"
        except Exception as e:  # noqa: BLE001
            return f"（没拿到日志：{e}）"

    async def repush(self, gid: str, s: dict):
        from .bot_groups import qp
        r = await self.g.merge(gid)
        g = self.g.group(gid)
        if g["status"] == "fixing":
            self.g.set_group(gid, queue=[s] + list(g.get("queue") or []))
            return
        if r.get("error"):
            raise RuntimeError("合并没做成：" + str(r["error"]))
        out = await self.g._sh(g["machine_id"], "bash -c " + shlex.quote(
            f'git -C {qp(g["repo"])} push origin aiw/{gid}/merge 2>&1 | tail -n 4; echo "EXIT ${{PIPESTATUS[0]}}"'), 300)
        if not re.search(r"EXIT 0\s*$", out.strip()):
            raise RuntimeError("推送失败：" + B.clip(out, 400))
        self.put(gid, phase="deliver")
        self.g.set_group(gid, status="idle", pending=[], queue=[])
        self.say(gid, "修复已推送到 PR，接着盯 CI。")

    async def deliver(self, gid: str, how: str):
        g = self.g.group(gid)
        if how == "pr":
            ok, text, url = await self.open_pr(g)
            self.say(gid, f"开 PR：{'完成' if ok else '没成功'}。\n\n{text}", {"delivered": "pr" if ok else "", **({"pr": url} if url else {})})
            if ok:
                p = self.state(gid)
                self.put(gid, delivered=sorted(set((p.get("delivered") or []) + ["push", "pr"])), pr=url,
                         watch={"on": True, "url": url, "since": time.time()})
                self.say(gid, "开始盯这个 PR：CI 失败、分支冲突、有人评论时，管家交回给负责的成员处理并重新推送。")
                self.watch(gid)
            return
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
        m = re.match(r"^(?:都不要|都不行|重新设计|重做设计)[，,。:：\s]*(?:理由(?:是)?[:：]?)?\s*(.*)$", t, re.S)
        if ph == "pick" and m:
            if not m.group(1).strip():
                self.say(gid, "说一下为什么都不要（比如“太花了，要简洁的深色风格”），设计的人才知道往哪改。")
                return True
            if int(p.get("redesigns") or 0) >= MAX_REDESIGN:
                self.say(gid, f"已经重做了 {MAX_REDESIGN} 轮，请从现有方案里选一个，或者直接说想要的样子，选定后再改。")
                return True
            self.g._bg(self.redesign(gid, m.group(1).strip()))
            return True
        if ph == "confirm" and len(t) > 4 and not re.fullmatch(r"(开工|开始|开始吧|确认|可以|没问题|好|go|ok)[。.!！]?", t, re.I):
            self.g._bg(self.revise(gid, t))
            return True
        if ph == "pick":
            i = pick_of(t, p.get("designs") or [])
            if i is not None:
                self.g._bg(self.pick(gid, i))
                return True
        if ph == "deliver" and len(t) <= 20:
            how = "pr" if re.search(r"开\s*PR|pull request|提\s*PR", t, re.I) else \
                "patch" if re.search(r"补丁|patch", t, re.I) else "push" if re.search(r"推送|push", t, re.I) else \
                "merge" if re.search(r"合入|合并到|合进", t) else ""
            if how:
                self.g._bg(self.deliver(gid, how))
                return True
        return False

    async def action(self, gid: str, act: str, value: str = "", reason: str = "") -> dict:
        p = self.state(gid)
        reason = (reason or "").strip()
        if act in ("redesign", "revise") and not reason:
            raise ValueError("这个选项要写理由")
        if act == "go" and p.get("phase") == "confirm":
            self.g._bg(self.go(gid))
        elif act == "revise" and p.get("phase") == "confirm":
            self.g._bg(self.revise(gid, reason))
        elif act == "redesign" and p.get("phase") == "pick":
            if int(p.get("redesigns") or 0) >= MAX_REDESIGN:
                raise ValueError(f"已经重做了 {MAX_REDESIGN} 轮")
            self.g._bg(self.redesign(gid, reason))
        elif act == "pick" and p.get("phase") == "pick":
            i = pick_of(value, p.get("designs") or [])
            if i is None:
                raise ValueError("没有这个方案")
            self.g._bg(self.pick(gid, i))
        elif act == "deliver" and p.get("phase") == "deliver":
            if value not in ("pr", "patch", "push", "merge"):
                raise ValueError("不认识的交付方式")
            self.g._bg(self.deliver(gid, value))
        else:
            raise ValueError("项目现在不在这一步")
        return {"ok": True}
