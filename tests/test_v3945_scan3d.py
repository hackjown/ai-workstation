"""v3.9.45: a 3D Model Anything room scan (house-capture zip) shared to Aiw -> scan-3d skill (GLB + offline 3D page)."""
import importlib.util
import json
import os
import struct
import zipfile

import pytest

from agent_runtime import cad as _cad
from agent_runtime import skills as _sk

np = pytest.importorskip("numpy")
HERE = os.path.dirname(__file__)
SCRIPT = os.path.join(HERE, "..", "skills", "scan-3d", "scan3d.py")

PROMPT = "建模\n\n附件（已放到电脑上）：\n- ~/workspace/uploads/大厅.zip"


def test_scan_zip_picks_scan3d_not_designer():
    assert "scan-3d" in _sk.auto(PROMPT)
    assert "house-plan-designer" not in _sk.auto(PROMPT + " 做成3D户型")
    assert not _sk.is_designer_task("把这个扫描做成3D户型\n- ~/workspace/uploads/大厅.zip")
    assert _cad.cad_mode("房间扫描的墙体尺寸\n- ~/workspace/uploads/大厅.zip") is None
    assert "scan-3d" in _sk.auto("用 3D Model Anything 扫的房间")


def test_other_zips_and_plans_unchanged():
    assert "scan-3d" not in _sk.auto("把这个 zip 解压，把里面的 PPT 转成 PDF\n- ~/workspace/uploads/a.zip")
    assert "scan-3d" not in _sk.auto("这张户型图做成3D鸟瞰\n- ~/workspace/uploads/plan.jpg")
    assert "house-plan-designer" in _sk.auto("这张户型图做成3D鸟瞰\n- ~/workspace/uploads/plan.jpg")


def test_role_and_preflight():
    from aiw_api import roles
    assert roles.SKILL_ROLE["scan-3d"] == "design"
    pf = [p for p in _sk.preflight(PROMPT) if p["id"] == "scan-3d"]
    assert pf and "aiwss.sh scan-3d" in pf[0]["setup"]
    man = open(os.path.join(HERE, "..", "skills", "scan-3d", "MANIFEST")).read().split()
    assert set(man) == {"SKILL.md", "scan3d.py"}


