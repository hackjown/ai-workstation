/* AIW web: open .md / .json / text output files in an in-app preview instead of downloading them.
   Loaded after app.js and ui.js. Any a.filecard pointing at /api/artifacts whose name has a previewable extension
   opens a modal: rendered Markdown (mdview.js), pretty JSON, or plain text, with 复制全文 / 下载 / 关闭.
   v3.9.2: Office files (officeview.js) too: Word / Excel rendered from the original file, the rest as LibreOffice PDF. */
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
    const p = decodeURIComponent((href.match(/[?&]path=([^&]+)/) || [])[1] || "");
    const k = AiwMd.kindOf(cardName(a)) || AiwMd.kindOf(p);
    if (k) return k;
    const O = window.AiwOffice;
    return O && (O.kindOf(cardName(a)) || O.kindOf(p)) ? "office" : "";
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

  let onResize = null;
  function close() {
    const m = document.getElementById("mdModal"); if (m) m.remove(); document.removeEventListener("keydown", onKey);
    if (onResize) { window.removeEventListener("resize", onResize); onResize = null; }
  }
  function onKey(e) { if (e.key === "Escape") close(); }

  // Office: Word/Excel straight from the file, a 「按打印版式查看(PDF)」 switch; other formats as PDF
  async function openOffice(href, name) {
    close();
    const O = window.AiwOffice, k = O.kindOf(name);
    const label = (name.split(".").pop() || "").toUpperCase();
    document.body.insertAdjacentHTML("beforeend", `<div class="mdm mdm-office" id="mdModal" role="dialog" aria-modal="true">
      <div class="mdm-box"><div class="mdm-h"><b></b><span class="mdm-k"></span><button type="button" class="mdm-pdf" hidden>按打印版式查看(PDF)</button><a class="mdm-dl">下载</a><button type="button" class="mdm-x" aria-label="关闭">×</button></div>
      <div class="mdm-b"></div></div></div>`);
    const m = document.getElementById("mdModal");
    m.querySelector(".mdm-h b").textContent = name; m.querySelector(".mdm-k").textContent = label;
    const dl = m.querySelector(".mdm-dl"); dl.href = href; dl.setAttribute("download", name);
    m.addEventListener("click", (e) => { if (e.target === m || e.target.closest(".mdm-x")) close(); });
    document.addEventListener("keydown", onKey);
    const body = m.querySelector(".mdm-b"), sw = m.querySelector(".mdm-pdf");
    let mode = "direct";
    const run = async (want) => {
      sw.disabled = true;
      const res = await O.show(body, href, name, { mode: want, download: href });
      if (!document.body.contains(m)) return;
      mode = res.mode;
      if (onResize) window.removeEventListener("resize", onResize);
      onResize = res.refit ? () => res.refit() : null;
      if (onResize) window.addEventListener("resize", onResize);
      // only Word / Excel have two views
      sw.hidden = k === "pdf";
      sw.textContent = mode === "pdf" ? "按原文件查看" : "按打印版式查看(PDF)";
      sw.disabled = false;
    };
    sw.onclick = () => run(mode === "pdf" ? "direct" : "pdf");
    await run(k === "pdf" ? "pdf" : "direct");
  }

  async function open(href, name, kind) {
    if (kind === "office") return openOffice(href, name);
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
