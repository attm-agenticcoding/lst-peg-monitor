/* Read-only stETH redemption scenarios. No wallet, trade, or notification actions. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Redemption = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const DAY = 86400;
  const finite = (n) => typeof n === "number" && Number.isFinite(n);
  const DEFAULTS = {
    swapGasUnits: { kyber: 350000, curve: 200000, curve_ng: 200000 },
    approvalGasUnits: 60000, requestGasUnits: 180000, claimGasUnits: 120000,
    gasMultiplier: 2, slippageBps: 1, haircutBps: 0, extraCostEth: 0,
    extraHours: 2, waitMultiplier: 2, manualWaitDays: null, manualConservativeDays: null,
    minProfitEth: 0.005, minNetBps: 1, premiumPctPoints: 1,
    quoteMaxAgeSeconds: 180, etaMaxAgeSeconds: 300, aprMaxAgeSeconds: 86400,
  };
  function options(cfg) {
    const c = cfg && cfg.stRedeem || {};
    return Object.assign({}, DEFAULTS, c, { swapGasUnits: Object.assign({}, DEFAULTS.swapGasUnits, c.swapGasUnits) });
  }
  // Decimal input only; avoid BigInt(float), exponent notation, or silent truncation.
  function toWei(n) {
    const s = String(n);
    if (!/^(?:0|[1-9]\d*)(?:\.\d{1,18})?$/.test(s)) throw new Error("请输入正数，最多 18 位小数");
    const [whole, frac = ""] = s.split(".");
    const w = BigInt(whole) * 10n ** 18n + BigInt(frac.padEnd(18, "0"));
    if (w <= 0n) throw new Error("数量必须大于 0");
    return w;
  }
  function fresh(at, now, limit) {
    return finite(at) && at <= now + 30 && now - at <= limit;
  }
  function gasUnits(q, via, x, got, o) {
    const m = q.quoteMeta && q.quoteMeta[via] && q.quoteMeta[via][x];
    const swap = m && finite(m.gasUnits) && m.gasUnits > 0 ? m.gasUnits : o.swapGasUnits[via];
    const requests = Math.ceil(got / 1000);
    return { swap, requests, total: swap + o.approvalGasUnits + requests * (o.requestGasUnits + o.claimGasUnits) };
  }
  function selectQuote(q, x, cfg, now = Math.floor(Date.now() / 1000)) {
    const o = options(cfg);
    const candidates = ["kyber", "curve", "curve_ng"].map((via) => {
      const got = q[via] && q[via][x];
      const meta = q.quoteMeta && q.quoteMeta[via] && q.quoteMeta[via][x];
      if (!finite(got) || got <= 0 || !meta || !fresh(meta.quotedAt, now, o.quoteMaxAgeSeconds)) return null;
      const gas = gasUnits(q, via, x, got, o);
      const gasEth = finite(q.gasGwei) && q.gasGwei > 0 ? q.gasGwei * 1e-9 * gas.total : null;
      return { via, got, gas, gasEth, score: got - (gasEth || 0) };
    }).filter(Boolean);
    return candidates.sort((a, b) => b.score - a.score)[0] || null;
  }
  // finalizationIn is milliseconds. nextCalculationAt is NOT a data timestamp.
  // The API constructs finalizationAt = Date.now() + finalizationIn; use their difference
  // only as response calculation time, never as proof its internal model inputs are fresh.
  function parseWait(d, amountSteth, fetchedAt) {
    const r = d && d.requestInfo;
    if (!d || d.status !== "calculated" || !r || !finite(r.finalizationIn) || r.finalizationIn <= 0
      || !["buffer", "bunker", "vaultsBalance", "rewardsOnly", "validatorBalances", "requestTimestampMargin", "exitValidators"].includes(r.type))
      return { status: d && d.status !== "calculated" ? d.status : "invalid", amountSteth, fetchedAt, reason: "官方参考估计尚未就绪" };
    const at = Date.parse(r.finalizationAt) / 1000;
    const calculatedAt = at - r.finalizationIn / 1000;
    if (!finite(at) || !finite(calculatedAt) || calculatedAt > fetchedAt + 30)
      return { status: "invalid", amountSteth, fetchedAt, reason: "官方参考估计时间字段无效" };
    return { status: "calculated", amountSteth, fetchedAt, calculatedAt, waitDays: r.finalizationIn / 1000 / DAY,
      type: r.type || "unknown", finalizationAt: r.finalizationAt, nextCalculationAt: d.nextCalculationAt || null };
  }
  function economics(input, received, gasEth, extra, days, stakingApr) {
    if (![input, received, gasEth, extra].every(finite) || input <= 0 || received <= 0 || gasEth < 0 || extra < 0) return null;
    const cost = input + gasEth + extra, profit = received - cost, netReturn = profit / cost;
    const years = finite(days) && days > 0 ? days / 365 : null;
    return { cost, received, gasEth, profit, netReturn, netBps: netReturn * 1e4,
      apr: years ? netReturn / years : null,
      stakingProfit: years && finite(stakingApr) ? cost * stakingApr * years : null,
      excessProfit: years && finite(stakingApr) ? profit - cost * stakingApr * years : null };
  }
  function estimate(q, cfg, now) {
    const o = options(cfg), sizes = o.sizes || [];
    if (!q || !sizes.length) return null;
    now = finite(now) ? now : Math.floor(Date.now() / 1000);
    const apr = finite(q.apr) && q.apr >= 0 && fresh(q.aprAt, now, o.aprMaxAgeSeconds) ? q.apr / 100 : null;
    const valid = [o.approvalGasUnits, o.requestGasUnits, o.claimGasUnits, o.extraHours, o.extraCostEth,
      o.minProfitEth, o.minNetBps, o.premiumPctPoints].every((v) => finite(v) && v >= 0)
      && [o.gasMultiplier, o.waitMultiplier].every((v) => finite(v) && v >= 1)
      && [o.slippageBps, o.haircutBps].every((v) => finite(v) && v >= 0 && v < 1e4)
      && Object.values(o.swapGasUnits).every((v) => finite(v) && v > 0)
      && [o.manualWaitDays, o.manualConservativeDays].every((v) => v == null || finite(v) && v > 0);
    const rows = sizes.map((x) => {
      const best = finite(x) && x > 0 ? selectQuote(q, x, cfg, now) : null;
      const empty = { eth: x, steth: null, back: null, diff: null, bps: null, apr: null, via: null,
        expected: null, conservative: null, waitDays: null, conservativeDays: null, signal: "unavailable", actionable: false, reasons: ["无新鲜有效买入报价"] };
      if (!best) return empty;
      const { via, got, gas } = best;
      const gasEth = fresh(q.gasAt, now, o.quoteMaxAgeSeconds) ? best.gasEth : null;
      const meta = q.quoteMeta && q.quoteMeta[via] && q.quoteMeta[via][x];
      const eta = q.waits && q.waits[x];
      const amountMatches = eta && finite(eta.amountSteth) && Math.abs(eta.amountSteth - got) <= Math.max(1e-9, got * 1e-10);
      const liveWait = amountMatches && eta.status === "calculated" && finite(eta.waitDays) && eta.waitDays > 0
        && fresh(eta.calculatedAt, now, o.etaMaxAgeSeconds) && fresh(eta.fetchedAt, now, o.etaMaxAgeSeconds);
      const manual = o.manualWaitDays != null;
      const waitDays = valid ? manual ? o.manualWaitDays : liveWait ? eta.waitDays : null : null;
      const totalDays = waitDays != null ? waitDays + o.extraHours / 24 : null;
      const conservativeDays = waitDays == null || !valid ? null
        : Math.max(waitDays, o.manualConservativeDays == null ? waitDays * o.waitMultiplier : o.manualConservativeDays) + o.extraHours / 24;
      const expected = valid ? economics(x, got, gasEth, o.extraCostEth, totalDays, apr) : null;
      const conservative = valid ? economics(x, got * (1 - o.slippageBps / 1e4) * (1 - o.haircutBps / 1e4),
        gasEth == null ? null : gasEth * o.gasMultiplier, o.extraCostEth, conservativeDays, apr) : null;
      const reasons = [];
      if (q.refreshFailed) reasons.push("本轮读取失败，旧快照仅供参考");
      if (!valid) reasons.push("情景参数无效");
      if (!meta || !fresh(meta.quotedAt, now, o.quoteMaxAgeSeconds)) reasons.push("买入报价缺少时间或已过期");
      if (!fresh(q.gasAt, now, o.quoteMaxAgeSeconds) || gasEth == null) reasons.push("gas 价格不可用或已过期");
      if (!fresh(q.aprAt, now, o.aprMaxAgeSeconds) || apr == null) reasons.push("质押 APR 不可用或已过期");
      if (manual) reasons.push("手动等待情景，未经校准");
      else if (!liveWait) reasons.push(eta && eta.reason || "对应数量的官方参考时间不可用或已过期");
      if (!q.queue || !fresh(q.queue.at, now, o.quoteMaxAgeSeconds)
        || !fresh(q.queue.blockTimestamp, now, o.quoteMaxAgeSeconds)) reasons.push("链上队列不可用或源区块过期");
      else {
        if (q.queue.paused !== false) reasons.push("提现暂停或状态未知");
        if (q.queue.bunker !== false) reasons.push("Bunker 模式或状态未知");
      }
      if (eta && eta.type === "bunker") reasons.push("官方时间使用 Bunker 情景");
      const passes = !!(conservative && conservative.apr != null && apr != null && conservative.profit > 0
        && conservative.profit >= o.minProfitEth && conservative.netBps >= o.minNetBps
        && conservative.apr >= apr + o.premiumPctPoints / 100);
      // Deliberately no actionable state: neither the official estimate nor a manual multiplier
      // is an independently calibrated bound. A quote is not a signed/guaranteed execution.
      const signal = reasons.length ? "unavailable" : passes ? "scenario_pass" : "below_threshold";
      reasons.push("独立等待模型尚未校准；报价和 gas 预算均非成交保证");
      return { eth: x, steth: got, back: got, diff: got - x, bps: (got / x - 1) * 1e4,
        via, gasEth, gasUnits: gas, quoteAt: meta && meta.quotedAt, eta: eta || null,
        waitDays, totalDays, conservativeDays, waitSource: manual ? "manual" : liveWait ? "official_unvalidated" : "unavailable",
        expected, conservative, apr: expected && expected.apr, scenarioPasses: passes, signal, actionable: false, reasons };
    });
    return { rows, apr, aprAt: q.aprAt || null, gasGwei: q.gasGwei || null, options: o, calibration: "pending", queue: q.queue || null };
  }
  return { DEFAULTS, options, toWei, fresh, parseWait, selectQuote, economics, estimate };
});
