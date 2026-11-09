/* v3.9.31 视频通话 (omni realtime voice + video call, like 豆包): a full-screen call view over /ws/omni-call.
   Microphone -> AudioWorklet (ScriptProcessor where there is none) -> 16 kHz pcm16 chunks of 40 ms; camera -> canvas
   -> JPEG (quality 0.6, long edge ≤720) at most once a second and only when the picture changed (or every 5 s);
   the model's 24 kHz pcm16 plays through a scheduled AudioBuffer queue, cut at once on speech_started / 打断.
   echoCancellation is asked for in getUserMedia so the model does not hear itself. Server messages: ready (mode
   badge 实时 / 轮流), audio, transcript (live captions; speak=true -> read out with speechSynthesis), speech_started,
   tool, error, ended. The transcript is saved to the 小秘 chat by the server at hang-up. */
const CALL = { ws: null, ctx: null, mic: null, cam: null, node: null, src: null, sources: new Set(), next: 0,
  muted: false, video: true, facing: "user", frameT: null, pingT: null, clockT: null, t0: 0, ended: false,
  lastThumb: null, lastSent: 0, caps: { user: "", assistant: "" }, mode: "", wake: null };

Object.assign(IC, {
  phone: "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z",
  hangup: "M10.7 13.3a16 16 0 0 0 3.4 2.6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 3.1 4.2 2 2 0 0 1 5.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L9 9.8M23 1 1 23",
  micoff: "M1 1l22 22M9 9v3a3 3 0 0 0 5.1 2.1M15 9.3V5a3 3 0 0 0-5.9-.6M17 16.9A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.1 1.2M12 19v3",
  camoff: "M1 1l22 22M21 21H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3m3-3h6l2 3h4a2 2 0 0 1 2 2v9.3M14.1 14.1a3 3 0 1 1-4.2-4.2",
  flip: "M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5",
});
document.querySelectorAll("#hsheet [data-hi], #callBtn [data-hi]").forEach((el) => { el.innerHTML = icon(el.dataset.hi); });

const CALL_WORKLET = `class P extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor("aiw-mic",P);`;

