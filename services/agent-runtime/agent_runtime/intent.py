"""v3.9.26 (task-830): what kind of job a request is — dev_fix / run / other.

task-830 (thread thr-8d8cfb0ce9) continued a "run ~/ai-workstation/news/aiw_news.py, 不要修改脚本" task with
"改了样式点击又进入不了详情，GitHub也没了，修复". Nothing in aiw knew this was now a code fix: the dev gates only fired
for an explicit 【技能：dev】 tag, the old "不要修改脚本" stayed in the prompt, and the agent patched the generated HTML
for 98 steps instead of fixing the generator.

classify() decides from the words AND the thread:
* dev verbs (修复 / 修一下 / 改成 / 加个 / 点不进 / 进不去 / 没了 / 不见了 / 报错 / bug / 样式 / 重构 / fix / implement …),
  with negated clauses removed first ("不要修改脚本" is a constraint, not a modify verb);
* code signals: the thread sent html/js/py files, ran python/node scripts, or names repo / source paths.
A strong verb (bug, 报错, 点不进, 样式, fix) with any code signal, or any dev verb with code words in the request itself,
is dev_fix. The explicit 【技能：dev】 tag always is (forced).

voided_constraints() lists the old constraints a dev_fix continuation overrides (不要修改脚本 / 不要改代码 / 不要重试 …).
Pure, tested in tests/test_v3926_devflow.py.
"""
from __future__ import annotations

import re
from typing import Optional

DEV_FIX, RUN, OTHER = "dev_fix", "run", "other"
INTENTS = (DEV_FIX, RUN, OTHER)

FORCED_DEV = re.compile(r"【技能[:：][^】]*\bdev\b", re.I)

# a negated clause: from the negation word to the end of the clause
NEGATED = re.compile(r"(不要|不用|不必|不准|不许|不能|不可以|禁止|严禁|别|勿|无需|千万别|\bdon'?t\b|\bdo not\b|\bnever\b|"
                     r"\bwithout\b|\bno need to\b)[^，。；！？,;.!?\n]{0,40}", re.I)

# verbs that alone say "something in the product is broken / must change"
STRONG = re.compile(r"修复|修一下|修好|修下|修正|bug|报错|出错了?|异常|崩溃|白屏|点不(了|进|开|动)|点击.{0,6}(不了|没反应|无效|没用)|"
                    r"进不(去|了)|进入不了|打不开|没反应|不能(点|用|打开|进入)|没了|丢了|不见了|消失了?|失效|坏了|样式|重构|"
                    r"优化代码|回归|\bfix(es|ed)?\b|\bimplement\b|\brefactor\b|\bbroken\b|\bregression\b|"
                    r"(does ?n[o']t|not) work", re.I)
# verbs that change something; dev only with code around
WEAK = re.compile(r"改一?下|改成|改为|改好|改回|改掉|改了|修改|调整|加个|加一个|加上|增加功能|增加|添加|新增|去掉|删掉|恢复|"
                  r"换成|优化|实现|支持|\badd\b|\bchange\b|\bupdate\b|\bmodify\b|\bremove\b", re.I)
# the request itself talks about code / a page
CODE_WORDS = re.compile(r"脚本|代码|源码|函数|接口|页面|网页|按钮|点击|详情|链接|弹窗|样式|css|html|\bjs\b|javascript|python|"
                        r"\.py\b|\.js\b|\.ts\b|\.html?\b|\.css\b|生成器|模板|前端|后端|程序|插件|仓库|repo|git\b", re.I)
RUN_WORDS = re.compile(r"运行|跑一?下|跑一遍|执行|生成(一份|今天|今日|最新)?|抓取|爬取|采集|每天|定时|出一份|做一份|\brun\b|"
                       r"\bexecute\b|\bgenerate\b", re.I)

# v3.9.30: a request to write a new program / site / tool is a coding task too (the dev loop runs, new-project mode)
CODING_NEW = re.compile(r"(?:写|开发|实现|搭建|编写|做|造|撸)(?:一个|一套|一款|个|套|款)?[^，。；,;\n]{0,10}?"
                        r"(?:程序|脚本|网站|网页应用|web\s*app|应用|App|app|小程序|插件|扩展|后端|前端|接口|API|服务端|命令行工具|"
                        r"CLI|cli|机器人|bot|爬虫|游戏|库|SDK|sdk)|\b(?:build|write|implement|develop|code)\s+(?:me\s+)?"
                        r"(?:an?\s+)?(?:\w+\s+){0,3}(?:app|application|website|web app|script|cli|tool|bot|api|server|"
                        r"extension|plugin|game|library)\b", re.I)
NEW_BUILD_REASON = "编码任务（新项目）"

# paths / commands that mean the thread has code or script-made artifacts
_CODE_FILE = re.compile(r"[\w./~-]+\.(?:py|js|mjs|ts|tsx|jsx|html?|css|vue|go|rs|java|sh)\b", re.I)
_SCRIPT_RUN = re.compile(r"\b(?:python3?|node|bash|sh|deno|bun|npx|uv run)\s+[^\s;&|]*\.(?:py|js|mjs|ts|sh)\b", re.I)
_REPO_PATH = re.compile(r"(?:~|/home/[\w.-]+|/data/home|/root)/[\w.-]+/(?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|sh|html?|css)\b|"
                        r"\bgit (?:diff|status|log|commit)\b", re.I)

