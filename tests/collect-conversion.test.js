"use strict";
const assert = require("node:assert/strict");
const Core = require("../core.js");
const { buildConversionRate, buildCbethConversionRate } = require("../scripts/conversion-rate.js");
const config = require("../config.json");
const cfg = { ...config, sizes: [], roundTrip: { sizes: [] }, wrapSell: { sizes: [] }, stRedeem: { sizes: [] } };
const raw = 1234567890123456789n;
const rawC = 1141640441769055706n;
const word = (n) => "0x" + BigInt(n).toString(16).padStart(64, "0");
const response = (data) => ({ ok: true, json: async () => data });
const blockAt = 1780000000;
const block = { number: "0x18cba80", hash: "0x" + "ab".repeat(32), timestamp: "0x" + blockAt.toString(16) };

async function collect(mode, options = { conversionSource: true }) {
  let now = (blockAt + 12) * 1000;
  const savedNow = Date.now;
  Date.now = () => now;
  const calls = [], cbCalls = [], blockRequests = [];
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
        if (selector === "0x3ba0b9a9") {
          cbCalls.push({ call, url });
          assert.equal(call.params[0].to, cfg.addresses.cbETH);
          if (mode === "cb-reorg" && typeof call.params[1] === "object") return { id: call.id, error: { code: -32000 } };
          if (mode === "cb-missing") return { id: call.id };
          if (mode === "cb-error") return { id: call.id, error: { code: -32000 }, result: word(rawC) };
          if (mode === "cb-zero") return { id: call.id, result: word(0) };
          if (mode === "cb-malformed") return { id: call.id, result: word(rawC) + "ff" };
          return { id: call.id, result: word(rawC) };
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
    return { snap, json: buildConversionRate(snap, cfg, now / 1000),
      cbJson: buildCbethConversionRate(snap, cfg, now / 1000), calls, cbCalls, blockRequests };
  } finally { Date.now = savedNow; }
}

(async () => {
  const { snap, json, cbJson, calls, cbCalls } = await collect("ok");
  assert.equal(calls.length, 1, "export must reuse the collected anchor, without a second rate call");
  assert.deepEqual(calls[0].call.params[1], { blockHash: block.hash, requireCanonical: true }, "rate call must use the exact timestamped block hash");
  assert.equal(snap.wstethConversion.raw, raw.toString());
  assert.equal(json.amount, "1.234567890123456789", "stETH per one wstETH, never inverse or market peg");
  assert.equal(json.observed_at, new Date(blockAt * 1000).toISOString());
  assert.equal(json.fetched_at, new Date((blockAt + 12) * 1000).toISOString());
  assert.equal(snap.ts, blockAt + 132, "slow unrelated calls must not refresh the rate's source time");
  assert.equal(json.source.block_number, BigInt(block.number).toString());
  assert.equal(json.source.block_hash, block.hash);
  assert.equal(cbCalls.length, 1, "cbETH export reuses the existing anchor call");
  assert.deepEqual(cbCalls[0].call.params[1], { blockHash: block.hash, requireCanonical: true });
  assert.equal(snap.cbethConversion.raw, rawC.toString());
  assert.equal(snap.nav.cbETH, Number(rawC) / 1e18, "dashboard calculation is unchanged");
  assert.equal(snap.rt.rate, 1.1, "Coinbase redemption inputs remain unchanged");
  assert.equal(cbJson.base, "cbETH"); assert.equal(cbJson.quote, "ETH");
  assert.equal(cbJson.amount, "1.141640441769055706", "all 18 decimals, no float reconstruction or price multiplication");
  assert.equal(cbJson.observed_at, json.observed_at);
  assert.equal(cbJson.fetched_at, json.fetched_at);
  assert.equal(cbJson.valid_until, json.valid_until);
  assert.equal(cbJson.provider_updated_at, null);
  assert.equal(cbJson.source.block_hash, block.hash);
  for (const mode of ["missing", "error", "zero", "malformed", "no-block", "bad-block", "reorg"]) {
    const result = await collect(mode);
    assert.equal(result.json.status, "unavailable", mode);
    assert.equal(result.json.amount, null, mode);
    assert.equal(result.json.observed_at, null, mode);
    assert.equal(result.snap.wstethConversion, null, mode);
    assert.equal(result.cbJson.status, ["no-block", "bad-block"].includes(mode) ? "unavailable" : "ok", "wstETH errors do not discard cbETH");
    if (mode === "reorg") assert.equal(result.snap.nav.wstETH, Number(raw) / 1e18, "unpinned fallback preserves dashboard but never feeds public export");
  }
  for (const mode of ["cb-missing", "cb-error", "cb-zero", "cb-malformed", "cb-reorg", "no-block", "bad-block"]) {
    const result = await collect(mode);
    assert.equal(result.cbJson.status, "unavailable", mode);
    assert.equal(result.cbJson.amount, null, mode);
    assert.equal(result.cbJson.observed_at, null, mode);
    assert.equal(result.snap.cbethConversion, null, mode);
    assert.equal(result.json.status, ["no-block", "bad-block"].includes(mode) ? "unavailable" : "ok", "cbETH errors do not discard wstETH");
  }
  const fallback = await collect("rate-fallback");
  assert.equal(fallback.json.status, "ok");
  assert.equal(fallback.json.source.rpc_url, cfg.rpcs[1], "provenance names the RPC that actually returned the rate");
  assert.deepEqual(fallback.calls[0].call.params[1], { blockHash: block.hash, requireCanonical: true });
  assert.equal(fallback.cbJson.source.rpc_url, cfg.rpcs[1]);
  assert.deepEqual(fallback.cbCalls[0].call.params[1], { blockHash: block.hash, requireCanonical: true });
  const blockFallback = await collect("block-fallback");
  assert.equal(blockFallback.json.status, "ok");
  assert.equal(blockFallback.json.source.rpc_url, cfg.rpcs[1]);
  assert.equal(blockFallback.cbJson.source.rpc_url, cfg.rpcs[1]);
  const browser = await collect("ok", {});
  assert.equal(browser.blockRequests.length, 0, "browser collection keeps its existing request count");
  assert.equal(browser.calls[0].call.params[1], "latest");
  assert.equal(browser.snap.nav.wstETH, Number(raw) / 1e18, "existing dashboard anchor is preserved");
  assert.equal(browser.snap.nav.cbETH, Number(rawC) / 1e18);
  assert.equal(browser.snap.cbethConversion, null, "unpinned browser reads cannot feed the public endpoint");
  console.log("19 conversion collection integration cases passed");
})().catch((e) => { console.error(e); process.exitCode = 1; });
