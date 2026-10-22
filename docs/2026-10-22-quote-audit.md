# 2026-10-22 cbETH quote and mobile scroll audit

Observation: 2026-10-22T11:49:40.000Z. Public API/RPC reads only; no wallet or trades. Local validation was used because this session has no dot cloud execution tool.

## Findings

The screenshot matches the Aerodrome tickSpacing=1 single-pool output, not the Base aggregate route. 100 ETH → about 17.7283 cbETH gives about 20.2481 ETH nominal redemption; −7975 bps is arithmetically consistent with that poor route. A direct RPC re-read reproduced it. The post-swap sqrt price was below the protocol maximum; no evidence established an incomplete fill. Do not describe this as a Coinbase redemption fee or Base-wide liquidity.

The browser previously issued all sell sizes and stETH buy sizes before cbETH buys and wrap/sells. Its 30-second deadline could cut off the latter while the UI already displayed single-pool returns. Requests now interleave categories one size per pass, strictly serial. Missing aggregate quotes leave that row’s returns blank. Row-specific quote times expire independently.

The 10-second UI expiry check replaced the entire scenario subtree. The fix patches existing DOM nodes; the scroll container survives timed checks and quote updates.

## Live verification

Corrected collector completed in 25.601 seconds, without source errors. Coinbase rate 1.1421381467180507, estimated wait 13.7 days. Mainnet exchangeRate independently returned 1.142138146718051. All numbers below are quotes, before gas and transfer/withdrawal costs, not guaranteed executions.

| Buy input ETH | cbETH quoted | Nominal redemption ETH | Spread bps | Simple APR |
|---:|---:|---:|---:|---:|
| 25 | 21.88691611 | 24.99788180 | -0.85 | -0.2257% |
| 50 | 43.77346372 | 49.99534273 | -0.93 | -0.2482% |
| 75 | 65.65988206 | 74.99265601 | -0.98 | -0.2609% |
| 100 | 87.54590259 | 99.98951493 | -1.05 | -0.2793% |

| Wrap input ETH | cbETH wrapped | Base sale ETH | Spread bps |
|---:|---:|---:|---:|
| 5 | 4.37775414 | 5.00002926 | 0.06 |
| 10 | 8.75550828 | 10.00006849 | 0.07 |
| 25 | 21.88877070 | 25.00004166 | 0.02 |
| 50 | 43.77754140 | 49.99975721 | -0.05 |
| 100 | 87.55508280 | 99.99829141 | -0.17 |
| 300 | 262.66524839 | 299.79926227 | -6.69 |

Formula checks: buy back = cbETH × exchangeRate; wrap amount = ETH / exchangeRate; bps = (back / input − 1) × 10000; simple APR = (back / input − 1) × 365 / days. The original numeric formulas were correct; the explanatory annualization formula omitted division by capital. APY and simple APR are now explicitly distinguished. No redemption fee of 7975 bps was found.

## Evidence and validation

- Immutable public observation: `tests/fixtures/cbeth-20261022-audit.json`.
- All dependency-free JavaScript regression suites passed, including amount conversion, redemption economics, collection ordering/cancellation and cache expiry.
- Mobile Chromium regression uses a 390 × 844 touch viewport, checks retained DOM identity and scroll offset across the 10-second tick and refresh, and missing/recovered aggregator states. This does not claim a physical iPhone Safari test.
- No collection schedules, alert thresholds, private services or credentials changed.
- Public sources: [Kyber API](https://api-docs.kyberswap.com/aggregator-api.html), [Coinbase wrapped asset endpoint](https://api.exchange.coinbase.com/wrapped-assets/CBETH), [Aerodrome Quoter deployed source](https://basescan.org/address/0x254cf9e1e6e233aa1ac962cb9b05b2cfeaae15b0).
