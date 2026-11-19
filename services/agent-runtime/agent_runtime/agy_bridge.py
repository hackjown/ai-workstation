#!/usr/bin/env python3
"""AIW <-> Antigravity CLI bridge (v3.9.54). Runs ON THE COMPUTER, stdlib only.

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

AGYV = "1"
ROOT = os.path.expanduser("~/.config/aiw/agy")
JOBS = os.path.join(ROOT, "jobs")
MAX_TOOL_OUT = 600
MAX_RESP = 6000


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
           "proxy": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))}
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
         "pid": p.pid, "status": st.get("status"), "error": st.get("error")})


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
                den = self.state.setdefault("denied", [])
                if s[:300] not in den:
                    den.append(s[:300])
                    del den[:-20]
                self.record({"kind": "denied", "text": s[:500]})
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
                self.record(rec)
                self.state["tools"] = int(self.state.get("tools") or 0) + 1
            if s.get("subagent_info"):
                subs = (s["subagent_info"] or {}).get("subagents") or []
                self.record({"kind": "subagents", "list": [{"type": x.get("type_name"), "role": x.get("role")}
                                                           for x in subs][:8]})
            self.save()
        elif kind == "result":
            r = e.get("result") or {}
            text = (r.get("response") or "".join(self.resp)).strip()
            self.resp = []
            self.turn_active = False
            self.idle_since = time.time()
            turns = int(self.state.get("turn") or 0)
            self.record({"kind": "result", "status": r.get("status"), "text": clip(text, MAX_RESP),
                         "error": r.get("error")})
            self.save(status="turn_done" if r.get("status") == "SUCCESS" else "turn_failed",
                      last_status=r.get("status"), last_error=r.get("error"), response=clip(text, MAX_RESP),
                      usage=r.get("usage"), num_turns=r.get("num_turns"), turn=turns,
                      conversation_id=r.get("conversation_id") or self.state.get("conversation_id"))

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
        self.save(status="running", agy_pid=proc.pid)
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
            elif not self.turn_active and now - self.idle_since > keep_s:
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
           "branch": job.get("branch"), "note": st.get("note"), "auth_error": st.get("auth_error")}
    if st.get("status") in ("running", "starting") and st.get("pid") and not alive(st.get("pid")):
        res["status"] = "error"
        res["error"] = "the bridge daemon is gone (computer restarted?)"
    if with_diff and job.get("worktree") and os.path.isdir(job["worktree"]):
        res["changes"] = changes(job)
    return res


def changes(job: dict, full: bool = False) -> dict:
    wt, base = job["worktree"], job["base"]
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
    if st.get("status") not in ("running", "starting", "turn_done", "turn_failed") or not alive(st.get("pid")):
        out({"ok": False, "job": jid, "status": st.get("status"),
             "error": "the agy session has ended; start a new job (pass conversation to keep its context)",
             "conversation_id": st.get("conversation_id")})
    text = base64.b64decode(text_b64).decode("utf-8")
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
    out({"ok": True, "job": jid, "worktree": job["worktree"], "branch": job["branch"], "base": job["base"][:12],
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
    else:
        out({"ok": False, "error": f"unknown op {a.op}"}, 2)


if __name__ == "__main__":
    main()
