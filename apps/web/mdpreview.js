/* AIW web: open .md / .json / text output files in an in-app preview instead of downloading them.
   Loaded after app.js and ui.js. Any a.filecard pointing at /api/artifacts whose name has a previewable extension
   opens a modal: rendered Markdown (mdview.js), pretty JSON, or plain text, with 复制全文 / 下载 / 关闭.
   v3.9.2: Office files (officeview.js) too: Word / Excel rendered from the original file, the rest as LibreOffice PDF.
   v3.9.8: .html deliverables (e.g. the 装修设计器 from house-plan-designer) run inside the app in a sandboxed iframe
   (no same-origin: the page can never read the AIW token); its localStorage is a shim kept per file in ours. */
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
    if (/\.html?$/i.test(cardName(a)) || /\.html?$/i.test(p)) return "html";
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

  // v3.9.8 HTML: fetch it, put a storage shim in front of its scripts, run it as srcdoc in a sandboxed iframe
  const HTML_MAX = 60 * 1024 * 1024;
  const SANDBOX = "allow-scripts allow-downloads allow-popups allow-popups-to-escape-sandbox allow-modals allow-forms allow-pointer-lock";
  function storeKey(href) {
    return "aiw-html:" + decodeURIComponent((href.match(/[?&]path=([^&]+)/) || [])[1] || href);
  }
  function shim(key) {
    let data = {};
    try { data = JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch (e) { data = {}; }
    const js = "(function(){var K=" + JSON.stringify(key) + ",m=new Map(Object.entries(" + JSON.stringify(data) + ")),t;" +
      "function push(){try{parent.postMessage({aiwHtmlStore:K,data:Object.fromEntries(m)},'*')}catch(e){}}" +
      "function ch(){clearTimeout(t);t=setTimeout(push,400)}" +
      "var S={getItem:function(k){k=String(k);return m.has(k)?m.get(k):null},setItem:function(k,v){m.set(String(k),String(v));ch()}," +
      "removeItem:function(k){m.delete(String(k));ch()},clear:function(){m.clear();ch()},key:function(i){return Array.from(m.keys())[i]||null}," +
      "get length(){return m.size}};var ok=1;try{window.localStorage.getItem('x')}catch(e){ok=0}" +
      "if(!ok){try{Object.defineProperty(window,'localStorage',{value:S,configurable:true})}catch(e){}" +
      "try{Object.defineProperty(window,'sessionStorage',{value:S,configurable:true})}catch(e){}}" +
      "addEventListener('pagehide',push)})();";
    return "<script>" + js.replace(/<\//g, "<\\/") + "</" + "script>";
  }
  function injectShim(html, key) {
    const tag = shim(key), m = html.match(/<meta[^>]+charset[^>]*>/i);
    if (m) return html.slice(0, m.index + m[0].length) + tag + html.slice(m.index + m[0].length);
    const h = html.match(/<head[^>]*>/i);
    return h ? html.slice(0, h.index + h[0].length) + tag + html.slice(h.index + h[0].length) : tag + html;
  }
  let onStore = null;
  async function openHtml(href, name) {
    close();
    const view = href.replace(/([?&])download=1&?/, "$1").replace(/[?&]$/, "");
    document.body.insertAdjacentHTML("beforeend", `<div class="mdm mdm-html" id="mdModal" role="dialog" aria-modal="true">
      <div class="mdm-box"><div class="mdm-h"><b></b><span class="mdm-k">HTML</span><button type="button" class="mdm-share" title="生成别人不用登录也能打开的链接和二维码">分享</button><button type="button" class="mdm-fs">全屏</button><a class="mdm-dl">下载</a><button type="button" class="mdm-x" aria-label="关闭">×</button></div>
      <div class="mdm-b"><div class="mdm-load">加载中…</div></div></div></div>`);
    const m = document.getElementById("mdModal");
    m.querySelector(".mdm-h b").textContent = name;
    const dl = m.querySelector(".mdm-dl"); dl.href = href; dl.setAttribute("download", name);
    m.addEventListener("click", (e) => { if (e.target.closest(".mdm-x")) close(); });   // no backdrop close: a 3D drag must not shut it
    document.addEventListener("keydown", onKey);
    const body = m.querySelector(".mdm-b");
    let html = "";
    try {
      const r = await fetch(view);
      if (!r.ok) throw new Error(r.status + "");
      const buf = await r.arrayBuffer();
      if (buf.byteLength > HTML_MAX) { body.innerHTML = `<div class="mdm-load">文件太大（${(buf.byteLength / 1048576).toFixed(1)} MB），请下载后用浏览器打开。</div>`; return; }
      html = new TextDecoder("utf-8").decode(buf);
    } catch (e) { body.innerHTML = `<div class="mdm-load">打不开这个文件（${String(e.message || e)}），可以下载查看。</div>`; return; }
    if (!document.body.contains(m)) return;
    const key = storeKey(href);
    const fr = document.createElement("iframe");
    fr.className = "mdm-frame"; fr.setAttribute("sandbox", SANDBOX);
    fr.setAttribute("allow", "fullscreen; clipboard-write"); fr.setAttribute("allowfullscreen", "");
    fr.title = name;
    body.innerHTML = ""; body.appendChild(fr);
    if (onStore) window.removeEventListener("message", onStore);
    onStore = (e) => {
      if (e.source !== fr.contentWindow || !e.data || e.data.aiwHtmlStore !== key) return;
      try { localStorage.setItem(key, JSON.stringify(e.data.data || {})); } catch (err) { /* over quota: this session only */ }
    };
    window.addEventListener("message", onStore);
    fr.srcdoc = injectShim(html, key);
    m.querySelector(".mdm-share").onclick = () => shareDialog(href, name);
    m.querySelector(".mdm-fs").onclick = () => { const b = m.querySelector(".mdm-box"); (b.requestFullscreen || b.webkitRequestFullscreen || (() => {})).call(b); };
  }

  // 3.9.13: a read-only link (/s/<code>) + QR code for an .html deliverable; the designer opens in 只看模式
  async function shareDialog(href, name) {
    const path = decodeURIComponent((href.match(/[?&]path=([^&]+)/) || [])[1] || "");
    document.getElementById("shareModal")?.remove();
    document.body.insertAdjacentHTML("beforeend", `<div class="mdm" id="shareModal" role="dialog" aria-modal="true" style="z-index:10050">
      <div class="mdm-box" style="max-width:420px;height:auto"><div class="mdm-h"><b>分享</b><span class="grow"></span><button type="button" class="mdm-x" aria-label="关闭">×</button></div>
      <div style="padding:14px 16px;display:flex;flex-direction:column;gap:10px">
        <div class="small" style="word-break:break-all"></div>
        <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" class="sh-vo" checked> 只看模式（不能改方案）</label>
        <label style="display:flex;gap:8px;align-items:center">有效期 <select class="sh-days"><option value="7">7 天</option><option value="30" selected>30 天</option><option value="365">1 年</option><option value="0">永久</option></select></label>
        <button type="button" class="btn primary sh-go">生成链接</button>
        <div class="sh-out" style="display:none;flex-direction:column;gap:8px;align-items:center">
          <div class="sh-qr" style="width:200px"></div>
          <input class="sh-url" readonly style="width:100%">
          <div style="display:flex;gap:8px"><button type="button" class="btn sh-copy">复制链接</button><a class="btn sh-open" target="_blank" rel="noopener">打开</a><button type="button" class="btn sh-rev">作废</button></div>
          <div class="small muted">对方不用登录；微信里长按二维码或点链接就能看。链接经过你的 AIW 服务器，服务器要能从外网访问。</div>
        </div></div></div></div>`);
    const m = document.getElementById("shareModal");
    m.querySelector(".small").textContent = name;
    m.addEventListener("click", (e) => { if (e.target === m || e.target.closest(".mdm-x")) m.remove(); });
    const out = m.querySelector(".sh-out");
    let code = "";
    m.querySelector(".sh-go").onclick = async (e) => {
      e.target.disabled = true;
      try {
        const r = await api("POST", "/api/shares", { path, days: +m.querySelector(".sh-days").value, view_only: m.querySelector(".sh-vo").checked, title: name.replace(/\.html?$/i, "") });
        code = r.code;
        m.querySelector(".sh-qr").innerHTML = r.qr_svg || "";
        const qs = m.querySelector(".sh-qr svg"); if (qs) { qs.style.width = "100%"; qs.style.height = "auto"; }
        m.querySelector(".sh-url").value = r.url; m.querySelector(".sh-open").href = r.url;
        out.style.display = "flex"; e.target.textContent = "重新生成";
      } catch (err) { alert("生成失败：" + (err.message || err)); }
      e.target.disabled = false;
    };
    m.querySelector(".sh-copy").onclick = async () => {
      const u = m.querySelector(".sh-url"); u.select();
      try { await navigator.clipboard.writeText(u.value); } catch (err) { document.execCommand("copy"); }
      m.querySelector(".sh-copy").textContent = "已复制";
    };
    m.querySelector(".sh-rev").onclick = async () => {
      if (!code || !confirm("作废后这个链接立刻打不开，确定？")) return;
      try { await api("DELETE", "/api/shares/" + code); m.querySelector(".sh-rev").textContent = "已作废"; m.querySelector(".sh-qr").innerHTML = ""; } catch (err) { alert(err.message || err); }
    };
  }
  window.AiwShare = { open: shareDialog };

  async function open(href, name, kind) {
    if (kind === "office") return openOffice(href, name);
    if (kind === "html") return openHtml(href, name);
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
