/* AIW web: open .md / .json / text output files in an in-app preview instead of downloading them.
   Loaded after app.js and ui.js. Any a.filecard pointing at /api/artifacts whose name has a previewable extension
   opens a modal: rendered Markdown (mdview.js), pretty JSON, or plain text, with 复制全文 / 下载 / 关闭. */
"use strict";
(function () {
  const PREVIEW_MAX = 4 * 1024 * 1024;   // bigger files just download
  function cardName(a) {
    const b = a.querySelector("b");
    return (a.getAttribute("download") || (b && b.textContent) || a.textContent || "").trim();
  }
  function cardKind(a) {
    const href = a.getAttribute("href") || "";
    if (!/\/api\/artifacts\?/.test(href) || /[?&]member=/.test(href)) return "";
    return AiwMd.kindOf(cardName(a)) || AiwMd.kindOf(decodeURIComponent((href.match(/[?&]path=([^&]+)/) || [])[1] || ""));
  }
  // label previewable cards (after app.js + ui.js have rendered them)
  function markCards(root) {
    (root || document).querySelectorAll("a.filecard:not([data-mdk])").forEach((a) => {
      const k = cardKind(a); a.dataset.mdk = k || "-";
      if (!k) return;
      a.classList.add("previewable");
      const sm = a.querySelector(".small"); if (sm) sm.textContent = sm.textContent.replace("点击下载", "点击预览").replace("刚刚生成", "刚刚生成 · 点击预览");
    });
  }
  new MutationObserver(() => markCards()).observe(document.documentElement, { childList: true, subtree: true });

  function close() { const m = document.getElementById("mdModal"); if (m) m.remove(); document.removeEventListener("keydown", onKey); }
  function onKey(e) { if (e.key === "Escape") close(); }

  async function open(href, name, kind) {
    close();
    const view = href.replace(/([?&])download=1&?/, "$1").replace(/[?&]$/, "");
    const k = { md: "MD", json: "JSON", text: (name.split(".").pop() || "TXT").toUpperCase() }[kind];
    document.body.insertAdjacentHTML("beforeend", `<div class="mdm" id="mdModal" role="dialog" aria-modal="true">
      <div class="mdm-box"><div class="mdm-h"><b></b><span class="mdm-k"></span><button type="button" class="mdm-cp">复制全文</button><a class="mdm-dl">下载</a><button type="button" class="mdm-x" aria-label="关闭">×</button></div>
      <div class="mdm-b"><div class="mdm-load">加载中…</div></div></div></div>`);
    const m = document.getElementById("mdModal");
    m.querySelector(".mdm-h b").textContent = name; m.querySelector(".mdm-k").textContent = k;
    const dl = m.querySelector(".mdm-dl"); dl.href = href; dl.setAttribute("download", name);
    m.addEventListener("click", (e) => { if (e.target === m || e.target.closest(".mdm-x")) close(); });
    document.addEventListener("keydown", onKey);
    const body = m.querySelector(".mdm-b");
    let text = "";
    try {
      const r = await fetch(view);
      if (!r.ok) throw new Error(r.status + "");
      const buf = await r.arrayBuffer();
      if (buf.byteLength > PREVIEW_MAX) { body.innerHTML = `<div class="mdm-load">文件太大（${(buf.byteLength / 1048576).toFixed(1)} MB），请下载查看。</div>`; return; }
      text = new TextDecoder("utf-8").decode(buf);
    } catch (e) { body.innerHTML = `<div class="mdm-load">打不开这个文件（${String(e.message || e)}），可以下载查看。</div>`; return; }
    if (!document.body.contains(m)) return;
    AiwMd.show(body, text, kind);
    body.scrollTop = 0;
    m.querySelector(".mdm-cp").onclick = (e) => {
      const b = e.currentTarget;
      AiwMd.copyText(text).then(() => { b.textContent = "已复制"; setTimeout(() => { b.textContent = "复制全文"; }, 1600); }, () => { b.textContent = "复制失败"; });
    };
  }

  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a.filecard");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button > 0) return;
    const kind = cardKind(a); if (!kind) return;
    e.preventDefault();
    open(a.getAttribute("href"), cardName(a).split("/").pop() || "file", kind);
  }, true);

  window.mdPreview = open;
})();
