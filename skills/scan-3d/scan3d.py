#!/usr/bin/env python3
"""scan3d.py: an iPhone room scan (3D Model Anything / house-capture zip) -> a model you can look at, and a slim copy.

  scan3d.py build <scan.zip|folder> [--out DIR] [--faces 300000] [--all-rooms]
      unzip, find every session (manifest.json, format house-capture/*), and for each room with a LiDAR mesh
      (mesh/room-N.ply) write into DIR:
        <name>-room-N.glb        vertex-coloured mesh (photo colours from mesh/room-N-colors.bin), simplified
        <name>-3d.html           one offline file: every room, drag/pinch/pan, top view, a slider that cuts the
                                 ceiling off (dollhouse view). No CDN, opens on a phone.
        <name>-summary.md/json   room size, walls / doors / windows / objects (RoomPlan), photo count, mesh stats
  scan3d.py slim <folder> [--keep-depth] [--quality 82]
      shrink the unzipped scan in place: re-save photos/*.jpg at --quality (same pixels and EXIF, so poses and
      intrinsics stay valid), drop depth/ (only optional depth fusion uses it) and *.arworldmap (only the phone uses
      it), then write <folder>-slim.zip. Prints sizes before and after.
  scan3d.py info <scan.zip|folder>      what is inside, sizes per part, no writing

Needs python3 + numpy (+ Pillow for slim). Coordinates: metres, +Y up (ARKit world), same as glTF.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shutil
import struct
import sys
import zipfile

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("numpy missing: python3 -m pip install --user numpy  (or --break-system-packages)")

CLASS_RGB = {0: (170, 170, 170), 1: (214, 205, 190), 2: (150, 125, 100), 3: (235, 235, 235), 4: (160, 110, 70),
             5: (120, 130, 160), 6: (150, 200, 230), 7: (140, 95, 60)}


# ----------------------------------------------------------------------------------------------------------- unpack
def unpack(src: str, out: str) -> str:
    """Folder that holds the scan: src itself, or src unzipped under out/src."""
    if os.path.isdir(src):
        return src
    if not zipfile.is_zipfile(src):
        sys.exit(f"not a zip or folder: {src}")
    dst = os.path.join(out, "src")
    if os.path.isdir(dst) and any(True for _ in os.scandir(dst)):
        return dst
    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        for i in z.infolist():
            n = i.filename
            if n.startswith("__MACOSX/") or "/._" in "/" + n:
                continue
            p = os.path.realpath(os.path.join(dst, n))
            if not p.startswith(os.path.realpath(dst) + os.sep):
                continue   # zip slip
            if i.is_dir():
                os.makedirs(p, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with z.open(i) as f, open(p, "wb") as g:
                shutil.copyfileobj(f, g, 1 << 20)
    return dst


def sessions(root: str) -> list[dict]:
    """Every scan session under root: {dir, manifest}. A whole-house export has many; a room export has one."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("photos", "depth", "mesh")]
        if "manifest.json" in files:
            try:
                m = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(m, dict) and (str(m.get("format", "")).startswith("house-capture")
                                        or os.path.isdir(os.path.join(d, "mesh")) or os.path.isdir(os.path.join(d, "rooms"))):
                out.append({"dir": d, "manifest": m})
    if not out and (os.path.isdir(os.path.join(root, "mesh")) or os.path.isdir(os.path.join(root, "rooms"))):
        out.append({"dir": root, "manifest": {}})
    return sorted(out, key=lambda s: s["dir"])


def room_indices(sdir: str) -> list[int]:
    got = set()
    for sub in ("mesh", "rooms"):
        p = os.path.join(sdir, sub)
        if os.path.isdir(p):
            for f in os.listdir(p):
                m = re.fullmatch(r"room-(\d+)(\.ply|\.json)", f)
                if m:
                    got.add(int(m.group(1)))
    return sorted(got)


def room_name(sdir: str, manifest: dict, idx: int) -> str:
    for key in ("rooms",):
        rs = manifest.get(key)
        if isinstance(rs, list):
            for r in rs:
                if isinstance(r, dict) and int(r.get("index", -1)) == idx and r.get("name"):
                    return str(r["name"])
            if idx < len(rs) and isinstance(rs[idx], dict) and rs[idx].get("name"):
                return str(rs[idx]["name"])
    return f"room-{idx}"


# -------------------------------------------------------------------------------------------------------------- PLY
PLY_T = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "<i2", "int16": "<i2", "ushort": "<u2",
         "uint16": "<u2", "int": "<i4", "int32": "<i4", "uint": "<u4", "uint32": "<u4", "float": "<f4",
         "float32": "<f4", "double": "<f8", "float64": "<f8"}


def read_ply(path: str) -> dict:
    """Binary little-endian PLY as written by the app (triangles only). Returns verts, normals, inroom, faces, cls."""
    with open(path, "rb") as f:
        data = f.read()
    end = data.find(b"end_header\n")
    if not data.startswith(b"ply") or end < 0:
        raise ValueError("not a PLY file")
    header = data[:end].decode("ascii", "ignore").splitlines()
    body = memoryview(data)[end + len(b"end_header\n"):]
    if not any("binary_little_endian" in h for h in header):
        raise ValueError("only binary_little_endian PLY is supported")
    elems, cur = [], None
    for h in header:
        p = h.split()
        if not p:
            continue
        if p[0] == "element":
            cur = {"name": p[1], "count": int(p[2]), "props": []}
            elems.append(cur)
        elif p[0] == "property" and cur is not None:
            if p[1] == "list":
                cur["props"].append(("list", p[2], p[3], p[4]))
            else:
                cur["props"].append((p[2], PLY_T[p[1]]))
    off = 0
    out: dict = {}
    for e in elems:
        if e["name"] == "vertex":
            dt = np.dtype([(n, t) for n, t in e["props"]])
            v = np.frombuffer(body, dtype=dt, count=e["count"], offset=off)
            off += dt.itemsize * e["count"]
            out["verts"] = np.stack([v["x"], v["y"], v["z"]], 1).astype(np.float32)
            if "nx" in dt.names:
                out["normals"] = np.stack([v["nx"], v["ny"], v["nz"]], 1).astype(np.float32)
            if "inroom" in dt.names:
                out["inroom"] = v["inroom"].astype(bool)
        elif e["name"] == "face":
            fields = []
            for pr in e["props"]:
                if pr[0] == "list":
                    fields += [("n", PLY_T[pr[1]]), ("i", PLY_T[pr[2]], (3,))]
                else:
                    fields.append((pr[0], pr[1]))
            dt = np.dtype(fields)
            f = np.frombuffer(body, dtype=dt, count=e["count"], offset=off)
            off += dt.itemsize * e["count"]
            if e["count"] and not np.all(f["n"] == 3):
                raise ValueError("faces that are not triangles")
            out["faces"] = f["i"].astype(np.int64)
            out["cls"] = f["class"].astype(np.uint8) if "class" in dt.names else np.zeros(e["count"], np.uint8)
        else:
            raise ValueError(f"unexpected PLY element {e['name']}")
    out.setdefault("faces", np.zeros((0, 3), np.int64))
    out.setdefault("cls", np.zeros(0, np.uint8))
    return out


