#!/usr/bin/env python3
"""Pause new entries, close this program's positions, and verify exchange positions.

Follows the trader's mode switch (OKX_TRADING_MODE): demo works on <data>/okx_demo, live on <data>/okx_live.
Live closes are real orders, so they need OKX_DRY_RUN=0; in live dry-run the program never opened anything and this exits."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from okx_demo_trader import (OKX, OKXError, Mirror, configure_volume_test, dashboard_actions, decision_snapshot,
                             trading_mode, dry_run_forced, state_dir, load_state)
from runtime_guard import atomic_json


def stop(data, client, wait=time.sleep, mode='demo'):
    out = state_dir(data, mode); out.mkdir(parents=True, exist_ok=True)
    path = out / 'state.json'
    state = load_state(path, mode)
    targets = {k: m for k, m in state['mirrored'].items() if m['status'] == 'open'}
    state['paused'] = True
    atomic_json(path, state)
    public = out / 'summary.json'
    summary = json.loads(public.read_text()) if public.exists() else None
    control = dict(status='pending', requested_at=datetime.now(timezone.utc).isoformat())
    if summary:
        summary.update(paused=True, stop_control=control)
        atomic_json(public, summary)
    now = datetime.now(timezone.utc)
    mirror = Mirror(client, state, {'positions': {}}, now)
    error = None
    try:
        mirror.run('flatten')
        remaining = []
        for _ in range(5):
            r = client.request('GET', '/api/v5/account/positions?instType=SWAP')
            if r.get('code') != '0':
                raise OKXError('Cannot verify exchange positions after stop')
            remaining = [m for m in targets.values() if any(p.get('instId') == m['inst'] and
                         p.get('posSide', 'net') == m['posSide'] and float(p.get('pos') or 0) != 0 for p in r.get('data', []))]
            if not remaining:
                break
            wait(2)
        for m in remaining:
            m['status'] = 'open'  # A submitted close is not a confirmed fill.
        if remaining:
            raise OKXError('Some program positions are still open; new entries remain paused')
        mirror.load()
        mirror.record_equity()
        summary = mirror.summary()
        control.update(status='completed', completed_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        error = str(exc)
        for m in targets.values():
            m['status'] = 'open'  # Reconcile uncertain closes on the next run; never drop their management.
        control.update(status='failed', message='已暂停新开仓，平仓结果未全部确认，请检查任务回执。')
    finally:
        state['paused'] = True
        state['stop_control'] = control
        atomic_json(path, state)
        log = out / 'actions.jsonl'
        with log.open('a') as f:
            f.writelines(json.dumps(a, ensure_ascii=False) + '\n' for a in mirror.actions)
        if summary:
            summary.update(decision_snapshot(data))
            rows = [json.loads(l) for l in log.read_text().splitlines()[-30:]]
            summary['recent_actions'], tests_closed = dashboard_actions(out, rows)
            summary.update(paused=True, stop_control=control, execution_test_trades=tests_closed,
                           closed_trades=sum(m['status'] == 'done' for m in state['mirrored'].values()) + tests_closed)
            atomic_json(public, summary)
    if error:
        raise OKXError(error)
    return control


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', type=Path, required=True)
    args = ap.parse_args()
    mode = trading_mode()
    if dry_run_forced(mode):
        raise SystemExit('dry-run 模式下程序不会开仓，也不会发送平仓；实盘真实平仓需同时设置 OKX_DRY_RUN=0。')
    creds = [os.environ.get(k, '').strip() for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')]
    if not all(creds):
        raise SystemExit('OKX credentials are missing')
    signal_path = args.data / 'combined' / 'state.json'
    signal_state = json.loads(signal_path.read_text()) if signal_path.exists() else {}
    configure_volume_test(universe=signal_state.get('universe') if signal_state.get('profile_mode') == 'aggressive' else None)
    print(json.dumps(stop(args.data, OKX(*creds, mode=mode, dry_run=False), mode=mode), ensure_ascii=False))


if __name__ == '__main__':
    main()
