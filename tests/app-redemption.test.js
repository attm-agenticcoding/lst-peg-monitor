/* Dependency-free UI-controller smoke test. Not a substitute for visual browser QA. */
"use strict";
const assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs");
const Core = require("../core.js"), config = require("../config.json"), LidoCutoff = require("../lido-cutoff.js"), cutoff = require("../data/lido-cutoff-snapshot.json");
// Pure display arithmetic, independent of net-cost economics and data collection.
const appSource = fs.readFileSync(require.resolve("../app.js"), "utf8");
const gross = vm.runInNewContext(appSource.slice(appSource.indexOf("  function stGrossComparison"), appSource.indexOf("  function stSection")) + "; stGrossComparison;");
const sample = { eth: 50, steth: 50.0115, totalDays: 3 };
assert.ok(Math.abs(gross(sample, 0.0224).bps - 2.3) < 1e-10);
assert.ok(Math.abs(gross(sample, 0.0224).annualized * 100 - 2.7983333333333333) < 1e-10);
assert.ok(Math.abs(gross({ ...sample, totalDays: 5 }, 0.0224).annualized * 100 - 1.679) < 1e-10);
assert.ok(Math.abs(gross(sample, 0.0224).vsStaking * 100 - 0.5583333333333333) < 1e-10);
assert.ok(Math.abs(gross({ ...sample, totalDays: 3 + 2 / 24 }, 0.0224).annualized * 100 - 2.722702702702703) < 1e-10);
for (const totalDays of [null, 0, -1, NaN, Infinity]) assert.equal(gross({ ...sample, totalDays }, 0.0224).annualized, null);
assert.equal(gross({ ...sample, steth: null }, 0.0224).bps, null);
assert.equal(gross(sample, null).vsStaking, null);
assert.ok(gross({ ...sample, steth: 49.99 }, 0.0224).annualized < 0);
const ids = new Map();
const element = (id) => {
  if (!ids.has(id)) ids.set(id, { id, focus() { ctx.document.activeElement = this; }, innerHTML: "", textContent: "", value: "", hidden: false, disabled: false, events: {},
    addEventListener(type, fn) { this.events[type] = fn; } });
  return ids.get(id);
};
const form = element("st-settings");
const html = fs.readFileSync(require.resolve("../index.html"), "utf8");
form.elements = [...html.matchAll(/<input name="([^"]+)"/g)].map((m) => ({ name: m[1], value: "" }));
for (const el of form.elements) form.elements[el.name] = el;
form.reportValidity = () => true;
let unavailable = false, fail = false, paused = false, bunker = false, grossQuoteReturn = 0.001, quotes = 0, currentTime = Date.now();
class TestDate extends Date { static now() { return currentTime; } }
const intervals = [];
let cutoffPending = false;
const collect = async (cfg) => {
  quotes++;
  if (fail) throw new Error("test source failed");
  const now = Math.floor(TestDate.now() / 1000), st = { kyber: {}, quoteMeta: { kyber: {} }, waits: {}, gasGwei: 10, gasAt: now, apr: 3, aprAt: now,
    queue: { at: now, blockTimestamp: now, blockNumber: 25000000, bunker, paused, unfinalizedSteth: 10000 } };
  for (const x of cfg.stRedeem.sizes) {
    const got = x * (1 + grossQuoteReturn); st.kyber[x] = got; st.quoteMeta.kyber[x] = { quotedAt: now };
    st.waits[x] = unavailable ? { status: "unavailable", reason: "HTTP 403" } : { amountSteth: got, waitDays: 2, calculatedAt: now, fetchedAt: now, type: "buffer", status: "calculated" };
  }
  return { ts: now, nav: { stETH: 1, wstETH: 1.24, cbETH: 1.15 }, px: { stETH: {}, cbETH: {} }, st, rt: {}, err: [] };
};
const documentEvents = {};
const ctx = { window: { LidoCutoff, PegCore: Object.assign({}, Core, { collect }) }, document: { getElementById: element, addEventListener(type, fn) { documentEvents[type] = fn; }, hidden: false },
  AbortController, setTimeout(fn, ms) { const timer = setTimeout(fn, ms); timer.unref(); return timer; }, clearTimeout,
  fetch: async (url) => cutoffPending && url === "data/lido-cutoff-snapshot.json" ? new Promise(() => {}) : url === "config.json" ? { json: async () => config } : url === "data/lido-cutoff-snapshot.json" ? { ok: true, json: async () => cutoff } : { ok: false },
  setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; }, clearInterval() {}, Date: TestDate, console };
