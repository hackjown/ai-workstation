"""v3.9.17: iOS froze opening a long dev task (7.5k-char pasted plan, many 继续 runs). Wiring checks for the SwiftUI
fix (the sources only compile in the ipa CI on macOS)."""
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "apps" / "ios" / "Sources"


def read(n):
    return (SRC / n).read_text(encoding="utf-8")


def test_text_sizes_are_cached():
    s = read("SelectableText.swift")
    assert "selectableSizeCache" in s and "setObject(NSValue(cgSize: out)" in s
    assert "func foldedText(" in s


def test_long_text_is_folded():
    assert "展开全文" in read("RichText.swift") and "var limit: Int = 4000" in read("RichText.swift")
    v2 = read("HomeV2.swift")
    assert "foldedText(p.text, limit: 600)" in v2 and "inlineMD(shownText)" in v2


def test_task_view_draws_latest_runs_only():
    c = read("ChatView.swift")
    assert "@State private var shownRuns = 4" in c and "显示更早的" in c
    assert "ForEach(m.tasks.indices, id: \\.self)" not in c
    assert "limit: latest ? 4000 : 800" in c


def test_scroll_fix_loop_is_capped():
    v2 = read("HomeV2.swift")
    assert "fixes < 3" in v2 and "fixes = 0" in v2


def test_card_prompt_is_one_short_line():
    assert "Text(promptLine(jstr(d, \"prompt\")))" in read("TaskCardV2.swift")
    assert "func promptLine(" in read("TaskCardV2.swift")
