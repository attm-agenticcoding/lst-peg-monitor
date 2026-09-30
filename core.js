/* core.js — 取数 + 计算。浏览器（app.js）和 Actions 快照（scripts/snapshot.js）共用这一份，
 * 所以网页上看到的数、历史里存的数、告警用的数是同一套算法，不会两边漂移。 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PegCore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* 场所。stETH 与 wstETH 可按 stEthPerToken 原子互换，合成一行看：
   * wstETH 的报价折成 stETH 口径后和 stETH 的报价放在一起比，谁深用谁。 */
  const VENUES = {
    stETH: [
      { id: "kyber", label: "KyberSwap 聚合路由", kind: "agg", note: "扫全部 DEX 找最优路径（含 Lido ARM、Fluid，按需 wrap 成 wstETH），≈ LlamaSwap 上看到的报价" },
      { id: "curve", label: "Curve stETH/ETH", kind: "dex", note: "原池，get_dy" },
      { id: "curve_ng", label: "Curve stETH-ng", kind: "dex", note: "get_dy" },
      { id: "uni_wsteth", label: "Uniswap v3 wstETH 0.01%", kind: "dex", note: "卖出等值的 wstETH，按 stEthPerToken 折成 stETH" },
      { id: "okx", label: "OKX STETH-ETH", kind: "cex", note: "按买盘逐档吃单" },
    ],
    // cbETH 的链上流动性主要在 Base（Coinbase 自家 L2），主网池子很薄
    cbETH: [
      { id: "kyber_base", label: "KyberSwap 聚合 · Base", kind: "agg", note: "Base 上扫全部 DEX（Maverick、Hydrex、Aerodrome、Uniswap v4…）" },
      { id: "aero_base", label: "Aerodrome cbETH/WETH · Base", kind: "dex", note: "Slipstream 池（费率 0.006%），QuoterV2 直读链上" },
      { id: "coinbase", label: "Coinbase CBETH-ETH", kind: "cex", note: "按买盘逐档吃单" },
      { id: "kyber", label: "KyberSwap 聚合 · 主网", kind: "agg", note: "主网扫全部 DEX" },
      { id: "uni_cbeth", label: "Uniswap v3 cbETH 0.05% · 主网", kind: "dex", note: "QuoterV2" },
    ],
  };
  const ASSETS = Object.keys(VENUES);
  const LEVEL = { dead: -1, ok: 0, watch: 1, alert: 2, crisis: 3 };
  const TEXT = { ok: "正常", watch: "关注", alert: "警戒", crisis: "危机", dead: "源不足" };
  const EDGE = { watch: "ok", alert: "watch", crisis: "alert" }; // 进入该档要越过的阈值名

  /* ---------- 小工具 ---------- */
  const r2 = (x) => (x == null || !isFinite(x) ? null : Math.round(x * 100) / 100);
  const num = (x) => typeof x === "number" && isFinite(x);
  function quantile(arr, q) {
    const a = (arr || []).filter(num).sort((x, y) => x - y);
    if (!a.length) return null;
    const i = (a.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
    return a[lo] + (a[hi] - a[lo]) * (i - lo);
  }
  const median = (a) => quantile(a, 0.5);
  const fmt = (x, d = 1) => (num(x) ? (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(d) : "—");
  const tier = (bps, th) => {
    const a = Math.abs(bps);
    return a > th.alert ? "crisis" : a > th.watch ? "alert" : a > th.ok ? "watch" : "ok";
  };

  /** 按买盘逐档吃单，返回平均成交价；深度不够返回 null。bids: [[price, qty, ...]]，价高在前。 */
  function sellBook(bids, size) {
    let left = size, got = 0;
    for (const b of bids || []) {
      const p = +b[0], q = +b[1];
      if (!(p > 0) || !(q > 0)) continue;
      const x = Math.min(left, q);
      got += x * p; left -= x;
      if (left <= 1e-12) return got / size;
    }
    return null;
  }

  /* ---------- 单个代币的判定 ----------
   * px: {venueId: {size: 每枚换回的 ETH}}；nav: 兑付锚（每枚背后的 ETH）
   * 1. 各场所各量级的执行价 → 相对兑付锚的 bps（负 = 折价）
   * 2. 可退出规模 clean = 最优场所在 cleanExitBps 内还卖得掉的最大量级
   * 3. 主报价 = 在「最优执行价不差于 −200 bps 的最大量级」上执行价最好的场所（= 流动性最深）；peg = 它卖 1 枚的价
   * 4. 状态 = 主报价所在的档，但至少还要一个别的场所也到这一档才算（取两者中较轻的那档）
   * 5. 最优场所卖 thinSize 枚都差于 −thinBps → 至少警戒（深度塌了） */
  function analyse(px, nav, venues, th, sizes) {
    const V = venues.map((v) => {
      const p = (px && px[v.id]) || {}, bps = {};
      for (const s of sizes) bps[s] = nav > 0 && num(p[s]) ? r2((p[s] / nav - 1) * 1e4) : null;
      return Object.assign({}, v, { bps, peg: bps[sizes[0]] });
    });
    const live = V.filter((v) => v.peg != null);
    const exit = {};
    for (const s of sizes) {
      const xs = V.map((v) => v.bps[s]).filter((x) => x != null);
      exit[s] = xs.length ? Math.max(...xs) : null;
    }
    let clean = null;
    for (const s of sizes) if (exit[s] != null && exit[s] >= -th.cleanExitBps) clean = s;
    // 「最深」在最优执行价还不差于 −alert 的最大量级上比 —— 比 clean（50 bps）宽，
    // 这样深池在压力下跌到 −80 时，不会被一个只挂着小单、价格还没跟上的薄盘口顶替成主报价
    let ref = sizes[0];
    for (const s of sizes) if (exit[s] != null && exit[s] >= -th.alert) ref = s;
    // 候选主报价先剔掉离群源（≥3 个场所时，离中位数超过 watch 阈值的不当主报价），
    // 免得一个报错成「高价」的源被当成最深的场所
    const med = median(live.map((v) => v.peg));
    let cand = live.length >= 3 ? live.filter((v) => Math.abs(v.peg - med) <= th.watch) : live;
    if (!cand.length) cand = live;
    let primary = null;
    for (const v of cand) if (v.bps[ref] != null && (!primary || v.bps[ref] > primary.bps[ref])) primary = v;

    let status = "dead", why = `可用场所只有 ${live.length} 个，至少要 ${th.minVenues} 个才能交叉确认`;
    if (live.length >= th.minVenues && primary) {
      // 主报价（最深的场所）决定档位，但至少还要一个别的场所也到这一档才算数：
      // 薄池子自己漂（主网 cbETH 池常年 −13 bps）不会误报，单个源报错也不会
      const tP = tier(primary.peg, th);
      const others = live.filter((v) => v !== primary).map((v) => ({ v, t: tier(v.peg, th) }))
        .sort((a, b) => LEVEL[b.t] - LEVEL[a.t] || Math.abs(b.v.peg) - Math.abs(a.v.peg));
      const tO = others.length ? others[0].t : "ok";
      status = LEVEL[tP] <= LEVEL[tO] ? tP : tO;
      if (status !== "ok") {
        const conf = others.filter((x) => LEVEL[x.t] >= LEVEL[status]).map((x) => `${x.v.label} ${fmt(x.v.peg)}`);
        why = `主报价 ${primary.label} ${fmt(primary.peg)}，${conf.join("、")} 确认：越过 ${th[EDGE[status]]} bps`;
      } else if (tP !== "ok") {
        why = `主报价 ${primary.label}（${fmt(primary.peg)} bps）越线，但没有别的场所确认`;
      } else if (tO !== "ok") {
        why = `主报价在 ±${th.ok} bps 内；${others[0].v.label}（${fmt(others[0].v.peg)}）偏离，但它不是主要流动性`;
      } else {
        why = `${live.length} 个场所都在 ±${th.ok} bps 以内`;
      }
      const t = exit[th.thinSize];
      if (t != null && t < -th.thinBps && LEVEL[status] < LEVEL.alert) {
        status = "alert";
        why = `卖 ${th.thinSize} 枚的最优执行价 ${fmt(t)} bps，差于 −${th.thinBps}：深度塌了`;
      }
    }
    return {
      venues: V, exit, clean, ref, live: live.length, status, why,
      primary: primary ? primary.id : null,
      primaryLabel: primary ? primary.label : null,
      peg: primary ? primary.peg : null,
    };
  }

  function evaluate(snap, cfg) {
    const out = {};
    for (const sym of ASSETS)
      out[sym] = analyse(snap.px[sym], sym === "stETH" ? 1 : snap.nav[sym], VENUES[sym], cfg.thresholds, cfg.sizes);
    out.cbETH.rt = roundTrip(snap.rt, snap.nav.cbETH, cfg);
    return out;
  }

  /* ---------- Base 买入 cbETH → Coinbase 赎回成 ETH ----------
   * 买入：KyberSwap 在 Base 上的聚合报价和 Aerodrome 单池报价，取买到 cbETH 更多的那个；
   * 赎回：Coinbase 公布的兑换率（取不到就用主网合约的 exchangeRate），unwrap 不收费；
   * 差额 = 赎回得到的 ETH − 投入的 ETH。赎回得到的是 Coinbase 上的质押 ETH，要变成可用 ETH
   * 还得排以太坊退出队列，所以另外按 Coinbase 的排队天数估计折成年化，方便和质押收益比。 */
  function roundTrip(q, navOnchain, cfg) {
    const sizes = (cfg.roundTrip && cfg.roundTrip.sizes) || [];
    if (!q) return null;
    const rate = num(q.rate) && q.rate > 0 ? q.rate : navOnchain, src = num(q.rate) && q.rate > 0 ? "coinbase" : "onchain";
    if (!(rate > 0)) return null;
    const rows = sizes.map((x) => {
      const k = q.kyber && q.kyber[x], a = q.aero && q.aero[x];
      const best = num(k) && (!num(a) || k >= a) ? { cb: k, via: "kyber_base" } : num(a) ? { cb: a, via: "aero_base" } : null;
      if (!best) return { eth: x, cbeth: null, back: null, diff: null, bps: null, apr: null, via: null };
      const back = best.cb * rate, diff = back - x, bps = (diff / x) * 1e4;
      const apr = num(q.waitDays) && q.waitDays > 0 ? (bps / 1e4) * (365 / q.waitDays) : null;
      return { eth: x, cbeth: best.cb, back, diff, bps: r2(bps), apr, via: best.via };
    });
    // 反方向：Coinbase 上把 X ETH 质押并包装成 X / 兑换率 枚 cbETH（不收费），提到 Base 上卖掉
    const ws = ((cfg.wrapSell && cfg.wrapSell.sizes) || []).map((x) => {
      const k = q.ws && q.ws.kyber && q.ws.kyber[x], a = q.ws && q.ws.aero && q.ws.aero[x];
      const best = num(k) && (!num(a) || k >= a) ? { back: k, via: "kyber_base" } : num(a) ? { back: a, via: "aero_base" } : null;
      if (!best) return { eth: x, cbeth: x / rate, back: null, diff: null, bps: null, via: null };
      const diff = best.back - x;
      return { eth: x, cbeth: x / rate, back: best.back, diff, bps: r2((diff / x) * 1e4), via: best.via };
    });
    return { rate, src, waitDays: num(q.waitDays) ? q.waitDays : null, apy: num(q.apy) ? q.apy : null, onchain: navOnchain, rows, ws };
  }

  /* ---------- 常态基线 ----------
   * 主报价 peg 的近 N 天分布。先按小时分桶取中位数，再对小时序列取分位 ——
   * 历史是稀疏化存的（近几天 10 分钟一条、更早每小时一条），不分桶的话近几天会被重复计权。 */
  function baseline(records, sym, now, opt) {
    const o = Object.assign({ days: 30, minHours: 12 }, opt || {});
    const since = now - o.days * 86400, buckets = new Map();
    for (const r of records || []) {
      const x = r && r[sym] ? r[sym].peg : null;
      if (!r || !(r.ts >= since) || !num(x)) continue;
      const h = Math.floor(r.ts / 3600);
      if (!buckets.has(h)) buckets.set(h, []);
      buckets.get(h).push(x);
    }
    const vals = [...buckets.values()].map(median);
    const b = { hours: vals.length, ready: vals.length >= o.minHours };
    if (b.ready) {
      b.median = r2(median(vals));
      b.mean = r2(vals.reduce((a, c) => a + c, 0) / vals.length);
      b.p10 = r2(quantile(vals, 0.1));
      b.p90 = r2(quantile(vals, 0.9));
    }
    return b;
  }

  /** 历史分层稀疏化。tiers = [[天数, 间隔秒], …]（天数递增）：落在某层的记录，每个间隔桶只留第一条；
   *  间隔 0 = 全留；比最后一层还旧的丢掉。 */
  function thin(records, now, tiers) {
    const T = tiers || [[1, 0], [3, 600], [60, 3600]];
    const out = [], seen = new Set();
    for (const r of records || []) {
      if (!r || !num(r.ts)) continue;
      const age = now - r.ts, i = T.findIndex(([d]) => age <= d * 86400);
      if (i < 0) continue;
      const sp = T[i][1];
      if (!sp) { out.push(r); continue; }
      const key = i + ":" + Math.floor(r.ts / sp);
      if (!seen.has(key)) { seen.add(key); out.push(r); }
    }
    return out;
  }

  /* ---------- 撤离预警：偏离自己的历史基线 ----------
   * 每个场所、每个指标（卖 1 枚 / 卖大额）各自对照自己的历史，不和别的场所混：
   *   窗口 24h / 7天 / 30天，都排除最近 excludeMinutes（免得正在发生的偏离把基线拖走）；
   *   7天、30天先每小时取一个点（历史本来就是分层稀疏的，这样各段权重一致）；
   *   中心 = 中位数，尺度 = 1.4826×MAD —— 不用均值和标准差：报价是离散跳档的、偶有无害尖刺，
   *   均值/σ 会被尖刺撑大；中位数/MAD 在窗口里混进一半的异常值之前都不会被拖走。
   *   预警线 = 中心 − max(k×尺度, floor)；各窗口取最紧（最高）的那条。只看折价一侧。 */
  function guardLevel(series, t, g) {
    let best = null;
    for (const w of g.windows) {
      const lo = t - w.hours * 3600, hi = t - g.excludeMinutes * 60;
      let pts = series.filter((p) => p[0] >= lo && p[0] < hi && num(p[1]));
      if (w.hourly) {
        const seen = new Set();
        pts = pts.filter((p) => { const k = Math.floor(p[0] / 3600); if (seen.has(k)) return false; seen.add(k); return true; });
      }
      if (pts.length < (w.hourly ? Math.min(g.minPoints, w.minHours) : g.minPoints)) continue;
      if (pts[pts.length - 1][0] - pts[0][0] < w.minHours * 3600) continue;
      const vals = pts.map((p) => p[1]), c = median(vals);
      const sc = 1.4826 * median(vals.map((v) => Math.abs(v - c)));
      const level = c - Math.max(g.k * sc, g.floorBps);
      if (!best || level > best.level) best = { level: r2(level), center: r2(c), scale: r2(sc), win: w.name, n: pts.length };
    }
    return best;
  }

  /** 对最新一条记录 rec，算每个代币、每个指标的预警线，以及有几个场所跌破。
   *  records = 之前的历史（可以含 rec 本身，最近一段本来就被排除）。 */
  function guard(records, rec, cfg) {
    const g = cfg.guard, out = {};
    for (const sym of ASSETS) {
      const a = rec[sym] || {}, res = {};
      for (const [m, key, size] of [["peg", "v", 1], ["big", "vb", g.bigSize[sym]]]) {
        const venues = [];
        for (const v of VENUES[sym]) {
          const x = a[key] ? a[key][v.id] : null;
          if (!num(x)) continue;
          const L = guardLevel((records || []).map((r) => [r.ts, r[sym] && r[sym][key] ? r[sym][key][v.id] : null]), rec.ts, g);
          // 平时卖这个量就要亏超过「关注」线（25 bps）的场所，本来就不是这个量级的出口，它的抖动不算确认。
          // 2026-09-29 08:01 ET：主网聚合路由卖 100 cbETH 常态 −33 bps、当时跳到 −183，加上 Coinbase 盘口
          // 变薄 3 bps，凑成两个场所误报了一次 —— 而真正的出口 Base 那时是 +0.2 bps。
          const thinVenue = L && L.center < -cfg.thresholds.ok;
          if (L) venues.push(Object.assign({ id: v.id, label: v.label, x, thin: thinVenue, hit: !thinVenue && x < L.level }, L));
        }
        const hits = venues.filter((v) => v.hit);
        // 展示用的「代表场所」：卖 1 枚用主报价场所，卖大额用这一档执行价最好的场所
        const rep = m === "peg" ? venues.find((v) => v.id === a.via)
          : venues.slice().sort((p, q) => q.x - p.x)[0];
        res[m] = { size, hit: hits.length >= g.minVenues, hits: hits.map((v) => v.id), venues, rep: rep || venues[0] || null };
      }
      out[sym] = res;
    }
    return out;
  }

  function toRecord(snap, res, cfg) {
    const rec = { ts: snap.ts };
    for (const sym of ASSETS) {
      const a = res[sym], big = cfg && cfg.guard ? cfg.guard.bigSize[sym] : null;
      rec[sym] = {
        st: a.status, peg: a.peg, via: a.primary, clean: a.clean, x: a.exit,
        v: Object.fromEntries(a.venues.map((v) => [v.id, v.peg])),
      };
      if (big) rec[sym].vb = Object.fromEntries(a.venues.map((v) => [v.id, v.bps[big]]));
      if (a.rt) {
        rec[sym].rt = Object.fromEntries(a.rt.rows.map((r) => [r.eth, r.bps]));
        if (a.rt.ws && a.rt.ws.length) rec[sym].ws = Object.fromEntries(a.rt.ws.map((r) => [r.eth, r.bps]));
        if (a.rt.waitDays != null) rec[sym].wait = a.rt.waitDays;
      }
    }
    rec.nav = { wstETH: snap.nav.wstETH, cbETH: snap.nav.cbETH };
    if (snap.err.length) rec.err = snap.err;
    return rec;
  }

  /* ---------- 取数 ---------- */
  async function collect(cfg, fetchFn) {
    const F = fetchFn || fetch;
    const A = cfg.addresses, sizes = cfg.sizes, err = [];
    const E18 = 10n ** 18n;
    const pad = (h) => String(h).replace(/^0x/, "").toLowerCase().padStart(64, "0");
    const U = (n) => pad(BigInt(n).toString(16));
    const word0 = (h) => (typeof h === "string" && /^0x[0-9a-f]{64}/i.test(h) ? BigInt(h.slice(0, 66)) : null);
    const note = (who, e) => err.push(`${who}: ${e && e.name === "AbortError" ? "超时" : (e && e.message) || e}`);
    const px = { stETH: {}, cbETH: {} };
    const put = (sym, id, s, v) => { if (num(v) && v > 0) (px[sym][id] = px[sym][id] || {})[s] = v; };

    async function getJSON(url, init, ms) {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), ms || 12000);
      try {
        const r = await F(url, Object.assign({ signal: ctl.signal }, init || {}));
        if (!r.ok) throw new Error("HTTP " + r.status);
        return await r.json();
      } finally { clearTimeout(t); }
    }

    // 公开 RPC：小批量发（大批量会被断开），一个节点不行换下一个
    const sticky = {};
    async function ethCalls(calls, chain) {
      const list = chain === "base" ? cfg.base.rpcs : cfg.rpcs, rpcUrl = sticky[chain || "eth"];
      const order = rpcUrl ? [rpcUrl, ...list.filter((u) => u !== rpcUrl)] : list;
      let last = null;
      for (const url of order) {
        try {
          const out = new Array(calls.length).fill(null), n = cfg.rpcBatchSize || 4;
          for (let i = 0; i < calls.length; i += n) {
            const body = calls.slice(i, i + n).map(([to, data], j) =>
              ({ jsonrpc: "2.0", id: i + j, method: "eth_call", params: [{ to, data }, "latest"] }));
            const res = await getJSON(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }, 15000);
            if (!Array.isArray(res)) throw new Error("节点不接受批量请求");
            for (const x of res) if (x && x.id >= 0 && x.id < out.length) out[x.id] = x.result || null;
          }
          sticky[chain || "eth"] = url;
          return out;
        } catch (e) { last = e; }
      }
      note(`rpc ${chain || "eth"}`, last);
      return calls.map(() => null);
    }

    const chain = (async () => {
      // 1) 兑付锚 + Curve 的 coin 顺序（每轮实时验证，顺序反了会静默返回倒数）
      const r1 = await ethCalls([
        [A.wstETH, "0x035faf82"],                      // stEthPerToken()
        [A.cbETH, "0x3ba0b9a9"],                       // exchangeRate()
        [A.curveStEth, "0xc6610657" + U(0)],           // coins(0)
        [A.curveStEthNg, "0xc6610657" + U(0)],
      ]);
      const rateW = word0(r1[0]), rateC = word0(r1[1]);
      const ord = (h) => (h && word0(h) != null
        ? ("0x" + h.slice(-40)).toLowerCase() === A.ETH.toLowerCase() ? [1, 0] : [0, 1] : null); // [i=stETH, j=ETH]
      const oA = ord(r1[2]), oB = ord(r1[3]);

      // 2) 各量级卖出报价
      const calls = [], keys = [];
      const add = (sym, id, s, to, data) => { keys.push([sym, id, s]); calls.push([to, data]); };
      const quote = (tin, amt, fee) => "0xc6a5026a" + pad(tin) + pad(A.WETH) + U(amt) + U(fee) + U(0);
      for (const s of sizes) {
        const dx = BigInt(s) * E18;
        if (oA) add("stETH", "curve", s, A.curveStEth, "0x5e0d443f" + U(oA[0]) + U(oA[1]) + U(dx));
        if (oB) add("stETH", "curve_ng", s, A.curveStEthNg, "0x5e0d443f" + U(oB[0]) + U(oB[1]) + U(dx));
        if (rateW) add("stETH", "uni_wsteth", s, A.uniQuoterV2, quote(A.wstETH, (dx * E18) / rateW, cfg.uniFee.wstETH));
        add("cbETH", "uni_cbeth", s, A.uniQuoterV2, quote(A.cbETH, dx, cfg.uniFee.cbETH));
      }
      const r2_ = await ethCalls(calls);
      r2_.forEach((h, i) => {
        const w = word0(h), [sym, id, s] = keys[i];
        if (w != null) put(sym, id, s, Number(w) / 1e18 / s);
      });
      return { stETH: 1, wstETH: rateW ? Number(rateW) / 1e18 : null, cbETH: rateC ? Number(rateC) / 1e18 : null };
    })();

    // Base：Aerodrome Slipstream cbETH/WETH，QuoterV2.quoteExactInputSingle((tokenIn,tokenOut,amountIn,int24 tickSpacing,sqrtPriceLimitX96))
    const rtSizes = (cfg.roundTrip && cfg.roundTrip.sizes) || [], rt = {};
    const wsSizes = (cfg.wrapSell && cfg.wrapSell.sizes) || [];
    const cbinfoP = cbInfo();
    // 包装得到多少 cbETH 要先知道兑换率：优先 Coinbase 公布的，取不到用主网合约 exchangeRate
    const rateP = (async () => { await cbinfoP; if (num(rt.rate) && rt.rate > 0) return rt.rate; const n = await chain; return n.cbETH; })();
    const wsWei = async () => { const r = await rateP; return r > 0 ? wsSizes.map((x) => BigInt(Math.floor((x / r) * 1e18))) : []; };
    const base = (async () => {
      const B = cfg.base;
      const r = await ethCalls(sizes.map((s) => [B.aeroQuoter, "0x9e7defe6" + pad(B.cbETH) + pad(B.WETH)
        + U(BigInt(s) * E18) + U(B.aeroTickSpacing) + U(0)]), "base");
      r.forEach((h, i) => { const w = word0(h); if (w != null) put("cbETH", "aero_base", sizes[i], Number(w) / 1e18 / sizes[i]); });
      // 买入方向（Base 上用 ETH 买 cbETH），给「Base 买入 → Coinbase 赎回」算账
      const rb = await ethCalls(rtSizes.map((x) => [B.aeroQuoter, "0x9e7defe6" + pad(B.WETH) + pad(B.cbETH)
        + U(BigInt(x) * E18) + U(B.aeroTickSpacing) + U(0)]), "base");
      rb.forEach((h, i) => { const w = word0(h); if (w != null) (rt.aero = rt.aero || {})[rtSizes[i]] = Number(w) / 1e18; });
      // 反方向：Coinbase 上质押包装出来的 cbETH 拿到 Base 上卖（Aerodrome 单池）
      const amts = await wsWei();
      if (amts.length) {
        const rs = await ethCalls(amts.map((w) => [B.aeroQuoter, "0x9e7defe6" + pad(B.cbETH) + pad(B.WETH) + U(w) + U(B.aeroTickSpacing) + U(0)]), "base");
        rs.forEach((h, i) => { const w = word0(h); if (w != null) ((rt.ws = rt.ws || {}).aero = rt.ws.aero || {})[wsSizes[i]] = Number(w) / 1e18; });
      }
    })();

    // KyberSwap 对并发很敏感（并发 8 个会回 503 overloaded），所以逐个发，失败的隔 0.8 秒重试一次
    async function kyber() {
      const jobs = [
        { name: "kyber stETH", url: cfg.http.kyber, tin: A.stETH, tout: A.ETH, sizes, put: (s, o) => put("stETH", "kyber", s, o / s) },
        { name: "kyber_base cbETH", url: cfg.http.kyberBase, tin: cfg.base.cbETH, tout: A.ETH, sizes, put: (s, o) => put("cbETH", "kyber_base", s, o / s) },
        { name: "kyber cbETH", url: cfg.http.kyber, tin: A.cbETH, tout: A.ETH, sizes, put: (s, o) => put("cbETH", "kyber", s, o / s) },
        { name: "kyber_base 买入", url: cfg.http.kyberBase, tin: A.ETH, tout: cfg.base.cbETH, sizes: rtSizes, put: (x, o) => { (rt.kyber = rt.kyber || {})[x] = o; } },
      ];
      // 反方向：包装得到的 cbETH 在 Base 上的聚合卖出报价（数量 = X / 兑换率，所以排在最后、等兑换率到手）
      if (wsSizes.length) jobs.push({ name: "kyber_base 包装后卖出", url: cfg.http.kyberBase, tin: cfg.base.cbETH, tout: A.ETH, sizes: wsSizes,
        amount: async (x) => { const a = await wsWei(); return a[wsSizes.indexOf(x)]; },
        put: (x, o) => { ((rt.ws = rt.ws || {}).kyber = rt.ws.kyber || {})[x] = o; } });
      for (const j of jobs) {
        let last = null, ok = 0;
        for (const s of j.sizes) {
          for (let k = 0; k < 2; k++) {
            try {
              const amt = j.amount ? await j.amount(s) : BigInt(s) * E18;
              if (!amt) throw new Error("没有兑换率");
              const d = await getJSON(`${j.url}?tokenIn=${j.tin}&tokenOut=${j.tout}&amountIn=${amt}`, null, 10000);
              const out = d && d.data && d.data.routeSummary && d.data.routeSummary.amountOut;
              if (!out) throw new Error((d && d.message) || "无报价");
              j.put(s, Number(BigInt(out)) / 1e18);
              ok++;
              break;
            } catch (e) { last = e; if (k === 0) await new Promise((r) => setTimeout(r, 800)); }
          }
        }
        if (ok < j.sizes.length) note(`${j.name}（${ok}/${j.sizes.length}）`, last);
      }
    }
    // Coinbase 自己公布的 cbETH 兑换率、赎回排队天数估计、质押年化
    async function cbInfo() {
      try {
        const d = await getJSON(cfg.http.coinbaseCbethInfo, null, 10000);
        rt.rate = +d.conversion_rate; rt.waitDays = +d.redeem_time_estimate_days; rt.apy = +d.apy;
      } catch (e) { note("coinbase 兑换率", e); }
    }
    async function book(sym, id, url, pick) {
      try {
        const bids = pick(await getJSON(url, null, 10000));
        if (!bids || !bids.length) throw new Error("盘口为空");
        for (const s of sizes) put(sym, id, s, sellBook(bids, s));
      } catch (e) { note(id, e); }
    }

    const [nav] = await Promise.all([
      chain,
      base,
      kyber(),
      book("stETH", "okx", cfg.http.okxBook, (d) => d && d.data && d.data[0] && d.data[0].bids),
      book("cbETH", "coinbase", cfg.http.coinbaseBook, (d) => d && d.bids),
      cbinfoP,
    ]);
    return { ts: Math.floor(Date.now() / 1000), nav, px, rt, err };
  }

  return { VENUES, ASSETS, LEVEL, TEXT, quantile, median, fmt, tier, sellBook, analyse, evaluate, roundTrip, baseline, thin, guardLevel, guard, toRecord, collect };
});
