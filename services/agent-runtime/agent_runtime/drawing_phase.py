"""Drawing phases, enforced in code: READ -> SPEC -> BUILD -> VERIFY -> DELIVER (3.8.8).

Why (task-41c, 200 steps, stopped by the user): with thinking off, every step began again from the user's correction
「伟少说尺寸对但户型画错了，我先看清原图…」, re-cropped the photo and asked the vision model where the labels are.
Root causes found in the log:
* the model sees only the last 25 history lines (router.user_text) and never its own earlier thoughts, and one
  shell line can carry 6000 chars of pixel dumps: what it measured at step 20 was gone by step 45, so it measured
  again (134 shell + 46 read_image + 9 files_write + 7 drawing_build);
* nothing limited the reading phase: the first spec script was written at step 124;
* the stale cad/spec.json from the earlier G plan (other image, no md5 in it) was taken as "the spec to revise"; the
  model noticed it only at step ~90;
* free-form questions about label positions got a different answer every time (九宫格 / percent / pixels);
* the repeat detector only caught byte-identical calls; near-identical intents went on for 150 steps, and the
  check-in card then quoted a stale JD page.

This module keeps the phase, the read budget, the findings (cad/facts.json, injected into every step's TASK text so
they survive history trimming) and a near-duplicate-intent detector. The loop calls:
  gate(tool, args)      before a tool runs: None, or a refusal text (read budget used up -> write the spec)
  after(tool, args, ok, text)   after it ran: advances the phase, auto-captures facts
  intent(thought, args)  each decision: None | "nudge" | "escalate"
  block()               text appended to the TASK every step
  hidden()              tools hidden from the model in the current phase
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

READ, SPEC, BUILD, VERIFY, DELIVER = "READ", "SPEC", "BUILD", "VERIFY", "DELIVER"
READ_BUDGET = int(os.environ.get("AIW_DRAW_READ_BUDGET", "8"))
VERIFY_ROUNDS = int(os.environ.get("AIW_DRAW_VERIFY_ROUNDS", "3"))
INTENT_WINDOW = int(os.environ.get("AIW_DRAW_INTENT_WINDOW", "4"))
INTENT_SIM = float(os.environ.get("AIW_DRAW_INTENT_SIM", "0.18"))
REFUSALS_MAX = int(os.environ.get("AIW_DRAW_REFUSALS_MAX", "6"))
ROUND_READS = int(os.environ.get("AIW_DRAW_ROUND_READS", "4"))     # targeted reads per failed build round (3.8.9)
QUIET_STEPS = int(os.environ.get("AIW_DRAW_QUIET_STEPS", "10"))    # after the user's 继续: no re-pause this long
FACTS = "cad/facts.json"
STOP_NOTE = "停止分析，立即根据已有 facts 写 spec 并生成。"

_IMG_RE = re.compile(r"Image\.open|PIL|numpy|np\.|\.crop\(|getpixel|cv2|ImageDraw|magick|convert\s|ffmpeg.*-vf", re.I)
_SPEC_RE = re.compile(r"spec[^\s'\"]*\.json", re.I)
_PRODUCE_RE = re.compile(r"drawkit\.py\s+(build|auto|check|fit)|mk\w*\.py|json\.dump", re.I)


def is_read(tool: str, args: dict) -> bool:
    """a step that only inspects the source (crop, zoom, pixel scan, vision question)."""
    a = args or {}
    if tool in ("read_image", "drawing_read"):
        return True
    if tool == "shell":
        cmd = str(a.get("command") or "")
        if _PRODUCE_RE.search(cmd):
            return False
        return bool(_IMG_RE.search(cmd)) or bool(re.search(r"drawkit\.py\s+(zoom|ticks|axes)", cmd))
    if tool == "files_read":
        return bool(re.search(r"\.(png|jpe?g|webp)$", str(a.get("path") or ""), re.I))
    return False


def writes_spec(tool: str, args: dict) -> bool:
    a = args or {}
    if tool == "files_write":
        p = str(a.get("path") or "")
        c = str(a.get("content") or "")
        return bool(_SPEC_RE.search(p)) or (p.endswith(".py") and bool(_SPEC_RE.search(c)))
    if tool == "drawing_build" and a.get("content"):
        return True
    if tool == "drawing_auto":
        return True
    if tool == "shell":
        cmd = str(a.get("command") or "")
        return bool(re.search(r"drawkit\.py\s+auto", cmd)) or (bool(_SPEC_RE.search(cmd)) and bool(re.search(r"mk\w*\.py|json\.dump|>\s*\S*spec", cmd)))
    return False


def _grams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    s = re.sub(r"\d+", "#", s)
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def similarity(a: str, b: str) -> float:
    """overlap of character bigrams (digits folded): two restatements of the same intent share most of the shorter
    one's pairs (41c's 「伟少说尺寸对、户型画错了，我先放大看…」 variants ~0.2-0.4; distinct steps ~0.0)."""
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


class DrawPhase:
    def __init__(self, read_budget: int = READ_BUDGET):
        self.phase = READ
        self.reads = 0
        self.read_budget = read_budget
        self.forced = False          # read budget used up: only spec-writing tools are allowed
        self.builds = 0
        self.verify = 0
        self.facts: dict = {"chains": {}, "labels": [], "walls": {}, "notes": [], "source": {}}
        self.locked: set = set()     # fact keys confirmed (two agreeing reads / the user) and not to be re-read
        self.intents: list = []      # (thought, args signature, progress counter)
        self.progress = 0            # bumps on every new file / spec change / build
        self.nudges = 0
        self.refusals = 0
        self.dirty = False           # facts changed since last saved on the computer
        # 3.8.9 (task-3fc): the guard must know a spec exists and what the build said
        self.spec_written = False
        self.last_ok = None          # None: never built; True / False: the last drawing_build passed / failed
        self.last_errors: list = []
        self.round_reads = 0         # reads since the last build (VERIFY / BUILD: targeted at the errors)
        self.resumes = 0             # times the user said 继续 after a pause
        self.quiet = 0               # decisions left without a re-pause (after 继续)
        self.auto_runs = 0           # deterministic pipeline runs
        self.must_deliver = False    # verify rounds / budget used up: deliver the best draft

    # ---- facts -----------------------------------------------------------------------------------------------
    def note(self, kind: str, key: str, value, confirmed: bool = False) -> str:
        kind = (kind or "notes").strip()
        key = str(key or "").strip()
        lk = f"{kind}:{key}"
        if lk in self.locked and not confirmed:
            return f"{lk} is LOCKED (confirmed earlier): {json.dumps(self._get(kind, key), ensure_ascii=False)[:200]}"
        if kind == "chains":
            self.facts["chains"][key] = value if isinstance(value, dict) else {"values": value}
        elif kind == "labels":
            v = value if isinstance(value, dict) else {"name": key, "at_px": value}
            self.facts["labels"] = [x for x in self.facts["labels"] if x.get("name") != v.get("name") or
                                    x.get("at_px") != v.get("at_px")] + [v]
        elif kind == "walls":
            self.facts["walls"][key] = value
        elif kind == "source":
            self.facts["source"][key] = value
        else:
            self.facts["notes"].append(str(value if not key else f"{key}: {value}")[:300])
            self.facts["notes"] = self.facts["notes"][-30:]
        if confirmed:
            self.locked.add(lk)
        self.dirty = True
        self.progress += 1
        return f"noted {lk}" + (" (locked)" if confirmed else "")

    def _get(self, kind, key):
        v = self.facts.get(kind)
        return v.get(key) if isinstance(v, dict) else v

    def facts_json(self) -> str:
        return json.dumps({**self.facts, "locked": sorted(self.locked), "phase": self.phase}, ensure_ascii=False, indent=1)

    # ---- gating ------------------------------------------------------------------------------------------------
    def hidden(self) -> tuple:
        if self.forced:
            return ("read_image", "drawing_read", "screenshot")
        return ()

    def gate(self, tool: str, args: dict) -> Optional[str]:
        if self.phase == READ and is_read(tool, args):
            if self.reads >= self.read_budget:
                self.forced = True
                self.refusals += 1
                return ("REFUSED (READ budget of %d inspection steps used up): %s Everything you found is in FACTS "
                        "above (cad/facts.json). Now write the spec: drawing_auto for a draft from the image (walls from "
                        "pixels + the chains in facts), or files_write cad/spec.json yourself, then drawing_build. "
                        "Where a fact is missing, write your best value and list it in `uncertain`; do not read the "
                        "image again." % (self.read_budget, STOP_NOTE))
        if self.phase in (BUILD, VERIFY) and is_read(tool, args) and self.last_ok is False:
            if self.round_reads >= ROUND_READS:
                self.refusals += 1
                return ("REFUSED: %d targeted reads after the failed build are used. Fix cad/spec.json from what you have "
                        "and drawing_build again (it auto-repairs chain boxes and rooms), or call drawing_auto for the "
                        "deterministic draft." % ROUND_READS)
        if self.phase in (VERIFY, DELIVER) and tool in ("read_image",) and self.verify >= VERIFY_ROUNDS:
            return ("REFUSED: %d verify rounds done. send_file the PNG and DXF and finish with done, listing what is "
                    "still uncertain." % self.verify)
        return None

    def after(self, tool: str, args: dict, ok: bool, text: str = "") -> None:
        if is_read(tool, args) and self.phase == READ:
            self.reads += 1
        elif is_read(tool, args):
            self.round_reads += 1
        if ok and writes_spec(tool, args):
            self.progress += 1
            self.spec_written = True
            if self.phase == READ:
                self.phase = SPEC
            self.forced = False
        if tool == "drawing_build" and ok:
            self.builds += 1
            self.progress += 1
            self.spec_written = True
            self.round_reads = 0
            self.last_ok = "ALL CHECKS PASS" in (text or "")
            m = re.search(r"ERRORS: (.*)", text or "")
            self.last_errors = [e.strip() for e in m.group(1).split(" | ")][:12] if m else []
            if not self.last_ok and self.builds >= VERIFY_ROUNDS:
                self.must_deliver = True
            if "ALL CHECKS PASS" in (text or "") or "STOP ITERATING" in (text or ""):
                self.phase = VERIFY if self.phase != DELIVER else DELIVER
            elif self.phase in (READ, SPEC):
                self.phase = BUILD
            if self.phase == VERIFY:
                self.verify += 1
                if self.verify >= VERIFY_ROUNDS:
                    self.must_deliver = True
        if tool == "send_file" and ok and self.phase in (VERIFY, BUILD):
            self.phase = DELIVER
        if tool == "files_write" and ok:
            self.progress += 1
        if tool == "drawing_read" and ok and text:
            self._capture_read(args, text)

    def _capture_read(self, args: dict, text: str) -> None:
        """drawing_read's agreed chain values become locked facts, so they are never re-read."""
        m = re.search(r"values (\[[^\]]*\]), total (\d+)", text)
        if m and not text.startswith("CONFLICT"):
            try:
                vals = json.loads(m.group(1))
            except ValueError:
                return
            box = (args or {}).get("box")
            side = str((args or {}).get("side") or "").strip() or _side_hint(text, box)
            key = side or f"chain@{box}"
            self.facts["chains"][key] = {"values": vals, "total": int(m.group(2)), "box_px": box}
            self.locked.add(f"chains:{key}")
            self.dirty = True
            self.progress += 1

    # ---- repeated intent -------------------------------------------------------------------------------------
    def intent(self, thought: str, tool: str, args: dict) -> Optional[str]:
        """None | 'nudge' | 'escalate'. N decisions in a row that restate the same intent (thoughts or arguments
        alike) while nothing new was written or built; also a model that keeps sending refused reads."""
        th = (thought or "")[:400]
        ar = tool + " " + json.dumps(args or {}, ensure_ascii=False)[:300]
        self.intents.append((th, ar, self.progress))
        self.intents = self.intents[-12:]
        if self.quiet > 0:                       # just resumed by the user: a fresh start, no re-pause
            self.quiet -= 1
            return None
        if self.phase in (BUILD, VERIFY) and self.last_ok is False and self.round_reads < ROUND_READS and is_read(tool, args):
            return None                          # reading what the build report flagged is the job right now
        if self.refusals >= REFUSALS_MAX:
            self.refusals = 0
            self.nudges = max(self.nudges, 2) + 1
            return "escalate"
        n = INTENT_WINDOW
        win = self.intents[-n:]
        if len(win) < n or len({p for _, _, p in win}) > 1:   # something new happened inside the window
            return None
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
        st = sum(similarity(win[i][0], win[j][0]) for i, j in pairs) / len(pairs)
        sa = sum(similarity(win[i][1], win[j][1]) for i, j in pairs) / len(pairs)
        if max(st, sa * 0.6) >= INTENT_SIM:
            self.intents = self.intents[-1:]           # a fresh window after the nudge
            self.nudges += 1
            self.forced = self.phase == READ           # stop the reading too
            return "escalate" if self.nudges > 2 else "nudge"
        return None

    # ---- 3.8.9: tell the truth when stuck, and start over properly on 继续 ---------------------------------------
    def stuck(self) -> tuple:
        """(kind, detail) describing where the drawing really is."""
        if self.last_ok is False:
            errs = "；".join(_short_err(e) for e in self.last_errors[:4]) or "见核对报告"
            return ("drawing_verify", f"spec 已生成并出过 {self.builds} 次图，但核对未通过：{errs}。")
        if self.builds and self.last_ok:
            return ("drawing_verify", f"图已生成且核对通过（{self.builds} 次），但模型还在反复看图，没有交付。")
        if self.spec_written:
            return ("drawing_verify", "spec 已写好，但还没生成图纸，模型在反复分析。")
        return ("drawing_loop", "连续多轮都在重复分析同一件事（看图、裁图、问标签位置），没有写 spec 也没有生成图。"
                                "已提醒两次仍在循环。")

    def resume(self) -> None:
        """the user said 继续: counters reset, a fresh budget, no re-pause for QUIET_STEPS decisions."""
        self.resumes += 1
        self.nudges = 0
        self.refusals = 0
        self.intents = []
        self.reads = 0
        self.round_reads = 0
        self.forced = False
        self.quiet = QUIET_STEPS
        self.must_deliver = False

    # ---- context ----------------------------------------------------------------------------------------------
    def block(self) -> str:
        left = max(0, self.read_budget - self.reads)
        head = f"DRAWING PHASE: {self.phase}"
        if self.phase == READ:
            head += (f" (inspection steps left: {left} of {self.read_budget}; then you MUST write cad/spec.json - "
                     "save every finding with drawing_note as you go, it is shown here every step)")
        elif self.phase == SPEC:
            head += " (write/finish cad/spec.json, then drawing_build; do not go back to reading)"
        elif self.phase == BUILD:
            head += (" (fix the spec from the build report and drawing_build again; up to %d targeted reads of what "
                     "the report flags; drawing_build re-locates wrong chain boxes and regenerates bad rooms itself)"
                     % ROUND_READS)
        elif self.phase == VERIFY:
            head += f" (verify round {self.verify}/{VERIFY_ROUNDS}: one pointed overlay check, then deliver)"
        elif self.phase == DELIVER:
            head += " (send_file PNG + DXF, then done)"
        if self.forced:
            head += "\n" + STOP_NOTE + " (reading tools are switched off until the spec is written)"
        f = self.facts
        if any(f.get(k) for k in ("chains", "labels", "walls", "notes", "source")):
            body = json.dumps({k: v for k, v in f.items() if v}, ensure_ascii=False)
            if len(body) > 3500:
                body = body[:3500] + " …(full list in cad/facts.json)"
            lk = (" LOCKED (do not re-read): " + ", ".join(sorted(self.locked))) if self.locked else ""
            head += f"\nFACTS so far (cad/facts.json):{lk}\n{body}"
        return head


