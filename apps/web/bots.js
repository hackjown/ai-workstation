/* v3.9.57 引擎 (Bot; was 引擎对话): talk to Antigravity (v3.9.60: also Codex, Claude Code, CodeBuddy, Grok Build) on the computer directly, or hand the talking to AIW 管家.
   GET /api/bots, POST /api/bots/convs, GET|PATCH|DELETE /api/bots/convs/{id}, POST …/send|stop|takeover. */
const BT = { engines: [], convs: [], cid: null, conv: null, msgs: [], quote: null, poll: null, last: 0, sending: false, attach: [], skills: [] };
const BT_WHO = { user: "我", aiw: "AIW 管家", system: "" };

async function btLoad() {
  try { const r = await api("GET", "/api/bots"); BT.engines = r.engines || []; BT.convs = r.convs || []; }
  catch (e) { toast("引擎加载失败：" + e.message); return; }
  await btLoadGroups();
  btRenderList();
  const saved = BT.gid ? "g:" + BT.gid : localStorage.getItem("aiw_bot_conv") || "";
  if (saved.startsWith("g:")) {   // v3.9.61: the last thing open was a group
    if (BT.groups.some((g) => g.id === saved.slice(2))) { btGroupOpen(saved.slice(2), true); return; }
    localStorage.removeItem("aiw_bot_conv");
  }
  const cur = BT.convs.find((c) => c.id === (BT.cid || localStorage.getItem("aiw_bot_conv")));
  btModels(false, cur ? cur.bot : "agy").then(() => { if (BT.conv) btRenderConv(); }).catch(() => {});   // v3.9.58: names for the model chip
  const want = BT.cid || localStorage.getItem("aiw_bot_conv");
  if (want && BT.convs.some((c) => c.id === want)) btOpen(want, true);
  else btShowConv(false);
}
// v3.9.69: round avatar — amber ring like AIW's, the engine's real logo inside (apps/web/engines/<id>.svg)
const BT_LOGOS = ["agy", "codex", "claude", "codebuddy", "grok", "muse", "cue"];
function btAv(bot, extra) {
  const id = bot || "agy", eng = btEngine(id);
  const inner = BT_LOGOS.includes(id) ? `<img src="engines/${id}.svg" alt="${esc(eng.title)}" draggable="false">` : esc((eng.short || eng.title || id).slice(0, 3));
  return `<span class="bt-av ${esc(id)}${extra ? " " + extra : ""}" title="${esc(eng.title)}">${inner}</span>`;
}
function btEngine(id) { return BT.engines.find((e) => e.id === id) || { title: id, short: id }; }
function btRenderList() {
  const box = $("btList"); if (!box) return;
  const items = BT.convs.map((c) => {
    const e = btEngine(c.bot), st = c.takeover ? "管家接管中" : (c.busy || c.status === "running") ? "回复中…" : "";
    return `<button class="bt-it ${esc(c.bot || "agy")} ${c.id === BT.cid ? "on" : ""}" onclick="btOpen('${c.id}')">
      ${btAv(c.bot)}
      <span class="grow"><span class="t1">${esc(c.title)}</span><span class="t2">${st ? `<b>${st}</b> · ` : ""}${c.last ? esc(c.last.text) : esc(e.title)}</span></span></button>`;
  }).join("");
  box.innerHTML = `<div class="bt-head"><div class="heading">引擎</div><button class="btn sm" onclick="btGroupNew()" title="几个引擎各开一个分支并行干，管家合并">${icon("plus")}群聊</button><button class="btn sm primary" onclick="btNew()">${icon("plus")}新对话</button></div>`
    + btGroupItems()
    + (items || `<div class="muted pad">还没有对话。新建一个，直接和电脑上的 Antigravity、Codex、Claude Code、CodeBuddy 或 Grok 说话；也可以把目标交给 AIW 管家，让它替你来回沟通。</div>`)
    + `<div class="small muted pad">${BT.engines.filter((e) => !e.available).map((e) => esc(e.title) + "：下一版接入").join(" · ")}</div>`;
  paintIcons(box);
}
function btIsCloud(id) { return !!(btEngine(id) || {}).cloud; }
function btNewToggleCloud(bot) {
  const cloud = btIsCloud(bot);
  const mEl = $("btNewMachine"), wEl = $("btNewWorkdir"), cEl = $("btNewCloudBar");
  if (mEl) mEl.style.display = cloud ? "none" : "";
  if (wEl) wEl.style.display = cloud ? "none" : "";
  if (cEl) {
    cEl.style.display = cloud ? "" : "none";
    if (cloud) {
      const eng = btEngine(bot);
      const ok = eng.configured;
      cEl.innerHTML = `<div class="row gap" style="align-items:center;justify-content:space-between;padding:8px 10px;border-radius:8px;background:var(--bg2,rgba(127,127,127,.08))">
        <span class="small">${esc(eng.title)}（云端）· ${ok ? "<b>已配置凭据</b>" : "<span class='muted'>未配置凭据（发消息时会自动尝试从浏览器读取）</span>"}</span>
        <button type="button" class="btn sm" onclick="btCloudAuth('${esc(bot)}')">配置 / 从浏览器读取</button>
      </div>`;
    }
  }
}
function btNew() {
  const opts = BT.engines.map((e) => `<option value="${esc(e.id)}" ${e.available ? "" : "disabled"}>${esc(e.title)}${e.cloud ? "（云端）" : ""}${e.available ? "" : "（即将支持）"}</option>`).join("");
  const first = (BT.engines.find((e) => e.available) || { id: "agy" }).id;
  modal(`<form class="callnote bt-form" onsubmit="btCreate(event)"><h3>新对话</h3>
      <label>引擎<select name="bot" onchange="btNewToggleCloud(this.value);btModelsFill('', '', false, this.value)">${opts}</select></label>
      <div id="btNewCloudBar" style="display:none"></div>
      <label>标题<input name="title" placeholder="例如 ldok 重建" /></label>
      <label id="btNewMachine">电脑<select name="machine">${btMachineOpts()}</select><small>自动：画 CAD、渲染或剪辑视频交给云电脑，写代码和其他事留在本地电脑。选云电脑时，它上面要装好并登录引擎。</small></label>
      <label id="btNewWorkdir">工作文件夹<input name="workdir" placeholder="~/workspace/ldok-rebuild" /><small>电脑上的文件夹，引擎就在这里改文件；空着就新建一个。</small></label>
      ${btModelFields()}
      <div class="row gap"><button class="btn primary">开始</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
  if (!(S.machines || []).length) api("GET", "/api/machines").then((ms) => { S.machines = ms; const m = document.querySelector("#modal select[name=machine]"); if (m) m.innerHTML = btMachineOpts(); }).catch(() => {});
  const sel = document.querySelector("#modal select[name=bot]"); if (sel) sel.value = first;
  btNewToggleCloud(first);
  btModelsFill("", "", false, first);
}
/* v3.9.73: which computer the conversation runs on; 自动 decides from the first message (CAD / video -> cloud) */
function btMachineOpts() {
  return `<option value="auto">自动（按任务选）</option>` + (S.machines || []).map((m) =>
    `<option value="${esc(m.id)}">${esc(m.name || m.id)}${m.provider === "instabox" && !/云/.test(m.name || "") ? "（云）" : ""}</option>`).join("");
}
async function btCreate(ev) {
  ev.preventDefault();
  const f = ev.target;
  try {
    const c = await api("POST", "/api/bots/convs", { bot: f.bot.value, title: f.title.value, workdir: f.workdir.value, machine_id: f.machine ? f.machine.value : "", ...btModelValue(f) });
    closeOverlays(); BT.convs.unshift(c); btOpen(c.id);
  } catch (e) { toast("没能新建：" + e.message); }
}
function btShowConv(on) { const v = $("v-bots"); if (v) v.classList.toggle("bt-open", !!on); }
async function btOpen(id, quiet) {
  BT.gid = null; BT.group = null;
  if (BT.cid !== id) { BT.msgs = []; BT.last = 0; BT.quote = null; BT.attach = []; BT.skills = []; btRenderAttach(); btRenderTokens(); }
  BT.cid = id; localStorage.setItem("aiw_bot_conv", id);
  btShowConv(true); btRenderList();
  await btRefresh(true);
  if (BT.conv && (BT.conv.bot || "agy") !== BM.bot) btModels(false, BT.conv.bot || "agy").then(() => btRenderConv()).catch(() => {});
  if (!quiet) { const el = $("btPrompt"); if (el && matchMedia("(hover:hover)").matches) el.focus(); }
}
function btBack() { btShowConv(false); BT.cid = null; BT.gid = null; BT.group = null; localStorage.removeItem("aiw_bot_conv"); btRenderList(); btStopPoll(); }
async function btRefresh(scroll) {
  if (BT.gid) return btGroupRefresh(scroll);
  if (!BT.cid) return;
  let r;
  try { r = await api("GET", `/api/bots/convs/${BT.cid}?after=0`); } catch (e) { return; }
  BT.conv = r.conv; BT.msgs = r.messages || [];
  const i = BT.convs.findIndex((c) => c.id === r.conv.id);
  if (i >= 0) BT.convs[i] = { ...BT.convs[i], ...r.conv };
  const box = $("btMsgs"), near = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  btRenderConv();
  if (scroll || near) btScroll();
  btSchedule();
}
function btSchedule() {
  btStopPoll();
  if (S.view !== "bots" || !(BT.cid || BT.gid)) return;
  const live = BT.gid ? BT.group && (BT.group.busy || BT.group.status !== "idle") : BT.conv && (BT.conv.busy || BT.conv.takeover || BT.conv.status === "running" || BT.conv.queued);
  BT.poll = setTimeout(() => btRefresh(false), live ? 2000 : 8000);
}
function btStopPoll() { if (BT.poll) { clearTimeout(BT.poll); BT.poll = null; } }
function btScroll() { const b = $("btMsgs"); if (b) b.scrollTop = b.scrollHeight; }

// v3.9.67: files an engine made on the computer (file:///home/aiw/workspace/…, ~/workspace/…): a link the phone can
// open. The API copies the file over (GET …/file) and it opens like any other output (preview, or download).
const BT_PATH_RE = /file:\/\/(\/[^\s)\]<>"'`]+)|((?:~|\/home\/[\w.-]+)\/workspace\/[^\s)\]<>"'`，。；]+\.[A-Za-z0-9]{1,6})/g;
function btPaths(text) {
  const out = [];
  for (const m of String(text || "").matchAll(BT_PATH_RE)) {
    let p = m[1] || m[2]; try { p = decodeURI(p); } catch (e) { /* keep */ }
    p = p.replace(/[.,;:]+$/, "");
    if (/\.[A-Za-z0-9]{1,6}$/.test(p) && !out.includes(p)) out.push(p);
  }
  return out.slice(0, 8);
}
const btFileLinks = (t) => String(t || "").replace(/\]\((file:\/\/[^)\s]+)\)/g, (_, u) => `](#aiwfile=${encodeURIComponent(u)})`);
function btFileCards(text) {
  return btPaths(text).map((p) => `<a class="bt-file" href="#aiwfile=${encodeURIComponent(p)}">${icon("file")}<span>${esc(p.split("/").pop())}</span><i>预览 / 下载</i></a>`).join("");
}
async function btFile(raw) {
  if (!BT.cid) return;
  toast("正在从电脑取文件…");
  try {
    const r = await api("GET", `/api/bots/convs/${BT.cid}/file?path=${encodeURIComponent(raw)}`);
    if (btMediaPreview(r)) return;
    const a = document.createElement("a");
    a.className = "filecard"; a.href = artUrl(r.path, true); a.setAttribute("download", r.name);
    a.innerHTML = `<b>${esc(r.name)}</b>`; a.style.display = "none";
    document.body.appendChild(a); a.click(); setTimeout(() => a.remove(), 1000);
  } catch (e) { toast(e.message); }
}
// v3.9.76: a video, picture, PDF or sound the engine made opens in a viewer (any size; the server streams it with
// Range), with a download button; other kinds keep downloading (Office / html / md go through mdpreview.js).
function btMediaPreview(r) {
  const n = String(r.name || "").toLowerCase();
  const kind = /\.(mp4|m4v|mov|webm)$/.test(n) ? "video" : /\.(png|jpe?g|gif|webp|svg|bmp)$/.test(n) ? "img"
    : /\.pdf$/.test(n) ? "pdf" : /\.(mp3|wav|m4a|aac|ogg|flac)$/.test(n) ? "audio" : "";
  if (!kind) return false;
  const src = artUrl(r.path), dl = artUrl(r.path, true);
  const inner = kind === "video" ? `<video src="${esc(src)}" controls autoplay playsinline style="max-width:100%;max-height:78vh;background:#000"></video>`
    : kind === "img" ? `<img src="${esc(src)}" alt="${esc(r.name)}" style="max-width:100%;max-height:78vh;object-fit:contain">`
    : kind === "audio" ? `<audio src="${esc(src)}" controls autoplay style="width:min(560px,90vw)"></audio>`
    : `<iframe src="${esc(src)}" title="${esc(r.name)}" style="width:min(1000px,94vw);height:80vh;border:0;background:#fff"></iframe>`;
  const m = document.createElement("div");
  m.className = "bt-media";
  m.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.82);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:16px";
  m.innerHTML = `${inner}<div style="display:flex;gap:10px;align-items:center;color:#fff;font-size:13px"><span>${esc(r.name)} · ${size(r.size || 0)}</span>
    <a class="btn sm primary" href="${esc(dl)}" download="${esc(r.name)}">下载</a><button class="btn sm" type="button">关闭</button></div>`;
  const close = () => { const v = m.querySelector("video,audio"); if (v) v.pause(); m.remove(); document.removeEventListener("keydown", key); };
  const key = (e) => { if (e.key === "Escape") close(); };
  m.addEventListener("click", (e) => { if (e.target === m || e.target.tagName === "BUTTON") close(); });
  document.addEventListener("keydown", key);
  document.body.appendChild(m);
  return true;
}
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest('a[href^="#aiwfile="]');
  if (!a) return;
  e.preventDefault(); e.stopPropagation();
  btFile(decodeURIComponent(a.getAttribute("href").slice(9)));
}, true);
function btMsg(m) {
  const d = m.data || {}, eng = btEngine((BT.conv || {}).bot);
  const q = typeof hQuoteShow === "function" ? hQuoteShow(d.quote) : "";
  const acts = `<span class="bt-acts"><button class="link" onclick="btQuote('${m.id}')">引用</button>${m.role === "bot" ? `<button class="link" onclick="btToChat('${m.id}')">问小秘</button>` : ""}</span>`;
  if (m.role === "system") return `<div class="bt-sys">${esc(m.text)}</div>`;
  if (m.role === "user" || m.role === "aiw") {
    return `<div class="bt-row me ${m.role}"><div class="bt-col">${m.role === "aiw" ? `<div class="bt-name">AIW 管家${d.report && typeof d.report === "string" ? " · " + esc(d.report) : ""}</div>` : ""}
      ${q}${(d.skills || []).length ? `<div>${d.skills.map((id) => `<span class="htok">✨ ${esc(hSkillTitle(id))}</span>`).join(" ")}</div>` : ""}
      <div class="bubble ${m.role === "aiw" ? "bt-aiw" : ""} ${d.report === true ? "hmd" : ""}">${d.report === true ? hMd(m.text) : esc(m.text)}</div>
      ${(d.attachments || []).length && typeof hAttPreview === "function" ? hAttPreview(d.attachments) : ""}
      ${(d.attachments || []).some((a) => !a.vm_path) ? `<div class="small" style="color:var(--bad)">有附件没能放到电脑上：${esc(d.attachments.filter((a) => !a.vm_path).map((a) => a.name).join("、"))}</div>` : ""}
      <div class="meta">${d.queued ? "排队中，它回完这一轮就发 · " : ""}${hTime(m.created_at)}${acts}</div></div></div>`;
  }
  // bot reply
  const running = d.status === "running";
  const steps = d.steps || [];
  const stepHtml = steps.length ? `<details class="bt-steps" ${running ? "open" : ""}><summary>${running ? "正在做" : "做了"} ${Math.max(Number(d.step_count) || 0, steps.length)} 步${(Number(d.step_count) || 0) > 15 ? "（显示最近 15 步）" : ""}</summary>
    ${steps.slice(-15).map((s) => `<div class="bt-step ${s.error ? "bad" : ""}"><b>${esc(s.tool || "")}</b> ${esc(s.arg || "")}${s.error ? ` <i>${esc(s.error)}</i>` : ""}</div>`).join("")}</details>` : "";
  const den = (d.denied || []).length ? `<div class="bt-denied">被拒绝的操作：${d.denied.map((x) => `${esc(x.tool || "")} ${esc(x.target || "")}（${esc(x.why || x.action || "")}）`).join("；")}</div>` : "";
  // v3.9.67: the newest reply stopped by agy's permission rules: the user decides (AIW never widens them itself)
  const lastBot = [...BT.msgs].reverse().find((x) => x.role === "bot");
  const permit = d.status === "blocked" && !d.permitted && lastBot && lastBot.id === m.id && (BT.conv || {}).bot === "agy"
    && !(BT.conv.busy || BT.conv.status === "running")
    ? `<div class="bt-permit"><button class="btn sm primary" onclick="btPermit('allow')">允许这类命令再试</button><button class="btn sm" onclick="btPermit('once')">这一轮放开权限重跑</button><button class="btn sm" onclick="btPermit('deny')">拒绝</button></div>` : "";
  const ch = d.changes && (d.changes.files || []).length ? `<div class="small muted">改动：${esc(d.changes.files.slice(0, 8).join("，"))}${d.changes.files.length > 8 ? " …" : ""}</div>` : "";
  const body = running && !m.text
    ? `<div class="bubble hdots-b"><span class="hdots"><i></i><i></i><i></i></span><span class="hnote">${esc(((d.said || [])[0] || "").slice(0, 80) || "在电脑上干活…")}</span></div>`
    : `<div class="bubble hmd ${["error", "failed", "blocked"].includes(d.status) ? "err" : ""}">${hMd(btFileLinks(m.text))}</div>`;
  const files = running ? "" : btFileCards(m.text);
  return `<div class="bt-row bot">${btAv((BT.conv || {}).bot)}<div class="bt-col">
    <div class="bt-name ${esc((BT.conv || {}).bot || "agy")}">${esc(eng.title)}${d.status === "blocked" ? " · 被权限挡住" : d.status === "stopped" ? " · 已停止" : ""}</div>
    ${body}${files ? `<div class="bt-files">${files}</div>` : ""}${stepHtml}${den}${permit}${ch}<div class="meta">${hTime(m.created_at)}${running ? "" : acts}</div></div></div>`;
}
function btRenderConv() {
  const c = BT.conv; if (!c) return;
  const eng = btEngine(c.bot), cloud = !!eng.cloud, tk = c.takeover, live = c.busy || c.status === "running";
  $("btHead").innerHTML = `<button class="icon only-mobile" onclick="btBack()" aria-label="返回">${icon("back")}</button>
    <div class="grow"><div class="heading" onclick="btRename()" title="点一下改名">${esc(c.title)}</div>
      <div class="small muted bt-sub">${esc(eng.title)}<span class="bt-wd">${c.machine_label ? " · " + esc(c.machine_label) : ""}${cloud ? "" : " · " + esc(c.workdir || "")}${c.session ? " · 会话已保存" : ""}</span></div></div>
    ${cloud ? `<button class="btn sm" onclick="btCloudAuth('${esc(c.bot)}')" title="配置凭据或从浏览器读取">凭据</button>` : ""}
    <button class="btn sm bt-model" onclick="btModelEdit()" title="换模型和强度">${esc(btModelName(c))}</button>
    ${tk ? `<button class="btn sm" onclick="btRelease()">收回</button>` : `<button class="btn sm primary" onclick="btTake()">交给管家</button>`}
    ${live ? `<button class="btn sm" onclick="btStop()">停止</button>` : ""}
    <button class="icon" onclick="btDelete()" aria-label="删除对话" title="删除对话">${icon("x")}</button>`;
  $("btTake").innerHTML = tk ? `<div class="bt-tk"><b>AIW 管家接管中</b>（第 ${tk.rounds}/${tk.max} 轮）：${esc(tk.goal)}<br><span class="small muted">你随时可以插话，它会先发你的话再继续；点「收回」自己接手。</span></div>` : "";
  $("btMsgs").innerHTML = BT.msgs.length ? BT.msgs.map(btMsg).join("")
    : `<div class="muted pad center">${cloud ? `直接对 ${esc(eng.title)} 说话；如尚未登录可点右上角「凭据」从浏览器读取。` : `直接说要它做什么。它在电脑的 ${esc(c.workdir || "")} 里干活，改动不会自动推送。`}</div>`;
  $("btPrompt").placeholder = matchMedia("(max-width: 760px)").matches ? "对它说，或输入 / 选技能…" : "对它说，或输入 / 选技能…（管家接管时也可以插话）";
  btQuoteRender();
  paintIcons($("btHead"));
}
async function btSend(ev) {
  if (ev) ev.preventDefault();
  const el = $("btPrompt"), text = el.value.trim();
  if (!(BT.cid || BT.gid) || BT.sending) return;
  if (BT.attach.some((a) => a.up)) { toast("附件还在上传，传完再发"); return; }
  if (!text && !BT.attach.length) { if (BT.skills.length) toast("说说要用这个技能做什么"); return; }
  BT.sending = true;
  const quote = BT.quote ? BT.quote.id : null, skills = BT.skills.slice();
  const attachments = BT.attach.map(({ up, pct, uid, ...a }) => a);
  try {
    if (BT.gid) await api("POST", `/api/bots/groups/${BT.gid}/send`, { text, attachments });
    else await api("POST", `/api/bots/convs/${BT.cid}/send`, { text, quote, attachments, skills });
    el.value = ""; autoGrow(el); BT.quote = null; BT.attach = []; BT.skills = []; btRenderAttach(); btRenderTokens();
    await btRefresh(true);
  } catch (e) { toast("没发出去：" + e.message); }
  finally { BT.sending = false; }
}
function btKey(e) {
  if (BK.open) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); btSkMove(e.key === "ArrowDown" ? 1 : -1); return; }
    if (e.key === "Enter" || e.key === "Tab") { const r = BK.rows[BK.sel]; if (r) { e.preventDefault(); btSkChoose(r.id); return; } }
    if (e.key === "Escape") { e.preventDefault(); btSkClose(); return; }
  }
  const t = e.target;
  if (e.key === "Backspace" && BT.skills.length && t.selectionStart === 0 && t.selectionEnd === 0) { e.preventDefault(); btSkRemove(BT.skills[BT.skills.length - 1]); return; }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(hover:hover)").matches) { e.preventDefault(); btSend(); }
}
function btInput(el) { autoGrow(el); btSlashCheck(el); }

