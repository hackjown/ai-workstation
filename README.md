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

## v3.9.48 扫描建模：自动平面图 + USDZ 合规检查

- 3D Model Anything 扫描时只有覆盖范围小地图，不导出平面图。现在每个房间都会出 `<名>-plan.png` 和 `<名>-plan.svg`：墙（门窗处断开）、门和开门方向、窗、家具轮廓和名字、每面墙的长度、总尺寸、地面面积、1 m 比例尺。
- 每个 USDZ（自己建的和 App 原生的 RoomPlan 导出）都会跑 OpenUSD 新的 UsdValidation（旧的 ComplianceChecker 已弃用），再加上快速查看的规则。摘要里会写「通过，iPhone 可以打开」，或者没通过的原因。
- 安装时会尽量装 `fonts-wqy-microhei`，平面图 PNG 里的中文标注要用到；没有中文字体就改用英文标注，SVG 不受影响。

## v3.9.47 扫描建模出「重新建模」的模型：iPhone 点开 USDZ 原生预览 / AR

- 之前的 `-3d.html` 只是 LiDAR 扫描网格。现在 `scan3d.py build` 还会按 RoomPlan 数据（rooms/room-N.json）重新建模：墙在门窗位置开洞，门、窗和窗框、地面都建出来；沙发、桌、椅、床做成有形状的几件，其他家具按尺寸做成盒子。每件都是一个带名字的部件（Wall_1、Sofa_1…）。
- 交付 `<名>-model.usdz`：在 Aiw 的 iPhone App 里点开就走系统快速查看，原生 3D 预览，可以切到 AR 摆在桌上；`<名>-model.glb`：在 Blender 或 three.js editor 里每件都能单独移动、隐藏、改色。离线网页第一个视图就是「建模」，下拉菜单切到「扫描」。
- zip 里如果有 App 自己存的 `rooms/room-N.usdz`（Apple RoomPlan 参数化导出），原样另存为 `<名>-roomplan.usdz` 一起交付。
- USDZ 需要 usd-core（`aiw-skill-setup.sh scan-3d` 会自动装；装不上也照样交付 GLB 和网页，摘要里会注明）。已经装过扫描建模的电脑，下次任务会自动补装。

## v3.9.46 扫描 zip 不再并进道闸任务，新要求的「继续」不再强行当修代码（task-610）

- 跟进判断：zip 太常见，不再按「文件类型相同」把它算进旧任务（道闸图集任务生成过 zip，大厅.zip 就被并了进去）；压缩包要和任务有共同的名字词（如「道闸补充.zip」）才算同一件事。另外，话加附件能被自动技能认出来（如「建模」+ 扫描 zip → 扫描建模），而旧任务不是这类，就开新任务。
- 模型路由：「长任务的继续/重试 → 修复代码」只给不带新要求的「继续」「重试」。带了新要求的继续（如「建模」）按新要求自己的话路由，不会再拿旧任务的 build_album.py 当源头去套修代码契约。

## v3.9.45 扫描建模：iPhone 扫的房间分享给 Aiw，直接出 3D 模型

- 新技能「扫描建模」（scan-3d）：在「3D Model Anything」里 Share room → 分享给 Aiw → 说「建模」。附件是 zip，且话里有建模/3D/模型/扫描/房间这类词，就自动用这个技能（不会再被当成户型设计器或 CAD 画图）。任务派给「设计制图」电脑。
- 第一步固定跑 `scan3d.py build`，几十秒，不用模型：读 LiDAR 网格和 App 缓存的照片颜色，只留本房间，简化到 ≤ 30 万面，交付 `<名>-3d.html`（离线 WebGL 页面，手机直接开：拖动旋转、双指缩放平移、俯视、剖切滑块去掉天花板）、`<名>.glb`（Blender / Three.js 可用）和尺寸摘要（长宽、层高、墙门窗、识别到的家具）。
- 瘦身 `scan3d.py slim`（用户提到太大或要存档时）：照片原尺寸按质量 82 重存（保留 EXIF，位姿和内参不变），删掉深度帧和 arworldmap，另存 scan-slim.zip。
- 精修成可编辑 Three.js 场景（wesbos/3d-model-anything 的 scan-to-threejs 流程）只在用户明确要求时做，需要强模型。
- 上传上限：分享 → App 上传走 /api/uploads/raw，服务器上限 500 MB（`AIW_UPLOAD_MAX_MB`），手机端同样 500 MB；任务开始时按 16 MB 分段推到电脑。中间如果有 Cloudflare 等代理，它自己的请求体上限（免费版 100 MB）会先拦。
- 安装：`aiw-skill-setup.sh scan-3d`（任务开始前自动检查并安装，装 numpy / Pillow）。

