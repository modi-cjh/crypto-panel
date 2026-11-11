# AGENTS.md — 给 Codex、Claude 等编码助手的工作说明

本仓库是 **MODI 交易看板**：GitHub Pages 静态网站（`index.html` + `assets/*.mjs`），展示 **OKX 模拟交易**账户、市场行情和 MODI 机器人伙伴；交易程序由 GitHub Actions 定时运行，结果写到 `paper-data` 分支供看板读取。多个助手（Codex、Claude）会先后在这里工作，请先读完本文件再动手。

## 1. 安全红线（任何情况下都不能违反）

- **只做模拟交易。** 所有发往 OKX 的私有请求必须带 `x-simulated-trading: 1`，统一使用 `tools/okx_demo_trader.py` 里的 `OKX` 客户端，不要另写下单代码，不要接入实盘。
- **密钥只放在 GitHub Secrets**（`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE`）或服务器环境变量。不要写进代码、日志、提交、PR、测试数据或聊天；curl 通过 stdin（`-K -`）传参，不放在命令行。
- **拒绝带提现权限的密钥**（`check` / `load` 已实现，不要去掉）。
- **不要动人工仓位。** 跟单程序只管理自己开的仓（`state.json` 的 `mirrored`）；看板可以显示全部 USDT 永续持仓，但平仓、改止损、“全部平仓并停止”只作用于程序自己的仓位。
- **网页里不放任何密钥或 GitHub token。** 停止交易等控制操作通过 GitHub Actions 任务页（需要登录）或 `runtime/` 常驻服务（密码 + CSRF）完成。
- **常驻服务与 GitHub 定时任务不能同时执行交易**（会重复下单）。`runtime/engine.py` 的 `Authority` 会检查，不要绕过。

## 2. 协作流程

- **先同步再改：** `git pull --rebase origin master`。
- **改动走分支 + PR，不要直接推 `master`，绝对不要 `git push --force` 到 `master`。** 两个助手都直接推 master 时最容易互相覆盖。
- **不要手动修改 `paper-data` 分支。** 它只由工作流写入（`combined/` 策略状态、`okx_demo/` 跟单状态与看板摘要）。需要重置或补跑时用 `paper-trading.yml` 的手动参数。
- **每个改动都在 `CHANGELOG.md` 记一条**（标题 + 做了什么 + 为什么），放在“逐个 PR”最后、“策略引擎”一节之前。接手前先读最近几条，了解对方改了什么。
- 不要修改主力盘交易程序 crypto-bot，也不要改动数据契约（`tools/account.schema.json`、`paper/*.json`、`config.js`），除非任务明确要求。

## 3. 目录与数据流

