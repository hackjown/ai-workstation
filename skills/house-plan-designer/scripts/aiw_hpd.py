#!/usr/bin/env python3
"""AIW 适配层（house-plan-designer 内置技能）。

上游 skill（github.com/ZWD11/house-plan-designer，MIT）按 Windows/macOS + 真显卡写；AIW 的电脑是 Linux VM、多数没有显卡。
这个脚本只做 AIW 需要的胶水，不改设计器模板：

  python3 aiw_hpd.py doctor                     # 依赖自检，最后一行 HPD_DOCTOR {json}
  python3 aiw_hpd.py ensure                     # 缺什么装什么（pip --user、npm install、Chrome/Chromium），可重复执行
  python3 aiw_hpd.py from-cad cad/spec.json -o hpd/draft.json [--name 户型名]
                                                # cad-drawing 的 spec（同一任务已出过平面图时）→ HouseSpec 初稿
  python3 aiw_hpd.py make hpd/spec.json --out hpd --name 户型名 [--final 目录]
                                                # build.py → check.mjs → 摘要；最后一行 HPD_RESULT {json}
  python3 aiw_hpd.py from-image 原图.jpg -o hpd/draft.json [--name 户型名]
                                                # 图片路线第一步：cad-drawing 的照图识别 → from-cad → 房间围合检查
  python3 aiw_hpd.py imgcheck 原图.jpg --total-width W [--total-height H] --out hpd/a0
                                                # 图片路线第一步：跑一次 analyze.py 并判断这张图能不能自动识别墙；
                                                # 最后一行 HPD_IMGCHECK {route: analyze|dims, why, advice}
  python3 aiw_hpd.py qa 户型名-装修设计器.html --out hpd/qa [--full]
                                                # 验收：默认 --lite --quick（VM 无显卡），--full 跑完整一遍

所有命令都可以在任意目录执行；路径相对当前目录。
"""
from __future__ import annotations

import argparse
import json
import re
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_FILE = Path.home() / ".config" / "aiw" / "hpd.env"
PY_MODS = {"numpy": "numpy", "PIL": "pillow", "cv2": "opencv-python-headless", "fitz": "pymupdf", "ezdxf": "ezdxf"}
CHROMES = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
           "/snap/bin/chromium", "/opt/google/chrome/chrome"]


# ------------------------------------------------------------------------------------------------ environment
def _load_env() -> dict:
    env = dict(os.environ)
    if ENV_FILE.exists():
        for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    return env


def chrome_path(env: dict | None = None) -> str | None:
    env = env or _load_env()
    c = env.get("CHROME_PATH")
    if c and Path(c).exists():
        return c
    for p in CHROMES:
        if Path(p).exists():
            return p
    tools = Path.home() / "tools" / "chrome"
    if tools.exists():
        for p in sorted(tools.rglob("chrome-headless-shell")) + sorted(tools.rglob("chrome")):
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    return None


def _missing_py() -> list[str]:
    out = []
    for mod, pkg in PY_MODS.items():
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            out.append(pkg)
    return out


def doctor() -> dict:
    env = _load_env()
    node = shutil.which("node")
    nv = ""
    if node:
        try:
            nv = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            nv = "?"
    rep = {
        "skill": str(ROOT),
        "template": (ROOT / "assets" / "template.html").exists(),
        "python_missing": _missing_py(),
        "node": nv or None,
        "node_ok": bool(nv) and _node_major(nv) >= 18,
        "puppeteer": (HERE / "node_modules" / "puppeteer-core").exists(),
        "chrome": chrome_path(env),
        "cjk_font": bool(shutil.which("fc-list") and subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True).stdout.strip()),
    }
    rep["ok"] = bool(rep["template"] and not rep["python_missing"] and rep["node_ok"] and rep["puppeteer"] and rep["chrome"])
    return rep


def _node_major(v: str) -> int:
    try:
        return int(v.lstrip("v").split(".")[0])
    except Exception:  # noqa: BLE001
        return 0


def _sudo() -> list[str]:
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return ["sudo", "-n"]
    return None  # type: ignore[return-value]


def _run(cmd, **kw) -> int:
    print("+", " ".join(cmd) if isinstance(cmd, list) else cmd, flush=True)
    return subprocess.run(cmd, **kw).returncode


def ensure() -> dict:
    rep = doctor()
    sudo = _sudo()
    miss = rep["python_missing"]
    if miss:
        base = [sys.executable, "-m", "pip", "install", "--user", "-q"]
        if _run(base + miss) != 0:
            _run(base + ["--break-system-packages"] + miss)
    if not rep["node_ok"]:
        if sudo is not None:
            _run(sudo + ["apt-get", "update", "-qq"])
            _run(sudo + ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-qq", "nodejs", "npm"])
        if _node_major(subprocess.run(["node", "--version"], capture_output=True, text=True).stdout if shutil.which("node") else "") < 18:
            # 发行版的 node 太旧：装一个用户级 node（不需要 sudo）
            _user_node()
    if not (HERE / "node_modules" / "puppeteer-core").exists():
        env = dict(os.environ, PUPPETEER_SKIP_DOWNLOAD="1", PUPPETEER_SKIP_CHROMIUM_DOWNLOAD="1")
        npm = shutil.which("npm") or str(Path.home() / ".local" / "node" / "bin" / "npm")
        _run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=str(HERE), env=env)
    if not chrome_path():
        ok = False
        if sudo is not None:
            _run(sudo + ["apt-get", "update", "-qq"])
            for pkg in ("chromium", "chromium-browser"):
                if _run(sudo + ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-qq", pkg]) == 0 and chrome_path():
                    ok = True
                    break
        if not ok:
            # 没有 sudo / 包里没有：下载 Chrome for Testing 的 headless shell 到 ~/tools/chrome
            npx = shutil.which("npx") or str(Path.home() / ".local" / "node" / "bin" / "npx")
            _run([npx, "-y", "@puppeteer/browsers", "install", "chrome-headless-shell@stable",
                  "--path", str(Path.home() / "tools" / "chrome")])
    if sudo is not None and shutil.which("fc-list"):
        zh = subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True).stdout.strip()
        if not zh:   # 没有中文字体时设计器界面和截图里的中文是方块
            _run(sudo + ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "-qq", "fonts-noto-cjk"])
    c = chrome_path()
    if c:
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = [ln for ln in (ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else [])
                 if not ln.startswith("CHROME_PATH=")]
        ENV_FILE.write_text("\n".join(lines + [f"CHROME_PATH={c}"]) + "\n", encoding="utf-8")
    return doctor()


