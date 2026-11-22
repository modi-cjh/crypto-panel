# 每小时信号：影子运行与切换方案（第一步）

> 时间都是 UTC。实盘服务器 = okx-live-sg（139.180.146.42）。本方案只在用户确认后由用户在服务器上执行；代码合并本身不改变线上行为。
> 前提：PR1（下一个整点生效，`paper.py` NEXT-HOUR RULE）已合并，**GitHub 至少已按新代码运行过一次**（已发布的 state.json 里有 `paper.account.next_hour_from`）。在此之前 `signals_hourly.py` 会以退出码 3 拒绝（`StateDrift`），影子期只能加 `--legacy-state` 观察，不能切换。

## 0. 组成

| 部分 | 作用 |
|---|---|
| `tools/evolve/signals_hourly.py` | 只算信号：读已发布的 paper-data `state.json` / `universe.json` / `dashboard.json`（按同一提交号读，generation / 最后 K 线不回退），增量拉 OKX 公共 1H K 线，检查每个币的最后一根已收盘（confirm=1）K 线 = floor(now) − 1h，不齐重试到 H:05（不齐就退出码 2，不写文件），资金费结算整点等记录到 H:08，`paper.update(..., replay=True)` 重放，原子写信号。不进化、不发布、无密钥。 |
| `deploy/live/okx-signals-shadow.service` + `.timer` | **影子**：H:01:30 运行 `--shadow`，写 `/var/lib/modi-okx-signals/evolve/signals.shadow.json` 与 `shadow/signals-YYYY-MM-DD.jsonl`，不触发跟单。 |
| `deploy/live/okx-signals.service` + `.timer` | **正式**：H:01:30 运行，写 `/var/lib/modi-okx-signals/evolve/signals.json`，`OnSuccess=` / `OnFailure=okx-live@run.service`。 |
| `deploy/live/okx-live-hourly.conf` | `okx-live@.service` 的 drop-in：`OKX_SIGNAL_MODE=hourly`。ExecStartPre（`fetch_signals.py`，多传 `--action %i`）改为检查并复制本地每小时信号；过期时只挡需要信号的动作，`pause` / `snapshot` 照常；交易程序的 `LIVE_SIGNAL_MAX_AGE` 2 h 15 min、`OPEN_WINDOW` 2 h。不装 = 现状（GitHub 信号，9 h / 5 h）。 |
| `tools/analysis/compare_signals.py` | 对账：按最后 K 线对齐影子信号与 GitHub 发布的 signals.json。 |
| `okx-live.timer`（:50） | 不动，影子期照常；切换后作为心跳保留。 |

### 截止时间（谁来中止卡住的请求）

| 时刻 | 谁 | 结果 |
|---|---|---|
| H:05 | 脚本：K 线截止。进行中的 curl 直接杀掉，`okx_get` 的静默重试、限速等待、重试循环一起结束 | 退出码 2，不写信号文件 |
| H:08 | 脚本：资金费截止（同样中止进行中的请求） | 用缓存的费率继续并告警；某个币一条费率都没有 → 退出码 2，不写 |
| H:09:30 | 脚本内看门狗（重放 / 磁盘等请求之外的卡死） | 退出码 2，不写；如果已开始写则不再触发 |
| H:11:30 | systemd `TimeoutStartSec=600` | 最后兜底，正常不会触发 |

GitHub 请求合计最多 60 秒，超过就用本地上一份 state（没有本地副本时退出码 3）。过了 H:05 才启动（手动运行）：取数最多 210 秒（`--late-budget`），并且不越过本小时末尾。“不写”指信号文件（`signals.json` / `signals.shadow.json`）：失败仍会记一行归档和 `last_run.json`（`ok: false`），已拉完的币会留在 K 线缓存里（缓存文件原子替换，不会留半个文件）。截止只约束取数；K 线在 H:05 前到齐后，资金费和重放之后写文件可以晚于 H:05。

## 1. 安装影子（不影响交易）

在服务器上（避开 :47–:53，实盘 :50 运行）：

```bash
sudo git -C /opt/modi-okx-live pull --ff-only          # 已合并 PR1 + PR2 的 master；交易程序本身行为不变
sudo useradd --system --home /var/lib/modi-okx-signals --shell /usr/sbin/nologin modi-signals
sudo install -m 644 /opt/modi-okx-live/deploy/live/okx-signals-shadow.service /opt/modi-okx-live/deploy/live/okx-signals-shadow.timer /etc/systemd/system/
sudo systemctl daemon-reload
# 先做首次冷缓存回填（见下一段），再启动定时器；之后手动验证一次：
sudo systemctl start okx-signals-shadow
journalctl -u okx-signals-shadow -n 30 --no-pager
sudo systemctl enable --now okx-signals-shadow.timer
systemctl list-timers 'okx-*'
```

