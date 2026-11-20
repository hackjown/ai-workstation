"""v3.9.58 引擎: attachments and skills from the chat composer reach the engine turn."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
sys.path.insert(0, str(ROOT / "services" / "api"))
from aiw_api import bots as B  # noqa: E402
from aiw_api.store import Store  # noqa: E402


class Comp:
    def __init__(self):
        self.calls = []

    async def execute(self, kind, payload, **kw):
        self.calls.append((kind, payload))
        return type("R", (), {"success": True, "result": {"size": 3}, "error": None})()


class Sess:
    def __init__(self):
        self.comp = Comp()

    def computer(self, mid):
        return self.comp


def make(tmp_path):
    b = B.Bots(Store(str(tmp_path / "t.db")), sessions=Sess(), default_machine=lambda: "vm1",
               shell=lambda *a: None)
    seen = []
    b._kick = lambda cid: seen.append(list(b.queues.get(cid) or []))
    return b, seen


def test_vm_rel_dir():
    assert B.vm_rel_dir("~/workspace/agy-chat/bc-1") == ("agy-chat/bc-1/uploads", "~/workspace/agy-chat/bc-1/uploads")
    assert B.vm_rel_dir("/opt/x") == ("uploads", "~/workspace/uploads")
    assert B.vm_rel_dir("~/workspace/../etc") == ("uploads", "~/workspace/uploads")


def test_skills_and_files_go_into_the_prompt(tmp_path):
    b, seen = make(tmp_path)
    c = b.create("agy", "t", "~/workspace/agy-chat/bc-1")
    att = [{"name": "a.png", "vm_path": "~/workspace/agy-chat/bc-1/uploads/a.png", "artifact": "/x/a.png"}]
    m = b.send(c["id"], "改一下", attachments=att, skills=["officecli"])
    assert m["data"]["skills"] == ["officecli"] and m["data"]["attachments"][0]["name"] == "a.png"
    p = seen[-1][-1]["prompt"]
    assert "officecli" in p and "SKILL.md" in p
    assert "~/workspace/agy-chat/bc-1/uploads/a.png" in p and p.rstrip().endswith("改一下")


def test_attachment_only_message(tmp_path):
    b, seen = make(tmp_path)
    c = b.create("agy", "t", "")
    b.send(c["id"], "", attachments=[{"name": "f.pdf", "vm_path": "~/workspace/uploads/f.pdf"}])
    assert "f.pdf" in seen[-1][-1]["prompt"]


def test_push_files_copies_into_the_session_folder(tmp_path):
    b, _ = make(tmp_path)
    c = b.create("agy", "t", "~/workspace/agy-chat/bc-9")
    f = tmp_path / "up.txt"
    f.write_text("hey")
    out = asyncio.run(b.push_files(c["id"], [{"name": "up.txt", "artifact": str(f), "up": False}],
                                   allowed=lambda p: p if p == str(f) else None))
    assert out[0]["vm_path"] == "~/workspace/agy-chat/bc-9/uploads/up.txt" and "up" not in out[0]
    kind, payload = b.sessions.comp.calls[0]
    assert kind == "files.upload" and payload["path"] == "agy-chat/bc-9/uploads/up.txt"
    out = asyncio.run(b.push_files(c["id"], [{"name": "nope", "artifact": "/etc/passwd"}], allowed=lambda p: None))
    assert "vm_path" not in out[0]


async def test_a_picked_skill_is_copied_into_the_session_folder(tmp_path):
    """The real bridge (fake agy): the skill's folder lands in <workdir>/.aiw-context/ and the prompt points there."""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_v3957_bots import make as make_real, settle, stop_all
    b, home = make_real(tmp_path)
    sk = home / ".claude" / "skills" / "demo-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("# demo\nsay hi")
    b.store.kv_set("skills_custom", [{"id": "demo-skill", "title": "演示", "desc": "d", "path": "~/.claude/skills/demo-skill"}])
    try:
        c = b.create("agy", "s", str(tmp_path / "proj"))
        b.send(c["id"], "write hello", skills=["demo-skill"])
        await settle(b, c["id"])
        copied = list((tmp_path / "proj" / ".aiw-context").rglob("SKILL.md"))
        assert copied and copied[0].read_text().startswith("# demo")
    finally:
        stop_all(home)


def test_model_flags():
    assert B.model_flags({"model": "gemini-4-neon-high", "effort": "low"}) == ("gemini-4-neon-high", "")
    assert B.model_flags({"model": "gemini-3.8-flash", "effort": "HIGH"}) == ("gemini-3.8-flash", "high")
    assert B.model_flags({"model": "", "effort": "bogus"}) == ("", "")


def test_group_models():
    from agent_runtime import agy_bridge as AB
    gs = AB.group_models(AB.parse_models('{"type":"models","data":{"models":[{"id":"gemini-4-neon-high","label":"Gemini 4 Neon (High)"},'
                                         '{"id":"gemini-4-neon-low","label":"Gemini 4 Neon (Low)"},{"id":"gemini-3.8-flash","label":"Gemini 3.8 Flash"}]}}'))
    assert gs[0] == {"id": "gemini-4-neon", "label": "Gemini 4 Neon", "efforts": ["low", "high"],
                     "slugs": {"high": "gemini-4-neon-high", "low": "gemini-4-neon-low"}}
    assert gs[1]["id"] == "gemini-3.8-flash" and gs[1]["efforts"] == [] and gs[1]["plain"]
    txt = AB.parse_models("gemini-4-argon-medium   Gemini 4 Argon (Medium)\nClaude Opus 4.6 (Thinking)")
    assert txt[0]["id"] == "gemini-4-argon-medium" and txt[1]["id"] == "Claude Opus 4.6 (Thinking)"


async def test_models_and_switching_restarts_with_the_same_session(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tests"))
    from test_v3957_bots import make as make_real, settle, stop_all
    args = tmp_path / "args.log"
    monkeypatch.setenv("FAKE_AGY_ARGS", str(args))
    b, home = make_real(tmp_path)
    try:
        r = await b.models("agy", "vm1")
        neon = [g for g in r["groups"] if g["id"] == "gemini-4-neon"][0]
        assert neon["efforts"] == ["low", "high"]
        c = b.create("agy", "m", str(tmp_path / "proj"), model="gemini-4-neon-low")
        b.send(c["id"], "write hello")
        await settle(b, c["id"])
        first = args.read_text().splitlines()[-1]
        assert "--model gemini-4-neon-low" in first and "--effort" not in first and "--conversation" not in first
        sess = b.conv(c["id"])["session"]
        assert sess
        b.set_model(c["id"], "gemini-3.8-flash", "high")
        b.send(c["id"], "again")
        await settle(b, c["id"])
        last = args.read_text().splitlines()[-1]
        assert "--model gemini-3.8-flash" in last and "--effort high" in last and f"--conversation {sess}" in last
        assert any("模型改成 gemini-3.8-flash" in m["text"] for m in b.messages(c["id"]) if m["role"] == "system")
        assert b.conv(c["id"])["job_model"] == "gemini-3.8-flash|high"
    finally:
        stop_all(home)