def _user_node():
    import platform
    arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "x64")
    ver = "v20.18.1"
    url = f"https://nodejs.org/dist/{ver}/node-{ver}-linux-{arch}.tar.xz"
    dst = Path.home() / ".local" / "node"
    tmp = Path("/tmp/node.tar.xz")
    if _run(["curl", "-fsSL", "--retry", "3", "-o", str(tmp), url]) == 0:
        dst.mkdir(parents=True, exist_ok=True)
        _run(["tar", "-xJf", str(tmp), "-C", str(dst), "--strip-components=1"])
        bin_ = Path.home() / ".local" / "bin"
        bin_.mkdir(parents=True, exist_ok=True)
        for b in ("node", "npm", "npx"):
            link = bin_ / b
            if not link.exists():
                link.symlink_to(dst / "bin" / b)
        os.environ["PATH"] = f"{bin_}:{dst / 'bin'}:" + os.environ.get("PATH", "")


# ------------------------------------------------------------------------------------------------ cad-drawing → HouseSpec
DOOR_TYPE = {"swing": "door", "door": "door", "double": "double", "sliding": "sliding", "opening": "hole",
             "hole": "hole", "folding": "folding", "pocket": "pocket", "unequal": "unequal"}
FURN = {"bed": "bed18", "sofa": "sofa", "dining_table": "diningTable", "table": "diningTable", "desk": "desk",
        "counter": "counter", "wardrobe": "wardrobe", "tv": "tvCabinet",  # stove / sink 已含在整体橱柜里
        "toilet": "toilet", "basin": "vanity", "shower": "shower", "bath": "bathtub"}


def _centroid(pts):
    a = cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % n]
        c = x1 * y2 - x2 * y1
        a += c; cx += (x1 + x2) * c; cy += (y1 + y2) * c
    if abs(a) < 1e-9:
        return [sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n]
    return [cx / (3 * a), cy / (3 * a)]


def _join_gaps(walls: list, ops_at: list, tol: float = 60.0, door_min: float = 550.0, door_max: float = 1300.0,
               op_max: float = 3600.0):
    """Join collinear wall pieces across a gap that holds an opening (≤ op_max) or is door-sized (door_min..door_max).
    Returns (walls, [(wall, from, to)] of door-sized gaps with no opening in them)."""
    def key(w):
        (x1, y1), (x2, y2) = w["a"], w["b"]
        if abs(y1 - y2) < 1e-6:
            return "h", y1, min(x1, x2), max(x1, x2)
        if abs(x1 - x2) < 1e-6:
            return "v", x1, min(y1, y2), max(y1, y2)
        return None
    straight = [(key(w), w) for w in walls]
    others = [w for k, w in straight if k is None]
    out, doors = list(others), []
    for o in ("h", "v"):
        ws = sorted([(k, w) for k, w in straight if k and k[0] == o], key=lambda kw: kw[0][1])
        groups: list[list] = []
        for k, w in ws:                      # same line within tol
            if groups and abs(groups[-1][0][0][1] - k[1]) <= tol:
                groups[-1].append((k, w))
            else:
                groups.append([(k, w)])
        for g in groups:
            g.sort(key=lambda kw: kw[0][2])
            line = sum(k[1] for k, _ in g) / len(g)
            cur = None
            for k, w in g:
                lo, hi = k[2], k[3]
                if cur is None:
                    cur = {"lo": lo, "hi": hi, "t": w["t"], "w": w, "gaps": []}
                    continue
                gap = lo - cur["hi"]
                has_op = any(cur["hi"] - 5 <= (p[0] if o == "h" else p[1]) <= lo + 5 and
                             abs((p[1] if o == "h" else p[0]) - line) <= max(cur["t"], w["t"]) / 2 + 80 for p in ops_at)
                if gap <= 0 or (has_op and gap <= op_max) or door_min <= gap <= door_max or gap < door_min and gap < 200:
                    if gap > 0 and not has_op and door_min <= gap <= door_max:
                        cur["gaps"].append((cur["hi"], lo))
                    cur["hi"] = max(cur["hi"], hi)
                    cur["t"] = max(cur["t"], w["t"])
                    continue
                out.append(_mk_wall(o, line, cur)); doors += [(out[-1], f, t) for f, t in cur["gaps"]]
                cur = {"lo": lo, "hi": hi, "t": w["t"], "w": w, "gaps": []}
            if cur:
                out.append(_mk_wall(o, line, cur)); doors += [(out[-1], f, t) for f, t in cur["gaps"]]
    return out, doors


def _mk_wall(o: str, line: float, cur: dict) -> dict:
    line = round(line, 1)
    a, b = ([cur["lo"], line], [cur["hi"], line]) if o == "h" else ([line, cur["lo"]], [line, cur["hi"]])
    w = {k: v for k, v in cur["w"].items() if k not in ("a", "b", "t", "openings")}
    w.update({"a": [round(a[0], 1), round(a[1], 1)], "b": [round(b[0], 1), round(b[1], 1)], "t": cur["t"], "openings": []})
    return w


