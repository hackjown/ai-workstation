"""BrowserOS neo tools (bos_*): how a call reaches the computer, and what it means to the safety gates.

A bos_act names an element only by its ref ("e12"). The policy and the task gates (price-watch order, 小红书运营) judge
clicks and typing by the element's TEXT, so before a gated call the ref is resolved to its snapshot line with
BrowserOS's grep (`[ref=e12]` -> `button "提交订单" [ref=e12]`), and the call is translated into the classic tool it
amounts to (browser_click / browser_fill / type / keypress / browser_navigate). The gates then run their usual checks
on that, so a bos_* call can never do what the same click or typing through the old tools would be refused.
"""
from __future__ import annotations

import re
from typing import Optional

# AIW tool -> BrowserOS MCP tool ("front" is the agent's own: bring a page to the front of the window)
MCP = {"bos_tabs": "tabs", "bos_navigate": "navigate", "bos_snapshot": "snapshot", "bos_act": "act", "bos_read": "read",
       "bos_grep": "grep", "bos_diff": "diff", "bos_wait": "wait", "bos_upload": "upload"}
READ_ONLY = {"bos_snapshot", "bos_read", "bos_grep", "bos_diff", "bos_wait"}
CLICK_KINDS = {"click", "check", "uncheck", "select", "download"}
COORD_KINDS = {"click_at", "type_at", "hover_at", "drag_at"}
_LINE = re.compile(r'^\s*-?\s*(?P<role>[\w-]+)?\s*(?P<name>"(?:[^"\\]|\\.)*")?(?P<rest>.*?)\[ref=(?P<ref>e\d+)\]', re.M)
CATEGORY = {"watch_order": "shopping", "watch": "shopping", "xhs": "social-media"}


def is_bos(name: str) -> bool:
    return name in MCP


def mcp_call(name: str, args: dict) -> tuple[str, dict]:
    """(MCP tool, its arguments) for an AIW bos_* call, with AIW's token-saving defaults."""
    a = {k: v for k, v in (args or {}).items() if v is not None and not str(k).startswith("_")}
    tool = MCP[name]
    if name == "bos_tabs" and a.get("action") == "front":
        return "front", {"page": a.get("page")}
    if name == "bos_snapshot":
        a.setdefault("mode", "interactive")
    if name == "bos_navigate":
        a.setdefault("action", "url")
    if name == "bos_act":
        d = a.get("diff")
        if d in (None, ""):
            a["diff"] = 3000.0        # capped readback: a full diff of a store page is tens of thousands of chars
        elif isinstance(d, str) and re.fullmatch(r"\d+(\.\d+)?", d):
            a["diff"] = float(d)
    if name == "bos_upload" and isinstance(a.get("files"), str):
        a["files"] = [a["files"]]
    if name == "bos_grep":
        a.setdefault("over", "ax")
    return tool, a


def refs_of(args: dict) -> list[str]:
    a = args or {}
    out = [str(a["ref"])] if a.get("ref") else []
    for f in a.get("fields") or []:
        if isinstance(f, dict) and f.get("ref"):
            out.append(str(f["ref"]))
    return out


def parse_labels(text: str, with_role: bool = False) -> dict[str, str]:
    """ref -> what the element says, from snapshot/grep lines: `button "提交订单" [ref=e12]` -> '提交订单'
    (with_role: 'button 提交订单'). The gates match words like 发送/提交订单 against the accessible name, so the role
    stays out of the gate label (a 'button 发送' would no longer be a bare 发送)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        for m in re.finditer(r"\[ref=(e\d+)\]", line):
            ref = m.group(1)
            head = line[:m.start()].strip().lstrip("-").strip()
            names = re.findall(r'"((?:[^"\\]|\\.)*)"', head)
            role = head.split('"')[0].strip().rstrip(":").strip()
            tail = line[m.end():]
            val = re.findall(r':\s*"((?:[^"\\]|\\.)*)"', tail)
            name = " ".join(x for x in [" ".join(names), " ".join(val)] if x).strip()
            label = (f"{role} {name}".strip() if with_role else name) or role or head
            out.setdefault(ref, label)
    return out


async def resolve(computer, key: Optional[str], page, refs: list[str], timeout: float = 30) -> dict[str, str]:
    """Labels for refs on a page, read through BrowserOS grep (the same refs as the latest snapshot)."""
    if not refs or page is None:
        return {}
    pat = r"\[ref=(" + "|".join(re.escape(r) for r in refs) + r")\]"
    r = await computer.execute("browseros.call", {"tool": "grep", "key": key or "default",
                                                  "args": {"page": page, "over": "ax", "pattern": pat, "limit": 20}},
                               observe=False, timeout=timeout)
    if not getattr(r, "success", False):
        return {}
    text = (getattr(r, "result", None) or {}).get("text") or ""
    names, full = parse_labels(text), parse_labels(text, with_role=True)
    return {k: v for k, v in names.items()} | {f"#{k}": v for k, v in full.items()}


async def page_url(computer, key: Optional[str], page) -> str:
    """The url of one of the task's BrowserOS pages (from tabs list)."""
    r = await computer.execute("browseros.call", {"tool": "tabs", "key": key or "default", "args": {"action": "list"}},
                               observe=False, timeout=30)
    text = (getattr(r, "result", None) or {}).get("text") or ""
    for m in re.finditer(r"^\[(\d+)\]\s+(\S+)", text, re.M):
        if page is not None and str(m.group(1)) == str(page):
            return m.group(2)
    return ""


