/* 设置 › 记忆 (2026-10-28): view, edit and delete what 球球 remembers (/api/memories*, services/api/aiw_api/memory.py).
   Kinds: 画像 profile / 事实 fact / 决定 decision. Replaced ("superseded") memories are hidden unless 显示被替换的. */
"use strict";

const MEM_KIND = { profile: "画像", fact: "事实", decision: "决定" };
const MS = { list: [], kind: "", all: false, q: "" };

async function loadMemoryPage() {
  try { MS.list = await api("GET", "/api/memories" + (MS.all ? "?all=true" : "")); }
  catch (e) { $("memPage").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; return; }
  renderMemoryPage();
}
function memKindSel(name, cur) {
  return `<select name="${name}">${Object.entries(MEM_KIND).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${v}</option>`).join("")}</select>`;
}
function renderMemoryPage() {
  const q = MS.q.trim();
  const rows = MS.list.filter((m) => (!MS.kind || m.kind === MS.kind) && (!q || m.text.includes(q)));
  const count = (k) => MS.list.filter((m) => (!k || m.kind === k) && !m.superseded).length;
  const seg = [["", "全部"], ["profile", "画像"], ["fact", "事实"], ["decision", "决定"]]
    .map(([k, v]) => `<button data-k="${k}" class="${MS.kind === k ? "on" : ""}" onclick="MS.kind='${k}';renderMemoryPage()">${v}<span class="mcount">${count(k)}</span></button>`).join("");
  $("memPage").innerHTML = `
    <div class="card mem-head"><div class="heading">记忆</div>
      <div class="muted">聊天和每个任务都会参考这里：你的画像，以及和当前请求最相关的 8 条事实和决定。在聊天里说「记住…」会立刻记下，说「忘掉…」会删掉；聊天之后球球也会自己整理。密码、验证码和 token 永远不会被记下。</div></div>
    <form class="mem-add" onsubmit="memAdd(event)">
      ${memKindSel("kind", "fact")}
      <input name="text" placeholder="添加一条记忆，例如：周报放在 ~/workspace/reports，用 Word 格式" maxlength="300" required />
      <button class="btn primary">${icon("plus")}添加</button>
    </form>
    <div class="row gap wrap mem-bar"><div class="seg">${seg}</div>
      <input class="grow" placeholder="搜索记忆…" value="${esc(MS.q)}" oninput="MS.q=this.value;renderMemoryPage();this.focus();this.setSelectionRange(this.value.length,this.value.length)" />
      <label class="toggle small"><input type="checkbox" ${MS.all ? "checked" : ""} onchange="MS.all=this.checked;loadMemoryPage()" /> 显示被替换的</label></div>
    <div class="list">${rows.map((m) => `<div class="item mem ${m.superseded ? "old" : ""}" data-id="${m.id}">
        <span class="chip mk-${m.kind}">${MEM_KIND[m.kind] || "事实"}</span>
        <div class="grow"><div class="mem-t">${esc(m.text)}</div>
          <div class="small">${m.source === "user" ? "你添加" : /^msg-/.test(m.source || "") ? "从聊天里记下" : /^task-/.test(m.source || "") ? "任务里记下" : "球球记下"} · ${ago(m.updated_at || m.created_at)}${m.superseded ? " · 已被新的记忆替换" : ""}</div></div>
        <div class="acts">${m.superseded ? `<button class="btn sm" onclick="memRestore('${m.id}')">恢复</button>` : `<button class="btn sm" onclick="memEdit('${m.id}')">编辑</button>`}
          <button class="icon sm" title="删除" onclick="memDel('${m.id}')">${icon("trash")}</button></div></div>`).join("")
      || `<div class="empty">${q || MS.kind ? "没有匹配的记忆" : "还没有记忆。在聊天里说「记住…」试试。"}</div>`}</div>`;
  paintIcons($("memPage"));
}
async function memAdd(e) {
  e.preventDefault(); const f = e.target;
  try { await api("POST", "/api/memories", { text: f.text.value, kind: f.kind.value }); toast("已记下"); loadMemoryPage(); }
  catch (err) { toast(err.message); }
}
function memEdit(id) {
  const m = MS.list.find((x) => x.id === id); if (!m) return;
  const el = document.querySelector(`.item.mem[data-id="${id}"]`);
  el.innerHTML = `<form class="mem-edit grow" onsubmit="memSave(event,'${id}')">${memKindSel("kind", m.kind)}
    <input name="text" value="${esc(m.text)}" maxlength="300" required />
    <button class="btn sm primary">保存</button><button type="button" class="btn sm" onclick="renderMemoryPage()">取消</button></form>`;
  el.querySelector("input").focus();
}
async function memSave(e, id) {
  e.preventDefault(); const f = e.target;
  try { await api("PUT", `/api/memories/${id}`, { text: f.text.value, kind: f.kind.value }); toast("已保存"); loadMemoryPage(); }
  catch (err) { toast(err.message); }
}
async function memDel(id) {
  const m = MS.list.find((x) => x.id === id);
  if (!confirm(`删除这条记忆？\n${m ? m.text : ""}`)) return;
  try { await api("DELETE", `/api/memories/${id}`); toast("已删除"); loadMemoryPage(); } catch (err) { toast(err.message); }
}
async function memRestore(id) { try { await api("POST", `/api/memories/${id}/restore`); toast("已恢复"); loadMemoryPage(); } catch (err) { toast(err.message); } }

TITLES.memory = "记忆";
const _goMem = go;
go = function (view) {
  const r = _goMem.apply(this, arguments);
  if (view === "memory") {
    $("backBtn").hidden = false; $("menuBtn").classList.add("hide-back");
    document.querySelectorAll('[data-view="settings"]').forEach((b) => b.classList.add("on"));
    loadMemoryPage();
  }
  return r;
};
const _backMem = backToChat;
backToChat = function () { if (S.view === "memory") go("settings"); else _backMem(); };
