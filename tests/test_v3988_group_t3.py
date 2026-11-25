"""v3.9.88 (ideas from t3code): restart recovery, retry / skip, choices with a reason, 需要你, 开 PR with a written
description and screenshots, watching the PR (CI fail -> owner fixes -> pushed again), per-turn diff, permits from
the group, and the review guide in the report."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_v3986_group_project as T  # noqa: E402
from aiw_api import bot_groups as G  # noqa: E402
from aiw_api import group_project as P  # noqa: E402

GUIDE = {"stops": [{"title": "数据模型", "files": ["app.py"], "why": "先看待办怎么存", "careful": False},
                   {"title": "删除逻辑", "files": ["app.py"], "why": "删除没有确认", "careful": True}],
         "mechanical": ["package-lock.json"]}


class LLM(T.LLM):
    async def complete(self, sys_p, user, n):
        if "审阅导览" in sys_p:
            return json.dumps(GUIDE, ensure_ascii=False)
        if "Pull Request" in sys_p:
            return json.dumps({"title": "待办网页", "body": "## 概述\n做了待办网页"}, ensure_ascii=False)
        if "PR 上有新的评论" in sys_p:
            return json.dumps({"issues": [{"bot": "agy", "fix": "按钮改成蓝色"}], "reply": "要改按钮颜色"}, ensure_ascii=False)
        return await super().complete(sys_p, user, n)


class Bots(T.FakeBots):
    def __init__(self, tmp):
        super().__init__(tmp)
        self._llm = LLM()
        self.llm = lambda: self._llm
        self.pr = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "h1", "comments": [], "reviews": [],
                   "url": "https://github.com/me/todo/pull/7"}
        self.checks_json = [{"name": "test", "bucket": "fail"}]
        self.permits, self.stopped = [], []
        self.conv_msgs = {}

    async def sh(self, mid, cmd, timeout):
        if "gh pr view" in cmd and "=====VIEW" in cmd:
            self.cmds.append(cmd)
            return f"=====VIEW\n{json.dumps(self.pr)}\n=====CHECKS\n{json.dumps(self.checks_json)}\n=====ME\nme"
        if "gh run" in cmd:
            self.cmds.append(cmd)
            return "FAILED test_app.py::test_add - AssertionError"
        if "push -u origin" in cmd:
            self.cmds.append(cmd)
            return "branch set up\nREMOTE git@github.com:me/todo.git\nSHOTS_OK"
        if "gh pr create" in cmd:
            self.cmds.append(cmd)
            return "https://github.com/me/todo/pull/7"
        if "push origin aiw/" in cmd:
            self.cmds.append(cmd)
            return "ok\nEXIT 0"
        if "=====DIFF" in cmd and "--stat=200" in cmd:
            self.cmds.append(cmd)
            return " app.py | 10 +\n=====DIFF\n+x"
        if "log --no-merges" in cmd:
            self.cmds.append(cmd)
            return "HEAD " + "b" * 40 + "\napp.py\n"
        if "rev-parse HEAD" in cmd:
            self.cmds.append(cmd)
            return "a" * 40
        return await super().sh(mid, cmd, timeout)

    async def permit(self, cid, mode, rule=""):
        self.permits.append((cid, mode, rule))
        return {}

    async def stop(self, cid):
        self.stopped.append(cid)
        return {}

    def messages(self, cid, limit=10):
        return self.conv_msgs.get(cid, [])


def make(tmp, bots_cls=Bots):
    b = bots_cls(tmp)
    g = G.Groups(b)
    mem = [{"bot": x, "branch": "" if x == "cue" else f"aiw/bg-1/{x}",
            "worktree": "" if x == "cue" else f"~/workspace/.aiw-worktrees/bg-1/{x}", "conv": f"c-{x}",
            **({"cloud": True} if x == "cue" else {})} for x in T.IDS]
    b.store._x("INSERT INTO bot_groups (id, title, machine_id, repo, base, status, auto_merge, members, pending, "
               "fixes, created_at, updated_at) VALUES ('bg-1','todo','vm1','~/workspace/todo','main','idle',1,?,'[]',0,0,0)",
               (json.dumps(mem),))
    return b, g


def test_reject_designs_with_reason_then_pr_and_watch(tmp_path):
    async def run():
        b, g = make(tmp_path)
        await g.send("bg-1", "项目规划：做一个待办网页，可以添加、完成、删除待办。")
        n = await T.drive(g, b, ("pick",))
        assert g.group("bg-1")["needs"] == "选设计方案"
        acts = g.messages("bg-1")[-1]["data"]["actions"]
        assert acts[-1]["id"] == "redesign" and acts[-1]["reason"] and all(a.get("desc") for a in acts)
        try:
            await g.project.action("bg-1", "redesign", "")
            raise AssertionError("a reason is required")
        except ValueError:
            pass
        await g.send("bg-1", "都不要，理由是太花了，要简洁的深色风格")
        n = await T.drive(g, b, ("pick",), n)
        p = g.group("bg-1")["project"]
        assert p["redesigns"] == 1 and any("太花了" in t and "【重做】" in t for c, t in b.sent if c == "c-agy")
        await g.send("bg-1", "选 A")
        n = await T.drive(g, b, ("deliver",), n)
        rep = g.messages("bg-1")[-1]
        assert "审阅导览" in rep["text"] and "要仔细看" in rep["text"] and rep["data"]["actions"][0]["value"] == "pr"
        assert g.group("bg-1")["needs"] == "选怎么交付"
        # a member turn's own diff range is on its message
        bm = [m for m in g.messages("bg-1") if m["role"] == "bot" and (m["data"] or {}).get("diff")]
        assert bm and bm[-1]["data"]["diff"]["files"] == ["app.py"]
        g.project.watch = lambda gid: None            # the test drives the ticks itself
        await g.project.action("bg-1", "deliver", "pr")
        await T.settle()
        p = g.group("bg-1")["project"]
        assert p["pr"].endswith("/pull/7") and p["watch"]["on"] and "pr" in p["delivered"]
        create = next(c for c in b.cmds if "gh pr create" in c)
        assert "base64 -d" in create and "--base main" in create.replace("'", "")
        assert g.group("bg-1")["needs"] == ""
        # CI red -> the owner fixes -> pushed again
        assert await g.project.watch_tick("bg-1")
        await T.settle()
        assert any("【CI 修复】" in t and "AssertionError" in t for c, t in b.sent)
        n = await T.drive(g, b, ("deliver",), n)
        assert any("push origin aiw/bg-1/merge" in c for c in b.cmds)
        assert "修复已推送" in g.messages("bg-1")[-1]["text"]
        # same head: no second fix; new head and green: told once
        b.pr["headRefOid"], b.checks_json = "h2", [{"name": "test", "bucket": "pass"}]
        await g.project.watch_tick("bg-1")
        assert "CI 全部通过" in g.messages("bg-1")[-1]["text"]
        # a reviewer's comment goes to its owner
        b.pr["comments"] = [{"author": {"login": "alice"}, "body": "按钮改成蓝色吧"}, {"author": {"login": "me"}, "body": "收到"}]
        await g.project.watch_tick("bg-1")
        await T.settle()
        assert any("【PR 评论】" in t and "蓝色" in t for c, t in b.sent if c == "c-agy")
        n = await T.drive(g, b, ("deliver",), n)
        b.pr["state"] = "MERGED"
        assert not await g.project.watch_tick("bg-1")
        assert not g.group("bg-1")["project"]["watch"]["on"]
    asyncio.run(run())


def test_revise_plan_with_reason(tmp_path):
    async def run():
        b, g = make(tmp_path)
        T.PLAN_SAVE = dict(T.PLAN)
        T.PLAN["has_ui"] = False
        try:
            await g.send("bg-1", "项目规划：做一个命令行记账工具，支持导入导出。")
            n = await T.drive(g, b, ("confirm",))
            assert g.group("bg-1")["needs"] == "确认方案"
            await g.project.action("bg-1", "revise", reason="导出要支持 CSV 和 Excel")
            await T.settle()
            assert g.group("bg-1")["project"]["feedback"] == ["导出要支持 CSV 和 Excel"]
            assert g.group("bg-1")["project"]["phase"] == "confirm"
        finally:
            T.PLAN.clear(); T.PLAN.update(T.PLAN_SAVE)
    asyncio.run(run())


def test_manager_step_retries_itself(tmp_path):
    async def run():
        b, g = make(tmp_path)
        P.OP_RETRY, old = (0.01, 0.01), P.OP_RETRY
        calls = []

        async def boom(gid, s):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("电脑没响应")
            g.set_group(gid, status="idle", queue=[])
        g.project.shots = boom
        try:
            g.set_group("bg-1", project={"phase": "run"}, status="working",
                        queue=[{"bot": P.MANAGER_ID, "op": "shots", "step": "实测截图", "task": "x"}])
            await g._run_next("bg-1")
            for _ in range(30):
                await asyncio.sleep(0.02)
            assert len(calls) == 3 and "自动重试" in g.messages("bg-1")[-2]["text"]

            async def bad(gid, s):
                raise RuntimeError("还是不行")
            g.project.shots = bad
            g.set_group("bg-1", status="working", queue=[{"bot": P.MANAGER_ID, "op": "shots", "step": "实测截图", "task": "x"}])
            await g._run_next("bg-1")
            for _ in range(30):
                await asyncio.sleep(0.02)
            grp = g.group("bg-1")
            assert grp["paused"]["bot"] == P.MANAGER_ID and grp["queue"][0]["op"] == "shots"
            assert g.messages("bg-1")[-1]["data"]["actions"][0]["id"] == "resume"
        finally:
            P.OP_RETRY = old
    asyncio.run(run())


def test_recover_after_restart(tmp_path):
    async def run():
        b, g = make(tmp_path)
        # 1. a manager step was in hand
        g.set_group("bg-1", status="working", pending=[], queue=[],
                    cur={"bot": P.MANAGER_ID, "op": "shots", "step": "实测截图", "task": "x"}, project={"phase": "run"})
        seen = []

        async def shots(gid, s):
            seen.append(s["op"])
            g.set_group(gid, status="idle")
        g.project.shots = shots
        g2 = G.Groups(b)
        g2.project.shots = shots
        await g2.recover()
        await T.settle()
        assert seen == ["shots"] and "服务重启过" in "".join(m["text"] for m in g2.messages("bg-1"))
        # 2. a member's turn was cut off -> stopped and given the step again
        step = {"bot": "agy", "task": "写前端", "step": "写前端"}
        g2.set_group("bg-1", status="working", pending=["agy"], queue=[], cur=step)
        b.conv_msgs["c-agy"] = [{"role": "bot", "id": "x1", "conv": "c-agy", "text": "", "data": {"status": "running"}}]
        n0 = len(b.sent)
        await G.Groups(b).recover()
        await T.settle()
        assert b.stopped == ["c-agy"] and b.sent[n0][0] == "c-agy" and "写前端" in b.sent[n0][1]
        # 3. its reply came while the API was down -> caught up, not sent again
        g3 = G.Groups(b)
        g3.set_group("bg-1", status="working", pending=["agy"], queue=[{"bot": "codebuddy", "task": "写测试", "step": "写测试"}], cur=step)
        b.conv_msgs["c-agy"] = [{"role": "bot", "id": "x2", "conv": "c-agy", "text": "做完了", "data": {"status": "turn_done"}}]
        n0 = len(b.sent)
        await g3.recover()
        await T.settle()
        assert any(m["text"] == "做完了" for m in g3.messages("bg-1")) and b.sent[n0][0] == "c-codebuddy"
    asyncio.run(run())


def test_permit_from_group_and_turn_diff(tmp_path):
    async def run():
        b, g = make(tmp_path)
        g.set_group("bg-1", status="working", queue=[{"bot": "agy", "task": "a", "step": "a"}, {"bot": "codebuddy", "task": "b", "step": "b"}])
        await g._run_next("bg-1")
        g._on_bot("message", {"role": "bot", "id": "m1", "conv": "c-agy", "text": "被拦了",
                              "data": {"status": "blocked", "denied": [{"tool": "run", "target": "npm i", "rule": "Bash(npm i)"}]}})
        await asyncio.sleep(0.02)
        grp = g.group("bg-1")
        assert grp["paused"]["why"] == "blocked" and grp["needs"] == "允许被拦的命令" and grp["queue"][0]["bot"] == "agy"
        await g.permit("bg-1", "agy", "allow", "Bash(npm i)")
        grp = g.group("bg-1")
        assert b.permits == [("c-agy", "allow", "Bash(npm i)")] and grp["pending"] == ["agy"] and grp["queue"][0]["bot"] == "codebuddy"
        assert not grp["paused"]
        b.cmds.clear()
        mid = next(m["id"] for m in g.messages("bg-1") if m["role"] == "bot")
        g.patch_msg(mid, diff={"from": "a" * 40, "to": "b" * 40, "files": ["app.py", "x y.py"]})
        async def sh(m, cmd, t):
            b.cmds.append(cmd)
            return " 2 files changed\n=====DIFF\ndiff --git a/app.py b/app.py\n+hi"
        b.sh = sh
        d = await g.diff("bg-1", mid)
        assert "+hi" in d["diff"] and "2 files" in d["stat"] and "'x y.py'" in b.cmds[-1]
        d = await g.diff("bg-1", whole=True)
        assert "main...aiw/bg-1/merge" in b.cmds[-1]
    asyncio.run(run())
