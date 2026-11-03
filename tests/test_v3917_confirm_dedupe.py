"""v3.9.17 item 9(c): read-only remote diagnosis without cards, one card per host for the rest."""
import json
from pathlib import Path

from tool_router.policy import PolicyEngine, RiskLevel
from tool_router import remote as RM

FIX = json.loads((Path(__file__).parent / "fixtures" / "task833_replay.json").read_text())


def test_task833_commands_need_no_card():
    p = PolicyEngine()
    cards = [s["step"] for s in FIX["steps"] if s["tool"] == "shell"
             and p.check("shell.exec", {"command": s["args"]["command"]}).needs_confirm]
    assert cards == [], cards          # was 9 cards in 14 steps


def test_password_in_code_is_not_a_sensitive_file():
    p = PolicyEngine()
    for c in ['python3 -c "c.connect(h, username=u, password=PWD)"', "echo password={{task_cred_1}} | wc -c",
              "grep -n password config.yml"]:
        assert p.check("shell.exec", {"command": c}).reason != "shell touches sensitive file", c
    for c in ["cat ~/.ssh/id_rsa", "cat app/.env", "cat passwords.txt", "cp server.pem /tmp"]:
        assert p.check("shell.exec", {"command": c}).needs_confirm, c
    assert p.check("shell.exec", {"command": "cat app/.env"}).reason == "shell touches sensitive file"


def test_remote_writes_still_ask_and_unsure_is_scoped_to_the_host():
    p = PolicyEngine()
    d = p.check("shell.exec", {"command": 'ssh root@10.0.0.2 "docker restart clipboard-online"'})
    assert d.level == RiskLevel.L3 and d.reason == "shell: remote change" and not d.scope
    d = p.check("shell.exec", {"command": "ssh root@10.0.0.2"})
    assert d.needs_confirm and d.scope == "remote_diag:10.0.0.2"
    d = p.check("shell.exec", {"command": 'ssh root@10.0.0.2 "docker stats --no-stream; cat /proc/loadavg"'})
    assert d.level == RiskLevel.L2


def test_read_only_classifier():
    assert RM.read_only("docker stats --no-stream --format '{{.Name}}' | sort -k2 -rn | head")
    assert RM.read_only("docker exec clipboard-online ps -ef")
    assert RM.read_only("docker logs --tail 100 x 2>&1 | tail -20")
    assert not RM.read_only("docker exec x rm -rf /tmp/a")
    assert not RM.read_only("docker compose up -d")
    assert not RM.read_only("curl -X POST http://x")


from test_v11_features import stack, wait_task  # noqa: E402,F401  (fixture)
from test_approval_ux import _wait  # noqa: E402


async def test_one_card_per_host_for_unclear_remote_commands(stack):
    client, backend, scripts, app, providers, _ = stack
    scripts.append([("shell", {"command": "ssh -V 2>&1; true"}), ("shell", {"command": "ssh -V 2>&1; echo second"}),
                    ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "看看 ssh 客户端版本"})).json()
    sid = r["session_id"]
    s = await _wait(client, sid, "pending_confirm")
    assert "只读诊断" in (s["pending_confirm"]["summary"].get("scope_note") or "")
    await client.post("/api/agent/confirm", json={"session_id": sid, "approve": True})
    t = await wait_task(client, r["task"]["id"])
    assert t["status"] == "succeeded", t
    evs = (await client.get(f"/api/sessions/{sid}")).json()["events"]
    assert len([e for e in evs if e["type"] == "agent.confirm_required"]) == 1
    ans = [(e["data"]["approve"], e["data"]["by"]) for e in evs if e["type"] == "agent.confirm_answered"]
    assert ans == [(True, "user"), (True, "scope")]
    res = [e["data"] for e in evs if e["type"] == "agent.action_result" and e["data"].get("tool") == "shell"]
    assert res and all("tool_ms" in d and "wait_ms" in d for d in res)
    assert res[0]["wait_ms"] >= 0