def vertex_colors(ply_path: str, mesh: dict) -> tuple[np.ndarray, str]:
    """Photo colours the app cached (mesh/room-N-colors.bin, rgb per vertex); else a shaded colour per surface class."""
    n = len(mesh["verts"])
    cpath = ply_path[:-4] + "-colors.bin"
    if os.path.isfile(cpath) and os.path.getsize(cpath) == n * 3:
        return np.fromfile(cpath, np.uint8).reshape(n, 3), "photo"
    col = np.full((n, 3), 170, np.float32)
    f, c = mesh["faces"], mesh["cls"]
    for k, rgb in CLASS_RGB.items():
        sel = f[c == k].ravel()
        col[sel] = rgb
    if "normals" in mesh:
        nn = mesh["normals"]
        light = np.clip(0.55 + 0.45 * (nn @ np.array([0.3, 0.8, 0.5], np.float32)), 0.35, 1.0)
        col *= light[:, None]
    return np.clip(col, 0, 255).astype(np.uint8), "class"


# --------------------------------------------------------------------------------------------------------- simplify
def keep_room(mesh: dict) -> np.ndarray:
    """Faces of this room only (the mesh can carry earlier rooms; inroom marks this one)."""
    f = mesh["faces"]
    if "inroom" not in mesh or not len(f):
        return f
    ok = mesh["inroom"][f].any(1)
    return f[ok] if ok.sum() > 0.1 * len(f) else f


def cluster(verts: np.ndarray, cols: np.ndarray, faces: np.ndarray, cell: float):
    q = np.floor(verts / cell).astype(np.int64)
    q -= q.min(0)
    dims = q.max(0) + 1
    key = (q[:, 0] * dims[1] + q[:, 1]) * dims[2] + q[:, 2]
    uniq, inv = np.unique(key, return_inverse=True)
    m = len(uniq)
    cnt = np.bincount(inv, minlength=m).astype(np.float64)
    v = np.stack([np.bincount(inv, verts[:, i], m) for i in range(3)], 1) / cnt[:, None]
    c = np.stack([np.bincount(inv, cols[:, i].astype(np.float64), m) for i in range(3)], 1) / cnt[:, None]
    nf = inv[faces]
    good = (nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) & (nf[:, 0] != nf[:, 2])
    nf = nf[good]
    s = np.sort(nf, 1)
    _, first = np.unique(s, axis=0, return_index=True)
    nf = nf[np.sort(first)]
    used = np.unique(nf)
    remap = np.full(m, -1, np.int64)
    remap[used] = np.arange(len(used))
    return v[used].astype(np.float32), np.clip(c[used], 0, 255).astype(np.uint8), remap[nf]


def simplify(verts, cols, faces, target: int):
    """Vertex clustering on a grid that grows until the mesh has <= target faces (keeps vertex colours)."""
    # drop vertices no face uses first
    used = np.unique(faces)
    remap = np.full(len(verts), -1, np.int64)
    remap[used] = np.arange(len(used))
    verts, cols, faces = verts[used], cols[used], remap[faces]
    if len(faces) <= target:
        return verts, cols, faces, 0.0
    cell = 0.01
    best = None
    for _ in range(12):
        v, c, f = cluster(verts, cols, faces, cell)
        best = (v, c, f, cell)
        if len(f) <= target:
            break
        cell *= 1.0 + 0.6 * min(1.0, len(f) / target - 1.0) + 0.1
    return best


# -------------------------------------------------------------------------------------------------------------- GLB
def write_glb(path: str, verts: np.ndarray, cols: np.ndarray, faces: np.ndarray, name: str) -> int:
    pos = verts.astype("<f4").tobytes()
    rgba = np.concatenate([cols, np.full((len(cols), 1), 255, np.uint8)], 1).astype(np.uint8).tobytes()
    idx = faces.astype("<u4").tobytes()
    def pad(b, ch=b"\x00"):
        return b + ch * (-len(b) % 4)
    blob = pad(pos) + pad(rgba) + pad(idx)
    o_rgba = len(pad(pos))
    o_idx = o_rgba + len(pad(rgba))
    gltf = {
        "asset": {"version": "2.0", "generator": "aiw scan3d"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": name}],
        "materials": [{"name": "scan", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0,
                                                                 "roughnessFactor": 1}, "doubleSided": True,
                       "extensions": {"KHR_materials_unlit": {}}}],
        "extensionsUsed": ["KHR_materials_unlit"],
        "meshes": [{"name": name, "primitives": [{"attributes": {"POSITION": 0, "COLOR_0": 1}, "indices": 2,
                                                   "material": 0, "mode": 4}]}],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos), "target": 34962},
                        {"buffer": 0, "byteOffset": o_rgba, "byteLength": len(rgba), "target": 34962},
                        {"buffer": 0, "byteOffset": o_idx, "byteLength": len(idx), "target": 34963}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3",
                       "min": verts.min(0).tolist(), "max": verts.max(0).tolist()},
                      {"bufferView": 1, "componentType": 5121, "normalized": True, "count": len(cols), "type": "VEC4"},
                      {"bufferView": 2, "componentType": 5125, "count": int(faces.size), "type": "SCALAR"}],
    }
    js = pad(json.dumps(gltf, separators=(",", ":")).encode(), b" ")
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(blob), 0x004E4942) + blob)
    return total


# --------------------------------------------------------------------------------------------------------- RoomPlan
def _mat(t):
    return [[t[c * 4 + r] for c in range(4)] for r in range(4)]


