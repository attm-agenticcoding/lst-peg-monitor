/* app.js — 数据层 + 渲染。计算逻辑全在 engine.js。 */
(() => {
"use strict";
const E = window.PegEngine;
let CFG = null;

/* ============================ 工具 ============================ */
const E18 = 10n ** 18n;
const pad = (h) => String(h).replace(/^0x/, "").toLowerCase().padStart(64, "0");
const eU  = (n) => pad(BigInt(n).toString(16));
const eA  = (a) => pad(a);
const w   = (h, i) => "0x" + h.replace(/^0x/, "").slice(i * 64, i * 64 + 64);
const big = (h) => { try { return BigInt(h); } catch { return null; } };
const int = (h) => { const v = BigInt(h); return v >> 255n ? v - (1n << 256n) : v; };
const $   = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
const nf  = (x, d=6) => (x == null || !isFinite(x)) ? "—" : x.toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
const bps = (x, d=1) => (x == null || !isFinite(x)) ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(d);
const ago = (s) => !isFinite(s) || s < 0 ? "时间异常"
  : s < 90 ? Math.round(s) + " 秒前"
  : s < 5400 ? Math.round(s/60) + " 分钟前"
  : (s/3600).toFixed(1) + " 小时前";

/* ============================ RPC ============================ */
let rpcUrl = null;
async function post(url, body, ms = 12000) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify(body), signal: ctl.signal });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } finally { clearTimeout(t); }
}
async function pickRpc() {
  if (rpcUrl) return rpcUrl;
  for (const u of CFG.rpcs) {
    try {
      const r = await post(u, { jsonrpc:"2.0", id:1, method:"eth_blockNumber", params:[] }, 7000);
      if (r && r.result) { rpcUrl = u; return u; }
    } catch (_) {}
  }
  throw new Error("公开节点都连不上（网络或 CORS 拦住了）");
}
/** calls: [{key,to,data}] → {key: hex|null}。分批发，单批失败不影响其他批。 */
async function ethCalls(calls) {
  const url = await pickRpc();
  const out = {};
  const n = CFG.rpcBatchSize || 4;
  const chunks = [];
  for (let i = 0; i < calls.length; i += n) chunks.push(calls.slice(i, i + n));
  await Promise.all(chunks.map(async (ch) => {
    const payload = ch.map((c, i) => ({ jsonrpc:"2.0", id:i+1, method:"eth_call",
      params:[{ to:c.to, data:c.data }, "latest"] }));
    try {
      const j = await post(url, payload, 14000);
      if (Array.isArray(j)) { for (const x of j) { const c = ch[(x.id|0)-1]; if (c) out[c.key] = x.result || null; } return; }
    } catch (_) {}
    await Promise.all(ch.map(async (c) => {
      try { const r = await post(url, { jsonrpc:"2.0", id:1, method:"eth_call",
        params:[{to:c.to,data:c.data},"latest"] }, 10000); out[c.key] = r.result || null; }
      catch { out[c.key] = null; }
    }));
  }));
  return out;
}

/* ============================ HTTP 源 ============================ */
async function getJSON(url, ms = 9000) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } finally { clearTimeout(t); }
}
async function llama() {
  const A = CFG.addresses;
  const ids = [A.stETH, A.wstETH, A.cbETH, A.WETH].map(a => "ethereum:" + a).join(",");
  const d = await getJSON(CFG.http.llama + ids);
  const pick = (a) => {
    const k = Object.keys(d.coins || {}).find(k => k.toLowerCase() === ("ethereum:" + a).toLowerCase());
    return k ? d.coins[k] : null;
  };
  const weth = pick(A.WETH);
  if (!weth || !weth.price) throw new Error("缺 WETH 报价");
  const out = {};
  for (const [sym, addr] of [["stETH",A.stETH],["wstETH",A.wstETH],["cbETH",A.cbETH]]) {
    const c = pick(addr);
    if (c && c.price) out[sym] = { price: c.price / weth.price, conf: c.confidence };
  }
  return out;
}
/* CoinGecko keyless 档每次只收 1 个合约地址，所以分三次拿。 */
async function gecko() {
  const A = CFG.addresses, out = {};
  for (const [sym, addr] of [["stETH",A.stETH],["wstETH",A.wstETH],["cbETH",A.cbETH]]) {
    try {
      const d = await getJSON(`${CFG.http.gecko}?contract_addresses=${addr}&vs_currencies=eth`, 8000);
      const k = Object.keys(d).find(k => k.toLowerCase() === addr.toLowerCase());
      if (k && d[k] && d[k].eth) out[sym] = { price: d[k].eth };
    } catch (_) {}
  }
  if (!Object.keys(out).length) throw new Error("全部失败或被限流");
  return out;
}
async function okx() {
  const d = await getJSON(CFG.http.okxStEth);
  const t = d && d.data && d.data[0];
  if (!t) throw new Error("无数据");
  const bid = +t.bidPx, ask = +t.askPx, mid = (bid > 0 && ask > 0) ? (bid + ask) / 2 : +t.last;
  if (!(mid > 0)) throw new Error("报价异常");
  return { price: mid, bid, ask, vol: +(t.volCcy24h || t.vol24h || 0) };
}
async function coinbase() {
  const d = await getJSON(CFG.http.coinbaseCbEth);
  const bid = +d.bid, ask = +d.ask, mid = (bid > 0 && ask > 0) ? (bid + ask) / 2 : +d.price;
  if (!(mid > 0)) throw new Error("报价异常");
  return { price: mid, bid, ask, vol: +(d.volume || 0) };
}

