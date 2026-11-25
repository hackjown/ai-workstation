"""v3.9.87: the iOS group shows the 项目模式 stage bar, design / run screenshots and the 开工 / 方案 / 交付 buttons;
engine conversations on iOS show the pictures and files Cue made (data.media)."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "apps" / "ios" / "Sources"


def test_ios_group_project_ui():
    ev = (SRC / "EngineView.swift").read_text()
    assert "/api/bots/groups/\\(gid)/project" in ev
    assert "var projectBar" in ev and "func actions(_ m: JSON)" in ev and "askDeliver" in ev
    assert ev.count("EngineMediaList(media:") >= 2 and "EngineMediaView(media: media)" in ev   # group bot + 管家 rows; engine conv uses upstream EngineMediaView


def test_ios_media_list():
    ap = (SRC / "AttachmentPreview.swift").read_text()
    assert "struct EngineMediaList" in ap and "struct ShotThumb" in ap and "struct AttViewer" in ap
    assert "private struct AttViewer" not in ap


def test_ios_v3988_group():
    ev = (SRC / "EngineView.swift").read_text()
    assert "/api/bots/groups/\\(gid)/permit" in ev and "/api/bots/groups/\\(gid)/diff?" in ev
    assert "struct GroupDiffView" in ev and "reasonAct" in ev and "func actLive" in ev
    assert '"redesign": "pick"' in ev and 'g.s("needs")' in ev and '"ci": "deliver"' in ev
