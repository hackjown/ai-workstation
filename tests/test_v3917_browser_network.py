"""v3.9.17 item 4: browser_network summarises CDP Network events without cookie values."""
from vm_agent.browser.network import summarize, text, safe_headers
from tool_router.registry import BY_NAME

EV = [
    {"method": "Network.requestWillBeSent", "params": {"requestId": "1", "type": "XHR", "timestamp": 10.0, "request": {
        "method": "POST", "url": "https://chat.qwen.ai/api/v2/chat/completions",
        "headers": {"Content-Type": "application/json", "bx-ua": "231!secretsig", "Authorization": "Bearer abc"}}}},
    {"method": "Network.requestWillBeSentExtraInfo", "params": {"requestId": "1", "headers": {"cookie": "cna=VAL1; ssxmod_itna=VAL2; token=VAL3"},
        "associatedCookies": [{"cookie": {"name": "cna", "value": "VAL1"}}, {"cookie": {"name": "x5sec", "value": "VAL4"}, "blockedReasons": ["x"]}]}},
    {"method": "Network.responseReceived", "params": {"requestId": "1", "timestamp": 10.25, "response": {
        "status": 200, "mimeType": "application/json", "headers": {"set-cookie": "tfstk=VAL5; Path=/\nisg=VAL6; Path=/"}}}},
    {"method": "Network.requestWillBeSent", "params": {"requestId": "2", "type": "Image", "timestamp": 10.1,
        "request": {"method": "GET", "url": "https://img.alicdn.com/a.png", "headers": {}}}},
    {"method": "Network.loadingFailed", "params": {"requestId": "2", "errorText": "net::ERR_BLOCKED_BY_CLIENT"}},
]


def test_summary_names_not_values():
    s = summarize(EV)
    assert s["count"] == 2
    r = s["requests"][0]
    assert r["method"] == "POST" and r["status"] == 200 and r["ms"] == 250
    assert r["cookies_sent"] == ["cna", "ssxmod_itna", "token"] and r["cookies_set"] == ["tfstk", "isg"]
    blob = str(s) + text(s)
    for v in ("VAL1", "VAL2", "VAL3", "VAL4", "VAL5", "VAL6", "secretsig", "Bearer abc"):
        assert v not in blob, v
    assert r["headers"]["Content-Type"] == "application/json" and r["headers"]["bx-ua"].startswith("<")
    assert s["requests"][1]["failed"].startswith("net::ERR_BLOCKED")


def test_match_filter_and_regex():
    assert summarize(EV, "completions")["count"] == 1
    assert summarize(EV, "/alicdn\\.com/")["count"] == 1


def test_tool_registered_without_screenshot():
    assert BY_NAME["browser_network"].action == "browser.network"
    from aiw_protocols import ActionType
    assert ActionType.BROWSER_NETWORK.value == "browser.network"
    assert safe_headers({"Cookie": "a=1"}) == {}
