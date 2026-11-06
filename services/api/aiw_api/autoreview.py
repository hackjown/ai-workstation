"""v3.9.24 (3) automatic review of actions that would pop an approval card.

Order for one action (only actions the policy already sends to the user ever get here):
  1. the user's DENY rules  -> denied (always wins, even over 本任务内都允许 / allow rules / the model)
  2. payments, sending messages/posts, order_confirm, xhs_review -> never auto-allowed: the normal card
  3. the user's ALLOW rules -> allowed
  4. 「用模型审核」 on: the review model answers allow / ask / deny with a reason
  5. anything else (or any error / timeout) -> the normal approval card, unchanged
Every verdict is logged (table review_log) and shown on the task timeline.

Rule syntax, one per line, matched case-insensitively against "<tool> <what>" (what = command / url / path / text):
  shell:git status*          tool prefix + glob
  *rm -rf /*                 glob over everything
  re:^shell .*docker (ps|logs)   a regular expression
kv "autoreview": {enabled, use_model, model, model_can_deny, allow: [...], deny: [...]}
Env: AIW_AUTOREVIEW=0 forces off.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

log = logging.getLogger("aiw.autoreview")
KV = "autoreview"
NEVER_TOOLS = ("order_confirm", "xhs_review")
NEVER_CATEGORIES = ("pay", "message")
DEFAULT = {"enabled": False, "use_model": False, "model": "", "model_can_deny": True, "allow": [], "deny": []}
SCHEMA = """CREATE TABLE IF NOT EXISTS review_log (id TEXT PRIMARY KEY, ts REAL, task_id TEXT, tool TEXT, subject TEXT,
  verdict TEXT, by TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_review_ts ON review_log(ts);"""


def ensure_table(store) -> None:
    with store.lock:
        store.db.executescript(SCHEMA)
        store.db.commit()


def cfg(store) -> dict:
    c = {**DEFAULT, **(store.kv_get(KV, {}) or {})}
    c["allow"] = [str(x) for x in c.get("allow") or [] if str(x).strip()]
    c["deny"] = [str(x) for x in c.get("deny") or [] if str(x).strip()]
    return c


def _lines(v) -> list[str]:
    if isinstance(v, str):
        v = v.splitlines()
    return [str(x).strip()[:300] for x in (v or []) if str(x).strip() and not str(x).strip().startswith("#")][:200]


def save(store, body: dict) -> dict:
    c = cfg(store)
    for k in ("enabled", "use_model", "model_can_deny"):
        if k in body:
            c[k] = bool(body[k])
    if "model" in body:
        c["model"] = str(body["model"] or "").strip()[:120]
    for k in ("allow", "deny"):
        if k in body:
            rules = _lines(body[k])
            for r in rules:
                if r.startswith("re:"):
                    try:
                        re.compile(r[3:])
                    except re.error as e:
                        raise ValueError(f"规则「{r}」不是有效的正则：{e}")
            c[k] = rules
    store.kv_set(KV, c)
    return c


def on(store) -> bool:
    return os.environ.get("AIW_AUTOREVIEW", "") != "0" and bool(cfg(store).get("enabled"))


def subject(tool: str, payload) -> str:
    p = payload if isinstance(payload, dict) else {}
    what = ""
    for k in ("command", "cmd", "url", "path", "text", "commands", "script", "query", "args"):
        if p.get(k):
            v = p[k]
            what = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            break
    if not what and p:
        what = json.dumps({k: v for k, v in p.items() if k not in ("confirm_timeout_s",)}, ensure_ascii=False)
    return f"{tool} {what}".strip()[:2000]


def match(rule: str, tool: str, subj: str) -> bool:
    rule = rule.strip()
    if not rule:
        return False
    s = subj.lower()
    if rule.startswith("re:"):
        try:
            return re.search(rule[3:], subj, re.I) is not None
        except re.error:
            return False
    m = re.match(r"^([A-Za-z0-9_.-]+):(.*)$", rule)
    if m and not rule.startswith(("http:", "https:")):
        if m.group(1).lower() != (tool or "").lower():
            return False
        rest = s[len(tool):].strip()
        return fnmatch.fnmatchcase(rest, m.group(2).strip().lower() or "*")
    if any(ch in rule for ch in "*?["):
        return fnmatch.fnmatchcase(s, rule.lower())
    return rule.lower() in s   # no wildcard: a plain substring


REVIEW_PROMPT = """你是电脑 Agent 的安全审核员。Agent 想执行下面这个需要授权的操作。根据用户的任务判断：
allow = 明显在任务范围内、可逆、不会泄露隐私或花钱；deny = 明显危险、越权、与任务无关或不可逆的破坏；ask = 拿不准就交给用户。
只输出 JSON：{"verdict":"allow|ask|deny","reason":"不超过40个字的中文理由"}"""


async def model_verdict(llm, task_prompt: str, tool: str, subj: str, summary: dict) -> Optional[dict]:
    user = (f"用户的任务：{(task_prompt or '')[:1200]}\n\n操作：{subj[:1500]}\n"
            f"风险说明：{(summary or {}).get('risk_cn') or ''} {(summary or {}).get('what') or ''}")
    try:
        from agent_runtime import prompts as _p
        out = await asyncio.wait_for(llm.complete(_p.get("review", REVIEW_PROMPT), user, 160), float(os.environ.get("AIW_AUTOREVIEW_TIMEOUT", "20")))
        m = re.search(r"\{.*\}", out or "", re.S)
        d = json.loads(m.group(0)) if m else {}
        v = str(d.get("verdict") or "").lower()
        if v in ("allow", "ask", "deny"):
            return {"verdict": v, "reason": str(d.get("reason") or "")[:120]}
    except Exception as e:  # noqa: BLE001
        log.info("review model: %s", e)
    return None


class Reviewer:
    def __init__(self, store, llm_factory=None):
        self.store = store
        self.llm_factory = llm_factory   # (model_name or "") -> LLMRouter
        ensure_table(store)

    def log(self, task_id: str, tool: str, subj: str, verdict: str, by: str, reason: str) -> None:
        try:   # never keep a password / token from a command line in the log
            from agent_runtime import redact as _rd
            subj = _rd.mask(subj)
        except Exception:  # noqa: BLE001
            pass
        self.store._x("INSERT INTO review_log VALUES (?,?,?,?,?,?,?,?)",
                      (uuid.uuid4().hex[:16], time.time(), task_id or "", tool or "", subj[:600], verdict, by, reason[:300]))

    async def decide(self, tool: str, payload, summary: Optional[dict] = None, task_prompt: str = "",
                     task_id: str = "", dry: bool = False) -> dict:
        """-> {verdict: allow|deny|ask, by: rule|model|policy|off, reason}. `ask` = show the normal card."""
        if not on(self.store) and not dry:
            return {"verdict": "ask", "by": "off", "reason": ""}
        c = cfg(self.store)
        subj = subject(tool, payload)
        res = None
        for r in c["deny"]:
            if match(r, tool, subj):
                res = {"verdict": "deny", "by": "rule", "reason": f"命中拒绝规则：{r}"}
                break
        cat = (summary or {}).get("category")
        if res is None and (tool in NEVER_TOOLS or cat in NEVER_CATEGORIES):
            res = {"verdict": "ask", "by": "policy", "reason": "付款、发消息/发布、下单始终由你确认"}
        if res is None:
            for r in c["allow"]:
                if match(r, tool, subj):
                    res = {"verdict": "allow", "by": "rule", "reason": f"命中允许规则：{r}"}
                    break
        if res is None and c.get("use_model") and self.llm_factory is not None:
            try:
                llm = self.llm_factory(c.get("model") or "")
            except Exception as e:  # noqa: BLE001
                llm = None
                log.info("review llm: %s", e)
            v = await model_verdict(llm, task_prompt, tool, subj, summary or {}) if llm is not None else None
            if v:
                if v["verdict"] == "deny" and not c.get("model_can_deny"):
                    v["verdict"] = "ask"
                res = {"verdict": v["verdict"], "by": "model", "reason": v["reason"] or "模型判断"}
        if res is None:
            res = {"verdict": "ask", "by": "none", "reason": "没有命中规则"}
        if not dry:
            try:
                self.log(task_id, tool, subj, res["verdict"], res["by"], res["reason"])
            except Exception as e:  # noqa: BLE001
                log.warning("review log: %s", e)
        return {**res, "subject": subj}

    def recent(self, limit: int = 100) -> list[dict]:
        return self.store._x("SELECT * FROM review_log ORDER BY ts DESC LIMIT ?", (max(1, min(int(limit), 500)),))


def mount(app, reviewer: Reviewer, auth) -> None:
    from fastapi import Body, Depends, HTTPException

    @app.get("/api/autoreview", dependencies=[Depends(auth)])
    async def ar_get():
        return {**cfg(reviewer.store), "env_off": os.environ.get("AIW_AUTOREVIEW", "") == "0"}

    @app.put("/api/autoreview", dependencies=[Depends(auth)])
    async def ar_put(body: dict = Body(...)):
        try:
            return save(reviewer.store, body or {})
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/autoreview/log", dependencies=[Depends(auth)])
    async def ar_log(limit: int = 100):
        return reviewer.recent(limit)

    @app.post("/api/autoreview/test", dependencies=[Depends(auth)])
    async def ar_test(body: dict = Body(...)):
        """Dry run: what would happen to {tool, payload} (nothing is logged)."""
        b = body or {}
        return await reviewer.decide(str(b.get("tool") or "shell"), b.get("payload") or {"command": b.get("command") or ""},
                                     {"category": b.get("category")}, str(b.get("task") or ""), dry=True)


try:   # 设置 › 提示词
    from agent_runtime import prompts as _prompts_reg
    _prompts_reg.register("review", "自动审核", REVIEW_PROMPT, "需要授权的操作交给模型判断 allow / ask / deny")
except Exception:  # noqa: BLE001
    pass