async def annotate(executor, name: str, args: dict) -> dict:
    """Add _labels {ref: label} (and _label for the main ref) so policy and gates can read what a ref is."""
    if name not in ("bos_act", "bos_upload") or "_labels" in (args or {}):
        return args
    refs = refs_of(args)
    if not refs:
        return {**args, "_labels": {}}
    try:
        labels = await resolve(executor.computer, getattr(executor, "bos_key", None), args.get("page"), refs)
    except Exception:  # noqa: BLE001
        labels = {}
    names = {k: v for k, v in labels.items() if not k.startswith("#")}
    out = {**args, "_labels": names}
    if args.get("ref"):
        out["_label"] = labels.get("#" + str(args["ref"])) or names.get(str(args["ref"]), "")
    return out


def _key(k: str) -> str:
    k = (k or "").strip()
    parts = [p.strip().lower() for p in re.split(r"\+", k) if p.strip()]
    alias = {"control": "ctrl", "meta": "super", "cmd": "super", "command": "super", "option": "alt", "esc": "escape"}
    return "+".join(alias.get(p, p) for p in parts)


def classic(name: str, args: dict) -> list[tuple[str, dict]]:
    """What this bos_* call amounts to in the classic tools, for the gates. [] = nothing a gate cares about."""
    a = args or {}
    labels = a.get("_labels") or {}
    if name == "bos_navigate":
        act = a.get("action") or "url"
        if act == "url":
            return [("browser_navigate", {"url": str(a.get("url") or "")})]
        return [("browser_back" if act == "back" else "browser_forward" if act == "forward" else "browser_navigate",
                 {} if act in ("back", "forward") else {"url": ""})]
    if name == "bos_tabs":
        if a.get("action") == "new" and a.get("url"):
            return [("browser_open", {"url": str(a["url"])})]
        return []
    if name == "bos_upload":
        return [("browser_fill", {"field": "file", "text": f}) for f in (a.get("files") or [])]
    if name != "bos_act":
        return []
    kind = str(a.get("kind") or "")
    label = labels.get(str(a.get("ref") or ""), "") if a.get("ref") else ""
    if kind in CLICK_KINDS:
        text = label + (f" {a.get('value')}" if kind == "select" and a.get("value") else "")
        return [("browser_click", {"text": text.strip()})]
    if kind == "fill":
        if a.get("fields"):
            return [("browser_fill", {"field": labels.get(str(f.get("ref")), ""), "text": str(f.get("value") or "")})
                    for f in a["fields"] if isinstance(f, dict)]
        return [("browser_fill", {"field": label, "text": str(a.get("value") or "")})]
    if kind in ("type", "type_at"):
        return [("type", {"text": str(a.get("text") or a.get("value") or "")})]
    if kind == "press":
        return [("keypress", {"key": _key(str(a.get("key") or ""))})]
    if kind == "click_at":
        return [("click", {"x": a.get("x"), "y": a.get("y"), "_viewport": True})]
    if kind == "dialog_accept":
        return [("browser_click", {"text": "确定 (dialog_accept)"})]
    return []


def unresolved(args: dict) -> list[str]:
    """Refs the gate could not put a label on (stale ref or a grep that failed)."""
    labels = (args or {}).get("_labels") or {}
    return [r for r in refs_of(args) if not labels.get(r)]


GateFn = "Callable[[str, dict], Awaitable[Optional[str]]]"
REF_KINDS = CLICK_KINDS | {"fill"}


async def gate_bos(executor, name: str, args: dict, gate_fn, approved: bool) -> tuple[Optional[str], dict]:
    """Run a task gate on a bos_* call. Returns (refusal or None, args with _labels for the executor).
    Refused outright in a gated task: coordinate actions (nothing to read the target from), refs that resolve to
    no element (stale or made up), and accepting a page dialog before the user approved (a confirm() popup can be the
    order/publish confirmation itself). Everything else is judged as the classic call it amounts to."""
    a = dict(args or {})
    if name not in MCP or name in READ_ONLY:
        return None, a
    if name == "bos_act":
        kind = str(a.get("kind") or "")
        if kind in COORD_KINDS or (kind == "drag" and not a.get("targetRef")):
            return ("这个任务里不能按坐标操作 BrowserOS 页面：先 bos_snapshot / bos_grep 找到元素的 ref，再用 bos_act 按 ref 操作。"), a
        if kind == "dialog_accept" and not approved:
            return "用户确认之前不能接受页面弹窗（dialog_accept）；用 dialog_dismiss 关掉，或先走确认流程。", a
    a = await annotate(executor, name, a)
    if name == "bos_act" and str(a.get("kind") or "") in REF_KINDS:
        bad = unresolved(a)
        if bad:
            return (f"找不到元素 {', '.join(bad)}（ref 已过期或不存在）。先重新 bos_snapshot（或 bos_grep）拿到新的 ref 再操作。"), a
    for cname, cargs in classic(name, a):
        why = await gate_fn(cname, cargs)
        if why:
            return why, a
    return None, a


def gate_text(name: str, args: dict) -> str:
    """The clicked text of a bos_* call (for 'is this a payment click' checks)."""
    return " ".join(str(c[1].get("text") or "") for c in classic(name, args) if c[0] == "browser_click")
