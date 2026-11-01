/* Aiw touch takeover (3.8.4): precise control of the remote desktop from a phone, modelled on Microsoft's
 * "Windows App" (Remote Desktop) for iOS.
 *
 * Used by the web app (app.js: the takeover iframe on a touch screen), by a phone browser (/takeover.html?src=…&mid=…)
 * and by the iOS app (takeover.html in the takeover WKWebView, the Aiw server as its origin). The viewer (noVNC for
 * local computers, InstaBox's own page for 云电脑) only shows the desktop; this layer covers it and turns touches
 * into input that goes STRAIGHT TO THE VM: small ops over /ws/input/<machine> (POST /api/machines/<machine>/input when
 * a socket cannot be opened), run there with absolute xdotool (services/api/aiw_api/remote_input.py, vm-agent
 * computer.input). So it works the same in portrait and landscape and for a viewer on another origin, and nothing
 * inside the viewer ever takes focus away from the keyboard. Without a machine id (an old page) it falls back to
 * faking mouse / key events on noVNC's canvas, as 3.7 did.
 *
 *   鼠标指针 (mode "trackpad", the default, the precise one): a visible cursor on the desktop. One finger moves it
 *     relatively like a trackpad, with acceleration (slow = finer than the finger, fast = far); tap = left click AT THE
 *     CURSOR (not at the finger); double tap = double click; tap-and-hold (or tap, then touch again and drag) = drag
 *     (button down / move / up); two-finger tap: right click; two-finger drag = scroll wheel; pinch = zoom the local
 *     view (1x-5x), the cursor stays visible and the view pans by itself when the cursor nears an edge.
 *   直接触控 (mode "direct"): tap = left click exactly where the finger is; long press = right click there;
 *     one-finger drag = mouse drag, with a loupe (magnifier + crosshair) above the finger; two-finger drag = scroll;
 *     pinch = zoom + pan.
 *   Toolbar: mode switch (remembered), keyboard, Esc, Tab, Ctrl / Alt / Super (sticky: they apply to the next key),
 *   ↵ ⌫ arrows, ^C ^V ^A, zoom reset, 横屏 and its exit. In the iOS app the keyboard is native (a hidden text field
 *   with its own key bar, DesktopView.swift KeyBridge) and reaches this page through `input(op)`.
 *
 * Coordinates: S = point in the stage's padding box (un-rotated when the stage is CSS-rotated for landscape),
 * z / (px, py) = zoom and pan. The iframe sits at the stage origin, z times the stage size, moved by translate(px, py),
 * so the iframe-internal client point is c = S - pan. The desktop picture inside it is `view()`: noVNC's canvas rect
 * when the viewer is same-origin (measured, exact), else the remote screen (its size comes in the socket's hello)
 * aspect-fit and centred in the iframe, as both viewers draw it (letterboxing). A point maps to the remote pixel
 * floor((c - view.origin) / view.size * remote size): zoom, pan, letterboxing, rotation and the device scale all
 * compose, and the cursor is kept as a fraction (u, v) of the desktop, so its precision is the remote pixel, not the
 * phone's CSS pixel.
 *
 * Mouse moves are coalesced to ~60 a second (MOVE_MS) and the last position is always sent before a click, a button
 * change or a scroll.
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
    ".aiwt-kb{position:absolute;left:0;bottom:0;width:1px;height:1px;opacity:0;border:0;padding:0;font-size:16px;z-index:0;caret-color:transparent;color:transparent;background:transparent}" +
    /* landscape: real fullscreen (+ orientation lock) where the browser has it, else this rotated full-screen view */
    ".aiwt-full{position:fixed!important;left:0;top:0;width:100vw!important;height:100vh!important;z-index:2147483000;border-radius:0!important;margin:0!important;aspect-ratio:auto!important}" +
    ".aiwt-rot{position:fixed!important;left:100vw;top:0;width:100vh!important;height:100vw!important;transform:rotate(90deg);transform-origin:0 0;" +
    "z-index:2147483000;border-radius:0!important;margin:0!important;aspect-ratio:auto!important}";

  var CURSOR = '<svg viewBox="0 0 22 22" width="22" height="22"><path d="M2 1.5 L2 17.5 L6.3 13.6 L9.2 20.2 L12 19 L9.2 12.6 L15 12.6 Z" ' +
    'fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg>';
  /* xdotool key names -> DOM keys, for the old in-viewer fallback only */
  var DOMK = {
    Escape: { key: "Escape", code: "Escape", keyCode: 27 }, Tab: { key: "Tab", code: "Tab", keyCode: 9 },
    Return: { key: "Enter", code: "Enter", keyCode: 13 }, BackSpace: { key: "Backspace", code: "Backspace", keyCode: 8 },
    Left: { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 }, Up: { key: "ArrowUp", code: "ArrowUp", keyCode: 38 },
    Right: { key: "ArrowRight", code: "ArrowRight", keyCode: 39 }, Down: { key: "ArrowDown", code: "ArrowDown", keyCode: 40 },
    Delete: { key: "Delete", code: "Delete", keyCode: 46 },
    ctrl: { key: "Control", code: "ControlLeft", keyCode: 17 }, alt: { key: "Alt", code: "AltLeft", keyCode: 18 },
    super: { key: "Meta", code: "MetaLeft", keyCode: 91 }, shift: { key: "Shift", code: "ShiftLeft", keyCode: 16 }
  };
  /* browser key names (a hardware keyboard on the hidden input) -> xdotool */
  var XK = { Enter: "Return", Backspace: "BackSpace", Tab: "Tab", Escape: "Escape", ArrowLeft: "Left", ArrowRight: "Right",
    ArrowUp: "Up", ArrowDown: "Down", Delete: "Delete", Home: "Home", End: "End", PageUp: "Prior", PageDown: "Next" };
  var TAP_MS = 260, TAP_MOVE = 9, HOLD_MS = 450, DTAP_MS = 320, ZMAX = 5, MOVE_MS = 16, SCROLL_PX = 16;

  function css(doc) {
    if (doc.getElementById("aiwt-css")) return;
    var s = doc.createElement("style"); s.id = "aiwt-css"; s.textContent = CSS; doc.head.appendChild(s);
  }
  function el(doc, tag, cls, html) { var e = doc.createElement(tag); if (cls) e.className = cls; if (html) e.innerHTML = html; return e; }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  /** attach(stage, frame, opts) -> controller. stage: the element around the iframe (made position:relative).
   *  opts: { mode: "trackpad"|"direct", native: bool (iOS: keyboard / landscape go to the app), mid: machine id,
   *          token: API token, base: server URL (default: the page's), onLandscape(on) } */
  function attach(stage, frame, opts) {
    opts = opts || {};
    var doc = stage.ownerDocument, win = doc.defaultView;
    css(doc);
    stage.classList.add("aiwt-stage");
    var ov = el(doc, "div", "aiwt-ov"), cur = el(doc, "div", "aiwt-cur", CURSOR), dot = el(doc, "div", "aiwt-dot");
    var loupe = el(doc, "div", "aiwt-loupe"), lc = el(doc, "canvas"), bar = el(doc, "div", "aiwt-bar"), note = el(doc, "div", "aiwt-note");
    var kb = el(doc, "input", "aiwt-kb");
    kb.setAttribute("autocapitalize", "off"); kb.setAttribute("autocomplete", "off"); kb.setAttribute("autocorrect", "off");
    kb.setAttribute("spellcheck", "false"); kb.setAttribute("aria-label", "键盘输入"); kb.setAttribute("enterkeyhint", "enter");
    loupe.appendChild(lc);
    [ov, cur, dot, loupe, bar, note, kb].forEach(function (e) { stage.appendChild(e); });

    var saved = "trackpad";
    try { saved = localStorage.getItem("aiw_touch_mode") || "trackpad"; } catch (e) { /* private mode */ }
    var st = {
      mode: opts.mode || saved,
      z: 1, px: 0, py: 0, u: -1, v: -1, mods: {}, dragging: false, landscape: false, rot: false, pass: false, gone: false,
      kbOn: false
    };
    var touches = {}, g = null, lastTap = 0, lastTapAt = { x: 0, y: 0 }, holdT = 0, raf = 0, pendMove = false;

    // ---- the input channel to the VM (3.8.4)
    var rx = { ws: null, open: false, w: 0, h: 0, tries: 0, pend: [], postT: 0, moveAt: 0, mv: null, mvT: 0, pingT: 0, errAt: 0 };
    var useRemote = !!opts.mid;
    function base() {
      var b = opts.base || doc.baseURI || win.location.href;
      try { return new URL(".", new URL(b, win.location.href)).href.replace(/[^/]*$/, ""); } catch (e) { return "/"; }
    }
    function root() {   // the server root ("https://host/"), whatever page this is
      try { var u = new URL(base()); return u.origin + "/"; } catch (e) { return "/"; }
    }
    function connect() {
      if (!useRemote || st.gone || typeof win.WebSocket !== "function") return;
      var u = root().replace(/^http/, "ws") + "ws/input/" + encodeURIComponent(opts.mid) + (opts.token ? "?token=" + encodeURIComponent(opts.token) : "");
      var ws;
      try { ws = new win.WebSocket(u); } catch (e) { retry(); return; }
      rx.ws = ws;
      ws.onopen = function () { rx.open = true; rx.tries = 0; flushPost(); };
      ws.onmessage = function (m) {
        var d; try { d = JSON.parse(m.data); } catch (e) { return; }
        if (d.op === "hello") { if (d.width) { rx.w = d.width; rx.h = d.height; } paint(); }
        else if (d.op === "error") oops("电脑没收到操作：" + String(d.error || "").slice(0, 60));
      };
      ws.onclose = function () { if (rx.ws === ws) { rx.open = false; rx.ws = null; retry(); } };
      ws.onerror = function () { /* onclose follows */ };
    }
    /** 3.8.7: one note per problem, not a toast for every failed batch (the same text stays quiet for 20 s) */
    function oops(text) {
      var now = Date.now();
      if (now - rx.errAt < 6000 || (text === rx.errText && now - rx.errAt < 20000)) return;
      rx.errAt = now; rx.errText = text; say(text);
    }
    function retry() {
      if (st.gone) return;
      rx.tries++;
      setTimeout(connect, Math.min(8000, 400 * Math.pow(2, Math.min(5, rx.tries))));
    }
    /** one op to the VM: the socket, or a POST batch while it is down */
    function send(op) {
      if (rx.open && rx.ws && rx.ws.readyState === 1) { try { rx.ws.send(JSON.stringify(op)); return; } catch (e) { /* below */ } }
      if (op.t === "move" && rx.pend.length && rx.pend[rx.pend.length - 1].t === "move") rx.pend[rx.pend.length - 1] = op;
      else rx.pend.push(op);
      if (!rx.postT) rx.postT = setTimeout(flushPost, 60);
    }
    function flushPost() {
      clearTimeout(rx.postT); rx.postT = 0;
      if (!rx.pend.length) return;
      var ops = rx.pend.splice(0, 200);
      if (rx.open && rx.ws && rx.ws.readyState === 1) { try { rx.ws.send(JSON.stringify({ ops: ops })); return; } catch (e) { /* POST */ } }
      var h = { "Content-Type": "application/json" };
      if (opts.token) h.Authorization = "Bearer " + opts.token;
      win.fetch(root() + "api/machines/" + encodeURIComponent(opts.mid) + "/input", { method: "POST", headers: h, body: JSON.stringify({ ops: ops }) })
        .then(function (r) { if (r.ok) return r.json(); throw new Error("HTTP " + r.status); })
        .then(function (j) { if (j && j.width && !rx.w) { rx.w = j.width; rx.h = j.height; paint(); } },
              function (e) { oops("连不上电脑：" + e.message); });
    }
    /** mouse moves: at most one per MOVE_MS (~60 a second); the latest position always wins */
    function moveTo(p) {
      if (!rx.mv && rx.lastMv && rx.lastMv.x === p.x && rx.lastMv.y === p.y) return;   // same pixel: nothing to send
      rx.mv = p;
      if (rx.mvT) return;
      var wait = Math.max(0, MOVE_MS - (Date.now() - rx.moveAt));
      rx.mvT = setTimeout(flushMove, wait);
    }
    function flushMove() {
      clearTimeout(rx.mvT); rx.mvT = 0;
      if (!rx.mv) return;
      send({ t: "move", x: rx.mv.x, y: rx.mv.y }); rx.lastMv = rx.mv; rx.mv = null; rx.moveAt = Date.now();
    }

    // ---- the viewer inside the iframe (only reachable when same-origin)
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
    /** the desktop picture in iframe client coordinates (c space) */
    function view() {
      var c = canvas();
      if (c) { var r = c.getBoundingClientRect(); if (r.width > 4) return { l: r.left, t: r.top, w: r.width, h: r.height }; }
      var fw = frame.clientWidth || stage.clientWidth, fh = frame.clientHeight || stage.clientHeight;
      if (rx.w && rx.h) {   // aspect-fit, centred: the letterbox bars are not desktop
        var k = Math.min(fw / rx.w, fh / rx.h), w = rx.w * k, h = rx.h * k;
        return { l: (fw - w) / 2, t: (fh - h) / 2, w: w, h: h };
      }
      return { l: 0, t: 0, w: fw, h: fh };
    }
    function rsize() {
      if (rx.w) return { w: rx.w, h: rx.h };
      var c = canvas(); if (c && c.width) return { w: c.width, h: c.height };
      var r = view(); return { w: Math.round(r.w), h: Math.round(r.h) };
    }
    /** c-space point -> remote pixel */
    function toR(c) {
      var r = view(), s = rsize();
      return { x: clamp(Math.floor((c.x - r.l) / Math.max(1, r.w) * s.w), 0, s.w - 1),
               y: clamp(Math.floor((c.y - r.t) / Math.max(1, r.h) * s.h), 0, s.h - 1) };
    }
    function inside(c) { var r = view(); return c.x >= r.l && c.y >= r.t && c.x < r.l + r.w && c.y < r.t + r.h; }
    function checkReach() {
      var d = idoc(), was = st.pass;
      st.pass = !d && !useRemote;   // another origin and no channel to the VM: step aside
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
    /** the cursor in c space (from its place on the desktop), centred on its remote pixel */
    function cur0() {
      var r = view(), s = rsize();
      var px = Math.min(s.w - 1, Math.floor(st.u * s.w)) + 0.5, py = Math.min(s.h - 1, Math.floor(st.v * s.h)) + 0.5;
      return { x: r.l + px / s.w * r.w, y: r.t + py / s.h * r.h };
    }
    function setCur(c) { var r = view(); st.u = clamp((c.x - r.l) / Math.max(1, r.w), 0, 0.99999); st.v = clamp((c.y - r.t) / Math.max(1, r.h), 0, 0.99999); }
    function clampPan() {
      var w = W(), h = H();
      st.px = Math.min(0, Math.max(w - st.z * w, st.px)); st.py = Math.min(0, Math.max(h - st.z * h, st.py));
    }
    function ensureCursor() { if (st.u < 0) { st.u = 0.5; st.v = 0.5; } }
    /** zoomed in: keep the cursor on screen by panning (鼠标指针) */
    function follow() {
      if (st.z <= 1.001) return;
      var s = fromC(cur0()), m = 40, w = W(), h = H();
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

    // ---- output: to the VM (remote) or, without a machine id, faked events on noVNC's canvas (old pages)
    function fire(type, c, button, buttons) {
      var cv = canvas(), d = idoc(); if (!cv || !d) return false;
      c = snap(c); st.last = { type: type, x: c.x, y: c.y };
      var v = d.defaultView, E = v.MouseEvent;
      cv.dispatchEvent(new E(type, { bubbles: true, cancelable: true, view: v, clientX: c.x, clientY: c.y,
        screenX: c.x, screenY: c.y, button: button || 0, buttons: buttons == null ? 0 : buttons }));
      return true;
    }
    var BTN = { left: 0, middle: 1, right: 2 };
    function outMove(c) {
      if (useRemote) { moveTo(toR(c)); return; }
      if (pendMove) return; pendMove = true;
      raf = win.requestAnimationFrame(function () { pendMove = false; fire("mousemove", c, 0, st.dragging ? 1 : 0); });
    }
    /** a click: the final position goes first, then the click at that same pixel */
    function outClick(c, button, count) {
      button = button || "left";
      if (useRemote) { var p = toR(c); rx.mv = p; flushMove(); send({ t: "click", x: p.x, y: p.y, button: button, count: count === 2 ? 2 : 1 }); st.last = { type: "click", x: p.x, y: p.y, button: button }; return; }
      var b = BTN[button], mask = b === 2 ? 2 : 1;
      fire("mousemove", c, 0, 0); fire("mousedown", c, b, mask); fire("mouseup", c, b, 0);
      if (b === 2) fire("contextmenu", c, 2, 0);
    }
    function outButton(kind, c) {   // "down" / "up", left button (drag)
      if (useRemote) { var p = toR(c); rx.mv = p; flushMove(); send({ t: kind, x: p.x, y: p.y, button: "left" }); st.last = { type: kind, x: p.x, y: p.y }; return; }
      fire("mousemove", c, 0, kind === "up" ? 1 : 0); fire(kind === "down" ? "mousedown" : "mouseup", c, 0, kind === "down" ? 1 : 0);
    }
    function outScroll(c, ny, nx) {   // wheel notches, ny > 0 = down
      if (useRemote) { var p = toR(c); flushMove(); send({ t: "scroll", x: p.x, y: p.y, dx: nx || 0, dy: ny || 0 }); return; }
      var cv = canvas(), d = idoc(); if (!cv || !d) return;
      var v = d.defaultView;
      cv.dispatchEvent(new v.WheelEvent("wheel", { bubbles: true, cancelable: true, view: v, clientX: c.x, clientY: c.y,
        deltaX: (nx || 0) * 50, deltaY: (ny || 0) * 50, deltaMode: 0 }));
    }
    function outText(t) {
      if (!t) return;
      if (useRemote) { flushMove(); send({ t: "text", text: t }); return; }
      for (var i = 0; i < t.length; i++) domKey(t[i] === "\n" ? "Return" : t[i]);
    }
    /** a key or combo in xdotool names ("Return", "ctrl+c"); the sticky Ctrl / Alt / Super join it and turn off */
    function outKey(k) {
      var mods = Object.keys(st.mods).filter(function (m) { return st.mods[m]; });
      var combo = mods.concat([k]).join("+");
      clearMods();
      if (useRemote) { flushMove(); send({ t: "key", key: combo }); return; }
      domKey(combo);
    }
    function domKey(combo) {
      var cv = canvas(), d = idoc(); if (!cv || !d) return;
      var parts = combo.split("+"), main = parts.pop(), v = d.defaultView;
      var k = DOMK[main] || { key: main, code: /^[a-z]$/i.test(main) ? "Key" + main.toUpperCase() : /^[0-9]$/.test(main) ? "Digit" + main : main === " " ? "Space" : "",
                              keyCode: main.length === 1 ? main.toUpperCase().charCodeAt(0) : 0 };
      function ev(t, kk) {
        var e = new v.KeyboardEvent(t, { bubbles: true, cancelable: true, key: kk.key, code: kk.code, ctrlKey: parts.indexOf("ctrl") >= 0 });
        try { Object.defineProperty(e, "keyCode", { get: function () { return kk.keyCode; } });
              Object.defineProperty(e, "which", { get: function () { return kk.keyCode; } }); } catch (x) { /* read-only */ }
        cv.dispatchEvent(e);
      }
      parts.forEach(function (m) { if (DOMK[m]) ev("keydown", DOMK[m]); });
      ev("keydown", k); ev("keyup", k);
      parts.slice().reverse().forEach(function (m) { if (DOMK[m]) ev("keyup", DOMK[m]); });
    }

    // ---- loupe (direct mode, while dragging)
    function showLoupe(s) {
      var cv = canvas();
      var w = W(), h = H(), dpr = win.devicePixelRatio || 1;
      var size = Math.round(Math.max(72, Math.min(124, h * 0.42, w * 0.42))), gap = 34;
      dot.style.display = "block"; dot.style.left = s.x + "px"; dot.style.top = s.y + "px";
      if (!cv) { loupe.style.display = "none"; return; }   // another origin: its picture cannot be read, the dot shows the spot
      // above the finger, else below, else beside it (left / right), else wherever it fits best in the stage
      var cand = [[s.x - size / 2, s.y - size - gap], [s.x - size / 2, s.y + gap], [s.x - size - gap, s.y - size / 2], [s.x + gap, s.y - size / 2]];
      var fit = cand.filter(function (q) { return q[0] >= 4 && q[1] >= 4 && q[0] + size <= w - 4 && q[1] + size <= h - 4; })[0] || cand[0];
      var lx = clamp(fit[0], 4, w - size - 4), ly = clamp(fit[1], 4, h - size - 4);
      loupe.style.width = loupe.style.height = size + "px";
      loupe.style.display = "block"; loupe.style.left = lx + "px"; loupe.style.top = ly + "px";
      if (lc.width !== Math.round(size * dpr)) { lc.width = lc.height = Math.round(size * dpr); }
      var c = toC(s), r = view(), k = cv.width / Math.max(1, r.w);
      var mag = 2.6, areaC = size / mag;                      // the desktop area the loupe shows, in c units
      var sw = areaC * k, sx = (c.x - r.l) * k - sw / 2, sy = (c.y - r.t) * k - sw / 2;
      var x2 = lc.getContext("2d");
      x2.imageSmoothingEnabled = false;
      x2.fillStyle = "#111"; x2.fillRect(0, 0, lc.width, lc.height);
      try { x2.drawImage(cv, sx, sy, sw, sw, 0, 0, lc.width, lc.height); } catch (e) { /* not drawable yet */ }
    }
    function hideLoupe() { loupe.style.display = "none"; dot.style.display = "none"; }
    function blip(s) {   // where a direct click / right click landed
      dot.style.display = "block"; dot.style.left = s.x + "px"; dot.style.top = s.y + "px";
      setTimeout(function () { if (!st.dragging) dot.style.display = "none"; }, 260);
    }

    // ---- gestures
    function pts() { return Object.keys(touches).map(function (k) { return touches[k]; }); }
    function mid(a, b) { return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }; }
    function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
    function onStart(e) {
      if (st.pass) return;
      e.preventDefault();   // no click / focus change: the keyboard stays up while you tap the desktop
      var now = Date.now();
      Array.prototype.forEach.call(e.changedTouches, function (t) { var s = toS(t.clientX, t.clientY); touches[t.identifier] = { x: s.x, y: s.y, x0: s.x, y0: s.y, t: now }; });
      var p = pts();
      ensureCursor();
      if (p.length === 1) {
        var a = p[0];
        g = { kind: "one", t0: now, x0: a.x, y0: a.y, lx: a.x, ly: a.y, lt: now, moved: 0,
              second: now - lastTap < DTAP_MS && Math.hypot(a.x - lastTapAt.x, a.y - lastTapAt.y) < 60 };
        clearTimeout(holdT);
        if (st.mode === "trackpad") {
          holdT = setTimeout(function () {   // tap-and-hold: hold the left button (drag / select) at the cursor
            if (g && g.kind === "one" && g.moved < TAP_MOVE && !st.dragging) { startDrag(cur0()); g.held = true; }
          }, HOLD_MS);
        } else {
          holdT = setTimeout(function () {   // direct: long press = right click where the finger is
            if (g && g.kind === "one" && g.moved < TAP_MOVE && !st.dragging) {
              var c = toC({ x: g.x0, y: g.y0 });
              if (inside(c)) { setCur(c); outClick(c, "right"); blip({ x: g.x0, y: g.y0 }); }
              g.done = true;
            }
          }, HOLD_MS + 50);
        }
      } else if (p.length === 2) {
        clearTimeout(holdT);
        if (st.dragging && g && g.kind === "one") endDrag(st.mode === "trackpad" ? cur0() : toC({ x: g.lx, y: g.ly }));
        hideLoupe();
        var m = mid(p[0], p[1]);
        g = { kind: "two", t0: now, d0: dist(p[0], p[1]), m0: m, lm: m, z0: st.z, px0: st.px, py0: st.py, pinch: false, moved: 0, sy: 0, sx: 0 };
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
        var a = p[0], dt = Math.max(1, now - g.lt);
        var v = Math.hypot(a.x - g.lx, a.y - g.ly) / dt;                    // finger speed, px / ms
        g.moved = Math.max(g.moved, Math.hypot(a.x - g.x0, a.y - g.y0));
        g.lx = a.x; g.ly = a.y; g.lt = now;
        if (st.mode === "trackpad") {
          if (g.moved < TAP_MOVE && !st.dragging) return;   // still a tap / hold candidate
          clearTimeout(holdT);
          if (g.second && !st.dragging && !g.held) startDrag(cur0());   // tap, then touch again and drag = drag
          // the movement since the last applied point (the first time: since the finger went down, so the tap
          // threshold is not lost)
          var ox = g.ox == null ? g.x0 : g.ox, oy = g.oy == null ? g.y0 : g.oy, dx = a.x - ox, dy = a.y - oy;
          g.ox = a.x; g.oy = a.y;
          // trackpad acceleration: slow = finer than the finger (0.55x), fast = far (up to ~3.4x)
          var gain = 0.55 + Math.min(2.85, Math.max(0, v - 0.12) * 2.4);
          var r = view();                                                   // zoomed in, the desktop is bigger: finer
          st.u = clamp(st.u + dx * gain / Math.max(1, r.w), 0, 0.99999);
          st.v = clamp(st.v + dy * gain / Math.max(1, r.h), 0, 0.99999);
          follow(); paint(); outMove(cur0());
        } else {
          if (g.done) return;
          if (!st.dragging) {
            if (g.moved < TAP_MOVE) return;
            clearTimeout(holdT);
            var c0 = toC({ x: g.x0, y: g.y0 });
            if (!inside(c0)) { g.done = true; return; }
            setCur(c0); startDrag(c0);   // one-finger drag in direct mode = mouse drag from where it began
          }
          var c = toC(a); setCur(c); outMove(c); showLoupe(a);
        }
      } else if (g.kind === "two" && p.length === 2) {
        var m = mid(p[0], p[1]), d = dist(p[0], p[1]);
        g.moved = Math.max(g.moved, Math.hypot(m.x - g.m0.x, m.y - g.m0.y), Math.abs(d - g.d0));
        if (!g.pinch && Math.abs(d / g.d0 - 1) > 0.1) {   // fingers apart / together: zoom (and pan) from here on
          g.pinch = true; g.d0 = d; g.m0 = m; g.z0 = st.z; g.px0 = st.px; g.py0 = st.py;
        }
        if (g.pinch) {
          var z = clamp(g.z0 * d / g.d0, 1, ZMAX);
          var fx = (g.m0.x - g.px0) / (W() * g.z0), fy = (g.m0.y - g.py0) / (H() * g.z0);   // the spot under the fingers
          st.z = z; st.px = m.x - fx * W() * z; st.py = m.y - fy * H() * z; clampPan(); paint();
        } else if (g.moved > TAP_MOVE) {   // two-finger drag: scroll wheel (natural direction) at the cursor / fingers
          g.sy += -(m.y - g.lm.y) / SCROLL_PX; g.sx += -(m.x - g.lm.x) / SCROLL_PX;
          var ny = g.sy > 0 ? Math.floor(g.sy) : Math.ceil(g.sy), nx = g.sx > 0 ? Math.floor(g.sx) : Math.ceil(g.sx);
          if (ny || nx) {
            outScroll(st.mode === "trackpad" ? cur0() : toC(m), ny, nx);
            g.sy -= ny; g.sx -= nx;
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
          if (st.dragging) endDrag(cur0());
          else if (g.moved < TAP_MOVE && quick) {   // tap = left click at the cursor; a second tap soon after = double click
            outClick(cur0(), "left", 1); lastTap = now; lastTapAt = { x: g.x0, y: g.y0 };
          }
        } else {
          hideLoupe();
          if (st.dragging) endDrag(toC({ x: g.lx, y: g.ly }));
          else if (!g.done && g.moved < TAP_MOVE) {   // tap = left click exactly under the finger (where it went down)
            var c = toC({ x: g.x0, y: g.y0 });
            if (inside(c)) { setCur(c); outClick(c, "left", 1); blip({ x: g.x0, y: g.y0 }); }
          }
        }
        g = null;
      } else if (g.kind === "two") {
        // fingers rarely lift together: the gesture is judged when the last one lifts; the one left down after a
        // pinch / scroll does nothing
        if (left === 0) {
          if (!g.pinch && g.moved < TAP_MOVE * 1.5 && now - g.t0 < TAP_MS + 120) {   // two-finger tap: right click
            outClick(st.mode === "trackpad" ? cur0() : toC(g.m0), "right", 1);
          }
          if (g.pinch && st.mode === "trackpad") { follow(); paint(); }   // the cursor stays in view after a zoom
          g = null;
        }
      } else if (left === 0) g = null;
    }
    function startDrag(c) { st.dragging = true; outButton("down", c); paint(); }
    function endDrag(c) { st.dragging = false; outButton("up", c); paint(); }
    ov.addEventListener("touchstart", onStart, { passive: false });
    ov.addEventListener("touchmove", onMove, { passive: false });
    ov.addEventListener("touchend", onEnd, { passive: false });
    ov.addEventListener("touchcancel", function (e) {
      touches = {}; clearTimeout(holdT); hideLoupe();
      if (st.dragging) endDrag(st.mode === "trackpad" ? cur0() : toC({ x: g ? g.lx : 0, y: g ? g.ly : 0 }));
      g = null; e.preventDefault();
    }, { passive: false });
    // a mouse / pen on the overlay (tablet with a pointer, desktop testing): straight through at the same point
    function mouseOk(e) { return !(e.sourceCapabilities && e.sourceCapabilities.firesTouchEvents); }
    ov.addEventListener("mousedown", function (e) { if (!mouseOk(e)) return; e.preventDefault(); var c = toC(toS(e.clientX, e.clientY)); setCur(c);
      if (e.button === 2) outClick(c, "right"); else startDrag(c); refocus(); });
    ov.addEventListener("mousemove", function (e) { if (!mouseOk(e)) return; var c = toC(toS(e.clientX, e.clientY)); setCur(c); outMove(c); paint(); });
    ov.addEventListener("mouseup", function (e) { if (!mouseOk(e) || e.button === 2) return; if (st.dragging) endDrag(toC(toS(e.clientX, e.clientY))); });
    ov.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    ov.addEventListener("wheel", function (e) { e.preventDefault(); var n = e.deltaY > 0 ? 1 : e.deltaY < 0 ? -1 : 0, nx = e.deltaX > 0 ? 1 : e.deltaX < 0 ? -1 : 0;
      if (n || nx) outScroll(toC(toS(e.clientX, e.clientY)), n, nx); }, { passive: false });

    // ---- keyboard (web): a hidden input that keeps the focus; committed text goes to the VM as unicode text
    // (Chinese IME: only the compositionend string, never the pinyin), special keys as xdotool keys.
    var SENT = "\u200b\u200b";
    kb.value = SENT;
    var composing = false;
    kb.addEventListener("compositionstart", function () { composing = true; });
    kb.addEventListener("compositionend", function (e) { composing = false; if (e.data) typed(e.data); kb.value = SENT; });
    kb.addEventListener("keydown", function (e) {
      if (composing || e.isComposing || e.keyCode === 229) return;
      var x = XK[e.key];
      if (x) { e.preventDefault(); outKey(x); kb.value = SENT; return; }
      if ((e.ctrlKey || e.metaKey || e.altKey) && e.key && e.key.length === 1) {   // hardware keyboard shortcut
        e.preventDefault();
        if (e.ctrlKey) st.mods.ctrl = true; if (e.altKey) st.mods.alt = true; if (e.metaKey) st.mods.super = true;
        outKey(e.key.toLowerCase()); kb.value = SENT;
      }
    });
    kb.addEventListener("input", function (e) {
      if (composing || e.isComposing) return;
      var v = kb.value;
      if (e.inputType === "deleteContentBackward" || v.length < SENT.length) outKey("BackSpace");
      else if (e.inputType === "insertLineBreak") outKey("Return");
      else { var t = v.replace(/\u200b/g, ""); if (t) typed(t); }
      kb.value = SENT;
    });
    kb.addEventListener("blur", function () {   // something took the focus while the keyboard is wanted: take it back
      if (st.kbOn && !st.gone) setTimeout(function () { if (st.kbOn && doc.activeElement !== kb) kb.focus({ preventScroll: true }); }, 0);
    });
    /** typed text; with Ctrl / Alt / Super on, one character becomes that shortcut */
    function typed(t) {
      var on = Object.keys(st.mods).some(function (m) { return st.mods[m]; });
      if (on && t.length === 1) { outKey(t === " " ? "space" : t.toLowerCase()); return; }
      clearMods(); outText(t);
    }
    function refocus() { if (st.kbOn && !opts.native && doc.activeElement !== kb) kb.focus({ preventScroll: true }); }
    function setKb(on) {
      st.kbOn = on; kbBtn.classList.toggle("on", on);
      if (opts.native) {   // iOS: the app's own keyboard (KeyBridge) and key bar
        try { win.webkit.messageHandlers.aiwTakeover.postMessage({ op: on ? "keyboard" : "keyboard-off" }); } catch (e) { /* not in the app */ }
        return;
      }
      if (on) { kb.value = SENT; kb.focus({ preventScroll: true }); } else kb.blur();
    }

    // ---- toolbar
    function btn(label, title, fn, keep) {
      var b = el(doc, "button"); b.type = "button"; b.textContent = label; b.title = title; b.setAttribute("aria-label", title);
      b.tabIndex = -1;
      if (keep) b.classList.add("keep");
      b.addEventListener("touchstart", function (e) { e.stopPropagation(); }, { passive: true });
      b.addEventListener("mousedown", function (e) { e.preventDefault(); });   // a button never takes the focus from the keyboard
      b.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); fn(b); refocus(); });
      bar.appendChild(b); return b;
    }
    function sep() { bar.appendChild(el(doc, "span", "sep")); }
    var modeBtn = btn("", "切换触控方式", function () { setMode(st.mode === "trackpad" ? "direct" : "trackpad"); }, true);
    var kbBtn = btn("⌨", "键盘", function () { setKb(!st.kbOn); }, true);
    sep();
    btn("Esc", "Esc", function () { outKey("Escape"); });
    btn("Tab", "Tab", function () { outKey("Tab"); });
    var modBtn = {};
    [["ctrl", "Ctrl"], ["alt", "Alt"], ["super", "Super"]].forEach(function (m) {
      modBtn[m[0]] = btn(m[0] === "super" ? "⌘" : m[1], m[1] + "（按一下作用于下一个键）", function () { setMod(m[0], !st.mods[m[0]]); });
    });
    btn("↵", "回车", function () { outKey("Return"); });
    btn("⌫", "退格", function () { outKey("BackSpace"); });
    btn("←", "左", function () { outKey("Left"); });
    btn("↑", "上", function () { outKey("Up"); });
    btn("↓", "下", function () { outKey("Down"); });
    btn("→", "右", function () { outKey("Right"); });
    btn("^C", "复制 Ctrl+C", function () { clearMods(); outKey("ctrl+c"); });
    btn("^V", "粘贴 Ctrl+V", function () { clearMods(); outKey("ctrl+v"); });
    btn("^A", "全选 Ctrl+A", function () { clearMods(); outKey("ctrl+a"); });
    if (win.aiwPasteToVM || (win.parent && win.parent.aiwPasteToVM))   // 3.9.10: text from this phone into the VM
      btn("贴", "把手机剪贴板的文字粘贴到电脑", function () { clearMods(); (win.aiwPasteToVM || win.parent.aiwPasteToVM)(); });
    sep();
    var zoomBtn = btn("1:1", "缩放复位", function () { st.z = 1; st.px = 0; st.py = 0; paint(); }, true);
    var landBtn = btn("横屏", "横屏全屏", function () { setLandscape(!st.landscape); }, true);
    var foldBtn = btn("▾", "收起工具条", function () {   // folded: just the mode, keyboard, 横屏 and this, so the desktop shows
      var min = !bar.classList.contains("min"); bar.classList.toggle("min", min);
      foldBtn.textContent = min ? "▸" : "▾"; foldBtn.title = min ? "展开工具条" : "收起工具条"; paint();
      try { localStorage.setItem("aiw_touch_bar", min ? "min" : ""); } catch (e) { /* private mode */ }
    }, true);
    try { if (localStorage.getItem("aiw_touch_bar") === "min") { bar.classList.add("min"); foldBtn.textContent = "▸"; } } catch (e) { /* ok */ }
    function setMod(m, on) { st.mods[m] = on; modBtn[m].classList.toggle("on", on); }
    function clearMods() { Object.keys(modBtn).forEach(function (m) { setMod(m, false); }); }
    function setMode(m) {
      if (m !== "direct") m = "trackpad";
      st.mode = m; try { localStorage.setItem("aiw_touch_mode", m); } catch (e) { /* private mode */ }
      modeBtn.textContent = m === "trackpad" ? "鼠标指针" : "直接触控"; modeBtn.classList.add("on");
      if (st.dragging) endDrag(cur0());
      hideLoupe(); ensureCursor(); paint();
      say(m === "trackpad" ? "鼠标指针：单指移动光标，轻点=左键，双击=双击，按住再拖=拖动，双指轻点=右键，双指拖=滚动，捏合=缩放"
                           : "直接触控：轻点=点在手指处，长按=右键，单指拖=拖动（放大镜），双指拖=滚动，捏合=缩放");
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
    function relayout() {   // orientation changed: back to the whole desktop, cursor where it was on it
      setTimeout(function () { st.z = 1; st.px = 0; st.py = 0; ensureCursor(); paint(); }, 350);
    }
    function onFs() { if (!fsEl() && st.landscape && !opts.native && !st.rot && !wantRot) { st.landscape = false; stage.classList.remove("aiwt-full"); landBtn.textContent = "横屏"; landBtn.classList.remove("on"); if (opts.onLandscape) opts.onLandscape(false); relayout(); } }
    doc.addEventListener("fullscreenchange", onFs); doc.addEventListener("webkitfullscreenchange", onFs);
    var lastWide = win.innerWidth > win.innerHeight;
    function onResize() {
      if (st.landscape && !opts.native && stage.classList.contains("aiwt-full")) { setRot(wantRot && win.innerHeight > win.innerWidth); return; }
      var wide = win.innerWidth > win.innerHeight;
      if (wide !== lastWide) { lastWide = wide; relayout(); return; }
      clampPan(); paint();   // the keyboard came up / went away: keep the zoom
    }
    win.addEventListener("resize", onResize);

    setMode(st.mode);
    checkReach(); tidy(); connect();
    var poll = setInterval(function () { if (st.gone) return; checkReach(); tidy(); paint(); }, 1500);
    rx.pingT = setInterval(function () { if (rx.open && rx.ws) { try { rx.ws.send(JSON.stringify({ op: "ping", t: Date.now() })); } catch (e) { /* closes */ } } }, 20000);

    return {
      setMode: setMode, setLandscape: setLandscape,
      /** the iOS keyboard: {t: "text", text} committed text, {t: "key", key: "Return" | "ctrl+c" …} */
      input: function (op) {
        if (!op) return;
        if (op.t === "text") outText(String(op.text || ""));
        else if (op.t === "key" && op.key) { clearMods(); outKey(String(op.key)); }
      },
      kb: function (on) { st.kbOn = !!on; kbBtn.classList.toggle("on", !!on); },
      state: function () { var c = cur0(); return { mode: st.mode, z: st.z, px: st.px, py: st.py, cx: c.x, cy: c.y, u: st.u, v: st.v, pass: st.pass, landscape: st.landscape, rot: st.rot, last: st.last, remote: useRemote, open: rx.open, rw: rx.w, rh: rx.h }; },
      /** for tests: a stage point -> the iframe client point, and -> the remote pixel */
      map: function (x, y) { return toC({ x: x, y: y }); },
      remote: function (x, y) { return toR(toC({ x: x, y: y })); },
      detach: function () {
        st.gone = true; clearInterval(poll); clearInterval(rx.pingT); win.cancelAnimationFrame(raf);
        if (st.dragging) endDrag(cur0());
        flushMove(); flushPost();
        if (rx.ws) { var w = rx.ws; rx.ws = null; setTimeout(function () { try { w.close(); } catch (e) { /* gone */ } }, 300); }
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
