# AI Workstation V1

一台跑在自有 Proxmox VE 上、可被 AI 自主操作的 Linux 电脑。依据《AI Workstation V1 开发实施说明书》实现。

```
Web Console ─▶ API ─▶ Agent Runtime ─▶ LLM Router (Claude / GPT / Gemini / 兼容 OpenAI 的本地模型)
                         │
                         └▶ Tool Router (Policy L0–L4 + 审计) ─▶ Computer Runtime ─WebSocket─▶ VM Agent ─▶ Ubuntu 24.04 XFCE (PVE VM 100)
API ─▶ VM Runtime ─▶ Proxmox API（只有这一层碰 PVE）
浏览器 ◀─ noVNC ◀─ API 代理 ◀─ websockify ◀─ x11vnc（实时桌面 / 用户接管）
```

## 进度（对照说明书 §26）

| 阶段 | 状态 | 位置 |
|---|---|---|
| Phase 0 参考仓库研究 | ✅ | `docs/architecture/github-analysis.md`，`scripts/clone-references.sh` |
| Phase 1 PVE VM | 🟡 脚本就绪，需在你的 PVE 上执行 | `deploy/pve/create-vm.sh`，`deploy/vm/setup-vm.sh` |
| Phase 2 VM Agent | ✅ 全部 25 个 Action | `vm-agent/` |
| Phase 3 Computer Runtime | ✅ `LinuxComputerProvider`（只经 WebSocket） | `services/computer-runtime/` |
| Phase 4 Desktop Streaming | ✅ x11vnc + noVNC，经 API 代理 | `deploy/vm/*.service`，`/ws/desktop/{id}` |
| Phase 5 Agent Runtime | ✅ Observe→Decide→Policy→Execute→Validate→Recovery，暂停/接管/恢复 | `services/agent-runtime/` |
| Phase 6 PVE Provider | ✅ status/start/stop/restart/snapshot/restore（最小权限 Token） | `services/vm-runtime/` |
| Phase 7 完整 V1 | ✅ API + Web Console + 事件/审计/截图 Artifact | `services/api/`，`apps/web/` |

测试：`pytest` 共 19 项，全部在模拟桌面上通过（真 WebSocket、真 Agent Loop、真 API）。真机验收（§27 的 8 项）需要在你的 PVE VM 上跑。

## 部署

**1. PVE 宿主机（root）**
```bash
ISO=local:iso/ubuntu-24.04.1-desktop-amd64.iso ./deploy/pve/create-vm.sh   # VM 100 ai-linux-001, 4C/8G/50G VirtIO, vmbr0
./deploy/pve/create-api-token.sh                                            # aiw@pve!runtime，只对 /vms/100 有电源+快照权限
```
**2. VM 内（装好 Ubuntu 24.04 Desktop，用户 aiw）**
```bash
git clone <本仓库> ~/ai-workstation
VM_AGENT_TOKEN=<随机串> VNC_PASSWORD=<密码> ~/ai-workstation/deploy/vm/setup-vm.sh ~/ai-workstation
sudo reboot
```
然后在 PVE 上打一个干净快照：`qm snapshot 100 clean`。

**3. API 主机（推荐 Docker，可放在 PVE 上的 Docker VM）**
```bash
cp .env.example .env    # 填 AIW_VM_IP、两个 token、PVE token secret、至少一个 LLM key
docker compose up -d --build
docker compose logs -f   # 看启动日志
```
打开 `http://<Docker主机IP>:8000/?token=<AIW_API_TOKEN>`。iPhone 用 Safari 打开后「分享 → 添加到主屏幕」即可当 App 用（首次会要求输入 token）。

**3b. API 主机（不用 Docker）**
```bash
pip install -r requirements.txt
cp .env.example .env    # 填 AIW_VM_IP、两个 token、PVE token、至少一个 LLM key
./run-api.sh            # 打开 http://<host>:8000/?token=<AIW_API_TOKEN>
```

**Phase 2/3 手动验收**（不需要 LLM）
```bash
VM_AGENT_TOKEN=... python tools-cli.py ws://<VM_IP>:8765 screenshot shot.png
VM_AGENT_TOKEN=... python tools-cli.py ws://<VM_IP>:8765 click 500 300
```

## 安全设计（§19、§30）
- Agent 不持有 PVE 凭据；PVE Token 只在 VM Runtime，且仅限 VM 100 的电源/快照权限。
- 所有工具调用必经 Tool Router → Policy Engine：L4（root、碰 PVE、关机、改防火墙、写裸盘）直接拒绝；L3（`rm -rf`、批量删除、`curl|sh`、ssh/scp、内网地址、敏感文件、付款意图、白名单外域名）弹窗等你确认；L2 执行并审计。
- VM Agent 以普通用户运行，文件操作限制在 `~/workspace`，Shell 有超时和输出上限。
- vm-agent 需 Token，x11vnc 只监听 localhost 且必须有密码，6080/8765 端口建议只放行 API 主机。
- 每个动作写入 `machine_actions`，每一步事件写入 `agent_events`，截图存为 Artifact。

## 目录
```
apps/web/                  Web Console（桌面 + 任务 + 事件流 + 暂停/接管/恢复/确认）
protocols/aiw_protocols/   Command / Event / Task / VM 协议模型
vm-agent/vm_agent/         VM 内 Agent：server/ computer/ browser/ shell/ files/ heartbeat/
services/computer-runtime/ ComputerProvider + LinuxComputerProvider
services/vm-runtime/       VMProvider + ProxmoxProvider
services/tool-router/      registry / policy / executor
services/llm-router/       Anthropic / OpenAI(兼容) / Gemini，按序回退
services/agent-runtime/    loop / planner / validator / recovery / control
services/api/              FastAPI：REST + WebSocket + 桌面代理 + SQLite 存储
infrastructure/postgres/   等价的 Postgres DDL
deploy/                    PVE 建机、Token、VM 初始化与 systemd 服务
docs/                      github-analysis.md、协议说明
```

## V1 之后
Windows VM（参考 Dexbox 的 Desktop 接口做 `WindowsComputerProvider`）→ 剪映 / PS / PR → Video Agent；实时桌面升级 WebRTC。新后端只加 Provider，不改 Agent Runtime。
