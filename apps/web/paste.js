/* v3.9.16 paste a screenshot (or any copied file) straight into the composer: ⌘V / Ctrl+V anywhere on the page, or drop it.
   Home chat (#hprompt) → hUpload (raw upload with progress), task view (#prompt) → uploadFiles. Text pastes are left
   alone (the textarea gets them as usual, and app.js's takeover paste still sends text to the computer). The clipboard
   names every screenshot "image.png", so each one is renamed 截图-<date>-<time>.png and several stay apart. */
(function () {
  function stamp(i) {
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}${i ? "-" + (i + 1) : ""}`;
  }
  function filesOf(dt) {
    if (!dt) return [];
    const out = [];
    for (const it of dt.items || []) if (it.kind === "file") { const f = it.getAsFile(); if (f) out.push(f); }
    if (!out.length) for (const f of dt.files || []) out.push(f);
    return out.map((f, i) => {
      const ext = ((f.type || "").split("/")[1] || "png").replace("jpeg", "jpg").replace(/[^a-z0-9]/gi, "") || "png";
      const generic = !f.name || /^image\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name);
      if (!generic) return f;
      try { return new File([f], `截图-${stamp(i)}.${ext}`, { type: f.type || "image/png" }); } catch (e) { return f; }
    });
  }
  function target(e) {
    const tg = e.target && e.target.closest ? e.target : null;
    if (tg && tg.closest("#hprompt, #hcomposer, .hcomp")) return "home";
    if (tg && tg.closest("#prompt, #composer")) return "task";
    if (tg && tg.closest("input, textarea, [contenteditable=true], #modal, .overlay")) return null;   // some other box
    if (S.view === "home" && document.getElementById("hprompt")) return "home";
    if (S.view === "chat" && document.getElementById("prompt")) return "task";
    return null;
  }
  window.aiwPasteFiles = function (files, where) {
    if (!files.length) return false;
    const pics = files.filter((f) => (f.type || "").startsWith("image")).length;
    toast(pics === files.length ? `已粘贴 ${pics} 张截图，正在上传…` : `已粘贴 ${files.length} 个文件，正在上传…`);
    if (where === "home" && typeof hUpload === "function") hUpload(files);
    else if (typeof uploadFiles === "function") uploadFiles(files);
    const box = document.getElementById(where === "home" ? "hprompt" : "prompt");
    if (box) box.focus();
    return true;
  };
  document.addEventListener("paste", (e) => {
    const files = filesOf(e.clipboardData);
    if (!files.length) return;               // plain text: the browser (and the takeover paste) handle it
    const where = target(e);
    if (!where) return;
    e.preventDefault(); e.stopImmediatePropagation();
    window.aiwPasteFiles(files, where);
  }, true);
  // dropping files from Finder / Explorer onto the page works the same way
  document.addEventListener("dragover", (e) => { if (e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files") && target(e)) e.preventDefault(); });
  document.addEventListener("drop", (e) => {
    const files = filesOf(e.dataTransfer);
    const where = files.length && target(e);
    if (!where) return;
    e.preventDefault();
    window.aiwPasteFiles(files, where);
  });
})();
