"""v3.9.79: a group follows the steps the user wrote (who and in what order), one after another, hands each member the
earlier results, and AIW 管家 does the step given to it."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bot_groups as G  # noqa: E402
from aiw_api.store import Store  # noqa: E402

IDS = ["agy", "codebuddy", "muse", "cue"]
MSG = """@管家 这是一次分工合作测试，请按下面的顺序把任务分给四个成员，前一步完成后再交给下一步，最后由你汇报。

目标：在工作文件夹 ~/workspace/aiw-team-test 里做一个 Python 命令行小工具 mdstat。

第 1 步 @cue（只调研，不写代码）
• 输出一份「验收标准」。

第 2 步 @agy（写实现）
• 按 SPEC.md 写 mdstat.py。

第 3 步 @cbc（写测试，不改 mdstat.py）
• 运行 python3 -m pytest -q。

第 4 步 @muse（审查和修复）
• 修复失败的测试。

第 5 步 @管家（验收和汇报）
• 自己运行一遍 python3 -m pytest -q 和 python3 mdstat.py tests/fixtures --top 5。

规则：Cue 只调研不碰代码；每个成员只做自己那一步。"""


def test_parse_steps_keeps_owner_and_order():
    st = G.parse_steps(MSG, IDS)
    assert [s["bot"] for s in st] == ["cue", "agy", "codebuddy", "muse", G.MANAGER_ID]
    assert "mdstat" in st[0]["task"] and "规则：Cue 只调研" in st[3]["task"]
    assert "第 4 步" not in st[2]["task"]


def test_parse_steps_needs_two():
    assert G.parse_steps("@cue 调研一下", IDS) is None
    assert G.parse_steps("帮我修 bug", IDS) is None


def test_parse_plan_steps():
    p = G.parse_plan(json.dumps({"steps": [{"bot": "cue", "task": "a"}, {"bot": "管家", "task": "b"},
                                           {"bot": "nobody", "task": "c"}], "report": "r"}), IDS)
    assert [s["bot"] for s in p["steps"]] == ["cue", G.MANAGER_ID]


class FakeBots:
    def __init__(self, tmp):
        self.store = Store(str(tmp / "t.db"))
        self.listeners, self.sent, self.cmds = [], [], []
        self.llm = None
        self.notifier = None
        self.convs = {f"c-{b}": {"id": f"c-{b}", "bot": b, "grp": "bg-1"} for b in IDS}

    def busy(self, c):
        return False

    def conv(self, c):
        return self.convs.get(c)

    def send(self, conv, text, role, attachments=None):
        self.sent.append((conv, text))

    async def sh(self, mid, cmd, timeout):
        self.cmds.append(cmd)
        return "EXIT 0"

    async def push_files(self, conv, att):
        return []


def test_serial_flow(tmp_path):
    async def run():
        b = FakeBots(tmp_path)
        g = G.Groups(b)
        mem = [{"bot": x, "branch": "", "worktree": "", "conv": f"c-{x}", **({"cloud": True} if x == "cue" else {})}
               for x in IDS]
        b.store._x("INSERT INTO bot_groups (id, title, machine_id, repo, base, status, auto_merge, members, pending, "
                   "fixes, created_at, updated_at) VALUES ('bg-1','t','vm1','','main','idle',1,?,'[]',0,0,0)",
                   (json.dumps(mem),))
        await g.send("bg-1", MSG)
        for _ in range(50):
            await asyncio.sleep(0.01)
        assert [c for c, _ in b.sent] == ["c-cue"]
        assert "不需要你去找群聊" in b.sent[0][1]
        for i, bot in enumerate(["cue", "agy", "codebuddy", "muse"]):
            g._on_bot("message", {"role": "bot", "id": f"m{i}", "conv": f"c-{bot}", "text": f"{bot} 的结果 SPEC{i}",
                                  "data": {"status": "turn_done"}})
            for _ in range(50):
                await asyncio.sleep(0.01)
            if bot != "muse":
                nxt = ["agy", "codebuddy", "muse"][i]
                assert b.sent[-1][0] == f"c-{nxt}"
                assert f"SPEC{i}" in b.sent[-1][1]
        assert len(b.sent) == 4
        last = g.messages("bg-1")[-1]
        assert last["data"].get("done") and g.group("bg-1")["status"] == "idle"
    asyncio.run(run())


def test_failed_step_pauses(tmp_path):
    async def run():
        b = FakeBots(tmp_path)
        g = G.Groups(b)
        mem = [{"bot": x, "branch": "", "worktree": "", "conv": f"c-{x}"} for x in IDS]
        b.store._x("INSERT INTO bot_groups (id, title, machine_id, repo, base, status, auto_merge, members, pending, "
                   "fixes, created_at, updated_at) VALUES ('bg-1','t','vm1','','main','idle',1,?,'[]',0,0,0)",
                   (json.dumps(mem),))
        await g.send("bg-1", MSG)
        for _ in range(50):
            await asyncio.sleep(0.01)
        g._on_bot("message", {"role": "bot", "id": "m0", "conv": "c-cue", "text": "没做成", "data": {"status": "turn_failed"}})
        await asyncio.sleep(0.05)
        assert len(b.sent) == 1 and "继续" in g.messages("bg-1")[-1]["text"]
        await g.send("bg-1", "@管家 继续")
        for _ in range(50):
            await asyncio.sleep(0.01)
        assert b.sent[-1][0] == "c-agy"
    asyncio.run(run())


def test_verify_cmds():
    g = G.Groups.__new__(G.Groups)
    t = "自己运行一遍 python3 -m pytest -q 和 python3 mdstat.py tests/fixtures --top 5。还有 `rm -rf /`"
    assert g._verify_cmds(t) == ["python3 -m pytest -q", "python3 mdstat.py tests/fixtures --top 5"]
