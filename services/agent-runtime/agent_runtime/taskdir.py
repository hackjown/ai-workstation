"""Every task is a fresh job in its own folder (user rule, 2026-10-30).

What went wrong: a follow-up drawing task (task-41c) found cad/spec.json from an earlier image in the shared cad/
folder and "revised" it for 100 steps; other runs compared the upload's md5 with an earlier upload, decided "same
picture" and re-sent the earlier task's PNG/DXF or edited image. The rule now:

* A new task ALWAYS starts empty, even when the upload's md5 equals an earlier one. Its outputs go to
  ~/workspace/tasks/<task id>/ ; for drawings ~/workspace/cad is a symlink to tasks/<id>/cad, so every tool and
  prompt that says cad/... works unchanged but only ever sees this task's files.
* Only a continuation of the SAME task card (tasks.continues = the root task, set by the API for 继续/修改/重试 on
  that card) inherits the root's folder: tasks/<this id> is linked to tasks/<root>.
* Earlier outputs are never a starting point or a deliverable. They live on only as distilled lessons and as the
  skill's generic examples.
* At delivery a file whose bytes equal a file delivered by ANOTHER task is refused (index of delivered sha256s on the
  server, artifacts/delivered-index.json).

Pure helpers + the small JSON index; the loop does the I/O on the computer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

TASKS_DIR = "tasks"
ROOT = '"${VM_AGENT_FILES_ROOT:-$HOME/workspace}"'
# the tool router's shell explainer only understands a literal ~/workspace as the workspace (a `cd "${VAR:-...}"`
# makes every mv look like it leaves the workspace and asks the user), so go there literally first
CD = 'cd ~/workspace 2>/dev/null; [ -z "$VM_AGENT_FILES_ROOT" ] || cd "$VM_AGENT_FILES_ROOT"'


def root_id(task) -> str:
    """the task whose folder this run works in: the root of a continuation chain, else the task itself."""
    c = str(getattr(task, "continues", "") or "").strip()
    return c or str(getattr(task, "id", "") or "task")


def safe(tid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tid or "task")[:64]


def rel_dir(task) -> str:
    return f"{TASKS_DIR}/{safe(root_id(task))}"


def setup_command(task, cad: bool) -> str:
    """create tasks/<root>/ (and tasks/<root>/cad), link a continuation's own id to its root's folder, and for a
    drawing task point ~/workspace/cad at this task's cad folder. A legacy real cad/ folder (before this rule) is moved
    to tasks/_legacy-<time>/cad, never reused."""
    d = rel_dir(task)
    me = safe(str(getattr(task, "id", "") or ""))
    lines = [CD, f'mkdir -p "{d}/out"']
    if me and f"{TASKS_DIR}/{me}" != d:
        lines.append(f'[ -e "{TASKS_DIR}/{me}" ] || ln -s "{safe(root_id(task))}" "{TASKS_DIR}/{me}"')
    if cad:
        cont = bool(me) and f"{TASKS_DIR}/{me}" != d
        if cont:   # a continuation of a task that ran before this rule: its work is the legacy cad/ folder
            lines.append(f'if [ ! -e "{d}/cad" ] && [ -d cad ] && [ ! -L cad ]; then mv cad "{d}/cad"; '
                         f'echo "adopted the earlier run\'s cad/ into {d}/cad"; fi')
        lines += [f'mkdir -p "{d}/cad"',
                  'if [ -d cad ] && [ ! -L cad ]; then L="tasks/_legacy-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$L"; '
                  'mv cad "$L/cad"; echo "legacy cad/ moved to $L/cad"; fi',
                  f'ln -sfn "{d}/cad" cad', 'pkill -x librecad 2>/dev/null; rm -f "$HOME/.cache/aiw-cad-progress"']
    lines.append(f'echo "workdir {d}"; ls -A "{d}" | head -40')
    return "; ".join(lines) + "; true"


RULE = ("FRESH-JOB RULE: this task is a NEW job. Write every output into {dir}/ (drawings: cad/, which is this task's "
        "own folder). Never copy, resend or start from another task's output (spec, dxf, png, pdf, edited image, video) "
        "- not even when the uploaded file has the same md5 as an earlier upload: same input still means a new job done "
        "anew. Earlier work only helps as lessons and the skill's generic examples. A file identical to one another task "
        "delivered is refused at send_file.")


def rule(task) -> str:
    return RULE.format(dir=rel_dir(task))


# ---- delivered-file index (server side) ---------------------------------------------------------------------------

def sha256_b64(b64: str) -> str:
    import base64
    return hashlib.sha256(base64.b64decode(b64)).hexdigest()


class DeliveredIndex:
    """sha256 -> {task, name, at} of every file sent to a user's chat. One small JSON file; last writer wins (one
    task per computer runs at a time, and a lost entry only weakens the check)."""

    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None

    def _load(self) -> dict:
        if not self.path or not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return {}

    def owner(self, sha: str) -> Optional[dict]:
        return self._load().get(sha)

    def conflict(self, sha: str, root: str) -> Optional[dict]:
        """the earlier delivery of these exact bytes by a DIFFERENT task (chain), or None."""
        o = self.owner(sha)
        if o and str(o.get("task")) != str(root):
            return o
        return None

    def record(self, sha: str, root: str, name: str) -> None:
        if not self.path:
            return
        d = self._load()
        d.setdefault(sha, {"task": root, "name": name, "at": time.time()})
        if len(d) > 20000:     # keep the newest
            d = dict(sorted(d.items(), key=lambda kv: kv[1].get("at", 0))[-15000:])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def index_for(artifacts_dir: Optional[str]) -> DeliveredIndex:
    p = os.environ.get("AIW_DELIVERED_INDEX") or (str(Path(artifacts_dir) / "delivered-index.json") if artifacts_dir else None)
    return DeliveredIndex(p)
