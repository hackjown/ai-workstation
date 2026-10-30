/* 京东/淘宝价格监控 on the 追踪 page (loaded after app.js + ui.js). Adds its own card and list and takes over
   loadWatches() so shop watches (mode "shop") get their own rows; other watches render as before. */
"use strict";

const SHOP_SITE = { jd: "京东", taobao: "淘宝", tmall: "天猫" };
const SHOP_STATUS = { ok: ["正常", "ok"], machine_busy: ["等电脑空闲", "warn"], machine_offline: ["等电脑上线", "warn"], no_machine: ["电脑不在了", "bad"], login: ["需要登录", "warn"], need_login: ["需要登录京东", "warn"], captcha: ["需要验证", "warn"], risk_limit: ["京东风控限制", "warn"], price_pending: ["未开价", "warn"], off_shelf: ["已下架", "bad"],
  no_price: ["读不到价格", "warn"], error: ["出错", "bad"], no_browser: ["浏览器未就绪", "warn"] };
const SHOP_ORDER = { running: ["下单中", "warn"], waiting_confirm: ["等你确认付款", "warn"], submitted: ["已提交订单", "ok"],
  rejected: ["已拒绝", ""], timeout: ["超时已取消", ""], above_target: ["实付超目标，未下单", "warn"], submit_failed: ["提交失败", "bad"],
  ended: ["下单任务结束", ""], failed: ["下单任务失败", "bad"], stopped: ["下单已停止", ""] };

function shopCard() {
  if ($("shopCard")) return;
  const page = document.querySelector("#v-tracking .page"); if (!page) return;
  const card = document.createElement("div");
  card.className = "card sky"; card.id = "shopCard";
  card.innerHTML = `<div class="heading">京东 / 淘宝价格监控</div>
    <div class="muted">用 AIW 电脑上已登录的 Chrome 读价格（含到手价/券后价）和库存。降到目标价时提醒你；打开“自动下单”后会把商品放进订单、停在付款前，推送给你确认，你点允许才提交并付款。支付密码永远由你在接管桌面里输入。</div>
    <form class="grid2" id="shopForm" onsubmit="addShopWatch(event)">
      <input name="url" placeholder="商品链接或分享文本（jd.com / taobao.com / tmall.com）" required style="grid-column:1/-1" />
      <input name="target_price" type="number" step="0.01" min="0.01" placeholder="目标单价（元），≤ 它时触发" required />
      <input name="quantity" type="number" min="1" max="99" value="1" placeholder="数量" />
      <input name="sku" placeholder="规格（可空），例如 黑色 256GB" />
      <input name="address_note" placeholder="收货地址（可空 = 默认地址）" />
      <select name="interval_min"><option value="10">每 10 分钟</option><option value="15">每 15 分钟</option><option value="30" selected>每 30 分钟</option><option value="60">每小时</option><option value="120">每 2 小时</option><option value="360">每 6 小时</option></select>
      <select name="machine_id" id="shopMachine"></select>
      <label class="small" style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" name="auto_order" /><span>降价后自动下单（付款前要你确认）</span></label>
      <input name="name" placeholder="名称（可空）" />
      <button class="btn primary"><i data-i="plus"></i>添加价格监控</button>
    </form>
    <div id="shopList" class="list" style="margin-top:10px"></div>`;
  page.insertBefore(card, page.firstChild);
  if (typeof paintIcons === "function") try { paintIcons(card); } catch (e) {}
}

async function shopMachines() {
  const sel = $("shopMachine"); if (!sel || sel.dataset.done) return;
  try {
    const ms = await api("GET", "/api/machines");
    SHOP_MS = ms;
    // 3.8.2: default to a local computer (home IP, the logged-in Chrome), not whichever one is selected now
    const local = (ms.find((m) => m.provider !== "instabox" && !String(m.id).startsWith("cloud-")) || {}).id || MID;
    sel.innerHTML = ms.map((m) => `<option value="${esc(m.id)}" ${m.id === local ? "selected" : ""}>在「${esc(m.name || m.id)}」上检查${m.provider === "instabox" ? "（云）" : ""}</option>`).join("");
    sel.dataset.done = "1";
  } catch (e) {}
}

