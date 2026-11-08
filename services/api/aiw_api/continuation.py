"""Continuing a task (3.8.5): 继续 / 接着做 / 我已经上传了 / 改一下 / 修改任务 resume the SAME task.

Root cause of the failure this fixes (task-7bd096288c): the user's PPT-explainer task stopped; "继续" and then "我已经
上传了" in the chat home were routed like any new message, so each became a brand-new task in a brand-new thread
(chat.start_task passed thread_id=None), with no link to the stopped run, no plan, no list of what was done, and the
PDF only as a path on whichever computer the chat had picked. 重试 re-sent the bare prompt and started from zero.

Now a continuation is a new run row in the root task's thread with ``tasks.continues = <root id>``:
* ``chain(store, id)``     every run of the task, root first
* ``resume_block(...)``    what the next run needs: the original (or edited) instructions, later additions, the plan
                           with ticks, the steps done (thought · tool · ok/error), files already made, the question that
                           was left unanswered, the computer and folder it ran in
* ``run_prompt(...)``      the prompt the agent loop sees: the task's instructions + "接着上次" + the new words, so
                           skills (【技能】), the video/CAD/image budgets and the planner still see the real request
* ``FOLLOWUP_RE``          short chat messages that refer back to an earlier task
"""
from __future__ import annotations

import json
import re
from typing import Optional

FINAL = ("succeeded", "failed", "stopped", "interrupted", "cancelled")
UNFINISHED = ("failed", "stopped", "interrupted")
LIVE = ("pending", "running", "paused", "takeover", "waiting_confirm", "waiting_input")

# a message that continues earlier work instead of asking for something new
FOLLOWUP_RE = re.compile(
    r"^\s*(?:好[的了吧]?[，,。！!\s]*)?(?:那)?(?:请)?(?:你)?(?:继续|接着|接下去|往下|再来|重试|再试|重新(?:做|来|试)|"
    r"我?(?:已经|刚刚?|刚才)?(?:上传|传|发|放)(?:了|好了|上去了|过来了|给你了)|文件(?:已经)?(?:上传|传|发)(?:了|好了)|"
    r"(?:附件|文件|ppt|pdf|PPT|PDF)(?:在|发)(?:这|了)|改(?:一下|改|成|为)|修改|再改|调整一下|按(?:刚才|上次|之前)|"
    r"(?:刚才|上次|之前)(?:那个|的)|还是(?:那个|这个)|搞定了|弄好了|好了[，,。！!]?$|可以了[，,。！!]?$|"
    r"(?:用|就用|选)(?:第?[一二三四五1-5]|这个|那个))", re.I)
EDIT_RE = re.compile(r"(改一下|改改|改成|改为|修改|再改|调整|换成|加上|加一|去掉|删掉|不要)")
NEW_TASK_RE = re.compile(r"(新任务|另外一个|另一件事|换个任务|新的任务|重新开一个)")



# 3.9.9 (task-0db): "继续把客厅和厨房的文字加上去" matched the old r"继续[^\n]{0,20}" and the user's words were dropped
# from the run's prompt. Only a bare "go on" (继续 / 继续做 / 接着做 / continue, with punctuation) carries no new goal.
_BARE_CONTINUE = re.compile(r"[（(\s]*(继续|继续做|继续吧|接着做|接着来|继续执行|go on|continue|resume)[\s。.!！~～)）吧啊呀]*", re.I)


def is_bare_continue(text: str) -> bool:
    return bool(_BARE_CONTINUE.fullmatch((text or "").strip()))

def is_followup(text: str, has_attachments: bool = False) -> bool:
    t = (text or "").strip()
    if not t:
        return has_attachments
    if NEW_TASK_RE.search(t):
        return False
    if FOLLOWUP_RE.search(t) and len(t) <= 120:
        return True
    return has_attachments and len(t) <= 24


def root_id(store, task_id: str) -> str:
    t = store.task(task_id) or {}
    return t.get("continues") or task_id


def chain(store, task_id: str) -> list[dict]:
    return store.continuations(root_id(store, task_id))


def latest(store, task_id: str) -> Optional[dict]:
    runs = chain(store, task_id)
    return runs[-1] if runs else None


def instructions(store, root: dict) -> str:
    """The task's instructions: the root prompt, or what the user last changed them to (修改任务)."""
    return str(store.kv_get(f"task_instructions:{root['id']}") or root.get("prompt") or "")


