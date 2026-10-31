"""v3.9.2: Office documents previewed faithfully.

* GET /api/artifacts/preview turns an Office file into a PDF (LibreOffice on the API host or on a computer), with the
  same auth and path checks as /api/artifacts, cached by md5 + mtime, and a JSON error when no converter is reachable.
* the web app renders Word / Excel from the original file (vendored docx-preview + jszip, SheetJS), PDF for the rest.
* iOS opens Office files in QuickLook from the original file.
"""
import asyncio
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF\n"


def _app(tmp_path, local=None, vm=None):
    from fastapi.testclient import TestClient
    from aiw_api.config import Settings
    from aiw_api.main import build_app
    s = Settings(api_token="t", agent_token="a", db_path=str(tmp_path / "a.db"), artifacts_dir=str(tmp_path / "art"))
    s.scheduler_enabled = False
    os.makedirs(s.artifacts_dir, exist_ok=True)
    app = build_app(s)
    op = app.state.office_preview
    calls = {"local": 0, "vm": 0}

    async def fake_local(src, binary):
        calls["local"] += 1
        if local == "fail":
            from aiw_api.office_preview import PreviewError
            raise PreviewError("LibreOffice 转换失败：broken")
        return PDF + open(src, "rb").read()[:4]

    op.local_binary = (lambda: "/usr/bin/soffice") if local else (lambda: None)
    op.local_convert = fake_local

    async def no_computers():
        return vm or []
    op._computers = no_computers
    return TestClient(app), s, op, calls


def _doc(s, name="报告.docx", data=b"PK\x03\x04docx-bytes"):
    d = Path(s.artifacts_dir) / "s1" / "files"; d.mkdir(parents=True, exist_ok=True)
    p = d / name; p.write_bytes(data)
    return p


H = {"Authorization": "Bearer t"}


def test_auth_and_path_checks(tmp_path):
    cl, s, _, calls = _app(tmp_path, local=True)
    with cl:
        p = _doc(s)
        assert cl.get("/api/artifacts/preview", params={"path": str(p)}).status_code == 401
        outside = tmp_path / "secret.docx"; outside.write_bytes(b"PK")
        assert cl.get("/api/artifacts/preview", params={"path": str(outside)}, headers=H).status_code == 404
        trav = str(Path(s.artifacts_dir) / "s1" / ".." / ".." / "secret.docx")
        assert cl.get("/api/artifacts/preview", params={"path": trav}, headers=H).status_code == 404
        assert cl.get("/api/artifacts/preview", params={"path": str(p) + ".nope"}, headers=H).status_code == 404
        # a directory under artifacts is not a file
        assert cl.get("/api/artifacts/preview", params={"path": str(p.parent)}, headers=H).status_code == 404
        txt = _doc(s, "a.txt", b"hi")
        r = cl.get("/api/artifacts/preview", params={"path": str(txt)}, headers=H)
        assert r.status_code == 415 and r.json()["code"] == "not_office"
        assert calls["local"] == 0
        # the token works as a query parameter too (iframes, new tabs)
        assert cl.get("/api/artifacts/preview", params={"path": str(p), "token": "t"}).status_code == 200


def test_pdf_returned_and_cached(tmp_path):
    cl, s, op, calls = _app(tmp_path, local=True)
    with cl:
        p = _doc(s)
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")
        assert "inline" in r.headers["content-disposition"] and "%E6%8A%A5%E5%91%8A.pdf" in r.headers["content-disposition"]
        r2 = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r2.status_code == 200 and r2.content == r.content
        assert calls["local"] == 1                      # second open came from the cache
        cached = list((Path(s.artifacts_dir) / ".preview").glob("*.pdf"))
        assert len(cached) == 1 and "-" in cached[0].stem   # <md5>-<mtime>.pdf
        # a changed file is converted again
        p.write_bytes(b"PK\x03\x04changed!")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
        assert cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H).status_code == 200
        assert calls["local"] == 2
        # same bytes, other file: same md5 + same mtime share the PDF
        q = _doc(s, "copy.docx", p.read_bytes()); os.utime(q, (p.stat().st_atime, p.stat().st_mtime))
        assert cl.get("/api/artifacts/preview", params={"path": str(q)}, headers=H).status_code == 200
        assert calls["local"] == 2


