# 每分钟检查 Binance 变化并通知微信

[通知脚本](notify_wechat.py)单次执行后退出，适合 cron 每分钟调用。依赖同目录的[数据客户端](binance_copytrade.py)，使用 Python 3.10+ 标准库；支持 Linux/macOS，不需要 pip 安装。

已经准备好[实际配置文件](notify_config.json)，其中推送凭据为空、权限为 600。可分享的模板是[配置示例](notify_config.example.json)。实际配置、数据库和日志已加入 [.gitignore](.gitignore)。

默认监控组合 `5075281354358777856`（熬鹰资本）。行为是：首次运行建立基线，旧记录不推送；之后只有新操作或已见操作的数据更新才进入消息队列；每次最多提交 3 条消息，每条合并最多 10 个变化。平时不发“无变化”消息，只有显式启用并记录激活时间后的临期摘要例外。

**推荐：PushPlus 公众号接收**

1. 打开 [PushPlus](https://www.pushplus.plus/)，按其流程关注公众号、登录并完成发送所需的实名认证，取得你自己的发送 token。
2. 编辑[实际配置](notify_config.json)，保持 `provider` 为 `pushplus`，把 `pushplus.token` 的空字符串替换成自己的 token。无需填写 WxPusher 部分。
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

# 再次执行：无变化不发送；有变化才提交推送。
python3 notify_wechat.py

# 查看基线、激活时刻、队列和需要人工处理的消息。
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

每条微信消息最多包含10条仓位记录；超过 `max_messages_per_run`（默认3条消息）的部分保留在待发送队列，由后续正常定时任务继续处理。因此选择全部历史时可能分多轮完成。请求失败和额度限制沿用现有重试、未知状态及激活机制，详见下文。

**每分钟定时运行**

本机已确认 Python 绝对路径为 `/opt/homebrew/opt/python@3.14/bin/python3.14`。在 `crontab -e` 中加入以下一整行：

```cron
* * * * * /opt/homebrew/opt/python@3.14/bin/python3.14 /Users/aifece/workspace/deepseek/fllow-trade/notify_wechat.py --config /Users/aifece/workspace/deepseek/fllow-trade/notify_config.json >> /Users/aifece/workspace/deepseek/fllow-trade/notify.log 2>&1
```

同样内容已放入[cron 示例](notify.cron.example)。这里只提供配置，没有修改系统 crontab。部署到其他服务器时，替换解释器、脚本、配置及日志的绝对路径，保持主机运行即可。建议通过 `crontab -e` 添加，避免用导入命令覆盖已有定时任务。

状态文件路径相对于配置文件所在目录解析，不依赖 cron 的工作目录。内置进程锁：上一轮尚未结束时，新的定时轮次会跳过；人工激活、重试、测试命令若遇到锁占用则返回失败，并提示稍后重试，不会假装执行成功。

**临期摘要与“保活”的真实规则**

自动发消息不能替代微信用户主动激活。`last_accepted_at`（上次推送受理）和 `activated_at`（用户确认的激活）分开保存，发送成功绝不会重置激活时钟。

| 渠道 | 当前文档规定 | 脚本的提醒 |
|---|---|---|
| PushPlus 公众号 `wechat` | 用户发送“激活消息”后，48 小时内可用客服消息；连续 5 条或超时后降级模板消息 | 激活后第 47 小时起尝试一次摘要，提示客服消息窗口将结束 |
| WxPusher 微信 ClawBot | 用户激活后 24 小时内最多 10 条，需回复任意内容再次激活 | 激活后第 23 小时起尝试一次摘要，并在接近条数额度时附带提醒 |

PushPlus 的 48 小时不是整个推送通道的失效时间，因此通常不需要“每两天人工激活才能继续监控”；模板模式仍可继续推送。若不需要客服窗口提醒，可设置 `reminder_enabled: false`。

如果要使用临期摘要，请在你**刚刚实际完成微信侧激活后**运行：

```bash
python3 notify_wechat.py --mark-active-now
```

PushPlus 对应在公众号发送“激活消息”；WxPusher 对应在微信 ClawBot 回复任意内容。此命令只记录你确认的实际激活时间，本身不会激活微信，也不发送消息。重新激活后需要再次执行它，本脚本没有公开回调服务器，无法自动感知你的微信交互。

默认提前 1 小时提醒，可用 `reminder_lead_hours` 调整。每个确认的激活周期最多创建一次提醒；已过期的提醒不会每分钟补发。未记录激活时间时，不虚构截止点、不发送临期摘要。没有新的交易时，摘要仍会包含最新可查询的操作和最近已平仓记录，并明确它们可能已经推送过。

摘要的“仓位”部分使用**仓位历史**，不代表当前实时持仓，也不会从零散操作推算当前持仓。它附带采集时间；仓位历史接口失败时，普通操作变化仍正常入队，下一轮在提醒窗口内重试摘要。

当前规则来源（2026-11-14 核实）：[PushPlus 激活说明](https://www.pushplus.plus/doc/help/activation.html)、[WxPusher 官方文档](https://wxpusher.zjiecode.com/docs/README.md)。不要将旧教程中的 48 小时规则套用到当前 WxPusher 微信通道。

**可选：使用 WxPusher**

[WxPusher](https://wxpusher.zjiecode.com/docs/)提供开源客户端及 SDK。本次核实的文档不能证明整个托管服务端均开源可自建。

1. 在 [WxPusher 后台](https://wxpusher.zjiecode.com/admin/)创建应用并取得 appToken；关注自己的应用，取得 UID。
2. 按官方 App 引导绑定并启用微信 ClawBot。只配置 UID 不代表该用户已绑定微信渠道。
3. 将配置中的 `provider` 改为 `wxpusher`，填写 `wxpusher.token` 和 `wxpusher.uid`；也可使用 `WXPUSHER_APP_TOKEN`、`WXPUSHER_UID` 环境变量。
4. 如果已经使用 PushPlus 建立过状态库，为 WxPusher 选择新的 `state_file`（例如 `state/wxpusher.sqlite3`）。换接收人时也使用新的库并修改 `recipient_id`，避免把旧队列发给另一个人。轮换同一接收人的 token 可以保留状态库。
5. 在微信实际回复激活后执行 `--mark-active-now`，然后执行 `--test-push`，确认微信收到。测试消息也计入额度。

脚本本地保守计数：每次网络提交前先占一个额度；结果未知也保持占用；明确被拒绝才释放。达到 10 条、未记录激活、窗口过期或时钟早于激活时间时，保留待发送消息，不继续调用微信通道。用户重新激活并记录后，再次正常运行会继续提交积压的变化消息。

这个计数只是本程序的估计，其他应用可能共用渠道额度，API 受理也不能证明该消息实际进入微信。因此 WxPusher 的“10 条”很容易影响交易频繁的监控，推荐优先使用 PushPlus 公众号；不要把每分钟主动推送当成保活。

**失败处理与推送状态**

SQLite 将采集进度、已观察事件和消息队列分开保存。先在同一个事务中保存变化和待发送消息，再尝试推送，所以进程在入队后退出不会丢掉待发送消息。提交成功后重跑，不会再次提交同一队列项。

| 状态 | 含义与处理 |
|---|---|
| `pending` | 待发送，或服务明确拒绝后等待退避重试；最多 5 次提交尝试 |
| `accepted` | 推送服务已经受理；不等于微信送达或用户已读 |
| `unknown` | 网络超时、响应损坏、HTTP 5xx 或发送过程中进程中断，是否受理未知；不自动重发 |
| `failed` | 明确失败累计 5 次，需要核查凭据、配额等原因 |
| `expired` / `superseded` | 临期提醒已过期，或因用户重新激活而作废 |

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

**验证情况**

已通过 37 项离线回归测试（包括手动仓位推送的格式、分页拆分、队列隔离、采集失败和预览行为），推送 HTTP 均使用替身，没有发送真实消息；覆盖初始化、重复运行、新操作与修订、金额格式变化、持久化、失败退避、未知结果、进程中断、并发锁、提醒只发一次、到期检查和 WxPusher 额度预占。真实 Binance `--dry-run` 成功读取当时最近 48 小时的 12 条操作记录，没有写入生产状态，也没有发送微信。

```bash
python3 -m unittest -v test_binance_copytrade.py test_notify_wechat.py
```

新增 `--push-positions` 的本次在线预览遇到 Binance 接口网络超时，未取得最新响应；新增功能的离线测试通过。本次开发未执行真实微信发送，微信实际接收仍需用自己的配置验证。当前配置没有安装定时任务。
