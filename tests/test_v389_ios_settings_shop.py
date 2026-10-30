"""3.8.9 (Build 30): the iOS 设置 has a 主模型思考 switch on the server's models.thinking (the web 深度思考 select), and
the price-watch card is laid out full width on web and iOS."""
import plistlib
import re
from pathlib import Path

from test_chat import FakeChat, stack  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "apps" / "ios"
WEB = ROOT / "apps" / "web"


def test_version_is_3_8_9_build_30_for_app_and_share():
    for p in (IOS / "Info.plist", IOS / "Share" / "Info.plist"):
        d = plistlib.loads(p.read_bytes())
        assert d["CFBundleShortVersionString"] == "3.8.9" and d["CFBundleVersion"] == "30", p


async def test_thinking_patch_changes_only_thinking(stack):
    client, app, scripts, fake = stack
    r = await client.put("/api/settings/models", json={"model": "glm-5v-turbo", "vision_model": "qwen3-vl", "thinking": "off"})
    assert r.status_code == 200
    r = await client.patch("/api/settings/models", json={"thinking": "on"})
    assert r.status_code == 200 and r.json()["thinking"] == "on"
    m = (await client.get("/api/settings/models")).json()
    assert m["thinking"] == "on" and m["model"] == "glm-5v-turbo" and m["vision_model"] == "qwen3-vl"
    assert app.state.store.kv_get("models")["thinking"] == "on"            # the very setting the web form saves
    r = await client.patch("/api/settings/models", json={"thinking": "maybe"})
    assert r.status_code == 400
    r = await client.patch("/api/settings/models", json={"thinking": "off"})
    assert (await client.get("/api/settings/models")).json()["thinking"] == "off"


def test_ios_settings_has_the_thinking_switch():
    src = (IOS / "Sources" / "SettingsView.swift").read_text(encoding="utf-8")
    assert "主模型思考" in src and "Toggle(isOn:" in src
    assert '"GET", "/api/settings/models"' in src and '"PATCH", "/api/settings/models"' in src
    assert '["thinking": on ? "on" : "off"]' in src and 'm.s("thinking") == "on"' in src
    web = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'name="thinking"' in web and "thinking: f.thinking.value" in web   # same key on the web


def test_web_watch_card_is_full_width():
    js = (WEB / "shop.js").read_text(encoding="utf-8")
    css = (WEB / "shop.css").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'href="shop.css"' in html
    row = js[js.index("function shopRow(w)"):js.index("loadWatches = async function")]
    for cls in ("sw-head", "sw-title", "sw-chips", "sw-price", "sw-meta", "sw-foot", "sw-machine", "sw-acts"):
        assert cls in row and "." + cls in css, cls
    assert "shopRunLine(w)" in row and row.index("shopRunLine(w)") < row.index("sw-foot")   # inside the card column
    assert "-webkit-line-clamp:2" in css and "@media (max-width:480px)" in css
    assert "shopTitle(w)" in row and "SHOP_GENERIC" in js and "shopCn(" in js
    assert re.search(r"probe failed: timeout after", js)


def test_ios_watch_card_wraps():
    src = (IOS / "Sources" / "ShopWatchView.swift").read_text(encoding="utf-8")
    assert src.count("WrapLayout(") >= 2 and "lineLimit(2)" in src and "检查电脑：" in src
    assert "isGenericJDTitle" in src and "shopCN(" in src and "confirmationDialog" in src
    assert ".listStyle(.insetGrouped)" in src
