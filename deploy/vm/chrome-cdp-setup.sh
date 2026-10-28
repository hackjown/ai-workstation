#!/usr/bin/env bash
# Chrome / Chromium on this AIW computer, ALWAYS with the DevTools port on 127.0.0.1 (default 9222), so the 京东/淘宝
# price watch and the browser tools can read the pages the user is logged in to. Run as the desktop user (aiw), no sudo:
#   bash deploy/vm/chrome-cdp-setup.sh install    # ~/.local/bin/aiw-chrome + menu / panel / desktop / autostart launchers
#   bash deploy/vm/chrome-cdp-setup.sh relaunch   # open the port now: a running Chrome without it is closed cleanly and
#                                                 # reopened on the same profile (tabs + logins kept)
#   bash deploy/vm/chrome-cdp-setup.sh check      # what is running, and curl 127.0.0.1:<port>/json/version
# AIW_CDP_PORT=9222 picks the port; AIW_CHROME_AUTOSTART=0 skips the autostart entry. The logic is in
# vm-agent/vm_agent/browser/chrome_cdp.py (the vm-agent and the API use the same file).
set -uo pipefail
R="${AIW_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)}"
[ -f "$R/vm-agent/vm_agent/browser/chrome_cdp.py" ] || R="$HOME/ai-workstation"
T="$R/vm-agent/vm_agent/browser/chrome_cdp.py"
PORT="${AIW_CDP_PORT:-9222}"
[ -f "$T" ] || { echo "ERROR: $T not found (set AIW_REPO)"; exit 1; }
if [ "$(id -u)" = 0 ]; then echo "ERROR: run this as the desktop user (aiw), not root"; exit 1; fi
case "${1:-install}" in
  install)  python3 "$T" install-launchers --port "$PORT" ;;
  relaunch) python3 "$T" ensure --port "$PORT" --also 0 ;;
  check)    python3 "$T" status --port "$PORT"
            echo "== curl 127.0.0.1:$PORT/json/version"
            curl -s -m 3 --noproxy '*' "http://127.0.0.1:$PORT/json/version" || echo "(port $PORT not answering)" ;;
  *) echo "usage: $0 install|relaunch|check"; exit 2 ;;
esac
