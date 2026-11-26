"""v3.9.92 每日回顾 + 主动纠错：每天 20:00（AIW_TZ）球球在聊天里发一条「今天做了什么、之前哪句话说错了或变了、还剩什么」。

数据来自 worklog.py 的 work_items（今天的 + 之前还没关掉的），再加上：
* 便宜的确定性核对（每个都有超时）：git ls-remote / GitHub API（仓库 head、最新 release）、PyPI JSON（最新版本 +
  上传日期）、网页（包含某段文字 / sha256 变没变）、已有追踪（watches）的状态。CHECKERS 可在测试里替换。
* 矛盾检测：后来的 work_items、被取代（superseded）的长期记忆、「用户纠正过」的经验，和之前说过的话比相似度
  （关键词，配了 embedding 时再加 cosine），候选对交给小模型判断；被取代的记忆不需要模型（用户自己改了说法）。
  先核对再说：只有核对成功或有用户自己的新说法做依据，才会写进「之前说错/有变化」。

消息五段：今日小结（每个话题一行 + 交付物）、之前说错/有变化（先写正确的事实，再写依据：版本 / 日期，链到原任务）、
没做完（失败 / 停止 / 没回答的）、待你决定（最多 3 条，每条一个动作：继续 / 按新情况重做 / 忽略）、
猜你想要（最多 2 条，见下）。按钮用聊天已有的 ask 消息（POST /api/chat/messages/{id}/answer，chat.answer 转到
review.answer）；原任务链接用 aiw://task/<id>（iOS 原生打开，网页版由 deeplink.js 接管）。

猜你想要：只根据用户自己重复的行为或挂着的事，每条一句「因为…」和一个一键动作（开一个预填好的任务，或建定时任务）：
* repeated_ask   14 天里同一类请求 ≥2 次（不同日子）→ 建定时任务，时间取这些请求的常见时刻（同一个星期几则每周）
* release_upgrade 说过的依赖 / 仓库有了新版本（核对得到的）→ 升级并跑测试（不推送、不发布）
* stale_watch    追踪连续读取失败 / 太久没检查、定时任务上次失败 → 排查修好
* next_step      之前做完但留了「下一步」的事 → 接着做
记忆和「用户纠正过」的经验只用来定时间、给预填的任务加一句注意事项，从不原样列出。付款、下单、发消息、发布
一律不作为建议动作。反馈记在 kv proactive_prefs：没点就算忽略，同一类连续忽略两次静音 14 天，「别再推荐」永久静音；
同一条建议 7 天内不重复（kv review_suggested）。

推送规则：没有可做的事就不发消息、不推送。同一发现按 hash 去重（20 小时内不重复）；一件事推了两次用户都没回应
→ expired，以后不再提。kv proactive_prefs：enabled、time（默认 20:00）、quiet_days（0=周一）、muted_topics、
muted_kinds、sugg_muted、sugg_feedback。聊天里说「别再提 X / 少推 X / 别再推荐 X / 关掉每日回顾」会直接改它
（chat.send 里，像 memory.command 一样）。

手机能力（device_review.py，走已有的设备桥）：已授权「读取日历 / 提醒事项」且手机在线时，加一节「明天」（时间冲突、
早开始的日程、过期 / 明天到期的提醒事项），同一条 20 小时内不重复；没授权时在回顾末尾最多提示一次。

推送：Bark 普通级别（不是 timeSensitive），点开 chat?m=<消息 id>。AIW_REVIEW=0 关掉定时回顾（手动 /run 仍可用）。
API：GET /api/review/today（只算不发）、POST /api/review/run（手动跑，默认发）、GET/PUT /api/review/prefs、
POST /api/review/feedback、GET /api/worklog?day=。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import statistics
import time
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from . import cron
from .memory import cosine, norm, similarity
from .worklog import UNFINISHED, Worklog, clip, day_of, parse_json, same_ask

log = logging.getLogger("aiw.review")

PREFS_KV = "proactive_prefs"
SUGGESTED_KV = "review_suggested"      # {suggestion key: ts}  7-day dedupe
PENDING_KV = "review_pending"          # the last review's suggestions waiting for a tap
NEXT_KV = "review_next_ts"
CAL_KV = "review_cal_pushed"           # tomorrow's calendar / reminder items already told (20 h dedupe)
DEFAULT_TIME = "20:00"
CHECK_TIMEOUT = 15.0
DEDUPE_S = 20 * 3600
SUGG_DEDUPE_S = 7 * 86400
SUGG_MUTE_S = 14 * 86400
MAX_DECIDE, MAX_SUGG = 3, 2
UA = "Mozilla/5.0 AIW-Review"

FINDING_KINDS = {"changed": "版本变化/说错", "unfinished": "没做完", "decide": "待你决定", "suggest": "猜你想要"}
SUGG_KINDS = {"repeated_ask": "定时任务建议", "release_upgrade": "升级建议", "stale_watch": "追踪/定时任务修复",
              "next_step": "接着做"}
SUGG_WORDS = {"repeated_ask": r"定时|重复|每天|每周|例行", "release_upgrade": r"升级|版本|依赖|release|发布",
              "stale_watch": r"追踪|监控|定时任务|watch", "next_step": r"接着|下一步|继续"}
# never offered as an automatic action
SENSITIVE_RE = re.compile(r"付款|支付|付钱|转账|下单|购买|买下|结账|充值|发送|发给|发消息|发邮件|回复.{0,6}邮件|发布|发帖|"
                          r"\bpay\b|purchase|checkout|\bsend\b|\bpost\b|\bpublish\b", re.I)
DEP_RE = re.compile(r"依赖|requirements|pyproject|package\.json|setup\.py|升级|upgrade|dependenc|deps|仓库|repo", re.I)
ACTION_CN = {"continue": "继续", "redo": "按新情况重做", "ignore": "忽略"}

JUDGE_PROMPT = """你是事实核对员。下面「之前的说法」是助理之前说的，「后来的信息」是之后用户说的或后来查到的。
判断后来的信息是否表明之前的说法错了或已经过时。输出一行 JSON：{"contradict": true 或 false, "correct": "正确的说法，一句话"}。
只有明确冲突才是 true；说的不是同一件事、或只是补充，就是 false。"""


# ------------------------------------------------------------------------------------------ prefs
def prefs(store) -> dict:
    p = store.kv_get(PREFS_KV, {}) or {}
    out = {"enabled": True, "time": DEFAULT_TIME, "quiet_days": [], "muted_topics": [], "muted_kinds": [],
           "sugg_muted": {}, "sugg_feedback": {}}
    out.update({k: v for k, v in p.items() if k in out})
    from .device_review import DEFAULTS as _dd   # v3.9.92 device keys live in the same kv
    out.update({k: p.get(k, v) for k, v in _dd.items()})
    return out


def save_prefs(store, body: dict) -> dict:
    p = prefs(store)
    body = body or {}
    if "enabled" in body:
        p["enabled"] = bool(body["enabled"])
    if "time" in body:
        t = str(body["time"] or "").strip()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("时间格式是 HH:MM，例如 20:00")
        p["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
        store.kv_set(NEXT_KV, None)   # recomputed on the next tick
    if "quiet_days" in body:
        p["quiet_days"] = sorted({int(d) for d in body["quiet_days"] or [] if str(d).isdigit() and 0 <= int(d) <= 6})
    for k in ("muted_topics", "muted_kinds"):
        if k in body:
            p[k] = [clip(x, 60) for x in body[k] or [] if str(x).strip()][:50]
    # v3.9.92 device parts (device_review.py)
    if "event_lead_min" in body:
        p["event_lead_min"] = max(0, min(180, int(body["event_lead_min"] or 0)))
    if "early_hour" in body:
        p["early_hour"] = max(0, min(23, int(body["early_hour"] or 0)))
    if "health_enabled" in body:
        p["health_enabled"] = bool(body["health_enabled"])
    if "health_summary_time" in body:
        t = str(body["health_summary_time"] or "off").strip().lower()
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if t != "off" and (not m or int(m.group(1)) > 23 or int(m.group(2)) > 59):
            raise ValueError("健康小结时间格式是 HH:MM，或 off")
        p["health_summary_time"] = t if t == "off" else f"{int(m.group(1)):02d}:{m.group(2)}"
        store.kv_set("health_next_ts", None)
    if "cap_hint_shown" in body:
        p["cap_hint_shown"] = bool(body["cap_hint_shown"])
    if "sugg_muted" in body and isinstance(body["sugg_muted"], dict):
        p["sugg_muted"] = {k: float(v or 0) for k, v in body["sugg_muted"].items() if k in SUGG_KINDS}
    store.kv_set(PREFS_KV, p)
    return p


def _mute_topic(store, topic: str) -> dict:
    p = prefs(store)
    if topic and topic not in p["muted_topics"]:
        p["muted_topics"] = (p["muted_topics"] + [topic])[-50:]
    store.kv_set(PREFS_KV, p)
    return p


def sugg_muted(p: dict, kind: str, now: float) -> bool:
    if kind not in (p.get("sugg_muted") or {}):
        return False
    until = float(p["sugg_muted"][kind] or 0)
    return until == 0 or until > now


def feedback(store, kind: str, verdict: str, now: Optional[float] = None) -> dict:
    """accepted / ignored / never for one suggestion kind. Two ignores in a row -> muted 14 days; never -> for good."""
    now = now or time.time()
    p = prefs(store)
    fb = p["sugg_feedback"].setdefault(kind, {"accepted": 0, "ignored": 0, "ignored_streak": 0})
    if verdict == "accepted":
        fb["accepted"] += 1
        fb["ignored_streak"] = 0
    elif verdict == "ignored":
        fb["ignored"] += 1
        fb["ignored_streak"] += 1
        if fb["ignored_streak"] >= 2:
            p["sugg_muted"][kind] = now + SUGG_MUTE_S
            fb["ignored_streak"] = 0
    elif verdict == "never":
        p["sugg_muted"][kind] = 0
    fb["last"] = now
    store.kv_set(PREFS_KV, p)
    return p


MUTE_RE = re.compile(r"^\s*(?:以后)?(?:请)?(?:你)?(?:别再|不要再|不用再|少)(推荐|提醒?|推送?|跟我说)(?:我)?(?:了)?(?:关于|有关)?[:：,，、\s]*(.*?)\s*(?:了|的事|这件事|这类|这种)?[。.!！]*$")
REVIEW_OFF_RE = re.compile(r"(关掉|关闭|停掉|不要|别发|取消)(每日|每天的?|晚上的?)?(回顾|复盘|晚报)")
REVIEW_ON_RE = re.compile(r"(打开|开启|恢复)(每日|每天的?|晚上的?)?(回顾|复盘|晚报)")


def prefs_command(store, text: str) -> Optional[str]:
    """别再提 X / 少推 X / 别再推荐 X / 关掉每日回顾: act on proactive_prefs and return the reply; None otherwise."""
    t = (text or "").strip()
    if len(t) > 80:
        return None
    if re.search(r"(关掉|关闭|停掉|不要|别发|取消)(每日|每天的?|晚上的?)?健康(小结|总结|汇总)", t):
        save_prefs(store, {"health_enabled": False})
        return "好的，健康小结关掉了。想再打开就说「打开健康小结」。"
    if re.search(r"(打开|开启|恢复)(每日|每天的?|晚上的?)?健康(小结|总结|汇总)", t):
        p = save_prefs(store, {"health_enabled": True})
        return f"好的，健康小结打开了，每天 {p['health_summary_time']} 发（需要手机 aiw App 打开「读取健康」）。"
    if REVIEW_OFF_RE.search(t):
        save_prefs(store, {"enabled": False})
        return "好的，每日回顾关掉了。想再打开就说「打开每日回顾」。"
    if REVIEW_ON_RE.search(t):
        p = save_prefs(store, {"enabled": True})
        return f"好的，每日回顾打开了，每天 {p['time']} 发。"
    m = MUTE_RE.match(t)
    if not m:
        return None
    verb, what = m.group(1), (m.group(2) or "").strip()
    if re.search(r"[，,；;]|帮我|请你|一下", what):   # a real request that happens to start with 别再…
        return None
    if verb == "推荐":
        kinds = [k for k, rx in SUGG_WORDS.items() if what and re.search(rx, what, re.I)]
        if not kinds:   # bare 别再推荐 / 别再推荐这个: the suggestions of the last review
            kinds = sorted({s["kind"] for s in (store.kv_get(PENDING_KV, {}) or {}).get("suggestions", [])
                            if not what or what in ("这个", "这些", "这类", "了") or similarity(what, s.get("title", "")) >= 0.4})
        if not kinds:
            return "最近没有给你推荐过东西。"
        for k in kinds:
            feedback(store, k, "never")
        return "好的，以后不再推荐：" + "、".join(SUGG_KINDS[k] for k in kinds) + "。"
    if not what or len(what) < 2:
        return None
    kinds = [k for k, cn in (("changed", r"版本|说错|变化"), ("unfinished", r"没做完|失败|未完成"), ("suggest", r"建议|猜你想要"))
             if re.fullmatch(f"(?:{cn})的?(?:提醒|消息|事)?", what)]
    if kinds:
        p = prefs(store)
        p["muted_kinds"] = sorted(set(p["muted_kinds"]) | set(kinds))
        store.kv_set(PREFS_KV, p)
        return "好的，每日回顾里不再提「" + "、".join(FINDING_KINDS[k] for k in kinds) + "」。"
    _mute_topic(store, what[:60])
    return f"好的，以后回顾里不再提「{what[:60]}」。想恢复就在 设置 › 通知 › 每日回顾 里删掉。"


def topic_muted(p: dict, *texts: str) -> bool:
    for topic in p.get("muted_topics") or []:
        nt = norm(topic)
        for t in texts:
            if nt and t and (nt in norm(t) or similarity(topic, t) >= 0.5):
                return True
    return False


# ------------------------------------------------------------------------------------------ checkers
def _client(timeout: float = CHECK_TIMEOUT):
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA})


def _ver_eq(a: str, b: str) -> bool:
    return re.sub(r"^v", "", str(a or "").strip(), flags=re.I) == re.sub(r"^v", "", str(b or "").strip(), flags=re.I)


async def check_pypi(target: str, expect: str, store=None) -> dict:
    name = re.sub(r"^https?://pypi\.org/(project|pypi)/", "", target).strip("/").split("/")[0]
    url = f"https://pypi.org/pypi/{quote(name)}/json"
    async with _client() as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()
    ver = str((d.get("info") or {}).get("version") or "")
    files = (d.get("releases") or {}).get(ver) or []
    date = str((files[0] if files else {}).get("upload_time") or "")[:10]
    return {"ok": bool(ver), "now": ver, "date": date, "source": "PyPI", "url": f"https://pypi.org/project/{name}/{ver}/",
            "changed": bool(ver and expect and not _ver_eq(ver, expect)), "subject": name}


async def check_github_release(target: str, expect: str, store=None) -> dict:
    m = re.search(r"(?:github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$", target.strip())
    repo = m.group(1) if m else target
    async with _client() as c:
        r = await c.get(f"https://api.github.com/repos/{repo}/releases/latest", headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        d = r.json()
    tag = str(d.get("tag_name") or "")
    return {"ok": bool(tag), "now": tag, "date": str(d.get("published_at") or "")[:10], "source": "GitHub Releases",
            "url": d.get("html_url") or f"https://github.com/{repo}/releases", "subject": repo,
            "changed": bool(tag and expect and not _ver_eq(tag, expect))}


async def check_git(target: str, expect: str, store=None) -> dict:
    url, _, ref = target.partition("#")
    ref = ref or "HEAD"
    proc = await asyncio.create_subprocess_exec("git", "ls-remote", url, ref, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    sha = (out.decode().split() or [""])[0]
    return {"ok": bool(sha), "now": sha[:10], "date": "", "source": "git ls-remote", "url": url, "subject": f"{url} {ref}",
            "changed": bool(sha and expect and not sha.startswith(expect.strip()[:7]))}


async def check_url(target: str, expect: str, store=None) -> dict:
    from .scheduler import page_text
    async with _client() as c:
        r = await c.get(target)
        r.raise_for_status()
    text = page_text(r.text)
    if expect.startswith("sha256:"):
        h = hashlib.sha256(text.encode()).hexdigest()
        return {"ok": True, "now": "页面内容变了" if not h.startswith(expect[7:]) else "没变", "date": "", "source": "网页",
                "url": target, "subject": target, "changed": not h.startswith(expect[7:])}
    has = bool(expect) and expect.lower() in text.lower()
    return {"ok": True, "now": f"页面{'仍然包含' if has else '已经没有'}「{clip(expect, 40)}」", "date": "", "source": "网页",
            "url": target, "subject": target, "changed": bool(expect) and not has}


async def check_watch(target: str, expect: str, store=None) -> dict:
    w = store.get("watches", target) if store is not None else None
    if not w and store is not None:
        rows = store._x("SELECT * FROM watches WHERE name=? OR url=? ORDER BY created_at DESC LIMIT 1", (target, target))
        w = rows[0] if rows else None
    if not w:
        return {"ok": False}
    bad = int(w.get("fail_count") or 0) >= 3
    trig = w.get("triggered_at")
    now = (f"连续 {w.get('fail_count')} 次读取失败" if bad else
           f"已触发（{day_of(trig)}）" if trig else f"当前值 {clip(w.get('last_value'), 40)}")
    return {"ok": True, "now": now, "date": day_of(w.get("last_checked")) if w.get("last_checked") else "", "source": "追踪",
            "url": w.get("url") or "", "subject": w.get("name") or target,
            "changed": bad or bool(expect and str(w.get("last_value") or "") and str(w.get("last_value")) != expect)}


CHECKERS: dict[str, Callable] = {"pypi": check_pypi, "github_release": check_github_release, "git": check_git,
                                 "url": check_url, "watch": check_watch}


async def run_check(claim: dict, store=None) -> Optional[dict]:
    h = claim.get("how_to_check") or {}
    fn = CHECKERS.get(h.get("kind") or "none")
    if not fn or not h.get("target"):
        return None
    try:
        res = await asyncio.wait_for(fn(h["target"], h.get("expect") or "", store), CHECK_TIMEOUT + 2)
    except Exception as e:  # noqa: BLE001  a failed check says nothing (verify before claiming)
        log.info("review check %s %s: %s", h.get("kind"), h.get("target"), str(e)[:120])
        return None
    return res if res and res.get("ok") else None


# ------------------------------------------------------------------------------------------ helpers
def link(task_id: Optional[str]) -> str:
    return f"[原任务](aiw://task/{quote(task_id, safe='')})" if task_id else ""


def fhash(item_id: str, kind: str, core: str) -> str:
    return hashlib.sha1(f"{item_id}|{kind}|{norm(core)}".encode()).hexdigest()[:16]


def local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, cron.tz())


def _mmdd(day: str) -> str:
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        return f"{d.month}月{d.day}日"
    except ValueError:
        return day


def _routine_schedule(stamps: list[float], hint: Optional[str] = None) -> tuple[str, str]:
    """(schedule, words) for a routine at the usual time of these requests; a memory may name the time instead."""
    if hint:
        m = re.search(r"(\d{1,2})\s*[:：点]\s*(\d{2}|半)?", hint)
        if m and int(m.group(1)) <= 23:
            h, mm = int(m.group(1)), (30 if m.group(2) == "半" else int(m.group(2) or 0))
            if re.search(r"晚上|下午", hint) and h < 12:
                h += 12
            return f"daily {h:02d}:{mm:02d}", f"每天 {h:02d}:{mm:02d}"
    mins = sorted(local(t).hour * 60 + local(t).minute for t in stamps)
    med = int(statistics.median(mins))
    med = (med // 30) * 30
    h, mm = divmod(med, 60)
    dows = {local(t).weekday() for t in stamps}
    if len(dows) == 1 and len(stamps) >= 2:
        wd = dows.pop()
        return f"{mm} {h} * * {(wd + 1) % 7}", f"每周{'一二三四五六日'[wd]} {h:02d}:{mm:02d}"
    return f"daily {h:02d}:{mm:02d}", f"每天 {h:02d}:{mm:02d}"


# ------------------------------------------------------------------------------------------ the review
class Review:
    def __init__(self, store, worklog: Worklog, chat=None, notifier=None, memory=None, llm: Optional[Callable] = None):
        self.store, self.worklog, self.chat, self.notifier, self.memory = store, worklog, chat, notifier, memory
        self.llm = llm
        self.device = None   # device_review.DeviceReview: tomorrow's calendar / reminders (granted capabilities only)
        self.jobs: set = set()
        self._running = asyncio.Lock()

    # ---------------------------------------------------------------- sources
    def _superseded_memories(self, since: float) -> list[tuple[dict, dict]]:
        try:
            rows = self.store._x("SELECT * FROM memories WHERE superseded IS NOT NULL AND superseded NOT IN ('', 'dropped')")
        except Exception:  # noqa: BLE001
            return []
        out = []
        for old in rows:
            new = self.store._x("SELECT * FROM memories WHERE id=?", (old["superseded"],))
            if new and float(new[0].get("created_at") or 0) >= since:
                out.append((old, new[0]))
        return out

    def _user_lessons(self, since: float = 0) -> list[dict]:
        try:
            return self.store._x("SELECT * FROM lessons WHERE source='user' AND COALESCE(updated_at, created_at)>=? "
                                 "ORDER BY created_at DESC LIMIT 100", (since,))
        except Exception:  # noqa: BLE001
            return []

    async def _judge(self, claim: str, later: str) -> Optional[str]:
        if not self.llm:
            return None
        try:
            out = await asyncio.wait_for(self.llm("small").complete(JUDGE_PROMPT, f"之前的说法：{claim}\n后来的信息：{later}", 200), 45)
        except Exception as e:  # noqa: BLE001
            log.info("review judge: %s", e)
            return None
        d = parse_json(out)
        if d.get("contradict") is not True:
            return None
        return clip(d.get("correct"), 200) or clip(later, 200)

    async def _vectors(self, texts: list[str]) -> Optional[dict]:
        mem = self.memory
        if not mem or not (getattr(mem, "embedder", None) or (hasattr(mem, "_embed_cfg") and mem._embed_cfg())):
            return None
        try:
            vecs = await asyncio.wait_for(mem.embed(texts), 20)
        except Exception:  # noqa: BLE001
            return None
        return dict(zip(texts, vecs)) if vecs else None

    async def contradictions(self, items: list[dict], all_recent: list[dict]) -> list[dict]:
        """Earlier claims (and outcomes) against what came later: later work items, superseded memories, user lessons."""
        claims = [(it, c) for it in items for c in (it.get("claims") or [])]
        if not claims:
            return []
        oldest = min(it["ts"] for it, _ in claims)
        sup = self._superseded_memories(oldest)
        les = self._user_lessons(oldest)
        later_items = [x for x in all_recent if x.get("ts")]
        texts = list({c["text"] for _, c in claims} | {n["text"] for _, n in sup} | {l["fix"] for l in les} |
                     {lc["text"] for x in later_items for lc in x.get("claims") or []})
        vec = await self._vectors(texts) if texts else None

        def near(a: str, b: str, kw: float) -> bool:
            if similarity(a, b) >= kw:
                return True
            return bool(vec and a in vec and b in vec and cosine(vec[a], vec[b]) >= 0.82)

        out, seen = [], set()
        for it, c in claims:
            if it["id"] in seen:
                continue
            # 1. the user changed a memory this claim rests on: no model needed
            for old, new in sup:
                if float(new.get("created_at") or 0) > it["ts"] and near(c["text"], old["text"], 0.5):
                    out.append({"kind": "changed", "item": it, "claim": c, "correct": new["text"],
                                "evidence": f"你在 {day_of(new.get('created_at'))} 改了说法（原来记的是「{clip(old['text'], 40)}」）",
                                "core": new["text"]})
                    seen.add(it["id"])
                    break
            if it["id"] in seen:
                continue
            # 2. a later item says something else about the same thing
            h = c.get("how_to_check") or {}
            for x in later_items:
                if x["id"] == it["id"] or x["ts"] <= it["ts"]:
                    continue
                for lc in x.get("claims") or []:
                    lh = lc.get("how_to_check") or {}
                    same_target = h.get("kind") not in (None, "none") and lh.get("kind") == h.get("kind") \
                        and lh.get("target") == h.get("target")
                    if same_target and lh.get("expect") and h.get("expect") and not _ver_eq(lh["expect"], h["expect"]):
                        correct = lc["text"]
                    elif near(c["text"], lc["text"], 0.45) and norm(c["text"]) != norm(lc["text"]):
                        correct = await self._judge(c["text"], lc["text"])
                    else:
                        continue
                    if correct:
                        out.append({"kind": "changed", "item": it, "claim": c, "correct": correct,
                                    "evidence": f"{day_of(x['ts'])}「{clip(x['title'], 24)}」里的新结果", "core": correct,
                                    "later_task": x.get("task_id")})
                        seen.add(it["id"])
                        break
                if it["id"] in seen:
                    break
            if it["id"] in seen:
                continue
            # 3. a correction the user made afterwards (用户纠正过)
            for l in les:
                if float(l.get("created_at") or 0) > it["ts"] and near(c["text"], f"{l['problem']} {l['fix']}", 0.3):
                    correct = await self._judge(c["text"], l["fix"])
                    if correct:
                        out.append({"kind": "changed", "item": it, "claim": c, "correct": correct,
                                    "evidence": f"你在 {day_of(l.get('created_at'))} 纠正过", "core": correct})
                        seen.add(it["id"])
                        break
        return out

    async def rechecks(self, items: list[dict], now: float) -> tuple[list[dict], list[dict]]:
        """(findings, all successful check results) for claims that are due."""
        jobs = [(it, c) for it in items for c in (it.get("claims") or [])
                if (c.get("how_to_check") or {}).get("kind") not in (None, "none") and float(c.get("check_after_ts") or 0) <= now]
        if not jobs:
            return [], []
        results = await asyncio.gather(*(run_check(c, self.store) for _, c in jobs))
        out, done, seen = [], [], set()
        for (it, c), r in zip(jobs, results):
            if not r:
                continue
            done.append({"item": it, "claim": c, "result": r})
            if r.get("changed") and (it["id"], c["text"]) not in seen:
                seen.add((it["id"], c["text"]))
                subj = r.get("subject") or c["how_to_check"]["target"]
                if c["how_to_check"]["kind"] in ("pypi", "github_release"):
                    correct = f"{subj} 现在的最新版本是 {r['now']}" + (f"（{r['date']} 发布）" if r.get("date") else "")
                elif c["how_to_check"]["kind"] == "git":
                    correct = f"{subj} 现在的 head 是 {r['now']}"
                else:
                    correct = f"{subj}：{r['now']}"
                ev = f"{r.get('source')}" + (f" · {r['date']}" if r.get("date") else "") + (f" · {r['url']}" if r.get("url") else "")
                out.append({"kind": "changed", "item": it, "claim": c, "correct": correct, "evidence": ev,
                            "core": f"{subj}={r['now']}", "check": r})
        return out, done

    # ---------------------------------------------------------------- 猜你想要
    def _lesson_note(self, prompt: str) -> str:
        best, score = None, 0.0
        for l in self._user_lessons():
            s = similarity(prompt, f"{l['problem']} {l['fix']}")
            if s > score:
                best, score = l, s
        return f"\n注意（你之前纠正过）：{clip(best['fix'], 120)}" if best and score >= 0.3 else ""

    def _memory_time(self, ask: str) -> Optional[str]:
        try:
            rows = self.store._x("SELECT text FROM memories WHERE (superseded IS NULL OR superseded='') AND kind IN ('decision','fact','profile')")
        except Exception:  # noqa: BLE001
            return None
        for r in rows:
            if re.search(r"\d{1,2}\s*[:：点]", r["text"]) and similarity(ask, r["text"]) >= 0.3:
                return r["text"]
        return None

    def suggestions(self, today: str, recent: list[dict], checks: list[dict], now: float, p: dict) -> list[dict]:
        out: list[dict] = []
        routines = self.store.all("routines", limit=500)
        # repeated_ask
        pool = []
        for it in recent:
            if not it.get("ask") or it.get("source") not in ("task", "chat"):
                continue
            t = self.store.task(it["task_id"]) if it.get("task_id") else None
            if t and (t.get("source") or "") == "routine":
                continue
            pool.append(it)
        clusters: list[list[dict]] = []
        for it in pool:
            for cl in clusters:
                if same_ask(cl[0]["ask"], it["ask"]) or same_ask(cl[0]["title"], it["title"]):
                    cl.append(it)
                    break
            else:
                clusters.append([it])
        for cl in clusters:
            days = {x["day"] for x in cl}
            if len(cl) < 2 or len(days) < 2:
                continue
            ask = cl[-1]["ask"]
            if any(same_ask(r.get("prompt") or "", ask) or same_ask(r.get("name") or "", cl[-1]["title"]) for r in routines):
                continue
            sched, words = _routine_schedule([x["ts"] for x in cl], self._memory_time(ask))
            title = cl[-1]["title"]
            out.append({"kind": "repeated_ask", "key": "repeated_ask:" + norm(title)[:40], "score": 2 + len(cl),
                        "title": f"{words} 自动做「{clip(title, 20)}」",
                        "because": f"最近 {max(1, int((now - cl[0]['ts']) // 86400) + 1)} 天你让我做了 {len(cl)} 次，多在 {words.split()[-1]} 前后",
                        "action": {"type": "routine", "name": clip(title, 30), "prompt": ask + self._lesson_note(ask), "schedule": sched},
                        "label": "建成定时任务"})
        # release_upgrade
        for ck in checks:
            r, it, c = ck["result"], ck["item"], ck["claim"]
            if not r.get("changed") or c["how_to_check"]["kind"] not in ("pypi", "github_release"):
                continue
            ctx = " ".join([it.get("ask") or "", it.get("outcome") or "", c["text"], " ".join(it.get("deliverables") or [])])
            if not DEP_RE.search(ctx):
                continue
            subj = r.get("subject") or c["how_to_check"]["target"]
            repo = re.search(r"github\.com/[\w.-]+/[\w.-]+", ctx)
            where = f"在 {repo.group(0)} 里" if repo else "在用到它的项目里"
            prompt = (f"{where}把 {subj} 从 {c['how_to_check'].get('expect') or '当前版本'} 升级到 {r['now']}，跑一遍测试，"
                      f"把改动和测试结果告诉我。只在新分支上改，先不要推送、合并或发布。")
            out.append({"kind": "release_upgrade", "key": f"release_upgrade:{norm(subj)}:{r['now']}", "score": 4,
                        "title": f"把 {subj} 升级到 {r['now']} 并跑测试",
                        "because": f"你在用的 {subj} 出了新版本 {r['now']}" + (f"（{r['date']}）" if r.get("date") else ""),
                        "action": {"type": "task", "prompt": prompt + self._lesson_note(prompt)}, "label": "升级并测试",
                        "safe_text": f"{where}把 {subj} 升级到 {r['now']} 并跑测试"})
        # stale_watch: failing / stale watches, failed routines
        for w in self.store.all("watches", "enabled=1", (), limit=200):
            interval = max(5, int(w.get("interval_min") or 60)) * 60
            fails = int(w.get("fail_count") or 0)
            stale = w.get("last_checked") and now - float(w["last_checked"]) > max(3 * interval, 86400)
            if fails < 3 and not stale:
                continue
            why = f"追踪「{clip(w['name'], 20)}」" + (f"连续 {fails} 次读取失败" if fails >= 3 else "已经超过一天没检查成功")
            prompt = f"检查追踪「{w['name']}」（{w.get('url') or ''}）为什么读取失败，修好后告诉我结果；不要改动别的追踪。"
            out.append({"kind": "stale_watch", "key": f"stale_watch:{w['id']}", "score": 2.5, "title": f"修好追踪「{clip(w['name'], 16)}」",
                        "because": why, "action": {"type": "task", "prompt": prompt + self._lesson_note(prompt)}, "label": "排查修好"})
        for r in routines:
            if not r.get("enabled") or not r.get("last_task_id"):
                continue
            t = self.store.task(r["last_task_id"]) or {}
            if t.get("status") not in UNFINISHED:
                continue
            prompt = f"定时任务「{r['name']}」上次运行没成功（{clip(t.get('result'), 120)}），查清原因并修好，然后手动跑一次确认。"
            out.append({"kind": "stale_watch", "key": f"stale_routine:{r['id']}:{r['last_task_id']}", "score": 2.5,
                        "title": f"修好定时任务「{clip(r['name'], 16)}」", "because": f"定时任务「{clip(r['name'], 20)}」上次没跑成功",
                        "action": {"type": "task", "prompt": prompt + self._lesson_note(prompt)}, "label": "排查修好"})
        # next_step: done earlier but left a next step
        for it in recent:
            if it["day"] >= today or it.get("status") != "open" or it.get("superseded_by") or it.get("task_status") in UNFINISHED:
                continue
            if not it.get("open_items") or now - it["ts"] > 7 * 86400:
                continue
            nxt = it["open_items"][0]
            prompt = f"接着上次「{it['title']}」做下一步：{nxt}" + (f"\n原来的要求：{clip(it.get('ask'), 300)}" if it.get("ask") else "")
            out.append({"kind": "next_step", "key": f"next_step:{it['id']}", "score": 1.5, "title": f"接着做：{clip(nxt, 24)}",
                        "because": f"上次「{clip(it['title'], 16)}」还留着这一步", "action": {"type": "task", "prompt": prompt + self._lesson_note(prompt)},
                        "label": "接着做", "item_id": it["id"]})
        # rules: safe, not muted, not suggested in 7 days, at most 2
        done = self.store.kv_get(SUGGESTED_KV, {}) or {}
        keep = []
        for s in sorted(out, key=lambda s: -s["score"]):
            a = s["action"]
            # our own "do not push / publish" guard words do not count; the user's ask and the action itself do
            if SENSITIVE_RE.search(s.get("safe_text") or a.get("prompt") or "") or SENSITIVE_RE.search(s["title"]):
                continue
            if sugg_muted(p, s["kind"], now) or "suggest" in p.get("muted_kinds", []):
                continue
            if topic_muted(p, s["title"], a.get("prompt") or ""):
                continue
            if now - float(done.get(s["key"]) or 0) < SUGG_DEDUPE_S:
                continue
            if any(k["key"] == s["key"] for k in keep):
                continue
            s["id"] = "sg-" + hashlib.sha1(s["key"].encode()).hexdigest()[:8]
            keep.append(s)
            if len(keep) >= MAX_SUGG:
                break
        return keep

    # ---------------------------------------------------------------- build
    async def build_review(self, day: Optional[str] = None, now: Optional[float] = None) -> dict:
        now = now or time.time()
        day = day or day_of(now)
        p = prefs(self.store)
        today = self.worklog.items(day)
        earlier = self.worklog.open_before(day)
        live = [i for i in today if not i.get("superseded_by")]
        pool = [i for i in live + earlier if i.get("status") != "expired"]

        def item_ok(it: dict) -> bool:
            if it.get("status") == "expired" or it.get("superseded_by") or it.get("responded_ts"):
                return False
            if int(it.get("push_count") or 0) >= 2:   # told twice, no answer: never again
                return False
            return not topic_muted(p, it.get("title") or "", it.get("ask") or "")

        def fresh(it: dict, h: str) -> bool:
            return not (h in (it.get("push_hash") or []) and now - float(it.get("last_pushed_ts") or 0) < DEDUPE_S)

        recent = self.worklog.recent(14, now)
        changed, checks = await self.rechecks([i for i in pool if item_ok(i)], now)
        cx = await self.contradictions([i for i in pool if item_ok(i) and i["id"] not in {f["item"]["id"] for f in changed}],
                                       recent)
        expired = [i for i in pool if i.get("status") == "open" and not i.get("responded_ts") and int(i.get("push_count") or 0) >= 2]
        findings = []
        for f in changed + cx:
            it = f["item"]
            if not item_ok(it) or "changed" in p["muted_kinds"]:
                continue
            f["hash"] = fhash(it["id"], "changed", f["core"])
            if fresh(it, f["hash"]):
                findings.append(f)
        unfinished = []
        for it in pool:
            if not item_ok(it) or "unfinished" in p["muted_kinds"]:
                continue
            if it.get("task_status") in UNFINISHED or (it.get("source") == "chat" and it.get("open_items")):
                h = fhash(it["id"], "unfinished", it.get("task_status") or "open")
                if fresh(it, h):
                    unfinished.append({"kind": "unfinished", "item": it, "hash": h})
        decide = []
        if "decide" not in p["muted_kinds"]:
            for f in findings:
                decide.append({"item": f["item"], "action": "redo", "why": f["correct"]})
            for u in unfinished:
                it = u["item"]
                if any(d["item"]["id"] == it["id"] for d in decide):
                    continue
                act = "ignore" if now - it["ts"] > 3 * 86400 else "continue"
                decide.append({"item": it, "action": act, "why": it.get("outcome") or ""})
            decide = decide[:MAX_DECIDE]
        sugg = self.suggestions(day, recent, checks, now, p)
        tomorrow = []
        if self.device is not None and "tomorrow" not in p["muted_kinds"]:
            try:
                cal_done = self.store.kv_get(CAL_KV, {}) or {}
                for c in await self.device.tomorrow(now):
                    if topic_muted(p, c["text"]) or now - float(cal_done.get(c["key"]) or 0) < DEDUPE_S:
                        continue
                    tomorrow.append(c)
            except Exception as e:  # noqa: BLE001  offline / no permission: skip silently
                log.info("review tomorrow: %s", e)
        if not any(not c.get("info") for c in tomorrow):
            tomorrow_actionable = False
        else:
            tomorrow_actionable = True
        topics: list[dict] = []
        for it in live:
            for t in topics:
                if same_ask(t["title"], it["title"]):
                    t["n"] += 1
                    t["deliverables"] += [d for d in it.get("deliverables") or [] if d not in t["deliverables"]]
                    t["outcome"] = it.get("outcome") or t["outcome"]
                    t["task_id"] = it.get("task_id") or t["task_id"]
                    break
            else:
                topics.append({"title": it["title"], "outcome": it.get("outcome") or "", "n": 1,
                               "deliverables": list(it.get("deliverables") or []), "task_id": it.get("task_id")})
        actionable = bool(findings or unfinished or decide or sugg or tomorrow_actionable)
        rev = {"day": day, "generated_at": now, "topics": topics, "changed": findings, "unfinished": unfinished,
               "decide": decide, "suggest": sugg, "tomorrow": tomorrow if actionable else [], "actionable": actionable,
               "expired": [i["id"] for i in expired], "hint": ""}
        rev["markdown"] = render(rev)
        rev["options"], rev["option_map"] = options(rev)
        return rev

    # ---------------------------------------------------------------- run / post
    def _settle_pending(self, now: float) -> None:
        """The last review's suggestions nobody tapped count as ignored."""
        pend = self.store.kv_get(PENDING_KV, {}) or {}
        for s in pend.get("suggestions") or []:
            if not s.get("answered"):
                feedback(self.store, s["kind"], "ignored", now)
        self.store.kv_set(PENDING_KV, {})

    async def run(self, day: Optional[str] = None, push: bool = True, now: Optional[float] = None) -> dict:
        async with self._running:
            now = now or time.time()
            if push:
                self._settle_pending(now)
                for iid in [r["id"] for r in self.store._x(
                        "SELECT id FROM work_items WHERE status='open' AND push_count>=2 AND responded_ts IS NULL")]:
                    self.worklog.update(iid, status="expired")
            rev = await self.build_review(day, now)
            rev["posted"] = None
            if not push or not rev["actionable"]:
                return public(rev)
            if self.device is not None:   # one gentle hint, ever, about a useful capability that is off
                rev["hint"] = self.device.hint()
                if rev["hint"]:
                    rev["markdown"] += "\n\n" + rev["hint"]
            msg = self.post(rev)
            rev["posted"] = msg
            self._mark(rev, now)
            return public(rev)

    def _mark(self, rev: dict, now: float) -> None:
        hashes: dict[str, list] = {}
        items: dict[str, dict] = {}
        for f in rev["changed"] + rev["unfinished"]:
            hashes.setdefault(f["item"]["id"], []).append(f["hash"])
            items[f["item"]["id"]] = f["item"]
        for d in rev["decide"]:
            items[d["item"]["id"]] = d["item"]
        for iid, it in items.items():
            cur = self.worklog.get(iid) or it
            self.worklog.update(iid, push_count=int(cur.get("push_count") or 0) + 1, last_pushed_ts=now,
                                push_hash=((cur.get("push_hash") or []) + hashes.get(iid, []))[-20:])
        cal = {k: v for k, v in (self.store.kv_get(CAL_KV, {}) or {}).items() if now - float(v) < 7 * 86400}
        for c in rev.get("tomorrow") or []:
            cal[c["key"]] = now
        self.store.kv_set(CAL_KV, cal)
        done = self.store.kv_get(SUGGESTED_KV, {}) or {}
        done = {k: v for k, v in done.items() if now - float(v) < 30 * 86400}
        for s in rev["suggest"]:
            done[s["key"]] = now
        self.store.kv_set(SUGGESTED_KV, done)
        msg = rev.get("posted") or {}
        self.store.kv_set(PENDING_KV, {"ts": now, "msg_id": msg.get("ask_id"),
                                       "suggestions": [{"id": s["id"], "kind": s["kind"], "key": s["key"], "title": s["title"]}
                                                       for s in rev["suggest"]]})

    def post(self, rev: dict) -> dict:
        out: dict = {}
        if self.chat is None:
            return out
        m = self.chat.add("assistant", "text", rev["markdown"], {"review": {"day": rev["day"]}})
        out["id"] = m["id"]
        if rev["options"]:
            a = self.chat.add("assistant", "ask", "要我怎么处理？（也可以直接回复，例如「别再提 X」）", {
                "review": {"day": rev["day"], "text_id": m["id"]}, "ask_kind": "question", "title": "每日回顾",
                "question": "要我怎么处理？", "options": rev["options"], "option_map": rev["option_map"]})
            out["ask_id"] = a["id"]
        if self.notifier is not None:
            n = len(rev["changed"]) + len(rev["unfinished"]) + len(rev["suggest"])
            body = summary_line(rev) or f"今天有 {n} 件事需要你看一下"
            try:
                asyncio.get_running_loop().create_task(self.notifier.notify(
                    "review", "今日回顾", body, deep=f"chat?m={m['id']}", message_id=m["id"]))
            except RuntimeError:
                pass
        return out

    # ---------------------------------------------------------------- answering a button
    async def answer(self, chat, m: dict, choice: str) -> dict:
        d = m.get("data") or {}
        omap = d.get("option_map") or {}
        act = omap.get(choice or "")
        now = time.time()
        if not act:   # typed words: 别再提… etc., or a free instruction that starts a task
            reply = prefs_command(self.store, choice or "")
            if reply:
                return {"ok": True, "message": m, "reply": reply}
            raise ValueError("请点一个按钮，或者直接说「别再提 X」")
        msgs = []
        if act["type"] == "item":
            it = self.worklog.get(act["item_id"]) or {}
            self.worklog.update(act["item_id"], responded_ts=now, status="closed")
            if act["action"] in ("continue", "redo"):
                prompt = it.get("ask") or it.get("title") or ""
                if act["action"] == "redo":
                    prompt = f"按新情况重做：{prompt}\n新情况：{act.get('why') or ''}（先核对这一点再动手）"
                card = await self._continue_or_start(chat, it, prompt, act["action"])
                if card:
                    msgs.append(card)
        elif act["type"] == "suggest":
            feedback(self.store, act["kind"], "accepted", now)
            pend = self.store.kv_get(PENDING_KV, {}) or {}
            for s in pend.get("suggestions") or []:
                if s["id"] == act["id"]:
                    s["answered"] = True
            self.store.kv_set(PENDING_KV, pend)
            a = act["action"]
            if a["type"] == "routine":
                nxt = cron.next_run(a["schedule"], now)
                r = self.store.insert("routines", {"name": a["name"], "prompt": a["prompt"], "schedule": a["schedule"],
                                                   "enabled": 1, "next_run": nxt})
                msgs.append(chat.add("assistant", "text", f"建好了定时任务「{r['name']}」（{a['schedule']}），"
                                                           f"下次 {local(nxt).strftime('%m-%d %H:%M')} 运行。可以在 定时任务 里改。",
                                     {"routine_id": r["id"]}))
            else:
                msgs.append(await chat.start_task(a["prompt"], clip(act.get("title"), 30), origin=m["id"]))
            if act.get("item_id"):
                self.worklog.update(act["item_id"], responded_ts=now, status="closed")
        opts = [o for o in d.get("options") or [] if o != choice]
        done = (d.get("done") or []) + [choice]
        data = {"options": opts, "done": done}
        if not opts:
            data["resolved"] = {"answer": choice, "text": "已处理：" + "、".join(done)[:80], "by": "chat", "at": now}
        m = chat.update(m["id"], data=data)
        return {"ok": True, "message": m, "messages": msgs}

    async def _continue_or_start(self, chat, it: dict, prompt: str, action: str) -> Optional[dict]:
        if action == "continue" and it.get("task_id"):
            try:
                from . import continuation as _cont
                root = _cont.root_id(self.store, it["task_id"])
                c = next((x for x in chat.recent_tasks(hours=24 * 14, limit=30) if x["root"] == root), None)
                if c is not None:
                    msgs = await chat.continue_card(c, "继续，把没做完的做完", [], None)
                    return msgs[-1] if msgs else None
            except Exception as e:  # noqa: BLE001
                log.warning("review continue: %s", e)
        return await chat.start_task(prompt, clip(it.get("title"), 30), origin="review")

    # ---------------------------------------------------------------- built-in routine
    async def tick(self, now: Optional[float] = None) -> Optional[str]:
        if os.environ.get("AIW_REVIEW", "1") == "0":
            return None
        p = prefs(self.store)
        if not p["enabled"]:
            return None
        now = now or time.time()
        nxt = self.store.kv_get(NEXT_KV)
        sched = f"daily {p['time']}"
        if not nxt:
            self.store.kv_set(NEXT_KV, cron.next_run(sched, now))
            return None
        if now < float(nxt):
            return None
        self.store.kv_set(NEXT_KV, cron.next_run(sched, now))
        if local(now).weekday() in p["quiet_days"]:
            return "quiet"
        job = asyncio.get_running_loop().create_task(self._safe_run())
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)
        return "started"

    async def _safe_run(self):
        try:
            await self.run(push=True)
        except Exception as e:  # noqa: BLE001
            log.warning("daily review: %s", e)