def _close_ends(walls: list, reach: float = 2700.0, door_min: float = 550.0, door_max: float = 1300.0) -> list:
    """v3.9.9 (task-9c4): a traced wall that stops in mid-air (its door / its thin partition was not traced as a wall)
    leaves the room open. Each free end of a straight wall is run on along its own direction to the first wall it
    meets within `reach`: a short run is plain wall, a door-sized run gets a door, a longer one a hole (a pass-through
    the room check still treats as its edge). Returns [(wall, from, to, type)] for the openings to add."""
    def seg(w):
        (x1, y1), (x2, y2) = w["a"], w["b"]
        if abs(y1 - y2) < 1e-6:
            return "h", y1, min(x1, x2), max(x1, x2)
        if abs(x1 - x2) < 1e-6:
            return "v", x1, min(y1, y2), max(y1, y2)
        return None

    def touches(pt, me):
        for w in walls:
            if w is me:
                continue
            k = seg(w)
            if not k:
                continue
            o, line, lo, hi = k
            pad = w["t"] / 2 + me["t"] / 2 + 60
            u, v = (pt[0], pt[1]) if o == "h" else (pt[1], pt[0])
            if lo - pad <= u <= hi + pad and abs(v - line) <= pad:
                return True
        return False

    adds = []
    for w in list(walls):
        k = seg(w)
        if not k:
            continue
        o, line, lo, hi = k
        for end, sign in ((lo, -1), (hi, 1)):
            pt = [end, line] if o == "h" else [line, end]
            if touches(pt, w):
                continue
            best = None
            for x in walls:
                kx = seg(x)
                if x is w or not kx:
                    continue
                ox, lx, lox, hix = kx
                if ox == o:
                    if abs(lx - line) <= max(w["t"], x["t"]) / 2 + 60:
                        d = (lox - end) if sign > 0 else (end - hix)
                        hit = lox if sign > 0 else hix
                    else:
                        continue
                else:
                    if not (lox - x["t"] / 2 <= line <= hix + x["t"] / 2):
                        continue
                    d = (lx - end) * sign
                    hit = lx
                if 0 < d <= reach and (best is None or d < best[0]):
                    best = (d, hit)
            if not best:
                continue
            d, hit = best
            f, t = sorted((end, hit))
            if o == "h":
                w["a"], w["b"] = [min(lo, f), line], [max(hi, t), line]
            else:
                w["a"], w["b"] = [line, min(lo, f)], [line, max(hi, t)]
            lo, hi = min(lo, f), max(hi, t)
            if d >= door_min:
                adds.append((w, f, t, "door" if d <= door_max else "hole"))
    # two free ends whose runs cross (an L corner the trace lost, often with the door beside it): meet at the crossing;
    # the longer run carries the door when it is door-sized
    free = []
    for w in walls:
        k = seg(w)
        if not k:
            continue
        o, line, lo, hi = k
        for end, sign in ((lo, -1), (hi, 1)):
            pt = [end, line] if o == "h" else [line, end]
            if not touches(pt, w):
                free.append((w, o, line, end, sign))
    used = set()
    for i, (w1, o1, l1, e1, s1) in enumerate(free):
        for j, (w2, o2, l2, e2, s2) in enumerate(free):
            if j <= i or o1 == o2 or i in used or j in used or w1 is w2:
                continue
            d1, d2 = (l2 - e1) * s1, (l1 - e2) * s2       # w1 runs to w2's line, w2 runs to w1's line
            if not (-60 <= d1 <= reach and -60 <= d2 <= reach and max(d1, d2) > 0):
                continue
            runs = []
            for w, o, l, e, sg, hit, d in ((w1, o1, l1, e1, s1, l2, d1), (w2, o2, l2, e2, s2, l1, d2)):
                k = seg(w)
                lo, hi = min(k[2], hit), max(k[3], hit)
                w["a"], w["b"] = ([lo, l], [hi, l]) if o == "h" else ([l, lo], [l, hi])
                runs.append((d, w, min(e, hit), max(e, hit)))
            d, w, f, t = max(runs, key=lambda r: r[0])
            if door_min <= d <= door_max:
                adds.append((w, f, t, "door"))
            used.update((i, j))
    return adds


def _kit_dir() -> Path | None:
    for d in (Path.home() / ".claude" / "skills" / "cad-drawing", ROOT.parent / "cad-drawing",
              Path(os.environ.get("VM_AGENT_FILES_ROOT", str(Path.home() / "workspace"))) / ".aiw" / "cad-drawing"):
        if (d / "drawkit.py").exists():
            return d
    return None


def room_closure(hs: dict, res: float = 50.0) -> dict:
    """Which room labels sit in a closed area: walls rasterised (openings other than windows count as the room's edge,
    as the designer does), flood fill from outside. Returns {"open": [names outside], "shared": [[names in one area]]}."""
    import numpy as np
    import cv2
    ws = [w for w in hs.get("walls") or [] if w.get("a") and w.get("b")]
    if not ws:
        return {"open": [r.get("name") for r in hs.get("rooms") or []], "shared": []}
    xs = [p[0] for w in ws for p in (w["a"], w["b"])]
    ys = [p[1] for w in ws for p in (w["a"], w["b"])]
    x0, y0 = min(xs) - 1000, min(ys) - 1000
    W, H = int((max(xs) - x0 + 1000) / res) + 1, int((max(ys) - y0 + 1000) / res) + 1
    g = np.zeros((H, W), np.uint8)
    P = lambda x, y: (int(round((x - x0) / res)), int(round((y - y0) / res)))
    for w in ws:
        th = max(1, int(round(float(w.get("t") or 120) / res)))
        cv2.line(g, P(*w["a"]), P(*w["b"]), 255, th + 1)
    lab = np.zeros((H + 2, W + 2), np.uint8)
    area = g.copy()
    cv2.floodFill(area, lab, (0, 0), 128)
    out, regions = [], {}
    n, comp = cv2.connectedComponents((area == 0).astype(np.uint8), 4)
    for r in hs.get("rooms") or []:
        cx, cy = P(*r["at"])
        if not (0 <= cx < W and 0 <= cy < H) or area[cy, cx] == 128:
            out.append(r.get("name"))
        elif area[cy, cx] == 0:
            regions.setdefault(int(comp[cy, cx]), []).append(r.get("name"))
    return split_closure({"open": out, "shared": [v for v in regions.values() if len(v) > 1]})


# 3.9.11 (task-a7b46fccd8): rooms that are open by nature. The run chased 阳台(半赠送) / 空调位 / 可拓展空间 for 100 steps.
OPEN_BY_NATURE = re.compile(r"阳台|露台|平台|空调|设备|花园|庭院|天井|可拓展|赠送|飘窗|走廊|过道|连廊|入户")
OPEN_PLAN = re.compile(r"客厅|餐厅|起居|客餐|玄关|门厅|过道|走廊|开放")


def split_closure(clo: dict) -> dict:
    """open / shared -> what needs a wall (open, shared) and what is fine as drawn (open_ok, shared_ok): a balcony, an AC
    ledge or an expansion space is open on purpose; living + dining (+ hall) in one area is an open plan."""
    names = lambda v: [str(x or "") for x in v]
    open_fix = [n for n in names(clo.get("open") or []) if not OPEN_BY_NATURE.search(n)]
    open_ok = [n for n in names(clo.get("open") or []) if OPEN_BY_NATURE.search(n)]
    shared_fix, shared_ok = [], []
    for grp in clo.get("shared") or []:
        g = names(grp)
        rest = [n for n in g if not OPEN_BY_NATURE.search(n)]
        if len(rest) <= 1 or all(OPEN_PLAN.search(n) for n in rest):
            shared_ok.append(g)
        else:
            shared_fix.append(g)
    return {"open": open_fix, "shared": shared_fix, "open_ok": open_ok, "shared_ok": shared_ok}


