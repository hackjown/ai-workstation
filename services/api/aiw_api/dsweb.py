"""v3.9.49: DeepSeek web tools - search, read a page and ask a document, on the API host, with no screen.

  web_search  POST https://api.deepseek.com/v1/tools/search  (query 1-128 chars, count 1-50, need_content)
  web_fetch   POST https://api.deepseek.com/v1/tools/fetch   (url -> {title, content} as Markdown)
  doc_ask     POST /files (purpose before file, expires_after = 1 h) -> chat/completions deepseek-flash with the
              file_id -> DELETE /files/{id}. Photos, scan packages and model files are never uploaded.

Docs (read 2026-11-15): api-docs.deepseek.com/guides/search, /guides/document_parsing, /guides/files_api,
/quick_start/pricing. Search and Fetch are $0.75 per 1,000 successful requests (Search: HTTP 200 with a non-empty
results array; Fetch: HTTP 200 with content); file parsing $0.225 per 1,000 pages; deepseek-flash tokens at the
peak price (the estimate errs high). 408 / 429 / 5xx are retried once; 402 = 余额不足.

The key (first that has one):
  1. kv web_tools.provider: the provider the user picked in 设置 › 联网工具
  2. a provider whose base_url host is api.deepseek.com
  3. the 默认 provider, only when its base_url is api.deepseek.com (another vendor's key is never sent to DeepSeek)
  A picked provider whose address is NOT api.deepseek.com (a relay / third-party gateway) is called at its own
  address: {base_url}/tools/search, /tools/fetch, /files, /chat/completions, with its own key and doc model.
  Its key never goes to api.deepseek.com, and "auto" never picks a third party.
  4. env DEEPSEEK_API_KEY
The provider's proxy is used for every call.

A page that is a login wall, a captcha / risk page or too short comes back as "FAILED: ..." with the advice to use
browser_open, so a task switches to the desktop browser instead of summarising a login form.

Settings  kv web_tools = {backend: deepseek | vm, provider: "", doc_ask: true, chat_fetch: true}
  backend vm switches all three tools off (tasks go back to the desktop browser).
Usage     table web_tools_usage, one row per call (day on the Asia/Shanghai clock).
API       GET/PUT /api/web-tools, POST /api/web-tools/test
CLI       python -m aiw_api.dsweb search "query" [--count 8] [--json]
          python -m aiw_api.dsweb fetch https://...  [--json]
          (key: the app's settings when its DB is readable - AIW_DB, else workspace/aiw.db, decrypted with the .secret_key
           next to it or AIW_SECRET_KEY - else DEEPSEEK_API_KEY)
          In the container: docker exec aiw-api sh -c 'cd /app && PYTHONPATH=services/api python -m aiw_api.dsweb search "今天 AI 新闻" --json'
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Optional

log = logging.getLogger("aiw.dsweb")

KV = "web_tools"
DEEPSEEK_HOST = "api.deepseek.com"
BASE = "https://api.deepseek.com"
DOC_MODEL = os.environ.get("AIW_DSWEB_DOC_MODEL", "deepseek-flash")
DEFAULTS = {"backend": "deepseek", "provider": "", "doc_ask": True, "chat_fetch": True, "doc_model": ""}
BACKENDS = ("deepseek", "vm")
TZ_OFFSET = 8 * 3600
MIN_CHARS = int(os.environ.get("AIW_DSWEB_MIN_CHARS", "200"))   # shorter page text = nothing to read
FILE_TTL = 3600   # the shortest lifetime the Files API accepts; the file is deleted right after the answer anyway

# USD, from the pricing page (2026-11-15)
PRICE_SEARCH = 0.75 / 1000
PRICE_FETCH = 0.75 / 1000
PRICE_PAGE = 0.225 / 1000
PRICE_IN = 0.30 / 1e6     # deepseek-flash input, cache miss, peak
PRICE_OUT = 1.20 / 1e6    # deepseek-flash output, peak

TRANSPORT = None   # tests: httpx.MockTransport

# ---------------------------------------------------------------------------------------------- settings / key


def cfg(store) -> dict:
    v = store.kv_get(KV, {}) or {}
    out = {**DEFAULTS, **{k: v[k] for k in DEFAULTS if k in v}}
    if out["backend"] not in BACKENDS:
        out["backend"] = "deepseek"
    return out


def _host(url: str) -> str:
    from .providers import host_of
    return host_of(url or "")


def resolve_key(store, secrets, env: Optional[dict] = None) -> Optional[dict]:
    """{key, source, provider, name, proxy} or None. source: picked | deepseek_provider | default | env."""
    from . import providers as pv
    env = os.environ if env is None else env
    c = cfg(store) if store is not None else dict(DEFAULTS)
    if store is not None:
        pid = str(c.get("provider") or "").strip()
        if pid:
            p = pv.find(store, pid) if pid != pv.DEFAULT_ID else None
            k = pv.conn(store, secrets, pid) if (pid == pv.DEFAULT_ID or (p and p.get("enabled") is not False)) else None
            if k and k.get("key") and not k.get("fell_back"):
                third = _host(k.get("base_url") or "") != DEEPSEEK_HOST
                return {"key": k["key"], "source": "picked", "provider": k["id"], "name": k.get("name") or k["id"],
                        "proxy": k.get("proxy") or "", "third_party": third,
                        "base": (k.get("base_url") or "").rstrip("/") if third else "",
                        "doc_model": str(c.get("doc_model") or "").strip()}
        for p in pv.stored(store):
            if p.get("enabled") is False or _host(p.get("base_url") or "") != DEEPSEEK_HOST:
                continue
            k = pv.conn(store, secrets, p["id"])
            if k.get("key"):
                return {"key": k["key"], "source": "deepseek_provider", "provider": p["id"],
                        "name": p.get("name") or p["id"], "proxy": k.get("proxy") or ""}
        d = pv.default_conn(store, secrets)
        if d.get("key") and _host(d.get("base_url") or "") == DEEPSEEK_HOST:
            return {"key": d["key"], "source": "default", "provider": pv.DEFAULT_ID, "name": pv.DEFAULT_NAME, "proxy": ""}
    k = (env.get("DEEPSEEK_API_KEY") or "").strip()
    if k:
        return {"key": k, "source": "env", "provider": "", "name": "DEEPSEEK_API_KEY", "proxy": ""}
    return None


def _url(k: dict, path: str) -> str:
    """path is the official one (/v1/tools/search, /files, /chat/completions). A third-party gateway gets it under
    its own base_url (which already carries its /v1): {base}/tools/search, {base}/files, {base}/chat/completions."""
    if k.get("third_party") and k.get("base"):
        return k["base"] + (path[3:] if path.startswith("/v1/") else path)
    return BASE + path


def _who(k: dict) -> str:
    return f"第三方「{k.get('name') or k.get('provider')}」" if k.get("third_party") else "DeepSeek"


def enabled(store, secrets) -> bool:
    """The three tools are offered only with backend deepseek and a key."""
    try:
        return cfg(store)["backend"] == "deepseek" and resolve_key(store, secrets) is not None
    except Exception as e:  # noqa: BLE001
        log.warning("web tools check: %s", e)
        return False


# ---------------------------------------------------------------------------------------------- usage

SCHEMA = """CREATE TABLE IF NOT EXISTS web_tools_usage (id TEXT PRIMARY KEY, day TEXT, ts REAL, kind TEXT, ok INTEGER,
  billable INTEGER, pages INTEGER, input INTEGER, output INTEGER, cost REAL, origin TEXT, err TEXT, task_id TEXT);
