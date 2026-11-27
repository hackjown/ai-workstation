"""v3.9.94 Home Assistant: 小秘聊天和语音/视频通话直接控制 Home Assistant 里的设备（灯、开关、空调、窗帘、
风扇、电视音箱、扫地机、门锁、场景、脚本……），查看传感器状态，聊天里还能看摄像头截图。

Everything goes through Home Assistant's REST API from the API host (never the VM):
  GET  {url}/api/                               ping ({"message": "API running."})
  GET  {url}/api/states                         every entity
  POST {url}/api/template                       area of each entity (area_name), one line per entity
  POST {url}/api/services/{domain}/{service}    the action; the answer is the list of states it changed
  GET  {url}/api/camera_proxy/{entity_id}       a camera's current picture (jpeg)
  Authorization: Bearer <长期访问令牌>  (HA 个人资料 › 安全 › 长期访问令牌)

Settings  kv home_assistant = {enabled, url, areas: [..], entities: [..], verify_ssl, confirm: [..]}
          secret ha_token
  areas / entities empty = every entity in a supported domain; otherwise only those areas / entity ids.
Safety    开锁 (lock.unlock / open)、撤防 (alarm_control_panel.alarm_disarm)、打开车库门/大门 (cover with device_class
          garage / gate / door) and anything in kv confirm need confirm=true: the tool first answers 「需要先问用户」,
          the model asks, and only the user's yes in this conversation lets it call again with confirm=true.
          homeassistant.* / system domains (restart, reload, shell_command …) are never callable.
API       GET/PUT /api/home-assistant, POST /api/home-assistant/test, GET /api/home-assistant/entities
Env       AIW_HA=0 turns the whole thing off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("aiw.home_assistant")

KV = "home_assistant"
SECRET = "ha_token"
DEFAULTS = {"enabled": True, "url": "", "areas": [], "entities": [], "verify_ssl": True, "confirm": []}
CACHE_S = float(os.environ.get("AIW_HA_CACHE_S", "20"))
TIMEOUT = float(os.environ.get("AIW_HA_TIMEOUT", "8"))

CONTROL = ("light", "switch", "fan", "cover", "climate", "media_player", "scene", "script", "lock", "vacuum",
           "humidifier", "water_heater", "input_boolean", "button", "input_button", "automation",
           "alarm_control_panel", "valve", "siren", "remote", "select", "input_select", "number", "input_number")
READ_ONLY = ("sensor", "binary_sensor", "camera", "weather", "person", "device_tracker", "event")
DOMAINS = CONTROL + READ_ONLY

DOMAIN_CN = {"light": "灯", "switch": "开关", "fan": "风扇", "cover": "窗帘/门", "climate": "空调", "media_player": "影音",
             "scene": "场景", "script": "脚本", "lock": "门锁", "vacuum": "扫地机", "humidifier": "加湿器",
             "water_heater": "热水器", "input_boolean": "开关", "button": "按钮", "input_button": "按钮",
             "automation": "自动化", "alarm_control_panel": "安防", "valve": "阀门", "siren": "警报器", "remote": "遥控",
             "select": "选项", "input_select": "选项", "number": "数值", "input_number": "数值", "sensor": "传感器",
             "binary_sensor": "传感器", "camera": "摄像头", "weather": "天气", "person": "人员",
             "device_tracker": "位置", "event": "事件"}

# words in a request -> the domains they mean (longest first when matching)
WORDS = [
    ("灯带", ("light",)), ("台灯", ("light",)), ("吊灯", ("light",)), ("灯", ("light",)), ("light", ("light",)),
    ("插座", ("switch",)), ("开关", ("switch", "input_boolean")), ("switch", ("switch",)),
    ("空调", ("climate",)), ("暖气", ("climate",)), ("地暖", ("climate",)), ("温控", ("climate",)), ("新风", ("fan", "climate")),
    ("窗帘", ("cover",)), ("卷帘", ("cover",)), ("百叶", ("cover",)), ("车库门", ("cover",)), ("晾衣架", ("cover",)),
    ("风扇", ("fan",)), ("电扇", ("fan",)), ("排气扇", ("fan",)),
    ("电视", ("media_player",)), ("音箱", ("media_player",)), ("音响", ("media_player",)), ("投影", ("media_player",)),
    ("扫地机", ("vacuum",)), ("扫地机器人", ("vacuum",)), ("拖地机", ("vacuum",)),
    ("门锁", ("lock",)), ("锁", ("lock",)),
    ("场景", ("scene", "script")), ("模式", ("scene", "script", "select", "input_select")),
    ("加湿器", ("humidifier",)), ("除湿", ("humidifier",)), ("热水器", ("water_heater",)),
    ("摄像头", ("camera",)), ("监控", ("camera",)),
    ("温度", ("sensor", "climate")), ("湿度", ("sensor",)), ("门窗", ("binary_sensor",)), ("人体", ("binary_sensor",)),
    ("安防", ("alarm_control_panel",)), ("布防", ("alarm_control_panel",)), ("撤防", ("alarm_control_panel",)),
]
ALL_RE = re.compile(r"全部|所有|都|全屋|一起|\ball\b", re.I)

# a chat line that is about the home: kept in the chat (tools), never sent to a computer
WANTS_RE = re.compile(
    r"home ?assistant|智能家居|米家|全屋|"
    r"(开|关|打开|关掉|关闭|调|调到|调成|调高|调低|把|帮我|设成|设为|暂停|播放|启动|停止|拉开|拉上|锁上|解锁|看看|查查|查一下)"
    r"[^。！？\n]{0,12}(灯|空调|暖气|地暖|窗帘|卷帘|插座|风扇|电扇|电视|音箱|音响|投影|加湿器|扫地机|门锁|热水器|新风|晾衣架|车库门|场景|摄像头|监控)|"
    r"(家里|屋里|室内|卧室|客厅|书房|厨房|卫生间|浴室)[^。！？\n]{0,8}(温度|湿度|几度|多少度|灯|门|窗|空调|摄像头|有人)",
    re.I)


class HAError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def wants_home(text: str) -> bool:
    return bool(WANTS_RE.search(text or ""))


def cfg(store) -> dict:
    c = dict(DEFAULTS)
    c.update(store.kv_get(KV, {}) or {})
    return c


def norm_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if u and not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    if u.endswith("/api"):
        u = u[:-4]
    return u


def enabled(store, secrets) -> bool:
    if os.environ.get("AIW_HA", "1") == "0" or store is None or secrets is None:
        return False
    c = cfg(store)
    try:
        return bool(c.get("enabled") and norm_url(c.get("url")) and secrets.get(SECRET))
    except Exception:  # noqa: BLE001
        return False


def validate(body: dict, old: dict) -> dict:
    new = dict(old)
    if "enabled" in body:
        new["enabled"] = bool(body["enabled"])
    if "url" in body:
        u = norm_url(str(body.get("url") or ""))
        if u and not re.match(r"^https?://[^\s/]+(:\d+)?(/[^\s]*)?$", u):
            raise ValueError("地址格式不对，例如 http://192.168.1.10:8123")
        new["url"] = u
    for k in ("areas", "entities", "confirm"):
        if k in body:
            v = body[k]
            if isinstance(v, str):
                v = [x for x in re.split(r"[,，、;\s]+", v) if x]
            new[k] = [str(x).strip()[:80] for x in (v or []) if str(x).strip()][:200]
    if "verify_ssl" in body:
        new["verify_ssl"] = bool(body["verify_ssl"])
    return new


def _key(s: str) -> str:
    return re.sub(r"[\s_\-·.的]+", "", (s or "").lower())


def _pct(v: Any) -> Optional[int]:
    try:
        return round(float(v) * 100 / 255)
    except (TypeError, ValueError):
        return None


STATE_CN = {"on": "开", "off": "关", "open": "开着", "closed": "关着", "opening": "正在打开", "closing": "正在关",
            "locked": "已上锁", "unlocked": "未上锁", "playing": "播放中", "paused": "暂停", "idle": "空闲",
            "cleaning": "清扫中", "docked": "在充电座", "returning": "回充中", "heat": "制热", "cool": "制冷",
            "auto": "自动", "dry": "除湿", "fan_only": "送风", "heat_cool": "冷暖", "unavailable": "离线",
            "unknown": "未知", "home": "在家", "not_home": "不在家", "armed_away": "离家布防", "armed_home": "在家布防",
            "disarmed": "已撤防", "triggered": "报警中", "standby": "待机"}


def describe(e: dict) -> str:
    """One short Chinese line for an entity: 客厅主灯（灯，开 60%）."""
    a, d, st = e.get("attrs") or {}, e["domain"], e.get("state")
    bits = [STATE_CN.get(st, st) if st is not None else ""]
    if d == "light" and st == "on" and a.get("brightness") is not None:
        bits.append(f"{_pct(a['brightness'])}%")
    if d == "climate":
        if a.get("temperature") is not None:
            bits.append(f"设定 {a['temperature']}°")
        if a.get("current_temperature") is not None:
            bits.append(f"室温 {a['current_temperature']}°")
    if d == "cover" and a.get("current_position") is not None:
        bits.append(f"开度 {a['current_position']}%")
    if d == "media_player" and a.get("media_title"):
        bits.append(str(a["media_title"])[:30])
    if d in ("sensor", "number", "input_number") and a.get("unit_of_measurement"):
        bits[0] = f"{st}{a['unit_of_measurement']}"
    head = e["name"] + (f"［{e['area']}］" if e.get("area") else "")
    return f"{head}（{DOMAIN_CN.get(d, d)}，{'，'.join(b for b in bits if b)}）"


class HomeAssistant:
    """One per request / call. `transport` is for tests (httpx.MockTransport)."""

    def __init__(self, store, secrets, transport=None):
        self.store, self.secrets, self.transport = store, secrets, transport
        self.c = cfg(store)
        self.url = norm_url(self.c.get("url"))
        self._cache: Optional[tuple[float, list]] = None

    # ------------------------------------------------------------------ http
    def _client(self, timeout: float = TIMEOUT) -> httpx.AsyncClient:
        tok = self.secrets.get(SECRET) if self.secrets is not None else None
        if not self.url or not tok:
            raise HAError("还没接入 Home Assistant（设置 › 智能家居 填地址和长期访问令牌）")
        kw: dict = {"base_url": self.url, "timeout": timeout, "verify": bool(self.c.get("verify_ssl", True)),
                    "headers": {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                    "trust_env": False}
        if self.transport is not None:
            kw["transport"] = self.transport
        return httpx.AsyncClient(**kw)

    async def _req(self, method: str, path: str, body: Any = None, timeout: float = TIMEOUT, raw: bool = False):
        try:
            async with self._client(timeout) as cl:
                r = await cl.request(method, path, json=body) if body is not None else await cl.request(method, path)
        except HAError:
            raise
        except httpx.TimeoutException:
            raise HAError(f"Home Assistant 没响应（{self.url}，超时 {timeout:.0f} 秒）")
        except httpx.HTTPError as e:
            raise HAError(f"连不上 Home Assistant（{self.url}）：{type(e).__name__}")
        if r.status_code == 401:
            raise HAError("Home Assistant 拒绝了令牌（401）：令牌过期或填错了，到 设置 › 智能家居 重新填")
        if r.status_code == 404:
            raise HAError(f"Home Assistant 没有这个接口或实体（404 {path}）")
        if r.status_code >= 400:
            raise HAError(f"Home Assistant 报错 {r.status_code}：{r.text[:160]}")
        if raw:
            return r
        try:
            return r.json()
        except ValueError:
            return r.text

    async def ping(self) -> dict:
        t0 = time.time()
        res = await self._req("GET", "/api/")
        return {"ok": True, "message": (res or {}).get("message") if isinstance(res, dict) else str(res)[:80],
                "ms": int((time.time() - t0) * 1000)}

    async def _areas(self) -> dict:
        tpl = "{% for s in states %}{% set a = area_name(s.entity_id) %}{% if a %}{{ s.entity_id }}|{{ a }}\n{% endif %}{% endfor %}"
        try:
            txt = await self._req("POST", "/api/template", {"template": tpl})
        except HAError as e:
            log.info("ha areas: %s", e.message)
            return {}
        out = {}
        for line in str(txt or "").splitlines():
            if "|" in line:
                k, v = line.split("|", 1)
                out[k.strip()] = v.strip()
        return out

    async def entities(self, force: bool = False) -> list[dict]:
        if self._cache and not force and time.time() - self._cache[0] < CACHE_S:
            return self._cache[1]
        states, areas = await asyncio.gather(self._req("GET", "/api/states"), self._areas())
        want_a = {_key(a) for a in self.c.get("areas") or []}
        want_e = set(self.c.get("entities") or [])
        out = []
        for s in states if isinstance(states, list) else []:
            eid = str(s.get("entity_id") or "")
            dom = eid.split(".", 1)[0]
            if dom not in DOMAINS:
                continue
            a = s.get("attributes") or {}
            area = areas.get(eid, "")
            if (want_a or want_e) and eid not in want_e and _key(area) not in want_a:
                continue
            if a.get("hidden"):
                continue
            out.append({"id": eid, "domain": dom, "name": str(a.get("friendly_name") or eid.split(".", 1)[-1]),
                        "area": area, "state": s.get("state"), "attrs": a})
        self._cache = (time.time(), out)
        return out

    # ------------------------------------------------------------------ resolve
    async def find(self, query: str, domain: str = "") -> dict:
        """-> {matches: [...], how: 'id'|'name'|'group'|'none', area, domains}. A request naming an area and a kind
        (客厅的灯) is a group: every match is meant. A request naming one device that several entities fit is
        ambiguous (the caller asks)."""
        ents = await self.entities()
        q = (query or "").strip()
        if q in {e["id"] for e in ents}:
            return {"matches": [e for e in ents if e["id"] == q], "how": "id"}
        qk = _key(q)
        areas = sorted({e["area"] for e in ents if e["area"]}, key=len, reverse=True)
        area = next((a for a in areas if _key(a) and _key(a) in qk), "")
        rest = qk.replace(_key(area), "") if area else qk
        doms: tuple = (domain,) if domain else ()
        word = ""
        if not doms:
            for w, ds in sorted(WORDS, key=lambda x: -len(x[0])):
                if w in rest:
                    doms, word = ds, w
                    break
        pool = [e for e in ents if (not area or e["area"] == area) and (not doms or e["domain"] in doms)]
        rest_name = rest.replace(_key(word), "") if word else rest
        rest_name = re.sub(r"(全部|所有|都|全屋|一起|打开|关闭|关掉|开|关)", "", rest_name)
        # exact / contained friendly names first
        exact = [e for e in pool if _key(e["name"]) == qk or _key(e["name"]) == rest]
        if exact:
            return {"matches": exact, "how": "name", "area": area}
        named = [e for e in pool if rest_name and (rest_name in _key(e["name"]) or _key(e["name"]) in qk)]
        if named:
            return {"matches": named, "how": "name", "area": area}
        if not rest_name and (area or doms):
            return {"matches": pool, "how": "group", "area": area, "domains": list(doms)}
        # loose: every character of the leftover name appears in the entity's name (主卧顶灯 ~ 主卧室顶灯)
        loose = [e for e in (pool or ents) if rest_name and all(ch in _key(e["name"] + e["area"]) for ch in rest_name)]
        return {"matches": loose, "how": "name" if loose else "none", "area": area}

    # ------------------------------------------------------------------ act
    def needs_confirm(self, e: dict, service: str) -> str:
        d, a = e["domain"], e.get("attrs") or {}
        if d == "lock" and service in ("unlock", "open"):
            return f"给「{e['name']}」开锁"
        if d == "alarm_control_panel" and service.startswith("alarm_disarm"):
            return f"把「{e['name']}」撤防"
        if d == "cover" and a.get("device_class") in ("garage", "gate", "door") and service in ("open_cover", "toggle",
                                                                                                   "set_cover_position"):
            return f"打开「{e['name']}」"
        if e["id"] in (self.c.get("confirm") or []) or d in (self.c.get("confirm") or []):
            return f"操作「{e['name']}」"
        return ""

    def plan(self, e: dict, action: str, value: Any) -> tuple[str, dict]:
        """(service, data) for one entity, or raise HAError with what this kind of device can do."""
        d, act = e["domain"], (action or "").strip().lower()
        v = value
        on, off = act in ("on", "turn_on", "open_or_on", "开", "打开"), act in ("off", "turn_off", "关", "关闭")
        if d in ("scene",):
            return "turn_on", {}
        if d == "script":
            return ("turn_off", {}) if off else ("turn_on", {})
        if d in ("button", "input_button"):
            return "press", {}
        if d == "cover":
            if act in ("open", "on", "turn_on", "打开"):
                return "open_cover", {}
            if act in ("close", "off", "turn_off", "关闭"):
                return "close_cover", {}
            if act == "stop":
                return "stop_cover", {}
            if act in ("set_position", "position"):
                return "set_cover_position", {"position": _clamp(v, 0, 100)}
            if act == "toggle":
                return "toggle", {}
        if d == "lock":
            if act in ("lock", "on", "close", "上锁"):
                return "lock", {}
            if act in ("unlock", "off", "open", "开锁"):
                return "unlock", {}
        if d == "vacuum":
            if act in ("start", "on", "turn_on", "clean"):
                return "start", {}
            if act in ("stop", "off", "turn_off"):
                return "stop", {}
            if act == "pause":
                return "pause", {}
            if act in ("return_home", "return", "dock", "off"):
                return "return_to_base", {}
        if d == "media_player":
            m = {"play": "media_play", "pause": "media_pause", "stop": "media_stop", "next": "media_next_track",
                 "previous": "media_previous_track", "mute": "volume_mute"}
            if act in m:
                return m[act], ({"is_volume_muted": True} if act == "mute" else {})
            if act in ("set_volume", "volume"):
                return "volume_set", {"volume_level": round(_clamp(v, 0, 100) / 100, 2)}
        if d == "climate":
            if act in ("set_temperature", "temperature"):
                return "set_temperature", {"temperature": float(_num(v))}
            if act in ("set_mode", "mode"):
                mode = {"制冷": "cool", "制热": "heat", "自动": "auto", "除湿": "dry", "送风": "fan_only", "关": "off"}.get(str(v), str(v))
                return "set_hvac_mode", {"hvac_mode": mode}
        if d == "light":
            if act in ("set_brightness", "brightness"):
                p = _clamp(v, 0, 100)
                return ("turn_off", {}) if p == 0 else ("turn_on", {"brightness_pct": p})
            if act in ("set_color", "color"):
                return "turn_on", {"color_name": _color(v)}
            if act in ("set_color_temp", "color_temp"):
                return "turn_on", {"color_temp_kelvin": int(_num(v))}
        if d == "fan" and act in ("set_speed", "speed", "percentage"):
            return "set_percentage", {"percentage": _clamp(v, 0, 100)}
        if d == "humidifier" and act in ("set_humidity", "humidity"):
            return "set_humidity", {"humidity": _clamp(v, 0, 100)}
        if d == "water_heater" and act in ("set_temperature", "temperature"):
            return "set_temperature", {"temperature": float(_num(v))}
        if d in ("select", "input_select") and act in ("select", "set_option", "set_mode", "mode"):
            return "select_option", {"option": str(v)}
        if d in ("number", "input_number") and act in ("set_value", "set", "value"):
            return "set_value", {"value": float(_num(v))}
        if d == "alarm_control_panel":
            m = {"arm_away": "alarm_arm_away", "arm_home": "alarm_arm_home", "arm_night": "alarm_arm_night",
                 "disarm": "alarm_disarm", "on": "alarm_arm_away", "off": "alarm_disarm"}
            if act in m:
                return m[act], {}
        if d in READ_ONLY:
            raise HAError(f"「{e['name']}」是{DOMAIN_CN.get(d, d)}，只能查看，不能控制")
        if on:
            return "turn_on", {}
        if off:
            return "turn_off", {}
        if act == "toggle":
            return "toggle", {}
        raise HAError(f"「{e['name']}」（{DOMAIN_CN.get(d, d)}）不支持「{action}」")

    async def control(self, target: str, action: str, value: Any = None, all_: bool = False,
                      confirm: bool = False, domain: str = "") -> str:
        if not (target or "").strip():
            return "没说要控制哪个设备"
        if os.environ.get("AIW_HA_READONLY") == "1":
            return "Home Assistant 现在是只读模式（AIW_HA_READONLY=1）"
        f = await self.find(target, domain)
        ms = [e for e in f["matches"] if e["domain"] in CONTROL]
        if not ms:
            ro = [e for e in f["matches"] if e["domain"] in READ_ONLY]
            if ro:
                return f"「{ro[0]['name']}」只能查看，不能控制。现在：{describe(ro[0])}"
            return f"没找到「{target}」。可以先用 ha_devices 查一下设备名" + (f"（{f.get('area')}）" if f.get("area") else "")
        if len(ms) > 1 and f["how"] == "name" and not all_ and not ALL_RE.search(target):
            names = "、".join(describe(e) for e in ms[:8])
            return f"「{target}」对应 {len(ms)} 个设备：{names}。问用户要哪一个，或者都要（all=true）"
        if len(ms) > 20:
            return f"一次要动 {len(ms)} 个设备，太多了，说具体一点（哪个房间、哪类设备）"
        plans, errs, ask = [], [], []
        for e in ms:
            try:
                svc, data = self.plan(e, action, value)
            except HAError as x:
                errs.append(x.message)
                continue
            why = self.needs_confirm(e, svc)
            if why and not confirm:
                ask.append(why)
            plans.append((e, svc, data))
        if ask:
            return ("需要先问用户确认：" + "；".join(ask) + "。安全相关，先口头/文字问用户，用户明确说「确认/可以」后，"
                    "再用同样参数加 confirm=true 调用。不要自己确认。")
        if not plans:
            return "；".join(errs[:3]) or "没有可以执行的操作"
        done, failed = [], []
        for e, svc, data in plans:
            try:
                res = await self._req("POST", f"/api/services/{e['domain']}/{svc}", {"entity_id": e["id"], **data})
                new = next((s for s in (res or []) if isinstance(res, list) and s.get("entity_id") == e["id"]), None)
                if new:
                    e = {**e, "state": new.get("state"), "attrs": new.get("attributes") or e["attrs"]}
                done.append(describe(e) if new else e["name"])
            except HAError as x:
                failed.append(f"{e['name']}：{x.message}")
        self._cache = None
        log.info("ha control %s %s -> %d ok %d failed", target, action, len(done), len(failed))
        out = ""
        if done:
            out = f"已执行（{action}）：" + "、".join(done[:12]) + (f" 等 {len(done)} 个" if len(done) > 12 else "")
        if failed:
            out += ("；" if out else "") + "没成功：" + "；".join(failed[:4])
        if errs:
            out += "；" + "；".join(errs[:2])
        return out

    async def list_text(self, query: str = "", domain: str = "", area: str = "", limit: int = 40) -> str:
        ents = await self.entities()
        if query:
            f = await self.find(query, domain)
            ents = f["matches"]
        if domain:
            ents = [e for e in ents if e["domain"] == domain]
        if area:
            ents = [e for e in ents if _key(area) in _key(e["area"])]
        if not ents:
            return "没有找到符合的设备"
        ents = sorted(ents, key=lambda e: (e["area"] or "~", DOMAINS.index(e["domain"]), e["name"]))
        lines = [f"{describe(e)} id={e['id']}" for e in ents[:limit]]
        more = f"\n…还有 {len(ents) - limit} 个，缩小范围再查" if len(ents) > limit else ""
        return f"共 {len(ents)} 个：\n" + "\n".join(lines) + more

    async def summary(self, limit: int = 80) -> str:
        """Device names by room, for a call's instructions (so the voice model can act without a lookup first)."""
        ents = [e for e in await self.entities() if e["domain"] in CONTROL and e["domain"] not in
                ("automation", "select", "input_select", "number", "input_number", "remote")]
        rooms: dict = {}
        for e in ents[:limit]:
            rooms.setdefault(e["area"] or "未分区", []).append(f"{e['name']}({DOMAIN_CN.get(e['domain'], e['domain'])})")
        return "；".join(f"{r}：{'、'.join(v)}" for r, v in rooms.items())

    async def camera(self, target: str) -> tuple[bytes, dict]:
        f = await self.find(target, "camera")
        cams = [e for e in f["matches"] if e["domain"] == "camera"]
        if not cams:
            raise HAError(f"没找到摄像头「{target}」")
        e = cams[0]
        r = await self._req("GET", f"/api/camera_proxy/{e['id']}", raw=True, timeout=15)
        return r.content, e


