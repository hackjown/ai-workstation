"""v3.9.17 item 9: replay of the user's export task-833 ("ssh 到 docker 服务器查哪些容器占用高").

Original run: 14 steps, 23 min, 9 confirmation cards, stopped by the user, no report. The cause was known at step
8-9. The replay feeds the recorded decisions (thought / tool / args) and the recorded tool results back into the real
AgentLoop + ToolExecutor + PolicyEngine. No command runs anywhere: the computer is a fake that answers from the
recording. Passwords were removed from the fixture (tests/fixtures/task833_replay.json).
"""
import base64
import io
import json
from pathlib import Path

from PIL import Image

from agent_runtime import AgentLoop, LoopConfig
from aiw_protocols import EventType, Task, TaskStatus
from computer_runtime.providers.base import ActionResult
from llm_router import LLMRouter, ScriptedProvider
from llm_router.router import LLMDecision
from tool_router import ToolExecutor

FIX = json.loads((Path(__file__).parent / "fixtures" / "task833_replay.json").read_text())


def _png() -> str:
    b = io.BytesIO()
    Image.new("RGB", (64, 40), "white").save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


class FakeComputer:
    """Answers each action from the recording (by the command / path the model gave)."""

    def __init__(self):
        self.by_key = {}
        for s in FIX["steps"]:
            self.by_key[self._key(s["tool"], s["args"])] = s
        self.ran = []

    @staticmethod
    def _key(tool, args):
        return (args.get("command") or args.get("path") or json.dumps(args, sort_keys=True, ensure_ascii=False))[:400]

    async def execute(self, action, payload=None, observe=False, timeout=30.0):
        payload = payload or {}
        if action == "computer.screenshot":
            return ActionResult(True, {"width": 64, "height": 40}, screenshot=_png(), width=64, height=40, action=action)
        self.ran.append(action)
        s = self.by_key.get(self._key("", payload))
        res = dict((s or {}).get("result") or {"exit_code": 0, "stdout": "", "stderr": ""})
        return ActionResult(True, res, action=action, duration_ms=5)

    async def observe(self):
        return None


class Replay(ScriptedProvider):
    def __init__(self, compliant: bool, report: str = ""):
        super().__init__([])
        self.steps = list(FIX["steps"])
        self.compliant = compliant
        self.report = report
        self.notes = []

    async def decide(self, req):
        self.requests.append(req)
        self.notes.append(req.note or "")
        if self.compliant and req.note and "DELIVER NOW" in req.note:
            return LLMDecision("信息已足够，直接出报告。", "done", {"summary": "诊断报告：主因是 clipboard-online 容器内 uvicorn "
                               "产生约 5700 个僵尸进程，clouddrive2 约 50% CPU，4 核 4G 跑 28 个容器。建议 compose 加 init: true。"})
        if not self.steps:
            return LLMDecision("script exhausted", "fail", {"reason": "script exhausted"})
        s = self.steps.pop(0)
        return LLMDecision(s["thought"], s["tool"], s["args"])

    async def complete(self, system, user, max_tokens=800):
        if not self.report:
            raise RuntimeError("no model for the report")
        return self.report


async def _go(compliant: bool, report: str = ""):
    confirms, events = [], []

    async def confirm(tool, payload, decision):
        confirms.append((tool, decision.reason))
        return True

    async def sink(e):
        events.append(e)

    llm = Replay(compliant, report)
    comp = FakeComputer()
    loop = AgentLoop(Task(prompt=FIX["prompt"]), "s833", comp, LLMRouter([llm]), ToolExecutor(comp, confirm=confirm),
                     emit=sink, config=LoopConfig(max_steps=0, timeout_s=0, make_plan=False))
    task = await loop.run()
    return task, loop, llm, comp, confirms, events


def test_fixture_has_no_password_and_is_the_original_run():
    raw = (Path(__file__).parent / "fixtures" / "task833_replay.json").read_text()
    assert "密码***" in FIX["prompt"]
    assert FIX["original"]["steps"] == 14 and FIX["original"]["confirms"] == 9
    from agent_runtime import redact
    assert redact.extract(FIX["prompt"])[1] == []          # nothing left to extract
    assert "password" not in raw.lower() or "***" in raw


async def test_compliant_model_delivers_by_step_9_with_at_most_2_confirms():
    task, loop, llm, comp, confirms, events = await _go(compliant=True)
    assert task.status == TaskStatus.SUCCEEDED, task.result
    assert loop.step <= 9, loop.step                        # was 14 steps + stopped by the user
    assert len(confirms) <= 2, confirms                     # was 9 cards
    assert "僵尸" in task.result
    assert any("DELIVER NOW" in n for n in llm.notes)
    # the facts board was built from the model's own words and rode along in the task text
    assert loop.findings.live()
    assert "CONFIRMED FINDINGS" in llm.requests[-1].task
    assert any(e.type == EventType.FINDING for e in events)


async def test_stubborn_model_gets_the_report_written_for_it():
    """The model keeps 'collecting one more group of data': the loop delivers anyway, from the findings board."""
    task, loop, llm, comp, confirms, events = await _go(compliant=False)
    assert task.status == TaskStatus.SUCCEEDED, task.result
    assert loop.step <= 10, loop.step
    assert len(confirms) <= 2
    assert "僵尸" in task.result and "clipboard" in task.result.lower()
    assert "深挖" in task.result                             # first version, then the user decides about more
    shells = [a for a in comp.ran if a == "shell.exec"]
    assert len(shells) <= 7                                  # the original ran 12 shell steps


async def test_stubborn_model_report_written_by_a_tool_free_model_call():
    task, loop, *_ = await _go(compliant=False, report="诊断报告（模型撰写）：clipboard-online 的 uvicorn 产生约 5700 个僵尸进程。" * 2)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.result.startswith("诊断报告（模型撰写）")


async def test_timing_fields_on_every_action_result():
    task, loop, llm, comp, confirms, events = await _go(compliant=True)
    res = [e for e in events if e.type == EventType.ACTION_RESULT]
    assert res and all("tool_ms" in e.data and "wait_ms" in e.data for e in res)
    dec = [e for e in events if e.type == EventType.DECISION and e.data.get("llm_ms") is not None]
    assert dec
