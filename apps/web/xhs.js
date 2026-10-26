/* 小红书运营 (loaded after app.js + ui.js). Adds a 设置 group that opens the 小红书运营 page (settings, approval
   queue, today's counts, publish), and turns the agent's xhs_review confirmation card into a readable draft list. */
"use strict";

const XHS_KIND = { like: "点赞", comment: "评论别人", reply: "回复评论", post: "发布笔记" };
const XHS_WARN = { captcha: "验证码/滑块", verify: "安全验证", rate_limit: "操作频繁", account_warning: "账号警告", other: "手动/其他" };
const xhsQ = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let XHS = null;

(function xhsStyle() {
  const st = document.createElement("style");
  st.textContent = `
  .xhs-ov{position:fixed;inset:0;z-index:60;background:var(--bg,#0a0a0a);overflow:auto;-webkit-overflow-scrolling:touch}
  .xhs-ov .wrap{max-width:860px;margin:0 auto;padding:18px 16px 80px}
  .xhs-ov .top{display:flex;align-items:center;gap:10px;margin-bottom:12px}
  .xhs-ov .top h2{flex:1;margin:0;font-size:20px}
  .xhs-ov .box{border:1px solid var(--line,rgba(127,127,127,.25));border-radius:12px;padding:14px;margin:12px 0}
  .xhs-ov .box h3{margin:0 0 8px;font-size:15px}
  .xhs-ov .g2{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
  .xhs-ov label.f{display:flex;flex-direction:column;gap:4px;font-size:12px;opacity:.95}
  .xhs-ov label.sw{display:flex;align-items:center;gap:8px;font-size:13px}
  .xhs-ov label.sw input{width:auto}
  .xhs-ov .bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
  .xhs-ov .meter{display:flex;gap:10px;flex-wrap:wrap}
  .xhs-ov .meter div{flex:1;min-width:120px;padding:8px 10px;border-radius:10px;background:rgba(200,118,24,.10)}
  .xhs-ov .meter b{font-size:18px}
  .xhs-d{border:1px solid var(--line,rgba(127,127,127,.25));border-radius:10px;padding:10px;margin:8px 0}
  .xhs-d .tg{font-size:12px;opacity:.8;margin-bottom:4px}
  .xhs-d .ex{font-size:12px;opacity:.75;border-left:3px solid rgba(200,118,24,.6);padding-left:8px;margin:6px 0}
  .xhs-d textarea{width:100%;min-height:58px}
  .xhs-warn{padding:10px 12px;border-radius:10px;background:rgba(220,60,60,.12);margin:10px 0;font-size:13px}
  .xhs-note{font-size:12px;opacity:.8}
  `;
  document.head.appendChild(st);
})();

/* ---------- settings group (after the 设置 page renders) */
const _xhsLoadSettings = loadSettings;
loadSettings = async function () {
  await _xhsLoadSettings.apply(this, arguments);
  try { xhsSettingsGroup(await api("GET", "/api/xhs")); } catch (e) { /* older server */ }
};

function xhsSettingsGroup(o) {
  const page = $("settingsPage"); if (!page || $("g-xhs")) return;
  const host = page.querySelector(".set") || page;
  const s = o.settings, c = o.counts, p = o.pause;
  const sec = document.createElement("section");
  sec.className = "grp"; sec.id = "g-xhs";
  sec.innerHTML = `<div class="grp-h"><h2>小红书运营</h2>
      ${p ? '<span class="chip bad">已暂停</span>' : s.enabled ? '<span class="chip ok">运行中</span>' : '<span class="chip">未开启</span>'}
      ${o.pending.length ? `<span class="chip warn">${o.pending.length} 条待审核</span>` : ""}
      <div class="act"><button type="button" class="btn sm primary" onclick="xhsOpen()">打开</button></div></div>
    <div class="grp-d">在 AIW 电脑上已登录的浏览器里点赞、回复你笔记下的评论、给兴趣相近的笔记写评论、发布笔记。评论和笔记先给你审核。
      今天：赞 ${c.like}/${s.caps.like} · 评论 ${c.comment}/${s.caps.comment} · 回复 ${c.reply}/${s.caps.reply} · 发布 ${c.post}/${s.caps.post}</div>`;
  const before = host.querySelector("#g-notify") || host.querySelector("#g-me");
  if (before) host.insertBefore(sec, before); else host.appendChild(sec);
  const nav = page.querySelector(".set-index");
  if (nav && !nav.querySelector('[href="#g-xhs"]')) {
    const a = document.createElement("a"); a.href = "#g-xhs"; a.textContent = "小红书";
    a.onclick = (e) => { e.preventDefault(); xhsOpen(); };
    nav.appendChild(a);
  }
}

