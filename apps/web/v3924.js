/* v3.9.24 (loaded after app.js + ui.js): a 设置 group 「进阶功能」 with one page each for
   模型路由 · 自动审核 · Webhook · 用量统计 · 应用 · 提示词 · 上下文 · 教学录制 · 远程访问 · 自动签到.
   Same dark style as the rest of the settings (CSS variables from ui.css). */
"use strict";

const V24 = {};
const q24 = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n24 = (n) => (n == null ? "-" : n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "k" : String(n));
const t24 = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "-");

(function v24Style() {
  const st = document.createElement("style");
  st.textContent = `
  
  #v-adv .v24-wrap{max-width:900px;margin:0 auto;padding:18px 16px 90px}
  #v-adv .v24-top{display:flex;align-items:center;gap:10px}
  #v-adv .v24-top h2{flex:1;margin:0;font-size:20px}
  #v-adv .v24-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
  #v-adv .v24-card h3{margin:0 0 8px;font-size:15px}
  #v-adv .v24-g2{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
  #v-adv label.v24-f{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mute)}
  #v-adv label.v24-sw{display:flex;align-items:center;gap:8px;font-size:13px}
  #v-adv label.v24-sw{cursor:pointer;line-height:22px}
  #v-adv textarea{width:100%;min-height:90px;font:12px/1.5 var(--mono)}
  #v-adv textarea.v24-big{min-height:320px}
  #v-adv .v24-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
  #v-adv .v24-note{font-size:12px;color:var(--mute);line-height:18px}
  #v-adv table{width:100%;border-collapse:collapse;font-size:13px}
  #v-adv th,#v-adv td{text-align:left;padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
  #v-adv th{color:var(--mute);font-weight:500;border-top:0}
  #v-adv .v24-mono{font:12px/1.5 var(--mono);word-break:break-all}
  #v-adv .v24-barc{height:8px;border-radius:4px;background:var(--acc,#c87618);min-width:2px}
  #v-adv .v24-meter{display:flex;gap:10px;flex-wrap:wrap}
  #v-adv .v24-meter div{flex:1;min-width:120px;padding:8px 10px;border-radius:10px;background:var(--panel2)}
  #v-adv .v24-meter b{font-size:18px;display:block}
  #v-adv .v24-item{border-top:1px solid var(--line);padding:10px 0;display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
  #v-adv .v24-item:first-child{border-top:0}
  #v-adv .v24-item .v24-grow{flex:1;min-width:200px}
  #v-adv .v24-shots{display:flex;gap:6px;overflow-x:auto;padding:6px 0}
  #v-adv .v24-shots img{height:90px;border-radius:6px;border:1px solid var(--line);cursor:pointer}
  #v-adv pre{white-space:pre-wrap;word-break:break-word;font:12px/1.5 var(--mono);background:var(--panel2);padding:10px;border-radius:8px;max-height:420px;overflow:auto}
  #v-adv .v24-warn{padding:10px 12px;border-radius:10px;background:var(--badBg);border:1px solid var(--badLine);margin:10px 0;font-size:13px}
  .v24-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;padding:12px 16px}
  .v24-grid button{justify-content:flex-start}
  
  #v-adv .v24-card{padding:18px 20px;margin:0}
  #v-adv .v24-card h3{font-size:16px;font-weight:700;margin:0 0 10px}
  #v-adv #v24Body{display:flex;flex-direction:column;gap:14px}
  #v-adv .v24-note{display:block;padding:0;border-radius:0;cursor:auto;background:none}
  #v-adv .v24-note:hover{background:none}
  #v-adv .v24-item{background:none;border:0;border-top:1px solid var(--line);border-radius:0}
  #v-adv code,#v-adv .v24-mono{white-space:pre-wrap;word-break:break-word}
  #v-adv input:not([type=checkbox]),#v-adv select,#v-adv textarea{width:100%;box-sizing:border-box}
  #v-adv .v24-bar input:not([type=checkbox]){width:auto;flex:1;min-width:160px}
  #v-adv td input,#v-adv td select{min-width:0}
  @media(max-width:700px){#v-adv table{display:block;overflow-x:auto}}
`;
  document.head.appendChild(st);
})();

const V24_PAGES = [
  ["route", "模型路由", "按任务难度自动选模型和推理强度"],
  ["review", "自动审核", "允许/拒绝规则 + 模型审核，减少弹窗"],
  ["webhook", "Webhook", "外部系统签名调用，触发任务"],
  ["usage", "用量统计", "每天、每个模型的 token"],
  ["apps", "应用", "单文件 JSON 应用：代码 / MCP / 技能"],
  ["prompts", "提示词", "查看、编辑、恢复各角色提示词"],
  ["context", "上下文", "每个模型的窗口与压缩阈值"],
  ["teach", "教学录制", "演示一遍，生成技能"],
  ["remote", "远程访问", "局域网 / Tailscale / HTTPS / 内置隧道"],
  ["checkin", "自动签到", "每天自动签到，失败交给 Agent"],
];

const _v24LoadSettings = loadSettings;
loadSettings = async function () {
  await _v24LoadSettings.apply(this, arguments);
  try { v24Group(); } catch (e) { /* never break the settings page */ }
};

function v24Group() {
  const page = $("settingsPage"); if (!page || $("g-v24")) return;
  const host = page.querySelector(".set") || page;
  const sec = document.createElement("section");
  sec.className = "grp"; sec.id = "g-v24";
  sec.innerHTML = `<div class="v24-grp-h"><h2>进阶功能</h2><span class="v24-sub">v3.9.24 · 每项默认关闭或为空，不配置不影响现有行为</span></div>
    <div class="rows"><div class="v24-grid">${V24_PAGES.map(([k, t, d]) => `<button type="button" class="btn sm" title="${q24(d)}" onclick="v24Open('${k}')">${q24(t)}</button>`).join("")}</div></div>`;
  const before = host.querySelector("#g-notify") || host.querySelector("#g-me");
  if (before) host.insertBefore(sec, before); else host.appendChild(sec);
  const nav = page.querySelector(".set-index");
  if (nav && !nav.querySelector('[href="#g-v24"]')) {
    const a = document.createElement("a"); a.href = "#g-v24"; a.textContent = "进阶";
    a.onclick = (e) => { e.preventDefault(); sec.scrollIntoView({ behavior: "smooth" }); };
    nav.appendChild(a);
  }
}

