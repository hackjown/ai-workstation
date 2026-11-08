"""v3.9.17 已确认事实板 (findings board).

What a step established is kept here and put in front of the model EVERY turn (it rides in the task text, which is
never trimmed or compressed, unlike the step history). An entry can only be overturned with evidence:
note_finding(replaces="F3", evidence="…") — without evidence the change is refused and the old fact stays.

Facts come from two places:
* the model: the note_finding tool (loop-handled, costs no tool round trip on the computer);
* the model's own words: a thought that states a conclusion ("已查清…", "主因是…", "发现…") is captured as an
  automatic finding, so a model that never calls note_finding still builds the board (task-833 stated the cause
  at step 8 and then repeated it six times without delivering).

v3.9.26 (task-830): a dev_fix task takes facts only from tool results. Its model kept "confirming" the same guess
about the HTML (steps 1-21) and the board filled with its own speculation. FindingsBoard(capture_thoughts=False)
ignores thoughts; from_result() records what a tool actually returned (a failing command's error, a verify_html
result) with source "result".

v3.9.29 (task-68b): facts can go stale. "源码共 303 行" stayed on the board after the source grew to 639 lines.
* a new fact with the same wording but different numbers supersedes the old one (it is not a "repeat");
* expire(pred) / expire_about(path) retire facts that a change made stale (the loop calls expire_about() after every
  source write: facts naming that file, or the source's size/line count, are dropped from the board).

Pure, tested in tests/test_v3917_findings.py, tests/test_v3926_devflow.py and tests/test_v3929_devflow_fixes.py.
"""
from __future__ import annotations

import re
from typing import Optional

# a thought that says it knows something
SUFFICIENT = re.compile(r"已(经)?(基本)?查(清|明|到)|基本查清|已(经)?掌握|关键(线索|疑点|证据)(已经|已)|已(经)?(定位|确认|找到|拿到大量|拿到关键)|"
                      r"主因|根因|根本原因|原因(是|在于|就是)|结论(是|：|:)|可以(确定|断定)|现状已|已足够|信息已足够|足以说明|"
                      r"\b(root cause|the cause is|confirmed that|found that)\b", re.I)
# a fact, not necessarily the whole answer
CONCLUDE = re.compile(SUFFICIENT.pattern + r"|(我|已)?发现(了)?|证实|确认(是|了)|说明(是|了)|显示(是|出)", re.I)
NEGATED = re.compile(r"(没|未|尚未|还没|没有|不能|无法)(有)?(查清|查明|查到|确认|定位|掌握|找到)")
# the part of a sentence that announces more collecting is not a fact
_MORE = re.compile(r"(，|,|。|；|;|\s)?(再补|再采|再查|再看|再跑|下一步|然后|接下来|还需要|还差|还要|我先补|先补|补一|补充一|"
                   r"next|then I will)", re.I)
_SPLIT = re.compile(r"[。！!？?\n；;]")


def _grams(s: str) -> set:
    s = re.sub(r"[\s\W_]+", "", (s or "").lower())
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or ({s} if s else set())


