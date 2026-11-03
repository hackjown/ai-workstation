"""v3.9.17 item 6: 密码保护 (prompt -> vault, *** everywhere, export, stored rows)."""
import json

from agent_runtime import redact as R

PW = "Zq9" + "xT2#kp"   # built at runtime: no literal password in the repo


def test_extract_chinese_prompt_with_host_and_user():
    clean, creds = R.extract(f"用本地vm去ssh服务器，192.168.50.188 用户名root 密码{PW}，这是docker服务器")
    assert PW not in clean and "密码***" in clean
    assert creds == [{"value": PW, "host": "192.168.50.188", "user": "root", "kind": "password"}]


def test_extract_other_forms_and_no_false_positives():
    assert R.extract(f"sshpass -p '{PW}' ssh admin@10.0.0.2")[1][0]["user"] == "admin"
    assert R.extract(f"连 mysql://u1:{PW}@db.lan:3306")[1][0]["value"] == PW
    assert R.extract(f"password: {PW}")[1]
    assert R.extract("给 AIW 做密码保护和打码")[1] == []
    assert R.extract("password=PWD 和 {{task_cred_1}}")[1] == []


def test_mask_known_and_env_secrets():
    s = f"OPENAI_API_KEY=sk-abcdefghijklmnop1234 GH_TOKEN: ghp_xyz123456 MY_SECRET='{PW}' 其它 {PW}"
    m = R.mask(s, [PW])
    assert PW not in m and "sk-abcdefghijklmnop1234" not in m and "ghp_xyz123456" not in m
    assert "OPENAI_API_KEY=" in m and "GH_TOKEN" in m
    assert R.mask("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert R.mask("PATH=/usr/bin HOME=/root") == "PATH=/usr/bin HOME=/root"


def test_mask_obj_keys_and_nested():
    o = {"task": {"prompt": f"密码{PW}"}, "events": [{"type": "task.started", "prompt": f"密码 {PW}"}],
         "api_key": "abcd1234", "args": {"password": "{{task_cred_x}}"}}
    m = json.dumps(R.mask_obj(o), ensure_ascii=False)
    assert PW not in m and "abcd1234" not in m and "{{task_cred_x}}" in m


def test_note_for_never_contains_the_value():
    clean, creds = R.extract(f"192.168.1.5 用户 root 密码 {PW}")
    note = R.note_for(creds, ["task_cred_t1_1"])
    assert PW not in note and "ssh_exec" in note and "{{task_cred_t1_1}}" in note


class _Secrets:
    def __init__(self):
        self.d = {}

    def put(self, k, v):
        self.d[k] = v

    def get(self, k):
        return self.d.get(k)


async def test_session_protects_prompt_and_export_masks_old_rows(tmp_path, monkeypatch):
    """The stored prompt has ***, the vault has the value, an old plaintext row is cleaned by redact_stored."""
    from aiw_api.sessions import SessionManager
    from aiw_api.store import Store
    from aiw_api.config import Settings
    store = Store(str(tmp_path / "a.db"))
    sec = _Secrets()
    sm = SessionManager.__new__(SessionManager)
    sm.store, sm.secrets = store, sec
    out = sm.protect("task-1", f"192.168.50.188 用户名root 密码{PW} 查容器")
    assert PW not in out and sec.get("task_cred_task-1_1") == PW
    assert sm.task_creds("task-1")[0]["host"] == "192.168.50.188"
    # an old row written before v3.9.17
    store.create_task({"id": "task-old", "prompt": f"ssh 10.0.0.9 用户名root 密码{PW}", "machine_id": "m", "status": "succeeded"})
    sid = store.create_session("m", "task-old")
    store.add_event({"id": "e1", "session_id": sid, "type": "task.started", "step": 0, "data": {"prompt": f"密码{PW}"}, "ts": 1.0})
    assert sm.redact_stored(force=True) == 1
    assert PW not in (store.task("task-old")["prompt"])
    assert PW not in json.dumps(store.events(sid), ensure_ascii=False)


from test_v11_features import stack, wait_task  # noqa: E402,F401  (fixture)


async def test_api_task_prompt_events_export_and_ssh_exec(stack):
    client, backend, scripts, app, providers, _ = stack
    scripts.append([("shell", {"command": "ssh root@192.168.50.188 uptime"}),
                    ("done", {"summary": "ok"})])
    r = (await client.post("/api/tasks", json={"prompt": f"ssh 到 192.168.50.188 用户名root 密码{PW} 看看哪些容器占用高"})).json()
    t = await wait_task(client, r["task"]["id"])
    assert PW not in t["prompt"] and "***" in t["prompt"]
    req = providers[0].requests[0]
    assert PW not in req.task and "保险箱" in req.task                     # the model never sees it
    assert "ssh_exec" in [x["name"] for x in req.tools]
    # 9(d): with ssh_exec there, the hand-written ssh in the shell is refused
    assert any("use ssh_exec" in h for h in providers[0].requests[1].history)
    exp = (await client.get(f"/api/tasks/{t['id']}/export")).text
    assert PW not in exp
    ev = json.loads(exp)["events"][0]
    assert ev["type"] == "task.started" and "***" in ev["prompt"]
    assert app.state.secrets.get(f"task_cred_{t['id']}_1") == PW
