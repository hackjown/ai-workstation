"""3.8.5: a continued task is the SAME task (link, plan, steps, files, attachments, computer), follow-ups in the chat
go to their task, 修改任务 / 补充要求, no auto-proceed on an unanswered question, guessed /home/user paths, and
self-evolution (lessons learned per computer / site / skill / tool)."""
import asyncio
import base64
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx
import pytest
from aiw_api import continuation as cont
from aiw_api import lessons as L
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.store import Store
from computer_runtime.providers.base import ActionResult
from llm_router import LLMRouter, ScriptedProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORT = 18797
TERMINAL = ("succeeded", "failed", "stopped", "cancelled", "interrupted")


class FakeVM:
    """A second computer with its own empty disk and a /data/home home (like an InstaBox cloud box)."""

    def __init__(self, mid="cloud-b", home="/data/home"):
        self.machine_id, self.home, self.files, self.calls = mid, home, {}, []

    async def execute(self, action, payload=None, observe=False, timeout=30.0):
        payload = payload or {}
        self.calls.append((action, payload))
        if action == "shell.exec":
            cmd = payload.get("command") or ""
            if cmd.startswith('echo "HOME=$HOME"'):
                lines = [f"HOME={self.home}", f"ROOT={self.home}/workspace"]
                import re
                for rel in re.findall(r'"\$R"/(\S+?); then', cmd):
                    rel = rel.strip("'")
                    lines.append(f"S={len(self.files[rel])}" if rel in self.files else "S=-1")
                return ActionResult(success=True, result={"stdout": "\n".join(lines) + "\n", "exit_code": 0})
            return ActionResult(success=True, result={"stdout": "", "stderr": "", "exit_code": 0})
        if action == "files.upload":
            raw = base64.b64decode(payload.get("content_b64") or "")
            p = payload["path"]
            self.files[p] = (self.files.get(p, b"") + raw) if payload.get("append") else raw
            return ActionResult(success=True, result={"path": p, "size": len(self.files[p])})
        return ActionResult(success=True, result={})

    async def observe(self):
        return None

    async def health(self):
        return ActionResult(success=True, result={})

    async def screenshot(self):
        return ActionResult(success=False, error="no screen")


class QuietSmall:
    """The chat's small model in these tests: routes to chat, learns nothing (so no task script is used up)."""
    name = "quiet"

    async def decide(self, req):
        raise NotImplementedError

    async def complete(self, system, user, max_tokens=800):
        return '{"route": "chat", "title": ""}' if "ROUTE" in system else '{"ops": []}'

    async def chat(self, messages, tools=None, max_tokens=1500, on_delta=None):
        return {"content": "好的", "tool_calls": []}


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", PORT, Router(MockBackend()), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.ask_timeout_s = 0.4
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORT}", desktop_http="http://127.0.0.1:1"),
                  MachineConfig(id="cloud-b", provider="proxmox", provider_vm_id="101",
                                agent_endpoint="ws://127.0.0.1:1", desktop_http="http://127.0.0.1:1")]
    scripts, made = [], []
    fake_vm = FakeVM()

    def llm():
        p = ScriptedProvider(scripts.pop(0) if scripts else [("done", {"summary": "ok"})])
        made.append(p)
        return LLMRouter([p])

    real = {}

    def computers(m):
        if m["id"] == "cloud-b":
            return fake_vm
        from computer_runtime import Computer, LinuxComputerProvider
        real[m["id"]] = Computer(LinuxComputerProvider(m["agent_endpoint"], "agent-secret"), m["id"])
        return real[m["id"]]

    app = build_app(s, llm_factory=llm, computer_factory=computers)
    app.state.chat.llm_factory = lambda role: LLMRouter([QuietSmall()])
    app.state.memory.llm = app.state.chat.llm
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    yield client, app, scripts, made, fake_vm, tmp_path
    for j in list(app.state.chat.jobs) + list(app.state.sessions.jobs):
        j.cancel()
    await client.aclose()
    stop.set(); await t


async def wait_task(client, tid, want=TERMINAL, timeout=15):
    t = {}
    for _ in range(int(timeout / 0.05)):
        t = (await client.get(f"/api/tasks/{tid}")).json()
        if t["status"] in want:
            return t
        await asyncio.sleep(0.05)
    raise AssertionError(f"{tid} stayed {t.get('status')}: {t}")


async def stop_on_question(client, app, tid):
    """The scripted run gives up -> 3.8.2 pauses it with a question -> the user chooses 停止任务."""
    sessions = app.state.sessions
    for _ in range(200):
        live = sessions.live_for_task(tid)
        if live and live.question_info:
            sessions.answer_question(live.id, "停止任务")
            break
        await asyncio.sleep(0.05)
    return await wait_task(client, tid)


