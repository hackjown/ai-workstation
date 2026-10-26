/* 京东/淘宝价格监控 on the 追踪 page (loaded after app.js + ui.js). Adds its own card and list and takes over
   loadWatches() so shop watches (mode "shop") get their own rows; other watches render as before. */
"use strict";

const SHOP_SITE = { jd: "京东", taobao: "淘宝", tmall: "天猫" };
const SHOP_STATUS = { ok: ["正常", "ok"], login: ["需要登录", "warn"], captcha: ["需要验证", "warn"], off_shelf: ["已下架", "bad"],
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
      <label class="small" style="display:flex;align-items:center;gap:6px"><input type="checkbox" name="auto_order" style="width:auto" />降价后自动下单（付款前要你确认）</label>
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
    sel.innerHTML = ms.map((m) => `<option value="${esc(m.id)}" ${m.id === MID ? "selected" : ""}>在「${esc(m.name || m.id)}」上检查${m.provider === "instabox" ? "（云）" : ""}</option>`).join("");
    sel.dataset.done = "1";
  } catch (e) {}
}

function shopChip(map, k) { const v = map[k]; return v ? `<span class="chip ${v[1]}">${v[0]}</span>` : ""; }
function yuan(v) { return v == null ? "—" : "¥" + (+v).toFixed(2).replace(/\.00$/, ""); }

function shopRow(w) {
  const site = SHOP_SITE[w.site] || "电商";
  const price = w.final_price != null ? `到手 <b>${yuan(w.final_price)}</b>（标价 ${yuan(w.price)}）` : `现价 <b>${yuan(w.price)}</b>`;
  const stock = w.in_stock == null ? "" : w.in_stock ? " · 有货" : ' · <span style="color:var(--danger)">无货</span>';
  return `<div class="item"><div class="iconbox sm">${icon("target")}</div><div class="grow">
    <div><b>${esc(w.item_title || w.name)}</b> <span class="chip">${site} ≤ ¥${esc(w.value)}</span>
      ${w.auto_order ? '<span class="chip warn">自动下单</span>' : ""} ${shopChip(SHOP_STATUS, w.shop_status)} ${shopChip(SHOP_ORDER, w.order_status)}
      ${w.enabled ? "" : '<span class="chip">已暂停</span>'}</div>
    <div class="small">${w.last_checked ? price + stock : "等待首次检查"} · 数量 ${w.quantity || 1}${w.sku ? " · 规格 " + esc(w.sku) : ""}${w.address_note ? " · " + esc(w.address_note) : ""}</div>
    <div class="small">每 ${w.interval_min} 分钟 · 上次 ${when(w.last_checked)}${w.triggered_at ? " · 触发于 " + when(w.triggered_at) : ""} · <a href="${esc(w.url)}" target="_blank" rel="noopener">打开商品</a>
      ${w.order_task_id ? ` · <a href="#" onclick="shopOpenTask('${w.order_task_id}');return false">查看下单过程</a>` : ""}</div>
    ${w.shop_status === "login" ? `<div class="small" style="color:var(--warn,#c87618)">这台电脑的 Chrome 还没登录${site}：到「Agent 电脑」页点接管，在 Chrome 里登录一次（扫码即可）。</div>` : ""}
    ${w.shop_status === "captcha" ? `<div class="small" style="color:var(--warn,#c87618)">遇到滑块验证：到「Agent 电脑」页接管，完成一次验证。</div>` : ""}
    ${w.last_error && w.shop_status !== "login" && w.shop_status !== "captcha" ? `<div class="small" style="color:var(--danger)">${esc(w.last_error)}（已失败 ${w.fail_count} 次，自动退避）</div>` : ""}</div>
    <div class="acts"><button class="btn sm" onclick="checkWatch('${w.id}')">立即检查</button>
    <button class="btn sm" onclick='toggleShop(${esc(JSON.stringify(w))})'>${w.enabled ? "暂停" : "启用"}</button>
    <button class="btn sm danger" onclick="delRow('watches','${w.id}',loadWatches)">删除</button></div></div>`;
}

loadWatches = async function () {
  shopCard(); shopMachines();
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