async function openCall(video = true) {
  if (CALL.ws) return;
  let st;
  try { st = await api("GET", "/api/omni/status"); } catch (e) { return toast("读不到通话设置：" + e.message); }
  if (!st.enabled) {
    return modal(`<div class="callnote"><h3>${icon("phone")} 视频通话还没开</h3><div class="muted">${esc(st.message || "先设置通话模型")}</div>
      <div class="row gap"><button class="btn primary" onclick="closeOverlays();go('settings');setTimeout(()=>{const el=document.getElementById('g-call');if(el)el.scrollIntoView({behavior:'smooth'})},400)">去设置</button><button class="btn" onclick="closeOverlays()">取消</button></div></div>`);
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return toast("这个浏览器不能用麦克风（需要 HTTPS 或 localhost）");
  CALL.video = video; CALL.ended = false; CALL.muted = false; CALL.facing = "user"; CALL.caps = { user: "", assistant: "" };
  callView(st);
  try {
    CALL.mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch (e) { callEnd("没有麦克风权限：" + (e.message || e.name)); return; }
  if (video) await callCamera(true);
  try { await callAudio(); } catch (e) { callEnd("麦克风打不开：" + e.message); return; }
  callConnect(st);
  try { CALL.wake = await navigator.wakeLock.request("screen"); } catch (e) { /* not supported */ }
}

function callView(st) {
  let el = $("callView");
  if (!el) { el = document.createElement("div"); el.id = "callView"; document.body.appendChild(el); }
  el.innerHTML = `<video id="callVideo" playsinline muted autoplay></video>
    <div class="call-orb" id="callOrb"><span class="mate" id="callMate" role="img" aria-label="球球"></span></div>
    <div class="call-top"><span class="call-badge" id="callBadge">连接中…</span><span class="call-clock" id="callClock">00:00</span></div>
    <div class="call-caps" id="callCaps" onclick="callInterrupt()" title="点一下打断"><div class="cap-u" id="capU"></div><div class="cap-a" id="capA">正在接通…</div></div>
    <div class="call-bar">
      <button class="cbtn" id="callMute" onclick="callMute()" aria-label="静音">${icon("mic")}</button>
      <button class="cbtn" id="callCam" onclick="callCamToggle()" aria-label="摄像头">${icon(CALL.video ? "video" : "camoff")}</button>
      <button class="cbtn" id="callFlip" onclick="callFlip()" aria-label="切换前后摄像头" hidden>${icon("flip")}</button>
      <button class="cbtn end" onclick="callHangup()" aria-label="挂断">${icon("hangup")}</button>
    </div>`;
  el.classList.add("on");
  document.body.classList.add("in-call");
  try { if (window.AiwMate && AiwMate.create) { CALL.mate = AiwMate.create($("callMate"), { eyeScale: 1.7 }); CALL.mate.set("02"); } } catch (e) { CALL.mate = null; }
  CALL.t0 = Date.now();
  CALL.clockT = setInterval(() => { const s = Math.floor((Date.now() - CALL.t0) / 1000); const c = $("callClock"); if (c) c.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; }, 1000);
  if (navigator.mediaDevices.enumerateDevices) navigator.mediaDevices.enumerateDevices().then((d) => { if (d.filter((x) => x.kind === "videoinput").length > 1) { const b = $("callFlip"); if (b) b.hidden = false; } }).catch(() => {});
}

async function callCamera(on) {
  if (CALL.cam) { CALL.cam.getTracks().forEach((t) => t.stop()); CALL.cam = null; }
  clearInterval(CALL.frameT); CALL.frameT = null;
  const v = $("callVideo");
  if (!on) { if (v) v.srcObject = null; $("callView").classList.remove("cam"); return; }
  try {
    CALL.cam = await navigator.mediaDevices.getUserMedia({ video: { facingMode: CALL.facing, width: { ideal: 1280 }, height: { ideal: 720 } } });
  } catch (e) { toast("摄像头打不开，先用语音：" + (e.message || e.name)); CALL.video = false; return; }
  v.srcObject = CALL.cam; v.classList.toggle("mirror", CALL.facing === "user");
  $("callView").classList.add("cam");
  CALL.lastThumb = null; CALL.lastSent = 0;
  CALL.frameT = setInterval(callFrame, 1000);
}

function callFrame() {
  const v = $("callVideo");
  if (!CALL.ws || CALL.ws.readyState !== 1 || !v || !v.videoWidth) return;
  // cheap change check on a 16x9 grey thumbnail: a still picture goes out every 5 s only
  const th = document.createElement("canvas"); th.width = 16; th.height = 9;
  const tc = th.getContext("2d"); tc.drawImage(v, 0, 0, 16, 9);
  const px = tc.getImageData(0, 0, 16, 9).data; const g = [];
  for (let i = 0; i < px.length; i += 4) g.push((px[i] + px[i + 1] + px[i + 2]) / 3);
  const diff = CALL.lastThumb ? g.reduce((s, x, i) => s + Math.abs(x - CALL.lastThumb[i]), 0) / g.length : 999;
  if (diff < 6 && Date.now() - CALL.lastSent < 5000) return;
  CALL.lastThumb = g;
  const k = Math.min(1, 720 / Math.max(v.videoWidth, v.videoHeight));
  const c = document.createElement("canvas"); c.width = Math.round(v.videoWidth * k); c.height = Math.round(v.videoHeight * k);
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
  c.toBlob((b) => {
    if (!b || !CALL.ws || CALL.ws.readyState !== 1) return;
    const r = new FileReader();
    r.onload = () => { const d = String(r.result).split(",")[1]; if (d && d.length < 250000) { CALL.ws.send(JSON.stringify({ type: "frame", data: d })); CALL.lastSent = Date.now(); } };
    r.readAsDataURL(b);
  }, "image/jpeg", 0.6);
}

async function callAudio() {
  const AC = window.AudioContext || window.webkitAudioContext;
  CALL.ctx = new AC();
  if (CALL.ctx.state === "suspended") await CALL.ctx.resume();
  CALL.src = CALL.ctx.createMediaStreamSource(CALL.mic);
  const rate = CALL.ctx.sampleRate; const ratio = rate / 16000;
  let acc = new Float32Array(0);
  const push = (f32) => {   // downsample by averaging to 16 kHz, send 40 ms (640 samples) at a time
    const n = Math.floor(f32.length / ratio); const out = new Float32Array(n);
    for (let i = 0; i < n; i++) { const a = Math.floor(i * ratio), b = Math.min(f32.length, Math.floor((i + 1) * ratio)); let s = 0; for (let j = a; j < b; j++) s += f32[j]; out[i] = s / Math.max(1, b - a); }
    const m = new Float32Array(acc.length + n); m.set(acc); m.set(out, acc.length); acc = m;
    while (acc.length >= 640) { callSendPcm(acc.subarray(0, 640)); acc = acc.slice(640); }
  };
  if (CALL.ctx.audioWorklet) {
    const url = URL.createObjectURL(new Blob([CALL_WORKLET], { type: "application/javascript" }));
    await CALL.ctx.audioWorklet.addModule(url); URL.revokeObjectURL(url);
    CALL.node = new AudioWorkletNode(CALL.ctx, "aiw-mic");
    CALL.node.port.onmessage = (e) => push(e.data);
  } else {
    CALL.node = CALL.ctx.createScriptProcessor(2048, 1, 1);
    CALL.node.onaudioprocess = (e) => push(e.inputBuffer.getChannelData(0));
  }
  const sink = CALL.ctx.createGain(); sink.gain.value = 0;   // keep the graph pulling without playing the mic back
  CALL.src.connect(CALL.node); CALL.node.connect(sink); sink.connect(CALL.ctx.destination);
}

function callSendPcm(f32) {
  if (CALL.muted || !CALL.ws || CALL.ws.readyState !== 1) return;
  const b = new Uint8Array(f32.length * 2); const dv = new DataView(b.buffer);
  for (let i = 0; i < f32.length; i++) { const s = Math.max(-1, Math.min(1, f32[i])); dv.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  let bin = ""; for (let i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
  CALL.ws.send(JSON.stringify({ type: "audio", data: btoa(bin) }));
}

function callConnect(st) {
  const ws = new WebSocket(wsUrl("/ws/omni-call"));
  CALL.ws = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "start", video: CALL.video }));
    CALL.pingT = setInterval(() => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" })); }, 10000);
  };
  ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch (e) { return; } callOn(m); };
  ws.onclose = () => { if (!CALL.ended) callEnd(CALL.mode ? "" : "通话没接通"); };
}

