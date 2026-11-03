"""v3.9.17: Bark pushes that went missing (the 9:00 news digest) or rang only while the phone was unlocked."""
import asyncio
import json

import httpx
import pytest

from aiw_api import notify as N
from aiw_api.store import Store


def test_default_sound_is_bundled_in_bark(monkeypatch):
    monkeypatch.delenv("AIW_BARK_SOUND", raising=False)
    monkeypatch.delenv("AIW_BARK_SOUND_MODE", raising=False)
    # the uploaded voice ringtones cannot be read by iOS while the phone is locked: the default is a Bark sound
    assert N.bark_params("需要你确认", "confirm", "task/t", {})["sound"] == "alarm"
    assert N.bark_params("任务完成", "done", "task/t", {"bark_sound": "on"})["sound"] == "glass"   # old "on"
    assert N.bark_params("任务完成", "done", "task/t", {"bark_sound": "voice"})["sound"] == "aiw-wancheng"
    assert "sound" not in N.bark_params("t", "done", "task/t", {"bark_sound": "off"})
    assert set(N.BUILTIN) == set(N.SOUNDS)


def test_critical_option_and_routine_level():
    p = N.bark_params("需要你确认", "confirm", "task/t", {"bark_critical": "on", "bark_volume": "8"})
    assert p["level"] == "critical" and p["volume"] == "8"
    assert N.bark_params("x", "confirm", "task/t", {})["level"] == "timeSensitive"
    assert N.bark_params("x", "done", "task/t", {}, level="timeSensitive")["level"] == "timeSensitive"
    assert "level" not in N.bark_params("x", "done", "task/t", {})


def test_text_with_slashes_goes_in_the_body_not_the_path():
    body = "今日新闻：https://news.example.com/a/b  1/2 版 ..." * 30
    u, payload = N.bark_post("https://api.day.app/KEY/", "⏰ 新闻汇总 已生成", body, "done", "task/t1", {})
    assert u == "https://api.day.app/KEY" and payload["body"] == body[:1000] and payload["title"].startswith("⏰")


class Seq:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, request):
        self.calls.append(request)
        return self.replies.pop(0) if self.replies else httpx.Response(200, json={"code": 200})


@pytest.fixture
def http(monkeypatch):
    def install(replies):
        rec = Seq(replies)
        real = httpx.AsyncClient
        monkeypatch.setattr(N.httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(rec)}))
        monkeypatch.setattr(N.asyncio, "sleep", _nosleep)
        return rec
    return install


async def _nosleep(*_a, **_k):
    return None


async def test_failed_push_is_retried_and_logged(tmp_path, http):
    st = Store(str(tmp_path / "db.sqlite"))
    st.kv_set("notify", {"bark_url": "https://api.day.app/KEY"})
    rec = http([httpx.Response(400, json={"code": 400, "message": "bad"}), httpx.Response(404, text="nf"),
                httpx.Response(200, json={"code": 200})])
    n = N.Notifier(st)
    await n._push("新闻汇总 已生成", "a/b/c", "done", "task/t1", level="timeSensitive")
    assert [c.method for c in rec.calls] == ["POST", "GET", "POST"]
    assert json.loads(rec.calls[0].content)["level"] == "timeSensitive"
    log = st.kv_get("push_log", [])
    assert log[-1]["ok"] is True and log[-1]["try"] == 2


async def test_push_that_never_lands_is_logged_as_failed(tmp_path, http):
    st = Store(str(tmp_path / "db.sqlite"))
    st.kv_set("notify", {"bark_url": "https://api.day.app/KEY"})
    http([httpx.Response(500, text="x")] * 4)
    await N.Notifier(st)._push("t", "b", "done", "task/t1")
    log = st.kv_get("push_log", [])
    assert log[-1]["ok"] is False and "500" in log[-1]["error"]


def test_routine_results_are_named_and_time_sensitive():
    import pathlib
    src = (pathlib.Path(N.__file__).parent / "sessions.py").read_text(encoding="utf-8")
    assert 'task, "source", "") == "routine"' in src and 'level = "timeSensitive"' in src
    main = (pathlib.Path(N.__file__).parent / "main.py").read_text(encoding="utf-8")
    assert '"/api/settings/notify/log"' in main and '"bark_critical"' in main
