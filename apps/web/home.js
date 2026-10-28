/* Aiw home (2026-10-28): the main screen is ONE persistent chat with 球球 (server: /api/chat*, services/api/aiw_api/chat.py).
   * history loads on open, scrolled to the bottom; older pages load when you scroll up (GET /api/chat?before=seq)
   * replies stream over SSE (POST /api/chat/send); no thinking and no agent steps are ever shown here
   * a task shows as a compact card (title, status, step) that follows the task live (/ws/events kind "chat");
     tapping it opens the existing task view (app.js "chat" view: steps in the middle, the computer on the right);
     ← returns here at the same scroll position
   * questions / confirmations from a task come in as messages with buttons; 我来接管桌面 opens the task view in takeover
   * an assistant reply that needs the computer carries a 交给电脑去做 button (POST /api/chat/messages/{id}/to_task)
   * 新建任务 (top right) sends the next message straight to the computer (mode "task")
   * the sidebar conversation list became the 任务 page (every task, status, retry, replay)
   Loaded last: wraps go()/newThread() from app.js; the task view itself is untouched. */
"use strict";

const HS = { msgs: [], hasMore: false, loading: false, loaded: false, stream: null, mode: "auto", attach: [], scroll: null,
  sending: false, agent: "", lastRead: 0, tasks: [], tkFilter: "all", archived: [] };
const hBox = () => $("hmsgs");
const hTime = (ts) => { const d = new Date(ts * 1000), n = new Date();
  return d.toDateString() === n.toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : when(ts); };
const hMd = (t) => (window.AiwMd ? AiwMd.render(t || "", "md") : esc(t || "").replace(/\n/g, "<br>"));
const hFind = (id) => HS.msgs.find((m) => m.id === id);

