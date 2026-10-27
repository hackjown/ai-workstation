/* AIW task state → 球球 (Emotion Ball) emotion ID. Pure functions, no DOM: shared by the web app (mate.js), the page the
   iOS app embeds (mate.html) and the tests (tests/test_mate_map.py runs tests/js/mate_map_test.js with node).
   Emotion IDs (agent group 30-41, lifecycle 00-02) are the Emotion Ball public contract (same IDs as Mood Mates) and never renumbered. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AiwMood = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  var ID = {
    SLEEP: "00", WAKE: "01", IDLE: "02",
    THINK: "30", RECEIVE: "31", BUSY: "32", DONE: "33", ERROR: "34", WAIT: "35",
    LOADING: "36", REFUSED: "38", REPLY: "39", SEARCH: "40", STOPPED: "41"
  };
  var NAMES = { "00": "睡眠", "01": "唤醒", "02": "待机", "30": "思考中", "31": "接收任务", "32": "处理中", "33": "任务完成",
    "34": "出错", "35": "等待你", "36": "联网加载", "38": "受限", "39": "输出回复", "40": "检索资料", "41": "已停止" };
  // captcha / risk-control / rate-limit wording seen in takeover reasons, tool errors and xhs/shop warnings
  var BLOCKED = /验证码|人机|滑块|安全验证|身份验证|风控|风险提示|操作频繁|访问频繁|请求频繁|账号异常|captcha|recaptcha|hcaptcha|cloudflare|verify (you are|that you are) human|human check|rate.?limit|too many requests|risk control/i;
  var NAV = /^(browser_(open|navigate|back|forward|new_tab|goto)|bos_(navigate|tabs|wait)|browseros_(navigate|open|new_page|new_tab|go_back|go_forward|goto|tab)\w*|open_url|navigate)$/;
  var LOOK = /(snapshot|_read|read_|search|fetch|extract|get_page|page_content|get_text|scrape|xhs_status|price_check|shop_probe)/;
  var WAITERS = { ask_user: 1, request_takeover: 1, order_confirm: 1, xhs_review: 1 };
  var LIVE = { queued: 1, sending: 1, running: 1, waiting_confirm: 1, waiting_input: 1, takeover: 1, paused: 1 };
  var SETTLE_MS = 6000;

  function str(v) { return v == null ? "" : typeof v === "string" ? v : JSON.stringify(v); }
  function blocked() { for (var i = 0; i < arguments.length; i++) if (BLOCKED.test(str(arguments[i]))) return true; return false; }

  /** The emotion for one tool the model just chose. */
  function forTool(tool, args) {
    tool = String(tool || "");
    args = args || {};
    if (tool === "done") return ID.REPLY;
    if (tool === "fail") return ID.ERROR;
    if (tool === "xhs_warning") return ID.REFUSED;
    if (tool === "request_takeover" && blocked(args.reason, args.question)) return ID.REFUSED;
    if (WAITERS[tool]) return ID.WAIT;
    if (tool === "update_plan" || tool === "remember" || tool === "(none)" || tool === "") return ID.THINK;
    if (NAV.test(tool)) return ID.LOADING;
    if (/^(bos|browseros)_/.test(tool) && /snapshot|content|read|grep|diff|screenshot/.test(tool)) return ID.SEARCH;
    if (LOOK.test(tool) || tool === "read_image" || tool === "files_read" || tool === "files_list") return ID.SEARCH;
    return ID.BUSY;
  }

  /** Map one task/session event to a mood change, or null when the event does not change the mood.
      Result: {id, burst?: true, settle?: ms (go back to idle after that long)}. */
  function fromEvent(ev) {
    if (!ev) return null;
    var t = ev.type || "", d = ev.data || {};
    switch (t) {
      case "task.started": return { id: ID.RECEIVE };
      case "agent.plan": return { id: ID.THINK };
      case "agent.thinking": return { id: ID.THINK };
      case "agent.decision": return { id: forTool(d.tool, d.args) };
      case "agent.tool_call": return d.tool ? { id: forTool(d.tool, d.args) } : null;
      case "agent.action_result":
        if (d.success === false && blocked(d.error, d.result)) return { id: ID.REFUSED };
        return null;
      case "agent.confirm_required": return { id: ID.WAIT };
      case "agent.confirm_answered": return { id: ID.BUSY };
      case "agent.input_required": {
        var f = d.form || {};
        if (blocked(d.question, f.title)) return { id: ID.REFUSED };
        return { id: ID.WAIT };
      }
      case "agent.input_answered": return { id: ID.BUSY };
      case "agent.takeover": return { id: ID.WAIT };
      case "agent.resumed": return { id: ID.BUSY };
      case "agent.paused": return { id: ID.WAIT };
      case "agent.message": return { id: ID.REPLY };
      case "agent.dispatch": return { id: ID.BUSY };
      case "agent.subagent": return { id: ID.BUSY };
      case "task.finished": return { id: ID.DONE, burst: true, settle: SETTLE_MS };
      case "task.failed": {
        var s = String(d.status || "");
        if (s === "cancelled" || s === "stopped") return { id: ID.STOPPED, settle: SETTLE_MS };
        if (blocked(d.result, d.error)) return { id: ID.REFUSED, settle: SETTLE_MS };
        return { id: ID.ERROR, settle: SETTLE_MS };
      }
      default: return null;
    }
  }

  /** Mood from a task's stored status (page load, thread switch) before any live event arrives. */
  function fromStatus(status) {
    switch (String(status || "")) {
      case "queued": case "sending": return ID.RECEIVE;
      case "running": return ID.THINK;
      case "waiting_confirm": case "waiting_input": case "takeover": case "paused": return ID.WAIT;
      default: return ID.IDLE;
    }
  }
  function isLive(status) { return !!LIVE[String(status || "")]; }

  /** Mood of a computer card: {online, paused, waking, offline, taskStatus, liveId}. */
  function forMachine(m) {
    m = m || {};
    if (m.liveId) return m.liveId;
    if (isLive(m.taskStatus)) return fromStatus(m.taskStatus);
    if (m.waking) return ID.WAKE;
    if (m.paused || m.offline || m.online === false) return ID.SLEEP;
    return ID.IDLE;
  }

  /** Replay a list of events (oldest first) and return the mood they end in, starting from a status. */
  function replay(events, status) {
    var id = fromStatus(status);
    (events || []).forEach(function (e) { var r = fromEvent(e); if (r) id = r.id; });
    if (!isLive(status) && status) id = ID.IDLE; // a finished task settles back to idle
    return id;
  }

  return { ID: ID, NAMES: NAMES, SETTLE_MS: SETTLE_MS, forTool: forTool, fromEvent: fromEvent, fromStatus: fromStatus,
    forMachine: forMachine, replay: replay, isLive: isLive, blocked: function (x) { return blocked(x); } };
});
