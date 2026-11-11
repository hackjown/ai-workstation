/* v3.9.40 出行: the result cards of restaurant_booking (大众点评 / 美团 hand-off) and flight_search (机票比价), and the
   设置 › 出行 group. A card's button / row carries {app, web, prefer}:
     Android app (WebView): AiwAndroid.openApp(app, web) tries the app scheme, then the web link (App Links may still
       hand it to the installed app), then says so.
     phone browser: prefer=app -> the scheme first; still on this page after 1.6 s -> the web page.
     desktop: the web page in a new tab.
   aiw never books or pays here: the user confirms in the platform's app. */
"use strict";
const TRV = { links: {}, n: 0 };

function trvLinkId(l) { const k = "l" + (++TRV.n); TRV.links[k] = l || {}; return k; }

function aiwOpenLink(l) {
  l = l || {};
  const app = l.app || "", web = l.web || "", preferApp = l.prefer === "app" && !!app;
  try {
    if (window.AiwAndroid && typeof AiwAndroid.openApp === "function") { AiwAndroid.openApp(preferApp ? app : "", web); return; }
  } catch (e) { /* older app: fall through */ }
  const mobile = /iPhone|iPad|iPod|Android|HarmonyOS/i.test(navigator.userAgent);
  if (mobile && preferApp) {
    let left = false;
    const away = () => { left = true; };
    document.addEventListener("visibilitychange", away, { once: true });
    window.addEventListener("pagehide", away, { once: true });
    location.href = app;
    setTimeout(() => { if (!left && !document.hidden && web) location.href = web; }, 1600);
    return;
  }
  if (web) window.open(web, "_blank", "noopener");
}
function aiwOpenLinkId(k) { aiwOpenLink(TRV.links[k]); }
function aiwOpenAppId(k) { const l = TRV.links[k] || {}; aiwOpenLink({ app: l.app, web: l.web, prefer: "app" }); }

function trvRestaurant(c) {
  const btns = (c.buttons || []).map((b, i) => `<button type="button" class="btn sm ${i === 0 ? "primary" : ""}" onclick="aiwOpenLinkId('${trvLinkId(b)}')">${esc(b.label || "打开")}</button>`).join("");
  return `<div class="trv-card trv-rest">
    <div class="trv-h"><span class="trv-ic">🍽</span><div class="grow"><b>${esc(c.title || "")}</b>${c.subtitle ? `<div class="small muted">${esc(c.subtitle)}</div>` : ""}</div></div>
    ${(c.facts || []).length ? `<div class="trv-facts">${(c.facts || []).map(esc).join(" · ")}</div>` : ""}
    ${c.suggest ? `<div class="trv-sug">建议 ${esc(c.suggest)}</div>` : ""}
    ${c.reason ? `<div class="small">${esc(c.reason)}</div>` : ""}
    <div class="trv-btns">${btns}</div>
    ${c.note ? `<div class="small muted trv-note">${esc(c.note)}</div>` : ""}</div>`;
}

function trvPrice(p) { return p == null ? "—" : "¥" + (Math.round(p * 100) / 100); }

function trvFlights(c) {
  const legs = (c.legs || []).map((lg) => {
    const rows = (lg.rows || []).map((x) => `<button type="button" class="trv-row" onclick="aiwOpenLinkId('${trvLinkId(x.link)}')" title="在${esc(x.platform_name)}打开这条航线">
        <span class="trv-p">${trvPrice(x.price)}</span>
        <span class="trv-f"><b>${esc(x.flight_no)}</b>${x.transfer ? ' <span class="chip">中转</span>' : ""} ${esc(x.airline || "")}<span class="small muted"> ${esc(x.dep_time || "?")}–${esc(x.arr_time || "?")}${x.next_day ? "+1" : ""} ${esc([x.dep_airport, x.arr_airport].filter(Boolean).join(" → "))}</span></span>
        <span class="trv-src">${esc(x.platform_name)}<span class="small muted">${esc(x.tax_note || "")}</span></span></button>`).join("");
    const plats = (lg.platforms || []).map((p) => `<button type="button" class="chip trv-st st-${esc(p.status)}" onclick="aiwOpenLinkId('${trvLinkId(p.link)}')" title="${esc(p.note || "")}">${esc(p.name)} · ${esc(p.status_cn)}${p.min != null ? " " + trvPrice(p.min) : ""}</button>`).join("");
    return `<div class="trv-leg"><div class="trv-legh">${esc(lg.label || "")} · ${esc(lg.from || "")} → ${esc(lg.to || "")}<span class="small muted"> 共 ${lg.total || 0} 条</span></div>
      ${rows || '<div class="small muted">没有查到可比较的价格（见下面各平台状态，点平台可直接去查）</div>'}
      <div class="trv-plats">${plats}</div></div>`;
  }).join("");
  return `<div class="trv-card trv-fl">
    <div class="trv-h"><span class="trv-ic">✈️</span><div class="grow"><b>${esc(c.title || "机票比价")}</b><div class="small">${esc(c.subtitle || "")}${c.cabin ? " · " + esc(c.cabin) : ""}${c.adults > 1 ? ` · ${c.adults} 人（单价）` : ""}${c.cached ? " · 缓存" : ""}</div></div></div>
    ${legs}
    ${c.note ? `<div class="small muted trv-note">${esc(c.note)}</div>` : ""}</div>`;
}