def _num(v: Any) -> float:
    m = re.search(r"-?\d+(\.\d+)?", str(v if v is not None else ""))
    if not m:
        raise HAError(f"需要一个数字，收到「{v}」")
    return float(m.group(0))


def _clamp(v: Any, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, round(_num(v)))))


COLORS = {"红": "red", "红色": "red", "绿": "green", "绿色": "green", "蓝": "blue", "蓝色": "blue", "黄": "yellow",
          "黄色": "yellow", "紫": "purple", "紫色": "purple", "粉": "pink", "粉色": "pink", "橙": "orange", "橙色": "orange",
          "白": "white", "白色": "white", "暖白": "navajowhite", "青": "cyan", "青色": "cyan"}


def _color(v: Any) -> str:
    s = str(v or "").strip()
    return COLORS.get(s, s.lower() or "white")


# ---------------------------------------------------------------------------------------------- tools
CONTROL_DESC = ("控制家里 Home Assistant 里的设备：开关灯、调亮度颜色、开关空调并调温度/模式、开关窗帘、风扇、电视音箱"
                "播放暂停音量、扫地机、门锁、场景、脚本。target 用用户说的话（「客厅的灯」「主卧空调」「回家模式」）或 entity_id。"
                "开锁、撤防、开车库门会先返回「需要先问用户确认」，用户明确同意后再加 confirm=true 调用。")
