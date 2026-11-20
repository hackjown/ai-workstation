/* v3.9.57 引擎对话 (Bot): talk to Antigravity (then Codex) on the computer directly, or hand the talking to AIW 管家.
   GET /api/bots, POST /api/bots/convs, GET|PATCH|DELETE /api/bots/convs/{id}, POST …/send|stop|takeover. */
const BT = { engines: [], convs: [], cid: null, conv: null, msgs: [], quote: null, poll: null, last: 0, sending: false };
const BT_WHO = { user: "我", aiw: "AIW 管家", system: "" };

async function btLoad() {
  try { const r = await api("GET", "/api/bots"); BT.engines = r.engines || []; BT.convs = r.convs || []; }
  catch (e) { toast("引擎对话加载失败：" + e.message); return; }
  btRenderList();
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
  box.innerHTML = `<div class="bt-head"><div class="heading">引擎对话</div><button class="btn sm primary" onclick="btNew()">${icon("plus")}新对话</button></div>`
    + (items || `<div class="muted pad">还没有对话。新建一个，直接和电脑上的 Antigravity 说话；也可以把目标交给 AIW 管家，让它替你和 Antigravity 来回沟通。</div>`)
    + `<div class="small muted pad">${BT.engines.filter((e) => !e.available).map((e) => esc(e.title) + "：下一版接入").join(" · ")}</div>`;
  paintIcons(box);
}
function btNew() {
  const opts = BT.engines.map((e) => `<option value="${esc(e.id)}" ${e.available ? "" : "disabled"}>${esc(e.title)}${e.available ? "" : "（即将支持）"}</option>`).join("");
  modal(`<div class="heading">新的引擎对话</div>
    <form class="col gap" onsubmit="btCreate(event)">
      <label class="small muted">引擎</label><select name="bot">${opts}</select>
      <label class="small muted">标题</label><input name="title" placeholder="例如 ldok 重建" />
      <label class="small muted">工作文件夹（电脑上，它就在这里改文件；空着就新建一个）</label>
      <input name="workdir" placeholder="~/workspace/ldok-rebuild" />
      <div class="row gap"><button class="btn primary">开始</button><button type="button" class="btn" onclick="closeOverlays()">取消</button></div>
    </form>`);
}
async function btCreate(ev) {
  ev.preventDefault();
  const f = ev.target;
  try {
    const c = await api("POST", "/api/bots/convs", { bot: f.bot.value, title: f.title.value, workdir: f.workdir.value });
    closeOverlays(); BT.convs.unshift(c); btOpen(c.id);
  } catch (e) { toast("没能新建：" + e.message); }
}
function btShowConv(on) { const v = $("v-bots"); if (v) v.classList.toggle("bt-open", !!on); }
async function btOpen(id, quiet) {
  if (BT.cid !== id) { BT.msgs = []; BT.last = 0; BT.quote = null; }
  BT.cid = id; localStorage.setItem("aiw_bot_conv", id);
  btShowConv(true); btRenderList();
  await btRefresh(true);
  if (!quiet) { const el = $("btPrompt"); if (el && matchMedia("(hover:hover)").matches) el.focus(); }
}
function btBack() { btShowConv(false); BT.cid = null; localStorage.removeItem("aiw_bot_conv"); btRenderList(); btStopPoll(); }
async function btRefresh(scroll) {
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
  if (S.view !== "bots" || !BT.cid) return;
  const live = BT.conv && (BT.conv.busy || BT.conv.takeover || BT.conv.status === "running" || BT.conv.queued);
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
      ${q}<div class="bubble ${m.role === "aiw" ? "bt-aiw" : ""} ${d.report === true ? "hmd" : ""}">${d.report === true ? hMd(m.text) : esc(m.text)}</div>
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
    ${tk ? `<button class="btn sm" onclick="btRelease()">收回</button>` : `<button class="btn sm primary" onclick="btTake()">交给管家</button>`}
    ${live ? `<button class="btn sm" onclick="btStop()">停止</button>` : ""}
    <button class="icon" onclick="btDelete()" aria-label="删除对话" title="删除对话">${icon("x")}</button>`;
  $("btTake").innerHTML = tk ? `<div class="bt-tk"><b>AIW 管家接管中</b>（第 ${tk.rounds}/${tk.max} 轮）：${esc(tk.goal)}<br><span class="small muted">你随时可以插话，它会先发你的话再继续；点「收回」自己接手。</span></div>` : "";
  $("btMsgs").innerHTML = BT.msgs.length ? BT.msgs.map(btMsg).join("")
    : `<div class="muted pad center">直接说要它做什么。它在电脑的 ${esc(c.workdir || "")} 里干活，改动不会自动推送。</div>`;
  btQuoteRender();
  paintIcons($("btHead"));
}
async function btSend(ev) {
  if (ev) ev.preventDefault();
  const el = $("btPrompt"), text = el.value.trim();
  if (!text || !BT.cid || BT.sending) return;
  BT.sending = true;
  const quote = BT.quote ? BT.quote.id : null;
  try {
    await api("POST", `/api/bots/convs/${BT.cid}/send`, { text, quote });
    el.value = ""; autoGrow(el); BT.quote = null;
    await btRefresh(true);
  } catch (e) { toast("没发出去：" + e.message); }
  finally { BT.sending = false; }
}
function btKey(e) { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(hover:hover)").matches) { e.preventDefault(); btSend(); } }
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

/* navigation */
TITLES.bots = "引擎对话";
const _goBots = go;
go = function (view) {
  const r = _goBots.apply(this, arguments);
  if (view === "bots") btLoad(); else btStopPoll();
  return r;
};
