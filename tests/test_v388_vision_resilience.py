"""3.8.8 vision resilience (task-e63: deveco-qwen3-vl on the UniAPI gateway answered HTTP 500 on every call; the
tool result said only "(RuntimeError)", so the agent re-encoded the image for 25+ steps at ~24 s a read)."""
import asyncio
import io
import os

import httpx
import pytest
from PIL import Image

from agent_runtime import escalate as es
from aiw_protocols import EventType, TaskStatus
from llm_router import LLMRouter
from llm_router import vision as vis
from test_phase5_agent_loop import env, make_loop  # noqa: F401
from test_phase7_api import stack  # noqa: F401

GW = "http://hackjown.3322.org:8787/v1/chat/completions"


def _status_error(code: int, url: str = GW) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    res = httpx.Response(code, request=req, text="boom")
    return httpx.HTTPStatusError(f"Server error '{code}' for url '{url}'", request=req, response=res)


class Eye:
    """A provider for the vision chain: raises `fail` (an exception) or answers `text`."""

    def __init__(self, model, fail=None, text="一张红色的图"):
        self.model, self.fail, self.text, self.calls, self.users = model, fail, text, 0, []

    async def describe(self, system, user, image_b64, max_tokens=900):
        self.calls += 1
        self.users.append(user)
        if self.fail is not None:
            raise self.fail
        return self.text


# ------------------------------------------------------------------ classify
def test_classify_service_auth_image():
    e = vis.classify(_status_error(500), "deveco-qwen3-vl")
    assert (e.kind, e.status, e.host, e.model) == ("service", 500, "hackjown.3322.org", "deveco-qwen3-vl")
    assert "HTTP 500 @ hackjown.3322.org，模型 deveco-qwen3-vl" in e.tool_text()
    assert "不是图片问题，不要重试或转换图片" in e.tool_text()
    # the router's wrapped text from task-e63 (no cause kept) still gives status and host
    msg = ("no provider could describe the image: Server error '500 Internal Server Error' for url "
           "'http://hackjown.3322.org:8787/v1/chat/completions' For more information check: https://x")
    e = vis.classify(RuntimeError(msg))
    assert (e.kind, e.status, e.host) == ("service", 500, "hackjown.3322.org")
    assert vis.classify(_status_error(429)).kind == "service"
    assert vis.classify(asyncio.TimeoutError()).kind == "service"
    assert vis.classify(httpx.ConnectError("refused", request=httpx.Request("POST", GW))).kind == "service"
    assert vis.classify(_status_error(401)).kind == "auth"
    assert vis.classify(_status_error(404)).kind == "auth"
    assert vis.classify(_status_error(413)).kind == "image"
    assert vis.classify(ValueError("cannot identify image")).kind == "other"


def test_router_keeps_the_cause():
    async def go():
        r = LLMRouter([Eye("m1", fail=_status_error(502))])
        try:
            await r.describe("s", "u", "b64")
        except RuntimeError as e:
            return vis.classify(e)
    e = asyncio.run(go())
    assert e.status == 502 and e.host == "hackjown.3322.org"


# ------------------------------------------------------------------ breaker
def test_breaker_opens_after_two_service_failures_and_cools_down():
    now = [100.0]
    br = vis.Breaker(cooldown=300, clock=lambda: now[0])
    svc = vis.VisionError("service", 500, "gw", "m")
    assert br.record(svc) is False and not br.is_open()
    assert br.record(svc) is True and br.is_open() and br.trips == 1
    assert "HTTP 500 @ gw" in br.blocked_text() and "不要重试或转换图片" in br.blocked_text()
    now[0] += 299
    assert br.is_open()
    now[0] += 2
    assert not br.is_open()
    assert br.record(svc) is True and br.is_open()        # half-open: one more failure after the cooldown reopens
    br.reset()
    assert not br.is_open() and br.fails == 0
    br.record(svc); br.record(vis.VisionError("image", 413))   # a non-service error breaks the run
    br.record(svc)
    assert not br.is_open()
    br.record(svc)
    assert br.is_open()
    br.reset()
    assert not br.is_open() and br.fails == 0