def roomplan(path: str) -> dict:
    """Walls / doors / windows / objects from rooms/room-N.json (RoomPlan CapturedRoom as JSON)."""
    if not os.path.isfile(path):
        return {}
    try:
        S = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    walls = S.get("walls") or []
    out = {"walls": len(walls), "doors": len(S.get("doors") or []), "windows": len(S.get("windows") or []),
           "openings": len(S.get("openings") or []), "objects": []}
    ends, tops, bottoms = [], [], []
    sx = sy = 0.0
    for w in walls:
        try:
            m = _mat(w["transform"])
            d = w["dimensions"]
        except Exception:  # noqa: BLE001
            continue
        x = (m[0][0], m[2][0])
        ang = math.atan2(x[1], x[0]) * 4
        sx += math.cos(ang) * d[0]
        sy += math.sin(ang) * d[0]
        cx, cy, cz = m[0][3], m[1][3], m[2][3]
        for s in (-1, 1):
            ends.append((cx + x[0] * s * d[0] / 2, cz + x[1] * s * d[0] / 2))
        tops.append(cy + d[1] / 2)
        bottoms.append(cy - d[1] / 2)
    if ends:
        th = math.atan2(sy, sx) / 4
        c, s = math.cos(-th), math.sin(-th)
        xs = [c * a - s * b for a, b in ends]
        zs = [s * a + c * b for a, b in ends]
        out["size_m"] = [round(max(xs) - min(xs), 2), round(max(zs) - min(zs), 2)]
        out["height_m"] = round(max(tops) - min(bottoms), 2)
    if S.get("floors"):
        try:
            poly = S["floors"][0].get("polygonCorners") or []
            if len(poly) >= 3:
                a = 0.0
                for i in range(len(poly)):
                    x1, y1 = poly[i][0], poly[i][1]
                    x2, y2 = poly[(i + 1) % len(poly)][0], poly[(i + 1) % len(poly)][1]
                    a += x1 * y2 - x2 * y1
                out["floor_area_m2"] = round(abs(a) / 2, 1)
        except Exception:  # noqa: BLE001
            pass
    for o in S.get("objects") or []:
        try:
            cat = next(iter(o["category"])) if isinstance(o.get("category"), dict) else str(o.get("category"))
            out["objects"].append({"category": cat, "size_m": [round(float(v), 2) for v in o.get("dimensions", [])[:3]]})
        except Exception:  # noqa: BLE001
            continue
    return out


PART_ZH = {"Wall": "墙段组", "Door": "门", "Window": "窗", "WindowFrame": "窗框"}
OBJ_ZH = {"storage": "柜子", "refrigerator": "冰箱", "stove": "灶台", "bed": "床", "sink": "水槽", "washerDryer": "洗衣机",
          "toilet": "马桶", "bathtub": "浴缸", "oven": "烤箱", "dishwasher": "洗碗机", "table": "桌子", "sofa": "沙发",
          "chair": "椅子", "fireplace": "壁炉", "television": "电视", "stairs": "楼梯"}


# ------------------------------------------------------------------------------------------------- RoomPlan model
# v3.9.47: the clean model the 3D Model Anything app previews: walls (with door / window / opening holes), doors,
# windows, floor and furniture rebuilt from rooms/room-N.json (RoomPlan CapturedRoom), one named part per element so
# Blender / three.js editor / Reality Composer can move, hide or recolour each. Written as GLB, USDZ (iPhone Quick
# Look and AR, needs usd-core) and a view in the offline page.
MAT = {"wall": ((0.93, 0.92, 0.89), 1.0), "floor": ((0.78, 0.68, 0.55), 1.0), "door": ((0.55, 0.38, 0.24), 1.0),
       "window": ((0.62, 0.80, 0.92), 0.35), "frame": ((0.85, 0.85, 0.85), 1.0),
       "sofa": ((0.45, 0.52, 0.62), 1.0), "chair": ((0.62, 0.45, 0.30), 1.0), "table": ((0.70, 0.52, 0.34), 1.0),
       "bed": ((0.85, 0.83, 0.80), 1.0), "storage": ((0.80, 0.74, 0.64), 1.0), "television": ((0.12, 0.12, 0.13), 1.0),
       "refrigerator": ((0.86, 0.87, 0.88), 1.0), "appliance": ((0.82, 0.83, 0.84), 1.0),
       "bath": ((0.96, 0.96, 0.97), 1.0), "stairs": ((0.66, 0.58, 0.48), 1.0), "fireplace": ((0.50, 0.42, 0.38), 1.0),
       "object": ((0.72, 0.70, 0.66), 1.0)}
_CAT_MAT = {"stove": "appliance", "oven": "appliance", "dishwasher": "appliance", "washerDryer": "appliance",
            "sink": "bath", "toilet": "bath", "bathtub": "bath"}
WALL_T = 0.10


def _cat(o) -> str:
    c = o.get("category")
    if isinstance(c, dict):
        return next(iter(c), "object")
    return str(c or "object")


def _m4(t) -> "np.ndarray":
    return np.array(t, dtype=float).reshape(4, 4).T   # RoomPlan / simd: column-major


def _box(M, c, s):
    """8 corners of a box (centre c, size s, in the frame M) -> (24 verts, 24 normals, 12 tris), flat faces."""
    c, s = np.asarray(c, float), np.asarray(s, float) / 2
    V, N, F = [], [], []
    for ax in range(3):
        for sg in (-1, 1):
            u, v = [a for a in range(3) if a != ax]
            n = np.zeros(3); n[ax] = sg
            q = []
            for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = np.zeros(3); p[ax] = sg; p[u] = a; p[v] = b
                q.append(c + p * s)
            if np.dot(np.cross(q[1] - q[0], q[2] - q[0]), n) < 0:
                q = q[::-1]
            base = len(V)
            V += q; N += [n] * 4
            F += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    V = np.array(V); N = np.array(N)
    V = (M[:3, :3] @ V.T).T + M[:3, 3]
    N = (M[:3, :3] @ N.T).T
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)
    return V, N, np.array(F)


class Parts:
    def __init__(self):
        self.items = []   # (name, mat, V, N, F)
        self.count = {}

    def add(self, kind, mat, boxes):
        if not boxes:
            return
        V, N, F = [], [], []
        for M, c, s in boxes:
            if min(s) <= 0:
                continue
            v, n, f = _box(M, c, s)
            F.append(f + sum(len(x) for x in V)); V.append(v); N.append(n)
        if not V:
            return
        self.count[kind] = self.count.get(kind, 0) + 1
        self.items.append((f"{kind}_{self.count[kind]}", mat, np.concatenate(V), np.concatenate(N), np.concatenate(F)))


