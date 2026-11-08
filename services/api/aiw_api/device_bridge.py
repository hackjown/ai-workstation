"""v3.9.27: device bridge — the aiw iOS / Android app runs tools on the phone for the agent (calendar, reminders,
contacts, health, location, alarms, Shortcuts), the server never touches the phone's data otherwise.

  WS /ws/device?token=<API token>   (same token the app already uses for /ws/events and every API call)
    app -> {"type":"hello","device_id","platform":"ios|android","name","capabilities":["calendar.read",...],
            "features":{"alarmkit":false,...}}
    srv -> {"type":"welcome","queued":n}
    srv -> {"type":"call","id","tool":"calendar_list","args":{...},"expires_at":<unix>}
    app -> {"type":"result","id","ok":true,"result":{...}} | {"type":"result","id","ok":false,"error":"..."}
    app -> {"type":"ping"}  srv -> {"type":"pong"}

v3.9.28: pending calls are persisted (kv "device_pending") and reloaded at startup, so a queued call survives an API
restart and still reaches the phone when it connects (its result then lands in kv "device_orphan_results"). The phone
fast path (phone_fast.py) calls with ttl=AIW_DEVICE_FAST_TTL (8 s): wake push, then "手机 App 未连接/不在前台".

A tool call made while no capable device is online waits in a queue for AIW_DEVICE_TTL seconds (default 300) and a
push asks the user to open the app; when the app connects the queue is flushed. Past the TTL the agent gets a
clear "device offline" result. Write tools (create/complete/write/alarm/shortcut) carry needs_confirm, so the agent
loop's normal confirm card goes up first — the same card the phone app shows, so it can be answered there.
Agent tool names use "_" (device_calendar_list) because LLM function names cannot contain ".".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Optional

from fastapi import Depends, WebSocket, WebSocketDisconnect   # module level: annotations are resolved here

log = logging.getLogger("aiw.device")

# tool -> (capability, write?, description, properties, required)
S = {"type": "string"}
TOOLS: dict[str, tuple] = {
    "calendar_list": ("calendar.read", False, "列出手机系统日历（iCloud/Google/Exchange 都在内）里一段时间的日程",
                      {"start": {"type": "string", "description": "开始 ISO 时间"}, "end": {"type": "string", "description": "结束 ISO 时间"},
                       "query": {"type": "string", "description": "按标题过滤，可选"}}, ["start", "end"]),
    "calendar_create": ("calendar.write", True, "在手机系统日历新建日程。会先请用户确认",
                        {"title": S, "start": {"type": "string", "description": "开始 ISO 时间"}, "end": {"type": "string", "description": "结束 ISO 时间"},
                         "location": S, "notes": S, "all_day": {"type": "boolean"},
                         "calendar": {"type": "string", "description": "日历名称，可选，默认系统默认日历"}}, ["title", "start", "end"]),
    "reminders_list": ("reminders.read", False, "列出手机提醒事项（默认未完成的）",
                       {"list": {"type": "string", "description": "列表名，可选"}, "include_completed": {"type": "boolean"}}, []),
    "reminders_create": ("reminders.write", True, "新建一条手机提醒事项。会先请用户确认",
                         {"title": S, "due": {"type": "string", "description": "到期 ISO 时间，可选"}, "notes": S,
                          "list": {"type": "string", "description": "列表名，可选"}, "priority": {"type": "integer", "description": "0-9"}}, ["title"]),
    "reminders_complete": ("reminders.write", True, "把一条提醒事项标为完成。会先请用户确认",
                           {"id": {"type": "string", "description": "reminders_list 返回的 id"}}, ["id"]),
    "reminders_delete": ("reminders.write", True, "删除一条提醒事项。会先请用户确认",
                         {"id": {"type": "string", "description": "reminders_list 返回的 id"}}, ["id"]),
    "calendar_today": ("calendar.read", False, "列出手机系统日历里今天的全部日程（按设备本地时区）", {}, []),
    "phone_call": ("phone.call", True, "用手机拨打电话（iOS 弹出系统拨号确认；Android 直接拨号或打开拨号盘）。会先请用户确认",
                   {"number": {"type": "string", "description": "电话号码，可用 contacts_search 查到"},
                    "name": {"type": "string", "description": "对方名字，仅用于显示，可选"}}, ["number"]),
    "sms_send": ("sms.send", True, "用手机发短信（iOS 打开系统短信编辑界面由用户点发送；Android 有权限时直接发送）。会先请用户确认",
                 {"to": {"type": "string", "description": "收件人号码；多个用逗号分隔"}, "body": {"type": "string", "description": "短信内容"}},
                 ["to", "body"]),
    "contacts_search": ("contacts.read", False, "在手机通讯录里按姓名/电话/邮箱搜索联系人",
                        {"query": S, "limit": {"type": "integer"}}, ["query"]),
    "health_read": ("health.read", False, "读取手机健康数据：steps（步数）、sleep（睡眠）、heart_rate（心率）、workouts（体能训练）",
                    {"kind": {"type": "string", "enum": ["steps", "sleep", "heart_rate", "workouts"]},
                     "start": {"type": "string", "description": "开始 ISO 时间"}, "end": {"type": "string", "description": "结束 ISO 时间"}},
                    ["kind", "start", "end"]),
    "health_write": ("health.write", True, "写入有限的健康数据：water（饮水 ml）、weight（体重 kg）、mindful（正念分钟）。会先请用户确认",
                     {"kind": {"type": "string", "enum": ["water", "weight", "mindful"]}, "value": {"type": "number"},
                      "at": {"type": "string", "description": "时间 ISO，默认现在"}}, ["kind", "value"]),
    "location_get": ("location.read", False, "获取手机当前位置（经纬度、精度、粗略地址）", {}, []),
    "alarm_set": ("alarm.write", True, "在手机上设闹钟（iOS 26+ AlarmKit；Android 系统闹钟）。会先请用户确认",
                  {"time": {"type": "string", "description": "HH:MM 24 小时制或 ISO 时间"}, "label": S,
                   "repeat": {"type": "array", "items": S, "description": "重复的星期，如 [\"mon\",\"tue\"]，可选"}}, ["time"]),
    "shortcut_run": ("shortcuts.run", True, "运行手机上的一个快捷指令（iOS 快捷指令 App）。会先请用户确认",
                     {"name": {"type": "string", "description": "快捷指令名称"}, "input": {"type": "string", "description": "传给快捷指令的文本，可选"}},
                     ["name"]),
}


class DeviceHub:
    def __init__(self, store, notifier=None, ttl: Optional[float] = None):
        self.store, self.notifier = store, notifier
        self.ttl = float(ttl if ttl is not None else os.environ.get("AIW_DEVICE_TTL", "300"))
        self.online: dict[str, dict] = {}       # device_id -> {send, platform, caps, name, features}
        self.pending: dict[str, dict] = {}      # call id -> {tool, args, cap, expires, future, sent_to}
        self.fast_ttl = float(os.environ.get("AIW_DEVICE_FAST_TTL", "8"))
        self._reload()

    # ------------------------------------------------------------- persistence (v3.9.28)
    def _reload(self):
        now = time.time()
        try:
            rows = self.store.kv_get("device_pending", []) or []
        except Exception as e:  # noqa: BLE001
            log.info("device pending reload: %s", e)
            rows = []
        for r in rows:
            if isinstance(r, dict) and r.get("id") and r.get("tool") in TOOLS and float(r.get("expires") or 0) > now:
                self.pending[r["id"]] = {"tool": r["tool"], "args": r.get("args") or {}, "cap": TOOLS[r["tool"]][0],
                                         "expires": float(r["expires"]), "future": None, "sent_to": None,
                                         "device_id": r.get("device_id") or "", "reloaded": True}
        self._persist()

    def _persist(self):
        rows = [{"id": cid, "tool": p["tool"], "args": p["args"], "expires": p["expires"], "device_id": p.get("device_id") or ""}
                for cid, p in self.pending.items() if not p.get("fast") and not _done(p)]
        try:
            self.store.kv_set("device_pending", rows)
        except Exception as e:  # noqa: BLE001
            log.info("device pending persist: %s", e)

    def _finish(self, cid: str, res: dict):
        p = self.pending.get(cid)
        if p is None:
            return
        if p["future"] is None:   # reloaded after a restart: nobody waits, keep the result
            keep = (self.store.kv_get("device_orphan_results", []) or [])[-49:]
            self.store.kv_set("device_orphan_results", keep + [{"id": cid, "tool": p["tool"], "args": p["args"],
                                                                 "at": time.time(), **res}])
            p["done"] = True
            self.pending.pop(cid, None)
        elif not p["future"].done():
            p["future"].set_result(res)
        self._persist()

    # ------------------------------------------------------------- registry
    def known(self) -> list[dict]:
        return list(self.store.kv_get("devices", []) or [])

    def devices(self) -> list[dict]:
        out = []
        for d in self.known():
            out.append({**d, "online": d["id"] in self.online})
        return out

    async def register(self, device_id: str, platform: str, caps: list[str], send: Callable, name: str = "",
                       features: Optional[dict] = None) -> int:
        self.online[device_id] = {"send": send, "platform": platform, "caps": list(caps or []), "name": name,
                                  "features": features or {}}
        rec = {"id": device_id, "platform": platform, "name": name or platform, "caps": list(caps or []),
               "features": features or {}, "last_seen": time.time()}
        self.store.kv_set("devices", [d for d in self.known() if d["id"] != device_id] + [rec])
        return await self.flush(device_id)

    def unregister(self, device_id: str, send: Optional[Callable] = None):
        cur = self.online.get(device_id)
        if cur and (send is None or cur["send"] is send):
            self.online.pop(device_id, None)
            for p in self.pending.values():   # calls sent to a device that dropped go back to the queue
                if p.get("sent_to") == device_id and not _done(p):
                    p["sent_to"] = None

    def _pick(self, cap: str, device_id: str = "") -> Optional[str]:
        for did, d in self.online.items():
            if (not device_id or did == device_id) and cap in d["caps"]:
                return did
        return None

    async def _send(self, did: str, cid: str, p: dict) -> bool:
        try:
            await self.online[did]["send"]({"type": "call", "id": cid, "tool": p["tool"], "args": p["args"],
                                            "expires_at": p["expires"]})
            p["sent_to"] = did
            return True
        except Exception as e:  # noqa: BLE001
            log.info("device %s send failed: %s", did, e)
            self.unregister(did)
            return False

    async def flush(self, device_id: str) -> int:
        self.expire()
        n = 0
        for cid, p in list(self.pending.items()):
            if p.get("sent_to") or _done(p):
                continue
            if self._pick(p["cap"], p.get("device_id") or "") == device_id and await self._send(device_id, cid, p):
                n += 1
        return n

    def expire(self):
        now = time.time()
        for cid, p in list(self.pending.items()):
            if p["expires"] <= now:
                if not _done(p):
                    self._finish(cid, {"ok": False, "error": "设备不在线，超过等待时间没有执行", "expired": True})
                self.pending.pop(cid, None)
                self._persist()

    # ------------------------------------------------------------- calls
    async def call(self, tool: str, args: dict, ttl: Optional[float] = None, device_id: str = "", fast: bool = False) -> dict:
        """fast (v3.9.28 phone fast path): wait at most AIW_DEVICE_FAST_TTL (wake push first), then
        "手机 App 未连接/不在前台"; a fast call is not persisted and is dropped when it times out."""
        if tool not in TOOLS:
            return {"ok": False, "error": f"未知的设备工具 {tool}"}
        cap = TOOLS[tool][0]
        if not any(cap in (d.get("caps") or []) for d in self.known()):
            return {"ok": False, "error": f"还没有设备授权「{cap}」：在手机 aiw App › 设置 › 设备能力 里打开"}
        ttl = (self.fast_ttl if fast else self.ttl) if ttl is None else ttl
        cid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        p = {"tool": tool, "args": args or {}, "cap": cap, "expires": time.time() + ttl, "future": fut,
             "sent_to": None, "device_id": device_id, "fast": fast}
        self.pending[cid] = p
        self._persist()
        did = self._pick(cap, device_id)
        if not (did and await self._send(did, cid, p)):
            await self._ask_open_app(tool)
        try:
            res = await asyncio.wait_for(asyncio.shield(fut), ttl + 1)
        except asyncio.TimeoutError:
            res = {"ok": False, "error": "设备不在线，超过等待时间没有执行", "expired": True}
        finally:
            self.pending.pop(cid, None)
            self._persist()
        if fast and res.get("expired"):
            res = {"ok": False, "error": "手机 App 未连接/不在前台", "expired": True, "offline": True}
        return res

    async def on_result(self, device_id: str, msg: dict) -> bool:
        cid = str(msg.get("id") or "")
        p = self.pending.get(cid)
        if not p or _done(p):
            return False
        if msg.get("ok"):
            self._finish(cid, {"ok": True, "result": msg.get("result"), "device": device_id})
        else:
            self._finish(cid, {"ok": False, "error": str(msg.get("error") or "设备执行失败")[:500], "device": device_id})
        return True

    async def _ask_open_app(self, tool: str):
        if self.notifier is None:
            return
        try:
            await self.notifier.notify("device", "打开 aiw App 完成手机上的操作",
                                       f"Agent 在等手机执行：{TOOLS[tool][2][:40]}", link="aiw://open")
        except Exception as e:  # noqa: BLE001
            log.info("device notify: %s", e)

    # ------------------------------------------------------------- agent tools
    def tools(self, fast: bool = False) -> dict:
        """device_<tool> for every tool some registered device ever offered (offline devices included: queued).
        fast: the phone fast path's copies (short wait, "手机 App 未连接/不在前台")."""
        caps = {c for d in self.known() for c in (d.get("caps") or [])}
        out = {}
        for tool, (cap, write, desc, props, req) in TOOLS.items():
            if cap not in caps:
                continue
            name = "device_" + tool
            schema = {"name": name, "description": desc + "（在用户手机上执行，手机不在线时排队等待）",
                      "parameters": {"type": "object", "properties": props, "required": req}}

            async def fn(a, _t=tool):
                r = await self.call(_t, a or {}, fast=fast)
                return r.get("result") if r.get("ok") and isinstance(r.get("result"), dict) else r
            fn.needs_confirm = (lambda a, _w=write: _w)   # type: ignore[attr-defined]
            fn.confirm_reason = "会在你的手机上写入数据或执行操作"   # type: ignore[attr-defined]
            fn.max_result = 4000   # type: ignore[attr-defined]
            out[name] = (schema, fn)
        return out


