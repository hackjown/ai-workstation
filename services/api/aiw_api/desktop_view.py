"""The local-VM desktop viewer (3.8.7): our own small page on noVNC's RFB API instead of noVNC's vnc.html.

Bug it fixes (iOS 3.8.6 电脑 › 查看 on a local VM): a black screen with bare white inputs, "Send Credentials" and
"Cancel". That was vnc.html with none of its CSS or JS: the iOS app loads the viewer in an iframe with ?token= on the
page URL only, the noVNC files it pulls (app/styles/*.css, app/ui.js, core/*.js) went through /desktop/<mid>/… with
no token and no aiw_token cookie (only the web console sets that cookie), so each one was a 401. Unstyled, vnc.html's
hidden credentials dialog shows as raw form controls and nothing ever connects. The websocket path had no token either.

Now:
  * /desktop/<mid>/aiw-view.html (this page, served by the API) imports ./core/rfb.js and connects itself: the
    websocket URL carries the token, the VNC password the server knows (AIW_VNC_PASSWORD) is sent on
    `credentialsrequired`, a wrong / missing password shows a styled prompt (kept per computer in localStorage),
    a dropped connection shows a styled note and reconnects. The canvas sits in #noVNC_container, where takeover.js
    looks for it. noVNC older than 1.0 (no ES modules) falls back to vnc.html with the token in its path.
  * noVNC's static code (css/js/images/fonts) is served without auth: it is the public noVNC source, nothing of the
    user's. Pages (html) still need the token, and the first authorised request sets the aiw_token cookie.
"""
from __future__ import annotations

import posixpath
from urllib.parse import quote

PUBLIC_EXT = (".css", ".js", ".mjs", ".svg", ".png", ".ico", ".gif", ".jpg", ".woff", ".woff2", ".ttf", ".oga",
              ".ogg", ".mp3", ".wav", ".map")
VIEW_PAGE = "aiw-view.html"


def public_asset(path: str) -> bool:
    """noVNC's own static code and images: fine without a token (no user data, no html)."""
    p = posixpath.normpath("/" + (path or "")).lstrip("/")
    return bool(p) and ".." not in p.split("/") and p.lower().endswith(PUBLIC_EXT)


def view_url(mid: str, view_only: bool, password: str = "") -> str:
    """What /api/machines/<mid>/desktop hands out (clients append &token=…)."""
    pw = f"&password={quote(password, safe='')}" if password else ""
    return (f"/desktop/{quote(mid, safe='')}/{VIEW_PAGE}?view_only={'1' if view_only else '0'}"
            f"&path={quote(f'ws/desktop/{mid}', safe='/')}{pw}")


VIEW_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>桌面</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; overflow: hidden; overscroll-behavior: none; }
  #noVNC_container { position: absolute; inset: 0; }
  #aiw_msg { position: absolute; inset: 0; display: none; flex-direction: column; align-items: center; justify-content: center;
             gap: 12px; padding: 24px; text-align: center; color: #eee; background: #000;
             font: 15px -apple-system, BlinkMacSystemFont, "PingFang SC", system-ui, sans-serif; }
  #aiw_msg.on { display: flex; }
  #aiw_msg .t { font-size: 16px; font-weight: 600; }
  #aiw_msg .d { color: #9a9a9e; font-size: 13px; max-width: 320px; line-height: 1.5; }
  #aiw_pw { display: none; gap: 8px; margin-top: 4px; }
  #aiw_pw.on { display: flex; }
  #aiw_pw input { width: 180px; padding: 10px 12px; border-radius: 10px; border: 1px solid #3a3a3c; background: #1c1c1e;
                  color: #fff; font-size: 16px; outline: none; }
  #aiw_msg button { padding: 10px 16px; border: 0; border-radius: 10px; background: #E8962E; color: #1a1a1a;
                    font-size: 15px; font-weight: 600; }
  #aiw_again { display: none; }
  #aiw_again.on { display: inline-block; }
  .aiw-spin { width: 26px; height: 26px; border-radius: 50%; border: 3px solid #333; border-top-color: #E8962E;
              animation: aiwspin .9s linear infinite; }
  .aiw-spin.off { display: none; }
  @keyframes aiwspin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div id="noVNC_container"></div>
<div id="aiw_msg" class="on">
  <div class="aiw-spin" id="aiw_spin"></div>
  <div class="t" id="aiw_t">正在连接桌面…</div>
  <div class="d" id="aiw_d"></div>
  <form id="aiw_pw" autocomplete="off"><input id="aiw_pwi" type="password" placeholder="VNC 密码" enterkeyhint="go"><button type="submit">连接</button></form>
  <button id="aiw_again" type="button">重新连接</button>
</div>
<script type="module">
const q = new URLSearchParams(location.search);
const token = q.get("token") || "";
const path = (q.get("path") || "").replace(/^\/+/, "");
const viewOnly = q.get("view_only") === "1" || q.get("view_only") === "true";
const mid = decodeURIComponent(location.pathname.split("/")[2] || "");
const KEY = "aiw_vnc_pw:" + mid;
const $ = (id) => document.getElementById(id);
let pw = q.get("password") || "";
try { pw = pw || localStorage.getItem(KEY) || ""; } catch (e) { /* private mode */ }
let rfb = null, RFB = null, sentPw = false, needPw = false, ok = false, tries = 0, timer = 0, watchdog = 0;

