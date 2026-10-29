/* 3.8.4 touch layer (apps/web/takeover.js) in jsdom: gestures -> the ops sent to /ws/input, with exact mapping
 * (letterboxing, landscape, pinch zoom) for a viewer on another origin (InstaBox) and a 1600x900 desktop.
 * Run: node tests/js/takeover_touch_test.js (needs jsdom; tests/test_v384_takeover_input.py skips without it). */
"use strict";
const fs = require("fs"), path = require("path");
const { JSDOM } = require("jsdom");
const SRC = fs.readFileSync(path.join(__dirname, "../../apps/web/takeover.js"), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fails = 0;
function ok(c, msg) { if (!c) { fails++; console.log("FAIL", msg); } }
function near(a, b, d, msg) { ok(Math.abs(a - b) <= d, `${msg}: ${a} vs ${b}`); }

function setup(W, H, opts) {
  const dom = new JSDOM(`<!doctype html><body><div id="stage"><iframe id="f"></iframe></div></body>`, { url: "https://aiw.example/", pretendToBeVisual: true, runScripts: "outside-only" });
  const win = dom.window, doc = win.document;
  const sent = [];
  class FakeWS {
    constructor(u) { this.url = u; this.readyState = 0; FakeWS.last = this; setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); this.onmessage && this.onmessage({ data: JSON.stringify({ op: "hello", width: 1600, height: 900 }) }); }, 5); }
    send(s) { const d = JSON.parse(s); if (d.ops) sent.push(...d.ops); else if (d.op !== "ping") sent.push(d); }
    close() {}
  }
  win.WebSocket = FakeWS;
  win.fetch = () => Promise.reject(new Error("no fetch in test"));
  const stage = doc.getElementById("stage"), f = doc.getElementById("f");
  const size = { W, H };
  Object.defineProperty(stage, "clientWidth", { get: () => size.W });
  Object.defineProperty(stage, "clientHeight", { get: () => size.H });
  stage.getBoundingClientRect = () => ({ left: 0, top: 0, right: size.W, bottom: size.H, width: size.W, height: size.H });
  Object.defineProperty(f, "clientWidth", { get: () => parseFloat(f.style.width) || size.W });
  Object.defineProperty(f, "clientHeight", { get: () => parseFloat(f.style.height) || size.H });
  Object.defineProperty(f, "contentDocument", { get: () => null });   // another origin
  win.eval(SRC);
  const ctl = win.AiwTouch.attach(stage, f, Object.assign({ mid: "m1", token: "tk" }, opts || {}));
  const ov = stage.querySelector(".aiwt-ov");
  let id = 0;
  function touch(type, list, active) {
    const e = new win.Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(e, "changedTouches", { value: list.map((p) => ({ identifier: p.id, clientX: p.x, clientY: p.y })) });
    ov.dispatchEvent(e);
  }
  const T = {
    down(pts) { touch("touchstart", pts); }, move(pts) { touch("touchmove", pts); }, up(pts) { touch("touchend", pts); },
    async tap(x, y, hold) { const p = [{ id: ++id, x, y }]; T.down(p); await sleep(hold || 30); T.up(p); },
  };
  return { win, doc, ctl, sent, T, size, stage, FakeWS };
}