def dark_screenshot(image: str) -> dict | None:
    """A black-background CAD screenshot (mostly dark, thin coloured lines): route it to upstream cadshot.py (b94183f)."""
    try:
        from PIL import Image, ImageStat
        im = Image.open(image).convert("L")
        im.thumbnail((400, 400))
        if ImageStat.Stat(im).mean[0] >= 60:
            return None
    except Exception:  # noqa: BLE001
        return None
    return {"ok": False, "route": "cadshot", "dark": True,
            "error": "这是黑底 CAD 截图，照图识别按“深色 = 墙”认不了",
            "next": (f"python3 {HERE}/cadshot.py {image} --out <工作目录>/shot 看 layers.png 哪个颜色是墙；按轴线尺寸加 "
                     "--x-dims/--y-dims 再跑一次拿 mmPerPx 和 axis_chain；然后 analyze.py --wall-color cyan --scale <mmPerPx>；"
                     f"尺寸数字 python3 {HERE}/dimocr.py {image} --color green --scale <mmPerPx> --out <工作目录>/ocr")}


def from_image(image: str, out: str, name: str | None = None, extra: list | None = None) -> dict:
    """v3.9.9 (task-9c4): a clean picture goes through cad-drawing's deterministic tracer first (照图画图: walls, door /
    window gaps, scale from the dimension numbers, room names by OCR, in seconds), then from-cad. task-9c4 skipped it,
    hand-measured pixel peaks for 35+ steps and merged six rooms into one; the same picture traced in ~10 s."""
    dark = dark_screenshot(image)
    if dark:   # v3.9.16: a black-background CAD screenshot: "dark = wall" tracing sees one big wall
        return dark
    kit = _kit_dir()
    if not kit:
        return {"ok": False, "route": "analyze", "error": "没有 cad-drawing 技能（~/.claude/skills/cad-drawing），改用 imgcheck / analyze.py"}
    od = Path(out).resolve().parent
    od.mkdir(parents=True, exist_ok=True)
    cad_spec = od / "cad-spec.json"
    r = subprocess.run([sys.executable, str(kit / "drawkit.py"), "auto", str(Path(image).resolve()), "--out", str(cad_spec), "--install-ocr"] + list(extra or []),
                       capture_output=True, text=True, timeout=600)
    line = next((x for x in reversed(r.stdout.splitlines()) if x.startswith("DRAWKIT_AUTO ")), "")
    if r.returncode != 0 or not cad_spec.exists():
        return {"ok": False, "route": "analyze", "error": "照图识别没成功：" + (r.stderr or r.stdout)[-400:].strip(),
                "next": "图上没有可读的尺寸数字时可加 --mm-per-px（总宽mm ÷ 外墙像素宽）再试一次；仍不行就跑 imgcheck 走 analyze / 尺寸链"}
    info = {}
    try:
        info = json.loads(line.split(" ", 1)[1]) if line else {}
    except Exception:  # noqa: BLE001
        pass
    res = from_cad(str(cad_spec), out, name)
    hs = json.loads(Path(out).read_text(encoding="utf-8"))
    clo = room_closure(hs)
    hs.setdefault("_aiw", {})["closure"] = clo
    Path(out).write_text(json.dumps(hs, ensure_ascii=False, indent=1), encoding="utf-8")
    res.update(ok=True, route="trace", cad_spec=str(cad_spec), warnings=(info.get("warnings") or [])[:4],
               open_rooms=clo["open"], shared_rooms=clo["shared"],
               open_ok=clo.get("open_ok") or [], shared_ok=clo.get("shared_ok") or [],
               next=("直接在这个 spec.json 上改（读入→改几面墙→写回），不要另写脚本从 cad-spec 重建墙。"
                     + ("只补 open_rooms / shared_rooms 列出的房间缺的墙；" if clo["open"] or clo["shared"] else
                        "墙已经够用，不用补墙；")
                     + "open_ok / shared_ok（阳台、空调位、可拓展空间、开放式客餐厅）本来就不封闭，不要补。"
                     "make 最多 3 轮，然后第 5 步摆家具 + autoElec，再 --final 交付。门（_aiw=gap）开向不对就改 hinge / into。"))
    return res