function v24View() {
  let v = $("v-adv");
  if (!v) {
    const ref = $("v-routines");
    v = document.createElement("div"); v.className = "view scroll"; v.id = "v-adv";
    ref.parentNode.insertBefore(v, ref.nextSibling);
    if (typeof TITLES === "object") TITLES.adv = "进阶功能";
  }
  return v;
}
function v24Open(key) {
  const v = v24View();
  const p = V24_PAGES.find((x) => x[0] === key) || [key, key];
  v.innerHTML = `<div class="page"><div class="v24-top"><button class="btn sm" onclick="v24Close()">‹ 设置</button></div><div id="v24Body"><div class="muted">加载中…</div></div></div>`;
  clearInterval(V24.timer);
  V24.page = key;
  go("adv");
  $("viewTitle").textContent = p[1];
  (V24_RENDER[key] || (() => {}))().catch((e) => { $("v24Body").innerHTML = `<div class="card v24-card"><div class="v24-warn">${q24(e.message)}</div></div>`; });
}
function v24Close() { clearInterval(V24.timer); go("settings"); }
const v24Body = (h) => { $("v24Body").innerHTML = h; };
async function v24Do(fn, ok) { try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast(e.message); throw e; } }

/* ------------------------------------------------------------------ 模型路由 */
const EFF = [["off", "默认"], ["low", "低"], ["medium", "中"], ["high", "高"]];
const V24_RENDER = {};
V24_RENDER.route = async function () {
  const c = await api("GET", "/api/autoroute");
  const models = await api("GET", "/api/models").catch(() => ({}));
  const names = (models.models || []).filter(Boolean);
  const tier = (k, t) => `<tr><td>${t}</td><td><input name="m_${k}" list="v24models" value="${q24(c.tiers[k].model)}" placeholder="留空 = 主模型" /></td>
    <td><select name="e_${k}">${EFF.map(([v, l]) => `<option value="${v}" ${c.tiers[k].effort === v ? "selected" : ""}>${l}</option>`).join("")}</select></td></tr>`;
  v24Body(`<form id="v24f" onsubmit="v24SaveRoute(event)">
    <div class="card v24-card"><label class="v24-sw"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}/>开启自动模型路由</label>
      ${c.env === "0" ? '<div class="v24-warn">环境变量 AIW_AUTOROUTE=0，已强制关闭</div>' : ""}
      <label class="v24-sw"><input type="checkbox" name="use_model" ${c.use_model ? "checked" : ""}/>规则拿不准时用小模型判断（多一次很短的调用）</label>
      <label class="v24-sw"><input type="checkbox" name="apply_chat" ${c.apply_chat ? "checked" : ""}/>聊天也按难度选模型</label>
      <div class="v24-note">先用你的规则，再用内置关键词+长度打分。任务里手动指定模型时永远以你的为准。推理强度以 reasoning_effort 发给网关，网关不支持时自动去掉。</div></div>
    <div class="card v24-card"><h3>三档</h3><table><tr><th>难度</th><th>模型</th><th>推理强度</th></tr>${tier("easy", "简单")}${tier("normal", "普通")}${tier("hard", "困难")}</table>
      <datalist id="v24models">${names.map((n) => `<option value="${q24(n)}">`).join("")}</datalist></div>
    <div class="card v24-card"><h3>你的规则</h3><div class="v24-note">每行一条：<span class="v24-mono">正则 =&gt; easy|normal|hard</span>，从上到下第一条命中的生效。</div>
      <textarea name="rules" placeholder="周报|日报 => easy&#10;CAD|户型 => hard">${q24((c.rules || []).map((r) => `${r.pattern} => ${r.tier}`).join("\n"))}</textarea></div>
    <div class="v24-bar"><button class="btn sm primary">保存</button></div></form>
    <div class="card v24-card"><h3>试一试</h3><div class="v24-bar"><input id="v24rt" placeholder="输入一句任务，看会分到哪一档" style="flex:1" /><button class="btn sm" onclick="v24TestRoute()">判断</button></div><div id="v24rto" class="v24-note"></div></div>`);
};
async function v24SaveRoute(e) {
  e.preventDefault(); const f = e.target;
  const rules = f.rules.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => { const m = l.split("=>"); return { pattern: (m[0] || "").trim(), tier: (m[1] || "").trim() }; });
  const tiers = {}; for (const k of ["easy", "normal", "hard"]) tiers[k] = { model: f["m_" + k].value.trim(), effort: f["e_" + k].value };
  await v24Do(() => api("PUT", "/api/autoroute", { enabled: f.enabled.checked, use_model: f.use_model.checked, apply_chat: f.apply_chat.checked, tiers, rules }), "已保存");
}
async function v24TestRoute() {
  const r = await v24Do(() => api("POST", "/api/autoroute/test", { text: $("v24rt").value }));
  $("v24rto").innerHTML = `→ <b>${q24(r.route.tier_cn)}</b>：${q24(r.route.model || "主模型")} · 推理 ${q24((EFF.find((x) => x[0] === r.route.effort) || [0, "默认"])[1])}<br>规则：${q24(r.rules.reason)}${r.rules.sure ? "" : "（不确定）"}${r.model ? "<br>小模型：" + q24(r.model.reason) : ""}`;
}

