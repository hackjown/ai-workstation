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

## v3.9.33 模型供应商列表

设置 › 模型 › **供应商**：除了原来的「网关地址 + API Key」（现在叫「默认」供应商，id `default`，原样保留、不迁移），可以再加多个 OpenAI 兼容供应商
（名称、类型、地址、Key、代理、启用；「测试连接」读一次 `/models`）。快速添加：百炼 `https://dashscope.aliyuncs.com/compatible-mode/v1`、
Gemini `https://generativelanguage.googleapis.com/v1beta/openai`（国内需代理）、OpenAI `https://api.openai.com/v1`、DeepSeek `https://api.deepseek.com`。
实现在 `services/api/aiw_api/providers.py`（`/api/providers` 增删改、`/api/providers/{id}/test`）。

- 存储：kv `providers` = `[{id, name, kind, base_url, proxy, enabled}]`；每个供应商的 Key 是加密密钥 `provider_key_<id>`，接口只返回 `has_key`。
- **方案（唯一）**：kv `models` 里每个模型槽位旁边可选一个 `<槽位>_provider` 字段（供应商 id，空 = 默认），模型名照旧是纯模型名。
  槽位：`model`、`fallback`、`vision_model`、`vision_fallback`、`video_model`、`subagent_model`、`chat_model`、`small_model`、`embed_model`、
  `summary_model`、`geometry_model`、`image_model`、`image2_model`（选了就代替「生图接口」的地址/Key）、`omni_model`（轮流模式；实时接口 Key/地址留空时也用它）、
  `omni_tts_model`、`omni_gemini`（Gemini Key 留空时借用该供应商的 Key 和代理）。
- 不在槽位里的模型名（模型路由的档位、`autoroute.dev.model`）：同名槽位的供应商 → 只有某个供应商的模型列表里有它（默认列表里没有）→ 默认；
  `autoroute.dev.provider` 可显式指定。
- 模型下拉框按「供应商 · 模型」分组（`GET /api/models` 多了 `groups`，`models` 仍是默认供应商的列表）；选某组里的模型会同时选上该槽位的供应商；「探测」走该槽位的供应商。
- 删除/停用还在用的供应商会被拒绝（409，列出在用的设置）；强制删除（`?force=1`）把这些设置改回「默认」。kv 里残留的未知 id 一律回落到默认。
- 类型只有 `openai`：llm-router 的 GoogleProvider 太简陋（单次工具调用、无流式/思考/重试），Gemini 走它的 OpenAI 兼容地址。
- 代理：每个供应商可填 `http(s)://` 或 `socks5://`；socks 需要 `pip install 'httpx[socks]'`。

## v3.9.32 视频通话：Gemini Live 后端

通话多了第三种后端 `gemini`：服务端用 WebSocket 连 Gemini Live API（`BidiGenerateContent`，`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=…`），
原生语音进出、服务端 VAD 打断、输入/输出字幕、工具调用（`create_aiw_task` / `task_status`）、上下文压缩（slidingWindow，否则音视频会话约 2 分钟就断）、
goAway / 断线时用 sessionResumption 句柄自动重连（客户端无感）。客户端协议不变。

- 设置 › 模型 › 视频通话：模式多了「Gemini Live」；Gemini Key（密钥 `omni_gemini_key`，也读环境变量 `GEMINI_API_KEY`/`GOOGLE_API_KEY`，在 https://aistudio.google.com/api-keys 创建）、
  Gemini 模型 `omni_gemini_model`（默认 `gemini-3.8-live`）、Gemini 音色 `omni_gemini_voice`（默认 `Puck`，30 个预置音色）、出站代理 `omni_proxy`（`http://`、`https://`、`socks5://`；留空用 `HTTPS_PROXY`/`ALL_PROXY`）、
  中转地址 `omni_gemini_url`（反向代理/中转的 wss 地址，可选）。
- **规则**：「自动」模式下填了 Gemini Key 就先连 Gemini，不通再连实时接口、再轮流（后两者需要 `omni_model`）；「Gemini Live」模式只用 Gemini。只填 Gemini Key、不设通话模型也能通话。
- **国内**：直连 Google 不通，必须填出站代理或中转地址。socks 代理需要 websockets≥15 且装 `python-socks[asyncio]`；websockets 13/14 只支持 `http://` 代理（aiw 自己建 CONNECT 隧道）。
- 免费层可用，但免费层数据会被 Google 用于改进产品。

