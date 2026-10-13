# AI Workstation V1.1

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

## V1.1 新功能

界面改为浅色、圆角、天蓝/灰气泡的对话式设计（参考 OpenMuse 的视觉语言），网页、iPhone 主屏 Web App 和 IPA 共用一套。

**对话体验**
- 多轮对话：同一对话里可以追问（"再把标题改大"），Agent 会带上前几轮的请求和结果。
- 排队：Agent 工作时继续发消息会排队，按顺序执行；停止会暂停队列，点"发送排队消息"继续，单条可移除。
- 发送/停止合一：工作中发送键变成停止方块，草稿按对话保存在本机。
- 先出计划：每个任务开始时生成 2–5 步清单，Agent 用 `update_plan` 打勾。
- 历史与回放：左侧对话列表（可重命名、归档），每个任务可逐步回放截图，查看操作收据（每条命令、风险等级、耗时、交付文件）。

**可靠性**
- 失败、停止或中断的任务一键重试；API 重启时，30 分钟内被中断的任务自动重新排队。
- `ask_user`：缺信息时 Agent 在对话里提问（可带选项），你回答后继续。
- 通知：任务完成/失败/需要确认/需要回答、追踪触发都会进通知中心，并可推送到 Bark（iOS）、ntfy 或 Webhook。

**自动化与记忆**
- 例程：`daily 09:00`、`every 2h` 或 cron（`0 9 * * 1-5`），到点自动执行，结果进对应对话。
- 追踪：网页变化 / 出现文字 / 价格低于阈值，只在状态变化时提醒一次，读取失败自动指数退避。
- 记忆与个人资料：称呼、Agent 名字、语气、长期事实；Agent 听到长期偏好会用 `remember` 记下，可在设置里编辑删除。
- 附件：网页上传文件（≤40MB）直接放到虚拟机 `~/workspace/uploads/`，配合 `send_file` 做"传入 → 处理 → 交回"。

**平台**
- 子 Agent：`delegate` 把纯命令行子任务交给不看截图的文本模型（设置 > 模型 > 子 Agent 模型）。
- 网页里切换主模型 / 备用模型 / 网关地址 / API Key（加密保存），可读取网关的模型列表。
- 一次性隔离电脑：勾选后下个任务在 PVE 完整克隆（来自快照 `AIW_CLONE_SNAPSHOT`）里运行，结束即删除。需要 qemu-guest-agent 和额外的 PVE 权限：`VM.Clone`、`VM.Allocate`、`VM.Config.*`、`Datastore.AllocateSpace`。
- 集成：MCP 服务器（Streamable HTTP）、OpenAPI 导入、自定义 HTTP 工具，在 API 主机上执行，请求头可用 `{{secret:名字}}` 引用加密密钥。
- 语音：浏览器听写 + 朗读结果；IPA 内用系统语音识别（SFSpeechRecognizer）和本地通知。
- 一键安装/升级：`curl -fsSL https://raw.githubusercontent.com/hackjown/ai-workstation/main/scripts/install.sh | bash`（保留现有 `.env`，只补新配置）。

**升级（VM 102）**：`cd ~/docker/aiw && docker-compose pull && docker-compose up -d --force-recreate`。数据库自动迁移，旧任务保留在"活动"里。

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