/* ------------------------------------------------------------------ 自动审核 */
V24_RENDER.review = async function () {
  const [c, log] = await Promise.all([api("GET", "/api/autoreview"), api("GET", "/api/autoreview/log?limit=60")]);
  v24Body(`<form onsubmit="v24SaveReview(event)">
    <div class="card v24-card"><label class="v24-sw"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}/>开启自动审核（只作用于原本会弹授权卡片的操作）</label>
      <label class="v24-sw"><input type="checkbox" name="use_model" ${c.use_model ? "checked" : ""}/>规则都没命中时问模型（allow / ask / deny）</label>
      <label class="v24-sw"><input type="checkbox" name="model_can_deny" ${c.model_can_deny ? "checked" : ""}/>模型判 deny 时直接拒绝（关掉则改为弹卡片）</label>
      <label class="v24-f">审核模型（留空 = 小模型）<input name="model" value="${q24(c.model)}" /></label>
      <div class="v24-note">顺序：拒绝规则 → 付款/发消息/下单永远弹卡片 → 允许规则 → 模型 → 卡片。拿不准一律回到原来的授权卡片。</div></div>
    <div class="v24-g2"><div class="card v24-card"><h3>允许规则</h3><textarea name="allow" placeholder="shell:git status*&#10;shell:docker ps*&#10;re:^shell (ls|cat) ">${q24((c.allow || []).join("\n"))}</textarea></div>
      <div class="card v24-card"><h3>拒绝规则（永远优先）</h3><textarea name="deny" placeholder="*rm -rf /*&#10;re:mkfs|dd if=">${q24((c.deny || []).join("\n"))}</textarea></div></div>
    <div class="v24-note">每行一条，匹配「工具名 + 命令/网址/路径」。写法：<span class="v24-mono">shell:git *</span>（工具前缀 + 通配）、<span class="v24-mono">*关键字*</span>、<span class="v24-mono">re:正则</span>；# 开头是注释。</div>
    <div class="v24-bar"><button class="btn sm primary">保存</button></div></form>
    <div class="card v24-card"><h3>试一试</h3><div class="v24-bar"><input id="v24rvt" value="shell" style="width:120px" /><input id="v24rvc" placeholder="命令，如 rm -rf ./build" style="flex:1" /><button class="btn sm" onclick="v24TestReview()">判断</button></div><div id="v24rvo" class="v24-note"></div></div>
    <div class="card v24-card"><h3>最近的决定</h3><table><tr><th>时间</th><th>结果</th><th>依据</th><th>操作</th></tr>
      ${log.map((x) => `<tr><td>${t24(x.ts)}</td><td>${{ allow: "✅ 允许", deny: "⛔ 拒绝", ask: "❔ 交给你" }[x.verdict] || q24(x.verdict)}</td><td>${q24(x.by)} · ${q24(x.reason)}</td><td class="v24-mono">${q24(x.subject)}</td></tr>`).join("") || '<tr><td colspan="4" class="v24-note">还没有记录</td></tr>'}</table></div>`);
};
async function v24SaveReview(e) {
  e.preventDefault(); const f = e.target;
  await v24Do(() => api("PUT", "/api/autoreview", { enabled: f.enabled.checked, use_model: f.use_model.checked, model_can_deny: f.model_can_deny.checked,
    model: f.model.value.trim(), allow: f.allow.value, deny: f.deny.value }), "已保存");
}
async function v24TestReview() {
  const r = await v24Do(() => api("POST", "/api/autoreview/test", { tool: $("v24rvt").value.trim(), command: $("v24rvc").value }));
  $("v24rvo").textContent = `${{ allow: "✅ 允许", deny: "⛔ 拒绝", ask: "❔ 弹卡片" }[r.verdict]} · ${r.by} · ${r.reason}`;
}

/* ------------------------------------------------------------------ Webhook */
V24_RENDER.webhook = async function () {
  const [list, ms] = await Promise.all([api("GET", "/api/webhooks"), api("GET", "/api/machines").catch(() => [])]);
  const base = location.origin;
  const opts = (sel) => `<option value="">自动选择</option>` + ms.map((m) => `<option value="${q24(m.id)}" ${m.id === sel ? "selected" : ""}>${q24(m.name || m.id)}</option>`).join("");
  v24Body(`${V24.newSecret ? `<div class="v24-warn">新密钥只显示这一次，请复制保存：<div class="v24-mono">${q24(V24.newSecret)}</div></div>` : ""}
    <div class="card v24-card"><h3>新建</h3><form onsubmit="v24AddHook(event)"><div class="v24-g2"><label class="v24-f">名称<input name="name" placeholder="如 GitHub 构建" /></label><label class="v24-f">在哪台电脑<select name="machine_id">${opts("")}</select></label></div>
      <label class="v24-f">任务模板（{{payload}} 整个 JSON，{{payload.a.b}} 单个字段，{{now}} 时间）<textarea name="prompt" placeholder="构建 {{payload.repo}} 的结果是 {{payload.status}}，失败的话看日志并告诉我原因"></textarea></label>
      <div class="v24-bar"><button class="btn sm primary">创建</button></div></form></div>
    ${list.map((w) => `<div class="card v24-card"><div class="v24-item"><div class="v24-grow"><b>${q24(w.name)}</b> ${w.enabled ? '<span class="chip ok">启用</span>' : '<span class="chip">停用</span>'}
        <div class="v24-mono">POST ${q24(base + w.path)}</div><div class="v24-note">调用 ${w.calls || 0} 次 · 最近 ${t24(w.last_call)}</div></div>
        <div class="v24-bar"><button class="btn sm" onclick="v24HookToggle('${w.id}',${w.enabled ? 0 : 1})">${w.enabled ? "停用" : "启用"}</button><button class="btn sm" onclick="v24HookRotate('${w.id}')">轮换密钥</button>
        <button class="btn sm" onclick="v24HookCalls('${w.id}')">最近调用</button><button class="btn sm ghost" style="color:var(--bad)" onclick="v24HookDel('${w.id}')">删除</button></div></div>
        <details><summary class="v24-note">调用示例（bash）</summary><pre>BODY='{"status":"ok"}'; TS=$(date +%s)
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')"
curl -X POST ${q24(base + w.path)} -H "Content-Type: application/json" \\
  -H "X-AIW-Timestamp: $TS" -H "X-AIW-Signature: $SIG" -d "$BODY"</pre><div class="v24-note">签名 = HMAC-SHA256(密钥, "时间戳.原始请求体")，时间戳 ±5 分钟内有效，同一签名只接受一次。</div></details>
        <div id="v24calls_${w.id}"></div></div>`).join("") || '<div class="v24-note">还没有 webhook</div>'}`);
  V24.newSecret = null;
};
async function v24AddHook(e) { e.preventDefault(); const f = e.target; const r = await v24Do(() => api("POST", "/api/webhooks", { name: f.name.value, prompt: f.prompt.value, machine_id: f.machine_id.value || null })); V24.newSecret = r.secret; V24_RENDER.webhook(); }
async function v24HookToggle(id, on) { await v24Do(() => api("PUT", `/api/webhooks/${id}`, { enabled: !!on })); V24_RENDER.webhook(); }
async function v24HookRotate(id) { if (!confirm("轮换后旧密钥立即失效，继续？")) return; const r = await v24Do(() => api("POST", `/api/webhooks/${id}/rotate`)); V24.newSecret = r.secret; V24_RENDER.webhook(); }
async function v24HookDel(id) { if (!confirm("删除这个 webhook？")) return; await v24Do(() => api("DELETE", `/api/webhooks/${id}`)); V24_RENDER.webhook(); }
async function v24HookCalls(id) {
  const c = await api("GET", `/api/webhooks/${id}/calls`);
  $("v24calls_" + id).innerHTML = `<table><tr><th>时间</th><th>状态</th><th>任务 / 错误</th><th>来源</th></tr>${c.map((x) => `<tr><td>${t24(x.ts)}</td><td>${x.ok ? "✅" : "❌"} ${x.status}</td><td class="v24-mono">${q24(x.task_id || x.error)}</td><td class="v24-mono">${q24(x.ip)}</td></tr>`).join("") || '<tr><td colspan="4" class="v24-note">没有调用</td></tr>'}</table>`;
}