def test_no_converter_is_a_json_error(tmp_path):
    cl, s, _, _ = _app(tmp_path, local=None, vm=[])
    with cl:
        p = _doc(s, "slides.pptx")
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 503
        j = r.json()
        assert j["code"] == "no_converter" and "LibreOffice" in j["error"]
        assert not list((Path(s.artifacts_dir) / ".preview").glob("*.pdf"))


def test_conversion_failure_reported(tmp_path):
    cl, s, _, _ = _app(tmp_path, local="fail")
    with cl:
        p = _doc(s, "bad.xlsx")
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 502 and r.json()["code"] == "convert_failed"


def test_too_big(tmp_path, monkeypatch):
    from aiw_api import office_preview
    monkeypatch.setattr(office_preview, "MAX_MB", 0)
    cl, s, _, calls = _app(tmp_path, local=True)
    with cl:
        p = _doc(s)
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 413 and r.json()["code"] == "too_big" and calls["local"] == 0


class _R:
    def __init__(self, success=True, result=None, error=None):
        self.success, self.result, self.error = success, result or {}, error


class FakeComputer:
    """A vm-agent that has soffice: records the commands, answers like vm-agent does."""
    def __init__(self, has_soffice=True):
        self.has, self.calls, self.files = has_soffice, [], {}

    async def execute(self, action, payload=None, observe=False, timeout=30):
        import base64
        self.calls.append((action, payload))
        if action == "files.upload":
            self.files[payload["path"]] = base64.b64decode(payload["content_b64"])
            return _R(result={"size": len(self.files[payload["path"]])})
        if action == "shell.exec":
            cmd = payload["command"]
            if "--convert-to pdf" in cmd:
                if not self.has:
                    return _R(False, {"exit_code": 127, "stdout": "NO_SOFFICE\n", "stderr": ""})
                job = cmd.split(".aiw-preview/")[1].split(" ")[0]
                self.files[f".aiw-preview/{job}/doc.pdf"] = PDF
                return _R(result={"exit_code": 0, "stdout": str(len(PDF))})
            return _R(result={"exit_code": 0})
        if action == "files.read":
            data = self.files.get(payload["path"])
            if data is None:
                return _R(False, error="no such file")
            return _R(result={"content_b64": base64.b64encode(data).decode(), "size": len(data)})
        return _R(False, error="unknown")


def test_converts_on_a_computer(tmp_path):
    comp = FakeComputer()
    cl, s, _, calls = _app(tmp_path, local=None, vm=[comp])
    with cl:
        p = _doc(s, "周报.pptx", b"PK\x03\x04ppt")
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 200 and r.content == PDF
        acts = [a for a, _ in comp.calls]
        assert acts[0] == "files.upload" and "files.read" in acts
        conv = next(pl["command"] for a, pl in comp.calls if a == "shell.exec" and "--convert-to" in pl["command"])
        # a private LibreOffice profile: never collides with a LibreOffice window open on that computer
        assert "-env:UserInstallation=file:///tmp/aiw-preview-lo-" in conv and "--headless" in conv
        assert "doc.pptx" in conv            # ascii name on the computer, whatever the real name is
        assert any(a == "shell.exec" and "rm -rf" in pl["command"] for a, pl in comp.calls)   # cleaned up
        n = len(comp.calls)
        assert cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H).status_code == 200
        assert len(comp.calls) == n           # cached


def test_computer_without_soffice_tries_the_next(tmp_path):
    a, b = FakeComputer(has_soffice=False), FakeComputer()
    cl, s, _, _ = _app(tmp_path, local=None, vm=[a, b])
    with cl:
        p = _doc(s, "x.doc", b"\xd0\xcf\x11\xe0old-word")
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 200 and r.content == PDF
        assert any(x == "files.read" for x, _ in b.calls)

    a2 = FakeComputer(has_soffice=False)
    cl, s, _, _ = _app(tmp_path / "2", local=None, vm=[a2])
    with cl:
        p = _doc(s, "x.odt")
        r = cl.get("/api/artifacts/preview", params={"path": str(p)}, headers=H)
        assert r.status_code == 503 and r.json()["code"] == "no_converter"


