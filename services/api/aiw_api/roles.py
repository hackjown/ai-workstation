"""Computer roles (aiw-roles-1): each cloud computer has a job, and new tasks go to the computer whose job fits.

kv "machine_roles":
  {"enabled": true,          # route new tasks by role
   "exclude_local": true,    # the local VM takes no routed or dispatched work...
   "lan_to_local": true,     # ...except work that needs the home network (192.168.x, iStoreOS, sing-box, NAS)
   "strict": false,          # true: wait for the role's computer even when it is busy; false: an idle cloud one helps
   "machines": {"<machine id>": "design" | "media" | "office" | "general"}}

What is routed: a NEW task from the chat or the task composer. A continuation (继续/修改/补充) stays on the computer
it ran on, a task that names a computer ("用 cloud-2 做…") goes there, and routines / price watches / 小红书 keep
the computer they were set up with. Dispatch (several computers at once) sees the roles and leaves the local VM out.
"""
from __future__ import annotations

import re
from typing import Optional

KEY = "machine_roles"

ROLES: dict[str, dict] = {
    "design": {"title": "设计制图", "desc": "CAD 施工图、照图出图、修图 P 图、海报与 UI 设计稿",
               "re": r"cad|dxf|librecad|施工图|平面图|立面图|户型|图纸|制图|画图|出图|修图|p图|改图|抠图|效果图|海报|"
                     r"photopea|photocraft|huashu-design|设计稿|mockup|ui ?设计|logo|图片|照片|(?<![a-z])(png|jpe?g|webp|heic|psd)(?![a-z])"},
    "media": {"title": "视频音频", "desc": "视频剪辑与复刻、讲解视频、配音字幕、音频处理",
              "re": r"视频|视屏|短片|剪(辑|成|一|个|掉|短|切)|配音|字幕|讲解片|宣传片|动画|动效|复刻|仿拍|拉片|huashu-motion|video-remake|"
                    r"ffmpeg|(?<![a-z0-9])(mp4|mov|webm|m4v|mp3|wav|m4a)(?![a-z0-9])|音频|录音|bgm|yt-dlp|抖音|b站|bilibili|youtube"},
    "office": {"title": "办公·网页·开发", "desc": "Word/Excel/PPT/PDF、网页查询比价与注册、小红书运营、写程序与部署",
               "re": r"word|excel|ppt|pdf|docx|xlsx|pptx|officecli|文档|表格|幻灯片|报告|周报|合同|简历|网页|网站|浏览器|"
                     r"搜索|查一下|比价|价格|京东|淘宝|拼多多|天猫|注册|登录|小红书|xhs|开发|写(个|一个)?(程序|脚本|网站|页面)|"
                     r"代码|部署|github|python|api"},
    "general": {"title": "通用", "desc": "什么都做", "re": r"$^"},
}
ORDER = ["design", "media", "office"]   # default roles, given to the cloud computers in id order

LAN = re.compile(r"192\.168\.\d|(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d|istore ?os|openwrt|"
                 r"路由器|软路由|sing-?box|clash|内网|局域网|\bnas\b|群晖|synology|家里的?(网络|电脑|设备)|ipad分流|分流规则", re.I)


def cfg(store) -> dict:
    c = store.kv_get(KEY, None) or {}
    out = {"enabled": True, "exclude_local": True, "lan_to_local": True, "strict": False, "machines": {}}
    out.update({k: v for k, v in c.items() if k in out})
    out["machines"] = dict(out.get("machines") or {})
    return out


def cloud_ids(sessions) -> list[str]:
    cloud = getattr(sessions, "cloud", None)
    if not cloud:
        return []
    ids = []
    for mid, e in sorted((cloud.entries() or {}).items()):
        if (e or {}).get("status") == "ready" and sessions.store.machine(mid):
            ids.append(mid)
    return ids


def roles_of(sessions) -> dict:
    """{machine id: role} for every ready cloud computer; one without a saved role gets the next default."""
    c = cfg(sessions.store)
    saved = c["machines"]
    out, i = {}, 0
    for mid in cloud_ids(sessions):
        r = saved.get(mid)
        if r not in ROLES:
            r = ORDER[i] if i < len(ORDER) else "general"
            i += 1
        out[mid] = r
    return out


def title(role: Optional[str]) -> str:
    return (ROLES.get(role or "") or {}).get("title", "")


def label(sessions, mid: Optional[str]) -> str:
    """How a card names the computer a task runs on: its name, plus its role when it has one (云电脑 2 · 设计制图)."""
    if not mid:
        return ""
    m = sessions.store.machine(mid) or {}
    name = m.get("name") or mid
    try:
        r = roles_of(sessions).get(mid) if cfg(sessions.store)["enabled"] else None
    except Exception:  # noqa: BLE001
        r = None
    return f"{name} · {title(r)}" if r else name


# A skill the user picked decides the role before any keyword does (3.9.0).
SKILL_ROLE = {"huashu-motion": "media", "video-remake": "media",
              "huashu-design": "design", "librecad": "design", "cad-drawing": "design", "photocraft": "design",
              "photopea": "design",
              "officecli": "office", "reverse-engineer-anything": "office", "dev": "office", "signup": "office",
              "xhs": "office"}