/* ------------------------------------------------------------------ 用量统计 */
V24_RENDER.usage = async function (days) {
  days = days || V24.usageDays || 14; V24.usageDays = days;
  const u = await api("GET", `/api/usage?days=${days}`);
  const max = Math.max(1, ...u.per_day.map((d) => d.total));
  v24Body(`<div class="v24-bar">${[1, 7, 14, 30, 90].map((d) => `<button class="btn sm ${d === days ? "primary" : ""}" onclick="V24_RENDER.usage(${d})">${d === 1 ? "今天" : d + " 天"}</button>`).join("")}
      ${u.recording ? "" : '<span class="chip">已停止记录（AIW_USAGE=0）</span>'}</div>
    <div class="card v24-card"><div class="v24-meter"><div><b>${n24(u.total.total)}</b>总 token</div><div><b>${n24(u.total.input)}</b>输入</div><div><b>${n24(u.total.output)}</b>输出</div><div><b>${u.total.calls}</b>次调用</div></div></div>
    <div class="card v24-card"><h3>每天</h3><table><tr><th>日期</th><th style="width:40%"></th><th>输入</th><th>输出</th><th>模型</th></tr>
      ${u.per_day.map((d) => `<tr><td>${d.day}</td><td><div class="v24-barc" style="width:${(100 * d.total / max).toFixed(1)}%"></div></td><td>${n24(d.input)}</td><td>${n24(d.output)}</td>
        <td class="v24-note">${d.models.map((m) => `${q24(m.model)} ${n24(m.total)}`).join("<br>")}</td></tr>`).join("") || '<tr><td colspan="5" class="v24-note">这段时间没有记录</td></tr>'}</table></div>
    <div class="v24-g2"><div class="card v24-card"><h3>按模型</h3><table>${u.per_model.map((m) => `<tr><td class="v24-mono">${q24(m.model)}</td><td>${n24(m.input + m.output)}</td><td class="v24-note">${m.calls} 次</td></tr>`).join("")}</table></div>
      <div class="card v24-card"><h3>按角色</h3><table>${u.per_role.map((m) => `<tr><td>${q24({ main: "任务主模型", chat: "聊天", small: "小模型", vision: "看图", subagent: "子代理", review: "审核", geometry: "定位" }[m.role] || m.role)}</td><td>${n24(m.input + m.output)}</td><td class="v24-note">${m.calls} 次</td></tr>`).join("")}</table></div></div>
    <div class="card v24-card"><h3>最耗 token 的任务</h3><table>${u.top_tasks.map((t) => `<tr><td>${q24(t.title || t.task_id)}</td><td>${n24(t.input + t.output)}</td></tr>`).join("") || '<tr><td class="v24-note">无</td></tr>'}</table></div>
    <div class="v24-note">数字来自网关返回的 usage；网关不返回 usage 的调用不计入。日期按北京时间。</div>`);
};

/* ------------------------------------------------------------------ 应用 */
V24_RENDER.apps = async function () {
  const r = await api("GET", "/api/apps");
  const kind = { code: "代码", mcp: "MCP", skill: "技能" };
  v24Body(`${r.enabled ? "" : '<div class="v24-warn">AIW_APPS=0：所有应用已关闭</div>'}
    <div class="card v24-card"><h3>导入应用</h3><div class="v24-note">一个应用就是一个 JSON 文件（"aiw_app": 1）。也可以在任务里让 Agent 用「编写应用」工具写一个，它会出现在这里等你启用。</div>
      <textarea id="v24appj" placeholder='{"aiw_app":1,"id":"hello","name":"你好","kind":"skill","skill":{"text":"…"}}'></textarea>
      <div class="v24-bar"><input type="file" accept=".json,application/json" onchange="v24AppFile(this)" /><button class="btn sm primary" onclick="v24AppImport()">导入</button></div></div>
    ${r.apps.map((a) => `<div class="card v24-card"><div class="v24-item"><div class="v24-grow"><b>${q24(a.name)}</b> <span class="chip nodot">${kind[a.kind]}</span> <span class="v24-note">v${q24(a.version)} · ${q24(a.id)}${a.source === "agent" ? " · Agent 编写" : a.source === "teach" ? " · 教学录制" : ""}</span>
        <div class="v24-note">${q24(a.description)}</div>
        ${a.secrets.map((s) => `<div class="v24-bar"><span class="v24-mono">${q24(s.name)}</span>${s.set ? '<span class="chip ok">已设置</span>' : '<span class="chip bad">未设置</span>'}<input type="password" id="v24s_${q24(s.name)}" placeholder="填入密钥（加密保存）" autocomplete="off" /><button class="btn sm" onclick="v24AppSecret('${q24(s.name)}')">保存</button></div>`).join("")}
        <div id="v24at_${a.id}" class="v24-note"></div></div>
        <div class="v24-bar"><label class="v24-sw"><input type="checkbox" ${a.enabled ? "checked" : ""} onchange="v24AppToggle('${a.id}',this.checked)"/>启用</label>
          <button class="btn sm" onclick="v24AppTest('${a.id}')">测试</button><a class="btn sm" href="/api/apps/${a.id}/export${TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""}" download>导出</a>
          <button class="btn sm ghost" style="color:var(--bad)" onclick="v24AppDel('${a.id}')">删除</button></div></div></div>`).join("") || '<div class="v24-note">还没有应用</div>'}`);
};
function v24AppFile(inp) { const f = inp.files[0]; if (!f) return; f.text().then((t) => { $("v24appj").value = t; }); }
async function v24AppImport() {
  let m; try { m = JSON.parse($("v24appj").value); } catch (e) { toast("不是有效的 JSON"); return; }
  const r = await v24Do(() => api("POST", "/api/apps/import", m), "已导入"); if (r) V24_RENDER.apps();
}
async function v24AppToggle(id, on) { await v24Do(() => api("PUT", `/api/apps/${id}/enabled`, { enabled: on }), on ? "已启用" : "已停用"); }
async function v24AppDel(id) { if (!confirm("删除这个应用？")) return; await v24Do(() => api("DELETE", `/api/apps/${id}`)); V24_RENDER.apps(); }
async function v24AppTest(id) { const r = await v24Do(() => api("POST", `/api/apps/${id}/test`)); $("v24at_" + id).textContent = r.ok ? (r.tools ? "可用工具：" + r.tools.join("、") : r.note) : "失败：" + r.error; }
async function v24AppSecret(name) { const v = $("v24s_" + name).value; if (!v) return; await v24Do(() => api("PUT", `/api/secrets/${encodeURIComponent(name)}`, { value: v }), "已加密保存"); V24_RENDER.apps(); }

