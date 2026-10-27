"""小红书运营: likes, comments on posts by people with similar interests, replies to comments on the user's own
posts, and publishing, done by the agent in the logged-in Chrome of an AIW computer.

Rules this module enforces server-side (the model cannot talk its way past them)
  * Daily caps per action (likes 30, comments on others 8, replies 20, posts 1 by default), counted per local day
    (AIW_TZ, default Asia/Shanghai) in table xhs_actions.
  * Approval: every post and every comment on someone else's post is a draft (table xhs_drafts) that the user
    approves, edits or skips, through the L3 confirmation card + push (xhs_review) or the queue in
    设置 › 小红书运营. Replies to comments on the user's own posts need approval too unless auto_reply is on.
    Likes need no approval but are capped.
  * The gate (guard) wraps the task's ToolExecutor: no typing, form filling, Enter, 发送/发布 click or shell-driven
    browser input unless a draft the user approved is armed with xhs_begin, and then only that draft's text.
  * Warnings: a captcha, verification, rate-limit or account warning on the page (detected by xhs_probe.py through
    Chrome's DevTools port, or reported by the agent with xhs_warning) stops all activity, notifies the user and pauses
    everything for 24 hours. Captchas are never solved automatically.
  * Scheduling: tick() (called by the scheduler) plans a few sessions a day at random times inside the active window,
    never on a fixed schedule, and starts each as an agent task (source "xhs") with its own random length.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("aiw.xhs")

SOURCE = "xhs"
SKILL_ID = "xhs"
KINDS = ("like", "comment", "reply", "post")
KIND_CN = {"like": "点赞", "comment": "评论别人的笔记", "reply": "回复我笔记下的评论", "post": "发布笔记"}
DEFAULT_CAPS = {"like": 30, "comment": 8, "reply": 20, "post": 1}
HARD_CAPS = {"like": 100, "comment": 30, "reply": 60, "post": 3}     # the settings page cannot go above these
PAUSE_S = 24 * 3600
REVIEW_TIMEOUT_S = 1800
TOKEN_TTL_S = 120
PROBE_SRC = Path(__file__).with_name("xhs_probe.py")
REMOTE = "$HOME/.config/aiw/xhs/probe.py"

DEFAULTS: dict = {
    "enabled": False, "machine_id": None, "persona": "", "keywords": [], "tone": "真诚、口语化、简短，像普通用户聊天",
    "caps": dict(DEFAULT_CAPS), "window": {"start": "09:30", "end": "22:30"},
    "actions": {"like": True, "comment": True, "reply": True, "post": True}, "auto_reply": False,
}

LINK_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(com|cn|net|top|xyz|cc|io|me)\b|xhslink", re.I)
AD_RE = re.compile(r"加\s*(我|v|V|微|薇|威)|vx|v信|wx|微信|薇信|qq|QQ群|私信我|私我|私聊|滴滴我|主页有|看我主页|进群|拉群|代购|返现|优惠券|领券|"
                   r"低价|包邮|下单|链接|二维码|扫码|淘宝|拼多多|闲鱼|抖音号|互关|互粉|互赞|回关|关注我", re.I)
WARN_KINDS = ("captcha", "verify", "rate_limit", "account_warning")
WARN_CN = {"captcha": "验证码/滑块", "verify": "安全验证", "rate_limit": "操作频繁限制", "account_warning": "账号警告/限制",
           "other": "异常提示"}
# url / document title / dialog + toast text -> what the page shows. Body text is not used for the softer words:
# a post about 违规停车 must not pause the account.
CAPTCHA_URL = re.compile(r"captcha|/website-login/verify|/verify|security-check|sec\.xiaohongshu|/risk", re.I)
CAPTCHA_TXT = re.compile(r"滑块|拖动.{0,6}(滑块|完成|拼图)|请完成(安全)?验证|安全验证|人机验证|验证码|向右滑动|点击.{0,6}验证|captcha", re.I)
VERIFY_TXT = re.compile(r"身份验证|验证身份|手机号验证|短信验证|二次验证|请先验证")
RATE_TXT = re.compile(r"操作(过于)?频繁|访问(过于)?频繁|请求(过于)?频繁|太频繁|稍后再试|休息一下|请求太快|操作太快|评论(过于)?频繁|"
                      r"今日.{0,6}(已达|上限)|次数(已达)?上限|too many|rate limit", re.I)
ACCOUNT_TXT = re.compile(r"账号(异常|存在(安全)?风险|已被(封|限|禁)|违规|被限制|功能受限)|封禁|封号|禁言|限流|违反.{0,6}社区(规范|公约)|"
                         r"社区规范|违规(内容|行为|提醒)|功能(已被)?限制|暂时无法(评论|点赞|发布)|账号.{0,4}冻结|申诉")
BODY_STRONG = re.compile(r"请通过(安全)?验证|请完成(安全)?验证|账号已被(封禁|限制)|操作过于频繁")
LOGIN_URL = re.compile(r"/login|website-login(?!/(captcha|verify))|passport", re.I)
LOGIN_TXT = re.compile(r"扫码登录|登录后(推荐|查看|即可)|手机号登录|小红书.{0,4}APP.{0,4}扫码|使用小红书.{0,6}扫码|请先登录|立即登录")

# what the gate looks for
SEND_RE = re.compile(r"^\s*(发送|发布|发表|提交|评论|回复|send|post|publish)\s*$", re.I)
SEND_LOOSE = re.compile(r"发送|发布笔记|发表|发布$|^发布|提交")
LIKE_RE = re.compile(r"^\s*(赞|点赞|喜欢|like)\s*$|点赞|like-wrapper|爱心", re.I)
ENTER_KEYS = re.compile(r"(^|\+)(enter|return|kp_enter)$", re.I)
SHELL_INPUT_RE = re.compile(r"xdotool|ydotool|9222|devtools|dispatchKeyEvent|dispatchMouseEvent|Input\.(insert|dispatch)|"
                            r"playwright|puppeteer|selenium|pyautogui|chrome-remote|wtype|xte\b", re.I)
BOS_SHELL_RE = re.compile(r"\b(9110|9210)\b|/mcp(?![\w.-])", re.I)   # BrowserOS's DevTools / MCP: around the gates
READ_TOOLS = {"screenshot", "scroll", "move", "files_list", "files_read", "read_image", "update_plan", "ask_user",
              "request_takeover", "remember", "done", "fail", "browser_back", "browser_forward", "send_file", "files_write"}


# ----------------------------------------------------------------------------- tables
def ensure_tables(store) -> None:
    store._x("CREATE TABLE IF NOT EXISTS xhs_drafts (id TEXT PRIMARY KEY, kind TEXT, status TEXT, target_url TEXT, "
             "target_title TEXT, target_excerpt TEXT, target_author TEXT, title TEXT, text TEXT, original_text TEXT, "
             "media TEXT, task_id TEXT, auto INTEGER DEFAULT 0, source TEXT, note TEXT, created_at REAL, decided_at REAL, "
             "sent_at REAL, sent_url TEXT)")
    store._x("CREATE TABLE IF NOT EXISTS xhs_actions (id TEXT PRIMARY KEY, day TEXT, kind TEXT, draft_id TEXT, "
             "target_url TEXT, task_id TEXT, created_at REAL)")
    store._x("CREATE INDEX IF NOT EXISTS ix_xhs_actions_day ON xhs_actions(day, kind)")
    store._x("CREATE INDEX IF NOT EXISTS ix_xhs_drafts_status ON xhs_drafts(status, created_at)")


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("AIW_TZ", "Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return None


def local(ts: Optional[float] = None) -> datetime:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), _tz())


def day_key(ts: Optional[float] = None) -> str:
    return local(ts).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------- settings
def _hm(s: str, dflt: str) -> str:
    m = re.match(r"^\s*(\d{1,2})[:：](\d{2})\s*$", str(s or ""))
    if not m or int(m.group(1)) > 24 or int(m.group(2)) > 59:
        return dflt
    return f"{min(int(m.group(1)), 23 if int(m.group(2)) else 24):02d}:{m.group(2)}"


def settings(store) -> dict:
    raw = store.kv_get("xhs_settings", {}) or {}
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    return s


def save_settings(store, body: dict) -> dict:
    s = settings(store)
    for k in ("enabled", "auto_reply"):
        if k in body:
            s[k] = bool(body[k])
    for k in ("persona", "tone"):
        if k in body:
            s[k] = str(body[k] or "").strip()[:600]
    if "machine_id" in body:
        s["machine_id"] = body["machine_id"] or None
    if "keywords" in body:
        kw = body["keywords"]
        if isinstance(kw, str):
            kw = re.split(r"[,，、;；\n]+", kw)
        s["keywords"] = [k.strip()[:20] for k in kw if str(k).strip()][:30]
    if isinstance(body.get("caps"), dict):
        for k in KINDS:
            if k in body["caps"]:
                try:
                    s["caps"][k] = max(0, min(HARD_CAPS[k], int(body["caps"][k])))
                except (TypeError, ValueError):
                    pass
    if isinstance(body.get("actions"), dict):
        for k in KINDS:
            if k in body["actions"]:
                s["actions"][k] = bool(body["actions"][k])
    if isinstance(body.get("window"), dict):
        s["window"] = {"start": _hm(body["window"].get("start"), s["window"]["start"]),
                       "end": _hm(body["window"].get("end"), s["window"]["end"])}
    store.kv_set("xhs_settings", s)
    store.kv_set("xhs_plan", {})      # re-plan today's sessions with the new window/caps
    return s


def default_machine(store) -> Optional[str]:
    """Prefer the local VM (home IP) over a cloud box (datacenter IP, more likely to be flagged)."""
    ms = store.machines()
    for m in ms:
        if (m.get("provider") or "") != "instabox":
            return m["id"]
    return ms[0]["id"] if ms else None


def machine_of(store) -> Optional[str]:
    return settings(store).get("machine_id") or default_machine(store)


# ----------------------------------------------------------------------------- counts and caps
def counts(store, day: Optional[str] = None) -> dict:
    ensure_tables(store)
    rows = store._x("SELECT kind, COUNT(*) AS n FROM xhs_actions WHERE day=? GROUP BY kind", (day or day_key(),))
    out = {k: 0 for k in KINDS}
    for r in rows:
        out[r["kind"]] = int(r["n"])
    return out


def reserved(store, kind: str) -> int:
    """Drafts that will use today's cap if sent: pending + approved, not yet sent, made today."""
    ensure_tables(store)
    start = local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    r = store._x("SELECT COUNT(*) AS n FROM xhs_drafts WHERE kind=? AND status IN ('pending','approved') AND created_at>=?",
                 (kind, start))
    return int(r[0]["n"]) if r else 0