def used(made):
    return [p for p in made if p.requests]


def task_text(provider) -> str:
    return provider.requests[0].task if provider.requests else ""


# ------------------------------------------------------------------------------ continuation on another computer
async def test_continue_on_another_computer_keeps_attachments_plan_and_link(stack):
    client, app, scripts, made, fake_vm, tmp = stack
    store = app.state.store
    up = (await client.post("/api/uploads", json={"name": "讲解.pdf", "content_b64": base64.b64encode(b"%PDF-1.4 deck").decode(),
                                                   "machine_id": "ai-linux-001"})).json()
    assert up["upload_id"] and os.path.isfile(up["artifact"])     # kept on the API host, recorded
    scripts.append([("update_plan", {"done": [0]}),
                    ("files_write", {"path": "draft.txt", "content": "第一章"}),
                    ("send_file", {"path": "draft.txt"}),
                    ("fail", {"reason": "步数上限"})])
    prompt = "【技能：huashu-motion】把这份 PPT 做成讲解动画\n\n附件（已放到电脑上）：\n- ~/workspace/uploads/讲解.pdf"
    r = (await client.post("/api/tasks", json={"prompt": prompt})).json()
    root = r["task"]["id"]
    t = await stop_on_question(client, app, root)
    assert t["status"] == "stopped"
    store.set_task_plan(root, {"steps": ["读 PPT", "写脚本", "渲染"], "done": [0]})   # the plan the run had made

    scripts.append([("done", {"summary": "接着做完了"})])
    c = (await client.post(f"/api/tasks/{root}/continue", json={"text": "继续", "machine_id": "cloud-b"})).json()
    run = await wait_task(client, c["task"]["id"])
    assert run["status"] == "succeeded", run
    assert run["continues"] == root and run["thread_id"] == r["thread_id"] and run["machine_id"] == "cloud-b"
    # the fresh computer got the PDF and the file the first run made, at ~/workspace/<rel>
    assert fake_vm.files.get("uploads/讲解.pdf") == b"%PDF-1.4 deck"
    assert fake_vm.files.get("draft.txt") == "第一章".encode()
    text = task_text(used(made)[-1])
    assert "CONTINUING AN EARLIER TASK" in text and "把这份 PPT 做成讲解动画" in text
    assert "/data/home/workspace/uploads/讲解.pdf" in text          # absolute path that resolves on THIS computer
    assert "【技能：huashu-motion】" in text and "接着上次继续做" in text   # the skill and the request come along
    assert "files_write" in text and "STEPS ALREADY DONE" in text
    assert "/data/home" in used(made)[-1].requests[0].system              # THIS COMPUTER: home folder …
    # the plan and its ticks carry over instead of a new plan
    plan = json.loads(store.task(run["id"])["plan"]) if store.task(run["id"]).get("plan") else None
    assert plan and plan["steps"][0] == "读 PPT" and 0 in plan["done"]
    runs = (await client.get(f"/api/tasks/{run['id']}/runs")).json()
    assert runs["root"] == root and [x["id"] for x in runs["runs"]] == [root, run["id"]]
    assert runs["attachments"][0]["name"] == "讲解.pdf"
    th = (await client.get(f"/api/threads/{r['thread_id']}")).json()
    assert th["tasks"][-1]["run_mode"] == "retry" or th["tasks"][-1]["run_mode"] == "continue"


async def test_retry_button_continues_the_same_task(stack):
    client, app, scripts, made, *_ = stack
    scripts.append([("fail", {"reason": "卡住"})])
    r = (await client.post("/api/tasks", json={"prompt": "整理桌面上的文件"})).json()
    await stop_on_question(client, app, r["task"]["id"])
    scripts.append([("done", {"summary": "好了"})])
    rr = (await client.post(f"/api/tasks/{r['task']['id']}/retry")).json()
    t = await wait_task(client, rr["task"]["id"])
    assert t["status"] == "succeeded" and t["continues"] == r["task"]["id"] and t["source"] == "retry"
    assert "CONTINUING AN EARLIER TASK" in task_text(used(made)[-1]) and "整理桌面上的文件" in task_text(used(made)[-1])


