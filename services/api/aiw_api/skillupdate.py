"""v3.9.51 技能「检查更新 / 一键更新」，自建技能在各电脑间同步（设置 › 技能）.

Three kinds of skills live in ~/.claude/skills/<id> on each computer:

* builtin    vendored in this repo under skills/<id> (house-plan-designer, cad-drawing, …). The repo copy is the
             expected version: each computer's copy is hashed over the same file list (MANIFEST, or every file) and
             compared. Outdated → run the skill's setup on that computer. When NOTICE.md names an upstream GitHub repo
             and commit, the upstream is asked how many commits it is ahead (shown only: a builtin with AIW changes is
             upgraded by an AIW release, not on the computer).
* installed  added in 设置 › 技能 › 安装 (`npx skills add <source>`), kept in kv skills_custom with its source. The
             GitHub head commit is recorded at install time (``rev``); a newer head, or computers whose copies differ
             or lack it → reinstall the same source on every computer.
* self       anything else with a SKILL.md: written by the user, by skill-creator in a task, or copied in by hand. The
             API host keeps the last synced copy (skills_store/<id>.tgz) and its hash (kv skills_selfsync). A computer
             whose copy changed since then is the new version and is copied to the others; when several changed
             differently the newest wins and the others are kept in ~/.claude/.aiw-skill-backup/. Missing copies are
             filled in. Deletions are not propagated (删除 in 设置 only removes the entry).

Every computer is reached through the callables main.py passes in (shell / read_file / write_file / is_online), so the
logic is tested with local folders standing in for computers.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

REPO = Path(__file__).resolve().parents[3]
SKILLS_DIR = "~/.claude/skills"
BACKUP_DIR = "~/.claude/.aiw-skill-backup"
MAX_BYTES = 40 * 1024 * 1024        # vm-agent files.read limit
ID_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,80}$")

# The same hashing runs on the API host (repo copies) and on every computer (inside INVENTORY), so it is kept as source.
HASH_SRC = r'''
import hashlib, os
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv"}
SKIP_FILES = {"package-lock.json", ".DS_Store", "MANIFEST"}


def _files(root, names=None):
    if names is not None:
        return sorted(n for n in names if os.path.basename(n) not in SKIP_FILES)
    out = []
    for d, ds, fs in os.walk(root, followlinks=True):
        ds[:] = sorted(x for x in ds if x not in SKIP_DIRS and not x.startswith(".aiw"))
        for f in fs:
            if f in SKIP_FILES or f.endswith(".pyc") or f.startswith(".aiw"):
                continue
            out.append(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return sorted(out)


def tree_hash(root, names=None):
    h = hashlib.sha256()
    newest, n, size = 0, 0, 0
    for rel in _files(root, names):
        p = os.path.join(root, rel)
        h.update(rel.encode() + b"\0")
        try:
            st = os.stat(p)
            newest, n, size = max(newest, st.st_mtime), n + 1, size + st.st_size
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()[:16], int(newest), n, size
'''
_ns: dict = {}
exec(HASH_SRC, _ns)  # noqa: S102 - our own constant
tree_hash = _ns["tree_hash"]
list_files = _ns["_files"]

INVENTORY = r'''
import json, os, re, sys
SPEC = json.loads(%(spec)r)
base = os.path.expanduser("~/.claude/skills")
items = []
if os.path.isdir(base):
    for sid in sorted(os.listdir(base)):
        d = os.path.join(base, sid)
        if sid.startswith(".") or not os.path.isfile(os.path.join(d, "SKILL.md")):
            continue
        h, mt, n, size = tree_hash(d, SPEC.get(sid))
        desc = ""
        try:
            head = open(os.path.join(d, "SKILL.md"), encoding="utf-8", errors="replace").read(4000)
            m = re.search(r"^description:\s*(.+)$", head, re.M)
            desc = (m.group(1).strip().strip('"') if m else "")[:200]
        except OSError:
            pass
        items.append({"id": sid, "hash": h, "mtime": mt, "files": n, "size": size, "desc": desc,
                      "link": os.path.islink(d)})
print("AIW_INV " + json.dumps(items, ensure_ascii=False))
'''


# ------------------------------------------------------------------------------------------------ what each kind is
def builtin_ids() -> set[str]:
    try:
        from agent_runtime.skills import CATALOG
        return {s["id"] for s in CATALOG}
    except Exception:  # noqa: BLE001
        return set()


def catalog_entry(sid: str) -> dict:
    try:
        from agent_runtime.skills import CATALOG
        return next((dict(s) for s in CATALOG if s["id"] == sid), {})
    except Exception:  # noqa: BLE001
        return {}


def vendored(repo: Path = REPO) -> dict[str, dict]:
    """skills/<id> in the repo → {names, hash, upstream} (names None = every file)."""
    out = {}
    root = repo / "skills"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not (d / "SKILL.md").is_file():
            continue
        man = d / "MANIFEST"
        names = [l.strip() for l in man.read_text(encoding="utf-8").splitlines() if l.strip()] if man.is_file() else None
        names = list_files(str(d), names)
        h, _, n, _ = tree_hash(str(d), names)
        out[d.name] = {"names": names, "hash": h, "files": n, "upstream": upstream_of(d)}
    return out


def upstream_of(d: Path) -> Optional[dict]:
    notice = d / "NOTICE.md"
    if not notice.is_file():
        return None
    t = notice.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?[\s/）)>\]]", t)
    c = re.search(r"(?:提交|commit)\s*([0-9a-f]{7,40})\b", t)
    if not m or not c:
        return None
    return {"repo": f"{m.group(1)}/{m.group(2)}", "commit": c.group(1)}


GH_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/@#].*)?$")


def gh_repo(source: str) -> Optional[str]:
    s = (source or "").strip()
    if not s or s.startswith(("local:", "@")) or ("://" in s and "github.com" not in s):
        return None
    m = GH_RE.match(s)
    return f"{m.group(1)}/{m.group(2)}" if m else None


# --------------------------------------------------------------------------------------------------- GitHub lookups
async def _gh(path: str, client: Optional[httpx.AsyncClient] = None) -> Any:
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": "aiw-skill-update"}
    tok = os.environ.get("AIW_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    own = client is None
    c = client or httpx.AsyncClient(timeout=12, follow_redirects=True)
    try:
        r = await c.get("https://api.github.com" + path, headers=hdr)
        if r.status_code != 200:
            raise RuntimeError(f"GitHub {r.status_code}")
        return r.json()
    finally:
        if own:
            await c.aclose()


async def gh_head(repo: str) -> dict:
    j = await _gh(f"/repos/{repo}/commits?per_page=1")
    c = j[0]
    return {"sha": c["sha"], "date": c["commit"]["committer"]["date"],
            "msg": c["commit"]["message"].splitlines()[0][:120]}


async def gh_ahead(repo: str, base: str) -> dict:
    j = await _gh(f"/repos/{repo}/compare/{base}...HEAD")
    cs = j.get("commits") or []
    return {"ahead": int(j.get("ahead_by") or 0), "head": (cs[-1]["sha"] if cs else base),
            "msgs": [c["commit"]["message"].splitlines()[0][:120] for c in cs[-5:]][::-1],
            "url": j.get("html_url", "")}


# ------------------------------------------------------------------------------------------------------- planning
def plan_self(versions: dict[str, dict], online: list[str], last: Optional[dict]) -> dict:
    """versions {machine: {hash, mtime}} of one self-built skill on the online computers; last = stored sync state.
    → {win, src, targets, backups, conflict, record}"""
    lh = (last or {}).get("hash")
    changed = {m: v for m, v in versions.items() if v["hash"] != lh}
    ch = {v["hash"] for v in changed.values()}
    conflict, src = False, None
    if not changed:
        win = lh
    elif len(ch) == 1:
        win = next(iter(ch))
        src = max((m for m, v in changed.items() if v["hash"] == win), key=lambda m: versions[m]["mtime"])
    else:
        conflict = True
        src = max(changed, key=lambda m: versions[m]["mtime"])
        win = versions[src]["hash"]
    targets = [m for m in online if versions.get(m, {}).get("hash") != win] if win else []
    return {"win": win, "src": src, "targets": targets, "backups": [m for m in targets if m in versions],
            "conflict": conflict, "record": bool(src) and win != lh,
            "changed_on": sorted(changed) if lh else []}


# ------------------------------------------------------------------------------------------------------------ class
Shell = Callable[[str, str, float], Awaitable[dict]]


class SkillUpdater:
    def __init__(self, *, machines: Callable[[], list[dict]], shell: Shell,
                 read_file: Callable[[str, str], Awaitable[bytes]], write_file: Callable[[str, str, bytes], Awaitable[None]],
                 is_online: Callable[[str], Awaitable[bool]], kv_get, kv_set, store_dir: str,
                 install: Optional[Callable] = None, repo: Path = REPO, github: bool = True):
        self.machines, self.shell, self.read_file, self.write_file = machines, shell, read_file, write_file
        self.is_online, self.kv_get, self.kv_set = is_online, kv_get, kv_set
        self.store_dir = Path(store_dir)
        self.install = install          # async (pkg, machines) → job dict (skillsync.install_all, bound)
        self.repo, self.github = repo, github
        self.lock = asyncio.Lock()

    # ---------------------------------------------------------------- inventory
    async def _inventory(self, mid: str, spec: dict) -> list[dict]:
        code = HASH_SRC + INVENTORY % {"spec": json.dumps(spec)}
        cmd = "python3 - <<'AIW_SKILL_INV'\n" + code + "\nAIW_SKILL_INV\n"
        r = await self.shell(mid, cmd, 60)
        for line in (r.get("stdout") or "").splitlines():
            if line.startswith("AIW_INV "):
                return json.loads(line[8:])
        raise RuntimeError(((r.get("stderr") or r.get("stdout") or "没有输出").strip().splitlines() or ["?"])[-1][:200])

    async def scan(self) -> tuple[dict, dict, dict]:
        """→ (vendored, machines {mid: {name, online, error}}, inv {mid: {sid: item}})"""
        vend = vendored(self.repo)
        spec = {sid: v["names"] for sid, v in vend.items()}
        ms, inv = {}, {}

        async def one(m):
            mid = m["id"]
            ms[mid] = {"name": m.get("name") or mid, "online": False}
            try:
                if not await self.is_online(mid):
                    return
                items = await asyncio.wait_for(self._inventory(mid, spec), 75)
                inv[mid] = {i["id"]: i for i in items if ID_RE.match(i.get("id", ""))}
                ms[mid]["online"] = True
            except Exception as e:  # noqa: BLE001
                ms[mid]["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        await asyncio.gather(*(one(m) for m in self.machines()))
        return vend, ms, inv

    # -------------------------------------------------------------------- check
    async def check(self, github: Optional[bool] = None) -> dict:
        gh = self.github if github is None else github
        vend, ms, inv = await self.scan()
        online = [m for m, v in ms.items() if v["online"]]
        custom = self.kv_get("skills_custom", []) or []
        by_src = {c["id"]: c for c in custom if c.get("source") and not c.get("self")}
        selfstate = self.kv_get("skills_selfsync", {}) or {}
        builtins = builtin_ids() | set(vend)
        ids = sorted({sid for m in inv.values() for sid in m} | set(by_src) | set(selfstate))
        items = []
        heads: dict[str, Any] = {}

        async def head(repo):
            if repo not in heads:
                heads[repo] = asyncio.ensure_future(gh_head(repo))
            return await heads[repo]

        for sid in ids:
            on = {mid: inv[mid][sid] for mid in online if sid in inv[mid]}
            it: dict[str, Any] = {"id": sid, "machines": {}, "state": "ok", "detail": ""}
            if sid in builtins:
                it["kind"] = "builtin"
                ce = catalog_entry(sid)
                it["title"] = ce.get("title") or sid
                v = vend.get(sid)
                if not v:
                    it["state"], it["detail"] = "n/a", "随 AIW 的安装脚本更新"
                    it["machines"] = {m: "has" for m in on}
                else:
                    old = [m for m, x in on.items() if x["hash"] != v["hash"]]
                    it["machines"] = {m: ("old" if m in old else "ok") for m in on}
                    if old:
                        it["state"] = "update"
                        it["detail"] = f"{len(old)} 台电脑上是旧版，更新 = 重新安装 AIW 里的这一版"
                    up = v.get("upstream")
                    if up and gh:
                        it["upstream"] = dict(up)
                        try:
                            a = await gh_ahead(up["repo"], up["commit"])
                            it["upstream"].update(a)
                            if a["ahead"] and it["state"] == "ok":
                                it["state"] = "upstream"
                                it["detail"] = f"上游 {up['repo']} 比 AIW 内置版新 {a['ahead']} 个提交，要等 AIW 合并后随新版更新"
                        except Exception as e:  # noqa: BLE001
                            it["upstream"]["error"] = str(e)[:120]
            elif sid in by_src:
                c = by_src[sid]
                it.update(kind="installed", title=c.get("title") or sid, source=c["source"], rev=c.get("rev", ""))
                hs = {x["hash"] for x in on.values()}
                missing = [m for m in online if m not in on]
                it["machines"] = {m: ("ok" if m in on else "missing") for m in online}
                why = []
                repo = gh_repo(c["source"])
                if repo and gh:
                    try:
                        h = await head(repo)
                        it["latest"] = h
                        if not c.get("rev"):
                            why.append("没记录装的是哪一版")
                        elif not h["sha"].startswith(c["rev"][:7]):
                            why.append(f"有新版：{h['msg']}")
                    except Exception as e:  # noqa: BLE001
                        it["latest_error"] = str(e)[:120]
                if len(hs) > 1:
                    why.append("各电脑上的版本不一样")
                if missing and on:
                    why.append(f"{len(missing)} 台电脑没装")
                if why:
                    it["state"], it["detail"] = "update", "；".join(why)
            else:
                it["kind"] = "self"
                c = next((x for x in custom if x.get("id") == sid), {})
                first = next(iter(on.values()), {})
                it["title"] = c.get("title") or sid
                it["desc"] = c.get("desc") or first.get("desc", "")
                p = plan_self({m: {"hash": x["hash"], "mtime": x["mtime"]} for m, x in on.items()}, online,
                              selfstate.get(sid))
                it["plan"] = p
                def mstate(m):
                    h = on.get(m, {}).get("hash")
                    if m in p["changed_on"]:
                        return "changed" if h == p["win"] else "conflict"
                    return "ok" if h == p["win"] else "old" if m in on else "missing"
                it["machines"] = {m: mstate(m) for m in online}
                if p["conflict"]:
                    it["state"] = "conflict"
                    it["detail"] = "多台电脑都改过，会用最新改的那台（" + ms[p["src"]]["name"] + "），其他的先备份"
                elif p["targets"]:
                    it["state"] = "sync"
                    src = p["src"] and ms[p["src"]]["name"]
                    it["detail"] = (f"在 {src} 上改过，" if src and selfstate.get(sid) else "") + \
                        f"要同步到 {len(p['targets'])} 台电脑"
                elif p["record"]:
                    it["state"], it["detail"] = "record", "各电脑一致，先存一份到 AIW"
                if not on and selfstate.get(sid):
                    it["detail"] = "开着的电脑上都没有（不自动删除）"
            items.append(it)
        res = {"checked_at": time.time(), "machines": ms, "items": items,
               "counts": {k: sum(1 for i in items if i["state"] == k)
                          for k in ("update", "sync", "conflict", "upstream", "record")}}
        if gh or not self.kv_get("skills_updates", None):   # a quick (no GitHub) auto run keeps the full check shown
            self.kv_set("skills_updates", res)
        return res

    # ------------------------------------------------------------------- update
    async def update(self, ids: Optional[list[str]] = None, kinds: Optional[set[str]] = None,
                     job_key: str = "skills_update_job") -> dict:
        async with self.lock:
            job: dict[str, Any] = {"started": time.time(), "done": False, "items": {}}
            self.kv_set(job_key, job)
            try:
                chk = await self.check(github=None if (not kinds or kinds - {"self"}) else False)
                ms = chk["machines"]
                todo = [i for i in chk["items"] if i["state"] in ("update", "sync", "conflict", "record")
                        and (not ids or i["id"] in ids) and (not kinds or i["kind"] in kinds)]
                for it in todo:
                    job["items"][it["id"]] = {"title": it["title"], "kind": it["kind"], "state": "waiting"}
                self.kv_set(job_key, job)
                sources_done: dict[str, dict] = {}
                for it in todo:
                    st = job["items"][it["id"]]
                    st["state"] = "running"; self.kv_set(job_key, job)
                    try:
                        if it["kind"] == "builtin":
                            st["msg"] = await self._update_builtin(it, ms)
                        elif it["kind"] == "installed":
                            src = it["source"]
                            if src not in sources_done:
                                sources_done[src] = await self._update_installed(it, ms)
                            st["msg"] = sources_done[src]["msg"]
                            if not sources_done[src]["ok"]:
                                raise RuntimeError(st["msg"])
                        else:
                            st["msg"] = await self._sync_self(it, ms)
                        st["state"] = "ok"
                    except Exception as e:  # noqa: BLE001
                        st["state"], st["error"] = "failed", f"{str(e)[:300]}"
                    self.kv_set(job_key, job)
                job["after"] = (await self.check(github=None if (not kinds or kinds - {"self"}) else False))["counts"]
            finally:
                job["done"], job["finished"] = True, time.time()
                self.kv_set(job_key, job)
            return job

    async def _update_builtin(self, it: dict, ms: dict) -> str:
        setup = catalog_entry(it["id"]).get("setup") or \
            f"bash ~/ai-workstation/deploy/vm/aiw-skill-setup.sh {shlex.quote(it['id'])}"
        old = [m for m, s in it["machines"].items() if s == "old"]
        errs = []

        async def one(mid):
            r = await self.shell(mid, setup, 900)
            if r.get("exit_code") not in (0, None):
                tail = ((r.get("stderr") or "") + (r.get("stdout") or "")).strip().splitlines()[-2:]
                errs.append(f"{ms[mid]['name']}: {' '.join(tail)[:200]}")
        await asyncio.gather(*(one(m) for m in old))
        if errs:
            raise RuntimeError("；".join(errs))
        return f"{len(old)} 台电脑已重新安装"

    async def _update_installed(self, it: dict, ms: dict) -> dict:
        if not self.install:
            return {"ok": False, "msg": "不能安装"}
        machines = [{"id": m, "name": ms[m]["name"]} for m, v in ms.items() if v["online"]]
        job = await self.install(it["source"], machines)
        bad = [v for v in (job.get("machines") or {}).values() if v.get("state") != "ok"]
        ok = not bad
        if ok and it.get("latest"):
            custom = self.kv_get("skills_custom", []) or []
            for c in custom:
                if c.get("source") == it["source"]:
                    c["rev"], c["rev_date"] = it["latest"]["sha"][:12], it["latest"]["date"]
            self.kv_set("skills_custom", custom)
        msg = f"{len(machines) - len(bad)}/{len(machines)} 台电脑已重新安装"
        if bad:
            msg += "；" + "；".join(f"{b.get('name')}: {b.get('error', '')[:120]}" for b in bad)
        return {"ok": ok, "msg": msg}

    async def _sync_self(self, it: dict, ms: dict) -> str:
        sid, p = it["id"], it["plan"]
        if not ID_RE.match(sid):
            raise RuntimeError("名字不合法")
        self.store_dir.mkdir(parents=True, exist_ok=True)
        blob_path = self.store_dir / f"{sid}.tgz"
        q = shlex.quote(sid)
        ws = '"${VM_AGENT_FILES_ROOT:-$HOME/workspace}"'
        rel = f".aiw-skillsync/{sid}.tgz"
        if p["src"]:
            r = await self.shell(p["src"], (
                f"set -e; mkdir -p {ws}/.aiw-skillsync; "
                f"tar czf {ws}/{shlex.quote(rel)} --exclude=node_modules --exclude=__pycache__ --exclude=.git "
                f"--exclude='*.pyc' -C {SKILLS_DIR} {q}; wc -c < {ws}/{shlex.quote(rel)}"), 180)
            try:
                size = int((r.get("stdout") or "0").strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise RuntimeError(f"打包失败：{(r.get('stderr') or '')[:200]}")
            if size > MAX_BYTES:
                raise RuntimeError(f"技能文件夹打包后 {size // 1048576} MB，超过 40 MB，不能同步")
            data = await self.read_file(p["src"], rel)
            if len(data) != size:
                raise RuntimeError("从电脑取回的文件不完整")
            tmp = blob_path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(blob_path)
            await self.shell(p["src"], f"rm -f {ws}/{shlex.quote(rel)}", 30)
        elif not blob_path.is_file():
            raise RuntimeError("AIW 上没有这份技能的存档，开着的电脑上也没有")
        data = blob_path.read_bytes()
        done, errs = [], []

        async def one(mid):
            try:
                await self.write_file(mid, rel, data)
                r = await self.shell(mid, (
                    f"set -e; d={SKILLS_DIR}; b={BACKUP_DIR}; mkdir -p \"$d\" \"$b\"; "
                    f"if [ -e \"$d\"/{q} ] || [ -L \"$d\"/{q} ]; then mv \"$d\"/{q} \"$b\"/{q}-$(date +%Y%m%d-%H%M%S); fi; "
                    f"tar xzf {ws}/{shlex.quote(rel)} -C \"$d\"; rm -f {ws}/{shlex.quote(rel)}; "
                    f"ls -1dt \"$b\"/{q}-* 2>/dev/null | tail -n +4 | xargs -r rm -rf; "
                    f"test -f \"$d\"/{q}/SKILL.md && echo AIW_OK"), 180)
                if "AIW_OK" not in (r.get("stdout") or ""):
                    raise RuntimeError((r.get("stderr") or "解压失败")[:200])
                done.append(mid)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{ms[mid]['name']}: {str(e)[:160]}")
        await asyncio.gather(*(one(m) for m in p["targets"]))
        state = self.kv_get("skills_selfsync", {}) or {}
        prev = state.get(sid) or {}
        state[sid] = {"hash": p["win"], "at": time.time(), "size": len(data),
                      "from": (ms[p["src"]]["name"] if p["src"] else prev.get("from", "")),
                      "conflict": bool(p["conflict"])}
        self.kv_set("skills_selfsync", state)
        self._register(sid, it, [m for m, s in it["machines"].items() if s != "missing"] + done)
        if errs:
            raise RuntimeError("；".join(errs))
        msg = f"已同步到 {len(done)} 台电脑" if done else "已存档"
        if p["backups"]:
            msg += f"，{len(p['backups'])} 台的旧版放在 {BACKUP_DIR}/"
        return msg

    def _register(self, sid: str, it: dict, machines: list[str]):
        """Self-built skills show up in the「+」menu like the others."""
        custom = self.kv_get("skills_custom", []) or []
        cur = next((c for c in custom if c.get("id") == sid), None)
        if cur is None:
            cur = {"id": sid, "title": it.get("title") or sid, "desc": it.get("desc", ""), "path": f"{SKILLS_DIR}/{sid}",
                   "setup": "", "source": "", "machines": []}
            custom.append(cur)
        cur["self"] = True
        cur["machines"] = sorted(set(cur.get("machines") or []) | set(machines))
        self.kv_set("skills_custom", custom)


# ------------------------------------------------------------------------------------------------------ auto loop
DEFAULT_AUTO = {"sync_self": True, "update_all": False, "hours": 2}


def auto_cfg(kv_get) -> dict:
    return {**DEFAULT_AUTO, **(kv_get("skills_auto", {}) or {})}


async def auto_loop(updater: SkillUpdater, kv_get, first_delay: float = 300):
    await asyncio.sleep(first_delay)
    while True:
        cfg = auto_cfg(kv_get)
        try:
            if cfg.get("update_all"):
                await updater.update(kinds={"builtin", "installed", "self"}, job_key="skills_auto_job")
            elif cfg.get("sync_self"):
                await updater.update(kinds={"self"}, job_key="skills_auto_job")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(max(0.5, float(cfg.get("hours") or 2)) * 3600)
