#!/usr/bin/env bash
# Keep the headless desktop :1 from ever going black (screensaver, power manager blanking, lockers).
# Run in the VM as the desktop user:  curl -fsSL <raw url of this file> | bash
set -uo pipefail
export DISPLAY=:1
SPID=$(pgrep -u "$(id -u)" -x xfce4-session | head -1)
[ -n "$SPID" ] && export $(tr '\0' '\n' < /proc/$SPID/environ | grep '^DBUS_SESSION_BUS_ADDRESS=')

echo "== 1/4 XFCE settings: no screensaver, no lock, no blanking"
q() { xfconf-query -c "$1" -p "$2" -n -t "$3" -s "$4" 2>/dev/null || true; }
q xfce4-screensaver /saver/enabled bool false
q xfce4-screensaver /lock/enabled bool false
q xfce4-power-manager /xfce4-power-manager/blank-on-ac int 0
q xfce4-power-manager /xfce4-power-manager/blank-on-battery int 0
q xfce4-power-manager /xfce4-power-manager/dpms-enabled bool false
q xfce4-power-manager /xfce4-power-manager/dpms-on-ac-sleep int 0
q xfce4-power-manager /xfce4-power-manager/dpms-on-ac-off int 0
q xfce4-power-manager /xfce4-power-manager/lock-screen-suspend-hibernate bool false

echo "== 2/4 Disable screensaver/locker autostart and stop running ones"
mkdir -p "$HOME/.config/autostart"
for a in xfce4-screensaver light-locker xscreensaver gnome-screensaver; do
  printf '[Desktop Entry]\nType=Application\nName=%s\nHidden=true\n' "$a" > "$HOME/.config/autostart/$a.desktop"
done
pkill -u "$(id -u)" -f 'xfce4-screensaver|light-locker|xscreensaver' 2>/dev/null || true

echo "== 3/4 Keep-alive service (resets the X screensaver every 60s)"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/aiw-noblank.service" <<'UNIT'
[Unit]
Description=Keep virtual desktop :1 awake

[Service]
Environment=DISPLAY=:1
ExecStart=/bin/sh -c 'while true; do xset s off; xset s noblank; xset s reset; sleep 60; done'
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now aiw-noblank >/dev/null 2>&1
xset s reset

echo "== 4/4 Check"
printf 'aiw-noblank  %s\n' "$(systemctl --user is-active aiw-noblank)"
xset q | grep -A2 'Screen Saver'
pgrep -a -u "$(id -u)" -f 'xfce4-screensaver|light-locker|xscreensaver' || echo "no screensaver running"
