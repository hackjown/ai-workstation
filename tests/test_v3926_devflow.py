"""v3.9.26 (task-830): the built-in dev/fix workflow.

task-830 (thread thr-8d8cfb0ce9, continues task-7bb0c3449c): "改了样式点击又进入不了详情，GitHub也没了，修复" after a
"run ~/ai-workstation/news/aiw_news.py, 不要修改脚本" task. The agent patched the generated ~/aiw-news/<date>.html with
ad-hoc inject3.py / restyle scripts for 98 steps, repeated one diagnosis ~10 times, fought headless chrome / CDP for
15 minutes and was marked succeeded with the new style probably lost.
"""
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from agent_runtime import dev_contract as DC
from agent_runtime import devflow as DF
from agent_runtime import escalate as ES
from agent_runtime import fingerprint as FP
from agent_runtime import git_ops as GO
from agent_runtime import intent as IN
from agent_runtime import provenance as PV
from agent_runtime import verify_html as VH
from agent_runtime.control import AgentControl, stop_text

ROOT = pathlib.Path(__file__).resolve().parents[1]
T830 = "改了样式点击又进入不了详情，GitHub也没了，修复"
OLD830 = "运行 ~/ai-workstation/news/aiw_news.py 生成今天的科技新闻页面，不要修改脚本"
ART = "/home/aiw/aiw-news/2026-11-05.html"
GEN_CMD = "cd ~/ai-workstation/news && python3 aiw_news.py"
GEN_OUT = 'done\n{"file": "/home/aiw/aiw-news/2026-11-05.html"}'


def page(gh=True, cards=4, script=True) -> bytes:
    gh_html = ('<section id="gh"><h2>GitHub</h2>' + "".join(
        f'<a data-gh="{i}" href="https://github.com/r{i}">r{i}</a>' for i in range(5)) + "</section>") if gh else ""
    cards_html = "".join(f'<div class="card" data-id="{i}" onclick="openDetail({i})">n{i}</div>' for i in range(cards))
    js = "<script>function openDetail(i){var o=document.getElementById('ov');o.classList.add('on');o.textContent='detail '+i}</script>" if script else ""
    return (f"<!doctype html><html><head><style>.ov{{display:none}}.ov.on{{display:block}}</style></head><body>"
            f"<section id=\"news\">{cards_html}</section>{gh_html}<div id=\"ov\" class=\"ov\"></div>{js}</body></html>").encode()


def book830() -> PV.ProvenanceBook:
    b = PV.ProvenanceBook()
    b.record(ART, PV.producer_for(ART, [{"step": 2, "command": GEN_CMD, "stdout": GEN_OUT}], "/home/aiw"), "task-7bb0c3449c",
             "/home/aiw")
    return b


# ------------------------------------------------------------------------------------------------ 1. intent
def test_intent_task830_phrase_is_dev_fix():
    sig = {"sent_files": [ART], "script_runs": ["~/ai-workstation/news/aiw_news.py"], "repo_paths": [], "provenance": {}}
    r = IN.classify(T830, sig)
    assert r["intent"] == IN.DEV_FIX and not r["forced"]
    assert IN.classify(T830, {})["intent"] == IN.DEV_FIX          # 样式 / 点击 / 详情 are code words by themselves


def test_intent_negation_and_run():
    assert IN.classify(OLD830, {})["intent"] == IN.RUN               # "不要修改脚本" is a constraint, not a modify verb
    assert IN.classify("不要修改脚本，检查一下每条新闻的字段是否完整", {"script_runs": ["x.py"]})["intent"] != IN.DEV_FIX
    assert "修改" not in IN.strip_negated("不要修改脚本")
    assert IN.classify("别改代码，直接再跑一遍生成", {"script_runs": ["gen.py"]})["intent"] == IN.RUN


def test_intent_forced_tag_and_english():
    r = IN.classify("【技能：dev】做一个记账小程序", {})
    assert r["intent"] == IN.DEV_FIX and r["forced"]
    assert IN.classify("the detail link is broken, fix it", {"sent_files": ["a.html"]})["intent"] == IN.DEV_FIX
    assert IN.classify("帮我订一张明天去上海的机票", {})["intent"] == IN.OTHER


def test_voided_constraints_and_rollback_words():
    v = IN.voided_constraints(OLD830 + "，不要重试", IN.DEV_FIX)
    assert "不要修改脚本" in v and any("重试" in x for x in v)
    assert IN.voided_constraints(OLD830, IN.RUN) == []
    assert IN.is_rollback_request("回滚") and IN.is_rollback_request("撤销刚才的修改") and not IN.is_rollback_request("回滚之后再改一下")


