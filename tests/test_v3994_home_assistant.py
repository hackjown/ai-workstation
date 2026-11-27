"""v3.9.94 Home Assistant: name resolution (房间+类型 = 一组, 一个名字多个设备 = 问), service plans, the safety confirm
(开锁/撤防/车库门), area / entity filters, errors in plain Chinese, the chat route and tools, the call's tool list."""
import asyncio
import json
import os

os.environ["AIW_NO_AUTOAPP"] = "1"

import httpx

from aiw_api import home_assistant as H
from aiw_api import omni_call as O
from aiw_api.store import Store


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


STATES = [
    {"entity_id": "light.living_main", "state": "on", "attributes": {"friendly_name": "客厅主灯", "brightness": 153}},
    {"entity_id": "light.living_strip", "state": "off", "attributes": {"friendly_name": "客厅灯带"}},
    {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "主卧顶灯"}},
    {"entity_id": "climate.bedroom_ac", "state": "cool", "attributes": {"friendly_name": "主卧空调", "temperature": 26,
                                                                       "current_temperature": 28}},
    {"entity_id": "cover.living_curtain", "state": "closed", "attributes": {"friendly_name": "客厅窗帘", "current_position": 0}},
    {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "车库门", "device_class": "garage"}},
    {"entity_id": "lock.front", "state": "locked", "attributes": {"friendly_name": "大门门锁"}},
    {"entity_id": "sensor.living_temp", "state": "24.5", "attributes": {"friendly_name": "客厅温度", "unit_of_measurement": "°C"}},
    {"entity_id": "scene.home", "state": "scening", "attributes": {"friendly_name": "回家模式"}},
    {"entity_id": "camera.door", "state": "idle", "attributes": {"friendly_name": "门口摄像头"}},
    {"entity_id": "update.core", "state": "off", "attributes": {"friendly_name": "HA 更新"}},
]
AREAS = {"light.living_main": "客厅", "light.living_strip": "客厅", "light.bedroom": "主卧", "climate.bedroom_ac": "主卧",
         "cover.living_curtain": "客厅", "sensor.living_temp": "客厅", "camera.door": "门口"}


class FakeHA:
    def __init__(self, token="tok", areas_ok=True):
        self.calls, self.token, self.areas_ok = [], token, areas_ok

    def __call__(self, req: httpx.Request):
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="unauthorized")
        p = req.url.path
        if p == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if p == "/api/states":
            return httpx.Response(200, json=STATES)
        if p == "/api/template":
            if not self.areas_ok:
                return httpx.Response(400, text="bad template")
            return httpx.Response(200, text="".join(f"{k}|{v}\n" for k, v in AREAS.items()))
        if p.startswith("/api/services/"):
            body = json.loads(req.content or b"{}")
            self.calls.append((p, body))
            st = next(s for s in STATES if s["entity_id"] == body["entity_id"])
            new = dict(st, state="off" if p.endswith("turn_off") else "on" if p.endswith("turn_on") else st["state"])
            return httpx.Response(200, json=[new])
        if p.startswith("/api/camera_proxy/"):
            return httpx.Response(200, content=b"\xff\xd8jpeg")
        return httpx.Response(404)


