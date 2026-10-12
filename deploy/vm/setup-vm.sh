#!/usr/bin/env bash
# Run INSIDE the Ubuntu 24.04 / Debian 12 VM as the desktop user (aiw), with sudo.
#   VM_AGENT_TOKEN=... VNC_PASSWORD=... ./setup-vm.sh /path/to/ai-workstation
# Installs XFCE, Chrome, Python, Node, FFmpeg, Playwright, xdotool, x11vnc, noVNC,
# qemu-guest-agent, and the vm-agent + desktop streaming systemd services (spec Phase 1/2/4).
set -euo pipefail
REPO=${1:-$HOME/ai-workstation}
: "${VM_AGENT_TOKEN:?set VM_AGENT_TOKEN}"
: "${VNC_PASSWORD:?set VNC_PASSWORD (x11vnc must never run without auth)}"
USER_NAME=$(id -un)

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  xfce4 xfce4-goodies lightdm lightdm-gtk-greeter \
  python3 python3-venv python3-pip git curl ffmpeg xdotool x11-utils wmctrl scrot \
  x11vnc novnc websockify qemu-guest-agent fonts-noto-cjk
sudo systemctl enable --now qemu-guest-agent

# Google Chrome (falls back to apt/snap chromium if download fails)
if ! command -v google-chrome >/dev/null; then
  curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && sudo apt-get install -y /tmp/chrome.deb \
    || sudo apt-get install -y chromium || sudo snap install chromium
fi
# Node.js 22 LTS
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs
fi

# X11 session (not Wayland) + autologin into XFCE so there is always a desktop
sudo mkdir -p /etc/lightdm/lightdm.conf.d
sudo tee /etc/lightdm/lightdm.conf.d/50-aiw.conf >/dev/null <<CONF
[Seat:*]
autologin-user=$USER_NAME
autologin-session=xfce
user-session=xfce
CONF
sudo systemctl set-default graphical.target
# Ubuntu Desktop ships GNOME on Wayland via gdm3; switch to lightdm + XFCE on X11
echo /usr/sbin/lightdm | sudo tee /etc/X11/default-display-manager >/dev/null
sudo systemctl disable gdm3 2>/dev/null || true
sudo systemctl enable lightdm --force
# disable screen lock / blanking
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/noblank.desktop <<CONF
[Desktop Entry]
Type=Application
Name=noblank
Exec=sh -c "xset s off; xset -dpms; xset s noblank"
CONF
xfconf-query -c xfce4-screensaver -p /saver/enabled -n -t bool -s false 2>/dev/null || true

# Python env for vm-agent
python3 -m venv ~/.aiw-venv
~/.aiw-venv/bin/pip install -U pip
~/.aiw-venv/bin/pip install -r "$REPO/vm-agent/requirements.txt"
~/.aiw-venv/bin/python -m playwright install chromium || true
mkdir -p ~/workspace/downloads

# VNC password file (x11vnc refuses unauthenticated clients)
mkdir -p ~/.config/aiw
x11vnc -storepasswd "$VNC_PASSWORD" ~/.config/aiw/vncpass
umask 077
cat > ~/.config/aiw/vm-agent.env <<ENV
VM_AGENT_TOKEN=$VM_AGENT_TOKEN
VM_AGENT_BACKEND=x11
VM_AGENT_FILES_ROOT=$HOME/workspace
VM_AGENT_WORKDIR=$HOME/workspace
PYTHONPATH=$REPO/protocols:$REPO/vm-agent
DISPLAY=:0
ENV

# user-level systemd services (start after the graphical login)
mkdir -p ~/.config/systemd/user
sed "s#@REPO@#$REPO#g; s#@HOME@#$HOME#g" "$REPO/deploy/vm/vm-agent.service" > ~/.config/systemd/user/vm-agent.service
sed "s#@HOME@#$HOME#g" "$REPO/deploy/vm/x11vnc.service" > ~/.config/systemd/user/x11vnc.service
cp "$REPO/deploy/vm/novnc.service" ~/.config/systemd/user/novnc.service
sudo loginctl enable-linger "$USER_NAME"
systemctl --user daemon-reload
systemctl --user enable vm-agent x11vnc novnc

echo
echo "Done. Reboot the VM. After autologin:"
echo "  vm-agent  ws://<VM_IP>:8765   (token-protected)"
echo "  noVNC     http://<VM_IP>:6080/vnc.html  (VNC password)"
echo "Firewall these ports so only the API host can reach them, e.g.:"
echo "  sudo ufw allow from <API_HOST_IP> to any port 8765,6080 proto tcp && sudo ufw enable"
