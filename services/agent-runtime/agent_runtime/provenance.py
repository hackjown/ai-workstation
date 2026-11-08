"""v3.9.26 (task-830): which command / script produced each delivered file, and the dev_fix rules built on it.

task-830 delivered ~/aiw-news/<date>.html made by ~/ai-workstation/news/aiw_news.py. Asked to fix it, the agent edited
the HTML with ad-hoc ~/workspace/inject3.py / restyle scripts; the next generator run (or the next patch on an older
copy) lost the earlier changes, so features kept disappearing.

* producer_for(path, shell_log): on send_file, look back through this task's shell steps (newest first) for the command
  that made the file: its JSON last line {"file": ...}, an output path in the command, or a script whose stdout names it.
* ProvenanceBook: per thread {artifact path -> {script, command, step, task, producers:[...]}}, kept across runs.
  source_of_truth() prefers the ORIGINAL generator over ad-hoc patchers (inject*/restyle*/patch*/fix*).
* artifact_edit(tool, args, book): in dev_fix mode a direct edit of a known artifact (files_write, sed -i, > / tee,
  inline python writing it, running a patch script that writes it) is refused:
  "这是 <script> 生成的产物，请修改源头脚本后重新生成".
* adhoc_patch_script(tool, args): creating inject*/restyle*/patch*/fix* scripts in ~/workspace gets a warning note.
Pure, tested in tests/test_v3926_devflow.py.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

ARTIFACT_EXT = re.compile(r"\.(html?|pdf|png|jpe?g|webp|svg|docx|xlsx|pptx|csv|json|md|txt|zip|mp4|dxf)$", re.I)
SOURCE_EXT = re.compile(r"\.(py|js|mjs|cjs|ts|tsx|jsx|vue|svelte|css|scss|less|html?|j2|jinja2?|tmpl|tpl|sh|go|rs|java|"
                        r"rb|php|toml|ya?ml|json|sql)$", re.I)
SCRIPT_EXT = re.compile(r"\.(py|js|mjs|cjs|ts|sh|rb|pl|php)$", re.I)
ADHOC = re.compile(r"(^|/)(inject|restyle|patch|fix|hotfix|tweak|modify|rewrite)[\w.-]*\.(py|js|sh|mjs)$", re.I)
_INTERP = re.compile(r"(?:^|[\s;&|(]|&&)(?:sudo\s+)?(?:timeout\s+(?:-\S+\s+)*\d+\S*\s+)?"
                     r"(python3?(?:\.\d+)?|node|bash|sh|deno|bun|ruby|perl|php|uv run|npx tsx|tsx)\s+(?:-[\w-]+\s+)*"
                     r"([^\s;&|<>]+\.(?:py|js|mjs|cjs|ts|sh|rb|pl|php))")
_HOME = re.compile(r"^(?:\$HOME|\$\{HOME\}|/data/home|/home/[\w.-]+|/root)(?=/|$)")


def norm(p: str, home: str = "") -> str:
    """A path with the home folder written as ~ (and quotes, ./ trimmed)."""
    p = str(p or "").strip().strip("'\"")
    if home and (p == home or p.startswith(home.rstrip("/") + "/")):
        p = "~" + p[len(home.rstrip("/")):]
    p = _HOME.sub("~", p)
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p)
    return p


def _ws(p: str) -> str:
    """A bare relative path is relative to ~/workspace (send_file, files_write)."""
    p = norm(p)
    if not p.startswith(("~", "/")):
        p = "~/workspace/" + p
    return p


def json_file_line(stdout: str) -> list[str]:
    """Paths named by a JSON line of the output ({"file": ...}, {"path": ...}, {"output": ...}), last lines first."""
    out = []
    for line in reversed([x.strip() for x in str(stdout or "").splitlines() if x.strip()][-6:]):
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        for k in ("file", "path", "output", "out", "html", "files", "outputs"):
            v = d.get(k)
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, str) and x.strip():
                    out.append(x.strip())
    return out


def script_of(command: str) -> Optional[str]:
    """The script a command runs (python3 ~/x/gen.py --out a.html -> ~/x/gen.py), with a relative path joined to the
    command's cd."""
    m = None
    for m in _INTERP.finditer(command or ""):
        pass
    if not m:
        return None
    s = m.group(2)
    if not s.startswith(("/", "~", "$")):
        cds = re.findall(r"\bcd\s+([^\s;&|]+)", command[:m.start(2)])
        if cds:
            s = cds[-1].rstrip("/") + "/" + s
    return norm(s)


