"""v3.9.92 每日回顾 + 主动纠错 (worklog.py, review.py, device_review.py): extraction and its fallback, claim rechecks,
contradictions, dedupe / expiry, no news no push, prefs mutes, secrets, 猜你想要 (repeated ask -> routine, two ignores
mute, 7-day dedupe, no grounding no section), device capabilities (not granted -> skipped, moved event -> one new
reminder), the health summary's 7-day baseline and its off switch, and the chat / HTTP wiring."""
import asyncio
import json
import os
import time

os.environ["AIW_NO_AUTOAPP"] = "1"

from aiw_api import device_review as DR
from aiw_api import review as R
from aiw_api import worklog as W
from aiw_api.memory import Memory
from aiw_api.store import Store


# ------------------------------------------------------------------------------------------ fakes
class Small:
    def __init__(self, answers=()):
        self.answers, self.calls = list(answers), []

    async def complete(self, system, user, max_tokens=800):
        self.calls.append((system, user))
        return self.answers.pop(0) if self.answers else "{}"


class Notes:
    def __init__(self):
        self.sent = []

    async def notify(self, kind, title, body="", **kw):
        self.sent.append({"kind": kind, "title": title, "body": body, **kw})
        return {}


class FakeChat:
    """The bits of chat.Chat the review uses."""
    def __init__(self):
        self.msgs, self.started = {}, []

    def add(self, role, kind, text="", data=None, task_id=None):
        mid = f"msg-{len(self.msgs) + 1}"
        self.msgs[mid] = {"id": mid, "role": role, "kind": kind, "text": text, "data": dict(data or {})}
        return self.msgs[mid]

    def update(self, mid, text=None, data=None, **kw):
        if data:
            self.msgs[mid]["data"].update(data)
        return self.msgs[mid]

    def recent_tasks(self, hours=24, limit=6):
        return []

    async def start_task(self, prompt, title="", machine_id=None, attachments=None, origin=None, source="chat", fast=None):
        self.started.append(prompt)
        return self.add("assistant", "task", title, {"prompt": prompt})


class FakeHub:
    def __init__(self, caps=(), online=True, answer=None):
        self.caps, self.is_online, self.answer, self.calls = list(caps), online, answer or (lambda t, a: {}), []

    def known(self):
        return [{"id": "iphone", "caps": self.caps}] if self.caps is not None else []

    def _pick(self, cap, device_id=""):
        return "iphone" if self.is_online and cap in self.caps else None

    async def call(self, tool, args, ttl=None, device_id="", fast=False):
        self.calls.append((tool, args))
        r = self.answer(tool, args)
        return {"ok": True, "result": r} if r is not None else {"ok": False, "error": "不支持的健康数据类型"}


def setup(tmp_path, answers=(), hub=None):
    store = Store(str(tmp_path / "r.db"))
    small = Small(answers or ())
    llm = (lambda role: small) if answers is not None else None
    wl = W.Worklog(store, llm=llm)
    chat, notes = FakeChat(), Notes()
    rv = R.Review(store, wl, chat, notes, None, llm=llm)
    if hub is not None:
        rv.device = DR.DeviceReview(store, hub, notes, chat)
    return store, wl, rv, chat, notes, small


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


NOW = time.time()


# ------------------------------------------------------------------------------------------ worklog
async def test_task_extraction_falls_back_without_a_model(tmp_path):
    store, wl, *_ = setup(tmp_path, answers=None)
    it = await wl.record_task({"id": "t1", "prompt": "【技能:dev】把 requests 升级一下", "thread_id": "th1"}, "failed",
                              "装到一半失败了，见 https://pypi.org/project/requests/2.32.3/", ["/w/out.log"])
    assert it["title"].startswith("把 requests 升级") and it["status"] == "open" and it["task_status"] == "failed"
    assert it["open_items"] and it["open_items"][0].startswith("没做完")
    assert it["claims"][0]["how_to_check"] == {"kind": "pypi", "target": "requests", "expect": "2.32.3"}
    assert it["deliverables"] == ["out.log"]
    # with a model: its JSON wins
    (tmp_path / "b").mkdir()
    store, wl, _, _, _, small = setup(tmp_path / "b", answers=[json.dumps({
        "title": "查 fastapi 版本", "outcome": "最新是 0.115", "deliverables": [], "open_items": [],
        "claims": [{"text": "fastapi 最新版本是 0.115.0", "how_to_check": {"kind": "pypi", "target": "fastapi", "expect": "0.115.0"},
                    "check_after_hours": 0}]})])
    it = await wl.record_chat({"id": "u1", "text": "fastapi 最新版本是多少"}, [{"kind": "text", "text": "0.115.0"}])
    assert it["title"] == "查 fastapi 版本" and it["claims"][0]["check_after_ts"] <= time.time() + 1 and small.calls


