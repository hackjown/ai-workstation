"""v3.9.17 items 7 + 8: lessons only for the same tool + error kind; risk-control tokens stop the task at once."""
from aiw_api import lessons as L
from aiw_api.store import Store
from agent_runtime import escalate as E


def test_lesson_needs_same_tool_and_same_error_kind(tmp_path):
    les = L.Lessons(Store(str(tmp_path / "l.db")))
    les.add("tool", "read_image", "读图报 502 网关错误", "换小图再试，连续 5xx 就改用 OCR", "auto")
    les.add("general", "", "shell 里 pip install 超时", "换清华镜像 -i https://pypi.tuna.tsinghua.edu.cn/simple", "auto")
    les.add("general", "", "网页登录后跳回登录页", "清 cookie 后重新登录", "auto")
    h, _ = les.hint("HTTP 502 Bad Gateway", "read_image")
    assert "OCR" in h
    h, _ = les.hint("HTTP 502 Bad Gateway", "shell")          # other tool: nothing
    assert h == ""
    h, _ = les.hint("pip install: Read timed out", "shell")
    assert "镜像" in h
    h, _ = les.hint("pip install: No module named setuptools", "shell")   # same tool, other error kind
    assert "镜像" not in h


def test_error_classes():
    assert L.error_class("Connection refused") == "network"
    assert L.error_class("timed out after 60s") == "timeout"
    assert L.error_class("RGV587_ERROR::SM") == "captcha"


def test_strong_gate_tokens_anywhere_but_not_in_code():
    for t in ['{"ret":["RGV587_ERROR::SM::哎哟喂,被挤爆啦"]}', "location: /_____tmd_____/punish?x5secdata=xx",
              '{"ret":["FAIL_SYS_USER_VALIDATE::请登录"]}', "页面提示：请拖动滑块完成验证", "登录已过期，请重新登录"]:
        assert E.human_gate(t)[0], t
    assert E.human_gate("src/qwen-bx.ts:12:  if (body.includes('RGV587')) return retry()")[0] == ""
    assert E.human_gate("const BAXIA = /baxia|punish/")[0] == ""


def test_stall_blocks_at_once_on_shell_output_and_says_what_to_do():
    s = E.Stall()
    assert s.feed("shell", {"command": "curl ..."}, 'HTTP/1.1 200\n{"ret":["RGV587_ERROR::SM"]}') == "blocked"
    assert "RGV587" in s.blocked
    assert "风控" in E.gate_todo(s.blocked)
    assert "重新登录" in E.gate_todo("登录已过期")
    assert "滑块" in E.gate_todo("拖动滑块")
    s.reset(handled=True)          # the user cleared it: the same wall is not raised again
    assert s.feed("shell", {}, 'RGV587_ERROR') is None
