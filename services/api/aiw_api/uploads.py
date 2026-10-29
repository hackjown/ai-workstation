"""Uploads (2026-10-28, v2 home): photos, videos and files from the chat's + sheet.

* POST /api/uploads            JSON {name, content_b64} (old clients; up to JSON_MAX_MB)
* POST /api/uploads/raw?name=  the file itself as the request body, streamed to disk (videos; up to RAW_MAX_MB)
* push_to_vm()                 copies a stored upload onto a computer in CHUNK pieces: one files.upload message stays far
                               under the vm-agent's 64 MB websocket frame limit, so a 300 MB video gets there too.
A vm-agent older than 2026-10-28 ignores ``append`` and would keep only the last piece; the size it reports gives that
away and the push fails with a plain message instead of leaving a broken file.
"""
from __future__ import annotations

import base64
import os
import re
import time
from typing import Optional

MB = 1024 * 1024
JSON_MAX_MB = int(os.environ.get("AIW_UPLOAD_JSON_MAX_MB", "100"))
RAW_MAX_MB = int(os.environ.get("AIW_UPLOAD_MAX_MB", "500"))
CHUNK = int(os.environ.get("AIW_UPLOAD_CHUNK_MB", "16")) * MB

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".hevc")


class PushError(RuntimeError):
    pass


def clean_name(name: str) -> str:
    n = os.path.basename(str(name or "")).replace("/", "_").replace("\\", "_").strip()
    n = re.sub(r"[\x00-\x1f]", "", n)[:120]
    return n or "upload.bin"


def local_path(artifacts_dir: str, name: str) -> str:
    d = os.path.join(artifacts_dir, "uploads")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{int(time.time())}-{name}")


async def push_to_vm(computer, local: str, vm_rel: str, timeout: float = 180) -> int:
    """Copy ``local`` to ``~/workspace/<vm_rel>`` on the computer. Returns the size; raises PushError."""
    total = os.path.getsize(local)
    sent = 0
    with open(local, "rb") as f:
        first = True
        while first or sent < total:
            piece = f.read(CHUNK)
            payload = {"path": vm_rel, "content_b64": base64.b64encode(piece).decode()}
            if not first:
                payload["append"] = True
            r = await computer.execute("files.upload", payload, observe=False, timeout=timeout)
            if not r.success:
                raise PushError(f"放到电脑上失败：{r.error}")
            sent += len(piece)
            res = getattr(r, "result", None)
            got = res.get("size") if isinstance(res, dict) else None
            if not first and got is not None and int(got) != sent:
                raise PushError(f"电脑上的 vm-agent 太旧，不能分段接收大于 {CHUNK // MB} MB 的文件：请更新 vm-agent（设置 › 电脑 › 更新）后再试")
            first = False
    return total


# ---------------------------------------------------------------------------------------------- 3.8.5: durable uploads
# Every upload (chat + sheet, task view, share sheet) is kept on the API host and recorded here with the thread and the
# task it belongs to. When a task starts (new, 继续, 重试, 修改) on ANY computer, sync_for_task() puts the files of that
# task's thread / runs onto that computer's ~/workspace/uploads (only what is missing or has another size), so a
# continuation on another VM or a fresh session still finds "the PPT I uploaded earlier". Before 3.8.5 a chat upload
# went to whichever computer was picked when it was sent, and nothing told a later run where it was.
import json as _json
import shlex as _shlex
import uuid as _uuid

UPLOAD_LINE = re.compile(r"(?:~|\$HOME|/[\w.\-/]*?)/workspace/(uploads/[^\n\r]+?)\s*$", re.M)


def ensure_table(store) -> None:
    store._x("CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, name TEXT, path TEXT, size INTEGER, kind TEXT, "
             "thread_id TEXT, task_id TEXT, msg_id TEXT, machine_id TEXT, created_at REAL)")
    store._x("CREATE INDEX IF NOT EXISTS ix_uploads_thread ON uploads(thread_id)")
    store._x("CREATE INDEX IF NOT EXISTS ix_uploads_task ON uploads(task_id)")


