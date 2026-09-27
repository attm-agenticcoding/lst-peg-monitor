/* app.js — 页面层。每 refreshSeconds 秒用 core.js 直读一次（和后台快照同一套算法），
 * 另外每 5 分钟读一次 data/history.json：算常态、画 7 天走势、看后台快照是否新鲜。 */
(() => {
  "use strict";
  const C = window.PegCore;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = C.fmt;
  const ago = (s) => (!isFinite(s) ? "—" : s < 90 ? `${Math.max(0, Math.round(s))} 秒前`
    : s < 5400 ? `${Math.round(s / 60)} 分钟前` : `${(s / 3600).toFixed(1)} 小时前`);
  const hm = (ts) => new Date(ts * 1000).toLocaleString("zh-CN",
    { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const span = (h) => (h < 48 ? `${h} 小时` : `${Math.round(h / 24)} 天`);
  const KIND = { agg: "聚合", dex: "链上", cex: "交易所" };
  let CFG = null, hist = null, histAt = 0, busy = false, timer = null;

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
    const dev = b.ready && a.peg != null ? a.peg - b.median : null;
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
        <div class="hnote">主报价 · ${esc(a.primaryLabel || "—")} 卖 1 枚<br><span class="why">${esc(a.why)}</span></div></div>
      <dl class="kpis">
        ${kpi("常态", b.ready ? `${fmt(b.median)} bps` : "积累中",
          b.ready ? `近 ${span(b.hours)}中位 · 均值 ${fmt(b.mean)} · 常见 ${fmt(b.p10)}~${fmt(b.p90)}` : `已有 ${b.hours} 小时，满 ${CFG.baseline.minHours} 小时出数`)}
        ${kpi("偏离常态", dev == null ? "—" : `<span class="${dev >= 0 ? "dev-up" : "dev-down"}">${fmt(dev)}</span> bps`, "现在 − 常态；负 = 比平时更折价")}
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

  /* ---------- 走势小图 ---------- */
  function drawSpark(sym, a, now) {
    const el = $(`spark-${sym}`);
    if (!el) return;
    const pts = (hist ? hist.records : [])
      .filter((r) => r.ts >= now - 7 * 86400 && r.ts < now && r[sym] && typeof r[sym].peg === "number")
      .map((r) => [r.ts, r[sym].peg]);
    if (a.peg != null) pts.push([now, a.peg]);
    if (pts.length < 3) {
      el.innerHTML = `<p class="empty">后台快照积累中（每 ${CFG.snapshotMinutes} 分钟一条），攒够几条后这里出走势。</p>`;
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
      note += ` 后台快照 <span class="${age > 30 * 60 ? "stale" : ""}">${ago(age)}</span>（每 ${CFG.snapshotMinutes} 分钟一条，推送巡检用它）。`;
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

  /* ---------- 主循环 ---------- */
  async function run() {
    if (busy) return;
    busy = true;
    $("spin").hidden = false; $("refresh").disabled = true; $("rlabel").textContent = "读取中";
    try {
      const [snap] = await Promise.all([C.collect(CFG), loadHist()]);
      const res = C.evaluate(snap, CFG);
      $("cards").innerHTML = C.ASSETS.map((s) => card(s, res[s], snap)).join("");
      for (const s of C.ASSETS) drawSpark(s, res[s], snap.ts);
      verdict(res, snap);
      $("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (e) {
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
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { clearInterval(timer); timer = null; }
      else if (!timer) { run(); timer = setInterval(run, CFG.refreshSeconds * 1000); }
    });
    await run();
    timer = setInterval(run, CFG.refreshSeconds * 1000);
  }
  boot().catch((e) => { $("vtitle").textContent = "初始化失败"; $("vnote").textContent = String(e); });
})();