def test_loop_uses_intent_not_tag():
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert "_dev_task(self.task.prompt)" not in src                 # every gate reads self._intent now
    assert "self._intent == _in.DEV_FIX" in src


# ------------------------------------------------------------------------------------------------ 2. continuation
def _store(tmp_path):
    from aiw_api.store import Store
    return Store(str(tmp_path / "db.sqlite"))


def _row(st, tid, prompt, thread, continues=None, source="chat"):
    st.create_task({"id": tid, "prompt": prompt, "machine_id": "ai-linux-001", "status": "succeeded", "thread_id": thread,
                    "source": source, "continues": continues})
    return st.task(tid)


def test_continuation_rewrite_voids_old_constraint(tmp_path):
    from aiw_api import continuation as C
    st = _store(tmp_path)
    th = st.create_thread("news")["id"]
    _row(st, "task-7bb0c3449c", OLD830, th)
    row = _row(st, "task-830", T830, th, continues="task-7bb0c3449c", source="continue")
    p = C.run_prompt(st, row, {"sent_files": [ART], "script_runs": ["~/ai-workstation/news/aiw_news.py"]})
    assert "【任务契约】" in p and f"目标（本次请求）：{T830}" in p
    assert "背景（原任务说明，仅作参考，不是本次目标）" in p
    assert "不要修改脚本（已失效）" in p
    assert p.rstrip().endswith(f"【接着上次继续做】用户补充：{T830}")
    assert p.index("目标（本次请求）") < p.index(OLD830)          # the new request comes first, the old one is background


def test_continuation_same_intent_keeps_concatenation(tmp_path):
    from aiw_api import continuation as C
    st = _store(tmp_path)
    th = st.create_thread("news")["id"]
    _row(st, "task-a", OLD830, th)
    row = _row(st, "task-b", "再生成一份明天的", th, continues="task-a", source="continue")
    p = C.run_prompt(st, row, {"script_runs": ["~/ai-workstation/news/aiw_news.py"]})
    assert p == f"{OLD830}\n\n【接着上次继续做】用户补充：再生成一份明天的"


def test_task_intent_is_persisted(tmp_path):
    from aiw_api import devflow as DA
    st = _store(tmp_path)
    th = st.create_thread("news")["id"]
    _row(st, "task-a", OLD830, th)
    row = _row(st, "task-b", T830, th, continues="task-a", source="continue")
    info = DA.task_intent(st, row, {"sent_files": [ART]})
    assert info["intent"] == "dev_fix" and st.kv_get("task_intent:task-b")["intent"] == "dev_fix"
    st.kv_set("task_intent:task-a", {"intent": "run"})
    retry = _row(st, "task-c", "继续", th, continues="task-a", source="retry")
    assert DA.task_intent(st, retry, {})["intent"] == "run"          # a bare 继续 keeps the task's own intent


# ------------------------------------------------------------------------------------------------ 3. provenance
def test_provenance_finds_generator_and_prefers_it_over_patchers():
    b = book830()
    assert b.get(ART)["script"] == "~/ai-workstation/news/aiw_news.py"
    log = [{"step": 9, "command": "python3 ~/workspace/inject3.py", "stdout": "patched /home/aiw/aiw-news/2026-11-05.html"}]
    b.record(ART, PV.producer_for(ART, log, "/home/aiw"), "task-830", "/home/aiw")
    assert b.get(ART)["script"] == "~/workspace/inject3.py"          # the newest producer …
    assert b.source_of_truth() == "~/ai-workstation/news/aiw_news.py"   # … but the source of truth is the generator
    assert PV.producer_for("/x/out.html", [{"step": 1, "command": "python3 /x/gen.py -o /x/out.html", "stdout": ""}])["script"] == "/x/gen.py"


@pytest.mark.parametrize("tool,args", [
    ("files_write", {"path": ART, "content": "<html/>"}),
    ("shell", {"command": "sed -i 's/old/new/' ~/aiw-news/2026-11-05.html"}),
    ("shell", {"command": "cd ~/aiw-news && cat x >> 2026-11-05.html"}),
    ("shell", {"command": "python3 - <<'EOF'\np='/home/aiw/aiw-news/2026-11-05.html'\ns=open(p).read()\nopen(p,'w').write(s.replace('a','b'))\nEOF"}),
])
def test_provenance_blocks_artifact_edits(tool, args):
    hit = PV.artifact_edit(tool, args, book830())
    assert hit and hit[1] == "~/ai-workstation/news/aiw_news.py"
    assert "这是 ~/ai-workstation/news/aiw_news.py 生成的产物" in PV.refuse_text(*hit)


