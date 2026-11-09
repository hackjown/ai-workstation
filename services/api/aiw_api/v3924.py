"""v3.9.24 wiring: mounts the new modules and gives the session manager its hooks (sessions.v3924).

  (2) autoroute     GET/PUT /api/autoroute · POST /api/autoroute/test {text}
  (3) autoreview    /api/autoreview*
  (4) webhooks      /api/webhooks*
  (5) usage         /api/usage*
  (6) apps          /api/apps*
  (7) prompts       /api/prompts*
  (8) model_context GET/PUT /api/model_context
  (9) teach         /api/teach*
  (11) remote       /api/remote*
  自动签到 checkin  /api/checkin*
Each one is off or empty until configured; nothing here changes a task when its settings are untouched.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("aiw.v3924")


class Ext:
    """What SessionManager calls into (all optional: a missing piece never blocks a task)."""

    def __init__(self, store, sessions, chat=None):
        self.store, self.sessions, self.chat = store, sessions, chat
        self.reviewer = None
        self.apps = None
        self.checkin = None

    def small_llm(self):
        if getattr(self.sessions, "_custom_llm", None):   # a test's scripted factory: never spend its scripts here
            return None
        try:
            if self.chat is not None:
                return self.chat.llm("small")
        except Exception:  # noqa: BLE001
            pass
        return None

    def review_llm(self, model: str = ""):
        if self.sessions._custom_llm:
            return None
        if not model:
            return self.small_llm()
        from llm_router import LLMRouter
        m = self.sessions.model_settings()
        from . import providers as _pv   # v3.9.33
        back = m.get("small_model") or m.get("model")
        return _pv.router(self.sessions.store, self.sessions.secrets,
                          [(model, ""), (back, "small_model" if back == m.get("small_model") else "model")],
                          None, "off", role="review", m=m)

    async def review(self, tool, payload, summary, task) -> Optional[dict]:
        if self.reviewer is None:
            return None
        return await self.reviewer.decide(tool, payload, summary, task.prompt or "", task.id)

    async def task_tools(self, task, computer) -> dict:
        out: dict = {}
        if self.apps is not None:
            from . import apps as _apps
            if _apps.enabled_globally():
                out.update(await self.apps.task_tools(computer))
                out["write_app"] = self.apps.write_tool(task.id)
        if self.checkin is not None and self.store.kv_get(f"checkin_task:{task.id}"):
            out["checkin_save_recipe"] = self.checkin.tool(task.id)
        return out

    def prompt_extra(self, task) -> str:
        if self.apps is None:
            return ""
        block = self.apps.prompt_block(task.prompt or "")
        return ("\n" + block) if block else ""


def mount(app, store, sessions, scheduler, notifier, secrets, settings, chat, auth) -> Ext:
    from fastapi import Body, Depends, HTTPException

    ext = Ext(store, sessions, chat)
    sessions.v3924 = ext

    # (5) usage — first, so every later model call is counted
    from . import usage as _usage
    usage = _usage.Usage(store)
    usage.install()
    _usage.mount(app, usage, auth)
    app.state.usage = usage

    # (2) auto routing
    from . import autoroute

    @app.get("/api/autoroute", dependencies=[Depends(auth)])
    async def ar_get():
        return {**autoroute.cfg(store), "env": os.environ.get("AIW_AUTOROUTE", ""), "active": autoroute.on(store)}

    @app.put("/api/autoroute", dependencies=[Depends(auth)])
    async def ar_put(body: dict = Body(...)):
        try:
            return autoroute.save(store, body or {})
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/autoroute/test", dependencies=[Depends(auth)])
    async def ar_test(body: dict = Body(...)):
        text = str((body or {}).get("text") or "")
        c = autoroute.cfg(store)
        tier, why, sure = autoroute.by_rules(text, c)
        out = {"rules": {"tier": tier, "reason": why, "sure": sure}}
        if c.get("use_model") and not sure:
            got = await autoroute.by_model(ext.small_llm(), text) if ext.small_llm() is not None else None
            out["model"] = {"tier": got[0], "reason": got[1]} if got else None
        t = (out.get("model") or out["rules"])["tier"]
        out["route"] = {"tier": t, "tier_cn": autoroute.TIER_CN.get(t), **(c["tiers"].get(t) or {})}
        return out

    # (3) auto review
    from . import autoreview
    ext.reviewer = autoreview.Reviewer(store, ext.review_llm)
    autoreview.mount(app, ext.reviewer, auth)

    # (4) webhooks
    from . import webhooks
    hooks = webhooks.Webhooks(store, secrets, sessions)
    webhooks.mount(app, hooks, auth)
    app.state.webhooks = hooks

    # (6) apps
    from . import apps as _apps
    ext.apps = _apps.Apps(store, secrets)
    _apps.mount(app, ext.apps, auth)

    # (7) prompts
    from agent_runtime import prompts as _P
    from . import prompts_api
    _P.set_trace_sink(prompts_api.trace_sink(store))
    prompts_api.mount(app, store, auth)

    # (8) per-model context
    from agent_runtime import context as _ctx

    def push_ctx():
        _ctx.set_config(store.kv_get("model_context", {}) or {})
    push_ctx()

    @app.get("/api/model_context", dependencies=[Depends(auth)])
    async def mc_get():
        return {"models": store.kv_get("model_context", {}) or {}, "off": os.environ.get("AIW_CONTEXT_MANAGE", "1") == "0"}

    @app.put("/api/model_context", dependencies=[Depends(auth)])
    async def mc_put(body: dict = Body(...)):
        clean = {}
        for name, c in ((body or {}).get("models") or {}).items():
            if not isinstance(c, dict) or not str(name).strip():
                continue
            row = {}
            for k, lo, hi in (("window", 1000, 4_000_000), ("trigger_tokens", 500, 4_000_000), ("keep_last", 2, 23)):
                if c.get(k) not in (None, "", 0):
                    try:
                        row[k] = max(lo, min(int(c[k]), hi))
                    except (TypeError, ValueError):
                        raise HTTPException(400, f"{name}.{k} 不是数字")
            if c.get("trigger_ratio") not in (None, "", 0):
                try:
                    row["trigger_ratio"] = max(0.1, min(float(c["trigger_ratio"]), 0.98))
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{name}.trigger_ratio 不是数字")
            if not row.get("window") and not row.get("trigger_tokens"):
                raise HTTPException(400, f"{name}：至少填上下文窗口或触发 token 数")
            clean[str(name).strip()[:120]] = row
        store.kv_set("model_context", clean)
        push_ctx()
        return {"models": clean}

    # (9) teach
    from . import teach as _teach
    tc = _teach.Teach(store, sessions, settings)

    def writer_llm():
        if sessions._custom_llm:
            return sessions._custom_llm()
        return sessions._llm()
    _teach.mount(app, tc, auth, apps=ext.apps, llm_for=writer_llm, vision_for=lambda: sessions._vision_llm())
    app.state.teach = tc

    # (11) remote access
    from . import remote_access
    tunnel = remote_access.mount(app, store, settings, auth)

    # 自动签到
    from . import checkin as _ck
    ext.checkin = _ck.Checkin(store, sessions, notifier, secrets)
    _ck.mount(app, ext.checkin, auth)
    app.state.checkin = ext.checkin
    scheduler.extra_ticks.append(ext.checkin.tick)

    async def on_start():
        await remote_access.autostart(store, settings, tunnel)

    async def on_stop():
        await tunnel.stop()
        for rid in list(tc.live):
            try:
                await tc.stop(rid)
            except Exception:  # noqa: BLE001
                pass
    ext.on_start, ext.on_stop = on_start, on_stop
    return ext
