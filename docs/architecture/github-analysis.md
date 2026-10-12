# AI Workstation V1 · Phase 0 参考项目分析（GitHub Analysis）

> 生成时间：2026-10-08（Asia/Shanghai）
> 分析对象均为 `git clone --depth 1` 浅克隆，位于 `/workspace/ai-workstation/references/`。
> License 结论全部来自仓库内实际的 LICENSE 文件 / LICENSING.md / Cargo.toml / package.json，**非法律意见**；商用前请法务复核。
> 本文只引用路径与极短片段，不复制大段代码。

| 目录 | 上游 | 快照 commit | 体积 |
|---|---|---|---|
| `references/cua` | https://github.com/trycua/cua | `a0394a6`（2026-10-07） | ~499 MB |
| `references/e2b-open-computer-use` | https://github.com/e2b-dev/open-computer-use | `610bac8`（2026-07-09） | ~0.9 MB |
| `references/dexbox` | https://github.com/getnenai/dexbox | `08f9cee`（2026-04-21） | ~1.3 MB |
| `references/open-computer-use` | https://github.com/lineCode/open-computer-use（LLmHub-dev 项目的 fork） | `79a13bc`（2025-10-11） | ~25 MB |
| `references/openhands` | https://github.com/OpenHands/OpenHands | `6e81571`（2026-10-07） | ~30 MB |
| `references/openhands-software-agent-sdk` | https://github.com/OpenHands/software-agent-sdk（**额外克隆**，见 §5） | `69e2688`（2026-10-07） | — |

五个指定仓库全部一次克隆成功，无失败。

---

## 0. 关键结论速览

1. **Cua 已大幅重构**：旧的 `computer` / `computer-server`（8000 端口 WebSocket）Python 包已不存在；现在是 Rust 的 `cua-driver`（MCP stdio，桌面自动化）+ `cua-spacesd`（gRPC/gRPC-Web，3211 端口，FSL）+ `cua` SDK。`cua-agent` Python 包已标注 **Deprecated**。
2. **Cua 的 License 是混合的**：根目录 MIT，但 `cua-spacesd`、流媒体（`cua-media-*`）、Spaces App 等为 **FSL-1.1-MIT**（禁止做竞争性托管服务，2 年后转 MIT）；`libs/python/som` 为 **AGPL-3.0-or-later**；`cua-perception` 扩展含 **AGPL-3.0-only** 的 OmniParser 图标检测模型；OmniParser 仓库本身声明 **CC-BY-4.0**。我们是“托管计算机使用平台”，正好落在 FSL 的 Competing Use 范围内 → **FSL 部分一律不可用**。
3. **E2B open-computer-use** 很小（~1000 行 Python），核心价值是“vision 模型描述屏幕 → action 模型出 tool call → grounding 模型定位坐标”的三模型 Agent Loop，Apache-2.0。
4. **OpenHands 主仓库现在只是 Agent Canvas 前端（React/TS）**；Agent Runtime、Event、Tool、Security、Agent Server 都在 `OpenHands/software-agent-sdk`（MIT），因此额外浅克隆了它。
5. **lineCode/open-computer-use** 是 Next.js + FastAPI + Docker(XFCE+TigerVNC+noVNC) 的完整“产品形态”，Supabase schema 中的 `user_machines` / `machine_sessions` / `machine_ai_actions` 表结构对我们很有参考价值；但安全上较弱（VNC 密码放 URL、无密码时放行、含反爬 stealth/代码混淆），且 Apache-2.0 文本未声明具体版权人。
6. **Dexbox** 是 Go 写的 Windows（VirtualBox / RDP + guacd）工具服务器，Apache-2.0，`Desktop` 接口 + 多模型 tool-call 适配器设计干净，**仅作 V2 Windows 参考**。
7. 对 PVE 方案最直接有用的“可读代码”：Cua `cua-vmm` 的 QMP / cloud-init NoCloud seed / 无 Agent VNC 截图（MIT）、Cua `libs/xfce` 的 TigerVNC+noVNC+supervisord 镜像脚本（MIT）、OpenHands SDK 的 Event/Tool/Confirmation/StuckDetector（MIT）。

---

## 1. trycua/cua

### 1.1 目录结构（顶层）
```
apps/            cua-spaces（Tauri）、cua-spaces-macos（SwiftUI）——FSL
libs/
  cua/           Rust 工作区：crates/*（cua-sdk、cua-cli、cua-vmm、cua-proto、cua-media-*…）、proto/、python/、typescript/、swift/、kotlin/
  cua-driver/    跨平台桌面自动化驱动（Rust + Python/TS 绑定），MCP stdio
  cua-spacesd/   沙箱内守护进程（gRPC/gRPC-Web/MCP/媒体流）——FSL
  python/        agent（已弃用）、core、cua-sandbox、cua-sandbox-apps、som（AGPL）、bench-ui、cua-train
  xfce/ xfce-cua/ kasm/ qemu-docker/   桌面容器镜像
  lume/ lumier/  macOS 上的 Apple Virtualization VM 管理（Swift）
  cua-bench*/ cua-s1/   评测与小模型
  typescript/ fleet/ images/ ...
docs/ rfcs/ samples/ examples/ infra/ clusters/ nix/ scripts/ tests/
LICENSE.md  LICENSING.md  COMMERCIAL.md  TRADEMARKS.md  CLA.md
```

### 1.2 核心模块
| 模块 | 路径 | 作用 |
|---|---|---|
| Cua Driver | `libs/cua-driver/rust/crates/cua-driver-core/src/` | 工具注册、MCP wire、session、capture registry、policy、recording、browser(CDP) 等 |
| 平台实现 | `libs/cua-driver/rust/crates/platform-linux/src/` | X11（`x11rb` + XTest/XInput/RandR/SHM）、Wayland（wlr 协议）、AT-SPI 无障碍树（`atspi`/`zbus`）、剪贴板、窗口/输入投递 |
| 驱动契约 | `libs/cua-driver/contract/manifest.json` | 29 个工具：`click`、`drag`、`scroll`、`type_text`、`press_key`、`hotkey`、`move_cursor`、`get_screen_size`、`get_cursor_position`、`list_windows`、`get_window_state`、`set_window_frame`、`list_apps`、`clipboard_read/write`、`start_session/end_session`、`verify_state`… |
| 权限策略 | `libs/cua-driver/rust/crates/cua-driver-core/src/policy.rs`、`authorization.rs`、`consent.rs` | YAML / Rego 策略引擎，对 MCP tool call 做 Allow/Deny；三种权限模式 `standard` / `bounded`（manifest 白名单）/ `unrestricted` |
| VMM | `libs/cua/crates/cua-vmm/src/` | `qemu/qmp.rs`、`qemu/args.rs`、`cloudinit.rs`（自写 NoCloud ISO）、`vnc.rs`（通过 VMM 的 VNC 端口无 Agent 截图，基于 `vnc-rs`）、`disk.rs`、`container/`、`lume/` |
| 沙箱 SDK | `libs/python/cua-sandbox/cua_sandbox/` | `interfaces/`（mouse、keyboard、screen、shell、files、window、clipboard、tunnel、driver…）、`runtime/`（docker、qemu、lume、hyperv、tart、android_emulator） |
| 沙箱守护进程 | `libs/cua-spacesd/`（FSL） | `cua.env.v1` 协议服务端 |
| 协议定义 | `libs/cua/proto/cua/env/v1/*.proto`（MIT） | `ComputerService`（Screenshot/Pointer/Keyboard/Clipboard/GetCursorPosition/ListDisplays）、Process、Filesystem、Windows、Accessibility、Stream… |
| 旧 Agent | `libs/python/agent/cua_agent/`（已弃用） | `agent.py`（~1100 行）、`loops/`（anthropic、openai、uitars、qwen3vl、omniparser 等 20+ 种）、`callbacks/`（budget_manager、image_retention、pii_anonymization、trajectory_saver…）、`computers/base.py`（83 行的最小 Computer 接口） |