def test_cooldown_env(monkeypatch):
    monkeypatch.setenv("AIW_VISION_COOLDOWN_S", "42")
    assert vis.Breaker().cooldown == 42


def test_read_falls_through_the_chain_and_feeds_the_breaker():
    br = vis.Breaker(cooldown=300)
    bad, good = Eye("deveco-qwen3-vl", fail=_status_error(500)), Eye("glm-4.5v")
    out = asyncio.run(vis.read(LLMRouter([bad, good]), "s", "u", "b64", 100, 5, br))
    assert out == "一张红色的图" and bad.calls == 1 and good.calls == 1 and br.fails == 0

    eyes = LLMRouter([Eye("deveco-qwen3-vl", fail=_status_error(500)), Eye("glm-4.5v", fail=_status_error(503))])
    with pytest.raises(vis.VisionFailed) as ei:
        asyncio.run(vis.read(eyes, "s", "u", "b64", 100, 5, br))
    assert "HTTP 500 @ hackjown.3322.org，模型 deveco-qwen3-vl" in str(ei.value) and not ei.value.opened
    with pytest.raises(vis.VisionFailed) as ei:
        asyncio.run(vis.read(eyes, "s", "u", "b64", 100, 5, br))
    assert ei.value.opened and br.is_open()


def test_describe_makes_at_most_two_http_attempts(monkeypatch):
    from llm_router.providers import openai as oa
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(500, text="upstream error")
    real = httpx.AsyncClient
    monkeypatch.setattr(oa.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.delenv("AIW_VISION_ATTEMPTS", raising=False)
    p = oa.OpenAIProvider(model="deveco-qwen3-vl", api_key="k", base_url="http://hackjown.3322.org:8787/v1")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(p.describe("s", "u", "iVBOR"))
    assert len(hits) == 2


# ------------------------------------------------------------------ chain
def test_vision_chain_order_exclusions_and_cap():
    m = {"model": "deveco-glm-5.3", "vision_model": "deveco-qwen3-vl", "vision_fallback": "glm-4.5v",
         "geometry_model": "qwen-vl-max"}
    probes = {"deveco-glm-5.3": {"vision": False, "notes": ["看图回答：i cannot see images"]},
              "gpt-4o": {"vision": True}, "glm-4.5v": {"vision": True}}
    assert vis.vision_chain(m, probes, env={}) == ["deveco-qwen3-vl", "glm-4.5v", "qwen-vl-max"]
    # a fallback probed text-only is left out; probed vision-capable models fill in
    probes["glm-4.5v"] = {"vision": False, "notes": ["看图回答：no image"]}
    assert vis.vision_chain(m, probes, env={}) == ["deveco-qwen3-vl", "qwen-vl-max", "gpt-4o"]
    # a probe that hit the 500 says nothing about the model: it stays in
    probes["deveco-qwen3-vl"] = {"vision": False, "notes": ["看图：HTTP 500: upstream error"]}
    assert vis.vision_chain(m, probes, env={})[0] == "deveco-qwen3-vl"
    assert vis.vision_chain({"model": "a"}, probes, env={}) == []
    assert vis.vision_chain({"model": "a", "vision_model": "a"}, probes, env={}) == []


def test_no_vision_model_is_said_clearly():
    probes = {"deveco-glm-5.3": {"vision": False, "notes": ["看图回答：sorry"]}}
    t = vis.no_vision_text({"model": "deveco-glm-5.3"}, probes, env={})
    assert t and "没有配置视觉模型" in t and "deveco-glm-5.3" in t
    assert vis.no_vision_text({"model": "gpt-4o"}, {"gpt-4o": {"vision": True}}, env={}) is None
    assert vis.no_vision_text({"model": "x", "vision_model": "v"}, {}, env={}) is None
    probes["v"] = {"vision": False, "notes": ["看图回答：?"]}
    assert "没有可用的视觉模型" in vis.no_vision_text({"model": "deveco-glm-5.3", "vision_model": "v"}, probes, env={})


def test_sessions_build_the_chain(tmp_path):
    from aiw_api.store import Store

    class S:
        _custom_llm = None
        secrets = {"openai_api_key": "k"}
        store = Store(str(tmp_path / "a.db"))

        def model_settings(self):
            return {"model": "deveco-glm-5.3", "vision_model": "deveco-qwen3-vl", "vision_fallback": "glm-4.5v",
                    "base_url": "http://gw/v1"}
    from aiw_api.sessions import SessionManager
    s = S()
    for name in ("vision_chain", "vision_missing", "_vision_llm"):
        setattr(s, name, getattr(SessionManager, name).__get__(s))
    r = s._vision_llm()
    assert [p.model for p in r.providers] == ["deveco-qwen3-vl", "glm-4.5v"]
    assert s.vision_missing() is None


# ------------------------------------------------------------------ escalate
def test_vision_down_options_and_card():
    assert es.options("vision_down") == [es.OPT_FIXED, es.OPT_TAKEOVER, es.OPT_STOP]
    assert es.options("fail") == es.OPTIONS
    assert es.choice(es.OPT_FIXED) == "retry"
    card = es.stuck_card("vision_down", "读图服务不可用：HTTP 500 @ hackjown.3322.org（模型 deveco-qwen3-vl），连续 2 次失败。")
    assert "hackjown.3322.org" in card["reason"] and card["actions"][0] == es.OPT_FIXED
    assert "修好后继续" in es.question("vision_down", "x")


# ------------------------------------------------------------------ the loop
def _png(tmp):
    (tmp / "uploads").mkdir(exist_ok=True)
    buf = io.BytesIO(); Image.new("RGB", (40, 30), (220, 30, 30)).save(buf, "PNG")
    (tmp / "uploads/plan.png").write_bytes(buf.getvalue())


async def test_loop_fails_fast_then_asks_and_resets(env, monkeypatch):  # noqa: F811
    backend, comp, tmp = env
    _png(tmp)
    monkeypatch.setenv("AIW_VISION_COOLDOWN_S", "300")
    events, asked = [], []
    rd = [("read_image", {"path": "uploads/plan.png", "question": f"第 {i} 次：图上有什么？"}) for i in range(5)]
    loop, llm = make_loop(comp, rd + [("done", {"summary": "ok"})], tmp, events=events)
    eye = Eye("deveco-qwen3-vl", fail=_status_error(500))
    loop.vision_llm = LLMRouter([eye])

    async def ask(q, opts, extra=None):
        asked.append((q, opts))
        eye.fail = None            # the user fixed the service
        return es.OPT_FIXED if len(asked) == 1 else es.OPT_STOP
    loop.ask = ask
    try:
        t = await asyncio.wait_for(loop.run(), 30)
    except asyncio.TimeoutError:
        raise AssertionError("\n".join(loop.history) + f"\nasked={asked} calls={eye.calls}")
    assert t.status == TaskStatus.SUCCEEDED, t.result
    hist = "\n".join(loop.history)
    assert "HTTP 500 @ hackjown.3322.org，模型 deveco-qwen3-vl" in hist and "不是图片问题" in hist
    assert "(RuntimeError)" not in hist
    assert hist.count("NOT RUN") == 2                      # steps 3-4 never reached the gateway
    reads = [u for u in eye.users if "图上有什么" in u]
    assert len(reads) == 3                                 # 2 failed reads + 1 after 修好后继续 (one HTTP call each)
    assert len(asked) == 1 and asked[0][1] == es.options("vision_down")
    downs = [e for e in events if e.type == EventType.ERROR and e.data.get("vision_down")]
    assert len(downs) == 1 and "hackjown.3322.org" in downs[0].data["error"]
    assert "image uploads/plan.png reads" in hist


async def test_open_breaker_skips_look_and_parallel_reads(env):  # noqa: F811
    from agent_runtime import vision_guard as vg
    backend, comp, tmp = env
    _png(tmp)
    loop, _ = make_loop(comp, [], tmp)
    eye = Eye("v", fail=_status_error(503))
    loop.vision_llm = LLMRouter([eye])
    br = vg.breaker(loop)
    br.record(vis.VisionError("service", 503, "gw", "v")); br.record(vis.VisionError("service", 503, "gw", "v"))
    assert await loop._look("abc", (10, 10)) is None and eye.calls == 0
    out = await loop._read_image({"paths": ["uploads/plan.png"], "again": True})
    assert out.startswith("FAILED: 读图服务不可用（HTTP 503 @ gw") and eye.calls == 0


async def test_no_vision_model_message(env):  # noqa: F811
    backend, comp, tmp = env
    _png(tmp)
    loop, _ = make_loop(comp, [], tmp)
    loop.vision_missing = "没有配置视觉模型：主模型 deveco-glm-5.3 是纯文本模型，看不了图。"
    out = await loop._read_image_one({"path": "uploads/plan.png"})
    assert "没有配置视觉模型" in out and "不要转换图片" in out


async def test_drawing_read_reports_the_service_not_a_conflict(env):  # noqa: F811
    backend, comp, tmp = env
    _png(tmp)
    loop, _ = make_loop(comp, [], tmp)
    loop.vision_llm = LLMRouter([Eye("deveco-qwen3-vl", fail=_status_error(500))])
    out = await loop._drawing_read({"path": "uploads/plan.png", "box": [0, 0, 40, 10], "kind": "chain"})
    assert out.startswith("FAILED: 读图服务不可用") and "CONFLICT" not in out


# ------------------------------------------------------------------ API + lessons
async def test_vision_test_endpoint(stack, monkeypatch):  # noqa: F811
    client, *_ = stack
    await client.put("/api/settings/models", json={"model": "deveco-glm-5.3", "vision_model": "deveco-qwen3-vl",
                                                   "vision_fallback": "glm-4.5v", "base_url": "http://hackjown.3322.org:8787/v1"})
    assert (await client.get("/api/settings/models")).json()["vision_fallback"] == "glm-4.5v"
    import aiw_api.probe as pr

    async def fake(c, url, key, model):
        if model == "deveco-qwen3-vl":
            return {"model": model, "vision": False, "ms": 5, "note": "看图：HTTP 500: x", "error": "RuntimeError: HTTP 500: x"}
        return {"model": model, "vision": True, "ms": 5, "note": ""}
    monkeypatch.setattr(pr, "probe_vision", fake)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    r = (await client.post("/api/vision/test")).json()
    assert r["chain"] == ["deveco-qwen3-vl", "glm-4.5v"] and r["ok"] and r["working"] == "glm-4.5v"
    bad = r["results"][0]
    assert bad["kind"] == "service" and bad["status"] == 500 and "hackjown.3322.org" in bad["message"]


def test_lesson_v2_seeded_once(tmp_path):
    from aiw_api.lessons import SEED_MARK_V2, Lessons
    from aiw_api.store import Store
    st = Store(str(tmp_path / "a.db"))
    les = Lessons(st)
    rows = [r for r in les.items(scope="tool") if r["key"] == "read_image"]
    assert len(rows) == 1 and "不要转换图片" in rows[0]["fix"] and st.kv_get(SEED_MARK_V2)
    les.delete(rows[0]["id"])
    Lessons(st)
    assert not [r for r in Lessons(st).items(scope="tool") if r["key"] == "read_image"]
    assert len([r for r in les.items(scope="skill") if r["key"] == "cad-drawing"]) == 11   # v1 (6) + 3 drawloop + 2 drawloop v2 seeds
