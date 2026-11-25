# AGENTS.md — 给 Codex、Claude 等编码助手的工作说明

本仓库是 **MODI 交易看板**：GitHub Pages 静态网站（`index.html` + `assets/*.mjs`），**只展示 OKX 实盘账户**（真实资金，数据来自 `live-data` 分支，由本地机器运行 `tools/live_sync.py` 从实盘服务器只读同步）、市场行情和 MODI 机器人伙伴。模拟盘跟单与策略进化仍由 GitHub Actions 定时运行，结果写到 `paper-data` 分支（为实盘提供信号），但看板不再显示模拟盘。多个助手（Codex、Claude）会先后在这里工作，请先读完本文件再动手。

## 1. 安全红线（任何情况下都不能违反）

- **默认只做模拟交易；实盘只走已有开关。** 统一使用 `tools/okx_demo_trader.py` 里的 `OKX` 客户端，不要另写下单代码。模拟盘（`OKX_TRADING_MODE` 未设或 `demo`）的私有请求必须带 `x-simulated-trading: 1`。实盘（`OKX_TRADING_MODE=live`）只在 API Key 绑定 IP 的新加坡服务器上用 `deploy/live/` 的 systemd 任务运行，默认 dry-run（`OKX_DRY_RUN` 精确为 `0` 才下单），状态在 `okx_live/`。不要去掉这些保护：live 拒绝在 GitHub Actions / `runtime/` 中运行、dry-run 时客户端拒绝非 GET 请求、demo 与 live 状态互斥、过期信号不操作。不要把 GitHub 工作流改成 live，不要在开发机上调用实盘接口。
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
  data.py                  数据层：币池 = 成交额前 50 的 USDT 永续（`UNIVERSE_SIZE`；上市满一年，首次选定后固定在 paper-data/evolve/universe.json，旧的更长列表按原顺序截到前 50；实盘跟单同样只扫信号列表前 50，`okx_demo_trader.SCAN_LIMIT`，两边要一起改）；OKX 1H K 线缓存在 GitHub Actions cache（`candles/`，不进 paper-data 分支）；共享时间轴、约 60% 训练 / 20% 样本外 / 20% 终审切分（`TRAIN_SHARE` / `TEST_SHARE`）、波动观察
  paper.py                 纸面账户：冠军组合共用一个全仓账户（`execution.simulate_account`），每次运行从账户起点带着席位 / 第一名历史整体重算，导出 OKX 跟单信号（同币种取排名高者）；旧的 5 席位代码只用于一次性转换
  execution.py             同源执行引擎（合约模式：整张、全仓、杠杆基因 1–20× 再受单币上限、可用保证金 95%、账户层强平、资金费，规则与 okx_demo_trader 一致，改一边要同步另一边）；`entries()` 一次算出某基因在所有币上的全部入场信号（与逐根调用 `signal()` 结果完全相同，快约 100 倍，几十个币的币池靠它才跑得动）
  genome.py                基因：物种（动量/均值回归/波动突破/均线趋势）与参数、交叉、突变
  evolution.py             每代：观察 → 繁殖 → 训练集适应度（训练期分 4 段检验，近期权重大）→ 样本外闸门 → 选择；冠军选择、谱系修剪、旧系统策略一次性迁入
  convert_old.py, migrants.json   旧系统（trading-evo）策略转换与迁入名单
  run.py                   一次运行 = 更新数据 + 进化 5 代 + 换冠军 + 纸面交易
                           → paper-data/evolve/{state.json, dashboard.json（精简谱系）, nodes.json（每个节点的基因与曲线，点开时加载）, signals.json}