## v3.9.44 聊天框的新任务不再并进旧任务；改文档不再当成修代码（task-b09）

- 跟进判断：聊天框里「改成…」「修改」这类话以前会并进最近的任务。现在要先看是不是同一件事：附件是这个任务从没碰过的文件类型（比如道闸图集任务收到一个姓名牌 .docx），或者选的技能和任务用过的技能不同，就开新任务。「我已经上传了」「继续」照旧接着原任务。
- 意图：带【技能：officecli】等非 dev 技能、或者改 Word/PPT/Excel/PDF/姓名牌这类文档的请求，不再判成「修复代码」，不会走重模型、也不会套上源头脚本契约。
- TODO 不再死锁：本次没有改过受控源文件时，标完成不要求验证；被拒的项退回 pending，不再和新的进行中项冲突；officecli query/view、soffice 转 PDF、pdftoppm、read_image 都算验证；同一项连拒 3 次放行并在回执里标「未验证」。
- 卡顿检测：发出文件、勾掉 TODO 都算进展，交付后不会再误报「30 步没有进展」。

## v3.9.43 连接器工具列表取不到时说清原因

- 任务开头那行和「测试」都会写出工具列表失败的真实原因（超时几秒 / 代理拒绝 / 空列表等），「测试」每次重新向服务器要列表，只算这个连接自己的工具。
- Adobe 这类预置连接器拉工具列表的时限从 20 秒放宽到 45 秒；刷新失败时沿用 24 小时内上次成功的列表，一次慢请求不会让任务丢掉 Adobe 工具。

## v3.9.42 用了「Photoshop」却没走 Adobe

- 规划规则：工具列表里有 `mcp_adobe_*` 且请求点名 Photoshop / Adobe / Firefly / Lightroom / Illustrator / Express 时，先用 Adobe 工具；没有合适的工具或调用失败才用 Photopea，并说明一句。没连接 Adobe 时，「Photoshop」仍指 Photopea。
- 任务开头写一行：这次带上了几个 Adobe 工具，或者为什么没带（未连接 / 需要重新授权 / 工具列表没取到），不再悄悄改用 Photopea。

## v3.9.41 连接器：Adobe 创意工具（Adobe 官方托管 MCP）

设置 › 连接器 › 设计 › **Adobe 创意工具**：点「连接」→「用 Adobe 账号登录」，在 Adobe 页面登录并同意授权即可。Agent 随后能直接调用 Adobe for Creativity（`https://adobe-creativity.adobe.io/mcp`，Photoshop、Lightroom、Illustrator、Firefly、Premiere、Express、InDesign、Stock，50+ 工具）。

