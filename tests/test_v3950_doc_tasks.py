"""v3.9.50: document follow-ups are not code fixes (task-271), scratch deletes in the task folder, 人民币大写 checked at
send_file, document acceptance without verify_html, and routine results (the 7:30 新闻早报) in the chat with the weather."""
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from agent_runtime import intent as I
from agent_runtime import money_cn as M
from agent_runtime.devflow import DevFlow
from tool_router.policy import PolicyEngine

DOC_SIG = {"sent_files": ["/data/home/workspace/tasks/task-0384a51cf9/out/响应文件.pdf"],
           "script_runs": ["~/workspace/tasks/task-0384a51cf9/make_doc.py"], "repo_paths": []}


# ---------------------------------------------------------------- intent
def test_task_271_follow_up_is_a_document_edit_not_a_code_fix():
    t = "在这个基础上修改，首先税率为1%，总价不要171900元整，可以上下浮动一点价格。最终我要word版本"
    assert I.classify(t, DOC_SIG)["intent"] == I.OTHER
    assert I.classify(t, {})["intent"] == I.OTHER   # "word版本": the ASCII boundary now matches


@pytest.mark.parametrize("t", ["把第三页的公司名称修正一下", "单价改成两位小数", "授权代表换成张三，日期改为18号",
                               "第5页的身份证图片放错了，修复一下"])
def test_any_follow_up_on_a_delivered_document_stays_a_document_edit(t):
    assert I.classify(t, DOC_SIG)["intent"] == I.OTHER


def test_words_about_code_still_make_a_code_fix_in_a_document_thread():
    assert I.classify("修改脚本 make_doc.py 让它多输出一页", DOC_SIG)["intent"] == I.DEV_FIX


def test_task_830_web_page_fix_is_still_dev_fix():
    sig = {"sent_files": ["~/ai-workstation/news/out/index.html"], "script_runs": ["~/ai-workstation/news/aiw_news.py"]}
    assert I.classify("改了样式点击又进入不了详情，GitHub也没了，修复", sig)["intent"] == I.DEV_FIX


def test_delivered_docs_only():
    assert I.delivered_docs_only(DOC_SIG)
    assert not I.delivered_docs_only({"sent_files": ["a.docx", "b/index.html"]})
    assert not I.delivered_docs_only({})


# ---------------------------------------------------------------- policy
@pytest.mark.parametrize("cmd", [
    "cd ~/workspace/tasks/task-0384a51cf9 && rm -f out/*.pdf out/pg-*.png && python3 make_doc.py",
    "cd ~/workspace/tasks/task-0384a51cf9/out && rm -f pg-*.png && pdftoppm -r 90 -png *.pdf pg",
    "cd ~/workspace/tasks/task-0384a51cf9 && python3 make_doc.py && cd out && rm -f *.pdf pg-*.png && soffice --headless --convert-to pdf *.docx",
])
def test_clearing_own_output_in_the_task_folder_needs_no_confirmation(cmd):
    assert PolicyEngine().check("shell.exec", {"command": cmd}).allowed


@pytest.mark.parametrize("cmd", ["cd ~/workspace/tasks/task-x && rm -rf *", "cd ~/workspace/tasks/task-x && rm -f ../other/*.pdf",
                                 "cd ~/workspace && rm -f *.pdf", "rm -f ~/Documents/*.pdf", "cd ~/workspace/tasks/t && rm -f .*"])
def test_other_bulk_deletes_still_ask(cmd):
    assert PolicyEngine().check("shell.exec", {"command": cmd}).needs_confirm


# ---------------------------------------------------------------- 人民币大写
@pytest.mark.parametrize("v,s", [(171866, "壹拾柒万壹仟捌佰陆拾陆元整"), (171900, "壹拾柒万壹仟玖佰元整"),
                                 (170164.36, "壹拾柒万零壹佰陆拾肆元叁角陆分"), (100500, "壹拾万零伍佰元整"),
                                 (1000005, "壹佰万零伍元整"), (1.05, "壹元零伍分"), (10.5, "壹拾元伍角整"),
                                 (100010000, "壹亿零壹万元整"), (0, "零元整"), (1701.64, "壹仟柒佰零壹元陆角肆分")])
def test_upper(v, s):
    assert M.upper(v) == s and s in M.accepted(v) and float(M.parse(s)) == pytest.approx(v)