class Sec:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k):
        return self.d.get(k)

    def put(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


def make(tmp_path, fake=None, **c):
    st = Store(str(tmp_path / "aiw.db"))
    st.kv_set(H.KV, {"url": "192.168.1.10:8123", **c})
    sec = Sec({H.SECRET: "tok"})
    fake = fake or FakeHA()
    return H.HomeAssistant(st, sec, transport=httpx.MockTransport(fake)), fake, st, sec


def test_enabled_and_url(tmp_path):
    ha, _, st, sec = make(tmp_path)
    assert ha.url == "http://192.168.1.10:8123"
    assert H.enabled(st, sec)
    assert not H.enabled(st, Sec())
    st.kv_set(H.KV, {"url": "http://x:8123", "enabled": False})
    assert not H.enabled(st, sec)
    assert H.norm_url("http://ha.local:8123/api/") == "http://ha.local:8123"


def test_entities_filtered_and_areas(tmp_path):
    ha, *_ = make(tmp_path)
    ents = run(ha.entities())
    ids = {e["id"] for e in ents}
    assert "update.core" not in ids and "light.living_main" in ids
    assert next(e for e in ents if e["id"] == "light.bedroom")["area"] == "主卧"
    ha2, *_ = make(tmp_path / "b", areas=["客厅"]) if (tmp_path / "b").mkdir() is None else None
    assert {e["area"] for e in run(ha2.entities())} == {"客厅"}


def test_template_failure_keeps_entities(tmp_path):
    ha, *_ = make(tmp_path, FakeHA(areas_ok=False))
    ents = run(ha.entities())
    assert ents and all(e["area"] == "" for e in ents)


def test_room_and_kind_is_a_group(tmp_path):
    ha, fake, *_ = make(tmp_path)
    out = run(ha.control("客厅的灯", "off"))
    assert "已执行" in out
    assert {b["entity_id"] for _, b in fake.calls} == {"light.living_main", "light.living_strip"}
    assert all(p == "/api/services/light/turn_off" for p, _ in fake.calls)


def test_one_name_exact(tmp_path):
    ha, fake, *_ = make(tmp_path)
    run(ha.control("客厅主灯", "set_brightness", "40"))
    assert fake.calls == [("/api/services/light/turn_on", {"entity_id": "light.living_main", "brightness_pct": 40})]


def test_climate_and_cover(tmp_path):
    ha, fake, *_ = make(tmp_path)
    run(ha.control("主卧空调", "set_temperature", "26度"))
    run(ha.control("客厅窗帘", "open"))
    run(ha.control("主卧空调", "set_mode", "制热"))
    assert fake.calls[0] == ("/api/services/climate/set_temperature", {"entity_id": "climate.bedroom_ac", "temperature": 26.0})
    assert fake.calls[1][0] == "/api/services/cover/open_cover"
    assert fake.calls[2][1]["hvac_mode"] == "heat"


def test_scene_by_name(tmp_path):
    ha, fake, *_ = make(tmp_path)
    run(ha.control("回家模式", "on"))
    assert fake.calls[0][0] == "/api/services/scene/turn_on"


def test_unlock_and_garage_need_confirm(tmp_path):
    ha, fake, *_ = make(tmp_path)
    out = run(ha.control("大门门锁", "unlock"))
    assert "需要先问用户确认" in out and not fake.calls
    out = run(ha.control("车库门", "open"))
    assert "需要先问用户确认" in out and not fake.calls
    run(ha.control("大门门锁", "lock"))          # locking is safe
    assert fake.calls[-1][0] == "/api/services/lock/lock"
    run(ha.control("大门门锁", "unlock", confirm=True))
    assert fake.calls[-1][0] == "/api/services/lock/unlock"


def test_extra_confirm_setting(tmp_path):
    ha, fake, *_ = make(tmp_path, confirm=["light.bedroom"])
    assert "需要先问用户确认" in run(ha.control("主卧顶灯", "on"))
    assert not fake.calls


def test_read_only_and_unknown(tmp_path):
    ha, fake, *_ = make(tmp_path)
    assert "只能查看" in run(ha.control("客厅温度", "off"))
    assert "没找到" in run(ha.control("阳台洗衣机", "on"))
    assert not fake.calls


def test_bad_token_plain_message(tmp_path):
    ha, *_ = make(tmp_path, FakeHA(token="other"))
    out = run(H.run_tool(ha, "ha_devices", {}))
    assert "令牌" in out and "401" in out


def test_list_and_summary(tmp_path):
    ha, *_ = make(tmp_path)
    txt = run(ha.list_text("客厅"))
    assert "客厅主灯" in txt and "60%" in txt and "主卧" not in txt
    s = run(ha.summary())
    assert "客厅：" in s and "主卧空调(空调)" in s and "温度" not in s


def test_camera(tmp_path):
    ha, *_ = make(tmp_path)
    img, e = run(ha.camera("门口"))
    assert img.startswith(b"\xff\xd8") and e["id"] == "camera.door"


def test_wants_home():
    for t in ("关掉客厅的灯", "把空调调到26度", "家里现在多少度", "打开窗帘", "看看门口摄像头", "home assistant 有哪些设备"):
        assert H.wants_home(t), t
    for t in ("帮我写个周报", "灯塔的故事讲一下", "明天天气怎么样"):
        assert not H.wants_home(t), t


def test_validate():
    v = H.validate({"url": "ha.local:8123", "areas": "客厅，主卧", "confirm": "cover"}, dict(H.DEFAULTS))
    assert v["url"] == "http://ha.local:8123" and v["areas"] == ["客厅", "主卧"] and v["confirm"] == ["cover"]
    try:
        H.validate({"url": "http://bad url"}, dict(H.DEFAULTS))
        assert False
    except ValueError:
        pass


def test_call_bridge_gets_ha_tools(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "aiw.db"))
    st.kv_set(H.KV, {"url": "http://ha:8123"})

    class C:
        store, secrets = st, Sec({H.SECRET: "tok"})
    fake = FakeHA()
    orig = H.HomeAssistant.__init__

    def init(self, store, secrets, transport=None):
        orig(self, store, secrets, transport=httpx.MockTransport(fake))
    monkeypatch.setattr(H.HomeAssistant, "__init__", init)
    b = O.ToolBridge(C())
    assert [t["function"]["name"] for t in b.specs()] == ["create_aiw_task", "task_status"]
    line = run(b.prepare())
    assert "ha_control" in line and "主卧空调" in line
    names = [t["function"]["name"] for t in b.specs()]
    assert "ha_control" in names and "ha_devices" in names
    g = O.gemini_tools(b.specs())[0]["functionDeclarations"]
    assert any(d["name"] == "ha_control" and d["parameters"]["properties"]["all"]["type"] == "BOOLEAN" for d in g)
    out = run(b.run("ha_control", json.dumps({"target": "客厅的灯", "action": "on"})))
    assert "已执行" in out and len(fake.calls) == 2


def test_call_bridge_without_ha(tmp_path):
    st = Store(str(tmp_path / "aiw.db"))

    class C:
        store, secrets = st, Sec()
    b = O.ToolBridge(C())
    assert run(b.prepare()) == "" and len(b.specs()) == 2
    assert "没有这个工具" in run(b.run("ha_control", {"target": "灯", "action": "on"}))