/* ---------------------------------------------------------------- render */
function hFiles(files) {
  if (!files || !files.length) return "";
  const pics = files.filter((f) => f.kind === "image");
  let h = pics.length ? gallery(pics.map((f) => ({ src: artUrl(f.path), dl: artUrl(f.path, true), name: f.name }))) : "";
  for (const f of files) if (f.kind !== "image")
    h += `<a class="filecard" href="${artUrl(f.path, true)}" download="${esc(f.name)}"><span class="iconbox">${icon("file")}</span><span class="grow"><b>${esc(f.name)}</b><div class="small">点击下载</div></span></a>`;
  return h;
}
function hTaskCard(m) {
  const d = m.data || {};
  const st = d.status || "running", live = LIVE.includes(st) || st === "queued";
  const plan = d.plan_total ? `<div class="hc-bar"><i style="width:${Math.round(100 * (d.plan_done || 0) / d.plan_total)}%"></i></div>` : "";
  const step = d.step ? `第 ${d.step}${d.max_steps ? "/" + d.max_steps : ""} 步` : "";
  const sub = [d.status_line && d.status_line !== (STATUS_CN[st] || [])[0] ? d.status_line : "", d.plan_total ? `${d.plan_done || 0}/${d.plan_total} 项` : "", live ? step : ""].filter(Boolean).join(" · ");
  const retry = ["failed", "stopped", "interrupted"].includes(st) && d.task_id
    ? `<button class="btn xs" onclick="event.stopPropagation();hRetry('${esc(d.task_id)}')">${icon("retry")}重试</button>` : "";
  return `<div class="a hrow" data-id="${m.id}"><div class="hcard st-${esc(st)}" role="button" tabindex="0" onclick="hOpenTask('${m.id}')">
    <div class="hc-ic">${live && st === "running" ? '<span class="spin"></span>' : icon("monitor")}</div>
    <div class="grow"><div class="hc-t">${esc(d.title || m.text || "任务")}</div><div class="hc-s">${esc(sub || "交给电脑的任务")}</div>${plan}</div>
    <div class="hc-r">${chip(st)}${retry}<span class="hc-go">${icon("chev")}</span></div></div></div>`;
}
function hAsk(m) {
  const d = m.data || {}, k = d.ask_kind || "question";
  const done = d.resolved, stale = d.stale && !done;
  let body = `<div class="hq-k">${k === "takeover" ? "需要你接管" : k === "form" ? "需要你填写" : k === "escalate" ? "任务卡住了" : "任务在问你"}${d.title ? " · " + esc(d.title) : ""}</div>
    <div class="hq-q">${esc(m.text || d.question || "")}</div>`;
  if (k === "takeover" && d.takeover && (d.takeover.steps || d.takeover.reason)) body += `<div class="small hq-d">${esc(d.takeover.steps || d.takeover.reason)}</div>`;
  if (done) {
    const a = done.answer;
    body += `<div class="hq-done">${icon("check")}${esc(a === true ? "已允许" : a === false ? "已拒绝" : a === "done" ? "已完成，交还给电脑" : "已回答：" + String(a).slice(0, 80))}</div>`;
  } else if (stale) {
    body += `<div class="hq-done muted">已过期（任务已经不在等这个回答）</div>`;
  } else if (k === "takeover") {
    body += `<div class="opts"><button class="btn sm primary" onclick="hTakeover('${m.id}')">${icon("hand")}接管桌面</button>
      <button class="btn sm" onclick="hAnswer('${m.id}', {choice: 'done'})">完成，继续</button></div>`;
  } else if (k === "form") {
    body += `<div class="opts"><button class="btn sm primary" onclick="hForm('${m.id}')">去填写</button></div>`;
  } else {
    const opts = d.options || [];
    if (opts.length) body += `<div class="opts">${opts.map((o, i) => `<button class="btn sm ${i === 0 ? "primary" : ""}" onclick='hAnswer("${m.id}", {choice: ${esc(JSON.stringify(o))}})'>${esc(o)}</button>`).join("")}</div>`;
    body += `<form class="hq-f" onsubmit="hAnswerText(event,'${m.id}')"><input name="a" placeholder="${opts.length ? "或者直接告诉它…" : "回答…"}" autocomplete="off" /><button class="btn sm primary">回复</button></form>`;
  }
  return `<div class="a hrow" data-id="${m.id}"><div class="hq ${done || stale ? "off" : ""}">${body}
    ${d.thread_id ? `<button class="link small hq-open" onclick="hOpenTask('${m.id}')">查看任务 ›</button>` : ""}</div></div>`;
}
function hConfirm(m) {
  const d = m.data || {}, s = d.summary || {}, done = d.resolved, stale = d.stale && !done;
  let body = `<div class="hq-k">需要你确认${d.title ? " · " + esc(d.title) : ""}</div><div class="hq-q">${esc(s.title || m.text || "")}</div>`;
  if (s.what) body += `<div class="small hq-d">${esc(s.what)}</div>`;
  if (s.risk_cn || s.why) body += `<div class="small hq-d">${esc([s.risk_cn, s.why].filter(Boolean).join(" · "))}</div>`;
  if (done) body += `<div class="hq-done">${icon("check")}${done.answer ? "已允许" : "已拒绝"}</div>`;
  else if (stale) body += `<div class="hq-done muted">已过期（任务已经不在等这个确认）</div>`;
  else body += `<div class="opts"><button class="btn sm primary" onclick="hAnswer('${m.id}', {approve: true})">允许</button><button class="btn sm" onclick="hAnswer('${m.id}', {approve: false})">不允许</button></div>`;
  return `<div class="a hrow" data-id="${m.id}"><div class="hq confirm ${done || stale ? "off" : ""}">${body}
    ${d.thread_id ? `<button class="link small hq-open" onclick="hOpenTask('${m.id}')">查看任务 ›</button>` : ""}</div></div>`;
}
function hMsg(m) {
  const d = m.data || {};
  if (m.role === "user") {
    const att = (d.attachments || []).map((a) => `<span class="chip">${icon("file")}${esc(a.name || "")}</span>`).join("");
    return `<div class="u hrow" data-id="${m.id}"><div><div class="bubble">${esc(m.text)}</div>${att ? `<div class="hatt">${att}</div>` : ""}<div class="meta">${m.tmp ? "发送中…" : hTime(m.created_at)}</div></div></div>`;
  }
  if (m.kind === "task") return hTaskCard(m);
  if (m.kind === "ask") return hAsk(m);
  if (m.kind === "confirm") return hConfirm(m);
  if (m.kind === "task_result") {
    const ok = d.status === "succeeded";
    return `<div class="a hrow" data-id="${m.id}"><div class="hres ${ok ? "ok" : "bad"}"><div class="bubble hmd">${hMd(m.text)}</div>${hFiles(d.files)}
      <div class="meta">${hTime(m.created_at)}${d.thread_id ? ` · <button class="link small" onclick="hOpenTask('${m.id}')">${d.has_more ? "查看完整结果" : "查看任务"} ›</button>` : ""}</div></div></div>`;
  }
  // assistant text
  let offer = "";
  if (d.offer_task) offer = d.task_started
    ? `<div class="opts"><button class="btn sm" disabled>${icon("check")}已交给电脑</button></div>`
    : `<div class="opts"><button class="btn sm primary" onclick="hToTask('${m.id}', this)">${icon("monitor")}交给电脑去做</button></div>`;
  return `<div class="a hrow" data-id="${m.id}"><div class="hbody"><div class="bubble hmd ${d.error ? "err" : ""}">${hMd(m.text)}</div>${offer}<div class="meta">${m.stream ? "" : hTime(m.created_at)}</div></div></div>`;
}
function hEmpty() {
  return `<div class="welcome hwelcome"><div class="avatar" style="background:${AVATAR_BG[(S.profile || {}).avatar] || AVATAR_BG.amber}">${avatarSVG()}</div>
    <div class="title">${esc((S.profile || {}).name ? "你好，" + S.profile.name : "我是" + (HS.agent || "小秘"))}</div>
    <div class="muted">随便聊，问我问题；要在电脑上动手的事我会交给电脑去做，进度会以卡片出现在这里。</div>
    <div class="sugs">${["今天有什么值得关注的科技新闻？", "帮我在电脑上打开 Hacker News，把前 5 条整理成表格发给我", "我有哪些任务没完成？"].map((s) => `<button onclick="hUse(this.textContent)">${esc(s)}</button>`).join("")}</div></div>`;
}
function hRender(keep) {
  const box = hBox(); if (!box) return;
  const list = HS.msgs.slice();
  if (HS.stream) list.push(HS.stream);
  const top = HS.hasMore ? `<div class="hmore">${HS.loading ? "加载中…" : "上滑加载更早的消息"}</div>` : (list.length ? `<div class="hmore end">· 聊天从这里开始 ·</div>` : "");
  box.innerHTML = list.length ? top + list.map(hMsg).join("") : hEmpty();
  paintIcons(box);
  if (keep === "bottom") hScrollLatest();
}
function hPatch(m) {   // one message changed: swap its node in place (no scroll jump, pictures stay loaded)
  const box = hBox(); if (!box) return;
  const el = box.querySelector(`[data-id="${CSS.escape(m.id)}"]`);
  if (!el) return false;
  const tpl = document.createElement("div"); tpl.innerHTML = hMsg(m); paintIcons(tpl);
  el.replaceWith(tpl.firstElementChild);
  return true;
}
function hNear() { const b = hBox(); return b.scrollHeight - b.scrollTop - b.clientHeight < 160; }
function hScrollLatest(smooth) { const b = hBox(); if (b) b.scrollTo({ top: b.scrollHeight, behavior: smooth ? "smooth" : "auto" }); }

