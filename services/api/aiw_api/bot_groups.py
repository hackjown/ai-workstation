"""v3.9.61 引擎群聊 and 派活: several engines on one project, and the main chat handing work to an engine.

A group (bot_groups) is one git repository on one computer and a few engines (agy, Codex, Claude Code, CodeBuddy,
Grok Build). Each member is an ordinary engine conversation (bots.Bots, bot_convs.grp = the group) working in its own
git worktree on its own branch, aiw/<group>/<engine>, made from the base branch. The group's timeline
(bot_group_messages) shows what the user and AIW 管家 said and every member's reply, mirrored from its conversation.

  @codex 做 X        goes to that member (several @ go to each of them)
  @管家 / no @        AIW 管家 splits the request into one task per member (an LLM call; when it fails, everyone gets
                     the request as it is) and sends each its part
  合并 (merge)        when every member that got work has replied, AIW 管家 commits what each left uncommitted in its
                     worktree and merges the branches into aiw/<group>/merge, in its own worktree made from the base.
                     Nothing is pushed, and the base branch and the user's checkout are never touched. A conflict is
                     handed back to that member: it merges aiw/<group>/merge into its own branch, resolves, commits,
                     and the merge runs again (at most MAX_FIX times)

The main chat (chat.Chat.send) hands work out through Dispatch: "让 codex 修一下登录" starts a Codex conversation,
"让 codex 和 claude 一起做 …" starts a group. When the engine (or the group's merge) is done, the result comes back
to the main chat as a message that opens the conversation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
import uuid
from typing import Optional

from . import bots as B

log = logging.getLogger("aiw.bot_groups")

MAX_FIX = 2
ALIASES = [   # (engine, names the user types), longest first inside each
    ("agy", ("antigravity", "反重力", "agy")),
    ("codex", ("codex",)),
    ("claude", ("claude code", "claudecode", "claude", "clade", "克劳德")),
    ("codebuddy", ("codebuddy", "workbuddy", "cbc")),
    ("grok", ("grok build", "grok")),
    ("muse", ("meta muse", "muse")),
    ("cue", ("cue by manus", "manus cue", "cue")),
]
ALL_WORDS = ("所有引擎", "全部引擎", "各个引擎", "所有 agent", "全部 agent", "大家")
MANAGER_WORDS = ("管家", "aiw")

# v3.9.84: 「让 agy 改代码」 without @ — the user wants that engine to do it. AIW must not do it itself; it offers a
# one-tap 「交给 @agy」 instead. A question about an engine (「agy 进度怎样」「查一下 agy 的任务」) is not this.
_EXEC_LEAD = r"(?:让|叫|请|交给|派给|派|给|用|找|由)"
_EXEC_VERB = r"(?:改|修|做|写|实现|开发|执行|跑|重构|加上?|删|优化|部署|按照|按|照着|继续|完成|处理|搞|弄)"


def implied_engine(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t or re.match(LEAD, t) or len(t) > 400:
        return None
    low = t.lower()
    if re.search(r"(查|看看?|问问?|进度|状态|怎么样|在做什么|做到哪)", low) and not re.search(r"(改|修|写|实现|重构|开发)", low):
        return None
    for eng, names in ALIASES:
        for n in names:
            if re.search(_EXEC_LEAD + r"\s*" + re.escape(n) + r"(?![a-z0-9])\s*.{0,40}?" + _EXEC_VERB, low):
                return eng
    return None

PLAN = """你是 AIW 管家，带一组 Agent 在群里协同工作：本地编程 Agent（agy、Codex、Claude Code、CodeBuddy、Grok）各自在独立的 git 分支和工作文件夹里写代码；云端 Agent（Meta Muse、Cue）只负责讨论、调研、方案设计、评审或资料汇总，绝不操作本地代码。
成员：{members}
根据用户的话分工。只输出一个 JSON 对象，不要别的文字：
{{"assign": {{"成员id": "发给它的具体任务", ...}}, "report": "一句话告诉用户怎么分的工"}}
要按顺序做（后一步要用前一步的结果）时，改用：
{{"steps": [{{"bot": "成员id 或 管家", "task": "这一步的具体任务"}}, ...], "report": "..."}}
如果用户明确要求管家自己做（不要转派给其他引擎，例如找工作区截图发到群里、直接总结/查询/回答），改用：
{{"self": true, "task": "管家自己要做的具体事", "report": ""}}
规则：
- 用户明确说让管家自己做、不要让别人做时，必须输出 {{"self": true, ...}}，绝不许派给群里成员。
- 用户已经点名谁做哪一步时，严格照用户的分配和顺序，不许换人、不许合并步骤；用户交给管家的步骤写 "bot": "管家"。
- 用户说“前一步完成后再交给下一步”或步骤之间有依赖时，必须用 steps。
- 成员id 只能用上面列出的（{ids}）；用不上的成员不要分。
- 写代码、改文件、修 bug、跑测试、以及任何需要读取/检查/验收本地路径或本地文件的任务，只能分给本地编程成员（标有“本地代码”），绝不能分给云端成员（Meta Muse / Cue）——云端成员无法访问本地磁盘。
- 给本地编程成员的任务要能独立完成，尽量改不同的文件，减少合并冲突；写清要改什么、怎么验证（跑什么测试或命令）。
- 给云端成员（标有“云端”）的任务只限纯文字讨论、调研、方案对比、创意出图、或基于群聊记录里已有文字做评审，绝不要让它读取本地路径、验收本地文件、写代码或跑本地命令。
- 任务本身不可拆时，交给最合适的一个成员，或者让两个成员各做一版供比较（在 report 里说明）。
- 写规范/验收标准的步骤要把口径一次钉死：输入范围（字符集、文件类型、要跳过的目录）、边界情况、输出格式、退出码、警告/错误的格式；没写清的地方不许留给实现的人自己决定。
- 每一步只做自己那份：实现的人只写实现，不改规范和测试；规范或测试有问题时，加一步交回给负责它的成员。
- 用中文。不许让 Agent push、切分支、合并别人的分支、删用户文件。"""


def _now() -> float:
    return time.time()


def ensure_tables(store) -> None:
    store._x("CREATE TABLE IF NOT EXISTS bot_groups (id TEXT PRIMARY KEY, title TEXT, machine_id TEXT, repo TEXT, "
             "base TEXT, status TEXT, auto_merge INTEGER DEFAULT 1, members TEXT, pending TEXT, fixes INTEGER DEFAULT 0, "
             "origin TEXT, merge TEXT, created_at REAL, updated_at REAL)")
    store._x("CREATE TABLE IF NOT EXISTS bot_group_messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, "
             "grp TEXT, role TEXT, who TEXT, text TEXT, data TEXT, src TEXT, created_at REAL)")
    store._x("CREATE INDEX IF NOT EXISTS ix_botgm_grp ON bot_group_messages(grp, seq)")
    for col in ("queue TEXT", "project TEXT", "cur TEXT", "paused TEXT", "read_seq INTEGER DEFAULT -1"):
        try:
            store._x(f"ALTER TABLE bot_groups ADD COLUMN {col}")
        except Exception:  # noqa: BLE001
            pass


def qp(path: str) -> str:
    """A computer path for bash: ~ expands, the rest is quoted."""
    path = (path or "").strip()
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def ok_path(p: str) -> bool:
    return bool(re.fullmatch(r"[~\w./ \-\u4e00-\u9fff]{1,200}", p or "")) and ".." not in p.split("/")


def ok_ref(r: str) -> bool:
    return bool(re.fullmatch(r"[\w./\-]{1,100}", r or "")) and ".." not in r and not r.startswith(("-", "/"))


def mentions(text: str, ids: list) -> tuple[list, bool, str]:
    """(@members, @管家, the text without the @s)."""
    found, manager = [], False

    def sub(m):
        nonlocal manager
        name = m.group(1).lower()
        if name in ("管家", "aiw", "all", "所有人", "大家"):
            manager = manager or name in ("管家", "aiw")
            if name in ("all", "所有人", "大家"):
                found.extend(i for i in ids if i not in found)
            return ""
        for eng, names in ALIASES:
            if name in names and eng in ids:
                if eng not in found:
                    found.append(eng)
                return ""
        return m.group(0)
    rest = re.sub(r"@\s?(claude code|grok build|meta muse|cue by manus|manus cue|[\w\u4e00-\u9fff]+)", sub, text or "", flags=re.I)
    return found, manager, re.sub(r"[ \t]{2,}", " ", rest).strip()


MANAGER_ID = "管家"
STEP_RE = re.compile(r"^\s*(?:第\s*[\d一二三四五六七八九十]+\s*步|步骤\s*\d+|step\s*\d+|\d{1,2}\s*[.、)）])", re.I)
RULES_RE = re.compile(r"^\s*(?:规则|要求|注意|约束)\s*[:：]")


def _owners(line: str, ids: list) -> list[str]:
    """All @member (or @管家) mentions on a step line, in order, deduplicated."""
    out: list[str] = []
    for m in re.finditer(r"@\s?(claude code|grok build|meta muse|cue by manus|manus cue|[\w\u4e00-\u9fff]+)", line, re.I):
        name = m.group(1).lower()
        who = None
        if name in MANAGER_WORDS:
            who = MANAGER_ID
        else:
            for eng, names in ALIASES:
                if name in names and eng in ids:
                    who = eng
                    break
        if who and who not in out:
            out.append(who)
    return out


def _owner(line: str, ids: list) -> Optional[str]:
    """Who a step line names: the first @member (or @管家) on it."""
    xs = _owners(line, ids)
    return xs[0] if xs else None


_LOCAL_TASK_RE = re.compile(
    r"(?:~/|/(?:home|Users|root|var|tmp|opt)/|worktree|本地路径|本地文件|本地目录|本地运行|本地测试|"
    r"工作区|工作目录|仓库里|分支里|查看.*文件|读取.*文件|检查.*路径|验收.*路径|验收.*文件|验收.*代码|"
    r"\b(?:git|pytest|npm|node|python3?|docker|bash)\b)",
    re.I,
)


def _needs_local_access(task: str) -> bool:
    return bool(_LOCAL_TASK_RE.search(task or ""))


def _reroute_cloud_local_plan(plan: Optional[dict], ids: list[str]) -> Optional[dict]:
    """Prevent 管家 from assigning local-filesystem / local-verification tasks to cloud engines (muse/cue)."""
    if not plan:
        return plan
    locals_ = [b for b in ids if not B.is_cloud_engine(b)]
    if not locals_:
        return plan

    def pick_local(avoid: str = "") -> str:
        for b in locals_:
            if b != avoid:
                return b
        return locals_[0]

    if plan.get("steps"):
        prev = ""
        fixed = []
        for s in plan["steps"]:
            bot = s.get("bot") or ""
            task = s.get("task") or ""
            if B.is_cloud_engine(bot) and _needs_local_access(task):
                bot = pick_local(prev)
                s = {**s, "bot": bot}
            fixed.append(s)
            if bot != MANAGER_ID:
                prev = bot
        plan = {**plan, "steps": fixed}
    elif plan.get("assign"):
        new_assign: dict[str, str] = {}
        for bot, task in plan["assign"].items():
            target = pick_local() if (B.is_cloud_engine(bot) and _needs_local_access(task)) else bot
            if target in new_assign:
                new_assign[target] = new_assign[target] + "\n\n" + task
            else:
                new_assign[target] = task
        plan = {**plan, "assign": new_assign}
    return plan


def parse_steps(text: str, ids: list) -> Optional[list]:
    """v3.9.79: a message that already says who does which step ("第 1 步 @cue …") is followed as written, in order,
    instead of letting the LLM re-split it. Each step gets the goal (the text before the first step) and the rules
    (a trailing 规则： paragraph). None when fewer than two steps are spelled out."""
    lines = (text or "").splitlines()
    starts = [i for i, ln in enumerate(lines) if STEP_RE.match(ln) and _owner(ln, ids)]
    if len(starts) < 2:
        return None
    end = len(lines)
    for i in range(starts[-1] + 1, len(lines)):
        if RULES_RE.match(lines[i]):
            end = i
            break
    goal = "\n".join(lines[:starts[0]]).strip()
    goal = re.sub(r"@\s?(管家|aiw)\b", "", goal, flags=re.I).strip()
    rules = "\n".join(lines[end:]).strip()
    steps = []
    for k, a in enumerate(starts):
        b = starts[k + 1] if k + 1 < len(starts) else end
        body = "\n".join(lines[a:b]).strip()
        whos = _owners(lines[a], ids)
        task = "\n\n".join(x for x in (f"总目标：{goal}" if goal else "", f"你负责的是这一步：\n{body}",
                                         rules) if x)
        for who in whos:
            steps.append({"bot": who, "task": task, "step": body})
    return steps


def parse_plan(out: str, ids: list) -> Optional[dict]:
    t = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    report = str(d.get("report") or "").strip()
    if d.get("self"):
        return {"self": True, "task": str(d.get("task") or "").strip(), "report": report, "assign": {}}
    st = d.get("steps")
    if isinstance(st, list):
        steps = []
        for x in st:
            if not isinstance(x, dict):
                continue
            who = str(x.get("bot") or x.get("who") or "").strip()
            who = MANAGER_ID if who.lower() in MANAGER_WORDS or who == MANAGER_ID else who
            task = str(x.get("task") or "").strip()
            if task and (who in ids or who == MANAGER_ID):
                steps.append({"bot": who, "task": task, "step": task})
        if steps:
            return {"assign": {}, "steps": steps, "report": report}
    a = d.get("assign")
    if not isinstance(a, dict):
        return None
    assign = {k: str(v).strip() for k, v in a.items() if k in ids and str(v or "").strip()}
    return {"assign": assign, "report": report} if assign else None


def parse_merge(out: str) -> dict:
    r = {"merged": [], "conflicts": {}, "empty": [], "committed": [], "error": "", "log": "", "stat": "", "ok": False}
    for line in (out or "").splitlines():
        w = line.split(" ", 2)
        if w[0] == "MERGED" and len(w) > 1:
            r["merged"].append(w[1])
        elif w[0] == "CONFLICT" and len(w) > 1:
            r["conflicts"][w[1]] = [f for f in (w[2] if len(w) > 2 else "").split(",") if f]
        elif w[0] == "EMPTY" and len(w) > 1:
            r["empty"].append(w[1])
        elif w[0] == "COMMITTED" and len(w) > 1:
            r["committed"].append(w[1])
        elif w[0] == "LOG":
            r["log"] += line[4:] + "\n"
        elif w[0] == "STAT":
            r["stat"] = line[5:]
        elif w[0] == "MERGE_ERR":
            r["error"] = line[10:]
        elif w[0] == "MERGE_DONE":
            r["ok"] = True
    return r


MANAGER_SELF_RE = re.compile(
    r"(?:不要|别|不用|无需)(?:让别人|让其他|派给|交给|找别人|叫别人|转派)"
    r"|(?:你|管家)自己(?:来)?做|你亲自|由你(?:自己)?(?:来)?做|你自己(?:找|发|查|看|处理|搞定)"
)


def is_manager_self_task(text: str, manager: bool) -> bool:
    return bool(manager and MANAGER_SELF_RE.search(text or ""))


def mentioned_engines(text: str, ids: list) -> list[str]:
    low = (text or "").lower()
    hits: list[str] = []
    for eng, names in ALIASES:
        if eng not in ids:
            continue
        if any(re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low) for n in names):
            hits.append(eng)
    return hits


class Groups:
    def __init__(self, bots: "B.Bots", chat=None):
        self.bots, self.store, self.chat = bots, bots.store, chat
        ensure_tables(self.store)
        self.listeners: list = []
        self._merging: set = set()
        self._tasks: set = set()
        bots.listeners.append(self._on_bot)
        from .group_project import Project
        self.project = Project(self)

    # ------------------------------------------------------------------ storage
    def _row(self, r: dict) -> dict:
        d = dict(r)
        d["members"] = B._j(d.get("members"), [])
        d["pending"] = B._j(d.get("pending"), [])
        d["merge"] = B._j(d.get("merge"), None)
        d["queue"] = B._j(d.get("queue"), [])
        d["project"] = B._j(d.get("project"), None)
        d["cur"] = B._j(d.get("cur"), None)
        d["paused"] = B._j(d.get("paused"), None)
        d["auto_merge"] = bool(d.get("auto_merge"))
        return d

    def _member_status(self, m: dict, pending: list) -> dict:
        bot = m.get("bot") or ""
        cid = m.get("conv") or ""
        busy = bool(cid and self.bots.busy(cid))
        state, label, activity = "ok", "空闲", ""
        if busy:
            state, label = "busy", "干活中"
            last = self.bots.messages(cid, limit=1)
            if last and last[-1].get("role") == "bot":
                d = last[-1].get("data") or {}
                said = [x for x in (d.get("said") or []) if x]
                steps = d.get("steps") or []
                if said:
                    activity = B.clip(said[-1], 60)
                elif steps:
                    st_last = steps[-1]
                    activity = B.clip(f"{st_last.get('tool') or ''} {st_last.get('arg') or ''}".strip(), 60)
        elif bot in pending:
            state, label = "busy", "等待回复"
        else:
            last_bot = next((x for x in reversed(self.bots.messages(cid, limit=6)) if x.get("role") == "bot"), None) if cid else None
            last_st = ((last_bot or {}).get("data") or {}).get("status") if last_bot else ""
            if last_st == "blocked":
                state, label = "error", "被权限拦住"
            elif last_st in ("failed", "turn_failed", "error"):
                state, label = "error", "出错/断线"
            else:
                auth = self.bots.auth_store.public_view(bot) if B.is_cloud_engine(bot) else None
                rd, why = self.bots.ready_state(bot, auth)
                if rd == "off":
                    state, label = "off", why or "未登录/离线"
                elif rd == "ok":
                    state, label = "ok", "空闲"
                else:
                    state, label = ("unknown" if B.is_cloud_engine(bot) else "ok"), ("待确认" if B.is_cloud_engine(bot) else "空闲")
        return {**m, "busy": busy, "state": state, "state_label": label, **({"activity": activity} if activity else {})}

    def unread_count(self, gid: str, read_seq: Optional[int] = None) -> int:
        if read_seq is None:
            r = self.store._x("SELECT read_seq FROM bot_groups WHERE id=?", (gid,))
            read_seq = r[0]["read_seq"] if r else None
        if read_seq is None or int(read_seq) < 0:
            return 0
        r = self.store._x(
            "SELECT COUNT(*) AS n FROM bot_group_messages WHERE grp=? AND role IN ('bot','aiw') AND seq>?",
            (gid, int(read_seq)),
        )
        return int(r[0]["n"]) if r else 0

    def mark_read(self, gid: str) -> None:
        r = self.store._x("SELECT COALESCE(MAX(seq), 0) AS s FROM bot_group_messages WHERE grp=?", (gid,))
        seq = int(r[0]["s"]) if r else 0
        self.store._x("UPDATE bot_groups SET read_seq=? WHERE id=?", (seq, gid))

    def group(self, gid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM bot_groups WHERE id=?", (gid,))
        if not r:
            return None
        g = self._row(r[0])
        g["members"] = [self._member_status(m, g["pending"]) for m in g["members"]]
        g["busy"] = any(m.get("busy") for m in g["members"]) or gid in self._merging
        g["needs"] = self.needs(g)
        g["unread"] = self.unread_count(gid, g.get("read_seq"))
        return g

    def needs(self, g: dict) -> str:
        """v3.9.88 需要你: what this group waits on the user for, in a few words ("" when nothing)."""
        if g.get("paused"):
            return "处理没做成的一步" if g["paused"].get("why") != "blocked" else "允许被拦的命令"
        p = g.get("project") or {}
        return {"confirm": "确认方案", "pick": "选设计方案",
                "deliver": "" if p.get("delivered") else "选怎么交付"}.get(p.get("phase") or "", "")

    def needs_list(self) -> list:
        out = []
        for g in self.groups():
            if g.get("needs"):
                out.append({"id": g["id"], "title": g["title"], "needs": g["needs"], "since": g.get("updated_at")})
        return out

    def groups(self) -> list[dict]:
        out = []
        for r in self.store._x("SELECT * FROM bot_groups ORDER BY updated_at DESC"):
            g = self.group(r["id"])
            last = self.store._x("SELECT who, role, text FROM bot_group_messages WHERE grp=? ORDER BY seq DESC LIMIT 1",
                                 (g["id"],))
            g["last"] = {"who": last[0]["who"], "role": last[0]["role"], "text": B.clip(last[0]["text"], 80)} if last else None
            out.append(g)
        return out

    def set_group(self, gid: str, **kw) -> Optional[dict]:
        for k in ("members", "pending", "merge", "queue", "project", "cur", "paused"):
            if k in kw:
                kw[k] = json.dumps(kw[k], ensure_ascii=False) if kw[k] is not None else None
        kw["updated_at"] = _now()
        self.store._x(f"UPDATE bot_groups SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), gid))
        g = self.group(gid)
        self._emit("group", g)
        return g

    def patch_msg(self, mid: str, **data):
        """v3.9.88: add keys to a group message's data (the turn's diff range)."""
        r = self.store._x("SELECT * FROM bot_group_messages WHERE id=?", (mid,))
        if not r:
            return None
        d = {**B._j(r[0]["data"], {}), **data}
        self.store._x("UPDATE bot_group_messages SET data=? WHERE id=?", (json.dumps(d, ensure_ascii=False), mid))
        m = B.msg_row(self.store._x("SELECT * FROM bot_group_messages WHERE id=?", (mid,))[0])
        self._emit("group_message", m)
        return m

    def messages(self, gid: str, after: int = 0, limit: int = 300) -> list[dict]:
        rows = self.store._x("SELECT * FROM bot_group_messages WHERE grp=? AND seq>? ORDER BY seq DESC LIMIT ?",
                             (gid, int(after or 0), int(limit)))
        return [B.msg_row(r) for r in reversed(rows)]

    def add(self, gid: str, role: str, who: str, text: str, data: Optional[dict] = None, src: str = "") -> dict:
        if role in ("bot", "aiw"):
            r = self.store._x("SELECT read_seq FROM bot_groups WHERE id=?", (gid,))
            if r and (r[0]["read_seq"] is None or int(r[0]["read_seq"]) < 0):
                cur = self.store._x("SELECT COALESCE(MAX(seq), 0) AS s FROM bot_group_messages WHERE grp=?", (gid,))
                self.store._x("UPDATE bot_groups SET read_seq=? WHERE id=?", (int(cur[0]["s"]) if cur else 0, gid))
        mid = f"gm-{uuid.uuid4().hex[:12]}"
        self.store._x("INSERT INTO bot_group_messages (id, grp, role, who, text, data, src, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (mid, gid, role, who, text or "", json.dumps(data or {}, ensure_ascii=False), src or None, _now()))
        self.store._x("UPDATE bot_groups SET updated_at=? WHERE id=?", (_now(), gid))
        if role == "user":
            self.mark_read(gid)
        m = B.msg_row(self.store._x("SELECT * FROM bot_group_messages WHERE id=?", (mid,))[0])
        self._emit("group_message", m)
        return m

    def _emit(self, kind, obj):
        for f in list(self.listeners):
            try:
                f(kind, obj)
            except Exception:  # noqa: BLE001
                pass

    def _bg(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _sh(self, mid: str, cmd: str, timeout: int = 120) -> str:
        return await self.bots.sh(mid, cmd, timeout)

    # ------------------------------------------------------------------ create & add members
    async def _init_worktrees(self, mid: str, repo: str, base: str, gid: str, local_members: list[dict]) -> tuple[str, int]:
        wt_root = f"~/workspace/.aiw-worktrees/{gid}"
        lines = [
            f"R={qp(repo)}",
            'G="git -c user.name=AIW -c user.email=aiw@localhost"',
            'mkdir -p "$R" || { echo "GROUP_ERR 建不了文件夹"; exit 0; }',
            'if ! git -C "$R" rev-parse --git-dir >/dev/null 2>&1; then git -C "$R" init -q && echo NEW_REPO; fi',
            'if ! git -C "$R" rev-parse -q --verify HEAD >/dev/null; then $G -C "$R" commit -q --allow-empty -m "aiw: start"; fi',
            f"BASE={shlex.quote(base)}" if base else 'BASE=$(git -C "$R" rev-parse --abbrev-ref HEAD)',
            'git -C "$R" rev-parse -q --verify "$BASE^{commit}" >/dev/null || { echo "GROUP_ERR 没有分支 $BASE"; exit 0; }',
            'D=$(git -C "$R" status --porcelain | wc -l); echo "DIRTY $D"',
        ]
        for m in local_members:
            lines.append(f"mkdir -p {qp(wt_root)} && git -C \"$R\" worktree add -q -B {shlex.quote(m['branch'])} "
                         f"{qp(m['worktree'])} \"$BASE\" >/dev/null 2>&1 || {{ echo \"GROUP_ERR 建不了 {m['bot']} 的工作树\"; exit 0; }}")
        lines.append('echo "GROUP_OK $BASE"')
        out = await self._sh(mid, "\n".join(lines), 120)
        if "GROUP_OK" not in out:
            err = re.search(r"GROUP_ERR (.*)", out)
            raise ValueError("没能建群：" + (err.group(1) if err else B.clip(out.strip() or "电脑没有响应", 300)))
        resolved_base = re.search(r"GROUP_OK (\S+)", out).group(1)
        dirty = int((re.search(r"DIRTY\s+(\d+)", out) or [0, 0])[1] or 0)
        return resolved_base, dirty

    async def create(self, title: str, repo: str, members: list, base: str = "", machine_id: str = "",
                     auto_merge: bool = True, origin: str = "", model: Optional[dict] = None) -> dict:
        ids = []
        for m in members or []:
            m = str(m).strip().lower()
            if m in B.ENGINES and B.ENGINES[m]["available"] and m not in ids:
                ids.append(m)
        if not ids:
            raise ValueError("至少选一个引擎")
        gid = f"bg-{uuid.uuid4().hex[:8]}"
        local_ids = [b for b in ids if not B.is_cloud_engine(b)]
        cloud_ids = [b for b in ids if B.is_cloud_engine(b)]

        wt_root = f"~/workspace/.aiw-worktrees/{gid}"
        mem: list[dict] = []
        for b in ids:
            if B.is_cloud_engine(b):
                mem.append({"bot": b, "branch": "", "worktree": "", "cloud": True})
            else:
                mem.append({"bot": b, "branch": f"aiw/{gid}/{b}", "worktree": f"{wt_root}/{b}"})

        dirty = 0
        if local_ids:
            repo = (repo or "").strip() or f"~/workspace/groups/{gid}"
            if not ok_path(repo):
                raise ValueError("仓库路径不合法")
            base = (base or "").strip()
            if base and not ok_ref(base):
                raise ValueError("基线分支名不合法")
            mid = machine_id or self.bots.default_machine()
            base, dirty = await self._init_worktrees(mid, repo, base, gid, [m for m in mem if not m.get("cloud")])
        else:
            repo = ""
            base = ""
            mid = B.CLOUD_MACHINE
            auto_merge = False

        now = _now()
        title = (title or "").strip()[:60] or "引擎群聊"
        self.store._x("INSERT INTO bot_groups (id, title, machine_id, repo, base, status, auto_merge, members, pending, "
                      "fixes, origin, merge, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (gid, title, mid, repo, base, "idle", 1 if auto_merge else 0, "[]", "[]", 0, origin or None,
                       None, now, now))
        for m in mem:
            mm = (model or {}).get(m["bot"]) or {}
            c = self.bots.create(
                m["bot"], f"{title} · {B.ENGINES[m['bot']]['title']}", m["worktree"],
                B.CLOUD_MACHINE if m.get("cloud") else mid,
                mm.get("model", ""), mm.get("effort", ""), grp=gid, branch=m["branch"],
            )
            m["conv"] = c["id"]
        self.set_group(gid, members=mem)
        names = "、".join(B.ENGINES[m["bot"]]["title"] for m in mem)
        if local_ids and cloud_ids:
            cloud_names = "、".join(B.ENGINES[b]["title"] for b in cloud_ids)
            local_names = "、".join(B.ENGINES[b]["title"] for b in local_ids)
            intro = (f"建好了：{local_names} 各在自己的分支（aiw/{gid}/…）上干活，从 {base} 开始；"
                     f"{cloud_names} 作为云端成员参与讨论与协作（不操作本地 git）。"
                     f"本地改动做完由 AIW 管家合并到 aiw/{gid}/merge，不推送，也不动 {base}。")
        elif local_ids:
            intro = (f"建好了：{names} 各在自己的分支（aiw/{gid}/…）上干活，从 {base} 开始；"
                     f"做完由 AIW 管家合并到 aiw/{gid}/merge，不推送，也不动 {base}。")
        else:
            intro = f"建好了云端群聊：{names} 已入群，可以 @ 指定成员发言，或不 @ 让 AIW 管家分工协作。"
        if dirty:
            intro += "\n注意：仓库里有没提交的改动，工作树里看不到它们，需要的话先提交。"
        self.add(gid, "system", "", intro)
        return self.group(gid)

    async def add_members(self, gid: str, members: list, model: Optional[dict] = None) -> dict:
        """Add one or more engines (cloud or local) to an existing group."""
        g = self.group(gid)
        if not g:
            raise KeyError(gid)
        existing = {m["bot"] for m in g["members"]}
        to_add = []
        for m in members or []:
            b = str(m).strip().lower()
            if b in B.ENGINES and B.ENGINES[b]["available"] and b not in existing and b not in to_add:
                to_add.append(b)
        if not to_add:
            raise ValueError("没有可添加的新引擎（可能已在群里）")

        wt_root = f"~/workspace/.aiw-worktrees/{gid}"
        new_local: list[dict] = []
        new_mem: list[dict] = []
        for b in to_add:
            if B.is_cloud_engine(b):
                new_mem.append({"bot": b, "branch": "", "worktree": "", "cloud": True})
            else:
                item = {"bot": b, "branch": f"aiw/{gid}/{b}", "worktree": f"{wt_root}/{b}"}
                new_mem.append(item)
                new_local.append(item)

        repo = g.get("repo") or ""
        base = g.get("base") or ""
        mid = g.get("machine_id") or ""
        if new_local:
            if not mid or mid == B.CLOUD_MACHINE:
                mid = self.bots.default_machine()
            if not repo:
                repo = f"~/workspace/groups/{gid}"
            base, _ = await self._init_worktrees(mid, repo, base, gid, new_local)

        for m in new_mem:
            mm = (model or {}).get(m["bot"]) or {}
            c = self.bots.create(
                m["bot"], f"{g['title']} · {B.ENGINES[m['bot']]['title']}", m["worktree"],
                B.CLOUD_MACHINE if m.get("cloud") else mid,
                mm.get("model", ""), mm.get("effort", ""), grp=gid, branch=m["branch"],
            )
            m["conv"] = c["id"]

        updated = list(g["members"]) + new_mem
        patch: dict = {"members": updated}
        if new_local and (not g.get("repo") or g.get("machine_id") == B.CLOUD_MACHINE):
            patch.update(repo=repo, base=base, machine_id=mid)
        self.set_group(gid, **patch)
        added_names = "、".join(B.ENGINES[m["bot"]]["title"] for m in new_mem)
        self.add(gid, "system", "", f"已添加 {added_names} 加入群聊。")
        return self.group(gid)

    def delete(self, gid: str) -> bool:
        g = self.group(gid)
        if not g:
            return False
        for m in g["members"]:
            if m.get("conv"):
                self.bots.queues[m["conv"]] = []
                self.bots.delete(m["conv"])
        self.store._x("DELETE FROM bot_group_messages WHERE grp=?", (gid,))
        self.store._x("DELETE FROM bot_groups WHERE id=?", (gid,))
        return True

    async def stop(self, gid: str) -> dict:
        g = self.group(gid)
        if not g:
            raise KeyError(gid)
        for m in g["members"]:
            if m.get("conv") and (self.bots.busy(m["conv"]) or (self.bots.conv(m["conv"]) or {}).get("job")):
                try:
                    await self.bots.stop(m["conv"])
                except Exception as e:  # noqa: BLE001
                    log.warning("group stop %s: %s", m["conv"], e)
        self.add(gid, "system", "", "已停止")
        self.project.unwatch(gid)
        p = (self.group(gid) or {}).get("project")
        if p and p.get("phase") not in ("deliver", "stopped"):
            p["phase"] = "stopped"
        return self.set_group(gid, status="idle", pending=[], queue=[], paused=None, **({"project": p} if p else {}))

    # ------------------------------------------------------------------ talking
    def member(self, g: dict, bot: str) -> Optional[dict]:
        return next((m for m in g["members"] if m["bot"] == bot), None)

    def _context(self, g: dict, m: dict, limit: int = 8000) -> str:
        """Recent group chat timeline (messages + posted images/files) so every engine sees the full group context."""
        rows = self.store._x("SELECT role, who, text, data FROM bot_group_messages WHERE grp=? ORDER BY seq DESC LIMIT 30",
                             (g["id"],))
        out, size = [], 0
        for r in rows:
            d = B._j(r["data"], {})
            if r["role"] == "system":
                continue
            txt = (r["text"] or "").strip()
            media = d.get("media") or []
            if not txt and not media:
                continue
            name = B.ENGINES.get(r["who"], {}).get("title", r["who"]) if r["role"] == "bot" else (r["who"] or "用户")
            m_note = ""
            if media:
                m_names = [x.get("label") or x.get("name") or x.get("kind") or "文件" for x in media[:8] if isinstance(x, dict)]
                if m_names:
                    m_note = f"\n[群内附图/文件：{'、'.join(m_names)}]"
            body = B.clip(txt, 1800) if txt else "（发送了图片/文件）"
            line = f"【{name}】{body}{m_note}"
            if size + len(line) > limit:
                break
            out.append(line)
            size += len(line)
        return "\n\n".join(reversed(out))

    def _recent_group_media(self, gid: str, limit: int = 6) -> list[dict]:
        """Recent image files posted in the group chat (on the API host) so engines can inspect them as attachments."""
        rows = self.store._x("SELECT data FROM bot_group_messages WHERE grp=? ORDER BY seq DESC LIMIT 25", (gid,))
        out: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            d = B._j(r["data"], {})
            for x in d.get("media") or []:
                if not isinstance(x, dict) or x.get("kind") != "image":
                    continue
                p = str(x.get("path") or "").strip()
                if not p or p in seen or not os.path.isfile(p):
                    continue
                seen.add(p)
                out.append({
                    "artifact": p,
                    "name": x.get("name") or os.path.basename(p),
                    "label": x.get("label") or "",
                    "kind": "image",
                })
                if len(out) >= limit:
                    return out
        return out

    def _brief(self, g: dict, m: dict, task: str, fix: bool = False, context: str = "", files: str = "") -> str:
        others = "、".join(B.ENGINES[x["bot"]]["title"] for x in g["members"] if x["bot"] != m["bot"]) or "（没有）"
        ctx = f"\n\n以下是本群最近的聊天记录与已发图片清单（由 AIW 直接提供，以此为准）：\n{context}" if context else ""
        if m.get("cloud") or B.is_cloud_engine(m["bot"]):
            head = (f"［引擎群聊「{g['title']}」］你是群里的云端协作成员，和你一起协作的成员：{others}。\n"
                    "AIW 已把本群最近的聊天记录附在下方，也会把你这次的回复原样贴到群里；不要去外部找群聊、加入群或调用工具确认群存在。"
                    "请直接结合下方群聊记录完成分配给你的任务（调研、方案、评审或回答问题），给出清晰、结构化的结论，不需要操作本地文件或 git。")
            if files:   # v3.9.82: reviewers read the real files instead of guessing (「需核对」)
                ctx += ("\n\n仓库里现在的文件（只读副本，由 AIW 提供，直接据此审查，引用时写文件名和行）：\n" + files)
            return head + ctx + "\n\n" + task
        head = (f"［引擎群聊「{g['title']}」］你在 git 仓库的独立工作树里干活：当前文件夹，分支 {m['branch']}"
                f"（从 {g['base']} 开出" + ("，已经合入前面步骤的成果" if context else "") + f"）。其他成员：{others}；最后由 AIW 管家合并。\n"
                "AIW 会把你的回复贴到群里。规则：只改当前文件夹里的文件；做完 git add 并 git commit（中文说明）；不要 push，不要切换分支，"
                + ("" if fix else "不要合并别人的分支，") + "不要改仓库外的文件。只做这一步要求的改动：前面步骤由其他成员负责的文件（规范、测试等）不要改，"
                "发现它们有问题就在回复里写明，由 AIW 管家交回给负责的成员。如果生成了设计图或截图，请保存在当前工作目录并在回复里写出文件路径（AIW 会自动把图片直接发到群里）。最后用几句话说明改了什么、怎么验证的。")
        return head + ctx + "\n\n" + task

    async def send(self, gid: str, text: str, attachments: Optional[list] = None, by: str = "user") -> dict:
        g = self.group(gid)
        if not g:
            raise KeyError(gid)
        text = (text or "").strip()
        if not text:
            raise ValueError("空消息")
        ids = [m["bot"] for m in g["members"]]
        to, manager, rest = mentions(text, ids)
        um = self.add(gid, by, "我" if by == "user" else "AIW 管家", text,
                      {"to": to, "manager": manager or not to, **({"attachments": attachments} if attachments else {})})
        if g.get("queue") and re.fullmatch(r"\s*(继续|接着做|下一步|重试|continue)\s*[。.!！]?\s*", rest or ""):
            self._bg(self.resume(gid, "retry"))
            return um
        if g.get("paused") and g.get("queue") and re.fullmatch(r"\s*(跳过|跳过这一步|skip)\s*[。.!！]?\s*", rest or ""):
            self._bg(self.resume(gid, "skip"))
            return um
        if g.get("project") and await self.project.on_text(gid, rest or text):   # v3.9.86: 开工 / 选 B / 推送
            return um
        if self.project_trigger(g, text):   # v3.9.86: a project plan -> 讨论 → 设计 → 开发 → 评审 → 验收 → 实测 → 交付
            self._bg(self.project.start(gid, text, attachments))
            return um
        if is_manager_self_task(text, manager):
            self._bg(self._manager_do(gid, rest or text))
            return um
        steps = parse_steps(text, ids)
        if steps:
            self._bg(self._start_steps(gid, steps, "按你写的分工和顺序来，前一步做完再交给下一步。", attachments))
            return um
        if to and not manager:
            await self._assign(g, {b: rest or text for b in to}, attachments)
            return um
        # the planner sees the message with its @s, so who-does-what the user wrote survives
        self._bg(self._plan(gid, text if manager else (rest or text), attachments, prefer=to))
        return um

    async def _manager_do(self, gid: str, task: str):
        """Execute a direct task requested of AIW 管家 itself (never delegating to group engines)."""
        g = self.group(gid)
        if not g:
            return
        ids = [m["bot"] for m in g["members"]]
        wants_media = bool(re.search(r"(截图|图片|设计图|效果图|快照|原型图|png|jpg|webp|发到群|发群里|发出来|贴到群|找到.*图)", task, re.I))
        if wants_media:
            await self._manager_find_media(g, task, ids)
            return
        # General self-task for 管家 (answer / summarize / inspect directly without dispatching)
        timeline = self._context(g, {"bot": ""}, 10000)
        text = ""
        if self.bots.llm:
            try:
                sys_p = ("你是 AIW 管家，用户明确要求你自己直接完成这件事，不要把任务再派给群里的其他引擎。"
                         "请直接根据群聊记录给出清晰、准确、简明的中文答复；不要编造没有看到的文件或结果。")
                text = await asyncio.wait_for(
                    self.bots.llm().complete(sys_p, f"用户的指令：\n{task}\n\n群聊记录：\n{timeline}", 2000), 90
                )
                text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
            except Exception as e:  # noqa: BLE001
                log.warning("manager self task: %s", e)
        self.add(gid, "aiw", "AIW 管家", text or "收到，这件事由我直接处理，没有转派给其他引擎。")

    async def _manager_find_media(self, g: dict, task: str, ids: list):
        """Find images/screenshots in the group's worktrees / repo and post them directly into the group."""
        gid = g["id"]
        target_bots = mentioned_engines(task, ids)
        local_members = [m for m in g["members"] if m.get("worktree") and m.get("conv")]
        if target_bots:
            chosen = [m for m in local_members if m["bot"] in target_bots]
            if not chosen:
                chosen = local_members
        else:
            chosen = local_members
        search_dirs: list[tuple[str, str, str]] = []   # (label_prefix, conv_id, dir_path)
        for m in chosen:
            title = B.ENGINES.get(m["bot"], {}).get("title", m["bot"])
            search_dirs.append((title, m["conv"], m["worktree"]))
        fallback_conv = next((m["conv"] for m in local_members if m.get("conv")), "")
        if g.get("repo") and fallback_conv:
            search_dirs.append(("仓库", fallback_conv, g["repo"]))
        if fallback_conv:
            search_dirs.append(("截图目录", fallback_conv, f"~/workspace/.aiw-worktrees/{gid}/_shots"))

        if not search_dirs:
            self.add(gid, "aiw", "AIW 管家", "这个群没有本地工作目录，找不到本地截图文件。")
            return

        # Scan for image/video files across the target directories on the computer (newest first, Linux & macOS safe)
        py_scan = (
            "import os, sys, json\n"
            "exts = {'.png','.jpg','.jpeg','.webp','.gif','.bmp','.svg','.mp4','.mov','.webm'}\n"
            "skip = {'.git','node_modules','.venv','__pycache__','dist','build'}\n"
            "for idx, raw in enumerate(json.loads(sys.argv[1])):\n"
            "  d = os.path.expanduser(raw)\n"
            "  if not os.path.isdir(d): continue\n"
            "  base_depth = d.rstrip('/').count('/')\n"
            "  for root, dirs, files in os.walk(d):\n"
            "    dirs[:] = [x for x in dirs if x not in skip and root.rstrip('/').count('/') - base_depth < 5]\n"
            "    for fn in files:\n"
            "      if os.path.splitext(fn)[1].lower() in exts:\n"
            "        fp = os.path.join(root, fn)\n"
            "        try: print(f'{idx}|{os.path.getmtime(fp)}|{fp}')\n"
            "        except OSError: pass\n"
        )
        dir_json = json.dumps([dpath for _, _, dpath in search_dirs], ensure_ascii=False)
        found: list[tuple[float, int, str]] = []
        try:
            out = await self._sh(g["machine_id"], f"python3 -c {shlex.quote(py_scan)} {shlex.quote(dir_json)}", 60)
            for line in (out or "").splitlines():
                parts = line.split("|", 2)
                if len(parts) != 3:
                    continue
                try:
                    idx, ts, fpath = int(parts[0]), float(parts[1]), parts[2].strip()
                except ValueError:
                    continue
                if fpath:
                    found.append((ts, idx, fpath))
        except Exception as e:  # noqa: BLE001
            log.warning("manager find media: %s", e)

        found.sort(key=lambda x: x[0], reverse=True)
        out_dir = self.bots._art_dir() if hasattr(self.bots, "_art_dir") else "workspace/artifacts"
        media: list[dict] = []
        seen_paths: set[str] = set()
        for _ts, idx, fpath in found:
            if len(media) >= 12:
                break
            lbl, conv_id, _ = search_dirs[idx]
            rel = re.sub(r"^/(?:home/[^/]+|root)/workspace/", "~/workspace/", fpath)
            if rel in seen_paths:
                continue
            seen_paths.add(rel)
            try:
                r = await self.bots.fetch_file(conv_id, rel, out_dir)
                media.append({
                    "name": r["name"],
                    "path": r["path"],
                    "size": r.get("size"),
                    "kind": B.media_kind(r["name"]),
                    "label": f"{lbl} · {r['name']}",
                })
            except Exception as e:  # noqa: BLE001
                log.debug("manager fetch media %s: %s", rel, e)

        # Fallback: if no images exist yet, check for HTML design files and screenshot them directly
        if not media and chosen:
            html_cmds = []
            for idx, (lbl, _cid, dpath) in enumerate(search_dirs[:len(chosen)]):
                html_cmds.append(
                    f'D={qp(dpath)}; if [ -d "$D" ]; then '
                    f'find "$D" -maxdepth 4 -type f -iname "*.html" '
                    f'! -path "*/.git/*" ! -path "*/node_modules/*" ! -path "*/.venv/*" '
                    f'-printf "{idx}|%T@|%p\\n" 2>/dev/null; fi'
                )
            html_hits: list[tuple[float, int, str]] = []
            try:
                hout = await self._sh(g["machine_id"], "bash -c " + shlex.quote("; ".join(html_cmds)), 60)
                for line in (hout or "").splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        try:
                            html_hits.append((float(parts[1]), int(parts[0]), parts[2].strip()))
                        except ValueError:
                            pass
            except Exception as e:  # noqa: BLE001
                log.debug("manager find html: %s", e)
            html_hits.sort(key=lambda x: x[0], reverse=True)
            shot_items = []
            for i, (_ts, idx, hp) in enumerate(html_hits[:6]):
                lbl = search_dirs[idx][0]
                stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{lbl}-{i+1}")
                shot_items.append((stem, "file://" + hp))
            if shot_items:
                sh = await self.project.screenshots(g, "manager-find", shot_items)
                media = sh.get("media") or []

        if media:
            names = "、".join(m.get("label") or m.get("name") or "" for m in media[:8])
            more = f" 等共 {len(media)} 个文件" if len(media) > 8 else f"（共 {len(media)} 张）"
            self.add(gid, "aiw", "AIW 管家", f"已亲自从工作区找到并发送到群里：{names}{more}。", {"media": media})
        else:
            dirs_txt = "、".join(f"{lbl}（{dp}）" for lbl, _, dp in search_dirs)
            self.add(gid, "aiw", "AIW 管家", f"我已亲自检查了工作区（{dirs_txt}），目前没有找到图片或截图文件。")

    def project_trigger(self, g: dict, text: str) -> bool:
        from .group_project import is_trigger
        return bool(g.get("repo")) and is_trigger(text)

    async def _plan(self, gid: str, text: str, attachments: Optional[list] = None, prefer: Optional[list] = None):
        g = self.group(gid)
        ids = prefer or [m["bot"] for m in g["members"]]
        plan = None
        if len(ids) > 1 and self.bots.llm:
            mem = "；".join(
                f"{b}（{B.ENGINES[b]['title']}{'，云端' if B.is_cloud_engine(b) else '，本地代码'}）"
                for b in ids
            )
            try:
                repo_note = f"仓库：{g['repo']}（基线 {g['base']}）\n" if g.get("repo") else "类型：云端讨论协作群\n"
                out = await asyncio.wait_for(self.bots.llm().complete(PLAN.format(members=mem, ids="、".join(ids)),
                                                                      f"{repo_note}用户：{text}", 1600), 90)
                plan = _reroute_cloud_local_plan(parse_plan(out, ids), ids)
            except Exception as e:  # noqa: BLE001
                log.warning("group plan: %s", e)
        if plan and plan.get("self"):
            await self._manager_do(gid, plan.get("task") or text)
            return
        if plan and plan.get("steps"):
            await self._start_steps(gid, plan["steps"], plan["report"], attachments)
            return
        if not plan:
            fallback_ids = [b for b in ids if not B.is_cloud_engine(b)] if _needs_local_access(text) else ids
            fallback_ids = fallback_ids or ids
            plan = {"assign": {b: text for b in fallback_ids},
                    "report": "没分开，每个人都拿到同一个任务，各做一版" if len(fallback_ids) > 1 else ""}
        lines = "\n".join(f"@{b} {t}" for b, t in plan["assign"].items())
        self.add(gid, "aiw", "AIW 管家", ((plan["report"] + "\n\n") if plan["report"] else "") + lines,
                 {"plan": plan["assign"]})
        await self._assign(self.group(gid), plan["assign"], attachments)

    async def _start_steps(self, gid: str, steps: list, report: str, attachments: Optional[list] = None):
        name = lambda b: "AIW 管家" if b == MANAGER_ID else B.ENGINES.get(b, {}).get("title", b)  # noqa: E731
        lines = "\n".join(f"{i}. {name(s['bot'])}：{B.clip(s['step'].splitlines()[0] if s.get('step') else s['task'], 80)}"
                          for i, s in enumerate(steps, 1))
        self.add(gid, "aiw", "AIW 管家", ((report + "\n\n") if report else "") + lines,
                 {"steps": [{"bot": s["bot"], "task": s["task"]} for s in steps]})
        q = [{**s, "n": i, "of": len(steps)} for i, s in enumerate(steps, 1)]
        if attachments:
            q[0]["attachments"] = attachments
        self.set_group(gid, queue=q, status="working", pending=[], fixes=0)
        await self._run_next(gid)

    async def _sync(self, g: dict, m: dict, done: list) -> str:
        """Bring the earlier local steps' commits into this member's branch before its step starts."""
        prior = [x for x in g["members"] if x["bot"] in done and x.get("branch") and x["bot"] != m["bot"]]
        if not prior or not m.get("worktree"):
            return ""
        lines = ['G="git -c user.name=AIW -c user.email=aiw@localhost"', f"W={qp(m['worktree'])}"]
        for p in prior:
            lines += [f"P={qp(p['worktree'])}",
                      'if [ -d "$P" ] && [ -n "$(git -C "$P" status --porcelain)" ]; then git -C "$P" add -A && '
                      f'$G -C "$P" commit -q -m {shlex.quote("aiw(" + p["bot"] + "): " + g["title"])}; fi',
                      f'$G -C "$W" merge -q --no-ff --no-edit -m {shlex.quote("AIW 管家：合入 " + p["bot"] + " 的成果")} '
                      f'{shlex.quote(p["branch"])} >/dev/null 2>&1 && echo "SYNCED {p["bot"]}" || '
                      f'{{ git -C "$W" merge --abort >/dev/null 2>&1; echo "SYNC_CONFLICT {p["bot"]}"; }}']
        try:
            return await self._sh(g["machine_id"], "\n".join(lines), 120)
        except Exception as e:  # noqa: BLE001
            log.warning("group sync: %s", e)
            return f"SYNC_ERR {e}"

    async def _run_next(self, gid: str):
        g = self.group(gid)
        if not g or not g.get("queue"):
            return
        q = list(g["queue"])
        s = q.pop(0)
        g = self.set_group(gid, queue=q, status="working", cur=s, paused=None)   # v3.9.88: survives a restart
        done = [r["who"] for r in self.store._x(
            "SELECT DISTINCT who FROM bot_group_messages WHERE grp=? AND role='bot'", (gid,))]
        if s["bot"] == MANAGER_ID:
            if s.get("op"):   # v3.9.86: a 项目模式 step of the manager's own
                await self.project.op(gid, s)
                return
            await self._final(gid, s["task"])
            return
        m = self.member(g, s["bot"])
        if not m or not m.get("conv"):
            self.add(gid, "aiw", "AIW 管家", f"{s.get('step') or '这一步'}的 {s['bot']} 不在群里，跳过。")
            await self._run_next(gid)
            return
        if not (m.get("cloud") or B.is_cloud_engine(m["bot"])):
            out = await self._sync(g, m, done)
            bad = re.findall(r"SYNC_CONFLICT (\S+)", out)
            if bad:
                self.add(gid, "aiw", "AIW 管家", f"把 {'、'.join(bad)} 的成果合进 {B.ENGINES[m['bot']]['title']} 的分支时冲突了，"
                         "让它自己先合并解决。")
                s["task"] = (f"开始前先执行 git merge {' '.join(x['branch'] for x in g['members'] if x['bot'] in bad)}"
                             "，解决冲突后提交。\n\n" + s["task"])
        files = ""
        if m.get("cloud") or B.is_cloud_engine(m["bot"]):
            files = await self._snapshot(g, done)
        else:
            m = await self._mark_start(g, m)
        self.set_group(gid, pending=[s["bot"]])
        att = []
        grp_imgs = self._recent_group_media(gid)
        merged_att = list(s.get("attachments") or []) + [x for x in grp_imgs if x not in (s.get("attachments") or [])]
        if merged_att:
            try:
                att = await self.bots.push_files(m["conv"], merged_att, allowed=lambda p: p if p and os.path.isfile(p) else None)
            except Exception as e:  # noqa: BLE001
                log.warning("group files: %s", e)
        self.add(gid, "aiw", "AIW 管家", (f"第 {s['n']}/{s['of']} 步交给 {B.ENGINES[m['bot']]['title']}。" if s.get("n") else
                                          f"{s.get('step') or '下一步'}：交给 {B.ENGINES[m['bot']]['title']}。"))
        self.bots.send(m["conv"], self._brief(g, m, s["task"], context=self._context(g, m), files=files), "aiw", attachments=att)

    # ------------------------------------------------------------------ v3.9.82: who owns which file, read-only copies
    def _put_member(self, gid: str, bot: str, **kw) -> dict:
        g = self.group(gid)
        mem = [dict(x, **kw) if x["bot"] == bot else x for x in g["members"]]
        return next(x for x in self.set_group(gid, members=mem)["members"] if x["bot"] == bot)

    async def _mark_start(self, g: dict, m: dict) -> dict:
        """Remember where this member's step starts, so its own changes can be told from the earlier steps'."""
        if not m.get("worktree"):
            return m
        try:
            out = await self._sh(g["machine_id"], f"git -C {qp(m['worktree'])} rev-parse HEAD", 30)
        except Exception as e:  # noqa: BLE001
            log.warning("group start: %s", e)
            return m
        h = re.search(r"\b[0-9a-f]{40}\b", out or "")
        return self._put_member(g["id"], m["bot"], start=h.group(0)) if h else m

    async def _scope(self, gid: str, bot: str):
        """After a step: the files this member changed, and a warning when it touched files an earlier member owns."""
        g = self.group(gid)
        m = self.member(g, bot) if g else None
        if not m or not m.get("worktree") or not m.get("start"):
            return
        cmd = ('G="git -c user.name=AIW -c user.email=aiw@localhost"; '
               f"W={qp(m['worktree'])}; "
               'if [ -n "$(git -C "$W" status --porcelain)" ]; then git -C "$W" add -A && '
               f'$G -C "$W" commit -q -m {shlex.quote("aiw(" + bot + "): " + g["title"])}; fi; '
               f"git -C \"$W\" diff --name-only {shlex.quote(m['start'])} HEAD")
        try:
            out = await self._sh(g["machine_id"], cmd, 60)
        except Exception as e:  # noqa: BLE001
            log.warning("group scope: %s", e)
            return
        mine = [x.strip() for x in (out or "").splitlines() if x.strip() and not x.startswith(("fatal", "error"))]
        await self._turn_diff(g, m)
        owner = {}
        for x in g["members"]:
            if x["bot"] != bot:
                for f in x.get("files") or []:
                    owner.setdefault(f, x["bot"])
        self._put_member(gid, bot, files=sorted(set((m.get("files") or []) + mine)))
        hit = {}
        for f in mine:
            if f in owner:
                hit.setdefault(owner[f], []).append(f)
        if hit:
            who = B.ENGINES.get(bot, {}).get("title", bot)
            parts = "；".join(f"{B.ENGINES.get(o, {}).get('title', o)} 负责的 {'、'.join(fs[:6])}" for o, fs in hit.items())
            self.add(gid, "aiw", "AIW 管家", f"注意：{who} 这一步改了 {parts}。这些改动建议让原负责人复核，确认没有为了通过而放宽。",
                     {"scope": {"bot": bot, "touched": hit}})

    async def _turn_diff(self, g: dict, m: dict):
        """v3.9.88 只看这一轮: the files this turn's own commits changed (merges it pulled in left out), on its message."""
        cmd = (f"W={qp(m['worktree'])}; echo HEAD $(git -C \"$W\" rev-parse HEAD); "
               f"git -C \"$W\" log --no-merges --name-only --format= {shlex.quote(m['start'])}..HEAD | sort -u")
        try:
            out = await self._sh(g["machine_id"], cmd, 60)
        except Exception as e:  # noqa: BLE001
            log.warning("group turn diff: %s", e)
            return
        h = re.search(r"^HEAD ([0-9a-f]{40})", out or "", re.M)
        files = [x.strip() for x in (out or "").splitlines()
                 if x.strip() and not x.startswith(("HEAD ", "fatal", "error", "warning"))]
        r = self.store._x("SELECT id FROM bot_group_messages WHERE grp=? AND role='bot' AND who=? ORDER BY seq DESC LIMIT 1",
                          (g["id"], m["bot"]))
        if h and r:
            self.patch_msg(r[0]["id"], diff={"from": m["start"], "to": h.group(1), "files": files[:200]})

    async def diff(self, gid: str, mid: str = "", whole: bool = False, limit: int = 400000) -> dict:
        """v3.9.88: one turn's own changes (its message's range, only the files its commits touched), or the whole
        result against the base branch."""
        g = self.group(gid)
        if not g:
            raise KeyError(gid)
        if whole:
            mb = f"aiw/{gid}/merge"
            cmd = (f"R={qp(g['repo'])}; git -C \"$R\" rev-parse -q --verify {shlex.quote(mb)} >/dev/null || "
                   f"{{ echo 'NO_MERGE'; exit 0; }}; git -C \"$R\" diff --stat {shlex.quote(g['base'])}...{shlex.quote(mb)} | tail -n 1; "
                   f"echo '=====DIFF'; git -C \"$R\" diff {shlex.quote(g['base'])}...{shlex.quote(mb)}")
            title = f"{g['base']} → {mb}"
        else:
            r = self.store._x("SELECT * FROM bot_group_messages WHERE grp=? AND id=?", (gid, mid))
            d = B._j(r[0]["data"], {}) if r else {}
            rng = d.get("diff") or {}
            m = self.member(g, r[0]["who"]) if r else None
            if not rng or not m or not m.get("worktree"):
                raise ValueError("这条回复没有记下改动范围")
            if not rng.get("files"):
                return {"title": "这一轮没有提交改动", "stat": "", "diff": "", "files": []}
            files = " ".join(shlex.quote(f) for f in rng["files"])
            cmd = (f"W={qp(m['worktree'])}; git -C \"$W\" diff --stat {rng['from']} {rng['to']} -- {files} | tail -n 1; "
                   f"echo '=====DIFF'; git -C \"$W\" diff {rng['from']} {rng['to']} -- {files}")
            title = f"{B.ENGINES.get(m['bot'], {}).get('title', m['bot'])} 这一轮"
        out = await self._sh(g["machine_id"], "bash -c " + shlex.quote(cmd), 120)
        if "NO_MERGE" in (out or "")[:200]:
            raise ValueError("还没有合并分支，先合并")
        stat, _, body = (out or "").partition("=====DIFF")
        return {"title": title, "stat": stat.strip(), "diff": B.clip(body.lstrip("\n"), limit),
                "truncated": len(body) > limit}

    async def resume(self, gid: str, how: str = "retry"):
        """v3.9.88: carry on after a pause. retry: the step that stopped runs again (it is back at the head of the
        queue); skip: drop it and go on with the next."""
        g = self.group(gid)
        if not g or not g.get("queue"):
            raise ValueError("没有等着继续的步骤")
        q = list(g["queue"])
        if how == "skip":
            s = q.pop(0)
            self.add(gid, "aiw", "AIW 管家", f"跳过「{s.get('step') or B.clip(s.get('task', ''), 40)}」，接着做下一步。")
            if not q:
                self.set_group(gid, queue=[], status="idle", paused=None)
                return
        self.set_group(gid, queue=q, paused=None)
        await self._run_next(gid)

    async def permit(self, gid: str, bot: str, mode: str, rule: str = "") -> dict:
        """v3.9.88: a member's blocked command answered in the group (the same rules as its own chat). allow / once
        rerun it there; its reply comes back here and the steps go on."""
        g = self.group(gid)
        m = self.member(g, bot) if g else None
        if not m or not m.get("conv"):
            raise ValueError("群里没有这个成员")
        await self.bots.permit(m["conv"], mode, rule)
        name = B.ENGINES.get(bot, {}).get("title", bot)
        if mode == "deny":
            self.add(gid, "aiw", "AIW 管家", f"已拒绝 {name} 被拦的命令。这一步停着：可以「重试这一步」或「跳过这一步」。",
                     {"actions": self.pause_actions()})
            return self.group(gid)
        q = list(g.get("queue") or [])
        if q and q[0].get("bot") == bot:
            q.pop(0)              # it reruns in its own chat; its reply carries the steps on
        self.add(gid, "aiw", "AIW 管家", f"{'已允许' if mode == 'allow' else '这一轮放开了权限'}，{name} 接着做。")
        return self.set_group(gid, queue=q, pending=[bot], status="working", paused=None)

    @staticmethod
    def pause_actions() -> list:
        return [{"id": "resume", "value": "retry", "label": "重试这一步", "desc": "把这一步重新交给它"},
                {"id": "resume", "value": "skip", "label": "跳过这一步", "desc": "不做这一步，接着做下一步"}]

    async def recover(self):
        """v3.9.88 可恢复: at startup, carry on every group a restart interrupted. The step in hand is kept (cur):
        a manager step runs again; a member whose reply already came is caught up; a member whose turn the restart
        cut off gets the step again. A project still watching its pull request starts watching again."""
        for row in self.store._x("SELECT id FROM bot_groups"):
            gid = row["id"]
            try:
                await self._recover_one(gid)
            except Exception as e:  # noqa: BLE001
                log.warning("group recover %s: %s", gid, e)

    async def _recover_one(self, gid: str):
        g = self.group(gid)
        if not g:
            return
        p = g.get("project") or {}
        if (p.get("watch") or {}).get("on"):
            self.project.watch(gid)
        if g["status"] not in ("working", "fixing", "merging"):
            return
        cur = g.get("cur") or {}
        if g["status"] == "merging" or (cur.get("bot") == MANAGER_ID and not g["pending"]):
            if cur:
                self.set_group(gid, queue=[cur] + list(g.get("queue") or []), status="working")
                self.add(gid, "aiw", "AIW 管家", f"服务重启过，从「{cur.get('step') or '这一步'}」接着做。")
                self._bg(self._run_next(gid))
            else:
                self.set_group(gid, status="idle")
            return
        for bot in list(g["pending"]):
            m = self.member(g, bot)
            if not m or not m.get("conv"):
                continue
            last = next((x for x in reversed(self.bots.messages(m["conv"], limit=10)) if x["role"] == "bot"), None)
            if last and (last.get("data") or {}).get("status") not in (None, "running"):
                self._on_bot("message", last)          # its reply came while the API was down: catch up
                continue
            if self.bots.busy(m["conv"]):
                continue
            try:
                await self.bots.stop(m["conv"])        # the turn the restart cut off
            except Exception as e:  # noqa: BLE001
                log.warning("recover stop %s: %s", bot, e)
            g = self.group(gid)
            self.set_group(gid, pending=[b for b in g["pending"] if b != bot])
            if cur.get("bot") == bot:
                self.set_group(gid, queue=[cur] + list(self.group(gid).get("queue") or []), status="working")
                self.add(gid, "aiw", "AIW 管家", f"服务重启打断了 {B.ENGINES.get(bot, {}).get('title', bot)} 的「{cur.get('step') or '这一步'}」，重新交给它。")
                self._bg(self._run_next(gid))
            else:
                self.add(gid, "aiw", "AIW 管家", f"服务重启打断了 {B.ENGINES.get(bot, {}).get('title', bot)} 这一轮，"
                         "它的结果没收到。需要的话再 @ 它说一次。")
                if not self.group(gid)["pending"]:
                    self.set_group(gid, status="idle")

    async def _snapshot(self, g: dict, done: list, limit: int = 40000) -> str:
        """What the earlier local steps produced, as text a cloud reviewer can read (it has no access to the computer)."""
        prior = [x for x in g["members"] if x["bot"] in done and x.get("worktree") and x.get("files")]
        if not prior:
            return ""
        src = prior[-1]
        names = []
        for x in prior:
            names += [f for f in x["files"] if f not in names]
        cat = " ".join(shlex.quote(f) for f in names[:40])
        cmd = (f"cd {qp(src['worktree'])} && for f in {cat}; do [ -f \"$f\" ] || continue; "
               'if grep -Iq . "$f" 2>/dev/null || [ ! -s "$f" ]; then echo "===== $f"; head -c 12000 "$f"; echo; '
               'else echo "===== $f（二进制，$(wc -c <"$f") 字节）"; fi; done')
        try:
            out = await self._sh(g["machine_id"], cmd, 60)
        except Exception as e:  # noqa: BLE001
            log.warning("group snapshot: %s", e)
            return ""
        return B.clip(out or "", limit)

    async def _after_step(self, gid: str, bot: str):
        try:
            await self._scope(gid, bot)
        except Exception as e:  # noqa: BLE001
            log.warning("group after step: %s", e)
        await self._run_next(gid)

    VERIFY_OK = re.compile(r"^(python3? -m pytest|pytest|python3? [\w./-]+\.py|node [\w./-]+|npm (run )?test|go test|cargo test|ls|cat|wc)\b")

    def _verify_cmds(self, task: str) -> list:
        cmds = []
        for c in re.findall(r"`([^`\n]+)`", task) + re.findall(r"(?:运行|跑|执行)(?:一遍|一下)?\s*((?:python3?|pytest|npm|node|go|cargo)[^，。,;；\n]*)", task):
            c = c.strip()
            for part in re.split(r"\s+和\s+", c):
                part = part.strip()
                if part and self.VERIFY_OK.match(part) and not re.search(r"[;&|`$<>]", part) and part not in cmds:
                    cmds.append(part)
        return cmds[:5]

    async def _run_checks(self, g: dict, cmds: list) -> list:
        """Run each check in the merged worktree; each output ends with EXIT <code>."""
        runs = []
        mwt = f"~/workspace/.aiw-worktrees/{g['id']}/_merge"
        for c in cmds:
            if not self.VERIFY_OK.match(c) or re.search(r"[;&|`$<>]", c):
                runs.append((c, "这条命令不在允许的验收命令里，没跑。\nEXIT 126"))
                continue
            try:
                out = await self._sh(g["machine_id"], "bash -c " + shlex.quote(   # v3.9.82: PIPESTATUS is bash-only
                    f"cd {qp(mwt)} && timeout 300 {c} 2>&1 | tail -n 40; echo \"EXIT ${{PIPESTATUS[0]}}\""), 330)
            except Exception as e:  # noqa: BLE001
                out = f"运行失败：{e}\nEXIT 1"
            runs.append((c, out.strip()))
        return runs

    async def _final(self, gid: str, task: str):
        """The step the user gave AIW 管家 itself: merge/check when requested, or compare/summarize/ask directly."""
        g = self.group(gid)
        if not g:
            return
        if re.search(r"(找到.*(?:截图|图片|设计图)|把.*(?:截图|图片).*发到群|发群里)", task or ""):
            await self._manager_find_media(g, task, [m["bot"] for m in g["members"]])
            self.set_group(gid, status="idle", pending=[], queue=[])
            return
        has_local = any(m.get("branch") and m.get("worktree") for m in g["members"])
        no_merge_intent = bool(re.search(r"(不要合并|别合并|暂不合并|选.*方案|选择.*方案|对比.*方案|汇总.*方案|向我提问|问我选|等我选)", task or ""))
        wants_merge = bool(re.search(r"(合并|merge|验收|测试|跑.*测|交付|部署|构建|编译|pytest|npm)", task or "", re.I)) and not no_merge_intent
        merge_note, runs = "", []
        if has_local and wants_merge:
            r = await self.merge(gid)
            g = self.group(gid)
            if g["status"] == "fixing":   # conflicts went back to the members; report once they have fixed them
                self.set_group(gid, queue=[{"bot": MANAGER_ID, "task": task, "step": task, "n": 0, "of": 0}])
                return
            merge_note = f"合并：{'、'.join(r.get('merged') or []) or '无'}；{r.get('stat') or ''}"
            runs = await self._run_checks(g, self._verify_cmds(task))
        timeline = self._context(g, {"bot": ""}, 12000)
        checks = "\n\n".join(f"$ {c}\n{B.clip(o, 2000)}" for c, o in runs)
        text = ""
        if self.bots.llm:
            try:
                if wants_merge:
                    sys_p = ("你是 AIW 管家，负责验收一次多引擎分工并向用户汇报。只依据给你的群聊记录、合并结果和你亲自运行的命令输出，"
                             "不要编造。用中文；先给一张 markdown 表（成员、负责什么、产出哪些文件、是否一次完成、遇到的问题），"
                             "再写几句评价（交接是否顺畅、谁越权、最终测试结果、下次怎么改进）。没有的数据写“未知”。")
                else:
                    sys_p = ("你是 AIW 管家，请严格按照用户交代的这一步任务来执行（如汇总对比各成员方案、列出优缺点、明确向用户提问等待选择等）。"
                             "只依据给你的群聊记录与已发图片清单，不要编造，不要强行做无关的代码验收表。用中文，条理清晰。")
                text = await asyncio.wait_for(self.bots.llm().complete(
                    sys_p, f"用户交给你的任务：\n{task}\n\n{merge_note}\n\n你运行的命令：\n{checks or '（没有）'}\n\n群聊记录：\n{timeline}", 2500), 120)
                text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
            except Exception as e:  # noqa: BLE001
                log.warning("group final: %s", e)
        if not text:
            text = "\n\n".join(x for x in (merge_note, checks) if x) or "都做完了。"
        elif checks:
            text += "\n\n运行记录：\n" + checks
        self.set_group(gid, status="idle", pending=[], queue=[])
        self.add(gid, "aiw", "AIW 管家", text, {"report": True, "done": True})
        await self._tell_chat(self.group(gid), B.clip(text, 400))

    async def _assign(self, g: dict, assign: dict, attachments: Optional[list] = None):
        pending = list(dict.fromkeys(list(g["pending"]) + list(assign)))
        self.set_group(g["id"], status="working", pending=pending, fixes=0 if g["status"] != "fixing" else g["fixes"])
        grp_imgs = self._recent_group_media(g["id"])
        for bot, task in assign.items():
            m = self.member(g, bot)
            if not m or not m.get("conv"):
                continue
            att = []
            merged_att = list(attachments or []) + [x for x in grp_imgs if x not in (attachments or [])]
            if merged_att:
                try:
                    att = await self.bots.push_files(m["conv"], merged_att, allowed=lambda p: p if p and os.path.isfile(p) else None)
                except Exception as e:  # noqa: BLE001
                    log.warning("group files: %s", e)
            ctx = self._context(g, m)
            self.bots.send(m["conv"], self._brief(g, m, task, fix=g["status"] == "fixing", context=ctx), "aiw", attachments=att)

    # ------------------------------------------------------------------ replies from the members
    def _on_bot(self, kind: str, obj):
        if kind != "message" or not obj or obj.get("role") != "bot":
            return
        d = obj.get("data") or {}
        if d.get("status") in (None, "running"):
            return
        c = self.bots.conv(obj["conv"])
        gid = (c or {}).get("grp")
        if not gid:
            return
        if self.store._x("SELECT 1 FROM bot_group_messages WHERE src=?", (obj["id"],)):
            return
        g = self.group(gid)
        if not g:
            return
        bot = c["bot"]
        self.add(gid, "bot", bot, obj.get("text") or "", {
            "status": d.get("status"), "changes": d.get("changes"), "denied": d.get("denied"),
            "steps": len(d.get("steps") or []), "conv": c["id"], **({"media": d["media"]} if d.get("media") else {})},
            src=obj["id"])
        pending = [b for b in g["pending"] if b != bot]
        g = self.set_group(gid, pending=pending)
        if not pending and g["status"] in ("working", "fixing"):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            if g.get("queue"):
                if g["status"] == "working" and d.get("status") in ("turn_failed", "blocked", "failed", "error"):
                    cur = g.get("cur") or {}   # v3.9.88: the step stays at the head of the queue: 重试 runs it again
                    q = ([cur] if cur.get("bot") == bot else []) + list(g["queue"])
                    why = "blocked" if d.get("status") == "blocked" else "failed"
                    self.set_group(gid, status="idle", queue=q, paused={"bot": bot, "why": why, "msg": obj["id"]})
                    name = B.ENGINES.get(bot, {}).get('title', bot)
                    self.add(gid, "aiw", "AIW 管家", (f"{name} 被权限拦住了，后面的步骤先停下。在它的回复上点「允许」让它接着做，" if why == "blocked"
                             else f"{name} 这一步没做成，后面的步骤先停下。") + "点「重试这一步」（或说「@管家 继续」）重新交给它，或跳过这一步。",
                             {"actions": self.pause_actions()})
                    loop.create_task(self._tell_chat(g, f"{B.ENGINES.get(bot, {}).get('title', bot)} 没做成，分工暂停了。"))
                    return
                self._bg(self._after_step(gid, bot))
                return
            has_local = any(m.get("branch") for m in g["members"])
            if g["auto_merge"] and has_local and self._round_has_local(gid):
                self._bg(self.merge(gid))
            else:
                self.set_group(gid, status="idle")
                if not self._round_has_local(gid):
                    return
                note = "都回复了。看一下各自的结果，需要合并就点「合并」。" if has_local else "群里的成员都回复了。"
                self.add(gid, "aiw", "AIW 管家", note)
                loop.create_task(self._tell_chat(g, "群里的引擎都做完了" + ("，等你看一下再合并。" if has_local else "。")))

    def _round_has_local(self, gid: str) -> bool:
        """True if at least one local code engine replied since the last user message in this group."""
        rows = self.store._x("SELECT role, who FROM bot_group_messages WHERE grp=? ORDER BY seq DESC LIMIT 30", (gid,))
        for r in rows:
            if r["role"] == "user":
                break
            if r["role"] == "bot" and not B.is_cloud_engine(r["who"] or ""):
                return True
        return False


    # ------------------------------------------------------------------ merge (AIW 管家)
    def merge_script(self, g: dict) -> str:
        gid, mb = g["id"], f"aiw/{g['id']}/merge"
        mwt = f"~/workspace/.aiw-worktrees/{gid}/_merge"
        local_members = [m for m in g["members"] if m.get("branch") and m.get("worktree")]
        lines = [f"R={qp(g['repo'])}", f"BASE={shlex.quote(g['base'])}", f"M={shlex.quote(mb)}", f"MW={qp(mwt)}",
                 'G="git -c user.name=AIW -c user.email=aiw@localhost"']
        for m in local_members:
            lines += [f"W={qp(m['worktree'])}",
                      'if [ -d "$W" ] && [ -n "$(git -C "$W" status --porcelain)" ]; then '
                      f'git -C "$W" add -A && $G -C "$W" commit -q -m {shlex.quote("aiw(" + m["bot"] + "): " + g["title"])} '
                      f'&& echo "COMMITTED {m["bot"]}"; fi']
        lines += ['if [ -d "$MW" ]; then git -C "$MW" merge --abort >/dev/null 2>&1; git -C "$MW" checkout -q -f -B "$M" "$BASE" '
                  '|| { echo "MERGE_ERR 合并工作树重置失败"; exit 0; }; '
                  'else git -C "$R" worktree add -q -B "$M" "$MW" "$BASE" >/dev/null 2>&1 || { echo "MERGE_ERR 建不了合并工作树"; exit 0; }; fi']
        for m in local_members:
            b = shlex.quote(m["branch"])
            lines.append(f'N=$(git -C "$MW" rev-list --count "$M"..{b} 2>/dev/null || echo 0); '
                         f'if [ "$N" = "0" ]; then echo "EMPTY {m["bot"]}"; '
                         f'elif $G -C "$MW" merge -q --no-ff --no-edit -m {shlex.quote("AIW 管家合并 " + m["bot"])} {b} >/dev/null 2>&1; '
                         f'then echo "MERGED {m["bot"]}"; '
                         f'else F=$(git -C "$MW" diff --name-only --diff-filter=U | tr "\\n" ","); '
                         f'git -C "$MW" merge --abort >/dev/null 2>&1; echo "CONFLICT {m["bot"]} $F"; fi')
        lines += ['git -C "$MW" log --oneline "$BASE..$M" | head -20 | sed "s/^/LOG /"',
                  'echo "STAT $(git -C "$MW" diff --shortstat "$BASE" "$M")"', 'echo MERGE_DONE']
        return "\n".join(lines)

    async def merge(self, gid: str) -> dict:
        if gid in self._merging:
            return {"busy": True}
        g = self.group(gid)
        if not g:
            raise KeyError(gid)
        if not any(m.get("branch") and m.get("worktree") for m in g["members"]):
            r = parse_merge("")
            r["ok"] = True
            self.add(gid, "aiw", "AIW 管家", "当前群里全是云端成员，没有本地 git 分支需要合并。", {"merge": r, "report": True, "done": True})
            return r
        self._merging.add(gid)
        try:
            self.set_group(gid, status="merging")
            try:
                out = await self._sh(g["machine_id"], self.merge_script(g), 300)
                r = parse_merge(out)
                if not r["ok"] and not r["error"]:
                    r["error"] = B.clip(out.strip() or "电脑没有响应", 300)
            except Exception as e:  # noqa: BLE001
                r = parse_merge("")
                r["error"] = B.clip(e, 300)
            mb = f"aiw/{gid}/merge"
            name = lambda b: B.ENGINES.get(b, {}).get("title", b)  # noqa: E731
            if r["error"]:
                self.set_group(gid, status="idle", merge=r)
                self.add(gid, "aiw", "AIW 管家", f"合并没做成：{r['error']}", {"merge": r, "report": True})
                await self._tell_chat(self.group(gid), f"合并没做成：{r['error']}")
                return r
            parts = []
            if r["merged"]:
                parts.append("合并了 " + "、".join(name(b) for b in r["merged"]))
            if r["empty"]:
                parts.append("、".join(name(b) for b in r["empty"]) + " 没有改动")
            if r["conflicts"]:
                parts.append("冲突：" + "；".join(f"{name(b)}（{'、'.join(f) or '?'}）" for b, f in r["conflicts"].items()))
            g = self.group(gid)
            if r["conflicts"] and g["fixes"] < MAX_FIX:
                self.set_group(gid, status="fixing", merge=r, fixes=g["fixes"] + 1, pending=[])
                self.add(gid, "aiw", "AIW 管家", "；".join(parts) + f"。把冲突交回给对应的引擎去解决（第 {g['fixes'] + 1}/{MAX_FIX} 次）。",
                         {"merge": r, "report": True})
                g = self.group(gid)
                await self._assign(g, {b: (f"合并时和其他成员的改动冲突了，冲突文件：{'、'.join(f) or '见 git status'}。"
                                           f"请在当前分支执行 git merge {mb}（这是管家合并好的其他成员的改动），"
                                           "解决冲突，保留双方的功能，跑一下测试，然后 git commit。")
                                       for b, f in r["conflicts"].items()})
                return r
            done = not r["conflicts"]
            self.set_group(gid, status="idle", merge=r, pending=[])
            text = ("；".join(parts) or "没有可合并的改动") + (
                f"。结果在分支 {mb}（{r['stat'] or '无改动'}），没有推送，{g['base']} 没动。"
                f"\n确认没问题后在电脑上合进去：\ngit -C {g['repo']} merge --no-ff {mb}" if done and r["merged"] else
                "。冲突自动解决不了，需要你看一下。" if r["conflicts"] else "。")
            self.add(gid, "aiw", "AIW 管家", text, {"merge": r, "report": True, "done": done})
            await self._tell_chat(self.group(gid), text)
            return r
        finally:
            self._merging.discard(gid)

    async def _tell_chat(self, g: Optional[dict], text: str):
        n = self.bots.notifier
        if n and g:
            try:
                await n.notify("reply", f"引擎群聊「{g['title']}」", B.clip(re.sub(r"\s+", " ", text), 200),
                               deep=f"bots?g={g['id']}")
            except Exception as e:  # noqa: BLE001
                log.debug("group notify: %s", e)
        if self.chat and g and g.get("origin"):
            try:
                self.chat.add("assistant", "text", f"引擎群聊「{g['title']}」：{text}", {"bot_group": g["id"]})
            except Exception as e:  # noqa: BLE001
                log.warning("group to chat: %s", e)


