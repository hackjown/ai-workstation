"""Online update: SSH to the Docker host, run `docker compose pull && docker compose up -d --force-recreate`
in the install folder, stream the log back, then check that the running container's AIW_VERSION matches the
newest image built from GitHub main.

The update recreates this very container, so the job runs detached on the host (nohup) and writes its log to
/tmp/aiw-update.log; every status call reads that log over a fresh SSH connection. That way the page keeps
showing progress after the API restarts.
Settings live in kv "update_cfg"; the SSH password is the encrypted secret "update_ssh_password".
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import time
from typing import Optional

import httpx

REPO = os.environ.get("AIW_GITHUB_REPO", "hackjown/ai-workstation")
WORK = ".aiw-update"   # under the login user's home: /tmp's sticky bit stops root (sudo) rewriting the user's files


def _paths(home: str) -> tuple[str, str, str]:
    d = f"{home.rstrip('/')}/{WORK}"
    return f"{d}/update.log", f"{d}/update.rc", f"{d}/update.sh"
SECRET = "update_ssh_password"
DEFAULT = {"host": "", "port": 22, "user": "root", "dir": "~/docker/aiw", "container": "aiw-api", "sudo": False}

STEPS = [("connect", "连接服务器"), ("pull", "拉取新镜像"), ("recreate", "重建容器"), ("start", "等待启动"), ("verify", "校验版本")]


def cfg(store) -> dict:
    c = dict(DEFAULT)
    c.update({k: v for k, v in (store.kv_get("update_cfg", {}) or {}).items() if k in DEFAULT})
    return c


def _script(c: dict) -> str:
    LOG, RC, SCRIPT = _paths(c["home"])
    d = c["dir"].strip() or "."
    cd = f"cd {shlex.quote(d)}"
    name = shlex.quote(c.get("container") or "aiw-api")
    return "\n".join([
        "#!/bin/sh",
        f"rm -f {RC}",
        "echo '##STEP pull'",
        f"{cd} || {{ echo '找不到安装目录 {d}'; echo 2 > {RC}; exit 2; }}",
        "if docker compose version >/dev/null 2>&1; then DC='docker compose'; else DC='docker-compose'; fi",
        "echo \"使用 $DC，目录 $(pwd)\"",
        "$DC pull 2>&1 || { echo 3 > " + RC + "; exit 3; }",
        "echo '##STEP recreate'",
        "$DC up -d --force-recreate 2>&1 || { echo 4 > " + RC + "; exit 4; }",
        "echo '##STEP start'",
        "i=0; while [ $i -lt 60 ]; do",
        f"  s=$(docker inspect -f '{{{{.State.Health.Status}}}}' {name} 2>/dev/null || echo none)",
        "  [ \"$s\" = healthy ] && break; [ \"$s\" = none ] && sleep 5 && break; sleep 2; i=$((i+1)); done",
        f"echo \"容器状态：$(docker inspect -f '{{{{.State.Status}}}} {{{{.State.Health.Status}}}}' {name} 2>/dev/null)\"",
        "echo '##STEP verify'",
        f"echo \"##VERSION $(docker exec {name} printenv AIW_VERSION)\"",
        f"echo 0 > {RC}",
    ]) + "\n"


async def _connect(c: dict, password: Optional[str]):
    import asyncssh  # imported lazily: only the update page needs it
    if not c.get("host"):
        raise ValueError("先填服务器地址")
    return await asyncio.wait_for(asyncssh.connect(
        c["host"], port=int(c.get("port") or 22), username=c.get("user") or "root", password=password or None,
        known_hosts=None, client_keys=None if password else (), connect_timeout=15), 20)


def _sudo(c: dict, cmd: str) -> str:
    return f"sudo -S -p '' sh -c {shlex.quote(cmd)}" if c.get("sudo") else cmd


async def _resolve(conn, c: dict) -> dict:
    """Turn ~/… into an absolute path as the login user (under sudo ~ would be /root)."""
    d = (c.get("dir") or ".").strip()
    r = await conn.run("echo $HOME", timeout=10)
    home = (r.stdout or "").strip() or "/tmp"
    if d.startswith("~"):
        d = home + d[1:]
    return dict(c, dir=d, home=home)


async def test(c: dict, password: Optional[str]) -> dict:
    async with await _connect(c, password) as conn:
        c = await _resolve(conn, c)
        cd = f"cd {shlex.quote(c['dir'])}"
        cmd = (f"{cd} && pwd && ls docker-compose.yml compose.yaml compose.yml docker-compose.yaml 2>/dev/null; "
               f"docker exec {shlex.quote(c.get('container') or 'aiw-api')} printenv AIW_VERSION")
        r = await conn.run(_sudo(c, cmd), input=(password + "\n") if c.get("sudo") and password else None, timeout=30)
        out = (r.stdout or "").strip().splitlines()
        if r.exit_status != 0 and len(out) < 2:
            raise ValueError(((r.stderr or "") + " " + " ".join(out)).strip()[:300] or "命令失败")
        return {"dir": out[0] if out else "", "compose": [x for x in out[1:-1]], "version": out[-1] if len(out) > 1 else ""}


async def start(c: dict, password: Optional[str]) -> dict:
    async with await _connect(c, password) as conn:
        c = await _resolve(conn, c)
        LOG, RC, SCRIPT = _paths(c["home"])
        await conn.run(f"mkdir -p {shlex.quote(c['home'] + '/' + WORK)} && cat > {SCRIPT} && chmod 700 {SCRIPT}", input=_script(c), check=True, timeout=20)
        await conn.run(f"rm -f {LOG} {RC}", timeout=10)   # as the login user, so a root-owned log from a sudo run goes too
        launch = f"nohup sh {SCRIPT} > {LOG} 2>&1 &"
        r = await conn.run(_sudo(c, launch), input=(password + "\n") if c.get("sudo") and password else None, timeout=20)
        if r.exit_status not in (0, None):
            raise ValueError((r.stderr or "启动失败")[:300])
    return {"started_at": time.time()}


async def status(c: dict, password: Optional[str]) -> dict:
    async with await _connect(c, password) as conn:
        c = await _resolve(conn, c)
        LOG, RC, _ = _paths(c["home"])
        r = await conn.run(f"cat {LOG} 2>/dev/null; echo '##RC'; cat {RC} 2>/dev/null", timeout=20)
    text = r.stdout or ""
    log, _, rc = text.partition("##RC")
    rc = rc.strip()
    steps = re.findall(r"^##STEP (\w+)", log, re.M)
    ver = re.findall(r"^##VERSION\s*(\S*)", log, re.M)
    clean = "\n".join(l for l in log.splitlines() if not l.startswith("##"))[-6000:]
    return {"log": clean, "step": steps[-1] if steps else ("connect" if log else ""), "rc": int(rc) if rc.isdigit() else None,
            "done": rc != "", "version": ver[-1] if ver else ""}


async def github_latest() -> dict:
    """Newest commit on main and the newest commit whose image build succeeded (the one `pull` can fetch)."""
    out: dict = {"repo": REPO}
    h = {"Accept": "application/vnd.github+json", "User-Agent": "aiw-updater"}
    async with httpx.AsyncClient(timeout=10, headers=h) as cl:
        try:
            r = await cl.get(f"https://api.github.com/repos/{REPO}/commits/main")
            if r.status_code == 200:
                j = r.json()
                out["head"] = {"sha": j["sha"], "message": (j["commit"]["message"] or "").splitlines()[0][:120],
                               "date": j["commit"]["committer"]["date"]}
            r = await cl.get(f"https://api.github.com/repos/{REPO}/actions/workflows/docker.yml/runs",
                             params={"branch": "main", "per_page": 5})
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                ok = next((x for x in runs if x.get("conclusion") == "success"), None)
                cur = runs[0] if runs else None
                if ok:
                    out["image"] = {"sha": ok["head_sha"], "date": ok.get("updated_at")}
                if cur and cur.get("status") != "completed":
                    out["building"] = {"sha": cur["head_sha"], "status": cur.get("status")}
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return out
