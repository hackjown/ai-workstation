"""v3.9.28 手机快通道 (phone fast path).

A request that only needs the phone (提醒事项 / 日历 / 通讯录 / 打电话 / 短信 / 闹钟 / 快捷指令 / 位置 / 健康) skips the
chat's LLM router, skips roles.pick_live and never wakes or holds a cloud computer: the task runs right away (not
queued behind a busy computer) as a small in-process tool loop with only the device_* tools, ask_user and
escalate_to_computer. Write tools go through the task's normal confirm card (Decision L3). escalate_to_computer hands
the same task to the normal computer path with what the phone already returned appended to its context.

  AIW_PHONE_FAST=0          turn the fast path off (everything goes the old way)
  AIW_DEVICE_FAST_TTL=8     seconds a fast-path device call waits for the app (a wake push is sent first)
  AIW_PHONE_FAST_ROUNDS=8   tool rounds before the fast loop gives up and escalates
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("aiw.phone_fast")

# kind -> pattern (Chinese + English). Order matters only for the reported kind.
KINDS: list[tuple[str, re.Pattern]] = [
    ("reminders", re.compile(r"提醒事项|提醒我|提醒|待办|\breminders?\b|\bremind me\b|\bto-?do\b", re.I)),
    ("sms", re.compile(r"短信|发信息给|发条信息|\bsms\b|\btext (message|him|her|them)\b|\bsend (a )?text\b", re.I)),
    ("call", re.compile(r"打电话|打个电话|拨打|拨个号|致电|给.{1,12}打(个)?电话|\bcall\b|\bphone (him|her|them|\w+)\b|\bdial\b", re.I)),
    ("calendar", re.compile(r"日历|日程|行程安排|今天有什么安排|明天有什么安排|会议安排|\bcalendar\b|\bschedule\b|\bagenda\b|\bappointments?\b", re.I)),
    ("contacts", re.compile(r"通讯录|联系人|电话号码|手机号|\bcontacts?\b|\bphone number\b", re.I)),
    ("alarm", re.compile(r"闹钟|叫我起床|\balarm\b|\bwake me\b", re.I)),
    ("shortcut", re.compile(r"快捷指令|\bshortcuts?\b", re.I)),
    ("location", re.compile(r"我在哪|我的位置|当前位置|定位|\bwhere am i\b|\bmy location\b|\bcurrent location\b", re.I)),
    ("health", re.compile(r"健康|步数|睡眠|心率|喝水|饮水|体重|运动记录|\bhealth\b|\bsteps\b|\bsleep\b|\bheart rate\b|\bworkouts?\b", re.I)),
]

# anything that needs a real computer: files, the browser, code, the web, attachments
VM_RE = re.compile(
    r"文件|文档|附件|表格|excel|word|ppt|pdf|截图|图片|照片|视频|浏览器|网页|网站|网址|链接|打开.{0,6}(网|站)|搜索|搜一下|上网|百度|谷歌|"
    r"代码|脚本|编程|程序|部署|服务器|终端|命令行|git|github|下载|上传|"
    r"https?://|www\.|\bfiles?\b|\bfolder\b|\bdocuments?\b|\battach|\bbrowser\b|\bwebsite\b|\bweb\b|\bsearch\b|\bgoogle\b|"
    r"\bcode\b|\bscript\b|\bpython\b|\bdeploy\b|\bdownload\b|\bupload\b|\bspreadsheet\b|\bscreenshot\b", re.I)


def enabled() -> bool:
    return os.environ.get("AIW_PHONE_FAST", "1") != "0"


def fast_ttl() -> float:
    try:
        return float(os.environ.get("AIW_DEVICE_FAST_TTL", "8"))
    except ValueError:
        return 8.0


def kind_of(text: str) -> Optional[str]:
    for k, rx in KINDS:
        if rx.search(text or ""):
            return k
    return None


def classify(text: str, attachments: Optional[list] = None, hub: Any = None) -> Optional[dict]:
    """{"path": "device", "kind": ...} when the request is phone-only and a phone is online or registered; else None
    (the normal route). Every decision is logged with its path."""
    text = (text or "").strip()
    if not enabled() or not text:
        return None
    k = kind_of(text)
    if not k:
        return None
    if attachments:
        log.info("phone_fast path=vm reason=attachments kind=%s", k)
        return None
    if VM_RE.search(text):
        log.info("phone_fast path=vm reason=vm_words kind=%s", k)
        return None
    if hub is None or not (hub.online or hub.known()):
        log.info("phone_fast path=vm reason=no_device kind=%s", k)
        return None
    log.info("phone_fast path=device kind=%s", k)
    return {"path": "device", "kind": k}


SYSTEM = (
    "你是 aiw 的手机快通道：只能用 device_* 工具在用户手机上完成请求（提醒事项、日历、通讯录、打电话、短信、闹钟、快捷指令、"
    "位置、健康）。写入/拨打/发送类工具会先请用户确认，直接调用即可。信息不够时用 ask_user 问用户（一个问题）。"
    "需要文件、浏览器、网页、代码或电脑上的软件时，调用 escalate_to_computer 并说明原因。"
    "完成后用一两句中文告诉用户结果，不要编造手机没有返回的数据。")

ASK = {"name": "ask_user", "description": "向用户问一个问题（信息不够时）",
       "parameters": {"type": "object", "properties": {"question": {"type": "string"},
                                                       "options": {"type": "array", "items": {"type": "string"}}},
                      "required": ["question"]}}
ESC = {"name": "escalate_to_computer", "description": "这件事手机做不了（需要文件/浏览器/网页/代码/电脑），转到电脑上继续",
       "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}


def results_block(log_items: list[dict]) -> str:
    if not log_items:
        return ""
    lines = ["【手机快通道已完成的步骤】（已在用户手机上执行，结果如下，不要重复执行写入/拨打/发送）"]
    for it in log_items:
        lines.append(f"- {it['tool']} {json.dumps(it.get('args') or {}, ensure_ascii=False)[:300]} -> "
                     f"{json.dumps(it.get('result'), ensure_ascii=False, default=str)[:800]}")
    return "\n".join(lines)


async def run(prompt: str, llm, tools: dict, confirm: Callable[..., Awaitable[bool]],
              ask: Callable[..., Awaitable[Optional[str]]], note: Callable[[str, dict], Awaitable[None]],
              context: str = "", rounds: Optional[int] = None) -> dict:
    """The fast loop. tools: {name: (schema, fn)} device tools only. Returns
    {"status": "succeeded"|"failed"|"escalate", "result": str, "reason": str, "log": [{tool,args,result}]}."""
    from tool_router.policy import Decision, RiskLevel
    rounds = rounds or int(os.environ.get("AIW_PHONE_FAST_ROUNDS", "8") or 8)
    schemas = [t[0] for t in tools.values()] + [ASK, ESC]
    messages = [{"role": "system", "content": SYSTEM + (f"\n\n背景：\n{context[:3000]}" if context else "")},
                {"role": "user", "content": prompt}]
    done: list[dict] = []
    for _ in range(rounds):
        r = await llm.chat(messages, schemas, 1200)
        calls = r.get("tool_calls") or []
        if not calls:
            text = (r.get("content") or "").strip() or "已在手机上完成。"
            return {"status": "succeeded", "result": text, "log": done}
        messages.append({"role": "assistant", "content": r.get("content") or "", "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False)}}
            for c in calls]})
        for c in calls:
            name, args = c["name"], c.get("args") or {}
            if name == "escalate_to_computer":
                return {"status": "escalate", "result": "", "reason": str(args.get("reason") or "")[:300], "log": done}
            if name == "ask_user":
                ans = await ask(str(args.get("question") or "")[:500], [str(o) for o in (args.get("options") or [])][:6])
                res: Any = {"answer": ans} if ans is not None else {"answer": None, "note": "用户没有回答"}
            elif name in tools:
                fn = tools[name][1]
                need = getattr(fn, "needs_confirm", None)
                ok = True
                if need and need(args):
                    ok = await confirm(name, args, Decision(RiskLevel.L3, getattr(fn, "confirm_reason", "") or "手机写入操作"))
                if not ok:
                    res = {"ok": False, "error": "用户没有同意，未执行"}
                else:
                    await note(f"手机：{name}", {"tool": name, "args": args})
                    try:
                        res = await fn(args)
                    except Exception as e:  # noqa: BLE001
                        res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
                    done.append({"tool": name, "args": args, "result": res})
            else:
                res = {"ok": False, "error": f"快通道没有工具 {name}"}
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(res, ensure_ascii=False, default=str)[:4000]})
    return {"status": "escalate", "result": "", "reason": "手机快通道轮数用完", "log": done}


__all__ = ["classify", "kind_of", "enabled", "fast_ttl", "run", "results_block", "KINDS", "VM_RE"]