CREATE INDEX IF NOT EXISTS ix_webtools_day ON web_tools_usage(day);"""


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts + TZ_OFFSET))


def ensure_table(store) -> None:
    with store.lock:
        store.db.executescript(SCHEMA)
        store.db.commit()


def cost_of(kind: str, billable: bool, pages: int = 0, inp: int = 0, out: int = 0) -> float:
    if kind == "search":
        return PRICE_SEARCH if billable else 0.0
    if kind == "fetch":
        return PRICE_FETCH if billable else 0.0
    if kind == "doc":
        return (pages * PRICE_PAGE if billable else 0.0) + inp * PRICE_IN + out * PRICE_OUT
    return 0.0


def record(store, kind: str, ok: bool, billable: bool, origin: str = "", pages: int = 0, inp: int = 0, out: int = 0,
           err: str = "") -> None:
    if store is None:
        return
    try:
        ensure_table(store)
        now = time.time()
        tid = ""
        try:   # the task this call belongs to (sessions.py sets it for the token rows too)
            from llm_router import usage as _u
            tid = str(_u.TASK.get(None) or "") if origin == "task" else ""
        except Exception:  # noqa: BLE001
            pass
        store._x("INSERT INTO web_tools_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uuid.uuid4().hex[:16], day_of(now), now, kind, int(bool(ok)), int(bool(billable)), int(pages),
                  int(inp), int(out), round(cost_of(kind, billable, pages, inp, out), 6), (origin or "")[:20],
                  (err or "")[:200], tid[:60]))
    except Exception as e:  # noqa: BLE001  counting never breaks a call
        log.warning("web tools usage: %s", e)


def usage_summary(store, days: int = 14) -> dict:
    ensure_table(store)
    days = max(1, min(int(days or 14), 366))
    since = day_of(time.time() - (days - 1) * 86400)
    rows = store._x("SELECT day, kind, COUNT(*) AS calls, SUM(ok) AS ok, SUM(billable) AS billable, SUM(pages) AS pages, "
                    "SUM(input) AS input, SUM(output) AS output, SUM(cost) AS cost FROM web_tools_usage WHERE day>=? "
                    "GROUP BY day, kind ORDER BY day DESC", (since,))
    per_day: dict[str, dict] = {}
    for r in rows:
        d = per_day.setdefault(r["day"], {"day": r["day"], "search": 0, "fetch": 0, "doc": 0, "failed": 0, "cost": 0.0})
        d[r["kind"]] = d.get(r["kind"], 0) + (r["calls"] or 0)
        d["failed"] += (r["calls"] or 0) - (r["ok"] or 0)
        d["cost"] = round(d["cost"] + (r["cost"] or 0.0), 6)
    tot = {"search": 0, "fetch": 0, "doc": 0, "failed": 0, "cost": 0.0}
    for d in per_day.values():
        for k in tot:
            tot[k] = round(tot[k] + d[k], 6) if k == "cost" else tot[k] + d[k]
    today = per_day.get(day_of(time.time()), {"day": day_of(time.time()), "search": 0, "fetch": 0, "doc": 0,
                                              "failed": 0, "cost": 0.0})
    return {"days": days, "since": since, "today": today, "total": tot, "per_day": list(per_day.values()),
            "currency": "USD", "note": "费用是估算：搜索/读页按每千次成功 $0.75，文档按每千页 $0.225 加 deepseek-flash 高峰价"}


def task_usage(store, task_id: str) -> dict:
    """One task's web tool calls (scripts/web_compare.py)."""
    ensure_table(store)
    rows = store._x("SELECT kind, COUNT(*) AS calls, SUM(ok) AS ok, SUM(cost) AS cost FROM web_tools_usage WHERE task_id=? "
                    "GROUP BY kind", (task_id,))
    out = {"search": 0, "fetch": 0, "doc": 0, "failed": 0, "cost": 0.0}
    for r in rows:
        out[r["kind"]] = r["calls"] or 0
        out["failed"] += (r["calls"] or 0) - (r["ok"] or 0)
        out["cost"] = round(out["cost"] + (r["cost"] or 0.0), 6)
    return out


