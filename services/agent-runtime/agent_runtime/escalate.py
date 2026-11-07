"""No silent failures (2026-10-27, task-d76ffb0bd1).

That run ("继续完成注册" on chat.qwen.ai) hit an Aliyun captcha (刷新验证码) at step 8 and then spent 32 steps on
screenshot / bos_snapshot / bos_tabs and reloading the page, never calling request_takeover, until it ended with
"max steps (40) reached" and nothing told the user what was wrong.

The rule now: a task never ends as failed on its own. Whatever would have failed it (the model calling fail, the
step budget, repeated errors, repeating one call, looking without acting, the time budget) becomes a question to the
user with what happened, where it is stuck, and the choices 继续再试 / 我来接管 / 停止. A captcha, a login or an
SMS code on the page becomes a takeover request right away. Only when nobody can be asked (a sub-agent, or no
answer) does it end, and then with a full report.

Pure helpers here (tested in tests/test_escalate.py); the loop wiring is in loop.py (_escalate).
"""
from __future__ import annotations

import os
import re
from typing import Optional


def step_cap() -> int:
    """Hard step budget for a task. 0 (the default since 3.8.2) = none: a task runs until it is done or the user
    presses stop (task-c5a94187a0 ended at 40 steps half way through a Fliggy comparison). AIW_TASK_MAX_STEPS=N
    brings a budget back; reaching it still only pauses and asks, it never ends the task."""
    try:
        return max(0, int(os.environ.get("AIW_TASK_MAX_STEPS", "0") or 0))
    except ValueError:
        return 0


def time_cap() -> float:
    """Time budget in seconds, 0 (default) = none. AIW_TASK_TIMEOUT=N brings one back (pauses and asks)."""
    try:
        return max(0.0, float(os.environ.get("AIW_TASK_TIMEOUT", "0") or 0))
    except ValueError:
        return 0.0


def checkin_steps() -> int:
    """The soft safety valve: after this many steps with no visible progress (no plan item ticked, no file sent,
    no new page, no answer from the user) the task pauses and asks. AIW_TASK_CHECKIN_STEPS, default 150, 0 = off."""
    try:
        return max(0, int(os.environ.get("AIW_TASK_CHECKIN_STEPS", "150") or 0))
    except ValueError:
        return 150

# captcha / risk-control / login walls a model cannot pass (same wording family as apps/web/mate-map.js BLOCKED)
BLOCKED = re.compile(
    r"刷新验证码|验证码|人机|滑块|拖动.{0,6}(滑块|完成|拼图)|安全验证|身份验证|风控|风险提示|操作频繁|访问频繁|请求频繁|账号异常|"
    r"扫码登录|短信验证|captcha|recaptcha|hcaptcha|turnstile|cloudflare|verify (you are|that you are) human|"
    r"human check|are you a robot|rate.?limit|too many requests|risk control|slide to verify|security check|"
    r"重新登录|请先登录|登录已过期|登录失效|会话已过期|punish|baxia|RGV587|FAIL_SYS_USER_VALIDATE|x5sec|_____tmd_____", re.I)

# v3.9.17 人工关卡: tokens only a risk-control / login wall sends (Alibaba baxia/punish, x5sec, mtop error codes). Seen
# anywhere - a page, a shell/curl/ssh output, browser_network - they stop the task at once (no 2 looks first) and the
# user is told exactly what to do. A line that looks like source code (const/import/def/#, a grep hit "file:line:")
# is not a wall: the uniapi qwen-bx.ts work had "baxia" all over its files.
STRONG = re.compile(r"RGV587(_ERROR)?|FAIL_SYS_USER_VALIDATE|FAIL_SYS_SESSION_EXPIRED|FAIL_SYS_TOKEN_EX\w*|_____tmd_____|"
                    r"x5secdata|x5sec=|/punish\?|punish\.(htm|html)|baxia-?punish|nocaptcha|\bbx-?(punish|captcha)\b|"
                    r"滑块验证|拖动滑块|请拖动|重新登录|登录已过期|登录失效|请先登录|session expired|please log ?in again", re.I)
_CODE_LINE = re.compile(r"^\s*(const|let|var|import|from|def|class|function|export|#|//|\*|/\*)|^[\w./-]+:\d+:|"
                        r"\bre\.compile\(|new RegExp|=>|\breturn\b|\bif\s*\(")