/* ---------- the page */
async function xhsOpen() {
  let ov = $("xhsOv");
  if (!ov) { ov = document.createElement("div"); ov.id = "xhsOv"; ov.className = "xhs-ov"; document.body.appendChild(ov); }
  ov.innerHTML = `<div class="wrap"><div class="top"><button class="btn sm" onclick="xhsClose()">‹ 返回</button><h2>小红书运营</h2></div><div class="muted">加载中…</div></div>`;
  await xhsLoad();
}
function xhsClose() { const ov = $("xhsOv"); if (ov) ov.remove(); if (S.view === "settings") loadSettings(); }

async function xhsLoad() {
  const ov = $("xhsOv"); if (!ov) return;
  try { XHS = await api("GET", "/api/xhs"); } catch (e) { toast(e.message); return; }
  const o = XHS, s = o.settings, c = o.counts, p = o.pause;
  const mopt = o.machines.map((m) => `<option value="${xhsQ(m.id)}" ${m.id === o.machine_id ? "selected" : ""}>${xhsQ(m.name)}${m.cloud ? "（云电脑 · 机房 IP）" : "（本地 · 家庭网络）"}</option>`).join("");
  const cloudPicked = (o.machines.find((m) => m.id === o.machine_id) || {}).cloud;
  const tog = (k) => `<label class="sw"><input type="checkbox" name="act_${k}" ${s.actions[k] ? "checked" : ""}/>${XHS_KIND[k]}</label>`;
  const cap = (k, max) => `<label class="f">${XHS_KIND[k]} / 天<input type="number" min="0" max="${max}" name="cap_${k}" value="${s.caps[k]}"/></label>`;
  const next = (o.next_runs || []).map((t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })).join("、");
  ov.innerHTML = `<div class="wrap">
    <div class="top"><button class="btn sm" onclick="xhsClose()">‹ 返回</button><h2>小红书运营</h2>
      <button class="btn sm" onclick="xhsRun()">现在跑一次</button></div>
    ${p ? `<div class="xhs-warn"><b>已暂停到 ${when(p.until)}</b>：${xhsQ(XHS_WARN[p.kind] || p.reason || "")}${p.detail ? " · " + xhsQ(p.detail) : ""}。
      页面出现验证码、验证、频繁提示或账号警告时会自动停下并暂停 24 小时；验证码不会自动处理，需要的话请在「电脑」页接管后自己处理。
      <button class="btn sm" onclick="xhsResume()">解除暂停</button></div>` : ""}
    <div class="box"><h3>今天</h3><div class="meter">
      ${["like", "comment", "reply", "post"].map((k) => `<div><div class="xhs-note">${XHS_KIND[k]}</div><b>${c[k]}</b> / ${s.caps[k]}</div>`).join("")}</div>
      <div class="xhs-note" style="margin-top:8px">${s.enabled ? (next ? `今天还会在 ${next} 左右各跑一次（时间随机，每次长短不同）` : "今天的时段已过或额度已满") : "未开启：打开下面的「自动运营」后，在活跃时段内随机安排几次"}</div></div>

    <div class="box"><h3>待审核 ${o.pending.length ? `<span class="chip warn">${o.pending.length}</span>` : ""}</h3>
      ${o.pending.length ? `<div class="bar" style="margin:0 0 6px"><label class="sw"><input type="checkbox" id="xhsAll" onchange="document.querySelectorAll('.xhs-pick').forEach(x=>x.checked=this.checked)"/>全选</label>
        <button class="btn sm primary" onclick="xhsBatch('approve')">通过所选</button><button class="btn sm" onclick="xhsBatch('skip')">跳过所选</button></div>` : ""}
      ${o.pending.map((d) => xhsDraftHTML(d, true)).join("") || '<div class="xhs-note">没有待审核的草稿。新草稿会推送通知给你。</div>'}
      ${o.approved.length ? `<div class="xhs-note" style="margin-top:8px">已批准、等下次会话发出：${o.approved.length} 条</div>${o.approved.map((d) => xhsDraftHTML(d, false)).join("")}` : ""}</div>

    <div class="box"><h3>发一篇笔记</h3>
      <div class="xhs-note">自己写好内容（图片/视频会上传到所选电脑），审核即视为批准，下次会话通过 creator.xiaohongshu.com 发布；或者只写要求，让 Agent 起草，再推给你审核。</div>
      <form id="xhsPost" onsubmit="xhsPost(event)" class="g2" style="margin-top:8px">
        <label class="f" style="grid-column:1/-1">标题（≤20 字）<input name="title" maxlength="20"/></label>
        <label class="f" style="grid-column:1/-1">正文<textarea name="text" rows="4" placeholder="留空 + 写在下面的要求里 = 让 Agent 起草"></textarea></label>
        <label class="f" style="grid-column:1/-1">或：让 Agent 起草的要求<input name="brief" placeholder="例如：写一篇周末湖边露营的装备分享，语气轻松"/></label>
        <label class="f" style="grid-column:1/-1">图片 / 视频<input type="file" name="files" multiple accept="image/*,video/*"/></label>
        <div class="bar" style="grid-column:1/-1"><button class="btn primary">提交</button><span class="xhs-note" id="xhsPostNote"></span></div>
      </form></div>

    <form class="box" onsubmit="xhsSave(event)"><h3>设置</h3>
      <label class="sw"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}/>自动运营（在活跃时段内随机时间运行）</label>
      <div class="g2" style="margin-top:10px">
        <label class="f" style="grid-column:1/-1">电脑<select name="machine_id">${mopt}</select></label>
      </div>
      <div class="xhs-note" style="margin-top:4px">${cloudPicked ? "⚠️ 云电脑是机房 IP，更容易被风控，建议用本地虚拟机（家庭网络 IP）。" : "推荐本地虚拟机（如 104）：家庭网络 IP，比云电脑的机房 IP 更不容易被风控。"}
        这台电脑的 Chrome 需要登录一次小红书：没登录时会通知你，到「电脑」页点接管，用小红书 App 扫码即可。</div>
      <div class="g2" style="margin-top:10px">
        <label class="f" style="grid-column:1/-1">人设<textarea name="persona" rows="2" placeholder="例如：杭州上班族，周末露营、手冲咖啡，喜欢分享实用经验">${xhsQ(s.persona)}</textarea></label>
        <label class="f" style="grid-column:1/-1">兴趣关键词（逗号分隔，用来找兴趣相近的笔记）<input name="keywords" value="${xhsQ((s.keywords || []).join("，"))}" placeholder="露营，手冲咖啡，徒步"/></label>
        <label class="f" style="grid-column:1/-1">语气<input name="tone" value="${xhsQ(s.tone)}"/></label>
        ${cap("like", 100)}${cap("comment", 30)}${cap("reply", 60)}${cap("post", 3)}
        <label class="f">活跃开始<input type="time" name="w_start" value="${xhsQ(s.window.start)}"/></label>
        <label class="f">活跃结束<input type="time" name="w_end" value="${xhsQ(s.window.end)}"/></label>
      </div>
      <div class="bar">${tog("like")}${tog("comment")}${tog("reply")}${tog("post")}</div>
      <label class="sw" style="margin-top:8px"><input type="checkbox" name="auto_reply" ${s.auto_reply ? "checked" : ""}/>回复我笔记下的评论时不用审核，直接发（评论别人和发布笔记始终要审核）</label>
      <div class="bar"><button class="btn primary">保存</button><span class="xhs-note">点赞不审核，但受每日上限限制。所有上限在服务器端计数。</span></div>
    </form>

    ${o.recent.length ? `<div class="box"><h3>最近</h3>${o.recent.map((d) => `<div class="xhs-note">${when(d.sent_at || d.decided_at || d.created_at)} · ${XHS_KIND[d.kind]} · ${{ sent: "已发出", skipped: "已跳过", failed: "未发出" }[d.status] || d.status} · ${xhsQ((d.text || "").slice(0, 40))}</div>`).join("")}</div>` : ""}
    <div class="box xhs-note">风险提示：自动化操作违反小红书社区规范时可能被限流或封号。Aiw 会模仿真人节奏（随机间隔、先浏览再互动、每天次数有限、不固定时间），
      但不能保证不被识别。遇到任何验证或警告会立刻停下并暂停 24 小时。</div>
  </div>`;
}

