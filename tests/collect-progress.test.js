"use strict";
const assert = require("node:assert/strict"), Core = require("../core.js"), cfg = require("../config.json");
const word = (n) => "0x" + BigInt(n).toString(16).padStart(64, "0");
const response = (data) => ({ ok: true, json: async () => data });
const flush = () => new Promise((resolve) => setImmediate(resolve));
const now = Math.floor(Date.now() / 1000);
function transport({ hang, fail = false } = {}) {
  const calls = [], signals = [];
  let release, kyberActive = 0, maxKyber = 0;
  const fetch = async (url, init) => {
    calls.push(url); signals.push(init.signal);
    if (url.includes("routes?")) {
      kyberActive++; maxKyber = Math.max(maxKyber, kyberActive);
      const value = { data: { routeSummary: { amountOut: new URL(url).searchParams.get("amountIn"), timestamp: now - 30, gas: "200000" } } };
      if (hang) {
        const promise = new Promise((resolve) => { release = () => { kyberActive--; resolve(hang === "body" ? value : response(value)); }; });
        return hang === "body" ? { ok: true, json: () => promise } : promise;
      }
      await flush(); kyberActive--; return response(value);
    }
    if (init.method === "POST") {
      const body = JSON.parse(init.body);
      if (!Array.isArray(body)) return response({ result: body.method === "eth_gasPrice" ? "0x12a05f200" : { number: "0x100", timestamp: "0x" + now.toString(16) } });
      return response(body.map((req) => {
        const data = req.params[0].data, sel = data.slice(0, 10);
        let amount = 10n ** 18n;
        if (sel === "0xc6610657") amount = BigInt(cfg.addresses.ETH);
        if (["0x2b95b781", "0xb187bd26"].includes(sel)) amount = 0n;
        if (sel === "0x5e0d443f") amount = BigInt("0x" + data.slice(-64));
        return { id: req.id, result: word(amount) };
      }));
    }
    if (url === cfg.http.okxBook) {
      if (fail) throw Error("OKX unavailable");
      return response({ data: [{ bids: [[1, 100000]] }] });
    }
    if (url === cfg.http.coinbaseBook) return response({ bids: [[1, 100000]] });
    if (url === cfg.http.coinbaseCbethInfo) return response({ conversion_rate: 1, redeem_time_estimate_days: 2, apy: 0.03 });
    if (url === cfg.http.lidoApr) return response({ data: { smaApr: 3 } });
    if (url.startsWith(cfg.http.lidoWq)) return response({ status: "calculated", requestInfo: { finalizationIn: 864e5, finalizationAt: new Date(Date.now() + 864e5).toISOString(), type: "buffer" } });
    throw Error(url);
  };
  return { fetch, calls, signals, release: () => release(), max: () => maxKyber };
}

(async () => {
  const normal = transport(), progress = [];
  const snap = await Core.collect(cfg, normal.fetch, { timeoutMs: 2000, onProgress: (s) => progress.push(s) });
  assert.equal(normal.max(), 1, "Kyber stays serial");
  assert.equal(snap.err.length, 0);
  assert.ok(progress.some((s) => s.px.stETH.okx && !s.st.waits[50]), "sell data arrives before ETA");
  assert.ok(progress.some((s) => s.st.waits[50] && !(s.rt.ws && s.rt.ws.kyber && s.rt.ws.kyber[300])), "stETH ETA is independent of later cbETH Kyber jobs");
  assert.equal(snap.st.quoteMeta.kyber[50].quotedAt, now - 30, "remote quote timestamp is never refreshed by progress");
  assert.ok(snap.st.queue.blockTimestamp === now);
  assert.ok(progress[0] !== progress[1] && progress[0].px !== progress[1].px, "progress snapshots are detached");
  const untouched = JSON.stringify(progress[0]); snap.px.stETH.kyber[1] = 10;
  assert.equal(JSON.stringify(progress[0]), untouched);

  const backend = transport(); await Core.collect(cfg, backend.fetch);
  assert.ok(backend.calls.findIndex((url) => url.startsWith(cfg.http.lidoWq)) > backend.calls.map((url, i) => url.includes("routes?") ? i : -1).reduce((a, b) => Math.max(a, b)), "backend request ordering is unchanged");

  for (const hang of ["fetch", "body"]) {
    const t = transport({ hang }), seen = [];
    await assert.rejects(Core.collect(cfg, t.fetch, { timeoutMs: 30, onProgress: (s) => seen.push(s) }), /超时/);
    assert.ok(seen.some((s) => s.px.cbETH.coinbase), "healthy source progresses during a hung Kyber request");
    const count = t.calls.length, before = JSON.stringify(seen);
    assert.ok(t.signals.some((s) => s.aborted));
    t.release(); await flush(); await flush();
    assert.equal(t.calls.length, count, "no retries or future serial requests after cancellation");
    assert.equal(JSON.stringify(seen), before, "late body/fetch completion cannot publish or mutate snapshots");
  }
  const aborted = new AbortController(); aborted.abort(Error("superseded"));
  let requests = 0;
  await assert.rejects(Core.collect(cfg, () => { requests++; }, { signal: aborted.signal }), /superseded/);
  assert.equal(requests, 0);
  await assert.rejects(Core.collect(cfg, () => new Promise(() => {}), { timeoutMs: 10, onProgress() {} }), /超时/);
  await flush(); // An unused rate dependency must not leave an unhandled rejection.
  const failed = transport({ fail: true });
  const partial = await Core.collect(cfg, failed.fetch, { timeoutMs: 2000, onProgress() {} });
  assert.match(partial.err.join(" "), /OKX unavailable/);
  assert.ok(partial.px.cbETH.coinbase, "a failed source cannot discard successful siblings");
  await assert.rejects(Core.bounded(() => new Promise(() => {}), 5), /超时/, "deadline works without cooperative transport");
  console.log("Progress collection passed: normal/partial success, source independence, serial Kyber, fetch/body hangs, deadline/cancellation, no late publication and original timestamps");
})().catch((e) => { console.error(e); process.exitCode = 1; });
