"""Sign-up flow: the agent asks the user for one page step's fields with a form card, types them in
(passwords only as {{key}}, never in history/events/receipts), and can hand the desktop over for a captcha."""
import asyncio
import json

import pytest

from test_v11_features import stack, wait_task  # noqa: F401  (fixture)


async def _pending(client, sid):
    for _ in range(80):
        s = (await client.get(f"/api/sessions/{sid}")).json()
        if s["pending_question"]:
            return s
        await asyncio.sleep(0.1)
    raise AssertionError("no question")


async def test_form_values_fill_page_and_password_stays_secret(stack):
    client, backend, scripts, app, providers, _ = stack
    scripts.append([
        ("ask_user", {"question": "第 1 步", "title": "Apple Account 注册 · 第 1 步", "description": "创建账号",
                      "fields": [{"key": "email", "label": "邮箱", "type": "email"},
                                 {"key": "password", "label": "密码", "type": "password"},
                                 {"key": "region", "label": "国家或地区", "type": "select", "options": ["中国香港", "美国"]}]}),
        ("type", {"text": "{{password}}"}),
        ("request_takeover", {"reason": "请点击 Cloudflare 验证框"}),
        ("done", {"summary": "注册完成"})])
    r = (await client.post("/api/tasks", json={"prompt": "帮我注册 apple id"})).json()
    sid = r["session_id"]
    s = await _pending(client, sid)
    f = s["pending_question"]["form"]
    assert f["kind"] == "form" and [x["key"] for x in f["fields"]] == ["email", "password", "region"]
    assert f["fields"][1]["secret"] and f["fields"][2]["options"] == ["中国香港", "美国"]
    await client.post("/api/agent/answer", json={"session_id": sid, "text": json.dumps(
        {"email": "a@b.com", "password": "S3cret!pw", "region": "中国香港"})})
    s = await _pending(client, sid)  # now the takeover request
    assert s["pending_question"]["form"]["kind"] == "takeover"
    await client.post("/api/agent/answer", json={"session_id": sid, "text": "done"})
    t = await wait_task(client, r["task"]["id"])
    assert t["status"] == "succeeded", t
    assert "S3cret!pw" in backend.typed                       # the page got the real password
    hist = "\n".join(providers[0].requests[-1].history)
    assert "a@b.com" in hist and "S3cret!pw" not in hist and "{{password}}" in hist
    ev = json.dumps((await client.get(f"/api/sessions/{sid}")).json()["events"], ensure_ascii=False)
    assert "S3cret!pw" not in ev
    rec = json.dumps((await client.get(f"/api/tasks/{r['task']['id']}/receipts")).json(), ensure_ascii=False)
    assert "S3cret!pw" not in rec
    # the sign-up guide reached the agent without being picked
    assert "Sign-up / form-filling flow" in providers[0].requests[0].system


def test_signup_skill_triggers():
    from agent_runtime import skills
    assert skills.auto("帮我注册 Apple ID") == ["signup"]
    assert skills.auto("打开注册表编辑器") == []