function callOn(m) {
  if (m.type === "ready") {
    CALL.mode = m.mode;
    $("callBadge").textContent = (m.mode === "realtime" ? "实时" : "轮流") + " · " + (m.model || "");
    $("callBadge").title = m.note || "";
    $("capA").textContent = m.note ? m.note : "已接通，说话吧";
  } else if (m.type === "audio") {
    callPlay(m.data);
  } else if (m.type === "speech_started") {
    callStopPlayback();
  } else if (m.type === "transcript") {
    if (m.role === "user") { CALL.caps.user = m.text; $("capU").textContent = m.text; if (m.final) $("capA").textContent = ""; }
    else { CALL.caps.assistant = m.text; $("capA").textContent = m.text; if (m.final && m.speak) callSpeak(m.text); }
  } else if (m.type === "tool") {
    const t = { create_aiw_task: "交给 aiw", task_status: "查任务进度" }[m.name] || m.name;
    toast(m.status === "running" ? `正在${t}…` : m.status === "done" ? (m.text || `${t}完成`) : `${t}没成功`);
  } else if (m.type === "error") {
    toast(m.message || "出错了");
  } else if (m.type === "ended") {
    callEnd();
  }
}

function callPlay(b64) {
  const ctx = CALL.ctx; if (!ctx) return;
  const bin = atob(b64); const n = bin.length >> 1; if (!n) return;
  const buf = ctx.createBuffer(1, n, 24000); const ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) { let v = bin.charCodeAt(i * 2) | (bin.charCodeAt(i * 2 + 1) << 8); if (v >= 0x8000) v -= 0x10000; ch[i] = v / 0x8000; }
  const s = ctx.createBufferSource(); s.buffer = buf; s.connect(ctx.destination);
  const at = Math.max(ctx.currentTime + 0.04, CALL.next);
  s.start(at); CALL.next = at + buf.duration;
  CALL.sources.add(s); s.onended = () => { CALL.sources.delete(s); if (!CALL.sources.size) $("callView")?.classList.remove("talking"); };
  $("callView").classList.add("talking");
}

