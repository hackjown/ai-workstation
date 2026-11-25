"""v3.9.86 项目模式: 讨论 → 设计（截图给用户选）→ 开发 → 评审返工 → 验收返修 → 实测截图 → 交付."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bot_groups as G  # noqa: E402
from aiw_api import group_project as P  # noqa: E402
from aiw_api.store import Store  # noqa: E402

IDS = ["agy", "codebuddy", "cue"]
PLAN = {"summary": "做一个待办网页", "has_ui": True, "design_by": ["agy", "cue"],
        "steps": [{"bot": "agy", "task": "写前端 index.html"}, {"bot": "codebuddy", "task": "写测试 tests/test_app.py"}],
        "review_by": "cue", "checks": ["python3 -m pytest -q"],
        "run": {"setup": "", "start": "python3 -m http.server 8765", "url": "http://127.0.0.1:8765/", "pages": ["/"], "cli": ""}}


class LLM:
    def __init__(self):
        self.reviews = 0

    async def complete(self, sys_p, user, n):
        if "定稿方案和分工" in sys_p:
            return json.dumps(PLAN, ensure_ascii=False)
        if "评审意见" in sys_p:
            self.reviews += 1
            return json.dumps({"pass": False, "issues": [{"bot": "codebuddy", "fix": "补测试"}]} if self.reviews == 1
                              else {"pass": True, "issues": []})
        if "验收命令失败" in sys_p:
            return json.dumps({"bot": "agy", "fix": "修 bug"})
        return "| 环节 | 结果 |\n|---|---|\n| 开发 | 完成 |"


class FakeBots:
    def __init__(self, tmp):
        self.store = Store(str(tmp / "t.db"))
        self.listeners, self.sent, self.cmds = [], [], []
        self._llm = LLM()
        self.llm = lambda: self._llm
        self.notifier = None
        self.checks = 0
        self.convs = {f"c-{b}": {"id": f"c-{b}", "bot": b, "grp": "bg-1"} for b in IDS}

    def busy(self, c):
        return False

    def conv(self, c):
        return self.convs.get(c)

    def send(self, conv, text, role, attachments=None):
        self.sent.append((conv, text))

    async def sh(self, mid, cmd, timeout):
        self.cmds.append(cmd)
        if "MERGE_DONE" in cmd:
            return "MERGED agy\nMERGED codebuddy\nSTAT 2 files changed\nMERGE_DONE"
        if "SHOTS_DONE" in cmd:
            name = "design-agy" if "design-agy" in cmd else "page1"
            return f"UP x\nSHOT {name} /home/aiw/workspace/.aiw-worktrees/bg-1/_shots/{name}.png\nSHOTS_DONE"
        if "timeout 300 python3 -m pytest" in cmd:
            self.checks += 1
            return "1 failed\nEXIT 1" if self.checks == 1 else "3 passed\nEXIT 0"
        if "format-patch" in cmd:
            return "FILES 4 个提交\nEXIT 0"
        return ""

    async def push_files(self, conv, att):
        return []

    async def fetch_file(self, cid, path, out_dir):
        return {"path": "/art/" + path.split("/")[-1], "name": path.split("/")[-1], "size": 10}

    def _art_dir(self):
        return "/art"


async def settle():
    for _ in range(80):
        await asyncio.sleep(0.005)


async def drive(g, b, until, n=0):
    for i in range(40):
        await settle()
        grp = g.group("bg-1")
        if (grp.get("project") or {}).get("phase") in until and not grp["pending"]:
            return n
        for bot in list(grp["pending"]):
            n += 1
            data = {"status": "turn_done"}
            if bot == "cue" and (grp.get("project") or {}).get("phase") == "design":
                data["media"] = [{"name": "cue.png", "path": "/art/cue.png", "kind": "image"}]
            g._on_bot("message", {"role": "bot", "id": f"m{n}", "conv": f"c-{bot}", "text": f"{bot} 回复 {n}", "data": data})
    raise AssertionError(f"stuck: {g.group('bg-1').get('project')}")


def test_project_pipeline(tmp_path):
    async def run():
        b = FakeBots(tmp_path)
        g = G.Groups(b)
        mem = [{"bot": x, "branch": "" if x == "cue" else f"aiw/bg-1/{x}",
                "worktree": "" if x == "cue" else f"~/workspace/.aiw-worktrees/bg-1/{x}", "conv": f"c-{x}",
                **({"cloud": True} if x == "cue" else {})} for x in IDS]
        b.store._x("INSERT INTO bot_groups (id, title, machine_id, repo, base, status, auto_merge, members, pending, "
                   "fixes, created_at, updated_at) VALUES ('bg-1','todo','vm1','~/workspace/todo','main','idle',1,?,'[]',0,0,0)",
                   (json.dumps(mem),))
        await g.send("bg-1", "项目规划：做一个待办网页，可以添加、完成、删除待办。")
        await settle()
        assert b.sent[0][0] == "c-cue" and "项目讨论" in b.sent[0][1]
        n = await drive(g, b, ("pick",))
        p = g.group("bg-1")["project"]
        assert [d["label"] for d in p["designs"]] == ["A", "B"]
        card = g.messages("bg-1")[-1]["data"]
        assert {x["label"] for x in card["media"]} == {"方案 A", "方案 B"} and card["actions"][0]["id"] == "pick"
        await g.send("bg-1", "选 B")
        n = await drive(g, b, ("deliver",), n)
        p = g.group("bg-1")["project"]
        assert p["pick"] == "B" and p["rework"] == 1 and p["fixes"] == 1
        dev = [t for c, t in b.sent if "你这一步" in t]
        assert dev and "方案 B" in dev[0]
        assert any("【返工" in t for c, t in b.sent if c == "c-codebuddy")
        assert any("【验收返修" in t for c, t in b.sent if c == "c-agy")
        rep = g.messages("bg-1")[-1]
        assert "验收报告" in rep["text"] and rep["data"]["media"][0]["kind"] == "image"
        assert not any("git -C" in c and "push" in c for c in b.cmds)   # nothing pushed before the user picks
        await g.send("bg-1", "生成补丁")
        await settle()
        last = g.messages("bg-1")[-1]
        assert "生成补丁：完成" in last["text"] and g.group("bg-1")["project"]["delivered"] == ["patch"]
    asyncio.run(run())


def test_trigger_and_pick():
    assert P.is_trigger("项目规划：做一个待办网页，可以添加和删除")
    assert P.is_trigger("@管家 项目：做个记账 App 带图表和导出")
    assert not P.is_trigger("帮我修个 bug")
    ds = [{"bot": "agy", "label": "A"}, {"bot": "cue", "label": "B"}]
    assert P.pick_of("选 B", ds) == 1 and P.pick_of("方案A", ds) == 0 and P.pick_of("就用 agy 的", ds) == 0
    assert P.pick_of("选 C", ds) is None


def test_shot_script():
    s = P.shot_script('"$HOME"/x', [("d", "file://~/workspace/a/design/agy.html")], start="python3 -m http.server 8765",
                      url="http://127.0.0.1:8765/")
    assert '"file://$HOME/workspace/a/design/agy.html"' in s and "setsid" in s and "kill -TERM -- -$PG" in s
    r = P.parse_shots("NO_BROWSER\nSHOT_FAIL d\nSHOTS_DONE")
    assert r["no_browser"] and r["fail"] == ["d"]
