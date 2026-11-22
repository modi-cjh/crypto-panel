# OKX 实盘跟单（新加坡服务器）

实盘就是原来的 OKX 模拟盘跟单程序 `tools/okx_demo_trader.py`，只是切换了模式：

| 环境变量 | 取值 | 作用 |
| --- | --- | --- |
| `OKX_TRADING_MODE` | `demo`（默认）/ `live` | `demo` 每个请求带 `x-simulated-trading: 1`，状态在 `<data>/okx_demo/`；`live` 去掉该请求头、走真实账户，状态在 `<data>/okx_live/`（两边状态互不读取，混用会拒绝运行）。 |
| `OKX_DRY_RUN` | 实盘默认开 | `live` 时**只有精确等于 `0` 才会真实下单**；不写、写 `1`、`false`、`off` 等都按 dry-run。dry-run 只读账户、把“本来会下的单”写进 `okx_live/dry_run_preview.json`，不发任何订单——OKX 客户端本身会拒绝所有非 GET 请求。 |
| `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` | 实盘密钥 | 只从环境变量读取，经 stdin 交给 curl，不打印、不写日志、不写文件。 |

实盘与 OKX 模拟盘跟单共用 `tools/okx_demo_trader.py` 的同一套常量，不另设参数：全仓、强制交易所止损、止盈由基因决定、杠杆 1–20× 再受币种上限、单笔风险 5–10%（`RISK_RANGE = (.05, .10)`，缺风险时默认 5%；主流币加倍仍 ≤ 10%，`MAX_TRADE_RISK = .10`）、同方向最多 20 仓、同方向合计风险不设上限、同向已开不到 10 仓按满风险 / ≥10 仓下一单减半（`CROWD_STEPS = ((10, .5),)`）、日亏 25% 停手、扫描信号列表前 50 个币、只跟冠军组合的信号。`LEADER_RISK` 跟随 `MAX_TRADE_RISK` 为 10%（只是上限）。纸面与进化（`tools/evolve/`）使用同样的 5–10% / 单笔 10% / 满 10 仓减半（2026-11-20 由 5–20% / 20% 下调），信号来源与跟单规则一致。移动止损 / 风险系数（2026-11-21）：浮盈 ≥ 0.5R 起移动止损，R = min(纸面 dist, 实盘 |入场 − 初始止损|)（state 里 `init_stop` / `live_dist`，旧记录用纸面 dist）；同冠军同 K 线同向合并风险与新任 / 离任席位半风险由纸面算出，经 signals.json `risk_factor` 乘到实盘风险上（缺省 1，可低于 5%）。

额外的实盘保护：

- 在 GitHub Actions（`GITHUB_ACTIONS=true`）或 `runtime/` 常驻服务里以 `live` 启动会直接拒绝。GitHub 上的定时任务继续跑模拟盘，不受影响。
- 实盘只跟随 `evolve/signals.json`，文件缺失或 `updated_at` 超过 9 小时（每小时信号模式 `OKX_SIGNAL_MODE=hourly`：2 小时 15 分）就不操作（交易所止损仍在）；不会退回已退役的旧策略引擎。
- 新的实盘状态默认暂停：即使关掉 dry-run，也要再明确执行一次 `resume` 才会开新仓。
- 冒烟测试 `okx_demo_smoke.py`（会真实开平一笔 BTC）只允许在模拟盘运行。
- 密钥开了提现权限会拒绝运行（原有检查）。

## 运行方式

- 进化与信号仍由 GitHub Actions 每 4 小时生成，发布到公开的 `paper-data` 分支。
- 服务器上的 `okx-live.timer` 每小时 :50（UTC）运行一次 `okx-live@run.service`：先用 `tools/fetch_signals.py` 下载并校验 `evolve/signals.json`，再运行跟单程序。没有新信号的那几次只核对止损、平仓和日亏停手。
- 实盘账户摘要只保存在服务器 `/var/lib/modi-okx-live/okx_live/`，**不会**发布到 `paper-data` 或公开看板。

## 每小时信号（可选，默认不启用）

