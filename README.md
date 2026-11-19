# MODI · AI 交易团队

## 看板：只显示 OKX 实盘（2026-11-15 起）

看板（GitHub Pages，`index.html` + `assets/app.mjs` / `assets/live.mjs`）**只显示 OKX 实盘账户（真实资金）**，模拟盘板块已全部移除，不再读取 `paper-data` 或 `paper/*.json`。真实资金，风险自负，不构成投资建议。

链路：

```
新加坡实盘服务器 (okx-live-sg)                 本地机器（有 GitHub 凭据）                     GitHub                      看板
/var/lib/modi-okx-live/okx_live/   --ssh 只读-->  tools/live_sync.py  --白名单 + 密钥检查-->  live-data 分支     --raw-->  实盘账户页
  summary.json / state.json / actions.jsonl                                                snapshot.json / fills.json
```

- **live-data 分支**：独立孤儿数据分支（和 `master`、`paper-data` 都分开），只有 `snapshot.json`（权益、可用、日内盈亏额与 %、paused / halted / dry_run、每笔持仓含止损止盈、资金曲线）、`fills.json`（最近 50 条开平仓 + 30 条跳过 / 暂停记录）和 `README.md`。看板读取 `https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/snapshot.json` 与 `.../fills.json`（每分钟换一次查询参数绕过缓存）。请勿手动修改。
- **同步脚本** `tools/live_sync.py`（只用标准库）：在本地机器上运行，`ssh` 到服务器用 `sudo -n -u modi-live` 只读三个文件（服务器上不写任何东西），按字段白名单组装，再推送到 `live-data`（只快进，不 force push；数据没变化就不提交）。手动运行：

  ```bash
  python3 tools/live_sync.py              # 默认 root@139.180.146.42、~/.ssh/okx_live_sg、分支 live-data
  python3 tools/live_sync.py --dry-run    # 只生成并检查，打印 snapshot.json，不推送
  ```

  参数：`--host`、`--key`、`--remote-dir`、`--remote-user`、`--branch`、`--repo`、`--workdir`（数据分支专用克隆，默认 `~/.cache/modi-live-sync/repo`，不碰其它 worktree）。退出码：0 已推送或无变化，1 读取 / 数据错误，2 密钥检查未通过，3 git 错误。建议每小时定时运行一次（服务器上的交易程序也是每小时一次）。
- **安全**：GitHub 凭据只在本地机器；服务器上不放任何 GitHub 凭据，也不需要。输出只含白名单字段（不含订单号、clOrdId、执行档案等），写出前对最终 JSON 文本检查：含 `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` 变量名或其值、UUID、长度 ≥ 16 的连续 base64 / hex 串（纯数字时间戳与小写下划线代码词除外）即拒绝发布并以退出码 2 结束；错误信息只给简短原因，不打印服务器文件内容。数据不是 `trading_mode: live` 时拒绝发布。
- **看板显示**：权益、日内盈亏（额 + %）、累计盈亏、可用、运行状态（实盘运行中 / 空跑 / 已暂停 / 日亏停手 / 数据离线）、每笔持仓（币种、方向、张数、杠杆、全仓、均价、标记价、浮盈、止损、止盈、保证金）、最近成交与运行记录、资金曲线、每日收益；时间按金边时间（UTC+7）；超过 2.5 小时没有新快照显示“可能离线”。持仓浮盈在行情连上时按 OKX 公共标记价（`wss://ws.okx.com`）实时估算。市场行情页和单币 K 线页保留；“策略进化”页（`#/evolve`）已在 PR #97 恢复（进化引擎在 GitHub Actions 后台运行，为实盘提供信号；不恢复模拟盘账户）。
- **异常告警检查** `tools/live_alert.py`（只用标准库，只读，自己不发任何消息）：每小时 `live_sync.py` 之后运行，用同样的 ssh 只读路径读 `summary.json` 与 `actions.jsonl` 末尾，检查以下条件，任一命中即告警：

  | code | 条件 |
  |---|---|
  | `stale` | `summary.updated_at` 距现在超过 2 小时（服务停了或定时没跑），或缺少该字段 |
  | `halted` | `halted == true`（含日亏 25% 熔断） |
  | `paused` | `paused == true` |
  | `day_loss` | `(equity - day_start) / day_start < -10%` |
  | `stop_streak` | 最近连续 3 笔平仓（`close` / `closed_on_exchange`，不含 dry-run）都是止损：`outcome` 为 `stop` / `liquidation`，或 `reason` 含 `stop`；`outcome` 可能只在 `summary.recent_actions` 里补上，会自动合并；结果未知的平仓不计入也不打断 |
  | `api_error` | 过去 90 分钟内有 `action: error`（下单 / 平仓 / 设杠杆 / 补止损失败），消息含步骤、币种与交易所错误摘要（先过密钥检查） |
  | `read_error` | 服务器读不到（ssh 失败 / 超时 / JSON 损坏） |

  调用与输出（stdout 一个 JSON 对象，退出码区分是否需要通知）：

  ```bash
  python3 tools/live_sync.py; python3 tools/live_alert.py     # 每小时定时：同步后检查
  python3 tools/live_alert.py --summary s.json --actions a.jsonl --state '' --now 2026-11-15T16:00:00Z   # 本地演练
  ```

  ```json
  {"ok": false, "notify": true, "checked_at": "2026-11-15T16:00:00Z",
   "alerts":     [{"code": "halted", "msg": "实盘已熔断停手（halted=true…）", "fingerprint": "halted:2026-11-15"}],
   "new_alerts": [{"code": "halted", "msg": "…", "fingerprint": "halted:2026-11-15"}],
   "resolved":   []}
  ```

  退出码：**0** 正常（`ok: true`，不发消息）；**1** 有异常且有新的（`notify: true`，把 `new_alerts` 的 `msg` 发给用户）；**2** 有异常但上一轮都已报过（`notify: false`，不发）。服务器读不到时也以 `read_error` 告警走同样流程。
  去重：状态文件默认 `/workspace/modi/.live_alert_state.json`（`--state ''` 关闭去重，每次有异常都 `notify`），记录上一轮仍然存在的告警指纹。指纹 = code（`stale`、`paused`）、code + 日期（`halted`、`day_loss`，第二天再出现会重新通知）、code + 最新事件时间（`stop_streak`、`api_error`，新的止损 / 新的报错会重新通知）。同一异常持续存在只通知一次；消失后（`resolved` 列出，仅供参考，不通知）再出现会重新通知。输出不含密钥：只用白名单字段，错误详情和最终 JSON 都过 `live_sync.secret_problems`。
