"""v3.9.17: pinyin input in the iOS composers was split ("shengcheng" -> "s" + "hengcheng")."""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "apps" / "ios" / "Sources"


def test_imefield_publishes_only_committed_text():
    s = (SRC / "IMEField.swift").read_text(encoding="utf-8")
    assert "struct IMEField: UIViewRepresentable" in s
    assert "guard tv.markedTextRange == nil else { return }" in s          # no binding update while composing
    assert "if v.markedTextRange == nil && v.text != text" in s             # no write-back while composing


def test_both_composers_use_imefield():
    home = (SRC / "HomeChat.swift").read_text(encoding="utf-8")
    chat = (SRC / "ChatView.swift").read_text(encoding="utf-8")
    assert "IMEField(placeholder: placeholder, text: $text, focused: $focused" in home
    assert 'IMEField(placeholder: "和电脑说点什么' in chat
    assert "TextField(placeholder, text: $text, axis: .vertical)" not in home
    assert 'TextField("和电脑说点什么' not in chat
    assert ".focused($focused)" not in home and ".focused($focused)" not in chat


def test_build_script_compiles_every_source():
    assert "Sources/*.swift" in (SRC.parent / "build-ipa.sh").read_text(encoding="utf-8")