GATE_TODO = {
    "slider": "在电脑桌面的浏览器里拖动滑块（或完成图形验证），页面恢复正常后点「完成，交还给 Aiw」",
    "login": "在电脑桌面的浏览器里重新登录这个网站（扫码或输入账号密码），登录好后点「完成，交还给 Aiw」",
    "risk": "这个网站的风控（baxia/x5sec/RGV587）拦住了自动访问：请在桌面浏览器里打开该页面，按提示完成验证（滑块/验证码/重新登录），完成后点「完成，交还给 Aiw」；若一直弹，可能是 IP 被风控，需要换网络或稍后再试",
    "captcha": "在页面上完成验证码，完成后点「完成，交还给 Aiw」",
}


def human_gate(text: str) -> tuple[str, str]:
    """('', '') or (the wall's word, kind) for a strong risk-control / re-login token outside source code."""
    for line in str(text or "").splitlines()[:4000]:
        m = STRONG.search(line)
        if not m or _CODE_LINE.search(line):
            continue
        w = m.group(0)
        if re.search(r"滑块|拖动|nocaptcha", w, re.I):
            return w, "slider"
        if re.search(r"登录|log ?in|SESSION_EXPIRED|TOKEN_EX", w, re.I):
            return w, "login"
        return w, "risk"
    return "", ""


def gate_todo(word: str) -> str:
    w = word or ""
    kind = "slider" if re.search(r"滑块|拖动|slide|nocaptcha", w, re.I) else \
        "login" if re.search(r"登录|log ?in|SESSION_EXPIRED|TOKEN", w, re.I) else \
        "risk" if STRONG.search(w) or re.search(r"punish|baxia|风控", w, re.I) else "captcha"
    return GATE_TODO[kind]

# a step that only looks: it cannot get a stuck page any further
LOOK_TOOLS = {"screenshot", "bos_snapshot", "bos_read", "bos_grep", "bos_diff", "bos_wait", "wait", "files_list",
              "files_read", "read_image"}

STALL_NUDGE = 5       # look-only steps in a row before the model is told to act or ask
STALL_ESCALATE = 8    # ... before the user is asked
BLOCKED_LOOKS = 2     # look-only steps after a captcha/login wall showed up before a takeover is requested

KIND_TEXT = {
    "fail": "模型判断这一步做不下去",
    "max_steps": "步数用完了，任务还没做完",
    "checkin": "已经做了很多步，最近一直没有明显进展",
    "time": "时间快用完了，任务还没做完",
    "errors": "连续几步都出错",
    "repeat": "模型在重复同一个操作",
    "noargs": "模型连续几次没给出操作参数",
    "stall": "模型连续好几步只在看页面、没有往前推进",
    "blocked": "页面出现了需要真人处理的验证",
    "error": "运行中出现了错误",
    "vision_down": "读图服务（视觉模型）不可用",
    "drawing_loop": "模型反复分析同一件事，没有动手写 spec、生成图纸",
    "drawing_verify": "图纸 spec 已生成，但核对没通过、模型在原地打转",
    "stuck_fact": "同一个结论反复出现 / 很久没有新事实，先给你汇报",
    "rabbit": "同一个计划步骤做了很久还没完成，可能在环境问题上打转",
    "dev_rabbit": "修复代码的任务做了很多步还没有改好，可能在重复诊断或在验证环境上打转",
}

OPT_RETRY, OPT_TAKEOVER, OPT_STOP = "继续，换个方法再试", "我来接管桌面", "停止任务"
# v3.9.20: the guard can be wrong (the user sees it is working): go on with the SAME plan, and the same kind of
# check stays quiet for a while (AIW_STUCK_SNOOZE steps, default 30)
OPT_GO = "继续执行（没卡住）"
OPTIONS = [OPT_GO, OPT_RETRY, OPT_TAKEOVER, OPT_STOP]
NO_SNOOZE = {"blocked", "vision_down", "fail", "max_steps", "time"}


def rabbit_steps() -> int:
    """v3.9.22: steps on one plan item before a step-back note (2x: the user is asked). AIW_RABBIT_STEPS, 40, 0 = off."""
    try:
        return max(0, int(os.environ.get("AIW_RABBIT_STEPS", "40") or 0))
    except ValueError:
        return 40