/* ------------------------------------------------------------------ 提示词 */
V24_RENDER.prompts = async function () {
  const [roles, tasks] = await Promise.all([api("GET", "/api/prompts"), api("GET", "/api/tasks?limit=40").catch(() => [])]);
  v24Body(`<div class="card v24-card"><h3>各角色的系统提示词</h3>${roles.map((r) => `<div class="v24-item"><div class="v24-grow"><b>${q24(r.title)}</b> ${r.active ? '<span class="chip live">已改</span>' : '<span class="chip">原版</span>'}
      <div class="v24-note">${q24(r.desc)} · ${r.versions.length} 个版本</div></div><button class="btn sm" onclick="v24Prompt('${r.role}')">查看 / 编辑</button></div>`).join("")}</div>
    <div class="card v24-card"><h3>某个任务实际收到了什么</h3><div class="v24-bar"><select id="v24ptask" style="flex:1">${tasks.map((t) => `<option value="${q24(t.id)}">${q24((t.prompt || "").slice(0, 50))} · ${q24(t.status)}</option>`).join("")}</select><button class="btn sm" onclick="v24Trace()">查看</button></div><div id="v24trace"></div></div>`);
};
async function v24Prompt(role) {
  const d = await api("GET", `/api/prompts/${role}`);
  const cur = d.active ? (d.versions.find((v) => v.id === d.active) || {}).text : d.original;
  v24Body(`<div class="v24-bar"><button class="btn sm" onclick="V24_RENDER.prompts()">‹ 全部角色</button><b>${q24(d.title)}</b> ${d.active ? '<span class="chip live">正在用你的版本</span>' : '<span class="chip">正在用原版</span>'}</div>
    <div class="card v24-card"><div class="v24-note">${q24(d.desc)}</div><textarea id="v24ptext" class="v24-big">${q24(cur)}</textarea>
      <div class="v24-bar"><input id="v24pnote" placeholder="版本备注（可选）" style="flex:1" /><button class="btn sm primary" onclick="v24PromptSave('${role}')">保存为新版本并启用</button>
      <button class="btn sm" onclick="v24PromptRestore('${role}')">一键恢复原版</button></div></div>
    <div class="card v24-card"><h3>版本</h3><table><tr><th>时间</th><th>备注</th><th>字数</th><th></th></tr>
      <tr><td>原版</td><td class="v24-note">代码自带</td><td>${d.original.length}</td><td>${d.active ? `<button class="btn sm" onclick="v24PromptRestore('${role}')">启用</button>` : "✔ 使用中"}</td></tr>
      ${d.versions.slice().reverse().map((v) => `<tr><td>${t24(v.ts)}</td><td>${q24(v.note)}</td><td>${(v.text || "").length}</td><td>${v.id === d.active ? "✔ 使用中" : `<button class="btn sm" onclick="v24PromptUse('${role}','${v.id}')">启用</button>`}</td></tr>`).join("")}</table></div>`);
}
async function v24PromptSave(role) { await v24Do(() => api("POST", `/api/prompts/${role}`, { text: $("v24ptext").value, note: $("v24pnote").value }), "已保存并启用"); v24Prompt(role); }
async function v24PromptRestore(role) { await v24Do(() => api("POST", `/api/prompts/${role}/restore`), "已恢复原版"); v24Prompt(role); }
async function v24PromptUse(role, id) { await v24Do(() => api("POST", `/api/prompts/${role}/activate`, { version_id: id }), "已切换"); v24Prompt(role); }
async function v24Trace() {
  const id = $("v24ptask").value; if (!id) return;
  const t = await api("GET", `/api/prompts/trace/${id}`);
  $("v24trace").innerHTML = t.runs.map((r) => `<div class="v24-note">运行 ${q24(r.task_id)}</div>` + (Object.entries(r.roles).map(([role, x]) =>
    `<details><summary><b>${q24(role)}</b> · ${q24(x.model || "")} · ${x.tools.length} 个工具 ${x.overridden ? '<span class="chip live">你的版本</span>' : ""}</summary>
      <div class="v24-note">工具：${q24(x.tools.join("、"))}</div><pre>${q24(x.system)}</pre></details>`).join("") || '<div class="v24-note">这次运行没有记录（v3.9.24 之前的任务没有）</div>')).join("");
}