let SHOP_MS = [];
function shopMachineName(id) { const m = SHOP_MS.find((x) => x.id === id); return m ? (m.name || m.id) : (id || "未指定"); }
async function shopSetMachine(w, mid) {   // 3.8.2: which computer this watch runs on (only that one, never another)
  try {
    await api("PUT", `/api/watches/${w.id}`, { name: w.name, url: w.url, mode: "shop", value: String(w.value), sku: w.sku || "",
      address_note: w.address_note || "", interval_min: w.interval_min, quantity: w.quantity || 1, auto_order: !!w.auto_order,
      enabled: !!w.enabled, machine_id: mid });
  } catch (e) { toast("没能换电脑：" + e.message); }
  loadWatches();
}
function shopChip(map, k) { const v = map[k]; return v ? `<span class="chip ${v[1]}">${v[0]}</span>` : ""; }
function yuan(v) { return v == null ? "—" : "¥" + (+v).toFixed(2).replace(/\.00$/, ""); }

/* v8 (3.8.9): errors stored by older servers read in Chinese too (the server translates new ones itself) */
function shopCn(t) {
  return String(t || "")
    .replace(/probe failed: timeout after (\d+)s?/gi, "检查超时（$1 秒没有结束）")
    .replace(/timeout after (\d+(?:\.\d+)?)s?/gi, "超时（$1 秒）")
    .replace(/^probe failed: no output$/i, "探针没有输出（电脑上的 python3 没运行起来？）")
    .replace(/^probe failed:\s*/i, "检查失败：")
    .replace(/NO_CDP:?\s*/gi, "连不上浏览器调试端口：")
    .replace(/Failed to fetch/gi, "请求被浏览器拦下");
}
const SHOP_GENERIC = /^\s*(京东\s*[(（]?\s*JD\.COM\s*[)）]?|JD\.COM|京东)\s*[-–—_|·]?\s*(正品低价|多快好省)|正品低价、品质保障、配送及时、轻松购物/i;
function shopTitle(w) {   // never JD's site-wide title (a page that did not render): the user's own name instead
  const t = (w.item_title || "").trim();
  return t && !SHOP_GENERIC.test(t) ? t : (w.name || "");
}
function shopNote(w, text) { return `<div class="sw-note">${text}</div>`; }

/* v8 (3.8.9): one full-width card per watch: title (2 lines), chips, price, meta, notes / error, the latest
   立即检查 line, then the computer and the actions in a row that wraps (≤480 px: stacked, buttons in a grid). */
