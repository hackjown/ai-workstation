#!/usr/bin/env bash
# BrowserOS neo (Debian package `browserclaw`, Chromium-based): an AI browser with its own MCP server
# (127.0.0.1:9210/mcp) and DevTools port (127.0.0.1:9110). AIW's vm-agent proxies its element-level tools
# (bos_* in the agent), with screenshot clicks as the fallback. amd64 only (no arm64 build is published).
#   bash browseros-setup.sh [install]   install when missing (idempotent: already installed = just make sure it runs)
#   bash browseros-setup.sh upgrade     download and install the newest deb even when one is installed
#   bash browseros-setup.sh check       exit 0 when installed and its MCP port answers
#   bash browseros-setup.sh status      one JSON line: installed / running / version / arch (what 重新检测 reads)
#   bash browseros-setup.sh start       start it on the desktop if it is not running
# On a computer whose sudo asks for a password, run it yourself in a terminal (接管 › 终端 or ssh):
#   curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/browseros-setup.sh | bash
# (sudo asks for the password on the terminal; `| sudo bash` works too.)
# Every failure ends with one line `ERROR: <step>: <reason>` on stdout; a sudo that needs a password ends with
# `ERROR: sudo: NEED_SUDO_PASSWORD` and exit code 10, so Aiw can say so plainly instead of showing a log tail.
set -uo pipefail
DEB_URL="${BROWSEROS_DEB_URL:-https://cdn.browseros.com/download/BrowserOS_neo.deb}"
BIN="${BROWSEROS_BIN:-/usr/bin/browserclaw}"
ARGS="--no-first-run --no-default-browser-check"
CMD="${1:-install}"
LOG="${TMPDIR:-/tmp}/browseros-setup.$$.log"
trap 'rm -f "$LOG"' EXIT

fail() {  # fail <step> <reason> [exit code]: the last meaningful lines of the step's output, then one ERROR line
  if [ -s "$LOG" ]; then
    grep -v -E '^\s*$|^(Reading|Building|Get:|Hit:|Ign:|Fetched|Selecting|Preparing|Unpacking|Setting up|Processing)' "$LOG" \
      | tail -n 6 | sed 's/^/   | /'
  fi
  echo "ERROR: $1: $2"
  exit "${3:-1}"
}

# --- who runs it: the desktop user (from the vm-agent / a terminal) or root (`| sudo bash`)
if [ "$(id -u)" = 0 ]; then
  SUDO=""
  DESK_USER="${SUDO_USER:-}"; [ "$DESK_USER" = root ] && DESK_USER=""
else
  SUDO="sudo -n"
  DESK_USER=""
fi
as_user() {  # run the per-user part (autostart, starting the window) as the desktop user, never as root
  if [ -n "$DESK_USER" ]; then runuser -u "$DESK_USER" -- env HOME="$(getent passwd "$DESK_USER" | cut -d: -f6)" "$@"
  else "$@"; fi
}
need_sudo() {  # sudo -n first (vm-agent, cloud boxes); a terminal falls back to the normal password prompt
  [ -z "$SUDO" ] && return 0
  sudo -n true 2>/dev/null && return 0
  if { : </dev/tty; } 2>/dev/null; then
    echo "== sudo needs this computer's password (asked once)"
    sudo -v </dev/tty && { SUDO="sudo"; return 0; }
  fi
  fail sudo "NEED_SUDO_PASSWORD" 10
}

mcp_up() { (exec 3<>/dev/tcp/127.0.0.1/9210) 2>/dev/null; }
cdp_up() { (exec 3<>/dev/tcp/127.0.0.1/9110) 2>/dev/null; }
# running = both ports: its MCP server (browseros-claw-server) can outlive a closed/crashed window and keep 9210 open
up() { mcp_up && cdp_up; }
browser_running() { pgrep -af "browserclaw/browserclaw" 2>/dev/null | grep -v -e "--type=" -e crashpad | grep -q .; }
installed() { [ -x "$BIN" ] && dpkg -s browserclaw >/dev/null 2>&1; }

display() {  # the desktop the agent drives: Xvnc :1 on the minimal/cloud images, :0 on setup-vm.sh's lightdm
  if [ -n "${DISPLAY:-}" ]; then echo "$DISPLAY"; return; fi
  for d in 1 0; do [ -S "/tmp/.X11-unix/X$d" ] && { echo ":$d"; return; }; done
  echo ":0"
}

