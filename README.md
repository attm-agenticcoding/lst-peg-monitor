# LST Peg Monitor

stETH / wstETH / cbETH 的脱锚监测看板。**兑付锚直接读代币合约，市价由多个互相独立的场所交叉确认** —— 不押注任何单一 DEX 的价格。

架构沿用 `gold-regime-dashboard`：GitHub Pages 静态站 + keyless API，前端在浏览器里直接取数；GitHub Actions 每 30 分钟跑一次 `scripts/snapshot.py`，把快照追加进 `data/history.json`。

---

## 上线（只需一次）

```bash
cd ~/agentic-coding/Fable5/lst-peg-monitor
gh repo create attm-agenticcoding/lst-peg-monitor --public --source=. --remote=origin --push
gh api -X POST repos/attm-agenticcoding/lst-peg-monitor/pages \
  -f 'source[branch]=main' -f 'source[path]=/'
```

之后是 `https://attm-agenticcoding.github.io/lst-peg-monitor/`。
Actions 第一次可以手动触发一下：`gh workflow run snapshot.yml`。

本地预览（前端要读 `config.json`，必须起 http，不能直接双击 html）：

```bash
python3 -m http.server 8080   # 然后开 http://localhost:8080
node tests/engine.test.js     # 引擎单测
python3 scripts/snapshot.py --dry-run
```

---

## 口径

### 兑付锚（NAV）—— 「背后到底能 redeem 多少 ETH」

| 代币 | 锚 | 来源 | 注意 |
|---|---|---|---|
| stETH | 固定 `1.0` | 协议 1:1 赎回 | 提现队列有 `maxShareRate` 封顶，极端亏损下可低于 1:1（历史未发生）；排队期间收益销毁 |
| wstETH | `stEthPerToken()` | wstETH 合约，链上实时 | 与 `tokensPerStEth()` 各自取整，别假设两者相乘等于 1e36 |
| cbETH | `exchangeRate()` | cbETH 合约 | Coinbase 预言机**每 24h（16:00 UTC）**写一次，是阶梯函数；**无链上赎回**，只能在 Coinbase 场内兑付 |

脱锚 = 市价 ÷ 兑付锚 − 1，单位 bps。

### 市价的多源交叉确认

每个代币同时取四类来源，逐个算 bps，再做稳健汇总：

| 类型 | stETH | wstETH | cbETH |
|---|---|---|---|
| 链上池（可执行报价） | Curve stETH/ETH、Curve stETH-ng | Uniswap v3 0.01% | Uniswap v3 0.05% |
| 预言机 | Chainlink stETH/ETH | ←× stEthPerToken 换算 | Chainlink cbETH/ETH |
| 交易所盘口 | OKX STETH-ETH | ←× 换算 | Coinbase CBETH-ETH |
| 聚合器 | DefiLlama、CoinGecko | 同左 | 同左 |

汇总规则：

1. **中位数**作共识，不用均值（均值会被一个离群源拖走）。
2. **MAD 判离群**：`|x − median| > max(3×1.4826×MAD, 20bps)` 的源标 outlier，剔出共识但仍显示。
3. 剔完不足 2 个就**回退到未剔除集合** —— 宁可显示分歧，也不造一个假共识。
4. **源间分歧** = 参与共识的源之间的极差。分歧拉大通常先于真正的脱锚。
5. 预言机超过心跳（默认 26h）未更新则标 stale，不参与共识。

### 退出折价曲线 / 可退出规模

中间价好看不代表跑得掉。在**单个链上池子**里模拟卖出 1 / 10 / 100 / 1000 枚（Curve `get_dy`、Uniswap `QuoterV2`），换算成相对兑付锚的 bps。
「可退出规模」= 还能在 50 bps 以内成交的最大档位。

⚠️ 这是单池报价，不是跨场所聚合路由。cbETH 的 Uniswap 池偏薄，100 枚以上的数字会很难看 —— 那是池深度的事实，不是脱锚。

### 状态阈值（改 `config.json`）

| 状态 | 条件 |
|---|---|
| 正常 | \|共识脱锚\| ≤ 25 bps |
| 关注 | 25–75 bps，或源间分歧 > 40 bps |
| 警戒 | 75–200 bps，或卖出 10 枚的执行价差于 −100 bps |
| 危机 | > 200 bps |

---

## 实盘验证过的坑

- **Curve coin 顺序不写死**：每轮实时 `coins(0)` 验证。顺序反了会静默返回倒数，看起来只像几个 bps 的误差。（实测两个池都是 `coins(0) = ETH`。）
- **主网 Chainlink 没有兑换率喂价**：`wstETH/stETH`、`cbETH/ETH` 的 Exchange Rate feed 只在 L2。网上广泛流传的 `0xB1552C5e...` 是 Arbitrum 的地址，主网上那是个零交易的空 EOA。所以兑付锚一律读代币合约，预言机只用在市价那一腿 —— 两腿来源独立反而是对的。
- **公开 RPC 批量请求不能太大**：13 条一批直接断连，4 条一批稳。所以 `eth_call` 按 `rpcBatchSize` 小批发，单批失败会退化成单发。
- **CoinGecko keyless 档每次只收 1 个合约地址**，多传会返回 10012 错误码，所以分三次拿。
- **可用的公开 RPC**：`ethereum-rpc.publicnode.com`、`1rpc.io/eth` 实测浏览器可直连；`eth.llamarpc.com` 无 CORS，`rpc.ankr.com` 要 key，`cloudflare-eth.com` 返回 -32046。
- **Balancer 已剔除**：2025-11 v2 可组合稳定池被利用约 1.28 亿美元（受影响的正是含 wstETH/osETH 的池），DAO 在走关停流程，v2 池预计 2026-11 起只读。
- **Claude Artifact 不能当线上看板**：其 CSP 禁止页面向外部 host 发 fetch/XHR，所以必须走 GitHub Pages。

---

## 加新代币

在 `config.json` 里加地址与 rate 方法，再在 `app.js` / `scripts/snapshot.py` 的 `buildTokens` / `collect` 里照抄一段即可。已核实过的候选：

| 代币 | 地址 | 兑换率方法 |
|---|---|---|
| rETH | `0xae78736Cd615f374D3085123A210448E74Fc6393` | `getExchangeRate()` `0xe6aa216c` |
| weETH | `0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee` | `getRate()` `0x679aefce`（注意是 weETH→eETH，不是直接对 ETH） |
| mETH | `0xd5F7838F5C461fefF7FE49ea5ebaF7728bB0ADfa` | `mETHToETH(uint256)` `0x5890c11c`，在 Staking 合约上；Mantle 源码里这两个函数的注释是反的，信函数名别信注释 |

---

*阈值与来源权重由我自己设定，这不是投资建议。*