- 验收：`npm test`、`python3 -m unittest discover -s tests -p 'test_*.py'`（`tests/test_live_sync.py`、`tests/test_live_alert.py`）、`PANEL_URL=http://127.0.0.1:8008/ node tests/live_browser.cjs`。

> 下面各节是历史说明（模拟盘、旧主力盘、策略引擎）。其中描述看板模拟盘板块的部分已不再适用；后台的模拟盘跟单与策略进化仍按原流程运行。


更新记录与本地同步注意事项见 [CHANGELOG.md](CHANGELOG.md)。

常驻调度、管理员登录、心跳、直接停止接口的部署包说明见 [deploy/README.md](deploy/README.md)。服务默认执行关闭，当前 GitHub Pages 继续原流程；服务器就绪并完成执行权迁移后再启用。

机器人面板新增“异动 / 决策”：展示当前模拟策略实际产生的信号、跳过原因与 OKX 执行反馈，同时保留旧 crypto-bot 的异动和会议关联记录。原历史数据源已暂停，旧快照会明确标注时间；新策略信号不冒充实时会议记录。账户历史第 05 项也提供机器人直达入口。

连接状态区分模拟持仓行情与账户快照：首页和模拟账户页只有当前全部持仓收到有效模拟报价才显示“行情实时 / 行情轮询”，行情缺失显示“行情断开”；无持仓显示“账户快照”。账户卡“允许开仓”仅表示未暂停。首页与进化页的 paper-data 快照以金边时间（UTC+7）显示「上次更新 / 下次更新」（定时 `17 */4 * * *` UTC），距上次更新超过 5 小时才提示延迟；GitHub Actions 实际运行可能晚于整点。旧的 `paper/*.json` 主力盘快照仍按 `config.js.offlineMinutes`（15 分钟）判断离线，仅在「显示原主力盘」里使用。WebSocket 连续失败后每 60 秒重新探测；连接诊断提供 8 秒超时测试和手动重新连接，真实网络故障不会被改成绿色在线状态。
模拟标记价 WebSocket 优先使用标准 443 端口，连接失败切换 8443 备用，两者均由只读连通性检查确认能取得模拟报价；CSP 显式允许这两条通道。持仓权益按接收到的新标记价更新，账户现金、成交和费用仍以后台快照为准。

手机优先的中文只读看板。首页聚焦主力盘权益、持仓、K 线与风控；团队工作台和时间线折叠展示。只统计 `paper_main`，会议单与均值回归之外的旧策略内容不进入主页面。`#/paper` 是账户历史，`#/market` 保留独立行情页，`#/coin/<symbol>` 是单币研究。

## 运行与快照

```bash
cd /workspace/crypto-panel
python3 tools/make_snapshot.py
python3 tools/validate_account.py paper/account.json
python3 -m http.server 8008 --bind 127.0.0.1
```

默认只读 `/workspace/crypto-bot/paper_main/{state.json,trades.csv,equity.csv,events.jsonl,scan.json}`，以及 `/workspace/crypto-bot/{decisions.json,alerts.jsonl,team/}`。不导入、启动或修改交易程序。主力盘必要文件缺失或校验失败时拒绝发布，保留上次快照；不会补造余额、成交或权益点。

本次开发期间主力盘被外部暂停、平仓并移入归档。当前公开产物使用这份真实归档，明确标记“已停止 / 历史快照”：

```bash
python3 tools/make_snapshot.py \
  --account-source-root /workspace/crypto-bot/archive/2026-11-03-reset \
  --historical
```