### 1.3 入口
- `cua-driver mcp`（stdio MCP）、`cua-driver serve`（守护进程）、`cua-driver call <tool>`（CLI）。
- `cua-spacesd`（单二进制，`0.0.0.0:3211`，需 `CUA_ENV_TOKEN`）。
- Python：`from cua_sandbox import Sandbox`；旧：`python -m cua_agent`。
- 镜像：`libs/xfce/Dockerfile` → supervisord 启动 `dhclient` / `vncserver` / `novnc` / `spacesd`。

### 1.4 协议
- **MCP**：cua-driver 的主要边界；支持 MCP `2026-07-28`（modern）及 legacy `2025-06-18` initialize 流（`libs/cua-driver/docs/mcp-protocol-and-skills.md`）。HTTP 仅为有限支持（`private-envelope-http.md`）。
- **gRPC / gRPC-Web**：cua-spacesd 在 3211 端口提供 `cua.env.v1`；`/mcp` 为 Streamable-HTTP MCP；`/files` 为 HMAC 签名 URL；`/tunnel`、`/hotspot` 为带票据的 WebSocket。
- **媒体**：`/media` + QUIC 3212，H.264 视频 + Opus 音频（media wire v2，`libs/cua/proto/MEDIA.md`），自研 RCDP——**FSL**。
- 认证：`authorization: Bearer <token>`，常量时间比较；非 loopback 绑定强制需要 token（见 `libs/cua-spacesd/README.md`）。

### 1.5 VM 方式
- 本地：Docker/Podman（可选 gVisor）、QEMU（`cua-vmm`）、Lume（macOS Apple Virtualization）。
- 云：自家 Fleet（gVisor / KubeVirt）、AWS/GCP/Modal BYOC。
- `libs/qemu-docker/{linux,windows,android}`：在 Docker 中跑 QEMU/KVM 的桌面 OS。
- **没有 Proxmox VE 支持**；但 `cua-vmm` 的 QMP、cloud-init seed、VNC 截图思路可直接映射到 PVE（PVE 本身就是 QEMU + QMP + cloud-init + vncproxy）。

### 1.6 Agent Loop
- 旧 `cua-agent`（弃用）：`ComputerAgent` + 可插拔 loop（按模型名路由到 `loops/anthropic.py` 等）+ callbacks 链（预算、图片保留数量、PII 脱敏、轨迹保存、OTel）。`loops/composed_grounded.py` 实现“规划模型 + grounding 模型”组合。
- 新方向：Cua 不再提供 Agent Loop，推荐“任意编码 Agent 通过 MCP 连 cua-driver”。

### 1.7 Desktop Streaming
- `libs/xfce`：TigerVNC `:1`（5901）+ noVNC `novnc_proxy`（6901）——经典方案，**MIT**。脚本：`libs/xfce/src/scripts/start-vnc.sh`、`start-novnc.sh`、`resize-display.sh`、`xstartup.sh`；`src/supervisor/supervisord.conf`。注意 `start-vnc.sh` 在未设置 `VNC_PW` 时使用 `-SecurityTypes None`（不安全，需我们改为强制认证）。
- `libs/kasm`：基于 `kasmweb/core-ubuntu-jammy`（KasmVNC），MIT（含 Kasm Technologies 部分版权）；Dockerfile 关闭了 SSL、设置了固定 sudo 密码——不可照搬。
- Cua Spaces：H.264/Opus over QUIC/WebSocket + WebCodecs HTML5 viewer（`cua-spacesd-html5`）——**FSL，禁用**。

### 1.8 License（已核实）
- 根 `LICENSE.md`：**MIT**，Copyright (c) 2025 Cua AI, Inc.
- `LICENSING.md` 列出的例外：
  - **FSL-1.1-MIT**：`apps/cua-spaces*`、`libs/cua-spacesd/`（含 cua-relay）、`libs/cua/crates/{cua-spaces-app-core, cua-keyvault, cua-teleport, cua-teleport-bundle, cua-machine-seal, cua-volume, cua-spaces-ext, cua-spaces-ffi, cua-spaces-cli, cua-media-protocol, cua-media-transport, cua-media-client, cua-media-codec, cua-viewer, cua-spacesd-html5, cua-logging}`、`libs/spaces-app-swift`、部分 `samples/*`。FSL 禁止 “Competing Use”（把它或实质相似的东西作为商业产品/服务提供给他人）；`COMMERCIAL.md` 明确“hosted desktops, sandboxes or computer-use environments”需商业授权。
  - **AGPL-3.0-or-later**：`libs/python/som/`（OmniParser 相关，已弃用）。
  - **AGPL-3.0-only**：`libs/cua-driver/rust/crates/cua-perception/models/` 中的 OmniParser v2.0 图标检测模型（源自 Ultralytics YOLO）及打包的 Ultralytics 源码快照；PP-OCRv5 模型为 Apache-2.0；ONNX Runtime 为 MIT（见 `models/THIRD_PARTY_NOTICES.md`）。cua-driver 本体仍为 MIT，perception 扩展需单独安装。
  - **CC-BY-4.0**：根 README “Licensing” 段说明 Microsoft OmniParser 仓库内容声明 CC-BY-4.0；模型文件需按具体 revision 另行核实。
  - `libs/kasm/LICENSE`：MIT（含 “Portions Copyright (c) 2022 Kasm Technologies Inc.”）。
  - `libs/python/cua-sandbox/THIRD_PARTY_NOTICES.md`：`builder/windows_unattend.py` 部分派生自 quickemu、Mido（MIT）。
- 仓库有 `scripts/check-license-direction.py` 强制 “MIT 不依赖 FSL”。
- `TRADEMARKS.md`：Cua 名称/Logo 为商标，我们不得使用。

