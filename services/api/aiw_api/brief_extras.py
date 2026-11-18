"""v3.9.53: two sections the 7:30 新闻早报 page gets from the API host itself, the same way as its weather.

1. 全球 AI 要闻: the last ~36 hours of AI news from several feeds (量子位, 36氪 filtered to AI, MIT Technology Review AI,
   The Verge AI, TechCrunch AI, Ars Technica AI, Hacker News front page filtered to AI) plus a DeepSeek search when
   联网工具 has a key. The small model picks the ten that matter and writes each as a Chinese headline + one line;
   without a model the raw titles go in.
2. GitHub 新榜: the brief's own script came back with nothing on 2026-11-18. When the page holds fewer than three
   github.com/<owner>/<repo> links, the API host reads https://github.com/trending (daily) itself; when that fails,
   the GitHub search API (repos created in the last 7 days, by stars).

Both go in as <section id="aiw-ai-news"> / <section id="aiw-trending"> right after the weather (once each), and a
short text version goes into the chat message when the run made no html.
Env: AIW_BRIEF_AI_NEWS=0 / AIW_BRIEF_TRENDING=0 turn either off. httpx honours HTTPS_PROXY for hosts that need one.
"""
from __future__ import annotations

import asyncio
import html as _h
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("aiw.brief")
TZ = ZoneInfo("Asia/Shanghai")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Safari/605.1.15", "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6"}

# (source name, url, every item is AI news?)
FEEDS = [
    ("量子位", "https://www.qbitai.com/feed", True),
    ("36氪", "https://www.36kr.com/feed", False),
    ("MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", True),
    ("The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", True),
    ("TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", True),
    ("Ars Technica", "https://arstechnica.com/ai/feed/", True),
]
HN_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=60"
AI_RE = re.compile(r"\bAI\b|A\.I\.|人工智能|大模型|模型|智能体|机器人|具身|算力|GPU|芯片|英伟达|Nvidia|OpenAI|ChatGPT|GPT-?\d|"
                   r"Anthropic|Claude|Gemini|DeepMind|Google AI|Llama|Meta AI|Mistral|DeepSeek|Qwen|通义|千问|文心|豆包|Kimi|月之暗面|"
                   r"智谱|GLM|MiniMax|xAI|Grok|Copilot|LLM|AGI|agent|diffusion|transformer|neural|machine learning|"
                   r"推理模型|生成式|AIGC|Sora|Midjourney|Hugging ?Face", re.I)
TRENDING_URL = "https://github.com/trending?since=daily"
GH_SEARCH = "https://api.github.com/search/repositories"
REPO_LINK = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
SKIP_OWNERS = {"trending", "topics", "sponsors", "login", "features", "about", "orgs", "settings", "marketplace", "apps"}


def on(kind: str) -> bool:
    return os.environ.get({"ai": "AIW_BRIEF_AI_NEWS", "trending": "AIW_BRIEF_TRENDING"}[kind], "1") != "0"


def _text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", _h.unescape(s)).strip()


def _when(raw: str) -> Optional[float]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(re.sub(r"\s+", " ", raw.replace("Z", "+0000") if fmt.endswith("%z") else raw), fmt)
            return (d if d.tzinfo else d.replace(tzinfo=ZoneInfo("UTC"))).timestamp()
        except ValueError:
            continue
    return None


