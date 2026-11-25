"""v3.9.87: the iOS group shows the 项目模式 stage bar, design / run screenshots and the 开工 / 方案 / 交付 buttons;
engine conversations on iOS show the pictures and files Cue made (data.media)."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "apps" / "ios" / "Sources"


def test_ios_group_project_ui():
    ev = (SRC / "EngineView.swift").read_text()
    assert "/api/bots/groups/\\(gid)/project" in ev
    assert "var projectBar" in ev and "func actions(_ m: JSON)" in ev and "askDeliver" in ev
    assert ev.count("EngineMediaList(media:") >= 3   # engine conv, group bot rows, 管家 rows


def test_ios_media_list():
    ap = (SRC / "AttachmentPreview.swift").read_text()
    assert "struct EngineMediaList" in ap and "struct ShotThumb" in ap and "struct AttViewer" in ap
    assert "private struct AttViewer" not in ap
