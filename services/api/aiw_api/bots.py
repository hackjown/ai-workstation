"""v3.9.57 引擎对话 (engine bots): talk to a coding agent CLI on the computer directly, or hand the talking to AIW.

Each conversation (bot_convs) is bound to one native session of an engine CLI on one computer. Phase 1 has
Antigravity (`agy`, through agy_bridge.py's plain mode: no worktree, the folder is edited in place, the session is
resumed with `--conversation <id>` when its process has ended). Codex is listed and comes next (`codex exec --json`,
`codex exec resume <id>`), behind the same Engine interface.

Who speaks in a conversation:
  user  the person, typing in the 引擎对话 page (or quoting an earlier message: its text and the turns around it
        travel with the message, see quote_block)
  aiw   AIW the manager (管家): when the user hands a goal over (takeover), AIW reads each engine reply, decides the
        next message, and stops when the goal is met, a decision needs the user, or the round limit is reached. The
        user can type at any time; their message goes first and the takeover carries on after it
  bot   the engine's reply for one turn: its final text, the steps it ran, denied steps, and what changed

Nothing here pushes, merges or deletes: the bridge's deny rules stay in force, and AIW's manager prompt forbids it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

log = logging.getLogger("aiw.bots")

ENGINES = {
    "agy": {"id": "agy", "title": "Antigravity", "short": "agy", "available": True,
            "desc": "Google Antigravity CLI，在电脑上的一个文件夹里写代码"},
    "codex": {"id": "codex", "title": "Codex", "short": "codex", "available": False,
              "desc": "OpenAI Codex CLI（下一版接入）"},
}
LIVE = ("running", "starting")
TURN_DONE = ("turn_done", "turn_failed", "blocked")
MAX_ROUNDS = 8
POLL_S = 20

MANAGER = """你是 AIW 管家，代替用户和编程 Agent（{engine}）对话，推进用户交代的目标。
你看到：目标、最近的对话、Agent 刚才这一轮的回复（含它执行的步骤、被拒绝的操作、改动的文件）。
只输出一个 JSON 对象，不要别的文字：
{{"done": true/false, "next": "发给 Agent 的下一句话（done 或 ask_user 时留空）", "report": "给用户的一两句进展或结论", "ask_user": "必须由用户决定的问题，没有就留空"}}
规则：
- 目标的验收点都满足了才算 done；Agent 只说"完成了"不算，让它给出验证结果（跑过的测试、命令输出）。
- next 要具体：指出没满足的点、要它做的下一步、怎么验证；一次一件事，用中文。
- 不许让 Agent git push、合并、删分支、重置、删用户文件、改 ~/.gemini 或 ~/.config/aiw；需要这些就 ask_user。
- Agent 的操作被拒绝（权限）时，让它换在工作文件夹里能完成的做法；做不到就 ask_user，说明被拒的是什么。
- 花钱、对外发消息、删除数据、不确定用户意图的决定，都 ask_user。"""


def _now() -> float:
    return time.time()


def ensure_tables(store) -> None:
    store._x("CREATE TABLE IF NOT EXISTS bot_convs (id TEXT PRIMARY KEY, bot TEXT, title TEXT, machine_id TEXT, "
             "workdir TEXT, job TEXT, session TEXT, status TEXT, takeover TEXT, created_at REAL, updated_at REAL)")
    store._x("CREATE TABLE IF NOT EXISTS bot_messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, "
             "conv TEXT, role TEXT, text TEXT, data TEXT, created_at REAL, updated_at REAL)")
    store._x("CREATE INDEX IF NOT EXISTS ix_botmsg_conv ON bot_messages(conv, seq)")
    try:
        store._x("ALTER TABLE bot_convs ADD COLUMN cursor INTEGER DEFAULT 0")
    except Exception:  # noqa: BLE001  already there
        pass


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def conv_row(r: dict) -> dict:
    d = dict(r)
    d["takeover"] = _j(d.get("takeover"), None)
    return d


def msg_row(r: dict) -> dict:
    d = dict(r)
    d["data"] = _j(d.get("data"), {})
    return d


def clip(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def who(role: str, engine: str = "Agent") -> str:
    return {"user": "用户", "aiw": "AIW 管家", "bot": engine, "assistant": "小秘"}.get(role, role)


def quote_block(q: dict | None) -> str:
    """The text a quoted message adds to the next turn: the quoted message, and the turns around it."""
    if not q:
        return ""
    lines = [f"［引用 {q.get('who') or ''} 的话］" + clip(q.get("text"), 1500)]
    ctx = [c for c in q.get("context") or [] if c.get("text")]
    if ctx:
        lines.append("［它前后的对话］")
        lines += [f"{c.get('who')}：{clip(c.get('text'), 400)}" for c in ctx]
    return "\n".join(lines)


def parse_decision(out: str) -> dict:
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    d = d if isinstance(d, dict) else {}
    return {"done": bool(d.get("done")), "next": str(d.get("next") or "").strip(),
            "report": str(d.get("report") or "").strip(), "ask_user": str(d.get("ask_user") or "").strip()}


def turn_text(r: dict) -> str:
    """The reply shown for one engine turn (the final response; a short note when there is none)."""
    t = (r.get("response") or "").strip()
    if t:
        return t
    if r.get("auth_error"):
        return "（agy 需要重新登录：在电脑终端运行一次 agy 完成登录）"
    if r.get("error"):
        return f"（这一轮没完成：{clip(r['error'], 300)}）"
    said = [s for s in r.get("said") or [] if s]
    return said[-1] if said else "（这一轮没有文字回复）"


# --------------------------------------------------------------------------------------------- engines
class AgyEngine:
    """Antigravity through agy_bridge.py (plain mode). All ops are one shell call on the computer."""
    id = "agy"

    def __init__(self, shell: Callable[[str, str, int], Any]):
        self.shell = shell           # async (machine_id, command, timeout) -> stdout
        self._installed: set = set()

    async def op(self, mid: str, op: str, a: dict, job: Optional[dict] = None) -> dict:
        from agent_runtime import antigravity as agy
        if mid not in self._installed:
            out = await self.shell(mid, agy.install_command(), 60)
            if "AGY_BRIDGE_OK" not in (out or ""):
                raise RuntimeError("没能把 agy 桥接脚本写到电脑上：" + clip(out, 200))
            self._installed.add(mid)
        cmd, to = agy.command(op, a, job)
        out = await self.shell(mid, cmd, to)
        r = agy.parse(out)
        if r is None:
            raise RuntimeError(f"agy {op} 没有结果：" + clip(out, 300))
        return r

    def job(self, conv: dict, prompt: str) -> dict:
        from agent_runtime import antigravity as agy
        j = agy.job_from_args({"conversation": conv.get("session") or ""}, prompt)
        j.update({"plain": True, "workdir": conv.get("workdir") or "~/workspace/agy-chat",
                  "max_turns": 1000, "timeout_min": 24 * 60, "keep_min": 120, "idle_min": 20})
        return j

    async def start(self, mid: str, conv: dict, prompt: str) -> dict:
        return await self.op(mid, "start", {}, self.job(conv, prompt))

    async def send(self, mid: str, job: str, text: str) -> dict:
        return await self.op(mid, "send", {"job": job, "message": text})

    async def wait(self, mid: str, job: str, cursor: int, seconds: int = POLL_S) -> dict:
        return await self.op(mid, "wait", {"job": job, "cursor": cursor, "seconds": seconds})

    async def stop(self, mid: str, job: str) -> dict:
        return await self.op(mid, "stop", {"job": job})


# --------------------------------------------------------------------------------------------- the service
class Bots:
    def __init__(self, store, sessions=None, notifier=None, llm: Optional[Callable[[], Any]] = None,
                 default_machine: Optional[Callable[[], str]] = None, shell=None):
        self.store, self.sessions, self.notifier = store, sessions, notifier
        self.llm = llm                                   # () -> router with .complete(system, user, max_tokens)
        self.default_machine = default_machine or (lambda: "ai-linux-001")
        ensure_tables(store)
        self.engines = {"agy": AgyEngine(shell or self._shell)}
        self.queues: dict[str, list] = {}
        self.runners: dict[str, asyncio.Task] = {}
        self.listeners: list = []

    # ------------------------------------------------------------------ computer
    async def _shell(self, mid: str, command: str, timeout: int) -> str:
        comp = self.sessions.computer(mid)
        r = await asyncio.wait_for(comp.execute("shell.exec", {"command": command, "timeout": timeout},
                                                timeout=timeout + 10), timeout + 20)
        res = getattr(r, "result", None) or {}
        if not getattr(r, "success", True) and not res.get("stdout"):
            raise RuntimeError(getattr(r, "error", None) or "电脑没有响应")
        return str(res.get("stdout") or "") + ("\n" + str(res.get("stderr")) if res.get("stderr") else "")

    # ------------------------------------------------------------------ storage
    def convs(self) -> list[dict]:
        rows = self.store._x("SELECT * FROM bot_convs ORDER BY updated_at DESC")
        out = []
        for r in rows:
            c = conv_row(r)
            last = self.store._x("SELECT role, text FROM bot_messages WHERE conv=? ORDER BY seq DESC LIMIT 1", (c["id"],))
            c["last"] = {"role": last[0]["role"], "text": clip(last[0]["text"], 80)} if last else None
            c["busy"] = self.busy(c["id"])
            out.append(c)
        return out

    def conv(self, cid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM bot_convs WHERE id=?", (cid,))
        return conv_row(r[0]) if r else None

    def set_conv(self, cid: str, **kw) -> Optional[dict]:
        if "takeover" in kw:
            kw["takeover"] = json.dumps(kw["takeover"], ensure_ascii=False) if kw["takeover"] else None
        kw["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.store._x(f"UPDATE bot_convs SET {cols} WHERE id=?", (*kw.values(), cid))
        c = self.conv(cid)
        self._emit("conv", c)
        return c

    def create(self, bot: str, title: str = "", workdir: str = "", machine_id: str = "") -> dict:
        eng = ENGINES.get(bot)
        if not eng:
            raise ValueError(f"没有这个引擎：{bot}")
        if not eng["available"]:
            raise ValueError(f"{eng['title']} 还没接入")
        cid = f"bc-{uuid.uuid4().hex[:10]}"
        wd = (workdir or "").strip() or f"~/workspace/agy-chat/{cid}"
        if not re.fullmatch(r"[~\w./ \-\u4e00-\u9fff]{1,200}", wd) or ".." in wd.split("/"):
            raise ValueError("工作文件夹路径不合法")
        now = _now()
        self.store._x("INSERT INTO bot_convs (id, bot, title, machine_id, workdir, job, session, status, takeover, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, bot, (title or "").strip()[:60] or f"{eng['title']} 对话", machine_id or self.default_machine(),
                       wd, None, None, "idle", None, now, now))
        return self.conv(cid)

    def delete(self, cid: str) -> bool:
        self.store._x("DELETE FROM bot_messages WHERE conv=?", (cid,))
        self.store._x("DELETE FROM bot_convs WHERE id=?", (cid,))
        return True

    def messages(self, cid: str, after: int = 0, limit: int = 200) -> list[dict]:
        rows = self.store._x("SELECT * FROM bot_messages WHERE conv=? AND seq>? ORDER BY seq DESC LIMIT ?",
                             (cid, int(after or 0), int(limit)))
        return [msg_row(r) for r in reversed(rows)]

    def message(self, mid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM bot_messages WHERE id=?", (mid,))
        return msg_row(r[0]) if r else None

    def add(self, cid: str, role: str, text: str, data: Optional[dict] = None) -> dict:
        mid = f"bm-{uuid.uuid4().hex[:12]}"
        now = _now()
        self.store._x("INSERT INTO bot_messages (id, conv, role, text, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                      (mid, cid, role, text or "", json.dumps(data or {}, ensure_ascii=False), now, now))
        self.store._x("UPDATE bot_convs SET updated_at=? WHERE id=?", (now, cid))
        m = self.message(mid)
        self._emit("message", m)
        return m

    def update(self, mid: str, text: Optional[str] = None, data: Optional[dict] = None) -> Optional[dict]:
        m = self.message(mid)
        if not m:
            return None
        nd = {**m["data"], **(data or {})}
        self.store._x("UPDATE bot_messages SET text=?, data=?, updated_at=? WHERE id=?",
                      (m["text"] if text is None else text, json.dumps(nd, ensure_ascii=False), _now(), mid))
        m = self.message(mid)
        self._emit("message", m)
        return m

    def _emit(self, kind: str, obj):
        for f in list(self.listeners):
            try:
                f(kind, obj)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ quoting
    def quote(self, ref: str, chat=None) -> Optional[dict]:
        """A quoted message (from an engine conversation, or the main chat when `chat` is given) with the two
        messages before it and the one after it, so the next turn knows what the quote was about."""
        if not ref:
            return None
        m = self.message(ref)
        if m:
            c = self.conv(m["conv"]) or {}
            eng = ENGINES.get(c.get("bot"), {}).get("title", "Agent")
            near = self.store._x("SELECT * FROM bot_messages WHERE conv=? AND seq<? ORDER BY seq DESC LIMIT 2",
                                 (m["conv"], m["seq"]))
            after = self.store._x("SELECT * FROM bot_messages WHERE conv=? AND seq>? ORDER BY seq LIMIT 1",
                                  (m["conv"], m["seq"]))
            ctx = [msg_row(r) for r in reversed(near)] + [msg_row(r) for r in after]
            return {"id": m["id"], "src": "bot", "conv": m["conv"], "who": who(m["role"], eng),
                    "text": clip(m["text"], 1500), "context": [{"who": who(x["role"], eng), "text": clip(x["text"], 400)}
                                                              for x in ctx]}
        if chat is not None:
            return chat.quote(ref)
        return None

    # ------------------------------------------------------------------ talking
    def busy(self, cid: str) -> bool:
        t = self.runners.get(cid)
        return bool(t and not t.done())

    def send(self, cid: str, text: str, role: str = "user", quote: Optional[dict] = None) -> dict:
        c = self.conv(cid)
        if not c:
            raise KeyError(cid)
        text = (text or "").strip()
        if not text:
            raise ValueError("空消息")
        data = {"quote": quote} if quote else {}
        if self.busy(cid):
            data["queued"] = True
        m = self.add(cid, role, text, data)
        prompt = (quote_block(quote) + "\n\n" + text) if quote else text
        self.queues.setdefault(cid, []).append({"msg": m["id"], "prompt": prompt})
        self._kick(cid)
        return m

    def _kick(self, cid: str):
        if not self.busy(cid):
            self.runners[cid] = asyncio.get_running_loop().create_task(self._run(cid))

    async def _run(self, cid: str):
        try:
            while True:
                q = self.queues.get(cid) or []
                if not q:
                    nxt = await self._manage(cid)        # takeover: AIW writes the next message
                    if not nxt:
                        break
                    continue
                item = q.pop(0)
                if item.get("msg"):
                    self.update(item["msg"], data={"queued": False})
                await self._turn(cid, item["prompt"])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("bot conv %s", cid)
            self.add(cid, "system", f"出错了：{clip(e, 300)}", {"error": True})
        finally:
            c = self.conv(cid)
            if c and c.get("status") == "running":
                self.set_conv(cid, status="idle")

    async def _turn(self, cid: str, prompt: str) -> dict:
        c = self.conv(cid)
        eng = self.engines[c["bot"]]
        mid = c["machine_id"]
        self.set_conv(cid, status="running")
        reply = self.add(cid, "bot", "", {"status": "running", "steps": [], "started": _now()})
        r: dict = {}
        job = c.get("job")
        if job:
            try:
                r = await eng.send(mid, job, prompt)
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            if not r.get("ok"):
                job = None                                # the session ended: start one that resumes it
        if not job:
            r = await eng.start(mid, c, prompt)
            if not r.get("ok"):
                if r.get("installed") is False:
                    msg = "电脑上没装 agy（Antigravity CLI），装好并登录后再试"
                else:
                    msg = "agy 没能启动：" + clip(r.get("error"), 300)
                self.update(reply["id"], msg, {"status": "error"})
                self.set_conv(cid, status="idle", job=None)
                return {"status": "error", "error": msg}
            job = r["job"]
            self.set_conv(cid, job=job, cursor=0)
            cursor = 0
        else:
            cursor = int(c.get("cursor") or 0)
        steps: list = []
        res: dict = {}
        for _ in range(int(24 * 3600 / POLL_S)):
            try:
                res = await eng.wait(mid, job, cursor)
            except Exception as e:  # noqa: BLE001  one slow poll is not the end of the turn
                log.warning("agy wait %s: %s", job, e)
                await asyncio.sleep(5)
                continue
            cursor = int(res.get("cursor") or cursor)
            for t in res.get("tools") or []:
                steps.append({k: clip(t.get(k), 240) for k in ("tool", "arg", "error") if t.get(k)})
            steps = steps[-40:]
            self.update(reply["id"], None, {"steps": steps, "said": (res.get("said") or [])[-1:],
                                            "status": "running"})
            if res.get("status") not in LIVE:
                break
        st = res.get("status") or "error"
        alive = st in TURN_DONE
        data = {"status": "done" if st == "turn_done" else ("blocked" if st == "blocked" else
                                                            ("failed" if st == "turn_failed" else st)),
                "steps": steps, "denied": (res.get("denied") or [])[-5:], "said": [],
                "changes": res.get("changes"), "usage": res.get("usage"), "ended": _now()}
        self.update(reply["id"], turn_text(res), data)
        self.set_conv(cid, status="idle", job=job if alive else None, cursor=cursor,
                      session=res.get("conversation_id") or c.get("session"))
        if not self.conv(cid).get("takeover"):
            await self._notify(cid, turn_text(res))
        return {"status": data["status"], "text": turn_text(res), "result": res}

    async def _notify(self, cid: str, text: str):
        if not self.notifier:
            return
        c = self.conv(cid) or {}
        try:
            await self.notifier.notify("reply", f"{ENGINES.get(c.get('bot'), {}).get('title', 'Agent')} 回复了",
                                       clip(re.sub(r"\s+", " ", text), 200), deep=f"bots?c={cid}")
        except Exception as e:  # noqa: BLE001
            log.debug("bot notify: %s", e)

    # ------------------------------------------------------------------ takeover (AIW 管家)
    def takeover(self, cid: str, goal: str, rounds: int = MAX_ROUNDS) -> dict:
        c = self.conv(cid)
        if not c:
            raise KeyError(cid)
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("说一下要它完成什么")
        tk = {"goal": goal[:2000], "rounds": 0, "max": max(1, min(int(rounds or MAX_ROUNDS), 30)), "since": _now()}
        self.set_conv(cid, takeover=tk)
        self.add(cid, "system", f"交给 AIW 管家：{clip(goal, 300)}", {"takeover": "on"})
        if not self.busy(cid):
            # nothing running: AIW opens with the goal itself (or the next step, when the engine already replied)
            self.queues.setdefault(cid, [])
            last = self.messages(cid, limit=3)
            if not any(m["role"] == "bot" for m in last[-2:]):
                self.queues[cid].append({"msg": self.add(cid, "aiw", goal)["id"], "prompt": goal})
            self._kick(cid)
        return self.conv(cid)

    def release(self, cid: str, note: str = "收回了，现在由你直接和它对话") -> dict:
        c = self.conv(cid)
        if c and c.get("takeover"):
            self.set_conv(cid, takeover=None)
            self.add(cid, "system", note, {"takeover": "off"})
        return self.conv(cid)

    async def _manage(self, cid: str) -> bool:
        """After an engine turn, under takeover: decide. True when a next message was queued."""
        c = self.conv(cid)
        tk = (c or {}).get("takeover")
        if not tk:
            return False
        msgs = self.messages(cid, limit=12)
        last = next((m for m in reversed(msgs) if m["role"] == "bot"), None)
        if not last or last["data"].get("status") == "running":
            return False
        if last["data"].get("managed"):
            return False                                 # already judged (a release / re-takeover in between)
        self.update(last["id"], data={"managed": True})
        eng = ENGINES.get(c["bot"], {}).get("title", "Agent")
        if tk["rounds"] >= tk["max"]:
            self.add(cid, "aiw", f"已经来回 {tk['max']} 轮还没完成，先停在这里。看一下它最后的回复，告诉我接下来怎么做，"
                                 "或者再交给我一次。", {"report": True})
            self.release(cid, "管家停下了（轮数用完）")
            await self._notify(cid, "管家停下了：轮数用完，需要你看一下")
            return False
        hist = "\n".join(f"{who(m['role'], eng)}：{clip(m['text'], 600)}" for m in msgs[-8:-1] if m["role"] != "system")
        d = last["data"]
        detail = []
        if d.get("steps"):
            detail.append("它执行的步骤：" + "；".join(f"{s.get('tool')} {s.get('arg', '')}"[:120] for s in d["steps"][-12:]))
        if d.get("denied"):
            detail.append("被拒绝的操作：" + "；".join(f"{x.get('tool')} {x.get('target')}（{x.get('why')}）"
                                                for x in d["denied"]))
        ch = d.get("changes") or {}
        if ch.get("files"):
            detail.append("改动的文件：" + "，".join(ch["files"][:20]))
        user = (f"目标：{tk['goal']}\n\n最近的对话：\n{hist or '（无）'}\n\nAgent 这一轮（状态 {d.get('status')}）的回复：\n"
                f"{clip(last['text'], 4000)}\n" + "\n".join(detail))
        try:
            out = await asyncio.wait_for(self.llm().complete(MANAGER.format(engine=eng), user, 800), 90)
            dec = parse_decision(out)
        except Exception as e:  # noqa: BLE001
            self.add(cid, "aiw", f"管家没能判断下一步（{clip(e, 200)}），先交还给你。", {"report": True})
            self.release(cid, "管家停下了（出错）")
            return False
        tk["rounds"] += 1
        self.set_conv(cid, takeover=tk)
        if dec["done"]:
            self.add(cid, "aiw", dec["report"] or "目标完成了。", {"report": True, "done": True})
            self.release(cid, "管家完成了目标，交还给你")
            await self._notify(cid, "管家：" + (dec["report"] or "目标完成了"))
            return False
        if dec["ask_user"] or not dec["next"]:
            q = dec["ask_user"] or "下一步需要你来定。"
            self.add(cid, "aiw", (dec["report"] + "\n\n" if dec["report"] else "") + "需要你决定：" + q,
                     {"report": True, "ask": True})
            self.release(cid, "管家在等你决定，回复后可以再交给它")
            await self._notify(cid, "管家需要你决定：" + q)
            return False
        m = self.add(cid, "aiw", dec["next"], {"report": dec["report"]})
        self.queues.setdefault(cid, []).append({"msg": m["id"], "prompt": dec["next"]})
        return True

    async def stop(self, cid: str) -> dict:
        c = self.conv(cid)
        if not c:
            raise KeyError(cid)
        self.queues[cid] = []
        t = self.runners.get(cid)
        if t and not t.done():
            t.cancel()
        if c.get("takeover"):
            self.release(cid, "已停止，管家也停了")
        if c.get("job"):
            try:
                await self.engines[c["bot"]].stop(c["machine_id"], c["job"])
            except Exception as e:  # noqa: BLE001
                log.warning("bot stop: %s", e)
        for m in self.messages(cid, limit=5):
            if m["role"] == "bot" and m["data"].get("status") == "running":
                self.update(m["id"], (m["text"] or "") + "（已停止）" if m["text"] else "（已停止）", {"status": "stopped"})
        return self.set_conv(cid, status="idle", job=None)


# --------------------------------------------------------------------------------------------- HTTP
try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover  (the bridge tests import this module without the API's dependencies)
    BaseModel = object

class ConvIn(BaseModel):
    bot: str = "agy"
    title: str = ""
    workdir: str = ""
    machine_id: str = ""

class SendIn(BaseModel):
    text: str
    quote: Optional[str] = None

class TakeIn(BaseModel):
    goal: str
    rounds: int = MAX_ROUNDS

class PatchIn(BaseModel):
    title: Optional[str] = None


def mount(app, bots: Bots, auth, chat=None) -> None:
    from fastapi import Depends, HTTPException

    dep = [Depends(auth)]

    def need(cid: str) -> dict:
        c = bots.conv(cid)
        if not c:
            raise HTTPException(404, "没有这个对话")
        return c

    @app.get("/api/bots", dependencies=dep)
    async def bots_list():
        return {"engines": list(ENGINES.values()), "convs": bots.convs()}

    @app.post("/api/bots/convs", dependencies=dep)
    async def bots_create(body: ConvIn):
        try:
            return bots.create(body.bot, body.title, body.workdir, body.machine_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/bots/convs/{cid}", dependencies=dep)
    async def bots_conv(cid: str, after: int = 0):
        c = need(cid)
        c["busy"] = bots.busy(cid)
        c["queued"] = len(bots.queues.get(cid) or [])
        return {"conv": c, "messages": bots.messages(cid, after)}

    @app.patch("/api/bots/convs/{cid}", dependencies=dep)
    async def bots_patch(cid: str, body: PatchIn):
        need(cid)
        if body.title is not None:
            return bots.set_conv(cid, title=body.title.strip()[:60] or "对话")
        return bots.conv(cid)

    @app.delete("/api/bots/convs/{cid}", dependencies=dep)
    async def bots_delete(cid: str):
        need(cid)
        if bots.busy(cid):
            await bots.stop(cid)
        return {"ok": bots.delete(cid)}

    @app.post("/api/bots/convs/{cid}/send", dependencies=dep)
    async def bots_send(cid: str, body: SendIn):
        need(cid)
        q = bots.quote(body.quote, chat) if body.quote else None
        try:
            return bots.send(cid, body.text, "user", q)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/bots/convs/{cid}/stop", dependencies=dep)
    async def bots_stop(cid: str):
        need(cid)
        return await bots.stop(cid)

    @app.post("/api/bots/convs/{cid}/takeover", dependencies=dep)
    async def bots_take(cid: str, body: TakeIn):
        need(cid)
        try:
            return bots.takeover(cid, body.goal, body.rounds)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/bots/convs/{cid}/takeover", dependencies=dep)
    async def bots_release(cid: str):
        need(cid)
        return bots.release(cid)
