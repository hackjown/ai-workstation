# Mood Mates · 云宝 (Nimbo) — vendored

Source: https://github.com/sam70361/aora-bot — `mood-mates/` directory, commit e3b6148 (2026-08).
Copyright (c) 2026 sam70361. Licensed under the **Mood Mates Community License (Non-Commercial)**, see `LICENSE`
in this folder (kept unmodified). Free for personal learning / non-commercial use with attribution; commercial use
needs a separate licence from the author (1251579308@qq.com).

Files copied unchanged from `mood-mates/src/`:

| file | from |
| --- | --- |
| geometry.js | src/core/geometry.js |
| render.js | src/core/render.js |
| features.js | src/core/features.js |
| fx.js | src/core/fx.js |
| emotions.js | src/data/emotions.js |
| engine.js | src/core/engine.js |
| nimbo.js | src/characters/nimbo.js (云宝 only; 亮亮 Twinkle not included) |

Load order is fixed: geometry → render → features → fx → emotions → engine → nimbo.

Not used: the `emotion-ball/` ball characters (blob / wedge / gem) of the same repository — their visual designs are
under a separate no-commercial-use notice and are deliberately **not** included in AIW.

AIW's own glue lives outside this folder: `apps/web/mate-map.js` (task event → emotion ID), `apps/web/mate.js` +
`mate.css` (web), `apps/web/mate.html` (page embedded by the iOS app). 设置 › 关于 › 表情助手 turns it off.
