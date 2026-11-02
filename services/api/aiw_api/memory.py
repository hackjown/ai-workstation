"""Long-term memory (2026-10-28): what 球球 knows about the user, for the chat AND for every task.

Three kinds in the existing ``memories`` table (text, source) plus new columns:
* profile   who the user is (name, city, job, family, how to address them); always in context
* fact      lasting facts and preferences ("周报放在 ~/workspace/reports", "喜欢靠窗座位")
* decision  choices the user made ("以后订酒店只要可退款的")

Writing
* "记住…" stores at once, "忘掉…" deletes (Memory.command, called by chat.py before any model reply).
* After every chat exchange the small model reads it in the background (Memory.learn) and proposes new memories.
  A duplicate only refreshes the old one; a memory that contradicts an older one wins and the old one is marked
  ``superseded`` (kept for the record, never shown to the model).
* Passwords, verification codes, tokens and keys are never stored, whatever the source (looks_secret).

Reading
* prompt_block(query): the profile (kv "profile" + profile memories) and the top-8 facts/decisions for the query.
  Ranking uses embeddings when an embedding model is set (设置 › 模型 › embed_model; vectors are kept as JSON in the
  row, SQLite has no vector type), otherwise a keyword score (CJK bigrams + words) with a little recency.
* The block is wrapped in [MEMORY] … [/MEMORY] so the task planner, which keeps only the tail of its context, can
  lift it out and keep it (agent_runtime.planner.make_plan).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from typing import Any, Callable, Optional

log = logging.getLogger("aiw.memory")

KINDS = ("profile", "fact", "decision")
KIND_CN = {"profile": "画像", "fact": "事实", "decision": "决定"}
TOP_K = 8
MAX_TEXT = 300
PROFILE_CAP = 16        # v3.9.14: profile lines in the frozen snapshot (consolidation merges past this)
PROFILE_CHARS = 1500
FACT_CAP = 300          # facts + decisions before a daily consolidation pass
OPEN, CLOSE = "[MEMORY]", "[/MEMORY]"

SECRET_RE = re.compile(
    r"(密码|口令|passw(or)?d|passcode|\bpwd\b|验证码|校验码|动态码|短信码|otp\b|一次性码|安全码|cvv|cvc|支付密码|"
    r"token|api[ _-]?key|secret|私钥|private key|access[ _-]?key|bearer\s|sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{10,}|"
    r"xox[abp]-|AKIA[0-9A-Z]{12,}|-----BEGIN)", re.I)
REMEMBER_RE = re.compile(r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?(?:要)?(?:记住|记下|记一下|牢记)(?:一下)?[:：,，、\s]*(.+)$", re.S)
FORGET_RE = re.compile(r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?(?:把)?(?:忘掉|忘记|忘了|别再记|不要再记|删掉记忆|删除记忆)[:：,，、\s]*(.+)$", re.S)

LEARN_PROMPT = """你是长期记忆整理员（MEMORY）。读用户和助理的最新一轮对话，找出值得长期记住的、关于用户本人的信息。
只记长期有用的：身份和画像（称呼、城市、职业、家人、常用账号名，kind=profile）、稳定的偏好和事实（kind=fact）、用户做的决定或规则（kind=decision）。
不要记：一次性的请求、闲聊、天气新闻等公共知识、助理自己说的话、任务的临时进度。
绝对不要记：密码、验证码、token、密钥、银行卡号、身份证号。
已有记忆（带 id）：
{existing}

