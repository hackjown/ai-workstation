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
  python3 aiw_hpd.py qa 户型名-装修设计器.html --out hpd/qa [--full]
                                                # 验收：默认 --lite --quick（VM 无显卡），--full 跑完整一遍

所有命令都可以在任意目录执行；路径相对当前目录。
"""
from __future__ import annotations

import argparse
import json
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


def from_cad(spec_path: str, out: str, name: str | None = None) -> dict:
    """cad-drawing 的 spec 是 y 向下的 mm（y_down=true，和 HouseSpec 一样）。墙取 drawkit 的墙段（traced 或房间网格推出来的），
    门窗按位置挂到最近的墙上，房间名变成标签点，家具块换成设计器的 key。只是初稿：之后照 SKILL.md 第 4 步 check 并修。"""
    sp_path = Path(spec_path).resolve()
    spec = json.loads(sp_path.read_text(encoding="utf-8"))
    if spec.get("kind", "floor_plan") != "floor_plan":
        raise SystemExit("from-cad 只接受户型平面图 spec（kind=floor_plan）")
    kit = None
    for d in (Path.home() / ".claude" / "skills" / "cad-drawing", ROOT.parent / "cad-drawing",
              Path(os.environ.get("VM_AGENT_FILES_ROOT", str(Path.home() / "workspace"))) / ".aiw" / "cad-drawing"):
        if (d / "drawkit.py").exists():
            kit = d
            break
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor"); sub.add_parser("ensure")
    p = sub.add_parser("from-cad"); p.add_argument("spec"); p.add_argument("-o", "--out", required=True); p.add_argument("--name")
    p = sub.add_parser("make"); p.add_argument("spec"); p.add_argument("--out", required=True)
    p.add_argument("--name", default="户型"); p.add_argument("--final")
    p = sub.add_parser("qa"); p.add_argument("html"); p.add_argument("--out", required=True); p.add_argument("--full", action="store_true")
    a = ap.parse_args()
    if a.cmd == "doctor":
        print("HPD_DOCTOR " + json.dumps(doctor(), ensure_ascii=False))
    elif a.cmd == "ensure":
        print("HPD_DOCTOR " + json.dumps(ensure(), ensure_ascii=False))
    elif a.cmd == "from-cad":
        print("HPD_DRAFT " + json.dumps(from_cad(a.spec, a.out, a.name), ensure_ascii=False))
    elif a.cmd == "make":
        print("HPD_RESULT " + json.dumps(make(a.spec, a.out, a.name, a.final), ensure_ascii=False))
    elif a.cmd == "qa":
        sys.exit(qa(a.html, a.out, a.full))


if __name__ == "__main__":
    main()
