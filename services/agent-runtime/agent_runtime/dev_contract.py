"""v3.9.26 (task-830): the task contract of a dev_fix task, built by the system at step 0.

{goal, source_of_truth, generator_cmd, deliverables, allowed_paths, must_keep, acceptance, checks, voided_constraints,
 allow_remove, confirm}

* goal              the user's newest words (a continuation's 用户补充 / 【任务契约】 goal), not the old run's request
* source_of_truth   the script that generated the delivered artifact (provenance), else a source path the request /
                    thread names (task-830: ~/ai-workstation/news/aiw_news.py, never ~/aiw-news/<date>.html)
* deliverables      the artifacts that script makes: they are regenerated, never edited
* allowed_paths     where the fix may change files (the source and its folder)
* must_keep         what the previous deliverable had (fingerprint) + what the user says went missing
* acceptance/checks what proves the fix (verify_html clicks / selector counts), run through the verify tool
* voided            old constraints the new request overrides (不要修改脚本 …)
* confirm           large / ambiguous change (no source found, many files): ask the user first with a card

v3.9.29 (task-68b):
* a bare script name in the request ("这些都做进 aiw_news.py") is a source too: it is resolved against the scripts
  the thread already ran (thread signals script_runs, from `cd X && python3 Y.py`), else kept as unresolved;
* a continuation inherits the parent run's contract (source of truth, generator command, deliverables) when this run
  cannot find one itself (prev=);
* generated outputs (.html …) are not allowed paths when a generator source is known (task-68b listed
  ~/aiw-news/n1.html as the only allowed path);
* req_checks: acceptance checks derived from the user's words (dark/light mode, font family, image count, named
  sections, back button, file size, no embedded fonts) that verify_html evaluates and the done gate enforces;
* generator_cmd_fast: the generator with a cache flag (--from-cache …) for iterations; one full run before done.

Pure; block() is the text put in front of the model every turn.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import fingerprint as _fp
from . import intent as _in
from .provenance import ADHOC, SOURCE_EXT, ProvenanceBook, norm

ARTIFACT_OUT = re.compile(r"\.(html?|pdf|png|jpe?g|webp|svg|docx|xlsx|pptx|csv|zip|mp4)$", re.I)

_SRC_PATH = re.compile(r"(?:~|\$HOME|/home/[\w.-]+|/data/home|/root|/opt|/srv)(?:/[\w.@+-]+)+\.(?:py|js|mjs|ts|tsx|jsx|vue|css|"
                       r"html?|j2|jinja2?|sh)\b")
# v3.9.29: a bare script name ("做进 aiw_news.py"), resolved against the scripts the thread ran
_BARE_SRC = re.compile(r"(?<![\w/.~$-])([A-Za-z_][\w.-]*\.(?:py|js|mjs|ts|sh))\b")
FAST_FLAGS = ("--from-cache", "--cached", "--use-cache", "--offline", "--no-fetch", "--fast", "--skip-fetch")
_GOAL = re.compile(r"【接着上次继续做】\s*用户补充[:：]\s*(.+)$", re.S)
_CONTRACT_GOAL = re.compile(r"目标（本次请求）[:：]\s*(.+?)(?:\n|$)")
# "GitHub也没了" / "详情进不去" -> the thing the user says is broken or gone
_LOST = re.compile(r"([\w\u4e00-\u9fff]{1,12}?)(?:也|又|都|全)?(?:没了|不见了|丢了|消失了?|没有了)")
_CLICK = re.compile(r"(点击|点|click)[^，。；,;\n]{0,10}(进入不了|进不去|进不了|打不开|没反应|不了|无效)|(详情|弹窗|链接)[^，。；,;\n]{0,6}"
                    r"(进不去|进入不了|打不开|没反应|不了)", re.I)
_REMOVE = re.compile(r"(?:去掉|删掉|删除|移除|不要显示|隐藏|remove|drop)\s*(?:掉)?([\w\u4e00-\u9fff#.\-]{2,16})", re.I)
CONFIRM_FILES = int(os.environ.get("AIW_DEV_CONFIRM_FILES", "6") or 6)


def goal_of(prompt: str) -> str:
    p = prompt or ""
    m = _CONTRACT_GOAL.search(p)
    if m:
        return m.group(1).strip()[:600]
    m = _GOAL.search(p)
    if m:
        return m.group(1).strip()[:600]
    return re.sub(r"\s+", " ", p).strip()[:600]


def source_paths(text: str, known: Optional[list] = None) -> list[str]:
    """Absolute/~ source paths named in the text, then bare script names resolved against `known` (scripts the
    thread ran, newest last). An unresolved bare name is returned as it is (no folder)."""
    out = []
    for m in _SRC_PATH.finditer(text or ""):
        p = norm(m.group(0))
        if "/workspace/" in p and ADHOC.search(p):
            continue
        if p not in out:
            out.append(p)
    names = {os.path.basename(p) for p in out}
    for m in _BARE_SRC.finditer(text or ""):
        name = m.group(1)
        if name in names or ADHOC.search(name):
            continue
        names.add(name)
        hit = resolve_bare(name, known)
        if hit and hit not in out:
            out.append(hit)
        elif not hit and name not in out:
            out.append(name)
    return out[:20]


def resolve_bare(name: str, known: Optional[list]) -> Optional[str]:
    """aiw_news.py -> ~/ai-workstation/news/aiw_news.py when the thread ran it (newest run wins, workspace scratch last)."""
    hits = [norm(k) for k in (known or []) if os.path.basename(norm(k)) == name]
    if not hits:
        return None
    real = [h for h in hits if "/workspace/" not in h and not h.startswith("~/workspace")]
    return (real or hits)[-1] if "/" in (real or hits)[-1] else None


def default_cmd(src: str) -> Optional[str]:
    if not src or "/" not in src:
        return None
    d, n = os.path.dirname(src), os.path.basename(src)
    prog = "python3" if n.endswith(".py") else "node" if n.endswith((".js", ".mjs")) else "bash" if n.endswith(".sh") else None
    return f"cd {d} && {prog} {n}" if prog else None


def fast_cmd(gen_cmd: Optional[str], flags: Optional[list]) -> Optional[str]:
    """v3.9.29: the generator command with the first cache flag the source supports (found by grepping it)."""
    if not gen_cmd or not flags:
        return None
    f = next((x for x in FAST_FLAGS if x in flags), None)
    if not f or f in gen_cmd:
        return None
    m = re.search(r"(\.(?:py|js|mjs|ts|sh))(\s|$)", gen_cmd)
    if not m:
        return None
    return gen_cmd[:m.end(1)] + " " + f + gen_cmd[m.end(1):]


def is_fast_run(command: str) -> bool:
    return any(re.search(re.escape(f) + r"(\s|$|=)", command or "") for f in FAST_FLAGS)


# ------------------------------------------------------------------------- v3.9.29 requirement-derived checks
_DARK = re.compile(r"深色|浅色|暗色|暗黑|夜间|黑夜|dark\s*mode|light\s*mode|prefers-color-scheme|跟随系统|随系统", re.I)
_FONT = {"宋体": ["Songti", "SimSun", "STSong", "宋体", "Noto Serif", "Source Han Serif", "serif"],
         "黑体": ["Heiti", "SimHei", "STHeiti", "黑体", "Noto Sans", "PingFang", "sans-serif"],
         "楷体": ["Kaiti", "KaiTi", "STKaiti", "楷体"], "衬线": ["serif", "Songti", "SimSun", "Noto Serif"],
         "serif": ["serif"], "monospace": ["monospace", "Mono", "Consolas", "Menlo"], "等宽": ["monospace", "Mono"]}
_FONT_RE = re.compile(r"(宋体|黑体|楷体|衬线|serif|monospace|等宽)", re.I)
_IMG_N = re.compile(r"(\d+)\s*(张|幅|个)\s*(图|图片|配图)")
_IMG = re.compile(r"配图|大图|小图|缩略图|图片|thumbnail|hero\s*image", re.I)
_SECTION = re.compile(r"(?:加|增加|新增|添加|再加)(?:上)?(?:[一两二三四五六七八九十\d]+个)?(?:栏目|板块|区块|模块|section)[：:]?\s*"
                      r"([^。；;\n]{2,60})", re.I)
_SECTION2 = re.compile(r"([\w\u4e00-\u9fff ]{2,16}?)(?:栏目|板块|区块)")
_IDS = re.compile(r"(?<![\w&])#([A-Za-z][\w-]{1,30})\b")
_BACK = re.compile(r"返回首页|回到首页|返回主页|back to (home|top)", re.I)
_SIZE = re.compile(r"(文件|页面|体积|大小)[^。；\n]{0,6}(别|不要|不能|勿)[^。；\n]{0,4}(太)?大|不超过\s*(\d+)\s*(KB|MB|K|M)", re.I)
_NOEMBED = re.compile(r"字体[^。；\n]{0,8}(别|不要|不用|勿)[^。；\n]{0,6}嵌|不要?内嵌字体|don'?t embed (the )?font", re.I)


def _names(raw: str) -> list:
    raw = re.split(r"[，,。；;]|每个|每条|点进去|要求|并且|也要|都要", raw)[0]
    parts = [x.strip(" ：:「」“”\"'") for x in re.split(r"和|与|、|,|，|及|以及|跟", raw)]
    return [p for p in parts if 2 <= len(p) <= 16][:6]


def requirement_checks(goal: str) -> list[dict]:
    """Checks derived from the user's request. Each: {id, label, kind, keywords, ...} — verify_html evaluates kinds
    dark_mode / font / min_count / text / max_bytes / no_embedded_font / selector."""
    g = _in.strip_negated(goal or "") if hasattr(_in, "strip_negated") else (goal or "")
    raw = goal or ""
    out: list[dict] = []
    if _DARK.search(g):
        out.append({"id": "dark_mode", "label": "深色/浅色随系统切换", "kind": "dark_mode",
                    "keywords": ["深色", "浅色", "暗色", "prefers-color-scheme", "dark"]})
    fm = _FONT_RE.search(g)
    if fm:
        key = fm.group(1).lower() if fm.group(1).isascii() else fm.group(1)
        out.append({"id": "font", "label": f"标题字体：{fm.group(1)}", "kind": "font",
                    "selector": "h1, h2, h3, .title, .masthead h1", "any_of": _FONT.get(key, [fm.group(1)]),
                    "keywords": [fm.group(1), "字体"]})
    n = _IMG_N.search(g)
    if n or _IMG.search(g):
        want = int(n.group(1)) if n else (2 if ("大图" in g and "小图" in g) else 1)
        out.append({"id": "images", "label": f"配图至少 {want} 张", "kind": "min_count", "min": want,
                    "selector": "img, [style*='background-image'], picture", "keywords": ["图", "image"]})
    secs: list = []
    for m in _SECTION.finditer(g):
        secs += _names(m.group(1))
    for sid in _IDS.findall(raw):
        out.append({"id": f"sel_{sid}", "label": f"区块 #{sid}", "kind": "selector", "selector": f"#{sid}", "min": 1,
                    "keywords": [sid]})
    for nm in dict.fromkeys(secs):
        out.append({"id": "text_" + re.sub(r"\W+", "_", nm)[:20], "label": f"栏目「{nm}」", "kind": "text", "contains": nm,
                    "keywords": [nm]})
    if _BACK.search(g):
        out.append({"id": "back_home", "label": "返回首页按钮", "kind": "text", "contains": _BACK.search(g).group(0),
                    "keywords": ["返回首页", "返回"]})
    sm = _SIZE.search(raw)
    if sm:
        lim = 400 * 1024
        if sm.group(4):
            lim = int(sm.group(4)) * (1024 * 1024 if sm.group(5).upper().startswith("M") else 1024)
        out.append({"id": "size", "label": f"文件不超过 {lim // 1024} KB", "kind": "max_bytes", "max": lim,
                    "keywords": ["KB", "MB", "大小", "体积"]})
    if _NOEMBED.search(raw):
        out.append({"id": "no_font_embed", "label": "没有内嵌字体", "kind": "no_embedded_font",
                    "keywords": ["内嵌", "嵌入", "字体"]})
    return out[:16]


def build(prompt: str, context: str = "", book: Optional[ProvenanceBook] = None, baselines: Optional[dict] = None,
          old_instructions: str = "", script_runs: Optional[list] = None, prev: Optional[dict] = None,
          fast_flags: Optional[list] = None) -> dict:
    """script_runs: scripts the thread ran (bare-name resolution); prev: the parent run's contract (continuations);
    fast_flags: cache flags the generator source supports (the loop greps it)."""
    book = book or ProvenanceBook()
    baselines = baselines or {}
    prev = prev or {}
    goal = goal_of(prompt)
    known = list(script_runs or []) + [x for x in (prev.get("source_of_truth"),) if x]
    mentioned = [p for p in source_paths(prompt + "\n" + (context or ""), known)
                 if not re.search(r"\.html?$", p) or not book.get(p)]
    # the user's own words name the source ("做进 aiw_news.py"): that beats a provenance guess for another file
    goal_srcs = [p for p in source_paths(goal, known) if "/" in p and re.search(r"\.(py|js|mjs|ts|sh)$", p)
                 and "/workspace/" not in p]
    sot = book.source_of_truth()
    how = "provenance" if sot else ""
    if goal_srcs and not ADHOC.search(goal_srcs[0]) and (not sot or ADHOC.search(sot) or sot.startswith("~/workspace")
                                                          or "/workspace/" in sot):
        sot, how = goal_srcs[0], "named"
    if not sot:
        gens = [p for p in mentioned if re.search(r"\.(py|js|mjs|ts|sh)$", p) and "/" in p and "/workspace/" not in p]
        if gens:
            sot, how = gens[0], "mentioned"
    if not sot and prev.get("source_of_truth"):
        sot, how = prev["source_of_truth"], "parent"
    unresolved = [p for p in mentioned if "/" not in p]
    deliverables = [p for p in book.artifacts() if sot and book.generator(p) == sot] or \
                   [p for p in book.artifacts() if book.generator(p)][-1:]
    if not deliverables and prev.get("deliverables") and prev.get("source_of_truth") == sot:
        deliverables = list(prev["deliverables"])
    gen_cmd = book.generator_command(deliverables[-1]) if deliverables and book.get(deliverables[-1]) else None
    if not gen_cmd and prev.get("source_of_truth") == sot:
        gen_cmd = prev.get("generator_cmd")
    if not gen_cmd and sot and any(norm(k) == sot for k in (script_runs or [])):   # the thread ran it as a program
        gen_cmd = default_cmd(sot)
    allowed: list[str] = []
    if sot:
        allowed += [sot, os.path.dirname(sot)]
    gen_known = bool(sot and re.search(r"\.(py|js|mjs|ts|sh)$", sot))
    for p in mentioned:
        if "/" not in p:
            continue
        if gen_known and ARTIFACT_OUT.search(p):   # v3.9.29: a generated output is never an allowed path
            continue
        if p not in allowed and SOURCE_EXT.search(p) and p not in deliverables and "/workspace/" not in p:
            allowed.append(p)
    must: list[str] = []
    checks = {"selectors": [], "min_counts": {}, "clicks": []}
    for d in deliverables:
        fp = baselines.get(d) or baselines.get(os.path.basename(d))
        if not fp:
            continue
        must += _fp.must_keep(fp)
        for k, v in sorted((fp.get("data_attrs") or {}).items(), key=lambda kv: -kv[1])[:8]:
            sel = f"[{k}]"
            checks["selectors"].append(sel)
            checks["min_counts"][sel] = max(1, int(v * 0.8))
        for s in (fp.get("section_ids") or [])[:10]:
            sel = "#" + s.split("#", 1)[1]
            checks["selectors"].append(sel)
            checks["min_counts"][sel] = 1
    lost = []
    for m in _LOST.finditer(_in.strip_negated(goal)):
        w = re.sub(r"^(而且|并且|然后|还有|另外|结果|现在|但是|可是)", "", m.group(1)).strip()
        if len(w) >= 2 and w not in lost:
            lost.append(w)
    must += [f"{w}（用户指出丢失，必须恢复并保留）" for w in lost]
    acceptance = []
    if _CLICK.search(goal):
        acceptance.append("点击列表项/卡片能进入详情（verify_html 的 clicks：点击后详情/弹窗可见且有内容）")
    for w in lost:
        acceptance.append(f"{w} 恢复显示（verify_html 的 selectors 计数 > 0）")
    if deliverables:
        acceptance.append("修改源头后重新运行生成命令，验证的是重新生成的产物")
    acceptance.append("与上一版交付物对比：之前有的功能/区块/交互没有丢失（系统自动对比特征指纹）")
    allow_remove = [m.group(1).lower() for m in _REMOVE.finditer(_in.strip_negated(goal))]
    req = requirement_checks(goal)
    for r in req:
        acceptance.append(f"[{r['id']}] {r['label']}（verify_html 自动检查，done 总结里写明每项结果）")
    voided = _in.voided_constraints(old_instructions or prompt.replace(goal, ""), _in.DEV_FIX)
    confirm = (not sot) or len(allowed) > CONFIRM_FILES
    return {"goal": goal, "source_of_truth": sot, "source_how": how, "generator_cmd": gen_cmd,
            "generator_cmd_fast": fast_cmd(gen_cmd, fast_flags), "deliverables": deliverables,
            "allowed_paths": allowed[:20], "must_keep": must[:20], "acceptance": acceptance, "checks": checks,
            "req_checks": req, "unresolved_sources": unresolved,
            "voided_constraints": voided, "allow_remove": allow_remove, "confirm": confirm,
            "confirm_reason": ("没有找到生成交付物的源头脚本" + (f"（{'、'.join(unresolved[:3])} 没有在之前的运行记录里找到路径）"
                                                         if unresolved else "") if not sot else
                               f"涉及 {len(allowed)} 个文件/目录，改动面较大" if confirm else "")}


def block(c: Optional[dict]) -> str:
    if not c:
        return ""
    lines = ["\n\n【任务契约】(dev_fix — 系统根据对话和产物来源生成，每一步都要遵守)",
             f"目标：{c.get('goal')}"]
    if c.get("source_of_truth"):
        lines.append(f"源头（source of truth）：{c['source_of_truth']} —— 只改这里（及允许的路径），不要直接改生成的产物")
    else:
        lines.append("源头：未确定 —— 先找到生成交付物的脚本/源码再改，不要直接改产物")
    if c.get("generator_cmd"):
        lines.append(f"改完源头后必须重新生成：{c['generator_cmd'][:300]}")
    if c.get("generator_cmd_fast"):
        lines.append(f"迭代时可用快速生成（读缓存，不重新抓取）：{c['generator_cmd_fast'][:300]}；done 之前必须完整跑一次上面的生成命令")
    if c.get("deliverables"):
        lines.append("交付物（只能重新生成，禁止直接编辑）：" + "、".join(c["deliverables"][:6]))
    if c.get("allowed_paths"):
        lines.append("允许修改的路径：" + "、".join(c["allowed_paths"][:10]))
    if c.get("must_keep"):
        lines.append("必须保留：" + "；".join(c["must_keep"][:14]))
    if c.get("acceptance"):
        lines.append("验收（用 verify_html 取证，done 时写进总结）：\n" + "\n".join(f"  - {a}" for a in c["acceptance"]))
    if c.get("voided_constraints"):
        lines.append("已失效的旧约束（本次是修复代码，可以修改源头脚本）：" + "；".join(c["voided_constraints"]))
    if (c.get("git") or {}).get("mode") == "copy":
        lines.append(f"快照：改动前的源文件已复制到 {c['git'].get('snapdir')}，系统可回滚；不要自己 git checkout/stash 平台仓库")
    if (c.get("git") or {}).get("branch"):
        g = c["git"]
        lines.append(f"Git：仓库 {g.get('repo')} 已记录 HEAD {str(g.get('base') or '')[:10]}，修改在分支 {g['branch']} 上，"
                     "done 通过后系统自动提交；不要自己 git reset/checkout/stash")
    if c.get("source_of_truth"):
        lines.append(f"改源头用 files_patch（path={c['source_of_truth']}，old=原文精确片段，new=替换内容，返回 diff）；"
                     "不要写 fix_*.py 之类的补丁脚本，写了脚本就必须在下一步执行")
    if c.get("req_checks"):
        lines.append("需求检查（verify_html 自动跑，done 总结里逐项写「通过/未通过+原因」，未通过的必须列进 已知问题）：" +
                     "；".join(f"[{r['id']}] {r['label']}" for r in c["req_checks"][:12]))
    lines.append("done 之前系统会检查：源头有真实改动、改动后重新生成过、验收和回归对比通过、需求检查逐项有结果。")
    return "\n".join(lines)


def card(c: dict) -> tuple[str, list]:
    """The confirmation card for a large / ambiguous change."""
    q = ("这是一个修复代码的任务，开始前请确认任务契约：\n目标：" + str(c.get("goal") or "")[:200] +
         ("\n源头：" + c["source_of_truth"] if c.get("source_of_truth") else "\n源头：还没找到生成交付物的脚本") +
         ("\n允许修改：" + "、".join(c.get("allowed_paths") or [])[:200] if c.get("allowed_paths") else "") +
         (f"\n原因：{c.get('confirm_reason')}" if c.get("confirm_reason") else ""))
    return q, ["按这个契约开始", "我来补充源头/范围", "停止任务"]