# ------------------------------------------------------------------------------------------ rendering
def summary_line(rev: dict) -> str:
    bits = []
    if rev["changed"]:
        bits.append(f"{len(rev['changed'])} 处说法有变化")
    if rev["unfinished"]:
        bits.append(f"{len(rev['unfinished'])} 件没做完")
    if rev["suggest"]:
        bits.append(f"{len(rev['suggest'])} 个建议")
    n_cal = len([c for c in rev.get("tomorrow") or [] if not c.get("info")])
    if n_cal:
        bits.append(f"明天有 {n_cal} 件要留意")
    first = rev["changed"][0]["correct"] if rev["changed"] else ""
    return ("、".join(bits) + (f"。{clip(first, 80)}" if first else "")) if bits else ""


def render(rev: dict) -> str:
    L = [f"🌙 **今日回顾 · {_mmdd(rev['day'])}**"]
    if rev["topics"]:
        L += ["", "**今日小结**"]
        for t in rev["topics"][:8]:
            line = f"- {clip(t['title'], 30)}" + (f"（{t['n']} 次）" if t["n"] > 1 else "") + (f"：{clip(t['outcome'], 60)}" if t["outcome"] else "")
            if t["deliverables"]:
                line += " · 交付：" + "、".join(clip(x, 40) for x in t["deliverables"][:3])
            L.append(line)
    if rev.get("tomorrow"):
        L += ["", "**明天**"]
        for c in rev["tomorrow"]:
            L.append(f"- {'⚠️ ' if c['kind'] in ('conflict', 'overdue') else ''}{c['text']}")
    if rev["changed"]:
        L += ["", "**之前说错 / 有变化**"]
        for f in rev["changed"]:
            it = f["item"]
            L.append(f"- ✅ {f['correct']}。之前说的是「{clip(f['claim']['text'], 60)}」（{_mmdd(it['day'])}）。"
                     f"依据：{f['evidence']}" + (f" · {link(it.get('task_id'))}" if it.get("task_id") else ""))
    if rev["unfinished"]:
        L += ["", "**没做完**"]
        for u in rev["unfinished"]:
            it = u["item"]
            why = (it.get("open_items") or [it.get("outcome") or ""])[0]
            L.append(f"- {clip(it['title'], 30)}" + (f"：{clip(why, 70)}" if why else "") +
                     (f" · {link(it.get('task_id'))}" if it.get("task_id") else ""))
    if rev["decide"]:
        L += ["", "**待你决定**"]
        for i, dd in enumerate(rev["decide"], 1):
            L.append(f"{i}. {clip(dd['item']['title'], 30)} → 建议：**{ACTION_CN[dd['action']]}**")
    if rev["suggest"]:
        L += ["", "**猜你想要**"]
        for s in rev["suggest"]:
            L.append(f"- {s['title']}（因为{s['because']}）")
    return "\n".join(L)


