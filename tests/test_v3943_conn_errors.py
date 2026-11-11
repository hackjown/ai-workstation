import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))
from aiw_api import integrations as I


class _Slow:
    async def list_tools(self):
        await asyncio.sleep(5)
        return [{"name": "x"}]


class _Boom:
    async def list_tools(self):
        raise RuntimeError("proxy refused")


def test_timeout_without_cache_raises_and_text():
    I._MCP_CACHE.pop("k1", None)
    try:
        asyncio.run(I._mcp_tools(_Slow(), "k1", 0.05))
        assert False
    except asyncio.TimeoutError as e:
        assert "超时" in I._err_text(e, 45)


def test_stale_list_used_when_refresh_fails():
    I._MCP_CACHE["k2"] = (time.time() - 3600, [{"name": "remove_background"}])
    assert asyncio.run(I._mcp_tools(_Boom(), "k2", 1)) == [{"name": "remove_background"}]


def test_too_old_list_not_used():
    I._MCP_CACHE["k3"] = (time.time() - 90000, [{"name": "old"}])
    try:
        asyncio.run(I._mcp_tools(_Boom(), "k3", 1)); assert False
    except RuntimeError:
        pass


def test_route_note_carries_error(monkeypatch):
    import aiw_api.oauth as O
    class M:
        def all(self): return [{"id": "adobe", "connector": "adobe", "status": "ok"}]
    monkeypatch.setattr(O, "manager", lambda a, b: M(), raising=False)
    I.CONN_ERRORS["adobe"] = "超时（45 秒内没拿到工具列表，多半是代理慢或没走代理）"
    try:
        assert "45 秒" in I.adobe_route_note({}, "用Photoshop改一下")
    finally:
        I.CONN_ERRORS.pop("adobe", None)