### 1.9 可复用文件（MIT，可阅读借鉴，必要时带版权声明复制）
- `libs/cua/crates/cua-vmm/src/qemu/qmp.rs`、`qemu/args.rs`、`cloudinit.rs`、`vnc.rs`、`disk.rs` —— PVE 驱动层思路（QMP、NoCloud seed、无 Agent VNC 截图）。
- `libs/cua/proto/cua/env/v1/computer.proto`、`process.proto`、`filesystem.proto`、`windows.proto`、`accessibility.proto` —— Computer Runtime API 命名与字段设计参考（MIT，`cua-proto` 明确保持 MIT）。
- `libs/cua-driver/contract/manifest.json` + `libs/cua-driver/docs/action-result-contract.md`、`tool-output-format.md`、`driver-permission-modes-and-consent-plan.md` —— 工具清单、结果格式、权限模式。
- `libs/cua-driver/rust/crates/cua-driver-core/src/policy.rs` —— YAML/Rego tool-call 策略引擎设计。
- `libs/cua-driver/rust/crates/platform-linux/src/{x11/, input/, capture.rs, a11y.rs, clipboard.rs, xauth.rs}` —— Linux 输入/截图/无障碍实现参考。
- `libs/xfce/Dockerfile`、`libs/xfce/src/scripts/*.sh`、`libs/xfce/src/supervisor/supervisord.conf` —— VM 内桌面栈（XFCE + TigerVNC + noVNC + supervisord）。
- `libs/python/agent/cua_agent/computers/base.py`（最小 Computer 接口）、`callbacks/budget_manager.py`、`callbacks/image_retention.py`、`callbacks/trajectory_saver.py`、`loops/anthropic.py`（Anthropic computer-use 适配，量大，读思路）。
- `libs/python/cua-sandbox/cua_sandbox/interfaces/*.py`、`runtime/qemu.py`、`runtime/base.py` —— 运行时抽象。

### 1.10 不可复用文件
- 全部 FSL 目录（见 1.8），尤其 `libs/cua-spacesd/**`、`libs/cua/crates/cua-media-*`、`cua-spacesd-html5`、`cua-viewer`、`apps/**`。
- `libs/python/som/**`（AGPL）、`libs/cua-driver/rust/crates/cua-perception/models/**` 及其 AGPL 模型/源码快照、任何 OmniParser 权重。
- `libs/kasm/Dockerfile` 中的不安全配置（固定 sudo 密码、关闭 SSL）——不照搬。
- Cua 名称、Logo（商标）。

### 1.11 依赖风险
- 迭代极快（快照为 2026-10-07），API/包名频繁变动（computer-server → spacesd，cua-agent 弃用），**不要直接依赖其 Python/Rust 包**，只借鉴设计。
- MIT 与 FSL 在同一工作区，若引入 `cua` SDK 的 extras 可能间接带入 FSL 组件（需按 `LICENSING.md` 逐包核对）。
- cua-driver 的 Linux 实现依赖 X11/Wayland/AT-SPI 会话环境，在 VM 中需正确的 DISPLAY/XAUTHORITY/DBus。
- Rust 工具链 + UniFFI 绑定，构建成本高；V1 若用 Python 实现 VM Agent，仅借鉴工具语义即可。

---

## 2. e2b-dev/open-computer-use

### 2.1 目录结构
```
main.py                       CLI 入口（poetry run start）
os_computer_use/
  sandbox_agent.py   (215 行) Agent Loop + 工具
  llm_provider.py    (246 行) OpenAI/Anthropic 兼容的 provider 基类、tool schema 转换
  providers.py       (89 行)  各厂商 provider（Groq/Fireworks/OpenRouter/Gemini/OpenAI/Anthropic/Mistral/Moonshot…）
  config.py          (20 行)  选择 grounding / vision / action 三个模型
  grounding.py, osatlas_provider.py, showui_provider.py   视觉定位（HF Spaces 上的 OS-Atlas / ShowUI，经 gradio-client）
  streaming.py       (65 行)  E2B Sandbox 子类：ffmpeg x11grab → mpegts；DisplayClient（ffplay）
  browser.py         (77 行)  pywebview 本地窗口打开 VNC URL
  logging.py + templates/log.html   HTML 日志
tests/
```

### 2.2 核心模块 / 入口
- 入口：`main.py` → `Sandbox()`（`e2b_desktop`）→ `SandboxAgent(sandbox).run(instruction)`；`sandbox.stream.start()` 获取 noVNC URL 并在本地 webview 打开。

### 2.3 协议
- 与沙箱之间：E2B SDK（`e2b`、`e2b-desktop`，HTTP/gRPC，云端托管）。无自定义 HTTP/WS 服务。
- 与 LLM：OpenAI / Anthropic 兼容 API 的 function calling。

### 2.4 VM 方式
- E2B 云端 Firecracker microVM（Desktop Sandbox 模板，Ubuntu + Xfce）；本仓库不含 VM 管理代码。

### 2.5 Agent Loop（本仓库最大价值）
`os_computer_use/sandbox_agent.py::SandboxAgent.run`：
1. 把 `OBJECTIVE: ...` 追加到 messages；
2. 每轮 `sandbox.set_timeout(60)` 续命；
3. **Vision 步**：截图 + 固定提示（“目标是… / 屏幕上看到… / 目标是否完成 / 下一步是…”）让 vision 模型产出 THOUGHT；
4. **Action 步**：把历史 + THOUGHT 交给 action 模型，带 tools（`run_command`、`run_background_command`、`send_key`、`type_text`、`click`、`double_click`、`right_click`、`stop`）；
5. 点击类工具参数是**自然语言 query**，由 **grounding 模型**（OS-Atlas/ShowUI）在最新截图上返回坐标，再 `move_mouse` + `left_click`；
6. 工具结果作为 `OBSERVATION` 追加；遇到 `stop` 结束。
- 特点：模型解耦（规划/视觉/定位分离），工具用装饰器 `@tool(description, params)` 注册；缺点：无最大步数、无错误重试策略、无并发、`run_command` 超时 5 秒、无安全审批。

### 2.6 Desktop Streaming
- 主路径：`e2b_desktop` 的 `sandbox.stream`（E2B 内置 noVNC）。
- 备用（已注释）：`streaming.py` 中 `ffmpeg -f x11grab ... -f mpegts -listen 1 http://localhost:8080`，客户端 ffplay 播放并落盘 mp4——可借鉴为“会话录像”。

### 2.7 License（已核实）
- `LICENSE`：**Apache-2.0**，“Copyright 2024 FoundryLabs, Inc.”。
- 依赖 `e2b-desktop`（上游 `e2b-dev/desktop` 的 LICENSE 为 Apache-2.0，已通过 raw.githubusercontent.com 核实）。
- OS-Atlas / ShowUI 模型通过 HuggingFace Spaces 远程调用，模型本身许可需单独核实（未在本仓库内声明）。