/* ============================ 取数 ============================ */
async function fetchAll() {
  const A = CFG.addresses, S = CFG.selectors, U = CFG.uniswap, SZ = CFG.sizes;
  const now = Math.floor(Date.now() / 1000);

  const r1 = await ethCalls([
    { key:"nav_wst", to:A.wstETH, data:S.stEthPerToken },
    { key:"nav_cb",  to:A.cbETH,  data:S.exchangeRate },
    { key:"coinsA",  to:A.curveStEth,   data:S.coins + eU(0) },
    { key:"coinsB",  to:A.curveStEthNg, data:S.coins + eU(0) }
  ]);

  const navWst = r1.nav_wst ? Number(big(r1.nav_wst)) / 1e18 : null;
  const navCb  = r1.nav_cb  ? Number(big(r1.nav_cb))  / 1e18 : null;
  const order = (hex) => {
    if (!hex) return { verified:false, eth:0, st:1 };
    const a = "0x" + hex.replace(/^0x/,"").slice(24);
    const isEth = a.toLowerCase() === A.ethPlaceholder.toLowerCase();
    return { verified:true, eth: isEth ? 0 : 1, st: isEth ? 1 : 0 };
  };
  const oA = order(r1.coinsA), oB = order(r1.coinsB);

  const calls = [];
  for (const s of SZ) {
    const dx = eU(BigInt(s) * E18);
    calls.push({ key:"cA"+s, to:A.curveStEth,   data:S.get_dy + eU(oA.st) + eU(oA.eth) + dx });
    calls.push({ key:"cB"+s, to:A.curveStEthNg, data:S.get_dy + eU(oB.st) + eU(oB.eth) + dx });
    calls.push({ key:"uW"+s, to:A.uniQuoterV2,
      data:S.quoteExactInputSingle + eA(A.wstETH) + eA(A.WETH) + eU(BigInt(s)*E18) + eU(U.wstETH.fee) + eU(0) });
    calls.push({ key:"uC"+s, to:A.uniQuoterV2,
      data:S.quoteExactInputSingle + eA(A.cbETH) + eA(A.WETH) + eU(BigInt(s)*E18) + eU(U.cbETH.fee) + eU(0) });
  }
  calls.push({ key:"clS", to:A.chainlinkStEthEth, data:S.latestRoundData });
  calls.push({ key:"clC", to:A.chainlinkCbEthEth, data:S.latestRoundData });
  const r2 = await ethCalls(calls);

  const unit = (key, s) => {
    const h = r2[key + s];
    if (!h || h.length < 66) return null;
    const v = big(w(h, 0));
    return v == null ? null : Number(v) / Number(BigInt(s) * E18);
  };
  const feed = (key) => {
    const h = r2[key];
    if (!h || h.replace(/^0x/,"").length < 320) return null;
    const ans = int(w(h,1)), upd = Number(big(w(h,3)));
    if (ans <= 0n) return null;
    return { price: Number(ans)/1e18, updated: upd, age: now - upd,
             stale: (now - upd) > CFG.thresholds.feedHeartbeatSeconds };
  };

  const settle = async (fn) => { try { return { ok:true, v: await fn() }; }
                                catch (e) { return { ok:false, e: String(e.message || e) }; } };
  const [L, G, OK, CB] = await Promise.all([settle(llama), settle(gecko), settle(okx), settle(coinbase)]);

  return { now, navWst, navCb, oA, oB, unit, feed, L, G, OK, CB };
}

