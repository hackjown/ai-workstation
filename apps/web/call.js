/* v3.9.31 视频通话 (omni realtime voice + video call, like 豆包): a full-screen call view over /ws/omni-call.
   Microphone -> AudioWorklet (ScriptProcessor where there is none) -> 16 kHz pcm16 chunks of 40 ms; camera -> canvas
   -> JPEG (quality 0.6, long edge ≤720) at most once a second and only when the picture changed (or every 5 s);
   the model's 24 kHz pcm16 plays through a scheduled AudioBuffer queue, cut at once on speech_started / 打断.
   echoCancellation is asked for in getUserMedia so the model does not hear itself. Server messages: ready (mode
   badge 实时 / 轮流), audio, transcript (live captions; speak=true -> read out with speechSynthesis), speech_started,
   tool, error, ended. The transcript is saved to the 小秘 chat by the server at hang-up.
   v3.9.36 (no mic audio from iPhone Safari): the AudioContext is created and resume()d synchronously inside the tap
   that opens the call (before any await - iOS forgets the user gesture after one), and reused; a silent buffer unlocks
   playback. If it is still not "running" (or iOS interrupts it later), a 「点一下开始声音」 overlay resumes it on the
   next tap; any tap in the call also resumes it. A mic level meter shows the microphone is heard, and 「没有收到麦克风
   声音」 shows when no samples arrive for 3 s (a tap then rebuilds the audio graph). The downsampler keeps its
   fractional position across 128-sample worklet blocks so 48 kHz / 44.1 kHz both give exact 16 kHz. start says
   client "web"; mute / unmute is told to the server so a muted mic is not reported as broken. */
const CALL = { ws: null, ctx: null, mic: null, cam: null, node: null, src: null, sink: null, sources: new Set(), next: 0,
  muted: false, video: true, facing: "user", frameT: null, pingT: null, clockT: null, micT: null, t0: 0, ended: false,
  lastThumb: null, lastSent: 0, caps: { user: "", assistant: "" }, mode: "", wake: null, opening: false,
  samples: 0, lastSample: 0, lastVoice: 0, audioAt: 0, level: 0, meterAt: 0, micWarn: false };

Object.assign(IC, {
  phone: "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2z",
  hangup: "M10.7 13.3a16 16 0 0 0 3.4 2.6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 3.1 4.2 2 2 0 0 1 5.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L9 9.8M23 1 1 23",
  micoff: "M1 1l22 22M9 9v3a3 3 0 0 0 5.1 2.1M15 9.3V5a3 3 0 0 0-5.9-.6M17 16.9A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.1 1.2M12 19v3",
  camoff: "M1 1l22 22M21 21H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3m3-3h6l2 3h4a2 2 0 0 1 2 2v9.3M14.1 14.1a3 3 0 1 1-4.2-4.2",
  flip: "M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5",
});
document.querySelectorAll("#hsheet [data-hi], #callBtn [data-hi]").forEach((el) => { el.innerHTML = icon(el.dataset.hi); });

const CALL_WORKLET = `class P extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor("aiw-mic",P);`;

/* v3.9.36: must run synchronously inside a user tap (no await before it): creates / resumes the one AudioContext of
   the call and plays one silent sample, which is what lets iOS Safari run the mic worklet and play the answer. */
function callUnlockAudio() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!CALL.ctx || CALL.ctx.state === "closed") {
    try { CALL.ctx = new AC({ latencyHint: "interactive" }); } catch (e) { CALL.ctx = new AC(); }
    CALL.ctx.onstatechange = callCtxState;
  }
  const ctx = CALL.ctx;
  try { const p = ctx.resume(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* old Safari */ }
  try {
    const b = ctx.createBuffer(1, 1, ctx.sampleRate); const s = ctx.createBufferSource();
    s.buffer = b; s.connect(ctx.destination); s.start(0);
  } catch (e) { /* nothing to unlock */ }
  return ctx;
}

function callCtxState() {   // iOS: "interrupted" (a phone call, Siri, another app's audio), "suspended" (no gesture)
  const ctx = CALL.ctx;
  if (!ctx || CALL.ended || ctx.onstatechange !== callCtxState) return;
  if (ctx.state === "running") { callAudioGate(false); return; }
  if (ctx.state === "closed") return;
  setTimeout(() => {   // try once on our own; iOS often lets an interrupted context resume after the interruption
    if (!CALL.ctx || CALL.ended || CALL.ctx.state === "running") return;
    try { CALL.ctx.resume().catch(() => {}); } catch (e) { /* no */ }
    setTimeout(() => { if (CALL.ctx && !CALL.ended && CALL.ctx.state !== "running") callAudioGate(true); }, 400);
  }, 300);
}

