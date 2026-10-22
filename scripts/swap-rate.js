"use strict";

const path = require("node:path");
const { createHash } = require("node:crypto");
const { bounded } = require("../core.js");
const { writeJson } = require("./conversion-rate.js");

const ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
const SCALE = 10n ** 18n, MAX_UINT = (1n << 256n) - 1n;
const MAX_AGE = 900, SOURCE_MAX_AGE = 180, COMPARISON_WINDOW = 120;
const PROVIDERS = ["kyberswap", "velora"];
const SPECS = [
  { id: "cbeth10", base: "cbETH", size: "10", chain: "base", chainId: 8453,
    token: "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22", file: "cbeth-10-swap-rate.json" },
  { id: "wsteth100", base: "wstETH", size: "100", chain: "ethereum", chainId: 1,
    token: "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0", file: "wsteth-100-swap-rate.json" },
];
const stamp = () => Math.floor(Date.now() / 1000);
const iso = n => new Date(n * 1000).toISOString();
const address = (a, b) => typeof a === "string" && a.toLowerCase() === b;
function integer(s) {
  if (typeof s !== "string" || !/^(0|[1-9][0-9]*)$/.test(s) || s.length > 78) throw Error("invalid_amount");
  const n = BigInt(s); if (n > MAX_UINT) throw Error("invalid_amount"); return n;
}
const decimal = n => `${n / SCALE}.${String(n % SCALE).padStart(18, "0")}`;
const time = n => Number.isSafeInteger(n) && n > 0 && n < 8_640_000_000_000 - MAX_AGE;
const input = spec => BigInt(spec.size) * SCALE;
const digest = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const names = values => [...new Set(values.filter(v => typeof v === "string" && /^[a-z0-9_. -]{1,80}$/i.test(v)))].sort();

function endpoint(spec, provider) {
  const params = provider === "kyberswap"
    ? { tokenIn: spec.token, tokenOut: ETH, amountIn: String(input(spec)), gasInclude: "false" }
    : { srcToken: spec.token, destToken: ETH, amount: String(input(spec)), srcDecimals: "18",
      destDecimals: "18", side: "SELL", network: String(spec.chainId), version: "6.2" };
  const base = provider === "kyberswap" ? `https://aggregator-api.kyberswap.com/${spec.chain}/api/v1/routes`
    : "https://api.velora.xyz/prices";
  return `${base}?${new URLSearchParams(params)}`;
}

