"""3.8.2: a price watch runs only on its own computer (the user chose 本地vm, logged in to JD there, and still got
captcha cards while tasks ran on the cloud computers). A watch created from chat used to land on the computer the
chat task ran on, with no way to name another; an old watch without a computer followed settings.machines[0]."""
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import pytest
from aiw_api import shopwatch as sw

from test_shop_nocdp import Comp, Notes, chat, store  # noqa: F401
from test_shopwatch import JD

LOCAL = {"id": "ai-linux-001", "name": "本地vm", "provider": "proxmox", "provider_vm_id": "104",
         "agent_endpoint": "ws://127.0.0.1:1", "desktop_endpoint": ""}
CLOUD = {"id": "cloud-instabox-2", "name": "aiw-2", "provider": "instabox", "provider_vm_id": "m_x",
         "agent_endpoint": "wss://x", "desktop_endpoint": "instabox:"}
CAPTCHA = {"url": "https://item.jd.com/100012043978.html", "text": "请完成安全验证 拖动滑块", "cdp": "http://127.0.0.1:9222",
           "profile": {"port": 9222, "dir": "/home/aiw/.config/aiw-chrome", "cookies": 0}}


@pytest.fixture
def two(store):  # noqa: F811
    store.delete_machine("m-nocdp")
    store.upsert_machine(CLOUD)
    store.upsert_machine(LOCAL)
    return store


class Sessions:
    """Records which computer each check asked for; busy / online per computer."""

    class settings:
        machines = []

    def __init__(self, comps, busy=(), offline=()):
        self.comps, self.busy, self.offline, self.asked = comps, set(busy), set(offline), []

    def computer(self, mid):
        self.asked.append(mid)
        return self.comps[mid]

    def busy_session(self, mid):
        return object() if mid in self.busy else None

    async def _probe(self, mid, timeout=5.0):
        return mid not in self.offline

    def _vision_llm(self):
        return None


class Sched:
    def __init__(self, store, sessions):
        self.store, self.sessions, self.notifier = store, sessions, Notes()


def _watch(store, mid="ai-linux-001"):
    f = sw.watch_fields({"url": "https://item.jd.com/100012043978.html", "target_price": "120"}, default_machine=mid)
    return store.insert("watches", f)


def test_resolve_by_id_name_and_local(two):
    assert sw.resolve_machine(two, "ai-linux-001") == "ai-linux-001"
    assert sw.resolve_machine(two, "本地vm") == "ai-linux-001" and sw.resolve_machine(two, "本地 VM") == "ai-linux-001"
    assert sw.resolve_machine(two, "本地") == "ai-linux-001" and sw.resolve_machine(two, "云电脑1") == "cloud-instabox-2"
    assert sw.resolve_machine(two, "办公室那台") is None
    assert sw.default_machine(two) == "ai-linux-001"     # a local computer first, even though the cloud one sorts first


async def test_chat_created_watch_uses_the_named_computer_not_the_tasks(two):
    class T:
        machine_id = "cloud-instabox-2"   # the chat task ran on the cloud computer
    _, call = sw.create_tool(two, T())
    r = await call({"url": "https://item.jd.com/100012043978.html", "target_price": 120, "machine": "本地vm"})
    assert two.get("watches", r["created"])["machine_id"] == "ai-linux-001" and r["machine_name"] == "本地vm"
    r2 = await call({"url": "https://item.jd.com/100012043978.html", "target_price": 120})
    assert two.get("watches", r2["created"])["machine_id"] == "ai-linux-001"     # default: local, not the task's
    bad = await call({"url": "https://item.jd.com/100012043978.html", "target_price": 120, "machine": "办公室那台"})
    assert "error" in bad and "本地vm" in bad["error"]


async def test_check_runs_only_on_the_watchs_computer(two):
    local, cloud = Comp(ensure={"ok": True, "port": 9222}, page=JD), Comp(page=JD)
    ses = Sessions({"ai-linux-001": local, "cloud-instabox-2": cloud}, busy={"cloud-instabox-2"})
    w = _watch(two)
    await sw.check(Sched(two, ses), w)
    assert ses.asked == ["ai-linux-001"] and not cloud.calls


async def test_busy_or_offline_computer_waits_and_never_borrows(two, chat):  # noqa: F811
    local, cloud = Comp(page=JD), Comp(page=JD)
    ses = Sessions({"ai-linux-001": local, "cloud-instabox-2": cloud}, busy={"ai-linux-001"})
    sch = Sched(two, ses)
    w = _watch(two)
    res = await sw.check(sch, w)
    assert res["waiting"] == "busy" and ses.asked == [] and not cloud.calls
    w2 = two.get("watches", w["id"])
    assert w2["shop_status"] == "machine_busy" and "本地vm" in w2["status_note"] and w2["next_check"] > 0
    said = [m["text"] for m in chat.history()["messages"]]
    assert any("本地vm" in t and "不会换到别的电脑" in t for t in said)
    ses.busy, ses.offline = set(), {"ai-linux-001"}
    res = await sw.check(sch, two.get("watches", w["id"]))
    assert res["waiting"] == "offline" and ses.asked == []
    assert any("本地vm" in t for _, t, _b in sch.notifier.sent)


async def test_missing_computer_is_reported_not_replaced(two, chat):  # noqa: F811
    ses = Sessions({"cloud-instabox-2": Comp(page=JD)})
    w = _watch(two, mid="ai-linux-009")
    res = await sw.check(Sched(two, ses), w)
    assert res["result"]["status"] == "no_machine" and ses.asked == []


async def test_old_watch_without_computer_is_pinned_to_the_local_one(two):
    ses = Sessions({"ai-linux-001": Comp(ensure={"ok": True, "port": 9222}, page=JD), "cloud-instabox-2": Comp(page=JD)})
    w = two.insert("watches", {**sw.watch_fields({"url": "https://item.jd.com/1.html", "target_price": "9"}), "machine_id": None})
    await sw.check(Sched(two, ses), w)
    assert two.get("watches", w["id"])["machine_id"] == "ai-linux-001" and ses.asked == ["ai-linux-001"]


async def test_captcha_card_names_the_computer_and_the_profile(two, chat):  # noqa: F811
    local = Comp(ensure={"ok": True, "port": 9222}, page=CAPTCHA)
    ses = Sessions({"ai-linux-001": local, "cloud-instabox-2": Comp(page=JD)})
    sch = Sched(two, ses)
    w = _watch(two)
    res = await sw.check(sch, w)
    assert res["result"]["status"] == "captcha" and "aiw-chrome" in res["result"]["note"]
    asks = [m for m in chat.history()["messages"] if m["kind"] == "ask"]
    assert asks and asks[-1]["data"]["machine"] == "本地vm" and asks[-1]["data"]["machine_id"] == "ai-linux-001"
    assert "本地vm" in asks[-1]["data"]["title"] and "本地vm" in asks[-1]["text"]
    assert any("本地vm" in title for _, title, _b in sch.notifier.sent)


def test_probe_reports_the_profile(tmp_path):
    from aiw_api import shop_probe as sp
    proc = tmp_path / "proc" / "42"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join([b"/usr/bin/chromium", b"--remote-debugging-port=9222",
                                              b"--user-data-dir=" + str(tmp_path / "prof").encode()]))
    (tmp_path / "prof" / "Default").mkdir(parents=True)
    (tmp_path / "prof" / "Default" / "Cookies").write_bytes(b"x" * 2048)
    p = sp.profile_of("http://127.0.0.1:9222", str(tmp_path / "proc"))
    assert p["dir"].endswith("prof") and p["cookies"] == 2048
