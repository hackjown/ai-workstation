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
   tapping a bubble runs it and typing stops the rotation (3.6: no quick chips; bubbles placed outside the 球球 circle by
   hPlace; light theme: white 球球 bubbles; the small 球球 head beside replies, cards and thinking; WeChat-like + panel). User bubbles are amber,
   球球's dark with an amber border and a small 球球 beside them; before the first token 球球 thinks (eyes up, dashed ring,
   bouncing dots + what it is doing) and the send key becomes 停止 (POST /api/chat/cancel). "/" opens the skill picker
   (a chosen skill is an amber token, sent as the 【技能：…】 marker like the old + menu), "+" a sheet with 拍照 / 录视频 /
   照片/视频 / 文件 / 新建任务 / 语音 / 链接 / 技能 (videos go through POST /api/uploads/raw).
   Loaded last: wraps go()/newThread() from app.js; the task view itself is untouched. */
"use strict";

const HS = { msgs: [], outbox: [], hasMore: false, loading: false, loaded: false, stream: null, mode: "auto", attach: [], scroll: null,
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
      const host = document.createElement("span"); host.className = "mate amber hav"; host.style.cssText = "position:fixed;left:-200px;top:0";
      document.body.appendChild(host);
      const eng = EmotionBall.create(host, { emotion: "02", idle: false, shape: "blob", eyeScale: 1.6, lite: true, autostart: false, label: "球球",
        color: (window.AiwMood && AiwMood.LOOK || {}).color || "#FFFFFF", eyeColor: (window.AiwMood && AiwMood.LOOK || {}).eyeColor || "#1A1A1A" });
      eng.renderStatic(); let html = host.innerHTML; try { eng.destroy(); } catch (e) { /* ignore */ }
      // 3.7: the clones carried copies of the gradient defs with the same ids, and url(#id) takes the FIRST one in the
      // page: one inside a hidden view (display:none) paints nothing, so the head lost its ball in the task view.
      // Now the ids are unique to the avatar, only this host keeps the defs, the clones only point at them.
      (html.match(/id="([^"]+)"/g) || []).forEach((m) => {
        const id = m.slice(4, -1), nid = "hav-" + id;
        html = html.split('id="' + id + '"').join('id="' + nid + '"').split("url(#" + id + ")").join("url(#" + nid + ")")
          .split("url(&quot;#" + id + "&quot;)").join("url(&quot;#" + nid + "&quot;)").split('href="#' + id + '"').join('href="#' + nid + '"');
      });
      host.innerHTML = html;       // keeps the gradient defs the clones point at
      hAvCache = html.replace(/<defs>[\s\S]*?<\/defs>/g, "");
      host.setAttribute("aria-hidden", "true"); host.style.visibility = "hidden";
    }
  } catch (e) { console.warn("avatar", e); }
  return hAvCache;
}
const hAv = () => `<span class="mate amber hav" aria-hidden="true">${hAvSvg()}</span>`;   // the small 球球 head (3.6: amber disc, like iOS)
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
    <div class="hc-ic">${live && st === "running" ? '<span class="spin"></span>' : st === "succeeded" ? icon("check") : icon("monitor")}</div>
    <div class="grow"><div class="hc-t">${esc(d.title || m.text || "任务")}</div><div class="hc-s">${esc(sub || "交给电脑的任务")}</div>${plan}</div>
    <div class="hc-r">${chip(st)}${retry}<span class="hc-go">${icon("chev")}</span></div></div>`, "nm");
}
/* what a resolved question / form card says (3.7: the server sends resolved.text; a form answered with nothing used
   to show "已回答：null") */
function hResolved(r) {
  const a = r.answer;
  if (a === true || a === false) return a ? "已允许" : "已拒绝";
  let t = typeof r.text === "string" ? r.text : null;
  if (t === null) {
    t = a == null ? "" : a === "done" ? "已完成，交还给电脑" : String(a);
    if (/^\s*\{/.test(t)) { try { const o = JSON.parse(t); t = Object.entries(o).filter(([, v]) => v != null && String(v).trim()).map(([k, v]) => k + "：" + v).join("；"); } catch (e) { /* keep */ } }
  }
  t = t.trim();
  if (!t || t === "null" || t === "{}") return "已回答";
  return t.startsWith("已") ? t : "已回答：" + t.slice(0, 80);
}
function hAsk(m) {
  const d = m.data || {}, k = d.ask_kind || "question";
  const done = d.resolved, stale = d.stale && !done;
  let body = `<div class="hq-k">${k === "takeover" ? "需要你接管" : k === "form" ? "需要你填写" : k === "escalate" ? "任务卡住了" : "任务在问你"}${d.title ? " · " + esc(d.title) : ""}</div>
    <div class="hq-q">${esc(m.text || d.question || "")}</div>`;
  if (k === "takeover" && d.takeover && (d.takeover.steps || d.takeover.reason)) body += `<div class="small hq-d">${esc(d.takeover.steps || d.takeover.reason)}</div>`;
  if (done) {
    body += `<div class="hq-done">${icon("check")}${esc(hResolved(done))}</div>`;
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
  if (m.stream && !m.text) return `<div class="a hrow hwa hthink nm" data-id="stream"><span class="mate amber hav hav-live" id="hThinkAv" aria-hidden="true">${hAvSvg()}</span>
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
  box.innerHTML = (list.length ? top + list.map(hMsg).join("") : hEmpty()) + (HS.outbox || []).map(hQueued).join("");
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
function hInput(el) { autoGrow(el); localStorage.setItem("aiw_hdraft", el.value); hStageTyping(el.value); hSlashCheck(el); hKeys(); }
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
function hSendKey(busy) { hKeys(); }
/* 3.8 the right-hand button(s): listening -> the mic (■ to finish); typed text / files -> ↑ send (queued while 球球
   answers); a reply with nothing typed -> ■ stop; otherwise the mic (Web Speech API where the browser has it) */