def options(rev: dict) -> tuple[list[str], dict]:
    opts, omap = [], {}
    for dd in rev["decide"]:
        it = dd["item"]
        label = f"{ACTION_CN[dd['action']]}：{clip(it['title'], 12)}"
        if label in omap:
            continue
        opts.append(label)
        omap[label] = {"type": "item", "item_id": it["id"], "action": dd["action"], "why": clip(dd.get("why"), 200)}
    for s in rev["suggest"]:
        label = f"{s['label']}：{clip(s['title'], 12)}"
        if label in omap:
            continue
        opts.append(label)
        omap[label] = {"type": "suggest", "id": s["id"], "kind": s["kind"], "action": s["action"], "title": s["title"],
                       "item_id": s.get("item_id")}
    return opts, omap


def public(rev: dict) -> dict:
    """JSON-safe view of a review (items reduced to ids / titles)."""
    def it_view(it: dict) -> dict:
        return {"id": it["id"], "title": it["title"], "day": it["day"], "task_id": it.get("task_id"),
                "link": f"aiw://task/{it['task_id']}" if it.get("task_id") else None}
    return {
        "day": rev["day"], "generated_at": rev["generated_at"], "actionable": rev["actionable"], "markdown": rev["markdown"],
        "topics": rev["topics"],
        "changed": [{"item": it_view(f["item"]), "was": f["claim"]["text"], "correct": f["correct"], "evidence": f["evidence"]}
                    for f in rev["changed"]],
        "unfinished": [{"item": it_view(u["item"]), "status": u["item"].get("task_status"), "open_items": u["item"].get("open_items")}
                       for u in rev["unfinished"]],
        "decide": [{"item": it_view(d["item"]), "action": d["action"], "action_cn": ACTION_CN[d["action"]]} for d in rev["decide"]],
        "suggest": [{k: s[k] for k in ("id", "kind", "key", "title", "because", "action", "label")} for s in rev["suggest"]],
        "tomorrow": [{k: c[k] for k in ("kind", "text") if k in c} for c in rev.get("tomorrow") or []],
        "options": rev["options"], "posted": rev.get("posted"), "expired": rev.get("expired", []), "hint": rev.get("hint", ""),
    }