## v3.9.31 视频通话（全模态实时语音 + 摄像头）

聊天顶部的电话按钮（web；iOS 首页聊天右上角）打开全屏通话：说话、看摄像头画面（前/后摄像头、可关）、静音、点字幕打断、挂断。
实现在 `services/api/aiw_api/omni_call.py`，客户端 `apps/web/call.js`、`apps/ios/Sources/CallView.swift`。

- **设置 › 模型 › 通话模型（全模态）** `omni_model`：留空 = 通话关闭（按钮会提示去设置）。还有「视频通话」一组：音色 `omni_voice`（默认 Tina）、
  模式 `omni_mode`（自动/只用实时/只用轮流）、实时接口地址 `omni_realtime_url`、实时接口 Key（密钥 `omni_realtime_key`，留空用主网关 Key）、
  实时模型 `omni_realtime_model`（留空 = 通话模型名加 `-realtime`）、朗读模型 `omni_tts_model`、最长通话 `omni_max_minutes`（默认 30）。「探测」会试两种模式。
- **实时模式**：服务端连 OpenAI-Realtime 风格的 WebSocket（百炼 Qwen-Omni-Realtime：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=…`；
  `qwen3.8-omni-flash-realtime` 必须用业务空间地址 `wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime`），服务端 VAD、打断、工具调用。
- **轮流模式**（任何 OpenAI 兼容网关）：服务端能量 VAD 切句 → `chat/completions`（stream、modalities text+audio、input_audio wav + 最新一帧画面 + 最近几轮），
  音频流式回放，说话即打断。只出文字的模型（如 `qwen3.8-omni-flash`）自动降级：由朗读模型（如 `qwen3.5-omni-flash`）或手机/浏览器朗读。
- **自动**：先短超时连实时接口，连不上就用轮流模式，并告诉客户端当前模式。
- **工具**：只有 `create_aiw_task`（走聊天的正常建任务流程，任务自己的确认卡照常弹）和 `task_status`。挂断后整通对话作为一条消息写进小秘聊天。
- 限制：单帧 ≤256 KB、服务端 ≤1 帧/秒、消息限速、30 秒没消息挂断、同时最多 2 通（`AIW_OMNI_MAX_CALLS`）。

## v3.9.30 编码任务的 Codex / Claude Code 式循环

编码/开发/修复任务自动进入（不用手动选）：选了「开发 程序」技能（【技能：dev】）、请求是写程序/网站/工具、对已有代码的修复/修改（含「继续」），或模型路由判为开发任务（长任务继续且对话里有代码）。实现在 `services/agent-runtime/agent_runtime/devloop.py`。

- **精简工具**：`read_file`（带行号，offset/limit）、`grep`、`glob`、`files_patch`（精确唯一替换，0 次/多次匹配会说明怎么改）、`shell`、`todo_write`、`done(outcome=done|nothing|failed|ask)` 完成回执。开发任务里写 `fix_*.py` 之类改文件的补丁脚本会被拒绝并指向 files_patch。
- **先探索后修改**：修改已有文件前必须先 read_file/grep 读过它；没有源头的修复把第一个读过并修改的源文件定为源头；直接改生成产物会被拒绝并指出源头。全新项目（dev 技能的 PLAN.md → 原型截图 → ask_user → 实现流程）不受限制：新建文件、改本项目 ~/workspace 下的文件都不拦。
- **TODO**：从任务契约的验收项（含需求检查）生成；同时只能有一项 in_progress；标 done 前必须在最后一次修改相关文件之后跑过验证（测试/生成器/verify_html/diff），否则拒绝；需求检查通过、改后重新生成会自动勾掉。done 时附「完成回执」，总结里声称完成但没验证的项会标 ⚠。
- **生成器**：有 `generator_cmd_fast` 时，迭代必须先用快速命令并验证通过才允许完整运行；源头没改动不重复生成。
- **上下文压缩**：滑出 25 行窗口的旧步骤并入「工作记忆」（读/改过的文件、决定、错误、未完成 TODO），可用便宜模型（flash）总结；保存到 kv `task_devmem:<任务>`，「继续」时恢复，不从头再来。
- **卡住**：同一错误签名出现 3 次 → 回到探索阶段（重新读代码前不能改）；超过步数预算（`AIW_DEV_STEP_BUDGET` 默认 100，新项目 `AIW_DEV_STEP_BUDGET_NEW` 默认 250）→ 带回执询问用户继续还是停止。
- **模型分工**：开发步骤用 `autoroute.dev.model`（强模型，未配置时照旧警告）；flash 只用于压缩总结（`models.summary_model` → flash 主模型 → 路由小模型）。
- **安全**：危险命令黑名单（rm -rf ~ / /、git reset --hard、git clean、git push、sudo、curl|sh 等）直接拒绝；编码任务不弹确认框，本需确认的命令以工具错误拒绝（项目内 pip/npm 安装除外，直接执行）。`AIW_DEV_NO_CONFIRM=0` 恢复确认框，`AIW_DEV_BLOCK_FIX_SCRIPTS=0` 允许补丁脚本，`AIW_DEV_GEN_GATE=0` 关闭生成器门禁。

## v3.9.29 开发/修复流程修正（task-68b）
- **模型路由**：修复代码（dev_fix）任务按顺序选模型：`autoroute.dev.model`（设置 › 模型路由 › 修复代码）→ `models.dev_model` →
  `models.strong_model` → `AIW_DEV_MODEL` → 困难档模型 → 主模型是 flash/lite/mini 类时先用 `models.fallback`（如 deveco-glm-5.3）→ 主模型；
  候选里非 flash 的优先，思考始终开、推理强度默认高。时间线上的「模型路由」写明来源设置；仍落到 flash 时显示 ⚠ 警告并写日志。
  flash 只留给普通档/聊天。**建议在 `autoroute.dev.model` 填一个强代码模型。**
- **files_patch 工具**（动作 `files.patch`）：精确字符串替换，`count` 指定期望匹配次数（默认 1，0=全部），不匹配不改文件并报告实际次数，
  返回 unified diff。任务契约里的源文件/目录在 ~/workspace 之外也能改（由系统按契约放行，模型不能自己打开；密钥/.ssh 等路径永远拒绝；
  vm-agent 允许的根目录 `VM_AGENT_PATCH_ROOTS`，默认 ~）。改契约源文件的脚本不再触发「临时补丁脚本」警告，改为提示用 files_patch。
- **写了不执行**：.py/.sh 写好后 2 步内没执行，系统点名提醒（执行命令或改用 files_patch）；第二个带同样替换内容的脚本直接拒绝；
  这些都计入卡住检测。
- **源头识别**：请求里的裸文件名（「做进 aiw_news.py」）按本线程以前运行过的脚本（`cd X && python3 Y.py`、shell 的 cwd）解析成完整路径；
  继续/重试沿用上一轮契约的源头、生成命令和交付物；已知生成脚本时生成的 .html 等产物不进允许路径；交付的是 `cp` 出来的副本时追溯到原生成脚本。
  源文件未被 git 跟踪、不在仓库里、或在 aiw 平台自身仓库里时，不建分支，改为把源文件复制到 `~/.aiw/snapshots/<任务>/`（`AIW_SNAPSHOT_DIR`），
  「回滚」把快照拷回（当前文件留作 `.aiw-before-rollback`）。
- **卡住提示具体化**：dev_rabbit 卡片/提示写明具体模式（N 个脚本写了没执行 / 同一处修改重复 N 次 / 在几种改法之间来回切换）；出现严重模式
  （≥4 个未执行脚本、同一修改 ≥4 次）立即询问。事实板：同一句话数字变了视为更新（旧条目被替代），源文件改动后关于它的行数/大小等事实自动过期。
- **需求验收检查**：契约从用户原话生成检查项（深色/浅色随系统切换、指定字体、配图数量、新增栏目/#id 区块、返回首页按钮、文件大小、不内嵌字体），
  verify_html 自动执行（结果在 `checks[]`，不影响页面本身的 ok）；done 必须：最后一次改动后跑过检查、未通过项列入「已知问题」并写原因、
  总结不能声称已完成一个检查未通过的项、每项检查都要在总结里写明结果。交付总结末尾自动附「需求检查」清单。
- **生成器快速迭代**：契约字段 `generator_cmd_fast` —— 生成脚本源码里支持 `--from-cache`/`--cached`/`--use-cache`/`--offline`/`--no-fetch`/
  `--fast`/`--skip-fetch` 之一时，系统自动给出带该参数的命令，迭代时用它（不重新抓取网络）；done 之前必须完整运行一次 `generator_cmd`
  并验证其产物，否则拒绝 done。生成脚本想享受这个能力，需要自己实现其中一个参数（读上次抓取的缓存）。

## v3.9.28 手机快通道（App）
- iOS / Android 设备桥新增 phone_call、sms_send、reminders_delete（仅 iOS）、calendar_today，hello 里声明 phone.call / sms.send；过了 expires_at 的调用直接丢弃；收到唤醒推送（aiw://open）或回到前台立即连 /ws/device。
- 首页聊天里「打电话给X / 我有什么提醒 / 今天有什么安排」直接在手机上处理（通讯录多个号码弹选择，拨号前本地确认），结果写进聊天记录（POST /api/chat/device_result）。没匹配或没权限照旧发给服务器。Android 没有系统提醒事项，提醒类仍走服务器。
- 任务卡片 fast_path=="device" 时不显示电脑进度条和「接管」，改标「手机」；"escalated" 照常显示。

## v3.9.28 手机快通道（后端）

只用手机就能完成的请求（提醒事项、日历/日程、通讯录、打电话、短信、闹钟、快捷指令、位置、健康）不再走 LLM 路由、不选/不唤醒云电脑、
不排在忙碌的电脑后面：任务立即开始，在 API 进程内用一个小工具循环（只有 device_* 工具 + ask_user + escalate_to_computer）完成，
写入类操作照常弹确认卡。提到文件/浏览器/网页/代码/附件的请求照旧交给电脑；手机做不了时 escalate_to_computer 在同一任务里
转到电脑继续，手机已返回的结果带进上下文。新设备工具：phone_call、sms_send、reminders_complete、reminders_delete、calendar_today。
设备待执行命令持久化（重启后仍会送达）。新接口 POST /api/chat/device_result。
环境变量：AIW_PHONE_FAST=0 关闭；AIW_DEVICE_FAST_TTL（默认 8 秒，先推送唤醒，超时返回「手机 App 未连接/不在前台」）。

## 安全设计（§19、§30）
- Agent 不持有 PVE 凭据；PVE Token 只在 VM Runtime，且仅限 VM 100 的电源/快照权限。
- 所有工具调用必经 Tool Router → Policy Engine：L4（root、碰 PVE、关机、改防火墙、写裸盘）直接拒绝；L3（删除/覆盖工作区以外或你上传的文件、安装软件、sudo、上传/外发数据、`git push`、`curl|sh`、ssh/scp、内网地址、敏感文件、付款意图、白名单外域名）弹窗等你确认；L2 执行并审计。只读命令、在新文件夹里转换文件、删除本任务自己建的临时文件夹不再打扰你。`AIW_AUTO_INSTALL=1` 让 pip/npm 安装恢复免确认（apt/sudo 仍要确认）。
- 确认卡片说人话：服务器把每一步翻译成标题、要做什么、影响（文件/应用/网站/电脑）、风险标签（低/中/高）和为什么；原始命令收在「查看技术细节」里。可选 `AIW_CONFIRM_LLM=1` 让模型润色标题（超时 `AIW_CONFIRM_LLM_TIMEOUT` 秒，失败用规则版）。按钮：允许 / 拒绝 / 本任务内都允许（付款、发布除外）。接管请求写明在哪台电脑、哪个网站、你要做什么，带「打开桌面接管」「完成，交还给 Aiw」「取消」。
- VM Agent 以普通用户运行，文件操作限制在 `~/workspace`，Shell 有超时和输出上限。
- 推荐运行 `deploy/vm/headless-desktop.sh`：用 TigerVNC 在开机时创建独立虚拟桌面 :1，不依赖 PVE 控制台登录，也不会因为控制台黑屏而黑屏。
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

## 表情助手 · 球球（第三方，非商用）
对话标题旁和电脑卡片上的「球球」表情小球来自 [sam70361/aora-bot](https://github.com/sam70361/aora-bot) 的 `emotion-ball/`（暗黑主题），原样放在 `apps/web/vendor/emotionball/`，附原作者 `LICENSE` / `NOTICE.md`。**其角色视觉形象仅供个人学习与研究，禁止任何商业用途**；引擎与表情数据非商业免费、商用须向原作者取得授权。商业用途请删除该目录并在 设置 › 关于 › 表情助手 关闭，或换成自有形象。

## V1 之后
Windows VM（参考 Dexbox 的 Desktop 接口做 `WindowsComputerProvider`）→ 剪映 / PS / PR → Video Agent；实时桌面升级 WebRTC。新后端只加 Provider，不改 Agent Runtime。
