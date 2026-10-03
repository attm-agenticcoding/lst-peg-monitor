"use strict";
const assert = require("node:assert/strict");
const Core = require("../core.js");
const config = require("../config.json");
const R = require("../redemption.js");
const word = (n) => "0x" + BigInt(n).toString(16).padStart(64, "0");
const response = (d, status = 200) => ({ ok: status === 200, status, json: async () => d });
const cfg = Object.assign({}, config, { sizes: [1], roundTrip: { sizes: [] }, wrapSell: { sizes: [] },
  stRedeem: Object.assign({}, config.stRedeem, { sizes: [1.234, 5] }) });
async function run(deny) {
  const amounts = [], buyAmounts = [];
  const mock = async (url, init) => {
    if (init && init.method === "POST") {
      const req = JSON.parse(init.body);
      if (!Array.isArray(req)) {
        if (req.method === "eth_gasPrice") return response({ result: "0x12a05f200" });
        if (req.method === "eth_getBlockByNumber") return response({ result: { number: "0x174876e", timestamp: "0x" + Math.floor(Date.now() / 1000).toString(16) } });
        throw Error(req.method);
      }
      return response(req.map((call) => {
        const data = call.params[0].data, selector = data.slice(0, 10);
        let result = word(10n ** 18n);
        if (["0x2b95b781", "0xb187bd26"].includes(selector)) result = word(0n);
        if (selector === "0xd0fb84e8") { result = word(100000n * 10n ** 18n); assert.notEqual(call.params[1], "latest", "queue calls must pin a block"); }
        if (selector === "0xc6610657") result = word(BigInt(config.addresses.ETH));
        if (selector === "0x5e0d443f") {
          const dx = BigInt("0x" + data.slice(-64)); result = word(dx); // less profitable than Kyber
        }
        return { id: call.id, result };
      }));
    }
    if (url.startsWith(cfg.http.lidoWq)) {
      amounts.push(Number(new URL(url).searchParams.get("amount")));
      if (deny) return response({}, 403);
      return response({ status: "calculated", requestInfo: { finalizationIn: 2 * 864e5,
        finalizationAt: new Date(Date.now() + 2 * 864e5).toISOString(), type: "buffer" } });
    }
    if (url.startsWith(cfg.http.lidoApr)) return response({ data: { smaApr: 3 } });
    if (url.startsWith(cfg.http.coinbaseCbethInfo)) return response({ conversion_rate: "1.15", redeem_time_estimate_days: "5", apy: "0.03" });
    if (url.startsWith(cfg.http.coinbaseBook)) return response({ bids: [["1.15", "1000"]] });
    if (url.startsWith(cfg.http.okxBook)) return response({ data: [{ bids: [["1", "1000"]] }] });
    if (url.includes("routes?")) {
      const u = new URL(url), input = BigInt(u.searchParams.get("amountIn"));
      const stBuy = u.searchParams.get("tokenIn") === cfg.addresses.ETH && u.searchParams.get("tokenOut") === cfg.addresses.stETH;
      if (stBuy) buyAmounts.push(input);
      return response({ data: { routeSummary: { amountOut: String(stBuy ? input * 1001n / 1000n : input), gas: "200000", timestamp: String(Math.floor(Date.now() / 1000)) } } });
    }
    throw Error("Unexpected URL " + url);
  };
  const snap = await Core.collect(cfg, mock);
  assert.deepEqual(buyAmounts, [R.toWei("1.234"), R.toWei("5")]);
  if (deny) {
    assert.equal(amounts.length, 1, "403 must stop the remaining ETA probes in this collection");
    for (const x of cfg.stRedeem.sizes) assert.equal(snap.st.waits[x].status, "unavailable");
  } else {
    assert.deepEqual(amounts, [1.235234, 5.005], "ETA amounts are purchased stETH, never input ETH or the maximum tier");
    for (const x of cfg.stRedeem.sizes) assert.equal(snap.st.waits[x].status, "calculated");
  }
  const result = Core.evaluate(snap, cfg), record = Core.toRecord(snap, result, cfg);
  assert.equal(record.stETH.redemption.calibration, "pending");
  assert.ok(record.stETH.redemption.rows.every((r) => r.actionable === false));
  assert.ok(!("manualWaitDays" in record.stETH.redemption));
}
(async () => { await run(false); await run(true); console.log("2 collection integration tests passed"); })().catch((e) => { console.error(e); process.exitCode = 1; });
