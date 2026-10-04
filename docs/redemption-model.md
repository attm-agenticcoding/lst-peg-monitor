# stETH 赎回测算：实现口径与未完成的验证

## 当前状态

这是成本/等待**情景测算**，不是已经验证的独立 ETA 模型。页面不会给出可执行套利指令，也没有新增 ntfy 通知或自动交易。官方时间仅作对照；手动等待和加倍等待都不叫 p50/p90。是否合并、部署与启用未来动作提示需要单独决定。

已有公开 Dune 查询给出的队列总量、最老等待与过去 7 天完成等待均值，不能预测一笔新申请的完成时间。2026-10-03 调研中这些 Dune 结果约 16 小时未更新。这个年龄不代表 Lido API 数据年龄：官方估计器并不使用 Dune 作为计算输入。

## 算账

- E：这笔交易投入的 ETH
- S：按 E 真实询价得到的 stETH，已含池费和数量对应的价格冲击
- G：swap + stETH 授权 + request + claim 的 gas 预算，以 ETH 计
- F：报价/gas 之外的其他成本，默认 0，可在页面设置
- C = E + G + F：全部占用资本
- V = S：名义兑付；压力情景先扣额外成交滑点，再扣兑付折损
- 净利润 = V − C；净回报 = 净利润 / C
- 简单年化 APR = 净回报 × 365 / 全周期天数
- 全周期 = 提款等待参考/手动假设 + 申请/领取额外延迟
- 同周期质押收益 = C × stETH staking APR × 全周期天数 / 365

不向 queued stETH 加质押收益。Lido APR 接口是 APR，不标成 APY。没有展示“同样机会立刻重复”假设下的复利 APY。

stETH 名义 1:1 不是绝对兑付保证；实际 finalization 受 shares、finalization share rate、亏损/罚没和取整限制。保守情景可加 haircut，并且未来 claim gas 不可预知。Gas 是预算，不是钱包签名前的 eth_estimateGas。默认假设需要一次 stETH 授权，金额超过 1000 stETH 时按申请数量增加 request/claim 预算；批量操作的实际 gas 可能不同。全额回收要等最后一部分可领取。

## 时间与数据质量

每个买入金额分别获取报价，按**买到的 stETH 数量**请求官方 V2 calculate，不用 ETH 投入数量，也不复用最大一档 ETA。当前买入路由返回 stETH；如果未来直接添加 wstETH 输出，必须先按当前链上转换率换算为 stETH，不能把 wstETH 数量直接送入队列估计。

- `quoteAt`：Curve RPC 报价观察时刻；Kyber 有 route timestamp 时优先用它，否则只是读取时刻
- `gasAt`、`aprAt`：读取时刻，不宣称底层经济指标在这一刻重新计算
- `eta.fetchedAt`：本次响应读取时刻
- `eta.calculatedAt`：由官方 `finalizationAt − finalizationIn` 反推的响应计算时刻；**不证明内部 validator snapshot 新鲜**
- `nextCalculationAt`：下一次 queue job 计划时间，绝不能当作上次更新时间
- 官方 V2 没有暴露其使用的 source block/validator snapshot 时间，因此页面明确显示内部数据时效未知
- 独立 RPC 队列观察使用自己的 pinned block，不伪装成官方 API 使用的 block

只接受 `status=calculated`、已知 calculation type、正数毫秒等待和有效时间字段。拒绝 missing/invalid/stale ETA；403/其他失败后本轮不继续其他金额请求，不走替代路径绕过 WAF。缺成本/时间、过期、暂停/Bunker、手动时间都不形成可执行信号。即使资料齐全且情景门槛通过，也只显示“情景达标 · 待校准”，`actionable` 始终为 false。

