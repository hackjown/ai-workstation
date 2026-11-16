# NOTICE

本目录基于 https://github.com/ZWD11/house-plan-designer （MIT License, Copyright (c) 见 LICENSE），
取自提交 dd5a610（2026-11-13 18:03，图片拉正 rectify、反向对账 coverage、门开向 doorcheck、回归基准 bench；含 5f55f7f 吊顶灯带与自由造型、d4e7374 家具揉捏变形与排灯）；
AIW v3.9.51 起由 b94183f 升级，v3.9.16 为 b94183f，v3.9.8 首次引入时为 58cef1d。

AIW 的改动：
- SKILL.md 顶部加「AIW 内置版说明」，上游正文原样保留；
- 新增 scripts/aiw_hpd.py（Linux VM 依赖安装、cad-drawing spec → HouseSpec、build+check 摘要、验收封装）；
- 浏览器启动用上游 scripts/browser.mjs（已支持 CHROME_PATH、Linux、root/容器 --no-sandbox、SwiftShader），AIW 不再改各脚本；
  只有 check.mjs 等待设计器就绪的超时从 15s 放宽到 90s；
- 新增 scripts/pano.mjs、assets/pano-viewer.html（360° 全景漫游与只读分享，v3.9.13）；
- aiw_hpd.py ensure 多装 scipy（上游 rectify.py 需要）；
- 去掉了大部分示例截图（只留 spec-format.md 引用的 example-input.jpg / example-plan2d.png）。
设计器模板 assets/template.html 未改动。
