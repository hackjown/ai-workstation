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


OBJ_ZH = {"storage": "柜子", "refrigerator": "冰箱", "stove": "灶台", "bed": "床", "sink": "水槽", "washerDryer": "洗衣机",
          "toilet": "马桶", "bathtub": "浴缸", "oven": "烤箱", "dishwasher": "洗碗机", "table": "桌子", "sofa": "沙发",
          "chair": "椅子", "fireplace": "壁炉", "television": "电视", "stairs": "楼梯"}


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
    rooms_v, summary = [], {"scan": name, "source": os.path.abspath(a.src), "unpacked": root, "sessions": []}
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
            ply = os.path.join(sd, "mesh", f"room-{i}.ply")
            if os.path.isfile(ply):
                mesh = read_ply(ply)
                cols, how = vertex_colors(ply, mesh)
                faces = keep_room(mesh)
                v, c, f, cell = simplify(mesh["verts"], cols, faces, a.faces)
                tag = f"{name}-room-{i}" if (len(idxs) > 1 or len(ss) > 1) else name
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
                rooms_v.append(viewer_room(rn, v, c, f, " · ".join(bits)))
            elif not rp:
                continue
            sess["rooms"].append(r)
        summary["sessions"].append(sess)
    if not rooms_v:
        print("NO_MESH: the scan has no mesh/room-N.ply (RoomPlan only); only the summary was written")
    else:
        html = VIEWER.replace("__TITLE__", f"{name} · 3D").replace("__DATA__", json.dumps({"rooms": rooms_v}))
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
                                                                              "glb_mb", "mesh_out", "colors") if k in r))
    print("SEND:", " ".join(p for p in [summary.get("html")] + [r.get("glb") for s in summary["sessions"] for r in s["rooms"]]
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