`okx-signals.service` / `.timer`（正式）与 `okx-signals-shadow.service` / `.timer`（影子）在 H:01:30 运行 `tools/evolve/signals_hourly.py`：只用公共数据重放 GitHub 已发布的纸面账户，不读 `okx-live.env`（`/etc/modi-okx-live` 对它不可见）、不持有密钥，以独立的低权限用户 `modi-signals` 运行，状态在 `/var/lib/modi-okx-signals/`。影子只写 `signals.shadow.json`、不触发跟单；正式单元写 `signals.json` 并以 `OnSuccess=` / `OnFailure=` 触发 `okx-live@run`。只有安装 drop-in `okx-live-hourly.conf`（`OKX_SIGNAL_MODE=hourly`）后，跟单才改用本地每小时信号（2 h 15 min / 2 h）；不装就是现状（GitHub 信号，9 h / 5 h），`okx-live@.service` 本身没有改。hourly 模式下信号过期时 `okx-live@run` 不运行，但 `okx-live@pause`（只设置暂停，不按过期信号管理仓位）和 `okx-live@snapshot` 照常，`okx-live-stop` 始终可用。安装、影子 2–3 天、对账、验收、切换与回退：[`docs/shadow-run-plan.md`](../../docs/shadow-run-plan.md)。

## 部署步骤（Ubuntu 24.04，139.180.146.42）

> 只能在这台服务器上操作：实盘 API Key 绑定它的 IP。以下命令都在服务器上执行。

1. **创建 OKX 实盘 API Key**：权限只勾“读取”和“交易”，**不要勾“提现”**；IP 白名单只填 `139.180.146.42`。账户切到“合约模式”（单币种保证金或以上）。

2. **安装代码与用户**（Python 3.12 与 curl 为系统自带；工具只用标准库，不需要 venv）：

   ```bash
   sudo apt-get install -y git curl python3
   sudo useradd --system --home /var/lib/modi-okx-live --shell /usr/sbin/nologin modi-live
   sudo git clone https://github.com/modi-cjh/crypto-panel.git /opt/modi-okx-live
   sudo chown -R root:root /opt/modi-okx-live && sudo chmod -R a+rX,go-w /opt/modi-okx-live
   ```

   之后更新代码：`sudo git -C /opt/modi-okx-live pull --ff-only`（只拉 `master`，即审过、合并后的版本）。

3. **写环境文件**（密钥只在这里，root 所有、0600）。用 `sudoedit` 填写，不要用 `echo 密钥 >>`（会进 shell 历史）：

   ```bash
   sudo install -d -m 700 /etc/modi-okx-live
   sudo install -m 600 -o root -g root /opt/modi-okx-live/deploy/live/okx-live.env.example /etc/modi-okx-live/okx-live.env
   sudoedit /etc/modi-okx-live/okx-live.env      # 填三项密钥；保持 OKX_TRADING_MODE=live、OKX_DRY_RUN=1
   ```

   systemd 以 root 读取该文件后把变量交给服务进程；服务用户 `modi-live` 读不到文件本身。不要把这个文件复制进仓库目录，也不要发到聊天、PR 或截图。

