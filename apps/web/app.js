/* AI Workstation console (vanilla JS, no build step). */
"use strict";
const Q = new URLSearchParams(location.search);
let MID = Q.get("machine") || localStorage.getItem("aiw_machine") || "ai-linux-001";
let TOKEN = Q.get("token") || localStorage.getItem("aiw_token") || "";
if (!TOKEN) TOKEN = (prompt("输入 AIW_API_TOKEN") || "").trim();
if (TOKEN) { localStorage.setItem("aiw_token", TOKEN); document.cookie = "aiw_token=" + TOKEN + "; path=/; SameSite=Strict"; }

const $ = (id) => document.getElementById(id);
const NATIVE = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.aiw;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const H = () => ({ "Content-Type": "application/json", ...(TOKEN ? { Authorization: "Bearer " + TOKEN } : {}) });
async function api(m, p, b) {
  const r = await fetch(p, { method: m, headers: H(), body: b !== undefined ? JSON.stringify(b) : undefined });
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch (e) {} throw new Error(typeof t === "string" ? t : JSON.stringify(t)); }
  return r.json();
}
function gallery(list) {
  const one = list.length === 1;
  return `<div class="gallery${one ? " one" : ""}">${list.map((p, i) => `<figure onclick="lightbox(this)" data-i="${i}" data-src="${esc(p.src)}" data-dl="${esc(p.dl)}" data-name="${esc(p.name)}">
    <img src="${esc(p.src)}" alt="${esc(p.name)}" loading="lazy" /><figcaption>${esc(p.name.split("/").pop().replace(/\.(png|jpe?g|webp|gif)$/i, ""))}</figcaption></figure>`).join("")}</div>`;
}
// a zip that holds screenshots: show the pictures inside it
const zipSeen = {};
function fillZipGalleries() {
  document.querySelectorAll(".zipgal:not(.done)").forEach(async (el) => {
    el.classList.add("done");
    const p = el.dataset.art;
    try {
      const list = zipSeen[p] || (zipSeen[p] = await fetch(artUrl(p) + "&images=1").then((r) => (r.ok ? r.json() : [])));
      if (list.length) el.outerHTML = gallery(list.map((m) => ({ src: artUrl(p) + "&member=" + encodeURIComponent(m.name), dl: artUrl(p, true) + "&member=" + encodeURIComponent(m.name), name: m.name })));
    } catch (e) { /* the zip still downloads */ }
  });
}
setInterval(fillZipGalleries, 800);
function lightbox(fig) {
  const figs = [...fig.parentNode.querySelectorAll("figure")]; let i = figs.indexOf(fig);
  let lb = document.getElementById("lightbox");
  if (!lb) { document.body.insertAdjacentHTML("beforeend", `<div id="lightbox" onclick="if(event.target===this||event.target.classList.contains('lb-x'))this.remove()"><button class="lb-x" aria-label="关闭">×</button><img /><div class="lb-bar"><span class="lb-n"></span><a class="lb-dl" download>下载</a></div><button class="lb-p" aria-label="上一张">‹</button><button class="lb-nx" aria-label="下一张">›</button></div>`); lb = document.getElementById("lightbox"); }
  const show = () => { const f = figs[i]; lb.querySelector("img").src = f.dataset.src; lb.querySelector(".lb-n").textContent = `${f.dataset.name.split("/").pop()}${figs.length > 1 ? `  ${i + 1} / ${figs.length}` : ""}`; const a = lb.querySelector(".lb-dl"); a.href = f.dataset.dl; a.setAttribute("download", f.dataset.name.split("/").pop()); lb.classList.toggle("multi", figs.length > 1); };
  lb.querySelector(".lb-p").onclick = (e) => { e.stopPropagation(); i = (i - 1 + figs.length) % figs.length; show(); };
  lb.querySelector(".lb-nx").onclick = (e) => { e.stopPropagation(); i = (i + 1) % figs.length; show(); };
  let x0 = null; lb.ontouchstart = (e) => { x0 = e.touches[0].clientX; };
  lb.ontouchend = (e) => { if (x0 == null) return; const dx = e.changedTouches[0].clientX - x0; x0 = null; if (Math.abs(dx) > 50 && figs.length > 1) { i = (i + (dx < 0 ? 1 : -1) + figs.length) % figs.length; show(); } };
  show();
}
const artUrl = (path, dl) => `/api/artifacts?${dl ? "download=1&" : ""}path=${encodeURIComponent(path)}${TOKEN ? "&token=" + encodeURIComponent(TOKEN) : ""}`;
const wsUrl = (p) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${p}${TOKEN ? (p.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : ""}`;
function toast(t) { const el = $("toast"); el.textContent = t; el.classList.add("on"); clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("on"), 2600); }
function ago(ts) { const s = Date.now() / 1000 - ts; if (s < 60) return "刚刚"; if (s < 3600) return Math.floor(s / 60) + " 分钟前"; if (s < 86400) return Math.floor(s / 3600) + " 小时前"; return new Date(ts * 1000).toLocaleDateString(); }
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString([], { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
function size(n) { return !n ? "" : n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }

/* ---------------------------------------------------------------- icons */
const IC = {
  plus: "M12 5v14M5 12h14", chat: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z",
  activity: "M3 12h4l3-8 4 16 3-8h4", target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 12h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2", monitor: "M3 4h18v12H3zM8 20h8M12 16v4",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  bell: "M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0", menu: "M4 6h16M4 12h16M4 18h16",
  clip: "M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5",
  mic: "M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2M12 19v3",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z", up: "M12 19V5M5 12l7-7 7 7", stop: "M7 7h10v10H7z", check: "M20 6 9 17l-5-5", x: "M18 6 6 18M6 6l12 12",
  chev: "M9 18l6-6-6-6", back: "M15 18l-6-6 6-6", bulb: "M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.2 1 2V17h6v-.3c0-.8.4-1.5 1-2A7 7 0 0 0 12 2z", file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  retry: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5", play: "M6 4l14 8-14 8z", hand: "M18 11V6a2 2 0 0 0-4 0M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8a8 8 0 0 0 16 0v-3a2 2 0 0 0-4 0",
  receipt: "M4 2v20l3-2 3 2 3-2 3 2 3-2 1 .7V2l-3 2-3-2-3 2-3-2-3 2zM8 8h8M8 12h8M8 16h5", trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
  bolt: "M13 2 3 14h9l-1 8 10-12h-9z", brain: "M12 5a3 3 0 0 0-5.9.8A3 3 0 0 0 4 11a3 3 0 0 0 2 5.2A3 3 0 0 0 12 18zM12 5a3 3 0 0 1 5.9.8A3 3 0 0 1 20 11a3 3 0 0 1-2 5.2A3 3 0 0 1 12 18z",
  plug: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4", key: "M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4",
  user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", speaker: "M11 5 6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14",
};
const icon = (n, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24"><path d="${IC[n] || ""}"/></svg>`;
function paintIcons(root = document) { root.querySelectorAll("i[data-i]").forEach((el) => { el.outerHTML = icon(el.dataset.i); }); }
// warm backgrounds that sit with the amber Aiw logo (old keys kept so saved profiles still map)
const AVATAR_BG = { amber: "#C87618", cream: "#F3E6D0", peach: "#F6C99A", sky: "#C87618", sand: "#F3E6D0", lilac: "#F6C99A" };
function avatarSVG() {
  return `<svg viewBox="0 0 48 48"><circle cx="24" cy="26" r="15" fill="#fff" stroke="#11191C" stroke-width="2"/><circle cx="18.5" cy="25" r="2.2" fill="#11191C"/><circle cx="29.5" cy="25" r="2.2" fill="#11191C"/><path d="M19 31.5c3 2.4 7 2.4 10 0" stroke="#11191C" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M24 11V6" stroke="#11191C" stroke-width="2" stroke-linecap="round"/><circle cx="24" cy="5" r="2.4" fill="#1473C8"/></svg>`;
}

/* ---------------------------------------------------------------- state */
const S = { view: "chat", threadId: localStorage.getItem("aiw_thread") || null, thread: null, threads: [], archived: false,
  evs: {}, evSt: {}, sock: {}, think: {}, profile: {}, notes: [], attach: [], take: false, agentFails: 0, actFilter: "all", tasks: [] };
const TOOL_CN = { screenshot: "截图", click: "点击", double_click: "双击", right_click: "右键", move: "移动鼠标", drag: "拖动", type: "输入文字",
  keypress: "按键", scroll: "滚动", open_app: "打开应用", close_app: "关闭应用", shell: "执行命令", browser_open: "打开浏览器",
  browser_navigate: "打开网址", browser_back: "后退", browser_forward: "前进", files_list: "列文件", files_read: "读文件", files_write: "写文件",
  files_download: "下载", send_file: "发送文件", done: "完成", fail: "放弃", update_plan: "更新计划", ask_user: "提问", remember: "记住",
  delegate: "交给助手", dispatch: "分派到多台电脑" };
const STATUS_CN = { queued: ["排队中", "warn"], running: ["进行中", "live"], paused: ["已暂停", "warn"], takeover: ["你在操作", "warn"],
  waiting_confirm: ["等你确认", "warn"], waiting_input: ["等你回答", "warn"], succeeded: ["完成", "ok"], failed: ["失败", "bad"],
  stopped: ["已停止", "bad"], interrupted: ["被中断", "bad"], cancelled: ["已取消", ""], pending: ["等待", ""] };
const LIVE = ["running", "paused", "takeover", "waiting_confirm", "waiting_input"];
const chip = (st) => { const [t, c] = STATUS_CN[st] || [st, ""]; return `<span class="chip ${c}">${t}</span>`; };
const toolName = (t) => TOOL_CN[t] || (t && t.startsWith("mcp_") ? "MCP · " + t.slice(4) : t && t.startsWith("ext_") ? "接口 · " + t.slice(4) : t);

/* ---------------------------------------------------------------- navigation */
const TITLES = { chat: "对话", activity: "活动", tracking: "追踪", routines: "例程", computer: "Agent 电脑", settings: "设置" };
function go(view) {
  S.view = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("on", v.id === "v-" + view));
  document.querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $("app").classList.toggle("nodesk", view !== "chat" || localStorage.getItem("aiw_desk") === "off");
  $("viewTitle").textContent = view === "chat" ? (S.thread ? S.thread.title : "新对话") : TITLES[view];
  $("viewSub").textContent = "";
  closeOverlays();
  if (view === "activity") loadActivity();
  if (view === "tracking") loadWatches();
  if (view === "routines") loadRoutines();
  if (view === "settings") loadSettings();
  if (view === "computer" || (view === "chat" && !deskLoaded.small)) loadDesktop();
}
function openDrawer() { $("side").classList.add("open"); $("shade").classList.add("on"); }
function closeOverlays() { $("side").classList.remove("open"); $("shade").classList.remove("on"); $("modal").classList.remove("on"); $("notes").classList.remove("on"); }
function toggleDeskPane() { localStorage.setItem("aiw_desk", localStorage.getItem("aiw_desk") === "off" ? "on" : "off"); go(S.view); }
function modal(html) { $("modal").innerHTML = html; paintIcons($("modal")); $("modal").classList.add("on"); $("shade").classList.add("on"); }

/* ---------------------------------------------------------------- threads */
async function loadThreads() {
  try { S.threads = await api("GET", "/api/threads" + (S.archived ? "?archived=true" : "")); } catch (e) { return; }
  $("threadList").innerHTML = S.threads.map((t) => `<button class="${t.id === S.threadId ? "on" : ""}" onclick="openThread('${t.id}')">
    <div class="t1">${esc(t.title)}</div><div class="t2">${t.last_status ? (STATUS_CN[t.last_status] || [t.last_status])[0] + " · " : ""}${ago(t.updated_at)}</div>
    <span class="thdel" title="删除对话" onclick="event.stopPropagation();deleteThread('${t.id}')">×</span></button>`).join("")
    || `<div class="small pad">还没有对话</div>`;
}
function toggleArchived() { S.archived = !S.archived; $("archToggle").textContent = S.archived ? "返回对话" : "查看已归档"; loadThreads(); }
function newThread() { S.threadId = null; S.thread = null; localStorage.removeItem("aiw_thread"); go("chat"); renderChat(); loadDraft(); $("prompt").focus(); loadThreads(); }
async function openThread(id) {
  S.threadId = id; localStorage.setItem("aiw_thread", id);
  go("chat"); await refreshThread(true); loadDraft(); loadThreads();
  followTaskMachine();
}
/* aiw-roles-1: tasks go to the computer whose role fits them, so the desktop pane shows the computer of the task you
   opened (the latest run in this conversation), not whichever computer was picked last */
function followTaskMachine() {
  const ts = ((S.thread && S.thread.tasks) || []).filter((t) => t.machine_id && t.status !== "cancelled");
  const mid = ts.length ? ts[ts.length - 1].machine_id : "";
  if (mid && mid !== MID && (S.machines || []).some((m) => m.id === mid)) pickMachine(mid);
}
async function refreshThread(scroll) {
  if (!S.threadId) { S.thread = null; renderChat(); return; }
  try { S.thread = await api("GET", `/api/threads/${S.threadId}`); }
  catch (e) { S.threadId = null; localStorage.removeItem("aiw_thread"); S.thread = null; renderChat(); return; }
  $("viewTitle").textContent = S.thread.title;
  const tasks = S.thread.tasks.slice(-12);
  await Promise.all(tasks.map(async (t) => {
    if (!t.session_id) return;
    if (!t.live && S.evs[t.id] && S.evSt[t.id] === t.status) return;
    if (!t.live || !S.sock[t.session_id]) {
      try { const s = await api("GET", `/api/sessions/${t.session_id}`); S.evs[t.id] = s.events; S.evSt[t.id] = t.status; t.pending_confirm = s.pending_confirm; t.pending_question = s.pending_question; } catch (e) {}
    }
    if (t.live) { subscribe(t); keepPending(t); }
  }));
  renderChat(scroll);
}
async function renameThread() {
  if (!S.thread) return; const n = prompt("对话名称", S.thread.title); if (!n) return;
  await api("PATCH", `/api/threads/${S.thread.id}`, { title: n }); refreshThread(); loadThreads();
}
async function deleteThread(id) {
  id = id || (S.thread && S.thread.id); if (!id) return;
  const t = S.threads.find((x) => x.id === id) || S.thread || {};
  if (!confirm(`删除对话「${t.title || ""}」？里面的任务记录会一起删除，电脑上的文件不受影响。`)) return;
  await api("DELETE", `/api/threads/${id}`); toast("已删除");
  if (S.threadId === id) newThread(); else loadThreads();
}
async function archiveThread(flag) { await api("PATCH", `/api/threads/${S.thread.id}`, { archived: flag }); toast(flag ? "已归档" : "已恢复"); newThread(); }

