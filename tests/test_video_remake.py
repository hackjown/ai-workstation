"""复刻视频 (video-remake) built-in skill: registration, auto-pick, and the prompt validator / JSON converter."""
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_runtime import skills

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "skills" / "video-remake"
_spec = importlib.util.spec_from_file_location("remake", SK / "remake.py")
remake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(remake)
EXAMPLE = (SK / "example-麻将局.md").read_text(encoding="utf-8")


def _final(text=EXAMPLE):
    return remake.final_region(text)[0]


def test_registered_and_listed():
    sk = next(s for s in skills.CATALOG if s["id"] == "video-remake")
    assert sk["title"] == "复刻视频" and sk["path"] == "~/.claude/skills/video-remake"
    assert "video-remake-setup.sh" in sk["setup"]
    assert any(s["id"] == "video-remake" and s["builtin"] for s in skills.listing([]))
    b = skills.brief("【技能：video-remake】 这个视频")
    assert "~/.claude/skills/video-remake/SKILL.md" in b and "remake.py check" in b and "@图片" in b


@pytest.mark.parametrize("prompt", [
    "复刻这个视频", "这条短片复刻一下", "帮我仿拍一下\n\n附件（已放到电脑上）：\n- ~/workspace/uploads/video-1.mp4",
    "继续分析这个视频，给我最终视屏提示词\n\n附件（已放到电脑上）：\n- ~/workspace/uploads/video-1793071858.mp4",
    "拉片分析这个视频，出提示词", "Remake this video for me"])
def test_auto_picked(prompt):
    assert "video-remake" in skills.auto(prompt)
    assert "video-remake" in skills.brief(prompt)


@pytest.mark.parametrize("prompt", ["帮我写一段视频提示词：一只猫在跑", "把PPT做成讲解视频", "复刻一个登录页面", "剪一下这个视频"])
def test_not_auto_picked(prompt):
    assert "video-remake" not in skills.auto(prompt)


def test_api_lists_it(tmp_path):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    with TestClient(build_app(s)) as cl:
        r = cl.get("/api/skills", headers={"Authorization": "Bearer t"})
        sk = next(x for x in r.json() if x["id"] == "video-remake")
        assert sk["title"] == "复刻视频" and sk["builtin"] is True


def test_skill_files_shipped_by_setup():
    names = {p.name for p in SK.iterdir() if p.is_file() and not p.name.startswith(".")}
    assert {"SKILL.md", "template.md", "checklist.md", "remake.py", "example-麻将局.md"} <= names
    script = (ROOT / "deploy" / "vm" / "video-remake-setup.sh").read_text(encoding="utf-8")
    from urllib.parse import quote
    for n in names:
        assert (n if n.isascii() else quote(n)) in script, n


def test_example_passes():
    r = remake.check_text(EXAMPLE, 15)
    assert r["ok"], r["errors"]
    assert r["deliverable"] and r["shots"] == 15 and r["total"] == 15
    assert r["slots"] == [f"@图片{i}" for i in range(1, 6)]
    assert not r["warnings"], r["warnings"]


def test_parse_slots_and_shots():
    p = remake.parse_prompt(_final())
    types = {s["slot"]: s["type"] for s in p["slots"]}
    assert types == {"@图片1": "character", "@图片2": "character", "@图片3": "character", "@图片4": "group",
                     "@图片5": "scene"}
    assert p["shots"][0]["start"] == 0 and p["shots"][-1]["end"] == 15
    assert p["shots"][6]["title"].startswith("切：手部特写") and p["shots"][6]["slots"] == ["@图片5"]
    assert remake.duration_hint(p) == 15


def _broken(old, new, text=EXAMPLE):
    assert old in text, old
    return remake.check_text(text.replace(old, new, 1), 15)


def test_unbound_and_unused_slots():
    r = _broken("背景@图片4站立观局", "背景@图片6站立观局")
    assert not r["ok"] and any("@图片6 被引用但没有" in e for e in r["errors"])
    fin = _final()
    body = fin.split("逐秒提示词", 1)[1].replace("@图片4", "背景人物")
    r = remake.check_text(fin.split("逐秒提示词", 1)[0] + "逐秒提示词" + body, 15)
    assert any("@图片4（背景4女）已绑定，但没有任何一个镜头引用它" in e for e in r["errors"])