def _short(v, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def steps_done(store, runs: list[dict], limit: int = 30) -> list[str]:
    """One line per step of the earlier runs: thought · tool(args) → ok / error."""
    lines: list[str] = []
    for run in runs:
        sid = run.get("session_id")
        if not sid:
            continue
        acts: dict = {}
        for e in store.events(sid, limit=2000):
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            typ, st = e.get("type"), e.get("step")
            if typ == "agent.decision" and d.get("tool"):
                acts[st] = {"thought": d.get("thought") or "", "tool": d.get("tool"), "args": d.get("args") or {}}
            elif typ == "agent.action_result" and st in acts:
                res = d.get("result") if isinstance(d.get("result"), dict) else {}
                err = d.get("error") or (res.get("stderr") if not d.get("success") else "")
                acts[st]["ok"] = bool(d.get("success"))
                acts[st]["err"] = err or ""
                if d.get("tool") == "ask_user":
                    acts[st]["answer"] = (res or {}).get("answer")
        for st in sorted(k for k in acts if isinstance(k, int)):
            a = acts[st]
            if a["tool"] in ("screenshot", "wait"):
                continue
            args = {k: v for k, v in a["args"].items() if k not in ("content",)}
            out = "ok" if a.get("ok") else ("出错：" + _short(a.get("err"), 120) if "ok" in a else "没有结果")
            if a["tool"] == "ask_user":
                out = f"用户回答：{a.get('answer')!r}" if a.get("answer") else "用户还没回答"
            lines.append(f"{len(lines) + 1}. {_short(a['thought'], 110)} — {a['tool']}({_short(args, 140)}) → {out}")
    return lines[-limit:]


def files_made(store, runs: list[dict]) -> list[dict]:
    """Files the earlier runs sent to the user: server copy + where they were on that computer."""
    out, seen = [], set()
    for run in runs:
        sid = run.get("session_id")
        if not sid:
            continue
        vm = {}
        for e in store.events(sid, limit=2000):
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            if e.get("type") == "agent.action_result" and d.get("tool") == "send_file" and isinstance(d.get("result"), dict):
                p = str(d["result"].get("path") or "")
                if p:
                    vm[p.rsplit("/", 1)[-1]] = p
        for a in store.artifacts(sid):
            if a.get("kind") != "file":
                continue
            name = str(a["path"]).rsplit("/", 1)[-1]
            if name in seen:
                continue
            seen.add(name)
            p = vm.get(name, "")
            rel = p.split("/workspace/", 1)[1] if "/workspace/" in p else name
            out.append({"name": name, "local": a["path"], "rel": rel, "vm_path": p, "machine_id": run.get("machine_id")})
    return out


def open_question(store, runs: list[dict]) -> Optional[str]:
    """The last question the agent asked that got no answer (the user's new words may be that answer)."""
    for run in reversed(runs):
        sid = run.get("session_id")
        if not sid:
            continue
        q = None
        for e in store.events(sid, limit=2000):
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            if e.get("type") == "agent.input_required":
                q = (d.get("question"), d.get("options") or [])
            elif e.get("type") == "agent.input_answered":
                q = None if d.get("answer") else q
        if q:
            return q[0] + (f"（选项：{' / '.join(map(str, q[1]))}）" if q[1] else "")
        break
    return None


def last_plan(runs: list[dict]) -> Optional[dict]:
    for run in reversed(runs):
        p = run.get("plan")
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except ValueError:
                p = None
        if isinstance(p, dict) and p.get("steps"):
            return {"steps": list(p["steps"]), "done": [int(i) for i in p.get("done") or [] if str(i).isdigit()]}
    return None


def resume_block(store, row: dict, machine_name: Optional[str] = None) -> str:
    """Context for a continuation run: everything the earlier runs knew (prompt, plan, steps, files, question)."""
    root_ = store.task(row.get("continues") or row["id"]) or row
    runs = [r for r in store.continuations(root_["id"]) if r["id"] != row["id"] and r.get("created_at", 0) <= row.get("created_at", 1e18)]
    if not runs:
        return ""
    mode = str(store.kv_get(f"task_run:{row['id']}", {}).get("mode") or row.get("source") or "continue")
    prev = runs[-1]
    instr = instructions(store, root_)
    parts = ["CONTINUING AN EARLIER TASK — this is the SAME task, not a new one. Pick up exactly where it stopped: "
             "keep what is already done, do not start over, do not redo finished steps, do not ask for files that are "
             "listed below."]
    if mode == "edit":
        parts.append("THE USER CHANGED THE INSTRUCTIONS. Follow the new instructions; keep the finished work that still "
                     "fits them and redo only what the change touches.\nNEW INSTRUCTIONS: " + instr)
        if instr != (root_.get("prompt") or ""):
            parts.append("ORIGINAL INSTRUCTIONS (replaced): " + _short(root_.get("prompt"), 1200))
    else:
        parts.append("ORIGINAL REQUEST: " + _short(instr, 1500))
    adds = [r.get("prompt") for r in runs[1:] if (r.get("prompt") or "").strip() and r.get("source") != "edit"]
    if adds:
        parts.append("LATER WORDS FROM THE USER (oldest first):\n" + "\n".join("- " + _short(a, 300) for a in adds[-6:]))
    plan = last_plan(runs)
    if plan:
        done = set(plan["done"])
        parts.append("PLAN SO FAR ([x] = done):\n" + "\n".join(
            f"[{'x' if i in done else ' '}] {i}. {s}" for i, s in enumerate(plan["steps"])))
    new_words = (row.get("prompt") or "").strip()
    if mode != "edit" and new_words and not is_bare_continue(new_words):
        parts.append("THIS RUN'S GOAL (aiw-fix-1): " + _short(new_words, 600) + "\nThe ORIGINAL REQUEST was handled by the "
                     "earlier runs: do not clone, install or set up again what STEPS ALREADY DONE / the files in "
                     "~/workspace show is there (check with one ls), and do not tick plan items you did not do in this run.")
    st = prev.get("status")
    res = _short(prev.get("result") or "", 600)
    parts.append(f"LAST RUN ENDED: {st}" + (f" — {res}" if res else ""))
    steps = steps_done(store, runs)
    if steps:
        parts.append("STEPS ALREADY DONE (oldest first):\n" + "\n".join(steps))
    q = open_question(store, runs)
    if q:
        parts.append("THE LAST RUN ASKED THE USER AND GOT NO ANSWER: " + q + "\nIf the user's new message answers it, "
                     "go on with that answer; if not, ask again. Never pick a default yourself.")
    mid = prev.get("machine_id")
    if mid and mid != row.get("machine_id"):
        parts.append(f"COMPUTER: the earlier runs ran on {mid}; this run is on {machine_name or row.get('machine_id')}. "
                     "Attachments and the files sent to the user were copied here (listed below); scratch files that "
                     "were never sent stay on the other computer: rebuild them if you need them.")
    else:
        parts.append(f"COMPUTER: the same one as before ({machine_name or mid}); its ~/workspace still holds the "
                     "earlier runs' files. Look there first (ls -t ~/workspace | head).")
    return "\n\n".join(parts)


def run_prompt(store, row: dict, signals: Optional[dict] = None) -> str:
    """The prompt the agent loop gets for a continuation run (the task's instructions + the user's new words).
    v3.9.26 (task-830): when the new words change the KIND of job (run -> dev_fix: "…进入不了详情，GitHub也没了，修复"
    after "运行 aiw_news.py，不要修改脚本"), the old instructions are no longer the goal: a 【任务契约】 section makes the new
    request the goal, keeps the old instructions as background only and lists the old constraints that conflict with it
    as 已失效. Same-kind continuations keep the plain concatenation."""
    root_ = store.task(row.get("continues") or row["id"]) or row
    if root_["id"] == row["id"]:
        return row.get("prompt") or ""
    instr = instructions(store, root_)
    mode = str(store.kv_get(f"task_run:{row['id']}", {}).get("mode") or row.get("source") or "continue")
    new = (row.get("prompt") or "").strip()
    if mode == "edit":
        return instr
    if not new or is_bare_continue(new):
        return f"{instr}\n\n【接着上次继续做】"
    try:
        from agent_runtime import intent as _in
        from . import devflow as _dfa
        if signals is None:
            signals = _dfa.thread_signals(store, row.get("thread_id"), exclude={row["id"]})
        old_kind = _dfa.root_intent(store, root_, signals)
        new_kind = _in.classify(new, signals).get("intent")
    except Exception:  # noqa: BLE001  a broken classifier never blocks the run
        old_kind = new_kind = None
    if new_kind and old_kind and new_kind != old_kind and new_kind == "dev_fix":
        return contract_prompt(instr, new, _in.voided_constraints(instr, new_kind))
    return f"{instr}\n\n【接着上次继续做】用户补充：{new}"


def parent_contract(store, row: dict) -> dict:
    """v3.9.29 (task-68b): the newest dev contract of an earlier run of this task (its source of truth, generator
    command, deliverables, git/snapshot state), so a 继续 run does not lose the source the previous run found. {} if none."""
    root = row.get("continues") or row.get("id")
    if not root:
        return {}
    try:
        runs = [r for r in (store.continuations(root) or []) if r.get("id") != row.get("id")]
    except Exception:  # noqa: BLE001
        runs = []
    if not runs and root != row.get("id"):
        runs = [store.task(root) or {}]
    for r in reversed(runs):
        c = store.kv_get(f"task_contract:{r.get('id')}") if r.get("id") else None
        if isinstance(c, dict) and c.get("source_of_truth"):
            keep = ("source_of_truth", "source_how", "generator_cmd", "generator_cmd_fast", "deliverables", "allowed_paths",
                    "req_checks", "git")
            out = {k: c.get(k) for k in keep if c.get(k)}
            out["from_task"] = r.get("id")
            return out
    return {}


def contract_prompt(old_instructions: str, new: str, voided: list) -> str:
    """v3.9.26: the continuation prompt when the job changed kind (see run_prompt)."""
    parts = ["【任务契约】这次是一个新的目标（修复/修改代码），以下面的「目标」为准。",
             f"目标（本次请求）：{new}",
             "背景（原任务说明，仅作参考，不是本次目标）：\n" + _short(old_instructions, 1500)]
    if voided:
        parts.append("已失效的旧约束（与本次修复冲突，不再适用；本次可以修改源头脚本/代码）：\n" +
                     "\n".join(f"- {v}（已失效）" for v in voided))
    parts.append("要求：找到生成交付物的源头脚本，在源头修复，重新生成后验证；不要直接修改生成出来的文件。")
    return "\n\n".join(parts) + f"\n\n【接着上次继续做】用户补充：{new}"