输出一行 JSON：{{"ops": [...]}}，每个 op 是下面之一：
- {{"op": "add", "kind": "profile|fact|decision", "text": "一句话，第三人称，例如：用户住在杭州"}}
- {{"op": "replace", "id": "被新信息推翻的旧记忆 id", "kind": "...", "text": "新的说法"}}（用户改了主意、搬家、换了偏好时用）
- {{"op": "same", "id": "..."}}（新信息和已有记忆说的是同一件事）
没有值得记的就输出 {{"ops": []}}。"""

CONSOLIDATE_PROMPT = """你是长期记忆整理员。下面是用户的一批长期记忆（带 id），有些重复、互相重叠或已经过时。
把说的是同一件事的合并成一条（保留最新、最具体的说法，数字、名字照原样），明显是一次性小事、已经没用的标记删除。
画像（profile）最后最多留 {cap} 条。拿不准的不要动。不要改写没问题的条目。
输出一行 JSON：{{"ops": [...]}}，op 只能是：
- {{"op": "merge", "ids": ["要合并的 id", "..."], "kind": "profile|fact|decision", "text": "合并后的一句话，第三人称"}}
- {{"op": "drop", "ids": ["..."]}}
没有要整理的就输出 {{"ops": []}}。"""


def looks_secret(text: str) -> bool:
    t = text or ""
    if SECRET_RE.search(t):
        return True
    # a bare code-like string: 4-8 digits next to 码 / code, or a long random token
    if re.search(r"(码|code)\D{0,6}\d{4,8}\b", t, re.I) or re.search(r"\b\d{4,8}\D{0,4}(是)?(验证|校验)", t):
        return True
    if re.search(r"\b(?=[A-Za-z0-9_\-]{24,}\b)(?=\S*\d)(?=\S*[A-Za-z])[A-Za-z0-9_\-]+\b", t):
        return True
    return False


def norm(text: str) -> str:
    return re.sub(r"[\s，。,.!！?？：:；;、\"'“”‘’()（）\[\]【】]+", "", (text or "").lower())


def terms(text: str) -> set[str]:
    """Keyword terms: latin words / numbers and CJK bigrams (single CJK chars for one-character runs)."""
    t = (text or "").lower()
    out = set(re.findall(r"[a-z0-9][a-z0-9_.\-]{1,}", t))
    for run in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(run) == 1:
            out.add(run)
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out - STOP


STOP = {"用户", "一个", "这个", "那个", "什么", "我们", "你们", "他们", "就是", "还是", "以后", "现在", "可以", "需要", "的是"}


def similarity(a: str, b: str) -> float:
    ta, tb = terms(a), terms(b)
    if not ta or not tb:
        return 1.0 if norm(a) == norm(b) and norm(a) else 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def cosine(u: list, v: list) -> float:
    if not u or not v or len(u) != len(v):
        return 0.0
    dot = sum(x * y for x, y in zip(u, v))
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


def guess_kind(text: str) -> str:
    t = text or ""
    if re.search(r"^(我|用户)?(叫|名字|是|住|在|今年|的生日|生日|老家|工作|做|职业|家里|老婆|老公|孩子|女儿|儿子)|"
                 r"称呼|叫我|我住|我在.{1,8}(工作|上班|上学)|我是", t):
        return "profile"
    if re.search(r"以后|今后|一律|总是|永远|不要再|别再|只要|必须|决定|规则|都用|统一", t):
        return "decision"
    return "fact"


def parse_ops(text: str) -> list[dict]:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    ops = d.get("ops") if isinstance(d, dict) else None
    return [o for o in ops if isinstance(o, dict)] if isinstance(ops, list) else []


def ensure_tables(store) -> None:
    cols = {r["name"] for r in store._x("PRAGMA table_info(memories)")}
    for col, typ in (("kind", "TEXT DEFAULT 'fact'"), ("superseded", "TEXT DEFAULT ''"), ("updated_at", "REAL"),
                     ("embedding", "TEXT"), ("hits", "INTEGER DEFAULT 0")):
        if col not in cols:
            store._x(f"ALTER TABLE memories ADD COLUMN {col} {typ}")
    store._x("UPDATE memories SET kind='fact' WHERE kind IS NULL OR kind=''")
    store._x("UPDATE memories SET superseded='' WHERE superseded IS NULL")


class Memory:
    def __init__(self, store, sessions=None, llm: Optional[Callable[[str], Any]] = None):
        self.store, self.sessions = store, sessions
        self.llm = llm                           # role -> router (chat.Chat.llm): "small" learns
        self.embedder: Optional[Callable] = None  # tests: async (list[str]) -> list[list[float]] | None
        self._qvec: dict[str, list] = {}         # query -> vector, filled by prepare()
        self._learning = asyncio.Lock()
        ensure_tables(store)

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _out(r: dict) -> dict:
        d = dict(r)
        d.pop("embedding", None)
        d["superseded"] = d.get("superseded") or ""
        d["kind"] = d.get("kind") or "fact"
        return d

    def items(self, include_superseded: bool = False, kind: Optional[str] = None) -> list[dict]:
        where, args = ["1=1"], []
        if not include_superseded:
            where.append("(superseded IS NULL OR superseded='')")
        if kind:
            where.append("kind=?"); args.append(kind)
        rows = self.store._x(f"SELECT * FROM memories WHERE {' AND '.join(where)} "
                             "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 500", tuple(args))
        return [self._out(r) for r in rows]

    def active_rows(self) -> list[dict]:
        return self.store._x("SELECT * FROM memories WHERE superseded IS NULL OR superseded='' "
                             "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 1000")

    def get(self, mid: str) -> Optional[dict]:
        r = self.store._x("SELECT * FROM memories WHERE id=?", (mid,))
        return self._out(r[0]) if r else None

    def duplicate_of(self, text: str, kind: Optional[str] = None) -> Optional[dict]:
        n = norm(text)
        best, score = None, 0.0
        for r in self.active_rows():
            if norm(r["text"]) == n:
                return r
            s = similarity(text, r["text"])
            if s > score:
                best, score = r, s
        # near-identical wording (one is a rephrase of the other); different numbers mean different facts
        if best is not None and score >= 0.85 and abs(len(norm(best["text"])) - len(n)) <= max(6, len(n) // 3) \
                and re.findall(r"\d+", best["text"]) == re.findall(r"\d+", text):
            return best
        return None

    def add(self, text: str, kind: Optional[str] = None, source: str = "user", supersedes: Optional[str] = None) -> Optional[dict]:
        """Store one memory; None when it is empty or looks like a secret. A duplicate refreshes the old row."""
        text = re.sub(r"\s+", " ", (text or "").strip())[:MAX_TEXT]
        if not text or looks_secret(text):
            return None
        kind = kind if kind in KINDS else guess_kind(text)
        now = time.time()
        dup = self.duplicate_of(text, kind)
        if dup and dup["id"] != supersedes:
            self.store._x("UPDATE memories SET updated_at=?, hits=COALESCE(hits,0)+1 WHERE id=?", (now, dup["id"]))
            return self.get(dup["id"])
        mid = f"mem-{uuid.uuid4().hex[:10]}"
        self.store._x("INSERT INTO memories (id, text, source, created_at, kind, superseded, updated_at, hits) "
                      "VALUES (?,?,?,?,?,?,?,0)", (mid, text, source, now, kind, "", now))
        if supersedes:
            self.store._x("UPDATE memories SET superseded=?, updated_at=? WHERE id=? AND id<>?", (mid, now, supersedes, mid))
        self._embed_later([mid])
        return self.get(mid)

    def edit(self, mid: str, text: Optional[str] = None, kind: Optional[str] = None) -> Optional[dict]:
        m = self.get(mid)
        if not m:
            return None
        sets, args = ["updated_at=?"], [time.time()]
        if text is not None:
            text = re.sub(r"\s+", " ", text.strip())[:MAX_TEXT]
            if not text:
                raise ValueError("记忆不能是空的")
            if looks_secret(text):
                raise ValueError("密码、验证码、token 这类内容不会存进记忆")
            sets += ["text=?", "embedding=NULL"]; args.append(text)
        if kind is not None:
            if kind not in KINDS:
                raise ValueError("kind 只能是 profile / fact / decision")
            sets.append("kind=?"); args.append(kind)
        self.store._x(f"UPDATE memories SET {','.join(sets)} WHERE id=?", (*args, mid))
        if text is not None:
            self._embed_later([mid])
        return self.get(mid)

    def delete(self, mid: str) -> bool:
        had = bool(self.get(mid))
        self.store._x("DELETE FROM memories WHERE id=?", (mid,))
        self.store._x("UPDATE memories SET superseded='' WHERE superseded=?", (mid,))   # its predecessor is not revived
        return had

    def restore(self, mid: str) -> Optional[dict]:
        self.store._x("UPDATE memories SET superseded='', updated_at=? WHERE id=?", (time.time(), mid))
        return self.get(mid)

    # ------------------------------------------------------------------ embeddings (optional)
    def _embed_cfg(self) -> Optional[dict]:
        s = self.sessions
        if not s:
            return None
        try:
            m = s.model_settings()
        except Exception:  # noqa: BLE001
            return None
        model = (m.get("embed_model") or "").strip()
        key = s.secrets.get("openai_api_key") if getattr(s, "secrets", None) else None
        if not model or not key:
            return None
        return {"model": model, "key": key, "base": (m.get("base_url") or "https://api.openai.com/v1").rstrip("/")}

    async def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        if self.embedder:
            return await self.embedder(texts)
        cfg = self._embed_cfg()
        if not cfg or not texts:
            return None
        import httpx
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(cfg["base"] + "/embeddings", headers={"Authorization": f"Bearer {cfg['key']}"},
                                 json={"model": cfg["model"], "input": texts})
            r.raise_for_status()
            data = sorted(r.json().get("data") or [], key=lambda d: d.get("index", 0))
            vecs = [d.get("embedding") for d in data]
            return vecs if len(vecs) == len(texts) and all(vecs) else None
        except Exception as e:  # noqa: BLE001  keyword search still works
            log.info("embedding unavailable: %s", str(e)[:160])
            return None

    def _embed_later(self, ids: list[str]):
        if not (self.embedder or self._embed_cfg()):
            return
        try:
            asyncio.get_running_loop().create_task(self.embed_rows(ids))
        except RuntimeError:
            pass

    async def embed_rows(self, ids: Optional[list[str]] = None) -> int:
        rows = [r for r in self.active_rows() if (ids is None or r["id"] in ids) and not r.get("embedding")]
        if not rows:
            return 0
        vecs = await self.embed([r["text"] for r in rows])
        if not vecs:
            return 0
        for r, v in zip(rows, vecs):
            self.store._x("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(v), r["id"]))
        return len(rows)

    async def prepare(self, query: str) -> None:
        """Embed the query (and any rows without a vector) before a sync prompt_block call; no-op without a model."""
        q = (query or "").strip()[:500]
        if not q or q in self._qvec or not (self.embedder or self._embed_cfg()):
            return
        try:
            await asyncio.wait_for(self.embed_rows(), 20)
            v = await asyncio.wait_for(self.embed([q]), 15)
            if v:
                if len(self._qvec) > 64:
                    self._qvec.clear()
                self._qvec[q] = v[0]
        except Exception as e:  # noqa: BLE001
            log.info("memory prepare: %s", e)

    # ------------------------------------------------------------------ search / prompt
    def search(self, query: str = "", k: int = TOP_K, kinds: tuple = ("fact", "decision")) -> list[dict]:
        rows = [r for r in self.active_rows() if (r.get("kind") or "fact") in kinds]
        if not rows:
            return []
        q = (query or "").strip()[:500]
        qv = self._qvec.get(q)
        now = time.time()
        scored = []
        for i, r in enumerate(rows):
            age_days = (now - (r.get("updated_at") or r.get("created_at") or now)) / 86400
            recency = 0.15 / (1 + age_days / 30)
            if q:
                rel = 0.0
                if qv and r.get("embedding"):
                    try:
                        rel = cosine(qv, json.loads(r["embedding"]))
                    except ValueError:
                        rel = 0.0
                else:
                    rel = similarity(q, r["text"])
                    if r["text"] and norm(r["text"]) and norm(r["text"]) in norm(q):
                        rel = max(rel, 0.9)
            else:
                rel = 0.0
            boost = 0.05 if r.get("kind") == "decision" else 0.0
            scored.append((rel + recency + boost + min(r.get("hits") or 0, 5) * 0.01, -i, r))
        scored.sort(reverse=True)
        return [self._out(r) for _, _, r in scored[:k]]

    def profile_lines(self) -> list[str]:
        """v3.9.14 frozen snapshot: oldest first and capped, so the profile text only changes when a profile memory is
        added, edited or replaced (a 'same' that bumps updated_at no longer reorders it). The chat puts it in the
        stable head of the system prompt, where the provider's prefix cache keeps hitting."""
        prof = self.store.kv_get("profile", {}) or {}
        out = []
        if prof.get("name"):
            out.append(f"称呼：{prof['name']}")
        if prof.get("about"):
            out.append(f"关于用户：{prof['about']}")
        rows = sorted(self.items(kind="profile"), key=lambda m: (m.get("created_at") or 0, m["id"]))
        rows = rows[-PROFILE_CAP:]
        total = sum(len(x) for x in out)
        for m in rows:
            if total + len(m["text"]) > PROFILE_CHARS:
                break
            out.append(m["text"])
            total += len(m["text"])
        return out

    def profile_block(self) -> str:
        prof = self.profile_lines()
        if not prof:
            return ""
        return "关于用户（长期画像；与用户最新的话冲突时以最新的话为准）：" + "；".join(prof)

    def facts_block(self, query: str = "", k: int = TOP_K) -> str:
        mems = self.search(query, k)
        if not mems:
            return ""
        lines = [OPEN, "和这句话相关的长期记忆（用户说过或确认过的；与用户最新的话冲突时以最新的话为准）："]
        lines += [f"- [{KIND_CN.get(m['kind'], '事实')}] {m['text']}" for m in mems]
        lines.append(CLOSE)
        return "\n".join(lines)

    def prompt_block(self, query: str = "", k: int = TOP_K) -> str:
        prof = self.profile_lines()
        mems = self.search(query, k)
        if not prof and not mems:
            return ""
        lines = [OPEN, "关于用户的长期记忆（用户亲口说过或确认过的；与用户最新的话冲突时以最新的话为准）："]
        if prof:
            lines.append("画像：" + "；".join(prof))
        for m in mems:
            lines.append(f"- [{KIND_CN.get(m['kind'], '事实')}] {m['text']}")
        lines.append(CLOSE)
        return "\n".join(lines)

    # ------------------------------------------------------------------ consolidation (v3.9.14)
    def needs_consolidation(self) -> bool:
        if (time.time() - float(self.store.kv_get("memory_consolidated_at", 0) or 0)) < 20 * 3600:
            return False
        rows = self.active_rows()
        prof = [r for r in rows if r.get("kind") == "profile"]
        return len(prof) > PROFILE_CAP or len(rows) - len(prof) > FACT_CAP

    async def consolidate(self, force: bool = False) -> list[dict]:
        """Merge overlapping memories so the store stays small and clean: at most once a day, only when the profile is
        over PROFILE_CAP or facts + decisions over FACT_CAP. Merged rows are superseded (kept, never shown)."""
        if not self.llm or (not force and not self.needs_consolidation()):
            return []
        self.store.kv_set("memory_consolidated_at", time.time())
        rows = self.active_rows()
        prof = [r for r in rows if r.get("kind") == "profile"]
        rest = [r for r in rows if r.get("kind") != "profile"]
        pick = prof if len(prof) > PROFILE_CAP else []
        pick += sorted(rest, key=lambda r: (r.get("hits") or 0, r.get("updated_at") or 0))[:80] if len(rest) > FACT_CAP else []
        if not pick:
            return []
        listing = "\n".join(f"{r['id']}: [{r.get('kind') or 'fact'}] {r['text']}" for r in pick)
        try:
            out = await asyncio.wait_for(self.llm("small").complete(CONSOLIDATE_PROMPT.format(cap=PROFILE_CAP), listing, 1200), 120)
        except Exception as e:  # noqa: BLE001
            log.warning("memory consolidate: %s", e)
            return []
        known = {r["id"] for r in pick}
        done = []
        for o in parse_ops(out)[:40]:
            op = str(o.get("op") or "").lower()
            ids = [str(i) for i in (o.get("ids") or []) if str(i) in known]
            if op == "merge" and len(ids) >= 2 and str(o.get("text") or "").strip():
                kinds = {self.get(i)["kind"] for i in ids if self.get(i)}
                kind = o.get("kind") if o.get("kind") in KINDS else (kinds.pop() if len(kinds) == 1 else "fact")
                row = self.add(str(o["text"]), kind, "consolidate", supersedes=ids[0])
                if row:
                    now = time.time()
                    for i in ids[1:]:
                        self.store._x("UPDATE memories SET superseded=?, updated_at=? WHERE id=? AND id<>?", (row["id"], now, i, row["id"]))
                    known -= set(ids)
                    done.append({"op": "merge", "id": row["id"], "old": ids})
            elif op == "drop" and ids:
                now = time.time()
                for i in ids:
                    self.store._x("UPDATE memories SET superseded='dropped', updated_at=? WHERE id=?", (now, i))
                known -= set(ids)
                done.append({"op": "drop", "old": ids})
        return done

    # ------------------------------------------------------------------ chat hooks
    async def command(self, text: str, msg_id: Optional[str] = None) -> Optional[str]:
        """记住… / 忘掉…: act at once and return the reply; None for any other message."""
        t = (text or "").strip()
        m = REMEMBER_RE.match(t)
        if m:
            body = m.group(1).strip().rstrip("。.!！")
            if len(body) < 2:
                return None
            if looks_secret(body):
                return "这类内容（密码、验证码、token、密钥）我不会记下来。需要时请在设置 › 密钥里加密保存。"
            row = self.add(body, None, "user")
            if not row:
                return "这条我没记下来（内容是空的）。"
            for old in self._contradicted_by(row):
                self.store._x("UPDATE memories SET superseded=? WHERE id=?", (row["id"], old["id"]))
            return f"记住了：{row['text']}"
        m = FORGET_RE.match(t)
        if m:
            what = m.group(1).strip().rstrip("。.!！")
            what = re.sub(r"^(关于|有关)|(的事情?|这件事|那件事|相关的?(记忆|内容)?|的记忆|这条|那条)$", "", what).strip() or what
            if not what:
                return None
            hits = self.matching(what)
            if not hits:
                return f"我没有关于「{what[:40]}」的记忆。"
            for h in hits:
                self.delete(h["id"])
            names = "；".join(h["text"] for h in hits[:5])
            return f"已忘掉 {len(hits)} 条：{names}" + ("…" if len(hits) > 5 else "")
        return None

    def matching(self, what: str) -> list[dict]:
        """Memories a 忘掉… names: substring either way, or a strong keyword overlap."""
        n = norm(what)
        out = []
        for r in self.active_rows():
            rn = norm(r["text"])
            if n and (n in rn or (rn in n and len(rn) >= 2)) or similarity(what, r["text"]) >= 0.6:
                out.append(self._out(r))
        return out[:20]

    def _contradicted_by(self, row: dict) -> list[dict]:
        """A user-dictated memory about the same subject replaces the older one ('我住在杭州' after '我住在上海')."""
        out = []
        subj = _subject(row["text"])
        if not subj:
            return out
        for r in self.active_rows():
            if r["id"] != row["id"] and (r.get("kind") or "fact") == row["kind"] and _subject(r["text"]) == subj \
                    and norm(r["text"]) != norm(row["text"]):
                out.append(r)
        return out

    async def learn(self, user: dict, replies: list[dict]) -> list[dict]:
        """Background: ask the small model what to keep from this exchange and apply it. Returns applied ops."""
        text = (user or {}).get("text") or ""
        if len(text.strip()) < 4 or REMEMBER_RE.match(text) or FORGET_RE.match(text) or not self.llm:
            return []
        reply = "\n".join((r.get("text") or "")[:600] for r in replies or [] if r.get("kind") in ("text", "task_result"))
        async with self._learning:
            related = self.search(text, 12, kinds=KINDS)
            existing = "\n".join(f"{m['id']}: [{m['kind']}] {m['text']}" for m in related) or "（无）"
            try:
                out = await asyncio.wait_for(self.llm("small").complete(
                    LEARN_PROMPT.format(existing=existing), f"用户：{text[:1500]}\n助理：{reply[:1200]}", 600), 90)
            except Exception as e:  # noqa: BLE001
                log.warning("memory learn: %s", e)
                return []
            done = self.apply(parse_ops(out), source=(user or {}).get("id") or "chat", known={m["id"] for m in related})
        if self.needs_consolidation():   # v3.9.14: keep the store small (at most once a day, only past the caps)
            try:
                await self.consolidate()
            except Exception as e:  # noqa: BLE001
                log.warning("memory consolidate: %s", e)
        return done

    def apply(self, ops: list[dict], source: str = "chat", known: Optional[set] = None) -> list[dict]:
        done = []
        for o in ops[:8]:
            op = str(o.get("op") or "").lower()
            kind = o.get("kind") if o.get("kind") in KINDS else None
            body = str(o.get("text") or "").strip()
            if op == "same" and o.get("id") and self.get(str(o["id"])):
                self.store._x("UPDATE memories SET updated_at=?, hits=COALESCE(hits,0)+1 WHERE id=?", (time.time(), o["id"]))
                done.append({"op": "same", "id": o["id"]})
            elif op == "replace" and o.get("id") and body:
                old = self.get(str(o["id"]))
                if not old or (known is not None and old["id"] not in known) or old["superseded"]:
                    row = self.add(body, kind, source)
                else:
                    row = self.add(body, kind or old["kind"], source, supersedes=old["id"])
                if row:
                    done.append({"op": "replace", "id": row["id"], "old": o["id"]})
            elif op == "add" and body:
                row = self.add(body, kind, source)
                if row:
                    done.append({"op": "add", "id": row["id"]})
        return done


