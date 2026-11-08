"""v3.9.29: devflow fixes from task-68b.

task-68b ("继续" of task-1689, the news restyle "做进 aiw_news.py"): the dev route fell back to the flash main model; the
contract missed the bare source name `aiw_news.py` and listed ~/aiw-news/n1.html as the only allowed path; files_write
was jailed to ~/workspace so the model wrote 11 fix_*.py side scripts (most never run, one replacement written 6 times);
the stuck card said only "在打转"; the board kept "源码共 303 行" after the file grew; the done summary claimed the style
was finished while verify_html had failed; every iteration re-ran the full (network) generator.
"""
import asyncio
import json
import os
import pathlib
import subprocess

import pytest

from agent_runtime import dev_contract as DC
from agent_runtime import devflow as DF
from agent_runtime import escalate as ES
from agent_runtime import findings as FD
from agent_runtime import git_ops as GO
from agent_runtime import provenance as PV
from agent_runtime import verify_html as VH

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = "~/ai-workstation/news/aiw_news.py"
G68B = ("早报改成截图里的样式：深色、浅色背景随系统切换、标题用截图里那种宋体、橙红色点缀，顶部有分类和来源标签，头条配大图，"
        "其余新闻右边配小图。 每条新闻点进去要有详情，正文用简体中文，详情页要有返回首页的按钮。 再加两个栏目：GitHub 热榜和新榜，"
        "每个项目点进去有中文介绍，也要能返回首页。 这些都做进 aiw_news.py，以后每天生成的早报自带这些功能。字体别整个嵌进去，"
        "文件别太大。做完把当天的早报发我。")
FIX_SCRIPT = ('import pathlib\np = pathlib.Path("/home/aiw/ai-workstation/news/aiw_news.py")\ns = p.read_text("utf-8")\n'
              "old = 'if(p.id===\"p-\"+h)'\nassert s.count(old) == 1\ns = s.replace(old, 'if(p.id===h)')\np.write_text(s, 'utf-8')\n")


class KV:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def kv_get(self, k, default=None):
        return self.d.get(k, default)

    def kv_set(self, k, v):
        self.d[k] = v


def _flow(goal=G68B, runs=(SRC,)):
    d = DF.DevFlow("dev_fix")
    d.contract = DC.build(goal, "", d.book, {}, script_runs=list(runs))
    return d


# ------------------------------------------------------------------------------------------- 1. dev model routing
def test_dev_route_prefers_fallback_over_flash_main(monkeypatch):
    from aiw_api import autoroute as AR
    monkeypatch.delenv("AIW_DEV_MODEL", raising=False)
    st = KV({"models": {"model": "workbuddy/cn:deepseek-v4.1-flash", "fallback": "deveco-glm-5.3"}})
    r = AR.dev_route(st, "修复")
    assert r["model"] == "deveco-glm-5.3" and r["model_source"] == "models.fallback"
    assert r["thinking"] == "on" and r["effort"] == "high" and not r.get("warning")


def test_dev_route_configured_dev_model_wins(monkeypatch):
    from aiw_api import autoroute as AR
    monkeypatch.delenv("AIW_DEV_MODEL", raising=False)
    st = KV({"models": {"model": "x-flash", "fallback": "deveco-glm-5.3", "dev_model": "claude-opus-4"},
             "autoroute": {"dev": {"model": ""}}})
    assert AR.dev_route(st, "x")["model"] == "claude-opus-4"
    st.d["autoroute"] = {"dev": {"model": "gpt-5-codex"}}
    r = AR.dev_route(st, "x")
    assert r["model"] == "gpt-5-codex" and r["model_source"] == "autoroute.dev.model"


def test_dev_route_warns_when_only_flash(monkeypatch):
    from aiw_api import autoroute as AR
    monkeypatch.delenv("AIW_DEV_MODEL", raising=False)
    st = KV({"models": {"model": "workbuddy/cn:deepseek-v4.1-flash"}})
    r = AR.dev_route(st, "x")
    assert r.get("warning") and "flash" in r["warning"]
    lab = AR.label(r, "workbuddy/cn:deepseek-v4.1-flash")
    assert "⚠" in lab and "来源" in lab and "思考 开" in lab