/* v3.9.58: the chat's attachments and skills, in the engine composer. Files go up like the chat's (POST /api/uploads/raw,
   kept on the API host); the send copies them into the session folder on the computer and names the paths in the
   prompt. Skills go as the task's skill brief (agent_runtime/skills.py); the bridge copies each skill into .aiw-context/. */
function btMenu(on) {
  const m = $("btMenu"); if (!m) return;
  if (on === undefined) on = m.hidden;
  m.hidden = !on;
  if (on) { btSkClose(); m.querySelectorAll("[data-hi]").forEach((x) => { if (!x.innerHTML) x.innerHTML = icon(x.dataset.hi); }); }
}
document.addEventListener("click", (e) => { const m = $("btMenu"); if (m && !m.hidden && !e.target.closest("#btMenu, .bt-cw .hplus")) m.hidden = true; });
function btPick(id) { btMenu(false); $(id).click(); }
async function btUpload(list, input) {
  const files = [...(list || [])];
  if (input) input.value = "";
  for (const f of files) {
    if (f.size > H_MAX_MB * 1048576) { toast(`${f.name} 超过 ${H_MAX_MB} MB`); continue; }
    const kind = (f.type || "").startsWith("video") ? "video" : (f.type || "").startsWith("image") ? "image" : "";
    const a = { uid: "u" + Date.now() + Math.random(), name: f.name || "file", size: f.size, kind, up: true, pct: 0 };
    BT.attach.push(a); btRenderAttach();
    try {
      const r = await hRawUpload(f, (p) => { a.pct = p; btRenderAttach(); });
      Object.assign(a, r, { kind: r.kind || kind, up: false });
    } catch (e) { BT.attach = BT.attach.filter((x) => x !== a); toast(`上传失败：${f.name}：${e.message}`); }
    btRenderAttach();
  }
}
function btRemoveAttach(i) { BT.attach.splice(i, 1); btRenderAttach(); }
function btRenderAttach() {
  const box = $("btAttach"); if (!box) return;
  box.innerHTML = BT.attach.map((a, i) => `<span class="chip ${a.up ? "up" : ""}">${hAttIcon(a)} ${esc(a.name)}${a.up ? `<span class="hpct">${a.pct || 0}%</span>` : ""}<button class="link" aria-label="移除" onclick="btRemoveAttach(${i})">${icon("x")}</button></span>`).join("");
}
const BK = { open: false, sel: 0, q: "", start: -1, rows: [], pack: null };
function btSlashCheck(el) {
  const pos = el.selectionStart == null ? el.value.length : el.selectionStart;
  const m = el.value.slice(0, pos).match(/(^|\s)\/([^\s/]*)$/);
  if (!m) { btSkClose(); return; }
  BK.start = pos - m[2].length - 1; BK.q = m[2].toLowerCase();
  btSkOpen();
}
async function btSkOpen() {
  const list = await hSkillList(), q = BK.q;
  const hit = (s) => !q || [s.id, s.title, s.desc].some((v) => String(v || "").toLowerCase().includes(q));
  if (!q && !BK.pack) BK.rows = skillGroups(list).map((g) => g.items ? { id: "pack:" + g.pack, title: g.title, desc: `技能包 · ${g.items.length} 个技能，回车展开`, pack: g.pack } : g).slice(0, 40);
  else if (BK.pack) BK.rows = [{ id: "pack:", title: "← 返回", desc: "" }].concat(list.filter((s) => s.pack === BK.pack && hit(s)));
  else BK.rows = list.filter(hit).sort((a, b) => (String(a.id).toLowerCase().startsWith(q) ? 0 : 1) - (String(b.id).toLowerCase().startsWith(q) ? 0 : 1)).slice(0, 30);
  BK.sel = Math.min(BK.sel, Math.max(0, BK.rows.length - 1)); BK.open = true; btSkRender();
}
function btSkRender() {
  const box = $("btSkills"); if (!box) return;
  box.hidden = !BK.open; if (!BK.open) return;
  box.innerHTML = `<div class="hsk-hd">技能${BK.q ? ` · “${esc(BK.q)}”` : ""}<span>↑↓ 选择 · 回车确定 · Esc 关闭</span></div>`
    + (BK.rows.map((s, i) => `<div class="hsk-r ${i === BK.sel ? "on" : ""}" role="option" aria-selected="${i === BK.sel}" data-id="${esc(s.id)}"
        onmousedown="event.preventDefault()" onclick="btSkChoose(this.dataset.id)"><span class="n">${s.pack ? "▸ " + esc(s.pack.split("/").pop()) : s.id === "pack:" ? "←" : "/" + esc(s.id)}</span><span class="g"><b>${esc(s.title || s.id)}</b><small>${esc(s.desc || "")}</small></span></div>`).join("")
    || `<div class="hsk-none">没有匹配的技能</div>`);
  const on = box.querySelector(".hsk-r.on"); if (on) on.scrollIntoView({ block: "nearest" });
}
function btSkMove(d) { if (!BK.rows.length) return; BK.sel = (BK.sel + d + BK.rows.length) % BK.rows.length; btSkRender(); }
function btSkClose() { BK.pack = null; if (!BK.open) return; BK.open = false; BK.sel = 0; btSkRender(); }
function btSkChoose(id) {
  const el = $("btPrompt");
  if (String(id).startsWith("pack:")) { BK.pack = id.slice(5) || null; BK.sel = 0; btSkOpen(); el.focus(); return; }
  const pos = el.selectionStart == null ? el.value.length : el.selectionStart;
  if (BK.start >= 0 && el.value[BK.start] === "/") { el.value = el.value.slice(0, BK.start) + el.value.slice(pos); el.selectionStart = el.selectionEnd = BK.start; }
  if (!BT.skills.includes(id)) BT.skills.push(id);
  BK.start = -1; btSkClose(); btRenderTokens(); autoGrow(el); el.focus();
}
function btSkRemove(id) { BT.skills = BT.skills.filter((x) => x !== id); btRenderTokens(); $("btPrompt").focus(); }
function btRenderTokens() {
  const box = $("btTokens"); if (!box) return;
  box.innerHTML = BT.skills.map((id) => `<span class="htok">✨ ${esc(hSkillTitle(id))}<button type="button" aria-label="移除技能" onclick="btSkRemove('${esc(id)}')">×</button></span>`).join("");
  $("btPrompt").placeholder = BT.skills.length ? "说说要用它做什么…" : "对它说，或输入 / 选技能…（管家接管时也可以插话）";
}
function btSlash() {
  const el = $("btPrompt"); el.focus();
  const pos = el.selectionStart == null ? el.value.length : el.selectionStart, pre = el.value.slice(0, pos);
  const ins = (pre && !/\s$/.test(pre) ? " " : "") + "/";
  el.value = pre + ins + el.value.slice(pos); el.selectionStart = el.selectionEnd = pos + ins.length;
  btInput(el);
}
function btQuote(id) {
  const m = BT.msgs.find((x) => x.id === id); if (!m) return;
  BT.quote = { id, who: m.role === "bot" ? btEngine(BT.conv.bot).title : (BT_WHO[m.role] || m.role), text: m.text || "" };
  btQuoteRender(); $("btPrompt").focus();
}
function btQuoteRender() {
  const box = $("btQuote"); if (!box) return;
  const q = BT.quote; box.hidden = !q;
  box.innerHTML = q ? `<div class="hquote"><b>${esc(q.who)}</b>${esc(q.text.replace(/\s+/g, " ").slice(0, 120))}</div><button type="button" class="link" aria-label="取消引用" onclick="BT.quote=null;btQuoteRender()">×</button>` : "";
}
function btToChat(id) {   // quote an engine reply in the main chat: 小秘 sees it and what was said around it
  const m = BT.msgs.find((x) => x.id === id); if (!m || typeof hQuoteSet !== "function") return;
  go("home"); hQuoteSet(m, btEngine(BT.conv.bot).title);
}
function btTake() {
  modal(`<form class="callnote bt-form" onsubmit="btTakeGo(event)"><h3>交给 AIW 管家</h3>
      <div class="muted">管家会替你和 ${esc(btEngine(BT.conv.bot).title)} 来回沟通，直到目标完成、需要你决定或轮数用完。它不会让对方推送、合并或删除东西。</div>
      <label>目标和验收标准<textarea name="goal" rows="4" placeholder="例如：把首页加载改成懒加载，跑通 npm test，给我改动摘要" required></textarea></label>
      <label>最多来回几轮<input name="rounds" type="number" min="1" max="30" value="8" /></label>
      <div class="row gap"><button class="btn primary">交给管家</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
}
async function btTakeGo(ev) {
  ev.preventDefault();
  try { await api("POST", `/api/bots/convs/${BT.cid}/takeover`, { goal: ev.target.goal.value, rounds: +ev.target.rounds.value || 8 }); closeOverlays(); btRefresh(true); }
  catch (e) { toast("没能交给管家：" + e.message); }
}
async function btPermit(mode) {
  let rule = "";
  if (mode === "allow") {
    const last = [...BT.msgs].reverse().find((x) => x.role === "bot") || {};
    const den = ((last.data || {}).denied || []).slice(-1)[0] || {};
    if (!den.rule) { toast("没抓到被拦的具体命令，用「这一轮放开权限重跑」"); return; }
    const r = prompt("把这条规则加进 agy 的权限设置（以后也算数，可以改窄一点）：", den.rule);
    if (!r) return; rule = r.trim();
  } else if (mode === "once" && !confirm("这一轮不做权限检查，让它把被拦的那一步做完？下一轮恢复原来的规则。")) return;
  try { await api("POST", `/api/bots/convs/${BT.cid}/permit`, { mode, rule }); toast(mode === "allow" ? "已允许，接着做" : mode === "deny" ? "已拒绝" : "这一轮已放开，接着做"); btRefresh(true); }
  catch (e) { toast(e.message); }
}
async function btRelease() { try { await api("DELETE", `/api/bots/convs/${BT.cid}/takeover`); btRefresh(true); } catch (e) { toast(e.message); } }
async function btStop() { try { await api("POST", `/api/bots/convs/${BT.cid}/stop`); toast("已停止"); btRefresh(true); } catch (e) { toast("停止失败：" + e.message); } }
async function btDelete() {
  if (!confirm("删除这个对话？（电脑上的文件夹和文件不会删）")) return;
  try { await api("DELETE", `/api/bots/convs/${BT.cid}`); BT.convs = BT.convs.filter((c) => c.id !== BT.cid); btBack(); } catch (e) { toast(e.message); }
}
async function btRename() {
  const t = prompt("对话名称", BT.conv.title); if (!t) return;
  try { await api("PATCH", `/api/bots/convs/${BT.cid}`, { title: t }); btRefresh(false); btLoad(); } catch (e) { toast(e.message); }
}

/* v3.9.58: model and strength. GET /api/bots/engines/agy/models runs `agy models` on the computer (this account's
   models), grouped: gemini-4-neon-high/-low… is one model with strengths; a model without variants takes --effort.
   Changing it mid-conversation restarts agy on the next turn with --conversation, so the session carries on. */
const BM = { bot: "agy", groups: null, efforts: null, loading: null, cache: {} };   // v3.9.60: one list per engine
const BT_EFF = { minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "超高", max: "最高" };
async function btModels(refresh, bot) {
  bot = bot || BM.bot;
  if (bot !== BM.bot) { BM.bot = bot; BM.loading = null; const hit = BM.cache[bot]; BM.groups = hit ? hit.groups : null; BM.efforts = hit ? hit.efforts : null; }
  if (BM.groups && !refresh) return BM.groups;
  if (!BM.loading || refresh) BM.loading = api("GET", `/api/bots/engines/${encodeURIComponent(bot)}/models` + (refresh ? "?refresh=1" : ""))
    .then((r) => {
      BM.cache[bot] = { groups: r.groups || [], efforts: r.efforts || Object.keys(BT_EFF) };
      if (BM.bot === bot) { BM.groups = BM.cache[bot].groups; BM.efforts = BM.cache[bot].efforts; }
      return BM.cache[bot].groups;
    }).catch((e) => { BM.loading = null; throw e; });
  return BM.loading;
}
function btGroupOf(model) {
  for (const g of BM.groups || []) {
    if (g.id === model) return { g, eff: "" };
    for (const [e, slug] of Object.entries(g.slugs || {})) if (slug === model) return { g, eff: e };
  }
  return null;
}
function btModelName(c) {
  if (!c.model && !c.effort) return "默认模型";
  const hit = c.model ? btGroupOf(c.model) : null;
  const name = hit ? hit.g.label : (c.model || "默认模型"), eff = (hit && hit.eff) || c.effort;
  return name + (eff ? " · " + (BT_EFF[eff] || eff) : "");
}
function btModelFields() {
  return `<label><span class="bt-lh">模型<button type="button" class="link small" onclick="btModelsFill(null,null,true)">刷新列表</button></span>
      <select name="mgroup" id="btMG" onchange="btEffFill()"><option value="">读取中…</option></select>
      <input name="mcustom" id="btMC" placeholder="模型名，例如 gpt-5-codex、opus、hy4-preview" style="display:none" />
      <small id="btMNote"></small></label>
    <label id="btMEL">强度<select name="meff" id="btME"><option value="">默认</option></select><small>越高越慢、越仔细。</small></label>`;
}
async function btModelsFill(model, effort, refresh, bot) {
  const sel = $("btMG"); if (!sel) return;
  if (bot && bot !== sel.dataset.bot) {   // another engine: drop the old engine's list at once, the new one may take a while
    sel.dataset.bot = bot;
    const hit = BM.cache[bot];
    if (!hit || refresh) { sel.innerHTML = `<option value="">读取 ${esc(btEngine(bot).title)} 的模型…</option>`; sel.disabled = true; $("btMNote").textContent = ""; btEffFill(""); }
  }
  bot = sel.dataset.bot || BM.bot;
  if (refresh) { sel.innerHTML = `<option value="">读取 ${esc(btEngine(bot).title)} 的模型…</option>`; sel.disabled = true; }
  if (model === null) { model = sel.dataset.model || ""; effort = sel.dataset.effort || ""; }
  sel.dataset.model = model || ""; sel.dataset.effort = effort || "";
  let groups = [];
  try { groups = await btModels(refresh, bot); $("btMNote").textContent = ""; }
  catch (e) { $("btMNote").textContent = "读不到模型列表（" + e.message + "），可以先用默认，或选「其他」手动填"; }
  if ((sel.dataset.bot || BM.bot) !== bot) return;           // the engine changed while this list loaded
  const hit = model ? btGroupOf(model) : null, title = btEngine(bot).title;
  sel.disabled = false;
  sel.innerHTML = `<option value="">默认（${esc(title)} 自己的设置）</option>` + groups.map((g) => `<option value="${esc(g.id)}">${esc(g.label)}</option>`).join("")
    + (model && !hit ? `<option value="${esc(model)}">${esc(model)}</option>` : "") + `<option value="__custom">其他（手动填模型名）</option>`;
  sel.value = hit ? hit.g.id : (model || "");
  if (!groups.length && !$("btMNote").textContent) $("btMNote").textContent = `${title} 没有给出模型列表，用默认或手动填。`;
  btEffFill((hit && hit.eff) || effort || "");
}
function btEffFill(want) {
  const sel = $("btMG"), es = $("btME"); if (!sel || !es) return;
  const bot = sel.dataset.bot || BM.bot, cached = BM.cache[bot];
  const mc = $("btMC"); if (mc) mc.style.display = sel.value === "__custom" ? "" : "none";
  const g = (cached ? cached.groups : []).find((x) => x.id === sel.value);
  const own = bot === "agy" ? Object.keys(BT_EFF).filter((e) => e !== "minimal") : (cached ? cached.efforts : []);
  const effs = g && (g.efforts || []).length ? g.efforts : own;
  if (want === undefined) want = es.value;
  const box = $("btMEL"); if (box) box.style.display = effs.length ? "" : "none";
  const plain = !g || !(g.efforts || []).length || g.plain;
  es.innerHTML = (plain ? `<option value="">默认</option>` : "") + effs.map((e) => `<option value="${e}">${BT_EFF[e] || e}（${e}）</option>`).join("");
  es.value = effs.includes(want) ? want : plain ? "" : (effs.includes("high") ? "high" : effs[effs.length - 1]);
}
function btModelValue(f) {
  let gid = f.mgroup ? f.mgroup.value : "";
  const eff = f.meff && $("btMEL") && $("btMEL").style.display !== "none" ? f.meff.value : "";
  if (gid === "__custom") gid = f.mcustom ? f.mcustom.value.trim() : "";
  const g = (BM.groups || []).find((x) => x.id === gid);
  if (g && eff && (g.slugs || {})[eff]) return { model: g.slugs[eff], effort: "" };   // the slug names its strength
  return { model: gid, effort: eff };
}
function btModelEdit() {
  const c = BT.conv; if (!c) return;
  modal(`<form class="callnote bt-form" onsubmit="btModelSave(event)"><h3>模型和强度</h3>
      <div class="muted">从下一句起生效：引擎会带着原会话接着聊，前面聊的都还在。</div>${btModelFields()}
      <div class="row gap"><button class="btn primary">保存</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
  btModelsFill(c.model || "", c.effort || "", false, c.bot || "agy");
}
async function btModelSave(ev) {
  ev.preventDefault();
  try { await api("PATCH", `/api/bots/convs/${BT.cid}`, btModelValue(ev.target)); closeOverlays(); btRefresh(true); }
  catch (e) { toast("没改成：" + e.message); }
}

/* navigation */
TITLES.bots = "引擎";
const _goBots = go;
go = function (view) {
  const r = _goBots.apply(this, arguments);
  if (view === "bots") btLoad(); else btStopPoll();
  return r;
};

/* v3.9.61 引擎群聊: several engines on one git repository, each on its own branch (aiw/<group>/<engine>) in its own
   worktree; @codex sends to one member, no @ (or @管家) lets AIW 管家 split the work; when they are done 管家 merges
   the branches into aiw/<group>/merge (nothing pushed, the base branch untouched).
   GET|POST /api/bots/groups, GET|DELETE /api/bots/groups/{id}, POST …/send|merge|stop. */
BT.groups = []; BT.gid = null; BT.group = null; BT.gmsgs = [];
const BT_GST = { working: "在干活", merging: "管家在合并", fixing: "在解决冲突", idle: "" };
async function btLoadGroups() {
  try { BT.groups = (await api("GET", "/api/bots/groups")).groups || []; } catch (e) { BT.groups = []; }
}
function btGroupItems() {
  if (!BT.groups.length) return "";
  return `<div class="small muted pad" style="padding-bottom:0">群聊</div>` + BT.groups.map((g) => {
    const st = BT_GST[g.status] || "";
    return `<button class="bt-it ${g.id === BT.gid ? "on" : ""}" onclick="btGroupOpen('${g.id}')">
      <span class="bt-av grp">${g.members.length}</span>
      <span class="grow"><span class="t1">${esc(g.title)}</span><span class="t2">${st ? `<b>${st}</b> · ` : ""}${g.last ? esc((g.last.role === "bot" ? btEngine(g.last.who).title + "：" : "") + g.last.text) : esc(g.members.map((m) => btEngine(m.bot).title).join("、"))}</span></span></button>`;
  }).join("") + `<div class="small muted pad" style="padding-bottom:0">对话</div>`;
}
function btGroupToggleGit() {
  const form = document.querySelector("#modal form"); if (!form) return;
  const picked = [...form.querySelectorAll("input[name=m]:checked")].map((x) => x.value);
  const hasLocal = picked.some((id) => !btIsCloud(id));
  const gitBox = $("btGrpGit"); if (gitBox) gitBox.style.display = hasLocal ? "" : "none";
}
function btGroupNew() {
  const boxes = BT.engines.filter((e) => e.available).map((e) => `<label class="bt-chk"><input type="checkbox" name="m" value="${esc(e.id)}" onchange="btGroupToggleGit()" ${["codex", "claude"].includes(e.id) ? "checked" : ""}/>${esc(e.title)}${e.cloud ? "（云端）" : ""}</label>`).join("");
  modal(`<form class="callnote bt-form" onsubmit="btGroupCreate(event)"><h3>新群聊</h3>
      <div class="muted">本地代码引擎各开独立分支并行开发，云端引擎（Meta Muse / Cue）参与调研、方案与评审；也可以组建纯云端讨论群。</div>
      <div class="bt-fl">成员<div class="bt-chks">${boxes}</div></div>
      <label>标题<input name="title" placeholder="例如 登录页改版" /></label>
      <div id="btGrpGit">
        <label>git 仓库<input name="repo" placeholder="~/workspace/ldok" /><small>电脑上的仓库；空着就新建一个。</small></label>
        <label>基线分支<input name="base" placeholder="main" /><small>空着用仓库当前分支。</small></label>
        <label class="bt-chk"><input type="checkbox" name="auto" checked />本地代码成员都做完后管家自动合并</label>
      </div>
      <label>要做什么<textarea name="goal" rows="3" placeholder="例如：做一个登录页，后端加接口，前端做页面，跑通测试"></textarea><small>可以先空着，进群再说。</small></label>
      <div class="row gap"><button class="btn primary">建群</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
  btGroupToggleGit();
}
async function btGroupCreate(ev) {
  ev.preventDefault();
  const f = ev.target, members = [...f.querySelectorAll("input[name=m]:checked")].map((x) => x.value);
  if (!members.length) { toast("至少选一个引擎"); return; }
  const btn = f.querySelector(".btn.primary"); btn.disabled = true; btn.textContent = "正在建工作树…";
  try {
    const g = await api("POST", "/api/bots/groups", { title: f.title.value, repo: f.repo.value, base: f.base.value, members, auto_merge: f.auto.checked, goal: f.goal.value });
    closeOverlays(); BT.groups.unshift(g); btGroupOpen(g.id);
  } catch (e) { toast("没能建群：" + e.message); btn.disabled = false; btn.textContent = "建群"; }
}
async function btGroupOpen(id, quiet) {
  if (BT.gid !== id) { BT.gmsgs = []; BT.quote = null; BT.attach = []; BT.skills = []; btRenderAttach(); btRenderTokens(); }
  BT.cid = null; BT.conv = null; BT.gid = id; localStorage.setItem("aiw_bot_conv", "g:" + id);
  btShowConv(true); btRenderList();
  await btGroupRefresh(true);
  if (!quiet) { const el = $("btPrompt"); if (el && matchMedia("(hover:hover)").matches) el.focus(); }
}
async function btGroupRefresh(scroll) {
  if (!BT.gid) return;
  let r;
  try { r = await api("GET", `/api/bots/groups/${BT.gid}?after=0`); } catch (e) { return; }
  if (r.group.id !== BT.gid) return;
  BT.group = r.group; BT.gmsgs = r.messages || [];
  const i = BT.groups.findIndex((g) => g.id === r.group.id);
  if (i >= 0) BT.groups[i] = { ...BT.groups[i], ...r.group };
  const box = $("btMsgs"), near = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  btGroupRender();
  if (scroll || near) btScroll();
  btSchedule();
}
function btGroupRender() {
  const g = BT.group; if (!g) return;
  const st = BT_GST[g.status] || "", live = g.busy || g.status !== "idle";
  const hasLocal = (g.members || []).some((m) => !!m.branch);
  const existingIds = new Set((g.members || []).map((m) => m.bot));
  const canAdd = BT.engines.some((e) => e.available && !existingIds.has(e.id));
  const chips = g.members.map((m) => `<button class="link small" onclick="btOpen('${m.conv}')" title="打开 ${esc(btEngine(m.bot).title)} 的对话${m.branch ? `（分支 ${esc(m.branch)}）` : "（云端）"}">${btAv(m.bot, "sm")}${m.busy ? "…" : ""}</button>`).join(" ");
  const sub = g.repo ? `${esc(g.repo)} · 基线 ${esc(g.base)}` : "云端协作群";
  $("btHead").innerHTML = `<button class="icon only-mobile" onclick="btBack()" aria-label="返回">${icon("back")}</button>
    <div class="grow"><div class="heading">${esc(g.title)}</div>
      <div class="small muted">${sub}${st ? " · <b>" + st + "</b>" : ""}</div></div>
    <span class="row gap">${chips}</span>
    ${canAdd ? `<button class="btn sm" onclick="btGroupAddMembers()" title="添加其他引擎（如 Muse / Cue）加入群聊">+ 成员</button>` : ""}
    ${hasLocal ? `<button class="btn sm primary" onclick="btGroupMerge()" ${g.status === "merging" ? "disabled" : ""} title="提交各分支的改动，合并到 aiw/${esc(g.id)}/merge">合并</button>` : ""}
    ${live ? `<button class="btn sm" onclick="btGroupStop()">停止</button>` : ""}
    <button class="icon" onclick="btGroupDelete()" aria-label="删除群聊" title="删除群聊">${icon("x")}</button>`;
  $("btTake").innerHTML = (g.pending || []).length ? `<div class="bt-tk"><b>${st || "在干活"}</b>：等 ${esc(g.pending.map((b) => btEngine(b).title).join("、"))} 回复${g.auto_merge && hasLocal ? "，都回复后管家自动合并" : ""}</div>` : "";
  $("btMsgs").innerHTML = BT.gmsgs.length ? BT.gmsgs.map(btGMsg).join("")
    : `<div class="muted pad center">说要做什么：不 @ 就由 AIW 管家分工；@muse @cue @codex 只发给指定成员。</div>`;
  $("btPrompt").placeholder = "说要做什么，@muse @cue @codex @claude @grok @cbc @agy 指定谁做，@管家 分工…";
  btQuoteRender();
  paintIcons($("btHead"));
}
function btGMsg(m) {
  const d = m.data || {};
  if (m.role === "system") return `<div class="bt-sys">${esc(m.text)}</div>`;
  if (m.role === "user" || m.role === "aiw") {
    return `<div class="bt-row me ${m.role}"><div class="bt-col">${m.role === "aiw" ? `<div class="bt-name">AIW 管家</div>` : ""}
      <div class="bubble ${m.role === "aiw" ? "bt-aiw hmd" : ""}">${m.role === "aiw" ? hMd(m.text) : esc(m.text)}</div>
      ${(d.attachments || []).length && typeof hAttPreview === "function" ? hAttPreview(d.attachments) : ""}
      <div class="meta">${hTime(m.created_at)}</div></div></div>`;
  }
  const eng = btEngine(m.who);
  const ch = d.changes && (d.changes.files || []).length ? `<div class="small muted">改动：${esc(d.changes.files.slice(0, 8).join("，"))}${d.changes.files.length > 8 ? " …" : ""}</div>` : "";
  const den = (d.denied || []).length ? `<div class="bt-denied">被拒绝的操作：${d.denied.map((x) => `${esc(x.tool || "")} ${esc(x.target || "")}`).join("；")}</div>` : "";
  return `<div class="bt-row bot">${btAv(m.who)}<div class="bt-col">
    <div class="bt-name ${esc(m.who)}">${esc(eng.title)}${["error", "failed", "blocked"].includes(d.status) ? " · 没做成" : d.status === "stopped" ? " · 已停止" : ""}</div>
    <div class="bubble hmd ${["error", "failed", "blocked"].includes(d.status) ? "err" : ""}">${hMd(m.text || "（没有文字）")}</div>${ch}${den}
    <div class="meta">${hTime(m.created_at)}${d.conv ? ` · <button class="link" onclick="btOpen('${d.conv}')">看它的对话 ›</button>` : ""}</div></div></div>`;
}
async function btGroupMerge() { try { await api("POST", `/api/bots/groups/${BT.gid}/merge`); toast("管家开始合并"); setTimeout(() => btGroupRefresh(true), 800); } catch (e) { toast("合并失败：" + e.message); } }
async function btGroupStop() { try { await api("POST", `/api/bots/groups/${BT.gid}/stop`); toast("已停止"); btGroupRefresh(true); } catch (e) { toast("停止失败：" + e.message); } }
async function btGroupDelete() {
  if (!confirm("删除这个群聊和成员的对话？（电脑上的仓库、分支和工作树不会删）")) return;
  try { await api("DELETE", `/api/bots/groups/${BT.gid}`); BT.groups = BT.groups.filter((g) => g.id !== BT.gid); btBack(); } catch (e) { toast(e.message); }
}
function btGroupAddMembers() {
  const g = BT.group; if (!g) return;
  const existing = new Set((g.members || []).map((m) => m.bot));
  const avail = BT.engines.filter((e) => e.available && !existing.has(e.id));
  if (!avail.length) { toast("所有引擎都已在群里"); return; }
  const boxes = avail.map((e) => `<label class="bt-chk"><input type="checkbox" name="m" value="${esc(e.id)}" ${e.cloud ? "checked" : ""}/>${esc(e.title)}${e.cloud ? "（云端）" : ""}</label>`).join("");
  modal(`<form class="callnote bt-form" onsubmit="btGroupAddSubmit(event)"><h3>添加成员到「${esc(g.title)}」</h3>
      <div class="muted">云端引擎（Meta Muse / Cue）会直接加入讨论；本地代码引擎会自动补建独立工作树。</div>
      <div class="bt-fl">选择引擎<div class="bt-chks">${boxes}</div></div>
      <div class="row gap"><button class="btn primary">添加</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
}
async function btGroupAddSubmit(ev) {
  ev.preventDefault();
  const f = ev.target, members = [...f.querySelectorAll("input[name=m]:checked")].map((x) => x.value);
  if (!members.length) { toast("至少选一个引擎"); return; }
  const btn = f.querySelector(".btn.primary"); btn.disabled = true; btn.textContent = "正在添加…";
  try {
    await api("POST", `/api/bots/groups/${BT.gid}/members`, { members });
    closeOverlays(); toast("已添加到群聊"); await btGroupRefresh(true);
  } catch (e) { toast("添加失败：" + e.message); btn.disabled = false; btn.textContent = "添加"; }
}
async function btCloudAuth(bot) {
  const eng = btEngine(bot);
  let info = {};
  try { info = await api("GET", `/api/bots/engines/${bot}/auth`); } catch (e) { toast("读取凭据状态失败：" + e.message); return; }
  const isCue = bot === "cue";
  const statusLine = info.configured
    ? `<b>已配置</b>${info.handle ? " · " + esc(info.handle) : info.viewer_id ? " · " + esc(info.viewer_id) : ""}${info.source ? " · 来源：" + esc(info.source) : ""}`
    : `<span class="muted">尚未配置</span>`;
  modal(`<form class="callnote bt-form" onsubmit="btCloudAuthSave(event, '${esc(bot)}')">
      <h3>${esc(eng.title)} 凭据设置</h3>
      <div class="small" id="btAuthStatus">状态：${statusLine}</div>
      <div class="row gap" style="margin:6px 0">
        <button type="button" class="btn sm primary" onclick="btCloudAuthBrowser('${esc(bot)}')">从本机浏览器读取</button>
        <button type="button" class="btn sm" onclick="btCloudAuthTest('${esc(bot)}')">测试连接</button>
        ${info.configured ? `<button type="button" class="btn sm" onclick="btCloudAuthClear('${esc(bot)}')">清除凭据</button>` : ""}
      </div>
      <small class="muted">先在 Chrome / Arc / Edge / BrowserOS 登录 ${isCue ? "cue.im" : "muse.ai"}，再点「从本机浏览器读取」即可自动导入登录态。</small>
      ${isCue ? `
        <label>Manus API Key（OpenAPI 模式，可选）<input name="api_key" type="password" placeholder="${info.has_api_key ? esc(info.api_key_preview) + "（留空保持不变）" : "粘贴 Manus API Key"}" /></label>
        <label>连接模式<select name="mode">
          <option value="auto" ${(info.mode || "auto") === "auto" ? "selected" : ""}>自动（有 API Key 优先 OpenAPI，否则用浏览器登录态）</option>
          <option value="openapi" ${info.mode === "openapi" ? "selected" : ""}>OpenAPI（api.manus.ai/v2）</option>
          <option value="native" ${info.mode === "native" ? "selected" : ""}>原生实时通道（api.manus.im + Socket.IO）</option>
        </select></label>
        <label>Cookie / Token（手动导入，可选）<textarea name="cookie_header" rows="2" placeholder="${info.has_cookie ? "已保存（留空保持不变）" : "cue_session_id=..."}"></textarea></label>
      ` : `
        <label>Muse Cookie（手动导入，可选）<textarea name="cookie_header" rows="3" placeholder="${info.has_cookie ? esc(info.cookie_preview) + "（留空保持不变）" : "从 muse.ai 请求头复制 Cookie"}"></textarea></label>
      `}
      <label>代理地址（可选）<input name="proxy_url" value="${esc(info.proxy_url || "")}" placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080" /></label>
      <div class="row gap"><button class="btn primary">保存并测试</button><button type="button" class="btn" onclick="closeOverlays()">关闭</button></div>
    </form>`);
}
async function btCloudAuthBrowser(bot) {
  const st = $("btAuthStatus"); if (st) st.innerHTML = "正在从本机/连接电脑的浏览器读取登录态…";
  try {
    const r = await api("POST", `/api/bots/engines/${bot}/auth/browser`, { verify: true });
    const v = r.verify || {};
    toast(v.ok ? `已从浏览器读取并验证通过（${r.source}）` : `已读取 ${r.cookie_count || 0} 条 Cookie（${r.source}），验证提示：${v.error || "待确认"}`);
    await btLoad();
    btCloudAuth(bot);
  } catch (e) {
    toast(e.message);
    if (st) st.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  }
}
async function btCloudAuthSave(ev, bot) {
  ev.preventDefault();
  const f = ev.target;
  const body = { proxy_url: f.proxy_url ? f.proxy_url.value.trim() : "", verify: true };
  if (f.api_key && f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
  if (f.cookie_header && f.cookie_header.value.trim()) body.cookie_header = f.cookie_header.value.trim();
  if (f.mode) body.mode = f.mode.value;
  try {
    const r = await api("PUT", `/api/bots/engines/${bot}/auth`, body);
    const v = r.verify || {};
    toast(v.ok ? "凭据已保存并验证通过" : ("已保存，但验证未通过：" + (v.error || "请检查凭据或代理")));
    await btLoad();
    closeOverlays();
  } catch (e) { toast("保存失败：" + e.message); }
}
async function btCloudAuthTest(bot) {
  try {
    const r = await api("POST", `/api/bots/engines/${bot}/auth/test`);
    toast(r.ok ? "连接验证通过" : ("验证失败：" + (r.error || "未知错误")));
  } catch (e) { toast("测试失败：" + e.message); }
}
async function btCloudAuthClear(bot) {
  if (!confirm("清除已保存的凭据？")) return;
  try { await api("DELETE", `/api/bots/engines/${bot}/auth`); toast("已清除"); await btLoad(); closeOverlays(); }
  catch (e) { toast(e.message); }
}
