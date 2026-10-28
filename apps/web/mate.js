/* 球球 expression assistant (loaded after app.js/ui.js). A small Emotion Ball 球球 (dark theme) in the chat header shows
   what the current task is doing, and a mini one sits on each computer card (设置 › 电脑, 电脑 page) showing that machine.
   Driven only by the task event stream the app already receives (no model output). Mapping: mate-map.js.
   Off switch: 设置 › 关于 › 表情助手 (localStorage aiw_mate = "0"). Vendored engine: vendor/emotionball (visuals for
   personal learning/research only, no commercial use: see its README, LICENSE, NOTICE.md). This file only wraps app.js functions and adds DOM; it never changes their behaviour. */
"use strict";
(function () {
  if (!window.EmotionBall || !EmotionBall.create || !window.AiwMood) return;
  const MAP = window.AiwMood, ID = MAP.ID;
  const RM = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : { matches: false, addEventListener() {} };
  const on = () => localStorage.getItem("aiw_mate") !== "0";

  /* ---------- one 球球 instance in a host element */
  const mates = new Set();
  function Mate(host, opts) {
    this.host = host; this.id = null; this.opts = opts || {}; this.eng = null; this.settleT = 0;
    mates.add(this);
  }
  Mate.prototype.ensure = function () {
    if (this.eng || !this.host.isConnected) return this.eng;
    const still = RM.matches;
    // 球球 = the blob body in white with black eyes (3.8, MAP.LOOK) inside the amber ring of mate.css. No idle timer:
    // AIW drives standby/sleep itself from the machine state.
    this.eng = EmotionBall.create(this.host, { emotion: this.id || ID.IDLE, idle: false, shape: "blob", eyeScale: this.opts.eyeScale || 1.6,
      color: MAP.LOOK.color, eyeColor: MAP.LOOK.eyeColor,
      lite: !!this.opts.lite, autostart: !still && !document.hidden && this.vis !== false, label: "球球" });
    if (still) this.eng.renderStatic();
    return this.eng;
  };
  const HOLD = { "31": 1400, "39": 1400, "36": 700, "40": 700, "38": 900 };
  Mate.prototype.set = function (id, o) {
    o = o || {};
    const now = Date.now();
    if (this.holdUntil > now && id !== this.id) {
      if (this.queued && this.queued.settle && !o.settle) return;   // a queued 完成/出错 wins over a later idle refresh
      clearTimeout(this.holdT); this.queued = o; this.holdT = setTimeout(() => { this.queued = null; this.set(id, o); }, this.holdUntil - now); return;
    }
    clearTimeout(this.holdT); this.queued = null;
    if (id !== this.id) this.holdUntil = now + (HOLD[id] || 0);
    clearTimeout(this.settleT); this.settleT = 0;
    if (o.settle) this.settleT = setTimeout(() => this.set(this.opts.rest ? this.opts.rest() : ID.IDLE), o.settle);
    const changed = id !== this.id;
    this.id = id;
    this.host.dataset.mood = id;
    this.host.title = "球球 · " + (MAP.NAMES[id] || id);
    const eng = this.ensure(); if (!eng) return;
    if (changed) eng.handleAIMessage({ emotionId: id });
    if (RM.matches || document.hidden) eng.renderStatic();
    else if (o.burst) { try { eng.burst(24); } catch (e) { /* ignore */ } }
  };
  Mate.prototype.active = function (a) { if (this.eng) { this.eng.setActive(a); if (!a) this.eng.renderStatic(); } };
  Mate.prototype.destroy = function () { if (io) io.unobserve(this.host); clearTimeout(this.settleT); clearTimeout(this.holdT); if (this.eng) this.eng.destroy(); this.eng = null; mates.delete(this); };
  function sweep() { for (const m of [...mates]) if (!m.host.isConnected) m.destroy(); }
  function setAllActive() { const a = !document.hidden && !RM.matches; for (const m of mates) m.active(a && m.vis !== false); }
  // minis on hidden views / scrolled away stop their loop (one shared rAF, but no need to animate what nobody sees)
  const io = "IntersectionObserver" in window ? new IntersectionObserver((es) => {
    for (const e of es) { const m = e.target._mate; if (m) { m.vis = e.isIntersecting; m.active(m.vis && !document.hidden && !RM.matches); } }
  }) : null;
  document.addEventListener("visibilitychange", setAllActive);
  (RM.addEventListener ? RM.addEventListener.bind(RM) : RM.addListener.bind(RM))("change", setAllActive);

  /* ---------- what each task / machine is doing */
  const taskMood = {};          // task id -> emotion id from live events
  let machineUp = null;         // current machine from the top pill: true / false / "paused"
  let wakingUntil = 0;

  function threadTasks() { return (S.thread && S.thread.tasks) || []; }
  function liveTask() { return threadTasks().find((t) => LIVE.includes(t.status) || t.live || t.status === "queued"); }
  function machineRest() {
    if (Date.now() < wakingUntil) return ID.WAKE;
    return machineUp === false || machineUp === "paused" ? ID.SLEEP : ID.IDLE;
  }

  /* ---------- chat header */
  let head = null;
  function headHost() {
    if (!on()) return null;
    let el = document.getElementById("mateHead");
    if (!el) {
      const top = $("top"), ttl = top && top.querySelector(".ttl"); if (!ttl) return null;
      el = document.createElement("span"); el.id = "mateHead"; el.className = "mate mate-head"; el.setAttribute("role", "img");
      ttl.parentNode.insertBefore(el, ttl);
    }
    if (!head || head.host !== el) { if (head) head.destroy(); head = new Mate(el, { eyeScale: 1.7, rest: machineRest }); }
    return head;
  }
  function headFromState() {
    const h = headHost(); if (!h) return;
    const t = liveTask();
    if (t) {
      const id = taskMood[t.id] || MAP.replay(S.evs[t.id] || [], t.status);
      taskMood[t.id] = id;
      if (h.id !== id) h.set(id);
    } else if (!h.settleT || ![ID.DONE, ID.ERROR, ID.STOPPED, ID.REFUSED].includes(h.id)) {
      h.set(machineRest());
    }
  }

  const _onLiveEvent = onLiveEvent;
  onLiveEvent = function (t, e) {
    _onLiveEvent(t, e);
    try {
      const r = MAP.fromEvent(e); if (!r) return;
      taskMood[t.id] = r.settle ? undefined : r.id;
      const cur = threadTasks().some((x) => x.id === t.id);
      if (cur && on()) { const h = headHost(); if (h) h.set(r.id, r); }
      paintCards();
    } catch (err) { console.warn("mate", err); }
  };
  const _renderChat = renderChat;
  renderChat = function (scroll) { _renderChat(scroll); try { headFromState(); } catch (e) { console.warn("mate", e); } };
  const _go = go;
  go = function (view) { _go(view); document.body.classList.toggle("mate-chat", view === "chat"); setTimeout(scan, 0); };
  document.body.classList.toggle("mate-chat", !S.view || S.view === "chat");

  // the top pill (refreshMachine) tells online / offline / paused for the selected computer
  function readPill() {
    const p = $("pillAgent"); if (!p) return;
    const tx = p.textContent || "";
    const prev = machineUp;
    machineUp = /暂停/.test(tx) ? "paused" : /在线/.test(tx) ? true : /离线|错误/.test(tx) ? false : machineUp;
    if ((prev === false || prev === "paused") && machineUp === true) wakingUntil = Date.now() + 2500;
    if (prev !== machineUp) { headFromState(); paintCards(); if (wakingUntil > Date.now()) setTimeout(() => { headFromState(); paintCards(); }, 2600); }
  }
  if ($("pillAgent")) new MutationObserver(readPill).observe($("pillAgent"), { childList: true, characterData: true, subtree: true, attributes: true });

  /* ---------- computer cards */
  let tasksByMachine = {}, tasksAt = 0, localId = null;
  async function loadMachineTasks(force) {
    if (!force && Date.now() - tasksAt < 8000) return;
    tasksAt = Date.now();
    try {
      const [rows, ms] = await Promise.all([api("GET", "/api/tasks?limit=40"), localId ? null : api("GET", "/api/machines").catch(() => [])]);
      const m = {};
      for (const t of rows || []) if (MAP.isLive(t.status) && t.machine_id && !m[t.machine_id]) m[t.machine_id] = t;
      tasksByMachine = m;
      if (ms) { const l = ms.find((x) => x.provider !== "instabox"); localId = l ? l.id : localId; }
      paintCards();
    } catch (e) { /* not signed in yet */ }
  }
  function cardMood(mid, chipText) {
    const t = tasksByMachine[mid] || threadTasks().find((x) => x.machine_id === mid && (LIVE.includes(x.status) || x.live));
    const tx = chipText || "";
    return MAP.forMachine({
      liveId: t && taskMood[t.id], taskStatus: t && t.status,
      paused: /暂停/.test(tx), offline: /未连接|出错|离线|未配置/.test(tx), online: /已连接|在线/.test(tx) || undefined,
      waking: /添加中|创建|安装中|唤醒/.test(tx) || (mid === MID && Date.now() < wakingUntil),
    });
  }
  function mini(slot, mid, chipText, cls) {
    let el = slot.querySelector(":scope > .mate-mini");
    if (!el) {
      el = document.createElement("span"); el.className = "mate mate-mini " + (cls || ""); el.setAttribute("role", "img");
      slot.classList.add("has-mate"); slot.appendChild(el);
    }
    if (!el._mate) { el._mate = new Mate(el, { eyeScale: 1.8, lite: true }); if (io) io.observe(el); }
    el._mate.set(cardMood(mid, chipText));
  }
  function paintCards() {
    if (!on()) return;
    sweep();
    // 设置 › 电脑: cloud computers (one row each) and the local VM head row
    document.querySelectorAll("#cloudList .rw").forEach((rw) => {
      const b = rw.querySelector("[onclick*=pickMachine]"); const ic = rw.querySelector(".mach .ic");
      const mid = b && (b.getAttribute("onclick").match(/pickMachine\('([^']+)'/) || [])[1];
      if (mid && ic) mini(ic, mid, (rw.querySelector(".t .chip") || {}).textContent);
    });
    // rows re-rendered by cloudRefresh (app.js cloudCard) have no icon: give them a slot
    document.querySelectorAll("#cloudList .cloudrow").forEach((rw) => {
      const b = rw.querySelector("[onclick*=pickMachine]");
      const mid = b && (b.getAttribute("onclick").match(/pickMachine\('([^']+)'/) || [])[1]; if (!mid) return;
      let slot = rw.querySelector(":scope > .mate-slot");
      if (!slot) { slot = document.createElement("span"); slot.className = "mate-slot"; rw.insertBefore(slot, rw.firstChild); }
      mini(slot, mid, (rw.querySelector(".chip") || {}).textContent);
    });
    const lh = document.querySelector("#sVmNet .rw.head");
    if (lh && localId) { const k = lh.querySelector(".k"); if (k) mini(k, localId, (lh.querySelector(".chip") || {}).textContent, "inline"); }
    // 电脑 page: the selected computer
    const ib = document.querySelector("#v-computer .card .iconbox");
    if (ib) mini(ib, MID, ($("pillAgent") || {}).textContent);
  }
  function scan() {
    if (!on()) return;
    if (S.view === "settings" || S.view === "computer") loadMachineTasks();
    paintCards();
  }
  const sp = $("settingsPage");
  // the page is rebuilt by loadSettings and #cloudList again by cloudRefresh: repaint after either (ignore our own nodes)
  if (sp) new MutationObserver((ms) => {
    if (!ms.some((m) => !(m.target.closest && m.target.closest(".mate, .mate-slot")) && [...m.addedNodes].some((n) => n.nodeType === 1 && !n.classList.contains("mate") && !n.classList.contains("mate-slot")))) return;
    const full = ms.some((m) => m.target === sp);
    clearTimeout(scan.t); scan.t = setTimeout(() => { addToggle(); if (full) loadMachineTasks(true); else paintCards(); }, 60);
  }).observe(sp, { childList: true, subtree: true });
  setInterval(() => { if (!document.hidden && on() && (S.view === "settings" || S.view === "computer")) { loadMachineTasks(); paintCards(); } }, 4000);

  /* ---------- 设置 › 关于 › 表情助手 */
  function addToggle() {
    const about = document.querySelector("#g-about .rows");
    if (!about || document.getElementById("mateToggle")) return;
    const rw = document.createElement("div"); rw.className = "rw sw";
    rw.innerHTML = `<div class="k">表情助手<small>球球随任务状态变表情，电脑卡片上也有一只（仅供个人学习研究，非商用）</small></div><div class="v"></div>
      <div class="t"><input type="checkbox" id="mateToggle" aria-label="表情助手" ${on() ? "checked" : ""} /></div>`;
    const first = about.querySelector(".rw");
    about.insertBefore(rw, first ? first.nextSibling : null);
    rw.querySelector("input").onchange = (e) => setEnabled(e.target.checked);
  }
  function setEnabled(v) {
    localStorage.setItem("aiw_mate", v ? "1" : "0");
    document.body.classList.toggle("mate-off", !v);
    if (!v) { for (const m of [...mates]) m.destroy(); head = null; document.querySelectorAll(".mate").forEach((el) => el.remove());
      document.querySelectorAll(".has-mate").forEach((el) => el.classList.remove("has-mate")); }
    else { headFromState(); scan(); }
    if (typeof toast === "function") toast(v ? "表情助手已打开" : "表情助手已关闭");
  }
  document.body.classList.toggle("mate-off", !on());

  // create(): a 球球 of its own in any host (home stage, the thinking avatar); same engine and look, the host's CSS sizes it
  window.AiwMate = { set: (id, o) => { const h = headHost(); if (h) h.set(id, o); }, paint: paintCards,
    create: (host, opts) => { const m = new Mate(host, opts); if (io) { host._mate = m; io.observe(host); } return m; }, enabled: on, setEnabled, _taskMood: taskMood,
    state: () => [...mates].map((m) => ({ id: m.id, active: !!(m.eng && m.eng._active) })) };
  readPill(); headFromState(); scan();
})();