def _furniture(cat, M, w, h, d):
    """A few boxes that read as the object (RoomPlan gives a box; +Z is the front)."""
    if cat == "table":
        t, l = min(0.05, h * 0.15), min(0.06, w * 0.1)
        b = [(M, (0, h / 2 - t / 2, 0), (w, t, d))]
        for sx in (-1, 1):
            for sz in (-1, 1):
                b.append((M, (sx * (w / 2 - l), -t / 2, sz * (d / 2 - l)), (l, h - t, l)))
        return b
    if cat == "chair":
        seat = min(h * 0.5, 0.48) - h / 2
        l = 0.04
        b = [(M, (0, seat, 0), (w, 0.06, d)), (M, (0, (seat + h / 2) / 2, -d / 2 + 0.03), (w, h / 2 - seat, 0.06))]
        for sx in (-1, 1):
            for sz in (-1, 1):
                b.append((M, (sx * (w / 2 - l), (seat - h / 2) / 2, sz * (d / 2 - l)), (l, seat + h / 2, l)))
        return b
    if cat == "sofa":
        seat = min(h * 0.55, 0.45)
        arm = min(0.2, w * 0.12)
        return [(M, (0, -h / 2 + seat / 2, 0.08), (w - 2 * arm, seat, d - 0.16)),
                (M, (0, 0, -d / 2 + 0.09), (w, h, 0.18)),
                (M, (-w / 2 + arm / 2, -h / 2 + seat * 0.65, 0), (arm, seat * 1.3, d)),
                (M, (w / 2 - arm / 2, -h / 2 + seat * 0.65, 0), (arm, seat * 1.3, d))]
    if cat == "bed":
        mat_h = min(0.5, h * 0.6)
        return [(M, (0, -h / 2 + mat_h / 2, 0.03), (w, mat_h, d - 0.06)), (M, (0, 0, -d / 2 + 0.03), (w, h, 0.06))]
    return [(M, (0, 0, 0), (w, h, d))]


def _triangulate(P):
    """Ear clipping of a simple polygon in 2D (list of (x, y)) -> triangles of indices."""
    idx = list(range(len(P)))
    area = sum(P[i][0] * P[(i + 1) % len(P)][1] - P[(i + 1) % len(P)][0] * P[i][1] for i in range(len(P)))
    if area < 0:
        idx.reverse()
    tris, guard = [], 0
    def inside(p, a, b, c):
        d = lambda p1, p2, p3: (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
        d1, d2, d3 = d(p, a, b), d(p, b, c), d(p, c, a)
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))
    while len(idx) > 3 and guard < 10000:
        guard += 1
        for k in range(len(idx)):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            a, b, c = P[i0], P[i1], P[i2]
            if (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) <= 0:
                continue
            if any(inside(P[j], a, b, c) for j in idx if j not in (i0, i1, i2)):
                continue
            tris.append((i0, i1, i2)); idx.pop(k); break
        else:
            break
    if len(idx) == 3:
        tris.append(tuple(idx))
    return tris


def roomplan_model(path: str):
    """Parts of the rebuilt room, or None when the RoomPlan file has no walls."""
    try:
        S = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    walls = [w for w in S.get("walls") or [] if w.get("transform") and w.get("dimensions")]
    if not walls:
        return None
    parts = Parts()
    holes = {i: [] for i in range(len(walls))}
    WM = [_m4(w["transform"]) for w in walls]
    ids = {str(w.get("identifier")): i for i, w in enumerate(walls) if w.get("identifier")}
    for kind in ("doors", "windows", "openings"):
        for o in S.get(kind) or []:
            try:
                M = _m4(o["transform"]); W, H = float(o["dimensions"][0]), float(o["dimensions"][1])
            except Exception:  # noqa: BLE001
                continue
            wi = ids.get(str(o.get("parentIdentifier")))
            if wi is None:   # nearest wall plane that contains the centre
                best = None
                for i, Mw in enumerate(WM):
                    loc = np.linalg.inv(Mw) @ np.append(M[:3, 3], 1)
                    dw = float(walls[i]["dimensions"][0])
                    if abs(loc[2]) < 0.3 and abs(loc[0]) <= dw / 2 + 0.1 and (best is None or abs(loc[2]) < best[0]):
                        best = (abs(loc[2]), i)
                wi = best[1] if best else None
            if wi is not None:
                loc = np.linalg.inv(WM[wi]) @ np.append(M[:3, 3], 1)
                holes[wi].append((float(loc[0]), float(loc[1]), W, H))
            if kind == "doors":
                parts.add("Door", "door", [(M, (0, 0, 0), (W, H, 0.04))])
            elif kind == "windows":
                parts.add("Window", "window", [(M, (0, 0, 0), (W, H, 0.02))])
                parts.add("WindowFrame", "frame", [(M, (0, -H / 2 + 0.025, 0), (W, 0.05, WALL_T + 0.02)),
                                                   (M, (0, H / 2 - 0.025, 0), (W, 0.05, WALL_T + 0.02)),
                                                   (M, (-W / 2 + 0.025, 0, 0), (0.05, H, WALL_T + 0.02)),
                                                   (M, (W / 2 - 0.025, 0, 0), (0.05, H, WALL_T + 0.02))])
    lows = []
    for i, w in enumerate(walls):
        Wd, Ht = float(w["dimensions"][0]), float(w["dimensions"][1])
        lows.append((WM[i] @ np.array([0, -Ht / 2, 0, 1]))[1])
        hs = [(max(-Wd / 2, x - a / 2), min(Wd / 2, x + a / 2), max(-Ht / 2, y - b / 2), min(Ht / 2, y + b / 2))
              for x, y, a, b in holes[i]]
        xs = sorted({-Wd / 2, Wd / 2, *[h[0] for h in hs], *[h[1] for h in hs]})
        ys = sorted({-Ht / 2, Ht / 2, *[h[2] for h in hs], *[h[3] for h in hs]})
        boxes = []
        for x0, x1 in zip(xs, xs[1:]):
            run = None
            for y0, y1 in zip(ys, ys[1:]):
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                solid = not any(h[0] < cx < h[1] and h[2] < cy < h[3] for h in hs)
                if solid:
                    run = (run[0], y1) if run else (y0, y1)
                elif run:
                    boxes.append((WM[i], ((x0 + x1) / 2, sum(run) / 2, 0), (x1 - x0, run[1] - run[0], WALL_T))); run = None
            if run:
                boxes.append((WM[i], ((x0 + x1) / 2, sum(run) / 2, 0), (x1 - x0, run[1] - run[0], WALL_T)))
        parts.add("Wall", "wall", boxes)
    floor_y = float(min(lows)) if lows else 0.0
    # floor: the RoomPlan floor polygon when there is one, else the wall ends' hull
    pts = []
    for f in S.get("floors") or []:
        try:
            M = _m4(f["transform"])
            pts = [(M @ np.array([float(c[0]), float(c[1]), float(c[2]) if len(c) > 2 else 0.0, 1]))[[0, 2]].tolist()
                   for c in f.get("polygonCorners") or []]
        except Exception:  # noqa: BLE001
            pts = []
        if len(pts) >= 3:
            break
    if len(pts) < 3:
        ends = []
        for i, w in enumerate(walls):
            Wd = float(w["dimensions"][0])
            for s in (-1, 1):
                ends.append(tuple((WM[i] @ np.array([s * Wd / 2, 0, 0, 1]))[[0, 2]]))
        ends = sorted(set((round(a, 3), round(b, 3)) for a, b in ends))
        if len(ends) >= 3:   # convex hull (monotone chain)
            def cr(o, a, b):
                return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
            lo_, hi_ = [], []
            for p in ends:
                while len(lo_) >= 2 and cr(lo_[-2], lo_[-1], p) <= 0:
                    lo_.pop()
                lo_.append(p)
            for p in reversed(ends):
                while len(hi_) >= 2 and cr(hi_[-2], hi_[-1], p) <= 0:
                    hi_.pop()
                hi_.append(p)
            pts = lo_[:-1] + hi_[:-1]
    if len(pts) >= 3:
        tris = _triangulate(pts)
        if tris:
            V = np.array([(x, floor_y - 0.005, z) for x, z in pts])
            F = []
            for a, b, c in tris:   # face up (+Y): (x,z) counter-clockwise seen from above is clockwise in x/-z
                F.append((a, c, b))
            parts.items.append(("Floor", "floor", V, np.tile([0, 1.0, 0], (len(V), 1)), np.array(F)))
    for o in S.get("objects") or []:
        try:
            M = _m4(o["transform"]); w, h, d = (float(v) for v in o["dimensions"][:3])
        except Exception:  # noqa: BLE001
            continue
        cat = _cat(o)
        mat = _CAT_MAT.get(cat, cat if cat in MAT else "object")
        parts.add(cat[:1].upper() + cat[1:], mat, _furniture(cat, M, w, h, d))
    return parts


