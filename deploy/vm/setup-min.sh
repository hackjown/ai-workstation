#!/usr/bin/env bash
# Lean AI Workstation computer on a fresh Debian 13 minimal install (no desktop chosen in the installer,
# only "SSH server" + "standard system utilities"). Same layout as the InstaBox cloud machines:
# XFCE core on a virtual TigerVNC screen :1 at boot, no display manager, no xfce4-goodies.
# Installs LibreCAD (+ hanzi font), LibreOffice (Writer/Calc/Impress, zh-CN), Chromium, the Python document
# libraries, vm-agent and noVNC.
#
# Run INSIDE the new VM as the desktop user (e.g. aiw, in the sudo group):
#   git clone --depth 1 https://github.com/hackjown/ai-workstation.git ~/ai-workstation && VM_AGENT_TOKEN='...' VNC_PASSWORD='...' bash ~/ai-workstation/deploy/vm/setup-min.sh
# Optional: GEOMETRY=1600x900 (default)   NO_OFFICE=1 (skip LibreOffice)
set -euo pipefail
: "${VM_AGENT_TOKEN:?set VM_AGENT_TOKEN (same as AIW_VM_AGENT_TOKEN in the API .env)}"
: "${VNC_PASSWORD:?set VNC_PASSWORD (the noVNC password)}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
U=$(id -un); H=$HOME; UID_=$(id -u); GEOMETRY=${GEOMETRY:-1600x900}
APT="sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends"

. /etc/os-release
[ "${ID:-}" = debian ] || echo "warning: written for Debian 13, found ${PRETTY_NAME:-unknown}"

sudo apt-get update
# 1) desktop core: just enough XFCE for windows, a panel and a file manager
$APT xfce4-session xfwm4 xfce4-panel xfdesktop4 xfce4-settings xfconf thunar xfce4-terminal \
  tigervnc-standalone-server tigervnc-tools dbus-x11 at-spi2-core adwaita-icon-theme \
  novnc websockify qemu-guest-agent ca-certificates
# 2) agent tools + document stack
$APT python3 python3-venv python3-pip git curl ffmpeg xdotool x11-utils x11-xserver-utils wmctrl scrot xclip \
  imagemagick poppler-utils fonts-noto-cjk fonts-noto-color-emoji librecad chromium \
  python3-openpyxl python3-docx python3-matplotlib python3-bs4 python3-ezdxf
# 3) office (no Java, no extra language packs beyond Chinese)
if [ -z "${NO_OFFICE:-}" ]; then
  $APT libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 libreoffice-l10n-zh-cn
fi
sudo systemctl enable --now qemu-guest-agent 2>/dev/null || true

# Chromium policy: no translate bubble, no password prompts
sudo mkdir -p /etc/chromium/policies/managed
echo '{"TranslateEnabled": false, "PasswordManagerEnabled": false}' | sudo tee /etc/chromium/policies/managed/aiw.json >/dev/null

# 4) vm-agent venv + LibreCAD hanzi font and 1:50 template
python3 -m venv "$H/.aiw-venv"
"$H/.aiw-venv/bin/pip" install -q -U pip
"$H/.aiw-venv/bin/pip" install -q -r "$REPO/vm-agent/requirements.txt" fonttools
"$H/.aiw-venv/bin/python" -m playwright install chromium || true
python3 -m pip install -q --user --break-system-packages python-pptx || true
mkdir -p "$H/workspace/downloads" "$H/.config/aiw" "$H/.config/systemd/user"

printf '%s\n' "$VNC_PASSWORD" | vncpasswd -f > "$H/.config/aiw/tigervnc.pass"
chmod 600 "$H/.config/aiw/tigervnc.pass"
umask 077
cat > "$H/.config/aiw/vm-agent.env" <<ENV
VM_AGENT_TOKEN=$VM_AGENT_TOKEN
VM_AGENT_BACKEND=x11
VM_AGENT_FILES_ROOT=$H/workspace
VM_AGENT_WORKDIR=$H/workspace
PYTHONPATH=$REPO/protocols:$REPO/vm-agent
DISPLAY=:1
ENV
umask 022

# 5) virtual screen :1 + XFCE session, both at boot without any login
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
sudo tee /etc/systemd/system/aiw-xfce.service >/dev/null <<UNIT
[Unit]
Description=AI Workstation XFCE session on :1
After=aiw-xvnc.service
Requires=aiw-xvnc.service

[Service]
User=$U
Environment=DISPLAY=:1
Environment=HOME=$H
Environment=XDG_RUNTIME_DIR=/run/user/$UID_
Environment=XDG_SESSION_TYPE=x11
Environment=XDG_CURRENT_DESKTOP=XFCE
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 30); do xdpyinfo -display :1 >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart=/usr/bin/dbus-launch --exit-with-session /usr/bin/startxfce4
ExecStartPost=-/bin/sh -c 'sleep 3; xset -display :1 s off; xset -display :1 s noblank; exit 0'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# 6) vm-agent + noVNC as user services (linger: they start at boot)
sed "s#@REPO@#$REPO#g; s#@HOME@#$H#g; s#^After=graphical-session.target#After=default.target#" \
  "$REPO/deploy/vm/vm-agent.service" > "$H/.config/systemd/user/vm-agent.service"
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
sudo loginctl enable-linger "$U"
sudo systemctl set-default multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now aiw-xvnc aiw-xfce
export XDG_RUNTIME_DIR=/run/user/$UID_
systemctl --user daemon-reload
systemctl --user enable --now vm-agent novnc

# LibreCAD font/template needs the screen up (it restarts LibreCAD)
sleep 5
DISPLAY=:1 bash "$REPO/deploy/vm/librecad-setup.sh" || true
xfconf-query --display :1 -c xfwm4 -p /general/use_compositing -n -t bool -s false 2>/dev/null || true

echo
echo "Done. Virtual desktop :1 ($GEOMETRY) runs at boot without any login."
echo "  vm-agent  ws://<VM_IP>:8765   noVNC  http://<VM_IP>:6080/vnc.html"
echo "Check: systemctl status aiw-xvnc aiw-xfce --no-pager; systemctl --user status vm-agent novnc --no-pager; free -m"
echo "Firewall: sudo apt-get install -y ufw && sudo ufw allow 22/tcp && sudo ufw allow from <API_HOST_IP> to any port 8765,6080 proto tcp && sudo ufw enable"