def test_provenance_allows_source_edits_and_generator_runs():
    b = book830()
    for tool, args in [("shell", {"command": GEN_CMD}), ("shell", {"command": "head -50 ~/aiw-news/2026-11-05.html"}),
                       ("files_write", {"path": "/home/aiw/ai-workstation/news/aiw_news.py", "content": "x"}),
                       ("shell", {"command": "sed -i 's/a/b/' ~/ai-workstation/news/aiw_news.py"})]:
        assert PV.artifact_edit(tool, args, b) is None, args


def test_dev_flow_refuses_patch_script_and_warns_adhoc():
    d = DF.DevFlow("dev_fix", book830())
    d.contract = DC.build(T830, "", d.book)
    src = "p='/home/aiw/aiw-news/2026-11-05.html'\nh=open(p).read()\nopen(p,'w').write(h)\n"
    v, warn = d.pre_tool(1, "files_write", {"path": "inject3.py", "content": src})
    assert v is None and "ad-hoc patch script" in warn                 # created: a warning
    v, note = d.pre_tool(2, "shell", {"command": "cd ~/workspace && python3 inject3.py"})
    assert v == "skip" and "生成的产物" in note                        # run against the artifact: refused
    assert DF.DevFlow("run", book830()).pre_tool(1, "files_write", {"path": ART, "content": "x"}) == (None, None)


# ------------------------------------------------------------------------------------------------ 4. contract
def test_contract_task830_points_at_the_generator():
    base = {PV.norm(ART): FP.fingerprint(page(), "2026-11-05.html")}
    c = DC.build(IN.voided_constraints and ("【任务契约】\n目标（本次请求）：" + T830 + "\n\n背景：" + OLD830), "", book830(), base,
                 OLD830)
    assert c["goal"] == T830
    assert c["source_of_truth"] == "~/ai-workstation/news/aiw_news.py"
    assert c["deliverables"] == ["~/aiw-news/2026-11-05.html"] and c["generator_cmd"] == GEN_CMD
    assert "不要修改脚本" in c["voided_constraints"]
    assert any("data-gh" in m for m in c["must_keep"]) and any("section#gh" in m for m in c["must_keep"])
    assert any("GitHub" in m for m in c["must_keep"])                  # "GitHub也没了"
    assert any("详情" in a for a in c["acceptance"]) and "[data-gh]" in c["checks"]["selectors"]
    assert not c["confirm"]                                            # a small fix: no card
    blk = DC.block(c)
    assert "【任务契约】" in blk and "aiw_news.py" in blk and "已失效" in blk


def test_contract_without_source_asks():
    c = DC.build("页面点不进去了，修复一下", "", PV.ProvenanceBook(), {})
    assert c["confirm"] and c["source_of_truth"] is None
    q, opts = DC.card(c)
    assert "源头" in q and len(opts) == 3


# ------------------------------------------------------------------------------------------------ 5. fingerprint
def test_fingerprint_diff_names_what_was_lost():
    old, new = FP.fingerprint(page(), "a.html"), FP.fingerprint(page(gh=False, script=False), "a.html")
    assert old["data_attrs"]["data-gh"] == 5 and "section#gh" in old["section_ids"] and old["handlers"]["onclick"] == 4
    keys = {x["key"] for x in FP.diff(old, new)}
    assert {"data-gh", "section#gh", "github.com", "script"} <= keys
    assert FP.diff(old, FP.fingerprint(page(), "a.html")) == []
    assert "data-gh" not in {x["key"] for x in FP.diff(old, new, allow=["gh"])}   # the user asked to remove it


