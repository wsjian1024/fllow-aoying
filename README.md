# Binance 跟单页面接口分析与 Python 拉取验证

已实现每分钟执行的企业微信群机器人变化通知脚本，配置和定时任务步骤见[通知说明](WECHAT_SETUP.md)，入口为[通知脚本](notify_wechat.py)。下面保留接口分析与验证证据。

验证日期：2026-11-14，时区 Asia/Shanghai。目标带单组合 `5075281354358777856`，详情接口返回昵称“熬鹰资本”。

已确认两个能够返回与用户截图一致数据的网页 BAPI 接口。本次请求无需 Cookie、登录、API Key 或签名。使用 Python 标准库完成真实 HTTP 拉取，操作记录去重后 41 条，仓位历史 108 条。

证据范围：普通 HTML 请求返回 HTTP 202 AWS WAF 验证页，独立 Chrome 的页面调试初始化也未成功，所以没有取得页面真实 XHR 抓包或前端源码。以下结论来自对 JSON 接口的直接请求、返回字段及截图逐项对照；不能描述为 Binance 官方文档保证的 API 契约。

- [用户提供的详情页](https://www.binance.com/zh-CN/copy-trading/lead-details/5075281354358777856?timeRange=30D&isSmartFilter=true)
- [Python 拉取脚本](binance_copytrade.py)
- [离线回归测试](test_binance_copytrade.py)
- [验证后的操作记录](samples/verified-20261114-dedup/orders.json)
- [验证后的仓位历史](samples/verified-20261114-dedup/positions.json)
- [实际请求参数、时间、业务码与原始响应索引](samples/verified-20261114-dedup/requests.json)

**接口与请求**

共同前缀：

```text
https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio
```

| 用途 | 方法 | 前缀后的路径 |
|---|---|---|
| 最新操作记录 | POST | `/order-history` |
| 仓位历史 | POST | `/position-history` |
| 带单组合详情 | GET | `/detail?portfolioId=5075281354358777856` |

来源入口：[操作接口](https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history)、[仓位接口](https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history)、[详情接口](https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId=5075281354358777856)。两个历史接口需要 POST，浏览器直接打开链接不能复现 JSON POST。

成功请求使用 `Content-Type: application/json`、`Accept: application/json`、`User-Agent: Mozilla/5.0`；主脚本另带 `clienttype: web`。独立仓位验证不带 `clienttype` 也成功。没有证明这些普通头各自是否必需，不应增加不必要的认证头。

操作记录第一页的成功请求体：

```json
{
  "portfolioId": "5075281354358777856",
  "pageNumber": 1,
  "pageSize": 10,
  "startTime": 1794067200000,
  "endTime": 1794635135788
}
```

该时间窗为北京时间 `2026-11-08 00:00:00` 至 `2026-11-14 13:45:35.788`。响应外层结构：

```json
{
  "code": "000000",
  "message": null,
  "messageDetail": null,
  "data": {
    "indexValue": "1794514186615",
    "total": 41,
    "list": []
  },
  "success": true
}
```

这里的 `list` 仅为结构示意，真实第一页有 10 条数据。下一页可保持同一个时间窗及 `pageNumber: 1`，加入上一页返回的 `indexValue`。这一路径已成功连续遍历五页，主脚本采用此方式。不要自己把游标解释为页码，也不要擅自做减一等算术处理。

退避后的补充验证还确认：不传游标、只设 `pageNumber: 2` 也能获得第二页；`pageSize: 50` 能一次返回上述窗口内全部 41 条；省略 `startTime/endTime` 则响应声明 `total: 2895`，此次只读取该模式第一页，没有全量拉取 2895 条。日期字段因此是可选筛选项，并非必填。两种分页方式各自使用，尚未验证混合递增页码与非空游标的语义。为了保留已验证的游标边界处理，主脚本默认操作页大小仍为 10。补充证据见[请求记录](research/order-followup/requests.json)。

仓位历史的成功请求体：

```json
{
  "portfolioId": "5075281354358777856",
  "pageNumber": 1,
  "pageSize": 100
}
```

第一页 100 条，`pageNumber: 2` 返回 8 条；`total: 108`，`indexValue: null`。已验证单页 100 条可用，尚未确认最大页大小。

`portfolioId` 及仓位的 `positionId` 必须保留字符串形式。组合 ID 大于 JavaScript 安全整数上限，转为 `Number` 会丢精度。所有实测时间戳均为毫秒。

**字段如何对应页面**

操作记录：

| 字段 | 页面对应与处理 |
|---|---|
| `symbol`, `baseAsset`, `quoteAsset` | 合约、标的及计价资产 |
| `side`, `positionSide` | 联合判断开多、平多、开空、平空 |
| `type` | `LIMIT` / `MARKET` 等订单类型 |
| `executedQty` | 成交数量 |
| `avgPrice` | 成交均价 |
| `executedQty × avgPrice` | 页面“总价值”，用 Decimal 计算 |
| `totalPnl` | 这条操作的已实现盈亏 |
| `orderUpdateTime` | 截图时间对应的更新时间 |
| `orderTime` | 订单创建时间，可能早于更新时间 |

| `side` | `positionSide` | 页面动作 |
|---|---|---|
| BUY | LONG | 开多，包括向已有多仓加仓 |
| SELL | LONG | 平多 |
| SELL | SHORT | 开空，包括向已有空仓加仓 |
| BUY | SHORT | 平空 |

如果出现 `BOTH` 或其他未见枚举，脚本标为“未知”，不能套用上述双向持仓映射。返回记录代表订单汇总，并非逐笔撮合成交，也不是订单刚创建时的实时通知。

仓位历史：

| 字段 | 页面对应 |
|---|---|
| `positionId`, `index` | 仓位标识及记录索引，建议持久化时共同保留 |
| `opened`, `closed`, `updateTime` | 开仓、平仓、更新时间 |
| `avgCost`, `avgClosePrice` | 开仓价格、平仓均价 |
| `maxOpenInterest`, `closedVolume` | 最大持仓量、已平仓量 |
| `closingPnl` | 仓位历史中的平仓盈亏 |
| `side` | `Long` / `Short` |
| `isolated` | 样本 `Cross`，即全仓 |
| `leverage` | 杠杆倍数，响应为字符串 |
| `status` | 样本 `All Closed`，即全部平仓 |
| `roi` | 仓位收益率字段，响应为字符串 |

响应的 `id` 在此次仓位样本中等于 `opened`，不要把它当作订单 ID。

截图对照（北京时间；价格与盈亏展示经过小数位格式化）：

| 类型 | 时间 / 合约 | 关键值 |
|---|---|---|
| 操作 | 11-14 02:51:48，NVDA 开多 | 数量 1,582.28，均价 162.78775090 |
| 操作 | 11-14 02:51:37，NVDA 开多 | 数量 1,624.71，均价 162.71125100 |
| 操作 | 11-14 01:10:45，SPCX 平空 | 盈亏 1,770.95469957 USDT |
| 仓位 | BTC，多，10x | 开仓均价 68,108.64468558，平仓盈亏 27,311.41351315 |
| 仓位 | SPCX，空，4x | 平仓盈亏 1,721.18670935 |
| 仓位 | ZEC，空，10x | 平仓盈亏 32,947.59947097 |
| 仓位 | ETH，空，10x | 平仓盈亏 44,759.35333925 |

**实测发现的分页与筛选问题**

1. 操作游标存在跨页重复。五页原始条数为 `10 + 10 + 10 + 10 + 3 = 43`；去除两条跨页完全相同记录后是 41，与 `total` 一致。重复记录分别是 NVDA 和 ETH；它们的 `orderTime` 小于 `orderUpdateTime`，返回游标等于该边界记录的更新时间。这里只描述已观察到的关系，尚无源码证实内部查询条件。
2. 主脚本使用服务端返回的游标，并只消除与更早页面完全相同的记录。原始响应保留全部 43 行，最终导出带 `rawCount`、`crossPageDuplicates`。它不会把同一页的两条相同记录自动合并。
3. 操作响应没有 `orderId` / `tradeId`。完整字段指纹可处理此次跨页重复，但不能提供交易所 ID 级别的严格唯一性；同一订单更新后的数据也会改变指纹。订阅实现需要区分“新事件”和“已有事件数值更新”。
4. 操作日期筛选有效：把开始时间设为北京时间 11-14 零点，返回 3 条当天记录。创建时间与更新时间何者用于时间筛选、边界是否包含等号，尚未做针对性验证。
5. 仓位接口的 `startTime/endTime` 没有产生筛选效果。独立测试无日期、2020 年某日、2030 年某日及最近 24 小时，返回的第一页 10 条完整记录都相同，总数均为 108。主脚本因此不向仓位请求传这些日期字段；如需过滤，请明确按 `opened` 或 `closed` 在本地处理。
6. 仓位默认顺序符合 `closed` 和 `updateTime` 降序，但不是 `opened` 降序。此次两种排序结果相同，无法从样本确定服务端究竟使用哪个键。用户截图选了“开仓时间”，所以导出额外提供 `rowsByOpenedDesc`，在取完所有分页后按开仓时间降序排列。尚未确认服务端排序参数。
7. 网页 URL 的 `timeRange=30D` 不能直接当成这两个接口的请求约定。操作页有独立日期区间，仓位接口这次返回的是当前可查询的 108 条。108 不代表该组合自创建以来全部历史；保留期未知。
8. `public` 和 `friendly` 不能互换。相同请求发往 `v1/public/.../position-history` 得到 `success: true`、`total: 0`，而 `friendly` 返回 108。不能仅凭 HTTP 200 或空列表就判定带单员没有历史。
9. 观察到 HTTP 200 但业务码 `11012005`、`success: false`、提示系统繁忙。相同请求退避后可成功，因此此前部分页码、页大小或省略日期的失败不能直接解释为参数不受支持。脚本检查 HTTP 状态、`code` 和 `success`，并对繁忙及部分暂时性 HTTP 错误有限重试。
10. 操作的 `totalPnl` 与仓位的 `closingPnl` 口径不同。例如 SPCX 分别为 `1770.95469957` 和 `1721.18670935`，BTC 分别为 `27836.55539876` 和 `27311.41351315`。不能直接互换，也不能在没有账单证据时把差额全部归因为手续费或资金费。

**运行和产物**

Python 3.10+，无需 pip 安装第三方依赖。系统需要可用的 IANA 时区数据。

```bash
# 两类数据；日期只控制操作记录。未指定结束时间时取执行时刻。
python3 binance_copytrade.py --start 2026-11-08

# 只取操作记录，指定精确时间窗口。
python3 binance_copytrade.py --kind orders \
  --start 2026-11-08T00:00:00+08:00 \
  --end 2026-11-14T13:45:35.788+08:00

# 只取当前接口可查询的全部仓位历史。
python3 binance_copytrade.py --kind positions

# 带单组合详情。
python3 binance_copytrade.py --kind detail

# 离线回归测试。
python3 -m unittest -v test_binance_copytrade.py
```

不指定开始时间时，操作记录默认最近 7 × 24 小时。只写日期时按北京时间当天 00:00:00 解释，所以 `--end 2026-11-14` 不包含 11-14 白天。自定义 `--out` 必须选择尚不存在的目录，避免覆盖样本。

每次运行保存原始响应、请求元数据和聚合数据。原始响应维持服务器 JSON 数字格式；聚合数据中的小数用字符串保存，便于无损读回 Decimal。仓位原始顺序在 `rows` 中，按开仓时间的顺序在 `rowsByOpenedDesc` 中。

遇到重复页、游标不前进、行数与 `total` 不一致、达到 `--max-pages` 等情况，脚本退出失败并保留已取原始响应，避免将部分结果误报为完整历史。`complete: true` 的含义仅是此次请求遍历与接口 `total` 对齐，不表示交易所终生账本完整。

已完成 8 个离线回归测试，覆盖动作映射、毫秒时区、Decimal 计算、两种分页、边界去重、重复页、中断时的完整性判断，以及 HTTP 200 业务错误。最终真实 HTTP 拉取成功，41 条操作记录、108 条仓位历史；对应文件见文首链接。

**订阅机器人的实现建议**

第一阶段可以采用轮询，不依赖页面渲染。未发现或验证公开带单员操作推送 WebSocket；不能把个人账户 User Data Stream 当成任意带单员的订阅流。

- 首次运行建立基线并入库；是否推送历史由产品配置决定，默认可只推送后续新变化。
- 操作接口用于开多、平多、开空、平空消息。建议从 30–60 秒一次的保守轮询起步，这是设计建议，不是已测出的官方限额。一次扫描固定 `endTime`，保留 24–48 小时重叠区间，并按返回游标补页。
- SQLite 可保存原始数据、完整字段指纹、处理进度及待发送消息。可用 `portfolioId + symbol + side + positionSide + orderTime` 构造候选事件分组，再比较数量、均价、盈亏与更新时间；因为缺少订单 ID，同毫秒碰撞和订单后续更新需要保留处理策略，不能宣称严格 exactly-once。
- 仓位按 `portfolioId + positionId + index` 保存，使用 `updateTime` 或记录内容变更检测平仓及修订。不要按开仓时间水位跳过旧仓位，新平掉的仓位可能很早以前就开了。
- 仓位历史可降低轮询频率，用作汇总和核对。首次全量同步，之后扫描最新变动并定期全量核对；服务端默认排序没有官方保证，仍需监控。
- 网络异常、WAF、业务繁忙均不表示“没有交易”。退避重试，不推进成功水位；持续失败时产生采集异常，而非交易消息。
- 推送系统宜使用持久化待发送队列，成功发送后再标记完成。初步建议还需要根据实际通知渠道、延迟目标和部署出口做持续观测。

目前可确认的是“匿名 HTTP 历史数据可获取并与截图一致”。尚未验证长期可用性、真实发布延迟、正式限流规则、精确历史保留期或实时挂单生命周期；不应承诺秒级跟单。接口分析阶段仅完成读取验证；后续实现的变化通知脚本及验证范围见[微信通知说明](WECHAT_SETUP.md)。尚未安装系统定时任务或发送真实微信消息。
