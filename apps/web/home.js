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
   v2 (2026-10-28, approved mockup): with nothing new, 球球 sits in the middle and its briefing (GET /api/greeting) pops
   up around it as speech bubbles, one every ~4.5 s (the old one fades and shrinks away); 球球's face follows each item,
   tapping a bubble runs it, quick chips sit above the input and typing stops the rotation. User bubbles are amber,
   球球's dark with an amber border and a small 球球 beside them; before the first token 球球 thinks (eyes up, dashed ring,
   bouncing dots + what it is doing) and the send key becomes 停止 (POST /api/chat/cancel). "/" opens the skill picker
   (a chosen skill is an amber token, sent as the 【技能：…】 marker like the old + menu), "+" a sheet with 拍照 / 录视频 /
   照片/视频 / 文件 / 新建任务 / 语音 / 链接 / 技能 (videos go through POST /api/uploads/raw).
   Loaded last: wraps go()/newThread() from app.js; the task view itself is untouched. */
"use strict";

const HS = { msgs: [], hasMore: false, loading: false, loaded: false, stream: null, mode: "auto", attach: [], scroll: null,
  sending: false, agent: "", lastRead: 0, tasks: [], tkFilter: "all", archived: [], skills: [], stageOff: false, liveAv: null };
const hBox = () => $("hmsgs");
const hTime = (ts) => { const d = new Date(ts * 1000), n = new Date();
  return d.toDateString() === n.toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : when(ts); };
const hMd = (t) => (window.AiwMd ? AiwMd.render(t || "", "md") : esc(t || "").replace(/\n/g, "<br>"));
const hFind = (id) => HS.msgs.find((m) => m.id === id);