def from_cad(spec_path: str, out: str, name: str | None = None) -> dict:
    """cad-drawing 的 spec 是 y 向下的 mm（y_down=true，和 HouseSpec 一样）。墙取 drawkit 的墙段（traced 或房间网格推出来的），
    门窗按位置挂到最近的墙上，房间名变成标签点，家具块换成设计器的 key。只是初稿：之后照 SKILL.md 第 4 步 check 并修。"""
    sp_path = Path(spec_path).resolve()
    spec = json.loads(sp_path.read_text(encoding="utf-8"))
    if spec.get("kind", "floor_plan") != "floor_plan":
        raise SystemExit("from-cad 只接受户型平面图 spec（kind=floor_plan）")
    kit = _kit_dir()
    if not kit:
        raise SystemExit("找不到 cad-drawing 的 drawkit.py（~/.claude/skills/cad-drawing）")
    sys.path.insert(0, str(kit))
    import drawkit  # type: ignore

    plan = drawkit.Plan(spec)
    m = plan.m
    Y = m.Y                                  # y_down 时是自身的逆：y_up = H - y_down
    segs = plan.wall_segments()
    walls = []
    for s in segs:
        if s["o"] == "h":
            a, b = [s["u"], Y(s["c"])], [s["v"], Y(s["c"])]
        else:
            a, b = [s["c"], Y(s["u"])], [s["c"], Y(s["v"])]
            if a[1] > b[1]:
                a, b = b, a
        walls.append({"a": [round(a[0], 1), round(a[1], 1)], "b": [round(b[0], 1), round(b[1], 1)],
                      "t": round(float(s["t"]), 1), "openings": []})
    for e in spec.get("hollow_walls") or []:
        walls.append({"a": e["a"], "b": e["b"], "t": e.get("t", plan.t_int), "bearing": False, "openings": []})

    # v3.9.9 (task-9c4): drawtrace breaks a wall at every door / window, so each opening sat in a GAP between two
    # collinear walls and attach() found no wall for any of them (8 of 8 unplaced), and the door gaps left every room
    # open (the check merged 6 rooms into one 63 m2 shape). Collinear pieces are joined across a gap that holds an
    # opening of the spec, or across a door-sized gap (it becomes a door), so rooms close and openings land on walls.
    ops_at = [d["at"] for d in (spec.get("doors") or []) if d.get("at")]
    ops_at += [w_["at"] for w_ in (spec.get("windows") or []) if w_.get("at")]
    ops_at += [[(w_["a"][0] + w_["b"][0]) / 2, (w_["a"][1] + w_["b"][1]) / 2] for w_ in (spec.get("windows") or [])
               if not w_.get("at") and w_.get("a") and w_.get("b")]
    walls, gap_doors = _join_gaps(walls, ops_at)
    end_runs = _close_ends(walls)

    rooms_by_id = {}
    rooms = []
    for r in spec.get("rooms") or []:
        pts = r.get("poly") or ([[r["box"][0], r["box"][1]], [r["box"][2], r["box"][1]], [r["box"][2], r["box"][3]],
                                 [r["box"][0], r["box"][3]]] if r.get("box") else None)
        at = r.get("label_at") or (_centroid(pts) if pts else None)
        if not at or not r.get("name"):
            continue
        at = [round(at[0], 1), round(at[1], 1)]
        rooms.append({"at": at, "name": r["name"]})
        rooms_by_id[r.get("id") or r["name"]] = at
    for L in spec.get("free_labels") or []:
        at = L.get("at")
        if not at and L.get("at_px") and (spec.get("source") or {}).get("transform"):
            t = spec["source"]["transform"]
            at = [(L["at_px"][0] - t["bx"]) / t["ax"], (L["at_px"][1] - t["by"]) / t["ay"]]
        if at and L.get("text"):
            rooms.append({"at": [round(at[0], 1), round(at[1], 1)], "name": L["text"]})

    unplaced = []

    def attach(at, width, op):
        best, bd = None, 1e18
        for w in walls:
            (x1, y1), (x2, y2) = w["a"], w["b"]
            horiz = abs(y1 - y2) < 1e-6
            if horiz:
                lo, hi = sorted((x1, x2)); d = abs(at[1] - y1); along = at[0]
            else:
                lo, hi = sorted((y1, y2)); d = abs(at[0] - x1); along = at[1]
            if lo - 5 <= along <= hi + 5 and d <= w["t"] / 2 + 80 and d < bd:
                best, bd = (w, horiz, lo, hi, along), d
        if not best:
            unplaced.append({"at": at, "width": width, "type": op.get("type")})
            return
        w, horiz, lo, hi, along = best
        f, t = max(lo, along - width / 2), min(hi, along + width / 2)
        op.update({"from": round(f, 1), "to": round(t, 1)})
        w["openings"].append(op)

    for d in spec.get("doors") or []:
        if not d.get("at"):
            continue
        op = {"type": DOOR_TYPE.get(d.get("type") or "swing", "door")}
        if op["type"] not in ("hole", "sliding"):
            op["hinge"] = "to" if d.get("hinge") == "end" else "from"
            if d.get("into") in rooms_by_id:
                op["into"] = rooms_by_id[d["into"]]
        attach(d["at"], float(d.get("width") or 900), op)
    for wdw in spec.get("windows") or []:
        if wdw.get("at"):
            attach(wdw["at"], float(wdw.get("width") or 1500), {"type": "window"})
        elif wdw.get("a") and wdw.get("b"):
            a, b = wdw["a"], wdw["b"]
            attach([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], math.hypot(b[0] - a[0], b[1] - a[1]), {"type": "window"})
    for w, f, t, typ in [(w, f, t, "door") for w, f, t in gap_doors] + end_runs:
        # a gap / an open run the trace saw no door in: a plain door (or a pass-through) there, so the room closes
        if not any(o["from"] < t and o["to"] > f for o in w["openings"]):
            op = {"type": typ, "from": round(f, 1), "to": round(t, 1), "_aiw": "gap"}
            if typ == "door":
                op["hinge"] = "from"
            w["openings"].append(op)
    for w in walls:
        if not w["openings"]:
            w.pop("openings")

    furniture = []
    rot_by_head = {"top": 0, "right": 90, "bottom": 180, "left": 270}
    for f in spec.get("furniture") or []:
        key = FURN.get(f.get("type"))
        if not key or not f.get("box"):
            continue
        x0, y0, x1, y1 = f["box"]
        x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1))
        cx, cy, bw, bd = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
        rot = rot_by_head.get(f.get("head") or "", None)
        if rot is None:                       # 背靠最近的墙
            best = (1e18, 0)
            for w in walls:
                (ax, ay), (bx, by) = w["a"], w["b"]
                if abs(ay - by) < 1e-6 and min(ax, bx) - 50 <= cx <= max(ax, bx) + 50:
                    best = min(best, (abs(ay - y0), 0), (abs(ay - y1), 180))
                elif abs(ax - bx) < 1e-6 and min(ay, by) - 50 <= cy <= max(ay, by) + 50:
                    best = min(best, (abs(ax - x1), 90), (abs(ax - x0), 270))
            rot = best[1]
        w_, d_ = (bw, bd) if rot in (0, 180) else (bd, bw)
        furniture.append({"key": key, "at": [round(cx, 1), round(cy, 1)], "rot": rot, "w": round(w_), "d": round(d_)})

    hs = {"name": name or spec.get("title") or "户型", "height": 2800, "phase": "renovate", "walls": walls,
          "rooms": rooms, "furniture": furniture, "autoElec": True,
          "_aiw": {"from": str(sp_path), "unplaced_openings": unplaced,
                   "note": "由 cad-drawing spec 转来的初稿：墙为墙中线；门窗按位置挂到最近墙上；家具只是占位，按 SKILL.md 第 5 步重摆"}}
    src = spec.get("source") or {}
    t = src.get("transform")
    img = src.get("image")
    if t and img:
        ip = Path(img)
        if not ip.is_absolute():
            for base in (sp_path.parent, sp_path.parent.parent, Path(os.environ.get("VM_AGENT_FILES_ROOT", str(Path.home() / "workspace")))):
                if (base / ip).exists():
                    ip = base / ip
                    break
        if ip.exists():
            mmpp = (1 / t["ax"] + 1 / t["ay"]) / 2
            hs["underlay"] = {"image": str(ip.resolve()), "mmPerPx": round(mmpp, 4),
                              "x": round(-t["bx"] / t["ax"], 1), "y": round(-t["by"] / t["ay"], 1)}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(hs, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"out": str(Path(out).resolve()), "walls": len(walls), "rooms": len(rooms), "furniture": len(furniture),
            "openings": sum(len(w.get("openings", [])) for w in walls), "unplaced_openings": len(unplaced),
            "underlay": "underlay" in hs}


