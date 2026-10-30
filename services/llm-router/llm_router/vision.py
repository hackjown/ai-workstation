"""Vision ("eyes") resilience, 3.8.8 (task-e63).

In task-e63 the vision model deveco-qwen3-vl on the UniAPI gateway answered HTTP 500 on every call. The loop turned
that into "FAILED: the vision model could not read the image (RuntimeError)" - no status, no host - so the main
model concluded the IMAGE was the problem and spent 25+ steps re-encoding it (PNG -> JPEG, smaller, cropped). Each
failed read also took ~24 s: OpenAIProvider._post retried 3x (2 s / 8 s waits) and _retry_once repeated all of it.

Here:
  * classify(exc)  -> service (5xx / 429 / timeout / connection) | auth (401/403/404, bad model id) | image (400/413/
                      415/422 about the picture) | other; with status, host and model kept for the message.
  * Breaker        -> per task: after BREAK_AFTER (2) consecutive service failures the service is "down" for
                      AIW_VISION_COOLDOWN_S (300) seconds; nothing calls the vision model while it is open.
  * read()         -> one vision call through the router's providers in order (the fallback chain), each provider
                      at most AIW_VISION_ATTEMPTS (2) HTTP attempts, failures classified, breaker fed.
  * vision_chain() -> which models read images: vision_model -> vision_fallback -> geometry_model -> models the
                      probe saw read the red "47" image; at most 3; models probed text-only are left out.
Pure helpers (tests/test_v388_vision_resilience.py); the loop wiring is in agent_runtime/loop.py.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

BREAK_AFTER = 2
MAX_CHAIN = 3
VISION_TOOLS = ("read_image", "drawing_read")   # compare is read_image compare=true


def cooldown_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIW_VISION_COOLDOWN_S", "300") or 300))
    except ValueError:
        return 300.0


def attempts() -> int:
    """HTTP attempts per vision call and model (the text decide path keeps AIW_LLM_RETRIES)."""
    try:
        return max(1, min(3, int(os.environ.get("AIW_VISION_ATTEMPTS", "2") or 2)))
    except ValueError:
        return 2


@dataclass
class VisionError:
    kind: str                 # service | auth | image | other
    status: Optional[int] = None
    host: str = ""
    model: str = ""
    detail: str = ""

    def where(self) -> str:
        what = f"HTTP {self.status}" if self.status else (self.detail.split(":")[0][:40] or "连接失败")
        bits = [what + (f" @ {self.host}" if self.host else "")]
        if self.model:
            bits.append(f"模型 {self.model}")
        return "，".join(bits)

    def tool_text(self) -> str:
        """What the tool result says, so the main model knows whose fault it is."""
        if self.kind == "none":   # no vision model at all
            return self.detail + " 不是图片问题，不要转换图片。"
        if self.kind == "service":
            return (f"读图服务不可用（{self.where()}），不是图片问题，不要重试或转换图片。"
                    "继续做不需要看图的部分，或告诉用户检查视觉模型服务。")
        if self.kind == "auth":
            return (f"读图模型配置有误（{self.where()}：密钥、权限或模型名不对），不是图片问题，不要转换图片；"
                    "请用户在 设置 › 模型 检查视觉模型。")
        if self.kind == "image":
            return f"视觉模型拒绝了这张图片（{self.where()}：{self.detail[:160]}），可以缩小或换格式后再试一次。"
        return f"视觉模型读图失败（{self.where()}：{self.detail[:160]}）"


_STATUS_RES = (re.compile(r"HTTP (\d{3})"), re.compile(r"error '(\d{3})"), re.compile(r"status(?:_code)?[ =:]+(\d{3})"),
               re.compile(r"\b(\d{3}) (?:Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|"
                          r"Too Many Requests|Unauthorized|Forbidden|Not Found|Bad Request)"))
_URL_RE = re.compile(r"https?://[^\s'\"]+")


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify(exc: BaseException, model: str = "") -> VisionError:
    """Sort a vision failure: service (5xx/429/timeout/connection), auth/config (401/403/404, unknown model),
    image (400/413/415/422 about the picture) or other."""
    status, host, text = None, "", ""
    timeout = conn = False
    try:
        import httpx
    except ImportError:  # pragma: no cover
        httpx = None
    for e in _chain(exc):
        text = text or f"{type(e).__name__}: {e}"
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            timeout = True
        if httpx is not None:
            if isinstance(e, httpx.HTTPStatusError):
                status = status or e.response.status_code
                host = host or (e.request.url.host or "")
            elif isinstance(e, httpx.TimeoutException):
                timeout = True
            elif isinstance(e, httpx.TransportError):
                conn = True
            if not host and isinstance(e, httpx.RequestError):
                try:
                    host = e.request.url.host or ""
                except RuntimeError:
                    pass
        if isinstance(e, (ConnectionError, OSError)) and not isinstance(e, TimeoutError):
            conn = True
    full = " ".join(str(e) for e in _chain(exc))
    if status is None:
        for rx in _STATUS_RES:
            m = rx.search(full)
            if m:
                status = int(m.group(1))
                break
    if not host:
        m = _URL_RE.search(full)
        if m:
            host = urlparse(m.group(0)).hostname or ""
    if not timeout and re.search(r"(?i)timed? ?out|超时|TimeoutError", full):
        timeout = True
    if not conn and re.search(r"(?i)connect(ion)? (error|refused|reset)|ConnectError|RemoteProtocolError|"
                              r"name or service not known|network is unreachable", full):
        conn = True
    detail = re.sub(r"\s+", " ", full).strip()[:300]
    if status is not None:
        if status >= 500 or status in (408, 425, 429):
            kind = "service"
        elif status in (401, 402, 403, 404):
            kind = "auth"
        elif status == 400 and re.search(r"(?i)model.{0,40}(not|unknown|invalid|无效|不存在|不支持)|"
                                         r"(no such|unknown|invalid) model|api.?key", full):
            kind = "auth"
        elif status in (400, 413, 415, 422):
            kind = "image"
        else:
            kind = "other"
    elif timeout or conn:
        kind = "service"
    else:
        kind = "other"
    return VisionError(kind, status, host, model, detail if not timeout or status else "timeout: " + detail)


class Breaker:
    """Per-task circuit breaker for the vision service."""

    def __init__(self, threshold: int = BREAK_AFTER, cooldown: Optional[float] = None, clock=time.monotonic):
        self.threshold, self._cooldown, self.clock = threshold, cooldown, clock
        self.fails = 0              # consecutive service failures
        self.open_until = 0.0
        self.last: Optional[VisionError] = None
        self.blocked = 0            # vision tool calls refused while open (2 -> ask the user)
        self.trips = 0              # times it opened this task (the first one notifies)

    @property
    def cooldown(self) -> float:
        return cooldown_s() if self._cooldown is None else self._cooldown

    def is_open(self) -> bool:
        return self.open_until > self.clock()

    def remaining(self) -> int:
        return max(0, int(self.open_until - self.clock()))

    def record(self, err: VisionError) -> bool:
        """Feed a failure; True when this one opened the breaker."""
        self.last = err
        if err.kind != "service":
            self.fails = 0
            return False
        self.fails += 1
        if self.fails >= self.threshold and not self.is_open():
            self.open_until = self.clock() + self.cooldown
            self.trips += 1
            return True
        return False

    def ok(self) -> None:
        self.fails, self.open_until, self.blocked = 0, 0.0, 0

    reset = ok

    def blocked_text(self) -> str:
        e = self.last or VisionError("service")
        return (f"读图服务不可用（{e.where()}），不是图片问题，不要重试或转换图片。读图已暂停约 {self.remaining()} 秒"
                "（熔断）：先做不需要看图的部分；需要看图就告诉用户检查视觉模型服务。")

    def notice(self) -> str:
        e = self.last or VisionError("service")
        return (f"读图服务不可用：{e.where()}，连续 {self.fails} 次失败。不是图片问题；请检查视觉模型服务"
                f"（网关 {e.host or '?'}），{int(self.cooldown)} 秒内不再调用它。")


class VisionFailed(RuntimeError):
    def __init__(self, err: VisionError, opened: bool = False):
        super().__init__(err.tool_text())
        self.err, self.opened = err, opened


def _model_of(p) -> str:
    return str(getattr(p, "model", "") or getattr(p, "name", "") or "")


async def read(eyes, system: str, user: str, image_b64: str, max_tokens: int, timeout: float,
               breaker: Optional[Breaker] = None, count_timeout: bool = True) -> str:
    """One vision call through eyes' providers in order. Raises VisionFailed with the classified error of the
    first provider (the configured vision model) unless a later one answered. Every provider gets at most
    attempts() HTTP attempts (OpenAIProvider.describe reads it); no second round on top."""
    providers = list(getattr(eyes, "providers", None) or [eyes])
    errs: list[VisionError] = []
    for p in providers:
        try:
            text = await asyncio.wait_for(p.describe(system, user, image_b64, max_tokens), timeout)
            if breaker:
                breaker.ok()
            return text
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            errs.append(classify(e, _model_of(p)))
    err = errs[0] if errs else VisionError("other", detail="no provider")
    # all-service failures feed the breaker; a timeout of the short screen description (_look) does not
    service = all(x.kind == "service" for x in errs)
    if not count_timeout and service and all(x.status is None and x.detail.startswith("timeout") for x in errs):
        raise VisionFailed(err, False)
    if not service:
        err = next((x for x in errs if x.kind != "service"), err)
    opened = breaker.record(err) if breaker else False
    raise VisionFailed(err, opened)


# ------------------------------------------------------------------ chain (settings + probes)
def probed_service_error(p: dict) -> bool:
    """The probe's vision check failed on the service, not on the model (5xx, timeout, connection)."""
    notes = " ".join(str(n) for n in (p.get("notes") or []) if "看图" in str(n))
    return bool(re.search(r"(?i)HTTP (5\d\d|429|408)|timed? ?out|超时|connect|reset", notes))