async def test_message_in_task_thread_continues_its_task(stack):
    client, app, scripts, made, *_ = stack
    r = (await client.post("/api/tasks", json={"prompt": "写一份周报"})).json()
    await wait_task(client, r["task"]["id"])
    r2 = (await client.post("/api/tasks", json={"prompt": "标题改大一点", "thread_id": r["thread_id"]})).json()
    t2 = await wait_task(client, r2["task"]["id"])
    assert t2["continues"] == r["task"]["id"]
    assert "写一份周报" in task_text(used(made)[-1]) and "标题改大一点" in task_text(used(made)[-1])
    r3 = (await client.post("/api/tasks", json={"prompt": "另一件事", "thread_id": r["thread_id"], "new_task": True})).json()
    assert not (await wait_task(client, r3["task"]["id"])).get("continues")


# ------------------------------------------------------------------------------ the chat routes follow-ups
async def test_chat_followup_with_upload_links_to_the_stopped_task(stack):
    client, app, scripts, made, *_ = stack
    chat = app.state.chat
    scripts.append([("fail", {"reason": "需要 PPT"})])
    out = await chat.send("用技能把我的 PPT 做成讲解动画", [], None, "task")
    card = out["messages"][0]
    root = card["data"]["task_id"]
    await stop_on_question(client, app, root)
    up = (await client.post("/api/uploads", json={"name": "deck.pdf", "content_b64": base64.b64encode(b"pdf").decode(),
                                                   "local_only": True})).json()
    scripts.append([("done", {"summary": "做好了"})])
    res = await chat.send("我已经上传了", [up], None, "auto")
    assert res["route"] == "continue"
    new = res["messages"][0]
    assert new["kind"] == "task" and new["data"]["continues"] == root
    run = await wait_task(client, new["data"]["task_id"])
    assert run["continues"] == root and run["thread_id"] == card["data"]["thread_id"]
    text = task_text(used(made)[-1])
    assert "做成讲解动画" in text and "uploads/deck.pdf" in text and "ATTACHMENTS" in text
    assert chat.get(card["id"])["data"]["continued_by"] == run["id"]


async def test_chat_asks_which_task_when_two_are_unfinished(stack):
    client, app, scripts, made, *_ = stack
    chat = app.state.chat
    ids = []
    for p in ("订一张去上海的机票", "把照片修一下"):
        scripts.append([("fail", {"reason": "x"})])
        out = await chat.send(p, [], None, "task")
        ids.append(out["messages"][0]["data"]["task_id"])
        await stop_on_question(client, app, ids[-1])
    res = await chat.send("继续", [], None, "auto")
    ask = res["messages"][0]
    assert ask["kind"] == "ask" and ask["data"]["pick_task"] and "这是新任务" in ask["data"]["options"]
    scripts.append([("done", {"summary": "ok"})])
    pick = next(o for o in ask["data"]["options"] if "照片" in o)
    a = await chat.answer(ask["id"], pick)
    run = await wait_task(client, a["messages"][0]["data"]["task_id"])
    assert run["continues"] == ids[1]


async def test_chat_reply_answers_the_waiting_question(stack):
    client, app, scripts, made, *_ = stack
    chat = app.state.chat
    scripts.append([("ask_user", {"question": "做成 Word 还是 PPT？", "options": ["Word", "PPT"]}),
                    ("done", {"summary": "按 PPT 做好了"})])
    out = await chat.send("把 PDF 整理一下", [], None, "task")
    tid = out["messages"][0]["data"]["task_id"]
    sessions = app.state.sessions
    for _ in range(100):
        if (sessions.live_for_task(tid) or type("x", (), {"question_info": None})).question_info:
            break
        await asyncio.sleep(0.05)
    res = await chat.send("PPT", [], None, "auto")
    assert res["route"] == "answer"
    t = await wait_task(client, tid)
    assert t["status"] == "succeeded"
    assert "user answered: 'PPT'" in "\n".join(used(made)[-1].requests[-1].history)


# ------------------------------------------------------------------------------ never decide alone
async def test_unanswered_question_keeps_waiting_and_reminds(stack):
    client, app, scripts, made, *_ = stack
    sent = []
    sessions = app.state.sessions
    orig = sessions._notify

    async def spy(kind, title, body, task, deep=None):
        sent.append(title)
        return await orig(kind, title, body, task, deep)
    sessions._notify = spy
    scripts.append([("ask_user", {"question": "要哪种格式？", "options": ["Word", "Excel"]}),
                    ("done", {"summary": "Excel 做好了"})])
    r = (await client.post("/api/tasks", json={"prompt": "整理成表格"})).json()
    await asyncio.sleep(1.5)                      # well past ask_timeout_s (0.4 s)
    t = (await client.get(f"/api/tasks/{r['task']['id']}")).json()
    assert t["status"] == "waiting_input"          # still paused, not "我按最实用的默认方案"
    assert "还在等你回答" in sent
    assert len(used(made)[-1].requests) == 1             # the model was not asked to go on without the answer
    await client.post("/api/agent/answer", json={"session_id": r["session_id"], "text": "Excel"})
    assert (await wait_task(client, r["task"]["id"]))["status"] == "succeeded"


