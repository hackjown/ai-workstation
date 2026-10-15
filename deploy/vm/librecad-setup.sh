#!/usr/bin/env bash
# One-time LibreCAD setup for drawing Chinese construction drawings from the command line (cad_command).
#  - notosc.lff: LibreCAD only draws its own .lff fonts and none of them has hanzi -> text showed as boxes
#  - default font notosc, new drawings from a 1:50 template (DIMSCALE 50, so dimension text is readable)
# Safe to run again. cad_command runs it automatically the first time.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
F="$HOME/.local/share/LibreCAD/fonts"; C="$HOME/.config/LibreCAD/LibreCAD.conf"; T="$HOME/.local/share/LibreCAD/aiw-1to50.dxf"
mkdir -p "$F" "$(dirname "$C")"; touch "$C"
if [ ! -s "$F/notosc.lff" ]; then
  P=python3
  for c in "$HOME/.aiw-venv/bin/python" python3; do "$c" -c "import fontTools" 2>/dev/null && { P=$c; break; }; done
  "$P" -c "import fontTools" 2>/dev/null || "$P" -m pip install -q fonttools 2>/dev/null || pip3 install -q --user fonttools 2>/dev/null
  "$P" "$HERE/ttf2lff.py" auto "$F/notosc.lff" || echo "font conversion failed (apt install fonts-noto-cjk)"
fi
cp "$HERE/librecad-template-1to50.dxf" "$T"
pkill -x librecad 2>/dev/null; sleep 1
python3 - "$C" "$F" "$T" <<'PY'
import re, sys
c, f, t = sys.argv[1:]
s = open(c).read()
def put(s, sec, k, v):
    if "[" + sec + "]" not in s:
        s += "\n[" + sec + "]\n"
    s = re.sub(r"(?m)^" + k + "=.*\n?", "", s)
    return s.replace("[" + sec + "]\n", "[" + sec + "]\n" + k + "=" + v + "\n", 1)
s = put(s, "Paths", "Fonts", f)
s = put(s, "Paths", "Template", t)
s = put(s, "Draw", "TextFont", "notosc")
s = put(s, "Startup", "FirstLoad", "0")
open(c, "w").write(s)
PY
mkdir -p "$HOME/.config/aiw" && touch "$HOME/.config/aiw/librecad-ready-v1"
echo "LibreCAD ready: font notosc, template $T"