`--account-source-root` 只切换主力盘账户、扫描与历史 CSV 的读取目录；当前决议、blackout_windows、事件流和团队仍读取 `--source-root`（默认 `/workspace/crypto-bot`）。账户历史会合并归档和当前日志的 wake 记录并去重。`--historical` 将账户标记为已停止，持仓与估值固定为归档观测。恢复运行后，移除这两个参数使用默认命令即可。不会自动找归档、重启或恢复资金。

其他选项：`--output` 设置兼容快照路径，其他公开文件默认写到相邻目录；`--account-output`、`--team-root`、`--team-output` 可单独指定，`--team-only` 仅生成团队数据。已删除 BT60 / 旧回测表数据生成、参数和相应测试。

## 自动发布到看板

模拟盘恢复运行后，在运行模拟盘的机器上（本仓库目录、`master` 分支、已配置可推送的 git 凭证）执行：

```bash
python3 tools/publish_snapshot.py                    # 每 10 分钟生成并推送一次，Ctrl+C 停止
python3 tools/publish_snapshot.py --once             # 只发布一次
python3 tools/publish_snapshot.py --dry-run --once   # 本地生成并提交，不推送
nohup python3 tools/publish_snapshot.py > publish.log 2>&1 &   # 后台常驻
```

每轮依次：`git pull --rebase` 同步远程代码 → 运行 `make_snapshot.py`（未识别参数原样转发，如 `--source-root`）→ 只暂存并提交 `paper/*.json`，提交信息 `snapshot <UTC时间>` → 推送，失败按 2/4/8/16 秒重试，仍失败则留在本地下轮再推。生成或校验失败时不提交，看板保留上次数据；快照无变化不提交；不在目标分支时拒绝运行。间隔最小 360 秒，默认 600 秒，以免超出 GitHub Pages 每小时约 10 次构建的软上限；页面超过 `config.js.offlineMinutes`（15 分钟）未更新会显示延迟 / 离线。



- 常驻余额与持仓金额只取 `paper_main`；现金加逐仓浮盈计算权益，每仓亏损按保证金封顶。旧账户不计入总额。
- 首页主卡显示账户权益和当日盈亏（USDT / %），轻量 SVG 曲线取 `account.json` 的 `equity_curve`，日 / 周为最近24h / 7d，全部为全部观测点；不插入实时估值。没有曲线时显示100U平线与“暂无记录”，缺当日基准显示“—”。
- 持仓行只显示币种、多空、名义金额和收益（USDT / 保证金百分比）。点击或键盘操作在原地展开同一套1H / 4H K线，显示入场、止损、止盈、保证金和杠杆；同时只打开一行，再点收起。扫描的非持仓信号币进入单币研究。WebSocket 报价失效时依次回退有效 REST、快照标记价。
- 策略状态、开关、参数只展示启用的均值回归；会议单作为独立执行来源。只读开关不能执行交易。已停止账户如实显示停止状态。
- 最近信号只展示均值回归和会议单，默认最近 10 条，其余点击“展开”。已停策略信号不显示。扫描默认前 15 行，支持排序、持仓 / 信号筛选及“展开全部”。
- 单币研究已删除放量突破六条件，改为均值回归入场条件，阈值来自主力盘启用配置中的 `rsi`、`trend`。逐项展示连续 201 根原生 1H 已收盘样本、趋势偏离、EMA50 五根斜率 / ATR、ATR / 收盘、成交额倍数、前一根 RSI、布林带回归；同运行器 `bt60.engine.make_signal` 的 EWM `adjust=False`、布林标准差 `ddof=0`。RSI 多头 `< rsi`、空头 `> 100−rsi`；偏离 `|EMA50/EMA200−1| < trend`；斜率 `< 0.8`；ATR / 价格范围 0.2%–3.5%；成交额 ≥ 前20根均值0.8倍。4H 仅供研究，不判定原生1H执行条件。
- 已删除归档回测表、归档行情30、旧四盘入口和突破参数。

## 自我进化交易策略（当前）

策略不再由人编写：`tools/evolve/` 在真实 OKX 行情（成交额靠前的 USDT 永续，1 小时 K 线，最近一年）上让一群策略竞争进化。

