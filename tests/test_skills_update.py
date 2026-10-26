from agent_runtime import skills
from aiw_api import updater


def test_skill_marker_brief():
    p = "【技能：officecli、huashu-motion】\n把周报改成表格"
    assert skills.picked(p) == ["officecli", "huashu-motion"]
    b = skills.brief(p)
    assert "~/.workbuddy/skills/officecli/SKILL.md" in b and "huashu-motion" in b
    assert skills.strip(p) == "把周报改成表格"
    assert skills.brief("no marker") == ""


def test_custom_skill_and_unknown():
    custom = [{"id": "pdf-tools", "title": "pdf-tools", "path": "~/.claude/skills/pdf-tools", "desc": "PDF"}]
    b = skills.brief("【技能：pdf-tools、mystery】 x", custom)
    assert "~/.claude/skills/pdf-tools/SKILL.md" in b and "grep -i mystery" in b
    ids = [s["id"] for s in skills.listing(custom)]
    assert "pdf-tools" in ids and "officecli" in ids


def test_update_script():
    c = dict(updater.DEFAULT, dir="/home/u/docker/aiw", home="/home/u")
    s = updater._script(c)
    assert "docker compose version" in s and "docker-compose" in s
    assert "$DC pull" in s and "$DC up -d --force-recreate" in s
    assert "docker exec aiw-api printenv AIW_VERSION" in s
    assert "/home/u/.aiw-update/update.rc" in s


def test_update_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    with TestClient(app) as cl:
        h = {"Authorization": "Bearer t"}
        r = cl.put("/api/update/config", json={"host": "10.0.0.2", "user": "jown", "dir": "~/docker/aiw", "password": "pw", "sudo": True}, headers=h)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["has_password"] and j["config"]["sudo"] is True and j["config"]["host"] == "10.0.0.2"
        r = cl.get("/api/skills", headers=h)
        assert any(s["id"] == "officecli" for s in r.json())
        r = cl.put("/api/skills/custom", json=[{"id": "pdf tools", "path": "~/x"}], headers=h)
        assert any(s["id"] == "pdf-tools" for s in r.json())


def test_delete_thread_removes_its_tasks(tmp_path):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    app = build_app(s)
    with TestClient(app) as cl:
        h = {"Authorization": "Bearer t"}
        th = cl.post("/api/threads", json={"title": "x"}, headers=h).json()
        st = app.state.store if hasattr(app.state, "store") else None
        if st is None:
            import aiw_api.store as S
            st = S.Store(s.db_path)
        st.insert("tasks", {"id": "task-1", "prompt": "p", "status": "failed", "thread_id": th["id"]})
        st._x("INSERT INTO sessions (id, task_id, status) VALUES ('sess-1', 'task-1', 'done')")
        st._x("INSERT INTO agent_events (id, session_id, type, step, data, ts) VALUES ('ev-1', 'sess-1', 'x', 1, '{}', 0)")
        cl.patch(f"/api/threads/{th['id']}", json={"archived": True}, headers=h)
        assert cl.delete(f"/api/threads/{th['id']}", headers=h).status_code == 200
        assert cl.get("/api/threads?archived=true", headers=h).json() == []
        assert st.task("task-1") is None and st._x("SELECT * FROM agent_events") == []