/* ---------------------------------------------------------------- data */
function hUpsert(m, opts = {}) {
  if (!m || !m.id) return;
  const near = hNear();
  const i = HS.msgs.findIndex((x) => x.id === m.id);
  if (i >= 0) { HS.msgs[i] = m; if (!hPatch(m)) hRender(); return; }
  // a real message replaces the optimistic one it stands for
  if (m.role === "user") HS.msgs = HS.msgs.filter((x) => !(x.tmp && x.text === m.text));
  HS.msgs.push(m); HS.msgs.sort((a, b) => (a.seq || 1e15) - (b.seq || 1e15));
  if (m.role !== "user" && HS.stream && m.kind === "text") HS.stream = null;
  hRender();
  if (near || opts.stick || m.role === "user") hScrollLatest(); else if (m.role !== "user") hUnread(true);
  hMarkRead();
}
async function hLoad() {
  try {
    const r = await api("GET", "/api/chat?limit=60");
    HS.msgs = r.messages || []; HS.hasMore = !!r.has_more; HS.agent = r.agent_name || HS.agent; HS.loaded = true;
    HS.lastRead = (r.read || {}).seq || 0;
    hRender("bottom"); hTitle(); hMarkRead();
    setTimeout(hScrollLatest, 60); setTimeout(hScrollLatest, 400);   // pictures and fonts settle the height late
  } catch (e) {
    hBox().innerHTML = `<div class="empty">聊天加载失败：${esc(e.message)}<br><button class="btn sm" onclick="hLoad()">再试一次</button></div>`;
  }
}
async function hOlder() {
  if (HS.loading || !HS.hasMore || !HS.msgs.length) return;
  HS.loading = true;
  const box = hBox(), h0 = box.scrollHeight, t0 = box.scrollTop;
  try {
    const first = HS.msgs.find((m) => m.seq);
    const r = await api("GET", `/api/chat?limit=40&before=${first.seq}`);
    const have = new Set(HS.msgs.map((m) => m.id));
    HS.msgs = (r.messages || []).filter((m) => !have.has(m.id)).concat(HS.msgs);
    HS.hasMore = !!r.has_more;
  } catch (e) { toast("加载更早的消息失败：" + e.message); }
  HS.loading = false;
  hRender();
  box.scrollTop = box.scrollHeight - h0 + t0;   // keep what you were reading where it was
}
async function hSync() {   // after a reconnect: pick up what came in while the socket was down
  if (!HS.loaded) return;
  try {
    const r = await api("GET", "/api/chat?limit=60");
    const near = hNear();
    for (const m of r.messages || []) { const i = HS.msgs.findIndex((x) => x.id === m.id); if (i >= 0) HS.msgs[i] = m; else HS.msgs.push(m); }
    HS.msgs.sort((a, b) => (a.seq || 1e15) - (b.seq || 1e15));
    hRender(); if (near) hScrollLatest();
  } catch (e) { /* the next reconnect tries again */ }
}
let hReadT = null;
function hMarkRead() {
  if (S.view !== "home" || document.hidden) return;
  const last = [...HS.msgs].reverse().find((m) => m.seq);
  if (!last || last.seq <= HS.lastRead) { hUnread(false); return; }
  clearTimeout(hReadT);
  hReadT = setTimeout(() => { HS.lastRead = last.seq; api("POST", "/api/chat/read", { seq: last.seq }).catch(() => {}); hUnread(false); }, 600);
}
function hUnread(on) {
  const n = on ? HS.msgs.filter((m) => m.seq > HS.lastRead && m.role !== "user").length : 0;
  const b = $("chatBadge"); if (b) b.textContent = n && S.view !== "home" ? n : "";
  $("hToLatest").classList.toggle("on", !!on && S.view === "home" && !hNear());
}
function hTitle() { if (S.view === "home") { $("viewTitle").textContent = HS.agent || (S.profile || {}).agent_name || "小秘"; $("viewSub").textContent = ""; } }

