#!/usr/bin/env python3
"""A stand-in for the Antigravity CLI: speaks its stream-json protocol (init / step_update / result)."""
import json, os, sys, time, uuid
if "--version" in sys.argv:
    print("agy 9.9.9-fake"); sys.exit(0)
if len(sys.argv) > 1 and sys.argv[1] == "models":   # v3.9.58: one slug per effort variant, like agy 1.1.15+
    ms = [("gemini-4-neon-high", "Gemini 4 Neon (High)"), ("gemini-4-neon-low", "Gemini 4 Neon (Low)"),
          ("gemini-3.8-flash", "Gemini 3.8 Flash"), ("claude-opus-5.5-thinking", "Claude Opus 5.5 (Thinking)")]
    if "--output-format" in sys.argv:
        print(json.dumps({"type": "models", "data": {"models": [{"id": i, "label": l} for i, l in ms]}}))
    else:
        print("\n".join(f"{i}\t{l}" for i, l in ms))
    sys.exit(0)
with open(os.environ.get("FAKE_AGY_ARGS", "/dev/null"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
if "-p" in sys.argv:
    print(json.dumps({"conversation_id": "c-probe", "status": "SUCCESS", "response": "ok\n"})); sys.exit(0)
cid = str(uuid.uuid4())
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
emit({"event": "init", "conversation_id": cid, "init": {"cwd": os.getcwd(), "tools": ["run_command"], "permission_mode": "request-review"}})
turn = 0
for line in sys.stdin:
    msg = json.loads(line)["message"]["content"]
    turn += 1
    if "HANG" in msg:
        time.sleep(3600)
    with open("seen_prompts.log", "a") as f:
        f.write(msg + "\n----\n")
    if "DENYCMD" in msg:    # v3.9.67: a command no allow rule covers; the tool step is announced but never DONE (a guess)
        cmd = msg.split("DENYCMD", 1)[1].strip().splitlines()[0]
        try:
            allow = json.load(open(os.path.expanduser("~/.gemini/antigravity-cli/settings.json")))["permissions"]["allow"]
        except Exception:
            allow = []
        import re as _re
        ok = "--dangerously-skip-permissions" in sys.argv or any(
            r == f"command({cmd})" or (r.startswith("command(regex:") and _re.fullmatch(r[len("command(regex:"):-1], cmd)) for r in allow)
        emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "WAITING", "step_type": "tool", "tool_name": "run_command",
              "tool_info": {"name": "run_command", "parameters": {"CommandLine": cmd}}}})
        if ok:
            emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
                  "tool_info": {"name": "run_command", "parameters": {"CommandLine": cmd}, "output": "converted"}}})
            emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": f"ran {cmd}\n", "num_turns": turn}})
            continue
        sys.stderr.write('jetski: no output produced \u2014 a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.\n'); sys.stderr.flush()
        time.sleep(0.3)
        emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "", "num_turns": turn}})
        continue
    if "DENYREAD" in msg:   # what the real agy 2026-11 did on a read outside its workspace (job agy-11200131-72a8)
        path = msg.split("DENYREAD", 1)[1].split()[0]
        emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": ""}})
        sys.stderr.write('jetski: no output produced \u2014 a tool required the "read_file" permission that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. read_file(<target>)). Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools.\n'); sys.stderr.flush()
        time.sleep(0.3)
        emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "read_file",
              "tool_info": {"name": "read_file", "parameters": {"Path": path, "StartLine": 1, "EndLine": 30}, "output": ""}}})
        emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "", "num_turns": turn}})
        continue
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 0, "state": "DONE", "step_type": "user_input"}})
    with open("hello.py", "a") as f:
        f.write(f"print('turn {turn}')\n")
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "write_to_file",
          "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "hello.py"}, "output": "ok"}}})
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": f"Done turn {turn}: edited hello.py\n"}})
    emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": f"Done turn {turn}: edited hello.py\n", "num_turns": turn,
          "usage": {"total_tokens": 100 * turn}}})