def _mentions(command: str, path: str) -> bool:
    p = norm(path)
    if not p:
        return False
    variants = {p, p.replace("~/", "$HOME/"), os.path.basename(p)}
    c = norm(command)
    for v in variants:
        if v and v in command or (v and v in c):
            if v == os.path.basename(p):
                # a bare name counts only next to its folder (cd dir) or as an output argument
                d = os.path.basename(os.path.dirname(p))
                if d and d not in command and not re.search(r"(-o|--out\w*|>)\s*['\"]?" + re.escape(v), command):
                    continue
            return True
    return False


def copy_source(command: str, target: str) -> Optional[str]:
    """v3.9.29 (task-68b): `cp ~/aiw-news/a.html ~/workspace/a.html` -> the file the delivered copy came from."""
    t = norm(target)
    for m in re.finditer(r"\b(?:cp|rsync)\s+(?:-\w+\s+)*['\"]?([^\s;&|'\"]+)['\"]?\s+['\"]?([^\s;&|'\"]+)['\"]?", command or ""):
        src, dst = norm(m.group(1)), norm(m.group(2))
        if dst == t or (dst.endswith("/") and os.path.basename(src) == os.path.basename(t)) or \
                (os.path.basename(dst) == os.path.basename(t) and (t.endswith(dst.lstrip("~")) or dst.endswith(t.lstrip("~")))):
            return src
        if os.path.basename(t) == os.path.basename(src) and dst.rstrip("/") == os.path.dirname(t):
            return src
    return None


def producer_for(path: str, shell_log: list, home: str = "", _depth: int = 0) -> Optional[dict]:
    """shell_log: [{step, command, stdout}] oldest first. -> {script, command, step, how} or None.
    v3.9.29: a delivered COPY (cp gen-output ~/workspace/x) is traced back to the generator of the original."""
    target = norm(path, home)
    base = os.path.basename(target)
    for e in reversed(shell_log or []):
        if _depth < 3:
            src = copy_source(norm(str(e.get("command") or ""), home), target)
            if src and src != target:
                earlier = [x for x in shell_log if (x.get("step") or 0) <= (e.get("step") or 0)]
                prod = producer_for(src, earlier, home, _depth + 1)
                if prod:
                    return {**prod, "how": "copy", "copy_of": src}
        cmd = str(e.get("command") or "")
        out = str(e.get("stdout") or "")
        for f in json_file_line(out):
            nf = norm(f, home)
            if nf == target or (base and os.path.basename(nf) == base and (nf.endswith(target.lstrip("~")) or
                                                                         target.endswith(nf.lstrip("~")) or "/" not in target)):
                return {"script": script_of(cmd), "command": cmd[:600], "step": e.get("step"), "how": "json"}
        sc = script_of(cmd)
        if sc and _mentions(cmd, target):
            return {"script": sc, "command": cmd[:600], "step": e.get("step"), "how": "arg"}
        if sc and base and base in out:
            return {"script": sc, "command": cmd[:600], "step": e.get("step"), "how": "stdout"}
    return None