def write_model_glb(path: str, parts: Parts) -> int:
    mats = sorted({p[1] for p in parts.items})
    mi = {m: i for i, m in enumerate(mats)}
    blob, views, accs, meshes, nodes = b"", [], [], [], []
    def put(arr, target):
        nonlocal blob
        b = arr.tobytes()
        blob += b"\x00" * (-len(blob) % 4)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(b), "target": target})
        blob += b
        return len(views) - 1
    for name, mat, V, N, F in parts.items:
        v = put(V.astype("<f4"), 34962); n = put(N.astype("<f4"), 34962); f = put(F.astype("<u4").ravel(), 34963)
        accs += [{"bufferView": v, "componentType": 5126, "count": len(V), "type": "VEC3",
                  "min": V.min(0).tolist(), "max": V.max(0).tolist()},
                 {"bufferView": n, "componentType": 5126, "count": len(N), "type": "VEC3"},
                 {"bufferView": f, "componentType": 5125, "count": int(F.size), "type": "SCALAR"}]
        k = len(accs)
        meshes.append({"name": name, "primitives": [{"attributes": {"POSITION": k - 3, "NORMAL": k - 2}, "indices": k - 1,
                                                     "material": mi[mat]}]})
        nodes.append({"name": name, "mesh": len(meshes) - 1})
    materials = []
    for m in mats:
        (r, g, b), a = MAT.get(m, MAT["object"])
        mm = {"name": m, "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, a], "metallicFactor": 0,
                                                    "roughnessFactor": 0.85}, "doubleSided": True}
        if a < 1:
            mm["alphaMode"] = "BLEND"
        materials.append(mm)
    gltf = {"asset": {"version": "2.0", "generator": "aiw scan3d model"}, "scene": 0,
            "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes, "materials": materials,
            "buffers": [{"byteLength": len(blob) + (-len(blob) % 4)}], "bufferViews": views, "accessors": accs}
    blob += b"\x00" * (-len(blob) % 4)
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        fh.write(struct.pack("<II", len(blob), 0x004E4942) + blob)
    return total


def write_model_usdz(path: str, parts: Parts) -> str:
    """'' when written, else why not (usd-core missing). Y up, metres: what iOS Quick Look / AR expects."""
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils, Vt
    except ImportError:
        return "usd-core missing (python3 -m pip install --user usd-core)"
    import tempfile
    tmp = tempfile.mkdtemp(prefix="scan3d-usd-")
    usdc = os.path.join(tmp, "model.usdc")
    st = Usd.Stage.CreateNew(usdc)
    UsdGeom.SetStageUpAxis(st, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(st, 1.0)
    root = UsdGeom.Xform.Define(st, "/Room")
    st.SetDefaultPrim(root.GetPrim())
    mats = {}
    for m in sorted({p[1] for p in parts.items}):
        (r, g, b), a = MAT.get(m, MAT["object"])
        mat = UsdShade.Material.Define(st, f"/Room/Materials/{m}")
        sh = UsdShade.Shader.Define(st, f"/Room/Materials/{m}/Surface")
        sh.CreateIdAttr("UsdPreviewSurface")
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(r, g, b))
        sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.85)
        sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        if a < 1:
            sh.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(float(a))
        mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
        mats[m] = mat
    for name, mat, V, N, F in parts.items:
        mesh = UsdGeom.Mesh.Define(st, f"/Room/{name}")
        mesh.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, p)) for p in V]))
        mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(F)))
        mesh.CreateFaceVertexIndicesAttr(Vt.IntArray([int(i) for i in F.ravel()]))
        nrm = mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, n)) for n in N]))
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        mesh.CreateDoubleSidedAttr(True)
        mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, V.min(0))), Gf.Vec3f(*map(float, V.max(0)))]))
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mats[mat])
        _ = nrm
    st.GetRootLayer().Save()
    if os.path.exists(path):
        os.remove(path)
    ok = UsdUtils.CreateNewARKitUsdzPackage(Sdf.AssetPath(usdc), path)
    shutil.rmtree(tmp, ignore_errors=True)
    return "" if ok and os.path.isfile(path) else "usdz packaging failed"


def model_view(name: str, parts: Parts, info: str) -> dict:
    """The rebuilt room for the offline page: light baked into vertex colours (the page draws unlit)."""
    V, C, F = [], [], []
    L = np.array([0.35, 0.85, 0.4]); L /= np.linalg.norm(L)
    for _, mat, v, n, f in parts.items:
        (r, g, b), a = MAT.get(mat, MAT["object"])
        if a < 1:   # glass: lighter, the page has no transparency
            r, g, b = (0.5 * r + 0.5, 0.5 * g + 0.5, 0.5 * b + 0.5)
        k = 0.62 + 0.38 * np.clip(n @ L, 0, 1)
        col = np.clip(np.stack([r * k, g * k, b * k], 1) * 255, 0, 255).astype(np.uint8)
        F.append(f + sum(len(x) for x in V)); V.append(v); C.append(col)
    return viewer_room(name, np.concatenate(V).astype(np.float32), np.concatenate(C), np.concatenate(F), info)


