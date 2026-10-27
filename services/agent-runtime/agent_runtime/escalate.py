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

import re
from typing import Optional

# captcha / risk-control / login walls a model cannot pass (same wording family as apps/web/mate-map.js BLOCKED)
BLOCKED = re.compile(
    r"刷新验证码|验证码|人机|滑块|拖动.{0,6}(滑块|完成|拼图)|安全验证|身份验证|风控|风险提示|操作频繁|访问频繁|请求频繁|账号异常|"
    r"扫码登录|短信验证|captcha|recaptcha|hcaptcha|turnstile|cloudflare|verify (you are|that you are) human|"
    r"human check|are you a robot|rate.?limit|too many requests|risk control|slide to verify|security check", re.I)

# a step that only looks: it cannot get a stuck page any further
LOOK_TOOLS = {"screenshot", "bos_snapshot", "bos_read", "bos_grep", "bos_diff", "bos_wait", "wait", "files_list",
              "files_read", "read_image"}

STALL_NUDGE = 5       # look-only steps in a row before the model is told to act or ask
STALL_ESCALATE = 8    # ... before the user is asked
BLOCKED_LOOKS = 2     # look-only steps after a captcha/login wall showed up before a takeover is requested

KIND_TEXT = {
    "fail": "模型判断这一步做不下去",
    "max_steps": "步数用完了，任务还没做完",
    "time": "时间快用完了，任务还没做完",
    "errors": "连续几步都出错",
    "repeat": "模型在重复同一个操作",
    "noargs": "模型连续几次没给出操作参数",
    "stall": "模型连续好几步只在看页面、没有往前推进",
    "blocked": "页面出现了需要真人处理的验证",
    "error": "运行中出现了错误",
}

OPT_RETRY, OPT_TAKEOVER, OPT_STOP = "继续，换个方法再试", "我来接管桌面", "停止任务"
OPTIONS = [OPT_RETRY, OPT_TAKEOVER, OPT_STOP]


def is_look(tool: str, args: Optional[dict] = None) -> bool:
    if tool in LOOK_TOOLS:
        return True
    if tool == "bos_tabs" and str((args or {}).get("action") or "list") in ("list", "front"):
        return True
    return False


def blocked_hint(text: str) -> str:
    """The wall's own words ('刷新验证码', 'captcha') when text shows one, else ''."""
    m = BLOCKED.search(text or "")
    return m.group(0) if m else ""


class Stall:
    """Counts steps that only look, and remembers a captcha/login wall seen on the page."""

    def __init__(self):
        self.looks = 0
        self.blocked = ""        # the wall's wording, while it is on the page
        self.blocked_looks = 0
        self.nudged = False

    def reset(self):
        self.__init__()

    def feed(self, tool: str, args: Optional[dict], result_text: str = "") -> Optional[str]:
        """After each step: None, 'nudge' (tell the model) or 'blocked' / 'stall' (ask the user)."""
        hint = blocked_hint(result_text) if result_text else ""
        if hint:
            self.blocked = hint
        if is_look(tool, args):
            self.looks += 1
            if self.blocked:
                self.blocked_looks += 1
        else:
            # a real action; reloading / reopening the same wall page does not count as getting past it
            if tool not in ("bos_navigate", "bos_tabs", "browser_open", "browser_navigate") or not self.blocked:
                self.looks = 0
                self.nudged = False
            if tool == "bos_act" and not hint:
                self.blocked, self.blocked_looks = "", 0
            elif self.blocked and tool != "bos_act":   # reloading / reopening the wall page
                self.blocked_looks += 1
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
    lines.append("你想怎么处理？")
    return "\n".join(lines)


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
    return "retry"
