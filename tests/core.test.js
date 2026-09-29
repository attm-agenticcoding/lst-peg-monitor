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

t("主报价越线、且有别的场所确认才升级", () => {
  const thin = st({ kyber: [-1, -1, -1, -1], curve: [-300, -300, -300, -300], okx: [-2, -2, -2, null] });
  assert.strictEqual(thin.status, "ok");
  assert.ok(thin.why.includes("不是主要流动性"));
  const lone = st({ kyber: [-30, -30, -30, -30], curve: [-2, -2, -2, -40], okx: [-2, -2, -2, null] });
  assert.strictEqual(lone.primary, "kyber");
  assert.strictEqual(lone.status, "ok");
  assert.ok(lone.why.includes("没有别的场所确认"));
  const two = st({ kyber: [-30, -30, -31, -33], curve: [-40, -40, -41, -45], okx: [-2, -2, -2, null] });
  assert.strictEqual(two.status, "watch");
  const mixed = st({ kyber: [-80, -80, -80, -82], curve: [-30, -30, -31, -90], okx: [-2, -2, null, null] });
  assert.strictEqual(mixed.status, "watch"); // 主报价到警戒，但确认的场所只到关注 → 取较轻的一档
  // 压力下深池跌到 −80，薄盘口的小单还挂在 −2：主报价不能被薄盘口顶替
  const stress = st({ kyber: [-80, -80, -81, -85], curve: [-82, -82, -83, -90], okx: [-2, -2, null, null] });
  assert.strictEqual(stress.primary, "kyber");
  assert.strictEqual(stress.status, "alert");
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

t("cbETH 按 exchangeRate 算：Base 最深时用 Base；主网薄池漂走不误报", () => {
  const nav = 1.14;
  const cb = (t) => C.analyse(pxFrom(nav, t), nav, C.VENUES.cbETH, TH, S);
  const live = { kyber_base: [-2.36, -2.43, -2.63, -5.81], aero_base: [-2.76, -2.78, -2.88, -3057], coinbase: [-2.3, -6.1, -13.2, null],
    kyber: [-12.7, -13.3, -37.6, -8391], uni_cbeth: [-12.7, -17, -467, -9027] };
  const a = cb(live);
  assert.strictEqual(a.clean, 1000);
  assert.strictEqual(a.primary, "kyber_base");
  assert.ok(Math.abs(a.peg + 2.36) < 0.02);
  assert.strictEqual(a.status, "ok");
  const drift = cb(Object.assign({}, live, { kyber: [-40, -41, -60, -8391], uni_cbeth: [-40, -45, -470, -9027] }));
  assert.strictEqual(drift.status, "ok");
  const noBase = cb({ coinbase: live.coinbase, kyber: live.kyber, uni_cbeth: live.uni_cbeth }); // Base 取不到时退回 Coinbase
  assert.strictEqual(noBase.primary, "coinbase");
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

t("历史分层稀疏化：1 天内全留、1–3 天 10 分钟一条、更早每小时一条、超期丢弃", () => {
  const now = 1_800_000_000, recs = [];
  for (let m = 70 * 24 * 30; m >= 0; m--) recs.push({ ts: now - m * 120 }); // 2 分钟一条，70 天
  const out = C.thin(recs, now, CFG.history.tiers);
  const age = (r) => now - r.ts;
  assert.strictEqual(out.filter((r) => age(r) <= 86400).length, 721);
  const d3 = out.filter((r) => age(r) > 86400 && age(r) <= 3 * 86400).length;
  const old = out.filter((r) => age(r) > 3 * 86400).length;
  assert.ok(Math.abs(d3 - 288) <= 1, d3);
  assert.ok(Math.abs(old - 57 * 24) <= 1, old);
  assert.ok(out.every((r) => age(r) <= 60 * 86400));
});

const G = CFG.guard;
const hist = (n, step, now, fn) => { const out = []; for (let i = n; i >= 1; i--) out.push(fn(now - i * step, i)); return out; };

t("预警线：平静期由 3 bps 下限决定；最近 1 小时不进基线；数据不够不给线", () => {
  const now = 1_800_000_000;
  const s = hist(720, 120, now, (ts, i) => [ts, i % 3 ? -1 : -0.9]);
  const L = C.guardLevel(s, now, G);
  assert.strictEqual(L.win, "24h");
  assert.strictEqual(L.level, -4); // 中位 −1，MAD = 0 → 下限 3 bps
  const crash = s.map(([ts, v]) => [ts, now - ts < 3600 ? -50 : v]);
  assert.strictEqual(C.guardLevel(crash, now, G).level, -4);
  assert.strictEqual(C.guardLevel(s.slice(-90), now, G), null); // 只有 3 小时
});

t("偶发的无害尖刺不会放宽预警线（中位数/MAD，不用均值/σ）", () => {
  const now = 1_800_000_000;
  const L = C.guardLevel(hist(720, 120, now, (ts, i) => [ts, i % 50 ? -2.4 : 4.2]), now, G);
  assert.strictEqual(L.level, -5.4);
});

t("偏离预警：同一次采样里至少两个场所跌破各自的线才触发；大额那一路单独判断", () => {
  const now = 1_800_000_000;
  const recs = hist(720, 120, now, (ts) => ({ ts, stETH: { v: { kyber: -1, curve: -2, uni_wsteth: -2 },
    vb: { kyber: -1.5, curve: -2.6, uni_wsteth: -3 } }, cbETH: {} }));
  const cur = (v, vb) => ({ ts: now, stETH: { via: "kyber", v, vb }, cbETH: {} });
  const calm = { kyber: -1.5, curve: -2.6, uni_wsteth: -3 };
  let g = C.guard(recs, cur({ kyber: -7, curve: -2, uni_wsteth: -2 }, calm), CFG);
  assert.strictEqual(g.stETH.peg.hit, false);
  assert.deepStrictEqual(g.stETH.peg.hits, ["kyber"]);
  g = C.guard(recs, cur({ kyber: -4.5, curve: -5.5, uni_wsteth: -2 }, calm), CFG);
  assert.strictEqual(g.stETH.peg.hit, true);
  assert.strictEqual(g.stETH.peg.rep.id, "kyber");
  assert.strictEqual(g.stETH.big.hit, false);
  g = C.guard(recs, cur({ kyber: -1, curve: -2, uni_wsteth: -2 }, { kyber: -8, curve: -9, uni_wsteth: -3 }), CFG);
  assert.strictEqual(g.stETH.peg.hit, false);
  assert.strictEqual(g.stETH.big.hit, true); // 卖 1 枚还没动，大额退出成本先恶化
});

t("Base 买入 → Coinbase 赎回：取买到更多 cbETH 的路由，按 Coinbase 兑换率算回 ETH", () => {
  const cfg = Object.assign({}, CFG, { roundTrip: { sizes: [25, 50] } });
  const q = { kyber: { 25: 21.91967, 50: 43.8383 }, aero: { 25: 21.72, 50: 23.08 }, rate: 1.1404925, waitDays: 10.61, apy: 0.0235 };
  const r = C.roundTrip(q, 1.14, cfg);
  assert.strictEqual(r.src, "coinbase");
  assert.deepStrictEqual(r.rows.map((x) => x.via), ["kyber_base", "kyber_base"]);
  assert.ok(Math.abs(r.rows[0].back - 21.91967 * 1.1404925) < 1e-9);
  assert.ok(Math.abs(r.rows[0].bps - ((21.91967 * 1.1404925 - 25) / 25) * 1e4) < 0.01);
  assert.ok(Math.abs(r.rows[0].apr - (r.rows[0].diff / 25) * 365 / 10.61) < 1e-6);
  const fb = C.roundTrip({ aero: { 25: 21.72 } }, 1.14, Object.assign({}, CFG, { roundTrip: { sizes: [25] } }));
  assert.strictEqual(fb.src, "onchain"); // 取不到 Coinbase 兑换率时退回链上 exchangeRate
  assert.strictEqual(fb.rows[0].via, "aero_base");
  assert.strictEqual(fb.rows[0].apr, null);
});

t("toRecord 字段齐全", () => {
  const snap = { ts: 1, nav: { stETH: 1, wstETH: 1.24, cbETH: 1.14 }, px: { stETH: pxFrom(1, { kyber: [-1, -1, -1, -1], curve: [-2, -2, -2, -2] }), cbETH: {} }, err: ["okx: 超时"] };
  const r = C.toRecord(snap, C.evaluate(snap, CFG), CFG);
  assert.deepStrictEqual(Object.keys(r.stETH).sort(), ["clean", "peg", "st", "v", "vb", "via", "x"]);
  assert.strictEqual(r.stETH.vb.kyber, -1);
  assert.strictEqual(r.stETH.via, "kyber");
  assert.strictEqual(r.cbETH.st, "dead");
  assert.deepStrictEqual(r.err, ["okx: 超时"]);
});

console.log(`\n${n} passed`);