def test_fingerprint_runs_on_the_computer(tmp_path):
    f = tmp_path / "p.html"
    f.write_bytes(page())
    out = subprocess.run(["bash", "-c", FP.remote_command(str(f))], capture_output=True, text=True, timeout=30).stdout
    lines = [x for x in out.splitlines() if x.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["data_attrs"]["data-gh"] == 5


# ------------------------------------------------------------------------------------------------ 6. done gate
def _flow():
    d = DF.DevFlow("dev_fix", book830(), {PV.norm(ART): FP.fingerprint(page(), "2026-11-05.html")})
    d.contract = DC.build(T830, "", d.book, d.baselines)
    return d


def test_done_gate_rejects_no_source_change():
    d = _flow()
    ok, note, msg = d.done_gate(False, [])
    assert not ok and "源头没有真实改动" in msg and "REJECTED" in note


def test_done_gate_rejects_artifact_not_regenerated():
    d = _flow()
    d.post_tool(3, "shell", {"command": "sed -i 's/old/new/' ~/ai-workstation/news/aiw_news.py"}, True)
    assert d.source_writes and d.last_source_step == 3
    ok, note, msg = d.done_gate(None, [])
    assert not ok and "没有重新生成" in msg and GEN_CMD in note
    d.post_tool(4, "shell", {"command": GEN_CMD}, True, GEN_OUT)
    d.post_tool(5, "shell", {"command": "cd ~/ai-workstation && python3 -m pytest -q"}, True)
    assert d.done_gate(True, []) == (True, None, None)


def test_done_gate_regression_lost_data_gh_and_section():
    d = _flow()
    d.post_tool(3, "files_write", {"path": "/home/aiw/ai-workstation/news/aiw_news.py", "content": "x"}, True)
    d.post_tool(4, "shell", {"command": GEN_CMD}, True, GEN_OUT)
    d.record_verify(5, {"ok": True})
    losses = FP.diff(d.baseline(), FP.fingerprint(page(gh=False), "2026-11-05.html"))
    ok, note, msg = d.done_gate(True, losses)
    assert not ok and "data-gh" in msg and "section#gh" in note


def test_done_gate_is_bounded_then_records_known_issues(monkeypatch):
    monkeypatch.setenv("AIW_DEV_DONE_REJECTS", "2")
    d = _flow()
    assert not d.done_gate(False, [])[0] and not d.done_gate(False, [])[0]
    assert d.done_gate(False, [])[0]                                   # third time: through …
    assert "已知问题" in d.known_tail() and "源头没有真实改动" in d.known_tail()   # … with 已知问题


# ------------------------------------------------------------------------------------------------ 7. loop guards
def test_dev_rabbit_threshold(monkeypatch):
    assert ES.dev_rabbit_steps() == 15 and ES.rabbit_steps() == 40
    monkeypatch.setenv("AIW_DEV_RABBIT_STEPS", "9")
    assert ES.dev_rabbit_steps() == 9
    assert "dev_rabbit" in ES.KIND_TEXT


def test_repeated_diagnosis_detector():
    d = _flow()
    cmds = ["grep -n openDetail ~/ai-workstation/news/aiw_news.py", "sed -n 1,80p ~/ai-workstation/news/aiw_news.py",
            "cat ~/ai-workstation/news/aiw_news.py | grep -n gh"]
    notes = [d.post_tool(i, "shell", {"command": c}, True, "x") for i, c in enumerate(cmds, 1)]
    assert notes[:2] == [None, None] and notes[2] and "已确认结论" in notes[2] and "下一步必须动手修改" in notes[2]
    d2 = _flow()
    d2.post_tool(1, "shell", {"command": cmds[0]}, True)
    d2.post_tool(2, "files_write", {"path": "/home/aiw/ai-workstation/news/aiw_news.py", "content": "x"}, True)
    d2.post_tool(3, "shell", {"command": cmds[1]}, True)
    assert d2.post_tool(4, "shell", {"command": cmds[2]}, True) is None   # a write in between resets it


def test_verification_circuit_breaker():
    d = _flow()
    c = {"command": "chromium --headless --screenshot=/tmp/a.png file:///home/aiw/aiw-news/2026-11-05.html"}
    assert d.post_tool(1, "shell", c, False, "", "timeout after 60s") is None
    n = d.post_tool(2, "shell", c, True, "Error: cannot connect", "")
    assert n and "verify_html" in n
    v, note = d.pre_tool(3, "shell", c)
    assert v == "skip" and "verify_html" in note
    cdp = {"command": "curl -s http://127.0.0.1:9222/json/list"}
    assert d.pre_tool(4, "shell", cdp)[0] is None                       # another approach is still allowed


@pytest.mark.parametrize("cmd,want", [
    ("chromium --headless --screenshot=a.png file:///x.html", "timeout -k 5 120 chromium"),
    ("cd ~/w && google-chrome --headless=new --dump-dom x", "&& timeout -k 5 120 google-chrome"),
    ("soffice --headless --convert-to pdf a.docx", "timeout -k 5 120 soffice"),
    ("nohup chromium --headless --remote-debugging-port=9222 &", "nohup timeout -k 5 120 chromium"),
])
def test_render_commands_get_a_timeout(cmd, want):
    assert want in ES.wrap_timeout(cmd)


@pytest.mark.parametrize("cmd", ["timeout 30 chromium --headless x", "pkill -f soffice; true", "which chromium",
                                 "soffice ~/workspace/a.docx &", "echo chrome is nice"])
def test_render_wrap_leaves_others_alone(cmd):
    assert ES.wrap_timeout(cmd) == cmd


def test_findings_only_from_results_in_dev():
    from agent_runtime.findings import FindingsBoard
    b = FindingsBoard(capture_thoughts=False)
    assert b.capture("已查清：主因是 openDetail 被删了，结论是 X1 版本", 3) == (0, 0)
    st, e = b.from_result("shell", {"command": "python3 aiw_news.py"}, False, "Traceback\nNameError: name 'gh' is not defined", 4)
    assert st == "added" and e["source"] == "result" and "NameError" in e["text"]
    assert FindingsBoard().capture("已查清：主因是 openDetail 被删了，版本 X1", 3)[0] == 1   # other tasks unchanged


# ------------------------------------------------------------------------------------------------ 8. routing
def test_autoroute_dev_goes_to_strongest_with_thinking(tmp_path, monkeypatch):
    from aiw_api import autoroute as AR
    monkeypatch.delenv("AIW_AUTOROUTE", raising=False)
    monkeypatch.delenv("AIW_DEV_ROUTE", raising=False)
    st = _store(tmp_path)
    st.kv_set("models", {"model": "deepseek-v4.1-flash", "strong_model": "claude-opus-4"})
    r = asyncio.run(AR.decide(st, T830, intent="dev_fix"))
    assert r["by"] == "dev" and r["model"] == "claude-opus-4" and r["effort"] == "high"
    assert asyncio.run(AR.decide(st, "帮我查天气")) is None            # routing off: nothing else changes
    assert asyncio.run(AR.decide(st, T830, manual_model="gpt-x", intent="dev_fix"))["by"] == "manual"
    assert asyncio.run(AR.decide(st, "继续", long_run=True))["by"] == "dev"
    AR.save(st, {"dev": {"model": "qwen-max", "effort": "medium"}})
    r = asyncio.run(AR.decide(st, T830, intent="dev_fix"))
    assert r["model"] == "qwen-max" and r["effort"] == "medium"
    monkeypatch.setenv("AIW_DEV_ROUTE", "0")
    assert asyncio.run(AR.decide(st, T830, intent="dev_fix")) is None


# ------------------------------------------------------------------------------------------------ 9. cad_mode
def test_cad_mode_task04d_not_revise():
    from agent_runtime.cad import cad_mode
    ctx = "EARLIER IN THIS CONVERSATION:\n- user: 打开 LibreCAD 画施工图\n  result (succeeded): cad/荣誉室施工图.dxf"
    p04d = "运行 ~/ai-workstation/news/aiw_news.py 生成今天的新闻，不要修改脚本，检查每条新闻的字段是否完整，有问题告诉我"
    assert cad_mode(p04d, ctx) is None
    assert cad_mode(p04d, ctx, history=True) is None
    assert cad_mode("第7张也一样", ctx, history=False) is None            # stored history says: no drawing in this thread
    assert cad_mode("第7张也一样", ctx, history=True) == "revise"
    assert cad_mode("图5的门也要跟图4一样", ctx, source="dispatch") is None   # a dispatched child is never a revision
    assert cad_mode("把字段改成中文", "施工图 立面图 平面图（只是聊天里提到过）") is None   # words are not CAD history


# ------------------------------------------------------------------------------------------------ 10. stop source
def test_control_stop_source():
    c = AgentControl()
    c.stop("parent", "主任务 t1 停止")
    c.stop("user")                                                     # the first stop says who did it
    assert c.stop_source == "parent" and c.stop_reason == "主任务 t1 停止"
    assert stop_text("user") == "stopped by user"
    assert stop_text("parent", "x") == "stopped by parent task: x"
    assert stop_text("send_now").startswith("stopped: a queued")
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert '_finish(TaskStatus.STOPPED, "stopped by user")' not in src


from test_v11_features import stack, wait_task  # noqa: E402,F401  (fixture)


async def test_stop_source_recorded(stack):
    client, backend, scripts, app, providers, tmp = stack
    scripts.append([("shell", {"command": "sleep 20"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "运行一个很慢的命令"})).json()
    sessions = app.state.sessions
    for _ in range(100):
        live = sessions.live_for_task(r["task"]["id"])
        if live and live.loop is not None and live.loop.step >= 1:
            break
        await asyncio.sleep(0.05)
    await sessions.control(live.id, "stop", source="parent", reason="主任务停止")
    t = await wait_task(client, r["task"]["id"])
    assert t["status"] == "stopped" and t["result"].startswith("stopped by parent task")
    assert app.state.store.kv_get(f"task_stop:{t['id']}")["source"] == "parent"
    evs = app.state.store.events(t["session_id"], limit=500)
    assert any(e["type"] == "task.failed" and (e.get("data") or {}).get("by") == "parent" for e in evs)
    assert t.get("thread_id") not in sessions.held_threads             # only the user's own stop holds the queue
    th = (await client.get(f"/api/threads/{t['thread_id']}")).json()
    assert th["tasks"][0]["stop"]["source"] == "parent"


# ------------------------------------------------------------------------------------------------ 11. git
def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


def _op(op, args):
    out = subprocess.run(["bash", "-c", GO.command(op, args)], capture_output=True, text=True, timeout=60).stdout
    return GO.parse(out)


def test_git_snapshot_commit_rollback(tmp_path):
    repo = tmp_path / "proj"
    (repo / "news").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "news" / "gen.py").write_text("STYLE='old'\n")
    (repo / "notes.txt").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "notes.txt").write_text("v1 + user's uncommitted edit\n")    # a dirty tree: must never be lost
    st = _op("start", {"path": str(repo / "news" / "gen.py"), "task": "task-830"})
    assert st["ok"] and st["base"] == base and st["branch"] == "aiw/task-830" and st["orig_branch"] == "main" and st["snapshot"]
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "aiw/task-830"
    assert (repo / "notes.txt").read_text().startswith("v1 + user")      # the working tree was not touched
    assert not _op("status", {**st, "paths": [str(repo / "news")]})["changed"]
    (repo / "news" / "gen.py").write_text("STYLE='new'\n")
    s2 = _op("status", {**st, "paths": [str(repo / "news")]})
    assert s2["changed"] and "news/gen.py" in s2["files"]
    cm = _op("commit", {**st, "message": "aiw: 修复详情点击", "paths": [str(repo / "news")]})
    assert cm["ok"] and cm["committed"] and "gen.py" in cm["stat"]
    assert _git(repo, "log", "-1", "--format=%s") == "aiw: 修复详情点击"
    assert _git(repo, "show", "--stat", "--format=", "HEAD").count("|") == 1   # only the allowed path was committed
    rb = _op("rollback", {**st, "task": "task-830"})
    assert rb["ok"] and rb["head"] == base and rb["restored_snapshot"]
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (repo / "news" / "gen.py").read_text() == "STYLE='old'\n"
    assert (repo / "notes.txt").read_text().startswith("v1 + user")      # the user's edit is back
    assert "aiw/task-830" in _git(repo, "branch")                        # the fix stays reachable on its branch


