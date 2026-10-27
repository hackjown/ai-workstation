#!/usr/bin/env bash
# Update this computer's AIW vm-agent to the newest code on main and restart it, so Aiw sees what is installed
# (for example BrowserOS in 设置 › 电脑 › BrowserOS › 重新检测). Run as the desktop user, in a terminal or over ssh:
#   curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/deploy/vm/update-agent.sh | bash
# Works when ~/ai-workstation is a git clone (pull) or a plain copy (refreshed from the main tarball).
# Needs no sudo when vm-agent is a systemd *user* service (setup-vm.sh) or runs from aiw-cloud.sh (cloud boxes).
set -uo pipefail
R="${AIW_REPO:-$HOME/ai-workstation}"
V="$HOME/.aiw-venv"
S="$HOME/.config/aiw"
TARBALL="${AIW_TARBALL:-https://codeload.github.com/hackjown/ai-workstation/tar.gz/main}"
say() { printf '== %s\n' "$*"; }

say "更新 $R"
if [ -d "$R/.git" ]; then
  git -C "$R" pull -q --ff-only 2>/dev/null || { git -C "$R" fetch -q --depth 1 origin main && git -C "$R" reset -q --hard origin/main; } \
    || { echo "ERROR: repo: git could not update $R"; exit 1; }
  echo "   $(git -C "$R" log -1 --format='%h %ci')"
else
  T=$(mktemp -d); mkdir -p "$R"
  curl -fsSL --retry 3 "$TARBALL" | tar xz -C "$T" --strip-components=1 || { rm -rf "$T"; echo "ERROR: repo: could not download $TARBALL"; exit 1; }
  cp -a "$T"/. "$R"/ && rm -rf "$T"
  echo "   copied main into $R (not a git clone)"
fi

say "Python 依赖"
[ -x "$V/bin/python" ] || { echo "ERROR: venv: $V is missing (run the setup script first)"; exit 1; }
"$V/bin/pip" install -q -r "$R/vm-agent/requirements.txt" 2>&1 | tail -n 3

say "这台电脑上报的 BrowserOS"
( cd "$R" && PYTHONPATH="$R/protocols:$R/vm-agent" "$V/bin/python" -c \
  'import json; from vm_agent.heartbeat.heartbeat import health; print("   " + json.dumps(health("x11").get("browseros")))' ) || true

say "重启 vm-agent（用上新代码）"
if [ -f "$S/aiw-cloud.sh" ] && [ -f "$S/vm-agent.env" ] && ! systemctl --user cat vm-agent >/dev/null 2>&1; then
  cp -f "$R/deploy/cloud/aiw-cloud.sh" "$S/aiw-cloud.sh"
  rm -f "$S/vm-agent.env.running"
  VM_AGENT_TOKEN=$(sed -n 's/^VM_AGENT_TOKEN=//p' "$S/vm-agent.env") bash "$S/aiw-cloud.sh" ensure
elif systemctl --user cat vm-agent >/dev/null 2>&1; then
  # --no-block: a terminal the agent opened lives in its unit, so this script must not wait on the restart
  systemctl --user --no-block restart vm-agent && echo "   vm-agent restarting (systemd user service)"
elif systemctl cat vm-agent >/dev/null 2>&1; then
  sudo systemctl --no-block restart vm-agent && echo "   vm-agent restarting (system service)"
else
  echo "   没找到 vm-agent 服务，请手动重启它"
fi
echo "完成。回到 Aiw 设置 › 电脑 › BrowserOS 点「重新检测」。"