- **数据层** `data.py`：K 线缓存在 GitHub Actions cache（`candles/`，不进 paper-data 分支），首次回补一年、之后只取新 K 线；按时间切分，约 60% 训练、20% 样本外检验、最新 20% 终审（终审不参与打分和繁殖）。
- **引擎层**：种群 60、精英 6。每一代先**观察波动率**（决定变异强度），再**选择**（锦标赛）、**交叉**、**突变**产生后代（含 3 个随机移民）；每个策略在训练集上算适应度，在样本外与终审上检验（收益 > 0、回撤 < 25%、交易次数门槛）。**合格策略中训练适应度最高的 8 个组成冠军组合**（同一物种最多 2 个，只做多 / 只做空各最多 5 个），共用一个全仓账户。
- **上场门槛**：每个席位各自累计守擂。同一策略连续 5 代留在组合且样本外合格，并且合并样本外窗口最大回撤 ≤ 30%，才允许开新仓并由 OKX 模拟盘跟单。失败或退出组合则清零；一次运行进化 5 代，合格成员可在当次运行内满门槛；仅 refresh、没有新代数时不加代。三项未过关则不再开新仓，已有持仓走到自己的退出；无人过关则持有现金。第一名（信号优先，不再加大仓位）也必须通过这三项门槛。每一席位（含第一名）按风险基因 2%–10%、杠杆基因 1–20× 开仓，单笔风险（含主流币加倍）最多 10%；杠杆再受该币 OKX 最高档限制（纸面用公共 `lever` 快照；快照或接口取不到上限时一律按 20×，主流币也一样。BTC/ETH/SOL/BNB/XRP/DOGE 维持保证金 0.5% 且风险加倍，其他币 2.5%）。保证金模式为全仓：当账户权益（现金加全部持仓浮盈，已扣手续费和资金费）低于合计维持保证金时，全部持仓按该根价格加手续费平仓（不做分层部分强平，因此单笔亏损可能连累整个账户）。若新开仓的止损（或所有持仓同时触及止损）会把权益打到剩余维持保证金及以下，则跳过该笔。OKX 模拟盘 / 实盘跟单使用同一套杠杆基因和全仓分档，全仓下单（`mgnMode`/`tdMode` = `cross`），运行时再拉取该币的 OKX 上限；取不到可用的上限时不开该币的新仓（不按 20× 兜底），已有仓位照常维护和平仓。跟单程序的风险区间从 2026-11-20 起与纸面分开：基因会被夹到 5%–20%（缺风险时默认 5%），主流币加倍后仍 ≤ 20%，同向已开 ≥10 仓时下一单减半；纸面仍是本段的 2%–10% / 单笔最多 10%。
- **同源执行** `execution.py`：回测与纸面交易共用同一个 `simulate()`：收盘出信号、下一根开盘成交、手续费 0.05%、滑点 0.02%、同一根 K 线同时触及止损与止盈按止损计。适应度回测同样受 10% 单笔风险帽约束，并按全仓规则计算强平（账户权益 vs 合计维持保证金；维持保证金率为名义价值的 0.5%/2.5%）。
- **界面层**：看板“策略进化”页（`#/evolve`）显示进化循环、每代适应度、基因池谱系图（点击节点看基因与训练 / 样本外 / 终审回测）和冠军组合的纸面交易（每个席位展示各自的守擂 N/5）；首页有进化卡片。
- **运行**：`paper-trading.yml` 约每 4 小时运行一次（cron `17 */4 * * *` UTC），每次进化 5 代，然后 OKX 模拟盘跟随通过门槛的持仓（`evolve/signals.json`）。手动运行时勾选 `reset` 会从新的随机种群重新开始（保留 K 线缓存）。本地：`python3 tools/evolve/run.py --data <目录> --generations 5`。

- **当前风控（代码为准；2026-11-20 起实盘 / OKX 模拟盘跟单与纸面不再共用风险区间和拥挤台阶）**：
  - **OKX 实盘与模拟盘跟单**（`okx_demo_trader.py`，两种模式同一套常量）：每笔止损风险 = 开仓策略的风险基因，夹到 5%–20%（`RISK_RANGE = (.05, .20)`，缺 `risk` 时默认 5%）；主流币（BTC/ETH/SOL/BNB/XRP/DOGE）风险 ×2，但加倍后仍 ≤ 20%（`MAX_TRADE_RISK = .20`）；同方向已开不到 10 仓按满风险，≥10 仓时下一单减半（`CROWD_STEPS = ((10, .5),)`）；`LEADER_RISK` 跟随 `MAX_TRADE_RISK`，因此上限从 10% 升到 20%，第一名仍按自己的基因开仓、只是信号优先。摘要 `execution_profile` 里 `risk` / `risk_max` / `max_risk` / `leader_risk` 分别为 5% / 20% / 20% / 20%。
  - **纸面与进化**（`execution.py` / `paper.py`，本次未改）：单笔风险仍是基因 2%–10%（`MIN_TRADE_RISK` / `MAX_TRADE_RISK = .10`），主流币加倍后仍 ≤ 10%，拥挤台阶仍是 ≥3 仓减半、≥6 仓四分之一，`ACCOUNT_RULES['leader_risk']` 仍为 10%。
  - **两边相同、未改**：同方向最多 20 仓（`MAX_SAME_SIDE_POSITIONS = 20`，含第一名）；同方向合计风险不设上限（`same_side_cap = None`）；当天（UTC+7）亏损达 25% 全部平仓停手到次日（`EVOLVED_DAILY_HALT = .25` / `ACCOUNT_RULES['daily_halt'] = .25`，含第一名）；杠杆 = 杠杆基因 1–20×，再受该币 OKX 最高档限制（实盘取不到上限不开新仓）；全仓（`cross`）；每笔都挂交易所止损；维持保证金率主流币 0.5%、其他 2.5%；保证金最多用可用的 95%；每个策略同时最多 3 仓；实盘只扫描信号列表前 50 个币（`SCAN_LIMIT = 50`，与币池 `UNIVERSE_SIZE = 50` 一致）。代码里 `DAILY_HALT = .10`、`RISK = .01`、10× 只属于已退役的旧档（legacy volume-test），不是实盘现状。