start() {
  up && { echo "BrowserOS already running"; return 0; }
  installed || { echo "BrowserOS is not installed"; return 1; }
  if [ -z "${DISPLAY:-}" ] && ! ls /tmp/.X11-unix/X* >/dev/null 2>&1; then
    echo "This computer has no desktop (no X server, /tmp/.X11-unix is empty). BrowserOS needs one, and so do the"
    echo "agent's screenshots and the takeover view. Set up the virtual desktop first (TigerVNC :1 + XFCE + vm-agent):"
    echo "  git clone --depth 1 https://github.com/hackjown/ai-workstation.git ~/ai-workstation && VM_AGENT_TOKEN='...' VNC_PASSWORD='...' bash ~/ai-workstation/deploy/vm/setup-min.sh"
    echo "then run this script again."
    return 1
  fi
  if ! browser_running; then
    pkill -f browseros-claw-server 2>/dev/null && sleep 1   # an orphaned MCP server would hold 9210
    # detached: the caller (vm-agent shell) must not wait on the browser
    ( as_user env DISPLAY="$(display)" setsid nohup "$BIN" $ARGS >/dev/null 2>&1 </dev/null & )
  fi
  for _ in $(seq 1 "${BROWSEROS_START_WAIT:-60}"); do up && { echo "BrowserOS MCP is up on 127.0.0.1:9210"; return 0; }; sleep 1; done
  echo "BrowserOS started but MCP port 9210 did not answer within ${BROWSEROS_START_WAIT:-60} s (is the desktop $(display) up?)"; return 1
}

status_json() {
  local inst=false run=false ver=""
  installed && inst=true
  up && run=true
  $inst && ver=$(dpkg-query -W -f='${Version}' browserclaw 2>/dev/null)
  printf '{"installed": %s, "running": %s, "version": "%s", "arch": "%s", "mcp_port": 9210, "cdp_port": 9110}\n' \
    "$inst" "$run" "$ver" "$(dpkg --print-architecture 2>/dev/null || uname -m)"
}

autostart() {
  local H; H="$(as_user sh -c 'echo $HOME')"
  as_user mkdir -p "$H/.config/autostart" "$H/Desktop"
  as_user sh -c "cat > '$H/.config/autostart/browseros.desktop'" <<EOF
[Desktop Entry]
Type=Application
Name=BrowserOS
Exec=$BIN $ARGS
X-GNOME-Autostart-enabled=true
NoDisplay=false
EOF
  local APP
  APP=$(ls /usr/share/applications/*browserclaw*.desktop /usr/share/applications/*rowser*os*.desktop 2>/dev/null | head -1 || true)
  if [ -n "$APP" ]; then as_user cp -f "$APP" "$H/Desktop/" && as_user chmod +x "$H/Desktop/$(basename "$APP")"
    command -v gio >/dev/null && as_user gio set "$H/Desktop/$(basename "$APP")" metadata::trusted true 2>/dev/null || true; fi
}

case "$CMD" in
  check) installed && up && { echo "ok"; exit 0; }; echo "not ready"; exit 1;;
  status) status_json; exit 0;;
  start) start; exit $?;;
  install|upgrade) ;;
  *) echo "usage: $0 [install|upgrade|check|status|start]"; exit 2;;
esac

ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
if [ "$ARCH" != amd64 ]; then echo "ERROR: arch: BrowserOS neo is published for amd64 only (this computer is $ARCH)"; exit 3; fi

if [ "$CMD" = install ] && installed; then
  echo "== BrowserOS is already installed ($(dpkg-query -W -f='${Version}' browserclaw)); making sure it runs"
  [ -f "$(as_user sh -c 'echo $HOME')/.config/autostart/browseros.desktop" ] || autostart
  start || fail start "BrowserOS is installed but its MCP port 9210 did not come up"
  echo "OK: BrowserOS installed (MCP 127.0.0.1:9210/mcp, CDP 127.0.0.1:9110)"
  exit 0
fi

need_sudo

echo "== downloading $DEB_URL"
DEB="${TMPDIR:-/tmp}/browseros-neo.deb"
curl -fsSL --retry 3 --connect-timeout 20 -o "$DEB" "$DEB_URL" >"$LOG" 2>&1 || fail download "curl could not fetch $DEB_URL"
[ "$(stat -c %s "$DEB" 2>/dev/null || echo 0)" -gt 1000000 ] || fail download "the downloaded file is too small to be the deb"
dpkg-deb -I "$DEB" >/dev/null 2>&1 || fail download "the downloaded file is not a valid .deb"

echo "== installing (apt resolves its dependencies; waits up to 5 min for another apt to finish)"
APT_OPTS="-o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get $APT_OPTS update -qq >"$LOG" 2>&1 || true
if ! $SUDO env DEBIAN_FRONTEND=noninteractive apt-get $APT_OPTS install -y -q "$DEB" >"$LOG" 2>&1; then
  # a half-configured earlier attempt: let dpkg finish it, then try once more
  $SUDO env DEBIAN_FRONTEND=noninteractive dpkg --configure -a >>"$LOG" 2>&1 || true
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get $APT_OPTS install -y -q -f "$DEB" >"$LOG" 2>&1 \
    || fail apt "apt-get install of the BrowserOS deb failed"
fi
rm -f "$DEB"
installed || fail install "browserclaw binary missing after install"

echo "== autostart in the desktop session"
autostart

echo "== starting"
start || fail start "BrowserOS installed but its MCP port 9210 did not come up (desktop $(display))"
dpkg -s browserclaw | grep -E '^(Version|Status):' || true
echo "OK: BrowserOS installed (MCP 127.0.0.1:9210/mcp, CDP 127.0.0.1:9110). Turn it on per computer in Aiw 设置 › 电脑 › BrowserOS."
