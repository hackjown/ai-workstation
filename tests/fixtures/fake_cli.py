#!/usr/bin/env python3
"""v3.9.60: a stand-in for codex / claude / codebuddy / grok in headless mode.

codex exec --json            -> thread.started / item.completed / turn.completed (prompt is the last argument)
claude|codebuddy -p           -> Messages stream-json (prompt on stdin)
grok --prompt-file F          -> Messages stream-json (streaming-messages-json)
One process per turn; the session id comes back on the next turn (resume / --resume / -r).
"""
import json, os, sys, uuid

a = sys.argv[1:]
if "--version" in a:
    print("fake-cli 1.0.0"); sys.exit(0)
if a[:1] == ["models"]:
    print(json.dumps({"models": [{"id": "grok-4.6", "label": "Grok 4.6"}, {"id": "grok-code-fast-2", "label": "Grok Code Fast 2"}]}))
    sys.exit(0)
with open(os.environ.get("FAKE_CLI_ARGS", "/dev/null"), "a") as f:
    f.write(json.dumps(a) + "\n")


def val(*flags):
    for fl in flags:
        if fl in a:
            i = a.index(fl)
            return a[i + 1] if i + 1 < len(a) else ""
    return ""


if a[:1] == ["exec"]:
    kind = "codex"
    prompt = a[-1]
    session = a[a.index("resume") + 1] if "resume" in a else ""
    cwd = val("-C") or os.getcwd()
elif "--prompt-file" in a:
    kind = "grok"
    prompt = open(val("--prompt-file"), encoding="utf-8").read()
    session = val("-r", "--resume")
    cwd = val("--cwd") or os.getcwd()
else:
    kind = "codebuddy" if "-y" in a else "claude"
    prompt = sys.stdin.read()
    session = val("--resume", "-r")
    cwd = os.getcwd()

if "NOLOGIN" in prompt:
    sys.stderr.write("Error: Not logged in. Please run /login\n"); sys.exit(1)
sid = session or str(uuid.uuid4())
sdir = os.environ.get("FAKE_CLI_STATE") or cwd          # v3.9.61: keep the fake's own files out of git worktrees
os.makedirs(sdir, exist_ok=True)
state_f = os.path.join(sdir, f".fake_sessions_{kind}.json")
target = f"{kind}.py" if "OWNFILE" in prompt else "hello.py"   # v3.9.61: OWNFILE: each engine edits its own file
try:
    st = json.load(open(state_f))
except (OSError, ValueError):
    st = {}
turn = st.get(sid, 0) + 1
st[sid] = turn
json.dump(st, open(state_f, "w"))
with open(os.path.join(sdir, "seen_prompts.log"), "a") as f:
    f.write(f"{kind} {sid} {turn}: {prompt}\n----\n")


def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()


if kind == "codex":
    emit({"type": "thread.started", "thread_id": sid})
    emit({"type": "turn.started"})
    emit({"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "thinking"}})
    if "SANDBOX" in prompt:
        emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "bash -lc 'touch /etc/x'",
              "aggregated_output": "touch: cannot touch '/etc/x': Read-only file system", "exit_code": 1, "status": "failed"}})
    with open(os.path.join(cwd, target), "a") as f:
        f.write(f"print('{kind} turn {turn}')\n")
    emit({"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "bash -lc ls",
          "aggregated_output": target + "\n", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "i3", "type": "file_change", "changes": [{"path": target, "kind": "update"}], "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": f"Done turn {turn}: edited {target}"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    sys.exit(0)

emit({"type": "system", "subtype": "init", "session_id": sid, "model": val("--model", "-m") or "default", "cwd": cwd})
if "DENY" in prompt:
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push"}}]}, "session_id": sid})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
          "content": "Permission to use Bash with command git push has been denied."}]}, "session_id": sid})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": sid,
          "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "git push"}}]})
    sys.exit(0)
with open(os.path.join(cwd, target), "a") as f:
    f.write(f"print('{kind} turn {turn}')\n")
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Writing the file."},
      {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": os.path.join(cwd, target)}}]}, "session_id": sid})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}]}, "session_id": sid})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Done turn {turn}: edited {target}"}]}, "session_id": sid})
emit({"type": "result", "subtype": "success", "is_error": False, "result": f"Done turn {turn}: edited {target}",
      "session_id": sid, "usage": {"input_tokens": 10, "output_tokens": 5}})
