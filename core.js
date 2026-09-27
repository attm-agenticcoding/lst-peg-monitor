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
    cbETH: [
      { id: "coinbase", label: "Coinbase CBETH-ETH", kind: "cex", note: "按买盘逐档吃单" },
      { id: "kyber", label: "KyberSwap 聚合路由", kind: "agg", note: "扫全部 DEX 找最优路径" },
      { id: "uni_cbeth", label: "Uniswap v3 cbETH 0.05%", kind: "dex", note: "QuoterV2" },
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
   * 3. 主报价 = 在 clean 这个量级上执行价最好的场所（= 流动性最深）；peg = 它卖 1 枚的价
   * 4. 状态 = 至少两个场所同时达到的那一档（第二差的场所）—— 单个源坏掉不会误报
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
    const ref = clean != null ? clean : sizes[0];
    // 候选主报价先剔掉离群源（≥3 个场所时，离中位数超过 watch 阈值的不当主报价），
    // 免得一个报错成「高价」的源被当成最深的场所
    const med = median(live.map((v) => v.peg));
    let cand = live.length >= 3 ? live.filter((v) => Math.abs(v.peg - med) <= th.watch) : live;
    if (!cand.length) cand = live;
    let primary = null;
    for (const v of cand) if (v.bps[ref] != null && (!primary || v.bps[ref] > primary.bps[ref])) primary = v;

    let status = "dead", why = `可用场所只有 ${live.length} 个，至少要 ${th.minVenues} 个才能交叉确认`;
    if (live.length >= th.minVenues) {
      const ranked = live.map((v) => ({ v, t: tier(v.peg, th) }))
        .sort((a, b) => LEVEL[b.t] - LEVEL[a.t] || Math.abs(b.v.peg) - Math.abs(a.v.peg));
      status = ranked[1].t;
      if (status !== "ok") {
        const hot = ranked.filter((x) => LEVEL[x.t] >= LEVEL[status]);
        why = `${hot.map((x) => `${x.v.label} ${fmt(x.v.peg)}`).join("、")}：${hot.length} 个场所同时越过 ${th[EDGE[status]]} bps`;
      } else if (ranked[0].t !== "ok") {
        why = `只有 ${ranked[0].v.label}（${fmt(ranked[0].v.peg)} bps）越线，没有第二个场所确认`;
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
    return out;
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

  /** 历史稀疏化：近 fullDays 天全留，更早的每小时留第一条，超过 keepDays 的丢掉。 */
  function thin(records, now, opt) {
    const o = Object.assign({ fullDays: 3, keepDays: 60 }, opt || {});
    const out = [];
    let lastHour = null;
    for (const r of records || []) {
      if (!r || !(r.ts >= now - o.keepDays * 86400)) continue;
      if (r.ts >= now - o.fullDays * 86400) { out.push(r); continue; }
      const h = Math.floor(r.ts / 3600);
      if (h !== lastHour) { out.push(r); lastHour = h; }
    }
    return out;
  }

  function toRecord(snap, res) {
    const rec = { ts: snap.ts };
    for (const sym of ASSETS) {
      const a = res[sym];
      rec[sym] = {
        st: a.status, peg: a.peg, via: a.primary, clean: a.clean, x: a.exit,
        v: Object.fromEntries(a.venues.map((v) => [v.id, v.peg])),
      };
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
    let rpcUrl = null;
    async function ethCalls(calls) {
      const order = rpcUrl ? [rpcUrl, ...cfg.rpcs.filter((u) => u !== rpcUrl)] : cfg.rpcs;
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
          rpcUrl = url;
          return out;
        } catch (e) { last = e; }
      }
      note("rpc", last);
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

    // KyberSwap 对并发很敏感（并发 8 个会回 503 overloaded），所以逐个发，失败的隔 0.8 秒重试一次
    async function kyber() {
      for (const [sym, token] of [["stETH", A.stETH], ["cbETH", A.cbETH]]) {
        let last = null, ok = 0;
        for (const s of sizes) {
          for (let k = 0; k < 2; k++) {
            try {
              const d = await getJSON(`${cfg.http.kyber}?tokenIn=${token}&tokenOut=${A.ETH}&amountIn=${BigInt(s) * E18}`, null, 10000);
              const out = d && d.data && d.data.routeSummary && d.data.routeSummary.amountOut;
              if (!out) throw new Error((d && d.message) || "无报价");
              put(sym, "kyber", s, Number(BigInt(out)) / 1e18 / s);
              ok++;
              break;
            } catch (e) { last = e; if (k === 0) await new Promise((r) => setTimeout(r, 800)); }
          }
        }
        if (ok < sizes.length) note(`kyber ${sym}（${ok}/${sizes.length}）`, last);
      }
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
      kyber(),
      book("stETH", "okx", cfg.http.okxBook, (d) => d && d.data && d.data[0] && d.data[0].bids),
      book("cbETH", "coinbase", cfg.http.coinbaseBook, (d) => d && d.bids),
    ]);
    return { ts: Math.floor(Date.now() / 1000), nav, px, err };
  }

  return { VENUES, ASSETS, LEVEL, TEXT, quantile, median, fmt, tier, sellBook, analyse, evaluate, baseline, thin, toRecord, collect };
});
