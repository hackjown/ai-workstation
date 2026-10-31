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

3.8.8 (task card v2): a task is ONE card in the chat, keyed by its task (data.root_id = the first run), created when
the task starts and updated in place (op "update" over /ws/events): title + prompt summary, status, step, the latest
one-line progress, started_at / ended_at for the elapsed time. Whatever needs the user (takeover, slider/captcha,
escalation, a question with option buttons, a form, a confirmation) is an entry of data.actions ON the card, answered
with POST /api/chat/messages/{card}/answer {action_id, choice|approve|text}; answered ones collapse to a ✓ line. The
end is data.result on the card (summary, files, thumb). A follow-up (继续 / 重试 / 补充 in the task view or the chat)
re-arms the SAME card and moves it to the bottom (its seq is bumped). Old ask / confirm / task_result messages are
never rewritten and still render and answer as before.
"""
from __future__ import annotations

import asyncio
import contextlib
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
CARD_V = 2              # task card v2: actions + result live on the card (3.8.8)
MAX_ACTIONS = 12        # answered actions kept on a card (the newest; the task view keeps them all)

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
    res = d["data"].get("resolved") if isinstance(d["data"], dict) else None
    if isinstance(res, dict) and "text" not in res:   # answered before 3.7: derive what the card shows
        a = res.get("answer")
        res["text"] = ("已允许" if a else "已拒绝") if isinstance(a, bool) else answer_text(a, d["data"].get("fields"))
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


def answer_text(ans: Any, fields: Optional[list] = None, cancelled: bool = False) -> str:
    """What a resolved ask/form card shows after 已回答 (3.7: was the raw answer, so a form answered with nothing came
    out as "<null>" on iOS). "" = nothing to show (the card says just 已回答). A form answer is the JSON of its non-secret
    values: shown as 标签：值 with the field labels; secret fields never reach here."""
    if cancelled or (isinstance(ans, str) and ans.strip().lower() in ("__cancel__", "cancel")):
        return "已取消"
    if ans is None or isinstance(ans, bool):
        return ""
    s = str(ans).strip()
    if not s or s.lower() in ("null", "none", "{}"):
        return ""
    if s == "done":
        return "已完成，交还给电脑"
    if s.startswith("{"):
        try:
            vals = json.loads(s)
        except ValueError:
            vals = None
        if isinstance(vals, dict):
            label = {str(f.get("key")): str(f.get("label") or f.get("key")) for f in fields or [] if isinstance(f, dict)}
            parts = [f"{label.get(k, k)}：{v}" for k, v in vals.items() if str(v or "").strip()]
            return one_line("；".join(parts), 80)
    return one_line(s, 80)


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


ROUTE_TASKS = """
最近交给电脑的任务（编号: 状态 · 标题）：
{tasks}
如果最新消息是在接着其中某个任务说（继续、我上传了、改一下、补充要求、回答它的问题、"就用第二个"），输出 {{"route": "continue", "task": 编号}}。
拿不准是哪个任务就输出 {{"route": "continue", "task": 0}}（会问用户）。是新的事就照常输出 chat 或 task。"""


def parse_route(text: str) -> dict:
    """{"route": chat|task|continue, "title"[, "task"]} from the classifier's answer; anything unclear is chat."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", t, re.S))):
        try:
            d = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(d, dict):
            r = str(d.get("route") or "").strip().lower()
            if r == "continue":
                try:
                    n = int(d.get("task") or 0)
                except (TypeError, ValueError):
                    n = 0
                return {"route": "continue", "title": "", "task": n}
            route = "task" if r == "task" else "chat"
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

    def delete_message(self, mid: str) -> dict:
        """3.8.2: delete one message of the chat. A task card of a queued task also takes the task out of the queue;
        a card whose task is still running is refused (stop the task first) so nothing keeps running unseen."""
        m = self.get(mid)
        if not m:
            raise KeyError(mid)
        cancelled = False
        if m["kind"] == "task" and m.get("task_id"):
            t = self.store.task(m["task_id"]) or {}
            st = t.get("status")
            if st == "queued":
                cancelled = bool(self.sessions and self.sessions.cancel_queued(t["id"]))
            elif st in ("pending", "running", "paused", "takeover", "waiting_confirm", "waiting_input"):
                raise ValueError("这个任务还在进行：先停止它，再删除这条")
        self.store._x("DELETE FROM chat_messages WHERE id=?", (mid,))
        self._push("delete", {"id": mid, "deleted": True, "task_id": m.get("task_id")})
        return {"deleted": True, "id": mid, "cancelled_task": cancelled}

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
        msgs = self.fold_old(msgs)
        return {"messages": msgs, "has_more": more, "read": self.store.kv_get("chat_read", {}) or {},
                "agent_name": self.agent_name()}

    # ------------------------------------------------------------------ 3.8.9: old task messages, folded when shown
    OLD_TASK_KINDS = ("task", "ask", "confirm", "task_result")

    def fold_old(self, msgs: list[dict]) -> list[dict]:
        """Chat written before the 3.8.8 task card (ccd7a59) has one bubble per task event: the v1 task card, then
        需要你接管 / 任务卡住了 questions, confirmations and the 任务已停止 / result message, all with a task_id. Shown
        now, each task's old messages become ONE v2 task card built from the task record, at the place of its first
        message. Render-time only: nothing in the database is rewritten, and a group that still waits for an answer
        (a live question) is left as it is so it can be answered."""
        from . import continuation as _cont
        groups: dict[str, list[int]] = {}
        for i, m in enumerate(msgs):
            d = m.get("data") or {}
            if m.get("role") != "assistant" or m.get("kind") not in self.OLD_TASK_KINDS or d.get("card") == CARD_V:
                continue
            tid = m.get("task_id") or d.get("task_id")
            if not tid:
                continue
            try:
                root = d.get("root_id") or _cont.root_id(self.store, tid)
            except Exception:  # noqa: BLE001
                root = tid
            groups.setdefault(root, []).append(i)
        if not groups:
            return msgs
        drop, put = set(), {}
        for root, idx in groups.items():
            ms = [msgs[i] for i in idx]
            if len(ms) < 2 and ms[0]["kind"] == "task":
                continue          # a lone old card is already one card
            if any(m["kind"] in ("ask", "confirm") and not (m.get("data") or {}).get("resolved")
                   and not (m.get("data") or {}).get("stale") for m in ms):
                continue          # still waiting for the user: keep the answerable bubbles
            put[idx[0]] = self._folded_card(root, ms)
            drop.update(idx[1:])
        return [put.get(i, m) for i, m in enumerate(msgs) if i not in drop]

    def _folded_card(self, root: str, ms: list[dict]) -> dict:
        first = ms[0]
        card = next((m for m in ms if m["kind"] == "task"), None)
        cd = (card or {}).get("data") or {}
        runs = []
        try:
            runs = self.store.continuations(root) or []
        except Exception:  # noqa: BLE001
            runs = []
        tids = [m.get("task_id") for m in ms if m.get("task_id")]
        last_tid = tids[-1] if tids else root
        t = self.store.task(last_tid) or self.store.task(root) or {}
        st = t.get("status") or cd.get("status") or "interrupted"
        if st in ("running", "paused", "takeover") + WAITING and not (self.sessions and self.sessions.live_for_task(t.get("id") or "")):
            st = "interrupted"
        title = cd.get("title") or (card or {}).get("text") or t.get("title") or one_line(t.get("prompt") or "", 30) or "任务"
        actions = []
        for m in ms:
            if m["kind"] not in ("ask", "confirm"):
                continue
            d = m.get("data") or {}
            a = {"id": "old-" + m["id"], "kind": m["kind"], "ask_kind": d.get("ask_kind") or m["kind"],
                 "question": d.get("question") or m.get("text") or "", "options": d.get("options") or [],
                 "at": m.get("created_at"), "task_id": m.get("task_id"), "folded_from": m["id"]}
            for k in ("summary", "takeover", "fields", "form_title", "stuck"):
                if d.get(k):
                    a[k] = d[k]
            if d.get("resolved"):
                a["resolved"] = d["resolved"]
            else:
                a["stale"] = True
            actions.append(a)
        res_msg = next((m for m in reversed(ms) if m["kind"] == "task_result"), None)
        result = None
        if res_msg:
            rd = res_msg.get("data") or {}
            files = rd.get("files") or []
            result = {"text": res_msg.get("text") or "", "status": rd.get("status") or st, "files": files,
                      "thumb": next((f.get("path") for f in files if isinstance(f, dict) and f.get("kind") == "image"), None),
                      "has_more": bool(rd.get("has_more")), "at": res_msg.get("created_at")}
        elif st in FINAL:
            line = one_line(t.get("result") or "")
            result = {"text": line or STATUS_CN.get(st, st), "status": st, "files": [], "thumb": None,
                      "has_more": len((t.get("result") or "").strip()) > len(line) + 2, "at": t.get("updated_at")}
        data = {"card": CARD_V, "folded": True, "folded_ids": [m["id"] for m in ms], "root_id": root,
                "task_id": t.get("id") or last_tid, "thread_id": cd.get("thread_id") or t.get("thread_id"),
                "machine_id": cd.get("machine_id") or t.get("machine_id"), "title": title,
                "prompt": cd.get("prompt") or one_line(SKILL_RE.sub("", t.get("prompt") or ""), 120),
                "status": st, "status_line": STATUS_CN.get(st, st), "step": cd.get("step") or t.get("steps") or 0,
                "max_steps": cd.get("max_steps") or t.get("max_steps"), "started_at": cd.get("started_at") or t.get("created_at")
                or first.get("created_at"), "ended_at": cd.get("ended_at") or (res_msg or {}).get("created_at")
                or (t.get("updated_at") if st in FINAL else None),
                "runs": max(1, len(runs)), "actions": actions[-MAX_ACTIONS:], "result": result}
        return {**first, "id": card["id"] if card else "fold-" + root, "kind": "task", "text": title,
                "task_id": t.get("id") or last_tid, "data": data,
                "updated_at": max((m.get("updated_at") or 0) for m in ms)}

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
            acts = (m.get("data") or {}).get("actions") or []
            if any(not a.get("resolved") and not a.get("stale") and not self._action_pending(a) for a in acts):
                acts = [{**a, "stale": True} if not a.get("resolved") and not a.get("stale") and not self._action_pending(a)
                        else a for a in acts]
                m = self.update(m["id"], data={"actions": acts}, push=False) or m
        elif m["kind"] in ("ask", "confirm") and not d.get("resolved") and not d.get("stale") and not d.get("watch_id"):
            live = self.sessions.live.get(d.get("session_id") or "")
            pending = live and (live.question_info if m["kind"] == "ask" else live.confirm_info)
            if not pending:
                m = self.update(m["id"], data={"stale": True}, push=False) or m
        return m

    def _action_pending(self, a: dict) -> bool:
        live = self.sessions.live.get(a.get("session_id") or "")
        if not live:
            return False
        return bool(live.confirm_info if a.get("kind") == "confirm" else live.question_info)

    # ------------------------------------------------------------------ 3.8.8: the task card
    def bump(self, mid: str, push: bool = True) -> Optional[dict]:
        """Move a message to the bottom of the chat (a continued task's card): its seq becomes the newest."""
        top = self.store._x("SELECT MAX(seq) AS s FROM chat_messages")
        m = self.get(mid)
        if not m:
            return None
        if top and top[0]["s"] and int(top[0]["s"]) > int(m["seq"]):
            self.store._x("UPDATE chat_messages SET seq=?, updated_at=? WHERE id=?", (int(top[0]["s"]) + 1, time.time(), mid))
            m = self.get(mid)
        if push:
            self._push("update", m)
        return m

    @staticmethod
    def open_actions(card: dict) -> list[dict]:
        return [a for a in (card.get("data") or {}).get("actions") or [] if not a.get("resolved") and not a.get("stale")]

    def add_action(self, card_msg: dict, action: dict, **card_data) -> dict:
        """Put something the user must answer on the card; an older open action of the same session is stale."""
        acts = list((card_msg.get("data") or {}).get("actions") or [])
        sid = action.get("session_id")
        same = next((a for a in acts if not a.get("resolved") and not a.get("stale") and a.get("session_id") == sid
                     and a.get("kind") == action.get("kind") and a.get("question") == action.get("question")
                     and a.get("confirm_id") == action.get("confirm_id")), None)
        if same:   # the same request seen twice (the catch-up at card creation and the event itself)
            return self.update(card_msg["id"], data=card_data) if card_data else card_msg
        acts = [{**a, "stale": True} if not a.get("resolved") and not a.get("stale") and a.get("session_id") == sid
                and a.get("kind") == action.get("kind") else a for a in acts]
        action = {"id": f"act-{uuid.uuid4().hex[:10]}", "at": time.time(), **action}
        acts.append(action)
        done = [a for a in acts if a.get("resolved") or a.get("stale")]
        if len(done) > MAX_ACTIONS:
            drop = {id(a) for a in done[:len(done) - MAX_ACTIONS]}
            acts = [a for a in acts if id(a) not in drop]
        return self.update(card_msg["id"], data={"actions": acts, **card_data}) or card_msg

    def resolve_actions(self, card: dict, kind: str, ans: Any, by: Optional[str] = None, cancelled: bool = False,
                        action_id: Optional[str] = None) -> Optional[dict]:
        acts = list((card.get("data") or {}).get("actions") or [])
        hit = False
        for i, a in enumerate(acts):
            if a.get("resolved") or a.get("stale") or a.get("kind") != kind or (action_id and a.get("id") != action_id):
                continue
            txt = ("已允许" if ans else "已拒绝") if kind == "confirm" else answer_text(ans, a.get("fields"), cancelled)
            acts[i] = {**a, "resolved": {"answer": ans if not isinstance(ans, str) else ans[:200], "text": txt,
                                         "by": by, "at": time.time()}}
            hit = True
        return self.update(card["id"], data={"actions": acts}) if hit else None

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
            t = f"［已交给电脑的任务「{d.get('title') or m.get('text')}」，状态：{STATUS_CN.get(d.get('status'), d.get('status'))}］"
            res = d.get("result") or {}
            if res.get("text"):
                names = "、".join(f["name"] for f in res.get("files") or [])
                t += "\n" + res["text"] + (f"（文件：{names}）" if names else "")
            for a in Chat.open_actions(m)[-1:]:
                t += f"\n［任务在{'请用户确认' if a.get('kind') == 'confirm' else '问用户'}：{str(a.get('question') or '')[:200]}］"
            return t
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
    async def classify(self, text: str, cands: Optional[list] = None) -> dict:
        recent = "\n".join(f"{'用户' if r['role'] == 'user' else '助理'}：{(self.render(r) or '')[:160]}"
                           for r in self.window(3)[:-1])
        user = (f"最近的聊天：\n{recent}\n\n" if recent else "") + f"最新消息：{text[:1500]}"
        system = ROUTE_PROMPT
        if cands:   # 3.8.5: the classifier may also say "this continues task n"
            system += ROUTE_TASKS.format(tasks="\n".join(
                f"{i + 1}: {STATUS_CN.get(c['run']['status'], c['run']['status'])} · {c['title']}" for i, c in enumerate(cands[:5])))
        try:
            out = await asyncio.wait_for(self.llm("small").complete(system, user, 200), 30)
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
            cands = self.recent_tasks() if mode == "auto" and not SKILL_RE.match(text) else []
            if cands:   # 3.8.5: a follow-up about an earlier task goes to THAT task, never to a new one
                fu = await self.followup(text, attachments, user, cands)
                if fu is not None:
                    await say("route", {"route": fu["route"]})
                    for m in fu["messages"]:
                        await say("task" if m["kind"] == "task" else "message", m)
                    self.background(self.after_exchange(user, fu["messages"]))
                    return {"user": user, **fu}
            if mode in ("chat", "task"):
                route = {"route": mode, "title": ""}
            elif SKILL_RE.match(text):   # a skill picked with "/" (or the + menu) runs on the computer, like the old skills menu
                route = {"route": "task", "title": "", "skill": True}
            else:
                route = await self.classify(text, [c for c in cands if c["run"]["status"] not in ("succeeded",)] or cands)
            if route.get("route") == "continue":
                pool = [c for c in cands if c["run"]["status"] not in ("succeeded",)] or cands
                n = int(route.get("task") or 0)
                if 1 <= n <= len(pool):
                    msgs = await self.continue_card(pool[n - 1], text, attachments, user)
                else:
                    msgs = [self.pick_task_ask(pool, text, attachments)]
                await say("route", {"route": "continue"})
                for m in msgs:
                    await say("task" if m["kind"] == "task" else "message", m)
                self.background(self.after_exchange(user, msgs))
                return {"route": "continue", "user": user, "messages": msgs}
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

    # ------------------------------------------------------------------ 3.8.5: follow-ups go to their task
    def recent_tasks(self, hours: float = 24.0, limit: int = 6) -> list[dict]:
        """Tasks the chat started recently, newest first, one per task (its latest run): {card, run, root, title}."""
        from . import continuation as _cont
        out, seen = [], set()
        now = time.time()
        for r in self.store._x("SELECT * FROM chat_messages WHERE conv=? AND kind='task' ORDER BY seq DESC LIMIT 30", (CONV,)):
            m = _row(r)
            tid = m.get("task_id") or (m["data"] or {}).get("task_id")
            if not tid or not self.store.task(tid):
                continue
            root = _cont.root_id(self.store, tid)
            if root in seen:
                continue
            seen.add(root)
            run = _cont.latest(self.store, root) or self.store.task(tid)
            if now - float(run.get("updated_at") or run.get("created_at") or 0) > hours * 3600:
                continue
            if run.get("status") == "cancelled":
                continue
            out.append({"card": m, "run": run, "root": root, "title": (m["data"] or {}).get("title") or m.get("text") or ""})
            if len(out) >= limit:
                break
        return out

    def _open_ask(self) -> Optional[dict]:
        """The newest question of a task that is still waiting for the user in this chat: an open ask action on a task
        card (3.8.8; returned as the card with data.action) or an old-style ask message."""
        for r in self.store._x("SELECT * FROM chat_messages WHERE conv=? AND kind IN ('ask','task') ORDER BY seq DESC LIMIT 20",
                               (CONV,)):
            m = _row(r)
            d = m["data"] or {}
            if m["kind"] == "task":
                for a in reversed(self.open_actions(m)):
                    if a.get("kind") == "ask" and a.get("ask_kind") != "form":
                        live = self.sessions.live.get(a.get("session_id") or "")
                        if live and live.question_info:
                            return {**m, "action": a}
                continue
            if d.get("resolved") or d.get("stale") or d.get("watch_id") or d.get("pick_task") or d.get("ask_kind") == "form":
                continue
            live = self.sessions.live.get(d.get("session_id") or "")
            if live and live.question_info:
                return m
        return None

    async def followup(self, text: str, attachments: list, user: dict, cands: list) -> Optional[dict]:
        """A message that answers a waiting task or continues an earlier one. None = route it as usual."""
        from . import continuation as _cont
        ask = self._open_ask()
        opts = [str(o) for o in ((ask or {}).get("action") or (ask or {}).get("data") or {}).get("options") or []]
        answers = bool(ask) and (bool(attachments) or _cont.is_followup(text) or any(o and (o in text or text in o) for o in opts)
                                 or (len(text) <= 40 and not re.search(r"[？?]\s*$", text)))
        if ask and answers and len(text) <= 300 and not _cont.NEW_TASK_RE.search(text) and (text or attachments):
            # the task asked the user something: a plain reply in the chat is the answer (task-7bd waited 600 s
            # on its question card while the user wrote in the chat, then went on alone)
            d = ask.get("action") or ask["data"]
            ans = text or "已上传附件"
            if attachments:
                ans += "\n附件：" + "、".join(str(a.get("name") or "") for a in attachments)
                self.sessions._link_attachments(attachments, d.get("task_id"), d.get("thread_id"))
                live = self.sessions.live.get(d.get("session_id") or "")
                if live:   # the files go to the computer the task runs on, now
                    with contextlib.suppress(Exception):
                        from .uploads import sync_files
                        await sync_files(self.sessions.computer(live.task.machine_id), [
                            {"local": a["artifact"], "rel": f"uploads/{a.get('name')}"} for a in attachments if a.get("artifact")])
            try:
                ok = self.sessions.answer_question(d.get("session_id") or "", ans)
            except KeyError:
                ok = False
            if ok:
                if ask.get("action"):
                    m = self.resolve_actions(ask, "ask", ans, "chat_text", action_id=ask["action"]["id"]) or self.get(ask["id"])
                else:
                    m = self.update(ask["id"], data={"resolved": {"answer": ans[:200], "text": answer_text(ans),
                                                                  "by": "chat_text", "at": time.time()}})
                return {"route": "answer", "messages": [m]}
        if not _cont.is_followup(text, bool(attachments)):
            return None
        open_ = [c for c in cands if c["run"]["status"] not in ("succeeded",)]
        if len(open_) == 1:
            target = open_[0]
        elif not open_:
            fresh = [c for c in cands if time.time() - float(c["run"].get("updated_at") or 0) < 3 * 3600]
            if not fresh:
                return None
            target = fresh[0]
        else:
            # two or more unfinished tasks: a word from one title decides, else ask (never guess)
            hits = [c for c in open_ if any(w and w in text for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", c["title"]))]
            if len(hits) == 1:
                target = hits[0]
            else:
                return {"route": "continue", "messages": [self.pick_task_ask(open_, text, attachments)]}
        return {"route": "continue", "messages": await self.continue_card(target, text, attachments, user)}

    def pick_task_ask(self, cands: list, text: str, attachments: list) -> dict:
        opts = [f"{c['title'][:24]}（{STATUS_CN.get(c['run']['status'], c['run']['status'])}）" for c in cands[:4]] + ["这是新任务"]
        return self.add("assistant", "ask", "你说的是哪个任务？", {
            "pick_task": True, "ask_kind": "question", "question": "你说的是哪个任务？", "options": opts,
            "task_ids": [c["root"] for c in cands[:4]], "pending": {"text": text[:2000], "attachments": attachments[:10]}})

    async def continue_card(self, c: dict, text: str, attachments: list, user: Optional[dict]) -> list[dict]:
        """Continue the task of card c with the user's words; its progress shows on a fresh card at the bottom."""
        run = c["run"]
        st = run.get("status")
        try:
            res = await self.sessions.continue_task(c["root"], text, attachments)
        except Exception as e:  # noqa: BLE001  never silent
            return [self.add("assistant", "text", f"没能接着做这个任务：{type(e).__name__}: {str(e)[:160]}", {"error": True})]
        title = c["title"] or _short_title(run.get("prompt") or "")
        if res.get("instructed") or res.get("answered"):
            return [self.add("assistant", "text", ("已把你的回答交给正在做的任务「" if res.get("answered") else
                                                    "已把补充要求交给正在做的任务「") + title + "」", {"task_id": run["id"]})]
        t = res["task"]
        if res.get("queued") and t["id"] == run["id"]:
            return [self.add("assistant", "text", f"「{title}」还在排队，你的补充已经加进去了", {"task_id": t["id"]})]
        st2 = t.get("status") or ("queued" if res.get("queued") else "running")
        card = c["card"]
        if card.get("kind") != "task" or not self.get(card["id"]):
            card = self.card_for(t["id"], res.get("thread_id") or t.get("thread_id")) or card
        if card.get("kind") == "task" and self.get(card["id"]):
            # 3.8.8: the follow-up attaches to the SAME card, re-armed and moved to the bottom
            cur = self.get(card["id"])
            fresh_run = (cur["data"] or {}).get("task_id") != t["id"]
            data = {"task_id": t["id"], "thread_id": res.get("thread_id") or t.get("thread_id"),
                    "session_id": t.get("session_id") or (cur["data"] or {}).get("session_id"),
                    "machine_id": t.get("machine_id"), "title": title, "status": st2,
                    "status_line": "接着做 · " + STATUS_CN.get(st2, st2), "continues": c["root"],
                    "continued_by": t["id"], "root_id": c["root"], "card": CARD_V}
            if fresh_run:
                data.update(step=0, result=None, ended_at=None, started_at=time.time(),
                            runs=int((cur["data"] or {}).get("runs") or 1) + 1, followup=one_line(text, 60) or None)
            self.update(card["id"], data=data, task_id=t["id"], push=False)
            return [self.bump(card["id"])]
        card = self.add("assistant", "task", title, {
            "task_id": t["id"], "thread_id": res.get("thread_id") or t.get("thread_id"), "session_id": t.get("session_id"),
            "machine_id": t.get("machine_id"), "machine_name": self._machine_label(t.get("machine_id")),
            "title": title, "status": st2, "status_line": "接着做 · " + STATUS_CN.get(st2, st2),
            "step": 0, "continues": c["root"], "root_id": c["root"], "card": CARD_V, "started_at": time.time(),
            "origin": (user or {}).get("id")}, task_id=t["id"])
        return [card]

    # ------------------------------------------------------------------ tasks from the chat
    def _machine_label(self, mid: Optional[str]) -> str:
        """The computer a task card runs on, with its role (aiw-roles-1), for web and iOS alike."""
        try:
            from . import roles as _roles
            return _roles.label(self.sessions, mid)
        except Exception:  # noqa: BLE001
            return mid or ""

    async def start_task(self, prompt: str, title: str = "", machine_id: Optional[str] = None,
                         attachments: Optional[list] = None, origin: Optional[str] = None, source: str = "chat") -> dict:
        prompt = (prompt or "").strip()
        if source == "chat":   # aiw-roles-1: a new task goes to the computer whose role fits it
            try:
                from . import roles as _roles
                machine_id = _roles.pick(self.sessions, prompt, machine_id) or machine_id
            except Exception as e:  # noqa: BLE001
                log.warning("roles: %s", e)
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
        self.sessions._link_attachments(attachments, t["id"], res.get("thread_id") or t.get("thread_id"))
        st = t.get("status") or ("queued" if res.get("queued") else "running")
        card = self.add("assistant", "task", title, {
            "task_id": t["id"], "thread_id": res.get("thread_id") or t.get("thread_id"), "session_id": t.get("session_id"),
            "machine_id": t.get("machine_id"), "machine_name": self._machine_label(t.get("machine_id")),
            "title": title, "status": st, "status_line": STATUS_CN.get(st, st),
            "step": 0, "max_steps": t.get("max_steps"), "origin": origin, "card": CARD_V, "root_id": t.get("continues") or t["id"],
            "prompt": one_line(SKILL_RE.sub("", prompt), 120), "started_at": time.time() if st != "queued" else None,
            "actions": []}, task_id=t["id"])
        return self._catch_up_sync(card)

    def _catch_up_sync(self, card: dict) -> dict:
        """Events of a task that starts at once can fire before its card exists (submit returns after the run began):
        a question / confirmation already waiting is put on the card now, so nothing that needs the user is lost."""
        tid = card.get("task_id")
        live = self.sessions.live_for_task(tid) if tid and self.sessions else None
        if not live:
            return card
        task = live.task
        sid = live.id
        if live.confirm_info and not any(a.get("kind") == "confirm" for a in (card["data"] or {}).get("actions") or []):
            self.background(self.on_task_event(task, sid, {"type": "agent.confirm_required", "data": live.confirm_info}))
        elif live.question_info and not any(a.get("kind") == "ask" for a in (card["data"] or {}).get("actions") or []):
            qi = dict(live.question_info)
            stuck = getattr(getattr(live, "loop", None), "_stuck", None)
            if stuck and not qi.get("form"):
                qi["stuck"] = stuck
            self.background(self.on_task_event(task, sid, {"type": "agent.input_required", "data": qi}))
        return card

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
        if not r:   # 3.8.8: the card is keyed by the task (its first run): a later run of it finds the same card
            from . import continuation as _cont
            root = _cont.root_id(self.store, task_id)
            if root != task_id:
                r = self.store._x("SELECT * FROM chat_messages WHERE kind='task' AND (task_id=? OR data LIKE ?) "
                                  "ORDER BY seq DESC LIMIT 1", (root, f'%"root_id": "{root}"%'))
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
                "title": cd.get("title") or card.get("text"), "card": CARD_V,
                "root_id": cd.get("root_id") or getattr(task, "continues", None) or task.id}
        if typ == "task.cancelled":   # 3.8.2: taken out of the queue (删除): the card says so instead of 排队中 forever
            self.update(card["id"], data={**base, "status": "cancelled", "status_line": "已从排队中删除",
                                          "queue": None, "ended_at": time.time()}, task_id=task.id)
            return
        if typ == "task.started":
            new_run = bool(cd.get("task_id")) and cd.get("task_id") != task.id
            data = {**base, "status": "running", "status_line": "进行中", "step": 0, "max_steps": task.max_steps,
                    "ended_at": None}
            if new_run or not cd.get("started_at") or cd.get("result"):
                data.update(started_at=time.time(), result=None, progress=None)
            if new_run:   # a retry / continue from the task view: the same card follows it, at the bottom
                data["runs"] = int(cd.get("runs") or 1) + 1
                self.update(card["id"], data=data, task_id=task.id, push=False)
                self.bump(card["id"])
            else:
                self.update(card["id"], data=data, task_id=task.id)
        elif typ == "agent.decision":
            step = msg.get("step") or cd.get("step") or 0
            status = cd.get("status") if cd.get("status") in WAITING else "running"
            prog = one_line(str(d.get("thought") or ""), 60) or cd.get("progress")
            if step != cd.get("step") or status != cd.get("status") or prog != cd.get("progress"):
                self.update(card["id"], data={**base, "step": step, "status": status, "progress": prog,
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
            # old-style question messages (before 3.8.8) still get their ✓
            for r in self.store._x("SELECT * FROM chat_messages WHERE kind=? AND task_id=? ORDER BY seq DESC LIMIT 3",
                                   (kind, task.id)):
                m = _row(r)
                if not m["data"].get("resolved"):
                    txt = ("已允许" if ans else "已拒绝") if kind == "confirm" else \
                        answer_text(ans, m["data"].get("fields"), bool(d.get("cancelled")))
                    self.update(m["id"], data={"resolved": {"answer": ans if not isinstance(ans, str) else ans[:200],
                                                            "text": txt, "by": d.get("by"), "at": time.time()}})
            card = self.resolve_actions(self.get(card["id"]) or card, kind, ans, d.get("by"), bool(d.get("cancelled"))) or card
            self.update(card["id"], data={**base, "status": "running", "status_line": self._line(cd, "running"),
                                          "stuck": None})
        elif typ == "agent.confirm_required":
            s = d.get("summary") or {}
            q = s.get("title") or d.get("reason") or "需要你确认"
            self.add_action(card, {
                "kind": "confirm", "ask_kind": "confirm", "question": q, "session_id": sid, "task_id": task.id,
                "summary": {k: s.get(k) for k in ("title", "what", "risk", "risk_cn", "why", "chips", "category")},
                "confirm_id": d.get("id"), "task_allow": d.get("task_allow")},
                **base, status="waiting_confirm", status_line="等你确认")
        elif typ == "task.end":
            st = msg.get("status") or "failed"
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
            thumb = next((f["path"] for f in files if f["kind"] == "image"), None)
            acts = [{**a, "stale": True} if not a.get("resolved") and not a.get("stale") else a
                    for a in (self.get(card["id"]) or card)["data"].get("actions") or []]
            # 3.8.8: the result is ON the card (no separate 球球 message); the push notification is sessions' own
            self.update(card["id"], data={**base, "status": st, "status_line": STATUS_CN.get(st, st), "ended_at": time.time(),
                                          "actions": acts, "stuck": None,
                                          "result": {"text": text, "status": st, "files": files, "thumb": thumb,
                                                     "has_more": len(res.strip()) > len(line) + 2, "at": time.time()}},
                        task_id=task.id)

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
        data = {"kind": "ask", "task_id": task.id, "session_id": sid, "ask_kind": kind, "question": text, "options": opts}
        if kind == "takeover":
            data["takeover"] = {k: form.get(k) for k in ("site", "url", "steps", "reason", "machine", "machine_id", "action_cn")}
        if kind == "form":
            data["form_title"] = form.get("title")
            # labels only (the answer line shows 标签：值); which fields are secret, never their values
            data["fields"] = [{"key": f.get("key"), "label": f.get("label"), "secret": bool(f.get("secret"))}
                              for f in form.get("fields") or [] if isinstance(f, dict)][:20]
        stuck = d.get("stuck") if isinstance(d.get("stuck"), dict) else None
        if stuck:   # 3.8.2: a stuck task is paused, not ended; the card says why and what the user can do
            data["stuck"] = stuck
        line = "需要你接管" if kind == "takeover" else (stuck or {}).get("line") or "等你回答"
        # 3.8.8: on the card itself, not a new message (the push notification comes from sessions._notify)
        self.add_action(self.get(card["id"]) or card, data, status="waiting_input", status_line=line, stuck=stuck,
                        thread_id=card["data"].get("thread_id") or task.thread_id, session_id=sid)

    async def answer(self, mid: str, choice: Optional[str] = None, approve: Optional[bool] = None,
                     text: Optional[str] = None, action_id: Optional[str] = None) -> dict:
        m = self.get(mid)
        if m and m["kind"] == "task":
            return await self.answer_action(m, action_id, choice, approve, text)
        if not m or m["kind"] not in ("ask", "confirm"):
            raise KeyError(mid)
        d = m["data"]
        if d.get("resolved"):
            raise ValueError("这个问题已经回答过了")
        if d.get("watch_id"):           # a price watch's question (立即重试 / 暂停), not a task's
            from . import shopwatch
            return await shopwatch.answer_ask(self, m, (choice if choice is not None else text) or OPT_RETRY_WATCH)
        if d.get("pick_task"):          # 3.8.5: which task a follow-up was for
            pick = (choice if choice is not None else text) or ""
            opts = d.get("options") or []
            ids = d.get("task_ids") or []
            pend = d.get("pending") or {}
            m = self.update(mid, data={"resolved": {"answer": pick[:200], "text": pick[:200], "by": "chat", "at": time.time()}})
            if pick in opts and opts.index(pick) < len(ids):
                root = ids[opts.index(pick)]
                c = next((x for x in self.recent_tasks(hours=24 * 7, limit=20) if x["root"] == root), None)
                if c is None:
                    from . import continuation as _cont
                    run = _cont.latest(self.store, root) or self.store.task(root) or {}
                    c = {"card": m, "run": run, "root": root, "title": pick.split("（")[0]}
                msgs = await self.continue_card(c, pend.get("text") or "", pend.get("attachments") or [], None)
            else:
                msgs = [await self.start_task(pend.get("text") or pick, "", None, pend.get("attachments") or [], origin=mid)]
            return {"ok": True, "message": m, "messages": msgs}
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
        txt = ("已允许" if ans else "已拒绝") if isinstance(ans, bool) else answer_text(ans, d.get("fields"))
        m = self.update(mid, data={"resolved": {"answer": ans, "text": txt, "by": "chat", "at": time.time()}})
        return {"ok": True, "message": m, "open_task": isinstance(ans, str) and "接管" in ans}


    async def answer_action(self, card: dict, action_id: Optional[str], choice: Optional[str] = None,
                            approve: Optional[bool] = None, text: Optional[str] = None) -> dict:
        """3.8.8: answer what a task card asks (its data.actions entry). No action_id = its newest open action."""
        acts = (card.get("data") or {}).get("actions") or []
        a = next((x for x in acts if x.get("id") == action_id), None) if action_id else \
            next(iter(reversed(self.open_actions(card))), None)
        if not a:
            raise KeyError(action_id or card["id"])
        if a.get("resolved"):
            raise ValueError("这个问题已经回答过了")
        sid = a.get("session_id") or ""
        kind = a.get("kind") or "ask"
        try:
            if kind == "confirm":
                ans: Any = bool(approve)
                ok = self.sessions.answer_confirm(sid, ans)
            else:
                ans = (choice if choice is not None else text) or ""
                if not ans.strip():
                    raise ValueError("回答是空的")
                ok = self.sessions.answer_question(sid, ans)
        except KeyError:
            ok = False
        if not ok:
            self.update(card["id"], data={"actions": [{**x, "stale": True} if x.get("id") == a["id"] else x
                                                      for x in (self.get(card["id"]) or card)["data"].get("actions") or []]})
            raise ValueError("这个问题已经过期（任务已经不在等这个回答了）")
        m = self.resolve_actions(self.get(card["id"]) or card, kind, ans, "chat", action_id=a["id"]) or self.get(card["id"])
        return {"ok": True, "message": m, "action_id": a["id"], "open_task": isinstance(ans, str) and "接管" in ans}


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
    action_id: Optional[str] = None     # 3.8.8: which action of a task card
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
            return await chat.answer(mid, body.choice, body.approve, body.text, body.action_id)
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.delete("/api/chat/messages/{mid}", dependencies=dep)
    async def chat_delete(mid: str):
        try:
            return chat.delete_message(mid)
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
