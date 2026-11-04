# 更新日志

基线版本：`cc25dc5`（v3：浅色主题、权益首页、持仓列表与图标、吉祥物、暂停重置）。
以下改动全部通过 PR #1 – #25 合并到 `master`（2026-11-03，时间为 UTC+7）。

---

## 给在本地 / 云电脑上继续开发的人（Codex 等）：先读这一段

1. **先同步，再改代码。** 本地副本若停在 `cc25dc5` 或更早，请先：
   ```bash
   cd /workspace/crypto-panel
   git status                      # 有未提交改动就先 commit 或 git stash
   git pull --rebase origin master # 有冲突逐个解决
   ```
   可能冲突的文件：`assets/app.mjs`、`assets/mascot.mjs`、`assets/style.css`、`index.html`、`README.md`。
2. **任何情况下都不要 `git push --force` 到 `master`。** 推送被拒绝说明远端有新提交，先 pull 再推。强推会抹掉下面所有改动。
3. **不要修改 `paper-data` 分支。** 它由 GitHub Actions 每小时自动提交（学习用模拟交易数据），与 `master` 无关。
4. **数据契约没有变化。** `tools/make_snapshot.py`、`tools/history_snapshot.py`、`tools/validate_account.py`、`tools/account.schema.json`、`paper/*.json`、`config.js` 自基线以来**一行未改**。crypto-bot 恢复运行后按原方式生成快照即可，看板原有部分照常工作。
5. **交易程序 crypto-bot 未被访问或修改。**

---

## 汇总

| 范围 | 内容 |
|---|---|
| 修复 | 会议名误判币种导致 WebSocket 整体失效、标记价覆盖最新价、已停止账户误用实时价、历史页风险权益计算、死代码 / 多余请求 |
| 新功能 | 悬浮团队伙伴（表情、播报、独立面板）、自动发布快照脚本、学习用模拟交易（GitHub Actions 每小时三账户）、看板“模拟交易”页面 |
| 体验 | 报错合并、暂停横幅缩小、拖动流畅度、触摸设备拖动、点按 / 焦点黑框 |
| 测试 | JS 69 项、Python 全部通过；新增 3 个浏览器验收脚本 |

---

## 逐个 PR

### PR #1 代码审查修复
- `assets/scan.mjs`：`meetingSymbol(meeting, fallback, known)` 新增第三个参数。会议名里的裸词（如 “P3”“RSI”“FOMC”）只有在已知合约集合中才会被当作焦点币，避免订阅不存在的 instId 让共享 WebSocket 报错并永久退回轮询。首次拿到行情后重新同步焦点币。
- `assets/app.mjs`：
  - mark-price 推送不再写入 `ticker.last`（曾导致 24h 涨跌幅失真）；只有 tickers 频道更新最新价。
  - WebSocket 推送触发的重绘合并为每帧一次（`scheduleLiveRender`）。
  - 已停止账户的持仓固定使用归档标记价。
  - 删除首页未使用的 `refreshTechnical` 请求。
- `assets/history.mjs`：风险权益改为复用 `isolatedPnL`（保证金为 null、数量单位为张时也正确）。
- `assets/mascot.mjs`：删除永远不成立的 `account.trades` 止损判断（公开 fills 不含平仓原因，止损靠事件流）。

### PR #2 自动发布快照
- 新增 `tools/publish_snapshot.py`：在运行模拟盘的机器上定时执行 `make_snapshot.py`，只提交 `paper/*.json` 并推送到 GitHub Pages 分支。先 `pull --rebase` 再推送，失败 2/4/8/16 秒重试；生成或校验失败、数据无变化都不提交；不在目标分支时拒绝运行；默认 600 秒，最小 360 秒（Pages 每小时约 10 次构建的软上限）。
  ```bash
  python3 tools/publish_snapshot.py --dry-run --once   # 先试
  python3 tools/publish_snapshot.py --once
  nohup python3 tools/publish_snapshot.py > publish.log 2>&1 &
  ```
- 新增 `tests/test_publish.py`。