/** Never reconstruct exact swap amounts from dashboard prices or NAV. */
function normalize(spec, provider, body, fetchedAt, block = null) {
  const q = provider === "kyberswap" ? body && body.data && body.data.routeSummary : body && body.priceRoute;
  if (!q || !time(fetchedAt)) throw Error("invalid_quote");
  let out, gross, observedAt, exchanges, basis;
  if (provider === "kyberswap") {
    if (body.code !== 0 || !address(q.tokenIn, spec.token) || !address(q.tokenOut, ETH)
        || integer(q.amountIn) !== input(spec) || !Array.isArray(q.route) || !q.route.length
        || q.route.some(r => !Array.isArray(r) || !r.length)) throw Error("quote_input_mismatch");
    const steps = q.route.flat();
    const consumed = steps.filter(s => address(s.tokenIn, spec.token)).reduce((n, s) => n + integer(s.swapAmount), 0n);
    if (consumed !== input(spec)) throw Error("incomplete_input");
    // No integrator fee is requested. Refuse an unexpected fee model rather
    // than guessing whether amountOut has already deducted it.
    if (q.extraFee && q.extraFee.feeAmount && integer(q.extraFee.feeAmount) !== 0n) throw Error("unexpected_fee");
    gross = out = integer(q.amountOut);
    observedAt = Number(q.timestamp); basis = "provider_quote_timestamp";
    exchanges = names(steps.map(s => s.exchange));
  } else {
    if (q.network !== spec.chainId || q.side !== "SELL" || q.version !== "6.2"
        || q.srcDecimals !== 18 || q.destDecimals !== 18
        || !address(q.srcToken, spec.token) || !address(q.destToken, ETH)
        || integer(q.srcAmount) !== input(spec) || !Array.isArray(q.bestRoute) || !q.bestRoute.length
        || q.bestRoute.some(r => !Number.isFinite(r.percent) || r.percent <= 0 || !Array.isArray(r.swaps) || !r.swaps.length)
        || Math.abs(q.bestRoute.reduce((n, r) => n + r.percent, 0) - 100) > 1e-8) throw Error("quote_input_mismatch");
    const first = q.bestRoute.map(r => r.swaps[0]);
    if (first.some(s => !address(s.srcToken, spec.token) || !Array.isArray(s.swapExchanges))
        || first.flatMap(s => s.swapExchanges).reduce((n, s) => n + integer(s.srcAmount), 0n) !== input(spec)) throw Error("incomplete_input");
    gross = integer(q.destAmount);
    if (q.destAmountAfterFee != null) out = integer(q.destAmountAfterFee);
    else if (q.partnerFee === 0) out = gross;
    else throw Error("fee_adjusted_output_missing");
    if (out > gross) throw Error("invalid_fee_adjusted_output");
    if (!block || block.number !== String(q.blockNumber)) throw Error("source_block_unavailable");
    observedAt = block.timestamp; basis = "source_block_timestamp";
    exchanges = names(q.bestRoute.flatMap(r => r.swaps.flatMap(s => s.swapExchanges.map(e => e.exchange))));
  }
  if (out <= 0n || !time(observedAt) || observedAt > fetchedAt || fetchedAt - observedAt > SOURCE_MAX_AGE) throw Error("source_stale_or_future");
  return { provider, status: "ok", amount_in_wei: String(input(spec)), amount_out_wei: String(out), amount: decimal(out),
    amount_before_aggregator_fee: decimal(gross), aggregator_fee_amount_wei: String(gross - out),
    observed_at: iso(observedAt), fetched_at: iso(fetchedAt), observedAt, fetchedAt,
    source: { type: "dex_aggregator", provider, chain_id: spec.chainId, endpoint: endpoint(spec, provider),
      token_in: spec.token, token_out: ETH, source_time_basis: basis,
      block_number: block && block.number || null, block_hash: block && block.hash || null,
      rpc_url: block && block.rpcUrl || null, exchanges, response_sha256: digest(body) } };
}

function buildSwapRate(spec, candidates, now = stamp()) {
  if (!time(now)) throw Error("invalid_clock");
  const compared = PROVIDERS.map(provider => {
    const found = candidates.filter(q => q.provider === provider);
    const q = found.length === 1 ? found[0] : null;
    if (!q || q.status !== "ok") return { provider, status: "unavailable", error: q && q.error || "source_unavailable" };
    try {
      if (integer(q.amount_out_wei) <= 0n || q.amount_in_wei !== String(input(spec))
          || !q.source || q.source.provider !== provider || q.source.chain_id !== spec.chainId
          || q.source.token_in !== spec.token || q.source.token_out !== ETH
          || q.source.endpoint !== endpoint(spec, provider)
          || !/^[a-f0-9]{64}$/.test(q.source.response_sha256)) throw Error("quote_provenance_mismatch");
    } catch { return { provider, status: "unavailable", error: "quote_provenance_mismatch" }; }
    if (!time(q.observedAt) || !time(q.fetchedAt) || q.observedAt > q.fetchedAt || q.fetchedAt > now
        || now - q.observedAt > SOURCE_MAX_AGE) return { provider, status: "unavailable", error: "source_stale_or_future" };
    return { ...q, amount: decimal(integer(q.amount_out_wei)), observed_at: iso(q.observedAt), fetched_at: iso(q.fetchedAt) };
  });
  const latest = Math.max(0, ...compared.filter(q => q.status === "ok").map(q => q.observedAt));
  for (let i = 0; i < compared.length; i++) if (compared[i].status === "ok" && latest - compared[i].observedAt > COMPARISON_WINDOW)
    compared[i] = { provider: compared[i].provider, status: "unavailable", error: "comparison_time_skew" };
  const eligible = compared.filter(q => q.status === "ok").sort((a, b) => integer(a.amount_out_wei) > integer(b.amount_out_wei) ? -1 : integer(a.amount_out_wei) < integer(b.amount_out_wei) ? 1 : 0);
  const best = eligible[0];
  const out = { schema_version: 1, status: best ? "ok" : "unavailable", kind: "fixed_input_swap_quote",
    base: spec.base, quote: "ETH", base_amount: spec.size, amount: best ? best.amount : null,
    amount_in_wei: String(input(spec)), amount_out_wei: best ? best.amount_out_wei : null,
    unit_rate: best ? decimal(integer(best.amount_out_wei) / BigInt(spec.size)) : null,
    chain: spec.chain, chain_id: spec.chainId, observed_at: best ? best.observed_at : null,
    fetched_at: best ? best.fetched_at : null, generated_at: iso(now),
    valid_until: best ? iso(best.observedAt + MAX_AGE) : null,
    max_age_seconds: MAX_AGE, max_source_age_at_generation_seconds: SOURCE_MAX_AGE,
    comparison_window_seconds: COMPARISON_WINDOW, selection: "highest_eth_output_after_aggregator_fee_before_gas",
    indicative: true, executable: false, gas_included: false, source: best ? best.source : null,
    coverage: { available: eligible.length, expected: PROVIDERS.length, partial: eligible.length < PROVIDERS.length },
    quotes: compared.map(({ observedAt, fetchedAt, ...q }) => q) };
  if (!best) out.error = "no_fresh_aggregate_quote";
  return out;
}

