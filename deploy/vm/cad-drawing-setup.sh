#!/usr/bin/env bash
# 照图出图 (cad-drawing) skill: SKILL.md, drawkit.py and the worked example into ~/.claude/skills/cad-drawing, plus
# numpy / Pillow / ezdxf and a Chinese font (drawkit.py ensure). The agent's drawing_build installs the same kit into
# ~/workspace/.aiw/cad-drawing by itself, so this script is optional.  Usage: bash cad-drawing-setup.sh [check]
set -euo pipefail
D="$HOME/.claude/skills/cad-drawing"
if [ "${1:-}" = "check" ]; then test -f "$D/drawkit.py" && python3 -c "import ezdxf, PIL, numpy" 2>/dev/null && exit 0 || exit 1; fi
BASE="${AIW_RAW:-https://raw.githubusercontent.com/hackjown/ai-workstation/main}/skills/cad-drawing"
LOCAL="$HOME/ai-workstation/skills/cad-drawing"
mkdir -p "$D/examples"
for f in SKILL.md drawkit.py drawauto.py examples/xinda-g-108.spec.json; do
  if ! curl -fsSL --retry 3 --connect-timeout 15 -o "$D/$f.tmp" "$BASE/$f"; then
    [ -f "$LOCAL/$f" ] || { echo "cannot fetch $f"; rm -f "$D/$f.tmp"; exit 1; }
    cp "$LOCAL/$f" "$D/$f.tmp"
  fi
  mv "$D/$f.tmp" "$D/$f"
done
chmod +x "$D/drawkit.py"
python3 "$D/drawkit.py" ensure || echo "some dependencies are missing (see above); drawkit tries again on first use"
echo "cad-drawing installed in $D"