class ProvenanceBook:
    """artifact path (normalized, ~) -> {script, command, step, task, producers:[{script, command, task}]}"""

    def __init__(self, data: Optional[dict] = None):
        self.items: dict = {}
        for k, v in (data or {}).items():
            if isinstance(v, dict):
                self.items[norm(k)] = dict(v)

    def record(self, path: str, prod: Optional[dict], task_id: str = "", home: str = "") -> dict:
        p = norm(path, home)
        e = self.items.setdefault(p, {"producers": []})
        e["name"] = os.path.basename(p)
        if prod and prod.get("script"):
            entry = {"script": prod["script"], "command": prod.get("command", "")[:600], "task": task_id}
            if not any(x.get("script") == entry["script"] for x in e["producers"]):
                e["producers"].append(entry)
            e.update(script=prod["script"], command=prod.get("command", "")[:600], step=prod.get("step"), task=task_id)
        e["producers"] = e["producers"][-6:]
        return e

    def get(self, path: str, home: str = "") -> Optional[dict]:
        p = norm(path, home)
        if p in self.items:
            return self.items[p]
        if not p.startswith(("~", "/")):
            q = _ws(p)
            if q in self.items:
                return self.items[q]
        return None

    def generator(self, path: str) -> Optional[str]:
        """The original generator of an artifact: the first producer that is not an ad-hoc patch script."""
        e = self.get(path) or {}
        prods = e.get("producers") or []
        for x in prods:
            if x.get("script") and not ADHOC.search(x["script"]):
                return x["script"]
        return (prods[0].get("script") if prods else None) or e.get("script")

    def generator_command(self, path: str) -> Optional[str]:
        e = self.get(path) or {}
        g = self.generator(path)
        for x in e.get("producers") or []:
            if x.get("script") == g:
                return x.get("command")
        return e.get("command")

    def source_of_truth(self) -> Optional[str]:
        """The generator of the newest artifact that has one."""
        for p in reversed(list(self.items)):
            g = self.generator(p)
            if g:
                return g
        return None

    def artifacts(self) -> list[str]:
        return list(self.items)

    def match(self, text: str) -> Optional[str]:
        """The known artifact a piece of text (a path, a command) refers to."""
        for p in self.items:
            if _mentions(text or "", p):
                return p
        return None

    def to_dict(self) -> dict:
        return {k: dict(v) for k, v in self.items.items()}


# ---------------------------------------------------------------------------------------------- dev_fix rules
_WRITE_SHELL = re.compile(r"\bsed\s+(?:-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|>>?\s*['\"]?[^\s;&|'\"]+|\btee\b|"
                          r"\bcp\b|\bmv\b|\bdd\b|\bpatch\b|\bgit\s+apply\b|write_text|write_bytes|\.write\(|open\([^)]*['\"][wa]b?\+?['\"]",
                          re.I)
_INLINE_CODE = re.compile(r"\bpython3?\s+(-c\b|-\s*<<|<<)|\bnode\s+(-e\b|-\s*<<|<<)|<<\s*['\"]?\w+", re.I)


def writes_path(command: str, path: str) -> bool:
    """Does a shell command write `path` itself (not by running a generator script with it as an argument)?"""
    c = command or ""
    if not _mentions(c, path):
        return False
    p = norm(path)
    names = [re.escape(x) for x in {p, p.replace("~/", "$HOME/"), os.path.basename(p)} if x]
    tgt = "(?:" + "|".join(names) + ")"
    if re.search(r"\bsed\s+(?:-[a-zA-Z]*i|--in-place)[^;&|]*" + tgt, c) or re.search(r"\bperl\s+-[a-zA-Z]*i[^;&|]*" + tgt, c):
        return True
    if re.search(r">>?\s*['\"]?[^\s;&|'\"]*" + tgt, c) or re.search(r"\btee\s+(-a\s+)?['\"]?[^\s;&|'\"]*" + tgt, c):
        return True
    if re.search(r"\b(cp|mv)\b[^;&|]*\s['\"]?[^\s;&|'\"]*" + tgt + r"['\"]?\s*($|[;&|])", c):
        return True
    if _INLINE_CODE.search(c) and re.search(r"write_text|write_bytes|\.write\(|open\([^)]*['\"][wa]b?\+?['\"]|writeFileSync|"
                                            r"fs\.writeFile", c):
        return True
    return False


def code_writes(content: str, path: str) -> bool:
    """A script's source writes the artifact (an ad-hoc patcher)."""
    if not content or not _mentions(content, path):
        return False
    return bool(re.search(r"write_text|write_bytes|\.write\(|open\([^)]*['\"][wa]b?\+?['\"]|writeFileSync|fs\.writeFile|"
                          r"sed\s+-i|>\s*['\"]?\S", content))