/* ---------------------------------------------------------------- render */
// the small 球球 beside 球球's messages: one still frame of the real ball (same engine, look and disc as everywhere), cloned
let hAvCache = null;
function hAvSvg() {
  if (hAvCache !== null) return hAvCache;
  hAvCache = "";
  try {
    if (window.EmotionBall && EmotionBall.create) {
      const host = document.createElement("span"); host.className = "mate hav"; host.style.cssText = "position:fixed;left:-200px;top:0";
      document.body.appendChild(host);
      const eng = EmotionBall.create(host, { emotion: "02", idle: false, shape: "blob", eyeScale: 1.6, lite: true, autostart: false, label: "球球" });
      eng.renderStatic(); hAvCache = host.innerHTML; try { eng.destroy(); } catch (e) { /* ignore */ }
      host.innerHTML = hAvCache;   // keeps the gradient defs the clones point at
      host.setAttribute("aria-hidden", "true"); host.style.visibility = "hidden";
    }
  } catch (e) { console.warn("avatar", e); }
  return hAvCache;
}
const hAv = () => `<span class="mate hav" aria-hidden="true">${hAvSvg()}</span>`;
const hA = (m, inner, cls) => `<div class="a hrow hwa ${cls || ""}" data-id="${m.id}">${hAv()}${inner}</div>`;
const SK_MARK = /^\s*【技能[:：]([^】]+)】\n?/;
function hSkillTitle(id) { const s = ((HK.list || S.skillList || []).find((x) => x.id === id)); return s ? s.title || id : id; }
function hUserText(t) {   // the skill marker shows as amber chips, like iOS
  const m = (t || "").match(SK_MARK);
  if (!m) return esc(t);
  const chips = m[1].split(/[、,，\s]+/).filter(Boolean).map((id) => `<span class="hsk-chip">✨ ${esc(hSkillTitle(id))}</span>`).join("");
  return chips + (t.slice(m[0].length) ? "<br>" + esc(t.slice(m[0].length)) : "");
}
const hAttIcon = (a) => (a.kind === "video" || /\.(mp4|mov|m4v|webm|mkv)$/i.test(a.name || "") ? "🎥" : a.kind === "image" || /\.(jpe?g|png|gif|webp|heic)$/i.test(a.name || "") ? "🖼" : "📎");
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
  const pct = d.plan_total ? 100 * (d.plan_done || 0) / d.plan_total : st === "succeeded" ? 100 : live && d.step && d.max_steps ? 100 * d.step / d.max_steps : live ? 4 : 0;
  const plan = pct ? `<div class="hc-bar"><i style="width:${Math.max(3, Math.min(100, Math.round(pct)))}%"></i></div>` : "";
  const step = d.step ? `第 ${d.step}${d.max_steps ? "/" + d.max_steps : ""} 步` : "";
  const sub = [d.status_line && d.status_line !== (STATUS_CN[st] || [])[0] ? d.status_line : "", d.plan_total ? `${d.plan_done || 0}/${d.plan_total} 项` : "", live ? step : ""].filter(Boolean).join(" · ");
  const retry = ["failed", "stopped", "interrupted"].includes(st) && d.task_id
    ? `<button class="btn xs" onclick="event.stopPropagation();hRetry('${esc(d.task_id)}')">${icon("retry")}重试</button>` : "";
  return hA(m, `<div class="hcard st-${esc(st)}" role="button" tabindex="0" onclick="hOpenTask('${m.id}')" onkeydown="if(event.key==='Enter')hOpenTask('${m.id}')">
    <div class="hc-ic">${live && st === "running" ? '<span class="spin"></span>' : icon("monitor")}</div>
    <div class="grow"><div class="hc-t">${esc(d.title || m.text || "任务")}</div><div class="hc-s">${esc(sub || "交给电脑的任务")}</div>${plan}</div>
    <div class="hc-r">${chip(st)}${retry}<span class="hc-go">${icon("chev")}</span></div></div>`, "nm");
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
  return hA(m, `<div class="hq ${done || stale ? "off" : ""}">${body}
    ${d.thread_id ? `<button class="link small hq-open" onclick="hOpenTask('${m.id}')">查看任务 ›</button>` : ""}</div>`, "nm");
}
function hConfirm(m) {
  const d = m.data || {}, s = d.summary || {}, done = d.resolved, stale = d.stale && !done;
  let body = `<div class="hq-k">需要你确认${d.title ? " · " + esc(d.title) : ""}</div><div class="hq-q">${esc(s.title || m.text || "")}</div>`;
  if (s.what) body += `<div class="small hq-d">${esc(s.what)}</div>`;
  if (s.risk_cn || s.why) body += `<div class="small hq-d">${esc([s.risk_cn, s.why].filter(Boolean).join(" · "))}</div>`;
  if (done) body += `<div class="hq-done">${icon("check")}${done.answer ? "已允许" : "已拒绝"}</div>`;
  else if (stale) body += `<div class="hq-done muted">已过期（任务已经不在等这个确认）</div>`;
  else body += `<div class="opts"><button class="btn sm primary" onclick="hAnswer('${m.id}', {approve: true})">允许</button><button class="btn sm" onclick="hAnswer('${m.id}', {approve: false})">不允许</button></div>`;
  return hA(m, `<div class="hq confirm ${done || stale ? "off" : ""}">${body}
    ${d.thread_id ? `<button class="link small hq-open" onclick="hOpenTask('${m.id}')">查看任务 ›</button>` : ""}</div>`, "nm");
}
function hMsg(m) {
  const d = m.data || {};
  if (m.role === "user") {
    const att = (d.attachments || []).map((a) => `<span class="chip">${hAttIcon(a)} ${esc(a.name || "")}</span>`).join("");
    return `<div class="u hrow" data-id="${m.id}"><div><div class="bubble">${hUserText(m.text)}</div>${att ? `<div class="hatt">${att}</div>` : ""}<div class="meta">${m.tmp ? "发送中…" : hTime(m.created_at)}</div></div></div>`;
  }
  if (m.kind === "task") return hTaskCard(m);
  if (m.kind === "ask") return hAsk(m);
  if (m.kind === "confirm") return hConfirm(m);
  if (m.kind === "task_result") {
    const ok = d.status === "succeeded";
    return hA(m, `<div class="hres ${ok ? "ok" : "bad"}"><div class="bubble hmd">${hMd(m.text)}</div>${hFiles(d.files)}
      <div class="meta">${hTime(m.created_at)}${d.thread_id ? ` · <button class="link small" onclick="hOpenTask('${m.id}')">${d.has_more ? "查看完整结果" : "查看任务"} ›</button>` : ""}</div></div>`);
  }
  // assistant text (the one being written: 球球 thinks until the first token)
  if (m.stream && !m.text) return `<div class="a hrow hwa hthink" data-id="stream"><span class="hav-wrap"><span class="mate hav hav-live" id="hThinkAv" aria-hidden="true">${hAvSvg()}</span><span class="hring"></span></span>
    <div class="bubble hdots-b"><span class="hdots"><i></i><i></i><i></i></span><span class="hnote">${esc(m.note || "正在想…")}</span></div></div>`;
  let offer = "";
  if (d.offer_task) offer = d.task_started
    ? `<div class="opts"><button class="btn sm" disabled>${icon("check")}已交给电脑</button></div>`
    : `<div class="opts"><button class="btn sm primary" onclick="hToTask('${m.id}', this)">${icon("monitor")}交给电脑去做</button></div>`;
  return hA(m, `<div class="hbody"><div class="bubble hmd ${d.error ? "err" : ""}">${hMd(m.text)}</div>${offer}<div class="meta">${m.stream ? "" : hTime(m.created_at)}</div></div>`);
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
  hThinkLive();
  if (keep === "bottom") hScrollLatest();
}
function hThinkLive() {   // a real, moving 球球 (思考中: eyes up) while the reply has not started
  const el = $("hThinkAv");
  if (!el) { if (HS.liveAv) { HS.liveAv.destroy(); HS.liveAv = null; } return; }
  if (el._m || !(window.AiwMate && AiwMate.create)) return;
  if (HS.liveAv) HS.liveAv.destroy();
  el.innerHTML = "";
  el._m = HS.liveAv = AiwMate.create(el, { eyeScale: 1.6, lite: true });
  el._m.set("30");
}
function hPatch(m) {   // one message changed: swap its node in place (no scroll jump, pictures stay loaded)
  const box = hBox(); if (!box) return;
  const el = box.querySelector(`[data-id="${CSS.escape(m.id)}"]`);
  if (!el) return false;
  const tpl = document.createElement("div"); tpl.innerHTML = hMsg(m); paintIcons(tpl);
  el.replaceWith(tpl.firstElementChild);
  hThinkLive();
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
  if (hStaged()) { HS.stageOff = true; hStage(false); }   // something new: the chat takes over from 球球's briefing
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
    const last = [...HS.msgs].reverse().find((m) => m.seq);
    if (!HS.stageOff) HS.stageOff = !!(last && last.seq > HS.lastRead && last.role !== "user");   // new messages: show them
    hRender("bottom"); hTitle(); hMarkRead();
    hStage(!HS.stageOff);
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
function hKey(e) {
  if (HK.open) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); hSkMove(e.key === "ArrowDown" ? 1 : -1); return; }
    if ((e.key === "Enter" && !e.isComposing) || e.key === "Tab") { if (HK.rows.length) { e.preventDefault(); hSkChoose(HK.rows[HK.sel].id); return; } }
    if (e.key === "Escape") { e.preventDefault(); hSkClose(); return; }
  }
  const t = e.target;
  if (e.key === "Backspace" && HS.skills.length && t.selectionStart === 0 && t.selectionEnd === 0) { e.preventDefault(); hSkRemove(HS.skills[HS.skills.length - 1]); return; }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 760) { e.preventDefault(); hSend(e); }
}
function hInput(el) { autoGrow(el); localStorage.setItem("aiw_hdraft", el.value); hStageTyping(el.value); hSlashCheck(el); }
function hBlur() { setTimeout(() => { if (document.activeElement !== $("hprompt")) { hSkClose(); hStageTyping($("hprompt").value, true); } }, 150); }
function hUse(t) { $("hprompt").value = t; autoGrow($("hprompt")); $("hprompt").focus(); hStageTyping(t); }
function hStreamShow(text, note) {
  const was = HS.stream;
  HS.stream = { id: "stream", role: "assistant", kind: "text", text, note: note || (was && was.note) || "正在想…", data: {}, stream: true };
  const near = hNear();
  if (!text && was && !was.text) {   // still thinking: only the line under 球球 changes (the moving 球球 stays)
    const n = hBox().querySelector('[data-id="stream"] .hnote'); if (n) { n.textContent = HS.stream.note; return; }
  }
  if (!hPatch(HS.stream)) hRender();
  if (near) hScrollLatest();
}
function hSendKey(busy) {   // ↑ send / ■ stop while 球球 is answering
  const b = $("hSendBtn"); if (!b) return;
  b.classList.toggle("stop", !!busy); b.classList.remove("busy");
  b.setAttribute("aria-label", busy ? "停止" : "发送"); b.title = busy ? "停止" : "";
  b.innerHTML = icon(busy ? "stop" : "up");
}
async function hStop() {
  try {
    const r = await api("POST", "/api/chat/cancel");
    if (!r.cancelled) toast("这条已经交给电脑了：要停的话，点任务卡片进去停止");
  } catch (e) { toast("停止失败：" + e.message); }
}
async function hSend(e) {
  if (e) e.preventDefault();
  if (HS.sending) { hStop(); return; }
  hSkClose();
  const box = $("hprompt"), raw = box.value, body = raw.trim();
  if (HS.attach.some((a) => a.up)) { toast("附件还在上传，传完再发"); return; }
  if (!body && !HS.attach.length) { if (HS.skills.length) toast("说说要用这个技能做什么"); return; }
  const skills = HS.skills.slice();
  const text = skills.length ? `【技能：${skills.join("、")}】\n${body}` : body;   // the old + menu's marker (agent_runtime/skills.py)
  const attachments = HS.attach.map(({ up, pct, uid, ...a }) => a), mode = HS.mode;
  HS.sending = true; hSendKey(true);
  if (hStaged()) { HS.stageOff = true; hStage(false); }
  box.value = ""; autoGrow(box); localStorage.removeItem("aiw_hdraft"); HS.attach = []; hRenderAttach(); hSetMode("auto");
  HS.skills = []; hRenderTokens();
  hUpsert({ id: "tmp-" + Date.now(), role: "user", kind: "text", text: text || "（附件）", data: { attachments }, tmp: true }, { stick: true });
  hStreamShow("", "正在想…");
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
        else if (ev === "route") { if (d.route === "task") hStreamShow("", d.skill ? "正在把技能交给电脑…" : "正在交给电脑…"); }
        else if (ev === "status") { if (!acc) hStreamShow("", d.text || "正在想…"); }
        else if (ev === "delta") { acc += d.text || ""; hStreamShow(acc); }
        else if (ev === "message" || ev === "task") { gotReply = true; HS.stream = null; hUpsert(d, { stick: true }); }
      }
    }
  } catch (err) {
    toast("发送失败：" + err.message);
    HS.msgs = HS.msgs.filter((m) => !m.tmp);
    if (!$("hprompt").value) { box.value = raw; autoGrow(box); }
    HS.attach = attachments; hRenderAttach(); HS.skills = skills; hRenderTokens();
  } finally {
    HS.stream = null; HS.sending = false; hSendKey(false);
    hRender(); if (gotReply) hScrollLatest();
  }
}
/* + sheet: photos, videos and files go up as raw bytes (POST /api/uploads/raw; a video is far over the old 40 MB JSON cap) */
const H_MAX_MB = 500;
function hRawUpload(f, onp) {
  return new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/uploads/raw?name=${encodeURIComponent(f.name || "file")}&machine_id=${encodeURIComponent(MID)}&local_only=true`);
    if (TOKEN) x.setRequestHeader("Authorization", "Bearer " + TOKEN);
    x.setRequestHeader("Content-Type", f.type || "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable && onp) onp(Math.round(100 * e.loaded / e.total)); };
    x.onload = () => {
      if (x.status >= 200 && x.status < 300) { try { res(JSON.parse(x.responseText)); } catch (e) { rej(new Error("服务器回复看不懂")); } return; }
      let msg = x.responseText || String(x.status); try { msg = JSON.parse(msg).detail || msg; } catch (e) { /* plain text */ }
      rej(new Error(x.status === 413 ? `文件太大（最多 ${H_MAX_MB} MB）` : String(msg).slice(0, 160)));
    };
    x.onerror = () => rej(new Error("网络断了"));
    x.send(f);
  });
}
async function hUpload(list, input) {
  const files = [...(list || [])];
  if (input) input.value = "";
  for (const f of files) {
    if (f.size > H_MAX_MB * 1048576) { toast(`${f.name} 超过 ${H_MAX_MB} MB`); continue; }
    const kind = (f.type || "").startsWith("video") ? "video" : (f.type || "").startsWith("image") ? "image" : "";
    const a = { uid: "u" + Date.now() + Math.random(), name: f.name || "file", size: f.size, kind, up: true, pct: 0 };
    HS.attach.push(a); hRenderAttach();
    try {
      const r = await hRawUpload(f, (p) => { a.pct = p; hRenderAttach(); });
      Object.assign(a, r, { kind: r.kind || kind, up: false });
    } catch (e) { HS.attach = HS.attach.filter((x) => x !== a); toast(`上传失败：${f.name}：${e.message}`); }
    hRenderAttach();
  }
}
function hRenderAttach() {
  $("hattach").innerHTML = HS.attach.map((a, i) => `<span class="chip ${a.up ? "up" : ""}">${hAttIcon(a)} ${esc(a.name)}${a.up ? `<span class="hpct">${a.pct || 0}%</span>` : ""}<button class="link" aria-label="移除" onclick="HS.attach.splice(${i},1);hRenderAttach()">${icon("x")}</button></span>`).join("");
}
function hSetMode(m) {
  HS.mode = m;
  const el = $("hmode");
  el.hidden = m !== "task";
  el.innerHTML = m === "task" ? `${icon("monitor")}<span>新建任务：这条消息直接交给电脑去做</span><button class="link" onclick="hSetMode('auto')" aria-label="取消">${icon("x")}</button>` : "";
  $("hprompt").placeholder = m === "task" ? "要电脑做什么？" : "和我聊聊，或输入 / 选技能…";
}
function newTaskMode() { go("home"); hSetMode("task"); setTimeout(() => $("hprompt").focus(), 30); }

/* ---------------------------------------------------------------- "/" skill picker (the skills the old + menu listed: GET /api/skills) */
const HK = { list: null, open: false, sel: 0, q: "", start: -1, rows: [] };
async function hSkillList() { if (!HK.list) { HK.list = await loadSkillList(); } return HK.list || []; }
function hSlashCheck(el) {
  const pos = el.selectionStart == null ? el.value.length : el.selectionStart;
  const m = el.value.slice(0, pos).match(/(^|\s)\/([^\s/]*)$/);
  if (!m) { hSkClose(); return; }
  HK.start = pos - m[2].length - 1; HK.q = m[2].toLowerCase();
  hSkOpen();
}
async function hSkOpen() {
  const list = await hSkillList(), q = HK.q;
  HK.rows = list.filter((s) => !q || [s.id, s.title, s.desc].some((v) => String(v || "").toLowerCase().includes(q)))
    .sort((a, b) => (String(a.id).toLowerCase().startsWith(q) ? 0 : 1) - (String(b.id).toLowerCase().startsWith(q) ? 0 : 1)).slice(0, 30);
  HK.sel = Math.min(HK.sel, Math.max(0, HK.rows.length - 1));
  HK.open = true; hSkRender();
}
function hSkRender() {
  const box = $("hskills");
  box.hidden = !HK.open;
  if (!HK.open) return;
  box.innerHTML = `<div class="hsk-hd">技能${HK.q ? ` · “${esc(HK.q)}”` : ""}<span>↑↓ 选择 · 回车确定 · Esc 关闭</span></div>`
    + (HK.rows.map((s, i) => `<div class="hsk-r ${i === HK.sel ? "on" : ""}" role="option" aria-selected="${i === HK.sel}" data-id="${esc(s.id)}"
        onmousedown="event.preventDefault()" onclick="hSkChoose(this.dataset.id)"><span class="n">/${esc(s.id)}</span><span class="g"><b>${esc(s.title || s.id)}</b><small>${esc(s.desc || "")}</small></span></div>`).join("")
    || `<div class="hsk-none">没有匹配的技能 · <button class="link" onmousedown="event.preventDefault()" onclick="hSkClose();go('settings');setTimeout(()=>{const g=document.getElementById('g-skill');g&&g.scrollIntoView()},400)">管理技能</button></div>`);
  const on = box.querySelector(".hsk-r.on"); if (on) on.scrollIntoView({ block: "nearest" });
}
function hSkMove(d) { if (!HK.rows.length) return; HK.sel = (HK.sel + d + HK.rows.length) % HK.rows.length; hSkRender(); }
function hSkClose() { if (!HK.open) return; HK.open = false; HK.sel = 0; hSkRender(); }
function hSkChoose(id) {
  const el = $("hprompt"), pos = el.selectionStart == null ? el.value.length : el.selectionStart;
  if (HK.start >= 0 && el.value[HK.start] === "/") { el.value = el.value.slice(0, HK.start) + el.value.slice(pos); el.selectionStart = el.selectionEnd = HK.start; }
  if (!HS.skills.includes(id)) HS.skills.push(id);
  HK.start = -1; hSkClose(); hRenderTokens(); autoGrow(el); el.focus();
  localStorage.setItem("aiw_hdraft", el.value);
}
function hSkRemove(id) { HS.skills = HS.skills.filter((x) => x !== id); hRenderTokens(); $("hprompt").focus(); }
function hClearSkills() { HS.skills = []; hRenderTokens(); }
function hRenderTokens() {
  if (HS.mode !== "task") $("hprompt").placeholder = HS.skills.length ? "说说要用它做什么…" : "和我聊聊，或输入 / 选技能…";
  $("htokens").innerHTML = HS.skills.map((id) => `<span class="htok">✨ ${esc(hSkillTitle(id))}<button type="button" aria-label="移除技能" onclick="hSkRemove('${esc(id)}')">×</button></span>`).join("");
}
function hSlash() {   // 技能 / in the + sheet: the same as typing "/"
  const el = $("hprompt"); el.focus();
  const pos = el.selectionStart == null ? el.value.length : el.selectionStart, pre = el.value.slice(0, pos);
  const ins = (pre && !/\s$/.test(pre) ? " " : "") + "/";
  el.value = pre + ins + el.value.slice(pos); el.selectionStart = el.selectionEnd = pos + ins.length;
  hInput(el);
}

/* ---------------------------------------------------------------- "+" sheet */
function hSheet(on) {
  const sh = $("hsheet"); if (on === undefined) on = !sh.classList.contains("on");
  sh.classList.toggle("on", on); $("hsheetDim").classList.toggle("on", on); sh.setAttribute("aria-hidden", on ? "false" : "true");
  if (on) { hSkClose(); const f = sh.querySelector(".hs-it"); if (f) setTimeout(() => f.focus(), 50); }
}
function hPick(id) { hSheet(false); $(id).click(); }
function hLink() {
  const u = (window.prompt("粘贴一个链接（网页、视频、商品…）", "https://") || "").trim();
  if (!u || u === "https://") return;
  const el = $("hprompt"); el.value = (el.value.trim() ? el.value.trim() + " " : "") + u + " "; hInput(el); el.focus();
}
let hRec = null;
function hVoice() {   // dictation into the input (the browser's speech recognition)
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast("这个浏览器不支持语音输入，可以用键盘上的听写键"); $("hprompt").focus(); return; }
  if (hRec) { hRec.stop(); return; }
  const el = $("hprompt"), base = el.value;
  hRec = new SR(); hRec.lang = "zh-CN"; hRec.interimResults = true; hRec.continuous = true;
  hRec.onresult = (e) => { let t = ""; for (const r of e.results) t += r[0].transcript; el.value = (base ? base + " " : "") + t; hInput(el); };
  hRec.onend = () => { hRec = null; $("hPlusBtn").classList.remove("rec"); };
  hRec.onerror = (e) => toast("语音输入：" + e.error);
  hRec.start(); $("hPlusBtn").classList.add("rec"); toast("在听…再点一次 ＋ › 语音 结束");
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("hsheet").classList.contains("on")) hSheet(false); });

/* ---------------------------------------------------------------- 球球's briefing (nothing new on the home screen) */
const HB = { items: [], chips: [], i: -1, timer: 0, shown: 0, stopped: false, mate: null, slot: -1, at: 0, cur: null };
const HB_MS = 4500;
const hStaged = () => $("v-home").classList.contains("staged");
function hStage(on) {
  const v = $("v-home");
  v.classList.toggle("staged", !!on);
  $("hHistBtn").hidden = !HS.msgs.length;
  if (on) hStageStart(); else hStageStop();
}
function hShowChat() { HS.stageOff = true; hStage(false); hRender("bottom"); setTimeout(hScrollLatest, 60); }
async function hStageStart() {
  if (!HB.mate && window.AiwMate && AiwMate.create) { HB.mate = AiwMate.create($("hstOrb"), { eyeScale: 1.7 }); HB.mate.set("02"); }
  if (!HB.items.length || Date.now() - HB.at > 60000) {
    try {
      const g = await api("GET", "/api/greeting");
      HB.items = g.items || []; HB.chips = g.chips || []; HB.at = Date.now(); HB.i = -1;
    } catch (e) {   // never silent: say it, and still greet
      HB.items = [{ id: "g-err", text: `播报没取到（${e.message}）。有什么要我做的，直接说就行。`, mood: "34", action: { type: "focus" } }];
      HB.chips = [{ label: "💬 随便聊聊", action: { type: "focus" } }];
    }
  }
  if (!hStaged()) return;
  $("hchips").innerHTML = HB.chips.map((c, i) => `<button type="button" onclick="hChip(${i})">${esc(c.label)}</button>`).join("");
  if (!HB.cur) hStageNext();
  hStageRun();
}
function hStageRun() {
  clearInterval(HB.timer); HB.timer = 0;
  if (HB.stopped || S.view !== "home" || !hStaged() || document.hidden || HB.items.length < 2) return;
  HB.timer = setInterval(hStageNext, HB_MS);
}
function hStageStop() { clearInterval(HB.timer); HB.timer = 0; }
function hSlots() { return window.innerWidth <= 760 ? [0, 3, 1, 5] : [0, 2, 3, 1, 4, 5]; }
function hStageNext() {
  if (!HB.items.length) return;
  HB.i = (HB.i + 1) % HB.items.length;
  const it = HB.items[HB.i], slots = hSlots();
  HB.slot = slots[(slots.indexOf(HB.slot) + 1) % slots.length];
  const box = $("hbbs");
  box.querySelectorAll(".hbb").forEach((b) => { b.classList.remove("on"); b.classList.add("out"); setTimeout(() => b.remove(), 650); });
  const b = document.createElement("button");
  b.type = "button"; b.className = `hbb s${HB.slot}`; b.textContent = it.text; b.dataset.act = JSON.stringify(it.action || {});
  b.onclick = () => hStageAct(it.action || {});
  box.appendChild(b);
  requestAnimationFrame(() => requestAnimationFrame(() => b.classList.add("on")));
  HB.cur = it; HB.shown++;
  if (HB.mate) HB.mate.set(it.mood || "02", { burst: it.mood === "33" });
}
function hStageTyping(v, blurred) {
  const typing = !!(v || "").trim() || HS.skills.length > 0;
  if (typing && !HB.stopped) { HB.stopped = true; hStageStop(); $("hstage").classList.add("typing"); }
  else if (!typing && HB.stopped && blurred) { HB.stopped = false; $("hstage").classList.remove("typing"); hStageRun(); }
}
function hChip(i) { const c = HB.chips[i]; if (c) hStageAct(c.action || {}); }
async function hStageAct(a) {
  const el = $("hprompt");
  switch (a.type) {
    case "thread": if (a.thread_id) { HS.fromHome = true; HS.scroll = null; await openThread(a.thread_id); } break;
    case "retry":
      try { const r = await api("POST", `/api/tasks/${a.task_id}/retry`); toast("好的，接着做"); HB.at = 0; HS.fromHome = true; await openThread(r.thread_id || a.thread_id); }
      catch (e) { toast("没能接着做：" + e.message); }
      break;
    case "view": if (a.view) go(a.view); break;
    case "send": el.value = a.text || ""; hSend(); break;
    case "prompt": hUse(a.text || ""); break;
    default: el.focus();
  }
}
document.addEventListener("visibilitychange", () => { if (document.hidden) hStageStop(); else if (hStaged()) hStageRun(); });

/* ---------------------------------------------------------------- the 电脑 button (top right) */
// Before the chat home the main screen was the task view, where this button shows / hides the desktop pane. On every
// other view (the chat home above all) that pane never shows, so the click did nothing: there it opens the 电脑 view.
const _toggleDeskPane = toggleDeskPane;
toggleDeskPane = function () {
  if (S.view === "chat") return _toggleDeskPane();
  if (S.view === "computer") return go(HS.deskFrom || "home");
  HS.deskFrom = S.view; go("computer");
};

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
  if (view !== "home") hStageStop();
  $("deskToggle").classList.toggle("on", view === "computer");
  if (view === "home") {
    hTitle();
    if (hStaged()) hStageRun();
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
if ($("hprompt").value.trim()) HB.stopped = true;   // a draft is waiting: 球球 does not talk over it
paintIcons();
go("home");
autoGrow($("hprompt"));   // measured once the view is visible (a hidden textarea measures 0)