/* ------------------------------------------------------------------ 上下文 */
V24_RENDER.context = async function () {
  const c = await api("GET", "/api/model_context");
  const rows = Object.entries(c.models || {});
  if (!rows.length) rows.push(["", {}]);
  const tr = ([n, x], i) => `<tr><td><input name="n${i}" value="${q24(n)}" placeholder="模型名或前缀；* = 默认" /></td><td><input name="w${i}" value="${q24(x.window || "")}" placeholder="128000" /></td>
    <td><input name="r${i}" value="${q24(x.trigger_ratio || "")}" placeholder="0.7" /></td><td><input name="t${i}" value="${q24(x.trigger_tokens || "")}" placeholder="可空" /></td><td><input name="k${i}" value="${q24(x.keep_last || "")}" placeholder="8" /></td></tr>`;
  V24.ctxRows = rows.length;
  v24Body(`${c.off ? '<div class="v24-warn">AIW_CONTEXT_MANAGE=0：已关闭</div>' : ""}<form onsubmit="v24SaveCtx(event)"><div class="card v24-card">
    <div class="v24-note">每一步发给模型的历史超过阈值时，较早的步骤被压成一行一条，最近几步保持原文；没有设置的模型完全照旧。阈值 = 窗口 × 比例，或直接填触发 token 数（优先）。token 数为估算。</div>
    <table id="v24ctx"><tr><th>模型</th><th>上下文窗口</th><th>触发比例</th><th>触发 token</th><th>保留最近步数</th></tr>${rows.map(tr).join("")}</table>
    <div class="v24-bar"><button type="button" class="btn sm" onclick="v24CtxRow()">加一行</button><button class="btn sm primary">保存</button></div></div></form>`);
};
function v24CtxRow() { const i = V24.ctxRows++; $("v24ctx").insertAdjacentHTML("beforeend", `<tr><td><input name="n${i}" placeholder="模型名" /></td><td><input name="w${i}" /></td><td><input name="r${i}" /></td><td><input name="t${i}" /></td><td><input name="k${i}" /></td></tr>`); }
async function v24SaveCtx(e) {
  e.preventDefault(); const f = e.target; const models = {};
  for (let i = 0; i < V24.ctxRows; i++) { const n = f["n" + i] && f["n" + i].value.trim(); if (!n) continue;
    models[n] = { window: +f["w" + i].value || null, trigger_ratio: +f["r" + i].value || null, trigger_tokens: +f["t" + i].value || null, keep_last: +f["k" + i].value || null }; }
  await v24Do(() => api("PUT", "/api/model_context", { models }), "已保存");
}

/* ------------------------------------------------------------------ 教学录制 */
V24_RENDER.teach = async function () {
  const [t, ms] = await Promise.all([api("GET", "/api/teach"), api("GET", "/api/machines").catch(() => [])]);
  const live = Object.values(t.active || {})[0];
  v24Body(`<div class="card v24-card"><h3>录一次演示</h3><div class="v24-note">开始后去「电脑」页点「接管桌面」，像平时一样操作一遍（点哪里、输入什么）。录下每次点击/输入和截图；边做边写备注更好。<b>不要在录制中输入密码</b>。</div>
      ${live ? `<div class="v24-bar"><span class="chip live">录制中</span><input id="v24tn" placeholder="备注：这一步在做什么" style="flex:1" /><button class="btn sm" onclick="v24TeachNote('${live}')">加备注</button><button class="btn sm primary" onclick="v24TeachStop('${live}')">停止录制</button></div>`
        : `<div class="v24-bar"><select id="v24tm">${ms.map((m) => `<option value="${q24(m.id)}">${q24(m.name || m.id)}</option>`).join("")}</select><input id="v24tt" placeholder="演示标题，如 某论坛签到" style="flex:1" /><button class="btn sm primary" onclick="v24TeachStart()">开始录制</button></div>`}</div>
    <div class="card v24-card"><h3>录制记录</h3>${t.recordings.map((r) => `<div class="v24-item"><div class="v24-grow"><b>${q24(r.title)}</b> <span class="v24-note">${t24(r.started)} · ${r.events} 个操作 · ${r.shots} 张截图${r.saved_as ? " · 已存为 " + q24(r.saved_as) : ""}</span></div>
      <button class="btn sm" onclick="v24TeachOpen('${r.id}')">打开</button><button class="btn sm ghost" style="color:var(--bad)" onclick="v24TeachDel('${r.id}')">删除</button></div>`).join("") || '<div class="v24-note">还没有录制</div>'}</div>`);
};
async function v24TeachStart() { await v24Do(() => api("POST", "/api/teach/start", { machine_id: $("v24tm").value, title: $("v24tt").value }), "开始录制"); V24_RENDER.teach(); }
async function v24TeachStop(id) { await v24Do(() => api("POST", `/api/teach/${id}/stop`), "已停止"); v24TeachOpen(id); }
async function v24TeachNote(id) { const v = $("v24tn").value.trim(); if (!v) return; await v24Do(() => api("POST", `/api/teach/${id}/note`, { text: v }), "已记下"); $("v24tn").value = ""; }
async function v24TeachDel(id) { if (!confirm("删除这次录制和截图？")) return; await v24Do(() => api("DELETE", `/api/teach/${id}`)); V24_RENDER.teach(); }
async function v24TeachOpen(id) {
  const d = await api("GET", `/api/teach/${id}`);
  const shot = (n) => `/api/teach/${id}/shot/${n}${TOKEN ? "?token=" + encodeURIComponent(TOKEN) : ""}`;
  v24Body(`<div class="v24-bar"><button class="btn sm" onclick="V24_RENDER.teach()">‹ 全部录制</button><b>${q24(d.title)}</b><span class="v24-note">${d.status === "recording" ? "录制中" : "已停止"}</span></div>
    <div class="card v24-card"><h3>截图</h3><div class="v24-shots">${d.shots.map((s) => `<a href="${shot(s.n)}" target="_blank"><img src="${shot(s.n)}" loading="lazy" title="#${s.n} ${q24(s.why)}" /></a>`).join("") || '<span class="v24-note">没有截图</span>'}</div></div>
    <div class="card v24-card"><h3>操作记录</h3><pre>${q24(d.timeline || "（空）")}</pre></div>
    <div class="card v24-card"><h3>生成技能</h3><label class="v24-sw"><input type="checkbox" id="v24tmask" />把输入的文字打码后再交给模型</label>
      <div class="v24-bar"><button class="btn sm" onclick="v24TeachDraft('${id}')">让模型写 SKILL.md</button><span class="v24-note" id="v24tdn"></span></div>
      <textarea id="v24tdraft" class="v24-big" placeholder="生成后在这里检查、修改">${q24(d.draft || "")}</textarea>
      <div class="v24-bar"><input id="v24tname" placeholder="技能名（小写英文-连接）" value="${q24(d.saved_as || "")}" /><label class="v24-sw"><input type="checkbox" id="v24tvm" />同时写到这台电脑的 ~/.claude/skills</label><button class="btn sm primary" onclick="v24TeachSave('${id}')">保存为技能</button></div></div>`);
}
async function v24TeachDraft(id) { $("v24tdn").textContent = "生成中…（约半分钟）"; try { const r = await api("POST", `/api/teach/${id}/draft`, { mask_text: $("v24tmask").checked }); $("v24tdraft").value = r.draft; $("v24tdn").textContent = "已生成，检查后保存"; } catch (e) { $("v24tdn").textContent = e.message; } }
async function v24TeachSave(id) { const r = await v24Do(() => api("POST", `/api/teach/${id}/save`, { name: $("v24tname").value, text: $("v24tdraft").value, vm: $("v24tvm").checked })); if (r) toast(`已保存为应用 ${r.name}${r.vm === false ? "（写入电脑失败）" : r.vm ? "，并写入电脑" : ""}`); }