/* ---------------------------------------------------------------- live session sockets */
// The socket outlives refreshThread, which swaps in fresh task objects. Live events must land on the task object
// that is rendered now (not the one captured when the socket opened), or confirm/ask cards only show after a reload.
S.pend = S.pend || {};
function curTask(t) { return ((S.thread && S.thread.tasks) || []).find((x) => x.id === t.id) || t; }
function keepPending(t) {
  const p = S.pend[t.id]; if (!p) return;
  if (!t.pending_confirm && p.confirm) t.pending_confirm = p.confirm;
  if (!t.pending_question && p.question) t.pending_question = p.question;
  if (t.pending_confirm && t.status === "running") t.status = "waiting_confirm";
  if (t.pending_question && t.status === "running") t.status = "waiting_input";
}
function subscribe(t) {
  if (S.sock[t.session_id]) return;
  const ws = new WebSocket(wsUrl(`/ws/session/${t.session_id}`));
  S.sock[t.session_id] = ws; S.evs[t.id] = [];
  ws.onopen = () => { if (window.onAiwEventsOpen) window.onAiwEventsOpen(); };
  ws.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (window.onAiwEvent) { try { window.onAiwEvent(e); } catch (err) { console.warn(err); } }   // home.js: the persistent chat if (e.kind !== "event") return;
    // reasoning streamed while the model thinks: kept aside (replaced each time), not appended to the event list
    if (e.type === "agent.thinking" && (e.data || {}).live) { S.think[t.id] = e.data; onLiveEvent(curTask(t), e); return; }
    if (e.type === "agent.thinking" || e.type === "agent.decision") delete S.think[t.id];
    (S.evs[t.id] = S.evs[t.id] || []).push(e); onLiveEvent(curTask(t), e);
  };
  ws.onclose = () => { delete S.sock[t.session_id]; };
}
let rerenderQ = null;
function onLiveEvent(t, e) {
  const d = e.data || {};
  const pend = (S.pend[t.id] = S.pend[t.id] || {});
  if (e.type === "agent.confirm_required") { t.pending_confirm = pend.confirm = d; t.status = "waiting_confirm"; }
  if (e.type === "agent.input_required") { t.pending_question = pend.question = d; t.status = "waiting_input"; }
  if (e.type === "agent.input_answered") { t.pending_question = pend.question = null; if (t.status === "waiting_input") t.status = "running"; }
  if (e.type === "agent.action_result" && (t.pending_confirm || pend.confirm)) { t.pending_confirm = pend.confirm = null; if (t.status === "waiting_confirm") t.status = "running"; }
  if (e.type === "agent.takeover") { t.status = "takeover"; setTake(true, true); }
  if (e.type === "agent.resumed") { t.status = "running"; setTake(false, true); }
  if (e.type === "agent.paused") t.status = "paused";
  if (e.type === "task.finished" || e.type === "task.failed") {
    t.status = d.status || (e.type === "task.finished" ? "succeeded" : "failed"); t.result = d.result; t.live = false; delete S.pend[t.id];
    if (e.type === "task.finished" && localStorage.getItem("aiw_tts") === "1") speak(d.result);
    setTimeout(() => refreshThread(), 400);
  }
  if (!rerenderQ) rerenderQ = requestAnimationFrame(() => { rerenderQ = null; renderChat(); });
}

/* ---------------------------------------------------------------- chat render */
const SUGS = ["打开 Chrome 搜索今天的科技新闻，挑 3 条总结给我", "用 Python 在 ~/workspace 新建 周报.docx，写三条要点，然后发给我", "查一下磁盘空间和内存占用", "打开 Hacker News，把前 5 条标题整理成表格发给我"];
/* v3.9.18: the task view follows new output only while you are at the bottom. Scrolling up (wheel, touch, keys,
   scrollbar) stops the follow until you come back down; a re-render keeps where you were reading and which
   folds (操作 / 确认记录 / 思考过程) you opened or closed. */
S.stick = true; S.det = S.det || {};
let progScrollAt = 0;
function chatDist(b) { return b.scrollHeight - b.scrollTop - b.clientHeight; }
function renderChat(scroll) {
  const box = $("msgs");
  if (scroll) S.stick = true;
  const t0 = box.scrollTop;
  box.querySelectorAll("details[data-k]").forEach((d) => { S.det[d.dataset.k] = d.open; });
  if (!S.thread || !S.thread.tasks.length) {
    box.innerHTML = `<div class="welcome"><div class="avatar" style="background:${AVATAR_BG[S.profile.avatar] || AVATAR_BG.amber}">${avatarSVG()}</div>
      <div class="title">${esc(S.profile.name ? "你好，" + S.profile.name : "想让我做什么？")}</div>
      <div class="muted">我在自己的 Linux 电脑上替你操作浏览器、终端和文件，做完把结果和文件交给你。</div>
      <div class="sugs">${SUGS.map((s) => `<button onclick="usePrompt(this.textContent)">${esc(s)}</button>`).join("")}</div></div>`;
    renderQueue(); setComposerBusy(null); return;
  }
  const from = S.thread.parent_thread_id ? `<button class="fromp" onclick="openThread('${S.thread.parent_thread_id}')"><span>↖ 来自主任务</span><b>${esc(S.thread.parent_title || "")}</b></button>` : "";
  const head = `<div class="row gap" style="justify-content:flex-end"><button class="btn sm" onclick="renameThread()">重命名</button>
    <button class="btn sm" onclick="archiveThread(${S.thread.archived ? "false" : "true"})">${S.thread.archived ? "取消归档" : "归档"}</button>
    <button class="btn sm" style="color:var(--bad)" onclick="deleteThread()">删除</button></div>`;
  box.innerHTML = head + from + S.thread.tasks.filter((t) => t.status !== "cancelled").map(renderTask).join("");
  paintIcons(box);
  renderQueue();
  const live = S.thread.tasks.find((t) => LIVE.includes(t.status) || t.live);
  setComposerBusy(live || null);
  if (live && live.status === "takeover" && !S.take) setTake(true, true);
  box.querySelectorAll("details[data-k]").forEach((d) => { const k = d.dataset.k; if (k in S.det) d.open = S.det[k]; });
  const keep = () => { if (S.stick) scrollLatest(); else { progScrollAt = Date.now(); box.scrollTop = t0; } };
  keep(); requestAnimationFrame(keep);   // again after ui.js / mate.js have patched the cards
}
function renderTask(t) {
  const evs = S.evs[t.id] || [];
  // 3.8.5: a 继续 / 修改 run of the task above is the same task (tasks.continues), labelled as such
  const runTag = t.continues ? ({ edit: "修改后接着做", retry: "接着上次继续", resume: "自动接着做" }[t.run_mode] || "接着上次继续") : "";
  const user = `<div class="u"><div><div class="bubble">${runTag ? `<span class="chip run-tag">↻ ${runTag}</span> ` : ""}${esc(t.prompt)}</div><div class="meta">${when(t.created_at)}${!t.continues && t.source && t.source !== "chat" ? " · " + ({ routine: "例程", retry: "重试", resume: "自动恢复", dispatch: "主任务分派" }[t.source] || t.source) : ""}</div></div></div>`;
  if (t.status === "sending") return user + `<div class="a"><div class="small" style="margin-left:8px">正在发送…</div></div>`;
  if (t.status === "queued") return user + `<div class="a"><div class="small" style="margin-left:8px">排队中，前面的任务完成后自动开始</div></div>`;
  let plan = t.plan && t.plan.steps && t.plan.steps.length ? t.plan : null;
  const steps = [], files = [];
  let cur = null, last = null, startTs = null, endTs = null;
  for (const e of evs) {
    const d = e.data || {};
    if (e.type === "task.started") startTs = e.ts;
    if (e.type === "agent.plan") plan = { steps: d.plan || [], done: d.done || [] };
    if (e.type === "agent.plan_update" && plan) plan = { ...plan, done: d.done || [] };
    if (e.type === "agent.decision") cur = { tool: d.tool, args: d.args, thought: d.thought, why: d.reasoning, ok: null, ms: d.llm_ms, tok: d.out_tokens, tries: d.attempts };
    if (e.type === "agent.action_result" && cur && cur.tool === d.tool) { cur.ok = d.success; cur.err = d.error; cur.tms = d.tool_ms; cur.wms = d.wait_ms; if (cur.tool !== "screenshot") steps.push(cur); cur = null; }
    if (e.type === "agent.subagent" && d.phase === "step") steps.push({ tool: d.tool, args: d.args, sub: true, ok: true });
    if (e.type === "agent.file") files.push(d);
    if (e.type === "agent.message") steps.push({ tool: "message", thought: d.text, ok: true });
    if (e.type === "agent.observation" && d.vision) steps.push({ tool: "vision", thought: d.vision, ok: true });
    if (e.type === "agent.error" && d.raw) steps.push({ tool: "message", thought: d.error + "：" + d.raw, ok: false });
    if (["agent.observation", "agent.thinking", "agent.action"].includes(e.type)) last = e;
    if (e.type === "task.finished" || e.type === "task.failed") endTs = e.ts;
  }
  const liveNow = LIVE.includes(t.status);
  let status = "";
  if (liveNow && last) {
    const d = last.data || {};
    status = last.type === "agent.observation" ? "正在看屏幕…" : last.type === "agent.thinking" ? ((last.data || {}).phase === "vision" ? "视觉模型在看屏幕…" : "正在思考…") : `正在${toolName(d.tool)}…`;
  } else if (liveNow) status = "启动中…";
  const dur = startTs ? Math.round(((endTs || Date.now() / 1000) - startTs)) : null;
  // 3.7: the agent's side looks like the chat's: the small 球球 head + the shared task card (home.css .tcard)
  let html = `<div class="a hwa nm tview"><span class="mate amber hav" aria-hidden="true">${typeof hAvSvg === "function" ? hAvSvg() : ""}</span><div class="work tcard st-${esc(t.status)}">
    <div class="head">${liveNow && t.status === "running" ? '<span class="spin"></span>' : chip(t.status)}
      <div class="grow">${esc(status || (dur != null ? `用时 ${dur}s · ${steps.length} 步` : ""))}</div>
      ${liveNow ? `<button class="btn sm" onclick="ctl('${t.session_id}','${t.status === "paused" || t.status === "takeover" ? "resume" : "pause"}')">${t.status === "paused" || t.status === "takeover" ? "继续" : "暂停"}</button>` : ""}</div>`;
  const th = liveNow && last && last.type === "agent.thinking" ? S.think[t.id] : null;
  if (th && th.text) html += `<div class="thinklive"><div class="tl">${icon("bulb")}思考过程<em>${th.chars || th.text.length} 字</em></div><div class="tt"><div>${esc(th.text)}</div></div></div>`;
  const barPct = plan ? 100 * new Set(plan.done || []).size / Math.max(1, plan.steps.length) : t.status === "succeeded" ? 100 : 0;
  if (barPct) html += `<div class="hc-bar tc-bar"><i style="width:${Math.max(3, Math.min(100, Math.round(barPct)))}%"></i></div>`;
  if (plan) {
    const done = new Set(plan.done || []); const firstOpen = plan.steps.findIndex((_, i) => !done.has(i));
    // v3.9.19: same as iOS — n/m count, ticks land live (agent.plan_update), the current step carries an arrow
    html += `<div class="plan"><div class="plan-n">${done.size}/${plan.steps.length} 项</div>${plan.steps.map((s, i) => `<div class="st ${done.has(i) ? "done" : i === firstOpen && liveNow ? "cur" : ""}"><span class="ck">${done.has(i) ? icon("check") : i === firstOpen && liveNow ? "→" : ""}</span><span class="tx">${esc(s)}</span></div>`).join("")}</div>`;
  }
  html += renderKids(t, evs);
  if (steps.length) {
    html += `<details class="steps" data-k="steps:${esc(t.id)}" ${liveNow ? "open" : ""}><summary>${icon("chev", "chev")}${steps.length} 个操作</summary><div class="steplist">${steps.slice(-40).map((s, i) =>
      `<div class="s ${s.ok === false ? "err" : ""} ${s.sub ? "sub" : ""}"><b>${esc(s.tool === "message" ? "说明" : s.tool === "vision" ? "看屏幕" : toolName(s.tool))}</b><span>${esc(stepText(s))}${(s.ms || s.tms || s.wms) ? `<em class="ms" title="思考 = 模型耗时，工具 = 执行耗时，等确认 = 等你点允许">${stepTimes(s)}${s.tok ? ` · ${s.tok} tok` : ""}${s.tries > 1 ? ` · 重试${s.tries - 1}` : ""}</em>` : ""}</span></div>`
      // the step's thought shows inline; only the long reasoning stays folded
      + (s.thought && s.tool !== "message" && s.tool !== "vision" ? `<p class="thinl">${esc(s.thought)}</p>` : "")
      + (s.why ? `<details class="why" data-k="why:${esc(t.id)}:${i}"><summary>${icon("chev", "chev")}思考过程</summary><pre>${esc(s.why)}</pre></details>` : "")).join("")}</div></details>`;
  }
  const fq = t.pending_question && liveNow && t.pending_question.form;
  if (fq && fq.kind === "form") {
    S.forms[t.session_id] = fq;
    html += `<div class="actcard"><div class="ac-ic">${icon("menu")}</div><div class="ac-b"><div class="ac-k">需要操作</div><b>${esc(fq.title)}</b>
      ${fq.description ? `<p>${esc(fq.description)}</p>` : ""}<button class="btn primary ac-go" onclick="openForm('${t.session_id}')">继续</button></div></div>`;
  } else if (fq && fq.kind === "takeover") {
    html += takeoverCard(fq, t);   // approval.js: what to do, where, which computer; open / hand back / cancel
  } else if (t.pending_question && liveNow) {
    const q = t.pending_question;
    html += `<div class="ask q"><div><b>${esc(S.profile.agent_name || "Agent")} 在问：</b>${esc(q.question)}</div>
      ${q.options && q.options.length ? `<div class="opts">${q.options.map((o) => `<button class="btn sm primary" onclick="answerQ('${t.session_id}', ${esc(JSON.stringify(o))})">${esc(o)}</button>`).join("")}</div>` : ""}
      <form onsubmit="answerQForm(event,'${t.session_id}')"><input name="a" placeholder="回答…" /><button class="btn sm primary">回复</button></form></div>`;
  }
  if (t.pending_confirm && liveNow) html += apvCard(t.pending_confirm, t.session_id);   // approval.js: plain summary
  html += apvLog(evs, t.id);   // past confirmations / takeovers with the user's decision (older ones folded)
  const allFiles = files.length ? files : (t.files || []).map((f) => ({ name: f.path.split("/").pop(), artifact: f.path }));
  // pictures show as pictures (a grid when there are several); everything else is a download card
  const pics = allFiles.filter((f) => /\.(png|jpe?g|webp|gif)$/i.test(f.name));
  if (pics.length) html += gallery(pics.map((f) => ({ src: artUrl(f.artifact), dl: artUrl(f.artifact, true), name: f.name })));
  for (const f of allFiles) {
    if (pics.includes(f)) continue;
    const zip = /\.zip$/i.test(f.name);
    html += `<a class="filecard" href="${artUrl(f.artifact, true)}" download="${esc(f.name)}"><span class="iconbox">${icon("file")}</span><span class="grow"><b>${esc(f.name)}</b><div class="small">${size(f.size)}${f.truncated ? " · 超过 40MB 已截断" : ""} · 点击下载</div></span></a>`;
    if (zip) html += `<div class="zipgal" data-art="${esc(f.artifact)}"></div>`;
  }
  if (t.result && !liveNow) html += `<div class="bubble result ${t.status === "succeeded" ? "ok" : "bad"}" style="max-width:100%">${esc(t.result)}</div>`;
  html += `<div class="foot">`;
  if (liveNow) html += `<button class="btn sm" onclick="go('computer')">${icon("monitor")}看桌面</button><button class="btn sm" onclick="taskInstruct('${t.id}')">补充要求</button><button class="btn sm danger" onclick="ctl('${t.session_id}','stop')">${icon("stop")}停止</button>`;
  if (t.session_id) html += `<button class="btn sm" onclick="replay('${t.session_id}')">${icon("play")}回放</button><button class="btn sm" onclick="receipts('${t.id}')">${icon("receipt")}收据</button><button class="btn sm" onclick="exportTask('${t.id}')">导出</button>`;
  if (["failed", "stopped", "interrupted"].includes(t.status)) html += `<button class="btn sm primary" onclick="retry('${t.id}')">${icon("retry")}继续</button>`;
  if (!["queued", "sending"].includes(t.status)) html += `<button class="btn sm" onclick="taskEdit('${t.id}')">修改任务</button>`;
  if (!liveNow && t.result && ("speechSynthesis" in window || NATIVE)) html += `<button class="btn sm" onclick="speak(${esc(JSON.stringify(t.result))})">${icon("speaker")}朗读</button>`;
  html += `</div></div></div>`;
  return user + html;
}
// pieces this task dispatched to other computers: machine, live status, a way into each child's own chat
function renderKids(t, evs) {
  let kids = null;
  for (const e of evs) if (e.type === "agent.dispatch" && (e.data || {}).children) kids = e.data.children;
  const known = {}; for (const c of t.children || []) known[c.task_id] = c;
  kids = (kids || t.children || []).map((c) => ({ ...known[c.task_id], ...c, thread_id: c.thread_id || (known[c.task_id] || {}).thread_id }));
  if (!kids.length) return "";
  const done = kids.filter((c) => ["succeeded", "failed", "stopped", "cancelled", "interrupted"].includes(c.status)).length;
  return `<div class="kids"><div class="kh">${icon("monitor")}分派到 ${new Set(kids.map((c) => c.machine)).size} 台电脑 · ${done}/${kids.length} 完成</div>${kids.map((c) =>
    `<button class="kid" ${c.thread_id ? `onclick="openThread('${c.thread_id}')"` : "disabled"}><span class="km">${esc(c.machine_name || c.machine)}</span><span class="kg">${esc(c.goal || "")}</span>${chip(c.status)}<span class="kgo">${icon("chev")}</span></button>`).join("")}</div>`;
}
function stepText(s) {
  if (s.tool === "message" || s.tool === "vision") return s.thought || "";
  const a = s.args || {};
  if (s.tool === "dispatch" && Array.isArray(a.tasks)) return a.tasks.map((x) => `${x.machine || "auto"}: ${x.goal || ""}`).join(" ｜ ").slice(0, 200);
  let t = a.command || a.url || a.text || a.key || a.name || a.path || a.goal || a.question || (a.x != null ? `(${a.x}, ${a.y})` : "");
  if (!t && s.args) t = JSON.stringify(s.args);
  if (s.ok === false && s.err) t += " — " + s.err;
  return String(t).slice(0, 200);
}
function scrollLatest(smooth) { const b = $("msgs"); S.stick = true; progScrollAt = Date.now(); b.scrollTo({ top: b.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }
(() => {
  const b = $("msgs");
  const up = () => { if (chatDist(b) > 4) S.stick = false; };
  b.addEventListener("wheel", (e) => { if (e.deltaY < 0) S.stick = false; }, { passive: true });
  b.addEventListener("touchmove", up, { passive: true });
  b.addEventListener("keydown", (e) => { if (["ArrowUp", "PageUp", "Home"].includes(e.key)) S.stick = false; });
  b.addEventListener("pointerdown", (e) => { if (e.target === b) up(); });   // grabbing the scrollbar
  b.addEventListener("scroll", () => {
    const dist = chatDist(b);
    // only the user's own scroll changes the follow: back near the bottom follows again, anywhere else stops it
    if (Date.now() - progScrollAt > 150) S.stick = dist < 60;
    $("toLatest").classList.toggle("on", dist > 300);
  });
})();

function renderQueue() {
  const q = S.thread ? S.thread.tasks.filter((t) => t.status === "queued") : [];
  // 3.8: each queued message says why it waits and has 立即发送 (the computer runs one task at a time) / 移除
  const why = (t) => { const x = t.queue || {}; if (x.first) return "下一条就发它";
    if (x.busy && x.busy.task_id) return `电脑正在做「${esc((x.busy.prompt || "").slice(0, 20))}」（${(STATUS_CN[x.busy.status] || [x.busy.status])[0]}），做完自动发送`;
    return x.held ? "上个任务被停止，排队已暂停" : "马上开始"; };
  $("queueBar").innerHTML = q.map((t) => `<div class="q"><span>排队：${esc(t.prompt)}<small class="muted"> · ${why(t)}</small></span>
    <button class="btn sm" onclick="sendQueuedNow('${t.id}')">立即发送</button><button class="icon soft" onclick="cancelQueued('${t.id}')" aria-label="移除">${icon("x")}</button></div>`).join("")
    + (q.length && S.thread.queue_held ? `<button class="btn sm primary" onclick="resumeQueue()">发送排队消息</button>` : "");
}

/* ---------------------------------------------------------------- composer */
let busyTask = null;
function setComposerBusy(t) {
  busyTask = t;
  const b = $("sendBtn");
  b.classList.toggle("stop", !!t);
  b.innerHTML = icon(t ? "stop" : "up");
  b.setAttribute("aria-label", t ? "停止" : "发送");
  $("composerHint").textContent = t ? "Agent 正在工作 · 现在发送的消息会排队，按顺序执行" : "";
  $("agentStatus").textContent = t ? (STATUS_CN[t.status] || [t.status])[0] : "空闲";
  $("agentDot").className = "sdot" + (t ? " on" : "");
}
function autoGrow(el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 180) + "px"; }
function promptKey(e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 760) { e.preventDefault(); submitPrompt(e); } }
function draftKey() { return "aiw_draft_" + (S.threadId || "new"); }
function saveDraft() { localStorage.setItem(draftKey(), $("prompt").value); }
function loadDraft() { $("prompt").value = localStorage.getItem(draftKey()) || ""; autoGrow($("prompt")); }
function usePrompt(t) { $("prompt").value = t; autoGrow($("prompt")); $("prompt").focus(); }
let sending = false;
async function submitPrompt(e) {
  if (e) e.preventDefault();
  if (sending) return;
  const raw = $("prompt").value;
  let p = raw.trim();
  if (!p && busyTask) { ctl(busyTask.session_id, "stop"); return; }   // the button is a stop square while working
  if (!p) return;
  if (S.attach.length) p += "\n\n附件（已放到电脑上）：\n" + S.attach.map((a) => "- " + a.vm_path).join("\n");
  const iso = localStorage.getItem("aiw_iso") === "1";
  // optimistic: clear the box and show the message right away, the server round trip happens behind it
  sending = true;
  const attach = S.attach;
  $("prompt").value = ""; autoGrow($("prompt")); S.attach = []; renderAttach();
  const tmp = { id: "tmp-" + Date.now(), prompt: p, status: "sending", created_at: Date.now() / 1000 };
  if (!S.thread) S.thread = { id: null, title: "新对话", tasks: [] };
  S.thread.tasks.push(tmp); renderChat(true);
  $("sendBtn").classList.add("busy");
  try {
    const r = await api("POST", "/api/tasks", { prompt: p, machine_id: MID, thread_id: S.threadId, isolated: iso });
    localStorage.removeItem(draftKey());
    if (iso) { localStorage.removeItem("aiw_iso"); $("isoToggle").checked = false; }
    S.threadId = r.thread_id; localStorage.setItem("aiw_thread", r.thread_id);
    if (r.notice) toast(r.notice);   // v3.9.3: a cooling cloud computer handed this to the backup computer
    else if (r.queued) toast("已加入队列");
    refreshThread(true); loadThreads();
  } catch (err) {
    S.thread.tasks = S.thread.tasks.filter((t) => t !== tmp); renderChat();
    $("prompt").value = raw; autoGrow($("prompt")); S.attach = attach; renderAttach();
    toast("发送失败：" + err.message);
  } finally { sending = false; $("sendBtn").classList.remove("busy"); }
}
async function uploadFiles(list) {
  for (const f of list) {
    if (f.size > 40 * 1048576) { toast(f.name + " 超过 40MB"); continue; }
    toast("上传 " + f.name + "…");
    const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(f); });
    try { S.attach.push(await api("POST", "/api/uploads", { name: f.name, content_b64: b64, machine_id: MID, thread_id: S.threadId || null })); renderAttach(); }
    catch (e) { toast("上传失败：" + e.message); }
  }
  $("file").value = "";
}
function renderAttach() {
  $("attach").innerHTML = S.attach.map((a, i) => `<span class="chip">${icon("file")}${esc(a.name)}<button class="link" onclick="S.attach.splice(${i},1);renderAttach()">${icon("x")}</button></span>`).join("");
}