旧的三套固定策略（`tools/paper_trader.py`）已退役，以下章节保留作历史参考（其中的 1% / 2% / 3% 风险、5% / 6% 同向上限、8% 日亏停手、10× 杠杆、5 币 / 100 币等数字只属于旧引擎，不是当前规则）。

## 学习用模拟交易（GitHub Actions）

### 临时增量测试（仅模拟资金）

**当前激进档：2026-11-04 05:20 至 2026-11-05 05:20（UTC+7）。** 每笔止损风险 3%、同向风险上限 6%，每轮按 24h USDT 估算成交额选择前 **100 个** live / linear / USDT 永续合约。排行榜、选中列表写入信号账户，两端共用同一份实际 100 币名单；掉出榜单的持仓与待成交单继续管理。均值回归 RSI 35/65、趋势偏离阈值 3%，小时突破回看 12 小时。日线规则、10 倍杠杆上限、逐仓止损和 8% 日亏停手保留。到期后的下一次运行恢复标准 5 币 / 1% 风险 / 5% 同向上限；测试中已有仓位继续管理到退出。配置由 `tools/volume_test.py` 统一提供，看板通过账户回执显示实际扫描数、档位、风险和到期时间。
扫描使用 4 个行情读取线程，全局请求间隔至少 150 ms，100 币模式每周期读取 300 根 1H 与 300 根日线（超过指标所需 201 根），保留当前未收盘小时开盘数据以处理待成交单。样本或接口异常时拒绝生成本轮数据，不用少量旧币冒充 100 币扫描。
启动任务 `.github/workflows/start-aggressive-demo.yml` 在此次发布后检查并执行一轮，后续由原有 Paper trading 定时任务继续执行；只在新收盘 K 线产生信号时下单，不重复回放已处理 K 线、不重置账户和历史。GitHub 定时任务可能明显延迟，不保证每五分钟执行或立即成交。

以下是先前增量测试档的说明，激进档生效期间由上述配置覆盖：2026-11-04 04:40 至 2026-11-05 04:40（UTC+7），配置见 `tools/volume_test.py`。
每笔新开仓的止损风险从权益的 1% 提高到 2%（相同止损距离时下单金额约翻倍），
扫描币种在 BTC / ETH / SOL / XRP / DOGE 上增加 ADA / AVAX / LINK / LTC / BCH；
均值回归 RSI 阈值由 25/75 放宽为 30/70，小时突破回看从 48 小时缩短到 24 小时。
这些变动增加信号机会，不保证成交笔数。日线策略、10 倍杠杆上限、5% 同向风险上限、8% 日亏停手和交易所止损保留。
现有仓位不主动加仓，账户和历史不重置，人工仓位不接管；只有后续新信号采用测试配置。
到期后的下一轮任务自动恢复 5 币、1% 风险和原入场阈值，测试中开的仓位仍持续管理到退出。
任务频率仍约每 5 分钟，GitHub Actions 可能延迟；小时策略仍只在新 1H 收盘后出信号。
两端执行日志会输出测试状态、风险比例和到期时间。所有 OKX 请求继续强制携带模拟交易标头。

### 常规配置

`tools/paper_trader.py` 由 `.github/workflows/paper-trading.yml` 约每 5 分钟（cron `2-59/5`，GitHub 定时任务常有延迟）检查一次（只处理新的已收盘 K 线，重复运行无副作用），只读 OKX 公共 K 线，不需要密钥、不下单。**一个 100 USDT 的合并账户**由三套策略共同交易 5 个主流 USDT 永续（BTC、ETH、SOL、XRP、DOGE）：共享余额、同向风险上限与日亏停手，一个币同时只持一个仓（先发信号的策略开仓），每笔成交记录来源策略。1H 策略在每根 1 小时 K 线收盘后行动，日线策略在每个 UTC 日收盘后行动：

- **均值回归**：看板 `assets/indicators.mjs` 的入场规则（RSI 25 / 趋势 2%），止损 1.5×ATR（0.5%–4%），止盈 20 根中轨，最长 24 小时。
- **趋势突破只做多**：收盘突破前 48 小时高点且 EMA50 > EMA200，止损 2×ATR，3×ATR 跟随止损，最长 168 小时，最多 6 仓。
- **日线趋势突破**：UTC 日线收盘突破前 20 日高点且 EMA50 > EMA200，止损 2×日线 ATR（0.5%–25%，止损较宽时自动降低杠杆，保证强平价在止损之外），3×ATR 跟随止损，最长 90 天，最多 6 仓。每天 UTC 0 点后的第一次运行处理新日线。

共同规则与研究回测一致：已收盘 K 线出信号、下一根开盘成交、每笔风险 1%、逐仓 10× / 5×、同向风险 ≤ 5%、每币一仓、UTC+7 日亏 8% 停手、单边 0.05% 手续费 + 0.02% 滑点。每次只处理比上次更新的 K 线，重复运行无副作用，漏跑的小时会自动补上。