# --------------------------------------------------------------------------------------------- 派活 from the main chat
LEAD = r"^\s*[@＠]\s*"   # v3.9.82: only 「@agy …」 (typed, or picked with @ / + 引擎) talks to an engine
SEP = r"\s*(?:(?:和|跟|与|、|,|，|/|\+|&|and)\s*[@＠]?|[@＠])\s*"


def _alias_at(s: str) -> Optional[tuple]:
    low = s.lower()
    best = None
    for eng, names in ALIASES:
        for n in names:
            if low.startswith(n) and (best is None or len(n) > len(best[1])):
                best = (eng, n)
    if not best:
        return None
    nxt = s[len(best[1]):len(best[1]) + 1]
    if nxt and re.match(r"[A-Za-z0-9_]", nxt):           # codexx, claude3: not the engine
        return None
    return best[0], len(best[1])


def parse_dispatch(text: str) -> Optional[dict]:
    """"@codex 修一下登录" -> {"bots": ["codex"], "task": "修一下登录", "workdir": "", "manage": False, "cont": False}."""
    t = (text or "").strip()
    m = re.match(LEAD, t)
    if not m:
        return None
    s = t[m.end():]
    bots: list = []
    for w in ALL_WORDS:
        if s.lower().startswith(w):
            bots = [e for e, _ in ALIASES]
            s = s[len(w):]
            break
    while not bots or re.match(SEP, s):
        if bots:
            s = s[re.match(SEP, s).end():]
        hit = _alias_at(s)
        if not hit:
            break
        if hit[0] not in bots:
            bots.append(hit[0])
        s = s[hit[1]:]
    if not bots:
        return None
    s = re.sub(r"^\s*(?:这几个引擎|几个引擎|引擎)?\s*(?:一起|分别|并行|都|各自)?\s*(?:去|来|帮我|帮忙|给我)?\s*[:：,，]?\s*",
               "", s, count=1)
    task = s.strip()
    if len(task) < 2:
        return None
    path = re.search(r"(?<![\w/])(~/[\w./\-\u4e00-\u9fff]+|/(?:home|Users|root|tmp|srv|opt|data|mnt|workspace)/"
                     r"[\w./\-\u4e00-\u9fff]+)", task)
    wd = path.group(1).rstrip("。.，,") if path else ""
    new = re.match(r"(?:新建|新开|开)(?:一个|个)?(?:新)?任务\s*[:：,，]?\s*(?:去|来)?", task)
    if new:   # v3.9.81: 「让 agy 新建任务，完成…」
        task = task[new.end():].strip() or task
    # v3.9.81: only these words make it a task; everything else is said in the engine's own chat
    cont = bool(re.match(r"(?:继续|接着)", task) or re.search(r"继续(?:做|完成|改|弄|跟进)?|接着做|(?:完成|做完|接着).{0,30}任务", task))
    manage = bool(re.search(r"盯着|直到|跟进|管家|做完为止|全程", task))
    wd = wd if ok_path(wd) else ""
    mode = "new" if new or len(bots) > 1 else ("continue" if cont else "chat")
    return {"bots": bots, "task": task, "workdir": wd, "manage": manage, "cont": cont, "mode": mode}