def similarity(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def conclusions(thought: str) -> list[str]:
    """Sentences of a thought that state a conclusion, without their "再补一组数据" tail."""
    out = []
    t = re.sub(r"\*\*|`", "", thought or "")
    for sent in _SPLIT.split(t):
        s = sent.strip(" ：:-·•*")
        if len(s) < 8 or not CONCLUDE.search(s) or NEGATED.search(s):
            continue
        st = _MORE.match(s)
        if st and st.start() <= 2:          # "再补一次数据：… 用来确定根因" is a plan, not a fact
            continue
        if not re.search(r"[0-9A-Za-z]", s):   # an automatic fact carries a number or a name ("信息已足够" is not one)
            continue
        m = _MORE.search(s, CONCLUDE.search(s).end())
        if m:
            s = s[:m.start()].strip(" ，,")
        if len(s) >= 8:
            out.append(s[:240])
    return out


def says_sufficient(thought: str) -> bool:
    """The model itself says it knows enough (已查清 / 基本查清 / 已掌握 / 关键线索已经 / 信息已足够)."""
    t = thought or ""
    for m in SUFFICIENT.finditer(t):
        pre = t[max(0, m.start() - 4):m.end() + 2]
        if not NEGATED.search(pre):
            return True
    return False


MISSING = re.compile(r"(还)?(缺|差|需要|要补|补)[^。；;\n]{0,6}(一|两|几|最后)?(组|块|项|个|条|次)?(数据|证据|信息|关键)|缺少|还差|missing|lack", re.I)


def names_missing(thought: str) -> bool:
    return bool(MISSING.search(thought or ""))


class FindingsBoard:
    DUP = 0.55

    def __init__(self, items: Optional[list] = None, capture_thoughts: bool = True):
        self.items: list[dict] = [dict(i) for i in (items or [])]
        self.capture_thoughts = capture_thoughts   # v3.9.26: False = facts only from tool results

    # ------------------------------------------------------------ write
    def _next_id(self) -> str:
        return f"F{len(self.items) + 1}"

    def live(self) -> list[dict]:
        return [i for i in self.items if not i.get("replaced_by") and not i.get("expired")]

    def expire(self, pred, step: int = 0, reason: str = "") -> list[str]:
        """Retire live facts for which pred(entry) is true; -> their ids."""
        out = []
        for i in self.live():
            try:
                hit = pred(i)
            except Exception:  # noqa: BLE001
                hit = False
            if hit:
                i["expired"], i["expired_step"], i["expired_reason"] = True, step, reason[:120]
                out.append(i["id"])
        return out

    def expire_about(self, path: str, step: int = 0) -> list[str]:
        """v3.9.29: after `path` changed, facts naming it, or giving the source's size / line count, are stale."""
        import os as _os
        name = _os.path.basename(str(path or "")).lower()
        size = re.compile(r"(源码|源文件|源头|脚本|source|文件)[^。；;\n]{0,10}\d+\s*(行|lines?|KB|字节|bytes|个函数)", re.I)
        return self.expire(lambda i: (name and name in i["text"].lower()) or bool(size.search(i["text"])),
                           step, f"{name or 'source'} changed")

    def find(self, fid: str) -> Optional[dict]:
        fid = str(fid or "").strip().upper()
        return next((i for i in self.items if i["id"] == fid), None)

    def add(self, text: str, step: int, source: str = "model", replaces: Optional[str] = None,
            evidence: str = "") -> tuple[str, Optional[dict]]:
        """('added'|'dup'|'replaced'|'refused'|'empty', entry). A duplicate bumps the entry's repeat count."""
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:300]
        if len(text) < 4:
            return "empty", None
        if replaces:
            old = self.find(replaces)
            if old is None:
                return "refused", None
            ev = str(evidence or "").strip()
            if len(ev) < 6:
                return "refused", old
            new = {"id": self._next_id(), "text": text, "step": step, "source": source, "evidence": ev[:300],
                   "repeats": 0, "replaces": old["id"]}
            old["replaced_by"] = new["id"]
            self.items.append(new)
            return "replaced", new
        nums = re.findall(r"\d+(?:\.\d+)?", text)
        for i in self.live():   # v3.9.29: same statement, other numbers -> the new reading supersedes the old one
            old_nums = re.findall(r"\d+(?:\.\d+)?", i["text"])
            if nums and old_nums and nums != old_nums and \
                    similarity(re.sub(r"\d+(?:\.\d+)?", "#", i["text"]), re.sub(r"\d+(?:\.\d+)?", "#", text)) >= 0.75:
                new = {"id": self._next_id(), "text": text, "step": step, "source": source, "repeats": 0,
                       "replaces": i["id"], "evidence": str(evidence or "")[:300] or "newer reading"}
                i["replaced_by"], i["superseded"] = new["id"], True
                self.items.append(new)
                return "replaced", new
        for i in self.live():
            if similarity(i["text"], text) >= self.DUP or (len(text) > 12 and text in i["text"]):
                i["repeats"] = int(i.get("repeats") or 0) + 1
                i["last_step"] = step
                if source == "model" and i.get("source") == "auto":   # the model confirmed what was captured
                    i["source"] = "model"
                return "dup", i
        e = {"id": self._next_id(), "text": text, "step": step, "source": source, "repeats": 0}
        if evidence:
            e["evidence"] = str(evidence)[:300]
        self.items.append(e)
        return "added", e

    def capture(self, thought: str, step: int) -> tuple[int, int]:
        """Auto findings from a thought: (new facts, repeats of known ones)."""
        new = rep = 0
        if not self.capture_thoughts:
            return 0, 0
        for c in conclusions(thought):
            st, _ = self.add(c, step, "auto")
            new += st == "added"
            rep += st == "dup"
        return new, rep

    def from_result(self, tool: str, args: dict, ok: bool, text: str, step: int) -> tuple[str, Optional[dict]]:
        """v3.9.26: a fact from what a tool returned (never from the model's words). Only results worth keeping:
        a failed command (its first error line) and verify_html outcomes."""
        t = str(text or "")
        if tool == "verify_html":
            return self.add(("页面验证通过：" if ok else "页面验证未通过：") + t[:220], step, "result", evidence="verify_html")
        if ok or tool not in ("shell", "files_read", "files_write"):
            return "empty", None
        hits = [x.strip() for x in t.splitlines() if re.search(r"error|exception|not found|denied|refused|失败|错误|找不到", x, re.I)
                and not re.match(r"\s*Traceback", x)]
        line = hits[-1] if hits else ""   # the last error line is the real one (a traceback ends with it)
        if not line:
            return "empty", None
        cmd = str((args or {}).get("command") or (args or {}).get("path") or "")[:80]
        return self.add(f"{tool} 失败（{cmd}）：{line[:180]}", step, "result", evidence=line[:200])

    def max_repeats(self) -> int:
        return max([int(i.get("repeats") or 0) for i in self.live()] or [0])

    # ------------------------------------------------------------ read
    def block(self, limit: int = 2400) -> str:
        live = self.live()
        if not live:
            return ""
        lines = []
        for i in live:
            tag = "" if i.get("source") == "model" else "（工具结果）" if i.get("source") == "result" else "（自动记录）"
            ev = f" 证据：{i['evidence']}" if i.get("evidence") and i.get("replaces") else ""
            lines.append(f"  {i['id']} [第{i['step']}步]{tag} {i['text']}{ev}")
        body = "\n".join(lines)
        if len(body) > limit:
            body = "…\n" + body[-limit:]
        return ("\n\nCONFIRMED FINDINGS (已确认事实板: kept every turn, never compressed). Build on them, do not collect "
                "them again. To overturn one, call note_finding with replaces=<id> AND the evidence that disproves it:\n" + body)

    def report(self) -> str:
        live = self.live()
        if not live:
            return ""
        return "\n".join(f"{n}. {i['text']}" for n, i in enumerate(live, 1))

    def to_list(self) -> list[dict]:
        return [dict(i) for i in self.items]