### PR #3 吉祥物改为悬浮团队伙伴；界面优化
- `assets/mascot.mjs` 重写，`#mascot` 从首页卡片移到 `<body>` 下全站悬浮（`index.html` 中位于 `<main>` 之后，`#mascot-drawer` 对话框内容由脚本生成）。
  - 表情系统 `FACES`：每个状态一套眼睛 / 嘴巴 / 配件；天线灯按状态变色；呼吸、漂浮、眨眼、随机张望，眼睛跟随指针；离线不再灰掉。
  - 交互：单击开面板、双击挥手、连点三下眩晕、长按比心；拖动后吸附到最近左右边缘，位置存 `localStorage` 键 `modi-companion-position`；面板“收进边缘”。
  - 面板三个标签：状态 / 团队（`#mascot-report-list`、`#team-updated`）/ 动态，顶部按页面给提示（`contextTip`）。
  - 气泡播报：新成交、wake 异动、日停手、强平、新报告、状态切换；队列最多 5 条、10 秒同句去重；用户互动插队。视觉气泡 `aria-hidden`，读屏仍走 `#toasts`。
  - 新导出纯函数：`faceFor`、`snapPosition`、`fillLine`、`eventLine`、`reportLine`、`contextTip`、`pushMessage`。
- `assets/app.mjs`：`notify()` 同时发 toast 与伙伴播报；`updateCompanion()` 在所有路由更新伙伴上下文；多条报错合并为一条可展开提示；暂停期资金曲线提示文字。
- `assets/style.css`：删除旧吉祥物样式，新增伙伴与面板样式；暂停横幅缩小；浅色主题下进度条底轨可见。
- 测试：`tests/mascot.test.mjs` 扩充；新增 `tests/mascot_browser.cjs`（Node Playwright），**删除旧的 `tests/mascot_browser.py`**；`tests/home_browser.py` 改为新的打开方式。

### PR #4 修复手机上无法拖动
- 机器人上阻止原生触摸手势（iOS 长按菜单 / 选择 / 缩放会触发 `pointercancel` 中断拖动）；单击改在 `pointerup` 判断，`click` 只保留键盘激活；系统中途取消手势时仍吸边。

### PR #5 拖动流畅度
- 性能分析显示拖动时主要耗时是重绘 SVG 待机动画：拖动期间暂停动画（同样拖动主线程时间约 650ms → 210ms），位置改用 `transform: translate3d` + `will-change`，pointermove 按帧合并，拖动中跳过眼睛跟随的布局读取；按住时轻微放大。

### PR #6 / #7 / #8 点击后的黑框
- #6：指针操作后隐藏焦点框（依赖脚本加类）。
- #7：关闭 iOS Safari 的深色点按高亮（`-webkit-tap-highlight-color: transparent`）。
- #8：逻辑反转为**焦点框默认不显示，只有按过键盘才显示**（`html.companion-keyboard`），即使浏览器缓存了旧脚本也不会再出现黑框。

### PR #9 学习用模拟交易（GitHub Actions）
- 新增 `tools/paper_trader.py`（仅标准库）：只读 OKX 公共 K 线，无密钥、不下单；逐根重放新的已收盘 K 线，重复运行无副作用，漏跑自动补上。
- 新增 `.github/workflows/paper-trading.yml`：每小时 UTC 第 7 分钟运行（可手动 workflow_dispatch），结果提交到 **`paper-data` 分支**：`summary.json`、`README.md`、每个账户的 `state.json` / `trades.csv` / `equity.csv` / `events.jsonl`。
- 共同规则：已收盘出信号、下一根开盘成交、每笔风险 1%、逐仓 10×/5×（24h 成交额 ≥ 5000 万）、同向风险 ≤ 5%、每币一仓、UTC+7 日亏 8% 停手、单边 0.05% 手续费 + 0.02% 滑点、亏损以保证金封顶。
- 新增 `tests/test_paper_trader.py`。