def test_routine_tiers_untouched(monkeypatch):
    from aiw_api import autoroute as AR
    monkeypatch.delenv("AIW_AUTOROUTE", raising=False)
    st = KV({"models": {"model": "x-flash", "fallback": "glm"}})
    assert asyncio.run(AR.decide(st, "你好", for_chat=True)) is None          # chat keeps the flash main model
    r = asyncio.run(AR.decide(st, "修复", intent="dev_fix"))
    assert r["by"] == "dev" and r["model"] == "glm"


# ------------------------------------------------------------------------------------------- 2. files_patch
def test_files_patch_inside_root_and_count(tmp_path, monkeypatch):
    from vm_agent import files as F
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    f = tmp_path / "ws" / "a.py"
    f.write_text("x = 1\ny = 1\n")
    r = F.patch("a.py", "x = 1", "x = 2")
    assert r["replaced"] == 1 and "-x = 1" in r["diff"] and "+x = 2" in r["diff"]
    assert f.read_text() == "x = 2\ny = 1\n"
    with pytest.raises(ValueError, match="found 2"):
        F.patch("a.py", " = ", " := ", count=1)
    assert f.read_text() == "x = 2\ny = 1\n"            # a mismatch changes nothing
    assert F.patch("a.py", " = ", " := ", count=0)["replaced"] == 2


def test_files_patch_outside_root_only_when_allowed(tmp_path, monkeypatch):
    from vm_agent import files as F
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("VM_AGENT_PATCH_ROOTS", str(tmp_path / "home"))
    src = tmp_path / "home" / "news" / "aiw_news.py"
    src.parent.mkdir(parents=True)
    src.write_text("A\n")
    with pytest.raises(PermissionError):
        F.patch(str(src), "A", "B")
    assert F.patch(str(src), "A", "B", allow_outside=True)["replaced"] == 1 and src.read_text() == "B\n"
    sec = tmp_path / "home" / ".ssh" / "config"
    sec.parent.mkdir()
    sec.write_text("A")
    with pytest.raises(PermissionError):
        F.patch(str(sec), "A", "B", allow_outside=True)
    other = tmp_path / "elsewhere.py"
    other.write_text("A")
    with pytest.raises(PermissionError):
        F.patch(str(other), "A", "B", allow_outside=True)


def test_files_patch_tool_registered_end_to_end(tmp_path, monkeypatch):
    from aiw_protocols import ActionType, Command
    from aiw_protocols.command import PAYLOAD_SCHEMA
    from tool_router.registry import BY_NAME, SUBAGENT_TOOLS
    from tool_router.policy import PolicyEngine
    from vm_agent.server.router import Router
    assert BY_NAME["files_patch"].action == "files.patch" and "files_patch" in SUBAGENT_TOOLS
    assert PAYLOAD_SCHEMA[ActionType.FILES_PATCH.value] == ["path", "old", "new"]
    assert not PolicyEngine().check("files.patch", {"path": "~/x/gen.py"}).needs_confirm
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path))
    (tmp_path / "g.py").write_text("old\n")

    class B:
        env = {}
    res = asyncio.run(Router(B()).handle(Command(type="files.patch", payload={"path": "g.py", "old": "old", "new": "new"})))
    assert res.success and (tmp_path / "g.py").read_text() == "new\n"


def test_patch_allowed_only_on_contract_paths():
    d = _flow()
    assert d.patch_allowed(SRC) and d.patch_allowed("~/ai-workstation/news/helper.py")
    assert not d.patch_allowed("~/.bashrc") and not d.patch_allowed("~/ai-workstation/services/x.py")