/* voice: dictation + read aloud (browser speech APIs, nothing leaves the device but the text) */
let rec = null;
let nativeBase = null;
window.aiwDictation = (text, final) => {           // called by the iOS app (SFSpeechRecognizer)
  if (nativeBase === null) nativeBase = $("prompt").value;
  $("prompt").value = (nativeBase ? nativeBase + " " : "") + text; autoGrow($("prompt"));
  if (final) { nativeBase = null; $("micBtn").classList.remove("rec"); saveDraft(); }
};
window.aiwDictationError = (msg) => { nativeBase = null; $("micBtn").classList.remove("rec"); toast("语音输入：" + msg); };
function toggleMic() {
  if (NATIVE) { const on = !$("micBtn").classList.contains("rec"); NATIVE.postMessage({ op: on ? "dictate_start" : "dictate_stop" }); $("micBtn").classList.toggle("rec", on); if (!on) nativeBase = null; return; }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast("这个浏览器不支持语音输入，可以用键盘上的听写键"); return; }
  if (rec) { rec.stop(); return; }
  rec = new SR(); rec.lang = "zh-CN"; rec.interimResults = true; rec.continuous = true;
  const base = $("prompt").value;
  rec.onresult = (e) => { let t = ""; for (const r of e.results) t += r[0].transcript; $("prompt").value = (base ? base + " " : "") + t; autoGrow($("prompt")); };
  rec.onend = () => { rec = null; $("micBtn").classList.remove("rec"); saveDraft(); };
  rec.onerror = (e) => toast("语音输入：" + e.error);
  rec.start(); $("micBtn").classList.add("rec");
}
function speak(text) {
  if (NATIVE) { NATIVE.postMessage({ op: "speak", text: String(text || "").slice(0, 600) }); return; }
  if (!("speechSynthesis" in window) || !text) return;
  speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(String(text).slice(0, 600)); u.lang = "zh-CN"; speechSynthesis.speak(u);
}

/* ---------------------------------------------------------------- agent control */
async function ctl(sid, op) { try { await api("POST", `/api/agent/${op}`, { session_id: sid }); if (op === "stop") toast("已停止，排队消息已暂停"); setTimeout(() => refreshThread(), 300); } catch (e) { toast(e.message); } }
function clearPend(sid, key) {
  for (const t of ((S.thread && S.thread.tasks) || [])) if (t.session_id === sid) { if (S.pend[t.id]) S.pend[t.id][key] = null; t["pending_" + key] = null; }
}
async function confirmAct(sid, ok, scope) {
  clearPend(sid, "confirm"); renderChat();
  await api("POST", "/api/agent/confirm", { session_id: sid, approve: ok, ...(scope ? { scope } : {}) });
  if (scope === "task") toast("这个任务里之后的确认都自动允许（付款、发布除外）");
  refreshThread();
}
S.forms = S.forms || {};
function openForm(sid) {
  const f = S.forms[sid]; if (!f) return;
  const saved = JSON.parse(sessionStorage.getItem("aiw_form_" + sid) || "{}");
  const input = (x) => {
    const v = esc(x.secret ? "" : saved[x.key] || ""), req = x.required ? "required" : "";
    if (x.type === "select") return `<select name="${esc(x.key)}" ${req}><option value="">请选择</option>${x.options.map((o) => `<option ${saved[x.key] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    if (x.type === "textarea") return `<textarea name="${esc(x.key)}" rows="3" ${req} placeholder="${esc(x.placeholder)}">${v}</textarea>`;
    const pw = x.type === "password" || x.secret;
    return `<div class="ff-in"><input name="${esc(x.key)}" type="${pw ? "password" : x.type}" value="${v}" ${req} placeholder="${esc(x.placeholder)}" autocomplete="${pw ? "new-password" : "off"}" />${pw ? `<button type="button" class="ff-eye" onclick="const i=this.previousElementSibling;i.type=i.type==='password'?'text':'password'">${icon("eye")}</button>` : ""}</div>`;
  };
  modal(`<form class="ffsheet" onsubmit="submitForm(event,'${sid}')">
    <div class="ff-h"><button type="button" class="icon ff-x" onclick="closeOverlays()">${icon("x")}</button><b>${esc(f.title)}</b>
      <button type="button" class="icon ff-pc" title="看桌面" onclick="closeOverlays();go('computer')">${icon("monitor")}</button><button class="icon ff-ok" title="提交">${icon("check")}</button></div>
    ${f.description ? `<p class="ff-d">${esc(f.description)}</p>` : ""}
    ${f.fields.map((x) => `<label class="ff"><span>${esc(x.label)}${x.required ? " *" : ""}</span>${input(x)}${x.hint ? `<small>${esc(x.hint)}</small>` : ""}</label>`).join("")}
    <p class="ff-d small">密码类字段只用于填入网页，不会显示在对话和记录里。</p>
    <button class="btn primary ff-sub">提交，继续注册</button></form>`);
  const first = document.querySelector(".ffsheet input,.ffsheet select"); if (first) first.focus();
}
async function submitForm(e, sid) {
  e.preventDefault();
  const f = S.forms[sid], el = e.target.elements, vals = {}, keep = {};
  for (const x of f.fields) { const v = (el[x.key] && el[x.key].value || "").trim(); vals[x.key] = v; if (!x.secret) keep[x.key] = v; }
  sessionStorage.setItem("aiw_form_" + sid, JSON.stringify(keep));
  closeOverlays(); delete S.forms[sid];
  await answerQ(sid, JSON.stringify(vals)); toast("已提交，Agent 正在填入网页");
}
async function takeoverDone(sid) {
  if (S.take) await toggleTakeover();
  await answerQ(sid, "done");
}
async function answerQ(sid, text) { clearPend(sid, "question"); await api("POST", "/api/agent/answer", { session_id: sid, text }); refreshThread(); }
function answerQForm(e, sid) { e.preventDefault(); const v = e.target.a.value.trim(); if (v) answerQ(sid, v); }
async function retry(id) { const r = await api("POST", `/api/tasks/${id}/retry`); S.threadId = r.thread_id; await refreshThread(true); }
/* 3.8.5 修改任务: change the task's instructions and re-run the SAME task (history, files, attachments kept) */
function taskEdit(id) {
  const root = (S.thread && S.thread.tasks || []).find((x) => x.id === id) || {};
  const rid = root.continues || id;
  const first = (S.thread && S.thread.tasks || []).find((x) => x.id === rid) || root;
  const cur = (S.thread && S.thread.instructions || {})[rid] || first.prompt || "";
  modal(`<div class="card"><div class="heading">修改任务</div>
    <div class="muted">改完后同一个任务接着做：做过的步骤、文件和附件都保留，只重做改动涉及的部分。</div>
    <textarea id="editPrompt" rows="7" style="width:100%;margin-top:10px">${esc(cur)}</textarea>
    <div class="row gap" style="justify-content:flex-end;margin-top:10px"><button class="btn sm" onclick="closeOverlays()">取消</button>
    <button class="btn sm primary" onclick="taskEditGo('${rid}')">改好了，重新做</button></div></div>`);
}
async function taskEditGo(id) {
  const p = ($("editPrompt").value || "").trim();
  if (!p) return toast("要求不能是空的");
  try { const r = await api("POST", `/api/tasks/${id}/edit`, { prompt: p }); closeOverlays(); toast("已修改，接着做"); S.threadId = r.thread_id || S.threadId; await refreshThread(true); }
  catch (e) { toast("没能修改：" + e.message); }
}
/* 3.8.5 补充要求 while the task runs: the agent reads it at its next step (or it answers the open question) */
function taskInstruct(id) {
  modal(`<div class="card"><div class="heading">补充要求</div>
    <div class="muted">任务不会重来，Aiw 下一步就会照着做。</div>
    <textarea id="instrText" rows="4" style="width:100%;margin-top:10px" placeholder="例如：顺便把 PDF 也转成 Word"></textarea>
    <div class="row gap" style="justify-content:flex-end;margin-top:10px"><button class="btn sm" onclick="closeOverlays()">取消</button>
    <button class="btn sm primary" onclick="taskInstructGo('${id}')">发送</button></div></div>`);
}
async function taskInstructGo(id) {
  const t = ($("instrText").value || "").trim();
  if (!t) return;
  try { const r = await api("POST", `/api/tasks/${id}/instruct`, { text: t }); closeOverlays(); toast(r.answered ? "已作为回答交给任务" : r.instructed ? "已交给正在做的任务" : "已接着做"); refreshThread(true); }
  catch (e) { toast("没能发送：" + e.message); }
}
async function cancelQueued(id) { await api("DELETE", `/api/tasks/${id}`); refreshThread(); }
async function sendQueuedNow(id) {
  try {
    const r = await api("POST", `/api/tasks/${id}/send_now`, {});
    if (!r.started && r.queue && r.queue.busy && r.queue.busy.task_id
        && confirm(`电脑正在做「${(r.queue.busy.prompt || "").slice(0, 30)}」。\n停止它，立即发送这条？\n（取消 = 等它做完，这条第一个发）`)) {
      await api("POST", `/api/tasks/${id}/send_now`, { stop_current: true });
    }
  } catch (e) { toast("没能发送：" + e.message); }
  refreshThread();
}
async function resumeQueue() { await api("POST", `/api/threads/${S.threadId}/queue/resume`); refreshThread(); }

async function replay(sid) {
  const r = await api("GET", `/api/sessions/${sid}/replay`);
  if (!r.frames.length) { toast("没有截图可回放"); return; }
  let i = 0;
  const show = () => {
    const f = r.frames[i];
    modal(`<div class="row gap"><div class="grow"><div class="heading">回放 · 第 ${f.step} 步（${i + 1}/${r.frames.length}）</div>
      <div class="small">${f.tool ? esc(toolName(f.tool)) + " " + esc(JSON.stringify(f.args || {}).slice(0, 160)) : ""}${f.ok === false ? " · 失败" : ""}</div></div>
      <button class="icon" onclick="closeOverlays()">${icon("x")}</button></div>
      <img class="shot" style="margin:12px 0" src="${artUrl(f.shot)}" alt="screenshot" />
      ${f.thought ? `<div class="muted" style="margin-bottom:10px">${esc(f.thought)}</div>` : ""}
      <div class="row gap"><button class="btn" id="rpPrev">上一步</button><input type="range" min="0" max="${r.frames.length - 1}" value="${i}" id="rpRange" class="grow" style="min-height:0;padding:0" /><button class="btn primary" id="rpNext">下一步</button></div>`);
    $("rpPrev").onclick = () => { if (i > 0) { i--; show(); } };
    $("rpNext").onclick = () => { if (i < r.frames.length - 1) { i++; show(); } };
    $("rpRange").oninput = (e) => { i = +e.target.value; show(); };
  };
  show();
}
async function receipts(tid) {
  const r = await api("GET", `/api/tasks/${tid}/receipts`);
  const rows = r.actions.map((a) => `<tr><td>${when(a.at)}</td><td><b>${esc(a.action)}</b><div class="small">${esc(JSON.stringify(a.payload || {}).slice(0, 220))}</div></td>
    <td>L${a.level ?? "?"}<div class="small">${esc(a.reason || "")}</div></td><td>${a.ok === true ? "✅" : a.ok === false ? "❌ " + esc(a.error || "") : "⛔ 未执行"}</td><td>${a.duration_ms ? Math.round(a.duration_ms) + "ms" : ""}</td></tr>`).join("");
  modal(`<div class="row gap"><div class="grow"><div class="heading">操作收据</div><div class="small">${esc(r.task.prompt).slice(0, 80)}</div></div><button class="btn sm" onclick="exportTask('${tid}')">导出</button><button class="btn sm" onclick="exportTask('${tid}', true)">复制</button><button class="icon" onclick="closeOverlays()">${icon("x")}</button></div>
    <div style="margin-top:12px">${rows ? `<table>${rows}</table>` : `<div class="empty">没有记录</div>`}</div>
    ${r.files.length ? `<div class="label" style="margin:14px 0 6px">交付的文件</div>` + r.files.map((f) => `<a class="filecard" href="${artUrl(f.path, true)}">${icon("file")}${esc(f.path.split("/").pop())}</a>`).join("") : ""}`);
}

async function exportTask(tid, copy) {
  try {
    const r = await api("GET", `/api/tasks/${tid}/export`);
    const text = JSON.stringify(r, null, 1);
    if (copy) {
      try { await navigator.clipboard.writeText(text); toast("已复制，可以直接粘贴给我看"); return; } catch (e) { /* insecure origin: fall back to download */ }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    a.download = `aiw-task-${tid.slice(0, 8)}.json`; document.body.appendChild(a); a.click(); a.remove();
    // Chrome reads the blob after a save dialog closes: revoking at 2 s gave "下载错误" (task-55a)
    setTimeout(() => URL.revokeObjectURL(a.href), 120000);
    toast(copy ? "浏览器不允许复制，已改为下载文件" : "已导出");
  } catch (e) { toast("导出失败：" + e.message); }
}

/* ---------------------------------------------------------------- activity */
async function loadActivity() {
  try { S.tasks = await api("GET", "/api/tasks?limit=100"); } catch (e) { return; }
  const need = S.tasks.filter((t) => ["waiting_confirm", "waiting_input"].includes(t.status)).length;
  $("needBadge").textContent = need || "";
  const f = S.actFilter;
  const list = S.tasks.filter((t) => f === "all" || (f === "live" && LIVE.includes(t.status)) || (f === "need" && ["waiting_confirm", "waiting_input"].includes(t.status))
    || (f === "failed" && ["failed", "stopped", "interrupted"].includes(t.status)) || (f === "queued" && t.status === "queued"));
  $("actList").innerHTML = list.map((t) => {
    const p = t.plan && t.plan.steps ? `${(t.plan.done || []).length}/${t.plan.steps.length} 步` : "";
    return `<div class="item"><div class="iconbox sm">${icon(t.source === "routine" ? "clock" : "bolt")}</div><div class="grow" style="cursor:pointer" onclick="openThread('${t.thread_id}')">
      <div><b>${esc(t.prompt.slice(0, 90))}</b></div><div class="small">${esc(t.thread_title || "")} · ${when(t.created_at)} ${p ? "· " + p : ""}</div>
      ${t.result ? `<div class="muted" style="margin-top:4px">${esc(t.result.slice(0, 160))}</div>` : ""}</div>
      <div class="acts">${chip(t.status)}${["failed", "stopped", "interrupted"].includes(t.status) ? `<button class="btn sm" onclick="retry('${t.id}').then(()=>go('chat'))">重试</button>` : ""}
      ${t.status === "queued" ? `<button class="btn sm" onclick="cancelQueued('${t.id}').then(loadActivity)">取消</button>` : ""}</div></div>`;
  }).join("") || `<div class="empty">这里会显示所有任务的进度、需要你处理的请求和失败记录</div>`;
}
$("actFilter").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; S.actFilter = b.dataset.f;
  $("actFilter").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); loadActivity(); });