### PR #10 日线趋势突破账户
- 第三个账户 `breakout_daily`：UTC 日线 20 日新高 + EMA50 > EMA200 做多，止损 2×日线 ATR（0.5%–25%，止损宽时自动降杠杆保证强平价在止损之外），3×ATR 跟随止损，最长 90 天，最多 6 仓。

### PR #11 看板接入模拟交易
- 新增 `#/sim` 页面（导航“模拟交易”）与首页摘要卡片 `#sim-summary`；新增 `assets/sim.mjs`。
- 数据来自 `https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/dashboard.json`（约 5 分钟缓存）；**CSP `connect-src` 增加 `https://raw.githubusercontent.com`**。
- `tools/paper_trader.py` 额外输出 `dashboard.json`（资金曲线降采样至 720 点、最近 30 笔成交、持仓数量与保证金）。
- 该页面隐藏旧主力盘的“已暂停”横幅。
- 新增 `tests/sim.test.mjs`、`tests/sim_browser.cjs`、`tests/fixtures/sim-dashboard.json`。

### PR #12 更新日志
- 新增本文件；README 顶部加链接。

### PR #13 伙伴机器人改为播报模拟盘
- 模拟盘数据（`dashboard.json`）加载成功后，伙伴的状态、面板与动态改为跟随三个模拟账户；加载前或从未加载成功时沿用旧主力盘快照逻辑。
- `assets/sim.mjs` 新增 `simMascotState`（优先级：数据超过 3 小时未更新 → 离线；任一账户日亏停手 → 停手；近 3 小时亏损平仓 → 刚止损；近 3 小时开仓 → 刚开仓；合计盈利 → 盈利中；其余待命）、`simChanges`（新旧数据对比得到开仓 / 平仓含盈亏 / 停手播报）、`simFeed`。
- 伙伴面板“状态”显示合计权益、合计收益、持仓、已平仓及每个账户概况；“动态”显示模拟盘开平仓；页面提示改为模拟盘摘要。
- 模拟盘数据改为在所有页面每 60 秒刷新（含切回前台时），新开平仓同时进入 toast（读屏）与伙伴气泡。
- 测试：`tests/sim.test.mjs` 新增状态优先级与变化播报用例；`tests/sim_browser.cjs` 验证伙伴跟随模拟盘并播报平仓；`tests/mascot_browser.cjs` 屏蔽模拟盘数据以继续覆盖旧逻辑兜底。

### PR #14 首页以模拟盘为主
- 当原主力盘（crypto-bot）暂停、停止或快照过期，且模拟盘数据已加载时（`html.sim-primary`）：隐藏“已暂停”横幅；首页显示模拟盘合计权益、对齐后的三条资金曲线、三个账户卡片、当前持仓与待成交、最近 10 笔平仓；原主力盘各区块收起，可用“显示原主力盘”按钮展开；顶栏常驻余额改为“模拟盘合计”。
- crypto-bot 恢复运行、快照重新新鲜后自动回到原首页，无需改代码。
- `index.html` 顶栏标签改为 `#pinned-label-text`；新增 `#home-sim` 区块。
- `assets/sim.mjs`：新增 `simPendingText`；`simCurveSVG` 从所有账户都有数据的时刻开始画（日线账户首个点比小时账户早一天），之前的值顺延。

### PR #15 模拟盘改为 5 个币，支持重置与历史补跑
- 交易范围改为 BTC、ETH、SOL、XRP、DOGE。
- `tools/paper_trader.py` 新增 `--reset`（清空全部账户）与 `--backfill-days N`（新账户从 N 天前开始、逐小时重放到现在后转实时，最多 90 天；只对全新账户生效）；状态与汇总新增 `started`、`live_since`。
- 工作流 `workflow_dispatch` 新增 `reset`、`backfill_days` 输入（通过环境变量传参）。
- 看板：`simLiveSince`、`simBackfillNote` 标注补跑区间，资金曲线在实时开始处画竖线。
- 测试：补跑结果与逐小时运行一致、`live_since` 标注、重置清空、已有账户不重复补跑；前端补跑标注用例。

