"""v3.9.26 (task-830): the task contract of a dev_fix task, built by the system at step 0.

{goal, source_of_truth, generator_cmd, deliverables, allowed_paths, must_keep, acceptance, checks, voided_constraints,
 allow_remove, confirm}

* goal              the user's newest words (a continuation's 用户补充 / 【任务契约】 goal), not the old run's request
* source_of_truth   the script that generated the delivered artifact (provenance), else a source path the request /
                    thread names (task-830: ~/ai-workstation/news/aiw_news.py, never ~/aiw-news/<date>.html)
* deliverables      the artifacts that script makes: they are regenerated, never edited
* allowed_paths     where the fix may change files (the source and its folder)
* must_keep         what the previous deliverable had (fingerprint) + what the user says went missing
* acceptance/checks what proves the fix (verify_html clicks / selector counts), run through the verify tool
* voided            old constraints the new request overrides (不要修改脚本 …)
* confirm           large / ambiguous change (no source found, many files): ask the user first with a card

Pure; block() is the text put in front of the model every turn.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import fingerprint as _fp
from . import intent as _in
from .provenance import ADHOC, SOURCE_EXT, ProvenanceBook, norm

_SRC_PATH = re.compile(r"(?:~|\$HOME|/home/[\w.-]+|/data/home|/root|/opt|/srv)(?:/[\w.@+-]+)+\.(?:py|js|mjs|ts|tsx|jsx|vue|css|"
                       r"html?|j2|jinja2?|sh)\b")
_GOAL = re.compile(r"【接着上次继续做】\s*用户补充[:：]\s*(.+)$", re.S)
_CONTRACT_GOAL = re.compile(r"目标（本次请求）[:：]\s*(.+?)(?:\n|$)")
# "GitHub也没了" / "详情进不去" -> the thing the user says is broken or gone
_LOST = re.compile(r"([\w\u4e00-\u9fff]{1,12}?)(?:也|又|都|全)?(?:没了|不见了|丢了|消失了?|没有了)")
_CLICK = re.compile(r"(点击|点|click)[^，。；,;\n]{0,10}(进入不了|进不去|进不了|打不开|没反应|不了|无效)|(详情|弹窗|链接)[^，。；,;\n]{0,6}"
                    r"(进不去|进入不了|打不开|没反应|不了)", re.I)
_REMOVE = re.compile(r"(?:去掉|删掉|删除|移除|不要显示|隐藏|remove|drop)\s*(?:掉)?([\w\u4e00-\u9fff#.\-]{2,16})", re.I)
CONFIRM_FILES = int(os.environ.get("AIW_DEV_CONFIRM_FILES", "6") or 6)


def goal_of(prompt: str) -> str:
    p = prompt or ""
    m = _CONTRACT_GOAL.search(p)
    if m:
        return m.group(1).strip()[:600]
    m = _GOAL.search(p)
    if m:
        return m.group(1).strip()[:600]
    return re.sub(r"\s+", " ", p).strip()[:600]


def source_paths(text: str) -> list[str]:
    out = []
    for m in _SRC_PATH.finditer(text or ""):
        p = norm(m.group(0))
        if "/workspace/" in p and ADHOC.search(p):
            continue
        if p not in out:
            out.append(p)
    return out[:20]


def build(prompt: str, context: str = "", book: Optional[ProvenanceBook] = None, baselines: Optional[dict] = None,
          old_instructions: str = "") -> dict:
    book = book or ProvenanceBook()
    baselines = baselines or {}
    goal = goal_of(prompt)
    sot = book.source_of_truth()
    how = "provenance" if sot else ""
    mentioned = [p for p in source_paths(prompt + "\n" + (context or "")) if not re.search(r"\.html?$", p) or not book.get(p)]
    if not sot:
        gens = [p for p in mentioned if re.search(r"\.(py|js|mjs|ts|sh)$", p) and "/workspace/" not in p]
        if gens:
            sot, how = gens[0], "mentioned"
    deliverables = [p for p in book.artifacts() if sot and book.generator(p) == sot] or \
                   [p for p in book.artifacts() if book.generator(p)][-1:]
    gen_cmd = book.generator_command(deliverables[-1]) if deliverables else None
    allowed: list[str] = []
    if sot:
        allowed += [sot, os.path.dirname(sot)]
    for p in mentioned:
        if p not in allowed and SOURCE_EXT.search(p) and p not in deliverables and "/workspace/" not in p:
            allowed.append(p)
    must: list[str] = []
    checks = {"selectors": [], "min_counts": {}, "clicks": []}
    for d in deliverables:
        fp = baselines.get(d) or baselines.get(os.path.basename(d))
        if not fp:
            continue
        must += _fp.must_keep(fp)
        for k, v in sorted((fp.get("data_attrs") or {}).items(), key=lambda kv: -kv[1])[:8]:
            sel = f"[{k}]"
            checks["selectors"].append(sel)
            checks["min_counts"][sel] = max(1, int(v * 0.8))
        for s in (fp.get("section_ids") or [])[:10]:
            sel = "#" + s.split("#", 1)[1]
            checks["selectors"].append(sel)
            checks["min_counts"][sel] = 1
    lost = []
    for m in _LOST.finditer(_in.strip_negated(goal)):
        w = re.sub(r"^(而且|并且|然后|还有|另外|结果|现在|但是|可是)", "", m.group(1)).strip()
        if len(w) >= 2 and w not in lost:
            lost.append(w)
    must += [f"{w}（用户指出丢失，必须恢复并保留）" for w in lost]
    acceptance = []
    if _CLICK.search(goal):
        acceptance.append("点击列表项/卡片能进入详情（verify_html 的 clicks：点击后详情/弹窗可见且有内容）")
    for w in lost:
        acceptance.append(f"{w} 恢复显示（verify_html 的 selectors 计数 > 0）")
    if deliverables:
        acceptance.append("修改源头后重新运行生成命令，验证的是重新生成的产物")
    acceptance.append("与上一版交付物对比：之前有的功能/区块/交互没有丢失（系统自动对比特征指纹）")
    allow_remove = [m.group(1).lower() for m in _REMOVE.finditer(_in.strip_negated(goal))]
    voided = _in.voided_constraints(old_instructions or prompt.replace(goal, ""), _in.DEV_FIX)
    confirm = (not sot) or len(allowed) > CONFIRM_FILES
    return {"goal": goal, "source_of_truth": sot, "source_how": how, "generator_cmd": gen_cmd, "deliverables": deliverables,
            "allowed_paths": allowed[:20], "must_keep": must[:20], "acceptance": acceptance, "checks": checks,
            "voided_constraints": voided, "allow_remove": allow_remove, "confirm": confirm,
            "confirm_reason": ("没有找到生成交付物的源头脚本" if not sot else
                               f"涉及 {len(allowed)} 个文件/目录，改动面较大" if confirm else "")}


def block(c: Optional[dict]) -> str:
    if not c:
        return ""
    lines = ["\n\n【任务契约】(dev_fix — 系统根据对话和产物来源生成，每一步都要遵守)",
             f"目标：{c.get('goal')}"]
    if c.get("source_of_truth"):
        lines.append(f"源头（source of truth）：{c['source_of_truth']} —— 只改这里（及允许的路径），不要直接改生成的产物")
    else:
        lines.append("源头：未确定 —— 先找到生成交付物的脚本/源码再改，不要直接改产物")
    if c.get("generator_cmd"):
        lines.append(f"改完源头后必须重新生成：{c['generator_cmd'][:300]}")
    if c.get("deliverables"):
        lines.append("交付物（只能重新生成，禁止直接编辑）：" + "、".join(c["deliverables"][:6]))
    if c.get("allowed_paths"):
        lines.append("允许修改的路径：" + "、".join(c["allowed_paths"][:10]))
    if c.get("must_keep"):
        lines.append("必须保留：" + "；".join(c["must_keep"][:14]))
    if c.get("acceptance"):
        lines.append("验收（用 verify_html 取证，done 时写进总结）：\n" + "\n".join(f"  - {a}" for a in c["acceptance"]))
    if c.get("voided_constraints"):
        lines.append("已失效的旧约束（本次是修复代码，可以修改源头脚本）：" + "；".join(c["voided_constraints"]))
    if (c.get("git") or {}).get("branch"):
        g = c["git"]
        lines.append(f"Git：仓库 {g.get('repo')} 已记录 HEAD {str(g.get('base') or '')[:10]}，修改在分支 {g['branch']} 上，"
                     "done 通过后系统自动提交；不要自己 git reset/checkout/stash")
    lines.append("done 之前系统会检查：源头有真实改动、改动后重新生成过、验收和回归对比通过。")
    return "\n".join(lines)


def card(c: dict) -> tuple[str, list]:
    """The confirmation card for a large / ambiguous change."""
    q = ("这是一个修复代码的任务，开始前请确认任务契约：\n目标：" + str(c.get("goal") or "")[:200] +
         ("\n源头：" + c["source_of_truth"] if c.get("source_of_truth") else "\n源头：还没找到生成交付物的脚本") +
         ("\n允许修改：" + "、".join(c.get("allowed_paths") or [])[:200] if c.get("allowed_paths") else "") +
         (f"\n原因：{c.get('confirm_reason')}" if c.get("confirm_reason") else ""))
    return q, ["按这个契约开始", "我来补充源头/范围", "停止任务"]