async def test_loop_no_answer_note_forbids_defaults():
    from agent_runtime import AgentLoop
    from aiw_protocols import Task
    from tool_router import ToolExecutor

    class Dummy:
        async def execute(self, *a, **k):
            return ActionResult(success=True, result={})

    async def no_answer(q, opts, extra=None):
        return None
    p = ScriptedProvider([("ask_user", {"question": "哪个？", "options": ["A", "B"]}), ("done", {"summary": "x"})])
    loop = AgentLoop(Task(prompt="整理文件"), "s1", Dummy(), LLMRouter([p]), ToolExecutor(Dummy()), ask=no_answer)
    loop.cfg.make_plan = False
    await loop.run()
    hist = "\n".join(p.requests[-1].history)
    assert "Do NOT decide yourself" in hist and "decide yourself (never" not in hist


# ------------------------------------------------------------------------------ edit + add instructions
async def test_edit_task_reruns_with_new_instructions(stack):
    client, app, scripts, made, *_ = stack
    scripts.append([("files_write", {"path": "a.txt", "content": "1"}), ("done", {"summary": "做了版本一"})])
    r = (await client.post("/api/tasks", json={"prompt": "做一个 3 页的 PPT"})).json()
    await wait_task(client, r["task"]["id"])
    scripts.append([("done", {"summary": "改好了"})])
    e = (await client.post(f"/api/tasks/{r['task']['id']}/edit", json={"prompt": "做一个 5 页的 PPT，配色用蓝色"})).json()
    run = await wait_task(client, e["task"]["id"])
    assert run["continues"] == r["task"]["id"] and run["source"] == "edit"
    text = task_text(used(made)[-1])
    assert "THE USER CHANGED THE INSTRUCTIONS" in text and "5 页" in text and "a.txt" in text
    th = (await client.get(f"/api/threads/{r['thread_id']}")).json()
    assert th["instructions"][r["task"]["id"]].startswith("做一个 5 页")