def record(store, name: str, path: str, size: int = 0, kind: str = "", thread_id: Optional[str] = None,
           task_id: Optional[str] = None, msg_id: Optional[str] = None, machine_id: Optional[str] = None) -> dict:
    """One row per stored file (keyed by its server path); later calls only fill in the links."""
    ensure_table(store)
    r = store._x("SELECT * FROM uploads WHERE path=?", (path,))
    if r:
        row = r[0]
        sets = {k: v for k, v in (("thread_id", thread_id), ("task_id", task_id), ("msg_id", msg_id),
                                  ("machine_id", machine_id)) if v and not row.get(k)}
        if sets:
            store._x(f"UPDATE uploads SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), row["id"]))
            row.update(sets)
        return row
    row = {"id": f"up-{_uuid.uuid4().hex[:10]}", "name": clean_name(name), "path": path, "size": int(size or 0),
           "kind": kind or "", "thread_id": thread_id, "task_id": task_id, "msg_id": msg_id, "machine_id": machine_id,
           "created_at": time.time()}
    store._x("INSERT INTO uploads (id, name, path, size, kind, thread_id, task_id, msg_id, machine_id, created_at) "
             "VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))
    return row


def link(store, path: str, task_id: Optional[str], thread_id: Optional[str], name: Optional[str] = None) -> Optional[dict]:
    """Tie a stored upload to a task (and its thread). A file sent for one task and then sent again for a later one
    keeps its first task; the thread link is what carries it to every later run of that thread."""
    if not path or not os.path.isfile(path):
        return None
    return record(store, name or os.path.basename(path).split("-", 1)[-1], path, os.path.getsize(path),
                  "video" if path.lower().endswith(VIDEO_EXT) else "", thread_id, task_id)


def names_in(prompt: str) -> list[str]:
    """Upload names a prompt points at (the 附件（已放到电脑上） block: - ~/workspace/uploads/<name>)."""
    out = []
    for m in UPLOAD_LINE.finditer(prompt or ""):
        n = m.group(1).split("/", 1)[-1].strip()
        if n and n not in out:
            out.append(n)
    return out


def link_prompt(store, prompt: str, task_id: str, thread_id: Optional[str]) -> int:
    """Old clients name their uploads only in the prompt: link the newest stored upload of each named file."""
    ensure_table(store)
    n = 0
    for name in names_in(prompt):
        r = store._x("SELECT * FROM uploads WHERE name=? ORDER BY created_at DESC LIMIT 1", (name,))
        if r:
            row = r[0]
            sets = {k: v for k, v in (("task_id", task_id), ("thread_id", thread_id)) if v and not row.get(k)}
            if sets:
                store._x(f"UPDATE uploads SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), row["id"]))
            n += 1
    return n


def for_task(store, task_ids: list[str], thread_id: Optional[str], prompts: Optional[list[str]] = None) -> list[dict]:
    """The files a run needs: uploads tied to any run of the task, to its thread, or named in its prompts.
    Newest copy per file name; files gone from the API host are left out."""
    ensure_table(store)
    rows: list[dict] = []
    ids = [t for t in task_ids if t]
    if ids:
        q = ",".join("?" * len(ids))
        rows += store._x(f"SELECT * FROM uploads WHERE task_id IN ({q})", tuple(ids))
    if thread_id:
        rows += store._x("SELECT * FROM uploads WHERE thread_id=?", (thread_id,))
    for p in prompts or []:
        for name in names_in(p):
            rows += store._x("SELECT * FROM uploads WHERE name=? ORDER BY created_at DESC LIMIT 1", (name,))
    best: dict[str, dict] = {}
    for r in rows:
        if not r.get("path") or not os.path.isfile(r["path"]):
            continue
        cur = best.get(r["name"])
        if not cur or (r.get("created_at") or 0) > (cur.get("created_at") or 0):
            best[r["name"]] = r
    return sorted(best.values(), key=lambda r: r.get("created_at") or 0)


def thread_uploads(store, thread_id: str) -> list[dict]:
    ensure_table(store)
    return store._x("SELECT * FROM uploads WHERE thread_id=? ORDER BY created_at", (thread_id,))


async def sync_files(computer, items: list[dict], timeout: float = 180) -> dict:
    """Make sure each {local, rel} is at <files root>/<rel> on the computer (the vm-agent's files root, normally
    ~/workspace). One shell call reads $HOME, the root and the sizes there; only missing files or files of another size
    are pushed. Returns {"home", "root", "files": [{rel, abs, local, pushed, ok, error}]}. Never raises."""
    out: dict = {"home": None, "root": None, "files": []}
    rels = [str(i["rel"]).lstrip("/") for i in items]
    probe = ('echo "HOME=$HOME"; R="${VM_AGENT_FILES_ROOT:-$HOME/workspace}"; echo "ROOT=$R"; '
             + "".join(f'if [ -f "$R"/{_shlex.quote(r)} ]; then echo "S=$(stat -c %s "$R"/{_shlex.quote(r)})"; '
                       f'else echo "S=-1"; fi; ' for r in rels))
    sizes: list[int] = []
    try:
        r = await computer.execute("shell.exec", {"command": probe, "timeout": 30}, observe=False, timeout=45)
        text = str(((getattr(r, "result", None) or {}) if isinstance(getattr(r, "result", None), dict) else {}).get("stdout") or "")
        for line in text.splitlines():
            if line.startswith("HOME="):
                out["home"] = line[5:].strip() or None
            elif line.startswith("ROOT="):
                out["root"] = line[5:].strip() or None
            elif line.startswith("S="):
                try:
                    sizes.append(int(line[2:].strip() or -1))
                except ValueError:
                    sizes.append(-1)
    except Exception:  # noqa: BLE001  an old/odd agent: push everything
        sizes = []
    root = out["root"] or (f"{out['home']}/workspace" if out["home"] else "~/workspace")
    for n, it in enumerate(items):
        rel = rels[n]
        f = {"rel": rel, "abs": f"{root}/{rel}", "local": it.get("local"), "name": it.get("name") or rel.split("/")[-1],
             "pushed": False, "ok": True, "error": None}
        try:
            want = os.path.getsize(it["local"])
            have = sizes[n] if n < len(sizes) else -1
            if have != want:
                await push_to_vm(computer, it["local"], rel, timeout=timeout)
                f["pushed"] = True
        except Exception as e:  # noqa: BLE001  the run still starts; its prompt says the file is missing and why
            f["ok"], f["error"] = False, str(e)[:200]
        out["files"].append(f)
    return out


def prompt_block(sync: dict, title: str = "ATTACHMENTS") -> str:
    """The files block for the task prompt: ~ paths plus the absolute path they resolve to on this computer."""
    lines = []
    for f in sync.get("files") or []:
        tilde = f"~/workspace/{f['rel']}"
        if f["ok"]:
            lines.append(f"- {tilde}  (= {f['abs']})")
        else:
            lines.append(f"- {tilde}  MISSING on this computer: {f['error']}")
    if not lines:
        return ""
    head = {"ATTACHMENTS": "ATTACHMENTS the user uploaded for this task (already on this computer; use them, never "
                           "ask for them again):",
            "FILES": "FILES this task already made in earlier runs (on this computer now):"}.get(title, title)
    return head + "\n" + "\n".join(lines)

