---
name: scan-3d
title: 扫描建模
description: 把 iPhone「3D Model Anything」扫描导出的房间 zip（house-capture 格式，含 manifest.json、mesh/room-N.ply、photos/）变成能看的 3D 模型：按 RoomPlan 重新建模（USDZ 给 iPhone 预览/AR、GLB 可编辑）+ 扫描网格 GLB + 手机能直接打开的离线 3D 网页 + 房间尺寸摘要；可选瘦身（照片重压、去掉深度帧）。精修成可编辑 Three.js 场景只在用户要时做。触发词：扫描建模、房间扫描、LiDAR、3D Model Anything、house-capture、scan zip、建模、三维模型、GLB。
---

# 扫描建模（scan-3d）

用户在 iPhone 上用「3D Model Anything」扫房间，在 App 里点 Share room，把 `.zip` 分享给 Aiw。
文件在 `~/workspace/uploads/<名>.zip`（ATTACHMENTS 一节给出确切路径）。脚本：`~/.claude/skills/scan-3d/scan3d.py`。

## 第 1 步 · 出模型（一定做，几十秒）

```bash
python3 ~/.claude/skills/scan-3d/scan3d.py build ~/workspace/uploads/<名>.zip
```

- 解压到 `~/workspace/scan3d/<名>/src/`，每个房间：读 LiDAR 网格 → 只留本房间 → 简化到 ≤ 30 万面（`--faces` 可调）
  → 写 `<名>.glb`（多房间时 `<名>-room-N.glb`），再写一个离线网页 `<名>-3d.html`（不用 CDN，手机可直接打开：
  拖动旋转、双指缩放和平移、俯视、剖切滑块去掉天花板看内部）和 `<名>-summary.md`（尺寸、墙门窗、识别到的家具）。
- 同时按 RoomPlan（`rooms/room-N.json`）**重新建模**：墙（门窗处开洞）、门、窗和窗框、地面、家具（沙发、桌、椅、床做成
  有形状的几件，其他按尺寸做成盒子），每件一个带名字的部件（Wall_1、Sofa_1…）。写 `<名>-model.usdz`（iPhone 点开就是
  原生 3D 预览，右上角能切 AR 摆到桌上；需要 usd-core，安装时自动装）和 `<名>-model.glb`（Blender / three.js editor
  里每件都能单独移动、隐藏、改色）。离线网页第一个视图就是「建模」，下拉切到「扫描」看原始网格。
  App 自己存的 `rooms/room-N.usdz`（Apple RoomPlan 参数化导出）如果在 zip 里，原样另存为 `<名>-roomplan.usdz`。
- 每个房间画一张**平面图**：`<名>-plan.png`（手机直接看）和 `<名>-plan.svg`（可编辑、无限放大）：墙（门窗处断开）、门和开门方向、
  窗、家具轮廓和名字、每面墙的长度、总尺寸、地面面积（RoomPlan 有地面多边形时）、1 m 比例尺。`--no-plan` 关掉。
- 每个 USDZ 都会做**合规检查**（OpenUSD UsdValidation：usdz 打包、核心、几何、材质校验器，再加快速查看的规则：Y 轴向上、
  metersPerUnit、defaultPrim、只用 UsdPreviewSurface），摘要里写「通过，iPhone 可以打开」或没通过的原因。没通过就别只发
  USDZ，先说明原因。
- 最后一行 `SEND:` 列出要交付的文件，逐个 send_file（网页放第一个）。
- 输出 `NO_SCAN`：不是这个 App 的导出，用 ask_user 说明需要在 App 里点 Share room 导出的 zip，不要猜。
- 输出 `NO_MESH`：只有 RoomPlan 数据没有网格，交付摘要并说明需要重新扫描。
- 摘要里写「没有照片颜色缓存」：模型按墙/地/家具上色。告诉用户在 App 里先打开一次 3D 预览再分享，就有照片颜色。
- 不要自己写 PLY 解析、Open3D、Blender 脚本代替这一步，也不要先去读几百张照片。

done 里写：平面图、USDZ 检查结果、建模出了哪些部件、房间尺寸、层高、墙门窗数量、家具、GLB 大小、解压后多大。

## 第 2 步 · 瘦身（用户提到太大 / 要存档 / 要瘦身时做）

```bash
python3 ~/.claude/skills/scan-3d/scan3d.py slim ~/workspace/scan3d/<名>/src     # --keep-depth 保留深度帧
```

照片按原尺寸用质量 82 重存（保留 EXIF，相机位姿和内参仍然有效），删掉 `depth/`（只有可选的深度融合会用）和
`.arworldmap`（只有手机续扫要用，原 zip 仍在手机上）。生成 `~/workspace/scan3d/<名>/scan-slim.zip`，打印前后大小。
瘦身后的 zip 只在用户要时 send_file。`scan3d.py info <zip>` 可看 zip 里各部分占多少。

## 第 3 步 · 精修成可编辑场景（只有用户明确要求时）

用户说「做成可编辑的」「精修」「重建成 three.js 场景」才做。这是一个长任务，需要多轮对比照片：
1. `git clone --depth 1 https://github.com/wesbos/3d-model-anything ~/workspace/3d-model-anything`（已存在就 `git pull`）；
2. 把第 1 步解压出的会话文件夹（含 manifest.json 的那个）复制到 `~/workspace/3d-model-anything/data/captures-in/<名>/`；
3. 读 `.agents/skills/scan-to-threejs/SKILL.md` 和 `references/house-capture.md`，按它的流程做（bundle_to_room.py →
   prepare_photos.py → 搭墙体和家具 → 同机位截图和原照片对比 → 改 → 再对比）。依赖：python3、Pillow、NumPy，
   可选 Open3D；`npm ci` 需要 Node 22.12+。
4. 每完成一轮就截图对比并 send_file，问用户哪里不像，再改。不要一次做完不给看。

## 注意

- 坐标单位米，+Y 朝上（ARKit 世界坐标），GLB 直接可用（Blender、Three.js、model-viewer）。
- 原始 zip 留在 `~/workspace/uploads/`，不要删除。
- 用户的房间照片是隐私，不要上传到任何公开地址。
