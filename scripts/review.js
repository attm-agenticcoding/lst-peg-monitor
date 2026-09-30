#!/usr/bin/env node
/* 预警规则每日体检（只读，不改任何文件）。
 *   node scripts/review.js            最近 24 小时的体检报告（中文）
 *   node scripts/review.js --json     同样的内容，JSON
 *
 * 1. 回放：用逐条存档（data/archive）重算每个场所、每个指标的基线（中位数、1.4826×MAD，排除最近 1 小时），
 *    按规则重放出「预警段」。基线每 10 分钟重算一次（和线上逐条重算只差几分钟的窗口边界）。
 * 2. 事后标注：「最优出口」（各场所里最好的那个价）比它自己 24h 中位数差 ≥ D bps、且连续 3 次采样 = 一个事件。
 *    预警开始后 6 小时内出现 D bps 事件 = 真预警，否则 = 误报；事件开始前 6 小时到开始后 10 分钟内没有预警 = 漏报。
 *    D 分 3 / 5 / 10 bps 三档一起报：「多大的恶化才算事」是你的决定，这里不替你定。
 * 3. 合成测试：真实的大事件很少，漏报率靠注入测：往真实背景里注入四种形态，量检出率和延迟 ——
 *    全市场 30 分钟缓跌 10 bps、全市场断崖 25 bps、只有大额变差 15 bps、单个场所故障 50 bps（这个不该报）。
 * 4. 候选规则：一小组参数在全部存档上算同样的账；只有严格不差于现行规则、且存档 ≥ 7 天才给「建议改」。 */
"use strict";
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const Core = require("../core.js");