### PR #16 定时改为每 30 分钟
- 工作流 cron 改为 `7,37 * * * *`：GitHub 定时任务常延迟或跳过，重复运行只处理新的已收盘 K 线，因此不会重复记账。

### PR #17 待成交单在下一根 K 线开盘即记录
- 信号出现后，只要下一根 K 线已开盘（`market/candles` 中未收盘的那根），就按其开盘价立即记录成交，持仓更早出现在看板上；该 K 线的止损 / 止盈仍在收盘后判断。结果与收盘后再记录完全一致（有测试保证），重复运行不会重复开仓。

### PR #18 首页持仓实时浮盈
- 首页模拟盘持仓卡片随 OKX 行情（每 8 秒）刷新现价与浮动盈亏，此前要等下一次读取模拟盘数据才更新。

### PR #19 连不上 OKX 时也能看到持仓浮盈
- 部分网络（如中国大陆）浏览器无法访问 OKX，看板拿不到实时价。模拟盘每次运行时在 GitHub 服务器上读取持仓币种最新价（`market/ticker`），把 `mark` 与 `unrealized`（以保证金封顶）写入 `summary.json` / `dashboard.json`。
- 看板优先用浏览器实时价（标注“现价（实时）”），拿不到时显示模拟盘记录的价格（标注“最新价（时间）”）。

### PR #20 账户权益随行情实时变化
- 模拟盘汇总新增 `cash`（已实现余额）。看板用“现金 + 持仓实时浮盈”计算各账户与合计权益（顶栏、首页合计、账户卡片、伙伴面板），随行情每 8 秒更新；缺少 `cash` 或价格时回退到记录的权益。新增 `liveEquity`。

### PR #21 三套策略合并为一个账户
- `tools/paper_trader.py` 改为单一合并账户（`combined/`，100 USDT）：三套策略共享余额、同向风险上限（5%）和日亏停手（8%）；一个币同时只持一个仓，同一根 K 线多个信号按 均值回归 → 1H 突破 → 日线突破 的顺序；每笔持仓 / 成交 / 事件记录 `strategy`。
- 时间轴按小时：1H 策略在每根 1 小时 K 线收盘后管理与出信号；日线策略在 UTC 日线收盘（当天最后一小时收盘）后管理持仓、出信号，次日开盘成交。
- 汇总新增 `by_strategy`（各策略已平仓、胜率、平均 R、盈亏、持仓）与 `pending_detail`；`dashboard.json` 的成交与持仓带 `strategy_name`。
- 看板：单账户卡片内含各策略表格；持仓 / 成交 / 播报标注来源策略；顶栏“模拟盘权益 · 合并账户”；伙伴面板按策略列出。
- 测试：Python 用例改为合并账户（含日线收盘管理与次日成交）；浏览器 fixture 用合并账户离线重放重新生成。
- 合并后以 `reset=true`、`backfill_days=30` 重新开始。

### PR #22 顶栏权益不再无故跳动
- 合并账户模式下，顶栏数字每次刷新会先被旧主力盘的 100.00 写一遍、再被模拟盘权益覆盖，导致数字没变也触发滚动动画。改为同一时刻只由一个来源写入。浏览器验收新增“价格不变时 20 秒内不出现滚动动画”。

### PR #23 权益小幅变动不再播放滚动动画
- 持仓浮盈随 8 秒行情轮询每次变动几分钱，导致权益数字每 8 秒滚动一次。`rollNumber` 新增 `minStep`：模拟盘权益变动小于 0.10 USDT 时静默更新数字，达到 0.10 USDT 才播放动画。

### PR #24 OKX 模拟交易连接检查（只读）
- 新增 `tools/okx_demo_check.py` 与手动工作流 `.github/workflows/okx-demo-check.yml`：用仓库 Secrets（`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE`）带 `x-simulated-trading: 1` 签名请求，检查密钥是否被模拟交易接受、权限（开启提现或缺少交易则失败）、账户模式与 USDT 余额。不下单；密钥经 stdin 传给 curl，不出现在命令行或日志中。

