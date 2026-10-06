"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs"), vm = require("node:vm");
const L = require("../lido-cutoff.js"), T = require("../display-time.js"), s = require("./fixtures/lido-cutoff-20261004.json"), config = require("../config.json");
const t = Date.parse(s.asOf) / 1000, clone = (x) => JSON.parse(JSON.stringify(x));
let count = 0;
function test(name, fn) { fn(); count++; console.log("ok", name); }
const cfg = { ...config, stRedeem: { ...config.stRedeem, sizes: [99.9], slippageBps: 0, haircutBps: 0 } };
const quote = () => ({ kyber: { 99.9: 100 }, quoteMeta: { kyber: { 99.9: { quotedAt: t } } }, gasGwei: 10, gasAt: t,
  apr: 3, aprAt: t, queue: { blockNumber: s.blockNumber, blockTimestamp: t, at: t, paused: false, bunker: false } });
test("Eastern timestamp helper uses automatic winter EST and summer EDT", () => {
  assert.equal(T.TIME_ZONE, "America/New_York");
  assert.equal(T.formatTimestamp("2026-01-15T12:34:56Z"), "2026-01-15 07:34:56 EST");
  assert.equal(T.formatTimestamp("2026-07-15T12:34:56Z"), "2026-07-15 08:34:56 EDT");
  assert.equal(T.formatDate("2026-01-15T12:34:56Z"), "2026/01/15 EST");
  assert.equal(T.formatDate("2026-07-15T12:34:56Z"), "2026/07/15 EDT");
  assert.equal(T.formatClock("2026-01-15T12:34:56Z"), "07:34:56 EST");
  assert.equal(T.formatClock("2026-07-15T12:34:56Z"), "08:34:56 EDT");
  assert.equal(T.formatShort("2026-01-15T12:34:56Z"), "01/15 07:34 EST");
  assert.equal(T.formatShort("2026-07-15T12:34:56Z"), "07/15 08:34 EDT");
});
test("Eastern spring-forward transition skips the missing hour exactly", () => {
  assert.equal(T.formatTimestamp("2026-03-08T06:59:59Z"), "2026-03-08 01:59:59 EST");
  assert.equal(T.formatTimestamp("2026-03-08T07:00:00Z"), "2026-03-08 03:00:00 EDT");
});
test("Eastern fall-back transition distinguishes the repeated hour exactly", () => {
  assert.equal(T.formatTimestamp("2026-11-01T05:59:59Z"), "2026-11-01 01:59:59 EDT");
  assert.equal(T.formatTimestamp("2026-11-01T06:00:00Z"), "2026-11-01 01:00:00 EST");
  assert.equal(T.formatClock("2026-11-01T05:30:00Z"), "01:30:00 EDT");
  assert.equal(T.formatClock("2026-11-01T06:30:00Z"), "01:30:00 EST");
});
test("Eastern dates cross UTC midnight, including the year boundary and local midnight", () => {
  assert.equal(T.formatTimestamp("2026-01-01T00:00:00Z"), "2025-12-31 19:00:00 EST");
  assert.equal(T.formatDate("2026-01-01T00:00:00Z"), "2025/12/31 EST");
  assert.equal(T.formatTimestamp("2026-07-15T00:00:00Z"), "2026-07-14 20:00:00 EDT");
  assert.equal(T.formatDate("2026-07-15T00:00:00Z"), "2026/07/14 EDT");
  assert.equal(T.formatShort("2026-07-15T00:00:00Z"), "07/14 20:00 EDT");
  assert.equal(T.formatClock("2026-07-15T04:00:00Z"), "00:00:00 EDT");
  assert.equal(T.formatClock("2026-01-15T05:00:00Z"), "00:00:00 EST");
});
test("display helper supports browser and Node, explicit milliseconds, and safe missing times", () => {
  const context = { self: {} };
  vm.runInNewContext(fs.readFileSync(require.resolve("../display-time.js"), "utf8"), context);
  const iso = "2026-10-04T19:23:45Z", date = new Date(iso), before = date.getTime();
  for (const helper of [T, context.self.DisplayTime]) {
    for (const input of [iso, date, before]) assert.equal(helper.formatTimestamp(input), "2026-10-04 15:23:45 EDT");
    for (const input of [null, undefined, "", "bad", NaN, Infinity, false, {}, Symbol("invalid"), new Date(NaN)]) {
      for (const method of ["formatTimestamp", "formatDate", "formatClock", "formatShort"]) assert.equal(helper[method](input), "—");
    }
  }
  assert.equal(date.getTime(), before, "formatting does not mutate its Date input");
});
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
test("UI shows Eastern timestamps, staleness, limitations, exact six tiers and expandable details", () => {
  const html = L.render(L.evaluate(s, null, cfg, t + 7200));
  for (const term of ["快照来源时点 2026-10-04 12:16:23 EDT", "2026/10/07 EDT", "2026/10/09 EDT", "2026-10-07 08:00:11 EDT", "美东参考时间（EDT/EST）", "已过期", "不是到账承诺", "1,500", "cutoff-details", "cutoff-economics", "样本外预测校准", "固定快照已过期"]) assert(html.includes(term), term);
  assert(!html.includes("UTC")); assert(!html.includes(s.asOf));
  assert(!html.includes("NaN")); assert(!html.includes("Infinity"));
});
test("missing fixture leaves prices available and explains error", () => { const v = L.evaluate(null, null, cfg, t); assert.equal(v.valid, false); assert(L.render(v).includes("实时价格仍独立刷新")); });
test("current public file remains valid independently of historical regression fixture", () => {
  assert(L.validate(require("../data/lido-cutoff-snapshot.json")));
});
test("hourly model freshness is separate from quote compatibility", () => {
  const x = clone(s); x.refresh = { mode: "hourly", state: "ok" };
  const v = L.evaluate(x, quote(), cfg, t + 1200);
  assert(v.stale); assert.equal(v.sourceStale, false); assert.equal(v.rows[0].mainEconomics, null);
  assert(L.render(v).includes("每小时重算 · 非实时条件情景"));
  assert(L.evaluate(x, quote(), cfg, t + 5401).sourceStale);
});
test("failed hourly attempt retains old scenario and explicit failure", () => {
  const x = clone(s); x.refresh = { mode: "hourly", state: "error", attemptAt: "2026-10-04T18:00:00Z", lastSuccessAt: "2026-10-04T16:30:00Z", error: "source <failed>" };
  const v = L.evaluate(x, quote(), cfg, t);
  assert(v.refreshFailed); assert.equal(v.rows[0].mainEconomics, null);
  const html = L.render(v); assert(html.includes("保留上次成功快照")); assert(html.includes("source &lt;failed&gt;"));
  assert(html.includes("最近尝试 2026-10-04 14:00:00 EDT"));
  assert(html.includes("最近成功重算 2026-10-04 12:30:00 EDT"));
  assert(html.includes("快照来源时点 2026-10-04 12:16:23 EDT"));
  assert(!html.includes(x.refresh.attemptAt)); assert(!html.includes(x.refresh.lastSuccessAt));
  assert.equal(v.snapshot.asOf, s.asOf); assert.deepEqual(v.snapshot.tiers, s.tiers);
});
test("hourly success and source timestamps stay distinct and do not rejuvenate the snapshot", () => {
  const x = clone(s); x.refresh = { mode: "hourly", state: "ok", lastSuccessAt: "2026-10-04T18:00:00Z" };
  const before = JSON.stringify(x), v = L.evaluate(x, quote(), cfg, t + 7200), html = L.render(v);
  assert(html.includes("最近成功重算 2026-10-04 14:00:00 EDT"));
  assert(html.includes("快照来源时点 2026-10-04 12:16:23 EDT"));
  assert.equal(v.ageSeconds, 7200); assert(v.sourceStale); assert(v.stale);
  assert.equal(v.rows[0].mainEconomics, null);
  assert.equal(JSON.stringify(x), before, "neither evaluating nor displaying mutates raw ISO JSON");
  delete x.refresh.lastSuccessAt;
  assert(!L.render(L.evaluate(x, quote(), cfg, t)).includes("最近成功重算"), "missing success time is not inferred from asOf");
});
test("report dates and horizon use the Eastern calendar across UTC midnight", () => {
  const x = clone(s);
  for (const row of x.tiers) for (const scenario of [row.main, row.stress]) {
    scenario.reference_time_utc = scenario.reference_time_utc.replace("T12:", "T00:");
    scenario.elapsed_to_reference_seconds = Date.parse(scenario.reference_time_utc) / 1000 - t;
  }
  for (const row of x.dailyCutoffs) row.referenceTime = row.referenceTime.replace("T12:", "T00:");
  x.horizonEnd = "2026-10-18T00:00:11Z";
  const before = JSON.stringify(x), html = L.render(L.evaluate(x, null, cfg, t));
  assert(html.includes("2026/10/06 EDT · 第 3 批"));
  assert(html.includes("2026/10/08 EDT · 第 5 批"));
  assert(html.includes("2026-10-06 20:00:11 EDT"));
  assert(html.includes("模拟截止 2026-10-17 20:00:11 EDT"));
  assert.equal(JSON.stringify(x), before);
});
test("uncovered tier stays unknown rather than inventing a completion", () => {
  const x = clone(s); x.horizonEnd = "2026-10-18T12:00:11Z"; x.tiers[5].stress = null;
  assert(L.validate(x)); const v = L.evaluate(x, quote(), cfg, t);
  assert.equal(v.rows[5].totalDays.stress, null); assert.equal(v.rows[5].stressEconomics, null);
  assert(L.render(v).includes("模拟期内未覆盖")); assert(!L.render(v).includes("NaN"));
  delete x.horizonEnd; assert(!L.validate(x));
});
test("daily operating cadence never relaxes five-minute economics", () => {
  const x = clone(s); x.refresh = {mode: "daily", state: "ok", expectedIntervalSeconds: 86400, maxOperationalAgeSeconds: 91800};
  const v = L.evaluate(x, quote(), cfg, t + 7200);
  assert(v.daily); assert(v.scheduled); assert.equal(v.sourceStale, false); assert(v.stale);
  assert.equal(v.rows[0].mainEconomics, null);
  const html=L.render(v); assert(html.includes("每日 00:00 UTC 重算")); assert(html.includes("非实时"));
  assert(!html.includes("每小时")); assert(!html.includes("未自动重算"));
  assert(L.evaluate(x, quote(), cfg, t + 91801).sourceStale);
  assert.equal(v.snapshot.asOf, s.asOf); assert.deepEqual(v.snapshot.tiers, s.tiers);
});
test("failed daily attempt displays failure and original source", () => {
  const x=clone(s);x.refresh={mode:"daily",state:"error",attemptAt:"2026-10-05T00:00:00Z",error:"RPC timeout"};
  const html=L.render(L.evaluate(x, null, cfg, t+3600));
  assert(html.includes("重算失败"));assert(html.includes("RPC timeout"));assert(html.includes("2026-10-04 12:16:23 EDT"));
});
test("twice-daily UTC cadence retains original source and strict economics", () => {
  const x = clone(s); x.refresh = {mode: "twice-daily", state: "ok", scheduleUtc: ["12:00", "18:00"], expectedIntervalSeconds: 64800, maxOperationalAgeSeconds: 70200};
  const v = L.evaluate(x, quote(), cfg, t + 7200);
  assert(v.twiceDaily); assert(v.scheduled); assert(!v.hourly); assert(!v.daily);
  assert.equal(v.sourceStale, false); assert(v.stale);
  assert.equal(v.rows[0].mainEconomics, null);
  const html = L.render(v);
  assert(html.includes("每日 12:00 / 18:00 UTC 重算"));
  assert(html.includes("每天 12:00 和 18:00 UTC 尝试重算"));
  assert(!html.includes("00:00 UTC")); assert(!html.includes("每小时"));
  assert(!L.evaluate(x, null, cfg, t + 70200).sourceStale);
  assert(L.evaluate(x, null, cfg, t + 70201).sourceStale);
  x.refresh.maxOperationalAgeSeconds = 999999; // metadata cannot loosen the fixed UI gate
  assert(L.evaluate(x, null, cfg, t + 70201).sourceStale);
  assert.equal(v.snapshot.asOf, s.asOf); assert.deepEqual(v.snapshot.tiers, s.tiers);
});
test("failed twice-daily attempt retains error and old source", () => {
  const x = clone(s); x.refresh = {mode: "twice-daily", state: "error", attemptAt: "2026-10-07T12:00:17Z", error: "RPC timeout"};
  const html = L.render(L.evaluate(x, null, cfg, t + 3600));
  assert(html.includes("重算失败")); assert(html.includes("RPC timeout"));
  assert(html.includes("每天 12:00 和 18:00 UTC"));
  assert(html.includes("2026-10-04 12:16:23 EDT"));
});
console.log(`${count} cutoff integration tests passed`);
