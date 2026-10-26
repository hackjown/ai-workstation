"""dispatch: one request split into child tasks that run on several computers at once."""
import asyncio
import os

os.environ["AIW_NO_AUTOAPP"] = "1"
os.environ["AIW_DISPATCH_POLL"] = "0.1"

import httpx
import pytest
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.sessions import _place, roster_text
from llm_router import LLMDecision, LLMRouter
from llm_router.router import LLMProvider
from vm_agent.computer.desktop import MockBackend
from vm_agent.server.router import Router
from vm_agent.server.websocket import serve

PORTS = (18791, 18792)
DEAD_PORT = 18799   # nothing listens here: an offline computer


class RoleProvider(LLMProvider):
    """Answers by role: the parent (no CHILD TASK note) follows `parent`; a child picks its script by a keyword of its
    goal. One provider per task, like the real factory makes one router per task."""
    name = "role"

    def __init__(self, scripts: dict, seen: list):
        self.scripts, self.seen, self.script, self.requests = scripts, seen, None, []

    async def decide(self, req):
        self.requests.append(req)
        if self.script is None:
            key = "parent"
            if "CHILD TASK" in req.task:
                key = next(k for k in self.scripts if k != "parent" and k in req.task.split("CURRENT REQUEST:")[-1])
            self.script = list(self.scripts[key])
            self.seen.append((key, self))
        if not self.script:
            return LLMDecision("end", "fail", {"reason": "script exhausted"})
        tool, args = self.script.pop(0)
        return LLMDecision(f"scripted {tool}", tool, args)

    async def complete(self, system, user, max_tokens=800):
        return '["做事", "交付"]'


async def _stack(tmp_path, monkeypatch, machines=2, second_online=True):
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(tmp_path / "vmfiles"))
    monkeypatch.setenv("AIW_SECRET_KEY", "test-key")
    servers = []
    for port in PORTS[:2 if second_online else 1]:
        ready, stop = asyncio.Event(), asyncio.Event()
        t = asyncio.create_task(serve("127.0.0.1", port, Router(MockBackend()), "agent-secret", ready, stop))
        await ready.wait()
        servers.append((t, stop))
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.ask_timeout_s = 5
    s.machines = [MachineConfig(id="pc-a", name="本地A", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{PORTS[0]}", desktop_http="http://127.0.0.1:1")]
    if machines > 1:
        s.machines.append(MachineConfig(id="pc-b", name="云B", provider="proxmox", provider_vm_id="101",
                                        agent_endpoint=f"ws://127.0.0.1:{PORTS[1] if second_online else DEAD_PORT}",
                                        desktop_http="http://127.0.0.1:1"))
    scripts: dict = {}
    seen: list = []
    app = build_app(s, llm_factory=lambda: LLMRouter([RoleProvider(scripts, seen)]))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})

    async def close():
        await client.aclose()
        for t, stop in servers:
            stop.set()
            await t
    return client, app, scripts, seen, close


async def wait_task(client, tid, timeout=20):
    t = {}
    for _ in range(int(timeout / 0.1)):
        t = (await client.get(f"/api/tasks/{tid}")).json()
        if t["status"] in ("succeeded", "failed", "stopped", "cancelled", "interrupted"):
            return t
        await asyncio.sleep(0.1)
    raise AssertionError(f"task did not finish: {t}")


def child_script(tag):
    return [("files_write", {"path": f"out_{tag}.txt", "content": f"result {tag}"}),
            ("send_file", {"path": f"out_{tag}.txt"}),
            ("done", {"summary": f"{tag} 部分完成"})]


