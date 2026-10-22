"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const { SPECS, normalize, buildSwapRate, collectSwapRates, writeSwapRates } = require("../scripts/swap-rate.js");
const cfg = require("../config.json"), now = 1792690000, ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
const raw = "124674722897333204917", clone = v => JSON.parse(JSON.stringify(v));
const response = data => ({ ok: true, json: async () => data });
const block = { number: "100", hash: "0x" + "ab".repeat(32), timestamp: now - 12, rpcUrl: cfg.rpcs[0] };
function kyber(spec, output = raw) {
  return { code: 0, data: { routeSummary: { tokenIn: spec.token, tokenOut: ETH,
    amountIn: String(BigInt(spec.size) * 10n ** 18n), amountOut: output, timestamp: now - 2,
    extraFee: { feeAmount: "" }, route: [[{ tokenIn: spec.token, tokenOut: ETH,
      swapAmount: String(BigInt(spec.size) * 10n ** 18n), exchange: "curve" }]] } } };
}
function velora(spec, output = raw) {
  return { priceRoute: { network: spec.chainId, side: "SELL", version: "6.2", blockNumber: 100,
    srcDecimals: 18, destDecimals: 18, srcToken: spec.token, destToken: ETH,
    srcAmount: String(BigInt(spec.size) * 10n ** 18n), destAmount: String(BigInt(output) + 1000n),
    destAmountAfterFee: output, partnerFee: 0.01,
    bestRoute: [{ percent: 100, swaps: [{ srcToken: spec.token, swapExchanges: [{ srcAmount: String(BigInt(spec.size) * 10n ** 18n), exchange: "maverickv2" }] }] }] } };
}
const k = (spec, output) => normalize(spec, "kyberswap", kyber(spec, output), now);
const v = (spec, output) => normalize(spec, "velora", velora(spec, output), now, block);