/* ============================ 组装 ============================ */
function buildTokens(d) {
  const SZ = CFG.sizes, U = CFG.uniswap;
  const src = (venue, meta, kind, price, extra) =>
    Object.assign({ venue, meta, kind, price }, extra || {});
  const httpSrc = (res, sym, venue, meta, kind, tx) => res.ok
    ? (res.v[sym] ? src(venue, meta(res.v[sym]), kind, tx ? tx(res.v[sym].price) : res.v[sym].price) : null)
    : src(venue, "不可用 · " + res.e, kind, null, { failed:true });

  const cA1 = d.unit("cA", 1), uW1 = d.unit("uW", 1), uC1 = d.unit("uC", 1);
  const fS = d.feed("clS"), fC = d.feed("clC");

  /* ---- stETH ---- */
  const st = [];
  if (cA1 != null) st.push(src("Curve stETH/ETH", d.oA.verified ? "get_dy · 卖出 1 枚" : "coin 顺序未验证", "onchain", cA1));
  const cB1 = d.unit("cB", 1);
  if (cB1 != null) st.push(src("Curve stETH-ng", d.oB.verified ? "get_dy · 卖出 1 枚" : "coin 顺序未验证", "onchain", cB1));
  if (fS) st.push(src("Chainlink stETH/ETH", "市价喂价 · " + ago(fS.age), "oracle", fS.price, { stale:fS.stale }));
  st.push(d.OK.ok ? src("OKX STETH-ETH", `盘口中间价 · 24h ${Math.round(d.OK.v.vol)} ETH`, "cex", d.OK.v.price)
                  : src("OKX STETH-ETH", "不可用 · " + d.OK.e, "cex", null, { failed:true }));
  const l1 = httpSrc(d.L, "stETH", "DefiLlama", v => `USD ÷ WETH · conf ${v.conf ?? "—"}`, "agg"); if (l1) st.push(l1);
  const g1 = httpSrc(d.G, "stETH", "CoinGecko", () => "vs_currency=eth", "agg"); if (g1) st.push(g1);

  /* ---- wstETH ---- */
  const ws = [];
  if (uW1 != null) ws.push(src(U.wstETH.label, "QuoterV2 · 卖出 1 枚", "onchain", uW1));
  if (cA1 != null && d.navWst) ws.push(src("Curve 换算", "stETH 执行价 × stEthPerToken", "onchain", cA1 * d.navWst));
  if (fS && d.navWst) ws.push(src("Chainlink 换算", "stETH/ETH 喂价 × stEthPerToken", "oracle", fS.price * d.navWst, { stale:fS.stale }));
  if (d.OK.ok && d.navWst) ws.push(src("OKX 换算", "STETH-ETH 中间价 × stEthPerToken", "cex", d.OK.v.price * d.navWst));
  const l2 = httpSrc(d.L, "wstETH", "DefiLlama", v => `USD ÷ WETH · conf ${v.conf ?? "—"}`, "agg"); if (l2) ws.push(l2);
  const g2 = httpSrc(d.G, "wstETH", "CoinGecko", () => "vs_currency=eth", "agg"); if (g2) ws.push(g2);

  /* ---- cbETH ---- */
  const cb = [];
  if (uC1 != null) cb.push(src(U.cbETH.label, "QuoterV2 · 卖出 1 枚", "onchain", uC1));
  if (fC) cb.push(src("Chainlink cbETH/ETH", "市价喂价 · " + ago(fC.age), "oracle", fC.price, { stale:fC.stale }));
  cb.push(d.CB.ok ? src("Coinbase CBETH-ETH", `盘口中间价 · 24h ${nf(d.CB.v.vol,2)}`, "cex", d.CB.v.price)
                  : src("Coinbase CBETH-ETH", "不可用 · " + d.CB.e, "cex", null, { failed:true }));
  const l3 = httpSrc(d.L, "cbETH", "DefiLlama", v => `USD ÷ WETH · conf ${v.conf ?? "—"}`, "agg"); if (l3) cb.push(l3);
  const g3 = httpSrc(d.G, "cbETH", "CoinGecko", () => "vs_currency=eth", "agg"); if (g3) cb.push(g3);

  const ser = (key, label, color) => ({ label, color,
    pts: SZ.map(s => ({ size:s, price: d.unit(key, s) })).filter(p => p.price != null) });
  const nonEmpty = (a) => a.filter(x => x.pts.length);

  return [
    { sym:"stETH", unit:"stETH", anchorKind:"协议赎回 1:1", nav:1,
      navNote:"Lido 提现队列 · maxShareRate 封顶", srcs:st,
      depth: nonEmpty([ ser("cA","Curve stETH/ETH","var(--cat-1)"), ser("cB","Curve stETH-ng","var(--cat-2)") ]) },
    { sym:"wstETH", unit:"wstETH", anchorKind:"链上兑换率", nav:d.navWst,
      navNote:"stEthPerToken() · 实时", srcs:ws,
      depth: nonEmpty([ ser("uW", U.wstETH.label, "var(--cat-1)") ]) },
    { sym:"cbETH", unit:"cbETH", anchorKind:"预言机兑换率", nav:d.navCb,
      navNote:"exchangeRate() · 每 24h 更新", srcs:cb,
      depth: nonEmpty([ ser("uC", U.cbETH.label, "var(--cat-1)") ]) }
  ];
}

