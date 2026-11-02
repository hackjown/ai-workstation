---
name: house-plan-designer
title: 户型图 → 装修设计器
description: 把户型图（酷家乐/贝壳/售楼处户型图、手绘尺寸图、CAD 导出的矢量 PDF/DXF、原始结构图）还原成可交互的装修设计器网页（单文件 HTML，离线可用）：2D 平面 + 3D 鸟瞰/漫游 + 家具布置 + 水电/吊顶/铺装/拆改图层 + 电路/水管/网线自动排布 + 报价 + PDF/DXF 施工图。触发词：装修设计器、户型图转 3D、户型做成网页、3D 漫游、水电排布、装修报价。
---

# AIW 内置版说明（先读这一节，再按下面的上游流程做）

本技能基于开源 [ZWD11/house-plan-designer](https://github.com/ZWD11/house-plan-designer)（MIT，见 LICENSE 与 NOTICE.md），
设计器模板 `assets/template.html` 原样使用，**不要改模板**。AIW 只加了 Linux VM 适配层 `scripts/aiw_hpd.py`。

技能目录：`~/.claude/skills/house-plan-designer`（下面写作 `$HPD`）。上游文档里的 `python scripts/…` / `node scripts/…`
在 VM 上一律写成 `python3 $HPD/scripts/…` / `node $HPD/scripts/…`。

## 0. 依赖（每个任务第一步，很快）
```bash
HPD=~/.claude/skills/house-plan-designer
python3 $HPD/scripts/aiw_hpd.py doctor     # 最后一行 HPD_DOCTOR {...}，ok=false 时再跑：
python3 $HPD/scripts/aiw_hpd.py ensure     # pip --user 装 numpy/pillow/opencv-headless/pymupdf/ezdxf、npm install、Chrome/Chromium、中文字体
```
VM 一般没有显卡：截图和验收用 SwiftShader 软件渲染（脚本已自动加参数），3D 截图慢一点是正常的，不要当成设计器坏了。

## 1. 工作目录与输入
- 上传的图在 `~/workspace/uploads/`。工作目录 `~/workspace/hpd/<户型名>/`，交付的 HTML 也放这里。
- DWG：先用 cad-drawing 工具链的 ODA File Converter 转 DXF（`~/tools/ODAFileConverter`，或 `python3 ~/workspace/.aiw/cad-drawing/drawkit.py dwgtest` 看能否用），再走上游的 CAD 路线。
- **同一任务里已经用 cad-drawing 画过这张图**（任务目录有 `cad/spec.json`，墙已经通过硬核对）：直接用它出初稿，不要重新识别：
  ```bash
  python3 $HPD/scripts/aiw_hpd.py from-cad cad/spec.json -o hpd/<户型名>/spec.json --name <户型名>
  ```
  初稿里墙、门窗、房间名、底图都有；家具只是占位（灶台/水槽并进整体橱柜），按下面第 5 步重摆。`_aiw.unplaced_openings`
  是没挂上墙的门窗，要手补。没有 cad/spec.json 时照上游第 0~3 步识别（analyze.py / cadvec.py / cadsplit.py）。
- **图片 / 截图（jpg、png、售楼处 / 贝壳户型图）第一步：from-image**——它调用 cad-drawing 的照图识别（确定性描墙、门窗缺口、
  尺寸数字定比例、OCR 房间名，十秒左右），再转成 HouseSpec 初稿、补齐墙缺口处的门、检查房间是否围合：
  ```bash
  python3 $HPD/scripts/aiw_hpd.py from-image ~/workspace/uploads/<图> -o hpd/<户型名>/spec.json --name <户型名>
  ```
  最后一行 `HPD_DRAFT`：
  - `ok: true`：直接进第 2 步 make。**不要再跑 analyze.py、不要自己按像素峰值手写墙**（task-9c4 这样绕了 35 步以上还把 6 个房间连成一片；
    同一张图照图识别 8 秒就围出了房间）。按 `open_rooms`（没围合的房间，多半是细线隔墙 / 栏杆没认成墙）和 `shared_rooms`
    （同一块区域里的几个名字；客餐厅这类开放式就保留）在 spec 里**只补缺的那几面墙**，缺的房间名补到 rooms，`_aiw: "gap"` 的门是按墙缺口补的，开向不对改 hinge / into。
  - `ok: false`：图上没有可读尺寸时按 `next` 加 `--mm-per-px` 再试**一次**；还不行再跑下面的 imgcheck。
- **from-image 不行时才用 imgcheck**（它会跑一次 analyze.py，再判断这张图的墙能不能自动认）：
  ```bash
  python3 $HPD/scripts/aiw_hpd.py imgcheck ~/workspace/uploads/<图> --total-width <总宽mm> --total-height <总深mm> --out hpd/<户型名>/a0
  ```
  总宽、总深读图上最外道尺寸（或分段加起来）。最后一行 `HPD_IMGCHECK`：
  - `route: "analyze"`：用 `a0/draft.json` 接着做第 3 步。make 后 freeWallEnds 仍 > 8，**最多再换一次参数**重跑 analyze.py，还不行就改走尺寸链。
  - `route: "dims"`（售楼处小图、效果图、外轮廓认错）：**不要再裁剪、放大、换参数重跑 analyze.py，也不要反复 read_image 让视觉模型报坐标**
    （task-3ddae3f2f2 这样绕了 126 步也没围出一个房间）。照 `advice` 走尺寸链：上 / 下 / 左 / 右分段数字累加成两套毫米轴网，
    按 `reference/example-gen.py` 的写法手写墙、门窗、房间名，make 后看 overlay.png 微调。
    **尺寸数字先用 `dimocr.py` 自动读**（上游 b94183f 新增，位图 OCR，不装 OCR 库）：
    `python3 $HPD/scripts/dimocr.py <图> --scale <mmPerPx> --out hpd/<户型名>/ocr`，只用 ocr.json 里 `ok` 的段，`?`/✗ 的段才看图人工补。
    交付时说明哪些位置是按图形估的，并建议用户换一张
    户型部分 ≥1500 像素宽、没有家具的线稿会更准。
- **黑底 CAD 截图**（模型空间截屏，黑底彩色线，没有矢量）：不要走 from-image / imgcheck（它们按“深色 = 墙”认，黑底整张都是墙）。
  照上游 `reference/route-image.md` 末节「黑底 CAD 截图」：`cadshot.py` 按线色分层 + 轴号定比例 → `analyze.py --wall-color cyan --scale <mmPerPx>`
  → `dimocr.py --color green` 读尺寸数字。
- 同一张图 read_image 总共不超过 3 次；看细节用 PIL 裁出局部放大后看一次，不要整张反复看。
- 一个户型图任务不要再调用 cad-drawing 的 drawing_auto / drawing_build 工具出 CAD 图（那是出 DXF 平面图的流程）；照图识别由 from-image 在内部调用，结果直接变成设计器初稿。

## 2. 生成 + 自查（代替上游第 4 步的两条命令）
```bash
python3 $HPD/scripts/aiw_hpd.py make hpd/<户型名>/spec.json --out hpd/<户型名>/work --name <户型名>
```
最后一行 `HPD_RESULT {...}`：`report` 是 check.mjs 的摘要（`freeWallEnds`、`wallsDropped`、`wallsSkewed`、`openingsHidden`、
`labelsOutsideRooms`、`issues_error/warn`、`must_fix` 原文、每个房间面积和开间进深），`clean=true` 最好，`can_finish=true`
（没有 errors / wallsDropped / issues_error）就可以往下做；`shots` 是 plan2d / overlay / view3d 截图路径。
- **最多 3 轮 make**（`round` 字段会数），第 3 轮之后不管 clean 与否都进第 5 步摆家具，再 --final 交付，没改掉的写进交付说明。
  task-a7b46fccd8 识别 15 秒就成功了，之后为了 clean=true make 了 11 轮、问了视觉模型 12 次 overlay、写了 7 个脚本重建墙，
  半小时没交付——阳台栏杆、空调位、可拓展空间本来就不封闭，`freeWallEnds` 在那里永远不会归零。
- from-image 的 `open_ok` / `shared_ok`（阳台、露台、空调位、设备平台、可拓展空间、开放式客餐厅）**不补墙**；只补 `open_rooms` /
  `shared_rooms` 里列的。
- **改墙 = 读入 spec.json，改那几面墙 / 洞口 / 房间名，写回**。不要另写生成脚本从 cad-spec.json / walls_traced 重建全部墙（门窗和已经对的围合会丢）。
- overlay.png / plan2d.png 每轮最多看一次，整个任务 read_image 不超过 4 次；不要问视觉模型"哪面墙缺了、在哪"，看 report 的 must_fix。

## 3. 交付
```bash
python3 $HPD/scripts/aiw_hpd.py make hpd/<户型名>/spec.json --out hpd/<户型名>/work --name <户型名> --final hpd/<户型名>
python3 $HPD/scripts/aiw_hpd.py qa hpd/<户型名>/<户型名>-装修设计器.html --out hpd/<户型名>/qa   # 默认 --lite --quick（约 8~15 分钟）
```
- 验收在 VM 上默认 `--lite --quick`（跳过 PDF 和光线追踪）。用户明确要完整验收再加 `--full`。验收里纯属软件渲染
  超时的项（3D/漫游/中控一片超时）按“环境限制”说明，不要为此改设计器；真报错（页面报错、NaN、导出没文件）必须修。
- send_file：`<户型名>-装修设计器.html`、`<户型名>-plan2d.png`、`<户型名>-view3d.png`（有底图时加 overlay）。
  done 的总结按上游「交付时告诉用户」写：哪些尺寸按标注、哪些按图形量、图纸自身矛盾、改动过的地方、打开后怎么用
  （PDF 施工图、DXF、报价、水电管线都在设计器里导出）。
- 用户只要求“改一下设计器里的 X”（续做）：改同一个 spec 重新 make + 交付，不要从头识别。

## 4. 不要做的事
- 不改 `assets/template.html`；上游提到的 designer 源码项目（`src/…`、`npm run skill:sync`）在 VM 上没有，家具库缺件就用最接近的件或自由模型，并在交付时说明。
- 不用视觉模型估墙坐标：墙来自 analyze.py / cadvec.py / cadsplit.py 或 cad-drawing 的确定性结果。

---


## 5. 全景漫游 / 分享（用户要「全景」「VR」「720」「发给别人看」时）

- **设计器出全景漫游**（交付后再做，不要在 make 循环里做）：
  `python3 $HPD/scripts/aiw_hpd.py pano <设计器.html> -o <交付目录> [--size 3072]` → 每个房间站中间拍一张 360° 全景，
  门洞自动变成跳转箭头，右上角平面小地图。约每间 20 秒。产出 `<名>-全景漫游.html`（单文件，直接发）和
  `<名>-全景/`（index.html + 图片，放网站用）。`unlinked` 非空说明有房间没连上，交付时说一句。
  家具越全越好看：先做完第 5 步再出全景。
- **用户自己的全景照片**（全景相机、酷家乐 / 三维家导出的 2:1 全景图；普通照片不行）：
  `python3 $HPD/scripts/aiw_hpd.py pano-photos 1.jpg 2.jpg … -o <目录> --name 我家 --names 客厅,主卧,… [--links "客厅>主卧@35,主卧>客厅@-150"] [--first 客厅]`
  角度＝那张图里门的位置：图片正中 0°，向右为正（-180..180）。用户没说门在哪就先不写 --links（只有底部房间列表），
  看过图后再按门的位置补。不是 2:1 的图会报错，告诉用户要全景图。
- **分享链接**：交付的 .html 在 AIW 网页 / App 里预览时点「分享」，生成别人免登录的只读链接 + 二维码（设计器自动只看模式）。
  不要自己去找网盘、COS 上传。

---

# 上游 SKILL.md（原文，提交 b94183f）

# 户型图 → 装修设计器网页

产出一个单文件 HTML，双击离线打开：界面和功能就是完整的“户型装修设计器”，只是示例户型换成了用户这张图。
模板 `assets/template.html` 由设计器项目打包而来（源码与本 skill 仓库：<https://github.com/ZWD11/house-plan-designer>），**不要改模板**。
你要做的只有一件事：写出准确的 **HouseSpec（户型描述 JSON）**，格式见 `reference/spec-format.md`（**动手前必读**）。

## 环境（首次）

- Python：`pip install numpy pillow opencv-python pymupdf ezdxf pypdf`；Node：在 `scripts/` 下 `npm install`（只装 puppeteer-core）。
- Windows / macOS / Linux / 云端容器通用：浏览器由 `scripts/browser.mjs` 自动找（`CHROME_PATH` → 系统 Chrome / Edge / Chromium → Playwright 缓存），root / 容器自动 `--no-sandbox`；没有显卡时验收自动进慢速模式。细节、npm 源、无显卡怎么验收见 `reference/env-cloud.md`。

## 0. 开工前先问清（列成「待确认清单」，交付时再集中列一次）

1. **一比一还原**图上所有内容（家具、柜子、洁具、符号、电箱）再补没有的，还是只照图放主要家具、其余补最优搭配？默认一比一。
2. **公共区**（电梯厅、楼梯间、强弱电井、风井、管井）还原不还原？默认还原墙体和设备、不布家具。
3. 图上没标的参数：**层高、楼板厚、看不出窗台高的玻璃 / 窗**。问不到就按常规估，写 `heightFrom: "estimated"`、洞口 `unsure`，设计器「批注」页会提醒用户。
4. 两张图（原图 + 设计图）时：结构以哪张为准（默认墙按原图、家具按设计图，见 `route-dual.md`）。
5. 图上**不认识的符号**列出来问，不要静默跳过。

## 1. 选识别路线

| 输入 | 路线（读对应文件） |
|---|---|
| 户型图、截图、扫描件、只有位图的 PDF（`fitz` 200dpi 渲成 PNG） | `reference/route-image.md`：analyze.py + axismap 按标注定位 |
| **黑底 CAD 截图**（模型空间截屏，彩色线） | `route-image.md`「黑底 CAD 截图」：cadshot.py 分色 + 自动找轴网定比例 → `analyze.py --wall-color cyan`；尺寸数字用 `dimocr.py` 读 |
| CAD 矢量 PDF（放大不糊，`get_drawings()` 几千个图元）、DXF | `reference/route-cad.md`：cadvec.py，尺寸按标注数字精确到 mm |
| cadvec 报“没认出墙厚” / 墙只认一半 / 数字炸成线条 / 一页一层的多页 PDF | `route-cad.md`「兜底」：cadpair.py（按线色线宽分层、平行线配墙、灰块认柱、多页共用原点） |
| 一页并排几层 / 描边墙在每个交接处断开（量房软件） | `reference/route-building.md`：cadsplit.py |
| 装修公司整套“家具布置图”DXF（几张图、块套块、乱码） | `reference/route-dxf-layout.md`：dxf_prepare + dxfinventory + dxfaudit |
| **原图 + 装修平面布置图两张图**（墙按原图、家具按设计图） | `reference/route-dual.md`：dimticks.py + align2.py 自动对位 |
| DWG | 让用户另存 DXF / 导出 SVG（最准），再走上面 CAD / 整栋楼路线 |

楼盘类型的补充：多层 / 复式 / 楼梯 → `route-building.md`；写字楼、培训、核心筒 → `route-office.md`；独栋别墅（车库、庭院、挑空、电梯）→ `route-villa.md`。
CAD 画法、**平面布置图图例**（三次实战读错的 30 个符号）、结构形式与承重判定 → `reference/cad-conventions.md`（**走 CAD 路线或图上有家具时必读**）。

## 2. 读图：先出「图上符号 → 建模」清单

逐块放大看全图（拿不准放大 4 倍），记下：总尺寸和**每一道**分段尺寸链、房间名、门（开向、门轴、门外是不是户内）、窗型、承重墙 / 柱 / 烟道 / 管井 / 立管 / 地漏、电箱表箱、空调位百叶、指北针、标注面积，以及每一件家具和设备符号。
把它整理成表（符号、位置、数量、对应 key / 做法），交付时附上（见 `reference/furnishing.md`）。图没有任何尺寸时问用户要一个已知尺寸，交付时说明比例是估的。

## 3. 写 HouseSpec

用 Python 生成脚本写（参考 `reference/example-gen*.py`），不要手写 JSON；识别脚本出的 `draft.json` / `building_draft.json` 读进来再补改，不要重写墙。
- 像素 → 毫米用 `axismap.Axis` 按标注定位，不要整体乘一个比例（`route-image.md`）。
- 尺寸链全部写进 spec 的 `dims` / `expectDims`，check 自动逐刻度对账（`workflow-check.md`）。
- 承重按结构形式判断，推断的写 `bearingFrom: "inferred"`；拿不准的写 `comments`（`spec-format.md`「批注与待确认」）。
- **生成脚本里注释一律独占一行，不用行尾注释**：用脚本替换代码时把注释插进行中间会吞掉后半行（实战里静默少过一面墙）。build.py 墙数比上次少会报 `[warn]`。

## 4. 生成并自查（循环直到 0 error 0 warn）

```bash
python scripts/build.py <工作目录>/spec.json -o <工作目录>/check.html
node scripts/check.mjs <工作目录>/check.html <工作目录>/check
```

逐项看 `report.json`（悬空墙端、被吞的墙、斜墙、丢掉的门窗、房间面积、`dimIssues` 尺寸链超差、`dimConflicts` 图纸自相矛盾、`issues`），一定要看 `overlay.png` / `plan2d.png` / `view3d.png`。细则见 `reference/workflow-check.md`。
**交付前固定跑三个对账**：墙体 `wallaudit.py`（全楼，玻璃 / 洞口 / 柱认错能查出）、尺寸链 `dims`、家具 `furnoverlay.py` 叠图 + 设计器自查的摆放检查（窗帘活动区、推拉门轨道、高柜挡窗）。用户说“整栋重新检查”时跑全套，不只修指出的那一处。
用户指出某处 3D 不对时先 `node scripts/closeup.mjs 设计器.html x y 方向 out.png` 拍近景再改。

## 5. 布置家具、家电和水电（默认要做）

原图画了的一比一还原（类型、位置、尺寸、朝向），同时门扇打得开、每扇门都有 ≥ 700 通道；没画的按房间功能补全套家具家电；`autoElec: true` 自动布水电点位和管线。
读图要点、家具库（含新增的立管 / 空调外机 / 百叶 / 表箱 / 斜角开放格 / 模块沙发 / 飘窗台 / 摆件 / U 形橱柜 `kit.d2` / 无门挂衣区 `hangOpen` / 双跑扇形楼梯 `stairDogleg`）、按现实装修的常理摆、水电规则 → `reference/furnishing.md`、`reference/mep-rules.md`。
**`[error]` 和 `[warn]` 必须清零才能交付**（用户把“检查”页的橙色标记当错误）；确实改不掉的（原图就这么画）交付时逐条说明。

## 6. 交付

```bash
python scripts/build.py <工作目录>/spec.json -o <图片同目录>/<户型名>-装修设计器.html --zip
node scripts/qa.mjs <户型名>-装修设计器.html <工作目录>/qa --lite --quick      # 全功能验收；无显卡自动慢速模式
python scripts/qa_files.py <工作目录>/qa                                        # 导出的文件能不能打开（0 个文件 = 失败）
```

- qa 全部通过（⚠ 未验证的项人工看截图）才能交付，**一定人工过一遍 `qa/shots/`**。验收参数、判定规则、不要和渲染同时跑等见 `reference/delivery.md`。
- 交付给用户：HTML 路径 + zip（HTML、原图、生成脚本、spec、截图，用户问“怎么下载”就给它）、`plan2d.png` / `view3d.png`、**待确认清单**、三个对账结果、「图上符号 → 建模」表、和原图可能不一致的地方、比例是否估算、承重判断依据；用法说明见 `delivery.md`。

## 更新模板

设计器项目改了功能后，在项目目录 `npm run skill:sync`（打包并复制到本 skill 的 `assets/template.html`，同时更新 `reference/catalog-sizes.json`）。
读取预设户型的代码在项目 `src/model/spec.ts`（`planFromSpec`、`window.__PRESET__`）和 `src/app.ts`（`initApp` / `samplePlan`）；自查规则在 `src/checks.ts`，自动水电在 `src/model/autoelec.ts`。
