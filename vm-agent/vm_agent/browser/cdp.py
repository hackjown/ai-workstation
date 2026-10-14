"""Find page elements through Chrome's DevTools protocol, then click them with the REAL mouse.

The DOM knows exactly where "注册" or the "密码" field is, so no vision model has to guess pixels.
The click itself still goes through xdotool, so the user watching noVNC sees the pointer move and click.

Needs Chrome started with --remote-debugging-port=9222 and a non-default --user-data-dir
(Chrome 136+ refuses remote debugging on the default profile). APP_COMMANDS["chrome"] does both.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
from typing import Optional

CDP_HTTP = os.environ.get("VM_AGENT_CDP", "http://127.0.0.1:9222")

FIND_JS = r"""
(function(q, mode){
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const Q = norm(q);
  const vis = el => { const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el); return st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity || '1') > 0.05; };
  const labelsOf = el => { const p = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('title'),
      el.getAttribute('alt'), el.getAttribute('name'), el.id];
    if (el.tagName === 'INPUT' && ['button','submit','reset'].includes(el.type)) p.unshift(el.value);
    if (el.labels) for (const l of el.labels) p.push(l.innerText);
    return p; };
  const score = texts => { let s = 0; for (const t of texts) { const n = norm(t); if (!n) continue;
      if (n === Q) s = Math.max(s, 100); else if (n.startsWith(Q)) s = Math.max(s, 70);
      else if (n.includes(Q)) s = Math.max(s, 50 - Math.min(20, (n.length - Q.length) / 4));
      else if (Q.includes(n) && n.length >= 2) s = Math.max(s, 30); } return s; };
  const area = el => { const r = el.getBoundingClientRect(); return r.width * r.height; };
  const FILL = 'input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]),textarea,select,[contenteditable=""],[contenteditable=true]';
  const CLICK = 'a,button,input,select,summary,label,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=option],[onclick],[tabindex]';
  const sel = mode === 'fill' ? FILL : CLICK;
  let best = null, bestScore = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    const t = labelsOf(el); if (mode !== 'fill') t.push(el.innerText);
    let s = score(t);
    if (s > bestScore || (s === bestScore && s > 0 && best && area(el) < area(best))) { best = el; bestScore = s; }
  }
  if (mode === 'fill' && bestScore < 50) {
    for (const lab of document.querySelectorAll('label,span,div,p,td,th,dt,legend')) {
      if (lab.children.length > 3 || !vis(lab)) continue;
      const s = score([lab.innerText]); if (s < 50 || s <= bestScore) continue;
      let f = lab.querySelector(FILL) || (lab.htmlFor && document.getElementById(lab.htmlFor));
      if (!f && lab.nextElementSibling && lab.nextElementSibling.matches(FILL)) f = lab.nextElementSibling;
      if (!f && lab.parentElement) f = lab.parentElement.querySelector(FILL);
      if (f && vis(f)) { best = f; bestScore = s; }
    }
  }
  if (mode !== 'fill' && bestScore < 50) {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
    while ((n = w.nextNode())) { const p = n.parentElement; if (!p || !vis(p)) continue;
      const s = score([n.textContent]); if (s > bestScore) { bestScore = s; best = p.closest(CLICK) || p; } }
  }
  if (!best || bestScore < 30) {
    const c = []; for (const el of document.querySelectorAll(sel)) { if (!vis(el)) continue;
      const t = ((labelsOf(el).find(x => x && x.trim())) || (mode !== 'fill' ? el.innerText : '') || '').trim().replace(/\s+/g, ' ').slice(0, 30);
      if (t && !c.includes(t)) c.push(t); if (c.length >= 40) break; }
    return JSON.stringify({found: false, candidates: c, url: location.href});
  }
  best.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = best.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const top = document.elementFromPoint(cx, cy);
  const covered = !!top && top !== best && !best.contains(top) && !top.contains(best);
  window.__aiwTarget = best;
  return JSON.stringify({found: true, score: bestScore, tag: best.tagName.toLowerCase(), type: best.type || null,
    text: (best.innerText || best.value || best.getAttribute('aria-label') || best.getAttribute('placeholder') || '').trim().slice(0, 60),
    cx, cy, sx: window.screenX, sy: window.screenY, ow: window.outerWidth, oh: window.outerHeight,
    iw: window.innerWidth, ih: window.innerHeight, dpr: window.devicePixelRatio, covered,
    url: location.href, title: document.title});
})
"""


def screen_point(info: dict) -> tuple[int, int]:
    """Viewport CSS pixels -> X11 screen pixels. The chrome above the page (tabs, address bar) is
    outerHeight - innerHeight; side borders split outerWidth - innerWidth."""
    dpr = float(info.get("dpr") or 1)
    x = info["sx"] + max(0, info["ow"] - info["iw"]) / 2 + info["cx"]
    y = info["sy"] + max(0, info["oh"] - info["ih"]) + info["cy"]
    return int(round(x * dpr)), int(round(y * dpr))


class CDP:
    def __init__(self, http: str = CDP_HTTP):
        self.http = http.rstrip("/")
        self._ids = itertools.count(1)

    async def available(self) -> bool:
        try:
            await self._pages()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _pages(self) -> list[dict]:
        import httpx
        async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
            r = await c.get(self.http + "/json/list")
            r.raise_for_status()
            return [p for p in r.json() if p.get("type") == "page" and p.get("webSocketDebuggerUrl")
                    and not p.get("url", "").startswith(("devtools://", "chrome-extension://"))]

    async def _call(self, ws, method: str, params: Optional[dict] = None) -> dict:
        i = next(self._ids)
        await ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    async def _eval(self, ws, expr: str):
        r = await self._call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError("page script error: " + str(r["exceptionDetails"].get("text"))[:200])
        return (r.get("result") or {}).get("value")

    async def session(self):
        """websocket to the tab the user is looking at (visible + focused wins)."""
        import websockets
        pages = await self._pages()
        if not pages:
            raise RuntimeError("Chrome has no open tab")
        chosen = pages[0]
        for p in pages[:8]:
            try:
                async with websockets.connect(p["webSocketDebuggerUrl"], max_size=None, open_timeout=3) as ws:
                    v = await self._eval(ws, "document.visibilityState + ':' + document.hasFocus()")
                if v and v.startswith("visible"):
                    chosen = p
                    if v.endswith("true"):
                        break
            except Exception:  # noqa: BLE001
                continue
        return websockets.connect(chosen["webSocketDebuggerUrl"], max_size=None, open_timeout=3)

    async def find(self, ws, query: str, mode: str) -> dict:
        raw = await self._eval(ws, f"({FIND_JS})({json.dumps(query)}, {json.dumps(mode)})")
        info = json.loads(raw or "{}")
        if not info.get("found"):
            cands = "、".join(info.get("candidates") or []) or "（页面上没有可见的可操作元素）"
            what = "输入框" if mode == "fill" else "可点击元素"
            raise LookupError(f"页面上没有找到「{query}」这个{what}。可见的{what}有：{cands}")
        return info


async def click(backend, query: str, cdp: Optional[CDP] = None) -> dict:
    cdp = cdp or CDP()
    async with await cdp.session() as ws:
        info = await cdp.find(ws, query, "click")
        await asyncio.sleep(0.15)  # after scrollIntoView
        info = await cdp.find(ws, query, "click")
        x, y = screen_point(info)
        await backend.click(x, y)
    return {"clicked": info["text"] or query, "tag": info["tag"], "x": x, "y": y, "covered": info["covered"],
            "url": info["url"], "via": "dom"}


async def fill(backend, field: str, text: str, submit: bool = False, cdp: Optional[CDP] = None) -> dict:
    cdp = cdp or CDP()
    async with await cdp.session() as ws:
        info = await cdp.find(ws, field, "fill")
        if info["tag"] == "select":
            ok = await cdp._eval(ws, "(function(t){const s=window.__aiwTarget;for(const o of s.options){if(o.text.trim()===t||o.value===t)"
                                     "{s.value=o.value;s.dispatchEvent(new Event('input',{bubbles:true}));s.dispatchEvent(new Event('change',{bubbles:true}));return true}}return false})"
                                     f"({json.dumps(text)})")
            if not ok:
                raise LookupError(f"下拉框「{field}」里没有选项「{text}」")
            return {"field": field, "selected": text, "via": "dom"}
        await asyncio.sleep(0.15)
        info = await cdp.find(ws, field, "fill")
        x, y = screen_point(info)
        await backend.click(x, y)
        await asyncio.sleep(0.1)
        await backend.key("ctrl+a")
        await backend.key("BackSpace")
        await backend.type(text)
        got = await cdp._eval(ws, "(function(){const e=window.__aiwTarget||document.activeElement;"
                                  "return (e && ('value' in e ? e.value : e.innerText)) || ''})()")
        method = "keyboard"
        if (got or "") != text:  # IME / xdotool lost characters: insert the text directly into the focused field
            await cdp._eval(ws, "(function(){const e=window.__aiwTarget||document.activeElement; if(!e) return;"
                                "e.focus(); if(e.select) e.select(); else document.execCommand('selectAll');})()")
            await cdp._call(ws, "Input.insertText", {"text": text})
            method = "insertText"
        if submit:
            await backend.key("Return")
    shown = "•" * len(text) if info.get("type") == "password" else text
    return {"field": field, "typed": shown, "x": x, "y": y, "via": "dom", "method": method}