def _short_err(e: str) -> str:
    """one build error in a few Chinese words for the stuck card."""
    m = re.match(r"(\w+) chain segment (\d+): written (\d+) but the drawing measures ~(\d+)", e)
    side = {"top": "上", "bottom": "下", "left": "左", "right": "右"}
    if m:
        w, me = float(m.group(3)), float(m.group(4))
        return f"{side.get(m.group(1), m.group(1))}链第{m.group(2)}段写 {int(w)} 量得 ~{int(me)}（×{me / w:.2f}）" if w else e[:60]
    if "not horizontal/vertical" in e:
        return e.split(":")[0].replace("room ", "房间") + " 不是直角多边形"
    if "overlap by" in e:
        return e.replace("rooms ", "房间 ").replace(" and ", "/").split(" overlap")[0] + " 重叠"
    if "not on any wall" in e:
        return "有门窗不在墙上"
    if "crossed by" in e:
        return "有文字被线压住"
    return e[:60]


def _side_hint(text: str, box) -> str:
    return ""


# ---- stale spec --------------------------------------------------------------------------------------------------

def stale_probe(spec: str, image: Optional[str]) -> str:
    """shell: print the spec's recorded source (image, md5, task) and the md5 of the current image."""
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"   # noqa: E731
    py = ("import json,sys,hashlib\n"
          "try: s=json.load(open(sys.argv[1],encoding='utf-8'))\n"
          "except Exception as e: print(json.dumps({'missing':True})); raise SystemExit\n"
          "src=s.get('source') or {}\n"
          "out={'image':src.get('image'),'md5':src.get('md5'),'task':src.get('task'),'title':s.get('title')}\n"
          "img=sys.argv[2] if len(sys.argv)>2 and sys.argv[2] else src.get('image')\n"
          "try: out['cur_md5']=hashlib.md5(open(img,'rb').read()).hexdigest(); out['cur_image']=img\n"
          "except Exception: pass\n"
          "print(json.dumps(out,ensure_ascii=False))\n")
    return (f'cd "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}" && python3 -c {q(py)} {q(spec)} {q(image or "")}')


