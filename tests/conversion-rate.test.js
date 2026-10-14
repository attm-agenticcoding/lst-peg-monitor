"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { buildConversionRate, writeConversionRate } = require("../scripts/conversion-rate.js");
const cfg = require("../config.json");
const NOW = 1_792_022_400;
const UINT256_MAX = "115792089237316195423570985008687907853269984665640564039457584007913129639935";
const iso = (seconds) => new Date(seconds * 1000).toISOString();
const observation = (changes = {}) => Object.assign({
  raw: "1234567890123456789",
  observedAt: NOW - 30,
  fetchedAt: NOW - 10,
  blockNumber: "24000000",
  blockHash: "0x" + "ab".repeat(32),
  rpcUrl: cfg.rpcs[0],
  contract: cfg.addresses.wstETH,
}, changes);
const snap = (changes = {}) => ({ ts: NOW, nav: { wstETH: 999, stETH: 1 }, px: { wstETH: 0.8 }, wstethConversion: observation(changes) });
const build = (changes = {}, now = NOW) => buildConversionRate(snap(changes), cfg, now);
function unavailable(out) {
  assert.equal(out.status, "unavailable");
  assert.equal(out.error, "source_unavailable");
  for (const key of ["amount", "observed_at", "fetched_at", "valid_until"]) assert.equal(out[key], null, key);
  for (const key of ["block_number", "block_hash", "rpc_url"]) assert.equal(out.source[key], null, key);
}
let passed = 0;
function test(name, fn) { fn(); passed++; console.log("ok -", name); }

test("exact contract ratio is stETH received for one wstETH, independent of market prices", () => {
  const out = build();
  assert.equal(out.schema_version, 1);
  assert.equal(out.status, "ok");
  assert.equal(out.base, "wstETH");
  assert.equal(out.quote, "stETH");
  assert.equal(out.base_amount, "1");
  assert.equal(out.amount, "1.234567890123456789");
  assert.equal(typeof out.amount, "string");
  assert.equal("error" in out, false);
  assert.deepEqual(out.source, {
    type: "ethereum_contract", chain_id: 1, contract: cfg.addresses.wstETH,
    method: "stEthPerToken()", selector: "0x035faf82", block_number: "24000000",
    block_hash: "0x" + "ab".repeat(32), rpc_url: cfg.rpcs[0],
  });
});

test("all 18 decimals are preserved across the uint256 range", () => {
  assert.equal(build({ raw: "1" }).amount, "0.000000000000000001");
  assert.equal(build({ raw: "1000000000000000000" }).amount, "1.000000000000000000");
  assert.equal(build({ raw: "1000000000000000001" }).amount, "1.000000000000000001");
  assert.equal(build({ raw: UINT256_MAX }).amount,
    "115792089237316195423570985008687907853269984665640564039457.584007913129639935");
});

test("source observation, fetch, generation, and expiry retain their separate meanings", () => {
  const input = snap();
  input.ts = NOW + 10000;
  const out = buildConversionRate(input, cfg, NOW);
  assert.equal(out.observed_at, iso(NOW - 30));
  assert.equal(out.fetched_at, iso(NOW - 10));
  assert.equal(out.generated_at, iso(NOW));
  assert.equal(out.valid_until, iso(NOW - 30 + 1800));
  assert.equal(out.max_age_seconds, 1800);
  input.ts = 1;
  assert.deepEqual(buildConversionRate(input, cfg, NOW), out);
  const later = buildConversionRate(input, cfg, NOW + 60);
  assert.equal(later.observed_at, out.observed_at);
  assert.equal(later.valid_until, out.valid_until);
  assert.equal(later.generated_at, iso(NOW + 60));
});

test("freshness is based on the source block with an explicit 1800-second boundary", () => {
  assert.equal(build({ observedAt: NOW - 1800 }).status, "ok");
  assert.equal(build({ observedAt: NOW - 1800 }).valid_until, iso(NOW));
  unavailable(build({ observedAt: NOW - 1801, fetchedAt: NOW }));
  unavailable(build({ observedAt: NOW + 1, fetchedAt: NOW + 1 }));
  unavailable(build({ fetchedAt: NOW + 1 }));
  unavailable(build({ fetchedAt: NOW - 31 }));
});

test("missing source data never falls back to history, market prices, or snapshot time", () => {
  for (const input of [null, undefined, {}, { ts: NOW, nav: { wstETH: 1.25 } },
    { wstethConversion: null }, { wstethConversion: [] }, { wstethConversion: "1.25" }]) {
    unavailable(buildConversionRate(input, cfg, NOW));
  }
});