def test_git_start_outside_repo(tmp_path, monkeypatch):
    # v3.9.29: outside a repo the source is snapshotted by copy (was: error no_repo, no snapshot at all)
    monkeypatch.setenv("AIW_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    (tmp_path / "x.py").write_text("x")
    g = _op("start", {"path": str(tmp_path / "x.py"), "task": "t"})
    assert g["ok"] and g["mode"] == "copy" and g["reason"] == "not_in_repo"


# ------------------------------------------------------------------------------------------------ 12. verify_html
def test_verify_html_without_browser_is_a_structured_error(tmp_path):
    f = tmp_path / "p.html"
    f.write_bytes(page())
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AIW_CHROME": str(tmp_path / "none")}
    if any(os.path.exists(os.path.join(p, n)) for p in ("/usr/bin", "/bin") for n in VH.NAMES):
        pytest.skip("a system browser is installed")
    out = subprocess.run(["bash", "-c", VH.command({"path": str(f), "timeout": 20})], capture_output=True, text=True,
                         timeout=60, env=env).stdout
    res = VH.parse(out)
    assert res["ok"] is False and res["error"] == "no_browser" and "chromium" in res["hint"]
    assert VH.parse("garbage")["error"] == "no_result"
    missing = VH.parse(subprocess.run(["bash", "-c", VH.command({"path": str(tmp_path / "nope.html")})],
                                      capture_output=True, text=True, timeout=60, env=env).stdout)
    assert missing["error"] == "not_found"


def _working_browser():
    b = VH.find_browser()
    if not b:
        return None
    try:
        r = subprocess.run([b, "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom", "about:blank"],
                           capture_output=True, timeout=20)
        return b if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def test_verify_html_real_run_if_chromium(tmp_path):
    if not _working_browser():
        pytest.skip("no working Chromium in this sandbox")
    f = tmp_path / "p.html"
    f.write_bytes(page())
    spec = {"path": str(f), "selectors": ["[data-gh]", "#gh"], "min_counts": {"[data-gh]": 5},
            "clicks": [{"click": ".card", "index": 1, "expect": "#ov", "min_text": 3}], "screenshot": str(tmp_path / "s.png"),
            "timeout": 60}
    res = VH.parse(subprocess.run(["bash", "-c", VH.command(spec)], capture_output=True, text=True, timeout=120).stdout)
    assert res["ok"], res
    assert res["selectors"]["[data-gh]"] == 5 and res["clicks"][0]["visible"] and os.path.getsize(tmp_path / "s.png") > 0


def test_verify_html_registered_as_tool():
    from tool_router.registry import BY_NAME, LOOP_TOOLS
    from tool_router import tool_schemas
    assert "verify_html" in LOOP_TOOLS and BY_NAME["verify_html"].action is None
    assert "verify_html" in [t["name"] for t in tool_schemas()]


# ------------------------------------------------------------------------------------------------ 13. dispatch budget
def test_dispatch_child_default_budget(tmp_path, monkeypatch):
    from aiw_api import devflow as DA
    st = _store(tmp_path)
    assert DA.dispatch_budget(st) == (150, 3600.0)
    monkeypatch.setenv("AIW_DISPATCH_MAX_STEPS", "80")
    assert DA.dispatch_budget(st)[0] == 80
    st.kv_set("dispatch", {"max_steps": 60, "timeout_s": 900})
    assert DA.dispatch_budget(st) == (60, 900.0)
    src = (ROOT / "services/api/aiw_api/sessions.py").read_text()
    assert "_dfa.dispatch_budget(self.store)" in src


# ------------------------------------------------------------------------------------------------ 14. task-830 replay
GEN_SRC = r'''import json, os, sys
OUT = os.environ.get("AIW_NEWS_OUT") or %r
STYLE = "body{font:14px serif}"
GH = True
def main():
    cards = "".join('<div class="card" data-id="%%d" onclick="openDetail(%%d)">n%%d</div>' %% (i, i, i) for i in range(4))
    gh = ('<section id="gh"><h2>GitHub</h2>' + "".join('<a data-gh="%%d" href="https://github.com/r%%d">r</a>' %% (i, i) for i in range(5)) + '</section>') if GH else ''
    html = ('<!doctype html><html><head><style>' + STYLE + '</style></head><body><section id="news">' + cards + '</section>' + gh +
            '<div id="ov"></div><script>function openDetail(i){document.getElementById("ov").textContent="d"+i}</script></body></html>')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").write(html)
    print("generated")
    print(json.dumps({"file": OUT}))
main()
'''

VMPORT = 18826


@pytest.fixture
async def vm(tmp_path, monkeypatch):
    from vm_agent.computer.desktop import MockBackend
    from vm_agent.server.router import Router
    from vm_agent.server.websocket import serve
    from computer_runtime import Computer, LinuxComputerProvider
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "ws"))
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", VMPORT, Router(MockBackend()), None, ready, stop))
    await ready.wait()
    comp = Computer(LinuxComputerProvider(f"ws://127.0.0.1:{VMPORT}"))
    yield comp, tmp_path
    await comp.provider.close()
    stop.set()
    await t