async def test_secrets_are_never_stored(tmp_path):
    store, wl, *_ = setup(tmp_path, answers=[json.dumps({"title": "登录", "claims": [{"text": "token 是 ghp_abcdefghijklmnop123"}],
                                                          "open_items": ["密码是 hunter2 记得改"]})])
    assert await wl.record_chat({"text": "我的密码是 abc123，帮我记一下"}, [{"kind": "text", "text": "好的"}]) is None
    it = await wl.record_chat({"text": "帮我登录一下后台看看"}, [{"kind": "text", "text": "好"}])
    assert it["claims"] == [] and it["open_items"] == []
    assert "hunter2" not in json.dumps(store._x("SELECT * FROM work_items"), ensure_ascii=False)


async def test_a_continuation_supersedes_the_older_line(tmp_path):
    store, wl, *_ = setup(tmp_path, answers=None)
    store.create_task({"id": "t1", "prompt": "做报表", "machine_id": "m", "status": "failed"})
    a = await wl.record_task(store.task("t1"), "failed", "超时")
    store.create_task({"id": "t2", "prompt": "继续", "machine_id": "m", "status": "succeeded", "continues": "t1"})
    b = await wl.record_task(store.task("t2"), "succeeded", "做完了")
    assert wl.get(a["id"])["superseded_by"] == b["id"] and wl.get(a["id"])["status"] == "closed"


# ------------------------------------------------------------------------------------------ rechecks / contradictions
async def test_claim_recheck_finds_a_new_pypi_version(tmp_path, monkeypatch):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)

    async def fake_pypi(target, expect, store=None):
        return {"ok": True, "now": "2.33.0", "date": "2026-11-25", "source": "PyPI", "url": "https://pypi.org/project/requests/2.33.0/",
                "changed": expect != "2.33.0", "subject": target}
    monkeypatch.setitem(R.CHECKERS, "pypi", fake_pypi)
    wl.add(source="task", title="查 requests 版本", ask="requests 最新版本是多少", outcome="2.32.3", task_id="task-1",
           task_status="succeeded", claims=[{"text": "requests 最新版本是 2.32.3", "check_after_ts": NOW - 60,
                                             "how_to_check": {"kind": "pypi", "target": "requests", "expect": "2.32.3"}}], ts=NOW - 7200)
    rev = await rv.run(now=NOW)
    assert rev["actionable"] and rev["changed"][0]["correct"].startswith("requests 现在的最新版本是 2.33.0（2026-11-25")
    md = rev["markdown"]
    assert "之前说错 / 有变化" in md and "aiw://task/task-1" in md and md.index("2.33.0") < md.index("之前说的是")
    assert rev["decide"][0]["action"] == "redo" and any(o.startswith("按新情况重做") for o in rev["options"])
    await settle()
    assert notes.sent[0]["kind"] == "review" and notes.sent[0]["deep"].startswith("chat?m=") and not notes.sent[0].get("level")


async def test_a_failed_check_claims_nothing(tmp_path, monkeypatch):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)

    async def boom(target, expect, store=None):
        raise RuntimeError("network down")
    monkeypatch.setitem(R.CHECKERS, "pypi", boom)
    wl.add(source="chat", title="查版本", claims=[{"text": "x 是 1.0", "check_after_ts": NOW - 1,
                                                 "how_to_check": {"kind": "pypi", "target": "x", "expect": "1.0"}}], ts=NOW - 100)
    rev = await rv.run(now=NOW)
    assert not rev["changed"] and not rev["actionable"] and rev["posted"] is None and not notes.sent