def test_concurrent_opens_convert_once(tmp_path):
    from aiw_api.office_preview import OfficePreview
    art = tmp_path / "art"; (art / "s").mkdir(parents=True)
    src = art / "s" / "a.docx"; src.write_bytes(b"PK..")
    op = OfficePreview(str(art))
    n = {"c": 0}

    async def slow(src_, b):
        n["c"] += 1
        await asyncio.sleep(0.05)
        return PDF
    op.local_binary, op.local_convert = (lambda: "soffice"), slow

    async def go():
        return await asyncio.gather(*[op.pdf(str(src)) for _ in range(5)])
    outs = asyncio.run(go())
    assert len(set(outs)) == 1 and n["c"] == 1


def test_vm_command_shape():
    from aiw_api.office_preview import vm_command, is_office
    c = vm_command("abc123", ".xlsx")
    assert "command -v soffice" in c and "NO_SOFFICE" in c and "--convert-to pdf" in c and "doc.xlsx" in c
    for n in ("a.docx", "A.DOC", "b.xlsx", "c.xls", "d.pptx", "e.ppt", "f.odt", "g.ods", "h.odp", "i.rtf"):
        assert is_office(n), n
    for n in ("a.pdf", "a.md", "a.txt", "a.zip", "docx"):
        assert not is_office(n), n


# ------------------------------------------------------------------------------------------- front end + iOS wiring
def test_web_office_preview_wired():
    idx = (WEB / "index.html").read_text()
    assert idx.index("officeview.js") < idx.index("mdpreview.js")
    ov = (WEB / "officeview.js").read_text()
    for f in ("docx-preview.min.js", "jszip.min.js", "xlsx.full.min.js"):
        assert f in ov and (WEB / "vendor" / "office" / f).stat().st_size > 10000
        assert f not in idx                     # lazy: loaded only when an Office file is opened
    assert "cdn" not in ov.lower().replace("no cdn", "")
    assert "/api/artifacts/preview?" in ov and "按打印版式查看(PDF)" in (WEB / "mdpreview.js").read_text()
    md = (WEB / "mdpreview.js").read_text()
    assert "AiwOffice" in md and '"office"' in md
    # every vendored library ships with its licence
    names = os.listdir(WEB / "vendor" / "office")
    assert {"LICENSE-docx-preview.txt", "LICENSE-jszip.md", "LICENSE-sheetjs.txt", "README.md"} <= set(names)


def test_ios_office_opens_original_in_quicklook():
    src = (ROOT / "apps" / "ios" / "Sources")
    mp = (src / "MarkdownPreview.swift").read_text()
    assert "func previewFileName(" in mp
    for f in ("ChatView.swift", "HomeChat.swift"):
        t = (src / f).read_text()
        assert "previewFileName(name, path: path)" in t
        assert "QuickLookView(url: p.url)" in t
    # Office never goes to the text renderer
    assert '"docx"' not in mp.split("func textPreviewKind")[1].split("}")[0]


def test_ios_share_goes_to_chat_composer():
    """v3.9.2: a file shared from another app lands in the chat composer as attachments, not in a task view."""
    src = ROOT / "apps" / "ios" / "Sources"
    root = (src / "ThreadsView.swift").read_text()
    assert "toTaskForImport" not in root and "func toChatForImport()" in root
    body = root.split("func toChatForImport()")[1].split("\n    }\n")[0]
    assert "path = []" in body and ".newTask" not in body and "aiwShowChat" in body
    hc = (src / "HomeChat.swift").read_text()
    assert "@ObservedObject private var inbox = ImportCenter.shared" in hc
    tk = hc.split("func takeImports()")[1].split("\n    func ")[0]
    assert "inbox.take()" in tk and "await uploadFile(f" in tk and "inbox.discard(f)" in tk
    assert "send(" not in tk                                  # nothing is sent automatically
    assert ".onChange(of: inbox.tick)" in hc
    cv = (src / "ChatView.swift").read_text()
    assert "takeImports" not in cv.replace("HomeChatView.takeImports", "")   # task views no longer take shares
    # the share extension still hands over through the App Group + aiw://import
    ext = (ROOT / "apps" / "ios" / "Share" / "ShareViewController.swift").read_text()
    assert "aiw://import?id=" in ext