`data/history.json` 和既有逐条 archive 在部署后会随原有采样保存通用金额的报价、gas、官方参考响应与独立队列观察。无需新调度。旧 `rt` 历史字段仍保留 gross 折价口径；新增 `redemption.version=1` 保存新口径，不能把两个字段混成同一历史序列。浏览器自定义参数不写入仓库/服务端；刷新页面即消失。报价数量会发送给相应公开数据服务，无需钱包、账户或余额。

## 独立 ETA 的下一步

需要真正的 request → finalization 标签与预测时可知的状态，不能拿“最老请求 55 小时”或“最近中位 19 小时”当作新申请 ETA。

1. 从 WithdrawalQueueERC721 事件重建 `WithdrawalRequested` 和 `WithdrawalsFinalized`，用区块时间戳作时间标签；finalization ID 范围为 **[from,to] 包含两端**，不要使用实际 claim 时间
2. 保留仍 pending 的请求，标记 right-censored；不能只保留快完成的样本
3. 重建每个 request 当时排在它前方的 ETH 数量，含自身最后一部分；考虑同块 log 顺序
4. 在预测时刻 pinned block 读取 withdrawal-eligible reserve、vaults、report/cutoff、暂停/Bunker；SR3 之后不能把 deposits reserve 当可提现 buffer
5. 记录当时已知的退出 validators、balance-based churn、withdrawal delay、sweep 位置与未来 oracle report frame；不能泄漏事后新增的存款/退出信息
6. 先建机械资金/队列模型，再用历史残差或生存/分位模型校准；按时间切分，按 finalization batch 隔离，训练只用当时已完成标签
7. 报告误差偏向、MAE、低估尾部、覆盖率，以及按金额、拥堵、是否需额外退出、协议版本分组表现；覆盖率验证通过前不称 p90
8. 前瞻保存官方预测，才可公平比较真实官方历史表现。官方当前 API 不提供过去某时刻的估计，回放源码只能叫 reconstructed baseline

2026-10-03 的有限免费 RPC 试取只拿到约 1.4 天内的 129 条申请与 2 批 finalization，两批覆盖更早的 IDs，样本内 129 条尚未完成。更老 getLogs 返回 pruned-history，历史 eth_call 不开放。因此当前没有足够配对标签或 request-time reserve 数据，**没有训练模型，也没有证明准确率提高**。可行下一步是允许历史导出的数据源/归档 RPC，或在现有采样中前瞻收集足够完整的 finalization 周期；不擅自买查询额度或创建密钥。

## 可复核来源

- [Lido API 定义](https://docs.lido.fi/integrations/api/#withdrawals-api)
- [V2 DTO：毫秒与状态](https://github.com/lidofinance/withdrawals-api/blob/0.29.0/src/http/request-time/dto/request-time-v2.dto.ts)
- [估计器原理](https://github.com/lidofinance/withdrawals-api/blob/0.29.0/how-estimation-works.md)
- [官方源状态 cache](https://github.com/lidofinance/withdrawals-api/blob/0.29.0/src/waiting-time/block-state-cache.service.ts)
- [validator 刷新任务](https://github.com/lidofinance/withdrawals-api/blob/0.29.0/src/jobs/validators/validators.service.ts)
- [官方页面 waiting-time hook](https://github.com/lidofinance/ethereum-staking-widget/blob/develop/features/withdrawals/hooks/useWaitingTime.ts)
- [队列合约与金额限制](https://docs.lido.fi/contracts/withdrawal-queue-erc721/)
- [主网合约地址](https://docs.lido.fi/deployed-contracts/)
- [finalization inclusive bounds 实现](https://github.com/lidofinance/core/blob/v4.0.0/contracts/0.8.9/WithdrawalQueueBase.sol)
- [validator exit 机制](https://docs.lido.fi/guides/oracle-spec/validator-exit-bus/)
- [Kyber routeSummary 字段](https://api-docs.kyberswap.com/aggregator-api.html)

文档是代码/方法依据，不是生产 API 当前版本、配置或数据新鲜度的保证。