def stale_verdict(info: dict, task_root: str) -> Optional[str]:
    """why the spec on disk must not be revised, or None. A spec with no md5 / task stamp predates this guard and
    is treated as stale when the current upload is known (it cannot be proven to belong to it)."""
    if not info or info.get("missing"):
        return None
    why = []
    if info.get("task") and str(info["task"]) != str(task_root):
        why.append(f"it was written by task {info['task']}, not this task ({task_root})")
    if info.get("md5") and info.get("cur_md5") and info["md5"] != info["cur_md5"]:
        why.append(f"it was read from another image (md5 {info['md5'][:8]} ≠ {info['cur_md5'][:8]})")
    if not info.get("md5") and not info.get("task") and info.get("cur_md5"):
        why.append("it records no source md5 / task, so it cannot be shown to belong to this image")
    return "; ".join(why) or None


def archive_command(spec: str) -> str:
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"   # noqa: E731
    return ('cd ~/workspace 2>/dev/null; [ -z "$VM_AGENT_FILES_ROOT" ] || cd "$VM_AGENT_FILES_ROOT"; '
            f'd="$(dirname {q(spec)})/_stale/$(date +%Y%m%d-%H%M%S)" && '
            f'mkdir -p "$d" && for f in {q(spec)} "$(dirname {q(spec)})"/*.png "$(dirname {q(spec)})"/*.dxf '
            f'"$(dirname {q(spec)})"/report.json; do [ -e "$f" ] && mv -- "$f" "$d/"; done; echo "$d"')