function callStopPlayback() {
  CALL.sources.forEach((s) => { try { s.stop(); } catch (e) { /* already stopped */ } });
  CALL.sources.clear(); CALL.next = 0;
  if (window.speechSynthesis) speechSynthesis.cancel();
  $("callView")?.classList.remove("talking");
}

function callSpeak(text) {   // the model gave no audio for this answer (text-only model, no 朗读模型): the browser reads it
  if (!window.speechSynthesis || !text) return;
  const u = new SpeechSynthesisUtterance(text); u.lang = "zh-CN"; u.rate = 1.05;
  speechSynthesis.speak(u);
}

function callInterrupt() {
  callStopPlayback();
  if (CALL.ws && CALL.ws.readyState === 1) CALL.ws.send(JSON.stringify({ type: "interrupt" }));
}

function callMute() {
  CALL.muted = !CALL.muted;
  const b = $("callMute"); b.innerHTML = icon(CALL.muted ? "micoff" : "mic"); b.classList.toggle("off", CALL.muted);
}

async function callCamToggle() {
  CALL.video = !CALL.video;
  const b = $("callCam"); b.innerHTML = icon(CALL.video ? "video" : "camoff"); b.classList.toggle("off", !CALL.video);
  await callCamera(CALL.video);
}

async function callFlip() {
  CALL.facing = CALL.facing === "user" ? "environment" : "user";
  if (CALL.video) await callCamera(true);
}

function callHangup() {
  if (CALL.ws && CALL.ws.readyState === 1) {
    CALL.ws.send(JSON.stringify({ type: "hangup" }));
    setTimeout(() => { if (!CALL.ended) callEnd(); }, 2500);   // the server answers "ended" after saving the transcript
  } else callEnd();
}

function callEnd(msg) {
  if (CALL.ended) return;
  CALL.ended = true;
  callStopPlayback();
  [CALL.frameT, CALL.pingT, CALL.clockT].forEach((t) => clearInterval(t));
  [CALL.mic, CALL.cam].forEach((s) => s && s.getTracks().forEach((t) => t.stop()));
  try { CALL.node && CALL.node.disconnect(); CALL.src && CALL.src.disconnect(); } catch (e) { /* gone */ }
  if (CALL.ctx) CALL.ctx.close().catch(() => {});
  if (CALL.ws && CALL.ws.readyState <= 1) CALL.ws.close();
  if (CALL.wake) CALL.wake.release().catch(() => {});
  try { CALL.mate && CALL.mate.destroy(); } catch (e) { /* gone */ }
  CALL.mate = null;
  Object.assign(CALL, { ws: null, ctx: null, mic: null, cam: null, node: null, src: null, wake: null, mode: "" });
  const el = $("callView"); if (el) { el.classList.remove("on", "cam", "talking"); el.innerHTML = ""; }
  document.body.classList.remove("in-call");
  if (msg) toast(msg);
}
