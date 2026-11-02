#!/usr/bin/env bash
# v3.9.14 generic installer for the skills vendored in skills/<id> (skill-creator, webapp-testing, frontend-design, ...).
# Copies skills/<id> into ~/.claude/skills/<id> (from the repo checkout on this computer when there is one, else every
# file in its MANIFEST from AIW_RAW), then installs what that skill needs.
# Usage: bash aiw-skill-setup.sh <id> [<id> ...]      bash aiw-skill-setup.sh check <id>
set -euo pipefail
RAW="${AIW_RAW:-https://raw.githubusercontent.com/hackjown/ai-workstation/main}"
check() {
  local d="$HOME/.claude/skills/$1"
  test -f "$d/SKILL.md" || return 1
  case "$1" in
    webapp-testing) python3 -c 'import playwright' 2>/dev/null || return 1 ;;
  esac
}
if [ "${1:-}" = "check" ]; then shift; for s in "$@"; do check "$s" || exit 1; done; exit 0; fi
[ $# -gt 0 ] || { echo "usage: $0 <skill-id> [...]"; exit 2; }
# the AIW checkout on this computer (~/ai-workstation, which also runs the VM agent: never move it) is updated first
[ -d "$HOME/ai-workstation/.git" ] && timeout 40 git -C "$HOME/ai-workstation" pull -q --ff-only >/dev/null 2>&1 || true
for s in "$@"; do
  case "$s" in *[!a-z0-9-]*|"") echo "bad skill id: $s"; exit 2 ;; esac
  D="$HOME/.claude/skills/$s"; LOCAL="$HOME/ai-workstation/skills/$s"; BASE="$RAW/skills/$s"
  mkdir -p "$D"
  if [ -f "$LOCAL/MANIFEST" ]; then
    (cd "$LOCAL" && tar --exclude=node_modules --exclude=__pycache__ -cf - .) | (cd "$D" && tar xf -)
  else
    curl -fsSL --retry 3 --connect-timeout 15 -o "$D/MANIFEST" "$BASE/MANIFEST"
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      mkdir -p "$D/$(dirname "$f")"
      enc=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$f")
      curl -fsSL --retry 3 --connect-timeout 15 -o "$D/$f.tmp" "$BASE/$enc" && mv "$D/$f.tmp" "$D/$f" || { echo "cannot fetch $f"; exit 1; }
    done < "$D/MANIFEST"
  fi
  case "$s" in
    webapp-testing)
      if ! python3 -c 'import playwright' 2>/dev/null; then
        python3 -m pip install -q --user playwright 2>/dev/null || python3 -m pip install -q --user --break-system-packages playwright
      fi
      # Chromium for Playwright; system libraries need sudo, which may ask for a password (then the browser may still run)
      python3 -m playwright install chromium >/dev/null 2>&1 || true
      sudo -n python3 -m playwright install-deps chromium >/dev/null 2>&1 || true
      ;;
    skill-creator)
      python3 -c 'import yaml' 2>/dev/null || python3 -m pip install -q --user pyyaml 2>/dev/null || \
        python3 -m pip install -q --user --break-system-packages pyyaml || true
      ;;
  esac
  echo "$s installed in $D"
done
