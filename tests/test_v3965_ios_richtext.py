"""v3.9.65 iOS: list items use the bubble's whole width; "19~22°C" is not struck through."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "apps/ios/Sources/RichText.swift").read_text(encoding="utf-8")


def block(case):
    i = SRC.index(f"case .{case}(")
    return SRC[i:SRC.index("case .", i + 6)]


def test_list_rows_give_the_text_the_rest_of_the_width():
    for case in ("bullet", "number", "quote"):
        assert "sel(s" in block(case) and ".frame(maxWidth: .infinity, alignment: .leading).layoutPriority(1)" in block(case), case
    assert ".fixedSize()" in block("bullet") and ".fixedSize()" in block("number")
    assert SRC.count("case .quote(") == 1


def escape_tildes(s):   # the Swift escapeTildes, line for line
    out, code, i = "", False, 0
    while i < len(s):
        c = s[i]
        if c == "`":
            code = not code; out += c; i += 1; continue
        if c == "~" and not code:
            if i + 1 < len(s) and s[i + 1] == "~":
                out += "~~"; i += 2; continue
            out += "\\~"; i += 1; continue
        out += c; i += 1
    return out


def test_single_tildes_are_escaped():
    assert "t = escapeTildes(t)" in SRC and 'out += "\\\\~"' in SRC
    assert escape_tildes("19~22°C (体感 18~23°C)") == "19\\~22°C (体感 18\\~23°C)"
    assert escape_tildes("~~删除~~ 和 `a~b`") == "~~删除~~ 和 `a~b`"
