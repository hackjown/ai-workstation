"""v3.9.30: the Codex / Claude Code style loop for coding / dev / fix tasks.

DevFlow (devflow.py, v3.9.26-29) already holds the task contract, provenance, the done gate and the stuck patterns.
DevLoop is the per-task working state on top of it, the part that makes the agent work like a careful engineer:

1. lean toolset   read_file (line numbers, offset/limit), grep, glob, files_patch (exact unique replace), shell,
                  todo_write, done with a receipt outcome (done / nothing / failed / ask). Ad-hoc fix_*.py edit
                  scripts are refused in dev mode with a tool error pointing at files_patch (AIW_DEV_BLOCK_FIX_SCRIPTS).
2. explore first  an EXISTING file may only be edited after it was read / grepped in this phase (read-before-edit,
                  per file), and a fix needs a source of truth: a contract without one adopts the first explored
                  source file the agent edits. A generated artifact (contract deliverable / generator output) is refused
                  with a message naming its source. A brand-new project (the dev skill's PLAN.md -> prototype ->
                  ask_user -> implement flow) is exempt: creating files is never blocked, and files this task created
                  count as known.
3. todos          seeded from the contract's acceptance items (and the requirement checks); one item in_progress at a
                  time; the done receipt lists every item; claims in the summary about items that are not verified
                  done are flagged.
4. verification   a todo item can only be marked done when a verification command (tests / generator / verify_html /
                  diff check) ran after the last edit of its related files; otherwise that todo_write is rejected.
5. generator      with generator_cmd_fast set, a full generator run is refused until the fast iteration is verified,
                  and a rerun with no source change since the last run is refused.
6. compaction     older steps (out of the 25-line window) are folded into a working memory: files read / edited,
                  decisions, errors, open todos; optionally summarised by a cheap (flash) model. Shown in the task
                  text every turn and persisted (kv task_devmem:<task>) so a 继续 run resumes instead of restarting.
7. stuck          the same error signature 3 times -> back to the explore phase (re-read before the next edit);
                  the step budget (AIW_DEV_STEP_BUDGET) exceeded -> an ask receipt to the user instead of going on.
8. dangerous      a blocklist (rm -rf ~ or /, git reset --hard, git clean, git push --force, sudo, curl|sh, mkfs, dd …)
                  is refused with a tool error; in dev mode a command the policy would confirm is refused too
                  (no confirm pop-ups during a coding loop; AIW_DEV_NO_CONFIRM=0 restores them).
Pure state, no I/O: loop.py calls in. Tested in tests/test_v3930_devloop.py.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import provenance as _pv
from .provenance import norm

STATUSES = ("pending", "in_progress", "done")
OUTCOMES = ("done", "nothing", "failed", "ask")
DEV_TOOLS = ("read_file", "grep", "glob", "todo_write")
KIND_CN = {"artifact": "拒绝：这是脚本生成的产物，请改源头脚本后重新生成",
           "explore": "先读后改：修改现有文件前先 read_file / grep 读到要改的代码",
           "fix_script": "拒绝补丁脚本：直接用 files_patch 改源文件",
           "blocklist": "危险命令已拦截（开发任务黑名单）",
           "generator": "生成器：迭代用快速命令 / 没有改动不重复生成",
           "confirm": "需要确认的命令已拦截（编码任务不弹确认框）"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def step_budget(new_build: bool = False) -> int:
    """Steps a dev task runs before it stops and asks with a receipt (0 = off)."""
    return _env_int("AIW_DEV_STEP_BUDGET_NEW", 250) if new_build else _env_int("AIW_DEV_STEP_BUDGET", 100)


def block_fix_scripts() -> bool:
    return os.environ.get("AIW_DEV_BLOCK_FIX_SCRIPTS", "1") != "0"


def no_confirm() -> bool:
    return os.environ.get("AIW_DEV_NO_CONFIRM", "1") != "0"


# ------------------------------------------------------------------------------------------------ dangerous commands
DANGEROUS = [
    (r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:-\S+\s+)*['\"]?(?:/|~/?|\$HOME/?|\$\{HOME\}/?|/home/[\w.-]+/?|/data/home/?|"
     r"/root/?|/etc\S*|/usr\S*|/var\S*|\.\.?/?)\*?['\"]?(?=\s|$|[;&|])", "删除整个根目录/家目录/系统目录"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard 会丢掉未提交的改动（系统已做快照，回滚请让用户点「回滚」）"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*[fdx]", "git clean 会删除未跟踪文件"),
    (r"\bgit\s+checkout\s+(?:--\s+)?\.(?:\s|$)|\bgit\s+restore\s+(?:--\S+\s+)*\.(?:\s|$)", "git checkout/restore . 会丢掉全部改动"),
    (r"\bgit\s+push\b[^;&|\n]*(?:--force\b|-f\b|--force-with-lease)", "强制推送"),
    (r"\bgit\s+push\b", "推送代码（发布前由用户决定）"),
    (r"\bgit\s+(?:stash|branch\s+-D|rebase)\b", "改写 git 状态（系统管理分支/快照）"),
    (r"\bsudo\b", "sudo 提权"),
    (r"(?:curl|wget)[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", "下载脚本直接执行"),
    (r"\bmkfs\b|\bdd\s+[^\n]*\bof=/dev/|>\s*/dev/sd", "写磁盘设备"),
    (r"\bchmod\s+-R\s+777\b|\bchown\s+-R\b", "递归改权限"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bkill(?:all)?\s+-9\s+-1\b|\bpkill\s+-9\s+-u\b", "杀掉所有进程"),
    (r"\b(?:shutdown|reboot|poweroff|halt)\b", "关机/重启"),
]


def dangerous(command: str) -> Optional[str]:
    """The reason a shell command is on the dev blocklist, else None."""
    c = command or ""
    for pat, why in DANGEROUS:
        if re.search(pat, c):
            return why
    return None


_LOCAL_INSTALL = re.compile(r"^\s*(?:cd\s+[^;&|]+&&\s*)?(?:(?:\.?venv/bin/)?(?:pip3?|python3?\s+-m\s+pip)\s+install\b|"
                            r"(?:npm|pnpm)\s+(?:install|i|ci|add)\b|yarn(?:\s+(?:install|add))?\b|python3?\s+-m\s+venv\b)")


def local_install(command: str) -> bool:
    """A project-local package install (pip/npm/pnpm/yarn, no sudo / global / system packages): in a coding task it
    runs without a confirm pop-up instead of being refused."""
    c = command or ""
    return bool(_LOCAL_INSTALL.search(c)) and not re.search(
        r"\bsudo\b|\s-g\b|--global|--break-system-packages|\b(apt|apt-get|dnf|yum|snap|brew)\b|(curl|wget)[^|]*\|", c) \
        and not re.search(r"[;|]|&&(?!.*\b(pip|npm|pnpm|yarn)\b)", re.sub(r"^\s*cd\s+[^;&|]+&&", "", c))


# ------------------------------------------------------------------------------------------------ error signatures
_ERR_LINE = re.compile(r"(\w*(?:Error|Exception|Warning)\b[:\s].*|error[:\s].*|FAILED.*|fatal:.*|"
                       r"expected .* match\(es\).*|no such file.*|command not found.*|SyntaxError.*)", re.I)


def error_signature(text: str) -> Optional[str]:
    """A normalised one-line signature of an error output (numbers, paths, quoted strings and hex folded)."""
    t = str(text or "").strip()
    if not t:
        return None
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    hit = None
    for ln in reversed(lines):
        if _ERR_LINE.search(ln):
            hit = _ERR_LINE.search(ln).group(0)
            break
    if hit is None:
        hit = lines[-1]
    s = re.sub(r"(?:~|/)[\w./@+-]*/([\w.@+-]+)", r"\1", hit)          # paths -> basename
    s = re.sub(r"0x[0-9a-f]+", "HEX", s, flags=re.I)
    s = re.sub(r"(['\"]).{0,80}?\1", "'…'", s)
    s = re.sub(r"\d+", "N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160] or None


# ------------------------------------------------------------------------------------------------ helpers
def _base(p: str) -> str:
    return os.path.basename(norm(p).rstrip("/"))


_GREP_PATH = re.compile(r"^((?:~|/|\.{0,2}/)?[\w./@+-]+\.[A-Za-z0-9]{1,6}):\d+[:-]", re.M)
_VERIFY_CMD = re.compile(r"\b(pytest|py\.test|python3? -m (pytest|unittest)|npm (run )?(test|build|lint)|pnpm (test|build)|"
                         r"yarn (test|build)|node --test|go (test|vet|build)|cargo (test|check|build)|make (test|check)|"
                         r"vitest|jest|tsc\b|eslint|ruff|flake8|mypy|py_compile|python3? -m compileall|git diff\b|"
                         r"\bdiff\s+-[a-zA-Z]*u|\bcmp\b|node -c|bash -n)\b")


def _kw(text: str) -> list[str]:
    """Words of a todo item that a summary claiming it would contain (CJK 2-4 grams + latin words)."""
    t = re.sub(r"（[^）]*）|\([^)]*\)|\[[^\]]*\]", " ", str(text or ""))
    out = []
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", t):
        if len(seg) <= 6:
            out.append(seg)
        else:
            out += [seg[i:i + 4] for i in range(0, len(seg) - 3, 2)]
    out += [w.lower() for w in re.findall(r"[A-Za-z][\w#.-]{3,}", t)]
    stop = {"verify_html", "selectors", "clicks", "done", "修改源头", "重新运行", "验证的是", "自动检查", "总结里写"}
    return [w for w in dict.fromkeys(out) if w not in stop][:8]


class DevLoop:
    def __init__(self, flow=None):
        self.flow = flow                       # the DevFlow (contract, book, gen runs, verify)
        self.new_build = False                 # a brand-new project (dev skill flow): edit gates relaxed
        self.phase = "explore"                 # explore | edit
        self.explored: set = set()             # files read / searched in this phase (norm paths and basenames)
        self.created: set = set()              # files this task created (known without a read)
        self.edits: dict = {}                  # file -> last edit step
        self.reads_log: list = []              # [(step, path)] for the working memory
        self.verifs: list = []                 # [(step, kind, what)]
        self.todos: list = []                  # [{"id", "content", "status", "files", "auto", "evidence"}]
        self.err_counts: dict = {}             # error signature -> count since the last explore reset
        self.forced_explore = 0
        self.decisions: list = []              # short lines: user answers, refusals, key thoughts
        self.errors: list = []                 # [(step, signature)]
        self.memory = ""                       # the compacted summary of older steps
        self.compacted_upto = 0                # history index already folded into memory
        self.resumed = False
        self.budget_from = 0                   # the step the current step budget counts from
        self.budget_extra = 0
        self.flagged: list = []                # receipt flags (unverified claims, open todos)
        self.last_kind = ""                    # what the last pre() refusal was (for the timeline label)

    # ------------------------------------------------------------------ todos
    def seed_todos(self, contract: Optional[dict]) -> None:
        """Todo items from the contract's acceptance items (requirement checks / regen / regression are auto)."""
        if self.todos:
            return
        c = contract or {}
        reqs = {r["id"]: r for r in c.get("req_checks") or []}
        for a in c.get("acceptance") or []:
            m = re.match(r"\[([\w-]+)\]\s*(.+)", a)
            auto = None
            if m and m.group(1) in reqs:
                auto, text = "req:" + m.group(1), m.group(2)
            elif "重新运行生成命令" in a:
                auto, text = "regen", a
            elif "与上一版交付物对比" in a:
                auto, text = "regression", a
            else:
                text = a
            self.todos.append({"id": str(len(self.todos) + 1), "content": re.sub(r"（verify_html[^）]*）", "", text).strip()[:200],
                               "status": "pending", "files": [], "auto": auto, "evidence": ""})

    def todo_text(self) -> str:
        if not self.todos:
            return ""
        mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        lines = [f"  {mark.get(t['status'], '[ ]')} {t['id']}. {t['content']}" + (f"  (证据：{t['evidence']})" if t.get("evidence") else "")
                 for t in self.todos]
        return ("\n\nTODO（todo_write 维护；同一时间只能有一项 in_progress；标 done 前必须在最后一次修改相关文件之后跑过验证："
                "测试/生成器/verify_html/diff）：\n" + "\n".join(lines))

    def last_edit(self, files: Optional[list] = None) -> int:
        """The step of the last edit of these files (all edits when none are named)."""
        steps = []
        for f, st in self.edits.items():
            if not files or any(norm(x) == f or _base(x) == _base(f) for x in files):
                steps.append(st)
        if self.flow is not None and not files:
            steps.append(int(getattr(self.flow, "last_source_step", 0) or 0))
        return max(steps or [0])

    def evidence_after(self, step: int) -> Optional[tuple]:
        later = [v for v in self.verifs if v[0] > step]
        return later[-1] if later else None

    def todo_write(self, step: int, items: list) -> tuple[bool, str]:
        """Replace the todo list. -> (ok, message). Rejected items keep their previous status."""
        if not isinstance(items, list) or not items:
            return False, "REJECTED: todo_write needs `todos`: a list of {content, status: pending|in_progress|done}."
        prev = {t["id"]: t for t in self.todos}
        by_content = {t["content"]: t for t in self.todos}
        new, rejected, bad = [], [], []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            content = str(it.get("content") or "").strip()[:200]
            st = str(it.get("status") or "pending").strip().lower().replace("completed", "done").replace("in-progress", "in_progress")
            if st not in STATUSES:
                bad.append(content or str(i))
                st = "pending"
            tid = str(it.get("id") or "") or (by_content.get(content) or {}).get("id") or str(len(new) + 1)
            old = prev.get(tid) or by_content.get(content) or {}
            t = {"id": tid, "content": content or old.get("content") or f"item {tid}", "status": st,
                 "files": [norm(x) for x in (it.get("files") or old.get("files") or []) if x][:10],
                 "auto": old.get("auto"), "evidence": old.get("evidence") or ""}
            if st == "done" and old.get("status") != "done":
                ev = self.evidence_after(self.last_edit(t["files"] or None))
                if not ev:
                    t["status"] = "in_progress" if old.get("status") == "in_progress" else (old.get("status") or "pending")
                    rejected.append(t)
                else:
                    t["evidence"] = f"第 {ev[0]} 步 {ev[1]}：{ev[2][:60]}"
            new.append(t)
        prog = [t for t in new if t["status"] == "in_progress"]
        if len(prog) > 1:
            return False, ("REJECTED: only ONE todo may be in_progress at a time (got "
                           + ", ".join(t["id"] for t in prog) + "). Finish or reset the others to pending.")
        self.todos = new
        msg = "todos updated: " + ", ".join(f"{t['id']}={t['status']}" for t in new)
        if rejected:
            le = self.last_edit()
            msg = ("PARTLY REJECTED: these items cannot be done yet, no verification ran after the last edit of their "
                   f"files (last edit at step {le}): " + "; ".join(f"{t['id']}. {t['content'][:60]}" for t in rejected)
                   + ". Run the tests / the generator / verify_html (or a diff check) first, then mark them done. " + msg)
        if bad:
            msg += f" (unknown status for {bad}: use pending|in_progress|done)"
        return not rejected, msg

    def auto_verify(self, step: int) -> list[str]:
        """System-verifiable todos: requirement checks that passed, a regeneration after the last source change."""
        out = []
        f = self.flow
        if f is None:
            return out
        for t in self.todos:
            if t["status"] == "done" or not t.get("auto"):
                continue
            le = self.last_edit()
            if t["auto"].startswith("req:"):
                st = (f.req_status or {}).get(t["auto"][4:])
                if st and st.get("ok") and st.get("step", -1) >= le:
                    t["status"], t["evidence"] = "done", f"第 {st['step']} 步 verify_html 需求检查通过"
                    out.append(t["id"])
            elif t["auto"] == "regen":
                runs = [s for s in (f.gen_runs or []) if s >= le and le > 0]
                if runs:
                    t["status"], t["evidence"] = "done", f"第 {runs[-1]} 步重新生成"
                    out.append(t["id"])
        return out

    # ------------------------------------------------------------------ evidence / reads / edits
    def record_read(self, step: int, paths) -> None:
        for p in paths or ():
            if not p:
                continue
            n = norm(str(p))
            self.explored.add(n)
            self.explored.add(_base(n))
            self.reads_log.append((step, n))
        self.reads_log = self.reads_log[-200:]

    def record_grep_output(self, step: int, stdout: str) -> None:
        self.record_read(step, [m.group(1) for m in _GREP_PATH.finditer(stdout or "")][:100])

    def record_verify(self, step: int, kind: str, what: str = "") -> None:
        self.verifs.append((step, kind, str(what or "")[:120]))
        self.verifs = self.verifs[-60:]

    def known(self, path: str) -> bool:
        n = norm(path)
        return n in self.explored or _base(n) in self.explored or n in self.created or _base(n) in self.created

    def record_edit(self, step: int, path: str, created: bool = False) -> None:
        n = norm(path)
        self.edits[n] = step
        if created:
            self.created.add(n)
        self.phase = "edit"

    # ------------------------------------------------------------------ gates before a tool runs
    def edit_targets(self, tool: str, args: dict) -> list[str]:
        """Existing-file edit targets of a call (files_patch / files_write / sed -i / perl -i)."""
        a = args or {}
        if tool in ("files_patch", "files_write"):
            return [_pv._ws(str(a.get("path") or ""))] if a.get("path") else []
        if tool != "shell":
            return []
        c = str(a.get("command") or "")
        out = []
        for m in re.finditer(r"\bsed\s+(?:-[a-zA-Z]*i\S*|--in-place\S*)\s+(?:(?:-e\s+)?(?:'[^']*'|\"[^\"]*\")\s+)+([^\s;&|]+)", c):
            out.append(norm(m.group(1)))
        for m in re.finditer(r"\bperl\s+-[a-zA-Z]*i\S*\s+(?:-[a-zA-Z]+\s+)*(?:'[^']*'|\"[^\"]*\")\s+([^\s;&|]+)", c):
            out.append(norm(m.group(1)))
        return out

    def artifact_of(self, path: str) -> Optional[str]:
        """The source that generates `path` when it is a generated artifact of this contract, else None."""
        f = self.flow
        if f is None:
            return None
        c = f.contract or {}
        n = norm(path)
        srcs = {norm(s) for s in (c.get("allowed_paths") or []) if _pv.SOURCE_EXT.search(s or "") and not
                re.search(r"\.html?$", s or "")}
        if n in srcs:
            return None
        outs = {norm(x) for x in (c.get("deliverables") or [])} | {norm(x) for x in (f.gen_outputs or [])}
        if n in outs or any(_base(n) == _base(o) and _base(n) for o in outs if re.search(r"\.html?$", n)):
            return c.get("source_of_truth") or f.book.generator(n) or "生成脚本"
        g = f.book.generator(n) if f.book else None
        return g

    def fix_script(self, tool: str, args: dict) -> Optional[str]:
        """An ad-hoc edit script (fix_*.py, or any script whose job is to rewrite a file) written in dev mode."""
        w = _pv.written_script(tool, args)
        if not w:
            return None
        path, content = w
        from .devflow import payload_sig
        edits_file = bool(payload_sig(content)) or bool(re.search(r"write_text|\.write\(|open\([^)]*['\"][wa]", content or ""))
        if _pv.ADHOC.search(path) and edits_file:
            return path
        if edits_file and re.search(r"\.replace\(|re\.sub", content or "") and not re.search(r"(^|/)test_[\w-]+\.py$", path):
            srcs = self.flow.sources() if self.flow is not None else ()
            sot = ((self.flow.contract or {}).get("source_of_truth") if self.flow is not None else None)
            if _pv.script_targets(content, tuple(srcs) + tuple(x for x in [sot] if x)):
                return path
        return None

    def pre(self, step: int, tool: str, args: dict, exists: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
        """-> ("skip", tool error) or (None, None). `exists`: {path: bool} the loop checked on the computer."""
        a = args or {}
        exists = exists or {}
        if tool == "shell":
            why = dangerous(str(a.get("command") or ""))
            if why:
                self.last_kind = "blocklist"
                return "skip", (f"BLOCKED (dev blocklist): {why}. 这条命令在开发任务里被禁止，不会弹确认框。换一种做法；"
                                "确实需要时用 done(outcome=\"ask\") 把原因交给用户决定。")
        if block_fix_scripts():
            fs = self.fix_script(tool, a)
            if fs:
                self.last_kind = "fix_script"
                sot = ((self.flow.contract or {}).get("source_of_truth") if self.flow is not None else None) or "源文件"
                return "skip", (f"REFUSED: 开发任务里不要写 {fs} 这种改文件的补丁脚本（task-68b：11 个 fix 脚本大多没执行）。"
                                f"直接用 files_patch 改 {sot}：path=<文件>，old=<从 read_file 复制的精确原文，唯一匹配>，new=<替换内容>；"
                                "改完立刻跑验证（测试/生成器/verify_html）。Use files_patch, not an edit script.")
        targets = self.edit_targets(tool, a)
        if not targets:
            return None, None
        for t in targets:
            gen = self.artifact_of(t)
            if gen and not self.new_build:
                self.last_kind = "artifact"
                return "skip", (f"REFUSED: {t} 是 {gen} 生成的产物，不能直接改。改源头 {gen}（先 read_file/grep 找到生成这段内容的"
                                f"模板/代码），再重新生成。Edit the source {gen}, then regenerate.")
        for t in targets:
            if tool == "files_write" and exists.get(t) is not True:
                continue                       # creating a file is never blocked (new projects, new modules, tests)
            if self.known(t):
                continue
            if self.new_build and (t.startswith("~/workspace") or "/workspace/" in t):
                continue                       # a new project's own folder: the dev skill's build flow, not a fix
            self.last_kind = "explore"
            hint = ("（连续 3 次同样的错误，已回到探索阶段，需要重新读代码）" if self.forced_explore else "")
            return "skip", (f"REFUSED (explore before edit){hint}: 修改现有文件 {t} 之前先读它：read_file(path={t}, "
                            "offset/limit 看相关行) 或 grep 找到要改的位置，确认源头（生成器/模板/源码）后再用 files_patch 改。"
                            "Read or grep the file first; edits are only accepted on code you have looked at.")
        return None, None

    def adopt_source(self, path: str) -> bool:
        """A fix with no source of truth adopts the first explored source file it edits."""
        f = self.flow
        if f is None or self.new_build:
            return False
        c = f.contract
        if c is None or c.get("source_of_truth"):
            return False
        n = norm(path)
        if not _pv.SOURCE_EXT.search(n) or re.search(r"\.html?$", n) or _pv.ADHOC.search(n) or not self.known(n):
            return False
        c["source_of_truth"], c["source_how"] = n, "explored"
        allowed = c.setdefault("allowed_paths", [])
        for p in (n, os.path.dirname(n)):
            if p and p not in allowed:
                allowed.append(p)
        c["confirm"] = False
        return True

    # ------------------------------------------------------------------ generator
    def generator_gate(self, step: int, command: str) -> Optional[str]:
        """Refuse a wasteful generator run (full when a fast one exists and is not verified yet; any rerun with no change)."""
        f = self.flow
        if f is None or not f.generator:
            return None
        c = f.contract or {}
        sc = _pv.script_of(command or "")
        if not sc or _base(sc) != _base(f.generator):
            return None
        from .dev_contract import is_fast_run
        fast = is_fast_run(command)
        last_src = int(f.last_source_step or 0)
        last_full = max(f.gen_runs or [0])
        last_fast = max(f.fast_runs or [0])
        if not fast and f.gen_runs and last_full >= last_src:
            return (f"SKIPPED: 生成器在第 {last_full} 步已经完整运行过，之后源头没有改动，产物是最新的，不要重复完整生成。"
                    "直接验证产物（verify_html/测试），或先改源头。")
        if fast and f.fast_runs and last_fast >= last_src and last_fast >= last_full and last_src > 0:
            return (f"SKIPPED: 快速生成在第 {last_fast} 步已经跑过，之后源头没有改动。先改源头或直接验证。")
        if not fast and c.get("generator_cmd_fast"):
            ok_after = [s for s in (f.verify_ok or []) + (f.test_ok or []) if s >= max(last_fast, 1)]
            if not (last_fast >= last_src and last_fast > 0 and ok_after):
                return (f"SKIPPED: 迭代时用快速生成：{c['generator_cmd_fast']}（读缓存，不重新抓取），验证通过后再完整跑一次 "
                        f"{c.get('generator_cmd') or f.generator}。Use the fast generator command while iterating.")
        return None

    # ------------------------------------------------------------------ after a tool
    def post(self, step: int, tool: str, args: dict, ok: bool, stdout: str = "", error: str = "") -> Optional[str]:
        """Bookkeeping after a tool ran; returns a note (forced explore) or None."""
        a = args or {}
        note = None
        if tool == "shell":
            cmd = str(a.get("command") or "")
            from .devflow import read_only_files
            ro = read_only_files("shell", a)
            if ro and ok:
                paths = [m.group(0) for m in _pv_paths(cmd)]
                self.record_read(step, list(ro) + paths)
                if re.search(r"\b(grep|rg|ag)\b", cmd):
                    self.record_grep_output(step, stdout)
            if ok and _VERIFY_CMD.search(cmd):
                self.record_verify(step, "test" if re.search(r"test|pytest|unittest|jest|vitest", cmd) else "check", cmd)
            f = self.flow
            if ok and f is not None and f.generator:
                sc = _pv.script_of(cmd)
                if sc and _base(sc) == _base(f.generator):
                    self.record_verify(step, "generator", cmd)
            for t in self.edit_targets(tool, a):
                if ok:
                    self.record_edit(step, t)
        elif tool == "files_read" and ok:
            self.record_read(step, [_pv._ws(str(a.get("path") or ""))])
        elif tool in ("files_patch", "files_write") and ok and a.get("path"):
            wp = _pv._ws(str(a["path"]))
            self.record_edit(step, wp)
            if tool == "files_write":
                self.created.add(wp)   # its content is known to the agent: it wrote it
        if not ok:
            sig = error_signature((error or "") + "\n" + (stdout or "")[-1500:])
            if sig:
                self.errors.append((step, sig))
                self.errors = self.errors[-40:]
                self.err_counts[sig] = self.err_counts.get(sig, 0) + 1
                if self.err_counts[sig] >= 3:
                    note = self.force_explore(sig)
        return note

    def force_explore(self, sig: str) -> str:
        self.phase = "explore"
        self.explored = set()
        self.created = {c for c in self.created if self.new_build}   # a new project keeps its own files
        self.err_counts = {}
        self.forced_explore += 1
        files = sorted(self.edits, key=lambda k: -self.edits[k])[:4]
        self.decisions.append(f"同一错误 3 次，回到探索阶段：{sig[:80]}")
        return (f"STUCK: 同一个错误已经出现 3 次：{sig}。停止重复修改，回到探索阶段：先用 read_file / grep 重新读相关代码"
                + (f"（最近改过：{', '.join(files)}）" if files else "") + "，找到真正的原因，再改。在重新读之前，对现有文件的修改会被拒绝。"
                " Re-read the relevant code before editing again.")

    # ------------------------------------------------------------------ budget
    def budget_exceeded(self, step: int) -> bool:
        b = step_budget(self.new_build)
        return bool(b) and step - self.budget_from >= b + self.budget_extra

    def extend_budget(self, step: int) -> None:
        self.budget_from = step
        self.budget_extra = 0

    # ------------------------------------------------------------------ receipt
    def receipt(self, summary: str, outcome: str = "done") -> str:
        """The completion receipt: every todo with its status / evidence, unverified claims flagged."""
        if not self.todos:
            return ""
        parts = re.split(r"已知(?:未解决)?问题|known issues?", summary or "", maxsplit=1, flags=re.I)
        claims = re.sub(r"\s+", "", parts[0]).lower()
        lines, flags = [], []
        for t in self.todos:
            if t["status"] == "done":
                lines.append(f"- ✓ {t['content']}" + (f"（{t['evidence']}）" if t.get("evidence") else ""))
                continue
            claimed = any(k.lower() in claims for k in _kw(t["content"]))
            lines.append(f"- {'⚠ 总结声称完成但未验证' if claimed else '✗ 未完成/未验证'}：{t['content']}")
            flags.append(("unverified_claim" if claimed else "open", t["content"]))
        self.flagged = flags
        head = {"done": "完成回执", "nothing": "完成回执（无需修改）", "failed": "完成回执（未能完成）",
                "ask": "进度回执（需要你决定）"}.get(outcome, "完成回执")
        return f"\n\n{head}（TODO 清单）：\n" + "\n".join(lines)

    # ------------------------------------------------------------------ compaction / memory
    def needs_compaction(self, history_len: int, window: int = 25) -> bool:
        every = _env_int("AIW_DEV_COMPACT_EVERY", 15)
        return history_len - window - self.compacted_upto >= every

    def fold(self, history: list, window: int = 25) -> list:
        """The history lines to fold now (they left the window); marks them folded."""
        end = max(0, len(history) - window)
        chunk = history[self.compacted_upto:end]
        self.compacted_upto = end
        for ln in chunk:
            if re.search(r"USER ADDED|user answered|用户|REJECTED|REFUSED|STUCK|BLOCKED", ln or ""):
                self.decisions.append(re.sub(r"\s+", " ", ln)[:160])
        self.decisions = self.decisions[-16:]
        return chunk

    def deterministic_memory(self) -> str:
        reads = list(dict.fromkeys(p for _, p in self.reads_log if "/" in p or "." in p))[-14:]
        edits = sorted(self.edits.items(), key=lambda kv: kv[1])[-12:]
        lines = []
        if reads:
            lines.append("读过的文件：" + "、".join(reads))
        if edits:
            lines.append("改过的文件：" + "、".join(f"{p}（第{s}步）" for p, s in edits))
        if self.decisions:
            lines.append("决定/拒绝/用户回答：\n" + "\n".join("  - " + d for d in self.decisions[-10:]))
        if self.errors:
            errs = list(dict.fromkeys(s for _, s in self.errors))[-5:]
            lines.append("出现过的错误：" + "；".join(errs))
        open_t = [t for t in self.todos if t["status"] != "done"]
        if open_t:
            lines.append("未完成的 TODO：" + "；".join(f"{t['id']}. {t['content'][:50]}" for t in open_t))
        return "\n".join(lines)

    def block(self) -> str:
        """The working memory + todos, in the task text every turn."""
        out = ""
        mem = self.deterministic_memory()
        if self.memory or mem:
            out += ("\n\n【工作记忆（较早步骤已压缩；文件路径、决定和未完成 TODO 都在这里）】\n" + (self.memory.strip() + "\n" if self.memory else "")
                    + mem)
        out += self.todo_text()
        if self.phase == "explore" and not self.new_build:
            out += ("\n阶段：探索 —— 修改现有文件前先 read_file/grep 读到要改的代码" +
                    ("（同一错误出现 3 次后被拉回探索阶段）" if self.forced_explore else ""))
        return out

    def snapshot(self) -> dict:
        return {"todos": self.todos, "memory": (self.memory or "")[:4000], "explored": sorted(self.explored)[:300],
                "created": sorted(self.created)[:200], "edits": self.edits, "decisions": self.decisions[-16:],
                "errors": [list(e) for e in self.errors[-10:]], "phase": self.phase, "new_build": self.new_build}

    def restore(self, snap: Optional[dict]) -> bool:
        """A 继续 run picks up the previous run's working memory instead of starting over."""
        s = snap or {}
        if not s:
            return False
        self.todos = [dict(t) for t in s.get("todos") or [] if isinstance(t, dict) and t.get("content")]
        for t in self.todos:   # an item left in progress stays open; done items keep their earlier evidence
            if t.get("status") not in STATUSES:
                t["status"] = "pending"
        prev_mem = str(s.get("memory") or "")
        prev_det = ""
        try:
            tmp = DevLoop()
            tmp.edits = {k: int(v) for k, v in (s.get("edits") or {}).items()}
            tmp.decisions = list(s.get("decisions") or [])
            tmp.reads_log = [(0, p) for p in s.get("explored") or [] if "/" in p]
            tmp.todos = []
            prev_det = tmp.deterministic_memory()
        except Exception:  # noqa: BLE001
            pass
        self.memory = ("上一轮运行的进度（继续，不要从头开始）：\n" + "\n".join(x for x in (prev_mem, prev_det) if x)).strip()
        self.explored = set(s.get("explored") or [])
        self.created = set(s.get("created") or [])
        self.decisions = list(s.get("decisions") or [])[-16:]
        self.new_build = bool(s.get("new_build")) or self.new_build
        self.phase = "edit" if self.explored else "explore"
        self.resumed = True
        return True


def _pv_paths(cmd: str):
    from .devflow import _PATHISH
    return _PATHISH.finditer(cmd or "")


def summary_prompt(lines: list, todos_text: str) -> tuple[str, str]:
    """(system, user) for the cheap model that compresses older steps."""
    sys = ("You compress an engineering agent's older steps into a short working memory in Chinese. Keep every file "
           "path, every decision and its reason, what was tried and failed (with the error), and what is still open. "
           "No speculation, no advice. At most 12 bullet lines.")
    user = "OLDER STEPS:\n" + "\n".join(str(x)[:400] for x in lines[-60:]) + ("\n\nOPEN TODOS:" + todos_text if todos_text else "")
    return sys, user


# ------------------------------------------------------------------------------------------------ activation
from .intent import NEW_BUILD_REASON  # noqa: E402


def is_new_build(intent_info: Optional[dict], artifacts, contract: Optional[dict]) -> bool:
    """A brand-new project: picked with the dev skill (or a 'write a program' request), no script-made artifacts in
    the thread and no existing source found. The explore/source gates then do not block creating files."""
    info = intent_info or {}
    c = contract or {}
    if c.get("source_of_truth") or list(artifacts or []):
        return False
    return bool(info.get("forced")) or NEW_BUILD_REASON in (info.get("reasons") or [])


# ------------------------------------------------------------------------------------------------ files_patch errors
def patch_error_hint(error: str, path=None) -> str:
    """A files_patch mismatch, explained: 0 matches -> copy exact text; N matches -> add context or count."""
    e = str(error or "")
    m = re.search(r"found (\d+)", e)
    if not m:
        return e
    n = int(m.group(1))
    p = path or "the file"
    if n == 0:
        return (e + f" | NO MATCH: `old` does not occur in {p}. read_file the region (offset/limit) and copy `old` "
                "exactly, including indentation and line breaks; do not retype it from memory.")
    return (e + f" | NOT UNIQUE: `old` occurs {n} times in {p}. Add surrounding lines to `old` so it matches exactly "
            f"once, or pass count={n} (count=0 = every occurrence) if all of them must change.")


# ------------------------------------------------------------------------------------------------ read_file / grep / glob
SENSITIVE = re.compile(r"(\.ssh/|id_rsa|id_ed25519|(^|/)\.env(\.|$)|\.aws/|credentials|\.pem$|\.key$|/etc/shadow|passwords?\.)", re.I)
_TOOL_PY = r'''
import base64, fnmatch, glob as G, json, os, re, subprocess, sys
a = json.loads(base64.b64decode(sys.argv[1]).decode())
t = a.pop("_tool")
SENS = re.compile(a.pop("_sens"), re.I)
def ex(p): return os.path.expanduser(os.path.expandvars(p or "."))
out = {"ok": True}
try:
    if t == "read_file":
        p = ex(a["path"])
        if SENS.search(p): raise PermissionError("sensitive path")
        off = max(1, int(a.get("offset") or 1)); lim = max(1, min(int(a.get("limit") or 400), 2000))
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        sel = lines[off - 1: off - 1 + lim]
        body = "\n".join("%6d\t%s" % (off + i, (ln if len(ln) <= 400 else ln[:400] + " …[line cut]")) for i, ln in enumerate(sel))
        more = len(lines) - (off - 1 + len(sel))
        out.update(path=p, total=len(lines), text=("%s (lines %d-%d of %d)\n" % (p, off, off + len(sel) - 1, len(lines))) + body[:30000]
                   + ("\n… %d more lines: read_file with offset=%d" % (more, off + len(sel)) if more > 0 else ""))
    elif t == "grep":
        root = ex(a.get("path") or "."); pat = a["pattern"]
        if SENS.search(root): raise PermissionError("sensitive path")
        cmd = ["grep", "-rnHE", "-I", "--exclude-dir=.git", "--exclude-dir=node_modules", "--exclude-dir=__pycache__",
               "--exclude-dir=.venv"] + (["-i"] if a.get("ignore_case") else []) + (["--include=" + a["glob"]] if a.get("glob") else []) \
              + (["-C", str(min(int(a.get("context") or 0), 5))] if a.get("context") else []) + ["--", pat, root]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = [ln for ln in r.stdout.splitlines() if not SENS.search(ln.split(":", 1)[0])]
        lim = min(int(a.get("max_results") or 200), 500)
        files = sorted({ln.split(":", 1)[0] for ln in lines if ":" in ln})
        if a.get("files_only"):
            text = "\n".join(files[:lim])
        else:
            text = "\n".join(ln[:300] for ln in lines[:lim])
        out.update(files=files[:200], text=(text or "(no matches)") + ("\n… %d more" % (len(lines) - lim) if len(lines) > lim else ""),
                   count=len(lines))
        if r.returncode > 1: out.update(ok=False, error=r.stderr[-300:])
    elif t == "glob":
        root = ex(a.get("path") or "."); pat = a["pattern"]
        hits = [h for h in G.glob(os.path.join(root, pat), recursive=True)
                if not any(x in h for x in ("/.git/", "/node_modules/", "/__pycache__/")) and not SENS.search(h)]
        hits.sort(key=lambda h: -os.path.getmtime(h) if os.path.exists(h) else 0)
        lim = min(int(a.get("max_results") or 200), 500)
        out.update(files=hits[:lim], text="\n".join(hits[:lim]) or "(no files)")
except Exception as e:
    out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
print("AIWTOOL " + json.dumps(out, ensure_ascii=False))
'''


def tool_command(tool: str, args: dict, default_dir: str = "~/workspace") -> tuple[Optional[str], Optional[str]]:
    """(shell command, error) for read_file / grep / glob. Read-only; sensitive paths refused."""
    import base64
    import json
    import shlex
    a = dict(args or {})
    if tool == "read_file" and not a.get("path"):
        return None, "read_file needs `path`"
    if tool in ("grep", "glob") and not a.get("pattern"):
        return None, f"{tool} needs `pattern`"
    if tool in ("grep", "glob") and not a.get("path"):
        a["path"] = default_dir
    if SENSITIVE.search(str(a.get("path") or "")):
        return None, f"refused: sensitive path {a.get('path')}"
    a["_tool"] = tool
    a["_sens"] = SENSITIVE.pattern
    b = base64.b64encode(json.dumps(a, ensure_ascii=False).encode()).decode()
    return "python3 -c " + shlex.quote(_TOOL_PY) + " " + b, None


def parse_tool_output(tool: str, stdout: str) -> dict:
    import json
    for ln in reversed((stdout or "").splitlines()):
        if ln.startswith("AIWTOOL "):
            try:
                return json.loads(ln[8:])
            except ValueError:
                break
    return {"ok": False, "error": "no tool output"}
