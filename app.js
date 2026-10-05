/* app.js — 页面层。每 refreshSeconds 秒用 core.js 直读一次（和后台快照同一套算法），
 * 另外每 5 分钟读一次 data/history.json：算常态、画 7 天走势、看后台快照是否新鲜。 */
(() => {
  "use strict";
  const C = window.PegCore, Cutoff = window.LidoCutoff;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = C.fmt;
  const ago = (s) => (!isFinite(s) ? "—" : s < 90 ? `${Math.max(0, Math.round(s))} 秒前`
    : s < 5400 ? `${Math.round(s / 60)} 分钟前` : `${(s / 3600).toFixed(1)} 小时前`);
  const hm = (ts) => new Date(ts * 1000).toLocaleString("zh-CN",
    { timeZone: "America/New_York", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) + " ET";
  const span = (h) => (h < 48 ? `${h} 小时` : `${Math.round(h / 24)} 天`);
  const KIND = { agg: "聚合", dex: "链上", cex: "交易所" };
  let CFG = null, hist = null, histAt = 0, busy = false, timer = null, stOverrides = {}, customAmount = null, lastSnap = null, lastCfg = null, refreshFailed = false;
  let cutoffSnapshot = null, cutoffLoadFailed = false, cutoffLoading = false;
  const currentCfg = () => Object.assign({}, CFG, { stRedeem: Object.assign({}, CFG.stRedeem, stOverrides, customAmount == null ? {} : { sizes: [customAmount] }) });

  function redrawCutoff() {
    const panel = $("lido-cutoff");
    if (!panel || !Cutoff || !CFG) return;
    const opened = ["cutoff-details", "cutoff-economics"].filter((id) => $(id) && $(id).open);
    panel.innerHTML = Cutoff.render(Cutoff.evaluate(cutoffSnapshot,
      lastSnap ? Object.assign({}, lastSnap.st, { refreshFailed }) : null,
      lastCfg || currentCfg(), Date.now() / 1000, cutoffLoadFailed));
    for (const id of opened) if ($(id)) $(id).open = true;
  }
  async function loadCutoff() {
    if (cutoffLoading) return;
    cutoffLoading = true;
    const controller = new AbortController();
    let timeout;
    try {
      const next = await Promise.race([
        (async () => {
          const r = await fetch("data/lido-cutoff-snapshot.json", { cache: "no-store", signal: controller.signal });
          if (!r.ok) throw new Error("snapshot unavailable");
          return r.json();
        })(),
        new Promise((_, reject) => { timeout = setTimeout(() => { controller.abort(); reject(new Error("snapshot timeout")); }, 8000); }),
      ]);
      if (!Cutoff || !Cutoff.validate(next)) throw new Error("invalid snapshot");
      cutoffSnapshot = next; cutoffLoadFailed = false;
    } catch { cutoffLoadFailed = true; }
    finally { clearTimeout(timeout); cutoffLoading = false; }
    redrawCutoff();
  }

  async function loadHist() {
    if (hist && Date.now() - histAt < 5 * 60e3) return;
    try {
      const r = await fetch(`data/history.json?t=${Math.floor(Date.now() / 60e3)}`, { cache: "no-store" });
      if (r.ok) {
        const h = await r.json();
        hist = h && h.schema === 2 ? h : null; // 旧版文件在第一条新快照写入前还在，忽略
        histAt = Date.now();
      }
    } catch { /* 历史取不到不影响实时数 */ }
  }

  /* ---------- 卡片 ---------- */
  function card(sym, a, snap) {
    const S = CFG.sizes, maxS = S[S.length - 1];
    const b = hist ? C.baseline(hist.records, sym, snap.ts, CFG.baseline) : { ready: false, hours: 0 };
    // 撤离预警线来自后台最近一次采样（history.json 顶层 guard），每 10 分钟同步一次
    const gd = hist && hist.guard ? hist.guard[sym] : null, gp = gd && gd.peg && gd.peg.rep, gb = gd && gd.big && gd.big.rep;
    const on = gd && ((gd.peg && gd.peg.on) || (gd.big && gd.big.on));
    const clean = a.clean == null ? `< ${S[0]} 枚` : `${a.clean === maxS ? "≥ " : ""}${a.clean.toLocaleString()} 枚`;
    const nav = sym === "stETH"
      ? ["1.0000", `Lido 1:1 · wstETH = ${snap.nav.wstETH ? snap.nav.wstETH.toFixed(4) : "—"} stETH`]
      : [snap.nav.cbETH ? snap.nav.cbETH.toFixed(6) : "—", "exchangeRate() · 每天 16:00 UTC 更新"];
    const kpi = (dt, dd, sm) => `<div class="kpi"><dt>${dt}</dt><dd class="num">${dd}<span class="sm">${sm}</span></dd></div>`;

    const rows = a.venues.map((v) => {
      const dead = v.peg == null;
      const cells = S.map((s) => (v.bps[s] == null ? `<td class="r num dim" title="${dead ? "取不到" : "深度不够或无报价"}">—</td>`
        : `<td class="r num">${fmt(v.bps[s], 2)}</td>`)).join("");
      return `<tr class="${v.id === a.primary ? "prim" : ""}"><td><span class="venue">${esc(v.label)}</span>
        <span class="tag ${v.kind}">${KIND[v.kind]}</span>${v.id === a.primary ? ' <span class="tag main">主报价</span>' : ""}
        ${dead ? ' <span class="flag-fail">不可用</span>' : ""}<span class="vmeta">${esc(v.note || "")}</span></td>${cells}</tr>`;
    }).join("");
    const best = S.map((s) => `<td class="r num">${fmt(a.exit[s], 2)}</td>`).join("");

    return `<section class="card" id="card-${sym}">
      <div class="card-head"><span class="tick">${sym}</span>
        <span class="anchor-kind">${sym === "stETH" ? "含 wstETH · 锚 1:1" : "锚 exchangeRate"}</span>
        <span class="pill p-${a.status}">${C.TEXT[a.status]}</span></div>
      <div class="headline"><span class="big num v-${a.status}">${fmt(a.peg)}</span><span class="big-unit">bps</span>
        <div class="hnote">主报价 · ${esc(a.primaryLabel || "—")} 卖 1 枚<br><span class="why">${esc(a.why)}</span>${on ? '<br><b class="stale">偏离预警中</b>' : ""}</div></div>
      <dl class="kpis">
        ${kpi("常态", b.ready ? `${fmt(b.median)} bps` : "积累中",
          b.ready ? `近 ${span(b.hours)}中位 · 均值 ${fmt(b.mean)} · 常见 ${fmt(b.p10)}~${fmt(b.p90)}` : `已有 ${b.hours} 小时，满 ${CFG.baseline.minHours} 小时出数`)}
        ${kpi("撤离预警线", gp ? `${fmt(gp.level)} bps` : "积累中",
          gp ? `卖 1 枚跌破即报（${gp.win} 中位 ${fmt(gp.center)} − ${Math.max(3 * gp.scale, CFG.guard.floorBps).toFixed(1)}）`
            + (gb ? `；卖 ${gd.big.size} 枚 ${fmt(gb.x)} / 线 ${fmt(gb.level)}` : `；卖 ${CFG.guard.bigSize[sym]} 枚的线积累中`)
            : `需要 ${CFG.guard.windows[0].minHours} 小时历史`)}
        ${kpi(`${CFG.thresholds.cleanExitBps} bps 内可卖`, clean, a.clean == null ? "1 枚都已超过" : `最优场所 ${fmt(a.exit[a.clean], 2)} bps`)}
        ${kpi("兑付锚", nav[0], nav[1])}
      </dl>
      <div class="sect"><h3>走势 <span class="cnt">主报价 · 近 7 天 · 虚线 = 常态，灰带 = 常见区间 p10~p90</span></h3>
        <div class="spark" id="spark-${sym}"></div></div>
      <div class="sect"><h3>各场所卖出执行价 <span class="cnt">相对兑付锚，bps；主报价 = 大额上最好的那家</span></h3>
        <div class="tw"><table class="src"><thead><tr><th>场所</th>${S.map((s, i) => `<th class="r">${i ? "" : "卖 "}${s.toLocaleString()}${i ? "" : " 枚"}</th>`).join("")}</tr></thead>
        <tbody>${rows}<tr class="best"><td>最优（逐档取最好）</td>${best}</tr></tbody></table></div></div>
    </section>`;
  }

  /* ---------- Base 买入 → Coinbase 赎回 ---------- */
  const RT_VIA = { kyber_base: "KyberSwap · Base", aero_base: "Aerodrome · Base" };
  function rtSection(rt) {
    if (!rt) return "";
    const n = (x, d) => (x == null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
    const days = rt.waitDays != null ? rt.waitDays.toFixed(1) : "—";
    const rows = rt.rows.map((r) => `<tr><td class="num">${r.eth}</td><td class="r num">${n(r.cbeth, 4)}</td>
      <td class="r num">${n(r.back, 4)}</td>
      <td class="r num">${r.diff == null ? "—" : `${r.diff >= 0 ? "+" : "−"}${n(Math.abs(r.diff), 4)}`}<span class="vmeta">${fmt(r.bps, 1)} bps</span></td>
      <td class="r num">${r.apr == null ? "—" : `${r.apr >= 0 ? "" : "−"}${Math.abs(r.apr * 100).toFixed(2)}%`}</td>
      <td class="dim" style="padding-left:16px">${RT_VIA[r.via] || "—"}</td></tr>`).join("");
    return `<div class="sect"><h3>Base 买入 → Coinbase 赎回 <span class="cnt">兑换率 ${rt.rate.toFixed(6)}${rt.src === "coinbase" ? "（Coinbase 公布）" : "（取不到 Coinbase 的，用链上 exchangeRate）"} · 赎回排队约 ${days} 天</span></h3>
      <div class="tw"><table class="src"><thead><tr><th>投入 ETH</th><th class="r">买到 cbETH</th><th class="r">赎回得 ETH</th>
        <th class="r">差额 ETH</th><th class="r">折合年化</th><th style="padding-left:16px">买入路由</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="empty" style="margin-top:8px">unwrap 不收费。赎回得到的是 Coinbase 上的质押 ETH，要排以太坊退出队列才变成可用 ETH（Coinbase 当前估计 ${days} 天），这段时间钱是锁住的；
      折合年化 = 差额 ÷ 排队天数 × 365，可以和质押年化（现在 ${rt.apy != null ? (rt.apy * 100).toFixed(2) + "%" : "—"}）对比。
      cbETH 可以直接走 Base 网络充值到 Coinbase；Base 上的 gas 不到 1 美分，没有计入。</p></div>${wsSection(rt, n)}`;
  }

  /* ---------- 主网买 stETH → Lido 提现赎回 ---------- */
  const ST_VIA = { kyber: "KyberSwap 聚合", curve: "Curve stETH/ETH", curve_ng: "Curve stETH-ng" };
  // Display-only gross comparison: quote output already includes pool fees/price impact.
  function stGrossComparison(r, stakingApr) {
    const valid = Number.isFinite(r.eth) && r.eth > 0 && Number.isFinite(r.steth) && r.steth > 0;
    const grossReturn = valid ? r.steth / r.eth - 1 : null;
    const annualized = grossReturn != null && Number.isFinite(r.totalDays) && r.totalDays > 0 ? grossReturn * 365 / r.totalDays : null;
    return { bps: grossReturn == null ? null : grossReturn * 1e4, annualized,
      vsStaking: annualized != null && Number.isFinite(stakingApr) ? annualized - stakingApr : null };
  }
  function stSection(rt) {
    if (!rt || !rt.rows.length) return "";
    const n = (x, d = 4) => (x == null || !Number.isFinite(x) ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
    const signed = (x, d = 4) => x == null || !Number.isFinite(x) ? "—" : `${x >= 0 ? "+" : "−"}${n(Math.abs(x), d)}`;
    const pct = (x) => x == null || !Number.isFinite(x) ? "—" : `${signed(x * 100, 2)}%`;
    const days = (x) => x == null || !Number.isFinite(x) ? "—" : n(x, 2);
    const state = (r) => r.signal === "scenario_pass" ? "情景达标 · 待校准" : r.signal === "below_threshold" ? "未达情景门槛" : "资料不足 · 不提示操作";
    const waitLabel = (r) => r.waitSource === "manual" ? "手动假设" : r.waitSource === "official_unvalidated" ? "官方参考 · 未验证" : "参考时间不可用";
    const missing = rt.rows.some((r) => !Number.isFinite(r.steth) || !Number.isFinite(r.totalDays) || !r.expected) || !Number.isFinite(rt.apr);
    const operationalWarnings = [...new Set(rt.rows.flatMap((r) => r.reasons).filter((x) => /暂停|Bunker|读取失败/.test(x)))];
    const queueStale = !rt.queue || !Number.isFinite(rt.queue.blockTimestamp) || Date.now() / 1000 - rt.queue.blockTimestamp > rt.options.quoteMaxAgeSeconds;
    const rows = rt.rows.map((r) => {
      const e = r.expected, gross = stGrossComparison(r, rt.apr);
      const comparison = gross.vsStaking == null ? "无法比较" : `${gross.vsStaking >= 0 ? "高" : "低"} ${n(Math.abs(gross.vsStaking) * 100, 2)} 个百分点`;
      return `<tr><th scope="row" class="num conversion">${n(r.eth, 2)} ETH <span class="conversion-out">→ ${n(r.steth)} stETH</span></th>
        <td data-label="兑换价差" class="r num">${signed(gross.bps, 2)} <span class="unit">bps</span></td>
        <td data-label="全周期假设 x" class="r num">${days(r.totalDays)} <span class="unit">天</span></td>
        <td data-label="价差折合年化" class="r num">${pct(gross.annualized)}<span class="vmeta">扣成本后 ${pct(e && e.apr)}</span></td>
        <td data-label="质押 APR · 7日均值" class="r num">${rt.apr == null ? "—" : n(rt.apr * 100, 2) + "%"}<span class="vmeta">${comparison}</span></td></tr>`;
    }).join("");
    const details = rt.rows.map((r) => {
      const e = r.expected, c = r.conservative;
      const fact = (label, value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;
      const reasons = r.reasons.filter((x) => x !== "独立等待模型尚未校准；报价和 gas 预算均非成交保证");
      return `<section class="redemption-row-detail"><h4>${n(r.eth, 2)} ETH <span class="dim">${state(r)}</span></h4>
        <dl class="redemption-facts">
          ${fact("净利润", signed(e && e.profit) + " ETH")}${fact("净回报", pct(e && e.netReturn))}${fact("买到 stETH", n(r.steth))}${fact("买入路由", esc(ST_VIA[r.via] || "无报价"))}
          ${fact("保守等待", days(r.conservativeDays) + " 天")}${fact("保守净利润", signed(c && c.profit) + " ETH")}
          ${fact("保守净回报", pct(c && c.netReturn))}${fact("保守简单年化", pct(c && c.apr))}
          ${fact("全部投入", n(e && e.cost) + " ETH")}${fact("gas 预算", n(r.gasEth, 5) + " ETH · 保守 ×" + rt.options.gasMultiplier)}
          ${fact("同周期质押收益", signed(e && e.stakingProfit) + " ETH")}${fact("保守超额收益", signed(c && c.excessProfit) + " ETH")}
          ${fact("保守年化较质押", signed(c && c.apr != null && rt.apr != null ? (c.apr - rt.apr) * 100 : null, 2) + " 百分点")}
          ${fact("等待依据", waitLabel(r))}
        </dl>
        <p class="empty">${r.quoteAt ? "报价 " + hm(r.quoteAt) + "；" : ""}${r.gasUnits ? `按 ${r.gasUnits.requests} 个 ≤1000 stETH 申请预算 gas；` : ""}${r.eta && r.eta.calculatedAt ? `官方参考响应计算 ${hm(r.eta.calculatedAt)}，读取 ${hm(r.eta.fetchedAt)}` : ""}</p>
        ${reasons.length ? `<p class="empty">${esc(reasons.join("；"))}</p>` : ""}</section>`;
    }).join("");
    return `<details class="card scenario-panel" id="st-redemption"><summary id="st-redemption-summary"><span>买入 stETH → Lido 赎回</span><span class="scenario-caption">价差 ÷ 天数 × 365${missing ? " · 部分数据缺失" : ""}</span></summary>
      <div class="sect">
      <p class="redemption-assumption">按名义 1:1 赎回；x 含排队和领取，实际等待或兑付额变化会改变收益。</p>
      ${operationalWarnings.length ? `<p class="redemption-availability" role="status">${esc(operationalWarnings.join("；"))}</p>` : ""}
      ${missing || queueStale ? `<p class="redemption-availability" role="status">${queueStale ? "链上队列不可用或已过期。" : ""}${missing ? "缺失或过期的结果显示 —；请设定等待天数或刷新报价。" : ""}</p>` : ""}
      <div class="tw"><table class="src redemption-table"><caption class="sr-only">ETH 买入 stETH 的兑换价差，以假设等待天数折合简单年化，与 Lido 质押 APR 比较</caption><thead><tr><th scope="col">ETH → stETH</th><th scope="col" class="r">兑换价差</th><th scope="col" class="r">全周期假设 x</th><th scope="col" class="r">价差折合年化</th><th scope="col" class="r">质押 APR · 7日均值</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="redemption-footer"><span>年化 = 价差 bps ÷ 100 ÷ x × 365（%）</span><a id="st-edit-settings" href="#st-settings-panel">修改参数</a></div>
      <details class="redemption-details" id="st-redemption-details"><summary id="st-redemption-details-summary">成本、等待假设与来源</summary>
        ${details}
        <section class="redemption-row-detail"><h4>计算口径与共同假设</h4>
        <p class="empty">价差 = (买到 stETH ÷ 投入 ETH − 1) ×10,000 bps。1 bps = 0.01%；价差折合年化只用于收益比较，不代表这笔机会能全年重复。排队 stETH 不再计质押收益。</p>
        <p class="empty">x = 排队假设 + 发布/申请/领取延迟 ${rt.options.extraHours} 小时。质押对照是 Lido 公布的近 7 天平均 APR（非 APY）${rt.aprAt ? "，读取 " + hm(rt.aprAt) : ""}。表中差值是扣成本前的价差年化减质押 APR；“扣成本后”另计 gas 和额外费用。</p>
        <p class="empty">净利润 = 赎回 ETH − 买入投入 − gas − 额外成本；净回报分母包含全部投入；净简单年化 APR = 净回报 ×365÷全周期天数。等待来自未经独立验证的官方参考或手动假设，不是兑付日期承诺。</p>
        <p class="empty">报价已含池费及该数量的价格冲击；保守情景另留 ${rt.options.slippageBps} bps 成交滑点、${rt.options.haircutBps} bps 兑付折损、较长等待及 gas 预算。名义 1:1 兑付可能受亏损、罚没和取整影响；未来 gas 与等待可能更差。压力情景不是置信区间。</p>
        <p class="empty">达标门槛：保守净利润 ≥${rt.options.minProfitEth} ETH、净回报 ≥${rt.options.minNetBps} bps、简单年化 ≥质押 APR +${rt.options.premiumPctPoints} 个百分点。独立等待模型尚未校准；报价和 gas 预算均非成交保证，达标也不代表可以执行。</p>
        <p class="empty">链上未完成队列：${rt.queue && rt.queue.unfinalizedSteth != null ? n(rt.queue.unfinalizedSteth, 0) + " stETH" : "不可用"}；${rt.queue ? `源区块 #${rt.queue.blockNumber} · ${hm(rt.queue.blockTimestamp)} · 读取 ${hm(rt.queue.at)}${queueStale ? " · 已过期" : ""}` : "源区块时间不可用"}。官方 API 内部队列/validator 快照时间：未暴露，时效未知。${rt.aprAt ? "质押 APR 读取 " + hm(rt.aprAt) + "。" : ""}</p>
        </section>
      </details></div></details>`;
  }

  // Keep disclosure choices stable during the 10-second expiry check and live refresh.
  const scenarioIds = ["st-redemption", "st-redemption-details", "cb-redemption"];
  const openScenarios = () => scenarioIds.filter((id) => $(id) && $(id).open);
  const restoreScenarios = (opened) => { for (const id of opened) if ($(id)) $(id).open = true; };
  function redrawScenarios(res) {
    const opened = openScenarios(), focusedId = document.activeElement && document.activeElement.id;
    $("scenarios").innerHTML = stSection(res.stETH.rt) + (res.cbETH.rt ? `<details class="card scenario-panel" id="cb-redemption"><summary><span>cbETH 买入赎回 / 质押卖出</span><span class="scenario-caption">收益测算</span></summary>${rtSection(res.cbETH.rt)}</details>` : "");
    restoreScenarios(opened);
    if (focusedId && $(focusedId) && $(focusedId).focus) $(focusedId).focus({ preventScroll: true });
  }

  /* ---------- 反方向：Coinbase 质押包装 → Base 卖出（溢价时看这个） ---------- */
  function wsSection(rt, n) {
    if (!rt.ws || !rt.ws.length) return "";
    const rows = rt.ws.map((r) => `<tr><td class="num">${r.eth}</td><td class="r num">${n(r.cbeth, 4)}</td>
      <td class="r num">${n(r.back, 4)}</td>
      <td class="r num">${r.diff == null ? "—" : `${r.diff >= 0 ? "+" : "−"}${n(Math.abs(r.diff), 4)}`}<span class="vmeta">${fmt(r.bps, 1)} bps</span></td>
      <td class="dim" style="padding-left:16px">${RT_VIA[r.via] || "—"}</td></tr>`).join("");
    return `<div class="sect"><h3>Coinbase 质押包装 → Base 卖出 <span class="cnt">按兑换率 ${rt.rate.toFixed(6)} 包装，不用排队</span></h3>
      <div class="tw"><table class="src"><thead><tr><th>投入 ETH</th><th class="r">包装得 cbETH</th><th class="r">Base 卖出得 ETH</th>
        <th class="r">差额 ETH</th><th style="padding-left:16px">卖出路由</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="empty" style="margin-top:8px">cbETH 在 Base 上有溢价时看这张：Coinbase 上质押 ETH 后包装成 cbETH 不收费，按兑换率换算；包装好的 cbETH 可以直接走 Base 网络提到钱包，再在 Base 上卖掉。
      提币时 Coinbase 会扣一笔网络费（Base 上通常几美分），这里没有计入。Coinbase 的说明里没有写明刚质押的 ETH 是否马上能包装，包装功能也按地区开放，第一次先用小额试一遍。</p></div>`;
  }

  /* ---------- 走势小图 ---------- */
  function drawSpark(sym, a, now) {
    const el = $(`spark-${sym}`);
    if (!el) return;
    const pts = (hist ? hist.records : [])
      .filter((r) => r.ts >= now - 7 * 86400 && r.ts < now && r[sym] && typeof r[sym].peg === "number")
      .map((r) => [r.ts, r[sym].peg]);
    if (a.peg != null) pts.push([now, a.peg]);
    if (pts.length < 3) {
      el.innerHTML = `<p class="empty">后台快照积累中，攒够几条后这里出走势。</p>`;
      return;
    }
    const b = C.baseline(hist.records, sym, now, CFG.baseline);
    const W = Math.max(280, el.clientWidth), H = 104, L = 46, R = 58, T = 8, B = 20;
    const ys = pts.map((p) => p[1]);
    let lo = Math.min(...ys, 0), hi = Math.max(...ys, 0);
    if (b.ready) { lo = Math.min(lo, b.p10); hi = Math.max(hi, b.p90); }
    if (hi - lo < 2) { const m = (hi + lo) / 2; lo = m - 1; hi = m + 1; }
    const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
    const X = (t) => L + ((t - x0) / Math.max(1, x1 - x0)) * (W - L - R);
    const Y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    let d = "", prev = null;
    for (const [t, v] of pts) { d += `${prev == null || t - prev > 5400 ? "M" : "L"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`; prev = t; }
    const [lt, lv] = pts[pts.length - 1];
    // 纵轴只标 0、最高、最低，彼此离得太近的就不标（0 优先）
    const ticks = [];
    for (const v of [0, hi, lo]) { const y = Y(v); if (ticks.every(([, yy]) => Math.abs(yy - y) >= 14)) ticks.push([v, y]); }
    const band = b.ready ? `<rect class="sp-band" x="${L}" y="${Y(b.p90)}" width="${W - L - R}" height="${Math.max(1, Y(b.p10) - Y(b.p90))}"/>
      <line class="sp-med" x1="${L}" x2="${W - R}" y1="${Y(b.median)}" y2="${Y(b.median)}"/>
      <text class="sp-t" x="${W - R + 6}" y="${Y(b.median) + 4}">常态 ${fmt(b.median)}</text>` : "";
    el.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="${sym} 主报价近 7 天走势">
      <line class="sp-zero" x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}"/>
      ${ticks.map(([v, y]) => `<text class="sp-t" x="${L - 6}" y="${y + 4}" text-anchor="end">${v === 0 ? "0" : fmt(v)}</text>`).join("")}
      ${band}
      <path class="sp-line" d="${d}"/>
      <circle class="sp-dot" cx="${X(lt)}" cy="${Y(lv)}" r="4"/>
      <text class="sp-t" x="${L}" y="${H - 4}">${hm(x0)}</text>
      <text class="sp-t" x="${W - R}" y="${H - 4}" text-anchor="end">现在</text>
      <line class="sp-xh" id="xh-${sym}" y1="${T}" y2="${H - B}" visibility="hidden"/>
      <rect class="sp-hit" x="${L}" y="0" width="${W - L - R}" height="${H}"/>
    </svg><div class="tip" hidden></div>`;
    const svg = el.querySelector("svg"), tip = el.querySelector(".tip"), xh = el.querySelector(".sp-xh");
    const move = (ev) => {
      const r = svg.getBoundingClientRect(), mx = ev.clientX - r.left;
      let best = pts[0];
      for (const p of pts) if (Math.abs(X(p[0]) - mx) < Math.abs(X(best[0]) - mx)) best = p;
      const x = X(best[0]);
      xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.style.left = `${Math.min(Math.max(x, 70), W - 70)}px`;
      tip.style.top = `${Math.max(0, Y(best[1]) - 34)}px`;
      tip.innerHTML = `${best[0] === now ? "现在" : hm(best[0])} · <b class="num">${fmt(best[1], 2)}</b> bps`;
    };
    const leave = () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", leave);
  }

  /* ---------- 顶部结论 ---------- */
  function verdict(res, snap) {
    const worst = C.ASSETS.reduce((w, s) => (C.LEVEL[res[s].status] > C.LEVEL[w] ? res[s].status : w), "dead");
    const allOk = C.ASSETS.every((s) => res[s].status === "ok");
    $("verdict").className = `verdict s-${allOk ? "ok" : worst}`;
    $("vtitle").textContent = allOk
      ? `都在锚附近：${C.ASSETS.map((s) => `${s} ${fmt(res[s].peg)} bps`).join("，")}`
      : C.ASSETS.filter((s) => res[s].status !== "ok").map((s) => `${s} ${C.TEXT[res[s].status]}：${res[s].why}`).join("；");
    let note = `页面每 ${CFG.refreshSeconds} 秒直读一次。`;
    if (hist) {
      const age = snap.ts - hist.updated;
      note += ` 后台每 ${CFG.snapshotMinutes} 分钟采样、当场判断预警并推送；历史 <span class="${age > 30 * 60 ? "stale" : ""}">${ago(age)}</span>同步（每 ${CFG.commitMinutes} 分钟）。`;
    } else note += " 后台快照还在按新口径积累。";
    if (snap.err.length) note += ` 本轮取不到：${esc(snap.err.join("；"))}`;
    $("vnote").innerHTML = note;
  }

  function staticBits() {
    const t = CFG.thresholds;
    $("thr-list").innerHTML = [
      ["正常", `|peg| ≤ ${t.ok} bps`], ["关注", `${t.ok}–${t.watch} bps`],
      ["警戒", `${t.watch}–${t.alert} bps，或最优场所卖 ${t.thinSize} 枚差于 −${t.thinBps} bps`],
      ["危机", `> ${t.alert} bps`], ["源不足", `可用场所少于 ${t.minVenues} 个`],
    ].map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join("");
    const A = CFG.addresses;
    $("addrs").innerHTML = [["stETH", A.stETH], ["wstETH", A.wstETH], ["cbETH", A.cbETH], ["Curve stETH/ETH", A.curveStEth],
      ["Curve stETH-ng", A.curveStEthNg], ["Uniswap QuoterV2", A.uniQuoterV2]]
      .map(([k, v]) => `<li>${k}：<code>${v}</code></li>`).join("");
  }

  // Expire scenario badges against wall-clock time, including slow or failed refreshes.
  function redrawRedemption() {
    const el = $("st-redemption");
    if (!el || !lastSnap || !lastCfg) return;
    const q = Object.assign({}, lastSnap.st, { refreshFailed });
    const opened = openScenarios(), focusedId = document.activeElement && document.activeElement.id;
    el.outerHTML = stSection(C.stRedeem(q, lastCfg, Math.floor(Date.now() / 1000)));
    restoreScenarios(opened);
    if (focusedId && $(focusedId) && $(focusedId).focus) $(focusedId).focus({ preventScroll: true });
  }

  /* ---------- 主循环 ---------- */
  async function run() {
    if (busy) return;
    busy = true;
    $("spin").hidden = false; $("refresh").disabled = true; $("rlabel").textContent = "读取中";
    try {
      const cfg = currentCfg();
      void loadCutoff(); // Independent static file must never hold live quotes or refresh controls.
      const [snap] = await Promise.all([C.collect(cfg), loadHist()]);
      const res = C.evaluate(snap, cfg);
      lastSnap = snap; lastCfg = cfg; refreshFailed = false; redrawCutoff();
      $("cards").innerHTML = C.ASSETS.map((s) => card(s, res[s], snap)).join("");
      redrawScenarios(res);
      for (const s of C.ASSETS) drawSpark(s, res[s], snap.ts);
      verdict(res, snap);
      $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { timeZone: "America/New_York", hour12: false }) + " ET";
    } catch (e) {
      refreshFailed = true; redrawRedemption(); redrawCutoff();
      $("vtitle").textContent = "读取失败";
      $("vnote").textContent = String((e && e.message) || e);
    } finally {
      busy = false; $("spin").hidden = true; $("refresh").disabled = false; $("rlabel").textContent = "刷新";
    }
  }

  async function boot() {
    CFG = await (await fetch("config.json", { cache: "no-store" })).json();
    staticBits();
    $("refresh").addEventListener("click", run);
    const form = $("st-settings");
    const fill = () => {
      for (const el of form.elements) {
        if (!el.name) continue;
        const v = el.name === "amount" ? customAmount : (stOverrides[el.name] ?? CFG.stRedeem[el.name]);
        el.value = v == null ? "" : String(v);
      }
    };
    fill();
    document.addEventListener("click", (ev) => {
      if (ev.target && ev.target.id === "st-edit-settings") $("st-settings-panel").open = true;
    });
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      if (busy) { $("st-settings-status").textContent = "正在读取，请稍后再应用参数"; return; }
      if (!form.reportValidity()) return;
      const next = {};
      for (const el of form.elements) if (el.name && el.name !== "amount") next[el.name] = el.value.trim() === "" ? null : Number(el.value);
      if (next.manualConservativeDays != null && next.manualWaitDays != null && next.manualConservativeDays < next.manualWaitDays) {
        $("st-settings-status").textContent = "保守等待不能短于手动预计等待"; return;
      }
      customAmount = form.elements.amount.value.trim() === "" ? null : Number(form.elements.amount.value);
      stOverrides = next;
      $("st-settings-status").textContent = "已应用；只在本页内存保留，刷新页面会恢复通用示例";
      run();
    });
    $("st-reset").addEventListener("click", () => {
      if (busy) { $("st-settings-status").textContent = "正在读取，请稍后再重置"; return; }
      customAmount = null; stOverrides = {}; fill();
      $("st-settings-status").textContent = "已恢复通用示例参数"; run();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { clearInterval(timer); timer = null; }
      else if (!timer) { run(); timer = setInterval(run, CFG.refreshSeconds * 1000); }
    });
    await run();
    timer = setInterval(run, CFG.refreshSeconds * 1000);
    setInterval(() => { redrawRedemption(); redrawCutoff(); }, 10000);
  }
  boot().catch((e) => { $("vtitle").textContent = "初始化失败"; $("vnote").textContent = String(e); });
})();
