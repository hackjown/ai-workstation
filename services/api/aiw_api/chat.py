"""The persistent chat with 小秘 / 球球 (2026-10-28).

The main screen on web and iOS is ONE chat that never clears. It lives here on the server (table chat_messages), so
every device opens it where the user left off.

* Every message is routed by one cheap model call: chat (the reply comes from the chat model with light tools: web
  search when a search connector is set up, read a URL or a file, look up tasks and routines) or task (a normal agent
  task starts and the chat shows it as a compact card: title, status line, step). Unsure means chat.
* A chat reply may end with ``[[交给电脑:…]]``: the line is hidden and the message gets a 交给电脑去做 button.
* The task's milestones come from SessionManager.listeners: the card follows status / plan / step, an escalation
  (escalate.py: 继续换个方法再试 / 我来接管桌面 / 停止任务), a takeover request, a question, a form or a confirmation
  becomes a chat message with buttons, and the end posts a one-line result from 球球 with the task's files.
* Context: the model gets the last CHAT_TURNS turns verbatim plus a rolling summary of everything older, refreshed
  in the background (kv chat_summary). The screen still gets the full history.
* Long-term memory (memory.py) plugs in through ``self.memory``: "记住…" / "忘掉…" act at once, the profile and the
  top memories for the message go into the system prompt, and it learns from each exchange in the background.

No thinking and no agent steps ever go into the chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("aiw.chat")

CONV = "main"
CHAT_TURNS = int(os.environ.get("AIW_CHAT_TURNS", "20"))          # turns sent verbatim
COMPACT_MIN = int(os.environ.get("AIW_CHAT_COMPACT_MIN", "6"))    # messages outside the window before a summary run
TOOL_ROUNDS = 4
FINAL = ("succeeded", "failed", "stopped", "interrupted", "cancelled")
WAITING = ("waiting_confirm", "waiting_input")
STATUS_CN = {"queued": "排队中", "pending": "准备中", "running": "进行中", "paused": "已暂停", "takeover": "你在接管",
             "waiting_confirm": "等你确认", "waiting_input": "等你回答", "succeeded": "已完成", "failed": "没完成",
             "stopped": "已停止", "interrupted": "被中断", "cancelled": "已取消"}
ESC_OPTIONS = ["继续，换个方法再试", "我来接管桌面", "停止任务"]

SKILL_RE = re.compile(r"^\s*【技能[:：][^】]+】")
OFFER_RE = re.compile(r"\[\[\s*交给电脑\s*[:：]\s*(.+?)\s*\]\]", re.S)

ROUTE_PROMPT = """你是消息分流器（ROUTE）。用户在和私人助理聊天；助理背后有一台能操作的电脑（浏览器、文件、软件、下单、注册、画图、做视频、批量处理等）。
判断用户这条最新消息要的是：
- "chat"：聊天、提问、咨询、解释、写一段文字、出主意、查一下任务进度、问知识或网上的信息（助理能联网搜索、读网页、读文件）。
- "task"：明确要电脑去动手办一件事（打开网站操作、登录、填表、下单、下载、做/改文件、画图、剪视频、在电脑上安装或运行东西、定时去做某事）。
拿不准就选 "chat"。
只输出一行 JSON：{"route": "chat" 或 "task", "title": "≤16 个字的任务标题（chat 时留空）"}"""

SYSTEM = """你是「{name}」，用户的私人助理，形象是一只叫球球的小球。你在一个永不清空的聊天里陪用户说话、回答问题。
现在是 {now}（北京时间）。
说话方式：简体中文，口语、简洁、直接给答案；不要客套，不要长篇大论；需要列点时用短列表。
你在聊天里能用的工具：{tools}。不知道的事先查，查不到就直说，绝不编造。
你自己不能操作电脑。用户要的事需要在电脑上动手（打开网站办事、登录、填表、下单、下载、做或改文件、画图、剪视频、长时间执行）时，
先用一两句话说明你会怎么做，然后在回复的最后单独一行写：[[交给电脑:一句话说清要电脑做什么]]。用户点按钮就会交给电脑去做。
不要输出你的思考过程。永远不要索要或复述密码、验证码。"""

SUMMARY_PROMPT = """你在给一段很长的聊天做滚动摘要，供助理以后回忆。把「已有摘要」和「新的对话」合并成一份新的摘要：
- 用简体中文，第三人称（"用户…"、"助理…"），不超过 500 字；
- 保留：用户的目标、偏好、做过的决定、提到的人和事、交给电脑的任务及结果、还没解决的问题；
- 去掉寒暄和重复；不要写密码、验证码。
只输出摘要正文。"""


# ---------------------------------------------------------------------------------------------- storage
def ensure_tables(store) -> None:
    store._x("CREATE TABLE IF NOT EXISTS chat_messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, "
             "conv TEXT DEFAULT 'main', role TEXT, kind TEXT, text TEXT, data TEXT, task_id TEXT, "
             "created_at REAL, updated_at REAL)")
    store._x("CREATE INDEX IF NOT EXISTS ix_chat_conv ON chat_messages(conv, seq)")
    store._x("CREATE INDEX IF NOT EXISTS ix_chat_task ON chat_messages(task_id)")


def _row(r: dict) -> dict:
    d = dict(r)
    try:
        d["data"] = json.loads(d.get("data") or "{}")
    except ValueError:
        d["data"] = {}
    d.pop("conv", None)
    return d


class OfferFilter:
    """Streams the visible reply while holding back a ``[[交给电脑:…]]`` line (and anything that may start one)."""

    def __init__(self):
        self.raw, self.sent = "", 0

    def feed(self, piece: str) -> str:
        self.raw += piece or ""
        cut = self.raw.find("[[")
        safe = self.raw if cut < 0 else self.raw[:cut]
        if cut < 0 and safe.endswith("["):
            safe = safe[:-1]
        out = safe[self.sent:] if len(safe) > self.sent else ""
        self.sent = max(self.sent, len(safe))
        return out

    def finish(self) -> tuple[str, Optional[str]]:
        m = None
        for m in OFFER_RE.finditer(self.raw):
            pass
        offer = m.group(1).strip()[:500] if m else None
        text = OFFER_RE.sub("", self.raw).strip()
        return text, offer


def one_line(text: str, limit: int = 90) -> str:
    """The first real sentence of a task result, without markdown, for 球球's one-line result."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    body = [ln for ln in lines if not ln.lstrip().startswith("#")]
    for line in body or lines:
        line = re.sub(r"^[#>*\-\s\d.、)]+", "", line).replace("**", "").replace("`", "").strip()
        if line:
            line = re.split(r"(?<=[。！？!?])", line)[0].strip() or line
            return line if len(line) <= limit else line[:limit - 1] + "…"
    return ""


