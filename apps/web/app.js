/* AI Workstation console (vanilla JS, no build step). */
"use strict";
const Q = new URLSearchParams(location.search);
const MID = Q.get("machine") || "ai-linux-001";
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
  bell: "M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0", menu: "M4 6h16M4 12h16M4 18h16",
  clip: "M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5",
  mic: "M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3zM19 10v2a7 7 0 0 1-14 0v-2M12 19v3",
  up: "M12 19V5M5 12l7-7 7 7", stop: "M7 7h10v10H7z", check: "M20 6 9 17l-5-5", x: "M18 6 6 18M6 6l12 12",
  chev: "M9 18l6-6-6-6", file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  retry: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5", play: "M6 4l14 8-14 8z", hand: "M18 11V6a2 2 0 0 0-4 0M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8a8 8 0 0 0 16 0v-3a2 2 0 0 0-4 0",
  receipt: "M4 2v20l3-2 3 2 3-2 3 2 3-2 1 .7V2l-3 2-3-2-3 2-3-2-3 2zM8 8h8M8 12h8M8 16h5", trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
  bolt: "M13 2 3 14h9l-1 8 10-12h-9z", brain: "M12 5a3 3 0 0 0-5.9.8A3 3 0 0 0 4 11a3 3 0 0 0 2 5.2A3 3 0 0 0 12 18zM12 5a3 3 0 0 1 5.9.8A3 3 0 0 1 20 11a3 3 0 0 1-2 5.2A3 3 0 0 1 12 18z",
  plug: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4", key: "M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4",
  user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", speaker: "M11 5 6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14",
};
const icon = (n, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24"><path d="${IC[n] || ""}"/></svg>`;
function paintIcons(root = document) { root.querySelectorAll("i[data-i]").forEach((el) => { el.outerHTML = icon(el.dataset.i); }); }
const AVATAR_BG = { sky: "#EDF7FD", sand: "#FAF0DF", lilac: "#F1ECF9" };
function avatarSVG() {
  return `<svg viewBox="0 0 48 48"><circle cx="24" cy="26" r="15" fill="#fff" stroke="#11191C" stroke-width="2"/><circle cx="18.5" cy="25" r="2.2" fill="#11191C"/><circle cx="29.5" cy="25" r="2.2" fill="#11191C"/><path d="M19 31.5c3 2.4 7 2.4 10 0" stroke="#11191C" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M24 11V6" stroke="#11191C" stroke-width="2" stroke-linecap="round"/><circle cx="24" cy="5" r="2.4" fill="#1473C8"/></svg>`;
}

/* ---------------------------------------------------------------- state */
const S = { view: "chat", threadId: localStorage.getItem("aiw_thread") || null, thread: null, threads: [], archived: false,
  evs: {}, evSt: {}, sock: {}, profile: {}, notes: [], attach: [], take: false, agentFails: 0, actFilter: "all", tasks: [] };
const TOOL_CN = { screenshot: "截图", click: "点击", double_click: "双击", right_click: "右键", move: "移动鼠标", drag: "拖动", type: "输入文字",
  keypress: "按键", scroll: "滚动", open_app: "打开应用", close_app: "关闭应用", shell: "执行命令", browser_open: "打开浏览器",
  browser_navigate: "打开网址", browser_back: "后退", browser_forward: "前进", files_list: "列文件", files_read: "读文件", files_write: "写文件",
  files_download: "下载", send_file: "发送文件", done: "完成", fail: "放弃", update_plan: "更新计划", ask_user: "提问", remember: "记住",
  delegate: "交给助手" };
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
    <div class="t1">${esc(t.title)}</div><div class="t2">${t.last_status ? (STATUS_CN[t.last_status] || [t.last_status])[0] + " · " : ""}${ago(t.updated_at)}</div></button>`).join("")
    || `<div class="small pad">还没有对话</div>`;
}
function toggleArchived() { S.archived = !S.archived; $("archToggle").textContent = S.archived ? "返回对话" : "查看已归档"; loadThreads(); }
function newThread() { S.threadId = null; S.thread = null; localStorage.removeItem("aiw_thread"); go("chat"); renderChat(); loadDraft(); $("prompt").focus(); loadThreads(); }
async function openThread(id) {
  S.threadId = id; localStorage.setItem("aiw_thread", id);
  go("chat"); await refreshThread(true); loadDraft(); loadThreads();
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
    if (t.live) subscribe(t);
  }));
  renderChat(scroll);
}
async function renameThread() {
  if (!S.thread) return; const n = prompt("对话名称", S.thread.title); if (!n) return;
  await api("PATCH", `/api/threads/${S.thread.id}`, { title: n }); refreshThread(); loadThreads();
}
async function archiveThread(flag) { await api("PATCH", `/api/threads/${S.thread.id}`, { archived: flag }); toast(flag ? "已归档" : "已恢复"); newThread(); }

/* ---------------------------------------------------------------- live session sockets */
function subscribe(t) {
  if (S.sock[t.session_id]) return;
  const ws = new WebSocket(wsUrl(`/ws/session/${t.session_id}`));
  S.sock[t.session_id] = ws; S.evs[t.id] = [];
  ws.onmessage = (m) => { const e = JSON.parse(m.data); if (e.kind !== "event") return; (S.evs[t.id] = S.evs[t.id] || []).push(e); onLiveEvent(t, e); };
  ws.onclose = () => { delete S.sock[t.session_id]; };
}
let rerenderQ = null;
function onLiveEvent(t, e) {
  const d = e.data || {};
  if (e.type === "agent.confirm_required") t.pending_confirm = d;
  if (e.type === "agent.input_required") t.pending_question = d;
  if (e.type === "agent.input_answered") t.pending_question = null;
  if (e.type === "agent.action_result" && t.pending_confirm) t.pending_confirm = null;
  if (e.type === "agent.takeover") { t.status = "takeover"; setTake(true, true); }
  if (e.type === "agent.resumed") { t.status = "running"; setTake(false, true); }
  if (e.type === "agent.paused") t.status = "paused";
  if (e.type === "task.finished" || e.type === "task.failed") {
    t.status = d.status || (e.type === "task.finished" ? "succeeded" : "failed"); t.result = d.result; t.live = false;
    if (e.type === "task.finished" && localStorage.getItem("aiw_tts") === "1") speak(d.result);
    setTimeout(() => refreshThread(), 400);
  }
  if (!rerenderQ) rerenderQ = requestAnimationFrame(() => { rerenderQ = null; renderChat(); });
}

/* ---------------------------------------------------------------- chat render */
const SUGS = ["打开 Chrome 搜索今天的科技新闻，挑 3 条总结给我", "用 Python 在 ~/workspace 新建 周报.docx，写三条要点，然后发给我", "查一下磁盘空间和内存占用", "打开 Hacker News，把前 5 条标题整理成表格发给我"];
function renderChat(scroll) {
  const box = $("msgs");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  if (!S.thread || !S.thread.tasks.length) {
    box.innerHTML = `<div class="welcome"><div class="avatar" style="background:${AVATAR_BG[S.profile.avatar] || AVATAR_BG.sky}">${avatarSVG()}</div>
      <div class="title">${esc(S.profile.name ? "你好，" + S.profile.name : "想让我做什么？")}</div>
      <div class="muted">我在自己的 Linux 电脑上替你操作浏览器、终端和文件，做完把结果和文件交给你。</div>
      <div class="sugs">${SUGS.map((s) => `<button onclick="usePrompt(this.textContent)">${esc(s)}</button>`).join("")}</div></div>`;
    renderQueue(); setComposerBusy(null); return;
  }
  const head = `<div class="row gap" style="justify-content:flex-end"><button class="btn sm" onclick="renameThread()">重命名</button>
    <button class="btn sm" onclick="archiveThread(${S.thread.archived ? "false" : "true"})">${S.thread.archived ? "取消归档" : "归档"}</button></div>`;
  box.innerHTML = head + S.thread.tasks.filter((t) => t.status !== "cancelled").map(renderTask).join("");
  paintIcons(box);
  renderQueue();
  const live = S.thread.tasks.find((t) => LIVE.includes(t.status) || t.live);
  setComposerBusy(live || null);
  if (live && live.status === "takeover" && !S.take) setTake(true, true);
  if (scroll || nearBottom) scrollLatest();
}
function renderTask(t) {
  const evs = S.evs[t.id] || [];
  const user = `<div class="u"><div><div class="bubble">${esc(t.prompt)}</div><div class="meta">${when(t.created_at)}${t.source && t.source !== "chat" ? " · " + ({ routine: "例程", retry: "重试", resume: "自动恢复" }[t.source] || t.source) : ""}</div></div></div>`;
  if (t.status === "queued") return user + `<div class="a"><div class="small" style="margin-left:8px">排队中，前面的任务完成后自动开始</div></div>`;
  let plan = t.plan && t.plan.steps && t.plan.steps.length ? t.plan : null;
  const steps = [], files = [];
  let cur = null, last = null, startTs = null, endTs = null;
  for (const e of evs) {
    const d = e.data || {};
    if (e.type === "task.started") startTs = e.ts;
    if (e.type === "agent.plan") plan = { steps: d.plan || [], done: d.done || [] };
    if (e.type === "agent.plan_update" && plan) plan = { ...plan, done: d.done || [] };
    if (e.type === "agent.decision") cur = { tool: d.tool, args: d.args, thought: d.thought, ok: null, ms: d.llm_ms, tok: d.out_tokens, tries: d.attempts };
    if (e.type === "agent.action_result" && cur && cur.tool === d.tool) { cur.ok = d.success; cur.err = d.error; if (cur.tool !== "screenshot") steps.push(cur); cur = null; }
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
  let html = `<div class="a"><div class="work">
    <div class="head">${liveNow && t.status === "running" ? '<span class="spin"></span>' : chip(t.status)}
      <div class="grow">${esc(status || (dur != null ? `用时 ${dur}s · ${steps.length} 步` : ""))}</div>
      ${liveNow ? `<button class="btn sm" onclick="ctl('${t.session_id}','${t.status === "paused" || t.status === "takeover" ? "resume" : "pause"}')">${t.status === "paused" || t.status === "takeover" ? "继续" : "暂停"}</button>` : ""}</div>`;
  if (plan) {
    const done = new Set(plan.done || []); const firstOpen = plan.steps.findIndex((_, i) => !done.has(i));
    html += `<div class="plan">${plan.steps.map((s, i) => `<div class="st ${done.has(i) ? "done" : i === firstOpen && liveNow ? "cur" : ""}"><span class="ck">${done.has(i) ? icon("check") : ""}</span><span class="tx">${esc(s)}</span></div>`).join("")}</div>`;
  }
  if (steps.length) {
    html += `<details class="steps" ${liveNow ? "open" : ""}><summary>${icon("chev", "chev")}${steps.length} 个操作</summary><div class="steplist">${steps.slice(-40).map((s) =>
      `<div class="s ${s.ok === false ? "err" : ""} ${s.sub ? "sub" : ""}"><b>${esc(s.tool === "message" ? "说明" : s.tool === "vision" ? "看屏幕" : toolName(s.tool))}</b><span>${esc(stepText(s))}${s.ms ? `<em class="ms">${(s.ms / 1000).toFixed(1)}s${s.tok ? ` · ${s.tok} tok` : ""}${s.tries > 1 ? ` · 重试${s.tries - 1}` : ""}</em>` : ""}</span></div>`).join("")}</div></details>`;
  }
  if (t.pending_question && liveNow) {
    const q = t.pending_question;
    html += `<div class="ask q"><div><b>${esc(S.profile.agent_name || "Agent")} 在问：</b>${esc(q.question)}</div>
      ${q.options && q.options.length ? `<div class="opts">${q.options.map((o) => `<button class="btn sm primary" onclick="answerQ('${t.session_id}', ${esc(JSON.stringify(o))})">${esc(o)}</button>`).join("")}</div>` : ""}
      <form onsubmit="answerQForm(event,'${t.session_id}')"><input name="a" placeholder="回答…" /><button class="btn sm primary">回复</button></form></div>`;
  }
  if (t.pending_confirm && liveNow) {
    const c = t.pending_confirm;
    html += `<div class="ask c"><div><b>需要你确认（L${c.level}）：</b>${esc(c.reason)}</div><div class="small">${esc(toolName(c.tool))} ${esc(JSON.stringify(c.payload).slice(0, 300))}</div>
      <div class="opts"><button class="btn sm primary" onclick="confirmAct('${t.session_id}',true)">允许</button><button class="btn sm" onclick="confirmAct('${t.session_id}',false)">拒绝</button></div></div>`;
  }
  const allFiles = files.length ? files : (t.files || []).map((f) => ({ name: f.path.split("/").pop(), artifact: f.path }));
  for (const f of allFiles) {
    html += `<a class="filecard" href="${artUrl(f.artifact, true)}" download="${esc(f.name)}"><span class="iconbox">${icon("file")}</span><span class="grow"><b>${esc(f.name)}</b><div class="small">${size(f.size)}${f.truncated ? " · 超过 40MB 已截断" : ""} · 点击下载</div></span></a>`;
  }
  if (t.result && !liveNow) html += `<div class="bubble result" style="max-width:100%;background:${t.status === "succeeded" ? "var(--sky)" : "var(--dangerBg)"}">${esc(t.result)}</div>`;
  html += `<div class="foot">`;
  if (liveNow) html += `<button class="btn sm" onclick="go('computer')">${icon("monitor")}看桌面</button><button class="btn sm danger" onclick="ctl('${t.session_id}','stop')">${icon("stop")}停止</button>`;
  if (t.session_id) html += `<button class="btn sm" onclick="replay('${t.session_id}')">${icon("play")}回放</button><button class="btn sm" onclick="receipts('${t.id}')">${icon("receipt")}收据</button>`;
  if (["failed", "stopped", "interrupted"].includes(t.status)) html += `<button class="btn sm primary" onclick="retry('${t.id}')">${icon("retry")}重试</button>`;
  if (!liveNow && t.result && ("speechSynthesis" in window || NATIVE)) html += `<button class="btn sm" onclick="speak(${esc(JSON.stringify(t.result))})">${icon("speaker")}朗读</button>`;
  html += `</div></div></div>`;
  return user + html;
}
function stepText(s) {
  if (s.tool === "message" || s.tool === "vision") return s.thought || "";
  const a = s.args || {};
  let t = a.command || a.url || a.text || a.key || a.name || a.path || a.goal || a.question || (a.x != null ? `(${a.x}, ${a.y})` : "");
  if (!t && s.args) t = JSON.stringify(s.args);
  if (s.ok === false && s.err) t += " — " + s.err;
  return String(t).slice(0, 200);
}
function scrollLatest(smooth) { const b = $("msgs"); b.scrollTo({ top: b.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }
$("msgs").addEventListener("scroll", () => { const b = $("msgs"); $("toLatest").classList.toggle("on", b.scrollHeight - b.scrollTop - b.clientHeight > 300); });

function renderQueue() {
  const q = S.thread ? S.thread.tasks.filter((t) => t.status === "queued") : [];
  $("queueBar").innerHTML = q.map((t) => `<div class="q"><span>排队：${esc(t.prompt)}</span><button class="icon soft" onclick="cancelQueued('${t.id}')" aria-label="移除">${icon("x")}</button></div>`).join("")
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
}
function autoGrow(el) { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 180) + "px"; }
function promptKey(e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 760) { e.preventDefault(); submitPrompt(e); } }
function draftKey() { return "aiw_draft_" + (S.threadId || "new"); }
function saveDraft() { localStorage.setItem(draftKey(), $("prompt").value); }
function loadDraft() { $("prompt").value = localStorage.getItem(draftKey()) || ""; autoGrow($("prompt")); }
function usePrompt(t) { $("prompt").value = t; autoGrow($("prompt")); $("prompt").focus(); }
async function submitPrompt(e) {
  if (e) e.preventDefault();
  let p = $("prompt").value.trim();
  if (!p && busyTask) { ctl(busyTask.session_id, "stop"); return; }   // the button is a stop square while working
  if (!p) return;
  if (S.attach.length) p += "\n\n附件（已放到电脑上）：\n" + S.attach.map((a) => "- " + a.vm_path).join("\n");
  const iso = localStorage.getItem("aiw_iso") === "1";
  try {
    const r = await api("POST", "/api/tasks", { prompt: p, machine_id: MID, thread_id: S.threadId, isolated: iso });
    localStorage.removeItem(draftKey());
    $("prompt").value = ""; autoGrow($("prompt")); S.attach = []; renderAttach();
    if (iso) { localStorage.removeItem("aiw_iso"); $("isoToggle").checked = false; }
    S.threadId = r.thread_id; localStorage.setItem("aiw_thread", r.thread_id);
    if (r.queued) toast("已加入队列");
    await refreshThread(true); loadThreads();
  } catch (err) { toast("发送失败：" + err.message); }
}
async function uploadFiles(list) {
  for (const f of list) {
    if (f.size > 40 * 1048576) { toast(f.name + " 超过 40MB"); continue; }
    toast("上传 " + f.name + "…");
    const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(f); });
    try { S.attach.push(await api("POST", "/api/uploads", { name: f.name, content_b64: b64, machine_id: MID })); renderAttach(); }
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
async function confirmAct(sid, ok) { await api("POST", "/api/agent/confirm", { session_id: sid, approve: ok }); refreshThread(); }
async function answerQ(sid, text) { await api("POST", "/api/agent/answer", { session_id: sid, text }); refreshThread(); }
function answerQForm(e, sid) { e.preventDefault(); const v = e.target.a.value.trim(); if (v) answerQ(sid, v); }
async function retry(id) { const r = await api("POST", `/api/tasks/${id}/retry`); S.threadId = r.thread_id; await refreshThread(true); }
async function cancelQueued(id) { await api("DELETE", `/api/tasks/${id}`); refreshThread(); }
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
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
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
async function checkWatch(id) { const r = await api("POST", `/api/watches/${id}/check`); toast(r.ok ? (r.triggered ? "触发提醒！" : "检查完成，没有新变化") : "失败：" + r.error); loadWatches(); }
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
    const url = d.novnc_url + (TOKEN ? "&token=" + encodeURIComponent(TOKEN) : "");
    const target = S.view === "computer" ? "big" : "small";
    const el = target === "big" ? $("deskBig") : $("desk");
    if (deskLoaded[target] !== url) { el.src = url; deskLoaded[target] = url; }
    if (target === "big") { $("desk").src = "about:blank"; deskLoaded.small = null; } else if (S.view !== "computer") { $("deskBig").src = "about:blank"; deskLoaded.big = null; }
  } catch (e) { toast("桌面：" + e.message); }
  $("deskMode").textContent = S.take ? "你正在操作桌面 · 鼠标键盘已接通" : "观看模式 · 只看不操作";
}
function liveTask() { return busyTask || (S.thread && S.thread.tasks.find((t) => LIVE.includes(t.status))); }
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
async function vmOp(op) { if (op !== "start" && !confirm("确定" + { stop: "关机", restart: "重启" }[op] + "？")) return; try { await api("POST", `/api/machines/${MID}/${op}`); toast("已发送"); refreshMachine(); } catch (e) { toast(e.message); } }
async function snapshot() { const n = prompt("快照名称（字母开头，字母数字-_）", "snap" + new Date().toISOString().slice(0, 10).replace(/-/g, "")); if (!n) return; try { await api("POST", `/api/machines/${MID}/snapshot`, { name: n }); toast("快照已创建"); } catch (e) { toast(e.message); } }
async function refreshMachine() {
  try {
    const m = await api("GET", `/api/machines/${MID}`);
    const up = !!(m.agent && m.agent.ok);
    S.agentFails = up ? 0 : S.agentFails + 1;
    if (up || S.agentFails >= 2) { const p = $("pillAgent"); p.textContent = up ? "Agent 在线" : "Agent 离线"; p.className = "chip " + (up ? "ok" : "bad"); p.title = up ? "" : (m.agent && m.agent.error) || ""; }
    const vm = m.vm ? (m.vm.state === "running" ? "运行中" : m.vm.state) : m.vm_error ? "未连接 PVE" : m.status;
    $("pcTitle").textContent = (up ? "电脑已连接" : "电脑未连接") + " · " + MID;
    $("pcSub").textContent = `VM ${vm}${m.vm && m.vm.ip ? " · " + m.vm.ip : ""}${m.vm && m.vm.mem_bytes ? " · " + Math.round(m.vm.mem_bytes / 1073741824) + " GB" : ""} · 浏览器、终端、文件和完整桌面`;
  } catch (e) { $("pillAgent").textContent = "API 错误"; $("pillAgent").className = "chip bad"; }
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
  ws.onmessage = (m) => {
    const e = JSON.parse(m.data);
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
  const [prof, mems, models, notify, integ, secs] = await Promise.all([api("GET", "/api/profile"), api("GET", "/api/memories"), api("GET", "/api/settings/models"),
    api("GET", "/api/settings/notify"), api("GET", "/api/integrations"), api("GET", "/api/secrets")]);
  S.profile = prof;
  const av = (k) => `<button type="button" class="avatar" style="background:${AVATAR_BG[k]};${(prof.avatar || "sky") === k ? "outline:2px solid var(--blueDark)" : ""}" onclick="this.form.avatar.value='${k}';[...this.parentNode.children].forEach(b=>b.style.outline='');this.style.outline='2px solid var(--blueDark)'">${avatarSVG()}</button>`;
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
    <span class="muted">头像底色</span><div class="row gap">${av("sky")}${av("sand")}${av("lilac")}</div></div>
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

  <form class="card sec" id="sNotify" onsubmit="saveNotify(event)"><h3>${icon("bell")} 手机推送</h3>
    <div class="muted">任务完成、失败、需要确认或回答、追踪触发时推送。iPhone 推荐 Bark。</div>
    <div class="kv"><span class="muted">Bark 地址</span><input name="bark_url" value="${esc(notify.bark_url || "")}" placeholder="https://api.day.app/你的key" />
    <span class="muted">ntfy 主题</span><input name="ntfy_url" value="${esc(notify.ntfy_url || "")}" placeholder="https://ntfy.sh/aiw-xxxx" />
    <span class="muted">Webhook</span><input name="webhook_url" value="${esc(notify.webhook_url || "")}" placeholder="POST {title, body}" /></div>
    <div class="row gap wrap"><button class="btn primary">保存</button><button type="button" class="btn" onclick="api('POST','/api/settings/notify/test').then(()=>toast('已发送测试通知'))">发送测试</button>
    <button type="button" class="btn" onclick="Notification.requestPermission().then(p=>toast('浏览器通知：'+p))">允许浏览器通知</button></div></form>

  <div class="card sec" id="sInteg"><h3>${icon("plug")} 集成</h3>
    <div class="muted">让 Agent 直接调 API，而不是点网页。MCP 服务器（Streamable HTTP）、OpenAPI 规范或自定义 HTTP 工具。请求头里可以用 {{secret:名字}} 引用下方加密保存的密钥。</div>
    <div class="label">MCP 服务器</div>
    <div class="list">${(integ.mcp_servers || []).map((m, i) => `<div class="item"><div class="grow"><b>${esc(m.name)}</b><div class="small">${esc(m.url)}</div></div><div class="acts"><button class="btn sm danger" onclick="delMcp(${i})">删除</button></div></div>`).join("") || `<div class="small">没有</div>`}</div>
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
  $("agentName").textContent = S.profile.agent_name || "AI Workstation";
  $("sideAvatar").innerHTML = avatarSVG(); $("sideAvatar").style.background = AVATAR_BG[S.profile.avatar] || AVATAR_BG.sky;
}
async function saveProfile(e) { e.preventDefault(); const f = e.target; S.profile = await api("PUT", "/api/profile", { name: f.name.value, agent_name: f.agent_name.value, tone: f.tone.value, about: f.about.value, avatar: f.avatar.value }); applyProfile(); toast("已保存"); }
async function addMem(e) { e.preventDefault(); const v = e.target.t.value.trim(); if (!v) return; await api("POST", "/api/memories", { text: v }); loadSettings(); }
async function editMem(m) { const v = prompt("编辑记忆", m.text); if (v === null) return; await api("PUT", `/api/memories/${m.id}`, { text: v }); loadSettings(); }
const MODEL_ROLES = [
  { key: "model", label: "主模型", hint: "决定操作，需能调用工具", need: "tools" },
  { key: "vision_model", label: "视觉模型", hint: "可选，主模型不会看图时设置", need: "vision" },
  { key: "fallback", label: "备用模型", hint: "主模型出错时用", need: "tools" },
  { key: "subagent_model", label: "子 Agent 模型", hint: "纯文本命令行任务", need: "tools" },
];
S.modelIds = []; S.probes = {};
async function fetchModels(manual) {
  const note = $("modelListNote"); if (note) note.textContent = "正在读取…";
  const [r, probes] = await Promise.all([api("GET", "/api/models").catch((e) => ({ models: [], error: e.message })), api("GET", "/api/models/probes").catch(() => ({}))]);
  S.modelIds = r.models || []; S.probes = probes || {};
  if (note) note.textContent = r.error ? "读取失败：" + r.error.slice(0, 80) : `网关有 ${S.modelIds.length} 个模型`;
  document.querySelectorAll("#sModel .mrow select").forEach((sel) => {
    const cur = sel.value || sel.dataset.cur || "";
    const ids = [...new Set([...(cur ? [cur] : []), ...S.modelIds])];
    const role = MODEL_ROLES.find((x) => x.key === sel.name);
    sel.innerHTML = `<option value="">${role.key === "model" ? "默认（.env）" : role.key === "vision_model" ? "不用（主模型自己看图）" : role.key === "fallback" ? "不用" : "同主模型"}</option>`
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
async function probeModel(btn) {
  const sel = btn.parentNode.querySelector("select"); if (!sel.value) return toast("先选一个模型");
  const box = btn.parentNode.querySelector(".probe"); box.innerHTML = `<span class="spin"></span> 正在实测…`; btn.disabled = true;
  try {
    const base = document.querySelector("#sModel [name=base_url]").value.trim();
    const r = await api("POST", "/api/models/probe", { model: sel.value, base_url: base || undefined });
    if (r.error) { box.textContent = r.error; return; }
    S.probes[sel.value] = r;
    document.querySelectorAll("#sModel .mrow select").forEach((s2) => { [...s2.options].forEach((o) => { if (o.value && o.value !== "__custom") o.textContent = o.value + probeTag(o.value); }); paintProbe(s2); });
  } catch (e) { box.textContent = "探测失败：" + e.message; } finally { btn.disabled = false; }
}
async function saveModels(e) {
  e.preventDefault(); const f = e.target;
  if (f.key.value) await api("PUT", "/api/secrets/openai_api_key", { value: f.key.value });
  const v = (n) => (f[n].value === "__custom" ? "" : f[n].value);
  await api("PUT", "/api/settings/models", { model: v("model"), vision_model: v("vision_model"), fallback: v("fallback"), subagent_model: v("subagent_model"), base_url: f.base_url.value, thinking: f.thinking.value });
  toast("已保存，下个任务生效"); loadSettings();
}
async function saveNotify(e) { e.preventDefault(); const f = e.target; await api("PUT", "/api/settings/notify", { bark_url: f.bark_url.value, ntfy_url: f.ntfy_url.value, webhook_url: f.webhook_url.value }); toast("已保存"); }
async function addMcp(e) {
  e.preventDefault(); const f = e.target; const cur = (await api("GET", "/api/integrations")).mcp_servers || [];
  cur.push({ name: f.name.value, url: f.url.value, headers: f.hk.value ? { [f.hk.value]: f.hv.value } : {} });
  try { await api("PUT", "/api/integrations", { mcp_servers: cur }); toast("已添加"); loadSettings(); } catch (err) { toast(err.message); }
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
go("chat"); loadThreads(); refreshThread(true); loadDraft(); loadDesktop(); refreshMachine(); loadNotes(); connectEvents();
setInterval(refreshMachine, 10000);
setInterval(() => { if (busyTask && S.view === "chat") renderChat(); }, 5000);
if (NATIVE) { document.body.classList.add("native"); NATIVE.postMessage({ op: "ready" }); }