### 2.8 可复用文件
- `os_computer_use/sandbox_agent.py` —— Agent Loop 结构（vision→action→grounding→observation），可按 Apache-2.0 改写（保留 NOTICE/版权并标注修改）。
- `os_computer_use/llm_provider.py` —— 多 provider 的 tool schema 归一化（`create_function_schema`、`wrap_block` 图片/文本块）。
- `os_computer_use/grounding.py`（在截图上画点，便于调试/审计）。
- `os_computer_use/streaming.py` 中 ffmpeg x11grab 录像命令思路。

### 2.9 不可复用文件
- `main.py`、`browser.py`（本地 pywebview，偏 demo）；`streaming.py` 的 Sandbox 子类绑定 E2B 云。
- `config.py` / `providers.py` 中的具体 API 端点与模型别名（易过期）。

### 2.10 依赖风险
- 强绑定 E2B 云（需 `E2B_API_KEY`），无法私有化；我们只取 Loop 设计。
- grounding 依赖 HF Spaces（gradio-client），有限流与可用性风险。
- 项目体量小、维护频率低（最近一次提交 2026-07-09）。

---

## 3. getnenai/dexbox（V2 Windows 参考）

### 3.1 目录结构
```
cmd/dexbox/            main.go（cobra CLI）、server.go
internal/
  server/server.go     HTTP 路由（:8600）
  desktop/             Desktop 接口、manager、vbox.go、rdp.go
  tools/               computer.go、bash.go、adapter*.go（Anthropic/OpenAI/Gemini 适配）、schema.go
  vbox/                VBoxManage CLI、SOAP（vboxwebsrv）、scancodes、autounattend.xml、NVRAM
  guacd/               guacd（Docker）管理
  mcpserver/           MCP server（go-sdk）
  web/static/          viewer.html + guacamole-common.min.js
agent/                 TS（Vercel AI SDK）与 Python（LangChain）参考 Agent
plugins/openclaw-dexbox/
```

### 3.2 核心模块 / 入口
- `dexbox create vm` / `start` / `up` / `view` / `run` / `mcp`；`dexbox start` 启动工具服务器（并拉起 vboxwebsrv、guacd）。
- `internal/desktop/desktop.go`：`Desktop` 接口（Connect/Disconnect/Screenshot/MouseClick/MouseMoveAbsolute/MouseScroll/MouseDown/Up/TypeText/KeyPress/Name/Type），VBox 与 RDP 两个实现。
- `internal/tools/adapter.go`：`CanonicalAction{Tool, Action, Params}` / `CanonicalResult` —— 先把各家模型原生 tool call 归一化，再执行，结果再转回该模型格式。

### 3.3 协议
- **HTTP**：`GET /tools`（模型无关 JSON Schema）、`POST /actions?model=...`（直接转发模型原生 tool call，支持 `Accept: image/png`）、`POST /actions/batch`、`/desktops` CRUD 与 `?action=up|down|pause|resume|suspend`、`/health`。
- **WebSocket**：`/desktops/{name}/tunnel`（Guacamole 协议到 guacd）；`/desktops/{name}/events`。
- **MCP**：`dexbox mcp`（stdio），只管理桌面生命周期（list/create/destroy/start/stop/status）。

### 3.4 VM 方式
- VirtualBox（headless，VBoxManage + SOAP 鼠标、scancode 键盘、guestcontrol 执行 PowerShell）、或外部 RDP 主机（guacd）。自动下载 ISO + `autounattend.xml` 无人值守安装 Windows 11。

### 3.5 Agent Loop
- 明确“**不是 Agent**，只是 tool server”；`agent/typescript-vercel-ai`、`agent/python-langchain` 是参考循环。

### 3.6 Desktop Streaming
- 浏览器内 Guacamole JS 客户端（`internal/web/static/viewer.html`）→ WS → guacd → RDP/VNC。

### 3.7 License（已核实）
- `LICENSE`：**Apache-2.0**，“Copyright 2026 NenAI, Inc.”。
- 内嵌 `guacamole-common.min.js`（Apache Guacamole 项目，通常为 Apache-2.0，文件内未见声明，需核实）。
- Go 依赖 `github.com/wwt/guac`、`github.com/deluan/bring`、`modelcontextprotocol/go-sdk` 等，许可证未在本仓库声明，引入前需逐个核实。

### 3.8 可复用文件（V2）
- `internal/desktop/desktop.go`（接口设计）、`internal/tools/adapter.go` + `adapter_anthropic.go` / `adapter_openai.go` / `adapter_gemini.go`（多模型 tool-call 归一化）、`internal/tools/computer.go`（坐标缩放 `scaleCoord`、截图 resize）。
- `internal/vbox/autounattend.xml`、`internal/vbox/scancodes.go`（Windows 无人值守与扫描码映射，PVE 上 Windows VM 也适用）。
- `internal/web/static/viewer.html`（Guacamole 前端接入示例）。

### 3.9 不可复用文件
- `internal/vbox/*` 的 VirtualBox SOAP/CLI 逻辑（我们用 PVE，不用 VBox）。
- `plugins/openclaw-dexbox/**`（第三方生态插件）。

### 3.10 依赖风险
- 依赖 VirtualBox + Docker(guacd)，与 PVE 栈不同；Windows 授权与 ISO 合规问题；Go 1.25。

---

## 4. lineCode/open-computer-use（LLmHub-dev 项目的 fork）

### 4.1 目录结构
```
app/                 Next.js 15 App Router
  api/machines/[id]/{route,screenshot,sessions,settings,status,vnc}/   机器 API
  api/chat/, api/credits/, api/subscription/, api/collaborative-rooms/ …
  components/machines/{novnc-viewer, simple-vnc-viewer, machine-viewer, ai-command-panel, file-transfer, sessions-list, usage-stats}.tsx
lib/
  docker/docker-service.ts       本地 Docker 机器管理
  azure/container-instances.ts   Azure ACI 机器管理
  machines-store.ts, machine-cleanup.ts, services/machine-cleanup.ts
  mcp/load-mcp-from-{local,url}.ts
  supabase/*, encryption.ts, csrf.ts
backend/ (FastAPI)
  app/services/multi_agent_executor.py (1769 行)  Planner + Browser/Terminal/Desktop 子 Agent
  app/services/vm_control.py (581 行)            与 VM 内 Agent 的 WebSocket 客户端（心跳/重连）
  app/api/routes/{chat, chat_vm_tools, vm_control, screenshots, file_operations}.py
  app/providers/*                                多 LLM provider
docker/ai-desktop/
  Dockerfile (Ubuntu 22.04 + XFCE + TigerVNC + noVNC/websockify + Chrome)
  ai_agent_server.py (5775 行)  VM 内 WebSocket Agent（8080），pyautogui/selenium
  vnc_startup.sh, novnc-config.sh, xstartup, health_check.sh, display_recovery.sh, keep-screen-alive.sh
  security-hardening.sh, stealth_browser.py, obfuscate.py
supabase/schema.sql   全部表结构
docker-compose.yml, docker-compose.ai-desktop.yml
```

