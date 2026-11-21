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
function btEngine(id) { return BT.engines.find((e) => e.id === id) || { title: id, short: id }; }
function btRenderList() {
  const box = $("btList"); if (!box) return;
  const items = BT.convs.map((c) => {
    const e = btEngine(c.bot), st = c.takeover ? "管家接管中" : (c.busy || c.status === "running") ? "回复中…" : "";
    return `<button class="bt-it ${c.id === BT.cid ? "on" : ""}" onclick="btOpen('${c.id}')">
      <span class="bt-av ${esc(c.bot)}">${esc((e.short || e.title).slice(0, 3))}</span>
      <span class="grow"><span class="t1">${esc(c.title)}</span><span class="t2">${st ? `<b>${st}</b> · ` : ""}${c.last ? esc(c.last.text) : esc(e.title)}</span></span></button>`;
  }).join("");
  box.innerHTML = `<div class="bt-head"><div class="heading">引擎</div><button class="btn sm" onclick="btGroupNew()" title="几个引擎各开一个分支并行干，管家合并">${icon("plus")}群聊</button><button class="btn sm primary" onclick="btNew()">${icon("plus")}新对话</button></div>`
    + btGroupItems()
    + (items || `<div class="muted pad">还没有对话。新建一个，直接和电脑上的 Antigravity、Codex、Claude Code、CodeBuddy 或 Grok 说话；也可以把目标交给 AIW 管家，让它替你来回沟通。</div>`)
    + `<div class="small muted pad">${BT.engines.filter((e) => !e.available).map((e) => esc(e.title) + "：下一版接入").join(" · ")}</div>`;
  paintIcons(box);
}
function btNew() {
  const opts = BT.engines.map((e) => `<option value="${esc(e.id)}" ${e.available ? "" : "disabled"}>${esc(e.title)}${e.available ? "" : "（即将支持）"}</option>`).join("");
  modal(`<div class="heading">新对话</div>
    <form class="col gap" onsubmit="btCreate(event)">
      <label class="small muted">引擎</label><select name="bot" onchange="btModelsFill('', '', false, this.value)">${opts}</select>
      <label class="small muted">标题</label><input name="title" placeholder="例如 ldok 重建" />
      <label class="small muted">工作文件夹（电脑上，它就在这里改文件；空着就新建一个）</label>
      <input name="workdir" placeholder="~/workspace/ldok-rebuild" />
      ${btModelFields()}
      <div class="row gap"><button class="btn primary">开始</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
  btModelsFill("", "", false, "agy");
}
async function btCreate(ev) {
  ev.preventDefault();
  const f = ev.target;
  try {
    const c = await api("POST", "/api/bots/convs", { bot: f.bot.value, title: f.title.value, workdir: f.workdir.value, ...btModelValue(f) });
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
  const stepHtml = steps.length ? `<details class="bt-steps" ${running ? "open" : ""}><summary>${running ? "正在做" : "做了"} ${steps.length} 步</summary>
    ${steps.slice(-15).map((s) => `<div class="bt-step ${s.error ? "bad" : ""}"><b>${esc(s.tool || "")}</b> ${esc(s.arg || "")}${s.error ? ` <i>${esc(s.error)}</i>` : ""}</div>`).join("")}</details>` : "";
  const den = (d.denied || []).length ? `<div class="bt-denied">被拒绝的操作：${d.denied.map((x) => `${esc(x.tool || "")} ${esc(x.target || "")}（${esc(x.why || x.action || "")}）`).join("；")}</div>` : "";
  const ch = d.changes && (d.changes.files || []).length ? `<div class="small muted">改动：${esc(d.changes.files.slice(0, 8).join("，"))}${d.changes.files.length > 8 ? " …" : ""}</div>` : "";
  const body = running && !m.text
    ? `<div class="bubble hdots-b"><span class="hdots"><i></i><i></i><i></i></span><span class="hnote">${esc(((d.said || [])[0] || "").slice(0, 80) || "在电脑上干活…")}</span></div>`
    : `<div class="bubble hmd ${["error", "failed", "blocked"].includes(d.status) ? "err" : ""}">${hMd(m.text)}</div>`;
  return `<div class="bt-row bot"><span class="bt-av ${esc((BT.conv || {}).bot || "")}">${esc((eng.short || eng.title).slice(0, 3))}</span><div class="bt-col">
    <div class="bt-name">${esc(eng.title)}${d.status === "blocked" ? " · 被权限挡住" : d.status === "stopped" ? " · 已停止" : ""}</div>
    ${body}${stepHtml}${den}${ch}<div class="meta">${hTime(m.created_at)}${running ? "" : acts}</div></div></div>`;
}
function btRenderConv() {
  const c = BT.conv; if (!c) return;
  const eng = btEngine(c.bot), tk = c.takeover, live = c.busy || c.status === "running";
  $("btHead").innerHTML = `<button class="icon only-mobile" onclick="btBack()" aria-label="返回">${icon("back")}</button>
    <div class="grow"><div class="heading" onclick="btRename()" title="点一下改名">${esc(c.title)}</div>
      <div class="small muted">${esc(eng.title)} · ${esc(c.workdir || "")}${c.session ? " · 会话已保存" : ""}</div></div>
    <button class="btn sm bt-model" onclick="btModelEdit()" title="换模型和强度">${esc(btModelName(c))}</button>
    ${tk ? `<button class="btn sm" onclick="btRelease()">收回</button>` : `<button class="btn sm primary" onclick="btTake()">交给管家</button>`}
    ${live ? `<button class="btn sm" onclick="btStop()">停止</button>` : ""}
    <button class="icon" onclick="btDelete()" aria-label="删除对话" title="删除对话">${icon("x")}</button>`;
  $("btTake").innerHTML = tk ? `<div class="bt-tk"><b>AIW 管家接管中</b>（第 ${tk.rounds}/${tk.max} 轮）：${esc(tk.goal)}<br><span class="small muted">你随时可以插话，它会先发你的话再继续；点「收回」自己接手。</span></div>` : "";
  $("btMsgs").innerHTML = BT.msgs.length ? BT.msgs.map(btMsg).join("")
    : `<div class="muted pad center">直接说要它做什么。它在电脑的 ${esc(c.workdir || "")} 里干活，改动不会自动推送。</div>`;
  $("btPrompt").placeholder = "对它说，或输入 / 选技能…（管家接管时也可以插话）";
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
  modal(`<div class="heading">交给 AIW 管家</div>
    <div class="small muted">管家会替你和 ${esc(btEngine(BT.conv.bot).title)} 来回沟通，直到目标完成、需要你决定或轮数用完。它不会让对方推送、合并或删除东西。</div>
    <form class="col gap" onsubmit="btTakeGo(event)">
      <textarea name="goal" rows="4" placeholder="目标和验收标准，例如：把首页加载改成懒加载，跑通 npm test，给我改动摘要" required></textarea>
      <label class="small muted">最多来回几轮</label><input name="rounds" type="number" min="1" max="30" value="8" />
      <div class="row gap"><button class="btn primary">交给管家</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
}
async function btTakeGo(ev) {
  ev.preventDefault();
  try { await api("POST", `/api/bots/convs/${BT.cid}/takeover`, { goal: ev.target.goal.value, rounds: +ev.target.rounds.value || 8 }); closeOverlays(); btRefresh(true); }
  catch (e) { toast("没能交给管家：" + e.message); }
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
  return `<label class="small muted">模型 <button type="button" class="link small" onclick="btModelsFill(null,null,true)">刷新</button></label>
    <select name="mgroup" id="btMG" onchange="btEffFill()"><option value="">读取中…</option></select>
    <label class="small muted">强度（越高越慢、越仔细）</label><select name="meff" id="btME"><option value="">默认</option></select>
    <div class="small muted" id="btMNote"></div>`;
}
async function btModelsFill(model, effort, refresh, bot) {
  const sel = $("btMG"); if (!sel) return;
  if (bot) sel.dataset.bot = bot;
  bot = sel.dataset.bot || BM.bot;
  if (model === null) { model = sel.dataset.model || ""; effort = sel.dataset.effort || ""; }
  sel.dataset.model = model || ""; sel.dataset.effort = effort || "";
  let groups = [];
  try { groups = await btModels(refresh, bot); $("btMNote").textContent = ""; }
  catch (e) { $("btMNote").textContent = "读不到模型列表（" + e.message + "），先用 " + bot + " 的默认模型"; }
  if ((sel.dataset.bot || BM.bot) !== bot) return;           // the engine changed while this list loaded
  const hit = model ? btGroupOf(model) : null;
  sel.innerHTML = `<option value="">默认（${esc(bot)} 自己的设置）</option>` + groups.map((g) => `<option value="${esc(g.id)}">${esc(g.label)}</option>`).join("")
    + (model && !hit ? `<option value="${esc(model)}">${esc(model)}</option>` : "");
  sel.value = hit ? hit.g.id : (model || "");
  btEffFill((hit && hit.eff) || effort || "");
}
function btEffFill(want) {
  const sel = $("btMG"), es = $("btME"); if (!sel || !es) return;
  const g = (BM.groups || []).find((x) => x.id === sel.value);
  const own = BM.bot === "agy" ? Object.keys(BT_EFF).filter((e) => e !== "minimal") : (BM.efforts || []);
  const effs = g && (g.efforts || []).length ? g.efforts : own;
  if (want === undefined) want = es.value;
  es.parentElement && es.previousElementSibling && (es.previousElementSibling.style.display = es.style.display = effs.length ? "" : "none");
  const plain = !g || !(g.efforts || []).length || g.plain;
  es.innerHTML = (plain ? `<option value="">默认</option>` : "") + effs.map((e) => `<option value="${e}">${BT_EFF[e] || e}（${e}）</option>`).join("");
  es.value = effs.includes(want) ? want : plain ? "" : (effs.includes("high") ? "high" : effs[effs.length - 1]);
}
function btModelValue(f) {
  const gid = f.mgroup ? f.mgroup.value : "", eff = f.meff ? f.meff.value : "";
  const g = (BM.groups || []).find((x) => x.id === gid);
  if (g && eff && (g.slugs || {})[eff]) return { model: g.slugs[eff], effort: "" };   // the slug names its strength
  return { model: gid, effort: eff };
}
function btModelEdit() {
  const c = BT.conv; if (!c) return;
  modal(`<div class="heading">模型和强度</div>
    <div class="small muted">从下一句起生效：引擎会带着原会话接着聊，前面聊的都还在。</div>
    <form class="col gap" onsubmit="btModelSave(event)">${btModelFields()}
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
function btGroupNew() {
  const boxes = BT.engines.filter((e) => e.available).map((e) => `<label class="row gap small"><input type="checkbox" name="m" value="${esc(e.id)}" ${["codex", "claude"].includes(e.id) ? "checked" : ""}/>${esc(e.title)}</label>`).join("");
  modal(`<div class="heading">新群聊</div>
    <div class="small muted">选几个引擎，各在自己的分支和工作树里并行干，做完由 AIW 管家合并到一个新分支。不推送，也不动你的基线分支。</div>
    <form class="col gap" onsubmit="btGroupCreate(event)">
      <label class="small muted">成员</label><div class="col">${boxes}</div>
      <label class="small muted">标题</label><input name="title" placeholder="例如 登录页改版" />
      <label class="small muted">git 仓库（电脑上；空着就新建一个）</label><input name="repo" placeholder="~/workspace/ldok" />
      <label class="small muted">基线分支（空着用仓库当前分支）</label><input name="base" placeholder="main" />
      <label class="row gap small"><input type="checkbox" name="auto" checked />都做完后管家自动合并</label>
      <label class="small muted">要做什么（可以先空着，进群再说）</label><textarea name="goal" rows="3" placeholder="例如：做一个登录页，后端加接口，前端做页面，跑通测试"></textarea>
      <div class="row gap"><button class="btn primary">建群</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
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
  const chips = g.members.map((m) => `<button class="link small" onclick="btOpen('${m.conv}')" title="打开 ${esc(btEngine(m.bot).title)} 的对话（分支 ${esc(m.branch)}）"><span class="bt-av ${esc(m.bot)} sm">${esc((btEngine(m.bot).short || m.bot).slice(0, 3))}</span>${m.busy ? "…" : ""}</button>`).join(" ");
  $("btHead").innerHTML = `<button class="icon only-mobile" onclick="btBack()" aria-label="返回">${icon("back")}</button>
    <div class="grow"><div class="heading">${esc(g.title)}</div>
      <div class="small muted">${esc(g.repo)} · 基线 ${esc(g.base)}${st ? " · <b>" + st + "</b>" : ""}</div></div>
    <span class="row gap">${chips}</span>
    <button class="btn sm primary" onclick="btGroupMerge()" ${g.status === "merging" ? "disabled" : ""} title="提交各分支的改动，合并到 aiw/${esc(g.id)}/merge">合并</button>
    ${live ? `<button class="btn sm" onclick="btGroupStop()">停止</button>` : ""}
    <button class="icon" onclick="btGroupDelete()" aria-label="删除群聊" title="删除群聊">${icon("x")}</button>`;
  $("btTake").innerHTML = (g.pending || []).length ? `<div class="bt-tk"><b>${st || "在干活"}</b>：等 ${esc(g.pending.map((b) => btEngine(b).title).join("、"))} 回复${g.auto_merge ? "，都回复后管家自动合并" : ""}</div>` : "";
  $("btMsgs").innerHTML = BT.gmsgs.length ? BT.gmsgs.map(btGMsg).join("")
    : `<div class="muted pad center">说要做什么：不 @ 就由 AIW 管家分工；@codex 只发给它。每个引擎在自己的分支上改，做完管家合并。</div>`;
  $("btPrompt").placeholder = "说要做什么，@codex @claude @grok @cbc @agy 指定谁做，@管家 分工…";
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
  return `<div class="bt-row bot"><span class="bt-av ${esc(m.who)}">${esc((eng.short || eng.title).slice(0, 3))}</span><div class="bt-col">
    <div class="bt-name">${esc(eng.title)}${["error", "failed", "blocked"].includes(d.status) ? " · 没做成" : d.status === "stopped" ? " · 已停止" : ""}</div>
    <div class="bubble hmd ${["error", "failed", "blocked"].includes(d.status) ? "err" : ""}">${hMd(m.text || "（没有文字）")}</div>${ch}${den}
    <div class="meta">${hTime(m.created_at)}${d.conv ? ` · <button class="link" onclick="btOpen('${d.conv}')">看它的对话 ›</button>` : ""}</div></div></div>`;
}
async function btGroupMerge() { try { await api("POST", `/api/bots/groups/${BT.gid}/merge`); toast("管家开始合并"); setTimeout(() => btGroupRefresh(true), 800); } catch (e) { toast("合并失败：" + e.message); } }
async function btGroupStop() { try { await api("POST", `/api/bots/groups/${BT.gid}/stop`); toast("已停止"); btGroupRefresh(true); } catch (e) { toast("停止失败：" + e.message); } }
async function btGroupDelete() {
  if (!confirm("删除这个群聊和成员的对话？（电脑上的仓库、分支和工作树不会删）")) return;
  try { await api("DELETE", `/api/bots/groups/${BT.gid}`); BT.groups = BT.groups.filter((g) => g.id !== BT.gid); btBack(); } catch (e) { toast(e.message); }
}
