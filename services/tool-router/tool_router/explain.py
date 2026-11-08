"""Plain-language summaries of what a tool call is about to do (approval cards, takeover cards, notifications).

A confirmation the user cannot read is no confirmation: task-135 showed `rm -rf recalc && ... soffice --convert-to ...`
as raw text. summarize() turns a tool call into

    {title, what, impact:[{kind,label}], risks:[str], risk: low|medium|high, why, category, tech:{...}}

deterministically: shell commands are tokenized and each simple command (rm/mv/cp/mkdir/redirects, soffice,
python -c with openpyxl, pip/apt/npm, curl/wget, git, kill, sudo …) gets a short Chinese phrase; other tools use
per-tool templates. The raw command only ever goes into `tech`, which the UI folds under 查看技术细节.

shell_risk() is the same parse used by the policy engine to decide what really needs the user (see policy.py).
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Optional
from urllib.parse import urlparse

LOW, MED, HIGH = 0, 1, 2
RISK_NAME = {LOW: "low", MED: "medium", HIGH: "high"}
RISK_CN = {"low": "低风险", "medium": "中风险", "high": "高风险"}

HOME_ALIASES = ("/data/home", "/home/aiw", "/home/user", "/root", "$HOME", "${HOME}")
TEMP_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/")

APP_CN = {"soffice": "LibreOffice", "libreoffice": "LibreOffice", "ffmpeg": "FFmpeg", "ffprobe": "FFmpeg",
          "google-chrome": "Chrome", "chromium": "Chromium", "librecad": "LibreCAD", "gimp": "GIMP", "inkscape": "Inkscape",
          "convert": "ImageMagick", "magick": "ImageMagick", "pandoc": "Pandoc", "docker": "Docker", "git": "Git"}
EXT_CN = {"xlsx": "Excel", "xls": "Excel", "xlsm": "Excel", "csv": "CSV", "docx": "Word", "doc": "Word", "pptx": "PPT",
          "ppt": "PPT", "pdf": "PDF", "odt": "文档", "ods": "表格", "png": "图片", "jpg": "图片", "jpeg": "图片", "webp": "图片",
          "html": "网页", "txt": "文本", "md": "Markdown", "mp4": "视频", "mov": "视频", "mp3": "音频", "wav": "音频",
          "dxf": "图纸", "dwg": "图纸", "zip": "压缩包", "json": "JSON"}
SITE_CN = [("jd.com", "京东"), ("jd.hk", "京东"), ("taobao.com", "淘宝"), ("tmall.com", "天猫"), ("xiaohongshu.com", "小红书"),
           ("weibo.com", "微博"), ("bilibili.com", "哔哩哔哩"), ("douyin.com", "抖音"), ("alipay.com", "支付宝"),
           ("pinduoduo.com", "拼多多"), ("yangkeduo.com", "拼多多"), ("meituan.com", "美团"), ("ctrip.com", "携程"),
           ("12306.cn", "12306"), ("zhihu.com", "知乎"), ("weixin.qq.com", "微信"), ("qq.com", "QQ"), ("baidu.com", "百度"),
           ("163.com", "网易"), ("aliyun.com", "阿里云"), ("github.com", "GitHub"), ("google.com", "Google"),
           ("openai.com", "OpenAI"), ("apple.com", "Apple"), ("microsoft.com", "Microsoft"), ("live.com", "Microsoft"),
           ("amazon.com", "Amazon"), ("cloudflare.com", "Cloudflare"), ("instabox.sh", "InstaBox")]

READ_ONLY = {"ls", "ll", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "echo", "printf", "pwd", "which",
             "whereis", "type", "file", "stat", "du", "df", "free", "ps", "pgrep", "uname", "date", "sleep", "true", "false",
             "test", "[", "[[", "sort", "uniq", "awk", "cut", "tr", "jq", "diff", "cmp", "md5sum", "sha1sum", "sha256sum",
             "zipinfo", "pdfinfo", "identify", "ffprobe", "nvidia-smi", "env", "printenv", "export", "id", "whoami", "hostname",
             "uptime", "lsof", "nproc", "lscpu", "lsblk", "realpath", "dirname", "basename", "readlink", "tree", "less", "more",
             "column", "nl", "xxd", "od", "hexdump", "strings", "fc-list", "locale", "wmctrl", "xdotool", "xwininfo", "xprop",
             "timeout", "time", "command", "set", "unset", "source", ".", "exit", "return", "local", "read", "seq", "bc",
             "expr", "yes", "watch", "history", "alias", "trap", "wait", "jobs", "fg", "bg", "ulimit", "umask", "shopt"}
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "php", "deno", "bun"}


# ------------------------------------------------------------------------------------------------ paths

def norm_path(p: str, cwd: str = "~") -> str:
    """Absolute-ish path with the home folder written as ~ (the VM home is /data/home or /home/aiw)."""
    p = (p or "").strip().strip("'\"")
    if not p:
        return cwd
    for h in HOME_ALIASES:
        if p == h or p.startswith(h + "/"):
            p = "~" + p[len(h):]
            break
    if p == "~" or p.startswith("~/") or p.startswith("/"):
        out = p
    else:
        out = (cwd.rstrip("/") + "/" + p) if cwd else p
    parts = []
    for seg in out.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts and parts[-1] != "~":
                parts.pop()
            continue
        parts.append(seg)
    s = "/".join(parts)
    return s if s.startswith("~") else "/" + s


def zone(p: str) -> str:
    """tmp | workspace | uploads | home_dot | home | system | dev"""
    if p in ("/dev/null", "/dev/stdout", "/dev/stderr") or p.startswith("/dev/fd/"):
        return "dev"
    if p.startswith(TEMP_PREFIXES) or p in ("/tmp", "/var/tmp"):
        return "tmp"
    if p == "~/workspace/uploads" or p.startswith("~/workspace/uploads/"):
        return "uploads"
    if p == "~/workspace" or p.startswith("~/workspace/"):
        return "workspace"
    if p.startswith("~/."):
        return "home_dot"
    if p == "~" or p.startswith("~/"):
        return "home"
    return "system"


def short(s: str, n: int = 40) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n // 2 - 1] + "…" + s[-(n // 2 - 1):]


def readable(p: str) -> str:
    """How a path is shown to a person: relative to the workspace when inside it, the file name kept whole."""
    if p.startswith("~/workspace/"):
        p = p[len("~/workspace/"):]
    elif p == "~/workspace":
        return "工作区"
    name = p.rstrip("/").rsplit("/", 1)[-1] if "/" in p.rstrip("/") else p
    if len(p) > 46:
        return "…/" + short(name, 44)
    return p


def ext_of(p: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", p.rstrip("/"))
    return m.group(1).lower() if m else ""


def kind_cn(p: str) -> str:
    return EXT_CN.get(ext_of(p), "文件")


def site_of(url_or_host: str) -> tuple[str, str]:
    """(display name, host) for a url: 京东 / passport.jd.com."""
    u = str(url_or_host or "").strip()
    if not u:
        return "", ""
    host = (urlparse(u if "://" in u else "https://" + u).hostname or "").lower()
    for dom, name in SITE_CN:
        if host == dom or host.endswith("." + dom):
            return name, host
    return host, host


# ------------------------------------------------------------------------------------------------ shell parsing

_SEPS = {"&&", "||", ";", "|", "&", "\n", "|&", ";;"}
_REDIR = {">", ">>", "<", ">&", "&>", ">|", "<<", "<<<", "&>>"}


def split_shell(cmd: str) -> Optional[list[list[str]]]:
    """Simple commands of a shell line (redirections kept as tokens). None when it does not tokenize."""
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>()\n")
        lx.whitespace = " \t\r"
        lx.whitespace_split = True
        lx.commenters = ""
        toks = list(lx)
    except ValueError:
        return None
    out, cur = [], []
    for t in toks:
        if t in _SEPS or t in ("(", ")", "{", "}"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


class Op:
    """One simple command, explained."""

    def __init__(self, phrase: str = "", sentence: str = "", sev: int = LOW, ask: Optional[str] = None,
                 trivial: bool = False, category: str = "other"):
        self.phrase = phrase          # for the title: 删除临时文件夹 recalc
        self.sentence = sentence      # for 要做什么
        self.sev = sev
        self.ask = ask                # why this needs the user (None = no)
        self.trivial = trivial        # ls / cd / mkdir of a scratch dir: not worth a title
        self.category = category
        self.impact: list[dict] = []
        self.risks: list[str] = []

    def hit(self, kind: str, label: str):
        if label and not any(i["label"] == label and i["kind"] == kind for i in self.impact):
            self.impact.append({"kind": kind, "label": label})

    def risk(self, text: str, sev: int = MED):
        if text not in self.risks:
            self.risks.append(text)
        self.sev = max(self.sev, sev)


def _strip_prefix(words: list[str]) -> tuple[list[str], bool]:
    """Drop VAR=x, env, nohup, timeout N, nice, stdbuf, xvfb-run, time; report sudo."""
    sudo = False
    w = list(words)
    while w:
        h = w[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", h):
            w.pop(0)
        elif h in ("sudo", "doas"):
            sudo = True
            w.pop(0)
            while w and w[0].startswith("-"):
                opt = w.pop(0)
                if opt in ("-u", "-g") and w:
                    w.pop(0)
        elif h in ("env", "nohup", "nice", "time", "command", "exec", "xvfb-run", "stdbuf", "ionice", "builtin"):
            w.pop(0)
            while w and w[0].startswith("-"):
                w.pop(0)
        elif h == "timeout":
            w.pop(0)
            while w and (w[0].startswith("-") or re.match(r"^\d+[smhd]?$", w[0])):
                w.pop(0)
        else:
            break
    return w, sudo


def _redirects(words: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Split off redirections: (args, [(op, target)])."""
    args, reds, i = [], [], 0
    while i < len(words):
        t = words[i]
        if t in _REDIR:
            tgt = words[i + 1] if i + 1 < len(words) else ""
            if args and re.fullmatch(r"\d", args[-1]):   # 2>&1, 2>/dev/null: the fd number is not an argument
                args.pop()
            reds.append((t, tgt))
            i += 2
            continue
        args.append(t)
        i += 1
    return args, reds