function analyse(tok) {
  const th = CFG.thresholds;
  const agg = E.aggregate(tok.srcs, tok.nav, { absFloor: th.outlierAbsFloor, minKeep: 2 });
  Object.assign(tok, agg);
  tok.exit = E.exitProfile(tok.depth, tok.nav, CFG.sizes, { cleanBps: th.cleanExitBps });
  Object.assign(tok, E.classify(agg, tok.exit, th));
  return tok;
}

/* ============================ 图形 ============================ */
const NS = "http://www.w3.org/2000/svg";
const el = (n, a = {}, t) => { const e = document.createElementNS(NS, n);
  for (const k in a) e.setAttribute(k, a[k]); if (t != null) e.textContent = t; return e; };
const cvar = (s) => s === "ok" ? "ok" : s === "watch" ? "watch" : s === "alert" ? "alert" : s === "crisis" ? "crisis" : "ink-3";

/* 退出折价曲线。Y 轴按可读范围夹逼，越界点画成底边的三角并标数值。 */
function depthChart(tok) {
  const W = 520, H = 176, M = { t:16, r:18, b:34, l:50 }, CLAMP = 400;
  const svg = el("svg", { viewBox:`0 0 ${W} ${H}`, width:"100%", height:"auto",
    role:"img", "aria-label":`${tok.sym} 退出折价曲线` });
  svg.style.display = "block"; svg.style.maxWidth = "100%";

  const pts = [];
  for (const s of tok.depth) for (const p of s.pts) {
    const b = E.pegBps(p.price, tok.nav); if (b != null) pts.push(b);
  }
  if (!pts.length) { svg.appendChild(el("text", { x:W/2, y:H/2, "text-anchor":"middle",
    fill:"var(--ink-3)", "font-size":"12", "font-family":"inherit" }, "链上报价不可用")); return svg; }

  const inRange = pts.filter(b => b >= -CLAMP);
  let lo = Math.min(...(inRange.length ? inRange : [-CLAMP]), 0);
  let hi = Math.max(...pts.filter(b => b <= CLAMP), 0);
  const p = Math.max((hi - lo) * 0.2, 4); lo -= p; hi += p;

  const SZ = CFG.sizes;
  const x = (i) => M.l + i * (W - M.l - M.r) / (SZ.length - 1);
  const y = (v) => M.t + (hi - v) * (H - M.t - M.b) / (hi - lo);
  const yc = (v) => Math.min(Math.max(y(v), M.t), H - M.b);

  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, yy = y(v);
    svg.appendChild(el("line", { x1:M.l, x2:W-M.r, y1:yy, y2:yy, stroke:"var(--line-soft)", "stroke-width":1 }));
    svg.appendChild(el("text", { x:M.l-8, y:yy+3.5, "text-anchor":"end", fill:"var(--ink-3)",
      "font-size":"10", "font-family":"IBM Plex Mono, monospace" }, v.toFixed(0)));
  }
  if (lo < 0 && hi > 0) {
    svg.appendChild(el("line", { x1:M.l, x2:W-M.r, y1:y(0), y2:y(0), stroke:"var(--ink-3)",
      "stroke-width":1.5, "stroke-dasharray":"3 3", opacity:".55" }));
    svg.appendChild(el("text", { x:W-M.r, y:y(0)-5, "text-anchor":"end", fill:"var(--ink-3)",
      "font-size":"9.5", "font-family":"inherit" }, "锚定"));
  }
  SZ.forEach((s, i) => svg.appendChild(el("text", { x:x(i), y:H-13, "text-anchor":"middle",
    fill:"var(--ink-3)", "font-size":"10", "font-family":"IBM Plex Mono, monospace" }, s.toLocaleString("en-US"))));
  svg.appendChild(el("text", { x:(M.l+W-M.r)/2, y:H-2, "text-anchor":"middle", fill:"var(--ink-3)",
    "font-size":"9.5", "font-family":"inherit", "letter-spacing":".05em" }, `单池卖出数量（${tok.unit}）`));
  svg.appendChild(el("text", { x:M.l-8, y:M.t-5, "text-anchor":"end", fill:"var(--ink-3)",
    "font-size":"9.5", "font-family":"inherit" }, "bps"));

  tok.depth.forEach((s, si) => {
    const path = [];
    s.pts.forEach((pt) => {
      const i = SZ.indexOf(pt.size); if (i < 0) return;
      const b = E.pegBps(pt.price, tok.nav); if (b == null) return;
      path.push({ x:x(i), y:yc(b), b, out: b < lo || b > hi });
    });
    for (let i = 1; i < path.length; i++) {
      const a = path[i-1], c = path[i];
      svg.appendChild(el("line", { x1:a.x, y1:a.y, x2:c.x, y2:c.y, stroke:s.color, "stroke-width":2,
        "stroke-linecap":"round", "stroke-dasharray": c.out ? "4 3" : "none", opacity: c.out ? ".65" : "1" }));
    }
    path.forEach((pt) => {
      if (pt.out) {
        svg.appendChild(el("path", { d:`M ${pt.x-5} ${pt.y-7} L ${pt.x+5} ${pt.y-7} L ${pt.x} ${pt.y} Z`,
          fill:"var(--alert)", stroke:"var(--panel)", "stroke-width":1.5 }));
        svg.appendChild(el("text", { x:pt.x, y:pt.y-11, "text-anchor":"middle", fill:"var(--alert)",
          "font-size":"9.5", "font-weight":"600", "font-family":"IBM Plex Mono, monospace" },
          Math.abs(pt.b) >= 1000 ? (pt.b/100).toFixed(0)+"%" : bps(pt.b,0)));
      } else {
        svg.appendChild(el("circle", { cx:pt.x, cy:pt.y, r:3.6, fill:s.color,
          stroke:"var(--panel)", "stroke-width":2 }));
      }
    });
    const last = path.filter(q => !q.out).pop();
    if (last) svg.appendChild(el("text", { x:last.x-7, y:Math.max(last.y - 9 - si * 13, M.t + 9),
      "text-anchor":"end", fill: tok.depth.length > 1 ? s.color : "var(--ink-2)",
      "font-size":"10.5", "font-weight":"600", "font-family":"IBM Plex Mono, monospace" }, bps(last.b, 0)));
  });
  return svg;
}