def dev_rabbit_steps() -> int:
    """v3.9.26 (task-830): a dev_fix task steps back much sooner — task-830 repeated one diagnosis ~10 times and the
    user was only asked at step 80 (2 x 40). AIW_DEV_RABBIT_STEPS, default 15 (2x: the user is asked), 0 = off."""
    try:
        return max(0, int(os.environ.get("AIW_DEV_RABBIT_STEPS", "15") or 0))
    except ValueError:
        return 15


# v3.9.26 (task-830): a headless chrome / soffice started from the shell hung the step until the tool's own limit and
# left the process behind. They get a `timeout -k 5 N` in front unless the command already has one.
_RENDER_PROG = re.compile(r"(?P<pre>(?:^|&&|\|\||;|\n|\(|\bthen\b|\bdo\b)\s*(?:(?:sudo|nohup|env(?:\s+\w+=\S+)*)\s+)?)"
                          r"(?P<prog>(?:/[\w./-]+/)?(?:google-chrome(?:-stable)?|chromium(?:-browser)?|chrome|headless_shell|"
                          r"soffice|libreoffice)(?:\.bin)?)(?=\s|$)")


def render_timeout() -> int:
    try:
        return max(0, int(os.environ.get("AIW_RENDER_TIMEOUT", "120") or 0))
    except ValueError:
        return 120


def wrap_timeout(command: str, seconds: Optional[int] = None) -> str:
    """Put `timeout -k 5 N` in front of every chrome/chromium/soffice/libreoffice invocation that has none.
    A backgrounded long-lived browser (`… &`) is wrapped too: it is killed after N s instead of hanging around."""
    n = render_timeout() if seconds is None else seconds
    c = command or ""
    if not n or not _RENDER_PROG.search(c):
        return c

    def rep(m):
        before = c[:m.start("prog")]
        if re.search(r"\btimeout\s+(?:-\S+\s+(?:\d+\S*\s+)?)*\d+\S*\s*$", before) or re.search(r"\b(which|command -v|type|pkill|killall|kill|pgrep|apt|apt-get|dpkg|snap|echo|ls)\s+(-\S+\s+)*$", before):
            return m.group(0)
        seg = re.split(r"&&|\|\||;|\n|\|", c[m.end("prog"):], maxsplit=1)[0]
        if not re.search(r"--headless|--convert-to|--screenshot|--dump-dom|--print-to-pdf|--remote-debugging|--cat\b", seg):
            return m.group(0)   # a GUI app opened for the user stays as it is
        return f"{m.group('pre')}timeout -k 5 {n} {m.group('prog')}"
    return _RENDER_PROG.sub(rep, c)


def rabbit_note(idx: int, item: str, since: int) -> str:
    return (f"STEP BACK: plan item {idx} ({item}) has taken {since} steps and is not done. Look at what the last steps were: "
            "if you are debugging the ENVIRONMENT (headless browser / no GPU / sandbox / a vendor library's internals) "
            "rather than the user's product, stop digging. Record it with note_finding as 已知问题 with the evidence, then "
            "either use a workaround that verifies the same thing another way (e.g. a headed browser under Xvfb, a "
            "different renderer, a unit test) or move on to the next plan item and list it in 已知问题 at the end. If the "
            "choice changes what the user gets, ask_user with 2-4 concrete options. Do NOT write another probe script "
            "for the same error.")


def snooze_steps() -> int:
    try:
        return max(0, int(os.environ.get("AIW_STUCK_SNOOZE", "30") or 0))
    except ValueError:
        return 30
OPT_FIXED = "修好后继续"   # 3.8.8 vision_down: the user fixed the vision service; resets the breaker


def options(kind: str) -> list:
    if kind == "vision_down":
        return [OPT_FIXED, OPT_TAKEOVER, OPT_STOP]
    if kind in ("blocked", "fail"):   # a wall / the model gave up: going on unchanged makes no sense
        return [OPT_RETRY, OPT_TAKEOVER, OPT_STOP]
    return list(OPTIONS)