def test_files_patch_counts_as_source_write():
    d = _flow()
    d.post_tool(3, "files_patch", {"path": SRC, "old": "a", "new": "b"}, True)
    assert d.source_writes.get(SRC) == 3 and SRC in d.just_written


def test_adhoc_warning_not_fired_for_script_editing_contract_source():
    d = _flow()
    _, warn = d.pre_tool(1, "files_write", {"path": "tasks/t/fix_route.py", "content": FIX_SCRIPT})
    assert warn and "files_patch" in warn and "loses the change" not in warn
    # a side script patching a generated artifact still gets the old warning
    w2 = PV.adhoc_warning("~/workspace/inject3.py", SRC)
    assert "loses the" in w2


def test_loop_prompts_point_at_files_patch():
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "use files_patch with a SHORT exact `old` snippet" in src
    assert "then RUN it in " in src and "files_write to save a script first, then run it.\")" not in src
    assert 'run_args["allow_outside"] = True' in src


# ------------------------------------------------------------------------------------------- 3. written, not run
def test_written_script_not_executed_gets_nudge():
    d = _flow()
    d.pre_tool(1, "files_write", {"path": "tasks/t/fix_route.py", "content": FIX_SCRIPT})
    d.post_tool(1, "files_write", {"path": "tasks/t/fix_route.py", "content": FIX_SCRIPT}, True)
    for st in (2, 3):
        v, w = d.pre_tool(st, "shell", {"command": "grep -n route ~/ai-workstation/news/aiw_news.py"})
        assert not (w or "").startswith("NOT EXECUTED")
        d.post_tool(st, "shell", {"command": "grep -n route ~/ai-workstation/news/aiw_news.py"}, True)
    v, w = d.pre_tool(4, "shell", {"command": "ls"})
    assert v is None and "NOT EXECUTED" in w and "fix_route.py" in w and "python3 ~/workspace/tasks/t/fix_route.py" in w
    assert len(d.unexec) == 1


def test_running_the_script_clears_it_and_edits_the_source():
    d = _flow()
    d.post_tool(1, "files_write", {"path": "tasks/t/fix_route.py", "content": FIX_SCRIPT}, True)
    d.post_tool(2, "shell", {"command": "cd ~/workspace/tasks/t && python3 fix_route.py"}, True)
    assert not d.pending and d.source_writes.get(SRC) == 2 and d.last_source_step == 2
    _, w = d.pre_tool(5, "shell", {"command": "ls"})
    assert not w


def test_second_script_with_same_payload_is_refused():
    d = _flow()
    d.post_tool(1, "files_write", {"path": "tasks/t/fix_route.py", "content": FIX_SCRIPT}, True)
    v, note = d.pre_tool(2, "files_write", {"path": "tasks/t/fix_hash_match.py", "content": FIX_SCRIPT.replace("p.id===h", "p.id==h")})
    assert v == "skip" and "fix_route.py" in note and "python3" in note
    # after it ran, the same replacement is "already applied"
    d.post_tool(3, "shell", {"command": "python3 ~/workspace/tasks/t/fix_route.py"}, True)
    v, note = d.pre_tool(4, "files_write", {"path": "tasks/t/fix_route_prefix.py", "content": FIX_SCRIPT})
    assert v == "skip" and "已经执行过" in note
    assert any(k == "repeated_edit" for k, _, _ in d.stuck_pattern())


def test_heredoc_script_run_in_same_command_is_not_pending():
    d = _flow()
    cmd = f"cat > ~/workspace/p1.py <<'EOF'\n{FIX_SCRIPT}\nEOF\npython3 ~/workspace/p1.py"
    d.post_tool(1, "shell", {"command": cmd}, True)
    assert not d.pending and d.source_writes.get(SRC) == 1


def test_repeated_files_patch_refused():
    d = _flow()
    a = {"path": SRC, "old": "x", "new": "y"}
    d.post_tool(1, "files_patch", a, True)
    v, note = d.pre_tool(2, "files_patch", a)
    assert v == "skip" and "already applied" in note


