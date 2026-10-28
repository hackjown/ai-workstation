/* Aiw touch takeover (3.7): precise control of the remote desktop (noVNC) from a phone.
 *
 * Used by the web app (app.js: the takeover iframe on a touch screen) and by the iOS app (takeover.html, loaded into
 * the takeover WKWebView with the Aiw server as its origin). Both put the noVNC page (/desktop/<id>/vnc.html, same
 * origin as the Aiw server) in an iframe; this layer covers the iframe and turns touches into mouse / wheel / key
 * events dispatched on noVNC's canvas, which noVNC then sends over its RFB socket like a real mouse:
 *
 *   触控板 (trackpad, default on phones): a visible cursor. One finger moves it relatively, with acceleration (slow =
 *     precise, fast = far); tap = left click at the cursor; two-finger tap = right click; long press, or tap then
 *     drag, = drag / select (button held until the finger lifts); two-finger drag = scroll.
 *   直点 (direct): the click lands where you touch. While the finger is down a loupe shows the desktop under it,
 *     magnified, with a crosshair; slide to aim, the click happens on release at that exact point.
 *   Both: pinch to zoom (1x-5x) and pan the desktop (two fingers). The zoom makes the iframe really bigger (its
 *     layout size, not a CSS scale): noVNC (resize=scale) then draws the desktop bigger and maps every CSS pixel to
 *     fewer framebuffer pixels, so zooming in really is more precise (a mouse event's clientX is a whole CSS pixel).
 *   Toolbar: mode switch, keyboard (a hidden input; typed text becomes key presses), Esc, Tab, Ctrl (sticky, applies
 *   to the next key), Enter, arrows, ⌫, zoom reset, 横屏 (landscape full screen) and its exit.
 *
 * Coordinates: S = point in the stage's padding box (the overlay's own frame; un-rotated when the stage is
 * CSS-rotated for landscape), z / (px, py) = zoom and pan. The iframe sits at the stage origin, z times the stage size,
 * moved by translate(px, py), so the iframe-internal client point is c = S - pan (rounded to a whole pixel, as a real
 * mouse event is). noVNC maps that client point to framebuffer pixels itself (its canvas is inside the iframe), so the
 * zoom, noVNC's own scale-to-fit and the rotation all compose correctly. The cursor is kept as a fraction of the
 * canvas (u, v), so it stays on the same desktop spot when the zoom changes; it is drawn at S = c + pan.
 *
 * A viewer on another origin (InstaBox cloud computers) cannot be reached from here: then the layer steps aside
 * (touches go to that viewer as before) and says so; landscape still works.
 */