tools/evolve/signals_hourly.py   实盘服务器每小时 H:01:30 只算信号（不进化、不发布、无密钥）：读已发布 state.json 重放（`paper.update(replay=True)`），K 线不齐到 H:05 失败不写（截止会中止进行中的请求，`data.okx_get(deadline=)` + 看门狗，不靠 systemd 超时）；systemd `deploy/live/okx-signals{,-shadow}.*`；跟单信号时效 `OKX_SIGNAL_MODE`（不设 = GitHub 9 h / 5 h，`hourly` = 2 h 15 min / 2 h，drop-in `okx-live-hourly.conf`）；方案见 docs/shadow-run-plan.md
tools/okx_demo_trader.py   OKX 模拟盘跟单：跟随 evolve/signals.json（冠军持仓），交易所止损/止盈、每单风险 = 开仓策略的风险基因（5%–10%，`RISK_RANGE = (.05, .10)`，缺 `p['risk']` 时默认 5%；主流币 BTC/ETH/SOL/BNB/XRP/DOGE 风险 ×2（`MAJOR_FACTOR`），拥挤降风险和加倍之后仍 ≤ 10%（`MAX_TRADE_RISK = .10`，2026-11-20 先由 10% 上调到 20%、同日再下调回 10%；纸面 `execution.MAX_TRADE_RISK` 同为 10%，两边一起改））、杠杆 = 杠杆基因 1–20×（`EVOLVED_MAX_LEVERAGE`）再受该币 OKX 最高档限制（取不到上限时不开该币新仓）、全仓（`MARGIN_MODE = 'cross'`）、每笔都挂交易所止损、同方向最多 20 单（`execution.MAX_SAME_SIDE_POSITIONS` / `MAX_SAME_SIDE`，含第一名，按冠军组合排名优先）、同方向合计风险不设上限（进化模式 `SAME_SIDE_CAP = None`，纸面 `ACCOUNT_RULES['same_side_cap'] = None`；2026-11-15 用户要求去掉原 20% 上限）、当天亏损达 25% 全部平仓停手到次日（跟随进化冠军时 `EVOLVED_DAILY_HALT = .25` / `ACCOUNT_RULES['daily_halt'] = .25`，含第一名；原 10%。代码里 `RISK, MAX_LEVERAGE, DAILY_HALT = .01, 10, .10` 只属于已退役的旧档 legacy volume-test，不是实盘现状）；同方向已开不到 10 仓按满风险、≥10 仓下一单减半（`CROWD_STEPS = ((10, .5),)`，2026-11-20；纸面 `execution.CROWD_STEPS` 相同，两边一起改）；**第一名**（信号 `leader: true`；`state['leader']`，必须样本外和终审都合格）只是信号优先，不再加大仓位：和其他席位一样按自己的风险基因（夹到 5%–10%）与杠杆基因（1–20×）开仓，计入同向 20 单上限和日亏停手（`LEADER_RISK = MAX_TRADE_RISK = 10%` / `LEADER_LEVERAGE = 20×` 只是上限，不是加大仓位；`LEADER_RISK` 跟随本文件的 `MAX_TRADE_RISK`；纸面 `ACCOUNT_RULES['leader_risk']` 同样跟随 `execution.MAX_TRADE_RISK` 为 10%）；每个策略最多同时 3 个持仓（`execution.MAX_POSITIONS`，回测与纸面同源）；**出场（2026-11-21，所有策略统一，不按策略开关）**：每仓同时有止损 + 止盈（`target_rr` 基因 1–6R，旧 0 由 `fix()` 映射为 `DEFAULT_TARGET_RR = 3R`）、反向信号平仓（账户级 `ACCOUNT_RULES['reverse_close']` = `'own'` 默认 / `'team'`，用原始物种信号）、移动止损（浮盈 ≥ 0.5×R 后止损 = 最佳价 ∓ 1×R，`TRAIL_START_R = .5`（2026-11-21 由 1.0 改，样本外重放选定）/ `TRAIL_DIST_R = 1`，只朝有利方向；保本一档 `BREAKEVEN_R = None` 关闭；实盘 / 模拟盘跟单的 R = min(纸面 dist, |实盘入场 − 实盘初始止损|)（`execution.trail_unit`，旧记录缺 `init_stop` 时用纸面 dist）；共用 `execution.trail_stop_level`）；**风险系数（2026-11-21，execution TRAIL & RISK RULES）**：同一冠军在同一根信号 K 线的同向开仓合计只占一笔的风险（每笔 ÷ n，`SAME_BAR_SHARE`）；席位在座不足 24 小时（`NEW_SEAT_HOURS`）的第一根开仓 K 线、或正在离任（winding）的席位开仓，风险 ×0.5（`NEW_SEAT_FACTOR`）；系数乘在 5–10% / 拥挤 / 主流币 / 10% 上限之后，可以低于 5%；纸面算出后经 signals.json `risk_factor` 交给跟单（缺省 = 1），`simulate()` 适应度只用同 K 线合并；纸面只对 `account['trail_risk_from']`（上一轮已发布的最后一根，只设一次）之后生效、**没有持仓时间上限**（`max_hold` / `trail` 基因保留但不再使用）；跟单每轮确认交易所止损 + 止盈都在、按已收盘 1H K 线 amend 止损（每小时一次，比纸面逐根慢最多 1 小时），纸面平仓原因见 signals.json `exits`（含 `manual_user`：用户手动平仓，`paper.MANUAL_CLOSES` 按 币种 + 开仓时间 精确匹配，每次重放都在原价平掉；`account['manual_from']` 与其他分段时间点一样只设一次，已发布的 K 线不重算，在之后第一根 K 线开盘入账；只能追加、不要改旧条目）
                           → paper-data/okx_demo/{state.json, actions.jsonl, summary.json}