def _done(p: dict) -> bool:
    f = p.get("future")
    return bool(p.get("done")) if f is None else f.done()


_HUBS: dict[int, DeviceHub] = {}


def hub_for(store, notifier=None) -> DeviceHub:
    h = _HUBS.get(id(store))
    if h is None or h.store is not store:
        h = _HUBS[id(store)] = DeviceHub(store, notifier)
    return h


def existing_hub(store) -> Optional[DeviceHub]:
    h = _HUBS.get(id(store))
    return h if h is not None and h.store is store else None


def mount(app, hub: DeviceHub, auth, ws_ok) -> None:
    @app.get("/api/devices", dependencies=[Depends(auth)])
    async def devices():
        return {"devices": hub.devices(), "tools": sorted(TOOLS), "queued": len(hub.pending)}

    @app.delete("/api/devices/{did}", dependencies=[Depends(auth)])
    async def forget_device(did: str):
        hub.store.kv_set("devices", [d for d in hub.known() if d["id"] != did])
        hub.unregister(did)
        return {"ok": True}

    @app.websocket("/ws/device")
    async def ws_device(ws: WebSocket):
        if not ws_ok(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        did = ""

        async def send(obj):
            await ws.send_text(json.dumps(obj, ensure_ascii=False))
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except ValueError:
                    continue
                t = msg.get("type")
                if t == "hello":
                    did = str(msg.get("device_id") or "")[:80] or uuid.uuid4().hex[:8]
                    caps = [str(c) for c in (msg.get("capabilities") or []) if isinstance(c, str)][:40]
                    await send({"type": "welcome", "device_id": did,
                                "queued": len([p for p in hub.pending.values() if not p.get("sent_to")])})
                    await hub.register(did, str(msg.get("platform") or "")[:20], caps, send,
                                       str(msg.get("name") or "")[:60], msg.get("features") or {})
                elif t == "result" and did:
                    await hub.on_result(did, msg)
                elif t == "ping":
                    await send({"type": "pong", "t": time.time()})
        except WebSocketDisconnect:
            pass
        finally:
            if did:
                hub.unregister(did, send)


__all__ = ["DeviceHub", "TOOLS", "hub_for", "existing_hub", "mount", "Any"]