def skill_role(prompt: str) -> Optional[str]:
    try:
        from agent_runtime import skills as _sk
        names = [n.lower() for n in _sk.picked(prompt) + _sk.mentioned(prompt)]
    except Exception:  # noqa: BLE001
        return None
    votes: dict = {}
    for n in names:
        r = SKILL_ROLE.get(n)
        if r:
            votes[r] = votes.get(r, 0) + 1
    return max(votes, key=lambda r: (votes[r], -ORDER.index(r))) if votes else None


def best_role(prompt: str) -> Optional[str]:
    by_skill = skill_role(prompt)
    if by_skill:
        return by_skill
    text = prompt or ""
    scores = {}
    for rid in ORDER:
        n = len(re.findall(ROLES[rid]["re"], text, re.I))
        if n:
            scores[rid] = n
    if not scores:
        return None
    return max(scores, key=lambda r: (scores[r], -ORDER.index(r)))


def _load(sessions, mid: str) -> int:
    n = 1 if sessions.busy_session(mid) else 0
    try:
        n += sum(1 for r in sessions.store.tasks_by_status(("queued",)) if r.get("machine_id") == mid)
    except Exception:  # noqa: BLE001
        pass
    return n


def pinned(sessions, prompt: str) -> Optional[str]:
    """A computer the request names by id or name (3+ characters, or 2+ with Chinese)."""
    low = (prompt or "").lower()
    for m in sessions.store.machines():
        for n in (m.get("id"), m.get("name")):
            n = str(n or "").strip().lower()
            if n and n in low and (len(n) >= 3 or (len(n) >= 2 and re.search(r"[^\x00-\x7f]", n))):
                return m["id"]
    return None


def pick(sessions, prompt: str, current: Optional[str]) -> Optional[str]:
    """The computer a NEW task should run on. Returns `current` when roles are off or nothing better is known."""
    c = cfg(sessions.store)
    if not c["enabled"]:
        return current
    roles = roles_of(sessions)
    clouds = list(roles)
    if not clouds:
        return current
    p = pinned(sessions, prompt)
    if p:
        return p
    local = [m["id"] for m in sessions.store.machines() if m["id"] not in clouds]
    if c["lan_to_local"] and local and LAN.search(prompt or ""):
        return current if current in local else local[0]
    if not c["exclude_local"] and current in local:
        return current
    role = best_role(prompt)
    idle = [m for m in clouds if _load(sessions, m) == 0]
    fit = [m for m in clouds if roles[m] == role] if role else []
    if fit:
        free = [m for m in fit if m in idle]
        if free:
            return free[0]
        if c["strict"] or not idle:
            return min(fit, key=lambda m: _load(sessions, m))
        general = [m for m in idle if roles[m] == "general"]
        return (general or idle)[0]
    if current in clouds and current in idle:
        return current
    general = [m for m in idle if roles[m] == "general"]
    if general or idle:
        return (general or idle)[0]
    return min(clouds, key=lambda m: _load(sessions, m))


# ---------------------------------------------------------------- v3.9.3: cooldown -> the local backup computer
COOL_CHECK_S = 8          # how long the InstaBox state lookup may take before the cloud one is taken as available


async def cooldown_left(sessions, mid: str) -> float:
    """Seconds an InstaBox computer still sits in its forced cooldown (after 8 h of running); 0 when it can run.
    A paused machine (30 idle minutes) is NOT cooling: the task wakes it as before."""
    cloud = getattr(sessions, "cloud", None)
    if not cloud or not cloud.is_cloud(mid):
        return 0.0
    try:
        import asyncio
        from .cloud import _cooldown_left
        st = await asyncio.wait_for(cloud.state(mid, max_age=60), COOL_CHECK_S)
        return _cooldown_left(st.get("cooldown_until"))
    except Exception:  # noqa: BLE001  unknown means try it: wake() still says why when it cannot start
        return 0.0


def backup_of(sessions) -> Optional[str]:
    """The local computer that stands in for a cooling cloud one: kv machine_roles.backup, else the first local."""
    c = sessions.store.kv_get(KEY, None) or {}
    clouds = set(cloud_ids(sessions)) | set((getattr(sessions, "cloud", None) and sessions.cloud.entries() or {}).keys())
    local = [m["id"] for m in sessions.store.machines() if m["id"] not in clouds]
    want = c.get("backup")
    if want and want in local:
        return want
    return local[0] if local else None