def _positional(args: list[str], with_value: tuple = ()) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-") and a != "-":
            if a in with_value:
                skip = True
            continue
        out.append(a)
    return out


class Ctx:
    def __init__(self, created: Optional[set] = None):
        self.cwd = "~"
        self.cds: list[str] = []
        self.created = set(created or ())     # dirs this task made (normalized), across commands
        self.made_here: set[str] = set()
        self.vars: dict[str, str] = {}        # v3.9.22: P=http://127.0.0.1:8799 earlier in the line
        self.heredocs: dict[str, str] = {}    # v3.9.22: NAME_BODY -> the heredoc text
        self.stdin_code = ""


def _is_scratch_dir(p: str, ctx: Ctx) -> bool:
    z = zone(p)
    if z == "tmp":
        return True
    if p in ctx.created or any(p == c or p.startswith(c.rstrip("/") + "/") for c in ctx.created):
        return z in ("workspace", "tmp", "home_dot")
    return False


def _target_label(p: str) -> str:
    return readable(p)


def _delete_op(paths: list[str], recursive: bool, ctx: Ctx, raw_targets: list[str]) -> Op:
    op = Op(category="delete")
    names, scratch_all = [], True
    for raw, p in zip(raw_targets, paths):
        z = zone(p)
        glob = any(ch in raw for ch in "*?[")
        name = _target_label(p)
        is_dir = raw.endswith("/") or (recursive and not ext_of(p)) or p in ctx.created
        scratch = _is_scratch_dir(p, ctx) and not glob
        tmp_word = "临时文件夹" if is_dir else "临时文件"
        if scratch:
            names.append(f"{tmp_word} {name}")
            op.hit("delete", f"{name}（临时{'文件夹' if is_dir else '文件'}）")
            op.risk(f"会删除 {name} {'文件夹' if is_dir else '文件'}（临时文件）", LOW)
            continue
        scratch_all = False
        what = ("文件夹" if is_dir else "文件") if not glob else "一批文件"
        names.append(f"{what} {name}")
        op.hit("delete", name)
        if p in ("~", "~/workspace", "/", "~/workspace/uploads") or z == "system":
            op.risk(f"会删除 {name}" + ("（整个文件夹）" if is_dir else ""), HIGH)
            op.ask = "删除系统或整个工作区里的文件" if z == "system" or p in ("/",) else "删除整个工作区/上传文件夹"
        elif z == "uploads":
            op.risk(f"会删除你上传的文件 {name}", HIGH)
            op.ask = "删除你上传的文件"
        elif z == "home" or (z == "home_dot" and is_dir and not p.startswith("~/.cache/")):
            op.risk(f"会删除工作区以外的 {name}", HIGH)
            op.ask = "删除工作区以外的文件"
        elif z == "home_dot":
            op.risk(f"会删除工作区以外的 {name}", MED)
        elif z == "workspace":
            op.risk(f"会删除 {name}" + ("（含里面所有文件）" if is_dir else ""), MED if not glob else HIGH)
        else:
            op.risk(f"会删除 {name}", MED)
        if glob:
            op.risk("用通配符批量删除", HIGH if z not in ("tmp",) else MED)
    if not scratch_all:
        op.risk("删除后不能撤销", MED)
    op.phrase = "删除" + "、".join(names[:2]) + ("等" if len(names) > 2 else "")
    op.sentence = op.phrase
    op.trivial = False
    return op


def _write_target(op: Op, p: str, verb: str = "写入", append: bool = False):
    z = zone(p)
    name = _target_label(p)
    if z == "dev":
        return
    op.hit("write" if not append else "write", name)
    if z == "uploads" and not append:
        op.risk(f"会覆盖你上传的 {name}", HIGH)
        op.ask = "覆盖你上传的文件"
    elif z == "system":
        op.risk(f"会改动系统文件 {name}", HIGH)
        op.ask = "改动工作区以外的系统文件"
    elif z == "home" and not append and re.match(r"^~/(Desktop|Documents|Downloads|Pictures|Videos|Music|桌面|文档|下载|图片|视频|音乐)(/|$)", p):
        op.risk(f"会覆盖你个人文件夹里的 {name}", HIGH)
        op.ask = "覆盖工作区以外的个人文件"
    elif z == "home" and not append:
        op.risk(f"会写到工作区以外：{name}", MED)
    elif not append and z == "workspace" and ext_of(p):
        op.risk(f"如果已有 {name} 会被覆盖", LOW)


def _expand(t: str, ctx: Ctx) -> str:
    if "$" not in t or not ctx.vars:
        return t
    return re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: ctx.vars.get(m.group(1), m.group(0)), t)


