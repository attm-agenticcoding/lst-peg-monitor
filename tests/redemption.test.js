"use strict";
const assert = require("node:assert/strict");
const R = require("../redemption.js");
const now = 1800000000;
const cfg = { stRedeem: { sizes: [100], minProfitEth: 0.005 } };
const wait = (amount = 100.1, days = 2) => ({ amountSteth: amount, waitDays: days, status: "calculated", type: "buffer", calculatedAt: now, fetchedAt: now });
const data = () => ({ kyber: { 100: 100.1 }, quoteMeta: { kyber: { 100: { quotedAt: now, gasUnits: 350000 } } },
  waits: { 100: wait() }, gasGwei: 10, gasAt: now, apr: 3, aprAt: now, queue: { bunker: false, paused: false, at: now, blockTimestamp: now } });
const row = (q = data(), options = {}) => R.estimate(q, { stRedeem: Object.assign({}, cfg.stRedeem, options) }, now).rows[0];
const close = (a, b, tolerance = 1e-10) => assert.ok(Math.abs(a - b) < tolerance, `${a} != ${b}`);
let count = 0;
function t(name, f) { f(); count++; console.log("ok - redemption:", name); }
t("all-in cost includes swap, approval, request and claim gas", () => {
  const r = row();
  close(r.gasEth, 0.0071);
  close(r.expected.cost, 100.0071);
  close(r.expected.profit, 0.0929);
  close(r.expected.netReturn, 0.0929 / 100.0071);
  close(r.expected.apr, r.expected.netReturn * 365 / (2 + 2 / 24));
});
t("quote already includes pool fee/size impact; no double fee or staking rewards", () => {
  const q = data(); q.apr = 100;
  const a = row(q), b = row();
  close(a.expected.profit, b.expected.profit);
  close(a.back, 100.1);
});
t("par plus gas is negative, never a positive scenario", () => {
  const q = data(); q.kyber[100] = 100; q.waits[100] = wait(100);
  assert.ok(row(q).expected.profit < 0); assert.equal(row(q).scenarioPasses, false);
});
t("small discount erased by gas", () => {
  const q = data(); q.kyber[100] = 100.001; q.waits[100] = wait(100.001);
  assert.ok(row(q).expected.profit < 0);
});
t("positive scenario is never an actionable trade instruction", () => {
  const r = row(); assert.equal(r.signal, "scenario_pass"); assert.equal(r.actionable, false);
  assert.ok(r.reasons.some((s) => s.includes("尚未校准")));
});
t("doubling wait halves simple annualization", () => {
  const q = data(); const a = row(q, { extraHours: 0 }); q.waits[100].waitDays = 4;
  close(row(q, { extraHours: 0 }).expected.apr, a.expected.apr / 2);
});
t("conservative scenario uses longer wait, slippage, gas and optional haircut", () => {
  const r = row(data(), { haircutBps: 20 });
  assert.ok(r.conservative.profit < 0); assert.equal(r.scenarioPasses, false);
  assert.ok(r.conservativeDays > r.totalDays);
});
t("manual ETA gives scenario arithmetic but suppresses signal", () => {
  const q = data(); q.waits = {};
  const r = row(q, { manualWaitDays: 2, manualConservativeDays: 5 });
  assert.equal(r.waitSource, "manual"); close(r.conservativeDays, 5 + 2 / 24);
  assert.equal(r.signal, "unavailable"); assert.equal(r.actionable, false);
});
t("zero negative and NaN waiting periods never annualize", () => {
  for (const v of [0, -1, NaN]) {
    const q = data(); q.waits[100].waitDays = v;
    assert.equal(row(q).expected.apr, null);
  }
});
t("403 missing or mismatched amount-specific ETA does not use gross max-size ETA", () => {
  const q = data(); q.waits[100] = { status: "unavailable", reason: "HTTP 403" }; q.waitMs = 86400000;
  assert.equal(row(q).expected.apr, null);
  q.waits[100] = wait(300);
  assert.equal(row(q).expected.apr, null);
});
t("stale response calculation timestamp blocks ETA even after a fresh fetch", () => {
  const q = data(); q.waits[100].calculatedAt = now - 301;
  assert.equal(row(q).waitSource, "unavailable"); assert.equal(row(q).expected.apr, null);
});
t("stale quote gas APR and missing protocol status suppress signal", () => {
  for (const change of [q => q.quoteMeta.kyber[100].quotedAt = now - 181,
    q => q.gasAt = now - 181, q => q.aprAt = now - 86401, q => q.queue = null]) {
    const q = data(); change(q); assert.equal(row(q).signal, "unavailable");
  }
});
t("paused or bunker protocol status suppresses signal", () => {
  for (const key of ["paused", "bunker"]) { const q = data(); q.queue[key] = true; assert.equal(row(q).signal, "unavailable"); }
});
t("unknown or invalid official ETA schema rejected", () => {
  for (const d of [{}, { status: "calculating" }, { status: "finalized" },
    { status: "calculated", requestInfo: { finalizationIn: -1 } }]) {
    assert.notEqual(R.parseWait(d, 10, now).status, "calculated");
  }
});
t("milliseconds interpreted correctly; nextCalculationAt not treated as freshness", () => {
  const r = R.parseWait({ status: "calculated", requestInfo: { finalizationIn: 2 * 864e5,
    finalizationAt: new Date((now + 2 * 86400) * 1000).toISOString(), type: "buffer" }, nextCalculationAt: "1990-01-01" }, 100, now);
  close(r.waitDays, 2); close(r.calculatedAt, now);
});
t("split requests increase request and claim gas budget", () => {
  const q = data(); q.kyber[100] = 1000.01; q.waits[100] = wait(1000.01);
  const r = row(q); assert.equal(r.gasUnits.requests, 2); assert.equal(r.gasUnits.total, 1010000);
});
t("best route selected by nominal gas-adjusted proceeds", () => {
  const q = data(); q.curve = { 100: 100.099 }; q.quoteMeta.curve = { 100: { quotedAt: now } };
  q.waits[100] = wait(100.099);
  assert.equal(row(q).via, "curve");
});
t("stale high-output Kyber quote falls back to fresh Curve before ETA selection", () => {
  const q = data(); q.quoteMeta.kyber[100].quotedAt = now - 181;
  q.curve = { 100: 100.05 }; q.quoteMeta.curve = { 100: { quotedAt: now } };
  q.waits[100] = wait(100.05);
  assert.equal(R.selectQuote(q, 100, cfg, now).via, "curve");
  assert.equal(row(q).via, "curve"); assert.equal(row(q).waitSource, "official_unvalidated");
});
t("missing gas does not pretend net equals gross", () => {
  const q = data(); delete q.gasGwei;
  assert.equal(row(q).expected, null); assert.equal(row(q).signal, "unavailable");
});
t("invalid scenario parameters do not yield arithmetic", () => {
  for (const options of [{ slippageBps: -1 }, { haircutBps: 10000 }, { gasMultiplier: 0 }, { manualWaitDays: 0 }])
    assert.equal(row(data(), options).expected, null);
});
t("decimal ETH inputs use exact wei conversion", () => {
  assert.equal(R.toWei("1.2345"), 1234500000000000000n);
  for (const invalid of ["0", "-1", "NaN", "1e-3", "1.0000000000000000001"]) assert.throws(() => R.toWei(invalid));
});
console.log(`${count} redemption tests passed`);