- **直接走 OAuth**：Adobe 对未登录的 initialize 回 403（不是 401，也没有 WWW-Authenticate），所以预设不先探测。发现顺序：受保护资源元数据（`/.well-known/oauth-protected-resource/mcp` → `ims-na1.adobelogin.com`）→ IMS 授权服务器元数据；adobe.io 读不到时用内置的 IMS 地址（`/ims/authorize/v2`、`/ims/token/v3`、`/ims/register`、`/ims/revoke`）。
- **动态注册（不用去 Adobe 开发者控制台）**：运行时向 `/ims/register` 注册机密客户端（`client_secret_post`，IMS 只支持 basic/post），注册时带上 scope——IMS 只把 `llm.assets.manage`、`llm.assets.upload` 给注册时申请了它们的客户端（2026-11-11 实测）。**每个回调地址一份注册**（SSH 隧道 127.0.0.1:8000、局域网 IP、公网地址互不覆盖，存 vault `oauth_dcr_adobe`），登录完成时用哪个回调地址，哪份注册就成为这个连接的客户端。client_secret 只在 vault（AES-256-GCM）里，代码里没有写死任何客户端。
- **授权**：PKCE S256；scope 默认 `openid AdobeID offline_access llm.assets.manage llm.assets.upload`（可在「代理和高级」里改）。IMS 拒绝 scope 时会重定向 `?error=invalid_scope` 且**不带 state**：aiw 认出这次刚发出的授权，自动退回基础权限 `openid AdobeID offline_access` 重新跳转登录。RFC 8707 `resource`：默认 auto——授权和换 token 都带；token 端点拒绝（invalid_target / invalid_request）就不带再换一次，并记住以后刷新也不带；也可设 on / off。
- **刷新**：token 过期前 60 秒刷新（每个连接一把锁）；IMS 对失效 refresh token 回 `access_denied`，现在也算「需要重新授权」。调用时遇到 403：先刷新一次重试；刷新后仍 403 则告诉 Agent「服务拒绝访问（账号/套餐/地区/代理）」，不把连接标成需要重新授权。
- **工具**：名字 `mcp_adobe_<工具名>`，说明前缀「[Adobe 创意工具]」；按 MCP annotations：readOnlyHint 不弹确认，destructiveHint 必弹，其余按原来的写操作规则。为省 token，**默认只在像修图/抠图/海报/封面/图片/视频/设计/PS/Firefly… 这类任务里提供**（「每个任务都带上」可关掉这个限制）；可设工具白名单（`allow_tools`，支持 `photoshop_*`）。
- **结果文件**：图片块、内嵌资源（blob）、resource_link 和文字里的媒体链接（Adobe 返回的预签名地址会过期，立即下载，单个上限 40 MB，最多 8 个）都变成文件：**放在任务卡片的结果文件里**（第一张图做缩略图），同时写到电脑 `~/workspace/connectors/adobe/`；第一张图给模型下一步看（注明是结果图不是屏幕）。base64 不进模型上下文。
- **代理**：连接器层新增代理（http:// / https:// / socks5://）。设置 › 连接器 › 高级 › 连接器代理（所有连接器），或 Adobe 自己的「代理和高级」（优先）。授权服务器元数据、注册、换 token、刷新、撤销、MCP 调用、结果下载都走它。依赖加了 `socksio`。
- 「试试免登录模式」：只有服务器真的接受未登录 initialize 时才启用（今天实测是 403，所以会提示不可用）。
- API：`POST /api/connections {connector:"adobe", proxy?, scopes?, resource_mode?, expose?, allow_tools?, guest?}`；`PATCH /api/connections/{id}`（改代理/白名单/暴露方式，不用重新授权）；`GET/PUT /api/oauth/settings` 多了 `proxy`。
- iOS / Android 的连接器在网页设置里，App 未改动，版本不变。

**未验证**：真实登录和 token 交换（只做到 Adobe 登录页）、真实 tools/list 和 tools/call（工具名、参数、结果格式按 MCP 规范处理）、免登录档、IMS token 端点对 `resource` 的态度（假 code 时有无 resource 都回 access_denied）、从国内访问 adobe.io / adobelogin.com / auth.services.adobe.com 的情况。

## v3.9.40 订餐厅交给大众点评/美团确认；机票多平台比价（携程/飞猪/去哪儿/同程）

**订餐厅（第一条路：aiw 找、手机 App 确认）**：说「帮我在静安寺附近订个本帮菜，周六晚上 4 个人」，任务里 Agent 先用搜索/浏览器找 1-3 家合适的餐厅（尽量拿到大众点评/美团店铺链接、评分、人均、地址），再对每家调用新工具 `restaurant_booking`，生成一张预订卡片：店名、地址、★/人均、建议的日期·时间·人数，按钮「打开大众点评预订」「打开美团」。手机上点按钮：有店铺 id 就直达 App 店铺页（`dianping://shopinfo?id=` / `?shopuuid=`、`imeituan://www.meituan.com/food/poi/detail?id=`），没有就打开 App 内搜索（`dianping://searchshoplist?keyword=`、`imeituan://www.meituan.com/search?q=`）；没装 App 时打开网页（m.dianping.com / www.dianping.com / www.meituan.com）。**aiw 不在网页上提交预订，也不付款**，用户在 App 里自己确认。