def _file_kind(path: str) -> str:
    p = (path or "").lower()
    if re.search(r"\.(png|jpe?g|gif|webp|bmp)$", p):
        return "image"
    if re.search(r"\.(mp4|mov|m4v|webm)$", p):
        return "video"
    return "file"


def files_of(paths: list[str]) -> list[dict]:
    seen, out = set(), []
    for p in paths or []:
        if p and p not in seen:
            seen.add(p)
            out.append({"path": p, "name": os.path.basename(p), "kind": _file_kind(p)})
    return out


def parse_route(text: str) -> dict:
    """{"route": chat|task, "title"} from the classifier's answer; anything unclear is chat."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", t, re.S))):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(d, dict):
            route = "task" if str(d.get("route") or "").strip().lower() == "task" else "chat"
            return {"route": route, "title": str(d.get("title") or "").strip()[:30]}
    return {"route": "chat", "title": ""}


class Chat:
    def __init__(self, store, sessions, notifier=None, settings=None, secrets=None):
        self.store, self.sessions, self.notifier = store, sessions, notifier
        self.settings, self.secrets = settings, secrets
        ensure_tables(store)
        self.llm_factory: Optional[Callable[[str], Any]] = None   # tests: role ("chat" | "small") -> router
        self.memory = None            # memory.Memory: prompt block + learning after each exchange
        self.tool_loader = None       # async () -> {name: (schema, fn)}: connectors (web search); default integrations
        self._lock = asyncio.Lock()
        self._compacting = False
        self.jobs: set = set()
        self._reply_job: Optional[asyncio.Task] = None   # the chat reply being written (POST /api/chat/cancel stops it)
        self._stop_next = False      # 停止 pressed while the message was still being routed: the reply never starts
        sessions.listeners.append(self.on_task_event)
        from . import shopwatch
        shopwatch.bind(chat=self)        # price-watch questions with one-tap 立即重试 land in this chat

    # ------------------------------------------------------------------ messages
    def add(self, role: str, kind: str, text: str = "", data: Optional[dict] = None, task_id: Optional[str] = None) -> dict:
        now = time.time()
        mid = f"msg-{uuid.uuid4().hex[:12]}"
        self.store._x("INSERT INTO chat_messages (id, conv, role, kind, text, data, task_id, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (mid, CONV, role, kind, text or "",
                                                     json.dumps(data or {}, ensure_ascii=False), task_id, now, now))
        m = self.get(mid)
        self._push("new", m)
        return m

    def get(self, mid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM chat_messages WHERE id=?", (mid,))
        return _row(r[0]) if r else None

    def update(self, mid: str, text: Optional[str] = None, data: Optional[dict] = None, task_id: Optional[str] = None,
               push: bool = True) -> Optional[dict]:
        m = self.get(mid)
        if not m:
            return None
        sets, args = ["updated_at=?"], [time.time()]
        if text is not None:
            sets.append("text=?"); args.append(text)
        if data is not None:
            sets.append("data=?"); args.append(json.dumps({**m["data"], **data}, ensure_ascii=False))
        if task_id is not None:
            sets.append("task_id=?"); args.append(task_id)
        self.store._x(f"UPDATE chat_messages SET {','.join(sets)} WHERE id=?", (*args, mid))
        m = self.get(mid)
        if push:
            self._push("update", m)
        return m

    def _push(self, op: str, m: Optional[dict]):
        if not (self.notifier and m):
            return
        try:
            asyncio.get_running_loop().create_task(self.notifier.broadcast({"kind": "chat", "op": op, "message": m}))
        except RuntimeError:
            pass

    def history(self, limit: int = 60, before: Optional[int] = None) -> dict:
        limit = max(1, min(int(limit or 60), 300))
        if before:
            rows = self.store._x("SELECT * FROM chat_messages WHERE conv=? AND seq<? ORDER BY seq DESC LIMIT ?",
                                 (CONV, int(before), limit + 1))
        else:
            rows = self.store._x("SELECT * FROM chat_messages WHERE conv=? ORDER BY seq DESC LIMIT ?", (CONV, limit + 1))
        more = len(rows) > limit
        msgs = [self.fresh(_row(r)) for r in reversed(rows[:limit])]
        return {"messages": msgs, "has_more": more, "read": self.store.kv_get("chat_read", {}) or {},
                "agent_name": self.agent_name()}

    def fresh(self, m: dict) -> dict:
        """A card or a question re-checked against the task as it is now (an API restart ends every live task)."""
        d = m.get("data") or {}
        if m["kind"] == "task" and m.get("task_id"):
            t = self.store.task(m["task_id"]) or {}
            st = t.get("status")
            if st and st != d.get("status"):
                if st in ("running", "paused", "takeover") + WAITING and not self.sessions.live_for_task(t["id"]):
                    st = "interrupted"
                m = self.update(m["id"], data={"status": st, "status_line": STATUS_CN.get(st, st)}, push=False) or m
        elif m["kind"] in ("ask", "confirm") and not d.get("resolved") and not d.get("stale") and not d.get("watch_id"):
            live = self.sessions.live.get(d.get("session_id") or "")
            pending = live and (live.question_info if m["kind"] == "ask" else live.confirm_info)
            if not pending:
                m = self.update(m["id"], data={"stale": True}, push=False) or m
        return m

    def agent_name(self) -> str:
        prof = self.store.kv_get("profile", {}) or {}
        return (prof.get("agent_name") or "小秘").strip()

    def mark_read(self, seq: int) -> dict:
        r = {"seq": int(seq), "at": time.time()}
        self.store.kv_set("chat_read", r)
        return r

    # ------------------------------------------------------------------ models
    def llm(self, role: str = "chat"):
        if self.llm_factory:
            return self.llm_factory(role)
        s = self.sessions
        if getattr(s, "_custom_llm", None):
            return s._custom_llm()
        from llm_router import LLMRouter
        m = s.model_settings()
        main = m.get("model")
        if role == "chat":
            name = m.get("chat_model") or main
        else:   # classifier, summaries, memory extraction: the small model
            name = m.get("small_model") or m.get("subagent_model") or m.get("chat_model") or main
        back = main if name != main else m.get("fallback")
        key = s.secrets.get("openai_api_key") if s.secrets else None
        return LLMRouter.from_settings(name, back, key, m.get("base_url") or None, None, "off")

    # ------------------------------------------------------------------ context
    def _recent_rows(self, n: int = 400) -> list[dict]:
        rows = self.store._x("SELECT * FROM chat_messages WHERE conv=? ORDER BY seq DESC LIMIT ?", (CONV, n))
        return [_row(r) for r in reversed(rows)]

    def window(self, turns: int = CHAT_TURNS) -> list[dict]:
        """The last `turns` turns (a turn starts at a user message), oldest first."""
        rows = self._recent_rows()
        users, start = 0, 0
        for i in range(len(rows) - 1, -1, -1):
            if rows[i]["role"] == "user":
                users += 1
                if users >= turns:
                    start = i
                    break
        return rows[start:]

    @staticmethod
    def render(m: dict) -> Optional[str]:
        d, k = m.get("data") or {}, m["kind"]
        if k == "text":
            t = m.get("text") or ""
            att = d.get("attachments") or []
            if att:
                t += "\n（附件：" + "、".join(a.get("name") or os.path.basename(a.get("vm_path") or "") for a in att) + "）"
            if d.get("offer_task"):
                t += f"\n（提议交给电脑：{d['offer_task']}）"
            return t
        if k == "task":
            return f"［已交给电脑的任务「{d.get('title') or m.get('text')}」，状态：{STATUS_CN.get(d.get('status'), d.get('status'))}］"
        if k == "task_result":
            names = "、".join(f["name"] for f in d.get("files") or [])
            return (m.get("text") or "") + (f"（文件：{names}）" if names else "")
        if k == "ask":
            return f"［任务在问用户：{(m.get('text') or '')[:200]}］"
        if k == "confirm":
            return f"［任务请用户确认：{(m.get('text') or '')[:200]}］"
        return m.get("text") or None

    def summary(self) -> dict:
        return self.store.kv_get("chat_summary", {}) or {}

    def build_messages(self, query: str = "") -> list[dict]:
        win = self.window()
        start = win[0]["seq"] if win else 0
        summ = self.summary()
        upto = int(summ.get("upto") or 0)
        gap = self.store._x("SELECT * FROM chat_messages WHERE conv=? AND seq>? AND seq<? ORDER BY seq DESC LIMIT 40",
                            (CONV, upto, start))
        tools = ["读网页（read_url）", "读文件（read_file）", "查任务和例程的状态（task_status）"]
        if getattr(self, "_search_names", None):
            tools.insert(0, "联网搜索")
        system = SYSTEM.format(name=self.agent_name(), now=time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600)),
                               tools="、".join(tools))
        prof = self.store.kv_get("profile", {}) or {}
        if prof.get("tone"):
            system += f"\n回复语气：{prof['tone']}。"
        if self.memory:
            try:
                block = self.memory.prompt_block(query)
                if block:
                    system += "\n\n" + block
            except Exception as e:  # noqa: BLE001
                log.warning("memory block: %s", e)
        if summ.get("text"):
            system += "\n\n更早的聊天摘要：\n" + summ["text"]
        if gap:
            lines = []
            for r in reversed(gap):
                r = _row(r)
                t = self.render(r)
                if t:
                    lines.append(f"{'用户' if r['role'] == 'user' else '助理'}：{t[:200]}")
            system += "\n\n再早一点的聊天（摘要还没整理进去）：\n" + "\n".join(lines)
        msgs = [{"role": "system", "content": system}]
        for r in win:
            t = self.render(r)
            if not t:
                continue
            role = "user" if r["role"] == "user" else "assistant"
            if msgs[-1]["role"] == role and len(msgs) > 1:   # some gateways refuse two turns of one role in a row
                msgs[-1]["content"] += "\n\n" + t
            else:
                msgs.append({"role": role, "content": t})
        if msgs[-1]["role"] != "user" and query:
            msgs.append({"role": "user", "content": query})
        return msgs

    def recent_text(self, turns: int = 6) -> str:
        lines = []
        for r in self.window(turns)[:-1]:
            t = self.render(r)
            if t:
                lines.append(f"- {'用户' if r['role'] == 'user' else '助理'}：{t[:300]}")
        return ("EARLIER IN THE USER'S CHAT WITH THE ASSISTANT (oldest first; the request below came from it):\n"
                + "\n".join(lines)) if lines else ""

    async def compact(self, force: bool = False) -> bool:
        """Fold messages that left the verbatim window into the rolling summary (background, one at a time)."""
        if self._compacting:
            return False
        win = self.window()
        start = win[0]["seq"] if win else 0
        summ = self.summary()
        upto = int(summ.get("upto") or 0)
        gap = [_row(r) for r in self.store._x("SELECT * FROM chat_messages WHERE conv=? AND seq>? AND seq<? "
                                              "ORDER BY seq LIMIT 200", (CONV, upto, start))]
        if not gap or (len(gap) < COMPACT_MIN and not force):
            return False
        self._compacting = True
        try:
            convo = "\n".join(f"{'用户' if r['role'] == 'user' else '助理'}：{(self.render(r) or '')[:600]}" for r in gap)
            new = await asyncio.wait_for(self.llm("small").complete(
                SUMMARY_PROMPT, f"已有摘要：\n{summ.get('text') or '（无）'}\n\n新的对话：\n{convo}", 1200), 120)
            new = re.sub(r"<think>.*?</think>", "", new or "", flags=re.S).strip()
            if not new:
                return False
            self.store.kv_set("chat_summary", {"text": new[:3000], "upto": gap[-1]["seq"], "updated_at": time.time()})
            return True
        except Exception as e:  # noqa: BLE001  the gap stays in the prompt until a later run succeeds
            log.warning("chat compaction failed: %s", e)
            return False
        finally:
            self._compacting = False

    # ------------------------------------------------------------------ routing
    async def classify(self, text: str) -> dict:
        recent = "\n".join(f"{'用户' if r['role'] == 'user' else '助理'}：{(self.render(r) or '')[:160]}"
                           for r in self.window(3)[:-1])
        user = (f"最近的聊天：\n{recent}\n\n" if recent else "") + f"最新消息：{text[:1500]}"
        try:
            out = await asyncio.wait_for(self.llm("small").complete(ROUTE_PROMPT, user, 200), 30)
            return parse_route(out)
        except Exception as e:  # noqa: BLE001  unsure (or no model) means chat
            log.warning("route failed: %s", e)
            return {"route": "chat", "title": "", "error": str(e)[:200]}

    # ------------------------------------------------------------------ the exchange
    async def send(self, text: str, attachments: Optional[list] = None, machine_id: Optional[str] = None,
                   mode: str = "auto", on_event: Optional[Callable[[str, Any], Awaitable[None]]] = None) -> dict:
        async def say(kind: str, data):
            if on_event:
                try:
                    await on_event(kind, data)
                except Exception:  # noqa: BLE001
                    pass
        text = (text or "").strip()
        attachments = [a for a in (attachments or []) if isinstance(a, dict)][:10]
        async with self._lock:
            self._stop_next = False
            user = self.add("user", "text", text, {"attachments": attachments} if attachments else {})
            await say("user", user)
            if self.memory:   # 记住… / 忘掉… act at once, no model reply needed
                try:
                    done = await self.memory.command(text, user["id"])
                except Exception as e:  # noqa: BLE001
                    done = None
                    log.warning("memory command: %s", e)
                if done:
                    m = self.add("assistant", "text", done, {"memory": True})
                    await say("route", {"route": "memory"})
                    await say("message", m)
                    return {"route": "memory", "user": user, "messages": [m]}
            if mode in ("chat", "task"):
                route = {"route": mode, "title": ""}
            elif SKILL_RE.match(text):   # a skill picked with "/" (or the + menu) runs on the computer, like the old skills menu
                route = {"route": "task", "title": "", "skill": True}
            else:
                route = await self.classify(text)
            await say("route", route)
            if route["route"] == "task":
                card = await self.start_task(text, route.get("title") or "", machine_id, attachments, origin=user["id"])
                await say("task" if card["kind"] == "task" else "message", card)
                out = {"route": "task", "user": user, "messages": [card]}
            else:
                await say("status", {"text": "正在想…"})
                m = await self._reply_cancellable(text, say)
                await say("message", m)
                out = {"route": "chat", "user": user, "messages": [m]}
        self.background(self.after_exchange(user, out["messages"]))
        return out

    async def _reply_cancellable(self, text: str, say) -> dict:
        """The reply runs as its own task so 停止 (POST /api/chat/cancel) can end it; what streamed so far is kept."""
        shown: list[str] = []
        if self._stop_next:
            self._stop_next = False
            return self.add("assistant", "text", "（已停止，这次没有回复）", {"stopped": True})

        async def delta(p):
            shown.append(p)
            await say("delta", {"text": p})

        async def status(t):
            await say("status", {"text": t})
        job = asyncio.get_running_loop().create_task(self.reply(text, delta, status))
        self._reply_job = job
        try:
            return await job
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is not None and me.cancelling():   # the request itself is going away: let it
                raise
            body = "".join(shown).strip()
            return self.add("assistant", "text", (body + "\n\n（已停止）") if body else "（已停止，这次没有回复）",
                            {"stopped": True})
        finally:
            self._reply_job = None

    def cancel(self) -> bool:
        """Stop the chat reply being written. False when nothing is being written (a task is stopped from its card)."""
        job = self._reply_job
        if job is not None and not job.done():
            job.cancel()
            return True
        if self._lock.locked():      # still routing: the reply will not start (a task that already started is stopped from its card)
            self._stop_next = True
            return True
        return False

    def background(self, coro):
        job = asyncio.get_running_loop().create_task(coro)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return job

    async def after_exchange(self, user: dict, replies: list[dict]):
        await self.compact()
        if self.memory:
            try:
                await self.memory.learn(user, replies)
            except Exception as e:  # noqa: BLE001
                log.warning("memory learn: %s", e)

    async def reply(self, text: str, on_delta=None, on_status=None) -> dict:
        tools = await self.tools()
        if self.memory and hasattr(self.memory, "prepare"):   # embeds the question when an embedding model is set
            try:
                await self.memory.prepare(text)
            except Exception as e:  # noqa: BLE001
                log.warning("memory prepare: %s", e)
        messages = self.build_messages(text)
        schemas = [t[0] for t in tools.values()]
        parts, offer = [], None
        try:
            llm = self.llm("chat")
            for rnd in range(TOOL_ROUNDS + 1):
                flt = OfferFilter()

                async def cb(piece, _f=flt):
                    vis = _f.feed(piece)
                    if vis and on_delta:
                        await on_delta(vis)
                r = await llm.chat(messages, schemas if (schemas and rnd < TOOL_ROUNDS) else None, 1500, cb)
                if not flt.raw:   # the provider gave the answer without calling back
                    flt.feed(r.get("content") or "")
                vis, off = flt.finish()
                if vis:
                    parts.append(vis)
                offer = off or offer
                calls = r.get("tool_calls") or []
                if not calls:
                    break
                messages.append({"role": "assistant", "content": r.get("content") or "", "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False)}}
                    for c in calls]})
                for c in calls:
                    if on_status:
                        try:
                            await on_status(self.tool_status(c["name"]))
                        except Exception:  # noqa: BLE001
                            pass
                    res = await self.run_tool(tools, c["name"], c.get("args") or {})
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": res[:6000]})
            body = "\n\n".join(p for p in parts if p).strip()
            if not body and offer:
                body = "这件事需要在电脑上动手，我可以交给电脑去做。"
            if not body:
                body = "嗯，我在。"
            data = {"offer_task": offer} if offer else {}
            return self.add("assistant", "text", body, data)
        except Exception as e:  # noqa: BLE001  never silent: say what went wrong and offer the computer instead
            log.warning("chat reply failed: %s", e)
            return self.add("assistant", "text", f"抱歉，模型这次没有回应（{type(e).__name__}: {str(e)[:120]}）。"
                            "可以稍后再发一次，或者点下面的按钮交给电脑去做。", {"error": True, "offer_task": text[:500]})

    # ------------------------------------------------------------------ tools for the chat
    async def tools(self) -> dict:
        out = {
            "read_url": ({"name": "read_url", "description": "读取一个网页（http/https）的正文文字",
                          "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
                         self._read_url),
            "read_file": ({"name": "read_file", "description": "读取用户上传的文件或任务产出的文件（给路径或文件名）",
                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
                          self._read_file),
            "task_status": ({"name": "task_status", "description": "查看用户的任务（最近、正在运行、失败的）和例程（定时任务）的状态",
                             "parameters": {"type": "object", "properties": {
                                 "query": {"type": "string", "description": "按任务内容筛选，可留空"},
                                 "task_id": {"type": "string"}}}},
                            self._task_status),
        }
        self._search_names = []
        try:
            loader = self.tool_loader
            if loader is None and self.secrets is not None:
                from .integrations import load_tools

                async def loader():
                    return await load_tools(self.store, self.secrets)
            ext = await asyncio.wait_for(loader(), 10) if loader else {}
            for name, (schema, fn) in (ext or {}).items():
                if re.search(r"search|搜索|tavily|exa|bing|serp|google", name + " " + (schema.get("description") or "")[:80], re.I) \
                        and len(self._search_names) < 3:
                    out[name] = (schema, fn)
                    self._search_names.append(name)
        except Exception as e:  # noqa: BLE001
            log.warning("chat search tools: %s", e)
        return out

    def tool_status(self, name: str) -> str:
        """The short line under 球球 while it thinks (正在查…)."""
        if name in (getattr(self, "_search_names", None) or []):
            return "正在搜索…"
        return {"read_url": "正在读网页…", "read_file": "正在读文件…", "task_status": "正在查任务…"}.get(name, "正在查…")

    async def run_tool(self, tools: dict, name: str, args: dict) -> str:
        if name not in tools:
            return f"没有这个工具：{name}"
        try:
            res = await asyncio.wait_for(tools[name][1](args), 40)
        except Exception as e:  # noqa: BLE001
            return f"工具出错：{type(e).__name__}: {str(e)[:200]}"
        return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)

    async def _read_url(self, a: dict) -> str:
        import httpx
        from .scheduler import UA, page_text
        url = str(a.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return "只能读取 http(s) 网址"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
        ctype = r.headers.get("content-type") or ""
        text = page_text(r.text) if "html" in ctype or "<html" in r.text[:500].lower() else r.text
        return f"[{r.status_code}] {url}\n{text[:8000]}"

    def _allowed(self, path: str) -> Optional[str]:
        root = os.path.realpath((self.settings.artifacts_dir if self.settings else None) or "workspace/artifacts")
        p = str(path or "").strip()
        cands = [p] if os.path.isabs(p) else [os.path.join(root, p), os.path.join(root, "uploads", p)]
        for c in cands:
            real = os.path.realpath(c)
            if real.startswith(root + os.sep) and os.path.isfile(real):
                return real
        name = os.path.basename(p)
        up = os.path.join(root, "uploads")
        if name and os.path.isdir(up):   # an upload is stored as <time>-<name>
            for f in sorted(os.listdir(up), reverse=True):
                if f == name or f.endswith("-" + name):
                    return os.path.join(up, f)
        return None

    async def _read_file(self, a: dict) -> str:
        real = self._allowed(a.get("path"))
        if not real:
            return "找不到这个文件（只能读用户上传的文件和任务产出的文件）"
        low = real.lower()
        try:
            if low.endswith(".pdf"):
                try:
                    from pypdf import PdfReader
                    return "\n".join((p.extract_text() or "") for p in PdfReader(real).pages[:30])[:8000]
                except ImportError:
                    return "这台服务器没装 PDF 读取库，交给电脑去读吧"
            if low.endswith(".docx"):
                import zipfile
                with zipfile.ZipFile(real) as z:
                    xml = z.read("word/document.xml").decode("utf-8", "ignore")
                return re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n"))[:8000]
            if os.path.getsize(real) > 5 * 1024 * 1024:
                return "文件太大，交给电脑去处理吧"
            raw = open(real, "rb").read()
            if b"\x00" in raw[:2000]:
                return f"这是二进制文件（{os.path.basename(real)}），聊天里读不了内容"
            return raw.decode("utf-8", "ignore")[:8000]
        except Exception as e:  # noqa: BLE001
            return f"读不了这个文件：{e}"

    async def _task_status(self, a: dict) -> str:
        if a.get("task_id"):
            t = self.store.task(str(a["task_id"]))
            rows = [t] if t else []
        else:
            rows = self.store.recent_tasks(40)
            q = str(a.get("query") or "").strip()
            if q:
                rows = [r for r in rows if q in (r.get("prompt") or "")] or rows
            rows = [r for r in rows if not r.get("parent_id")][:12]
        tasks = [{"id": r["id"], "title": (r.get("prompt") or "")[:60], "status": STATUS_CN.get(r["status"], r["status"]),
                  "when": time.strftime("%m-%d %H:%M", time.gmtime((r.get("created_at") or 0) + 8 * 3600)),
                  "result": (r.get("result") or "")[:200]} for r in rows]
        routines = [{"name": r.get("name"), "schedule": r.get("schedule"), "enabled": bool(r.get("enabled")),
                     "next": time.strftime("%m-%d %H:%M", time.gmtime(r["next_run"] + 8 * 3600)) if r.get("next_run") else None}
                    for r in self.store.all("routines", order="created_at ASC", limit=30)]
        return json.dumps({"tasks": tasks, "routines": routines}, ensure_ascii=False)

    # ------------------------------------------------------------------ tasks from the chat
    async def start_task(self, prompt: str, title: str = "", machine_id: Optional[str] = None,
                         attachments: Optional[list] = None, origin: Optional[str] = None, source: str = "chat") -> dict:
        prompt = (prompt or "").strip()
        attachments = await self._push_files(attachments or [], machine_id)
        vm = [a.get("vm_path") for a in attachments if a.get("vm_path")]
        if vm:
            prompt += "\n\n附件（已放到电脑上）：\n" + "\n".join("- " + p for p in vm)
        title = (title or "").strip() or _short_title(prompt)
        try:
            res = await self.sessions.submit(prompt, machine_id or None, None, source=source, title=title,
                                             context_extra=self.recent_text(6) or None)
        except Exception as e:  # noqa: BLE001  never silent
            return self.add("assistant", "text", f"没能把任务交给电脑：{type(e).__name__}: {str(e)[:160]}",
                            {"error": True, "offer_task": prompt[:500]})
        t = res["task"]
        st = t.get("status") or ("queued" if res.get("queued") else "running")
        return self.add("assistant", "task", title, {
            "task_id": t["id"], "thread_id": res.get("thread_id") or t.get("thread_id"), "session_id": t.get("session_id"),
            "machine_id": t.get("machine_id"), "title": title, "status": st, "status_line": STATUS_CN.get(st, st),
            "step": 0, "max_steps": t.get("max_steps"), "origin": origin}, task_id=t["id"])

    async def _push_files(self, attachments: list, machine_id: Optional[str]) -> list:
        """Chat attachments live on the API host (local_only uploads); a task needs them on its computer."""
        out = []
        mid = machine_id or (self.settings.machines[0].id if self.settings and self.settings.machines else "ai-linux-001")
        for a in attachments:
            a = dict(a)
            local = a.get("artifact")
            if not a.get("vm_path") and local and self._allowed(local):
                name = a.get("name") or os.path.basename(local).split("-", 1)[-1]
                try:
                    cloud = getattr(self.sessions, "cloud", None)
                    if cloud and cloud.is_cloud(mid):
                        await cloud.wake(mid)
                    from .uploads import push_to_vm   # in 16 MB pieces: videos are bigger than one agent message
                    await push_to_vm(self.sessions.computer(mid), self._allowed(local), f"uploads/{name}")
                    a["vm_path"] = f"~/workspace/uploads/{name}"
                except Exception as e:  # noqa: BLE001  the task still starts; its prompt names the missing file and why
                    log.warning("chat attachment upload: %s", e)
                    a["vm_error"] = str(e)[:200]
                if not a.get("vm_path"):
                    a["vm_path"] = f"（没能放到电脑上：{name}" + (f"，{a['vm_error']}" if a.get("vm_error") else "") + "）"
            out.append(a)
        return out

    async def to_task(self, mid: str, machine_id: Optional[str] = None) -> dict:
        m = self.get(mid)
        if not m or not (m["data"] or {}).get("offer_task"):
            raise KeyError(mid)
        if m["data"].get("task_started"):
            return self.get(m["data"]["task_started"]) or m
        async with self._lock:
            card = await self.start_task(m["data"]["offer_task"], "", machine_id, origin=mid)
            if card["kind"] == "task":
                self.update(mid, data={"task_started": card["id"]})
        return card

    def card_for(self, task_id: str, thread_id: Optional[str]) -> Optional[dict]:
        r = self.store._x("SELECT * FROM chat_messages WHERE kind='task' AND task_id=? ORDER BY seq DESC LIMIT 1", (task_id,))
        if not r and thread_id:   # a retry / follow-up in the task view: the same card follows the newest task
            r = self.store._x("SELECT * FROM chat_messages WHERE kind='task' AND data LIKE ? ORDER BY seq DESC LIMIT 1",
                              (f'%"thread_id": "{thread_id}"%',))
        return _row(r[0]) if r else None

    async def on_task_event(self, task, sid: str, msg: dict):
        card = self.card_for(task.id, getattr(task, "thread_id", None))
        if not card:
            return
        typ = msg.get("type")
        d = msg.get("data") or {}
        cd = card["data"]
        base = {"task_id": task.id, "thread_id": cd.get("thread_id") or task.thread_id, "session_id": sid,
                "title": cd.get("title") or card.get("text")}
        if typ == "task.started":
            self.update(card["id"], data={**base, "status": "running", "status_line": "进行中", "step": 0,
                                          "max_steps": task.max_steps}, task_id=task.id)
        elif typ == "agent.decision":
            step = msg.get("step") or cd.get("step") or 0
            status = cd.get("status") if cd.get("status") in WAITING else "running"
            if step != cd.get("step") or status != cd.get("status"):
                self.update(card["id"], data={**base, "step": step, "status": status,
                                              "status_line": self._line(cd, status)}, task_id=task.id)
        elif typ in ("agent.plan", "agent.plan_update"):
            steps = d.get("plan") if typ == "agent.plan" else cd.get("plan")
            steps = steps or []
            done = d.get("done") or []
            cur = next((s for i, s in enumerate(steps) if i not in set(done)), "")
            self.update(card["id"], data={**base, "plan": steps[:8], "plan_done": len(set(done)), "plan_total": len(steps),
                                          "current": str(cur)[:60], "status_line": str(cur)[:60] or self._line(cd, "running")})
        elif typ == "agent.input_required":
            await self._ask_message(task, sid, card, d)
        elif typ in ("agent.input_answered", "agent.confirm_answered"):
            kind = "confirm" if typ == "agent.confirm_answered" else "ask"
            ans = d.get("approve") if kind == "confirm" else d.get("answer")
            for r in self.store._x("SELECT * FROM chat_messages WHERE kind=? AND task_id=? ORDER BY seq DESC LIMIT 3",
                                   (kind, task.id)):
                m = _row(r)
                if not m["data"].get("resolved"):
                    self.update(m["id"], data={"resolved": {"answer": ans if not isinstance(ans, str) else ans[:200],
                                                            "by": d.get("by"), "at": time.time()}})
            self.update(card["id"], data={**base, "status": "running", "status_line": self._line(cd, "running")})
        elif typ == "agent.confirm_required":
            s = d.get("summary") or {}
            self.add("assistant", "confirm", s.get("title") or d.get("reason") or "需要你确认", {
                **base, "summary": {k: s.get(k) for k in ("title", "what", "risk", "risk_cn", "why", "chips", "category")},
                "confirm_id": d.get("id"), "task_allow": d.get("task_allow")}, task_id=task.id)
            self.update(card["id"], data={**base, "status": "waiting_confirm", "status_line": "等你确认"})
        elif typ == "task.end":
            st = msg.get("status") or "failed"
            self.update(card["id"], data={**base, "status": st, "status_line": STATUS_CN.get(st, st)}, task_id=task.id)
            files = files_of(msg.get("files") or [])
            res = msg.get("result") or ""
            line = one_line(res)
            if st == "succeeded":
                text = line or "做完了。"
            elif st == "stopped":
                text = "任务已停止。" + (f"{line}" if line and "停止" not in line[:6] else "")
            else:
                text = line or "任务没有完成。"
                if not text.startswith(("任务没", "没", "抱歉")):
                    text = "任务没有完成：" + text
            self.add("assistant", "task_result", text, {**base, "status": st, "files": files,
                                                        "has_more": len(res.strip()) > len(line) + 2}, task_id=task.id)

    def _line(self, cd: dict, status: str) -> str:
        return cd.get("current") or STATUS_CN.get(status, status)

    async def _ask_message(self, task, sid: str, card: dict, d: dict):
        form = d.get("form") or {}
        opts = [str(o) for o in d.get("options") or []]
        q = str(d.get("question") or form.get("title") or "需要你")
        if form.get("kind") == "takeover":
            kind, text = "takeover", form.get("title") or q
        elif form.get("kind") == "form":
            kind, text = "form", form.get("title") or q
        elif opts[:3] == ESC_OPTIONS:
            kind, text = "escalate", q
        else:
            kind, text = "question", q
        data = {"task_id": task.id, "thread_id": card["data"].get("thread_id") or task.thread_id, "session_id": sid,
                "title": card["data"].get("title"), "ask_kind": kind, "question": q, "options": opts}
        if kind == "takeover":
            data["takeover"] = {k: form.get(k) for k in ("site", "url", "steps", "reason", "machine", "machine_id", "action_cn")}
        if kind == "form":
            data["form_title"] = form.get("title")
        self.add("assistant", "ask", text, data, task_id=task.id)
        self.update(card["id"], data={"status": "waiting_input",
                                      "status_line": "需要你接管" if kind == "takeover" else "等你回答"})

    async def answer(self, mid: str, choice: Optional[str] = None, approve: Optional[bool] = None,
                     text: Optional[str] = None) -> dict:
        m = self.get(mid)
        if not m or m["kind"] not in ("ask", "confirm"):
            raise KeyError(mid)
        d = m["data"]
        if d.get("resolved"):
            raise ValueError("这个问题已经回答过了")
        if d.get("watch_id"):           # a price watch's question (立即重试 / 暂停), not a task's
            from . import shopwatch
            return await shopwatch.answer_ask(self, m, (choice if choice is not None else text) or OPT_RETRY_WATCH)
        sid = d.get("session_id") or ""
        try:
            if m["kind"] == "confirm":
                ok = self.sessions.answer_confirm(sid, bool(approve))
                ans = bool(approve)
            else:
                ans = (choice if choice is not None else text) or ""
                if not ans.strip():
                    raise ValueError("回答是空的")
                ok = self.sessions.answer_question(sid, ans)
        except KeyError:
            ok = False
        if not ok:
            self.update(mid, data={"stale": True})
            raise ValueError("这个问题已经过期（任务已经不在等这个回答了）")
        m = self.update(mid, data={"resolved": {"answer": ans, "by": "chat", "at": time.time()}})
        return {"ok": True, "message": m, "open_task": isinstance(ans, str) and "接管" in ans}


OPT_RETRY_WATCH = "立即重试"


def _short_title(prompt: str) -> str:
    t = re.sub(r"【技能：[^】]*】", "", prompt or "").strip().splitlines()
    t = t[0] if t else ""
    return t[:24] + ("…" if len(t) > 24 else "") or "新任务"


# ---------------------------------------------------------------------------------------------- HTTP
from pydantic import BaseModel  # noqa: E402


class ChatIn(BaseModel):
    text: str = ""
    attachments: list = []
    machine_id: Optional[str] = None
    mode: str = "auto"          # auto | chat | task (新建任务 / a forced route)
    stream: bool = True

class AnswerIn(BaseModel):
    choice: Optional[str] = None
    approve: Optional[bool] = None
    text: Optional[str] = None

class ToTaskIn(BaseModel):
    machine_id: Optional[str] = None

class ReadIn(BaseModel):
    seq: int


def mount(app, chat: Chat, auth) -> None:
    from fastapi import Depends, HTTPException
    from fastapi.responses import StreamingResponse

    dep = [Depends(auth)]

    @app.get("/api/chat", dependencies=dep)
    async def chat_history(limit: int = 60, before: Optional[int] = None):
        return chat.history(limit, before)

    @app.get("/api/chat/summary", dependencies=dep)
    async def chat_summary():
        return chat.summary()

    @app.post("/api/chat/read", dependencies=dep)
    async def chat_read(body: ReadIn):
        return chat.mark_read(body.seq)

    @app.post("/api/chat/send", dependencies=dep)
    async def chat_send(body: ChatIn):
        if not body.text.strip() and not body.attachments:
            raise HTTPException(400, "empty message")
        if body.machine_id and not chat.store.machine(body.machine_id):
            raise HTTPException(404, "machine not found")
        q: asyncio.Queue = asyncio.Queue()

        async def on_event(kind, data):
            await q.put((kind, data))

        async def run():
            try:
                return await chat.send(body.text, body.attachments, body.machine_id, body.mode, on_event)
            except Exception as e:  # noqa: BLE001  never silent
                log.exception("chat send")
                m = chat.add("assistant", "text", f"出错了：{type(e).__name__}: {str(e)[:160]}", {"error": True})
                await q.put(("message", m))
            finally:
                await q.put(None)
        job = chat.background(run())   # the reply finishes and is stored even if this client goes away
        if not body.stream:
            await job
            events = []
            while not q.empty():
                it = q.get_nowait()
                if it:
                    events.append({"event": it[0], "data": it[1]})
            return {"events": events, "messages": [e["data"] for e in events if e["event"] in ("user", "message", "task")]}

        async def gen():
            yield ": ok\n\n"
            while True:
                it = await q.get()
                if it is None:
                    yield "event: done\ndata: {}\n\n"
                    break
                yield f"event: {it[0]}\ndata: {json.dumps(it[1], ensure_ascii=False)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/chat/cancel", dependencies=dep)
    async def chat_cancel():
        return {"cancelled": chat.cancel()}

    @app.post("/api/chat/messages/{mid}/answer", dependencies=dep)
    async def chat_answer(mid: str, body: AnswerIn):
        try:
            return await chat.answer(mid, body.choice, body.approve, body.text)
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/chat/messages/{mid}/to_task", dependencies=dep)
    async def chat_to_task(mid: str, body: ToTaskIn):
        try:
            return await chat.to_task(mid, body.machine_id)
        except KeyError:
            raise HTTPException(404, "no task offer on this message")
