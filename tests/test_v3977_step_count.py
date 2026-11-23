"""v3.9.77: 「正在做 40 步」 was only the shown list (newest 40), not a limit: the reply carries the whole count."""
import os

R = os.path.join(os.path.dirname(__file__), "..")


def _r(p):
    return open(os.path.join(R, p), encoding="utf-8").read()


def test_api_counts_every_step():
    s = _r("services/api/aiw_api/bots.py")
    assert s.count('"step_count": step_count') == 2 and "step_count += 1" in s
    assert '"max_turns": 1000' in s and '"timeout_min": 24 * 60' in s


def test_apps_show_whole_count():
    assert "d.step_count" in _r("apps/web/bots.js")
    sw = _r("apps/ios/Sources/EngineView.swift")
    assert "func stepCount" in sw and "stepCount(d, steps.count)" in sw