test("invalid rate representations and uint256 overflow fail closed", () => {
  for (const raw of [null, undefined, 1234567890123456789, 1n, 0, "0", "-1", "+1", "1.2", "1e18", "0x01", "01",
    " 1", "1 ", "", "NaN", "Infinity", "9".repeat(79), String(BigInt(UINT256_MAX) + 1n)]) {
    unavailable(build({ raw }));
  }
});

test("invalid or mismatched source provenance fails closed", () => {
  const cases = [
    { contract: cfg.addresses.stETH }, { contract: null }, { contract: "not-an-address" },
    { blockNumber: null }, { blockNumber: 24000000 }, { blockNumber: "-1" }, { blockNumber: "0" },
    { blockNumber: "2.4e7" }, { blockNumber: String(BigInt(UINT256_MAX) + 1n) },
    { blockHash: null }, { blockHash: "0x1234" }, { blockHash: "0x" + "gh".repeat(32) }, { blockHash: "0x" + "00".repeat(32) },
    { rpcUrl: null }, { rpcUrl: "https://unconfigured.invalid" },
  ];
  for (const changes of cases) unavailable(build(changes));
  assert.equal(build({ contract: cfg.addresses.wstETH.toLowerCase(), rpcUrl: cfg.rpcs[1] }).status, "ok");
  unavailable(buildConversionRate(snap(), {}, NOW));
  for (const rpc of ["not-a-url", "file:///tmp/rpc", "http://rpc.invalid", "https://user:password@rpc.invalid"]) {
    unavailable(buildConversionRate(snap({ rpcUrl: rpc }), { ...cfg, rpcs: [rpc] }, NOW));
  }
});

test("non-integer or missing source times fail closed", () => {
  for (const field of ["observedAt", "fetchedAt"]) {
    for (const value of [null, undefined, String(NOW), 0, -1, NOW - 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER]) {
      unavailable(build({ [field]: value }));
    }
  }
  assert.throws(() => buildConversionRate(snap(), cfg, NaN), RangeError);
});

test("successful publication is replaced by unavailable JSON after a failed collection", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "lpm-conversion-"));
  try {
    const file = path.join(directory, "nested", "conversion-rate.json");
    const successful = writeConversionRate(file, snap(), cfg, NOW);
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), successful);
    const failed = writeConversionRate(file, { ts: NOW + 60 }, cfg, NOW + 60);
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), failed);
    unavailable(failed);
    assert.equal(failed.generated_at, iso(NOW + 60));
    assert.deepEqual(fs.readdirSync(path.dirname(file)), ["conversion-rate.json"]);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("failed atomic replacement preserves complete prior JSON and removes its temporary file", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "lpm-conversion-"));
  const rename = fs.renameSync;
  try {
    const file = path.join(directory, "conversion-rate.json");
    writeConversionRate(file, snap(), cfg, NOW);
    const previous = fs.readFileSync(file, "utf8");
    fs.renameSync = (from, to) => {
      assert.equal(path.dirname(from), directory, "replacement must stay on the same filesystem");
      assert.equal(to, file);
      assert.equal(JSON.parse(fs.readFileSync(from, "utf8")).status, "unavailable");
      throw new Error("simulated rename failure");
    };
    assert.throws(() => writeConversionRate(file, null, cfg, NOW + 60), /simulated rename failure/);
    assert.equal(fs.readFileSync(file, "utf8"), previous);
    assert.equal(JSON.parse(previous).observed_at, iso(NOW - 30));
    assert.deepEqual(fs.readdirSync(directory), ["conversion-rate.json"]);
  } finally {
    fs.renameSync = rename;
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("partial temporary write failure cannot corrupt the previous public file", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "lpm-conversion-"));
  const write = fs.writeFileSync;
  try {
    const file = path.join(directory, "conversion-rate.json");
    writeConversionRate(file, snap(), cfg, NOW);
    const previous = fs.readFileSync(file, "utf8");
    fs.writeFileSync = (temporary, data, options) => {
      write(temporary, data.slice(0, 20), options);
      throw new Error("simulated partial write failure");
    };
    assert.throws(() => writeConversionRate(file, null, cfg, NOW + 60), /simulated partial write failure/);
    assert.equal(fs.readFileSync(file, "utf8"), previous);
    assert.deepEqual(fs.readdirSync(directory), ["conversion-rate.json"]);
  } finally {
    fs.writeFileSync = write;
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

console.log(`${passed} conversion-rate tests passed`);
