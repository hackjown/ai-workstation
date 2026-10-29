"""Turn a casual photo-edit request into an explicit brief, before the agent starts.

Users write "把上传图片中白色柜子加高到和左侧台面齐平，做好发给我" and expect the obvious result: the cabinet gets
taller, its counter and everything standing on it rise with it, the floor stays put. Left to itself the agent
read that as exact geometry (task-55a): it stretched the doors upward over the counter edge and called the
picture done. A person only gets the right picture from the image model by spelling out what moves with the
target and what stays (fengmu_combined2), so the vision model writes that out once, looking at the photo:
target, what moves with it, what stays, which text regions to paste back, the image-model prompt, and what the
before/after check must see. The loop then makes the first edit itself and hands the agent the check.
"""
from __future__ import annotations

import json
import re
from typing import Optional

_IMG = re.compile(r"(?:~/workspace/|\$HOME/workspace/|/(?:data/home|home/[\w.-]+)/workspace/)?([^\s\n'\"]+?\.(?:png|jpe?g|webp|bmp))\b", re.I)
_ATTACH = re.compile(r"\n*附件（已放到电脑上）：.*", re.S)
_EDIT = re.compile(r"加高|降低|抬高|升高|变高|变矮|压低|齐平|对齐|一样高|移到|挪|换成|替换|改成|变成|去掉|删掉|删除|去除|"
                   r"抹掉|P掉|p掉|加上|添加|放上|摆上|改色|颜色|调整|放大|缩小|拉长|拉宽|加宽|变宽|变窄|修图|改图|美化|"
                   r"效果图|墙面|地面|换个|改一下|修一下|弄成", re.I)
_NOT_EDIT = re.compile(r"是什么|识别|描述一下|提取|文字转|转成文字|OCR|翻译|多少钱|看看这张|分析一下|转成\s*pdf|压缩", re.I)

BRIEF_SYSTEM = ("你是修图需求分析师。用户会用很随意的一句话说想怎么改一张照片。你要看图，按常识理解他真正想要的结果，"
                "把它翻译成给 AI 修图模型的明确指令。只输出一个 JSON 对象，不要输出其他文字。")


def image_paths(prompt: str) -> list[str]:
    """Images the task names (uploads are listed as ~/workspace/uploads/x.png), relative to ~/workspace."""
    out = []
    for m in _IMG.finditer(prompt or ""):
        p = m.group(1).lstrip("/")
        for pre in ("home/aiw/workspace/", "workspace/"):
            if p.startswith(pre):
                p = p[len(pre):]
        if p not in out:
            out.append(p)
    return out


def instruction(prompt: str) -> str:
    """The user's own words, without the attachment list the API appends."""
    return _ATTACH.sub("", prompt or "").strip()


def wants_edit(prompt: str) -> bool:
    t = instruction(prompt)
    return bool(_EDIT.search(t)) and not _NOT_EDIT.search(t)