/* ---------------------------------------------------------------- tracking */
const MODE_CN = { change: "页面变化", contains: "出现文字", price_below: "价格低于" };
async function loadWatches() {
  const ws = await api("GET", "/api/watches");
  $("watchList").innerHTML = ws.map((w) => `<div class="item"><div class="iconbox sm">${icon("target")}</div><div class="grow">
    <div><b>${esc(w.name)}</b> <span class="chip">${MODE_CN[w.mode]}${w.value ? " " + esc(w.value) : ""}</span> ${w.enabled ? "" : '<span class="chip">已暂停</span>'}</div>
    <div class="small"><a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.url.slice(0, 70))}</a></div>
    <div class="small">每 ${w.interval_min} 分钟 · 上次 ${when(w.last_checked)}${w.mode === "price_below" && w.last_value ? " · 当前 " + esc(w.last_value) : ""}${w.triggered_at ? " · 提醒于 " + when(w.triggered_at) : ""}</div>
    ${w.last_error ? `<div class="small" style="color:var(--danger)">${esc(w.last_error)}（已失败 ${w.fail_count} 次，自动退避）</div>` : ""}</div>
    <div class="acts"><button class="btn sm" onclick="checkWatch('${w.id}')">立即检查</button>
    <button class="btn sm" onclick="toggleWatch(${esc(JSON.stringify(w))})">${w.enabled ? "暂停" : "启用"}</button>
    <button class="btn sm danger" onclick="delRow('watches','${w.id}',loadWatches)">删除</button></div></div>`).join("") || `<div class="empty">还没有追踪</div>`;
}
async function addWatch(e) {
  e.preventDefault(); const f = e.target;
  try { await api("POST", "/api/watches", { name: f.name.value, url: f.url.value, mode: f.mode.value, value: f.value.value, interval_min: +f.interval_min.value });
    f.reset(); toast("已添加，首次检查会建立基线"); loadWatches(); } catch (err) { toast(err.message); }
}
async function checkWatch(id) { const r = await api("POST", `/api/watches/${id}/check`); toast(r.ok ? (r.triggered ? "触发提醒！" : "检查完成，没有新变化") : "失败：" + (r.note || r.error || r.status)); loadWatches(); }
async function toggleWatch(w) { await api("PUT", `/api/watches/${w.id}`, { ...w, enabled: !w.enabled }); loadWatches(); }
async function delRow(kind, id, after) { if (!confirm("确定删除？")) return; await api("DELETE", `/api/${kind}/${id}`); after(); }

/* ---------------------------------------------------------------- routines */
async function loadRoutines() {
  const rs = await api("GET", "/api/routines");
  $("routineList").innerHTML = rs.map((r) => `<div class="item"><div class="iconbox sm">${icon("clock")}</div><div class="grow">
    <div><b>${esc(r.name)}</b> <span class="chip">${esc(r.schedule)}</span> ${r.enabled ? "" : '<span class="chip">已暂停</span>'}</div>
    <div class="muted">${esc(r.prompt)}</div><div class="small">下次 ${when(r.next_run)} · 上次 ${when(r.last_run)}</div></div>
    <div class="acts"><button class="btn sm primary" onclick="runRoutine('${r.id}')">现在运行</button>
    ${r.thread_id ? `<button class="btn sm" onclick="openThread('${r.thread_id}')">结果</button>` : ""}
    <button class="btn sm" onclick="toggleRoutine(${esc(JSON.stringify(r))})">${r.enabled ? "暂停" : "启用"}</button>
    <button class="btn sm danger" onclick="delRow('routines','${r.id}',loadRoutines)">删除</button></div></div>`).join("") || `<div class="empty">还没有例程</div>`;
}
async function addRoutine(e) {
  e.preventDefault(); const f = e.target;
  try { await api("POST", "/api/routines", { name: f.name.value, prompt: f.prompt.value, schedule: f.schedule.value, machine_id: MID }); f.reset(); toast("例程已添加"); loadRoutines(); }
  catch (err) { toast(err.message); }
}
async function runRoutine(id) { const r = await api("POST", `/api/routines/${id}/run`); openThread(r.thread_id); }
async function toggleRoutine(r) { await api("PUT", `/api/routines/${r.id}`, { name: r.name, prompt: r.prompt, schedule: r.schedule, machine_id: r.machine_id, enabled: !r.enabled }); loadRoutines(); }

/* ---------------------------------------------------------------- computer */
let deskLoaded = { small: null, big: null };
async function loadDesktop() {
  const viewOnly = !S.take;
  try {
    const d = await api("GET", `/api/machines/${MID}/desktop?view_only=${viewOnly}`);
    const target = S.view === "computer" ? "big" : "small";
    const el = target === "big" ? $("deskBig") : $("desk");
    if (!d.novnc_url) {  // a paused cloud computer: nothing to show until a task or 开机 wakes it
      el.src = "about:blank"; deskLoaded[target] = null;
      $("deskMode").textContent = "云电脑已自动暂停 · 发任务或点开机会自动唤醒"; return;
    }
    const url = d.external ? d.novnc_url : d.novnc_url + (TOKEN ? "&token=" + encodeURIComponent(TOKEN) : "");
    if (deskLoaded[target] !== url) { el.src = url; deskLoaded[target] = url; }
    if (target === "big") { $("desk").src = "about:blank"; deskLoaded.small = null; } else if (S.view !== "computer") { $("deskBig").src = "about:blank"; deskLoaded.big = null; }
  } catch (e) { toast("桌面：" + e.message); }
  $("deskMode").textContent = S.take ? "你正在操作桌面 · 鼠标键盘已接通" : "观看模式 · 只看不操作";
  deskTouch();
}
function liveTask() { return busyTask || (S.thread && S.thread.tasks.find((t) => LIVE.includes(t.status))); }
/* 3.7: on a touch screen, taking over puts the touch layer (takeover.js: 触控板 / 直点, pinch zoom, toolbar, 横屏)
   over the desktop iframe that is on screen; handing back removes it. A mouse keeps driving noVNC directly. */
const deskTouchCtl = {};
function deskTouch() {
  const touch = window.AiwTouch && (matchMedia("(pointer: coarse)").matches || "ontouchstart" in window);
  ["desk", "deskBig"].forEach((id) => {
    const f = $(id), on = !!(touch && S.take && f && f.offsetParent && f.src && f.src !== "about:blank");
    // 3.8.4: mid + token: touches and keys go straight to the VM (/ws/input), not into the viewer page
    if (on && deskTouchCtl[id] && deskTouchCtl[id].mid !== MID) { deskTouchCtl[id].detach(); delete deskTouchCtl[id]; }
    if (on && !deskTouchCtl[id]) { deskTouchCtl[id] = AiwTouch.attach(f.parentElement, f, { mid: MID, token: TOKEN }); deskTouchCtl[id].mid = MID; }
    else if (!on && deskTouchCtl[id]) { deskTouchCtl[id].detach(); delete deskTouchCtl[id]; }
  });
}
function setTake(on, quiet) {
  const was = !!S.take; S.take = on;
  const live = liveTask();
  const back = live ? "交还给 Agent" : "结束操作";
  $("takeBtn").innerHTML = icon("hand") + (on ? back : "接管桌面");
  $("takeBtn").classList.toggle("danger", on);
  $("takeBtn2").textContent = on ? (live ? "交还" : "结束操作") : "接管";
  $("takeBtn2").classList.toggle("primary", on);
  document.querySelectorAll(".deskhead, .deskframe").forEach((el) => el.classList.toggle("taking", on));
  document.querySelectorAll(".takebanner").forEach((el) => el.remove());
  if (on) document.querySelectorAll(".deskframe").forEach((el) => {
    const b = document.createElement("div"); b.className = "takebanner";
    b.innerHTML = `<span class="dot"></span>你正在操作桌面${live ? "，Agent 已暂停" : ""}<button type="button" onclick="toggleTakeover()">${back}</button>`;
    el.appendChild(b);
  });
  if (was !== on) { deskLoaded = { small: null, big: null }; loadDesktop(); }
  deskTouch();
  if (!quiet && was !== on) toast(on ? (live ? "已接管：Agent 已暂停，你可以直接操作桌面" : "已接管：你可以直接操作桌面") : (live ? "已交还给 Agent，回到观看模式" : "已回到观看模式"));
}
async function toggleTakeover() {
  const live = liveTask(); const want = !S.take;
  const btns = [$("takeBtn"), $("takeBtn2")]; btns.forEach((b) => (b.disabled = true));
  $("deskMode").textContent = want ? "正在接管…" : "正在交还…";
  try {
    if (live && live.session_id) await api("POST", `/api/agent/${want ? "takeover" : "resume"}`, { session_id: live.session_id });
    setTake(want);
    if (live) setTimeout(() => refreshThread(), 300);
  } catch (e) { toast((want ? "接管失败：" : "交还失败：") + e.message); $("deskMode").textContent = S.take ? "你正在操作桌面" : "观看模式"; }
  finally { btns.forEach((b) => (b.disabled = false)); }
}
/* 3.9.10 clipboard: the viewer (noVNC / InstaBox) has no paste, so text goes to the VM's clipboard through the API
   (xclip there) and Ctrl+V (Ctrl+Shift+V in a terminal) is pressed in the window that has the focus. */