function hKeys() {
  const b = $("hSendBtn"), mic = $("hMicBtn"); if (!b) return;
  const box = $("hprompt"), has = !!((box && box.value.trim()) || HS.attach.length);
  const canMic = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  const stop = HS.sending && !has && !hRec;
  const showMic = !!mic && canMic && (hRec || (!has && !HS.sending));
  b.hidden = showMic;
  if (mic) { mic.hidden = !showMic; mic.classList.toggle("rec", !!hRec); mic.setAttribute("aria-label", hRec ? "停止语音输入" : "语音输入");
    mic.innerHTML = hRec ? icon("stop") : icon("mic"); }
  b.classList.toggle("stop", stop); b.classList.remove("busy");
  b.setAttribute("aria-label", stop ? "停止" : HS.sending ? "排队发送" : "发送"); b.title = stop ? "停止" : "";
  b.innerHTML = icon(stop ? "stop" : "up");
}
async function hStop() {
  try {
    const r = await api("POST", "/api/chat/cancel");
    if (!r.cancelled) toast("这条已经交给电脑了：要停的话，点任务卡片进去停止");
  } catch (e) { toast("停止失败：" + e.message); }
}
async function hSend(e) {
  if (e) e.preventDefault();
  hSkClose();
  const box = $("hprompt"), raw = box.value, body = raw.trim();
  // 3.8: while 球球 answers, an empty box stops the reply; a typed message queues and goes out after it (before, the
  // button was always 停止 then, and Enter stopped the reply instead of sending what you typed)
  if (HS.sending && !body && !HS.attach.length) { hStop(); return; }
  if (hRec) hRec.stop();
  if (HS.attach.some((a) => a.up)) { toast("附件还在上传，传完再发"); return; }
  if (!body && !HS.attach.length) { if (HS.skills.length) toast("说说要用这个技能做什么"); return; }
  const skills = HS.skills.slice();
  const text = skills.length ? `【技能：${skills.join("、")}】\n${body}` : body;   // the old + menu's marker (agent_runtime/skills.py)
  const attachments = HS.attach.map(({ up, pct, uid, ...a }) => a), mode = HS.mode;
  if (hStaged()) { HS.stageOff = true; hStage(false); }
  box.value = ""; autoGrow(box); localStorage.removeItem("aiw_hdraft"); HS.attach = []; hRenderAttach(); hSetMode("auto");
  HS.skills = []; hRenderTokens();
  if (HS.sending) { HS.outbox.push({ id: "q-" + Date.now(), text, attachments, mode }); hRender(); hScrollLatest(); hKeys(); return; }
  await hSendNow(text, attachments, mode, { raw, skills });
  await hFlush();
}
async function hFlush() {   // queued messages, oldest first, one after the other
  while (!HS.sending && HS.outbox.length) {
    const q = HS.outbox.shift();
    if (!(await hSendNow(q.text, q.attachments, q.mode))) { HS.outbox.unshift(q); hRender(); break; }
  }
}
function hQueued(q) {
  return `<div class="u hrow hqueued" data-id="${q.id}"><div class="bubble" onclick="hQueuedMenu('${q.id}')" title="点一下：立即发送或删除">${hUserText(q.text || "（附件）")}</div>
    <div class="hmeta hq-meta">排队中 · 回复完自动发送 · <button class="link" onclick="hQueuedNow('${q.id}')">立即发送</button> · <button class="link" onclick="hQueuedDrop('${q.id}')">删除</button></div></div>`;
}
function hQueuedMenu(id) { if (confirm(HS.sending ? "立即发送这条？（会停止球球正在写的回复）\n取消 = 继续排队" : "立即发送这条？")) hQueuedNow(id); }
async function hQueuedNow(id) {
  const i = HS.outbox.findIndex((q) => q.id === id); if (i < 0) return;
  HS.outbox.unshift(HS.outbox.splice(i, 1)[0]);
  if (HS.sending) await hStop(); else await hFlush();
}
function hQueuedDrop(id) { HS.outbox = HS.outbox.filter((q) => q.id !== id); hRender(); hKeys(); }
async function hSendNow(text, attachments, mode, undo) {
  HS.sending = true; hSendKey(true);
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
    const box = $("hprompt");
    if (undo && !box.value) { box.value = undo.raw; autoGrow(box); HS.attach = attachments; hRenderAttach(); HS.skills = undo.skills; hRenderTokens(); }
    return false;
  } finally {
    HS.stream = null; HS.sending = false; hSendKey(false);
    hRender(); if (gotReply) hScrollLatest();
  }
  return true;
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
  setTimeout(hKeys, 0);
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

