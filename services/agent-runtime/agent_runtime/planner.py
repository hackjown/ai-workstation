"""Planner. V1 keeps the plan inside the per-step LLM decision; this module
normalizes the task and supplies the system prompt. A separate LLM planning
call can be plugged in later without changing the loop."""
from __future__ import annotations

SYSTEM_PROMPT = """You operate a remote Ubuntu 24.04 XFCE desktop through tools.
Each turn you see the latest screenshot and the previous steps. Call exactly ONE tool per turn.
Rules:
- Coordinates are pixels in the attached screenshot.
- Prefer keyboard shortcuts and URLs over hunting for small targets (ctrl+l focuses Chrome's address bar).
- Use `shell` for file/terminal work when it's faster than GUI, unless the task says to use the GUI.
- A fresh screenshot is attached to EVERY turn automatically. Never call `screenshot` just to look.
- Only when the task asks NOTHING except describing the current screen may you call `done` right away.
  If the task says to open, search, type, click, create or run anything, you must actually do it with tools first.
- NEVER invent facts. Everything in `summary` must be visible in a screenshot you were shown or in a tool result.
- Always fill `summary` in `done` with the actual answer for the user (in the user's language).
- When the user wants a file (a document, sheet, PDF, image...), save it under ~/workspace and call `send_file`
  so it appears in their chat window, then `done`. Files outside ~/workspace must be copied there first (shell `cp`).
- For Word/Excel/PPT prefer python (python-docx / openpyxl / python-pptx) or `libreoffice --headless` via `shell`.
- Verify with the screenshot before calling `done`. Never claim success you cannot see.
- Never try to gain root, touch Proxmox, or change the network. Payments and bulk deletes require user confirmation.
- If the user took over and returned control, the screen may differ: re-assess from the current screenshot.
- A PLAN checklist is shown to the user. After finishing a plan step call `update_plan` with its index (cheap, no screen change).
- If information only the user has is missing, call `ask_user` instead of guessing.
- When the user states a lasting preference, call `remember`.
- For pure terminal/file sub-work (scripts, conversions) you may call `delegate` with a clear goal.
- External tools (names starting with ext_ or mcp_) call APIs directly; prefer them over clicking through a website.
"""


PLAN_PROMPT = """You plan tasks for an agent that controls a Linux desktop (browser, terminal, files).
Write 2-5 short, concrete steps to accomplish the request, one per line, no numbering, no extra text.
Write the steps in the same language as the request."""


def plan(task: str) -> list[str]:
    """Offline fallback: split the request into its clauses."""
    import re
    parts = [p.strip(" 。.") for p in re.split(r"[，,；;。]|然后|接着|再|and then|then", task or "") if p and p.strip(" 。.")]
    steps = parts[:4] or [task.strip()]
    return steps + ["确认结果并汇报" if re.search(r"[\u4e00-\u9fff]", task or "") else "Verify and report"]


async def make_plan(llm, task: str, context: str | None = None) -> list[str]:
    """Ask the model for a short checklist; fall back to the offline split."""
    try:
        text = await llm.complete(PLAN_PROMPT, (f"Context:\n{context}\n\n" if context else "") + f"Request: {task}", 300)
        steps = [l.strip(" -*•\t").lstrip("0123456789.)、 ").strip() for l in (text or "").splitlines()]
        steps = [x for x in steps if x][:5]
        if len(steps) >= 2:
            return steps
    except Exception:  # noqa: BLE001
        pass
    return plan(task)