def text_only(p: Optional[dict]) -> bool:
    """Probed and could not read the image for a reason that is the model's (an answer without 47/red, or a 4xx):
    such a model never goes into the vision chain. A probe that hit a 5xx/timeout says nothing about the model."""
    return bool(p) and p.get("vision") is False and not probed_service_error(p)


def vision_chain(m: dict, probes: Optional[dict] = None, env: Optional[dict] = None) -> list[str]:
    """vision_model -> vision_fallback -> geometry_model -> models probed vision-capable; at most 3, none probed
    text-only, no duplicates. Empty when vision_model is unset or is the main model (the main model looks itself)."""
    env = os.environ if env is None else env
    probes = probes or {}
    if not m.get("vision_model") or m.get("vision_model") == m.get("model"):
        return []
    names = [m.get("vision_model"), m.get("vision_fallback") or env.get("AIW_VISION_FALLBACK"),
             m.get("geometry_model") or env.get("AIW_GEOMETRY_MODEL")]
    names += [k for k, v in probes.items() if isinstance(v, dict) and v.get("vision") is True]
    out: list[str] = []
    for n in names:
        n = (n or "").strip()
        if n and n not in out and not text_only(probes.get(n)):
            out.append(n)
    return out[:MAX_CHAIN]


def no_vision_text(m: dict, probes: Optional[dict] = None, env: Optional[dict] = None) -> Optional[str]:
    """When nothing can read images: the vision chain is empty and the main model (which then looks itself) was
    probed text-only. None when something can, or when it is not known."""
    env = os.environ if env is None else env
    probes = probes or {}
    if vision_chain(m, probes, env):
        return None
    main = m.get("model") or env.get("OPENAI_MODEL") or ""
    if not main or not text_only(probes.get(main)):
        return None
    vm = m.get("vision_model")
    if vm and vm != main:
        return (f"没有可用的视觉模型：视觉模型 {vm} 实测不能看图，也没有能看图的备用模型，主模型 {main} 是纯文本模型。"
                "请在 设置 › 模型 选一个实测能“看图”的视觉模型。")
    return (f"没有配置视觉模型：主模型 {main} 是纯文本模型，看不了图。请在 设置 › 模型 选一个实测能“看图”的视觉模型。")