class Dispatch:
    """The main chat's hand-off to the engines (see Chat.send and the engine_task tool)."""

    def __init__(self, bots: "B.Bots", groups: Groups, chat=None):
        self.bots, self.groups, self.chat = bots, groups, chat
        bots.listeners.append(self._on_bot)

    parse = staticmethod(parse_dispatch)

    async def run(self, p: dict, attachments: Optional[list] = None, origin: str = "", machine_id: str = "") -> dict:
        """Start the work; returns {"text", "data"} for the chat's reply."""
        bots = [b for b in p["bots"] if b in B.ENGINES]
        task = p["task"]
        title = re.sub(r"\s+", " ", task)[:24]
        if len(bots) > 1:
            g = await self.groups.create(title, p.get("workdir") or "", bots, machine_id=machine_id, origin=origin or "chat")
            await self.groups.send(g["id"], "@管家 " + task, attachments)
            names = "、".join(B.ENGINES[b]["title"] for b in bots)
            return {"text": f"建了群聊「{g['title']}」：{names} 各开一个分支并行干，AIW 管家分工，做完自动合并，结果回到这里。",
                    "data": {"bot_group": g["id"], "bots": [{"bot": b} for b in bots]}}
        bot = bots[0]
        c = None
        if p.get("conv"):   # v3.9.64: an existing conversation the chat found (engine_convs): carry on in it
            c = self.bots.conv(p["conv"])
            if not c:
                raise ValueError(f"没有这个引擎对话：{p['conv']}")
            bot = c["bot"]
        elif p.get("mode") == "chat" and not p.get("workdir"):   # v3.9.81: no 新建任务 / 继续 words: its own chat
            # (v3.9.82: 「@codex 在 ~/workspace/x 里写个 hello」 names a folder: a task there, not the 单聊's folder)
            c = self.bots.dm(bot)
        elif p.get("mode") != "new" and not p.get("workdir"):
            c = self.named_task(bot, task)   # 「让 agy 继续 ldok 增加导出功能」: the ldok task, not a new one
            if not c and p.get("cont"):
                c = next(iter(self.tasks(bot)), None)
        if not c:
            c = self.bots.create(bot, title, p.get("workdir") or "", machine_id, origin=origin or "chat")
        elif not c.get("origin"):
            c = self.bots.set_conv(c["id"], origin=origin or "chat")
        att = []
        if attachments:
            try:
                att = await self.bots.push_files(c["id"], attachments)
            except Exception as e:  # noqa: BLE001
                log.warning("dispatch files: %s", e)
        eng = B.ENGINES[bot]["title"]
        where = f"「{c['title']}」" if p.get("conv") or p.get("cont") or c["created_at"] < time.time() - 5 else ""
        if p.get("manage"):
            self.bots.takeover(c["id"], task)
            text = f"交给 {eng}{where} 了，AIW 管家盯着它做完，结果回到这里。"
        else:
            self.bots.send(c["id"], task, "user", attachments=att, via="chat")
            if c.get("kind") == "dm":
                return {"text": f"发到 {eng} 的单聊了，它回复后结果回到这里。",
                        "data": {"bot_conv": c["id"], "bots": [{"bot": bot, "conv": c["id"]}]}}
            text = (f"在 {eng} 的任务{where}里接着做了，它回复后结果回到这里。" if where else
                    f"交给 {eng} 了（在电脑的 {c['workdir']} 里），它回复后结果回到这里。")
        return {"text": text, "data": {"bot_conv": c["id"], "bots": [{"bot": bot, "conv": c["id"]}]}}

    def tasks(self, bot: str) -> list:
        """v3.9.81: this engine's own tasks (not its 单聊, not group members), newest first."""
        return [x for x in self.bots.convs() if x["bot"] == bot and x.get("kind") != "dm" and not x.get("grp")]

    def named_task(self, bot: str, text: str) -> Optional[dict]:
        """v3.9.81: the task the words name, by its title or its folder's name (「ldok」 for ~/workspace/ldok)."""
        low = (text or "").lower()
        for x in self.tasks(bot):
            title = (x.get("title") or "").strip().lower()
            folder = [w for w in (x.get("workdir") or "").lower().rstrip("/").split("/") if w][-2:]
            names = [title] if len(title) >= 2 else []
            names += [w for w in folder if len(w) >= 3 and w not in ("main", "workspace", "home", "aiw")]
            names += [w for w in re.findall(r"[a-z][a-z0-9_\-]{2,}", title)]
            if any(n and n in low for n in names):
                return x
        return None

    def _on_bot(self, kind: str, obj):
        """A conversation started from the chat finished a turn: its reply goes back to the chat."""
        if kind != "message" or not obj or obj.get("role") != "bot" or not self.chat:
            return
        d = obj.get("data") or {}
        if d.get("status") in (None, "running") or d.get("to_chat"):
            return
        c = self.bots.conv(obj["conv"])
        if not c or not c.get("origin") or c.get("grp") or c.get("takeover"):
            return       # a group reports through its merge; under takeover the 管家's verdict is reported instead
        if c.get("kind") == "dm":   # v3.9.81: the engine's own chat reports only what the main chat asked
            u = self.bots.store._x("SELECT data FROM bot_messages WHERE conv=? AND role='user' AND seq<? "
                                   "ORDER BY seq DESC LIMIT 1", (c["id"], obj.get("seq") or 1 << 60))
            try:
                if not u or json.loads(u[0]["data"] or "{}").get("via") != "chat":
                    return
            except Exception:  # noqa: BLE001
                return
        self.bots.store._x("UPDATE bot_messages SET data=json_set(data, '$.to_chat', 1) WHERE id=?", (obj["id"],))
        eng = B.ENGINES.get(c["bot"], {}).get("title", c["bot"])
        try:
            self.chat.add("assistant", "text", f"{eng} 回复（{c['title']}）：\n\n{B.clip(obj.get('text') or '（没有文字）', 3000)}",
                          {"bot_conv": c["id"], "report": True})
        except Exception as e:  # noqa: BLE001
            log.warning("dispatch to chat: %s", e)