```
tools/evolve/              自我进化策略引擎（当前唯一信号来源）
  data.py                  数据层：币池 = 成交额前 100 的 USDT 永续（上市满一年，首次选定后固定在 paper-data/evolve/universe.json）；OKX 1H K 线缓存在 GitHub Actions cache（`candles/`，不进 paper-data 分支）；共享时间轴、70/30 切分、波动观察
  execution.py             同源执行引擎（合约模式：整张、逐仓杠杆 ≤10、可用保证金 95%、强平、资金费，规则与 okx_demo_trader 一致，改一边要同步另一边）；`entries()` 一次算出某基因在所有币上的全部入场信号（与逐根调用 `signal()` 结果完全相同，快约 100 倍，100 个币靠它才跑得动）
  genome.py                基因：物种（动量/均值回归/波动突破/均线趋势）与参数、交叉、突变
  execution.py             同源执行：回测与纸面交易共用的唯一撮合引擎 simulate()
  evolution.py             每代：观察 → 繁殖 → 训练集适应度（训练期分 4 段检验，近期权重大）→ 样本外闸门 → 选择；冠军选择、谱系修剪、旧系统策略一次性迁入
  convert_old.py, migrants.json   旧系统（trading-evo）策略转换与迁入名单
  paper.py                 冠军组合纸面交易（5 个席位、每席按段重放 simulate），导出 OKX 跟单信号（同币种取排名高者）
  run.py                   一次运行 = 更新数据 + 进化 5 代 + 换冠军 + 纸面交易
                           → paper-data/evolve/{state.json, dashboard.json（精简谱系）, nodes.json（每个节点的基因与曲线，点开时加载）, signals.json}
tools/okx_demo_trader.py   OKX 模拟盘跟单：跟随 evolve/signals.json（冠军持仓），交易所止损/止盈、每单风险 = 开仓策略的风险基因（2%–5%，`RISK_RANGE`）、同方向合计 ≤ 20%（`SAME_SIDE_CAP`，按冠军组合排名优先）、日亏 20% 停手（`DAILY_HALT`）；每个策略最多同时 3 个持仓（`execution.MAX_POSITIONS`，回测与纸面同源）
                           → paper-data/okx_demo/{state.json, actions.jsonl, summary.json}
tools/paper_trader.py, tools/volume_test.py   已退役的旧三策略引擎与限时档位（仅在没有 evolve/signals.json 时作为后备）
tools/stop_demo.py         全部平仓并暂停（stop-demo.yml）
.github/workflows/paper-trading.yml   定时运行上面两个程序（cron 每 4 小时，`17 */4 * * *`；为减少账号上的自动运行和提交，不要调高频率）
runtime/ + deploy/         可选常驻服务（FastAPI）；部署后替代 GitHub 定时任务
index.html, assets/*.mjs   看板（无构建步骤，原生 ES 模块）
  assets/evolve.mjs        策略进化页：进化循环、适应度曲线、基因池谱系图、节点详情、纸面交易
  assets/okx.mjs           OKX 模拟账户：摘要校验、实时浮盈、持仓/成交渲染、机器人状态
  assets/robot-work.mjs    机器人“在做什么”（盯盘/扫描/等信号）、成交票据、持仓播报
  assets/mascot.mjs        机器人伙伴（表情、动画、面板、桌面悬浮窗）
  assets/app.mjs           页面主逻辑：数据读取、行情流、渲染调度
widgets/                   iPhone 主屏幕小组件（Scriptable）与安装说明
manifest.webmanifest, assets/icons/   手机“添加到主屏幕”App 配置
```

看板读取 `https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/okx_demo/summary.json`（约 5 分钟缓存），持仓浮盈用 **OKX 模拟盘自己的标记价**（`wss://wspap.okx.com` 推送，失败时 REST 备用）。

## 4. 代码约定

- **前端**：原生 ES 模块，不引入构建工具或框架。外部脚本只用 `cdn.jsdelivr.net`；新增网络地址要同步改 `index.html` 里的 CSP `connect-src`。
- **界面文字用简体中文，时间显示 UTC+7**，金额单位 USDT。
- **渲染性能**：行情每秒推送多次。不要每次推送都重写整块 DOM——用 `setHTML` / `setText`（内容不变不写入），持仓行就地更新（`renderOkxPositions`），重绘频率已限制为每秒一次。数字动画用 `rollNumber`（同一元素最多每 3 秒一次）。
- **转义**：所有来自交易所或数据文件的文字都要经过 `esc()` 再写入 HTML。
- **Python 工具只用标准库**（`runtime/` 除外，见 `runtime/requirements.txt`）；对 OKX 的请求统一走 `OKX.request`。
- **纯逻辑放进可测试的函数**（如 `okx.mjs`、`robot-work.mjs`、`widgetModel`），DOM / 网络部分保持薄。

## 5. 测试（提交前运行，PR 描述里写结果）

