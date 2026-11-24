"""v3.9.85: Cue's images / videos are copied to AIW and shown in the reply; the web desktop has a 全屏 button."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
sys.path.insert(0, str(ROOT / "services" / "agent-runtime"))
from aiw_api import bots as B  # noqa: E402
from aiw_api import cue_engine as CE  # noqa: E402


def test_collect_and_kinds():
    sink = []
    am = {"content": "好了 ![猫](/home/ubuntu/cat.png)", "attachments": [
        {"type": "image", "filename": "cat.png", "url": "https://x/cat.png?sig=1", "path": "/home/ubuntu/cat.png",
         "content_type": "image/png", "version_uid": "v1"},
        {"type": "file", "filename": "a.mp4", "url": "https://x/a.mp4", "content_type": "video/mp4"},
        {"type": "file", "filename": "bad", "url": "file:///x"}]}
    CE._collect_media(am, sink)
    CE._collect_media(am, sink)
    assert [x["filename"] for x in sink] == ["cat.png", "a.mp4"]
    assert B.media_kind("cat.png", "image/png") == "image" and B.media_kind("a.mp4", "video/mp4") == "video"
    t = B.media_text(am["content"], [{"src": "/home/ubuntu/cat.png"}])
    assert "cat.png" not in t and t.startswith("好了")


def test_save_media(tmp_path, monkeypatch):
    import httpx

    def handler(req):
        return httpx.Response(200, content=b"PNGDATA")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    b = B.Bots.__new__(B.Bots)
    b.artifacts_dir = str(tmp_path)
    out = asyncio.run(b.save_media("c1", [{"filename": "cat.png", "url": "https://x/cat.png", "content_type": "image/png"}]))
    assert out[0]["kind"] == "image" and Path(out[0]["path"]).read_bytes() == b"PNGDATA"


def test_web():
    js = (ROOT / "apps/web/bots.js").read_text(encoding="utf-8")
    html = (ROOT / "apps/web/index.html").read_text(encoding="utf-8")
    assert "function btMedia" in js and "deskFull('desk')" in html and "deskFull('deskBig')" in html