### 4.2 核心模块 / 入口
- 前端 `npm run dev`（Next.js）；后端 `backend/main.py`（FastAPI）；VM 镜像入口 `docker/ai-desktop/entrypoint.sh` → `startup.sh` → VNC + noVNC + `ai_agent_server.py`。

### 4.3 协议
- 浏览器 ↔ Next.js：HTTP + 流式 chat（Vercel `ai` SDK）。
- 后端 ↔ VM Agent：**WebSocket JSON**（`ws://<ip>:8080`），消息 `{"type":"auth"|"ping"|...}`，命令 `execute_command(command, parameters)`；命令集合包括 `screenshot`、`click`、`double_click`、`right_click`、`type`、`key_press`、`key_combo`、`execute_command`、`terminal_*`、`file_*`、`directory_*`、`browser_*`（selenium/CDP）、`list_windows`/`switch_window`/`arrange_windows`…、`detect_elements`、`ocr`。
- 浏览器 ↔ 桌面：noVNC iframe（`vnc.html` + websockify 6080）。
- 支持加载 MCP（`lib/mcp/`）。

### 4.4 VM 方式
- “VM”实为 **Docker 容器**（本地 Docker 或 Azure Container Instances），非真正虚拟机。

### 4.5 Agent Loop
- `multi_agent_executor.py`：`plan_tasks()` 由 Planner 生成 `TaskPlan`（`SubTask` 列表，含依赖与 `AgentType` BROWSER/TERMINAL/DESKTOP），`get_next_executable_task()` 顺序执行，`stream_execution()` 流式输出，每个子任务结束生成 summary，最后 `_generate_final_report()`；工具输出统一截断（`_wrap_tools_with_truncation`）。

### 4.6 Desktop Streaming
- TigerVNC(5901) + noVNC/websockify(6080)，前端 `app/components/machines/novnc-viewer.tsx` 用 iframe 打开 `vnc.html?autoconnect=1&reconnect=1&resize=scale&view_only=...`；AI 控制会话时 `view_only=1`（人机互斥的简单做法）。
- 问题：**VNC 密码以 URL query 传递**、`encrypt=0`、直连公网 IP 端口；`ai_agent_server.py::authenticate_client` 在未设置密码时直接 `auth_success`（开发模式放行）。

### 4.7 数据表（`supabase/schema.sql`，对我们“机器/会话”建模很有价值）
- `user_machines`：`status ∈ {creating, starting, running, stopping, stopped, error, deleting}`、`vnc_password`、`vnc_port`、`websocket_port`、`cpu_cores`、`memory_gb`、`storage_gb`、`gpu_enabled`、`last_active_at`、`auto_shutdown_at`、`auto_shutdown_minutes`、`settings jsonb`（另有 Azure 专属字段）。
- `machine_sessions`：`session_type ∈ {ai_controlled, user_controlled, mixed}`、`ai_objective`、`ai_completion_status ∈ {pending, in_progress, completed, failed, cancelled}`、计数器（screenshots/commands/errors）。
- `machine_ai_actions`：`action_type`、`action_target`、`action_parameters jsonb`、`execution_time_ms`、`success`、`error_message`、`screenshot_before/after`、`ai_reasoning`、`status ∈ {pending, executing, completed, failed}`——几乎就是我们需要的“动作审计表”。
- `machine_limits`：按 tier 的配额（`max_running_machines`、`max_cpu_cores`、`allow_internet_access`、`allowed_domains[]`、`allow_snapshots`…）。
- `machine_snapshots`、`machine_usage`，以及与计费相关的 `user_credits`、`credit_transactions`、`stripe_*`、`subscription_plans`（与我们无关）。

### 4.8 License（已核实）
- `LICENSE`：**Apache-2.0** 标准全文，附录仍为模板 “Copyright [yyyy] [name of copyright owner]”，仓库内无 NOTICE 文件、无具体版权人声明（不影响授权效力；如复制代码，署名应按上游 LLmHub-dev 项目）。README 徽章亦为 Apache-2.0。
- 前端依赖 `@novnc/novnc ^1.6.0`（noVNC 本体为 MPL-2.0，需单独核实）；Dockerfile 中 `git clone` noVNC/websockify（websockify 为 LGPL-3.0，需核实）。
- 依赖 `undetected-chromedriver`、`selenium-stealth` 等反检测库。

### 4.9 可复用文件
- `supabase/schema.sql` 中 `user_machines` / `machine_sessions` / `machine_ai_actions` / `machine_limits` / `machine_snapshots` 的**字段设计**（我们用自己的 PostgreSQL schema 重写，不直接复制 Supabase RLS/函数）。
- `app/components/machines/novnc-viewer.tsx`、`machine-viewer.tsx`、`sessions-list.tsx`、`ai-command-panel.tsx` —— Web 桌面页 UI 交互参考（view_only 切换、重连参数、全屏）。
- `backend/app/services/vm_control.py` —— 后端到 VM Agent 的 WS 连接池、心跳、重连、超时模式。
- `docker/ai-desktop/vnc_startup.sh`、`xstartup`、`health_check.sh`、`display_recovery.sh`、`keep-screen-alive.sh` —— VM 内桌面保活/自愈脚本。
- `docker/ai-desktop/ai_agent_server.py` 中命令清单（作为 VM Agent API 覆盖面的 checklist，**不复制实现**）。
- `lib/machine-cleanup.ts` / `lib/services/machine-cleanup.ts` —— 空闲自动关机/清理逻辑。

### 4.10 不可复用文件
- `docker/ai-desktop/stealth_browser.py`、`test_anti_detection.py`、`obfuscate.py`（反爬/混淆，合规与安全风险）。
- `ai_agent_server.py` 的认证实现（无密码放行、明文密码比较）。
- 计费/Stripe/credits 全部模块、Supabase Auth 绑定、Azure ACI（`lib/azure/container-instances.ts`）。
- `novnc-viewer.tsx` 中把 `password` 放 URL 的做法。

### 4.11 依赖风险
- 这是 fork（最近提交 2025-10-11），上游活跃度与差异未知；README 的 clone 地址指向 `LLmHub-dev/open-computer-use`。
- Next.js 使用 canary 版本（`15.4.0-canary.47`），`ai@4`；Supabase 强绑定。
- `ai_agent_server.py` 单文件 5775 行，可维护性差。

---

## 5. OpenHands（OpenHands/OpenHands + OpenHands/software-agent-sdk）

