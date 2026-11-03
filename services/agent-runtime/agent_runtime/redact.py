"""v3.9.17 密码保护: passwords written in a task never reach the model, the events, the export or the database.

task-833's prompt carried "…用户名root 密码<明文>" and the export JSON still had it in task.prompt and in the
task.started event (the old export regex only knew `password=`). Now:

* `extract(prompt)` finds the credentials the user wrote (密码/口令/password/pwd …, sshpass -p, user:pass@host) with the
  host and user next to them, and returns the prompt with every value replaced by ***. The values go to the vault
  (sessions.py, secret `task_cred_<task>_<n>`); ssh_exec takes them from there by host.
* `mask(text, known)` replaces known secret values and anything that looks like one (密码 xxx, password=xxx,
  *_KEY= / *_SECRET= / *_TOKEN= env lines, Bearer tokens, sk-… keys) with ***. `mask_obj` walks dicts/lists.

Pure functions, tested in tests/test_v3917_redact.py.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

STARS = "***"

# a value must have at least one ASCII printable character ("密码保护" / "密码打码" are words, not passwords)
_VAL = r"([^\s，,。;；、'\"“”‘’()（）<>【】\[\]{}]{3,64})"
_PW_WORD = r"(?:(?:登录|root|ssh|账号)?(?:密码|口令)|(?<![A-Za-z_])(?:password|passwd|passphrase|pwd|pass)(?![A-Za-z_]))"
PW_RE = re.compile(r"(?i)" + _PW_WORD + r"\s*(?:是|为|:|：|=|＝)?\s*['\"“]?" + _VAL)
SSHPASS_RE = re.compile(r"(sshpass\s+-p\s*)(['\"]?)([^\s'\"]+)\2")
URL_PW_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s/]+)@")
HOST_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])|\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|cn|net|org|io|lan|local|top|xyz|cc))\b", re.I)
USER_RE = re.compile(r"(?i)(?:用户名|用户|账号|帐号|user(?:name)?|login)\s*(?:是|为|:|：|=)?\s*([A-Za-z_][A-Za-z0-9_.-]{0,31})")
AT_USER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.-]{0,31})@((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)

# masking without knowing the value
_ENV_SECRET = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]*(?:_KEY|_SECRET|_TOKEN|_PASSWORD|_PASSWD|_PWD|_PASS|APIKEY|_AK|_SK))"
    r"(\s*[\"']?\s*(?:=|:)\s*[\"']?)([^\s\"',;}]{3,})")
_GENERIC = [
    (re.compile(r"""(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key)(\s*["']?\s*[:=]\s*)(["']?)([^"'\s,;)}\]]{2,})"""),
     lambda m: m.group(1) + m.group(2) + m.group(3) + (m.group(4) if _placeholder(m.group(4)) else STARS)),
    (re.compile(r"(?i)((?:密码|口令)\s*(?:是|为|:|：|=)?\s*)([^\s，,。;；、'\"]{3,64})"),
     lambda m: m.group(1) + (m.group(2) if (_placeholder(m.group(2)) or not re.search(r"[!-~]", m.group(2))) else STARS)),
    (SSHPASS_RE, lambda m: m.group(1) + m.group(2) + STARS + m.group(2)),
    (URL_PW_RE, lambda m: m.group(1) + STARS + "@"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]{12,}"), lambda m: m.group(1) + STARS),
    (re.compile(r"(?i)(Authorization\s*[\"']?\s*[:=]\s*[\"']?(?:Bearer|Basic|token)\s+)[A-Za-z0-9._~+/=-]+"), lambda m: m.group(1) + STARS),
    (_ENV_SECRET, lambda m: m.group(1) + m.group(2) + (m.group(3) if _placeholder(m.group(3)) else STARS)),
]
_SECRET_KEYS = re.compile(r"(?i)^(password|passwd|pwd|secret|token|api_?key|access_?key|secret_?key|.*_(key|secret|token|password))$")


def _placeholder(v: str) -> bool:
    v = (v or "").strip("'\"")
    return (not v or v == STARS or v.startswith(("{{", "$", "<", "%(", "os.environ", "getpass", "env[", "***"))
            or v in ("None", "null", "PWD", "PASS", "PASSWORD", "password", "pw", "pwd", "USER", "HOST", "True", "False")
            or bool(re.fullmatch(r"[A-Z_]{2,}", v)))   # a variable name, not a value


def _looks_value(v: str) -> bool:
    return bool(v) and bool(re.search(r"[!-~]", v)) and not _placeholder(v) and not re.fullmatch(r"\*+", v)


def _near(text: str, pos: int, rx: re.Pattern, group_pick=None, span: int = 120) -> Optional[str]:
    """The closest match of rx to pos (before it first, then after)."""
    best, dist = None, 10 ** 9
    for m in rx.finditer(text[max(0, pos - span): pos + span]):
        start = m.start() + max(0, pos - span)
        d = abs(start - pos) + (0 if start <= pos else 5)
        v = group_pick(m) if group_pick else m.group(1)
        if v and d < dist:
            best, dist = v, d
    return best


def extract(prompt: str) -> tuple[str, list[dict]]:
    """(prompt with every password replaced by ***, [{value, host, user, kind}]). Same value twice = one entry."""
    text = prompt or ""
    creds: list[dict] = []
    seen: set = set()

    def add(value: str, pos: int, kind: str):
        value = value.strip().rstrip(".。")
        if not _looks_value(value) or value in seen:
            return
        seen.add(value)
        host = _near(text, pos, HOST_RE, lambda m: m.group(1) or m.group(2))
        user = _near(text, pos, USER_RE)
        if not user:
            au = _near(text, pos, AT_USER_RE)
            user = au
        creds.append({"value": value, "host": host or "", "user": user or "", "kind": kind})

    for m in PW_RE.finditer(text):
        add(m.group(1), m.start(), "password")
    for m in SSHPASS_RE.finditer(text):
        add(m.group(3), m.start(), "sshpass")
    for m in URL_PW_RE.finditer(text):
        add(m.group(2), m.start(), "url")
    clean = text
    for c in sorted(creds, key=lambda c: -len(c["value"])):
        clean = clean.replace(c["value"], STARS)
    return clean, creds


def mask(text: Any, known: Iterable[str] = ()) -> Any:
    """Known secret values and secret-looking assignments -> ***. Non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for v in sorted({k for k in known if k and len(k) >= 3}, key=len, reverse=True):
        out = out.replace(v, STARS)
    for rx, fn in _GENERIC:
        out = rx.sub(fn, out)
    return out


def mask_obj(obj: Any, known: Iterable[str] = ()) -> Any:
    known = list(known)
    if isinstance(obj, str):
        return mask(obj, known)
    if isinstance(obj, dict):
        return {k: (STARS if isinstance(v, str) and v and _SECRET_KEYS.match(str(k)) and not _placeholder(v)
                    else mask_obj(v, known)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v, known) for v in obj]
    return obj


def note_for(creds: list[dict], keys: list[str]) -> str:
    """The line the task gets instead of the password (the model sees where it is, never what it is)."""
    if not creds:
        return ""
    parts = []
    for c, k in zip(creds, keys):
        who = f"{c.get('user') or '?'}@{c.get('host') or '?'}"
        parts.append(f"{who} 的密码已存入保险箱（{{{{{k}}}}}）")
    return ("【密码保护】" + "；".join(parts) + "。连 SSH 用 ssh_exec（自动取密码，不要自己写 sshpass/paramiko 脚本）；"
            "网页登录在 type / bos_act 里写 {{键名}} 会自动填入。任何输出里都不要回显密码。")