const flush = () => new Promise((r) => setImmediate(r));
const submit = () => form.events.submit({ preventDefault() {} });
(async () => {
  vm.runInNewContext(fs.readFileSync(require.resolve("../app.js"), "utf8"), ctx);
  await flush();
  assert.ok(element("scenarios").innerHTML.includes("净简单年化 APR"));
  assert.ok(!element("cards").innerHTML.includes("st-redemption"), "direct sell cards contain no redemption scenarios");
  assert.ok(!element("scenarios").innerHTML.includes('id="st-redemption" open'), "scenario starts collapsed");
  assert.ok(!element("scenarios").innerHTML.includes('id="st-redemption-details" open'), "explanatory details start collapsed");
  assert.ok(element("scenarios").innerHTML.includes("ETH → stETH</th>"), "conversion is visible in the main row");
  assert.ok(element("scenarios").innerHTML.includes("兑换价差</th>"), "gross spread has explicit bps units");
  assert.ok(element("scenarios").innerHTML.includes("质押 APR · 7日均值"), "benchmark specifies APR and averaging window");
  assert.ok(element("scenarios").innerHTML.includes("扣成本后"), "post-cost APR remains separate");
  const mainCopy = element("scenarios").innerHTML.split('<details class="redemption-details"')[0];
  assert.ok(!/保守|未验证|待校准/.test(mainCopy), "primary view avoids unexplained validation jargon");
  assert.ok(element("scenarios").innerHTML.includes("实际等待或兑付额变化会改变收益"), "short conditional warning remains visible when expanded");
  assert.ok(element("clock").textContent.endsWith(" ET"));
  assert.ok(html.indexOf('id="cards"') < html.indexOf('id="scenarios"'));
  assert.ok(html.indexOf('id="scenarios"') < html.indexOf('id="cutoff-section"'));
  element("st-redemption").open = true;
  element("st-redemption-details").open = true;
  assert.ok(element("lido-cutoff").innerHTML.includes("10/07"));
  assert.ok(element("lido-cutoff").innerHTML.includes("已过期"));
  element("cutoff-details").open = true;
  assert.ok(element("scenarios").innerHTML.includes("内部队列/validator 快照时间：未暴露"));
  grossQuoteReturn = 0.00023;
  form.elements.amount.value = "50"; form.elements.manualWaitDays.value = "3"; form.elements.extraHours.value = "0";
  submit(); await flush();
  const samplePipeline = element("scenarios").innerHTML.split('<details class="redemption-details"')[0];
  for (const term of ["50.00 ETH", "50.0115 stETH", "+2.30", "+2.80%", "扣成本后", "全周期假设 x"]) assert.ok(samplePipeline.includes(term), term);
  form.elements.manualWaitDays.value = "5"; submit(); await flush();
  assert.ok(element("scenarios").innerHTML.split('<details class="redemption-details"')[0].includes("+1.68%"));
  grossQuoteReturn = 0.001; element("st-reset").events.click(); await flush();
  form.elements.amount.value = "1.234"; submit(); await flush();
  assert.ok(element("scenarios").innerHTML.includes("1.23"));
  form.elements.manualWaitDays.value = "2"; form.elements.manualConservativeDays.value = "1";
  submit(); assert.ok(element("st-settings-status").textContent.includes("不能短于"));
  form.elements.manualConservativeDays.value = "5"; submit(); await flush();
  assert.ok(element("scenarios").innerHTML.includes("手动假设"));
  assert.ok(element("scenarios").innerHTML.includes("资料不足 · 不提示操作"));
  form.elements.amount.value = "10"; await element("refresh").events.click();
  assert.equal(form.elements.amount.value, "10", "unsaved edits survive background card replacement");
  element("st-reset").events.click(); await flush();
  assert.equal(form.elements.amount.value, "");
  assert.ok(element("scenarios").innerHTML.includes("300.00"));
  unavailable = true; await element("refresh").events.click();
  assert.ok(element("scenarios").innerHTML.includes("HTTP 403"));
  assert.ok(!element("scenarios").innerHTML.includes("Infinity"));
  const before = quotes; const p = element("refresh").events.click(); element("refresh").events.click(); await p;
  assert.equal(quotes, before + 1, "repeated refresh does not duplicate collection");
  assert.equal(element("cutoff-details").open, true, "open cutoff detail survives refresh");
  assert.equal(element("st-redemption").open, true, "open scenario survives refresh");
  assert.equal(element("st-redemption-details").open, true, "open explanation survives refresh");
  unavailable = false; paused = true; bunker = true; await element("refresh").events.click();
  const visibleMain = element("scenarios").innerHTML.split('<details class="redemption-details"')[0];
  assert.ok(visibleMain.includes("提现暂停"), "operational pause remains above collapsed details");
  assert.ok(visibleMain.includes("Bunker 模式"), "Bunker warning remains above collapsed details");
  paused = false; bunker = false; await element("refresh").events.click();
  assert.ok(element("scenarios").innerHTML.includes("情景达标 · 待校准"));
  documentEvents.click({ target: { id: "st-edit-settings" } });
  assert.equal(element("st-settings-panel").open, true, "edit link opens settings disclosure");
  element("st-redemption-details-summary").focus();
  currentTime += 301000;
  intervals.find((x) => x.ms === 10000).fn();
  assert.ok(element("st-redemption").outerHTML.includes("已过期"));
  assert.equal(ctx.document.activeElement.id, "st-redemption-details-summary", "expiry redraw restores disclosure focus");
  assert.ok(!element("st-redemption").outerHTML.includes("情景达标 · 待校准"));
  cutoffPending = true;
  const beforePending = quotes;
  await element("refresh").events.click();
  assert.equal(quotes, beforePending + 1, "hung static source does not block live collection");
  assert.equal(element("refresh").disabled, false, "hung static source does not lock refresh");
  cutoffPending = false;
  currentTime += 301000;
  fail = true; await element("refresh").events.click();
  assert.ok(element("st-redemption").outerHTML.includes("无新鲜有效买入报价"));
  assert.ok(!element("st-redemption").outerHTML.includes("情景达标 · 待校准"));
  assert.ok(element("lido-cutoff").innerHTML.includes("10/07"), "fixed scenario remains visible on live refresh failure");
  assert.ok(element("lido-cutoff").innerHTML.includes("当前报价读取不可用"));
  console.log("UI controller smoke passed: render, decimal amount, invalid/manual scenario, refresh/reset, edit preservation, unavailable ETA, double refresh, wall-clock expiry and failed refresh");
})().catch((e) => { console.error(e); process.exitCode = 1; });