def _subject(text: str) -> str:
    """A crude subject key for user-dictated replacements: '我住在X' / '我叫X' / '我的X是Y'."""
    t = re.sub(r"^(用户|我)", "", (text or "").strip())
    for pat in (r"^(住在|住|现在住在|家在)", r"^(叫|名字是|的名字是)", r"^(在)(.{1,10})(工作|上班)", r"^的(.{1,8})(是|为)",
                r"^(生日是|的生日是)", r"^(喜欢喝|爱喝)", r"^(喜欢吃|爱吃)"):
        m = re.match(pat, t)
        if m:
            return pat if "(.{1,8})" not in pat else pat + m.group(1)
    return ""


# ---------------------------------------------------------------------------------------------- HTTP
from pydantic import BaseModel  # noqa: E402


class MemIn(BaseModel):
    text: str
    kind: Optional[str] = None


class MemPatch(BaseModel):
    text: Optional[str] = None
    kind: Optional[str] = None


def mount(app, mem: Memory, auth) -> None:
    from fastapi import Depends, HTTPException

    dep = [Depends(auth)]

    @app.get("/api/memories", dependencies=dep)
    async def memories(all: bool = False, kind: Optional[str] = None, q: Optional[str] = None):
        if q:
            await mem.prepare(q)
            return mem.search(q, 20, kinds=KINDS)
        return mem.items(include_superseded=all, kind=kind)

    @app.post("/api/memories", dependencies=dep)
    async def add_memory(body: MemIn):
        if looks_secret(body.text):
            raise HTTPException(400, "密码、验证码、token 这类内容不会存进记忆")
        if body.kind and body.kind not in KINDS:
            raise HTTPException(400, "kind 只能是 profile / fact / decision")
        row = mem.add(body.text, body.kind, "user")
        if not row:
            raise HTTPException(400, "记忆不能是空的")
        return row

    @app.put("/api/memories/{mid}", dependencies=dep)
    async def put_memory(mid: str, body: MemPatch):
        try:
            row = mem.edit(mid, body.text, body.kind)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not row:
            raise HTTPException(404)
        return row

    @app.delete("/api/memories/{mid}", dependencies=dep)
    async def del_memory(mid: str):
        if not mem.delete(mid):
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/memories/{mid}/restore", dependencies=dep)
    async def restore_memory(mid: str):
        row = mem.restore(mid)
        if not row:
            raise HTTPException(404)
        return row
