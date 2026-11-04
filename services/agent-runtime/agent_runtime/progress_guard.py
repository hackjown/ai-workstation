"""v3.9.17 速度与效率: stop collecting once the answer is known, and never loop on the same conclusion.

task-833 ("ssh 到 docker 服务器查哪些容器占用高", 23 min, 14 steps, stopped by the user, no report): the cause was clear
at step 8-9 (~9 min) and every later thought said "已查清/基本查清/已掌握大部分，再补一组数据" while six more scripts
ran. The rules here:

(a) sufficiency — a thought that says it knows (已查清 / 基本查清 / 已掌握 / 关键线索已经 / 信息已足够) on a question /
    diagnosis task, with something on the findings board, switches the task to DELIVER: the step that said it may
    still run (that is the one extra collection), the next step must deliver (done / send_file / a report file /
    ask_user). One more collection is allowed only when that thought names what is missing and none was used yet.
    A model that still does not deliver gets a report written from the findings board for it.
(b) budget — a diagnosis / analysis task delivers a first version at ~8 steps or ~10 minutes (AIW_DIAG_STEPS,
    AIW_DIAG_SECONDS), ending with "要深挖的话回复…"; the soft cap AIW_DIAG_SOFT_CAP (60) applies when (b) is off.
(2) stuck — the same conclusion 3 times (AIW_SAME_FACT_LIMIT) or 20 steps without a new fact (AIW_NO_FACT_STEPS)
    pauses and asks the user, with what is known so far.

Pure state machine (no I/O): the loop calls decide() once per model decision. Tested in tests/test_v3917_progress.py
and the task-833 replay in tests/test_v3917_replay833.py.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from .findings import FindingsBoard, names_missing, says_sufficient

DIAG_TASK = re.compile(
    r"诊断|排查|分析|查(一下|一查|看|查|找|明|清|出|下|哪些|为什么|原因|是谁|什么)|看看|检查|定位|为什么|原因|占用|慢在哪|怎么回事|"
    r"调查|对比|对照|比较|审计|评估|报告|\b(diagnos|investigat|analy[sz]|troubleshoot|why|audit|inspect|check)\w*", re.I)
MAKE_TASK = re.compile(r"画|绘制|生成|制作|做一个|做个|写一个|写个|开发|实现|部署|安装|下单|购买|注册|发布|发帖|修图|剪辑|渲染|"
                       r"转成|转换|改成|装修|设计|订(票|机票|酒店|餐)|预订|预约|发送|发给|回复|填写|提交|上传|删除|修复|修好|修改|"
                       r"重启|迁移|备份|配置|登录|签到|领取|抢|draw|build|deploy|install|create|generate|render|book|send|fix|"
                       # v3.9.20 task-62c: 「看看这个网址，能不能复刻」 is building, not a diagnosis
                       r"复刻|仿站|仿一个|仿做|克隆|还原|逆向|破解|抓包|爬取|跑通|打通|搭建|搭一个|做出来|"
                       r"clone|replicate|reverse|scrape|【技能[:：][^】]*(dev|reverse|engineer)", re.I)
# v3.9.20: the user asks for depth or to keep going: never cut it short with "已查清→交付"
DEEP_TASK = re.compile(r"深挖|继续挖|继续深入|往下挖|挖到底|别停|不要停|做完再|做到.{0,6}(为止|才)|必须.{0,12}(才|再)汇报|"
                       r"直到|不达.{0,4}不|【接着上次继续做】", re.I)
# delivering, not collecting
DELIVER_TOOLS = {"done", "send_file", "ask_user", "note_finding", "update_plan", "remember", "request_takeover", "fail"}
_REPORT_FILE = re.compile(r"\.(md|txt|html?|docx?|pdf|csv|xlsx?)$", re.I)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def is_diagnostic(prompt: str) -> bool:
    p = prompt or ""
    return bool(DIAG_TASK.search(p)) and not MAKE_TASK.search(re.sub(r"(给|出|写|交)(一份|个)?(分析)?报告", "", p))


def is_delivery(tool: str, args: Optional[dict] = None) -> bool:
    if tool in DELIVER_TOOLS:
        return True
    if tool == "files_write":
        a = args or {}
        return bool(_REPORT_FILE.search(str(a.get("path") or ""))) and "paramiko" not in str(a.get("content") or "")
    return False


class ProgressGuard:
    def __init__(self, prompt: str, board: Optional[FindingsBoard] = None, diagnostic: Optional[bool] = None,
                 question: bool = False):
        self.board = board or FindingsBoard()
        self.diagnostic = is_diagnostic(prompt) if diagnostic is None else diagnostic
        self.answer_task = self.diagnostic or question
        self.step_budget = _env_int("AIW_DIAG_STEPS", 8)
        self.time_budget = _env_int("AIW_DIAG_SECONDS", 600)
        self.soft_cap = _env_int("AIW_DIAG_SOFT_CAP", 60)
        self.same_limit = _env_int("AIW_SAME_FACT_LIMIT", 5)   # v3.9.20: was 3 (asked at step 3)
        self.ask_min_steps = _env_int("AIW_STUCK_MIN_STEPS", 12)
        self.no_fact_steps = _env_int("AIW_NO_FACT_STEPS", 20)
        self.mode = "work"            # work | deliver
        self.reason = ""              # sufficient | budget | soft_cap
        self.armed_at = 0             # step that switched to deliver
        self.extra_used = False
        self.last_new = 0             # step of the last new fact
        self.budget_done = False      # the first version was asked for already
        self.forced = 0
        self.last_thought = ""
        self.off = os.environ.get("AIW_PROGRESS_GUARD", "1") == "0"
        # v3.9.20 (task-62c 「继续深挖」 delivered at step 1 from the facts the LAST run found): only facts found in this
        # run can say "known", never before AIW_DELIVER_MIN_STEPS steps, and not at all when the user asked for depth
        self.deep = bool(DEEP_TASK.search(prompt or ""))
        self.min_steps = _env_int("AIW_DELIVER_MIN_STEPS", 4)
        self.inherited = 0            # facts carried over from the run this one continues
        self.new_here = 0             # facts found in this run

    def inherit(self) -> None:
        """The board was seeded from the previous run: those facts do not count as this run's progress."""
        self.inherited = len(self.board.items)
        for i in self.board.items:
            i["repeats"] = 0

    # --------------------------------------------------------------- feed
    def on_fact(self, step: int) -> None:
        self.last_new = step

    def reset_after_ask(self, step: int) -> None:
        """The user said go on: fresh counters (the known facts stay)."""
        self.last_new = step
        for i in self.board.live():
            i["repeats"] = 0
        self.mode, self.extra_used = "work", False

    def decide(self, step: int, thought: str, tool: str, args: Optional[dict] = None,
               elapsed_s: float = 0.0, progress_at: int = 0) -> tuple[str, str]:
        """('ok'|'extra'|'deliver_now'|'force_done'|'ask', note). Call once per decision, before it runs."""
        if self.off:
            return "ok", ""
        self.last_thought = thought or self.last_thought
        self.last_new = max(self.last_new, progress_at)   # a plan tick / new page / file sent / user answer counts too
        new, _rep = self.board.capture(thought or "", step)
        if new:
            self.last_new = step
            self.new_here += 1
        delivering = is_delivery(tool, args)
        if self.mode == "deliver":
            if delivering:
                return "ok", ""
            if not self.extra_used and names_missing(thought):
                self.extra_used = True
                return "extra", ("This is the LAST data collection allowed (you named what is missing). After this "
                                 "step deliver the report with done; no more collecting.")
            self.forced += 1
            return "force_done", ""
        # switching to deliver
        why = ""
        if self.answer_task and not self.deep and says_sufficient(thought) and self.board.live() \
                and step >= self.min_steps and (self.new_here > 0 or not self.inherited):
            why = "sufficient"
        elif self.diagnostic and not self.deep and not self.budget_done and (
                (self.step_budget > 0 and step >= self.step_budget) or (self.time_budget > 0 and elapsed_s >= self.time_budget)):
            why = "budget"
        elif self.diagnostic and not self.deep and self.soft_cap > 0 and step >= self.soft_cap and not self.budget_done:
            why = "soft_cap"
        if why:
            self.mode, self.reason, self.armed_at = "deliver", why, step
            if why != "sufficient":
                self.budget_done = True
            if not delivering:
                self.extra_used = True   # this step's collection is the one extra
            return "deliver_now", self.deliver_note(why, elapsed_s, step)
        # stuck
        # v3.9.20: a thought that restates a known fact while working is normal; only a question / diagnosis task
        # that has run a while and keeps landing on the same conclusion asks
        if self.answer_task and step >= self.ask_min_steps and self.board.max_repeats() >= self.same_limit - 1:
            return "ask", "同一个结论已经重复了 {} 次，没有新进展".format(self.board.max_repeats() + 1)
        # 20 steps without a new fact: only for a task whose job is an answer (diagnosis / question). A long action
        # task (a 45-step price comparison) keeps running: its valve is the 150-step check-in (3.8.2)
        if self.answer_task and self.no_fact_steps > 0 and step - self.last_new >= self.no_fact_steps:
            return "ask", f"已经 {step - self.last_new} 步没有得出新的事实"
        return "ok", ""

    # --------------------------------------------------------------- words
    def deliver_note(self, why: str, elapsed_s: float = 0.0, step: int = 0) -> str:
        head = {"sufficient": "You said you already know the answer (已查清/已掌握).",
                "budget": f"This diagnosis has used {step} steps / {int(elapsed_s // 60)} min: the budget for a first answer.",
                "soft_cap": f"This diagnosis has run {step} steps."}.get(why, "")
        return (f"DELIVER NOW. {head} The CONFIRMED FINDINGS above answer the user's question. Your NEXT call must be "
                "done with the full report in Chinese in summary: 结论（哪些是主因，按影响排序）、关键数据/证据、"
                "建议怎么处理（可直接执行的命令）、还可以深挖的方向. Do NOT collect more data (no new script, no new "
                "ssh/sampling). Only if one specific number is truly missing for the answer, you may run ONE more "
                "collection and must say exactly what is missing.")

    def tail(self) -> str:
        """The line a first-version report ends with (b): the user decides about digging deeper."""
        if self.reason in ("budget", "soft_cap") or self.diagnostic:
            return "\n\n—— 以上是第一版结论。要继续深挖哪一部分，直接回复「继续深挖：…」。"
        return ""

    def synth_report(self) -> str:
        facts = self.board.report() or "（事实板为空）"
        last = re.sub(r"\s+", " ", self.last_thought or "").strip()
        last = re.split(r"(再补|下一步|还需要|还差|我先补|然后)", last)[0].strip(" ，,")[:600]
        return ("诊断结论（按已确认事实整理；模型还想继续补数据，已按效率规则先交付）：\n" + facts +
                (f"\n\n模型最后的判断：{last}" if last else "") + self.tail())

    def ask_text(self, detail: str) -> str:
        facts = self.board.report()
        return detail + ("。目前已确认：\n" + facts if facts else "") + "\n要我按现有结论直接出报告，还是换个方向继续？"
