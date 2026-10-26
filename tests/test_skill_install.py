"""设置 › 技能: one line installs a skill on every computer and registers it."""
import asyncio

import pytest
from aiw_api import skillsync as K


def test_parse_source():
    assert K.parse_source("npx skills add openfilm/openfilm") == "openfilm/openfilm"
    assert K.parse_source("npx -y skills add https://github.com/a/b -g -a claude-code") == "https://github.com/a/b"
    assert K.parse_source(" openfilm/openfilm ") == "openfilm/openfilm"
    with pytest.raises(ValueError):
        K.parse_source("a; rm -rf ~")


def test_install_all_registers_per_machine():
    kv = {"skills_custom": [{"id": "old", "path": "~/x"}]}
    out = {"m1": "…\nAIW_SKILL openfilm|Make videos\nAIW_RC 0\n", "m2": "AIW_ERR 这台电脑没有 Node/npx\n"}

    async def shell(mid, cmd, t):
        assert "npx -y skills add openfilm/openfilm -g -a claude-code" in cmd
        return {"stdout": out[mid]}
    woke = []

    async def wake(mid):
        woke.append(mid)
    job = asyncio.run(K.install_all("openfilm/openfilm", [{"id": "m1"}, {"id": "m2", "name": "云"}], shell, wake,
                                    lambda k, d=None: kv.get(k, d), kv.__setitem__))
    assert job["done"] and job["machines"]["m1"]["state"] == "ok" and job["machines"]["m2"]["state"] == "failed"
    assert "npx" in job["machines"]["m2"]["error"] and woke == ["m1", "m2"]
    s = next(x for x in kv["skills_custom"] if x["id"] == "openfilm")
    assert s["path"] == "~/.claude/skills/openfilm" and s["source"] == "openfilm/openfilm" and s["machines"] == ["m1"]
    assert s["desc"] == "Make videos"


def test_brief_installs_missing_skill_from_source():
    from agent_runtime import skills
    b = skills.brief("【技能：openfilm】做个视频", [{"id": "openfilm", "path": "~/.claude/skills/openfilm", "source": "openfilm/openfilm"}])
    assert "~/.claude/skills/openfilm/SKILL.md" in b and "npx -y skills add openfilm/openfilm" in b


def test_dev_skill_with_design_gets_mockups_first_and_a_long_budget():
    from agent_runtime import skills
    from aiw_api.sessions import _budget
    p = "【技能：dev、huashu-design】做一个记账网页"
    b = skills.brief(p)
    assert "do NOT write app code yet" in b and "ask_user" in b and "huashu-design" in b and "SKILL.md" in b
    assert _budget(p, 40, 900) == (300, 5400)
    assert "~/.local/bin/officecli" in skills.brief("【技能：officecli】去掉空白页")


def test_skill_named_in_words_gets_its_brief_and_office_budget():
    from agent_runtime import skills
    from aiw_api.sessions import _budget
    b = skills.brief("你用officecli去修改，去掉空白页面")
    assert "~/.local/bin/officecli" in b and "overflow" in b
    assert _budget("你用officecli去修改，去掉空白页面", 40, 900) == (80, 1800.0)
    assert skills.mentioned("develop a page") == []