const PASTE_KEYS = [["ctrl+v", "Ctrl+V（浏览器、文档、大多数程序）"], ["ctrl+shift+v", "Ctrl+Shift+V（终端）"], ["", "只放进电脑剪贴板，不按键"]];
async function sendPaste(text, key) {
  if (!text) return;
  if (!S.take) { toast("先点「接管」再粘贴：粘贴会在电脑里按一次 " + (key || "Ctrl+V")); return; }
  try { await api("POST", `/api/machines/${MID}/input`, { ops: [{ t: "paste", text, key }] });
    toast(key ? `已粘贴 ${text.length} 个字符到电脑` : "已放进电脑剪贴板"); }
  catch (e) { toast("粘贴失败：" + e.message); }
}
async function deskPaste() {
  const k = localStorage.getItem("aiw_paste_key") ?? "ctrl+v";
  modal(`<div class="card"><div class="heading">粘贴到电脑</div>
    <div class="muted">先在电脑里点好要粘贴的位置。在下面按 ⌘V / Ctrl+V 贴入文字，再点发送。</div>
    <textarea id="pasteText" rows="6" style="width:100%;margin-top:10px" placeholder="在这里粘贴"></textarea>
    <div class="row gap wrap" style="justify-content:space-between;margin-top:10px">
      <select id="pasteKey">${PASTE_KEYS.map(([v, l]) => `<option value="${v}" ${v === k ? "selected" : ""}>${l}</option>`).join("")}</select>
      <span class="row gap"><button class="btn sm" onclick="closeOverlays()">取消</button>
      <button class="btn sm primary" onclick="deskPasteGo()">发送</button></span></div></div>`);
  const ta = $("pasteText"); ta.focus();
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); deskPasteGo(); } });
  try { if (navigator.clipboard && navigator.clipboard.readText) { const t = await navigator.clipboard.readText(); if (t && !ta.value) { ta.value = t; ta.select(); } } }
  catch (e) { /* no permission / not https: the user pastes into the box */ }
}
async function deskPasteGo() {
  const t = $("pasteText").value, key = $("pasteKey").value;
  if (!t) { toast("还没有内容"); return; }
  localStorage.setItem("aiw_paste_key", key); closeOverlays(); await sendPaste(t, key);
}
window.aiwPasteToVM = deskPaste;
async function deskCopyBack() {
  let t = "";
  try { t = (await api("GET", `/api/machines/${MID}/clipboard`)).text || ""; } catch (e) { toast("读不到电脑剪贴板：" + e.message); return; }
  if (!t) { toast("电脑剪贴板是空的：先在电脑里选中文字按 Ctrl+C"); return; }
  try { await navigator.clipboard.writeText(t); toast(`已复制 ${t.length} 个字符到本机`); return; } catch (e) { /* not https: show it */ }
  modal(`<div class="card"><div class="heading">电脑剪贴板</div><div class="muted">已选中，按 ⌘C / Ctrl+C 复制。</div>
    <textarea id="clipBack" rows="6" style="width:100%;margin-top:10px" readonly></textarea>
    <div class="row gap" style="justify-content:flex-end;margin-top:10px"><button class="btn sm" onclick="closeOverlays()">关闭</button></div></div>`);
  const ta = $("clipBack"); ta.value = t; ta.focus(); ta.select();
}
// ⌘V / Ctrl+V on the AIW page itself (not inside the desktop frame, not in a text box) while taking over: straight in
document.addEventListener("paste", (e) => {
  const tg = e.target, typing = tg && (tg.closest && tg.closest("input,textarea,[contenteditable=true],#modal"));
  if (!S.take || typing) return;
  const t = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
  if (!t) return;
  e.preventDefault(); sendPaste(t, localStorage.getItem("aiw_paste_key") ?? "ctrl+v");
});
async function vmOp(op) { if (op !== "start" && !confirm("确定" + { stop: "关机", restart: "重启" }[op] + "？")) return; try { await api("POST", `/api/machines/${MID}/${op}`); toast("已发送"); refreshMachine(); } catch (e) { toast(e.message); } }
async function snapshot() { const n = prompt("快照名称（字母开头，字母数字-_）", "snap" + new Date().toISOString().slice(0, 10).replace(/-/g, "")); if (!n) return; try { await api("POST", `/api/machines/${MID}/snapshot`, { name: n }); toast("快照已创建"); } catch (e) { toast(e.message); } }
async function refreshMachine() {
  try {
    const m = await api("GET", `/api/machines/${MID}`);
    const up = !!(m.agent && m.agent.ok);
    S.agentFails = up ? 0 : S.agentFails + 1;
    if (up || S.agentFails >= 2) { const p = $("pillAgent"); p.textContent = up ? "Agent 在线" : "Agent 离线"; p.className = "chip " + (up ? "ok" : "bad"); p.title = up ? "" : (m.agent && m.agent.error) || ""; }
    const vm = m.vm ? ({ running: "运行中", paused: "已暂停", suspended: "已暂停", stopped: "已关机" }[m.vm.state] || m.vm.state) : m.vm_error ? "未连接 PVE" : m.status;
    if (m.agent && m.agent.paused) { const p = $("pillAgent"); p.textContent = "云电脑暂停中"; p.className = "chip"; p.title = m.agent.error; }
    $("pcTitle").textContent = (up ? "电脑已连接" : "电脑未连接") + " · " + (m.name || MID);
    $("pcSub").textContent = `VM ${vm}${m.vm && m.vm.ip ? " · " + m.vm.ip : ""}${m.vm && m.vm.mem_bytes ? " · " + Math.round(m.vm.mem_bytes / 1073741824) + " GB" : ""} · 浏览器、终端、文件和完整桌面`;
  } catch (e) { $("pillAgent").textContent = "API 错误"; $("pillAgent").className = "chip bad"; }
}

/* ---------------------------------------------------------------- machine picker */
async function loadMachines() {
  let ms = [];
  try { ms = await api("GET", "/api/machines"); } catch (e) { return; }
  if (!ms.some((m) => m.id === MID) && ms.length) { MID = ms[0].id; localStorage.setItem("aiw_machine", MID); }
  S.machines = ms;   // display names (云电脑 N or the user's own) for toasts and cards
  const opts = ms.map((m) => `<option value="${esc(m.id)}" ${m.id === MID ? "selected" : ""}>${esc(m.name || m.id)}${m.provider === "instabox" && !/云/.test(m.name || "") ? "（云）" : ""}</option>`).join("");
  // one picker on the 电脑 page and one on the chat's desktop pane, always shown so it is easy to find
  for (const [id, anchor] of [["machineSel", "pcTitle"], ["machineSel2", "deskTitle"]]) {
    let sel = $(id); const a = $(anchor);
    if (!a) continue;
    if (!sel) {
      sel = document.createElement("select"); sel.id = id; sel.className = "machinesel"; sel.title = "选择电脑";
      sel.onchange = () => pickMachine(sel.value);
      a.parentElement.appendChild(sel);
    }
    sel.innerHTML = opts; sel.disabled = ms.length < 2;
  }
}
function pickMachine(id) {
  MID = id; localStorage.setItem("aiw_machine", id);
  deskLoaded = { small: null, big: null }; S.agentFails = 0;
  for (const sid of ["machineSel", "machineSel2"]) { const s = $(sid); if (s) s.value = id; }
  loadDesktop(); refreshMachine(); toast("已切换到 " + (((S.machines || []).find((m) => m.id === id) || {}).name || id));
}

/* ---------------------------------------------------------------- cloud computers */
const CLOUD_ST = { adding: "添加中", creating: "创建机器中", installing: "安装中", ready: "可用", error: "出错" };
function cloudCard(list) {
  const rows = list.map((c) => `<div class="cloudrow"><div class="grow"><b>${esc(c.name)}</b> <span class="chip ${c.status === "ready" ? "ok" : c.status === "error" ? "bad" : ""}">${CLOUD_ST[c.status] || esc(c.status)}</span>
      <div class="small muted">${esc(c.id)} · InstaBox ${esc(c.remote_id || "")}</div>
      <div class="small muted">${esc((c.log || []).slice(-1)[0] || c.error || "")}</div></div>
      <button type="button" class="btn sm" onclick="pickMachine('${esc(c.id)}');go('chat')">使用</button>
      <button type="button" class="btn sm" onclick="cloudSetup('${esc(c.id)}')">重新安装</button>
      <button type="button" class="btn sm danger" onclick="cloudRemove('${esc(c.id)}')">移除</button></div>`).join("");
  return `<form class="card sec" id="sCloud" onsubmit="cloudAdd(event)"><h3>${icon("plug")} 云电脑</h3>
    <div class="muted">添加 InstaBox 云电脑（免费 1 台 4 核 8G）。添加后会自动装好 LibreCAD、中文字体和 vm-agent，聊天里切换电脑即可使用。空闲 30 分钟会自动暂停，发任务时自动唤醒，任务运行期间不会暂停。</div>
    <div id="cloudList">${rows || '<div class="small muted">还没有云电脑</div>'}</div>
    <div class="kv"><span class="muted">名称</span><input name="name" placeholder="instabox" value="instabox" />
    <span class="muted">API Key</span><input name="api_key" type="password" autocomplete="off" placeholder="ibk_…（添加后加密保存在服务器，不再显示）" />
    <span class="muted">机器</span><select name="remote_id"><option value="">新建一台</option></select></div>
    <div class="row gap wrap"><button type="button" class="btn" onclick="cloudLookup()">读取已有机器</button><button class="btn primary">添加</button><span id="cloudNote" class="small"></span></div></form>`;
}
async function cloudRefresh() {
  let list = [];
  try { list = await api("GET", "/api/cloud"); } catch (e) { return; }
  const box = $("cloudList"); if (!box) return;
  const tmp = document.createElement("div"); tmp.innerHTML = cloudCard(list);
  box.innerHTML = tmp.querySelector("#cloudList").innerHTML;
  clearTimeout(cloudRefresh._t);
  if (list.some((c) => !["ready", "error"].includes(c.status))) cloudRefresh._t = setTimeout(cloudRefresh, 4000);
  else loadMachines();
}
async function cloudLookup() {
  const f = $("sCloud"); const key = f.api_key.value.trim();
  if (!key) return toast("先填 API Key");
  $("cloudNote").textContent = "读取中…";
  try {
    const r = await api("POST", "/api/cloud/instabox/machines", { api_key: key });
    f.remote_id.innerHTML = `<option value="">新建一台</option>` + r.machines.map((m) => `<option value="${esc(m.id)}" ${m.in_use ? "disabled" : ""}>${esc(m.name)} · ${esc(m.state)} · ${m.spec ? m.spec.vcpus + "核 " + Math.round(m.spec.memMiB / 1024) + "G" : ""}${m.in_use ? "（已添加）" : ""}</option>`).join("");
    const free = r.machines.find((m) => !m.in_use); if (free) f.remote_id.value = free.id;
    $("cloudNote").textContent = `账号 ${r.account}：${r.machines.length} 台机器。选好机器后点「添加」才会保存`;
  } catch (e) { $("cloudNote").textContent = "读取失败：" + e.message; }
}
async function cloudAdd(e) {
  e.preventDefault(); const f = e.target; const btn = f.querySelector("button.primary");
  if (!f.api_key.value.trim()) { $("cloudNote").textContent = "先填 API Key"; return; }
  btn.disabled = true; $("cloudNote").textContent = "添加中…";
  try {
    const r = await api("POST", "/api/cloud", { provider: "instabox", name: f.name.value.trim(), api_key: f.api_key.value.trim(), remote_id: f.remote_id.value });
    f.api_key.value = "";
    $("cloudNote").textContent = `已保存为「${r.name}」，正在安装（约 3-6 分钟），完成后点该行的「使用」`;
    toast("已添加，正在安装"); cloudRefresh(); loadMachines();
  } catch (err) { $("cloudNote").textContent = "添加失败：" + err.message; toast("添加失败：" + err.message); }
  finally { btn.disabled = false; }
}
async function cloudSetup(id) { try { await api("POST", `/api/cloud/${id}/setup`); toast("开始重新安装"); setTimeout(cloudRefresh, 800); } catch (e) { toast(e.message); } }
async function cloudRemove(id) {
  if (!confirm("从 AIW 移除这台云电脑？InstaBox 上的机器本身会保留。")) return;
  try { await api("DELETE", `/api/cloud/${id}`); if (MID === id) pickMachine("ai-linux-001"); cloudRefresh(); loadMachines(); } catch (e) { toast(e.message); }
}

/* ---------------------------------------------------------------- notifications */
async function loadNotes() {
  try { S.notes = await api("GET", "/api/notifications"); } catch (e) { return; }
  $("bellDot").classList.toggle("on", S.notes.some((n) => !n.read));
  $("notes").innerHTML = `<div class="row gap pad"><div class="heading grow">通知</div><button class="btn sm" onclick="readAll()">全部已读</button></div>`
    + (S.notes.map((n) => `<div class="note ${n.read ? "" : "unread"}" onclick="openNote('${n.id}')"><div class="iconbox sm">${icon({ done: "check", failed: "x", watch: "target", watch_error: "target", input: "chat", confirm: "hand" }[n.kind] || "bell")}</div>
      <div class="grow"><div><b>${esc(n.title)}</b></div><div class="small">${esc((n.body || "").slice(0, 120))}</div><div class="small">${ago(n.created_at)}</div></div></div>`).join("") || `<div class="empty">没有通知</div>`);
}
function toggleNotes() { const on = !$("notes").classList.contains("on"); closeOverlays(); if (on) { loadNotes(); $("notes").classList.add("on"); } }
async function readAll() { await api("POST", "/api/notifications/read", {}); loadNotes(); }
async function openNote(id) {
  const n = S.notes.find((x) => x.id === id); await api("POST", "/api/notifications/read", { ids: [id] });
  if (n.thread_id) openThread(n.thread_id); else if (n.link) window.open(n.link, "_blank"); else loadNotes();
}
function connectEvents() {
  const ws = new WebSocket(wsUrl("/ws/events"));
  ws.onopen = () => { if (window.onAiwEventsOpen) window.onAiwEventsOpen(); };
  ws.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (window.onAiwEvent) { try { window.onAiwEvent(e); } catch (err) { console.warn(err); } }   // home.js: the persistent chat
    if (e.kind === "notification") { $("bellDot").classList.add("on"); toast(e.notification.title + (e.notification.body ? "：" + e.notification.body.slice(0, 60) : ""));
      if (NATIVE) NATIVE.postMessage({ op: "notify", title: e.notification.title, body: e.notification.body || "" });
      else if (document.hidden && "Notification" in window && Notification.permission === "granted") new Notification(e.notification.title, { body: e.notification.body || "" }); }
    if ((e.kind === "task" || e.type === "task.started") && (e.thread_id || (e.task && e.task.thread_id)) === S.threadId) refreshThread();
    if (e.kind === "task" || e.type === "task.started") { loadThreads(); if (S.view === "activity") loadActivity(); }
  };
  ws.onclose = () => setTimeout(connectEvents, 3000);
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
}

