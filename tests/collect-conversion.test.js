"use strict";
const assert = require("node:assert/strict");
const Core = require("../core.js");
const { buildConversionRate } = require("../scripts/conversion-rate.js");
const config = require("../config.json");
const cfg = { ...config, sizes: [], roundTrip: { sizes: [] }, wrapSell: { sizes: [] }, stRedeem: { sizes: [] } };
const raw = 1234567890123456789n;
const word = (n) => "0x" + BigInt(n).toString(16).padStart(64, "0");
const response = (data) => ({ ok: true, json: async () => data });
const blockAt = 1780000000;
const block = { number: "0x18cba80", hash: "0x" + "ab".repeat(32), timestamp: "0x" + blockAt.toString(16) };

async function collect(mode, options = { conversionSource: true }) {
  let now = (blockAt + 12) * 1000;
  const savedNow = Date.now;
  Date.now = () => now;
  const calls = [], blockRequests = [];
  const mock = async (url, init) => {
    if (init && init.method === "POST") {
      const req = JSON.parse(init.body);
      if (!Array.isArray(req)) {
        assert.equal(req.method, "eth_getBlockByNumber");
        blockRequests.push(url);
        if (mode === "no-block" || mode === "block-fallback" && url === cfg.rpcs[0]) throw Error("block RPC unavailable");
        return response({ result: mode === "bad-block" ? { ...block, hash: "0xbad" } : block });
      }
      if (mode === "rate-fallback" && url === cfg.rpcs[0]) throw Error("batch RPC unavailable");
      return response(req.map((call) => {
        const selector = call.params[0].data.slice(0, 10);
        if (selector === "0x035faf82") {
          calls.push({ call, url });
          assert.equal(call.params[0].to, cfg.addresses.wstETH);
          if (mode === "reorg" && typeof call.params[1] === "object") return { id: call.id, error: { code: -32000, message: "block is not canonical" } };
          if (mode === "missing") return { id: call.id };
          if (mode === "error") return { id: call.id, error: { code: -32000 }, result: word(raw) };
          if (mode === "zero") return { id: call.id, result: word(0) };
          if (mode === "malformed") return { id: call.id, result: word(raw) + "ff" };
          return { id: call.id, result: word(raw) };
        }
        return { id: call.id, result: word(selector === "0xc6610657" ? BigInt(cfg.addresses.ETH) : 10n ** 18n) };
      }));
    }
    if (url === cfg.http.okxBook) {
      // Simulate a slow unrelated market request after the source RPC completes.
      await new Promise((resolve) => setImmediate(resolve));
      now += 120000;
      return response({ data: [{ bids: [["0.99", "1000"]] }] });
    }
    if (url === cfg.http.coinbaseBook) return response({ bids: [["1.1", "1000"]] });
    if (url === cfg.http.coinbaseCbethInfo) return response({ conversion_rate: "1.1", redeem_time_estimate_days: "5", apy: "0.03" });
    throw Error("Unexpected URL " + url);
  };
  try {
    const snap = await Core.collect(cfg, mock, options);
    return { snap, json: buildConversionRate(snap, cfg, now / 1000), calls, blockRequests };
  } finally { Date.now = savedNow; }
}

(async () => {
  const { snap, json, calls } = await collect("ok");
  assert.equal(calls.length, 1, "export must reuse the collected anchor, without a second rate call");
  assert.deepEqual(calls[0].call.params[1], { blockHash: block.hash, requireCanonical: true }, "rate call must use the exact timestamped block hash");
  assert.equal(snap.wstethConversion.raw, raw.toString());
  assert.equal(json.amount, "1.234567890123456789", "stETH per one wstETH, never inverse or market peg");
  assert.equal(json.observed_at, new Date(blockAt * 1000).toISOString());
  assert.equal(json.fetched_at, new Date((blockAt + 12) * 1000).toISOString());
  assert.equal(snap.ts, blockAt + 132, "slow unrelated calls must not refresh the rate's source time");
  assert.equal(json.source.block_number, BigInt(block.number).toString());
  assert.equal(json.source.block_hash, block.hash);
  for (const mode of ["missing", "error", "zero", "malformed", "no-block", "bad-block", "reorg"]) {
    const result = await collect(mode);
    assert.equal(result.json.status, "unavailable", mode);
    assert.equal(result.json.amount, null, mode);
    assert.equal(result.json.observed_at, null, mode);
    assert.equal(result.snap.wstethConversion, null, mode);
    if (mode === "reorg") assert.equal(result.snap.nav.wstETH, Number(raw) / 1e18, "unpinned fallback preserves dashboard but never feeds public export");
  }
  const fallback = await collect("rate-fallback");
  assert.equal(fallback.json.status, "ok");
  assert.equal(fallback.json.source.rpc_url, cfg.rpcs[1], "provenance names the RPC that actually returned the rate");
  assert.deepEqual(fallback.calls[0].call.params[1], { blockHash: block.hash, requireCanonical: true });
  const blockFallback = await collect("block-fallback");
  assert.equal(blockFallback.json.status, "ok");
  assert.equal(blockFallback.json.source.rpc_url, cfg.rpcs[1]);
  const browser = await collect("ok", {});
  assert.equal(browser.blockRequests.length, 0, "browser collection keeps its existing request count");
  assert.equal(browser.calls[0].call.params[1], "latest");
  assert.equal(browser.snap.nav.wstETH, Number(raw) / 1e18, "existing dashboard anchor is preserved");
  console.log("12 conversion collection integration cases passed");
})().catch((e) => { console.error(e); process.exitCode = 1; });