# ------------------------------------------------------------------------------------------- 4. contract source
def test_bare_source_name_resolved_from_thread_runs():
    c = DC.build(G68B, "之前的产物 ~/aiw-news/n1.html", PV.ProvenanceBook(), {},
                 script_runs=["~/workspace/tasks/t/fix_x.py", SRC])
    assert c["source_of_truth"] == SRC and c["source_how"] == "named"
    assert "~/aiw-news/n1.html" not in c["allowed_paths"]            # generated output is never an allowed path
    assert c["allowed_paths"][:2] == [SRC, "~/ai-workstation/news"]
    assert c["generator_cmd"] == "cd ~/ai-workstation/news && python3 aiw_news.py" and not c["confirm"]


def test_bare_source_name_unresolved_asks_with_reason():
    c = DC.build("这些都做进 aiw_news.py", "", PV.ProvenanceBook(), {}, script_runs=[])
    assert c["source_of_truth"] is None and c["confirm"] and "aiw_news.py" in c["confirm_reason"]
    assert c["unresolved_sources"] == ["aiw_news.py"]


def test_parent_contract_carries_source():
    prev = {"source_of_truth": SRC, "generator_cmd": "cd ~/ai-workstation/news && python3 aiw_news.py --no-push",
            "deliverables": ["~/aiw-news/2026-11-08.html"]}
    c = DC.build("继续", "", PV.ProvenanceBook(), {}, prev=prev)
    assert c["source_of_truth"] == SRC and c["source_how"] == "parent"
    assert c["generator_cmd"].endswith("--no-push") and c["deliverables"] == prev["deliverables"]


def test_continuation_parent_contract_lookup():
    from aiw_api import continuation as CT

    class St(KV):
        def continuations(self, root):
            return [{"id": "task-1689"}, {"id": "task-68b"}]

        def task(self, tid):
            return {"id": tid}
    st = St({"task_contract:task-1689": {"source_of_truth": SRC, "generator_cmd": "g", "goal": "x"}})
    pc = CT.parent_contract(st, {"id": "task-68b", "continues": "task-1689"})
    assert pc["source_of_truth"] == SRC and pc["from_task"] == "task-1689" and "goal" not in pc
    assert CT.parent_contract(St(), {"id": "task-68b", "continues": "task-1689"}) == {}
    sess = (ROOT / "services/api/aiw_api/sessions.py").read_text()
    assert 'meta["parent_contract"] = _cont.parent_contract(self.store, row)' in sess


def test_scan_parses_cd_and_python_and_flat_events():
    from aiw_api import devflow as AD

    class St:
        def thread_tasks(self, tid):
            return [{"id": "task-1689", "session_id": "s1"}]

        def events(self, sid, limit=3000):
            return [{"type": "agent.decision", "step": 5, "tool": "shell",
                     "args": {"command": "cd ~/ai-workstation/news && timeout 560 python3 aiw_news.py --no-push | tail -4"}},
                    {"type": "agent.action_result", "step": 5, "tool": "shell", "success": True, "result": {"stdout": ""}},
                    {"type": "agent.decision", "step": 6, "data": {"tool": "shell", "args": {"command": "python3 gen2.py",
                                                                                             "cwd": "~/proj"}}},
                    {"type": "agent.action_result", "step": 6, "data": {"tool": "shell", "success": True, "result": {}}}]
    out = AD._scan(St(), "thr", set())
    assert SRC in out["script_runs"] and "~/proj/gen2.py" in out["script_runs"] and SRC in out["repo_paths"]


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout


def _op(op, args, env=None):
    out = subprocess.run(["bash", "-c", GO.command(op, args)], capture_output=True, text=True, timeout=60,
                         env={**os.environ, **(env or {})}).stdout
    return GO.parse(out)