# ---------------------------------------------------------------------------------------------- HTTP


class DSError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


_ERR_CN = {400: "请求不对", 401: "DeepSeek API Key 无效", 402: "DeepSeek 余额不足，充值后再用",
           403: "这个网页被 DeepSeek 的安全策略拒绝", 404: "DeepSeek 读不到这个网页的正文",
           408: "请求超时", 413: "请求太大", 429: "DeepSeek 限流了", 500: "DeepSeek 内部错误", 503: "DeepSeek 忙",
           504: "DeepSeek 下游超时"}


def _err_text(status: int, body: str) -> str:
    msg = ""
    try:
        msg = (json.loads(body).get("error") or {}).get("message") or ""
    except Exception:  # noqa: BLE001
        msg = (body or "")[:200]
    base = _ERR_CN.get(status, f"HTTP {status}")
    return f"{base}（{status}{'：' + msg[:200] if msg else ''}）"


def _client(k: dict, timeout: float):
    import httpx
    kw: dict = {"timeout": timeout}
    if TRANSPORT is not None:
        kw["transport"] = TRANSPORT
    elif k.get("proxy"):
        kw["proxy"] = k["proxy"]
    return httpx.AsyncClient(**kw)


async def _post_json(k: dict, path: str, body: dict, timeout: float = 30) -> dict:
    last: Optional[DSError] = None
    for attempt in range(2):
        try:
            async with _client(k, timeout) as cl:
                r = await cl.post(_url(k, path), json=body,
                                  headers={"Authorization": f"Bearer {k['key']}", "Content-Type": "application/json"})
        except Exception as e:  # noqa: BLE001  network: one retry
            last = DSError(0, f"连不上 {_who(k)}：{type(e).__name__}: {str(e)[:150]}")
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise DSError(502, f"{_who(k)} 返回的不是 JSON（{r.headers.get('content-type', '?')}）：多半不支持这个接口")
            last = DSError(r.status_code, _err_text(r.status_code, r.text))
            if k.get("third_party") and r.status_code in (404, 405, 501):
                raise DSError(r.status_code, f"{_who(k)} 不支持 {path[3:] if path.startswith('/v1/') else path} 接口"
                                             f"（{r.status_code}）：它只转发了聊天模型。联网工具要 DeepSeek 官方 Key，或转发了这个接口的中转")
            if not (r.status_code in (408, 429) or r.status_code >= 500):
                raise last
        if attempt == 0:
            await asyncio.sleep(float(os.environ.get("AIW_DSWEB_RETRY_S", "1.5")))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------- search / fetch


