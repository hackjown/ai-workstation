"""v3.9.24 (2) automatic model routing (idea: a cheap "Jev"-style difficulty classifier before each run).

Before a task (and, if switched on, a chat turn) starts, the request is put in a tier — easy / normal / hard —
first by rules (the user's own regex rules, then built-in keyword + length scoring), and only when the rules are
unsure and 「用小模型判断」 is on, by one short call to the small model. Each tier names a model and a reasoning effort
(off = the model's normal setting / low / medium / high, sent as reasoning_effort) in 设置 › 模型路由. A model the user picked for the task (POST /api/tasks {model, effort})
always wins. A tier with no model keeps the normal main model (only the effort changes).

kv "autoroute": {enabled, use_model, apply_chat, tiers: {easy|normal|hard: {model, effort}}, rules: [{pattern, tier}]}
Env: AIW_AUTOROUTE=0 forces it off; =1 forces it on (settings still give the tiers).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger("aiw.autoroute")
KV = "autoroute"
TIERS = ("easy", "normal", "hard")
TIER_CN = {"easy": "简单", "normal": "普通", "hard": "困难"}
EFFORTS = ("off", "low", "medium", "high")
DEFAULT = {"enabled": False, "use_model": False, "apply_chat": False,
           "tiers": {"easy": {"model": "", "effort": "off"}, "normal": {"model": "", "effort": "off"},
                     "hard": {"model": "", "effort": "high"}},
           "rules": []}

HARD_RE = re.compile(r"开发|写(一个|个)?(程序|脚本|代码|插件|网站|应用|App|app)|重构|调试|debug|修(复)?\s*bug|报错|逆向|复刻|"
                     r"架构|设计方案|部署|上线|迁移|诊断|排查|深挖|分析报告|研究报告|论文|CAD|户型|平面图|渲染|视频|剪辑|"
                     r"批量|自动化|爬虫|数据分析|建模|多步|整个项目|全栈|refactor|implement|architecture", re.I)
EASY_RE = re.compile(r"^(帮我)?(查一?下|搜一?下|打开|看看|翻译|几点|天气|是什么|什么是|截图|截个图|告诉我|提醒我|关掉|关闭)|"
                     r"^(打开|搜索|搜|查)\S{0,20}$|^(hi|hello|你好|在吗)", re.I)


def cfg(store) -> dict:
    c = store.kv_get(KV, {}) or {}
    out = json.loads(json.dumps(DEFAULT))
    out.update({k: v for k, v in c.items() if k in DEFAULT and k != "tiers"})
    for t in TIERS:
        out["tiers"][t].update((c.get("tiers") or {}).get(t) or {})
        if out["tiers"][t].get("effort") not in EFFORTS:
            out["tiers"][t]["effort"] = "off"
    return out


def save(store, body: dict) -> dict:
    c = cfg(store)
    for k in ("enabled", "use_model", "apply_chat"):
        if k in body:
            c[k] = bool(body[k])
    for t in TIERS:
        tb = (body.get("tiers") or {}).get(t) or {}
        if "model" in tb:
            c["tiers"][t]["model"] = str(tb["model"] or "").strip()[:120]
        if tb.get("effort") in EFFORTS:
            c["tiers"][t]["effort"] = tb["effort"]
    if "rules" in body:
        rules = []
        for r in body.get("rules") or []:
            pat, tier = str((r or {}).get("pattern") or "").strip(), (r or {}).get("tier")
            if not pat or tier not in TIERS:
                continue
            try:
                re.compile(pat)
            except re.error as e:
                raise ValueError(f"规则「{pat}」不是有效的正则：{e}")
            rules.append({"pattern": pat[:200], "tier": tier})
        c["rules"] = rules[:50]
    store.kv_set(KV, c)
    return c


def on(store) -> bool:
    env = os.environ.get("AIW_AUTOROUTE")
    if env == "0":
        return False
    return env == "1" or bool(cfg(store).get("enabled"))


def by_rules(text: str, c: dict) -> tuple[Optional[str], str, bool]:
    """-> (tier, reason, sure). User rules first (always sure), then the built-in scoring."""
    t = (text or "").strip()
    for r in c.get("rules") or []:
        try:
            if re.search(r["pattern"], t, re.I):
                return r["tier"], f"命中你的规则 /{r['pattern']}/", True
        except re.error:
            continue
    score = 0
    hard = [m.group(0) for m in HARD_RE.finditer(t)]
    score += 2 * min(len(hard), 3)
    n = len(t)
    score += 2 if n > 400 else 1 if n > 150 else 0
    score += min(len(re.findall(r"(?m)^\s*(\d+[.、)]|[-*•])\s", t)), 3)   # numbered / bulleted requirements
    if EASY_RE.search(t) and not hard and n <= 60:
        return "easy", "短小的查询/打开类请求", True
    if score >= 4:
        return "hard", "开发/诊断/长需求类" + (f"（{'、'.join(dict.fromkeys(hard))[:40]}）" if hard else ""), True
    if score >= 2:
        return "hard" if hard else "normal", "中等偏难", False
    if n <= 25:
        return "easy", "很短的请求", False
    return "normal", "一般任务", score == 0 and n < 150


CLASSIFY_PROMPT = """把用户交给电脑 Agent 的请求按难度分级，只输出 JSON：{"tier":"easy|normal|hard","why":"不超过15个字"}。
easy：一两步就能完成（打开、查一下、简单问答）。normal：几步到十几步的常规操作。hard：写代码/开发、诊断排查、多步长流程、设计制图、需要深入推理。"""


async def by_model(llm, text: str) -> Optional[tuple[str, str]]:
    try:
        from agent_runtime import prompts as _p
        out = await asyncio.wait_for(llm.complete(_p.get("route_classifier", CLASSIFY_PROMPT), f"请求：{(text or '')[:1500]}", 80), 15)
        m = re.search(r"\{.*\}", out or "", re.S)
        d = json.loads(m.group(0)) if m else {}
        if d.get("tier") in TIERS:
            return d["tier"], "小模型判断：" + str(d.get("why") or "")[:30]
    except Exception as e:  # noqa: BLE001
        log.info("route classifier: %s", e)
    return None


async def decide(store, text: str, small_llm=None, manual_model: str = "", manual_effort: str = "",
                 for_chat: bool = False) -> Optional[dict]:
    """The route for one request, or None to keep today's behaviour (routing off, or nothing to change)."""
    if manual_model or manual_effort:
        return {"tier": "manual", "tier_cn": "手动", "model": manual_model or "", "effort": manual_effort or "",
                "by": "manual", "reason": "你为这个任务指定了模型"}
    if not on(store):
        return None
    c = cfg(store)
    if for_chat and not c.get("apply_chat"):
        return None
    tier, why, sure = by_rules(text, c)
    by = "rule"
    if not sure and c.get("use_model") and small_llm is not None:
        got = await by_model(small_llm, text)
        if got:
            tier, why = got
            by = "model"
    t = c["tiers"].get(tier or "normal") or {}
    return {"tier": tier, "tier_cn": TIER_CN.get(tier, tier), "model": t.get("model") or "",
            "effort": t.get("effort") or "off", "by": by, "reason": why}


def label(route: dict, main_model: str = "") -> str:
    m = route.get("model") or main_model or "主模型"
    eff = route.get("effort") or "off"
    eff_cn = {"off": "默认", "": "默认", "low": "低", "medium": "中", "high": "高"}.get(eff, eff)
    return f"模型路由：{route.get('tier_cn')} → {m} · 推理强度 {eff_cn}（{route.get('reason')}）"


try:   # 设置 › 提示词
    from agent_runtime import prompts as _prompts_reg
    _prompts_reg.register("route_classifier", "模型路由分级", CLASSIFY_PROMPT, "规则拿不准时，小模型判断任务难度")
except Exception:  # noqa: BLE001
    pass
