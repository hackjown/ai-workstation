"""Self-evolution (3.8.5): what Aiw learned the hard way, so a problem solved once is handled directly next time.

A *lesson* is a problem signature and the fix that worked, scoped to where it applies:
* machine  one computer        ("这台电脑的家目录是 /data/home：用 ~/workspace，不是 /home/user/workspace")
* site     a web domain        ("trade.m.jd.com/common/limit.html 是京东风控限流页，不是验证码")
* skill    a skill id          ("huashu-motion 渲染前先 om doctor")
* tool     an agent tool       ("officecli 用全路径 ~/.local/bin/officecli")
* general  anything else

Writing
* After a task ends well (succeeded, also a 继续 run after the user fixed something), `learn_from_task` collects the
  errors and what followed them, the user's answers and corrections, and asks the small model for at most 4 lessons
  (JSON). A few problems are recognised without a model (`quick`): a guessed /home/<x> path on a computer whose home is
  elsewhere, JD's limit.html risk page.
* What the user says to correct Aiw ("不是…，是…", "应该…", "以后…", "别再…") is stored as a user lesson with a high
  weight and shown as「用户纠正过：…」.
* Duplicates merge: same scope + key and a similar problem (or fix) → one row, newest fix, hits + 1.
* Passwords, codes and tokens are refused (memory.looks_secret).

Reading
* `relevant(machine, domains, skills, tools, text)` scores rows by scope match, text similarity (CJK bigrams, the same
  as memory.py), user weight and hits. sessions.py puts the top ones into the task prompt at start ([LESSONS] block)
  and loop.py asks again with the error text when an action fails (`hint`), so the fix shows up at the moment it is
  needed. Every use bumps hits and last_used.
* /api/lessons lists, searches and deletes them (设置 › Aiw 学到的经验 on web and iOS).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from .memory import norm, similarity

log = logging.getLogger("aiw.lessons")

SCOPES = ("machine", "site", "skill", "tool", "general")
SCOPE_CN = {"machine": "电脑", "site": "网站", "skill": "技能", "tool": "工具", "general": "通用"}
USER_WEIGHT, AUTO_WEIGHT = 3.0, 1.0
MAX_TEXT = 400
OPEN, CLOSE = "[LESSONS]", "[/LESSONS]"

CORRECTION_RE = re.compile(
    r"(不是[^，。,.!！?？]{1,40}[，,、 ]*(而)?是|应该(是|用|在|去|先)|搞错了|弄错了|错了[，,。 ]|不对[，,。 ]|"
    r"以后(都|要|别|不要|记得)|下次(要|别|不要|记得)|别再|不要再|记得(要|先)|你应该|正确的(是|做法)|其实是)")

URL_RE = re.compile(r"https?://[^\s'\"<>)）]+", re.I)
DOMAIN_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|ai|co|me|tv|app|dev|sh|top|xyz|hk|tw|jp))\b", re.I)

EXTRACT_PROMPT = """你是 Aiw 的经验整理员（LESSONS）。Aiw 是在用户的电脑上替用户干活的智能体。下面是一个已经完成的任务的记录：
它遇到的错误、之后成功的做法、用户的回答和纠正。请提炼「下次遇到同样问题可以直接用」的经验。
只记可复用的：环境事实（路径、家目录、已装/没装的软件）、网站的登录流程和风控页的含义、工具/命令的坑和能用的命令、技能的正确用法、用户纠正过的做法。
不要记：这次任务的具体内容和结果、一次性的数据、闲聊、密码、验证码、token、账号密码。
已有的经验（不要重复，同一件事用 same）：
{existing}

输出一行 JSON：{{"lessons": [...]}}，最多 4 条，每条：
{{"scope": "machine|site|skill|tool|general", "key": "machine 填电脑 id，site 填域名如 jd.com，skill 填技能 id，tool 填工具名，general 留空",
 "problem": "问题特征，一句话（看到什么报错/页面/现象）", "fix": "可直接照做的解决办法，一句话，含能用的命令或路径",
 "from_user": true 或 false（是不是用户纠正/告诉的）}}