def test_timing_errors():
    r = _broken("第 5 秒（0:04–0:05）", "第 5 秒（0:04–0:06）")
    assert any("不衔接" in e for e in r["errors"]) and any("秒号与时间码不一致" in e for e in r["errors"])
    r = remake.check_text(EXAMPLE, 16)
    assert any("与视频时长 0:16 不一致" in e for e in r["errors"])
    r = _broken("第 3 秒（0:02–0:03）", "第 3 秒（0:02.5–0:03）")
    assert not r["ok"]


def test_naming_and_vague_words():
    r = _broken("中央@图片2微笑望向右侧", "中央女A微笑望向右侧")
    assert any("别名「女A」" in e for e in r["errors"])
    r = _broken("场景道具：深色麻将桌，麻将牌，骰子，酒杯，毛绒皮草装饰。", "场景道具：深色麻将桌、麻将牌等。")
    assert any("等" in e for e in r["errors"])
    r = _broken("烟雾缭绕。", "烟雾缭绕之类的氛围，4K高清。")
    assert any("之类" in e for e in r["errors"]) and any("4K" in w for w in r["warnings"])
    r = _broken("@图片2眼神轻微移动", "女子眼神轻微移动")
    assert any("泛称" in w for w in r["warnings"])


def test_deliverable_needs_table_row_and_prompt_section():
    r = _broken("| @图片5 | 场景1 |", "| 场景图 | 场景1 |")
    assert any("槽位表里没有 @图片5" in e for e in r["errors"])
    r = _broken("### @图片3 · 男B", "### 男B")
    assert any("没有 @图片3（男B）的生成提示词小节" in e for e in r["errors"])


def test_old_agent_style_fails():
    old = ("# 最终视频提示词\n\n## 一、全片统一设定\n- **风格**：写实生活喜剧短片，AI生成质感\n\n## 二、逐秒提示词\n\n"
           "| 秒 | 提示词 |\n|---|---|\n| 1 | 中景，女子站在木桌左侧，男子手持木擀面杖。 |\n")
    r = remake.check_text(old, 15)
    assert not r["ok"]
    assert any("参考图绑定" in e for e in r["errors"]) and any("镜头标题" in e for e in r["errors"])


def test_json_deliverable():
    d = remake.to_json(EXAMPLE, 15, {"video": {"duration": 15}, "cuts": [6, 8]})
    assert d["duration"] == 15 and d["cuts"] == [6, 8] and d["check"]["ok"]
    s3 = next(s for s in d["slots"] if s["slot"] == "@图片3")
    assert s3["id"] == "男B" and "黑框方形眼镜" in s3["prompt"] and "纯浅灰背景" in s3["upload"]
    assert d["final_prompt"].startswith("参考图绑定") and "```" not in d["final_prompt"]
    assert len(d["shots"]) == 15 and d["shots"][0]["slots"] == ["@图片1", "@图片2", "@图片3", "@图片4", "@图片5"]


def test_cut_picking():
    flat = [(i / 25, 0.01) for i in range(200)]
    hard = list(flat)
    hard[75] = (3.0, 0.6)          # a classic hard cut
    hard[150] = (6.0, 0.12)        # a cut between two dark shots: low score but a lone spike
    cuts, maybe = remake.pick_cuts(hard)
    assert cuts == [3.0, 6.0]
    motion = [(i / 25, 0.2 if 100 <= i < 130 else 0.01) for i in range(200)]  # fast motion, no cut
    assert remake.pick_cuts(motion)[0] == []
    assert remake.shots_from_cuts([3.0, 6.0], 7.5) == [
        {"index": 1, "start": 0.0, "end": 3.0}, {"index": 2, "start": 3.0, "end": 6.0}, {"index": 3, "start": 6.0, "end": 7.5}]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_frames_end_to_end(tmp_path):
    v = tmp_path / "cuts.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=25:duration=2", "-filter_complex",
                    "[0][1]concat=n=2:v=1[v]", "-map", "[v]", str(v)], check=True, timeout=60)
    out = tmp_path / "out"
    assert remake.main(["frames", str(v), "--out", str(out)]) == 0
    import json
    a = json.loads((out / "analysis.json").read_text(encoding="utf-8"))
    assert a["video"]["aspect"] == "16:9" and abs(a["video"]["duration"] - 4) < 0.1
    assert a["cuts"] == [2.0] and len(a["shots"]) == 2 and len(a["seconds"]) == 4
    assert all((out / "frames" / Path(f["frame"]).name).exists() for f in a["seconds"])
    assert re.search(r"cut02_", " ".join(a["shots"][1]["frames"]))
