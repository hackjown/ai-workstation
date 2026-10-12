# VM Agent 协议（V1）

传输：WebSocket，`ws://<VM_IP>:8765`，鉴权 `Authorization: Bearer $VM_AGENT_TOKEN`（或 `?token=`）。
同一连接可并发多条请求，按 `id` 匹配响应；GUI 输入类动作在 Agent 内部串行执行。

请求
```json
{"id": "action-001", "type": "computer.click", "payload": {"x": 820, "y": 450}, "observe": true, "timeout": 30}
```
响应
```json
{"id": "action-001", "success": true, "result": {"x": 820, "y": 450, "button": "left"},
 "observation": {"screenshot": "<base64 PNG>", "width": 1920, "height": 1080, "cursor": [820, 450]},
 "duration_ms": 412}
```
`observe: true` 时动作完成后附带截图（Validator 用来做页面变化检测）。

| type | payload |
|---|---|
| computer.screenshot | `max_width?` |
| computer.click | `x, y, button?=left/middle/right` |
| computer.double_click / right_click / move | `x, y` |
| computer.drag | `x1, y1, x2, y2` |
| computer.type | `text` |
| computer.keypress | `key`（如 `ENTER`、`ctrl+l`） |
| computer.hotkey | `keys: ["ctrl","l"]` |
| computer.scroll | `dx, dy, x?, y?`（dy>0 向下） |
| computer.open_app / close_app | `name`：chrome / terminal / files / editor |
| computer.cursor_position / screen_size | — |
| shell.exec | `command, cwd?, timeout?` |
| browser.open | `url?` |
| browser.navigate | `url` |
| browser.back / forward | — |
| files.list / read | `path`（限制在 `~/workspace` 内） |
| files.write | `path, content, append?` |
| files.download | `url, path?` → `~/workspace/downloads/` |
| files.upload | `path, content_b64` |
| system.health | — |

错误：`success=false` + `error`（参数缺失、坐标越界、路径越权、超时、Shell 非零退出码等）。