window.onAiwEvent = (e) => {
  if (e.kind === "chat" && e.message) {
    hUpsert(e.message);
    if (S.view !== "home" && e.op === "new" && e.message.role !== "user") hUnread(true);
  }
  if ((e.kind === "task" || e.type === "task.started") && S.view === "tasks") loadTasksPage();
};
window.onAiwEventsOpen = () => hSync();

/* ---------------------------------------------------------------- send (SSE) */
function hKey(e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 760) { e.preventDefault(); hSend(e); } }
function hUse(t) { $("hprompt").value = t; autoGrow($("hprompt")); $("hprompt").focus(); }
function hStreamShow(text, waiting) {
  HS.stream = { id: "stream", role: "assistant", kind: "text", text, data: {}, stream: true };
  const near = hNear();
  if (!hPatch(HS.stream)) hRender();
  const el = hBox().querySelector('[data-id="stream"] .bubble');
  if (el && waiting) el.innerHTML = `<span class="hdots"><i></i><i></i><i></i></span>${waiting === true ? "" : `<span class="small">${esc(waiting)}</span>`}`;
  if (near) hScrollLatest();
}
async function hSend(e) {
  if (e) e.preventDefault();
  if (HS.sending) return;
  const box = $("hprompt"), raw = box.value, text = raw.trim();
  if (!text && !HS.attach.length) return;
  const attachments = HS.attach.slice(), mode = HS.mode;
  HS.sending = true; $("hSendBtn").classList.add("busy");
  box.value = ""; autoGrow(box); localStorage.removeItem("aiw_hdraft"); HS.attach = []; hRenderAttach(); hSetMode("auto");
  hUpsert({ id: "tmp-" + Date.now(), role: "user", kind: "text", text: text || "（附件）", data: { attachments }, tmp: true }, { stick: true });
  hStreamShow("", true);
  let gotReply = false;
  try {
    const res = await fetch("/api/chat/send", { method: "POST", headers: H(), body: JSON.stringify({ text, attachments, machine_id: MID, mode, stream: true }) });
    if (!res.ok) throw new Error((await res.text()).slice(0, 200) || res.status);
    const rd = res.body.getReader(), dec = new TextDecoder();
    let buf = "", acc = "";
    for (;;) {
      const { value, done } = await rd.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
        let ev = "message", data = "";
        for (const line of chunk.split("\n")) {
          if (line.startsWith("event:")) ev = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        let d; try { d = JSON.parse(data); } catch (err) { continue; }
        if (ev === "user") hUpsert(d, { stick: true });
        else if (ev === "route") { if (d.route === "task") hStreamShow("", "正在交给电脑…"); }
        else if (ev === "delta") { acc += d.text || ""; hStreamShow(acc); }
        else if (ev === "message" || ev === "task") { gotReply = true; HS.stream = null; hUpsert(d, { stick: true }); }
      }
    }
  } catch (err) {
    toast("发送失败：" + err.message);
    HS.msgs = HS.msgs.filter((m) => !m.tmp);
    if (!$("hprompt").value) { box.value = raw; autoGrow(box); }
    HS.attach = attachments; hRenderAttach();
  } finally {
    HS.stream = null; HS.sending = false; $("hSendBtn").classList.remove("busy");
    hRender(); if (gotReply) hScrollLatest();
  }
}
async function hUpload(list) {
  for (const f of list) {
    if (f.size > 40 * 1048576) { toast(f.name + " 超过 40MB"); continue; }
    toast("上传 " + f.name + "…");
    const b64 = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1]); r.onerror = rej; r.readAsDataURL(f); });
    try { HS.attach.push(await api("POST", "/api/uploads", { name: f.name, content_b64: b64, machine_id: MID, local_only: true })); hRenderAttach(); }
    catch (e) { toast("上传失败：" + e.message); }
  }
  $("hfile").value = "";
}
function hRenderAttach() {
  $("hattach").innerHTML = HS.attach.map((a, i) => `<span class="chip">${icon("file")}${esc(a.name)}<button class="link" onclick="HS.attach.splice(${i},1);hRenderAttach()">${icon("x")}</button></span>`).join("");
}
function hSetMode(m) {
  HS.mode = m;
  const el = $("hmode");
  el.hidden = m !== "task";
  el.innerHTML = m === "task" ? `${icon("monitor")}<span>新建任务：这条消息直接交给电脑去做</span><button class="link" onclick="hSetMode('auto')" aria-label="取消">${icon("x")}</button>` : "";
  $("hprompt").placeholder = m === "task" ? "要电脑做什么？" : "和我聊聊，或者让电脑去办件事…";
}
function newTaskMode() { go("home"); hSetMode("task"); setTimeout(() => $("hprompt").focus(), 30); }

