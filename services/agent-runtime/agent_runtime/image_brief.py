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

_IMG = re.compile(r"(?:~/workspace/|/home/aiw/workspace/)?([^\s\n'\"]+?\.(?:png|jpe?g|webp|bmp))\b", re.I)
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
            ' "question": ""\n'
            "}\n"
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
