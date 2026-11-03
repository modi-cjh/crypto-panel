# MODI · AI 交易团队

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

首屏只保留一个 SVG 团队吉祥物，`mascotState(snapshot)` 以输入的 `now` 计算优先级：离线、暂停、止损、开仓、异动、盈利、待命；近期事件窗口为 15 分钟。机器人在预留区域内拖动，位置保存到 localStorage；点按切换表情和真实状态气泡，“团队报告”打开可用 Esc 关闭的底部抽屉。reduced-motion、CPU 核数或内存不超过 2 时禁用循环动画。

新增验收：`PANEL_URL=http://127.0.0.1:8008/ /home/box/.pw/bin/python tests/mascot_browser.py`，覆盖重置空页、鼠标/触摸、拖动与点击、位置恢复、收起、报告、Esc 和动画降级。生成真实公开快照下的 `shot-mobile.png`、`shot-history.png`、`shot-mascot.png`、`shot-drawer.png`（均 390×844）及不超过 8 秒的 `rec-mascot.webm`；这些文件全部忽略，不写入 Git。


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
