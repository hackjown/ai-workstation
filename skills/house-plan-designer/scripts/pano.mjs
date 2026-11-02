/**
 * 3.9.13 全景漫游：打开生成的设计器 HTML（无头 Chrome），每个房间站在中间拍一张 360° 全景（等距柱状 2:1），
 * 按门洞算出房间之间的跳转热点，再拍一张 2D 平面做小地图。输出 scenes.json + scene-*.jpg + plan.png。
 *
 *   node pano.mjs 设计器.html 输出目录 [--size 3072] [--eye 1500] [--min-area 2]
 *
 * scenes.json: {name, plan:{image, x0, y0, s}, scenes:[{id, name, x, y, image, yaw0, links:[{to, yaw, pitch, label}]}]}
 *   坐标 mm（和设计器一样，y 向下）；yaw 弧度，0 = +x（图上向右），π/2 = +y（图上向下）；pitch 向上为正。
 * aiw_hpd.py pano 再把它和 assets/pano-viewer.html 拼成离线漫游网页。
 */
import puppeteer from 'puppeteer-core';
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const [file, outArg] = argv.filter((a, i) => !a.startsWith('--') && !(i > 0 && argv[i - 1].startsWith('--')));
if (!file || !outArg) { console.error('用法：node pano.mjs 设计器.html 输出目录 [--size 3072] [--eye 1500] [--min-area 2]'); process.exit(1); }
const W = Math.max(1024, Math.min(8192, parseInt(opt('--size', '3072'), 10) || 3072));
const EYE = parseInt(opt('--eye', '1500'), 10) || 1500;
const MIN_AREA = parseFloat(opt('--min-area', '2')) || 2;
const out = path.resolve(outArg); fs.mkdirSync(out, { recursive: true });