function shopRow(w) {
  const site = SHOP_SITE[w.site] || "电商";
  const mname = shopMachineName(w.machine_id);
  // compared against the target: the after-coupon price (券后/到手), the list price only when there is none
  const price = (w.final_price != null
    ? (w.price != null && +w.price !== +w.final_price ? `原价 ${yuan(w.price)} · ` : "") + `券后 <b>${yuan(w.final_price)}</b>`
    : `现价 <b>${yuan(w.price)}</b>`) + ` · 目标 ${yuan(w.value)}`;
  const stock = w.in_stock == null ? "" : w.in_stock ? " · 有货" : ' · <span class="sw-bad">无货</span>';
  const title = shopTitle(w);
  const ms = SHOP_MS.length ? SHOP_MS : [{ id: w.machine_id, name: w.machine_id }];
  const notes = [];
  if (w.shop_status === "login") notes.push(`这台电脑的 Chrome 还没登录${site}：到「电脑」页点接管，在 Chrome 里登录一次（扫码即可）。`);
  if (w.shop_status === "captcha") notes.push(`「${esc(mname)}」上遇到滑块验证：到这台电脑的桌面接管，完成一次验证。`);
  if (w.shop_status === "risk_limit") notes.push(`${esc(shopCn(w.status_note) || "京东风控限制了这台电脑的访问（活动火爆页）")}。这不是验证码：可以在「${esc(mname)}」的桌面里手动打开一次这个商品，或等几个小时，检查已自动放慢。`);
  if (w.shop_status === "need_login") notes.push(`${esc(shopCn(w.status_note))}：到「电脑」页点接管，在浏览器里登录京东一次（扫码即可）。`);
  if (["machine_busy", "machine_offline", "no_machine"].includes(w.shop_status)) notes.push(esc(shopCn(w.status_note)));
  const quiet = ["login", "need_login", "captcha", "risk_limit", "machine_busy", "machine_offline", "no_machine"];
  const err = w.last_error && !quiet.includes(w.shop_status)
    ? `<div class="sw-err">${esc(shopCn(w.last_error))}<span class="sw-errn">（已失败 ${w.fail_count} 次${w.fail_count > 1 ? "，已自动放慢；点「立即检查」会重新计数" : ""}）</span></div>` : "";
  return `<div class="item shopw" id="shopw-${w.id}">
    <div class="sw-head"><div class="iconbox sm">${icon("target")}</div>
      <div class="sw-title" title="${esc(title)}">${esc(title)}</div></div>
    <div class="sw-chips"><span class="chip">${site} ≤ ¥${esc(w.value)}</span>${w.auto_order ? '<span class="chip warn">自动下单</span>' : ""}${shopChip(SHOP_STATUS, w.shop_status)}${shopChip(SHOP_ORDER, w.order_status)}${w.enabled ? "" : '<span class="chip">已暂停</span>'}</div>
    <div class="sw-price">${w.last_checked ? price + stock : "等待首次检查"} · 数量 ${w.quantity || 1}${w.sku ? " · 规格 " + esc(w.sku) : ""}${w.address_note ? " · " + esc(w.address_note) : ""}</div>
    <div class="sw-meta">每 ${w.interval_min} 分钟 · 上次 ${when(w.last_checked)}${w.triggered_at ? " · 触发于 " + when(w.triggered_at) : ""} · <a href="${esc(w.url)}" target="_blank" rel="noopener">打开商品</a>${w.order_task_id ? ` · <a href="#" onclick="shopOpenTask('${w.order_task_id}');return false">查看下单过程</a>` : ""}</div>
    ${notes.map((n) => shopNote(w, n)).join("")}${err}
    ${shopRunLine(w)}
    <div class="sw-foot">
      <label class="sw-machine"><span title="只用这台电脑检查，不借别的电脑">检查电脑</span><select class="sm" onchange='shopSetMachine(${esc(JSON.stringify(w))}, this.value)'>${ms.map((m) => `<option value="${esc(m.id)}" ${m.id === w.machine_id ? "selected" : ""}>${esc(m.name || m.id)}${m.provider === "instabox" ? "（云）" : ""}</option>`).join("")}</select></label>
      <div class="sw-acts"><button class="btn sm" id="shopBtn-${w.id}" onclick="shopCheck('${w.id}')" ${SHOP_RUN[w.id] ? "disabled" : ""}>${SHOP_RUN[w.id] ? "检查中…" : "立即检查"}</button>${w.diag_at ? `<button class="btn sm" onclick="shopDiag('${w.id}')">查看诊断</button>` : ""}<button class="btn sm" onclick='toggleShop(${esc(JSON.stringify(w))})'>${w.enabled ? "暂停" : "启用"}</button><button class="btn sm danger" onclick="delRow('watches','${w.id}',loadWatches)">删除</button></div>
    </div></div>`;
}

loadWatches = async function () {
  shopCard(); await shopMachines();
  const ws = await api("GET", "/api/watches");
  const shop = ws.filter((w) => w.mode === "shop"), rest = ws.filter((w) => w.mode !== "shop");
  $("shopList").innerHTML = shop.map(shopRow).join("") || `<div class="empty">还没有价格监控。也可以在对话里说：帮我盯着这个链接，低于 199 就下单。</div>`;
  $("watchList").innerHTML = rest.map((w) => `<div class="item"><div class="iconbox sm">${icon("target")}</div><div class="grow">
    <div><b>${esc(w.name)}</b> <span class="chip">${MODE_CN[w.mode] || w.mode}${w.value ? " " + esc(w.value) : ""}</span> ${w.enabled ? "" : '<span class="chip">已暂停</span>'}</div>
    <div class="small"><a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.url.slice(0, 70))}</a></div>
    <div class="small">每 ${w.interval_min} 分钟 · 上次 ${when(w.last_checked)}${w.mode === "price_below" && w.last_value ? " · 当前 " + esc(w.last_value) : ""}${w.triggered_at ? " · 提醒于 " + when(w.triggered_at) : ""}</div>
    ${w.last_error ? `<div class="small" style="color:var(--danger)">${esc(w.last_error)}（已失败 ${w.fail_count} 次，自动退避）</div>` : ""}</div>
    <div class="acts"><button class="btn sm" onclick="checkWatch('${w.id}')">立即检查</button>
    <button class="btn sm" onclick="toggleWatch(${esc(JSON.stringify(w))})">${w.enabled ? "暂停" : "启用"}</button>
    <button class="btn sm danger" onclick="delRow('watches','${w.id}',loadWatches)">删除</button></div></div>`).join("") || `<div class="empty">还没有追踪</div>`;
};

