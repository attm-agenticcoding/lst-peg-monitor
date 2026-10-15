/* Deterministic controller tests, including transports that ignore AbortSignal. */
"use strict";
const assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs");
const config = require("../config.json"), Redemption = require("../redemption.js");
const source = (name) => fs.readFileSync(require.resolve("../" + name), "utf8");
const clone = (x) => JSON.parse(JSON.stringify(x));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const response = (data) => ({ ok: true, json: async () => data });

function snapshot(ts, price = 1) {
  const px = { stETH: {}, cbETH: {} }, meta = { kyber: {} }, st = { kyber: {}, quoteMeta: meta, waits: {}, gasGwei: 1, gasAt: ts, apr: 3, aprAt: ts,
    queue: { at: ts, blockTimestamp: ts, blockNumber: 100, bunker: false, paused: false } };
  for (const sym of Object.keys(px)) for (const { id } of require("../core.js").VENUES[sym])
    px[sym][id] = Object.fromEntries(config.sizes.map((size) => [size, price]));
  for (const x of config.stRedeem.sizes) {
    st.kyber[x] = x * 1.001; meta.kyber[x] = { quotedAt: ts - 10, fetchedAt: ts };
    st.waits[x] = { status: "calculated", amountSteth: st.kyber[x], waitDays: 2, calculatedAt: ts - 5, fetchedAt: ts };
  }
  return { ts, nav: { stETH: 1, cbETH: 1, wstETH: 1.2 }, px, st,
    rt: { rate: 1, waitDays: 3, apy: 0.03, kyber: Object.fromEntries(config.roundTrip.sizes.map((x) => [x, x])),
      ws: { kyber: Object.fromEntries(config.wrapSell.sizes.map((x) => [x, x])) } }, err: [], sectionAt: { stETH: ts, cbETH: ts, st: ts, rt: ts } };
}

function harness({ cached, collect, history, latest, brokenStorage = false } = {}) {
  let now = 1792065600000, timerId = 0;
  const timers = new Map(), ids = new Map(), intervals = [], calls = [], events = {};
  const el = (id) => {
    if (!ids.has(id)) ids.set(id, { id, innerHTML: "", textContent: "", events: {}, value: "", hidden: false, disabled: false,
      addEventListener(type, fn) { this.events[type] = fn; }, focus() {} });
    return ids.get(id);
  };
  const form = el("st-settings");
  form.elements = [...source("index.html").matchAll(/<input name="([^"]+)"/g)].map((m) => ({ name: m[1], value: "" }));
  for (const field of form.elements) form.elements[field.name] = field;
  form.reportValidity = () => true;
  const storage = new Map();
  if (cached) {
    const views = Object.fromEntries(["stETH", "cbETH", "st", "rt"].map((key) => [key, { snap: cached, cfg: config, at: cached.sectionAt[key], partial: false }]));
    storage.set("lpm-view-v1", JSON.stringify({ config: JSON.stringify(config), views }));
  }
  const context = vm.createContext({ self: {}, AbortController, console,
    Date: class extends Date { static now() { return now; } },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; }, clearInterval() {},
    document: { getElementById: el, hidden: false, addEventListener(type, fn) { events[type] = fn; } },
    localStorage: { getItem(key) { if (brokenStorage) throw Error("denied"); return storage.get(key); }, setItem(key, value) { if (brokenStorage) throw Error("full"); storage.set(key, value); } },
    fetch: async (url, init) => {
      calls.push({ url, init });
      if (url === "config.json") return response(config);
      if (url === "data/latest.json") return latest ? latest(url, init) : { ok: false };
      if (url.startsWith("data/history")) return history ? history(url, init) : response({ schema: 2, updated: now / 1000, records: [] });
      return { ok: false };
    },
  });
  context.self.Redemption = Redemption;
  vm.runInContext(source("core.js"), context);
  const real = context.self.PegCore;
  let collections = 0;
  const core = Object.assign({}, real, { collect(cfg, fetch, options) { collections++; return collect ? collect(cfg, options, collections) : Promise.resolve(snapshot(now / 1000)); } });
  context.window = { PegCore: core };
  vm.runInContext(source("app.js"), context);
  async function tick(ms) {
    const end = now + ms;
    await flush();
    for (;;) {
      const next = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at; timers.delete(next[0]); next[1].fn(); await flush();
    }
    now = end; await flush();
  }
  return { el, calls, tick, intervals, storage, click: () => el("refresh").events.click(), count: () => collections, now: () => now / 1000 };
}

