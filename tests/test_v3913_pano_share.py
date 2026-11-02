"""3.9.13: 全景漫游 + 只读分享链接.

* house-plan-designer: `aiw_hpd.py pano` (pano.mjs renders one 360° picture per room, door hotspots, minimap) and
  `aiw_hpd.py pano-photos` (the user's own 2:1 panoramas) both write assets/pano-viewer.html with the scenes inlined.
* /api/shares + /s/<code>: a link anyone can open, designer pages in 只看模式, folders serve their images, revoke / expiry.
* web (mdpreview.js 分享) and iOS (HtmlPreview.swift) have the share button.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SK = ROOT / "skills" / "house-plan-designer"


def _app(tmp_path):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    os.makedirs(s.artifacts_dir, exist_ok=True)
    return TestClient(build_app(s)), s


H = {"Authorization": "Bearer t"}


def test_share_designer_view_only_and_revoke(tmp_path):
    c, s = _app(tmp_path)
    f = Path(s.artifacts_dir) / "家-装修设计器.html"
    f.write_text('<!doctype html><html><head><meta charset="utf-8"><script>window.__PRESET__={}</script></head><body>'
                 '<script>localStorage.getItem("house-designer-v2-plan")</script></body></html>', encoding="utf-8")
    assert c.post("/api/shares", json={"path": str(f)}).status_code == 401
    r = c.post("/api/shares", json={"path": str(f), "days": 7}, headers=H).json()
    assert r["url"].endswith("/s/" + r["code"]) and r["view_only"] and r["expires"] > time.time()
    page = c.get("/s/" + r["code"])            # no token: public
    assert page.status_code == 200 and "aiw-view-only" in page.text and "只看模式" in page.text
    assert page.headers.get("x-robots-tag") == "noindex"
    rows = c.get("/api/shares", headers=H).json()
    assert rows[0]["code"] == r["code"] and rows[0]["views"] == 1 and rows[0]["alive"]
    assert c.delete("/api/shares/" + r["code"], headers=H).json()["ok"]
    assert c.get("/s/" + r["code"]).status_code == 410
    # editable share: no view-only injection
    r2 = c.post("/api/shares", json={"path": str(f), "view_only": False}, headers=H).json()
    assert "aiw-view-only" not in c.get("/s/" + r2["code"]).text


def test_share_folder_files_and_guards(tmp_path):
    c, s = _app(tmp_path)
    d = Path(s.artifacts_dir) / "家-全景"
    d.mkdir()
    (d / "index.html").write_text("<html><body><img src='scene-s0.jpg'></body></html>", encoding="utf-8")
    (d / "scene-s0.jpg").write_bytes(b"\xff\xd8jpg")
    (Path(s.artifacts_dir) / "secret.txt").write_text("x")
    r = c.post("/api/shares", json={"path": str(d)}, headers=H).json()
    assert r["url"].endswith("/")
    assert c.get("/s/" + r["code"], follow_redirects=False).status_code == 307
    assert "scene-s0.jpg" in c.get(f"/s/{r['code']}/").text
    assert c.get(f"/s/{r['code']}/scene-s0.jpg").content == b"\xff\xd8jpg"
    assert c.get(f"/s/{r['code']}/../secret.txt").status_code in (404, 400)
    assert c.get(f"/s/{r['code']}/%2e%2e/secret.txt").status_code in (404, 400)
    assert c.post("/api/shares", json={"path": str(Path(s.artifacts_dir) / "secret.txt")}, headers=H).status_code == 400
    assert c.post("/api/shares", json={"path": "/etc/passwd"}, headers=H).status_code == 404
    # expiry
    db = json.loads((Path(s.artifacts_dir) / ".shares.json").read_text())
    db[r["code"]]["expires"] = time.time() - 1
    (Path(s.artifacts_dir) / ".shares.json").write_text(json.dumps(db))
    assert c.get(f"/s/{r['code']}/").status_code == 410
    assert c.get("/s/nope").status_code == 404


def test_share_qr_svg():
    pytest.importorskip("segno")
    from aiw_api.share import qr_svg
    assert qr_svg("https://example.com/s/abc").startswith("<svg")


def _equirect(p, w=800, h=400):
    from PIL import Image
    Image.new("RGB", (w, h), (120, 140, 160)).save(p)


def test_pano_photos_builds_viewer(tmp_path):
    pytest.importorskip("PIL")
    a, b, bad = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    _equirect(a); _equirect(b); _equirect(bad, 800, 600)
    cmd = [sys.executable, str(SK / "scripts" / "aiw_hpd.py"), "pano-photos"]
    r = subprocess.run(cmd + [str(a), str(bad), "-o", str(tmp_path / "o")], capture_output=True, text=True)
    res = json.loads(r.stdout.split("HPD_PANO ", 1)[1])
    assert not res["ok"] and "2:1" in res["error"]
    r = subprocess.run(cmd + [str(a), str(b), "-o", str(tmp_path / "o"), "--name", "我家", "--names", "客厅,主卧",
                              "--links", "客厅>主卧@30,主卧>客厅@-150^-10,坏>x@1", "--first", "主卧"], capture_output=True, text=True)
    res = json.loads(r.stdout.split("HPD_PANO ", 1)[1])
    assert res["ok"] and res["scenes"] == ["客厅", "主卧"] and res["first"] == "s1" and res["warnings"]
    html = Path(res["html"]).read_text(encoding="utf-8")
    assert "data:image/jpeg;base64," in html and '"label": "主卧"' in html
    idx = (Path(res["folder"]) / "index.html").read_text(encoding="utf-8")
    assert "scene-s0.jpg" in idx and (Path(res["folder"]) / "scene-s1.jpg").exists()


def test_pano_sources_wired():
    a = (SK / "scripts" / "aiw_hpd.py").read_text(encoding="utf-8")
    assert 'add_parser("pano")' in a and 'add_parser("pano-photos")' in a
    m = (SK / "scripts" / "pano.mjs").read_text(encoding="utf-8")
    assert "PANO_SCENES" in m and "readPixels" in m and "enterFP" in m
    v = (SK / "assets" / "pano-viewer.html").read_text(encoding="utf-8")
    assert "window.__PANO__ = window.__PANO__ || null;" in v and "deviceorientation" in v
    assert "assets/pano-viewer.html" in (SK / "MANIFEST").read_text() and "scripts/pano.mjs" in (SK / "MANIFEST").read_text()
    assert "pano" in (SK / "SKILL.md").read_text(encoding="utf-8")


def test_clients_have_share():
    js = (ROOT / "apps" / "web" / "mdpreview.js").read_text(encoding="utf-8")
    assert "/api/shares" in js and "mdm-share" in js
    sw = (ROOT / "apps" / "ios" / "Sources" / "HtmlPreview.swift").read_text(encoding="utf-8")
    assert "/api/shares" in sw and "qrCodeGenerator" in sw and "loadFileURL" in sw
    for f in ("ChatView.swift", "HomeChat.swift"):
        assert "HtmlPreviewView(url: p.url, serverPath: p.path)" in (ROOT / "apps" / "ios" / "Sources" / f).read_text(encoding="utf-8")


@pytest.mark.skipif(not (os.environ.get("AIW_PANO_E2E") and Path("/tmp/pano/h.html").exists()), reason="needs chromium + a designer")
def test_pano_e2e(tmp_path):
    r = subprocess.run([sys.executable, str(SK / "scripts" / "aiw_hpd.py"), "pano", "/tmp/pano/h.html", "-o", str(tmp_path), "--size", "1024"],
                       capture_output=True, text=True, timeout=1800)
    res = json.loads(r.stdout.split("HPD_PANO ", 1)[1])
    assert res["ok"] and len(res["scenes"]) >= 5 and not res["unlinked"]
