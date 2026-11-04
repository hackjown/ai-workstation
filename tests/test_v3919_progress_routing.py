"""v3.9.19: live plan ticks, done gates, dev routing, continue-on-cooldown, exclude-arg policy, iOS approval fold."""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in ("services/tool-router", "services/agent-runtime", "services/api"):
    sys.path.insert(0, str(ROOT / p))


def test_policy_exclude_args_not_sensitive():
    from tool_router.policy import PolicyEngine
    e = PolicyEngine()
    ok = ["cd ~/workspace/tasks/t1 && rm -rf pkg_out && rsync -a --exclude '.env' A/ pkg_out/A/",
          "cd ~/workspace/p && zip -qr o.zip . -x '.git/*' '.env' && ls -lh o.zip",
          "find . -type f ! -name '.env' | head"]
    for c in ok:
        assert int(e.check("shell.exec", {"command": c}).level) < 3, c
    for c in ["cat .env", "cd ~/workspace/a && rm -rf ../x", "cp ~/.ssh/id_rsa /tmp/"]:
        assert int(e.check("shell.exec", {"command": c}).level) >= 3, c


def test_dev_skill_routes_to_office():
    src = (ROOT / "services/api/aiw_api/roles.py").read_text()
    assert 'if "dev" in names' in src and 'return "office"' in src
    assert "async def continue_live" in src and "CONTINUE_ON_BACKUP" in src


def test_continue_uses_backup_in_main():
    src = (ROOT / "services/api/aiw_api/main.py").read_text()
    assert "_roles.continue_live(sessions, own)" in src
    assert "CONTINUE_ON_BACKUP" in src


def test_loop_plan_gate_and_known_issues():
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "拒绝 done：清单还有未勾选的步骤" in src
    assert "拒绝 done：开发任务缺少「已知问题」" in src
    assert "PLAN CHECK:" in src and "self._since_plan = 0" in src
    from agent_runtime.loop import _dev_task
    assert _dev_task("【技能：dev】\n项目：x")
    assert _dev_task("【技能：dev、frontend-design】做个页面")
    assert not _dev_task("帮我画一张图")


def test_planner_rules():
    src = (ROOT / "services/agent-runtime/agent_runtime/planner.py").read_text()
    assert "THE MOMENT it is finished" in src
    assert "do not search the disk" in src
    assert "2-3" in src and "mockups" in src
    assert "先给方案再动手" in (ROOT / "skills/frontend-design/SKILL.md").read_text()


def test_ios_approval_fold_and_plan_like_web():
    a = (ROOT / "apps/ios/Sources/Approval.swift").read_text()
    assert "更早的" in a and "items.count > 3" in a and "未允许" in a
    c = (ROOT / "apps/ios/Sources/ChatView.swift").read_text()
    assert "arrow.right.circle.fill" in c and "项\")" in c
    assert "if !live.plan.isEmpty && running {" not in c
    w = (ROOT / "apps/web/app.js").read_text()
    assert "plan-n" in w and re.search(r'case "agent.plan_update"|agent\.plan_update', w)