def brief_user(instr: str, w: int, h: int) -> str:
    return (f"图片 {w}x{h} 像素。图上画了红色像素网格，线旁数字是原图像素坐标（x 向右，y 向下）。\n"
            f"用户的要求：「{instr}」\n\n"
            "按常识理解：家具“加高/降低到和某处齐平”= 整件家具变高或变矮，它上面的台面和摆在台面上的东西跟着一起上移或下移，"
            "地面、踢脚线、墙、窗户不动，透视保持一致；“换颜色/换材质”只改颜色材质不改形状；“去掉某物”要把后面的背景自然补全；"
            "“移到某处”要连影子一起移。用户说的参照物（如“左侧台面”）就是目标位置，要在指令里用它描述改到哪里。\n\n"
            "输出 JSON：\n"
            "{\n"
            ' "target": "要改的物体（位置+外观，具体到能在图里认出来）",\n'
            ' "reference": "用户说的参照物，没有就空字符串",\n'
            ' "change": "怎么改、改多少（像素或相对参照物）",\n'
            ' "moves_with": ["跟着一起移动或变化的东西"],\n'
            ' "keep": ["必须保持原样的东西"],\n'
            ' "keep_text_boxes": [[左,上,右,下]],\n'
            ' "ai_prompt": "给修图模型的完整中文指令",\n'
            ' "check": "改完后和原图并排对比时要确认的 2-3 点，一眼能判断成败",\n'
            ' "question": "",\n'
            ' "geometry": null,\n'
            ' "front_boxes": []\n'
            "}\n"
            "geometry：只有当要求是把一组柜子/台面/桌子整体加高或降低（对齐到某个参照高度）时才填，否则 null。填写原图像素：\n"
            '{"x_left": 目标柜体左端x, "x_right": 目标柜体右端x（被前面东西挡住的部分也算）, "top_y": 台面上摆的物品最高点y, '
            '"front_top_y": 目标台面前沿上边y, "base_y": 柜体底部（踢脚线上沿或柜门下沿）y, '
            '"target_y": 目标台面前沿上边应该到达的y（参照物在交接处的对应高度）, "probe_x": 目标与参照物交接处附近的x}\n'
            "front_boxes：站在目标前面、不该跟着动的东西（地上的海报架、堆着的水、椅子等）的外框 [[左,上,右,下]]，没有就 []。\n"
            "keep_text_boxes：不该被改动、而且上面有文字或 logo 的区域（海报、标签、招牌、包装上的字），原图像素坐标，"
            "改完要从原图贴回去；与要改或要移动的物体重叠的区域不要列（贴回会把修改盖掉）。没有就 []。\n"
            "ai_prompt：写清改哪个物体、往哪个方向、改到什么位置（用参照物说，例如“台面前沿与左侧台面在拐角处连成一线”）、"
            "哪些东西一起动、哪些东西完全不动，要求照片级真实、透视和光线不变、不要新增文字。\n"
            "question：只有当要求确实有两种完全不同的理解、看图也判断不了时才填一句中文问题，否则留空。")


def parse(text: str, w: int, h: int) -> Optional[dict]:
    """The brief as a dict, or None when the reply holds no usable JSON / no ai_prompt."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    boxes = []
    for b in d.get("keep_text_boxes") or []:
        try:
            l, t, r, bt = (float(v) for v in b)
        except Exception:  # noqa: BLE001
            continue
        l, r = sorted((max(0.0, l), min(float(w), r)))
        t, bt = sorted((max(0.0, t), min(float(h), bt)))
        if r - l < 8 or bt - t < 8 or (r - l) * (bt - t) > 0.3 * w * h:  # a box that big would undo the edit
            continue
        boxes.append([int(l), int(t), int(r), int(bt)])
    d["keep_text_boxes"] = boxes[:6]
    d["front_boxes"] = _boxes(d.get("front_boxes"), w, h, 0.4)
    d["geometry"] = _geometry(d.get("geometry"), w, h)
    for k in ("moves_with", "keep"):
        v = d.get(k)
        d[k] = [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])
    for k in ("target", "reference", "change", "ai_prompt", "check", "question"):
        d[k] = str(d.get(k) or "").strip()
    if not d["ai_prompt"] and not d["question"]:
        return None
    return d


def full_prompt(d: dict) -> str:
    """The image-model prompt: the brief's own words plus the lists, so nothing it decided gets lost."""
    p = d["ai_prompt"]
    if d.get("moves_with") and not all(x in p for x in d["moves_with"][:3]):
        p += "\n一起移动/变化：" + "、".join(d["moves_with"])
    if d.get("keep"):
        p += "\n保持完全不变：" + "、".join(d["keep"])
    return p


def summary(d: dict) -> str:
    parts = [f"目标：{d['target']}"]
    if d.get("reference"):
        parts.append(f"参照：{d['reference']}")
    if d.get("change"):
        parts.append(f"改法：{d['change']}")
    if d.get("moves_with"):
        parts.append("一起动：" + "、".join(d["moves_with"]))
    if d.get("keep"):
        parts.append("不动：" + "、".join(d["keep"][:6]))
    return "；".join(parts)


def _boxes(v, w, h, max_share):
    out = []
    for b in v or []:
        try:
            l, t, r, bt = (float(x) for x in b)
        except Exception:  # noqa: BLE001
            continue
        l, r = sorted((max(0.0, l), min(float(w), r)))
        t, bt = sorted((max(0.0, t), min(float(h), bt)))
        if r - l >= 8 and bt - t >= 8 and (r - l) * (bt - t) <= max_share * w * h:
            out.append([int(l), int(t), int(r), int(bt)])
    return out[:6]


