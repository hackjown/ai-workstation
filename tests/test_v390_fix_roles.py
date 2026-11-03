"""3.9.0: task-c987469146 fixes (false JD captcha, vision on demand, continuations, installed skills, export redaction)
and computer roles (each cloud computer has a job, new tasks go to the one that fits, the local VM stays out)."""
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from agent_runtime import escalate as es
from agent_runtime import skills as sk
from aiw_api import roles as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOP = open(os.path.join(ROOT, "services/agent-runtime/agent_runtime/loop.py"), encoding="utf-8").read()
CONT = ("安装https://github.com/linkease/kai-skills这个skill\n\n【接着上次继续做】用户补充：首先备份目前在使用的singbox配置文件，"
        "然后把之前iPad分流的加入到配置文件并重启singbox测试")


# ------------------------------------------------------------------ captcha
def test_shell_and_file_output_never_raise_captcha():
    s = es.Stall()
    out = (s.feed("shell", {"command": "ls"}, "geosite-recaptcha.srs  rule-set recaptcha"),
           s.feed("files_write", {"path": "a.py"}, ""),
           s.feed("shell", {"command": "python3 a.py"}, "recaptcha"))
    assert out == (None, None, None) and not s.blocked


def test_negated_captcha_mention_is_not_a_wall():
    assert es.blocked_hint("当前无弹窗、错误提示或验证码") == ""
    assert es.blocked_hint("请完成安全验证，拖动滑块") != ""


def test_real_captcha_once_per_page():
    s = es.Stall()
    assert s.feed("bos_snapshot", {}, "请拖动滑块完成安全验证") is None
    assert s.feed("bos_snapshot", {}, "请拖动滑块完成安全验证") == "blocked"
    s.reset(handled=True)
    assert s.feed("bos_snapshot", {}, "请拖动滑块完成安全验证") is None
    assert s.feed("bos_snapshot", {}, "请拖动滑块完成安全验证") is None
    s.feed("bos_navigate", {"url": "https://x"}, "")
    s.feed("bos_snapshot", {}, "请拖动滑块完成安全验证")
    assert s.feed("bos_snapshot", {}, "请拖动滑块完成安全验证") == "blocked"


# ------------------------------------------------------------------ vision on demand
def _gui_ns():
    ns = {"re": re}
    exec(re.search(r"(_GUI_TASK = re\.compile\(.*?re\.I\)\n)", LOOP, re.S).group(1), ns)
    exec(re.search(r"(_GIT_URL = .*?\n\n\ndef _gui_text.*?return _GIT_URL\.sub\(\" \", p\)\n)", LOOP, re.S).group(1), ns)
    return ns


def test_shell_task_with_github_url_needs_no_desktop():
    ns = _gui_ns()
    assert not ns["_GUI_TASK"].search(ns["_gui_text"](CONT))
    assert ns["_GUI_TASK"].search(ns["_gui_text"]("打开京东看看价格"))
    assert "AIW_VISION_ON_DEMAND" in LOOP and "self._gui_used = True" in LOOP


# ------------------------------------------------------------------ continuations and skills
def test_git_skill_reused_not_reinstalled_and_plan_not_autoticked():
    b = sk.brief(CONT)
    assert "kai-skills" in b and "do NOT clone or install it again" in b
    assert "AIW_PLAN_AUTOTICK" in LOOP


def test_installed_skills_are_read_from_the_computer(tmp_path):
    d = tmp_path / ".claude/skills/kai/istoreos"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text('---\nname: istoreos-ssh\ndescription: "iStoreOS over SSH: sing-box config"\n---\n')
    out = subprocess.run(["bash", "-c", sk.INSTALLED_PROBE], capture_output=True, text=True,
                         env={**os.environ, "HOME": str(tmp_path)}).stdout
    items = sk.parse_installed(out)
    assert items and items[0]["name"] == "istoreos-ssh"
    b = sk.installed_brief(items, "改一下 sing-box 配置")
    assert "never clone or install them again" in b and "istoreos-ssh" in b
    assert sk.installed_brief([], "x") == ""