/* ---------------------------------------------------------------- "+" sheet (3.6: WeChat-like panel) */
// grey line icons for the tiles (stroke style of the app's icon set)
Object.assign(IC, {
  camera: "M3 8a2 2 0 0 1 2-2h2.5l1.5-2h6l1.5 2H19a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  video: "M3 7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM16 10l5-3v10l-5-3",
  image: "M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM3 16l5-5 4 4 3-3 6 6M15.5 8.5h.01",
  folder: "M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  sparkle: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z",
});
document.querySelectorAll("#hsheet [data-hi]").forEach((el) => { el.innerHTML = icon(el.dataset.hi); });   // after IC has them (app.js paints <i data-i> first)
function hSheetPages() {   // 4 per row, 2 rows a page; more pages swipe sideways (dots under them)
  const grid = $("hsheet").querySelector(".hs-grid");
  if (!grid || grid.dataset.paged) return;
  const items = [...grid.querySelectorAll(".hs-it")].filter((x) => !x.hidden && getComputedStyle(x).display !== "none");
  grid.dataset.paged = "1"; grid.innerHTML = "";
  for (let i = 0; i < items.length; i += 8) {
    const pg = document.createElement("div"); pg.className = "hs-page"; items.slice(i, i + 8).forEach((x) => pg.appendChild(x)); grid.appendChild(pg);
  }
  const n = grid.children.length, dots = $("hsDots");
  dots.hidden = n < 2;
  dots.innerHTML = Array.from({ length: n }, (_, i) => `<i class="${i ? "" : "on"}"></i>`).join("");
  grid.onscroll = () => { const k = Math.round(grid.scrollLeft / Math.max(1, grid.clientWidth)); [...dots.children].forEach((d, i) => d.classList.toggle("on", i === k)); };
}
function hSheet(on) {
  const sh = $("hsheet"); if (on === undefined) on = !sh.classList.contains("on");
  sh.classList.toggle("on", on); $("hsheetDim").classList.toggle("on", on); sh.setAttribute("aria-hidden", on ? "false" : "true");
  if (on) { hSkClose(); hSheetPages(); const f = sh.querySelector(".hs-it"); if (f) setTimeout(() => f.focus(), 50); }
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
  hRec.onresult = (e) => { let t = ""; for (const r of e.results) t += r[0].transcript;
    el.value = base + (base && /[A-Za-z0-9]$/.test(base) && /^[A-Za-z0-9]/.test(t) ? " " : "") + t; hInput(el); };
  hRec.onend = () => { hRec = null; $("hPlusBtn").classList.remove("rec"); hKeys(); };
  hRec.onerror = (e) => toast(e.error === "not-allowed" || e.error === "service-not-allowed" ? "没有麦克风权限：在浏览器的网站设置里允许麦克风" : "语音输入：" + e.error);
  try { hRec.start(); } catch (err) { hRec = null; toast("语音输入没能开始：" + err.message); hKeys(); return; }
  $("hPlusBtn").classList.add("rec"); hKeys(); toast("在听…点右边的 ■ 结束");
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("hsheet").classList.contains("on")) hSheet(false); });