/* 各来源分布点条 */
function dotStrip(tok) {
  const W = 520, H = 58, M = { l:12, r:12 };
  const svg = el("svg", { viewBox:`0 0 ${W} ${H}`, width:"100%", height:"auto",
    role:"img", "aria-label":`${tok.sym} 各来源脱锚分布` });
  svg.style.display = "block";
  const ps = tok.sources.filter(s => s.bps != null);
  if (ps.length < 2) { svg.appendChild(el("text", { x:W/2, y:34, "text-anchor":"middle",
    fill:"var(--ink-3)", "font-size":"12", "font-family":"inherit" }, "可用来源不足，无法交叉确认")); return svg; }
  const vs = ps.map(p => p.bps);
  let lo = Math.min(...vs, 0), hi = Math.max(...vs, 0);
  const pad2 = Math.max(hi - lo, 8) * 0.22; lo -= pad2; hi += pad2;
  const x = (v) => M.l + (v - lo) * (W - M.l - M.r) / (hi - lo);
  const ax = 36;
  svg.appendChild(el("line", { x1:M.l, x2:W-M.r, y1:ax, y2:ax, stroke:"var(--line)", "stroke-width":1.5 }));
  if (lo < 0 && hi > 0) {
    svg.appendChild(el("line", { x1:x(0), x2:x(0), y1:ax-13, y2:ax+8, stroke:"var(--ink-3)",
      "stroke-width":1.5, "stroke-dasharray":"3 3" }));
    svg.appendChild(el("text", { x:x(0), y:ax+21, "text-anchor":"middle", fill:"var(--ink-3)",
      "font-size":"9.5", "font-family":"inherit" }, "0"));
  }
  if (tok.consensus != null) {
    const cx = x(tok.consensus);
    svg.appendChild(el("line", { x1:cx, x2:cx, y1:ax-22, y2:ax+6,
      stroke:`var(--${cvar(tok.status)})`, "stroke-width":2.5 }));
    svg.appendChild(el("text", { x:Math.min(Math.max(cx, 24), W-24), y:ax-27, "text-anchor":"middle",
      fill:`var(--${cvar(tok.status)})`, "font-size":"10.5", "font-weight":"600",
      "font-family":"IBM Plex Mono, monospace" }, bps(tok.consensus)));
  }
  for (const p of ps) {
    const c = el("circle", { cx:x(p.bps), cy:ax, r:5,
      fill: p.outlier ? "var(--alert)" : p.stale ? "var(--watch)" : "var(--cat-1)",
      stroke:"var(--panel)", "stroke-width":2 });
    c.appendChild(el("title", {}, `${p.venue}: ${bps(p.bps)} bps`));
    svg.appendChild(c);
  }
  return svg;
}

