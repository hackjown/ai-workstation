#!/usr/bin/env bash
# One-line install / upgrade of the AI Workstation API host (Docker).
#   curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/scripts/install.sh | bash
# Safe to re-run: keeps your .env and data, only adds new settings and pulls the latest image.
set -euo pipefail
DIR=${AIW_DIR:-$HOME/docker/aiw}
RAW=https://raw.githubusercontent.com/hackjown/ai-workstation/main
mkdir -p "$DIR/data" && cd "$DIR"
rand() { head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-40}"; }
ask() { local v; if [ -t 0 ] || [ -e /dev/tty ]; then read -r -p "$1 [$2]: " v </dev/tty || true; fi; echo "${v:-$2}"; }

[ -f docker-compose.yml ] || curl -fsSL "$RAW/docker-compose.yml" -o docker-compose.yml

if [ ! -f .env ]; then
  echo "== 首次安装：生成 .env =="
  curl -fsSL "$RAW/.env.example" -o .env
  VM_IP=$(ask "Agent 虚拟机 IP" "192.168.50.104")
  VM_ID=$(ask "Agent 虚拟机 PVE VMID" "104")
  sed -i "s|^AIW_API_TOKEN=.*|AIW_API_TOKEN=$(rand 40)|; s|^VM_AGENT_TOKEN=.*|VM_AGENT_TOKEN=$(rand 40)|; \
          s|^AIW_VM_IP=.*|AIW_VM_IP=$VM_IP|; s|^AIW_VM_ID=.*|AIW_VM_ID=$VM_ID|; s|^AIW_SECRET_KEY=.*|AIW_SECRET_KEY=$(rand 48)|" .env
  chmod 600 .env
  echo "!! 把 .env 里的 VM_AGENT_TOKEN 填到虚拟机的 ~/.config/aiw/vm-agent.env，并补全 PVE_* 和模型 Key（也可以在网页 设置 > 模型 里填）"
else
  echo "== 升级：保留现有 .env，只补充新配置 =="
  add() { grep -q "^$1=" .env || echo "$1=$2" >> .env; }
  add AIW_SECRET_KEY "$(rand 48)"
  add AIW_TZ Asia/Shanghai
  add AIW_RESUME_INTERRUPTED true
  add AIW_ASK_TIMEOUT 600
  add AIW_CLONE_SNAPSHOT clean
fi

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
$DC pull
$DC up -d --force-recreate
PORT=$(grep -oE '"[0-9]+:8000"' docker-compose.yml | head -1 | tr -d '"' | cut -d: -f1)
TOKEN=$(grep '^AIW_API_TOKEN=' .env | cut -d= -f2-)
echo "== 完成：http://$(hostname -I | awk '{print $1}'):${PORT:-8000}/?token=$TOKEN"