/* the tap-to-start-audio overlay: a real tap is what iOS needs to resume the context */
function callAudioGate(on, text) {
  const v = $("callView"); if (!v) return;
  let g = $("callGate");
  if (!on) { if (g) g.remove(); return; }
  if (!g) {
    g = document.createElement("button"); g.id = "callGate"; g.className = "call-gate"; g.type = "button";
    g.onclick = (e) => { e.stopPropagation(); callGateTap(); };
    v.appendChild(g);
  }
  g.innerHTML = `${icon("mic")}<span>${esc(text || "点一下开始声音")}</span>`;
}

function callGateTap() {
  const ctx = callUnlockAudio();   // synchronous, inside the tap
  if (!ctx) return;
  const stale = CALL.audioAt && Date.now() - Math.max(CALL.lastSample, CALL.audioAt) > 3000;
  if (CALL.mic && (stale || !CALL.node)) {
    // the context runs but the mic delivers nothing: rebuild the graph on a fresh context made in this tap
    try { CALL.node && CALL.node.disconnect(); CALL.src && CALL.src.disconnect(); } catch (e) { /* gone */ }
    if (CALL.samples === 0 || stale) {
      try { ctx.onstatechange = null; ctx.close().catch(() => {}); } catch (e) { /* closed */ }
      CALL.ctx = null; callUnlockAudio();
    }
    CALL.sources.clear(); CALL.next = 0;   // the answer queue was timed on the old context
    callAudio().catch((e) => toast("麦克风打不开：" + e.message));
  }
  setTimeout(() => { if (CALL.ctx && CALL.ctx.state === "running") callAudioGate(false); }, 250);
}

function callTapResume() {   // any tap in the call: resume a suspended / interrupted context
  if (CALL.ctx && CALL.ctx.state !== "running" && CALL.ctx.state !== "closed") callUnlockAudio();
}