def _shell_op(words: list[str], ctx: Ctx) -> Optional[Op]:
    if not words:
        return None
    while words and words[0] in _SH_KEYWORDS:   # v3.9.22: `do tail x`, `then rm y`: the command after the keyword
        words = words[1:]
    if not words:
        return None
    if words[0] in ("for", "case", "select"):   # `for f in a b` / `case $x in`: the loop header itself does nothing
        return Op(trivial=True, category="read")
    if all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) for w in words):   # P=http://... on its own
        for w in words:
            k, _, v = w.partition("=")
            ctx.vars[k] = v
        return Op(trivial=True, category="read")
    words = [_expand(w, ctx) for w in words]
    words, sudo = _strip_prefix(words)
    if not words:
        return None
    args, reds = _redirects(words)
    ctx.stdin_code = next((ctx.heredocs.get(t, "") for r, t in reds if r == "<<"), "")
    if args and args[0] in ("[", "[[", "test", "true", "false", ":"):
        return Op(trivial=True, category="read")
    if not args:
        op = Op(trivial=True, category="read")
    else:
        op = _command_op(args, ctx)
    for r, tgt in reds:
        if r in (">", ">|", "&>", ">>", "&>>") and tgt and not tgt.startswith("&"):
            p = norm_path(tgt, ctx.cwd)
            if zone(p) == "dev":
                continue
            _write_target(op, p, append=r.endswith(">>"))
            if op.trivial or not op.phrase:
                op.phrase = f"把结果{'追加' if r.endswith('>>') else '写入'} {_target_label(p)}"
                op.sentence = op.phrase
                op.trivial = False
                op.category = "edit"
    if sudo:
        op.risk("会用管理员权限（sudo）运行", HIGH)
        op.ask = op.ask or "用管理员权限运行命令"
        op.category = "privileged" if op.category in ("read", "other", "edit") else op.category
        op.trivial = False
        if op.phrase and not op.phrase.startswith("以管理员"):
            op.phrase = "以管理员权限" + op.phrase
    return op


def _python_code_op(code: str, ctx: Ctx, op: Op):
    loads = re.findall(r"load_workbook\(\s*(?:[a-zA-Z_]+\s*=\s*)?['\"]([^'\"]+)['\"]", code)
    var_files = dict(re.findall(r"(\w+)\s*=\s*['\"]([^'\"]+\.(?:xlsx|xlsm|xls|csv|docx|pptx|json|txt|md|png|jpg|pdf))['\"]", code))
    loads += [var_files[v] for v in re.findall(r"load_workbook\(\s*(\w+)\b", code) if v in var_files]
    saves = re.findall(r"\.save\(\s*['\"]([^'\"]+)['\"]", code)
    saves += [var_files[v] for v in re.findall(r"\.save\(\s*(\w+)\b", code) if v in var_files]
    writes = re.findall(r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wa]", code)
    writes += re.findall(r"\.to_(?:excel|csv|json)\(\s*['\"]([^'\"]+)['\"]", code)
    writes += re.findall(r"\.write_(?:text|bytes)\(", code) and [] or []
    deletes = re.findall(r"(?:os\.remove|os\.unlink|shutil\.rmtree)\(\s*['\"]([^'\"]+)['\"]", code)
    net = re.search(r"\b(requests|httpx|urllib\.request|aiohttp)\b.*\b(post|put|patch|delete|urlopen|get)\b", code, re.S)
    send = re.search(r"\b(requests|httpx)\.(post|put|patch)\b|smtplib|\.sendmail\(", code)
    subprocess_rm = re.search(r"subprocess\.\w+\(.*\brm\b", code)
    readonly = "data_only" in code or ("print(" in code and not (saves or writes or deletes))
    for f in loads:
        op.hit("read", _target_label(norm_path(f, ctx.cwd)))
    for f in saves + writes:
        p = norm_path(f, ctx.cwd)
        if p not in [norm_path(x, ctx.cwd) for x in loads]:
            _write_target(op, p)
        else:
            op.hit("write", _target_label(p))
            op.risk(f"会覆盖 {_target_label(p)}", MED if zone(p) != "uploads" else HIGH)
            if zone(p) == "uploads":
                op.ask = "覆盖你上传的文件"
    for f in deletes:
        p = norm_path(f, ctx.cwd)
        d = _delete_op([p], "rmtree" in code, ctx, [f])
        op.impact += d.impact
        for r in d.risks:
            op.risk(r, d.sev)
        op.ask = op.ask or d.ask
    if send:
        op.risk("会通过网络把数据发出去", HIGH)
        op.ask = op.ask or "通过网络发送数据"
        op.category = "network"
    elif net:
        op.risk("会访问网络", LOW)
    if subprocess_rm:
        op.risk("脚本里会删除文件", MED)
    xl = any(ext_of(f) in ("xlsx", "xlsm", "xls") for f in loads + saves)
    if saves or writes:
        tgt = (saves + writes)[0]
        p = norm_path(tgt, ctx.cwd)
        same = p in [norm_path(x, ctx.cwd) for x in loads]
        k = kind_cn(p)
        op.phrase = (f"修改 {k} {_target_label(p)}" if same else f"生成 {k} {_target_label(p)}")
        op.sentence = ("用 Python 读取并改写《" + _target_label(p) + "》" if same else
                       ("用 Python 读取" + ("《" + _target_label(norm_path(loads[0], ctx.cwd)) + "》" if loads else "数据")
                        + "，另存为《" + _target_label(p) + "》"))
        op.category = "edit"
    elif deletes:
        op.phrase = "用 Python 删除文件"
        op.sentence = op.phrase
        op.category = "delete"
    elif loads:
        op.phrase = f"读取{'表格' if xl else '文件'} {_target_label(norm_path(loads[0], ctx.cwd))}"
        op.sentence = f"用 Python 读取《{_target_label(norm_path(loads[0], ctx.cwd))}》{'里的单元格和公式' if xl else ''}（只读，不改文件）"
        op.trivial = True
        op.category = "read"
    else:
        op.phrase = "运行一段 Python 代码"
        op.sentence = "运行一段 Python 代码" + ("（只读，打印结果）" if readonly else "")
        op.trivial = readonly
        op.category = "read" if readonly else "other"