# old constraints a code fix overrides
CONFLICT = re.compile(r"(?:不要|不准|不许|不能|禁止|严禁|别|勿|无需|不用)(?:再)?(?:去)?(?:修改|改动|改|动|碰|编辑|重写)"
                      r"[^，。；,;.\n]{0,12}(?:脚本|代码|源码|程序|文件|生成器|源文件|源头)?|"
                      r"(?:不要|别|不准|禁止)(?:再)?重试|只(?:需要?)?(?:运行|执行|跑)[^，。；,;.\n]{0,20}|"
                      r"(?:don'?t|do not|never)\s+(?:modify|change|edit|touch|rewrite)\b[^,.;\n]{0,30}|"
                      r"(?:don'?t|do not)\s+retry\b[^,.;\n]{0,20}", re.I)


def strip_negated(text: str) -> str:
    """The request with its negated clauses removed ("不要修改脚本" is not a modify verb)."""
    return NEGATED.sub(" ", text or "")


def signals_from_text(text: str) -> dict:
    """Code signals from free text (the thread context / earlier turns) when no stored history is at hand."""
    t = text or ""
    files = sorted(set(m.group(0) for m in _CODE_FILE.finditer(t)))[:40]
    runs = sorted(set(m.group(0) for m in _SCRIPT_RUN.finditer(t)))[:20]
    repo = sorted(set(m.group(0) for m in _REPO_PATH.finditer(t)))[:20]
    return {"sent_files": [], "code_files": files, "script_runs": runs, "repo_paths": repo}


def has_code(signals: Optional[dict]) -> bool:
    s = signals or {}
    sent = [f for f in s.get("sent_files") or [] if _CODE_FILE.search(str(f))]
    return bool(sent or s.get("script_runs") or s.get("repo_paths") or s.get("code_files") or s.get("provenance"))


def classify(text: str, signals: Optional[dict] = None, context: Optional[str] = None) -> dict:
    """-> {"intent": dev_fix|run|other, "forced": bool, "reasons": [..]}.
    `signals` come from the thread's stored history (sent files, script runs, provenance); `context` (free text) is a
    weaker fallback used only when no signals are given."""
    t = text or ""
    if FORCED_DEV.search(t):
        return {"intent": DEV_FIX, "forced": True, "reasons": ["显式标签【技能：dev】"]}
    sig = signals if signals is not None else signals_from_text(context or "")
    plain = strip_negated(t)
    strong = [m.group(0) for m in STRONG.finditer(plain)]
    weak = [m.group(0) for m in WEAK.finditer(plain)]
    code_words = [m.group(0) for m in CODE_WORDS.finditer(plain)]
    code_thread = has_code(sig)
    reasons = []
    if strong and (code_thread or code_words):
        reasons.append("问题/修复用词：" + "、".join(dict.fromkeys(strong))[:60])
    elif weak and code_words and (code_thread or re.search(r"脚本|代码|源码|函数|接口|\.py|\.js|css|html", plain, re.I)):
        reasons.append("修改用词 + 代码相关：" + "、".join(dict.fromkeys(weak + code_words))[:60])
    elif weak and code_thread and not RUN_WORDS.search(plain):
        reasons.append("修改用词，且对话里有脚本生成的文件：" + "、".join(dict.fromkeys(weak))[:60])
    if reasons:
        if code_thread:
            reasons.append("对话里有代码/脚本产物")
        return {"intent": DEV_FIX, "forced": False, "reasons": reasons}
    if CODING_NEW.search(plain):
        return {"intent": DEV_FIX, "forced": False, "reasons": [NEW_BUILD_REASON if not code_thread else "编码任务",
                                                                "写程序/网站/工具：" + CODING_NEW.search(plain).group(0)[:40]]}
    if RUN_WORDS.search(plain) or (sig.get("script_runs") and len(plain.strip()) <= 12 and re.search(r"再来|重新|再跑|再生成", plain)):
        return {"intent": RUN, "forced": False, "reasons": ["运行/生成类请求"]}
    return {"intent": OTHER, "forced": False, "reasons": []}


def voided_constraints(old_text: str, new_intent: str) -> list[str]:
    """Old instructions' constraints that the new intent overrides (a code fix may edit the script)."""
    if new_intent != DEV_FIX:
        return []
    out = []
    for m in CONFLICT.finditer(old_text or ""):
        s = m.group(0).strip(" ，,。")
        if s and s not in out:
            out.append(s[:60])
    return out[:8]


def is_rollback_request(text: str) -> bool:
    """「撤销」「回滚」 on a dev task: undo the last fix (git rollback to the recorded HEAD) instead of a new run."""
    return bool(re.fullmatch(r"\s*(请|帮我)?(撤销|回滚|还原|撤回)(一下|吧)?(刚才|上次|这次)?(的)?(修改|改动|提交)?[。.!！]?\s*|"
                             r"\s*(undo|revert|roll ?back)( (it|that|the (last )?(change|fix)))?[.!]?\s*", text or "", re.I))