tools/paper_trader.py, tools/volume_test.py   已退役的旧三策略引擎与限时档位（仅在没有 evolve/signals.json 时作为后备）
tools/stop_demo.py         全部平仓并暂停（stop-demo.yml）
tools/fetch_signals.py     实盘服务器下载并校验 paper-data/evolve/signals.json（只读公开文件）
deploy/live/               OKX 实盘：systemd 单元 / 定时器 / 环境变量模板 / 部署与切换说明（OKX_TRADING_MODE=live，默认 dry-run）
.github/workflows/paper-trading.yml   定时运行上面两个程序（cron 每 4 小时，`17 */4 * * *`；为减少账号上的自动运行和提交，不要调高频率）
runtime/ + deploy/         可选常驻服务（FastAPI）；部署后替代 GitHub 定时任务
index.html, assets/*.mjs   看板（无构建步骤，原生 ES 模块）
  assets/evolve.mjs        策略进化页（#/evolve，读 paper-data/evolve）：进化循环、适应度曲线、基因池谱系图、节点详情、冠军组合（不显示模拟账户数字）
  assets/okx.mjs           OKX 模拟账户：摘要校验、实时浮盈、持仓/成交渲染、机器人状态
  assets/robot-work.mjs    机器人“在做什么”（盯盘/扫描/等信号）、成交票据、持仓播报
  assets/mascot.mjs        机器人伙伴（表情、动画、面板、桌面悬浮窗）
  assets/app.mjs           页面主逻辑：数据读取、行情流、渲染调度
widgets/                   iPhone 主屏幕小组件（Scriptable）与安装说明
manifest.webmanifest, assets/icons/   手机“添加到主屏幕”App 配置
```

看板优先读服务器只读接口 `https://139-180-146-42.sslip.io/live.json`（`{snapshot, fills, trades}`，与 live-data 同字段，每 60 秒刷新），读不到时静默退回 `https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/snapshot.json`、`fills.json` 与 `trades.json`（可缺，旧同步时显示空状态）（约 5 分钟缓存，`?t=` 每分钟换一次），持仓浮盈用 **OKX 公共标记价**（`wss://ws.okx.com` 推送，备用 `wss://wsaws.okx.com:8443`，连不上时用快照值）。公共 REST 依次试 `www.okx.com` → `aws.okx.com` → 服务器 `/tickers.json`，有一个成功就不显示红色错误。浏览器测试必须拦截 `sslip.io`（否则会读到真实账户）。`live-data` 分支只由 `tools/live_sync.py` 写入（白名单字段 + 密钥检查，不 force push），不要手动修改；GitHub 凭据只在本地机器，服务器上不放。

```
tools/live_sync.py         本地机器：ssh 只读服务器 okx_live/{summary,state}.json + actions.jsonl → 白名单 → live-data 分支 {snapshot,fills,trades}.json（trades = 已平仓明细 + 胜率 / 盈亏比 / 回撤 / 按种子统计）
tools/live_readapi.py      实盘服务器（modi-okx-readapi.service，modi-live 身份，代码在 /opt/modi-okx-readapi）：每 60 秒只读 OKX 账户（强制 dry-run 客户端）+ okx_live 文件 → 与 live_sync 同一白名单 + 密钥检查 → 内存缓存；127.0.0.1:8787 只回 GET /live.json /tickers.json /healthz /candles（K 线备用源，参数白名单）/stream（SSE 推送持仓公共标记价，约每秒，看板断开时退回 60 秒轮询）；Caddy 在 443 提供 https://139-180-146-42.sslip.io（只开 443 与 22）
tools/live_alert.py        本地机器：live_sync 之后只读检查实盘异常（停跑 / 熔断 / 暂停 / 日亏 >10% / 连续 3 止损 / 报错）→ stdout JSON + 退出码（0 正常 / 1 需通知 / 2 已报过），不发消息
assets/live.mjs            实盘看板纯逻辑：校验、实时浮盈、新鲜度 / 状态、持仓 / 成交渲染、机器人状态
```

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
PANEL_URL=http://127.0.0.1:8008/ node tests/live_browser.cjs      # OKX 实盘账户首页（live-data 夹具）/ 标记价 / 离线 / 机器人
PANEL_URL=http://127.0.0.1:8008/ node tests/evolve_browser.cjs    # 策略进化页（paper-data/evolve 夹具）/ 谱系 / 冠军组合 / 导航
PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs    # 机器人交互
PANEL_URL=http://127.0.0.1:8008/ node tests/market_browser.cjs    # 市场行情前 100
PANEL_URL=http://127.0.0.1:8008/ node tests/float_browser.cjs     # 电脑桌面悬浮窗
PANEL_URL=http://127.0.0.1:8008/ node tests/app_browser.cjs       # 手机主屏幕 App（manifest / 图标）
PANEL_URL=http://127.0.0.1:8008/ node tests/fallback_browser.cjs  # 手机 4G 屏蔽 OKX：服务器接口 / aws 域名 / 服务器行情兜底，无红条
```

已知：`tests/mascot_browser.cjs` 中“Esc 关闭面板后焦点回到机器人”一项偶发失败（焦点落在 body；基线 `9ffb027` 上用 Playwright 1.65 / Chromium 也出现过），重跑即可通过。浏览器测试全部失败、报 `ERR_CONNECTION_REFUSED` 时，是本地服务没启动。

## 6. 常见坑（都真实发生过）

- **进化系统的硬规则不要破坏**：回测与纸面交易必须都走 `execution.simulate()`（同源执行）；信号只用已收盘数据、下一根开盘成交；样本外 20% 和终审 20% 都不参与适应度（终审连繁殖筛选也不参与）；纸面交易由**冠军组合**（“合格” = 样本外 20% 与终审 20% 两段都过；合格者中训练适应度前 8（`TEAM_SIZE`）、同物种最多 2 个、只做多 / 只做空各最多 5 个（`TEAM_DIRECTION_CAP`，超额时任期满后每次运行换下一个，理由 `balance`）、至少 2 席留给 1H / 4H 周期的合格策略（`TEAM_SHORT_TF_SEATS = 2`、`SHORT_TFS = (1, 4)`，含已在席的短周期成员，替换最弱日线成员、不增加席位，理由 `short_tf`；0 = 旧选择）、共用一个全仓账户、走 `simulate_account()`（规则与 `simulate()` 相同，单策略时结果一致；单笔风险 5%–10%（风险基因 `genome.GENES['risk'] = (.05, .10)`，缺失按 5%；`execution.MIN_TRADE_RISK` / `MAX_TRADE_RISK = .05 / .10`，主流币加倍仍 ≤ 10%）/ 杠杆 1–20× / 同向最多 20 单 / 同向风险不设上限 / 日亏 25% 停手 / 拥挤降风险（同向已开 ≥10 仓下一单减半，`CROWD_STEPS = ((10, .5),)`）/ 维持保证金主流币 0.5%、其他 2.5%；这些与 OKX 跟单（`okx_demo_trader`，模拟盘与实盘共用）的 `RISK_RANGE` / `MAX_TRADE_RISK` / `CROWD_STEPS` / `LEADER_RISK` 一致（2026-11-20 重新同步），改一边要同步另一边。纸面重放的出场同样分段：`account['exit_rules_from']`（上一轮已发布的最后一根）及之前用旧出场（席位保存时的 target_rr / trail / max_hold、`MAX_HOLD_H`），之后全部仓位用新出场，不要移动这个时间点；纸面重放按开仓时间分段定尺寸、已成交仓位尺寸不变：`account['risk_rules_from']` 及之前用 `execution.LEGACY_SIZING`（2%–10% / 10% / ≥3 减半 ≥6 四分之一），之后到 `account['risk_cap_from']` 用 `execution.PRIOR_SIZING`（5%–20% / 20% / 满 10 仓减半），再之后用当前规则；以后再改尺寸规则时照此加一段，不要删掉或前移这些时间点、不要改旧段的值（与 `side_cap_from` 同理）））执行，成员只能从样本外合格者中选；稳定规则（每席最短任期 7 天、被换下的成员不强平而是持仓走完（`paper.py` winding）、挑战者连续 3 次合格且高出最弱成员 2%、每次运行最多换一席、保留线：样本外或终审亏损 > 5% 或回撤 > 30% 才提前换下）在 `evolution.choose_team`，不要去掉，否则样本外窗口每小时滚动会让冠军来回切换。改执行规则时同时更新 `tests/test_evolve.py` 并重新生成夹具。**GitHub 纸面运行的所有改动（换席位 / 第一名 / price_off / 人工平仓 / 新 cutover 键）都从下一个整点 C = floor(now, 1h) + 1h 生效**（`paper.py` NEXT-HOUR RULE，`effective_bar()`；`next_hour_from`），**price_off 只挡纸面与模拟盘，不挡实盘**（2026-11-25，PRICE_OFF LIVE）：纸面账户照旧不在 price_off 币上开仓（逐字节不变），只因 price_off 被拒的合格信号作为虚拟仓（`simulate_account(off_book_from=)`，不碰账户；同向 20 单上限按 纸面 + 虚拟 一起数，纸面自己的检查仍只数纸面仓）从 `account['price_off_live_from']` 起发布到 `signals.json` 的 `price_off_live` / `price_off_live_exits`；只有 live 跟单（`Mirror.follow_price_off`）跟，止损止盈由 `Mirror.real_levels()` 用真实 K 线 ATR 在实盘现价重算，同一冠军同一根 K 线同向的普通 + price_off 入场在实盘合计只占一笔风险（`execution.live_same_bar_groups`；price_off 信号的 `risk_factor` 和同 K 线普通信号的 `live_risk_factor` 按合并组算，例如 1 + 1 = ½ + ½；纸面 `risk_factor` / 尺寸不变，live 跟单有 `live_risk_factor` 时用它，demo 只开普通单、仍用 `risk_factor`），demo 不读、仍用 `demo_price_off`；没有这个字段的旧 signals.json 不开也不平。以后新增 cutover 键要加进 `paper.CUTOVER_KEYS` 并用 `cutover()` 设置，不要再盖在本次运行的最后一根上，否则每小时服务器信号会与纸面分叉。训练集适应度会随代数上升，样本外合格数下降是过拟合的正常信号，不是 bug。多样性保护（每个物种的最佳者必定保留、选父母用适应度共享、每物种至少 `MIN_PER_SPECIES` 个）不要去掉：没有它时种群曾收敛到 36/40 同一物种、样本外合格数降到 0。

- **OKX 模拟盘是独立市场，价格与真实盘不同**（例：XRP 模拟盘 1.2686 vs 真实盘 1.2811）。模拟仓位的浮盈必须用模拟盘标记价，不能用真实盘行情。
- **部分 4G 网络屏蔽 OKX**（请求一直挂起直到超时），Wi‑Fi / VPN 正常。看板必须在连不上 OKX 时退回账户快照，不能空白或报错。
- **定时任务每 4 小时一次**（用户要求，担心高频自动运行导致 GitHub 账号被封）：看板上的“快照 X 小时前”是正常的，不是 bug；不要改回高频。
- **已实现盈亏 = 价差 + 手续费 + 资金费**，模拟盘资金费可能很高；止盈成交也可能整体亏损。交易所止损 / 止盈的金额来自 `/api/v5/account/positions-history`（注意它和 `/account/positions` 前缀相同，测试桩里要先匹配 history）。
- **GitHub Pages 缓存约 10 分钟**，手机 Safari 会继续用旧的 JS 模块；验证线上效果时在网址后加 `?v=数字`。
- **Safari 设置**（隐藏 IP 地址、内容拦截器）可能单独拦截 OKX 连接；右上角连接状态可点开“连接诊断”逐项测试。