def parse_feed(xml: str, source: str, all_ai: bool) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, url, source, at, summary}] (only AI items when the feed is not an AI feed)."""
    out = []
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return out
    ns_atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{ns_atom}entry")
    for it in items:
        def g(tag):
            e = it.find(tag)
            if e is None:
                e = it.find(ns_atom + tag)
            return e
        t = g("title")
        title = _text(t.text if t is not None and t.text else "")
        link_el = g("link")
        url = ""
        if link_el is not None:
            url = (link_el.text or "").strip() or link_el.get("href", "")
        when = None
        for tag in ("pubDate", "published", "updated", "{http://purl.org/dc/elements/1.1/}date"):
            e = g(tag)
            if e is not None and e.text:
                when = _when(e.text)
                if when:
                    break
        d = g("description")
        if d is None:
            d = g("summary")
        summ = _text(d.text if d is not None and d.text else "")[:300]
        if not title or not url:
            continue
        if not all_ai and not AI_RE.search(title + " " + summ[:120]):
            continue
        out.append({"title": title[:200], "url": url.split("?f=rss")[0], "source": source, "at": when, "summary": summ})
    return out


async def _get(cl: httpx.AsyncClient, url: str, **kw) -> Optional[httpx.Response]:
    try:
        r = await cl.get(url, **kw)
        if r.status_code == 200:
            return r
        log.info("brief source %s: HTTP %s", url, r.status_code)
    except Exception as e:  # noqa: BLE001
        log.info("brief source %s: %s", url, type(e).__name__)
    return None


async def gather_ai(store=None, secrets=None, hours: int = 36, timeout: float = 15.0) -> tuple[list[dict], list[str]]:
    """Raw AI items from every source, newest first, de-duplicated; plus the names of sources that failed."""
    cutoff = time.time() - hours * 3600
    failed: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, headers=UA, follow_redirects=True) as cl:
        rs = await asyncio.gather(*[_get(cl, u) for _, u, _ in FEEDS], _get(cl, HN_URL))
    items: list[dict] = []
    for (name, _, all_ai), r in zip(FEEDS, rs[:-1]):
        got = parse_feed(r.text, name, all_ai) if r is not None else []
        if r is None:
            failed.append(name)
        items += got
    hn = rs[-1]
    if hn is not None:
        try:
            for h in hn.json().get("hits") or []:
                t = str(h.get("title") or "")
                if t and AI_RE.search(t):
                    items.append({"title": t[:200], "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                                  "source": "Hacker News", "at": float(h.get("created_at_i") or 0) or None, "summary": "",
                                  "points": int(h.get("points") or 0)})
        except ValueError:
            failed.append("Hacker News")
    else:
        failed.append("Hacker News")
    if store is not None and secrets is not None:   # DeepSeek search, when 联网工具 has a key: Chinese coverage
        try:
            from . import dsweb as _ds
            if _ds.enabled(store, secrets):
                today = datetime.now(TZ).strftime("%Y年%m月%d日")
                for res in await _ds.search(store, secrets, f"{today} 全球 人工智能 AI 重要新闻", count=10, origin="brief"):
                    t, u = str(res.get("title") or ""), str(res.get("url") or "")
                    if t and u:
                        items.append({"title": t[:200], "url": u, "source": str(res.get("site_name") or _host(u)), "at": None,
                                      "summary": _text(str(res.get("summary") or res.get("snippet") or ""))[:300]})
        except Exception as e:  # noqa: BLE001
            failed.append("DeepSeek 搜索")
            log.info("brief ds search: %s", e)
    seen, out = set(), []
    for it in items:
        if it.get("at") and it["at"] < cutoff:
            continue
        key = re.sub(r"\W+", "", it["title"].lower())[:40]
        if key in seen or it["url"] in seen:
            continue
        seen.update((key, it["url"]))
        out.append(it)
    out.sort(key=lambda x: x.get("at") or 0, reverse=True)
    return out, failed


def _host(u: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", u or "")
    return m.group(1) if m else ""


PICK_SYSTEM = ("你是科技编辑。从候选新闻里挑出对关注全球 AI 的中文读者最重要的 10 条（大模型/产品发布、公司与融资、政策监管、"
               "芯片算力、研究突破、行业应用），同一事件只留一条，中外都要有。只输出 JSON 数组，每项 "
               '{"i": 候选编号, "title": "中文标题（不超过 30 字）", "line": "一句话说清楚发生了什么、为什么重要（不超过 60 字）"}。'
               "不要编造候选里没有的事实。")


async def pick(items: list[dict], llm=None, n: int = 10) -> list[dict]:
    """The small model's ten, in Chinese; without a model (or when it fails) the newest n, as they are."""
    cand = items[:60]
    if llm is not None and cand:
        lines = [f"[{i}] {c['source']} | {c['title']}" + (f" | {c['summary'][:120]}" if c.get("summary") else "")
                 for i, c in enumerate(cand)]
        try:
            raw = await asyncio.wait_for(llm.complete(PICK_SYSTEM, "\n".join(lines), 1500), 60)
            m = re.search(r"\[.*\]", str(raw or ""), re.S)
            arr = json.loads(m.group(0)) if m else []
            out = []
            for x in arr:
                i = int(x.get("i", -1))
                if 0 <= i < len(cand) and x.get("title"):
                    out.append({**cand[i], "zh": str(x["title"])[:60], "line": str(x.get("line") or "")[:120]})
            if out:
                return out[:n]
        except Exception as e:  # noqa: BLE001
            log.info("brief ai pick: %s", e)
    # no model: prefer one per source first, then the rest by time
    per, rest, seen = [], [], set()
    for c in cand:
        (rest if c["source"] in seen else per).append(c)
        seen.add(c["source"])
    return (per + rest)[:n]


async def ai_news(store=None, secrets=None, llm=None) -> dict:
    items, failed = await gather_ai(store, secrets)
    return {"items": await pick(items, llm), "failed": failed, "found": len(items)}


