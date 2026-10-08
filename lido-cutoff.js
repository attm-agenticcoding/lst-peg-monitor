/* Verified point-in-time mechanism scenarios; a daily collector may replace them. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./redemption.js"), require("./display-time.js"));
  else root.LidoCutoff = factory(root.Redemption, root.DisplayTime);
})(typeof self !== "undefined" ? self : this, function (R, T) {
  "use strict";
  const DAY = 86400, TIERS = [100, 200, 300, 500, 1000, 1500];
  const finite = (x) => typeof x === "number" && Number.isFinite(x);
  const ts = (x) => Date.parse(x) / 1000;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (x, digits = 4) => x == null || !Number.isFinite(Number(x)) ? "—" : Number(x).toLocaleString("en-US", { maximumFractionDigits: digits });
  const signed = (x) => x == null ? "—" : `${x >= 0 ? "+" : "−"}${n(Math.abs(x), 5)}`;
  function validate(s) {
    if (!s || s.schema !== 1 || s.live !== false || s.calibrated !== false || !finite(ts(s.asOf))
      || !Number.isSafeInteger(s.blockNumber) || !/^0x[0-9a-f]{64}$/.test(s.blockHash)
      || !Number.isInteger(s.pendingRequests) || !finite(Number(s.pendingSteth)) || !finite(Number(s.physicalCashEth))
      || !finite(s.maxEconomicAgeSeconds) || s.maxEconomicAgeSeconds <= 0 || s.maxEconomicAgeSeconds > 300
      || !Array.isArray(s.tiers) || s.tiers.length !== TIERS.length || !Array.isArray(s.dailyCutoffs) || s.dailyCutoffs.length < 1) return false;
    return s.tiers.every((r, i) => r.steth === TIERS[i] && Array.isArray(r.split)
      && r.split.every((x) => finite(x) && x > 0 && x <= 1000) && r.split.reduce((a, b) => a + b, 0) === r.steth
      && [r.main, r.stress].every((c) => c === null ? finite(ts(s.horizonEnd)) && ts(s.horizonEnd) > ts(s.asOf) : c && Number.isInteger(c.eligible_report_index) && c.eligible_report_index > 0
        && finite(ts(c.reference_time_utc)) && c.elapsed_to_reference_seconds > 0
        && ts(c.reference_time_utc) - ts(s.asOf) === c.elapsed_to_reference_seconds))
      && s.dailyCutoffs.every((r) => finite(ts(r.referenceTime)) && ts(r.referenceTime) > ts(s.asOf)
        && /^\d+\.\d{18}$/.test(r.mainSteth) && /^\d+\.\d{18}$/.test(r.stressSteth));
  }
  function evaluate(s, q, cfg, now = Date.now() / 1000, loadFailed = false) {
    if (!validate(s)) return { valid: false, actionable: false, reason: "情景文件不可用或格式无效" };
    const sourceAt = ts(s.asOf), age = now - sourceAt, o = R.options(cfg);
    const stale = !finite(now) || age < 0 || age > s.maxEconomicAgeSeconds;
    const scheduled = s.refresh && ["twice-daily", "daily", "hourly"].includes(s.refresh.mode);
    const twiceDaily = scheduled && s.refresh.mode === "twice-daily";
    const daily = scheduled && (twiceDaily || s.refresh.mode === "daily");
    const refreshFailed = scheduled && s.refresh.state === "error";
    // Daily operational freshness and five-minute quote compatibility are different.
    // Collection/publication still require a source younger than 90 minutes.
    // Neither a lease nor a failed attempt changes the underlying asOf.
    const sourceStale = !finite(now) || age < 0 || age > (scheduled ? (twiceDaily ? 48600 : daily ? 91800 : 5400) : s.maxEconomicAgeSeconds);
    const common = [];
    if (loadFailed) common.push("情景文件本轮读取失败");
    if (refreshFailed) common.push("最近重算失败，保留上次成功情景");
    if (stale) common.push("固定快照已过期，不能与当前报价合算");
    if (!q || q.refreshFailed) common.push("当前报价读取不可用");
    if (!q || !q.queue || q.queue.blockNumber !== s.blockNumber || q.queue.blockTimestamp !== sourceAt
      || q.queue.paused !== false || q.queue.bunker !== false) common.push("报价所用队列与情景快照不一致");
    if (!q || !R.fresh(q.gasAt, now, o.quoteMaxAgeSeconds) || !finite(q.gasGwei) || q.gasGwei <= 0) common.push("gas 价格缺失或过期");
    if (!q || !R.fresh(q.aprAt, now, o.aprMaxAgeSeconds) || !finite(q.apr) || q.apr < 0) common.push("质押 APR 缺失或过期");
    if (![o.extraHours, o.extraCostEth, o.slippageBps, o.haircutBps].every((v) => finite(v) && v >= 0)
      || o.slippageBps >= 10000 || o.haircutBps >= 10000 || !finite(o.gasMultiplier) || o.gasMultiplier < 1) common.push("成本或延迟参数无效");
    const rows = s.tiers.map((tier) => {
      const reasons = [...common];
      if (!tier.main || !tier.stress) reasons.push("模拟期内未得到两种场景的完整完成批次");
      // An ETH-input quote cannot be relabeled as the same nominal stETH tier.
      // Require its actual bought quantity to match; never scale another amount's price.
      let match = null;
      if (q) for (const input of o.sizes || []) {
        const best = R.selectQuote(q, input, cfg, now);
        if (best && Math.abs(best.got - tier.steth) <= 1e-9 && best.gasEth != null) {
          const meta = q.quoteMeta[best.via][input];
          if (Math.abs(meta.quotedAt - sourceAt) <= s.maxEconomicAgeSeconds) {
            const candidate = { ...best, input, quotedAt: meta.quotedAt };
            if (!match || candidate.input + candidate.gasEth < match.input + match.gasEth) match = candidate;
          }
        }
      }
      if (!match) reasons.push("缺少同一时点、实际买到该 stETH 数量的报价");
      const totalDays = { main: tier.main ? tier.main.elapsed_to_reference_seconds / DAY + o.extraHours / 24 : null,
        stress: tier.stress ? tier.stress.elapsed_to_reference_seconds / DAY + o.extraHours / 24 : null };
      const main = reasons.length ? null : R.economics(match.input, match.got, match.gasEth, o.extraCostEth, totalDays.main, q.apr / 100);
      const stress = reasons.length ? null : R.economics(match.input, match.got * (1 - o.slippageBps / 1e4) * (1 - o.haircutBps / 1e4),
        match.gasEth * o.gasMultiplier, o.extraCostEth, totalDays.stress, q.apr / 100);
      return { ...tier, mainEconomics: main, stressEconomics: stress, totalDays, quote: match, reasons, actionable: false };
    });
    return { valid: true, snapshot: s, rows, ageSeconds: Math.max(0, age), stale, sourceStale, scheduled, daily, twiceDaily, hourly: scheduled && !daily, refreshFailed, loadFailed, options: o, actionable: false };
  }
  function render(v) {
    if (!v.valid) return `<div class="cutoff-head"><h2>Lido 六档赎回批次情景</h2><span class="stale">资料不可用</span></div><p class="empty">${esc(v.reason)}；实时价格仍独立刷新</p>`;
    const s = v.snapshot, main = s.tiers[0].main, stress = s.tiers[0].stress;
    const date = (c) => c ? T.formatDate(c.reference_time_utc) : "模拟期内未覆盖";
    const batch = (c) => c ? `${date(c)} · 第 ${c.eligible_report_index} 批` : "模拟期内未覆盖";
    const rows = v.rows.map((r) => `<tr><td class="num">${n(r.steth, 0)}</td><td class="num">${r.split.map((a) => n(a, 0)).join(" + ")}</td><td>${batch(r.main)}</td><td>${batch(r.stress)}</td></tr>`).join("");
    const cutoffs = s.dailyCutoffs.map((r) => `<tr><td>${T.formatTimestamp(r.referenceTime)}</td><td class="r num">${n(r.mainSteth)}</td><td class="r num">${n(r.stressSteth)}</td></tr>`).join("");
    const cadence = v.twiceDaily ? "每日 00:00 / 12:00 UTC 重算" : v.daily ? "每日 00:00 UTC 重算" : "每小时重算";
    const status = v.scheduled ? (v.refreshFailed ? "重算失败 · 保留上次成功快照" : s.refresh.state === "pending" ? cadence + " · 等待首次重算" : v.sourceStale ? cadence + " · 已超过更新周期" : s.refresh.state === "running" ? "重算中 · 上次成功快照" : cadence + " · 非实时条件情景") : (v.stale ? "固定快照 · 已过期" : "固定快照 · 非实时 ETA");
    const error = v.refreshFailed ? `<p class="redemption-warning">最近尝试 ${s.refresh.attemptAt ? T.formatTimestamp(s.refresh.attemptAt) : "未知时间"}；${esc(s.refresh.error || "数据来源或完整性校验未通过")}。原快照时点和批次保持不变。</p>` : "";
    const lastSuccess = v.scheduled && s.refresh.lastSuccessAt ? `<p class="empty">最近成功重算 ${T.formatTimestamp(s.refresh.lastSuccessAt)}；快照来源时点见下方</p>` : "";
    const economics = v.rows.map((r) => `<tr><td class="num">${n(r.steth, 0)}</td>${[r.mainEconomics, r.stressEconomics].map((e, i) => `<td class="num">${signed(e && e.profit)}<span class="vmeta">简单 APR ${e ? n(e.apr * 100, 2) + "%" : "—"}<br>较同周期质押 ${signed(e && e.excessProfit)} ETH<br>资金周期 ${n(r.totalDays[i ? "stress" : "main"], 3)} 天</span></td>`).join("")}<td class="dim">${esc(r.reasons.join("；") || "条件情景，未经预测校准")}</td></tr>`).join("");
    return `<div class="cutoff-head"><h2>Lido 六档赎回批次情景</h2><span class="cutoff-status ${v.sourceStale || v.refreshFailed || v.loadFailed ? "stale" : ""}">${status}${v.loadFailed ? " · 文件读取失败" : ""}</span></div>${error}${lastSuccess}
      <p class="empty">快照来源时点 ${T.formatTimestamp(s.asOf)}；假设此时加入队尾；${n(s.pendingRequests, 0)} 笔 / ${n(s.pendingSteth)} stETH 待处理，同区块现有现金 ${n(s.physicalCashEth)} ETH${s.scenarioAvailableCashEth != null ? "，扣除储备及未入账余额后情景起始现金 " + n(s.scenarioAvailableCashEth) + " ETH" : ""}</p>
      <div class="cutoff-summary"><div><span>已知状态主情景 · 100 stETH</span><strong>${date(main)} <small>${main ? "第 " + main.eligible_report_index + " 个参考报告" : ""}</small></strong></div><div><span>每块预留 8 个部分提款槽位的压力情景 · 100 stETH</span><strong>${date(stress)} <small>${stress ? "第 " + stress.eligible_report_index + " 个参考报告" : ""}</small></strong></div></div>
      <p class="redemption-warning">各批次参考时间见下表；实际报告发布后才可能完成定案，再等待领取。不是到账承诺、概率区间或最坏上限；日期不会随网页时钟滑动。快照距今 ${n(v.ageSeconds / 3600, 1)} 小时。${v.scheduled ? (v.twiceDaily ? "每天 00:00 / 12:00 UTC 尝试重算；额外单次更新按请求执行。" : v.daily ? "每天 00:00 UTC 尝试重算；额外单次更新按请求执行。" : "每小时尝试重算。") + "重算覆盖队列、共识现金流和六档完整完成批次；来源延迟或失败保留原时点。日内仍是上述时点的历史情景，非实时 ETA。" : "尚未完成自动重算；当前仍为固定历史快照。"}${s.horizonEnd ? "模拟截止 " + T.formatTimestamp(s.horizonEnd) + "。" : ""}</p>
      <div class="tw"><table class="src cutoff-table"><thead><tr><th>假设申请 stETH</th><th>申请拆分</th><th>主情景参考批次</th><th>压力参考批次</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="empty">六档是同一队尾位置的独立假设，不依次叠加。1,500 stETH 至少分两笔，可在同一批完成；全部完成按最后一笔计。</p>
      <details id="cutoff-details" class="cutoff-details"><summary>逐日 funding cutoff、来源与假设</summary>
        <p class="empty">cutoff = 原队列之后、可全额覆盖的新增名义 stETH 上限，不是当天处理量；也须满足申请年龄条件</p>
        <div class="tw"><table class="src cutoff-table"><thead><tr><th>美东参考时间（EDT/EST）</th><th class="r">主情景 stETH</th><th class="r">压力情景 stETH</th></tr></thead><tbody>${cutoffs}</tbody></table></div>
        <ul class="cutoff-notes"><li>主情景延续现有 legacy 部分提款负载。压力情景将每块竞争性部分提款负载补足至 8 个槽位，不在已有槽位之上再加 8 个，也不代表最坏情况。</li><li>建模到账资金被各报告全部接纳；名义兑付无折损；无现金分流；所有 Accounting、份额、共识检查继续通过，正常日报告且不暂停。</li><li>不预测新增退出、未来存入/奖励、罚没、漏块、finality 变化或新提款负载。现有合并来源余额不直接当作提现现金。</li><li>FIFO、共识规则和历史参考样本均有回归测试。历史样本的独立回放属于机制一致性验证，不是本次未来结果验证；未完成样本外预测校准。</li></ul>
        <p class="empty">执行源 <a href="https://etherscan.io/block/${s.blockNumber}" target="_blank" rel="noopener">#${s.blockNumber}</a> · Beacon slot ${n(s.beaconSlot, 0)}；完整 SSZ root 与后继执行头 EIP-4788 锚一致。配置页面部分读数为 latest/unpinned。<a href="docs/lido-cutoff-scenarios.md">方法及复核依据</a> · <a href="data/lido-cutoff-snapshot.json">下载原始精度快照</a></p>
      </details>
      <details id="cutoff-economics" class="cutoff-details"><summary>六档情景净收益与同周期质押对照</summary>
        <p class="empty">只合算金额与时点一致的报价；固定快照超过 5 分钟、队列块不一致或报价/gas 缺失即留空。ETH 投入不能当作同数值 stETH。全资金周期从固定申请时点起算，另加报告发布/申请/领取延迟假设 ${n(v.options.extraHours, 2)} 小时（可展开“买入 stETH → Lido 赎回”修改参数），不随当前时钟缩短。</p>
        <div class="tw"><table class="src cutoff-economics"><thead><tr><th>stETH</th><th>主情景净利润 ETH</th><th>压力净利润 ETH</th><th>数据限制</th></tr></thead><tbody>${economics}</tbody></table></div>
        <p class="empty">含按实际数量预算的 swap、授权、申请、领取 gas 与其他成本；压力沿用页面的 gas 倍数、滑点、兑付折损。净回报分母为全部投入，质押对照使用同一资金周期。两种场景均不发出可执行交易提示。当前实时买入测算可展开“买入 stETH → Lido 赎回”查看，使用官方参考或手动等待，不冒充本模型已更新。</p>
      </details>`;
  }
  return { validate, evaluate, render };
});
