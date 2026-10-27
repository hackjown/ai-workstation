// node tests/js/mate_map_test.js — state→emotion mapping of apps/web/mate-map.js (run by tests/test_mate_map.py)
const assert = require("assert");
const M = require("../../apps/web/mate-map.js");
const ev = (type, data) => ({ type, data: data || {} });
const id = (e) => (M.fromEvent(e) || {}).id;
const cases = [
  [ev("task.started"), "31"],
  [ev("agent.thinking"), "30"],
  [ev("agent.thinking", { live: true, text: "…" }), "30"],
  [ev("agent.decision", { tool: "shell", args: { command: "ls" } }), "32"],
  [ev("agent.decision", { tool: "click", args: { x: 1, y: 2 } }), "32"],
  [ev("agent.decision", { tool: "browser_open", args: { url: "https://x" } }), "36"],
  [ev("agent.decision", { tool: "browser_navigate", args: { url: "https://x" } }), "36"],
  [ev("agent.decision", { tool: "browseros_navigate_page", args: {} }), "36"],
  [ev("agent.decision", { tool: "browseros_new_page", args: {} }), "36"],
  [ev("agent.decision", { tool: "browseros_take_snapshot", args: {} }), "40"],
  [ev("agent.decision", { tool: "browseros_click", args: { ref: "e3" } }), "32"],
  [ev("agent.decision", { tool: "bos_navigate", args: { url: "https://x" } }), "36"],
  [ev("agent.decision", { tool: "bos_tabs", args: {} }), "36"],
  [ev("agent.decision", { tool: "bos_snapshot", args: {} }), "40"],
  [ev("agent.decision", { tool: "bos_read", args: {} }), "40"],
  [ev("agent.decision", { tool: "bos_grep", args: {} }), "40"],
  [ev("agent.decision", { tool: "bos_act", args: { ref: "e3" } }), "32"],
  [ev("agent.decision", { tool: "bos_key", args: {} }), "32"],
  [ev("agent.decision", { tool: "files_read", args: {} }), "40"],
  [ev("agent.decision", { tool: "read_image", args: {} }), "40"],
  [ev("agent.decision", { tool: "ask_user", args: { question: "?" } }), "35"],
  [ev("agent.decision", { tool: "request_takeover", args: { reason: "请完成滑块验证码" } }), "38"],
  [ev("agent.decision", { tool: "request_takeover", args: { reason: "请输入短信里的内容" } }), "35"],
  [ev("agent.decision", { tool: "xhs_warning", args: {} }), "38"],
  [ev("agent.decision", { tool: "update_plan", args: {} }), "30"],
  [ev("agent.decision", { tool: "done", args: { summary: "ok" } }), "39"],
  [ev("agent.message", { text: "好了" }), "39"],
  [ev("agent.confirm_required", { tool: "shell", level: 3 }), "35"],
  [ev("agent.input_required", { question: "选哪个？", options: ["a"] }), "35"],
  [ev("agent.input_required", { question: "Cloudflare 人机验证，请接管", form: { kind: "takeover" } }), "38"],
  [ev("agent.input_answered", { answer: "a" }), "32"],
  [ev("agent.takeover"), "35"],
  [ev("agent.resumed"), "32"],
  [ev("agent.action_result", { tool: "browser_navigate", success: false, error: "captcha detected" }), "38"],
  [ev("task.finished", { status: "succeeded" }), "33"],
  [ev("task.failed", { status: "failed", result: "boom" }), "34"],
  [ev("task.failed", { status: "cancelled" }), "41"],
  [ev("task.failed", { status: "failed", result: "操作频繁，触发风控" }), "38"],
];
for (const [e, want] of cases) assert.strictEqual(id(e), want, `${e.type} ${JSON.stringify(e.data)} -> ${id(e)} (want ${want})`);
// events that leave the mood alone
assert.strictEqual(M.fromEvent(ev("agent.observation", { artifact: "a.png" })), null);
assert.strictEqual(M.fromEvent(ev("agent.action_result", { success: true })), null);
assert.strictEqual(M.fromEvent(null), null);
// finish bursts and settles back to idle
const fin = M.fromEvent(ev("task.finished"));
assert.ok(fin.burst && fin.settle > 0);
assert.ok(M.fromEvent(ev("task.failed")).settle > 0);
// stored status
const st = { queued: "31", running: "30", waiting_confirm: "35", waiting_input: "35", takeover: "35", paused: "35", succeeded: "02", failed: "02", cancelled: "02", "": "02" };
for (const [s, want] of Object.entries(st)) assert.strictEqual(M.fromStatus(s), want, "status " + s);
// machines
assert.strictEqual(M.forMachine({ online: true }), "02");
assert.strictEqual(M.forMachine({ paused: true }), "00");
assert.strictEqual(M.forMachine({ online: false }), "00");
assert.strictEqual(M.forMachine({ offline: true }), "00");
assert.strictEqual(M.forMachine({ waking: true }), "01");
assert.strictEqual(M.forMachine({ paused: true, taskStatus: "running" }), "30");
assert.strictEqual(M.forMachine({ online: true, taskStatus: "running", liveId: "36" }), "36");
// replay
assert.strictEqual(M.replay([ev("task.started"), ev("agent.decision", { tool: "browser_open" })], "running"), "36");
assert.strictEqual(M.replay([ev("task.started"), ev("task.finished")], "succeeded"), "02");
console.log("mate-map ok: " + cases.length + " event cases");