/* ------------------------------------------------------------------ 远程访问 */
V24_RENDER.remote = async function () {
  const r = await api("GET", "/api/remote");
  const tn = r.tunnel;
  v24Body(`${r.auth_ok ? "" : '<div class="v24-warn">没有设置 AIW_API_TOKEN：任何能访问的人都能控制你的电脑。开放到局域网以外之前必须先设置令牌（内置隧道会拒绝启动）。</div>'}
    <form onsubmit="v24SaveRemote(event)">
    <div class="card v24-card"><h3>局域网</h3><div class="v24-note">本机地址：${r.lan_ips.map((i) => `<span class="v24-mono">http://${q24(i)}:${r.port}</span>`).join("、") || "-"}${r.in_docker ? "（在 Docker 里看到的是容器地址，填下面的真实地址或设 AIW_LAN_IP）" : ""}</div>
      <label class="v24-f">局域网地址<input name="lan_url" value="${q24(r.lan_url)}" placeholder="http://192.168.50.10:${r.port}" /></label></div>
    <div class="card v24-card"><h3>Tailscale</h3><div class="v24-note">${r.tailscale.cli ? (r.tailscale.ip ? `检测到 Tailscale IP <span class="v24-mono">${q24(r.tailscale.ip)}</span>` : "有 tailscale 命令但没拿到 IP") : "容器里没有 tailscale 命令：装在宿主机上即可，把地址填在这里方便手机复制"}</div>
      <label class="v24-f">Tailscale 地址<input name="tailscale_url" value="${q24(r.tailscale_url)}" placeholder="http://100.x.y.z:${r.port} 或 https://aiw.tailnet.ts.net" /></label></div>
    <div class="card v24-card"><h3>自己的 HTTPS 反向代理</h3><label class="v24-f">地址<input name="custom_url" value="${q24(r.custom_url)}" placeholder="https://aiw.example.com" /></label>
      <div class="v24-note">Caddy / Nginx / Lucky / frp 等指向 ${r.port} 端口；需要转发 WebSocket（/ws/*）。</div></div>
    <div class="v24-bar"><button class="btn sm primary">保存地址</button></div></form>
    <div class="card v24-card"><h3>内置隧道（免账号）</h3>
      ${r.tunnel_allowed ? `<div class="v24-bar">${tn.running ? '<span class="chip ok">运行中</span>' : '<span class="chip">未运行</span>'}
        ${tn.url ? `<span class="v24-mono">${q24(tn.url)}</span><button class="btn sm" onclick="navigator.clipboard.writeText('${q24(tn.url)}').then(()=>toast('已复制'))">复制</button>` : ""}
        ${tn.binary ? "" : '<button class="btn sm" onclick="v24TunnelInstall()">安装 cloudflared</button>'}
        ${tn.running ? '<button class="btn sm" onclick="v24Tunnel(\'stop\')">关闭</button>' : '<button class="btn sm primary" onclick="v24Tunnel(\'start\')">开启</button>'}
        <label class="v24-sw"><input type="checkbox" ${r.tunnel_autostart ? "checked" : ""} onchange="api('PUT','/api/remote',{tunnel_autostart:this.checked}).then(()=>toast('已保存'))" />随服务启动</label></div>
        ${tn.error ? `<div class="v24-warn">${q24(tn.error)}</div>` : ""}
        <div class="v24-note">用 Cloudflare 的 quick tunnel：不用注册、不用开端口，地址是随机的 *.trycloudflare.com，每次开启都会变；仍然需要 API Token 登录。<br>
        <b>国内注意</b>：trycloudflare 走 Cloudflare 国际网络，大陆访问经常很慢、偶尔被重置或无法打开，也没有可用性保证；长期、稳定使用请选 Tailscale 或自己的 HTTPS。下载 cloudflared 来自 GitHub，可用 AIW_CLOUDFLARED_URL 指定镜像。推送仍走 Bark。</div>`
        : '<div class="v24-note">已被 AIW_REMOTE_TUNNEL=0 关闭</div>'}</div>`);
  clearInterval(V24.timer);
  if (tn.running && !tn.url) V24.timer = setTimeout(() => V24.page === "remote" && V24_RENDER.remote(), 3000);
};
async function v24SaveRemote(e) { e.preventDefault(); const f = e.target; await v24Do(() => api("PUT", "/api/remote", { lan_url: f.lan_url.value, tailscale_url: f.tailscale_url.value, custom_url: f.custom_url.value }), "已保存"); V24_RENDER.remote(); }
async function v24Tunnel(op) { toast(op === "start" ? "正在开启，约 5-15 秒…" : "正在关闭"); await v24Do(() => api("POST", `/api/remote/tunnel/${op}`)); V24_RENDER.remote(); }
async function v24TunnelInstall() { toast("下载中（约 30 MB）…"); await v24Do(() => api("POST", "/api/remote/tunnel/install"), "已安装"); V24_RENDER.remote(); }

/* ------------------------------------------------------------------ 自动签到 */
V24_RENDER.checkin = async function () {
  const [d, ms, runs] = await Promise.all([api("GET", "/api/checkin/sites"), api("GET", "/api/machines").catch(() => []), api("GET", "/api/checkin/runs?limit=40")]);
  const st = { ok: "✅ 成功", already: "☑️ 已签过", failed: "❌ 失败", replay_failed: "⚠️ 重放失败", human: "🖐 需要你", pending: "⏳ Agent 处理中" };
  const names = Object.fromEntries(d.sites.map((s) => [s.id, s.name]));
  v24Body(`${d.env_off ? '<div class="v24-warn">AIW_CHECKIN=0：定时签到已关闭（手动「立即签到」仍可用）</div>' : ""}
    <div class="card v24-card"><div class="v24-bar"><label class="v24-sw"><input type="checkbox" ${d.settings.enabled ? "checked" : ""} onchange="api('PUT','/api/checkin/settings',{enabled:this.checked}).then(()=>toast('已保存'))" />开启自动签到</label>
      <label class="v24-sw">默认随机延后 0–<input type="number" min="0" max="180" value="${d.settings.jitter_min}" style="width:70px" onchange="api('PUT','/api/checkin/settings',{jitter_min:+this.value}).then(()=>toast('已保存'))" /> 分钟</label></div>
      <div class="v24-note">第一次由 Agent 在电脑上已登录的浏览器里签到并录下「配方」；之后每天按配方自动重放，不用模型。重放失败就交给 Agent；遇到验证码/滑块会请你接管，绝不绕过。每批签到只推送一条 Bark 汇总。</div></div>
    <div class="card v24-card"><h3>添加网站</h3><form onsubmit="v24CkAdd(event)"><div class="v24-g2"><label class="v24-f">名称<input name="name" placeholder="如 某论坛" /></label><label class="v24-f">签到页网址<input name="url" required placeholder="https://…" /></label>
      <label class="v24-f">时间（cron / daily 08:30 / every 12h）<input name="schedule" value="daily 08:30" /></label><label class="v24-f">随机延后（分钟）<input name="jitter_min" type="number" value="${d.settings.jitter_min}" /></label>
      <label class="v24-f">在哪台电脑<select name="machine_id"><option value="">默认</option>${ms.map((m) => `<option value="${q24(m.id)}">${q24(m.name || m.id)}</option>`).join("")}</select></label></div>
      <label class="v24-f">说明（可选，告诉 Agent 签到入口在哪）<input name="note" placeholder="如 右上角头像 → 每日签到" /></label>
      <div class="v24-bar"><button class="btn sm primary">添加</button></div></form></div>
    <div class="card v24-card"><h3>网站</h3>${d.sites.map((s) => `<div class="v24-item"><div class="v24-grow"><b>${q24(s.name)}</b> ${s.enabled ? "" : '<span class="chip">停用</span>'} ${s.has_recipe ? `<span class="chip ok">有配方 · ${q24(s.recipe.kind)}</span>` : '<span class="chip">待录制</span>'}
        <div class="v24-note v24-mono">${q24(s.url)}</div><div class="v24-note">${q24(s.schedule)} + 0–${s.jitter_min} 分 · 下次 ${t24(s.next_run)} · 上次 ${t24(s.last_run)} ${st[s.last_status] || ""} ${q24(s.last_message || "")}</div></div>
        <div class="v24-bar"><button class="btn sm primary" onclick="v24CkRun('${s.id}')">立即签到</button><button class="btn sm" onclick="v24CkRerec('${s.id}')">重新录制</button>
        <button class="btn sm" onclick="v24CkToggle('${s.id}',${s.enabled ? 0 : 1})">${s.enabled ? "停用" : "启用"}</button><button class="btn sm ghost" style="color:var(--bad)" onclick="v24CkDel('${s.id}')">删除</button></div></div>`).join("") || '<div class="v24-note">还没有网站（不预置任何站点）</div>'}</div>
    <div class="card v24-card"><h3>历史</h3><table><tr><th>时间</th><th>网站</th><th>方式</th><th>结果</th></tr>${runs.map((r) => `<tr><td>${t24(r.ts)}</td><td>${q24(names[r.site_id] || r.site_id)}</td><td>${q24({ replay: "重放", agent_record: "Agent 录制", agent_escalate: "Agent 接手" }[r.mode] || r.mode)}</td><td>${st[r.status] || q24(r.status)} <span class="v24-note">${q24(r.message)}</span></td></tr>`).join("") || '<tr><td colspan="4" class="v24-note">没有记录</td></tr>'}</table></div>`);
};
async function v24CkAdd(e) { e.preventDefault(); const f = e.target; await v24Do(() => api("POST", "/api/checkin/sites", { name: f.name.value, url: f.url.value, schedule: f.schedule.value, jitter_min: +f.jitter_min.value, machine_id: f.machine_id.value || null, note: f.note.value }), "已添加"); V24_RENDER.checkin(); }
async function v24CkRun(id) { toast("签到中…"); const r = await v24Do(() => api("POST", `/api/checkin/sites/${id}/run`)); toast(r.pending ? "已交给 Agent，完成后推送" : "完成"); V24_RENDER.checkin(); }
async function v24CkRerec(id) { if (!confirm("删掉现有配方，下次由 Agent 重新录制？")) return; await v24Do(() => api("POST", `/api/checkin/sites/${id}/rerecord`), "下次重新录制"); V24_RENDER.checkin(); }
async function v24CkToggle(id, on) { await v24Do(() => api("PUT", `/api/checkin/sites/${id}`, { enabled: !!on })); V24_RENDER.checkin(); }
async function v24CkDel(id) { if (!confirm("删除这个网站和它的历史？")) return; await v24Do(() => api("DELETE", `/api/checkin/sites/${id}`)); V24_RENDER.checkin(); }