### 5.0 重要说明
`OpenHands/OpenHands` 主仓库当前快照**只是 “Agent Canvas” React/TypeScript 前端**（`package.json` name `@openhands/agent-canvas`，v1.25.0）。其 `AGENTS.md` 的 “Repository Ownership” 表写明：Python SDK、Agent Server、agents/tools、conversations、events、REST/WebSocket API 均归 `OpenHands/software-agent-sdk`。旧的 `openhands/runtime`、`openhands/events`、`openhands/security` Python 目录已不在主仓库。因此为完成“agent runtime/events/tool/security”分析，额外浅克隆了 `software-agent-sdk` 到 `references/openhands-software-agent-sdk`（同样 depth 1，未删除 .git）。

### 5.1 目录结构
**OpenHands（Canvas 前端）**
```
src/api/         Agent Server / Cloud / settings / automations 适配器
src/components/  conversation、chat、browser、files、terminal(xterm)、settings、backend 管理
src/stores/      Zustand；src/hooks/ React Query
bin/agent-canvas.mjs  本地全栈启动器（agent-server + automation + ingress）
docker/ electron/ helm/ specs/ docs/{architecture.md, SELF_HOSTING.md, DefenseClaw.md}
```
**software-agent-sdk**
```
openhands-sdk/openhands/sdk/
  agent/        agent.py(1622 行，Agent.step)、parallel_executor.py、response_dispatch.py、acp_agent.py
  conversation/ impl/local_conversation.py(3174 行，run 循环)、impl/remote_conversation.py、state.py、event_store.py、stuck_detector.py、cancellation.py
  event/        base.py(Event / LLMConvertibleEvent)、llm_convertible/{action,observation,message,system}.py、condenser.py
  tool/         tool.py(ToolDefinition / ToolExecutor / ToolAnnotations)、registry.py、spec.py、schema.py
  security/     risk.py、confirmation_policy.py、analyzer.py、llm_analyzer.py、ensemble.py、shell_parser.py、defense_in_depth/{policy_rails,shell_semantics,pattern}.py
  workspace/    base.py、local.py、remote/
  mcp/ llm/ context/ hooks/ secret/ critic/ skills/ subagent/
openhands-tools/openhands/tools/  terminal、file_editor、apply_patch、browser_use（基于 browser-use）、glob、grep、task_tracker、delegate…
openhands-agent-server/openhands/agent_server/  FastAPI：conversation_router、event_router、sockets.py、bash_router、file_router、tool_router、auth_router、docker/Dockerfile…
openhands-workspace/openhands/workspace/  docker、apptainer、cloud、remote_api、agent_sandbox
clients/typescript/   TS 客户端
```

### 5.2 入口
- 前端：`npm install -g @openhands/agent-canvas && agent-canvas`；或 Docker `ghcr.io/openhands/agent-canvas`。
- 后端：`python -m openhands.agent_server --host 0.0.0.0 --port 8000`；SDK：`Agent(llm, tools)` + `Conversation(agent, workspace).run()`。

### 5.3 协议
- **REST**：`/api/conversations`（创建、搜索、发送消息、run、pause、confirm…）、文件、bash、git、skills、MCP、settings 等 router。
- **WebSocket**：`/events/{conversation_id}`（事件流）、`/bash-events`、`/session/{conversation_id}`（`sockets.py`、`session_socket.py`），认证用 `x-session-api-key` 头或 query，失败 close code 4001。
- **MCP**：SDK 内置 MCP 客户端（`sdk/mcp/`），服务器侧 `mcp_router.py` + OAuth store。
- **ACP**（Agent Client Protocol）：可接入 Claude Code、Codex、Gemini 等第三方 Agent（`agent/acp_agent.py`）。
- Webhooks、Telemetry（默认 `none`）。

### 5.4 VM 方式
- 工作区抽象 `BaseWorkspace`：Local / Docker / Apptainer / Remote API / Cloud；Canvas 支持 `OH_CONVERSATION_RUNTIME=docker` 每个会话一个容器。**无 GUI 桌面/VNC**（本快照在 agent-server、workspace、tools 中未找到 vnc/xvfb），浏览器能力走 `browser_use` 工具（headless Chromium + 截图/录制）。

### 5.5 Agent Loop
- `LocalConversation.run()`：循环调用 `agent.step()`，`max_iteration_per_run` 默认 500；每步后检查 `ConversationExecutionStatus`（`idle / running / paused / waiting_for_confirmation / finished / error / stuck / deleting`，见 `conversation/state.py`）。
- `Agent.step()`：把事件历史转换为 LLM 消息 → LLM 返回 tool calls → 生成 `ActionEvent`（含 `security_risk`）→ 若 `confirmation_policy.should_confirm(risk)` 则进入 `WAITING_FOR_CONFIRMATION`，否则执行 → `ObservationEvent` / `AgentErrorEvent` / `UserRejectObservation`。
- `ParallelToolExecutor`（`agent/parallel_executor.py`）：同一批 tool calls 并行执行。
- `StuckDetector`（`conversation/stuck_detector.py`）：重复 action-observation、重复错误、独白、交替模式四类卡死检测。
- Condenser（`event/condenser.py` 与 `context/`）：长上下文压缩。

### 5.6 Desktop Streaming
- 无 VNC；Canvas 有 browser 面板（截图）、terminal（xterm.js over WS）、文件、VS Code（`vscode_router.py`）。

### 5.7 Security 架构（我们 tool-router policy 的主要参考）
- `security/risk.py`：`SecurityRisk = UNKNOWN / LOW / MEDIUM / HIGH`，带 `is_riskier()` 比较。
- `security/confirmation_policy.py`：`AlwaysConfirm`、`NeverConfirm`、`ConfirmRisky(threshold=HIGH, confirm_unknown=True)`。
- `security/analyzer.py`（`SecurityAnalyzerBase`）+ `llm_analyzer.py`（让 LLM 在 tool call 里自报风险）+ `ensemble.py`（多分析器组合）+ `toolshield_*`、`grayswan/`（第三方检测）。
- `security/defense_in_depth/`：`policy_rails.py`、`shell_semantics.py`、`pattern.py`——基于 shell AST（`_shell_ast.py`、`shell_parser.py`）的命令语义规则。
- Agent Server 侧：`_secret_redaction.py`、`_secrets_exposure.py`、`secret_registry.py`（输出中的密钥脱敏）、`OH_SECRET_KEY` 加密持久化。

### 5.8 License（已核实）
- `OpenHands/OpenHands/LICENSE`：**MIT**，“Copyright © 2025 OpenHands contributors”；仓库内仅此一个 LICENSE 文件（本快照中**没有**旧版的 `enterprise/` 非 MIT 目录）。
- `software-agent-sdk/LICENSE`：**MIT**，“Copyright (c) 2026 OpenHands contributors”；`clients/typescript/LICENSE` 亦存在（MIT）。
- 依赖：`browser-use>=0.8.0`（openhands-tools），其许可需单独核实；前端依赖 `posthog-js` 等。