(function () {
  "use strict";
  var CSS = "" +
    ".aiwt-stage{position:relative;overflow:hidden;touch-action:none;-webkit-user-select:none;user-select:none;background:#000}" +
    ".aiwt-stage>iframe{position:absolute;left:0;top:0;width:100%;height:100%;border:0;transform-origin:0 0}" +
    ".aiwt-ov{position:absolute;inset:0;z-index:5;touch-action:none;-webkit-touch-callout:none;-webkit-tap-highlight-color:transparent}" +
    ".aiwt-ov.pass{pointer-events:none}" +
    ".aiwt-cur{position:absolute;left:0;top:0;width:22px;height:22px;margin:-1px 0 0 -1px;pointer-events:none;z-index:6;" +
    "filter:drop-shadow(0 1px 2px rgba(0,0,0,.6));transition:transform .05s linear}" +
    ".aiwt-cur.drag path{fill:#F0A030}" +
    ".aiwt-loupe{position:absolute;width:124px;height:124px;border-radius:50%;overflow:hidden;pointer-events:none;z-index:7;display:none;" +
    "border:2px solid #F0A030;box-shadow:0 6px 20px rgba(0,0,0,.45);background:#111}" +
    ".aiwt-loupe canvas{width:100%;height:100%;display:block}" +
    ".aiwt-loupe:before,.aiwt-loupe:after{content:'';position:absolute;background:rgba(240,160,48,.95);z-index:1}" +
    ".aiwt-loupe:before{left:50%;top:22%;bottom:22%;width:1.5px;margin-left:-.75px}" +
    ".aiwt-loupe:after{top:50%;left:22%;right:22%;height:1.5px;margin-top:-.75px}" +
    ".aiwt-dot{position:absolute;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;border:2px solid #F0A030;pointer-events:none;z-index:6;display:none}" +
    ".aiwt-bar{position:absolute;left:50%;bottom:10px;transform:translateX(-50%);z-index:8;display:flex;gap:4px;align-items:center;" +
    "max-width:calc(100% - 16px);overflow-x:auto;scrollbar-width:none;padding:5px;border-radius:14px;background:rgba(20,17,13,.86);" +
    "border:1px solid rgba(240,160,48,.55);box-shadow:0 6px 18px rgba(0,0,0,.35);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}" +
    ".aiwt-bar::-webkit-scrollbar{display:none}" +
    ".aiwt-bar button{flex:none;min-width:34px;height:32px;padding:0 9px;border:0;border-radius:9px;background:rgba(255,255,255,.08);" +
    "color:#f3ede4;font:600 13px -apple-system,system-ui,sans-serif;white-space:nowrap;touch-action:manipulation}" +
    ".aiwt-bar button.on{background:#F0A030;color:#2A1A05}" +
    ".aiwt-bar .sep{flex:none;width:1px;height:20px;background:rgba(255,255,255,.18);margin:0 2px}" +
    ".aiwt-bar.min>:not(.keep){display:none}" +
    ".aiwt-note{position:absolute;left:50%;top:48px;transform:translateX(-50%);z-index:8;padding:6px 12px;border-radius:999px;" +
    "background:rgba(20,17,13,.86);border:1px solid rgba(240,160,48,.55);color:#f3ede4;font:500 12px -apple-system,system-ui,sans-serif;" +
    "pointer-events:none;opacity:0;transition:opacity .25s;white-space:nowrap;max-width:90%;overflow:hidden;text-overflow:ellipsis}" +
    ".aiwt-note.on{opacity:1}" +
    /* the web app's 你正在操作桌面 banner: readable on the desktop picture, and only its button takes touches */
    ".aiwt-stage .takebanner{pointer-events:none;background:rgba(20,17,13,.82)!important;color:#f3ede4!important;border:1px solid rgba(240,160,48,.55);z-index:9}" +
    ".aiwt-stage .takebanner button{pointer-events:auto}" +
    ".aiwt-stage .vfchip{display:none}" +
    ".aiwt-kb{position:absolute;left:0;bottom:0;width:1px;height:1px;opacity:0;border:0;padding:0;font-size:16px;z-index:0}" +
    /* landscape: real fullscreen (+ orientation lock) where the browser has it, else this rotated full-screen view */
    ".aiwt-full{position:fixed!important;left:0;top:0;width:100vw!important;height:100vh!important;z-index:2147483000;border-radius:0!important;margin:0!important;aspect-ratio:auto!important}" +
    ".aiwt-rot{position:fixed!important;left:100vw;top:0;width:100vh!important;height:100vw!important;transform:rotate(90deg);transform-origin:0 0;" +
    "z-index:2147483000;border-radius:0!important;margin:0!important;aspect-ratio:auto!important}";

  var CURSOR = '<svg viewBox="0 0 22 22" width="22" height="22"><path d="M2 1.5 L2 17.5 L6.3 13.6 L9.2 20.2 L12 19 L9.2 12.6 L15 12.6 Z" ' +
    'fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg>';
  var KEYS = {
    Escape: { key: "Escape", code: "Escape", keyCode: 27 }, Tab: { key: "Tab", code: "Tab", keyCode: 9 },
    Enter: { key: "Enter", code: "Enter", keyCode: 13 }, Backspace: { key: "Backspace", code: "Backspace", keyCode: 8 },
    ArrowLeft: { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 }, ArrowUp: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
    ArrowRight: { key: "ArrowRight", code: "ArrowRight", keyCode: 39 }, ArrowDown: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
    Control: { key: "Control", code: "ControlLeft", keyCode: 17 }
  };
  var TAP_MS = 260, TAP_MOVE = 9, HOLD_MS = 450, ZMAX = 5;

  function css(doc) {
    if (doc.getElementById("aiwt-css")) return;
    var s = doc.createElement("style"); s.id = "aiwt-css"; s.textContent = CSS; doc.head.appendChild(s);
  }
  function el(doc, tag, cls, html) { var e = doc.createElement(tag); if (cls) e.className = cls; if (html) e.innerHTML = html; return e; }

  /** attach(stage, frame, opts) -> controller. stage: the element around the iframe (made position:relative).
   *  opts: { mode: "trackpad"|"direct", native: bool (iOS: landscape / exit go to the app), onLandscape(on), onExit() } */
  function attach(stage, frame, opts) {
    opts = opts || {};
    var doc = stage.ownerDocument, win = doc.defaultView;
    css(doc);
    stage.classList.add("aiwt-stage");
    var ov = el(doc, "div", "aiwt-ov"), cur = el(doc, "div", "aiwt-cur", CURSOR), dot = el(doc, "div", "aiwt-dot");
    var loupe = el(doc, "div", "aiwt-loupe"), lc = el(doc, "canvas"), bar = el(doc, "div", "aiwt-bar"), note = el(doc, "div", "aiwt-note");
    var kb = el(doc, "input", "aiwt-kb");
    kb.setAttribute("autocapitalize", "off"); kb.setAttribute("autocomplete", "off"); kb.setAttribute("autocorrect", "off");
    kb.setAttribute("spellcheck", "false"); kb.setAttribute("aria-label", "键盘输入");
    loupe.appendChild(lc);
    [ov, cur, dot, loupe, bar, note, kb].forEach(function (e) { stage.appendChild(e); });

    var st = {
      mode: opts.mode || (localStorage.getItem("aiw_touch_mode") || "trackpad"),
      z: 1, px: 0, py: 0, u: -1, v: -1, ctrl: false, dragging: false, landscape: false, rot: false, pass: false, gone: false
    };
    var touches = {}, g = null, lastTap = 0, lastTapAt = { x: 0, y: 0 }, holdT = 0, raf = 0, pendMove = false, wheelAcc = 0;

    // ---- the noVNC page inside the iframe
    function idoc() { try { return frame.contentDocument; } catch (e) { return null; } }
    function canvas() {
      var d = idoc(); if (!d) return null;
      var c = d.querySelector("#noVNC_container canvas, #noVNC_screen canvas");
      if (c) return c;
      var best = null, area = 0;
      Array.prototype.forEach.call(d.querySelectorAll("canvas"), function (k) {
        var r = k.getBoundingClientRect(), a = r.width * r.height; if (a > area) { area = a; best = k; }
      });
      return best;
    }
    function crect() {   // the canvas in iframe client coordinates (c space), or the whole iframe until it exists
      var c = canvas();
      if (c) { var r = c.getBoundingClientRect(); if (r.width > 4) return { l: r.left, t: r.top, w: r.width, h: r.height }; }
      return { l: 0, t: 0, w: frame.clientWidth || stage.clientWidth, h: frame.clientHeight || stage.clientHeight };
    }
    function checkReach() {
      var d = idoc(), was = st.pass;
      st.pass = !d;   // another origin: step aside
      ov.classList.toggle("pass", st.pass);
      if (st.pass && !was) say("这台电脑的桌面不支持精确触控：直接点画面操作");
      paint();
    }
    // hide noVNC's own control bar handle (the toolbar here replaces it) once the page is there
    function tidy() {
      var d = idoc(); if (!d || d.getElementById("aiwt-tidy")) return;
      var s = d.createElement("style"); s.id = "aiwt-tidy";
      s.textContent = "#noVNC_control_bar_anchor,#noVNC_control_bar_hint{display:none!important}";
      (d.head || d.documentElement).appendChild(s);
    }
    frame.addEventListener("load", function () { checkReach(); tidy(); });

    // ---- geometry
    function W() { return stage.clientWidth; } function H() { return stage.clientHeight; }
    /** client (viewport) point -> stage point S, un-rotating the CSS-rotated landscape view */
    function toS(x, y) {   // relative to the padding box (where the iframe and the overlay sit), not the border
      var r = stage.getBoundingClientRect(), bl = stage.clientLeft || 0, bt = stage.clientTop || 0;
      if (st.rot && win.getComputedStyle(stage).transform !== "none") return { x: y - r.top - bl, y: r.right - x - bt };
      return { x: x - r.left - bl, y: y - r.top - bt };
    }
    function toC(s) { return { x: s.x - st.px, y: s.y - st.py }; }
    function fromC(c) { return { x: c.x + st.px, y: c.y + st.py }; }
    function snap(c) { return { x: Math.round(c.x), y: Math.round(c.y) }; }
    /** the cursor in c space (from its place on the canvas) */
    function cur0() { var r = crect(); return snap({ x: r.l + st.u * r.w, y: r.t + st.v * r.h }); }
    function setCur(c) { var r = crect(); st.u = Math.max(0, Math.min(1, (c.x - r.l) / Math.max(1, r.w))); st.v = Math.max(0, Math.min(1, (c.y - r.t) / Math.max(1, r.h))); }
    function clampPan() {
      var w = W(), h = H();
      st.px = Math.min(0, Math.max(w - st.z * w, st.px)); st.py = Math.min(0, Math.max(h - st.z * h, st.py));
    }
    function ensureCursor() { if (st.u < 0) { st.u = 0.5; st.v = 0.5; } }
    /** zoomed in: keep the cursor on screen by panning (trackpad) */
    function follow() {
      if (st.z <= 1.001) return;
      var s = fromC(cur0()), m = 36, w = W(), h = H();
      if (s.x < m) st.px += m - s.x; else if (s.x > w - m) st.px -= s.x - (w - m);
      if (s.y < m) st.py += m - s.y; else if (s.y > h - 70) st.py -= s.y - (h - 70);
      clampPan();
    }
    function paint() {
      var zoomed = st.z > 1.001;
      frame.style.width = zoomed ? Math.round(W() * st.z) + "px" : "";
      frame.style.height = zoomed ? Math.round(H() * st.z) + "px" : "";
      frame.style.transform = zoomed || st.px || st.py ? "translate(" + st.px + "px," + st.py + "px)" : "";
      var show = st.mode === "trackpad" && !st.pass && st.u >= 0;
      cur.style.display = show ? "block" : "none";
      if (show) { var s = fromC(cur0()); cur.style.transform = "translate(" + s.x + "px," + s.y + "px)"; }
      cur.classList.toggle("drag", st.dragging);
      zoomBtn.style.display = st.z > 1.001 ? "" : "none";
    }

    // ---- events into noVNC
    function fire(type, c, button, buttons) {
      var cv = canvas(), d = idoc(); if (!cv || !d) return false;
      c = snap(c); st.last = { type: type, x: c.x, y: c.y };
      var v = d.defaultView, E = v.MouseEvent;
      cv.dispatchEvent(new E(type, { bubbles: true, cancelable: true, view: v, clientX: c.x, clientY: c.y,
        screenX: c.x, screenY: c.y, button: button || 0, buttons: buttons == null ? 0 : buttons }));
      return true;
    }
    function move(c) { return fire("mousemove", c, 0, st.dragging ? 1 : 0); }
    function click(c, button) {
      button = button || 0; var mask = button === 2 ? 2 : 1;
      move(c); fire("mousedown", c, button, mask); fire("mouseup", c, button, 0);
      if (button === 2) fire("contextmenu", c, 2, 0);
    }
    function wheel(c, dy, dx) {
      var cv = canvas(), d = idoc(); if (!cv || !d) return;
      var v = d.defaultView;
      cv.dispatchEvent(new v.WheelEvent("wheel", { bubbles: true, cancelable: true, view: v, clientX: c.x, clientY: c.y,
        deltaX: dx || 0, deltaY: dy, deltaMode: 0 }));
    }
    function key(name, down) {
      var cv = canvas(), d = idoc(); if (!cv || !d) return;
      var k = KEYS[name] || { key: name, code: codeOf(name), keyCode: name.length === 1 ? name.toUpperCase().charCodeAt(0) : 0 };
      var v = d.defaultView;
      ["keydown", "keyup"].forEach(function (t) {
        if (down === true && t === "keyup") return; if (down === false && t === "keydown") return;
        var e = new v.KeyboardEvent(t, { bubbles: true, cancelable: true, key: k.key, code: k.code, ctrlKey: st.ctrl && name !== "Control" });
        try { Object.defineProperty(e, "keyCode", { get: function () { return k.keyCode; } });
              Object.defineProperty(e, "which", { get: function () { return k.keyCode; } }); } catch (x) { /* read-only */ }
        cv.dispatchEvent(e);
      });
    }
    function codeOf(ch) {
      if (/^[a-z]$/i.test(ch)) return "Key" + ch.toUpperCase();
      if (/^[0-9]$/.test(ch)) return "Digit" + ch;
      if (ch === " ") return "Space";
      return "";
    }
    /** one key press; with Ctrl on it is Ctrl+key and Ctrl turns off again */
    function press(name) {
      if (st.ctrl && name !== "Control") { key("Control", true); key(name); key("Control", false); setCtrl(false); }
      else key(name);
    }
    function typeText(t) { for (var i = 0; i < t.length; i++) { var ch = t[i]; press(ch === "\n" ? "Enter" : ch); } }

    // ---- loupe (direct mode)
    function showLoupe(s) {
      var cv = canvas(); if (!cv) { loupe.style.display = "none"; return; }
      var w = W(), h = H(), dpr = win.devicePixelRatio || 1;
      var size = Math.round(Math.max(72, Math.min(124, h * 0.42, w * 0.42))), gap = 34;
      // above the finger, else below, else beside it (left / right), else wherever it fits best in the stage
      var cand = [[s.x - size / 2, s.y - size - gap], [s.x - size / 2, s.y + gap], [s.x - size - gap, s.y - size / 2], [s.x + gap, s.y - size / 2]];
      var fit = cand.filter(function (q) { return q[0] >= 4 && q[1] >= 4 && q[0] + size <= w - 4 && q[1] + size <= h - 4; })[0] || cand[0];
      var lx = Math.max(4, Math.min(w - size - 4, fit[0])), ly = Math.max(4, Math.min(h - size - 4, fit[1]));
      loupe.style.width = loupe.style.height = size + "px";
      loupe.style.display = "block"; loupe.style.left = lx + "px"; loupe.style.top = ly + "px";
      dot.style.display = "block"; dot.style.left = s.x + "px"; dot.style.top = s.y + "px";
      if (lc.width !== Math.round(size * dpr)) { lc.width = lc.height = Math.round(size * dpr); }
      var c = toC(s), r = crect(), k = cv.width / Math.max(1, r.w);
      var mag = 2.6, areaC = size / mag;                      // the desktop area the loupe shows, in c units
      var sw = areaC * k, sx = (c.x - r.l) * k - sw / 2, sy = (c.y - r.t) * k - sw / 2;
      var x2 = lc.getContext("2d");
      x2.imageSmoothingEnabled = false;
      x2.fillStyle = "#111"; x2.fillRect(0, 0, lc.width, lc.height);
      try { x2.drawImage(cv, sx, sy, sw, sw, 0, 0, lc.width, lc.height); } catch (e) { /* not drawable yet */ }
    }
    function hideLoupe() { loupe.style.display = "none"; dot.style.display = "none"; }

    // ---- gestures
    function pts() { return Object.keys(touches).map(function (k) { return touches[k]; }); }
    function mid(a, b) { return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }; }
    function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
    function onStart(e) {
      if (st.pass) return;
      e.preventDefault();
      if (e.target === kb) return;
      var now = Date.now();
      Array.prototype.forEach.call(e.changedTouches, function (t) { var s = toS(t.clientX, t.clientY); touches[t.identifier] = { x: s.x, y: s.y, x0: s.x, y0: s.y, t: now }; });
      var p = pts();
      ensureCursor();
      if (p.length === 1) {
        var a = p[0];
        g = { kind: "one", t0: now, x0: a.x, y0: a.y, lx: a.x, ly: a.y, lt: now, moved: 0,
              second: now - lastTap < 300 && Math.hypot(a.x - lastTapAt.x, a.y - lastTapAt.y) < 60 };
        clearTimeout(holdT);
        if (st.mode === "trackpad") {
          holdT = setTimeout(function () {   // long press: hold the left button (drag / select)
            if (g && g.kind === "one" && g.moved < TAP_MOVE && !st.dragging) { startDrag(); g.held = true; }
          }, HOLD_MS);
        } else showLoupe(a);
      } else if (p.length === 2) {
        clearTimeout(holdT);
        if (st.dragging && g && g.kind === "one") endDrag();
        hideLoupe();
        var m = mid(p[0], p[1]);
        g = { kind: "two", t0: now, d0: dist(p[0], p[1]), m0: m, lm: m, z0: st.z, px0: st.px, py0: st.py, pinch: false, moved: 0 };
      } else if (g) { g.kind = "many"; }
    }
    function onMove(e) {
      if (st.pass) return;
      e.preventDefault();
      var now = Date.now();
      Array.prototype.forEach.call(e.changedTouches, function (t) {
        var o = touches[t.identifier]; if (!o) return; var s = toS(t.clientX, t.clientY); o.x = s.x; o.y = s.y;
      });
      var p = pts(); if (!g) return;
      if (g.kind === "one" && p.length === 1) {
        var a = p[0], dx = a.x - g.lx, dy = a.y - g.ly, dt = Math.max(1, now - g.lt);
        g.moved = Math.max(g.moved, Math.hypot(a.x - g.x0, a.y - g.y0));
        g.lx = a.x; g.ly = a.y; g.lt = now;
        if (st.mode === "trackpad") {
          if (g.moved < TAP_MOVE && !st.dragging) return;   // still a tap / long press candidate
          if (g.second && !st.dragging && !g.held) startDrag();   // tap, then drag = drag
          var v = Math.hypot(dx, dy) / dt;                          // px / ms
          var gain = 1 + Math.min(2.4, Math.max(0, v - 0.2) * 2.2); // slow 1x (precise) .. fast 3.4x
          var r = crect();                                          // zoomed in, the canvas is bigger: finer steps
          st.u = Math.max(0, Math.min(1, st.u + dx * gain / Math.max(1, r.w)));
          st.v = Math.max(0, Math.min(1, st.v + dy * gain / Math.max(1, r.h)));
          follow(); queueMove();
        } else showLoupe(a);
      } else if (g.kind === "two" && p.length === 2) {
        var m = mid(p[0], p[1]), d = dist(p[0], p[1]);
        g.moved = Math.max(g.moved, Math.hypot(m.x - g.m0.x, m.y - g.m0.y), Math.abs(d - g.d0));
        if (!g.pinch && Math.abs(d / g.d0 - 1) > 0.1) {   // fingers apart / together: zoom (and pan) from here on
          g.pinch = true; g.d0 = d; g.m0 = m; g.z0 = st.z; g.px0 = st.px; g.py0 = st.py;
        }
        if (g.pinch) {
          var z = Math.max(1, Math.min(ZMAX, g.z0 * d / g.d0));
          var fx = (g.m0.x - g.px0) / (W() * g.z0), fy = (g.m0.y - g.py0) / (H() * g.z0);   // the spot under the fingers
          st.z = z; st.px = m.x - fx * W() * z; st.py = m.y - fy * H() * z; clampPan(); paint();
        } else if (g.moved > TAP_MOVE) {   // two-finger drag: scroll (natural direction) at the cursor / fingers
          var sdy = m.y - g.lm.y, sdx = m.x - g.lm.x;
          wheelAcc += -sdy * 2.2;
          if (Math.abs(wheelAcc) >= 1) {
            var at = st.mode === "trackpad" ? cur0() : toC(m);
            wheel(at, wheelAcc, -sdx * 2.2); wheelAcc = 0;
          }
        }
        g.lm = m;
      }
    }
    function onEnd(e) {
      if (st.pass) return;
      e.preventDefault();
      var now = Date.now();
      Array.prototype.forEach.call(e.changedTouches, function (t) { delete touches[t.identifier]; });
      var left = pts().length;
      if (!g) return;
      if (g.kind === "one" && left === 0) {
        clearTimeout(holdT);
        var quick = now - g.t0 < TAP_MS + (g.second ? 120 : 0);
        if (st.mode === "trackpad") {
          if (st.dragging) endDrag();
          else if (g.moved < TAP_MOVE && quick) { click(cur0(), 0); lastTap = now; lastTapAt = { x: g.x0, y: g.y0 }; }
        } else {
          var c = toC({ x: g.lx, y: g.ly }); hideLoupe();
          var r = crect();
          if (c.x >= r.l && c.y >= r.t && c.x < r.l + r.w && c.y < r.t + r.h) { setCur(c); click(c, 0); }
        }
        g = null;
      } else if (g.kind === "two") {
        // fingers rarely lift together: the gesture is judged when the last one lifts; the one left down after a
        // pinch / scroll does nothing
        if (left === 0) {
          if (!g.pinch && g.moved < TAP_MOVE * 1.5 && now - g.t0 < TAP_MS + 120) {   // two-finger tap: right click
            click(st.mode === "trackpad" ? cur0() : toC(g.m0), 2);
          }
          g = null;
        }
      } else if (left === 0) g = null;
    }
    function startDrag() { st.dragging = true; move(cur0()); fire("mousedown", cur0(), 0, 1); paint(); }
    function endDrag() { st.dragging = false; fire("mouseup", cur0(), 0, 0); paint(); }
    function queueMove() {
      paint();
      if (pendMove) return; pendMove = true;
      raf = win.requestAnimationFrame(function () { pendMove = false; move(cur0()); });
    }
    ov.addEventListener("touchstart", onStart, { passive: false });
    ov.addEventListener("touchmove", onMove, { passive: false });
    ov.addEventListener("touchend", onEnd, { passive: false });
    ov.addEventListener("touchcancel", function (e) {
      touches = {}; clearTimeout(holdT); hideLoupe(); if (st.dragging) endDrag(); g = null; e.preventDefault();
    }, { passive: false });
    // a mouse / pen on the overlay (tablet with a pointer, desktop testing): pass straight through at the same point
    ov.addEventListener("mousedown", function (e) { if (e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents) return; var c = toC(toS(e.clientX, e.clientY)); fire("mousedown", c, e.button, e.buttons); });
    ov.addEventListener("mousemove", function (e) { if (e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents) return; var c = toC(toS(e.clientX, e.clientY)); fire("mousemove", c, e.button, e.buttons); });
    ov.addEventListener("mouseup", function (e) { if (e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents) return; var c = toC(toS(e.clientX, e.clientY)); fire("mouseup", c, e.button, e.buttons); });
    ov.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    ov.addEventListener("wheel", function (e) { e.preventDefault(); wheel(toC(toS(e.clientX, e.clientY)), e.deltaY, e.deltaX); }, { passive: false });

    // ---- keyboard: a hidden input; what is typed becomes key presses on the desktop
    var SENT = "\u200b\u200b";
    kb.value = SENT;
    var composing = false;
    kb.addEventListener("compositionstart", function () { composing = true; });
    kb.addEventListener("compositionend", function (e) { composing = false; if (e.data) typeText(e.data); kb.value = SENT; });
    kb.addEventListener("keydown", function (e) {
      if (composing) return;
      var special = { Enter: 1, Backspace: 1, Tab: 1, Escape: 1, ArrowLeft: 1, ArrowRight: 1, ArrowUp: 1, ArrowDown: 1 };
      if (special[e.key]) { e.preventDefault(); press(e.key); kb.value = SENT; }
    });
    kb.addEventListener("input", function (e) {
      if (composing) return;
      var v = kb.value;
      if (e.inputType === "deleteContentBackward" || v.length < SENT.length) press("Backspace");
      else { var t = v.replace(/\u200b/g, ""); if (t) typeText(t); }
      kb.value = SENT;
    });

    // ---- toolbar
    function btn(label, title, fn, keep) {
      var b = el(doc, "button"); b.type = "button"; b.textContent = label; b.title = title; b.setAttribute("aria-label", title);
      if (keep) b.classList.add("keep");
      b.addEventListener("touchstart", function (e) { e.stopPropagation(); }, { passive: true });
      b.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); fn(b); });
      bar.appendChild(b); return b;
    }
    function sep() { bar.appendChild(el(doc, "span", "sep")); }
    var modeBtn = btn("", "切换触控方式", function () { setMode(st.mode === "trackpad" ? "direct" : "trackpad"); }, true);
    btn("⌨", "键盘", function () {
      if (doc.activeElement === kb) { kb.blur(); return; }
      kb.value = SENT; kb.focus({ preventScroll: true });
    });
    sep();
    btn("Esc", "Esc", function () { press("Escape"); });
    btn("Tab", "Tab", function () { press("Tab"); });
    var ctrlBtn = btn("Ctrl", "Ctrl（按一下作用于下一个键）", function () { setCtrl(!st.ctrl); });
    btn("↵", "回车", function () { press("Enter"); });
    btn("⌫", "退格", function () { press("Backspace"); });
    btn("←", "左", function () { press("ArrowLeft"); });
    btn("↑", "上", function () { press("ArrowUp"); });
    btn("↓", "下", function () { press("ArrowDown"); });
    btn("→", "右", function () { press("ArrowRight"); });
    sep();
    var zoomBtn = btn("1:1", "缩放复位", function () { st.z = 1; st.px = 0; st.py = 0; paint(); }, true);
    var landBtn = btn("横屏", "横屏全屏", function () { setLandscape(!st.landscape); }, true);
    var foldBtn = btn("▾", "收起工具条", function () {   // folded: just the mode, 横屏 and this, so the desktop shows
      var min = !bar.classList.contains("min"); bar.classList.toggle("min", min);
      foldBtn.textContent = min ? "▸" : "▾"; foldBtn.title = min ? "展开工具条" : "收起工具条"; paint();
      try { localStorage.setItem("aiw_touch_bar", min ? "min" : ""); } catch (e) { /* private mode */ }
    }, true);
    if (localStorage.getItem("aiw_touch_bar") === "min") { bar.classList.add("min"); foldBtn.textContent = "▸"; }
    function setCtrl(on) { st.ctrl = on; ctrlBtn.classList.toggle("on", on); }
    function setMode(m) {
      st.mode = m; localStorage.setItem("aiw_touch_mode", m);
      modeBtn.textContent = m === "trackpad" ? "触控板" : "直点"; modeBtn.classList.add("on");
      if (st.dragging) endDrag();
      hideLoupe(); ensureCursor(); paint();
      say(m === "trackpad" ? "触控板：单指移动光标，轻点=左键，双指轻点=右键，长按拖动=选择，双指拖=滚动" : "直点：按住看放大镜对准，松手就点在那里；双指拖=滚动，捏合=缩放");
    }
    var noteT = 0;
    function say(t) { note.textContent = t; note.classList.add("on"); clearTimeout(noteT); noteT = setTimeout(function () { note.classList.remove("on"); }, 3200); }

    // ---- landscape
    function fsEl() { return doc.fullscreenElement || doc.webkitFullscreenElement; }
    function setLandscape(on) {
      st.landscape = on;
      landBtn.textContent = on ? "退出横屏" : "横屏"; landBtn.classList.toggle("on", on);
      if (opts.native) {   // iOS: the app rotates the screen and hides its own bar
        try { win.webkit.messageHandlers.aiwTakeover.postMessage({ op: on ? "landscape" : "portrait" }); } catch (e) { /* not in the app */ }
        if (opts.onLandscape) opts.onLandscape(on);
        relayout(); return;
      }
      if (on) {
        stage.classList.add("aiwt-full");
        var req = stage.requestFullscreen || stage.webkitRequestFullscreen;
        var p = null;
        try { p = req ? req.call(stage) : null; } catch (e) { p = null; }
        var lock = function () {
          var o = win.screen && win.screen.orientation;
          if (o && o.lock) return o.lock("landscape").then(function () { setRot(false); }, fallbackRot);
          fallbackRot(); return null;
        };
        if (p && p.then) p.then(lock, fallbackRot); else if (fsEl()) lock(); else fallbackRot();
      } else {
        wantRot = false;
        try { var o = win.screen && win.screen.orientation; if (o && o.unlock) o.unlock(); } catch (e) { /* ok */ }
        if (fsEl()) { (doc.exitFullscreen || doc.webkitExitFullscreen).call(doc); }
        stage.classList.remove("aiwt-full"); setRot(false);
      }
      if (opts.onLandscape) opts.onLandscape(on);
      relayout();
    }
    /** no orientation lock (iPhone Safari, a desktop browser): the rotated view. A fullscreen element cannot be rotated
     *  (the browser forces transform:none on it), so leave fullscreen first. */
    var wantRot = false;
    function fallbackRot() {
      wantRot = true;
      if (!fsEl()) { setRot(true); return; }
      var ex = doc.exitFullscreen || doc.webkitExitFullscreen, q = null;
      try { q = ex.call(doc); } catch (e) { q = null; }
      if (q && q.then) q.then(function () { setRot(true); }, function () { setRot(true); }); else setTimeout(function () { setRot(true); }, 120);
    }
    /** the CSS-rotated view, only while the phone is held upright (a phone already turned needs no rotation) */
    function setRot(on) {
      st.rot = !!on && win.innerHeight > win.innerWidth;
      stage.classList.toggle("aiwt-rot", st.rot);
      relayout();
    }
    function relayout() {
      setTimeout(function () { st.z = 1; st.px = 0; st.py = 0; ensureCursor(); paint(); }, 350);
    }
    function onFs() { if (!fsEl() && st.landscape && !opts.native && !st.rot && !wantRot) { st.landscape = false; stage.classList.remove("aiwt-full"); landBtn.textContent = "横屏"; landBtn.classList.remove("on"); if (opts.onLandscape) opts.onLandscape(false); relayout(); } }
    doc.addEventListener("fullscreenchange", onFs); doc.addEventListener("webkitfullscreenchange", onFs);
    function onResize() { if (st.landscape && !opts.native && stage.classList.contains("aiwt-full")) setRot(wantRot && win.innerHeight > win.innerWidth); else relayout(); }
    win.addEventListener("resize", onResize);

    setMode(st.mode);
    checkReach(); tidy();
    var poll = setInterval(function () { if (st.gone) return; checkReach(); tidy(); paint(); }, 1500);

    return {
      setMode: setMode, setLandscape: setLandscape,
      state: function () { var c = cur0(); return { mode: st.mode, z: st.z, px: st.px, py: st.py, cx: c.x, cy: c.y, u: st.u, v: st.v, pass: st.pass, landscape: st.landscape, rot: st.rot, last: st.last }; },
      /** for tests / the screenshots: map a stage point to the iframe client point */
      map: function (x, y) { return toC({ x: x, y: y }); },
      detach: function () {
        st.gone = true; clearInterval(poll); win.cancelAnimationFrame(raf); if (st.dragging) endDrag();
        if (st.landscape) setLandscape(false);
        win.removeEventListener("resize", onResize);
        doc.removeEventListener("fullscreenchange", onFs); doc.removeEventListener("webkitfullscreenchange", onFs);
        [ov, cur, dot, loupe, bar, note, kb].forEach(function (e) { e.remove(); });
        frame.style.transform = ""; frame.style.width = ""; frame.style.height = ""; stage.classList.remove("aiwt-stage", "aiwt-full", "aiwt-rot");
      }
    };
  }

  window.AiwTouch = { attach: attach };
})();
