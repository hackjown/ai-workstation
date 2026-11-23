"""v3.9.76: an engine's file over 40 MB (a 3-minute video) opens: the computer is woken first (an asleep cloud box
answered with HTTP 409) and the file comes over in 24 MB pieces."""
import asyncio
import base64
import os
import re
from types import SimpleNamespace

from aiw_api import bots as B


class FakeComp:
    def __init__(self, blob: bytes, name: str):
        self.blob, self.name, self.parts, self.calls = blob, name, {}, []

    async def execute(self, action, payload, observe=False, timeout=30):
        self.calls.append(action)
        if action == "files.list":
            return SimpleNamespace(success=True, result={"entries": [{"name": self.name, "dir": False,
                                                                      "size": len(self.blob), "mtime": 1}]})
        if action == "shell.exec":
            m = re.search(r"of=(\S+) .*skip=(\d+) count=(\d+)", payload["command"])
            if m:
                a, n = int(m.group(2)), int(m.group(3))
                self.parts[m.group(1)] = self.blob[a:a + n]
            return SimpleNamespace(success=True, result={"exit_code": 0, "stdout": "", "stderr": ""})
        if action == "files.read":
            data = self.parts.get(payload["path"], self.blob[:40 * 1024 * 1024])
            return SimpleNamespace(success=True, result={"content_b64": base64.b64encode(data).decode(),
                                                         "size": len(self.blob)})
        raise AssertionError(action)


def _mgr(comp, woke):
    m = B.Bots.__new__(B.Bots)
    m.conv = lambda cid: {"id": cid, "machine_id": "cloud-media", "workdir": "~/workspace/agy-chat/x"}
    m.default_machine = lambda: "ai-linux-001"
    m.machine_label = lambda mid: mid

    async def wake(mid):
        woke.append(mid)
    m.wake = wake
    m.sessions = SimpleNamespace(computer=lambda mid: comp, cloud=None)
    return m


def test_big_video_comes_in_pieces(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "PIECE", 1000)
    monkeypatch.setattr(B, "ONE_SHOT", 1500)
    blob = os.urandom(4321)
    comp, woke = FakeComp(blob, "v.mp4"), []
    r = asyncio.run(_mgr(comp, woke).fetch_file("c1", "v.mp4", str(tmp_path)))
    assert woke == ["cloud-media"]
    assert open(r["path"], "rb").read() == blob and r["size"] == 4321
    assert comp.calls.count("files.read") == 5
    r2 = asyncio.run(_mgr(comp, woke).fetch_file("c1", "v.mp4", str(tmp_path)))
    assert r2.get("cached")


def test_small_file_one_read(tmp_path):
    blob = b"%PDF-1.4 hello"
    comp, woke = FakeComp(blob, "a.pdf"), []
    r = asyncio.run(_mgr(comp, woke).fetch_file("c2", "a.pdf", str(tmp_path)))
    assert open(r["path"], "rb").read() == blob and comp.calls.count("shell.exec") == 0


def test_web_previews_media():
    src = open(os.path.join(os.path.dirname(__file__), "..", "apps/web/bots.js"), encoding="utf-8").read()
    assert "function btMediaPreview" in src and "<video" in src and "pdf" in src
