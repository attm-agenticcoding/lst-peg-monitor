/* engine.js — 纯计算层：脱锚、共识、离群、退出曲线、状态判定
 * 无 DOM、无网络依赖，浏览器与 Node 共用，便于单测。 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PegEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* ---------- 基础统计 ---------- */
  function median(arr) {
    if (!arr || !arr.length) return null;
    const s = [...arr].sort((a, b) => a - b);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function mad(arr, med) {
    if (!arr || arr.length < 2) return null;
    const c = med == null ? median(arr) : med;
    return median(arr.map((v) => Math.abs(v - c)));
  }

  /* ---------- peg ---------- */
  /** 市价相对兑付锚的偏离，单位 bps。负数 = 折价。 */
  function pegBps(price, nav) {
    if (price == null || nav == null || !(nav > 0) || !isFinite(price)) return null;
    return (price / nav - 1) * 1e4;
  }

  /* ---------- 多源共识 ----------
   * sources: [{venue, kind, price, stale?, failed?}]
   * 规则：
   *   1. 逐源算 bps；
   *   2. 取全部可用源的中位数作初始锚；
   *   3. MAD 判离群：|x-med| > max(3*1.4826*MAD, absFloor) 剔除；
   *   4. 剔除后若剩余 < 2 个则回退到未剔除集合（宁可显示分歧，也不要假共识）；
   *   5. 共识 = 剩余集合的中位数；分歧 = 剩余集合的极差。
   */
  function aggregate(sources, nav, opts) {
    const o = Object.assign({ absFloor: 20, minKeep: 2 }, opts || {});
    const srcs = sources.map((s) => Object.assign({}, s, { bps: pegBps(s.price, nav) }));

    const usable = srcs.filter((s) => s.bps != null && !s.stale && !s.failed);
    const vals = usable.map((s) => s.bps);
    const med0 = median(vals);
    let m = null;

    if (med0 != null && usable.length >= 3) {
      m = mad(vals, med0);
      const cut = Math.max(3 * 1.4826 * m, o.absFloor);
      for (const s of usable) s.outlier = Math.abs(s.bps - med0) > cut;
    }

    const kept = usable.filter((s) => !s.outlier);
    const pool = kept.length >= o.minKeep ? kept : usable;
    if (pool !== kept) for (const s of usable) s.outlier = false;

    const bpsPool = pool.map((s) => s.bps);
    return {
      sources: srcs,
      consensus: median(bpsPool),
      medPrice: median(pool.map((s) => s.price)),
      confirms: pool.length,
      dispersion: pool.length >= 2 ? Math.max(...bpsPool) - Math.min(...bpsPool) : null,
      mad: m,
      outliers: usable.filter((s) => s.outlier).map((s) => s.venue),
    };
  }

  /* ---------- 退出曲线 ----------
   * series: [{label, color, pts:[{size, price}]}]
   * 返回每个量级上「最差的那个场所」的 bps，以及在 cleanBps 内还跑得掉的最大量级。
   * 注意：这是单池报价，不是跨场所聚合路由 —— 只反映该池自身深度。
   */
  function exitProfile(series, nav, sizes, opts) {
    const o = Object.assign({ cleanBps: 50 }, opts || {});
    const worstBySize = {};
    const bestBySize = {};
    for (const size of sizes) {
      let worst = null, best = null;
      for (const ser of series) {
        const p = (ser.pts || []).find((x) => x.size === size);
        const b = p ? pegBps(p.price, nav) : null;
        if (b == null) continue;
        if (worst == null || b < worst) worst = b;
        if (best == null || b > best) best = b;
      }
      worstBySize[size] = worst;
      bestBySize[size] = best;
    }
    // 在 cleanBps 内跑得掉的最大量级，用「最好的那个场所」判定（现实里会选最优场所）
    let cleanSize = null;
    for (const size of [...sizes].sort((a, b) => a - b)) {
      const b = bestBySize[size];
      if (b != null && b >= -o.cleanBps) cleanSize = size;
    }
    return { worstBySize, bestBySize, cleanSize, cleanBps: o.cleanBps };
  }

  /* ---------- 状态判定 ---------- */
  const LEVELS = { dead: 0, ok: 1, watch: 2, alert: 3, crisis: 4 };
  function classify(agg, exit, th) {
    const t = Object.assign(
      { ok: 25, watch: 75, alert: 200, disp: 40, thinSize: 10, thinBps: 100, minConfirms: 2 },
      th || {}
    );
    if (agg.consensus == null || agg.confirms < t.minConfirms)
      return { status: "dead", text: "源不足", why: "可用来源不足，无法交叉确认" };

    const a = Math.abs(agg.consensus);
    const thin = exit && exit.bestBySize && exit.bestBySize[t.thinSize] != null
      && exit.bestBySize[t.thinSize] < -t.thinBps;

    if (a > t.alert) return { status: "crisis", text: "危机", why: `共识脱锚 ${agg.consensus.toFixed(1)} bps，超过 ${t.alert} bps` };
    if (a > t.watch) return { status: "alert", text: "警戒", why: `共识脱锚 ${agg.consensus.toFixed(1)} bps` };
    if (thin) return { status: "alert", text: "警戒", why: `卖出 ${t.thinSize} 枚的执行价已差于 −${t.thinBps} bps，池子深度不足` };
    if (a > t.ok) return { status: "watch", text: "关注", why: `共识脱锚 ${agg.consensus.toFixed(1)} bps` };
    if (agg.dispersion != null && agg.dispersion > t.disp)
      return { status: "watch", text: "关注", why: `各源分歧 ${agg.dispersion.toFixed(1)} bps，偏大` };
    return { status: "ok", text: "正常", why: "各源一致且在阈值内" };
  }
  const worse = (a, b) => (LEVELS[b] > LEVELS[a] ? b : a);

  return { median, mad, pegBps, aggregate, exitProfile, classify, worse, LEVELS };
});
