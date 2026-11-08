"""v3.9.26: git automation for dev_fix tasks — snapshot at start, commit on a passing done, one-call rollback.

* start(task, path): when the source of truth is inside a git repo, record HEAD and the current branch; a dirty tree is
  first saved as a stash snapshot (`git stash create` + `git stash store`: the working tree is NOT touched, so no user
  change is ever lost), then branch `aiw/<task-id>` is created from HEAD (the working tree carries over).
* status(state, paths): did the fix change anything (diff vs the recorded HEAD + new untracked files), with --stat.
* commit(state, message, paths): commit the fix on the aiw branch; returns the commit id and `git diff --stat`.
* rollback(state): back to the recorded HEAD: the current tree is stashed first (nothing lost), the aiw branch reset,
  the original branch checked out and the start snapshot re-applied — the repo is as it was before the task.

v3.9.29 (task-68b): ~/ai-workstation/news/aiw_news.py is an UNTRACKED file inside the aiw platform's own checkout.
Checking out an aiw/<task> branch there switches the running platform's code. So when the source is untracked, not in
a repo at all, or inside the platform repo (it has services/agent-runtime/agent_runtime, or AIW_PLATFORM_REPO), start
uses mode "copy": the source files are copied to ~/.aiw/snapshots/<task>/ (sha256 recorded); status compares hashes
(+ a line count diff), commit is a no-op that reports the change, rollback copies the snapshot back (the current file is
kept as <file>.aiw-before-rollback). The platform repo's branch is never touched.

Everything runs ON THE COMPUTER: command(op, args) embeds the part above `# ---- loop side` in a python3 heredoc and
prints one JSON line. The API's rollback endpoint uses the same command.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys


def _git(repo, *args, check=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _repo_of(path):
    p = os.path.expanduser(path or "")
    d = p if os.path.isdir(p) else os.path.dirname(p)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if not d:
        return None
    rc, out, _ = _git(d, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else None


def _ident(repo):
    rc, out, _ = _git(repo, "config", "user.email")
    return [] if out else ["-c", "user.name=aiw", "-c", "user.email=aiw@localhost"]


def _is_platform(repo):
    env = os.environ.get("AIW_PLATFORM_REPO")
    if env and os.path.realpath(os.path.expanduser(env)) == os.path.realpath(repo):
        return True
    return os.path.isdir(os.path.join(repo, "services", "agent-runtime", "agent_runtime"))


def _tracked(repo, path):
    rc, _, _ = _git(repo, "ls-files", "--error-unmatch", "--", os.path.expanduser(path))
    return rc == 0


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snap_root():
    return os.path.expanduser(os.environ.get("AIW_SNAPSHOT_DIR", "~/.aiw/snapshots"))


def copy_start(a, repo, why):
    task = a.get("task") or "task"
    prev = a.get("prev") or {}
    if prev.get("mode") == "copy" and prev.get("files") and all(os.path.exists(f.get("snap", "")) for f in prev["files"]):
        return {**prev, "ok": True, "reused": True}
    d = os.path.join(_snap_root(), task)
    os.makedirs(d, exist_ok=True)
    files = []
    srcs = [a.get("path")] + [x for x in (a.get("files") or []) if x != a.get("path")]
    for i, f in enumerate(srcs):
        p = os.path.expanduser(f or "")
        if not p or not os.path.isfile(p):
            continue
        snap = os.path.join(d, f"{i}_{os.path.basename(p)}")
        shutil.copy2(p, snap)
        files.append({"orig": p, "snap": snap, "sha": _sha(p)})
    if not files:
        return {"ok": False, "error": "no_source_file", "repo": repo}
    return {"ok": True, "mode": "copy", "reason": why, "repo": repo, "snapdir": d, "files": files,
            "base": "copy:" + task, "branch": None}


def op_start(a):
    repo = _repo_of(a.get("path"))
    if not repo:
        return copy_start(a, None, "not_in_repo")
    if _is_platform(repo):
        return copy_start(a, repo, "platform_repo")
    if not _tracked(repo, a.get("path")):
        return copy_start(a, repo, "untracked")
    rc, head, err = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return {"ok": False, "error": "no_commits", "repo": repo}
    _, orig, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _, dirty, _ = _git(repo, "status", "--porcelain")
    task = a.get("task") or "task"
    want = "aiw/" + task
    if orig == want:   # a continuation of the same task: already on its branch
        st = a.get("prev") or {}
        return {"ok": True, "repo": repo, "base": st.get("base") or head, "orig_branch": st.get("orig_branch") or orig,
                "branch": want, "snapshot": st.get("snapshot"), "dirty": len(dirty.splitlines()), "reused": True}
    snap = None
    if dirty:
        rc, sha, err = _git(repo, "stash", "create", f"aiw snapshot before {task}")
        if rc == 0 and sha:
            _git(repo, "stash", "store", "-m", f"aiw/{task} snapshot (before the fix)", sha)
            snap = sha
    branch, n = want, 1
    while _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)[0] == 0:
        n += 1
        branch = f"{want}-{n}"
    rc, _, err = _git(repo, "checkout", "-b", branch)
    if rc != 0:
        return {"ok": False, "error": "checkout_failed", "detail": err[:300], "repo": repo, "base": head, "snapshot": snap}
    return {"ok": True, "repo": repo, "base": head, "orig_branch": orig, "branch": branch, "snapshot": snap,
            "dirty": len(dirty.splitlines())}


def _paths(repo, paths):
    out = []
    for p in paths or []:
        q = os.path.expanduser(p)
        if os.path.isabs(q):
            rel = os.path.relpath(q, repo)
            if rel.startswith(".."):
                continue
            out.append(rel)
        else:
            out.append(q)
    return out


def _line_stat(files):
    out, changed = [], []
    for f in files or []:
        try:
            old = open(f["snap"], encoding="utf-8", errors="replace").read().splitlines()
            new = open(f["orig"], encoding="utf-8", errors="replace").read().splitlines() if os.path.exists(f["orig"]) else []
        except OSError:
            continue
        if _sha(f["orig"]) == f.get("sha") if os.path.exists(f["orig"]) else False:
            continue
        add = rem = 0
        for ln in difflib.unified_diff(old, new, lineterm="", n=0):
            if ln.startswith("+") and not ln.startswith("+++"):
                add += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                rem += 1
        changed.append(f["orig"])
        out.append(f" {f['orig']} | +{add} -{rem}")
    return changed, "\n".join(out)


def op_status(a):
    if a.get("mode") == "copy":
        changed, stat = _line_stat(a.get("files"))
        return {"ok": True, "changed": bool(changed), "files": changed, "stat": stat}
    repo, base = a.get("repo"), a.get("base")
    if not repo or not base:
        return {"ok": False, "error": "no_state"}
    ps = _paths(repo, a.get("paths"))
    sel = ["--"] + ps if ps else []
    _, names, _ = _git(repo, "diff", "--name-only", base, *sel)
    _, untracked, _ = _git(repo, "ls-files", "--others", "--exclude-standard", *sel)
    _, stat, _ = _git(repo, "diff", "--stat", base, *sel)
    files = [x for x in names.splitlines() + untracked.splitlines() if x]
    return {"ok": True, "changed": bool(files), "files": files[:50], "stat": stat[-2000:]}


def op_commit(a):
    if a.get("mode") == "copy":   # nothing to commit on: the snapshot stays for rollback
        changed, stat = _line_stat(a.get("files"))
        return {"ok": True, "committed": False, "commit": None, "stat": stat, "mode": "copy"}
    repo, base = a.get("repo"), a.get("base")
    if not repo:
        return {"ok": False, "error": "no_state"}
    ps = _paths(repo, a.get("paths"))
    _git(repo, "add", "-A", "--", *(ps or ["."]))
    rc, _, _ = _git(repo, "diff", "--cached", "--quiet")
    if rc == 0:
        _, head, _ = _git(repo, "rev-parse", "HEAD")
        _, stat, _ = _git(repo, "diff", "--stat", base or head, head)
        return {"ok": True, "committed": False, "commit": head if head != base else None, "stat": stat[-2000:]}
    msg = (a.get("message") or "aiw fix").strip()[:3000]
    rc, out, err = _git(repo, *_ident(repo), "commit", "-q", "-m", msg)
    if rc != 0:
        return {"ok": False, "error": "commit_failed", "detail": (err or out)[:300]}
    _, head, _ = _git(repo, "rev-parse", "HEAD")
    _, stat, _ = _git(repo, "diff", "--stat", base or (head + "~1"), head)
    return {"ok": True, "committed": True, "commit": head, "stat": stat[-2000:]}


def op_rollback(a):
    if a.get("mode") == "copy":
        restored = []
        for f in a.get("files") or []:
            if not os.path.exists(f.get("snap", "")):
                continue
            if os.path.exists(f["orig"]):
                shutil.copy2(f["orig"], f["orig"] + ".aiw-before-rollback")
            shutil.copy2(f["snap"], f["orig"])
            restored.append(f["orig"])
        return {"ok": bool(restored), "mode": "copy", "restored": restored,
                "error": None if restored else "no_snapshot"}
    repo, base = a.get("repo"), a.get("base")
    if not repo or not base:
        return {"ok": False, "error": "no_state"}
    task = a.get("task") or "task"
    _, dirty, _ = _git(repo, "status", "--porcelain")
    saved = None
    if dirty:   # whatever is in the tree now is kept as a stash entry: a rollback never loses work
        rc, sha, _ = _git(repo, "stash", "create", f"aiw before rollback of {task}")
        if rc == 0 and sha:
            _git(repo, "stash", "store", "-m", f"aiw/{task} state before rollback", sha)
            saved = sha
    _, cur, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    br = a.get("branch")
    if br and cur == br:
        rc, _, err = _git(repo, "reset", "-q", "--hard", base)
    else:
        rc, _, err = _git(repo, "reset", "-q", "--hard")
    if rc != 0:
        return {"ok": False, "error": "reset_failed", "detail": err[:300], "saved": saved}
    orig = a.get("orig_branch")
    if orig and orig != "HEAD":
        rc, _, err = _git(repo, "checkout", "-q", orig)
    else:
        rc, _, err = _git(repo, "checkout", "-q", base)
    if rc != 0:
        return {"ok": False, "error": "checkout_failed", "detail": err[:300], "saved": saved}
    applied = None
    if a.get("snapshot"):
        rc, _, err = _git(repo, "stash", "apply", "-q", a["snapshot"])
        applied = rc == 0
    _, head, _ = _git(repo, "rev-parse", "HEAD")
    return {"ok": True, "head": head, "branch": orig or base, "restored_snapshot": applied, "saved": saved,
            "kept_branch": br}


OPS = {"start": op_start, "status": op_status, "commit": op_commit, "rollback": op_rollback}


def main(argv):
    try:
        a = json.loads(argv[2]) if len(argv) > 2 else {}
        res = OPS[argv[1]](a)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": type(e).__name__, "detail": str(e)[:400]}
    print(json.dumps(res, ensure_ascii=False))


# ---- loop side (not shipped to the computer)
def command(op: str, args: dict) -> str:
    import inspect
    import shlex
    src = inspect.getsource(sys.modules[__name__]).split("\n# ---- loop side")[0]
    return (f"python3 - {shlex.quote(op)} {shlex.quote(json.dumps(args, ensure_ascii=False))} <<'AIW_GIT_EOF'\n"
            f"{src}\nmain(sys.argv)\nAIW_GIT_EOF")


def parse(stdout: str) -> dict:
    for line in reversed([x for x in str(stdout or "").splitlines() if x.strip()]):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "error": "no_result", "detail": str(stdout or "")[-300:]}


STATE_KEYS = ("repo", "base", "branch", "orig_branch", "snapshot", "mode", "files", "snapdir")


def summary_line(g: dict) -> str:
    if g and g.get("mode") == "copy":
        s = f"快照：改动前的源文件已复制到 {g.get('snapdir')}（源文件不在 git 跟踪中或位于平台仓库内，未建分支）"
        if g.get("stat"):
            s += "\n" + str(g["stat"]).strip()[-800:]
        return s + "\n（如需撤销，可在任务卡片上点「回滚」，或回复「回滚」：把源文件恢复成快照）"
    if not g or not g.get("branch"):
        return ""
    s = f"Git：分支 {g['branch']}"
    if g.get("commit"):
        s += f"，提交 {str(g['commit'])[:10]}"
    if g.get("stat"):
        s += "\n" + str(g["stat"]).strip()[-800:]
    s += f"\n（如需撤销，可在任务卡片上点「回滚」，或回复「回滚」：恢复到 {str(g.get('base') or '')[:10]}）"
    return s


if __name__ == "__main__":
    main(sys.argv)
