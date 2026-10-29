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
    ToolSpec("shell", "shell.exec", "Run a shell command in the VM (non-root user). Returns stdout/stderr. Default time "
             "limit 60 s (installs 300 s, ffmpeg/TTS 600 s, renders 1800 s); pass timeout (seconds, up to 3600) for "
             "anything longer instead of starting it again.",
             {"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]),
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
    ToolSpec("files_write", "files.write", "Write a text file in the VM workspace. append=true adds the content to the "
             "end of the file instead of replacing it (write a long file in parts, e.g. one sheet of plan.md per call).",
             {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}}, ["path", "content"]),
    ToolSpec("files_download", "files.download", "Download a URL into the VM workspace (downloads/).",
             {"url": {"type": "string"}, "path": {"type": "string"}}, ["url"]),
    ToolSpec("send_file", "files.read",
             "Send a finished file (docx/xlsx/pdf/png/zip...) to the user's chat window as a download. "
             "Path is relative to ~/workspace; copy or save the file there first. Images (png/jpg/webp/gif) show inline in "
             "the chat as pictures, so for screenshots or images the user wants to see or confirm, send_file EACH image "
             "on its own (one call per image) and never zip them; zip only when the user asks for an archive.",
             {"path": {"type": "string"}}, ["path"]),
    ToolSpec("read_image", None,
             "Read an image or a PDF page in ~/workspace (an uploaded design drawing, rendering, sketch, quote-sheet "
             "photo, or a cad_draw preview) with the vision model. Returns a structured Chinese description: drawing "
             "type, every readable dimension, components left-to-right, materials, and unclear points. Ask a specific "
             "question to focus it, e.g. '电视墙从左到右每段宽度是多少'. Several images: pass them all in `paths` in ONE "
             "call; they are read in parallel (about 50 s in total instead of 50 s each). To COMPARE images "
             "(before/after an edit, two versions) pass compare=true: they are shown side by side in one picture.",
             {"path": {"type": "string", "description": "relative to ~/workspace, e.g. uploads/设计图.jpg"},
              "paths": {"type": "array", "items": {"type": "string"}, "description": "several images read at once"},
              "page": {"type": "integer", "description": "PDF page, 1-based"},
              "compare": {"type": "boolean", "description": "with paths: read them side by side in one picture "
                                                           "(plus an enlarged view of where they differ)"},
              "grid": {"type": "boolean", "description": "draw a labelled pixel grid so positions come back in real "
                                                        "pixels (use before image_edit move/restore boxes)"},
              "question": {"type": "string"}}, []),
    ToolSpec("image_edit", None,
             "AI image editing: describe a change in words and an image model redraws the photo (make the cabinets "
             "taller, remove a person, change a wall colour, put the sofa elsewhere, add a sign). For changes a "
             "script cannot do. The result is scaled back to the original size. AI redraws small text and logos "
             "badly: afterwards read_image the result and paste those regions back from the original with "
             "`restore` (aligned automatically). `move` (no AI, the pixels of box are "
             "scaled into to) is only for flat things with nothing on them (a sign, a picture, a logo); furniture made "
             "taller or lower carries what stands on it, so that is a prompt edit. "
             "Exact edits (text layers, colours, crops, PSD) are photopea's job.",
             {"path": {"type": "string", "description": "image to edit, relative to ~/workspace"},
              "prompt": {"type": "string", "description": "the change only, in plain words; everything else is kept"},
              "save": {"type": "string", "description": "output .png, relative to ~/workspace (default <name>-ai.png)"},
              "restore": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                          "description": "[[left, top, right, bottom], ...] in pixels of the ORIGINAL image: regions "
                                         "copied back from the original (garbled text, labels, logos, faces)"},
              "original": {"type": "string", "description": "the untouched original to restore from, when `path` "
                                                            "is already an AI result"},
              "move": {"type": "array", "items": {"type": "object"},
                       "description": '[{"box": [l,t,r,b], "to": [l,t,r,b]}]: copy box and scale it into to, exactly '
                                      '(raise a cabinet 30 px: same box, to-top 30 smaller). Optional prompt then '
                                      'only cleans seams'},
              "keep_size": {"type": "boolean", "description": "scale the result to the original size (default true)"}},
             ["path"]),
    ToolSpec("photopea", None,
             "Edit images like in Photoshop: PSD/PNG/JPG/WebP in Photopea (a Photoshop clone) in a browser window on "
             "the desktop; the user watches it. One call = open files -> run `script` -> save. `script` is Photoshop "
             "ExtendScript-style JavaScript on app.activeDocument (layers, artLayers.add(), LayerKind.TEXT + textItem."
             "contents/size/font/color/position, layers.getByName, visible, opacity, translate(dx,dy), resize(%,%), "
             "rotate, resizeImage(w,h), resizeCanvas, crop([l,t,r,b]), selection.select/fill, applyGaussianBlur, "
             "adjustBrightnessContrast, desaturate, duplicate, remove, merge, flatten). Built-ins: ppLog(...) prints "
             "values back to you, ppColor('ff3300') makes a SolidColor, ppPlace('logo.png') adds a file listed in "
             "`place` as a new top layer (it arrives a moment later: move it in a second call with keep=true). "
             "Chinese text: font 'NotoSansSC-Regular' or 'NotoSansSC-Bold'. layer.bounds[i] and textItem.size are "
             "UnitValues: read .value. No for-in loops over Photopea objects. Never call saveToOE or app.echoToOE; "
             "list outputs in `save`. Use info=true to get every layer's name, bounds and text before editing.",
             {"open": {"type": "array", "items": {"type": "string"},
                       "description": "files to open, relative to ~/workspace; each becomes a document, the last is active. "
                                      "Without keep, documents already open are closed first"},
              "script": {"type": "string", "description": "Photoshop-style JavaScript run on the active document"},
              "save": {"type": "array", "items": {"type": "string"},
                       "description": "outputs relative to ~/workspace; format from the extension: .psd .png .jpg "
                                      "(.jpg:0.8 sets quality) .webp .svg .pdf"},
              "place": {"type": "array", "items": {"type": "string"}, "description": "images the script can ppPlace"},
              "fonts": {"type": "array", "items": {"type": "string"}, "description": "font files (.ttf/.otf) to load first"},
              "keep": {"type": "boolean", "description": "keep working on the documents already open"},
              "info": {"type": "boolean", "description": "return documents and layers (after the script)"},
              "timeout": {"type": "integer", "description": "seconds per step, default 90"}}, []),
    ToolSpec("drawing_read", None,
             "Read ONE region of a source drawing/photo rigorously: it is cropped and enlarged (a vertical dimension chain "
             "is turned to read left->right) and read by the vision model TWICE, asked two ways; a third larger read only "
             "when they disagree. kind='chain' returns the dimension numbers in reading order (left->right, or top->bottom "
             "for a vertical chain) with the total, or a CONFLICT to re-read; kind='text' returns the labels both reads "
             "saw. Use one call per dimension chain (top, bottom, left, right, inner rows), never a whole-photo question.",
             {"path": {"type": "string", "description": "source image relative to ~/workspace, e.g. uploads/photo.jpg"},
              "box": {"type": "array", "items": {"type": "number"},
                      "description": "[x0,y0,x1,y1] in the image's pixels: just the chain (its line, ticks and numbers) or one room"},
              "kind": {"type": "string", "enum": ["chain", "text"]},
              "question": {"type": "string", "description": "optional extra hint, e.g. '共 6 段'"}}, ["path", "box"]),
    ToolSpec("drawing_build", None,
             "Generate the WHOLE drawing deterministically from a structured spec (JSON, schema in the cad-drawing skill: "
             "floor_plan rooms/doors/windows/furniture/dimension chains, diagram nodes/edges, or generic entities/dims). "
             "Writes <out>/<name>.dxf (+ a LibreCAD viewing copy, opened on the desktop), <name>.png, overlay.png (the "
             "result drawn on the source photo with the pixel->mm fit of the dimension ticks) and report.json, and returns "
             "the automatic checks: opposite chains add up, each written number fits its measured tick spacing, no text "
             "overlap (computed from text boxes), every room labelled, walls closed, doors/windows on walls. To change "
             "the drawing, change the spec and call again (at most 3 fix rounds); never ERASE or retype it.",
             {"spec": {"type": "string", "description": "spec path relative to ~/workspace (default cad/spec.json)"},
              "content": {"type": "string", "description": "optional: the full spec JSON, saved to `spec` first"},
              "name": {"type": "string", "description": "output file name without extension (default plan)"},
              "out": {"type": "string", "description": "output folder (default the spec's folder)"},
              "source": {"type": "string", "description": "optional source image, overrides spec.source.image"}}, []),
    ToolSpec("cad_command", None,
             "Manual edits in the LibreCAD window (NEW drawings: write a spec and use drawing_build), typed like a draftsman at its command line. Each item of "
             "`commands` is typed into LibreCAD's command line and followed by Enter, in order; the user watches it. "
             "Units are mm at 1:1. Examples: ['line','0,0','6000,0','6000,10000','0,10000','0,0','ESC'], "
             "['rectangle','0,0','@1500,150'], ['circle','3000,750','350'], ['polyline','0,0','@1000,0','@0,500','ESC'], "
             "['dimhorizontal','0,0','6000,0','3000,-800']. Use 'ESC' to finish a command. After each call the view zooms "
             "to fit the whole drawing automatically. Shorthand items: 'line|x1,y1|x2,y2|...' (add '|C' to close), 'rect|x1,y1|x2,y2', 'circle|x,y|r', 'arc|cx,cy|r|a1|a2'; 'TEXT|x,y|height|内容' (x,y = LOWER LEFT corner, Chinese ok), 'TEXTC|x,y|height|内容' (centred on x,y), "
             "'FRAME|n|图名|图号|1:50|工程名称' (A3 frame + title block of sheet n at x=n*25000; the points after it may be "
             "sheet-local 0..21000 and are moved onto sheet n), 'SHEET|n' (continue on sheet n), 'NOTE|x,y|175|行1;行2', "
             "'DIMH|y_measured|y_dimline|x1,x2,...' / 'DIMV|x_measured|x_dimline|y1,y2,...' (dimension chains), "
             "'LEADER|x1,y1|x2,y2|175|材料', (drawing area sheet-local x 1250..20500, y 500..14350; title block x>11500,y<2500; a batch with points outside the border, in the title block or with unknown commands is sent back untyped; text overlaps are reported as warnings), 'SAVE|cad/name.dxf', 'PDF|cad/name.dxf|cad/name.pdf' (LibreCAD dxf2pdf, a page "
             "per sheet). Send a whole sheet (100-200 items) per call. Items already drawn in this task are skipped, so resending a failed sheet only types what is missing. Never type a bare 'text'.",
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
             "a value). Waits for the reply. Do not use it for confirmations of risky actions. "
             "FORMS (sign-ups, registrations, profiles, checkout details): read the CURRENT page first, then pass `fields` "
             "with exactly the inputs this page step shows (same order, the page's own labels translated to Chinese, "
             "select options copied from the page, e.g. country/region list or security questions). The user gets a "
             "card, fills it and you get the values; fields of type password (or secret:true) come back as {{key}}: "
             "type the literal {{key}} with `type` and the real value is filled in. One form per page step: fill it, "
             "go to the next step, then ask again for the next step's fields. Never invent personal data.",
             {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
              "title": {"type": "string", "description": "form card title, e.g. 'Apple Account 注册 · 第 1 步'"},
              "description": {"type": "string", "description": "one line: what the data is for"},
              "fields": {"type": "array", "items": {"type": "object", "properties": {
                  "key": {"type": "string"}, "label": {"type": "string"},
                  "type": {"type": "string", "enum": ["text", "password", "email", "tel", "date", "number", "select", "textarea"]},
                  "options": {"type": "array", "items": {"type": "string"}}, "required": {"type": "boolean"},
                  "secret": {"type": "boolean"}, "placeholder": {"type": "string"}, "hint": {"type": "string"}},
                  "required": ["key", "label"]}}}, ["question"]),
    ToolSpec("request_takeover", None, "Ask the user to do something on the desktop themselves and wait until they are "
             "done: a captcha/human check you could not pass after trying (Cloudflare 'verify you are human': click its "
             "checkbox yourself first, wait 5 s, screenshot; only after 2 failed tries), an SMS/2FA step on their "
             "phone, a payment, or a login only they can do. Fill the structured fields so the user sees a clear card: "
             "`action` (what kind of step), `site` (the site/app in Chinese, e.g. 京东), `what_to_do` (one short Chinese "
             "instruction for the user, e.g. 在登录页输入手机收到的短信验证码，然后点登录) and `reason` (a one-line Chinese "
             "title of what you need, e.g. 在京东登录页输入短信验证码). Never ask for passwords or codes in chat.",
             {"reason": {"type": "string", "description": "one-line Chinese title: 在京东登录页输入短信验证码"},
              "action": {"type": "string", "enum": ["login", "sms_code", "captcha", "slider", "qr_scan", "payment", "2fa",
                                                    "confirm", "other"]},
              "site": {"type": "string", "description": "site or app name, e.g. 京东 / 小红书 / 微信"},
              "what_to_do": {"type": "string", "description": "short Chinese instruction for the user, e.g. 拖动滑块完成验证后点“我已完成”"},
              "url": {"type": "string", "description": "the page's url, if known"}}, ["reason"]),
    ToolSpec("remember", None, "Save a durable fact or preference about the user for future tasks "
             "(e.g. 'weekly report goes to ~/workspace/reports'). Only facts the user stated or confirmed.",
             {"text": {"type": "string"}}, ["text"]),
    ToolSpec("delegate", None, "Hand a self-contained terminal/file sub-task to a text-only helper agent (no screen). "
             "Faster and cheaper for shell work: scripts, file conversion, data processing. Returns its summary.",
             {"goal": {"type": "string"}}, ["goal"]),
    ToolSpec("dispatch", None, "Split the request into independent pieces and run them AT THE SAME TIME on several "
             "computers (see COMPUTERS in the system prompt). Each item becomes its own child task with its own chat on "
             "that computer; the call waits until all of them finish (or `timeout_min` runs out) and returns each one's "
             "status, summary and output files, copied into ~/workspace/dispatch/ on YOUR computer so you can combine "
             "them, send_file the result and finish. machine: a computer id from COMPUTERS, or \"auto\" (an idle online "
             "one; paused cloud computers are woken). Your own computer may take one item. Each goal must be complete "
             "and self-contained (the child does not see this conversation): say what to produce, where to save it under "
             "~/workspace and to send_file it. Do not use it for steps that depend on each other or for one small job.",
             {"tasks": {"type": "array", "items": {"type": "object", "properties": {
                 "machine": {"type": "string", "description": "computer id from COMPUTERS, or \"auto\""},
                 "goal": {"type": "string", "description": "the complete, self-contained instruction for that computer"}},
                 "required": ["goal"]}},
              "timeout_min": {"type": "integer", "description": "overall limit for all items, minutes (default 60)"}},
             ["tasks"]),
    # BrowserOS neo (only shown when the computer has it and the user left it on in 设置 › 电脑): element-level web
    # actions through its accessibility snapshot. Each one maps onto BrowserOS's MCP tool of the same suffix.
    ToolSpec("bos_tabs", "browseros.call",
             "BrowserOS browser tabs (this task has its own tab group). action=new + url opens a page and returns its page "
             "id plus a first snapshot; list shows your pages; close closes one. The page you open or work on is put on "
             "the screen automatically (the user watches it); front does that again by hand.",
             {"action": {"type": "string", "enum": ["new", "list", "active", "close", "front"]},
              "url": {"type": "string"}, "page": {"type": "integer"}}, ["action"]),
    ToolSpec("bos_navigate", "browseros.call",
             "Load a url in a BrowserOS page (or back/forward/reload). Returns the new page's snapshot; old refs are invalid.",
             {"page": {"type": "integer"}, "action": {"type": "string", "enum": ["url", "back", "forward", "reload"]},
              "url": {"type": "string"}}, ["page"]),
    ToolSpec("bos_snapshot", "browseros.call",
             "Accessibility tree of a BrowserOS page; every actionable element carries [ref=eN] for bos_act. "
             "mode=interactive (default here) lists only actionables with headings/context: far fewer tokens.",
             {"page": {"type": "integer"}, "mode": {"type": "string", "enum": ["interactive", "full"]},
              "depth": {"type": "number"}}, ["page"]),
    ToolSpec("bos_act", "browseros.call",
             "Act on a BrowserOS page element by its ref from the latest snapshot: kind=click | fill (ref+value, or "
             "fields=[{ref,value}] for a whole form) | type (text into the focused element) | press (key, e.g. Enter) | "
             "select (ref+value) | check | uncheck | hover | focus | scroll (direction, amount) | dialog_accept | "
             "dialog_dismiss. Returns a short diff of what changed, so no screenshot is needed afterwards. Prefer refs; "
             "click_at/type_at with viewport x,y only when an element has no ref.",
             {"page": {"type": "integer"}, "kind": {"type": "string", "enum": [
                 "click", "fill", "type", "press", "select", "check", "uncheck", "hover", "focus", "scroll",
                 "dialog_accept", "dialog_dismiss", "click_at", "type_at", "hover_at", "drag"]},
              "ref": {"type": "string"}, "value": {"type": "string"}, "text": {"type": "string"},
              "key": {"type": "string"},
              "fields": {"type": "array", "items": {"type": "object", "properties": {
                  "ref": {"type": "string"}, "value": {"type": "string"}}, "required": ["ref", "value"]}},
              "direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "amount": {"type": "number"},
              "targetRef": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
              "diff": {"type": "string", "description": "summary | none | full (default: capped)"}}, ["page", "kind"]),
    ToolSpec("bos_read", "browseros.call",
             "Read a BrowserOS page's content as markdown (or text/links), optionally only a CSS subtree. For reading prices, "
             "lists, articles; not for acting.",
             {"page": {"type": "integer"}, "format": {"type": "string", "enum": ["markdown", "text", "links"]},
              "selector": {"type": "string"}, "viewportOnly": {"type": "boolean"}}, ["page"]),
    ToolSpec("bos_grep", "browseros.call",
             "Search a BrowserOS page without dumping it: over=ax matches snapshot lines (keeping their [ref=eN]), "
             "over=content matches visible text. Cheapest way to find one button or price.",
             {"page": {"type": "integer"}, "pattern": {"type": "string"}, "over": {"type": "string", "enum": ["ax", "content"]}},
             ["page", "pattern"]),
    ToolSpec("bos_diff", "browseros.call", "What changed on a BrowserOS page since the last snapshot/diff.",
             {"page": {"type": "integer"}}, ["page"]),
    ToolSpec("bos_wait", "browseros.call",
             "Wait on a BrowserOS page until text (for=text, value=substring) or a CSS selector appears, or for=time value=ms.",
             {"page": {"type": "integer"}, "for": {"type": "string", "enum": ["text", "selector", "time"]},
              "value": {"type": "string"}, "timeout": {"type": "number"}}, ["page"]),
    ToolSpec("bos_upload", "browseros.call",
             "Put local files (paths on this computer, e.g. ~/workspace/uploads/a.jpg) on an <input type=file> by its ref.",
             {"page": {"type": "integer"}, "ref": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
             ["page", "ref", "files"]),
    ToolSpec("done", None, "Finish: the task is complete. Give a short summary.", {"summary": {"type": "string"}}, ["summary"]),
    ToolSpec("fail", None, "Only when truly nothing more can be tried: explain in Chinese what was done, where it is stuck and "
             "what the user could do. The user is then asked whether to retry, take over or stop. For a captcha/login/"
             "SMS code use request_takeover instead; for missing information use ask_user.", {"reason": {"type": "string"}}, ["reason"]),
]

BY_NAME = {t.name: t for t in TOOLS}
BOS_TOOLS = tuple(t.name for t in TOOLS if t.action == "browseros.call")


LOOP_TOOLS = {"done", "fail", "update_plan", "ask_user", "request_takeover", "remember", "delegate", "dispatch", "click_element", "cad_draw", "cad_command", "drawing_read", "drawing_build", "read_image", "photopea", "image_edit"}
SUBAGENT_TOOLS = ("shell", "files_list", "files_read", "files_write", "files_download", "done", "fail")


def tool_schemas(extra: list[dict] | None = None, only: tuple[str, ...] | None = None,
                 hide: tuple[str, ...] | set[str] = ()) -> list[dict]:
    """Neutral schema list; llm-router adapts to OpenAI/Anthropic/Gemini formats.
    `extra` adds external (HTTP/OpenAPI/MCP) tools; `only` restricts to a subset."""
    out = [{"name": t.name, "description": t.description,
            "parameters": {"type": "object", "properties": t.params, "required": t.required}}
           for t in TOOLS if (only is None or t.name in only) and t.name not in hide]
    return out + list(extra or [])