/* ---------------------------------------------------------------- 球球's briefing (nothing new on the home screen) */
const HB = { items: [], chips: [], i: -1, timer: 0, shown: 0, stopped: false, mate: null, slot: -1, at: 0, cur: null };   // chips: no longer shown (3.6)
const HB_MS = 4500;
const hStaged = () => $("v-home").classList.contains("staged");
function hStage(on) {
  const v = $("v-home");
  v.classList.toggle("staged", !!on);
  $("hHistBtn").hidden = !HS.msgs.length;
  if (on) { hStageStart(); const w = document.querySelector("#hbbs .hbw:not(.out)"); if (w) requestAnimationFrame(() => hLayoutBubble(w)); } else hStageStop();
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
  if (!HB.cur) hStageNext();
  hStageRun();
}
function hStageRun() {
  clearInterval(HB.timer); HB.timer = 0;
  if (HB.stopped || S.view !== "home" || !hStaged() || document.hidden || HB.items.length < 2) return;
  HB.timer = setInterval(hStageNext, HB_MS);
}
function hStageStop() { clearInterval(HB.timer); HB.timer = 0; }
/* 3.6: bubble placement computed from the 球球 circle and the stage (same math as iOS StageLayout, HomeV2.swift):
   a slot outside the circle (above / below, beside it when there is room), never closer than GAP to it, never outside
   the stage; when no slot holds the whole text, the placement showing the most lines, cut with an ellipsis. */