(async () => {
  const cached = snapshot(1792065000, 0.99), live = deferred(), latest = deferred();
  cached.err = ["old cached source failure"];
  let progress;
  const h = harness({ cached, collect: (_, opts) => { progress = opts.onProgress; return live.promise; }, latest: () => latest.promise });
  await flush();
  assert.match(h.el("cards").innerHTML, /本地缓存.*已过期/);
  assert.match(h.el("cards").innerHTML, /−100\.0/);
  assert.ok(!h.el("vnote").innerHTML.includes("old cached source failure"), "cached failures are never described as current-run failures");
  const originalClock = h.el("clock").textContent;
  assert.equal(h.el("refresh").disabled, true);
  const partial = snapshot(h.now(), 1.002); partial.px.stETH = {}; partial.st = null; partial.rt = null;
  progress(clone(partial)); await h.tick(50);
  assert.match(h.el("cards").innerHTML.split('id="card-cbETH"')[1], /实时 · 部分数据/);
  assert.match(h.el("cards").innerHTML.split('id="card-cbETH"')[0], /本地缓存/);
  latest.resolve(response({ schema: 1, config: JSON.stringify(config), snapshot: snapshot(h.now() - 60, 0.95) })); await flush();
  assert.ok(!h.el("cards").innerHTML.split('id="card-cbETH"')[1].includes("−500.0"), "late published cache cannot overwrite live section");
  live.resolve(snapshot(h.now(), 1.003)); await flush();
  assert.equal(h.el("refresh").disabled, false);
  assert.match(h.el("cards").innerHTML, /实时 · 采集/);
  assert.notEqual(h.el("clock").textContent, originalClock);
  assert.equal(JSON.parse(h.storage.get("lpm-view-v1")).views.st.snap.st.quoteMeta.kyber[50].quotedAt, h.now() - 10, "cache preserves original quote time");

  const old = deferred(), newer = deferred(), hungHistory = deferred();
  let oldOptions, newOptions;
  const timed = harness({ cached, history: () => hungHistory.promise,
    collect: (_, opts, n) => { if (n === 1) { oldOptions = opts; return old.promise; } newOptions = opts; return newer.promise; } });
  await flush();
  timed.click(); timed.click(); assert.equal(timed.count(), 1, "double click is single-flight");
  oldOptions.onProgress(snapshot(timed.now(), 1.001));
  await timed.tick(30000);
  assert.equal(oldOptions.signal.aborted, true);
  assert.equal(timed.el("refresh").disabled, false, "hung live request releases controls at 30 seconds");
  assert.match(timed.el("vnote").innerHTML, /读取超时/);
  assert.match(timed.el("vnote").innerHTML, /历史读取失败/);
  assert.match(timed.el("cards").innerHTML, /部分数据.*更新失败/);
  const nextRun = timed.click(); await flush();
  newOptions.onProgress(snapshot(timed.now(), 1.004)); await timed.tick(50);
  const beforeLate = timed.el("cards").innerHTML;
  oldOptions.onProgress(snapshot(timed.now(), 0.5)); old.resolve(snapshot(timed.now(), 0.5));
  hungHistory.resolve(response({ schema: 2, updated: 1, records: [] })); await flush();
  assert.equal(timed.el("cards").innerHTML, beforeLate, "expired progress and completion cannot replace a newer refresh");
  assert.equal(timed.el("refresh").disabled, true, "old finally cannot unlock a newer refresh");
  newer.resolve(snapshot(timed.now(), 1.004)); await nextRun;
  assert.equal(timed.el("refresh").disabled, false);

  const histBody = deferred();
  const separate = harness({ history: () => ({ ok: true, json: () => histBody.promise }) });
  await flush();
  assert.equal(separate.el("refresh").disabled, false, "history body does not block successful live data");
  assert.match(separate.el("vnote").innerHTML, /历史单独加载中/);
  await separate.tick(8000);
  assert.match(separate.el("vnote").innerHTML, /历史读取失败/);
  assert.equal(separate.calls.find((c) => c.url.startsWith("data/history")).init.signal.aborted, true);

  let fail = false;
  const failure = harness({ cached, brokenStorage: true, collect: () => fail ? Promise.reject(Error("source failed")) : Promise.resolve(snapshot(1792065600)), history: () => ({ ok: false, status: 503 }) });
  await flush(); fail = true; const clock = failure.el("clock").textContent;
  await failure.click();
  assert.equal(failure.el("clock").textContent, clock, "failed refresh never advances observation time");
  assert.match(failure.el("cards").innerHTML, /上轮数据.*更新失败/);
  assert.match(failure.el("vnote").innerHTML, /source failed/);
  assert.equal(failure.el("refresh").disabled, false);
  const expiry = failure.intervals.find((i) => i.ms === 10000);
  await failure.tick(181000); expiry.fn();
  assert.match(failure.el("cards").innerHTML, /已过期/);

  const cold = harness({ collect: () => new Promise(() => {}), latest: () => response({ schema: 1, config: JSON.stringify(config), snapshot: cached }) });
  await flush();
  assert.match(cold.el("cards").innerHTML, /后台缓存.*已过期/);
  await cold.tick(30000);
  assert.equal(cold.el("refresh").disabled, false);
  console.log("Progressive refresh passed: cached/cold start, independent sections/history, normal/failed/hung fetch and body, timeout, abort, repeat refresh, late completion, timestamps, storage failure and expiry");
})().catch((e) => { console.error(e); process.exitCode = 1; });