没有值得记的就输出 {{"lessons": []}}。"""


# 3.8.7: drawing lessons from task-a12 (a floor plan redrawn from a sales photo, 187 steps, stopped by the user)
SEED_MARK = "lessons_seeded_v1"
SEED = [
    ("skill", "cad-drawing", "照片读尺寸：一次问整张图的所有尺寸，两次答案不同（9300/9500），还编出九宫格布局",
     "drawing_read 一条尺寸链一条链地放大读两遍，两遍一致才用；对边链总和必须相等；drawkit.py fit 用刻度像素核对每段"),
    ("skill", "cad-drawing", "小图上的数字容易看错：3900 像 3000、1000 像 1600、两段 600 被当成一段",
     "竖向链旋转成横排放大 6–10 倍再读；fit 报某段写的数与量出的长度差 >20% 就只重读那一段"),
    ("skill", "cad-drawing", "用户转述的尺寸链与原图不一致",
     "以原图为准（两遍读 + 刻度核对），交付时列出与用户所给数字不同之处"),
    ("skill", "cad-drawing", "每一版布局都不一样，房间位置来回变",
     "房间只取图上的文字标签和像素位置，按拟合比例换成 mm 吸附到轴线，写进 cad/spec.json；之后只改 spec 里那一项"),
    ("skill", "cad-drawing", "在 LibreCAD 里逐批手打坐标，几十步在挪文字、用 ERASE 打补丁留下孤立图元",
     "写 spec，用 drawing_build 整张重新生成（文字位置自动避让、缩字号）；LibreCAD 只用来看和小改"),
    ("skill", "cad-drawing", "核对只靠视觉模型说“基本一致”",
     "看 drawing_build 的 report.json（errors 必须为空）和 overlay.png 叠图，再用 read_image 针对性问一次；最多 3 轮，之后报告不确定项"),
]

SECRET_VALUE_RE = re.compile(
    r"((密码|口令|passw(or)?d|pwd|验证码|校验码|动态码|token|api[ _-]?key|secret|私钥|cvv|cvc|支付密码)\s*(是|为|[:：=])\s*\S{3,}|"
    r"sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{10,}|xox[abp]-|AKIA[0-9A-Z]{12,}|-----BEGIN|"
    r"\b(?=[A-Za-z0-9_\-]{28,}\b)(?=\S*\d)(?=\S*[A-Za-z])[A-Za-z0-9_\-]+\b)", re.I)


def looks_secret(text: str) -> bool:
    """A lesson may talk ABOUT captchas and logins ("limit.html 不是验证码"); it may never hold a secret value."""
    return bool(SECRET_VALUE_RE.search(text or ""))


def ensure_table(store) -> None:
    store._x("CREATE TABLE IF NOT EXISTS lessons (id TEXT PRIMARY KEY, scope TEXT, key TEXT, problem TEXT, fix TEXT, "
             "source TEXT, weight REAL, hits INTEGER DEFAULT 0, last_used REAL, task_id TEXT, created_at REAL, "
             "updated_at REAL)")
    store._x("CREATE INDEX IF NOT EXISTS ix_lessons_scope ON lessons(scope, key)")


def domain_of(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = urlparse(s).hostname or ""
    s = s.split("/")[0].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def base_domain(host: str) -> str:
    """item.jd.com -> jd.com, trade.m.jd.com -> jd.com, a.b.com.cn -> b.com.cn."""
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if parts[-2] in ("com", "net", "org", "gov", "edu", "co") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domains_in(text: str) -> list[str]:
    out: list[str] = []
    for m in URL_RE.finditer(text or ""):
        d = base_domain(domain_of(m.group(0)))
        if d and d not in out:
            out.append(d)
    for m in DOMAIN_RE.finditer(text or ""):
        d = base_domain(domain_of(m.group(1)))
        if d and d not in out:
            out.append(d)
    return out


def _clip(s: Any, n: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


class Lessons:
    def __init__(self, store, llm: Optional[Callable[[str], Any]] = None):
        self.store = store
        self.llm = llm          # role -> router (chat.Chat.llm); "small" extracts
        self._lock = asyncio.Lock()
        ensure_table(store)
        try:
            self.seed()
        except Exception as e:  # noqa: BLE001
            log.warning("seeding lessons failed: %s", e)

    def seed(self) -> int:
        """Built-in lessons, written once (kv lessons_seeded_v1), so the user may delete them for good."""
        try:
            if self.store.kv_get(SEED_MARK):
                return 0
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for scope, key, problem, fix in SEED:
            if self.add(scope, key, problem, fix, "auto"):
                n += 1
        self.store.kv_set(SEED_MARK, {"at": time.time(), "n": n})
        return n

    # ------------------------------------------------------------------ rows
    def items(self, scope: Optional[str] = None, q: Optional[str] = None, limit: int = 300) -> list[dict]:
        if scope and scope in SCOPES:
            rows = self.store._x("SELECT * FROM lessons WHERE scope=? ORDER BY updated_at DESC LIMIT ?", (scope, limit))
        else:
            rows = self.store._x("SELECT * FROM lessons ORDER BY updated_at DESC LIMIT ?", (limit,))
        if q:
            rows = sorted((r for r in rows if similarity(q, f"{r['key']} {r['problem']} {r['fix']}") > 0.05
                           or q.lower() in f"{r['key']} {r['problem']} {r['fix']}".lower()),
                          key=lambda r: -similarity(q, f"{r['problem']} {r['fix']}"))
        return [self._out(r) for r in rows]

    def _out(self, r: dict) -> dict:
        d = dict(r)
        d["scope_cn"] = SCOPE_CN.get(d.get("scope") or "general", "通用")
        d["from_user"] = (d.get("source") or "") == "user"
        d["where"] = self.where(d)
        return d

    def where(self, r: dict) -> str:
        scope, key = r.get("scope") or "general", r.get("key") or ""
        if scope == "machine" and key:
            m = self.store.machine(key) if hasattr(self.store, "machine") else None
            return f"电脑 {(m or {}).get('name') or key}"
        return f"{SCOPE_CN.get(scope, '通用')} {key}".strip()

    def get(self, lid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM lessons WHERE id=?", (lid,))
        return self._out(r[0]) if r else None

    def delete(self, lid: str) -> bool:
        if not self.get(lid):
            return False
        self.store._x("DELETE FROM lessons WHERE id=?", (lid,))
        return True

    def clear(self) -> int:
        n = len(self.store._x("SELECT id FROM lessons"))
        self.store._x("DELETE FROM lessons")
        return n

    def add(self, scope: str, key: str, problem: str, fix: str, source: str = "auto",
            task_id: Optional[str] = None) -> Optional[dict]:
        """Store one lesson, or merge it into the same one (same scope + key, similar problem or fix)."""
        scope = scope if scope in SCOPES else "general"
        key = _clip(key, 120)
        if scope == "site":
            key = base_domain(domain_of(key)) or key
        if scope == "general":
            key = ""
        problem, fix = _clip(problem), _clip(fix)
        if not fix or looks_secret(problem + " " + fix):
            return None
        if not problem:
            problem = fix[:60]
        user = source == "user"
        now = time.time()
        same = None
        for r in self.store._x("SELECT * FROM lessons WHERE scope=? AND key=?", (scope, key)):
            if (norm(r["problem"]) == norm(problem) or similarity(problem, r["problem"]) >= 0.55
                    or similarity(fix, r["fix"]) >= 0.7):
                same = r
                break
        if same:
            # newest words win; a user's correction is never overwritten by an automatic note
            keep_old = (same.get("source") == "user") and not user
            self.store._x("UPDATE lessons SET problem=?, fix=?, source=?, weight=?, hits=COALESCE(hits,0)+1, "
                          "updated_at=?, task_id=COALESCE(?, task_id) WHERE id=?",
                          (same["problem"] if keep_old else problem, same["fix"] if keep_old else fix,
                           "user" if (user or same.get("source") == "user") else "auto",
                           max(float(same.get("weight") or 0), USER_WEIGHT if user else AUTO_WEIGHT), now, task_id,
                           same["id"]))
            return self.get(same["id"])
        lid = f"les-{uuid.uuid4().hex[:10]}"
        self.store._x("INSERT INTO lessons (id, scope, key, problem, fix, source, weight, hits, last_used, task_id, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (lid, scope, key, problem, fix, "user" if user else "auto", USER_WEIGHT if user else AUTO_WEIGHT,
                       1, None, task_id, now, now))
        return self.get(lid)

    # ------------------------------------------------------------------ reading
    def relevant(self, machine: Optional[str] = None, domains: Iterable[str] = (), skills: Iterable[str] = (),
                 tools: Iterable[str] = (), text: str = "", k: int = 6, exclude: Iterable[str] = (),
                 min_score: float = 1.0) -> list[dict]:
        doms = {base_domain(domain_of(d)) for d in domains if d}
        doms |= set(domains_in(text))
        sks = {str(s).lower() for s in skills if s}
        tls = {str(t).lower() for t in tools if t}
        skip = set(exclude)
        scored = []
        for r in self.store._x("SELECT * FROM lessons ORDER BY updated_at DESC LIMIT 800"):
            if r["id"] in skip:
                continue
            scope, key = r.get("scope") or "general", (r.get("key") or "").lower()
            sim = similarity(text, f"{r['problem']} {r['fix']}") if text else 0.0
            if scope == "machine":
                match = 3.0 if machine and key == str(machine).lower() else None
            elif scope == "site":
                match = 3.0 if key and any(d == key or d.endswith("." + key) for d in doms) else None
            elif scope == "skill":
                match = 3.0 if key in sks else None
            elif scope == "tool":
                match = 2.0 if key in tls else None
            else:
                match = 0.4
            if match is None:   # scoped elsewhere: only when the error text itself points at it
                if sim < 0.35:
                    continue
                match = 0.0
            if scope == "general" and sim < 0.12 and r.get("source") != "user":
                continue
            s = match + 4.0 * sim + float(r.get("weight") or 1.0) + 0.3 * math.log1p(int(r.get("hits") or 0))
            if s >= min_score:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        return [self._out(r) for _, r in scored[:k]]

    def mark_used(self, ids: Iterable[str]) -> None:
        now = time.time()
        for i in ids:
            self.store._x("UPDATE lessons SET hits=COALESCE(hits,0)+1, last_used=? WHERE id=?", (now, i))

    @staticmethod
    def line(r: dict) -> str:
        head = "用户纠正过：" if r.get("from_user") or r.get("source") == "user" else ""
        return f"- [{r.get('where') or r.get('scope')}] {head}{r['problem']} → {r['fix']}"

    def block(self, rows: list[dict]) -> str:
        if not rows:
            return ""
        return (OPEN + "\nLESSONS LEARNED (Aiw's own notes from earlier tasks on these computers/sites/skills; apply "
                "them directly instead of finding the problem again. 「用户纠正过」 lines are the user's own corrections: "
                "they always win):\n" + "\n".join(self.line(r) for r in rows) + "\n" + CLOSE)

    def for_task(self, machine: str, prompt: str, skills: Iterable[str] = (), k: int = 6) -> tuple[str, list[str]]:
        rows = self.relevant(machine=machine, domains=domains_in(prompt), skills=skills, text=prompt, k=k)
        if rows:
            self.mark_used(r["id"] for r in rows)
        return self.block(rows), [r["id"] for r in rows]

    def hint(self, error: str, tool: str = "", machine: Optional[str] = None, domains: Iterable[str] = (),
             skills: Iterable[str] = (), exclude: Iterable[str] = ()) -> tuple[str, list[str]]:
        """Lessons for a failed action, retrieved by the error text (plus where it happened)."""
        if not (error or "").strip():
            return "", []
        rows = self.relevant(machine=machine, domains=domains, skills=skills, tools=[tool] if tool else [],
                             text=error, k=3, exclude=exclude, min_score=2.2)
        if not rows:
            return "", []
        self.mark_used(r["id"] for r in rows)
        return ("A LESSON from earlier tasks matches this error; apply it now:\n" + "\n".join(self.line(r) for r in rows),
                [r["id"] for r in rows])

    # ------------------------------------------------------------------ learning
    def quick(self, task: dict, events: list[dict], home: Optional[str] = None) -> list[dict]:
        """Lessons recognised without a model."""
        out = []
        mid = task.get("machine_id") or ""
        blob = " ".join(_clip(json.dumps(e.get("data") or {}, ensure_ascii=False), 4000) for e in events
                        if e.get("type") in ("agent.action_result", "agent.decision", "agent.recovery", "agent.error"))
        m = re.search(r"/home/(user|aiw|ubuntu)/workspace", blob)
        if m and home and not home.startswith(f"/home/{m.group(1)}") and re.search(
                r"No such file|FileNotFound|not found|不存在|找不到|/data/home", blob):
            out.append(self.add("machine", mid, f"工作区路径：这台电脑的家目录是 {home}，不是 /home/{m.group(1)}",
                                f"用 ~/workspace 或 $HOME/workspace（实际是 {home}/workspace；Python 里用 "
                                "os.path.expanduser('~/workspace')），绝不写死 /home/user/workspace", "auto", task.get("id")))
        if "limit.html" in blob and "jd" in blob.lower():
            out.append(self.add("site", "jd.com", "trade.m.jd.com/common/limit.html（活动火爆）是京东风控限流页，不是验证码",
                                "不要当验证码处理、不要反复刷新；改用 item.jd.com/<sku>.html 桌面链接，间隔 2 小时以上再试",
                                "auto", task.get("id")))
        return [r for r in out if r]

    def corrections(self, texts: Iterable[str], task: dict, domains: Iterable[str] = ()) -> list[dict]:
        """The user's own corrections, kept with a high weight (用户纠正过)."""
        out = []
        doms = [d for d in domains if d]
        title = _clip(re.sub(r"【技能[:：][^】]*】", "", task.get("prompt") or ""), 40)
        for t in texts:
            t = _clip(t, 300)
            if len(t) < 6 or not CORRECTION_RE.search(t) or looks_secret(t):
                continue
            scope, key = ("site", doms[0]) if doms else ("general", "")
            r = self.add(scope, key, f"做「{title}」这类事时", t, "user", task.get("id"))
            if r:
                out.append(r)
        return out

    async def learn_from_task(self, task: dict, events: list[dict], user_texts: Iterable[str] = (),
                              home: Optional[str] = None, skills: Iterable[str] = ()) -> list[dict]:
        """Background, after a task ended well: quick rules, the user's corrections, then the small model."""
        user_texts = [t for t in user_texts if t]
        doms = domains_in((task.get("prompt") or "") + " " + " ".join(
            str((e.get("data") or {}).get("args", {}).get("url") or "") for e in events if e.get("type") == "agent.decision"))
        done = self.quick(task, events, home) + self.corrections(user_texts, task, doms)
        if not self.llm:
            return done
        trouble = []   # an error and what the agent did right after it
        decisions = {}
        for e in events:
            d = e.get("data") or {}
            if e.get("type") == "agent.decision":
                decisions[e.get("step")] = d
            if e.get("type") == "agent.action_result" and (not d.get("success") or d.get("error")):
                res = d.get("result") or {}
                err = d.get("error") or (res.get("stderr") if isinstance(res, dict) else "") or ""
                trouble.append(f"第{e.get('step')}步 {d.get('tool')} 出错：{_clip(err, 300)}")
        if not trouble and not user_texts:
            return done
        steps = []
        for st in sorted(k for k in decisions if isinstance(k, int)):
            d = decisions[st]
            steps.append(f"{st}. {_clip(d.get('thought'), 160)} | {d.get('tool')} {_clip(json.dumps(d.get('args') or {}, ensure_ascii=False), 200)}")
        body = (f"电脑：{task.get('machine_id')}（家目录 {home or '未知'}）\n任务：{_clip(task.get('prompt'), 500)}\n"
                f"技能：{', '.join(skills) or '无'}\n网站：{', '.join(doms) or '无'}\n"
                "出过的错：\n" + ("\n".join(trouble[-12:]) or "无") + "\n用户说过：\n" +
                ("\n".join("- " + _clip(t, 300) for t in user_texts[-8:]) or "无") +
                "\n步骤（节选）：\n" + "\n".join(steps[-30:]) + f"\n结果：{_clip(task.get('result'), 400)}")
        async with self._lock:
            near = self.relevant(machine=task.get("machine_id"), domains=doms, skills=skills, text=body, k=12, min_score=0)
            existing = "\n".join(self.line(r) for r in near) or "（无）"
            try:
                out = await asyncio.wait_for(self.llm("small").complete(EXTRACT_PROMPT.format(existing=existing), body, 700), 90)
            except Exception as e:  # noqa: BLE001
                log.warning("lessons learn: %s", e)
                return done
            for it in parse(out)[:4]:
                scope = it.get("scope") if it.get("scope") in SCOPES else "general"
                key = str(it.get("key") or "")
                if scope == "machine" and not key:
                    key = task.get("machine_id") or ""
                r = self.add(scope, key, str(it.get("problem") or ""), str(it.get("fix") or ""),
                             "user" if it.get("from_user") else "auto", task.get("id"))
                if r:
                    done.append(r)
        return done


