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
    ToolSpec("browser_click", "browser.click",
             "BEST way to click on a web page in Chrome: give the visible text (or aria-label/title) of the button, "
             "link, tab or checkbox, e.g. 注册 / Sign in / 下一步. The element is found in the page DOM and clicked with "
             "the real mouse, so it is exact. If it is not found, the error lists the clickable texts that exist.",
             {"text": {"type": "string", "description": "visible text of the element"}}, ["text"]),
    ToolSpec("browser_fill", "browser.fill",
             "BEST way to type into a web form field in Chrome: give the field's label or placeholder (e.g. 用户名, "
             "密码, Email) and the text. Clears the field first. submit=true presses Enter afterwards. Also selects "
             "an option in a <select>.",
             {"field": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["field", "text"]),
    ToolSpec("click_element", None,
             "Click a UI element described in words when browser_click cannot be used (desktop apps, canvas, images, "
             "icons). A vision model locates it in two zoom passes, which is far more precise than guessing x/y. "
             "Describe it with position and look, e.g. '右上角红色的注册按钮'.",
             {"target": {"type": "string"}, "double": {"type": "boolean"},
              "button": {"type": "string", "enum": ["left", "right"]}}, ["target"]),
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
    ToolSpec("read_image", None,
             "Read an image or a PDF page in ~/workspace (an uploaded design drawing, rendering, sketch, quote-sheet "
             "photo, or a cad_draw preview) with the vision model. Returns a structured Chinese description: drawing "
             "type, every readable dimension, components left-to-right, materials, and unclear points. Ask a specific "
             "question to focus it, e.g. '电视墙从左到右每段宽度是多少'.",
             {"path": {"type": "string", "description": "relative to ~/workspace, e.g. uploads/设计图.jpg"},
              "page": {"type": "integer", "description": "PDF page, 1-based"},
              "question": {"type": "string"}}, ["path"]),
    ToolSpec("cad_command", None,
             "Draw in the LibreCAD window on the desktop, like a draftsman typing at its command line. Each item of "
             "`commands` is typed into LibreCAD's command line and followed by Enter, in order; the user watches it. "
             "Units are mm at 1:1. Examples: ['line','0,0','6000,0','6000,10000','0,10000','0,0','ESC'], "
             "['rectangle','0,0','@1500,150'], ['circle','3000,750','350'], ['polyline','0,0','@1000,0','@0,500','ESC'], "
             "['dimhorizontal','0,0','6000,0','3000,-800']. Use 'ESC' to finish a command. After each call the view zooms "
             "to fit the whole drawing automatically. Put 'text' (or 'hatch') LAST in a call: it opens a dialog that "
             "you fill in the GUI; afterwards ['text','字','x,y'] places more text without the dialog. "
             "`open` launches LibreCAD on a .dxf in ~/workspace first. Text, layers, hatches and saving are done in "
             "the GUI dialogs (keypress/type/click_element) after checking the screenshot.",
             {"commands": {"type": "array", "items": {"type": "string"}},
              "open": {"type": "string", "description": "optional .dxf path relative to ~/workspace to open first"}},
             ["commands"]),
    ToolSpec("cad_draw", None,
             "ONLY when the user explicitly asked for a script/代码: make a construction drawing (施工图: plan/elevation/section/detail) as DXF + PDF with exact dimensions. "
             "`code` is the Python BODY only (ezdxf, millimetres, 1:scale); a prelude already defines doc, msp, SCALE "
             "and helpers: frame(x0,y0,size='A3',title,project,sheet,date)->(ix0,iy0,ix1,iy1) inner box; "
             "rect(x,y,w,h,layer); line(p1,p2,layer); pline(pts,close,layer); circle(c,r); arc(c,r,a1,a2); "
             "wall(x1,y1,x2,y2,t=240); door(x,y,w,angle,swing); window(x1,y1,x2,y2,t); hatch(pts,pattern,scale); "
             "text(x,y,s,h=3.5,layer,align); note(x,y,lines,h,title); leader(pts,s); dim(p1,p2,offset); "
             "dim_chain(points,offset) (segments + overall); elev_level(x,y,value_m); view_title(x,y,s,scale). "
             "Layers: 墙体 门窗 柜体 家具 完成面 轴线 隐藏线 填充 尺寸 文字 图框 索引. Text heights are printed mm. "
             "Put several views on one sheet by offsetting origins inside the frame. The DXF opens in LibreCAD on "
             "the desktop and PDF+DXF are sent to the user. On a script error, fix the code and call again.",
             {"name": {"type": "string", "description": "file name without extension, e.g. 客厅背景墙立面"},
              "code": {"type": "string"},
              "scale": {"type": "integer", "description": "1:scale, default 50 (details 1:10 or 1:20)"},
              "open": {"type": "boolean"}, "deliver": {"type": "boolean"}}, ["name", "code"]),
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


LOOP_TOOLS = {"done", "fail", "update_plan", "ask_user", "remember", "delegate", "click_element", "cad_draw", "cad_command", "read_image"}
SUBAGENT_TOOLS = ("shell", "files_list", "files_read", "files_write", "files_download", "done", "fail")


def tool_schemas(extra: list[dict] | None = None, only: tuple[str, ...] | None = None,
                 hide: tuple[str, ...] | set[str] = ()) -> list[dict]:
    """Neutral schema list; llm-router adapts to OpenAI/Anthropic/Gemini formats.
    `extra` adds external (HTTP/OpenAPI/MCP) tools; `only` restricts to a subset."""
    out = [{"name": t.name, "description": t.description,
            "parameters": {"type": "object", "properties": t.params, "required": t.required}}
           for t in TOOLS if (only is None or t.name in only) and t.name not in hide]
    return out + list(extra or [])
