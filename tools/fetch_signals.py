#!/usr/bin/env python3
"""Fetch the evolution champions' mirror signals (paper-data/evolve/signals.json) onto the live server.

The live mirror (okx_demo_trader.py with OKX_TRADING_MODE=live) runs on the server, while the evolution itself keeps
running in GitHub Actions and publishes signals.json to the public `paper-data` branch. This copies that one file into
<data>/evolve/signals.json, atomically, after checking its shape. Public URL, no credentials; nothing else is fetched.
The trader itself refuses signals older than LIVE_SIGNAL_MAX_AGE, so a failed fetch never trades on stale data.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_guard import atomic_json  # noqa: E402

URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/evolve/signals.json'


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--url', default=URL)
    args = ap.parse_args()
    sig = fetch(args.url)
    atomic_json(args.data / 'evolve' / 'signals.json', sig)
    print(f"signals.json 已更新：{sig['updated_at']} · {len(sig['positions'])} 个信号")


if __name__ == '__main__':
    main()
