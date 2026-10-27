#!/usr/bin/env bash
# 复刻视频 (video-remake) skill: SKILL.md, template, checklist, worked example and remake.py into
# ~/.claude/skills/video-remake; makes sure ffmpeg/ffprobe are there.  Usage: bash video-remake-setup.sh [check]
set -euo pipefail
D="$HOME/.claude/skills/video-remake"
if [ "${1:-}" = "check" ]; then test -f "$D/remake.py" && command -v ffprobe >/dev/null && exit 0 || exit 1; fi
BASE="${AIW_RAW:-https://raw.githubusercontent.com/hackjown/ai-workstation/main}/skills/video-remake"
LOCAL="$HOME/ai-workstation/skills/video-remake"
mkdir -p "$D"
for f in SKILL.md template.md checklist.md remake.py "example-%E9%BA%BB%E5%B0%86%E5%B1%80.md"; do
  out="$D/$(python3 -c 'import sys,urllib.parse;print(urllib.parse.unquote(sys.argv[1]))' "$f")"
  if ! curl -fsSL --retry 3 --connect-timeout 15 -o "$out.tmp" "$BASE/$f"; then
    # no GitHub from here: fall back to the repo copy on this computer, when there is one
    src="$LOCAL/$(basename "$out")"
    [ -f "$src" ] || { echo "cannot fetch $f"; rm -f "$out.tmp"; exit 1; }
    cp "$src" "$out.tmp"
  fi
  mv "$out.tmp" "$out"
done
chmod +x "$D/remake.py"
if ! command -v ffprobe >/dev/null; then
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  $SUDO apt-get update -qq || true
  $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y ffmpeg
fi
python3 "$D/remake.py" --help >/dev/null
echo "video-remake installed in $D"