def artifact_edit(tool: str, args: dict, book: ProvenanceBook, patchers: Optional[dict] = None,
                  sources: tuple = ()) -> Optional[tuple[str, str]]:
    """-> (artifact, generator) when the call edits a generated artifact directly; None otherwise.
    `patchers`: {script path -> artifact} scripts this task wrote that write an artifact.
    `sources`: paths that are the contract's own sources (an artifact that IS a source file is editable)."""
    a = args or {}
    srcs = {norm(s) for s in sources if s}
    if tool == "files_write":
        p = _ws(str(a.get("path") or ""))
        e = book.get(p)
        # only a file a SCRIPT made is an artifact: a report the agent wrote itself and sent stays editable
        if e is not None and p not in srcs and book.generator(p):
            return p, book.generator(p)
        return None
    if tool != "shell":
        return None
    cmd = str(a.get("command") or "")
    for p in book.artifacts():
        if p in srcs or not book.generator(p):
            continue
        if writes_path(cmd, p):
            return p, book.generator(p) or "脚本"
    sc = script_of(cmd)
    if sc and patchers:
        for s, art in patchers.items():
            if norm(s) == sc or os.path.basename(norm(s)) == os.path.basename(sc):
                return art, book.generator(art) or "脚本"
    return None


def written_script(tool: str, args: dict) -> Optional[tuple[str, str]]:
    """(path, content) of a script file the call creates (files_write x.py, cat > x.py <<EOF ... EOF)."""
    a = args or {}
    if tool == "files_write":
        p = _ws(str(a.get("path") or ""))
        if SCRIPT_EXT.search(p):
            return p, str(a.get("content") or "")
        return None
    if tool == "shell":
        cmd = str(a.get("command") or "")
        m = re.search(r"\bcat\s+>\s*['\"]?([^\s'\";&|]+\.(?:py|js|mjs|sh))['\"]?\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\2\b", cmd, re.S)
        if m:
            p = m.group(1)
            cds = re.findall(r"\bcd\s+([^\s;&|]+)", cmd[:m.start()])
            if not p.startswith(("/", "~", "$")):
                p = (cds[-1].rstrip("/") + "/" + p) if cds else ("~/" + p)
            return norm(p), m.group(3)
    return None


def adhoc_patch_script(tool: str, args: dict) -> Optional[str]:
    """The path of an ad-hoc patch script (inject*/restyle*/patch*/fix*) created in ~/workspace, else None."""
    w = written_script(tool, args)
    if not w:
        return None
    p = w[0]
    if ADHOC.search(p) and (p.startswith("~/workspace") or p.startswith("/tmp") or "/workspace/" in p):
        return p
    return None


def script_targets(content: str, sources) -> Optional[str]:
    """v3.9.29: the contract source a side script edits (it names the source and writes/replace()s it), else None."""
    for src in sources or ():
        if src and code_writes(content, src):
            return src
        if src and _mentions(content or "", src) and re.search(r"\.replace\(|re\.sub\(|write_text|\.write\(", content or ""):
            return src
    return None


def adhoc_warning(path: str, generator: Optional[str], targets_source: Optional[str] = None) -> str:
    if targets_source:   # v3.9.29: editing the real source is right — just do it directly, and run what you write
        return (f"NOTE: {path} edits the contract source {targets_source}. That is the right file, but edit it directly "
                f"with files_patch (path={targets_source}, old=<exact text>, new=<replacement>) instead of a side script; "
                "if you keep the script, RUN it in your very next step.")
    return (f"WARNING: {path} looks like an ad-hoc patch script. Patching a generated file with a side script loses the "
            "change the next time the generator runs (task-830). Put the change into the source "
            + (f"({generator}) " if generator else "") + "and re-run the generator instead.")


def refuse_text(artifact: str, generator: str) -> str:
    return (f"REFUSED: 这是 {generator} 生成的产物（{artifact}），请修改源头脚本后重新生成。Edit {generator} (the source "
            f"of truth), run it again to regenerate {os.path.basename(artifact)}, then verify the regenerated file.")