首次冷缓存回填（50 币一年约 25 MB，本盒实测 17 分 18 秒）不能用影子单元：它过了 H:05 只有 210 秒（`--late-budget`），单元也有 `TimeoutStartSec=600`。用一次性运行放宽截止（同样的用户与限制，不触发跟单；在整点后不久开始，截止不会越过本小时末尾）。没拉完就以退出码 2 按时失败，已拉完的币保留在缓存里，再跑一次即可：

```bash
sudo systemd-run --wait --collect -p User=modi-signals -p StateDirectory=modi-okx-signals -p MemoryMax=520M -p Nice=15 \
  -p WorkingDirectory=/opt/modi-okx-live /usr/bin/python3 tools/evolve/signals_hourly.py --data /var/lib/modi-okx-signals --shadow --deadline 59 --funding-deadline 59
```

PR1 合并后、GitHub 还没按新代码跑过时，影子可临时加 `--legacy-state`（只允许和 `--shadow` 一起用，信号带 `legacy_state: true`，交易程序拒收）：`sudo systemctl edit okx-signals-shadow` 覆盖 `ExecStart=`。GitHub 按新代码跑过一次后去掉。

## 2. 影子运行 2–3 天

- 只记录不下单：交易程序继续用 `fetch_signals.py` 下载的 GitHub 信号，影子文件它从不读取（文件名不同、`shadow: true`，`fetch_signals.py` hourly 模式也拒收影子文件）。
- 每小时看一次健康：

```bash
cat /var/lib/modi-okx-signals/last_run.json                      # ok / code / timing / warnings
journalctl -u okx-signals-shadow --since -2h --no-pager | tail -20
systemctl show okx-signals-shadow -p MemoryPeak,CPUUsageNSec,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp
```

  oneshot 退出后 systemd 255 不再保留 `MemoryPeak`（`CPUUsageNSec` 会保留），所以单元的 `ExecStopPost=` 在 cgroup 删除前把
  `memory.peak`、`cpu.stat usage_usec`、结果和退出码追加到 `/var/lib/modi-okx-signals/metrics.log`（每小时一行）；进程自己的峰值 RSS
  在信号文件 / 归档的 `timing.max_rss_mb`。影子结束时统计：

```bash
tail -n 72 /var/lib/modi-okx-signals/metrics.log
awk '{for(i=1;i<=NF;i++) if($i ~ /^memory_peak=/){split($i,a,"="); print a[2]/1048576}}' /var/lib/modi-okx-signals/metrics.log | sort -n | tail -1   # MB 最大
```

- **每 4 小时对账**（GitHub 每次发布后，约 :20–:59 之后）。在本盒或服务器上：

```bash
# 服务器（只读公开接口，不需要凭据；GitHub API 匿名每小时 60 次，够用）
python3 /opt/modi-okx-live/tools/analysis/compare_signals.py --shadow /var/lib/modi-okx-signals/shadow --github-api 20 \
  --json /tmp/compare.json --out /tmp/compare.md; echo rc=$?
# 本盒（先 scp 影子归档；或在 paper-data 克隆里用 git）
python3 tools/analysis/compare_signals.py --shadow ./shadow --github-git ~/crypto-panel --ref origin/paper-data -n 30
```

  对齐方式：GitHub 的 signals.json 的最后 K 线 = 同一提交 dashboard.json 的纸面曲线最后一点；影子归档里 `last_bar_ts` 相同的那一小时就是对齐行。比较每个币的仓位（side / entry / stop / tp / opened / dist / trailing / champion / risk_factor / seat_from / winding / risk / leverage …，**rank 单列**，按 PR1 契约它跟随最新 team / 适应度、可以不同）、相邻对齐行之间的开 / 平仓信号、exits、权益（容差 0.01 USDT）；team / champion 只做参考。报告还给出成功率、失败退出码、缺失小时、收盘后延迟与单次用时。退出码 0 = 全部一致，1 = 有差异，2 = 没有对齐行。

## 3. 验收标准（全部满足才切换）

