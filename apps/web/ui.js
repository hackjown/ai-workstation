/* AIW UI layer (loaded after app.js): AtomCode-style theme (dark/light), current-step card, grouped settings,
   composer "+" menu (upload + skills), connectors, online update. It wraps app.js functions so the layout,
   handlers and API calls of app.js stay the same. */
"use strict";

/* ---------- chat: current step expands into a card (tool steps, file, 暂停/停止/接管) */
const _renderChat = renderChat;
renderChat = function (scroll) { _renderChat(scroll); patchChat(); };
function mmss(s) { s = Math.max(0, Math.round(s)); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
function patchChat() {
  const box = $("msgs"); if (!box) return;
  box.querySelectorAll(".work").forEach((w, wi) => {
    // timings sit in their own column, never wrap under the command
    w.querySelectorAll(".steplist .s").forEach((s) => { const em = s.querySelector("span em.ms"); if (em) s.appendChild(em); });
    // plan numbers
    const sts = [...w.querySelectorAll(".plan .st")];
    const done = sts.filter((s) => s.classList.contains("done")).length;
    const cur = w.querySelector(".plan .st.cur");
    const head = w.querySelector(".head");
    if (!cur) return;
    const live = (S.thread && S.thread.tasks || []).find((t) => LIVE.includes(t.status) || t.live);
    const evs = live ? S.evs[live.id] || [] : [];
    const st = evs.find((e) => e.type === "task.started");
    const el = st ? Date.now() / 1000 - st.ts : null;
    const status = head.querySelector(".grow").textContent;
    head.querySelector(".grow").innerHTML = `工作中 · 步骤 <b>${done + 1}</b> / ${sts.length}${el != null ? ` · 已用 ${mmss(el)}` : ""}`;
    const card = document.createElement("div"); card.className = "curcard";
    cur.parentNode.insertBefore(card, cur); card.appendChild(cur);
    // the step's own running time = since the last plan_update
    const pu = [...evs].reverse().find((e) => e.type === "agent.plan_update");
    cur.insertAdjacentHTML("beforeend", `${pu ? `<span class="now">${mmss(Date.now() / 1000 - pu.ts)}</span>` : ""}<span class="state">${esc(status)}</span>`);
    const det = w.querySelector("details.steps"); if (det) card.appendChild(det);
    w.querySelectorAll(".filecard").forEach((f) => {
      const ext = (f.querySelector("b").textContent.split(".").pop() || "").slice(0, 4).toUpperCase();
      f.querySelector(".iconbox").textContent = ext;
      const sm = f.querySelector(".small"); sm.textContent = sm.textContent.replace(" · 点击下载", " · 刚刚生成");
      f.insertAdjacentHTML("beforeend", `<span class="dl">${icon("up").replace('class="i ', 'style="transform:rotate(180deg)" class="i ')}</span>`);
      card.appendChild(f);
    });
    const pause = head.querySelector(".btn"); const foot = w.querySelector(".foot");
    const stop = foot && [...foot.querySelectorAll(".btn")].find((b) => b.textContent.includes("停止"));
    const see = foot && [...foot.querySelectorAll(".btn")].find((b) => b.textContent.includes("看桌面"));
    const ctl = document.createElement("div"); ctl.className = "ctl";
    if (pause) { pause.className = "btn sm"; ctl.appendChild(pause); }
    if (stop) { stop.className = "btn sm danger"; ctl.appendChild(stop); }
    ctl.insertAdjacentHTML("beforeend", `<button class="btn sm primary" onclick="toggleTakeover()">${icon("hand")}接管</button>`);
    card.appendChild(ctl);
    if (see) see.remove();   // the live screen is already beside the chat on wide screens
  });
  // live readout on the viewfinder
  const fr = document.querySelector("#deskPane .deskframe");
  if (fr) {
    fr.querySelectorAll(".vfnow").forEach((n) => n.remove());
    const c = box.querySelector(".curcard .st.cur");
    if (c && !fr.classList.contains("empty")) {
      const last = box.querySelector(".curcard .steplist .s:last-child");
      fr.insertAdjacentHTML("beforeend", `<div class="vfnow"><small>${esc(last ? last.querySelector("b").textContent + " " + (last.querySelector(".ms") || { textContent: "" }).textContent : "")}</small>${esc(c.querySelector(".tx").textContent)}</div>`);
    }
  }
}

/* ---------- thread list: status dot per thread */
const _loadThreads = loadThreads;
loadThreads = async function () {
  await _loadThreads();
  const btns = $("threadList").querySelectorAll("button");
  (S.threads || []).forEach((t, i) => { if (btns[i] && t.last_status) btns[i].classList.add("st-" + t.last_status); });
};

/* ---------- desktop: viewfinder chip + a real empty state */
function ensureEmpty(frame) {
  if (frame.querySelector(".deskempty")) return;
  frame.insertAdjacentHTML("beforeend", `<div class="vfchip" id="vf_${frame.querySelector("iframe").id}">观看中</div>
    <div class="deskempty"><div class="ring">${icon("monitor")}</div><b class="em-t">还没有画面</b><p class="em-p"></p>
    <div class="row"><button class="btn sm primary" onclick="vmOp('start')">开机</button><button class="btn sm" onclick="go('settings');setTimeout(()=>document.getElementById('g-pc')&&document.getElementById('g-pc').scrollIntoView(),300)">检查电脑设置</button></div></div>`);
}
const _loadDesktop = loadDesktop;
loadDesktop = async function () {
  document.querySelectorAll(".deskframe").forEach(ensureEmpty);
  await _loadDesktop();
  const paused = /暂停/.test($("deskMode").textContent);
  document.querySelectorAll(".deskframe").forEach((fr) => {
    const src = fr.querySelector("iframe").getAttribute("src") || "";
    const empty = !src || src === "about:blank";
    const visible = fr.offsetParent !== null;
    fr.classList.toggle("empty", empty && visible);
    fr.querySelector(".em-t").textContent = paused ? "云电脑已自动暂停" : "电脑画面未连接";
    fr.querySelector(".em-p").textContent = paused ? "空闲 30 分钟后会自动暂停。发一个任务或点开机，它会自己醒来，大约 20 秒。"
      : "可能是电脑没开机，或 VM 地址变了。点开机试试，或到设置 › 电脑检查 IP。";
    const chip = fr.querySelector(".vfchip"); if (chip) chip.textContent = S.take ? "你在操作" : "观看中";
  });
  if (S.view === "chat") patchChat();
};

/* quieter option labels (the chips under each select already show the probe result) */
probeTag = function (m) { const p = S.probes[m]; if (!p) return ""; return "  · " + [p.tools ? "工具" : "", p.vision ? "看图" : "", { fast: "快", medium: "中速", slow: "慢" }[p.speed] || ""].filter(Boolean).join(" "); };
const HINTS = { model: "决定每一步操作，需能调用工具", vision_model: "可选，主模型看不了图时用", fallback: "主模型出错时用", subagent_model: "纯文本命令行任务",
  video_model: "可选，做视频的任务用它，失败回到主模型", image_model: "可选，如 gpt-image-1" };
MODEL_ROLES.forEach((r) => { if (HINTS[r.key]) r.hint = HINTS[r.key]; });
loadThreads();

/* ---------- settings: grouped page (模型 / 电脑 / 集成 / 通知 / 你 / 关于), same handlers */
const SECTIONS = [["g-model", "模型"], ["g-pc", "电脑"], ["g-conn", "连接器"], ["g-int", "集成"], ["g-skill", "技能"], ["g-notify", "通知"], ["g-me", "你和记忆"], ["g-upd", "更新"], ["g-about", "关于"]];
const secNo = (id) => { const i = SECTIONS.findIndex((x) => x[0] === id); return `<span class="no">// ${String(i + 1).padStart(2, "0")}</span>`; };
const pill = (ok, on, off) => `<span class="chip ${ok === true ? "ok" : ok === false ? "" : "bad"}">${ok ? on : off}</span>`;
function row(k, hint, v, t = "", cls = "") { return `<div class="rw ${cls}"><div class="k">${k}${hint ? `<small>${hint}</small>` : ""}</div><div class="v">${v}</div><div class="t">${t}</div></div>`; }
/* ---------- computers: display names (云电脑 1, 2, 3… by the order added, or the user's own), shared with iOS */
function curBadge(id) { return id === MID ? '<span class="curb">当前</span>' : ""; }
function useBtn(id) { return id === MID ? '<button type="button" class="btn sm" disabled>使用中</button>'
  : `<button type="button" class="btn sm" onclick="pickMachine('${esc(id)}');go('chat')">使用</button>`; }
function renameBtn(id, name) { return `<button type="button" class="btn sm" onclick="renameMachine('${esc(id)}', this.dataset.n)" data-n="${esc(name)}">重命名</button>`; }
function localNameRow(m) {
  return row("名称", m.custom_name ? "默认 " + esc(m.default_name) : "web 和 iOS 通用", `<div class="mach"><span class="ic">${icon("monitor")}</span><div><b>${esc(m.name)}</b>${curBadge(m.id)}<span class="mono idl">${esc(m.id)}${m.provider_vm_id ? " · VM " + esc(m.provider_vm_id) : ""}</span></div></div>`,
    useBtn(m.id) + renameBtn(m.id, m.name));
}
function cloudRowsHTML(cloudList) {
  return cloudList.slice().sort((a, b) => (a.order || 99) - (b.order || 99)).map((c) => {
    const ok = c.status === "ready", paused = c.paused;
    return `<div class="rw"><div class="k"><div class="mach"><span class="ic">${icon("monitor")}</span><div><b>${esc(c.name)}</b>${curBadge(c.id)}<span class="mono">4 核 8G</span></div></div></div>
      <div class="v"><span class="mono idl">${esc(c.remote_id || c.id)}</span><span class="small">${esc((c.log || []).slice(-1)[0] || c.error || "空闲 30 分钟自动暂停，发任务时自动唤醒")}</span></div>
      <div class="t">${c.status === "error" ? '<span class="chip bad">出错</span>' : paused ? '<span class="chip">已暂停</span>' : ok ? '<span class="chip ok">已连接</span>' : `<span class="chip">${CLOUD_ST[c.status] || esc(c.status)}</span>`}
      ${useBtn(c.id)}${renameBtn(c.id, c.name)}
      <button type="button" class="btn sm ghost" onclick="cloudSetup('${esc(c.id)}')">重装</button>
      <button type="button" class="btn sm ghost" style="color:var(--bad)" onclick="cloudRemove('${esc(c.id)}')">移除</button></div></div>`; }).join("");
}
async function renameMachine(id, cur) {
  const n = prompt("给这台电脑起个名字（留空恢复默认名，如「云电脑 1」）", cur || "");
  if (n === null) return;
  try { const m = await api("PUT", `/api/machines/${encodeURIComponent(id)}/name`, { name: n.trim() }); toast("已改名为「" + m.name + "」"); loadMachines(); if (S.view === "settings") loadSettings(); }
  catch (e) { toast(e.message); }
}
cloudRefresh = async function () {
  let list = [];
  try { list = await api("GET", "/api/cloud"); } catch (e) { return; }
  const box = $("cloudList"); if (!box) return;
  box.innerHTML = cloudRowsHTML(list);
  clearTimeout(cloudRefresh._t);
  if (list.some((c) => !["ready", "error"].includes(c.status))) cloudRefresh._t = setTimeout(cloudRefresh, 4000);
  else loadMachines();
};
loadSettings = async function () {
  const cloudList = await api("GET", "/api/cloud").catch(() => []);
  if (cloudList.some((c) => !["ready", "error"].includes(c.status))) setTimeout(cloudRefresh, 3000);
  const [prof, mems, models, notify, integ, secs, vmnet, machines] = await Promise.all([api("GET", "/api/profile"), api("GET", "/api/memories"), api("GET", "/api/settings/models"),
    api("GET", "/api/settings/notify"), api("GET", "/api/integrations"), api("GET", "/api/secrets"), api("GET", "/api/settings/vm").catch(() => ({})), api("GET", "/api/machines").catch(() => [])]);
  S.profile = prof;
  const dep = await api("GET", "/api/deploy/config").catch(() => ({ config: {} }));
  const rhosts = await api("GET", "/api/remote-hosts").catch(() => []);
  const bos = await api("GET", "/api/browseros").catch(() => ({ machines: [] }));
  const [skillList, upd, skJob] = await Promise.all([api("GET", "/api/skills").catch(() => []), api("GET", "/api/update").catch(() => null),
    api("GET", "/api/skills/install").catch(() => null)]);
  S.skillList = skillList; S.skillJob = skJob && Date.now() / 1000 - (skJob.finished || skJob.started || 0) < 1800 ? skJob : null;
  const localM = machines.find((m) => m.provider !== "instabox");
  let localUp = null;
  if (localM) { try { const m = await api("GET", `/api/machines/${localM.id}`); localUp = !!(m.agent && m.agent.ok); } catch (e) { localUp = false; } }
  const mcp = integ.mcp_servers || [];
  const roles = MODEL_ROLES.filter((r) => ["model", "vision_model", "video_model", "image_model"].includes(r.key));
  const extra = MODEL_ROLES.filter((r) => !roles.includes(r));
  const mrow = (r) => row(r.label, r.hint, `<div class="mrow"><select name="${r.key}" data-cur="${esc(models[r.key] || "")}" onchange="paintProbe(this)"></select>
      <button type="button" class="btn sm" onclick="probeModel(this)">探测</button><div class="probe"></div></div>`, "", "top");
  const cloudRows = cloudRowsHTML(cloudList);
  const presets = MCP_PRESETS.map((p) => { const on = mcp.some((m) => m.name === p.name);
    return `<div class="rw mcp"><div class="k"><div class="nm">${esc(p.title)} <span class="tag ${p.secret ? "" : "free"}">${p.secret ? "需 Key" : "免费"}</span></div></div>
      <div class="v"><span class="ds">${esc(p.desc)}</span></div>
      <div class="t">${on ? '<span class="chip ok">已连接</span>' : `<button type="button" class="btn sm" onclick="addPreset('${p.name}')">${icon("plus")}添加</button>`}</div></div>`; }).join("");
  const custom = mcp.filter((m) => !MCP_PRESETS.some((p) => p.name === m.name)).map((m) => { const i = mcp.indexOf(m);
    return `<div class="rw mcp"><div class="k"><div class="nm">${esc(m.name)}</div></div><div class="v"><span class="url">${esc(m.url)}</span><span class="small" id="mcpT${i}"></span></div>
      <div class="t"><span class="chip ok">已连接</span><button class="btn sm" onclick="testMcp(${i})">测试</button><button class="btn sm ghost" style="color:var(--bad)" onclick="delMcp(${i})">删除</button></div></div>`; }).join("");
  const keyOk = !!models.has_key;
  $("settingsPage").innerHTML = `<div class="set">
  <nav class="set-index">${SECTIONS.map(([id, t], i) =>
    `<a href="#${id}" class="${i ? "" : "on"}" onclick="event.preventDefault();document.getElementById('${id}').scrollIntoView({behavior:'smooth'});this.parentNode.querySelectorAll('a').forEach(a=>a.classList.toggle('on',a===this))">${t}</a>`).join("")}</nav>

  <form class="grp" id="g-model" onsubmit="saveModels(event)"><span id="sModel" hidden></span>
    <div class="grp-h"><h2>${secNo("g-model")}模型</h2>${pill(keyOk, "网关已连接", "未配置")}<span class="sub" id="modelListNote"></span>
      <div class="act"><button type="button" class="btn sm ghost" onclick="fetchModels(true)">刷新列表</button><button class="btn sm primary">保存</button></div></div>
    <div class="grp-d">主模型决定每一步操作，必须能调用工具；它看不了图时再配一个视觉模型。点"探测"会实测工具调用、看图和速度。</div>
    <div class="rows">
      ${row("网关地址", "OpenAI 兼容接口", `<input name="base_url" value="${esc(models.base_url || "")}" placeholder="${esc(models.env_base_url || "https://api.openai.com/v1")}" />`)}
      ${row("API Key", "加密保存在服务器", `<input name="key" type="password" placeholder="${keyOk ? "已设置，留空不修改" : "sk-…"}" autocomplete="off" />`, keyOk ? '<span class="chip ok">已设置</span>' : '<span class="chip">未设置</span>')}
    </div>
    <div class="rows">
      ${roles.map(mrow).join("")}
      <details class="more"><summary>${icon("chev")}更多角色<span class="cnt">${extra.map((r) => r.label).join("、")}</span></summary>${extra.map(mrow).join("")}</details>
      ${row("深度思考", "关掉可快很多", `<div class="segsw"><label><input type="radio" name="thinking" value="off" ${(models.thinking || "off") === "off" ? "checked" : ""}>关闭</label><label><input type="radio" name="thinking" value="on" ${models.thinking === "on" ? "checked" : ""}>模型默认</label></div>`)}
    </div>
  </form>

  <section class="grp" id="g-pc">
    <div class="grp-h"><h2>${secNo("g-pc")}电脑</h2><span class="sub">Agent 在这些电脑上操作，聊天里随时切换</span></div>
    <form class="rows" id="sVmNet" onsubmit="saveVmNet(event)">
      <div class="rw head"><div class="k">本地虚拟机${localM ? " · " + esc(localM.name) : ""}</div><div>${localUp == null ? '<span class="chip">未配置</span>' : pill(localUp, "已连接", "未连接")}</div></div>
      ${localM ? localNameRow(localM) : ""}
      ${row("IP / 主机名", "留空则用 .env 的 AIW_VM_IP", `<input name="ip" value="${esc(vmnet.ip || "")}" placeholder="${esc(vmnet.env_ip || "192.168.50.x")}" /><span class="mono">当前连接 ${esc(vmnet.agent_endpoint || "-")}</span>`, "", "top")}
      ${row("自动跟随", vmnet.pve ? "PVE 报告新 IP 时自动更新" : "未配置 PVE，不可用", "", `<input type="checkbox" name="auto" ${vmnet.auto ? "checked" : ""} ${vmnet.pve ? "" : "disabled"} aria-label="自动跟随" />`, "sw")}
      <div class="savebar"><span class="note">保存立即生效，不用重启</span>${vmnet.pve ? `<button type="button" class="btn sm" onclick="detectVmIp()">从 PVE 检测</button>` : ""}<button class="btn sm primary">保存</button></div>
    </form>
    <form class="rows" id="sCloud" onsubmit="cloudAdd(event)">
      <div class="rw head"><div class="k">InstaBox 云电脑</div><div>${!cloudList.length ? '<span class="chip">未配置</span>' : cloudList.some((c) => c.status === "ready" && !c.paused) ? `<span class="chip ok">${cloudList.length} 台已连接</span>` : cloudList.every((c) => c.paused) ? `<span class="chip">${cloudList.length} 台 · 暂停中</span>` : '<span class="chip bad">未就绪</span>'}</div></div>
      <div id="cloudList">${cloudRows || ""}</div>
      <details class="more" ${cloudList.length ? "" : "open"}><summary>${icon("chev")}添加云电脑<span class="cnt">免费 1 台 · 4 核 8G</span></summary>
        ${row("API Key", "ibk_…，加密保存", `<input name="api_key" type="password" autocomplete="off" placeholder="ibk_…" />`)}
        ${row("名称 / 机器", "自动装好 LibreCAD、中文字体和 vm-agent", `<div class="inl"><input name="name" value="instabox" placeholder="instabox" /><select name="remote_id" style="flex:1;min-width:140px"><option value="">新建一台</option></select></div>`)}
        <div class="savebar"><span class="note" id="cloudNote"></span><button type="button" class="btn sm" onclick="cloudLookup()">读取已有机器</button><button class="btn sm primary">添加</button></div>
      </details>
    </form>
    ${bosSection(bos)}
    <form class="rows" id="sDeploy" onsubmit="depSave(event)">
      <div class="rw head"><div class="k">部署服务器 · 开发好的程序装到这里</div><div>${dep.config.host ? '<span class="chip ok">已设置</span>' : '<span class="chip">未设置</span>'}</div></div>
      ${row("服务器地址", "随时可以换", `<div class="inl"><input name="host" value="${esc(dep.config.host || "")}" placeholder="192.168.50.120 或域名" /><input name="port" value="${esc(dep.config.port || 22)}" style="flex:0 0 80px;min-width:0" aria-label="端口" /></div>`)}
      ${row("用户名", "", `<input name="user" value="${esc(dep.config.user || "root")}" autocomplete="off" />`)}
      ${row("密码", "加密保存在 AIW 服务端，不会发给虚拟机", `<input name="password" type="password" autocomplete="new-password" placeholder="${dep.has_password ? "已保存，留空不修改" : "SSH 密码"}" />`, dep.has_password ? '<span class="chip ok">已保存</span>' : '<span class="chip">未设置</span>')}
      ${row("部署目录", "每个程序一个子文件夹", `<input name="dir" value="${esc(dep.config.dir || "~/apps")}" />`)}
      ${row("用 sudo", "需要 root 权限安装时打开", "", `<input type="checkbox" name="sudo" ${dep.config.sudo ? "checked" : ""} aria-label="sudo" />`, "sw")}
      <div class="savebar"><span class="note" id="depNote">每次部署前都会弹卡片请你允许</span><button type="button" class="btn sm" onclick="depTest()">测试连接</button><button class="btn sm primary">保存</button></div>
    </form>
    <form class="rows" id="sRemote" onsubmit="rhAdd(event)">
      <div class="rw head"><div class="k">运维主机 · 授权 Agent 远程执行命令</div><div>${rhosts.length ? `<span class="chip ok">${rhosts.length} 台已授权</span>` : '<span class="chip">未授权</span>'}</div></div>
      ${rhosts.map((h) => `<div class="rw"><div class="k"><div class="mach"><span class="ic">${icon("monitor")}</span><div><b>${esc(h.name)}</b><span class="mono">${esc(h.user || "root")}@${esc(h.host)}:${esc(h.port || 22)}</span></div></div></div>
        <div class="v"><div class="segsw"><label><input type="radio" name="m_${h.id}" ${h.mode !== "auto" ? "checked" : ""} onchange="rhMode('${h.id}','confirm')">每条都确认</label><label><input type="radio" name="m_${h.id}" ${h.mode === "auto" ? "checked" : ""} onchange="rhMode('${h.id}','auto')">只读免确认</label></div><span class="small" id="rhT_${h.id}">${h.has_password ? "密码已加密保存" : "未保存密码"}</span></div>
        <div class="t"><button type="button" class="btn sm" onclick="rhTest('${h.id}')">测试</button><button type="button" class="btn sm ghost" style="color:var(--bad)" onclick="rhDel('${h.id}')">移除</button></div></div>`).join("")}
      <details class="more" ${rhosts.length ? "" : "open"}><summary>${icon("chev")}添加主机<span class="cnt">PVE、LXC、NAS、服务器</span></summary>
        ${row("名称", "聊天里这样称呼它", `<input name="name" placeholder="如 PVE、LXC102" />`)}
        ${row("地址", "", `<div class="inl"><input name="host" placeholder="192.168.50.200" required /><input name="port" value="22" style="flex:0 0 80px;min-width:0" aria-label="端口" /></div>`)}
        ${row("用户名", "", `<input name="user" value="root" autocomplete="off" />`)}
        ${row("密码", "加密保存在 AIW 服务端，模型和虚拟机都看不到", `<input name="password" type="password" autocomplete="new-password" placeholder="SSH 密码" />`)}
        ${row("执行方式", "改动主机和危险命令（删除、关机、销毁虚拟机）始终要你允许", `<div class="segsw"><label><input type="radio" name="mode" value="confirm" checked>每条都确认</label><label><input type="radio" name="mode" value="auto">只读免确认</label></div>`)}
        <div class="savebar"><span class="note" id="rhNote">Agent 用 remote_exec 工具在这些主机上执行，每次执行都记入收据</span><button class="btn sm primary">授权并测试</button></div>
      </details>
    </form>
  </section>

  ${connSection(mcp)}

  <section class="grp" id="g-int"><span id="sInteg" hidden></span>
    <div class="grp-h"><h2>${secNo("g-int")}集成</h2>${mcp.length ? `<span class="chip ok">${mcp.length} 个 MCP 已连接</span>` : '<span class="chip">未配置</span>'}
      <div class="act"><button type="button" class="btn sm ghost" onclick="listTools()">查看可用工具</button></div></div>
    <div class="grp-d">让 Agent 直接调 API，而不是点网页。请求头里可以用 {{secret:名字}} 引用下方加密保存的密钥。</div>
    <div class="rows">
      <div class="rw head"><div class="k">推荐 MCP · 一键添加</div><div></div></div>
      ${presets}
    </div>
    <div class="rows">
      <div class="rw head"><div class="k">自定义 MCP 服务器</div><div></div></div>
      ${custom}
      <form onsubmit="addMcp(event)">${row("添加服务器", "Streamable HTTP", `<div class="inl"><input name="name" placeholder="名称，如 gmail" required style="flex:.6" /><input name="url" placeholder="https://…/mcp" required /></div>
        <div class="inl"><input name="hk" placeholder="请求头名（可空）" style="flex:.6" /><input name="hv" placeholder="值，如 Bearer {{secret:gmail_token}}" /></div>`, `<button class="btn sm primary">添加</button>`, "top")}</form>
      <details class="more"><summary>${icon("chev")}导入 OpenAPI 与 HTTP 工具<span class="cnt">${(integ.http_tools || []).length} 个 HTTP 工具</span></summary>
        <form onsubmit="importOpenAPI(event)">${row("OpenAPI", "", `<div class="inl"><input name="url" placeholder="openapi.json 地址" required /><input name="prefix" placeholder="前缀，如 ha" style="flex:.4" /></div><div class="inl"><input name="base_url" placeholder="API 根地址（可空）" /><input name="auth" placeholder="Authorization（可空）" /></div>`, `<button class="btn sm">导入</button>`, "top")}</form>
        ${row("HTTP 工具", "JSON", `<textarea id="httpTools" rows="5" style="font:12px/1.5 var(--mono)">${esc(JSON.stringify(integ.http_tools || [], null, 2))}</textarea>`, `<button class="btn sm" onclick="saveHttpTools()">保存</button>`, "top")}
      </details>
      <details class="more"><summary>${icon("chev")}密钥<span class="cnt">${secs.length} 个，加密保存，只显示末 4 位</span></summary>
        ${secs.map((s) => row(esc(s.name), "", `<span class="mono">${esc(s.preview)}</span>`, `<button class="btn sm ghost" style="color:var(--bad)" onclick="delSecret('${esc(s.name)}')">删除</button>`)).join("")}
        <form onsubmit="addSecret(event)">${row("新密钥", "", `<div class="inl"><input name="n" placeholder="名字，如 gmail_token" required style="flex:.6" /><input name="v" type="password" placeholder="值" required autocomplete="off" /></div>`, `<button class="btn sm primary">保存</button>`)}</form>
      </details>
      <div id="toolList" class="small" style="padding:0 16px"></div>
    </div>
  </section>

  ${skillSection(skillList)}

  <form class="grp" id="g-notify" onsubmit="saveNotify(event)"><span id="sNotify" hidden></span>
    <div class="grp-h"><h2>${secNo("g-notify")}通知</h2>${pill(!!(notify.bark_url || notify.ntfy_url || notify.webhook_url), "已配置", "未配置")}
      <div class="act"><button type="button" class="btn sm" onclick="api('POST','/api/settings/notify/test').then(()=>toast('已发送测试通知'))">发送测试</button><button class="btn sm primary">保存</button></div></div>
    <div class="rows">
      ${row("Bark", "iPhone 推荐", `<input name="bark_url" value="${esc(notify.bark_url || "")}" placeholder="https://api.day.app/你的key" />`)}
      ${row("ntfy 主题", "", `<input name="ntfy_url" value="${esc(notify.ntfy_url || "")}" placeholder="https://ntfy.sh/aiw-xxxx" />`)}
      ${row("Webhook", "POST {title, body}", `<input name="webhook_url" value="${esc(notify.webhook_url || "")}" placeholder="https://…" />`)}
      ${row("浏览器通知", "这台设备", "", `<button type="button" class="btn sm" onclick="Notification.requestPermission().then(p=>toast('浏览器通知：'+p))">允许</button>`)}
    </div>
  </form>

  <section class="grp" id="g-me"><span id="sMem" hidden></span>
    <div class="grp-h"><h2>${secNo("g-me")}你和记忆</h2></div>
    <form class="rows" onsubmit="saveProfile(event)">
      ${row("你的称呼", "", `<input name="name" value="${esc(prof.name || "")}" placeholder="例如 Jown" />`)}
      ${row("Agent 名字", "", `<input name="agent_name" value="${esc(prof.agent_name || "")}" placeholder="例如 小A" />`)}
      ${row("语气", "", `<input name="tone" value="${esc(prof.tone || "")}" placeholder="例如 简洁、用中文" />`)}
      ${row("关于你", "工作、常用网站、文件放哪", `<textarea name="about" rows="2">${esc(prof.about || "")}</textarea>`, "", "top")}
      ${row("完成时朗读结果", "", "", `<input type="checkbox" ${localStorage.getItem("aiw_tts") === "1" ? "checked" : ""} onchange="localStorage.setItem('aiw_tts', this.checked?'1':'')" aria-label="朗读" />`, "sw")}
      <input type="hidden" name="avatar" value="${esc(prof.avatar || "amber")}" />
      <div class="savebar"><button class="btn sm primary">保存</button></div>
    </form>
    <div class="rows">
      ${row("记忆", "聊天和每个任务都会参考；在聊天里说「记住…」「忘掉…」也行", `<span class="small">${mems.length} 条</span>`, `<button type="button" class="btn sm" onclick="go('memory')">管理记忆 ›</button>`)}
    </div>
  </section>

  ${updSection(upd)}

  <section class="grp" id="g-about">
    <div class="grp-h"><h2>${secNo("g-about")}关于</h2></div>
    <div class="rows">
      ${row("外观", "深色 / 浅色，默认跟随系统", themeSeg())}
      ${row("服务器", "", `<span class="mono">${esc(location.host)}</span>`, '<span class="chip ok">已连接</span>')}
      ${row("API Token", "保存在这台设备上", "", `<button class="btn sm" onclick="localStorage.removeItem('aiw_token');location.href=location.pathname">更换 Token</button>`)}
      ${row("API 文档", "", "", `<a class="btn sm" href="/docs" target="_blank">打开</a>`)}
      ${row("例程", "按时间自动交给 Agent 的任务", "", `<button class="btn sm" onclick="go('routines')">管理</button>`)}
    </div>
  </section>
  </div>`;
  // the original code looks models up under #sModel; keep that id on the form itself
  const g = $("g-model"); $("sModel").remove(); g.id = "sModel"; g.dataset.anchor = "g-model";
  const idx = document.querySelector('.set-index a[href="#g-model"]'); if (idx) idx.setAttribute("onclick", idx.getAttribute("onclick").replace("'g-model'", "'sModel'"));
  paintIcons($("settingsPage"));
  applyProfile();
  fetchModels(false);
  connFilter(S.connCat || "全部");
  updResume();
  checkSkillMachines();
  if (S.skillJob && !S.skillJob.done) pollSkillJob();
};

/* ======================================================= theme: 跟随系统 / 深色 / 浅色 */
Object.assign(IC, {
  plus: "M12 5v14M5 12h14", sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z", spark: "M12 3l1.9 5.8L20 10.7l-5 3.9 1.7 6.4L12 17.3 7.3 21l1.7-6.4-5-3.9 6.1-1.9z",
  film: "M4 4h16v16H4zM8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4", doc: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h6",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3", image: "M3 5h18v14H3zM3 16l5-5 4 4 3-3 6 6M15.5 9.5h.01",
  ruler: "M3 17 17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2", refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
  code: "M8 6l-6 6 6 6M16 6l6 6-6 6M14 4l-4 16", link: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
});
function themeMode() { return localStorage.getItem("aiw_theme") || "system"; }
function applyTheme() {
  const m = themeMode();
  const t = m === "system" ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : m;
  document.documentElement.dataset.theme = t;
  const meta = document.querySelector('meta[name="theme-color"]'); if (meta) meta.content = t === "light" ? "#f6f3ec" : "#0a0a0a";
  const b = $("themeBtn"); if (b) { b.innerHTML = icon(t === "light" ? "moon" : "sun"); b.title = t === "light" ? "切到深色" : "切到浅色"; }
  if (NATIVE) try { NATIVE.postMessage({ op: "theme", theme: t }); } catch (e) {}
}
function setTheme(m) { localStorage.setItem("aiw_theme", m); applyTheme(); document.querySelectorAll(".themeseg input").forEach((i) => { i.checked = i.value === m; }); }
function themeSeg() {
  const m = themeMode();
  return `<div class="segsw themeseg">${[["system", "跟随系统"], ["dark", "深色"], ["light", "浅色"]].map(([v, t]) =>
    `<label><input type="radio" name="theme_ui" value="${v}" ${m === v ? "checked" : ""} onchange="setTheme('${v}')">${t}</label>`).join("")}</div>`;
}
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (themeMode() === "system") applyTheme(); });
(function addThemeButton() {
  const bell = $("bell"); if (!bell || $("themeBtn")) return;
  bell.insertAdjacentHTML("beforebegin", `<button class="icon" id="themeBtn" aria-label="切换深浅色" onclick="setTheme(document.documentElement.dataset.theme==='light'?'dark':'light')"></button>`);
  applyTheme();
})();

/* ======================================================= composer "+": upload a file or pick installed skills */
S.skills = [];
async function loadSkillList() { try { S.skillList = await api("GET", "/api/skills"); } catch (e) { S.skillList = S.skillList || []; } return S.skillList; }
(function setupPlus() {
  const clip = document.querySelector('#v-chat .composer button[aria-label="附件"]'); if (!clip) return;
  clip.innerHTML = icon("plus"); clip.setAttribute("aria-label", "添加"); clip.title = "上传文件或选择技能";
  clip.setAttribute("onclick", "togglePlus(event)");
  clip.insertAdjacentHTML("afterend", '<div class="plusmenu" id="plusMenu" role="menu"></div>');
  $("attach").insertAdjacentHTML("afterend", '<div class="skillchips" id="skillChips"></div>');
  document.addEventListener("click", (e) => { const m = $("plusMenu"); if (m && m.classList.contains("open") && !e.target.closest("#plusMenu") && !e.target.closest('#v-chat .composer button[aria-label="添加"]')) m.classList.remove("open"); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const m = $("plusMenu"); if (m) m.classList.remove("open"); } });
})();
async function togglePlus(e) {
  if (e) e.preventDefault();
  const m = $("plusMenu"); if (m.classList.contains("open")) { m.classList.remove("open"); return; }
  m.innerHTML = `<button role="menuitem" onclick="$('plusMenu').classList.remove('open');$('file').click()"><span class="pi">${icon("clip")}</span><b>上传文件</b><span class="ck"></span><small>图片、PDF、Office 文档，会放到电脑上</small></button>
    <div class="sep"></div><div class="pm-h">技能</div><div id="pmSkills"><div class="small" style="padding:6px 10px">加载中…</div></div>`;
  m.classList.add("open");
  const list = await loadSkillList();
  $("pmSkills").innerHTML = list.map((s) => `<button role="menuitemcheckbox" class="${S.skills.includes(s.id) ? "on" : ""}" onclick="pickSkill('${esc(s.id)}')">
      <span class="pi">${icon(IC[s.icon] ? s.icon : "spark")}</span><b>${esc(s.title)}</b><span class="ck">${icon("check")}</span><small>${esc(s.desc || s.path || "")}</small></button>`).join("")
    + `<button onclick="$('plusMenu').classList.remove('open');go('settings');setTimeout(()=>{const g=document.getElementById('g-skill');g&&g.scrollIntoView()},400)"><span class="pi">${icon("settings")}</span><b>管理技能</b><span class="ck"></span><small>添加你自己装在电脑上的技能</small></button>`;
}
function pickSkill(id) {
  S.skills = S.skills.includes(id) ? S.skills.filter((x) => x !== id) : S.skills.concat(id);
  document.querySelectorAll("#pmSkills button").forEach((b) => { const on = b.getAttribute("onclick") === `pickSkill('${id}')`; if (on) b.classList.toggle("on", S.skills.includes(id)); });
  renderSkillChips(); $("prompt").focus();
}
function renderSkillChips() {
  const name = (id) => ((S.skillList || []).find((s) => s.id === id) || { title: id }).title;
  $("skillChips").innerHTML = S.skills.map((id) => `<span class="skillchip">${icon("spark")}${esc(name(id))}<button onclick="pickSkill('${esc(id)}')" aria-label="移除">${icon("x")}</button></span>`).join("");
}
const _submitPrompt = submitPrompt;
submitPrompt = async function (e) {
  const p = $("prompt");
  if (S.skills.length && p.value.trim() && !/^【技能[:：]/.test(p.value)) {
    p.value = `【技能：${S.skills.join("、")}】\n` + p.value;
    S.skills = []; renderSkillChips();
  }
  return _submitPrompt(e);
};
function skillBadges(root) {
  root.querySelectorAll(".u .bubble").forEach((b) => {
    if (b.dataset.sk) return; b.dataset.sk = "1";
    b.innerHTML = b.innerHTML.replace(/【技能[:：]([^】]+)】\n?/g, (_, names) => names.split(/[、,，\s]+/).filter(Boolean).map((n) => `<span class="sk">⚡ ${n}</span>`).join("") + "<br>");
  });
}
const _renderChat2 = renderChat;
renderChat = function (scroll) { _renderChat2(scroll); const b = $("msgs"); if (b) skillBadges(b); };

/* settings › 技能: built-in list + your own; 安装 runs `npx skills add` on every computer */
const SK_ST = { waiting: "排队", waking: "唤醒中", installing: "安装中", ok: "已装好", failed: "失败" };
function skillSection(list) {
  const job = S.skillJob;
  return `<section class="grp" id="g-skill">
    <div class="grp-h"><h2>${secNo("g-skill")}技能</h2><span class="chip ok">${(list || []).length} 个可用</span></div>
    <div class="grp-d">在对话框点「+」选技能，Agent 会先读电脑上这个技能的 SKILL.md 再动手。新技能填一行就会装到所有电脑的 ~/.claude/skills/。</div>
    <div class="rows">
      <form onsubmit="installSkill(event)">${row("安装技能", "装到所有电脑", `<div class="inl"><input name="src" placeholder="npx skills add openfilm/openfilm，或 owner/repo、GitHub 链接" required ${job && !job.done ? "disabled" : ""} /></div>`,
        `<button class="btn sm primary" ${job && !job.done ? "disabled" : ""}>${job && !job.done ? "安装中…" : "安装"}</button>`, "top")}</form>
      <div id="skJob">${skillJobHtml(job)}</div>
      ${(list || []).map((s) => `<div class="rw"><div class="k">${esc(s.title)}<small>${esc(s.id)}</small></div><div class="v"><span class="small">${esc(s.desc || "")}</span>${s.path ? `<span class="mono">${esc(s.path)}</span>` : ""}${s.builtin ? "" : `<span class="skm" data-sk="${esc(s.id)}"><span class="small">正在检查各电脑…</span></span>`}</div>
        <div class="t">${s.builtin ? '<span class="tag">内置</span>' : `${s.source ? `<button class="btn sm ghost" onclick="syncSkill('${esc(s.source)}')" title="${esc(s.source)}">同步</button>` : ""}<button class="btn sm ghost" style="color:var(--bad)" onclick="delSkill('${esc(s.id)}')">删除</button>`}</div></div>`).join("")}
      <details class="skmanual"><summary class="small">已经手动装好了？只登记文件夹</summary>
      <form onsubmit="addSkill(event)">${row("登记技能", "默认 ~/.claude/skills/名字", `<div class="inl"><input name="id" placeholder="名字，如 pdf-tools" required style="flex:.6" /><input name="path" placeholder="留空 = ~/.claude/skills/名字" /></div><input name="desc" placeholder="一句话说明它能做什么（可选）" />`, `<button class="btn sm">登记</button>`, "top")}</form></details>
    </div></section>`;
}
function skillJobHtml(job) {
  if (!job || !job.machines) return "";
  const ms = Object.entries(job.machines);
  if (job.done && ms.every(([, m]) => m.state === "ok")) return "";  // the skill's own row already shows each computer
  return `<div class="skjob ${job.done ? "done" : ""}"><div class="small">${job.done ? "安装完成" : "正在安装"} · <span class="mono">${esc(job.source)}</span></div>
    ${ms.map(([id, m]) => `<div class="skm-row"><b>${esc(m.name)}</b><span class="chip ${m.state === "ok" ? "ok" : m.state === "failed" ? "bad" : ""}">${SK_ST[m.state] || m.state}</span>
      <span class="small">${m.state === "ok" ? esc((m.skills || []).join("、")) : esc(m.error || "")}</span>
      ${m.log ? `<details><summary class="small">日志</summary><pre class="mono">${esc(m.log)}</pre></details>` : ""}</div>`).join("")}</div>`;
}
async function installSkill(e) {
  e.preventDefault();
  try { S.skillJob = await api("POST", "/api/skills/install", { source: e.target.elements.src.value.trim() }); loadSettings().then(() => { const g = $("g-skill"); g && g.scrollIntoView(); }); pollSkillJob(); }
  catch (err) { toast("安装失败：" + err.message); }
}
async function syncSkill(src) {
  try { S.skillJob = await api("POST", "/api/skills/install", { source: src }); toast("开始同步到所有电脑"); loadSettings(); pollSkillJob(); } catch (err) { toast(err.message); }
}
let skPoll = null;
function pollSkillJob() {
  clearTimeout(skPoll);
  skPoll = setTimeout(async () => {
    try { S.skillJob = await api("GET", "/api/skills/install"); } catch (e) { return; }
    const box = $("skJob"); if (box) box.innerHTML = skillJobHtml(S.skillJob);
    if (S.skillJob && !S.skillJob.done) pollSkillJob(); else if (S.view === "settings") loadSettings();
  }, 2000);
}
async function checkSkillMachines() {
  if (!document.querySelector(".skm[data-sk]")) return;
  let r = {}; try { r = await api("GET", "/api/skills/check"); } catch (e) { return; }
  document.querySelectorAll(".skm[data-sk]").forEach((el) => {
    const id = el.dataset.sk;
    el.innerHTML = Object.values(r).map((m) => m.offline ? `<span class="chip">${esc(m.name)} · 未开机</span>`
      : (m.has || []).includes(id) ? `<span class="chip ok">${esc(m.name)} ✓</span>` : `<span class="chip bad">${esc(m.name)} 未装</span>`).join("") || "";
  });
}
async function addSkill(e) {
  e.preventDefault(); const f = e.target.elements;
  const mine = (S.skillList || []).filter((s) => !s.builtin);
  const id = f.id.value.trim();
  mine.push({ id, title: id, path: f.path.value.trim() || `~/.claude/skills/${id}`, desc: f.desc.value.trim() });
  try { S.skillList = await api("PUT", "/api/skills/custom", mine); toast("已登记"); loadSettings(); } catch (err) { toast("登记失败：" + err.message); }
}
async function delSkill(id) {
  if (!confirm("从列表删除 " + id + "？（电脑上的文件夹不会删）")) return;
  const mine = (S.skillList || []).filter((s) => !s.builtin && s.id !== id);
  S.skillList = await api("PUT", "/api/skills/custom", mine); loadSettings();
}

/* ======================================================= settings › 连接器 */
// kind "token": an MCP server that takes a key/token. kind "url": OAuth apps — paste the personal MCP address
// you get from Zapier / Composio / Pipedream after signing in there (it carries your login).
const SI = (slug) => `https://cdn.jsdelivr.net/npm/simple-icons@13/icons/${slug}.svg`;
const CONNECTORS = [
  { id: "gmail", name: "Gmail", cat: "办公", logo: "gmail", kind: "url", desc: "读邮件、搜邮件、起草和发送" },
  { id: "gcal", name: "Google 日历", cat: "办公", logo: "googlecalendar", kind: "url", desc: "查日程、建会议、找空闲时间" },
  { id: "gdrive", name: "Google 云端硬盘", cat: "文件", logo: "googledrive", kind: "url", desc: "搜索、读取、上传文件和表格" },
  { id: "github", name: "GitHub", cat: "开发", logo: "github", kind: "token", url: "https://api.githubcopilot.com/mcp/", header: "Bearer", keyName: "个人访问令牌 ghp_… / github_pat_…", keyUrl: "https://github.com/settings/personal-access-tokens", desc: "仓库、Issue、PR、代码搜索" },
  { id: "notion", name: "Notion", cat: "办公", logo: "notion", kind: "url", desc: "读写页面和数据库" },
  { id: "slack", name: "Slack", cat: "沟通", logo: "slack", kind: "url", desc: "读频道、发消息、搜聊天记录" },
  { id: "outlook", name: "Outlook", cat: "办公", logo: "", letter: "O", color: "#0a64d6", kind: "url", desc: "Microsoft 365 邮件和日历" },
  { id: "onepassword", name: "1Password", cat: "安全", logo: "1password", kind: "url", desc: "按需读取登录信息（通过 Zapier / Composio）" },
  { id: "asana", name: "Asana", cat: "项目", logo: "asana", kind: "url", desc: "任务、项目、评论" },
  { id: "box", name: "Box", cat: "文件", logo: "box", kind: "url", desc: "企业文件搜索和读取" },
  { id: "calendly", name: "Calendly", cat: "办公", logo: "calendly", kind: "url", desc: "预约链接和日程" },
  { id: "canva", name: "Canva", cat: "设计", logo: "canva", kind: "url", desc: "生成和导出设计" },
  { id: "dropbox", name: "Dropbox", cat: "文件", logo: "dropbox", kind: "url", desc: "文件搜索、读取、分享链接" },
  { id: "figma", name: "Figma", cat: "设计", logo: "figma", kind: "url", desc: "读取设计稿、导出切图" },
  { id: "linear", name: "Linear", cat: "项目", logo: "linear", kind: "token", url: "https://mcp.linear.app/mcp", header: "Bearer", keyName: "Linear API Key（lin_api_…）", keyUrl: "https://linear.app/settings/account/security", desc: "Issue、项目、周期" },
  { id: "jira", name: "Jira / Confluence", cat: "项目", logo: "jira", kind: "url", desc: "工单和文档（Atlassian）" },
  { id: "trello", name: "Trello", cat: "项目", logo: "trello", kind: "url", desc: "看板和卡片" },
  { id: "todoist", name: "Todoist", cat: "项目", logo: "todoist", kind: "url", desc: "待办和提醒" },
  { id: "feishu", name: "飞书", cat: "沟通", logo: "", letter: "飞", color: "#3370ff", kind: "url", desc: "消息、文档、多维表格（飞书开放平台 MCP 地址）" },
  { id: "dingtalk", name: "钉钉", cat: "沟通", logo: "", letter: "钉", color: "#1a7cff", kind: "url", desc: "消息和待办（钉钉 MCP 地址）" },
  { id: "huggingface", name: "Hugging Face", cat: "开发", logo: "huggingface", kind: "token", url: "https://huggingface.co/mcp", header: "Bearer", keyName: "Access Token（hf_…）", keyUrl: "https://huggingface.co/settings/tokens", desc: "搜模型、数据集、Spaces" },
  { id: "supabase", name: "Supabase", cat: "开发", logo: "supabase", kind: "token", url: "https://mcp.supabase.com/mcp", header: "Bearer", keyName: "Personal Access Token（sbp_…）", keyUrl: "https://supabase.com/dashboard/account/tokens", desc: "数据库查询、表结构、项目" },
  { id: "stripe", name: "Stripe", cat: "开发", logo: "stripe", kind: "token", url: "https://mcp.stripe.com", header: "Bearer", keyName: "Secret / Restricted Key（rk_… 推荐只读）", keyUrl: "https://dashboard.stripe.com/apikeys", desc: "订单、客户、退款查询" },
  { id: "firecrawl", name: "Firecrawl", cat: "开发", logo: "", letter: "F", color: "#ff6a00", kind: "token", url: "https://mcp.firecrawl.dev/{{secret:conn_firecrawl}}/v2/mcp", keyName: "Firecrawl API Key（fc-…）", keyUrl: "https://www.firecrawl.dev/app/api-keys", desc: "整站抓取、网页转 Markdown" },
  { id: "tavily", name: "Tavily", cat: "开发", logo: "", letter: "T", color: "#3b5bdb", kind: "token", url: "https://mcp.tavily.com/mcp/?tavilyApiKey={{secret:conn_tavily}}", keyName: "Tavily API Key（tvly-…）", keyUrl: "https://app.tavily.com", desc: "面向 AI 的联网搜索" },
  { id: "zapier", name: "Zapier", cat: "自动化", logo: "zapier", kind: "url", desc: "一个地址接 8000+ 应用（Gmail、日历、表格…）" },
  { id: "composio", name: "Composio", cat: "自动化", logo: "", letter: "C", color: "#222", kind: "url", desc: "一个地址接 250+ 应用，含 OAuth 登录" },
];
const CONN_CATS = ["全部", "已连接", "办公", "文件", "沟通", "项目", "设计", "开发", "安全", "自动化"];
const connLogo = (c) => `<span class="lg">${c.logo ? `<img src="${SI(c.logo)}" alt="" loading="lazy" onerror="this.outerHTML='<span>${esc(c.name[0])}</span>'">` : `<span style="color:${c.color || "#333"}">${esc(c.letter || c.name[0])}</span>`}</span>`;
function connSection(mcp) {
  const on = (c) => mcp.some((m) => m.name === c.id);
  const n = CONNECTORS.filter(on).length;
  const rows = CONNECTORS.map((c) => {
    const connected = on(c);
    const form = c.kind === "token"
      ? `<div class="inl"><input type="password" autocomplete="off" placeholder="${esc(c.keyName)}" id="ck_${c.id}" /><button type="button" class="btn sm primary" onclick="connSave('${c.id}')">测试并连接</button></div>
         <span class="small">加密保存在服务器，只用来调 ${esc(c.name)} 的官方 MCP。<a href="${esc(c.keyUrl)}" target="_blank" rel="noopener">去生成 →</a></span>`
      : `<div class="inl"><input type="password" autocomplete="off" placeholder="https://…/mcp（含你的个人密钥）" id="ck_${c.id}" /><button type="button" class="btn sm primary" onclick="connSave('${c.id}')">测试并连接</button></div>
         <span class="small">${esc(c.name)} 需要 OAuth 登录：在 <a href="https://mcp.zapier.com" target="_blank" rel="noopener">Zapier MCP</a> 或 <a href="https://mcp.composio.dev" target="_blank" rel="noopener">Composio</a> 登录并授权 ${esc(c.name)}，把生成的 MCP 地址粘到这里。地址加密保存。</span>`;
    return `<div class="conn ${connected ? "is-on" : ""}" data-cat="${c.cat}" data-name="${esc(c.name.toLowerCase())}" id="cn_${c.id}">${connLogo(c)}
      <div class="nm">${esc(c.name)}${connected ? '<span class="chip ok">已连接</span>' : ""}</div>
      <div class="t">${connected ? `<button type="button" class="btn sm" onclick="connTest('${c.id}')">测试</button><button type="button" class="btn sm ghost" style="color:var(--bad)" onclick="connRemove('${c.id}')">断开</button>`
        : `<button type="button" class="link" onclick="this.closest('.conn').classList.toggle('open');const i=document.getElementById('ck_${c.id}');i&&i.focus()">连接</button>`}</div>
      <div class="ds" id="cs_${c.id}">${esc(c.desc)}</div>
      <div class="form">${form}</div></div>`; }).join("");
  return `<section class="grp" id="g-conn">
    <div class="grp-h"><h2>${secNo("g-conn")}连接器</h2>${n ? `<span class="chip ok">${n} 个已连接</span>` : '<span class="chip">未连接</span>'}</div>
    <div class="grp-d">连上后 Agent 直接调这些应用的接口，不用在电脑上点网页。每个连接都是一个 MCP 服务器，也会出现在下面的「集成」里。</div>
    <div class="rows"><div class="connfilter">${CONN_CATS.map((c) => `<button type="button" data-c="${c}" onclick="connFilter('${c}')">${c}</button>`).join("")}<input placeholder="搜索连接器" oninput="connFilter(S.connCat||'全部', this.value)" /></div>
    <div id="connList">${rows}</div></div></section>`;
}
function connFilter(cat, q) {
  S.connCat = cat; q = (q == null ? (document.querySelector(".connfilter input") || {}).value || "" : q).trim().toLowerCase();
  document.querySelectorAll(".connfilter button").forEach((b) => b.classList.toggle("on", b.dataset.c === cat));
  document.querySelectorAll("#connList .conn").forEach((r) => {
    const ok = (cat === "全部" || (cat === "已连接" ? r.classList.contains("is-on") : r.dataset.cat === cat)) && (!q || r.dataset.name.includes(q));
    r.style.display = ok ? "" : "none";
  });
}
function connSpec(c) {
  const secret = "conn_" + c.id;
  if (c.kind === "url") return { name: c.id, url: `{{secret:${secret}}}`, headers: {} };
  if (c.url.includes("{{secret:")) return { name: c.id, url: c.url, headers: {} };
  return { name: c.id, url: c.url, headers: { Authorization: `${c.header || "Bearer"} {{secret:${secret}}}` } };
}
async function connSave(id) {
  const c = CONNECTORS.find((x) => x.id === id); const v = ($("ck_" + id).value || "").trim(); const st = $("cs_" + id);
  if (!v) { toast(c.kind === "url" ? "先粘贴 MCP 地址" : "先填 " + c.keyName); return; }
  if (c.kind === "url" && !/^https?:\/\//.test(v)) { toast("MCP 地址要以 https:// 开头"); return; }
  try {
    st.textContent = "连接测试中…";
    await api("PUT", `/api/secrets/conn_${id}`, { value: v });
    const sp = connSpec(c);
    const t = await api("POST", "/api/integrations/mcp/test", { url: sp.url, headers: sp.headers });
    if (!t.tools.length) throw new Error("没有返回任何工具，检查一下密钥或地址");
    const cur = ((await api("GET", "/api/integrations")).mcp_servers || []).filter((m) => m.name !== id);
    cur.push(sp); await api("PUT", "/api/integrations", { mcp_servers: cur });
    toast(`${c.name} 已连接 · ${t.tools.length} 个工具`); loadSettings();
  } catch (err) { st.textContent = "连接失败：" + err.message; st.style.color = "var(--bad)"; await api("DELETE", `/api/secrets/conn_${id}`).catch(() => {}); }
}
async function connTest(id) {
  const c = CONNECTORS.find((x) => x.id === id); const st = $("cs_" + id); const sp = connSpec(c);
  st.textContent = "测试中…"; st.style.color = "";
  try { const t = await api("POST", "/api/integrations/mcp/test", { url: sp.url, headers: sp.headers }); st.textContent = `可用 · ${t.tools.length} 个工具：${t.tools.slice(0, 6).join("、")}${t.tools.length > 6 ? "…" : ""}`; }
  catch (err) { st.textContent = "不可用：" + err.message; st.style.color = "var(--bad)"; }
}
async function connRemove(id) {
  const c = CONNECTORS.find((x) => x.id === id); if (!confirm(`断开 ${c.name}？`)) return;
  const cur = ((await api("GET", "/api/integrations")).mcp_servers || []).filter((m) => m.name !== id);
  await api("PUT", "/api/integrations", { mcp_servers: cur }); await api("DELETE", `/api/secrets/conn_${id}`).catch(() => {});
  toast(`已断开 ${c.name}`); loadSettings();
}
// recommended MCP presets that are now connectors don't show twice
for (let i = MCP_PRESETS.length - 1; i >= 0; i--) if (CONNECTORS.some((c) => c.id === MCP_PRESETS[i].name)) MCP_PRESETS.splice(i, 1);

/* ======================================================= settings › 在线更新 */
const short = (v) => (v || "").slice(0, 7) || "—";
const UPD_STEPS = [["connect", "连接服务器"], ["pull", "拉取新镜像"], ["recreate", "重建容器"], ["start", "等待启动"], ["verify", "校验版本"]];
function updVerdict(cur, gh) {
  const img = (gh && gh.image || {}).sha, head = (gh && gh.head || {}).sha;
  if (!img) return gh && gh.error ? '<span class="chip">GitHub 连不上</span>' : '<span class="chip">未知</span>';
  if (cur && cur === img) return head && head !== img ? '<span class="chip live">已是最新镜像 · 新提交构建中</span>' : '<span class="chip ok">与 GitHub 一致</span>';
  return '<span class="chip live">有新版本</span>';
}
function updSection(u) {
  u = u || { config: {}, github: {} };
  const c = u.config || {}, gh = u.github || {};
  const img = gh.image || {}, head = gh.head || {};
  return `<form class="grp" id="g-upd" onsubmit="updSave(event)">
    <div class="grp-h"><h2>${secNo("g-upd")}在线更新</h2><span id="updVerdict">${updVerdict(u.current, gh)}</span>
      <div class="act"><button type="button" class="btn sm ghost" onclick="updRefresh()">${icon("refresh")}检查</button><button type="button" class="btn sm primary" id="updGo" onclick="updStart()">在线更新</button></div></div>
    <div class="grp-d">通过 SSH 登录 Docker 主机，在安装目录执行 <span class="mono">docker compose pull && docker compose up -d --force-recreate</span>，再用 <span class="mono">docker exec ${esc(c.container || "aiw-api")} printenv AIW_VERSION</span> 核对版本。更新时页面会断开约半分钟，进度会自动接上。</div>
    <div class="rows">
      <div class="upd-ver">
        <div><small>当前运行</small><b id="updCur">${esc(short(u.current))}</b><span>${u.current === "dev" ? "本地构建，没有版本号" : "AIW_VERSION"}</span></div>
        <div><small>GitHub 最新镜像</small><b id="updGh">${esc(short(img.sha))}</b><span>${head.message ? esc(head.message) : gh.error ? "读取失败：" + esc(gh.error) : ""}${gh.building ? " · 新镜像构建中" : ""}</span></div>
      </div>
      <div class="upd-steps" id="updSteps">${UPD_STEPS.map(([k, t]) => `<div class="s" data-k="${k}"><i></i>${t}</div>`).join("")}</div>
      <div class="upd-res" id="updRes"></div>
      <pre class="upd-log" id="updLog"></pre>
    </div>
    <div class="rows">
      ${row("服务器地址", "Docker 主机的 IP 或域名", `<div class="inl"><input name="host" value="${esc(c.host || "")}" placeholder="192.168.50.102" /><input name="port" value="${esc(c.port || 22)}" style="flex:0 0 80px;min-width:0" aria-label="端口" /></div>`)}
      ${row("用户名", "", `<input name="user" value="${esc(c.user || "root")}" autocomplete="off" />`)}
      ${row("密码", "加密保存在服务器", `<input name="password" type="password" autocomplete="new-password" placeholder="${u.has_password ? "已保存，留空不修改" : "SSH 密码"}" />`, u.has_password ? '<span class="chip ok">已保存</span>' : '<span class="chip">未设置</span>')}
      ${row("安装目录", "docker-compose.yml 所在的文件夹", `<input name="dir" value="${esc(c.dir || "~/docker/aiw")}" />`)}
      ${row("容器名", "", `<input name="container" value="${esc(c.container || "aiw-api")}" />`)}
      ${row("用 sudo", "用户不在 docker 组时打开", "", `<input type="checkbox" name="sudo" ${c.sudo ? "checked" : ""} aria-label="sudo" />`, "sw")}
      <div class="savebar"><span class="note" id="updNote"></span><button type="button" class="btn sm" onclick="updTest()">测试连接</button><button class="btn sm primary">保存</button></div>
    </div></form>`;
}
function depForm() { const e = $("sDeploy").elements; return { host: e.host.value, port: e.port.value, user: e.user.value, dir: e.dir.value, sudo: e.sudo.checked, password: e.password.value }; }
async function depSave(e) { if (e) e.preventDefault(); const r = await api("PUT", "/api/deploy/config", depForm()); $("sDeploy").elements.password.value = ""; toast("已保存"); return r; }
async function depTest() {
  $("depNote").textContent = "连接中…";
  try { await depSave(); const r = await api("POST", "/api/deploy/test"); $("depNote").textContent = `连接成功 · ${r.dir} · ${r.system} · ${r.docker}`; }
  catch (err) { $("depNote").textContent = err.message; }
}
function updForm() { const e = $("g-upd").elements; return { host: e.host.value, port: e.port.value, user: e.user.value, dir: e.dir.value, container: e.container.value, sudo: e.sudo.checked, password: e.password.value }; }
async function updSave(e) { if (e) e.preventDefault(); const r = await api("PUT", "/api/update/config", updForm()); $("g-upd").elements.password.value = ""; toast("已保存"); return r; }
async function updTest() {
  $("updNote").textContent = "连接中…";
  try { await updSave(); const r = await api("POST", "/api/update/test"); $("updNote").textContent = `连接成功 · ${r.dir} · ${r.compose.length ? r.compose.join(" ") : "没找到 compose 文件"} · 运行中 ${short(r.version)}`; }
  catch (err) { $("updNote").textContent = err.message; }
}
async function updRefresh() { const u = await api("GET", "/api/update"); $("updVerdict").innerHTML = updVerdict(u.current, u.github); $("updCur").textContent = short(u.current); $("updGh").textContent = short((u.github.image || {}).sha); return u; }
function updPaint(step, err) {
  const ks = UPD_STEPS.map((x) => x[0]); const at = ks.indexOf(step);
  document.querySelectorAll("#updSteps .s").forEach((el, i) => {
    el.className = "s" + (at < 0 ? "" : i < at ? " done" : i === at ? (err ? " err" : " cur") : "");
    el.querySelector("i").innerHTML = i < at ? icon("check") : i === at && err ? icon("x") : "";
  });
}
function updAllDone() { document.querySelectorAll("#updSteps .s").forEach((el) => { el.className = "s done"; el.querySelector("i").innerHTML = icon("check"); }); }
async function updStart() {
  if (!confirm("开始在线更新？AIW 会重启，大约半分钟连不上。")) return;
  $("updRes").className = "upd-res"; $("updRes").textContent = ""; $("updLog").textContent = "";
  updPaint("connect"); $("updGo").disabled = true;
  try { await updSave(); const j = await api("POST", "/api/update/start"); localStorage.setItem("aiw_upd", JSON.stringify({ t: j.started_at, target: j.target })); updPoll(); }
  catch (err) { updPaint("connect", true); updResult("bad", err.message); $("updGo").disabled = false; }
}
function updResult(kind, html) { const r = $("updRes"); if (!r) return; r.className = "upd-res " + kind; r.innerHTML = html; }
function updResume() { const j = JSON.parse(localStorage.getItem("aiw_upd") || "null"); if (j && Date.now() / 1000 - j.t < 900) { $("updGo").disabled = true; updPoll(); } }
let _updTimer = null, _updMiss = 0;
async function updPoll() {
  clearTimeout(_updTimer); if (!$("g-upd")) return;
  let st;
  try { st = await api("GET", "/api/update/status"); _updMiss = 0; }
  catch (err) {
    _updMiss++;
    updResult("warn", _updMiss > 2 ? "AIW 正在重启，等它回来…" : "读取进度中…");
    if (_updMiss > 90) { updResult("bad", "等了太久还没恢复：" + esc(err.message)); $("updGo").disabled = false; localStorage.removeItem("aiw_upd"); return; }
    _updTimer = setTimeout(updPoll, 2000); return;
  }
  $("updLog").textContent = st.log || ""; $("updLog").scrollTop = 1e9;
  if (!st.done) { updPaint(st.step || "connect"); updResult("", ""); _updTimer = setTimeout(updPoll, 2000); return; }
  localStorage.removeItem("aiw_upd"); $("updGo").disabled = false;
  if (st.rc !== 0) { updPaint(st.step || "pull", true); updResult("bad", `更新失败（退出码 ${st.rc}），看下面的日志。`); return; }
  updAllDone();
  const u = await updRefresh().catch(() => null);
  const img = u && u.github && u.github.image ? u.github.image.sha : "";
  const got = st.version || "";
  if (got && img && got === img) updResult("ok", `${icon("check")}已更新到 <b class="mono">${esc(short(got))}</b>，与 GitHub 最新镜像一致。<button type="button" class="btn sm" style="margin-left:auto" onclick="location.reload()">刷新页面</button>`);
  else if (got && img) updResult("warn", `已更新到 <b class="mono">${esc(short(got))}</b>，GitHub 最新镜像是 <b class="mono">${esc(short(img))}</b>${u.github.building ? "（新镜像还在构建，几分钟后再更新一次）" : ""}。`);
  else updResult("warn", `更新完成，容器版本 ${esc(short(got))}。没读到 GitHub 版本，无法对比。`);
}

/* ======================================================= AtomCode mouse effects (see ui.css) */
(function mouseFx() {
  if (matchMedia("(hover: none), (pointer: coarse), (prefers-reduced-motion: reduce)").matches) return;
  const au = document.createElement("div"); au.className = "aurora"; au.setAttribute("aria-hidden", "true"); document.body.appendChild(au);
  const SPOT = ".rows, .curcard, .bubble.result, .plusmenu, .upd-ver > div";
  let x = -400, y = -400, raf = 0, lit = null;
  const paint = () => {
    raf = 0;
    au.style.setProperty("--mx", x + "px"); au.style.setProperty("--my", y + "px");
    const el = document.elementFromPoint(x, y); const card = el && el.closest ? el.closest(SPOT) : null;
    if (lit && lit !== card) lit.classList.remove("lit");
    if (card) {
      card.classList.add("spot", "lit"); lit = card;
      const r = card.getBoundingClientRect(); card.style.setProperty("--sx", x - r.left + "px"); card.style.setProperty("--sy", y - r.top + "px");
    } else lit = null;
  };
  addEventListener("pointermove", (e) => { if (e.pointerType !== "mouse") return; x = e.clientX; y = e.clientY; au.classList.add("on"); if (!raf) raf = requestAnimationFrame(paint); }, { passive: true });
  document.addEventListener("pointerleave", () => { au.classList.remove("on"); if (lit) lit.classList.remove("lit"); lit = null; });
  addEventListener("blur", () => au.classList.remove("on"));
})();

/* ---------- mobile: viewfinder (direction A) — the live computer on top of the chat while a task runs */
const NARROW = window.matchMedia("(max-width:760px)");
function mobileViewfinder() {
  const chat = $("v-chat"); if (!chat) return;
  let vf = $("mvf");
  const live = liveTask();
  // the task view on a phone: the computer on top, the steps below (always, not only while the task runs)
  const want = NARROW.matches && S.view === "chat" && !!(live || (S.thread && S.thread.id));
  const frame = document.querySelector("#deskPane .deskframe, #mvf .deskframe");
  if (!want) {
    if (vf) { if (frame && frame.parentNode !== $("deskPane")) { $("deskPane").appendChild(frame); deskLoaded.small = null; if (S.view === "chat") loadDesktop(); } vf.remove(); }
    return;
  }
  if (!vf) {
    chat.insertAdjacentHTML("afterbegin", `<div id="mvf"><div class="mvf-screen"></div>
      <div class="mvf-bar" onclick="if(event.target.closest('button'))return;mvfFold()"><span class="mvf-dot"></span><div class="mvf-now"><small></small><span></span></div>
      <button class="mvf-take" onclick="mvfTake()">${icon("hand")}<span class="tk">接管</span></button>
      <button class="mvf-eye" onclick="mvfFold()" aria-label="显示电脑画面">${icon("monitor")}<span class="lb">画面</span></button></div></div>`);
    vf = $("mvf");
    if (frame) vf.querySelector(".mvf-screen").appendChild(frame);
    deskLoaded.small = null; loadDesktop();
  }
  // folded (just the slim status bar) unless the user opened the screen, or is driving it
  const fold = localStorage.getItem("aiw_mvf_fold") === "1" && !S.take;
  vf.classList.toggle("fold", fold);
  vf.classList.toggle("taking", !!S.take);
  vf.querySelector(".tk").textContent = S.take ? "交还" : "接管";
  vf.querySelector(".mvf-eye .lb").textContent = fold ? "画面" : "收起";
  vf.querySelector(".mvf-eye").setAttribute("aria-label", fold ? "显示电脑画面" : "隐藏电脑画面");
  const c = document.querySelector("#msgs .curcard .st.cur .tx");
  const lastS = document.querySelector("#msgs .work:last-of-type .steplist .s:last-child") || [...document.querySelectorAll("#msgs .steplist .s")].pop();
  const st = live ? (S.evs[live.id] || []).find((e) => e.type === "task.started") : null;
  vf.querySelector(".mvf-now small").textContent = (lastS ? lastS.querySelector("b").textContent + " · " : "") + (st ? mmss(Date.now() / 1000 - st.ts) : "");
  vf.querySelector(".mvf-now span").textContent = c ? c.textContent : live ? (document.querySelector("#msgs .work .head .grow") || { textContent: "工作中…" }).textContent : "电脑画面 · 任务没在运行";
}
// taking over needs the screen: open it first
function mvfTake() { if (!S.take) localStorage.setItem("aiw_mvf_fold", "0"); toggleTakeover(); setTimeout(() => { mobileViewfinder(); if (typeof deskTouch === "function") deskTouch(); }, 50); }
function mvfFold() { localStorage.setItem("aiw_mvf_fold", localStorage.getItem("aiw_mvf_fold") === "1" ? "0" : "1"); mobileViewfinder(); }
const _patchChatVF = patchChat;
patchChat = function () { _patchChatVF(); mobileViewfinder(); };
NARROW.addEventListener("change", mobileViewfinder);
const _goVF = go;
go = function (v) { const r = _goVF.apply(this, arguments); setTimeout(mobileViewfinder, 0); return r; };
setInterval(() => { if ($("mvf")) mobileViewfinder(); }, 1000);

/* ---------- composer: round amber start / stop button (direction A), pause beside the hint */
const _setBusyA = setComposerBusy;
setComposerBusy = function (t) {
  _setBusyA(t);
  const h = $("composerHint");
  if (t && LIVE.includes(t.status) && t.session_id) {
    const paused = t.status === "paused" || t.status === "takeover";
    h.innerHTML = `<span>${paused ? "已暂停 · 新消息会排队" : "工作中 · 新消息会排队"}</span><button class="btn xs" onclick="ctl('${t.session_id}','${paused ? "resume" : "pause"}')">${paused ? "继续" : "暂停"}</button>`;
  }
  mobileViewfinder();
};

/* ---------- BrowserOS (per computer) */
function bosSection(bos) {
  const ms = (bos && bos.machines) || [];
  const on = ms.filter((m) => m.enabled).length;
  const st = (m) => { const s = m.seen || {};
    if (!s.at) return '<span class="chip">未检测</span>';
    if (!s.installed) return '<span class="chip">未安装</span>';
    if (s.agent_old) return '<span class="chip warn">已安装 · vm-agent 需更新</span>';
    return s.running ? `<span class="chip ok">运行中${s.version ? " · " + esc(s.version) : ""}</span>` : `<span class="chip">已安装 · 未运行</span>`; };
  const note = (m) => { const s = m.seen || {};
    if (s.agent_old) return "BrowserOS 已装好，但这台电脑的 vm-agent 太旧，用不了它。在这台电脑的终端里跑下面这行更新：";
    return m.enabled ? "任务里优先用 BrowserOS 按元素操作网页" : s.installed ? "已关闭，任务只用截图点击" : ""; };
  const rows = ms.map((m) => { const s = m.seen || {}; const id = esc(m.id);
    return `<div class="rw"><div class="k"><div class="mach"><span class="ic">${icon("monitor")}</span><div><b>${esc(m.name)}</b><span class="mono">${m.cloud ? "云电脑" : "本地"}</span></div></div></div>
      <div class="v">${st(m)}<span class="small" id="bosT_${id}">${esc(note(m))}</span><div id="bosC_${id}">${m.command ? bosCmd(m.command) : ""}</div></div>
      <div class="t">${s.installed && !s.agent_old ? `<input type="checkbox" ${m.enabled ? "checked" : ""} onchange="bosToggle('${id}',this.checked)" aria-label="在这台电脑上使用 BrowserOS" />`
        : `<button type="button" class="btn sm" onclick="bosInstall('${id}')">${s.installed ? "重装" : "安装"}</button>`}</div></div>`; }).join("");
  return `<div class="rows" id="sBos">
      <div class="rw head"><div class="k">BrowserOS · 按网页元素操作，省 token、少点错</div><div>${on ? `<span class="chip ok">${on} 台已开启</span>` : '<span class="chip">未开启</span>'}</div></div>
      ${rows || '<div class="rw"><div class="k small">还没有电脑</div></div>'}
      <div class="savebar"><span class="note" id="bosNote">装好后默认开启；网站登录要在 BrowserOS 里登一次（接管桌面）。只支持 x86 电脑。</span><button type="button" class="btn sm" onclick="bosRefresh()">重新检测</button></div>
    </div>`;
}
/* the one-line command for the computer's own terminal (接管桌面 › 终端, or ssh); no password goes through Aiw */
function bosCmd(cmd) {
  return `<div class="bos-cmd"><code>${esc(cmd)}</code><button type="button" class="btn sm" onclick="bosCopy(this)" data-cmd="${esc(cmd)}">复制</button></div>
    <span class="small">打开「接管桌面」里的终端粘贴运行，输一次这台电脑的密码；跑完回来点「重新检测」。</span>`;
}
function bosCopy(btn) {
  const text = btn.getAttribute("data-cmd") || "";
  const done = () => toast("已复制命令");
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done, () => {}); return; }
  const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("复制失败，请手动选中命令"); }
  ta.remove();
}
/* what a failed install says: the script's own error line (+ a couple of lines before it), never curl's progress meter */
function bosInstallMsg(r) {
  if (r.need_sudo) return "需要这台电脑的 sudo 密码。Aiw 不经手密码：在这台电脑的终端里跑下面这行即可：";
  if (r.ok && r.agent_old) return "BrowserOS 已装好，但这台电脑的 vm-agent 太旧，用不了它。在这台电脑的终端里跑下面这行更新：";
  const d = (r.detail || []).filter(Boolean).slice(-2);
  return "安装没有完成：" + (r.error || "未知原因") + (d.length ? " · " + d.join(" · ") : "");
}
async function bosRefresh() { $("bosNote").textContent = "检测中…"; try { await api("GET", "/api/browseros?refresh_now=true"); loadSettings(); } catch (err) { $("bosNote").textContent = err.message; } }
async function bosToggle(id, on) {
  try { await api("PUT", `/api/browseros/${encodeURIComponent(id)}`, { enabled: on }); toast(on ? "已开启 BrowserOS" : "已关闭 BrowserOS，任务改用截图点击");
    const el = $("bosT_" + id); if (el) el.textContent = on ? "任务里优先用 BrowserOS 按元素操作网页" : "已关闭，任务只用截图点击"; }
  catch (err) { toast(err.message); }
}
async function bosInstall(id) {
  if (!confirm("在这台电脑上安装 BrowserOS？要下载约 200 MB，几分钟。已经装好的话只检查并启动它。")) return;
  const el = $("bosT_" + id), box = $("bosC_" + id);
  if (el) el.textContent = "安装中，几分钟…"; if (box) box.innerHTML = "";
  try { const r = await api("POST", `/api/browseros/${encodeURIComponent(id)}/install`);
    if (r.ok && !r.agent_old) { toast("BrowserOS 已装好并开启"); loadSettings(); return; }
    if (el) el.textContent = bosInstallMsg(r);
    if (box && r.command) box.innerHTML = bosCmd(r.command); }
  catch (err) { if (el) el.textContent = err.message; }
}