/* ---------------------------------------------------------------- settings ("Apps") */
async function loadSettings() {
  const cloudList = await api("GET", "/api/cloud").catch(() => []);
  if (cloudList.some((c) => !["ready", "error"].includes(c.status))) setTimeout(cloudRefresh, 3000);
  const [prof, mems, models, notify, integ, secs, vmnet] = await Promise.all([api("GET", "/api/profile"), api("GET", "/api/memories"), api("GET", "/api/settings/models"),
    api("GET", "/api/settings/notify"), api("GET", "/api/integrations"), api("GET", "/api/secrets"), api("GET", "/api/settings/vm").catch(() => ({}))]);
  S.profile = prof;
  const av = (k) => `<button type="button" class="avatar" style="background:${AVATAR_BG[k]};${(prof.avatar || "amber") === k || ({sky:"amber",sand:"cream",lilac:"peach"})[prof.avatar] === k ? "outline:2px solid var(--blueDark)" : ""}" onclick="this.form.avatar.value='${k}';[...this.parentNode.children].forEach(b=>b.style.outline='');this.style.outline='2px solid var(--blueDark)'">${avatarSVG()}</button>`;
  $("settingsPage").innerHTML = `
  <div class="apps">
    <button style="background:var(--lavender)" onclick="go('routines')">${icon("clock")}例程</button>
    <button style="background:var(--sky)" onclick="document.getElementById('sMem').scrollIntoView({behavior:'smooth'})">${icon("brain")}记忆</button>
    <button style="background:var(--green)" onclick="document.getElementById('sModel').scrollIntoView({behavior:'smooth'})">${icon("bolt")}模型</button>
    <button style="background:var(--orange)" onclick="document.getElementById('sNotify').scrollIntoView({behavior:'smooth'})">${icon("bell")}通知</button>
    <button style="background:var(--sand)" onclick="document.getElementById('sInteg').scrollIntoView({behavior:'smooth'})">${icon("plug")}集成</button>
  </div>
  <form class="card sec" onsubmit="saveProfile(event)"><h3>${icon("user")} 个人资料</h3>
    <div class="kv"><span class="muted">你的称呼</span><input name="name" value="${esc(prof.name || "")}" placeholder="例如 Jown" />
    <span class="muted">Agent 名字</span><input name="agent_name" value="${esc(prof.agent_name || "")}" placeholder="例如 小A" />
    <span class="muted">语气</span><input name="tone" value="${esc(prof.tone || "")}" placeholder="例如 简洁、用中文" />
    <span class="muted">关于你</span><textarea name="about" rows="2" placeholder="工作、常用网站、文件放哪…">${esc(prof.about || "")}</textarea>
    <span class="muted">头像底色</span><div class="row gap">${av("amber")}${av("cream")}${av("peach")}</div></div>
    <input type="hidden" name="avatar" value="${esc(prof.avatar || "sky")}" />
    <label class="toggle"><input type="checkbox" ${localStorage.getItem("aiw_tts") === "1" ? "checked" : ""} onchange="localStorage.setItem('aiw_tts', this.checked?'1':'')" /> 任务完成时朗读结果</label>
    <div><button class="btn primary">保存</button></div></form>

  <div class="card sec" id="sMem"><h3>${icon("brain")} 记忆</h3><div class="muted">Agent 每次任务都会参考这些事实。Agent 也会在你说出长期偏好时自己记下。</div>
    <form class="row gap" onsubmit="addMem(event)"><input name="t" placeholder="例如：周报放在 ~/workspace/reports，用 Word 格式" /><button class="btn primary">添加</button></form>
    <div class="list">${mems.map((m) => `<div class="item"><div class="grow">${esc(m.text)}<div class="small">${m.source === "user" ? "你添加" : "Agent 记下"} · ${ago(m.created_at)}</div></div>
      <div class="acts"><button class="btn sm" onclick="editMem(${esc(JSON.stringify(m))})">编辑</button><button class="btn sm danger" onclick="delRow('memories','${m.id}',loadSettings)">删除</button></div></div>`).join("") || `<div class="small">还没有记忆</div>`}</div></div>

  <form class="card sec" id="sModel" onsubmit="saveModels(event)"><h3>${icon("bolt")} 模型</h3>
    <div class="muted">从网关读取模型列表后用下拉选择，点"探测"会实测这个模型能不能稳定调用工具、能不能看图。主模型负责决定下一步操作，需要能调用工具；如果它看不了图，再选一个视觉模型，由它把屏幕描述给主模型。留空则用 .env（当前 ${esc(models.env_model || "未设置")}）。</div>
    <div class="kv">
      <span class="muted">网关地址</span><input name="base_url" value="${esc(models.base_url || "")}" placeholder="${esc(models.env_base_url || "https://api.openai.com/v1")}" />
      <span class="muted">API Key</span><input name="key" type="password" placeholder="${models.has_key ? "已设置（加密保存），留空不修改" : "sk-…"}" autocomplete="off" />
      ${MODEL_ROLES.map((r) => `<span class="muted">${r.label}<div class="small">${r.hint}</div></span>
        <div class="mrow"><select name="${r.key}" data-cur="${esc(models[r.key] || "")}" onchange="paintProbe(this)"></select>
        <button type="button" class="btn sm" onclick="probeModel(this)">探测</button><div class="probe small"></div></div>`).join("")}
      <span class="muted">深度思考<div class="small">关掉可快很多</div></span>
      <select name="thinking"><option value="off" ${(models.thinking || "off") === "off" ? "selected" : ""}>关闭（推荐，更快）</option><option value="on" ${models.thinking === "on" ? "selected" : ""}>模型默认</option></select>
    </div>
    <div class="row gap wrap"><button class="btn primary">保存</button><button type="button" class="btn" onclick="fetchModels(true)">刷新模型列表</button><span id="modelListNote" class="small"></span></div></form>

  ${cloudCard(cloudList)}

  <form class="card sec" id="sVmNet" onsubmit="saveVmNet(event)"><h3>${icon("plug")} 虚拟机地址</h3>
    <div class="muted">AI 电脑（VM）的 IP 变了就在这里改，保存立即生效，不用改 .env、不用重启。留空则用 .env 里的 AIW_VM_IP${vmnet.env_ip ? "（" + esc(vmnet.env_ip) + "）" : ""}。</div>
    <div class="kv"><span class="muted">IP / 主机名</span><input name="ip" value="${esc(vmnet.ip || "")}" placeholder="${esc(vmnet.env_ip || "192.168.50.x")}" />
    <span class="muted">自动跟随<div class="small">PVE 报告的新 IP</div></span><label><input type="checkbox" name="auto" ${vmnet.auto ? "checked" : ""} ${vmnet.pve ? "" : "disabled"} /> 自动更新${vmnet.pve ? "" : "（未配置 PVE）"}</label></div>
    <div class="small muted">当前连接：${esc(vmnet.agent_endpoint || "-")}</div>
    <div class="row gap wrap"><button class="btn primary">保存</button>${vmnet.pve ? `<button type="button" class="btn" onclick="detectVmIp()">从 PVE 检测</button>` : ""}</div></form>

  <form class="card sec" id="sNotify" onsubmit="saveNotify(event)"><h3>${icon("bell")} 手机推送</h3>
    <div class="muted">任务完成、失败、需要确认或回答、追踪触发时推送。iPhone 推荐 Bark。</div>
    <div class="kv"><span class="muted">Bark 地址</span><input name="bark_url" value="${esc(notify.bark_url || "")}" placeholder="https://api.day.app/你的key" />
    <span class="muted">ntfy 主题</span><input name="ntfy_url" value="${esc(notify.ntfy_url || "")}" placeholder="https://ntfy.sh/aiw-xxxx" />
    <span class="muted">Webhook</span><input name="webhook_url" value="${esc(notify.webhook_url || "")}" placeholder="POST {title, body}" />
    <span class="muted">Bark 铃声</span><select name="bark_sound"><option value="builtin"${!["voice", "off"].includes(notify.bark_sound) ? " selected" : ""}>Bark 内置铃声（锁屏也响，推荐）</option><option value="voice"${notify.bark_sound === "voice" ? " selected" : ""}>女声语音（只在解锁时响）</option><option value="off"${notify.bark_sound === "off" ? " selected" : ""}>Bark 默认铃声</option></select>
    <span class="muted">需要你确认时</span><select name="bark_critical"><option value=""${notify.bark_critical === "on" ? "" : " selected"}>时效性通知</option><option value="on"${notify.bark_critical === "on" ? " selected" : ""}>重要警告（静音也响）</option></select></div>
    <details class="muted" style="margin:6px 0"><summary>女声铃声：第一次用要先在 Bark 里上传（点开看步骤）</summary>
      <div style="margin-top:6px">在 iPhone 上点下面每个文件，存到「文件」App；再打开 Bark › 设置 › 铃声 › 上传铃声，选这些 .caf 文件。上传后点「测试全部铃声」逐个听。</div>
      <div class="row gap wrap" style="margin-top:6px">${[["aiw-shouquan","请授权"],["aiw-tianxie","请填写资料"],["aiw-jieguan","请接管桌面"],["aiw-kazhu","任务卡住了"],["aiw-huida","有问题需要你回答"],["aiw-wancheng","任务完成"],["aiw-shibai","任务失败了"],["aiw-jiangjia","价格提醒"],["aiw-jiankong","监控出错了"],["aiw-xiaohongshu","小红书需要你处理"],["aiw-tixing","小秘提醒"]].map(([f, t]) => `<a class="btn xs" href="sounds/${f}.caf" download>${t}</a>`).join("")}</div></details>
    <div class="row gap wrap"><button class="btn primary">保存</button><button type="button" class="btn" onclick="api('POST','/api/settings/notify/test').then(()=>toast('已发送测试通知'))">发送测试</button>
    <button type="button" class="btn" onclick="api('POST','/api/settings/notify/test_sounds').then(r=>toast('正在逐个推送 '+r.sent+' 种铃声，每 3 秒一条'))">测试全部铃声</button>
    <button type="button" class="btn" onclick="Notification.requestPermission().then(p=>toast('浏览器通知：'+p))">允许浏览器通知</button></div></form>

  <div class="card sec" id="sInteg"><h3>${icon("plug")} 集成</h3>
    <div class="muted">让 Agent 直接调 API，而不是点网页。MCP 服务器（Streamable HTTP）、OpenAPI 规范或自定义 HTTP 工具。请求头里可以用 {{secret:名字}} 引用下方加密保存的密钥。</div>
    <div class="label">推荐 MCP（一键添加）</div>
    <div class="list">${MCP_PRESETS.map((p) => { const on = (integ.mcp_servers || []).some((m) => m.name === p.name);
      return `<div class="item"><div class="grow"><b>${esc(p.title)}</b>${p.secret ? ' <span class="chip">需 Key</span>' : ' <span class="chip ok">免费</span>'}<div class="small">${esc(p.desc)}</div></div>
      <div class="acts">${on ? '<span class="chip ok">已添加</span>' : `<button type="button" class="btn sm primary" onclick="addPreset('${p.name}')">添加</button>`}</div></div>`; }).join("")}</div>
    <div class="label">MCP 服务器</div>
    <div class="list">${(integ.mcp_servers || []).map((m, i) => `<div class="item"><div class="grow"><b>${esc(m.name)}</b><div class="small">${esc(m.url)}</div><div class="small muted" id="mcpT${i}"></div></div><div class="acts"><button class="btn sm" onclick="testMcp(${i})">测试</button><button class="btn sm danger" onclick="delMcp(${i})">删除</button></div></div>`).join("") || `<div class="small">没有</div>`}</div>
    <form class="grid2" onsubmit="addMcp(event)"><input name="name" placeholder="名称，例如 gmail" required /><input name="url" placeholder="https://…/mcp" required />
      <input name="hk" placeholder="请求头名（可空），例如 Authorization" /><input name="hv" placeholder="值，例如 Bearer {{secret:gmail_token}}" /><button class="btn primary span2">添加 MCP 服务器</button></form>
    <div class="label">导入 OpenAPI</div>
    <form class="grid2" onsubmit="importOpenAPI(event)"><input name="url" placeholder="openapi.json 地址" required /><input name="prefix" placeholder="前缀，例如 ha" />
      <input name="base_url" placeholder="API 根地址（可空）" /><input name="auth" placeholder="Authorization 头（可空），例如 Bearer {{secret:ha_token}}" /><button class="btn primary span2">导入</button></form>
    <div class="label">HTTP 工具（JSON）</div>
    <textarea id="httpTools" rows="6" style="font:12px ui-monospace,Menlo,monospace">${esc(JSON.stringify(integ.http_tools || [], null, 2))}</textarea>
    <div class="row gap"><button class="btn primary" onclick="saveHttpTools()">保存 HTTP 工具</button><button class="btn" onclick="listTools()">查看可用工具</button></div>
    <div id="toolList" class="small"></div>
    <div class="label">密钥（加密保存，只显示末 4 位）</div>
    <div class="list">${secs.map((s) => `<div class="item"><div class="grow"><b>${esc(s.name)}</b> <span class="small">${esc(s.preview)}</span></div><div class="acts"><button class="btn sm danger" onclick="delSecret('${esc(s.name)}')">删除</button></div></div>`).join("") || `<div class="small">没有</div>`}</div>
    <form class="grid2" onsubmit="addSecret(event)"><input name="n" placeholder="名字，例如 gmail_token" required /><input name="v" type="password" placeholder="值" required autocomplete="off" /><button class="btn primary span2">保存密钥</button></form>
  </div>

  <div class="card sec"><h3>连接</h3><div class="muted">API Token 保存在这台设备上。</div>
    <div class="row gap wrap"><button class="btn" onclick="localStorage.removeItem('aiw_token');location.href=location.pathname">更换 Token</button><a class="btn" href="/docs" target="_blank">API 文档</a></div></div>`;
  paintIcons($("settingsPage"));
  applyProfile();
  fetchModels(false);
}
function applyProfile() {
  $("agentName").textContent = S.profile.agent_name || "Agent";
  $("sideAvatar").innerHTML = avatarSVG(); $("sideAvatar").style.background = AVATAR_BG[S.profile.avatar] || AVATAR_BG.amber;
}
async function saveProfile(e) { e.preventDefault(); const f = e.target; S.profile = await api("PUT", "/api/profile", { name: f.name.value, agent_name: f.agent_name.value, tone: f.tone.value, about: f.about.value, avatar: f.avatar.value }); applyProfile(); toast("已保存"); }
async function addMem(e) { e.preventDefault(); const v = e.target.t.value.trim(); if (!v) return; await api("POST", "/api/memories", { text: v }); loadSettings(); }
async function editMem(m) { const v = prompt("编辑记忆", m.text); if (v === null) return; await api("PUT", `/api/memories/${m.id}`, { text: v }); loadSettings(); }
const MODEL_ROLES = [
  { key: "model", label: "主模型", hint: "决定操作，需能调用工具", need: "tools" },
  { key: "vision_model", label: "视觉模型", hint: "可选，主模型不会看图时设置", need: "vision" },
  { key: "vision_fallback", label: "备用视觉模型", hint: "视觉模型报错（如 HTTP 500）时改用它读图", need: "vision" },
  { key: "fallback", label: "备用模型", hint: "主模型出错时用", need: "tools" },
  { key: "subagent_model", label: "子 Agent 模型", hint: "纯文本命令行任务", need: "tools" },
  { key: "video_model", label: "视频模型", hint: "可选，制作/修改视频的任务用它（如 claude-opus），失败回到主模型", need: "tools" },
  { key: "image_model", label: "生图/改图模型", hint: "主网关上的 AI 生图和改图模型，如 gpt-image-1、gemini-2.5-flash-image", need: "image" },
  { key: "omni_model", label: "通话模型（全模态）", hint: "视频/语音通话用：全模态模型或 Gemini Live 模型", need: "omni" },
];
S.modelIds = []; S.probes = {}; S.modelGroups = [];
/* v3.9.33 供应商 (设置 › 模型): kv providers + the implicit 默认 (网关地址 + API Key). Each model slot has a
   "<slot>_provider" select next to it; the model list is grouped by provider ("供应商 · model") and picking a model
   from a group sets that slot's provider. Keys are write-only (PUT/POST body.key), GET shows has_key only. */
const PROVIDER_ONLY_SLOTS = [["chat_model", "聊天模型的供应商", "chat_model（只能用 API 设置）"], ["small_model", "小模型的供应商", "分类/摘要用的 small_model"],
  ["embed_model", "向量模型的供应商", "embed_model"], ["summary_model", "压缩模型的供应商", "summary_model"],
  ["image2_model", "生图接口的供应商", "选了就用它的地址和 Key，代替下面「生图接口」的地址/Key"],
  ["omni_tts_model", "朗读模型的供应商", "视频通话 › 朗读模型"], ["omni_gemini", "Gemini Live 的供应商", "Gemini Key 留空时用这个供应商的 Key（选 Gemini 供应商）"]];