async def pick_live(sessions, prompt: str, current: Optional[str]) -> tuple:
    """pick(), then: when the chosen cloud computer is in its cooldown, the task goes to the local backup computer
    (where it queues behind what is already there, in order). Returns (machine id, notice or None)."""
    mid = pick(sessions, prompt, current) or current
    if not mid:
        return mid, None
    left = await cooldown_left(sessions, mid)
    if left <= 0:
        return mid, None
    bk = backup_of(sessions)
    if not bk:
        return mid, None
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(seconds=left)).astimezone(timezone(timedelta(hours=8)))
    m, b = sessions.store.machine(mid) or {}, sessions.store.machine(bk) or {}
    ahead = 1 if sessions.busy_session(bk) else 0
    try:
        ahead += sum(1 for r in sessions.store.tasks_by_status(("queued",)) if r.get("machine_id") == bk)
    except Exception:  # noqa: BLE001
        pass
    return bk, {"cooling": mid, "cooling_name": m.get("name") or mid, "backup": bk, "backup_name": b.get("name") or bk,
                "minutes": int(left // 60) + 1, "until": f"{until:%H:%M}", "ahead": ahead}


def notice_text(n: dict) -> str:
    t = (f"云电脑「{n['cooling_name']}」在 8 小时后的冷却期，约 {n['minutes']} 分钟后（北京时间 {n['until']}）才能再开，"
         f"这次改用备用电脑「{n['backup_name']}」执行。")
    if n.get("ahead"):
        t += f"它前面还有 {n['ahead']} 个任务，会按顺序排队执行。"
    return t


def annotate(sessions, roster: list[dict], own: Optional[str] = None) -> list[dict]:
    """Roster for dispatch: each cloud computer's role; the local VM left out (unless it is the task's own)."""
    c = cfg(sessions.store)
    if not c["enabled"]:
        return roster
    roles = roles_of(sessions)
    if not roles:
        return roster
    for m in roster:
        r = roles.get(m["id"])
        if r:
            m["role"] = r
            m["role_title"] = title(r)
        elif c["exclude_local"] and m["id"] != own:
            m["available"] = False
            m["state"] = "local VM, kept out of shared work"
    return roster


def place_auto(sessions, items: list[dict]) -> list[dict]:
    """Dispatch items left on 'auto' get the computer whose role fits their goal (when that one is not taken)."""
    c = cfg(sessions.store)
    if not c["enabled"]:
        return items
    roles = roles_of(sessions)
    taken = {str(it.get("machine") or "") for it in items}
    for it in items:
        if str(it.get("machine") or "auto").strip().lower() != "auto":
            continue
        r = best_role(it.get("goal") or "")
        fit = [m for m in roles if roles[m] == r and m not in taken]
        if fit:
            it["machine"] = fit[0]
            taken.add(fit[0])
    return items


def show(sessions) -> dict:
    c = cfg(sessions.store)
    roles = roles_of(sessions)
    ms = []
    for m in sessions.store.machines():
        r = roles.get(m["id"])
        ms.append({"id": m["id"], "name": m.get("name") or m["id"], "kind": "cloud" if r else "local",
                   "role": r or ("excluded" if c["exclude_local"] else ""), "role_title": title(r) if r else
                   ("不参与（只做家里内网的活）" if c["exclude_local"] and c["lan_to_local"] else
                    "不参与" if c["exclude_local"] else "")})
    return {**c, "computers": ms, "roles": {k: {"title": v["title"], "desc": v["desc"]} for k, v in ROLES.items()}}


# ---------------------------------------------------------------- CLI (aiw-roles.sh runs this inside the container)
def _cli():
    import json
    import sys
    from types import SimpleNamespace
    from aiw_api.config import Settings
    from aiw_api.store import Store

    st = Store(Settings().db_path)

    class _Cloud:
        def entries(self):
            return st.kv_get("cloud_vms", {}) or {}

    s = SimpleNamespace(store=st, cloud=_Cloud(), busy_session=lambda mid: None)
    a = sys.argv[1:]
    c = cfg(st)
    if a and a[0] == "set" and len(a) == 3:
        mid = a[1]
        hit = [m["id"] for m in st.machines() if a[1] in (m["id"], m.get("name"))]
        if not hit or hit[0] not in cloud_ids(s):
            sys.exit(f"不是一台已就绪的云电脑：{a[1]}")
        if a[2] not in ROLES:
            sys.exit("角色只能是：" + " / ".join(ROLES))
        c["machines"][hit[0]] = a[2]
    elif a and a[0] in ("on", "off"):
        c["enabled"] = a[0] == "on"
    elif a and a[0] in ("strict", "loose"):
        c["strict"] = a[0] == "strict"
    elif a and a[0] == "local" and len(a) == 2 and a[1] in ("lan", "off", "on"):
        c["exclude_local"] = a[1] != "on"
        c["lan_to_local"] = a[1] == "lan"
    elif a and a[0] == "test":
        print(json.dumps({"prompt": " ".join(a[1:]), "role": best_role(" ".join(a[1:])),
                          "computer": pick(s, " ".join(a[1:]), None)}, ensure_ascii=False))
        return
    elif a and a[0] != "show":
        sys.exit("用法：show | set <电脑id或名字> <design|media|office|general> | on | off | strict | loose | "
                 "local lan|off|on | test <任务文字>")
    if not (a and a[0] == "show") and a:
        c["machines"].update({k: v for k, v in roles_of(s).items() if k not in c["machines"]})
        st.kv_set(KEY, c)
    else:   # first run: write the default roles down so they never shift when a computer is added
        if not st.kv_get(KEY, None):
            c["machines"] = roles_of(s)
            st.kv_set(KEY, c)
    print(json.dumps(show(s), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _cli()
