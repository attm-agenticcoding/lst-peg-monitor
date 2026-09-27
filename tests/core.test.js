/* node tests/core.test.js — core.js 的纯计算部分 */
"use strict";
const assert = require("assert");
const C = require("../core.js");
const CFG = require("../config.json");
const TH = CFG.thresholds, S = CFG.sizes;
let n = 0;
const t = (name, fn) => { fn(); n++; console.log("ok -", name); };

// 由 bps 反推每枚价格，方便写用例
const pxFrom = (nav, table) => Object.fromEntries(Object.entries(table).map(([id, arr]) =>
  [id, Object.fromEntries(S.map((s, i) => [s, arr[i] == null ? null : nav * (1 + arr[i] / 1e4)]))]));
const st = (table) => C.analyse(pxFrom(1, table), 1, C.VENUES.stETH, TH, S);

t("sellBook 逐档吃单、深度不够返回 null", () => {
  const bids = [["1.0", "2"], ["0.99", "3"]];
  assert.strictEqual(C.sellBook(bids, 1), 1.0);
  assert.ok(Math.abs(C.sellBook(bids, 4) - (2 * 1 + 2 * 0.99) / 4) < 1e-12);
  assert.strictEqual(C.sellBook(bids, 6), null);
});

t("常态：主报价 = 大额最优的场所，状态正常", () => {
  const a = st({ kyber: [-0.96, -0.96, -0.96, -1.31], curve: [-2.03, -2.04, -2.09, -2.62],
    curve_ng: [-1.65, -1.66, -1.79, -3.2], uni_wsteth: [-2.02, -2.03, -2.13, -3.2], okx: [-2.26, -2.93, -3.35, null] });
  assert.strictEqual(a.clean, 1000);
  assert.strictEqual(a.primary, "kyber");
  assert.strictEqual(a.peg, -0.96);
  assert.strictEqual(a.status, "ok");
  assert.strictEqual(a.exit[1000], -1.31);
});

t("大额场所换人：1000 枚时 Curve 更好就用 Curve 卖 1 枚的价", () => {
  const a = st({ kyber: [-1, -1, -1, -60], curve: [-2, -2, -2, -3], okx: [-2, -2, null, null] });
  assert.strictEqual(a.primary, "curve");
  assert.strictEqual(a.peg, -2);
});

t("单个场所越线不升级，两个同时越线才升级", () => {
  const one = st({ kyber: [-1, -1, -1, -1], curve: [-300, -300, -300, -300], okx: [-2, -2, -2, null] });
  assert.strictEqual(one.status, "ok");
  assert.ok(one.why.includes("没有第二个场所确认"));
  const two = st({ kyber: [-30, -30, -31, -33], curve: [-40, -40, -41, -45], okx: [-2, -2, -2, null] });
  assert.strictEqual(two.status, "watch");
  const bad = st({ kyber: [-230, -230, -240, -260], curve: [-250, -250, -260, -300], uni_wsteth: [-90, -90, -95, -120] });
  assert.strictEqual(bad.status, "crisis");
});

t("报错成高价的离群源不能当主报价，也不会触发状态", () => {
  const a = st({ kyber: [500, 500, 500, 500], curve: [-2, -2, -2, -3], curve_ng: [-2, -2, -2, -4], okx: [-2, -2, -2, null] });
  assert.notStrictEqual(a.primary, "kyber");
  assert.strictEqual(a.status, "ok");
});

t("深度塌了：最优场所卖 10 枚差于 −100 bps → 警戒", () => {
  const a = st({ kyber: [-10, -150, -400, -900], curve: [-12, -160, -500, -1200] });
  assert.strictEqual(a.status, "alert");
  assert.strictEqual(a.clean, 1);
});

t("可用场所不足 2 个 → 源不足", () => {
  const a = st({ kyber: [-1, -1, -1, -1] });
  assert.strictEqual(a.status, "dead");
  assert.strictEqual(a.primary, "kyber");
});

t("cbETH 按 exchangeRate 算：Coinbase 更深时当主报价", () => {
  const nav = 1.14;
  const a = C.analyse(pxFrom(nav, { coinbase: [-2.3, -6.1, -13.2, null], kyber: [-12.7, -13.3, -37.6, -8391],
    uni_cbeth: [-12.7, -17, -467, -9027] }), nav, C.VENUES.cbETH, TH, S);
  assert.strictEqual(a.clean, 100);
  assert.strictEqual(a.primary, "coinbase");
  assert.ok(Math.abs(a.peg + 2.3) < 0.02);
  assert.strictEqual(a.status, "ok");
});

t("常态基线按小时分桶，稀疏化不影响权重", () => {
  const now = 1_800_000_000, recs = [];
  for (let h = 0; h < 48; h++) {
    const k = h < 24 ? 6 : 1; // 近 24 小时每小时 6 条（值 −1），更早每小时 1 条（值 −3）
    for (let i = 0; i < k; i++) recs.push({ ts: now - (47 - h) * 3600 + i * 60, stETH: { peg: h < 24 ? -3 : -1 } });
  }
  const b = C.baseline(recs, "stETH", now, { days: 30, minHours: 12 });
  assert.strictEqual(b.hours, 48);
  assert.ok(b.ready);
  assert.strictEqual(b.median, -2);
  assert.strictEqual(b.mean, -2);
  assert.strictEqual(C.baseline(recs.slice(-5), "stETH", now, { minHours: 12 }).ready, false);
});

t("历史稀疏化：近 3 天全留，更早每小时一条，超期丢弃", () => {
  const now = 1_800_000_000, recs = [];
  for (let m = 0; m <= 70 * 24 * 6; m++) recs.push({ ts: now - m * 600 });
  recs.reverse();
  const out = C.thin(recs, now, { fullDays: 3, keepDays: 60 });
  const full = out.filter((r) => r.ts >= now - 3 * 86400).length;
  assert.strictEqual(full, 3 * 144 + 1);
  assert.ok(out[0].ts >= now - 60 * 86400);
  assert.ok(Math.abs(out.length - (full + 57 * 24)) <= 1);
});

t("toRecord 字段齐全", () => {
  const snap = { ts: 1, nav: { stETH: 1, wstETH: 1.24, cbETH: 1.14 }, px: { stETH: pxFrom(1, { kyber: [-1, -1, -1, -1], curve: [-2, -2, -2, -2] }), cbETH: {} }, err: ["okx: 超时"] };
  const r = C.toRecord(snap, C.evaluate(snap, CFG));
  assert.deepStrictEqual(Object.keys(r.stETH).sort(), ["clean", "peg", "st", "v", "via", "x"]);
  assert.strictEqual(r.stETH.via, "kyber");
  assert.strictEqual(r.cbETH.st, "dead");
  assert.deepStrictEqual(r.err, ["okx: 超时"]);
});

console.log(`\n${n} passed`);