def _project(tmp):
    """~/ai-workstation (a git repo) with news/aiw_news.py, which generates ~/aiw-news/2026-11-05.html."""
    repo = tmp / "ai-workstation"
    (repo / "news").mkdir(parents=True)
    (repo / "tests").mkdir()
    art = tmp / "aiw-news" / "2026-11-05.html"
    (repo / "news" / "aiw_news.py").write_text(GEN_SRC % str(art))
    (repo / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    gen_cmd = f"cd {repo}/news && python3 aiw_news.py"
    out = subprocess.run(["bash", "-c", gen_cmd], capture_output=True, text=True, check=True).stdout
    book = PV.ProvenanceBook()     # the earlier run (task-7bb0c3449c) generated and sent the page
    book.record(str(art), PV.producer_for(str(art), [{"step": 2, "command": gen_cmd, "stdout": out}]), "task-7bb0c3449c")
    fps = {PV.norm(str(art)): FP.fingerprint(art.read_bytes(), art.name)}
    return repo, art, gen_cmd, book, fps


def _loop830(comp, tmp, script, book, fps, events):
    from agent_runtime import AgentLoop, LoopConfig
    from aiw_api.continuation import contract_prompt
    from aiw_protocols import Task
    from llm_router import LLMRouter, ScriptedProvider
    from tool_router import ToolExecutor

    async def sink(e):
        events.append(e)
    prompt = contract_prompt(OLD830, T830, IN.voided_constraints(OLD830, IN.DEV_FIX))
    task = Task(prompt=prompt, source="continue", meta={"provenance": book.to_dict(), "fingerprints": fps,
                                                       "old_instructions": OLD830})
    llm = ScriptedProvider(script)
    loop = AgentLoop(task, "s830", comp, LLMRouter([llm]), ToolExecutor(comp), emit=sink,
                     config=LoopConfig(max_steps=0, timeout_s=120, make_plan=False, artifacts_dir=str(tmp / "artifacts")))
    return loop, llm


def _msgs(events, typ="agent.validation"):
    return [str((e.data or {}).get("message") or "") for e in events if e.type.value == typ]


async def test_task830_replay_contract_and_artifact_only_patch_is_blocked(vm):
    comp, tmp = vm
    repo, art, gen_cmd, book, fps = _project(tmp)
    before = art.read_bytes()
    events = []
    script = [("shell", {"command": f"head -c 200 {art}"}),
              ("shell", {"command": f"sed -i 's/serif/sans-serif/' {art}"}),            # patch the artifact: refused
              ("files_write", {"path": str(art), "content": "<html>restyled</html>"}),  # refused
              ("done", {"summary": "已修复。已知问题：无"}),                                   # no source change: rejected
              ("done", {"summary": "已修复。已知问题：无"}),
              ("done", {"summary": "已修复。已知问题：无"}),
              ("done", {"summary": "已修复。已知问题：无"})]                                 # bounded: through, with 已知问题
    loop, llm = _loop830(comp, tmp, script, book, fps, events)
    assert loop._intent == IN.DEV_FIX
    task = await loop.run()
    c = loop.dev.contract
    assert c["source_of_truth"] == PV.norm(str(repo / "news" / "aiw_news.py"))
    assert c["deliverables"] == [PV.norm(str(art))] and "不要修改脚本" in c["voided_constraints"]
    assert art.read_bytes() == before                                   # the generated page was never patched
    msgs = _msgs(events)
    assert sum("生成的产物" in m for m in msgs) == 2
    assert sum("源头没有真实改动" in m for m in msgs) == 3
    assert "【任务契约】" in llm.requests[1].task and "aiw_news.py" in llm.requests[1].task
    assert task.status.value == "succeeded" and "已知问题（系统的开发流程检查没有通过" in task.result
    assert "Git：分支 aiw/" in task.result                              # the snapshot branch was made
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("aiw/")


async def test_task830_replay_regression_names_the_lost_github_section(vm, monkeypatch):
    monkeypatch.setenv("AIW_DEV_DONE_REJECTS", "1")
    comp, tmp = vm
    repo, art, gen_cmd, book, fps = _project(tmp)
    events = []
    gen = repo / "news" / "aiw_news.py"
    script = [("grep", {"pattern": "GH", "path": str(gen)}),                          # v3.9.30: read before edit
              ("shell", {"command": f"sed -i 's/^GH = True/GH = False/' {gen}"}),     # the restyle drops GitHub
              ("shell", {"command": gen_cmd}),
              ("shell", {"command": f"cd {repo} && python3 -m unittest discover -s tests -q"}),
              ("done", {"summary": "样式已改好。已知问题：无"}),
              ("done", {"summary": "样式已改好。已知问题：无"})]
    loop, _ = _loop830(comp, tmp, script, book, fps, events)
    task = await loop.run()
    msgs = _msgs(events)
    lost = next(m for m in msgs if "丢失" in m)
    assert "data-gh" in lost and "section#gh" in lost
    assert "已知问题" in task.result and "data-gh" in task.result     # let through after the bound, but recorded


async def test_task830_replay_real_fix_passes_commits_and_rolls_back(vm):
    comp, tmp = vm
    repo, art, gen_cmd, book, fps = _project(tmp)
    base = _git(repo, "rev-parse", "HEAD")
    events = []
    gen = repo / "news" / "aiw_news.py"
    script = [("read_file", {"path": str(gen), "offset": 1, "limit": 5}),            # v3.9.30: read before edit
              ("shell", {"command": f"sed -i 's/font:14px serif/font:15px sans-serif/' {gen}"}),
              ("shell", {"command": gen_cmd}),
              ("shell", {"command": f"cd {repo} && python3 -m unittest discover -s tests -q"}),
              ("done", {"summary": "新样式已在 aiw_news.py 里改好并重新生成，详情和 GitHub 都在。已知问题：无"})]
    loop, _ = _loop830(comp, tmp, script, book, fps, events)
    task = await loop.run()
    assert task.status.value == "succeeded", task.result
    assert not any("拒绝 done" in m for m in _msgs(events))
    assert "提交" in task.result and "aiw_news.py" in task.result     # commit id + diff --stat in the summary
    g = loop.dev.git
    assert g["commit"] and _git(repo, "log", "-1", "--format=%s").startswith("aiw: ")
    assert "15px" in art.read_text()                                     # the regenerated page has the new style
    rb = _op("rollback", {**g, "task": loop._root_id})
    assert rb["ok"] and _git(repo, "rev-parse", "HEAD") == base and "14px serif" in gen.read_text()