**机票比价**：说「查一下 11 月 20 日上海到北京最便宜的机票」，任务里调用新工具 `flight_search`（出发地、目的地、日期，可选返程、人数、舱位、只要直飞、指定平台）。它把 `flight_probe.py` 推到任务所在电脑，在电脑的 Chrome（DevTools 端口，和京东监控同一套）里**同时**为每个平台（和返程）开一个窗口打开公开搜索页，每个平台单独限时；读取页面自己请求的航班 JSON（页面自己签名的请求，不重放、不逆向签名）、同程的页面状态（`__NUXT__`），都没有时读页面文字（去哪儿 PC 页数字有 CSS 偏移混淆，不读文字）。在电脑上就解析成精简的行（vm-agent shell 输出上限 64 KB），原始抓取存在电脑 `~/.cache/aiw/flight/<平台>-<去程/返程>.json` 便于排查。服务器合并成一张按价格排序的表：平台、航班号（中转显示 A/B）、航司、起降时间、机场、单人价、是否含税（平台 JSON 标志 / 税费字段 / 页面「机建+燃油」字样，没有就「未注明」），每个平台的状态：成功 / 需登录 / 需验证 / 无结果 / 超时 / 失败 / 已关闭——一个平台被登录墙或滑块拦住不影响其他平台。点一行或平台标签打开该平台这条航线这一天的页面（手机上装了 App 时系统可能直接用 App 打开），在那里选同一航班下单；**不下单、不付款**。同一航线同一天 10 分钟内直接用缓存（`refresh` 可强制重查）。

- 结果卡片：工具把卡片存到 kv `task_cards:<任务 id>`，任务结束时放进任务卡片的 `result.cards`；网页（travel.js / travel.css）、Android（WebView 新增 `AiwAndroid.openApp(app, web)`：先 App scheme，再网页链接）、iOS（新 TravelCards.swift：先 App scheme，再 universal link only，最后 Safari；Info.plist `LSApplicationQueriesSchemes` 加了 dianping、imeituan、ctrip、qunariphone、fliggy、tctravel）都会显示。`GET /api/travel/cards/{task_id}` 也能取。
- 路由：聊天里机票比价/订餐厅类的话直接交给电脑（任务），并给任务提示词加上用 `flight_search` / `restaurant_booking` 的规则。视频通话不加新工具：`create_aiw_task` 的说明里写明查机票、订餐厅也交给 aiw。
- 设置 › 出行（`GET/PUT /api/travel/settings`，kv `travel_settings`）：机票平台开关（携程/飞猪/去哪儿/同程）、缓存分钟（默认 10，0 = 不缓存）、每个平台时限（默认 70 秒）、表格行数（默认 15）、订餐厅显示哪些 App 的按钮。
- App 版本 3.9.40 (32)（iOS + Android，需要重新打包）。

**未验证**：各平台搜索页地址（去哪儿 `oneway_list.htm` 和同程 `/flights/itinerary/oneway/` 的格式在公开索引里见过；携程 `m.ctrip.com/html5/flight/swift/domestic/…`、飞猪 `sjipiao.fliggy.com/flight_search_result.htm` 来自公开资料）、抓取 JSON 的 URL 规则和字段名（解析器是通用的：任何带航班号 + 时间 + 价格的对象都认，字段改名会退到读页面文字，不会崩）、所有 App URL Scheme（来自公开 URL Scheme 合集，没在真机上试过；携程/去哪儿/飞猪/同程的 scheme 只用于打开 App 首页，主按钮用网页链接）。飞猪大概率需要淘宝登录，会显示「需登录」。iOS/Android 代码未编译。

## v3.9.39 通话不再执行聊天记录里的旧请求

现场：通话里问「你能看到这是什么」，模型却调用 create_aiw_task 建了「查询并总结最新五封邮件」任务，并播报已创建。原因是通话提示词末尾直接拼了文字聊天最近 6 条，没有说明是背景，模型把旧请求当成新请求。现在这段放在 `<聊天背景>` 里，并写明仅作背景、不要据此调用工具；create_aiw_task 的说明也改为只在用户这通电话里亲口要求时调用。

