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

PLAN = """你是 AIW 管家，带一组 Agent 在群里协同工作：本地编程 Agent（agy、Codex、Claude Code、CodeBuddy、Grok）各自在独立的 git 分支和工作文件夹里写代码；云端 Agent（Meta Muse、Cue）只负责讨论、调研、方案设计、评审或资料汇总，绝不操作本地代码。
成员：{members}
根据用户的话分工。只输出一个 JSON 对象，不要别的文字：
{{"assign": {{"成员id": "发给它的具体任务", ...}}, "report": "一句话告诉用户怎么分的工"}}
要按顺序做（后一步要用前一步的结果）时，改用：
{{"steps": [{{"bot": "成员id 或 管家", "task": "这一步的具体任务"}}, ...], "report": "..."}}
规则：
- 用户已经点名谁做哪一步时，严格照用户的分配和顺序，不许换人、不许合并步骤；用户交给管家的步骤写 "bot": "管家"。
- 用户说“前一步完成后再交给下一步”或步骤之间有依赖时，必须用 steps。
- 成员id 只能用上面列出的（{ids}）；用不上的成员不要分。
- 写代码、改文件、修 bug、跑测试的任务只能分给本地编程成员（标有“本地代码”），绝不能分给云端成员（Meta Muse / Cue）。
- 给本地编程成员的任务要能独立完成，尽量改不同的文件，减少合并冲突；写清要改什么、怎么验证（跑什么测试或命令）。
- 给云端成员（标有“云端”）的任务只限讨论、调研、方案对比、代码/设计评审，不要让它写代码或跑本地命令。
- 任务本身不可拆时，交给最合适的一个成员，或者让两个成员各做一版供比较（在 report 里说明）。
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
    try:   # v3.9.79: serial steps waiting their turn
        store._x("ALTER TABLE bot_groups ADD COLUMN queue TEXT")
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


def _owner(line: str, ids: list) -> Optional[str]:
    """Who a step line names: the first @member (or @管家) on it."""
    for m in re.finditer(r"@\s?(claude code|grok build|meta muse|cue by manus|manus cue|[\w\u4e00-\u9fff]+)", line, re.I):
        name = m.group(1).lower()
        if name in MANAGER_WORDS:
            return MANAGER_ID
        for eng, names in ALIASES:
            if name in names and eng in ids:
                return eng
    return None


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
        who = _owner(lines[a], ids)
        task = "\n\n".join(x for x in (f"总目标：{goal}" if goal else "", f"你负责的是这一步：\n{body}",
                                         rules) if x)
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


class Groups:
    def __init__(self, bots: "B.Bots", chat=None):
        self.bots, self.store, self.chat = bots, bots.store, chat
        ensure_tables(self.store)
        self.listeners: list = []
        self._merging: set = set()
        self._tasks: set = set()
        bots.listeners.append(self._on_bot)

    # ------------------------------------------------------------------ storage
    def _row(self, r: dict) -> dict:
        d = dict(r)
        d["members"] = B._j(d.get("members"), [])
        d["pending"] = B._j(d.get("pending"), [])
        d["merge"] = B._j(d.get("merge"), None)
        d["queue"] = B._j(d.get("queue"), [])
        d["auto_merge"] = bool(d.get("auto_merge"))
        return d

    def group(self, gid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM bot_groups WHERE id=?", (gid,))
        if not r:
            return None
        g = self._row(r[0])
        g["busy"] = any(self.bots.busy(m["conv"]) for m in g["members"] if m.get("conv")) or gid in self._merging
        return g

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
        for k in ("members", "pending", "merge", "queue"):
            if k in kw:
                kw[k] = json.dumps(kw[k], ensure_ascii=False) if kw[k] is not None else None
        kw["updated_at"] = _now()
        self.store._x(f"UPDATE bot_groups SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", (*kw.values(), gid))
        g = self.group(gid)
        self._emit("group", g)
        return g

    def messages(self, gid: str, after: int = 0, limit: int = 300) -> list[dict]:
        rows = self.store._x("SELECT * FROM bot_group_messages WHERE grp=? AND seq>? ORDER BY seq DESC LIMIT ?",
                             (gid, int(after or 0), int(limit)))
        return [B.msg_row(r) for r in reversed(rows)]

    def add(self, gid: str, role: str, who: str, text: str, data: Optional[dict] = None, src: str = "") -> dict:
        mid = f"gm-{uuid.uuid4().hex[:12]}"
        self.store._x("INSERT INTO bot_group_messages (id, grp, role, who, text, data, src, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (mid, gid, role, who, text or "", json.dumps(data or {}, ensure_ascii=False), src or None, _now()))
        self.store._x("UPDATE bot_groups SET updated_at=? WHERE id=?", (_now(), gid))
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
        return self.set_group(gid, status="idle", pending=[], queue=[])

    # ------------------------------------------------------------------ talking
    def member(self, g: dict, bot: str) -> Optional[dict]:
        return next((m for m in g["members"] if m["bot"] == bot), None)

    def _context(self, g: dict, m: dict, limit: int = 6000) -> str:
        """v3.9.79: what the group said since the last plan, so each member sees the earlier steps' results."""
        rows = self.store._x("SELECT role, who, text, data FROM bot_group_messages WHERE grp=? ORDER BY seq DESC LIMIT 40",
                             (g["id"],))
        out, size = [], 0
        for r in rows:
            d = B._j(r["data"], {})
            if r["role"] == "aiw" and (d.get("plan") or d.get("steps")):
                break
            if r["role"] == "system" or not (r["text"] or "").strip():
                continue
            if r["role"] == "bot" and r["who"] == m["bot"]:
                continue
            name = B.ENGINES.get(r["who"], {}).get("title", r["who"]) if r["role"] == "bot" else (r["who"] or "用户")
            line = f"【{name}】{B.clip(r['text'].strip(), 2500)}"
            if size + len(line) > limit:
                break
            out.append(line)
            size += len(line)
        return "\n\n".join(reversed(out))

    def _brief(self, g: dict, m: dict, task: str, fix: bool = False, context: str = "") -> str:
        others = "、".join(B.ENGINES[x["bot"]]["title"] for x in g["members"] if x["bot"] != m["bot"]) or "（没有）"
        ctx = f"\n\n群里前面的成员已经给出的结果（以此为准）：\n{context}" if context else ""
        if m.get("cloud") or B.is_cloud_engine(m["bot"]):
            head = (f"［引擎群聊「{g['title']}」］你是群里的云端协作成员，和你一起协作的成员：{others}。\n"
                    "AIW 会把你这次的回复原样贴到群里，不需要你去找群聊、加入群或给群发消息，也不要打开链接或调用工具去确认身份。"
                    "请直接完成下面分配给你的任务（调研、方案、评审或回答问题），给出清晰、结构化的结论，不需要操作本地文件或 git。")
            return head + ctx + "\n\n" + task
        head = (f"［引擎群聊「{g['title']}」］你在 git 仓库的独立工作树里干活：当前文件夹，分支 {m['branch']}"
                f"（从 {g['base']} 开出" + ("，已经合入前面步骤的成果" if context else "") + f"）。其他成员：{others}；最后由 AIW 管家合并。\n"
                "AIW 会把你的回复贴到群里。规则：只改当前文件夹里的文件；做完 git add 并 git commit（中文说明）；不要 push，不要切换分支，"
                + ("" if fix else "不要合并别人的分支，") + "不要改仓库外的文件。最后用几句话说明改了什么、怎么验证的。")
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
        if g.get("queue") and re.fullmatch(r"\s*(继续|接着做|下一步|continue)\s*[。.!！]?\s*", rest or ""):
            self._bg(self._run_next(gid))
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
                plan = parse_plan(out, ids)
            except Exception as e:  # noqa: BLE001
                log.warning("group plan: %s", e)
        if plan and plan.get("steps"):
            await self._start_steps(gid, plan["steps"], plan["report"], attachments)
            return
        if not plan:
            plan = {"assign": {b: text for b in ids},
                    "report": "没分开，每个人都拿到同一个任务，各做一版" if len(ids) > 1 else ""}
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
        g = self.set_group(gid, queue=q, status="working")
        done = [r["who"] for r in self.store._x(
            "SELECT DISTINCT who FROM bot_group_messages WHERE grp=? AND role='bot'", (gid,))]
        if s["bot"] == MANAGER_ID:
            await self._final(gid, s["task"])
            return
        m = self.member(g, s["bot"])
        if not m or not m.get("conv"):
            self.add(gid, "aiw", "AIW 管家", f"第 {s['n']} 步的 {s['bot']} 不在群里，跳过。")
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
        self.set_group(gid, pending=[s["bot"]])
        att = []
        if s.get("attachments"):
            try:
                att = await self.bots.push_files(m["conv"], s["attachments"])
            except Exception as e:  # noqa: BLE001
                log.warning("group files: %s", e)
        self.add(gid, "aiw", "AIW 管家", f"第 {s['n']}/{s['of']} 步交给 {B.ENGINES[m['bot']]['title']}。")
        self.bots.send(m["conv"], self._brief(g, m, s["task"], context=self._context(g, m)), "aiw", attachments=att)

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

    async def _final(self, gid: str, task: str):
        """The step the user gave AIW 管家 itself: merge the local work, run the checks it names, and report."""
        g = self.group(gid)
        has_local = any(m.get("branch") and m.get("worktree") for m in g["members"])
        merge_note, runs = "", []
        if has_local:
            r = await self.merge(gid)
            g = self.group(gid)
            if g["status"] == "fixing":   # conflicts went back to the members; report once they have fixed them
                self.set_group(gid, queue=[{"bot": MANAGER_ID, "task": task, "step": task, "n": 0, "of": 0}])
                return
            merge_note = f"合并：{'、'.join(r.get('merged') or []) or '无'}；{r.get('stat') or ''}"
            mwt = f"~/workspace/.aiw-worktrees/{gid}/_merge"
            for c in self._verify_cmds(task):
                try:
                    out = await self._sh(g["machine_id"], f"cd {qp(mwt)} && timeout 300 {c} 2>&1 | tail -n 40; echo \"EXIT ${{PIPESTATUS[0]}}\"", 330)
                except Exception as e:  # noqa: BLE001
                    out = f"运行失败：{e}"
                runs.append((c, out.strip()))
        timeline = self._context(g, {"bot": ""}, 12000)
        checks = "\n\n".join(f"$ {c}\n{B.clip(o, 2000)}" for c, o in runs)
        text = ""
        if self.bots.llm:
            try:
                sys_p = ("你是 AIW 管家，负责验收一次多引擎分工并向用户汇报。只依据给你的群聊记录、合并结果和你亲自运行的命令输出，"
                         "不要编造。用中文；先给一张 markdown 表（成员、负责什么、产出哪些文件、是否一次完成、遇到的问题），"
                         "再写几句评价（交接是否顺畅、谁越权、最终测试结果、下次怎么改进）。没有的数据写“未知”。")
                text = await asyncio.wait_for(self.bots.llm().complete(
                    sys_p, f"用户交给你的验收任务：\n{task}\n\n{merge_note}\n\n你运行的命令：\n{checks or '（没有）'}\n\n群聊记录：\n{timeline}", 2500), 120)
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
        for bot, task in assign.items():
            m = self.member(g, bot)
            if not m or not m.get("conv"):
                continue
            att = []
            if attachments:
                try:
                    att = await self.bots.push_files(m["conv"], attachments)
                except Exception as e:  # noqa: BLE001
                    log.warning("group files: %s", e)
            self.bots.send(m["conv"], self._brief(g, m, task, fix=g["status"] == "fixing"), "aiw", attachments=att)

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
            "steps": len(d.get("steps") or []), "conv": c["id"]}, src=obj["id"])
        pending = [b for b in g["pending"] if b != bot]
        g = self.set_group(gid, pending=pending)
        if not pending and g["status"] in ("working", "fixing"):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            if g.get("queue"):
                if g["status"] == "working" and d.get("status") in ("turn_failed", "blocked", "failed", "error"):
                    self.set_group(gid, status="idle")
                    self.add(gid, "aiw", "AIW 管家", f"{B.ENGINES.get(bot, {}).get('title', bot)} 这一步没做成，后面的步骤先停下。"
                             "处理好后说「@管家 继续」接着往下做。")
                    loop.create_task(self._tell_chat(g, f"{B.ENGINES.get(bot, {}).get('title', bot)} 没做成，分工暂停了。"))
                    return
                self._bg(self._run_next(gid))
                return
            has_local = any(m.get("branch") for m in g["members"])
            if g["auto_merge"] and has_local:
                self._bg(self.merge(gid))
            else:
                self.set_group(gid, status="idle")
                note = "都回复了。看一下各自的结果，需要合并就点「合并」。" if has_local else "群里的成员都回复了。"
                self.add(gid, "aiw", "AIW 管家", note)
                loop.create_task(self._tell_chat(g, "群里的引擎都做完了" + ("，等你看一下再合并。" if has_local else "。")))


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
LEAD = r"^\s*(?:请|麻烦|帮我)?\s*(?:让|叫|派|交给|发给|安排给?|用|找|@)\s*"
SEP = r"\s*(?:和|跟|与|、|,|，|/|\+|&|and)\s*"


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
    """"让 codex 修一下登录" -> {"bots": ["codex"], "task": "修一下登录", "workdir": "", "manage": False, "cont": False}."""
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
    if not bots or re.match(r"\s*(?:的|是|怎么|吗|能不能|可以吗|有什么)", s):
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
        elif p.get("mode") == "chat":   # v3.9.81: no 新建任务 / 继续 words: the engine's own chat, always
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