# ------------------------------------------------------------------------------------------------ build + check
def _node_env() -> dict:
    env = _load_env()
    c = chrome_path(env)
    if c:
        env["CHROME_PATH"] = c
    extra = [str(Path.home() / ".local" / "bin"), str(Path.home() / ".local" / "node" / "bin")]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    return env


def _summary(rep: dict) -> dict:
    if rep.get("floors"):                       # 整栋楼：逐层合并
        fl = [_summary(dict(f, errors=[])) for f in rep["floors"]]
        out = {"floors": fl, "errors": len(rep.get("errors") or [])}
        for k in ("freeWallEnds", "wallsDropped", "wallsSkewed", "openingsHidden", "labelsOutsideRooms", "openingsDropped",
                  "issues_error", "issues_warn"):
            out[k] = sum(int(f.get(k) or 0) for f in fl)
        out["must_fix"] = [f"{i + 1}层 {x}" for i, f in enumerate(fl) for x in f.get("must_fix", [])][:30]
        return out
    iss = rep.get("issues") or []
    def lvl(x):
        s = x if isinstance(x, str) else (x.get("level") or x.get("severity") or "")
        s = s if isinstance(s, str) else ""
        return "error" if "[error]" in s or s == "error" else "warn" if "[warn]" in s or s == "warn" else "info"
    levels = [lvl(x) for x in iss]
    keys = ("freeWallEnds", "wallsDropped", "wallsSkewed", "openingsHidden", "labelsOutsideRooms", "openingsDropped", "errors")
    out = {k: (len(rep[k]) if isinstance(rep.get(k), list) else rep.get(k)) for k in keys if k in rep}
    out["issues_error"] = levels.count("error")
    out["issues_warn"] = levels.count("warn")
    out["rooms"] = [{"name": r.get("name"), "area": r.get("area") or r.get("area_m2"), "size_mm": r.get("size_mm")}
                    for r in (rep.get("rooms") or [])][:40]
    out["must_fix"] = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
                       for x, l in zip(iss, levels) if l != "info"][:30]
    # v3.9.16 (upstream b94183f): dimension-chain reconciliation — spec dims / expectDims against the model
    dim = [str(x) for x in (rep.get("dimIssues") or [])] + [str(x) for x in (rep.get("dimConflicts") or [])]
    if dim:
        out["dimIssues"] = len(dim)
        out["must_fix"] = (out["must_fix"] + ["尺寸对账：" + x for x in dim])[:40]
    return out


def make(spec: str, out: str, name: str, final: str | None) -> dict:
    out_d = Path(out); out_d.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    check_html = out_d / "check.html"
    r = subprocess.run([py, str(HERE / "build.py"), spec, "-o", str(check_html)], capture_output=True, text=True)
    if r.returncode:
        return {"ok": False, "stage": "build", "error": (r.stderr or r.stdout)[-2000:]}
    env = _node_env()
    if not env.get("CHROME_PATH"):
        return {"ok": False, "stage": "check", "error": "没有 Chrome/Chromium：先跑 python3 aiw_hpd.py ensure"}
    chk = out_d / "check"
    r2 = subprocess.run(["node", str(HERE / "check.mjs"), str(check_html), str(chk)], capture_output=True, text=True,
                        env=env, timeout=900)
    res = {"ok": r2.returncode == 0, "stage": "check", "html_check": str(check_html.resolve()),
           "shots": {k: str((chk / f).resolve()) for k, f in (("plan2d", "plan2d.png"), ("overlay", "overlay.png"),
                                                                 ("view3d", "view3d.png")) if (chk / f).exists()}}
    rp = chk / "report.json"
    if rp.exists():
        res["report"] = _summary(json.loads(rp.read_text(encoding="utf-8")))
        rep = res["report"]
        res["clean"] = (not rep.get("errors") and not rep.get("freeWallEnds") and not rep.get("wallsDropped")
                        and not rep.get("wallsSkewed") and not rep.get("openingsHidden")
                        and rep.get("issues_error", 0) == 0 and rep.get("issues_warn", 0) == 0)
        # 3.9.11: good enough to furnish and deliver (free wall ends at a balcony rail or an AC ledge never go away)
        res["can_finish"] = bool(not rep.get("errors") and not rep.get("wallsDropped") and rep.get("issues_error", 0) == 0)
        if not final:
            rf = out_d / ".rounds"
            try:
                n = int(rf.read_text() or 0) + 1 if rf.exists() else 1
            except ValueError:
                n = 1
            rf.write_text(str(n))
            res["round"] = n
            if n >= 3 or (res["can_finish"] and not res["clean"] and n >= 2):
                res["next"] = (f"第 {n} 轮检查：别再改墙了。按 SKILL.md 第 5 步摆家具 + autoElec（同一个 spec），然后 make --final 交付，"
                               "剩下的问题在交付说明里写清楚。")
    else:
        res["error"] = (r2.stderr or r2.stdout)[-2000:]
    if final:
        fd = Path(final); fd.mkdir(parents=True, exist_ok=True)
        fh = fd / f"{name}-装修设计器.html"
        r3 = subprocess.run([py, str(HERE / "build.py"), spec, "-o", str(fh)], capture_output=True, text=True)
        if r3.returncode == 0:
            res["html"] = str(fh.resolve())
            for k, p in list(res["shots"].items()):
                dst = fd / f"{name}-{k}.png"
                shutil.copyfile(p, dst)
                res["shots"][k] = str(dst.resolve())
    return res


def qa(html: str, out: str, full: bool) -> int:
    args = ["node", str(HERE / "qa.mjs"), html, out] + ([] if full else ["--lite", "--quick"])
    rc = subprocess.run(args, env=_node_env()).returncode
    dl = Path(out) / "downloads"
    if dl.exists():
        subprocess.run([sys.executable, str(HERE / "qa_files.py"), str(dl)])
    print("HPD_QA " + json.dumps({"rc": rc, "report": str(Path(out, "report.md").resolve())}, ensure_ascii=False))
    return rc


