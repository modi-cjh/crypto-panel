# 常驻模拟执行器与停止接口

本包只支持 OKX 模拟账户，所有交易请求仍强制 `x-simulated-trading: 1`。默认 `PANEL_EXECUTION_ENABLED=0`，启动网站不代表启动交易。当前 GitHub Pages 与原交易任务在迁移前继续使用原流程。

## 服务行为

- 一个常驻执行线程按“扫描 → 执行 → 读取交易所快照”顺序运行。无并发扫描和下单，数据目录排他锁阻止第二个实例。心跳每 2 秒写入 SQLite，与耗时扫描独立；看板将执行器心跳和账户更新时间分开展示。
- 默认每 300 秒启动一轮，基于单调时钟；若本轮超时跨过时点，跳到下一个时点，不叠加并发任务。失败的行情扫描按 30/60/120/240/300 秒退避，连续三次失败暂停；私有执行失败立即暂停，避免盲目重放下单。策略仍只处理新收盘 K 线，漏跑时沿用原运行器补处理机制。
- 停止请求经管理员会话、同源检查、CSRF 和幂等键验证后持久化，先关闭开仓闸门。扫描在下一次行情请求边界退出；已经提交给交易所的在途请求可能完成，停止流程随后核对并关闭程序仓位。当前交易请求不被强杀；平仓结果按可查询回执确认，不承诺零延迟或每个请求都能立即成交。
- 停止优先于后续扫描；平仓失败或结果不明时保持暂停和仓位管理。控制请求、周期状态、暂停位、会话和心跳存入 SQLite，重启会恢复未完成的停止请求；重启前中断的交易周期保持暂停，需核对后人工恢复。恢复接口不会清除日亏停手。
- 密码 PBKDF2 哈希、登录限速、HttpOnly/Secure/SameSite=Strict 会话、Origin/CSRF 校验；密钥和私有日志放在网站目录之外。服务只公开 `assets`、归档 `paper` 和脱敏账户快照，不提供源码目录浏览。
- `/control` 是同源管理控制台，停止/恢复通过后端直接执行，无 GitHub 排队。`/api/commands/<id>` 提供鉴权任务回执；浏览器不存 API key 或 GitHub token。

## Linux 部署

需要 Python 3.12+、curl、systemd、HTTPS 域名和反向代理；建议至少 2 核 / 2 GB 内存。应用监听 `127.0.0.1:8011`，通过 Caddy/Nginx 提供 HTTPS。运行单实例，数据放持久化磁盘；不要开启多个 Uvicorn worker。

1. 将部署包解压到 `/opt/crypto-panel`，创建专用系统用户与目录：

   ```bash
   sudo useradd --system --home /var/lib/crypto-panel --shell /usr/sbin/nologin crypto-panel
   sudo install -d -o crypto-panel -g crypto-panel -m 700 /var/lib/crypto-panel
   cd /opt/crypto-panel
   python3 -m venv .venv
   .venv/bin/pip install -r runtime/requirements.txt
   sudo chown -R root:crypto-panel /opt/crypto-panel
   sudo chmod -R u=rwX,g=rX,o= /opt/crypto-panel
   ```

2. 在服务器交互式生成配置。密码和密钥用隐藏输入，不要放到命令参数、网页或聊天：

   ```bash
   sudo /opt/crypto-panel/.venv/bin/python -m runtime.admin configure \
     --origin https://trade.example.com --output /etc/crypto-panel/runtime.env
   ```

   配置默认执行关闭。OKX 模拟密钥仅开读取/交易，服务器固定 IP 白名单；可配置只读 Actions 权限的 `PANEL_GITHUB_READ_TOKEN`，用于更及时地核验执行权并提高 GitHub API 额度。无 token 时检查节流至每四分钟一次。

3. 先在 GitHub Actions 停用以下四个工作流，并等待其运行和排队任务全部结束：
   `paper-trading.yml`、`start-aggressive-demo.yml`、`okx-demo-smoke.yml`、`stop-demo.yml`。GitHub Pages 部署任务保留启用。

4. 从停止后的 `paper-data` 分支取得最终账户数据，导入空数据目录。导入默认暂停，拒绝覆盖已有数据：

   ```bash
   git clone --depth 1 --branch paper-data https://github.com/modi-cjh/crypto-panel.git /tmp/crypto-panel-paper-data
   sudo -u crypto-panel /opt/crypto-panel/.venv/bin/python -m runtime.admin import-data \
     --source /tmp/crypto-panel-paper-data --data /var/lib/crypto-panel/data
   ```

5. 安装 systemd 服务和 HTTPS 代理，先保持执行关闭验证网站、登录与心跳：

   ```bash
   sudo install -m 644 deploy/crypto-panel.service /etc/systemd/system/crypto-panel.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now crypto-panel
   ```

   将 `deploy/Caddyfile.example` 的域名改为自己的域名，按 Caddy 的安装方式启用。应用源目录须可供专用用户读取，且只允许受控部署用户修改；私有目录和数据目录仅供服务用户写入。

6. 在服务器配置中将 `PANEL_EXECUTION_ENABLED=1` 后重启服务。服务仍保持暂停；在 HTTPS `/control` 登录，核对导入账户后明确点击“恢复按策略交易”。服务在下单前核验旧工作流确已停用且无在途任务，无法核验时拒绝接管。

## 看板地址与回退

常驻服务器同时提供整套看板与控制台，建议以其 HTTPS 地址作为新看板地址。服务自动返回同源 `runtime-config.json`，账户读取本地实时发布快照。

仓库中的 `runtime-config.json` 默认仍为 `github_actions`，所以本次发布不会把现有 GitHub Pages 指向不存在的服务器。若保留 GitHub Pages 作为主站，须在真实域名确定后显式配置控制台/公开账户/心跳地址，给该域名增加 CSP `connect-src`，并按需求限制公开接口 CORS；本包不开放跨站的带凭据控制请求。更简单的迁移方式是直接使用常驻服务提供的同源看板。

回退前先通过控制台停止并确认程序仓位已平、停用服务器交易执行，再将最终 `combined` / `okx_demo` 状态同步回 `paper-data`，最后才恢复 GitHub 工作流；禁止同时启用两套执行器。先备份 SQLite、账户 JSON 和成交日志，不从旧副本恢复活动订单状态。

## 验证与边界

```bash
.venv/bin/pip install -r runtime/requirements-test.txt
.venv/bin/python -m unittest discover -s tests
npm test
```

代码测试使用本地假交易所，不自动登录真实账户或下交易单。尚未在长期运行服务器进行故障演练前，不应作为实盘执行器使用；本包不提供实盘开关。服务器断网/断电时本地 API 无法执行停止，仍需依赖已有交易所止损并保留交易所端人工操作渠道。日志在私有 `logs` 目录；生产环境为其设置轮转、磁盘与进程告警。
