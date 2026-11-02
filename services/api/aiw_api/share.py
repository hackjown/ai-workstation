"""3.9.13 只读分享链接：把 artifacts 里的一个 HTML（装修设计器、全景漫游）或一个带 index.html 的文件夹，
用随机短码发给别人，不用登录就能打开。

POST   /api/shares {path, days=30, view_only=true, title}  → {code, url, qr_svg, expires}
GET    /api/shares                                        → 列表（需登录）
DELETE /api/shares/{code}                                 → 立即作废
GET    /s/{code}                                          → 公开：页面本身（设计器在只看模式下打开）
GET    /s/{code}/{file}                                   → 公开：分享的是文件夹时，里面的图片等

存在 artifacts/.shares.json；过期、作废、文件被删的链接返回 404 / 410。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from html import escape
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

_LOCK = threading.Lock()

# 只看模式：设计器打开时锁定 3D 拖动，藏起会改方案的按钮，顶部一条说明。不改方案文件本身，刷新也一样。
VIEW_ONLY = """<script>try{localStorage.setItem("hd-lock3d","1")}catch(e){}window.__AIW_VIEW_ONLY__=1</script>
<style id="aiw-view-only">
#toolSeg,#undoBtn,#redoBtn,#saveBtn,#schemeBtn,#moreMenu,#leftToggle,#leftPanel,#lockBtn,#quoteBtn,#dimInput{display:none!important}
#aiwVoBar{position:fixed;left:50%;top:6px;transform:translateX(-50%);z-index:99999;background:rgba(20,24,32,.82);color:#fff;
font:13px/1.4 "PingFang SC","Microsoft YaHei",sans-serif;padding:5px 12px;border-radius:14px;pointer-events:none}
</style>"""
VIEW_ONLY_BAR = '<div id="aiwVoBar">只看模式 · 可以看 2D / 3D、漫游，不能修改</div>'


def _db_path(root: str) -> str:
    return os.path.join(root, ".shares.json")


def _load(root: str) -> dict:
    try:
        with open(_db_path(root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(root: str, db: dict) -> None:
    tmp = _db_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _db_path(root))


def _inside(root: str, path: str) -> Optional[str]:
    real = os.path.realpath(path)
    base = os.path.realpath(root)
    return real if real.startswith(base + os.sep) and os.path.exists(real) else None


def qr_svg(text: str) -> Optional[str]:
    try:
        import segno
    except ImportError:
        return None
    return segno.make(text, error="m").svg_inline(scale=6, border=2, dark="#111", light="#fff")


def public_base(request: Request) -> str:
    env = os.environ.get("AIW_PUBLIC_URL", "").rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def is_designer(html_head: str) -> bool:
    return "house-designer" in html_head or "__PRESET__" in html_head


def create(root: str, path: str, days: float = 30, view_only: bool = True, title: str = "") -> dict:
    real = _inside(root, path)
    if not real:
        raise HTTPException(404, "文件不在产物目录里")
    if os.path.isdir(real):
        if not os.path.isfile(os.path.join(real, "index.html")):
            raise HTTPException(400, "文件夹里没有 index.html")
    elif not real.lower().endswith((".html", ".htm")):
        raise HTTPException(400, "只能分享网页（.html）或带 index.html 的文件夹")
    code = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")
    now = time.time()
    rec = {"path": real, "created": now, "expires": now + days * 86400 if days and days > 0 else None,
           "view_only": bool(view_only), "title": title or os.path.splitext(os.path.basename(real.rstrip(os.sep)))[0],
           "views": 0, "revoked": False}
    with _LOCK:
        db = _load(root)
        db[code] = rec
        _save(root, db)
    return {"code": code, **rec}


def _get(root: str, code: str) -> dict:
    with _LOCK:
        rec = _load(root).get(code)
    if not rec:
        raise HTTPException(404, "链接不存在")
    if rec.get("revoked"):
        raise HTTPException(410, "链接已作废")
    if rec.get("expires") and time.time() > rec["expires"]:
        raise HTTPException(410, "链接已过期")
    if not os.path.exists(rec["path"]):
        raise HTTPException(410, "分享的文件已被删除")
    return rec


def _count(root: str, code: str) -> None:
    with _LOCK:
        db = _load(root)
        if code in db:
            db[code]["views"] = db[code].get("views", 0) + 1
            db[code]["last_view"] = time.time()
            _save(root, db)


def _page(rec: dict) -> HTMLResponse:
    p = rec["path"]
    f = os.path.join(p, "index.html") if os.path.isdir(p) else p
    with open(f, encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    if rec.get("view_only") and is_designer(html[:400000]):
        i = html.lower().find("<head>")
        html = (html[:i + 6] + VIEW_ONLY + html[i + 6:]) if i >= 0 else VIEW_ONLY + html
        j = html.lower().rfind("</body>")
        html = (html[:j] + VIEW_ONLY_BAR + html[j:]) if j >= 0 else html + VIEW_ONLY_BAR
    return HTMLResponse(html, headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex",
                                       "Referrer-Policy": "no-referrer"})


def _gone(msg: str, status: int) -> HTMLResponse:
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        f'<body style="font:16px sans-serif;text-align:center;padding:80px 20px;color:#555">{escape(msg)}</body>',
                        status_code=status)


def mount(app, settings, auth_dep) -> None:
    from fastapi import Depends
    root = settings.artifacts_dir

    @app.post("/api/shares", dependencies=[Depends(auth_dep)])
    async def share_create(request: Request, body: dict):
        rec = create(root, str(body.get("path") or ""), float(body.get("days") if body.get("days") is not None else 30),
                     bool(body.get("view_only", True)), str(body.get("title") or ""))
        url = f"{public_base(request)}/s/{rec['code']}"
        if os.path.isdir(rec["path"]):
            url += "/"
        return {**{k: v for k, v in rec.items() if k != "path"}, "url": url, "qr_svg": qr_svg(url),
                "file": os.path.basename(rec["path"].rstrip(os.sep))}

    @app.get("/api/shares", dependencies=[Depends(auth_dep)])
    async def share_list(request: Request, path: str = ""):
        with _LOCK:
            db = _load(root)
        base = public_base(request)
        rows = []
        for code, r in sorted(db.items(), key=lambda kv: -kv[1].get("created", 0)):
            if path and os.path.realpath(path) != r["path"]:
                continue
            alive = not r.get("revoked") and not (r.get("expires") and time.time() > r["expires"]) and os.path.exists(r["path"])
            rows.append({"code": code, "url": f"{base}/s/{code}" + ("/" if os.path.isdir(r["path"]) else ""), "alive": alive,
                         "file": os.path.basename(r["path"].rstrip(os.sep)),
                         **{k: r.get(k) for k in ("title", "created", "expires", "view_only", "views", "last_view", "revoked")}})
        return rows

    @app.delete("/api/shares/{code}", dependencies=[Depends(auth_dep)])
    async def share_revoke(code: str):
        with _LOCK:
            db = _load(root)
            if code not in db:
                raise HTTPException(404)
            db[code]["revoked"] = True
            _save(root, db)
        return {"ok": True}

    @app.get("/s/{code}")
    async def share_open(code: str, request: Request):
        try:
            rec = _get(root, code)
        except HTTPException as e:
            return _gone(str(e.detail), e.status_code)
        if os.path.isdir(rec["path"]) and not request.url.path.endswith("/"):
            # relative image paths in index.html need the trailing slash
            from fastapi.responses import RedirectResponse
            return RedirectResponse(f"/s/{code}/", status_code=307)
        _count(root, code)
        return _page(rec)

    @app.get("/s/{code}/{file:path}")
    async def share_file(code: str, file: str):
        try:
            rec = _get(root, code)
        except HTTPException as e:
            return _gone(str(e.detail), e.status_code)
        if not file or file == "index.html":
            _count(root, code)
            return _page(rec)
        if not os.path.isdir(rec["path"]):
            raise HTTPException(404)
        real = os.path.realpath(os.path.join(rec["path"], file))
        if not real.startswith(rec["path"] + os.sep) or not os.path.isfile(real) or os.path.basename(real).startswith("."):
            raise HTTPException(404)
        return FileResponse(real, headers={"Cache-Control": "public, max-age=86400"})

    app.state.shares_root = root