const exe = [process.env.CHROME_PATH, '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
  '/snap/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean).find(p => fs.existsSync(p));
if (!exe) { console.error('找不到 Chrome/Chromium，设置 CHROME_PATH'); process.exit(1); }
const browser = await puppeteer.launch({ executablePath: exe, headless: true, timeout: 120000, protocolTimeout: 600000,
  defaultViewport: { width: 1280, height: 900 },
  args: [...(process.getuid && process.getuid() === 0 ? ['--no-sandbox'] : ['--no-sandbox']), '--enable-unsafe-swiftshader', '--use-angle=swiftshader', '--hide-scrollbars'] });
const t0 = Date.now();
try {
  const page = await browser.newPage();
  page.setDefaultTimeout(600000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(pathToFileURL(path.resolve(file)).href + '?debug', { waitUntil: 'load' });
  await page.waitForFunction(() => window.__designer, { timeout: 120000 });
  await new Promise(r => setTimeout(r, 800));

  // ---- rooms, doors, links (pure geometry on the designer's own plan)
  const layout = await page.evaluate((MIN_AREA) => {
    const D = window.__designer, app = D.app, plan = app.plan;
    const NN = new Map(plan.nodes.map(n => [n.id, n])), WW = new Map(plan.walls.map(w => [w.id, w]));
    const inReg = (r, x, y) => r.rects.some(q => x >= q.x && x <= q.x + q.w && y >= q.y && y <= q.y + q.h);
    const regs = app.rooms.regions.filter(r => r.area >= MIN_AREA || (r.labels.length && r.area >= 1.2));
    const used = new Map();
    const scenes = regs.map((r, i) => {
      let name = r.labels.map(l => l.name).filter(Boolean).join(' · ') || `房间 ${i + 1}`;
      used.set(name, (used.get(name) || 0) + 1);
      if (used.get(name) > 1) name += ' ' + used.get(name);
      // stand where the room is widest: the interior point, pushed clear of walls by the designer's own collision
      let p = { x: r.interior.x, y: r.interior.y };
      try { const c = D.S3.collide(p.x, p.y); if (c && inReg(r, c.x, c.y)) p = { x: c.x, y: c.y }; } catch (e) { /* keep */ }
      return { id: 's' + i, name, x: Math.round(p.x), y: Math.round(p.y), area: Math.round(r.area * 10) / 10, reg: i, links: [] };
    });
    const regOf = (x, y) => { for (let i = 0; i < regs.length; i++) if (inReg(regs[i], x, y)) return i; return -1; };
    const doors = [];
    for (const o of plan.openings) {
      if ((o.sill || 0) >= 300 || o.type === 'window') continue;
      const w = WW.get(o.wall); if (!w || w.status === 'removed') continue;
      const a = NN.get(w.a), b = NN.get(w.b); if (!a || !b) continue;
      const L = Math.hypot(b.x - a.x, b.y - a.y) || 1, ux = (b.x - a.x) / L, uy = (b.y - a.y) / L, nx = -uy, ny = ux;
      const c = { x: a.x + ux * o.pos, y: a.y + uy * o.pos };
      let s1 = -1, s2 = -1;
      for (const d of [250, 400, 600, 900]) {
        if (s1 < 0) s1 = regOf(c.x + nx * d, c.y + ny * d);
        if (s2 < 0) s2 = regOf(c.x - nx * d, c.y - ny * d);
      }
      doors.push({ id: o.id, type: o.type, x: Math.round(c.x), y: Math.round(c.y), a: s1, b: s2 });
    }
    const byReg = new Map(scenes.map(s => [s.reg, s]));
    const link = (from, to, via) => {
      if (!from || !to || from === to || from.links.some(l => l.to === to.id)) return;
      const tx = via ? via.x : to.x, ty = via ? via.y : to.y;
      from.links.push({ to: to.id, label: to.name, yaw: Math.atan2(ty - from.y, tx - from.x), dist: Math.round(Math.hypot(tx - from.x, ty - from.y)) });
    };
    for (const d of doors) { const A = byReg.get(d.a), B = byReg.get(d.b); if (A && B) { link(A, B, d); link(B, A, d); } }
    // open plan (客餐厅 with no door between): a straight line from one standing point to the other that crosses no wall
    const segs = plan.walls.filter(w => w.status !== 'removed').map(w => [NN.get(w.a), NN.get(w.b)]).filter(q => q[0] && q[1]);
    const cross = (p1, p2, p3, p4) => { const d = (a, b, c) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
      const d1 = d(p3, p4, p1), d2 = d(p3, p4, p2), d3 = d(p1, p2, p3), d4 = d(p1, p2, p4);
      return ((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0)) && d1 && d2 && d3 && d4; };
    for (let i = 0; i < scenes.length; i++) for (let j = i + 1; j < scenes.length; j++) {
      const A = scenes[i], B = scenes[j]; if (Math.hypot(A.x - B.x, A.y - B.y) > 9000) continue;
      if (!segs.some(q => cross(A, B, q[0], q[1]))) { link(A, B); link(B, A); }
    }
    // open passages: two rooms whose cells share an edge with ≥ 600 mm of it not covered by any wall (客餐厅、过道口)
    const wallsT = plan.walls.filter(w => w.status !== 'removed').map(w => ({ a: NN.get(w.a), b: NN.get(w.b), t: w.t || 120 })).filter(w => w.a && w.b);
    const onWall = (x, y) => wallsT.some(w => { const dx = w.b.x - w.a.x, dy = w.b.y - w.a.y, L2 = dx * dx + dy * dy || 1;
      const k = Math.max(0, Math.min(1, ((x - w.a.x) * dx + (y - w.a.y) * dy) / L2));
      return Math.hypot(x - w.a.x - k * dx, y - w.a.y - k * dy) < w.t / 2 + 40; });
    const gapOn = (pts) => { let run = [], best = []; for (const q of pts) { if (onWall(q.x, q.y)) { if (run.length > best.length) best = run; run = []; } else run.push(q); }
      if (run.length > best.length) best = run; return best; };
    for (let i = 0; i < scenes.length; i++) for (let j = i + 1; j < scenes.length; j++) {
      const A = scenes[i], B = scenes[j], RA = regs[A.reg], RB = regs[B.reg]; let gap = [];
      for (const p of RA.rects) for (const q of RB.rects) {
        const pts = [];
        for (const [e1, e2, vert] of [[p.x + p.w, q.x, 1], [q.x + q.w, p.x, 1], [p.y + p.h, q.y, 0], [q.y + q.h, p.y, 0]]) {
          if (Math.abs(e1 - e2) > 80) continue;
          const lo = vert ? Math.max(p.y, q.y) : Math.max(p.x, q.x), hi = vert ? Math.min(p.y + p.h, q.y + q.h) : Math.min(p.x + p.w, q.x + q.w);
          const c = (e1 + e2) / 2;
          for (let t = lo + 50; t < hi - 50; t += 100) pts.push(vert ? { x: c, y: t } : { x: t, y: c });
        }
        const g = gapOn(pts); if (g.length > gap.length) gap = g;
      }
      if (gap.length >= 6) { const m = gap[gap.length >> 1]; link(A, B, m); link(B, A, m); }
    }
    // every room reachable: join each separate group to its nearest room in another group
    const comp = () => { const c = new Map(); let k = 0; for (const s of scenes) { if (c.has(s.id)) continue; const st = [s]; c.set(s.id, k);
      while (st.length) { const t = st.pop(); for (const l of t.links) if (!c.has(l.to)) { c.set(l.to, k); st.push(scenes.find(z => z.id === l.to)); } } k++; } return { c, k }; };
    for (let guard = 0; guard < scenes.length; guard++) {
      const { c, k } = comp(); if (k <= 1) break;
      let best = null;
      for (const A of scenes) for (const B of scenes) if (c.get(A.id) === 0 && c.get(B.id) !== 0) {
        const d = Math.hypot(A.x - B.x, A.y - B.y); if (!best || d < best.d) best = { A, B, d }; }
      link(best.A, best.B); link(best.B, best.A);
    }
    // the first scene: the biggest room with 客 / 厅 in its name, else the biggest
    const first = scenes.filter(s => /客|厅/.test(s.name)).sort((p, q) => q.area - p.area)[0] || scenes.slice().sort((p, q) => q.area - p.area)[0];
    const xs = plan.nodes.map(n => n.x), ys = plan.nodes.map(n => n.y);
    const bb = { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
    return { name: plan.name, height: plan.height, scenes, doors, first: first && first.id, bb };
  }, MIN_AREA);
  if (!layout.scenes.length) throw new Error('没有找到房间（regions 为空）');

  // ---- minimap: the designer's own 2D drawing, the whole plan fitted in 1400 x 1000
  const pad = 600, bw = layout.bb.x1 - layout.bb.x0 + 2 * pad, bh = layout.bb.y1 - layout.bb.y0 + 2 * pad;
  const MW = Math.round(Math.max(500, Math.min(1400, 1000 * bw / bh))), MH = Math.round(Math.max(500, Math.min(1400, MW * bh / bw)));
  const s = Math.min(MW / bw, MH / bh), cx = (layout.bb.x0 + layout.bb.x1) / 2, cy = (layout.bb.y0 + layout.bb.y1) / 2;
  const planPng = await page.evaluate((cx, cy, s, MW, MH) => window.__designer.shot2d('plan', cx, cy, s, MW, MH), cx, cy, s, MW, MH);
  fs.writeFileSync(path.join(out, 'plan.png'), Buffer.from(planPng.split(',')[1], 'base64'));
  const planInfo = { image: 'plan.png', w: MW, h: MH, x0: cx - MW / 2 / s, y0: cy - MH / 2 / s, s };

  // ---- 3D: walk mode (ceilings on), then six 90° views per scene, resampled to one equirectangular picture
  await page.evaluate(() => window.__designer.setView('3d'));
  await page.waitForFunction(() => window.__designer.S3.ready, { timeout: 180000 });
  await new Promise(r => setTimeout(r, 2500));
  await page.evaluate((x, y) => window.__designer.S3.enterFP({ x, y }), layout.scenes[0].x, layout.scenes[0].y);
  await new Promise(r => setTimeout(r, 3500));
  for (const sc of layout.scenes) {
    const t1 = Date.now();
    const jpg = await page.evaluate(async (sc, W, EYE) => {
      const D = window.__designer, S = D.S3, T = S.three(), R = T.renderer, scene = T.scene;
      S.fp.x = sc.x; S.fp.y = sc.y;
      if (T.ceilG) T.ceilG.visible = true;
      await new Promise(r => setTimeout(r, 120));
      const floorZ = S.fp.z || 0;
      const N = Math.max(512, Math.round(W / 3));
      const cam = new T.camera.constructor(90, 1, 0.03, 300);
      const V = T.camera.position.constructor;
      const pos = new V(sc.x / 1000, (floorZ + EYE) / 1000, sc.y / 1000);
      const oldPR = R.getPixelRatio();
      const canvas = R.domElement, oldW = canvas.width, oldH = canvas.height, oldCssW = canvas.style.width, oldCssH = canvas.style.height;
      R.setPixelRatio(1); R.setSize(N, N, false);
      const gl = R.getContext();
      const dirs = [[1, 0, 0, 0, 1, 0], [-1, 0, 0, 0, 1, 0], [0, 0, 1, 0, 1, 0], [0, 0, -1, 0, 1, 0], [0, 1, 0, 0, 0, -1], [0, -1, 0, 0, 0, 1]];
      const faces = [];
      for (const d of dirs) {
        cam.position.copy(pos); cam.up.set(d[3], d[4], d[5]);
        cam.lookAt(pos.x + d[0], pos.y + d[1], pos.z + d[2]);
        cam.updateMatrixWorld(true); cam.updateProjectionMatrix();
        R.setRenderTarget(null); R.render(scene, cam);
        const buf = new Uint8Array(N * N * 4); gl.readPixels(0, 0, N, N, gl.RGBA, gl.UNSIGNED_BYTE, buf);
        faces.push({ buf, inv: cam.matrixWorldInverse.elements.slice(), proj: cam.projectionMatrix.elements.slice() });
      }
      R.setPixelRatio(oldPR); R.setSize(oldW / oldPR, oldH / oldPR, false); canvas.style.width = oldCssW; canvas.style.height = oldCssH;
      // equirectangular: u -> yaw (-π..π, 0 = +x), v -> pitch (π/2..-π/2); three: plan x -> +x, plan y -> +z, up -> +y
      const H = W / 2, img = new ImageData(W, H), px = img.data;
      const face = (x, y, z) => { const ax = Math.abs(x), ay = Math.abs(y), az = Math.abs(z);
        if (ax >= ay && ax >= az) return x > 0 ? 0 : 1; if (az >= ay) return z > 0 ? 2 : 3; return y > 0 ? 4 : 5; };
      for (let v = 0; v < H; v++) {
        const pitch = Math.PI / 2 - (v + 0.5) / H * Math.PI, cp = Math.cos(pitch), sp = Math.sin(pitch);
        for (let u = 0; u < W; u++) {
          const yaw = (u + 0.5) / W * 2 * Math.PI - Math.PI;
          const dx = cp * Math.cos(yaw), dy = sp, dz = cp * Math.sin(yaw);
          const f = faces[face(dx, dy, dz)], m = f.inv, p = f.proj;
          // view space (direction only), then clip space
          const vx = m[0] * dx + m[4] * dy + m[8] * dz, vy = m[1] * dx + m[5] * dy + m[9] * dz, vz = m[2] * dx + m[6] * dy + m[10] * dz;
          const cx = p[0] * vx + p[4] * vy + p[8] * vz, cy = p[1] * vx + p[5] * vy + p[9] * vz, cw = p[3] * vx + p[7] * vy + p[11] * vz;
          const nx = cx / cw, ny = cy / cw;
          const ix = Math.min(N - 1, Math.max(0, Math.floor((nx * 0.5 + 0.5) * N))), iy = Math.min(N - 1, Math.max(0, Math.floor((ny * 0.5 + 0.5) * N)));
          const si = (iy * N + ix) * 4, di = (v * W + u) * 4;   // readPixels rows go bottom-up, as clip y does
          px[di] = f.buf[si]; px[di + 1] = f.buf[si + 1]; px[di + 2] = f.buf[si + 2]; px[di + 3] = 255;
        }
      }
      const c2 = document.createElement('canvas'); c2.width = W; c2.height = H; c2.getContext('2d').putImageData(img, 0, 0);
      return c2.toDataURL('image/jpeg', 0.85);
    }, sc, W, EYE);
    sc.image = `scene-${sc.id}.jpg`;
    fs.writeFileSync(path.join(out, sc.image), Buffer.from(jpg.split(',')[1], 'base64'));
    console.log(`scene ${sc.id} ${sc.name}: ${((Date.now() - t1) / 1000).toFixed(1)}s, ${sc.links.length} links`);
  }
  const doc = { name: layout.name, first: layout.first, eye: EYE, plan: planInfo,
    scenes: layout.scenes.map(({ reg, ...s }) => ({ ...s, yaw0: s.links.length ? s.links[0].yaw : 0 })), doors: layout.doors };
  fs.writeFileSync(path.join(out, 'scenes.json'), JSON.stringify(doc, null, 1));
  console.log('PANO_SCENES ' + JSON.stringify({ ok: true, out, scenes: doc.scenes.length, size: [W, W / 2], seconds: Math.round((Date.now() - t0) / 1000),
    errors: errors.slice(0, 3), unlinked: doc.scenes.filter(s => !s.links.length).map(s => s.name) }));
} catch (e) {
  console.log('PANO_SCENES ' + JSON.stringify({ ok: false, error: String(e && e.message || e).slice(0, 400) }));
  process.exitCode = 1;
} finally { await browser.close(); }
