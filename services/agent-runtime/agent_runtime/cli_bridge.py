#!/usr/bin/env python3
"""AIW <-> one-shot coding CLIs bridge (v3.9.60). Runs ON THE COMPUTER, stdlib only.

agy keeps one process per session (agy_bridge.py). The engines here run one process per turn and resume their own
session id on the next turn, so this bridge keeps a small daemon per conversation that takes queued messages and
runs one CLI call for each:

  codex      `codex exec --json [-c ...] [resume <id>] <prompt>`  (OpenAI Codex CLI; sandbox workspace-write, no
             approvals; --skip-git-repo-check for folders that are not repositories)
  claude     `claude -p --output-format stream-json --verbose [--resume <id>]`, prompt on stdin (Claude Code; edits
             auto-accepted, shell allowed, the deny list below removed)
  codebuddy  `codebuddy -p --output-format stream-json -y [-r <id>]`, prompt on stdin (CodeBuddy Code, the CLI of
             Tencent WorkBuddy, same account; -y is required headless or every edit/command is blocked)
  grok       `grok --prompt-file F --output-format streaming-messages-json --cwd W --always-approve [-r <id>]`
             (xAI Grok Build; same Messages stream as Claude Code)

Ops (each prints exactly one line `CLIRESULT {json}` as its last stdout line):
  check ENGINE [--probe]   is the CLI installed (and, with --probe, signed in: one tiny prompt)
  start --job-b64 B64      a daemon for one conversation; the first prompt is queued
  wait  JOB [--seconds N] [--cursor C]
  send  JOB --text-b64 B64 queue a follow-up (fails when the daemon has ended: start again with the session id)
  stop  JOB                interrupt the running turn and end the daemon
  diff  JOB [--full]       git status / diff of the folder
  models ENGINE            the models this computer's CLI can use (its own list when it has one, else its config)
  list                     jobs on this computer
  daemon JOB               (internal)

Job state lives in ~/.config/aiw/cli/jobs/<job>/ (job.json, state.json, events.jsonl, inbox/, turn-N.stderr).
The event and summary shapes match agy_bridge.py, so AIW reads every engine the same way. Nothing here pushes,
merges or deletes a branch.
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

CLIV = "1"
ROOT = os.path.expanduser("~/.config/aiw/cli")
JOBS = os.path.join(ROOT, "jobs")
MAX_TOOL_OUT = 600
MAX_RESP = 6000
MAX_PROMPT = 120_000

ENGINES = {
    "codex": {"title": "Codex", "bins": ("codex",), "env": "AIW_CODEX_BIN", "stdin": False,
              "install": "npm i -g @openai/codex，然后运行一次 codex 登录",
              "efforts": ["minimal", "low", "medium", "high", "xhigh"]},
    "claude": {"title": "Claude Code", "bins": ("claude",), "env": "AIW_CLAUDE_BIN", "stdin": True,
               "install": "npm i -g @anthropic-ai/claude-code，然后运行一次 claude 登录", "efforts": []},
    "codebuddy": {"title": "CodeBuddy", "bins": ("codebuddy", "cbc"), "env": "AIW_CODEBUDDY_BIN", "stdin": True,
                  "install": "npm i -g @tencent-ai/codebuddy-code，然后运行一次 codebuddy 用 WorkBuddy 账号登录",
                  "efforts": ["minimal", "low", "medium", "high", "xhigh", "max"]},   # settings.reasoningEffort
    "grok": {"title": "Grok Build", "bins": ("grok",), "env": "AIW_GROK_BIN", "stdin": False,
             "install": "curl -fsSL https://x.ai/cli/install.sh | bash，然后运行一次 grok login --device-auth",
             "efforts": ["low", "medium", "high"]},
}

# What no engine may run (the same lines as agy's deny rules). Claude-style engines take them as deny rules; Codex
# runs in its workspace-write sandbox instead, and AIW's prompt rules forbid the rest.
DENY_SHELL = ["git push", "git reset --hard", "git clean", "git checkout .", "git restore .", "sudo", "rm -rf /",
              "rm -rf ~", "rm -rf $HOME"]
DENY_PATHS = ["~/.config/aiw/**", "~/.codex/**", "~/.claude/**", "~/.codebuddy/**", "~/.grok/**", "~/.gemini/**"]
STATIC_MODELS = {   # shown when the CLI has no model list of its own; any model id the account has also works
    "claude": [("opus", "Opus（最新）"), ("sonnet", "Sonnet（最新）"), ("haiku", "Haiku（最新）")],
    # v3.9.63: CodeBuddy only lists models inside its TUI (/model), per WorkBuddy account; this is the list a
    # WorkBuddy account showed on 2026-11-21 (credits multiplier in the label). Others: 「其他（手动填模型名）」.
    "codebuddy": [("hy4std", "hy4std（x0.29）"), ("hy4flash-free", "hy4flash-free（免费）"),
                  ("hy4flash", "hy4flash（x0.03）"), ("deepseek-v4.1-flash", "deepseek-v4.1-flash（x0.11）"),
                  ("glm-5.3", "glm-5.3（x0.69）"), ("glm-5.3-flash", "glm-5.3-flash（x0.08）"),
                  ("glm-5.2", "glm-5.2（x0.79）"), ("space-bunny", "space-bunny（x0.08）"),
                  ("minimax-m3.1-flash", "minimax-m3.1-flash（x0.08）"), ("minimax-m2.7", "minimax-m2.7（x0.19）"),
                  ("kimi-k3.1-pro", "kimi-k3.1-pro（x0.97）"), ("kimi-k3-1", "kimi-k3-1（x1.62）")],
}
AUTH_RE = re.compile(r"not logged in|log ?in required|please (log|sign) ?in|authenticat|unauthori[sz]ed|\b401\b|"
                     r"api key|invalid x-api-key|credentials", re.I)


# ------------------------------------------------------------------------------------------------------- helpers
def out(obj: dict, code: int = 0):
    sys.stdout.write("CLIRESULT " + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def find_bin(engine: str) -> str | None:
    spec = ENGINES.get(engine)
    if not spec:
        return None
    env = os.environ.get(spec["env"])
    if env:
        p = shutil.which(env) or (env if os.access(os.path.expanduser(env), os.X_OK) else None)
        if p:
            return os.path.expanduser(p)
    for name in spec["bins"]:
        p = shutil.which(name)
        if p:
            return p
        for d in ("~/.local/bin", "~/bin", "/usr/local/bin", "~/.npm-global/bin", f"~/.{engine}/bin", "~/.grok/bin"):
            f = os.path.join(os.path.expanduser(d), name)
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
        if not pid:
            return False
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:   # a zombie (exited, not reaped) is not alive
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def clip(s, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 20] + f"…(+{len(s) - n + 20})"


def run_env() -> dict:
    env = dict(os.environ)
    home = os.path.expanduser("~")
    env["PATH"] = ":".join([f"{home}/.local/bin", f"{home}/bin", f"{home}/.npm-global/bin", f"{home}/.grok/bin",
                            "/usr/local/bin", env.get("PATH", "/usr/bin:/bin")])
    env.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    env.setdefault("CI", "1")
    return env


# ------------------------------------------------------------------------------------------------------- commands
def claude_tools() -> tuple:
    deny = [f"Bash({c}:*)" for c in DENY_SHELL] + [f"Edit({p})" for p in DENY_PATHS] + [f"Write({p})" for p in DENY_PATHS]
    return "Bash,Edit,Write,MultiEdit,Read,Glob,Grep,LS,TodoWrite,NotebookEdit", ",".join(deny)


def build_cmd(job: dict, prompt: str, session: str, turn_dir: str) -> tuple:
    """(argv, stdin text or None) for one turn."""
    e, b, wd = job["engine"], job["bin"], job["workdir"]
    model, effort = str(job.get("model") or ""), str(job.get("effort") or "")
    if e == "codex":
        c = [b, "exec", "--json", "--skip-git-repo-check", "-C", wd,
             "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"']
        if model:
            c += ["-m", model]
        if effort:
            c += ["-c", f'model_reasoning_effort="{effort}"']
        if session:
            c += ["resume", session]
        return c + [prompt], None
    if e in ("claude", "codebuddy"):
        allow, deny = claude_tools()
        c = [b, "-p", "--output-format", "stream-json"]
        if e == "claude":
            c += ["--verbose", "--permission-mode", "acceptEdits", "--allowedTools", allow]
        else:
            c += ["-y"]
        c += ["--disallowedTools", deny]
        if model:
            c += ["--model", model]
        if effort and e == "codebuddy":   # no --effort flag; CodeBuddy reads it from settings
            c += ["--settings", json.dumps({"reasoningEffort": effort})]
        if session:
            c += ["--resume", session]
        return c, prompt
    if e == "grok":
        pf = os.path.join(turn_dir, "prompt.txt")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt)
        c = [b, "--prompt-file", pf, "--output-format", "streaming-messages-json", "--cwd", wd, "--no-auto-update",
             "--always-approve"]
        for cmd in DENY_SHELL:
            c += ["--deny", f"Bash({cmd}*)"]
        for p in DENY_PATHS:
            c += ["--deny", f"Edit({p})", "--deny", f"Write({p})"]
        if model:
            c += ["-m", model]
        if effort:
            c += ["--effort", effort]
        if session:
            c += ["-r", session]
        return c, None
    raise ValueError(f"unknown engine {e}")


# ------------------------------------------------------------------------------------------------------- parsing
def tool_arg(inp) -> str:
    if not isinstance(inp, dict):
        return clip(inp, 300)
    for k in ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description", "notebook_path",
              "target_file", "TargetFile", "CommandLine"):
        if inp.get(k):
            v = inp[k]
            return clip(" ".join(v) if isinstance(v, list) else v, 300)
    return clip(json.dumps(inp, ensure_ascii=False), 300)


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    return "" if content is None else json.dumps(content, ensure_ascii=False)


class Turn:
    """What one CLI call said and did, read from its JSON lines (Messages stream or Codex exec events)."""

    def __init__(self, engine: str, record):
        self.engine, self.record = engine, record
        self.session = ""
        self.texts: list = []
        self.final = None
        self.failed = False
        self.error = ""
        self.usage = None
        self.denied: list = []
        self.pending: dict = {}
        self.tools = 0
        self.got_result = False

    def say(self, text: str):
        text = (text or "").strip()
        if text:
            self.texts.append(text)
            self.record({"kind": "say", "text": clip(text, 1200)})

    def tool(self, name, arg, out_="", err=""):
        rec = {"kind": "tool", "tool": name, "arg": clip(arg, 300), "out": clip(out_, MAX_TOOL_OUT)}
        if err:
            rec["error"] = clip(err, 300)
        self.record(rec)
        self.tools += 1

    def line(self, raw: str):
        try:
            e = json.loads(raw)
        except ValueError:
            return
        if isinstance(e, dict):
            (self.codex if self.engine == "codex" else self.messages)(e)

    # ---- Claude Code / CodeBuddy / Grok (Messages stream-json)
    def messages(self, e: dict):
        t = e.get("type")
        sid = e.get("session_id") or e.get("sessionId")
        if sid:
            self.session = str(sid)
        if t == "assistant":
            for blk in (e.get("message") or {}).get("content") or []:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    self.say(blk.get("text"))
                elif blk.get("type") in ("tool_use", "server_tool_use"):
                    self.pending[blk.get("id") or str(len(self.pending))] = (blk.get("name"), tool_arg(blk.get("input")))
        elif t == "user":
            for blk in (e.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    name, arg = self.pending.pop(blk.get("tool_use_id"), ("?", ""))
                    txt = block_text(blk.get("content"))
                    err = txt if blk.get("is_error") else ""
                    if err and re.search(r"permission|not allowed|denied|requires approval", err, re.I):
                        self.denied.append({"action": "command" if name in ("Bash", "run_terminal_cmd") else "write_file",
                                            "tool": name, "target": arg, "outside_worktree": False,
                                            "why": clip(err, 200)})
                    self.tool(name, arg, "" if err else txt, err)
        elif t == "result":
            self.got_result = True
            r = e.get("result")
            if isinstance(r, str) and r.strip():
                self.final = r
            self.usage = e.get("usage") or self.usage
            if e.get("is_error") or str(e.get("subtype") or "success") != "success":
                self.failed = True
                errs = e.get("errors") or []
                self.error = clip("; ".join(str(x) for x in errs) or e.get("subtype") or (r if isinstance(r, str) else "")
                                  or "error", 400)
            for d in e.get("permission_denials") or []:
                self.denied.append({"action": "command" if d.get("tool_name") == "Bash" else "write_file",
                                    "tool": d.get("tool_name"), "target": tool_arg(d.get("tool_input")),
                                    "outside_worktree": False, "why": "the tool needs approval, and headless mode cannot ask"})
        elif t == "error":
            self.failed = True
            self.error = clip(e.get("message") or e.get("error") or "error", 400)

    # ---- Codex exec --json
    def codex(self, e: dict):
        t = e.get("type") or ""
        if t == "thread.started":
            self.session = str(e.get("thread_id") or self.session)
        elif t == "item.completed":
            it = e.get("item") or {}
            k = it.get("type") or it.get("item_type")
            if k in ("agent_message", "assistant_message"):
                self.say(it.get("text"))
            elif k == "command_execution":
                code = it.get("exit_code")
                fail = it.get("status") == "failed" or (code not in (None, 0))
                o = it.get("aggregated_output") or ""
                self.tool("shell", it.get("command"), o, (f"exit {code}: " + clip(o, 200)) if fail else "")
                if fail and re.search(r"sandbox|operation not permitted|permission denied|read-only file system", o, re.I):
                    self.denied.append({"action": "command", "tool": "shell", "target": clip(it.get("command"), 300),
                                        "outside_worktree": False, "why": "blocked by Codex's workspace-write sandbox"})
            elif k == "file_change":
                ch = it.get("changes") or []
                self.tool("edit", ", ".join(f"{c.get('kind', '')} {c.get('path', '')}".strip() for c in ch[:8]),
                          "", "" if it.get("status") != "failed" else "patch failed")
            elif k == "mcp_tool_call":
                self.tool(f"{it.get('server')}.{it.get('tool')}", tool_arg(it.get("arguments")), "",
                          (it.get("error") or {}).get("message", "") if isinstance(it.get("error"), dict) else "")
            elif k == "web_search":
                self.tool("web_search", it.get("query"))
            elif k == "error":
                self.error = clip(it.get("message"), 400)
        elif t == "turn.completed":
            self.got_result = True
            self.usage = e.get("usage")
        elif t == "turn.failed":
            self.got_result = True
            self.failed = True
            self.error = clip((e.get("error") or {}).get("message") or "turn failed", 400)
        elif t == "error":
            self.failed = True
            self.error = clip(e.get("message") or "error", 400)
        elif "msg" in e:   # older codex exec --json (one event per line under "msg")
            m = e.get("msg") or {}
            mt = m.get("type")
            if mt == "session_configured":
                self.session = str(m.get("session_id") or self.session)
            elif mt == "agent_message":
                self.say(m.get("message"))
            elif mt == "exec_command_end":
                self.tool("shell", " ".join(m.get("command") or []) if isinstance(m.get("command"), list) else m.get("command"),
                          m.get("aggregated_output") or m.get("stdout") or "",
                          f"exit {m.get('exit_code')}" if m.get("exit_code") not in (None, 0) else "")
            elif mt == "task_complete":
                self.got_result = True
                if m.get("last_agent_message"):
                    self.final = m["last_agent_message"]
            elif mt == "error":
                self.failed = True
                self.error = clip(m.get("message"), 400)

    def response(self) -> str:
        if self.final and str(self.final).strip():
            return str(self.final).strip()
        return self.texts[-1] if self.texts else ""


# ------------------------------------------------------------------------------------------------------- check / models
# v3.9.83: where each CLI keeps its login (files under ~, or an API key in the environment). None = we don't know.
CREDS = {
    "codex": (["~/.codex/auth.json"], ["OPENAI_API_KEY", "CODEX_API_KEY"]),
    "claude": (["~/.claude/.credentials.json"], ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]),
    "codebuddy": (["~/.codebuddy/auth.json", "~/.codebuddy/credentials.json", "~/.codebuddy/.credentials.json"],
                  ["CODEBUDDY_API_KEY"]),
    "grok": (["~/.grok/auth.json", "~/.grok/credentials.json", "~/.config/grok/auth.json"], ["XAI_API_KEY", "GROK_API_KEY"]),
}


def has_creds(engine: str):
    """True: a login file or key is there. False: none of the known places has one. Claude also logs in through
    ~/.claude.json (oauthAccount)."""
    spec = CREDS.get(engine)
    if not spec:
        return None
    files, keys = spec
    if any(os.environ.get(k) for k in keys):
        return True
    for f in files:
        f = os.path.expanduser(f)
        try:
            if os.path.isfile(f) and os.path.getsize(f) > 2:
                return True
        except OSError:
            pass
    if engine in ("codebuddy", "grok"):   # their login file layout isn't pinned: no folder = never logged in
        homes = {"codebuddy": ["~/.codebuddy"], "grok": ["~/.grok", "~/.config/grok"]}[engine]
        return None if any(os.path.isdir(os.path.expanduser(h)) for h in homes) else False
    if engine == "claude":
        try:
            with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as fh:
                if '"oauthAccount"' in fh.read():
                    return True
        except OSError:
            pass
    return False


def op_check(engine: str, probe: bool):
    if engine not in ENGINES:
        out({"ok": False, "error": f"unknown engine {engine}"})
    b = find_bin(engine)
    if not b:
        out({"ok": False, "installed": False, "engine": engine, "bridge": CLIV,
             "error": f"{engine} not found on PATH", "install": ENGINES[engine]["install"]})
    ver = ""
    try:
        r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=30, env=run_env(),
                           stdin=subprocess.DEVNULL)
        ver = ((r.stdout or r.stderr).strip().splitlines() or [""])[0][:80]
    except Exception as e:  # noqa: BLE001
        ver = f"? ({type(e).__name__})"
    res = {"ok": True, "installed": True, "engine": engine, "path": b, "version": ver, "bridge": CLIV,
           "git": bool(shutil.which("git")), "creds": has_creds(engine)}
    if probe:
        d = os.path.join(ROOT, "probe")
        os.makedirs(d, exist_ok=True)
        job = {"engine": engine, "bin": b, "workdir": d}
        argv, stdin = build_cmd(job, "Reply with exactly: ok", "", d)
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=150, cwd=d, env=run_env(),
                               stdin=None if stdin is not None else subprocess.DEVNULL)
            t = Turn(engine, lambda rec: None)
            for line in (r.stdout or "").splitlines():
                t.line(line)
            res["signed_in"] = bool(t.got_result and not t.failed and r.returncode == 0)
            if not res["signed_in"]:
                res["ok"] = False
                res["error"] = clip(t.error or r.stderr or r.stdout or "no response", 500)
        except Exception as e:  # noqa: BLE001
            res["ok"], res["signed_in"], res["error"] = False, False, f"{type(e).__name__}: {e}"[:300]
    out(res)


def parse_models(stdout: str) -> list:
    txt = (stdout or "").strip()
    try:
        d = json.loads(txt)
        lst = (d.get("models") or d.get("data") or []) if isinstance(d, dict) else d
        if isinstance(lst, dict):
            lst = lst.get("models") or []
        res = []
        for m in lst or []:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("slug") or m.get("model") or m.get("name")
                if mid:
                    res.append({"id": str(mid), "label": str(m.get("label") or m.get("display_name") or m.get("name") or mid)})
            elif isinstance(m, str):
                res.append({"id": m, "label": m})
        return res
    except ValueError:
        pass
    res = []
    for line in txt.splitlines():
        m = re.match(r"^\s*[*>-]?\s*([a-z][a-z0-9._:/-]+)(?:\s{2,}|\t|\s+-\s+)?(.*)$", line)
        if m and re.search(r"\d|opus|sonnet|haiku|grok|gpt|codex|claude|gemini|deepseek|kimi|qwen|glm|hunyuan", m.group(1)):
            res.append({"id": m.group(1), "label": (m.group(2) or m.group(1)).strip() or m.group(1)})
    return res[:60]


def codex_rows(d) -> list:
    """Codex's model catalog ({models:[{slug, display_name, visibility, supported_reasoning_levels:[{effort}]}]}):
    the models its picker shows, each with the strengths it takes."""
    lst = d.get("models") if isinstance(d, dict) else d
    res = []
    for m in lst or []:
        if not isinstance(m, dict) or not m.get("slug") or m.get("visibility") in ("hide", "hidden"):
            continue
        effs = [str(e.get("effort") if isinstance(e, dict) else e) for e in (m.get("supported_reasoning_levels") or [])]
        res.append({"id": str(m["slug"]), "label": str(m.get("display_name") or m["slug"]), "efforts": [e for e in effs if e]})
    return res[:40]


def codex_models(b: str) -> list:
    """`codex debug models` (this login's catalog, no model call), else the cache Codex keeps, else the bundled list."""
    for args in ([b, "debug", "models"], [b, "debug", "models", "--bundled"]):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=30, env=run_env(), stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                rows = codex_rows(json.loads(r.stdout))
                if rows:
                    return rows
        except Exception:  # noqa: BLE001
            pass
        if args[-1] == "models":
            home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
            rows = codex_rows(read_json(os.path.join(home, "models_cache.json"), {}) or {})
            if rows:
                return rows
    return []


def config_models(engine: str) -> list:
    """Models named in the CLI's own config file (the default, profiles)."""
    files = {"codex": "~/.codex/config.toml", "grok": "~/.grok/config.toml"}.get(engine)
    res = []
    if files:
        try:
            with open(os.path.expanduser(files), encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'^\s*model\s*=\s*"([^"]+)"', line)
                    if m and m.group(1) not in [x["id"] for x in res]:
                        res.append({"id": m.group(1), "label": m.group(1) + "（配置里的）"})
        except OSError:
            pass
    if engine == "codebuddy":
        s = read_json(os.path.expanduser("~/.codebuddy/settings.json"), {}) or {}
        if s.get("model"):
            res.append({"id": str(s["model"]), "label": str(s["model"]) + "（配置里的）"})
        # ~/.codebuddy/models.json: custom models, and availableModels (the ids the account wants shown)
        mj = read_json(os.path.expanduser("~/.codebuddy/models.json"), {}) or {}
        for m in (mj.get("models") or []) if isinstance(mj, dict) else []:
            if isinstance(m, dict) and m.get("id") and m["id"] not in [x["id"] for x in res]:
                res.append({"id": str(m["id"]), "label": str(m.get("name") or m["id"]) + "（models.json）"})
        for i in (mj.get("availableModels") or []) if isinstance(mj, dict) else []:
            if i and str(i) not in [x["id"] for x in res]:
                res.append({"id": str(i), "label": str(i)})
    return res


def op_models(engine: str):
    if engine not in ENGINES:
        out({"ok": False, "error": f"unknown engine {engine}"})
    b = find_bin(engine)
    if not b:
        out({"ok": False, "installed": False, "error": f"{engine} not found on PATH", "install": ENGINES[engine]["install"]})
    ms: list = []
    if engine == "codex":
        ms = codex_models(b)
    elif engine == "grok":   # `grok models` lists them; CodeBuddy has no such subcommand (`codebuddy models` would start
        for args in ([b, "models", "--json"], [b, "models"]):   # a chat with the prompt "models"), so it is not run
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=25, env=run_env(), stdin=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                continue
            if r.returncode == 0:
                ms = parse_models(r.stdout)
                if ms:
                    break
    seen = {m["id"] for m in ms}
    for m in config_models(engine) + [{"id": i, "label": l} for i, l in STATIC_MODELS.get(engine, [])]:
        if m["id"] not in seen:
            ms.append(m)
            seen.add(m["id"])
    for x in (os.environ.get(f"AIW_{engine.upper()}_MODELS") or "").split(","):
        if x.strip() and x.strip() not in seen:
            ms.append({"id": x.strip(), "label": x.strip()})
            seen.add(x.strip())
    groups = [{"id": m["id"], "label": m["label"], "efforts": m.get("efforts") or [], "slugs": {}, "plain": True} for m in ms]
    out({"ok": True, "engine": engine, "models": ms, "groups": groups, "efforts": ENGINES[engine]["efforts"]})


# ------------------------------------------------------------------------------------------------------- start
def op_start(job_b64: str):
    try:
        job = json.loads(base64.b64decode(job_b64).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        out({"ok": False, "error": f"bad job: {e}"}, 2)
    engine = job.get("engine")
    if engine not in ENGINES:
        out({"ok": False, "error": f"unknown engine {engine!r}"})
    b = find_bin(engine)
    if not b:
        out({"ok": False, "installed": False, "engine": engine, "error": f"{engine} not found on PATH",
             "install": ENGINES[engine]["install"]})
    wd = os.path.abspath(os.path.expanduser(job.get("workdir") or f"~/workspace/{engine}-chat"))
    os.makedirs(wd, exist_ok=True)
    jid = job.get("id") or (f"{engine[:5]}-" + time.strftime("%m%d%H%M") + "-" + uuid.uuid4().hex[:4])
    d = jdir(jid)
    if os.path.exists(d):
        out({"ok": False, "error": f"job {jid} exists"})
    os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
    job.update({"id": jid, "bin": b, "workdir": wd, "worktree": wd, "branch": "", "base": "", "created": time.time()})
    write_json(os.path.join(d, "job.json"), job)
    write_json(os.path.join(d, "state.json"), {"status": "starting", "turn": 0, "events": 0, "ts": time.time(),
                                               "conversation_id": job.get("session") or ""})
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
    out({"ok": st.get("status") not in ("error",), "job": jid, "engine": engine, "worktree": wd, "branch": "",
         "pid": p.pid, "status": st.get("status"), "error": st.get("error"), "plain": True})


# ------------------------------------------------------------------------------------------------------- daemon
class Daemon:
    def __init__(self, jid: str):
        self.jid, self.d = jid, jdir(jid)
        self.job = read_json(os.path.join(self.d, "job.json"), {})
        self.state = read_json(os.path.join(self.d, "state.json"), {}) or {}
        self.ev = open(os.path.join(self.d, "events.jsonl"), "a", encoding="utf-8")

    def save(self, **kw):
        self.state.update(kw, ts=time.time(), pid=os.getpid())
        write_json(os.path.join(self.d, "state.json"), self.state)

    def record(self, rec: dict):
        self.state["events"] = int(self.state.get("events") or 0) + 1
        rec["n"] = self.state["events"]
        rec["t"] = round(time.time(), 2)
        self.ev.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.ev.flush()
        if rec.get("kind") == "tool":
            self.state["tools"] = int(self.state.get("tools") or 0) + 1
        self.save()

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

    def stopped(self) -> bool:
        return os.path.exists(os.path.join(self.d, "STOP"))

    def turn(self, text: str) -> str:
        """Run one CLI call. Returns the end reason when the daemon must stop (else "")."""
        j = self.job
        n = int(self.state.get("turn") or 0) + 1
        tdir = os.path.join(self.d, f"turn-{n}")
        os.makedirs(tdir, exist_ok=True)
        session = str(self.state.get("conversation_id") or j.get("session") or "")
        argv, stdin_text = build_cmd(j, clip(text, MAX_PROMPT), session, tdir)
        self.record({"kind": "prompt", "turn": n, "text": clip(text, 800), "resume": bool(session)})
        self.save(status="running", turn=n, response=None, denied=[], last_error=None, error=None)
        err_path = os.path.join(self.d, f"turn-{n}.stderr")
        errf = open(err_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(argv, cwd=j["workdir"], stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1, env=run_env(),
                                    start_new_session=True)
        except Exception as e:  # noqa: BLE001
            self.save(status="turn_failed", error=f"cannot start {j['engine']}: {e}", response="")
            return ""
        self.save(cli_pid=proc.pid)
        if stdin_text is not None:
            def feed():
                try:
                    proc.stdin.write(stdin_text)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            threading.Thread(target=feed, daemon=True).start()
        t = Turn(j["engine"], self.record)
        lines: "queue.Queue[str | None]" = queue.Queue()

        def pump():
            for line in iter(proc.stdout.readline, ""):
                lines.put(line)
            lines.put(None)
        threading.Thread(target=pump, daemon=True).start()
        idle_s = float(j.get("idle_min") or 20) * 60
        last_io, reason = time.time(), ""
        while True:
            if self.stopped():
                reason = "stopped"
            elif time.time() - last_io > idle_s:
                reason = "idle"
            if reason:
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                    proc.wait(15)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                break
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None and lines.empty():
                    break
                continue
            if line is None:
                break
            last_io = time.time()
            t.line(line)
            if t.session and t.session != self.state.get("conversation_id"):
                self.save(conversation_id=t.session)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
        while not lines.empty():
            line = lines.get_nowait()
            if line:
                t.line(line)
        errf.close()
        try:
            with open(err_path, encoding="utf-8", errors="replace") as f:
                err = f.read()[-4000:]
        except OSError:
            err = ""
        text_out = t.response()
        auth = ""
        if (t.failed or not t.got_result) and AUTH_RE.search((t.error or "") + "\n" + err):
            auth = clip((t.error or err).strip(), 300)
        if t.session:
            self.save(conversation_id=t.session)
        ok = t.got_result and not t.failed and proc.returncode in (0, None)
        blocked = bool(t.denied) and not text_out
        if reason == "stopped":
            status = "stopped"
        elif blocked:
            status = "blocked"
            d0 = t.denied[-1]
            text_out = (f"({j['engine']} produced no report: its step {d0.get('tool')} {d0.get('target') or ''} "
                        f"was denied ({d0.get('why')}))")
        else:
            status = "turn_done" if ok else "turn_failed"
        error = "" if ok else (t.error or (f"{j['engine']} exited with code {proc.returncode}: " + clip(err.strip(), 300)
                                            if proc.returncode else "") or ("no reply: the turn went quiet for "
                                                                             f"{int(idle_s / 60)} min" if reason == "idle" else ""))
        self.record({"kind": "result", "status": status, "text": clip(text_out, MAX_RESP), "error": error})
        self.save(status=status, last_status="SUCCESS" if ok else "ERROR", last_error=error or None,
                  response=clip(text_out, MAX_RESP), usage=t.usage, denied=t.denied[-20:], auth_error=auth or None,
                  exit_code=proc.returncode)
        return "stopped" if reason == "stopped" else ""

    def run(self):
        j = self.job
        keep_s = float(j.get("keep_min") or 120) * 60
        total_s = float(j.get("timeout_min") or 24 * 60) * 60
        max_turns = int(j.get("max_turns") or 1000)
        started, idle_since = time.time(), time.time()
        self.save(status="starting", settings={})
        end = ""
        while not end:
            if self.stopped():
                end = "stopped"
                break
            if time.time() - started > total_s:
                end = "timeout"
                break
            text = self.next_inbox()
            if text is None:
                if time.time() - idle_since > keep_s:
                    end = "expired"
                    break
                if self.state.get("status") == "starting":
                    self.save(status="turn_done")
                time.sleep(0.5)
                continue
            if int(self.state.get("turn") or 0) >= max_turns:
                self.record({"kind": "refused", "text": f"max_turns {max_turns} reached"})
                self.save(status="turn_done", note=f"max_turns {max_turns} reached; start a new job")
                continue
            end = self.turn(text)
            idle_since = time.time()
        final = {"stopped": "stopped", "timeout": "timed_out", "expired": "ended"}.get(end, "ended")
        self.record({"kind": "end", "reason": end})
        self.save(status=final, end_reason=end)


# ------------------------------------------------------------------------------------------------------- wait etc.
def changes(job: dict, full: bool = False) -> dict:
    wt = job.get("worktree") or job.get("workdir")
    if not wt or git(["rev-parse", "--git-dir"], wt).returncode != 0:
        return {"files": [], "stat": "not a git repository", "commits": []}
    names = git(["status", "--short"], wt).stdout.strip().splitlines()
    r = {"files": names[:80], "stat": f"{len(names)} changed" if names else "no changes", "commits": []}
    if full:
        r["diff"] = clip(git(["diff"], wt, 120).stdout, 60000)
    return r


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
    res = {"ok": True, "job": jid, "engine": job.get("engine"), "status": st.get("status"), "turn": st.get("turn"),
           "conversation_id": st.get("conversation_id"), "cursor": int(st.get("events") or cursor),
           "tools_total": st.get("tools"), "tools_new": len(tools),
           "tools": [{k: e.get(k) for k in ("tool", "arg", "error", "out") if e.get(k)} for e in tools[-25:]],
           "said": [e.get("text") for e in evs if e.get("kind") == "say"][-3:],
           "response": st.get("response"), "last_status": st.get("last_status"),
           "error": st.get("error") or st.get("last_error"), "denied": st.get("denied") or [], "usage": st.get("usage"),
           "worktree": job.get("workdir"), "branch": "", "note": st.get("note"), "auth_error": st.get("auth_error")}
    if st.get("status") in ("running", "starting") and st.get("pid") and not alive(st.get("pid")):
        res["status"] = "error"
        res["error"] = "the bridge daemon is gone (computer restarted?)"
    if with_diff and job.get("workdir") and os.path.isdir(job["workdir"]):
        res["changes"] = changes(job)
    return res


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
        if st.get("pid") and not alive(st.get("pid")):
            break
        time.sleep(1)
    out(summary(jid, cursor))


def op_send(jid: str, text_b64: str):
    d = jdir(jid)
    st = read_json(os.path.join(d, "state.json"), {}) or {}
    if st.get("status") not in ("running", "starting", "turn_done", "turn_failed", "blocked") or not alive(st.get("pid")):
        out({"ok": False, "job": jid, "status": st.get("status"), "conversation_id": st.get("conversation_id"),
             "error": "the session's daemon has ended; start a new job with the session id"})
    text = base64.b64decode(text_b64).decode("utf-8")
    with open(os.path.join(d, "inbox", f"{time.time():.6f}.json"), "w", encoding="utf-8") as f:
        json.dump({"text": text}, f, ensure_ascii=False)
    # the daemon flips to running when it picks the message up; mark it now so a wait right after this does not
    # return the previous turn's result
    st["status"] = "running"
    write_json(os.path.join(d, "state.json"), {**st, "ts": time.time()})
    out({"ok": True, "job": jid, "queued": True, "status": "running"})


def op_stop(jid: str):
    d = jdir(jid)
    if not os.path.isdir(d):
        out({"ok": False, "error": f"no job {jid}"})
    open(os.path.join(d, "STOP"), "w").close()
    for _ in range(40):
        st = read_json(os.path.join(d, "state.json"), {}) or {}
        if not alive(st.get("pid")):
            break
        time.sleep(0.5)
    out(summary(jid, 10 ** 9))


def op_diff(jid: str, full: bool):
    job = read_json(os.path.join(jdir(jid), "job.json"), None)
    if not job:
        out({"ok": False, "error": f"no job {jid}"})
    out({"ok": True, "job": jid, "worktree": job.get("workdir"), "branch": "", "base": "", **changes(job, full)})


def op_list():
    rows = []
    for n in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        st = read_json(os.path.join(JOBS, n, "state.json"), {}) or {}
        job = read_json(os.path.join(JOBS, n, "job.json"), {}) or {}
        rows.append({"job": n, "engine": job.get("engine"), "status": st.get("status"), "turn": st.get("turn"),
                     "alive": alive(st.get("pid")), "workdir": job.get("workdir")})
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
        op_check(a.job or "", a.probe)
    elif a.op == "models":
        op_models(a.job or "")
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
