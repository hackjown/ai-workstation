"""v3.9.82: only 「@引擎 …」 talks to an engine from the main chat; groups mark who owns which file, warn when a later
step changes another member's files, give cloud reviewers read-only copies, and run the 管家's checks with bash."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "tests"))
from aiw_api import bot_groups as G  # noqa: E402
from test_v3979_group_steps import FakeBots  # noqa: E402


def test_only_at_talks_to_an_engine():
    assert G.parse_dispatch("查询agy的ldok开发进度") is None
    assert G.parse_dispatch("让 agy 查一下 ldok") is None
    assert G.parse_dispatch("agy 在做什么") is None
    p = G.parse_dispatch("@agy 查一下 ldok 的进度")
    assert p["bots"] == ["agy"] and p["mode"] == "chat" and p["task"] == "查一下 ldok 的进度"
    p = G.parse_dispatch("＠agy @codex 一起做登录页")
    assert p["bots"] == ["agy", "codex"] and p["mode"] == "new"
    assert G.parse_dispatch("@agy 继续 ldok 任务")["mode"] == "continue"
    assert G.parse_dispatch("@agy 新建任务，做天气")["mode"] == "new"
    assert G.parse_dispatch("@agyx 你好") is None


def test_chat_no_longer_offers_engine_task():
    src = (ROOT / "services/api/aiw_api/chat.py").read_text(encoding="utf-8")
    assert 'out["engine_task"]' not in src and "只查不下指令" in src


class ScopeBots(FakeBots):
    def __init__(self, tmp, diffs):
        super().__init__(tmp)
        self.diffs = diffs

    async def sh(self, mid, cmd, timeout):
        self.cmds.append(cmd)
        if "rev-parse HEAD" in cmd:
            return "a" * 40
        if "diff --name-only" in cmd:
            return self.diffs.pop(0) if self.diffs else ""
        if "=====" in cmd:
            return "===== mdstat.py\nprint('hi')\n"
        return "EXIT 0"


def test_scope_warning_and_reviewer_copy(tmp_path):
    async def run():
        ids = ["agy", "codebuddy", "muse"]
        b = ScopeBots(tmp_path, ["SPEC.md\n", "tests/test_mdstat.py\n", "mdstat.py\nSPEC.md\ntests/test_mdstat.py\n"])
        b.convs = {f"c-{x}": {"id": f"c-{x}", "bot": x, "grp": "bg-1"} for x in ids}
        g = G.Groups(b)
        mem = [{"bot": x, "branch": f"aiw/bg-1/{x}" if x != "muse" else "", "worktree": f"~/w/{x}" if x != "muse" else "",
                "conv": f"c-{x}", **({"cloud": True} if x == "muse" else {})} for x in ids]
        b.store._x("INSERT INTO bot_groups (id, title, machine_id, repo, base, status, auto_merge, members, pending, "
                   "fixes, created_at, updated_at) VALUES ('bg-1','t','vm1','','main','idle',1,?,'[]',0,0,0)",
                   (json.dumps(mem),))
        msg = ("@管家 按顺序做，前一步完成后再交给下一步。\n第 1 步 @agy 写 SPEC.md\n第 2 步 @cbc 写测试\n"
               "第 3 步 @muse 审查\n第 4 步 @agy 写 mdstat.py\n第 5 步 @管家 验收，运行 python3 -m pytest -q")

        async def settle():
            for _ in range(60):
                await asyncio.sleep(0.01)
        await g.send("bg-1", msg)
        await settle()
        for i, bot in enumerate(["agy", "codebuddy", "muse", "agy"]):
            g._on_bot("message", {"role": "bot", "id": f"m{i}", "conv": f"c-{bot}", "text": f"{bot} 做完了",
                                  "data": {"status": "turn_done"}})
            await settle()
            if bot == "codebuddy":
                assert b.sent[-1][0] == "c-muse" and "只读副本" in b.sent[-1][1] and "print('hi')" in b.sent[-1][1]
        warns = [m for m in g.messages("bg-1") if (m.get("data") or {}).get("scope")]
        assert len(warns) == 1 and "CodeBuddy" in warns[0]["text"] and "tests/test_mdstat.py" in warns[0]["text"]
        assert "SPEC.md" not in warns[0]["text"]   # agy's own earlier file is not a warning
        assert any(c.startswith("bash -c ") and "PIPESTATUS" in c for c in b.cmds)
    asyncio.run(run())


def test_brief_and_plan_rules():
    assert "不改规范和测试" in G.PLAN and "退出码" in G.PLAN
    g = G.Groups.__new__(G.Groups)
    gr = {"title": "t", "base": "main", "members": [{"bot": "agy", "branch": "b", "worktree": "w"}]}
    assert "前面步骤由其他成员负责的文件" in g._brief(gr, gr["members"][0], "写实现")