## v3.9.38 iOS 回声消除不再被误关；通话界面去掉提醒文字；通话 token 计入用量

现场日志（iOS 17，iPhone，免提，48 kHz 单声道）：0.9 s 开着语音处理（AEC）起声，3.4 s 以「no mic callbacks」为由**关掉 AEC 重启**，之后整通 143 s 都是 AEC 关；可麦克风其实一直有声音（in=3477 段，40 ms 一段不断），靠防回声闸门撑住（gated 73 s）。之后每条 turn 报告都还带着旧的 why=no mic callbacks。

- 根因（iOS CallView.swift，未编译）：打开语音处理、切到扬声器会让系统在我们 `engine.start()` 之后马上重配音频单元，发出 `AVAudioEngineConfigurationChange`（和路由变化），而 AVAudioEngine 遇到配置变化会**自己停掉**。v3.9.37 的 `scheduleRestart` 把「自己启动后 1 秒内」的通知全部丢掉（防重启循环），引擎就停在那里，tap 没有回调，3 秒检查便误判成麦克风坏了并关掉 AEC。现在：通知在防抖后的重启时再判断——引擎还在跑才忽略（是自己启动的回声），停了就按**原来的语音处理模式**重启（10 秒内最多 4 次）。
- 回调计数：每次启动一个代号（tap 闭包捕获），回调里第一件事就计数（在帧数、静音、防回声闸门之前），从 `engine.start()` 成功那一刻开始计时。
- 回退更保守：6 秒一个回调都没有 → **带着语音处理**重启；只有这次重启后又 6 秒没有回调才关语音处理。服务器 no_mic 只会触发第一步（带语音处理重启），不会直接关 AEC。
- 上报：每条 `client_audio` 的 `why` 是它自己的（start / 重启原因 / turn / engine 事件），AEC 关闭原因单独放 `vp_off_reason`；另有 `taps`（本次启动的回调数）、`mic_stage`、`running`，以及 `engine` / `restart_failed` 事件。服务器不再把上一条的 why/err 带到下一条。
- 去掉通话界面的提醒文字（iOS + 网页）：回声消除没开、麦克风没声音/只有静音、音量条、系统打断、工具进度、ready 提示、非致命错误等都不再显示，只写进 通话诊断（网页另打 console）。保留：结束通话的真错误（模型断开、额度、Key、没权限等，在挂断时显示）和网页必要的「点一下开始声音」按钮（改成小按钮；麦克风 6 秒没样本时也用它重建）。服务器照旧记录并发送 no_mic（客户端不显示）。通话诊断保留「回声消除关闭」并附原因。
- 通话 token 计入用量：通话不走 llm_router，以前没记。现在写进 `token_usage`（role=`call`，model=实际模型，task_id 空）。Gemini Live 读 `usageMetadata`（字段按 ai.google.dev/api/live 核对：promptTokenCount、responseTokenCount、toolUsePromptTokenCount、thoughtsTokenCount、totalTokenCount、promptTokensDetails/responseTokensDetails 按 modality）；不确定是累计还是每轮，开始按累计只记增量，一旦同一连接里数变小就判定为每轮并补齐差额（只可能少记，不会重复记）；重连时每个连接单独基线，续接会话的计数接着算或从零算都不会重复。DashScope 实时 `response.done` 的 usage、轮流模式的流式 usage（include_usage）也记。每个模型最多 30 秒写一行（`AIW_OMNI_USAGE_FLUSH_S`），挂断时写剩余。通话诊断显示 tokens 输入/输出及音频/视频/图像/文字拆分；用量页「按角色」显示「通话」（网页和 iOS）。

## v3.9.37 通话里球球总被自己的回声打断

现场日志（iOS App，免提，Gemini Live，起说灵敏度低）：每句回答都在半句被截断，文字稿却完整；时间线里的「user」文字其实是球球自己的声音（「我看到你带」「请问还有」），被麦克风收回去 → Gemini VAD 当成用户说话 → `interrupted` → 客户端停播。

