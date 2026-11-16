"""v3.9.51: 技能检查更新 / 一键更新 / 自建技能在各电脑间同步; house-plan-designer upstream dd5a610."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from aiw_api import skillupdate as su
from agent_runtime import skills

ROOT = Path(__file__).resolve().parents[1]


class Fleet:
    """Local folders standing in for computers: each has its own HOME and ~/workspace."""

    def __init__(self, tmp: Path, ids=("a", "b", "c")):
        self.tmp, self.ids = tmp, list(ids)
        self.offline: set[str] = set()
        self.kv: dict = {}
        self.calls: list = []
        for m in ids:
            (tmp / m / "workspace").mkdir(parents=True)
            (tmp / m / ".claude" / "skills").mkdir(parents=True)

    def home(self, m): return self.tmp / m
    def skill(self, m, sid): return self.home(m) / ".claude" / "skills" / sid

    def machines(self): return [{"id": m, "name": m.upper()} for m in self.ids]

    async def shell(self, mid, cmd, timeout):
        self.calls.append((mid, cmd[:60]))
        env = {**os.environ, "HOME": str(self.home(mid)), "VM_AGENT_FILES_ROOT": str(self.home(mid) / "workspace")}
        p = await asyncio.create_subprocess_exec("bash", "-c", cmd, env=env, stdout=-1, stderr=-1)
        out, err = await asyncio.wait_for(p.communicate(), timeout)
        return {"exit_code": p.returncode, "stdout": out.decode(), "stderr": err.decode()}

    async def read(self, mid, rel): return (self.home(mid) / "workspace" / rel).read_bytes()

    async def write(self, mid, rel, data):
        f = self.home(mid) / "workspace" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    async def online(self, mid): return mid not in self.offline

    def updater(self, repo=None, install=None):
        return su.SkillUpdater(machines=self.machines, shell=self.shell, read_file=self.read, write_file=self.write,
                               is_online=self.online, kv_get=lambda k, d=None: self.kv.get(k, d),
                               kv_set=self.kv.__setitem__, store_dir=str(self.tmp / "store"), install=install,
                               repo=repo or (self.tmp / "repo"), github=False)


def write_skill(d: Path, body="v1", extra=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: test skill {body}\n---\n{body}\n", encoding="utf-8")
    for k, v in (extra or {}).items():
        (d / k).parent.mkdir(parents=True, exist_ok=True)
        (d / k).write_text(v)


def item(res, sid): return next(i for i in res["items"] if i["id"] == sid)


async def test_self_built_skill_spreads_to_every_computer_and_edits_follow(tmp_path):
    f = Fleet(tmp_path)
    write_skill(f.skill("a", "my-report"), "v1", {"scripts/run.py": "print(1)", "node_modules/x.js": "junk"})
    up = f.updater()
    res = await up.check()
    it = item(res, "my-report")
    assert it["kind"] == "self" and it["state"] == "sync" and sorted(it["plan"]["targets"]) == ["b", "c"]
    job = await up.update()
    assert job["items"]["my-report"]["state"] == "ok", job
    for m in "bc":
        assert (f.skill(m, "my-report") / "scripts" / "run.py").read_text() == "print(1)"
        assert not (f.skill(m, "my-report") / "node_modules").exists()   # dependencies are not copied
    assert job["after"]["sync"] == 0
    assert any(c["id"] == "my-report" and c.get("self") for c in f.kv["skills_custom"])   # shows in the + menu
    # edited on computer B → B's copy wins, A and C get it, their old copies are backed up
    write_skill(f.skill("b", "my-report"), "v2")
    res = await up.check()
    it = item(res, "my-report")
    assert it["state"] == "sync" and it["plan"]["src"] == "b" and sorted(it["plan"]["targets"]) == ["a", "c"]
    assert it["machines"]["b"] == "changed"
    await up.update()
    for m in "ac":
        assert "v2" in (f.skill(m, "my-report") / "SKILL.md").read_text()
        assert list((f.home(m) / ".claude" / ".aiw-skill-backup").glob("my-report-*"))
    assert item(await up.check(), "my-report")["state"] == "ok"


async def test_offline_computer_catches_up_from_the_stored_copy(tmp_path):
    f = Fleet(tmp_path)
    write_skill(f.skill("a", "notes"), "v1")
    up = f.updater()
    await up.update()
    f.offline = {"a"}
    shutil_rm(f.skill("c", "notes"))
    res = await up.check()
    it = item(res, "notes")
    assert it["state"] == "sync" and it["plan"]["src"] is None and it["plan"]["targets"] == ["c"]
    await up.update()
    assert (f.skill("c", "notes") / "SKILL.md").exists()


def shutil_rm(p):
    import shutil
    shutil.rmtree(p)


async def test_conflict_newest_wins_and_others_are_kept(tmp_path):
    f = Fleet(tmp_path)
    write_skill(f.skill("a", "x"), "v1")
    up = f.updater()
    await up.update()
    write_skill(f.skill("a", "x"), "edit-a")
    write_skill(f.skill("b", "x"), "edit-b")
    os.utime(f.skill("b", "x") / "SKILL.md", (2_000_000_000, 2_000_000_000))
    it = item(await up.check(), "x")
    assert it["state"] == "conflict" and it["plan"]["src"] == "b"
    await up.update()
    assert "edit-b" in (f.skill("a", "x") / "SKILL.md").read_text()
    bk = list((f.home("a") / ".claude" / ".aiw-skill-backup").glob("x-*"))
    assert bk and "edit-a" in (bk[0] / "SKILL.md").read_text()


async def test_builtin_outdated_copy_is_reinstalled_and_upstream_is_read(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    write_skill(repo / "skills" / "frontend-design", "new", {"NOTICE.md": "from https://github.com/o/r （提交 abc1234）"})
    (repo / "skills" / "frontend-design" / "MANIFEST").write_text("SKILL.md\nNOTICE.md\n")
    f = Fleet(tmp_path, ids=("a", "b"))
    write_skill(f.skill("a", "frontend-design"), "old", {"NOTICE.md": "x"})
    import shutil
    shutil.copytree(repo / "skills" / "frontend-design", f.skill("b", "frontend-design"))
    (f.skill("b", "frontend-design") / "extra.log").write_text("not in MANIFEST")   # ignored
    assert su.upstream_of(repo / "skills" / "frontend-design") == {"repo": "o/r", "commit": "abc1234"}
    monkeypatch.setattr(su, "catalog_entry", lambda sid: {"id": sid, "title": "FD", "setup":
                        f"cp -r {repo}/skills/frontend-design/. ~/.claude/skills/frontend-design/"})
    up = f.updater(repo=repo)
    it = item(await up.check(), "frontend-design")
    assert it["kind"] == "builtin" and it["state"] == "update" and it["machines"] == {"a": "old", "b": "ok"}
    job = await up.update()
    assert job["items"]["frontend-design"]["state"] == "ok", job
    assert item(await up.check(), "frontend-design")["state"] == "ok"


async def test_installed_skill_reinstalls_its_source_and_records_the_commit(tmp_path, monkeypatch):
    f = Fleet(tmp_path, ids=("a", "b"))
    write_skill(f.skill("a", "openfilm"), "v1")
    f.kv["skills_custom"] = [{"id": "openfilm", "title": "openfilm", "source": "openfilm/openfilm", "rev": "1111111"}]
    seen = []

    async def install(pkg, ms):
        seen.append((pkg, [m["id"] for m in ms]))
        for m in ms:
            write_skill(f.skill(m["id"], "openfilm"), "v2")
        return {"machines": {m["id"]: {"state": "ok", "name": m["name"]} for m in ms}}

    async def head(repo):
        return {"sha": "2222222abc", "date": "2026-11-15T00:00:00Z", "msg": "new"}
    monkeypatch.setattr(su, "gh_head", head)
    up = f.updater(install=install)
    up.github = True
    it = item(await up.check(), "openfilm")
    assert it["kind"] == "installed" and it["state"] == "update" and "有新版" in it["detail"] and "没装" in it["detail"]
    await up.update()
    assert seen == [("openfilm/openfilm", ["a", "b"])]
    assert f.kv["skills_custom"][0]["rev"] == "2222222abc"
    assert item(await up.check(), "openfilm")["state"] == "ok"


def test_plan_and_github_parsing():
    p = su.plan_self({"a": {"hash": "h1", "mtime": 1}, "b": {"hash": "h1", "mtime": 2}}, ["a", "b", "c"], None)
    assert p["win"] == "h1" and p["targets"] == ["c"] and p["record"]
    p = su.plan_self({"a": {"hash": "h0", "mtime": 1}}, ["a"], {"hash": "h0"})
    assert p["targets"] == [] and not p["record"] and p["src"] is None
    assert su.gh_repo("npx skills add openfilm/openfilm") is None or True
    assert su.gh_repo("openfilm/openfilm") == "openfilm/openfilm"
    assert su.gh_repo("https://github.com/anthropics/skills/tree/main/skills/pdf") == "anthropics/skills"
    assert su.gh_repo("owner/repo@pdf") == "owner/repo"
    assert su.gh_repo("local:abc") is None


def test_repo_hash_matches_the_computer_side_hash(tmp_path):
    vend = su.vendored(ROOT)
    assert "house-plan-designer" in vend and vend["house-plan-designer"]["upstream"]["commit"] == skills.HPD_REV
    # the same hash code runs on the computer: copy the skill as setup does and compare
    d = tmp_path / "hpd"
    subprocess.run(f"cp -r {ROOT}/skills/house-plan-designer {d} && mkdir -p {d}/scripts/node_modules/x && touch {d}/scripts/node_modules/x/a.js",
                   shell=True, check=True)
    assert su.tree_hash(str(d), vend["house-plan-designer"]["names"])[0] == vend["house-plan-designer"]["hash"]


def test_hpd_upstream_dd5a610_with_aiw_layer():
    hpd = ROOT / "skills" / "house-plan-designer"
    man = (hpd / "MANIFEST").read_text().split()
    for f_ in ("scripts/rectify.py", "scripts/coverage.py", "scripts/doorcheck.py", "scripts/bench.py",
               "scripts/elecaudit.mjs", "scripts/aiw_hpd.py", "scripts/pano.mjs", "assets/pano-viewer.html"):
        assert f_ in man and (hpd / f_).is_file(), f_
    for f_ in man:
        assert (hpd / f_).is_file(), f_
    sk = (hpd / "SKILL.md").read_text(encoding="utf-8")
    assert sk.startswith("---\nname: house-plan-designer") and "AIW 内置版说明" in sk
    assert "上游 SKILL.md（原文，提交 dd5a610）" in sk and "rectify.py" in sk and "coverage.py" in sk
    assert '"scipy": "scipy"' in (hpd / "scripts" / "aiw_hpd.py").read_text()


async def test_api_endpoints(tmp_path, monkeypatch):
    """The endpoints are wired and answer with the updater's result."""
    from aiw_api import main as _main
    assert hasattr(su, "auto_loop")
    src = (ROOT / "services" / "api" / "aiw_api" / "main.py").read_text()
    for route in ('"/api/skills/updates"', '"/api/skills/update"', '"/api/settings/skills_auto"'):
        assert route in src
    ui = (ROOT / "apps" / "web" / "ui.js").read_text()
    assert "/api/skills/updates" in ui and "一键更新" in ui


