"""iOS v2: computer display names. InstaBox computers default to 云电脑 1, 2, 3… in the order they were added; the user
can rename any computer (kv machine_names, shared by web and iOS) and that name is what every list hands out."""
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"
os.environ.setdefault("AIW_SECRET_KEY", "test-key")

import httpx
import pytest
from aiw_api.config import MachineConfig, Settings
from aiw_api.main import build_app
from aiw_api.store import Store


def _cloud(store, mid, name, created):
    store.upsert_machine({"id": mid, "name": name, "provider": "instabox", "provider_vm_id": "m_" + mid,
                          "agent_endpoint": "wss://pending.invalid", "desktop_endpoint": "instabox:"})
    store._x("UPDATE machines SET created_at=? WHERE id=?", (created, mid))


@pytest.fixture
async def api(tmp_path):
    s = Settings(api_token="api-secret", agent_token="agent-secret", db_path=str(tmp_path / "db.sqlite"),
                 artifacts_dir=str(tmp_path / "artifacts"))
    s.machines = [MachineConfig(id="ai-linux-001", name="aiw", provider="proxmox", provider_vm_id="104",
                                agent_endpoint="ws://127.0.0.1:1", desktop_http="http://127.0.0.1:1")]
    app = build_app(s)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": "Bearer api-secret"})
    client.app = app
    yield client, app.state.store
    await client.aclose()


def test_default_names_follow_the_order_added(tmp_path):
    st = Store(str(tmp_path / "db.sqlite"))
    now = time.time()
    # ids sort differently from the order they were added in: the order added is what counts
    _cloud(st, "cloud-aiw-3", "aiw-3", now + 2)
    _cloud(st, "cloud-aiw", "aiw", now)
    _cloud(st, "cloud-aiw-2", "aiw-2", now + 1)
    names = {m["id"]: m["name"] for m in st.machines()}
    assert names == {"cloud-aiw": "云电脑 1", "cloud-aiw-2": "云电脑 2", "cloud-aiw-3": "云电脑 3"}
    m = st.machine("cloud-aiw-2")
    assert m["raw_name"] == "aiw-2" and m["default_name"] == "云电脑 2" and m["custom_name"] is None and m["order"] == 2


def test_rename_reset_and_upsert_keeps_the_stored_name(tmp_path):
    st = Store(str(tmp_path / "db.sqlite"))
    _cloud(st, "cloud-a", "a", 1)
    assert st.rename_machine("cloud-a", "  渲染  机 ")["name"] == "渲染 机"
    # a row read back and written again (e.g. a new IP) must not freeze the display name into the table
    st.upsert_machine(st.machine("cloud-a"))
    assert st._x("SELECT name FROM machines WHERE id='cloud-a'")[0]["name"] == "a"
    assert st.machine("cloud-a")["name"] == "渲染 机"
    assert st.rename_machine("cloud-a", "")["name"] == "云电脑 1"
    assert st.rename_machine("nope", "x") is None
    st.rename_machine("cloud-a", "x")
    st.delete_machine("cloud-a")
    assert "cloud-a" not in (st.kv_get("machine_names", {}) or {})


async def test_rename_api_is_shared_by_every_list(api):
    client, st = api
    _cloud(st, "cloud-aiw", "aiw", 1)
    _cloud(st, "cloud-aiw-2", "aiw-2", 2)
    st.kv_set("cloud_vms", {"cloud-aiw": {"name": "aiw", "provider": "instabox", "status": "ready", "remote_id": "m_1"},
                            "cloud-aiw-2": {"name": "aiw-2", "provider": "instabox", "status": "ready", "remote_id": "m_2"}})
    ms = {m["id"]: m for m in (await client.get("/api/machines")).json()}
    assert ms["ai-linux-001"]["name"] == "aiw" and ms["cloud-aiw"]["name"] == "云电脑 1" and ms["cloud-aiw-2"]["name"] == "云电脑 2"

    r = await client.put("/api/machines/cloud-aiw-2/name", json={"name": "剪辑机"})
    assert r.status_code == 200 and r.json()["name"] == "剪辑机" and r.json()["default_name"] == "云电脑 2"
    r = await client.put("/api/machines/ai-linux-001/name", json={"name": "家里的 104"})
    assert r.json()["name"] == "家里的 104"

    ms = {m["id"]: m for m in (await client.get("/api/machines")).json()}
    assert ms["cloud-aiw-2"]["name"] == "剪辑机" and ms["ai-linux-001"]["name"] == "家里的 104"
    cl = {c["id"]: c for c in (await client.get("/api/cloud")).json()}
    assert cl["cloud-aiw-2"]["name"] == "剪辑机" and cl["cloud-aiw-2"]["raw_name"] == "aiw-2"
    assert cl["cloud-aiw"]["name"] == "云电脑 1"
    bos = {m["id"]: m for m in (await client.get("/api/browseros")).json()["machines"]}
    assert bos["cloud-aiw-2"]["name"] == "剪辑机"

    # empty name: back to the default
    r = await client.put("/api/machines/cloud-aiw-2/name", json={"name": "  "})
    assert r.json()["name"] == "云电脑 2" and r.json()["custom_name"] is None


async def test_rename_api_validation(api):
    client, _ = api
    assert (await client.put("/api/machines/nope/name", json={"name": "x"})).status_code == 404
    assert (await client.put("/api/machines/ai-linux-001/name", json={"name": "长" * 33})).status_code == 400
    assert (await client.put("/api/machines/ai-linux-001/name", json={"name": 5})).status_code == 400
    assert (await client.put("/api/machines/ai-linux-001/name", json={"name": "a\x07b"})).status_code == 400
    r = await client.put("/api/machines/ai-linux-001/name", json={"name": "x"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_dispatch_roster_uses_display_names(api):
    client, st = api
    await client.put("/api/machines/ai-linux-001/name", json={"name": "画图机"})
    roster = await client.app.state.sessions.roster(probe_timeout=0.5)
    assert {r["id"]: r["name"] for r in roster}["ai-linux-001"] == "画图机"


def test_thread_list_carries_card_fields(tmp_path):
    """History cards: last result as the summary, source, file count + newest file, plan progress."""
    st = Store(str(tmp_path / "db.sqlite"))
    th = st.create_thread("【技能：video-remake】复刻麻将局")
    st.create_task({"id": "t1", "prompt": "p", "machine_id": "m", "status": "succeeded", "thread_id": th["id"], "source": "chat"})
    st._x("UPDATE tasks SET result=? WHERE id='t1'", ("分镜脚本与提示词已生成",))
    sid = st.create_session("m", "t1")
    st._x("UPDATE tasks SET session_id=? WHERE id='t1'", (sid,))
    st.set_task_plan("t1", {"steps": ["a", "b", "c", "d"], "done": [0, 1]})
    st.add_artifact(sid, "file", "/x/a.mp4")
    r = st.threads()[0]
    assert r["last_result"] == "分镜脚本与提示词已生成" and r["last_source"] == "chat"
    assert r["file_count"] == 1 and r["last_file"] == "/x/a.mp4" and r["progress"] == 0.5
    assert "last_plan" not in r
