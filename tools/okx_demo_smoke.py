#!/usr/bin/env python3
"""One explicitly requested, idempotent BTC demo entry and immediate exit. No strategy signal required."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from okx_demo_trader import OKX, OKXError, fmt, quantize, local_day

INST = 'BTC-USDT-SWAP'
ENTRY_ID, EXIT_ID = 'smk20261104a', 'smk20261104closea'
BUDGET, CEILING = Decimal('10'), Decimal('20')


def checked(c, method, path, body=None):
    r = c.request(method, path, body)
    if r.get('code') != '0' or any(x.get('sCode', '0') != '0' for x in r.get('data', [])):
        raise OKXError(f'{method} {path}: {r.get("code")} {r.get("msg", "")}')
    return r.get('data', [])


def order(c, client_id):
    r = c.request('GET', f'/api/v5/trade/order?instId={INST}&clOrdId={client_id}')
    if r.get('code') == '51603':  # order does not exist
        return None
    if r.get('code') != '0' or not r.get('data'):
        raise OKXError(f'Cannot establish order state for {client_id}: {r.get("code")}')
    return r['data'][0]


def filled(c, client_id, wait=time.sleep):
    for _ in range(15):
        o = order(c, client_id)
        if o and o.get('state') == 'filled':
            return o
        if o and o.get('state') == 'canceled':
            raise OKXError(f'{client_id} was canceled; inspect any partial fill before retrying')
        wait(1)
    raise OKXError(f'{client_id} fill not confirmed; entry is not resubmitted')


def once(c, save, wait=time.sleep):
    report = dict(instrument=INST, demo=True, entry_client_id=ENTRY_ID, exit_client_id=EXIT_ID,
                  updated_at=datetime.now(timezone.utc).isoformat(), status='checking')
    save(report)
    cfg = checked(c, 'GET', '/api/v5/account/config')[0]
    if cfg.get('acctLv') == '1' or 'withdraw' in str(cfg.get('perm', '')):
        raise OKXError('Account mode/permissions unsuitable for the demo test')
    hedged = cfg.get('posMode') == 'long_short_mode'
    extra = {'posSide': 'long'} if hedged else {}
    previous = order(c, ENTRY_ID)
    previous_exit = order(c, EXIT_ID)
    if previous_exit and previous_exit.get('state') == 'filled':
        report.update(status='completed', entry=previous, exit=previous_exit)
        save(report); return report
    if previous is None:
        positions = checked(c, 'GET', f'/api/v5/account/positions?instId={INST}')
        pending = checked(c, 'GET', f'/api/v5/trade/orders-pending?instId={INST}')
        algos = checked(c, 'GET', f'/api/v5/trade/orders-algo-pending?instId={INST}&ordType=conditional')
        oco = checked(c, 'GET', f'/api/v5/trade/orders-algo-pending?instId={INST}&ordType=oco')
        if any(Decimal(p.get('pos') or '0') != 0 for p in positions) or pending or algos or oco:
            raise OKXError('BTC position or orders already exist; test left them untouched')
        spec = checked(c, 'GET', f'/api/v5/public/instruments?instType=SWAP&instId={INST}')[0]
        if spec.get('settleCcy') != 'USDT' or spec.get('ctType') != 'linear' or spec.get('state') != 'live':
            raise OKXError('Unexpected instrument contract')
        px = Decimal(checked(c, 'GET', f'/api/v5/market/ticker?instId={INST}')[0]['last'])
        value, minimum = Decimal(spec['ctVal']), Decimal(spec['minSz'])
        if px <= 0 or value <= 0:
            raise OKXError('Invalid market price or contract value')
        qty = max(minimum, quantize(BUDGET / px / value, spec['lotSz']))
        notional = qty * value * px
        if qty <= 0 or notional > CEILING:
            raise OKXError('Minimum contract size exceeds the 20 USDT test ceiling')
        balances = checked(c, 'GET', '/api/v5/account/balance?ccy=USDT')[0]['details']
        available = next((Decimal(x.get('availBal') or '0') for x in balances if x.get('ccy') == 'USDT'), Decimal('0'))
        if available < notional * Decimal('1.1'):
            raise OKXError('Insufficient available balance for isolated 1x test')
        checked(c, 'POST', '/api/v5/account/set-leverage', dict(instId=INST, lever='1', mgnMode='isolated', **extra))
        stop = fmt(quantize(px * Decimal('.99'), spec['tickSz'], ROUND_HALF_UP))
        body = dict(instId=INST, tdMode='isolated', side='buy', ordType='market', sz=fmt(qty), clOrdId=ENTRY_ID,
                    attachAlgoOrds=[dict(slTriggerPx=stop, slOrdPx='-1', slTriggerPxType='last')], **extra)
        report.update(status='entry_submitting', estimated_notional_usdt=fmt(notional), size=fmt(qty), stop=stop)
        save(report)
        # A timeout is ambiguous. Query this fixed ID; never resend the entry automatically.
        try:
            checked(c, 'POST', '/api/v5/trade/order', body)
        except OKXError:
            if order(c, ENTRY_ID) is None:
                raise
    entry = filled(c, ENTRY_ID, wait)
    report.update(status='entry_filled', entry=entry); save(report)
    qty = Decimal(entry['accFillSz'])
    if qty <= 0 or (entry.get('posSide', 'net') != ('long' if hedged else 'net')):
        raise OKXError('Unexpected entry fill or position mode')
    for _ in range(10):
        positions = checked(c, 'GET', f'/api/v5/account/positions?instId={INST}')
        positions = [p for p in positions if Decimal(p.get('pos') or '0') != 0]
        if positions:
            break
        wait(1)
    if not positions:
        raise OKXError('Entry filled but no visible BTC position; verify stop/position history, do not reopen')
    if len(positions) != 1 or Decimal(positions[0]['pos']) != qty or positions[0].get('posSide', 'net') != ('long' if hedged else 'net'):
        raise OKXError('BTC position changed independently; refusing to close an unrelated position')
    if previous_exit is None:
        body = dict(instId=INST, tdMode='isolated', side='sell', ordType='market', sz=fmt(qty), clOrdId=EXIT_ID, **extra)
        if not hedged:
            body['reduceOnly'] = True
        report['status'] = 'exit_submitting'; save(report)
        try:
            checked(c, 'POST', '/api/v5/trade/order', body)
        except OKXError:
            if order(c, EXIT_ID) is None:
                raise
    exit_order = filled(c, EXIT_ID, wait)
    remaining = checked(c, 'GET', f'/api/v5/account/positions?instId={INST}')
    if any(Decimal(p.get('pos') or '0') != 0 for p in remaining):
        raise OKXError('Exit filled but BTC still has a position; inspect manually')
    report.update(status='completed', exit=exit_order); save(report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    creds = [os.environ.get(k, '').strip() for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')]
    if not all(creds):
        raise SystemExit('OKX demo credentials are not configured')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    state_path = args.output.parent / 'state.json'
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get('paused') or (state.get('halted') and state.get('day') == local_day(datetime.now(timezone.utc))):
            raise SystemExit('Demo mirror is paused or daily loss halt is active; test refused')
    def save(report):
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    once(OKX(*creds, attempts=1), save)


if __name__ == '__main__':
    main()
