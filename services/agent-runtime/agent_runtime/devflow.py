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
Pure state (no I/O); loop.py gathers git / fingerprint facts and calls in. Tested in tests/test_v3926_devflow.py.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import provenance as _pv
from .provenance import ADHOC, SOURCE_EXT, ProvenanceBook, norm

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


def written_files(tool: str, args: dict) -> set:
    """Files a step writes (best effort)."""
    a = args or {}
    if tool == "files_write":
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

    # ------------------------------------------------------------------ contract helpers
    @property
    def generator(self) -> Optional[str]:
        return (self.contract or {}).get("source_of_truth") if (self.contract or {}).get("deliverables") else None

    def sources(self) -> tuple:
        c = self.contract or {}
        return tuple(x for x in (c.get("allowed_paths") or []) if SOURCE_EXT.search(x or ""))

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
        warn = None
        w = _pv.written_script(tool, args)
        if w:
            for art in self.book.artifacts():
                if _pv.code_writes(w[1], art):
                    self.patchers[w[0]] = art
        adhoc = _pv.adhoc_patch_script(tool, args)
        if adhoc and adhoc not in self.warned:
            self.warned.add(adhoc)
            warn = _pv.adhoc_warning(adhoc, (self.contract or {}).get("source_of_truth"))
        return None, warn

    # ------------------------------------------------------------------ after a tool ran
    def post_tool(self, step: int, tool: str, args: dict, ok: bool, stdout: str = "", error: str = "") -> Optional[str]:
        a = args or {}
        note = None
        if tool == "shell":
            cmd = str(a.get("command") or "")
            self.shell_log.append({"step": step, "command": cmd[:2000], "stdout": str(stdout or "")[-4000:]})
            self.shell_log = self.shell_log[-120:]
            gen = self.generator
            sc = _pv.script_of(cmd)
            if ok and gen and sc and os.path.basename(sc) == os.path.basename(gen):
                self.gen_runs.append(step)
                for f in _pv.json_file_line(stdout):
                    self.gen_outputs.append(norm(f))
            if ok and _TEST_CMD.search(cmd):
                self.test_ok.append(step)
            if re.search(r"\bgit\s+apply\b|\bpatch\s+-p\d|\bapply_patch\b", cmd) and ok:
                self.patched, self.last_source_step = True, step
        if not self.dev:
            return None
        wrote = written_files(tool, a) if ok else set()
        for p in wrote:
            if self._is_source(p):
                self.source_writes[p] = step
                self.last_source_step = step
        # repeated diagnosis: 3 read-only inspections in a row on the same file(s), nothing written
        files = read_only_files(tool, a)
        if wrote or tool in ("files_write",):
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
                            "（用 files_write 或 sed/python 写入），然后重新生成并验证。STOP inspecting: make the edit now.")
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

    def problems(self, git_changed: Optional[bool], losses: list) -> list[tuple]:
        c = self.contract or {}
        out = []
        changed = bool(git_changed) or bool(self.source_writes) or self.patched
        if not changed:
            out.append(("no_source", "拒绝 done：源头没有真实改动（git diff 为空，也没有写过源文件）",
                        "REJECTED: there is no real source change: git diff in the allowed paths is empty and no source file "
                        f"was written. Fix the cause in the source ({c.get('source_of_truth') or 'the script that generates the file'}), "
                        "not in the generated output, then regenerate and verify."))
        gen = self.generator
        if gen and changed and (not self.gen_runs or max(self.gen_runs) < self.last_source_step):
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
        if losses:
            out.append(("regression", "拒绝 done：和上一版交付物相比丢失了：" + "；".join(x["text"] for x in losses[:6]),
                        "REJECTED: compared with the previous deliverable these features are missing now: "
                        + "; ".join(x["text"] for x in losses[:8]) + ". Restore them in the source and regenerate (the user "
                        "did not ask to remove them)."))
        return out

    def done_gate(self, git_changed: Optional[bool], losses: list) -> tuple[bool, Optional[str], Optional[str]]:
        """-> (let done through, note for the model, message for the timeline)."""
        if not self.dev:
            return True, None, None
        probs = self.problems(git_changed, losses)
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
        if not self.known_issues:
            return ""
        return "\n\n已知问题（系统的开发流程检查没有通过，已放行交付）：\n" + "\n".join("- " + k for k in self.known_issues)

    def state(self) -> dict:
        return {"intent": self.intent, "source_writes": self.source_writes, "gen_runs": self.gen_runs,
                "verify_ok": self.verify_ok, "test_ok": self.test_ok, "done_rejects": self.done_rejects,
                "vblocked": sorted(self.vblocked)}