async def test_contradictions_from_a_changed_memory_and_a_later_answer(tmp_path):
    judge = json.dumps({"contradict": True, "correct": "周会改到周四 10 点"})
    store, wl, rv, *_ = setup(tmp_path, answers=[judge])
    mem = Memory(store)
    old = mem.add("用户的周报放在 ~/reports 目录", "fact")
    wl.add(source="chat", title="周报位置", claims=[{"text": "周报放在 ~/reports 目录", "how_to_check": {"kind": "none"}}], ts=NOW - 3000)
    mem.add("用户的周报放在 ~/workspace/weekly 目录", "fact", supersedes=old["id"])
    wl.add(source="chat", title="周会时间", claims=[{"text": "周会在周三 10 点开", "how_to_check": {"kind": "none"}}], ts=NOW - 2000)
    wl.add(source="chat", title="周会时间改了", claims=[{"text": "周会在周四 10 点开", "how_to_check": {"kind": "none"}}], ts=NOW - 1000)
    rev = await rv.build_review(now=NOW)
    corrects = {f["correct"] for f in rev["changed"]}
    assert "用户的周报放在 ~/workspace/weekly 目录" in corrects and "周会改到周四 10 点" in corrects


# ------------------------------------------------------------------------------------------ push rules
async def test_dedupe_then_expiry_after_two_pushes(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    it = wl.add(source="task", title="打包 iOS", ask="打包 iOS 并上传", outcome="签名失败", task_id="t9", task_status="failed",
                open_items=["证书过期"], ts=NOW - 3600)
    r1 = await rv.run(now=NOW)
    assert r1["posted"] and wl.get(it["id"])["push_count"] == 1
    r2 = await rv.run(now=NOW + 60)            # same finding, same day: not again
    assert r2["posted"] is None and not r2["unfinished"]
    r3 = await rv.run(now=NOW + 86400 + 60)    # next evening: one more reminder
    assert r3["posted"] and wl.get(it["id"])["push_count"] == 2
    r4 = await rv.run(now=NOW + 2 * 86400 + 60)   # told twice, no answer: expired, never again
    assert r4["posted"] is None and wl.get(it["id"])["status"] == "expired"


async def test_no_news_no_message_no_push(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    wl.add(source="chat", title="聊了聊天气", outcome="晴", ts=NOW - 100)
    rev = await rv.run(now=NOW)
    await settle()
    assert not rev["actionable"] and rev["posted"] is None and chat.msgs == {} and notes.sent == []
    assert "今日小结" in rev["markdown"]   # GET /api/review/today still shows the summary


async def test_prefs_mute_topic_kind_and_review_off(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    wl.add(source="task", title="打包 iOS", task_id="t1", task_status="failed", ts=NOW - 100)
    assert R.prefs_command(store, "别再提打包 iOS 了").startswith("好的")
    assert "打包 iOS" in R.prefs(store)["muted_topics"]
    assert (await rv.run(now=NOW))["posted"] is None
    assert R.prefs_command(store, "少推没做完的") and "unfinished" in R.prefs(store)["muted_kinds"]
    assert R.prefs_command(store, "关掉每日回顾") and R.prefs(store)["enabled"] is False
    assert await rv.tick(NOW) is None
    assert R.prefs_command(store, "别再提醒我，帮我查一下天气") is None   # a real request is left alone
    assert R.prefs_command(store, "今天天气怎么样") is None


# ------------------------------------------------------------------------------------------ 猜你想要
def _repeat(wl, n=3, title="整理 Hacker News 前五条", hour_shift=0):
    for i in range(n):
        wl.add(source="task", title=title, ask="打开 Hacker News，把前 5 条整理成表格", task_id=f"hn{i}",
               task_status="succeeded", ts=NOW - (i + 1) * 86400 + hour_shift)


async def test_repeated_ask_suggests_a_routine_and_the_tap_creates_it(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    _repeat(wl)
    rev = await rv.run(now=NOW)
    s = rev["suggest"][0]
    assert s["kind"] == "repeated_ask" and s["action"]["type"] == "routine" and "3 次" in s["because"]
    assert "猜你想要" in rev["markdown"]
    ask = chat.msgs[rev["posted"]["ask_id"]]
    label = next(o for o in ask["data"]["options"] if o.startswith("建成定时任务"))
    res = await rv.answer(chat, ask, label)
    r = store.all("routines")[0]
    assert r["prompt"].startswith("打开 Hacker News") and r["schedule"].startswith("daily ") and r["next_run"] > time.time()
    assert "建好了定时任务" in res["messages"][0]["text"]
    assert R.prefs(store)["sugg_feedback"]["repeated_ask"]["accepted"] == 1


async def test_a_suggestion_kind_ignored_twice_is_muted(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    _repeat(wl, title="整理 HN")
    assert (await rv.run(now=NOW))["suggest"]
    _repeat(wl, title="导出账单 CSV")
    store.kv_set(R.SUGGESTED_KV, {})            # pretend a week went by for the dedupe
    assert (await rv.run(now=NOW + 60))["suggest"]   # this run settles the first one as ignored
    store.kv_set(R.SUGGESTED_KV, {})
    rev = await rv.run(now=NOW + 120)            # the second one ignored too -> repeated_ask muted for 14 days
    p = R.prefs(store)
    assert R.sugg_muted(p, "repeated_ask", NOW + 120) and not R.sugg_muted(p, "repeated_ask", NOW + 15 * 86400)
    assert not rev["suggest"]
    assert R.prefs_command(store, "别再推荐升级") and p is not None and R.prefs(store)["sugg_muted"]["release_upgrade"] == 0


async def test_the_same_suggestion_is_not_repeated_within_a_week(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    _repeat(wl)
    first = await rv.run(now=NOW)
    assert first["suggest"]
    again = await rv.build_review(now=NOW + 3 * 86400)
    assert not any(s["key"] == first["suggest"][0]["key"] for s in again["suggest"])
    assert NOW + 8 * 86400 - store.kv_get(R.SUGGESTED_KV)[first["suggest"][0]["key"]] > R.SUGG_DEDUPE_S   # free again after 7 days


async def test_no_grounding_means_no_suggestion_section(tmp_path):
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None)
    wl.add(source="task", title="查一下明天的天气", ask="查一下明天的天气", task_status="succeeded", ts=NOW - 100)   # once only
    wl.add(source="task", title="付款", ask="帮我把这个月房租付款给房东", task_id="p1", task_status="succeeded", ts=NOW - 86400)
    wl.add(source="task", title="付款", ask="帮我把这个月房租付款给房东", task_id="p2", task_status="succeeded", ts=NOW - 2 * 86400)
    rev = await rv.build_review(now=NOW)
    assert rev["suggest"] == [] and "猜你想要" not in rev["markdown"]   # repeated, but payments are never offered


async def test_release_of_a_dependency_offers_upgrade_and_test(tmp_path, monkeypatch):
    store, wl, rv, *_ = setup(tmp_path, answers=None)

    async def fake(target, expect, store=None):
        return {"ok": True, "now": "0.116.0", "date": "2026-11-24", "source": "PyPI", "url": "", "changed": True, "subject": target}
    monkeypatch.setitem(R.CHECKERS, "pypi", fake)
    wl.add(source="task", title="更新依赖", ask="把 github.com/hackjown/ai-workstation 的 requirements 里的 fastapi 固定版本",
           task_id="d1", task_status="succeeded", claims=[{"text": "fastapi 最新是 0.115.0", "check_after_ts": NOW - 5,
                                                           "how_to_check": {"kind": "pypi", "target": "fastapi", "expect": "0.115.0"}}],
           ts=NOW - 5000)
    rev = await rv.build_review(now=NOW)
    up = [s for s in rev["suggest"] if s["kind"] == "release_upgrade"][0]
    assert "github.com/hackjown/ai-workstation" in up["action"]["prompt"] and "不要推送" in up["action"]["prompt"]


# ------------------------------------------------------------------------------------------ device: calendar / reminders
async def test_capability_not_granted_is_skipped_with_one_hint(tmp_path):
    hub = FakeHub(caps=["location.read"])
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None, hub=hub)
    wl.add(source="task", title="打包", task_id="t1", task_status="failed", ts=NOW - 100)
    rev = await rv.run(now=NOW)
    assert hub.calls == [] and rev["tomorrow"] == [] and "设备能力" in rev["hint"]
    wl.add(source="task", title="另一件", task_id="t2", task_status="failed", ts=NOW - 50)
    rev2 = await rv.run(now=NOW + 120)
    assert rev2["hint"] == ""   # once, then never again
    # granted but the phone is offline: skipped silently too
    hub2 = FakeHub(caps=["calendar.read"], online=False)
    assert await DR.DeviceReview(store, hub2).tomorrow(NOW) == [] and hub2.calls == []
    assert (await DR.DeviceReview(store, hub2).sync_events(NOW)) == {"synced": False}


async def test_tomorrow_conflicts_early_start_and_overdue_reminders(tmp_path):
    t0 = DR.day_start(NOW) + 86400
    events = [{"id": "e1", "title": "晨跑", "start": DR.iso(t0 + 6.5 * 3600), "end": DR.iso(t0 + 7.5 * 3600)},
              {"id": "e2", "title": "周会", "start": DR.iso(t0 + 10 * 3600), "end": DR.iso(t0 + 11 * 3600)},
              {"id": "e3", "title": "面试", "start": DR.iso(t0 + 10.5 * 3600), "end": DR.iso(t0 + 11.5 * 3600)}]
    rems = [{"id": "r1", "title": "交水费", "due": DR.iso(NOW - 2 * 86400), "done": False},
            {"id": "r2", "title": "取快递", "due": DR.iso(t0 + 18 * 3600), "done": False}]
    hub = FakeHub(caps=["calendar.read", "reminders.read"],
                  answer=lambda t, a: {"events": events} if t == "calendar_list" else {"reminders": rems})
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None, hub=hub)
    rev = await rv.run(now=NOW)
    kinds = {c["kind"] for c in rev["tomorrow"]}
    assert {"conflict", "early", "overdue", "due", "agenda"} <= kinds and rev["posted"]
    assert "**明天**" in rev["markdown"] and "交水费" in rev["markdown"]
    again = await rv.run(now=NOW + 60)
    assert again["posted"] is None   # the same calendar items are not told twice


async def test_event_moved_reschedules_the_reminder_once_and_cancelled_drops_it(tmp_path):
    state = {"start": NOW + 3600, "gone": False}
    hub = FakeHub(caps=["calendar.read"], answer=lambda t, a: {"events": [] if state["gone"] else
                  [{"id": "ev1", "title": "牙医", "start": DR.iso(state["start"]), "end": DR.iso(state["start"] + 1800)}]})
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None, hub=hub)
    dv = rv.device
    await dv.sync_events(NOW)
    assert await dv.fire_due(NOW + 2700 - 60) == []          # 16 min before: not yet
    assert await dv.fire_due(NOW + 2700 + 1) == ["ev1"]      # 15 min before
    assert await dv.fire_due(NOW + 2700 + 30) == []          # once
    state["start"] = NOW + 7200                               # moved one hour later
    st = await dv.sync_events(NOW + 2760)
    st2 = await dv.sync_events(NOW + 2800)                    # a second sync with the same time changes nothing
    assert st["moved"] == 1 and st2["moved"] == 0
    assert await dv.fire_due(NOW + 7200 - 900 + 1) == ["ev1"] and await dv.fire_due(NOW + 7200 - 800) == []
    ev = [n for n in notes.sent if n["kind"] == "event"]
    assert len(ev) == 2 and ev[0]["deep"] == "chat" and "牙医" in ev[0]["title"]
    state["gone"] = True
    st3 = await dv.sync_events(NOW + 7000)
    assert st3["dropped"] == 1 and store.kv_get(DR.EVENTS_KV) == {}


# ------------------------------------------------------------------------------------------ device: health
def _health_hub(today_steps=4000, today_sleep=300):
    d0 = DR.day_start(NOW)

    def ans(tool, a):
        start = DR.parse_iso(a["start"])
        k = a["kind"]
        if k == "steps":
            return {"steps": today_steps if start >= d0 else 8000}
        if k == "sleep":
            return {"asleep_minutes": today_sleep if start >= d0 - 6 * 3600 - 1 else 420, "segments": [{"start": "x"}]}
        if k == "workouts":
            return {"workouts": [{"minutes": 30, "kcal": 250}]} if start >= d0 else {"workouts": []}
        if k == "heart_rate":
            return {"count": 100, "avg": 72, "recent": [{"at": "x", "bpm": 70}]}
        return None   # active_energy / resting_heart_rate / hrv: this app does not support them yet
    return ans


async def test_health_summary_uses_the_7_day_baseline_and_stores_only_aggregates(tmp_path):
    hub = FakeHub(caps=["health.read"], answer=_health_hub())
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None, hub=hub)
    res = await rv.device.health_summary(NOW)
    assert res["baseline"]["days"] == 7 and res["baseline"]["steps"] == 8000 and res["baseline"]["sleep_min"] == 420
    assert "7 天平均 8,000，-50%" in res["text"] and "平均 7 小时 00 分" in res["text"]
    assert res["tip"] == "昨晚睡得比你平时少，今晚可以早点休息。"   # one suggestion, a habit, no diagnosis
    rows = store._x("SELECT * FROM health_daily")
    assert len(rows) == 8 and set(rows[0]) == {"day", "steps", "sleep_min", "active_kcal", "workout_min", "workout_kcal",
                                                "hr_avg", "resting_hr", "hrv", "updated_at"}
    assert "segments" not in json.dumps(rows) and "recent" not in json.dumps(rows)
    assert [n for n in notes.sent if n["kind"] == "health"][0]["deep"].startswith("chat?m=")
    # the unsupported newer kinds are asked once per process, not for every backfilled day
    assert sum(1 for t, a in hub.calls if a.get("kind") == "hrv") == 1


async def test_health_summary_off_or_not_granted_sends_nothing(tmp_path):
    hub = FakeHub(caps=["health.read"], answer=_health_hub())
    store, wl, rv, chat, notes, _ = setup(tmp_path, answers=None, hub=hub)
    R.save_prefs(store, {"health_enabled": False})
    assert await rv.device.health_summary(NOW) is None
    R.save_prefs(store, {"health_enabled": True, "health_summary_time": "off"})
    assert await rv.device.health_summary(NOW) is None
    await rv.device.tick(NOW)
    assert hub.calls == [] and notes.sent == []
    hub2 = FakeHub(caps=["calendar.read"], answer=_health_hub())
    assert await DR.DeviceReview(store, hub2, notes).health_summary(NOW) is None and hub2.calls == []


def test_abnormal_looking_values_are_only_worth_watching():
    text, tip = DR.compose({"steps": 9000, "sleep_min": 430, "resting_hr": 70},
                           {"days": 7, "steps": 8000, "sleep_min": 420, "resting_hr": 60})
    assert "值得留意" in tip and not any(w in tip for w in ("病", "诊断", "就医", "药"))


# ------------------------------------------------------------------------------------------ wiring (chat + HTTP)
from test_chat import stack  # noqa: E402,F401


async def test_chat_and_http_wiring(stack):
    client, app, scripts, fake = stack
    r = (await client.put("/api/review/prefs", json={"time": "21:15", "event_lead_min": 10})).json()
    assert r["time"] == "21:15" and r["event_lead_min"] == 10
    assert (await client.put("/api/review/prefs", json={"time": "25:00"})).status_code == 400
    r = (await client.post("/api/chat/send", json={"text": "别再提小红书", "stream": False})).json()
    assert r["events"][1]["data"]["route"] == "review_prefs" and "小红书" in (await client.get("/api/review/prefs")).json()["muted_topics"]
    wl = app.state.worklog
    for i in range(2):
        wl.add(source="task", title="整理 HN 前五", ask="打开 HN 整理前五", task_id=f"x{i}", task_status="succeeded",
               ts=time.time() - (i + 1) * 86400)
    wl.add(source="task", title="打包", ask="打包 iOS", task_id="tt", task_status="failed", ts=time.time() - 60)
    assert (await client.get("/api/review/today")).json()["posted"] is None
    rev = (await client.post("/api/review/run", json={})).json()
    assert rev["posted"]["id"] and rev["suggest"]
    day = rev["day"]
    assert len((await client.get(f"/api/worklog?day={day}")).json()) >= 1
    ask_id = rev["posted"]["ask_id"]
    label = next(o for o in rev["options"] if o.startswith("建成定时任务"))
    out = (await client.post(f"/api/chat/messages/{ask_id}/answer", json={"choice": label})).json()
    assert out["ok"] and (await client.get("/api/routines")).json()[0]["name"].startswith("整理 HN")
    assert (await client.post("/api/review/feedback", json={"kind": "next_step", "verdict": "never"})).json()["sugg_muted"]["next_step"] == 0


async def test_a_real_task_end_and_a_chat_exchange_write_the_worklog(stack):
    from test_chat import wait_for
    client, app, scripts, fake = stack
    fake.route = "task"
    scripts.append([("done", {"summary": "做好了，见 https://pypi.org/project/httpx/0.28.1/"})])
    r = (await client.post("/api/chat/send", json={"text": "在电脑上查一下 httpx 的版本", "stream": False})).json()
    tid = r["messages"][-1]["data"]["task_id"]

    async def logged():
        return app.state.worklog.store._x("SELECT * FROM work_items WHERE task_id=?", (tid,))
    row = (await wait_for(logged, 15))[0]
    assert row["source"] == "task" and row["task_status"] == "succeeded" and "httpx" in row["claims"]
    fake.route = "chat"
    await client.post("/api/chat/send", json={"text": "今天适合跑步吗", "stream": False})

    async def chat_logged():
        return app.state.worklog.store._x("SELECT * FROM work_items WHERE source='chat'")
    assert (await wait_for(chat_logged, 10))[0]["ask"] == "今天适合跑步吗"