def _scan(tmp, colors=True):
    s = tmp / "s"
    for d in ("mesh", "rooms", "photos", "depth"):
        (s / d).mkdir(parents=True)
    n = 40
    xs, zs = np.meshgrid(np.linspace(0, 4, n), np.linspace(0, 3, n))
    V = np.stack([xs.ravel(), np.zeros(n * n), zs.ravel()], 1).astype(np.float32)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a = i * n + j
            F += [(a, a + n, a + 1), (a + 1, a + n, a + n + 1)]
    F = np.array(F)
    h = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(V)}\nproperty float x\nproperty float y\n"
         "property float z\nproperty float nx\nproperty float ny\nproperty float nz\nproperty float coverage\n"
         "property uchar red\nproperty uchar green\nproperty uchar blue\nproperty uchar inroom\n"
         f"element face {len(F)}\nproperty list uchar uint vertex_indices\nproperty uchar class\nend_header\n")
    vd = np.zeros(len(V), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4"),
                                 ("c", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1"), ("i", "u1")])
    vd["x"], vd["y"], vd["z"] = V.T
    vd["ny"], vd["i"] = 1, 1
    fd = np.zeros(len(F), dtype=[("n", "u1"), ("v", "<u4", (3,)), ("k", "u1")])
    fd["n"], fd["v"], fd["k"] = 3, F, 2
    (s / "mesh" / "room-0.ply").write_bytes(h.encode() + vd.tobytes() + fd.tobytes())
    if colors:
        np.full((len(V), 3), 200, np.uint8).tofile(str(s / "mesh" / "room-0-colors.bin"))
    T = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 2, 1.3, 0, 1]
    (s / "rooms" / "room-0.json").write_text(json.dumps({"walls": [{"transform": T, "dimensions": [4, 2.6, 0]}],
                                                          "doors": [{}], "windows": [], "openings": [],
                                                          "objects": [{"category": {"sofa": {}}, "dimensions": [2, .8, .9]}]}))
    (s / "manifest.json").write_text(json.dumps({"format": "house-capture/5", "rooms": [{"index": 0, "name": "大厅"}]},
                                                ensure_ascii=False))
    (s / "depth" / "00001.f16z").write_bytes(b"x" * 1000)
    (s / "worldmap.arworldmap").write_bytes(b"x" * 1000)
    zp = tmp / "1731480000-大厅.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for d, _, fs in os.walk(s):
            for f in fs:
                z.write(os.path.join(d, f), os.path.relpath(os.path.join(d, f), s))
    return zp


def _mod():
    spec = importlib.util.spec_from_file_location("scan3d", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_build_writes_glb_html_summary(tmp_path, capsys):
    m = _mod()
    zp = _scan(tmp_path)
    out = tmp_path / "out"
    class A: src = str(zp); faces = 1000; pass
    A.out = str(out)
    assert m.cmd_build(A) == 0
    glb = (out / "大厅.glb").read_bytes()
    magic, ver, total = struct.unpack("<III", glb[:12])
    assert magic == 0x46546C67 and ver == 2 and total == len(glb)
    jl = struct.unpack("<I", glb[12:16])[0]
    g = json.loads(glb[20:20 + jl])
    assert g["accessors"][2]["count"] // 3 <= 1000
    assert "COLOR_0" in g["meshes"][0]["primitives"][0]["attributes"]
    html = (out / "大厅-3d.html").read_text(encoding="utf-8")
    assert "webgl2" in html and "http" not in html.split("<script>")[1][:200]
    md = (out / "大厅-summary.md").read_text(encoding="utf-8")
    assert "沙发" in md and "照片颜色" in md
    assert "SEND:" in capsys.readouterr().out


def test_no_colors_falls_back_and_slim(tmp_path, capsys):
    m = _mod()
    zp = _scan(tmp_path, colors=False)
    class A: faces = 300000
    A.src, A.out = str(zp), str(tmp_path / "o")
    assert m.cmd_build(A) == 0
    assert "没有照片颜色缓存" in (tmp_path / "o" / "大厅-summary.md").read_text(encoding="utf-8")
    class B: keep_depth = False; quality = 82
    B.folder = str(tmp_path / "o" / "src")
    assert m.cmd_slim(B) == 0
    assert not (tmp_path / "o" / "src" / "depth").exists()
    assert not (tmp_path / "o" / "src" / "worldmap.arworldmap").exists()
    assert (tmp_path / "o" / "scan-slim.zip").exists()


def test_not_a_scan(tmp_path, capsys):
    m = _mod()
    zp = tmp_path / "x.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("a.txt", "hi")
    class A: faces = 1000
    A.src, A.out = str(zp), str(tmp_path / "o")
    assert m.cmd_build(A) == 2
    assert "NO_SCAN" in capsys.readouterr().out


def _roomplan_scan(tmp):
    import math
    s = tmp / "rp"
    (s / "rooms").mkdir(parents=True)
    def T(yaw, x, y, z):
        c, sn = math.cos(yaw), math.sin(yaw)
        return np.array([[c, 0, sn, x], [0, 1, 0, y], [-sn, 0, c, z], [0, 0, 0, 1]]).T.ravel().tolist()
    walls = [{"identifier": "w0", "transform": T(0, 0, 1.35, -2), "dimensions": [5, 2.7, 0]},
             {"identifier": "w1", "transform": T(0, 0, 1.35, 2), "dimensions": [5, 2.7, 0]},
             {"identifier": "w2", "transform": T(math.pi / 2, -2.5, 1.35, 0), "dimensions": [4, 2.7, 0]},
             {"identifier": "w3", "transform": T(math.pi / 2, 2.5, 1.35, 0), "dimensions": [4, 2.7, 0]}]
    room = {"walls": walls,
            "doors": [{"parentIdentifier": "w1", "transform": T(0, 1, 1.05, 2), "dimensions": [0.9, 2.1, 0]}],
            "windows": [{"transform": T(0, -1, 1.5, -2), "dimensions": [1.5, 1.2, 0]}], "openings": [],
            "objects": [{"category": {"sofa": {}}, "transform": T(0, -1, 0.42, -1.4), "dimensions": [2, 0.85, 0.9]},
                        {"category": {"table": {}}, "transform": T(0, 1, 0.38, 0), "dimensions": [1.4, 0.76, 0.8]}]}
    (s / "rooms" / "room-0.json").write_text(json.dumps(room))
    (s / "rooms" / "room-0.usdz").write_bytes(b"PK-apple")
    (s / "manifest.json").write_text(json.dumps({"format": "house-capture/5", "rooms": [{"index": 0, "name": "大厅"}]},
                                                ensure_ascii=False))
    return s


def test_roomplan_rebuild(tmp_path, capsys):
    mod = _mod()
    s = _roomplan_scan(tmp_path)
    parts = mod.roomplan_model(str(s / "rooms" / "room-0.json"))
    assert parts.count["Wall"] == 4 and parts.count["Door"] == 1 and parts.count["Window"] == 1
    assert parts.count["Sofa"] == 1 and parts.count["Table"] == 1
    assert any(p[0] == "Floor" for p in parts.items)
    wall_w0 = next(p for p in parts.items if p[0] == "Wall_1")
    assert len(wall_w0[4]) == 12 * 4          # the window cuts wall 1 into 4 boxes (left, right, below, above)
    out = tmp_path / "out"
    import sys
    argv = sys.argv
    sys.argv = ["scan3d.py", "build", str(s), "--out", str(out)]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = argv
    so = capsys.readouterr().out
    assert "NO_MESH" in so and "rebuilt model was still written" in so
    glb = (out / "rp-model.glb").read_bytes()
    assert glb[:4] == b"glTF"
    js = json.loads(glb[20:20 + struct.unpack("<I", glb[12:16])[0]])
    assert {"Wall_1", "Door_1", "Sofa_1", "Floor"} <= {n["name"] for n in js["nodes"]}
    assert (out / "rp-roomplan.usdz").read_bytes() == b"PK-apple"
    html = (out / "rp-3d.html").read_text()
    assert "大厅 · 建模" in html
    send = [l for l in so.splitlines() if l.startswith("SEND:")][0]
    assert "rp-model.glb" in send and "rp-roomplan.usdz" in send
    try:
        import pxr  # noqa: F401
        assert (out / "rp-model.usdz").read_bytes()[:2] == b"PK"
    except ImportError:
        assert "model_usdz_error" in (out / "rp-summary.json").read_text()


def test_floorplan_and_usdz_check(tmp_path):
    mod = _mod()
    s = _roomplan_scan(tmp_path)
    G = mod.plan_geometry(str(s / "rooms" / "room-0.json"))
    assert len(G["doors"]) == 1 and len(G["windows"]) == 1 and len(G["objs"]) == 2
    assert len(G["segs"]) == 6          # 4 walls, two of them cut by the door / window
    assert sorted(round(L, 2) for _, _, L in G["lens"]) == [4.0, 4.0, 5.0, 5.0]
    x0, y0, x1, y1 = G["box"]
    assert abs((x1 - x0) * (y1 - y0) - 20) < 0.01
    svg = tmp_path / "p.svg"
    mod.write_floorplan_svg(str(svg), G, "大厅 · 平面图")
    t = svg.read_text(encoding="utf-8")
    assert "沙发" in t and "5.00 m" in t and "<path" in t
    png = tmp_path / "p.png"
    mod.write_floorplan_png(str(png), G, "大厅 · 平面图")
    assert png.read_bytes()[:4] == b"\x89PNG"
    bad = tmp_path / "bad.usdz"
    bad.write_bytes(b"not a zip")
    assert mod.check_usdz(str(bad))["ok"] in (False, None)
    try:
        import pxr  # noqa: F401
    except ImportError:
        return
    good = tmp_path / "m.usdz"
    assert mod.write_model_usdz(str(good), mod.roomplan_model(str(s / "rooms" / "room-0.json"))) == ""
    ck = mod.check_usdz(str(good))
    assert ck["ok"] is True, ck
    import zipfile
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(tmp_path / "d.usdz", "w", zipfile.ZIP_DEFLATED) as z:
        for i in src.infolist():
            z.writestr(i.filename, src.read(i))
    assert mod.check_usdz(str(tmp_path / "d.usdz"))["ok"] is False