# ----------------------------------------------------------------------------------------------------------- viewer
VIEWER = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>__TITLE__</title>
<style>html,body{margin:0;height:100%;background:#f3efe9;font:14px -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#2a2620;overflow:hidden}
canvas{display:block;width:100%;height:100%;touch-action:none}
#bar{position:fixed;left:0;right:0;bottom:0;padding:10px 12px calc(10px + env(safe-area-inset-bottom));background:rgba(243,239,233,.92);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
#bar button,#bar select{border:1px solid #c9bfb2;background:#fff;border-radius:9px;padding:7px 11px;font:inherit}
#bar label{display:flex;align-items:center;gap:6px;flex:1;min-width:180px}#bar input{flex:1}
#info{position:fixed;left:12px;top:calc(10px + env(safe-area-inset-top));right:12px;font-size:13px;color:#6b6155}
#err{position:fixed;inset:0;display:none;align-items:center;justify-content:center;padding:30px;text-align:center}</style></head>
<body><canvas id="c"></canvas><div id="info"></div><div id="err"></div>
<div id="bar"><select id="room"></select><button id="top">俯视</button><button id="reset">重置</button>
<label>剖切<input id="cut" type="range" min="0" max="1000" value="880"></label></div>
<script>
const DATA=__DATA__;
const cv=document.getElementById('c'),gl=cv.getContext('webgl2',{antialias:true});
if(!gl){const e=document.getElementById('err');e.style.display='flex';e.textContent='这个浏览器不支持 WebGL2，换 Safari 或 Chrome 打开';throw 0}
function b64(s){const b=atob(s),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u}
const VS=`#version 300 es
in vec3 p;in vec3 c;uniform mat4 M;uniform vec3 lo;uniform vec3 sc;out vec3 vc;out float vy;
void main(){vec3 w=lo+p*sc;vy=w.y;vc=c;gl_Position=M*vec4(w,1.);}`;
const FS=`#version 300 es
precision highp float;in vec3 vc;in float vy;uniform float cut;out vec4 o;
void main(){if(vy>cut)discard;o=vec4(vc,1.);}`;
function sh(t,s){const x=gl.createShader(t);gl.shaderSource(x,s);gl.compileShader(x);return x}
const pr=gl.createProgram();gl.attachShader(pr,sh(gl.VERTEX_SHADER,VS));gl.attachShader(pr,sh(gl.FRAGMENT_SHADER,FS));
gl.bindAttribLocation(pr,0,'p');gl.bindAttribLocation(pr,1,'c');gl.linkProgram(pr);gl.useProgram(pr);
const U=n=>gl.getUniformLocation(pr,n);
let R=null;
function load(i){const r=DATA.rooms[i];const pos=b64(r.pos),col=b64(r.col),idx=b64(r.idx);
 const va=gl.createVertexArray();gl.bindVertexArray(va);
 let b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,pos,gl.STATIC_DRAW);
 gl.enableVertexAttribArray(0);gl.vertexAttribPointer(0,3,gl.UNSIGNED_SHORT,false,0,0);
 b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,col,gl.STATIC_DRAW);
 gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,3,gl.UNSIGNED_BYTE,true,0,0);
 b=gl.createBuffer();gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,b);gl.bufferData(gl.ELEMENT_ARRAY_BUFFER,idx,gl.STATIC_DRAW);
 R={va,n:idx.length/4,lo:r.lo,sc:r.sc,hi:r.hi,name:r.name,info:r.info};
 document.getElementById('info').textContent=r.name+' · '+r.info;reset();}
let yaw=-0.7,pitch=0.75,dist=8,tx=0,ty=0,tz=0;
function reset(){const lo=R.lo,hi=R.hi;tx=(lo[0]+hi[0])/2;ty=(lo[1]+hi[1])/2;tz=(lo[2]+hi[2])/2;
 const asp=cv.clientWidth/Math.max(1,cv.clientHeight);dist=Math.max(hi[0]-lo[0],hi[2]-lo[2],hi[1]-lo[1])*1.15/Math.min(1,asp)+1;yaw=-0.7;pitch=0.75;cutUpd();draw()}
function cutUpd(){const v=+document.getElementById('cut').value/1000;R.cut=R.lo[1]+(R.hi[1]-R.lo[1])*v+(v>=1?1:0);}
function mul(a,b){const o=new Float32Array(16);for(let i=0;i<4;i++)for(let j=0;j<4;j++){let s=0;for(let k=0;k<4;k++)s+=a[k*4+j]*b[i*4+k];o[i*4+j]=s}return o}
function draw(){const w=cv.clientWidth*devicePixelRatio,h=cv.clientHeight*devicePixelRatio;if(cv.width!==w||cv.height!==h){cv.width=w;cv.height=h}
 gl.viewport(0,0,w,h);gl.clearColor(.953,.937,.914,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);
 if(!R)return;const f=1/Math.tan(0.45),a=w/h,n=0.05,fr=500;
 const P=new Float32Array([f/a,0,0,0,0,f,0,0,0,0,(fr+n)/(n-fr),-1,0,0,2*fr*n/(n-fr),0]);
 const ex=tx+dist*Math.cos(pitch)*Math.sin(yaw),ey=ty+dist*Math.sin(pitch),ez=tz+dist*Math.cos(pitch)*Math.cos(yaw);
 let zx=ex-tx,zy=ey-ty,zz=ez-tz;let l=Math.hypot(zx,zy,zz);zx/=l;zy/=l;zz/=l;
 let xx=zz,xy=0,xz=-zx;l=Math.hypot(xx,xz)||1;xx/=l;xz/=l;const yx=zy*xz-zz*xy,yy=zz*xx-zx*xz,yz=zx*xy-zy*xx;
 const V=new Float32Array([xx,yx,zx,0,xy,yy,zy,0,xz,yz,zz,0,-(xx*ex+xy*ey+xz*ez),-(yx*ex+yy*ey+yz*ez),-(zx*ex+zy*ey+zz*ez),1]);
 gl.uniformMatrix4fv(U('M'),false,mul(P,V));gl.uniform3fv(U('lo'),R.lo);gl.uniform3fv(U('sc'),R.sc);gl.uniform1f(U('cut'),R.cut);
 gl.bindVertexArray(R.va);gl.drawElements(gl.TRIANGLES,R.n,gl.UNSIGNED_INT,0);}
const pts=new Map();let last=null;
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);pts.set(e.pointerId,[e.clientX,e.clientY]);last=null});
cv.addEventListener('pointerup',e=>{pts.delete(e.pointerId);last=null});cv.addEventListener('pointercancel',e=>{pts.delete(e.pointerId);last=null});
cv.addEventListener('pointermove',e=>{if(!pts.has(e.pointerId))return;const p=pts.get(e.pointerId);const dx=e.clientX-p[0],dy=e.clientY-p[1];pts.set(e.pointerId,[e.clientX,e.clientY]);
 if(pts.size===1&&!e.shiftKey){yaw-=dx*0.008;pitch=Math.min(1.55,Math.max(-0.2,pitch+dy*0.008))}
 else{const q=[...pts.values()];if(q.length>=2){const d=Math.hypot(q[0][0]-q[1][0],q[0][1]-q[1][1]);if(last)dist=Math.max(0.3,dist*last/d);last=d}
  const s=dist*0.0015/(pts.size||1);tx-=(Math.cos(yaw)*dx)*s;tz-=(-Math.sin(yaw)*dx)*s;ty+=dy*s}draw()});