```bash
npm test                                             # JS 单元测试
python3 -m unittest discover -s tests -p 'test_*.py' # Python 单元测试
# runtime 测试需要依赖：python3 -m venv .venv && .venv/bin/pip install -r runtime/requirements-test.txt
python3 -m http.server 8008 --bind 127.0.0.1 &      # 浏览器验收前启动本地服务（Playwright + Chromium）
PANEL_URL=http://127.0.0.1:8008/ node tests/okx_browser.cjs       # OKX 账户首页 / 模拟交易页 / 机器人
PANEL_URL=http://127.0.0.1:8008/ node tests/okx_rest_browser.cjs  # 模拟盘标记价 REST 备用 + 连接诊断
PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs    # 机器人交互
PANEL_URL=http://127.0.0.1:8008/ node tests/market_browser.cjs    # 市场行情前 100
PANEL_URL=http://127.0.0.1:8008/ node tests/float_browser.cjs     # 电脑桌面悬浮窗
PANEL_URL=http://127.0.0.1:8008/ node tests/app_browser.cjs       # 手机主屏幕 App（manifest / 图标）
PANEL_URL=http://127.0.0.1:8008/ node tests/evolve_browser.cjs    # 策略进化页（夹具 tests/fixtures/evolve-dashboard.json）
```

已知：`tests/team_browser.cjs`、`tests/browser_smoke.cjs` 在基线 `cc25dc5` 上就已失败（仍查找 v3 删除的角色卡），不是新改动造成的。浏览器测试全部失败、报 `ERR_CONNECTION_REFUSED` 时，是本地服务没启动。

## 6. 常见坑（都真实发生过）

- **进化系统的硬规则不要破坏**：回测与纸面交易必须都走 `execution.simulate()`（同源执行）；信号只用已收盘数据、下一根开盘成交；样本外 30% 不参与适应度；纸面交易由**冠军组合**（样本外合格者中训练适应度前 5、同物种最多 2 个、只做多 / 只做空各最多 3 个（`TEAM_DIRECTION_CAP`，超额时任期满后每次运行换下一个，理由 `balance`）、资金五等分、各自走 `simulate()`）执行，成员只能从样本外合格者中选；稳定规则（每席最短任期 24 小时、挑战者连续 3 次合格且高出最弱成员 2%、每次运行最多换一席、保留线：样本外亏损 > 2% 或回撤 > 30% 才提前换下）在 `evolution.choose_team`，不要去掉，否则样本外窗口每小时滚动会让冠军来回切换。改执行规则时同时更新 `tests/test_evolve.py` 并重新生成夹具。训练集适应度会随代数上升，样本外合格数下降是过拟合的正常信号，不是 bug。多样性保护（每个物种的最佳者必定保留、选父母用适应度共享、每物种至少 `MIN_PER_SPECIES` 个）不要去掉：没有它时种群曾收敛到 36/40 同一物种、样本外合格数降到 0。

- **OKX 模拟盘是独立市场，价格与真实盘不同**（例：XRP 模拟盘 1.2686 vs 真实盘 1.2811）。模拟仓位的浮盈必须用模拟盘标记价，不能用真实盘行情。
- **部分 4G 网络屏蔽 OKX**（请求一直挂起直到超时），Wi‑Fi / VPN 正常。看板必须在连不上 OKX 时退回账户快照，不能空白或报错。
- **定时任务每 4 小时一次**（用户要求，担心高频自动运行导致 GitHub 账号被封）：看板上的“快照 X 小时前”是正常的，不是 bug；不要改回高频。
- **已实现盈亏 = 价差 + 手续费 + 资金费**，模拟盘资金费可能很高；止盈成交也可能整体亏损。交易所止损 / 止盈的金额来自 `/api/v5/account/positions-history`（注意它和 `/account/positions` 前缀相同，测试桩里要先匹配 history）。
- **GitHub Pages 缓存约 10 分钟**，手机 Safari 会继续用旧的 JS 模块；验证线上效果时在网址后加 `?v=数字`。
- **Safari 设置**（隐藏 IP 地址、内容拦截器）可能单独拦截 OKX 连接；右上角连接状态可点开“连接诊断”逐项测试。
