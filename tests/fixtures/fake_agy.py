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
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 0, "state": "DONE", "step_type": "user_input"}})
    with open("hello.py", "a") as f:
        f.write(f"print('turn {turn}')\n")
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE", "step_type": "tool", "tool_name": "write_to_file",
          "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "hello.py"}, "output": "ok"}}})
    sys.stderr.write("permission denied: run_command(git push) requires approval; add it to permissions.allow\n"); sys.stderr.flush()
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": f"Done turn {turn}: edited hello.py\n"}})
    emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": f"Done turn {turn}: edited hello.py\n", "num_turns": turn,
          "usage": {"total_tokens": 100 * turn}}})