ACTIONS = ("on, off, toggle, open, close, stop, set_brightness(value 0-100), set_color(value 红/蓝/暖白…), "
           "set_color_temp(value 开尔文), set_temperature(value 度), set_mode(value 制冷/制热/自动/除湿/送风), "
           "set_position(value 0-100), set_speed(value 0-100), set_humidity, play, pause, next, previous, "
           "set_volume(value 0-100), mute, start, return_home, lock, unlock, arm_away, arm_home, disarm, select(value)")

SPECS = [
    {"type": "function", "function": {
        "name": "ha_control", "description": CONTROL_DESC,
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "设备：用户的说法或 entity_id"},
            "action": {"type": "string", "description": "动作，可选：" + ACTIONS},
            "value": {"type": "string", "description": "数值或选项（亮度、温度、颜色、音量、模式），没有就不填"},
            "all": {"type": "boolean", "description": "名字对应多个设备时是否全部执行（用户说了「都/全部」才填 true）"},
            "confirm": {"type": "boolean", "description": "只有用户刚刚明确同意了开锁/撤防/开车库门才填 true"}},
            "required": ["target", "action"]}}},
    {"type": "function", "function": {
        "name": "ha_devices",
        "description": "查家里 Home Assistant 的设备和状态（哪些灯开着、室温湿度、门窗、空调设定、谁在家）。"
                       "query 可写「客厅」「灯」「温度」或设备名；不填返回全部",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "房间、设备类型或名字，可选"}}, "required": []}}},
]
CAMERA_SPEC = {"type": "function", "function": {
    "name": "ha_camera", "description": "拍一张家里摄像头的当前画面给用户看（图片会直接显示）",
    "parameters": {"type": "object", "properties": {
        "target": {"type": "string", "description": "摄像头名字或房间，例如「门口」「客厅摄像头」"}}, "required": ["target"]}}}