def test_export_masks_passwords():
    from aiw_api.main import _redact
    r = _redact({"a": ['ssh.connect("192.168.50.1", username="root", password="Abc123!x")', "sshpass -p 'p@ss' ssh root@h",
                       "git clone https://u:tok123@github.com/x"], "password": "zz", "n": 1})
    text = str(r)
    assert "Abc123!x" not in text and "p@ss" not in text and "tok123" not in text and "zz" not in text
    assert r["n"] == 1 and "192.168.50.1" in text


# ------------------------------------------------------------------ roles
class _St:
    def __init__(self):
        self.kv = {"cloud_vms": {c: {"status": "ready"} for c in ("cloud-a", "cloud-b", "cloud-c")}}
        self.ms = [{"id": "ai-linux-001", "name": "本地VM"}, {"id": "cloud-a", "name": "云A"},
                   {"id": "cloud-b", "name": "云B"}, {"id": "cloud-c", "name": "云C"}]

    def kv_get(self, k, d=None):
        return self.kv.get(k, d)

    def kv_set(self, k, v):
        self.kv[k] = v

    def machines(self):
        return self.ms

    def machine(self, mid):
        return next((m for m in self.ms if m["id"] == mid), None)

    def tasks_by_status(self, s):
        return []


def _s(busy=()):
    st = _St()
    return SimpleNamespace(store=st, cloud=SimpleNamespace(entries=lambda: st.kv["cloud_vms"]),
                           busy_session=lambda mid: (mid in busy) or None)


L = "ai-linux-001"


def test_default_roles_and_routing():
    s = _s()
    assert R.roles_of(s) == {"cloud-a": "design", "cloud-b": "media", "cloud-c": "office"}
    assert R.pick(s, "照着这张照片画施工图，导出 dxf", L) == "cloud-a"
    assert R.pick(s, "把这个mp4剪成30秒", L) == "cloud-b"
    assert R.pick(s, "把这个 excel 表格整理成周报 word", L) == "cloud-c"
    assert R.pick(s, "帮我想一下这个问题", L) != L
    assert R.pick(s, "备份 192.168.50.1 上的 singbox 配置", L) == L
    assert R.pick(s, "用 云B 帮我整理 excel", L) == "cloud-b"
    assert R.label(s, "cloud-a") == "云A · 设计制图" and R.label(s, L) == "本地VM"


def test_busy_role_machine_and_strict():
    s = _s(busy={"cloud-a"})
    assert R.pick(s, "画户型图", L) in ("cloud-b", "cloud-c")
    s.store.kv["machine_roles"] = {"strict": True}
    assert R.pick(s, "画户型图", L) == "cloud-a"


def test_dispatch_sees_roles_and_skips_local():
    s = _s()
    ro = R.annotate(s, [{"id": L, "available": True}, {"id": "cloud-a", "available": True}])
    assert ro[0]["available"] is False and ro[1]["role"] == "design"
    it = R.place_auto(s, [{"machine": "auto", "goal": "剪一个 30 秒视频"}, {"machine": "auto", "goal": "做一份 PPT"}])
    assert [i["machine"] for i in it] == ["cloud-b", "cloud-c"]


def test_roles_off_changes_nothing():
    s = _s()
    s.store.kv["machine_roles"] = {"enabled": False}
    assert R.pick(s, "画户型图", L) == L


def test_clients_follow_the_tasks_computer():
    web = open(os.path.join(ROOT, "apps/web/app.js"), encoding="utf-8").read()
    ios = open(os.path.join(ROOT, "apps/ios/Sources/ChatView.swift"), encoding="utf-8").read()
    pane = open(os.path.join(ROOT, "apps/ios/Sources/DesktopView.swift"), encoding="utf-8").read()
    assert "followTaskMachine()" in web
    assert "follow: taskMachine" in ios and "var follow: String? = nil" in pane


def test_picked_skill_decides_the_role():
    s = _s()
    assert R.best_role("【技能：huashu-motion】把这份文档做成讲解") == "media"
    assert R.pick(s, "【技能：officecli】把这张图片插到文档里", L) == "cloud-c"
    assert R.pick(s, "【技能：cad-drawing】整理一下", L) == "cloud-a"


