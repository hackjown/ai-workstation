"""Install a skill on every computer at once (设置 › 技能 › 添加技能).

The user types what they would type in a terminal — `npx skills add openfilm/openfilm`, `openfilm/openfilm`, or a
GitHub URL — and each machine runs `npx skills add <pkg> -g -a claude-code -s '*' -y --copy`, which puts the skill in
~/.claude/skills/<name>. The skills found afterwards are registered in kv "skills_custom" with their path, description
and source, so the "+" menu offers them and 同步 can repeat the install on a machine that was off.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import time
from typing import Any, Callable, Optional

DIR = "~/.claude/skills"
PKG_RE = re.compile(r"^[A-Za-z0-9_.@:/#~+-]{2,200}$")


def parse_source(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^(npx\s+(-y\s+|--yes\s+)?)?skills(@\S+)?\s+(add|a|install|i)\s+", "", t)
    parts = [p for p in t.split() if not p.startswith("-")]
    pkg = parts[0] if parts else ""
    if not PKG_RE.match(pkg):
        raise ValueError("看不懂这个来源，请填 owner/repo、GitHub 链接，或整条 npx skills add 命令")
    return pkg


def script(pkg: str) -> str:
    q = shlex.quote(pkg)
    return f"""export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:$PATH"
d="$HOME/.claude/skills"; mkdir -p "$d"; m=$(mktemp); sleep 1
if ! command -v npx >/dev/null 2>&1; then echo "AIW_ERR 这台电脑没有 Node/npx"; exit 3; fi
npx -y skills add {q} -g -a claude-code -s '*' -y --copy </dev/null 2>&1 | sed 's/\\x1b\\[[0-9;]*[A-Za-z]//g' | grep -v 'npm notice' | tail -25
rc=${{PIPESTATUS[0]}}
for f in "$d"/*/SKILL.md; do [ -f "$f" ] || continue; [ "$f" -nt "$m" ] || [ -n "$(find "$(dirname "$f")" -newer "$m" -print -quit)" ] || continue
  s=$(basename "$(dirname "$f")"); desc=$(awk '/^description:/{{sub(/^description:[ ]*/,"");print;exit}}' "$f" | cut -c1-200)
  echo "AIW_SKILL $s|$desc"; done
rm -f "$m"; echo "AIW_RC $rc"
"""


def check_script(ids: list[str]) -> str:
    return "; ".join(f'[ -f "$HOME/.claude/skills/{i}/SKILL.md" ] && echo "AIW_HAS {i}"' for i in ids
                     if re.match(r"^[A-Za-z0-9_.-]+$", i)) + "; true"


def parse_output(out: str) -> dict:
    skills, rc, err = [], None, ""
    log = []
    for line in (out or "").splitlines():
        if line.startswith("AIW_SKILL "):
            name, _, desc = line[10:].partition("|")
            skills.append({"id": name.strip(), "desc": desc.strip().strip('"')})
        elif line.startswith("AIW_RC "):
            rc = int(line[7:].strip() or 1)
        elif line.startswith("AIW_ERR "):
            err = line[8:]
        else:
            log.append(line)
    return {"skills": skills, "rc": rc, "error": err, "log": "\n".join(log[-25:])}


def merge(custom: list[dict], found: list[dict], pkg: str, mid: str) -> list[dict]:
    out = [dict(s) for s in custom or []]
    for f in found:
        cur = next((s for s in out if s.get("id") == f["id"]), None)
        if cur is None:
            cur = {"id": f["id"], "title": f["id"], "path": f"{DIR}/{f['id']}", "setup": "", "machines": []}
            out.append(cur)
        cur["desc"] = f.get("desc") or cur.get("desc", "")
        cur["source"] = pkg
        cur["path"] = cur.get("path") or f"{DIR}/{f['id']}"
        ms = set(cur.get("machines") or []); ms.add(mid); cur["machines"] = sorted(ms)
    return out


async def install_all(pkg: str, machines: list[dict], shell: Callable, wake: Optional[Callable], kv_get, kv_set,
                      job_key: str = "skills_install_job") -> dict:
    job: dict[str, Any] = {"source": pkg, "started": time.time(), "done": False,
                           "machines": {m["id"]: {"name": m.get("name") or m["id"], "state": "waiting"} for m in machines}}
    kv_set(job_key, job)
    lock = asyncio.Lock()

    async def one(m: dict):
        mid, st = m["id"], job["machines"][m["id"]]
        try:
            if wake:
                st["state"] = "waking"; kv_set(job_key, job)
                await wake(mid)
            st["state"] = "installing"; kv_set(job_key, job)
            r = await shell(mid, script(pkg), 300)
            text = (r.get("stdout") or "") + ("\n" + r["stderr"] if r.get("stderr") else "")
            p = parse_output(text)
            st.update(log=p["log"], skills=[s["id"] for s in p["skills"]])
            if p["error"] or p["rc"] not in (0, None) or not p["skills"]:
                st["state"] = "failed"
                st["error"] = p["error"] or (f"安装命令退出码 {p['rc']}" if p["rc"] else "没有装上任何技能")
            else:
                st["state"] = "ok"
                async with lock:
                    kv_set("skills_custom", merge(kv_get("skills_custom", []) or [], p["skills"], pkg, mid))
        except Exception as e:  # noqa: BLE001
            st["state"], st["error"] = "failed", f"{type(e).__name__}: {str(e)[:200]}"
        kv_set(job_key, job)

    await asyncio.gather(*(one(m) for m in machines))
    job["done"], job["finished"] = True, time.time()
    kv_set(job_key, job)
    return job