def parse(text: str) -> list[dict]:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    items = d.get("lessons") if isinstance(d, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


# ---------------------------------------------------------------------------------------------- HTTP
from pydantic import BaseModel  # noqa: E402


class LessonIn(BaseModel):
    scope: str = "general"
    key: str = ""
    problem: str = ""
    fix: str


def mount(app, lessons: Lessons, auth) -> None:
    from fastapi import Depends, HTTPException

    dep = [Depends(auth)]

    @app.get("/api/lessons", dependencies=dep)
    async def list_lessons(scope: Optional[str] = None, q: Optional[str] = None):
        return lessons.items(scope, q)

    @app.post("/api/lessons", dependencies=dep)
    async def add_lesson(body: LessonIn):
        if looks_secret(body.problem + " " + body.fix):
            raise HTTPException(400, "密码、验证码、token 这类内容不会存进经验")
        row = lessons.add(body.scope, body.key, body.problem, body.fix, "user")
        if not row:
            raise HTTPException(400, "经验不能是空的")
        return row

    @app.delete("/api/lessons/{lid}", dependencies=dep)
    async def del_lesson(lid: str):
        if not lessons.delete(lid):
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/api/lessons", dependencies=dep)
    async def clear_lessons():
        return {"deleted": lessons.clear()}
