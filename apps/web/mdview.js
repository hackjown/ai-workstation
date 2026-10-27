/* AIW file preview renderer, shared by the web app (mdpreview.js) and the iOS app (mdview.html in a WKWebView).
   Markdown -> markdown-it (GFM tables, fenced code; raw HTML off) -> DOMPurify. Fenced code blocks get a copy button,
   tables sit in a horizontally scrolling wrapper. JSON is pretty-printed monospace, other text is shown as is.
   Needs vendor/markdown/markdown-it.min.js and vendor/markdown/purify.min.js loaded first. */
(function (root) {
  "use strict";
  let md = null;
  const escH = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const COPY = "复制", COPIED = "已复制";

  function engine() {
    if (md) return md;
    md = root.markdownit({ html: false, linkify: true, breaks: true, typographer: false });
    md.renderer.rules.fence = (tokens, idx) => {
      const t = tokens[idx];
      const lang = (t.info || "").trim().split(/\s+/)[0];
      return `<div class="mdv-code"><div class="mdv-code-h"><span>${escH(lang || "代码")}</span><button type="button" class="mdv-copy">${COPY}</button></div>` +
        `<pre><code>${escH(t.content.replace(/\n$/, ""))}</code></pre></div>\n`;
    };
    md.renderer.rules.code_block = md.renderer.rules.fence;
    md.renderer.rules.table_open = () => `<div class="mdv-tw"><table>\n`;
    md.renderer.rules.table_close = () => `</table></div>\n`;
    if (root.DOMPurify && !root.DOMPurify.__aiw) {
      root.DOMPurify.addHook("afterSanitizeAttributes", (n) => {
        if (n.tagName === "A" && n.getAttribute("href")) { n.setAttribute("target", "_blank"); n.setAttribute("rel", "noopener noreferrer"); }
        if (n.tagName === "IMG" && !/^(https?:|data:image\/)/i.test(n.getAttribute("src") || "")) n.removeAttribute("src");
      });
      root.DOMPurify.__aiw = true;
    }
    return md;
  }

  function kindOf(name) {
    const ext = String(name || "").toLowerCase().split(".").pop();
    if (ext === "md" || ext === "markdown" || ext === "mdown") return "md";
    if (ext === "json" || ext === "jsonl") return "json";
    if (["txt", "log", "csv", "tsv", "yaml", "yml", "ini", "toml", "py", "js", "sh", "xml"].includes(ext)) return "text";
    return "";
  }

  function render(text, kind) {
    text = String(text == null ? "" : text).replace(/^\uFEFF/, "");
    if (kind === "json") {
      let out = text;
      try { out = JSON.stringify(JSON.parse(text), null, 2); } catch (e) {
        // JSON Lines: pretty-print each line that parses
        try { out = text.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.stringify(JSON.parse(l), null, 2)).join("\n\n"); } catch (e2) { out = text; }
      }
      return `<div class="mdv-code mdv-raw"><div class="mdv-code-h"><span>JSON</span><button type="button" class="mdv-copy">${COPY}</button></div><pre><code>${escH(out)}</code></pre></div>`;
    }
    if (kind === "text") return `<pre class="mdv-plain">${escH(text)}</pre>`;
    const html = engine().render(text);
    return root.DOMPurify ? root.DOMPurify.sanitize(html, { ADD_ATTR: ["target"] }) : escH(text);
  }

  function copyText(text) {
    try { if (root.webkit && root.webkit.messageHandlers && root.webkit.messageHandlers.aiwCopy) { root.webkit.messageHandlers.aiwCopy.postMessage(text); return Promise.resolve(); } } catch (e) { /* fall through */ }
    if (navigator.clipboard && root.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise((ok, bad) => {
      const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy") ? ok() : bad(new Error("copy failed")); } catch (e) { bad(e); } finally { ta.remove(); }
    });
  }

  // one delegated handler per container: copy buttons
  function wire(el) {
    if (el.__mdvWired) return; el.__mdvWired = true;
    el.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest(".mdv-copy"); if (!b || !el.contains(b)) return;
      e.preventDefault();
      const code = b.closest(".mdv-code").querySelector("code");
      copyText(code ? code.textContent : "").then(() => { b.textContent = COPIED; b.classList.add("ok"); setTimeout(() => { b.textContent = COPY; b.classList.remove("ok"); }, 1600); },
        () => { b.textContent = "复制失败"; setTimeout(() => { b.textContent = COPY; }, 1600); });
    });
  }

  function show(el, text, kind) {
    el.classList.add("mdv"); el.classList.toggle("mdv-mono", kind !== "md");
    el.innerHTML = render(text, kind || "md");
    // tables: short cells stay on one line, long ones get a readable width; the wrapper scrolls sideways
    el.querySelectorAll(".mdv-tw td, .mdv-tw th").forEach((c) => { c.classList.add(c.textContent.trim().length <= 14 ? "mdv-nw" : "mdv-long"); });
    wire(el);
  }

  root.AiwMd = { render, show, kindOf, copyText };
})(typeof window !== "undefined" ? window : this);
