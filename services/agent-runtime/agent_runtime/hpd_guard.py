"""3.9.11 (task-a7b46fccd8): keep a house-plan-designer task moving to delivery.

That run traced the picture in 15 s (from-image ok: 20 walls, 12 rooms), then spent 30+ minutes and 110 steps on the
walls: `make` 11 times chasing clean=true (an open balcony and an AC ledge can never close, so it never got clean),
read_image of overlay.png 12 times asking the vision model where walls were missing, and seven generator scripts
(gen / gen2 / gen3 / fill_walls / fill2 / grid / grid2) that rebuilt every wall from cad-spec.json instead of patching
the draft. It never reached furniture or delivery.

The guard watches the steps and adds one note to the model's next turn when the job drifts:
  * the 3rd (and every later) check `make`: stop fixing walls, furnish (SKILL.md step 5) and deliver with --final;
  * read_image beyond 4 since the draft: no more overlay questions to the vision model;
  * a script that rebuilds walls from cad-spec.json: patch draft/spec.json in place instead;
  * 45 steps after the draft without --final: deliver now.
Pure logic (tests/test_v3911_hpd_guard.py); loop.py feeds it.
"""
from __future__ import annotations

import re

MAKE_ROUNDS = 3
LOOKS = 4
FINISH_AFTER = 45

_MAKE = re.compile(r"aiw_hpd\.py\s+make\b")
_DRAFT = re.compile(r"aiw_hpd\.py\s+(from-image|from-cad|imgcheck)\b")
_REBUILD = re.compile(r"cad-spec\.json|walls_traced")


class HpdGuard:
    def __init__(self):
        self.drafted = False
        self.since_draft = 0
        self.makes = 0
        self.looks = 0
        self.final = False
        self.rebuild_warned = False
        self.finish_warned = 0

    def feed(self, tool: str, args: dict, ok: bool = True, out: str = "") -> str | None:
        args = args or {}
        cmd = str(args.get("command") or "")
        if self.drafted:
            self.since_draft += 1
        if tool == "shell" and _DRAFT.search(cmd) and ok and ('"ok": true' in out or not out):
            self.drafted = True
            return None
        if tool == "shell" and _MAKE.search(cmd):
            if "--final" in cmd:
                self.final = True
                return None
            self.makes += 1
            if self.makes >= MAKE_ROUNDS:
                return (f"[designer] This is check round {self.makes} (SKILL.md allows at most {MAKE_ROUNDS}). Stop fixing "
                        "walls now: rooms that are open by nature (阳台/露台/空调位/设备平台/可拓展空间, open living-dining) "
                        "never close and freeWallEnds there are fine. Go to step 5 (furniture + autoElec) on the SAME "
                        "spec, then `aiw_hpd.py make ... --final hpd/<name>` and deliver; list what is approximate in done.")
            return None
        if tool == "read_image" and self.drafted:
            self.looks += 1
            if self.looks > LOOKS:
                return ("[designer] Enough looking: do not ask the vision model about overlay.png / plan2d.png again "
                        "(it cannot give wall positions). Use HPD_RESULT's report and must_fix, patch the few walls it "
                        "names, or move on to furniture and --final delivery.")
            return None
        if tool in ("files_write", "shell") and self.drafted and not self.rebuild_warned:
            body = str(args.get("content") or "") if tool == "files_write" else cmd
            path = str(args.get("path") or "")
            if _REBUILD.search(body) and ("walls" in body) and (path.endswith(".py") or "python3 -" in body or tool == "files_write"):
                self.rebuild_warned = True
                return ("[designer] Do not rebuild the walls from cad-spec.json in a new generator script. from-image already "
                        "wrote them into spec.json: load THAT file, change only the walls / openings / rooms that need it "
                        "(a few lines), save it, make once. Rebuilding loses doors, windows and closures that were right.")
        if self.drafted and not self.final and self.since_draft >= FINISH_AFTER and self.finish_warned < 2 \
                and self.since_draft >= FINISH_AFTER + 15 * self.finish_warned:
            self.finish_warned += 1
            return (f"[designer] {self.since_draft} steps since the draft and nothing delivered yet. Furnish what is there "
                    "and run `make ... --final hpd/<name>` now; the user can move walls and furniture in the designer.")
        return None