def test_both_accepted_forms_at_a_zero_boundary():
    assert {"壹佰万伍仟元整", "壹佰万零伍仟元整"} <= M.accepted(1005000)
    assert "壹拾柒万零壹仟捌佰陆拾陆元整" not in M.accepted(171866)


def test_snippet_matches_upper():
    ns = {}
    exec(M.SNIPPET, ns)
    import random
    rnd = random.Random(7)
    for _ in range(3000):
        v = round(rnd.choice([rnd.uniform(0, 100), rnd.uniform(0, 1e6), rnd.uniform(0, 1e9)]), 2)
        assert ns["rmb_upper"](v) == M.upper(v)


def test_check_text_finds_the_task_271_mistake():
    bad = M.check_text("报价 171,866.00 元（大写：壹拾柒万零壹仟捌佰陆拾陆元整，含税）")
    assert bad and bad[0]["expected"] == "壹拾柒万壹仟捌佰陆拾陆元整"
    assert M.check_text("报价 171,866.00 元（大写：壹拾柒万壹仟捌佰陆拾陆元整，含税）") == []
    wrong = M.check_text("报价 171,866.00 元（大写：壹拾柒万壹仟玖佰元整，含税）")
    assert wrong and wrong[0]["problem"].startswith("文档里没有")
    assert M.check_text("注册资本伍拾万元整，成立于2019年") == []


