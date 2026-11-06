"""v3.9.24 自动签到 (daily check-in on sites you name; no site is preconfigured).

Per site: a schedule (cron.py syntax, default `daily 08:30`) plus a random delay of 0–N minutes (default 15) so the
run never lands on the same second every day.

  first run   an agent task signs in on the site (on the chosen computer's logged-in browser), checks in and saves a
              replayable RECIPE with the built-in tool checkin_save_recipe
  later runs  the recipe is replayed without the model: browser steps in the computer's Chrome (open / click by text /
              fill / wait / expect page text) or HTTP requests from the AIW server (cookie / token from the vault)
  replay fails  -> an agent task does it this time (and may save a better recipe)
  captcha / slider / SMS code -> never bypassed: the agent asks you to take over (接管卡片) and a push says so
One Bark push per batch (all sites that ran in the same scheduler tick): 「自动签到 3/4 成功」 + one line per site.

Recipe JSON:
  {"kind": "browser", "steps": [{"op": "open", "url": "…"}, {"op": "click", "text": "签到"},
      {"op": "fill", "field": "…", "text": "…"}, {"op": "wait", "s": 2},
      {"op": "expect", "contains": "签到成功|已签到"}]}
  {"kind": "http", "cookie_secret": "checkin_<site>_cookie", "steps": [{"method": "POST", "url": "…",
      "headers": {"X-Token": "{{secret:name}}"}, "json": {…} | "body": "…",
      "expect": {"status": 200, "contains": "成功|已签到", "json_path": "code", "equals": 0}}]}

  GET/POST /api/checkin/sites · PUT/DELETE /api/checkin/sites/{id} · POST /api/checkin/sites/{id}/run
  POST /api/checkin/sites/{id}/rerecord (drop the recipe; next run records again) · GET /api/checkin/runs?site_id=
  GET/PUT /api/checkin/settings {enabled, jitter_min}
AIW_CHECKIN=0 stops scheduled runs (manual 立即签到 still works).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import time
import uuid
from typing import Optional

import httpx

from . import cron

log = logging.getLogger("aiw.checkin")
SCHEMA = """CREATE TABLE IF NOT EXISTS checkin_sites (id TEXT PRIMARY KEY, name TEXT, url TEXT, note TEXT, enabled INTEGER DEFAULT 1,
  schedule TEXT, jitter_min INTEGER, machine_id TEXT, recipe TEXT, recipe_at REAL, next_run REAL, last_run REAL,
  last_status TEXT, last_message TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS checkin_runs (id TEXT PRIMARY KEY, site_id TEXT, batch_id TEXT, ts REAL, mode TEXT, status TEXT,
  message TEXT, task_id TEXT);
CREATE INDEX IF NOT EXISTS ix_checkin_runs ON checkin_runs(site_id, ts);"""
KV = "checkin_settings"
HUMAN_RE = re.compile(r"验证码|滑块|拖动.{0,6}(完成|验证)|人机|captcha|recaptcha|hcaptcha|geetest|极验|slider|安全验证|短信验证", re.I)
OK_STATUSES = ("ok", "already")


def settings(store) -> dict:
    return {"enabled": True, "jitter_min": 15, **(store.kv_get(KV, {}) or {})}


def _json(v, default=None):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def validate_recipe(r) -> dict:
    if isinstance(r, str):
        r = _json(r)
    if not isinstance(r, dict) or r.get("kind") not in ("browser", "http"):
        raise ValueError("recipe 需要 kind = browser 或 http")
    steps = r.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > 40:
        raise ValueError("recipe.steps 需要 1-40 步")
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("每一步都要是对象")
        if r["kind"] == "browser":
            if s.get("op") not in ("open", "click", "fill", "wait", "expect"):
                raise ValueError(f"不认识的浏览器步骤：{s.get('op')}")
            if s["op"] == "open" and not re.match(r"^https?://", str(s.get("url") or "")):
                raise ValueError("open 需要 http(s) url")
        else:
            if not re.match(r"^https?://", str(s.get("url") or "")):
                raise ValueError("HTTP 步骤需要 url")
    blob = json.dumps(r, ensure_ascii=False)
    if re.search(r"(?i)\b(password|passwd)\b\s*[\"']?\s*[:=]", blob):
        raise ValueError("recipe 里不能有密码：用 {{secret:名字}} 引用保险箱")
    return r


def is_ok_text(text: str, pat: str) -> bool:
    try:
        return re.search(pat, text or "", re.I) is not None
    except re.error:
        return (pat or "") in (text or "")


PAGE_TEXT_PY = r'''
import asyncio, json
from vm_agent.browser.cdp import CDP
async def main():
    c = CDP()
    async with await c.session() as ws:
        v = await c._eval(ws, "(document.title||'') + '\n' + location.href + '\n' + (document.body ? document.body.innerText : '')")
    print((v or '')[:8000])
asyncio.run(main())
'''


class Checkin:
    def __init__(self, store, sessions, notifier, secrets):
        self.store, self.sessions, self.notifier, self.secrets = store, sessions, notifier, secrets
        self.running: set[str] = set()
        self._batches: dict[str, dict] = {}   # batch id -> {"sites": n, "results": [...]}
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()
        sessions.listeners.append(self.on_task_event)

    # ------------------------------------------------------------ sites
    def sites(self) -> list[dict]:
        rows = self.store.all("checkin_sites", order="created_at ASC")
        for r in rows:
            r["has_recipe"] = bool(r.get("recipe"))
            r["recipe"] = _json(r.get("recipe"))
        return rows

    def site(self, sid: str) -> Optional[dict]:
        r = self.store.get("checkin_sites", sid)
        if r:
            r["has_recipe"] = bool(r.get("recipe"))
            r["recipe"] = _json(r.get("recipe"))
        return r

    def next_time(self, schedule: str, jitter_min: int, now: float) -> Optional[float]:
        try:
            base = cron.next_run(schedule or "daily 08:30", now)
        except ValueError:
            return None
        return base + random.uniform(0, max(0, int(jitter_min)) * 60) if base else None

    def add(self, body: dict) -> dict:
        name, url = str(body.get("name") or "").strip()[:60], str(body.get("url") or "").strip()[:500]
        if not re.match(r"^https?://", url):
            raise ValueError("网址要以 http:// 或 https:// 开头")
        schedule = str(body.get("schedule") or "daily 08:30").strip()
        cron.next_run(schedule, time.time())   # ValueError on a bad schedule
        jit = int(body.get("jitter_min") if body.get("jitter_min") is not None else settings(self.store)["jitter_min"])
        row = {"name": name or re.sub(r"^https?://(www\.)?", "", url).split("/")[0], "url": url,
               "note": str(body.get("note") or "")[:500], "enabled": 1 if body.get("enabled", True) else 0,
               "schedule": schedule, "jitter_min": max(0, min(jit, 180)), "machine_id": body.get("machine_id") or None}
        row["next_run"] = self.next_time(schedule, row["jitter_min"], time.time())
        if body.get("recipe"):
            row["recipe"] = json.dumps(validate_recipe(body["recipe"]), ensure_ascii=False)
            row["recipe_at"] = time.time()
        return self.site(self.store.insert("checkin_sites", row)["id"])

    def edit(self, sid: str, body: dict) -> Optional[dict]:
        cur = self.store.get("checkin_sites", sid)
        if not cur:
            return None
        f = {}
        for k in ("name", "url", "note", "machine_id"):
            if k in body:
                f[k] = str(body[k] or "")[:500] or (None if k == "machine_id" else "")
        if "url" in f and not re.match(r"^https?://", f["url"]):
            raise ValueError("网址要以 http:// 或 https:// 开头")
        if "enabled" in body:
            f["enabled"] = 1 if body["enabled"] else 0
        if "schedule" in body or "jitter_min" in body:
            sch = str(body.get("schedule") or cur["schedule"])
            cron.next_run(sch, time.time())
            jit = max(0, min(int(body.get("jitter_min", cur.get("jitter_min") or 0)), 180))
            f.update(schedule=sch, jitter_min=jit, next_run=self.next_time(sch, jit, time.time()))
        if "recipe" in body:
            f["recipe"] = json.dumps(validate_recipe(body["recipe"]), ensure_ascii=False) if body["recipe"] else None
            f["recipe_at"] = time.time() if body["recipe"] else None
        self.store.update("checkin_sites", sid, **f)
        return self.site(sid)

    def delete(self, sid: str) -> None:
        self.store.delete("checkin_sites", sid)
        self.store._x("DELETE FROM checkin_runs WHERE site_id=?", (sid,))

    def runs(self, site_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if site_id:
            return self.store.all("checkin_runs", "site_id=?", (site_id,), "ts DESC", limit)
        return self.store.all("checkin_runs", order="ts DESC", limit=limit)

    def _run_row(self, site: dict, batch: str, mode: str, status: str, message: str, task_id: str = "") -> dict:
        row = {"id": "cr-" + uuid.uuid4().hex[:10], "site_id": site["id"], "batch_id": batch, "ts": time.time(), "mode": mode,
               "status": status, "message": (message or "")[:500], "task_id": task_id}
        self.store._x("INSERT INTO checkin_runs VALUES (?,?,?,?,?,?,?,?)", tuple(row.values()))
        self.store.update("checkin_sites", site["id"], last_run=row["ts"], last_status=status, last_message=row["message"])
        return row

    # ------------------------------------------------------------ scheduling
    async def tick(self, now: Optional[float] = None) -> Optional[str]:
        if os.environ.get("AIW_CHECKIN", "1") == "0" or not settings(self.store).get("enabled"):
            return None
        now = now or time.time()
        due = [s for s in self.sites() if s.get("enabled") and s.get("next_run") and s["next_run"] <= now
               and s["id"] not in self.running]
        if not due:
            return None
        for s in due:   # move next_run first so a slow batch never fires twice
            self.store.update("checkin_sites", s["id"], next_run=self.next_time(s["schedule"], s.get("jitter_min") or 0, now + 60))
        batch = "cb-" + uuid.uuid4().hex[:8]
        task = asyncio.create_task(self.run_batch(due, batch))
        self._batches[batch] = {"sites": len(due), "results": {}, "task": task}
        return batch

    async def run_batch(self, sites: list[dict], batch: str) -> list[dict]:
        self._batches.setdefault(batch, {"sites": len(sites), "results": {}})
        for s in sites:   # one after the other: they share the computer's browser
            self.running.add(s["id"])
            try:
                res = await self.run_site(s, batch)
            except Exception as e:  # noqa: BLE001
                res = self._run_row(s, batch, "replay", "failed", f"{type(e).__name__}: {e}")
            finally:
                self.running.discard(s["id"])
            if res.get("status") != "pending":
                self._batches[batch]["results"][s["id"]] = (s, res)
        await self._maybe_push(batch)
        return [r for _, r in self._batches.get(batch, {}).get("results", {}).values()]

    async def run_site(self, s: dict, batch: str) -> dict:
        recipe = s.get("recipe")
        if not recipe:
            return await self._agent(s, batch, "record")
        ok, status, msg = await self.replay(s, recipe)
        if ok:
            return self._run_row(s, batch, "replay", status, msg)
        if status == "human":
            self._run_row(s, batch, "replay", "human", msg)
            return await self._agent(s, batch, "escalate", why=msg, human=True)
        self._run_row(s, batch, "replay", "replay_failed", msg)
        return await self._agent(s, batch, "escalate", why=msg)

    # ------------------------------------------------------------ replay
    async def replay(self, s: dict, recipe: dict) -> tuple[bool, str, str]:
        """-> (ok, status ok|already|failed|human, message)."""
        try:
            if recipe["kind"] == "http":
                return await self._replay_http(s, recipe)
            return await self._replay_browser(s, recipe)
        except Exception as e:  # noqa: BLE001
            return False, "failed", f"{type(e).__name__}: {e}"[:300]

    async def _replay_http(self, s: dict, r: dict) -> tuple[bool, str, str]:
        cookie = self.secrets.get(r["cookie_secret"]) if r.get("cookie_secret") else None
        last = ""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}) as c:
            for st in r["steps"]:
                h = {k: self.secrets.expand(str(v)) for k, v in (st.get("headers") or {}).items()}
                if cookie and not any(k.lower() == "cookie" for k in h):
                    h["Cookie"] = cookie
                kw = {"headers": h}
                if st.get("json") is not None:
                    kw["json"] = json.loads(self.secrets.expand(json.dumps(st["json"], ensure_ascii=False)))
                elif st.get("body") is not None:
                    kw["content"] = self.secrets.expand(str(st["body"])).encode()
                resp = await c.request(str(st.get("method") or "GET").upper(), self.secrets.expand(st["url"]), **kw)
                text = resp.text[:20000]
                last = text
                if HUMAN_RE.search(text) and not is_ok_text(text, (st.get("expect") or {}).get("contains") or "^$"):
                    return False, "human", "网站要求人工验证（验证码/滑块）"
                ex = st.get("expect") or {}
                if ex.get("status") and resp.status_code != int(ex["status"]):
                    return False, "failed", f"HTTP {resp.status_code}（期望 {ex['status']}）：{text[:160]}"
                if not ex.get("status") and resp.status_code >= 400:
                    return False, "failed", f"HTTP {resp.status_code}：{text[:160]}"
                if ex.get("json_path"):
                    cur = _json(text, {})
                    for part in str(ex["json_path"]).split("."):
                        cur = cur.get(part) if isinstance(cur, dict) else None
                    if "equals" in ex and cur != ex["equals"] and str(cur) != str(ex["equals"]):
                        return False, "failed", f"{ex['json_path']}={cur!r}（期望 {ex['equals']!r}）：{text[:160]}"
                if ex.get("contains") and not is_ok_text(text, ex["contains"]):
                    return False, "failed", f"返回里没有「{ex['contains']}」：{text[:160]}"
        already = bool(re.search(r"已签到|已经签到|already", last, re.I))
        return True, "already" if already else "ok", "今天已经签过" if already else "签到成功"

    async def _page_text(self, computer) -> str:
        cmd = ("cd ~/ai-workstation 2>/dev/null && PYTHONPATH=protocols:vm-agent "
               "\"$( [ -x ~/.aiw-venv/bin/python ] && echo ~/.aiw-venv/bin/python || echo python3 )\" - <<'AIWPY'\n"
               + PAGE_TEXT_PY + "\nAIWPY")
        r = await computer.execute("shell.exec", {"command": cmd, "timeout": 30}, timeout=40)
        return ((r.result or {}).get("stdout") or "") if r.success else ""

    async def _replay_browser(self, s: dict, r: dict) -> tuple[bool, str, str]:
        mid = s.get("machine_id") or (self.sessions.settings.machines[0].id if self.sessions.settings.machines else None)
        if not mid:
            return False, "failed", "没有可用的电脑"
        if self.sessions.busy_session(mid):
            return False, "failed", "这台电脑正在跑别的任务"
        cloud = getattr(self.sessions, "cloud", None)
        if cloud and cloud.is_cloud(mid):
            await cloud.wake(mid)
        computer = self.sessions.computer(mid)
        saw = ""
        for st in r["steps"]:
            op = st["op"]
            if op == "wait":
                await asyncio.sleep(min(float(st.get("s") or 1), 30))
                continue
            if op == "expect":
                await asyncio.sleep(1.5)
                saw = await self._page_text(computer)
                if HUMAN_RE.search(saw) and not is_ok_text(saw, st.get("contains") or "^$"):
                    return False, "human", "网站要求人工验证（验证码/滑块）"
                if not saw:
                    return False, "failed", "读不到页面文字（VM 上的 vm-agent 太旧？跑一次 update-agent.sh）"
                if not is_ok_text(saw, st.get("contains") or "签到成功|已签到"):
                    return False, "failed", f"页面里没有「{st.get('contains')}」：{' '.join(saw.split())[:160]}"
                continue
            action, payload = {"open": ("browser.open", {"url": st.get("url")}),
                               "click": ("browser.click", {"text": st.get("text") or ""}),
                               "fill": ("browser.fill", {"field": st.get("field") or "",
                                                         "text": self.secrets.expand(str(st.get("text") or "")),
                                                         "submit": bool(st.get("submit"))})}[op]
            res = await computer.execute(action, payload, timeout=60)
            if not res.success:
                err = res.error or ""
                if HUMAN_RE.search(err):
                    return False, "human", "网站要求人工验证（验证码/滑块）"
                return False, "failed", f"{op} 失败：{err[:200]}"
            await asyncio.sleep(float(st.get("after_s") or 1.5))
        already = bool(re.search(r"已签到|已经签到|明天再来|already", saw, re.I))
        return True, "already" if already else "ok", "今天已经签过" if already else "签到成功"

    # ------------------------------------------------------------ the agent
    def prompt(self, s: dict, mode: str, why: str = "", human: bool = False) -> str:
        head = f"给网站「{s['name']}」（{s['url']}）做每日签到。"
        if s.get("note"):
            head += f"\n用户的说明：{s['note']}"
        if mode == "escalate":
            head += f"\n这次按已保存的签到配方自动重放失败了：{why}。请你手动完成今天的签到，并保存一个更可靠的新配方。"
        if human:
            head += "\n网站要求人工验证：直接请用户接管桌面完成验证（用接管请求，说明要做什么），不要尝试识别或绕过验证码/滑块。"
        return head + (
            "\n要求：\n1. 用这台电脑上已登录的浏览器打开网站；没有登录就请用户接管登录（不要索要或记录密码）。"
            "\n2. 找到签到入口完成签到；页面显示「已签到/今天已签」也算完成。"
            "\n3. 遇到验证码、滑块、短信验证：请用户接管，绝不尝试绕过。"
            "\n4. 完成后必须调用 checkin_save_recipe 保存可重放的配方：优先 kind=browser（步骤 open/click(按钮文字)/fill/wait/expect(成功后页面上会出现的文字，正则)），"
            "只有在签到就是一个不需要页面的接口请求、并且登录凭据已在保险箱里时才用 kind=http。配方里不要写密码和 cookie 值。"
            "\n5. 最后用一句话汇报：成功 / 已签过 / 失败（原因）。")

    async def _agent(self, s: dict, batch: str, mode: str, why: str = "", human: bool = False) -> dict:
        res = await self.sessions.submit(self.prompt(s, mode, why, human), s.get("machine_id") or None,
                                         source="checkin", title=f"✅ 签到 · {s['name']}")
        tid = res["task"]["id"]
        self.store.kv_set(f"checkin_task:{tid}", {"site_id": s["id"], "batch": batch, "mode": mode})
        if human:
            try:
                await self.notifier.notify("input", f"签到需要你：{s['name']}", "网站要求人工验证，打开任务接管桌面完成", task_id=tid,
                                           thread_id=res["task"].get("thread_id"))
            except Exception as e:  # noqa: BLE001
                log.warning("checkin push: %s", e)
        row = self._run_row(s, batch, "agent_" + mode, "pending", "交给 Agent：" + (why or "首次签到，录制配方"), tid)
        return {**row, "status": "pending"}

    def tool(self, task_id: str):
        """checkin_save_recipe, offered to check-in tasks only."""
        schema = {"name": "checkin_save_recipe",
                  "description": ("保存这个网站的签到配方，以后每天自动重放（不用模型）。kind=browser: steps 由 "
                                  "{op:'open',url} {op:'click',text:'按钮上的字'} {op:'fill',field,text} {op:'wait',s} "
                                  "{op:'expect',contains:'签到成功|已签到'} 组成；kind=http: {cookie_secret, steps:[{method,url,headers,json|body,"
                                  "expect:{status,contains,json_path,equals}}]}。同时报告今天的结果 result=ok|already|failed。"),
                  "parameters": {"type": "object", "properties": {
                      "recipe": {"type": "object"},
                      "result": {"type": "string", "enum": ["ok", "already", "failed"]},
                      "message": {"type": "string"}}, "required": ["recipe", "result"]}}

        async def call(a: dict) -> str:
            meta = self.store.kv_get(f"checkin_task:{task_id}") or {}
            site = self.store.get("checkin_sites", meta.get("site_id") or "")
            if not site:
                return "这个任务不属于任何签到站点。"
            try:
                rec = validate_recipe((a or {}).get("recipe"))
            except ValueError as e:
                return f"配方没有保存：{e}。改好再调用一次。"
            self.store.update("checkin_sites", site["id"], recipe=json.dumps(rec, ensure_ascii=False), recipe_at=time.time())
            meta["result"] = (a or {}).get("result") or "ok"
            meta["message"] = str((a or {}).get("message") or "")[:300]
            self.store.kv_set(f"checkin_task:{task_id}", meta)
            return "配方已保存，明天起自动重放。"
        return schema, call

    async def on_task_event(self, task, sid: str, msg: dict) -> None:
        if msg.get("type") != "task.end":
            return
        meta = self.store.kv_get(f"checkin_task:{task.id}")
        if not meta:
            return
        site = self.site(meta["site_id"])
        if not site:
            return
        st = msg.get("status")
        result = meta.get("result") or ("ok" if st == "succeeded" else "failed")
        if st != "succeeded":
            result = "failed"
        text = meta.get("message") or (msg.get("result") or "")[:200] or {"ok": "签到成功", "already": "今天已经签过"}.get(result, "签到失败")
        self.store._x("UPDATE checkin_runs SET status=?, message=? WHERE task_id=?", (result, text[:500], task.id))
        self.store.update("checkin_sites", site["id"], last_status=result, last_message=text[:500], last_run=time.time())
        b = self._batches.get(meta.get("batch") or "")
        if b is not None:
            b["results"][site["id"]] = (site, {"status": result, "message": text, "mode": "agent"})
            await self._maybe_push(meta["batch"])

    async def _maybe_push(self, batch: str) -> None:
        b = self._batches.get(batch)
        if not b or b.get("pushed") or len(b["results"]) < b["sites"]:
            return
        b["pushed"] = True
        res = list(b["results"].values())
        ok = sum(1 for _, r in res if r.get("status") in OK_STATUSES)
        icon = {"ok": "✅", "already": "☑️", "human": "🖐", "failed": "❌", "replay_failed": "❌"}
        lines = [f"{icon.get(r.get('status'), '•')} {s['name']}：{(r.get('message') or r.get('status') or '')[:60]}" for s, r in res]
        try:
            await self.notifier.notify("checkin", f"自动签到 {ok}/{len(res)} 成功", "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log.warning("checkin summary push: %s", e)
        self._batches.pop(batch, None)


def mount(app, ck: Checkin, auth) -> None:
    from fastapi import Body, Depends, HTTPException

    @app.get("/api/checkin/sites", dependencies=[Depends(auth)])
    async def ck_sites():
        return {"sites": ck.sites(), "settings": settings(ck.store), "env_off": os.environ.get("AIW_CHECKIN", "1") == "0"}

    @app.post("/api/checkin/sites", dependencies=[Depends(auth)])
    async def ck_add(body: dict = Body(...)):
        try:
            return ck.add(body or {})
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/checkin/sites/{sid}", dependencies=[Depends(auth)])
    async def ck_edit(sid: str, body: dict = Body(...)):
        try:
            r = ck.edit(sid, body or {})
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not r:
            raise HTTPException(404, "no such site")
        return r

    @app.delete("/api/checkin/sites/{sid}", dependencies=[Depends(auth)])
    async def ck_del(sid: str):
        ck.delete(sid)
        return {"ok": True}

    @app.post("/api/checkin/sites/{sid}/run", dependencies=[Depends(auth)])
    async def ck_run(sid: str):
        s = ck.site(sid)
        if not s:
            raise HTTPException(404, "no such site")
        if sid in ck.running:
            raise HTTPException(409, "正在签到")
        batch = "cb-" + uuid.uuid4().hex[:8]
        ck._batches[batch] = {"sites": 1, "results": {}}
        res = await ck.run_batch([s], batch)
        return {"batch": batch, "results": res, "pending": not res}

    @app.post("/api/checkin/sites/{sid}/rerecord", dependencies=[Depends(auth)])
    async def ck_rerecord(sid: str):
        if not ck.store.get("checkin_sites", sid):
            raise HTTPException(404, "no such site")
        ck.store.update("checkin_sites", sid, recipe=None, recipe_at=None)
        return ck.site(sid)

    @app.get("/api/checkin/runs", dependencies=[Depends(auth)])
    async def ck_runs(site_id: Optional[str] = None, limit: int = 50):
        return ck.runs(site_id, max(1, min(limit, 300)))

    @app.get("/api/checkin/settings", dependencies=[Depends(auth)])
    async def ck_settings():
        return settings(ck.store)

    @app.put("/api/checkin/settings", dependencies=[Depends(auth)])
    async def ck_settings_put(body: dict = Body(...)):
        cur = settings(ck.store)
        if "enabled" in (body or {}):
            cur["enabled"] = bool(body["enabled"])
        if "jitter_min" in (body or {}):
            cur["jitter_min"] = max(0, min(int(body["jitter_min"]), 180))
        ck.store.kv_set(KV, cur)
        return cur