### PR #25 OKX 模拟交易跟单
- 新增 `tools/okx_demo_trader.py`：把合并模拟盘的决策执行到 OKX **模拟交易**账户（所有请求带 `x-simulated-trading: 1`）。模拟盘 2 小时内的新仓位按市价开仓（逐仓、杠杆 ≤ 10×、按模拟账户权益 1% 风险定量），止损 / 止盈作为交易所条件单附带；模拟盘止损上移时同步修改交易所止损；模拟盘平仓时平仓；交易所止损已触发的不再重开。
- 保护：新建时默认暂停（先 `dry-run` 预览，再 `resume`）；只操作自己开的仓；UTC+7 日亏 8% 全部平仓并停止开仓至次日；`pause` / `resume` / `flatten`（全部平仓并暂停）；拒绝现货模式或带提现权限的密钥。
- 工作流 `paper-trading.yml` 在模拟盘之后执行跟单（失败不影响模拟盘提交），手动运行可选 `okx_demo` 动作；结果写入 `paper-data` 分支 `okx_demo/`（`state.json`、`actions.jsonl`、`summary.json`）。

### PR #26 看板接入 OKX 模拟交易结果
- 首页模拟盘区块与“模拟交易”页新增“OKX 模拟交易 · 交易所实测”卡片，读取 `paper-data/okx_demo/summary.json`（随模拟盘每分钟刷新）：状态（运行中 / 已暂停 / 日亏停手 / 数据延迟）、权益、今日盈亏、可用资金、交易所持仓（非本程序开的仓位会标注）、最近 8 条跟单动作（开仓、平仓、上移止损、交易所止损触发、跳过原因等，中文说明）。
- 摘要文件不存在或格式不对时卡片保持隐藏；交易所返回的文字全部转义。新增 3 个单元测试，浏览器验收覆盖卡片内容。

### PR #27 看板只显示 OKX 模拟交易账户
- 看板不再显示 100 USDT 模拟盘账户：首页、顶栏权益、“模拟交易”页和机器人播报全部改为 OKX 模拟交易账户（`paper-data/okx_demo/summary.json`）。删除 `assets/sim.mjs` 及其测试与样例数据，新增 `assets/okx.mjs`。
- 策略引擎 `tools/paper_trader.py` 仍在后台每 30 分钟运行，只作为三套策略的信号来源（OKX 跟单按它的开平仓执行），`dashboard.json` 照常生成，但看板不再读取。
- 首页：OKX 模拟权益（浮盈按实时价与合约面值计算）、累计收益（相对起始权益）、今日盈亏、运行状态、资金曲线、持仓（含策略名与交易所止损）、最近交易动作。完整页面多了可用资金、已平仓笔数和最近 30 条动作。
- 机器人跟随 OKX 账户：刚开仓兴奋、交易所止损触发难过、暂停 / 日亏停手、数据超过 3 小时未更新为离线；新的开仓 / 平仓 / 止损触发会弹出提示。
- `okx_demo_trader.py` 摘要新增 `start_equity`、`history`（每次运行记录一个权益点，最多 3000 个）、`closed_trades` 与持仓 `ctVal`。
- 测试：`tests/okx.test.mjs`（7 项）、`tests/okx_browser.cjs`（替代 `sim_browser.cjs`）、Python 新增 1 项。

### PR #28 运行频率提到约每 5 分钟
- `paper-trading.yml` 定时改为 `2-59/5 * * * *`（GitHub 允许的最短间隔，实际常有延迟）。策略只看已收盘 K 线，重复运行无副作用；更频繁的运行让 OKX 模拟账户的持仓、权益和成交在看板上更快更新（交易所止损 / 止盈本来就是实时触发）。
- 资金曲线每 15 分钟存一个点（最多 3000 个，约 31 天），摘要里的曲线末尾总是最新一次运行的权益。
- 看板：数据超过 90 分钟未更新即显示“数据延迟”（原 3 小时），文案改为“约每 5 分钟更新”。

