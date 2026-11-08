"""v3.9.26 (task-830): the dev/fix workflow aiw enforces itself, whatever the model does.

One DevFlow per task (also for non-dev tasks, which only use the provenance / fingerprint bookkeeping):

* provenance  every shell step is logged; on send_file the producing command/script is recorded (provenance.py)
* contract    dev_contract.build() at step 0 (goal, source of truth, deliverables, must_keep, acceptance, voided)
* pre_tool    dev_fix: a direct edit of a generated artifact is refused ("这是 <script> 生成的产物…"), running an ad-hoc
              patch script that writes it too; a verification approach that failed twice is switched off in favour of
              verify_html; creating inject*/restyle*/patch*/fix* scripts gets a warning
* post_tool   source writes / generator runs / verify + test evidence are tracked; 3 read-only inspections of the same
              file(s) in a row -> "已确认结论…下一步必须动手修改"
* done_gate   dev_fix done needs (a) a real source change (git diff vs the recorded HEAD, or a recorded source write),
              (b) the generator re-run after the last source edit when the deliverable is generated, (c) acceptance
              evidence after the last change and no regression against the previous deliverable's fingerprint.
              Bounded: after AIW_DEV_DONE_REJECTS rejections done goes through with 已知问题 listing what was unmet.
v3.9.29 (task-68b: 11 fix_*.py scripts written, most never run; one replacement written 6 times; a 303-line fact that
went stale; a done summary claiming the style was done while verify had failed):
* files_patch edits count as source writes; a written script that edits a contract source gets "use files_patch" instead
  of the ad-hoc warning, and running it counts as a source write;
* written-but-not-executed: a .py/.sh written and not run within the next 2 steps gets a nudge naming it; a second
  script carrying the same replacement payload as an unexecuted (or already applied) one is refused; both are counted;
* stuck_pattern(): the concrete loop (N scripts written unexecuted / the same edit repeated N times / alternating
  approaches) for the dev_rabbit note and card;
* req_checks (dev_contract.requirement_checks) evaluated by verify_html: done needs each one checked after the last
  change, failed ones listed under 已知问题 with a reason, no summary claim that contradicts a failed check, and every
  check's status named in the summary;
* generator_cmd_fast: fast (cache) generator runs count for iterating, done needs one FULL run after the last change.
Pure state (no I/O); loop.py gathers git / fingerprint facts and calls in. Tested in tests/test_v3926_devflow.py and
tests/test_v3929_devflow_fixes.py.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import provenance as _pv
from .provenance import ADHOC, SOURCE_EXT, ProvenanceBook, norm


def _dc_fast(command: str) -> bool:
    from .dev_contract import is_fast_run   # dev_contract imports provenance only: no cycle
    return is_fast_run(command)

_READ_PROG = re.compile(r"^\s*(?:sudo\s+)?(cat|head|tail|less|more|grep|egrep|fgrep|rg|ag|wc|ls|ll|stat|file|diff|cmp|awk|"
                        r"sed|nl|sort|uniq|cut|tr|jq|xxd|od|hexdump|find|tree|du|md5sum|sha1sum|sha256sum|python3?|node|"
                        r"echo|printf|pwd|which|type|git|curl|true)\b")
_WRITEY = re.compile(r"(?<![<>&0-9])>>?(?!&)|\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|\brm\b|\bmv\b|\bcp\b|"
                     r"\bmkdir\b|\btouch\b|write_text|write_bytes|\.write\(|open\([^)]*['\"][wa]|writeFile|\bgit\s+(commit|apply|"
                     r"checkout|reset|stash|add)\b|\bpatch\b|-delete\b|-exec\b")
_PATHISH = re.compile(r"(?:~|\.{0,2}/|\$HOME/)?[\w.@+-]+(?:/[\w.@+-]+)*\.(?:py|js|mjs|ts|tsx|jsx|vue|css|scss|html?|json|"
                      r"j2|jinja2?|sh|md|txt|yaml|yml|toml)\b")
_TEST_CMD = re.compile(r"\b(pytest|py\.test|python3? -m (pytest|unittest)|npm (run )?test|pnpm test|yarn test|node --test|"
                       r"go test|cargo test|make (test|check)|vitest|jest)\b")
# verification approaches (task-830 tried headless chrome, BrowserOS MCP 9210, CDP on 9222 …)
_V_HEADLESS = re.compile(r"\b(google-chrome(-stable)?|chromium(-browser)?|chrome|headless_shell)\b[^;&|]*(--headless|--screenshot|"
                         r"--dump-dom|--print-to-pdf)", re.I)
_V_CDP = re.compile(r"remote-debugging|:9222\b|:9210\b|:9110\b|/json/(list|version|new)|devtools|websocket|ws://", re.I)
_V_PW = re.compile(r"\b(playwright|puppeteer|selenium|pyppeteer)\b", re.I)
VERIFY_ALT = ("改用内置的 verify_html 工具验证页面（它自己启动 headless Chromium、带超时、自动清理进程，返回控制台错误、"
              "选择器计数和点击结果）。Do NOT start chrome/chromium/CDP/BrowserOS by hand again.")


def dev_done_rejects() -> int:
    try:
        return max(0, int(os.environ.get("AIW_DEV_DONE_REJECTS", "3") or 0))
    except ValueError:
        return 3


def verify_approach(tool: str, args: dict) -> Optional[str]:
    a = args or {}
    if tool == "shell":
        c = str(a.get("command") or "")
        if _V_PW.search(c):
            return "playwright"
        if _V_HEADLESS.search(c):
            return "headless_chrome"
        if _V_CDP.search(c):
            return "cdp"
        return None
    if tool.startswith("bos_"):
        return "browseros"
    if tool in ("browser_open", "browser_navigate") and re.match(r"(file://|https?://(localhost|127\.0\.0\.1))", str(a.get("url") or "")):
        return "browser_gui"
    return None


def read_only_files(tool: str, args: dict) -> Optional[set]:
    """The files a read-only inspection step looks at (None: not a read-only inspection)."""
    a = args or {}
    if tool == "files_read":
        return {norm(str(a.get("path") or ""))}
    if tool != "shell":
        return None
    c = str(a.get("command") or "")
    if _WRITEY.search(c):
        return None
    parts = [p for p in re.split(r"&&|\|\||;|\||\n", c) if p.strip()]
    for p in parts:
        p = p.strip()
        if re.match(r"^cd\b", p):
            continue
        m = _READ_PROG.match(p)
        if not m:
            return None
        if m.group(1) in ("python3", "python", "node") and not re.search(r"\s-c\s|\s-e\s", p):
            return None
    files = {norm(x.group(0)) for x in _PATHISH.finditer(c)}
    return {os.path.basename(f) for f in files} or None


_INTERP_ALL = re.compile(r"(?:^|[\s;&|(]|&&)(?:sudo\s+)?(?:timeout\s+(?:-\S+\s+)*\d+\S*\s+)?"
                         r"(?:python3?(?:\.\d+)?|node|bash|sh|deno|bun|ruby|perl|php)\s+(?:-[\w-]+\s+)*"
                         r"([^\s;&|<>]+\.(?:py|js|mjs|cjs|ts|sh|rb|pl|php))")
_PAYLOAD = (re.compile(r"\bold\w*\s*=\s*[rbuf]?('{3}|\"{3}|'|\")(.*?)\1", re.S),
            re.compile(r"\.replace\(\s*[rbuf]?('{3}|\"{3}|'|\")(.*?)\1", re.S),
            re.compile(r"\bre\.sub[n]?\(\s*[rbuf]?('{3}|\"{3}|'|\")(.*?)\1", re.S),
            re.compile(r"\.count\(\s*[rbuf]?('{3}|\"{3}|'|\")(.*?)\1", re.S))


def scripts_run(command: str) -> list[str]:
    """Every script a shell command runs (cd-relative names joined to the last cd before them)."""
    out = []
    c = command or ""
    for m in _INTERP_ALL.finditer(c):
        sc = m.group(1)
        if not sc.startswith(("/", "~", "$")):
            cds = re.findall(r"\bcd\s+([^\s;&|]+)", c[:m.start(1)])
            if cds:
                sc = cds[-1].rstrip("/") + "/" + sc
        out.append(norm(sc))
    return out


def payload_sig(content: str) -> Optional[tuple]:
    """The replacement payload of a patch script: the `old` strings it looks for (sorted), None when it has none."""
    olds = set()
    for rx in _PAYLOAD:
        for m in rx.finditer(content or ""):
            t = re.sub(r"\s+", " ", m.group(2)).strip()
            if len(t) >= 6:
                olds.add(t[:300])
    return tuple(sorted(olds)) or None


def edit_approach(tool: str, args: dict) -> Optional[str]:
    a = args or {}
    if tool == "files_patch":
        return "files_patch"
    if tool == "files_write":
        return "script" if _pv.SCRIPT_EXT.search(str(a.get("path") or "")) else "files_write"
    if tool != "shell":
        return None
    c = str(a.get("command") or "")
    if re.search(r"\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i", c):
        return "sed"
    if _pv.written_script(tool, a):
        return "script"
    if re.search(r"\bpython3?\s+(-\s*)?<<|\bpython3?\s+-c\b", c) and re.search(r"write_text|\.write\(|\.replace\(", c):
        return "inline_python"
    return None


APPROACH_CN = {"files_patch": "files_patch", "script": "写补丁脚本", "sed": "sed -i", "inline_python": "内联 python",
               "files_write": "整文件重写"}


def _nt(t: str) -> str:
    return "".join(str(t or "").split()).lower()


def written_files(tool: str, args: dict) -> set:
    """Files a step writes (best effort)."""
    a = args or {}
    if tool in ("files_write", "files_patch"):
        return {norm(str(a.get("path") or ""))}
    if tool != "shell":
        return set()
    c = str(a.get("command") or "")
    out = set()
    for m in re.finditer(r"\bsed\s+(?:-[a-zA-Z]*i\S*|--in-place\S*)\s+(?:(?:-e\s+)?(?:'[^']*'|\"[^\"]*\")\s+)+([^\s;&|]+)", c):
        out.add(norm(m.group(1)))
    for m in re.finditer(r"\bperl\s+-[a-zA-Z]*i\S*\s+(?:-[a-zA-Z]+\s+)*(?:'[^']*'|\"[^\"]*\")\s+([^\s;&|]+)", c):
        out.add(norm(m.group(1)))
    for m in re.finditer(r"(?<![<>&0-9])>>?\s*['\"]?([^\s;&|'\"<>]+)", c):
        if not m.group(1).startswith(("/dev/", "&")):
            out.add(norm(m.group(1)))
    for m in re.finditer(r"\btee\s+(?:-a\s+)?['\"]?([^\s;&|'\"]+)", c):
        out.add(norm(m.group(1)))
    for m in re.finditer(r"\b(?:cp|mv)\s+(?:-\w+\s+)*[^;&|]*?\s([^\s;&|]+)\s*(?:$|[;&|])", c):
        out.add(norm(m.group(1)))
    if re.search(r"write_text|write_bytes|\.write\(|open\([^)]*['\"][wa]|writeFileSync|fs\.writeFile", c):
        for m in _PATHISH.finditer(c):
            out.add(norm(m.group(0)))
    w = _pv.written_script(tool, a)
    if w:
        out.add(w[0])
    return {p for p in out if p and not p.startswith("/dev/")}


class DevFlow:
    def __init__(self, intent: str = "other", book: Optional[ProvenanceBook] = None, baselines: Optional[dict] = None):
        self.intent = intent
        self.dev = intent == "dev_fix"
        self.book = book or ProvenanceBook()
        self.baselines: dict = dict(baselines or {})   # artifact path -> fingerprint of the previous deliverable
        self.delivered: dict = {}                       # this run: path -> fingerprint
        self.contract: Optional[dict] = None
        self.git: dict = {}
        self.shell_log: list = []
        self.source_writes: dict = {}
        self.last_source_step = 0
        self.patched = False
        self.gen_runs: list = []
        self.gen_outputs: list = []
        self.verify_ok: list = []
        self.verify_fail = 0
        self.test_ok: list = []
        self.patchers: dict = {}
        self._reads: list = []
        self._diag_noted = False
        self.vfail: dict = {}
        self.vblocked: set = set()
        self.done_rejects = 0
        self.known_issues: list = []
        self.warned: set = set()
        # v3.9.29
        self.pending: dict = {}        # script path -> {"step", "sig", "target"} written, not run yet
        self.unexec: list = []         # scripts that stayed unexecuted for 2+ steps (counted for stuck detection)
        self.payloads: dict = {}       # payload sig -> {"path", "step", "applied", "repeats"}
        self.script_target: dict = {}  # script path -> contract source it edits
        self.edits: list = []          # [(step, approach)] source-edit attempts
        self.fast_runs: list = []
        self.req_status: dict = {}     # req check id -> {"ok", "step", "detail", "label"}
        self.just_written: set = set()  # sources written by the last post_tool (the loop expires findings about them)

    # ------------------------------------------------------------------ contract helpers
    @property
    def generator(self) -> Optional[str]:
        c = self.contract or {}
        return c.get("source_of_truth") if (c.get("deliverables") or c.get("generator_cmd")) else None

    def sources(self) -> tuple:
        c = self.contract or {}
        return tuple(x for x in (c.get("allowed_paths") or []) if SOURCE_EXT.search(x or ""))

    def patch_allowed(self, path: str) -> bool:
        """v3.9.29: files_patch may edit this path outside ~/workspace (a contract source / allowed folder)."""
        p = norm(path)
        for a in (self.contract or {}).get("allowed_paths") or []:
            a = norm(a)
            if a and (p == a or p.startswith(a.rstrip("/") + "/")):
                return True
        return False

    def _is_source(self, p: str) -> bool:
        if not p or not SOURCE_EXT.search(p) or ADHOC.search(p) or p.startswith("/tmp") or self.book.generator(p):
            return False
        allowed = [norm(x) for x in (self.contract or {}).get("allowed_paths") or []]
        if allowed and not any(p == a or p.startswith(a.rstrip("/") + "/") or os.path.basename(p) == os.path.basename(a)
                               for a in allowed):
            # outside the contract: a source file in ~/workspace scratch does not count
            return "/workspace/" not in p and not p.startswith("~/workspace")
        return True

    # ------------------------------------------------------------------ before a tool runs
    def pre_tool(self, step: int, tool: str, args: dict) -> tuple[Optional[str], Optional[str]]:
        """-> ("skip", note) to refuse | (None, warning or None) to run."""
        if not self.dev:
            return None, None
        hit = _pv.artifact_edit(tool, args, self.book, self.patchers, self.sources())
        if hit:
            art, gen = hit
            return "skip", _pv.refuse_text(art, gen)
        ap = verify_approach(tool, args)
        if ap and ap in self.vblocked:
            return "skip", f"REFUSED: the {ap} way of verifying already failed twice in this task. " + VERIFY_ALT
        warns = []
        w = _pv.written_script(tool, args)
        srcs = self.sources() or tuple(x for x in [(self.contract or {}).get("source_of_truth")] if x)
        target = None
        if w:
            for art in self.book.artifacts():
                if _pv.code_writes(w[1], art):
                    self.patchers[w[0]] = art
            target = _pv.script_targets(w[1], srcs)
            # v3.9.29: the same replacement payload as a script that was not run yet / was already applied: refuse
            sig = payload_sig(w[1])
            prev = self.payloads.get(sig) if sig else None
            if prev and prev.get("path") == w[0] and not prev.get("applied"):
                prev["repeats"] = int(prev.get("repeats") or 0) + 1   # rewriting its own unexecuted script: counted
            elif prev and (prev.get("applied") or prev.get("path") in self.pending):
                prev["repeats"] = int(prev.get("repeats") or 0) + 1
                what = "已经执行过（这处替换已经应用）" if prev.get("applied") else "还没有执行"
                run = f"python3 {prev['path']}" if prev["path"].endswith(".py") else f"bash {prev['path']}"
                return "skip", (f"REFUSED: 第 {prev['step']} 步写的 {prev['path']} 已经包含同样的替换（old={sig[0][:60]!r}），{what}。"
                                + ("先 grep 源头确认现状，不要再写同样的补丁。" if prev.get("applied") else
                                   f"下一步直接执行它：{run}；") +
                                (f" 或者用 files_patch 直接改 {target or srcs[0]}。" if (target or srcs) else "") +
                                " Do NOT write the same patch again.")
        elif tool == "files_patch":
            sig = ("files_patch", norm(str((args or {}).get("path") or "")), str((args or {}).get("old") or "")[:300],
                   str((args or {}).get("new") or "")[:300])
            prev = self.payloads.get(sig)
            if prev and prev.get("applied"):
                prev["repeats"] = int(prev.get("repeats") or 0) + 1
                return "skip", (f"REFUSED: this exact files_patch was already applied at step {prev['step']}. The text is "
                                "replaced already; read the current source (grep) before changing it again.")
        # v3.9.29: scripts written 2+ steps ago and never run
        running = scripts_run(str((args or {}).get("command") or "")) if tool == "shell" else []
        for path, e in list(self.pending.items()):
            if any(os.path.basename(r) == os.path.basename(path) for r in running):
                continue
            if step - e["step"] >= 3 and ("unexec:" + path) not in self.warned:
                self.warned.add("unexec:" + path)
                if path not in [u["path"] for u in self.unexec]:
                    self.unexec.append({"path": path, "step": e["step"]})
                run = f"python3 {path}" if path.endswith(".py") else f"bash {path}"
                warns.append(f"NOT EXECUTED: 你在第 {e['step']} 步写了 {path}，之后 {step - e['step'] - 1} 步都没有执行它，"
                             f"源头还没改。下一步要么执行：{run}，要么放弃它、用 files_patch 直接改 "
                             f"{e.get('target') or (srcs[0] if srcs else '源文件')}。"
                             f"（本任务已有 {len(self.unexec)} 个脚本写了没执行）")
        adhoc = _pv.adhoc_patch_script(tool, args)
        if adhoc and adhoc not in self.warned:
            self.warned.add(adhoc)
            warns.append(_pv.adhoc_warning(adhoc, (self.contract or {}).get("source_of_truth"), target))
        elif w and target and ("note:" + w[0]) not in self.warned:
            self.warned.add("note:" + w[0])
            warns.append(_pv.adhoc_warning(w[0], None, target))
        return None, ("\n".join(warns) or None)

    # ------------------------------------------------------------------ after a tool ran
    def post_tool(self, step: int, tool: str, args: dict, ok: bool, stdout: str = "", error: str = "") -> Optional[str]:
        a = args or {}
        note = None
        self.just_written = set()
        if tool == "shell":
            cmd = str(a.get("command") or "")
            self.shell_log.append({"step": step, "command": cmd[:2000], "stdout": str(stdout or "")[-4000:]})
            self.shell_log = self.shell_log[-120:]
            gen = self.generator
            sc = _pv.script_of(cmd)
            if ok and gen and sc and os.path.basename(sc) == os.path.basename(gen):
                (self.fast_runs if _dc_fast(cmd) else self.gen_runs).append(step)
                for f in _pv.json_file_line(stdout):
                    self.gen_outputs.append(norm(f))
            ran = scripts_run(cmd)
            if ran:   # v3.9.29: a written script that is run leaves the pending list (and edits its target)
                for path in list(self.pending):
                    if any(r == path or os.path.basename(r) == os.path.basename(path) for r in ran):
                        e = self.pending.pop(path)
                        if ok and e.get("sig") and e["sig"] in self.payloads:
                            self.payloads[e["sig"]]["applied"] = True
                        tgt = e.get("target") or self.script_target.get(path)
                        if ok and tgt and self.dev:
                            self.source_writes[tgt] = step
                            self.last_source_step = step
                            self.just_written.add(tgt)
            if ok and _TEST_CMD.search(cmd):
                self.test_ok.append(step)
            if re.search(r"\bgit\s+apply\b|\bpatch\s+-p\d|\bapply_patch\b", cmd) and ok:
                self.patched, self.last_source_step = True, step
        if not self.dev:
            return None
        wrote = written_files(tool, a) if ok else set()
        for p in wrote:
            if self._is_source(p) or (tool == "files_patch" and self.patch_allowed(p)):
                self.source_writes[p] = step
                self.last_source_step = step
                self.just_written.add(p)
        ap = edit_approach(tool, a)
        if ok and ap:
            self.edits.append((step, ap))
            self.edits = self.edits[-20:]
        if ok and tool == "files_patch":
            sig = ("files_patch", norm(str(a.get("path") or "")), str(a.get("old") or "")[:300], str(a.get("new") or "")[:300])
            self.payloads.setdefault(sig, {"path": norm(str(a.get("path") or "")), "step": step, "repeats": 0})["applied"] = True
        w = _pv.written_script(tool, a) if ok else None
        if w:
            srcs = self.sources() or tuple(x for x in [(self.contract or {}).get("source_of_truth")] if x)
            tgt = _pv.script_targets(w[1], srcs)
            if tgt:
                self.script_target[w[0]] = tgt
            sig = payload_sig(w[1])
            run_now = tool == "shell" and any(os.path.basename(r) == os.path.basename(w[0])
                                              for r in scripts_run(str(a.get("command") or "")))
            if sig:
                e = self.payloads.setdefault(sig, {"path": w[0], "step": step, "repeats": 0})
                e.update(path=w[0], step=step)
                if run_now:
                    e["applied"] = True
            if not run_now and re.search(r"\.(py|sh)$", w[0]):
                self.pending[w[0]] = {"step": step, "sig": sig, "target": tgt}
            elif run_now and tgt:
                self.source_writes[tgt] = step
                self.last_source_step = step
                self.just_written.add(tgt)
        # repeated diagnosis: 3 read-only inspections in a row on the same file(s), nothing written
        files = read_only_files(tool, a)
        if wrote or tool in ("files_write", "files_patch"):
            self._reads, self._diag_noted = [], False
        elif files is not None:
            self._reads.append(files)
            self._reads = self._reads[-3:]
            if len(self._reads) == 3 and not self._diag_noted:
                common = set.intersection(*self._reads)
                if common:
                    self._diag_noted = True
                    names = "、".join(sorted(common)[:4])
                    target = (self.contract or {}).get("source_of_truth") or "源头文件"
                    note = (f"已确认结论：{names} 已经连续 3 次只读检查，信息已足够，不要再重复诊断。下一步必须动手修改 {target}"
                            "（用 files_patch 直接改源文件，返回 diff），然后重新生成并验证。STOP inspecting: make the edit now.")
        elif tool not in ("update_plan", "note_finding"):
            self._reads = []
        # verification circuit breaker
        ap = verify_approach(tool, a)
        if ap:
            bad = (not ok) or bool(re.search(r"\b403\b|refused|forbidden|timed? ?out|ECONNREFUSED|cannot connect|"
                                             r"not found|No such file|Traceback|exit=1\b", str(error or "") + " " + str(stdout or "")[-1500:], re.I))
            if bad:
                self.vfail[ap] = self.vfail.get(ap, 0) + 1
                if self.vfail[ap] >= 2 and ap not in self.vblocked:
                    self.vblocked.add(ap)
                    note = ((note + "\n") if note else "") + (f"VERIFICATION CIRCUIT BREAKER: the {ap} approach failed twice. "
                                                              + VERIFY_ALT)
        return note

    def record_verify(self, step: int, res: dict) -> None:
        if (res or {}).get("ok"):
            self.verify_ok.append(step)
        else:
            self.verify_fail += 1
        for c in (res or {}).get("checks") or []:   # v3.9.29 requirement checks
            if c.get("id"):
                self.req_status[c["id"]] = {"ok": bool(c.get("ok")), "step": step, "detail": str(c.get("detail") or "")[:160],
                                            "label": c.get("label") or c["id"]}

    # ------------------------------------------------------------------ v3.9.29 stuck patterns
    def stuck_pattern(self) -> list[tuple]:
        """[(kind, Chinese text, severity 'note'|'ask')] — the concrete loop this dev task is in."""
        out = []
        pend = [p for p in self.pending]
        n = len(self.unexec)
        if n:
            names = "、".join(os.path.basename(u["path"]) for u in self.unexec[-4:])
            still = sum(1 for u in self.unexec if u["path"] in pend)
            out.append(("unexecuted", f"写了 {n} 个脚本却没有执行（{names}{'…' if n > 4 else ''}；其中 {still} 个至今没跑）",
                        "ask" if n >= 4 else "note"))
        reps = sorted(((int(e.get("repeats") or 0), sig, e) for sig, e in self.payloads.items() if e.get("repeats")),
                      key=lambda x: -x[0])
        if reps:
            r, sig, e = reps[0]
            snippet = (sig[2] if sig and sig[0] == "files_patch" else (sig[0] if sig else ""))[:40]
            out.append(("repeated_edit", f"同一处修改重复了 {r + 1} 次（{snippet!r}，第一次在第 {e.get('step')} 步）",
                        "ask" if r >= 3 else "note"))
        last_ok = max(self.verify_ok + self.gen_runs + self.fast_runs + [0])
        recent = [a for st, a in self.edits if st > last_ok][-6:]
        switches = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i - 1])
        if len(recent) >= 4 and switches >= 3 and len(set(recent)) >= 2:
            kinds = "/".join(APPROACH_CN.get(k, k) for k in dict.fromkeys(recent))
            out.append(("alternating", f"在 {kinds} 几种改法之间来回切换（最近 {len(recent)} 次修改，中间没有成功生成或验证）",
                        "ask" if len(recent) >= 6 else "note"))
        if self.vblocked:
            out.append(("verify", "验证方式 " + "、".join(sorted(self.vblocked)) + " 已失败两次被熔断", "note"))
        return out

    def stuck_text(self) -> str:
        return "；".join(t for _, t, _ in self.stuck_pattern())

    def stuck_ask(self) -> bool:
        return any(sv == "ask" for _, _, sv in self.stuck_pattern())

    def record_delivery(self, path: str, fp: dict) -> None:
        self.delivered[norm(path)] = fp

    # ------------------------------------------------------------------ done
    def deliverable(self) -> Optional[str]:
        """The file whose fingerprint is compared: the newest generator output of this run, else the contract's."""
        if self.gen_outputs:
            return self.gen_outputs[-1]
        d = (self.contract or {}).get("deliverables") or []
        return d[-1] if d else None

    def baseline(self) -> Optional[dict]:
        d = (self.contract or {}).get("deliverables") or []
        for p in reversed(d):
            fp = self.baselines.get(p)
            if fp:
                return fp
        return None

    def req_problems(self, summary: str, last: int) -> list[tuple]:
        """v3.9.29: requirement checks — checked after the last change, failures listed as 已知问题 with a reason, no
        claim that contradicts a failed check, every check's status named in the summary."""
        reqs = (self.contract or {}).get("req_checks") or []
        if not reqs:
            return []
        out = []
        unchecked = [r for r in reqs if (self.req_status.get(r["id"]) or {}).get("step", -1) < last]
        if unchecked:
            out.append(("req_unchecked", "拒绝 done：需求检查没有在最后一次改动后跑过：" + "、".join(r["label"] for r in unchecked[:6]),
                        "REJECTED: these requirement checks were not evaluated after the last change: "
                        + "; ".join(f"[{r['id']}] {r['label']}" for r in unchecked) + ". Run verify_html on the regenerated "
                        "file (the system adds the checks automatically) and report each result."))
            return out
        parts = re.split(r"已知(?:未解决)?问题|known issues?", summary or "", maxsplit=1, flags=re.I)
        claims, known = _nt(parts[0]), _nt(parts[1] if len(parts) > 1 else "")
        failed = [r for r in reqs if not self.req_status[r["id"]]["ok"]]
        bad, unlisted, unreported = [], [], []
        for r in reqs:
            kws = [_nt(k) for k in (r.get("keywords") or []) + [r.get("label") or ""] if k]
            in_claims = any(k and k in claims for k in kws)
            in_known = any(k and k in known for k in kws) or _nt(r["id"]) in known
            if r in failed:
                if in_claims and not in_known:
                    bad.append(r)
                elif not in_known:
                    unlisted.append(r)
            elif not (in_claims or in_known or _nt(r["id"]) in claims):
                unreported.append(r)
        if bad:
            out.append(("req_contradiction", "拒绝 done：总结声称完成，但检查未通过：" + "、".join(r["label"] for r in bad[:6]),
                        "REJECTED: the summary claims these are done but verify_html says they FAILED: "
                        + "; ".join(f"[{r['id']}] {r['label']} ({self.req_status[r['id']]['detail']})" for r in bad)
                        + ". Fix them, or move them under 已知问题 with the reason. Do not claim what the check disproves."))
        if unlisted:
            out.append(("req_failed", "拒绝 done：需求检查未通过且没有列进已知问题：" + "、".join(r["label"] for r in unlisted[:6]),
                        "REJECTED: these requirement checks FAILED: "
                        + "; ".join(f"[{r['id']}] {r['label']} ({self.req_status[r['id']]['detail']})" for r in unlisted)
                        + ". Fix them in the source and regenerate, or list each under 已知问题 with the reason."))
        if unreported:
            out.append(("req_unreported", "拒绝 done：总结没有写出每项需求检查的结果",
                        "REJECTED: the done summary must state the result of every requirement check: "
                        + "; ".join(f"[{r['id']}] {r['label']} = {'通过' if self.req_status[r['id']]['ok'] else '未通过'}"
                                    for r in unreported) + "."))
        return out

    def checks_report(self) -> str:
        reqs = (self.contract or {}).get("req_checks") or []
        if not reqs:
            return ""
        lines = []
        for r in reqs:
            st = self.req_status.get(r["id"])
            mark = "未检查" if not st else ("✓ 通过" if st["ok"] else "✗ 未通过")
            lines.append(f"- {r['label']}：{mark}" + (f"（{st['detail']}）" if st and not st["ok"] else ""))
        return "\n\n需求检查（verify_html）：\n" + "\n".join(lines)

    def problems(self, git_changed: Optional[bool], losses: list, summary: str = "") -> list[tuple]:
        c = self.contract or {}
        out = []
        changed = bool(git_changed) or bool(self.source_writes) or self.patched
        if not changed:
            out.append(("no_source", "拒绝 done：源头没有真实改动（git diff 为空，也没有写过源文件）",
                        "REJECTED: there is no real source change: git diff in the allowed paths is empty and no source file "
                        f"was written. Fix the cause in the source ({c.get('source_of_truth') or 'the script that generates the file'}), "
                        "not in the generated output, then regenerate and verify."))
        gen = self.generator
        full_after = bool(self.gen_runs) and max(self.gen_runs) >= self.last_source_step
        fast_after = bool(self.fast_runs) and max(self.fast_runs) >= self.last_source_step
        if gen and changed and not full_after and fast_after:   # v3.9.29: iterated on the cache, never ran it for real
            out.append(("no_full_run", "拒绝 done：只用快速模式（缓存）生成过，done 前必须完整运行一次生成命令",
                        f"REJECTED: after the last source change (step {self.last_source_step}) the generator only ran in fast/"
                        f"cache mode. Run the FULL generator once now: {c.get('generator_cmd') or gen}, then verify that output."))
        elif gen and changed and not full_after:
            out.append(("not_regenerated", "拒绝 done：改了源头之后没有重新生成产物",
                        f"REJECTED: the source was changed (step {self.last_source_step}) but the generator was not run again "
                        f"after that. Run it now: {c.get('generator_cmd') or gen}, then verify the REGENERATED file."))
        last = max([self.last_source_step] + self.gen_runs)
        if changed and not any(s >= last for s in self.verify_ok + self.test_ok):
            out.append(("no_acceptance", "拒绝 done：改动后没有验收证据（verify_html / 测试没有在最后一次改动后通过）",
                        "REJECTED: no acceptance evidence after the last change. Run verify_html on the regenerated file "
                        f"({self.deliverable() or 'the deliverable'}) with the contract's checks (selectors: "
                        f"{((c.get('checks') or {}).get('selectors') or [])[:6]}; clicks for every acceptance item), or the "
                        "project's tests, and make them pass. Put the evidence in the done summary."))
        out += self.req_problems(summary, last)
        if losses:
            out.append(("regression", "拒绝 done：和上一版交付物相比丢失了：" + "；".join(x["text"] for x in losses[:6]),
                        "REJECTED: compared with the previous deliverable these features are missing now: "
                        + "; ".join(x["text"] for x in losses[:8]) + ". Restore them in the source and regenerate (the user "
                        "did not ask to remove them)."))
        return out

    def done_gate(self, git_changed: Optional[bool], losses: list, summary: str = "") -> tuple[bool, Optional[str], Optional[str]]:
        """-> (let done through, note for the model, message for the timeline)."""
        if not self.dev:
            return True, None, None
        probs = self.problems(git_changed, losses, summary)
        if not probs:
            return True, None, None
        if self.done_rejects < dev_done_rejects():
            self.done_rejects += 1
            msg = probs[0][1] + (f"（另有 {len(probs) - 1} 项）" if len(probs) > 1 else "")
            note = "\n".join(p[2] for p in probs) + f"\n(dev gate {self.done_rejects}/{dev_done_rejects()})"
            return False, note, msg
        self.known_issues = [p[1].replace("拒绝 done：", "") for p in probs]
        return True, None, None

    def known_tail(self) -> str:
        out = self.checks_report()
        if self.known_issues:
            out += "\n\n已知问题（系统的开发流程检查没有通过，已放行交付）：\n" + "\n".join("- " + k for k in self.known_issues)
        return out

    def state(self) -> dict:
        return {"intent": self.intent, "source_writes": self.source_writes, "gen_runs": self.gen_runs,
                "verify_ok": self.verify_ok, "test_ok": self.test_ok, "done_rejects": self.done_rejects,
                "vblocked": sorted(self.vblocked), "fast_runs": self.fast_runs, "pending_scripts": sorted(self.pending),
                "unexecuted": [u["path"] for u in self.unexec], "req_status": self.req_status,
                "stuck": self.stuck_text()}
