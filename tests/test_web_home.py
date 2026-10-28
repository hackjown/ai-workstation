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
    for f in ("home.js", "app.js", "ui.js"):
        r = subprocess.run(["node", "--check", str(WEB / f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