/* 历史迷你走势 */
function spark(series) {
  const W = 520, H = 60, M = { t:8, r:8, b:8, l:8 };
  const svg = el("svg", { viewBox:`0 0 ${W} ${H}`, width:"100%", height:"auto", role:"img",
    "aria-label":"共识脱锚历史" });
  svg.style.display = "block";
  if (!series || series.length < 4) return null;
  const vs = series.map(p => p.v);
  let lo = Math.min(...vs, 0), hi = Math.max(...vs, 0);
  const p2 = Math.max((hi - lo) * 0.2, 2); lo -= p2; hi += p2;
  const x = (i) => M.l + i * (W - M.l - M.r) / (series.length - 1);
  const y = (v) => M.t + (hi - v) * (H - M.t - M.b) / (hi - lo);
  if (lo < 0 && hi > 0) svg.appendChild(el("line", { x1:M.l, x2:W-M.r, y1:y(0), y2:y(0),
    stroke:"var(--line)", "stroke-width":1, "stroke-dasharray":"3 3" }));
  svg.appendChild(el("path", { d:"M" + series.map((p,i) => `${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join(" L "),
    fill:"none", stroke:"var(--cat-1)", "stroke-width":2, "stroke-linejoin":"round" }));
  const lastI = series.length - 1;
  svg.appendChild(el("circle", { cx:x(lastI), cy:y(series[lastI].v), r:4, fill:"var(--cat-1)",
    stroke:"var(--panel)", "stroke-width":2 }));
  return svg;
}

/* ============================ 渲染 ============================ */
const KIND = { onchain:["链上池","onchain"], oracle:["预言机","oracle"], cex:["交易所","cex"], agg:["聚合器",""] };

function rows(tok) {
  return [...tok.sources].sort((a,b) => {
    if (a.price == null && b.price != null) return 1;
    if (b.price == null && a.price != null) return -1;
    return (a.bps ?? 0) - (b.bps ?? 0);
  }).map(s => {
    const [kt, kc] = KIND[s.kind] || [s.kind, ""];
    let j = `<span class="flag-fail">—</span>`;
    if (s.failed) j = `<span class="flag-fail">不可用</span>`;
    else if (s.stale) j = `<span class="flag-stale">过期</span>`;
    else if (s.outlier) j = `<span class="flag-out">离群</span>`;
    else if (s.bps != null) j = `<span class="ok-tick">✓</span>`;
    const hot = s.bps != null && Math.abs(s.bps) > CFG.thresholds.watch;
    return `<tr>
      <td><span class="venue">${esc(s.venue)}</span><span class="vmeta">${esc(s.meta)}</span></td>
      <td><span class="tag ${kc}">${kt}</span></td>
      <td class="r num">${s.price == null ? "—" : nf(s.price, 6)}</td>
      <td class="r num"${hot ? ' style="color:var(--alert);font-weight:600"' : ""}>${bps(s.bps)}</td>
      <td class="r">${j}</td></tr>`;
  }).join("");
}

function render(tokens, hist) {
  const box = $("cards"); box.textContent = "";
  for (const t of tokens) {
    const clean = t.exit.cleanSize;
    const cleanTxt = clean == null ? "＜1" : (clean === CFG.sizes[CFG.sizes.length-1] ? clean.toLocaleString("en-US")+"+" : clean.toLocaleString("en-US"));
    const card = document.createElement("section");
    card.className = "card";
    card.innerHTML = `
      <div class="card-head">
        <span class="tick">${esc(t.sym)}</span>
        <span class="anchor-kind">${esc(t.anchorKind)}</span>
        <span class="pill p-${t.status}">${esc(t.text)}</span>
      </div>
      <div class="headline">
        <span class="big num v-${t.status}">${bps(t.consensus)}</span>
        <span class="big-unit">bps · 共识脱锚</span>
        <span class="hnote">${t.confirms} 源交叉确认<br><span class="why">${esc(t.why)}</span></span>
      </div>
      <dl class="kpis">
        <div class="kpi"><dt>兑付锚 NAV</dt><dd class="num">${nf(t.nav,6)}<span class="sm">${esc(t.navNote)}</span></dd></div>
        <div class="kpi"><dt>共识市价</dt><dd class="num">${nf(t.medPrice,6)}<span class="sm">ETH · 各源中位数</span></dd></div>
        <div class="kpi"><dt>源间分歧</dt><dd class="num">${t.dispersion == null ? "—" : t.dispersion.toFixed(1)}<span class="sm">bps · ${t.dispersion != null && t.dispersion > CFG.thresholds.disp ? "偏大，留意" : "各源一致"}</span></dd></div>
        <div class="kpi"><dt>可退出规模</dt><dd class="num">${cleanTxt}<span class="sm">${t.unit} · 单池内 ${CFG.thresholds.cleanExitBps} bps 以内</span></dd></div>
      </dl>
      <div class="sect"><h3>来源明细 <span class="cnt">按偏离排序</span></h3>
        <div class="tw"><table class="src">
          <thead><tr><th>来源</th><th>类型</th><th class="r">价格 (ETH)</th><th class="r">偏离 (bps)</th><th class="r">采信</th></tr></thead>
          <tbody>${rows(t)}</tbody></table></div></div>
      <div class="sect"><h3>各来源分布</h3><div data-strip></div>
        <div class="legend">
          <span><i style="background:var(--cat-1)"></i>参与共识</span>
          <span><i style="background:var(--alert)"></i>离群已剔除</span>
          <span><i style="background:var(--watch)"></i>预言机过期</span>
          <span><i class="ln" style="background:var(--${cvar(t.status)})"></i>共识中位数</span>
        </div></div>
      <div class="sect"><h3>退出折价曲线 <span class="cnt">单池报价，非跨场所路由</span></h3>
        <div data-depth></div>
        ${t.depth.length > 1 ? `<div class="legend">${t.depth.map(s =>
          `<span><i class="ln" style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>` : ""}
      </div>
      <div class="sect" data-hist hidden><h3>共识脱锚历史 <span class="cnt">最近快照</span></h3><div data-spark></div></div>`;
    box.appendChild(card);
    card.querySelector("[data-strip]").appendChild(dotStrip(t));
    card.querySelector("[data-depth]").appendChild(depthChart(t));
    const h = hist && hist[t.sym];
    if (h && h.length >= 4) {
      const sv = spark(h);
      if (sv) { card.querySelector("[data-spark]").appendChild(sv); card.querySelector("[data-hist]").hidden = false; }
    }
  }
}

function renderVerdict(tokens) {
  const worst = tokens.reduce((a, b) => (E.LEVELS[b.status] > E.LEVELS[a.status] ? b : a), tokens[0]);
  const v = $("verdict"); v.className = "verdict s-" + worst.status;
  const titles = { ok:"三个都在锚上", watch:`${worst.sym} 值得留意`,
    alert:`${worst.sym} 已进入警戒`, crisis:`${worst.sym} 脱锚`, dead:"数据不足，无法下判断" };
  $("vtitle").textContent = titles[worst.status];
  const parts = tokens.map(t => `${t.sym} ${bps(t.consensus)}`).join(" · ");
  const total = tokens.reduce((n, t) => n + t.confirms, 0);
  const nOk = tokens.filter(t => t.status === "ok").length;
  $("vnote").textContent = worst.status === "dead"
    ? "公开节点或某些 API 没取到，下表标了具体是哪一个。"
    : `${parts}（bps）。本轮 ${total} 个来源参与交叉确认，${nOk}/3 在 ±${CFG.thresholds.ok} bps 以内。`;
}

/* ============================ 历史 ============================ */
const LS_KEY = "lst-peg-monitor:local-history";
function pushLocal(tokens) {
  try {
    const raw = localStorage.getItem(LS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    arr.push({ t: Date.now(), v: Object.fromEntries(tokens.map(k => [k.sym, k.consensus])) });
    localStorage.setItem(LS_KEY, JSON.stringify(arr.slice(-400)));
  } catch (_) {}
}
function readLocal() {
  try {
    const raw = localStorage.getItem(LS_KEY); if (!raw) return null;
    const arr = JSON.parse(raw); const out = {};
    for (const sym of ["stETH","wstETH","cbETH"])
      out[sym] = arr.filter(r => r.v && r.v[sym] != null).map(r => ({ t:r.t, v:r.v[sym] }));
    return out;
  } catch { return null; }
}
async function readRepoHistory() {
  try {
    const d = await getJSON("data/recent.json", 7000);
    if (!d || !Array.isArray(d.records)) return null;
    const out = {};
    for (const sym of ["stETH","wstETH","cbETH"])
      out[sym] = d.records.filter(r => r[sym] && r[sym].consensus != null)
        .map(r => ({ t: r.ts * 1000, v: r[sym].consensus }));
    return out;
  } catch { return null; }
}
function mergeHist(a, b) {
  if (!a) return b; if (!b) return a;
  const out = {};
  for (const k of Object.keys(Object.assign({}, a, b))) {
    const m = [...(a[k] || []), ...(b[k] || [])].sort((x, y) => x.t - y.t);
    out[k] = m.filter((p, i) => i === 0 || p.t !== m[i-1].t).slice(-400);
  }
  return out;
}

/* ============================ 主流程 ============================ */
let timer = null, busy = false, repoHist = null;
async function run() {
  if (busy) return; busy = true;
  $("spin").hidden = false; $("refresh").disabled = true; $("rlabel").textContent = "读取中";
  try {
    const d = await fetchAll();
    const tokens = buildTokens(d).map(analyse);
    pushLocal(tokens);
    if (repoHist === null) repoHist = (await readRepoHistory()) || false;
    render(tokens, mergeHist(repoHist || null, readLocal()));
    renderVerdict(tokens);
    $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12:false })
      + (rpcUrl ? " · " + new URL(rpcUrl).hostname.replace(/^www\./, "") : "");
  } catch (e) {
    $("verdict").className = "verdict s-crisis";
    $("vtitle").textContent = "取数失败";
    $("vnote").textContent = String(e.message || e) + "。点右上角刷新重试。";
    $("clock").textContent = "失败";
  } finally {
    busy = false; $("spin").hidden = true; $("refresh").disabled = false; $("rlabel").textContent = "刷新";
  }
}

async function boot() {
  try { CFG = await getJSON("config.json", 8000); }
  catch (e) {
    $("vtitle").textContent = "配置文件读不到";
    $("vnote").textContent = "config.json 没加载成功：" + String(e.message || e);
    return;
  }
  $("addrs").innerHTML = [
    ["wstETH", CFG.addresses.wstETH, "stEthPerToken()"],
    ["cbETH", CFG.addresses.cbETH, "exchangeRate()"],
    ["Curve stETH/ETH", CFG.addresses.curveStEth, "get_dy(int128,int128,uint256)"],
    ["Curve stETH-ng", CFG.addresses.curveStEthNg, "get_dy(int128,int128,uint256)"],
    ["Chainlink stETH/ETH", CFG.addresses.chainlinkStEthEth, "市价喂价"],
    ["Chainlink cbETH/ETH", CFG.addresses.chainlinkCbEthEth, "市价喂价"],
    ["Uniswap v3 QuoterV2", CFG.addresses.uniQuoterV2, "quoteExactInputSingle"]
  ].map(([n,a,f]) => `<li>${esc(n)} — <code>${esc(a)}</code> · ${esc(f)}</li>`).join("");
  $("thr-list").innerHTML = [
    ["正常", `|共识脱锚| ≤ ${CFG.thresholds.ok} bps`],
    ["关注", `${CFG.thresholds.ok} – ${CFG.thresholds.watch} bps，或源间分歧 &gt; ${CFG.thresholds.disp} bps`],
    ["警戒", `${CFG.thresholds.watch} – ${CFG.thresholds.alert} bps，或卖出 ${CFG.thresholds.thinSize} 枚差于 −${CFG.thresholds.thinBps} bps`],
    ["危机", `&gt; ${CFG.thresholds.alert} bps`]
  ].map(([a,b]) => `<b>${a}</b><span>${b}</span>`).join("");

  $("refresh").addEventListener("click", run);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearInterval(timer); timer = null; }
    else if (!timer) { run(); timer = setInterval(run, (CFG.refreshSeconds || 90) * 1000); }
  });
  await run();
  timer = setInterval(run, (CFG.refreshSeconds || 90) * 1000);
}
boot();
})();
