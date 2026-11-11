import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))
from aiw_api import integrations as I


class _M:
    def __init__(self, recs): self.recs = recs
    def all(self): return self.recs


def _patch(monkeypatch, recs):
    import aiw_api.oauth as O
    monkeypatch.setattr(O, "manager", lambda store, secrets: _M(recs), raising=False)


def test_no_adobe_words_no_note(monkeypatch):
    _patch(monkeypatch, [])
    assert I.adobe_route_note({}, "帮我查天气") == ""


def test_tools_present(monkeypatch):
    _patch(monkeypatch, [])
    t = {"mcp_adobe_remove_background": ({}, None), "mcp_adobe_x": ({}, None), "shell": ({}, None)}
    assert "2 个" in I.adobe_route_note(t, "用Photoshop把背景去掉")


def test_not_connected(monkeypatch):
    _patch(monkeypatch, [])
    assert "还没连接" in I.adobe_route_note({}, "用Photoshop把黑色指针移动一下")


def test_needs_reauth(monkeypatch):
    _patch(monkeypatch, [{"id": "c1", "connector": "adobe", "status": "needs_reauth"}])
    assert "重新授权" in I.adobe_route_note({}, "Firefly 生成一张图")


def test_connected_but_list_failed(monkeypatch):
    _patch(monkeypatch, [{"id": "c1", "connector": "adobe", "status": "ok"}])
    assert "工具列表没取到" in I.adobe_route_note({}, "用 PS 修一下")


def test_planner_rule_present():
    p = os.path.join(os.path.dirname(__file__), "..", "services", "agent-runtime", "agent_runtime", "planner.py")
    assert "ADOBE FIRST" in open(p, encoding="utf-8").read()
