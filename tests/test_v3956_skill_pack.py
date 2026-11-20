"""v3.9.56: skills of one pack (front matter owner: system-pack/istoreos, as linkease/kai-skills installs them) are
shown as one pack, are not self-synced, and are removed together; agy's own summary reaches the task result."""
from __future__ import annotations

from pathlib import Path

from aiw_api import skillupdate as su
from agent_runtime import antigravity as agy
from agent_runtime import skills

from test_v3951_skill_update import Fleet, item, write_skill


def write_pack_skill(d: Path, owner="system-pack/istoreos"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {d.name}\ndescription: pack skill\nowner: {owner}\nsystems: istoreos\n---\nx\n",
                                encoding="utf-8")


async def test_pack_is_one_entry_not_synced_and_adopts_old_self_records(tmp_path):
    f = Fleet(tmp_path)
    for sid in ("istoreos-app-search", "istoreos-package-manager", "istoreos-backup-restore"):
        write_pack_skill(f.skill("a", sid))
    write_pack_skill(f.skill("b", "istoreos-app-search"))
    write_skill(f.skill("a", "my-report"))
    # what v3.9.51-55 left behind: the pack's skills recorded as self-built
    f.kv["skills_selfsync"] = {"istoreos-app-search": {"hash": "x", "at": 1}, "my-report": {"hash": "y", "at": 1}}
    f.kv["skills_custom"] = [{"id": "istoreos-app-search", "title": "istoreos-app-search", "self": True}]
    up = f.updater()
    res = await up.check()
    it = item(res, "istoreos-package-manager")
    assert it["kind"] == "pack" and it["pack"] == "system-pack/istoreos" and it["state"] == "ok"
    pk = res["packs"]["system-pack/istoreos"]
    assert pk["title"] == "iStoreOS 技能包" and pk["source"] == "linkease/kai-skills"
    assert sorted(pk["ids"]) == ["istoreos-app-search", "istoreos-backup-restore", "istoreos-package-manager"]
    assert pk["state"] == "partial" and pk["machines"] == {"a": 3, "b": 1}
    assert "istoreos-app-search" not in f.kv["skills_selfsync"] and "my-report" in f.kv["skills_selfsync"]
    c = f.kv["skills_custom"][0]
    assert c["pack"] == "system-pack/istoreos" and "self" not in c
    job = await up.update()
    assert not any(k.startswith("istoreos") for k in job["items"])     # nothing of the pack is copied around
    assert not f.skill("c", "istoreos-package-manager").exists()
    assert (f.skill("c", "my-report") / "SKILL.md").exists()            # self-built skills still sync
    lst = skills.listing(f.kv["skills_custom"])
    assert next(s for s in lst if s["id"] == "istoreos-app-search")["pack"] == "system-pack/istoreos"


async def test_remove_pack_moves_every_member_to_backup(tmp_path):
    f = Fleet(tmp_path)
    for m in "ab":
        for sid in ("istoreos-app-search", "istoreos-luci-recovery"):
            write_pack_skill(f.skill(m, sid))
    write_pack_skill(f.skill("a", "other-x"), owner="someone/else")
    write_skill(f.skill("a", "my-report"))
    f.kv["skills_custom"] = [{"id": "istoreos-app-search", "pack": "system-pack/istoreos"}, {"id": "my-report"}]
    f.offline = {"c"}
    up = f.updater()
    r = await up.remove_pack("system-pack/istoreos")
    assert r["removed"] == ["istoreos-app-search", "istoreos-luci-recovery"] and r["offline"] == ["C"]
    for m in "ab":
        assert not f.skill(m, "istoreos-app-search").exists()
        backups = list((f.home(m) / ".claude" / ".aiw-skill-backup").glob("pack-*/istoreos-luci-recovery/SKILL.md"))
        assert backups
    assert f.skill("a", "other-x").exists() and f.skill("a", "my-report").exists()
    assert [c["id"] for c in f.kv["skills_custom"]] == ["my-report"]
    try:
        await up.remove_pack("not a pack")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_agy_report_is_appended_to_the_result():
    rep = {"job": "agy-1", "branch": "aiw/agy-1", "text": "Rebuilt the login page; tests pass.", "changes": "3 files changed"}
    out = agy.with_report("改好了，分支 aiw/agy-1 可以合并。", rep)
    assert out.startswith("改好了") and "Antigravity 的总结" in out and "Rebuilt the login page" in out
    assert agy.with_report(out, rep) == out                     # never twice
    assert agy.with_report("x", None) == "x"
    long = agy.with_report("x", {**rep, "text": "a" * 5000})
    assert "~/.config/aiw/agy/jobs/agy-1/" in long and len(long) < 3400


def test_agy_report_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AIW_AGY_SHOW_REPORT", "0")
    assert agy.with_report("x", {"job": "j", "text": "y"}) == "x"


def test_ui_groups_packs():
    ui = (Path(__file__).resolve().parents[1] / "apps/web/ui.js").read_text(encoding="utf-8")
    assert "function skillGroups" in ui and "删除整包" in ui and "/api/skills/pack?owner=" in ui
    assert "_agy_report" in (Path(__file__).resolve().parents[1] /
                             "services/agent-runtime/agent_runtime/loop.py").read_text(encoding="utf-8")


async def test_quick_check_replaces_a_cached_check_from_before_packs(tmp_path):
    f = Fleet(tmp_path)
    write_pack_skill(f.skill("a", "istoreos-app-search"))
    f.kv["skills_updates"] = {"checked_at": 1, "items": [], "counts": {}}   # what v3.9.55 kept
    await f.updater().check(github=False)
    assert "system-pack/istoreos" in f.kv["skills_updates"]["packs"]


def test_slash_picker_and_endpoints_know_packs():
    root = Path(__file__).resolve().parents[1]
    home = (root / "apps/web/home.js").read_text(encoding="utf-8")
    assert "skillGroups(list)" in home and 'startsWith("pack:")' in home
    main = (root / "services/api/aiw_api/main.py").read_text(encoding="utf-8")
    assert '"packs" not in cur' in main
