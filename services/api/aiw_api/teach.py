"""v3.9.24 (9) teach a task: record a demonstration on a VM, then let the model write a skill (SKILL.md) from it.

How the recording sees what you do (no vm-agent update needed):
  * every op of the takeover input layer (remote_input: 接管桌面 on web / iOS) — clicks, drags, scrolls, keys and
    typed text — with the time and the pointer position
  * a screenshot after every click / Enter and at most every AIW_TEACH_SHOT_S seconds (default 4) while the screen
    changes, so a demonstration done in the plain noVNC view still has pictures to go by
  * notes you type while recording (「这一步是在登录」)
Typed text is kept as typed: do not type passwords while recording (the review page masks them before saving the
skill, and the writer is told to replace any credential by a {{placeholder}}).

  POST /api/teach/start {machine_id, title}      -> recording
  POST /api/teach/{id}/note {text} · POST /api/teach/{id}/stop
  POST /api/teach/{id}/draft                     the model turns it into SKILL.md (draft kept on the recording)
  POST /api/teach/{id}/save {name, text, vm?}    saved as a skill app (设置 › 应用); vm=true also writes
                                                 ~/.claude/skills/<name>/SKILL.md on the computer and registers it
  GET  /api/teach · /api/teach/{id} · /api/teach/{id}/shot/{n} · DELETE /api/teach/{id}
AIW_TEACH=0 turns recording off.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from typing import Optional

log = logging.getLogger("aiw.teach")
MAX_EVENTS = 5000
MAX_SHOTS = 300

# remote_input calls this for every accepted op: (machine_id, op dict)
_ACTIVE: dict[str, "Recording"] = {}


def on_op(mid: str, op: dict) -> None:
    rec = _ACTIVE.get(mid)
    if rec is not None:
        rec.add_op(op)


class Recording:
    def __init__(self, teach: "Teach", data: dict, computer):
        self.teach, self.data, self.computer = teach, data, computer
        self.task: Optional[asyncio.Task] = None
        self._last_digest = ""
        self._want_shot = asyncio.Event()
        self._last_shot_at = 0.0

    def add_op(self, op: dict) -> None:
        ev = self.data["events"]
        t = op.get("t")
        if t == "move":   # pointer travel is noise; the position rides on the next click
            return
        if t == "text" and ev and ev[-1].get("t") == "text" and time.time() - ev[-1]["at"] < 3:
            ev[-1]["text"] = (ev[-1].get("text") or "") + str(op.get("text") or "")
            return
        if len(ev) >= MAX_EVENTS:
            return
        e = {"at": time.time(), **{k: v for k, v in op.items() if k in ("t", "x", "y", "button", "dx", "dy", "text", "key", "count")}}
        if t == "paste":
            e["t"] = "text"
        ev.append(e)
        if t in ("click", "up") or (t == "key" and str(op.get("key") or "").lower() in ("return", "enter", "kp_enter")):
            self._want_shot.set()

    async def run(self) -> None:
        every = float(os.environ.get("AIW_TEACH_SHOT_S", "4"))
        try:
            await self.shot("开始")
            while self.data["status"] == "recording":
                try:
                    await asyncio.wait_for(self._want_shot.wait(), every)
                    self._want_shot.clear()
                    await asyncio.sleep(0.8)   # let the click's effect paint
                    await self.shot("操作后")
                except asyncio.TimeoutError:
                    await self.shot("")
                if time.time() - self.data["started"] > float(os.environ.get("AIW_TEACH_MAX_S", "3600")):
                    self.data["status"] = "stopped"
                    self.data["note_auto_stop"] = "录制超过上限自动停止"
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("teach recorder: %s", e)
        finally:
            self.teach.persist(self.data)

    async def shot(self, why: str) -> None:
        if self.computer is None or len(self.data["shots"]) >= MAX_SHOTS:
            return
        try:
            snap = await asyncio.wait_for(self.computer.observe(), 15)
        except Exception as e:  # noqa: BLE001
            log.info("teach shot: %s", e)
            return
        if not snap or snap.digest == self._last_digest:
            return
        self._last_digest = snap.digest
        raw = base64.b64decode(snap.screenshot_b64)
        ext = "jpg" if raw[:3] == b"\xff\xd8\xff" else "png"
        n = len(self.data["shots"])
        path = os.path.join(self.teach.dir_of(self.data["id"]), f"{n:03d}.{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        self.data["shots"].append({"n": n, "at": time.time(), "file": os.path.basename(path), "why": why,
                                   "after_event": len(self.data["events"]), "w": snap.width, "h": snap.height})
        self.teach.persist(self.data)


WRITER_PROMPT = """你根据用户在电脑上的一次操作演示，写一个可复用的技能文件 SKILL.md（Markdown，中文）。
要求：
- 开头 YAML front matter：name（小写英文-连接）、description（一句话：什么时候用这个技能）。
- 正文：## 目标、## 前提（需要登录的网站、需要的软件）、## 步骤（编号，每步写清楚点哪里/输入什么/看到什么算成功；
  用界面上的文字和位置描述，不要只写坐标）、## 检查（怎么确认做成了）、## 常见问题。