function xhsDraftHTML(d, editable) {
  const tgt = d.kind === "post" ? `<b>${xhsQ(d.title || "（无标题）")}</b>${(d.media || []).length ? ` · ${d.media.length} 个图片/视频` : ""}`
    : `${d.target_url ? `<a href="${xhsQ(d.target_url)}" target="_blank" rel="noopener">${xhsQ(d.target_title || d.target_url.slice(0, 50))}</a>` : xhsQ(d.target_title)}${d.target_author ? " · @" + xhsQ(d.target_author) : ""}`;
  return `<div class="xhs-d" id="xd-${xhsQ(d.id)}">
    <div class="tg">${editable ? `<input type="checkbox" class="xhs-pick" value="${xhsQ(d.id)}" style="width:auto;margin-right:6px"/>` : ""}<span class="chip">${XHS_KIND[d.kind]}</span>${d.auto ? ' <span class="chip">自动回复</span>' : ""} ${tgt}</div>
    ${d.target_excerpt ? `<div class="ex">${xhsQ(d.target_excerpt)}</div>` : ""}
    ${editable ? `${d.kind === "post" ? `<input class="xt" value="${xhsQ(d.title)}" maxlength="20" style="margin-bottom:6px"/>` : ""}<textarea class="xx">${xhsQ(d.text)}</textarea>
      <div class="bar"><button class="btn sm primary" onclick="xhsDecide('${xhsQ(d.id)}','approve')">通过</button>
      <button class="btn sm" onclick="xhsDecide('${xhsQ(d.id)}','edit')">保存修改</button>
      <button class="btn sm" onclick="xhsDecide('${xhsQ(d.id)}','skip')">跳过</button></div>`
      : `<div>${xhsQ(d.text)}</div><div class="bar"><button class="btn sm" onclick="xhsDecide('${xhsQ(d.id)}','skip')">撤回</button></div>`}
  </div>`;
}

