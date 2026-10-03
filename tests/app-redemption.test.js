/* Dependency-free UI-controller smoke test. Not a substitute for visual browser QA. */
"use strict";
const assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs");
const Core = require("../core.js"), config = require("../config.json");
const ids = new Map();
const element = (id) => {
  if (!ids.has(id)) ids.set(id, { innerHTML: "", textContent: "", value: "", hidden: false, disabled: false, events: {},
    addEventListener(type, fn) { this.events[type] = fn; } });
  return ids.get(id);
};
const form = element("st-settings");
const html = fs.readFileSync(require.resolve("../index.html"), "utf8");
form.elements = [...html.matchAll(/<input name="([^"]+)"/g)].map((m) => ({ name: m[1], value: "" }));
for (const el of form.elements) form.elements[el.name] = el;
form.reportValidity = () => true;
let unavailable = false, fail = false, quotes = 0, currentTime = Date.now();
class TestDate extends Date { static now() { return currentTime; } }
const intervals = [];
const collect = async (cfg) => {
  quotes++;
  if (fail) throw new Error("test source failed");
  const now = Math.floor(TestDate.now() / 1000), st = { kyber: {}, quoteMeta: { kyber: {} }, waits: {}, gasGwei: 10, gasAt: now, apr: 3, aprAt: now,
    queue: { at: now, blockTimestamp: now, blockNumber: 25000000, bunker: false, paused: false, unfinalizedSteth: 10000 } };
  for (const x of cfg.stRedeem.sizes) {
    const got = x * 1.001; st.kyber[x] = got; st.quoteMeta.kyber[x] = { quotedAt: now };
    st.waits[x] = unavailable ? { status: "unavailable", reason: "HTTP 403" } : { amountSteth: got, waitDays: 2, calculatedAt: now, fetchedAt: now, type: "buffer", status: "calculated" };
  }
  return { ts: now, nav: { stETH: 1, wstETH: 1.24, cbETH: 1.15 }, px: { stETH: {}, cbETH: {} }, st, rt: {}, err: [] };
};
const ctx = { window: { PegCore: Object.assign({}, Core, { collect }) }, document: { getElementById: element, addEventListener() {}, hidden: false },
  fetch: async (url) => url === "config.json" ? { json: async () => config } : { ok: false },
  setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; }, clearInterval() {}, Date: TestDate, console };
const flush = () => new Promise((r) => setImmediate(r));
const submit = () => form.events.submit({ preventDefault() {} });
(async () => {
  vm.runInNewContext(fs.readFileSync(require.resolve("../app.js"), "utf8"), ctx);
  await flush();
  assert.ok(element("cards").innerHTML.includes("净简单年化 APR"));
  assert.ok(element("cards").innerHTML.includes("内部队列/validator 快照时间：未暴露"));
  form.elements.amount.value = "1.234"; submit(); await flush();
  assert.ok(element("cards").innerHTML.includes("1.23"));
  form.elements.manualWaitDays.value = "2"; form.elements.manualConservativeDays.value = "1";
  submit(); assert.ok(element("st-settings-status").textContent.includes("不能短于"));
  form.elements.manualConservativeDays.value = "5"; submit(); await flush();
  assert.ok(element("cards").innerHTML.includes("手动假设"));
  assert.ok(element("cards").innerHTML.includes("资料不足 · 不提示操作"));
  form.elements.amount.value = "10"; await element("refresh").events.click();
  assert.equal(form.elements.amount.value, "10", "unsaved edits survive background card replacement");
  element("st-reset").events.click(); await flush();
  assert.equal(form.elements.amount.value, "");
  assert.ok(element("cards").innerHTML.includes("300.00"));
  unavailable = true; await element("refresh").events.click();
  assert.ok(element("cards").innerHTML.includes("HTTP 403"));
  assert.ok(!element("cards").innerHTML.includes("Infinity"));
  const before = quotes; const p = element("refresh").events.click(); element("refresh").events.click(); await p;
  assert.equal(quotes, before + 1, "repeated refresh does not duplicate collection");
  unavailable = false; await element("refresh").events.click();
  assert.ok(element("cards").innerHTML.includes("情景达标 · 待校准"));
  currentTime += 301000;
  intervals.find((x) => x.ms === 10000).fn();
  assert.ok(element("st-redemption").outerHTML.includes("已过期"));
  assert.ok(!element("st-redemption").outerHTML.includes("情景达标 · 待校准"));
  fail = true; await element("refresh").events.click();
  assert.ok(element("st-redemption").outerHTML.includes("无新鲜有效买入报价"));
  assert.ok(!element("st-redemption").outerHTML.includes("情景达标 · 待校准"));
  console.log("UI controller smoke passed: render, decimal amount, invalid/manual scenario, refresh/reset, edit preservation, unavailable ETA, double refresh, wall-clock expiry and failed refresh");
})().catch((e) => { console.error(e); process.exitCode = 1; });