def is_look(tool: str, args: Optional[dict] = None) -> bool:
    if tool in LOOK_TOOLS:
        return True
    if tool == "bos_tabs" and str((args or {}).get("action") or "list") in ("list", "front"):
        return True
    return False


def blocked_hint(text: str) -> str:
    """The wall's own words ('刷新验证码', 'captcha') when text shows one, else ''.
    aiw-fix-1: a negated mention ("当前无弹窗或验证码", "no captcha") is not a wall."""
    t = text or ""
    for m in BLOCKED.finditer(t):
        pre = re.split(r"[。；;\n!！?？]", t[max(0, m.start() - 14):m.start()])[-1]
        if re.search(r"无|没有|未|不|并非|no\b|not\b|without", pre, re.I):
            continue
        return m.group(0)
    return ""


# aiw-fix-1: only these tools show a web page / the screen; shell stdout, file contents (a sing-box config with a
# "recaptcha" rule set, a script) and the vision text of a tab the task never used are not a captcha wall
PAGE_TOOLS = {"screenshot", "click", "double_click", "right_click", "type", "keypress", "scroll", "drag", "move",
              "click_element", "open_app"}
NAV_TOOLS = {"bos_navigate", "browser_open", "browser_navigate"}
RELOAD_TOOLS = NAV_TOOLS | {"bos_tabs"}


def page_tool(tool: str) -> bool:
    t = str(tool or "")
    return t in PAGE_TOOLS or t.startswith(("bos_", "browser_"))


class Stall:
    """Counts steps that only look, and remembers a captcha/login wall seen on the page."""

    def __init__(self):
        self.looks = 0
        self.blocked = ""        # the wall's wording, while it is on the page
        self.blocked_looks = 0
        self.nudged = False
        self.handled = set()     # aiw-fix-1: walls the user already cleared on this page

    def reset(self, handled: bool = False):
        keep = set(getattr(self, "handled", set()))
        if handled and self.blocked:
            keep.add(self.blocked.lower())
        self.__init__()
        self.handled = keep

    def feed(self, tool: str, args: Optional[dict], result_text: str = "") -> Optional[str]:
        """After each step: None, 'nudge' (tell the model) or 'blocked' / 'stall' (ask the user)."""
        if tool in NAV_TOOLS:          # a new page: a wall there is a new one
            self.handled = set()
        on_page = page_tool(tool)
        hint = blocked_hint(result_text) if (result_text and on_page) else ""
        strong, _kind = human_gate(result_text) if result_text else ("", "")
        if strong and on_page and _kind == "slider":
            strong = ""   # on a page a slider wording keeps the two-look rule below (a page can name one in passing)
        if strong and strong.lower() not in self.handled:   # v3.9.17: a risk-control token stops at once
            self.blocked, self.blocked_looks = strong, BLOCKED_LOOKS
            return "blocked"
        if hint and hint.lower() in self.handled:
            hint = ""
        if hint:
            self.blocked = hint
        if is_look(tool, args):
            self.looks += 1
            if self.blocked and on_page:
                self.blocked_looks += 1
        else:
            # a real action; reloading / reopening the same wall page does not count as getting past it
            if tool not in RELOAD_TOOLS or not self.blocked:
                self.looks = 0
                self.nudged = False
            if tool == "bos_act" and not hint:
                self.blocked, self.blocked_looks = "", 0
            elif self.blocked and tool in RELOAD_TOOLS:   # reloading / reopening the wall page
                self.blocked_looks += 1
            # shell / files / other non-page work never counts as looking at the wall
        if self.blocked and self.blocked_looks >= BLOCKED_LOOKS:
            return "blocked"
        if self.looks >= STALL_ESCALATE:
            return "stall"
        if self.looks >= STALL_NUDGE and not self.nudged:
            self.nudged = True
            return "nudge"
        return None


NUDGE = ("You have only been LOOKING for several steps (screenshot / snapshot / tabs) and the task has not moved. "
         "Do not take another look. Either take a real action that gets past what is shown, or, if a person is needed "
         "(captcha, slider, login, SMS code, QR scan, payment), call request_takeover NOW with what_to_do, or call "
         "ask_user explaining the problem. Never just stop.")