async function collectSwapRates(cfg, fetchFn = fetch, options = {}) {
  const now = options.now || stamp;
  const collected = Object.fromEntries(SPECS.map(s => [s.id, []]));
  try {
    await bounded(async signal => {
      const json = (url, init, ms = 8000) => bounded(async requestSignal => {
        const r = await fetchFn(url, { ...init, signal: requestSignal, headers: { "content-type": "application/json" } });
        if (!r.ok) throw Error(`http_${r.status}`);
        return r.json();
      }, ms, signal);
      const blockAt = async (spec, number) => {
        if (!Number.isSafeInteger(number) || number <= 0) throw Error("invalid_source_block");
        const rpcUrl = spec.chainId === 1 ? cfg.rpcs[0] : cfg.base.rpcs[0];
        const body = await json(rpcUrl, { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: 1,
          method: "eth_getBlockByNumber", params: ["0x" + number.toString(16), false] }) }, 5000);
        const b = body && !body.error && body.id === 1 && body.result;
        if (!b || !/^0x[0-9a-f]+$/i.test(b.number) || BigInt(b.number) !== BigInt(number)
            || !/^0x[0-9a-f]{64}$/i.test(b.hash) || /^0x0{64}$/i.test(b.hash)
            || !/^0x[0-9a-f]+$/i.test(b.timestamp)) throw Error("invalid_source_block");
        return { number: String(number), hash: b.hash, timestamp: Number(BigInt(b.timestamp)), rpcUrl };
      };
      // Two independent providers; requests to each provider remain serial.
      await Promise.all(PROVIDERS.map(async provider => {
        for (const spec of SPECS) {
          try {
            const body = await json(endpoint(spec, provider));
            const fetchedAt = now();
            const block = provider === "velora" ? await blockAt(spec, body.priceRoute && body.priceRoute.blockNumber) : null;
            collected[spec.id].push(normalize(spec, provider, body, fetchedAt, block));
          } catch (e) {
            const code = e && typeof e.message === "string" && /^http_[0-9]{3}$/.test(e.message) ? e.message
              : e && typeof e.message === "string" && /^[a-z_]{1,64}$/.test(e.message) ? e.message : "quote_unavailable";
            collected[spec.id].push({ provider, status: "unavailable", error: code });
          }
        }
      }));
    }, options.timeoutMs || 30000, options.signal);
  } catch { /* A total timeout cannot leave a successful old file or late write. */ }
  return Object.fromEntries(SPECS.map(s => [s.id, buildSwapRate(s, collected[s.id], now())]));
}

function writeSwapRates(directory, rates) {
  const errors = [];
  for (const spec of SPECS) {
    try { writeJson(path.join(directory, spec.file), rates[spec.id] || buildSwapRate(spec, [])); }
    catch (e) { errors.push(e); }
  }
  if (errors.length) throw new AggregateError(errors, "swap rate publication failed");
}

module.exports = { SPECS, normalize, buildSwapRate, collectSwapRates, writeSwapRates };

if (require.main === module) {
  const cfg = require("../config.json");
  collectSwapRates(cfg).then(rates => {
    if (!process.argv.includes("--dry-run")) writeSwapRates(path.join(__dirname, "../data"), rates);
    console.log(JSON.stringify(rates, null, 2));
    if (Object.values(rates).some(q => q.status !== "ok")) process.exitCode = 2;
  }).catch(e => { console.error(e.message); process.exitCode = 1; });
}