/* ---------- 运维主机 */
async function rhAdd(e) {
  e.preventDefault(); const f = e.target.elements;
  const body = { name: f.name.value, host: f.host.value, port: f.port.value, user: f.user.value, password: f.password.value, mode: f.mode.value };
  $("rhNote").textContent = "保存中…";
  try { const h = await api("POST", "/api/remote-hosts", body); $("rhNote").textContent = "已授权，连接测试中…";
    try { const r = await api("POST", `/api/remote-hosts/${h.id}/test`); toast("连接成功：" + (r.output || "").split("\n")[0]); } catch (err) { toast(err.message); }
    loadSettings(); }
  catch (err) { $("rhNote").textContent = err.message; }
}
async function rhTest(id) {
  const el = $("rhT_" + id); if (el) el.textContent = "连接中…";
  try { const r = await api("POST", `/api/remote-hosts/${id}/test`); if (el) el.textContent = "连接成功 · " + (r.output || "").split("\n").slice(0, 3).join(" · "); }
  catch (err) { if (el) el.textContent = err.message; }
}
async function rhMode(id, mode) { try { await api("PUT", `/api/remote-hosts/${id}`, { mode }); toast(mode === "auto" ? "只读命令不再询问" : "每条命令都会请你确认"); } catch (err) { toast(err.message); } }
async function rhDel(id) { if (!confirm("移除这台主机的授权？保存的密码会一起删除。")) return; await api("DELETE", `/api/remote-hosts/${id}`); toast("已移除"); loadSettings(); }