const ROOT = path.join(__dirname, "..");
const CFG = JSON.parse(fs.readFileSync(path.join(ROOT, "config.json"), "utf8"));
const G = CFG.guard, H = 3600, EXCL = G.excludeMinutes * 60, GRID = 600;
const asJson = process.argv.includes("--json");
const hoursArg = process.argv.indexOf("--hours");
const EVAL_H = hoursArg > 0 ? +process.argv[hoursArg + 1] : 24;
const DS = [3, 5, 10], LOOKAHEAD = 6 * H, MIN_DAYS_TO_CHANGE = 7;
const f = (x, d = 1) => Core.fmt(x, d);
const et = (t) => new Date(t * 1000).toLocaleString("zh-CN", { timeZone: "America/New_York", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const METRICS = [["peg", "v", "卖 1 枚"], ["big", "vb", "卖大额"]];

/* ---------- 读数据 ---------- */
function loadRecords() {
  let recs = [];
  const dir = path.join(ROOT, "data", "archive");
  if (fs.existsSync(dir)) {
    for (const name of fs.readdirSync(dir).sort()) {
      const p = path.join(dir, name);
      let txt = null;
      if (name.endsWith(".jsonl")) txt = fs.readFileSync(p, "utf8");
      else if (name.endsWith(".jsonl.gz")) txt = zlib.gunzipSync(fs.readFileSync(p)).toString("utf8");
      if (!txt) continue;
      for (const line of txt.split("\n")) if (line.trim()) { try { recs.push(JSON.parse(line)); } catch { /* 半行，跳过 */ } }
    }
  }
  // 存档刚开始时，用 history.json 补（它保留最近 1 天的逐条采样，更早的是稀疏的）
  try { recs = recs.concat(JSON.parse(fs.readFileSync(path.join(ROOT, "data", "history.json"), "utf8")).records || []); } catch { /* 没有也行 */ }
  const seen = new Set();
  return recs.filter((r) => r && r.ts && !seen.has(r.ts) && seen.add(r.ts)).sort((a, b) => a.ts - b.ts);
}

/* ---------- 基线 ---------- */
const lb = (pts, t) => { let a = 0, b = pts.length; while (a < b) { const m = (a + b) >> 1; if (pts[m][0] < t) a = m + 1; else b = m; } return a; };
function medMad(vals) {
  const a = vals.slice().sort((p, q) => p - q), n = a.length, mid = (arr) => (n % 2 ? arr[(n - 1) / 2] : (arr[n / 2 - 1] + arr[n / 2]) / 2);
  const c = mid(a);
  return [c, 1.4826 * mid(a.map((v) => Math.abs(v - c)).sort((p, q) => p - q))];
}
/** 每个网格时点、每个窗口的 [中心, 尺度]；数据量不够的窗口为 null */
function baseSeries(T, X, grid, windows) {
  const full = [], hourly = [];
  let lastH = null;
  for (let i = 0; i < T.length; i++) {
    if (X[i] == null) continue;
    full.push([T[i], X[i]]);
    const h = Math.floor(T[i] / H);
    if (h !== lastH) { hourly.push([T[i], X[i]]); lastH = h; }
  }
  return grid.map((t) => windows.map((w) => {
    const pts = w.hourly ? hourly : full, lo = lb(pts, t - w.hours * H), hi = lb(pts, t - EXCL), n = hi - lo;
    if (n < (w.hourly ? Math.min(G.minPoints, w.minHours) : G.minPoints)) return null;
    if (pts[hi - 1][0] - pts[lo][0] < w.minHours * H) return null;
    return medMad(pts.slice(lo, hi).map((p) => p[1]));
  }));
}

function build(R) {
  const T = R.map((r) => r.ts), t0 = Math.floor(T[0] / GRID) * GRID, grid = [];
  for (let t = t0; t <= T[T.length - 1] + GRID; t += GRID) grid.push(t);
  const gi = T.map((t) => Math.floor((t - t0) / GRID));
  const S = {};
  for (const sym of Core.ASSETS) {
    S[sym] = {};
    for (const [m, key] of METRICS) {
      const ids = Core.VENUES[sym].map((v) => v.id), X = {}, B = {};
      for (const id of ids) {
        X[id] = R.map((r) => { const o = r[sym] && r[sym][key]; const x = o ? o[id] : null; return typeof x === "number" && isFinite(x) ? x : null; });
        B[id] = baseSeries(T, X[id], grid, G.windows);
      }
      // 最优出口：同一时刻各场所里最好的价
      const E = T.map((_, i) => { let b = null; for (const id of ids) if (X[id][i] != null && (b == null || X[id][i] > b)) b = X[id][i]; return b; });
      S[sym][m] = { ids, X, B, E, EB: baseSeries(T, E, grid, [G.windows[0]]).map((w) => w[0]), T, gi };
    }
  }
  return S;
}

/* ---------- 规则重放 ---------- */
function hitsAt(s, i, V, shock) {
  let n = 0;
  for (const id of s.ids) {
    let x = s.X[id][i];
    if (x == null) continue;
    if (shock) x += shock(id);
    const bw = s.B[id][s.gi[i]];
    if (!bw) continue;
    let best = null;
    for (const b of bw) if (b) { const L = b[0] - Math.max(V.k * b[1], V.floor); if (!best || L > best[0]) best = [L, b[0]]; }
    if (!best || best[1] < -V.thin) continue; // 平时就很薄的场所不算出口
    if (x < best[0]) n++;
  }
  return n >= V.minVenues;
}
function episodes(s, V, from) {
  const eps = [];
  let cur = null, clear = 0;
  for (let i = 0; i < s.T.length; i++) {
    if (s.T[i] < from) continue;
    const h = hitsAt(s, i, V);
    if (!cur && h) { cur = { start: s.T[i], end: s.T[i] }; eps.push(cur); clear = 0; }
    else if (cur && h) { cur.end = s.T[i]; clear = 0; }
    else if (cur && !h && ++clear >= V.clear) cur = null;
  }
  return eps;
}
function events(s, D, from) {
  const ev = [];
  let cur = null, run = [], rec = 0;
  for (let i = 0; i < s.T.length; i++) {
    if (s.T[i] < from) continue;
    const e = s.E[i], b = s.EB[s.gi[i]];
    if (e == null || !b) continue;
    const d = e - b[0];
    if (!cur) {
      if (d <= -D) { run.push(i); if (run.length >= 3) { cur = { start: s.T[run[0]], end: s.T[i], depth: d }; ev.push(cur); rec = 0; } }
      else run = [];
    } else {
      cur.depth = Math.min(cur.depth, d); cur.end = s.T[i];
      if (d > -D / 2) { if (++rec >= 3) { cur = null; run = []; } } else rec = 0;
    }
  }
  return ev;
}

/* ---------- 合成测试 ---------- */
const SHOCKS = [
  { id: "ramp", name: "全市场 30 分钟缓跌 10 bps", metrics: ["peg", "big"], dur: 3600, fn: (dt) => -10 * Math.min(1, dt / 1800), all: true },
  { id: "step", name: "全市场断崖 25 bps", metrics: ["peg", "big"], dur: 1800, fn: () => -25, all: true },
  { id: "liq", name: "只有大额变差 15 bps", metrics: ["big"], dur: 1800, fn: () => -15, all: true },
  { id: "glitch", name: "单个场所故障 50 bps（不该报）", metrics: ["peg", "big"], dur: 1200, fn: () => -50, all: false },
];
function synthetic(S, V, from, liveEps) {
  const out = {};
  for (const sh of SHOCKS) {
    const lat = [];
    let tries = 0;
    for (const sym of Core.ASSETS) for (const m of sh.metrics) {
      const s = S[sym][m];
      for (let i0 = 0; i0 < s.T.length; i0++) {
        const t0 = s.T[i0];
        if (t0 < from || (t0 - from) % (2 * H) >= 120) continue; // 每 2 小时注入一次
        if (s.T[s.T.length - 1] - t0 < sh.dur) continue;
        if ((liveEps[sym][m] || []).some((e) => t0 >= e.start - 1800 && t0 <= e.end + 1800)) continue; // 真预警期间不注入
        // 单场所故障打在当时最好的那个场所上（最坏情况）
        let target = null;
        for (const id of s.ids) if (s.X[id][i0] != null && (target == null || s.X[id][i0] > s.X[target][i0])) target = id;
        tries++;
        let hitAt = null;
        for (let i = i0; i < s.T.length && s.T[i] - t0 <= sh.dur; i++) {
          const dt = s.T[i] - t0, shock = (id) => (sh.all || id === target ? sh.fn(dt) : 0);
          if (hitsAt(s, i, V, shock)) { hitAt = dt; break; }
        }
        if (hitAt != null) lat.push(hitAt / 60);
      }
    }
    lat.sort((a, b) => a - b);
    out[sh.id] = { name: sh.name, tries, hits: lat.length, median: lat.length ? lat[lat.length >> 1] : null };
  }
  return out;
}

/* ---------- 一条规则的完整账 ---------- */
function score(S, V, from, days) {
  const eps = {}, rows = [];
  let nEps = 0, fp = { 3: 0, 5: 0, 10: 0 }, miss = { 3: 0, 5: 0, 10: 0 }, nEv = { 3: 0, 5: 0, 10: 0 };
  for (const sym of Core.ASSETS) {
    eps[sym] = {};
    for (const [m, , label] of METRICS) {
      const s = S[sym][m], E = episodes(s, V, from);
      eps[sym][m] = E; nEps += E.length;
      const evs = Object.fromEntries(DS.map((D) => [D, events(s, D, from - LOOKAHEAD)]));
      for (const e of E) {
        const tag = {};
        for (const D of DS) {
          const hit = evs[D].find((v) => v.start <= e.start + LOOKAHEAD && v.end >= e.start - 1800);
          tag[D] = hit ? { lead: (hit.start - e.start) / 60, depth: hit.depth } : null;
          if (!hit) fp[D]++;
        }
        rows.push({ kind: "alert", sym, m, label, start: e.start, end: e.end, tag });
      }
      for (const D of DS) for (const v of evs[D].filter((x) => x.start >= from)) {
        nEv[D]++;
        const a = E.find((e) => e.start >= v.start - LOOKAHEAD && e.start <= v.start + 600);
        if (!a) miss[D]++;
        rows.push({ kind: "event", sym, m, label, D, start: v.start, end: v.end, depth: v.depth, alertLead: a ? (v.start - a.start) / 60 : null });
      }
    }
  }
  return { V, eps, rows, perDay: nEps / days, nEps, fp, miss, nEv, syn: synthetic(S, V, from, eps) };
}

/* ---------- 主流程 ---------- */
const R = loadRecords();
if (R.length < 50) { console.log("数据太少，还不能复盘"); process.exit(0); }
const S = build(R);
const now = R[R.length - 1].ts, from = now - EVAL_H * H;
const archDays = (now - R[0].ts) / 86400;
const live = { name: "现行", k: G.k, floor: G.floorBps, minVenues: G.minVenues, thin: CFG.thresholds.ok, clear: G.clearSamples };
const today = score(S, live, from, EVAL_H / 24);

// 候选规则：在全部数据上算（有基线的那一段起）
const allFrom = R[0].ts + 7 * H, allDays = Math.max((now - allFrom) / 86400, 1 / 24);
const grid = [];
for (const k of [2.5, 3, 4]) for (const floor of [2, 3, 4, 5]) for (const minVenues of [2, 3]) for (const thin of [25, 50])
  grid.push({ name: `k=${k} 下限 ${floor} bps 确认 ${minVenues} 个场所 薄场所线 ${thin}`, k, floor, minVenues, thin, clear: G.clearSamples });
const liveAll = score(S, live, allFrom, allDays);
const cands = grid.map((V) => score(S, V, allFrom, allDays));
const worseOrEq = (c, L) => c.syn.glitch.hits <= L.syn.glitch.hits && c.miss[5] <= L.miss[5] && c.miss[10] <= L.miss[10]
  && ["ramp", "step", "liq"].every((k) => c.syn[k].hits >= L.syn[k].hits)
  && c.fp[5] <= L.fp[5] && (c.syn.ramp.median ?? 1e9) <= (L.syn.ramp.median ?? 1e9);
// 「更好」要有实质差别：少至少 1 次 5bps 误报、或缓跌检出快至少 2 分钟（一个采样间隔的抖动不算）、或多检出、或少漏报
const strictly = (c, L) => c.fp[5] <= L.fp[5] - 1 || (c.syn.ramp.median ?? 1e9) <= (L.syn.ramp.median ?? 1e9) - 2
  || ["ramp", "step", "liq"].some((k) => c.syn[k].hits > L.syn[k].hits) || c.miss[5] < L.miss[5];
const better = cands.filter((c) => worseOrEq(c, liveAll) && strictly(c, liveAll))
  .sort((a, b) => a.fp[5] - b.fp[5] || (a.syn.ramp.median ?? 1e9) - (b.syn.ramp.median ?? 1e9));
const enoughData = archDays >= MIN_DAYS_TO_CHANGE;

if (asJson) {
  console.log(JSON.stringify({ now, archDays, version: G.version, today: { rows: today.rows, fp: today.fp, miss: today.miss, nEv: today.nEv, syn: today.syn },
    all: { live: { fp: liveAll.fp, miss: liveAll.miss, perDay: liveAll.perDay, syn: liveAll.syn }, better: better.slice(0, 3).map((c) => ({ V: c.V, fp: c.fp, miss: c.miss, perDay: c.perDay, syn: c.syn })) }, enoughData }));
  process.exit(0);
}

/* ---------- 中文报告 ---------- */
const L = [];
L.push(`LST 预警体检 · 最近 ${EVAL_H} 小时（截至 ${et(now)} ET）· 规则版本 ${G.version} · 存档 ${archDays.toFixed(1)} 天`);
const alerts = today.rows.filter((r) => r.kind === "alert"), evs = today.rows.filter((r) => r.kind === "event");
L.push(`【预警】${alerts.length ? "" : "无"}`);
for (const a of alerts) {
  const t = DS.map((D) => (a.tag[D] ? `${D}bps 真（${a.tag[D].lead >= 0 ? `提前 ${a.tag[D].lead.toFixed(0)} 分钟` : `晚 ${(-a.tag[D].lead).toFixed(0)} 分钟`}）` : `${D}bps 误报`)).join("，");
  L.push(`  ${a.sym} ${a.label} ${et(a.start)}–${et(a.end)}：${t}`);
}
L.push(`【事后事件】（最优出口比 24h 中位数差 ≥D bps 且连续 3 次采样）${evs.length ? "" : "无"}`);
for (const e of evs.filter((x) => x.D >= 5)) L.push(`  ${e.sym} ${e.label} ≥${e.D}bps ${et(e.start)} 起，最深 ${f(e.depth)}：${e.alertLead == null ? "漏报" : e.alertLead >= 0 ? `预警提前 ${e.alertLead.toFixed(0)} 分钟` : `预警晚 ${(-e.alertLead).toFixed(0)} 分钟`}`);
const small = evs.filter((x) => x.D === 3).length;
if (small) L.push(`  另有 ${small} 次 3 bps 级别的小波动`);
L.push("【合成测试】（注入到最近这段真实数据里）");
for (const k of ["ramp", "step", "liq", "glitch"]) {
  const s = today.syn[k];
  L.push(`  ${s.name}：${k === "glitch" ? `误报 ${s.hits}/${s.tries}` : `检出 ${s.hits}/${s.tries}${s.median != null ? `，中位延迟 ${s.median.toFixed(0)} 分钟` : ""}`}`);
}
L.push(`【候选规则】全部数据 ${allDays.toFixed(1)} 天：现行规则 ${liveAll.perDay.toFixed(1)} 次预警/天，5bps 误报 ${liveAll.fp[5]}，5bps 漏报 ${liveAll.miss[5]}/${liveAll.nEv[5]}，缓跌中位延迟 ${liveAll.syn.ramp.median ?? "—"} 分钟`);
for (const c of better.slice(0, 3))
  L.push(`  ${c.V.name}：${c.perDay.toFixed(1)} 次/天，5bps 误报 ${c.fp[5]}，漏报 ${c.miss[5]}/${c.nEv[5]}，缓跌中位延迟 ${c.syn.ramp.median ?? "—"} 分钟，单场所故障误报 ${c.syn.glitch.hits}`);
if (!better.length) L.push("  没有哪个候选严格优于现行规则");
// 取舍参考：只看「少误报」最好的候选（不漏报、单场所故障不多报），代价通常是检出更慢 —— 这一步要你来定
const quiet = cands.filter((c) => c.miss[5] <= liveAll.miss[5] && c.syn.glitch.hits <= liveAll.syn.glitch.hits && c.fp[5] < liveAll.fp[5])
  .sort((a, b) => a.fp[5] - b.fp[5] || (a.syn.ramp.median ?? 1e9) - (b.syn.ramp.median ?? 1e9))[0];
if (quiet) L.push(`  取舍参考（更少误报、但可能更慢）：${quiet.V.name} → 5bps 误报 ${quiet.fp[5]}，缓跌中位延迟 ${quiet.syn.ramp.median != null ? quiet.syn.ramp.median.toFixed(0) : "—"} 分钟，缓跌检出 ${quiet.syn.ramp.hits}/${quiet.syn.ramp.tries}`);
L.push(`结论：${better.length && enoughData ? `建议改成「${better[0].V.name}」（严格不差于现行，且在 5bps 误报或延迟上更好）` : better.length ? `有更好的候选，但存档只有 ${archDays.toFixed(1)} 天（<${MIN_DAYS_TO_CHANGE} 天），先观察不改` : "维持现行规则"}`);
console.log(L.join("\n"));
