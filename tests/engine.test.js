/* 引擎单测：node tests/engine.test.js */
const assert = require("assert");
const E = require("../engine.js");

let pass = 0;
const t = (name, fn) => { try { fn(); pass++; console.log("  ✓ " + name); }
  catch (e) { console.error("  ✗ " + name + "\n    " + e.message); process.exitCode = 1; } };
const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} !≈ ${b}`);

console.log("engine");

t("median 奇偶数都对", () => {
  close(E.median([3, 1, 2]), 2);
  close(E.median([4, 1, 2, 3]), 2.5);
  assert.strictEqual(E.median([]), null);
});

t("pegBps: 折价为负，锚上为 0", () => {
  close(E.pegBps(0.999, 1), -10);
  close(E.pegBps(1, 1), 0);
  close(E.pegBps(1.2438, 1.2441), -2.4113, 1e-3);
  assert.strictEqual(E.pegBps(1, 0), null);
  assert.strictEqual(E.pegBps(null, 1), null);
});

t("aggregate: 取中位数而不是均值", () => {
  const a = E.aggregate([
    { venue: "a", price: 0.9998 }, { venue: "b", price: 0.9999 },
    { venue: "c", price: 0.99985 }
  ], 1);
  close(a.consensus, -1.5, 1e-6);
  assert.strictEqual(a.confirms, 3);
});

t("aggregate: 明显离群的源被剔除且不影响共识", () => {
  const a = E.aggregate([
    { venue: "a", price: 0.9998 }, { venue: "b", price: 0.99981 },
    { venue: "c", price: 0.99982 }, { venue: "d", price: 0.99983 },
    { venue: "bad", price: 0.95 }
  ], 1);
  assert.deepStrictEqual(a.outliers, ["bad"]);
  assert.ok(Math.abs(a.consensus) < 3, "共识不该被拖走：" + a.consensus);
  assert.ok(a.dispersion < 3, "分歧应只算保留源");
});

t("aggregate: 剔完不足两个时回退，不造假共识", () => {
  const a = E.aggregate([
    { venue: "a", price: 1.0 }, { venue: "b", price: 0.90 }, { venue: "c", price: 1.10 }
  ], 1);
  assert.strictEqual(a.confirms, 3);
  assert.deepStrictEqual(a.outliers, []);
});

t("aggregate: stale / failed 源不参与", () => {
  const a = E.aggregate([
    { venue: "a", price: 0.999 }, { venue: "b", price: 0.9991 },
    { venue: "old", price: 0.5, stale: true }, { venue: "x", price: null, failed: true }
  ], 1);
  assert.strictEqual(a.confirms, 2);
  assert.ok(a.consensus < -9 && a.consensus > -11);
});

t("exitProfile: cleanSize 取最优场所还跑得掉的最大档", () => {
  const nav = 1;
  const series = [{ label: "p", pts: [
    { size: 1, price: 0.9998 }, { size: 10, price: 0.9995 },
    { size: 100, price: 0.99 }, { size: 1000, price: 0.5 }
  ] }];
  const x = E.exitProfile(series, nav, [1, 10, 100, 1000], { cleanBps: 50 });
  assert.strictEqual(x.cleanSize, 10);          // 100 枚是 −100bps，已超
  close(x.worstBySize[1], -2, 1e-6);
});

t("exitProfile: 两个场所时按更好的那个判定可退出规模", () => {
  const series = [
    { label: "thin", pts: [{ size: 1, price: 1 }, { size: 10, price: 0.9 }] },
    { label: "deep", pts: [{ size: 1, price: 1 }, { size: 10, price: 0.9999 }] }
  ];
  const x = E.exitProfile(series, 1, [1, 10], { cleanBps: 50 });
  assert.strictEqual(x.cleanSize, 10);
  close(x.worstBySize[10], -1000, 1e-6);
});

t("classify: 阈值边界", () => {
  const mk = (c, n = 4, d = 5) => ({ consensus: c, confirms: n, dispersion: d });
  const deep = { bestBySize: { 10: -5 } };
  assert.strictEqual(E.classify(mk(-10), deep).status, "ok");
  assert.strictEqual(E.classify(mk(-40), deep).status, "watch");
  assert.strictEqual(E.classify(mk(-120), deep).status, "alert");
  assert.strictEqual(E.classify(mk(-350), deep).status, "crisis");
  assert.strictEqual(E.classify(mk(-5, 1), deep).status, "dead");
});

t("classify: 分歧过大也升级为关注", () => {
  const r = E.classify({ consensus: -3, confirms: 5, dispersion: 90 }, { bestBySize: { 10: -2 } });
  assert.strictEqual(r.status, "watch");
});

t("classify: 池子太薄时即使中间价好看也报警戒", () => {
  const r = E.classify({ consensus: -4, confirms: 5, dispersion: 4 }, { bestBySize: { 10: -600 } });
  assert.strictEqual(r.status, "alert");
});

t("worse 取更严重的一档", () => {
  assert.strictEqual(E.worse("ok", "alert"), "alert");
  assert.strictEqual(E.worse("crisis", "watch"), "crisis");
});

t("aggregate: 聚合器默认不进共识（kind 过滤）", () => {
  const srcs = [
    { venue: "curve", kind: "onchain", price: 0.99980 },
    { venue: "cl",    kind: "oracle",  price: 0.99981 },
    { venue: "okx",   kind: "cex",     price: 0.99982 },
    { venue: "llama", kind: "agg",     price: 0.99857 },   // −14.3 bps
    { venue: "gecko", kind: "agg",     price: 0.99944 },   // −5.6 bps
  ];
  // 旧行为：绝对地板 20 bps 让 −14.3 的聚合器留在共识里，把分歧顶到 12.5
  const before = E.aggregate(srcs, 1);
  assert.strictEqual(before.confirms, 5);
  assert.ok(before.dispersion > 12, "旧逻辑分歧被聚合器顶高：" + before.dispersion);
  // 新行为：聚合器不进共识，分歧回到真实场所价差
  const after = E.aggregate(srcs, 1, { consensusKinds: ["onchain", "oracle", "cex"] });
  assert.strictEqual(after.confirms, 3);
  assert.deepStrictEqual([...after.excluded].sort(), ["gecko", "llama"]);
  assert.ok(after.dispersion < 1, "剔出聚合器后分歧应回到场所价差：" + after.dispersion);
  assert.strictEqual(after.sources.length, 5, "被剔的源仍要返回，看板照常显示");
});

t("aggregate: 够格的源不足两个时退回全集，不造假共识", () => {
  const a = E.aggregate([
    { venue: "uni",   kind: "onchain", price: 0.9998 },
    { venue: "llama", kind: "agg",     price: 0.9997 },
    { venue: "gecko", kind: "agg",     price: 0.9999 },
  ], 1, { consensusKinds: ["onchain"] });
  assert.strictEqual(a.kindFallback, true);
  assert.strictEqual(a.confirms, 3);
});

t("aggregate: derived 源不计入 independentConfirms", () => {
  const a = E.aggregate([
    { venue: "uni",            kind: "onchain", price: 1.2439 },
    { venue: "curve 换算",     kind: "onchain", price: 1.24392, derived: true },
    { venue: "chainlink 换算", kind: "oracle",  price: 1.24393, derived: true },
    { venue: "okx 换算",       kind: "cex",     price: 1.24391, derived: true },
  ], 1.2441, { consensusKinds: ["onchain", "oracle", "cex"] });
  assert.strictEqual(a.confirms, 4);
  assert.strictEqual(a.independentConfirms, 1, "wstETH 真正独立的市价源只有 Uni v3 一个");
});

t("换算源对本代币零信息量：nav 在 pegBps 里约掉", () => {
  const nav = 1.2441763842073688;
  for (const p of [0.9998, 1.0, 0.97, 1.02]) close(E.pegBps(p * nav, nav), E.pegBps(p, 1), 1e-9);
});

t("stETH 与 wstETH 可原子互换 ⇒ 同一组源下共识必须相等", () => {
  const nav = 1.2441763842073688;
  const px = { curve: 0.99977, cl: 1.00006, okx: 0.99985 };
  const kind = { curve: "onchain", cl: "oracle", okx: "cex" };
  const mk = (scale, derived) => Object.keys(px).map((k) =>
    ({ venue: k, kind: kind[k], price: px[k] * scale, derived }));
  const K = { consensusKinds: ["onchain", "oracle", "cex"] };
  const st = E.aggregate(mk(1, false), 1, K);
  const ws = E.aggregate(mk(nav, true), nav, K);
  close(st.consensus, ws.consensus, 1e-9);
  assert.strictEqual(ws.independentConfirms, 0, "全是换算源时独立确认数应为 0");
});

console.log(`\n${pass} 项通过${process.exitCode ? "，有失败" : ""}`);
