"""v3.9.54 antigravity tool: AIW hands a coding job to Google's Antigravity CLI (`agy`) and supervises it.

AIW stays the manager: it writes the brief (task + acceptance items from the 【任务契约】), Antigravity writes the code
in its own git worktree on a fresh branch (`aiw/<job>`), and AIW verifies the result with its own commands before
anything reaches the user. Nothing is pushed or merged by Antigravity (`git push` is in the deny list it gets).

The bridge (agy_bridge.py, stdlib only) is written to the computer on first use like the Photopea bridge, so a
computer only needs `agy` installed and signed in once. Ops: check / start / wait / send / diff / stop / list.

Env:
  AIW_AGY                  0 = tool hidden; 1 = always offered on coding tasks; auto (default) = offered when the
                           request mentions Antigravity / agy
  AIW_AGY_MODEL, AIW_AGY_EFFORT, AIW_AGY_MODE (default accept-edits)
  AIW_AGY_TIMEOUT_MIN (120), AIW_AGY_IDLE_MIN (15), AIW_AGY_MAX_TURNS (8)
  AIW_AGY_SANDBOX=1        run agy with --sandbox
  AIW_AGY_REMOTE_CONTROL=1 start sessions with --remote-control (watch / take over from a browser)
  AIW_AGY_SKIP_PERMISSIONS=1  --dangerously-skip-permissions (only on a throwaway computer)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
from pathlib import Path

BRIDGE_SRC = Path(__file__).with_name("agy_bridge.py")
REMOTE = "$HOME/.config/aiw/agy/agy_bridge.py"
OPS = ("check", "start", "wait", "send", "diff", "stop", "list")
WANT = re.compile(r"antigravity|反重力|\bagy\b|gemini\s*(?:4|cli)|谷歌.{0,4}(?:编程|写代码|agent)", re.I)

# what agy may do without asking (settings.json permissions) lives in the bridge (v3.9.56: written by
# `agy_bridge.py settings`, only valid actions read_file / write_file / command); anything else is soft-denied in
# headless mode and shows up in the job's `denied` list (which step, on what, why)
from . import agy_bridge as _ab  # noqa: E402
DEFAULT_ALLOW, DEFAULT_DENY = _ab.DEFAULT_ALLOW, _ab.DEFAULT_DENY


def mode() -> str:
    return (os.environ.get("AIW_AGY", "auto") or "auto").strip().lower()


def offered(prompt: str, dev_task: bool) -> bool:
    """Show the tool? Never when AIW_AGY=0; on coding tasks when =1; otherwise only when the user names it."""
    m = mode()
    if m in ("0", "off", "false", "no"):
        return False
    if WANT.search(prompt or ""):
        return True
    return dev_task and m in ("1", "on", "true", "yes", "always")


def install_command() -> str:
    """Write the bridge, then let it write the default permission rules (only when agy has none, or AIW manages the
    file) in one shell call. v3.9.56: the command never names agy's settings path, so the policy rule that stops the
    agent from editing agy's settings does not catch AIW's own install."""
    src = BRIDGE_SRC.read_bytes()
    b64 = base64.b64encode(src).decode()
    digest = hashlib.sha1(src).hexdigest()
    return (f'mkdir -p "$HOME/.config/aiw/agy" && f="{REMOTE}" && '
            f'if [ "$(sha1sum "$f" 2>/dev/null | cut -d" " -f1)" != "{digest}" ]; then '
            f"echo '{b64}' | base64 -d > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"; fi && "
            f"python3 \"$f\" settings >/dev/null 2>&1; "
            f"[ \"$(sha1sum \"$f\" | cut -d' ' -f1)\" = \"{digest}\" ] && echo AGY_BRIDGE_OK")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def brief(task: str, contract: dict | None = None, extra: str = "") -> str:
    """The prompt agy gets: the task, the acceptance items AIW will check, and the house rules."""
    parts = [task.strip()]
    c = contract or {}
    acc = [str(x) for x in (c.get("acceptance") or c.get("acceptance_items") or []) if str(x).strip()]
    if acc:
        parts.append("Acceptance (AIW will verify each of these itself after you finish):\n"
                     + "\n".join(f"- {x}" for x in acc[:20]))
    if c.get("source_of_truth"):
        parts.append(f"Source of truth: {c['source_of_truth']} (edit it, never a generated output).")
    if c.get("generator_cmd"):
        parts.append(f"Generator (re-run it after editing the source): {c['generator_cmd']}")
    if c.get("must_keep"):
        parts.append("Must keep working: " + "; ".join(str(x) for x in c["must_keep"][:12]))
    if c.get("allowed_paths"):
        parts.append("Only change files under: " + ", ".join(str(x) for x in c["allowed_paths"][:10]))
    if extra.strip():
        parts.append(extra.strip())
    parts.append("Rules: work only inside this git worktree (your current directory); use paths relative to it, and "
                 "run each shell command on its own (no `cd x && ...` chains). Commit your finished work on the current branch with a "
                 "clear message. Never push, never rebase or reset other branches. Run the relevant tests before "
                 "you finish. End with a short report: files changed, how you verified, anything left undone.")
    return "\n\n".join(parts)


def job_from_args(a: dict, prompt: str) -> dict:
    j = {"prompt": prompt, "repo": str(a.get("repo") or "~/workspace"), "base": str(a.get("base") or "HEAD"),
         "model": a.get("model") or os.environ.get("AIW_AGY_MODEL") or "",
         "effort": a.get("effort") or os.environ.get("AIW_AGY_EFFORT") or "",
         "mode": a.get("mode") or os.environ.get("AIW_AGY_MODE") or "accept-edits",
         "agent": a.get("agent") or "",
         "conversation": a.get("conversation") or "",
         "timeout_min": _env_int("AIW_AGY_TIMEOUT_MIN", int(a.get("timeout_min") or 120), 5, 600),
         "idle_min": _env_int("AIW_AGY_IDLE_MIN", 15, 2, 120),
         "max_turns": _env_int("AIW_AGY_MAX_TURNS", 8, 1, 50),
         "sandbox": os.environ.get("AIW_AGY_SANDBOX") == "1",
         "remote_control": os.environ.get("AIW_AGY_REMOTE_CONTROL") == "1",
         "skip_permissions": os.environ.get("AIW_AGY_SKIP_PERMISSIONS") == "1"}
    if j["mode"] not in ("accept-edits", "auto", "plan", "default", ""):
        j["mode"] = "accept-edits"
    if j["mode"] == "default":
        j["mode"] = ""
    return j


def command(op: str, a: dict, job: dict | None = None) -> tuple[str, int]:
    """(shell command, timeout seconds) for one op."""
    py = f'python3 "{REMOTE}"'
    jid = shlex.quote(str(a.get("job") or ""))
    if op == "check":
        return f"{py} check" + (" --probe" if a.get("probe") else ""), 150
    if op == "start":
        b = base64.b64encode(json.dumps(job or {}, ensure_ascii=False).encode()).decode()
        return f"{py} start --job-b64 {b}", 180
    if op == "wait":
        secs = max(10, min(int(a.get("seconds") or 600), 1500))
        return f"{py} wait {jid} --seconds {secs} --cursor {int(a.get('cursor') or 0)}", secs + 60
    if op == "send":
        b = base64.b64encode(str(a.get("message") or "").encode()).decode()
        return f"{py} send {jid} --text-b64 {b}", 30
    if op == "diff":
        return f"{py} diff {jid}" + (" --full" if a.get("full") else ""), 150
    if op == "stop":
        return f"{py} stop {jid}", 60
    return f"{py} list", 30


def parse(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("AGYRESULT "):
            try:
                return json.loads(line[10:])
            except ValueError:
                return None
    return None


def stop_command(job: str) -> str:
    return f'python3 "{REMOTE}" stop {shlex.quote(job)} >/dev/null 2>&1 || true'


def render(op: str, r: dict) -> str:
    """The history note the model reads (and the user sees in the step card)."""
    if not r.get("ok", True) and r.get("installed") is False:
        return ("FAILED: Antigravity CLI (agy) is not installed on this computer. Ask the user (ask_user) to install it "
                "from https://antigravity.google/download and sign in once by running `agy` in a terminal on the "
                "computer (or set GEMINI_API_KEY with modelProvider gemini). On a computer in mainland China it also "
                "needs HTTPS_PROXY.")
    if op == "check":
        s = f"agy {r.get('version') or ''} at {r.get('path')}; git={'yes' if r.get('git') else 'NO'}; " \
            f"proxy={'set' if r.get('proxy') else 'not set'}"
        se = r.get("settings") or {}
        if se.get("ignored_rules"):
            s += "; agy IGNORES these permission rules (unknown action): " + " | ".join(se["ignored_rules"][:6])
        if se.get("skip_all"):
            s += "; WARNING: settings auto-approve everything"
        if "signed_in" in r:
            s += "; signed in" if r.get("signed_in") else f"; NOT signed in: {r.get('error')}"
        return s
    if op == "list":
        rows = r.get("jobs") or []
        return "agy jobs: " + ("; ".join(f"{x['job']} {x['status']} turn {x.get('turn')} {x.get('branch')}"
                                         for x in rows) or "none")
    if not r.get("ok", True):
        return f"FAILED: {r.get('error')}"
    if op == "send":
        return f"queued for job {r.get('job')}. Call antigravity op=wait job={r.get('job')} next."
    if op == "diff":
        lines = [f"job {r.get('job')} branch {r.get('branch')} worktree {r.get('worktree')} vs {r.get('base')}: "
                 f"{r.get('stat')}", "files: " + ", ".join(r.get("files") or []) or "files: none"]
        if r.get("commits"):
            lines.append("commits: " + " | ".join(r["commits"]))
        if r.get("diff"):
            lines.append(r["diff"])
        return "\n".join(lines)
    lines = []
    if op == "start":
        lines.append(f"started agy job {r.get('job')} on branch {r.get('branch')} in {r.get('worktree')} "
                     f"(base {r.get('base')}).")
        if r.get("staged"):
            lines.append("copied into the worktree's .aiw-context/ (agy cannot read outside its worktree): "
                         + "; ".join(r["staged"][:8]))
        lines.append(f"NEXT: antigravity op=wait job={r.get('job')} (blocks until its turn ends).")
        return "\n".join(lines)
    st = r.get("status")
    lines.append(f"agy job {r.get('job')}: {st}, turn {r.get('turn')}, {r.get('tools_new')} new tool steps "
                 f"({r.get('tools_total') or 0} total), cursor={r.get('cursor')}")
    for t in r.get("tools") or []:
        lines.append(f"  · {t.get('tool')}: {t.get('arg', '')[:160]}" + (f"  ERROR {t['error']}" if t.get("error") else ""))
    if r.get("response"):
        lines.append("agy's report:\n" + str(r["response"])[:4000])
    elif r.get("said"):
        lines.append("agy says: " + str(r["said"][-1])[:1500])
    ch = r.get("changes") or {}
    if ch:
        lines.append(f"worktree changes: {ch.get('stat')}; files: {', '.join((ch.get('files') or [])[:30]) or 'none'}")
        if ch.get("commits"):
            lines.append("commits: " + " | ".join(ch["commits"][:10]))
    if r.get("denied"):
        lines.append("DENIED steps (headless agy cannot ask, so it stopped): " + " | ".join(
            (f"{d.get('tool')} {d.get('target') or ''} [{d.get('action')} permission: {d.get('why')}]"
             if isinstance(d, dict) else str(d)) for d in r["denied"][-5:]))
    if r.get("ignored_rules"):
        lines.append("agy ignores these settings rules (unknown action): " + " | ".join(r["ignored_rules"][:5]))
    if r.get("auth_error"):
        lines.append(f"AUTH: {r['auth_error']}")
    if r.get("error"):
        lines.append(f"error: {r['error']}")
    if st == "running":
        lines.append(f"NEXT: still working. antigravity op=wait job={r.get('job')} cursor={r.get('cursor')}.")
    elif st in ("turn_done", "turn_failed"):
        lines.append("NEXT: do NOT trust the report. Verify it yourself in the worktree (run the tests / build / "
                     "verify_html there; read the diff with op=diff full=true). If something fails, op=send the exact "
                     "failure output back to the same job; when it passes, finish with done and tell the user the branch "
                     "to merge. If DENIED actions matter, ask_user whether to allow them.")
    elif st == "blocked":
        lines.append("NEXT: agy was blocked by a permission. NEVER work around it: do not run `agy` yourself in shell, "
                     "never pass --dangerously-skip-permissions, never edit ~/.gemini (those are forbidden and will be "
                     "refused). If the denied path is outside the worktree, op=send the same job the path of the file "
                     "inside the worktree (or under .aiw-context/) and tell it to continue; otherwise ask_user, quoting "
                     "the denied step and its reason, whether to allow it. If you finish the work yourself instead, "
                     "say so to the user: Antigravity did not do it.")
    elif st in ("stuck", "timed_out", "error"):
        lines.append("NEXT: the session ended abnormally. Check op=diff; if the work is usable verify it, otherwise "
                     "tell the user what happened (start a new job with conversation=<conversation_id> to resume).")
    return "\n".join(lines)


def with_report(result: str, rep: dict | None) -> str:
    """v3.9.56: the task result ends with Antigravity's own last report (what agy says it did), marked as its words,
    so the user reads it next to AIW's verified summary. AIW_AGY_SHOW_REPORT=0 turns it off."""
    if not rep or not rep.get("text") or os.environ.get("AIW_AGY_SHOW_REPORT", "1") == "0":
        return result
    text = rep["text"]
    if len(text) > 3000:
        text = text[:3000].rstrip() + " …（完整内容在电脑上 ~/.config/aiw/agy/jobs/" + str(rep.get("job")) + "/）"
    if text[:200] in (result or ""):
        return result
    head = f"Antigravity 的总结（job {rep.get('job')}" + (f"，分支 {rep['branch']}" if rep.get("branch") else "") + \
        (f"，{rep['changes']}" if rep.get("changes") else "") + "；这是它自己的说法，以上面 AIW 的结论为准）："
    return (result or "").rstrip() + "\n\n" + head + "\n" + text
