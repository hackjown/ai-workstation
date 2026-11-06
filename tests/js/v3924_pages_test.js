// v3.9.24: every 进阶功能 page renders from sample API answers without throwing (node, no browser).
// run: node tests/js/v3924_pages_test.js apps/web/v3924.js
const fs = require("fs");
const vm = require("vm");

const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", value: "", checked: false, textContent: "", style: {},
  appendChild() {}, insertAdjacentHTML(_, h) { this.innerHTML += h; }, querySelector() { return null; }, remove() {} });
const now = Date.now() / 1000;
const DATA = {
  "GET /api/autoroute": { enabled: true, use_model: false, apply_chat: false, env: "", tiers: { easy: { model: "", effort: "off" }, normal: { model: "", effort: "off" }, hard: { model: "big", effort: "high" } }, rules: [{ pattern: "周报", tier: "easy" }] },
  "GET /api/models": { models: ["a", "b"] },
  "GET /api/autoreview": { enabled: true, use_model: true, model: "", model_can_deny: true, allow: ["shell:git *"], deny: ["*rm -rf /*"] },
  "GET /api/autoreview/log?limit=60": [{ ts: now, verdict: "allow", by: "rule", reason: "x", subject: "shell git status" }],
  "GET /api/webhooks": [{ id: "wh1", name: "ci", enabled: 1, path: "/api/webhooks/wh1", calls: 2, last_call: now }],
  "GET /api/machines": [{ id: "ai-linux-001", name: "本地" }],
  "GET /api/usage?days=14": { recording: true, total: { total: 30, input: 20, output: 10, calls: 2 }, per_day: [{ day: "2026-11-06", total: 30, input: 20, output: 10, models: [{ model: "m", total: 30 }] }],
    per_model: [{ model: "m", input: 20, output: 10, calls: 2 }], per_role: [{ role: "main", input: 20, output: 10, calls: 2 }], top_tasks: [] },
  "GET /api/apps": { enabled: true, apps: [{ id: "x", name: "X", kind: "mcp", version: "1", description: "d", source: "agent", enabled: false, secrets: [{ name: "app_x_key", set: false }] }] },
  "GET /api/prompts": [{ role: "task_agent", title: "任务 Agent", desc: "d", active: null, versions: [] }],
  "GET /api/tasks?limit=40": [{ id: "t1", prompt: "p", status: "succeeded" }],
  "GET /api/model_context": { models: { deepseek: { window: 64000, trigger_ratio: 0.6 } }, off: false },
  "GET /api/teach": { recordings: [{ id: "teach-1", title: "演示", started: now, events: 3, shots: 1 }], active: {} },
  "GET /api/remote": { port: 8000, lan_ips: ["172.17.0.2"], in_docker: true, lan_url: "", tailscale: { cli: false }, tailscale_url: "", custom_url: "", tunnel_autostart: false,
    auth_ok: true, tunnel_allowed: true, tunnel: { running: false, url: null, error: null, binary: false, log: [] } },
  "GET /api/checkin/sites": { sites: [{ id: "s1", name: "站", url: "https://a", enabled: 1, has_recipe: true, recipe: { kind: "browser" }, schedule: "daily 08:30", jitter_min: 15, next_run: now, last_status: "ok" }], settings: { enabled: true, jitter_min: 15 }, env_off: false },
  "GET /api/checkin/runs?limit=40": [{ ts: now, site_id: "s1", mode: "replay", status: "ok", message: "签到成功" }],
};
const ctx = {
  console, setTimeout() {}, setInterval() {}, clearInterval() {}, clearTimeout() {}, confirm: () => true,
  document: { getElementById: el, createElement: () => el("_new" + Math.random()), head: { appendChild() {} }, body: { appendChild() {} }, querySelector: () => null },
  $: el, toast() {}, TOKEN: "tok", location: { origin: "http://aiw.local" }, navigator: {},
  loadSettings: async () => {},
  api: async (m, p) => { const k = `${m} ${p}`; if (!(k in DATA)) throw new Error("no fixture for " + k); return DATA[k]; },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
vm.runInContext(`
(async () => {
  const want = { route: "三档", review: "拒绝规则", webhook: "X-AIW-Signature", usage: "按模型", apps: "app_x_key", prompts: "任务 Agent",
                 context: "deepseek", teach: "演示", remote: "trycloudflare", checkin: "立即签到" };
  for (const [k, needle] of Object.entries(want)) {
    els.v24Body = undefined;
    await V24_RENDER[k]();
    const h = $("v24Body").innerHTML;
    if (!h.includes(needle)) throw new Error(k + " page lacks " + needle);
  }
  console.log("ok " + Object.keys(want).length);
})().catch((e) => { console.error(e.stack || e); process.exitCode = 1; });
`, Object.assign(ctx, { els, process }));