def _command_op(args: list[str], ctx: Ctx) -> Op:
    c = os.path.basename(args[0])
    rest = args[1:]
    op = Op()
    if c == "cd":
        ctx.cwd = norm_path(rest[0] if rest else "~", ctx.cwd)
        ctx.cds.append(ctx.cwd)
        op.trivial = True
        op.category = "read"
        return op
    if c in ("rm", "unlink", "rmdir", "shred", "trash", "trash-put"):
        flags = "".join(a.lstrip("-") for a in rest if a.startswith("-") and not a.startswith("--"))
        recursive = "r" in flags.lower() or "--recursive" in rest or c == "rmdir"
        tg = _positional(rest)
        paths = [norm_path(t, ctx.cwd) for t in tg]
        if not paths:
            return Op(trivial=True)
        d = _delete_op(paths, recursive, ctx, tg)
        if c == "shred":
            d.risk("会彻底粉碎文件，无法恢复", HIGH)
        return d
    if c == "mkdir":
        tg = [norm_path(t, ctx.cwd) for t in _positional(rest, ("-m",))]
        for p in tg:
            ctx.created.add(p)
            ctx.made_here.add(p)
            op.hit("create", _target_label(p) + "/")
            if zone(p) == "system":
                op.risk(f"会在工作区以外新建文件夹 {_target_label(p)}", MED)
        op.phrase = "新建文件夹 " + "、".join(_target_label(p) for p in tg[:2])
        op.sentence = op.phrase
        op.trivial = all(zone(p) in ("workspace", "tmp", "home_dot") for p in tg)
        op.category = "edit"
        return op
    if c == "touch":
        tg = [norm_path(t, ctx.cwd) for t in _positional(rest)]
        for p in tg:
            op.hit("create", _target_label(p))
        op.phrase = "新建文件 " + "、".join(_target_label(p) for p in tg[:2])
        op.sentence = op.phrase
        op.trivial = True
        return op
    if c in ("mv", "cp", "rsync", "install", "ln") and not (c == "rsync" and any("@" in a or "::" in a for a in rest)):
        pos = _positional(rest, ("-t", "--target-directory", "-S", "--suffix", "-e"))
        if len(pos) < 2:
            return Op(phrase=f"{'移动' if c == 'mv' else '复制'}文件", sentence=f"{'移动' if c == 'mv' else '复制'}文件")
        srcs, dst = [norm_path(s, ctx.cwd) for s in pos[:-1]], norm_path(pos[-1], ctx.cwd)
        verb = {"mv": "移动", "cp": "复制", "rsync": "同步", "install": "安装", "ln": "链接"}[c]
        same_dir = c == "mv" and len(srcs) == 1 and srcs[0].rsplit("/", 1)[0] == dst.rsplit("/", 1)[0]
        if same_dir:
            verb = "重命名"
        for s in srcs:
            op.hit("read" if c != "mv" else "move", _target_label(s))
            if c == "mv" and zone(s) == "uploads":
                op.risk(f"会移走你上传的 {_target_label(s)}", MED)
            if c == "mv" and zone(s) in ("system", "home"):
                op.risk(f"会移动工作区以外的 {_target_label(s)}", HIGH)
                op.ask = "移动工作区以外的文件"
        _write_target(op, dst)
        if "-n" not in rest and "--no-clobber" not in rest and ext_of(dst):
            op.risk(f"如果已有 {_target_label(dst)} 会被覆盖", LOW if zone(dst) in ("workspace", "tmp") else MED)
        names = "、".join(_target_label(s) for s in srcs[:2])
        op.phrase = f"{verb} {names}" + (f" 为 {_target_label(dst)}" if same_dir else f" 到 {_target_label(dst)}")
        op.sentence = op.phrase
        op.category = "edit"
        return op
    if c in ("soffice", "libreoffice", "lowriter", "localc", "loimpress"):
        conv = next((rest[i + 1] for i, a in enumerate(rest) if a == "--convert-to" and i + 1 < len(rest)), "")
        outdir = next((rest[i + 1] for i, a in enumerate(rest) if a == "--outdir" and i + 1 < len(rest)), "")
        fmt = conv.split(":")[0].lower()
        files = [a for a in _positional(rest, ("--convert-to", "--outdir", "--infilter", "-env", "--cat", "--print-to-file"))
                 if not a.startswith("-env:")]
        op.hit("app", "LibreOffice")
        if not conv:
            op.phrase = "打开 LibreOffice"
            op.sentence = "在电脑上打开 LibreOffice" + (f"，打开《{_target_label(norm_path(files[0], ctx.cwd))}》" if files else "")
            op.category = "app"
            return op
        od = norm_path(outdir, ctx.cwd) if outdir else ctx.cwd
        outs = []
        for f in files:
            src = norm_path(f, ctx.cwd)
            base = src.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0] if "." in base else base
            out = od.rstrip("/") + "/" + stem + "." + fmt
            outs.append((src, out))
            op.hit("read", _target_label(src))
            op.hit("create", _target_label(out))
            if out == src:
                op.risk(f"会覆盖原文件 {_target_label(src)}", MED if zone(src) != "uploads" else HIGH)
                if zone(src) == "uploads":
                    op.ask = "覆盖你上传的文件"
            elif zone(od) in ("workspace", "tmp") and (od in ctx.made_here or od in ctx.created):
                pass  # into a fresh folder: nothing of the user's is touched
            else:
                _write_target(op, out)
        src0 = outs[0][0] if outs else ""
        same_fmt = bool(src0) and ext_of(src0) == fmt
        k = EXT_CN.get(fmt, fmt.upper())
        if same_fmt:
            op.phrase = f"重新生成 {k}"
            op.sentence = (f"用 LibreOffice 把《{_target_label(src0)}》重新计算并另存一份到 {_target_label(od)} 文件夹"
                           + ("（原文件不动）" if outs and outs[0][1] != src0 else ""))
        else:
            op.phrase = f"把{kind_cn(src0) if src0 else '文件'}转成 {k}"
            op.sentence = (f"用 LibreOffice 把《{_target_label(src0)}》转成 {k}，存到 {_target_label(od)}" if src0
                           else f"用 LibreOffice 转换成 {k}")
        op.category = "edit"
        op.sev = max(op.sev, LOW)
        return op
    if c in ("pip", "pip3", "uv", "pipx", "conda", "mamba") or (c in INTERPRETERS and rest[:2] == ["-m", "pip"]):
        sub = rest[2:] if c in INTERPRETERS else rest
        if c == "uv" and sub[:1] == ["pip"]:
            sub = sub[1:]
        if sub[:1] in (["install"], ["add"]):
            pk = [a for a in _positional(sub[1:], ("-r", "-i", "--index-url", "-c", "--target", "-t", "--extra-index-url"))]
            req = next((sub[i + 1] for i, a in enumerate(sub) if a == "-r" and i + 1 < len(sub)), "")
            label = "、".join(pk[:3]) + ("等" if len(pk) > 3 else "") if pk else (f"{req} 里的依赖" if req else "依赖")
            op.phrase = f"安装 Python 包 {label}"
            op.sentence = f"从网上下载并安装 Python 包：{label}"
            for p in pk[:6]:
                op.hit("package", p)
            op.risk("会从网上下载并安装软件包", MED)
            if "--break-system-packages" in sub:
                op.risk("会改动系统自带的 Python", HIGH)
            op.ask = "安装软件"
            op.category = "install"
            return op
        if sub[:1] in (["uninstall"], ["remove"]):
            pk = _positional(sub[1:])
            op.phrase = "卸载 Python 包 " + "、".join(pk[:3])
            op.sentence = op.phrase
            op.risk("会卸载软件包", MED)
            op.ask = "卸载软件"
            op.category = "install"
            return op
        op.phrase, op.sentence, op.trivial, op.category = "查看 Python 包", "查看已安装的 Python 包", True, "read"
        return op
    if c in ("apt", "apt-get", "snap", "dnf", "yum", "apk", "dpkg", "brew", "flatpak"):
        sub = [a for a in rest if not a.startswith("-")]
        act = sub[0] if sub else ""
        pk = sub[1:]
        if act in ("install", "reinstall", "add") or (c == "dpkg" and "-i" in rest):
            label = "、".join(pk[:3]) + ("等" if len(pk) > 3 else "")
            op.phrase = f"安装系统软件 {label}".strip()
            op.sentence = f"在这台电脑上安装系统软件：{label}".rstrip("：")
            for p in pk[:6]:
                op.hit("package", p)
            op.risk("会安装系统软件（改动系统）", HIGH)
            op.ask = "安装系统软件"
            op.category = "install"
        elif act in ("remove", "purge", "autoremove", "erase", "del"):
            op.phrase = "卸载系统软件 " + "、".join(pk[:3])
            op.sentence = op.phrase
            op.risk("会卸载系统软件", HIGH)
            op.ask = "卸载系统软件"
            op.category = "install"
        elif act in ("update", "upgrade", "full-upgrade", "dist-upgrade", "refresh"):
            op.phrase = "更新系统软件源" if act in ("update", "refresh") else "升级系统软件"
            op.sentence = op.phrase
            op.risk("会访问网络", LOW)
            if act != "update":
                op.risk("会升级系统软件", HIGH)
                op.ask = "升级系统软件"
            op.category = "install"
        else:
            op.phrase, op.sentence, op.trivial, op.category = "查看系统软件", "查看系统软件信息", True, "read"
        return op
    if c in ("npm", "pnpm", "yarn", "bun", "npx", "corepack") and rest:
        sub = rest[0]
        glob_ = "-g" in rest or "--global" in rest
        if sub in ("i", "install", "add", "ci") or (c == "yarn" and sub == "global"):
            pk = [a for a in _positional(rest[1:]) if a != "add"]
            label = "、".join(pk[:3]) if pk else "项目依赖"
            op.phrase = f"安装 Node 包 {label}"
            op.sentence = f"从网上下载并安装 Node 包：{label}" + ("（全局安装）" if glob_ else "")
            for p in pk[:6]:
                op.hit("package", p)
            op.risk("会从网上下载并安装软件包", HIGH if glob_ else MED)
            op.ask = "安装软件"
            op.category = "install"
            return op
        if c == "npx":
            op.phrase = f"运行 {rest[0]}"
            op.sentence = f"用 npx 运行 {rest[0]}（可能先从网上下载）"
            op.risk("可能会从网上下载并运行程序", MED)
            return op
        op.phrase, op.sentence = f"运行 {c} {sub}", f"运行 {c} {sub}"
        return op
    if c in ("curl", "wget", "aria2c", "http", "httpie"):
        urls = [a for a in rest if re.match(r"^(https?|ftp)://", a) or re.match(r"^[\w.-]+\.[a-z]{2,}(:\d+)?(/|$)", a)
                or re.match(r"^(localhost|\d{1,3}(\.\d{1,3}){3})(:\d+)?(/|$)", a)]   # v3.9.22 localhost:8000/api
        name, host = site_of(urls[0]) if urls else ("", "")
        up = any(a in ("-T", "--upload-file", "-F", "--form", "-d", "--data", "--data-binary", "--data-raw",
                       "--data-urlencode", "--json", "--post-file", "--post-data", "--body-file") or a.startswith(("-d", "-F", "--data"))
                 for a in rest)
        method = next((rest[i + 1].upper() for i, a in enumerate(rest) if a in ("-X", "--request", "--method") and i + 1 < len(rest)), "")
        up = up or method in ("POST", "PUT", "PATCH", "DELETE")
        out = next((rest[i + 1] for i, a in enumerate(rest) if a in ("-o", "--output", "-O", "--output-document")
                    and i + 1 < len(rest) and not rest[i + 1].startswith("-")), "")
        if not out and any(a in ("-O", "--remote-name") for a in rest) and urls:
            out = urlparse(urls[0]).path.rsplit("/", 1)[-1] or ""
        if c == "wget" and not out and urls and not any(a in ("-q", "-qO-", "-O-") for a in rest):
            out = urlparse(urls[0]).path.rsplit("/", 1)[-1] or ""
        if host:
            op.hit("site", name if name == host else f"{name}（{host}）")
        local = host in ("localhost", "127.0.0.1", "0.0.0.0") or host.startswith(("10.", "192.168.", "172.16.")) or host.endswith(".lan")
        if up:
            op.phrase = f"向 {name or '网络'} 发送数据"
            op.sentence = f"通过网络向 {name or host or '一个网址'} 上传/提交数据"
            op.risk(f"会把数据发到 {name or host or '外部网站'}", HIGH if not local else MED)
            if not local:
                op.ask = "把数据发到网上"
            op.category = "network"
        else:
            if out:
                p = norm_path(out, ctx.cwd)
                op.hit("create", _target_label(p))
                _write_target(op, p)
                op.phrase = f"从 {name or '网上'} 下载 {_target_label(p)}"
            else:
                op.phrase = f"访问 {name or '网址'}"
            op.sentence = op.phrase + ("（本机服务）" if local else "")
            op.risk("会访问网络" if not local else "会访问本机服务", LOW)
            op.category = "network"
            op.trivial = local and not out
        return op
    if c in ("scp", "sftp", "nc", "ncat", "socat", "rsync", "ftp", "rclone"):
        op.phrase = "把文件传到其他电脑"
        op.sentence = f"用 {c} 把文件传到这台电脑以外的地方"
        op.risk("会把文件传出这台电脑", HIGH)
        op.ask = "把文件传出这台电脑"
        op.category = "network"
        return op
    if c in ("ssh", "mosh"):
        host = next((a for a in rest if not a.startswith("-")), "")
        op.phrase = f"远程登录 {host}".strip()
        op.sentence = f"通过 SSH 登录另一台电脑 {host} 执行命令".replace("  ", " ")
        op.hit("site", host)
        op.risk("会登录到另一台电脑", HIGH)
        op.ask = "远程登录其他电脑"
        op.category = "network"
        return op
    if c == "git" and rest:
        sub = next((a for a in rest if not a.startswith("-")), "")
        if sub == "push":
            op.phrase = "把代码推送到远程仓库"
            op.sentence = "用 git push 把本地的代码改动发布到远程仓库"
            op.risk("会把代码公开/发布到远程仓库", HIGH)
            if "-f" in rest or "--force" in rest or any(a.startswith("--force") for a in rest):
                op.risk("强制推送会覆盖远程的历史", HIGH)
            op.ask = "发布代码"
            op.category = "publish"
        elif sub == "clone":
            url = next((a for a in rest if "://" in a or "@" in a), "")
            nm, host = site_of(url)
            op.phrase = f"下载代码仓库{(' ' + url.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')) if url else ''}"
            op.sentence = f"从 {nm or '网上'} 下载代码仓库到这台电脑"
            if host:
                op.hit("site", nm)
            op.risk("会访问网络", LOW)
            op.category = "network"
        elif sub in ("reset", "clean", "checkout", "restore") and ("--hard" in rest or "-f" in rest or "-fd" in rest or "-fdx" in rest or "." in rest):
            op.phrase = "丢弃代码改动"
            op.sentence = f"用 git {sub} 丢弃本地没保存的改动"
            op.risk("会丢掉没提交的改动", MED)
            op.category = "delete"
        else:
            op.phrase = f"git {sub}".strip()
            op.sentence = f"运行 git {sub}".strip()
            op.trivial = sub in ("status", "log", "diff", "show", "branch", "remote", "rev-parse", "config", "ls-files", "fetch", "")
            op.category = "read" if op.trivial else "edit"
        return op
    if c in ("kill", "pkill", "killall", "xkill"):
        tg = _positional(rest, ("-s", "-n", "-u"))
        label = "、".join(tg[:3]) if tg else "程序"
        op.phrase = f"结束程序 {label}"
        op.sentence = f"强制结束正在运行的程序：{label}"
        op.hit("app", label)
        op.risk("会结束正在运行的程序（没保存的内容会丢）", MED)
        op.category = "app"
        return op
    if c in ("systemctl", "service"):
        sub = next((a for a in rest if not a.startswith("-")), "")
        unit = next((a for a in rest[rest.index(sub) + 1:] if not a.startswith("-")), "") if sub in rest else ""
        op.phrase = f"{ {'start': '启动', 'stop': '停止', 'restart': '重启', 'enable': '开机启动', 'disable': '取消开机启动'}.get(sub, '查看') }系统服务 {unit}".strip()
        op.sentence = op.phrase
        op.trivial = sub in ("status", "is-active", "list-units", "")
        if not op.trivial:
            op.risk("会改动系统服务", MED)
        op.category = "privileged" if not op.trivial else "read"
        return op
    if c in ("chmod", "chown", "chgrp"):
        tg = [norm_path(t, ctx.cwd) for t in _positional(rest)[1:]]
        op.phrase = f"修改文件权限 {'、'.join(_target_label(p) for p in tg[:2])}"
        op.sentence = op.phrase
        if "-R" in rest and any(zone(p) in ("system", "home") for p in tg):
            op.risk("会批量修改工作区以外文件的权限", HIGH)
        elif "777" in rest:
            op.risk("会让所有人都能改这些文件", MED)
        op.category = "edit"
        return op
    if c in ("ffmpeg",):
        outs = [a for a in _positional(rest, ("-i", "-c", "-c:v", "-c:a", "-b:v", "-b:a", "-r", "-s", "-t", "-ss", "-to", "-vf",
                                              "-af", "-filter_complex", "-map", "-f", "-crf", "-preset", "-pix_fmt", "-ar", "-ac",
                                              "-loglevel", "-threads", "-movflags", "-q:v", "-frames:v", "-vcodec", "-acodec",
                                              "-shortest", "-aspect", "-g", "-tune", "-profile:v", "-level", "-maxrate", "-bufsize"))]
        ins = [rest[i + 1] for i, a in enumerate(rest) if a == "-i" and i + 1 < len(rest)]
        out = outs[-1] if outs else ""
        op.hit("app", "FFmpeg")
        for i in ins[:3]:
            op.hit("read", _target_label(norm_path(i, ctx.cwd)))
        if out:
            p = norm_path(out, ctx.cwd)
            op.hit("create", _target_label(p))
            _write_target(op, p)
            op.phrase = f"生成{kind_cn(p)} {_target_label(p)}"
        else:
            op.phrase = "处理音视频"
        op.sentence = "用 FFmpeg " + op.phrase.replace("生成", "生成")
        op.category = "edit"
        return op
    if c in ("tar", "zip", "unzip", "7z", "gzip", "gunzip", "xz", "unrar"):
        pos = _positional(rest, ("-C", "-d", "-f", "-x"))
        extract = c in ("unzip", "gunzip", "unrar") or any(a.startswith("-x") or a.startswith("x") for a in rest[:1]) or "-x" in rest
        if c == "unzip" and "-l" in rest:
            op.phrase, op.sentence, op.trivial, op.category = "查看压缩包", "查看压缩包里的文件", True, "read"
            return op
        op.phrase = ("解压 " if extract else "打包 ") + (_target_label(norm_path(pos[0], ctx.cwd)) if pos else "")
        op.sentence = op.phrase
        op.category = "edit"
        return op
    if c == "sed":
        if any(a.startswith("-i") or a == "--in-place" for a in rest):
            tg = [norm_path(t, ctx.cwd) for t in _positional(rest, ("-e", "-f"))[1:]]
            for p in tg:
                _write_target(op, p)
            op.phrase = "修改文件内容 " + "、".join(_target_label(p) for p in tg[:2])
            op.sentence = op.phrase + "（直接改原文件）"
            op.category = "edit"
            return op
        op.phrase, op.sentence, op.trivial, op.category = "处理文本", "处理文本（只读）", True, "read"
        return op
    if c == "tee":
        tg = [norm_path(t, ctx.cwd) for t in _positional(rest)]
        for p in tg:
            _write_target(op, p, append="-a" in rest)
        op.phrase = "写入 " + "、".join(_target_label(p) for p in tg[:2])
        op.sentence = op.phrase
        op.category = "edit"
        return op
    if c == "find" and ("-delete" in rest or ("-exec" in rest and "rm" in rest)):
        root = norm_path(_positional(rest)[0] if _positional(rest) else ".", ctx.cwd)
        d = _delete_op([root], True, ctx, [root + "/*"])
        d.phrase = f"批量删除 {_target_label(root)} 里找到的文件"
        d.sentence = d.phrase
        return d
    if c in ("xdg-open", "open", "google-chrome", "chromium", "chromium-browser", "firefox", "librecad", "gimp", "code"):
        target = next((a for a in rest if not a.startswith("-")), "")
        op.phrase = f"打开 {APP_CN.get(c, c)}" + (f" {short(target, 30)}" if target else "")
        op.sentence = op.phrase
        op.hit("app", APP_CN.get(c, c))
        op.category = "app"
        return op
    if c in INTERPRETERS:
        if c.startswith("python") and ctx.stdin_code and (not rest or rest[:1] == ["-"]):   # python3 - <<'EOF'
            _python_code_op(ctx.stdin_code, ctx, op)
            if not op.phrase:
                op.phrase, op.sentence = "运行一段 Python 代码", "运行一段 Python 代码"
            return op
        if "-c" in rest and c.startswith("python"):
            code = rest[rest.index("-c") + 1] if rest.index("-c") + 1 < len(rest) else ""
            _python_code_op(code, ctx, op)
            return op
        if c in ("bash", "sh", "zsh") and "-c" in rest:
            inner = rest[rest.index("-c") + 1] if rest.index("-c") + 1 < len(rest) else ""
            sub = explain_shell(inner, ctx.created)
            op.phrase, op.sentence = sub["phrase"], sub["what"]
            op.impact = sub["impact"]
            for r in sub["risks"]:
                op.risk(r, LOW)
            op.sev = sub["sev"]
            op.ask = sub["ask"]
            op.category = sub["category"]
            return op
        script = next((a for a in rest if not a.startswith("-")), "")
        if script:
            op.phrase = f"运行脚本 {readable(norm_path(script, ctx.cwd))}"
            op.sentence = f"运行脚本《{readable(norm_path(script, ctx.cwd))}》"
            op.hit("read", readable(norm_path(script, ctx.cwd)))
        else:
            op.phrase = f"运行 {c}"
            op.sentence = f"运行 {c} 程序"
        return op
    if c in ("docker", "podman"):
        sub = next((a for a in rest if not a.startswith("-")), "")
        op.phrase = f"运行 Docker {sub}".strip()
        op.sentence = op.phrase
        op.trivial = sub in ("ps", "images", "logs", "inspect", "version", "info")
        if sub in ("rm", "rmi", "prune", "system", "volume", "kill", "stop"):
            op.risk("会删除或停止容器", MED)
        op.category = "app"
        return op
    if c in READ_ONLY:
        op.trivial = True
        op.category = "read"
        if c in ("ls", "ll", "tree", "find", "du"):
            tg = _positional(rest, ("-name", "-type", "-maxdepth", "-mindepth", "-iname", "-path", "-d", "-L"))
            op.phrase = "查看 " + ("、".join(readable(norm_path(t, ctx.cwd)) for t in tg[:2]) or readable(ctx.cwd)) + " 里的文件"
        elif c in ("cat", "head", "tail", "less", "more", "wc", "file", "stat", "pdfinfo", "identify", "ffprobe"):
            tg = _positional(rest, ("-n", "-c"))
            op.phrase = "查看 " + ("、".join(readable(norm_path(t, ctx.cwd)) for t in tg[:2]) or "输出")
        elif c in ("xdotool", "wmctrl"):
            op.phrase = "在桌面上操作窗口/键盘鼠标"
        else:
            op.phrase = f"运行 {c}"
        op.sentence = op.phrase
        return op
    op.phrase = f"运行 {APP_CN.get(c, c)}"
    op.sentence = f"运行命令 {APP_CN.get(c, c)}"
    if c in APP_CN:
        op.hit("app", APP_CN[c])
    return op