- 演示里的账号、密码、验证码、手机号一律写成 {{占位符}}，绝不照抄。
- 只根据给你的操作记录、截图说明和用户备注写，看不出来的地方写「（演示里没看出来，执行时先确认）」。
只输出 SKILL.md 的内容。"""


def _mask_text(s: str) -> str:
    return re.sub(r"\S", "•", s) if s else s


def timeline(data: dict, mask_text: bool = False) -> str:
    t0 = data["started"]
    shots = {s["after_event"]: s for s in data["shots"]}
    lines = []
    notes = sorted(data.get("notes") or [], key=lambda n: n["at"])
    ni = 0
    for i, e in enumerate(data["events"]):
        while ni < len(notes) and notes[ni]["at"] <= e["at"]:
            lines.append(f"[{notes[ni]['at'] - t0:6.1f}s] 用户备注：{notes[ni]['text']}")
            ni += 1
        if i in shots:
            s = shots[i]
            lines.append(f"[{s['at'] - t0:6.1f}s] 截图 #{s['n']}" + (f"：{s['desc']}" if s.get("desc") else ""))
        t = e.get("t")
        at = f"[{e['at'] - t0:6.1f}s] "
        if t in ("click", "down", "up"):
            dbl = t == "click" and e.get("count") == 2
            lines.append(at + f"{'双击' if dbl else '点击' if t == 'click' else '按下' if t == 'down' else '松开'} "
                              f"({e.get('x')},{e.get('y')}) {e.get('button') or ''}".rstrip())
        elif t == "scroll":
            lines.append(at + f"滚动 dx={e.get('dx', 0)} dy={e.get('dy', 0)} 在 ({e.get('x')},{e.get('y')})")
        elif t == "text":
            txt = e.get("text") or ""
            lines.append(at + f"输入文字「{_mask_text(txt) if mask_text else txt}」")
        elif t == "key":
            lines.append(at + f"按键 {e.get('key') or e.get('keys')}")
        else:
            lines.append(at + json.dumps(e, ensure_ascii=False))
    for n in notes[ni:]:
        lines.append(f"[{n['at'] - t0:6.1f}s] 用户备注：{n['text']}")
    return "\n".join(lines)[-30000:]


class Teach:
    def __init__(self, store, sessions, settings):
        self.store, self.sessions, self.settings = store, sessions, settings
        self.root = os.path.join(settings.artifacts_dir, "teach")
        self.live: dict[str, Recording] = {}

    def dir_of(self, rid: str) -> str:
        d = os.path.join(self.root, re.sub(r"[^A-Za-z0-9_-]", "", rid))
        os.makedirs(d, exist_ok=True)
        return d

    def persist(self, data: dict) -> None:
        self.store.kv_set(f"teach:{data['id']}", data)
        idx = self.store.kv_get("teach_index", []) or []
        if data["id"] not in idx:
            self.store.kv_set("teach_index", [data["id"]] + idx[:199])

    def get(self, rid: str) -> Optional[dict]:
        if rid in self.live:
            return self.live[rid].data
        return self.store.kv_get(f"teach:{rid}")

    def list(self) -> list[dict]:
        out = []
        for rid in self.store.kv_get("teach_index", []) or []:
            d = self.get(rid)
            if d:
                out.append({k: d.get(k) for k in ("id", "title", "machine_id", "status", "started", "stopped", "saved_as")}
                           | {"events": len(d.get("events") or []), "shots": len(d.get("shots") or []), "has_draft": bool(d.get("draft"))})
        return out

    async def start(self, machine_id: str, title: str = "") -> dict:
        if os.environ.get("AIW_TEACH", "1") == "0":
            raise ValueError("教学录制已关闭（AIW_TEACH=0）")
        if machine_id in _ACTIVE:
            raise ValueError("这台电脑已经在录制了")
        computer = self.sessions.computer(machine_id)
        rid = "teach-" + uuid.uuid4().hex[:10]
        data = {"id": rid, "title": (title or "演示")[:80], "machine_id": machine_id, "status": "recording",
                "started": time.time(), "stopped": None, "events": [], "shots": [], "notes": [], "draft": "", "saved_as": None}
        rec = Recording(self, data, computer)
        self.live[rid] = rec
        _ACTIVE[machine_id] = rec
        self.persist(data)
        rec.task = asyncio.create_task(rec.run())
        return data

    async def stop(self, rid: str) -> Optional[dict]:
        rec = self.live.pop(rid, None)
        if rec is None:
            d = self.get(rid)
            return d
        rec.data["status"] = "stopped"
        rec.data["stopped"] = time.time()
        _ACTIVE.pop(rec.data["machine_id"], None)
        rec._want_shot.set()
        if rec.task:
            try:
                await asyncio.wait_for(rec.task, 20)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                rec.task.cancel()
        try:
            await rec.shot("结束")
        except Exception:  # noqa: BLE001
            pass
        self.persist(rec.data)
        return rec.data

    def note(self, rid: str, text: str) -> Optional[dict]:
        d = self.get(rid)
        if not d:
            return None
        d.setdefault("notes", []).append({"at": time.time(), "text": text[:500]})
        self.persist(d)
        return d

    async def draft(self, rid: str, llm=None, vision=None, max_described: int = 8, mask_text: bool = False) -> str:
        d = self.get(rid)
        if not d:
            raise KeyError(rid)
        if vision is not None:   # describe a few key screenshots so the writer knows what was on screen
            picks = d["shots"] if len(d["shots"]) <= max_described else \
                [d["shots"][round(i * (len(d["shots"]) - 1) / (max_described - 1))] for i in range(max_described)]
            for s in picks:
                if s.get("desc"):
                    continue
                try:
                    with open(os.path.join(self.dir_of(rid), s["file"]), "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    s["desc"] = (await asyncio.wait_for(vision.describe(
                        "用一两句中文说出这张电脑截图上是什么界面、焦点在哪里。", "这是演示录制里的一张截图。", b64, 200), 60) or "")[:300]
                except Exception as e:  # noqa: BLE001
                    log.info("teach describe: %s", e)
            self.persist(d)
        if llm is None:
            raise ValueError("没有可用的模型")
        from agent_runtime import prompts as _p
        user = (f"演示标题：{d['title']}\n电脑：{d['machine_id']}\n\n操作记录（时间、坐标是屏幕像素）：\n"
                f"{timeline(d, mask_text=mask_text)}")
        text = await asyncio.wait_for(llm.complete(_p.get("teach_writer", WRITER_PROMPT), user, 2500), 180)
        d["draft"] = (text or "").strip()
        self.persist(d)
        return d["draft"]

    async def save(self, rid: str, name: str, text: str, apps=None, to_vm: bool = False) -> dict:
        d = self.get(rid)
        if not d:
            raise KeyError(rid)
        name = re.sub(r"[^a-z0-9-]", "-", (name or "").lower()).strip("-")[:40] or f"taught-{rid[-6:]}"
        if len(name) < 2:
            name = f"taught-{rid[-6:]}"
        out = {"name": name}
        if apps is not None:
            res = apps.save({"aiw_app": 1, "id": name, "name": d["title"], "version": "1.0.0",
                             "description": f"从演示录制「{d['title']}」学到的技能", "kind": "skill",
                             "skill": {"text": text, "keywords": [d["title"]]}}, source="teach", enabled=True)
            out["app"] = res
        if to_vm:
            b64 = base64.b64encode(text.encode()).decode()
            cmd = (f"mkdir -p ~/.claude/skills/{name} && echo {b64} | base64 -d > ~/.claude/skills/{name}/SKILL.md "
                   f"&& echo ok")
            try:
                r = await self.sessions.computer(d["machine_id"]).execute("shell.exec", {"command": cmd, "timeout": 20}, timeout=30)
                out["vm"] = bool(r.success)
                if r.success:
                    custom = self.store.kv_get("skills_custom", []) or []
                    custom = [s for s in custom if s.get("id") != name] + [{
                        "id": name, "title": d["title"][:60], "desc": f"演示录制学到的技能：{d['title']}"[:200],
                        "path": f"~/.claude/skills/{name}", "setup": "", "source": "", "machines": [d["machine_id"]]}]
                    self.store.kv_set("skills_custom", custom)
            except Exception as e:  # noqa: BLE001
                out["vm"] = False
                out["vm_error"] = str(e)[:200]
        d["saved_as"] = name
        d["draft"] = text
        self.persist(d)
        return out

    def delete(self, rid: str) -> None:
        rec = self.live.pop(rid, None)
        if rec:
            rec.data["status"] = "stopped"
            _ACTIVE.pop(rec.data["machine_id"], None)
            if rec.task:
                rec.task.cancel()
        self.store._x("DELETE FROM kv WHERE key=?", (f"teach:{rid}",))
        self.store.kv_set("teach_index", [x for x in self.store.kv_get("teach_index", []) or [] if x != rid])
        shutil.rmtree(os.path.join(self.root, re.sub(r"[^A-Za-z0-9_-]", "", rid)), ignore_errors=True)


try:   # 设置 › 提示词
    from agent_runtime import prompts as _prompts_reg
    _prompts_reg.register("teach_writer", "教学 → 技能", WRITER_PROMPT, "把演示录制写成 SKILL.md")
except Exception:  # noqa: BLE001
    pass


def mount(app, teach: Teach, auth, apps=None, llm_for=None, vision_for=None) -> None:
    from fastapi import Body, Depends, HTTPException
    from fastapi.responses import FileResponse

    @app.get("/api/teach", dependencies=[Depends(auth)])
    async def teach_list():
        return {"recordings": teach.list(), "active": {mid: r.data["id"] for mid, r in _ACTIVE.items()}}

    @app.post("/api/teach/start", dependencies=[Depends(auth)])
    async def teach_start(body: dict = Body(...)):
        mid = str((body or {}).get("machine_id") or "")
        if not teach.store.machine(mid):
            raise HTTPException(404, "no such computer")
        try:
            return await teach.start(mid, str((body or {}).get("title") or ""))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/teach/{rid}", dependencies=[Depends(auth)])
    async def teach_get(rid: str):
        d = teach.get(rid)
        if not d:
            raise HTTPException(404, "no such recording")
        return {**d, "timeline": timeline(d, mask_text=True)}

    @app.post("/api/teach/{rid}/note", dependencies=[Depends(auth)])
    async def teach_note(rid: str, body: dict = Body(...)):
        d = teach.note(rid, str((body or {}).get("text") or ""))
        if not d:
            raise HTTPException(404, "no such recording")
        return {"ok": True, "notes": len(d["notes"])}

    @app.post("/api/teach/{rid}/stop", dependencies=[Depends(auth)])
    async def teach_stop(rid: str):
        d = await teach.stop(rid)
        if not d:
            raise HTTPException(404, "no such recording")
        return {k: d.get(k) for k in ("id", "status", "stopped")} | {"events": len(d["events"]), "shots": len(d["shots"])}

    @app.post("/api/teach/{rid}/draft", dependencies=[Depends(auth)])
    async def teach_draft(rid: str, body: dict = Body(default={})):
        try:
            text = await teach.draft(rid, llm_for() if llm_for else None, vision_for() if vision_for else None,
                                     mask_text=bool((body or {}).get("mask_text")))
        except KeyError:
            raise HTTPException(404, "no such recording")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"生成失败：{e}"[:300])
        return {"draft": text}

    @app.post("/api/teach/{rid}/save", dependencies=[Depends(auth)])
    async def teach_save(rid: str, body: dict = Body(...)):
        b = body or {}
        text = str(b.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "SKILL.md 内容为空")
        try:
            return await teach.save(rid, str(b.get("name") or ""), text, apps, bool(b.get("vm")))
        except KeyError:
            raise HTTPException(404, "no such recording")

    @app.get("/api/teach/{rid}/shot/{n}", dependencies=[Depends(auth)])
    async def teach_shot(rid: str, n: int):
        d = teach.get(rid)
        s = next((x for x in (d or {}).get("shots") or [] if x["n"] == n), None)
        if not s:
            raise HTTPException(404, "no such shot")
        return FileResponse(os.path.join(teach.dir_of(rid), s["file"]))

    @app.delete("/api/teach/{rid}", dependencies=[Depends(auth)])
    async def teach_delete(rid: str):
        teach.delete(rid)
        return {"ok": True}