function say(title, detail, opt) {
  opt = opt || {};
  $("aiw_t").textContent = title; $("aiw_d").textContent = detail || "";
  $("aiw_spin").classList.toggle("off", !opt.spin);
  $("aiw_pw").classList.toggle("on", !!opt.pw);
  $("aiw_again").classList.toggle("on", !!opt.again);
  $("aiw_msg").classList.add("on");
  if (opt.pw) setTimeout(() => $("aiw_pwi").focus(), 50);
}
function hide() { $("aiw_msg").classList.remove("on"); }
function wsUrl() {
  const u = new URL("/" + path, location.href);
  u.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  if (token) u.searchParams.set("token", token);
  return u.href;
}
function fallback() {   // noVNC < 1.0 has no core/rfb.js module: its own page, now with styles and the token in its path
  const p = path + (token ? "?token=" + encodeURIComponent(token) : "");
  location.replace("vnc.html?autoconnect=1&resize=scale&reconnect=1&view_only=" + (viewOnly ? 1 : 0) +
                   "&path=" + encodeURIComponent(p) + (pw ? "&password=" + encodeURIComponent(pw) : "") +
                   (token ? "&token=" + encodeURIComponent(token) : ""));
}
function later() {
  clearTimeout(timer);
  const s = Math.min(10, 1 + tries * 2);
  say("桌面断开了", s + " 秒后自动重连", { spin: true, again: true });
  timer = setTimeout(connect, s * 1000);
}
function connect() {
  clearTimeout(timer); clearTimeout(watchdog);
  if (rfb) { try { rfb.disconnect(); } catch (e) { /* gone */ } rfb = null; }
  if (!path) { say("没有桌面地址", "这台电脑没有配置桌面（desktop_endpoint）"); return; }
  tries++; sentPw = false; ok = false;
  say("正在连接桌面…", "", { spin: true });
  try {
    rfb = new RFB($("noVNC_container"), wsUrl(), { shared: true, wsProtocols: ["binary"],
                                                    credentials: pw ? { password: pw } : undefined });
  } catch (e) { say("桌面打不开", String(e && e.message || e), { again: true }); return; }
  rfb.scaleViewport = true; rfb.resizeSession = false; rfb.clipViewport = false;
  rfb.viewOnly = viewOnly; rfb.background = "#000";
  try { rfb.focusOnClick = !viewOnly; rfb.qualityLevel = 6; rfb.compressionLevel = 2; } catch (e) { /* older noVNC */ }
  // v3.9.22 (接管时有时看不到鼠标): the VNC server sometimes sends an empty / transparent cursor (x11vnc and
  // TigerVNC do it over some windows and right after xdotool moved the pointer); noVNC then draws nothing. The dot
  // cursor shows in its place, and the viewer takes the keyboard focus on connect.
  try { rfb.showDotCursor = !viewOnly; } catch (e) { /* older noVNC */ }
  rfb.addEventListener("connect", () => {
    ok = true; tries = 0; needPw = false; clearTimeout(watchdog); hide();
    if (!viewOnly) { try { rfb.focus(); } catch (e) { /* older noVNC */ } }
    if (sentPw && pw) { try { localStorage.setItem(KEY, pw); } catch (e) { /* private mode */ } }
  });
  rfb.addEventListener("credentialsrequired", (e) => {
    const types = (e.detail && e.detail.types) || ["password"];
    if (pw && !sentPw) {   // the password the server gave (or the one typed last time): no form at all
      sentPw = true;
      const c = { password: pw };
      if (types.indexOf("username") >= 0) c.username = q.get("username") || "";
      rfb.sendCredentials(c);
      return;
    }
    needPw = true; clearTimeout(watchdog);
    say("这台电脑的桌面要 VNC 密码", sentPw ? "密码不对，再输一次" : "输一次就记住（只存在这台手机上）", { pw: true });
  });
  rfb.addEventListener("securityfailure", (e) => {
    needPw = true; clearTimeout(watchdog);
    try { localStorage.removeItem(KEY); } catch (x) { /* private mode */ }
    pw = "";
    const why = (e.detail && e.detail.reason) || "";
    say("桌面拒绝了连接", (why ? why + " · " : "") + "检查 VNC 密码后再连", { pw: true, again: true });
  });
  rfb.addEventListener("disconnect", () => {
    clearTimeout(watchdog);
    if (needPw) return;            // waiting for the password form
    later();
  });
  watchdog = setTimeout(() => { if (!ok && !needPw) say("桌面还没连上…", "电脑可能还在开机，或者桌面服务（novnc）没在运行", { spin: true, again: true }); }, 12000);
}
$("aiw_pw").addEventListener("submit", (e) => {
  e.preventDefault();
  pw = $("aiw_pwi").value || ""; $("aiw_pwi").value = ""; needPw = false; tries = 0;
  if (pw) connect();
});
$("aiw_again").addEventListener("click", () => { tries = 0; needPw = false; connect(); });
window.aiwView = { get rfb() { return rfb; }, reconnect: () => { tries = 0; connect(); } };

import("./core/rfb.js").then((m) => { RFB = m.default; connect(); }, (e) => {
  console.warn("noVNC module:", e);
  fallback();
});
</script>
</body>
</html>
"""
