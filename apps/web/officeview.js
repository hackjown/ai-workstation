/* AIW web v3.9.2: Office documents previewed in the browser from the ORIGINAL file (no conversion where avoidable).
   - Word .docx/.docm/.dotx  -> vendor/office/docx-preview (+ jszip): pages with the document's own fonts, tables, images
   - Excel .xlsx/.xlsm/.xls/.xlsb/.ods -> vendor/office/xlsx.full.min.js (SheetJS): every sheet as a grid with tabs,
     merged cells, column widths, number formats and cell fills
   - everything else (.pptx/.ppt/.doc/.odt/.odp/.rtf/...) and anything the renderers fail on -> GET /api/artifacts/preview
     (LibreOffice -> PDF on the API host or one of the computers), shown in an iframe; docx/xlsx get a
     「按打印版式查看(PDF)」button for the same print layout.
   The libraries are vendored (no CDN) and loaded only when an Office file is opened. Used by mdpreview.js. */
"use strict";
(function (root) {
  const BASE = (function () {
    const s = document.currentScript && document.currentScript.src;
    return s ? s.replace(/[^/]*$/, "") : "";
  })();
  const DIRECT_MAX = 25 * 1024 * 1024;     // bigger files go straight to the PDF view
  const XLS_MAX_ROWS = 3000, XLS_MAX_COLS = 200;
  const escH = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const DOCX = ["docx", "docm", "dotx"];
  const XLSX = ["xlsx", "xlsm", "xls", "xlsb", "ods", "xltx"];
  const PDF_ONLY = ["doc", "dot", "ppt", "pptx", "pptm", "pps", "ppsx", "pot", "potx", "odt", "ott", "odp", "otp", "odg",
    "ots", "rtf", "wps", "et", "dps", "xlt"];
  const ext = (name) => String(name || "").toLowerCase().split(".").pop();
  /** "docx" | "xlsx" | "pdf" (convert on the server) | "" (not Office) */
  function kindOf(name) {
    const e = ext(name);
    if (DOCX.includes(e)) return "docx";
    if (XLSX.includes(e)) return "xlsx";
    if (PDF_ONLY.includes(e)) return "pdf";
    return "";
  }

  const loaded = {};
  function load(file) {
    if (!loaded[file]) loaded[file] = new Promise((ok, bad) => {
      const s = document.createElement("script"); s.src = BASE + "vendor/office/" + file; s.async = true;
      s.onload = () => ok(); s.onerror = () => { delete loaded[file]; bad(new Error("加载 " + file + " 失败")); };
      document.head.appendChild(s);
    });
    return loaded[file];
  }
  const libDocx = () => load("jszip.min.js").then(() => load("docx-preview.min.js")).then(() => root.docx);
  const libXlsx = () => load("xlsx.full.min.js").then(() => root.XLSX);

  /* ---------------------------------------------------------------- Word */
  async function renderDocx(el, buf) {
    const docx = await libDocx();
    if (!docx || !docx.renderAsync) throw new Error("docx-preview 不可用");
    el.innerHTML = "";
    const host = document.createElement("div"); host.className = "ofv-docx"; el.appendChild(host);
    await docx.renderAsync(buf, host, null, {
      className: "docx", inWrapper: true, ignoreWidth: false, ignoreHeight: false, ignoreFonts: false,
      breakPages: true, ignoreLastRenderedPageBreak: true, experimental: true, renderHeaders: true, renderFooters: true,
      renderFootnotes: true, renderEndnotes: true, useBase64URL: true,
    });
    // links open outside, never javascript:
    host.querySelectorAll("a[href]").forEach((a) => {
      const h = a.getAttribute("href") || "";
      if (/^\s*javascript:/i.test(h)) a.removeAttribute("href");
      else if (!h.startsWith("#")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
    });
    fitDocx(el);
    return () => fitDocx(el);
  }
  // pages keep their real width (A4 = 794 px); on a narrow screen the whole page is scaled down instead of reflowed
  function fitDocx(el) {
    const w = el.querySelector(".docx-wrapper"); if (!w) return;
    const page = w.querySelector("section.docx"); if (!page) return;
    w.style.zoom = "";
    const need = page.offsetWidth + 32, have = el.clientWidth;
    if (need > have && have > 0) w.style.zoom = String(Math.max(0.3, have / need));
  }

  /* ---------------------------------------------------------------- Excel */
  function colName(c) { let s = ""; c++; while (c > 0) { const m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = Math.floor((c - 1) / 26); } return s; }
  function rgb(x) { return x && /^[0-9a-f]{6,8}$/i.test(x) ? "#" + x.slice(-6) : ""; }
  function sheetHtml(X, ws) {
    if (!ws || !ws["!ref"]) return `<div class="ofv-empty">（空工作表）</div>`;
    const r = X.utils.decode_range(ws["!ref"]);
    const r1 = Math.min(r.e.r, r.s.r + XLS_MAX_ROWS - 1), c1 = Math.min(r.e.c, r.s.c + XLS_MAX_COLS - 1);
    const span = {}, hidden = {};
    (ws["!merges"] || []).forEach((m) => {
      span[m.s.r + "," + m.s.c] = [m.e.r - m.s.r + 1, m.e.c - m.s.c + 1];
      for (let y = m.s.r; y <= m.e.r; y++) for (let x = m.s.c; x <= m.e.c; x++) if (y !== m.s.r || x !== m.s.c) hidden[y + "," + x] = 1;
    });
    const cols = ws["!cols"] || [], rows = ws["!rows"] || [];
    let h = `<table class="ofv-grid"><colgroup><col class="ofv-rh">`;
    for (let c = r.s.c; c <= c1; c++) {
      const ci = cols[c] || {};
      const px = ci.hidden ? 0 : Math.round(ci.wpx || (ci.wch ? ci.wch * 7 + 5 : ci.width ? ci.width * 7 : 72));
      h += `<col style="width:${Math.max(px, ci.hidden ? 0 : 24)}px">`;
    }
    h += `</colgroup><thead><tr><th class="ofv-corner"></th>`;
    for (let c = r.s.c; c <= c1; c++) h += `<th>${colName(c)}</th>`;
    h += `</tr></thead><tbody>`;
    for (let y = r.s.r; y <= r1; y++) {
      const ri = rows[y] || {};
      if (ri.hidden) continue;
      h += `<tr${ri.hpx ? ` style="height:${Math.round(ri.hpx)}px"` : ""}><th>${y + 1}</th>`;
      for (let x = r.s.c; x <= c1; x++) {
        const k = y + "," + x; if (hidden[k]) continue;
        const cell = ws[X.utils.encode_cell({ r: y, c: x })];
        const sp = span[k];
        let st = "", cls = "";
        if (cell) {
          if (cell.t === "n" || cell.t === "d") cls = "n";
          else if (cell.t === "b") cls = "b";
          else if (cell.t === "e") cls = "e";
          const s = cell.s || {};
          const bg = (s.patternType && s.patternType !== "none") ? rgb(s.fgColor && s.fgColor.rgb) : "";
          if (bg && bg.toLowerCase() !== "#ffffff") st += `background:${bg};`;
        }
        let txt = cell ? (cell.w != null ? cell.w : (cell.v != null ? String(cell.v) : "")) : "";
        if (cell && cell.t === "b" && cell.w == null) txt = cell.v ? "TRUE" : "FALSE";
        const link = cell && cell.l && cell.l.Target && /^(https?:|mailto:)/i.test(cell.l.Target) ? cell.l.Target : "";
        const inner = link ? `<a href="${escH(link)}" target="_blank" rel="noopener noreferrer">${escH(txt)}</a>` : escH(txt);
        h += `<td${sp ? ` rowspan="${sp[0]}" colspan="${sp[1]}"` : ""}${cls ? ` class="${cls}"` : ""}${st ? ` style="${st}"` : ""}${cell && cell.f ? ` title="=${escH(cell.f)}"` : ""}>${inner}</td>`;
      }
      h += `</tr>`;
    }
    h += `</tbody></table>`;
    if (r1 < r.e.r || c1 < r.e.c) h += `<div class="ofv-more">只显示前 ${r1 - r.s.r + 1} 行、${c1 - r.s.c + 1} 列，完整内容请下载或用 PDF 查看。</div>`;
    return h;
  }
  async function renderXlsx(el, buf) {
    const X = await libXlsx();
    if (!X || !X.read) throw new Error("SheetJS 不可用");
    const wb = X.read(new Uint8Array(buf), { type: "array", cellStyles: true, cellNF: true, cellDates: false, dense: false });
    const meta = (wb.Workbook && wb.Workbook.Sheets) || [];
    const names = wb.SheetNames.filter((n, i) => !(meta[i] && meta[i].Hidden));
    if (!names.length) throw new Error("没有工作表");
    el.innerHTML = `<div class="ofv-xls"><div class="ofv-sheet"></div>${names.length > 1 ? `<div class="ofv-tabs" role="tablist"></div>` : ""}</div>`;
    const sheet = el.querySelector(".ofv-sheet"), tabs = el.querySelector(".ofv-tabs");
    const show = (i) => {
      sheet.innerHTML = sheetHtml(X, wb.Sheets[names[i]]); sheet.scrollTop = 0; sheet.scrollLeft = 0;
      if (tabs) tabs.querySelectorAll("button").forEach((b, j) => { b.classList.toggle("on", i === j); b.setAttribute("aria-selected", i === j); });
    };
    if (tabs) {
      tabs.innerHTML = names.map((n, i) => `<button type="button" role="tab" data-i="${i}">${escH(n)}</button>`).join("");
      tabs.addEventListener("click", (e) => { const b = e.target.closest("button[data-i]"); if (b) show(+b.dataset.i); });
    }
    show(0);
    return null;
  }

  /* ---------------------------------------------------------------- PDF (server-side LibreOffice) */
  function pdfUrl(href) {
    return href.replace("/api/artifacts?", "/api/artifacts/preview?").replace(/([?&])download=1&?/, "$1").replace(/[?&]$/, "");
  }
  async function renderPdf(el, href, dlHref, name) {
    const url = pdfUrl(href);
    el.innerHTML = `<div class="ofv-wait"><div class="ofv-spin"></div><div>正在转换为打印版式（PDF），第一次打开可能要几十秒…</div></div>`;
    let r;
    try { r = await fetch(url); } catch (e) { r = null; }
    const ct = (r && r.headers.get("content-type")) || "";
    if (!r || !r.ok || !/pdf/i.test(ct)) {
      let msg = r ? "预览失败（" + r.status + "）" : "连不上服务器";
      try { const j = r && /json/i.test(ct) ? await r.json() : null; if (j && j.error) msg = j.error; } catch (e) { /* keep */ }
      el.innerHTML = `<div class="ofv-fail"><b>暂时无法预览这个文件</b><div>${escH(msg)}</div><a class="ofv-btn" href="${escH(dlHref)}" download="${escH(name)}">下载原文件</a></div>`;
      return false;
    }
    try { await r.arrayBuffer(); } catch (e) { /* the iframe loads it again from the cache */ }
    el.innerHTML = `<iframe class="ofv-pdf" title="${escH(name)}" src="${escH(url)}"></iframe>` +
      `<div class="ofv-pdfbar"><a href="${escH(url)}" target="_blank" rel="noopener">在新窗口打开 PDF</a></div>`;
    return true;
  }

  /**
   * Fill ``el`` with a preview of the Office file at ``href`` (/api/artifacts?...). ``opts.mode`` "direct" (default)
   * or "pdf". Returns {mode, refit} so the caller can offer the other view and re-fit on resize.
   */
  async function show(el, href, name, opts) {
    opts = opts || {};
    const dl = opts.download || href;
    const k = kindOf(name);
    el.classList.add("ofv");
    if (opts.mode === "pdf" || k === "pdf") { await renderPdf(el, href, dl, name); return { mode: "pdf", refit: null }; }
    el.innerHTML = `<div class="ofv-wait"><div class="ofv-spin"></div><div>正在打开…</div></div>`;
    const view = href.replace(/([?&])download=1&?/, "$1").replace(/[?&]$/, "");
    try {
      const r = await fetch(view);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const len = +(r.headers.get("content-length") || 0);
      if (len > DIRECT_MAX) throw new Error("too big");
      const buf = await r.arrayBuffer();
      if (buf.byteLength > DIRECT_MAX) throw new Error("too big");
      const refit = k === "docx" ? await renderDocx(el, buf) : await renderXlsx(el, buf);
      return { mode: "direct", refit };
    } catch (e) {
      // the renderer could not read it (old binary format saved as .docx, too big, broken file): print layout instead
      if (root.console) console.warn("office preview fallback to PDF:", e);
      await renderPdf(el, href, dl, name);
      return { mode: "pdf", refit: null };
    }
  }

  root.AiwOffice = { kindOf, show, pdfUrl };
})(window);