# ------------------------------------------------------------------------------------------ HTTP
def mount(app, review: Review, auth) -> None:
    from fastapi import Depends, HTTPException

    dep = [Depends(auth)]

    @app.get("/api/review/today", dependencies=dep)
    async def review_today(day: Optional[str] = None):
        return await review.run(day, push=False)

    @app.post("/api/review/run", dependencies=dep)
    async def review_run(body: Optional[dict] = None):
        body = body or {}
        return await review.run(body.get("day"), push=body.get("push", True) is not False)

    @app.get("/api/review/prefs", dependencies=dep)
    async def review_prefs():
        p = prefs(review.store)
        return {**p, "next_ts": review.store.kv_get(NEXT_KV), "env_disabled": os.environ.get("AIW_REVIEW", "1") == "0"}

    @app.put("/api/review/prefs", dependencies=dep)
    async def put_review_prefs(body: dict):
        try:
            return save_prefs(review.store, body or {})
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/review/feedback", dependencies=dep)
    async def review_feedback(body: dict):
        kind, verdict = str((body or {}).get("kind") or ""), str((body or {}).get("verdict") or "")
        if kind not in SUGG_KINDS or verdict not in ("accepted", "ignored", "never"):
            raise HTTPException(400, "kind 是 " + "/".join(SUGG_KINDS) + "，verdict 是 accepted/ignored/never")
        return feedback(review.store, kind, verdict)

    @app.get("/api/worklog", dependencies=dep)
    async def worklog_day(day: Optional[str] = None):
        return review.worklog.items(day or day_of())