const HP = { gap: 30, edge: 14, sideMin: 170 };
function hSideRoom(c, r, A) { return Math.min(A.x + A.w - HP.edge - (c.x + r + HP.gap), (c.x - r - HP.gap) - (A.x + HP.edge)); }
function hFrame(side, lean, b, c, r, A) {
  const minX = A.x + HP.edge, maxX = A.x + A.w - HP.edge, minY = A.y + HP.edge, maxY = A.y + A.h - HP.edge;
  const cx = (x) => Math.max(minX, Math.min(x, maxX - b.w)), cy = (y) => Math.max(minY, Math.min(y, maxY - b.h));
  if (side === "above") return { x: cx(c.x - b.w / 2 + lean * b.w * 0.28), y: c.y - r - HP.gap - b.h, w: b.w, h: b.h };
  if (side === "below") return { x: cx(c.x - b.w / 2 + lean * b.w * 0.28), y: c.y + r + HP.gap, w: b.w, h: b.h };
  if (side === "right") return { x: c.x + r + HP.gap, y: cy(c.y - b.h * 0.65), w: b.w, h: b.h };
  return { x: c.x - r - HP.gap - b.w, y: cy(c.y - b.h * 0.65), w: b.w, h: b.h };
}
function hDist(p, f) { const dx = Math.max(f.x - p.x, 0, p.x - f.x - f.w), dy = Math.max(f.y - p.y, 0, p.y - f.y - f.h); return Math.hypot(dx, dy); }
function hFits(f, c, r, A) {
  const e = HP.edge - 0.5;
  return f.x >= A.x + e && f.y >= A.y + e && f.x + f.w <= A.x + A.w - e && f.y + f.h <= A.y + A.h - e && hDist(c, f) >= r + HP.gap * 0.8;
}
function hPlace(slot, b, c, r, A, lineH, padV) {
  const L = hSideRoom(c, r, A) >= HP.sideMin
    ? [["above", 1], ["right", 0], ["below", -1], ["left", 0], ["above", -1], ["below", 1]]
    : [["above", 1], ["below", -1], ["above", -1], ["below", 1]];
  for (let i = 0; i < L.length; i++) {
    const [side, lean] = L[(slot % L.length + i) % L.length], f = hFrame(side, lean, b, c, r, A);
    if (hFits(f, c, r, A)) return { f, side, lines: 0 };
  }
  let best = null;
  const offer = (f, side, lines) => { if (hFits(f, c, r, A) && lines > (best ? best.lines : 0)) best = { f, side, lines }; };
  const need = Math.max(1, Math.round((b.h - padV) / lineH));
  for (const side of ["above", "below"]) {
    const room = side === "above" ? (c.y - r - HP.gap) - (A.y + HP.edge) : (A.y + A.h - HP.edge) - (c.y + r + HP.gap);
    const lines = Math.min(need, Math.floor((room - padV) / lineH));
    if (lines >= 1) offer(hFrame(side, side === "above" ? 1 : -1, { w: b.w, h: padV + lines * lineH }, c, r, A), side, lines);
  }
  const sr = hSideRoom(c, r, A);
  if (sr >= 110) {
    const w2 = Math.min(b.w, sr), lines = Math.min(Math.ceil(need * b.w / w2), Math.floor((A.h - 2 * HP.edge - padV) / lineH));
    if (lines >= 1) for (const side of ["right", "left"]) offer(hFrame(side, 0, { w: w2, h: padV + lines * lineH }, c, r, A), side, lines);
  }
  if (best) return best;
  const up = (c.y - r - HP.gap) - (A.y + HP.edge), down = (A.y + A.h - HP.edge) - (c.y + r + HP.gap), side = up >= down ? "above" : "below";
  const f = hFrame(side, 0, { w: b.w, h: padV + lineH }, c, r, A); f.y = Math.max(A.y + HP.edge, Math.min(f.y, A.y + A.h - HP.edge - f.h));
  return { f, side, lines: 1 };
}
function hTail(p, c, r) {   // two dots between the bubble edge facing 球球 and the circle
  const f = p.f; let st;
  if (p.side === "above") st = { x: Math.max(f.x + 24, Math.min(c.x, f.x + f.w - 24)), y: f.y + f.h };
  else if (p.side === "below") st = { x: Math.max(f.x + 24, Math.min(c.x, f.x + f.w - 24)), y: f.y };
  else if (p.side === "right") st = { x: f.x, y: Math.max(f.y + 16, Math.min(c.y, f.y + f.h - 16)) };
  else st = { x: f.x + f.w, y: Math.max(f.y + 16, Math.min(c.y, f.y + f.h - 16)) };
  const vx = st.x - c.x, vy = st.y - c.y, len = Math.max(1, Math.hypot(vx, vy)), e = { x: c.x + vx / len * r, y: c.y + vy / len * r };
  return [0.32, 0.68].map((t) => ({ x: st.x + (e.x - st.x) * t, y: st.y + (e.y - st.y) * t }));
}
function hLayoutBubble(wrap) {   // place one bubble (wrap = .hbw holding the .hbb and its two dots) for its turn
  const stage = $("hstage"), orb = $("hstOrb"), b = wrap.querySelector(".hbb"), t = b.querySelector(".hbt");
  const S0 = stage.getBoundingClientRect(), O = orb.getBoundingClientRect();
  if (!S0.width || !O.width) return null;
  const top = $("hHistBtn").hidden ? 4 : 46;
  const A = { x: 0, y: top, w: S0.width, h: Math.max(120, S0.height - top - 4) };
  const r = O.width / 2, c = { x: O.left - S0.left + r, y: O.top - S0.top + r - (parseFloat(getComputedStyle(orb).getPropertyValue("--hf")) || 0) };
  const maxW = Math.min(270, A.w - 2 * HP.edge);
  b.style.width = ""; b.style.maxWidth = maxW + "px"; t.style.webkitLineClamp = ""; t.classList.remove("cut");
  const nat = { w: Math.ceil(b.offsetWidth) + 1, h: Math.ceil(b.offsetHeight) };
  const lh = parseFloat(getComputedStyle(t).lineHeight) || 21, padV = nat.h - Math.round((t.offsetHeight || lh) / lh) * lh;
  const p = hPlace(+wrap.dataset.slot || 0, nat, c, r, A, lh, Math.max(16, padV));
  b.style.left = p.f.x + "px"; b.style.top = p.f.y + "px"; b.style.width = p.f.w + "px"; b.style.maxWidth = "none";
  if (p.lines) { t.classList.add("cut"); t.style.webkitLineClamp = String(p.lines); }
  b.style.transformOrigin = p.side === "above" ? "50% 100%" : p.side === "below" ? "50% 0" : p.side === "right" ? "0 50%" : "100% 50%";
  const [d1, d2] = hTail(p, c, r), dots = wrap.querySelectorAll(".hbd");
  dots[0].style.left = d1.x - 5.5 + "px"; dots[0].style.top = d1.y - 5.5 + "px";
  dots[1].style.left = d2.x - 3 + "px"; dots[1].style.top = d2.y - 3 + "px";
  wrap.dataset.side = p.side; wrap.dataset.lines = p.lines;
  return { bubble: { x: p.f.x, y: p.f.y, w: b.offsetWidth, h: b.offsetHeight }, orb: { cx: c.x, cy: c.y, r }, stage: A, side: p.side, lines: p.lines };
}
function hStageNext() {
  if (!HB.items.length) return;
  HB.i = (HB.i + 1) % HB.items.length;
  const it = HB.items[HB.i];
  HB.slot = (HB.slot + 1) % 12;
  const box = $("hbbs");
  box.querySelectorAll(".hbw").forEach((w) => { w.classList.remove("on"); w.classList.add("out"); setTimeout(() => w.remove(), 650); });
  const w = document.createElement("div");
  w.className = "hbw"; w.dataset.slot = HB.slot;
  w.innerHTML = `<span class="hbd d1"></span><span class="hbd d2"></span><button type="button" class="hbb"><span class="hbt"></span></button>`;
  const b = w.querySelector(".hbb");
  b.querySelector(".hbt").textContent = String(it.text || "").replace(/\*\*|__|`/g, "");   // a bubble is plain text
  b.dataset.act = JSON.stringify(it.action || {});
  b.onclick = () => hStageAct(it.action || {});
  box.appendChild(w);
  hLayoutBubble(w);
  requestAnimationFrame(() => requestAnimationFrame(() => w.classList.add("on")));
  HB.cur = it; HB.shown++;
  if (HB.mate) HB.mate.set(it.mood || "02", { burst: it.mood === "33" });
}
window.addEventListener("resize", () => { const w = document.querySelector("#hbbs .hbw.on, #hbbs .hbw:not(.out)"); if (w && hStaged()) hLayoutBubble(w); });
function hStageTyping(v, blurred) {
  const typing = !!(v || "").trim() || HS.skills.length > 0;
  if (typing && !HB.stopped) { HB.stopped = true; hStageStop(); $("hstage").classList.add("typing"); }
  else if (!typing && HB.stopped && blurred) { HB.stopped = false; $("hstage").classList.remove("typing"); hStageRun(); }
}
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


/* ======================================================= 3.7
   * ↓ 最新消息 / ↓ 最新 float above the composer's REAL top (its height changes with attachments, the skill picker,
     a growing textarea, the on-screen keyboard), not at a fixed 110px that the input bar grew over
   * the task view's "+" opens the same WeChat-like panel as the chat (拍照 / 录视频 / 照片/视频 / 文件 / 技能) */
function hPlaceFabs() {
  [["hToLatest", "v-home"], ["toLatest", "v-chat"]].forEach(([f, v]) => {
    const fab = $(f), view = $(v); if (!fab || !view) return;
    const w = view.querySelector(".composer-wrap"); if (!w) return;
    const vb = view.getBoundingClientRect(), wb = w.getBoundingClientRect();
    if (!vb.height || !wb.height) return;
    fab.style.bottom = Math.max(12, Math.round(vb.bottom - wb.top + 12)) + "px";
  });
}
(function () {
  const ro = window.ResizeObserver ? new ResizeObserver(() => hPlaceFabs()) : null;
  document.querySelectorAll("#v-home, #v-chat, #v-home .composer-wrap, #v-chat .composer-wrap").forEach((e) => ro && ro.observe(e));
  window.addEventListener("resize", hPlaceFabs);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", hPlaceFabs);
  const _go37 = go; go = function () { const r = _go37.apply(this, arguments); setTimeout(hPlaceFabs, 0); return r; };
  setTimeout(hPlaceFabs, 0);
})();
(function tSheetSetup() {
  const view = $("v-chat"), clip = document.querySelector('#v-chat .composer button[aria-label="添加"], #v-chat .composer button[aria-label="附件"]');
  if (!view || !clip || $("tsheet")) return;
  view.insertAdjacentHTML("beforeend", `<div id="tsheetDim" class="hsheet-dim" onclick="tSheet(false)"></div>
    <div id="tsheet" class="hsheet" role="dialog" aria-label="添加" aria-hidden="true"><div class="hs-grab"></div><div class="hs-grid"><div class="hs-page">
      <button class="hs-it" onclick="tPick('tcam')"><span class="hs-ic">${icon("camera")}</span>拍照</button>
      <button class="hs-it" onclick="tPick('tvid')"><span class="hs-ic">${icon("video")}</span>录视频</button>
      <button class="hs-it" onclick="tPick('tlib')"><span class="hs-ic">${icon("image")}</span>照片/视频</button>
      <button class="hs-it" onclick="tPick('file')"><span class="hs-ic">${icon("folder")}</span>文件</button>
      <button class="hs-it" onclick="tSheet(false);togglePlus()"><span class="hs-ic">${icon("sparkle")}</span>技能</button>
    </div></div></div>
    <input type="file" id="tcam" hidden accept="image/*" capture="environment" onchange="uploadFiles(this.files)" />
    <input type="file" id="tvid" hidden accept="video/*" capture="environment" onchange="uploadFiles(this.files)" />
    <input type="file" id="tlib" hidden multiple accept="image/*,video/*" onchange="uploadFiles(this.files)" />`);
  clip.setAttribute("onclick", "tSheet()");
  clip.classList.add("hplus"); clip.innerHTML = "＋";
})();
function tSheet(on) {
  const sh = $("tsheet"); if (!sh) return; if (on === undefined) on = !sh.classList.contains("on");
  sh.classList.toggle("on", on); $("tsheetDim").classList.toggle("on", on); sh.setAttribute("aria-hidden", on ? "false" : "true");
  const pm = $("plusMenu"); if (on && pm) pm.classList.remove("open");
}
function tPick(id) { tSheet(false); $(id).click(); }

setTimeout(() => { try { hKeys(); } catch (e) { /* composer not on this page */ } }, 0);
