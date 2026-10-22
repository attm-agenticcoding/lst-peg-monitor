"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const MAX_AGE_SECONDS = 1800;
const UINT256_MAX = (1n << 256n) - 1n;
const SCALE = 10n ** 18n;
const MAX_DATE_SECONDS = 8_640_000_000_000;
const ADDRESS = /^0x[0-9a-f]{40}$/i;
const HASH = /^0x[0-9a-f]{64}$/i;
const DECIMAL = /^(0|[1-9][0-9]*)$/;
const WSTETH = { base: "wstETH", quote: "stETH", key: "wstethConversion", method: "stEthPerToken()", selector: "0x035faf82" };
const CBETH = { base: "cbETH", quote: "ETH", key: "cbethConversion", method: "exchangeRate()", selector: "0x3ba0b9a9" };

function uint256(value) {
  if (typeof value !== "string" || value.length > 78 || !DECIMAL.test(value)) return null;
  const n = BigInt(value);
  return n <= UINT256_MAX ? n : null;
}

function unixSeconds(value) {
  return Number.isSafeInteger(value) && value > 0 && value <= MAX_DATE_SECONDS - MAX_AGE_SECONDS;
}

const iso = (seconds) => new Date(seconds * 1000).toISOString();

function configuredRpc(url, cfg) {
  if (typeof url !== "string" || !Array.isArray(cfg && cfg.rpcs) || !cfg.rpcs.includes(url)) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password;
  } catch { return false; }
}

/** Build the contract conversion quote without rounding or borrowing snapshot time.
 * observedAt is the timestamp of the block used by the contract read, not the time
 * this JSON was generated. Consumers must also check valid_until when reading a
 * cached static response: a stopped publisher cannot update its own status.
 */
function buildContractConversionRate(snap, cfg, spec, nowSeconds) {
  if (!unixSeconds(nowSeconds)) throw new RangeError("nowSeconds must be valid Unix seconds");
  const contract = cfg && cfg.addresses && cfg.addresses[spec.base];
  const out = {
    schema_version: 1,
    status: "unavailable",
    base: spec.base,
    quote: spec.quote,
    base_amount: "1",
    amount: null,
    observed_at: null,
    fetched_at: null,
    generated_at: iso(nowSeconds),
    valid_until: null,
    max_age_seconds: MAX_AGE_SECONDS,
    source: {
      type: "ethereum_contract",
      chain_id: 1,
      contract: typeof contract === "string" ? contract : null,
      method: spec.method,
      selector: spec.selector,
      block_number: null,
      block_hash: null,
      rpc_url: null,
    },
    error: "source_unavailable",
  };
  const q = snap && snap[spec.key];
  if (!q || typeof q !== "object" || Array.isArray(q)) return out;
  const raw = uint256(q.raw), block = uint256(q.blockNumber);
  if (raw === null || raw === 0n || block === null || block === 0n
      || typeof contract !== "string" || !ADDRESS.test(contract)
      || typeof q.contract !== "string" || !ADDRESS.test(q.contract)
      || q.contract.toLowerCase() !== contract.toLowerCase()
      || typeof q.blockHash !== "string" || !HASH.test(q.blockHash) || /^0x0{64}$/i.test(q.blockHash)
      || !configuredRpc(q.rpcUrl, cfg)
      || !unixSeconds(q.observedAt) || !unixSeconds(q.fetchedAt)
      || q.observedAt > q.fetchedAt || q.fetchedAt > nowSeconds
      || nowSeconds - q.observedAt > MAX_AGE_SECONDS) return out;

  out.status = "ok";
  out.amount = `${raw / SCALE}.${String(raw % SCALE).padStart(18, "0")}`;
  out.observed_at = iso(q.observedAt);
  out.fetched_at = iso(q.fetchedAt);
  out.valid_until = iso(q.observedAt + MAX_AGE_SECONDS);
  out.source.block_number = q.blockNumber;
  out.source.block_hash = q.blockHash;
  out.source.rpc_url = q.rpcUrl;
  delete out.error;
  return out;
}

function buildConversionRate(snap, cfg, nowSeconds = Math.floor(Date.now() / 1000)) {
  return buildContractConversionRate(snap, cfg, WSTETH, nowSeconds);
}

function buildCbethConversionRate(snap, cfg, nowSeconds = Math.floor(Date.now() / 1000)) {
  const out = buildContractConversionRate(snap, cfg, CBETH, nowSeconds);
  // The source block proves when we observed the rate, not when Coinbase last
  // changed it. exchangeRate() exposes no update timestamp; do not invent one.
  out.provider_updated_at = null;
  return out;
}

/** Replace one complete JSON file atomically; never keep a successful rate as
 * the result of a failed collection. A write/rename failure preserves the prior
 * file, including its original observation and expiration timestamps.
 */
function writeJson(file, out) {
  const target = path.resolve(file);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(out, null, 2) + "\n", { encoding: "utf8", flag: "wx", mode: 0o644 });
    fs.renameSync(temporary, target);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
  return out;
}

function writeConversionRate(file, snap, cfg, nowSeconds = Math.floor(Date.now() / 1000)) {
  return writeJson(file, buildConversionRate(snap, cfg, nowSeconds));
}

function writeCbethConversionRate(file, snap, cfg, nowSeconds = Math.floor(Date.now() / 1000)) {
  return writeJson(file, buildCbethConversionRate(snap, cfg, nowSeconds));
}

module.exports = { buildConversionRate, writeConversionRate, buildCbethConversionRate, writeCbethConversionRate, writeJson };