def report_takeover(bots: "B.Bots", chat):
    """Under takeover, the 管家's final report (done / ask / out of rounds) of a chat-started conversation goes back."""
    def on(kind, obj):
        if kind != "message" or not obj or obj.get("role") != "aiw" or not chat:
            return
        d = obj.get("data") or {}
        if not d.get("report") is True:
            return
        c = bots.conv(obj["conv"])
        if not c or not c.get("origin") or c.get("grp"):
            return
        try:
            chat.add("assistant", "text", f"AIW 管家（{c['title']}）：{B.clip(obj.get('text'), 2000)}",
                     {"bot_conv": c["id"], "report": True})
        except Exception as e:  # noqa: BLE001
            log.warning("takeover to chat: %s", e)
    bots.listeners.append(on)


# --------------------------------------------------------------------------------------------- HTTP
try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    BaseModel = object


class GroupIn(BaseModel):
    title: str = ""
    repo: str = ""
    members: list = []
    base: str = ""
    machine_id: str = ""
    auto_merge: bool = True
    goal: str = ""


class GroupProjectIn(BaseModel):
    action: str
    value: str = ""
    reason: str = ""


class GroupPermitIn(BaseModel):
    bot: str
    mode: str
    rule: str = ""


class GroupSendIn(BaseModel):
    text: str = ""
    attachments: list = []


