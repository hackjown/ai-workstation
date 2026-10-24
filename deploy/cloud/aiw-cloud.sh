#!/usr/bin/env bash
# Turns a cloud machine (InstaBox agent-browser: Debian, sudo, Xvnc desktop on :1) into an AI Workstation
# computer: the same vm-agent, LibreCAD + Chinese font and tools as the Proxmox VM (deploy/vm/setup-vm.sh).
# The AIW API runs it through the provider's exec API; nothing here needs systemd.
#   aiw-cloud.sh install   long: apt, repo, venv, LibreCAD font; runs in the background, see `status`
#   aiw-cloud.sh ensure    fast, idempotent: screen size + vm-agent running (after a stop/start or a reboot)
#   aiw-cloud.sh status    one line: INSTALLING <step> | INSTALLED | FAILED <step> | NONE
# env: VM_AGENT_TOKEN (required for ensure), AIW_REPO_URL, AIW_SCREEN (default 1600x900), AIW_DISPLAY (:1)
set -u
REPO_URL=${AIW_REPO_URL:-https://github.com/hackjown/ai-workstation.git}
R=$HOME/ai-workstation
V=$HOME/.aiw-venv
S=$HOME/.config/aiw
ST=$S/cloud-install.status
LOG=$S/cloud-install.log
DISP=${AIW_DISPLAY:-:1}
SCREEN=${AIW_SCREEN:-1600x900}
mkdir -p "$S" "$HOME/workspace/downloads"

step() { echo "INSTALLING $1" > "$ST"; echo "== $1" >> "$LOG"; }

do_install() {
  : > "$LOG"
  step apt
  if ! command -v librecad >/dev/null || ! command -v xclip >/dev/null; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq >>"$LOG" 2>&1
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git curl xdotool x11-utils x11-xserver-utils \
      wmctrl scrot fonts-noto-cjk librecad xclip >>"$LOG" 2>&1 || { echo "FAILED apt" > "$ST"; return 1; }
  fi
  step repo
  if [ -d "$R/.git" ]; then git -C "$R" pull -q --ff-only >>"$LOG" 2>&1 || true
  else git clone -q --depth 1 "$REPO_URL" "$R" >>"$LOG" 2>&1 || { echo "FAILED repo" > "$ST"; return 1; }; fi
  step venv
  [ -x "$V/bin/python" ] || python3 -m venv "$V" >>"$LOG" 2>&1
  "$V/bin/pip" install -q -U pip >>"$LOG" 2>&1
  "$V/bin/pip" install -q -r "$R/vm-agent/requirements.txt" fonttools >>"$LOG" 2>&1 || { echo "FAILED venv" > "$ST"; return 1; }
  step librecad
  DISPLAY=$DISP bash "$R/deploy/vm/librecad-setup.sh" >>"$LOG" 2>&1 || true
  step browser
  "$V/bin/python" -m playwright install chromium >>"$LOG" 2>&1 || true
  echo INSTALLED > "$ST"
}

screen_size() {
  export DISPLAY=$DISP
  xdpyinfo >/dev/null 2>&1 || return 0
  cur=$(xdpyinfo | awk '/dimensions:/{print $2}')
  [ "$cur" = "$SCREEN" ] && return 0
  w=${SCREEN%x*}; h=${SCREEN#*x}
  out=$(xrandr | awk '/ connected/{print $1; exit}')
  if ! xrandr | grep -q "^ *$SCREEN "; then
    ml=$(cvt "$w" "$h" 60 2>/dev/null | sed -n 's/^Modeline "[^"]*" *//p')
    [ -n "$ml" ] && xrandr --newmode "$SCREEN" $ml 2>/dev/null
    xrandr --addmode "$out" "$SCREEN" 2>/dev/null
  fi
  xrandr --output "$out" --mode "$SCREEN" 2>/dev/null || xrandr --fb "$SCREEN" 2>/dev/null || true
}

do_ensure() {
  : "${VM_AGENT_TOKEN:?VM_AGENT_TOKEN missing}"
  [ "$(cat "$ST" 2>/dev/null)" = INSTALLED ] || { echo "NOT_INSTALLED $(cat "$ST" 2>/dev/null)"; return 2; }
  umask 077
  cat > "$S/vm-agent.env" <<ENV
VM_AGENT_TOKEN=$VM_AGENT_TOKEN
VM_AGENT_BACKEND=x11
VM_AGENT_FILES_ROOT=$HOME/workspace
VM_AGENT_WORKDIR=$HOME/workspace
PYTHONPATH=$R/protocols:$R/vm-agent
DISPLAY=$DISP
ENV
  umask 022
  screen_size
  # restart when the token changed (a new API host) or the agent died
  if pgrep -f "vm_agent.server.websocket" >/dev/null && cmp -s "$S/vm-agent.env" "$S/vm-agent.env.running"; then
    echo AGENT_UP; return 0
  fi
  pkill -f "vm_agent.server.websocket" 2>/dev/null; sleep 1
  cp "$S/vm-agent.env" "$S/vm-agent.env.running"
  ( set -a; . "$S/vm-agent.env"; set +a; cd "$HOME/workspace" &&
    setsid nohup "$V/bin/python" -m vm_agent.server.websocket --host 0.0.0.0 --port 8765 >"$S/vm-agent.log" 2>&1 </dev/null & )
  for _ in $(seq 1 20); do
    ss -ltn 2>/dev/null | grep -q ':8765 ' && { echo AGENT_STARTED; return 0; }
    sleep 0.5
  done
  echo "AGENT_FAILED $(tail -3 "$S/vm-agent.log" 2>/dev/null | tr '\n' ' ')"; return 1
}

case "${1:-status}" in
  install)
    if grep -q '^INSTALLING' "$ST" 2>/dev/null && pgrep -f "aiw-cloud.sh install-run" >/dev/null; then cat "$ST"; exit 0; fi
    echo "INSTALLING start" > "$ST"
    setsid nohup bash "$0" install-run >/dev/null 2>&1 </dev/null &
    echo "INSTALLING start" ;;
  install-run) do_install ;;
  ensure) do_ensure ;;
  status) cat "$ST" 2>/dev/null || echo NONE; [ -f "$LOG" ] && grep -q FAILED "$ST" 2>/dev/null && tail -5 "$LOG" ;;
  *) echo "usage: $0 install|ensure|status"; exit 1 ;;
esac