def _need_key(store, secrets, key: Optional[dict]) -> dict:
    k = key or resolve_key(store, secrets)
    if not k:
        raise DSError(401, "没有 DeepSeek API Key：在 设置 › 模型 › 供应商 加一个 DeepSeek（https://api.deepseek.com），"
                           "或设环境变量 DEEPSEEK_API_KEY")
    return k


async def search(store, secrets, query: str, count: int = 8, ori_question: str = "", need_content: bool = False,
                 key: Optional[dict] = None, origin: str = "") -> list[dict]:
    q = re.sub(r"\s+", " ", str(query or "")).strip()[:128]
    if not q:
        raise DSError(400, "搜索词是空的")
    k = _need_key(store, secrets, key)
    body: dict[str, Any] = {"query": q, "count": max(1, min(int(count or 8), 50)), "need_content": bool(need_content)}
    oq = re.sub(r"\s+", " ", str(ori_question or "")).strip()[:128]
    if oq:
        body["ori_question"] = oq
    try:
        res = (await _post_json(k, "/v1/tools/search", body)).get("results") or []
    except DSError as e:
        record(store, "search", False, False, origin, err=e.message)
        raise
    record(store, "search", True, bool(res), origin)
    return res


_LOGIN = re.compile(r"(请|立即|账号|帐号|扫码|手机号|密码)\s*登[录陆]|登[录陆]\s*(后|以|查看|继续|注册)|login\s+to|"
                    r"log\s*in\s+to\s+continue|sign\s*in\s+to|please\s+(log|sign)\s*in|\bsign in\b.*\bpassword\b", re.I | re.S)
_WALL = re.compile(r"验证码|安全验证|人机验证|滑块|拖动.{0,6}(滑块|拼图)|访问(过于)?频繁|异常(访问|流量)|风控|captcha|"
                   r"are you a robot|verify (you are|you're) (a )?human|checking your browser|just a moment|"
                   r"access denied|unusual traffic|enable javascript", re.I)


def wall_reason(title: str, content: str) -> str:
    """Why a fetched page is not the page ('' = fine): a login wall, a captcha / risk check, or almost no text."""
    t, c = str(title or ""), str(content or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)", r"\1", c)   # markdown links/images -> their words
    plain = re.sub(r"[#>*_`|\-\s]+", " ", text).strip()
    head = (t + "\n" + plain[:1500])
    if _WALL.search(head) and len(plain) < 3000:
        return "验证/风控页"
    if _LOGIN.search(head) and len(plain) < 2500:
        return "登录页"
    if len(plain) < MIN_CHARS:
        return f"正文太短（{len(plain)} 字）"
    return ""


async def fetch(store, secrets, url: str, key: Optional[dict] = None, origin: str = "") -> dict:
    u = str(url or "").strip()
    if not re.match(r"^https?://[^/\s]+", u, re.I) or len(u) > 8192:
        raise DSError(400, "只能读 http(s) 网址")
    if re.match(r"^https?://[^/]*@", u, re.I):
        raise DSError(400, "网址里不能带用户名或密码")
    k = _need_key(store, secrets, key)
    try:
        r = await _post_json(k, "/v1/tools/fetch", {"url": u}, timeout=45)
    except DSError as e:
        record(store, "fetch", False, False, origin, err=e.message)
        raise
    title, content = str(r.get("title") or ""), str(r.get("content") or "")
    record(store, "fetch", True, bool(content), origin)
    return {"title": title, "content": content, "url": u, "blocked": wall_reason(title, content)}


def fmt_search(res: list[dict], query: str, limit_chars: int = 9000) -> str:
    if not res:
        return f"没有搜到「{query}」。换个说法或更短的关键词再试一次。"
    lines = [f"搜索「{query}」共 {len(res)} 条（DeepSeek）："]
    for i, r in enumerate(res, 1):
        date = (r.get("date") or "")[:10]
        site = r.get("site_name") or ""
        meta = " · ".join(x for x in (site, date) if x)
        body = (r.get("summary") or r.get("snippet") or "").strip()
        lines.append(f"{i}. {r.get('title') or ''}{'（' + meta + '）' if meta else ''}\n   {r.get('url') or ''}\n   {body[:700]}")
        if r.get("content"):
            lines.append("   正文：" + str(r["content"])[:1500])
    return "\n".join(lines)[:limit_chars]