def flat(spec: dict) -> dict:
    """the chat's tool schema shape: {name, description, parameters}"""
    return dict(spec["function"])


async def run_tool(ha: HomeAssistant, name: str, args: dict) -> str:
    args = args if isinstance(args, dict) else {}
    try:
        if name == "ha_control":
            return await ha.control(str(args.get("target") or ""), str(args.get("action") or ""), args.get("value"),
                                    bool(args.get("all")), bool(args.get("confirm")))
        if name == "ha_devices":
            return await ha.list_text(str(args.get("query") or ""))
    except HAError as e:
        return e.message
    except Exception as e:  # noqa: BLE001  a tool failure is said, never raised into the chat / call
        log.warning("ha tool %s: %s", name, e)
        return f"Home Assistant 出错：{type(e).__name__}: {str(e)[:120]}"
    return f"没有这个工具：{name}"


PROMPT = ("家里的 Home Assistant 已接入：用户要开关或调节家里的设备、问家里的温度湿度门窗状态时，直接用 ha_control / ha_devices "
          "做完并用一句话回报结果，不要交给电脑。名字对应多个设备时问用户要哪个；开锁、撤防、开车库门必须先问用户，用户明确同意后才加 confirm=true。")


# ---------------------------------------------------------------------------------------------- HTTP
def public(store, secrets) -> dict:
    c = cfg(store)
    return {"enabled": bool(c.get("enabled")), "url": c.get("url") or "", "areas": c.get("areas") or [],
            "entities": c.get("entities") or [], "confirm": c.get("confirm") or [],
            "verify_ssl": bool(c.get("verify_ssl", True)),
            "token_set": bool(secrets.get(SECRET)) if secrets is not None else False,
            "active": enabled(store, secrets)}