def test_check_file_reads_a_docx(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("含税总价 171,866.00 元（大写：壹拾柒万零壹仟捌佰陆拾陆元整）")
    p = tmp_path / "a.docx"
    d.save(p)
    issues = M.check_file(p.read_bytes(), "a.docx")
    assert issues and "壹拾柒万壹仟捌佰陆拾陆元整" in M.refusal("a.docx", issues)


def test_loop_refuses_send_file_with_a_wrong_amount_twice_then_lets_it_through(tmp_path):
    docx = pytest.importorskip("docx")
    import base64
    from agent_runtime.loop import AgentLoop
    d = docx.Document()
    d.add_paragraph("含税总价 171,866.00 元（大写：壹拾柒万零壹仟捌佰陆拾陆元整）")
    p = tmp_path / "a.docx"
    d.save(p)
    L = types.SimpleNamespace(step=3, history=[])
    res = {"path": "/x/out/a.docx", "content_b64": base64.b64encode(p.read_bytes()).decode()}
    f = AgentLoop._money_check
    assert f(L, res) and f(L, res) and f(L, res) is None
    assert "WARNING" in L.history[-1]


# ---------------------------------------------------------------- document acceptance
def test_document_deliverable_is_accepted_by_reading_its_text():
    df = DevFlow("dev_fix")
    df.contract = {"deliverables": ["~/workspace/tasks/t/out/x.pdf"], "source_of_truth": "~/workspace/tasks/t/price.py"}
    df.source_writes["~/workspace/tasks/t/price.py"] = 5
    df.last_source_step = 5
    df.post_tool(6, "shell", {"command": "cd ~/workspace/tasks/t/out && python3 -c 'import pymupdf; print(1)'"}, True, "1")
    assert not [p for p in df.problems(True, []) if p[0] == "no_acceptance"]


def test_document_deliverable_without_a_check_names_the_document_check():
    df = DevFlow("dev_fix")
    df.contract = {"deliverables": ["~/workspace/tasks/t/out/x.docx"]}
    df.source_writes["s.py"] = 5
    df.last_source_step = 5
    p = [x for x in df.problems(True, []) if x[0] == "no_acceptance"]
    assert p and "verify_html is for web pages" in p[0][2]


# ---------------------------------------------------------------- routine -> chat, weather
FC = {"current": {"time": "2026-11-17T07:30", "temperature_2m": 24.2, "apparent_temperature": 27.9, "relative_humidity_2m": 88,
                  "weather_code": 2, "cloud_cover": 60, "pressure_msl": 1012.6, "wind_speed_10m": 14.0,
                  "wind_direction_10m": 120, "wind_gusts_10m": 30.0, "visibility": 15100},
      "daily": {"time": ["2026-11-17", "2026-11-18"], "weather_code": [61, 3], "temperature_2m_max": [29.6, 28],
                "temperature_2m_min": [21.4, 21], "apparent_temperature_max": [33.1, 31], "apparent_temperature_min": [24.0, 23],
                "precipitation_sum": [3.2, 0], "precipitation_hours": [4, 0], "precipitation_probability_max": [70, 10],
                "wind_speed_10m_max": [22, 15], "wind_gusts_10m_max": [45, 30], "wind_direction_10m_dominant": [100, 90],
                "uv_index_max": [7.2, 6], "sunrise": ["2026-11-17T06:52", "x"], "sunset": ["2026-11-17T17:58", "x"]},
      "hourly": {"time": [f"2026-11-17T{h:02d}:00" for h in range(24)],
                 "temperature_2m": [22 + h % 8 for h in range(24)], "weather_code": [61 if 14 <= h <= 17 else 2 for h in range(24)],
                 "precipitation_probability": [70 if 14 <= h <= 17 else 5 for h in range(24)],
                 "precipitation": [0.8 if 14 <= h <= 17 else 0 for h in range(24)], "wind_speed_10m": [10] * 24,
                 "relative_humidity_2m": [85] * 24}}
AQ = {"current": {"us_aqi": 42, "pm2_5": 18.3, "pm10": 30.1}}


def test_weather_render_is_detailed():
    from aiw_api import brief_weather as B
    t = B.render({"name": "海口市", "fc": FC, "aq": AQ}, now=datetime(2026, 11, 17, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    for want in ("今日天气 · 海口市 · 11月17日 周二", "小雨", "21~30°C", "体感 24~33°C", "湿度 88%", "东南风 3级",
                 "概率 70%", "3.2 mm", "集中在 14:00–17:59", "紫外线：7（强）", "日出 06:52", "AQI 42（优）", "分时段",
                 "出门带伞", "注意防晒", "早晚温差"):
        assert want in t, want


def test_brief_routines_get_weather(tmp_path):
    from aiw_api import brief_weather as B
    from aiw_api.store import Store
    st = Store(str(tmp_path / "db.sqlite"))
    assert B.wants_weather(st, "新闻早报", "运行 aiw_news.py")
    assert not B.wants_weather(st, "签到", "京东签到")
    B.save(st, {"routines": "off", "city": "北京"})
    assert not B.wants_weather(st, "新闻早报", "") and B.cfg(st)["city"] == "北京"


async def test_routine_result_lands_in_the_chat_with_weather(tmp_path, monkeypatch):
    from aiw_api import brief_weather as B
    from aiw_api.chat import Chat
    from aiw_api.store import Store
    st = Store(str(tmp_path / "db.sqlite"))
    sessions = types.SimpleNamespace(listeners=[])
    chat = Chat(st, sessions)

    async def fake_today(store):
        return "### 今日天气 · 海口市\n**小雨**"
    monkeypatch.setattr(B, "today_text", fake_today)
    thr = st.create_thread("⏰ 新闻早报")["id"]
    r = st.insert("routines", {"name": "新闻早报", "prompt": "运行 ~/ai-workstation/news/aiw_news.py", "schedule": "daily 07:30",
                               "enabled": 1, "thread_id": thr})
    task = types.SimpleNamespace(id="task-r1", thread_id=thr, source="routine", prompt="运行 aiw_news.py", continues=None)
    st.update("routines", r["id"], last_task_id="task-r1")
    await sessions.listeners[0](task, "sess-1", {"type": "task.end", "status": "succeeded",
                                                 "result": "**今日要闻**\n1. 第一条\n2. 第二条", "files": ["/a/news.html"]})
    msgs = chat.history(limit=10)["messages"]
    m = msgs[-1]
    assert m["kind"] == "task_result" and m["data"]["routine"] == "新闻早报"
    assert m["text"].startswith("⏰ **新闻早报**") and "今日天气" in m["text"] and "2. 第二条" in m["text"]
    assert m["text"].index("今日天气") < m["text"].index("今日要闻")
    assert m["data"]["files"] and m["data"]["status"] == "succeeded"


async def test_routine_to_chat_can_be_turned_off(tmp_path, monkeypatch):
    from aiw_api.chat import Chat
    from aiw_api.store import Store
    monkeypatch.setenv("AIW_ROUTINE_TO_CHAT", "0")
    st = Store(str(tmp_path / "db.sqlite"))
    sessions = types.SimpleNamespace(listeners=[])
    chat = Chat(st, sessions)
    task = types.SimpleNamespace(id="t", thread_id=None, source="routine", prompt="x", continues=None)
    await sessions.listeners[0](task, "s", {"type": "task.end", "status": "succeeded", "result": "x", "files": []})
    assert not chat.history(limit=5)["messages"]