def remaining(store, kind: str, include_reserved: bool = False) -> int:
    cap = int(settings(store)["caps"].get(kind, 0))
    used = counts(store).get(kind, 0) + (reserved(store, kind) if include_reserved else 0)
    return max(0, cap - used)


def record(store, kind: str, draft_id: Optional[str] = None, target_url: str = "", task_id: Optional[str] = None,
           now: Optional[float] = None) -> dict:
    ensure_tables(store)
    return store.insert("xhs_actions", {"day": day_key(now), "kind": kind, "draft_id": draft_id,
                                        "target_url": (target_url or "")[:500], "task_id": task_id,
                                        "created_at": now or time.time()})


def can_act(store, kind: str, for_draft: bool = False) -> Optional[str]:
    """None when allowed, else the reason (in Chinese, shown to the model and the user)."""
    s = settings(store)
    p = pause_state(store)
    if p:
        return f"小红书运营已暂停到 {local(p['until']).strftime('%m-%d %H:%M')}（{p.get('reason', '')}），现在不能做任何操作"
    if not s["actions"].get(kind, False):
        return f"设置里关掉了「{KIND_CN[kind]}」"
    if remaining(store, kind, include_reserved=for_draft) <= 0:
        return f"今天的「{KIND_CN[kind]}」已到上限 {s['caps'].get(kind, 0)}"
    return None


# ----------------------------------------------------------------------------- pause
def pause_state(store, now: Optional[float] = None) -> Optional[dict]:
    p = store.kv_get("xhs_pause") or None
    if not p:
        return None
    if float(p.get("until") or 0) <= (now or time.time()):
        return None
    return p


async def trigger_pause(store, notifier, kind: str, detail: str = "", url: str = "", hours: float = 24,
                        sessions=None, task_id: Optional[str] = None, now: Optional[float] = None) -> dict:
    now = now or time.time()
    until = now + hours * 3600
    p = {"until": until, "kind": kind, "reason": WARN_CN.get(kind, kind), "detail": (detail or "")[:300],
         "url": url, "at": now, "task_id": task_id}
    store.kv_set("xhs_pause", p)
    if notifier:
        try:
            await notifier.notify("xhs_warning", f"小红书运营已暂停 {hours:g} 小时：{p['reason']}",
                                  (f"页面出现了{p['reason']}，所有自动操作已停止，{local(until).strftime('%m-%d %H:%M')} 前不会再动。"
                                   f"不会自动处理验证码。需要的话请在接管桌面里自己处理。{(' 详情：' + detail[:200]) if detail else ''}"),
                                  task_id=task_id, link=url or None)
        except Exception as e:  # noqa: BLE001
            log.warning("xhs notify failed: %s", e)
    if sessions is not None:
        await stop_live(sessions, except_task=task_id)
    return p


def resume(store) -> None:
    store.kv_set("xhs_pause", None)
    store.kv_set("xhs_plan", {})


async def stop_live(sessions, except_task: Optional[str] = None) -> int:
    n = 0
    for s in list(getattr(sessions, "live", {}).values()):
        t = s.task
        if t.id == except_task:
            continue
        if (t.source or "") == SOURCE and s.runner and not s.runner.done():
            s.control.stop()
            n += 1
    return n


