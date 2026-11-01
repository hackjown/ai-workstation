#!/usr/bin/env bash
# 户型图 → 装修设计器 (house-plan-designer) skill: copies skills/house-plan-designer into ~/.claude/skills/house-plan-designer
# (the repo copy on this computer when there is one, else every file in MANIFEST from AIW_RAW), then installs the
# Python / Node / Chrome / CJK-font dependencies with scripts/aiw_hpd.py ensure.  Usage: bash house-plan-designer-setup.sh [check]
set -euo pipefail
D="$HOME/.claude/skills/house-plan-designer"
if [ "${1:-}" = "check" ]; then
  test -f "$D/assets/template.html" && test -d "$D/scripts/node_modules/puppeteer-core" && \
    python3 "$D/scripts/aiw_hpd.py" doctor | grep -q '"ok": true' && exit 0 || exit 1
fi
BASE="${AIW_RAW:-https://raw.githubusercontent.com/hackjown/ai-workstation/main}/skills/house-plan-designer"
LOCAL="$HOME/ai-workstation/skills/house-plan-designer"
mkdir -p "$D"
if [ -f "$LOCAL/MANIFEST" ]; then
  (cd "$LOCAL" && tar --exclude=node_modules -cf - .) | (cd "$D" && tar xf -)
else
  curl -fsSL --retry 3 --connect-timeout 15 -o "$D/MANIFEST" "$BASE/MANIFEST"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    mkdir -p "$D/$(dirname "$f")"
    enc=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$f")
    curl -fsSL --retry 3 --connect-timeout 15 -o "$D/$f.tmp" "$BASE/$enc" && mv "$D/$f.tmp" "$D/$f" || { echo "cannot fetch $f"; exit 1; }
  done < "$D/MANIFEST"
fi
python3 "$D/scripts/aiw_hpd.py" ensure
echo "house-plan-designer installed in $D"