def cjoin(parts: list[str], word: str) -> str:
    """Join Chinese phrases with a word, spacing it from a Latin end/start (删除 recalc 并重新生成 Excel)."""
    out = ""
    for p in parts:
        if not out:
            out = p
            continue
        left = " " if re.search(r"[A-Za-z0-9._/)\]]$", out) else ""
        right = " " if re.match(r"^[A-Za-z0-9]", p) else ""
        out += left + word + right + p
    return out


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w]*)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
_SH_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "done", "fi", "esac"}


def _prep(cmd: str, ctx: Optional["Ctx"] = None) -> str:
    """v3.9.22 (task-ca7 cards: 「运行 \\necho 并运行 \\ncurl」「运行 for 并运行 do」「运行 python3 并运行 p」):
    join backslash-newline continuations and drop heredoc bodies (the text fed to cat / python is data, not commands;
    the redirect that writes it stays)."""
    cmd = re.sub(r"\\\r?\n", " ", cmd or "")
    def sub(m):
        if ctx is not None:
            ctx.heredocs[m.group(2) + "_BODY"] = m.group(3)
        return "<< " + m.group(2) + "_BODY\n"
    return _HEREDOC.sub(sub, cmd)


def explain_shell(cmd: str, created: Optional[set] = None) -> dict:
    """{phrase, what, impact, risks, sev, ask, category, ops} for a shell line."""
    ctx = Ctx(set(created or ()) | created_dirs(cmd or ""))   # a folder this line (re)creates is scratch
    cmd = _prep(cmd, ctx)
    parts = split_shell(cmd or "")
    if parts is None:
        return {"phrase": "运行一段命令", "what": "运行一段命令（格式复杂，看技术细节）", "impact": [], "risks": ["命令格式复杂，自动解读不了"],
                "sev": MED, "ask": None, "category": "other", "ops": []}
    ops = []
    for words in parts:
        op = _shell_op(words, ctx)
        if op is not None:
            ops.append(op)
    pipe_sh = re.search(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", cmd or "")
    meaningful = [o for o in ops if not o.trivial and o.phrase]
    shown = meaningful or [o for o in ops if o.phrase]
    phrases = []
    for o in shown:
        if o.phrase not in phrases:
            phrases.append(o.phrase)
    phrase = cjoin(phrases[:2], "并") + ("等" if len(phrases) > 2 else "") if phrases else "运行命令"
    sentences = []
    for o in (meaningful or shown):
        if o.sentence and o.sentence not in sentences:
            sentences.append(o.sentence)
    if not sentences:
        sentences = ["查看文件和信息（只读）"]
    what = "先" + sentences[0] + "，再" + "，然后".join(sentences[1:3]) if len(sentences) > 1 else sentences[0]
    if len(sentences) > 3:
        what += "，等等"
    impact, risks, sev, ask, cat, seen = [], [], LOW, None, "read", set()
    rank = ["read", "other", "app", "edit", "network", "delete", "install", "privileged", "publish"]
    for o in ops:
        for i in o.impact:
            key = re.sub(r"（.*?）$", "", i["label"]).rstrip("/")
            if key not in seen:
                seen.add(key)
                impact.append(dict(i))
            elif i["kind"] in ("write", "delete"):     # read then overwritten: show the stronger verb
                for j in impact:
                    if re.sub(r"（.*?）$", "", j["label"]).rstrip("/") == key and j["kind"] in ("read", "create"):
                        j["kind"] = i["kind"]
        for r in o.risks:
            if r not in risks:
                risks.append(r)
        sev = max(sev, o.sev)
        ask = ask or o.ask
        if rank.index(o.category if o.category in rank else "other") > rank.index(cat):
            cat = o.category
    if pipe_sh:
        risks.insert(0, "会从网上下载脚本并直接运行")
        sev, ask, cat = HIGH, ask or "下载并直接运行网上的脚本", "install"
        phrase = "下载并运行安装脚本"
        what = "从网上下载一个脚本并直接运行（通常用来安装软件）"
    return {"phrase": phrase, "what": what + "。", "impact": impact[:10], "risks": risks[:6], "sev": sev, "ask": ask,
            "category": cat, "ops": ops}


def created_dirs(cmd: str, cwd: str = "~") -> set:
    """Folders a shell line makes (mkdir -p X, --outdir X), normalized: remembered so a later rm -rf of them is low risk."""
    out = set()
    ctx = Ctx()
    for words in split_shell(cmd or "") or []:
        w, _ = _strip_prefix(words)
        if not w:
            continue
        c = os.path.basename(w[0])
        if c == "cd":
            ctx.cwd = norm_path(w[1] if len(w) > 1 else "~", ctx.cwd)
        elif c == "mkdir":
            for t in _positional(w[1:], ("-m",)):
                q = norm_path(t, ctx.cwd)
                out.add(q)
                if "-p" in w[1:] or "--parents" in w[1:]:   # mkdir -p build/x also made build (below the cwd only)
                    base = ctx.cwd.rstrip("/") + "/"
                    while "/" in q and q.startswith(base) and q.rsplit("/", 1)[0] + "/" != base:
                        q = q.rsplit("/", 1)[0]
                        out.add(q)
        elif "--outdir" in w:
            i = w.index("--outdir")
            if i + 1 < len(w):
                out.add(norm_path(w[i + 1], ctx.cwd))
    return out


# ------------------------------------------------------------------------------------------------ tool templates

def _mask(s: str, secret: bool) -> str:
    return "••••" if secret else short(s, 40)


def summarize(tool: str, payload: Optional[dict], reason: str = "", thought: str = "", level: Optional[int] = None,
              created: Optional[set] = None, machine: str = "") -> dict:
    """The plain-language card for one approval. Never raises: falls back to a generic summary."""
    p = dict(payload or {})
    try:
        s = _summarize(tool, p, reason, created)
    except Exception:  # noqa: BLE001  never let a summary break a confirmation
        s = {"title": f"确认：{tool}", "what": "执行一步操作（详情见技术细节）。", "impact": [], "risks": [], "sev": MED, "category": "other"}
    if machine:
        s["impact"] = [{"kind": "computer", "label": machine}] + list(s.get("impact") or [])
    sev = s.pop("sev", MED)
    if PAY_HINT.search(" ".join([str(thought or ""), str(reason or "")])) and s.get("category") not in ("pay",) and level and level >= 3:
        s.setdefault("risks", []).insert(0, "这一步可能涉及付款")
        sev = HIGH
    s["risk"] = RISK_NAME[sev]
    s["risk_cn"] = RISK_CN[s["risk"]]
    w = str(thought or "").strip()
    s["why"] = w if len(w) <= 300 else w[:299] + "…"
    tech = {"tool": tool, "reason": reason}
    if level is not None:
        tech["level"] = int(level)
    if p.get("command"):
        tech["command"] = str(p["command"])[:4000]
    else:
        tech["payload"] = {k: (v if not isinstance(v, str) else v[:600]) for k, v in p.items()
                           if k not in ("content_b64", "confirm_timeout_s", "drafts") and not str(k).startswith("_")}
    s["tech"] = tech
    s["title"] = short(s.get("title") or "确认这一步", 60)
    return s


PAY_HINT = re.compile(r"(checkout|pay now|place order|立即支付|确认付款|提交订单|下单|支付|付款)", re.I)


def _summarize(tool: str, p: dict, reason: str, created: Optional[set]) -> dict:
    secret = bool(p.get("secret"))
    if tool in ("shell", "remote_exec") or p.get("command") and tool not in ("order_confirm", "xhs_review"):
        e = explain_shell(str(p.get("command") or ""), created)
        title = "确认：" + e["phrase"]
        what = e["what"]
        impact = list(e["impact"])
        if tool == "remote_exec":
            host = str(p.get("host") or "")
            title = f"确认：在运维主机 {host} 上{e['phrase']}" if host else title
            what = f"在你授权的运维主机 {host} 上执行：" + what
            impact.insert(0, {"kind": "computer", "label": f"运维主机 {host}".strip()})
            e["risks"].insert(0, "会在这台云电脑以外的主机上执行")
            e["sev"] = max(e["sev"], MED)
        return {"title": title, "what": what, "impact": impact, "risks": e["risks"], "sev": e["sev"], "category": e["category"]}
    if tool == "order_confirm":
        item = str(p.get("商品") or "")
        amt = str(p.get("实付金额") or "")
        return {"title": f"确认：下单并付款 {amt}".strip(),
                "what": f"在购物网站提交订单：{short(item, 40)}，{p.get('规格') or '默认规格'} × {p.get('数量') or 1}，实付 {amt}。",
                "impact": [i for i in ({"kind": "site", "label": site_of(str(p.get('链接') or ''))[0] or "购物网站"},
                                        {"kind": "money", "label": amt}, {"kind": "place", "label": short(str(p.get('收货地址') or ''), 30)})
                           if i["label"]],
                "risks": ["会花钱", "提交后要走退款流程才能撤销"], "sev": HIGH, "category": "pay"}
    if tool == "xhs_review":
        n = p.get("count") or len(p.get("drafts") or [])
        return {"title": f"确认：小红书 {n} 条草稿", "what": f"审核 {n} 条准备发到小红书的内容；允许 = 全部按原文通过。",
                "impact": [{"kind": "site", "label": "小红书"}], "risks": ["会以你的账号公开发布"], "sev": MED, "category": "message"}
    if tool in ("browser_click", "click_element") or (tool == "bos_act" and (p.get("kind") or "click") == "click"):
        label = str(p.get("element") or p.get("text") or p.get("target") or p.get("selector") or "")
        label = re.sub(r"^(button|link|menuitem|tab|checkbox)\s+", "", label)
        pay = bool(PAY_HINT.search(label))
        return {"title": f"确认：在网页上点击「{short(label, 24)}」" if label else "确认：点击网页上的按钮",
                "what": f"在浏览器里点击「{short(label, 40)}」" + ("，这会提交订单/付款。" if pay else "。"),
                "impact": [{"kind": "site", "label": "当前网页"}],
                "risks": (["会花钱", "不可撤销"] if pay else ["会在网页上执行这个操作"]), "sev": HIGH if pay else MED,
                "category": "pay" if pay else "account"}
    if tool in ("browser_fill", "type") or tool == "bos_act":
        field = str(p.get("field") or p.get("element") or "")
        text = str(p.get("text") or p.get("value") or "")
        sub = bool(p.get("submit"))
        return {"title": f"确认：在「{short(field, 16)}」里填写内容" if field else "确认：输入文字",
                "what": (f"在「{short(field, 30)}」里填写「{_mask(text, secret)}」" if field else f"在当前窗口输入「{_mask(text, secret)}」")
                + ("，然后提交。" if sub else "。"),
                "impact": [{"kind": "site", "label": "当前网页"}], "risks": ["会把这些内容填进网页"] + (["会提交表单"] if sub else []),
                "sev": MED, "category": "account"}
    if tool in ("click", "double_click", "right_click"):
        return {"title": "确认：点击屏幕上的一个位置", "what": f"在桌面坐标 ({p.get('x')}, {p.get('y')}) 处点击。",
                "impact": [], "risks": ["可能会提交或付款（看思考说明）"], "sev": MED, "category": "account"}
    if tool in ("browser_open", "browser_navigate", "bos_navigate", "files_download"):
        url = str(p.get("url") or "")
        name, host = site_of(url)
        priv = "private network" in reason or host.startswith(("10.", "192.168.", "172.16.")) or host in ("localhost", "127.0.0.1")
        dl = tool == "files_download"
        r = ["会访问内网地址"] if priv else (["这个网站不在允许列表里"] if "allowlist" in reason else ["会访问网络"])
        impact = [{"kind": "site", "label": name if name == host else f"{name}（{host}）"}]
        if dl and p.get("path"):
            impact.append({"kind": "create", "label": readable(norm_path(str(p["path"])))})
        return {"title": f"确认：{'下载文件自' if dl else '打开网站'} {name or '网址'}",
                "what": f"在浏览器里打开 {short(url, 60)}。" if not dl else f"从 {name} 下载文件到这台电脑。",
                "impact": impact, "risks": r, "sev": MED, "category": "network"}
    if tool in ("files_write", "files_upload", "files_patch"):
        path = norm_path(str(p.get("path") or ""), "~/workspace")
        op = Op()
        _write_target(op, path)
        return {"title": f"确认：写入文件 {readable(path)}", "what": f"把内容写进《{readable(path)}》。",
                "impact": [{"kind": "write", "label": readable(path)}],
                "risks": op.risks or ["会改动这个文件"] + (["文件可能含有密码/密钥"] if "sensitive" in reason else []),
                "sev": max(op.sev, MED if "sensitive" in reason else LOW), "category": "edit"}
    if tool == "send_file":
        path = norm_path(str(p.get("path") or ""), "~/workspace")
        return {"title": f"确认：把 {readable(path)} 发给你", "what": f"把《{readable(path)}》作为结果文件发给你。",
                "impact": [{"kind": "read", "label": readable(path)}], "risks": ["文件可能含有敏感信息"], "sev": MED, "category": "message"}
    if tool.startswith("mcp_") or tool.startswith("ext_") or tool in ("deploy_server",):
        name = tool[4:] if tool[:4] in ("mcp_", "ext_") else tool
        return {"title": f"确认：调用外部工具 {short(name, 30)}", "what": f"调用你接入的外部工具「{name}」，它可能在这台电脑以外产生效果（发消息、改数据、花钱等）。",
                "impact": [{"kind": "app", "label": name}], "risks": ["会在这台电脑以外产生效果"], "sev": MED, "category": "network"}
    return {"title": f"确认：{reason or tool}", "what": f"执行「{tool}」这一步。", "impact": [], "risks": [reason] if reason else [],
            "sev": MED, "category": "other"}


# ------------------------------------------------------------------------------------------------ takeover

TAKEOVER_ACTIONS = {
    "login": ("登录", "登录账号"), "sms_code": ("输入短信验证码", "输入手机收到的短信验证码"),
    "captcha": ("完成人机验证", "完成页面上的人机验证"), "slider": ("完成滑块验证", "拖动滑块完成验证"),
    "qr_scan": ("扫码登录", "用手机 App 扫描屏幕上的二维码"), "payment": ("完成付款", "完成付款（输入支付密码/指纹）"),
    "2fa": ("完成二次验证", "完成二次验证（验证器/邮箱验证码）"), "confirm": ("确认操作", "在页面上确认操作"),
    "other": ("处理一下", "在桌面上处理一下"),
}
_ACTION_WORDS = [("sms_code", r"短信|验证码|sms|otp|手机.*码"), ("slider", r"滑块|slider|拖动"), ("qr_scan", r"扫码|二维码|qr"),
                 ("captcha", r"captcha|人机|cloudflare|verify you are human|验证"), ("payment", r"支付|付款|pay|密码.*付"),
                 ("2fa", r"2fa|二次验证|两步|authenticator"), ("login", r"登录|login|sign ?in|登陆")]


def guess_action(text: str) -> str:
    for k, pat in _ACTION_WORDS:
        if re.search(pat, text or "", re.I):
            return k
    return "other"


def takeover_card(args: dict, intent: str = "", page: Optional[dict] = None, machine: str = "") -> dict:
    """The takeover request shown to the user: {title, action, action_cn, site, site_host, url, page_title, steps, why, machine}."""
    a = dict(args or {})
    page = page or {}
    reason = str(a.get("reason") or "").strip()
    action = str(a.get("action") or "").strip().lower() or guess_action(reason + " " + str(a.get("what_to_do") or ""))
    if action not in TAKEOVER_ACTIONS:
        action = guess_action(action) if action else "other"
    act_short, act_long = TAKEOVER_ACTIONS[action]
    url = str(a.get("url") or page.get("url") or "")
    site = str(a.get("site") or "").strip()
    host = site_of(url)[1] if url else ""
    if not site:
        site = site_of(url)[0] if url else ""
    if not site and page.get("title"):
        site = short(str(page["title"]), 20)
    steps = str(a.get("what_to_do") or a.get("steps") or "").strip()
    if not steps:
        steps = reason or act_long
    where = f"在{site}" if site else "在桌面上"
    page_kind = {"login": "登录页", "sms_code": "登录页" if re.search(r"登录|login", reason + url, re.I) else "页面", "payment": "收银台",
                 "qr_scan": "登录页"}.get(action, "页面")
    if action == "other" and reason:
        title = f"需要你接管：{short(reason, 40)}"
    else:
        title = f"需要你接管：{where}{page_kind if site else ''}{act_short}"
    return {"kind": "takeover", "title": short(title, 60), "action": action, "action_cn": act_short, "site": site,
            "site_host": host, "url": url, "page_title": short(str(page.get("title") or ""), 60), "steps": short(steps, 200),
            "reason": short(reason, 200), "why": short(str(intent or "").strip(), 200), "machine": machine,
            "done_label": "完成，交还给 Aiw"}
