"""v3.9.2: faithful preview of Office documents (Word / Excel / PowerPoint / OpenDocument / RTF).

GET /api/artifacts/preview?path=...   same auth and path checks as /api/artifacts
  -> application/pdf (the document converted by LibreOffice: real fonts, tables, page layout, slides)
  -> JSON {"error": "...", "code": "no_converter" | "convert_failed" | "too_big" | "not_office"} with 4xx/5xx

The API image stays slim (no LibreOffice in it). A converter is found in this order:
  1. a local ``soffice`` / ``libreoffice`` on PATH (AIW_SOFFICE overrides the binary) - dev hosts, bare-metal installs;
  2. a computer the agent uses (its vm-agent): the file goes up with files.upload, ``soffice --headless --convert-to pdf``
     runs there through shell.exec in a private LibreOffice profile (so it never collides with a LibreOffice window the
     user or the agent has open), and the PDF comes back with files.read. AIW_PREVIEW_MACHINE picks the computer to
     try first; otherwise every online computer is tried in turn. AIW_PREVIEW_VM=0 turns this off.
PDFs are cached in <artifacts>/.preview/<md5>-<mtime>.pdf, so opening the same file again is instant; one conversion
runs per file at a time even when several clients open it together.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import shlex
import shutil
import tempfile
import time
import uuid
from typing import Awaitable, Callable, Optional

log = logging.getLogger("aiw.preview")

OFFICE_EXT = {".doc", ".docx", ".docm", ".dot", ".dotx", ".xls", ".xlsx", ".xlsm", ".xlsb", ".xlt", ".xltx",
              ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".pot", ".potx",
              ".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".odg", ".rtf", ".wps", ".et", ".dps"}
MAX_MB = int(os.environ.get("AIW_PREVIEW_MAX_MB", "40"))
CONVERT_TIMEOUT = int(os.environ.get("AIW_PREVIEW_TIMEOUT_S", "150"))
VM_READ_MAX = 40 * 1024 * 1024   # vm-agent files.read(binary) cap


class PreviewError(Exception):
    def __init__(self, msg: str, code: str = "convert_failed", status: int = 502):
        super().__init__(msg)
        self.code, self.status = code, status


def is_office(name: str) -> bool:
    return os.path.splitext(str(name or "").lower())[1] in OFFICE_EXT


def cache_dir(artifacts_dir: str) -> str:
    d = os.path.join(artifacts_dir, ".preview")
    os.makedirs(d, exist_ok=True)
    return d


_md5_memo: dict[tuple, str] = {}


def file_md5(path: str) -> str:
    st = os.stat(path)
    k = (path, st.st_size, st.st_mtime_ns)
    hit = _md5_memo.get(k)
    if hit:
        return hit
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    if len(_md5_memo) > 2000:
        _md5_memo.clear()
    _md5_memo[k] = h.hexdigest()
    return _md5_memo[k]


def cache_key(path: str) -> str:
    return f"{file_md5(path)}-{int(os.stat(path).st_mtime)}"


def local_soffice() -> Optional[str]:
    env = os.environ.get("AIW_SOFFICE")
    if env:
        return env if (os.path.exists(env) or shutil.which(env)) else None
    return shutil.which("soffice") or shutil.which("libreoffice")


def _profile_arg(d: str) -> str:
    return "-env:UserInstallation=file://" + d


async def convert_local(src: str, binary: str) -> bytes:
    """soffice on this host -> PDF bytes."""
    with tempfile.TemporaryDirectory(prefix="aiw-preview-") as tmp:
        ext = os.path.splitext(src)[1].lower()
        inp = os.path.join(tmp, "doc" + ext)   # ascii name: no surprises with quotes or CJK in soffice's argv
        shutil.copyfile(src, inp)
        proc = await asyncio.create_subprocess_exec(
            binary, _profile_arg(os.path.join(tmp, "profile")), "--headless", "--norestore", "--nolockcheck",
            "--convert-to", "pdf", "--outdir", tmp, inp,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), CONVERT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise PreviewError("转换超时", "convert_failed", 504)
        out = os.path.join(tmp, "doc.pdf")
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise PreviewError(f"LibreOffice 转换失败：{(err or b'').decode(errors='replace')[-300:]}")
        with open(out, "rb") as f:
            return f.read()


def vm_command(job: str, ext: str) -> str:
    """The shell line run on the computer (in ~/workspace/.aiw-preview/<job>/doc<ext>)."""
    d = f"$HOME/workspace/.aiw-preview/{job}"
    return (f"cd {d} && S=$(command -v soffice || command -v libreoffice || ls /opt/libreoffice*/program/soffice "
            f"2>/dev/null | head -1); [ -n \"$S\" ] || {{ echo NO_SOFFICE; exit 127; }}; "
            f"timeout {CONVERT_TIMEOUT} \"$S\" {shlex.quote(_profile_arg('/tmp/aiw-preview-lo-' + job))} --headless "
            f"--norestore --nolockcheck --convert-to pdf --outdir {d} doc{ext} >/dev/null 2>err.txt; rc=$?; "
            f"rm -rf /tmp/aiw-preview-lo-{job}; [ -s doc.pdf ] && stat -c %s doc.pdf || {{ tail -c 300 err.txt; exit 1; }}")


async def convert_on_computer(computer, src: str) -> bytes:
    """Upload, convert with the computer's soffice, read the PDF back. Raises PreviewError."""
    from . import uploads as _uploads
    job = uuid.uuid4().hex[:12]
    ext = os.path.splitext(src)[1].lower()
    rel = f".aiw-preview/{job}/doc{ext}"
    try:
        try:
            await _uploads.push_to_vm(computer, src, rel, timeout=120)
        except Exception as e:  # noqa: BLE001
            raise PreviewError(f"放到电脑上失败：{e}", "no_converter", 503)
        r = await computer.execute("shell.exec", {"command": vm_command(job, ext), "timeout": CONVERT_TIMEOUT + 15},
                                   observe=False, timeout=CONVERT_TIMEOUT + 40)
        res = getattr(r, "result", None) or {}
        out = str(res.get("stdout") or "")
        if "NO_SOFFICE" in out or res.get("exit_code") == 127:
            raise PreviewError("这台电脑没有装 LibreOffice", "no_converter", 503)
        if not getattr(r, "success", False):
            raise PreviewError(f"LibreOffice 转换失败：{(out + str(res.get('stderr') or '') or str(r.error or ''))[-300:]}")
        f = await computer.execute("files.read", {"path": f".aiw-preview/{job}/doc.pdf", "binary": True},
                                   observe=False, timeout=120)
        fr = getattr(f, "result", None) or {}
        if not getattr(f, "success", False) or not fr.get("content_b64"):
            raise PreviewError(f"读取转换结果失败：{getattr(f, 'error', '')}")
        data = base64.b64decode(fr["content_b64"])
        if (fr.get("size") or 0) > len(data):
            raise PreviewError("转换出的 PDF 超过 40MB，请下载原文件查看", "too_big", 413)
        return data
    finally:
        try:
            await computer.execute("shell.exec", {"command": f"rm -rf $HOME/workspace/.aiw-preview/{job}", "timeout": 20},
                                   observe=False, timeout=30)
        except Exception:  # noqa: BLE001
            pass