### 5.9 可复用文件（MIT）
- `openhands-sdk/openhands/sdk/event/base.py`、`event/llm_convertible/action.py`、`observation.py` —— Event 模型（Action/Observation/Error/Reject）。
- `openhands-sdk/openhands/sdk/tool/tool.py`、`tool/registry.py`、`tool/spec.py` —— Tool 定义（Action/Observation 类型化、`ToolAnnotations`、`DeclaredResources`）。
- `openhands-sdk/openhands/sdk/security/{risk.py, confirmation_policy.py, analyzer.py, ensemble.py}`、`security/defense_in_depth/*` —— 风险分级 + 确认策略。
- `openhands-sdk/openhands/sdk/conversation/{state.py, stuck_detector.py, event_store.py, cancellation.py}`、`conversation/impl/local_conversation.py::run` —— 循环/状态机/卡死检测/取消。
- `openhands-sdk/openhands/sdk/agent/parallel_executor.py`、`agent/agent.py::step` —— 单步语义。
- `openhands-agent-server/openhands/agent_server/{sockets.py, event_service.py, pub_sub.py, conversation_service.py, _secret_redaction.py}` —— 事件推送 WS、认证、脱敏。
- OpenHands Canvas：`src/api/`（与 Agent Server 交互的类型化适配层思路）、`docs/SELF_HOSTING.md`（加固建议）。

### 5.10 不可复用文件
- Canvas 前端整体（React Router + HeroUI 的大型应用，与我们 noVNC 桌面形态差异大），Electron、Helm、PostHog 遥测。
- `security/grayswan/`、`toolshield_*`（依赖外部服务）。
- ACP 相关（V1 不需要）。

### 5.11 依赖风险
- 架构刚拆分（Canvas / SDK / extensions / automation 四仓），API 演进快；Canvas 通过 `compatibility.minimumAgentServer` 管版本耦合。
- SDK 体量大（local_conversation.py 3000+ 行），直接依赖会把大量我们不需要的功能（ACP、skills、marketplace、critic）带进来——建议**借鉴结构自行实现精简版**。
- Python 3.13、Node 24。

---

## 6. License 总表

| 仓库 | 主 License（文件核实） | 需特别注意的第三方/子目录 | 我们的使用结论 |
|---|---|---|---|
| trycua/cua | MIT（`LICENSE.md`） | FSL-1.1-MIT（spacesd、media 流、Spaces 应用等，禁止竞争性托管服务）；AGPL-3.0-or-later（`libs/python/som`）；AGPL-3.0-only（cua-perception 的 OmniParser 模型/Ultralytics 快照）；CC-BY-4.0（OmniParser 仓库内容）；Kasm 部分版权（MIT）；商标 | **仅借鉴 MIT 部分的设计**；FSL/AGPL 目录禁止复制、禁止依赖；不使用 Cua 商标 |
| e2b-dev/open-computer-use | Apache-2.0（FoundryLabs, Inc. 2024） | e2b-desktop（Apache-2.0）；OS-Atlas/ShowUI 模型许可待核实 | 可改写借鉴，复制时保留版权与修改声明 |
| getnenai/dexbox | Apache-2.0（NenAI, Inc. 2026） | guacamole-common.min.js、wwt/guac、deluan/bring 许可待核实 | V2 Windows 参考，可借鉴 |
| lineCode/open-computer-use | Apache-2.0（未声明具体版权人、无 NOTICE） | noVNC（MPL-2.0，待核实）、websockify（LGPL，待核实）、反检测库 | 可借鉴表结构与 UI；不复制安全实现与 stealth/混淆代码 |
| OpenHands/OpenHands | MIT（2025 OpenHands contributors） | 无子目录例外 | 可借鉴 |
| OpenHands/software-agent-sdk | MIT（2026 OpenHands contributors） | browser-use 许可待核实 | 可借鉴，优先参考对象 |

---

## 7. 映射到我们的 V1 方案

V1 组成：**PVE Linux VM + VM Agent + Computer Runtime + Agent Loop + noVNC Web Desktop**（V2 再加 Windows）。

### 7.1 总体映射
| V1 模块 | 主要参考 | 借鉴什么 |
|---|---|---|
| Computer Runtime（driver 抽象） | **Cua**：cua-driver 工具契约 / `cua.env.v1` ComputerService / `cua-vmm` | 统一动作集合与结果格式；“VM 内 Agent 驱动” 与 “VMM 层无 Agent（VNC/QMP）兜底” 双通道；权限模式 |
| Agent Loop | **E2B open-computer-use**（+ Cua 旧 agent 的 callbacks） | 截图→思考→工具→观察 循环；规划/视觉/grounding 模型解耦；预算、图片保留数、轨迹保存 |
| Agent Runtime / Events / Tools / Security | **OpenHands software-agent-sdk** | Event 类型体系、类型化 Tool、ConversationExecutionStatus 状态机、SecurityRisk + ConfirmationPolicy、StuckDetector、WS 事件流 + 脱敏 |
| Web / Session / Streaming / 机器表 | **lineCode open-computer-use**（+ Cua `libs/xfce`） | `user_machines`/`machine_sessions`/`machine_ai_actions` 表；noVNC 嵌入与 view_only 人机切换；VM 内 TigerVNC+noVNC+supervisord |
| Windows（V2） | **Dexbox** | `Desktop` 接口、多模型 tool-call 适配、Guacamole/RDP、autounattend |

### 7.2 按模块列出值得精读的文件

#### (a) vm-agent（运行在 PVE Linux VM 内）
- Cua：`libs/cua-driver/contract/manifest.json`（工具覆盖面）、`libs/cua-driver/docs/action-result-contract.md`、`tool-output-format.md`
- Cua：`libs/cua-driver/rust/crates/platform-linux/src/x11/mod.rs`、`input/delivery.rs`、`input/foreground.rs`、`capture.rs`、`a11y.rs`、`clipboard.rs`、`xauth.rs`（X11/XTest/AT-SPI 实现要点）
- Cua：`libs/cua/proto/cua/env/v1/{computer,process,filesystem,windows,accessibility}.proto`（API 字段命名）
- Cua：`libs/xfce/Dockerfile`、`libs/xfce/src/scripts/{start-vnc.sh,start-novnc.sh,resize-display.sh,xstartup.sh}`、`libs/xfce/src/supervisor/supervisord.conf`（VM 镜像内服务编排；注意改为强制 VNC 认证）
- lineCode：`docker/ai-desktop/ai_agent_server.py`（命令清单 checklist：terminal_*/file_*/window 管理/OCR）、`health_check.sh`、`display_recovery.sh`、`keep-screen-alive.sh`
- 反例：lineCode `authenticate_client`（无密码放行）、Cua xfce `-SecurityTypes None` 默认 —— 我们必须 token 强制 + 仅内网/隧道暴露。

