#!/usr/bin/env bash
# Give the agent its own virtual screen, independent of the PVE console.
#
# Before: x11vnc mirrored :0, the VM's physical (virtual-GPU) display. It only exists after a login on the
# PVE console and goes black whenever that console blanks or locks.
# After:  TigerVNC (Xvnc) runs a virtual X server :1 at boot as a system service, with its own XFCE session.
#         No login, no console, no screen blanking. The PVE console becomes a plain text login.
#
# Run INSIDE the VM as the desktop user (e.g. aiw), with sudo:
#   VNC_PASSWORD='new-password' ./deploy/vm/headless-desktop.sh
# Optional: GEOMETRY=1920x1080 (default 1600x900)
set -euo pipefail
: "${VNC_PASSWORD:?set VNC_PASSWORD (the noVNC password; pick a new one)}"
U=$(id -un); H=$HOME; GEOMETRY=${GEOMETRY:-1600x900}

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tigervnc-standalone-server tigervnc-tools dbus-x11 x11-utils \
  python3-pip python3-openpyxl python3-docx python3-matplotlib fonts-noto-cjk
python3 -m pip install -q --user --break-system-packages python-pptx || true

mkdir -p "$H/.config/aiw"
printf '%s\n' "$VNC_PASSWORD" | vncpasswd -f > "$H/.config/aiw/tigervnc.pass"
chmod 600 "$H/.config/aiw/tigervnc.pass"

# 1) virtual X server :1 (VNC on localhost:5901, screen saver off)
sudo tee /etc/systemd/system/aiw-xvnc.service >/dev/null <<UNIT
[Unit]
Description=AI Workstation virtual display :1 (TigerVNC)
After=network.target

[Service]
User=$U
ExecStartPre=-/bin/rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
ExecStart=/usr/bin/Xtigervnc :1 -geometry $GEOMETRY -depth 24 -rfbport 5901 -localhost \\
  -SecurityTypes VncAuth -PasswordFile $H/.config/aiw/tigervnc.pass -AlwaysShared -s 0
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

# 2) XFCE session on :1
sudo tee /etc/systemd/system/aiw-xfce.service >/dev/null <<UNIT
[Unit]
Description=AI Workstation XFCE session on :1
After=aiw-xvnc.service
Requires=aiw-xvnc.service

[Service]
User=$U
Environment=DISPLAY=:1
Environment=HOME=$H
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u)
Environment=XDG_SESSION_TYPE=x11
Environment=XDG_CURRENT_DESKTOP=XFCE
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 30); do xdpyinfo -display :1 >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/bin/dbus-launch --exit-with-session /usr/bin/startxfce4
ExecStartPost=-/bin/sh -c 'sleep 3; xset -display :1 s off; xset -display :1 s noblank; pkill -u \$USER -f xfce4-screensaver; exit 0'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# no lock screen / screensaver inside the session
sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y light-locker xfce4-screensaver 2>/dev/null || true

# 3) point the agent and noVNC at :1 / 5901, retire the :0 mirror
sed -i 's/^DISPLAY=.*/DISPLAY=:1/' "$H/.config/aiw/vm-agent.env"
grep -q '^DISPLAY=' "$H/.config/aiw/vm-agent.env" || echo 'DISPLAY=:1' >> "$H/.config/aiw/vm-agent.env"
mkdir -p "$H/.config/systemd/user"
cat > "$H/.config/systemd/user/novnc.service" <<UNIT
[Unit]
Description=noVNC / websockify (6080 -> localhost:5901)

[Service]
ExecStart=/usr/bin/websockify --web /usr/share/novnc 6080 localhost:5901
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
sed -i 's/^After=graphical-session.target/After=default.target/' "$H/.config/systemd/user/vm-agent.service" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user disable --now x11vnc 2>/dev/null || true
sudo loginctl enable-linger "$U"

# 4) the console no longer needs a graphical login (avoids two XFCE sessions for one user)
sudo systemctl disable lightdm 2>/dev/null || true
sudo systemctl set-default multi-user.target

sudo systemctl daemon-reload
sudo systemctl enable --now aiw-xvnc aiw-xfce
systemctl --user restart novnc vm-agent

echo
echo "Done. Virtual desktop :1 ($GEOMETRY) runs at boot without any login."
echo "Check:  systemctl status aiw-xvnc aiw-xfce --no-pager;  systemctl --user status vm-agent novnc --no-pager"
echo "Reboot once to confirm, and do NOT open the PVE console: the web 'Computer' view should show XFCE."
echo "Undo:   sudo systemctl disable --now aiw-xvnc aiw-xfce && sudo systemctl enable lightdm && sudo systemctl set-default graphical.target"
