"""Web: the persistent chat is the main screen (apps/web/home.js), the conversation list became the 任务 page."""
import pathlib
import shutil
import subprocess

import pytest

WEB = pathlib.Path(__file__).resolve().parents[1] / "apps" / "web"


def test_index_wires_the_home_chat_and_the_tasks_page():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for needle in ('id="v-home"', 'id="hmsgs"', 'id="v-tasks"', 'id="newTaskBtn"', 'id="backBtn"', "home.js", "home.css",
                   "go('home')", "go('tasks')", "go('activity')", "go('routines')", "go('computer')", "go('settings')"):
        assert needle in html, needle
    # the old sidebar conversation list is gone from view (kept hidden for app.js)
    assert '<div id="threadList" class="threads" hidden>' in html
    # home.js loads last so it can wrap go()/newThread()
    tag = lambda f: html.index(f'<script src="{f}">')
    assert tag("home.js") > tag("mate.js") > tag("app.js")


def test_home_js_uses_the_chat_api_and_never_shows_steps():
    js = (WEB / "home.js").read_text(encoding="utf-8")
    for needle in ("/api/chat/send", "/api/chat?limit=", "before=", "/to_task", "/answer", "/api/chat/read", "open_task",
                   "HS.mode", "getReader", "replay(", "/retry"):
        assert needle in js, needle
    for banned in ("agent.thinking", "S.think", "steplist"):
        assert banned not in js, banned


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_web_scripts_parse():
    for f in ("home.js", "memory.js", "app.js", "ui.js"):
        r = subprocess.run(["node", "--check", str(WEB / f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_settings_memory_page_is_wired():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="v-memory"' in html and '<script src="memory.js">' in html
    js = (WEB / "memory.js").read_text(encoding="utf-8")
    for needle in ("/api/memories", "?all=true", "/restore", '"PUT"', '"DELETE"', "profile", "decision"):
        assert needle in js, needle
    assert "go('memory')" in (WEB / "ui.js").read_text(encoding="utf-8")


def test_v2_home_stage_picker_sheet_and_computer_button():
    """v2 (2026-10-28): 球球 briefing stage, "/" skills, "+" sheet with camera / video, stop key, the 电脑 button."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for needle in ('id="hstage"', 'id="hstOrb"', 'id="hbbs"', 'id="hskills"', 'id="htokens"', 'id="hsheet"',
                   'accept="image/*" capture="environment"', 'accept="video/*" capture="environment"', 'accept="image/*,video/*"',
                   "拍照", "录视频", "照片/视频", "文件", "新建任务", "链接", "技能 /", 'placeholder="和我聊聊，或输入 / 选技能…"'):
        assert needle in html, needle
    js = (WEB / "home.js").read_text(encoding="utf-8")
    for needle in ("/api/greeting", "HB_MS = 4500", "hStageTyping", "/api/chat/cancel", 'ev === "status"', "/api/uploads/raw",
                   "loadSkillList", "【技能：", "hSkChoose", "AiwMate.create", 'set("30")', "toggleDeskPane = function", 'go("computer")'):
        assert needle in js, needle
    css = (WEB / "home.css").read_text(encoding="utf-8")
    for needle in ("#ffbe4d", "#f0a030", "#2a1a05", "20px 20px 6px 20px", "#15120d", "1.5px solid", "20px 20px 20px 6px", "#ffc35c",
                   "grid-template-columns:repeat(4, 1fr)", "border-left:4px solid"):   # 3.6: no chips, no dashed thinking ring
        assert needle in css, needle
    # the 球球 itself is not restyled: no disc colour / glow overrides in the home stylesheet
    for banned in ("--mate-disc", "--mate-glow"):
        assert banned not in css, banned
    assert "create: (host, opts)" in (WEB / "mate.js").read_text(encoding="utf-8")
