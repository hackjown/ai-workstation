/* 3.8 push deep links on the web. A Bark / ntfy push in "web" link mode opens <public url>/open/<path>; the server
   redirects that to /?open=<path> and this file routes it (the iOS app gets the same paths as aiw://<path>):
     task/<id>                     the task's own view (its thread)
     takeover/<machine>?task=<id>  that task's view, then the desktop takeover
     monitor/<watch id>            追踪, the watch highlighted (立即检查 on its row)
     chat[?m=<message id>]         the chat home at the newest message
   window.aiwOpen(path) is the same router for anything else in the page. */
"use strict";
(function () {
  function parse(path) {
    const [p, q] = String(path || "").replace(/^\/+/, "").split("?");
    return { parts: p.split("/").filter(Boolean).map(decodeURIComponent), q: new URLSearchParams(q || "") };
  }
  async function threadOf(taskId) {
    const t = await api("GET", `/api/tasks/${encodeURIComponent(taskId)}`);
    return (t && (t.thread_id || (t.task || {}).thread_id)) || null;
  }
  async function aiwOpen(path) {
    const { parts, q } = parse(path), kind = parts[0] || "chat";
    try {
      if (kind === "task" || kind === "takeover") {
        const taskId = kind === "task" ? parts[1] : q.get("task");
        if (kind === "takeover" && parts[1] && typeof pickMachine === "function") { try { pickMachine(parts[1]); } catch (e) { /* ignore */ } }
        const tid = taskId ? await threadOf(taskId) : null;
        if (!tid) { if (kind === "takeover") { go("computer"); return; } throw new Error("找不到这个任务"); }
        await openThread(tid);
        if (kind === "takeover" && typeof toggleTakeover === "function") setTimeout(() => { if (!S.take) toggleTakeover(); }, 1200);
        return;
      }
      if (kind === "monitor") {
        go("tracking");
        const id = parts[1];
        for (let i = 0; i < 20 && id; i++) {   // the list loads async
          await new Promise((r) => setTimeout(r, 150));
          const b = document.querySelector(`#v-tracking [onclick*="'${CSS.escape(id)}'"]`);
          const row = b && b.closest(".item, .row, .card");
          if (row) { row.classList.add("deep-hit"); row.scrollIntoView({ block: "center", behavior: "smooth" }); return; }
        }
        return;
      }
      // chat (and anything unknown): the chat home at the newest message
      go("home");
      if (typeof hShowChat === "function") hShowChat();
    } catch (e) { if (typeof toast === "function") toast("没能打开通知里的内容：" + e.message); }
  }
  window.aiwOpen = aiwOpen;
  const open = new URLSearchParams(location.search).get("open");
  if (open) {
    history.replaceState(null, "", location.pathname + location.hash);   // a reload must not route again
    setTimeout(() => aiwOpen(open), 400);   // after app.js / home.js have booted
  }
})();
