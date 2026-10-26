"""小红书运营: daily caps, the approval gate (nothing outbound without the user's approval unless auto-reply is on),
and the 24-hour pause after a captcha / rate-limit / account warning."""
import asyncio
import json
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import xhs
from aiw_api.store import Store
from aiw_protocols import Task


class Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, kind, title, body="", **kw):
        self.sent.append((kind, title, body))
        return {}


class R:
    def __init__(self, stdout):
        self.success, self.result, self.error, self.screenshot = True, {"stdout": stdout}, None, None


class Computer:
    """Answers the probe with a snapshot the test sets."""
    def __init__(self, snap=None):
        self.snap = snap or {"url": "https://www.xiaohongshu.com/explore", "title": "小红书", "alerts": [], "text": "推荐"}
        self.calls = []

    async def execute(self, action, payload, observe=False, timeout=30):
        self.calls.append((action, payload))
        return R(json.dumps(self.snap, ensure_ascii=False))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    s.upsert_machine({"id": "cloud-1", "name": "云", "provider": "instabox", "provider_vm_id": "", "agent_endpoint": ""})
    s.upsert_machine({"id": "ai-linux-001", "name": "104", "provider": "proxmox", "provider_vm_id": "104", "agent_endpoint": ""})
    xhs.save_settings(s, {"enabled": True, "persona": "喜欢露营和咖啡的上班族", "keywords": "露营,手冲咖啡"})
    return s


def task(source="xhs"):
    return Task(prompt="【技能：xhs】小红书运营例行会话", machine_id="ai-linux-001", source=source)


def tools_for(store, comp=None, answer=True, notifier=None):
    t = task()
    seen = []

    async def confirm(tool, payload, decision):
        seen.append((tool, payload, decision))
        return answer
    tl = xhs.task_tools(store, notifier or Notifier(), t, comp or Computer(), confirm)
    return t, {k: v[1] for k, v in tl.items()}, seen


# ----------------------------------------------------------------------------- settings and caps
def test_defaults_prefer_local_vm_and_caps(store):
    s = xhs.settings(store)
    assert s["caps"] == {"like": 30, "comment": 8, "reply": 20, "post": 1}
    assert xhs.machine_of(store) == "ai-linux-001"           # home IP before the datacenter box
    assert s["keywords"] == ["露营", "手冲咖啡"]
    s = xhs.save_settings(store, {"caps": {"like": 9999, "comment": -3}, "window": {"start": "8:05", "end": "bad"}})
    assert s["caps"]["like"] == xhs.HARD_CAPS["like"] and s["caps"]["comment"] == 0
    assert s["window"] == {"start": "08:05", "end": "22:30"}