/* ---------------------------------------------------------------- actions */
function hOpenTask(mid) {
  const m = hFind(mid); const d = (m && m.data) || {};
  if (!d.thread_id) { toast("这个任务还没有开始"); return Promise.resolve(); }
  HS.scroll = hBox().scrollTop; HS.fromHome = true;
  return openThread(d.thread_id);
}
function backToChat() { go("home"); }
async function hAnswer(mid, body) {
  const m = hFind(mid);
  try {
    const r = await api("POST", `/api/chat/messages/${mid}/answer`, body);
    if (r.message) hUpsert(r.message);
    if (r.open_task) { await hOpenTask(mid); hTakeLater(); }
  } catch (e) { toast(e.message); if (m) hSync(); }
}
function hAnswerText(e, mid) { e.preventDefault(); const v = e.target.a.value.trim(); if (v) hAnswer(mid, { text: v }); }
function hTakeLater() {   // the task view in takeover mode: interactive desktop, the takeover card's 完成 button below
  let n = 0;
  const t = setInterval(() => { n++; if (S.thread || n > 20) { clearInterval(t); if (!S.take) { setTake(true, true); toast("桌面已接通：直接在电脑画面上操作，做完点「完成，继续」"); } } }, 150);
}
async function hTakeover(mid) { await hOpenTask(mid); hTakeLater(); }
async function hForm(mid) {
  const m = hFind(mid); const sid = ((m && m.data) || {}).session_id;
  await hOpenTask(mid);
  let n = 0;
  const t = setInterval(() => { n++; if ((S.forms || {})[sid] || n > 30) { clearInterval(t); if ((S.forms || {})[sid]) openForm(sid); } }, 150);
}
async function hToTask(mid, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "交给电脑…"; }
  try { const card = await api("POST", `/api/chat/messages/${mid}/to_task`, { machine_id: MID }); hUpsert(card, { stick: true });
    const m = hFind(mid); if (m) hUpsert({ ...m, data: { ...m.data, task_started: card.id } }); }
  catch (e) { toast("没能交给电脑：" + e.message); if (btn) { btn.disabled = false; btn.innerHTML = icon("monitor") + "交给电脑去做"; } }
}
async function hRetry(taskId) {
  try { await api("POST", `/api/tasks/${taskId}/retry`); toast("已重新开始"); } catch (e) { toast("重试失败：" + e.message); }
}