def _geometry(g, w, h) -> Optional[dict]:
    """Lenient: clamp what the vision model gave into the picture, fill the optional fields, refuse only nonsense."""
    keys = ("x_left", "x_right", "top_y", "front_top_y", "base_y", "target_y")
    if not isinstance(g, dict):
        return None
    src = g
    try:
        g = {k: int(round(float(src[k]))) for k in keys}
    except Exception:  # noqa: BLE001
        return None
    for k in ("x_left", "x_right"):
        g[k] = max(0, min(w, g[k]))
    for k in ("top_y", "front_top_y", "base_y", "target_y"):
        g[k] = max(0, min(h, g[k]))
    if g["x_left"] > g["x_right"]:
        g["x_left"], g["x_right"] = g["x_right"], g["x_left"]
    for k, default in (("probe_x", g["x_left"] + 15), ("x_left_top", g["x_left"])):
        try:
            g[k] = max(0, min(w, int(round(float(src.get(k))))))
        except Exception:  # noqa: BLE001
            g[k] = default
    if g["top_y"] >= g["front_top_y"]:
        g["top_y"] = max(0, g["front_top_y"] - int(0.15 * h))
    if not (g["front_top_y"] < g["base_y"] and g["x_right"] - g["x_left"] >= 0.08 * w):
        return None
    return g


RAISE = re.compile(r"加高|降低|抬高|升高|变高|变矮|压低|调高|调低|齐平|一样高|对齐|平齐")


def wants_raise(instr: str) -> bool:
    return bool(RAISE.search(instr or ""))


def geometry_user(instr: str, brief: dict, w: int, h: int) -> str:
    return (f"图片 {w}x{h} 像素，图上红色网格数字是原图像素坐标。用户要求：「{instr}」\n"
            f"要改的：{brief.get('target') or '目标柜子'}；参照：{brief.get('reference') or '用户说的参照物'}。\n"
            "按网格读出下面这些像素值，只输出 JSON，不要解释：\n"
            '{"x_left": 目标柜体左端x, "x_right": 目标柜体右端x（被挡住的部分也算）, "top_y": 目标台面上物品的最高点y, '
            '"front_top_y": 目标台面前沿上边y, "base_y": 目标柜门下沿或踢脚线上沿y, '
            '"target_y": 目标台面前沿上边应到达的y（参照台面在交接处的高度）, "probe_x": 目标与参照交接处的x, '
            '"x_left_top": 台面以上属于目标的物品从哪个x开始（相邻柜子上的东西伸过来时取它们右边，否则等于 x_left）}')


def grounding_user(instr: str, brief: dict) -> str:
    """Qwen3-VL / GLM-V / Gemini are trained to box things in 0-1000 normalized coordinates, not to read pixels off a
    grid (task-3f3: asked for pixels, it answered 900/850 for everything). Ask in their native format."""
    return (f"用户要求：「{instr}」\n要改的：{brief.get('target') or '目标柜子'}；参照：{brief.get('reference') or '用户说的参照物'}。\n"
            "用你的目标定位能力框出下面这些东西，坐标用 0-1000 归一化的 [x1, y1, x2, y2]（左上到右下），只输出 JSON：\n"
            '{"target_cabinet": 目标柜子从台面前沿上边到柜门下沿（踢脚线以上）的框, '
            '"items_on_top": 把目标台面上所有物品（咖啡机、水槽龙头、杯子等）一起框住的框, '
            '"reference_counter": 参照台面前沿那条边（厚度那一条）的框, '
            '"junction_x": 目标和参照交接处的 x（0-1000）}')