(async () => {
  // ---------- direct mode, portrait 390x844: the desktop is letterboxed (390 x 219.375 at top 312.3125)
  {
    const s = setup(390, 844, { mode: "direct" });
    await sleep(20);
    ok(s.FakeWS.last.url === "wss://aiw.example/ws/input/m1?token=tk", "socket url " + s.FakeWS.last.url);
    const st = s.ctl.state(); ok(st.remote && st.open && st.rw === 1600, "hello size");
    await s.T.tap(195, 312.3125 + 219.375 / 2);
    let c = s.sent.filter((o) => o.t === "click");
    ok(c.length === 1 && c[0].x === 800 && c[0].y === 450 && c[0].button === "left", "direct tap centre " + JSON.stringify(c));
    const mv = s.sent.filter((o) => o.t === "move");
    ok(s.sent.indexOf(mv[mv.length - 1]) < s.sent.indexOf(c[0]) && mv[mv.length - 1].x === 800, "final position before the click");
    await s.T.tap(1, 312.4);  // the top-left desktop pixel
    c = s.sent.filter((o) => o.t === "click"); ok(c[1].x === 4 && c[1].y === 0, "top-left " + JSON.stringify(c[1]));
    await s.T.tap(389.9, 312.3125 + 219.37);
    c = s.sent.filter((o) => o.t === "click"); ok(c[2].x === 1599 && c[2].y === 899, "bottom-right " + JSON.stringify(c[2]));
    s.sent.length = 0;
    await s.T.tap(195, 100);   // the black bar above the desktop: nothing
    ok(!s.sent.some((o) => o.t === "click"), "letterbox tap ignored");
    // long press = right click
    await s.T.tap(97.5, 312.3125 + 54.84, 600);
    c = s.sent.filter((o) => o.t === "click");
    ok(c.length === 1 && c[0].button === "right" && c[0].x === 400 && Math.abs(c[0].y - 225) <= 1, "long press right click " + JSON.stringify(c));
    // one-finger drag = down / moves / up
    s.sent.length = 0;
    const y0 = 312.3125 + 100;
    s.T.down([{ id: 50, x: 100, y: y0 }]);
    for (let i = 1; i <= 20; i++) { await sleep(4); s.T.move([{ id: 50, x: 100 + i * 5, y: y0 }]); }
    await sleep(30); s.T.up([{ id: 50, x: 200, y: y0 }]); await sleep(30);
    const kinds = s.sent.map((o) => o.t);
    ok(kinds[0] === "move" && kinds.includes("down") && kinds[kinds.length - 1] === "up", "drag ops " + kinds.join(","));
    const dn = s.sent.find((o) => o.t === "down"), upo = s.sent.find((o) => o.t === "up");
    ok(dn.x === Math.floor(100 / 390 * 1600) && upo.x === Math.floor(200 / 390 * 1600), "drag ends " + dn.x + " " + upo.x);
    ok(s.sent.filter((o) => o.t === "move").length <= 12, "moves coalesced (" + s.sent.filter((o) => o.t === "move").length + " for 20 touch moves in ~100ms)");
    // two-finger drag up 48px = 3 notches down
    s.sent.length = 0;
    s.T.down([{ id: 60, x: 150, y: 500 }, { id: 61, x: 230, y: 500 }]);
    for (let i = 1; i <= 4; i++) { await sleep(5); s.T.move([{ id: 60, x: 150, y: 500 - i * 12 }, { id: 61, x: 230, y: 500 - i * 12 }]); }
    s.T.up([{ id: 60, x: 150, y: 452 }, { id: 61, x: 230, y: 452 }]);
    const dy = s.sent.filter((o) => o.t === "scroll").reduce((a, o) => a + o.dy, 0);
    ok(dy === 3, "scroll notches " + dy);
    // pinch zoom 2x around the desktop centre: the spot under the fingers stays put
    const cy = 312.3125 + 219.375 / 2;
    s.T.down([{ id: 70, x: 145, y: cy }, { id: 71, x: 245, y: cy }]);
    for (let i = 1; i <= 10; i++) s.T.move([{ id: 70, x: 145 - i * 5, y: cy }, { id: 71, x: 245 + i * 5, y: cy }]);
    s.T.move([{ id: 70, x: 95 - 50, y: cy }, { id: 71, x: 295 + 50, y: cy }]);
    s.T.up([{ id: 70, x: 45, y: cy }, { id: 71, x: 345, y: cy }]);
    ok(s.ctl.state().z > 1.9, "zoomed " + s.ctl.state().z);
    const r = s.ctl.remote(195, cy); near(r.x, 800, 2, "zoomed centre x"); near(r.y, 450, 2, "zoomed centre y");
    const r2 = s.ctl.remote(195 + 10, cy);   // 10 css px at ~2x is ~half the remote px it was
    near(r2.x - r.x, 10 / 390 * 1600 / s.ctl.state().z, 1.5, "zoomed is finer");
    // keyboard: text, Chinese IME commit, Enter, sticky Ctrl, ^V
    s.sent.length = 0;
    const kb = s.stage.querySelector(".aiwt-kb");
    kb.value = "\u200b\u200ba"; kb.dispatchEvent(new s.win.InputEvent("input", { inputType: "insertText", data: "a" }));
    kb.dispatchEvent(new s.win.CompositionEvent("compositionstart", {}));
    kb.value = "\u200b\u200bni"; kb.dispatchEvent(new s.win.InputEvent("input", { inputType: "insertCompositionText", data: "ni", isComposing: true }));
    kb.dispatchEvent(new s.win.CompositionEvent("compositionend", { data: "你好" }));
    kb.dispatchEvent(new s.win.KeyboardEvent("keydown", { key: "Enter", cancelable: true }));
    kb.value = "\u200b"; kb.dispatchEvent(new s.win.InputEvent("input", { inputType: "deleteContentBackward" }));
    const bar = [...s.stage.querySelectorAll(".aiwt-bar button")];
    const b = (t) => bar.find((x) => x.textContent === t);
    b("Ctrl").click(); kb.value = "\u200b\u200bc"; kb.dispatchEvent(new s.win.InputEvent("input", { inputType: "insertText", data: "c" }));
    b("^V").click(); b("Alt").click(); b("Tab").click(); b("Esc").click();
    const want = [["text", "a"], ["text", "你好"], ["key", "Return"], ["key", "BackSpace"], ["key", "ctrl+c"], ["key", "ctrl+v"], ["key", "alt+Tab"], ["key", "Escape"]];
    const got = s.sent.filter((o) => o.t === "text" || o.t === "key").map((o) => [o.t, o.text || o.key]);
    ok(JSON.stringify(got) === JSON.stringify(want), "keys " + JSON.stringify(got));
    ok(!got.some((x) => x[1] === "ni"), "no pinyin sent");
    // the iOS keyboard path
    s.sent.length = 0; s.ctl.input({ t: "text", text: "中文" }); s.ctl.input({ t: "key", key: "ctrl+a" });
    ok(JSON.stringify(s.sent) === JSON.stringify([{ t: "text", text: "中文" }, { t: "key", key: "ctrl+a" }]), "native input " + JSON.stringify(s.sent));
    s.ctl.detach();
  }
  // ---------- 鼠标指针 (trackpad), landscape 844x390: desktop 693.33 x 390 at left 75.33
  {
    const s = setup(844, 390, {});
    await sleep(20);
    s.ctl.setMode("trackpad");
    ok(s.win.localStorage.getItem("aiw_touch_mode") === "trackpad", "mode remembered");
    s.sent.length = 0;
    await s.T.tap(800, 40);   // tap far from the cursor: the click is at the cursor (centre), not at the finger
    let c = s.sent.filter((o) => o.t === "click");
    ok(c.length === 1 && c[0].x === 800 && c[0].y === 450, "trackpad tap at cursor " + JSON.stringify(c));
    // slow 30px move right: finer than the finger (0.55x .. ~1x), relative
    s.sent.length = 0;
    s.T.down([{ id: 80, x: 300, y: 200 }]);
    for (let i = 1; i <= 30; i++) { await sleep(10); s.T.move([{ id: 80, x: 300 + i, y: 200 }]); }
    s.T.up([{ id: 80, x: 330, y: 200 }]); await sleep(30);
    const mv = s.sent.filter((o) => o.t === "move"), last = mv[mv.length - 1];
    const perCss = 1600 / 693.333;
    ok(last && last.y === 450 && last.x > 800 + 30 * 0.5 * perCss && last.x < 800 + 30 * 1.1 * perCss, "slow move " + JSON.stringify(last));
    ok(!s.sent.some((o) => o.t === "click"), "a move is not a click");
    // fast flick goes further than the finger
    const before = s.ctl.remote(s.ctl.state().cx + s.ctl.state().px, 0).x;
    s.T.down([{ id: 81, x: 100, y: 200 }]);
    for (let i = 1; i <= 5; i++) { await sleep(8); s.T.move([{ id: 81, x: 100 + i * 20, y: 200 }]); }
    s.T.up([{ id: 81, x: 200, y: 200 }]); await sleep(30);
    const after = s.ctl.remote(s.ctl.state().cx + s.ctl.state().px, 0).x;
    ok(after - before > 100 * perCss * 1.3, "fast move accelerates " + (after - before));
    // double tap = two clicks at the cursor (the desktop pairs them)
    s.sent.length = 0;
    await s.T.tap(400, 200); await sleep(60); await s.T.tap(402, 201);
    c = s.sent.filter((o) => o.t === "click"); ok(c.length === 2 && c[0].x === c[1].x, "double tap " + JSON.stringify(c));
    // tap-and-hold then drag = drag
    s.sent.length = 0;
    s.T.down([{ id: 90, x: 400, y: 200 }]); await sleep(520);
    for (let i = 1; i <= 10; i++) { await sleep(10); s.T.move([{ id: 90, x: 400 - i * 4, y: 200 }]); }
    s.T.up([{ id: 90, x: 360, y: 200 }]); await sleep(30);
    const k = s.sent.map((o) => o.t);
    ok(k.indexOf("down") >= 0 && k.lastIndexOf("up") === k.length - 1 && k.indexOf("down") < k.indexOf("up"), "hold-drag " + k.join(","));
    const d = s.sent.find((o) => o.t === "down"), u = s.sent.find((o) => o.t === "up"); ok(u.x < d.x - 40, "dragged left " + JSON.stringify(s.sent));
    // two-finger tap = right click at the cursor
    s.sent.length = 0;
    s.T.down([{ id: 91, x: 300, y: 200 }, { id: 92, x: 360, y: 200 }]); await sleep(40); s.T.up([{ id: 91, x: 300, y: 200 }, { id: 92, x: 360, y: 200 }]);
    c = s.sent.filter((o) => o.t === "click"); ok(c.length === 1 && c[0].button === "right" && c[0].x === u.x, "two-finger right click " + JSON.stringify(c));
    s.ctl.detach();
  }
  console.log(fails ? fails + " failed" : "ALL OK");
  process.exit(fails ? 1 : 0);
})();