def fmt_fetch(page: dict, limit_chars: int = 12000) -> str:
    if page.get("blocked"):
        return (f"FAILED: {page['blocked']}，DeepSeek 读到的不是正文（{page['url']}）。"
                "这个网页要登录、过验证或靠脚本加载：改用 browser_open 在桌面浏览器里打开。")
    return f"# {page.get('title') or page['url']}\n来源：{page['url']}\n\n{page['content'][:limit_chars]}"


# ---------------------------------------------------------------------------------------------- doc_ask

DOC_PARSE = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}                    # purpose text_recognition (billed per page)
DOC_TEXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".html", ".htm",
            ".xlsx", ".xls", ".epub", ".mobi", ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h",
            ".sh", ".sql", ".ini", ".toml", ".srt"}                       # purpose user_data (free)
PHOTO = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".raw", ".dng", ".cr2",
         ".nef", ".arw", ".svg", ".psd"}
PACKAGE = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
MODEL = {".usdz", ".usd", ".usda", ".usdc", ".glb", ".gltf", ".obj", ".stl", ".ply", ".fbx", ".3ds", ".blend", ".dwg",
         ".dxf", ".step", ".stp", ".iges", ".igs", ".3dm", ".skp", ".safetensors", ".ckpt", ".pt", ".pth", ".onnx",
         ".gguf", ".bin", ".h5", ".pb", ".tflite", ".mlmodel"}
PRIVATE_NAME = re.compile(r"身份证|护照|户口|驾驶证|驾照|行驶证|社保卡|银行卡|信用卡|结婚证|离婚证|出生证|病历|体检|"
                          r"扫描件|扫描|scan|id[ _-]?card|passport|license|银行流水|工资条", re.I)


def doc_refusal(path: str) -> str:
    """Why a file must not go to DeepSeek ('' = it may)."""
    name = os.path.basename(str(path or ""))
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO:
        return "这是照片/图片，不发给 DeepSeek（照片一律不外传）。要看图请用 read_image。"
    if ext in PACKAGE:
        return "这是压缩包/扫描包，不发给 DeepSeek。先在电脑上解压，再处理里面的文档。"
    if ext in MODEL:
        return "这是模型/图纸文件，不发给 DeepSeek。"
    if PRIVATE_NAME.search(name):
        return "文件名像证件、扫描件或隐私材料，不发给 DeepSeek。在电脑上本地处理。"
    if ext not in DOC_PARSE and ext not in DOC_TEXT:
        return f"不支持的文件类型（{ext or '无扩展名'}）：只问 PDF、Word、PPT、Excel 和文本文件。"
    return ""