async function xhsDecide(id, action) {
  const box = $("xd-" + id); const ta = box && box.querySelector(".xx"); const tt = box && box.querySelector(".xt");
  const body = { action }; if (ta) body.text = ta.value; if (tt) body.title = tt.value;
  try { await api("POST", `/api/xhs/drafts/${id}`, body); toast({ approve: "已通过", skip: "已跳过", edit: "已保存" }[action]); xhsLoad(); }
  catch (e) { toast(e.message); }
}

async function xhsBatch(action) {
  const ids = [...document.querySelectorAll(".xhs-pick:checked")].map((x) => x.value);
  if (!ids.length) { toast("先勾选草稿"); return; }
  // edited text in the boxes is saved first so 通过所选 sends what the user sees
  for (const id of ids) {
    const box = $("xd-" + id); const ta = box && box.querySelector(".xx"); const d = XHS.pending.find((x) => x.id === id);
    if (action === "approve" && ta && d && ta.value.trim() !== d.text) {
      try { await api("POST", `/api/xhs/drafts/${id}`, { action: "edit", text: ta.value }); } catch (e) { toast(e.message); return; }
    }
  }
  await api("POST", "/api/xhs/drafts/batch", { ids, action });
  toast(action === "approve" ? `已通过 ${ids.length} 条` : `已跳过 ${ids.length} 条`); xhsLoad();
}