### PR #29 市场行情扩展到前 100
- 市场行情页由成交额前 30 改为前 100（`MARKET_SIZE`）。行情与持仓量本来就是整批接口，请求数不变；资金费率是逐个合约请求，前 30 名每 60 秒刷新、第 31–100 名每 3 分钟刷新，并继续走共享限速队列（每 150 ms 一个、最多 3 个并发），不超过 OKX 公共接口限额。
- 新增浏览器验收 `tests/market_browser.cjs`：120 个模拟合约只显示前 100、资金费率全部到齐且 2 秒内请求数 ≤ 20、搜索、320/390 无溢出。

### PR #30 手动跟进当前持仓
- Paper trading 手动运行新增 `okx_demo = follow-current`：把策略中因超过 2 小时而被跳过的现有仓位按当前价在 OKX 模拟交易开仓。止损已被越过、已到止盈、仓位过小或保证金不足时仍会跳过；止损 / 止盈仍按策略价位挂在交易所。看板动作显示为“手动跟进当前持仓”。

### PR #31 价格按交易所精度显示
- OKX 持仓的开仓均价、止损、标记价改为约 5 位有效数字（BTC 70,000.1 · XRP 1.2595 · DOGE 0.18123），不再把 XRP 1.2595 显示成 1.260、止损 1.2918 显示成 1.292。

### PR #32 浮动盈亏改用 OKX 模拟盘自己的标记价
- OKX 模拟交易是独立的市场，价格和真实盘不同（例：XRP 模拟盘标记价 1.2686，真实盘 1.2811）。看板之前用真实盘行情估算模拟仓位的浮盈，与 OKX App 相差很大。现在订阅模拟盘公共 WebSocket（`wss://wspap.okx.com:8443/ws/v5/public`，已加入 CSP）的 `mark-price`，按 OKX 自己计算未实现盈亏的同一价格估算；30 秒内没有模拟盘标记价时显示上一次运行记录的数值。
- 浏览器验收用模拟的 WebSocket 推送验证：持仓按模拟盘标记价计算浮盈、顶栏权益随之更新。

### PR #33 顶栏连接状态不再卡在“离线”
- 右上角状态只在 WebSocket 状态变化或旧主力盘快照加载时重算，手机从后台回来或 WebSocket 较慢时会一直显示“离线”，即使 8 秒行情轮询正常。现在每次行情刷新（成功后和每 8 秒）都重算；OKX 模拟盘标记价推送也算作“实时”。

### PR #34 模拟盘标记价 REST 备用 + 连接诊断
- iPhone Safari 上看板一直“离线”、浮盈停在上次运行的数字（同一手机的 Chrome 正常），说明该 Safari 的设置拦截了 OKX 连接（常见为内容拦截器、iCloud 专用代理或锁定模式）。新增备用：模拟盘 WebSocket 没有数据时，每 8 秒用 REST 一次取全部模拟盘标记价（`/public/mark-price` + `x-simulated-trading: 1`，OKX 对本站允许此标头跨域）。
- 点右上角连接状态打开“连接诊断”：版本、浏览器、行情 REST / 两个 WebSocket 的状态与最近事件、脚本报错；“测试连接”分别直连 REST、模拟盘 REST、真实盘 WS、模拟盘 WS 并显示结果，可复制。不含密钥或账户信息。
- 新增 `tests/okx_rest_browser.cjs`：WebSocket 全部失败时按模拟盘 REST 标记价计算（用 OKX App 截图数值验证：1.2703 → -17.06 USDT）、诊断面板可用。

