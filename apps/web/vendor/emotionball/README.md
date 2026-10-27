# Emotion Ball · 球球 (dark theme) — vendored, NON-COMMERCIAL

Source: https://github.com/sam70361/aora-bot — `emotion-ball/` directory, commit e3b6148 (2026-08-25).
Copyright (c) 2026 sam70361. `LICENSE` (Emotion Ball Community License) and `NOTICE.md` are copied unmodified
from the repository root and apply to everything in this folder.

**Licence summary — read `LICENSE` and `NOTICE.md` for the binding text:**

- The 球球 ball character's **visual design** (blob body, colours, ribbon/confetti visuals, overall look) is for
  **personal technical learning and research only. Any commercial use is prohibited**, and the author offers no
  commercial licence for it.
- The engine code and emotion data are free for non-commercial use with attribution; commercial use needs a licence
  from the author (1251579308@qq.com) *and* a different, lawfully licensed character design.
- AIW includes it at the repository owner's explicit request for personal use. If you fork AIW for anything
  commercial, remove this folder and the 表情助手 feature (or swap in your own character).

Files copied unchanged from `emotion-ball/js/`:

| file | role |
| --- | --- |
| rings.js | eye-ring and body-shape geometry (data) |
| emotions.js | the 32 emotion definitions (IDs 00-41, public contract) |
| ball.js | SVG renderer |
| engine.js | state machine + `EmotionBall.create` |

Load order is fixed: rings → emotions → ball → engine. Not included: the gallery site (app.js, i18n.js,
hero-particles.js, css) and the screenshots.

The 暗黑 (dark) look of the upstream gallery is reproduced in AIW's own CSS, not by changing these files: the
default cream body `#F3F0EA` with `#1A1A1A` eyes on the gallery's `#09090b` backdrop, a soft violet glow behind
the ball (`rgba(145,109,255,.16)`) and a drop shadow under it.

AIW's own glue lives outside this folder: `apps/web/mate-map.js` (task event → emotion ID), `apps/web/mate.js` +
`mate.css` (web), `apps/web/mate.html` (page embedded by the iOS app). 设置 › 关于 › 表情助手 turns it off.
