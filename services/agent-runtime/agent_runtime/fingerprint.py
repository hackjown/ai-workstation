"""v3.9.26 (task-830): a feature fingerprint of each delivered file, so a fix cannot silently lose what worked.

task-830's restyle dropped the click-to-detail handlers and the GitHub section and was still marked succeeded. Every
send_file now stores a fingerprint per thread; on dev_fix done the new deliverable is compared with the previous one
and done is rejected naming exactly what disappeared (unless the user asked to remove it).

HTML (pure html.parser, no new dependency): size, element counts for key tags, data-* attributes grouped by name, ids
(and the ids of sections / dialogs / overlays), <script> count and bytes, a hash of the <style> text, on* handler
counts, embedded (data: url) font bytes, link hosts (github.com …). Other files: size + sha1.

This file is also run ON THE COMPUTER (remote_command embeds its own source in a python3 heredoc), so it imports only
the standard library.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from typing import Optional

KEY_TAGS = ("a", "section", "article", "button", "img", "details", "dialog", "nav", "header", "footer", "li", "table",
            "form", "input", "svg", "canvas", "iframe", "video")
SECTION_TAGS = {"section", "article", "nav", "header", "footer", "main", "aside", "dialog"}
SECTION_CLASS = re.compile(r"section|modal|overlay|detail|dialog|drawer|panel|popup", re.I)
_FONT_DATA = re.compile(r"url\(\s*['\"]?data:(?:font|application)/[\w.+-]*(?:;[\w=-]+)*;base64,([A-Za-z0-9+/=]+)", re.I)
_HOST = re.compile(r"^(?:https?:)?//([^/:?#]+)", re.I)


class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: dict = {}
        self.data_attrs: dict = {}
        self.ids: list = []
        self.section_ids: list = []
        self.handlers: dict = {}
        self.scripts = 0
        self.script_bytes = 0
        self.ext_scripts = 0
        self.styles: list = []
        self.hosts: dict = {}
        self._in = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in KEY_TAGS:
            self.tags[tag] = self.tags.get(tag, 0) + 1
        d = {k.lower(): (v or "") for k, v in attrs}
        for k in d:
            if k.startswith("data-"):
                self.data_attrs[k] = self.data_attrs.get(k, 0) + 1
            elif k.startswith("on") and len(k) > 2:
                self.handlers[k] = self.handlers.get(k, 0) + 1
        if d.get("id"):
            self.ids.append(d["id"])
            if tag in SECTION_TAGS or SECTION_CLASS.search(d.get("class", "")) or SECTION_CLASS.search(d["id"]):
                self.section_ids.append(f"{tag}#{d['id']}")
        for k in ("href", "src"):
            m = _HOST.match(d.get(k, "") or "")
            if m:
                h = m.group(1).lower()
                h = h[4:] if h.startswith("www.") else h
                self.hosts[h] = self.hosts.get(h, 0) + 1
        if tag == "script":
            self.scripts += 1
            if d.get("src"):
                self.ext_scripts += 1
            self._in = "script"
        elif tag == "style":
            self._in = "style"

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style"):
            self._in = None

    def handle_data(self, data):
        if self._in == "script":
            self.script_bytes += len(data)
        elif self._in == "style":
            self.styles.append(data)


def is_html(name: str, data: bytes) -> bool:
    if re.search(r"\.html?$", name or "", re.I):
        return True
    head = data[:600].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def fingerprint(data: bytes, name: str = "") -> dict:
    fp = {"name": name, "size": len(data), "sha1": hashlib.sha1(data).hexdigest(), "kind": "file"}
    if not is_html(name, data):
        return fp
    text = data.decode("utf-8", errors="replace")
    p = _P()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001  a broken page still gets size/hash
        pass
    css = "".join(p.styles)
    fonts = sum(len(m.group(1)) * 3 // 4 for m in _FONT_DATA.finditer(text))
    fp.update(kind="html", tags=p.tags, data_attrs=p.data_attrs, ids=len(p.ids), section_ids=sorted(set(p.section_ids))[:200],
              handlers=p.handlers, handlers_total=sum(p.handlers.values()) + len(re.findall(r"addEventListener\(", text)),
              scripts=p.scripts, ext_scripts=p.ext_scripts, script_bytes=p.script_bytes,
              style_hash=hashlib.sha1(css.encode()).hexdigest()[:12], style_bytes=len(css), font_bytes=fonts,
              hosts=dict(sorted(p.hosts.items(), key=lambda kv: -kv[1])[:30]))
    return fp


def _drastic(before: int, after: int) -> bool:
    return before > 0 and (after == 0 or (before >= 4 and after < before * 0.5))


def diff(old: Optional[dict], new: Optional[dict], allow: Optional[list] = None) -> list[dict]:
    """Features present in `old` but missing / drastically reduced in `new`: [{key, before, after, text}].
    `allow`: words the user explicitly asked to remove (a loss whose key contains one is not reported)."""
    if not old or not new:
        return []
    out: list = []
    if old.get("kind") == "html" and new.get("kind") == "html":
        for k, b in (old.get("data_attrs") or {}).items():
            a = (new.get("data_attrs") or {}).get(k, 0)
            if _drastic(b, a):
                out.append({"key": k, "before": b, "after": a, "text": f"{k} 属性 {b} → {a}"})
        gone = sorted(set(old.get("section_ids") or []) - set(new.get("section_ids") or []))
        for s in gone[:12]:
            out.append({"key": s, "before": 1, "after": 0, "text": f"区块 {s} 不见了"})
        for k, b in (old.get("handlers") or {}).items():
            a = (new.get("handlers") or {}).get(k, 0)
            if _drastic(b, a):
                out.append({"key": k, "before": b, "after": a, "text": f"{k} 事件处理 {b} → {a}"})
        b, a = int(old.get("handlers_total") or 0), int(new.get("handlers_total") or 0)
        if b >= 4 and a < b * 0.5 and not any(x["key"].startswith("on") for x in out):
            out.append({"key": "handlers", "before": b, "after": a, "text": f"交互事件 {b} → {a}"})
        b, a = int(old.get("scripts") or 0), int(new.get("scripts") or 0)
        if _drastic(b, a) or (b and a < b and int(new.get("script_bytes") or 0) < int(old.get("script_bytes") or 0) * 0.5):
            out.append({"key": "script", "before": b, "after": a, "text": f"<script> {b} → {a}（脚本 {old.get('script_bytes')} → "
                                                                          f"{new.get('script_bytes')} 字节）"})
        b, a = int(old.get("font_bytes") or 0), int(new.get("font_bytes") or 0)
        if b > 10000 and a < b * 0.1:
            out.append({"key": "font", "before": b, "after": a, "text": f"内嵌字体 {b // 1024}KB → {a // 1024}KB"})
        for h, b in (old.get("hosts") or {}).items():
            a = (new.get("hosts") or {}).get(h, 0)
            if _drastic(b, a) and (b >= 2 or a == 0):
                out.append({"key": h, "before": b, "after": a, "text": f"指向 {h} 的链接 {b} → {a}"})
        for t in ("section", "article", "details", "dialog", "button", "a", "img"):
            b, a = int((old.get("tags") or {}).get(t, 0)), int((new.get("tags") or {}).get(t, 0))
            if b >= 3 and _drastic(b, a):
                out.append({"key": f"<{t}>", "before": b, "after": a, "text": f"<{t}> 元素 {b} → {a}"})
    b, a = int(old.get("size") or 0), int(new.get("size") or 0)
    if b > 2000 and a < b * 0.4:
        out.append({"key": "size", "before": b, "after": a, "text": f"文件大小 {b // 1024}KB → {a // 1024}KB"})
    words = [w.lower() for w in (allow or []) if w]
    if words:
        out = [x for x in out if not any(w in x["key"].lower() or x["key"].lower().strip("<>#") in w for w in words)]
    return out


def summary(fp: Optional[dict]) -> str:
    if not fp:
        return ""
    if fp.get("kind") != "html":
        return f"{fp.get('name')}: {fp.get('size')} 字节"
    da = ", ".join(f"{k}×{v}" for k, v in sorted((fp.get("data_attrs") or {}).items(), key=lambda kv: -kv[1])[:8])
    return (f"{fp.get('name')}: {fp.get('size') // 1024}KB, script×{fp.get('scripts')}, 事件×{fp.get('handlers_total')}, "
            f"区块 {', '.join((fp.get('section_ids') or [])[:8]) or '-'}" + (f", {da}" if da else ""))


def must_keep(fp: Optional[dict]) -> list[str]:
    """Contract items from a baseline fingerprint: what the previous deliverable had."""
    if not fp or fp.get("kind") != "html":
        return []
    out = [f"{k}（{v} 处）" for k, v in sorted((fp.get("data_attrs") or {}).items(), key=lambda kv: -kv[1])[:8]]
    out += [f"区块 {s}" for s in (fp.get("section_ids") or [])[:10]]
    if fp.get("handlers_total"):
        out.append(f"交互事件（{fp['handlers_total']} 处）")
    if fp.get("font_bytes", 0) > 10000:
        out.append(f"内嵌字体（{fp['font_bytes'] // 1024}KB）")
    for h, n in list((fp.get("hosts") or {}).items())[:4]:
        if "github" in h:
            out.append(f"{h} 链接（{n} 处）")
    return out


def remote_command(path: str) -> str:
    """Shell: fingerprint `path` on the computer with this very file (stdlib only); prints one JSON line."""
    import inspect
    src = inspect.getsource(sys.modules[__name__]).split('\nif __name__ == "__main__" and len(sys.argv)')[0]
    q = path.replace("'", "'\"'\"'")
    return (f"P='{q}'; case \"$P\" in '~/'*) P=\"$HOME/${{P#\\~/}}\";; esac; python3 - \"$P\" <<'AIW_FP_EOF'\n{src}\n"
            "if __name__ == '__main__':\n    import os as _o\n    _p = sys.argv[1]\n"
            "    print(json.dumps(fingerprint(open(_p, 'rb').read(), _o.path.basename(_p)), ensure_ascii=False))\n"
            "AIW_FP_EOF")


if __name__ == "__main__" and len(sys.argv) > 1:   # python3 fingerprint.py FILE
    import os as _os
    print(json.dumps(fingerprint(open(sys.argv[1], "rb").read(), _os.path.basename(sys.argv[1])), ensure_ascii=False))
