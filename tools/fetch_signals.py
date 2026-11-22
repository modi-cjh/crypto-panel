#!/usr/bin/env python3
"""Fetch the evolution champions' mirror signals (paper-data/evolve/signals.json) onto the live server.

The live mirror (okx_demo_trader.py with OKX_TRADING_MODE=live) runs on the server, while the evolution itself keeps
running in GitHub Actions and publishes signals.json to the public `paper-data` branch. This copies that one file into
<data>/evolve/signals.json, atomically, after checking its shape. Public URL, no credentials; nothing else is fetched.
The trader itself refuses signals older than LIVE_SIGNAL_MAX_AGE, so a failed fetch never trades on stale data.

Signal mode (env OKX_SIGNAL_MODE, the same switch okx_demo_trader.signal_timing reads; set by the systemd drop-in
deploy/live/okx-live-hourly.conf, never in the key file):
  github (default, unset)  download paper-data/evolve/signals.json as above — unchanged; also during the shadow run.
  hourly                   no download: check the local file written by tools/evolve/signals_hourly.py
                           (OKX_HOURLY_SIGNALS, default /var/lib/modi-okx-signals/evolve/signals.json): shape,
                           source = server-hourly, not a shadow / legacy-state file, age ≤ the trader's max age, last bar
                           not older than the copy already in <data>; then copy it atomically into <data>/evolve/.
                           --action (the drop-in passes the instance name): a file that fails the check stops every action
                           that needs signals; for NO_SIGNAL_ACTIONS (pause, snapshot) it is only reported and the copy in
                           <data> is left as it is — those two must always be possible. No --action = needs signals.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_guard import atomic_json  # noqa: E402

URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/evolve/signals.json'
HOURLY_FILE = Path('/var/lib/modi-okx-signals/evolve/signals.json')
HOURLY_SOURCE = 'server-hourly'
NO_SIGNAL_ACTIONS = ('pause', 'snapshot')


def valid(sig):
    """signals.json as written by tools/evolve/run.py: updated_at, instruments (USDT swaps), positions {inst: signal}."""
    if not isinstance(sig, dict) or not isinstance(sig.get('updated_at'), str):
        return False
    insts, pos = sig.get('instruments'), sig.get('positions')
    if not isinstance(insts, list) or not isinstance(pos, dict):
        return False
    if not all(isinstance(i, str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', i) for i in insts):
        return False
    need = ('side', 'entry', 'stop', 'opened')
    return all(isinstance(p, dict) and all(k in p for k in need) and p['side'] in (1, -1) for p in pos.values())


def fetch(url=URL, curl='curl'):
    out = subprocess.run([curl, '-sS', '-f', '-m', '30', '-H', 'Cache-Control: no-cache', url], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f'下载 signals.json 失败（curl 退出码 {out.returncode}）')
    try:
        sig = json.loads(out.stdout)
    except ValueError:
        raise SystemExit('signals.json 不是有效 JSON') from None
    if not valid(sig):
        raise SystemExit('signals.json 格式不符，未覆盖本地文件')
    return sig


def local_hourly(src, dest, now=None, env=None):
    """hourly mode: the server's own signals file, checked, or SystemExit (the trader then does not run this hour —
    the same as a failed download in github mode; the exchange stops stay in place)."""
    from okx_demo_trader import signal_timing, signal_age_ms       # one definition of the max age for both
    _, max_age, _ = signal_timing(env)
    try:
        sig = json.loads(Path(src).read_text())
    except OSError:
        raise SystemExit(f'每小时信号文件不存在或不可读：{src}') from None
    except ValueError:
        raise SystemExit('每小时信号文件不是有效 JSON') from None
    if not valid(sig):
        raise SystemExit('每小时信号文件格式不符，未覆盖本地文件')
    if sig.get('source') != HOURLY_SOURCE or sig.get('shadow') or sig.get('legacy_state'):
        raise SystemExit('每小时信号文件不是服务器正式信号（source / shadow / legacy_state 不符），未覆盖本地文件')
    age = signal_age_ms(sig, now or datetime.now(timezone.utc))
    if age is None or age > max_age:
        raise SystemExit(f'每小时信号已过期或时间戳异常（超过 {max_age // 60_000} 分钟，signals_hourly 可能连续失败），未覆盖本地文件')
    try:
        cur = json.loads(Path(dest).read_text())
    except (OSError, ValueError):
        cur = None
    if isinstance(cur, dict) and isinstance(cur.get('last_bar_ts'), int) and isinstance(sig.get('last_bar_ts'), int) \
            and sig['last_bar_ts'] < cur['last_bar_ts']:
        print('本地 signals.json 的 K 线比每小时信号新，保留本地文件')
        return cur
    return sig


def main(argv=None, env=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--url', default=URL)
    ap.add_argument('--action', default='run', help='okx-live@ instance name; only used in hourly mode')
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    mode = str(env.get('OKX_SIGNAL_MODE', '')).strip().lower() or 'github'
    dest = args.data / 'evolve' / 'signals.json'
    if mode == 'hourly':
        try:
            sig = local_hourly(Path(env.get('OKX_HOURLY_SIGNALS') or HOURLY_FILE), dest, env=env)
        except SystemExit as exc:
            if args.action not in NO_SIGNAL_ACTIONS:
                raise
            print(f'警告：{exc}；{args.action} 不需要信号，继续')
            return
        atomic_json(dest, sig)
        print(f"signals.json（每小时，服务器）：{sig['updated_at']} · 最后 K 线 {sig.get('last_bar')} · {len(sig['positions'])} 个信号")
        return
    if mode != 'github':
        raise SystemExit('OKX_SIGNAL_MODE 只能是 github 或 hourly，已拒绝运行')
    sig = fetch(args.url)
    atomic_json(dest, sig)
    print(f"signals.json 已更新：{sig['updated_at']} · {len(sig['positions'])} 个信号")


if __name__ == '__main__':
    main()
