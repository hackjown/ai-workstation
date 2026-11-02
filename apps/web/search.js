/* v3.9.14 in-app search over the chat, tasks and files (GET /api/search, server: search.py).
   The top-bar search button or "/" (outside a text field) or Ctrl/⌘+K opens it; Enter / a click opens the hit:
   a chat message scrolls the chat to it, a task (or a task's file) opens that task's view (window.aiwOpen). */
"use strict";
(function () {
  const SS = { q: "", kind: "", res: [], sel: 0, t: null, seq: 0 };
  const KINDS = [["", "全部"], ["chat", "对话"], ["task", "任务"], ["file", "文件"]];
  const STATUS = { succeeded: "完成", failed: "失败", running: "进行中", cancelled: "已取消", waiting: "等待中" };
  const e = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function root() {
    let el = document.getElementById("aiwSearch");
    if (el) return el;
    el = document.createElement("div");
    el.id = "aiwSearch";
    el.innerHTML = `<div class="sbox" role="dialog" aria-label="搜索">
      <div class="shead"><svg class="i" viewBox="0 0 24 24"><path d="M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3"/></svg>
        <input id="aiwSearchQ" type="search" placeholder="搜对话、任务和文件" autocomplete="off" enterkeyhint="search" />
        <button class="btn sm" onclick="closeSearch()">取消</button></div>
      <div class="skinds">${KINDS.map(([k, n]) => `<button data-k="${k}" class="${k === SS.kind ? "on" : ""}">${n}</button>`).join("")}</div>
      <div class="slist" id="aiwSearchList"><div class="sempty">输入关键词，中文两个字以上</div></div></div>`;
    document.body.appendChild(el);
    el.addEventListener("click", (ev) => { if (ev.target === el) closeSearch(); });
    el.querySelectorAll(".skinds button").forEach((b) => b.addEventListener("click", () => {
      SS.kind = b.dataset.k; el.querySelectorAll(".skinds button").forEach((x) => x.classList.toggle("on", x === b)); run();
    }));
    const q = el.querySelector("#aiwSearchQ");
    q.addEventListener("input", () => { SS.q = q.value; clearTimeout(SS.t); SS.t = setTimeout(run, 220); });
    q.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); closeSearch(); }
      else if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); SS.sel = Math.max(0, Math.min(SS.res.length - 1, SS.sel + (ev.key === "ArrowDown" ? 1 : -1))); paint(); }
      else if (ev.key === "Enter") { ev.preventDefault(); clearTimeout(SS.t); if (SS.res[SS.sel]) pick(SS.sel); else run(); }
    });
    return el;
  }
  function mark(text, q) {
    let h = e(text);
    for (const w of String(q || "").split(/[\s,，、]+/).filter((x) => x.length >= 1).slice(0, 6)) {
      const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
      h = h.replace(re, (m) => `<mark>${m}</mark>`);
    }
    return h;
  }
  function when(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000), now = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
    if (d.toDateString() === now.toDateString()) return "今天 " + hm;
    return (d.getFullYear() === now.getFullYear() ? "" : d.getFullYear() + "-") + `${d.getMonth() + 1}-${d.getDate()} ` + hm;
  }
  function paint() {
    const list = document.getElementById("aiwSearchList");
    if (!list) return;
    if (!SS.q.trim()) { list.innerHTML = `<div class="sempty">输入关键词，中文两个字以上</div>`; return; }
    if (!SS.res.length) { list.innerHTML = `<div class="sempty">没有搜到「${e(SS.q)}」，换个说法试试</div>`; return; }
    let html = "", last = "";
    SS.res.forEach((r, i) => {
      if (!SS.kind && r.kind !== last) { html += `<div class="sgrp">${e(r.kind_cn)}</div>`; last = r.kind; }
      const title = r.kind === "chat" ? (r.role === "user" ? "我说" : "Aiw 说") : r.title;
      const meta = [when(r.ts), r.kind === "task" && r.status ? (STATUS[r.status] || r.status) : "", r.kind === "file" && r.path ? r.path : ""]
        .filter(Boolean).join(" · ");
      html += `<button class="shit${i === SS.sel ? " sel" : ""}" data-i="${i}"><div class="t">${mark(title, SS.q)}</div>`
        + (r.kind === "file" ? "" : `<div class="s">${mark(r.snippet, SS.q)}</div>`) + `<div class="m">${e(meta)}</div></button>`;
    });
    if (SS.total > SS.res.length) html += `<div class="sempty">共 ${SS.total} 条，只显示最相关的 ${SS.res.length} 条</div>`;
    list.innerHTML = html;
    list.querySelectorAll(".shit").forEach((b) => b.addEventListener("click", () => pick(+b.dataset.i)));
    const sel = list.querySelector(".shit.sel"); if (sel) sel.scrollIntoView({ block: "nearest" });
  }
  async function run() {
    const q = SS.q.trim(), my = ++SS.seq;
    if (!q) { SS.res = []; paint(); return; }
    try {
      const r = await api("GET", `/api/search?q=${encodeURIComponent(q)}&limit=40${SS.kind ? "&kind=" + SS.kind : ""}`);
      if (my !== SS.seq) return;   // a newer keystroke already asked
      SS.res = r.results || []; SS.total = r.total || 0; SS.sel = 0; paint();
    } catch (err) { if (my === SS.seq) document.getElementById("aiwSearchList").innerHTML = `<div class="sempty">搜索失败：${e(err.message)}</div>`; }
  }
  function pick(i) {
    const r = SS.res[i];
    if (!r) return;
    closeSearch();
    if (typeof window.aiwOpen === "function") window.aiwOpen(r.open || "chat");
  }
  window.openSearch = function () {
    const el = root();
    if (typeof closeOverlays === "function") closeOverlays();
    el.classList.add("on");
    const q = document.getElementById("aiwSearchQ");
    setTimeout(() => { q.focus(); q.select(); }, 30);
    if (SS.q) run();
  };
  window.closeSearch = function () { const el = document.getElementById("aiwSearch"); if (el) el.classList.remove("on"); };
  document.addEventListener("keydown", (ev) => {
    const t = ev.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
    if ((ev.key === "k" || ev.key === "K") && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); window.openSearch(); }
    else if (ev.key === "/" && !typing && !ev.metaKey && !ev.ctrlKey) { ev.preventDefault(); window.openSearch(); }
  });
})();