async def test_like_cap_enforced_server_side(store):
    xhs.save_settings(store, {"caps": {"like": 2}})
    _t, tl, _ = tools_for(store)
    assert (await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/a"})).startswith("OK")
    assert (await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/a"})).startswith("REFUSED")  # same post
    assert (await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/b"})).startswith("OK")
    r = await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/c"})
    assert r.startswith("REFUSED") and "上限" in r
    assert xhs.counts(store)["like"] == 2


def test_counts_reset_per_local_day(store):
    yesterday = time.time() - 86400
    for _ in range(30):
        xhs.record(store, "like", now=yesterday)
    assert xhs.counts(store)["like"] == 0 and xhs.remaining(store, "like") == 30


def test_comment_cap_counts_pending_drafts(store):
    xhs.save_settings(store, {"caps": {"comment": 2}})
    a = xhs.create_draft(store, "comment", "这个营地的星空也太绝了，夜里冷不冷？", "https://www.xiaohongshu.com/explore/1")
    b = xhs.create_draft(store, "comment", "手冲用的是什么豆子呀，看起来油脂好足", "https://www.xiaohongshu.com/explore/2")
    c = xhs.create_draft(store, "comment", "求帐篷型号！这个前庭空间好大", "https://www.xiaohongshu.com/explore/3")
    assert "draft" in a and "draft" in b and "上限" in c["error"]


def test_action_toggle_off(store):
    xhs.save_settings(store, {"actions": {"comment": False}})
    r = xhs.create_draft(store, "comment", "这个配色好舒服，想问是哪个牌子的", "https://www.xiaohongshu.com/explore/1")
    assert "关掉" in r["error"]


def test_drafts_reject_links_ads_and_repeats(store):
    u = "https://www.xiaohongshu.com/explore/1"
    assert "链接" in xhs.create_draft(store, "comment", "看这里 https://x.com 超赞", u)["error"]
    assert "广告" in xhs.create_draft(store, "comment", "姐妹加我微信一起交流呀", u)["error"]
    assert "广告" in xhs.create_draft(store, "comment", "写得真好，互关一下吗", u)["error"]
    assert "draft" in xhs.create_draft(store, "comment", "山里的清晨雾气好美，几点起来拍的？", u)
    assert "一样" in xhs.create_draft(store, "comment", "山里的清晨雾气好美，几点起来拍的", u + "2")["error"]
    assert "短" in xhs.create_draft(store, "comment", "好看", u)["error"]


# ----------------------------------------------------------------------------- the approval gate
async def test_nothing_outbound_without_approval(store):
    t, tl, _ = tools_for(store)
    st = xhs.task_state(t.id)
    # no armed draft: typing, form filling, Enter, paste, 发送 and shell-driven input are all refused
    assert xhs.gate(store, st, "type", {"text": "好美呀，在哪里"})
    assert xhs.gate(store, st, "browser_fill", {"field": "评论", "text": "好美呀"})
    assert xhs.gate(store, st, "keypress", {"key": "Return"})
    assert xhs.gate(store, st, "keypress", {"key": "ctrl+v"})
    assert xhs.gate(store, st, "browser_click", {"text": "发送"})
    assert xhs.gate(store, st, "browser_click", {"text": "发布"})
    assert xhs.gate(store, st, "shell", {"command": "xdotool type 'hello'"})
    # browsing stays free
    assert xhs.gate(store, st, "browser_navigate", {"url": "https://www.xiaohongshu.com/search_result?keyword=露营"}) is None
    assert xhs.gate(store, st, "scroll", {"dx": 0, "dy": 5}) is None
    assert xhs.gate(store, st, "browser_click", {"text": "露营装备清单"}) is None
    # a pending draft cannot be armed
    r = await tl["xhs_draft"]({"kind": "comment", "text": "这片草地太适合躺着发呆了，是哪个营地？",
                               "target_url": "https://www.xiaohongshu.com/explore/9", "target_title": "周末露营"})
    did = r.split()[1]
    assert (await tl["xhs_begin"]({"id": did})).startswith("REFUSED")
    assert xhs.gate(store, st, "type", {"text": "这片草地太适合躺着发呆了，是哪个营地？"})


async def test_review_card_then_send_only_approved_text(store):
    t, tl, seen = tools_for(store, answer=True)
    st = xhs.task_state(t.id)
    r = await tl["xhs_draft"]({"kind": "comment", "text": "这杯拉花好稳！奶泡打了多久呀",
                               "target_url": "https://www.xiaohongshu.com/explore/c1", "target_title": "今日手冲",
                               "target_excerpt": "早起给自己做了一杯"})
    did = r.split()[1]
    out = json.loads(await tl["xhs_review"]({}))
    assert out["approved"][0]["id"] == did
    tool, payload, decision = seen[0]
    assert tool == "xhs_review" and int(decision.level) == 3
    d = payload["drafts"][0]
    assert d["target_title"] == "今日手冲" and d["target_excerpt"] == "早起给自己做了一杯" and d["text"].startswith("这杯拉花")
    armed = json.loads(await tl["xhs_begin"]({"id": did}))
    assert armed["text"] == "这杯拉花好稳！奶泡打了多久呀"
    assert xhs.gate(store, st, "browser_click", {"text": "发送"})          # type first
    assert xhs.gate(store, st, "type", {"text": "随便写点别的"})            # not the approved text
    assert xhs.gate(store, st, "type", {"text": armed["text"]}) is None
    assert xhs.gate(store, st, "browser_click", {"text": "发送"}) is None
    assert (await tl["xhs_sent"]({"id": did})).startswith("OK")
    assert xhs.counts(store)["comment"] == 1
    assert xhs.gate(store, st, "browser_click", {"text": "发送"})          # disarmed again


async def test_queue_edit_and_skip_wake_the_review(store):
    t, tl, seen = tools_for(store)

    async def confirm(tool, payload, decision):    # the user never answers the card; decides in the queue
        await asyncio.sleep(30)
        return False
    tl2 = {k: v[1] for k, v in xhs.task_tools(store, Notifier(), t, Computer(), confirm).items()}
    a = (await tl2["xhs_draft"]({"kind": "comment", "text": "帐篷边那盏灯好有氛围，想知道型号",
                                 "target_url": "https://www.xiaohongshu.com/explore/l1"})).split()[1]
    b = (await tl2["xhs_draft"]({"kind": "comment", "text": "秋天的山真的每一帧都好出片",
                                 "target_url": "https://www.xiaohongshu.com/explore/l2"})).split()[1]
    job = asyncio.create_task(tl2["xhs_review"]({}))
    await asyncio.sleep(0.05)
    assert xhs.decide(store, a, "approve", text="帐篷边那盏灯好有氛围，是什么型号呀")["draft"]["status"] == "approved"
    assert not job.done()
    xhs.decide(store, b, "skip")
    out = json.loads(await asyncio.wait_for(job, 2))
    assert [x["id"] for x in out["approved"]] == [a] and out["approved"][0]["edited"] and out["skipped"] == [b]
    armed = json.loads(await tl2["xhs_begin"]({"id": a}))
    assert armed["text"] == "帐篷边那盏灯好有氛围，是什么型号呀"
    assert (await tl2["xhs_begin"]({"id": b})).startswith("REFUSED")


async def test_review_timeout_keeps_drafts_pending(store):
    t, tl, _ = tools_for(store, answer=False)       # a timeout comes back as False with no explicit reject
    did = (await tl["xhs_draft"]({"kind": "comment", "text": "这条徒步路线难度大吗，新手能走吗",
                                  "target_url": "https://www.xiaohongshu.com/explore/h1"})).split()[1]
    out = json.loads(await tl["xhs_review"]({}))
    assert out["still_pending"] == [did] and store.get("xhs_drafts", did)["status"] == "pending"


async def test_replies_need_approval_unless_auto_reply(store):
    t, tl, _ = tools_for(store)
    st = xhs.task_state(t.id)
    r = await tl["xhs_draft"]({"kind": "reply", "text": "谢谢喜欢！那天风有点大但很值得",
                               "target_url": "https://www.xiaohongshu.com/explore/mine1"})
    assert "pending" in r
    assert (await tl["xhs_begin"]({"id": r.split()[1]})).startswith("REFUSED")
    xhs.save_settings(store, {"auto_reply": True})
    r = await tl["xhs_draft"]({"kind": "reply", "text": "是在莫干山那边，开车两小时左右",
                               "target_url": "https://www.xiaohongshu.com/explore/mine1"})
    assert "auto-approved" in r
    armed = json.loads(await tl["xhs_begin"]({"id": r.split()[1]}))
    assert xhs.gate(store, st, "type", {"text": armed["text"]}) is None
    # auto-reply never covers comments on other people's posts
    r = await tl["xhs_draft"]({"kind": "comment", "text": "你家猫咪的表情好像在思考人生",
                               "target_url": "https://www.xiaohongshu.com/explore/x"})
    assert "pending" in r


async def test_like_click_needs_a_token(store):
    t, tl, _ = tools_for(store)
    st = xhs.task_state(t.id)
    assert xhs.gate(store, st, "browser_click", {"text": "赞"})
    assert (await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/z"})).startswith("OK")
    assert xhs.gate(store, st, "browser_click", {"text": "赞"}) is None
    assert xhs.gate(store, st, "browser_click", {"text": "赞"})              # one like per approval


def test_user_post_is_approved_and_typed_with_media(store):
    r = xhs.create_draft(store, "post", "周末带着新帐篷去了湖边，记录一下装备和路线。", title="湖边露营初体验",
                         media=["~/workspace/uploads/a.jpg"], source="user", approved=True)
    assert r["draft"]["status"] == "approved"
    st = {"armed": r["draft"], "like_tokens": [], "typed": False}
    assert xhs.gate(store, st, "type", {"text": "湖边露营初体验"}) is None
    assert xhs.gate(store, st, "type", {"text": "~/workspace/uploads/a.jpg"}) is None
    assert xhs.gate(store, st, "type", {"text": "另外加一段没批准的话"})
    assert xhs.gate(store, st, "browser_click", {"text": "发布"}) is None


# ----------------------------------------------------------------------------- warnings and the pause
def test_classify_page_states():
    ok = {"url": "https://www.xiaohongshu.com/explore", "title": "小红书", "alerts": [], "text": "今天聊聊违规停车被罚的经历"}
    assert xhs.classify(ok) == "ok"                       # a post about 违规 is not an account warning
    assert xhs.classify({**ok, "url": "https://www.xiaohongshu.com/website-login/captcha?redirect=x"}) == "captcha"
    assert xhs.classify({**ok, "alerts": ["请拖动滑块完成验证"]}) == "captcha"
    assert xhs.classify({**ok, "alerts": ["操作太频繁，请稍后再试"]}) == "rate_limit"
    assert xhs.classify({**ok, "alerts": ["你的账号存在违规行为，评论功能已被限制"]}) == "account_warning"
    assert xhs.classify({**ok, "alerts": ["扫码登录 使用小红书APP扫码"]}) == "login"
    assert xhs.classify({"error": "devtools not reachable"}) == "no_browser"


async def test_warning_pauses_24h_and_blocks_everything(store):
    n = Notifier()
    comp = Computer()
    t, tl, _ = tools_for(store, comp, notifier=n)
    st = xhs.task_state(t.id)
    comp.snap = {**comp.snap, "alerts": ["请完成安全验证"]}
    r = await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/q"})
    assert r.startswith("STOP")
    p = xhs.pause_state(store)
    assert p and p["kind"] == "captcha" and 23.9 * 3600 < p["until"] - time.time() <= 24 * 3600
    assert n.sent and n.sent[0][0] == "xhs_warning"
    assert xhs.counts(store)["like"] == 0
    # everything that acts is refused; reading and finishing still work
    assert xhs.gate(store, st, "browser_click", {"text": "露营"})
    assert xhs.gate(store, st, "browser_navigate", {"url": "https://www.xiaohongshu.com"})
    assert xhs.gate(store, st, "click", {"x": 1, "y": 2})
    assert xhs.gate(store, st, "done", {"text": "已暂停"}) is None
    assert xhs.gate(store, st, "screenshot", {}) is None
    comp.snap = {**comp.snap, "alerts": []}
    assert (await tl["xhs_like"]({"post_url": "https://www.xiaohongshu.com/explore/q2"})).startswith("REFUSED")
    assert "已暂停" in xhs.create_draft(store, "comment", "这家店的肉桂卷看起来好扎实", "https://www.xiaohongshu.com/explore/r")["error"]


async def test_agent_reported_warning_and_no_sessions_while_paused(store):
    t, tl, _ = tools_for(store)
    assert (await tl["xhs_warning"]({"kind": "rate_limit", "detail": "评论过于频繁"})).startswith("STOPPED")

    class Sessions:
        live = {}
        submitted = []

        def __init__(self, st):
            self.store = st

        def busy_session(self, mid):
            return None

        async def submit(self, *a, **k):
            self.submitted.append((a, k))
            return {"task": {"id": "t"}}

    class Sched:
        pass
    sc = Sched()
    sc.store, sc.sessions = store, Sessions(store)
    store.kv_set("xhs_plan", {"day": xhs.day_key(), "slots": [time.time() - 10]})
    assert await xhs.tick(sc) is None and not sc.sessions.submitted
    # 24 h later the pause has expired and a due slot inside the window starts a session
    later = time.time() + 24 * 3600 + 60
    assert xhs.pause_state(store, later) is None


async def test_tick_starts_one_session_at_a_random_slot(store):
    xhs.save_settings(store, {"window": {"start": "00:00", "end": "23:59"}})

    class Sessions:
        live = {}

        def __init__(self, st):
            self.store, self.submitted = st, []

        def busy_session(self, mid):
            return None

        async def submit(self, prompt, mid, **k):
            self.submitted.append((prompt, mid, k))
            return {"task": {"id": "t1"}}

    class Sched:
        pass
    sc = Sched()
    sc.store, sc.sessions = store, Sessions(store)
    now = time.time()
    if not xhs.in_window(xhs.settings(store), now):
        pytest.skip("edge of the day")
    store.kv_set("xhs_plan", {"day": xhs.day_key(now), "slots": [now - 5, now + 4000]})
    await xhs.tick(sc, now)
    assert len(sc.sessions.submitted) == 1
    prompt, mid, k = sc.sessions.submitted[0]
    assert mid == "ai-linux-001" and k["source"] == "xhs" and "【技能：xhs】" in prompt
    await xhs.tick(sc, now + 1)
    assert len(sc.sessions.submitted) == 1          # nothing else due yet


def test_plan_day_is_random_and_spaced(store):
    import random
    s = xhs.settings(store)
    s["window"] = {"start": "00:00", "end": "23:59"}
    base = xhs.local().replace(hour=0, minute=5).timestamp()
    a = xhs.plan_day(s, base, random.Random(1))
    b = xhs.plan_day(s, base, random.Random(2))
    assert a and b and a != b
    for p in (a, b):
        assert all(y - x >= 35 * 60 for x, y in zip(p, p[1:]))


async def test_api_settings_queue_and_pause(tmp_path):
    import httpx
    from aiw_api.config import MachineConfig, Settings
    from aiw_api.main import build_app
    s = Settings(api_token="k", agent_token="a", db_path=str(tmp_path / "db.sqlite"), artifacts_dir=str(tmp_path / "art"))
    s.machines = [MachineConfig(id="ai-linux-001", provider="proxmox", provider_vm_id="104",
                                agent_endpoint="ws://127.0.0.1:1", desktop_http="http://127.0.0.1:1")]
    app = build_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": "Bearer k"}) as c:
        o = (await c.get("/api/xhs")).json()
        assert o["caps"] == {"like": 30, "comment": 8, "reply": 20, "post": 1} and o["machine_id"] == "ai-linux-001"
        r = await c.put("/api/xhs/settings", json={"persona": "露营", "keywords": "露营、咖啡", "auto_reply": True})
        assert r.json()["keywords"] == ["露营", "咖啡"] and r.json()["auto_reply"] is True
        st = app.state.store
        d1 = xhs.create_draft(st, "comment", "这个湖的颜色好像果冻，几月去的呀", "https://www.xiaohongshu.com/explore/1")["draft"]
        d2 = xhs.create_draft(st, "comment", "跟着你的清单买了炉头，确实好用", "https://www.xiaohongshu.com/explore/2")["draft"]
        assert len((await c.get("/api/xhs")).json()["pending"]) == 2
        r = await c.post(f"/api/xhs/drafts/{d1['id']}", json={"action": "approve", "text": "这个湖的颜色像果冻，几月去的呀"})
        assert r.json()["status"] == "approved" and r.json()["text"].startswith("这个湖的颜色像")
        r = await c.post(f"/api/xhs/drafts/{d1['id']}", json={"action": "edit", "text": "加个微信聊"})
        assert r.status_code == 400
        await c.post("/api/xhs/drafts/batch", json={"ids": [d2["id"]], "action": "skip"})
        assert st.get("xhs_drafts", d2["id"])["status"] == "skipped"
        r = await c.post("/api/xhs/post", json={"title": "湖边露营", "text": "第一次在湖边过夜，记录一下。", "media": ["~/workspace/uploads/a.jpg"]})
        assert r.json()["status"] == "approved" and r.json()["media"] == ["~/workspace/uploads/a.jpg"]
        assert (await c.post("/api/xhs/pause", json={"hours": 24})).json()["until"] > time.time()
        assert (await c.post("/api/xhs/run")).status_code == 409
        await c.delete("/api/xhs/pause")
        assert (await c.get("/api/xhs")).json()["pause"] is None