class GroupMembersIn(BaseModel):
    members: list = []


def mount(app, groups: Groups, auth) -> None:
    from fastapi import Depends, HTTPException

    dep = [Depends(auth)]

    def need(gid: str) -> dict:
        g = groups.group(gid)
        if not g:
            raise HTTPException(404, "没有这个群聊")
        return g

    @app.get("/api/bots/groups", dependencies=dep)
    async def groups_list():
        return {"groups": groups.groups()}

    @app.get("/api/bots/groups-needs", dependencies=dep)
    async def groups_needs():
        """v3.9.88 需要你: every group waiting on the user, and for what."""
        return {"needs": groups.needs_list()}

    @app.post("/api/bots/groups", dependencies=dep)
    async def groups_create(body: GroupIn):
        try:
            g = await groups.create(body.title, body.repo, body.members, body.base, body.machine_id, body.auto_merge)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001  the computer is offline
            raise HTTPException(502, f"电脑没有响应：{B.clip(e, 200)}")
        if body.goal.strip():
            await groups.send(g["id"], body.goal)
        return groups.group(g["id"])

    @app.get("/api/bots/groups/{gid}", dependencies=dep)
    async def groups_get(gid: str, after: int = 0):
        g = need(gid)
        for m in g["members"]:
            if m.get("conv") and B.is_cloud_engine(m.get("bot") or ""):
                await groups.bots.sync_cloud(m["conv"])
        groups.mark_read(gid)
        g["unread"] = 0
        convs = {m["conv"]: groups.bots.conv(m["conv"]) for m in g["members"] if m.get("conv")}
        for m in g["members"]:
            c = convs.get(m.get("conv")) or {}
            m["busy"] = groups.bots.busy(m.get("conv") or "")
            m["status"] = c.get("status")
        return {"group": g, "messages": groups.messages(gid, after)}

    @app.post("/api/bots/groups/{gid}/members", dependencies=dep)
    async def groups_add_members(gid: str, body: GroupMembersIn):
        need(gid)
        try:
            return await groups.add_members(gid, body.members)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"添加成员失败：{B.clip(e, 200)}")

    @app.post("/api/bots/groups/{gid}/send", dependencies=dep)
    async def groups_send(gid: str, body: GroupSendIn):
        need(gid)
        try:
            return await groups.send(gid, body.text, body.attachments)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/bots/groups/{gid}/project", dependencies=dep)
    async def groups_project(gid: str, body: GroupProjectIn):
        """v3.9.86: 开工 (go) / 用方案 X (pick) / 交付 (deliver: patch|push|merge)."""
        need(gid)
        try:
            if body.action == "resume":   # v3.9.88: 重试 / 跳过 the step that stopped (any group, project or not)
                groups._bg(groups.resume(gid, body.value or "retry"))
                return {"ok": True}
            return await groups.project.action(gid, body.action, body.value, body.reason)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/bots/groups/{gid}/permit", dependencies=dep)
    async def groups_permit(gid: str, body: GroupPermitIn):
        """v3.9.88: allow / once / deny a member's blocked command from the group."""
        need(gid)
        try:
            return await groups.permit(gid, body.bot, body.mode, body.rule)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/bots/groups/{gid}/diff", dependencies=dep)
    async def groups_diff(gid: str, msg: str = "", whole: bool = False):
        """v3.9.88: one member turn's own changes (msg), or the whole result against the base branch (whole)."""
        need(gid)
        try:
            return await groups.diff(gid, msg, whole)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"电脑没有响应：{B.clip(e, 200)}")

    @app.post("/api/bots/groups/{gid}/merge", dependencies=dep)
    async def groups_merge(gid: str):
        need(gid)
        groups._bg(groups.merge(gid))
        return {"ok": True}

    @app.post("/api/bots/groups/{gid}/stop", dependencies=dep)
    async def groups_stop(gid: str):
        need(gid)
        return await groups.stop(gid)

    @app.delete("/api/bots/groups/{gid}", dependencies=dep)
    async def groups_delete(gid: str):
        need(gid)
        return {"ok": groups.delete(gid)}
