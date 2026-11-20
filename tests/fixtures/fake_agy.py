#!/usr/bin/env python3
"""A stand-in for the Antigravity CLI: speaks its stream-json protocol (init / step_update / result)."""
import json, os, sys, time, uuid
if "--version" in sys.argv:
    print("agy 9.9.9-fake"); sys.exit(0)
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
