#!/usr/bin/env bash
# Repair the headless desktop :1 (black screen) and the Python tools the agent needs, then print a diagnosis.
# Run in the VM as the desktop user:  curl -fsSL <raw url of this file> | bash
set -uo pipefail
U=$(id -un); H=$HOME; UID_=$(id -u)

echo "== 1/4 Python packages (Debian blocks plain pip; use apt + allow pip --user)"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-pip python3-openpyxl python3-bs4 python3-docx python3-matplotlib fonts-noto-cjk imagemagick x11-apps scrot \
  dbus-x11 x11-utils xfce4 xfce4-session >/dev/null
python3 -m pip install -q --user --break-system-packages python-pptx || true
grep -q '^PIP_BREAK_SYSTEM_PACKAGES=' "$H/.config/aiw/vm-agent.env" || echo 'PIP_BREAK_SYSTEM_PACKAGES=1' >> "$H/.config/aiw/vm-agent.env"

echo "== 2/4 XFCE session on :1 (needs a runtime dir and session env when started by systemd)"
sudo loginctl enable-linger "$U"
sudo tee /etc/systemd/system/aiw-xfce.service >/dev/null <<UNIT
[Unit]
Description=AI Workstation XFCE session on :1
After=aiw-xvnc.service user@$UID_.service
Requires=aiw-xvnc.service

[Service]
User=$U
Environment=DISPLAY=:1
Environment=HOME=$H
Environment=XDG_RUNTIME_DIR=/run/user/$UID_
Environment=XDG_SESSION_TYPE=x11
Environment=XDG_CURRENT_DESKTOP=XFCE
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 30); do xdpyinfo -display :1 >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/bin/dbus-launch --exit-with-session /usr/bin/xfce4-session
ExecStartPost=-/bin/sh -c 'sleep 4; xset -display :1 s off; xset -display :1 s noblank; pkill -u \$USER -f xfce4-screensaver; exit 0'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable aiw-xvnc aiw-xfce >/dev/null 2>&1
sudo systemctl restart aiw-xvnc; sleep 2; sudo systemctl restart aiw-xfce
systemctl --user restart vm-agent novnc 2>/dev/null
sleep 8

echo "== 3/4 Status"
for s in aiw-xvnc aiw-xfce; do printf '%-10s %s\n' "$s" "$(systemctl is-active $s)"; done
for s in vm-agent novnc; do printf '%-10s %s\n' "$s" "$(systemctl --user is-active $s)"; done
grep '^DISPLAY=' "$H/.config/aiw/vm-agent.env"
DISPLAY=:1 xdpyinfo >/dev/null 2>&1 && echo "display :1 ok" || echo "display :1 NOT reachable"
echo "windows on :1:"; DISPLAY=:1 xprop -root _NET_CLIENT_LIST 2>/dev/null || true
pgrep -a -u "$U" -f 'xfce4-panel|xfdesktop|xfwm4' | head -5 || echo "(no XFCE processes)"
python3 -c "import openpyxl, docx; print('openpyxl', openpyxl.__version__, '+ python-docx ok')"

echo "== 4/4 Last log lines (send these if the screen is still black)"
sudo journalctl -u aiw-xfce -n 15 --no-pager
sudo journalctl -u aiw-xvnc -n 5 --no-pager