def mount(app, store, secrets, auth) -> None:
    from fastapi import Depends, HTTPException

    @app.get("/api/home-assistant", dependencies=[Depends(auth)])
    async def get_ha():
        return public(store, secrets)

    @app.put("/api/home-assistant", dependencies=[Depends(auth)])
    async def put_ha(body: dict):
        body = body or {}
        try:
            new = validate(body, cfg(store))
        except ValueError as e:
            raise HTTPException(400, str(e))
        tok = body.get("token")
        if isinstance(tok, str) and tok.strip():
            secrets.put(SECRET, tok.strip())
        if body.get("clear_token"):
            secrets.delete(SECRET)
        store.kv_set(KV, new)
        return public(store, secrets)

    @app.post("/api/home-assistant/test", dependencies=[Depends(auth)])
    async def test_ha():
        ha = HomeAssistant(store, secrets)
        try:
            p = await ha.ping()
            ents = await ha.entities(force=True)
        except HAError as e:
            return {"ok": False, "error": e.message}
        by: dict = {}
        for e in ents:
            by[DOMAIN_CN.get(e["domain"], e["domain"])] = by.get(DOMAIN_CN.get(e["domain"], e["domain"]), 0) + 1
        areas = sorted({e["area"] for e in ents if e["area"]})
        return {"ok": True, "ms": p["ms"], "count": len(ents), "by_kind": by, "areas": areas,
                "controllable": sum(1 for e in ents if e["domain"] in CONTROL)}

    @app.get("/api/home-assistant/entities", dependencies=[Depends(auth)])
    async def list_ha(q: str = ""):
        ha = HomeAssistant(store, secrets)
        try:
            ents = (await ha.find(q))["matches"] if q else await ha.entities(force=True)
        except HAError as e:
            raise HTTPException(502, e.message)
        return {"entities": [{"id": e["id"], "name": e["name"], "area": e["area"], "domain": e["domain"],
                              "state": e["state"], "text": describe(e)} for e in ents[:500]]}
