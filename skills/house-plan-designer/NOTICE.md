# NOTICE

本目录基于 https://github.com/ZWD11/house-plan-designer （MIT License, Copyright (c) 见 LICENSE），
取自提交 58cef1d99c4662ac95d50913dd424547c6559670（2026-10-28）。

AIW 的改动：
- SKILL.md 顶部加「AIW 内置版说明」，上游正文原样保留；
- 新增 scripts/aiw_hpd.py（Linux VM 依赖安装、cad-drawing spec → HouseSpec、build+check 摘要、验收封装）；
- check.mjs / qa.mjs / qa_furniture.mjs / qa_styles.mjs / walktest.mjs：支持 CHROME_PATH 与 Linux 的 Chrome/Chromium 路径，
  非 Windows 用 SwiftShader，root 下加 --no-sandbox；check.mjs 等待设计器就绪的超时从 15s 放宽到 90s；
- 去掉了大部分示例截图（只留 spec-format.md 引用的 example-input.jpg / example-plan2d.png）。
设计器模板 assets/template.html 未改动。