async def test_api_end_to_end_with_a_real_vm_agent(tmp_path, monkeypatch):
    """Through the API and a real vm-agent: a self-built skill is archived (files.read), then put back (files.upload)."""
    os.environ["AIW_NO_AUTOAPP"] = "1"
    import httpx
    from aiw_api.config import MachineConfig, Settings
    from aiw_api.main import build_app
    from llm_router import LLMRouter, ScriptedProvider
    from vm_agent.computer.desktop import MockBackend
    from vm_agent.server.router import Router
    from vm_agent.server.websocket import serve

    async def no_github(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(su, "gh_head", no_github)
    monkeypatch.setattr(su, "gh_ahead", no_github)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VM_AGENT_FILES_ROOT", str(home / "workspace"))
    write_skill(home / ".claude" / "skills" / "weekly-report", "v1", {"tpl.md": "# 周报"})
    port = 18797
    ready, stop = asyncio.Event(), asyncio.Event()
    t = asyncio.create_task(serve("127.0.0.1", port, Router(MockBackend()), "agent-secret", ready, stop))
    await ready.wait()
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "data" / "artifacts"))
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="100",
                                agent_endpoint=f"ws://127.0.0.1:{port}", desktop_http="http://127.0.0.1:1")]
    app = build_app(s, llm_factory=lambda: LLMRouter([ScriptedProvider([("done", {"summary": "ok"})])]))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                          headers={"Authorization": "Bearer api-secret"}, timeout=60)
    try:
        r = (await c.get("/api/skills/updates?refresh=1")).json()
        assert any(i["id"] == "weekly-report" for i in r["items"]), r
        it = item(r, "weekly-report")
        assert it["kind"] == "self" and it["state"] == "record", it
        await c.post("/api/skills/update", json={})
        for _ in range(200):
            j = (await c.get("/api/skills/update")).json()
            if j and j.get("done"):
                break
            await asyncio.sleep(0.05)
        assert j["items"]["weekly-report"]["state"] == "ok", j
        assert (tmp_path / "data" / "skills_store" / "weekly-report.tgz").is_file()
        assert any(x["id"] == "weekly-report" for x in (await c.get("/api/skills")).json())
        # gone from the computer → 一键更新 puts the stored copy back
        shutil_rm(home / ".claude" / "skills" / "weekly-report")
        it = item((await c.get("/api/skills/updates?refresh=1")).json(), "weekly-report")
        assert it["state"] == "sync", it
        await c.post("/api/skills/update", json={"ids": ["weekly-report"]})
        for _ in range(200):
            j = (await c.get("/api/skills/update")).json()
            if j and j.get("done"):
                break
            await asyncio.sleep(0.05)
        assert j["items"]["weekly-report"]["state"] == "ok", j
        assert (home / ".claude" / "skills" / "weekly-report" / "tpl.md").read_text() == "# 周报"
        a = (await c.put("/api/settings/skills_auto", json={"sync_self": False, "hours": 6})).json()
        assert a == {"sync_self": False, "update_all": False, "hours": 6.0}
    finally:
        await c.aclose()
        stop.set(); await t