async function addShopWatch(e) {
  e.preventDefault(); const f = e.target;
  const body = { mode: "shop", url: f.url.value.trim(), value: f.target_price.value, quantity: +f.quantity.value || 1,
    sku: f.sku.value, address_note: f.address_note.value, interval_min: +f.interval_min.value, machine_id: f.machine_id.value || null,
    auto_order: f.auto_order.checked, name: f.name.value };
  if (body.auto_order && !confirm(`降到 ¥${body.value} 时，AIW 会自动把商品放进订单并停在付款前，推送给你确认后才付款。开启？`)) return;
  try { await api("POST", "/api/watches", body); f.reset(); f.quantity.value = 1; f.interval_min.value = 30;
    toast("已添加，1 分钟内开始首次检查"); loadWatches(); } catch (err) { toast(err.message); }
}

async function toggleShop(w) {
  await api("PUT", `/api/watches/${w.id}`, { ...w, auto_order: !!w.auto_order, enabled: !w.enabled, target_price: w.value });
  loadWatches();
}

async function shopOpenTask(tid) {
  try { const t = await api("GET", `/api/tasks/${tid}`); if (t.thread_id) { go("chat"); openThread(t.thread_id); } } catch (e) { toast(e.message); }
}

/* v6: 立即检查 runs as a job; the card shows its stage (推送探针 → 连接浏览器 → 打开商品页 → 等待价格渲染 → 读取价格 → 完成) */
const SHOP_RUN = {};    // watch id -> {job, label, t0}
const SHOP_LAST = {};   // watch id -> {text, at, ok}
function shopRunLine(w) {
  const run = SHOP_RUN[w.id];
  if (run) return `<div class="sw-run" id="shopRun-${w.id}"><span class="spin"></span><span>正在检查：${esc(run.label)} · 已 ${Math.round((Date.now() - run.t0) / 1000)} 秒</span></div>`;
  const last = SHOP_LAST[w.id];
  if (last) {
    const t = new Date(last.at).toLocaleTimeString("zh-CN", { hour12: false });
    return `<div class="sw-run ${last.ok ? "ok" : "bad"}" id="shopRun-${w.id}"><b>${last.ok ? "✓" : "!"}</b><span>刚才 ${t} 立即检查：${esc(shopCn(last.text))}</span></div>`;
  }
  return `<div class="sw-run empty" id="shopRun-${w.id}"></div>`;
}
function shopPaintRun(id) {
  const el = $("shopRun-" + id); const w = { id };
  if (el) el.outerHTML = shopRunLine(w);
  const b = $("shopBtn-" + id); if (b) { b.disabled = !!SHOP_RUN[id]; b.textContent = SHOP_RUN[id] ? "检查中…" : "立即检查"; }
}
async function shopCheck(id) {
  if (SHOP_RUN[id]) return;
  SHOP_RUN[id] = { label: "开始检查", t0: Date.now() }; delete SHOP_LAST[id]; shopPaintRun(id);
  let job;
  try { job = (await api("POST", `/api/watches/${id}/check?async=1`)).job; }
  catch (e) { delete SHOP_RUN[id]; SHOP_LAST[id] = { text: "没能开始：" + e.message, at: Date.now(), ok: false }; shopPaintRun(id); return; }
  const tick = setInterval(() => shopPaintRun(id), 1000);
  try {
    for (let i = 0; i < 400 && job && job.state !== "done"; i++) {
      await new Promise((ok) => setTimeout(ok, 1200));
      try { job = await api("GET", `/api/watches/${id}/check/${job.id}`); } catch (e) { if (/404/.test(e.message) || /没有这个检查/.test(e.message)) break; continue; }
      if (SHOP_RUN[id]) SHOP_RUN[id].label = job.label || job.stage || "检查中";
    }
  } finally { clearInterval(tick); }
  const r = (job && job.result) || {};
  delete SHOP_RUN[id];
  SHOP_LAST[id] = { text: job && job.state === "done" ? (job.label || "").replace(/^完成：/, "") : "检查没有结束（服务器重启过？）",
    at: Date.now(), ok: !!r.ok };
  toast(r.ok ? (r.triggered ? "触发提醒！" : "检查完成") : "失败：" + shopCn(r.note || r.error || r.status || "没有结果"));
  loadWatches();
}
checkWatch = async function (id) {   // other watches on the 追踪 page use the same job (stage text in the toast)
  let job;
  try { job = (await api("POST", `/api/watches/${id}/check?async=1`)).job; } catch (e) { toast("失败：" + e.message); return; }
  toast("检查中…");
  for (let i = 0; i < 300 && job.state !== "done"; i++) {
    await new Promise((ok) => setTimeout(ok, 1200));
    try { job = await api("GET", `/api/watches/${id}/check/${job.id}`); } catch (e) { break; }
  }
  const r = job.result || {};
  toast(r.ok ? (r.triggered ? "触发提醒！" : "检查完成，没有新变化") : "失败：" + (r.note || r.error || r.status || "没有结果"));
  loadWatches();
};
async function shopDiag(id) {
  let d;
  try { d = await api("GET", `/api/watches/${id}/diag`); } catch (e) { toast(e.message); return; }
  const row = (k, v) => v == null || v === "" || (Array.isArray(v) && !v.length) ? "" : `<tr><td class="small" style="white-space:nowrap;vertical-align:top;padding-right:8px">${k}</td><td class="small" style="word-break:break-all">${esc(typeof v === "string" ? v : JSON.stringify(v))}</td></tr>`;
  const stages = (d.probe_stages && d.probe_stages.length ? d.probe_stages : d.stages || []).map((s) => `${s.label} ${s.ms != null ? (s.ms / 1000).toFixed(1) + "s" : ""}`).join(" → ");
  const shot = d.screenshot ? `<img src="/api/watches/${id}/diag.jpg?token=${encodeURIComponent(TOKEN)}&t=${Date.now()}" style="width:100%;border-radius:8px;border:1px solid var(--line,#ddd);margin:8px 0" />` : '<div class="muted small">这次没有截图（只在读不到价格/风控/要登录时截图）</div>';
  modal(`<div class="heading">诊断 · ${esc(SHOP_STATUS[d.status] ? SHOP_STATUS[d.status][0] : d.status || "")}</div>
    <div class="small">${esc(when(d.at))} · 探针 v${esc(d.probe_version || d.server_probe_version || "?")}${d.probe_answered === false ? "（已推送，探针没回话）" : ""}（服务器带 v${esc(d.server_probe_version || "?")}）· 服务器 ${esc(d.server_version || "")}</div>
    ${(d.hints || []).length ? `<div class="small" style="color:var(--warn,#c87618);margin-top:6px">${d.hints.map(esc).join("<br>")}</div>` : ""}
    ${shot}
    <table style="width:100%;border-collapse:collapse">${row("说明", d.note)}${row("最终网址", d.final_url)}${row("页面标题", d.page_title)}${row("标题来自", d.title_from)}
    ${row("页面状态", [d.ready, d.visibility, d.fronted ? "已切前台" : ""].filter(Boolean).join(" · "))}${row("元素数", d.n_elems)}
    ${row("价格元素", (d.price_elements || []).map((x) => x.role + ":" + x.t))}${row("¥ 文字", d.price_texts)}${row("价格附近文字", d.snippets)}
    ${row("页面价格接口", (d.net || []).map((n) => n.src + (n.status ? " " + n.status : "") + (n.price ? " ✓" : "") + (n.error ? " " + n.error : "")))}
    ${row("直接调用接口", (d.api || []).map((a) => a.src + " " + (a.status || a.error || "")))}${row("移动版", d.mobile)}
    ${row("浏览器配置", d.profile)}${row("阶段耗时", stages)}${row("超时", d.stage_error)}${row("错误", shopCn(d.error))}</table>
    <div style="margin-top:10px;text-align:right"><button class="btn sm" onclick="closeOverlays()">关闭</button></div>`);
}