async def test_instruction_mid_task_reaches_the_running_agent(stack):
    client, app, scripts, made, *_ = stack
    scripts.append([("keypress", {"key": f"F{i % 12 + 1}"}) for i in range(40)] + [("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "慢慢整理文件"})).json()
    for _ in range(100):
        if app.state.sessions.live_for_task(r["task"]["id"]) and used(made) and used(made)[-1].requests:
            break
        await asyncio.sleep(0.02)
    res = (await client.post(f"/api/tasks/{r['task']['id']}/instruct", json={"text": "顺便把 PDF 也转成 Word"})).json()
    assert res.get("instructed"), res
    await wait_task(client, r["task"]["id"], timeout=30)
    notes = " ".join(q.note or "" for q in used(made)[-1].requests)
    assert "顺便把 PDF 也转成 Word" in notes and "THE USER JUST ADDED" in notes


# ------------------------------------------------------------------------------ paths
def test_guessed_home_is_fixed_and_prefixes_generic():
    from agent_runtime.loop import AgentLoop, _ws_rel
    from aiw_protocols import Task
    loop = AgentLoop.__new__(AgentLoop)
    loop.home = "/data/home"
    args, fixed = AgentLoop._fix_paths(loop, "files_write", {"path": "x.py", "content": 'out = "/home/user/workspace/a.docx"'})
    assert fixed and args["content"] == 'out = "/data/home/workspace/a.docx"'
    loop.home = "/home/user"
    assert not AgentLoop._fix_paths(loop, "shell", {"command": "ls /home/user/workspace"})[1]
    assert _ws_rel("/data/home/workspace/a/b.png") == "a/b.png" and _ws_rel("~/workspace/c.png") == "c.png"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in ("skills", "services/agent-runtime/agent_runtime"):
        for dp, _, fs in os.walk(os.path.join(root, d)):
            for f in fs:
                if f.endswith((".md", ".py")):
                    txt = open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read()
                    assert "/home/user/workspace" not in txt.replace("/home/user/workspace →", "").replace(
                        "/home/user/workspace (", "") or f in ("loop.py", "photopea_bridge.py", "planner.py"), f


# ------------------------------------------------------------------------------ lessons
def test_lessons_dedupe_scope_and_user_weight(tmp_path):
    store = Store(str(tmp_path / "l.db"))
    les = L.Lessons(store)
    a = les.add("machine", "cloud-b", "工作区路径不是 /home/user", "用 ~/workspace", "auto", "t1")
    b = les.add("machine", "cloud-b", "工作区路径不是 /home/user/workspace", "用 ~/workspace（$HOME/workspace）", "auto", "t2")
    assert a["id"] == b["id"] and b["hits"] == 2 and "$HOME" in b["fix"]
    les.add("site", "https://trade.m.jd.com/common/limit.html", "limit.html 是风控页", "不要当验证码", "auto")
    les.add("general", "", "做表格", "用户纠正过：表头要加粗", "user")
    assert les.add("tool", "shell", "登录", "密码是 abc123456", "auto") is None      # secrets never stored
    rows = les.relevant(machine="cloud-b", domains=["item.jd.com"], text="打开京东商品页")
    keys = [(r["scope"], r["key"]) for r in rows]
    assert ("machine", "cloud-b") in keys and ("site", "jd.com") in keys
    assert all(r["key"] != "cloud-b" for r in les.relevant(machine="ai-linux-001", text="打开京东"))
    u = les.relevant(text="做一个表格")
    assert u and u[0]["from_user"] and "用户纠正过" in L.Lessons.line(u[0])
    hint, ids = les.hint("FileNotFoundError: /home/user/workspace/a.docx 工作区路径", "shell", machine="cloud-b")
    assert "~/workspace" in hint and ids


def test_quick_lessons_and_corrections(tmp_path):
    store = Store(str(tmp_path / "l.db"))
    les = L.Lessons(store)
    events = [{"type": "agent.action_result", "data": {"tool": "shell", "success": False,
                                                       "result": {"stderr": "FileNotFoundError: '/home/user/workspace/x.docx'"}}}]
    got = les.quick({"id": "t", "machine_id": "cloud-instabox-2"}, events, home="/data/home")
    assert got and got[0]["scope"] == "machine" and "/data/home" in got[0]["problem"]
    c = les.corrections(["不是 PPT，是 Word", "好的"], {"id": "t", "prompt": "整理资料"})
    assert len(c) == 1 and c[0]["source"] == "user" and c[0]["weight"] >= L.USER_WEIGHT


async def test_lessons_go_into_the_task_prompt_and_errors(stack):
    client, app, scripts, made, *_ = stack
    les = app.state.lessons
    les.add("machine", "ai-linux-001", "这台电脑上 officecli 找不到", "用全路径 ~/.local/bin/officecli", "user")
    les.add("tool", "shell", "command not found: om", "先 source ~/.bashrc 再运行 om，或用 npx om", "auto")
    scripts.append([("shell", {"command": "om render 2>&1; echo 'om: command not found' >&2; exit 127"}),
                    ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "用 officecli 改一下文档"})).json()
    await wait_task(client, r["task"]["id"])
    first = used(made)[-1].requests[0]
    assert "LESSONS LEARNED" in first.task and "用户纠正过" in first.task and "officecli" in first.task
    assert any("A LESSON from earlier tasks" in (q.note or "") and "npx om" in (q.note or "") for q in used(made)[-1].requests[1:])
    lst = (await client.get("/api/lessons")).json()
    assert len(lst) >= 2 and all("where" in x for x in lst)
    assert (await client.delete(f"/api/lessons/{lst[0]['id']}")).json()["ok"]
    assert len((await client.get("/api/lessons")).json()) == len(lst) - 1


async def test_lessons_learned_after_success(stack):
    client, app, scripts, made, *_ = stack
    sessions, les = app.state.sessions, app.state.lessons

    class Small:
        name = "small"

        async def complete(self, system, user, max_tokens=800):
            assert "LESSONS" in system
            return json.dumps({"lessons": [{"scope": "site", "key": "jd.com", "problem": "京东登录要扫码",
                                            "fix": "请求接管让用户扫码，不要自己点", "from_user": False}]}, ensure_ascii=False)
    les.llm = lambda role: Small()
    scripts.append([("shell", {"command": "ls /nope-aiw"}), ("shell", {"command": "echo ok"}), ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": "登录 https://www.jd.com 看订单"})).json()
    await wait_task(client, r["task"]["id"])
    for _ in range(100):
        if any(x["key"] == "jd.com" for x in les.items()):
            break
        await asyncio.sleep(0.05)
    assert any(x["key"] == "jd.com" and "扫码" in x["fix"] for x in les.items())


def test_followup_detection():
    for t in ("继续", "接着做", "我已经上传了", "改一下标题", "好的，继续", "用第二个"):
        assert cont.is_followup(t), t
    for t in ("帮我订明天去上海的机票", "今天天气怎么样？", "新任务：查快递"):
        assert not cont.is_followup(t), t
    assert cont.is_followup("这个", has_attachments=True)