结果写入单独的 `paper-data` 分支（`master` 与看板不受影响）：`summary.json`（含各策略统计 `by_strategy`）、`dashboard.json`、`README.md` 汇总表，以及 `combined/` 下的 `state.json`、`trades.csv`、`equity.csv`、`events.jsonl`。可在 Actions 页面手动运行（workflow_dispatch），并可选择 `reset`（清空全部账户重新开始）和 `backfill_days`（新账户从 N 天前开始，用同一套规则逐小时重放到现在再转为实时，最多 90 天；看板会标注补跑区间）。

**OKX 模拟交易跟单**：配置仓库 Secrets `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE`（OKX 模拟交易密钥，只开读取 + 交易）后，`tools/okx_demo_trader.py` 在每次运行时把合并模拟盘的开仓、跟随止损与平仓执行到 OKX 模拟交易账户，结果写入 `paper-data/okx_demo/`。新建时处于暂停状态：在 Actions 手动运行 Paper trading 并选择 `okx_demo = dry-run` 预览，确认后选 `resume` 开始；`flatten` 全部平仓并暂停。连接检查：手动运行 “OKX demo connection check”。

看板 `#/sim`（导航“模拟交易”）和首页（旧主力盘暂停时）显示 OKX 模拟交易账户：读取 `paper-data/okx_demo/summary.json`（约 5 分钟缓存），包括权益、累计 / 今日收益、资金曲线、交易所持仓与最近交易动作。100 USDT 策略模拟盘只在后台运行、作为信号来源，看板不再显示。验收：`PANEL_URL=http://127.0.0.1:8008/ node tests/okx_browser.cjs`。全部仅为模拟资金，用于观察执行与学习。

首页与模拟交易页提供“全部平仓并停止”。公开静态页面通过需要仓库权限的 GitHub Actions 控制任务执行，浏览器不保存交易密钥或 GitHub token：打开提示框 → 前往 GitHub → 在 `stop-demo.yml` 任务页选择 master 并点击 Run workflow。只有这一步才提交停止请求，任务可能排队。
`tools/stop_demo.py` 先持久化暂停新开仓，再关闭本程序管理的全部模拟仓位，读取交易所仓位确认后公布成功状态；平仓失败或无法确认时保留暂停和待管理仓位，并在看板标明结果未确认。人工仓位保持由人工管理。任务与交易程序共享并发组，失败也发布暂停状态和回执；添加按钮不会执行停止操作。恢复需显式在 Paper trading 任务中选择 `resume`。

### OKX 实盘（模式开关，默认 dry-run）

同一个跟单程序 `tools/okx_demo_trader.py` 通过环境变量切换模拟盘 / 实盘：`OKX_TRADING_MODE=demo`（默认，带 `x-simulated-trading: 1`，状态在 `okx_demo/`）或 `live`（不带该请求头，状态在 `okx_live/`）。实盘下 `OKX_DRY_RUN` 默认开，只有精确设为 `0` 才真实下单，新实盘状态还需显式 `resume`；dry-run 时 OKX 客户端拒绝一切非 GET 请求。实盘拒绝在 GitHub Actions 与 `runtime/` 中运行，只能在 API Key 绑定 IP 的新加坡服务器上以 systemd 定时任务运行；交易规则与模拟盘相同（2026-11-15 起：同方向最多 20 单、同方向合计风险不设上限、当天亏损 25% 全部平仓停手；止损、拥挤降风险、杠杆 1–20×、风险 2–10% 不变）。部署、切换与紧急平仓见 [`deploy/live/README.md`](deploy/live/README.md)。

## 账户历史

所有新增内容集中在 `#/paper` 的五个可折叠分区，手机使用卡片列表。

1. **资金曲线与回撤**：`paper/history.json.equity` 完整保留 `paper_main/equity.csv` 的有效观测点，按时间去重，不使用500点采样数据。日 / 周为截至当前最近24h / 7d，全部为完整历史。最大回撤按所选范围从峰值到后续低点的最大百分比计算，显示金额、百分比及峰值 / 低点时间，图上圆点标记起止。当前回撤为所选范围最后观测相对此前峰值的下降；不往权益曲线插入实时估值。
2. **策略战绩**：只取 `trades.csv` 中 `mean_reversion`、`breakout`、`manual` 的已平仓交易。会议单即 manual；突破只在历史中显示。笔数是完整已平仓交易数，不把开仓和平仓重复计数。胜率 = 净盈利笔数 / 有效已平仓笔数（保本计入分母）；盈亏比 = 平均净盈利 / 平均净亏损绝对值；平均与累计盈亏取 net_pnl（兼容 pnl）。手续费 `fee` 是运行器记录的开平仓手续费总额，**不能再加 entry_fee**。毛盈亏使用 gross_pnl，缺失时按 `side × 币数 × (exit−entry)` 重算。手续费占比 = `Σfee / |Σgross_pnl| × 100%`；分母为零或费用缺失显示“—”。没有亏损样本时盈亏比显示“—”。
3. **成交记录**：已平仓完整交易按平仓时间倒序，手机卡片、币种和策略筛选、每页20笔；显示 UTC+7 开平时间、币、方向、杠杆、开平价格、开仓名义金额、净盈亏和保证金收益率。名义金额 = 开仓价 × 币数量；盈亏% = `net_pnl / 入场margin × 100%`，缺少保证金显示“—”。数量统一为币，明确标注 contracts 时按 ctVal × ctMult 换算。
4. **风控仪表**：日亏按 UTC+7 日初权益计算，固定展示距8%熔断的额度使用进度；跨日、缺基准显示缺失。多空分别汇总持仓非负 planned_risk；缺失时用 `|entry−stop| × qty` 重算，除当前权益，与5%上限比较，进度条封顶100%，旁边保留真实比例。每仓止损距离 = `side × (mark−stop)/mark × 100%`，穿越止损可为负。blackout_windows 只输出 start/end/symbols/reason，生效采用 `[start,end)`，进行中显示“禁开中，剩余 …”；否则展示下一个时段倒计时，每秒刷新，列明适用币种，过期窗口不显示。多个窗口重叠时先显示最早结束者，结束后切换到其余进行中窗口。
5. **异动 → 决策记录**：只取 alerts.jsonl 的 `wake=true`，按 wake_index 分组；缺失编号时按 ts 分组，不丢失异动。只公开时间、币种、固定异动类型、匿名会议单状态和最终净盈亏，不发布 detail / note / 聊天内容 / 订单ID。每个异动币和没有关联单子的组均显示“未采纳”。