def auto_command(image: str, out: str, chains_from: str, labels: Optional[str], task: str, debug: str,
                 from_spec: Optional[str] = None) -> str:
    q = lambda s: "'" + str(s).replace("'", "'\\''") + "'"   # noqa: E731
    cmd = (f'cd "${{VM_AGENT_FILES_ROOT:-$HOME/workspace}}" && python3 .aiw/cad-drawing/drawkit.py auto {q(image)} '
           f'--chains {q(chains_from)} --out {q(out)} --debug {q(debug)} --task {q(task)} --install-ocr')
    if labels:
        cmd += f" --labels {q(labels)}"
    if from_spec:
        cmd += f" --from-spec {q(from_spec)}"
    return cmd


def parse_auto(stdout: str) -> Optional[dict]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("DRAWKIT_AUTO "):
            try:
                return json.loads(line[len("DRAWKIT_AUTO "):])
            except ValueError:
                return None
    return None


LABEL_Q = ("图上画了红色像素网格，网格旁的数字是原图像素坐标。只做一件事：找出图中每一个房间名称文字（如 卧室、主卧、客厅、"
           "餐厅、厨房、卫生间、阳台、衣帽间、书房、玄关、过道、管井），给出文字中心的原图像素坐标。没有文字的房间不要编。"
           "只输出 JSON：{\"labels\": [{\"name\": \"卧室\", \"x\": 123, \"y\": 456}, ...]}")


def labels_from(text: str) -> list:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in d.get("labels") or []:
        try:
            out.append({"name": str(it["name"]).strip(), "x": float(it["x"]), "y": float(it["y"]), "by": "vlm"})
        except (KeyError, TypeError, ValueError):
            continue
    return [o for o in out if o["name"]]
