"""v3.9.52: 早报 html (not a zip) with the weather inside it, the 金额大写 check no longer stopping news pages, and a
.mov picked from 相册 that arrived as photo-<ts>.jpg (task-22d) stored and named as the video it is."""
import base64
import os
import types
import zipfile

import pytest


# ------------------------------------------------------------------ 金额大写: units in a news page are not amounts
def test_units_in_news_are_not_daxie_amounts():
    from agent_runtime import money_cn as m
    assert m.check_text("营收120亿元，同比增长30万元；融资数千万元，拾万元以上") == []
    bad = m.check_text("合计 171,866.00 元（大写：壹拾柒万零壹仟捌佰陆拾陆元整）")
    assert bad and bad[0]["expected"] == "壹拾柒万壹仟捌佰陆拾陆元整"
    assert m.check_text("合计 171,866.00 元（大写：壹拾柒万壹仟捌佰陆拾陆元整）") == []
    assert "Never zip" in m.refusal("报价.docx", bad)


def test_money_refusals_are_counted_per_task_not_per_name():
    from agent_runtime.loop import AgentLoop
    page = "<p>合计 171,866.00 元（大写：壹拾柒万零壹仟捌佰陆拾陆元整）</p>".encode()
    me = types.SimpleNamespace(history=[], step=1)
    res = lambda n: {"path": n, "content_b64": base64.b64encode(page).decode()}   # noqa: E731
    assert AgentLoop._money_check(me, res("a.html"))
    assert AgentLoop._money_check(me, res("b.html"))     # renamed copy: still the 2nd refusal
    assert AgentLoop._money_check(me, res("c.html")) is None   # the 3rd goes through with a warning


# ------------------------------------------------------------------ uploads: a video named .jpg
def test_sniff_and_fix_kind(tmp_path):
    from aiw_api import uploads as U
    mov = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 64
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    assert U.sniff(mov) == "mov" and U.sniff(mp4) == "mp4" and U.sniff(heic) == "heic" and U.sniff(jpg) == "jpg"
    for data, want in ((mov, "photo-1794874653.mov"), (mp4, "photo-1794874653.mp4"), (jpg, "photo-1794874653.jpg"),
                       (heic, "photo-1794874653.heic")):
        local = tmp_path / f"1794874653-photo-1794874653.jpg"
        local.write_bytes(data)
        name, path = U.fix_kind("photo-1794874653.jpg", str(local))
        assert name == want and os.path.basename(path) == f"1794874653-{want}" and os.path.isfile(path)
        os.remove(path)
    doc = tmp_path / "1-报价.docx"
    doc.write_bytes(b"PK\x03\x04" + b"\x00" * 30)
    assert U.fix_kind("报价.docx", str(doc)) == ("报价.docx", str(doc))


def test_upload_endpoint_renames_a_video_sent_as_photo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    os.makedirs(s.artifacts_dir, exist_ok=True)
    app = build_app(s)
    mov = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x01" * 2048
    with TestClient(app) as c:
        h = {"Authorization": "Bearer t"}
        r = c.post("/api/uploads/raw?name=photo-1794874653.jpg&local_only=true", content=mov, headers=h)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["name"] == "photo-1794874653.mov" and j["kind"] == "video" and j["artifact"].endswith(".mov")
        assert open(j["artifact"], "rb").read() == mov


# ------------------------------------------------------------------ 早报: weather in the page, html not zip
WTTR = {"current_condition": [{"temp_C": "24", "FeelsLikeC": "27", "humidity": "93", "windspeedKmph": "11",
                               "winddirDegree": "90", "cloudcover": "52", "visibility": "10", "pressure": "1014",
                               "lang_zh": [{"value": "局部多云"}]}],
        "weather": [{"mintempC": "22", "maxtempC": "29", "uvIndex": "7",
                     "astronomy": [{"sunrise": "06:52 AM", "sunset": "05:58 PM"}],
                     "hourly": [{"time": str(h * 100), "tempC": str(22 + h // 3), "chanceofrain": "60" if h == 15 else "10",
                                 "precipMM": "1.2" if h == 15 else "0", "WindGustKmph": "30",
                                 "lang_zh": [{"value": "阵雨" if h == 15 else "多云"}]} for h in range(0, 24, 3)]}]}


def test_wttr_render_and_html_block(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from aiw_api import brief_weather as B
    md = B.render_wttr(WTTR, "海口", now=datetime(2026, 11, 17, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert "今日天气 · 海口 · 11月17日 周二" in md and "22~29°C" in md and "降水：概率 60%" in md and "分时段" in md
    page = tmp_path / "2026-11-17.html"
    page.write_text("<html><head></head><body class='x'><h1>早报</h1></body></html>", encoding="utf-8")
    assert B.inject_html(str(page), md) and not B.inject_html(str(page), md)
    t = page.read_text(encoding="utf-8")
    assert t.index('id="aiw-weather"') < t.index("<h1>早报") and t.count('id="aiw-weather"') == 1


async def test_weather_falls_back_to_wttr(tmp_path, monkeypatch):
    from aiw_api import brief_weather as B
    B._CACHE.clear()

    async def boom(store, timeout=12.0):
        raise TimeoutError("open-meteo")

    async def wttr(store, timeout=15.0):
        return "### 今日天气 · 海口\n**多云**"
    monkeypatch.setattr(B, "fetch", boom)
    monkeypatch.setattr(B, "fetch_wttr", wttr)
    st = types.SimpleNamespace(kv_get=lambda k, d=None: {"city": "海口"}, kv_set=lambda k, v: None)
    assert (await B.today_text(st)).startswith("### 今日天气 · 海口")


async def test_routine_posts_the_html_with_weather_not_the_zip(tmp_path, monkeypatch):
    from aiw_api import brief_weather as B
    from aiw_api import chat as C
    page = "<html><body><h1>新闻早报</h1></body></html>"
    z = tmp_path / "aiw-news-2026-11-17.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("aiw-news-2026-11-17.html", page)

    async def wt(store):
        return "### 今日天气 · 海口\n**多云**，22~29°C\n- 降水：概率 60%"
    monkeypatch.setattr(B, "today_text", wt)
    monkeypatch.setattr(B, "routine_for", lambda store, task: {"name": "新闻早报", "prompt": "运行 aiw_news.py，把生成的html发给我"})
    added = []
    me = types.SimpleNamespace(store=types.SimpleNamespace(kv_get=lambda k, d=None: {}, kv_set=lambda k, v: None),
                               add=lambda role, kind, text, data, task_id=None: added.append((text, data)) or {"id": "m1"})
    task = types.SimpleNamespace(id="task-ea6", thread_id="thr-1", prompt="")
    await C.Chat.post_routine_result(me, task, {"status": "succeeded", "result": "已生成。", "files": [str(z)]})
    text, data = added[0]
    assert "今日天气" in text and data["files"][0]["name"] == "aiw-news-2026-11-17.html"
    html = open(data["files"][0]["path"], encoding="utf-8").read()
    assert 'id="aiw-weather"' in html and "降水：概率 60%" in html