def parse_grounding(text: str, w: int, h: int) -> Optional[dict]:
    """0-1000 boxes -> the pixel geometry the move planner uses."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        cab, items, ref = (list(map(float, d[k])) for k in ("target_cabinet", "items_on_top", "reference_counter"))
        if len(cab) != 4 or len(items) != 4 or len(ref) != 4:
            return None
        X = lambda v: v * w / 1000.0
        Y = lambda v: v * h / 1000.0
        jx = d.get("junction_x")
        g = {"x_left": X(min(cab[0], cab[2])), "x_right": X(max(cab[0], cab[2])),
             "top_y": Y(min(items[1], items[3])), "front_top_y": Y(min(cab[1], cab[3])),
             "base_y": Y(max(cab[1], cab[3])), "target_y": Y(min(ref[1], ref[3])),
             "x_left_top": X(min(items[0], items[2], cab[0], cab[2]))}
        g["probe_x"] = X(float(jx)) if jx is not None else (g["x_left"] if X(ref[0]) < g["x_left"] else g["x_right"])
        return _geometry(g, w, h)
    except Exception:  # noqa: BLE001
        return None


def parse_geometry(text: str, w: int, h: int) -> Optional[dict]:
    m = re.search(r"\{[^{}]*\}", text or "", re.S)
    if not m:
        return None
    try:
        return _geometry(json.loads(m.group(0)), w, h)
    except Exception:  # noqa: BLE001
        return None


def measure_crop(img, g: dict):
    """A 3x zoom of where the two edges meet, with a 10 px grid labelled in original pixels, and its box."""
    from PIL import ImageDraw
    W, H = img.size
    y0 = max(0, min(g["front_top_y"], g["target_y"]) - 70)
    y1 = min(H, max(g["front_top_y"], g["target_y"]) + 70)
    x0 = max(0, g["probe_x"] - 160)
    x1 = min(W, g["probe_x"] + 160)
    k = 3
    c = img.crop((x0, y0, x1, y1)).resize(((x1 - x0) * k, (y1 - y0) * k))
    d = ImageDraw.Draw(c)
    for y in range((y0 // 10 + 1) * 10, y1, 10):
        Y = (y - y0) * k
        d.line([(0, Y), (c.width if y % 50 == 0 else 24, Y)], fill=(255, 0, 0), width=1)
        if y % 20 == 0:
            d.text((28, Y - 6), str(y), fill=(255, 0, 0))
    for x in range((x0 // 50 + 1) * 50, x1, 50):
        X = (x - x0) * k
        d.line([(X, 0), (X, 18)], fill=(0, 0, 255))
        d.text((X + 2, 20), str(x), fill=(0, 0, 255))
    return c, (x0, y0, x1, y1)


def measure_question(brief: dict) -> str:
    return ("这是原图交接处放大 3 倍的局部，左边红色数字是原图的 y 像素坐标（每格 10 像素），上方蓝色数字是 x。\n"
            f"A = {brief['target'] or '要改的台面'} 的台面前沿上边；B = {brief['reference'] or '参照台面'} 在交接处的台面前沿上边。\n"
            '按红色刻度读出 A 和 B 的 y，只输出 JSON：{"front_top_y": A的y, "target_y": B的y}')


def parse_measure(text: str) -> Optional[tuple[int, int]]:
    m = re.search(r"\{[^{}]*\}", text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return int(round(float(d["front_top_y"]))), int(round(float(d["target_y"])))
    except Exception:  # noqa: BLE001
        return None


def plan_moves(g: dict, dy: int) -> list[dict]:
    """Raise (dy > 0) or lower the cabinet: what stands on it and the counter shift by dy, the doors stretch or
    shrink between the counter and the base, the base stays. Continuous at the split by construction."""
    xl, xr, top, base = g["x_left"], g["x_right"], max(0, g["top_y"] - 12), g["base_y"]
    xlt = min(max(g.get("x_left_top") or xl, xl), xr - 20)  # above the counter, a neighbour's things may overlap
    ft = g["front_top_y"] - 8
    split = min(base - 10, g["front_top_y"] + 30)   # just under the counter's front edge
    moves = []
    if ft - top > 4:
        moves.append({"box": [xlt, top, xr, ft], "to": [xlt, top - dy, xr, ft - dy]})   # what stands on it
    moves.append({"box": [xl, ft, xr, split], "to": [xl, ft - dy, xr, split - dy]})     # the counter edge
    moves.append({"box": [xl, split, xr, base], "to": [xl, split - dy, xr, base]})      # the doors stretch
    return moves


def blend_prompt(brief: dict, dy: int) -> str:
    way = f"整体上移了 {dy} 像素" if dy > 0 else f"整体下移了 {-dy} 像素"
    return (f"这张照片里的{brief['target'] or '柜子'}刚被{way}（台面和上面的物品一起动的），接缝处可能有错位或重复。"
            "只修补接缝：让墙面、窗框、瓷砖线条、柜子侧边在接缝处自然连续，补全露出的背景，去掉重复的残影。"
            "柜门、台面、台面上物品的位置和大小保持完全不变。")