### 异动引用与关联规则

建议未来在 `decisions.json.manual_orders[]` 增加以下任一可选字段（也兼容决议顶层字段）：

```json
{"symbol":"BTC-USDT-SWAP","side":"long","alert_ref":12}
```

或 `"source_alert":"2026-11-03T00:47:07.596+07:00"`。字段值是原始 `wake_index`（整数或整数字符串）或原始异动 `ts`（带时区时间戳）。这些引用只在生成器内部使用，公开结果只给出 `match=exact|time`。

精确引用优先，并要求同币；引用存在但没有命中时不降级为模糊时间关联。无引用时，匹配同币且决议 updated_at 或订单创建 / 更新时间、状态成交时间位于异动之后 `[0,24h]` 的候选；一个订单只分配给最近一组候选。时间候选不代表因果。旧订单会从主力盘 manual_status 的 order 恢复，避免 decisions.json 替换后丢失。

manual_status 优先用内部订单ID关联；ID不会公开。最终盈亏仅关联 paper_main/trades.csv 中同币 manual 成交的相同内部 order_id；CSV缺ID时，仅使用同币、精确开仓时间等于状态成交时间的记录。没有精确成交对应时盈亏留空，不能把同币其他单的盈亏串过来。净盈亏包括运行器已记录的费用 / 资金费 / 逐仓保险补偿，若运行器之后补记资金费，重新生成快照即更新。

## 白名单与发布

`tools/make_snapshot.py` 与 `tools/history_snapshot.py` 只构建明确字段的新对象，禁止透传源对象。`tools/account.schema.json` 和 `tools/validate_account.py` 验证账户模式、必填数值、合约数量、成交额、窗口时间及敏感内容；schema拒绝额外字段。主力盘持仓新增 nullable 非负 `planned_risk`。

公开的 history 契约为 `{schema_version,account_id,generated_at,historical,equity,trades,alert_groups}`。trade 只含数值、策略枚举、symbol、side、entry_time、exit_time、reason；alert仅固定类型/符号/时间/是否关联，order仅symbol/side/status/time/closed_time/pnl/match。未知平仓原因保留不敏感的短原因码；包含凭证、姓名黑名单、聊天原文或非法格式的原因拒绝发布。对公开字符串先检查完整值再截断。异动事件摘要仅固定类型标签与有限数值，绝不使用 detail 或字符串 value。

团队数据只发布六角色、经过过滤的结构化报告、任务和评级；不发布任意未知字段或群聊原文。公开文本过滤已有姓名黑名单、密钥关键词、长token、邮箱、手机号、账户标识；会议信息和事件窗口原因同样过滤。原始备注、私有ID、错误栈、服务器文件路径和凭证不得进入快照。

静态发布仅包含 `index.html`、`config.js`、`assets/`、`paper/`、`.nojekyll`。不要发布 tools、tests、截图、视频、源CSV或运行环境文件。禁止将密钥、真名、群聊原文写入浏览器资源。`make_snapshot.py` 本身不会 git commit；自动发布由下面的 `publish_snapshot.py` 显式启动。

行情仅使用 OKX 公共 GET 和公开 WebSocket，credentials=omit。CSP 限制固定CDN / OKX端点。前端保留请求限速、重试、过期回退与后台暂停；不调用私有交易接口。所有时间显示 UTC+7；`config.js.startDate` 控制 DAY 起点。支持 reduced-motion、低性能设备和无图表库时的文本数据回退。历史权益图是本地 SVG，不依赖外部图表库。

## 测试与截图

```bash
npm test
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tools/validate_account.py paper/account.json
# 启动本地 http.server 后，最终统一执行一次：
PANEL_URL=http://127.0.0.1:8008/ /home/box/.pw/bin/python tests/history_browser.py
```