def imgcheck(image: str, out: str, total_width: float | None, total_height: float | None, extra: list[str]) -> dict:
    """v3.9.9: one analyze.py run on the ORIGINAL picture, then a verdict on whether its walls can be trusted.
    task-3ddae3f2f2: a 534×800 售楼处效果图 (plan body ~230 px wide, a 240 wall ~5 px, furniture and coloured floors) —
    analyze.py was rerun on crops / 2x / 5x upscales about eight times, 126 steps, and no room ever closed. A picture
    that small is read from its dimension chains instead, from the first step."""
    import statistics
    dark = dark_screenshot(image)
    if dark:
        return dark
    cmd = [sys.executable, str(HERE / "analyze.py"), image, "--out", out]
    if total_width:
        cmd += ["--total-width", str(total_width)]
    if total_height:
        cmd += ["--total-height", str(total_height)]
    cmd += list(extra or [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    res: dict = {"ok": r.returncode == 0, "analysis": out, "cmd": " ".join(cmd[1:])}
    if r.returncode != 0:
        res.update(route="dims", why="analyze.py 出错：" + (r.stderr or r.stdout)[-300:].strip())
    else:
        try:
            a = json.load(open(os.path.join(out, "analysis.json"), encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            a = {}
            res["error"] = str(e)
        size = a.get("size") or [0, 0]
        o = a.get("outline_px") or [0, 0, size[0], size[1]]
        bw, bh = max(1, o[2] - o[0]), max(1, o[3] - o[1])
        segs = a.get("segments_px") or []
        t_med = statistics.median([float(x.get("t") or 0) for x in segs]) if segs else 0.0
        chk = a.get("scale_check") or {}
        ws, hs = chk.get("width_scale"), chk.get("height_scale")
        spread = abs(ws - hs) / min(ws, hs) if ws and hs and min(ws, hs) > 0 else None
        mmpx = float(a.get("mmPerPx") or 0) or None
        wall_px = round(240.0 / mmpx, 1) if mmpx else None
        why = []
        if spread is not None and spread > 0.08:
            why.append(f"按总宽、总深算的比例差 {int(spread * 100)}%（认出的外轮廓不对）")
        if bw < 450 or bh < 350:
            why.append(f"认出的户型主体只有 {bw}×{bh} 像素")
        if wall_px is not None and wall_px < 7:
            why.append(f"240 墙只有约 {wall_px} 像素")
        if len(segs) < 8:
            why.append(f"只认出 {len(segs)} 段墙")
        res.update(size=size, outline_px=o, mmPerPx=round(mmpx, 3) if mmpx else None, wall240_px=wall_px,
                   wall_t_px_median=t_med, segments=len(segs), scale_spread=round(spread, 3) if spread is not None else None,
                   dim_lines={k: len(v) for k, v in (a.get("dims_px") or {}).items()})
        res["route"] = "dims" if why else "analyze"
        res["why"] = "；".join(why) or f"户型主体 {bw}×{bh} 像素，240 墙约 {wall_px} 像素，比例一致"
    if res["route"] == "dims":
        res["advice"] = ("这张图不要再换参数、裁剪、放大重跑 analyze.py（不会变好）。改走尺寸链：先用 dimocr.py --scale <mmPerPx> 自动读分段尺寸数字（只信 ok 的段），再读图补上 / 下 / 左 / 右没读出的段，"
                         "累加成 X、Y 两套轴网（毫米），按 reference/example-gen.py 的写法手写墙（外圈先闭合，内墙端点落在别的墙中线上，"
                         "墙厚按常用值 240 / 200 / 120），门窗按原图位置写在墙上，房间名放在原图文字处；underlay 用原图并按总宽换算 mmPerPx，"
                         "然后 make，看 overlay.png 对齐再微调。同一张图 read_image 不超过 3 次。图太小认不清的地方，在交付时说明，"
                         "并建议用户换一张户型部分 ≥1500 像素宽、没有家具的线稿。")
    else:
        res["advice"] = ("用这次的 draft.json 继续第 3 步。make 之后 freeWallEnds 仍 > 8 或房间围不起来，最多再调一次 analyze.py 参数"
                         "（--outlined / --gray-max / --crop），还不行就改走尺寸链手写，不要反复重跑。")
    return res


# ------------------------------------------------------------------------------------------------ 3.9.13 全景漫游
def _pano_write(doc: dict, src: Path, out_dir: Path, name: str) -> dict:
    """doc = scenes.json（图片是 src 里的相对文件名）。写两份：
    <out_dir>/<name>-全景/index.html + 图片（放到网站 / COS 上用），<out_dir>/<name>-全景漫游.html（单文件，图片内嵌，直接发微信）。"""
    import base64
    viewer = (HERE.parent / "assets" / "pano-viewer.html").read_text(encoding="utf-8")
    marker = "window.__PANO__ = window.__PANO__ || null;"
    assert marker in viewer
    folder = out_dir / f"{name}-全景"
    folder.mkdir(parents=True, exist_ok=True)
    files = [s["image"] for s in doc["scenes"]] + ([doc["plan"]["image"]] if doc.get("plan") else [])
    for f in files:
        if (src / f).resolve() != (folder / f).resolve():
            shutil.copyfile(src / f, folder / f)
    js = lambda d: json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    (folder / "index.html").write_text(viewer.replace(marker, "window.__PANO__ = " + js(doc) + ";"), encoding="utf-8")
    (folder / "scenes.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    inl = json.loads(json.dumps(doc))
    def uri(f):
        mime = "image/png" if f.lower().endswith(".png") else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode((folder / f).read_bytes()).decode()
    for sc in inl["scenes"]:
        sc["image"] = uri(sc["image"])
    if inl.get("plan"):
        inl["plan"]["image"] = uri(inl["plan"]["image"])
    single = out_dir / f"{name}-全景漫游.html"
    single.write_text(viewer.replace(marker, "window.__PANO__ = " + js(inl) + ";"), encoding="utf-8")
    return {"ok": True, "html": str(single.resolve()), "mb": round(single.stat().st_size / 1e6, 1),
            "folder": str(folder.resolve()), "scenes": [s["name"] for s in doc["scenes"]],
            "first": doc.get("first"), "unlinked": [s["name"] for s in doc["scenes"] if not s.get("links")]}


def pano(html: str, out: str, name: str | None, size: int, eye: int) -> dict:
    """生成好的设计器 HTML → 每间房一张 360° 全景 + 门洞跳转热点 + 平面小地图 → 离线漫游网页。"""
    work = Path(out) / "_pano"
    work.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["node", str(HERE / "pano.mjs"), html, str(work), "--size", str(size), "--eye", str(eye)],
                       capture_output=True, text=True, env=_node_env(), timeout=3600)
    line = [x for x in r.stdout.splitlines() if x.startswith("PANO_SCENES ")]
    res = json.loads(line[-1][12:]) if line else {"ok": False, "error": (r.stderr or r.stdout)[-400:]}
    if not res.get("ok"):
        return res
    doc = json.loads((work / "scenes.json").read_text(encoding="utf-8"))
    name = name or doc.get("name") or Path(html).stem.split("-装修设计器")[0]
    name = re.sub(r"\s*[·-]\s*装修设计器$", "", name).strip() or "户型"
    doc["name"] = name
    w = _pano_write(doc, work, Path(out), name)
    shutil.rmtree(work, ignore_errors=True)
    w["seconds"] = res.get("seconds")
    return w


def pano_photos(images: list[str], out: str, name: str, names: str | None, links: str | None, first: str | None) -> dict:
    """用户自己的 360° 全景照片（2:1 等距柱状图：全景相机 / 酷家乐、三维家导出）→ 同一个漫游网页。
    --names 客厅,主卧,…（按图片顺序，缺省用文件名）
    --links "客厅>主卧@35,主卧>客厅@-150"：在「客厅」那张图里朝 35° 的地方放一个去「主卧」的箭头
             （角度：图片正中 = 0°，向右为正，-180..180；可加 ^-20 表示俯仰，默认 -18° 偏地面）。不写 --links 就只有底部房间列表。"""
    from PIL import Image
    work = Path(out) / "_pano"
    work.mkdir(parents=True, exist_ok=True)
    nm = [x.strip() for x in (names or "").replace("，", ",").split(",") if x.strip()]
    scenes, bad = [], []
    for i, f in enumerate(images):
        im = Image.open(f)
        im = im.convert("RGB")
        ratio = im.width / max(1, im.height)
        if abs(ratio - 2) > 0.15:
            bad.append(f"{Path(f).name}：{im.width}×{im.height}（不是 2:1 全景图）")
        if im.width > 6144:
            im = im.resize((6144, 3072), Image.LANCZOS)
        elif abs(ratio - 2) <= 0.15 and ratio != 2:
            im = im.resize((im.width, im.width // 2), Image.LANCZOS)
        sid = f"s{i}"
        im.save(work / f"scene-{sid}.jpg", quality=85)
        scenes.append({"id": sid, "name": nm[i] if i < len(nm) else Path(f).stem, "image": f"scene-{sid}.jpg", "yaw0": 0, "links": []})
    if bad:
        shutil.rmtree(work, ignore_errors=True)
        return {"ok": False, "error": "这些图不是 360° 全景（要 2:1 的等距柱状图，全景相机或装修软件导出的全景图）：" + "；".join(bad)}
    by = {s["name"]: s for s in scenes}
    warn = []
    for item in [x.strip() for x in (links or "").replace("，", ",").split(",") if x.strip()]:
        m = re.match(r"^(.+?)>(.+?)@(-?\d+(?:\.\d+)?)(?:\^(-?\d+(?:\.\d+)?))?$", item)
        if not m or m.group(1) not in by or m.group(2) not in by:
            warn.append(f"看不懂或房间名不对：{item}")
            continue
        by[m.group(1)]["links"].append({"to": by[m.group(2)]["id"], "label": m.group(2),
                                        "yaw": math.radians(float(m.group(3))),
                                        "pitch": math.radians(float(m.group(4) if m.group(4) else -18))})
    doc = {"name": name, "first": by[first]["id"] if first in by else scenes[0]["id"], "plan": None, "scenes": scenes}
    w = _pano_write(doc, work, Path(out), name)
    shutil.rmtree(work, ignore_errors=True)
    if warn:
        w["warnings"] = warn
    return w


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor"); sub.add_parser("ensure")
    p = sub.add_parser("from-cad"); p.add_argument("spec"); p.add_argument("-o", "--out", required=True); p.add_argument("--name")
    p = sub.add_parser("make"); p.add_argument("spec"); p.add_argument("--out", required=True)
    p.add_argument("--name", default="户型"); p.add_argument("--final")
    p = sub.add_parser("from-image"); p.add_argument("image"); p.add_argument("-o", "--out", required=True); p.add_argument("--name")
    p = sub.add_parser("imgcheck"); p.add_argument("image"); p.add_argument("--out", required=True)
    p.add_argument("--total-width", type=float); p.add_argument("--total-height", type=float)
    p = sub.add_parser("qa"); p.add_argument("html"); p.add_argument("--out", required=True); p.add_argument("--full", action="store_true")
    p = sub.add_parser("pano"); p.add_argument("html"); p.add_argument("-o", "--out", required=True); p.add_argument("--name")
    p.add_argument("--size", type=int, default=3072); p.add_argument("--eye", type=int, default=1500)
    p = sub.add_parser("pano-photos"); p.add_argument("images", nargs="+"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--name", default="全景漫游"); p.add_argument("--names"); p.add_argument("--links"); p.add_argument("--first")
    a, extra = ap.parse_known_args()
    if extra and a.cmd not in ("imgcheck", "from-image"):
        ap.error("unrecognized arguments: " + " ".join(extra))
    if a.cmd == "doctor":
        print("HPD_DOCTOR " + json.dumps(doctor(), ensure_ascii=False))
    elif a.cmd == "ensure":
        print("HPD_DOCTOR " + json.dumps(ensure(), ensure_ascii=False))
    elif a.cmd == "from-cad":
        print("HPD_DRAFT " + json.dumps(from_cad(a.spec, a.out, a.name), ensure_ascii=False))
    elif a.cmd == "make":
        print("HPD_RESULT " + json.dumps(make(a.spec, a.out, a.name, a.final), ensure_ascii=False))
    elif a.cmd == "from-image":
        print("HPD_DRAFT " + json.dumps(from_image(a.image, a.out, a.name, extra), ensure_ascii=False))
    elif a.cmd == "imgcheck":
        print("HPD_IMGCHECK " + json.dumps(imgcheck(a.image, a.out, a.total_width, a.total_height, extra), ensure_ascii=False))
    elif a.cmd == "pano":
        print("HPD_PANO " + json.dumps(pano(a.html, a.out, a.name, a.size, a.eye), ensure_ascii=False))
    elif a.cmd == "pano-photos":
        print("HPD_PANO " + json.dumps(pano_photos(a.images, a.out, a.name, a.names, a.links, a.first), ensure_ascii=False))
    elif a.cmd == "qa":
        sys.exit(qa(a.html, a.out, a.full))


if __name__ == "__main__":
    main()