#### (b) computer-runtime（控制面侧的 Computer 抽象 + PVE 驱动）
- Cua：`libs/cua/crates/cua-vmm/src/qemu/qmp.rs`、`qemu/args.rs`、`cloudinit.rs`、`vnc.rs`、`disk.rs`、`runtime.rs`、`types.rs`（PVE 同为 QEMU：QMP 截图/发键、cloud-init NoCloud 注入 token、VNC 无 Agent 截图兜底）
- Cua：`libs/python/cua-sandbox/cua_sandbox/runtime/base.py`、`runtime/qemu.py`、`interfaces/{screen,mouse,keyboard,shell,files,window,clipboard}.py`（Python 接口分层）
- Cua：`libs/python/agent/cua_agent/computers/base.py`（83 行最小 Computer 接口：screenshot/click/double_click/scroll/type/keypress/drag/move/wait/get_dimensions）
- Dexbox：`internal/tools/computer.go` 的 `scaleCoord` / `resizePNG`（模型坐标 ↔ 真实分辨率缩放）

#### (c) agent-runtime loop
- E2B：`os_computer_use/sandbox_agent.py`（`run` 循环与工具装饰器）、`os_computer_use/llm_provider.py`（多 provider tool schema）、`os_computer_use/grounding.py`
- OpenHands SDK：`openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py`（`run()` 约第 1902 行起：迭代上限、确认等待、暂停）、`conversation/state.py`（状态枚举）、`conversation/stuck_detector.py`、`agent/agent.py`（`step()` 约第 693 行）、`agent/parallel_executor.py`、`event/base.py`、`event/llm_convertible/{action,observation}.py`、`event/condenser.py`
- Cua 旧 agent：`libs/python/agent/cua_agent/agent.py`、`loops/anthropic.py`（Anthropic computer-use 工具适配）、`loops/composed_grounded.py`、`callbacks/{budget_manager,image_retention,trajectory_saver,pii_anonymization}.py`
- lineCode：`backend/app/services/multi_agent_executor.py`（Planner + 子 Agent、输出截断、最终报告）

#### (d) tool-router / policy
- OpenHands SDK：`openhands-sdk/openhands/sdk/security/risk.py`、`confirmation_policy.py`、`analyzer.py`、`ensemble.py`、`llm_analyzer.py`、`defense_in_depth/{policy_rails,shell_semantics,pattern}.py`、`shell_parser.py`、`_shell_ast.py`
- OpenHands SDK：`openhands-sdk/openhands/sdk/tool/{tool.py,registry.py,spec.py}`（`ToolAnnotations`：只读/破坏性等标注，可作为风险默认值来源）
- OpenHands Server：`openhands-agent-server/openhands/agent_server/{_secret_redaction.py,_secrets_exposure.py}`、`conversation/secret_registry.py`
- Cua：`libs/cua-driver/rust/crates/cua-driver-core/src/policy.rs`（YAML/Rego allow/deny）、`authorization.rs`、`session_authorization.rs`、`docs/driver-permission-modes-and-consent-plan.md`（standard/bounded/unrestricted）
- lineCode：`supabase/schema.sql` 中 `machine_limits`（`allow_internet_access`、`allowed_domains[]` 等配额字段）

#### (e) web desktop / noVNC
- lineCode：`app/components/machines/novnc-viewer.tsx`（noVNC 参数：autoconnect/reconnect/resize=scale/view_only/shared）、`machine-viewer.tsx`、`machine-detail-content.tsx`、`sessions-list.tsx`、`ai-command-panel.tsx`、`file-transfer.tsx`；`app/api/machines/[id]/{vnc,status,sessions,screenshot}/route.ts`
- lineCode：`supabase/schema.sql` → `user_machines`、`machine_sessions`、`machine_ai_actions`、`machine_snapshots`、`machine_usage`
- lineCode：`backend/app/services/vm_control.py`（WS 连接池、心跳、重连）
- Cua：`libs/xfce/src/scripts/start-novnc.sh`（`novnc_proxy --vnc localhost:5901 --listen 6901`）
- OpenHands SDK：`openhands-agent-server/openhands/agent_server/sockets.py`（WS 认证：header `x-session-api-key`、失败 4001）、`pub_sub.py`、`event_service.py`
- 改进点（相对 lineCode）：noVNC 经我们网关反代并用短期票据（参考 Cua spacesd README 中“短期 ticket / 签名 URL 由 token 派生、轮换即失效”的**设计思想**，不使用其 FSL 代码）；不在 URL 中放 VNC 密码；PVE 场景可直接用 PVE `vncproxy` + `vncwebsocket` API 或 VM 内 TigerVNC。

#### (f) V2 Windows（仅记录）
- Dexbox：`internal/desktop/desktop.go`、`internal/desktop/rdp.go`、`internal/tools/adapter*.go`、`internal/vbox/autounattend.xml`、`internal/vbox/scancodes.go`、`internal/web/static/viewer.html`、`internal/guacd/guacd.go`
- Cua：`libs/qemu-docker/windows/`、`libs/python/cua-sandbox/cua_sandbox/builder/windows_unattend.py`（MIT，含 quickemu/Mido 派生代码声明）、`libs/cua-driver/rust/crates/platform-windows/`

### 7.3 建议的取舍
1. **自研实现，借鉴设计**：五个仓库都不作为运行时依赖引入（Cua 包变化太快且混有 FSL/AGPL；E2B 绑定云；OpenHands SDK 过重；lineCode 安全薄弱；Dexbox 是 Windows/VBox）。
2. **动作契约**：以 Cua cua-driver 29 工具 + Anthropic computer-use 动作集为并集，定义我们 `protocols/` 中的 Computer Action schema（坐标缩放规则参考 Dexbox）。
3. **事件与状态机**：采用 OpenHands 的 Action/Observation/Error/Reject 事件与 `idle/running/paused/waiting_for_confirmation/finished/error/stuck` 状态。
4. **策略**：OpenHands 的 `SecurityRisk` + `ConfirmRisky` 作为 tool-router 的默认策略；Cua 的 manifest 白名单（bounded 模式）作为“机器级允许工具集”。
5. **审计表**：基于 lineCode 的 `machine_ai_actions` 扩展（加 `risk`、`policy_decision`、`approved_by`、`trace_id`）。
6. **License 红线**：不得复制/链接 Cua 的 FSL 目录与 AGPL 组件、OmniParser 权重；Apache-2.0 代码如有复制须保留 LICENSE/NOTICE 并标注修改；noVNC（MPL-2.0）与 websockify 许可在引入前核实。

---

## 8. 待核实事项
- noVNC（`@novnc/novnc`）、websockify、Apache Guacamole JS、`wwt/guac`、`deluan/bring`、`browser-use`、OS-Atlas/ShowUI 模型的许可证（本次未打开其 LICENSE）。
- lineCode fork 与上游 `LLmHub-dev/open-computer-use` 的差异与上游当前许可状态。
- OmniParser 模型文件按具体 revision 的许可（Cua README 提示需另行核实）。
