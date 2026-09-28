# LST Peg Monitor

stETH（含 wstETH）与 cbETH 的脱锚监测看板：https://attm-agenticcoding.github.io/lst-peg-monitor/

**兑付锚读代币合约；市价取流动性最深的场所；主报价越档、且至少还有一个场所确认才升级状态。**

## 架构

| 部分 | 做什么 |
|---|---|
| `core.js` | 取数 + 计算，浏览器和 Actions 共用同一份（旧版是 Python 与 JS 两套实现，要手动对齐） |
| `app.js` / `index.html` | 页面每 90 秒用 `core.js` 直读一次；读 `data/history.json` 算常态、画 7 天走势 |
| `scripts/snapshot.js` | 采样一次 → 判断撤离预警和档位变化 → 需要时推送到手机（ntfy）→ 追加进 `data/history.json` |
| `scripts/loop.sh` + `.github/workflows/snapshot.yml` | 一个 run 在 runner 上循环约 5.5 小时、每 2 分钟采样、每 10 分钟提交一次（有推送时立刻提交），结束前用 `workflow_dispatch` 接力下一个 run；cron 每小时只当看门狗 |

为什么不用 cron 定时：GitHub 的 schedule 是尽力而为，拥堵时直接丢弃。旧版「每 30 分钟」的 cron 实测 2–5.5 小时才跑一次（中位约 4 小时），这正是巡检误报「快照停更」的原因。
`GITHUB_TOKEN` 触发的 `workflow_dispatch` 会真的起新 run，所以接力链不需要任何私钥；公共仓库的 Actions 分钟数不计费。

```bash
node tests/core.test.js            # 单测
node scripts/snapshot.js --dry-run # 取一份快照看看（需要能连外网）
python3 -m http.server 8080        # 本地预览（前端要读 config.json，必须起 http）
gh workflow run snapshot.yml       # 手动拉起接力链（看门狗也会自动拉）
```

## 推送（ntfy）

预警要在采样的那台机器上当场发出，才能做到偏离出现后约 2 分钟内到手机；Claude 的定时任务最短一小时一次，只用来发现后台本身停了。

1. 手机装 ntfy（iOS / Android，免费、不用注册），订阅一个只有自己知道的频道名。
2. `gh secret set NTFY_TOPIC --body "<频道名>"`
3. 下一个 run 起来时会先推一条「LST 预警通道已接通」。没设这个 secret 时照常采样，只是不推送。

频道名等于密码：知道它的人能看到也能往里发，所以只放在 GitHub secret 里，不进仓库。

## 撤离预警

两个指标，各自对照每个场所**自己的**历史：卖 1 枚的价，卖大额（stETH 1000 枚、cbETH 100 枚）的执行价。

- 窗口 24h / 7 天 / 30 天，都排除最近 1 小时；7 天、30 天先每小时取一个点。
- 中心 = 中位数，尺度 = 1.4826×MAD。不用均值/标准差：报价离散跳档，平静期 σ 会塌到 ~0.1 bps（3σ 的线贴着现价，回测 19 小时里 77 个时点误报 8 次）；偶发溢价尖刺又会把 σ 撑大 7 倍。
- 预警线 = 中心 − max(3×尺度, 3 bps)，取三个窗口里最紧的一条；3 bps 下限 ≈ 撤离本身的成本（手续费 + gas + 大额滑点）。
- 同一次采样里 ≥ 2 个场所跌破各自的线才报；只看折价；连续 3 次采样回到线上才报恢复。
- 绝对档位 25 / 75 / 200 bps 照旧，兜住慢慢漂下去的偏离。

## 口径

**脱锚 = 卖出执行价 ÷ 兑付锚 − 1**，bps，负数 = 折价。用卖出价：看的是现在真要跑能拿回多少 ETH。

- **stETH**：锚 1.0（Lido 提现队列 1:1）。wstETH 与 stETH 可按 `stEthPerToken()` 原子互换，不单列 —— wstETH 场所卖出等值 wstETH，折成 stETH 口径一起比。
- **cbETH**：锚 = `exchangeRate()`，Coinbase 每天 16:00 UTC 写一次（日内约 0.8 bps 锯齿）；无链上赎回。