async function openCall(video = true) {
  if (CALL.ws || CALL.opening) return;
  callUnlockAudio();   // v3.9.36: before the first await, while the tap still counts
  CALL.opening = true;
  let st;
  try { st = await api("GET", "/api/omni/status"); } catch (e) { CALL.opening = false; callDropCtx(); return toast("读不到通话设置：" + e.message); }
  CALL.opening = false;
  if (!st.enabled) {
    callDropCtx();
    return modal(`<div class="callnote"><h3>${icon("phone")} 视频通话还没开</h3><div class="muted">${esc(st.message || "先设置通话模型")}</div>
      <div class="row gap"><button class="btn primary" onclick="closeOverlays();go('settings');setTimeout(()=>{const el=document.getElementById('g-call');if(el)el.scrollIntoView({behavior:'smooth'})},400)">去设置</button><button class="btn" onclick="closeOverlays()">取消</button></div></div>`);
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { callDropCtx(); return toast("这个浏览器不能用麦克风（需要 HTTPS 或 localhost）"); }
  CALL.video = video; CALL.ended = false; CALL.muted = false; CALL.facing = "user"; CALL.caps = { user: "", assistant: "" };
  Object.assign(CALL, { samples: 0, lastSample: 0, lastVoice: 0, audioAt: 0, level: 0, micWarn: false });
  callView(st);
  try {
    CALL.mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch (e) { callEnd("没有麦克风权限：" + (e.message || e.name)); return; }
  if (video) await callCamera(true);
  try { await callAudio(); } catch (e) { callEnd("麦克风打不开：" + e.message); return; }
  if (CALL.ended) return;
  CALL.mic.getAudioTracks().forEach((t) => { t.onended = () => { if (!CALL.ended) callMicWarn(true, "麦克风被系统停掉了，挂断后重拨"); }; });
  CALL.micT = setInterval(callMicCheck, 1000);
  callConnect(st);
  try { CALL.wake = await navigator.wakeLock.request("screen"); } catch (e) { /* not supported */ }
}

function callDropCtx() {
  if (CALL.ctx && !CALL.ws) { try { CALL.ctx.onstatechange = null; CALL.ctx.close().catch(() => {}); } catch (e) { /* closed */ } CALL.ctx = null; }
}

function callView(st) {
  let el = $("callView");
  if (!el) { el = document.createElement("div"); el.id = "callView"; document.body.appendChild(el); }
  el.innerHTML = `<video id="callVideo" playsinline muted autoplay></video>
    <div class="call-orb" id="callOrb"><span class="mate" id="callMate" role="img" aria-label="球球"></span></div>
    <div class="call-top"><span class="call-badge" id="callBadge">连接中…</span><span class="call-meter" id="callMeter" title="麦克风音量"><i></i></span><span class="call-clock" id="callClock">00:00</span></div>
    <div class="call-warn" id="callWarn" hidden></div>
    <div class="call-caps" id="callCaps" onclick="callInterrupt()" title="点一下打断"><div class="cap-u" id="capU"></div><div class="cap-a" id="capA">正在接通…</div></div>
    <div class="call-bar">
      <button class="cbtn" id="callMute" onclick="callMute()" aria-label="静音">${icon("mic")}</button>
      <button class="cbtn" id="callCam" onclick="callCamToggle()" aria-label="摄像头">${icon(CALL.video ? "video" : "camoff")}</button>
      <button class="cbtn" id="callFlip" onclick="callFlip()" aria-label="切换前后摄像头" hidden>${icon("flip")}</button>
      <button class="cbtn end" onclick="callHangup()" aria-label="挂断">${icon("hangup")}</button>
    </div>`;
  el.classList.add("on");
  el.onpointerdown = callTapResume;   // v3.9.36
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

/* v3.9.36: a downsampler to 16 kHz that keeps its fractional read position between calls, so 128-sample worklet
   blocks at 48 kHz (ratio 3) and 44.1 kHz (ratio 2.75625) lose nothing; chunks of 640 samples (40 ms) go to onChunk. */
function callDownsampler(rate, onChunk, size = 640) {
  const ratio = rate / 16000;
  let raw = new Float32Array(0), pos = 0, out = new Float32Array(size), k = 0;
  return (f32) => {
    const m = new Float32Array(raw.length + f32.length); m.set(raw); m.set(f32, raw.length); raw = m;
    while (pos + ratio <= raw.length) {
      const a = Math.floor(pos), b = Math.max(a + 1, Math.floor(pos + ratio)); let s = 0;
      for (let j = a; j < b; j++) s += raw[j];
      out[k++] = s / (b - a); pos += ratio;
      if (k === size) { onChunk(out); out = new Float32Array(size); k = 0; }
    }
    const used = Math.floor(pos); raw = raw.slice(used); pos -= used;
  };
}

async function callAudio() {
  const ctx = CALL.ctx && CALL.ctx.state !== "closed" ? CALL.ctx : callUnlockAudio();
  if (!ctx) throw new Error("这个浏览器没有 Web Audio");
  CALL.ctx = ctx;
  // resume() can stay pending forever on iOS without a gesture: never wait on it for long
  if (ctx.state !== "running") await Promise.race([ctx.resume().catch(() => {}), new Promise((r) => setTimeout(r, 600))]);
  CALL.src = ctx.createMediaStreamSource(CALL.mic);
  const push = callDownsampler(ctx.sampleRate, callSendPcm);
  const feed = (f32) => {   // level meter + "are samples arriving at all"
    const now = Date.now(); CALL.samples += f32.length; CALL.lastSample = now;
    let pk = 0, sq = 0; for (let i = 0; i < f32.length; i++) { const v = f32[i]; sq += v * v; if (v > pk) pk = v; else if (-v > pk) pk = -v; }
    if (pk > 1e-4) CALL.lastVoice = now;
    CALL.level = Math.max(Math.sqrt(sq / Math.max(1, f32.length)), CALL.level * 0.85);
    if (now - CALL.meterAt > 100) { CALL.meterAt = now; const m = $("callMeter"); if (m && m.firstChild) m.firstChild.style.width = Math.min(100, Math.round(CALL.level * 400)) + "%"; }
    push(f32);
  };
  if (ctx.audioWorklet && window.AudioWorkletNode) {
    const url = URL.createObjectURL(new Blob([CALL_WORKLET], { type: "application/javascript" }));
    try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
    CALL.node = new AudioWorkletNode(ctx, "aiw-mic", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: "explicit" });
    CALL.node.port.onmessage = (e) => feed(e.data);
  } else {
    CALL.node = ctx.createScriptProcessor(2048, 1, 1);
    CALL.node.onaudioprocess = (e) => feed(e.inputBuffer.getChannelData(0));
  }
  CALL.sink = ctx.createGain(); CALL.sink.gain.value = 0;   // keep the graph pulling without playing the mic back
  CALL.src.connect(CALL.node); CALL.node.connect(CALL.sink); CALL.sink.connect(ctx.destination);
  CALL.audioAt = Date.now();
  if (ctx.state !== "running") callAudioGate(true);
}

function callMicWarn(on, text) {
  CALL.micWarn = on;
  const w = $("callWarn"); if (!w) return;
  w.hidden = !on; if (on) w.textContent = text || "没有收到麦克风声音";
}

function callMicCheck() {   // every second: samples stopped (or never came) for 3 s -> say so and offer the tap
  if (CALL.ended || !CALL.audioAt) return;
  const now = Date.now();
  if (CALL.muted) { if (CALL.micWarn) callMicWarn(false); return; }
  const noSamples = now - Math.max(CALL.lastSample, CALL.audioAt) > 3000;
  const allZero = !noSamples && CALL.samples > 0 && now - Math.max(CALL.lastVoice, CALL.audioAt) > 3000;
  if (noSamples) {
    callMicWarn(true, "没有收到麦克风声音，点一下屏幕试试");
    callAudioGate(true, CALL.ctx && CALL.ctx.state !== "running" ? "点一下开始声音" : "点一下重新打开麦克风");
  } else if (allZero) {
    callMicWarn(true, "麦克风只有静音：检查静音键 / 系统麦克风权限");
  } else if (CALL.micWarn) {
    callMicWarn(false); callAudioGate(false);
  }
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
    ws.send(JSON.stringify({ type: "start", video: CALL.video, client: "web", ua: String(navigator.userAgent || "").slice(0, 160) }));
    if (CALL.muted) ws.send(JSON.stringify({ type: "mute", on: true }));
    CALL.pingT = setInterval(() => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" })); }, 10000);
  };
  ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch (e) { return; } callOn(m); };
  ws.onclose = () => { if (!CALL.ended) callEnd(CALL.mode ? "" : "通话没接通"); };
}

function callOn(m) {
  if (m.type === "ready") {
    CALL.mode = m.mode;
    $("callBadge").textContent = ({ realtime: "实时", gemini: "Gemini" }[m.mode] || "轮流") + " · " + (m.model || "");
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
    if (m.code === "no_mic") {   // v3.9.36: the server heard nothing from us
      callMicWarn(true, m.message || "服务器没有收到麦克风声音");
      callAudioGate(true, CALL.ctx && CALL.ctx.state !== "running" ? "点一下开始声音" : "点一下重新打开麦克风");
    } else toast(m.message || "出错了");
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
  if (CALL.ws && CALL.ws.readyState === 1) CALL.ws.send(JSON.stringify({ type: "mute", on: CALL.muted }));
  if (!CALL.muted) CALL.audioAt = Date.now();   // fresh 3 s before the no-mic note
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
  [CALL.frameT, CALL.pingT, CALL.clockT, CALL.micT].forEach((t) => clearInterval(t));
  [CALL.mic, CALL.cam].forEach((s) => s && s.getTracks().forEach((t) => t.stop()));
  try { CALL.node && CALL.node.disconnect(); CALL.src && CALL.src.disconnect(); } catch (e) { /* gone */ }
  if (CALL.ctx) { CALL.ctx.onstatechange = null; CALL.ctx.close().catch(() => {}); }
  if (CALL.ws && CALL.ws.readyState <= 1) CALL.ws.close();
  if (CALL.wake) CALL.wake.release().catch(() => {});
  try { CALL.mate && CALL.mate.destroy(); } catch (e) { /* gone */ }
  CALL.mate = null;
  Object.assign(CALL, { ws: null, ctx: null, mic: null, cam: null, node: null, src: null, sink: null, wake: null, mode: "", micT: null, audioAt: 0 });
  const el = $("callView"); if (el) { el.classList.remove("on", "cam", "talking"); el.innerHTML = ""; el.onpointerdown = null; }
  document.body.classList.remove("in-call");
  if (msg) toast(msg);
}
