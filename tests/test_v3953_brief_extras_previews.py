"""v3.9.53: 早报 gets 全球 AI 要闻 and a GitHub 新榜 that does not depend on the brief's own script (it came back
empty on 2026-11-18), and a photo / video the user sends shows as a preview in the chat (web + iOS)."""
import asyncio
import json
import os

import pytest

from aiw_api import brief_extras as B

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TRENDING = """<html><body>
<article class="Box-row"><div class="float-right"><a href="/sponsors/x">Sponsor</a></div>
<h2 class="h3 lh-condensed"><a data-x="1" href="/elementalsouls/Claude-BugHunter" class="Link"><svg></svg>
<span class="text-normal"> elementalsouls / </span> Claude-BugHunter</a></h2>
<p class="col-9 color-fg-muted my-1 tmp-pr-4"> A Claude Code skill bundle &amp; more. </p>
<div class="f6"><span itemprop="programmingLanguage">Python</span>
<a href="/elementalsouls/Claude-BugHunter/stargazers" class="Link"><svg></svg> 10,361</a>
<span class="d-inline-block float-sm-right"><svg></svg> 2,322 stars today</span></div></article>
<article class="Box-row"><h2 class="h3"><a href="/cursor/plugins"><span>cursor / </span> plugins</a></h2>
<div class="f6"><a href="/cursor/plugins/stargazers"><svg></svg> 17,046</a> 315 stars today</div></article>
</body></html>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>英伟达扩大芯片供应链合作</title><link>https://36kr.com/p/1?f=rss</link><pubDate>Wed, 18 Nov 2026 10:00:00 +0800</pubDate><description>&lt;p&gt;算力&lt;/p&gt;</description></item>
<item><title>某公司完成超亿元A轮融资</title><link>https://36kr.com/p/2</link><pubDate>Wed, 18 Nov 2026 09:00:00 +0800</pubDate><description>高温合金</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Suno now lets you make AI videos</title><link rel="alternate" href="https://www.theverge.com/a"/>
<published>2026-11-18T01:00:00-05:00</published><summary>music</summary></entry></feed>"""


def test_parse_trending():
    items = B.parse_trending(TRENDING)
    assert [i["repo"] for i in items] == ["elementalsouls/Claude-BugHunter", "cursor/plugins"]
    a = items[0]
    assert a["stars"] == 10361 and a["today"] == 2322 and a["lang"] == "Python" and a["desc"] == "A Claude Code skill bundle & more."
    assert items[1]["today"] == 315 and items[1]["stars"] == 17046


def test_needs_trending_counts_repo_links():
    empty = '<h2>GitHub 新榜</h2><p>暂无数据</p><a href="https://github.com/trending">trending</a>'
    assert B.needs_trending(empty)
    full = "".join(f'<a href="https://github.com/o{i}/r{i}">r</a>' for i in range(5))
    assert not B.needs_trending(full)
    assert not B.needs_trending(empty + '<section id="aiw-trending"></section>')


def test_parse_feeds_rss_atom_and_ai_filter():
    rs = B.parse_feed(RSS, "36氪", False)
    assert [r["title"] for r in rs] == ["英伟达扩大芯片供应链合作"]          # the funding round is not AI
    assert rs[0]["url"] == "https://36kr.com/p/1" and rs[0]["at"]
    at = B.parse_feed(ATOM, "The Verge", True)
    assert at and at[0]["url"] == "https://www.theverge.com/a" and at[0]["at"]
    assert B.parse_feed("not xml", "x", True) == []


class FakeLLM:
    def __init__(self, out):
        self.out = out

    async def complete(self, system, user, max_tokens):
        assert "全球 AI" in system and "[0]" in user
        return self.out


def test_pick_uses_model_and_falls_back():
    items = [{"title": f"t{i}", "url": f"https://x/{i}", "source": "A" if i % 2 else "B", "at": 100 - i, "summary": ""}
             for i in range(12)]
    out = asyncio.run(B.pick(items, FakeLLM('好的：[{"i": 3, "title": "中文三", "line": "一句话"}, {"i": 99, "title": "x"}]')))
    assert len(out) == 1 and out[0]["zh"] == "中文三" and out[0]["url"] == "https://x/3"
    raw = asyncio.run(B.pick(items, FakeLLM("not json")))
    assert len(raw) == 10 and {raw[0]["source"], raw[1]["source"]} == {"A", "B"}