def test_skill_preflight_list():
    items = {i["id"]: i for i in sk.preflight("【技能：officecli、huashu-motion、dev】做个视频")}
    assert "dev" not in items   # nothing to check
    assert items["officecli"]["check"].startswith("test -x ~/.local/bin/officecli")
    assert items["huashu-motion"]["check"] == "test -f ~/.claude/skills/huashu-motion/SKILL.md"
    assert items["huashu-motion"]["setup"].endswith("motion-setup.sh")


async def test_preflight_installs_a_missing_skill_once():
    from aiw_api.sessions import SessionManager
    state = {"installed": False, "setups": 0}

    class Comp:
        async def execute(self, kind, args, observe=False, timeout=0):
            cmd = args["command"]
            if "motion-setup.sh" in cmd:
                state["setups"] += 1
                state["installed"] = True
                ok = True
            elif "huashu-motion/SKILL.md" in cmd:
                ok = state["installed"]
            else:
                ok = True
            return SimpleNamespace(success=ok, result={"exit_code": 0 if ok else 1, "stdout": "", "stderr": ""})

    msgs = []

    async def emit(ev):
        msgs.append(ev.data.get("text"))

    me = SimpleNamespace(store=SimpleNamespace(kv_get=lambda k, d=None: d), forgot=[])
    me.forget_installed_skills = lambda mid=None: me.forgot.append(mid)
    task = SimpleNamespace(prompt="【技能：huashu-motion】做个讲解视频", machine_id="cloud-b")
    note = await SessionManager._skill_preflight(me, task, Comp(), emit, "s1")
    assert state["setups"] == 1 and "just installed" in note and me.forgot == ["cloud-b"]
    assert any("先安装" in (m or "") for m in msgs)
    note2 = await SessionManager._skill_preflight(me, task, Comp(), emit, "s1")
    assert state["setups"] == 1 and "huashu-motion: installed" in note2


def test_bark_sound_per_push_kind(monkeypatch):
    from aiw_api import notify as N
    monkeypatch.delenv("AIW_BARK_SOUND", raising=False)
    assert N.sound_for("confirm") == "aiw-shouquan"
    assert N.sound_for("input", "需要你填写资料") == "aiw-tianxie"
    assert N.sound_for("input", "需要你接管桌面", "takeover/m1?task=t") == "aiw-jieguan"
    assert N.sound_for("input", "任务卡住了，等你处理") == "aiw-kazhu"
    assert N.sound_for("input", "Agent 在问你") == "aiw-huida"
    assert N.sound_for("done") == "aiw-wancheng" and N.sound_for("failed") == "aiw-shibai"
    assert N.sound_for("watch") == "aiw-jiangjia" and N.sound_for("xhs_login") == "aiw-xiaohongshu"
    assert N.sound_for("test") == "aiw-tixing"
    _, p = N.bark_request("https://api.day.app/KEY", "需要你确认", "b", "confirm", "task/t1", {"bark_sound": "voice"})
    assert p["sound"] == "aiw-shouquan"
    _, p = N.bark_request("https://api.day.app/KEY", "t", "b", "done", "task/t1", {"bark_sound": "off"})
    assert "sound" not in p
    root = Path(__file__).resolve().parents[1] / "apps" / "web" / "sounds"
    for name, _t in N.SOUNDS.values():
        assert (root / f"{name}.caf").stat().st_size > 10000


def test_ios_task_composer_is_isolated():
    src = (Path(__file__).resolve().parents[1] / "apps" / "ios" / "Sources" / "ChatView.swift").read_text(encoding="utf-8")
    assert "struct TaskComposerInput: View" in src and "@StateObject private var box = ComposerBox()" in src
    assert "@State private var text = \"\"\n    @State private var attach" not in src
    pane = (Path(__file__).resolve().parents[1] / "apps" / "ios" / "Sources" / "DesktopView.swift").read_text(encoding="utf-8")
    assert "Task.detached(priority: .utility) { aiwDecodeScreenshot(" in pane