async def test_dispatch_runs_children_on_two_computers(tmp_path, monkeypatch):
    client, app, scripts, seen, close = await _stack(tmp_path, monkeypatch)
    try:
        scripts["parent"] = [("dispatch", {"tasks": [{"machine": "auto", "goal": "调研 alpha 写成文件"},
                                                     {"machine": "auto", "goal": "调研 beta 写成文件"}]}),
                             ("done", {"summary": "两台电脑的结果已合并"})]
        scripts["alpha"] = child_script("alpha")
        scripts["beta"] = child_script("beta")
        r = (await client.post("/api/tasks", json={"prompt": "同时调研 alpha 和 beta", "machine_id": "pc-a"})).json()
        t = await wait_task(client, r["task"]["id"])
        assert t["status"] == "succeeded", t
        parent = next(p for k, p in seen if k == "parent")
        # the model was told which computers exist and was offered the tool
        assert "COMPUTERS" in parent.requests[0].system and "pc-b" in parent.requests[0].system
        assert any(x["name"] == "dispatch" for x in parent.requests[0].tools)
        note = next(h for h in parent.requests[-1].history if "dispatch finished" in h)
        assert "2/2" in note and "alpha 部分完成" in note and "beta 部分完成" in note
        # one piece went to the other computer, one stayed on the parent's own
        th = (await client.get(f"/api/threads/{r['thread_id']}")).json()
        kids = th["tasks"][0]["children"]
        assert sorted(k["machine"] for k in kids) == ["pc-a", "pc-b"]
        assert all(k["status"] == "succeeded" for k in kids)
        assert {k["machine_name"] for k in kids} == {"本地A", "云B"}
        # the children's files were copied onto the parent's computer
        root = tmp_path / "vmfiles" / "dispatch"
        got = sorted(p.name for p in root.rglob("out_*.txt"))
        assert got == ["out_alpha.txt", "out_beta.txt"]
        assert "~/workspace/dispatch/" in note
        # a child thread says where it came from, and is not listed among the user's conversations
        ch = (await client.get(f"/api/threads/{kids[0]['thread_id']}")).json()
        assert ch["parent_thread_id"] == r["thread_id"] and ch["parent_task_id"] == r["task"]["id"]
        assert ch["tasks"][0]["parent_id"] == r["task"]["id"] and ch["tasks"][0]["source"] == "dispatch"
        assert ch["parent_title"]
        ids = {x["id"] for x in (await client.get("/api/threads")).json()}
        assert r["thread_id"] in ids and kids[0]["thread_id"] not in ids
        # the children did not get the dispatch tool themselves (no recursion)
        for k, p in seen:
            if k != "parent":
                assert not any(x["name"] == "dispatch" for x in p.requests[0].tools)
                assert "CHILD TASK" in p.requests[0].task
        # live progress was recorded on the parent's session
        sess = (await client.get(f"/api/sessions/{r['session_id']}")).json()
        phases = [e["data"]["phase"] for e in sess["events"] if e["type"] == "agent.dispatch"]
        assert phases[0] == "start" and phases[-1] == "done"
        # deleting the parent conversation removes the child threads too
        await client.delete(f"/api/threads/{r['thread_id']}")
        assert (await client.get(f"/api/threads/{kids[0]['thread_id']}")).status_code == 404
    finally:
        await close()


async def test_stop_parent_stops_children(tmp_path, monkeypatch):
    client, app, scripts, seen, close = await _stack(tmp_path, monkeypatch)
    try:
        scripts["parent"] = [("dispatch", {"tasks": [{"machine": "pc-a", "goal": "长任务 gamma"},
                                                     {"machine": "pc-b", "goal": "长任务 delta"},
                                                     {"machine": "pc-b", "goal": "长任务 epsilon"}]}),
                             ("done", {"summary": "x"})]
        for k in ("gamma", "delta", "epsilon"):
            scripts[k] = [("shell", {"command": "sleep 20", "timeout": 30}), ("done", {"summary": k})]
        r = (await client.post("/api/tasks", json={"prompt": "分头跑三个长任务", "machine_id": "pc-a"})).json()
        sm = app.state.sessions
        for _ in range(100):
            kids = app.state.store.children(r["task"]["id"])
            if len(kids) == 3 and sum(k["status"] == "running" for k in kids) >= 2:
                break
            await asyncio.sleep(0.1)
        assert sum(k["status"] == "running" for k in kids) >= 2, kids
        # the third piece waits in pc-b's queue behind the second
        assert any(k["status"] == "queued" and k["machine_id"] == "pc-b" for k in kids)
        await client.post("/api/agent/stop", json={"session_id": r["session_id"]})
        t = await wait_task(client, r["task"]["id"])
        assert t["status"] == "stopped"
        for _ in range(60):
            kids = app.state.store.children(r["task"]["id"])
            if all(k["status"] in ("stopped", "cancelled") for k in kids):
                break
            await asyncio.sleep(0.1)
        assert sorted(k["status"] for k in kids) == ["cancelled", "stopped", "stopped"], kids
        assert not any(s.runner and not s.runner.done() for s in sm.live.values())
    finally:
        await close()