| 场所 | stETH | cbETH |
|---|---|---|
| 聚合路由 | KyberSwap 主网（扫全部 DEX，含 Lido ARM、Fluid，按需 wrap wstETH ≈ LlamaSwap 的报价） | KyberSwap **Base**、KyberSwap 主网 |
| 链上池（合约直读） | Curve stETH/ETH、Curve stETH-ng、Uniswap v3 wstETH 0.01% | Aerodrome Slipstream cbETH/WETH（**Base**）、Uniswap v3 cbETH 0.05%（主网） |
| 交易所盘口 | OKX STETH-ETH（按买盘逐档吃单） | Coinbase CBETH-ETH |

cbETH 的链上流动性主要在 Base（Coinbase 自家 L2）：Base 上卖 1000 枚约 −5 bps，主网池子卖 100 枚就要 −40 到 −470 bps。cbETH 的兑付锚仍然读主网合约的 `exchangeRate()`（Base 上的 cbETH 是桥过去的同一资产，合约里没有兑换率）。

每个场所模拟卖出 1 / 10 / 100 / 1000 枚：

1. **可退出规模** = 最优场所在 50 bps 内还卖得掉的最大量级。
2. **主报价** = 在「最优执行价还不差于 −200 bps 的最大量级」上执行价最好的场所（流动性最深），取它卖 1 枚的价。用 −200 而不是 50 bps 来比，是为了压力下深池跌到 −80 时，不会被一个只挂着小单、价格还没跟上的薄盘口顶替。离中位数超过 75 bps 的源不能当主报价。
3. **状态** = 主报价所在的档，但至少还要一个别的场所也到这一档才算（两者取较轻的一档）：正常 ≤25 / 关注 25–75 / 警戒 75–200 / 危机 >200 bps。薄池子自己漂（主网 cbETH 常年 −13 bps）、单个源报错都不会误报。最优场所卖 10 枚差于 −100 bps 至少警戒；可用场所 <2 个为「源不足」。
4. **常态** = 主报价近 30 天：先每小时取中位数，再取中位数（常态）与 p10~p90（常见区间）。满 12 小时出数。

`data/history.json`：`{schema:2, updated, push, alerts, guard, baseline, records:[{ts, stETH:{st,peg,via,clean,x,v,vb}, cbETH:{…}, nav, g?, err?}]}`。`v` / `vb` 是各场所卖 1 枚 / 卖大额的 bps，`g` 是当时处于预警中的指标。1 天内全留（2 分钟一条），1–3 天 10 分钟一条，更早每小时一条，保留 60 天。旧版三币口径的历史归档在 `data/legacy/`。

## 实盘验证过的坑

- **KyberSwap 怕并发**：一次并发 8 个请求会回 503 overloaded，所以逐个发、失败隔 0.8 秒重试一次。
- **Curve coin 顺序不写死**：每轮 `coins(0)` 实时验证，顺序反了会静默返回倒数。
- **公开 RPC 批量不能大**：4 条一批；`ethereum-rpc.publicnode.com`、`1rpc.io/eth` 浏览器可直连。
- **cbETH 看 Base，别看主网**：主网 Uniswap 卖 1 枚约 −13 bps，Base 上约 −1 到 −3 bps，Coinbase 盘口约 −2 bps。
- **池子手续费**：Curve stETH/ETH 原池 1 bp、stETH-ng 0.8 bp，Uniswap wstETH 池 1 bp，Aerodrome cbETH 池 0.6 bp —— 这部分付给 LP，已经含在执行价里。LlamaSwap 本身不加收费用（它的收入是聚合器给的分成）。
- **Aerodrome Slipstream 的 QuoterV2** 参数里是 `int24 tickSpacing` 而不是 `uint24 fee`，选择器是 `0x9e7defe6`，不是 Uniswap 的 `0xc6a5026a`。
- **已去掉的来源**：Chainlink stETH/ETH（0.5% 偏离阈值 + 24h 心跳，比 25 bps 关注线还钝）；DefiLlama / CoinGecko 聚合价（USD 口径二次换算，噪声大一个量级）；Balancer（2025-11 v2 可组合稳定池被利用，正在关停）。
- **Claude Artifact 不能当线上看板**：CSP 禁止向外部 host 发请求，所以走 GitHub Pages。

---

*阈值由我自己设定，这不是投资建议。*