class OfficePreview:
    """Find a converter, convert once per (md5, mtime), keep the PDF."""

    def __init__(self, artifacts_dir: str, sessions=None, store=None):
        self.artifacts_dir, self.sessions, self.store = artifacts_dir, sessions, store
        self._locks: dict[str, asyncio.Lock] = {}
        # hooks the tests replace
        self.local_binary: Callable[[], Optional[str]] = local_soffice
        self.local_convert: Callable[[str, str], Awaitable[bytes]] = convert_local
        self.vm_convert: Callable[[object, str], Awaitable[bytes]] = convert_on_computer

    def cached(self, src: str) -> str:
        return os.path.join(cache_dir(self.artifacts_dir), cache_key(src) + ".pdf")

    async def _computers(self) -> list:
        if os.environ.get("AIW_PREVIEW_VM", "1") == "0" or not self.sessions or not self.store:
            return []
        try:
            ids = [m["id"] for m in self.store.machines()]
        except Exception:  # noqa: BLE001
            return []
        first = os.environ.get("AIW_PREVIEW_MACHINE", "")
        ids.sort(key=lambda i: 0 if i == first else 1)
        out = []
        for mid in ids:
            try:
                probe = getattr(self.sessions, "_probe", None)
                if probe and not await probe(mid, 4.0):
                    continue
                out.append(self.sessions.computer(mid))
            except Exception:  # noqa: BLE001
                continue
        return out

    async def _convert(self, src: str) -> bytes:
        errors: list[PreviewError] = []
        b = self.local_binary()
        if b:
            try:
                return await self.local_convert(src, b)
            except PreviewError as e:
                errors.append(e)
            except Exception as e:  # noqa: BLE001
                errors.append(PreviewError(f"LibreOffice 转换失败：{e}"))
        for comp in await self._computers():
            try:
                return await self.vm_convert(comp, src)
            except PreviewError as e:
                errors.append(e)
                if e.code not in ("no_converter",) and e.status != 503:
                    break   # the document itself failed: another computer would fail the same way
            except Exception as e:  # noqa: BLE001
                errors.append(PreviewError(f"电脑上转换失败：{e}", "no_converter", 503))
        real = [e for e in errors if e.code != "no_converter"]
        if real:
            raise real[0]
        raise PreviewError("现在没有可用的转换服务（需要电脑在线并装有 LibreOffice），可以先下载查看",
                           "no_converter", 503)

    async def pdf(self, src: str) -> str:
        """Path of the cached PDF for ``src`` (converting it first if needed). Raises PreviewError."""
        if not is_office(src):
            raise PreviewError("不是 Office 文档", "not_office", 415)
        if os.path.getsize(src) > MAX_MB * 1024 * 1024:
            raise PreviewError(f"文件超过 {MAX_MB}MB，请下载查看", "too_big", 413)
        out = self.cached(src)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        lock = self._locks.setdefault(out, asyncio.Lock())
        async with lock:
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
            t0 = time.time()
            data = await self._convert(src)
            if not data.startswith(b"%PDF"):
                raise PreviewError("转换结果不是 PDF")
            tmp = out + f".{uuid.uuid4().hex[:6]}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out)
            log.info("preview %s -> %s (%d KB, %.1fs)", os.path.basename(src), os.path.basename(out),
                     len(data) // 1024, time.time() - t0)
        self._locks.pop(out, None)
        return out