def test_git_untracked_source_in_platform_repo_snapshots_by_copy(tmp_path):
    repo = tmp_path / "ai-workstation"
    (repo / "services/agent-runtime/agent_runtime").mkdir(parents=True)
    (repo / "news").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], check=True)
    src = repo / "news" / "aiw_news.py"
    src.write_text("STYLE='old'\n")
    env = {"AIW_SNAPSHOT_DIR": str(tmp_path / "snaps")}
    g = _op("start", {"path": str(src), "task": "task-68b"}, env)
    assert g["ok"] and g["mode"] == "copy" and g["reason"] == "platform_repo"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"     # the platform's branch untouched
    st = {k: g.get(k) for k in GO.STATE_KEYS}
    assert _op("status", st, env)["changed"] is False
    src.write_text("STYLE='new'\nX=1\n")
    s = _op("status", st, env)
    assert s["changed"] and "+2 -1" in s["stat"]
    assert _op("commit", st, env)["committed"] is False
    rb = _op("rollback", st, env)
    assert rb["ok"] and src.read_text() == "STYLE='old'\n"
    assert (repo / "news" / "aiw_news.py.aiw-before-rollback").read_text().startswith("STYLE='new'")
    assert "快照" in GO.summary_line({**g, "stat": s["stat"]})


def test_git_untracked_in_ordinary_repo_also_copy(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], check=True)
    (repo / "gen.py").write_text("x")
    g = _op("start", {"path": str(repo / "gen.py"), "task": "t1"}, {"AIW_SNAPSHOT_DIR": str(tmp_path / "s")})
    assert g["mode"] == "copy" and g["reason"] == "untracked"


# ------------------------------------------------------------------------------------------- 5. specific stuck msgs
def test_stuck_pattern_names_unexecuted_scripts():
    d = _flow()
    for i, st in enumerate((1, 2, 3, 4)):
        body = FIX_SCRIPT.replace("p.id===h", f"p.id===h{i}").replace('old = \'if(p.id==="p-"+h)\'', f"old = 'marker_{i}_xyz'")
        d.post_tool(st, "files_write", {"path": f"tasks/t/fix_{i}.py", "content": body}, True)
    d.pre_tool(9, "shell", {"command": "ls"})
    pats = d.stuck_pattern()
    kinds = {k: (t, sv) for k, t, sv in pats}
    assert "unexecuted" in kinds and "写了 4 个脚本却没有执行" in kinds["unexecuted"][0] and kinds["unexecuted"][1] == "ask"
    assert d.stuck_ask()
    det = ES.dev_stuck_detail("早报改版", 12, d.stuck_text())
    assert "写了 4 个脚本" in det and "12 步" in det
    note = ES.dev_rabbit_note(d.stuck_text(), SRC)
    assert "STUCK PATTERN" in note and "files_patch" in note and SRC in note


def test_stuck_pattern_alternating_approaches():
    d = _flow()
    seq = [("files_patch", {"path": SRC, "old": "a", "new": "b"}),
           ("shell", {"command": f"sed -i 's/a/b/' {SRC}"}),
           ("files_patch", {"path": SRC, "old": "c", "new": "d"}),
           ("shell", {"command": f"sed -i 's/c/d/' {SRC}"})]
    for i, (t, a) in enumerate(seq, 1):
        d.post_tool(i, t, a, True)
    k = {x[0]: x[1] for x in d.stuck_pattern()}
    assert "alternating" in k and "来回切换" in k["alternating"]


def test_loop_dev_rabbit_uses_concrete_pattern():
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "_es.dev_stuck_detail(item, since, pattern)" in src and "self.dev.stuck_ask()" in src
    assert "_es.dev_rabbit_note(pattern" in src


def test_findings_superseded_by_new_number_and_expired_on_change():
    b = FD.FindingsBoard(capture_thoughts=False)
    b.add("aiw_news.py 源码共 303 行", 2, "model")
    st, e = b.add("aiw_news.py 源码共 639 行", 9, "model")
    assert st == "replaced" and [i["text"] for i in b.live()] == ["aiw_news.py 源码共 639 行"]
    b.add("GitHub 接口未登录时 403", 10, "result")
    gone = b.expire_about(SRC, 12)
    assert gone == [e["id"]] and [i["text"] for i in b.live()] == ["GitHub 接口未登录时 403"]
    assert "639" not in b.block()


