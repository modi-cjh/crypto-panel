# 更新日志

基线版本：`cc25dc5`（v3：浅色主题、权益首页、持仓列表与图标、吉祥物、暂停重置）。
以下改动全部通过 PR #1 – #17 合并到 `master`（2026-11-03，时间为 UTC+7）。

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

---

## 当前三个模拟账户

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
npm test                                             # JS 单元测试（69 项）
python3 -m unittest discover -s tests -p 'test_*.py' # Python 单元测试
python3 -m http.server 8008 --bind 127.0.0.1 &      # 浏览器验收前启动本地服务
PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs
PANEL_URL=http://127.0.0.1:8008/ node tests/sim_browser.cjs
```

已知情况：`tests/team_browser.cjs`、`tests/browser_smoke.cjs` 在基线 `cc25dc5` 上就已失败（仍查找 v3 删除的角色卡），本次改动未改变其结果。