async def test_single_usable_computer_runs_pieces_one_after_another(tmp_path, monkeypatch):
    client, app, scripts, seen, close = await _stack(tmp_path, monkeypatch, second_online=False)
    try:
        scripts["parent"] = [("dispatch", {"tasks": [{"machine": "auto", "goal": "整理 zeta"},
                                                     {"machine": "pc-b", "goal": "整理 eta"}]}),
                             ("done", {"summary": "ok"})]
        scripts["zeta"] = child_script("zeta")
        scripts["eta"] = child_script("eta")
        r = (await client.post("/api/tasks", json={"prompt": "同时整理 zeta 和 eta", "machine_id": "pc-a"})).json()
        t = await wait_task(client, r["task"]["id"], timeout=40)
        assert t["status"] == "succeeded", t
        kids = app.state.store.children(r["task"]["id"])
        assert [k["machine_id"] for k in kids] == ["pc-a", "pc-a"]   # pc-b is offline
        assert all(k["status"] == "succeeded" for k in kids)
        parent = next(p for k, p in seen if k == "parent")
        assert "offline" in parent.requests[0].system
    finally:
        await close()


async def test_one_computer_has_no_dispatch_tool(tmp_path, monkeypatch):
    client, app, scripts, seen, close = await _stack(tmp_path, monkeypatch, machines=1)
    try:
        scripts["parent"] = [("dispatch", {"tasks": [{"goal": "x"}]}), ("done", {"summary": "ok"})]
        r = (await client.post("/api/tasks", json={"prompt": "做两件事", "machine_id": "pc-a"})).json()
        t = await wait_task(client, r["task"]["id"])
        assert t["status"] == "succeeded"
        parent = seen[0][1]
        assert not any(x["name"] == "dispatch" for x in parent.requests[0].tools)
        assert "COMPUTERS" not in parent.requests[0].system
        assert any("dispatch is not available" in h for h in parent.requests[-1].history)
    finally:
        await close()


def _r(mid, **kw):
    return {"id": mid, "name": mid, "kind": "local", "available": True, "asleep": False, "state": "online",
            "busy_task": None, **kw}


def test_place_rules():
    roster = [_r("a"), _r("b"), _r("c", asleep=True), _r("d", available=False, state="offline")]
    # auto prefers idle awake computers, then asleep ones, the parent's own last
    got = _place([{"machine": "auto", "goal": "1"}, {"machine": "auto", "goal": "2"}, {"machine": "auto", "goal": "3"}],
                 roster, "a")
    assert [m for _, m, _ in got] == ["b", "c", "a"] and not any(q for _, _, q in got)
    # a named computer is kept; an offline one falls back to auto; more items than computers share out
    got = _place([{"machine": "d", "goal": "1"}, {"machine": "c", "goal": "2"}, {"machine": "auto", "goal": "3"},
                  {"machine": "auto", "goal": "4"}], roster, "a")
    assert [m for _, m, _ in got] == ["b", "c", "a", "a"] and got[3][2] is True
    # a computer busy with another task: its item moves to an idle one, or queues there when none is idle
    busy = [_r("a"), _r("b", busy_task="t-1"), _r("c")]
    got = _place([{"machine": "b", "goal": "1"}], busy, "a")
    assert got[0][1] == "c"
    got = _place([{"machine": "b", "goal": "1"}, {"machine": "c", "goal": "2"}, {"machine": "auto", "goal": "3"},
                  {"machine": "auto", "goal": "4"}], busy, "a")
    assert [m for _, m, _ in got] == ["c", "c", "a", "a"] and got[1][2] is True
    # names work too
    named = [_r("cloud-x", name="云电脑")]
    assert _place([{"machine": "云电脑", "goal": "1"}], named + [_r("a")], "a")[0][1] == "cloud-x"
    # only the parent's computer: everything runs there, one after another
    got = _place([{"machine": "auto", "goal": "1"}, {"machine": "auto", "goal": "2"}], [_r("a")], "a")
    assert [m for _, m, _ in got] == ["a", "a"]
    txt = roster_text(roster, "a")
    assert "dispatch" in txt and "this task's computer" in txt and "asleep" in txt and "offline" in txt
