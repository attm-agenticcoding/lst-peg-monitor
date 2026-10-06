# Lido 六档批次情景：来源、时间与收益门禁

## 当前公开快照

- 执行区块：26120128，2026-10-04 16:16:23 UTC
- 执行块哈希：`0x2e16104d47adde9a36610c72cf57b6eea262636f2a155aa534ee44be0fe15631`
- Beacon slot：15358880；完整 SSZ state root：`0xa4797903dfc2a2f9e03d808729007afe66378c465ae8ff34cd491e1b3bff5183`
- SSZ SHA-256：`f4c691ecda333085a3f1019b500028ab34ba23c00825fe57d1bf3c911e120987`
- 同一时点：479 待处理请求，147365.696918335063465874 stETH；core 与两座 vault 实际现金合计 3586.962265060984543582 ETH
- 当前队列 getter 与事件总和、core 事件回放与同块物理余额差均为 0 wei

SSZ hash-tree-root 独立重算，与相同执行块哈希一致，并以紧接执行头的 EIP-4788 parentBeaconBlockRoot 锚定。未本地验证 BLS finality。公共供应商日志不是独立 canonical receipt proofs。配置/模式的部分 Etherscan 读数为 latest/unpinned，不能当作同一固定块证明。

机器可读数据：[lido-cutoff-snapshot.json](../data/lido-cutoff-snapshot.json)。保留原始金额精度和用于构建本小型公开快照的四个源文件 SHA-256；页面显示金额四舍五入到四位。没有钱包、私人持仓或账户资料。

## 场景含义

六档 100 / 200 / 300 / 500 / 1000 / 1500 stETH 是在固定快照时点、同一队尾位置的六种独立假设。1500 拆成 1000 + 500；完整完成取最后一笔，拆单可以同一批完成。

主场景模拟延续已有 legacy 部分提款负载；静止余额场景也得到相同批次。压力场景额外每块预留 8 个部分提款槽位。使用全网 registry 顺序、现有 pending deposits/consolidations/partial withdrawals，区分 0x01/0x02 上限、每块 16 笔提款/16384 个 validator 扫描、pending-partial 优先和 epoch 边界转移。311492.271058206 ETH 合并来源余额不被直接计作现金。

两场景额外要求：

1. 未来建模到账的现金被相应报告全部接纳，Accounting/rebase/share/internal-supply/consensus 检查继续通过
2. 原队列名义无折损兑付、无现金分流、不额外计入未知未来 staking/EL 收入
3. 正常每日报告、无暂停，健康 finality，每个 12 秒 slot 有 execution block
4. 不预测新增未知退出、未来存入/奖励、罚没、漏块、finality 改变、新部分提款负载或模式变更

在这些条件下，主场景六档均落在 2026-10-07 12:00:11 UTC 第 3 份参考报告；压力为 2026-10-09 12:00:11 UTC 第 5 份。参考时点不是发布时刻，更不是实际领取时刻。它们不是概率区间、最早/最晚保证或已观察到的未来事实。

| 2026 年参考日期，12:00:11 UTC | 主场景新增名义 stETH cutoff | 压力场景 cutoff |
|---|---:|---:|
| 10-05 | 0 | 0 |
| 10-06 | 0 | 0 |
| 10-07 | 7541.174114849921077708 | 0 |
| 10-08 | 36803.194551070921077708 | 0 |
| 10-09 | 41848.048846979921077708 | 9584.582771778921077708 |

cutoff 是原队列之后可全额覆盖的新增名义申请量，不是每天处理总量。页内不是重新实现预测引擎，也不自动外推新增档位。

## 收益与时间一致性

批次情景的全周期 = 固定快照至对应参考报告的秒数 / 86400 + 页面“发布/申请/领取额外延迟”小时 / 24。主场景原间隔 243828 秒，压力 416628 秒。额外延迟默认为 2 小时，纯假设，绝不是发布时间保证。页面时钟只改变快照年龄，不能让原等待周期自动变短。

只有同时满足以下条件才把金额专属买入报价用于该档收益：

- 快照不超过 300 秒，且不在未来；这是保守数据兼容性门禁，不是统计有效期
- 当前队列 blockNumber 和 blockTimestamp 与快照相同，且 paused/bunker 均为 false
- 报价实际得到的 stETH 与该档一致（浮点表示容差 1e-9 stETH），不把投入 ETH 当作同数值 stETH，不线性放大另一个数量的报价
- 报价时间与快照相距不超过 300 秒；quote、gas 和 APR 通过现有时效检查；页面本轮取数没有失败
- 成本和延迟参数有效

否则对应利润/APR/超额收益为 null，页面显示破折号和具体原因。当前公开快照为固定历史资料，正常会显示过期且收益留空。不会假装滚动更新。真实刷新需要重新取得并验证队列/共识状态和机制场景，再提交新快照。

净利润 = 兑付 ETH − 买入投入 − gas − 额外成本；净回报分母为所有投入；净简单 APR = 净回报 × 365 / 全周期天数。质押机会成本用同样全部投入、同样资金周期与所读取 APR。压力情景另计页面 gas 倍数、滑点、兑付折损。当前手续费/价格冲击已含在金额专属报价内。赎回、领取、授权与 swap gas 用实际请求拆单预算。

原实时 stETH 买入情景继续独立刷新，等待来自官方未经验证参考或用户手动输入。它不表示此固定批次模型已刷新。所有路线 `actionable=false`，无新交易、钱包访问、密钥、持久权限或通知。

## 证据与验证边界

底层研究通过 18 项 FIFO 单元测试、8 项共识规则测试、291 项集成断言，以及独立 Python 实现对主场景前 20,319 个区块的累计现金、指针、registry 长度和提款数复核。这是机制一致性验证，**不是预测准确率或样本外校准**。

公开来源：

- [执行快照区块](https://etherscan.io/block/26120128)
- [Lido WithdrawalQueue Read as Proxy](https://etherscan.io/address/0x889edC2eDab5f40e902b864aD4d7AdE8E412F9B1#readProxyContract)
- [Lido Read as Proxy](https://etherscan.io/address/0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84#readProxyContract)
- [AWS Public Blockchain](https://registry.opendata.aws/aws-public-blockchain/) 2026-09-30 至 10-03 公开日志分区，以及近期 finalized headers/logs/receipts/balances
- [Ethereum consensus-specs v1.6.0 Electra](https://github.com/ethereum/consensus-specs/blob/v1.6.0/specs/electra/beacon-chain.md) 及同版本 Fulu/Capella/Phase0
- [Lido core v4.0.1](https://github.com/lidofinance/core/commit/2da0f48f1a2a103a394dcf8760810fe9165697fb)、[oracle 8.1.0](https://github.com/lidofinance/lido-oracle/commit/032c228c767759e67da43e6c40fa81732257879d)

网页集成测试另验证固定日期、精度、拆单、时点/数量门禁、失效状态、利润与同周期质押公式、刷新不篡改时间以及无可执行提示。无新依赖。

## 每日刷新入口

以上数值保留为 2026-10-04 历史回归样本；当前公开 JSON 可由每日 00:00 UTC 任务更新。执行入口及原子发布门禁见 [每日更新操作说明](lido-daily-refresh.md)。只有成功取得并校验新的同块执行/共识数据、完整重放队列并完成两种机制模拟后才推进来源时点和批次。失败只更新错误状态，保留旧情景；显式单次更新沿用同一门禁。未来日期仍为条件情景，并未完成样本外预测校准。