# ------------------------------------------------------------------ GitHub 新榜
def parse_trending(page: str) -> list[dict]:
    out = []
    for a in re.findall(r'<article class="Box-row">(.*?)</article>', page or "", re.S):
        m = re.search(r'<h2[^>]*>.*?href="/([^"/]+)/([^"/]+)"', a, re.S)
        if not m:
            continue
        desc = re.search(r'<p class="col-9[^"]*">(.*?)</p>', a, re.S)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', a)
        stars = re.search(r'/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', a, re.S)
        today = re.search(r"([\d,]+)\s+stars?\s+today", a)
        out.append({"repo": f"{m.group(1)}/{m.group(2)}", "url": f"https://github.com/{m.group(1)}/{m.group(2)}",
                    "desc": _text(desc.group(1)) if desc else "", "lang": lang.group(1).strip() if lang else "",
                    "stars": int(stars.group(1).replace(",", "")) if stars else None,
                    "today": int(today.group(1).replace(",", "")) if today else None})
    return out


async def trending(n: int = 15, timeout: float = 20.0) -> dict:
    """{"items", "via": "trending" | "search", "error"}: today's trending; else repos made this week, by stars."""
    errs = []
    async with httpx.AsyncClient(timeout=timeout, headers=UA, follow_redirects=True) as cl:
        for wait in (0, 3):
            if wait:
                await asyncio.sleep(wait)
            r = await _get(cl, TRENDING_URL)
            items = parse_trending(r.text) if r is not None else []
            if items:
                return {"items": items[:n], "via": "trending", "error": ""}
            errs.append("trending 页面" + ("读不到" if r is None else "解析为空"))
        since = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
        hdr = {"Accept": "application/vnd.github+json"}
        tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if tok:
            hdr["Authorization"] = f"Bearer {tok}"
        r = await _get(cl, GH_SEARCH, params={"q": f"created:>{since}", "sort": "stars", "order": "desc", "per_page": n},
                       headers=hdr)
        if r is not None:
            try:
                items = [{"repo": x["full_name"], "url": x["html_url"], "desc": (x.get("description") or "")[:200],
                          "lang": x.get("language") or "", "stars": x.get("stargazers_count"), "today": None}
                         for x in r.json().get("items") or []]
                if items:
                    return {"items": items[:n], "via": "search", "error": "；".join(errs)}
            except (ValueError, KeyError):
                pass
        errs.append("GitHub 搜索 API 读不到")
    return {"items": [], "via": "", "error": "；".join(errs)}


def repo_links(page: str) -> int:
    return len({f"{a}/{b}".lower() for a, b in REPO_LINK.findall(page or "") if a.lower() not in SKIP_OWNERS})


def needs_trending(page: str) -> bool:
    """The brief has no usable GitHub list: fewer than three repo links on the whole page."""
    return 'id="aiw-trending"' not in page and repo_links(page) < 3


# ------------------------------------------------------------------ rendering
_SEC = ('<section id="{id}" style="margin:16px auto;max-width:960px;padding:16px 20px;border-radius:14px;'
        'background:{bg};color:#123;font-family:-apple-system,system-ui,sans-serif">{body}</section>')