| 项 | 标准 |
|---|---|
| 一致率 | 影子期所有对齐行（≥ 12 行，约 2 天）`compare_signals` 全部“一致”（rank 除外）；任何不一致都要解释（代码版本不同、GitHub 运行在 `now` 之后超过 55 分钟才提交等）并修复后重新计数 |
| 成功率 | ≥ 95% 的小时退出码 0；退出码 2（K 线不齐）每天 ≤ 1 次；没有退出码 1 / 3 |
| 延迟 | `timing.latency_s`（收盘到写完）p50 ≤ 180 s，p90 ≤ 300 s（本盒两次真实 H:01:30 运行为 105 s 和 302 s，第二次超过 300 s；旧代码，原因推断为 OKX 请求静默重试，没有请求日志） |
| 截止 | 没有一次运行的取数越过 H:05；`systemctl show` / metrics.log 里没有 `result=timeout`（有就说明脚本自己的截止没起作用） |
| systemd 单元 | 仓库里只做过静态检查（本盒跑不了 `systemd-analyze verify`）：安装后先在服务器上 `systemd-analyze verify /etc/systemd/system/okx-signals*.service`，并确认 metrics.log 有内容 |
| 内存 | `MemoryPeak` 最大 ≤ 460 MB（= MemoryHigh），没有 OOM / 被杀；同时 `free -m` 的 available 最低 ≥ 100 MB |
| CPU | `CPUUsageNSec` 每次 ≤ 60 s；实盘 `okx-live@run` 的耗时与影子前相同（journal 里 :50 那次的 2–6 s） |
| 警告 | `warnings` 里没有 `code … ≠ GitHub master`（服务器代码 = GitHub master）；资金费警告每天 ≤ 1 次 |

## 4. 正式切换（用户确认后）

避开 :47–:53 与 :01–:05。

```bash
sudo git -C /opt/modi-okx-live pull --ff-only                    # 与 GitHub master 同一提交
sudo systemctl disable --now okx-signals-shadow.timer
sudo install -m 644 /opt/modi-okx-live/deploy/live/okx-signals.service /opt/modi-okx-live/deploy/live/okx-signals.timer /etc/systemd/system/
sudo install -d /etc/systemd/system/okx-live@.service.d
sudo install -m 644 /opt/modi-okx-live/deploy/live/okx-live-hourly.conf /etc/systemd/system/okx-live@.service.d/hourly-signals.conf
sudo systemctl daemon-reload
sudo systemctl start okx-signals                                  # 立即算一次 → 成功后自动触发 okx-live@run
journalctl -u okx-signals -u 'okx-live@run' --since -10min --no-pager | tail -40
#   应看到：“信号已写入 …/signals.json”，随后 okx-live@run 的 “signals.json（每小时，服务器）…” 与 “信号模式 hourly · 信号最长 135 分钟”
sudo systemctl enable --now okx-signals.timer
systemctl list-timers 'okx-*'                                     # okx-signals.timer H:01:30、okx-live.timer :50 两个都在
```

注意：`okx-live.env` 里**不要**写 `OKX_SIGNAL_MODE`（EnvironmentFile 优先于 drop-in 的 Environment=）。

切换后第一天每 4 小时继续跑 `compare_signals.py`（影子归档换成 `/var/lib/modi-okx-signals/archive`）。

## 5. 回退（任何时候，一分钟内）

```bash
sudo systemctl disable --now okx-signals.timer
sudo rm /etc/systemd/system/okx-live@.service.d/hourly-signals.conf
sudo systemctl daemon-reload
sudo systemctl start okx-live@run          # 立刻用 GitHub 信号跑一次（fetch_signals 下载，9 h / 5 h）
```

回退后线上与现在完全相同（`okx-live@.service` 本身没有改过）。影子可以重新 `enable --now okx-signals-shadow.timer` 继续观察。

## 6. 失败时会发生什么

| 情况 | 结果 |
|---|---|
| K 线到 H:05 还不齐 / OKX 不通 | `signals_hourly` 退出码 2，不写 signals.json；OnFailure 触发跟单，沿用上一小时的文件（≤ 2 h 15 min 内有效）；:50 心跳再跑一次 |
| 读不到 paper-data / CDN 给了旧版本 | 用本地上一份 state（`source/`），日志有警告；本地也没有时退出码 3 |
| state 与 team 不一致（StateDrift） | 退出码 3，不写；等 GitHub 下一次运行 |
| OKX 请求卡住 | 脚本自己在 H:05 以退出码 2 结束（不等 systemd）；OnFailure 触发跟单 |
| 重放 / 磁盘卡死 | 看门狗 H:09:30 退出码 2；OnFailure 触发跟单 |
| 超过 MemoryMax / systemd 超时（兜底） | 被杀 → OnFailure → 跟单沿用上一小时 |
| 连续 2 h 15 min 以上失败 | `fetch_signals.py` hourly 模式拒绝过期文件，`okx-live@run`（以及 `resume` / `follow-current` / `flatten`）本小时不运行（交易所止损仍在）；`okx-live@pause` 与 `okx-live@snapshot` 照常可用，此时的 pause 只设置暂停、不按过期信号平仓或改止损；`okx-live-stop`（全部平仓并暂停）不经过信号检查——与现在 GitHub 信号超过 9 h 时相同；`live_alert.py` 的“停跑”告警会报 |
