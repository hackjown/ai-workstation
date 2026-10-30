"""Loop side of 3.8.8 vision resilience (task-e63); the classifier, breaker and chain live in llm_router/vision.py.

  * read()  - every read_image / compare / drawing_read / screen-description call goes through here: refused at once
              while the breaker is open (or when no vision model exists), one notification when it first opens.
  * gate()  - a read_image / drawing_read decision while the breaker is open is not run; the 2nd such attempt asks the
              user (_escalate("vision_down")): 修好后继续 (resets the breaker) / 接管 / 停止.
"""
from __future__ import annotations

from typing import Optional

from aiw_protocols import EventType
from llm_router import vision as _vis

TOOLS = _vis.VISION_TOOLS
ASK_AFTER = 2   # vision calls refused while open before the user is asked


def breaker(loop) -> _vis.Breaker:
    br = getattr(loop, "_vision", None)
    if br is None:
        br = loop._vision = _vis.Breaker()
    return br


def is_open(loop) -> bool:
    return breaker(loop).is_open()


async def read(loop, eyes, system: str, user: str, image_b64: str, max_tokens: int, timeout: float,
               count_timeout: bool = True) -> str:
    missing = getattr(loop, "vision_missing", None)
    if missing and eyes is getattr(loop, "llm", None):
        raise _vis.VisionFailed(_vis.VisionError("none", detail=missing))
    br = breaker(loop)
    if br.is_open():
        raise _vis.VisionFailed(br.last or _vis.VisionError("service"))
    try:
        return await _vis.read(eyes, system, user, image_b64, max_tokens, timeout, br, count_timeout)
    except _vis.VisionFailed as e:
        if e.opened and br.trips == 1:   # one notification per task naming the service
            await loop.emit(EventType.ERROR, error=br.notice(), vision_down=True,
                            host=e.err.host, status=e.err.status, model=e.err.model)
        raise


def failed_text(e: BaseException, what: str = "image") -> str:
    """The tool result for a failed read: the classified reason when known, never a bare exception class."""
    if isinstance(e, _vis.VisionFailed):
        return "FAILED: " + str(e)
    return f"FAILED: the vision model could not read the {what} ({type(e).__name__}: {str(e)[:160]})"


async def gate(loop, decision) -> Optional[str]:
    """A vision tool while the breaker is open: not run. Returns the note for the next step, or None when the
    task ended (the user chose 停止)."""
    br = breaker(loop)
    br.blocked += 1
    text = br.blocked_text()
    await loop.emit(EventType.ACTION_RESULT, tool=decision.tool, success=False, error=text[:200],
                    result={"vision_down": True})
    loop.history.append(f"{loop.step}. {decision.tool} NOT RUN -> {text}")
    if br.blocked < ASK_AFTER:
        return text
    n = await loop._escalate("vision_down", br.notice())
    if n is None:
        return None
    br.reset()
    return ("The user says the vision service is fixed: reading images works again (breaker reset). Read the image "
            "once; if it still fails with a 5xx, do not retry or convert it - tell the user.\n" + n)