/* ---------------------------------------------------------------- 任务 page */
async function loadTasksPage() {
  try {
    if (HS.tkFilter === "arch") HS.archived = await api("GET", "/api/threads?archived=true");
    else HS.tasks = await api("GET", "/api/tasks?limit=200");
  } catch (e) { $("tkList").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; return; }
  renderTasksPage();
}
function renderTasksPage() {
  const q = ($("tkSearch").value || "").trim();
  if (HS.tkFilter === "arch") {
    const rows = HS.archived.filter((t) => !q || (t.title || "").includes(q));
    $("tkList").innerHTML = rows.map((t) => `<div class="item"><div class="iconbox sm">${icon("bolt")}</div><div class="grow" style="cursor:pointer" onclick="hOpenThread('${t.id}')">
      <div><b>${esc(t.title)}</b></div><div class="small">${t.last_status ? (STATUS_CN[t.last_status] || [t.last_status])[0] + " · " : ""}${ago(t.updated_at)}</div></div>
      <div class="acts"><button class="btn sm" onclick="tkUnarchive('${t.id}')">取消归档</button><button class="btn sm danger" onclick="deleteThread('${t.id}').then(loadTasksPage)">删除</button></div></div>`).join("")
      || `<div class="empty">没有归档的任务</div>`;
    return;
  }
  // one row per conversation: its newest task (a retry or follow-up continues the same task)
  const seen = new Set(), rows = [];
  for (const t of HS.tasks) { const k = t.thread_id || t.id; if (seen.has(k)) continue; seen.add(k); rows.push(t); }
  const f = HS.tkFilter;
  const list = rows.filter((t) => (f === "all" || (f === "live" && (LIVE.includes(t.status) || t.status === "queued"))
    || (f === "need" && ["waiting_confirm", "waiting_input"].includes(t.status)) || (f === "failed" && ["failed", "stopped", "interrupted"].includes(t.status))
    || (f === "done" && t.status === "succeeded")) && (!q || (t.prompt || "").includes(q) || (t.thread_title || "").includes(q)));
  $("tkList").innerHTML = list.map((t) => {
    const p = t.plan && t.plan.steps ? `${(t.plan.done || []).length}/${t.plan.steps.length} 项` : "";
    const title = (t.thread_title && t.thread_title !== "新对话" ? t.thread_title : t.prompt || "").replace(/【技能：[^】]*】/g, "");
    return `<div class="item tk"><div class="iconbox sm">${icon(t.source === "routine" ? "clock" : t.source === "chat" ? "chat" : "bolt")}</div>
      <div class="grow" style="cursor:pointer" onclick="hOpenThread('${t.thread_id}')">
        <div><b>${esc(title.slice(0, 80))}</b></div><div class="small">${when(t.created_at)}${p ? " · " + p : ""}${t.steps ? " · " + t.steps + " 步" : ""}</div>
        ${t.result ? `<div class="muted tk-res">${esc(t.result.slice(0, 140))}</div>` : ""}</div>
      <div class="acts">${chip(t.status)}
        ${["failed", "stopped", "interrupted"].includes(t.status) ? `<button class="btn sm primary" onclick="tkRetry('${t.id}')">${icon("retry")}重试</button>` : ""}
        ${t.session_id && !LIVE.includes(t.status) ? `<button class="btn sm" onclick="replay('${t.session_id}')">${icon("play")}回放</button>` : ""}
        ${t.status === "queued" ? `<button class="btn sm" onclick="cancelQueued('${t.id}').then(loadTasksPage)">取消</button>` : ""}
        ${t.thread_id ? `<button class="icon sm" title="删除" onclick="deleteThread('${t.thread_id}').then(loadTasksPage)">${icon("trash")}</button>` : ""}</div></div>`;
  }).join("") || `<div class="empty">${q ? "没有匹配的任务" : "还没有任务。在聊天里让我去电脑上办件事，或点右上角的 新建任务。"}</div>`;
}
function hOpenThread(id) { HS.fromHome = false; return openThread(id); }
async function tkRetry(id) { try { const r = await api("POST", `/api/tasks/${id}/retry`); toast("已重新开始"); hOpenThread(r.thread_id); } catch (e) { toast("重试失败：" + e.message); } }
async function tkUnarchive(id) { await api("PATCH", `/api/threads/${id}`, { archived: false }); toast("已恢复"); loadTasksPage(); }
$("tkFilter").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; HS.tkFilter = b.dataset.f;
  $("tkFilter").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); loadTasksPage(); });