def test_inject_after_weather_once(tmp_path):
    p = tmp_path / "2026-11-18.html"
    p.write_text('<html><body><section id="aiw-weather">天气</section><h1>早报</h1></body></html>', encoding="utf-8")
    ai = {"items": [{"title": "Suno AI video", "zh": "Suno 可以做 AI 视频了", "line": "音乐配视频", "url": "https://v/a",
                     "source": "The Verge", "at": None}], "failed": []}
    tr = {"items": B.parse_trending(TRENDING), "via": "trending", "error": ""}
    assert B.inject(str(p), [("aiw-ai-news", B.ai_html(ai)), ("aiw-trending", B.trending_html(tr))]) == ["aiw-ai-news", "aiw-trending"]
    page = p.read_text(encoding="utf-8")
    assert page.index("aiw-weather") < page.index("aiw-ai-news") < page.index("aiw-trending") < page.index("<h1>")
    assert "Suno 可以做 AI 视频了" in page and "今日 +2,322★" in page and "elementalsouls/Claude-BugHunter" in page
    assert B.inject(str(p), [("aiw-ai-news", B.ai_html(ai))]) == []        # once


def test_enrich_skips_trending_when_page_has_it(tmp_path, monkeypatch):
    calls = []

    async def fake_ai(store=None, secrets=None, llm=None):
        calls.append("ai")
        return {"items": [], "failed": ["量子位"], "found": 0}

    async def fake_tr(n=15, timeout=20.0):
        calls.append("tr")
        return {"items": [], "via": "", "error": "trending 页面读不到"}

    monkeypatch.setattr(B, "ai_news", fake_ai)
    monkeypatch.setattr(B, "trending", fake_tr)
    good = tmp_path / "a.html"
    good.write_text("<body>" + "".join(f'<a href="https://github.com/o{i}/r{i}">r</a>' for i in range(4)) + "</body>", encoding="utf-8")
    r = asyncio.run(B.enrich([str(good)]))
    assert calls == ["ai"] and r["added"][str(good)] == ["aiw-ai-news"] and r["md"] == ""
    assert "量子位" in good.read_text(encoding="utf-8")
    calls.clear()
    r = asyncio.run(B.enrich([]))          # no page: both, for the chat text
    assert calls == ["ai", "tr"]


def test_trending_falls_back_to_search(monkeypatch):
    class R:
        def __init__(self, code, text="", js=None):
            self.status_code, self.text, self._js = code, text, js

        def json(self):
            return self._js

    async def fake_get(cl, url, **kw):
        if url == B.TRENDING_URL:
            return None
        assert kw["params"]["q"].startswith("created:>")
        return R(200, js={"items": [{"full_name": "a/b", "html_url": "https://github.com/a/b", "description": "d",
                                     "language": "Go", "stargazers_count": 900}]})

    async def no_sleep(s):
        return None

    monkeypatch.setattr(B, "_get", fake_get)
    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)
    d = asyncio.run(B.trending())
    assert d["via"] == "search" and d["items"][0]["repo"] == "a/b" and "trending" in d["error"]
    assert "近 7 天" in B.trending_html(d)


def test_web_and_ios_show_previews():
    js = open(os.path.join(ROOT, "apps/web/home.js"), encoding="utf-8").read()
    assert "function hAttPreview" in js and "const att = hAttPreview(d.attachments || []);" in js
    assert "<video controls playsinline" in js
    sw = open(os.path.join(ROOT, "apps/ios/Sources/HomeV2.swift"), encoding="utf-8").read()
    assert "AttachmentPreviews(attachments: attachments)" in sw
    pv = open(os.path.join(ROOT, "apps/ios/Sources/AttachmentPreview.swift"), encoding="utf-8").read()
    assert "AVAssetImageGenerator" in pv and "VideoPlayer(player:" in pv
    main = open(os.path.join(ROOT, "services/api/aiw_api/main.py"), encoding="utf-8").read()
    assert '"video/mp4" if not download and real.lower().endswith((".mov", ".m4v"))' in main
    chat = open(os.path.join(ROOT, "services/api/aiw_api/chat.py"), encoding="utf-8").read()
    assert "brief_extras as _bx" in chat
