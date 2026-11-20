#!/usr/bin/env python3
"""AIW <-> Antigravity CLI bridge (v3.9.54, v3.9.56). Runs ON THE COMPUTER, stdlib only.

The agent writes this file to ~/.config/aiw/agy/agy_bridge.py on first use (antigravity.py), then drives it with
one shell call per op. Every op prints exactly one line `AGYRESULT {json}` as its last stdout line.

  check [--probe]          is `agy` installed (and, with --probe, signed in: one tiny headless prompt)
  start --job-b64 B64      git worktree + a detached daemon that runs
                           `agy --input-format stream-json --output-format stream-json` inside it
  wait  JOB [--seconds N] [--cursor C]
                           block until the current turn ends (or N s pass) and report what happened since cursor C
  send  JOB --text-b64 B64 queue a follow-up prompt (sent into the same agy process when the turn is over)
  diff  JOB [--full]       git status / diff of the worktree against the base commit
  stop  JOB                end the agy process (the worktree and branch stay for review)
  list                     jobs on this computer
  settings [--reset]       v3.9.56: write AIW's permission rules into agy's settings.json when it has none (or AIW
                           manages it), report rules agy ignores; --reset (run by the user in a terminal) puts the
                           AIW defaults back after a backup
  daemon JOB               (internal) the process that owns agy

Job state lives in ~/.config/aiw/agy/jobs/<job>/: job.json, state.json, events.jsonl, inbox/, stderr.log.
Nothing here pushes, merges or deletes a branch: AIW verifies the worktree and the user decides.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

AGYV = "3"
ROOT = os.path.expanduser("~/.config/aiw/agy")
JOBS = os.path.join(ROOT, "jobs")
MAX_TOOL_OUT = 600
MAX_RESP = 6000
SETTINGS = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
CTX = ".aiw-context"                 # files from outside the worktree are copied here (ignored by git)
CTX_MAX_BYTES = 60 * 1024 * 1024
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv"}

# v3.9.56: agy's permission grammar only knows these actions (cli logs of 2026-11-20: "unknown action" for bash,
# shell, run_command, edit_file, apply_patch, glob, grep_search, list_directory: those rules were silently ignored).
VALID_ACTIONS = ("read_file", "write_file", "command")
DEFAULT_ALLOW = [
    "command(git status)", "command(git diff)", "command(git log)", "command(git add)", "command(git commit)",
    "command(regex:git (status|diff|log|show|add|commit|branch|rev-parse|ls-files)( .*)?)",
    "command(regex:(ls|cat|head|tail|wc|grep|rg|find|tree|du|stat|file|diff|cmp|sort|uniq|cut|awk|jq|sha1sum|md5sum|"
    "realpath|basename|dirname|pwd|which|echo|printf|date) .*)",
    "command(regex:(mkdir|cp|mv|touch|ln) .*)",
    "command(regex:(tar|unzip|zip|gzip|gunzip) .*)",
    "command(regex:(npm|pnpm|yarn) (run )?(build|lint|test|typecheck|install|ci)( .*)?)",
    "command(regex:(python3?|uv run) -m (pytest|unittest|py_compile|ruff|mypy|http\\.server|json\\.tool)( .*)?)",
    "command(regex:(python3?|node|bash|sh) [\\w./-]+\\.(py|js|mjs|cjs|sh)( .*)?)",
    "command(regex:pytest( .*)?)",
]
DEFAULT_DENY = [
    "command(regex:.*git push.*)", "command(regex:.*git (reset --hard|clean|checkout \\.|restore \\.).*)",
    "command(regex:.*rm -rf? (/|~|\\$HOME).*)", "command(regex:.*sudo .*)",
    "command(regex:.*(curl|wget)[^|]*\\|\\s*(ba)?sh.*)",
    "command(regex:.*\\.gemini/.*)", "command(regex:.*\\.config/aiw/.*)",
    "command(regex:.*--dangerously-skip-permissions.*)",
    "write_file(~/.gemini/**)", "write_file(~/.config/aiw/**)",
]


# ------------------------------------------------------------------------------------------------------- helpers
def out(obj: dict, code: int = 0):
    sys.stdout.write("AGYRESULT " + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def agy_bin() -> str | None:
    cand = os.environ.get("AIW_AGY_BIN") or "agy"
    p = shutil.which(cand)
    if p:
        return p
    for d in ("~/.local/bin", "~/bin", "/usr/local/bin", "~/.antigravity/bin", "~/.gemini/antigravity-cli/bin"):
        f = os.path.join(os.path.expanduser(d), "agy")
        if os.access(f, os.X_OK):
            return f
    return None


def jdir(job: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", job or ""):
        out({"ok": False, "error": f"bad job id {job!r}"}, 2)
    return os.path.join(JOBS, job)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def git(args: list, cwd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n * 3 // 4] + f" …[{len(s) - n} chars]… " + s[-n // 4:]



# ------------------------------------------------------------------------------------------------------- settings
def rule_action(rule: str) -> str:
    m = re.match(r"\s*([A-Za-z_]+)\s*\(", rule or "")
    return m.group(1) if m else ""


def lint_rules(perm: dict) -> list:
    """Rules agy ignores (unknown action): AIW reports them instead of trusting them."""
    bad = []
    for k in ("allow", "deny", "ask"):
        for r in (perm or {}).get(k) or []:
            if rule_action(str(r)) not in VALID_ACTIONS:
                bad.append(f"{k}: {r}")
    return bad


def settings_state() -> dict:
    cur = read_json(SETTINGS, None)
    perm = (cur or {}).get("permissions") or {}
    return {"path": SETTINGS, "exists": cur is not None, "managed": bool((cur or {}).get("aiwManaged")),
            "allow": len(perm.get("allow") or []), "deny": len(perm.get("deny") or []),
            "ignored_rules": lint_rules(perm)[:20],
            "skip_all": bool((cur or {}).get("dangerouslySkipPermissions"))}


def op_settings(reset: bool):
    cur = read_json(SETTINGS, None)
    want = {"allow": DEFAULT_ALLOW, "deny": DEFAULT_DENY}
    action = "kept"
    if reset or cur is None or (cur.get("aiwManaged") and cur.get("permissions") != want):
        new = dict(cur or {})
        if cur is not None:
            bak = f"{SETTINGS}.bak-aiw-{time.strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(SETTINGS, bak)
        new["permissions"] = want
        new["aiwManaged"] = True
        new.setdefault("artifactReviewPolicy", "always-proceed")
        new.pop("dangerouslySkipPermissions", None)
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        write_json(SETTINGS, new)
        action = "reset" if reset else ("written" if cur is None else "updated")
    out({"ok": True, "action": action, **settings_state()})


def newest_cli_log(since: float) -> str | None:
    d = os.path.expanduser("~/.gemini/antigravity-cli/log")
    try:
        cands = [os.path.join(d, n) for n in os.listdir(d) if n.startswith("cli-") and n.endswith(".log")]
    except OSError:
        return None
    cands = [c for c in cands if os.path.getmtime(c) >= since - 5]
    return max(cands, key=os.path.getmtime) if cands else None


# ------------------------------------------------------------------------------------------------------- staging
PATH_RE = re.compile(r"(?:~|\$HOME|/home/[\w.-]+|/root|/tmp|/opt|/srv|/data)(?:/[^\s`'\"<>()\[\]{}，。；：、（）《》“”‘’!?|*]+)+")


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    n = 0
    for d, ds, fs in os.walk(path):
        ds[:] = [x for x in ds if x not in SKIP_DIRS]
        for f in fs:
            try:
                n += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return n


def _copy(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False,
                        ignore=shutil.ignore_patterns(*SKIP_DIRS))
    else:
        shutil.copy2(src, dst)


def stage(text: str, job: dict) -> tuple:
    """v3.9.56: agy only reads freely inside its workspace (the worktree): a read_file of
    /home/aiw/workspace/ldok-rebuild/mirror/... or ~/.claude/skills/... was soft-denied in headless mode, even with
    read_file(**) allowed (jobs agy-11200120-3f7d, -9524, -72a8). So every absolute path in a prompt is pointed into
    the worktree: paths in the main checkout map to the same file in the worktree; files that exist only outside it
    (untracked files, skills) are copied into <worktree>/.aiw-context/ (ignored by git) and the prompt names the copy.
    Returns (new text, [mapping lines])."""
    home = os.path.expanduser("~")
    top, wt = job["repo"].rstrip("/"), job["worktree"].rstrip("/")
    ctx = os.path.join(wt, CTX)
    staged = dict(job.get("staged") or {})
    used = sum(int(v.get("bytes") or 0) for v in staged.values())
    notes = []

    def norm(p: str) -> str:
        p = p.rstrip(".,;:")
        if p.startswith("~"):
            p = home + p[1:]
        elif p.startswith("$HOME"):
            p = home + p[5:]
        return os.path.normpath(p)

    def ensure_ctx():
        os.makedirs(ctx, exist_ok=True)
        gi = os.path.join(ctx, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as f:
                f.write("*\n")

    def mapped(raw: str) -> str:
        nonlocal used
        p = norm(raw)
        if p == wt or p.startswith(wt + "/"):
            return raw
        if p == top or p.startswith(top + "/"):
            rel = os.path.relpath(p, top)
            in_wt = os.path.normpath(os.path.join(wt, rel))
            if os.path.exists(in_wt) or not os.path.exists(p):
                return in_wt                     # tracked file, or an output path to create inside the worktree
            src, dst = p, os.path.join(ctx, "repo", rel)
        else:
            if not os.path.exists(p):
                return raw                       # nothing to copy; a write there will be denied and reported
            src = p
            m = re.match(re.escape(home) + r"/\.(?:claude|codex|agents|gemini)/skills/[^/]+", p)
            if m:
                src = m.group(0)                 # a skill: copy the whole skill folder (references/, scripts/)
            dst = os.path.join(ctx, "ext", src.lstrip("/"))
        if src not in staged:
            sz = _size(src)
            if used + sz > CTX_MAX_BYTES:
                notes.append(f"NOT copied (too big, {sz // 1024} KB): {src}")
                return raw
            ensure_ctx()
            _copy(src, dst)
            used += sz
            staged[src] = {"to": dst, "bytes": sz}
            notes.append(f"{src} -> {dst}")
        dst = staged[src]["to"]
        return dst + p[len(src):]

    new = PATH_RE.sub(lambda m: mapped(m.group(0)), text or "")
    job["staged"] = staged
    if notes:
        new += ("\n\n[AIW] Files from outside this worktree were copied into ./" + CTX +
                "/ (read them there; it is not committed). Write only inside the worktree.\n" +
                "\n".join("- " + n for n in notes))
    return new, notes

# ------------------------------------------------------------------------------------------------------- check
def op_check(probe: bool):
    b = agy_bin()
    if not b:
        out({"ok": False, "installed": False, "bridge": AGYV,
             "error": "agy not found on PATH (install the Antigravity CLI from antigravity.google/download)"})
    ver = ""
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20)
        ver = (r.stdout or r.stderr).strip().splitlines()[0][:80] if (r.stdout or r.stderr).strip() else ""
    except Exception as e:  # noqa: BLE001
        ver = f"? ({type(e).__name__})"
    res = {"ok": True, "installed": True, "path": b, "version": ver, "bridge": AGYV,
           "git": bool(shutil.which("git")),
           "proxy": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
           "settings": settings_state()}
    if probe:
        try:
            r = subprocess.run([b, "-p", "Reply with exactly: ok", "--output-format", "json", "--print-timeout", "90s"],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            env = json.loads((r.stdout or "").strip().splitlines()[-1]) if (r.stdout or "").strip() else {}
            res["signed_in"] = env.get("status") == "SUCCESS"
            if not res["signed_in"]:
                res["ok"] = False
                res["error"] = (env.get("error") or r.stderr or "no response")[-500:]
        except Exception as e:  # noqa: BLE001
            res["ok"], res["signed_in"], res["error"] = False, False, f"{type(e).__name__}: {e}"[:300]
    out(res)


# ------------------------------------------------------------------------------------------------------- start
def op_start(job_b64: str):
    try:
        job = json.loads(base64.b64decode(job_b64).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        out({"ok": False, "error": f"bad job: {e}"}, 2)
    b = agy_bin()
    if not b:
        out({"ok": False, "installed": False, "error": "agy not found on PATH"})
    if job.get("plain"):   # v3.9.57: a chat session (AIW 的 agy 对话) runs in a folder, no worktree or branch
        return start_plain(job, b)
    repo = os.path.abspath(os.path.expanduser(job.get("repo") or "~/workspace"))
    r = git(["rev-parse", "--show-toplevel"], repo)
    if r.returncode != 0:
        out({"ok": False, "error": f"{repo} is not a git repository (git init it first): {r.stderr.strip()[:200]}"})
    top = r.stdout.strip()
    base = (job.get("base") or "HEAD").strip()
    rb = git(["rev-parse", "--verify", base + "^{commit}"], top)
    if rb.returncode != 0:
        out({"ok": False, "error": f"base {base!r} not found: {rb.stderr.strip()[:200]}"})
    base_sha = rb.stdout.strip()
    jid = (job.get("id") or ("agy-" + time.strftime("%m%d%H%M") + "-" + uuid.uuid4().hex[:4]))
    d = jdir(jid)
    if os.path.exists(d):
        out({"ok": False, "error": f"job {jid} exists"})
    os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
    branch = job.get("branch") or f"aiw/{jid}"
    wt_root = os.path.join(os.path.dirname(top), ".aiw-worktrees", os.path.basename(top))
    os.makedirs(wt_root, exist_ok=True)
    wt = os.path.join(wt_root, jid)
    r = git(["worktree", "add", "-b", branch, wt, base_sha], top, 120)
    if r.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        out({"ok": False, "error": f"git worktree add failed: {(r.stderr or r.stdout).strip()[:300]}"})
    job.update({"id": jid, "repo": top, "worktree": wt, "branch": branch, "base": base_sha, "agy": b,
                "created": time.time()})
    try:
        job["prompt"], _ = stage(job.get("prompt") or "", job)
    except Exception as e:  # noqa: BLE001  (staging is a help, never a reason not to start)
        job["stage_error"] = f"{type(e).__name__}: {e}"[:300]
    write_json(os.path.join(d, "job.json"), job)
    write_json(os.path.join(d, "state.json"), {"status": "starting", "turn": 0, "events": 0, "ts": time.time()})
    with open(os.path.join(d, "inbox", f"{time.time():.6f}.json"), "w", encoding="utf-8") as f:
        json.dump({"text": job.get("prompt") or ""}, f, ensure_ascii=False)
    log = open(os.path.join(d, "daemon.log"), "ab")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon", jid], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, cwd=wt)
    for _ in range(40):   # up to 10 s for the daemon to come up
        st = read_json(os.path.join(d, "state.json"), {}) or {}
        if st.get("status") not in (None, "starting") or not alive(p.pid):
            break
        time.sleep(0.25)
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    out({"ok": st.get("status") not in ("error",), "job": jid, "worktree": wt, "branch": branch, "base": base_sha[:12],
         "pid": p.pid, "status": st.get("status"), "error": st.get("error"),
         "staged": [f"{k} -> {v['to']}" for k, v in (job.get("staged") or {}).items()][:20],
         "settings": settings_state()})


def start_plain(job: dict, b: str):
    """v3.9.57: the folder is the session's workspace (made when missing). agy edits it in place, so AIW's chat shows
    `git status` there when it is a repository; nothing is branched, pushed or reset."""
    wd = os.path.abspath(os.path.expanduser(job.get("workdir") or "~/workspace/agy-chat"))
    os.makedirs(wd, exist_ok=True)
    jid = job.get("id") or ("agyc-" + time.strftime("%m%d%H%M") + "-" + uuid.uuid4().hex[:4])
    d = jdir(jid)
    if os.path.exists(d):
        out({"ok": False, "error": f"job {jid} exists"})
    os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
    job.update({"id": jid, "repo": wd, "worktree": wd, "branch": "", "base": "", "agy": b, "created": time.time()})
    try:
        job["prompt"], _ = stage(job.get("prompt") or "", job)
    except Exception as e:  # noqa: BLE001
        job["stage_error"] = f"{type(e).__name__}: {e}"[:300]
    write_json(os.path.join(d, "job.json"), job)
    write_json(os.path.join(d, "state.json"), {"status": "starting", "turn": 0, "events": 0, "ts": time.time()})
    with open(os.path.join(d, "inbox", f"{time.time():.6f}.json"), "w", encoding="utf-8") as f:
        json.dump({"text": job.get("prompt") or ""}, f, ensure_ascii=False)
    log = open(os.path.join(d, "daemon.log"), "ab")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "daemon", jid], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, cwd=wd)
    for _ in range(40):
        st = read_json(os.path.join(d, "state.json"), {}) or {}
        if st.get("status") not in (None, "starting") or not alive(p.pid):
            break
        time.sleep(0.25)
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    out({"ok": st.get("status") not in ("error",), "job": jid, "worktree": wd, "branch": "", "base": "",
         "pid": p.pid, "status": st.get("status"), "error": st.get("error"), "plain": True,
         "settings": settings_state()})


# ------------------------------------------------------------------------------------------------------- daemon
class Daemon:
    def __init__(self, jid: str):
        self.jid, self.d = jid, jdir(jid)
        self.job = read_json(os.path.join(self.d, "job.json"), {})
        self.state = read_json(os.path.join(self.d, "state.json"), {}) or {}
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.ev = open(os.path.join(self.d, "events.jsonl"), "a", encoding="utf-8")
        self.err = open(os.path.join(self.d, "stderr.log"), "a", encoding="utf-8")
        self.turn_active = False
        self.last_io = time.time()
        self.started = time.time()
        self.idle_since = time.time()
        self.resp: list[str] = []
        self.pending_denial: str | None = None
        self.turn_denied = False

    def save(self, **kw):
        self.state.update(kw, ts=time.time(), pid=os.getpid())
        write_json(os.path.join(self.d, "state.json"), self.state)

    def record(self, rec: dict):
        self.state["events"] = int(self.state.get("events") or 0) + 1
        rec["n"] = self.state["events"]
        rec["t"] = round(time.time(), 2)
        self.ev.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.ev.flush()

    def cmd(self) -> list:
        j = self.job
        c = [j["agy"], "--input-format", "stream-json", "--output-format", "stream-json"]
        for k, flag in (("model", "--model"), ("effort", "--effort"), ("agent", "--agent"), ("mode", "--mode")):
            if j.get(k):
                c += [flag, str(j[k])]
        if j.get("conversation"):
            c += ["--conversation", str(j["conversation"])]
        if j.get("sandbox"):
            c.append("--sandbox")
        if j.get("remote_control"):
            c.append("--remote-control")
        if j.get("skip_permissions"):
            c.append("--dangerously-skip-permissions")
        return c

    def pump(self, stream, name: str):
        for line in iter(stream.readline, ""):
            self.lines.put(line if name == "out" else "\x00ERR" + line)
        self.lines.put(None if name == "out" else "\x00ERREOF")

    def next_inbox(self):
        ib = os.path.join(self.d, "inbox")
        try:
            names = sorted(n for n in os.listdir(ib) if n.endswith(".json"))
        except OSError:
            return None
        for n in names:
            path = os.path.join(ib, n)
            m = read_json(path, {}) or {}
            try:
                os.remove(path)
            except OSError:
                pass
            if str(m.get("text") or "").strip():
                return str(m["text"])
        return None

    def handle(self, line: str):
        if line.startswith("\x00ERR"):
            s = line[4:].rstrip("\n")
            if s == "EOF":
                return
            self.err.write(s + "\n")
            self.err.flush()
            if re.search(r"denied|not allowed|permission|requires approval|permissions\.allow", s, re.I):
                m = re.search(r'the "([\w-]+)" permission', s)
                self.pending_denial = m.group(1) if m else "?"
                self.turn_denied = True
                self.record({"kind": "denied", "text": s[:500], "action": self.pending_denial})
            elif re.search(r"authentication required|sign in|login", s, re.I):
                self.state["auth_error"] = s[:300]
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        self.last_io = time.time()
        kind = e.get("event")
        if kind == "init":
            ini = e.get("init") or {}
            self.save(status="running", conversation_id=e.get("conversation_id"),
                      permission_mode=ini.get("permission_mode"), model=ini.get("model"))
            self.record({"kind": "init", "conversation_id": e.get("conversation_id"),
                         "permission_mode": ini.get("permission_mode")})
        elif kind == "step_update":
            s = e.get("step_update") or {}
            st = s.get("step_type")
            if st == "agent_response":
                if s.get("text_delta"):
                    self.resp.append(s["text_delta"])
                if s.get("state") == "DONE":
                    self.record({"kind": "say", "text": clip("".join(self.resp).strip(), 1200)})
            elif st == "tool" and s.get("state") == "DONE":
                ti = s.get("tool_info") or {}
                params = ti.get("parameters") or {}
                arg = (params.get("CommandLine") or params.get("TargetFile") or params.get("AbsolutePath")
                       or params.get("Query") or params.get("Url") or json.dumps(params, ensure_ascii=False))
                rec = {"kind": "tool", "tool": s.get("tool_name") or ti.get("name"), "arg": clip(str(arg), 300),
                       "out": clip(str(ti.get("output") or ""), MAX_TOOL_OUT)}
                if ti.get("error"):
                    rec["error"] = clip(str((ti["error"] or {}).get("message") or ti["error"]), 300)
                if self.pending_denial and not rec["out"]:
                    self.add_denied(self.pending_denial, rec["tool"], params)
                    rec["error"] = rec.get("error") or f"DENIED ({self.pending_denial} permission)"
                    self.pending_denial = None
                self.record(rec)
                self.state["tools"] = int(self.state.get("tools") or 0) + 1
            if s.get("subagent_info"):
                subs = (s["subagent_info"] or {}).get("subagents") or []
                self.record({"kind": "subagents", "list": [{"type": x.get("type_name"), "role": x.get("role")}
                                                           for x in subs][:8]})
            self.save()
        elif kind == "result":
            if self.pending_denial:              # a denial with no tool step after it
                self.add_denied(self.pending_denial, "?", {})
                self.pending_denial = None
            r = e.get("result") or {}
            text = (r.get("response") or "".join(self.resp)).strip()
            self.resp = []
            self.turn_active = False
            self.idle_since = time.time()
            turns = int(self.state.get("turn") or 0)
            self.record({"kind": "result", "status": r.get("status"), "text": clip(text, MAX_RESP),
                         "error": r.get("error")})
            blocked = self.turn_denied
            self.turn_denied = False
            if blocked and not text:
                d0 = (self.state.get("denied") or [{}])[-1]
                text = (f"(agy produced no report: its step {d0.get('tool')} {d0.get('target') or ''} was denied "
                        f"by the {d0.get('action')} permission, and headless mode cannot ask)")
            self.save(status="blocked" if blocked else ("turn_done" if r.get("status") == "SUCCESS" else "turn_failed"),
                      last_status=r.get("status"), last_error=r.get("error"), response=clip(text, MAX_RESP),
                      usage=r.get("usage"), num_turns=r.get("num_turns"), turn=turns,
                      conversation_id=r.get("conversation_id") or self.state.get("conversation_id"))

    def add_denied(self, action: str, tool, params: dict):
        """v3.9.56: say which step was denied, on what, and why (the old list only had agy's generic text)."""
        target = ""
        for k in ("Path", "AbsolutePath", "TargetFile", "File", "CommandLine", "Command"):
            if params.get(k):
                target = str(params[k])
                break
        wt = self.job.get("worktree") or ""
        outside = bool(target.startswith("/") and wt and not (target == wt or target.startswith(wt.rstrip("/") + "/")))
        if outside:
            why = "the path is outside the job's worktree; agy only reads/writes freely inside it"
        elif action == "command":
            why = "the command matches no allow rule in agy settings.json (AIW defaults)"
        else:
            why = f"no {action} allow rule covers it"
        rec = {"action": action, "tool": tool, "target": clip(target, 300), "outside_worktree": outside, "why": why}
        den = self.state.setdefault("denied", [])
        den.append(rec)
        del den[:-20]

    def run(self):
        j = self.job
        idle_s = float(j.get("idle_min") or 15) * 60
        total_s = float(j.get("timeout_min") or 120) * 60
        keep_s = float(j.get("keep_min") or 60) * 60    # an idle session waiting for a follow-up stays this long
        max_turns = int(j.get("max_turns") or 8)
        env = dict(os.environ)
        home = os.path.expanduser("~")
        env["PATH"] = ":".join([f"{home}/.local/bin", f"{home}/bin", "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
        try:
            proc = subprocess.Popen(self.cmd(), cwd=j["worktree"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, env=env, start_new_session=True)
        except Exception as e:  # noqa: BLE001
            self.save(status="error", error=f"cannot start agy: {e}")
            return
        self.save(status="running", agy_pid=proc.pid, settings=settings_state())
        threading.Thread(target=self.pump, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=self.pump, args=(proc.stderr, "err"), daemon=True).start()
        stop_file = os.path.join(self.d, "STOP")
        end_reason = None
        while True:
            now = time.time()
            if os.path.exists(stop_file):
                end_reason = "stopped"
            elif now - self.started > total_s:
                end_reason = "timeout"
            elif self.turn_active and now - self.last_io > idle_s:
                end_reason = "idle"
            elif not self.turn_active and now - self.idle_since > (
                    min(keep_s, 600) if self.state.get("status") == "blocked" else keep_s):
                end_reason = "expired"
            if end_reason:
                break
            if not self.turn_active and proc.poll() is None:
                text = self.next_inbox()
                if text is not None:
                    if int(self.state.get("turn") or 0) >= max_turns:
                        self.record({"kind": "refused", "text": f"max_turns {max_turns} reached"})
                        self.save(status="turn_done", note=f"max_turns {max_turns} reached; start a new job")
                    else:
                        try:
                            proc.stdin.write(json.dumps({"event": "user", "message": {"content": text}},
                                                        ensure_ascii=False) + "\n")
                            proc.stdin.flush()
                        except (BrokenPipeError, OSError) as e:
                            self.save(status="error", error=f"agy stdin closed: {e}")
                            end_reason = "crashed"
                            break
                        self.turn_active = True
                        self.last_io = time.time()
                        t = int(self.state.get("turn") or 0) + 1
                        self.record({"kind": "prompt", "turn": t, "text": clip(text, 800)})
                        self.save(status="running", turn=t, response=None)
            try:
                line = self.lines.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None and self.lines.empty():
                    end_reason = "exited"
                    break
                continue
            if line is None:
                end_reason = "exited"
                break
            self.handle(line)
        # ---- shut down
        if proc.poll() is None:
            try:
                if end_reason in ("idle", "timeout", "stopped") and self.turn_active:
                    os.killpg(proc.pid, signal.SIGINT)
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        # drain what is left (the final result after SIGINT)
        t_end = time.time() + 3
        while time.time() < t_end:
            try:
                line = self.lines.get(timeout=0.3)
            except queue.Empty:
                break
            if line is None:
                break
            self.handle(line)
        final = {"stopped": "stopped", "timeout": "timed_out", "idle": "stuck", "expired": "ended",
                 "exited": "ended", "crashed": "error"}.get(end_reason or "", "ended")
        if final == "ended" and self.turn_active:
            final = "error"
            self.state["error"] = self.state.get("auth_error") or "agy exited in the middle of a turn"
        self.record({"kind": "end", "reason": end_reason, "exit_code": proc.returncode})
        self.save(status=final, end_reason=end_reason, exit_code=proc.returncode)


# ------------------------------------------------------------------------------------------------------- wait
def summary(jid: str, cursor: int, with_diff: bool = True) -> dict:
    d = jdir(jid)
    job = read_json(os.path.join(d, "job.json"), {}) or {}
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    evs = []
    try:
        with open(os.path.join(d, "events.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if int(e.get("n") or 0) > cursor:
                    evs.append(e)
    except OSError:
        pass
    tools = [e for e in evs if e.get("kind") == "tool"]
    shown = tools[-25:]
    res = {"ok": True, "job": jid, "status": st.get("status"), "turn": st.get("turn"),
           "conversation_id": st.get("conversation_id"), "cursor": int(st.get("events") or cursor),
           "tools_total": st.get("tools"), "tools_new": len(tools),
           "tools": [{k: e.get(k) for k in ("tool", "arg", "error", "out") if e.get(k)} for e in shown],
           "said": [e.get("text") for e in evs if e.get("kind") == "say"][-3:],
           "response": st.get("response"), "last_status": st.get("last_status"), "error": st.get("error") or st.get("last_error"),
           "denied": st.get("denied") or [], "usage": st.get("usage"), "worktree": job.get("worktree"),
           "branch": job.get("branch"), "note": st.get("note"), "auth_error": st.get("auth_error"),
           "ignored_rules": ((st.get("settings") or {}).get("ignored_rules") or [])[:8],
           "staged": len(job.get("staged") or {})}
    if st.get("status") in ("running", "starting") and st.get("pid") and not alive(st.get("pid")):
        res["status"] = "error"
        res["error"] = "the bridge daemon is gone (computer restarted?)"
    if with_diff and job.get("worktree") and os.path.isdir(job["worktree"]):
        res["changes"] = changes(job)
    return res


def changes(job: dict, full: bool = False) -> dict:
    wt, base = job["worktree"], job["base"]
    if not base:   # v3.9.57 plain session: what changed in the folder (when it is a git repository)
        if git(["rev-parse", "--git-dir"], wt).returncode != 0:
            return {"files": [], "stat": "not a git repository", "commits": []}
        names = git(["status", "--short"], wt).stdout.strip().splitlines()
        r = {"files": names[:80], "stat": f"{len(names)} changed" if names else "no changes", "commits": []}
        if full:
            r["diff"] = clip(git(["diff"], wt, 120).stdout, 60000)
        return r
    git(["add", "-A", "-N"], wt)   # untracked files show in the diff without staging content
    stat = git(["diff", "--stat", base], wt).stdout.strip().splitlines()
    names = git(["diff", "--name-status", base], wt).stdout.strip().splitlines()
    commits = git(["log", "--oneline", f"{base}..HEAD"], wt).stdout.strip().splitlines()
    r = {"files": names[:80], "stat": stat[-1] if stat else "no changes", "commits": commits[:20]}
    if full:
        r["diff"] = clip(git(["diff", base], wt, 120).stdout, 60000)
    return r


def op_wait(jid: str, seconds: float, cursor: int):
    d = jdir(jid)
    if not os.path.isdir(d):
        out({"ok": False, "error": f"no job {jid}"})
    t_end = time.time() + max(1.0, min(seconds, 1800))
    while time.time() < t_end:
        st = read_json(os.path.join(d, "state.json"), {}) or {}
        inbox_empty = not any(n.endswith(".json") for n in os.listdir(os.path.join(d, "inbox")))
        if st.get("status") not in ("running", "starting") and inbox_empty:
            break
        if st.get("pid") and not alive(st.get("pid")) and st.get("status") in ("running", "starting"):
            break
        time.sleep(2)
    out(summary(jid, cursor))


def op_send(jid: str, text_b64: str):
    d = jdir(jid)
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    if st.get("status") not in ("running", "starting", "turn_done", "turn_failed", "blocked") or not alive(st.get("pid")):
        out({"ok": False, "job": jid, "status": st.get("status"),
             "error": "the agy session has ended; start a new job (pass conversation to keep its context)",
             "conversation_id": st.get("conversation_id")})
    text = base64.b64decode(text_b64).decode("utf-8")
    job = read_json(os.path.join(d, "job.json"), {}) or {}
    if job.get("worktree"):
        try:
            text, _ = stage(text, job)
            write_json(os.path.join(d, "job.json"), job)
        except Exception:  # noqa: BLE001
            pass
    with open(os.path.join(d, "inbox", f"{time.time():.6f}.json"), "w", encoding="utf-8") as f:
        json.dump({"text": text}, f, ensure_ascii=False)
    out({"ok": True, "job": jid, "queued": True, "status": st.get("status")})


def op_stop(jid: str):
    d = jdir(jid)
    if not os.path.isdir(d):
        out({"ok": False, "error": f"no job {jid}"})
    open(os.path.join(d, "STOP"), "w").close()
    for _ in range(30):
        st = read_json(os.path.join(d, "state.json"), {}) or {}
        if not alive(st.get("pid")):
            break
        time.sleep(1)
    out(summary(jid, 10 ** 9))


def op_diff(jid: str, full: bool):
    job = read_json(os.path.join(jdir(jid), "job.json"), None)
    if not job:
        out({"ok": False, "error": f"no job {jid}"})
    out({"ok": True, "job": jid, "worktree": job["worktree"], "branch": job["branch"], "base": (job["base"] or "")[:12],
         **changes(job, full)})


def op_list():
    rows = []
    for n in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        st = read_json(os.path.join(JOBS, n, "state.json"), {}) or {}
        job = read_json(os.path.join(JOBS, n, "job.json"), {}) or {}
        rows.append({"job": n, "status": st.get("status"), "turn": st.get("turn"), "branch": job.get("branch"),
                     "alive": alive(st.get("pid")), "repo": job.get("repo")})
    out({"ok": True, "jobs": rows[-30:]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("op")
    ap.add_argument("job", nargs="?")
    ap.add_argument("--job-b64")
    ap.add_argument("--text-b64")
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--cursor", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    os.makedirs(JOBS, exist_ok=True)
    if a.op == "check":
        op_check(a.probe)
    elif a.op == "start":
        op_start(a.job_b64 or "")
    elif a.op == "daemon":
        dm = Daemon(a.job)
        try:
            dm.run()
        except Exception as e:  # noqa: BLE001
            dm.save(status="error", error=f"bridge crashed: {type(e).__name__}: {e}")
            raise
    elif a.op == "wait":
        op_wait(a.job, a.seconds, a.cursor)
    elif a.op == "send":
        op_send(a.job, a.text_b64 or "")
    elif a.op == "stop":
        op_stop(a.job)
    elif a.op == "diff":
        op_diff(a.job, a.full)
    elif a.op == "list":
        op_list()
    elif a.op == "settings":
        op_settings(a.reset)
    else:
        out({"ok": False, "error": f"unknown op {a.op}"}, 2)


if __name__ == "__main__":
    main()
