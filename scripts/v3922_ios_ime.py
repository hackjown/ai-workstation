"""v3.9.22: iOS 补充/回答输入框改用 IMEField（拼音首字母被截断）。按字符串替换，不覆盖你本地的其他改动；可重复运行。"""
import pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
IME_ADD = "\n/// v3.9.21: IMEField with its own focus and a rounded border, for the answer / 补充 boxes that sit inside views\n/// a running task keeps re-rendering. SwiftUI's TextField drops the pinyin being composed on such a re-render,\n/// which is what cut the first letter off (the same bug v3.9.17 fixed in the composer).\nstruct IMEInput: View {\n    let placeholder: String\n    @Binding var text: String\n    var maxLines: Int = 4\n    var autofocus = false\n    @State private var focused = false\n    var body: some View {\n        IMEField(placeholder: placeholder, text: $text, focused: $focused, maxLines: maxLines)\n            .padding(.horizontal, 8)\n            .background(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.35)))\n            .onAppear { if autofocus { focused = true } }\n    }\n}\n"
REPS = [
 [
  "apps/ios/Sources/ChatView.swift",
  "TextField(ask.kind == .edit ? \"新的要求\" : \"例如：顺便把 PDF 也转成 Word\", text: $text, axis: .vertical).lineLimit(3...10)",
  "IMEInput(placeholder: ask.kind == .edit ? \"新的要求\" : \"例如：顺便把 PDF 也转成 Word\", text: $text, maxLines: 10, autofocus: true)   // v3.9.21 pinyin"
 ],
 [
  "apps/ios/Sources/ChatView.swift",
  "TextField(\"回答…\", text: $answer).textFieldStyle(.roundedBorder)",
  "IMEInput(placeholder: \"回答…\", text: $answer)   // v3.9.21 pinyin"
 ],
 [
  "apps/ios/Sources/HomeChat.swift",
  "TextField(opts.isEmpty ? \"回答…\" : \"或者直接告诉它…\", text: $reply).textFieldStyle(.roundedBorder).submitLabel(.send)\n                        .onSubmit { send() }",
  "IMEInput(placeholder: opts.isEmpty ? \"回答…\" : \"或者直接告诉它…\", text: $reply)   // v3.9.21 pinyin"
 ],
 [
  "apps/ios/Sources/TaskCardV2.swift",
  "TextField(opts.isEmpty ? \"回答…\" : \"或者直接告诉它…\", text: $reply).textFieldStyle(.roundedBorder).submitLabel(.send)\n                .onSubmit { send() }",
  "IMEInput(placeholder: opts.isEmpty ? \"回答…\" : \"或者直接告诉它…\", text: $reply)   // v3.9.21 pinyin"
 ]
]
bad = 0
f = ROOT / "apps/ios/Sources/IMEField.swift"
s = f.read_text()
if "struct IMEInput" not in s:
    f.write_text(s.rstrip("\n") + "\n" + IME_ADD); print("IMEField.swift: 加上 IMEInput")
for rel, old, new in REPS:
    p = ROOT / rel; s = p.read_text()
    if new in s:
        print(rel, ": 已改过"); continue
    # tolerate different indentation before .onSubmit
    pat = re.escape(old).replace(re.escape("\n") + r"\ +", r"\n\s+")
    pat = re.sub(r"(\\\n)(\\ )+", r"\\n\\s+", re.escape(old))
    s2, n = re.subn(pat, lambda m: new, s, count=1)
    if n:
        p.write_text(s2); print(rel, ": 已替换")
    else:
        bad += 1; print(rel, ": 没找到原来的输入框，请把这个文件发我", file=sys.stderr)
sys.exit(1 if bad else 0)