(async () => {
  for (const spec of SPECS) {
    const exact = buildSwapRate(spec, [k(spec), v(spec, String(BigInt(raw) + 1n))], now);
    assert.equal(exact.amount_out_wei, String(BigInt(raw) + 1n), "one wei beats the other provider without float rounding");
    assert.equal(exact.amount, "124.674722897333204918");
    assert.equal(exact.source.provider, "velora");
    assert.equal(exact.base_amount, spec.size); assert.equal(exact.chain_id, spec.chainId);
    assert.equal(exact.amount_in_wei, String(BigInt(spec.size) * 10n ** 18n));
    assert.equal(exact.valid_until, new Date((now - 12 + 900) * 1000).toISOString());
    assert.equal(exact.source.source_time_basis, "source_block_timestamp");
    assert.equal(exact.unit_rate, spec.size === "100" ? "1.246747228973332049" : "12.467472289733320491");
    assert.equal(exact.coverage.available, 2); assert.equal(exact.gas_included, false); assert.equal(exact.executable, false);
    const fees = buildSwapRate(spec, [k(spec), v(spec, String(BigInt(raw) - 1n))], now);
    assert.equal(fees.source.provider, "kyberswap", "compare Velora's after-fee output, not its larger gross quote");
    assert.equal(fees.quotes[1].aggregator_fee_amount_wei, "1000");
    const stale = buildSwapRate(spec, [k(spec)], now + 181);
    assert.equal(stale.status, "unavailable"); assert.equal(stale.amount, null); assert.equal(stale.observed_at, null);
    const wrong = k(spec); wrong.source.chain_id = 999;
    assert.equal(buildSwapRate(spec, [wrong], now).status, "unavailable");
    assert.equal(buildSwapRate(spec, [k(spec)], now).coverage.partial, true);
    assert.equal(buildSwapRate(spec, [k(spec), k(spec)], now).status, "unavailable");
    for (const mutate of [q => q.amountIn = "1", q => q.tokenIn = ETH, q => q.tokenOut = spec.token,
      q => q.amountOut = "0", q => q.amountOut = "1.23", q => q.timestamp = now + 1,
      q => q.timestamp = now - 181, q => q.route[0][0].swapAmount = "1", q => q.extraFee.feeAmount = "1"]) {
      const bad = kyber(spec); mutate(bad.data.routeSummary);
      assert.throws(() => normalize(spec, "kyberswap", bad, now));
    }
    for (const mutate of [q => q.network = 999, q => q.side = "BUY", q => q.srcDecimals = 6,
      q => q.destAmountAfterFee = String(BigInt(q.destAmount) + 1n), q => delete q.destAmountAfterFee,
      q => q.bestRoute[0].percent = 99, q => q.bestRoute[0].swaps[0].swapExchanges[0].srcAmount = "1"]) {
      const bad = velora(spec); mutate(bad.priceRoute);
      assert.throws(() => normalize(spec, "velora", bad, now, block));
    }
    assert.throws(() => normalize(spec, "velora", velora(spec), now, { ...block, number: "101" }));
  }
  const split = kyber(SPECS[1]);
  split.data.routeSummary.route.push([{ tokenIn: cfg.addresses.stETH, tokenOut: ETH, swapAmount: raw, exchange: "lido-arm" }]);
  assert.equal(normalize(SPECS[1], "kyberswap", split, now).amount_out_wei, raw, "shared unwrap plus split stETH legs are not counted as extra wstETH input");
  const old = k(SPECS[0]); old.observedAt = now - 150;
  const skew = buildSwapRate(SPECS[0], [old, v(SPECS[0])], now);
  assert.equal(skew.coverage.available, 1); assert.equal(skew.quotes[0].error, "comparison_time_skew");

  const active = {}, max = {}, calls = [];
  const mock = async (url, init) => {
    calls.push({ url, init });
    if (init.method === "POST") {
      const req = JSON.parse(init.body); assert.equal(req.method, "eth_getBlockByNumber");
      assert.equal(req.params[0], "0x64");
      return response({ jsonrpc: "2.0", id: 1, result: { number: "0x64", hash: block.hash, timestamp: "0x" + block.timestamp.toString(16) } });
    }
    const u = new URL(url), provider = u.hostname.includes("kyberswap") ? "kyberswap" : "velora";
    const token = u.searchParams.get(provider === "velora" ? "srcToken" : "tokenIn");
    const spec = SPECS.find(s => s.token === token);
    assert.ok(spec); assert.equal(u.searchParams.get(provider === "velora" ? "amount" : "amountIn"), String(BigInt(spec.size) * 10n ** 18n));
    active[provider] = (active[provider] || 0) + 1; max[provider] = Math.max(max[provider] || 0, active[provider]);
    await new Promise(r => setImmediate(r)); active[provider]--;
    return response(provider === "velora" ? velora(spec) : kyber(spec));
  };
  const rates = await collectSwapRates(cfg, mock, { now: () => now });
  assert.ok(Object.values(rates).every(q => q.status === "ok" && q.coverage.available === 2));
  assert.deepEqual(max, { kyberswap: 1, velora: 1 }); assert.equal(calls.length, 6);
  const partial = await collectSwapRates(cfg, (url, init) => url.includes("api.velora.xyz") ? Promise.resolve({ ok: false, status: 403 }) : mock(url, init), { now: () => now });
  assert.ok(Object.values(partial).every(q => q.status === "ok" && q.coverage.available === 1 && q.quotes[1].error === "http_403"));
  let attempted = 0;
  const timed = await collectSwapRates(cfg, () => { attempted++; return new Promise(() => {}); }, { now: () => now, timeoutMs: 10 });
  assert.ok(Object.values(timed).every(q => q.status === "unavailable" && q.amount === null));
  assert.equal(attempted, 2, "aborted hung providers never start more network calls");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "swap-rate-test-"));
  try {
    writeSwapRates(dir, rates);
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, SPECS[0].file))).status, "ok");
    const previous = fs.readFileSync(path.join(dir, SPECS[0].file), "utf8"), rename = fs.renameSync;
    fs.renameSync = (from, to) => { if (to.endsWith(SPECS[0].file)) throw Error("injected first-file failure"); return rename(from, to); };
    try { assert.throws(() => writeSwapRates(dir, timed), AggregateError); }
    finally { fs.renameSync = rename; }
    assert.equal(fs.readFileSync(path.join(dir, SPECS[0].file), "utf8"), previous, "failed replacement preserves original bytes and times");
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, SPECS[1].file))).status, "unavailable", "first-file failure does not block the other export");
    writeSwapRates(dir, timed);
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, SPECS[0].file))).amount, null, "failed sample replaces old success");
    assert.ok(fs.readdirSync(dir).every(p => p.endsWith(".json")));
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  console.log("Fixed-size swap quotes: exact input and wei output, after-fee selection, chain/source checks, stale/skew/failure, serial requests, deadline and atomic publication passed");
})().catch(e => { console.error(e); process.exitCode = 1; });