### 直接推送到 master 的更新（2026-11-04，未经 PR，此处补记）
- `22b5a6e` 24 小时增量测试（`tools/volume_test.py`）：风险 2%、10 币、RSI 30/70、小时突破 24 小时，到期自动恢复。
- `a9e4038` / `24d7c44` 一次性小额 BTC 模拟成交测试（`tools/okx_demo_smoke.py`、`okx-demo-smoke.yml`），看板显示为“模拟成交测试”；新增只读 `snapshot` 动作。
- `7ee53ee` “全部平仓并停止”：看板按钮只打开 GitHub `stop-demo.yml` 任务页，需登录后手动 Run workflow；`tools/stop_demo.py` 先暂停、再平仓、读交易所确认后发布回执。浏览器不保存密钥或 token。
- `3883fad` 伙伴状态与最近一次已确认开 / 平仓保持一致。
- `cc14b35` 激进档（11/04 05:20–11/05 05:20 UTC+7）：风险 3%、同向上限 6%、前 100 成交额扫描（15 个基础币）、RSI 35/65、趋势 3%、小时突破 12 小时；`start-aggressive-demo.yml` 启动。日亏 8%、10 倍杠杆上限、交易所止损保留。
- `5723b7c` 可选常驻运行环境 `runtime/`（FastAPI + systemd + Caddy 示例，见 `deploy/README.md`）：PBKDF2 管理员密码、httpOnly + SameSite=strict 会话、CSRF、Origin 校验、登录限速、幂等控制命令；密钥只在服务器环境变量，`admin configure` 用 getpass 读取。启动前确认 GitHub 交易工作流已停用，避免两处同时下单。伙伴新增“异动 / 决策”页。
- `4d3570f` / `90f05ba` / `2b3ab10` 模拟盘行情连接与交易权限、快照时间分开显示；WebSocket 卡住时自动恢复；改用标准端口 `wss://wspap.okx.com`（已加入 CSP），失败时用校验过的 REST 备用；新增 `quote-connection-check.yml` 诊断。

### PR #35 同步上述更新
- `tests/mascot_browser.cjs` 跟随新增的“异动 / 决策”标签（位于“团队”与“动态”之间）。复核：JS 83 项、Python 124 项（含 `runtime` 测试，需 `pip install -r runtime/requirements-test.txt`）、4 个浏览器验收全部通过。


### PR #36 诊断日志只记录状态变化
- 行情流每条消息都会上报一次状态，诊断面板的“最近事件”被同一行“实时”刷满。现在相同的连续事件只记一次。


### PR #37 看板显示 OKX 模拟账户里的全部 USDT 永续持仓
- 跟单程序只读取它交易的币种，所以在 OKX App 里手动开的其他币仓位不会出现在看板。现在摘要列出全部 USDT 本位永续持仓（非本程序的标为“非本程序仓位”），币本位合约不显示；下单、改止损、平仓仍只针对本程序管理的仓位，“全部平仓并停止”也不会动手动仓位。


### PR #38 界面整体优化
- 首页第一屏只回答“账户现在怎么样”：大号权益、累计 / 今日盈亏、运行状态；激进档、执行调度、快照提示三块合并成一行摘要（“快照 X 分钟前 · 行情实时 · 激进档 3%”），点开看全文，延迟时变成橙色。资金曲线紧跟其后；宽屏时数字与曲线左右并排。
- 持仓改为紧凑行：币种、方向与杠杆、来源、浮盈（大号红绿）一行显示，点开看张数、开仓均价、标记价、止损、保证金、收益率；刷新行情时保持展开状态。首页默认收起，“模拟交易”页默认展开。
- 交易动作分开：首页“最近成交”和“模拟交易”页“成交记录”只列开仓、平仓、止损触发、止损上移、日亏停手、出错，并用颜色点区分；暂停、恢复、跳过的信号收进可展开的“运行记录”。字体改为正文字体，更易读。
- “全部平仓并停止”移到“模拟交易”页底部的“停止交易”区，改为低调的描边按钮（点开仍有说明对话框，需到 GitHub 执行）。
- 顶栏在 OKX 模式下只显示两行：权益 + “今日 / 累计”。手机首次访问时机器人默认收在屏幕边缘，点一下出来；之后保留用户的选择。手机页面底部留白，避免最后内容被机器人挡住。
- 资金曲线坐标文字放大、不再被裁切；页脚说明更新为当前数据来源。
- 只改界面，不涉及交易逻辑。浏览器验收随新布局更新（持仓折叠 / 展开、成交与运行记录分开、停止按钮位置、机器人首次收边）。


