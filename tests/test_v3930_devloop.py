"""v3.9.30: the Codex / Claude Code style loop for coding / dev / fix tasks (agent_runtime/devloop.py).

One block per item of the v3.9.30 brief: 1 lean toolset, 2 explore before edit, 3 todos + receipt, 4 verification
before done, 5 incremental generator, 6 compaction + continuation, 7 stuck strategy, 8 model split, plus activation
(dev skill / route / fix words / continuations) and the dev skill's new-project exemption. The end-to-end cases drive
the real AgentLoop against a local vm-agent (the task-830 project from test_v3926_devflow).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess

import pytest

from agent_runtime import dev_contract as DC
from agent_runtime import devflow as DF
from agent_runtime import devloop as DL
from agent_runtime import intent as IN
from agent_runtime import provenance as PV

from test_v3926_devflow import T830, _git, _project, _store, vm  # noqa: F401  (vm is a fixture)

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = "~/proj/gen.py"
ART = "~/out/page.html"


def _flow(contract=None, intent="dev_fix"):
    f = DF.DevFlow(intent)
    f.contract = contract if contract is not None else {
        "source_of_truth": SRC, "allowed_paths": [SRC, "~/proj"], "deliverables": [ART],
        "generator_cmd": "cd ~/proj && python3 gen.py", "acceptance": [], "req_checks": []}
    f.lp = DL.DevLoop(f)
    return f, f.lp


def _sh(cmd):
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60).stdout


# ------------------------------------------------------------------------------------------------ 1. lean toolset
def test_dev_tools_registered_and_done_has_receipt_outcome():
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    for t in DL.DEV_TOOLS:
        assert t in LOOP_TOOLS and BY_NAME[t].action is None
    assert BY_NAME["read_file"].params.get("offset") and BY_NAME["read_file"].params.get("limit")
    assert BY_NAME["done"].params["outcome"]["enum"] == ["done", "nothing", "failed", "ask"]
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "hide.extend(_dl.DEV_TOOLS)" in src          # hidden for non-coding tasks


def test_read_file_grep_glob_commands_run(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("".join(f"line{i}\n" for i in range(1, 51)))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("needle = 1\n")
    cmd, err = DL.tool_command("read_file", {"path": str(f), "offset": 10, "limit": 3})
    r = DL.parse_tool_output("read_file", _sh(cmd))
    assert not err and r["ok"] and "    10\tline10" in r["text"] and "line13" not in r["text"]
    assert "more lines: read_file with offset=13" in r["text"]
    cmd, _ = DL.tool_command("grep", {"pattern": "needle", "path": str(tmp_path)})
    r = DL.parse_tool_output("grep", _sh(cmd))
    assert r["ok"] and r["files"] == [str(tmp_path / "sub" / "x.py")] and ":1:needle" in r["text"]
    cmd, _ = DL.tool_command("grep", {"pattern": "line7", "path": str(f)})   # a single file still names the file
    assert DL.parse_tool_output("grep", _sh(cmd))["files"] == [str(f)]
    cmd, _ = DL.tool_command("glob", {"pattern": "**/*.py", "path": str(tmp_path)})
    assert set(DL.parse_tool_output("glob", _sh(cmd))["files"]) == {str(f), str(tmp_path / "sub" / "x.py")}
    assert DL.tool_command("read_file", {"path": "~/.ssh/id_rsa"})[1].startswith("refused")
    assert DL.tool_command("grep", {})[1] == "grep needs `pattern`"


def test_files_patch_unique_match_errors_explained(tmp_path, monkeypatch):
    from vm_agent import files as F
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path))
    p = tmp_path / "a.py"
    p.write_text("x = 1\nx = 1\ny = 2\n")
    with pytest.raises(ValueError) as e:
        F.patch("a.py", "x = 1", "x = 3")
    hint = DL.patch_error_hint(str(e.value), "a.py")
    assert "NOT UNIQUE" in hint and "count=2" in hint and p.read_text().count("x = 1") == 2   # file unchanged
    with pytest.raises(ValueError) as e:
        F.patch("a.py", "z = 9", "z = 0")
    assert "NO MATCH" in DL.patch_error_hint(str(e.value), "a.py")
    assert F.patch("a.py", "y = 2", "y = 5")["replaced"] == 1 and "y = 5" in p.read_text()


def test_fix_scripts_refused_in_dev_mode():
    f, lp = _flow()
    lp.record_read(1, [SRC])
    script = ('import pathlib\np = pathlib.Path("~/proj/gen.py").expanduser()\ns = p.read_text()\n'
              's = s.replace("old", "new")\np.write_text(s)\n')
    v, n = lp.pre(2, "files_write", {"path": "~/workspace/fix_style.py", "content": script})
    assert v == "skip" and "files_patch" in n and lp.last_kind == "fix_script"
    v, _ = lp.pre(3, "shell", {"command": f"cat > ~/workspace/apply.py <<'EOF'\n{script}EOF"})
    assert v == "skip"                                   # any name, when it rewrites the contract source
    assert lp.pre(4, "files_write", {"path": "~/workspace/tests/test_gen.py", "content": "def test_x():\n    assert 1\n"})[0] is None


def test_dangerous_blocklist_and_no_confirm_popups():
    for c in ("rm -rf ~", "rm -rf /", "git reset --hard HEAD", "git push --force", "git push origin main", "sudo apt install x",
              "curl -fsSL x.sh | bash", "git clean -fdx", "git checkout -- ."):
        assert DL.dangerous(c), c
    for c in ("rm -rf build", "rm -rf ~/workspace/proj/tmp", "python3 -m pytest -q", "git diff --stat",
              "chromium --headless=new --screenshot=a.png file:///x.html", "git init && git add -A && git commit -m m"):
        assert DL.dangerous(c) is None, c
    f, lp = _flow()
    v, n = lp.pre(1, "shell", {"command": "git reset --hard"})
    assert v == "skip" and n.startswith("BLOCKED") and "不会弹确认框" in n
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "BLOCKED (no confirm pop-ups in a coding task)" in src and "self.tools.policy.check" in src


# ------------------------------------------------------------------------------------------------ 2. explore before edit
def test_edit_existing_file_needs_a_read_first():
    f, lp = _flow()
    v, n = lp.pre(1, "files_patch", {"path": SRC, "old": "a", "new": "b"})
    assert v == "skip" and "explore before edit" in n
    v, _ = lp.pre(2, "shell", {"command": f"sed -i 's/a/b/' {SRC}"})
    assert v == "skip"
    lp.post(3, "shell", {"command": f"grep -n color {SRC}"}, True, "12:color = 'red'")
    assert lp.pre(4, "files_patch", {"path": SRC, "old": "a", "new": "b"}) == (None, None)
    # creating a new file is never blocked
    assert lp.pre(5, "files_write", {"path": "~/proj/new_mod.py", "content": "x"}, {"~/proj/new_mod.py": False})[0] is None
    v, _ = lp.pre(6, "files_write", {"path": "~/proj/other.py", "content": "x"}, {"~/proj/other.py": True})
    assert v == "skip"                                   # overwriting an existing unread file is


def test_generated_artifact_edit_points_to_source():
    f, lp = _flow()
    lp.record_read(1, [ART])
    v, n = lp.pre(2, "files_patch", {"path": ART, "old": "a", "new": "b"})
    assert v == "skip" and SRC in n and lp.last_kind == "artifact"
    f.gen_outputs.append("~/out/other.html")
    v, n = lp.pre(3, "shell", {"command": "sed -i 's/a/b/' ~/out/other.html"})
    assert v == "skip" and "生成的产物" in n


def test_no_source_of_truth_adopts_the_explored_file():
    f, lp = _flow({"source_of_truth": None, "allowed_paths": [], "deliverables": [], "acceptance": []})
    lp.record_read(1, ["~/app/views.py"])
    assert lp.adopt_source("~/app/views.py")
    c = f.contract
    assert c["source_of_truth"] == "~/app/views.py" and c["source_how"] == "explored" and "~/app" in c["allowed_paths"]
    assert f.patch_allowed("~/app/views.py")             # files_patch may now edit it outside ~/workspace


# ------------------------------------------------------------------------------------------------ 3. todos + receipt
def _contract_with_acceptance():
    c = DC.build("页面要支持深色模式，标题用宋体，点击卡片进不去详情了，修复 ~/proj/gen.py")
    return c


def test_todos_seeded_from_acceptance_items_one_in_progress():
    c = _contract_with_acceptance()
    f, lp = _flow(c)
    lp.seed_todos(c)
    texts = [t["content"] for t in lp.todos]
    assert len(lp.todos) == len(c["acceptance"]) and any("详情" in t for t in texts)
    assert any((t.get("auto") or "").startswith("req:") for t in lp.todos)
    ok, msg = lp.todo_write(1, [{"id": "1", "content": texts[0], "status": "in_progress"},
                                {"id": "2", "content": texts[1], "status": "in_progress"}])
    assert not ok and "only ONE" in msg
    assert "TODO" in lp.block() and "[ ] 1." in lp.block()


def test_receipt_flags_unverified_claims():
    f, lp = _flow()
    lp.todos = [{"id": "1", "content": "点击卡片进入详情", "status": "pending", "files": [], "auto": None, "evidence": ""},
                {"id": "2", "content": "深色模式随系统切换", "status": "done", "files": [], "auto": None, "evidence": "第 5 步 test"}]
    rec = lp.receipt("已修复：点击卡片进入详情正常，深色模式也好了。已知问题：无")
    assert "⚠ 总结声称完成但未验证：点击卡片进入详情" in rec and "✓ 深色模式随系统切换" in rec
    assert lp.flagged == [("unverified_claim", "点击卡片进入详情")]
    rec2 = lp.receipt("改了样式。已知问题：点击卡片进入详情还没验证")
    assert "✗ 未完成/未验证：点击卡片进入详情" in rec2


# ------------------------------------------------------------------------------------------------ 4. verification
def test_todo_done_needs_verification_after_last_edit():
    f, lp = _flow()
    lp.todos = [{"id": "1", "content": "改按钮颜色", "status": "in_progress", "files": [SRC], "auto": None, "evidence": ""}]
    lp.record_read(1, [SRC])
    lp.post(2, "shell", {"command": "cd ~/proj && python3 -m pytest -q"}, True, "3 passed")   # before the edit
    lp.post(3, "files_patch", {"path": SRC, "old": "red", "new": "blue"}, True)
    ok, msg = lp.todo_write(4, [{"id": "1", "content": "改按钮颜色", "status": "done", "files": [SRC]}])
    assert not ok and "PARTLY REJECTED" in msg and lp.todos[0]["status"] == "in_progress"
    lp.post(5, "shell", {"command": "cd ~/proj && python3 -m pytest -q"}, True, "3 passed")
    ok, msg = lp.todo_write(6, [{"id": "1", "content": "改按钮颜色", "status": "done", "files": [SRC]}])
    assert ok and lp.todos[0]["status"] == "done" and "第 5 步" in lp.todos[0]["evidence"]
    # the generator and a diff check count as verification too
    lp.post(7, "files_patch", {"path": SRC, "old": "blue", "new": "green"}, True)
    lp.post(8, "shell", {"command": "cd ~/proj && git diff --stat"}, True, "1 file changed")
    assert lp.evidence_after(7)[1] == "check"


def test_requirement_check_todos_close_on_verify_html_pass():
    c = _contract_with_acceptance()
    f, lp = _flow(c)
    lp.seed_todos(c)
    lp.post(2, "files_patch", {"path": "~/proj/gen.py", "old": "a", "new": "b"}, True)
    f.last_source_step = 2
    f.record_verify(3, {"ok": True, "checks": [{"id": "dark_mode", "ok": True, "label": "深色"}]})
    closed = lp.auto_verify(3)
    t = next(x for x in lp.todos if x.get("auto") == "req:dark_mode")
    assert t["id"] in closed and t["status"] == "done"


# ------------------------------------------------------------------------------------------------ 5. generator runs
def test_full_generator_refused_while_fast_exists_and_rerun_without_change():
    f, lp = _flow()
    f.contract["generator_cmd_fast"] = "cd ~/proj && python3 gen.py --from-cache"
    g = lp.generator_gate(1, "cd ~/proj && python3 gen.py")
    assert g and "--from-cache" in g                     # iterate with the fast command first
    assert lp.generator_gate(1, "cd ~/proj && python3 gen.py --from-cache") is None
    f.last_source_step = 2
    f.fast_runs.append(3)
    assert "没有改动" in lp.generator_gate(4, "cd ~/proj && python3 gen.py --from-cache")
    f.verify_ok.append(4)                                # fast output verified -> one full run is fine
    assert lp.generator_gate(5, "cd ~/proj && python3 gen.py") is None
    f.gen_runs.append(5)
    assert "已经完整运行过" in lp.generator_gate(6, "cd ~/proj && python3 gen.py")


# ------------------------------------------------------------------------------------------------ 6. compaction
def test_compaction_keeps_paths_decisions_and_todos():
    f, lp = _flow()
    lp.todos = [{"id": "1", "content": "恢复 GitHub 栏目", "status": "pending", "files": [], "auto": None, "evidence": ""}]
    lp.record_read(1, ["~/proj/gen.py", "~/proj/templates/page.j2"])
    lp.post(2, "files_patch", {"path": "~/proj/templates/page.j2", "old": "a", "new": "b"}, True)
    hist = [f"{i}. shell(...) -> ok" for i in range(1, 30)] + ["30. done REJECTED by the dev workflow gate: 没有验证"]
    hist += [f"{i}. shell(...) -> ok" for i in range(31, 70)]
    assert lp.needs_compaction(len(hist))
    chunk = lp.fold(hist)
    assert len(chunk) == len(hist) - 25 and not lp.needs_compaction(len(hist))
    b = lp.block()
    assert "~/proj/templates/page.j2" in b and "~/proj/gen.py" in b and "恢复 GitHub 栏目" in b and "没有验证" in b


async def test_compaction_uses_the_cheap_model_and_survives_failure():
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor

    class Cheap:
        calls = []

        async def complete(self, system, user, max_tokens=800):
            self.calls.append(user)
            return "- 改了 ~/proj/gen.py 的 STYLE；GitHub 栏目还没恢复"
    loop = AgentLoop(Task(prompt="修复 ~/proj/gen.py 的样式 bug"), "s", None, LLMRouter([ScriptedProvider([])]),
                     ToolExecutor(None), config=LoopConfig(make_plan=False))
    assert loop._intent == IN.DEV_FIX
    stored = {}
    loop.persist = lambda k, v: stored.__setitem__(k, v)
    loop.summary_llm = Cheap()
    loop.history = [f"{i}. shell(cat ~/proj/gen.py) -> ok" for i in range(1, 50)]
    await loop._dev_compact()
    assert "STYLE" in loop.lp.memory and Cheap.calls and stored["devmem"]["memory"].startswith("- 改了")

    class Broken:
        async def complete(self, *a, **k):
            raise RuntimeError("gateway down")
    loop.summary_llm = Broken()
    loop.history += [f"{i}. shell(x) -> ok" for i in range(50, 80)]
    await loop._dev_compact()                            # no exception: the deterministic memory stays
    assert loop.lp.compacted_upto == len(loop.history) - 25


def test_devmem_persisted_and_restored_for_continuation(tmp_path):
    from aiw_api import continuation as CT
    from aiw_api import devflow as AD
    from aiw_protocols import Task
    st = _store(tmp_path)
    f, lp = _flow()
    lp.todos = [{"id": "1", "content": "恢复详情弹窗", "status": "in_progress", "files": [], "auto": None, "evidence": ""}]
    lp.record_read(3, [SRC])
    lp.post(4, "files_patch", {"path": SRC, "old": "a", "new": "b"}, True)
    lp.memory = "- 详情弹窗的 openDetail 在 gen.py 第 120 行"
    root = Task(id="task-1689", prompt="修复")
    AD.persister(st, root)("devmem", lp.snapshot())
    assert st.kv_get("task_devmem:task-1689")["todos"][0]["content"] == "恢复详情弹窗"

    class S:
        def continuations(self, rid):
            return [{"id": "task-1689"}, {"id": "task-68b", "continues": "task-1689"}]

        def kv_get(self, k, d=None):
            return st.kv_get(k, d)

        def task(self, tid):
            return {"id": tid}
    m = CT.parent_devmem(S(), {"id": "task-68b", "continues": "task-1689"})
    assert m["from_task"] == "task-1689"
    lp2 = DL.DevLoop(DF.DevFlow("dev_fix"))
    assert lp2.restore(m)
    assert lp2.resumed and lp2.todos[0]["status"] == "in_progress" and "openDetail" in lp2.memory
    assert lp2.known(SRC) and "上一轮运行的进度" in lp2.block()      # no restart: the file is already explored
    src = (ROOT / "services/api/aiw_api/sessions.py").read_text()
    assert 'meta["parent_devmem"] = _cont.parent_devmem(self.store, row)' in src


# ------------------------------------------------------------------------------------------------ 7. stuck strategy
def test_same_error_three_times_forces_explore():
    f, lp = _flow()
    lp.record_read(1, [SRC])
    err = 'Traceback (most recent call last):\n  File "/home/a/proj/gen.py", line %d\nKeyError: \'title\''
    assert lp.post(2, "shell", {"command": "python3 gen.py"}, False, "", err % 10) is None
    assert lp.post(3, "shell", {"command": "python3 gen.py"}, False, "", err % 11) is None   # line numbers fold
    note = lp.post(4, "shell", {"command": "python3 gen.py"}, False, "", err % 12)
    assert note and note.startswith("STUCK") and "KeyError" in note
    assert lp.phase == "explore" and not lp.known(SRC)
    v, n = lp.pre(5, "files_patch", {"path": SRC, "old": "a", "new": "b"})
    assert v == "skip" and "回到探索阶段" in n
    lp.post(6, "shell", {"command": f"grep -n title {SRC}"}, True, "5:title = x")
    assert lp.pre(7, "files_patch", {"path": SRC, "old": "a", "new": "b"}) == (None, None)


def test_step_budget_asks_with_receipt(monkeypatch):
    monkeypatch.setenv("AIW_DEV_STEP_BUDGET", "20")
    f, lp = _flow()
    assert not lp.budget_exceeded(19) and lp.budget_exceeded(20)
    lp.extend_budget(20)
    assert not lp.budget_exceeded(39) and lp.budget_exceeded(40)
    lp.new_build = True
    assert not lp.budget_exceeded(100)                   # a new build gets AIW_DEV_STEP_BUDGET_NEW (250)


async def test_loop_budget_ask_stops_with_receipt(vm, monkeypatch):
    monkeypatch.setenv("AIW_DEV_STEP_BUDGET", "2")
    comp, tmp = vm
    repo, art, gen_cmd, book, fps = _project(tmp)
    gen = repo / "news" / "aiw_news.py"
    asked = []

    async def ask(q, opts):
        asked.append((q, opts))
        return "停止并出报告"
    loop, _ = _loop(comp, tmp, [("read_file", {"path": str(gen)}), ("grep", {"pattern": "GH", "path": str(gen)}),
                                ("shell", {"command": "true"})], book, fps, [], ask=ask)
    task = await loop.run()
    assert asked and "预算 2 步" in asked[0][0] and "继续（再给一轮预算）" in asked[0][1]
    assert task.status.value == "stopped" and "TODO" in task.result


# ------------------------------------------------------------------------------------------------ 8. model split
def test_dev_route_strong_and_summary_model_flash(tmp_path, monkeypatch):
    from aiw_api import autoroute as AR
    from aiw_api.sessions import SessionManager
    monkeypatch.delenv("AIW_DEV_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    st = _store(tmp_path)
    st.kv_set("models", {"model": "deepseek-v4.1-flash", "fallback": ""})
    r = AR.dev_route(st, "修复")
    assert r["warning"] and "autoroute.dev.model" in r["warning"]          # fallback warning kept
    st.kv_set("autoroute", {"dev": {"model": "claude-opus-4"}})
    r = AR.dev_route(st, "修复")
    assert r["dev_model"] == "claude-opus-4" and not r.get("warning") and r["thinking"] == "on"

    class Fake:
        _custom_llm = None
        secrets = None
        store = st
        model_settings = SessionManager.model_settings
        _tool_choices = SessionManager._tool_choices
    sm = Fake()
    got = SessionManager._summary_llm(sm)
    assert got is not None                                                   # the flash main model, thinking off
    st.kv_set("models", {"model": "glm-5", "summary_model": "qwen-flash"})
    assert SessionManager._summary_llm(sm) is not None
    st.kv_set("models", {"model": "glm-5"})
    assert SessionManager._summary_llm(sm, small="SMALL") == "SMALL"         # no flash configured: the small model
    src = (ROOT / "services/api/aiw_api/sessions.py").read_text()
    assert "loop.summary_llm = self._summary_llm(" in src


# ------------------------------------------------------------------------------------------------ activation
def test_activation_dev_skill_coding_words_fix_words():
    assert IN.classify("【技能：dev】做一个记账本")["intent"] == IN.DEV_FIX
    r = IN.classify("帮我写一个记账网站", {})
    assert r["intent"] == IN.DEV_FIX and IN.NEW_BUILD_REASON in r["reasons"]
    assert IN.classify("build me a todo app", {})["intent"] == IN.DEV_FIX
    assert IN.classify(T830, {"script_runs": ["~/ai-workstation/news/aiw_news.py"]})["intent"] == IN.DEV_FIX
    assert IN.classify("帮我查一下明天的天气", {})["intent"] == IN.OTHER
    assert IN.classify("运行 aiw_news.py 生成今天的新闻", {})["intent"] == IN.RUN


def test_activation_by_dev_route_on_long_continuation(tmp_path):
    from aiw_api import devflow as AD
    from aiw_protocols import Task
    st = _store(tmp_path)
    t = Task(id="task-x", prompt="继续", intent="run", meta={"signals": {"script_runs": ["~/p/gen.py"]}})
    assert AD.promote_on_route(st, t, {"by": "dev", "reason": "长任务的继续/重试"})
    assert t.intent == IN.DEV_FIX and st.kv_get("task_intent:task-x")["intent"] == IN.DEV_FIX
    t2 = Task(id="task-y", prompt="继续", intent="other", meta={"signals": {}})      # no code in the thread
    assert not AD.promote_on_route(st, t2, {"by": "dev"}) and t2.intent == "other"
    assert not AD.promote_on_route(st, Task(id="task-z", prompt="x", intent="other"), {"by": "rule"})


def test_continuation_keeps_dev_intent(tmp_path):
    from aiw_api import devflow as AD
    st = _store(tmp_path)
    st.kv_set("task_intent:task-1689", {"intent": "dev_fix", "forced": False, "reasons": ["修复"]})

    class S:
        def kv_get(self, k, d=None):
            return st.kv_get(k, d)

        def kv_set(self, k, v):
            st.kv_set(k, v)

        def task(self, tid):
            return {"id": tid, "prompt": T830}
    info = AD.task_intent(S(), {"id": "task-68b", "continues": "task-1689", "prompt": "继续", "source": "continue"})
    assert info["intent"] == IN.DEV_FIX


def test_new_build_detection():
    assert DL.is_new_build({"forced": True}, [], {"source_of_truth": None})
    assert DL.is_new_build({"reasons": [IN.NEW_BUILD_REASON]}, [], {})
    assert not DL.is_new_build({"forced": True}, ["~/out/a.html"], {})
    assert not DL.is_new_build({"forced": True}, [], {"source_of_truth": "~/p/gen.py"})
    assert not DL.is_new_build({"reasons": ["问题/修复用词：修复"]}, [], {})


def test_new_project_exempt_from_explore_gate():
    f, lp = _flow({"source_of_truth": None, "allowed_paths": [], "deliverables": [], "acceptance": []})
    lp.new_build = True
    assert lp.pre(1, "files_write", {"path": "proj/PLAN.md", "content": "# plan"}, {"~/workspace/proj/PLAN.md": False})[0] is None
    assert lp.pre(2, "files_write", {"path": "proj/index.html", "content": "<p>"}, {})[0] is None
    lp.post(2, "files_write", {"path": "proj/index.html", "content": "<p>"}, True)
    assert lp.pre(3, "shell", {"command": "sed -i 's/a/b/' ~/workspace/proj/style.css"})[0] is None
    assert lp.pre(4, "files_patch", {"path": "proj/index.html", "old": "<p>", "new": "<h1>"})[0] is None
    assert not lp.adopt_source("~/workspace/proj/app.py")          # no source-of-truth demand for a new build
    # existing code outside the new project still needs a read
    assert lp.pre(5, "files_patch", {"path": "~/other/lib.py", "old": "a", "new": "b"})[0] == "skip"
    assert "阶段：探索" not in lp.block()


def test_dev_skill_note_matches_the_framework():
    from agent_runtime import skills as S
    note = next(x for x in S.CATALOG if x["id"] == "dev")["note"]
    for kept in ("PLAN.md", "static clickable prototype", "ask_user", "write automated tests", "deploy_server"):
        assert kept in note                                   # nothing of the old flow removed
    for new in ("todo_write", "acceptance items", "files_patch", "read_file", "verification", 'outcome=\\"ask\\"'.replace("\\", ""),
                "Creating new files"):
        assert new in note, new


# ------------------------------------------------------------------------------------------------ end to end (vm-agent)
def _loop(comp, tmp, script, book, fps, events, prompt=None, ask=None):
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor

    async def sink(e):
        events.append(e)
    task = Task(prompt=prompt or ("修复 aiw_news.py：" + T830), meta={"provenance": book.to_dict(), "fingerprints": fps,
                                                                    "signals": {"script_runs": []}})
    llm = ScriptedProvider(script)
    loop = AgentLoop(task, "s930", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink, ask=ask,
                     config=LoopConfig(max_steps=0, timeout_s=120, make_plan=False, artifacts_dir=str(tmp / "artifacts")))
    return loop, llm


def _vmsgs(events):
    return [str((e.data or {}).get("message") or "") for e in events if e.type.value == "agent.validation"]


async def test_e2e_fix_read_patch_verify_todo_receipt(vm, monkeypatch):
    monkeypatch.setenv("VM_AGENT_PATCH_ROOTS", "/")
    comp, tmp = vm
    repo, art, gen_cmd, book, fps = _project(tmp)
    gen = repo / "news" / "aiw_news.py"
    events = []
    script = [("files_patch", {"path": str(gen), "old": "font:14px serif", "new": "font:15px sans-serif"}),  # unread: refused
              ("read_file", {"path": str(gen), "offset": 1, "limit": 6}),
              ("files_patch", {"path": str(gen), "old": "font:14px serif", "new": "font:15px sans-serif"}),
              ("todo_write", {"todos": [{"id": "1", "content": "新样式", "status": "done", "files": [str(gen)]}]}),  # no verify yet
              ("shell", {"command": gen_cmd}),
              ("shell", {"command": f"cd {repo} && python3 -m unittest discover -s tests -q"}),
              ("todo_write", {"todos": [{"id": "1", "content": "新样式", "status": "done", "files": [str(gen)]}]}),
              ("done", {"summary": "新样式已改好并重新生成，单测通过。已知问题：无"})]
    loop, llm = _loop(comp, tmp, script, book, fps, events)
    task = await loop.run()
    msgs = _vmsgs(events)
    assert msgs[0].startswith("先读后改")
    assert any("TODO 标完成被拒绝" in m for m in msgs)
    assert "15px" in art.read_text() and "15px" in gen.read_text()
    assert task.status.value == "succeeded", task.result
    assert "完成回执" in task.result and "✓ 新样式" in task.result
    assert any("PARTLY REJECTED" in h for h in loop.history)
    assert "TODO（todo_write 维护" in llm.requests[-1].task                # the list is in the task text


async def test_e2e_new_project_dev_skill_flow_not_blocked(vm, monkeypatch):
    monkeypatch.setenv("AIW_DEV_DONE_REJECTS", "0")
    comp, tmp = vm
    from agent_runtime.provenance import ProvenanceBook
    events, asked = [], []

    async def ask(q, opts):
        asked.append(q)
        return opts[0] if opts else "好"
    script = [("files_write", {"path": "ledger/PLAN.md", "content": "# 记账网站\n- 功能\n"}),
              ("files_write", {"path": "ledger/proto/index.html", "content": "<html><body><h1>记账</h1></body></html>"}),
              ("ask_user", {"question": "选哪个方向？", "options": ["A", "B"]}),
              ("files_write", {"path": "ledger/proto/index.html", "content": "<html><body><h1>记账 A</h1></body></html>"}),
              ("files_patch", {"path": "ledger/proto/index.html", "old": "记账 A", "new": "记账本"}),
              ("files_write", {"path": "ledger/app.py", "content": "print('ok')\n"}),
              ("done", {"summary": "做好了。已知问题：无"})]
    loop, _ = _loop(comp, tmp, script, ProvenanceBook(), {}, events, prompt="【技能：dev】帮我写一个记账网站", ask=ask)
    assert loop._intent == IN.DEV_FIX
    task = await loop.run()
    assert loop.lp.new_build
    msgs = _vmsgs(events)
    assert not any(m.startswith(("先读后改", "拒绝：这是脚本生成", "拒绝补丁脚本")) for m in msgs), msgs
    assert (tmp / "ws" / "ledger" / "proto" / "index.html").read_text().count("记账本") == 1
    assert asked == ["选哪个方向？"]                                          # no contract card for a new build
    assert task.status.value == "succeeded", task.result


def test_local_installs_run_without_popup_others_refused():
    assert DL.local_install("pip install -r requirements.txt") and DL.local_install("cd ~/workspace/p && npm install")
    for c in ("npm i -g x", "sudo pip install x", "pip install x --break-system-packages", "cd p && pip install a && pytest"):
        assert not DL.local_install(c), c


async def test_e2e_dev_task_never_pops_a_confirm(vm):
    comp, tmp = vm
    repo, art, gen_cmd, book, fps = _project(tmp)
    events, confirms = [], []
    script = [("shell", {"command": "scp a.txt user@host:/tmp/"}),        # policy L3 -> refused, no pop-up
              ("shell", {"command": "git push origin main"}),             # blocklist
              ("done", {"summary": "停下。", "outcome": "failed"})]
    loop, _ = _loop(comp, tmp, script, book, fps, events)

    async def confirm(*a, **k):
        confirms.append(a)
        return True
    loop.tools.confirm = confirm

    async def ask(q, opts):
        return "停止"
    loop.ask = ask
    await loop.run()
    assert not confirms
    hist = "\n".join(loop.history)
    assert "BLOCKED (no confirm pop-ups in a coding task)" in hist and "BLOCKED (dev blocklist)" in hist


def test_readme_documents_v3930():
    s = (ROOT / "README.md").read_text()
    assert "## v3.9.30" in s and "devloop.py" in s and "AIW_DEV_STEP_BUDGET" in s