def recent_done(history: list[str], n: int = 6) -> list[str]:
    """The last few steps that did something (not looks), in short Chinese-friendly form."""
    out = []
    for h in reversed(history or []):
        s = str(h)
        tool = s.split(". ", 1)[-1].split("(", 1)[0].split(" ", 1)[0]
        if tool in LOOK_TOOLS or "SKIPPED" in s or "REJECTED" in s:
            continue
        out.append(s[:140])
        if len(out) >= n:
            break
    return list(reversed(out))


def question(kind: str, detail: str, page: Optional[dict] = None, blocked: str = "") -> str:
    """The plain-language question the user gets when the task cannot go on by itself."""
    why = KIND_TEXT.get(kind, KIND_TEXT["error"])
    where = ""
    if page and (page.get("title") or page.get("url")):
        where = f"（当前页面：{page.get('title') or ''} {page.get('url') or ''}".rstrip() + "）"
    lines = [f"我卡住了：{why}{where}。"]
    if blocked:
        lines.append(f"页面上有「{blocked}」，需要真人完成验证。")
    d = (detail or "").strip()
    if d:
        lines.append(f"具体情况：{d[:300]}")
    if kind == "vision_down":
        lines.append("这是服务故障，不是图片问题。任务已暂停：检查/修好视觉模型服务后点「修好后继续」/ 接管桌面 / 停止任务。")
    else:
        lines.append("任务已暂停，不会自己结束。你可以：继续执行（没卡住，照原计划做）/ 换个方法再试 / 接管桌面自己处理 / 停止任务。")
    return "\n".join(lines)


def stuck_card(kind: str, detail: str = "", blocked: str = "") -> dict:
    """What the task card shows while the task waits: what blocked it and what the user can do."""
    why = KIND_TEXT.get(kind, KIND_TEXT["error"])
    if blocked:
        why = f"页面出现「{blocked}」，需要真人验证"
    if kind == "vision_down":   # name the service: the notification and the card say what to fix
        why = str(detail or why).strip().split("。")[0][:120] or why
        return {"kind": kind, "reason": why, "detail": str(detail or "").strip()[:200],
                "actions": [OPT_FIXED, "接管", "停止"], "line": f"卡住了：{why} · 等你选 修好后继续 / 接管 / 停止"}
    return {"kind": kind, "reason": why, "detail": str(detail or "").strip()[:200],
            "actions": ["继续执行", "换个方法", "接管", "停止"],
            "line": f"卡住了：{why} · 等你选 继续执行 / 换个方法 / 接管 / 停止"}


def report(kind: str, detail: str, history: list[str], page: Optional[dict] = None, blocked: str = "",
           answer: Optional[str] = None) -> str:
    """The task's final result when it ends without being done: what happened, how far it got, what is needed."""
    why = KIND_TEXT.get(kind, KIND_TEXT["error"])
    parts = [f"任务没有完成：{why}。"]
    if blocked:
        parts.append(f"卡在：页面上的「{blocked}」需要真人处理。")
    if detail:
        parts.append(f"具体情况：{str(detail).strip()[:400]}")
    if page and (page.get("url") or page.get("title")):
        parts.append(f"停在的页面：{page.get('title') or ''} {page.get('url') or ''}".rstrip())
    done = recent_done(history)
    if done:
        parts.append("最后做过的步骤：\n" + "\n".join("· " + d for d in done))
    if answer is None:
        parts.append("没有收到你的回复，所以先停在这里。回复「继续」或点重试，我会从这里接着做；需要验证的话可以先接管桌面完成它。")
    else:
        parts.append("按你的选择已停止。点重试或回复「继续」可以从这里接着做。")
    return "\n".join(parts)


def choice(reply: Optional[str]) -> str:
    """'retry' | 'takeover' | 'stop' | 'none' from the user's answer (free text counts as retry with that hint)."""
    if reply is None:
        return "none"
    r = str(reply).strip()
    if not r:
        return "none"
    if r == OPT_STOP or r in ("停止", "停", "stop", "__cancel__", "取消", "算了", "不用了"):
        return "stop"
    if r == OPT_TAKEOVER or "接管" in r:
        return "takeover"
    if r == OPT_GO or re.fullmatch(r"(继续执行|继续|接着做|没卡住|没有卡住|继续执行（没卡住）)[。.!！]?", r):
        return "go"
    return "retry"
