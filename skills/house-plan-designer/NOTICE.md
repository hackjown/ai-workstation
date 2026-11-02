# NOTICE

本目录基于 https://github.com/ZWD11/house-plan-designer （MIT License, Copyright (c) 见 LICENSE），
取自提交 b94183f（2026-11-02 13:08，跨平台运行、黑底截图识别、CAD 兜底、尺寸数字 OCR）；AIW v3.9.8 首次引入时为 58cef1d。

AIW 的改动：
- SKILL.md 顶部加「AIW 内置版说明」，上游正文原样保留；
- 新增 scripts/aiw_hpd.py（Linux VM 依赖安装、cad-drawing spec → HouseSpec、build+check 摘要、验收封装）；
- 浏览器启动用上游 scripts/browser.mjs（已支持 CHROME_PATH、Linux、root/容器 --no-sandbox、SwiftShader），AIW 不再改各脚本；
  只有 check.mjs 等待设计器就绪的超时从 15s 放宽到 90s；
- 新增 scripts/pano.mjs、assets/pano-viewer.html（360° 全景漫游与只读分享，v3.9.13）；
- 去掉了大部分示例截图（只留 spec-format.md 引用的 example-input.jpg / example-plan2d.png）。
设计器模板 assets/template.html 未改动。
