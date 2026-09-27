# LST Peg Monitor

stETH（含 wstETH）与 cbETH 的脱锚监测看板：https://attm-agenticcoding.github.io/lst-peg-monitor/

**兑付锚读代币合约；市价取流动性最深的场所；至少两个场所同时越档才升级状态。**

## 架构

| 部分 | 做什么 |
|---|---|
| `core.js` | 取数 + 计算，浏览器和 Actions 共用同一份（旧版是 Python 与 JS 两套实现，要手动对齐） |
| `app.js` / `index.html` | 页面每 90 秒用 `core.js` 直读一次；读 `data/history.json` 算常态、画 7 天走势 |
| `scripts/snapshot.js` | 取一份快照，追加进 `data/history.json`（schema 2），重算常态 |
| `scripts/loop.sh` + `.github/workflows/snapshot.yml` | 一个 run 在 runner 上循环约 5.5 小时、每 10 分钟一条快照，结束前用 `workflow_dispatch` 接力下一个 run；cron 每小时只当看门狗 |

为什么不用 cron 定时：GitHub 的 schedule 是尽力而为，拥堵时直接丢弃。旧版「每 30 分钟」的 cron 实测 2–5.5 小时才跑一次（中位约 4 小时），这正是巡检误报「快照停更」的原因。
`GITHUB_TOKEN` 触发的 `workflow_dispatch` 会真的起新 run，所以接力链不需要任何私钥；公共仓库的 Actions 分钟数不计费。

```bash
node tests/core.test.js            # 单测
node scripts/snapshot.js --dry-run # 取一份快照看看（需要能连外网）
python3 -m http.server 8080        # 本地预览（前端要读 config.json，必须起 http）
gh workflow run snapshot.yml       # 手动拉起接力链（看门狗也会自动拉）
```

## 口径

**脱锚 = 卖出执行价 ÷ 兑付锚 − 1**，bps，负数 = 折价。用卖出价：看的是现在真要跑能拿回多少 ETH。

- **stETH**：锚 1.0（Lido 提现队列 1:1）。wstETH 与 stETH 可按 `stEthPerToken()` 原子互换，不单列 —— wstETH 场所卖出等值 wstETH，折成 stETH 口径一起比。
- **cbETH**：锚 = `exchangeRate()`，Coinbase 每天 16:00 UTC 写一次（日内约 0.8 bps 锯齿）；无链上赎回。

| 场所 | stETH | cbETH |
|---|---|---|
| 聚合路由 | KyberSwap（扫全部 DEX，含 Lido ARM、Fluid，按需 wrap wstETH ≈ LlamaSwap 的报价） | KyberSwap |
| 链上池 | Curve stETH/ETH、Curve stETH-ng、Uniswap v3 wstETH 0.01% | Uniswap v3 cbETH 0.05% |
| 交易所盘口 | OKX STETH-ETH（按买盘逐档吃单） | Coinbase CBETH-ETH |

每个场所模拟卖出 1 / 10 / 100 / 1000 枚：

1. **可退出规模** = 最优场所在 50 bps 内还卖得掉的最大量级。
2. **主报价** = 在这个量级上执行价最好的场所（流动性最深），取它卖 1 枚的价。离中位数超过 75 bps 的源不能当主报价。
3. **状态** = 至少两个场所同时达到的那一档（第二差的场所）：正常 ≤25 / 关注 25–75 / 警戒 75–200 / 危机 >200 bps；最优场所卖 10 枚差于 −100 bps 至少警戒；可用场所 <2 个为「源不足」。
4. **常态** = 主报价近 30 天：先每小时取中位数，再取中位数（常态）与 p10~p90（常见区间）。满 12 小时出数。

`data/history.json`：`{schema:2, updated, baseline:{stETH,cbETH}, records:[{ts, stETH:{st,peg,via,clean,x,v}, cbETH:{…}, nav, err?}]}`。近 3 天全留，更早每小时一条，保留 60 天。旧版三币口径的历史归档在 `data/legacy/`。

## 实盘验证过的坑

- **KyberSwap 怕并发**：一次并发 8 个请求会回 503 overloaded，所以逐个发、失败隔 0.8 秒重试一次。
- **Curve coin 顺序不写死**：每轮 `coins(0)` 实时验证，顺序反了会静默返回倒数。
- **公开 RPC 批量不能大**：4 条一批；`ethereum-rpc.publicnode.com`、`1rpc.io/eth` 浏览器可直连。
- **cbETH 链上常年比 Coinbase 低约 10 bps**：Uniswap 1 枚约 −13 bps，Coinbase 盘口约 −2 bps；100 枚时链上 −40 到 −470 bps，Coinbase 约 −13。所以 cbETH 的主报价通常是 Coinbase。
- **已去掉的来源**：Chainlink stETH/ETH（0.5% 偏离阈值 + 24h 心跳，比 25 bps 关注线还钝）；DefiLlama / CoinGecko 聚合价（USD 口径二次换算，噪声大一个量级）；Balancer（2025-11 v2 可组合稳定池被利用，正在关停）。
- **Claude Artifact 不能当线上看板**：CSP 禁止向外部 host 发请求，所以走 GitHub Pages。

---

*阈值由我自己设定，这不是投资建议。*