def _hm(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%m-%d %H:%M") if ts else ""


def ai_html(d: dict) -> str:
    e = _h.escape
    rows = []
    for i, it in enumerate(d.get("items") or [], 1):
        title = it.get("zh") or it["title"]
        orig = f'<div style="font-size:13px;color:#667">{e(it["title"])}</div>' if it.get("zh") and it["zh"] != it["title"] else ""
        line = f'<div style="margin-top:2px">{e(it["line"])}</div>' if it.get("line") else ""
        meta = " · ".join(x for x in (it.get("source"), _hm(it.get("at"))) if x)
        rows.append(f'<li style="margin:0 0 12px"><a href="{e(it["url"])}" target="_blank" rel="noopener" '
                    f'style="font-weight:600;color:#0b5cad;text-decoration:none">{i}. {e(title)}</a>{orig}{line}'
                    f'<div style="font-size:12px;color:#889">{e(meta)}</div></li>')
    body = '<h2 style="margin:0 0 10px;font-size:20px">🌍 全球 AI 要闻</h2>'
    body += (f'<ul style="list-style:none;margin:0;padding:0;line-height:1.6">{"".join(rows)}</ul>' if rows
             else '<p style="margin:4px 0">（今天没读到 AI 新闻源' + (f"：{e('、'.join(d.get('failed') or []))}" if d.get("failed") else "") + "）</p>")
    return _SEC.format(id="aiw-ai-news", bg="#f3f0ff", body=body)


def trending_html(d: dict) -> str:
    e = _h.escape
    head = "🔥 GitHub 新榜" + ("（今日 Trending）" if d.get("via") == "trending" else "（近 7 天新建仓库 · 按 star）" if d.get("via") else "")
    rows = []
    for i, it in enumerate(d.get("items") or [], 1):
        nums = []
        if it.get("today") is not None:
            nums.append(f"今日 +{it['today']:,}★")
        if it.get("stars") is not None:
            nums.append(f"共 {it['stars']:,}★")
        if it.get("lang"):
            nums.append(it["lang"])
        rows.append(f'<li style="margin:0 0 10px"><a href="{e(it["url"])}" target="_blank" rel="noopener" '
                    f'style="font-weight:600;color:#0b5cad;text-decoration:none">{i}. {e(it["repo"])}</a>'
                    f'<span style="font-size:12px;color:#889"> · {e(" · ".join(nums))}</span>'
                    + (f'<div>{e(it["desc"])}</div>' if it.get("desc") else "") + "</li>")
    body = f'<h2 style="margin:0 0 10px;font-size:20px">{head}</h2>'
    body += (f'<ul style="list-style:none;margin:0;padding:0;line-height:1.55">{"".join(rows)}</ul>' if rows
             else f'<p style="margin:4px 0">（GitHub 榜单今天没读到：{e(d.get("error") or "未知原因")}）</p>')
    return _SEC.format(id="aiw-trending", bg="#effaf3", body=body)


def ai_md(d: dict, n: int = 8) -> str:
    items = (d.get("items") or [])[:n]
    if not items:
        return ""
    return "### 🌍 全球 AI 要闻\n" + "\n".join(
        f"{i}. [{it.get('zh') or it['title']}]({it['url']})" + (f"：{it['line']}" if it.get("line") else "")
        for i, it in enumerate(items, 1))


def trending_md(d: dict, n: int = 10) -> str:
    items = (d.get("items") or [])[:n]
    if not items:
        return ""
    return "### 🔥 GitHub 新榜\n" + "\n".join(
        f"{i}. [{it['repo']}]({it['url']})" + (f"（今日 +{it['today']}★）" if it.get("today") is not None else "")
        + (f"：{it['desc'][:60]}" if it.get("desc") else "") for i, it in enumerate(items, 1))


def inject(path: str, blocks: list[tuple[str, str]]) -> list[str]:
    """Put each (id, html) block in once, after the weather section (or right after <body>). Returns the ids added."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            page = f.read()
    except OSError as e:
        log.warning("brief extras %s: %s", path, e)
        return []
    add = [(i, b) for i, b in blocks if b and f'id="{i}"' not in page]
    if not add:
        return []
    chunk = "".join(b for _, b in add)
    m = re.search(r'<section id="aiw-weather".*?</section>', page, re.S) or re.search(r"<body[^>]*>", page, re.I)
    page = page[:m.end()] + chunk + page[m.end():] if m else chunk + page
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
    except OSError as e:
        log.warning("brief extras %s: %s", path, e)
        return []
    return [i for i, _ in add]


async def enrich(paths: list[str], store=None, secrets=None, llm=None) -> dict:
    """The brief's html pages get 全球 AI 要闻 (always) and GitHub 新榜 (when they have none). Returns what was added
    and the markdown to put in the chat when no page took it."""
    pages = [p for p in paths if p.lower().endswith((".html", ".htm")) and os.path.isfile(p)]
    texts = {}
    for p in pages:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                texts[p] = f.read()
        except OSError:
            pass
    want_ai = on("ai") and not all('id="aiw-ai-news"' in t for t in texts.values()) if texts else on("ai")
    want_tr = on("trending") and (any(needs_trending(t) for t in texts.values()) if texts else True)
    jobs = [ai_news(store, secrets, llm) if want_ai else asyncio.sleep(0, None),
            trending() if want_tr else asyncio.sleep(0, None)]
    ai, tr = await asyncio.gather(*jobs, return_exceptions=True)
    ai = ai if isinstance(ai, dict) else None
    tr = tr if isinstance(tr, dict) else None
    added: dict[str, list[str]] = {}
    for p, t in texts.items():
        blocks = []
        if ai is not None:
            blocks.append(("aiw-ai-news", ai_html(ai)))
        if tr is not None and needs_trending(t):
            blocks.append(("aiw-trending", trending_html(tr)))
        added[p] = inject(p, blocks)
    md = []
    if not texts:   # no page: the chat message carries them
        if ai:
            md.append(ai_md(ai))
        if tr:
            md.append(trending_md(tr))
    return {"added": added, "md": "\n\n".join(x for x in md if x), "ai": ai, "trending": tr}
