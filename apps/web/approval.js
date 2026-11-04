/* Plain-language approval and takeover cards (server builds the summary: tool_router/explain.py).
   A confirmation shows what will happen in words — title, 要做什么, 影响, 风险 tag, 为什么 — and keeps the raw
   command folded under 查看技术细节. Past decisions stay on the task as a short log. Loaded after app.js. */
(function () {
  const E = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const KIND = { computer: "电脑", delete: "删除", create: "新建", write: "写入", read: "读取", move: "移动", app: "应用",
    site: "网站", package: "软件包", money: "金额", place: "地址" };
  const RISK_CN = { low: "低风险", medium: "中风险", high: "高风险" };
  const tn = (t) => (typeof toolName === "function" ? toolName(t) : t);

  /** A summary for cards from an older server that sent only {tool, payload, reason}. */
  function fallback(c) {
    const p = c.payload || {};
    return { title: "确认：" + tn(c.tool) + (c.reason ? "（" + c.reason + "）" : ""), what: "Aiw 想执行「" + tn(c.tool) + "」这一步。",
      impact: [], risks: c.reason ? [c.reason] : [], risk: "medium", why: "",
      tech: { tool: c.tool, reason: c.reason, level: c.level, command: p.command, payload: p.command ? undefined : p } };
  }
  function chips(list) {
    if (!list || !list.length) return "";
    return `<div class="apv-chips">${list.map((i) => `<span class="apv-chip k-${E(i.kind)}"><em>${E(KIND[i.kind] || i.kind)}</em>${E(i.label)}</span>`).join("")}</div>`;
  }
  function tag(risk) { return `<span class="apv-risk ${E(risk)}">${E(RISK_CN[risk] || risk)}</span>`; }
  function tech(s) {
    const t = s.tech || {};
    const body = t.command ? t.command : JSON.stringify(t.payload || {}, null, 2);
    return `<details class="apv-tech"><summary>查看技术细节</summary><pre>${E(body)}</pre>
      <div class="small">工具 ${E(tn(t.tool))}${t.level != null ? ` · 级别 L${E(t.level)}` : ""}${t.reason ? ` · ${E(t.reason)}` : ""}</div></details>`;
  }

  /** The pending confirmation card. Root keeps class "ask c" (xhs.js finds the review card by it). */
  window.apvCard = function (c, sid) {
    const s = c.summary || fallback(c);
    const risk = s.risk || "medium";
    return `<div class="ask c apv r-${E(risk)}">
      <div class="apv-k">${typeof icon === "function" ? icon("hand") : ""}<span>需要你确认</span>${tag(risk)}</div>
      <b class="apv-t">${E(s.title)}</b>
      <p class="apv-w">${E(s.what)}</p>
      ${s.impact && s.impact.length ? `<div class="apv-sec"><span class="apv-l">影响</span>${chips(s.impact)}</div>` : ""}
      ${s.risks && s.risks.length ? `<div class="apv-sec"><span class="apv-l">风险</span><ul class="apv-risks">${s.risks.map((r) => `<li>${E(r)}</li>`).join("")}</ul></div>` : ""}
      ${s.why ? `<div class="apv-sec"><span class="apv-l">为什么</span><p class="apv-why">${E(s.why)}</p></div>` : ""}
      <div class="opts apv-btns"><button class="btn primary" onclick="confirmAct('${sid}',true)">允许</button>
        <button class="btn" onclick="confirmAct('${sid}',false)">拒绝</button>
        ${c.task_allow ? `<button class="btn ghost" onclick="confirmAct('${sid}',true,'task')" title="这个任务里之后的确认都自动允许（付款、发布除外）">本任务内都允许</button>` : ""}</div>
      ${tech(s)}</div>`;
  };

  /** The takeover request: what to do, where, on which computer; open the desktop, hand back, or cancel. */
  window.takeoverCard = function (fq, t) {
    const sid = t.session_id;
    const where = [];
    if (fq.machine) where.push({ kind: "computer", label: fq.machine });
    if (fq.site) where.push({ kind: "site", label: fq.site + (fq.site_host && fq.site_host !== fq.site ? " · " + fq.site_host : "") });
    else if (fq.page_title) where.push({ kind: "site", label: fq.page_title });
    const title = fq.title && fq.title.indexOf("需要你接管") === 0 ? fq.title : "需要你接管：" + (fq.title || "在桌面上处理一下");
    return `<div class="actcard tk apv-tk">
      <div class="ac-b">
        <div class="ac-k">${typeof icon === "function" ? icon("hand") : ""}Aiw 在等你</div>
        <b class="apv-t">${E(title)}</b>
        ${fq.steps ? `<p class="tk-do"><span>你要做的</span>${E(fq.steps)}</p>` : ""}
        ${chips(where)}
        ${fq.why ? `<p class="tk-why">为什么：${E(fq.why)}</p>` : ""}
        <div class="opts"><button class="btn primary" onclick="openTakeover('${sid}','${E(fq.machine_id || "")}')">${typeof icon === "function" ? icon("hand") : ""}打开桌面接管</button>
          <button class="btn" onclick="takeoverDone('${sid}')">${E(fq.done_label || "完成，交还给 Aiw")}</button>
          <button class="btn ghost" onclick="takeoverCancel('${sid}')">取消</button></div>
        <p class="small">接管时 Aiw 会暂停；处理好点「${E(fq.done_label || "完成，交还给 Aiw")}」，它从当前页面接着做。不要在聊天里发密码或验证码。</p>
      </div></div>`;
  };

  /** Past approvals and takeovers of a task, from its events: plain title, risk, and what the user decided. */
  window.apvLog = function (evs, tid) {
    const rows = [];
    let lastTk = null;
    for (const e of evs || []) {
      const d = e.data || {};
      if (e.type === "agent.confirm_answered") {
        const s = d.summary || { title: d.title || ("确认：" + tn(d.tool)), risk: d.risk || "medium" };
        const dec = d.approve ? ({ task: "本任务内自动允许", user_task: "已允许（本任务内都允许）" }[d.by] || "已允许")
          : ({ timeout: "没有确认，已跳过", stop: "任务已停止" }[d.by] || "已拒绝");
        rows.push(`<div class="apv-row ${d.approve ? "ok" : "no"}"><span class="apv-dec">${E(dec)}</span>${tag(s.risk || "medium")}
          <details><summary>${E(s.title)}</summary>${s.what ? `<p>${E(s.what)}</p>` : ""}${chips(s.impact)}
          ${s.risks && s.risks.length ? `<ul class="apv-risks">${s.risks.map((r) => `<li>${E(r)}</li>`).join("")}</ul>` : ""}${s.tech ? tech(s) : ""}</details></div>`);
      }
      if (e.type === "agent.input_required" && d.form && d.form.kind === "takeover") lastTk = d.form;
      if (e.type === "agent.input_answered" && d.takeover && lastTk) {
        const dec = d.cancelled ? "已取消接管" : d.answer == null ? "没有接管，已超时" : "你已处理并交还";
        rows.push(`<div class="apv-row tk ${d.cancelled || d.answer == null ? "no" : "ok"}"><span class="apv-dec">${E(dec)}</span>
          <span class="apv-tt">${E(d.title || lastTk.title || "接管")}</span></div>`);
        lastTk = null;
      }
    }
    if (!rows.length) return "";
    // v3.9.18: many approvals used to push the task's status off screen; only the latest 2 stay open, the rest fold
    const SHOW = 2;
    const ok = rows.filter((r) => r.includes('apv-row ok') || r.includes('apv-row tk ok')).length;
    const head = `确认记录 · ${rows.length} 条（允许 ${ok}${rows.length - ok ? `，未允许 ${rows.length - ok}` : ""}）`;
    if (rows.length <= SHOW + 1) return `<div class="apv-log"><div class="apv-l">${E(head)}</div>${rows.join("")}</div>`;
    const old = rows.slice(0, -SHOW), recent = rows.slice(-SHOW);
    return `<div class="apv-log"><div class="apv-l">${E(head)}</div>
      <details class="apv-fold" data-k="apv:${E(tid || "")}"><summary>更早的 ${old.length} 条</summary>${old.join("")}</details>${recent.join("")}</div>`;
  };

  window.openTakeover = async function (sid, mid) {
    try {
      if (mid && typeof MID !== "undefined" && mid !== MID && typeof pickMachine === "function") await pickMachine(mid);
    } catch (e) { /* stay on the current computer */ }
    if (typeof S !== "undefined" && !S.take && typeof toggleTakeover === "function") await toggleTakeover();
    const pane = document.getElementById("deskPane");
    if (pane && pane.scrollIntoView) pane.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
  window.takeoverCancel = async function (sid) {
    if (typeof S !== "undefined" && S.take && typeof toggleTakeover === "function") await toggleTakeover();
    if (typeof answerQ === "function") await answerQ(sid, "__cancel__");
    if (typeof toast === "function") toast("已取消接管，Aiw 会换个办法或说明卡在哪里");
  };
})();
