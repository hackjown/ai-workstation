/* 设置 › Aiw 学到的经验 (3.8.5): what Aiw learned from earlier tasks (/api/lessons, services/api/aiw_api/lessons.py).
   Each lesson: where it applies (电脑 / 网站 / 技能 / 工具 / 通用), the problem, the fix that worked, how often it was used.
   「用户纠正过」 = the user's own correction (high weight). The user can view, search and delete. */
"use strict";

const LS = { list: [], scope: "", q: "" };
const LS_SCOPE = { machine: "电脑", site: "网站", skill: "技能", tool: "工具", general: "通用" };

async function loadLessonsPage() {
  try { LS.list = await api("GET", "/api/lessons"); }
  catch (e) { $("lessonPage").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; return; }
  renderLessonsPage();
}
function renderLessonsPage() {
  const q = LS.q.trim();
  const rows = LS.list.filter((l) => (!LS.scope || l.scope === LS.scope) &&
    (!q || `${l.where} ${l.problem} ${l.fix}`.includes(q)));
  const count = (k) => LS.list.filter((l) => !k || l.scope === k).length;
  const seg = [["", "全部"], ...Object.entries(LS_SCOPE)]
    .map(([k, v]) => `<button class="${LS.scope === k ? "on" : ""}" onclick="LS.scope='${k}';renderLessonsPage()">${v}<span class="mcount">${count(k)}</span></button>`).join("");
  $("lessonPage").innerHTML = `
    <div class="card mem-head"><div class="heading">Aiw 学到的经验</div>
      <div class="muted">任务做完后，Aiw 会把「遇到什么问题 → 怎么解决的」记下来，按电脑、网站、技能、工具分类；下次遇到同样的问题直接用，出错时也会按报错去找。你纠正过的做法标为「用户纠正过」，最优先。不对的经验可以删掉。</div></div>
    <div class="row gap wrap mem-bar"><div class="seg">${seg}</div>
      <input class="grow" placeholder="搜索经验…" value="${esc(LS.q)}" oninput="LS.q=this.value;renderLessonsPage();this.focus();this.setSelectionRange(this.value.length,this.value.length)" />
      ${LS.list.length ? `<button class="btn sm" onclick="lessonClear()">全部清空</button>` : ""}</div>
    <div class="list">${rows.map((l) => `<div class="item mem" data-id="${l.id}">
        <span class="chip mk-${l.from_user ? "decision" : "fact"}">${esc(l.scope_cn || LS_SCOPE[l.scope] || "通用")}</span>
        <div class="grow"><div class="mem-t">${l.from_user ? "<b>用户纠正过：</b>" : ""}${esc(l.problem)}</div>
          <div class="mem-t">→ ${esc(l.fix)}</div>
          <div class="small">${esc(l.where || "")} · 用过 ${l.hits || 0} 次 · ${l.last_used ? "上次用 " + ago(l.last_used) : "记于 " + ago(l.created_at)}</div></div>
        <div class="acts"><button class="icon sm" title="删除" onclick="lessonDel('${l.id}')">${icon("trash")}</button></div></div>`).join("")
      || `<div class="empty">${q || LS.scope ? "没有匹配的经验" : "还没有经验。Aiw 做完任务后会自己总结。"}</div>`}</div>`;
  paintIcons($("lessonPage"));
}
async function lessonDel(id) {
  if (!confirm("删除这条经验？以后 Aiw 不会再参考它。")) return;
  try { await api("DELETE", `/api/lessons/${id}`); toast("已删除"); loadLessonsPage(); } catch (err) { toast(err.message); }
}
async function lessonClear() {
  if (!confirm("清空全部经验？")) return;
  try { await api("DELETE", "/api/lessons"); toast("已清空"); loadLessonsPage(); } catch (err) { toast(err.message); }
}

TITLES.lessons = "Aiw 学到的经验";
const _goLes = go;
go = function (view) {
  const r = _goLes.apply(this, arguments);
  if (view === "lessons") {
    $("backBtn").hidden = false; $("menuBtn").classList.add("hide-back");
    document.querySelectorAll('[data-view="settings"]').forEach((b) => b.classList.add("on"));
    loadLessonsPage();
  }
  return r;
};
const _backLes = backToChat;
backToChat = function () { if (S.view === "lessons") go("settings"); else _backLes(); };
