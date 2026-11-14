# 每分钟查询 Binance 操作变化并通过第三方服务通知微信

[通知脚本](notify_wechat.py)单次执行后退出，适合 cron 每分钟调用。依赖同目录的[数据客户端](binance_copytrade.py)，使用 Python 3.10+ 标准库；支持 Linux/macOS，不需要 pip 安装。微信推送由 PushPlus 或 WxPusher 第三方服务发送。

在[实际配置文件](notify_config.json)中设置推送服务和凭据，配置格式可参考[配置示例](notify_config.example.json)。请妥善保管真实凭据；实际配置、数据库和日志已加入 [.gitignore](.gitignore)。

默认监控组合 `5075281354358777856`（熬鹰资本）。定时模式只查询 `orders` 操作接口：首次运行建立基线，旧记录不推送；之后只有新操作或已见操作的数据更新才自动创建消息。每次默认最多提交 3 条消息，每条合并最多 10 个变化。没有变化时不自动创建消息，也不查询仓位历史；没有保活、定时摘要、本地激活窗口或额度计数。

正常运行还会处理已有待发送队列，包括用户显式创建的测试消息、手动仓位历史消息和手动重试的消息。

**推荐：PushPlus 公众号接收**

1. 打开 [PushPlus](https://www.pushplus.plus/)，按其流程关注公众号、登录并完成发送所需的实名认证，取得你自己的发送 token。
2. 编辑[实际配置](notify_config.json)，保持 `provider` 为 `pushplus`，将 `pushplus.token` 设为自己的 token。无需填写 WxPusher 部分。
3. 运行下面的连接测试，确认微信实际收到。服务返回成功只是受理，脚本不会据此声称微信已经送达。

```bash
python3 notify_wechat.py --test-push
```

也可以通过 `PUSHPLUS_TOKEN` 环境变量提供 token；环境变量优先于文件。cron 不会自动继承你在交互终端临时 export 的变量，因此定时部署时用受保护的配置文件最直接。请不要把真实 token 发到聊天或提交到版本库。

初始化和检查：

```bash
# 预览真实 Binance 数据，不推送、不修改磁盘状态，无需推送 token。
python3 notify_wechat.py --dry-run

# 填好 token 后正常执行，首次只保存基线。
python3 notify_wechat.py

# 再次执行：只为操作变化创建消息，并继续处理已有待发送队列。
python3 notify_wechat.py

# 查看基线、队列和需要人工处理的消息。
python3 notify_wechat.py --status
```

首次基线默认覆盖最近 48 小时。此后使用“上次成功采集时间减去 48 小时”作为查询起点，正常运行时保留重叠窗口，停机后也会尝试补齐；超过 `max_catchup_days` 时明确失败，需调整配置后补齐，不会静默跳过。接口历史保留期及迟到超过查询窗口的更新无法由此保证。

**单独推送当前查询到的仓位历史**

```bash
# 立即提交最新10条仓位历史，按平仓时间倒序。
python3 notify_wechat.py --push-positions

# 指定条数，例如最新20条。
python3 notify_wechat.py --push-positions 20

# 选择当前接口可查询的全部仓位历史。
python3 notify_wechat.py --push-positions 0

# 只预览内容，不发送、不写入状态，也不要求推送凭据。
python3 notify_wechat.py --push-positions --dry-run
```

消息包含合约、方向、杠杆、开平仓时间、开平仓均价、已平数量和平仓盈亏，并明确标记为仓位历史。它与当前实时持仓不同。

每次手动调用都会创建一次新的历史快照，即使数据没有变化也会提交；这个参数用于单次手动操作，不要放入每分钟 cron。它不会初始化或推进操作监控基线，本轮只提交本次仓位快照，不提前发送其他已排队的消息。

每条微信消息最多包含10条仓位记录；超过 `max_messages_per_run`（默认3条消息）的部分保留在待发送队列，由后续正常定时任务继续处理。因此选择全部历史时可能分多轮完成。请求失败沿用下文的退避重试和未知状态处理；服务是否受理或送达由第三方决定，脚本没有本地激活或额度门槛。

**每分钟定时运行**

本机已确认 Python 绝对路径为 `/opt/homebrew/opt/python@3.14/bin/python3.14`。在 `crontab -e` 中加入以下一整行：

```cron
* * * * * /opt/homebrew/opt/python@3.14/bin/python3.14 /Users/aifece/workspace/deepseek/fllow-trade/notify_wechat.py --config /Users/aifece/workspace/deepseek/fllow-trade/notify_config.json >> /Users/aifece/workspace/deepseek/fllow-trade/notify.log 2>&1
```

同样内容已放入[cron 示例](notify.cron.example)。这里只提供配置，没有修改系统 crontab。部署到其他服务器时，替换解释器、脚本、配置及日志的绝对路径，保持主机运行即可。建议通过 `crontab -e` 添加，避免用导入命令覆盖已有定时任务。

状态文件路径相对于配置文件所在目录解析，不依赖 cron 的工作目录。内置进程锁：上一轮尚未结束时，新的定时轮次会跳过；手动仓位推送、重试、测试命令若遇到锁占用则返回失败，并提示稍后重试，不会假装执行成功。

**旧状态迁移与第三方服务规则**

升级后的下次正常启动会自动把历史遗留的 `reminder` 队列中处于 `pending`、`failed`、`unknown` 或 `sending` 状态的消息标记为 `cancelled`，不再发送或重试；已受理的 `accepted` 记录保留。旧提醒配置会被忽略，旧激活相关 meta 会移除。`--status` 和 `--dry-run` 会在内存副本中应用迁移以查看或预览，但不修改磁盘状态。

脚本不提供保活命令，也不管理激活窗口或累计渠道额度。第三方服务仍可能要求关注、绑定、实名认证或微信侧交互，并自行设置发送频率、额度和接收条件；请以 [PushPlus 官方文档](https://www.pushplus.plus/doc/)和 [WxPusher 官方文档](https://wxpusher.zjiecode.com/docs/README.md)为准。脚本不能保证第三方服务持续可用或消息送达。

**可选：使用 WxPusher**

[WxPusher](https://wxpusher.zjiecode.com/docs/)可作为另一种第三方推送服务。

1. 在 [WxPusher 后台](https://wxpusher.zjiecode.com/admin/)创建应用并取得 appToken；关注自己的应用，取得 UID。
2. 按服务官方引导绑定并启用微信接收渠道。只配置 UID 不代表该用户已绑定微信渠道。
3. 将配置中的 `provider` 改为 `wxpusher`，填写 `wxpusher.token` 和 `wxpusher.uid`；也可使用 `WXPUSHER_APP_TOKEN`、`WXPUSHER_UID` 环境变量。
4. 如果已经使用 PushPlus 建立过状态库，为 WxPusher 选择新的 `state_file`（例如 `state/wxpusher.sqlite3`）。换接收人时也使用新的库并修改 `recipient_id`，避免把旧队列发给另一个人。轮换同一接收人的 token 可以保留状态库。
5. 完成服务侧要求后执行 `--test-push`，确认微信实际收到。脚本按第三方响应记录受理或失败，不要求记录本地激活时间，也不在本地预占或累计渠道额度。

**失败处理与推送状态**

SQLite 将采集进度、已观察事件和消息队列分开保存。先在同一个事务中保存变化和待发送消息，再尝试推送，所以进程在入队后退出不会丢掉待发送消息。提交成功后重跑，不会再次提交同一队列项。

| 状态 | 含义与处理 |
|---|---|
| `pending` | 待发送，或服务明确拒绝后等待退避重试；最多 5 次提交尝试 |
| `accepted` | 推送服务已经受理；不等于微信送达或用户已读 |
| `unknown` | 网络超时、响应损坏、HTTP 5xx 或发送过程中进程中断，是否受理未知；不自动重发 |
| `failed` | 明确失败累计 5 次，需要核查凭据、配额等原因 |
| `cancelled` | 升级时自动取消的旧 `reminder` 消息，不再发送，且不能手动重试 |

`--status` 会显示需要处理的队列 ID。确认消息没有收到、并接受重试可能重复后，可以把指定消息重新加入队列：

```bash
python3 notify_wechat.py --status
python3 notify_wechat.py --retry-message 3
# 下次正常运行/下一次 cron 才会发送；--retry-message 本身不发送。
```

明确拒绝后按 2、4、8、16 分钟退避；第 5 次失败转 `failed`。每轮最多 3 条，遇到发送错误后停止本轮继续提交。接口暂时繁忙、非 JSON/WAF、分页不完整均不会当作“无交易”推进采集基线；已有待发送消息仍可继续处理。

两项送达限制需要保留：

- WxPusher 当前官方送达查询已停用，不能轮询旧接口，更不能把停用提示当作失败并重发。[官方 OpenAPI](https://wxpusher.zjiecode.com/docs/openapi.yaml)
- PushPlus 标准发送响应是受理流水号。送达结果查询需要另行开通 OpenAPI、AccessKey、安全 IP 等，本版本未接入，也没有部署回调服务。[发送 API](https://www.pushplus.plus/doc/guide/api.html)、[开放接口](https://www.pushplus.plus/doc/guide/openApi.html)

因此本脚本诚实标记“受理”，不承诺严格不重不漏或自动确认微信送达。Binance 操作接口也不返回 orderId：完整字段指纹用于去重，按合约、方向和订单时间匹配的“记录更新”只是候选匹配；同毫秒不同订单仍存在歧义。

**离线验证范围**

离线回归测试使用 HTTP 替身，不发送真实消息。验证范围包括初始化、重复运行、新操作与修订、金额格式变化、持久化、失败退避、未知结果、进程中断、并发锁，以及手动仓位推送的格式、分页拆分、队列隔离、采集失败和预览行为。本次回归还验证了只查询操作变化、忽略旧提醒配置、迁移旧 `reminder` 队列、移除旧激活 meta，以及保留采集进度和已受理记录；全部通过。

```bash
python3 -m unittest -v test_binance_copytrade.py test_notify_wechat.py
```

运行上述命令确认当前代码的回归结果。离线测试不能证明第三方服务可用或微信实际收到；实际接收需用自己的配置显式执行 `--test-push` 验证。