# ----------------------------------------------------------------------------- page state
def classify(snap: dict) -> str:
    """xhs_probe snapshot -> ok | login | captcha | verify | rate_limit | account_warning | no_browser | no_tab."""
    if not snap or snap.get("error"):
        return "no_browser"
    if not snap.get("url"):
        return "no_tab"
    url, title = snap.get("url") or "", snap.get("title") or ""
    alerts = " ".join(snap.get("alerts") or [])
    head = f"{title} {alerts}"
    body = snap.get("text") or ""
    if CAPTCHA_URL.search(url) or CAPTCHA_TXT.search(head) or snap.get("captcha_frame"):
        return "captcha"
    if RATE_TXT.search(head):
        return "rate_limit"
    if ACCOUNT_TXT.search(head):
        return "account_warning"
    if VERIFY_TXT.search(head):
        return "verify"
    if BODY_STRONG.search(body):
        return "rate_limit" if "频繁" in BODY_STRONG.search(body).group(0) else "captcha"
    if LOGIN_URL.search(url) or LOGIN_TXT.search(head) or snap.get("qr_login"):
        return "login"
    return "ok"


def probe_command(sub: str = "state") -> str:
    src = PROBE_SRC.read_bytes()
    b64 = base64.b64encode(src).decode()
    digest = hashlib.sha1(src).hexdigest()
    return (f'mkdir -p "$HOME/.config/aiw/xhs" && f="{REMOTE}" && '
            f'if [ "$(sha1sum "$f" 2>/dev/null | cut -d" " -f1)" != "{digest}" ]; then '
            f"echo '{b64}' | base64 -d > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"; fi && python3 \"$f\" {sub}")


async def probe(computer, timeout: float = 20) -> dict:
    try:
        from . import browseros as _bos   # BrowserOS on: its DevTools port, where the user logged in
        r = await computer.execute("shell.exec", {"command": _bos.probe_env(computer) + probe_command("state"),
                                                  "timeout": timeout}, timeout=timeout + 10)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    res = getattr(r, "result", None) or {}
    for line in reversed((res.get("stdout") or "").strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"error": str(res.get("stderr") or getattr(r, "error", None) or "no output")[-300:]}


async def check_page(store, notifier, computer, sessions=None, task_id: Optional[str] = None) -> tuple[str, dict]:
    """Look at the xiaohongshu tab. A warning pauses everything; a login wall notifies (once a day)."""
    snap = await probe(computer)
    st = classify(snap)
    if st in WARN_KINDS:
        await trigger_pause(store, notifier, st, " ".join(snap.get("alerts") or [])[:200] or snap.get("title", ""),
                            snap.get("url", ""), sessions=sessions, task_id=task_id)
    elif st == "login":
        await notify_login(store, notifier, task_id)
    return st, snap


def _bname(store, mid) -> str:
    from . import browseros as _bos
    return _bos.browser_name(store, mid)


async def notify_login(store, notifier, task_id: Optional[str] = None) -> None:
    key = f"xhs_login_notified:{day_key()}"
    if store.kv_get(key) or not notifier:
        return
    store.kv_set(key, True)
    mid = machine_of(store)
    m = store.machine(mid) if mid else None
    await notifier.notify("xhs_login", "小红书需要登录一次",
                          f"「{(m or {}).get('name') or mid}」上的 {_bname(store, mid)} 还没登录小红书。打开 Aiw → 电脑 → 接管，在 {_bname(store, mid)} 里用小红书 App "
                          "扫一次二维码即可（登录状态会保存在这台电脑的浏览器里）。", task_id=task_id)