/* ---------------------------------------------------------------- navigation */
TITLES.home = "聊天"; TITLES.tasks = "任务"; TITLES.chat = "任务";
const _goHome = go;
go = function (view) {
  const from = S.view;
  if (from === "home" && view !== "home" && hBox()) HS.scroll = hBox().scrollTop;
  const r = _goHome.apply(this, arguments);
  $("backBtn").hidden = view !== "chat";
  $("menuBtn").classList.toggle("hide-back", view === "chat");
  $("newTaskBtn").hidden = view === "chat";
  document.querySelectorAll('[data-view="tasks"]').forEach((b) => b.classList.toggle("on", view === "tasks" || (view === "chat" && !HS.fromHome)));
  document.querySelectorAll('[data-view="home"]').forEach((b) => b.classList.toggle("on", view === "home" || (view === "chat" && HS.fromHome)));
  if (view === "home") {
    hTitle();
    if (!HS.loaded) hLoad();
    else if (HS.scroll != null) requestAnimationFrame(() => { hBox().scrollTop = HS.scroll; });
    hMarkRead(); hUnread(false);
  }
  if (view === "tasks") loadTasksPage();
  return r;
};
backToChat = function () { go(HS.fromHome === false ? "tasks" : "home"); };
// deleting / archiving the open task used to start an empty conversation: now it goes back where it came from
newThread = function () { S.threadId = null; S.thread = null; localStorage.removeItem("aiw_thread"); backToChat(); };
hBox().addEventListener("scroll", () => {
  const b = hBox();
  if (b.scrollTop < 80) hOlder();
  $("hToLatest").classList.toggle("on", b.scrollHeight - b.scrollTop - b.clientHeight > 300);
  if (hNear()) hMarkRead();
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) hMarkRead(); });
const _applyProfileH = applyProfile;
applyProfile = function () { _applyProfileH(); if (S.profile && S.profile.agent_name) HS.agent = S.profile.agent_name; hTitle(); };

/* boot: the chat is the main screen */
$("hprompt").value = localStorage.getItem("aiw_hdraft") || "";
paintIcons();
go("home");
autoGrow($("hprompt"));   // measured once the view is visible (a hidden textarea measures 0)