cv.addEventListener('wheel',e=>{e.preventDefault();dist=Math.max(0.3,dist*Math.exp(e.deltaY*0.001));draw()},{passive:false});
document.getElementById('top').onclick=()=>{pitch=1.55;yaw=0;draw()};document.getElementById('reset').onclick=reset;
document.getElementById('cut').oninput=()=>{cutUpd();draw()};
const sel=document.getElementById('room');DATA.rooms.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.name;sel.appendChild(o)});
if(DATA.rooms.length<2)sel.style.display='none';sel.onchange=()=>load(+sel.value);
addEventListener('resize',draw);load(0);
</script></body></html>"""


def viewer_room(name: str, verts, cols, faces, info: str) -> dict:
    lo, hi = verts.min(0), verts.max(0)
    sc = np.maximum(hi - lo, 1e-6) / 65535.0
    q = np.round((verts - lo) / sc).clip(0, 65535).astype("<u2")
    return {"name": name, "info": info, "lo": lo.tolist(), "hi": hi.tolist(), "sc": sc.tolist(),
            "pos": base64.b64encode(q.tobytes()).decode(), "col": base64.b64encode(cols.astype(np.uint8).tobytes()).decode(),
            "idx": base64.b64encode(faces.astype("<u4").tobytes()).decode()}


# ------------------------------------------------------------------------------------------------------------ build
def dir_size(p: str) -> int:
    if os.path.isfile(p):
        return os.path.getsize(p)
    t = 0
    for d, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return t


def mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def stem_of(src: str) -> str:
    s = os.path.basename(os.path.normpath(src))
    s = re.sub(r"\.zip$", "", s, flags=re.I)
    s = re.sub(r"^\d{9,}-", "", s)   # aiw upload prefix <time>-
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", s) or "scan"


def cmd_build(a) -> int:
    name = stem_of(a.src)
    out = os.path.expanduser(a.out or f"~/workspace/scan3d/{name}")
    os.makedirs(out, exist_ok=True)
    root = unpack(a.src, out)
    ss = sessions(root)
    if not ss:
        print("NO_SCAN: no manifest.json / mesh/ / rooms/ inside: this is not a 3D Model Anything (house-capture) export")
        return 2
    rooms_v, models_v, summary = [], [], {"scan": name, "source": os.path.abspath(a.src), "unpacked": root, "sessions": []}
    md = [f"# {name}", ""]
    for s in ss:
        man = s["manifest"]
        sd = s["dir"]
        photos = len([f for f in os.listdir(os.path.join(sd, "photos")) if f.lower().endswith(".jpg")]) \
            if os.path.isdir(os.path.join(sd, "photos")) else 0
        sess = {"dir": sd, "format": man.get("format"), "photos": photos, "rooms": []}
        idxs = room_indices(sd)
        for i in idxs:
            rn = room_name(sd, man, i)
            rp = roomplan(os.path.join(sd, "rooms", f"room-{i}.json"))
            r = {"index": i, "name": rn, **rp}
            tag = f"{name}-room-{i}" if (len(idxs) > 1 or len(ss) > 1) else name
            parts = None if getattr(a, "no_model", False) else roomplan_model(os.path.join(sd, "rooms", f"room-{i}.json"))
            if parts and parts.items:
                mg = os.path.join(out, f"{tag}-model.glb")
                r["model_glb"] = mg
                r["model_glb_mb"] = round(write_model_glb(mg, parts) / 1048576, 2)
                r["model_parts"] = dict(parts.count)
                mu = os.path.join(out, f"{tag}-model.usdz")
                why = write_model_usdz(mu, parts)
                if why:
                    r["model_usdz_error"] = why
                else:
                    r["model_usdz"] = mu
                bits = [f"{rp['size_m'][0]}×{rp['size_m'][1]} m"] if rp.get("size_m") else []
                bits.append(f"{len(parts.items)} 个部件")
                models_v.append(model_view(f"{rn} · 建模", parts, " · ".join(bits)))
            apple = os.path.join(sd, "rooms", f"room-{i}.usdz")   # RoomPlan's own parametric export, when the app kept it
            if os.path.isfile(apple) and os.path.getsize(apple) > 0:
                r["roomplan_usdz"] = os.path.join(out, f"{tag}-roomplan.usdz")
                shutil.copyfile(apple, r["roomplan_usdz"])
            ply = os.path.join(sd, "mesh", f"room-{i}.ply")
            if os.path.isfile(ply):
                mesh = read_ply(ply)
                cols, how = vertex_colors(ply, mesh)
                faces = keep_room(mesh)
                v, c, f, cell = simplify(mesh["verts"], cols, faces, a.faces)
                glb = os.path.join(out, f"{tag}.glb")
                size = write_glb(glb, v, c, f, rn)
                r.update({"glb": glb, "glb_mb": round(size / 1048576, 1), "colors": how,
                          "mesh_in": [int(len(mesh["verts"])), int(len(mesh["faces"]))],
                          "mesh_out": [int(len(v)), int(len(f))], "cluster_cm": round(cell * 100, 1)})
                bits = []
                if rp.get("size_m"):
                    bits.append(f"{rp['size_m'][0]}×{rp['size_m'][1]} m")
                if rp.get("height_m"):
                    bits.append(f"高 {rp['height_m']} m")
                bits.append(f"{len(f)//1000}k 面")
                rooms_v.append(viewer_room(f"{rn} · 扫描" if not getattr(a, "no_model", False) else rn, v, c, f, " · ".join(bits)))
            elif not rp:
                continue
            sess["rooms"].append(r)
        summary["sessions"].append(sess)
    if not rooms_v:
        print("NO_MESH: the scan has no mesh/room-N.ply (RoomPlan only)"
              + ("; the rebuilt model was still written" if models_v else "; only the summary was written"))
    if rooms_v or models_v:
        html = VIEWER.replace("__TITLE__", f"{name} · 3D").replace("__DATA__", json.dumps({"rooms": models_v + rooms_v}, ensure_ascii=False))
        hp = os.path.join(out, f"{name}-3d.html")
        with open(hp, "w", encoding="utf-8") as f:
            f.write(html)
        summary["html"] = hp
        summary["html_mb"] = round(os.path.getsize(hp) / 1048576, 1)
    summary["unpacked_mb"] = round(dir_size(root) / 1048576, 1)
    for s in summary["sessions"]:
        for r in s["rooms"]:
            md.append(f"## {r['name']}")
            if r.get("size_m"):
                md.append(f"- 尺寸（墙体外包）：{r['size_m'][0]} × {r['size_m'][1]} m，层高约 {r.get('height_m', '?')} m"
                          + (f"，地面 {r['floor_area_m2']} m²" if r.get("floor_area_m2") else ""))
            if "walls" in r:
                md.append(f"- 墙 {r['walls']} 面，门 {r['doors']}，窗 {r['windows']}，洞口 {r['openings']}")
            if r.get("objects"):
                cnt: dict = {}
                for o in r["objects"]:
                    k = OBJ_ZH.get(o["category"], o["category"])
                    cnt[k] = cnt.get(k, 0) + 1
                md.append("- 识别到的家具：" + "、".join(f"{k}×{v}" if v > 1 else k for k, v in cnt.items()))
            if r.get("model_parts"):
                md.append("- 建模：" + "、".join(f"{OBJ_ZH.get(k.lower(), PART_ZH.get(k, k))}×{v}" for k, v in r["model_parts"].items())
                          + f"，GLB {r['model_glb_mb']} MB" + ("，USDZ（iPhone 点开可看、可 AR）" if r.get("model_usdz")
                                                             else f"，没有 USDZ：{r.get('model_usdz_error')}"))
            if r.get("glb"):
                md.append(f"- 网格：{r['mesh_in'][1]:,} → {r['mesh_out'][1]:,} 面（聚类 {r['cluster_cm']} cm），GLB {r['glb_mb']} MB，"
                          + ("照片颜色" if r["colors"] == "photo" else "没有照片颜色缓存，按墙/地/家具上色（在 App 里打开一次 3D 预览再分享就有颜色）"))
            md.append("")
        md.append(f"照片 {s['photos']} 张。")
    md.append(f"\n解压后 {summary['unpacked_mb']} MB。")
    with open(os.path.join(out, f"{name}-summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    with open(os.path.join(out, f"{name}-summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: summary[k] for k in summary if k != "sessions"}, ensure_ascii=False))
    for s in summary["sessions"]:
        for r in s["rooms"]:
            print(f"ROOM {r['name']}: " + ", ".join(f"{k}={r[k]}" for k in ("size_m", "height_m", "walls", "doors", "windows",
                                                                              "glb_mb", "mesh_out", "colors", "model_parts", "model_usdz_error") if k in r))
    rs = [r for s in summary["sessions"] for r in s["rooms"]]
    print("SEND:", " ".join(p for p in [summary.get("html")] + [r.get("model_usdz") for r in rs] + [r.get("roomplan_usdz") for r in rs] + [r.get("model_glb") for r in rs]
                            + [r.get("glb") for r in rs]
                            + [os.path.join(out, f"{name}-summary.md")] if p))
    return 0


# ------------------------------------------------------------------------------------------------------------- slim
def cmd_slim(a) -> int:
    root = os.path.expanduser(a.folder)
    if zipfile.is_zipfile(root):
        sys.exit("slim works on the unzipped folder (build prints it as \"unpacked\")")
    before = dir_size(root)
    parts = {"photos": 0, "depth": 0, "worldmap": 0}
    try:
        from PIL import Image
    except ImportError:
        Image = None
        print("Pillow missing: photos kept as they are (python3 -m pip install --user pillow)")
    for d, dirs, fs in os.walk(root):
        base = os.path.basename(d)
        if base == "depth" and not a.keep_depth:
            parts["depth"] += dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
            dirs[:] = []
            continue
        for f in fs:
            p = os.path.join(d, f)
            if f.endswith(".arworldmap"):
                parts["worldmap"] += os.path.getsize(p)
                os.remove(p)
            elif Image and base == "photos" and f.lower().endswith((".jpg", ".jpeg")):
                old = os.path.getsize(p)
                try:
                    im = Image.open(p)
                    exif = im.info.get("exif")
                    tmp = p + ".tmp"
                    kw = {"quality": a.quality, "optimize": True}
                    if exif:
                        kw["exif"] = exif
                    im.save(tmp, "JPEG", **kw)
                    if os.path.getsize(tmp) < old:
                        os.replace(tmp, p)
                        parts["photos"] += old - os.path.getsize(p)
                    else:
                        os.remove(tmp)
                except Exception as e:  # noqa: BLE001
                    print(f"skip {f}: {e}")
    after = dir_size(root)
    zp = os.path.normpath(root) + "-slim.zip"
    if os.path.basename(os.path.normpath(root)) == "src":
        zp = os.path.join(os.path.dirname(os.path.normpath(root)), "scan-slim.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:   # jpg/ply barely compress; stored is fast
        for d, _, fs in os.walk(root):
            for f in fs:
                p = os.path.join(d, f)
                z.write(p, os.path.relpath(p, root))
    print(f"SLIM before {mb(before)} -> after {mb(after)}  (photos -{mb(parts['photos'])}, depth -{mb(parts['depth'])}, "
          f"worldmap -{mb(parts['worldmap'])})  zip {mb(os.path.getsize(zp))}: {zp}")
    return 0


def cmd_info(a) -> int:
    src = a.src
    if zipfile.is_zipfile(src):
        parts: dict = {}
        with zipfile.ZipFile(src) as z:
            for i in z.infolist():
                seg = [s for s in i.filename.split("/") if s]
                key = next((s for s in seg if s in ("photos", "depth", "mesh", "rooms")), None) or \
                      ("worldmap" if i.filename.endswith(".arworldmap") else "other")
                parts[key] = parts.get(key, 0) + i.file_size
        print(f"zip {mb(os.path.getsize(src))}: " + ", ".join(f"{k} {mb(v)}" for k, v in sorted(parts.items(), key=lambda x: -x[1])))
        return 0
    for s in sessions(src):
        print(s["dir"], s["manifest"].get("format"), "rooms", room_indices(s["dir"]),
              ", ".join(f"{k} {mb(dir_size(os.path.join(s['dir'], k)))}" for k in ("photos", "depth", "mesh", "rooms")))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("src")
    b.add_argument("--out")
    b.add_argument("--faces", type=int, default=300000, help="most triangles per room (default 300000)")
    b.add_argument("--no-model", action="store_true", help="skip the rebuilt RoomPlan model (walls, doors, furniture)")
    s = sub.add_parser("slim")
    s.add_argument("folder")
    s.add_argument("--keep-depth", action="store_true")
    s.add_argument("--quality", type=int, default=82)
    i = sub.add_parser("info")
    i.add_argument("src")
    a = ap.parse_args()
    return {"build": cmd_build, "slim": cmd_slim, "info": cmd_info}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