function provName(id) { const p = (S.providers || []).find((x) => x.id === (id || "default")); return p ? p.name : (id || "默认"); }
function provSelect(name, cur) {
  const ps = S.providers || [];
  return `<select name="${name}" class="psel" title="供应商" onchange="fetchModels(false)">${ps.map((p) => `<option value="${p.id === "default" ? "" : esc(p.id)}" ${(cur || "") === (p.id === "default" ? "" : p.id) ? "selected" : ""}>${esc(p.name)}${p.enabled ? "" : "（已停用）"}</option>`).join("")}</select>`;
}
function providersHTML() {
  const ps = S.providers || [];
  return `<div class="rw head"><div class="k">供应商<small>多个 OpenAI 兼容接口；每个模型旁边选用哪个。「默认」= 上面的网关地址和 API Key</small></div>
    <div><button type="button" class="btn sm" onclick="editProvider()">${icon("plus")}添加</button></div></div>`
    + ps.map((p) => row(esc(p.name) + (p.default ? ' <span class="tag">默认</span>' : ""), esc(p.base_url) + (p.proxy ? " · 代理 " + esc(p.proxy) : ""),
      `<span class="small" id="provT_${esc(p.id)}"></span>`,
      `${p.has_key ? '<span class="chip ok">已设置</span>' : '<span class="chip">未设置 Key</span>'}${p.enabled ? "" : ' <span class="chip">已停用</span>'}
       <button type="button" class="btn sm" onclick="testProvider('${esc(p.id)}')">测试连接</button>${p.default ? "" : `<button type="button" class="btn sm" onclick="editProvider('${esc(p.id)}')">编辑</button><button type="button" class="btn sm ghost" style="color:var(--bad)" onclick="delProvider('${esc(p.id)}')">删除</button>`}`)).join("");
}
function editProvider(id) {
  const p = (S.providers || []).find((x) => x.id === id) || { id: "", name: "", base_url: "", proxy: "", enabled: true, kind: "openai" };
  const presets = id ? "" : `<div class="row gap">${(S.provPresets || []).map((x, i) => `<button type="button" class="btn sm" onclick="provPreset(${i})">${esc(x.name)}</button>`).join("")}</div><div class="muted" id="provHint"></div>`;
  modal(`<form class="callnote" onsubmit="saveProvider(event, '${esc(id || "")}')"><h3>${id ? "编辑供应商" : "添加供应商"}</h3>${presets}
    <label>名称<input name="name" value="${esc(p.name)}" required placeholder="百炼" /></label>
    ${id ? "" : `<label>ID<input name="id" value="" placeholder="留空按名称生成，如 bailian" /></label>`}
    <label>类型<select name="kind"><option value="openai">OpenAI 兼容</option></select></label>
    <label>地址<input name="base_url" value="${esc(p.base_url)}" required placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" /></label>
    <label>API Key<input name="key" type="password" autocomplete="off" placeholder="${p.has_key ? "已设置，留空不修改" : "sk-…"}" /></label>
    <label>代理（可选）<input name="proxy" value="${esc(p.proxy || "")}" placeholder="http://127.0.0.1:7890" /></label>
    <label><input type="checkbox" name="enabled" ${p.enabled ? "checked" : ""} /> 启用</label>
    <div class="row gap"><button class="btn primary">保存</button><button type="button" class="btn" onclick="testProviderForm(this, '${esc(id || "")}')">测试连接</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    <div class="small" id="provFormT"></div></form>`);
}
function provPreset(i) {
  const x = S.provPresets[i]; const f = document.querySelector("#modal form"); if (!x || !f) return;
  f.name.value = x.name; f.id.value = x.id; f.base_url.value = x.base_url; const h = $("provHint"); if (h) h.textContent = x.hint || "";
}
function provBody(f) { return { name: f.name.value.trim(), base_url: f.base_url.value.trim(), proxy: f.proxy.value.trim(), kind: f.kind.value, enabled: f.enabled.checked, ...(f.key.value ? { key: f.key.value.trim() } : {}), ...(f.id && f.id.value.trim() ? { id: f.id.value.trim() } : {}) }; }
async function saveProvider(e, id) {
  e.preventDefault();
  try { await api(id ? "PUT" : "POST", id ? `/api/providers/${id}` : "/api/providers", provBody(e.target)); closeOverlays(); toast("供应商已保存"); loadSettings(); }
  catch (err) { $("provFormT").textContent = "保存失败：" + err.message; }
}
async function testProviderForm(btn, id) {
  const f = btn.closest("form"); const out = $("provFormT"); out.innerHTML = `<span class="spin"></span> 正在读取模型列表…`;
  try { const r = await api("POST", `/api/providers/${id || "new"}/test`, provBody(f)); out.textContent = r.ok ? `连接正常：${r.count} 个模型，${(r.ms / 1000).toFixed(1)}s` : "连不上：" + (r.error || ""); }
  catch (err) { out.textContent = "测试失败：" + err.message; }
}
async function testProvider(id) {
  const out = $("provT_" + id); if (out) out.innerHTML = `<span class="spin"></span>`;
  try { const r = await api("POST", `/api/providers/${id}/test`, {}); if (out) out.textContent = r.ok ? `✓ ${r.count} 个模型 · ${(r.ms / 1000).toFixed(1)}s` : "✕ " + (r.error || "").slice(0, 100); }
  catch (err) { if (out) out.textContent = "✕ " + err.message; }
}
async function delProvider(id) {
  if (!confirm(`删除供应商「${provName(id)}」？它的 Key 也会删除`)) return;
  try { await api("DELETE", `/api/providers/${id}`); }
  catch (err) {
    if (!/还在用/.test(err.message) || !confirm(err.message + "\n\n强制删除？用到它的设置会回到「默认」")) return toast("没删：" + err.message);
    await api("DELETE", `/api/providers/${id}?force=1`);
  }
  toast("已删除"); loadSettings();
}
function pickModelOption(sel) {   // a model picked from a provider's group also picks that provider for the slot
  const o = sel.options[sel.selectedIndex]; if (!o || o.dataset.p === undefined) return;
  const ps = sel.parentNode.querySelector(".psel"); if (ps && ps.value !== o.dataset.p) ps.value = o.dataset.p;
}
async function fetchModels(manual) {
  const note = $("modelListNote"); if (note) note.textContent = "正在读取…";
  const [r, probes] = await Promise.all([api("GET", "/api/models").catch((e) => ({ models: [], error: e.message })), api("GET", "/api/models/probes").catch(() => ({}))]);
  S.modelIds = r.models || []; S.probes = probes || {}; S.modelGroups = r.groups || [];
  const nOther = S.modelGroups.slice(1).reduce((a, g) => a + (g.models || []).length, 0);
  if (note) note.textContent = r.error ? "读取失败：" + r.error.slice(0, 80) : `网关有 ${S.modelIds.length} 个模型` + (nOther ? `，其他供应商 ${nOther} 个` : "");
  document.querySelectorAll("#sModel .mrow select:not(.psel)").forEach((sel) => {
    const cur = sel.value || sel.dataset.cur || "";
    const ps = sel.parentNode.querySelector(".psel"); const pid = ps ? ps.value : "";
    const role = MODEL_ROLES.find((x) => x.key === sel.name);
    if (!role) return;
    if (S.modelGroups.length > 1) {   // v3.9.33: grouped "供应商 · model", the slot's own provider first
      const gs = [...S.modelGroups].sort((a, b) => ((b.provider === (pid || "default")) - (a.provider === (pid || "default"))));
      const inAny = S.modelGroups.some((g) => (g.models || []).includes(cur));
      sel.innerHTML = `<option value="">${role.key === "model" ? "默认（.env）" : role.key === "omni_model" ? "不用（关闭通话）" : "不用 / 同主模型"}</option>`
        + (cur && !inAny ? `<option value="${esc(cur)}" selected>${esc(cur)}</option>` : "")
        + gs.map((g) => `<optgroup label="${esc(g.name)}${g.error ? "（读取失败）" : ""}">${(g.models || []).map((m) => `<option value="${esc(m)}" data-p="${g.provider === "default" ? "" : esc(g.provider)}" ${m === cur && g.provider === (pid || "default") ? "selected" : ""}>${esc(g.name)} · ${esc(m)}${probeTag(m)}</option>`).join("")}</optgroup>`).join("")
        + `<option value="__custom">手动输入…</option>`;
      if (cur && sel.value !== cur) sel.value = cur;
      paintProbe(sel);
      return;
    }
    const ids = [...new Set([...(cur ? [cur] : []), ...S.modelIds])];
    sel.innerHTML = `<option value="">${role.key === "model" ? "默认（.env）" : role.key === "vision_model" ? "不用（主模型自己看图）" : role.key === "fallback" || role.key === "vision_fallback" ? "不用" : role.key === "image_model" ? "不用（只用生图接口）" : role.key === "omni_model" ? "不用（关闭通话）" : "同主模型"}</option>`
      + ids.map((m) => `<option value="${esc(m)}" ${m === cur ? "selected" : ""}>${esc(m)}${probeTag(m)}</option>`).join("")
      + `<option value="__custom">手动输入…</option>`;
    paintProbe(sel);
  });
  if (manual && !r.error) toast(`找到 ${S.modelIds.length} 个模型`);
}
function probeTag(m) { const p = S.probes[m]; if (!p) return ""; return "  " + (p.tools ? "🛠" : "") + (p.vision ? "👁" : "") + (!p.tools && !p.vision ? "✕" : "") + ({ fast: " ⚡快", medium: " 中速", slow: " 🐢慢" }[p.speed] || ""); }
function paintProbe(sel) {
  if (sel.value === "__custom") {
    const v = prompt("模型名称"); const val = (v || "").trim();
    if (val) { const o = document.createElement("option"); o.value = val; o.textContent = val; sel.insertBefore(o, sel.lastChild); sel.value = val; } else sel.value = sel.dataset.cur || "";
  }
  const box = sel.parentNode.querySelector(".probe"); const p = S.probes[sel.value];
  const role = MODEL_ROLES.find((x) => x.key === sel.name);
  if (!sel.value) { box.innerHTML = ""; return; }
  if (role.need === "image") { box.innerHTML = `<span class="muted">需要网关支持图片输出，第一次改图时验证</span>`; return; }
  if (role.need === "omni") { box.innerHTML = omniProbeHTML((S.omniProbes || {})[sel.value], omniFamily(sel)); return; }
  if (!p) { box.innerHTML = `<span class="muted">未探测</span>`; return; }
  const ok = (b, t) => `<span class="chip ${b ? "ok" : "bad"}">${b ? "✓" : "✕"} ${t}</span>`;
  let warn = "";
  if (role.need === "tools" && !p.tools) warn = "这个模型调用工具时参数不完整，不适合做主模型";
  if (role.need === "vision" && !p.vision) warn = "这个模型看不懂图片";
  if (role.need === "tools" && p.tools && p.speed === "slow") warn = "能用但很慢，每步可能要几十秒到几分钟，建议换更快的主模型";
  const spd = p.speed ? `<span class="chip ${p.speed === "slow" ? "bad" : "ok"}">${{ fast: "⚡ 快", medium: "中速", slow: "🐢 慢" }[p.speed]} · ${(p.speed_ms / 1000).toFixed(1)}s${p.tok_s ? " · " + p.tok_s + " tok/s" : ""}${p.tools_ms ? " · 工具 " + (p.tools_ms / 1000).toFixed(1) + "s" : ""}</span>` : "";
  box.innerHTML = `${ok(p.tools, "工具调用" + (p.tool_choice === "auto" ? "(auto)" : ""))} ${ok(p.vision, "看图")} ${spd}`
    + (warn ? `<div style="color:var(--danger)">${esc(warn)}</div>` : "")
    + (p.notes && p.notes.length && (!p.tools || !p.vision || p.reasoning_tokens) ? `<div class="muted">${esc(p.notes.join("；").slice(0, 200))}</div>` : "");
}
/* v3.9.31 通话模型 › 探测: saves the omni settings on screen first, then tries the realtime endpoint and a tiny turn-mode
   request (POST /api/omni/probe) and shows which mode works and whether the model speaks */
/* v3.9.34: which kind of call model the slot holds - "gemini" (a Gemini provider or a Gemini Live model name: routed
   to Gemini Live), "qwen" (qwen*-omni) or "" - mirrors omni_call.family(); the probe result's family wins */
const OMNI_HINTS = { gemini: "Gemini Live：国内需代理（填「出站代理」或在供应商里填代理）；免费层数据会用于改进产品",
  qwen: "Qwen-Omni：qwen3.8-omni-flash 只输出文字（轮流模式配「朗读模型」）；qwen3.8-omni-flash-realtime 要填业务空间实时接口地址" };