4. **安装 systemd 单元**：

   ```bash
   sudo install -m 644 /opt/modi-okx-live/deploy/live/okx-live@.service /opt/modi-okx-live/deploy/live/okx-live.timer \
     /opt/modi-okx-live/deploy/live/okx-live-stop.service /opt/modi-okx-live/deploy/live/okx-live-check.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

5. **只读检查密钥**（不下单）：

   ```bash
   sudo systemctl start okx-live-check && journalctl -u okx-live-check -n 20 --no-pager
   ```

   应看到“✅ 密钥有效，已连上 OKX 实盘”、权限不含 withdraw、合约模式和 USDT 余额。

6. **dry-run 观察**（默认状态，不下单）：

   ```bash
   sudo systemctl start okx-live@run && journalctl -u okx-live@run -n 50 --no-pager
   sudo cat /var/lib/modi-okx-live/okx_live/dry_run_preview.json
   sudo systemctl enable --now okx-live.timer      # 每小时继续 dry-run，观察一段时间
   ```

   日志第一行是“OKX 实盘 · dry-run（只读账户、记录意图，不发送任何订单）”。核对 preview 里的 `open` / `skip`：币种、方向、张数、杠杆、止损与模拟盘同一信号的结果一致，再进入下一步。

## 从 dry-run 切换到真实下单

1. `sudoedit /etc/modi-okx-live/okx-live.env`，把 `OKX_DRY_RUN=1` 改为 `OKX_DRY_RUN=0`。
2. 下一次运行（或 `sudo systemctl start okx-live@run`）日志变为“OKX 实盘 · 真实下单”，并创建 `okx_live/state.json`，**仍处于暂停**，只管理、不开新仓。
3. 确认后恢复：`sudo systemctl start okx-live@resume`。此后定时任务按信号真实下单。

## 暂停、全部平仓、退回 dry-run

```bash
sudo systemctl start okx-live@pause          # 停止开新仓，已有仓位继续由程序管理止损 / 平仓（需 OKX_DRY_RUN=0 才会保存）
sudo systemctl start okx-live-stop           # 平掉程序自己开的全部仓位并暂停（人工仓位不动）；需 OKX_DRY_RUN=0
sudo systemctl disable --now okx-live.timer  # 停止定时运行
```

- 退回 dry-run：先 `okx-live-stop` 平仓（或确认程序没有持仓），再把 `OKX_DRY_RUN` 改回 `1`。dry-run 下程序不会管理已有仓位的平仓（只读），交易所止损仍在。
- 服务器断网 / 宕机时程序无法操作，只能依赖已挂在交易所的止损，并通过 OKX App / 网页人工处理。
- 在程序持有的币上**不要手动开同方向仓**：程序按币种和方向识别自己的仓位，平仓会连同手动部分一起平。其它币上的人工仓位不受影响。

## 日志与排查

```bash
systemctl list-timers okx-live.timer
journalctl -u 'okx-live@*' --since today --no-pager
sudo tail -n 20 /var/lib/modi-okx-live/okx_live/actions.jsonl
```

日志只有动作、币种、价格、数量和交易所错误码，没有密钥。常见错误：`50101`/`50113` 类（密钥或环境不匹配：模拟盘密钥不能用于实盘）、`50110`（IP 不在白名单）、`signals.json 已过期`（GitHub 定时任务停了或延迟，检查 Actions）。

## 只读 HTTPS 接口（看板每分钟刷新）

`tools/live_readapi.py` 以 `modi-live` 身份常驻（`modi-okx-readapi.service`），每 60 秒用同一个只交易 key **只读**账户（客户端强制 dry-run，任何非 GET 请求都会被拒绝），合并 `okx_live/` 里的文件（只读），用 `live_sync.py` 的同一白名单与密钥检查生成 `/live.json`，缓存在内存。HTTP 只监听 `127.0.0.1:8787`，由 Caddy 在 443 提供 `https://139-180-146-42.sslip.io`。它不写任何文件，与交易服务互不影响。

```bash
# 代码放在独立目录，升级接口不会改动交易程序的 /opt/modi-okx-live
sudo git clone https://github.com/modi-cjh/crypto-panel.git /opt/modi-okx-readapi
sudo chown -R root:root /opt/modi-okx-readapi && sudo chmod -R a+rX,go-w /opt/modi-okx-readapi
sudo install -m 644 /opt/modi-okx-readapi/deploy/live/modi-okx-readapi.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now modi-okx-readapi
curl -s http://127.0.0.1:8787/healthz

# HTTPS（Ubuntu 自带 caddy 2.6；证书用 443 的 TLS-ALPN 验证，不需要开 80）
sudo apt-get install -y caddy
sudo install -m 644 /opt/modi-okx-readapi/deploy/live/Caddyfile.readapi /etc/caddy/Caddyfile
sudo systemctl restart caddy

# 防火墙：只新增 443；22 必须保持开放（先确认 `ufw status` 里有 22/tcp，再另开一个 ssh 验证能登录）
sudo ufw allow 443/tcp
```

另有两条公共行情路由（不碰私有 key）：`/candles`（K 线备用源，参数白名单，同键 4 秒缓存）与 `/stream`（SSE 实时推送持仓标记价，约每秒一条；有人连着才每秒读一次 OKX 公共标记价；最多 50 个连接）。Caddy 对 `/stream` 用 `flush_interval -1` 不缓冲。

升级：`sudo git -C /opt/modi-okx-readapi pull --ff-only && sudo systemctl restart modi-okx-readapi`（Caddyfile 有变化时再 `sudo install -m 644 /opt/modi-okx-readapi/deploy/live/Caddyfile.readapi /etc/caddy/Caddyfile && sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`）。
停用：`sudo systemctl disable --now modi-okx-readapi caddy && sudo ufw delete allow 443/tcp`（看板自动退回 GitHub live-data）。
排查：`journalctl -u modi-okx-readapi -n 20 --no-pager`（只有“状态：正常 / OKX 读取失败”这类短句，没有数据和密钥）。
