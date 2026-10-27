#!/usr/bin/env bash
# BrowserOS neo (Debian package `browserclaw`, Chromium-based): an AI browser with its own MCP server
# (127.0.0.1:9210/mcp) and DevTools port (127.0.0.1:9110). AIW's vm-agent proxies its element-level tools
# (bos_* in the agent), with screenshot clicks as the fallback. amd64 only (no arm64 build is published).
#   bash browseros-setup.sh [install]   install/upgrade, autostart in the desktop session, start it now
#   bash browseros-setup.sh check       exit 0 when installed and its MCP port answers
#   bash browseros-setup.sh start       start it on the desktop if it is not running
set -euo pipefail
DEB_URL="${BROWSEROS_DEB_URL:-https://cdn.browseros.com/download/BrowserOS_neo.deb}"
BIN=/usr/bin/browserclaw
ARGS="--no-first-run --no-default-browser-check"
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"
CMD="${1:-install}"

mcp_up() { (exec 3<>/dev/tcp/127.0.0.1/9210) 2>/dev/null; }

display() {  # the desktop the agent drives: Xvnc :1 on the minimal/cloud images, :0 on setup-vm.sh's lightdm
  if [ -n "${DISPLAY:-}" ]; then echo "$DISPLAY"; return; fi
  for d in 1 0; do [ -S "/tmp/.X11-unix/X$d" ] && { echo ":$d"; return; }; done
  echo ":0"
}

start() {
  mcp_up && { echo "BrowserOS already running"; return 0; }
  pgrep -f "$BIN" >/dev/null 2>&1 || (DISPLAY="$(display)" setsid nohup "$BIN" $ARGS >/dev/null 2>&1 &)
  for _ in $(seq 1 60); do mcp_up && { echo "BrowserOS MCP is up on 127.0.0.1:9210"; return 0; }; sleep 1; done
  echo "BrowserOS started but MCP port 9210 did not answer within 60 s"; return 1
}

case "$CMD" in
  check) [ -x "$BIN" ] && dpkg -s browserclaw >/dev/null 2>&1 && mcp_up && { echo "ok"; exit 0; }; echo "not ready"; exit 1;;
  start) start; exit $?;;
  install) ;;
  *) echo "usage: $0 [install|check|start]"; exit 2;;
esac

ARCH="$(dpkg --print-architecture)"
if [ "$ARCH" != amd64 ]; then echo "BrowserOS neo is published for amd64 only (this computer is $ARCH); skipped"; exit 3; fi

echo "== downloading $DEB_URL"
curl -fL --retry 3 --connect-timeout 20 -o /tmp/browseros-neo.deb "$DEB_URL"
$SUDO apt-get update -qq || true
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/browseros-neo.deb
rm -f /tmp/browseros-neo.deb
[ -x "$BIN" ] || { echo "browserclaw binary missing after install"; exit 1; }

echo "== autostart in the desktop session"
A="$HOME/.config/autostart"; mkdir -p "$A"
cat > "$A/browseros.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BrowserOS
Exec=$BIN $ARGS
X-GNOME-Autostart-enabled=true
NoDisplay=false
EOF
D="$HOME/Desktop"; mkdir -p "$D"
APP=$(ls /usr/share/applications/*browserclaw*.desktop /usr/share/applications/*rowser*os*.desktop 2>/dev/null | head -1 || true)
if [ -n "$APP" ]; then cp "$APP" "$D/" && chmod +x "$D/$(basename "$APP")"
  command -v gio >/dev/null && gio set "$D/$(basename "$APP")" metadata::trusted true 2>/dev/null || true; fi

echo "== starting"
start || true
dpkg -s browserclaw | grep -E '^(Version|Status):' || true
echo "OK: BrowserOS installed (MCP 127.0.0.1:9210/mcp, CDP 127.0.0.1:9110). Turn it on per computer in Aiw 设置 › 电脑 › BrowserOS."