function omniFamily(sel) {
  const m = (sel.value || "").trim().toLowerCase().replace(/^models\//, ""); if (!m) return "";
  const pr = (S.omniProbes || {})[sel.value]; if (pr && pr.family !== undefined) return pr.family;
  const ps = sel.parentNode.querySelector(".psel"); const pid = ps ? ps.value : "";
  const p = pid ? (S.providers || []).find((x) => x.id === pid) : null;
  const host = (u) => { try { return new URL(/^[a-z]+:\/\//i.test(u || "") ? u : "https://" + u).hostname.toLowerCase(); } catch (e) { return ""; } };
  const f = sel.closest("form"); const relay = f && f.omni_gemini_url ? host(f.omni_gemini_url.value.trim()) : "";
  const gp = p && (host(p.base_url) === "generativelanguage.googleapis.com" || p.id === "gemini" || (p.name || "").trim().toLowerCase() === "gemini" || (relay && host(p.base_url) === relay));
  if (gp || (m.startsWith("gemini-") && m.includes("live")) || m.includes("native-audio")) return "gemini";
  return m.startsWith("qwen") && m.includes("omni") ? "qwen" : "";
}
function omniProbeHTML(p, fam) {
  fam = p && p.family !== undefined ? p.family : (fam || "");
  let hint = OMNI_HINTS[fam] ? `<div class="muted">${esc(OMNI_HINTS[fam])}</div>` : "";
  if (!p) return `<span class="muted">未探测：点「探测」试${fam === "gemini" ? " Gemini Live" : "实时接口和轮流模式"}</span>` + hint;
  const ok = (b, t) => `<span class="chip ${b ? "ok" : "bad"}">${b ? "✓" : "✕"} ${t}</span>`;
  const rt = p.realtime || {}, tn = p.turn || {}, gm = p.gemini;
  const notes = (p.notes || []).length ? `<div class="muted">${esc(p.notes.join("；").slice(0, 240))}</div>` : "";
  hint += ` <a href="#" onclick="omniCallsDiag();return false" class="small">通话诊断</a>`;   // v3.9.35
  if (p.route === "gemini" && gm) {   // v3.9.34: the call slot is routed to Gemini Live - only its badge
    return `${ok(gm.ok, "Gemini Live" + (gm.ms ? " " + (gm.ms / 1000).toFixed(1) + "s" : ""))} ${gm.ok ? ok(gm.audio !== false, "会说话") : ""}`
      + `<div class="muted">${gm.ok ? "通话会用：Gemini Live（" + esc(gm.model || "") + "）" : "Gemini Live 不通：" + esc((gm.error || "").slice(0, 120))}</div>` + notes + hint;
  }
  const gHTML = gm ? `${ok(gm.ok, "Gemini" + (gm.ms ? " " + (gm.ms / 1000).toFixed(1) + "s" : ""))} ` : "";
  if (gm && !p.model) return gHTML + `<div class="muted">${gm.ok ? "通话会用：Gemini Live" : "Gemini 不通：" + esc((gm.error || "").slice(0, 120))}</div>` + notes;
  const rtHTML = rt.skipped ? "" : `${ok(rt.ok, "实时" + (rt.ms ? " " + (rt.ms / 1000).toFixed(1) + "s" : ""))} `;
  return gHTML + rtHTML + `${ok(tn.ok, "轮流" + (tn.ms ? " " + (tn.ms / 1000).toFixed(1) + "s" : ""))} ${tn.ok ? ok(tn.audio, "会说话") : ""}`
    + `<div class="muted">${p.mode ? "通话会用：" + ({ realtime: "实时模式", gemini: "Gemini Live" }[p.mode] || "轮流模式") : "都不通"}${gm && gm.error ? "；Gemini：" + esc(gm.error.slice(0, 100)) : ""}${rt.error ? "；实时：" + esc(rt.error.slice(0, 100)) : ""}${tn.error ? "；轮流：" + esc(tn.error.slice(0, 100)) : ""}${rt.skipped ? "；实时接口未试（供应商不是百炼）" : ""}</div>`
    + notes + hint;
}
/* v3.9.35 设置 › 视频通话 › 通话诊断: the last 20 calls' connection timelines and counters (GET /api/omni/calls) */
async function omniCallsDiag() {
  let r;
  try { r = await api("GET", "/api/omni/calls"); } catch (e) { return toast("读取通话诊断失败：" + e.message); }
  const calls = r.calls || [];
  const when = (t) => new Date(t * 1000).toLocaleString();
  const one = (c, i) => { const n = c.counters || {};
    return `<details ${i === 0 ? "open" : ""} style="margin-top:8px"><summary>${esc(when(c.started))} · ${esc(c.backend || "未接通")} ${esc(c.model || "")} · ${Math.round(c.seconds)} 秒 · ${c.active ? "进行中" : esc(c.reason || "")}${c.last_error ? ` · <b>${esc(c.last_error.slice(0, 90))}</b>` : ""}</summary>
      <div class="small muted">麦克风 ${n.audio_in || 0} 段 → 发给模型 ${n.audio_up || 0}（重连时排队 ${n.audio_pending || 0}）· 画面 ${n.frames_in || 0}（丢 ${n.frames_dropped || 0}）· 模型消息 ${n.up_msgs || 0} · 回放声音 ${n.audio_out || 0} 段 · 重连 ${n.reconnects || 0} · 断开 ${n.upstream_closes || 0} · 看门狗 ${n.watchdog || 0}</div>
      <pre style="white-space:pre-wrap;font-size:12px;max-height:40vh;overflow:auto">${(c.events || []).map((e) => `${String(e[0]).padStart(6)}s ${e[2] === "warn" ? "⚠ " : ""}${esc(e[1])}`).join("\n")}</pre></details>`; };
  modal(`<div class="card"><div class="heading">通话诊断</div>
    <div class="muted">最近 ${calls.length} 次通话（服务重启后清空）。服务器日志：<code>${esc(r.log_hint || "")}</code></div>
    <div style="max-height:70vh;overflow:auto">${calls.length ? calls.map(one).join("") : '<div class="muted" style="margin-top:8px">还没有通话记录</div>'}</div>
    <div class="row gap" style="justify-content:flex-end;margin-top:10px"><button class="btn sm" onclick="omniCallsDiag()">刷新</button><button class="btn sm" onclick="closeOverlays()">关闭</button></div></div>`);
}
async function probeOmni(sel, box, btn) {
  box.innerHTML = `<span class="spin"></span> 正在试 Gemini / 实时接口 / 轮流模式…`; btn.disabled = true;
  try {
    const f = sel.closest("form"); const body = { omni_model: sel.value };
    const ps = sel.parentNode.querySelector(".psel"); if (ps) body.omni_model_provider = ps.value;   // v3.9.34: probe the provider on screen
    for (const k of ["omni_voice", "omni_realtime_url", "omni_realtime_model", "omni_tts_model", "omni_gemini_model", "omni_gemini_voice", "omni_gemini_url", "omni_proxy", "omni_gemini_vad_start", "omni_gemini_vad_end", "omni_gemini_silence_ms"]) if (f && f[k]) body[k] = f[k].value.trim();
    if (f && f.omni_mode) body.omni_mode = (f.querySelector("[name=omni_mode]:checked") || {}).value || "auto";
    if (f && f.omni_key && f.omni_key.value) await api("PUT", "/api/secrets/omni_realtime_key", { value: f.omni_key.value });
    if (f && f.omni_gemini_key && f.omni_gemini_key.value) await api("PUT", "/api/secrets/omni_gemini_key", { value: f.omni_gemini_key.value });
    await api("PATCH", "/api/settings/models", body);
    const r = await api("POST", "/api/omni/probe", {});
    S.omniProbes = S.omniProbes || {}; S.omniProbes[sel.value] = r;
    box.innerHTML = r.error ? esc(r.error) : omniProbeHTML(r, omniFamily(sel));
  } catch (e) { box.textContent = "探测失败：" + e.message; } finally { btn.disabled = false; }
}
async function probeModel(btn) {
  const sel = btn.parentNode.querySelector("select:not(.psel)");
  if (sel.name === "omni_model") {   // v3.9.32: a Gemini-only setup (no omni model) can be probed too
    const f = sel.closest("form"); const gk = f && ((f.omni_gemini_key && f.omni_gemini_key.value) || (f.omni_gemini_key && f.omni_gemini_key.placeholder.startsWith("已设置")));
    if (!sel.value && !gk) return toast("先选通话模型或填 Gemini Key");
    return probeOmni(sel, btn.parentNode.querySelector(".probe"), btn);
  }
  if (!sel.value) return toast("先选一个模型");
  const box = btn.parentNode.querySelector(".probe"); box.innerHTML = `<span class="spin"></span> 正在实测…`; btn.disabled = true;
  try {
    const base = document.querySelector("#sModel [name=base_url]").value.trim();
    const ps = sel.parentNode.querySelector(".psel");   // v3.9.33: probe through the slot's provider
    const r = await api("POST", "/api/models/probe", { model: sel.value, provider: ps ? ps.value : undefined, base_url: (!ps || !ps.value) && base ? base : undefined });
    if (r.error) { box.textContent = r.error; return; }
    S.probes[sel.value] = r;
    document.querySelectorAll("#sModel .mrow select:not(.psel)").forEach((s2) => { [...s2.options].forEach((o) => { if (o.value && o.value !== "__custom") o.textContent = (o.dataset.p !== undefined && S.modelGroups.length > 1 ? provName(o.dataset.p) + " · " : "") + o.value + probeTag(o.value); }); paintProbe(s2); });
  } catch (e) { box.textContent = "探测失败：" + e.message; } finally { btn.disabled = false; }
}
async function saveModels(e) {
  e.preventDefault(); const f = e.target;
  if (f.key.value) await api("PUT", "/api/secrets/openai_api_key", { value: f.key.value });
  if (f.image2_key && f.image2_key.value) await api("PUT", "/api/secrets/image2_api_key", { value: f.image2_key.value });
  const v = (n) => (f[n].value === "__custom" ? "" : f[n].value);
  const body = { model: v("model"), vision_model: v("vision_model"), vision_fallback: v("vision_fallback"), fallback: v("fallback"), subagent_model: v("subagent_model"), video_model: v("video_model"), image_model: v("image_model"), base_url: f.base_url.value, thinking: f.thinking.value };
  if (f.image2_base_url) { body.image2_base_url = f.image2_base_url.value.trim(); body.image2_model = f.image2_model.value.trim(); body.image_default = (f.image_default && f.image_default.value) || ""; }
  if (f.omni_model) body.omni_model = v("omni_model");
  f.querySelectorAll("select.psel").forEach((s) => { body[s.name] = s.value; });   // v3.9.33 *_provider
  if (f.omni_voice) {   // v3.9.31 视频通话
    body.omni_voice = f.omni_voice.value; body.omni_realtime_url = f.omni_realtime_url.value.trim(); body.omni_realtime_model = f.omni_realtime_model.value.trim();
    body.omni_tts_model = f.omni_tts_model.value.trim(); body.omni_mode = (f.querySelector("[name=omni_mode]:checked") || {}).value || "auto";
    body.omni_max_minutes = f.omni_max_minutes.value.trim();
    if (f.omni_key && f.omni_key.value) await api("PUT", "/api/secrets/omni_realtime_key", { value: f.omni_key.value });
    if (f.omni_gemini_model) {   // v3.9.32 Gemini Live
      body.omni_gemini_model = f.omni_gemini_model.value.trim(); body.omni_gemini_voice = f.omni_gemini_voice.value;
      body.omni_gemini_url = f.omni_gemini_url.value.trim(); body.omni_proxy = f.omni_proxy.value.trim();
      if (f.omni_gemini_vad_start) {   // v3.9.35 Gemini VAD
        body.omni_gemini_vad_start = f.omni_gemini_vad_start.value; body.omni_gemini_vad_end = f.omni_gemini_vad_end.value;
        body.omni_gemini_silence_ms = f.omni_gemini_silence_ms.value.trim();
      }
      if (f.omni_gemini_key && f.omni_gemini_key.value) await api("PUT", "/api/secrets/omni_gemini_key", { value: f.omni_gemini_key.value });
    }
  }
  await api("PUT", "/api/settings/models", body);
  toast("已保存，下个任务生效"); loadSettings();
}
/* v3.9.15 设置 → 生图接口 → 测试: saves first (so the test uses what is on screen), then one small picture per endpoint */
async function testImage(btn) {
  const out = $("imageTestOut"); const f = btn.closest("form");
  btn.disabled = true; out.innerHTML = `<span class="spin"></span> 先保存，再各生成一张小图（可能要一分钟）…`;
  try {
    if (f.image2_key && f.image2_key.value) await api("PUT", "/api/secrets/image2_api_key", { value: f.image2_key.value });
    await api("PATCH", "/api/settings/models", { image2_base_url: f.image2_base_url.value.trim(), image2_model: f.image2_model.value.trim(), image_default: f.image_default.value || "" });
    const r = await api("POST", "/api/image/test", {});
    out.innerHTML = r.results && r.results.length ? r.results.map((x, i) => `<span class="chip ${x.ok ? "ok" : "bad"}">${i === 0 ? "默认" : "备选"} · ${esc(x.name)} · ${esc(x.model)} ${x.ok ? "✓ " + (x.ms / 1000).toFixed(1) + "s" : "✕"}</span>${x.ok ? "" : ` <span class="small" style="color:var(--bad)">${esc((x.error || "").slice(0, 160))}</span>`}`).join("<br>") : esc(r.error || "没有可测的接口");
  } catch (e) { out.textContent = "测试失败：" + e.message; } finally { btn.disabled = false; }
}
async function saveVmNet(e) {
  e.preventDefault(); const f = e.target;
  try { const r = await api("PUT", "/api/settings/vm", { ip: f.ip.value.trim(), auto: f.auto.checked }); toast("已保存，现连接 " + r.agent_endpoint); }
  catch (err) { toast("保存失败：" + (err.message || err)); }
}
async function detectVmIp() {
  try { const r = await api("POST", "/api/settings/vm/detect"); document.querySelector("#sVmNet [name=ip]").value = r.ip; toast("检测到 " + r.ip + "，点保存生效"); }
  catch (err) { toast("检测失败：" + (err.message || err)); }
}
async function saveNotify(e) { e.preventDefault(); const f = e.target; await api("PUT", "/api/settings/notify", { bark_url: f.bark_url.value, ntfy_url: f.ntfy_url.value, webhook_url: f.webhook_url.value,
  public_url: f.public_url ? f.public_url.value : undefined, link_mode: f.link_mode ? f.link_mode.value : undefined, bark_sound: f.bark_sound ? f.bark_sound.value : undefined, bark_critical: f.bark_critical ? f.bark_critical.value : undefined }); toast("已保存"); }
async function addMcp(e) {
  e.preventDefault(); const f = e.target; const cur = (await api("GET", "/api/integrations")).mcp_servers || [];
  cur.push({ name: f.name.value, url: f.url.value, headers: f.hk.value ? { [f.hk.value]: f.hv.value } : {} });
  try { await api("PUT", "/api/integrations", { mcp_servers: cur }); toast("已添加"); loadSettings(); } catch (err) { toast(err.message); }
}
const MCP_PRESETS = [
  { name: "exa", title: "Exa 联网搜索", desc: "搜网页、读网页全文，查规范、材料、做法（免费，无需 Key）", url: "https://mcp.exa.ai/mcp" },
  { name: "jina", title: "Jina 网页阅读", desc: "把任意网址转成干净正文、给网页截图（读网页免 Key）", url: "https://mcp.jina.ai/v1" },
  { name: "context7", title: "Context7 技术文档", desc: "查 ezdxf、LibreOffice、Python 库的最新文档和示例", url: "https://mcp.context7.com/mcp" },
  { name: "deepwiki", title: "DeepWiki", desc: "读懂任意 GitHub 开源项目（结构、用法、问答）", url: "https://mcp.deepwiki.com/mcp" },
  { name: "mslearn", title: "Microsoft Learn", desc: "Office、Excel、Windows 官方文档和代码示例", url: "https://learn.microsoft.com/api/mcp" },
  { name: "amap", title: "高德地图", desc: "地点搜索、路线规划、天气、地址转坐标（高德开放平台免费申请「Web 服务」Key）",
    url: "https://mcp.amap.com/mcp?key={{secret:amap_key}}", secret: "amap_key", keyName: "高德 Web 服务 Key" },
  { name: "github", title: "GitHub", desc: "读写你的仓库、Issue、PR（GitHub 个人访问令牌）",
    url: "https://api.githubcopilot.com/mcp/", headers: { Authorization: "Bearer {{secret:github_token}}" }, secret: "github_token", keyName: "GitHub 个人访问令牌（ghp_… / github_pat_…）" },
];
async function addPreset(name) {
  const p = MCP_PRESETS.find((x) => x.name === name); if (!p) return;
  try {
    if (p.secret) {
      const have = (await api("GET", "/api/secrets")).some((x) => (x.name || x) === p.secret);
      if (!have) {
        const v = prompt(`填入${p.keyName}（加密保存，不再显示）`); if (!v || !v.trim()) return;
        await api("PUT", `/api/secrets/${p.secret}`, { value: v.trim() });
      }
    }
    toast("连接测试中…");
    const t = await api("POST", "/api/integrations/mcp/test", { url: p.url, headers: p.headers || {} });
    if (!t.tools.length) throw new Error(p.secret ? "没有返回任何工具，Key 可能无效（可在下方密钥里删掉 " + p.secret + " 重填）" : "没有返回任何工具");
    const cur = (await api("GET", "/api/integrations")).mcp_servers || [];
    cur.push({ name: p.name, url: p.url, headers: p.headers || {} });
    await api("PUT", "/api/integrations", { mcp_servers: cur });
    toast(`已添加 ${p.title}：${t.tools.length} 个工具`); loadSettings();
  } catch (err) { toast("添加失败：" + err.message); }
}
async function testMcp(i) {
  const m = ((await api("GET", "/api/integrations")).mcp_servers || [])[i]; const el = $("mcpT" + i); if (!m || !el) return;
  el.textContent = "测试中…";
  try { const t = await api("POST", "/api/integrations/mcp/test", { url: m.url, headers: m.headers || {} }); el.textContent = `可用 · ${t.tools.length} 个工具：${t.tools.slice(0, 8).join("、")}${t.tools.length > 8 ? "…" : ""}`; }
  catch (err) { el.textContent = "不可用：" + err.message; }
}
async function delMcp(i) { const cur = (await api("GET", "/api/integrations")).mcp_servers || []; cur.splice(i, 1); await api("PUT", "/api/integrations", { mcp_servers: cur }); loadSettings(); }
async function importOpenAPI(e) {
  e.preventDefault(); const f = e.target;
  try { const r = await api("POST", "/api/integrations/openapi", { url: f.url.value, prefix: f.prefix.value, base_url: f.base_url.value || null, headers: f.auth.value ? { Authorization: f.auth.value } : {} });
    toast(`导入了 ${r.imported} 个工具`); loadSettings(); } catch (err) { toast(err.message); }
}
async function saveHttpTools() { try { await api("PUT", "/api/integrations", { http_tools: JSON.parse($("httpTools").value || "[]") }); toast("已保存"); } catch (e) { toast("保存失败：" + e.message); } }
async function listTools() { $("toolList").textContent = "加载中…"; const t = await api("GET", "/api/integrations/tools"); $("toolList").innerHTML = t.map((x) => `<div>• <b>${esc(x.name)}</b> ${esc(x.description)}</div>`).join("") || "没有可用的外部工具"; }
async function addSecret(e) { e.preventDefault(); const f = e.target; await api("PUT", `/api/secrets/${encodeURIComponent(f.n.value)}`, { value: f.v.value }); toast("已加密保存"); loadSettings(); }
async function delSecret(n) { if (!confirm("删除密钥 " + n + "？")) return; await api("DELETE", `/api/secrets/${encodeURIComponent(n)}`); loadSettings(); }

/* ---------------------------------------------------------------- boot */
paintIcons();
api("GET", "/api/profile").then((p) => { S.profile = p; applyProfile(); renderChat(); }).catch(() => {});
go("chat"); loadThreads(); refreshThread(true); loadDraft(); loadMachines().then(() => { loadDesktop(); refreshMachine(); }); loadNotes(); connectEvents();
setInterval(refreshMachine, 10000);
setInterval(() => { if (busyTask && S.view === "chat") renderChat(); }, 5000);
if (NATIVE) { document.body.classList.add("native"); NATIVE.postMessage({ op: "ready" }); }

/* v3.9.17: per-step time split, so a slow task shows where the time went (model / tool / waiting for a confirm) */
function fmtDur(ms) { ms = +ms || 0; return ms >= 60000 ? `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s` : `${(ms / 1000).toFixed(1)}s`; }
function stepTimes(s) {
  const p = [];
  if (s.ms) p.push(`思考 ${fmtDur(s.ms)}`);
  if (s.tms != null && s.tms > 0) p.push(`工具 ${fmtDur(s.tms)}`);
  if (s.wms) p.push(`等确认 ${fmtDur(s.wms)}`);
  return p.join(" · ");
}
