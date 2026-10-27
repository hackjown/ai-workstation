#!/usr/bin/env bash
# Put an AIW computer back after a snapshot restore: tools, LibreCAD, WorkBuddy/CodeBuddy/REA/OfficeCLI, video kit,
# and the desktop icons. Run in the VM as the desktop user (it asks for the sudo password once):
#   curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/restore-apps.sh | bash
#   bash restore-apps.sh icons     only the desktop icons
set -u
R="$HOME/ai-workstation"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
say() { printf '\n== %s\n' "$*"; }

icons() {
  say "桌面图标"
  D=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop"); mkdir -p "$D"
  for f in /usr/share/applications/librecad.desktop /usr/share/applications/*[Ww]ork[Bb]uddy*.desktop \
           /usr/share/applications/libreoffice-writer.desktop /usr/share/applications/xfce4-terminal.desktop; do
    [ -f "$f" ] || continue
    t="$D/$(basename "$f")"; cp -f "$f" "$t"; chmod +x "$t"
    # XFCE only runs launchers it trusts
    command -v gio >/dev/null && gio set "$t" metadata::xfce-exe-checksum "$(sha256sum "$t" | cut -d' ' -f1)" 2>/dev/null
    gio set "$t" metadata::trusted true 2>/dev/null || true
    echo "   + $(basename "$f")"
  done
  pgrep -u "$(id -u)" xfdesktop >/dev/null && DISPLAY=${DISPLAY:-:0} xfdesktop --reload 2>/dev/null || true
}

if [ "${1:-}" = "icons" ]; then icons; exit 0; fi

$SUDO -v || { echo "需要 sudo 密码"; exit 1; }
say "更新 ~/ai-workstation"
if [ -d "$R/.git" ]; then git -C "$R" pull -q --ff-only || git -C "$R" fetch -q --depth 1 origin main && git -C "$R" reset -q --hard origin/main
else git clone -q --depth 1 https://github.com/hackjown/ai-workstation.git "$R"; fi
V="$R/deploy/vm"

say "常用工具（Office/PDF/OCR/音视频…，要几分钟）"; bash "$V/toolbox-setup.sh"
say "LibreCAD"; command -v librecad >/dev/null || $SUDO apt-get install -y -q librecad fonts-noto-cjk; bash "$V/librecad-setup.sh"
say "WorkBuddy / CodeBuddy / REA / OfficeCLI"; bash "$V/devagents-setup.sh"
say "视频工具（huashu-motion）"; bash "$V/motion-setup.sh" || echo "   视频工具没装全，之后可单独重跑 motion-setup.sh"
# BrowserOS: reinstalled when it was there before the restore marker, or asked for (AIW_BROWSEROS=1)
if [ "${AIW_BROWSEROS:-0}" = 1 ] || [ -f "$HOME/.config/autostart/browseros.desktop" ] || [ -d "$HOME/.config/browser-claw" ]; then
  say "BrowserOS"; bash "$V/browseros-setup.sh" install || echo "   BrowserOS 没装上（只支持 amd64），之后可单独重跑 browseros-setup.sh"
fi
icons
say "重启 vm-agent（用上新代码）"; systemctl --user restart vm-agent 2>/dev/null || $SUDO systemctl restart vm-agent 2>/dev/null || echo "   没找到 vm-agent 服务，跳过"
say "检查"; bash "$V/devagents-setup.sh" check
echo; echo "完成。WorkBuddy 和 codebuddy 第一次打开需要你自己登录一次（快照恢复后登录状态也一起回退了）。"