单元测试覆盖最大 / 当前回撤、日周范围、战绩、费用分母、原因映射、8% / 5%风险与缺失基准、倒计时边界、同币24h / 精确引用 / 未采纳、匿名状态与最终盈亏、白名单脱敏及源文件只读。删除的旧突破六条件和BT60测试已替换或移除。

统一浏览器验收覆盖首页精简、15行 / 10条展开、选币与1H/4H、账户历史所有分区、筛选 / 分页 / 折叠 / 倒计时、390 / 360px 页面无横向溢出及控制台错误。合成响应只用于隔离交互断言，不写入 paper；最终 `shot-history.png` 使用真实公开历史快照，390×844，首屏截图，已加入 `.gitignore`。此前截图和录屏的忽略规则保留。


暂停与重置读取 `paper_main/PAUSED.json` 的公开字段，归档路径不会进入快照。`reset=true` 且状态文件缺失时，余额/权益取 `next_initial_equity`，成交、资金曲线和持仓为空；日志、异动、团队报告及决议按 `reset_at` 过滤。重置期间不从历史目录回填账户，也不假设旧风控参数。暂停横幅中的 since 使用重置后的有效起点。

### 团队伙伴（悬浮吉祥物）

吉祥物是全站常驻的悬浮伙伴（`assets/mascot.mjs`），不占首屏。`mascotState(snapshot)` 以输入的 `now` 计算优先级：离线、暂停、止损、开仓、异动、盈利、待命；近期事件窗口为 15 分钟。止损只依据事件流（公开成交不含平仓原因）。

- **表情**：每个状态一套眼睛 / 嘴巴 / 配件（`FACES`），如暂停闭眼 + Zzz、离线断网图标、止损流泪、开仓星星眼；天线灯按状态变色。待机呼吸、漂浮、眨眼、随机张望，眼睛跟随指针。
- **交互**：单击打开伙伴面板；双击挥手问候；连点三下会晕；长按比心；拖动到任意位置，松手吸附到最近的左右边缘，位置（边 + 距底部）存 localStorage；面板里“收进边缘”让它半藏在屏幕边上，点一下回来。键盘 Enter 直接打开面板。
- **面板**：`状态`（权益、今日盈亏、持仓、日亏与多空同向风险进度）、`团队`（6 角色最新报告）、`动态`（播报记录 + 最近事件），顶部按当前页面给提示；方向键切换标签，Esc 关闭并把焦点还给伙伴。
- **播报**：新成交、wake 异动、日停手、强平、新团队报告和状态切换会进入气泡队列（最多 5 条，10 秒内同句去重）；用户互动会插队立即回应。视觉气泡 `aria-hidden`，读屏仍通过 `#toasts` 播报。
- reduced-motion、CPU 核数或内存不超过 2、页面隐藏时停止全部动画。

其他界面调整：多条数据报错合并为一条可展开的提示；暂停横幅缩小；暂停期资金曲线提示“重新开始后从 100 USDT 起记录”；进度条在浅色主题下显示底轨。

伙伴验收：`PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs`，覆盖全局悬浮、表情、问候、单击 / 方向键 / Esc / Enter、双击、连点、长按、拖动吸边与刷新恢复、收进边缘、页面提示、新成交播报、报告转义、动画降级及 320 / 360 / 390px 无横向溢出。


### 浅色首页与币种图标

主题集中在 `assets/style.css` 的 CSS 变量：浅灰底、白卡、浅灰边框和轻阴影，无玻璃滤镜与光斑。正文、次要文字、涨绿跌红及强调色按 WCAG AA 4.5:1 检查；K线和权益图均使用浅色背景与网格。

币种图标来自 [spothq/cryptocurrency-icons](https://github.com/spothq/cryptocurrency-icons)，采用 [CC0 1.0 Universal](https://github.com/spothq/cryptocurrency-icons/blob/master/LICENSE.md)，下载脚本固定上游提交，许可副本随仓库存于 `assets/coins/LICENSE.md`。页面只读本地 `assets/coins/<symbol>.svg`，固定26px、lazy加载。SPCX、INTC、XAU等缺图标币种按符号哈希生成彩色圆底及首字母；图片加载失败同样回退，不请求外部图标CDN。

```bash
python3 tools/fetch_icons.py                            # 当前公开扫描/持仓币池；已有缓存不重复下载
python3 tools/fetch_icons.py --universe /path/to/universe.json  # 支持 symbols 数组或 instId 合约列表
npm test
python3 -m unittest discover -s tests -p 'test_*.py'
PANEL_URL=http://127.0.0.1:8008/ /home/box/.pw/bin/python tests/home_browser.py
```

`tests/home_browser.py` 验证权益卡/区间、单行展开/收起与键盘操作、复用K线/价位线、刷新保留展开状态、缺图标回退及320–1440px无溢出。产出390×844浅色截图 `shot-home.png`（真实暂停快照）、`shot-home-demo.png`（`tests/fixtures/home.json` 的2个假持仓，展开一行）与10秒以内 `rec-home.webm`（区间切换、展开持仓、点击吉祥物、打开报告抽屉）。演示数据仅通过Playwright响应拦截注入，不写入公开数据，产物均已加入 `.gitignore`。