async function xhsSave(e) {
  e.preventDefault(); const f = e.target;
  const body = { enabled: f.enabled.checked, machine_id: f.machine_id.value, persona: f.persona.value, keywords: f.keywords.value, tone: f.tone.value,
    caps: { like: +f.cap_like.value, comment: +f.cap_comment.value, reply: +f.cap_reply.value, post: +f.cap_post.value },
    window: { start: f.w_start.value, end: f.w_end.value }, auto_reply: f.auto_reply.checked,
    actions: { like: f.act_like.checked, comment: f.act_comment.checked, reply: f.act_reply.checked, post: f.act_post.checked } };
  if (body.auto_reply && !(XHS.settings.auto_reply) && !confirm("回复你笔记下的评论将不再经过你审核，直接发出。开启？")) return;
  try { await api("PUT", "/api/xhs/settings", body); toast("已保存"); xhsLoad(); } catch (err) { toast(err.message); }
}

async function xhsPost(e) {
  e.preventDefault(); const f = e.target; const note = $("xhsPostNote");
  const mid = (XHS && XHS.machine_id) || MID;
  const media = [];
  for (const file of f.files.files) {
    note.textContent = `上传 ${file.name}…`;
    const b64 = await new Promise((ok, bad) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1]); r.onerror = bad; r.readAsDataURL(file); });
    try { media.push((await api("POST", "/api/uploads", { name: file.name, content_b64: b64, machine_id: mid })).vm_path); }
    catch (err) { note.textContent = ""; toast(err.message); return; }
  }
  note.textContent = "";
  const text = f.text.value.trim(), brief = f.brief.value.trim();
  if (!text && !brief) { toast("写正文，或写要求让 Agent 起草"); return; }
  if (text && !confirm("确认发布这篇笔记？会在下一次会话（几分钟到一小时内，活跃时段内）发出。")) return;
  try {
    const r = await api("POST", "/api/xhs/post", text ? { title: f.title.value, text, media } : { brief, media });
    toast(text ? "已加入发布队列" : "Agent 开始起草，写好后推给你审核"); f.reset(); xhsLoad();
    if (!text && r.task && r.task.thread_id) { /* the drafting task is in 对话 */ }
  } catch (err) { toast(err.message); }
}

async function xhsRun() { try { await api("POST", "/api/xhs/run"); toast("已开始一次会话，可在对话里看过程"); } catch (e) { toast(e.message); } }
async function xhsResume() { if (!confirm("解除暂停？如果页面的警告还在，可能导致更严重的限制。")) return; await api("DELETE", "/api/xhs/pause"); xhsLoad(); }

/* ---------- chat: the review card shows the target posts and drafts */
const _xhsRenderChat = renderChat;
renderChat = function () {
  _xhsRenderChat.apply(this, arguments);
  try {
    for (const t of (S.thread && S.thread.tasks) || []) {
      const c = t.pending_confirm;
      if (!c || c.tool !== "xhs_review" || !t.session_id) continue;
      const btn = document.querySelector(`.ask.c button[onclick*="${t.session_id}"]`);
      const card = btn && btn.closest(".ask.c"); if (!card || card.dataset.xhs) continue;
      card.dataset.xhs = "1";
      const ds = (c.payload && c.payload.drafts) || [];
      card.innerHTML = `<div><b>小红书：${ds.length} 条草稿等你审核</b></div>
        ${ds.map((d) => `<div class="xhs-d"><div class="tg"><span class="chip">${xhsQ(d.kind_cn || d.kind)}</span>
          ${d.target_url ? `<a href="${xhsQ(d.target_url)}" target="_blank" rel="noopener">${xhsQ(d.target_title || "目标笔记")}</a>` : xhsQ(d.title || "")}</div>
          ${d.target_excerpt ? `<div class="ex">${xhsQ(d.target_excerpt)}</div>` : ""}<div>${xhsQ(d.text)}</div></div>`).join("")}
        <div class="opts"><button class="btn sm primary" onclick="confirmAct('${t.session_id}',true)">全部通过</button>
          <button class="btn sm" onclick="xhsOpen()">逐条编辑 / 跳过</button>
          <button class="btn sm" onclick="confirmAct('${t.session_id}',false)">全部跳过</button></div>`;
    }
  } catch (e) { /* never break the chat */ }
};