### PR #39 数字变化更柔和
- 权益数字原来每次变化都整串从下方滑入（位移 65%）并整体变绿 / 变红 1.4 秒。现在只有变化的那几位数字淡入、上移 2px，并带约 1 秒逐渐褪去的红绿色；前面没变的数字保持不动。变化小于 0.10 USDT 仍不播放动画，系统“减少动态效果”时直接更新。


### PR #40 持仓列表不再整体闪烁
- 行情每秒推送多次，持仓列表每次都被整块重画，币种图标（延迟加载的图片）重新加载，所有行一起闪。现在持仓不变（同样的币与顺序）时只就地更新文字有变化的部分（浮盈、详情），行与图标保持不动；开仓 / 平仓时才整块重画，展开状态保留。实测真实模拟盘行情 20 秒内浮盈更新 192 次，行与图标重建 0 次。


### PR #41 行情推送不再让整页反复重绘
- PR #40 只处理了持仓行，但模拟盘标记价每秒推送约 10 次，每次都会重绘整个账户区：30 秒内资金曲线重画 305 次、成交列表与运行记录各重建上千次、权益数字动画 211 次（约每秒 7 次）。
- 现在：行情推送引起的重绘最多每秒一次；资金曲线、成交、运行记录、提示、状态等只在内容真的变化时才写入；权益数字同一位置最多每 3 秒播放一次动画，其余时间静默更新数字。
- 实测真实行情 30 秒：曲线、列表、提示重建 0 次，权益动画 9 次，持仓浮盈约每秒更新一次。浏览器验收新增“2 秒内 10 次推送后曲线 / 成交 / 持仓元素不变、权益动画不超过一次、数值收敛到最新价”。

---

## 策略引擎（自 PR #21 起为一个合并账户；自 PR #27 起只作为 OKX 模拟交易的信号来源，看板不再显示）

| 键 | 名称 | 周期 | 规则 |
|---|---|---|---|
| `mean_reversion` | 均值回归 | 1H | 看板 `indicators.mjs` 入场规则，RSI 25 / 趋势 2%；止损 1.5×ATR（0.5%–4%），止盈 20 根中轨，最长 24 小时 |
| `breakout_long` | 趋势突破只做多 | 1H | 48 小时新高 + EMA50 > EMA200；止损 2×ATR，3×ATR 跟随，最长 168 小时，最多 6 仓 |
| `breakout_daily` | 日线趋势突破 | 1D (UTC) | 20 日新高 + EMA50 > EMA200；止损 2×ATR，3×ATR 跟随，最长 90 天，最多 6 仓 |

交易范围（自 PR #15 起）：BTC ETH SOL XRP DOGE（USDT 永续）。各从 100 USDT 起步，从 2026-10-04 起按小时补跑 30 天后转为实时（看板标注补跑区间）。PR #15 之前曾以 23 个币从 2026-11-03 13:00 UTC 开始记录，该段数据保留在 `paper-data` 分支历史中。

回测参考（仅供学习，不构成投资建议）：两套 1H 规则在 2025-11 → 2026-11 均亏损；日线突破 2024-02 → 2026-11 回测 +31.6%、最大回撤约 20%，但只有 92 笔，样本有限。日收益 1% 的目标在约 1100 组参数（含不限杠杆 / 风险）的前后段验证中均未找到可持续方案。

---

## 测试命令

```bash
npm test                                             # JS 单元测试（83 项）
python3 -m unittest discover -s tests -p 'test_*.py' # Python 单元测试
python3 -m http.server 8008 --bind 127.0.0.1 &      # 浏览器验收前启动本地服务
PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs
PANEL_URL=http://127.0.0.1:8008/ node tests/okx_browser.cjs
PANEL_URL=http://127.0.0.1:8008/ node tests/market_browser.cjs
PANEL_URL=http://127.0.0.1:8008/ node tests/okx_rest_browser.cjs
```

已知情况：`tests/team_browser.cjs`、`tests/browser_smoke.cjs` 在基线 `cc25dc5` 上就已失败（仍查找 v3 删除的角色卡），本次改动未改变其结果。