- iOS App（CallView.swift，未编译）：v3.9.36 的「3 秒无声就关掉语音处理重启」会在开头没人说话时触发（语音处理后的信号本来就几乎是静音），于是系统回声消除（AEC）被关掉。现在只有麦克风回调**完全没有**来 3 秒（或服务器报 no_mic 且本地确实没有回调）才回退；安静/全零信号只提示，不回退。语音处理开/关、原因、setVoiceProcessingEnabled 的错误、输入采样率/声道、输出路由通过 `{"type":"client_audio"}` 发给服务器，进「通话诊断」时间线（AEC 关闭记 WARNING），屏幕上也会提示「回声消除没开…建议戴耳机」。播放仍走同一个开了语音处理的引擎（player → mainMixerNode，会话 .voiceChat），AEC 有参考信号。
- 防回声闸门（iOS + 网页）：球球说话时（以及播放队列放完后 300 ms 内），麦克风每 40 ms 一段改发全零；只有明显比回声大（RMS > max(0.06, 3×播放时测到的回声电平) 持续 ≥ 200 ms）才算真打断，那时把攒下的语音和后续原样发出。设置 › 视频通话 ›「防回声」（`omni_echo_gate`，默认开）可关；网页也可 `localStorage.aiw_call_echo_gate = "off"`，iOS `UserDefaults aiw.call.echoGate = "off"`。每句回答放完客户端上报一次回声电平/阈值/被压住的秒数/打断次数。
- Gemini 打断开关：设置 ›「Gemini 打断（barge-in）」（`omni_gemini_barge_in`：空/auto/on = API 默认 START_OF_ACTIVITY_INTERRUPTS；off = `realtimeInputConfig.activityHandling: "NO_INTERRUPTION"`，球球总把话说完）。字段名按 ai.google.dev/api/live 核对。
- 诊断：每次 `serverContent.interrupted` 计数（stats 行 `interrupts=N`，计数器 `interrupts`），时间线记「model interrupted by user speech (#N; said so far …; heard …)」；ready 消息带 `echo_gate` / `barge_in`；backend 行显示 `barge_in=… echo_gate=…`。
- 播放尾巴：iOS 缓冲按 `.dataPlayedBack` 完成；路由/配置变化重启引擎时把还没播的缓冲重新排上，不再丢掉回答的后半截；打断后迟到的完成回调按代号忽略。服务器转发全部音频块（out 计数在发出前累计），网页播放队列不变。

## v3.9.36 通话收不到麦克风声音（手机网页 / iOS App）

现场诊断：Gemini 已接通（0.9 s），但 21 秒里客户端只发了摄像头画面，没有一条麦克风音频（`in=0 frames=15 … user_tr=never`）。

- 网页（call.js）：点「视频通话」的那一下就同步创建并 `resume()` AudioContext（在任何 await 之前，iOS Safari 只认这一下点击），全程复用；播放一个静音样本解锁外放。若仍不是 running（或被系统打断成 interrupted），屏幕中间出现「点一下开始声音」；通话里点任意位置也会恢复。顶部有麦克风音量条；3 秒没有采样显示「没有收到麦克风声音」，点一下会在新的 AudioContext 上重建麦克风链路；只有数字静音时提示检查静音键/权限。降采样保留小数位置，48 kHz / 44.1 kHz 都精确输出 16 kHz。
- iOS App（CallView.swift，未编译）：去掉会静默失败的 AVAudioConverter，麦克风缓冲按实际格式（Float32/Int16/Int32、交错与否、多声道取第一个有信号的声道）手工转成 16 kHz 单声道；3 秒无声提示「麦克风没有声音，请检查权限/静音」，并自动关掉语音处理（voice processing）重启一次引擎；处理 AVAudioSession 打断、路由变化和 AVAudioEngine 配置变化（重启引擎）。
- 服务器：接通后 5 秒（`AIW_OMNI_NO_MIC_S`，0 = 关）仍没收到任何音频，在通话时间线记 WARNING「no audio from client」，并给客户端发一次 `{"type":"error","code":"no_mic"}`（「服务器没有收到麦克风声音（摄像头画面正常）…」）。客户端静音时发 `{"type":"mute","on":true}`，不会误报；取消静音后重新计时。
- start 消息新增 `client`（`ios` / `web`）和 `ua`；「call from …」日志行和 `GET /api/omni/calls` 显示 client / ua（老客户端用请求头 User-Agent）。
- 提示：手机网页通话必须用 HTTPS（反向代理/远程访问也要 https://），否则浏览器不给麦克风。

## v3.9.35 Gemini Live 通话加固与诊断

- 上游断开必报原因：每次关闭都记录代码和原因；超配额、Key 错误、参数错误（1007/1008/1011 等）会把原因发给客户端并结束通话；连续 3 次快速断开结束通话；重连异常也会结束通话，不再出现「已接通但不回话」。
- 修复：重连过程中新连接再次断开时重连请求被忽略，导致通话永久静默。
- 同一时刻只保留一条上游连接；挂断、客户端断线、取消时关闭上游连接。发送卡住 5 秒断开重连；工具调用 30 秒超时。
- 看门狗：用户有效说话后 20 秒无任何模型回复，提示「模型没有响应，正在重连…」并续接，第二次开新会话，第三次报错挂断。
- 客户端打断后丢弃该回答剩余音频；麦克风停顿 1.5 秒发送 audioStreamEnd。
- 新设置：`omni_gemini_vad_start`（low/high/api，留空=low，减少外放回声误触）、`omni_gemini_vad_end`（留空=API 默认）、`omni_gemini_silence_ms`（100–5000）。
- 诊断：`docker logs aiw-api 2>&1 | grep aiw.omni`（每 15 秒一行计数）；`GET /api/omni/calls` 保存最近 20 通的时间线；设置 › 视频通话 有「通话诊断」按钮。
- iOS：通话中的错误会停留显示（未编译）。
- 环境变量：AIW_OMNI_WATCHDOG_S、AIW_OMNI_SEND_TIMEOUT、AIW_OMNI_TOOL_TIMEOUT、AIW_OMNI_STATS_S、AIW_OMNI_LOG 等。

## v3.9.34 通话模型槽位按模型/供应商自动选后端

「通话模型（全模态）」槽位（`omni_model` + `omni_model_provider`）里放的是 Gemini 时，通话直接走 Gemini Live，不再把 Gemini 的 Key 发给百炼实时接口（之前探测会出现「实时 ✗ HTTP 401」）。
实现在 `services/api/aiw_api/omni_call.py`（`config` / `plan` / `probe`）和 `providers.py`（`is_gemini` / `is_dashscope`）。

- **判定为 Gemini Live**：槽位的供应商是 Gemini（地址主机为 `generativelanguage.googleapis.com`，或 id/名称为 `gemini`，或与「Gemini 中转地址」同主机），
  或模型名像 Live 模型（`gemini-` 开头且含 `live`，或含 `native-audio`）。此时 Gemini 模型 = 通话模型；Key = 「Gemini Key」，留空用该 Gemini 供应商的 Key（不会借用非 Gemini 供应商的 Key），
  再空用「Gemini Live 的供应商」/ 环境变量；代理 = 「出站代理」，留空用该供应商的代理。
- 「自动」模式下这种情况**只用 Gemini Live**：不试实时接口，也不退到轮流（Live 模型一般不是 chat/completions 模型），Live 断线就结束通话。手动选「只用轮流」仍按轮流走该供应商。
- **实时接口（DashScope 协议）只在以下情况尝试**：填了「实时接口地址」，或槽位供应商是百炼（`dashscope*.aliyuncs.com`、`*.maas.aliyuncs.com`），或槽位用的是「默认」网关（与 v3.9.31 相同）。
  其他供应商不会再把 Key 发到 `dashscope.aliyuncs.com`；「只用实时」模式此时直接报错说明原因。
- 探测：Gemini 路线只显示「Gemini Live」和「会说话」（Live 会话固定输出语音，连接 + setupComplete 成功即算），附 Gemini 提示（国内需代理；免费层数据会用于改进产品）；
  实时接口未尝试时不显示「实时」徽章；Qwen 的说明只在模型名是 `qwen*-omni` 时显示。`/api/omni/status` 和探测结果多了 `family`、`hint`，状态里多了 `realtime_on`、`gemini.route`。

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