# ----------------------------------------------------------------------------- drafts
def _norm(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", (t or "").lower())


def check_text(store, kind: str, text: str, title: str = "") -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return "草稿是空的"
    if LINK_RE.search(t) or LINK_RE.search(title or ""):
        return "草稿里不能有链接或网址"
    if AD_RE.search(t) or AD_RE.search(title or ""):
        return "草稿像广告或引流（微信/私信/互关/购买链接之类），不允许"
    lim = {"comment": 200, "reply": 200, "post": 1000}[kind]
    if len(t) > lim:
        return f"太长了（{len(t)} 字，上限 {lim}）"
    if kind == "post" and len(title or "") > 20:
        return "笔记标题最多 20 个字"
    n = _norm(t)
    if kind != "post" and len(n) < 4:
        return "评论太短，像机器人；根据笔记内容写一句具体的话"
    ensure_tables(store)
    for r in store._x("SELECT text FROM xhs_drafts WHERE kind=? ORDER BY created_at DESC LIMIT 300", (kind,)):
        o = _norm(r["text"])
        if o and (o == n or difflib.SequenceMatcher(None, o, n).ratio() > 0.8):
            return "和以前写过的一条几乎一样；每条都要根据这篇笔记的内容重新写，不要套模板"
    return None


def draft_view(d: dict) -> dict:
    d = dict(d)
    try:
        d["media"] = json.loads(d.get("media") or "[]")
    except ValueError:
        d["media"] = []
    return d


def create_draft(store, kind: str, text: str, target_url: str = "", target_title: str = "", target_excerpt: str = "",
                 target_author: str = "", title: str = "", media: Optional[list] = None, task_id: Optional[str] = None,
                 source: str = "agent", approved: bool = False) -> dict:
    """-> {"draft": row} or {"error": reason}. Replies are auto-approved when auto_reply is on."""
    if kind not in ("comment", "reply", "post"):
        return {"error": f"unknown kind {kind}"}
    why = can_act(store, kind, for_draft=True)
    if why:
        return {"error": why}
    why = check_text(store, kind, text, title)
    if why:
        return {"error": why}
    if kind != "post" and not (target_url or "").strip():
        return {"error": "评论/回复要给出目标笔记的链接 target_url"}
    auto = kind == "reply" and settings(store).get("auto_reply")
    status = "approved" if (auto or approved) else "pending"
    row = store.insert("xhs_drafts", {
        "kind": kind, "status": status, "target_url": (target_url or "")[:500], "target_title": (target_title or "")[:120],
        "target_excerpt": (target_excerpt or "")[:300], "target_author": (target_author or "")[:60], "title": (title or "")[:40],
        "text": text.strip(), "original_text": text.strip(), "media": json.dumps(media or [], ensure_ascii=False),
        "task_id": task_id, "auto": 1 if auto else 0, "source": source,
        "decided_at": time.time() if status == "approved" else None})
    return {"draft": draft_view(row)}


def decide(store, did: str, action: str, text: Optional[str] = None, title: Optional[str] = None) -> dict:
    ensure_tables(store)
    d = store.get("xhs_drafts", did)
    if not d:
        return {"error": "not found"}
    if d["status"] not in ("pending", "approved"):
        return {"error": f"这条草稿已经是 {d['status']}"}
    fields: dict[str, Any] = {}
    if text is not None and text.strip() and text.strip() != d["text"]:
        why = check_text(store, d["kind"], text, title if title is not None else d.get("title") or "")
        if why and "几乎一样" not in why:
            return {"error": why}
        fields["text"] = text.strip()
    if title is not None and d["kind"] == "post":
        fields["title"] = title.strip()[:40]
    if action == "approve":
        fields.update(status="approved", decided_at=time.time())
    elif action == "skip":
        fields.update(status="skipped", decided_at=time.time())
    elif action != "edit":
        return {"error": f"unknown action {action}"}
    store.update("xhs_drafts", did, **fields)
    _wake(d.get("task_id"))
    if action == "approve":
        add_soon_slot(store)
    return {"draft": draft_view(store.get("xhs_drafts", did))}


def drafts(store, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    ensure_tables(store)
    if status:
        sts = tuple(status.split(","))
        rows = store.all("xhs_drafts", f"status IN ({','.join('?' * len(sts))})", sts, limit=limit)
    else:
        rows = store.all("xhs_drafts", limit=limit)
    return [draft_view(r) for r in rows]


# a review call waits on these; the queue page wakes it when the user decides drafts one by one
_waiters: dict[str, asyncio.Event] = {}


def _wake(task_id: Optional[str]) -> None:
    ev = _waiters.get(task_id or "")
    if ev:
        ev.set()


# ----------------------------------------------------------------------------- scheduling
def _window(s: dict, day: datetime) -> tuple[float, float]:
    def at(hm: str) -> float:
        h, m = (int(x) for x in hm.split(":"))
        base = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return (base + timedelta(hours=h, minutes=m)).timestamp()
    a, b = at(s["window"]["start"]), at(s["window"]["end"])
    if b <= a:
        b += 86400
    return a, b


def in_window(s: dict, now: Optional[float] = None) -> bool:
    now = now or time.time()
    d = local(now)
    for off in (0, -1):
        a, b = _window(s, d + timedelta(days=off))
        if a <= now < b:
            return True
    return False


def plan_day(s: dict, now: float, rng: Optional[random.Random] = None) -> list[float]:
    """Random session start times for the rest of today's window: 2-6 sessions depending on the caps, at least
    35 minutes apart, each jittered, so nothing runs on a fixed clock."""
    rng = rng or random.Random()
    a, b = _window(s, local(now))
    if now >= b:
        a, b = _window(s, local(now) + timedelta(days=1))
    start = max(a, now + rng.uniform(120, 900))
    if start >= b - 600:
        return []
    caps = s.get("caps") or {}
    load = sum(int(caps.get(k, 0)) for k in KINDS if (s.get("actions") or {}).get(k))
    if load <= 0:
        return []
    n = 2 + min(4, load // 15) + rng.choice((0, 0, 1))
    span = b - 600 - start
    n = max(1, min(n, int(span // (35 * 60)) + 1))
    times: list[float] = []
    for _ in range(n * 20):
        if len(times) >= n:
            break
        t = start + rng.random() * span
        if all(abs(t - x) >= 35 * 60 for x in times):
            times.append(t)
    return sorted(times)


def add_soon_slot(store, now: Optional[float] = None) -> None:
    """Approved drafts are sent at the next session; if none is due within the hour, add one 5-25 min from now."""
    now = now or time.time()
    s = settings(store)
    if not s.get("enabled") or pause_state(store, now):
        return
    t = now + random.uniform(300, 1500)
    if not in_window(s, t):
        return
    plan = store.kv_get("xhs_plan") or {}
    slots = [x for x in plan.get("slots") or [] if x > now]
    if any(x - now < 3600 for x in slots):
        return
    plan.setdefault("day", day_key(now))
    plan["slots"] = sorted(slots + [t])
    store.kv_set("xhs_plan", plan)


def session_prompt(store, rng: Optional[random.Random] = None, slots_left: int = 1) -> tuple[str, int, int]:
    """-> (prompt, max_steps, timeout_s) for one session with a random length and a random share of today's work."""
    rng = rng or random.Random()
    s = settings(store)
    left = max(1, slots_left)
    targets = {}
    for k in ("like", "comment", "reply"):
        r = remaining(store, k, include_reserved=k != "like") if s["actions"].get(k) else 0
        targets[k] = 0 if r <= 0 else max(1, min(r, round(r / left * rng.uniform(0.6, 1.5))))
    approved = drafts(store, "approved", 50)
    minutes = rng.choice((12, 15, 20, 25, 30, 40))
    steps = int(minutes * 3.2)
    lines = [f"【技能：{SKILL_ID}】小红书运营例行会话（这次大约 {minutes} 分钟，到时就收尾，不要拖长）。",
             "先调用 xhs_status 看设置、今日剩余额度、暂停状态和已批准待发送的草稿。"]
    if approved:
        lines.append(f"先处理 {len(approved)} 条已批准的草稿：对每条调用 xhs_begin，按返回的内容发出，然后 xhs_sent。")
    goals = []
    if targets["reply"]:
        goals.append(f"看我笔记下的新评论，最多起草 {targets['reply']} 条回复")
    if targets["like"]:
        goals.append(f"浏览兴趣相近的笔记，真正读过后最多点赞 {targets['like']} 篇")
    if targets["comment"]:
        goals.append(f"挑最多 {targets['comment']} 篇真正有话可说的笔记起草评论")
    rng.shuffle(goals)
    if goals:
        lines.append("这次的目标（不必做满，宁少勿滥，顺序随意）：" + "；".join(goals) + "。")
    lines.append("所有评论/回复草稿最后一起用 xhs_review 送审，审批通过的再发出。遇到验证码、验证、频繁提示或账号警告立即调用 xhs_warning 并结束。")
    return "\n".join(lines), steps, minutes * 60 + 300


def live_xhs_task(sessions) -> bool:
    for s in list(getattr(sessions, "live", {}).values()):
        if (s.task.source or "") == SOURCE and s.runner and not s.runner.done():
            return True
    st = sessions.store
    return bool(st._x("SELECT id FROM tasks WHERE source=? AND status='queued' LIMIT 1", (SOURCE,)))


async def start_session(store, sessions, reason: str = "", slots_left: int = 1, title: str = "") -> dict:
    mid = machine_of(store)
    if not mid:
        raise RuntimeError("没有可用的电脑")
    prompt, steps, secs = session_prompt(store, slots_left=slots_left)
    return await sessions.submit(prompt, mid, source=SOURCE, max_steps=steps, timeout_s=secs,
                                 title=title or "📕 小红书运营")


async def tick(sched, now: Optional[float] = None) -> Optional[dict]:
    """Called on every scheduler tick. Starts at most one session when one is due."""
    store, sessions = sched.store, sched.sessions
    now = now or time.time()
    s = settings(store)
    if not s.get("enabled") or pause_state(store, now):
        return None
    plan = store.kv_get("xhs_plan") or {}
    if plan.get("day") != day_key(now):
        plan = {"day": day_key(now), "slots": plan_day(s, now)}
        store.kv_set("xhs_plan", plan)
    slots = sorted(plan.get("slots") or [])
    due = [x for x in slots if x <= now]
    if not due:
        return None
    if not in_window(s, now):
        plan["slots"] = [x for x in slots if x > now]
        store.kv_set("xhs_plan", plan)
        return None
    mid = machine_of(store)
    if live_xhs_task(sessions) or (mid and sessions.busy_session(mid)):
        # the computer is busy: try again a little later, still inside the window
        plan["slots"] = [x for x in slots if x > now] + ([now + random.uniform(600, 1800)] if in_window(s, now + 1800) else [])
        store.kv_set("xhs_plan", plan)
        return None
    plan["slots"] = [x for x in slots if x > now]
    plan["last"] = now
    store.kv_set("xhs_plan", plan)
    try:
        return await start_session(store, sessions, slots_left=len(plan["slots"]) + 1)
    except Exception as e:  # noqa: BLE001
        log.warning("xhs session not started: %s", e)
        return None


# ----------------------------------------------------------------------------- agent tools
def is_xhs_task(task) -> bool:
    if (getattr(task, "source", "") or "") == SOURCE:
        return True
    try:
        from agent_runtime import skills as _sk
        names = [n.lower() for n in _sk.picked(task.prompt) + _sk.auto(task.prompt)]
        return SKILL_ID in names or "小红书运营" in names
    except Exception:  # noqa: BLE001
        return False


def task_state(task_id: str) -> dict:
    return _STATE.setdefault(task_id, {"armed": None, "like_tokens": [], "typed": False, "stopped": False})


_STATE: dict[str, dict] = {}


def _schema(name: str, desc: str, props: dict, req: list) -> dict:
    return {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": req}}


def task_tools(store, notifier, task, computer, confirm, sessions=None) -> dict:
    """Extra agent tools for a 小红书运营 task."""
    ensure_tables(store)
    st = task_state(task.id)
    tools: dict = {}

    def tool(name, desc, props, req, max_result=1600):
        def deco(fn):
            fn.needs_confirm = lambda a: False   # the tools gate themselves; xhs_review raises its own card
            fn.max_result = max_result
            tools[name] = (_schema(name, desc, props, req), fn)
            return fn
        return deco

    async def page_ok() -> Optional[str]:
        stt, snap = await check_page(store, notifier, computer, sessions, task.id)
        if stt in WARN_KINDS:
            st["stopped"] = True
            return (f"STOP: 页面出现{WARN_CN[stt]}。已暂停所有小红书操作 24 小时并通知用户。不要尝试处理验证码，"
                    "不要再点任何东西，立即调用 done 说明情况。")
        if stt == "login":
            return (f"NEED_LOGIN: 这台电脑的 {_bname(store, getattr(computer, 'machine_id', None))} 没登录小红书。已通知用户。调用 request_takeover，请用户在接管桌面里用小红书 App "
                    "扫二维码登录（只需一次），用户完成后再继续。")
        return None

    @tool("xhs_status", "小红书运营: settings (persona, keywords, tone, which actions are on, auto_reply), today's counts and "
          "remaining caps, pause state, drafts approved and waiting to be sent, and whether the browser tab shows a login wall "
          "or a warning. Call it first in every session.", {}, [], max_result=3000)
    async def xhs_status(a: dict) -> str:
        s = settings(store)
        stt, snap = await check_page(store, notifier, computer, sessions, task.id)
        out = {"persona": s["persona"], "keywords": s["keywords"], "tone": s["tone"], "actions": s["actions"],
               "auto_reply": s["auto_reply"], "caps": s["caps"], "done_today": counts(store),
               "remaining": {k: remaining(store, k, include_reserved=k != "like") for k in KINDS},
               "paused": pause_state(store), "page": stt, "page_url": snap.get("url"),
               "approved_to_send": [{"id": d["id"], "kind": d["kind"], "target_url": d["target_url"],
                                     "target_title": d["target_title"]} for d in drafts(store, "approved", 30)],
               "pending_review": len(drafts(store, "pending", 100))}
        if stt in WARN_KINDS:
            st["stopped"] = True
            out["STOP"] = "页面出现警告/验证，已暂停 24 小时并通知用户，立即 done。"
        if stt == "login":
            out["NEED_LOGIN"] = "调用 request_takeover 请用户扫码登录一次。"
        return json.dumps(out, ensure_ascii=False)

    @tool("xhs_like", "Ask permission to like ONE post you have actually opened and read. Counts toward today's like cap. "
          "Only when it returns OK, click that post's like (heart) button once, then keep reading for a while.",
          {"post_url": {"type": "string"}, "post_title": {"type": "string"}}, ["post_url"])
    async def xhs_like(a: dict) -> str:
        why = can_act(store, "like")
        if why:
            return f"REFUSED: {why}。不要点赞。"
        bad = await page_ok()
        if bad:
            return bad
        url = str(a.get("post_url") or "")
        if store._x("SELECT id FROM xhs_actions WHERE kind='like' AND target_url=? LIMIT 1", (url[:500],)):
            return "REFUSED: 这篇笔记已经点过赞了。"
        record(store, "like", target_url=url, task_id=task.id)
        st["like_tokens"].append(time.time())
        return f"OK: 可以点赞这一篇（今日剩余 {remaining(store, 'like')} 个）。点一次爱心，然后继续正常浏览。"

    @tool("xhs_draft", "Save a draft for the user to approve. kind=comment (on someone else's post), reply (to a comment on "
          "the user's own post) or post (a new note). Write it fresh from THIS post's actual content and the persona/tone, "
          "like a real person: specific, short, no links, no ads, no emoji spam, never a template or a repeat. "
          "target_excerpt = 1-2 sentences you read in the post (or the comment you reply to). Nothing is sent by this tool.",
          {"kind": {"type": "string", "enum": ["comment", "reply", "post"]}, "text": {"type": "string"},
           "target_url": {"type": "string"}, "target_title": {"type": "string"}, "target_excerpt": {"type": "string"},
           "target_author": {"type": "string"}, "title": {"type": "string", "description": "post title, ≤20 chars"},
           "media": {"type": "array", "items": {"type": "string"}, "description": "post images/video paths on this computer"}},
          ["kind", "text"])
    async def xhs_draft(a: dict) -> str:
        r = create_draft(store, str(a.get("kind") or ""), str(a.get("text") or ""), a.get("target_url") or "",
                         a.get("target_title") or "", a.get("target_excerpt") or "", a.get("target_author") or "",
                         a.get("title") or "", [str(m) for m in (a.get("media") or [])], task_id=task.id)
        if r.get("error"):
            return f"REFUSED: {r['error']}"
        d = r["draft"]
        if d["status"] == "approved":
            return f"SAVED {d['id']} (auto-approved reply: call xhs_begin with this id to send it)"
        return f"SAVED {d['id']} (pending; call xhs_review when you have drafted this session's batch)"

    @tool("xhs_review", "Send this session's pending drafts to the user for approval (L3 card + push; the user can edit, "
          "approve or skip each one, or approve all). Waits up to 30 min. Returns which drafts were approved (with the final "
          "text, maybe edited) and which were skipped. Drafts not decided in time stay in the user's queue.",
          {"ids": {"type": "array", "items": {"type": "string"}}}, [], max_result=4000)
    async def xhs_review(a: dict) -> str:
        ids = [str(i) for i in (a.get("ids") or [])]
        mine = [d for d in drafts(store, "pending", 200) if d["task_id"] == task.id and (not ids or d["id"] in ids)]
        if not mine:
            return "没有待审核的草稿。"
        payload = {"kind": "xhs_review", "confirm_timeout_s": REVIEW_TIMEOUT_S, "count": len(mine),
                   "drafts": [{"id": d["id"], "kind": d["kind"], "kind_cn": KIND_CN[d["kind"]], "target_title": d["target_title"],
                               "target_url": d["target_url"], "target_excerpt": d["target_excerpt"], "title": d["title"],
                               "text": d["text"]} for d in mine]}
        reason = f"小红书：{len(mine)} 条草稿等你审核（可逐条编辑/跳过，允许 = 全部按原文通过）"
        ev = _waiters.setdefault(task.id, asyncio.Event())
        ids_set = {d["id"] for d in mine}

        def undecided() -> list:
            return [d for d in drafts(store, "pending", 200) if d["id"] in ids_set]

        card = asyncio.ensure_future(confirm("xhs_review", payload, _decision(reason)))
        try:
            while True:
                ev.clear()
                waiter = asyncio.ensure_future(ev.wait())
                done, _ = await asyncio.wait({card, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if card in done:
                    waiter.cancel()
                    ok = bool(card.result())
                    left = undecided()
                    if ok:
                        for d in left:
                            decide(store, d["id"], "approve")
                    elif left and _card_answered(sessions, task.id):
                        for d in left:
                            decide(store, d["id"], "skip")
                    break
                if not undecided():       # every draft was decided in the queue page
                    card.cancel()
                    break
        finally:
            _waiters.pop(task.id, None)
        rows = [store.get("xhs_drafts", i) for i in ids_set]
        ok_rows = [r for r in rows if r and r["status"] == "approved"]
        skip_rows = [r for r in rows if r and r["status"] == "skipped"]
        wait_rows = [r for r in rows if r and r["status"] == "pending"]
        return json.dumps({"approved": [{"id": r["id"], "kind": r["kind"], "target_url": r["target_url"],
                                         "edited": r["text"] != r["original_text"]} for r in ok_rows],
                           "skipped": [r["id"] for r in skip_rows], "still_pending": [r["id"] for r in wait_rows],
                           "next": "对每条 approved 调用 xhs_begin，按返回的原文发出，再 xhs_sent。skipped 的不要发。"},
                          ensure_ascii=False)

    @tool("xhs_begin", "Arm ONE approved draft for sending. Returns the exact text (and title/media for a post) to type. "
          "Only while a draft is armed can you type into the comment box / editor and click 发送/发布, and only that text. "
          "After sending call xhs_sent (or xhs_failed).", {"id": {"type": "string"}}, ["id"])
    async def xhs_begin(a: dict) -> str:
        d = store.get("xhs_drafts", str(a.get("id") or ""))
        if not d:
            return "REFUSED: 没有这条草稿。"
        if d["status"] != "approved":
            return f"REFUSED: 这条草稿是 {d['status']}，只有用户批准的才能发。"
        why = can_act(store, d["kind"])
        if why:
            return f"REFUSED: {why}"
        bad = await page_ok()
        if bad:
            return bad
        st["armed"] = draft_view(d)
        out = {"id": d["id"], "kind": d["kind"], "text": d["text"]}
        if d["kind"] == "post":
            out.update(title=d["title"], media=draft_view(d)["media"],
                       how="打开 https://creator.xiaohongshu.com/publish/publish ，上传图文（或视频），用文件对话框选上面的 media 文件"
                           "（ctrl+L 输入路径），填标题和正文，检查预览后点发布。")
        else:
            out.update(target_url=d["target_url"],
                       how=("打开 target_url，先滚动读一会儿，点评论框" + ("里那条评论的「回复」" if d["kind"] == "reply" else "") +
                            "，用 type 输入上面的 text（原样，一字不改），停顿几秒再点发送。"))
        return json.dumps(out, ensure_ascii=False)

    @tool("xhs_sent", "Report that the armed draft was sent (counts toward today's cap). Give the post/comment URL if shown.",
          {"id": {"type": "string"}, "url": {"type": "string"}}, ["id"])
    async def xhs_sent(a: dict) -> str:
        arm = st.get("armed")
        did = str(a.get("id") or "")
        if not arm or arm["id"] != did:
            return "REFUSED: 这条草稿没有用 xhs_begin 打开。"
        store.update("xhs_drafts", did, status="sent", sent_at=time.time(), sent_url=str(a.get("url") or "")[:500])
        record(store, arm["kind"], draft_id=did, target_url=arm.get("target_url") or "", task_id=task.id)
        st["armed"] = None
        st["typed"] = False
        bad = await page_ok()
        return bad or f"OK: 已记录（今日剩余 {KIND_CN[arm['kind']]} {remaining(store, arm['kind'])}）。停一会儿、继续浏览，再做下一条。"

    @tool("xhs_failed", "The armed draft could not be sent (post deleted, comments closed, …). It is not counted.",
          {"id": {"type": "string"}, "reason": {"type": "string"}}, ["id"])
    async def xhs_failed(a: dict) -> str:
        did = str(a.get("id") or "")
        if st.get("armed") and st["armed"]["id"] == did:
            st["armed"] = None
            st["typed"] = False
        if store.get("xhs_drafts", did):
            store.update("xhs_drafts", did, status="failed", note=str(a.get("reason") or "")[:300])
        return "OK"

    @tool("xhs_warning", "Call IMMEDIATELY when the page shows a captcha/slider, a verification, a 'too frequent' message or any "
          "account warning/restriction. Stops all 小红书 activity for 24 h and notifies the user. Never try to solve it.",
          {"kind": {"type": "string", "enum": ["captcha", "verify", "rate_limit", "account_warning", "other"]},
           "detail": {"type": "string"}}, ["kind"])
    async def xhs_warning(a: dict) -> str:
        k = str(a.get("kind") or "other")
        await trigger_pause(store, notifier, k if k in WARN_CN else "other", str(a.get("detail") or ""),
                            sessions=sessions, task_id=task.id)
        st["stopped"] = True
        st["armed"] = None
        return "STOPPED: 已暂停 24 小时并通知用户。不要再操作页面，立即调用 done。"

    return tools


def _decision(reason: str):
    from tool_router import Decision, RiskLevel
    return Decision(RiskLevel.L3, reason)


def _card_answered(sessions, task_id: str) -> bool:
    """True when the confirm came back from the user (reject) rather than a timeout. The session clears its future
    either way, so the flag is set by answer hooks when available; default to 'answered' only if the task's status
    shows it was not a timeout. We cannot tell for sure, so a False keeps drafts pending (the safe side)."""
    return bool(_REJECTED.pop(task_id, False))


_REJECTED: dict[str, bool] = {}


def note_confirm_answer(sessions, session_id: str, approve: bool) -> None:
    """Called by the confirm route: remembers an explicit reject so xhs_review skips the batch (a timeout keeps it)."""
    live = getattr(sessions, "live", {}).get(session_id)
    if live and not approve and (live.confirm_info or {}).get("tool") == "xhs_review":
        _REJECTED[live.task.id] = True


# ----------------------------------------------------------------------------- the gate
def gate(store, state: dict, name: str, args: dict) -> Optional[str]:
    """Why this tool call is refused in a 小红书 task, or None."""
    args = args or {}
    if state.get("stopped") or pause_state(store):
        if name in READ_TOOLS or name.startswith("xhs_"):
            return None
        return "小红书运营已因页面警告暂停：不要再操作页面，调用 done。"
    armed = state.get("armed")
    allowed = ""
    if armed:
        allowed = (armed.get("title") or "") + "\n" + (armed.get("text") or "") + "\n" + "\n".join(armed.get("media") or [])
    if name == "shell":
        cmd = str(args.get("command") or "")
        if (SHELL_INPUT_RE.search(cmd) and "aiw/xhs/probe.py" not in cmd) or BOS_SHELL_RE.search(cmd):
            return "小红书任务里不能用 shell 去驱动浏览器或模拟输入；用 browser_* / type 工具，发送内容要先经用户批准（xhs_begin）。"
        return None
    if name == "type":
        text = str(args.get("text") or "")
        if _is_path(text):
            return None
        if armed and text.strip() and text.strip() in allowed:
            state["typed"] = True
            return None
        if not armed:
            return "没有已批准并用 xhs_begin 打开的草稿时不能输入文字（评论/回复/笔记必须先经用户批准）。搜索请用 browser_navigate 打开搜索结果链接。"
        return "只能原样输入 xhs_begin 返回的已批准内容，不能改写或另写。"
    if name == "browser_fill":
        text = str(args.get("text") or "")
        if _is_path(text):
            return None
        if armed and text.strip() and text.strip() in allowed:
            state["typed"] = True
            return None
        return "browser_fill 只能填入 xhs_begin 返回的已批准内容。搜索请用 browser_navigate 打开 https://www.xiaohongshu.com/search_result?keyword=…"
    if name == "keypress":
        keys = str(args.get("keys") or args.get("key") or "")
        if any(ENTER_KEYS.search(k.strip()) for k in re.split(r"[\s,]+", keys) if k.strip()) and not armed:
            return "没有已批准的草稿时不能按回车（可能直接发出评论）。"
        if re.search(r"ctrl\+v|shift\+insert", keys, re.I) and not armed:
            return "没有已批准的草稿时不能粘贴文字。"
        return None
    if name == "browser_click":
        text = str(args.get("text") or "")
        if SEND_RE.search(text) or (SEND_LOOSE.search(text) and len(text) <= 8):
            if not armed:
                return "发送/发布按钮只能在用户批准的草稿（xhs_begin）输入后点击。"
            if not state.get("typed"):
                return "先原样输入已批准的内容，再点发送/发布。"
            return None
        if LIKE_RE.search(text):
            return _like_token(state)
        return None
    if name == "click_element":
        target = str(args.get("target") or "")
        if re.search(r"发送|发布|发表|提交|send|post\b|publish", target, re.I) and not (armed and state.get("typed")):
            return "发送/发布按钮只能在用户批准的草稿（xhs_begin）输入后点击。"
        if re.search(r"点赞|爱心|赞|like|heart", target, re.I):
            return _like_token(state)
        return None
    return None


# a like button in an accessibility snapshot is often 'button "赞 128"' or an unnamed heart icon with a count
LIKE_LOOSE = re.compile(r"^\s*(赞|点赞|喜欢|like|liked|已赞)\b|^\s*(赞|点赞|喜欢)\s*\d|点赞|爱心|heart|like-wrapper", re.I)


def _media_ok(files: list, media: list) -> Optional[str]:
    """Every file to upload must be one of the approved draft's media (same path, or same file name when one side
    is written with ~ or a different home)."""
    import os.path as op
    allowed = [str(m).strip() for m in media or [] if str(m).strip()]
    for f in files or []:
        f = str(f).strip()
        hit = any(f == m or op.expanduser(f) == op.expanduser(m)
                  or (op.basename(f) == op.basename(m) and (f.endswith(m.lstrip("~")) or m.endswith(f.lstrip("~"))
                                                            or "/" not in f or "/" not in m))
                  for m in allowed)
        if not hit:
            return f"只能上传已批准草稿里的文件（{', '.join(allowed) or '没有'}），{f} 不在其中。"
    return None


async def gate_bos(executor, store, state: dict, name: str, args: dict) -> tuple[Optional[str], dict]:
    """gate() for a BrowserOS (bos_*) call: refs resolved to element text, then judged as the classic call."""
    from tool_router import browseros as bos
    a = dict(args or {})
    if state.get("stopped") or pause_state(store):
        if name in bos.READ_ONLY or (name == "bos_tabs" and a.get("action") in ("list", "active", "close")):
            return None, a
        return "小红书运营已因页面警告暂停：不要再操作页面，调用 done。", a
    armed = state.get("armed")
    if name == "bos_upload":
        if not armed:
            return "没有已批准并用 xhs_begin 打开的草稿时不能上传文件。", a
        why = _media_ok(a.get("files") or ([a["file"]] if a.get("file") else []), armed.get("media") or [])
        if why:
            return why, a
        a = await bos.annotate(executor, name, a)
        return None, a
    why, a = await bos.gate_bos(executor, name, a, lambda n, ca: _async(gate(store, state, n, ca)), bool(armed))
    if why:
        return why, a
    if name == "bos_act" and str(a.get("kind") or "") in bos.CLICK_KINDS:
        label = (a.get("_labels") or {}).get(str(a.get("ref") or ""), "")
        if LIKE_LOOSE.search(label) and not LIKE_RE.search(label):   # gate() already took a token for strict matches
            return _like_token(state), a
    return None, a


async def _async(v):
    return v


def bos_page_step(name: str, args: dict) -> bool:
    """bos calls after which the page may show a warning: navigation, opening a page, clicks, scrolling."""
    a = args or {}
    if name == "bos_navigate":
        return True
    if name == "bos_tabs":
        return a.get("action") == "new"
    if name == "bos_act":
        return str(a.get("kind") or "") in ("click", "check", "uncheck", "select", "press", "scroll", "dialog_accept",
                                            "dialog_dismiss")
    return False


def _is_path(text: str) -> bool:
    t = text.strip()
    return bool(re.match(r"^(~|/home/|/tmp/|/root/)[^\n]{0,300}$", t)) and "\n" not in t


def _like_token(state: dict) -> Optional[str]:
    now = time.time()
    toks = [t for t in state.get("like_tokens") or [] if now - t < TOKEN_TTL_S]
    if not toks:
        state["like_tokens"] = []
        return "点赞前先调用 xhs_like（计入每日上限），返回 OK 后再点这一篇的爱心。"
    toks.pop(0)
    state["like_tokens"] = toks
    return None


def guard(executor, store, state: dict, notifier=None, computer=None, sessions=None, task_id: Optional[str] = None) -> None:
    """Wrap a task's ToolExecutor.run with gate(); after page-changing steps, look for warnings on the page."""
    from tool_router import Decision, RiskLevel
    from tool_router.executor import ToolOutcome
    orig = executor.run
    counter = {"n": 0}

    async def run(name: str, args: dict, intent: str = ""):
        page_step = name in ("browser_click", "browser_navigate", "browser_open", "click", "click_element", "scroll")
        if name.startswith("bos_"):   # BrowserOS: judged as the classic click/typing it amounts to
            why, args = await gate_bos(executor, store, state, name, args)
            page_step = bos_page_step(name, args)
        else:
            why = gate(store, state, name, args)
        if why:
            return ToolOutcome(name, None, False, Decision(RiskLevel.L4, why), error=f"forbidden by policy: {why}")
        out = await orig(name, args, intent)
        if page_step and computer is not None:
            counter["n"] += 1
            if name != "scroll" or counter["n"] % 4 == 0:
                try:
                    stt, _snap = await check_page(store, notifier, computer, sessions, task_id)
                    if stt in WARN_KINDS:
                        state["stopped"] = True
                        state["armed"] = None
                        out.success = False
                        out.error = (out.error or "") + f" | STOP: 页面出现{WARN_CN[stt]}，已暂停所有小红书操作 24 小时并通知用户。立即调用 done。"
                except Exception as e:  # noqa: BLE001
                    log.debug("xhs page check failed: %s", e)
        return out

    executor.run = run


# ----------------------------------------------------------------------------- routes
def overview(store) -> dict:
    ensure_tables(store)
    s = settings(store)
    plan = store.kv_get("xhs_plan") or {}
    now = time.time()
    ms = store.machines()
    return {"settings": s, "machine_id": machine_of(store), "default_machine": default_machine(store),
            "machines": [{"id": m["id"], "name": m.get("name") or m["id"], "cloud": (m.get("provider") or "") == "instabox"} for m in ms],
            "counts": counts(store), "caps": s["caps"], "pause": pause_state(store),
            "next_runs": [x for x in sorted(plan.get("slots") or []) if x > now][:6], "in_window": in_window(s),
            "pending": drafts(store, "pending", 100), "approved": drafts(store, "approved", 50),
            "recent": [d for d in drafts(store, None, 40) if d["status"] in ("sent", "skipped", "failed")][:20]}


def mount(app, store, sessions, notifier, auth) -> None:
    from fastapi import Depends, HTTPException
    ensure_tables(store)
    dep = [Depends(auth)]
    _orig_answer = sessions.answer_confirm

    def answer_confirm(sid: str, approve: bool, *rest) -> bool:   # an explicit 拒绝 on the review card skips the batch
        try:
            note_confirm_answer(sessions, sid, approve)
        except Exception:  # noqa: BLE001
            pass
        return _orig_answer(sid, approve, *rest)   # rest: scope ("task" = 本任务内都允许)
    sessions.answer_confirm = answer_confirm

    @app.get("/api/xhs", dependencies=dep)
    async def xhs_get():
        return overview(store)

    @app.put("/api/xhs/settings", dependencies=dep)
    async def xhs_put(body: dict):
        return save_settings(store, body or {})

    @app.get("/api/xhs/drafts", dependencies=dep)
    async def xhs_drafts(status: Optional[str] = None):
        return drafts(store, status)

    @app.post("/api/xhs/drafts/batch", dependencies=dep)
    async def xhs_batch(body: dict):
        out = []
        for did in (body or {}).get("ids") or []:
            out.append(decide(store, str(did), str(body.get("action") or "approve")))
        return {"results": out}

    @app.post("/api/xhs/drafts/{did}", dependencies=dep)
    async def xhs_decide(did: str, body: dict):
        r = decide(store, did, str((body or {}).get("action") or "edit"), body.get("text"), body.get("title"))
        if r.get("error"):
            raise HTTPException(400, r["error"])
        return r["draft"]

    @app.post("/api/xhs/post", dependencies=dep)
    async def xhs_post(body: dict):
        """The user's own note (text + media from uploads) -> an approved post draft, sent at the next session;
        or {"brief": "..."} -> a task now that drafts it for review."""
        body = body or {}
        if body.get("brief") and not body.get("text"):
            mid = machine_of(store)
            if not mid:
                raise HTTPException(400, "没有可用的电脑")
            media = [str(m) for m in body.get("media") or []]
            prompt = (f"【技能：{SKILL_ID}】按用户的要求起草一篇小红书笔记（不要发布）：{str(body['brief'])[:1500]}\n"
                      + (f"可用的图片/视频：{', '.join(media)}\n" if media else "")
                      + "先 xhs_status，写好标题（≤20 字）和正文后用 xhs_draft kind=post（media 填上面的文件）保存，"
                        "再 xhs_review 送审；批准后按 xhs_begin 的说明在 creator.xiaohongshu.com 发布并 xhs_sent。")
            return await sessions.submit(prompt, mid, source=SOURCE, max_steps=120, timeout_s=3600, title="📕 小红书起草笔记")
        r = create_draft(store, "post", str(body.get("text") or ""), title=str(body.get("title") or ""),
                         media=[str(m) for m in body.get("media") or []], source="user", approved=True)
        if r.get("error"):
            raise HTTPException(400, r["error"])
        add_soon_slot(store)
        return r["draft"]

    @app.post("/api/xhs/run", dependencies=dep)
    async def xhs_run():
        if pause_state(store):
            raise HTTPException(409, "已暂停（页面出现过警告），到期前不能运行；可以先点「解除暂停」")
        if live_xhs_task(sessions):
            raise HTTPException(409, "已经有一个小红书会话在进行")
        try:
            return await start_session(store, sessions)
        except (RuntimeError, KeyError) as e:
            raise HTTPException(409, str(e))

    @app.post("/api/xhs/pause", dependencies=dep)
    async def xhs_pause(body: dict):
        hours = float((body or {}).get("hours") or 24)
        return await trigger_pause(store, None, "other", "用户手动暂停", hours=hours, sessions=sessions)

    @app.delete("/api/xhs/pause", dependencies=dep)
    async def xhs_resume():
        resume(store)
        return {"ok": True}