function hTravelCards(cards) {
  if (!Array.isArray(cards) || !cards.length) return "";
  return `<div class="trv-cards">${cards.map((c) => (c.kind === "flights" ? trvFlights(c) : c.kind === "restaurant" ? trvRestaurant(c) : "")).join("")}</div>`;
}

/* ---------- 设置 › 出行 */
function travelSettingsHTML(cfg, no) {
  if (!cfg) return "";
  const names = cfg.platform_names || { ctrip: "携程", fliggy: "飞猪", qunar: "去哪儿", tongcheng: "同程" };
  const fp = cfg.flight_platforms || {};
  const on = Object.keys(names).filter((k) => fp[k]).length;
  const apps = cfg.restaurant_apps || ["dianping", "meituan"];
  const sw = (k) => `<label class="trv-sw"><input type="checkbox" name="fp_${k}" ${fp[k] ? "checked" : ""}> ${esc(names[k])}</label>`;
  return `<form class="grp" id="g-travel" onsubmit="travelSave(event)">
    <div class="grp-h"><h2>${no || ""}出行</h2><span class="chip ${on ? "ok" : ""}">机票 ${on} 个平台</span>
      <div class="act"><button class="btn sm primary">保存</button></div></div>
    <div class="grp-d">机票比价（flight_search）在电脑的浏览器里同时打开各平台的搜索页读价格，合并成按价格排序的表；订餐厅（restaurant_booking）生成大众点评/美团卡片，在手机 App 里确认。aiw 不下单、不付款。</div>
    <div class="rows">
      ${row("机票平台", "要比价的平台；需要登录或滑块验证的平台会显示「需登录/需验证」，不影响其他平台", `<div class="trv-sws">${Object.keys(names).map(sw).join("")}</div>`)}
      ${row("缓存", "分钟：同一航线同一天在这段时间内直接用上次的结果（0 = 不缓存）", `<input name="cache_minutes" value="${esc(String(cfg.cache_minutes ?? 10))}" inputmode="numeric" style="max-width:120px" />`)}
      ${row("每个平台的时限", "秒（20-160）：到点没出结果就记为「超时」", `<input name="timeout_s" value="${esc(String(cfg.timeout_s ?? 70))}" inputmode="numeric" style="max-width:120px" />`)}
      ${row("表格行数", "结果卡片里最多显示几条（5-40）", `<input name="max_rows" value="${esc(String(cfg.max_rows ?? 15))}" inputmode="numeric" style="max-width:120px" />`)}
      ${row("订餐厅按钮", "预订卡片上显示哪些 App 的按钮", `<div class="trv-sws"><label class="trv-sw"><input type="checkbox" name="ra_dianping" ${apps.includes("dianping") ? "checked" : ""}> 大众点评</label><label class="trv-sw"><input type="checkbox" name="ra_meituan" ${apps.includes("meituan") ? "checked" : ""}> 美团</label></div>`)}
      ${cfg.links_note ? `<div class="rw"><div class="k"></div><div class="v"><span class="small muted">${esc(cfg.links_note)}</span></div><div class="t"></div></div>` : ""}
    </div></form>`;
}

async function travelSave(e) {
  e.preventDefault();
  const f = e.target.elements, fp = {};
  ["ctrip", "fliggy", "qunar", "tongcheng"].forEach((k) => { if (f["fp_" + k]) fp[k] = f["fp_" + k].checked; });
  const apps = ["dianping", "meituan"].filter((k) => f["ra_" + k] && f["ra_" + k].checked);
  try {
    await api("PUT", "/api/travel/settings", { flight_platforms: fp, cache_minutes: f.cache_minutes.value, timeout_s: f.timeout_s.value,
      max_rows: f.max_rows.value, restaurant_apps: apps });
    toast("已保存");
  } catch (err) { toast(err.message); }
}
