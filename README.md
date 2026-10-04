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
node tests/lido-cutoff.test.js     # 固定批次情景、时点兼容性、净收益门禁
node tests/core.test.js            # 原有 peg/guard 逻辑
node tests/redemption.test.js      # 净收益/等待/质量门槛
node tests/collect-redemption.test.js # mock API 集成，无外部调用
node tests/app-redemption.test.js   # 页面控制器 smoke（不能替代视觉 QA）
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
- 平时卖这个量就要亏超过 25 bps（关注线）的场所不算这个量级的出口，它们的抖动不算确认。2026-09-29 08:01 ET 主网聚合路由卖 100 cbETH 从常态 −33 跳到 −183 bps，加上 Coinbase 盘口变薄 3 bps，凑成两个场所误报了一次，而 Base 上的出口当时是 +0.2 bps。
- 同一天 07:54 ET 那次 stETH 预警是真的：Curve / Uniswap 池子从 −2 扩到 −4.9 bps，Uniswap wstETH 池卖 1000 枚直接吃穿；最优路由（Lido ARM）撑到 09:46 才跟着降到 −3.8。预警比最优出口变差早了约 2 小时。
- 绝对档位 25 / 75 / 200 bps 照旧，兜住慢慢漂下去的偏离。

## 预警规则每日体检（`scripts/review.js`）

规则是在只有几天数据时定的，每天用新数据复盘一次，攒够证据再改。只读，不改任何文件：`node scripts/review.js`（`--json` 出 JSON，`--hours N` 改复盘窗口）。

1. **逐条存档**：后台每次采样都追加进 `data/archive/<UTC 日期>.jsonl`，隔天压成 `.jsonl.gz`（约 60 KB/天）。`history.json` 只保留 1 天的逐条数据，回测更长时间要靠存档。每条记录带 `rv`（当时生效的规则版本，`config.guard.version`）。
2. **事后标注**：「最优出口」（同一时刻各场所里最好的价）比它自己 24h 中位数差 ≥ D bps、且连续 3 次采样 = 一个事件。预警开始后 6 小时内出现 D bps 事件 = 真预警，否则 = 误报；事件开始前 6 小时到开始后 10 分钟内没有预警 = 漏报。D 分 3 / 5 / 10 bps 三档一起报 —— 多大的恶化才值得撤，是持仓人的决定。
3. **合成测试**：真正的大脱锚很少，漏报率在真实数据上测不出来，所以往真实背景里注入四种形态，每 2 小时一次：全市场 30 分钟缓跌 10 bps、全市场断崖 25 bps、只有大额变差 15 bps（流动性先撤）、单个场所故障 50 bps（不该报）。注入时长不超过 1 小时，基线排除最近 1 小时，所以注入不会污染基线。
4. **候选规则**：k ∈ {2.5, 3, 4} × 下限 ∈ {2, 3, 4, 5} bps × 确认场所 ∈ {2, 3} × 薄场所线 ∈ {25, 50} bps，在全部存档上算同样的账。只有「各项都不差于现行、且至少一项有实质改善（少 1 次误报 / 快 2 分钟 / 多检出）」、并且存档 ≥ 7 天，才会给「建议改」。另给一条「更少误报但可能更慢」的取舍参考。规则不会自动改：要改由人拍板，改完 `config.guard.version` 跟着换。

## Base 买入 → Coinbase 赎回（cbETH 卡片里的表）

在 Base 上用 25 / 50 / 75 / 100 ETH 买 cbETH，再到 Coinbase 按兑换率 unwrap，能拿回多少 ETH。

- 买入：KyberSwap 在 Base 上的聚合报价（ETH → cbETH）和 Aerodrome 单池报价，取买到 cbETH 更多的那个。Aerodrome 单池在买入方向很浅（实测 50 ETH 就吃穿），大额基本都靠聚合器走做市商和多个池子。
- 赎回：Coinbase 公开接口 `wrapped-assets/CBETH` 的 `conversion_rate`（与主网合约 `exchangeRate()` 一致；取不到时退回链上值）。unwrap 不收费。
- 同一个接口还给 `redeem_time_estimate_days`（赎回排队天数估计）和 `apy`。赎回得到的是 Coinbase 上的质押 ETH，要排以太坊退出队列才变成可用 ETH；表里的「折合年化」= 差额 ÷ 排队天数 × 365，用来和质押年化对比。
- cbETH 可以直接走 Base 网络充值到 Coinbase（2026-08-17 起 Coinbase 只保留 Ethereum 和 Base 两条网络）。Base 上的 gas 不到 1 美分，没有计入。

## Lido 六档批次情景（固定研究快照）

页面已接入 2026-10-04 16:16:23 UTC / 区块 26120128 的完整队列与认证 BeaconState 情景。100 / 200 / 300 / 500 / 1000 / 1500 stETH 分别是假设从相同队尾加入；不是个人仓位，也不是六笔累计。主情景均为 10 月 7 日参考报告，重负载压力为 10 月 9 日；参考时间为 12:00:11 UTC，发布和领取在后。这不是实时 ETA、概率区间或最坏上限。

独立卡片展示来源时间、快照年龄、拆单、每日 funding cutoff 和净收益/同周期质押对照。超过 5 分钟或买入实际 stETH 数量、队列块、quote/gas/APR 不兼容时，批次情景净收益留空；不会用当前时钟缩短原情景周期。刷新仅重新读取已发布的快照文件，不重新跑共识模型。原实时价格采样及预警不变。

数据和方法：[固定快照](data/lido-cutoff-snapshot.json)、[方法与验证边界](docs/lido-cutoff-scenarios.md)。18 FIFO 测试、8 共识测试、291 集成断言与 20,319 区块回放是机制验证，不是样本外预测验证。

## 买入 stETH → Lido 提现赎回（净收益情景）

通用 50 / 100 / 200 / 300 ETH 示例，或本页临时输入金额。按对应数量的 Kyber/Curve 买入报价，计入 swap、授权、申请与领取 gas 后，展示净利润、净回报、全周期简单年化 APR、保守等待/成本情景和同周期质押收益对照。

**独立等待模型尚未校准。** 官方 API 只作未经验证的对照；按真正买到的 stETH 数量分别查询。API 不可用/过期不会填一个假 ETA，手动等待不会触发可执行机会提示。情景通过门槛也标注“待校准”，不新增手机推送或自动交易。

默认 gas/延迟/滑点/门槛都是可见的假设，不是统计置信区间。Lido queued stETH 不再计收益，名义 1:1 兑付也可能因亏损/罚没下降。详见 [公式、时间戳、验证限制与独立模型路线](docs/redemption-model.md)。

## Coinbase 质押包装 → Base 卖出（反方向，cbETH 在 Base 上有溢价时看）

在 Coinbase 上用 5 / 10 / 25 / 50 / 100 / 300 ETH 质押并包装成 cbETH（不收费，得到 X ÷ 兑换率 枚），直接走 Base 网络提出来，在 Base 上卖掉，能拿回多少 ETH。

- 卖出：KyberSwap 在 Base 上的聚合报价和 Aerodrome 单池报价，取拿回 ETH 更多的那个。卖出数量要先知道兑换率，所以这组报价排在兑换率拿到之后。
- 不用排队，不折年化。提币网络费（Base 上几美分）没有计入。
- Coinbase 的帮助文档没写明刚质押的 ETH 是否马上能包装，包装功能按地区开放，文档还提到「交易偶尔会因为网络或流动性情况失败」。

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
