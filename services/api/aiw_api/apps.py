"""v3.9.24 (6) apps: one JSON file per app (idea from ProkBot's single-file plugins; our own format and code).

Manifest ("aiw_app": 1):
  {"aiw_app": 1, "id": "cn-weather", "name": "天气", "version": "1.0.0", "description": "…", "author": "…",
   "kind": "code" | "mcp" | "skill",
   "code":  {"runtime": "python3|bash|node", "entry": "main.py", "files": {"main.py": "…"}, "setup": "pip install --user x",
             "input_schema": {JSON schema of the tool's arguments}, "timeout": 120},
   "mcp":   {"url": "https://…/mcp", "auth": {"type": "none|api_key|bearer|oauth", "header": "Authorization",
             "prefix": "Bearer ", "secret": "<vault name>", "oauth": {"token_url", "client_id", "scope",
             "grant": "client_credentials|refresh_token", "client_secret_secret", "refresh_secret"}}},
   "skill": {"text": "SKILL.md 的内容", "keywords": ["…"], "always": false}}

What an enabled app gives a task:
  code   a tool app_<id>: the files are written to ~/workspace/aiw-apps/<id>/<version>/ on the task's computer, `setup`
         runs once, then the entry runs with the tool arguments as JSON on stdin (and in input.json); stdout is the result
  mcp    the server's tools as mcp_<id>_<tool> (key / token from the vault; OAuth client-credentials or refresh-token
         grants are fetched and cached; the browser authorization-code flow is not built in)
  skill  its text in the task prompt when the request mentions the app (name / id / keywords) or `always` is set
Secrets never travel inside the JSON (only the vault NAME); export strips nothing else.
The agent can author an app with the built-in tool write_app (编写应用): it is saved as a DRAFT, off until the user
switches it on in 设置 › 应用. AIW_APPS=0 turns every app off.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("aiw.apps")
KV = "apps"
KINDS = ("code", "mcp", "skill")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
RUNTIMES = {"python3": "python3", "python": "python3", "bash": "bash", "sh": "bash", "node": "node"}
_TOKENS: dict[str, tuple[float, str]] = {}   # app id -> (expires at, access token)


class AppError(ValueError):
    pass


def enabled_globally() -> bool:
    return os.environ.get("AIW_APPS", "1") != "0"


def validate(m: Any) -> dict:
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError as e:
            raise AppError(f"不是有效的 JSON：{e}")
    if not isinstance(m, dict):
        raise AppError("应用必须是一个 JSON 对象")
    if int(m.get("aiw_app") or 0) != 1:
        raise AppError('缺少 "aiw_app": 1')
    aid = str(m.get("id") or "").strip().lower()
    if not ID_RE.match(aid):
        raise AppError("id 只能用小写字母、数字和 -（2-41 个字符）")
    kind = m.get("kind")
    if kind not in KINDS:
        raise AppError("kind 必须是 code / mcp / skill")
    out = {"aiw_app": 1, "id": aid, "name": str(m.get("name") or aid)[:60], "version": str(m.get("version") or "1.0.0")[:20],
           "description": str(m.get("description") or "")[:500], "author": str(m.get("author") or "")[:60], "kind": kind}
    if kind == "code":
        c = m.get("code") or {}
        files = c.get("files") or {}
        if not isinstance(files, dict) or not files:
            raise AppError("code 应用需要 files（文件名 → 内容）")
        clean = {}
        total = 0
        for name, body in files.items():
            n = str(name).strip().lstrip("/")
            if not n or ".." in n.split("/") or len(n) > 120:
                raise AppError(f"文件名不合法：{name}")
            body = str(body)
            total += len(body)
            clean[n] = body
        if total > 400_000:
            raise AppError("代码总量超过 400 KB")
        entry = str(c.get("entry") or next(iter(clean))).strip()
        if entry not in clean:
            raise AppError("entry 必须是 files 里的一个文件")
        rt = RUNTIMES.get(str(c.get("runtime") or ("python3" if entry.endswith(".py") else "node" if entry.endswith(".js") else "bash")))
        if not rt:
            raise AppError("runtime 只支持 python3 / bash / node")
        schema = c.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            raise AppError("input_schema 必须是 JSON schema 对象")
        out["code"] = {"runtime": rt, "entry": entry, "files": clean, "setup": str(c.get("setup") or "")[:2000],
                       "input_schema": schema, "timeout": max(5, min(int(c.get("timeout") or 120), 1800))}
    elif kind == "mcp":
        c = m.get("mcp") or {}
        url = str(c.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            raise AppError("mcp 应用需要 url（http/https）")
        a = c.get("auth") or {}
        at = a.get("type") or "none"
        if at not in ("none", "api_key", "bearer", "oauth"):
            raise AppError("auth.type 必须是 none / api_key / bearer / oauth")
        for k in ("token", "key", "value", "client_secret", "refresh_token", "password"):
            if a.get(k) or (a.get("oauth") or {}).get(k):
                raise AppError(f"不要把密钥（{k}）写进应用 JSON：保存在保险箱里，这里只写 secret 名字")
        auth = {"type": at, "header": str(a.get("header") or "Authorization")[:60],
                "prefix": str(a.get("prefix") if a.get("prefix") is not None else ("Bearer " if at in ("bearer", "oauth") else ""))[:30],
                "secret": str(a.get("secret") or (f"app_{aid}_key" if at != "none" else ""))[:80]}
        if at == "oauth":
            o = a.get("oauth") or {}
            if not o.get("token_url"):
                raise AppError("oauth 需要 token_url")
            auth["oauth"] = {"token_url": str(o["token_url"])[:300], "client_id": str(o.get("client_id") or "")[:200],
                             "scope": str(o.get("scope") or "")[:300],
                             "grant": o.get("grant") if o.get("grant") in ("client_credentials", "refresh_token") else "client_credentials",
                             "client_secret_secret": str(o.get("client_secret_secret") or f"app_{aid}_client_secret")[:80],
                             "refresh_secret": str(o.get("refresh_secret") or f"app_{aid}_refresh")[:80]}
        out["mcp"] = {"url": url[:500], "auth": auth}
    else:
        c = m.get("skill") or {}
        text = str(c.get("text") or "").strip()
        if not text:
            raise AppError("skill 应用需要 text（技能说明）")
        out["skill"] = {"text": text[:30000], "keywords": [str(k)[:30] for k in (c.get("keywords") or [])][:20],
                        "always": bool(c.get("always"))}
    return out


class Apps:
    def __init__(self, store, secrets):
        self.store, self.secrets = store, secrets

    def _all(self) -> dict:
        return self.store.kv_get(KV, {}) or {}

    def list(self) -> list[dict]:
        out = []
        for aid, rec in sorted(self._all().items(), key=lambda kv: -(kv[1].get("installed_at") or 0)):
            m = rec["manifest"]
            need = self.needed_secrets(m)
            out.append({"id": aid, "name": m["name"], "version": m["version"], "kind": m["kind"], "description": m["description"],
                        "author": m.get("author") or "", "enabled": bool(rec.get("enabled")), "source": rec.get("source") or "import",
                        "installed_at": rec.get("installed_at"), "secrets": [{"name": n, "set": bool(self.secrets.get(n))} for n in need]})
        return out

    def get(self, aid: str) -> Optional[dict]:
        return self._all().get(aid)

    def needed_secrets(self, m: dict) -> list[str]:
        if m.get("kind") != "mcp":
            return []
        a = m["mcp"]["auth"]
        if a["type"] == "none":
            return []
        if a["type"] == "oauth":
            o = a["oauth"]
            return [o["client_secret_secret"]] + ([o["refresh_secret"]] if o["grant"] == "refresh_token" else [])
        return [a["secret"]]

    def save(self, manifest, source: str = "import", enabled: Optional[bool] = None) -> dict:
        m = validate(manifest)
        data = self._all()
        old = data.get(m["id"])
        if enabled is None:   # an update keeps the switch; a new app from the agent starts off
            enabled = bool(old.get("enabled")) if old else source != "agent"
        data[m["id"]] = {"manifest": m, "enabled": bool(enabled), "source": source, "installed_at": time.time()}
        self.store.kv_set(KV, data)
        return {"id": m["id"], "enabled": bool(enabled), "updated": bool(old)}

    def set_enabled(self, aid: str, on: bool) -> bool:
        data = self._all()
        if aid not in data:
            return False
        data[aid]["enabled"] = bool(on)
        self.store.kv_set(KV, data)
        return True

    def delete(self, aid: str) -> bool:
        data = self._all()
        if data.pop(aid, None) is None:
            return False
        self.store.kv_set(KV, data)
        _TOKENS.pop(aid, None)
        return True

    def export(self, aid: str) -> Optional[dict]:
        rec = self.get(aid)
        return json.loads(json.dumps(rec["manifest"])) if rec else None

    # ------------------------------------------------------------ what a task gets
    def active(self) -> list[dict]:
        if not enabled_globally():
            return []
        return [r["manifest"] for r in self._all().values() if r.get("enabled")]

    def prompt_block(self, prompt: str) -> str:
        p = (prompt or "").lower()
        parts = []
        for m in self.active():
            if m["kind"] != "skill":
                continue
            s = m["skill"]
            names = [m["id"], m["name"]] + list(s.get("keywords") or [])
            if s.get("always") or any(n and n.lower() in p for n in names):
                parts.append(f"APP SKILL「{m['name']}」({m['id']} v{m['version']}): follow it for this task.\n{s['text']}")
        listing = [f"- app_{m['id'].replace('-', '_')}: {m['name']} — {m['description'][:120]}" for m in self.active() if m["kind"] == "code"]
        if listing:
            parts.append("INSTALLED APPS (tools you can call):\n" + "\n".join(listing))
        return ("\n\n".join(parts))[:40000]

    async def _auth_headers(self, m: dict) -> dict:
        a = m["mcp"]["auth"]
        if a["type"] == "none":
            return {}
        if a["type"] == "oauth":
            tok = await self._oauth(m)
        else:
            tok = self.secrets.get(a["secret"]) or ""
        if not tok:
            raise AppError(f"应用 {m['id']} 缺少密钥 {a.get('secret') or 'oauth'}")
        return {a["header"]: f"{a['prefix']}{tok}"}

    async def _oauth(self, m: dict) -> str:
        hit = _TOKENS.get(m["id"])
        if hit and hit[0] > time.time() + 30:
            return hit[1]
        o = m["mcp"]["auth"]["oauth"]
        form = {"grant_type": o["grant"], "client_id": o["client_id"]}
        cs = self.secrets.get(o["client_secret_secret"])
        if cs:
            form["client_secret"] = cs
        if o.get("scope"):
            form["scope"] = o["scope"]
        if o["grant"] == "refresh_token":
            form["refresh_token"] = self.secrets.get(o["refresh_secret"]) or ""
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(o["token_url"], data=form)
            r.raise_for_status()
            d = r.json()
        tok = d.get("access_token") or ""
        if d.get("refresh_token") and o["grant"] == "refresh_token":
            self.secrets.put(o["refresh_secret"], d["refresh_token"])
        _TOKENS[m["id"]] = (time.time() + float(d.get("expires_in") or 3600), tok)
        return tok

    async def task_tools(self, computer=None) -> dict:
        """name -> (schema, async call(args)) for every enabled code / mcp app. A broken app is skipped."""
        tools: dict = {}
        for m in self.active():
            try:
                if m["kind"] == "code" and computer is not None:
                    tools[f"app_{m['id'].replace('-', '_')}"] = self._code_tool(m, computer)
                elif m["kind"] == "mcp":
                    from .integrations import MCPClient, _mcp_tools, safe_name
                    headers = await self._auth_headers(m)
                    client = MCPClient(m["mcp"]["url"], headers)
                    for t in await _mcp_tools(client, f"app:{m['id']}:{m['version']}:{m['mcp']['url']}"):
                        name = safe_name("mcp_", m["id"].replace("-", "_"), t["name"])
                        schema = {"name": name, "description": (f"[{m['name']}] " + (t.get("description") or t["name"]))[:900],
                                  "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}

                        async def fn(args, _c=client, _n=t["name"]):
                            return await _c.call(_n, args)
                        tools[name] = (schema, fn)
            except Exception as e:  # noqa: BLE001
                log.warning("app %s unavailable: %s", m.get("id"), e)
        return tools

    def _code_tool(self, m: dict, computer):
        c = m["code"]
        base = f"aiw-apps/{m['id']}/{m['version']}"
        schema = {"name": f"app_{m['id'].replace('-', '_')}",
                  "description": f"应用「{m['name']}」：{m['description'][:600]}（在这台电脑上运行，返回它的输出）",
                  "parameters": c.get("input_schema") or {"type": "object", "properties": {}}}

        async def call(args: dict) -> str:
            for name, body in c["files"].items():
                r = await computer.execute("files.write", {"path": f"{base}/{name}", "content": body}, timeout=30)
                if not r.success:
                    return f"应用文件写入失败：{r.error}"
            inp = json.dumps(args or {}, ensure_ascii=False)
            r = await computer.execute("files.write", {"path": f"{base}/input.json", "content": inp}, timeout=15)
            # the agent's files root (~/workspace unless VM_AGENT_FILES_ROOT says otherwise) is where they landed
            where = str((r.result or {}).get("path") or "") if r.success else ""
            cwd = shlex.quote(where.rsplit("/", 1)[0]) if where.endswith("/input.json") else f"~/workspace/{base}"
            if c.get("setup"):
                setup = f"cd {cwd} && if [ ! -f .aiw-setup-done ]; then ({c['setup']}) && touch .aiw-setup-done; fi"
                r = await computer.execute("shell.exec", {"command": setup, "timeout": 600}, timeout=620)
                if not r.success:
                    return f"应用 setup 失败：{(r.result or {}).get('stderr') or r.error}"[:1500]
            cmd = f"cd {cwd} && AIW_APP_INPUT={shlex.quote(base64.b64encode(inp.encode()).decode())} {c['runtime']} {shlex.quote(c['entry'])} < input.json"
            r = await computer.execute("shell.exec", {"command": cmd, "timeout": c["timeout"]}, timeout=c["timeout"] + 15)
            res = r.result or {}
            out = (res.get("stdout") or "").strip()
            if not r.success:
                return f"应用运行失败（exit {res.get('exit_code')}）：{(res.get('stderr') or r.error or '')[-1500:]}\n{out[-1500:]}"
            return out[-6000:] or "(没有输出)"
        call.max_result = 6000
        return schema, call

    WRITE_TOOL = {"name": "write_app",
                  "description": ("编写应用：把一个功能做成 AIW 应用（单个 JSON 文件）并保存为草稿，用户在「设置 › 应用」里查看并启用后，以后的任务就能直接用。"
                                  "manifest 字段：aiw_app=1, id(小写字母数字-), name, version, description, kind=code|mcp|skill；"
                                  "code: {runtime: python3|bash|node, entry, files: {文件名: 内容}, setup?, input_schema, timeout?}（参数以 JSON 从 stdin 传入，stdout 是结果）；"
                                  "mcp: {url, auth: {type: none|api_key|bearer|oauth, secret: 保险箱里的名字}}（绝不写入密钥本身）；"
                                  "skill: {text: 技能说明, keywords: [...]}。先在电脑上把代码跑通再保存。"),
                  "parameters": {"type": "object", "properties": {
                      "manifest": {"type": "object", "description": "完整的应用 JSON"}}, "required": ["manifest"]}}

    def write_tool(self, task_id: str = ""):
        async def call(a: dict) -> str:
            m = (a or {}).get("manifest")
            try:
                res = self.save(m, source="agent")
            except AppError as e:
                return f"应用没有保存：{e}。改好后再调用 write_app。"
            state = "已启用（更新了已启用的同名应用）" if res["enabled"] else "已保存为草稿，需用户在 设置 › 应用 里启用"
            return f"应用 {res['id']} {state}。在总结里告诉用户应用名称和用途。"
        return self.WRITE_TOOL, call


def mount(app, apps: Apps, auth) -> None:
    from fastapi import Body, Depends, HTTPException
    from fastapi.responses import Response

    @app.get("/api/apps", dependencies=[Depends(auth)])
    async def apps_list():
        return {"apps": apps.list(), "enabled": enabled_globally()}

    @app.post("/api/apps/import", dependencies=[Depends(auth)])
    async def apps_import(body: dict = Body(...)):
        b = body or {}
        manifest = b.get("manifest") if "manifest" in b else b
        try:
            return apps.save(manifest, source="import", enabled=b.get("enabled") if "manifest" in b else None)
        except AppError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/apps/validate", dependencies=[Depends(auth)])
    async def apps_validate(body: dict = Body(...)):
        try:
            m = validate((body or {}).get("manifest") if "manifest" in (body or {}) else body)
            return {"ok": True, "id": m["id"], "kind": m["kind"], "secrets": apps.needed_secrets(m)}
        except AppError as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/apps/{aid}", dependencies=[Depends(auth)])
    async def apps_get(aid: str):
        rec = apps.get(aid)
        if not rec:
            raise HTTPException(404, "no such app")
        return {**rec, "secrets": [{"name": n, "set": bool(apps.secrets.get(n))} for n in apps.needed_secrets(rec["manifest"])]}

    @app.get("/api/apps/{aid}/export", dependencies=[Depends(auth)])
    async def apps_export(aid: str):
        m = apps.export(aid)
        if not m:
            raise HTTPException(404, "no such app")
        return Response(json.dumps(m, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{aid}.aiwapp.json"'})

    @app.put("/api/apps/{aid}/enabled", dependencies=[Depends(auth)])
    async def apps_enable(aid: str, body: dict = Body(...)):
        if not apps.set_enabled(aid, bool((body or {}).get("enabled"))):
            raise HTTPException(404, "no such app")
        return {"ok": True}

    @app.delete("/api/apps/{aid}", dependencies=[Depends(auth)])
    async def apps_delete(aid: str):
        if not apps.delete(aid):
            raise HTTPException(404, "no such app")
        return {"ok": True}

    @app.post("/api/apps/{aid}/test", dependencies=[Depends(auth)])
    async def apps_test(aid: str):
        """An MCP app: connect and list its tools. A skill / code app: validation only (code runs inside tasks)."""
        rec = apps.get(aid)
        if not rec:
            raise HTTPException(404, "no such app")
        m = rec["manifest"]
        if m["kind"] != "mcp":
            return {"ok": True, "note": "已通过格式检查；代码应用在任务里运行"}
        try:
            from .integrations import MCPClient
            client = MCPClient(m["mcp"]["url"], await apps._auth_headers(m))
            import asyncio
            tools = await asyncio.wait_for(client.list_tools(), 12)
            return {"ok": True, "tools": [t.get("name") for t in tools]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