def _pages(path: str) -> int:
    if not path.lower().endswith(".pdf"):
        return 1 if os.path.splitext(path)[1].lower() in DOC_PARSE else 0
    try:
        from pypdf import PdfReader
        return max(1, len(PdfReader(path).pages))
    except Exception:  # noqa: BLE001
        return max(1, os.path.getsize(path) // 60000)   # rough guess without pypdf


async def doc_ask(store, secrets, path: str, question: str, key: Optional[dict] = None, origin: str = "") -> dict:
    """Upload (1 h expiry) -> ask deepseek-flash -> delete. {answer, pages, input, output, file_deleted}."""
    why = doc_refusal(path)
    if why:
        raise DSError(400, why)
    if not os.path.isfile(path):
        raise DSError(404, "找不到这个文件")
    q = str(question or "").strip() or "总结这个文档的要点。"
    k = _need_key(store, secrets, key)
    ext = os.path.splitext(path)[1].lower()
    purpose = "text_recognition" if ext in DOC_PARSE else "user_data"
    pages = _pages(path) if purpose == "text_recognition" else 0
    fid = None
    deleted = False
    try:
        async with _client(k, 240) as cl:
            with open(path, "rb") as f:
                # purpose BEFORE file (documents get 400 otherwise); httpx sends data fields before files
                r = await cl.post(_url(k, "/files"), headers={"Authorization": f"Bearer {k['key']}"},
                                  data={"purpose": purpose, "expires_after[anchor]": "created_at",
                                        "expires_after[seconds]": str(FILE_TTL)},
                                  files={"file": (os.path.basename(path), f)})
            if r.status_code != 200:
                raise DSError(r.status_code, "上传失败：" + _err_text(r.status_code, r.text))
            fid = r.json().get("id")
            if not fid:
                raise DSError(500, "上传后没拿到 file_id")
            body = {"model": k.get("doc_model") or DOC_MODEL, "thinking": {"type": "disabled"},
                    "messages": [{"role": "system", "content": "只根据文档回答，用中文，引用页码或小节（有的话）。文档里没有的就说没有。"},
                                 {"role": "user", "content": [{"type": "file", "file_id": fid},
                                                              {"type": "text", "text": q[:4000]}]}]}
            r = await cl.post(_url(k, "/chat/completions"), json=body,
                              headers={"Authorization": f"Bearer {k['key']}", "Content-Type": "application/json"})
            if r.status_code != 200:
                raise DSError(r.status_code, "提问失败：" + _err_text(r.status_code, r.text))
            j = r.json()
            ans = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            u = j.get("usage") or {}
            inp, out = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    except DSError as e:
        record(store, "doc", False, fid is not None, origin, pages=pages if fid else 0, err=e.message)
        raise
    except Exception as e:  # noqa: BLE001
        record(store, "doc", False, fid is not None, origin, pages=pages if fid else 0, err=str(e))
        raise DSError(0, f"连不上 {_who(k)}：{type(e).__name__}: {str(e)[:150]}")
    finally:
        if fid:
            try:
                async with _client(k, 20) as cl:
                    d = await cl.delete(_url(k, f"/files/{fid}"), headers={"Authorization": f"Bearer {k['key']}"})
                deleted = d.status_code == 200
            except Exception as e:  # noqa: BLE001  it still expires in an hour
                log.warning("delete %s: %s", fid, e)
    record(store, "doc", True, True, origin, pages=pages, inp=inp, out=out)
    return {"answer": ans, "pages": pages, "input": inp, "output": out, "file_deleted": deleted}


# ---------------------------------------------------------------------------------------------- tools


def resolver(root: str) -> Callable[[str], Optional[str]]:
    """A path the user sent or a task produced, inside the artifacts folder (uploads are stored as <time>-<name>)."""
    rootr = os.path.realpath(root or "workspace/artifacts")

    def find(path: str) -> Optional[str]:
        p = str(path or "").strip()
        cands = [p] if os.path.isabs(p) else [os.path.join(rootr, p), os.path.join(rootr, "uploads", p)]
        for c in cands:
            real = os.path.realpath(c)
            if real.startswith(rootr + os.sep) and os.path.isfile(real):
                return real
        name = os.path.basename(p)
        up = os.path.join(rootr, "uploads")
        if name and os.path.isdir(up):
            for f in sorted(os.listdir(up), reverse=True):
                if f == name or f.endswith("-" + name):
                    return os.path.join(up, f)
        return None
    return find



def tools(store, secrets, origin: str = "task", resolve_path: Optional[Callable[[str], Optional[str]]] = None,
          doc: Optional[bool] = None) -> dict:
    """{name: (schema, async fn(args) -> str)} for the chat and for tasks; {} when switched off or keyless."""
    if not enabled(store, secrets):
        return {}
    c = cfg(store)

    async def _search(a: dict) -> str:
        q = str((a or {}).get("query") or "")
        try:
            res = await search(store, secrets, q, int((a or {}).get("count") or 8), str((a or {}).get("question") or ""),
                               bool((a or {}).get("need_content")), origin=origin)
        except DSError as e:
            return f"FAILED: 搜索失败，{e.message}"
        return fmt_search(res, q[:128])

    async def _fetch(a: dict) -> str:
        try:
            page = await fetch(store, secrets, str((a or {}).get("url") or ""), origin=origin)
        except DSError as e:
            hint = "改用 browser_open 在桌面浏览器里打开。" if origin == "task" else "可以交给电脑用浏览器打开。"
            return f"FAILED: {e.message}。{hint}"
        return fmt_fetch(page)

    async def _doc(a: dict) -> str:
        raw = str((a or {}).get("path") or "")
        why = doc_refusal(raw)
        if why:
            return "REFUSED: " + why
        real = resolve_path(raw) if resolve_path else (raw if os.path.isfile(raw) else None)
        if not real:
            return "找不到这个文件（给附件的文件名或路径）"
        try:
            r = await doc_ask(store, secrets, real, str((a or {}).get("question") or ""), origin=origin)
        except DSError as e:
            return f"FAILED: {e.message}"
        return r["answer"] + (f"\n\n（DeepSeek 读了 {r['pages']} 页；文件已从 DeepSeek 删除）" if r["file_deleted"]
                              else "\n\n（文件一小时后在 DeepSeek 过期）")

    out = {
        "web_search": ({"name": "web_search", "description":
                        "联网搜索（DeepSeek，服务器端，不开浏览器）。返回标题、网址、日期和摘要。支持 site:、-site:、\"精确词\"、"
                        "lang:zh、after:2026-01-01 这类写法。查资料、新闻、价格、文档时先用它",
                        "parameters": {"type": "object", "properties": {
                            "query": {"type": "string", "description": "搜索词，最多 128 字"},
                            "count": {"type": "integer", "description": "条数 1-50，默认 8"},
                            "question": {"type": "string", "description": "用户原话（可选，帮助理解）"},
                            "need_content": {"type": "boolean", "description": "要不要带正文（默认不要，需要细节时再用 web_fetch）"}},
                            "required": ["query"]}}, _search),
        "web_fetch": ({"name": "web_fetch", "description":
                       "读一个网页的正文（DeepSeek Fetch，返回 Markdown，不开浏览器）。遇到登录页、验证页或读不到正文会返回 "
                       "FAILED，那时改用浏览器",
                       "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
                      _fetch),
    }
    if (c.get("doc_ask", True) if doc is None else doc):
        out["doc_ask"] = ({"name": "doc_ask", "description":
                           "对一个文档文件提问（PDF、Word、PPT、Excel、txt/md/csv），由 DeepSeek 解析后回答，用完即删。"
                           "照片、扫描包、压缩包、模型和图纸文件、证件类文件不能用",
                           "parameters": {"type": "object", "properties": {
                               "path": {"type": "string", "description": "附件的文件名或路径"},
                               "question": {"type": "string", "description": "要问什么；留空就是总结要点"}},
                               "required": ["path"]}}, _doc)
    return out


TASK_RULES = (
    "\nWEB TOOLS (DeepSeek, run on the server, no screen): web_search finds pages, web_fetch reads one page as text, "
    "doc_ask asks a question about a document file. For looking things up, reading articles and summarising, use them "
    "FIRST - do not open the desktop browser for that. Switch to browser_open when web_fetch returns FAILED (login page, "
    "captcha, page built by scripts), or when the task needs logging in, clicking, filling a form, ordering, a "
    "screenshot or a specific app. Cite the URLs you used. Never pass photos, scan packages, archives, 3D/CAD model files "
    "or ID documents to doc_ask.")


# ---------------------------------------------------------------------------------------------- API


def public(store, secrets) -> dict:
    from . import providers as pv
    c = cfg(store)
    k = resolve_key(store, secrets)
    ds = [{"id": p["id"], "name": p.get("name") or p["id"]} for p in pv.stored(store)
          if p.get("enabled") is not False and _host(p.get("base_url") or "") == DEEPSEEK_HOST]
    d = pv.default_conn(store, secrets)
    allp = [{"id": pv.DEFAULT_ID, "name": pv.DEFAULT_NAME, "host": _host(d.get("base_url") or ""),
             "official": _host(d.get("base_url") or "") == DEEPSEEK_HOST}] + [
        {"id": p["id"], "name": p.get("name") or p["id"], "host": _host(p.get("base_url") or ""),
         "official": _host(p.get("base_url") or "") == DEEPSEEK_HOST}
        for p in pv.stored(store) if p.get("enabled") is not False]
    return {**c, "active": c["backend"] == "deepseek" and k is not None,
            "key": ({"source": k["source"], "provider": k["provider"], "name": k["name"],
                     "third_party": bool(k.get("third_party")), "host": _host(k.get("base") or BASE)} if k else None),
            "deepseek_providers": ds, "providers": allp, "usage": usage_summary(store, 14)}


def validate(body: dict, cur: dict) -> dict:
    out = dict(cur)
    if "backend" in body:
        b = str(body["backend"] or "").strip()
        if b not in BACKENDS:
            raise ValueError("backend 只能是 deepseek 或 vm")
        out["backend"] = b
    if "provider" in body:
        p = str(body["provider"] or "").strip()
        if p and not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", p):
            raise ValueError("供应商 id 不对")
        out["provider"] = p
    for f in ("doc_ask", "chat_fetch"):
        if f in body:
            out[f] = bool(body[f])
    if "doc_model" in body:
        m = str(body["doc_model"] or "").strip()
        if len(m) > 120 or any(ch.isspace() for ch in m):
            raise ValueError("文档模型名不对")
        out["doc_model"] = m
    return out


def mount(app, store, secrets, auth) -> None:
    from fastapi import Depends, HTTPException
    ensure_table(store)

    @app.get("/api/web-tools", dependencies=[Depends(auth)])
    async def get_web_tools(days: int = 14):
        p = public(store, secrets)
        if days != 14:
            p["usage"] = usage_summary(store, days)
        return p

    @app.put("/api/web-tools", dependencies=[Depends(auth)])
    async def put_web_tools(body: dict):
        try:
            new = validate(body or {}, cfg(store))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if new.get("provider"):
            from . import providers as pv
            if new["provider"] != pv.DEFAULT_ID and not pv.find(store, new["provider"]):
                raise HTTPException(404, "没有这个供应商")
        store.kv_set(KV, new)
        return public(store, secrets)

    @app.post("/api/web-tools/test", dependencies=[Depends(auth)])
    async def test_web_tools(body: Optional[dict] = None):
        """One search + one fetch (about $0.0015), each with its time. body.query / body.url override the defaults."""
        body = body or {}
        k = resolve_key(store, secrets)
        if not k:
            return {"ok": False, "error": "没有 DeepSeek API Key"}
        out: dict = {"key": {"source": k["source"], "name": k["name"], "third_party": bool(k.get("third_party"))}}
        t0 = time.time()
        try:
            res = await search(store, secrets, str(body.get("query") or "DeepSeek API"), 3, key=k, origin="test")
            out["search"] = {"ok": True, "count": len(res), "ms": int((time.time() - t0) * 1000),
                             "sample": [{"title": r.get("title"), "url": r.get("url")} for r in res[:3]]}
        except DSError as e:
            out["search"] = {"ok": False, "error": e.message, "ms": int((time.time() - t0) * 1000)}
        t0 = time.time()
        try:
            page = await fetch(store, secrets, str(body.get("url") or "https://api-docs.deepseek.com/"), key=k, origin="test")
            out["fetch"] = {"ok": not page["blocked"], "title": page["title"], "chars": len(page["content"]),
                            "blocked": page["blocked"], "ms": int((time.time() - t0) * 1000)}
        except DSError as e:
            out["fetch"] = {"ok": False, "error": e.message, "ms": int((time.time() - t0) * 1000)}
        out["ok"] = bool(out["search"].get("ok") and out["fetch"].get("ok"))
        return out


# ---------------------------------------------------------------------------------------------- CLI


def _cli_store():
    """The app's DB (AIW_DB, else workspace/aiw.db) and its key file let the CLI use the app's settings; otherwise
    only DEEPSEEK_API_KEY. Read-only use: the CLI never creates a key file."""
    db = os.environ.get("AIW_DB")
    if not db:   # the app's own default (config.Settings.db_path)
        from pathlib import Path
        db = str(Path(__file__).resolve().parents[3] / "workspace" / "aiw.db")
    if not os.path.isfile(db):
        return None, None
    try:
        from .store import Store
        st = Store(db)
        sec = None
        try:
            kd = os.path.dirname(os.path.abspath(db))
            if not os.environ.get("AIW_SECRET_KEY") and not os.path.isfile(os.path.join(kd, ".secret_key")):
                raise RuntimeError("no .secret_key next to the DB (never create one from the CLI)")
            from .crypto import SecretBox
            sec = SecretBox(st, kd)   # same key as the app (AIW_SECRET_KEY or .secret_key)
        except Exception as e:  # noqa: BLE001
            log.warning("cli secrets: %s", e)
        return st, sec
    except Exception as e:  # noqa: BLE001
        log.warning("cli store: %s", e)
        return None, None


def main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m aiw_api.dsweb", description="DeepSeek search / fetch (aiw_news.py)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--count", type=int, default=8)
    s.add_argument("--content", action="store_true", help="带正文")
    s.add_argument("--json", action="store_true")
    f = sub.add_parser("fetch")
    f.add_argument("url")
    f.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    st, sec = _cli_store()
    try:
        if a.cmd == "search":
            res = asyncio.run(search(st, sec, a.query, a.count, need_content=a.content, origin="cli"))
            print(json.dumps({"results": res}, ensure_ascii=False) if a.json else fmt_search(res, a.query, 10 ** 9))
        else:
            page = asyncio.run(fetch(st, sec, a.url, origin="cli"))
            print(json.dumps(page, ensure_ascii=False) if a.json else fmt_fetch(page, 10 ** 9))
            if page["blocked"]:
                return 3
    except DSError as e:
        print(json.dumps({"error": e.message, "status": e.status}, ensure_ascii=False) if getattr(a, "json", False)
              else f"FAILED: {e.message}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
