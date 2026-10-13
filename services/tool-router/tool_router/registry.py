"""Tool registry + JSON schemas shown to the LLM (provider-neutral).

Each LLM tool maps onto one protocol action. Coordinates are in the screenshot's
pixel space; the executor rescales if the screenshot was downsized.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    name: str
    action: str | None  # protocol action; None = control tool handled by the loop
    description: str
    params: dict = field(default_factory=dict)
    required: list[str] = field(default_factory=list)


def _xy(desc="pixel coordinate in the latest screenshot"):
    return {"x": {"type": "integer", "description": desc}, "y": {"type": "integer", "description": desc}}


TOOLS: list[ToolSpec] = [
    ToolSpec("screenshot", "computer.screenshot", "Take a fresh screenshot of the desktop."),
    ToolSpec("click", "computer.click", "Left-click at a point.", {**_xy(), "button": {"type": "string", "enum": ["left", "middle", "right"]}}, ["x", "y"]),
    ToolSpec("double_click", "computer.double_click", "Double-click at a point.", _xy(), ["x", "y"]),
    ToolSpec("right_click", "computer.right_click", "Right-click at a point.", _xy(), ["x", "y"]),
    ToolSpec("move", "computer.move", "Move the mouse.", _xy(), ["x", "y"]),
    ToolSpec("drag", "computer.drag", "Drag from (x1,y1) to (x2,y2).",
             {k: {"type": "integer"} for k in ("x1", "y1", "x2", "y2")}, ["x1", "y1", "x2", "y2"]),
    ToolSpec("type", "computer.type", "Type text at the current focus.", {"text": {"type": "string"}}, ["text"]),
    ToolSpec("keypress", "computer.keypress", "Press a key or combo, e.g. ENTER, ctrl+l, alt+Left.", {"key": {"type": "string"}}, ["key"]),
    ToolSpec("scroll", "computer.scroll", "Scroll; dy>0 scrolls down. Optional x,y to scroll over.",
             {"dx": {"type": "integer"}, "dy": {"type": "integer"}, **_xy()}, ["dx", "dy"]),
    ToolSpec("open_app", "computer.open_app", "Launch an app: chrome, terminal, files, editor.", {"name": {"type": "string"}}, ["name"]),
    ToolSpec("close_app", "computer.close_app", "Close an app by name.", {"name": {"type": "string"}}, ["name"]),
    ToolSpec("shell", "shell.exec", "Run a shell command in the VM (non-root user). Returns stdout/stderr.",
             {"command": {"type": "string"}, "cwd": {"type": "string"}}, ["command"]),
    ToolSpec("browser_open", "browser.open", "Open Chrome, optionally at a URL.", {"url": {"type": "string"}}),
    ToolSpec("browser_navigate", "browser.navigate", "Navigate the visible Chrome tab to a URL.", {"url": {"type": "string"}}, ["url"]),
    ToolSpec("browser_back", "browser.back", "Browser back."),
    ToolSpec("browser_forward", "browser.forward", "Browser forward."),
    ToolSpec("files_list", "files.list", "List files under the VM workspace.", {"path": {"type": "string"}}, ["path"]),
    ToolSpec("files_read", "files.read", "Read a text file in the VM workspace.", {"path": {"type": "string"}}, ["path"]),
    ToolSpec("files_write", "files.write", "Write a text file in the VM workspace.",
             {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    ToolSpec("files_download", "files.download", "Download a URL into the VM workspace (downloads/).",
             {"url": {"type": "string"}, "path": {"type": "string"}}, ["url"]),
    ToolSpec("send_file", "files.read",
             "Send a finished file (docx/xlsx/pdf/png/zip...) to the user's chat window as a download. "
             "Path is relative to ~/workspace; copy or save the file there first.",
             {"path": {"type": "string"}}, ["path"]),
    ToolSpec("update_plan", None, "Mark plan steps finished (0-based indexes) and optionally add a short progress note.",
             {"done": {"type": "array", "items": {"type": "integer"}}, "note": {"type": "string"}}, ["done"]),
    ToolSpec("ask_user", None, "Ask the user a question when required information is missing (login choice, which file, "
             "a value). Waits for the reply. Do not use it for confirmations of risky actions.",
             {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, ["question"]),
    ToolSpec("remember", None, "Save a durable fact or preference about the user for future tasks "
             "(e.g. 'weekly report goes to ~/workspace/reports'). Only facts the user stated or confirmed.",
             {"text": {"type": "string"}}, ["text"]),
    ToolSpec("delegate", None, "Hand a self-contained terminal/file sub-task to a text-only helper agent (no screen). "
             "Faster and cheaper for shell work: scripts, file conversion, data processing. Returns its summary.",
             {"goal": {"type": "string"}}, ["goal"]),
    ToolSpec("done", None, "Finish: the task is complete. Give a short summary.", {"summary": {"type": "string"}}, ["summary"]),
    ToolSpec("fail", None, "Give up: the task cannot be completed. Explain why.", {"reason": {"type": "string"}}, ["reason"]),
]

BY_NAME = {t.name: t for t in TOOLS}


LOOP_TOOLS = {"done", "fail", "update_plan", "ask_user", "remember", "delegate"}
SUBAGENT_TOOLS = ("shell", "files_list", "files_read", "files_write", "files_download", "done", "fail")


def tool_schemas(extra: list[dict] | None = None, only: tuple[str, ...] | None = None) -> list[dict]:
    """Neutral schema list; llm-router adapts to OpenAI/Anthropic/Gemini formats.
    `extra` adds external (HTTP/OpenAPI/MCP) tools; `only` restricts to a subset."""
    out = [{"name": t.name, "description": t.description,
            "parameters": {"type": "object", "properties": t.params, "required": t.required}}
           for t in TOOLS if only is None or t.name in only]
    return out + list(extra or [])