# ------------------------------------------------------------------------------------------- 6. requirement checks
def test_requirement_checks_from_task68b_request():
    ids = {r["id"]: r for r in DC.requirement_checks(G68B)}
    assert {"dark_mode", "font", "images", "back_home", "size", "no_font_embed"} <= set(ids)
    assert ids["images"]["min"] == 2 and "Songti" in ids["font"]["any_of"]
    assert any(r["kind"] == "text" and r["contains"] == "GitHub 热榜" for r in ids.values())
    assert any(r["kind"] == "text" and r["contains"] == "新榜" for r in ids.values())
    assert "sel_gh" in {r["id"] for r in DC.requirement_checks("加一个 #gh 区块")}
    c = DC.build(G68B, "", PV.ProvenanceBook(), {}, script_runs=[SRC])
    assert c["req_checks"] and "需求检查" in DC.block(c) and "files_patch" in DC.block(c)


PAGE = """<html><head><style>body{background:#fff;color:#111}@media (prefers-color-scheme: dark){body{background:#111}}
h1{font-family:"Songti SC",SimSun,serif}</style></head><body><h1>早报</h1>
<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
<div style="background-image:url(x.png)">x</div><section id="gh">GitHub热榜</section><a>← 返回首页</a></body></html>"""


@pytest.mark.skipif(VH.find_browser() is None, reason="no chromium")
def test_verify_html_evaluates_requirement_checks(tmp_path, monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    f = tmp_path / "p.html"
    f.write_text(PAGE)
    r = VH.run({"path": str(f), "req_checks": DC.requirement_checks(G68B), "timeout": 60})
    got = {c["id"]: c["ok"] for c in r["checks"]}
    assert got["dark_mode"] and got["font"] and got["images"] and got["back_home"] and got["size"] and got["no_font_embed"]
    assert got["text_GitHub_热榜"] and not got["text_新榜"] and r["checks_ok"] is False
    assert r["ok"]   # requirement checks are reported separately; they do not flip the page check


def test_verify_html_static_checks_without_browser(tmp_path):
    f = tmp_path / "p.html"
    f.write_text("<style>@font-face{font-family:x;src:url(data:font/woff2;base64,AAAA)}</style>" + "x" * 100)
    res = VH.eval_checks(None, [{"id": "size", "kind": "max_bytes", "max": 50},
                                {"id": "nf", "kind": "no_embedded_font"}], "file://" + str(f))
    assert [c["ok"] for c in res] == [False, False]


def _done_ready(d, checks):
    d.post_tool(3, "files_patch", {"path": SRC, "old": "a", "new": "b"}, True)
    d.post_tool(4, "shell", {"command": "cd ~/ai-workstation/news && python3 aiw_news.py --no-push"}, True,
                '{"ok": true, "file": "/home/aiw/aiw-news/2026-11-08.html"}')
    d.record_verify(5, {"ok": True, "checks": checks})


def test_done_gate_requires_checks_run_after_last_change():
    d = _flow("改成深色模式，做进 aiw_news.py")
    d.post_tool(3, "files_patch", {"path": SRC, "old": "a", "new": "b"}, True)
    d.post_tool(4, "shell", {"command": "cd ~/ai-workstation/news && python3 aiw_news.py"}, True)
    d.record_verify(5, {"ok": True})
    ok, note, msg = d.done_gate(True, [], "深色模式已完成。已知问题：无")
    assert not ok and "需求检查没有在最后一次改动后跑过" in msg


def test_done_gate_rejects_contradicting_claim_and_unlisted_failure():
    d = _flow("改成深色模式，标题用宋体，做进 aiw_news.py")
    _done_ready(d, [{"id": "dark_mode", "ok": False, "detail": "light==dark", "label": "深色/浅色随系统切换"},
                    {"id": "font", "ok": True, "label": "标题字体：宋体"}])
    ok, note, msg = d.done_gate(True, [], "完成：深色/浅色随系统切换，标题宋体。已知问题：无")
    assert not ok and "总结声称完成，但检查未通过" in msg and "light==dark" in note
    d2 = _flow("改成深色模式，标题用宋体，做进 aiw_news.py")
    _done_ready(d2, [{"id": "dark_mode", "ok": False, "detail": "light==dark"}, {"id": "font", "ok": True}])
    ok, _, msg = d2.done_gate(True, [], "完成：标题宋体。已知问题：无")
    assert not ok and "没有列进已知问题" in msg


def test_done_gate_accepts_failure_listed_as_known_issue_and_requires_statuses():
    d = _flow("改成深色模式，标题用宋体，做进 aiw_news.py")
    _done_ready(d, [{"id": "dark_mode", "ok": False, "detail": "light==dark"}, {"id": "font", "ok": True}])
    ok, _, msg = d.done_gate(True, [], "源头已改并重新生成。已知问题：无")
    assert not ok and ("没有列进已知问题" in msg)
    d = _flow("改成深色模式，标题用宋体，做进 aiw_news.py")
    _done_ready(d, [{"id": "dark_mode", "ok": True}, {"id": "font", "ok": True}])
    ok, note, msg = d.done_gate(True, [], "源头已改并重新生成。已知问题：无")
    assert not ok and "每项需求检查的结果" in msg
    d = _flow("改成深色模式，标题用宋体，做进 aiw_news.py")
    _done_ready(d, [{"id": "dark_mode", "ok": False, "detail": "light==dark"}, {"id": "font", "ok": True}])
    ok, _, _ = d.done_gate(True, [], "标题宋体：通过。\n已知问题：\n- 深色模式未通过：CSS 变量没生效，原因是缓存旧模板")
    assert ok
    assert "需求检查（verify_html）" in d.known_tail() and "✗ 未通过" in d.known_tail()


# ------------------------------------------------------------------------------------------- 7. fast generator
def test_fast_cmd_and_full_run_before_done():
    assert DC.fast_cmd("cd ~/n && python3 aiw_news.py --no-push", ["--from-cache"]) == \
        "cd ~/n && python3 aiw_news.py --from-cache --no-push"
    assert DC.fast_cmd("python3 g.py", []) is None and DC.is_fast_run("python3 g.py --from-cache")
    d = _flow("修一下 aiw_news.py 的详情页")
    d.contract["generator_cmd_fast"] = "cd ~/ai-workstation/news && python3 aiw_news.py --from-cache"
    d.post_tool(3, "files_patch", {"path": SRC, "old": "a", "new": "b"}, True)
    d.post_tool(4, "shell", {"command": "cd ~/ai-workstation/news && python3 aiw_news.py --from-cache"}, True)
    d.record_verify(5, {"ok": True})
    assert d.fast_runs == [4] and not d.gen_runs
    ok, note, msg = d.done_gate(True, [], "完成。已知问题：无")
    assert not ok and "完整运行一次" in msg
    d.post_tool(6, "shell", {"command": "cd ~/ai-workstation/news && python3 aiw_news.py"}, True)
    d.record_verify(7, {"ok": True})
    assert d.done_gate(True, [], "完成。已知问题：无")[0]
    assert "generator_cmd_fast" in DC.block(d.contract) or "快速生成" in DC.block(d.contract)


def test_readme_documents_v3929():
    r = (ROOT / "README.md").read_text()
    assert "v3.9.29" in r and "generator_cmd_fast" in r and "files_patch" in r and "autoroute.dev.model" in r
    assert "v3.9.29" in (ROOT / "services/api/aiw_api/main.py").read_text()
