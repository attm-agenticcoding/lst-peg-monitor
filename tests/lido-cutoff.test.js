"use strict";
const assert = require("node:assert/strict");
const L = require("../lido-cutoff.js"), s = require("../data/lido-cutoff-snapshot.json"), config = require("../config.json");
const t = Date.parse(s.asOf) / 1000, clone = (x) => JSON.parse(JSON.stringify(x));
let count = 0;
function test(name, fn) { fn(); count++; console.log("ok", name); }
const cfg = { ...config, stRedeem: { ...config.stRedeem, sizes: [99.9], slippageBps: 0, haircutBps: 0 } };
const quote = () => ({ kyber: { 99.9: 100 }, quoteMeta: { kyber: { 99.9: { quotedAt: t } } }, gasGwei: 10, gasAt: t,
  apr: 3, aprAt: t, queue: { blockNumber: s.blockNumber, blockTimestamp: t, at: t, paused: false, bunker: false } });
test("exact public fixture, six independent tiers, authenticated snapshot", () => {
  assert(L.validate(s)); assert.equal(s.blockNumber, 26120128); assert.equal(s.pendingSteth, "147365.696918335063465874");
  assert.equal(s.physicalCashEth, "3586.962265060984543582"); assert.deepEqual(s.tiers.map((x) => x.steth), [100, 200, 300, 500, 1000, 1500]);
  assert.deepEqual(s.tiers[5].split, [1000, 500]);
  for (const r of s.tiers) { assert.equal(r.main.reference_time_utc, "2026-10-07T12:00:11Z"); assert.equal(r.stress.reference_time_utc, "2026-10-09T12:00:11Z"); }
});
test("daily cutoffs preserve exact decimal strings", () => {
  assert.deepEqual(s.dailyCutoffs.map((x) => x.mainSteth), ["0.000000000000000000", "0.000000000000000000", "7541.174114849921077708", "36803.194551070921077708", "41848.048846979921077708"]);
  assert.equal(s.dailyCutoffs[4].stressSteth, "9584.582771778921077708");
});
test("same amount/time scenario uses gas and total capital horizon", () => {
  const r = L.evaluate(s, quote(), cfg, t).rows[0], e = r.mainEconomics;
  const days = 243828 / 86400 + cfg.stRedeem.extraHours / 24;
  assert(e); assert.equal(r.totalDays.main, days); assert.equal(e.cost, 99.9 + e.gasEth); assert.equal(e.profit, 100 - e.cost);
  assert(Math.abs(e.apr - e.profit / e.cost * 365 / days) < 1e-14); assert.equal(e.stakingProfit, e.cost * .03 * (days / 365));
  assert.equal(e.excessProfit, e.profit - e.stakingProfit); assert.equal(r.actionable, false);
  assert.equal(r.stressEconomics.gasEth, e.gasEth * cfg.stRedeem.gasMultiplier);
});
test("1500 stETH exact quote budgets two requests", () => {
  const q = quote(); q.kyber = { 1499: 1500 }; q.quoteMeta.kyber = { 1499: { quotedAt: t } };
  const c = { ...cfg, stRedeem: { ...cfg.stRedeem, sizes: [1499] } };
  const r = L.evaluate(s, q, c, t).rows[5]; assert(r.mainEconomics); assert.equal(r.quote.gas.requests, 2);
});
test("stETH amount is not ETH input and cannot scale another quote", () => {
  const q = quote(); q.kyber[99.9] = 100.01;
  const r = L.evaluate(s, q, cfg, t).rows[0]; assert.equal(r.mainEconomics, null); assert(r.reasons.some((x) => x.includes("实际买到")));
});
test("stale fixture, fresh prices never rejuvenate scenario", () => {
  const q = quote(); q.gasAt = q.aprAt = q.quoteMeta.kyber[99.9].quotedAt = t + 7200;
  const v = L.evaluate(s, q, cfg, t + 7200); assert(v.stale); assert.equal(v.rows[0].mainEconomics, null);
  assert.equal(v.rows[0].main.reference_time_utc, s.tiers[0].main.reference_time_utc);
});
test("clock never shortens scenario capital time", () => {
  const a = L.evaluate(s, quote(), cfg, t), b = L.evaluate(s, quote(), cfg, t + 1000000);
  assert.deepEqual(a.rows.map((r) => r.totalDays), b.rows.map((r) => r.totalDays));
});
test("future-dated fixture rejected for economics", () => { assert(L.evaluate(s, quote(), cfg, t - 1).stale); });
test("queue block and timestamp both must match", () => {
  for (const field of ["blockNumber", "blockTimestamp"]) { const q = quote(); q.queue[field]++; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null); }
});
test("paused, Bunker, unknown mode and failed fetch all suppress yield", () => {
  for (const field of ["paused", "bunker"]) { const q = quote(); q.queue[field] = true; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null); q.queue[field] = null; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null); }
  const q = quote(); q.refreshFailed = true; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null);
  assert.equal(L.evaluate(s, quote(), cfg, t, true).rows[0].mainEconomics, null);
});
test("missing or stale gas and APR suppress all scenario economics", () => {
  for (const field of ["gasAt", "aprAt"]) { const q = quote(); q[field] = 0; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null); }
  for (const field of ["gasGwei", "apr"]) { const q = quote(); q[field] = null; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null); }
});
test("stale quote is missing rather than synthetic", () => { const q = quote(); q.quoteMeta.kyber[99.9].quotedAt -= 181; assert.equal(L.evaluate(s, q, cfg, t).rows[0].mainEconomics, null); });
test("malformed fixture and split fail closed", () => {
  for (const mutate of [(x) => x.live = true, (x) => x.calibrated = true, (x) => x.asOf = "bad", (x) => x.maxEconomicAgeSeconds = 999999,
    (x) => x.tiers[0].split = [101], (x) => x.tiers[0].main.elapsed_to_reference_seconds--, (x) => x.dailyCutoffs[0].mainSteth = "NaN"]) {
    const x = clone(s); mutate(x); assert.equal(L.evaluate(x, quote(), cfg, t).valid, false);
  }
});
test("invalid economics parameters do not print NaN or Infinity", () => {
  const c = { ...cfg, stRedeem: { ...cfg.stRedeem, extraHours: -1 } }; const v = L.evaluate(s, quote(), c, t);
  assert.equal(v.rows[0].mainEconomics, null); assert(!L.render(v).includes("NaN"));
});
test("UI shows UTC, staleness, limitations, exact six tiers and expandable details", () => {
  const html = L.render(L.evaluate(s, null, cfg, t + 7200));
  for (const term of ["2026-10-04 16:16:23 UTC", "10/07", "10/09", "已过期", "不是到账承诺", "1,500", "cutoff-details", "cutoff-economics", "样本外预测校准", "固定快照已过期"]) assert(html.includes(term), term);
  assert(!html.includes("NaN")); assert(!html.includes("Infinity"));
});
test("missing fixture leaves prices available and explains error", () => { const v = L.evaluate(null, null, cfg, t); assert.equal(v.valid, false); assert(L.render(v).includes("实时价格仍独立刷新")); });
console.log(`${count} cutoff integration tests passed`);
